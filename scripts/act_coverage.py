#!/usr/bin/env python3
"""How much of each clause do its extracted rules actually account for?

A rule is supposed to state one thing an agreement does: who may, must or must
not do what. A clause is a container and routinely holds several. Section 13.1
of a typical confidentiality provision holds four -- a permission to disclose,
an obligation to hold in confidence, and two prohibitions -- and yields one
rule, labelled from the first act, whose evidence span covers all four. The
interface then highlights words the label does not describe, and three of the
four acts are invisible to retrieval.

Two numbers matter and they are different:

  dropped    a clause holds more acts than it produced rules, so meaning is
             missing from the graph entirely
  overclaim  a rule's evidence contains acts other than its own, so the span
             asserts more than the rule says

Counting acts by regex is the obvious way to be wrong, so `--samples` prints the
clauses behind the numbers. Read them before believing the percentages.

    python scripts/act_coverage.py
    python scripts/act_coverage.py --only OpenText --samples 12
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from library_store import LibraryStore  # noqa: E402

# Ordered longest-first and matched non-overlapping, so "may not" is one act
# rather than "may" plus a stray negative. Infinitive duties are included
# because a drafter writes "agrees to hold ... not to disclose ... and not to
# use", which carries three duties past any modal-keyword detector.
ACT = re.compile(
    r"\b("
    r"shall not|must not|may not|will not|is not required to|is not entitled to|"
    r"agrees not to|undertakes not to|not to [a-z]+|"
    r"shall|must|may|will be deemed|is required to|is entitled to|"
    r"agrees to|undertakes to|covenants to"
    r")\b",
    re.I,
)
# A modal inside a definition or a recital is not an operative act. "means any
# entity that may control" defines a term; it imposes nothing.
NON_OPERATIVE = re.compile(r"^\s*[\"“(]?[A-Z][^\"”]{0,60}[\"”]?\s+means\b", re.I)
# "Licensee may allow no more than five individuals (may be employees or
# contractors of Licensee) to access the Software" contains one permission. The
# parenthetical describes who those individuals are; counting its modal reported
# a second act that the drafter never wrote, and so reported a rule quoting one
# statement as though it quoted two.
PARENTHETICAL = re.compile(r"\([^()]{0,200}\)")
# A relative clause qualifies a noun rather than binding a party: "information
# which is required to be disclosed by law" is part of the description of the
# information, not a duty to disclose it. These appear throughout the carve-out
# limbs of a confidentiality definition, where every limb was being counted as
# an act.
RELATIVE = re.compile(
    r"\b(?:which|that|who|whom|whose)\s+(?:[\w'-]+\s+){0,2}?"
    r"(?:shall|must|may|will|is\s+required\s+to|is\s+entitled\s+to)\b",
    re.I,
)
# A lettered limb opening on a copula has no actor: "(e) is required to be
# disclosed by the Receiving Party as a matter of law" completes "shall not
# apply to any information that:" several limbs earlier, so the relative
# pronoun is too far back to see. It states when an obligation does not bite,
# not a duty on anyone.
COPULA_LIMB = re.compile(
    r"(?:^|;|\.)\s*\(?[a-z]\)\s*(?:is|are|was|were)\s+[a-z]+", re.I
)


def acts(text: str) -> list[str]:
    """The deontic acts a passage imposes, excluding what it merely describes.

    Precision matters more than recall here: this number is used to decide
    whether extraction is dropping meaning, and a detector that counts asides
    reports work that does not exist.
    """

    if not text or NON_OPERATIVE.match(text.strip()):
        return []
    # Blanked rather than removed, so offsets and word boundaries survive.
    stripped = PARENTHETICAL.sub(lambda m: " " * len(m.group(0)), text)
    stripped = RELATIVE.sub(lambda m: " " * len(m.group(0)), stripped)
    # Blank from the limb marker to the end of its own limb, so the descriptive
    # clause it introduces is not read as a duty.
    for match in COPULA_LIMB.finditer(stripped):
        end = stripped.find(";", match.end())
        end = len(stripped) if end < 0 else end
        stripped = (
            stripped[: match.start()] + " " * (end - match.start()) + stripped[end:]
        )
    return [match.group(1).lower() for match in ACT.finditer(stripped)]


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open() if line.strip()]


def survey(root: Path) -> dict:
    clauses = {item["id"]: item for item in read(root / "legal" / "clauses.jsonl")}
    rules = read(root / "legal" / "operative_rules.jsonl")
    by_clause: dict[str, list[dict]] = {}
    for rule in rules:
        by_clause.setdefault(str(rule.get("clause_id", "")), []).append(rule)

    dropped_clauses: list[tuple[dict, int, int]] = []
    dropped_acts = 0
    overclaim: list[dict] = []
    total_acts = 0

    for clause_id, clause_rules in by_clause.items():
        clause = clauses.get(clause_id)
        if not clause:
            continue
        count = len(acts(str(clause.get("text", ""))))
        total_acts += count
        if count > len(clause_rules):
            dropped_clauses.append((clause, count, len(clause_rules)))
            dropped_acts += count - len(clause_rules)

    for rule in rules:
        if len(acts(str(rule.get("evidence", "")))) > 1:
            overclaim.append(rule)

    return {
        "clauses_with_rules": len(by_clause),
        "rules": len(rules),
        "acts": total_acts,
        "dropped_clauses": dropped_clauses,
        "dropped_acts": dropped_acts,
        "overclaim": overclaim,
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
    print(f"{'family':24} {'rules':>7} {'acts':>7} {'dropped':>9} {'overclaim':>11}")
    print("-" * 62)
    totals = {"rules": 0, "acts": 0, "dropped_acts": 0, "overclaim": 0}
    samples: list[tuple[str, dict, int, int]] = []
    for family in families:
        result = survey(family.root)
        if not result["rules"]:
            continue
        over = len(result["overclaim"])
        print(
            f"{family.name[:24]:24} {result['rules']:7} {result['acts']:7} "
            f"{result['dropped_acts']:9} "
            f"{over:6} ({over / result['rules']:3.0%})"
        )
        totals["rules"] += result["rules"]
        totals["acts"] += result["acts"]
        totals["dropped_acts"] += result["dropped_acts"]
        totals["overclaim"] += over
        samples.extend(
            (family.name, clause, count, got)
            for clause, count, got in result["dropped_clauses"]
        )

    print("-" * 62)
    print(
        f"{'ALL':24} {totals['rules']:7} {totals['acts']:7} "
        f"{totals['dropped_acts']:9} "
        f"{totals['overclaim']:6} ({totals['overclaim'] / max(1, totals['rules']):3.0%})"
    )

    if args.samples:
        print(
            f"\nclauses holding more acts than they produced rules "
            f"(showing {min(args.samples, len(samples))} of {len(samples)}):\n"
        )
        samples.sort(key=lambda item: item[3] - item[2])
        for name, clause, count, got in samples[: args.samples]:
            print(
                f"  [{name}] §{clause.get('section_id', '')} "
                f"{str(clause.get('heading', ''))[:38]}  {count} acts -> {got} rule(s)"
            )
            print(f"     acts: {acts(str(clause.get('text', '')))}")
            print(f"     {str(clause.get('text', ''))[:200]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
