from __future__ import annotations

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
lm_slots = threading.BoundedSemaphore(env_int("LMSTUDIO_MAX_CONCURRENT_JOBS", 1))

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


def workspace_store():
    """Whichever store owns workspaces in this mode."""

    return library_store if PERSISTENT else session_store


def ingest_uploads(visitor, uploaded: list[tuple[str, bytes]]) -> dict:
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

    store = workspace_store()
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
    if PERSISTENT:
        current = library_store.get(visitor.id)
        if current and current.name in {"", "Untitled family"}:
            # An unnamed family takes the name the ingest derived for it, so the
            # sidebar reads as documents rather than as "Untitled family (3)".
            families = read_jsonl(active.root / "legal" / "agreement_families.jsonl")
            derived = str(families[0].get("title", "")) if families else ""
            library_store.update(visitor.id, name=derived or "Untitled family")
        else:
            library_store.touch(visitor.id)
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


def start_enrichment(visitor, model: str) -> dict:
    with job_guard:
        current = jobs.get(visitor.id)
        if current and current.get("state") == "running":
            raise APIError(
                409,
                "enrichment_running",
                "Enrichment is already running for this session.",
            )
        if not lm_slots.acquire(blocking=False):
            raise APIError(
                429,
                "model_busy",
                "The local model is busy with another request. Please try again shortly.",
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
        # A family that has been deleted mid-run must stop the job, but the
        # session store knows nothing about families -- ask the right one.
        if not workspace_store().is_active(visitor.id):
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
            if PERSISTENT and library_store.get(visitor.id):
                library_store.update(visitor.id, enriched=True, enrichment_model=model)
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
            lm_slots.release()

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
        visitor, created = session_store.get_or_create(self._cookie_value())
        self.visitor = visitor
        self.set_session_cookie = created
        self.clear_session_cookie = False
        return visitor

    def selected_family(self):
        """The agreement family this request addresses, or None.

        Local mode never creates a workspace implicitly: an empty library is a
        real state the interface has to show, and auto-creating on every
        cookieless visit littered the disk with empty directories.
        """

        assert library_store is not None
        value = parse_qs(urlparse(self.path).query).get("family", [""])[0]
        return library_store.get(value)

    def require_family(self):
        family = self.selected_family()
        if family is None:
            raise APIError(
                404, "no_family", "Select or create an agreement family first."
            )
        return family

    def ensure_workspace(self):
        """The workspace for this request under either storage model."""

        if PERSISTENT:
            return self.require_family()
        return self.ensure_session()

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
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def empty_status_payload(self) -> dict:
        """What the interface shows before any family is selected."""

        return {
            "persistent": True,
            "families": [item.public_record() for item in library_store.list()],
            "family": None,
            "session": {
                "expires_at": 0,
                "retention_hours": 0,
                "file_count": 0,
                "file_limit": MAX_FILES,
                "total_bytes": 0,
                "byte_limit": MAX_TOTAL_BYTES,
            },
            "documents": [],
            "graph_ready": False,
            "enriched": False,
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
                "retention_hours": 0,
                "cloud_inference": False,
            },
        }

    def status_payload(self, visitor) -> dict:
        file_count, total_bytes = workspace_usage(visitor.root)
        build = schema_status(visitor.root)
        enrichment = enrichment_status(visitor.id)
        graph_path = visitor.root / "output" / "legal_relationship_graph.json"
        return {
            "persistent": PERSISTENT,
            "families": (
                [item.public_record() for item in library_store.list()]
                if PERSISTENT
                else []
            ),
            "family": (
                visitor.public_record() if hasattr(visitor, "public_record") else None
            ),
            "session": {
                "expires_at": getattr(visitor, "expires_at", 0),
                "retention_hours": 0 if PERSISTENT else SESSION_TTL_HOURS,
                "file_count": file_count,
                "file_limit": MAX_FILES,
                "total_bytes": total_bytes,
                "byte_limit": MAX_TOTAL_BYTES,
            },
            "documents": workspace_documents(visitor.root),
            "graph_ready": (
                visitor.root / "output" / "legal_relationship_graph.json"
            ).exists(),
            "enriched": (
                visitor.root / "output" / "legal_relationship_graph_enriched.json"
            ).exists(),
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
                if not PERSISTENT:
                    raise APIError(
                        404, "not_available", "The library is disabled in demo mode."
                    )
                self.json_response(
                    {
                        "families": [
                            item.public_record() for item in library_store.list()
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
                self.check_rate("status", visitor, limit=120, window=60)
                self.json_response(self.status_payload(visitor))
                return
            if path == "/api/graph":
                visitor = self.ensure_workspace()
                self.check_rate("graph", visitor, limit=60, window=60)
                view = parse_qs(parsed.query).get("view", ["overview"])[0]
                if view not in {"overview", "detail"}:
                    raise APIError(400, "graph_view", "Choose overview or detail.")
                maximum = 60 if view == "overview" else 180
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
                if not PERSISTENT:
                    raise APIError(
                        404, "not_available", "The library is disabled in demo mode."
                    )
                data = self.read_json()
                family = library_store.create(clean_name(data.get("name", "")))
                self.json_response(family.public_record(), 201)
                return
            visitor = self.ensure_workspace()
            if path == "/api/upload":
                self.check_rate("upload", visitor, limit=4, window=10 * 60)
                body = self.read_body(MAX_REQUEST_BYTES)
                uploaded = parse_multipart_files(
                    self.headers.get("Content-Type", ""), body
                )
                self.json_response(ingest_uploads(visitor, uploaded), 201)
                return
            if path == "/api/enrich":
                self.check_rate("enrich", visitor, limit=2, window=60 * 60)
                if not workspace_documents(visitor.root):
                    raise APIError(
                        400, "no_documents", "Upload at least one agreement first."
                    )
                data = self.read_json()
                model = select_model(str(data.get("model", "")).strip())
                self.json_response(start_enrichment(visitor, model), 202)
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
                if not lm_slots.acquire(blocking=False):
                    raise APIError(
                        429,
                        "model_busy",
                        "The local model is busy. Please try again shortly.",
                    )
                try:
                    result = answer_question(visitor.root, lm_client, model, question)
                except LMStudioError as exc:
                    raise APIError(502, "model_error", safe_lm_error(exc)) from None
                finally:
                    lm_slots.release()
                result["disclaimer"] = (
                    "AI-assisted document interpretation, not legal advice. "
                    "Verify conclusions against the cited agreement text."
                )
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
            if not PERSISTENT or urlparse(self.path).path != "/api/families":
                raise APIError(404, "not_found", "The API endpoint was not found.")
            family = self.require_family()
            self.check_rate("rename", family, limit=30, window=60 * 60)
            data = self.read_json()
            renamed = library_store.rename(family.id, str(data.get("name", "")))
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
            if path != "/api/session":
                raise APIError(404, "not_found", "The API endpoint was not found.")
            self.check_rate("delete", visitor, limit=6, window=60 * 60)
            cancel_jobs([visitor.id])
            session_store.delete(visitor.id)
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

    def serve_static(self, path: str) -> None:
        if path in {"/", ""}:
            path = "/index.html"
        target = WEB / path.lstrip("/")
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
            }
            content_type = content_types.get(
                resolved.suffix, "application/octet-stream"
            )
            body = resolved.read_bytes()
            # Revalidate rather than cache. These are a few kilobytes from a
            # local server, and an hour-long cache means a changed interface
            # silently keeps running the previous script until a hard refresh --
            # indistinguishable, on screen, from the change not working.
            cache = "no-cache"
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
