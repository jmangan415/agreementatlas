#!/usr/bin/env python3
"""Build the sample family a public visitor gets on one click.

The public demo disables the shared library, so a first-time visitor landed on
an empty workspace asking them to upload a contract -- which the terms tell them
not to do. Nothing about the product was visible without committing a document.

Enrichment takes minutes and is rate limited, so it cannot run per visitor.
Instead it runs once, here, and the finished workspace is committed as a bundle
that a session copies. The copy is a few megabytes and completes in under a
second, so the demo is populated before the page finishes settling.

    python scripts/build_demo_bundle.py
    python scripts/build_demo_bundle.py --skip-enrich   # deterministic only
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import (  # noqa: E402
    OperativeRule,
    _construct,
    build_embeddings,
    enrich_workspace,
    read_jsonl,
)
from legal_ingest import rebuild_workspace  # noqa: E402
from lmstudio_client import LMStudioClient  # noqa: E402

SOURCES = ROOT / "samples" / "northwind"
BUNDLE = ROOT / "samples" / "demo_bundle"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--out", type=Path, default=BUNDLE)
    args = parser.parse_args()

    documents = sorted(SOURCES.glob("*.md"))
    if not documents:
        print(f"no source documents in {SOURCES}", file=sys.stderr)
        return 2

    staging = args.out.with_name(args.out.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "sources").mkdir(parents=True)
    for document in documents:
        shutil.copy2(document, staging / "sources" / document.name)
    print(f"{len(documents)} source documents")

    started = time.monotonic()
    result = rebuild_workspace(staging)
    print(
        f"  parsed   {result['clauses']} clauses, {result['definitions']} definitions, "
        f"{result['rules']} rules  ({time.monotonic() - started:.1f}s)"
    )
    offerings = staging / "legal" / "offerings.jsonl"
    print(
        f"  offerings {sum(1 for _ in offerings.open()) if offerings.exists() else 0}"
    )

    client = LMStudioClient()
    model = client.extractor_model
    if not args.skip_enrich:
        started = time.monotonic()

        def progress(done: int, total: int) -> None:
            if done % 40 == 0 or done == total:
                print(f"    enriching {done}/{total}", flush=True)

        summary = enrich_workspace(staging, client, model, progress=progress)
        print(
            f"  enriched {summary.get('rules', 0)} rules, "
            f"{summary.get('failed_clauses', 0)} failed  "
            f"({time.monotonic() - started:.0f}s)"
        )

    rules_path = staging / "legal" / "resolved_rules.jsonl"
    if not rules_path.exists():
        rules_path = staging / "legal" / "operative_rules.jsonl"
    rules = [_construct(OperativeRule, item) for item in read_jsonl(rules_path)]
    embedding = build_embeddings(staging, client, rules)
    print(f"  embedded {embedding.get('records', 0)} records")

    # A manifest so the server can report what the visitor is looking at without
    # re-reading the whole workspace, and so a stale bundle is obvious.
    (staging / "demo.json").write_text(
        json.dumps(
            {
                "name": "Northwind Data Systems (sample)",
                "built_at": int(time.time()),
                "documents": [item.name for item in documents],
                "clauses": result["clauses"],
                "definitions": result["definitions"],
                "enriched": not args.skip_enrich,
                "embedding_records": embedding.get("records", 0),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if args.out.exists():
        shutil.rmtree(args.out)
    staging.rename(args.out)
    size = sum(item.stat().st_size for item in args.out.rglob("*") if item.is_file())
    print(f"\nbundle at {args.out.relative_to(ROOT)}  ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
