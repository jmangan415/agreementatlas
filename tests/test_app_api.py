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
        cls.original_slots = app.lm_slots
        app.session_store = SessionStore(
            Path(cls.temporary.name) / "sessions", ttl_seconds=3600
        )
        app.library_store = LibraryStore(Path(cls.temporary.name) / "library")
        app.rate_limiter = RateLimiter()
        app.lm_client = FakeLMStudioClient()
        app.lm_slots = threading.BoundedSemaphore(1)
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
        app.lm_slots = cls.original_slots
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
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("content-security-policy", headers)
        self.assertIn("HttpOnly", headers["set-cookie"])
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


if __name__ == "__main__":
    unittest.main()
