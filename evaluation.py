"""Model-free acceptance metrics for the fictional AgreementAtlas family."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from legal_graph_service import legal_resolution_trace, read_jsonl, retrieve_evidence
from legal_ingest import rebuild_workspace


def evaluate_workspace(root: Path, gold_path: Path) -> dict:
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    instruments = read_jsonl(root / "legal" / "instruments.jsonl")
    rules = read_jsonl(root / "legal" / "operative_rules.jsonl")
    supported = [item for item in gold if item["id"] != "unsupported"]
    source_questions = [item for item in supported if item["expects"].get("source")]
    precedence_questions = [
        item
        for item in gold
        if item["expects"].get("relationship") == "CONTROLS_FOR_DEFINED_SCOPE"
    ]
    definition_questions = [
        item
        for item in gold
        if item["expects"].get("relationship") == "CONTROLLING_DEFINITION"
    ]

    correct_sources = 0
    unsupported_correct = 0
    precedence_correct = 0
    definition_correct = 0
    details: list[dict] = []
    for item in gold:
        evidence = retrieve_evidence(root, item["question"], 14)
        trace = legal_resolution_trace(root, item["question"], evidence)
        expected = item["expects"]
        if item["id"] == "instrument-classification":
            expected_types = set(expected["instrument_types"])
            actual_types = {record["instrument_type"] for record in instruments}
            passed = expected_types <= actual_types
        elif item["id"] == "unsupported":
            passed = not evidence and trace["status"] == "UNRESOLVED"
            unsupported_correct += int(passed)
        else:
            expected_source = expected.get("source", "")
            passed = bool(evidence and evidence[0]["source"] == expected_source)
            if expected_source:
                correct_sources += int(passed)
            if expected.get("relationship") == "CONTROLS_FOR_DEFINED_SCOPE":
                precedence_correct += int(
                    any(record.get("source") == expected_source for record in evidence)
                    and trace["status"] == "RESOLVED"
                )
            if expected.get("relationship") == "CONTROLLING_DEFINITION":
                definition_correct += int(bool(trace["definition_steps"]))
        details.append(
            {
                "id": item["id"],
                "passed": passed,
                "top_source": evidence[0]["source"] if evidence else "",
                "resolution_status": trace["status"],
            }
        )

    list_clause_ids = {
        item["id"]
        for item in read_jsonl(root / "legal" / "clauses.jsonl")
        if item.get("clause_kind") == "LIST_ITEM"
    }
    list_rules = [item for item in rules if item["clause_id"] in list_clause_ids]
    negation_correct = sum(
        item["effect"] == "PROHIBITION"
        and item["polarity"] == "NEGATIVE"
        and item["modality"] == "SHALL"
        for item in list_rules
    )
    expected_types = set(
        next(
            item["expects"]["instrument_types"]
            for item in gold
            if item["id"] == "instrument-classification"
        )
    )
    actual_types = {item["instrument_type"] for item in instruments}
    return {
        "schema_version": "3.0",
        "questions": len(gold),
        "controlling_clause_precision": round(
            correct_sources / max(1, len(source_questions)), 4
        ),
        "instrument_classification_accuracy": round(
            len(expected_types & actual_types) / len(expected_types), 4
        ),
        # Rates, with their denominators stated. These were previously emitted as
        # raw counts named "accuracy", so a single passing question reported 1.0
        # and read as 100%.
        "precedence_resolution_accuracy": round(
            precedence_correct / max(1, len(precedence_questions)), 4
        ),
        "definition_conflict_accuracy": round(
            definition_correct / max(1, len(definition_questions)), 4
        ),
        "sample_sizes": {
            "questions": len(gold),
            "source_questions": len(source_questions),
            "precedence_questions": len(precedence_questions),
            "definition_questions": len(definition_questions),
            "list_item_rules": len(list_rules),
        },
        "negation_preservation_rate": round(
            negation_correct / max(1, len(list_rules)), 4
        ),
        "unsupported_claim_rate": float(1 - unsupported_correct),
        "details": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate AgreementAtlas against its fictional gold questions."
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Existing schema-v3 workspace. Omit to build from samples.",
    )
    parser.add_argument(
        "--samples",
        type=Path,
        default=Path(__file__).resolve().parent / "samples",
    )
    args = parser.parse_args()
    gold = args.samples / "gold_questions.json"
    if args.root:
        print(json.dumps(evaluate_workspace(args.root, gold), indent=2))
        return
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = root / "sources"
        sources.mkdir()
        for source in args.samples.glob("acme-*.md"):
            shutil.copy2(source, sources / source.name)
        rebuild_workspace(root)
        print(json.dumps(evaluate_workspace(root, gold), indent=2))


if __name__ == "__main__":
    main()
