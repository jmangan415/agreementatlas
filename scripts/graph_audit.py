#!/usr/bin/env python3
"""Traverse a built workspace and catalogue rule- and graph-level defects.

`parse_health.py` asks whether the documents parsed. This asks whether what was
built on top of them holds together: does every rule cite text that exists, does
any rule contradict itself, does the precedence graph state two opposite things
at once.

Every check here is a defect that was found by reading one record at a time.
That does not scale, and a defect nobody happens to click on is a defect that
ships.

    python scripts/graph_audit.py --root data/library            # whole library
    python scripts/graph_audit.py --root data/library/<id>       # one family
    python scripts/graph_audit.py --root knowledge --json

Exit code is 1 when any ERROR-severity finding is present.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legal_graph_service import read_jsonl  # noqa: E402

# A modal verb. More than one in a single rule's evidence means the clause was
# not split into its separate statements.
MODAL = re.compile(r"\b(shall|must|may|will|cannot|can not)\b", re.I)
# "L icensee", "o ther" -- a letter stranded from its word by PDF extraction.
SPLIT_WORD = re.compile(r"\b[A-Za-z]\s[a-z]{2,}\b")
# Titles that are page furniture rather than the name of an instrument.
TITLE_FURNITURE = re.compile(
    r"^(?:v(?:ersion)?\.?\s*\d|page\s+\d|\d{1,2}\.?$|"
    r"(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+\d{4})",
    re.I,
)
EFFECT_MODALITY = {
    "PROHIBITION": {"SHALL", "MUST", "MAY", "WILL", "CAN", "OTHER"},
    "OBLIGATION": {"SHALL", "MUST", "WILL", "OTHER"},
    "PERMISSION": {"MAY", "CAN", "OTHER"},
}
GENERIC_ACTIONS = {"govern", "unspecified", ""}


@dataclass
class Finding:
    check: str
    severity: str
    detail: str
    sample: str = ""


@dataclass
class Audit:
    family: str
    counts: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def add(
        self, check: str, severity: str, hits: list, total: int, sample: str = ""
    ) -> None:
        if not hits:
            return
        share = f"{len(hits)}/{total}" if total else str(len(hits))
        self.findings.append(
            Finding(
                check, severity, f"{share} ({len(hits) / max(1, total):.0%})", sample
            )
        )

    @property
    def worst(self) -> str:
        levels = {item.severity for item in self.findings}
        if "ERROR" in levels:
            return "ERROR"
        return "WARN" if "WARN" in levels else "OK"


def first(hits: list, key=lambda item: item) -> str:
    return str(key(hits[0]))[:150] if hits else ""


def audit_family(name: str, root: Path) -> Audit:
    report = Audit(name)
    legal = root / "legal"
    rules = read_jsonl(legal / "operative_rules.jsonl")
    clauses = {
        str(item.get("id")): item for item in read_jsonl(legal / "clauses.jsonl")
    }
    spans = {
        str(item.get("id")): item for item in read_jsonl(legal / "evidence_spans.jsonl")
    }
    instruments = read_jsonl(legal / "instruments.jsonl")
    precedence = read_jsonl(legal / "precedence_rules.jsonl")
    definitions = read_jsonl(legal / "defined_terms.jsonl")
    parties = read_jsonl(legal / "parties.jsonl")
    known_actors = {str(item.get("role", "")).lower() for item in parties} | {
        str(item.get("entity_name", "")).lower() for item in parties
    }
    known_actors.discard("")
    instrument_ids = {str(item.get("id")) for item in instruments}

    report.counts = {
        "instruments": len(instruments),
        "clauses": len(clauses),
        "rules": len(rules),
        "definitions": len(definitions),
        "precedence_rules": len(precedence),
    }
    total = len(rules)

    # --- rule internal consistency ---------------------------------------------
    same = [
        item
        for item in rules
        if item.get("actor")
        and item.get("object")
        and str(item["actor"]).strip().lower() == str(item["object"]).strip().lower()
    ]
    report.add(
        "actor-is-object",
        "ERROR",
        same,
        total,
        first(same, lambda r: f"{r['actor']!r} :: {r['evidence'][:80]}"),
    )

    unknown = [
        item
        for item in rules
        if item.get("actor") and str(item["actor"]).lower() not in known_actors
    ]
    report.add(
        "actor-not-a-party",
        "WARN",
        unknown,
        total,
        first(unknown, lambda r: f"{r['actor']!r} :: {r['evidence'][:70]}"),
    )

    contradictory = [
        item
        for item in rules
        if item.get("effect") in EFFECT_MODALITY
        and item.get("modality")
        and item["modality"] not in EFFECT_MODALITY[item["effect"]]
    ]
    report.add(
        "effect-modality-mismatch",
        "ERROR",
        contradictory,
        total,
        first(contradictory, lambda r: f"{r['effect']}/{r['modality']}"),
    )

    polarity = [
        item
        for item in rules
        if (item.get("effect") == "PROHIBITION" and item.get("polarity") == "POSITIVE")
        or (item.get("effect") == "PERMISSION" and item.get("polarity") == "NEGATIVE")
    ]
    report.add(
        "effect-polarity-mismatch",
        "ERROR",
        polarity,
        total,
        first(
            polarity, lambda r: f"{r['effect']}/{r['polarity']}: {r['evidence'][:60]}"
        ),
    )

    generic = [item for item in rules if str(item.get("action", "")) in GENERIC_ACTIONS]
    report.add(
        "action-not-derived",
        "WARN",
        generic,
        total,
        first(generic, lambda r: r["evidence"][:80]),
    )

    # --- evidence grounding -----------------------------------------------------
    orphan = [item for item in rules if str(item.get("clause_id")) not in clauses]
    report.add(
        "rule-cites-missing-clause",
        "ERROR",
        orphan,
        total,
        first(orphan, lambda r: r.get("clause_id", "")),
    )

    unquotable = [
        item
        for item in rules
        if str(item.get("clause_id")) in clauses
        and item.get("evidence")
        and item["evidence"] not in clauses[str(item["clause_id"])].get("text", "")
    ]
    report.add(
        "evidence-not-in-clause",
        "ERROR",
        unquotable,
        total,
        first(unquotable, lambda r: r["evidence"][:80]),
    )

    spanless = [item for item in rules if not item.get("evidence_span_ids")]
    report.add("rule-without-span", "WARN", spanless, total)

    dangling = [
        item
        for item in rules
        for span_id in item.get("evidence_span_ids", [])
        if span_id not in spans
    ]
    report.add("rule-span-missing", "ERROR", dangling, total)

    misaligned = []
    for span in spans.values():
        clause = clauses.get(str(span.get("clause_id")))
        if not clause:
            continue
        text = clause.get("text", "")
        start, end = int(span.get("start", 0)), int(span.get("end", 0))
        if text[start:end].strip() != str(span.get("text", "")).strip():
            misaligned.append(span)
    report.add(
        "span-offsets-wrong",
        "ERROR",
        misaligned,
        len(spans),
        first(misaligned, lambda s: s.get("text", "")[:80]),
    )

    multi = [
        item
        for item in rules
        if len(set(MODAL.findall(str(item.get("evidence", ""))))) > 1
    ]
    report.add(
        "unsplit-multi-statement",
        "WARN",
        multi,
        total,
        first(multi, lambda r: r["evidence"][:110]),
    )

    damaged = [
        item for item in rules if SPLIT_WORD.search(str(item.get("evidence", "")))
    ]
    report.add(
        "split-word-damage",
        "WARN",
        damaged,
        total,
        first(damaged, lambda r: r["evidence"][:80]),
    )

    # Two clause records sharing one id: every id-keyed lookup then resolves to
    # whichever was written last, and the enrichment checkpoint -- which counts
    # distinct ids -- can never reach a record count that includes both.
    clause_ids = Counter(
        str(item.get("id")) for item in read_jsonl(legal / "clauses.jsonl")
    )
    collisions = [key for key, count in clause_ids.items() if count > 1]
    report.add(
        "duplicate-clause-ids",
        "ERROR",
        collisions,
        sum(clause_ids.values()),
        first(collisions),
    )

    duplicates = Counter(
        (str(item.get("clause_id")), item.get("effect"), str(item.get("evidence")))
        for item in rules
    )
    repeated = [key for key, count in duplicates.items() if count > 1]
    report.add("duplicate-rules", "WARN", repeated, total)

    # --- precedence graph -------------------------------------------------------
    pairs = {
        (str(item.get("higher_instrument_id")), str(item.get("lower_instrument_id")))
        for item in precedence
    }
    opposing = [pair for pair in pairs if (pair[1], pair[0]) in pairs]
    report.add("precedence-contradiction", "ERROR", opposing, len(pairs) or 1)

    reflexive = [pair for pair in pairs if pair[0] == pair[1]]
    report.add("precedence-self-loop", "ERROR", reflexive, len(pairs) or 1)

    unknown_ends = [
        pair
        for pair in pairs
        if pair[0] not in instrument_ids or pair[1] not in instrument_ids
    ]
    report.add("precedence-unknown-instrument", "ERROR", unknown_ends, len(pairs) or 1)

    edges: defaultdict[str, set[str]] = defaultdict(set)
    for higher, lower in pairs:
        edges[higher].add(lower)
    cycles = find_cycles(edges)
    report.add("precedence-cycle", "ERROR", cycles, len(pairs) or 1, first(cycles))

    # --- instruments and definitions -------------------------------------------
    furniture = [
        item
        for item in instruments
        if TITLE_FURNITURE.match(str(item.get("title", "")).strip())
    ]
    report.add(
        "title-is-furniture",
        "ERROR",
        furniture,
        len(instruments),
        first(furniture, lambda i: f"{i.get('title')!r} <- {i.get('source')}"),
    )

    untitled = [
        item
        for item in instruments
        if len(str(item.get("title", "")).strip()) < 6
        or str(item.get("title", "")).strip().lower() == "untitled agreement"
    ]
    report.add("title-too-short", "WARN", untitled, len(instruments))

    by_term: defaultdict[str, set[str]] = defaultdict(set)
    for item in definitions:
        by_term[str(item.get("term", "")).lower()].add(
            str(item.get("instrument_id") or item.get("document_id"))
        )
    competing = [term for term, owners in by_term.items() if len(owners) > 1]
    graph_path = root / "output" / "legal_relationship_graph.json"
    controlling = 0
    if graph_path.exists():
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        controlling = sum(
            1
            for edge in graph.get("relationships", [])
            if edge.get("type") == "CONTROLLING_DEFINITION"
        )
    if competing and not controlling:
        report.findings.append(
            Finding(
                "competing-definitions-unresolved",
                "WARN",
                f"{len(competing)} term(s) defined in more than one instrument, "
                "but no CONTROLLING_DEFINITION edge decides between them",
                ", ".join(sorted(competing)[:4]),
            )
        )
    return report


def find_cycles(edges: dict[str, set[str]]) -> list[str]:
    """Any precedence loop. A ranking that comes back to itself decides nothing."""

    cycles: list[str] = []
    colour: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = 1
        for peer in sorted(edges.get(node, ())):
            if colour.get(peer) == 1:
                cycles.append(" > ".join(trail[trail.index(peer) :] + [peer]))
            elif colour.get(peer, 0) == 0:
                visit(peer, trail + [peer])
        colour[node] = 2

    for node in sorted(edges):
        if colour.get(node, 0) == 0:
            visit(node, [node])
    return cycles


def families(root: Path) -> list[tuple[str, Path]]:
    if (root / "legal" / "clauses.jsonl").exists():
        return [(root.name, root)]
    found = []
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and (candidate / "legal" / "clauses.jsonl").exists():
            name = candidate.name
            metadata = candidate / "family.json"
            if metadata.exists():
                try:
                    name = json.loads(metadata.read_text(encoding="utf-8")).get(
                        "name", name
                    )
                except json.JSONDecodeError:
                    pass
            found.append((name, candidate))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/library"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    groups = families(args.root)
    if not groups:
        print(f"no built workspace under {args.root}", file=sys.stderr)
        return 2

    reports = [audit_family(name, path) for name, path in groups]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "family": item.family,
                        "status": item.worst,
                        "counts": item.counts,
                        "findings": [vars(finding) for finding in item.findings],
                    }
                    for item in reports
                ],
                indent=2,
            )
        )
        return 1 if any(item.worst == "ERROR" for item in reports) else 0

    for item in reports:
        print(f"\n{'=' * 78}\n[{item.worst}] {item.family}\n{'=' * 78}")
        print("  " + " ".join(f"{key}={value}" for key, value in item.counts.items()))
        if not item.findings:
            print("  no defects found")
        for finding in sorted(
            item.findings, key=lambda f: (f.severity != "ERROR", f.check)
        ):
            print(f"    [{finding.severity:5}] {finding.check:32} {finding.detail}")
            if finding.sample:
                print(f"              e.g. {finding.sample}")

    errors = [item for item in reports if item.worst == "ERROR"]
    print(
        f"\n{'=' * 78}\n{len(reports)} family(ies) audited · "
        f"{len(errors)} with errors\n{'=' * 78}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
