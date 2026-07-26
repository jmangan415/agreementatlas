"""Parse-health diagnostics for the deterministic agreement parser.

The deterministic layer is regex-driven, so it fails silently: a document whose
numbering style is unsupported still produces clauses, rules and a graph -- they
are simply wrong. Every defect found so far was invisible in the summary counts
and obvious in these ratios.

Run it over a tree of real agreements before trusting a build:

    ./.venv/bin/python scripts/parse_health.py --root knowledge/sources
    ./.venv/bin/python scripts/parse_health.py --root knowledge/sources --json

Each immediate subdirectory is treated as one agreement family; loose files at
the top level form a further family. Nothing is written to the repository -- each
family is parsed in a temporary workspace.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legal_ingest import (  # noqa: E402
    DEFINITION_START,
    SUPPORTED,
    rebuild_workspace,
)

# Language that should have produced a structured record. A document containing
# these phrases but yielding no corresponding records has a detection gap.
PRECEDENCE_LANGUAGE = re.compile(
    r"\b(?:conflict|inconsisten\w*)\b.{0,160}?"
    r"\b(?:prevail\w*|control\w*|govern\w*|take[s]? precedence|priority)\b",
    re.I | re.S,
)
# Matches that look like precedence but cannot yield an instrument rule: choice-of-law
# boilerplate, ordering between parts of one document, and third-party licences
# reproduced inside an agreement (whose "these terms will control" is about the
# embedded licence, not about a sibling instrument).
NOT_INSTRUMENT_PRECEDENCE = re.compile(
    r"conflict of laws"
    r"|\b(?:section|clause|paragraph|sub-?section|article)\s+\d"
    r"|\bthis section\b|\bmain body of\b",
    re.I,
)
CROSS_REFERENCE_LANGUAGE = re.compile(
    r"\b(?:subject to|in accordance with|as (?:defined|set out|described) in|"
    r"pursuant to)\s+(?:section|clause|schedule|appendix|annex|exhibit|the)\b",
    re.I,
)
# PDF extraction sometimes splits words ("L icensee must d eliver").
MANGLED_SPACING = re.compile(r"\b[A-Za-z]\s[a-z]{2,}\b")
TITLE_NOT_A_TITLE = re.compile(
    r"^(?:v(?:ersion)?[\s.]*[\d.]+|[\d.]+|page\s+\d+|contents|confidential"
    r"|(?:january|february|march|april|may|june|july|august|september|october"
    r"|november|december)\s+\d{4})$",
    re.I,
)


class Report:
    """Collects PASS/WARN/FAIL findings for one scope."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.findings: list[tuple[str, str, str]] = []
        self.metrics: dict[str, object] = {}

    def check(self, level: str, ok: bool, label: str, detail: str) -> None:
        if not ok:
            self.findings.append((level, label, detail))

    @property
    def worst(self) -> str:
        levels = {level for level, _, _ in self.findings}
        if "FAIL" in levels:
            return "FAIL"
        return "WARN" if "WARN" in levels else "PASS"


def families(root: Path) -> list[tuple[str, list[Path]]]:
    groups: list[tuple[str, list[Path]]] = []
    loose = [
        path
        for path in sorted(root.iterdir())
        if path.is_file() and path.suffix.lower() in SUPPORTED
    ]
    if loose:
        groups.append((root.name, loose))
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        found = [
            path
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED
        ]
        if found:
            groups.append((f"{root.name}/{directory.name}", found))
    return groups


def build(
    paths: list[Path], classifier=None
) -> tuple[Path, dict, tempfile.TemporaryDirectory]:
    handle = tempfile.TemporaryDirectory(prefix="parse-health-")
    workspace = Path(handle.name)
    sources = workspace / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    for path in paths:
        # Flatten, keeping the parent directory to avoid basename collisions.
        target = sources / f"{path.parent.name}__{path.name}"
        target.write_bytes(path.read_bytes())
    summary = rebuild_workspace(workspace, classifier=classifier)
    return workspace, summary, handle


def load(workspace: Path, name: str) -> list[dict]:
    path = workspace / "legal" / f"{name}.jsonl"
    if not path.exists():
        return []
    output = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            output.append(json.loads(line))
    return output


