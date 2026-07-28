#!/usr/bin/env python3
"""What was said earlier in this workspace's chat.

Every question was answered as though it were the first. Asked "what is a CPU",
the assistant correctly found two licence models, listed them and ended "Which
applies?" -- and then received the word "logical" with no record that it had
asked anything, and replied that "logical" is not a question. Offering a choice
and then being unable to hear the answer is worse than not offering it.

History lives in the visitor's own workspace and expires with it, so this adds
no retention beyond what the privacy notice already describes.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

HISTORY_FILE = ".conversation.jsonl"
# Enough for a follow-up to resolve against, short enough that a long session
# cannot crowd the evidence out of the prompt.
KEEP_TURNS = 8
# A reply selecting from a menu is short and has no verb. Anything longer is a
# new question and must not be rewritten into the previous one.
SELECTOR_WORDS = 6

STOP = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "to",
    "in",
    "on",
    "and",
    "or",
    "please",
    "one",
    "that",
    "this",
    "it",
    "model",
    "licence",
    "license",
    "type",
}


def _path(root: Path) -> Path:
    return root / HISTORY_FILE


def load(root: Path, limit: int = KEEP_TURNS) -> list[dict]:
    path = _path(root)
    if not path.is_file():
        return []
    turns: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                turns.append(json.loads(line))
    except (OSError, ValueError):
        return []
    return turns[-limit:]


def append(root: Path, turn: dict, limit: int = KEEP_TURNS) -> None:
    turn = {**turn, "at": int(time.time())}
    turns = load(root, limit * 2) + [turn]
    body = "\n".join(json.dumps(item, ensure_ascii=False) for item in turns[-limit:])
    try:
        _path(root).write_text(body + "\n", encoding="utf-8")
    except OSError:
        # A workspace that expired mid-request is not worth failing a good
        # answer over.
        pass


def clear(root: Path) -> None:
    try:
        _path(root).unlink()
    except OSError:
        pass


def _tokens(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", str(value).lower())
        if word not in STOP and len(word) > 1
    }


def resolve_followup(question: str, history: list[dict]) -> tuple[str, str]:
    """Rewrite a menu selection into the question it is answering.

    Returns the question to actually run and, where a rewrite happened, the
    variant that was selected so the interface can say what it understood.
    Anything that does not look like a selection is returned untouched: a
    follow-up that is itself a question must be answered as asked.
    """

    asked = question.strip()
    if not asked or not history:
        return asked, ""
    previous = history[-1]
    offered = [str(name) for name in previous.get("offered") or []]
    if not offered:
        return asked, ""
    if len(asked.split()) > SELECTOR_WORDS or asked.endswith("?"):
        return asked, ""

    wanted = _tokens(asked)
    if not wanted:
        return asked, ""
    matches = [name for name in offered if wanted <= _tokens(name)]
    # "production cpu" is contained in both "Production CPU" and "Production
    # Logical CPU", so containment alone calls the exact name ambiguous. Naming
    # a variant outright is the least ambiguous thing a reader can do.
    exact = [name for name in offered if _tokens(name) == wanted]
    if len(exact) == 1:
        matches = exact
    if len(matches) != 1:
        # Ambiguous or unrecognised: answering the wrong variant confidently is
        # the failure this whole path exists to prevent.
        return asked, ""

    chosen = matches[0]
    original = str(previous.get("question", "")).strip().rstrip("?")
    if not original:
        return chosen, chosen
    return f"{original}, specifically the {chosen}?", chosen


def recap(history: list[dict], limit: int = 2) -> str:
    """A compact record of the last few turns, for the prompt."""

    recent = [item for item in history if item.get("question")][-limit:]
    if not recent:
        return ""
    lines = []
    for item in recent:
        answer = " ".join(str(item.get("answer", "")).split())[:220]
        lines.append(f"Q: {item['question']}\nA: {answer}")
    return "\n".join(lines)
