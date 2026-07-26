from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legal_graph_service import (
    LMStudioError,
    answer_question,
    enrich_workspace,
    query_terms,
    read_jsonl,
    resolve_returned_clause_id,
    retrieve_evidence,
    stem,
    substantive_clauses,
    validate_extracted_rule,
)
from legal_ingest import rebuild_workspace
from lmstudio_client import LMStudioClient

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class FakeDeepClient:
    embedding_model = "fake-nomic"

    def __init__(self) -> None:
        self.structured_calls = 0
        self.embedding_input_types: list[str] = []

    def structured_chat(self, **kwargs: object) -> dict:
        self.structured_calls += 1
        user = str(kwargs["user"])
        blocks = re.findall(
            r"\[CLAUSE_ID\] ([^\n]+)\n"
            r"\[SECTION\] ([^\n]+)\n"
            r"\[STRUCTURED_SCOPE\] ([^\n]+)"
            r"(?:\n\[CHAPEAU\] ([^\n]+))?"
            r"\n\[TEXT\] (.*?)(?=\n\n\[CLAUSE_ID\]|\Z)",
            user,
            re.S,
        )
        rules = []
        for clause_id, _section, raw_scope, chapeau, text in blocks:
            evidence = [value.strip() for value in (chapeau, text) if value.strip()]
            combined = " ".join(evidence)
            negative = bool(
                re.search(
                    r"\b(shall not|must not|may not|cannot|can not)\b",
                    combined,
                    re.I,
                )
            )
            if re.search(r"\bexcluded from\b", combined, re.I):
                effect, modality, polarity = "EXCLUSION", "OTHER", "NEGATIVE"
            elif re.search(r"\bsole (?:monetary )?remedy\b", combined, re.I):
                effect, modality, polarity = "REMEDY", "OTHER", "POSITIVE"
            elif negative:
                effect, polarity = "PROHIBITION", "NEGATIVE"
                modality = "SHALL" if "shall" in combined.lower() else "MUST"
            elif re.search(r"\bmay\b", combined, re.I):
                effect, modality, polarity = "PERMISSION", "MAY", "POSITIVE"
            else:
                effect, modality, polarity = "OBLIGATION", "MUST", "POSITIVE"
            rules.append(
                {
                    "clause_id": clause_id,
                    "effect": effect,
                    "modality": modality,
                    "polarity": polarity,
                    "actor": "Customer",
                    "action": "apply contractual rule",
                    "object": "agreement subject",
                    "scope": json.loads(raw_scope),
                    "conditions": [],
                    "carve_outs": [],
                    "cross_refs": [],
                    "summary": "Validated fictional contractual rule.",
                    "evidence_spans": evidence,
                }
            )
        return {"rules": rules}

    def embeddings(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str,
    ) -> list[list[float]]:
        self.embedding_input_types.append(input_type)
        vectors = []
        for text in texts:
            lower = text.lower()
            vector = [
                1.0 + lower.count("credential"),
                1.0 + lower.count("affiliate"),
                1.0 + lower.count("availability"),
                1.0 + lower.count("personal data"),
            ]
            vectors.append(vector)
        return vectors


