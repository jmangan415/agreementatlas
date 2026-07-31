from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from email import policy
from email.parser import BytesParser
from email.utils import formatdate
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

import conversation
from legal_graph_service import (
    answer_question,
    compact_graph,
    enrich_workspace,
    enrichment_coverage,
    prune_stale_enrichment,
    read_jsonl,
    restore_effective_graph,
    schema_status,
)
from legal_ingest import SUPPORTED, rebuild_workspace
from library_store import LibraryStore, clean_name
from lmstudio_client import LMStudioClient, LMStudioError
from session_store import RateLimiter, RateLimitError, SessionStore, VisitorSession

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA_ROOT = ROOT / "data" / "sessions"
load_dotenv(ROOT / ".env")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_MODE = os.environ.get("APP_MODE", "local").strip().lower()
SESSION_COOKIE = "agreementatlas_session"
SESSION_TTL_HOURS = env_int("SESSION_TTL_HOURS", 6)
MAX_FILES = env_int("MAX_FILES_PER_SESSION", 12)
MAX_TOTAL_BYTES = env_int("MAX_SESSION_BYTES", 50 * 1024 * 1024)
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES + 2 * 1024 * 1024
COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE")
TRUST_CLOUDFLARE = env_bool("TRUST_CLOUDFLARE_HEADERS")
OPERATOR_NAME = os.environ.get(
    "PUBLIC_OPERATOR_NAME", "The person running this local copy of AgreementAtlas"
).strip()
PRIVACY_EMAIL = os.environ.get(
    "PRIVACY_CONTACT_EMAIL", "Not applicable in local-only mode"
).strip()

# A local install keeps its work; a public demo must not. Everything downstream
# reads this one flag rather than testing APP_MODE in a dozen places.
PERSISTENT = APP_MODE != "public-demo"
LIBRARY_ROOT = Path(
    os.environ.get("LIBRARY_ROOT", "") or (ROOT / "data" / "library")
).expanduser()

session_store = SessionStore(DATA_ROOT, ttl_seconds=SESSION_TTL_HOURS * 60 * 60)
library_store = LibraryStore(LIBRARY_ROOT) if PERSISTENT else None
rate_limiter = RateLimiter()
lm_client = LMStudioClient()
# Two independent gates on the local model. An enrichment holds its slot for
# the whole run -- minutes -- and used to hold the only slot, so every chat
# question during an enrichment answered 429. Questions now have their own
# lane: the app stays answerable while it extracts. (LM Studio itself should
# allow 2 parallel predictions, or the second request queues behind the first
# batch for a few seconds; it still answers either way.)
enrich_slots = threading.BoundedSemaphore(env_int("LMSTUDIO_MAX_CONCURRENT_JOBS", 1))
query_slots = threading.BoundedSemaphore(env_int("LMSTUDIO_MAX_CONCURRENT_QUERIES", 1))

# The public demo nests a family library inside every visitor session, so the
# pre-built samples and the visitor's own uploads are separate workspaces that
# share one six-hour lifetime. One store per session id, so concurrent requests
# from the same visitor share one per-family lock table.
MAX_PUBLIC_FAMILIES = env_int("MAX_PUBLIC_FAMILIES", 3)
session_library_guard = threading.Lock()
session_libraries: dict[str, LibraryStore] = {}


def visitor_library(visitor: VisitorSession) -> LibraryStore:
    with session_library_guard:
        store = session_libraries.get(visitor.id)
        if store is None:
            store = LibraryStore(visitor.root / "families")
            session_libraries[visitor.id] = store
        return store


def drop_visitor_libraries(session_ids: list[str]) -> None:
    if not session_ids:
        return
    with session_library_guard:
        for session_id in session_ids:
            session_libraries.pop(session_id, None)


# name -> (mtime_ns, digest). Recomputed only when the file changes.
ASSET_VERSIONS: dict[str, tuple[int, str]] = {}

job_guard = threading.RLock()
jobs: dict[str, dict] = {}
lm_status_guard = threading.Lock()
lm_status_cache: tuple[float, dict] = (0.0, {})


class APIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def safe_name(name: str) -> str:
    normalised = str(name or "").replace("\\", "/")
    base = normalised.rsplit("/", 1)[-1]
    suffix = Path(base).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._")
    return f"{stem or 'document'}{suffix}"


def validate_upload_content(name: str, content: bytes) -> None:
    suffix = Path(name).suffix.lower()
    if not content:
        raise APIError(400, "empty_file", f"{name} is empty.")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise APIError(415, "file_type_mismatch", f"{name} is not a valid PDF.")
    if suffix in {".docx", ".xlsx", ".pptx"}:
        if not content.startswith(b"PK"):
            raise APIError(
                415, "file_type_mismatch", f"{name} is not a valid Office file."
            )
        expected = {
            ".docx": "word/",
            ".xlsx": "xl/",
            ".pptx": "ppt/",
        }[suffix]
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                expanded = sum(item.file_size for item in members)
                names = {item.filename for item in members}
                if (
                    expanded > 200 * 1024 * 1024
                    or "[Content_Types].xml" not in names
                    or not any(item.startswith(expected) for item in names)
                ):
                    raise ValueError
        except (ValueError, zipfile.BadZipFile):
            raise APIError(
                415, "file_type_mismatch", f"{name} is not a valid Office file."
            ) from None
    if suffix == ".xls" and not content.startswith(b"\xd0\xcf\x11\xe0"):
        raise APIError(415, "file_type_mismatch", f"{name} is not a valid Excel file.")
    if suffix in {".txt", ".md", ".csv"} and b"\x00" in content[:8192]:
        raise APIError(
            415, "file_type_mismatch", f"{name} does not look like a text file."
        )