def inspect_instrument(
    instrument: dict,
    clauses: list[dict],
    definitions: list[dict],
    rules: list[dict],
    raw_text: str,
) -> Report:
    report = Report(instrument.get("source", "?"))
    total = len(clauses)
    preamble = sum(
        1 for c in clauses if str(c.get("section_id", "")).startswith("Preamble")
    )
    lengths = sorted(len(str(c.get("text", ""))) for c in clauses) or [0]
    packed = [
        c for c in clauses if len(DEFINITION_START.findall(str(c.get("text", "")))) > 1
    ]
    definition_language = len(DEFINITION_START.findall(raw_text))
    precedence_language = len(PRECEDENCE_LANGUAGE.findall(raw_text))
    crossref_language = len(CROSS_REFERENCE_LANGUAGE.findall(raw_text))
    mangled = len(MANGLED_SPACING.findall(raw_text))
    title = str(instrument.get("title", ""))

    report.metrics = {
        "clauses": total,
        "preamble_ratio": round(preamble / total, 3) if total else 1.0,
        "median_clause_chars": statistics.median(lengths),
        "max_clause_chars": lengths[-1],
        "definitions": len(definitions),
        "definition_language_hits": definition_language,
        "packed_definition_clauses": len(packed),
        "rules": len(rules),
        "rules_without_object": sum(1 for r in rules if not r.get("object")),
        "rules_without_actor": sum(1 for r in rules if not r.get("actor")),
        "precedence_language_hits": precedence_language,
        "crossref_language_hits": crossref_language,
        "mangled_spacing_hits": mangled,
        "instrument_class": instrument.get("instrument_class"),
        "instrument_type": instrument.get("instrument_type"),
        "effective_date": instrument.get("effective_date"),
        "title": title,
    }

    report.check(
        "FAIL",
        total > 0,
        "no clauses",
        "the document produced no clauses at all",
    )
    report.check(
        "FAIL",
        report.metrics["preamble_ratio"] <= 0.25,
        "section numbering not detected",
        f"{preamble}/{total} clauses fell back to 'Preamble' -- the heading style "
        "is probably unsupported",
    )
    report.check(
        "FAIL",
        not TITLE_NOT_A_TITLE.match(title.strip()),
        "title is not a title",
        f"extracted title {title!r} looks like a date, version or page marker; "
        "this poisons every instrument cross-reference",
    )
    report.check(
        "WARN",
        bool(title.strip()) and len(title.strip()) > 3,
        "title too short",
        f"extracted title {title!r}",
    )
    report.check(
        "WARN",
        # Published clickwrap terms (master terms, annexes, policies) frequently
        # carry no effective date at all -- that is the document, not a parse
        # failure. Negotiated instruments are different: an order or an amendment
        # without a date cannot be placed in a version chain.
        bool(instrument.get("effective_date"))
        or str(instrument.get("instrument_class")) not in {"ORDER", "AMENDMENT"},
        "no effective date on a dated instrument",
        f"{instrument.get('instrument_class')} instruments need a parsed date for "
        "amendment ordering and version chains",
    )
    report.check(
        "FAIL",
        not (definition_language >= 3 and not definitions),
        "definitions not extracted",
        f"text contains {definition_language} '\"X\" means' patterns but "
        f"{len(definitions)} definitions were recorded",
    )
    report.check(
        "FAIL",
        not packed,
        "definition blocks left unsplit",
        f"{len(packed)} clause(s) still contain more than one definition; only the "
        "first in each is recognised",
    )
    report.check(
        "WARN",
        report.metrics["max_clause_chars"] < 4000,
        "oversized clause",
        f"longest clause is {lengths[-1]} chars, which usually means a block was "
        "never split",
    )
    report.check(
        "WARN",
        not (total and (sum(1 for x in lengths if x < 60) / total) > 0.25),
        "many tiny clauses",
        "more than a quarter of clauses are under 60 characters, suggesting the "
        "text was fragmented",
    )
    if rules:
        no_object = report.metrics["rules_without_object"] / len(rules)
        report.check(
            "WARN",
            no_object < 0.6,
            "rules lack an object",
            f"{no_object:.0%} of rules have no identified object, so they cannot be "
            "matched against competing rules",
        )
    report.check(
        "WARN",
        mangled < 40,
        "damaged text extraction",
        f"{mangled} split-word artefacts (e.g. 'L icensee'); exact-span evidence "
        "validation may reject valid quotes",
    )
    return report


