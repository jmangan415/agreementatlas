#!/usr/bin/env python3
"""Score extracted precedence against each vendor's own stated ordering.

`evaluation.py` measures the fictional Acme family, whose questions and answers
were written alongside the parser -- it can only confirm the tool still does what
its author expected. This scores real published agreements against ground truth
taken from the vendors' own precedence clauses, so a rule can be wrong here in a
way it cannot be there.

Reports precision and recall separately. They fail differently and it matters
which: a missing rule leaves a question unanswered, while a wrong rule answers it
confidently in the wrong direction.

    python scripts/corpus_eval.py --root knowledge/sources/<batch>/license-doc-sets
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legal_ingest import (  # noqa: E402
    SUPPORTED,
    detect_pdf_headings,
    extract_precedence,
    extract_source_text,
    make_instrument,
    parse_clauses,
)

GOLD = Path(__file__).resolve().parent / "corpus_gold.json"


def extracted_pairs(directory: Path, classifier) -> set[tuple[str, str]]:
    instruments = []
    clauses = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        text = extract_source_text(path)
        if not text.strip():
            continue
        instrument = make_instrument(path, text, "family", classifier=classifier)
        parsed, _ = parse_clauses(instrument, text, detect_pdf_headings(path))
        instruments.append(instrument)
        clauses.extend(parsed)
    by_id = {item.id: item for item in instruments}
    return {
        (
            by_id[rule.higher_instrument_id].source,
            by_id[rule.lower_instrument_id].source,
        )
        for rule in extract_precedence("family", instruments, clauses)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--classify-model", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    classifier = None
    if args.classify_model:
        from legal_graph_service import LMStudioClient, lm_instrument_classifier

        classifier = lm_instrument_classifier(LMStudioClient(), args.classify_model)

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    results = []
    found_total = correct_total = expected_total = 0
    for family in gold["families"]:
        directory = args.root / family["vendor"]
        if not directory.is_dir():
            continue
        with tempfile.TemporaryDirectory(prefix="corpus-eval-") as temporary:
            staging = Path(temporary) / "sources"
            staging.mkdir(parents=True)
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path.suffix.lower() in SUPPORTED:
                    shutil.copy2(path, staging / path.name)
            found = extracted_pairs(staging, classifier)
        expected = {(item["higher"], item["lower"]) for item in family["expected"]}
        correct = found & expected
        found_total += len(found)
        correct_total += len(correct)
        expected_total += len(expected)
        results.append(
            {
                "vendor": family["vendor"],
                "basis": family["basis"],
                "expected": sorted(expected),
                "correct": sorted(correct),
                "missed": sorted(expected - found),
                "unexpected": sorted(found - expected),
            }
        )

    summary = {
        # Precision counts every rule produced, including ones the gold file does
        # not mention -- an unexpected rule is not automatically wrong, but it is
        # unverified, and unverified precedence is what this tool exists to avoid.
        "precedence_precision": round(correct_total / max(1, found_total), 4),
        "precedence_recall": round(correct_total / max(1, expected_total), 4),
        "rules_expected": expected_total,
        "rules_extracted": found_total,
        "rules_correct": correct_total,
        "families": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"\n{'=' * 78}\nprecedence vs vendor-stated ground truth\n{'=' * 78}")
    for item in results:
        mark = "ok " if not item["missed"] and not item["unexpected"] else "   "
        print(f"\n{mark}{item['vendor']}  -- {item['basis']}")
        for higher, lower in item["correct"]:
            print(f"      correct    {higher[:40]:42} > {lower[:40]}")
        for higher, lower in item["missed"]:
            print(f"      MISSED     {higher[:40]:42} > {lower[:40]}")
        for higher, lower in item["unexpected"]:
            print(f"      unverified {higher[:40]:42} > {lower[:40]}")
    print(
        f"\n{'=' * 78}\n"
        f"precision {summary['precedence_precision']:.2f} "
        f"({correct_total}/{found_total} extracted rules confirmed)   "
        f"recall {summary['precedence_recall']:.2f} "
        f"({correct_total}/{expected_total} expected rules found)\n{'=' * 78}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
