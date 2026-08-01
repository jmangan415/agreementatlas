from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

import app
from library_store import LibraryStore
from session_store import RateLimiter, SessionStore

ROOT = Path(__file__).resolve().parents[1]


class FakeLMStudioClient:
    def status(self) -> dict:
        return {
            "available": True,
            "models": [{"id": "local-test-model", "owned_by": "test"}],
        }

    def structured_chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int = 2400,
    ) -> dict:
        clause = re.search(r"\[CLAUSE_ID\] ([^\n]+)", user)
        text = re.search(r"\[TEXT\] (.*?)(?:\n\n\[CLAUSE_ID\]|\Z)", user, re.S)
        assert clause and text
        evidence = text.group(1).strip()
        return {
            "rules": [
                {
                    "clause_id": clause.group(1),
                    "rule_type": "OBLIGATION",
                    "actor": "Customer",
                    "action": "protect credentials",
                    "object": "credentials",
                    "scope": "Security",
                    "conditions": [],
                    "summary": "Customer must protect account credentials.",
                    "evidence": evidence,
                }
            ]
        }

    def chat(self, **_: object) -> str:
        return "Customer must protect account credentials [1]."


def multipart(files: list[tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = "----agreementatlas-test-boundary"
    chunks: list[bytes] = []
    for name, content in files:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    'Content-Disposition: form-data; name="files"; '
                    f'filename="{name}"\r\n'
                ).encode(),
                b"Content-Type: text/markdown\r\n\r\n",
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class AgreementAtlasAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.original_store = app.session_store
        cls.original_library = app.library_store
        cls.original_limiter = app.rate_limiter
        cls.original_client = app.lm_client
        cls.original_enrich_slots = app.enrich_slots
        cls.original_query_slots = app.query_slots
        app.session_store = SessionStore(
            Path(cls.temporary.name) / "sessions", ttl_seconds=3600
        )
        app.library_store = LibraryStore(Path(cls.temporary.name) / "library")
        app.rate_limiter = RateLimiter()
        app.lm_client = FakeLMStudioClient()
        app.enrich_slots = threading.BoundedSemaphore(1)
        app.query_slots = threading.BoundedSemaphore(1)
        app.lm_status_cache = (0.0, {})
        app.jobs.clear()
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        app.session_store = cls.original_store
        app.library_store = cls.original_library
        app.rate_limiter = cls.original_limiter
        app.lm_client = cls.original_client
        app.enrich_slots = cls.original_enrich_slots
        app.query_slots = cls.original_query_slots
        app.jobs.clear()
        cls.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        cookie: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        request_headers = dict(headers or {})
        if cookie:
            request_headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, payload

    @staticmethod
    def cookie(headers: dict[str, str]) -> str:
        return headers["set-cookie"].split(";", 1)[0]

    def new_session(self) -> str:
        """Create a family and return the query fragment addressing it."""

        status, _, payload = self.request(
            "POST",
            "/api/families",
            body=b'{"name": "Test family"}',
            headers={
                "Content-Type": "application/json",
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201)
        return f"?family={json.loads(payload)['id']}"

    def test_security_headers_and_csrf_guard(self) -> None:
        status, headers, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("content-security-policy", headers)
        self.assertIn("HttpOnly", headers["set-cookie"])
        self.assertIn(b"A software licence is rarely one document", payload)

        status, _, payload = self.request("GET", "/workbench/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="familyList"', payload)
        self.assertIn(b'id="familyWorkspace" class="family-workspace" hidden', payload)
        self.assertIn(b'id="graphGate"', payload)
        self.assertIn(b'id="chatGate"', payload)
        self.assertIn(b"STEP 1 \xc2\xb7 LIBRARY", payload)
        self.assertIn(b'class="graph-search" hidden', payload)
        self.assertIn(b'id="enrichmentChoice"', payload)
        self.assertIn(b"Start local AI enrichment", payload)

        status, _, payload = self.request(
            "POST",
            "/api/query",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["code"], "request_origin")

    def test_uploads_are_isolated_graph_is_private_and_delete_is_immediate(
        self,
    ) -> None:
        first_family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "portfolio-agreement.md",
                    (
                        b"# Portfolio Cloud Agreement\n\n1. Security\n\n"
                        b"Customer must protect account credentials and must notify "
                        b"Provider of suspected unauthorised access."
                    ),
                )
            ]
        )
        status, _, payload = self.request(
            "POST",
            "/api/upload" + first_family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201, payload)

        status, _, payload = self.request("GET", "/api/status" + first_family)
        first_status = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(first_status["documents"]), 1)
        self.assertTrue(first_status["graph_ready"])
        self.assertEqual(first_status["build"]["schema_version"], "3.0")
        self.assertEqual(first_status["build"]["mode"], "baseline")
        self.assertIn("extractor", first_status["lmstudio"])
        self.assertIn("embedder", first_status["lmstudio"])

        second_family = self.new_session()
        status, _, payload = self.request("GET", "/api/status" + second_family)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["documents"], [])

        status, _, payload = self.request(
            "GET", "/api/graph?view=overview&family=" + first_family.split("=")[1]
        )
        self.assertEqual(status, 200)
        graph = json.loads(payload)
        self.assertTrue(graph["nodes"])
        self.assertNotIn(self.temporary.name, payload.decode())

        query = json.dumps(
            {
                "question": "What must Customer do with account credentials?",
                "model": "local-test-model",
            }
        ).encode()
        status, _, payload = self.request(
            "POST",
            "/api/query" + first_family,
            body=query,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(query)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 200, payload)
        answer = json.loads(payload)
        self.assertTrue(answer["evidence"])
        self.assertEqual(answer["retrieval"]["engine"], "agreementatlas-graph")
        self.assertTrue(answer["retrieval"]["graph_augmented"])
        self.assertEqual(answer["graph_build_mode"], "baseline")
        self.assertIn("resolution_trace", answer)
        self.assertIn("components", answer["retrieval"])
        self.assertIn("not legal advice", answer["disclaimer"])

        status, _, payload = self.request(
            "DELETE",
            "/api/families" + first_family,
            headers={"X-AgreementAtlas-Request": "1"},
        )
        self.assertEqual(status, 200, payload)

        # Deleting one family removes it entirely and leaves the other alone.
        status, _, payload = self.request("GET", "/api/status" + first_family)
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertIsNone(body["family"])
        self.assertEqual(body["documents"], [])
        remaining = {item["id"] for item in body["families"]}
        self.assertNotIn(first_family.split("=")[1], remaining)
        self.assertIn(second_family.split("=")[1], remaining)

    def test_background_enrichment_reports_completion(self) -> None:
        family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "enrichment-agreement.md",
                    b"# Enrichment Agreement\n\n1. Security\n\nCustomer must protect credentials.",
                )
            ]
        )
        status, _, _ = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201)
        request = json.dumps({"model": "local-test-model"}).encode()
        status, _, payload = self.request(
            "POST",
            "/api/enrich" + family,
            body=request,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(request)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 202, payload)
        result = {}
        for _ in range(30):
            status, _, payload = self.request("GET", "/api/enrich/status" + family)
            result = json.loads(payload)
            if result["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(result["state"], "complete", result)
        self.assertGreaterEqual(result["summary"]["rules"], 1)
        self.assertEqual(result["summary"]["schema_version"], "3.0")
        self.assertEqual(result["summary"]["build_mode"], "deep")

    def test_enrichment_survives_a_family_growing_by_one_document(self) -> None:
        """Adding a document must not discard rules already extracted.

        Re-ingest rebuilds the deterministic layer from scratch. Before the
        library, that rebuild also replaced `legal/` wholesale, so a family that
        had cost hours of strict extraction lost all of it the moment a two-page
        addendum was added to it.
        """

        family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "base-agreement.md",
                    b"# Base Agreement\n\n1. Security\n\n"
                    b"Customer must protect credentials.",
                )
            ]
        )
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "X-AgreementAtlas-Request": "1",
        }
        status, _, _ = self.request(
            "POST", "/api/upload" + family, body=body, headers=headers
        )
        self.assertEqual(status, 201)

        request = json.dumps({"model": "local-test-model"}).encode()
        status, _, payload = self.request(
            "POST",
            "/api/enrich" + family,
            body=request,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(request)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 202, payload)
        for _ in range(30):
            status, _, payload = self.request("GET", "/api/enrich/status" + family)
            if json.loads(payload)["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(json.loads(payload)["state"], "complete", payload)

        root = app.library_store.get(family.split("=")[1]).root
        rules_path = root / "legal" / "lm_rules.jsonl"
        before = rules_path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(before)

        content_type, body = multipart(
            [
                (
                    "addendum.md",
                    b"# Addendum\n\n1. Support\n\n"
                    b"Provider must respond within two business days.",
                )
            ]
        )
        status, _, payload = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201, payload)

        after = rules_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            before, after, "extracted rules were discarded when the family grew"
        )
        status, _, payload = self.request("GET", "/api/status" + family)
        self.assertEqual(len(json.loads(payload)["documents"]), 2)

    def test_public_upload_is_blocked_only_for_samples(self) -> None:
        # Local mode may grow a sample copy; the read-only guard is public-only.
        family = self.new_session()
        root = app.library_store.get(family.split("=")[1]).root
        (root / app.SAMPLE_MARKER).write_text("Sample", encoding="utf-8")
        content_type, body = multipart([("extra.md", b"# Extra\n\n1. A\n\nB.")])
        status, _, payload = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201, payload)

    def test_clearing_the_conversation_forgets_the_history(self) -> None:
        family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "chat-agreement.md",
                    b"# Chat Agreement\n\n1. Security\n\n"
                    b"Customer must protect credentials.",
                )
            ]
        )
        status, _, _ = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201)
        query = json.dumps(
            {"question": "What must Customer protect?", "model": "local-test-model"}
        ).encode()
        status, _, _ = self.request(
            "POST",
            "/api/query" + family,
            body=query,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(query)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 200)
        root = app.library_store.get(family.split("=")[1]).root
        self.assertTrue((root / ".conversation.jsonl").is_file())
        status, _, payload = self.request(
            "DELETE",
            "/api/conversation" + family,
            headers={"X-AgreementAtlas-Request": "1"},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["cleared"])
        self.assertFalse((root / ".conversation.jsonl").exists())

    def test_query_stays_available_while_enrichment_holds_its_slot(self) -> None:
        family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "busy-agreement.md",
                    b"# Busy Agreement\n\n1. Security\n\n"
                    b"Customer must protect credentials.",
                )
            ]
        )
        status, _, _ = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201)
        # An enrichment holds its slot for the whole run; the chat must not care.
        self.assertTrue(app.enrich_slots.acquire(blocking=False))
        try:
            query = json.dumps(
                {"question": "What must Customer do?", "model": "local-test-model"}
            ).encode()
            status, _, payload = self.request(
                "POST",
                "/api/query" + family,
                body=query,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(query)),
                    "X-AgreementAtlas-Request": "1",
                },
            )
            self.assertEqual(status, 200, payload)
            request = json.dumps({"model": "local-test-model"}).encode()
            status, _, payload = self.request(
                "POST",
                "/api/enrich" + family,
                body=request,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(request)),
                    "X-AgreementAtlas-Request": "1",
                },
            )
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(payload)["code"], "model_busy")
        finally:
            app.enrich_slots.release()

    def test_removing_a_document_removes_what_was_extracted_from_it(self) -> None:
        # The opposite guarantee: the graph must never cite a document that is
        # no longer in the family.
        family = self.new_session()
        content_type, body = multipart(
            [
                (
                    "solo-agreement.md",
                    b"# Solo Agreement\n\n1. Security\n\n"
                    b"Customer must protect credentials.",
                )
            ]
        )
        status, _, _ = self.request(
            "POST",
            "/api/upload" + family,
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
        )
        self.assertEqual(status, 201)
        root = app.library_store.get(family.split("=")[1]).root
        rules_path = root / "legal" / "lm_rules.jsonl"
        rules_path.write_text(
            json.dumps({"clause_id": "clause:gone", "summary": "orphan"}) + "\n",
            encoding="utf-8",
        )
        from legal_graph_service import prune_stale_enrichment

        prune_stale_enrichment(root)
        self.assertEqual(rules_path.read_text(encoding="utf-8").strip(), "")


