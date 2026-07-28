#!/usr/bin/env python3
"""Rebuild the deterministic layer of every library family.

A titled section that names a licence model is now registered as a definition of
that model, which only happens at ingest. Until a family is rebuilt it keeps the
old `defined_terms.jsonl` and asking what one of its licence models is still
retrieves the clauses that merely mention it.

This rebuilds parsing, definitions, offerings, rules and relationships. It does
not run the model, so it is minutes rather than hours -- but clause ids are
derived from content and position, so rules previously enriched by the model can
be left pointing at ids that no longer exist. Re-run enrichment afterwards.

    python scripts/reingest_library.py --dry-run
    python scripts/reingest_library.py
    python scripts/reingest_library.py --only OpenText
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_ingest import rebuild_workspace  # noqa: E402
from library_store import LibraryStore  # noqa: E402


def definition_count(root: Path) -> int:
    path = root / "legal" / "defined_terms.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.open() if line.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "library")
    parser.add_argument("--only", action="append", help="family name; repeatable")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    families = [
        item
        for item in LibraryStore(args.root).list()
        if not args.only or item.name in args.only
    ]
    if not families:
        print("no families matched", file=sys.stderr)
        return 2

    print(f"{len(families)} family(ies)\n")
    failures = 0
    for family in families:
        before = definition_count(family.root)
        if args.dry_run:
            print(f"  {family.name:24} {before:4} definitions (dry run)")
            continue
        started = time.monotonic()
        try:
            result = rebuild_workspace(family.root)
        except Exception as error:  # noqa: BLE001 - one bad family must not stop the rest
            failures += 1
            print(f"  {family.name:24} FAILED  {error}", flush=True)
            traceback.print_exc(file=sys.stderr)
            continue
        after = definition_count(family.root)
        print(
            f"  {family.name:24} {before:4} -> {after:4} definitions"
            f"  ({result.get('clauses', 0)} clauses,"
            f" {len(json.dumps(result)) and result.get('rules', 0)} rules)"
            f"  {time.monotonic() - started:5.1f}s",
            flush=True,
        )
    if failures:
        print(f"\n{failures} family(ies) failed", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
