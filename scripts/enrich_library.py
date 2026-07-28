#!/usr/bin/env python3
"""Enrich every family, most important first, several at once.

Enrichment has only ever been driven from the web request path, one family at a
time and one clause per request. The library is 5,682 substantive clauses, which
at the measured 7.6 s/clause is about twelve hours strictly serial.

The concurrency here is *across families*, not within one. Running several
`enrich_workspace` calls at once puts several requests in flight without
touching the extraction loop, which is the code the whole night depends on --
refactoring it minutes before an unattended run is the wrong trade. Each family
writes only to its own workspace, so there is no shared state to protect.

One slot is deliberately left idle: the public demo shares this LM Studio, and a
visitor asking a question should not queue behind the rebuild.

    python scripts/enrich_library.py --measure        # 1 vs 3, then stop
    python scripts/enrich_library.py
    python scripts/enrich_library.py --only "SAP Cloud"
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import (  # noqa: E402
    OperativeRule,
    _construct,
    build_embeddings,
    enrich_workspace,
    read_jsonl,
    substantive_clauses,
)
from library_store import LibraryStore  # noqa: E402
from lmstudio_client import LMStudioClient  # noqa: E402

# The corpora the benchmarks and the demo actually use. Whatever else happens
# overnight, these must be finished by morning.
GOLDEN = ("SAP Cloud", "OpenText (full set)", "OpenText")

speak = threading.Lock()


def say(message: str) -> None:
    with speak:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def ordered(families: list) -> list:
    """Golden sets first, then smallest first so failures surface early."""

    rank = {name: index for index, name in enumerate(GOLDEN)}

    def size(family) -> int:
        try:
            return len(substantive_clauses(family.root))
        except Exception:  # noqa: BLE001
            return 0

    return sorted(families, key=lambda f: (rank.get(f.name, len(GOLDEN)), size(f)))


def enrich_one(family, model: str) -> dict:
    """One family, start to finish, including its embeddings."""

    started = time.monotonic()
    # A client per thread: the shared one is not documented as thread-safe and
    # this is running unattended.
    client = LMStudioClient()
    clauses = len(substantive_clauses(family.root))
    say(f"start   {family.name} ({clauses} clauses)")
    summary = enrich_workspace(family.root, client, model)

    rules_path = family.root / "legal" / "resolved_rules.jsonl"
    if not rules_path.exists():
        rules_path = family.root / "legal" / "operative_rules.jsonl"
    rules = [_construct(OperativeRule, item) for item in read_jsonl(rules_path)]
    embedding = build_embeddings(family.root, client, rules)

    elapsed = time.monotonic() - started
    say(
        f"done    {family.name}: {summary.get('rules', 0)} rules, "
        f"{summary.get('failed_clauses', 0)} failed, "
        f"{embedding.get('records', 0)} vectors, {elapsed / 60:.1f} min"
        + (f" ({elapsed / clauses:.1f}s/clause)" if clauses else "")
    )
    return {"family": family.name, "seconds": elapsed, "clauses": clauses}


def measure(families: list, model: str) -> int:
    """Time one family alone, then three at once, before betting the night."""

    sample = [f for f in families if f.name in GOLDEN][:3]
    if len(sample) < 2:
        say("not enough families to measure")
        return 1
    say(f"measuring: 1 family alone, then {len(sample)} together")
    solo = enrich_one(sample[0], model)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(sample)) as pool:
        results = [
            item.result()
            for item in as_completed(
                [pool.submit(enrich_one, f, model) for f in sample[1:]]
            )
        ]
    wall = time.monotonic() - started
    done = sum(r["clauses"] for r in results)
    say(
        f"serial {solo['seconds'] / max(1, solo['clauses']):.2f}s/clause · "
        f"concurrent {wall / max(1, done):.2f}s/clause across {len(results)} families"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "library")
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="families at once; leave one LM Studio slot for the public demo",
    )
    parser.add_argument("--only", action="append")
    parser.add_argument("--measure", action="store_true")
    args = parser.parse_args()

    client = LMStudioClient()
    model = client.extractor_model
    families = [
        item
        for item in LibraryStore(args.root).list()
        if not args.only or item.name in args.only
    ]
    if not families:
        print("no families matched", file=sys.stderr)
        return 2
    families = ordered(families)
    say(f"model {model} · {len(families)} families · {args.workers} at a time")
    say("order: " + ", ".join(f.name for f in families))

    if args.measure:
        return measure(families, model)

    failures = 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(enrich_one, f, model): f for f in families}
        for future in as_completed(futures):
            family = futures[future]
            try:
                future.result()
            except Exception as error:  # noqa: BLE001 - one bad family must not end the night
                failures += 1
                say(f"FAILED  {family.name}: {error}")
                traceback.print_exc(file=sys.stderr)
    say(f"finished in {(time.monotonic() - started) / 3600:.1f} h · {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