class PublicDemoFamilyTests(unittest.TestCase):
    """The public demo's session-scoped family library.

    Every visitor session holds its own library: the shipped samples arrive
    pre-installed and read-only, the visitor's own families are theirs alone,
    and deleting the session removes all of it.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name)
        for slug, name in (("alpha", "Alpha sample"), ("beta", "Beta sample")):
            bundle = base / "bundles" / slug
            (bundle / "legal").mkdir(parents=True)
            (bundle / "sources").mkdir()
            (bundle / "output").mkdir()
            (bundle / "sources" / f"{slug}.md").write_text(
                "# Sample\n\n1. Scope\n\nProvider may host data.", encoding="utf-8"
            )
            (bundle / "legal" / "documents.jsonl").write_text(
                json.dumps({"id": f"doc:{slug}", "source": f"{slug}.md", "title": name})
                + "\n",
                encoding="utf-8",
            )
            (bundle / "legal" / "lm_rules.jsonl").write_text("", encoding="utf-8")
            (bundle / "output" / "legal_relationship_graph.json").write_text(
                '{"nodes": [], "relationships": []}', encoding="utf-8"
            )
            (bundle / "demo.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "enriched": True,
                        "questions": ["What may Provider do?"],
                        "source_url": "https://example.com/agreements",
                    }
                ),
                encoding="utf-8",
            )
        cls.originals = {
            "session_store": app.session_store,
            "rate_limiter": app.rate_limiter,
            "lm_client": app.lm_client,
            "enrich_slots": app.enrich_slots,
            "query_slots": app.query_slots,
            "persistent": app.PERSISTENT,
            "demo_root": app.DEMO_ROOT,
            "demo_order": app.DEMO_ORDER,
        }
        app.session_store = SessionStore(base / "sessions", ttl_seconds=3600)
        app.rate_limiter = RateLimiter()
        app.lm_client = FakeLMStudioClient()
        app.enrich_slots = threading.BoundedSemaphore(1)
        app.query_slots = threading.BoundedSemaphore(1)
        app.lm_status_cache = (0.0, {})
        app.jobs.clear()
        app.session_libraries.clear()
        app.PERSISTENT = False
        app.DEMO_ROOT = base / "bundles"
        app.DEMO_ORDER = ("alpha", "beta")
        cls.server = app.create_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        app.session_store = cls.originals["session_store"]
        app.rate_limiter = cls.originals["rate_limiter"]
        app.lm_client = cls.originals["lm_client"]
        app.enrich_slots = cls.originals["enrich_slots"]
        app.query_slots = cls.originals["query_slots"]
        app.PERSISTENT = cls.originals["persistent"]
        app.DEMO_ROOT = cls.originals["demo_root"]
        app.DEMO_ORDER = cls.originals["demo_order"]
        app.jobs.clear()
        app.session_libraries.clear()
        cls.temporary.cleanup()

    request = AgreementAtlasAPITests.request
    cookie = staticmethod(AgreementAtlasAPITests.cookie)

    def json_post(
        self, path: str, payload: dict, cookie: str
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(payload).encode()
        return self.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
            cookie=cookie,
        )

    def open_session(self) -> tuple[str, dict]:
        status, headers, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        return self.cookie(headers), json.loads(payload)

    def test_samples_arrive_preinstalled_and_read_only(self) -> None:
        cookie, first = self.open_session()
        self.assertFalse(first["persistent"])
        self.assertIsNone(first["family"])
        self.assertGreater(first["session"]["expires_at"], 0)
        names = {item["name"] for item in first["families"]}
        self.assertEqual(names, {"Alpha sample", "Beta sample"})
        self.assertTrue(all(item["is_sample"] for item in first["families"]))
        self.assertTrue(all(item["enriched"] for item in first["families"]))
        self.assertEqual(first["sample_family"], "Alpha sample")

        sample_id = first["families"][0]["id"]
        status, _, payload = self.request(
            "GET", f"/api/status?family={sample_id}", cookie=cookie
        )
        selected = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue(selected["family"]["is_sample"])
        self.assertEqual(len(selected["documents"]), 1)

        content_type, body = multipart([("own.md", b"# Own\n\n1. A\n\nB.")])
        status, _, payload = self.request(
            "POST",
            f"/api/upload?family={sample_id}",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
            cookie=cookie,
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["code"], "sample_family")

    def test_own_families_are_session_scoped_capped_and_enrichable(self) -> None:
        cookie, _ = self.open_session()
        status, _, payload = self.json_post(
            "/api/families", {"name": "My uploads"}, cookie
        )
        self.assertEqual(status, 201, payload)
        own = json.loads(payload)
        self.assertFalse(own["is_sample"])

        content_type, body = multipart(
            [
                (
                    "own-agreement.md",
                    b"# Own Agreement\n\n1. Security\n\n"
                    b"Customer must protect credentials.",
                )
            ]
        )
        status, _, payload = self.request(
            "POST",
            f"/api/upload?family={own['id']}",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
            cookie=cookie,
        )
        self.assertEqual(status, 201, payload)

        status, _, payload = self.json_post(
            f"/api/enrich?family={own['id']}", {"model": "local-test-model"}, cookie
        )
        self.assertEqual(status, 202, payload)
        result = {}
        for _ in range(30):
            status, _, payload = self.request(
                "GET", f"/api/enrich/status?family={own['id']}", cookie=cookie
            )
            result = json.loads(payload)
            if result["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(result["state"], "complete", result)
        status, _, payload = self.request(
            "GET", f"/api/status?family={own['id']}", cookie=cookie
        )
        self.assertTrue(json.loads(payload)["family"]["enriched"])

        # The cap counts the visitor's own families, never the samples.
        for index in range(2):
            status, _, payload = self.json_post(
                "/api/families", {"name": f"Extra {index}"}, cookie
            )
            self.assertEqual(status, 201, payload)
        status, _, payload = self.json_post("/api/families", {"name": "Over"}, cookie)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(payload)["code"], "family_limit")

        # A different visitor sees fresh samples and none of these families.
        other_cookie, other = self.open_session()
        self.assertNotEqual(other_cookie, cookie)
        self.assertEqual(
            {item["name"] for item in other["families"]},
            {"Alpha sample", "Beta sample"},
        )

    def test_deleting_the_session_removes_every_family(self) -> None:
        cookie, first = self.open_session()
        status, _, payload = self.json_post(
            "/api/families", {"name": "Short lived"}, cookie
        )
        self.assertEqual(status, 201)
        status, headers, payload = self.request(
            "DELETE",
            "/api/session",
            headers={"X-AgreementAtlas-Request": "1"},
            cookie=cookie,
        )
        self.assertEqual(status, 200, payload)
        self.assertIn("Max-Age=0", headers["set-cookie"])
        session_id = cookie.split("=", 1)[1]
        self.assertFalse((Path(app.session_store.root) / session_id).exists())

    def test_expiry_cleanup_removes_uploaded_and_generated_data(self) -> None:
        cookie, _ = self.open_session()
        status, _, payload = self.json_post(
            "/api/families", {"name": "Expiring upload"}, cookie
        )
        self.assertEqual(status, 201, payload)
        family = json.loads(payload)
        content_type, body = multipart(
            [("private.md", b"# Private\n\n1. Security\n\nKeep this confidential.")]
        )
        status, _, payload = self.request(
            "POST",
            f"/api/upload?family={family['id']}",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-AgreementAtlas-Request": "1",
            },
            cookie=cookie,
        )
        self.assertEqual(status, 201, payload)

        session_id = cookie.split("=", 1)[1]
        session_root = Path(app.session_store.root) / session_id
        family_root = app.session_libraries[session_id].get(family["id"]).root
        uploaded = family_root / "sources" / "private.md"
        generated = family_root / "output" / "legal_relationship_graph.json"
        self.assertTrue(uploaded.exists())
        self.assertTrue(generated.exists())

        metadata_path = session_root / ".session.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["expires_at"] = 0
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        expired = app.cleanup_expired_sessions(force=True)

        self.assertIn(session_id, expired)
        self.assertFalse(session_root.exists())
        self.assertFalse(uploaded.exists())
        self.assertFalse(generated.exists())
        self.assertNotIn(session_id, app.session_libraries)


if __name__ == "__main__":
    unittest.main()
