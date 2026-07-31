#!/usr/bin/env python3
"""Turn data/incoming/<vendor>/ PDF sets into deterministic library families.

Each vendor directory becomes one family: create it in the library, copy the
documents into sources/, run the deterministic rebuild (no model calls), and
report what parsed. Existing families with the same name are left alone --
delete the family first if a re-ingest is wanted.

    python scripts/ingest_incoming.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_ingest import rebuild_workspace  # noqa: E402
from library_store import LibraryStore  # noqa: E402


def count(path: Path) -> int:
    return sum(1 for line in path.open() if line.strip()) if path.exists() else 0


def main() -> int:
    incoming = ROOT / "data" / "incoming"
    store = LibraryStore(ROOT / "data" / "library")
    existing = {family.name.casefold() for family in store.list()}
    failures = 0
    for vendor_dir in sorted(item for item in incoming.iterdir() if item.is_dir()):
        name = vendor_dir.name.capitalize()
        documents = sorted(vendor_dir.glob("*.pdf"))
        if not documents:
            continue
        if name.casefold() in existing:
            print(f"SKIP {name}: family already exists")
            continue
        family = store.create(name)
        sources = family.root / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        for document in documents:
            shutil.copy2(document, sources / document.name)
        try:
            summary = rebuild_workspace(family.root)
        except Exception as exc:  # a family that will not parse is a finding
            print(f"FAIL {name}: {exc}")
            failures += 1
            continue
        legal = family.root / "legal"
        print(
            f"OK   {name}: {summary.get('documents', 0)} docs, "
            f"{count(legal / 'clauses.jsonl')} clauses, "
            f"{count(legal / 'defined_terms.jsonl')} terms, "
            f"{count(legal / 'precedence_rules.jsonl')} precedence"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
