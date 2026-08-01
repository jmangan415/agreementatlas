from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from session_store import RateLimiter, RateLimitError, SessionStore


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = SessionStore(
            Path(self.temporary.name), ttl_seconds=120, clock=self.clock
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sessions_are_random_isolated_and_reusable(self) -> None:
        first, first_created = self.store.get_or_create(None)
        second, second_created = self.store.get_or_create(None)
        reused, reused_created = self.store.get_or_create(first.id)

        self.assertTrue(first_created)
        self.assertTrue(second_created)
        self.assertFalse(reused_created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(reused.root, first.root)
        for name in ("sources", "input", "legal", "output"):
            self.assertTrue((first.root / name).is_dir())
            self.assertTrue((second.root / name).is_dir())

    def test_expiry_is_absolute_and_cleanup_removes_workspace(self) -> None:
        visitor, _ = self.store.get_or_create(None)
        uploaded = visitor.root / "sources" / "agreement.txt"
        generated = visitor.root / "output" / "legal_relationship_graph.json"
        uploaded.write_text("confidential agreement", encoding="utf-8")
        generated.write_text('{"nodes": []}', encoding="utf-8")
        self.clock.value += 119
        self.assertIsNotNone(self.store.get(visitor.id))
        self.assertTrue(uploaded.exists())
        self.assertTrue(generated.exists())
        self.clock.value += 1
        self.assertIsNone(self.store.get(visitor.id))
        expired = self.store.cleanup_expired(force=True)
        self.assertEqual(expired, [visitor.id])
        self.assertFalse(visitor.root.exists())
        self.assertFalse(uploaded.exists())
        self.assertFalse(generated.exists())

    def test_delete_removes_every_session_directory(self) -> None:
        visitor, _ = self.store.get_or_create(None)
        (visitor.root / "sources" / "agreement.txt").write_text(
            "sample", encoding="utf-8"
        )
        self.assertTrue(self.store.delete(visitor.id))
        self.assertFalse(visitor.root.exists())
        self.assertFalse(self.store.delete(visitor.id))


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window_reports_retry_time(self) -> None:
        clock = Clock()
        limiter = RateLimiter(clock=clock)
        limiter.check("upload", "visitor", limit=2, window=60)
        limiter.check("upload", "visitor", limit=2, window=60)
        with self.assertRaises(RateLimitError) as raised:
            limiter.check("upload", "visitor", limit=2, window=60)
        self.assertEqual(raised.exception.retry_after, 60)
        clock.value += 61
        limiter.check("upload", "visitor", limit=2, window=60)


if __name__ == "__main__":
    unittest.main()
