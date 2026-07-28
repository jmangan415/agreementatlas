#!/usr/bin/env python3
"""Which clauses did the model actually get asked about?

`scripts/act_coverage.py` and `evaluation.py` both read `operative_rules.jsonl`,
the deterministic fallback. Neither reads `lm_rules.jsonl`, so neither can see
whether the model was ever given a clause -- and the failure that prompted this
was exactly that. A list item under a chapeau carries no modal of its own
("only be used to support Licensee's use of the Software"; the "may" is in the
chapeau), so `substantive_clauses` filtered it out, it never entered a batch,
and the model was never asked. The interface then showed the deterministic
guess as though it were analysis.

A clause is accounted for when it either produced a model rule or is recorded
as failed. A clause in neither set was never attempted, and that is the number
this script exists to report.

    python scripts/model_coverage.py
    python scripts/model_coverage.py --only OpenText --samples 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import substantive_clauses  # noqa: E402
from library_store import LibraryStore  # noqa: E402


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open() if line.strip()]


def survey(root: Path) -> dict:
    legal = root / "legal"
    clauses = read(legal / "clauses.jsonl")
    children = [item for item in clauses if item.get("chapeau_clause_id")]
    eligible = {str(item.get("id", "")) for item in substantive_clauses(root)}

    checkpoint: dict = {}
    path = legal / "deep_build_checkpoint.json"
    if path.exists():
        try:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            checkpoint = {}
    attempted = set(checkpoint.get("completed_clause_ids", [])) | set(
        checkpoint.get("failed_clause_ids", [])
    )
    with_rule = {
        str(item.get("clause_id", "")) for item in read(legal / "lm_rules.jsonl")
    }

    never = [
        item
        for item in children
        if str(item.get("id", "")) not in attempted
        and str(item.get("id", "")) not in with_rule
    ]
    return {
        "clauses": len(clauses),
        "children": len(children),
        "children_eligible": sum(
            1 for item in children if str(item.get("id", "")) in eligible
        ),
        "children_with_rule": sum(
            1 for item in children if str(item.get("id", "")) in with_rule
        ),
        "never_attempted": never,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "library")
    parser.add_argument("--only", action="append")
    parser.add_argument("--samples", type=int, default=0)
    args = parser.parse_args()

    families = [
        item
        for item in LibraryStore(args.root).list()
        if not args.only or item.name in args.only
    ]
    header = f"{'family':24} {'clauses':>8} {'children':>9} {'eligible':>9} {'w/ rule':>8} {'NEVER ASKED':>12}"
    print(header)
    print("-" * len(header))
    totals = {
        "children": 0,
        "children_eligible": 0,
        "children_with_rule": 0,
        "never": 0,
    }
    samples: list[tuple[str, dict]] = []
    for family in families:
        result = survey(family.root)
        never = len(result["never_attempted"])
        print(
            f"{family.name[:24]:24} {result['clauses']:8} {result['children']:9} "
            f"{result['children_eligible']:9} {result['children_with_rule']:8} "
            f"{never:7} ({never / max(1, result['children']):3.0%})"
        )
        totals["children"] += result["children"]
        totals["children_eligible"] += result["children_eligible"]
        totals["children_with_rule"] += result["children_with_rule"]
        totals["never"] += never
        samples.extend((family.name, item) for item in result["never_attempted"])

    print("-" * len(header))
    print(
        f"{'ALL':24} {'':>8} {totals['children']:9} {totals['children_eligible']:9} "
        f"{totals['children_with_rule']:8} {totals['never']:7} "
        f"({totals['never'] / max(1, totals['children']):3.0%})"
    )

    if args.samples:
        print(f"\nnever asked (showing {min(args.samples, len(samples))}):\n")
        for name, clause in samples[: args.samples]:
            print(f"  [{name}] {clause.get('id')}")
            print(f"     {str(clause.get('text', ''))[:110]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