def parse_multipart_files(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise APIError(415, "content_type", "Upload files using multipart/form-data.")
    message = BytesParser(policy=policy.default).parsebytes(
        (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
        + body
    )
    if not message.is_multipart():
        raise APIError(400, "multipart", "The upload body is not valid multipart data.")
    files: list[tuple[str, bytes]] = []
    for part in message.iter_parts():
        if (
            part.get_content_disposition() == "form-data"
            and part.get_param("name", header="content-disposition") == "files"
            and part.get_filename()
        ):
            files.append(
                (
                    str(part.get_filename()),
                    part.get_payload(decode=True) or b"",
                )
            )
    if not files:
        raise APIError(400, "no_files", "Choose at least one agreement to upload.")
    return files


def workspace_documents(root: Path) -> list[dict]:
    sizes = {
        path.name: path.stat().st_size
        for path in (root / "sources").iterdir()
        if path.is_file()
    }
    records = []
    for document in read_jsonl(root / "legal" / "documents.jsonl"):
        source = str(document.get("source", ""))
        records.append(
            {
                "id": document.get("id", ""),
                "name": source,
                "title": document.get("title", source),
                "document_type": document.get("document_type", "AGREEMENT"),
                "instrument_class": document.get("instrument_class", ""),
                "instrument_type": document.get("instrument_type", ""),
                "version": document.get("version", ""),
                "effective_date": document.get("effective_date", ""),
                "size": sizes.get(source, 0),
            }
        )
    return records


def workspace_usage(root: Path) -> tuple[int, int]:
    files = [path for path in (root / "sources").iterdir() if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


# Extracted by the language model, not by the deterministic rebuild, so a fresh
# ingest cannot reproduce them. Carried across a re-ingest or a family that grows
# by one document pays for a full re-extraction of every document it already had.
ENRICHMENT_ARTEFACTS = (
    "lm_rules.jsonl",
    "resolved_rules.jsonl",
    "deep_build_checkpoint.json",
)


def carry_enrichment(current: Path, staging: Path) -> None:
    """Copy model-extracted artefacts from the live workspace into the new one."""

    source = current / "legal"
    target = staging / "legal"
    if not source.is_dir() or not target.is_dir():
        return
    for name in ENRICHMENT_ARTEFACTS:
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, target / name)


def replace_workspace(root: Path, staging: Path) -> None:
    carry_enrichment(root, staging)
    token = secrets.token_hex(8)
    backup = root / f".previous-{token}"
    backup.mkdir()
    moved: list[str] = []
    installed: list[str] = []
    try:
        for name in ("sources", "input", "legal", "output"):
            current = root / name
            if current.exists():
                current.rename(backup / name)
                moved.append(name)
        for name in ("sources", "input", "legal", "output"):
            (staging / name).rename(root / name)
            installed.append(name)
    except Exception:
        for name in installed:
            shutil.rmtree(root / name, ignore_errors=True)
        for name in moved:
            previous = backup / name
            if previous.exists():
                previous.rename(root / name)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    # Carried rules may refer to clauses of a document that is no longer here.
    prune_stale_enrichment(root)
    # The rebuild above produced a baseline graph. Where enrichment survived,
    # put it back into the graph rather than leaving the rules unreachable.
    restore_effective_graph(root)


DEMO_ROOT = Path(
    os.environ.get("DEMO_BUNDLES", "") or (ROOT / "samples" / "demo_bundles")
)
# The bundle a visitor gets before choosing. OpenText leads because its trap
# questions are the measured ones (model_bench answers all seven correctly on
# this exact workspace) and the operator can defend every answer in person.
DEMO_ORDER = ("opentext", "sap")
# Written into a workspace that holds the sample, so the interface can label it
# as sample data for as long as it is loaded rather than only before it is.
SAMPLE_MARKER = ".sample"


def sample_flagged(family) -> dict:
    """A family's public record plus whether it is a shipped sample.

    The client used to guess from names, which filed the SAP bundle under the
    owner's families the moment its name drifted from the catalogue's. The
    marker the sample loader already writes is the fact itself.
    """

    return {
        **family.public_record(),
        "is_sample": (family.root / SAMPLE_MARKER).exists(),
    }


def demo_manifests() -> dict[str, dict]:
    """Every installed sample bundle, keyed by directory name, default first."""

    found: dict[str, dict] = {}
    if not DEMO_ROOT.is_dir():
        return found
    for directory in sorted(DEMO_ROOT.iterdir()):
        manifest = directory / "demo.json"
        if not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            found[directory.name] = value
    return {
        slug: found[slug] for slug in (*DEMO_ORDER, *sorted(found)) if slug in found
    }


def demo_manifest() -> dict:
    """The default sample bundle's manifest, or {} if none is installed."""

    for manifest in demo_manifests().values():
        return manifest
    return {}


def sample_catalogue() -> list[dict]:
    """What the interface needs to offer each installed sample bundle."""

    return [
        {
            "slug": slug,
            "name": manifest.get("name", slug),
            "short_name": manifest.get("short_name", manifest.get("name", slug)),
            "headline": manifest.get("headline", ""),
            "source_url": manifest.get("source_url", ""),
            "documents": len(manifest.get("documents", [])),
            "clauses": manifest.get("clauses", 0),
            "definitions": manifest.get("definitions", 0),
            "enriched": bool(manifest.get("enriched")),
            "questions": [str(item) for item in manifest.get("questions", [])[:8]],
        }
        for slug, manifest in demo_manifests().items()
    ]


def autoload_sample(visitor) -> None:
    """Give a first-time visitor both sample families, ready to explore.

    A public visitor lands on a session whose library is empty, and the terms
    correctly tell them not to upload a real agreement -- so without this,
    everything the product does is invisible to anyone unwilling to hand over
    a contract. Each installed bundle becomes its own pre-enriched family; the
    visitor's own uploads go into families they create beside these.

    It runs on the status call, which is the first thing the interface asks
    for, and only while the session's library is completely empty: a visitor
    who deletes a sample family has said they do not want it back.
    """

    if PERSISTENT:
        return
    manifests = demo_manifests()
    if not manifests:
        return
    library = visitor_library(visitor)
    if library.list():
        return
    # Install in reverse catalogue order: the list the interface shows is
    # most-recently-touched first, so the catalogue's default bundle must be
    # the last one created here.
    for slug in reversed(list(manifests)):
        try:
            family = library.create(str(manifests[slug].get("name", slug)))
            with library.lock_for(family.id):
                install_bundle(family.root, slug)
        except (APIError, OSError):
            # A missing or half-installed bundle must not stop the page
            # loading; the interface copes with whatever families exist.
            continue


def install_bundle(root: Path, slug: str = "") -> dict:
    """Copy a prebuilt sample bundle into a workspace, replacing its contents.

    The bundle is already parsed, enriched and embedded, so this is a file copy
    rather than minutes of model work, and it cannot be used to skip the
    enrichment rate limit: nothing here calls the model.

    Replace rather than merge. Merging a prebuilt graph into whatever the
    workspace already held would produce a family whose precedence rules span
    two unrelated agreements and answer questions about neither.
    """

    manifests = demo_manifests()
    if not manifests:
        raise APIError(
            503,
            "no_demo",
            "The sample family is not installed on this deployment.",
        )
    chosen = slug or next(iter(manifests))
    manifest = manifests.get(chosen)
    if manifest is None:
        raise APIError(404, "no_demo", "That sample family is not installed.")
    bundle = DEMO_ROOT / chosen
    source = bundle / "legal"
    if not source.is_dir():
        raise APIError(503, "no_demo", "The sample family is incomplete.")

    for name in ("legal", "sources", "input", "output"):
        target = root / name
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    for name in ("legal", "sources", "input", "output"):
        origin = bundle / name
        if origin.is_dir():
            shutil.copytree(origin, root / name)

    # Mark the workspace so the interface can say so. The client used to infer
    # it by testing whether a document name began with "01-BASE_EULA_UK-Ireland",
    # which broke silently the moment the bundle was renamed and could never
    # distinguish the sample from a visitor's own upload of a similarly named
    # file. A marker written where the copy happens cannot drift from it.
    (root / SAMPLE_MARKER).write_text(
        manifest.get("name", "Sample family"), encoding="utf-8"
    )
    return manifest


def load_demo_family(visitor, slug: str = "") -> dict:
    """Install a sample bundle into this workspace and describe the result."""

    manifest = install_bundle(visitor.root, slug)
    status = schema_status(visitor.root)
    return {
        "loaded": True,
        "slug": slug or next(iter(demo_manifests())),
        "name": manifest.get("name", "Sample family"),
        "documents": workspace_documents(visitor.root),
        "clauses": manifest.get("clauses", 0),
        "definitions": manifest.get("definitions", 0),
        "enriched": bool(manifest.get("enriched")),
        "build_mode": status["build_mode"],
        "schema_version": status["schema_version"],
    }


def ingest_uploads(visitor, uploaded: list[tuple[str, bytes]], store) -> dict:
    if not PERSISTENT and (visitor.root / SAMPLE_MARKER).exists():
        raise APIError(
            409,
            "sample_family",
            "Samples are read-only here. Create your own agreement family to "
            "upload agreements.",
        )
    if len(uploaded) > MAX_FILES:
        raise APIError(
            413, "file_count", f"A session can contain at most {MAX_FILES} files."
        )
    prepared: list[tuple[str, bytes]] = []
    names: set[str] = set()
    for original_name, content in uploaded:
        name = safe_name(original_name)
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED:
            raise APIError(
                415,
                "unsupported_file",
                f"{suffix or 'This file type'} is not supported.",
            )
        if name in names:
            raise APIError(
                409, "duplicate_name", f"The upload contains {name} more than once."
            )
        validate_upload_content(name, content)
        prepared.append((name, content))
        names.add(name)

    with store.lock_for(visitor.id):
        active = store.get(visitor.id)
        if not active:
            raise APIError(
                410, "session_expired", "This workspace is no longer available."
            )
        current_files = [
            path for path in (active.root / "sources").iterdir() if path.is_file()
        ]
        current_names = {path.name for path in current_files}
        duplicates = sorted(current_names & names)
        if duplicates:
            raise APIError(
                409,
                "duplicate_name",
                f"{duplicates[0]} is already in this agreement family.",
            )
        if len(current_files) + len(prepared) > MAX_FILES:
            raise APIError(
                413, "file_count", f"A session can contain at most {MAX_FILES} files."
            )
        current_bytes = sum(path.stat().st_size for path in current_files)
        new_bytes = sum(len(content) for _, content in prepared)
        if current_bytes + new_bytes > MAX_TOTAL_BYTES:
            raise APIError(
                413,
                "session_size",
                "These files would exceed the 50 MB session allowance.",
            )

        staging = active.root / f".staging-{secrets.token_hex(8)}"
        sources = staging / "sources"
        sources.mkdir(parents=True)
        try:
            for source in current_files:
                shutil.copy2(source, sources / source.name)
            for name, content in prepared:
                target = sources / name
                target.write_bytes(content)
                target.chmod(0o600)
            summary = rebuild_workspace(staging)
            if summary["documents"] != len(current_files) + len(prepared):
                raise APIError(
                    422,
                    "unreadable_file",
                    "One or more files did not contain readable agreement text.",
                )
            replace_workspace(active.root, staging)
            # Whatever this workspace is now, it is no longer only the sample,
            # so it must stop being labelled as one.
            (active.root / SAMPLE_MARKER).unlink(missing_ok=True)
        except APIError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise APIError(
                422,
                "conversion_failed",
                "One or more files could not be safely converted. The existing session was unchanged.",
            ) from None
    if isinstance(store, LibraryStore):
        current = store.get(visitor.id)
        if current and current.name in {"", "Untitled family"}:
            # An unnamed family takes the name the ingest derived for it, so the
            # sidebar reads as documents rather than as "Untitled family (3)".
            families = read_jsonl(active.root / "legal" / "agreement_families.jsonl")
            derived = str(families[0].get("title", "")) if families else ""
            store.update(visitor.id, name=derived or "Untitled family")
        else:
            store.touch(visitor.id)
    return {
        "saved": [{"name": name, "size": len(content)} for name, content in prepared],
        "legal_index": summary,
    }


def public_lm_status(*, refresh: bool = False) -> dict:
    global lm_status_cache
    now = time.monotonic()
    with lm_status_guard:
        timestamp, cached = lm_status_cache
        if cached and not refresh and now - timestamp < 5:
            return cached
        raw = lm_client.status()
        loaded_models = [
            item for item in raw.get("models", []) if item.get("type", "llm") == "llm"
        ]
        allowed = (
            lm_client.allowed_model_ids()
            if hasattr(lm_client, "allowed_model_ids")
            else {str(item.get("id", "")) for item in loaded_models}
        )
        loaded_models = [
            item
            for item in loaded_models
            if str(item.get("id", "")) in allowed or str(item.get("key", "")) in allowed
        ]
        result = {
            "available": bool(raw.get("available")),
            "models": loaded_models,
            "downloaded_count": raw.get("downloaded_count", len(loaded_models)),
            "extractor": raw.get("extractor"),
            "embedder": raw.get("embedder"),
            "automanage": bool(raw.get("automanage", False)),
        }
        if result["available"] and not result["models"]:
            result["available"] = False
            result["message"] = "LM Studio is running, but no model is loaded."
        elif not result["available"]:
            result["message"] = (
                "LM Studio is offline. Start its local server and load an instruct model."
            )
        configured = os.environ.get("LMSTUDIO_MODEL", "")
        if not configured and hasattr(lm_client, "extractor_model"):
            configured = lm_client.extractor_model
        model_ids = [str(item.get("id", "")) for item in result["models"]]
        result["selected_model"] = (
            configured
            if configured in model_ids
            else (model_ids[0] if model_ids else "")
        )
        lm_status_cache = (now, result)
        return result


def select_model(requested: str) -> str:
    status = public_lm_status(refresh=True)
    if not status["available"]:
        raise APIError(503, "lmstudio_offline", status["message"])
    model_ids = {str(item.get("id", "")) for item in status["models"] if item.get("id")}
    model = requested or str(status.get("selected_model", ""))
    if model not in model_ids:
        raise APIError(400, "invalid_model", "Choose an available LM Studio model.")
    return model


def safe_lm_error(exc: Exception) -> str:
    message = str(exc)
    allowed = (
        "valid structured JSON",
        "wrong JSON shape",
        "no model is available",
        "Upload agreements",
        "do not contain relevant evidence",
        "cancelled",
    )
    if any(fragment.lower() in message.lower() for fragment in allowed):
        return message
    return "The local model request failed. Check LM Studio and try another instruct model."


def enrichment_status(session_id: str) -> dict:
    with job_guard:
        value = jobs.get(session_id)
        if not value:
            return {
                "state": "idle",
                "stage": "idle",
                "completed": 0,
                "total": 0,
                "completed_batches": 0,
                "total_batches": 0,
            }
        return {
            key: item
            for key, item in value.items()
            if key
            in {
                "state",
                "stage",
                "model",
                "completed",
                "total",
                "completed_batches",
                "total_batches",
                "error",
                "summary",
            }
        }


def cancel_jobs(session_ids: list[str]) -> None:
    if not session_ids:
        return
    with job_guard:
        for session_id in session_ids:
            if session_id in jobs:
                jobs[session_id]["cancel_requested"] = True


def start_enrichment(visitor, model: str, store) -> dict:
    with job_guard:
        current = jobs.get(visitor.id)
        if current and current.get("state") == "running":
            raise APIError(
                409,
                "enrichment_running",
                "Enrichment is already running for this session.",
            )
        if not enrich_slots.acquire(blocking=False):
            raise APIError(
                429,
                "model_busy",
                "Another enrichment is already using the local model. "
                "Please try again shortly.",
            )
        job_id = secrets.token_hex(16)
        jobs[visitor.id] = {
            "id": job_id,
            "state": "running",
            "stage": "extracting",
            "model": model,
            "completed": 0,
            "total": 0,
            "completed_batches": 0,
            "total_batches": 0,
            "cancel_requested": False,
        }

    def is_cancelled() -> bool:
        # A family that has been deleted mid-run must stop the job -- and in
        # public mode a family vanishes with its session, so asking the store
        # that owns this workspace covers expiry too.
        if not store.is_active(visitor.id):
            return True
        with job_guard:
            value = jobs.get(visitor.id, {})
            return value.get("id") != job_id or bool(value.get("cancel_requested"))

    def progress(completed: int, total: int) -> None:
        with job_guard:
            value = jobs.get(visitor.id)
            if value and value.get("id") == job_id:
                value["completed_batches"] = completed
                value["total_batches"] = total
                value["completed"] = completed
                value["total"] = total
                value["stage"] = "extracting"

    def run() -> None:
        try:
            summary = enrich_workspace(
                visitor.root,
                lm_client,
                model,
                progress=progress,
                cancelled=is_cancelled,
            )
            if isinstance(store, LibraryStore) and store.get(visitor.id):
                store.update(visitor.id, enriched=True, enrichment_model=model)
            with job_guard:
                value = jobs.get(visitor.id)
                if value and value.get("id") == job_id:
                    value["state"] = "complete"
                    value["stage"] = "complete"
                    value["summary"] = summary
                    value["completed"] = summary.get(
                        "completed", value.get("completed", 0)
                    )
                    value["total"] = summary.get(
                        "clauses_considered", value.get("total", 0)
                    )
        except Exception as exc:
            with job_guard:
                value = jobs.get(visitor.id)
                if value and value.get("id") == job_id:
                    value["state"] = "cancelled" if is_cancelled() else "error"
                    value["stage"] = value["state"]
                    if value["state"] == "error":
                        value["error"] = safe_lm_error(exc)
        finally:
            enrich_slots.release()

    threading.Thread(
        target=run, name=f"agreementatlas-enrich-{visitor.id[:8]}", daemon=True
    ).start()
    return {"started": True, "model": model}


def validate_public_configuration() -> None:
    if APP_MODE not in {"local", "public-demo"}:
        raise RuntimeError("APP_MODE must be local or public-demo.")
    if APP_MODE != "public-demo":
        return
    missing = [
        name
        for name in ("PUBLIC_OPERATOR_NAME", "PRIVACY_CONTACT_EMAIL", "PUBLIC_BASE_URL")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError("Public demo mode requires: " + ", ".join(sorted(missing)))
    if not COOKIE_SECURE:
        raise RuntimeError("Public demo mode requires SESSION_COOKIE_SECURE=true.")
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError(
            "The temporary public demo must remain loopback-bound behind the tunnel."
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "AgreementAtlas/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:
        path = urlparse(self.path).path
        print(f"[web] {self.command} {path} {args[1] if len(args) > 1 else ''}")

    def _cookie_value(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        item = cookie.get(SESSION_COOKIE)
        return item.value if item else None

    def ensure_session(self) -> VisitorSession:
        expired = session_store.cleanup_expired()
        cancel_jobs(expired)
        drop_visitor_libraries(expired)
        visitor, created = session_store.get_or_create(self._cookie_value())
        self.visitor = visitor
        self.set_session_cookie = created
        self.clear_session_cookie = False
        return visitor

    def family_param(self) -> str:
        return parse_qs(urlparse(self.path).query).get("family", [""])[0]

    def selected_family(self):
        """The agreement family this request addresses, or None.

        Local mode never creates a workspace implicitly: an empty library is a
        real state the interface has to show, and auto-creating on every
        cookieless visit littered the disk with empty directories.
        """

        assert library_store is not None
        return library_store.get(self.family_param())

    def require_family(self):
        family = self.selected_family()
        if family is None:
            raise APIError(
                404, "no_family", "Select or create an agreement family first."
            )
        return family

    def session_family(self, session):
        """The family this request addresses inside the visitor's own library."""

        return visitor_library(session).get(self.family_param())

    def require_session_family(self, session):
        family = self.session_family(session)
        if family is None:
            raise APIError(
                404, "no_family", "Select or create an agreement family first."
            )
        return family

    def ensure_workspace(self):
        """The workspace for this request under either storage model."""

        if PERSISTENT:
            return self.require_family()
        session = self.ensure_session()
        self.visitor_session = session
        return self.require_session_family(session)

    def owning_store(self):
        """The store that holds this request's workspace."""

        if PERSISTENT:
            return library_store
        return visitor_library(self.visitor_session)

    def client_key(self) -> str:
        value = self.client_address[0]
        if TRUST_CLOUDFLARE and value in {"127.0.0.1", "::1"}:
            candidate = self.headers.get("CF-Connecting-IP", "")
            try:
                value = str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return value

    def check_rate(self, scope: str, visitor, *, limit: int, window: int) -> None:
        rate_limiter.check(f"{scope}:session", visitor.id, limit=limit, window=window)
        rate_limiter.check(
            f"{scope}:address", self.client_key(), limit=limit * 3, window=window
        )

    def _session_cookie_header(self, visitor: VisitorSession) -> str:
        remaining = max(0, visitor.expires_at - int(time.time()))
        parts = [
            f"{SESSION_COOKIE}={visitor.id}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={remaining}",
            f"Expires={formatdate(visitor.expires_at, usegmt=True)}",
        ]
        if COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _common_headers(
        self, content_type: str, length: int, *, cache: str = "no-store"
    ) -> None:
        self.send_header("Content-Type", content_type)
        # A stream has no length to declare; anything else must state one.
        if length:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # Announced only for the public deployment: sent from a local install it
        # would teach the browser to force HTTPS on 127.0.0.1, which has no
        # certificate, and lock the developer out of their own copy.
        if not PERSISTENT:
            self.send_header(
                "Strict-Transport-Security", "max-age=15552000; includeSubDomains"
            )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; "
            "style-src 'self'",
        )
        if getattr(self, "clear_session_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; "
                "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
            )
        elif getattr(self, "set_session_cookie", False):
            self.send_header("Set-Cookie", self._session_cookie_header(self.visitor))

    def sse_start(self) -> None:
        """Begin a server-sent-event stream.

        The answer takes tens of seconds on a local model. Held until complete
        it arrives as a wall of text after a blank wait; streamed, the reader
        watches it being written and can stop reading when they have what they
        asked for.
        """

        self.send_response(200)
        self._common_headers("text/event-stream; charset=utf-8", 0)
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def sse_send(self, event: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def json_response(
        self, payload: dict | list, code: int = 200, *, extra: dict | None = None
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._common_headers("application/json; charset=utf-8", len(body))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def error_response(self, exc: APIError | RateLimitError) -> None:
        if isinstance(exc, RateLimitError):
            self.json_response(
                {
                    "error": "Too many requests. Please wait and try again.",
                    "code": "rate_limited",
                },
                429,
                extra={"Retry-After": str(exc.retry_after)},
            )
            return
        self.json_response({"error": exc.message, "code": exc.code}, exc.status)

    def read_body(self, maximum: int) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise APIError(411, "content_length", "Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError:
            raise APIError(
                400, "content_length", "Content-Length is invalid."
            ) from None
        if length < 0 or length > maximum:
            raise APIError(413, "request_size", "The request is too large.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise APIError(400, "incomplete_request", "The request body is incomplete.")
        return body

    def read_json(self) -> dict:
        if (
            not self.headers.get("Content-Type", "")
            .lower()
            .startswith("application/json")
        ):
            raise APIError(415, "content_type", "Send JSON as application/json.")
        try:
            value = json.loads(self.read_body(64 * 1024) or b"{}")
        except json.JSONDecodeError:
            raise APIError(400, "invalid_json", "The JSON body is invalid.") from None
        if not isinstance(value, dict):
            raise APIError(400, "invalid_json", "The JSON body must be an object.")
        return value

    def require_same_origin_request(self) -> None:
        if self.headers.get("X-AgreementAtlas-Request") != "1":
            raise APIError(403, "request_origin", "The request could not be verified.")

    def empty_status_payload(self, session=None) -> dict:
        """What the interface shows before any family is selected."""

        library = library_store if PERSISTENT else visitor_library(session)
        return {
            "persistent": PERSISTENT,
            "families": [sample_flagged(item) for item in library.list()],
            "family": None,
            "session": {
                "expires_at": getattr(session, "expires_at", 0),
                "retention_hours": 0 if PERSISTENT else SESSION_TTL_HOURS,
                "file_count": 0,
                "file_limit": MAX_FILES,
                "total_bytes": 0,
                "byte_limit": MAX_TOTAL_BYTES,
                "own_family_limit": 0 if PERSISTENT else MAX_PUBLIC_FAMILIES,
            },
            "documents": [],
            "graph_ready": False,
            "enriched": False,
            "sample_family": demo_manifest().get("name", ""),
            "samples": sample_catalogue(),
            "is_sample": False,
            "sample_name": "",
            "enrichment": {
                "state": "idle",
                "stage": "idle",
                "completed": 0,
                "total": 0,
            },
            "build": {
                "mode": "baseline",
                "stage": "idle",
                "completed": 0,
                "total": 0,
                "schema_version": "3.0",
                "rebuild_required": False,
                "built_at": 0,
            },
            "lmstudio": public_lm_status(),
            "privacy": {
                "mode": APP_MODE,
                "operator": OPERATOR_NAME,
                "retention_hours": 0 if PERSISTENT else SESSION_TTL_HOURS,
                "cloud_inference": False,
            },
        }

    def status_payload(self, visitor, session=None) -> dict:
        file_count, total_bytes = workspace_usage(visitor.root)
        build = schema_status(visitor.root)
        enrichment = enrichment_status(visitor.id)
        graph_path = visitor.root / "output" / "legal_relationship_graph.json"
        library = library_store if PERSISTENT else visitor_library(session)
        return {
            "persistent": PERSISTENT,
            "families": [sample_flagged(item) for item in library.list()],
            "family": (
                sample_flagged(visitor) if hasattr(visitor, "public_record") else None
            ),
            "session": {
                "expires_at": getattr(session or visitor, "expires_at", 0),
                "retention_hours": 0 if PERSISTENT else SESSION_TTL_HOURS,
                "file_count": file_count,
                "file_limit": MAX_FILES,
                "total_bytes": total_bytes,
                "byte_limit": MAX_TOTAL_BYTES,
                "own_family_limit": 0 if PERSISTENT else MAX_PUBLIC_FAMILIES,
            },
            "documents": workspace_documents(visitor.root),
            "graph_ready": (
                visitor.root / "output" / "legal_relationship_graph.json"
            ).exists(),
            "enriched": (
                visitor.root / "output" / "legal_relationship_graph_enriched.json"
            ).exists(),
            # Offered only where a bundle is installed, so the interface does
            # not advertise a button that would fail.
            "sample_family": demo_manifest().get("name", ""),
            "samples": sample_catalogue(),
            "is_sample": (visitor.root / SAMPLE_MARKER).exists(),
            # Which sample, so a two-bundle interface can mark the loaded one.
            "sample_name": (
                (visitor.root / SAMPLE_MARKER).read_text(encoding="utf-8").strip()
                if (visitor.root / SAMPLE_MARKER).exists()
                else ""
            ),
            "enrichment": enrichment,
            # Durable, unlike the job dictionary above, which is empty after a
            # restart even for a family that took hours to extract.
            "enrichment_coverage": enrichment_coverage(visitor.root),
            "build": {
                "mode": build["build_mode"],
                "stage": enrichment.get("stage", "idle"),
                "completed": enrichment.get("completed", 0),
                "total": enrichment.get("total", 0),
                "schema_version": build["schema_version"] or "3.0",
                "rebuild_required": build["rebuild_required"],
                # When the graph on screen was produced. A workspace built by an
                # older parser looks identical to a current one without this.
                "built_at": (
                    int(graph_path.stat().st_mtime) if graph_path.exists() else 0
                ),
            },
            "lmstudio": public_lm_status(),
            "privacy": {
                "mode": APP_MODE,
                "operator": OPERATOR_NAME,
                "retention_hours": 0 if PERSISTENT else SESSION_TTL_HOURS,
                "cloud_inference": False,
            },
        }

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/healthz":
                self.json_response({"status": "ok"})
                return
            if path == "/api/families":
                if PERSISTENT:
                    self.json_response(
                        {
                            "families": [
                                sample_flagged(item) for item in library_store.list()
                            ]
                        }
                    )
                    return
                # Session-scoped: a visitor sees the shipped samples plus the
                # families they created, never anyone else's.
                visitor = self.ensure_session()
                autoload_sample(visitor)
                self.json_response(
                    {
                        "families": [
                            sample_flagged(item)
                            for item in visitor_library(visitor).list()
                        ]
                    }
                )
                return
            if path == "/api/status":
                if PERSISTENT:
                    family = self.selected_family()
                    if family is None:
                        self.json_response(self.empty_status_payload())
                        return
                    self.check_rate("status", family, limit=120, window=60)
                    self.json_response(self.status_payload(family))
                    return
                visitor = self.ensure_session()
                autoload_sample(visitor)
                self.check_rate("status", visitor, limit=120, window=60)
                family = self.session_family(visitor)
                if family is None:
                    self.json_response(self.empty_status_payload(session=visitor))
                    return
                self.json_response(self.status_payload(family, session=visitor))
                return
            if path == "/api/graph":
                visitor = self.ensure_workspace()
                self.check_rate("graph", visitor, limit=60, window=60)
                view = parse_qs(parsed.query).get("view", ["overview"])[0]
                if view not in {"overview", "detail"}:
                    raise APIError(400, "graph_view", "Choose overview or detail.")
                maximum = 150 if view == "overview" else 320
                self.json_response(compact_graph(visitor.root, max_rules=maximum))
                return
            if path == "/api/enrich/status":
                visitor = self.ensure_workspace()
                self.check_rate("status", visitor, limit=120, window=60)
                self.json_response(enrichment_status(visitor.id))
                return
            self.serve_static(path)
        except (APIError, RateLimitError) as exc:
            self.error_response(exc)
        except Exception as exc:
            print(f"[server] Unhandled {type(exc).__name__}")
            self.json_response(
                {
                    "error": "The server could not complete this request.",
                    "code": "server_error",
                },
                500,
            )

    def do_POST(self) -> None:
        try:
            self.require_same_origin_request()
            path = urlparse(self.path).path
            if path == "/api/families":
                data = self.read_json()
                if PERSISTENT:
                    family = library_store.create(clean_name(data.get("name", "")))
                    self.json_response(sample_flagged(family), 201)
                    return
                visitor = self.ensure_session()
                self.check_rate("family", visitor, limit=10, window=60 * 60)
                library = visitor_library(visitor)
                own = [
                    item
                    for item in library.list()
                    if not (item.root / SAMPLE_MARKER).exists()
                ]
                if len(own) >= MAX_PUBLIC_FAMILIES:
                    raise APIError(
                        409,
                        "family_limit",
                        "A session can hold at most "
                        f"{MAX_PUBLIC_FAMILIES} of your own agreement families.",
                    )
                family = library.create(clean_name(data.get("name", "")))
                self.json_response(sample_flagged(family), 201)
                return
            visitor = self.ensure_workspace()
            if path == "/api/upload":
                self.check_rate("upload", visitor, limit=4, window=10 * 60)
                body = self.read_body(MAX_REQUEST_BYTES)
                uploaded = parse_multipart_files(
                    self.headers.get("Content-Type", ""), body
                )
                self.json_response(
                    ingest_uploads(visitor, uploaded, self.owning_store()), 201
                )
                return
            if path == "/api/demo":
                self.check_rate("demo", visitor, limit=6, window=10 * 60)
                # The body is optional: the original client posts none, the
                # sample picker names a bundle.
                slug = ""
                if (
                    self.headers.get("Content-Type", "")
                    .lower()
                    .startswith("application/json")
                ):
                    slug = str(self.read_json().get("bundle", "")).strip()
                self.json_response(load_demo_family(visitor, slug), 201)
                return
            if path == "/api/enrich":
                self.check_rate("enrich", visitor, limit=2, window=60 * 60)
                if not workspace_documents(visitor.root):
                    raise APIError(
                        400, "no_documents", "Upload at least one agreement first."
                    )
                data = self.read_json()
                model = select_model(str(data.get("model", "")).strip())
                self.json_response(
                    start_enrichment(visitor, model, self.owning_store()), 202
                )
                return
            if path == "/api/query":
                self.check_rate("query", visitor, limit=20, window=10 * 60)
                if not workspace_documents(visitor.root):
                    raise APIError(
                        400, "no_documents", "Upload at least one agreement first."
                    )
                data = self.read_json()
                question = str(data.get("question", "")).strip()
                if len(question) < 2:
                    raise APIError(400, "question", "Enter a question.")
                if len(question) > 2000:
                    raise APIError(
                        413, "question_length", "Keep questions under 2,000 characters."
                    )
                model = select_model(str(data.get("model", "")).strip())
                if not query_slots.acquire(blocking=False):
                    raise APIError(
                        429,
                        "model_busy",
                        "The local model is answering another question. "
                        "Please try again shortly.",
                    )
                # The assistant may have ended the previous turn by asking
                # which variant applies. Without this the reply to that question
                # arrives as a bare word with nothing to attach it to, and gets
                # answered as though it were a new question about nothing.
                history = conversation.load(visitor.root)
                asked, chosen = conversation.resolve_followup(question, history)
                streaming = bool(data.get("stream"))
                reasoning = bool(data.get("reasoning")) and streaming
                started = False

                def emit(kind: str, piece: str) -> None:
                    nonlocal started
                    if not streaming:
                        return
                    if not started:
                        self.sse_start()
                        started = True
                    self.sse_send(kind, {"text": piece})

                try:
                    result = answer_question(
                        visitor.root,
                        lm_client,
                        model,
                        asked,
                        history=history,
                        on_token=emit if streaming else None,
                        reasoning=reasoning,
                    )
                except LMStudioError as exc:
                    if started:
                        # The stream is already open, so an error has to travel
                        # inside it rather than as a status code.
                        self.sse_send("error", {"error": safe_lm_error(exc)})
                        query_slots.release()
                        return
                    query_slots.release()
                    raise APIError(502, "model_error", safe_lm_error(exc)) from None
                except Exception:
                    query_slots.release()
                    raise
                else:
                    query_slots.release()
                conversation.append(
                    visitor.root,
                    {
                        # The standalone rewrite, where one happened: the next
                        # follow-up resolves against what the question meant,
                        # not against "what about for standard named users?".
                        "question": str(result.get("understood_as") or asked),
                        "answer": str(result.get("answer", ""))[:600],
                        "offered": result.get("offered") or [],
                    },
                )
                if chosen:
                    result["understood_as"] = asked
                    result["selected_variant"] = chosen
                result["disclaimer"] = (
                    "AI-assisted document interpretation, not legal advice. "
                    "Verify conclusions against the cited agreement text."
                )
                if streaming:
                    if not started:
                        self.sse_start()
                    # Evidence, trace and offered variants arrive once the
                    # prose is done; the client swaps its streamed text for
                    # this complete record.
                    self.sse_send("result", result)
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                self.json_response(result)
                return
            raise APIError(404, "not_found", "The API endpoint was not found.")
        except (APIError, RateLimitError) as exc:
            self.error_response(exc)
        except Exception as exc:
            print(f"[server] Unhandled {type(exc).__name__}")
            self.json_response(
                {
                    "error": "The server could not complete this request.",
                    "code": "server_error",
                },
                500,
            )

    def do_PATCH(self) -> None:
        try:
            self.require_same_origin_request()
            if urlparse(self.path).path != "/api/families":
                raise APIError(404, "not_found", "The API endpoint was not found.")
            if PERSISTENT:
                family = self.require_family()
                store = library_store
            else:
                session = self.ensure_session()
                family = self.require_session_family(session)
                store = visitor_library(session)
            self.check_rate("rename", family, limit=30, window=60 * 60)
            data = self.read_json()
            renamed = store.rename(family.id, str(data.get("name", "")))
            if renamed is None:
                raise APIError(404, "no_family", "That agreement family is gone.")
            self.json_response(renamed.public_record())
        except (APIError, RateLimitError) as exc:
            self.error_response(exc)
        except Exception as exc:
            print(f"[server] Unhandled {type(exc).__name__}")
            self.json_response(
                {
                    "error": "The server could not complete this request.",
                    "code": "server_error",
                },
                500,
            )

    def do_DELETE(self) -> None:
        try:
            self.require_same_origin_request()
            path = urlparse(self.path).path
            if path == "/api/conversation":
                # Clearing the chat clears the history that resolves follow-ups,
                # or the next "logical" would answer a question nobody can see.
                visitor = self.ensure_workspace()
                self.check_rate("conversation", visitor, limit=30, window=60 * 60)
                conversation.clear(visitor.root)
                self.json_response({"cleared": True})
                return
            if PERSISTENT:
                if path not in {"/api/families", "/api/session"}:
                    raise APIError(404, "not_found", "The API endpoint was not found.")
                family = self.require_family()
                self.check_rate("delete", family, limit=12, window=60 * 60)
                cancel_jobs([family.id])
                library_store.delete(family.id)
                self.json_response({"deleted": True})
                return
            visitor = self.ensure_session()
            if path == "/api/families":
                family = self.require_session_family(visitor)
                self.check_rate("delete", family, limit=12, window=60 * 60)
                cancel_jobs([family.id])
                visitor_library(visitor).delete(family.id)
                self.json_response({"deleted": True})
                return
            if path != "/api/session":
                raise APIError(404, "not_found", "The API endpoint was not found.")
            self.check_rate("delete", visitor, limit=6, window=60 * 60)
            # "Delete my documents" is the whole session: every family the
            # visitor holds, sample copies included, and any running job.
            family_ids = [item.id for item in visitor_library(visitor).list()]
            cancel_jobs([visitor.id, *family_ids])
            session_store.delete(visitor.id)
            drop_visitor_libraries([visitor.id])
            self.clear_session_cookie = True
            self.set_session_cookie = False
            self.json_response({"deleted": True})
        except (APIError, RateLimitError) as exc:
            self.error_response(exc)
        except Exception as exc:
            print(f"[server] Unhandled {type(exc).__name__}")
            self.json_response(
                {
                    "error": "The server could not complete this request.",
                    "code": "server_error",
                },
                500,
            )

    def asset_version(self, name: str) -> str:
        """A short content hash for a static asset, recomputed when it changes.

        The origin asks for `no-cache`, and the CDN in front of it answers with
        a four-hour browser TTL regardless, so a deployed change kept running
        the previous script in every browser that had already visited -- which
        on screen is indistinguishable from the change not working. Versioning
        the URL settles it without depending on a CDN setting: the HTML is
        `no-store`, so it always carries the current hash, and a changed file is
        a different URL.
        """

        target = (WEB / name).resolve()
        try:
            stamp = target.stat().st_mtime_ns
        except OSError:
            return ""
        cached = ASSET_VERSIONS.get(name)
        if cached and cached[0] == stamp:
            return cached[1]
        digest = hashlib.sha256(target.read_bytes()).hexdigest()[:10]
        ASSET_VERSIONS[name] = (stamp, digest)
        return digest

    def serve_static(self, path: str) -> None:
        if path in {"/", ""}:
            # One interface in both modes. The tool page reads as a working
            # product in a way the essay page never did; the essay's job moves
            # to the story page and the engineering log. demo.html stays
            # reachable by name while the convergence settles.
            path = "/index.html"
        target = WEB / path.lstrip("/")
        # A section address ("/blog/") means that section's index, the same
        # convention every static host honours.
        if target.is_dir() and (target / "index.html").is_file():
            target = target / "index.html"
        try:
            resolved = target.resolve()
        except OSError:
            raise APIError(404, "not_found", "Page not found.") from None
        if not resolved.is_file() or WEB.resolve() not in resolved.parents:
            raise APIError(404, "not_found", "Page not found.")
        if resolved.suffix == ".html":
            visitor = self.ensure_session()
            replacements = {
                "{{OPERATOR_NAME}}": html.escape(OPERATOR_NAME),
                "{{PRIVACY_EMAIL}}": html.escape(PRIVACY_EMAIL),
                "{{RETENTION_HOURS}}": str(SESSION_TTL_HOURS),
                "{{APP_MODE}}": html.escape(APP_MODE),
            }
            for asset in (
                "app.js",
                "styles.css",
                "demo.js",
                "demo.css",
                "demo-graph.js",
            ):
                digest = self.asset_version(asset)
                if digest:
                    replacements[f'"/{asset}"'] = f'"/{asset}?v={digest}"'
            content = resolved.read_text(encoding="utf-8")
            for marker, replacement in replacements.items():
                content = content.replace(marker, replacement)
            body = content.encode("utf-8")
            content_type = "text/html; charset=utf-8"
            cache = "no-store"
            self.visitor = visitor
        else:
            content_types = {
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".png": "image/png",
                ".ico": "image/x-icon",
                ".json": "application/json; charset=utf-8",
                ".svg": "image/svg+xml",
                ".woff2": "font/woff2",
                ".xml": "application/xml; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
            }
            content_type = content_types.get(
                resolved.suffix, "application/octet-stream"
            )
            body = resolved.read_bytes()
            # Revalidate rather than cache. These are a few kilobytes from a
            # local server, and an hour-long cache means a changed interface
            # silently keeps running the previous script until a hard refresh --
            # indistinguishable, on screen, from the change not working.
            # A fingerprinted URL names one immutable body, so it can be kept
            # forever. A bare one is whatever is current and must be revalidated.
            cache = (
                "public, max-age=31536000, immutable"
                if parse_qs(urlparse(self.path).query).get("v")
                else "no-cache"
            )
        self.send_response(200)
        self._common_headers(content_type, len(body), cache=cache)
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str | None = None, port: int | None = None
) -> ThreadingHTTPServer:
    address = host or os.environ.get("HOST", "127.0.0.1")
    selected_port = port if port is not None else env_int("PORT", 8000, minimum=0)
    return ThreadingHTTPServer((address, selected_port), Handler)


def main() -> None:
    validate_public_configuration()
    server = create_server()
    host, port = server.server_address[:2]
    mode = "public demo" if APP_MODE == "public-demo" else "local"
    print(f"AgreementAtlas ({mode}) is ready at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping AgreementAtlas.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
