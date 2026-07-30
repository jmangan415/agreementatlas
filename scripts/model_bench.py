#!/usr/bin/env python3
"""Score answering models against each other on the same retrieved evidence.

Two assumptions went untested for weeks: that the structured reading carried
into the prompt helps, and that the local model is good enough. Both are
measurable, and neither was measured.

Retrieval embeds the question locally, so swapping the whole client would change
the evidence as well as the model and the comparison would measure both at once.
A cloud model therefore keeps local retrieval and only the answering call goes
out. Nothing here runs in the request path; the application reads no cloud
credentials and inference stays on this machine unless this script is run.

    python scripts/model_bench.py                              # local, reading on/off
    python scripts/model_bench.py --models cloud:gpt-5,google/gemma-4-26b-a4b-qat
    python scripts/model_bench.py --list-cloud-models

Each case is scored against checks a machine can settle: how the answer must
open, what it must mention, what it must not claim.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import legal_graph_service as svc  # noqa: E402
from legal_graph_service import answer_question  # noqa: E402
from library_store import LibraryStore  # noqa: E402
from lmstudio_client import LMStudioClient, LMStudioError  # noqa: E402


class HybridClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 180):
        self._local = LMStudioClient()
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    # retrieval path -- unchanged, local
    def embeddings(self, *args, **kwargs):
        return self._local.embeddings(*args, **kwargs)

    @property
    def extractor_model(self):
        return self._local.extractor_model

    @property
    def embedding_model(self):
        return self._local.embedding_model

    def status(self):
        return self._local.status()

    # answering path -- cloud
    def chat(self, *, model, system, user, temperature=0.1, max_tokens=1600, **_):
        # A reasoning model spends the completion budget thinking before it
        # writes anything. At the 1600 the local path uses, gpt-5 consumed all
        # 1600 on reasoning and returned an empty string with finish_reason
        # "length" -- which scored as a bad answer rather than as no answer.
        # The budget is headroom, not a target: gpt-5 reasons ~770 and answers
        # in ~450 when given room.
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max(max_tokens, 6000),
        }
        request = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except Exception as error:
            detail = getattr(error, "read", lambda: b"")()
            raise LMStudioError(f"{error} {detail[:300]!r}") from error
        text = body["choices"][0]["message"]["content"]
        if not text.strip():
            reason = body["choices"][0].get("finish_reason", "")
            raise LMStudioError(f"empty completion (finish_reason={reason})")
        return text


class AnthropicClient(HybridClient):
    """Anthropic speaks a different wire format, not an OpenAI-compatible one."""

    def chat(self, *, model, system, user, temperature=0.1, max_tokens=1600, **_):
        payload = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        request = urllib.request.Request(
            f"{self._base}/messages",
            data=json.dumps(payload).encode(),
            headers={
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except Exception as error:
            detail = getattr(error, "read", lambda: b"")()
            raise LMStudioError(f"{error} {detail[:300]!r}") from error
        return "".join(
            part.get("text", "")
            for part in body.get("content", [])
            if isinstance(part, dict)
        )


def list_anthropic_models(api_key: str) -> list[str]:
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return [item["id"] for item in json.loads(response.read())["data"]]


def list_models(base_url: str, api_key: str) -> list[str]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return sorted(item["id"] for item in json.loads(response.read())["data"])


CASES = [
    # Negation traps -- the clause says the opposite of what a careless read gives.
    dict(
        family="Salesforce",
        q="Does SFDC warrant that Customer's use of the Free Services will be uninterrupted?",
        opens=("no",),
        must=(),
        # Quoting the disclaimed words is how you show the disclaimer, so the
        # earlier check penalised every model for answering correctly.
        must_not=("sfdc warrants that",),
    ),
    dict(
        family="Salesforce",
        q="Does SFDC warrant that usage data provided through the Free Services will be accurate?",
        opens=("no",),
        must=(),
        must_not=(),
    ),
    dict(
        family="OpenText",
        q="May an Occasional Named User access the Software on more than 52 days in a calendar year?",
        opens=("no",),
        must=("52",),
        must_not=(),
    ),
    dict(
        family="OpenText",
        q="May Actuate Named User licences be allocated to functions or shared processes?",
        opens=("no",),
        must=(),
        must_not=(),
    ),
    # Actor: who bears the duty.
    dict(
        family="OpenText",
        q="Who must document allocations of Actuate Named User Licenses?",
        opens=(),
        must=("licensee",),
        must_not=(),
    ),
    # Definitional limbs.
    dict(
        family="OpenText",
        q="Is every file read by StreamServe counted as a Transaction?",
        opens=("yes",),
        must=("transaction",),
        must_not=("inconclusive", "cannot determine"),
    ),
    # Ambiguity: must not answer for one variant.
    dict(
        family="OpenText",
        q="what is a named user",
        opens=(),
        must=("standard", "occasional", "actuate"),
        must_not=(),
    ),
]


def score(case, text):
    # A failed call must score nothing. Scoring it against the checks gave the
    # must-not clauses a vacuous pass, and three models that never loaded came
    # back at 4/15 apiece, which reads as a weak result rather than no result.
    if text.startswith("ERROR "):
        return 0, 0
    low = text.lower()
    points = hits = 0
    if case["opens"]:
        hits += 1
        first = low.strip()[:60]
        if any(re.match(rf"^\W*{w}\b", first) for w in case["opens"]):
            points += 1
    for term in case["must"]:
        hits += 1
        if term.lower() in low:
            points += 1
    for term in case["must_not"]:
        hits += 1
        if term.lower() not in low:
            points += 1
    return points, hits


def make_client(model):
    """Local by default; a cloud model keeps local retrieval and only answers."""
    if not model.startswith("cloud:"):
        return LMStudioClient(), model
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    name = model.split(":", 1)[1]
    # One key field, either vendor. An Anthropic key is recognisable and the
    # wire format differs, so the client is chosen from the key rather than
    # asking the caller to keep two settings straight.
    if key.startswith("sk-ant-"):
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        if not key:
            raise SystemExit("OPENAI_API_KEY is not set")
        return AnthropicClient(base, key), name

    key = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set")
    return HybridClient(base, key), model.split(":", 1)[1]


def run(model, structured, transcript):
    original = svc.evidence_block
    if not structured:

        def plain(index, item):
            return (
                f"[{index}] SOURCE={item['source']} SECTION={item['section']} "
                f"SCOPE={item['scope']}\n{item['text']}"
            )

        svc.evidence_block = plain
    client, model = make_client(model)
    got = total = failures = 0
    seconds = 0.0
    detail = []
    for case in CASES:
        fam = next(
            (
                f
                for f in LibraryStore(Path("data/library")).list()
                if f.name == case["family"]
            ),
            None,
        )
        if not fam:
            continue
        # A case may open with unscored turns, so the scored question arrives
        # as a real follow-up. History is recorded the way app.py records it:
        # the standalone rewrite where one happened, the answer capped at 600.
        history = []
        for prior in case.get("prior", ()):
            try:
                first = answer_question(fam.root, client, model, prior)
            except Exception:
                break
            history.append(
                {
                    "question": str(first.get("understood_as") or prior),
                    "answer": str(first.get("answer", ""))[:600],
                    "offered": first.get("offered") or [],
                }
            )
        started = time.monotonic()
        try:
            answer = answer_question(
                fam.root, client, model, case["q"], history=history
            )["answer"]
        except Exception as error:
            answer = f"ERROR {error}"
        seconds += time.monotonic() - started
        points, hits = score(case, answer)
        if not hits:
            failures += 1
        got += points
        total += hits
        detail.append((case["q"][:44], points, hits, answer[:90].replace("\n", " ")))
        transcript.append(
            {
                "id": case.get("id", case["q"][:40]),
                "family": case["family"],
                "question": case["q"],
                "points": points,
                "hits": hits,
                "answer": answer,
            }
        )
    svc.evidence_block = original
    return got, total, seconds, detail, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="google/gemma-4-26b-a4b-qat")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--list-cloud-models", action="store_true")
    parser.add_argument(
        "--save-answers",
        type=Path,
        help="Write every full answer to this JSON file, keyed by model and "
        "reading toggle. Keyword scoring misfires -- an answer can contain "
        "'two' while refusing to choose -- so the numbers it produces are "
        "only final after a human reads the answers. This file is what makes "
        "that read possible.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        help="JSON list of cases (family/q/opens/must/must_not) replacing the "
        "built-in suite. The built-in seven saturated -- both models score "
        "14/14 -- so a harder set lives beside this script rather than here.",
    )
    args = parser.parse_args()
    if args.cases:
        global CASES
        CASES = json.loads(args.cases.read_text(encoding="utf-8"))

    if args.list_cloud_models:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            print("OPENAI_API_KEY is empty in .env", file=sys.stderr)
            return 2
        names = (
            list_anthropic_models(key)
            if key.startswith("sk-ant-")
            else list_models(
                os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), key
            )
        )
        for name in names:
            print(name)
        return 0

    print(f"{'model':34} {'reading':>9} {'score':>10} {'seconds':>9}")
    answers: dict[str, list] = {}
    for model in args.models.split(","):
        for structured in (True, False):
            transcript: list[dict] = []
            got, total, seconds, detail, failures = run(
                model.strip(), structured, transcript
            )
            answers[f"{model.strip()} reading={'on' if structured else 'off'}"] = (
                transcript
            )
            print(
                f"{model.strip()[:34]:34} {'on' if structured else 'off':>9} "
                f"{got:4}/{total:<5} {seconds:8.0f}"
                + (f"   {failures} CALL(S) FAILED" if failures else ""),
                flush=True,
            )
            if args.verbose:
                for question, points, hits, answer in detail:
                    print(f"      {points}/{hits}  {question:46} {answer}")
    if args.save_answers:
        args.save_answers.write_text(
            json.dumps(answers, indent=1, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
