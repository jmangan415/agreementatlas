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
    DEFINITION_CLOSURE_LIMIT,
    PERVASIVE_TERM_SHARE,
    LMStudioError,
    answer_question,
    enrich_workspace,
    expand_followup,
    foreign_question_terms,
    leans_on_context,
    query_terms,
    read_jsonl,
    relationship_records,
    resolve_returned_clause_id,
    retrieve_evidence,
    search_records,
    stem,
    substantive_clauses,
    validate_extracted_rule,
)
from legal_ingest import modality_and_polarity, rebuild_workspace
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


class QuestionCaptureRetriever(FixedRetriever):
    def retrieve(self, root: Path, question: str) -> list[dict]:
        self.question = question
        return super().retrieve(root, question)


class ExpandingChatClient:
    """Answers as usual, and rewrites follow-ups when asked as the expander asks."""

    def chat(self, *, system: str = "", **_kwargs: object) -> str:
        if system.startswith("Rewrite the reader's follow-up"):
            return "Can Customer allocate StreamFlow access to subsidiaries?"
        return "Customer may allocate access only under the stated conditions [1]."


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
        # An unusable actor now costs the actor, not the rule. Eleven of twelve
        # rules from real clauses were being discarded on this field alone,
        # taking their effect, modality, polarity and evidence with them.
        unknown_actor = validate_extracted_rule(
            {**base, "actor": "Software"},
            item_clause,
            clause_lookup=clause_lookup,
            span_lookup=span_lookup,
            actors={"customer"},
        )
        self.assertIsNotNone(unknown_actor)
        self.assertEqual(unknown_actor["actor"], "")
        self.assertEqual(unknown_actor["effect"], "PROHIBITION")

        # "parties" and "party" are the same party.
        plural = validate_extracted_rule(
            {**base, "actor": "Customers"},
            item_clause,
            clause_lookup=clause_lookup,
            span_lookup=span_lookup,
            actors={"customer"},
        )
        self.assertEqual(plural["actor"], "Customers")

        # NOT_STATED is a real answer, not a rejection.
        unnamed = validate_extracted_rule(
            {**base, "actor": "NOT_STATED"},
            item_clause,
            clause_lookup=clause_lookup,
            span_lookup=span_lookup,
            actors={"customer"},
        )
        self.assertEqual(unnamed["actor"], "")
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

    def test_evidence_carries_the_definitions_its_passages_rely_on(self) -> None:
        evidence = retrieve_evidence(
            self.workspace, "Can Customer share account credentials?", 8
        )
        closure = [
            item
            for item in evidence
            if item.get("retrieval_components", {}).get("definition_closure")
        ]
        self.assertTrue(closure, "no definition was closed over")
        chosen = {item["id"] for item in evidence}
        uses_term = {
            (str(edge.get("source", "")), str(edge.get("target", "")))
            for edge in relationship_records(self.workspace)
            if str(edge.get("type", "")) == "USES_TERM"
        }
        for item in closure:
            self.assertEqual(item["kind"], "Definition")
            self.assertTrue(
                any(
                    (source, item["id"]) in uses_term
                    for source in chosen
                    if source != item["id"]
                ),
                f"{item['citation']} is not relied on by any chosen passage",
            )

    def test_closure_accompanies_the_budget_instead_of_spending_it(self) -> None:
        """A definition must not cost the ranking a slot it earned.

        Reserving a slot inside the budget was measured taking it off the
        bottom of the ranking, which is where a clause that only just made the
        cut sits -- one question gained a definition and another lost the
        provision that decided it.
        """

        question = "Can Customer allocate StreamFlow access to an Affiliate?"
        with patch("legal_graph_service.DEFINITION_CLOSURE_LIMIT", 0):
            without = [
                item["id"] for item in retrieve_evidence(self.workspace, question, 8)
            ]
        evidence = retrieve_evidence(self.workspace, question, 8)
        ranked = [
            item["id"]
            for item in evidence
            if not item.get("retrieval_components", {}).get("definition_closure")
        ]
        self.assertEqual(len(without), 8)
        self.assertGreater(len(evidence), len(ranked))
        self.assertEqual(sorted(ranked), sorted(without))

    def test_closure_is_bounded_and_skipped_on_a_small_budget(self) -> None:
        for question in (
            "Can Customer share account credentials?",
            "Can Customer allocate StreamFlow access to an Affiliate?",
        ):
            evidence = retrieve_evidence(self.workspace, question, 8)
            self.assertLessEqual(len(evidence), 8 + DEFINITION_CLOSURE_LIMIT)
            small = retrieve_evidence(self.workspace, question, 4)
            self.assertEqual(len(small), 4)
            self.assertFalse(
                [
                    item
                    for item in small
                    if item.get("retrieval_components", {}).get("definition_closure")
                ]
            )

    def test_closure_does_not_spend_itself_on_a_pervasive_term(self) -> None:
        records = search_records(self.workspace)
        haystack = [str(item.get("_search_text", "")).casefold() for item in records]
        for question in (
            "Can Customer share account credentials?",
            "Can Customer allocate StreamFlow access to an Affiliate?",
        ):
            for item in retrieve_evidence(self.workspace, question, 8):
                if not item.get("retrieval_components", {}).get("definition_closure"):
                    continue
                term = str(item.get("term", "")).casefold()
                self.assertTrue(term)
                share = sum(1 for text in haystack if term in text) / len(haystack)
                self.assertLessEqual(share, PERVASIVE_TERM_SHARE)

    def test_answer_adds_a_source_marker_when_model_omits_citations(self) -> None:
        answer = answer_question(
            self.workspace,
            NoCitationChatClient(),  # type: ignore[arg-type]
            "fake-model",
            "Can Customer allocate StreamFlow access to an Affiliate?",
            retriever=FixedRetriever(),  # type: ignore[arg-type]
        )
        self.assertIn("[1]", answer["answer"])

    def test_a_followup_is_expanded_before_anything_reads_it(self) -> None:
        # Retrieval, offering matching and the resolution trace all read the
        # question text; the standalone rewrite must be what they read, and the
        # reader must be told how the follow-up was understood.
        retriever = QuestionCaptureRetriever()
        result = answer_question(
            self.workspace,
            ExpandingChatClient(),  # type: ignore[arg-type]
            "fake-model",
            "what about for subsidiaries?",
            retriever=retriever,  # type: ignore[arg-type]
            history=[
                {
                    "question": "Can Customer allocate StreamFlow access to an Affiliate?",
                    "answer": "Yes, Customer may allocate StreamFlow access to an "
                    "Affiliate under the stated conditions.",
                    "offered": [],
                }
            ],
        )
        self.assertEqual(
            result.get("understood_as"),
            "Can Customer allocate StreamFlow access to subsidiaries?",
        )
        self.assertEqual(retriever.question, result["understood_as"])


