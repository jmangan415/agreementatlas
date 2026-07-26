"""Run the fictional-only Gemma + Nomic acceptance suite.

This is intentionally outside unittest discovery. It requires a running LM
Studio server and writes all generated data to a temporary directory.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import (  # noqa: E402
    answer_question,
    enrich_workspace,
    read_jsonl,
)
from legal_ingest import rebuild_workspace  # noqa: E402
from lmstudio_client import LMStudioClient, LMStudioError  # noqa: E402


def main() -> None:
    client = LMStudioClient()
    status = client.status()
    if not status.get("available"):
        raise LMStudioError(str(status.get("error", "LM Studio is unavailable")))
    if not status.get("extractor"):
        raise LMStudioError("The configured extractor is not genuinely loaded.")
    if not status.get("embedder"):
        raise LMStudioError("The configured embedder is not genuinely loaded.")
    model = client.extractor_model

    with tempfile.TemporaryDirectory(prefix="agreementatlas-live-") as temporary:
        workspace = Path(temporary)
        sources = workspace / "sources"
        sources.mkdir()
        for source in (ROOT / "samples").glob("acme-*.md"):
            shutil.copy2(source, sources / source.name)
        baseline = rebuild_workspace(workspace)
        started = time.monotonic()
        progress: list[tuple[int, int]] = []
        deep = enrich_workspace(
            workspace,
            client,
            model,
            progress=lambda completed, total: progress.append((completed, total)),
        )
        deep_seconds = round(time.monotonic() - started, 3)
        lm_rules = read_jsonl(workspace / "legal" / "lm_rules.jsonl")
        successful_clauses = {item["clause_id"] for item in lm_rules}

        question = "Can Customer allocate StreamFlow access to an Affiliate?"
        answer_started = time.monotonic()
        answer = answer_question(workspace, client, model, question)
        answer_seconds = round(time.monotonic() - answer_started, 3)
        controlling = [
            item
            for item in answer["resolution_trace"]["steps"]
            if item["final_status"] == "CONTROLLING"
        ]
        result = {
            "extractor": status["extractor"],
            "embedder": status["embedder"],
            "baseline": baseline,
            "deep": deep,
            "deep_seconds": deep_seconds,
            "progress_final": progress[-1] if progress else [0, 0],
            "validated_lm_rules": len(lm_rules),
            "successful_clauses": len(successful_clauses),
            "successful_clause_rate": round(
                len(successful_clauses) / max(1, deep["clauses_considered"]),
                4,
            ),
            "query_seconds": answer_seconds,
            "query": {
                "evidence_count": len(answer["evidence"]),
                "top_source": (
                    answer["evidence"][0]["source"] if answer["evidence"] else ""
                ),
                "resolution_status": answer["resolution_trace"]["status"],
                "controlling_sources": [item["source"] for item in controlling],
                "used_vector": answer["retrieval"]["components"]["vector"],
                "answer_has_citation": bool(re.search(r"\[\d+\]", answer["answer"])),
            },
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
