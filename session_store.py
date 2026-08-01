from __future__ import annotations

import json
import math
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SESSION_ID = re.compile(r"^[a-f0-9]{64}$")
WORKSPACE_DIRECTORIES = ("sources", "input", "legal", "output")


class SessionError(RuntimeError):
    pass


class RateLimitError(SessionError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__("Too many requests. Please wait and try again.")


@dataclass(frozen=True)
class VisitorSession:
    id: str
    root: Path
    created_at: int
    expires_at: int


class SessionStore:
    """Creates short-lived, filesystem-isolated visitor workspaces."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_seconds: int = 6 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.resolve()
        self.ttl_seconds = max(60, ttl_seconds)
        self.clock = clock
        self._guard = threading.RLock()
        self._locks: dict[str, threading.RLock] = {}
        self._last_cleanup = 0.0
        self.root.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, session_root: Path) -> Path:
        return session_root / ".session.json"

    def _read_metadata(self, session_root: Path) -> dict | None:
        try:
            value = json.loads(
                self._metadata_path(session_root).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        return value

    def _from_existing(self, session_id: str) -> VisitorSession | None:
        if not SESSION_ID.fullmatch(session_id):
            return None
        session_root = self.root / session_id
        metadata = self._read_metadata(session_root)
        if not metadata:
            return None
        try:
            created_at = int(metadata["created_at"])
            expires_at = int(metadata["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if expires_at <= int(self.clock()):
            return None
        return VisitorSession(session_id, session_root, created_at, expires_at)

    def _create(self) -> VisitorSession:
        now = int(self.clock())
        while True:
            session_id = secrets.token_hex(32)
            session_root = self.root / session_id
            try:
                session_root.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        for name in WORKSPACE_DIRECTORIES:
            (session_root / name).mkdir(mode=0o700)
        metadata = {
            "schema_version": 1,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        self._metadata_path(session_root).write_text(
            json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
        )
        return VisitorSession(session_id, session_root, now, now + self.ttl_seconds)

    def get_or_create(self, session_id: str | None) -> tuple[VisitorSession, bool]:
        with self._guard:
            if session_id:
                existing = self._from_existing(session_id)
                if existing:
                    return existing, False
            return self._create(), True

    def get(self, session_id: str | None) -> VisitorSession | None:
        if not session_id:
            return None
        with self._guard:
            return self._from_existing(session_id)

    def lock_for(self, session_id: str) -> threading.RLock:
        if not SESSION_ID.fullmatch(session_id):
            raise SessionError("Invalid visitor session.")
        with self._guard:
            return self._locks.setdefault(session_id, threading.RLock())

    def is_active(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    def delete(self, session_id: str) -> bool:
        if not SESSION_ID.fullmatch(session_id):
            return False
        with self.lock_for(session_id):
            with self._guard:
                session_root = self.root / session_id
                if not session_root.exists():
                    return False
                shutil.rmtree(session_root)
                self._locks.pop(session_id, None)
                return True

    def cleanup_expired(self, *, force: bool = False) -> list[str]:
        now = self.clock()
        with self._guard:
            if not force and now - self._last_cleanup < 60:
                return []
            self._last_cleanup = now
            candidates: list[tuple[str, Path, threading.RLock]] = []
            for candidate in self.root.iterdir():
                if not candidate.is_dir() or not SESSION_ID.fullmatch(candidate.name):
                    continue
                metadata = self._read_metadata(candidate)
                try:
                    expires_at = int(metadata["expires_at"]) if metadata else 0
                except (KeyError, TypeError, ValueError):
                    expires_at = 0
                if expires_at > int(now):
                    continue
                lock = self._locks.setdefault(candidate.name, threading.RLock())
                candidates.append((candidate.name, candidate, lock))
        expired: list[str] = []
        for session_id, candidate, lock in candidates:
            with lock:
                try:
                    shutil.rmtree(candidate)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    # Keep the session eligible for the next cleanup pass. A
                    # failed filesystem deletion must never be reported as a
                    # successful expiry -- but a workspace that fails every
                    # pass needs an operator signal, not a silent retry loop.
                    print(
                        f"[sessions] Could not delete expired workspace "
                        f"{session_id[:8]}…: {type(exc).__name__}: {exc}"
                    )
                    continue
            with self._guard:
                self._locks.pop(session_id, None)
            expired.append(session_id)
        return expired


class RateLimiter:
    """Small in-memory sliding-window limiter for the local/demo server."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._guard = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def check(self, scope: str, key: str, *, limit: int, window: int) -> None:
        if limit <= 0:
            raise RateLimitError(window)
        now = self.clock()
        cutoff = now - window
        bucket_key = (scope, key)
        with self._guard:
            events = self._events[bucket_key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = math.ceil(events[0] + window - now)
                raise RateLimitError(retry_after)
            events.append(now)

            if len(self._events) > 4096:
                stale = [
                    item
                    for item, values in self._events.items()
                    if not values or values[-1] <= cutoff
                ]
                for item in stale[:1024]:
                    self._events.pop(item, None)
