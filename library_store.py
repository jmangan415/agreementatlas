"""Persistent, named agreement-family workspaces.

The sibling `session_store` creates short-lived visitor workspaces that delete
themselves, which is right for an untrusted public demo and wrong for a local
install. Ingesting a document set and running strict extraction over it costs
hours; a library keeps that work and lets it accumulate across vendors.

The two stores deliberately share a shape -- same per-id locking, same
filesystem-safe id validation, same workspace layout -- so the server can hold
either one behind the same calls.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from session_store import WORKSPACE_DIRECTORIES, SessionError

# Hex only. The readable name lives in family.json, never in the path, so a
# family called "../../etc" cannot escape the library root.
FAMILY_ID = re.compile(r"^[a-f0-9]{32}$")
METADATA_NAME = "family.json"
MAX_NAME_LENGTH = 120


class LibraryError(SessionError):
    pass


@dataclass(frozen=True)
class Family:
    id: str
    root: Path
    name: str
    created_at: int
    updated_at: int
    enriched: bool
    enrichment_model: str
    document_count: int

    def public_record(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "enriched": self.enriched,
            "enrichment_model": self.enrichment_model,
            "document_count": self.document_count,
        }


def clean_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    return name[:MAX_NAME_LENGTH]


class LibraryStore:
    """Named workspaces that persist until the user deletes them."""

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.root = root.resolve()
        self.clock = clock
        self._guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, family_root: Path) -> Path:
        return family_root / METADATA_NAME

    def _read_metadata(self, family_root: Path) -> dict | None:
        try:
            value = json.loads(
                self._metadata_path(family_root).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_metadata(self, family_root: Path, metadata: dict) -> None:
        # Write then rename: a half-written family.json would make the family
        # unreadable, and the library is the only record that it exists.
        target = self._metadata_path(family_root)
        temporary = family_root / f".{METADATA_NAME}.{secrets.token_hex(4)}"
        temporary.write_text(
            json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
        )
        temporary.replace(target)

    def _count_documents(self, family_root: Path) -> int:
        sources = family_root / "sources"
        if not sources.is_dir():
            return 0
        return sum(1 for path in sources.iterdir() if path.is_file())

    def _is_enriched(self, family_root: Path, metadata: dict) -> bool:
        """Whether model-extracted rules exist for this family.

        The artefact on disk outranks the metadata flag: enrichment run from the
        command line, or interrupted after writing rules but before the job
        recorded completion, is still enrichment the reader can see cited.
        """

        if (family_root / "legal" / "lm_rules.jsonl").is_file():
            return True
        return bool(metadata.get("enriched", False))

    def _from_existing(self, family_id: str) -> Family | None:
        if not FAMILY_ID.fullmatch(family_id):
            return None
        family_root = self.root / family_id
        metadata = self._read_metadata(family_root)
        if metadata is None:
            return None
        try:
            created_at = int(metadata.get("created_at", 0))
            updated_at = int(metadata.get("updated_at", created_at))
        except (TypeError, ValueError):
            return None
        return Family(
            id=family_id,
            root=family_root,
            name=clean_name(metadata.get("name", "")) or "Untitled family",
            created_at=created_at,
            updated_at=updated_at,
            enriched=self._is_enriched(family_root, metadata),
            enrichment_model=str(metadata.get("enrichment_model", "")),
            document_count=self._count_documents(family_root),
        )

    def create(self, name: str = "") -> Family:
        now = int(self.clock())
        with self._guard:
            while True:
                family_id = secrets.token_hex(16)
                family_root = self.root / family_id
                try:
                    family_root.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            for directory in WORKSPACE_DIRECTORIES:
                (family_root / directory).mkdir(mode=0o700)
            self._write_metadata(
                family_root,
                {
                    "schema_version": 1,
                    "id": family_id,
                    "name": clean_name(name) or "Untitled family",
                    "created_at": now,
                    "updated_at": now,
                    "enriched": False,
                    "enrichment_model": "",
                },
            )
        family = self._from_existing(family_id)
        if family is None:  # pragma: no cover - only on a filesystem failure
            raise LibraryError("Could not create the agreement family.")
        return family

    def get(self, family_id: str | None) -> Family | None:
        if not family_id:
            return None
        with self._guard:
            return self._from_existing(family_id)

    def list(self) -> list[Family]:
        with self._guard:
            families = [
                family
                for candidate in self.root.iterdir()
                if candidate.is_dir() and FAMILY_ID.fullmatch(candidate.name)
                for family in [self._from_existing(candidate.name)]
                if family is not None
            ]
        # Most recently worked on first: that is what the user is coming back to.
        return sorted(families, key=lambda item: item.updated_at, reverse=True)

    def update(self, family_id: str, **changes: object) -> Family | None:
        if not FAMILY_ID.fullmatch(family_id):
            return None
        with self.lock_for(family_id), self._guard:
            family_root = self.root / family_id
            metadata = self._read_metadata(family_root)
            if metadata is None:
                return None
            if "name" in changes:
                changes["name"] = clean_name(str(changes["name"])) or metadata.get(
                    "name", "Untitled family"
                )
            metadata.update(changes)
            metadata["updated_at"] = int(self.clock())
            self._write_metadata(family_root, metadata)
            return self._from_existing(family_id)

    def rename(self, family_id: str, name: str) -> Family | None:
        return self.update(family_id, name=name)

    def touch(self, family_id: str) -> Family | None:
        return self.update(family_id)

    def delete(self, family_id: str) -> bool:
        if not FAMILY_ID.fullmatch(family_id):
            return False
        with self.lock_for(family_id):
            with self._guard:
                family_root = self.root / family_id
                if not family_root.exists():
                    return False
                shutil.rmtree(family_root, ignore_errors=True)
                self._locks.pop(family_id, None)
                return True

    def lock_for(self, family_id: str) -> threading.RLock:
        if not FAMILY_ID.fullmatch(family_id):
            raise LibraryError("Invalid agreement family.")
        with self._guard:
            return self._locks.setdefault(family_id, threading.RLock())

    def is_active(self, family_id: str) -> bool:
        return self.get(family_id) is not None