class FakeNativeClient(LMStudioClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1234/v1")
        self.loaded: list[dict] = []

    def _native_request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        del method, timeout
        if path == "models":
            return {
                "models": [
                    {
                        "key": "allowed-extractor",
                        "type": "llm",
                        "display_name": "Allowed",
                        "loaded_instances": self.loaded,
                    }
                ]
            }
        if path == "models/load":
            instance = {
                "id": str(payload["model"]),
                "config": {"context_length": payload.get("context_length", 4096)},
            }
            self.loaded.append(instance)
            return {
                "type": "llm",
                "instance_id": instance["id"],
                "status": "loaded",
            }
        if path == "models/unload":
            instance_id = str(payload["instance_id"])
            self.loaded = [item for item in self.loaded if item["id"] != instance_id]
            return {"instance_id": instance_id}
        raise AssertionError(path)


class PrefixCaptureClient(LMStudioClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://127.0.0.1:1234/v1")
        self.payload: dict = {}

    def _openai_request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        del method, path, timeout
        self.payload = payload or {}
        if "messages" in self.payload:
            return {"choices": [{"message": {"content": '{"rules": []}'}}]}
        return {
            "data": [
                {"index": index, "embedding": [1.0, float(index + 1)]}
                for index, _ in enumerate(self.payload["input"])
            ]
        }


class FixedRetriever:
    name = "fixed-test-retriever"

    def retrieve(self, root: Path, question: str) -> list[dict]:
        return retrieve_evidence(root, question, 8)


class NoCitationChatClient:
    def chat(self, **_kwargs: object) -> str:
        return "Customer may allocate access only under the stated conditions."


class DeepIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        sources = self.workspace / "sources"
        sources.mkdir()
        for source in SAMPLES.glob("acme-*.md"):
            shutil.copy2(source, sources / source.name)
        rebuild_workspace(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validation_rejects_lost_negation_actor_and_nonexistent_evidence(
        self,
    ) -> None:
        clauses = read_jsonl(self.workspace / "legal" / "clauses.jsonl")
        spans = read_jsonl(self.workspace / "legal" / "evidence_spans.jsonl")
        clause_lookup = {item["id"]: item for item in clauses}
        span_lookup = {item["id"]: item for item in spans}
        item_clause = next(
            item for item in clauses if item["clause_kind"] == "LIST_ITEM"
        )
        chapeau = clause_lookup[item_clause["chapeau_clause_id"]]
        base = {
            "clause_id": item_clause["id"],
            "effect": "PROHIBITION",
            "modality": "SHALL",
            "polarity": "NEGATIVE",
            "actor": "Customer",
            "action": "restrict",
            "object": "service",
            "scope": item_clause["scope"],
            "conditions": [],
            "carve_outs": [],
            "cross_refs": [],
            "summary": "Customer is prohibited.",
            "evidence_spans": [chapeau["text"], item_clause["text"]],
        }
        self.assertIsNotNone(
            validate_extracted_rule(
                base,
                item_clause,
                clause_lookup=clause_lookup,
                span_lookup=span_lookup,
                actors={"customer"},
            )
        )
        lost_negation = {**base, "effect": "PERMISSION", "polarity": "POSITIVE"}
        self.assertIsNone(
            validate_extracted_rule(
                lost_negation,
                item_clause,
                clause_lookup=clause_lookup,
                span_lookup=span_lookup,
                actors={"customer"},
            )
        )
        invalid_actor = {**base, "actor": "Software"}
        self.assertIsNone(
            validate_extracted_rule(
                invalid_actor,
                item_clause,
                clause_lookup=clause_lookup,
                span_lookup=span_lookup,
                actors={"customer"},
            )
        )
        invented = {**base, "evidence_spans": ["invented evidence"]}
        self.assertIsNone(
            validate_extracted_rule(
                invented,
                item_clause,
                clause_lookup=clause_lookup,
                span_lookup=span_lookup,
                actors={"customer"},
            )
        )

    def test_clause_namespace_repair_is_batch_scoped_and_unambiguous(self) -> None:
        clause_id = "clause:abc123"
        self.assertEqual(
            resolve_returned_clause_id("abc123", {clause_id}),
            clause_id,
        )
        self.assertEqual(
            resolve_returned_clause_id("clause:abc123", {clause_id}),
            clause_id,
        )
        self.assertEqual(
            resolve_returned_clause_id("abc123", {"clause:abc123", "abc123"}),
            "abc123",
        )
        self.assertEqual(
            resolve_returned_clause_id("invented", {clause_id}),
            "",
        )

    def test_complete_deep_build_replaces_successes_and_builds_vectors(self) -> None:
        client = FakeDeepClient()
        progress: list[tuple[int, int]] = []
        with patch.dict(
            os.environ,
            {
                "LMSTUDIO_BATCH_CLAUSES": "4",
                "LMSTUDIO_EMBED_BATCH": "8",
            },
        ):
            summary = enrich_workspace(
                self.workspace,
                client,
                "fake-gemma",
                progress=lambda complete, total: progress.append((complete, total)),
            )
        self.assertEqual(summary["build_mode"], "deep")
        self.assertEqual(summary["completed"], summary["clauses_considered"])
        self.assertEqual(summary["failed_clauses"], 0)
        self.assertEqual(summary["embedding"]["status"], "complete")
        self.assertGreater(summary["embedding"]["records"], summary["rules"])
        self.assertEqual(progress[-1], (summary["completed"], summary["completed"]))
        self.assertTrue((self.workspace / "legal" / "embeddings.f32").exists())
        self.assertTrue((self.workspace / "legal" / "embeddings.index.jsonl").exists())
        self.assertTrue(
            (
                self.workspace / "output" / "legal_relationship_graph_enriched.json"
            ).exists()
        )
        resolved = read_jsonl(self.workspace / "legal" / "resolved_rules.jsonl")
        lm_rules = read_jsonl(self.workspace / "legal" / "lm_rules.jsonl")
        successful = {item["clause_id"] for item in lm_rules}
        self.assertTrue(successful)
        self.assertFalse(
            any(
                item["extraction_method"] == "deterministic"
                and item["clause_id"] in successful
                for item in resolved
            )
        )
        self.assertIn("search_document", client.embedding_input_types)
        evidence = retrieve_evidence(
            self.workspace,
            "Can Customer share account credentials?",
            8,
            embedding_client=client,
        )
        self.assertTrue(evidence)
        self.assertTrue(
            any(
                "vector_rank" in item.get("retrieval_components", {})
                for item in evidence
            )
        )
        self.assertIn("search_query", client.embedding_input_types)

    def test_checkpoint_resume_skips_completed_work(self) -> None:
        first_client = FakeDeepClient()
        cancellation = {"requested": False}

        def progress(_completed: int, _total: int) -> None:
            cancellation["requested"] = True

        with patch.dict(os.environ, {"LMSTUDIO_BATCH_CLAUSES": "2"}):
            with self.assertRaisesRegex(LMStudioError, "cancelled"):
                enrich_workspace(
                    self.workspace,
                    first_client,
                    "fake-gemma",
                    progress=progress,
                    cancelled=lambda: cancellation["requested"],
                )
        checkpoint = json.loads(
            (self.workspace / "legal" / "deep_build_checkpoint.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertGreater(checkpoint["completed"], 0)
        total_batches = (len(substantive_clauses(self.workspace)) + 1) // 2
        second_client = FakeDeepClient()
        with patch.dict(os.environ, {"LMSTUDIO_BATCH_CLAUSES": "2"}):
            summary = enrich_workspace(self.workspace, second_client, "fake-gemma")
        self.assertTrue(summary["resumed"])
        self.assertLess(second_client.structured_calls, total_batches)
        self.assertEqual(summary["completed"], summary["clauses_considered"])

    def test_bm25_remains_available_without_embeddings(self) -> None:
        evidence = retrieve_evidence(
            self.workspace,
            "Does announced maintenance count against StreamFlow availability?",
            8,
        )
        self.assertTrue(evidence)
        self.assertTrue(
            all(
                "vector_rank" not in item.get("retrieval_components", {})
                for item in evidence
            )
        )
        self.assertTrue(
            any(
                "bm25_rank" in item.get("retrieval_components", {}) for item in evidence
            )
        )

    def test_answer_adds_a_source_marker_when_model_omits_citations(self) -> None:
        answer = answer_question(
            self.workspace,
            NoCitationChatClient(),  # type: ignore[arg-type]
            "fake-model",
            "Can Customer allocate StreamFlow access to an Affiliate?",
            retriever=FixedRetriever(),  # type: ignore[arg-type]
        )
        self.assertIn("[1]", answer["answer"])


class LMStudioManagementTests(unittest.TestCase):
    def test_allowlisted_load_and_managed_only_unload(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LMSTUDIO_MODEL": "allowed-extractor",
                "LMSTUDIO_EMBEDDING_MODEL": "allowed-embedder",
                "LMSTUDIO_ALLOWED_MODELS": "",
            },
        ):
            client = FakeNativeClient()
            result = client.load_model("allowed-extractor", context_length=32768)
            self.assertEqual(result["status"], "loaded")
            self.assertIn("allowed-extractor", client.managed_instance_ids)
            with self.assertRaisesRegex(LMStudioError, "allowlist"):
                client.load_model("visitor-selected-model")
            with self.assertRaisesRegex(LMStudioError, "only unload"):
                client.unload_model("externally-loaded")
            client.unload_model("allowed-extractor")
            self.assertNotIn("allowed-extractor", client.managed_instance_ids)

    def test_loaded_instance_alias_inherits_only_its_configured_key_allowlist(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "LMSTUDIO_MODEL": "allowed-extractor",
                "LMSTUDIO_EMBEDDING_MODEL": "allowed-embedder",
                "LMSTUDIO_ALLOWED_MODELS": "",
                "LMSTUDIO_NO_THINK_MODELS": "allowed-extractor",
            },
        ):
            client = FakeNativeClient()
            client.loaded = [{"id": "runtime-instance", "config": {}}]
            self.assertEqual(client.model_id("runtime-instance"), "runtime-instance")
            self.assertTrue(client.model_reference_allowed("runtime-instance"))
            self.assertFalse(client.model_reference_allowed("unrelated-instance"))
            self.assertTrue(
                client.prepare_user_message("runtime-instance", "Extract.").endswith(
                    "/no_think"
                )
            )

    def test_nomic_prefixes_are_added_by_the_client(self) -> None:
        with patch.dict(
            os.environ,
            {"LMSTUDIO_EMBEDDING_MODEL": "prefix-model"},
        ):
            client = PrefixCaptureClient()
            vectors = client.embeddings(
                ["Clause one", "Clause two"],
                model="prefix-model",
                input_type="search_document",
            )
            self.assertEqual(len(vectors), 2)
            self.assertEqual(
                client.payload["input"],
                [
                    "search_document: Clause one",
                    "search_document: Clause two",
                ],
            )
            client.embeddings(
                ["Can Customer transfer?"],
                model="prefix-model",
                input_type="search_query",
            )
            self.assertEqual(
                client.payload["input"],
                ["search_query: Can Customer transfer?"],
            )

    def test_no_think_is_added_only_for_configured_qwen_model(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LMSTUDIO_MODEL": "qwen3.6-27b-mlx",
                "LMSTUDIO_NO_THINK_MODELS": "qwen3.6-27b-mlx",
            },
        ):
            client = PrefixCaptureClient()
            client.chat(
                model="qwen3.6-27b-mlx",
                system="Return JSON.",
                user="Extract this clause.",
            )
            self.assertEqual(
                client.payload["messages"][1]["content"],
                "Extract this clause.\n\n/no_think",
            )
            self.assertEqual(client.payload["reasoning_effort"], "none")

        with patch.dict(
            os.environ,
            {
                "LMSTUDIO_MODEL": "google/gemma-4-26b-a4b-qat",
                "LMSTUDIO_NO_THINK_MODELS": "qwen3.6-27b-mlx",
            },
        ):
            client = PrefixCaptureClient()
            client.chat(
                model="google/gemma-4-26b-a4b-qat",
                system="Return JSON.",
                user="Extract this clause.",
            )
            self.assertEqual(
                client.payload["messages"][1]["content"],
                "Extract this clause.",
            )


class TokenNormalisationTests(unittest.TestCase):
    """Retrieval must match legal inflections without merging opposite parties."""

    def test_inflections_of_the_same_legal_act_share_a_stem(self) -> None:
        for family in (
            ("assign", "assigned", "assignment", "assigns"),
            ("allocate", "allocated", "allocation", "allocating"),
            ("control", "controls", "controlling", "controlled"),
            ("define", "defined", "definition", "definitions"),
            ("transfer", "transferred", "transfers"),
        ):
            with self.subTest(family=family):
                self.assertEqual(len({stem(word) for word in family}), 1)

    def test_opposite_parties_never_collapse(self) -> None:
        # Merging these would make every licensor obligation look like a licensee one.
        for first, second in (
            ("licensee", "licensor"),
            ("assignee", "assignor"),
            ("lessee", "lessor"),
        ):
            with self.subTest(pair=(first, second)):
                self.assertNotEqual(stem(first), stem(second))

    def test_assignment_is_not_treated_as_allocation(self) -> None:
        # Assigning an agreement to another legal entity and allocating a seat to a
        # user are different legal acts; conflating them produced a wrong answer on
        # "can a licence be assigned to another entity".
        assignment = query_terms("can a license be assigned to another entity")
        self.assertIn(stem("assign"), assignment)
        self.assertNotIn(stem("allocation"), assignment)
        allocation = query_terms("how are licences allocated to named users")
        self.assertNotIn(stem("assignment"), allocation)

    def test_licence_does_not_expand_to_software(self) -> None:
        # In a licence corpus "software" matches nearly every clause and drowns the
        # discriminating term.
        self.assertNotIn(stem("software"), query_terms("licence"))


if __name__ == "__main__":
    unittest.main()