def inspect_family(
    name: str, workspace: Path, summary: dict, raw_by_source: dict[str, str]
) -> tuple[Report, list[Report]]:
    report = Report(name)
    instruments = load(workspace, "instruments")
    clauses = load(workspace, "clauses")
    definitions = load(workspace, "defined_terms")
    rules = load(workspace, "operative_rules")
    precedence = load(workspace, "precedence_rules")
    graph_path = workspace / "output" / "legal_relationship_graph.json"
    graph = (
        json.loads(graph_path.read_text(encoding="utf-8"))
        if graph_path.exists()
        else {}
    )
    edges = Counter(str(e.get("type")) for e in graph.get("relationships", []))

    by_instrument: defaultdict[str, list[dict]] = defaultdict(list)
    for clause in clauses:
        by_instrument[str(clause.get("document_id"))].append(clause)
    definitions_by: defaultdict[str, list[dict]] = defaultdict(list)
    for item in definitions:
        definitions_by[
            str(item.get("instrument_id") or item.get("document_id"))
        ].append(item)
    rules_by: defaultdict[str, list[dict]] = defaultdict(list)
    for item in rules:
        rules_by[str(item.get("document_id"))].append(item)

    per_instrument = [
        inspect_instrument(
            instrument,
            by_instrument[str(instrument.get("id"))],
            definitions_by[str(instrument.get("id"))],
            rules_by[str(instrument.get("id"))],
            raw_by_source.get(str(instrument.get("source")), ""),
        )
        for instrument in instruments
    ]

    all_text = "\n".join(raw_by_source.values())
    # Only cross-instrument precedence is this tool's job. "This Section 3 ...
    # prevails over any contradicting terms in Section 2" is an internal ordering
    # and produces no instrument rule however well it is parsed, so counting it
    # as an extraction failure reports a defect that does not exist.
    precedence_language = sum(
        1
        for match in PRECEDENCE_LANGUAGE.finditer(all_text)
        if not NOT_INSTRUMENT_PRECEDENCE.search(
            all_text[match.start() : match.end() + 120]
        )
    )
    terms = defaultdict(set)
    for item in definitions:
        terms[str(item.get("term", "")).lower()].add(
            item.get("instrument_id") or item.get("document_id")
        )
    competing = [term for term, owners in terms.items() if len(owners) > 1]
    legal_edges = sum(
        edges[key]
        for key in (
            "OVERRIDES",
            "QUALIFIES",
            "AMENDS",
            "SUPERSEDES",
            "CONTROLS_FOR_DEFINED_SCOPE",
            "REDEFINES",
            "CONTROLLING_DEFINITION",
            "EXCEPTION_TO",
        )
    )

    report.metrics = {
        "documents": len(instruments),
        "clauses": len(clauses),
        "definitions": len(definitions),
        "competing_definitions": len(competing),
        "rules": len(rules),
        "precedence_rules": len(precedence),
        "precedence_language_hits": precedence_language,
        "legal_edges": legal_edges,
        "edges_per_rule": round(legal_edges / len(rules), 2) if rules else 0.0,
        "edge_types": dict(edges),
        "summary": summary,
    }

    report.check(
        # A warning, not a failure: some agreements genuinely rank nothing, and
        # conflict wording appears in clauses that state no instrument ordering.
        # This flags a family worth reading, not a proven parser defect.
        "WARN",
        not (precedence_language >= 1 and not precedence),
        "precedence language not converted to rules",
        f"{precedence_language} clause(s) contain conflict/prevail language but "
        "0 precedence rules were extracted",
    )
    report.check(
        "WARN",
        not (len(instruments) > 1 and not competing),
        "no competing definitions found",
        "a multi-instrument family that redefines nothing is unusual; term "
        "extraction may be incomplete",
    )
    report.check(
        "WARN",
        report.metrics["edges_per_rule"] < 5,
        "relationship cross-product",
        f"{report.metrics['edges_per_rule']} legal edges per rule suggests rules are "
        "being paired on too coarse a key",
    )
    report.check(
        "WARN",
        len(instruments) < 2 or legal_edges > 0,
        "no legal relationships",
        "a multi-document family produced no override/qualification/definition edges",
    )
    skipped = summary.get("skipped") or []
    report.check(
        "WARN",
        not skipped,
        "documents skipped",
        "; ".join(f"{item['file']} ({item['reason']})" for item in skipped),
    )
    return report, per_instrument


