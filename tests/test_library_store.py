"""Tests for the persistent agreement library.

The store this replaces deleted its own workspaces on a timer. The properties
worth pinning here are the opposite: nothing expires, ids cannot escape the
library root, and deleting one family leaves the others alone.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library_store import FAMILY_ID, Family, LibraryError, LibraryStore, clean_name


class LibraryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = LibraryStore(Path(self.temporary.name))

    def test_a_new_family_has_a_workspace_and_survives_lookup(self) -> None:
        family = self.store.create("OpenText")
        self.assertTrue(FAMILY_ID.fullmatch(family.id))
        for directory in ("sources", "input", "legal", "output"):
            self.assertTrue((family.root / directory).is_dir())
        again = self.store.get(family.id)
        self.assertIsInstance(again, Family)
        self.assertEqual(again.name, "OpenText")

    def test_families_do_not_expire(self) -> None:
        # The whole point of the library: a workspace is still there long after
        # the six-hour session TTL that this store replaces.
        far_future = LibraryStore(Path(self.temporary.name), clock=lambda: 10**10)
        family = self.store.create("Micro Focus")
        self.assertIsNotNone(far_future.get(family.id))

    def test_listing_is_most_recently_updated_first(self) -> None:
        ticks = iter(range(1000, 1100))
        store = LibraryStore(Path(self.temporary.name), clock=lambda: next(ticks))
        first = store.create("First")
        second = store.create("Second")
        store.touch(first.id)
        self.assertEqual([item.name for item in store.list()], ["First", "Second"])
        store.touch(second.id)
        self.assertEqual([item.name for item in store.list()], ["Second", "First"])

    def test_rename_keeps_the_workspace_and_its_id(self) -> None:
        family = self.store.create("Untitled family")
        (family.root / "sources" / "eula.pdf").write_bytes(b"%PDF-1.4")
        renamed = self.store.rename(family.id, "  OpenText   EULA + LMS ")
        self.assertEqual(renamed.id, family.id)
        self.assertEqual(renamed.name, "OpenText EULA + LMS")
        self.assertEqual(renamed.document_count, 1)

    def test_delete_removes_only_that_family(self) -> None:
        keep = self.store.create("Keep")
        drop = self.store.create("Drop")
        self.assertTrue(self.store.delete(drop.id))
        self.assertFalse(drop.root.exists())
        self.assertTrue(keep.root.exists())
        self.assertEqual([item.id for item in self.store.list()], [keep.id])

    def test_ids_outside_the_hex_format_are_refused(self) -> None:
        # A readable name must never reach the filesystem path.
        for value in ("../../etc", "OpenText", "", "z" * 32, "a" * 31):
            with self.subTest(value=value):
                self.assertIsNone(self.store.get(value))
                self.assertFalse(self.store.delete(value))
                with self.assertRaises(LibraryError):
                    self.store.lock_for(value)

    def test_enrichment_state_is_recorded_on_the_family(self) -> None:
        family = self.store.create("OpenText")
        self.assertFalse(family.enriched)
        updated = self.store.update(
            family.id, enriched=True, enrichment_model="google/gemma-4-26b-a4b-qat"
        )
        self.assertTrue(updated.enriched)
        self.assertEqual(updated.enrichment_model, "google/gemma-4-26b-a4b-qat")
        self.assertTrue(self.store.get(family.id).enriched)

    def test_a_family_with_unreadable_metadata_is_ignored_not_fatal(self) -> None:
        family = self.store.create("Broken")
        (family.root / "family.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.get(family.id))
        self.assertEqual(self.store.list(), [])

    def test_names_are_trimmed_and_bounded(self) -> None:
        self.assertEqual(clean_name("  a\n\n  b  "), "a b")
        self.assertEqual(len(clean_name("x" * 500)), 120)
        self.assertEqual(self.store.create("").name, "Untitled family")


if __name__ == "__main__":
    unittest.main()


class EnrichmentVisibilityTests(unittest.TestCase):
    """Whether a family has been AI-enriched must survive a restart.

    The in-memory job dictionary is empty after the server restarts, so a family
    that took hours to extract would otherwise report "Not started".
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = LibraryStore(Path(self.temporary.name))

    def test_extracted_rules_on_disk_mark_the_family_enriched(self) -> None:
        family = self.store.create("OpenText")
        self.assertFalse(family.enriched)
        (family.root / "legal" / "lm_rules.jsonl").write_text(
            '{"clause_id": "clause:1"}\n', encoding="utf-8"
        )
        # A fresh store, as if the process had restarted.
        self.assertTrue(LibraryStore(Path(self.temporary.name)).get(family.id).enriched)

    def test_the_metadata_flag_alone_still_counts(self) -> None:
        family = self.store.create("Micro Focus")
        self.store.update(family.id, enriched=True, enrichment_model="gemma")
        reopened = LibraryStore(Path(self.temporary.name)).get(family.id)
        self.assertTrue(reopened.enriched)
        self.assertEqual(reopened.enrichment_model, "gemma")
