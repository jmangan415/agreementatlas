from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from evaluation import evaluate_workspace
from legal_graph_service import (
    AgreementAtlasGraphRetriever,
    WorkspaceSchemaError,
    compact_graph,
    legal_resolution_trace,
    retrieve_evidence,
)
from legal_ingest import rebuild_workspace

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class LegalGraphV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.workspace = Path(cls.temporary.name)
        sources = cls.workspace / "sources"
        sources.mkdir()
        for source in SAMPLES.glob("acme-*.md"):
            shutil.copy2(source, sources / source.name)
        cls.summary = rebuild_workspace(cls.workspace)
        cls.legal = cls.workspace / "legal"
        cls.instruments = read_jsonl(cls.legal / "instruments.jsonl")
        cls.clauses = read_jsonl(cls.legal / "clauses.jsonl")
        cls.spans = read_jsonl(cls.legal / "evidence_spans.jsonl")
        cls.rules = read_jsonl(cls.legal / "operative_rules.jsonl")
        cls.precedence = read_jsonl(cls.legal / "precedence_rules.jsonl")
        cls.relationships = read_jsonl(cls.legal / "relationships.jsonl")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_schema_v3_records_and_instrument_taxonomy(self) -> None:
        self.assertEqual(self.summary["schema_version"], "3.0")
        self.assertEqual(self.summary["documents"], 6)
        types = {
            item["source"]: (item["instrument_class"], item["instrument_type"])
            for item in self.instruments
        }
        self.assertEqual(types["acme-cloud-master-agreement.md"], ("MASTER", "MSA"))
        self.assertEqual(
            types["acme-streamflow-order-schedule.md"],
            ("ORDER", "ORDER_SCHEDULE"),
        )
        self.assertEqual(types["acme-data-processing-addendum.md"], ("ADDENDUM", "DPA"))
        self.assertEqual(
            types["acme-cloud-master-amendment-1.md"],
            ("AMENDMENT", "AMENDMENT"),
        )
        self.assertEqual(
            types["acme-streamflow-service-level-agreement.md"],
            ("POLICY", "SLA"),
        )
        self.assertTrue(
            all(
                not item["effective_date"] or item["effective_date"].count("-") == 2
                for item in self.instruments
            )
        )
        for filename in (
            "agreement_families.jsonl",
            "parties.jsonl",
            "defined_terms.jsonl",
            "precedence_rules.jsonl",
            "cross_references.jsonl",
            "amendments.jsonl",
            "relationships.jsonl",
        ):
            self.assertTrue((self.legal / filename).exists(), filename)

    def test_chapeau_negation_is_propagated_with_two_exact_spans(self) -> None:
        list_clause_ids = {
            item["id"] for item in self.clauses if item["clause_kind"] == "LIST_ITEM"
        }
        list_rules = [
            item for item in self.rules if item["clause_id"] in list_clause_ids
        ]
        self.assertEqual(len(list_rules), 3)
        self.assertTrue(all(item["effect"] == "PROHIBITION" for item in list_rules))
        self.assertTrue(all(item["modality"] == "SHALL" for item in list_rules))
        self.assertTrue(all(item["polarity"] == "NEGATIVE" for item in list_rules))
        self.assertTrue(all(len(item["evidence_span_ids"]) == 2 for item in list_rules))
        span_ids = {item["id"] for item in self.spans}
        self.assertTrue(
            all(set(item["evidence_span_ids"]) <= span_ids for item in list_rules)
        )

    def test_precedence_is_scoped_evidence_backed_and_not_hardcoded(self) -> None:
        self.assertGreaterEqual(len(self.precedence), 8)
        span_lookup = {item["id"]: item for item in self.spans}
        clause_lookup = {item["id"]: item for item in self.clauses}
        for item in self.precedence:
            self.assertEqual(item["status"], "RESOLVED")
            self.assertTrue(item["evidence_span_ids"])
            evidence = " ".join(
                span_lookup[span_id]["text"] for span_id in item["evidence_span_ids"]
            )
            clause = clause_lookup[item["source_clause_id"]]
            self.assertIn(evidence, clause["text"])
            self.assertTrue(item["subject_scope"]["subject_matter"])
        edge_types = Counter(item["type"] for item in self.relationships)
        self.assertGreater(edge_types["CONTROLS_FOR_DEFINED_SCOPE"], 0)
        self.assertGreater(edge_types["OVERRIDES"], 0)
        self.assertGreater(edge_types["QUALIFIES"], 0)
        legal_edges = [
            item
            for item in self.relationships
            if item["type"] in {"CONTROLS_FOR_DEFINED_SCOPE", "OVERRIDES", "QUALIFIES"}
        ]
        self.assertTrue(all(item["evidence_span_ids"] for item in legal_edges))

    def test_controlling_definition_and_amendment_are_resolved(self) -> None:
        definitions = read_jsonl(self.legal / "defined_terms.jsonl")
        authorised = [item for item in definitions if item["term"] == "Authorised User"]
        self.assertEqual(len(authorised), 2)
        self.assertTrue(
            any(
                item["type"] == "CONTROLLING_DEFINITION"
                and item["source"] in {definition["id"] for definition in authorised}
                and item["target"] in {definition["id"] for definition in authorised}
                for item in self.relationships
            )
        )
        amendments = read_jsonl(self.legal / "amendments.jsonl")
        self.assertEqual(len(amendments), 1)
        self.assertEqual(amendments[0]["operation"], "REPLACE")
        self.assertEqual(amendments[0]["status"], "RESOLVED")
        self.assertEqual(amendments[0]["effective_date"], "2026-04-01")
        self.assertTrue(any(item["type"] == "AMENDS" for item in self.relationships))

    def test_retrieval_uses_directional_legal_edges_without_hubs(self) -> None:
        retriever = AgreementAtlasGraphRetriever()
        evidence = retriever.retrieve(
            self.workspace,
            "Can Customer allocate StreamFlow access to an Affiliate?",
            14,
        )
        self.assertEqual(retriever.name, "agreementatlas-graph")
        self.assertEqual(evidence[0]["source"], "acme-streamflow-order-schedule.md")
        text = "\n".join(item["text"] for item in evidence)
        self.assertIn("may not allocate StreamFlow access", text)
        self.assertIn("may permit its Affiliates", text)
        paths = [
            item.get("graph_relationships", [])
            for item in evidence
            if item.get("graph_relationships")
        ]
        self.assertTrue(paths)
        flattened = " ".join(value for path in paths for value in path)
        for banned in ("CONTAINS", "HAS_ROLE", "BELONGS_TO"):
            self.assertNotIn(banned, flattened)
        self.assertTrue(
            any(
                "in:OVERRIDES" in flattened_path or "in:QUALIFIES" in flattened_path
                for flattened_path in (" ".join(path) for path in paths)
            )
        )

    def test_resolution_trace_handles_scope_definitions_amendments_and_no_answer(
        self,
    ) -> None:
        affiliate_evidence = retrieve_evidence(
            self.workspace,
            "Can Customer allocate StreamFlow access to an Affiliate?",
            14,
        )
        affiliate_trace = legal_resolution_trace(
            self.workspace,
            "Can Customer allocate StreamFlow access to an Affiliate?",
            affiliate_evidence,
        )
        self.assertEqual(affiliate_trace["status"], "RESOLVED")
        self.assertTrue(
            any(
                item["source"] == "acme-streamflow-order-schedule.md"
                and item["final_status"] == "CONTROLLING"
                for item in affiliate_trace["steps"]
            )
        )

        definition_evidence = retrieve_evidence(
            self.workspace, "Who is an Authorised User for InsightHub?", 12
        )
        self.assertEqual(
            definition_evidence[0]["source"],
            "acme-insighthub-order-schedule.md",
        )
        definition_trace = legal_resolution_trace(
            self.workspace,
            "Who is an Authorised User for InsightHub?",
            definition_evidence,
        )
        self.assertTrue(definition_trace["definition_steps"])

        amendment_evidence = retrieve_evidence(
            self.workspace,
            "What must Customer do about account credentials today?",
            12,
        )
        amendment_trace = legal_resolution_trace(
            self.workspace,
            "What must Customer do about account credentials today?",
            amendment_evidence,
        )
        self.assertTrue(
            any(item["final_status"] == "AMENDED" for item in amendment_trace["steps"])
        )

        sla = retrieve_evidence(
            self.workspace,
            "Does announced maintenance count against StreamFlow availability?",
            12,
        )
        self.assertTrue(
            any(
                item["source"] == "acme-streamflow-service-level-agreement.md"
                and "announced maintenance" in item["text"]
                for item in sla
            )
        )
        self.assertEqual(
            retrieve_evidence(
                self.workspace,
                "What insurance coverage must Customer maintain?",
                12,
            ),
            [],
        )

    def test_stable_ids_and_clause_anatomy_projection(self) -> None:
        before = {
            filename: [item["id"] for item in read_jsonl(self.legal / filename)]
            for filename in (
                "instruments.jsonl",
                "clauses.jsonl",
                "operative_rules.jsonl",
                "relationships.jsonl",
            )
        }
        rebuild_workspace(self.workspace)
        after = {
            filename: [item["id"] for item in read_jsonl(self.legal / filename)]
            for filename in before
        }
        self.assertEqual(before, after)
        graph = compact_graph(self.workspace, max_rules=100)
        self.assertEqual(graph["schema_version"], "3.0")
        anatomy_nodes = [
            item
            for item in graph["nodes"]
            if item.get("type") == "rule"
            and item.get("modality") == "SHALL"
            and item.get("evidence_segments")
        ]
        self.assertTrue(anatomy_nodes)
        self.assertTrue(
            any(
                {segment["purpose"] for segment in item["evidence_segments"]}
                == {"chapeau", "list_item"}
                for item in anatomy_nodes
            )
        )

    def test_schema_v2_is_rejected_for_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "output").mkdir()
            (root / "output" / "legal_relationship_graph.json").write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "nodes": [],
                        "relationships": [],
                    }
                ),
                encoding="utf-8",
            )
            graph = compact_graph(root)
            self.assertTrue(graph["rebuild_required"])
            with self.assertRaises(WorkspaceSchemaError):
                retrieve_evidence(root, "What applies?")

    def test_fictional_gold_metrics(self) -> None:
        metrics = evaluate_workspace(self.workspace, SAMPLES / "gold_questions.json")
        self.assertEqual(metrics["controlling_clause_precision"], 1.0)
        self.assertEqual(metrics["instrument_classification_accuracy"], 1.0)
        self.assertEqual(metrics["precedence_resolution_accuracy"], 1.0)
        self.assertEqual(metrics["definition_conflict_accuracy"], 1.0)
        self.assertEqual(metrics["negation_preservation_rate"], 1.0)
        self.assertEqual(metrics["unsupported_claim_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