def render(report: Report, children: list[Report], verbose: bool) -> None:
    badge = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[report.worst]
    print(f"\n{'=' * 78}\n[{badge}] FAMILY {report.name}\n{'=' * 78}")
    metrics = report.metrics
    print(
        f"  documents={metrics['documents']} clauses={metrics['clauses']} "
        f"rules={metrics['rules']} definitions={metrics['definitions']} "
        f"(competing={metrics['competing_definitions']})"
    )
    print(
        f"  precedence_rules={metrics['precedence_rules']} "
        f"(language hits={metrics['precedence_language_hits']}) "
        f"legal_edges={metrics['legal_edges']} "
        f"per_rule={metrics['edges_per_rule']}"
    )
    if verbose:
        print(f"  edge_types={metrics['edge_types']}")
    for level, label, detail in report.findings:
        print(f"    [{level}] {label}: {detail}")

    for child in children:
        print(f"\n  --- [{child.worst}] {child.name}")
        m = child.metrics
        print(
            f"      {m['instrument_class']}/{m['instrument_type']} "
            f"title={m['title']!r} effective={m['effective_date'] or '-'}"
        )
        print(
            f"      clauses={m['clauses']} preamble={m['preamble_ratio']:.0%} "
            f"median={m['median_clause_chars']}c max={m['max_clause_chars']}c "
            f"definitions={m['definitions']}/{m['definition_language_hits']} "
            f"rules={m['rules']}"
        )
        for level, label, detail in child.findings:
            print(f"        [{level}] {label}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("knowledge/sources"))
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--classify-model",
        default="",
        help=(
            "LM Studio model id used to classify documents the deterministic rules "
            "cannot place. One call per document."
        ),
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    classifier = None
    if args.classify_model:
        from legal_graph_service import lm_instrument_classifier
        from lmstudio_client import LMStudioClient

        classifier = lm_instrument_classifier(LMStudioClient(), args.classify_model)

    groups = families(root)
    if not groups:
        print(f"no supported documents under {root}", file=sys.stderr)
        return 2

    payload: list[dict] = []
    worst = "PASS"
    for name, paths in groups:
        if not args.json:
            print(f"parsing {name} ({len(paths)} documents)...", file=sys.stderr)
        workspace, summary, handle = build(paths, classifier)
        try:
            raw_dir = workspace / "legal" / "raw"
            raw_by_source: dict[str, str] = {}
            for path in raw_dir.glob("*.txt") if raw_dir.exists() else []:
                raw_by_source[f"{path.stem}{_suffix_for(workspace, path.stem)}"] = (
                    path.read_text(encoding="utf-8", errors="replace")
                )
            family, children = inspect_family(name, workspace, summary, raw_by_source)
            if args.json:
                payload.append(
                    {
                        "family": name,
                        "status": family.worst,
                        "metrics": family.metrics,
                        "findings": [
                            {"level": lvl, "label": lab, "detail": det}
                            for lvl, lab, det in family.findings
                        ],
                        "instruments": [
                            {
                                "source": child.name,
                                "status": child.worst,
                                "metrics": child.metrics,
                                "findings": [
                                    {"level": lvl, "label": lab, "detail": det}
                                    for lvl, lab, det in child.findings
                                ],
                            }
                            for child in children
                        ],
                    }
                )
            else:
                render(family, children, args.verbose)
            statuses = [family.worst, *(child.worst for child in children)]
            if "FAIL" in statuses:
                worst = "FAIL"
            elif "WARN" in statuses and worst != "FAIL":
                worst = "WARN"
        finally:
            handle.cleanup()

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"\n{'=' * 78}\noverall: {worst}\n{'=' * 78}")
    return 1 if worst == "FAIL" else 0


def _suffix_for(workspace: Path, stem: str) -> str:
    for candidate in (workspace / "sources").iterdir():
        if candidate.stem == stem:
            return candidate.suffix
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
