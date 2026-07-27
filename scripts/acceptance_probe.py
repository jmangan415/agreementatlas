#!/usr/bin/env python3
"""Measure how many rules the model returns survive validation, and why.

Phase 1 moved acceptance on a fixed set of OpenText clauses from 1 of 12 to
13 of 13 by widening the extraction contract. That number was measured on the
corpus the fix was developed against, so it proves the fix works there and
nothing more. This runs the same measurement over a family the pipeline has
never been tuned on.

It sends production's own prompt and schema -- ``EXTRACTION_SYSTEM``,
``extraction_prompt`` and ``rule_schema`` are imported, not copied -- so the
figure it reports is the figure enrichment would get. Nothing is written to the
family; the workspace is read only.

    python scripts/acceptance_probe.py --family Salesforce --clauses 40
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legal_graph_service import (  # noqa: E402
    EXTRACTION_SYSTEM,
    NOT_STATED,
    VALID_EFFECTS,
    VALID_MODALITIES,
    VALID_POLARITIES,
    actor_choices,
    allowed_actors,
    compact_text,
    extraction_batches,
    extraction_prompt,
    read_jsonl,
    resolve_returned_clause_id,
    rule_schema,
    substantive_clauses,
    validate_extracted_rule,
)
from library_store import LibraryStore  # noqa: E402
from lmstudio_client import LMStudioClient, LMStudioError  # noqa: E402


def rejection_reason(
    item: dict,
    clause: dict,
    clause_lookup: dict[str, dict],
    actors: set[str],
) -> str:
    """Name the gate that discarded a rule, in the order the validator applies.

    The validator returns None for every failure alike, which is right for
    production and useless for measurement: it cannot distinguish a hallucinated
    quote from a mislabelled modality. This repeats its checks to say which one
    fired, so a low acceptance rate points at a specific contract to widen.
    """

    if str(item.get("clause_id", "")) != str(clause.get("id", "")):
        return "clause_id"
    effect = str(item.get("effect") or item.get("rule_type", "")).upper()
    if effect not in VALID_EFFECTS:
        return f"effect={effect or '<blank>'}"
    modality = str(item.get("modality", "")).upper()
    if modality and modality not in VALID_MODALITIES:
        return f"modality={modality}"
    polarity = str(item.get("polarity", "")).upper()
    if polarity and polarity not in VALID_POLARITIES:
        return f"polarity={polarity}"

    raw = item.get("evidence_spans") or (
        [item["evidence"]] if item.get("evidence") else []
    )
    spans = [str(value) for value in raw if str(value).strip()]
    if not spans:
        return "evidence=<none>"
    permitted = [
        compact_text(str(candidate.get("text", "")))
        for candidate in clause_lookup.values()
        if candidate.get("id") in {clause.get("id"), clause.get("chapeau_clause_id")}
    ]
    for span in spans:
        if not any(compact_text(span) in text for text in permitted):
            return "evidence=not-in-clause"

    chapeau_id = str(clause.get("chapeau_clause_id", ""))
    chapeau = clause_lookup.get(chapeau_id) if chapeau_id else None
    if chapeau:
        text = str(chapeau.get("text", "")).lower()
        negating = any(
            phrase in text
            for phrase in (
                "shall not",
                "must not",
                "may not",
                "will not",
                "cannot",
                "can not",
                "not permitted",
            )
        )
        if negating and (polarity != "NEGATIVE" or effect != "PROHIBITION"):
            return "chapeau-negates"

    actor = compact_text(str(item.get("actor", "")))
    if actor and actor != NOT_STATED and actor not in actors:
        return f"actor={actor[:32]}"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/library"))
    parser.add_argument("--family", required=True)
    parser.add_argument("--clauses", type=int, default=40)
    parser.add_argument("--model", default="google/gemma-4-26b-a4b-qat")
    parser.add_argument("--json", type=Path, help="write the full record here")
    args = parser.parse_args()

    family = next(
        (
            item
            for item in LibraryStore(args.root).list()
            if item.name.casefold() == args.family.casefold()
        ),
        None,
    )
    if family is None:
        print(f"no family named {args.family!r} under {args.root}", file=sys.stderr)
        return 2

    root = family.root
    clauses = substantive_clauses(root)[: args.clauses]
    if not clauses:
        print(f"{family.name} has no substantive clauses", file=sys.stderr)
        return 2
    clause_lookup = {
        str(item.get("id", "")): item
        for item in read_jsonl(root / "legal" / "clauses.jsonl")
    }
    span_lookup = {
        str(item.get("id", "")): item
        for item in read_jsonl(root / "legal" / "evidence_spans.jsonl")
    }
    actors = allowed_actors(root)
    schema = rule_schema(actor_choices(root))

    client = LMStudioClient()
    returned = 0
    accepted = 0
    implied = 0
    reasons: Counter[str] = Counter()
    clauses_with_a_rule: set[str] = set()
    records: list[dict] = []

    batches = extraction_batches(clauses)
    print(
        f"probing {family.name}: {len(clauses)} clauses in {len(batches)} batches, "
        f"{len(actor_choices(root))} actor choices",
        file=sys.stderr,
    )
    for index, batch in enumerate(batches, start=1):
        batch_ids = {str(item["id"]) for item in batch}
        try:
            result = client.structured_chat(
                model=args.model,
                system=EXTRACTION_SYSTEM,
                user=extraction_prompt(batch, clause_lookup),
                schema=schema,
            )
        except LMStudioError as error:
            print(f"  batch {index}: {error}", file=sys.stderr)
            continue
        for raw in result.get("rules", []):
            if not isinstance(raw, dict):
                continue
            clause_id = resolve_returned_clause_id(raw.get("clause_id", ""), batch_ids)
            clause = clause_lookup.get(clause_id)
            if clause_id not in batch_ids or not clause:
                returned += 1
                reasons["clause_id"] += 1
                continue
            raw = {**raw, "clause_id": clause_id}
            returned += 1
            if raw.get("actor_is_implied"):
                implied += 1
            validated = validate_extracted_rule(
                raw,
                clause,
                clause_lookup=clause_lookup,
                span_lookup=span_lookup,
                actors=actors,
            )
            if validated:
                accepted += 1
                clauses_with_a_rule.add(clause_id)
            else:
                reason = rejection_reason(raw, clause, clause_lookup, actors)
                reasons[reason] += 1
                records.append({"clause_id": clause_id, "reason": reason, "rule": raw})
        print(
            f"  batch {index}/{len(batches)}: {accepted}/{returned} accepted",
            file=sys.stderr,
        )

    print(f"\n{'=' * 68}\nacceptance: {family.name}\n{'=' * 68}")
    print(f"rules returned   {returned}")
    print(f"rules accepted   {accepted}  ({accepted / max(1, returned):.0%})")
    print(f"actor implied    {implied}  ({implied / max(1, returned):.0%} of returned)")
    print(
        f"clauses covered  {len(clauses_with_a_rule)}/{len(clauses)} "
        f"({len(clauses_with_a_rule) / max(1, len(clauses)):.0%})"
    )
    if reasons:
        print("\nrejected by gate:")
        for reason, count in reasons.most_common():
            print(f"  {reason:28} {count}")

    if args.json:
        args.json.write_text(json.dumps(records, indent=2), encoding="utf-8")
        print(f"\nrejected rules written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