class ModalityEffectTests(unittest.TestCase):
    """A right expressed with "can" is a right, and must not vanish.

    CAN was recognised as a modality and had no effect branch, so it fell
    through to no effect at all -- and a proposition with no effect is
    discarded, not stored badly. 131 of 440 propositions containing a positive
    "can" produced no rule, including a customer's right to request an audit.
    """

    def test_positive_can_grants_a_permission(self) -> None:
        effect, modality, polarity = modality_and_polarity(
            "Customer can request an on-site audit of the Processing activities."
        )
        self.assertEqual(
            (effect, modality, polarity), ("PERMISSION", "CAN", "POSITIVE")
        )

    def test_cannot_is_still_a_prohibition(self) -> None:
        effect, _, polarity = modality_and_polarity(
            "Licensee cannot share administrator credentials."
        )
        self.assertEqual((effect, polarity), ("PROHIBITION", "NEGATIVE"))

    def test_a_can_clause_actually_produces_a_rule(self) -> None:
        # The regression that matters is absence, not a wrong label, so assert
        # against the rule list rather than against the classifier alone.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        workspace = Path(temporary.name)
        sources = workspace / "sources"
        sources.mkdir()
        (sources / "audit.md").write_text(
            "1. Audit\n\nCustomer can request an on-site audit of the "
            "Processing activities covered by this Agreement.\n",
            encoding="utf-8",
        )
        rebuild_workspace(workspace)
        rules = read_jsonl(workspace / "legal" / "operative_rules.jsonl")
        self.assertTrue(
            [r for r in rules if r.get("modality") == "CAN"],
            "a positive 'can' clause produced no rule at all",
        )


class EffectPolarityConsistencyTests(unittest.TestCase):
    """A prohibition is negative. The model wrote the two fields separately."""

    def _clause_and_lookup(self):
        clause = {
            "id": "clause:1",
            "family_id": "f",
            "document_id": "d",
            "source": "s.pdf",
            "section_id": "1",
            "section_path": "1",
            "text": "SAP warrants to maintain an average monthly system availability.",
            "scope": {},
        }
        return clause, {"clause:1": clause}

    def _item(self, effect: str, polarity: str) -> dict:
        return {
            "clause_id": "clause:1",
            "effect": effect,
            "modality": "OTHER",
            "polarity": polarity,
            "actor": "SAP",
            "action": "maintain",
            "object": "system availability",
            "evidence_spans": [
                "SAP warrants to maintain an average monthly system availability."
            ],
            "summary": "SAP maintains availability",
        }

    def test_a_positive_prohibition_is_rejected(self) -> None:
        clause, lookup = self._clause_and_lookup()
        self.assertIsNone(
            validate_extracted_rule(
                self._item("PROHIBITION", "POSITIVE"),
                clause,
                clause_lookup=lookup,
                span_lookup={},
                actors=["SAP"],
            )
        )

    def test_a_negative_obligation_is_rejected(self) -> None:
        clause, lookup = self._clause_and_lookup()
        self.assertIsNone(
            validate_extracted_rule(
                self._item("OBLIGATION", "NEGATIVE"),
                clause,
                clause_lookup=lookup,
                span_lookup={},
                actors=["SAP"],
            )
        )

    def test_a_consistent_rule_survives(self) -> None:
        clause, lookup = self._clause_and_lookup()
        self.assertIsNotNone(
            validate_extracted_rule(
                self._item("OBLIGATION", "POSITIVE"),
                clause,
                clause_lookup=lookup,
                span_lookup={},
                actors=["SAP"],
            )
        )


