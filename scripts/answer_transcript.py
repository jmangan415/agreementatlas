#!/usr/bin/env python3
"""Write out exactly what is sent to the model, and exactly what comes back.

The inference server's log truncates the system prompt, the question and the
answer, which makes it useless for the thing you actually want to check: that
the prompt says what you think it says, and that the evidence handed over is
the evidence you meant. This wraps the client, records every field verbatim,
and writes one readable transcript.

    python scripts/answer_transcript.py --family "OpenText"
    python scripts/answer_transcript.py --family Qlik --out tmp/qlik.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_graph_service import answer_question  # noqa: E402
from library_store import LibraryStore  # noqa: E402
from lmstudio_client import LMStudioClient  # noqa: E402

QUESTIONS = [
    "Is every file I read via Streamserve counted as a Transaction?",
    "Can I assign my license to an affiliate?",
    "What if a customer doesn't cooperate with an audit?",
    "What is a 'CPU'?",
    "Do I need a named user license even if the person has never logged in to the software?",
]


class RecordingClient:
    """Passes everything through, keeping a verbatim copy of each exchange."""

    def __init__(self, inner: LMStudioClient) -> None:
        self._inner = inner
        self.exchanges: list[dict] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def chat(self, *, model, system, user, **kwargs):
        answer = self._inner.chat(model=model, system=system, user=user, **kwargs)
        self.exchanges.append(
            {"model": model, "system": system, "user": user, "answer": answer}
        )
        return answer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", default="OpenText")
    parser.add_argument("--model", default="google/gemma-4-26b-a4b-qat")
    parser.add_argument("--out", type=Path, default=ROOT / "tmp" / "transcript.md")
    parser.add_argument("--questions", nargs="*", default=QUESTIONS)
    args = parser.parse_args()

    family = next(
        (
            item
            for item in LibraryStore(ROOT / "data" / "library").list()
            if item.name.casefold() == args.family.casefold()
        ),
        None,
    )
    if family is None:
        print(f"no family named {args.family!r}", file=sys.stderr)
        return 2

    client = RecordingClient(LMStudioClient())
    lines: list[str] = [
        f"# Answer transcript — {family.name}",
        "",
        f"Model: `{args.model}`  ·  {len(args.questions)} questions",
        "",
        "Everything below is verbatim: the system prompt as sent, the user "
        "message as sent (question, resolution trace and evidence), and the "
        "answer as returned.",
        "",
    ]
    for index, question in enumerate(args.questions, start=1):
        print(
            f"  [{index}/{len(args.questions)}] {question[:56]}",
            file=sys.stderr,
            flush=True,
        )
        before = len(client.exchanges)
        try:
            result = answer_question(family.root, client, args.model, question)
            answer = result["answer"]
            evidence = result.get("evidence", [])
        except Exception as error:  # noqa: BLE001 -- the transcript records failures too
            answer, evidence = f"ERROR {error}", []
        exchange = client.exchanges[before] if len(client.exchanges) > before else {}
        lines += [
            "",
            "---",
            "",
            f"## Q{index}. {question}",
            "",
            f"**Evidence items retrieved:** {len(evidence)}",
            "",
            "### System prompt (verbatim)",
            "",
            "```text",
            exchange.get("system", "(no exchange recorded)"),
            "```",
            "",
            "### User message (verbatim)",
            "",
            "```text",
            exchange.get("user", "(no exchange recorded)"),
            "```",
            "",
            "### Answer (verbatim)",
            "",
            "```text",
            answer,
            "```",
        ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    total = sum(len(item.get("user", "")) for item in client.exchanges)
    print(f"\nwrote {args.out} ({args.out.stat().st_size} bytes)")
    print(f"user messages totalled {total} chars (~{total // 4} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
