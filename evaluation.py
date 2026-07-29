"""Acceptance metrics for AgreementAtlas gold-question sets.

Retrieval here must be the retrieval the application runs. The app fuses BM25
with local embeddings; scoring BM25 alone measured a pipeline the user never
sees, and on the SAP corpus the two disagree. The only model call this makes is
embedding each question -- if LM Studio is down, vector scoring degrades to
empty exactly as it does in the app, and the report says which one ran.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path

from legal_graph_service import (
    legal_resolution_trace,
    load_vectors,
    read_jsonl,
    retrieve_evidence,
)
from legal_ingest import rebuild_workspace
from lmstudio_client import LMStudioClient


def evaluate_workspace(root: Path, gold_path: Path) -> dict:
    embedding_client = LMStudioClient()
    index, payload = load_vectors(root)
    vectors_present = bool(index and payload)
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
        evidence = retrieve_evidence(
            root, item["question"], 14, embedding_client=embedding_client
        )
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

    # Negation inheritance, measured on the clauses that actually test it: a list
    # item under a negative chapeau must come out negative, because the "not" is
    # in the sentence above it and nowhere in its own words.
    #
    # This previously counted list-item rules that were PROHIBITION and NEGATIVE
    # and SHALL, all three, over every list item in the corpus. On the fictional
    # set that is exactly the three rules under "Customer shall not:", so it
    # reported 1.0 and read as a rate. On any real corpus it is not a rate at
    # all: SAP's fifty list items are legitimately 24 permissions, 11 "will not"
    # prohibitions and 10 "shall" obligations, of which one matched the triple,
    # so the same healthy corpus scored 0.02. It was asserting a fixture, under
    # the name of a property.
    clauses_by_id = {
        item["id"]: item for item in read_jsonl(root / "legal" / "clauses.jsonl")
    }
    negating = re.compile(r"\b(not|no|never|neither|nor|without)\b", re.I)
    list_rules = []
    for item in rules:
        clause = clauses_by_id.get(item["clause_id"])
        if not clause or clause.get("clause_kind") != "LIST_ITEM":
            continue
        chapeau = clauses_by_id.get(str(clause.get("chapeau_clause_id") or ""))
        if chapeau and negating.search(str(chapeau.get("text", ""))):
            list_rules.append(item)
    negation_correct = sum(item["polarity"] == "NEGATIVE" for item in list_rules)
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
        # Which retrieval actually ran. A score with vectors silently absent is
        # a different measurement wearing the same name.
        "retrieval": {
            "vectors_present": vectors_present,
            "embedder_reachable": bool(
                vectors_present and embedding_client.status().get("available")
            ),
        },
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
            "list_items_under_negative_chapeau": len(list_rules),
        },
        "negation_preservation_rate": round(
            negation_correct / max(1, len(list_rules)), 4
        ),
        "unsupported_claim_rate": float(1 - unsupported_correct),
        "details": details,
    }


def sap_family_root() -> Path | None:
    """The SAP Cloud family, if this machine has it loaded.

    The eight documents are SAP's own published cloud agreement set, but they are
    not committed -- `data/` is ignored in full -- so the gold set that depends on
    them has to find them rather than ship them, and say so plainly when it
    cannot.
    """

    try:
        from library_store import LibraryStore
    except ImportError:
        return None
    root = Path(__file__).resolve().parent / "data" / "library"
    if not root.is_dir():
        return None
    for family in LibraryStore(root).list():
        if family.name == "SAP Cloud":
            return family.root
    return None


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
    # The fictional corpus is 1,130 words across six documents, and its single
    # precedence question is answered by a class-keyed heuristic rather than by
    # reading a stated ladder -- so it scored 1.0 for precedence throughout the
    # period when the ladder parser was shredding a real one and "which document
    # controls" was answering "the evidence does not settle it". A second gold
    # set on a real vendor corpus is the only way that failure gets caught.
    parser.add_argument(
        "--corpus",
        choices=("acme", "sap"),
        default="acme",
        help="acme: the committed fictional set. sap: the SAP Cloud family in the "
        "local library, whose documents are not committed.",
    )
    args = parser.parse_args()
    gold = args.samples / (
        "gold_questions_sap.json" if args.corpus == "sap" else "gold_questions.json"
    )
    if args.corpus == "sap" and not args.root:
        family = sap_family_root()
        if family is None:
            print(
                json.dumps(
                    {
                        "skipped": "sap",
                        "reason": "The SAP Cloud family is not in this library. Its "
                        "documents are not committed, so this gold set only runs "
                        "where they have been loaded.",
                    },
                    indent=2,
                )
            )
            return
        print(json.dumps(evaluate_workspace(family, gold), indent=2))
        return
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