class ChapeauEligibilityTests(unittest.TestCase):
    """A list item must reach the model even though its modal is elsewhere.

    "only be used to support Licensee's use of the Software" carries no modal of
    its own -- the "may" belongs to the chapeau above it -- so a filter that
    tests the clause's own text excluded it, and the model was never asked about
    it at all. The interface then presented the deterministic guess as analysis.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        sources = self.workspace / "sources"
        sources.mkdir()
        # The list splitter needs parenthesised labels and a prefix ending in a
        # colon, which is how a numbered agreement actually prints a list.
        (sources / "chapeau.md").write_text(
            "1. Copies and Documentation\n\n"
            "Licensee may make copies of the Software as licensed. "
            "The Documentation may: (a) only be used to support internal "
            "business operations; (b) not be published to any third party.\n",
            encoding="utf-8",
        )
        rebuild_workspace(self.workspace)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_list_item_without_its_own_modal_is_still_offered_to_the_model(
        self,
    ) -> None:
        clauses = read_jsonl(self.workspace / "legal" / "clauses.jsonl")
        children = [item for item in clauses if item.get("chapeau_clause_id")]
        self.assertTrue(children, "fixture produced no chapeau children")
        eligible = {
            str(item.get("id", "")) for item in substantive_clauses(self.workspace)
        }
        missing = [
            str(item.get("text", ""))
            for item in children
            if str(item.get("id", "")) not in eligible
        ]
        self.assertEqual(
            missing,
            [],
            "chapeau children excluded from enrichment: " + "; ".join(missing),
        )

    def test_a_chapeau_itself_is_still_excluded(self) -> None:
        # The chapeau is extracted through its children; sending it separately
        # would duplicate every rule the list already produces.
        eligible = substantive_clauses(self.workspace)
        self.assertFalse(
            [item for item in eligible if item.get("clause_kind") == "CHAPEAU"]
        )


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


class RecordingExpanderClient:
    """A chat fake that returns a scripted rewrite and counts its calls."""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.calls = 0

    def chat(self, **kwargs: object) -> str:
        self.calls += 1
        self.kwargs = kwargs
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


REASSIGN_HISTORY = [
    {
        "question": "Can I reassign named user licenses when someone leaves the company",
        "answer": (
            "Yes, subject to conditions. Standard Named User licences may be "
            "re-allocated to another individual provided the original user's "
            "account is first deleted from the system."
        ),
        "offered": [],
    }
]


class FollowupExpansionTests(unittest.TestCase):
    """A follow-up is expanded only from words the conversation already used.

    The live failure: "what about for standard named users?" retrieved almost
    nothing, because retrieval, offering matching and the resolution trace all
    read the question text and the question's meaning lived in the previous
    turn. The rewrite is allowed to recombine the conversation's words and
    nothing else -- an open paraphrase smooths "reassign" toward "assign",
    which are different mechanisms in these agreements.
    """

    def test_standalone_question_never_calls_the_model(self) -> None:
        client = RecordingExpanderClient("unused")
        result = expand_followup(
            client,
            "fake",
            "How long must Licensee keep records sufficient for an audit?",
            REASSIGN_HISTORY,
        )
        self.assertEqual(result, "")
        self.assertEqual(client.calls, 0)

    def test_first_question_never_calls_the_model(self) -> None:
        client = RecordingExpanderClient("unused")
        self.assertEqual(expand_followup(client, "fake", "why?", []), "")
        self.assertEqual(client.calls, 0)

    def test_conversation_vocabulary_recombines_into_a_standalone_question(
        self,
    ) -> None:
        client = RecordingExpanderClient(
            "Can Standard Named User licences be re-allocated when someone "
            "leaves the company?"
        )
        result = expand_followup(
            client, "fake", "what about for standard named users?", REASSIGN_HISTORY
        )
        self.assertIn("Standard Named User", result)
        self.assertEqual(client.calls, 1)

    def test_substituting_a_users_term_is_rejected(self) -> None:
        # "reassign" rewritten as "assign" is the exact conflation the synonym
        # groups were deliberately never bridged across.
        client = RecordingExpanderClient(
            "Can Standard Named User licences be assigned when someone leaves "
            "the company?"
        )
        result = expand_followup(
            client,
            "fake",
            "what about reassigning standard named users?",
            REASSIGN_HISTORY,
        )
        self.assertEqual(result, "")

    def test_vocabulary_from_outside_the_conversation_is_rejected(self) -> None:
        client = RecordingExpanderClient(
            "Can Standard Named User licences be sublicensed to contractors "
            "when someone leaves the company?"
        )
        result = expand_followup(
            client, "fake", "what about for standard named users?", REASSIGN_HISTORY
        )
        self.assertEqual(result, "")

    def test_an_unchanged_rewrite_reports_no_expansion(self) -> None:
        client = RecordingExpanderClient("What about for standard named users?")
        result = expand_followup(
            client, "fake", "what about for standard named users?", REASSIGN_HISTORY
        )
        self.assertEqual(result, "")

    def test_a_failed_model_call_never_costs_the_answer(self) -> None:
        client = RecordingExpanderClient(LMStudioError("model offline"))
        result = expand_followup(
            client, "fake", "what about for standard named users?", REASSIGN_HISTORY
        )
        self.assertEqual(result, "")

    def test_foreign_terms_flag_only_true_vocabulary_gaps(self) -> None:
        records = [
            {
                "_search_text": (
                    "The Licensee may re-allocate Standard Named User licences "
                    "after a notice period of 120 days, prior to deletion of "
                    "the account."
                )
            },
            {"_search_text": "This version of the Schedule states the material terms."},
        ]
        # The word the family never uses, in any spelling or curated synonym.
        self.assertEqual(
            foreign_question_terms(
                records, "if I disable users do I still need a license for them?"
            ),
            ["disable"],
        )
        # "reassign" travels its synonym group to "re-allocate"; "before"
        # travels to "prior"; "escrow" has no route and is flagged.
        self.assertEqual(
            foreign_question_terms(
                records, "can we reassign the licences before the escrow?"
            ),
            ["escrow"],
        )
        # Everyday words, possessives and digit-bearing data never flag.
        self.assertEqual(
            foreign_question_terms(
                records,
                "what happens if someone misses the version's 120-day notice period?",
            ),
            [],
        )

    def test_leans_on_context_reads_the_signals(self) -> None:
        for question in (
            "why?",
            "and affiliates?",
            "what about for standard named users?",
            "does that apply to contractors?",
        ):
            with self.subTest(question=question):
                self.assertTrue(leans_on_context(question))
        self.assertFalse(
            leans_on_context(
                "Can I reassign named user licenses when someone leaves the company"
            )
        )


if __name__ == "__main__":
    unittest.main()


class RestoredRuleLocationTests(unittest.TestCase):
    """A carried rule's location comes from today's clause, not its birth parse."""

    def test_restore_refreshes_stale_section_labels_from_the_clause(self) -> None:
        from legal_graph_service import restore_effective_graph

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            sources = workspace / "sources"
            sources.mkdir()
            shutil.copy2(
                SAMPLES / "acme-cloud-master-agreement.md",
                sources / "acme-cloud-master-agreement.md",
            )
            rebuild_workspace(workspace)
            clause = next(
                item
                for item in read_jsonl(workspace / "legal" / "clauses.jsonl")
                if item["clause_kind"] == "CLAUSE" and item.get("section_id")
            )
            stale = {
                "id": "rule:stale-location",
                "family_id": clause["family_id"],
                "document_id": clause["document_id"],
                "clause_id": clause["id"],
                "source": clause["source"],
                # The labels of a parse that no longer exists.
                "section_id": "29 (2)",
                "section_path": "29 D. Something The Document Never Numbered",
                "effect": "OBLIGATION",
                "modality": "SHALL",
                "polarity": "POSITIVE",
                "actor": "Customer",
                "action": "keep records",
                "object": "records",
                "scope": clause["scope"],
                "conditions": [],
                "carve_outs": [],
                "cross_refs": [],
                "summary": "Customer must keep records.",
                "evidence": clause["text"],
                "evidence_span_ids": clause["evidence_span_ids"],
                "extraction_method": "lmstudio",
            }
            with (workspace / "legal" / "lm_rules.jsonl").open("w") as handle:
                handle.write(json.dumps(stale) + "\n")
            self.assertTrue(restore_effective_graph(workspace))
            restored = next(
                item
                for item in read_jsonl(workspace / "legal" / "resolved_rules.jsonl")
                if item["id"] == "rule:stale-location"
            )
            self.assertEqual(restored["section_id"], clause["section_id"])
            self.assertEqual(restored["section_path"], clause["section_path"])
