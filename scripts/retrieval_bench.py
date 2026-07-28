#!/usr/bin/env python3
"""Does retrieval surface the provision that actually decides the question?

Asked whether a licence is needed for someone who never logged in, the answer
quoted a clause about undeleted accounts and sounded entirely confident. The
provision that decides it -- "authorized to access or use the Software
(regardless of whether the individual accesses or uses the Software)" -- was in
the graph, extracted correctly, and nowhere in the fourteen items retrieved.

Answer quality cannot see that failure: "Yes" was right by accident. This scores
retrieval alone, so the ranking work has a number to move that is not confounded
by what the model does with what it is handed. No answering model is called, so
it runs in seconds.

Embeddings are not optional. `retrieve_evidence` fuses BM25 with vector
similarity, and given no client it silently drops the vector arm and ranks on
BM25 alone. The first version of this file did exactly that, so a whole round of
ranking work was scored against a pipeline the application never runs: the
decisive clause below sat at rank 4 with BM25 alone and did not appear at all
once the vector arm was restored. `--no-embed` isolates the lexical arm on
purpose; it does not measure the product.

Ground truth is a distinctive fragment of the text that ought to be cited rather
than a record id, because ids change with a rebuild and a fragment can be read
and checked by eye.

    python scripts/retrieval_bench.py
    python scripts/retrieval_bench.py --corpus 2 --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import compact_text, retrieve_evidence  # noqa: E402
from library_store import LibraryStore  # noqa: E402
from lmstudio_client import LMStudioClient  # noqa: E402

# corpus 1 -- the families the pipeline was developed against.
# corpus 2 -- the cloud vendors held back, so ranking is not tuned on OpenText.
CASES: list[dict] = [
    # --- the case that prompted this ------------------------------------------
    dict(
        corpus=1,
        family="OpenText",
        question="Do I need a named user license even if the person has never logged in to the software?",
        must_retrieve="regardless of whether the individual accesses or uses the Software",
        note="core obligation of the Standard Named User model; heading carries 'Named User', the text does not",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="Is every file I read via Streamserve counted as a Transaction?",
        must_retrieve="input to, output from, created, processed, or manipulated",
        note="decided entirely by the definition of Transaction",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="Can I assign my license to an affiliate?",
        must_retrieve="without the prior written consent of OT",
        note="assignment is barred; allocation to Affiliates is not -- different provisions",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="What if a customer doesn't cooperate with an audit?",
        must_retrieve="reimburse all reasonable costs incurred by OT",
        note="consequence of failing an audit",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="What is a 'CPU'?",
        must_retrieve="a single central processing unit",
        note="plain definitional lookup",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="May an Occasional Named User access the Software on more than 52 days in a calendar year?",
        must_retrieve="52 calendar days",
        note="a variant's stated exception to the model it inherits",
    ),
    dict(
        corpus=1,
        family="OpenText",
        question="May Actuate Named User licences be allocated to functions or shared processes?",
        must_retrieve="may not allocate Actuate Named User licenses to functions",
        note="prohibition; the modal is 'may' inside a negative",
    ),
    # --- corpus 2, never tuned against ----------------------------------------
    dict(
        corpus=2,
        family="Salesforce",
        question="Does SFDC warrant that usage data provided through the Free Services will be accurate?",
        must_retrieve="DO NOT REPRESENT OR WARRANT",
        note="the negating chapeau; the list item alone inverts the meaning",
    ),
    dict(
        corpus=2,
        family="Salesforce",
        question="Can Customer use the Free Services for production?",
        must_retrieve="Free Services",
        note="loose on purpose -- checks the subject is reached at all",
    ),
    dict(
        corpus=2,
        family="Qlik",
        question="May a Named User licence be transferred to another user?",
        must_retrieve="transferred",
        note="licence metric transfer terms",
    ),
    dict(
        corpus=2,
        family="Adobe",
        question="What happens to Customer Data on termination?",
        must_retrieve="terminat",
        note="loose -- corpus 2 ground truth kept shallow to avoid tuning to it",
    ),
    dict(
        corpus=2,
        family="Okta",
        question="Who is responsible for the security of Customer's account credentials?",
        must_retrieve="credential",
        note="loose -- see above",
    ),
]


def rank_of(evidence: list[dict], fragment: str) -> int:
    """1-based rank of the first item quoting the fragment, or 0 if absent."""

    wanted = compact_text(fragment).casefold()
    for position, item in enumerate(evidence, start=1):
        if wanted in compact_text(str(item.get("text", ""))).casefold():
            return position
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "data" / "library")
    parser.add_argument(
        "--corpus", type=int, choices=(1, 2), help="restrict to one corpus"
    )
    parser.add_argument("--limit", type=int, default=14, help="evidence budget")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="lexical arm only -- diagnostic, not the shipped pipeline",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    client = None
    if not args.no_embed:
        client = LMStudioClient()
        # Falling back to BM25 on a dead embedding server would print a plausible
        # score for the wrong pipeline, which is the mistake this guard exists to
        # stop repeating.
        client.embeddings(
            ["ranking benchmark reachability probe"], input_type="search_query"
        )

    families = {item.name: item for item in LibraryStore(args.root).list()}
    cases = [
        case
        for case in CASES
        if (args.corpus is None or case["corpus"] == args.corpus)
        and case["family"] in families
    ]
    if not cases:
        print("no cases match -- are the families loaded?", file=sys.stderr)
        return 2

    found = 0
    reciprocal = 0.0
    rows: list[tuple[dict, int]] = []
    for case in cases:
        evidence = retrieve_evidence(
            families[case["family"]].root,
            case["question"],
            limit=args.limit,
            embedding_client=client,
        )
        rank = rank_of(evidence, case["must_retrieve"])
        rows.append((case, rank))
        if rank:
            found += 1
            reciprocal += 1.0 / rank
        print(
            f"  {'rank ' + str(rank) if rank else 'MISSING':>9}  "
            f"[{case['corpus']}] {case['family'][:12]:14} {case['question'][:52]}",
            flush=True,
        )
        if args.verbose and not rank:
            print(f"            wanted: {case['must_retrieve'][:70]}")
            for position, item in enumerate(evidence[:5], start=1):
                print(
                    f"            got [{position}] {str(item.get('citation', ''))[:34]:36}"
                    f" {str(item.get('text', ''))[:52]}"
                )

    total = len(cases)
    print(f"\n{'=' * 70}")
    print(f"recall@{args.limit}   {found}/{total} ({found / total:.0%})")
    print(f"MRR         {reciprocal / total:.3f}")
    for corpus in sorted({case["corpus"] for case in cases}):
        subset = [(case, rank) for case, rank in rows if case["corpus"] == corpus]
        hits = sum(1 for _, rank in subset if rank)
        print(f"  corpus {corpus}    {hits}/{len(subset)}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
