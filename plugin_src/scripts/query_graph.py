#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'’-]{1,}")
STOP = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "but",
    "can",
    "could",
    "does",
    "for",
    "from",
    "have",
    "into",
    "its",
    "more",
    "not",
    "that",
    "the",
    "their",
    "them",
    "there",
    "these",
    "they",
    "this",
    "those",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "your",
}

SYNONYMS = {
    "allocate": {"allocated", "allocation", "assign", "transfer"},
    "allocated": {"allocate", "allocation", "assign", "transfer"},
    "allocation": {"allocate", "allocated", "assign", "transfer"},
    "transfer": {"assign", "allocation", "allocate", "sublicense"},
    "party": {"affiliate", "customer", "contractor", "entity"},
    "license": {"licence", "software"},
    "licence": {"license", "software"},
    "cloud": {"service", "saas", "hosting"},
    "terminate": {"termination", "expire", "suspend"},
    "audit": {"inspect", "records", "compliance"},
}


def tokens(value: str) -> list[str]:
    return [x.lower() for x in TOKEN.findall(value) if x.lower() not in STOP]


def query_tokens(value: str) -> Counter:
    base = tokens(value)
    expanded = list(base)
    for word in base:
        expanded.extend(SYNONYMS.get(word, ()))
    return Counter(expanded)


def text_value(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return str(value)


def score(query: Counter, text: str) -> float:
    hay = Counter(tokens(text))
    overlap = sum(min(count, hay[word]) for word, count in query.items())
    phrase = " ".join(query) in text.lower() if query else False
    return overlap + (3 if phrase else 0)


def row_text(row: pd.Series) -> str:
    preferred = [
        "title",
        "name",
        "description",
        "summary",
        "content",
        "text",
        "human_readable_id",
        "source",
        "target",
        "type",
    ]
    keys = [key for key in preferred if key in row.index]
    if not keys:
        keys = list(row.index[:12])
    return " | ".join(
        f"{key}: {text_value(row[key])}" for key in keys if text_value(row[key])
    )


def parquet_evidence(root: Path, query: Counter, limit: int) -> list[dict]:
    output = root / "output"
    candidates: list[dict] = []
    table_priority = {
        "entities": 1.4,
        "relationships": 1.5,
        "community_reports": 1.2,
        "text_units": 1.0,
        "documents": 0.9,
        "covariates": 1.0,
    }
    for path in output.glob("*.parquet"):
        table = path.stem
        if table not in table_priority:
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception:
            continue
        for index, row in frame.iterrows():
            rendered = row_text(row)
            relevance = score(query, rendered) * table_priority[table]
            if relevance:
                candidates.append(
                    {
                        "kind": table,
                        "record": text_value(row.get("id", index)),
                        "score": round(relevance, 2),
                        "evidence": rendered[:4000],
                    }
                )
    return sorted(candidates, key=lambda x: x["score"], reverse=True)[:limit]


def manual_graph_evidence(root: Path, query: Counter, limit: int) -> list[dict]:
    candidates: list[dict] = []
    for path in (
        root / "output" / "legal_relationship_graph.json",
        root / "output" / "relationship_graph.json",
    ):
        if not path.exists():
            continue
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        for node in nodes.values():
            rendered = (
                f"{node.get('label', '')} ({node.get('type', 'entity')}): "
                f"{node.get('description', '')} "
                f"[scope {node.get('scope', 'general')}; "
                f"source {node.get('source', path.name)}; "
                f"section {node.get('section', 'unknown')}]"
            )
            multiplier = 1.75 if node.get("type") == "rule" else 1.35
            relevance = score(query, rendered) * multiplier
            if relevance:
                candidates.append(
                    {
                        "kind": node.get("type", "entity"),
                        "record": node["id"],
                        "score": round(relevance, 2),
                        "evidence": rendered,
                    }
                )
        for index, edge in enumerate(graph.get("relationships", [])):
            source = nodes.get(edge.get("source"), {"label": edge.get("source")})
            target = nodes.get(edge.get("target"), {"label": edge.get("target")})
            rendered = (
                f"{source.get('label')} --{edge.get('type', edge.get('label'))}--> "
                f"{target.get('label')} [source section {edge.get('section', 'unknown')}]"
            )
            relevance = score(query, rendered) * 1.6
            if relevance:
                candidates.append(
                    {
                        "kind": "relationship",
                        "record": f"{path.stem}-{index + 1}",
                        "score": round(relevance, 2),
                        "evidence": rendered,
                    }
                )
    return sorted(candidates, key=lambda x: x["score"], reverse=True)[:limit]


def jsonl_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def legal_evidence(
    root: Path, query: Counter, limit: int, original_query: str = ""
) -> list[dict]:
    candidates: list[dict] = []
    for kind, filename, multiplier in (
        ("legal_rule", "rules.jsonl", 2.0),
        ("legal_clause", "clauses.jsonl", 1.55),
    ):
        for record in jsonl_records(root / "legal" / filename):
            rendered = " | ".join(
                f"{key}: {text_value(record.get(key))}"
                for key in (
                    "rule_type",
                    "actor",
                    "action",
                    "object",
                    "section_id",
                    "section_path",
                    "scope",
                    "source",
                    "conditions",
                    "evidence",
                    "text",
                )
                if text_value(record.get(key))
            )
            relevance = score(query, rendered) * multiplier
            if (
                re.search(
                    r"\b(what is|what does|define|definition|meaning of)\b",
                    original_query,
                    re.I,
                )
                and kind == "legal_rule"
                and record.get("rule_type") == "DEFINITION"
                and any(
                    word
                    in tokens(
                        f"{record.get('object', '')} {record.get('section_path', '')}"
                    )
                    for word in query
                )
            ):
                relevance += 12
            if relevance:
                candidates.append(
                    {
                        "kind": kind,
                        "record": record.get("id", ""),
                        "score": round(relevance, 2),
                        "evidence": rendered[:6000],
                    }
                )
    ranked = sorted(candidates, key=lambda item: item["score"], reverse=True)
    selected: list[dict] = []
    selected_ids: set[str] = set()

    # Agreement questions often have a general EULA rule plus narrower schedule
    # rules. Reserve evidence from each document so repeated model-specific
    # wording cannot crowd the general rule out of a small result set.
    documents: dict[str, list[dict]] = {}
    for item in ranked:
        match = re.search(r"(?:document_id|source): ([^|]+)", item["evidence"])
        document = match.group(1).strip() if match else "unknown"
        documents.setdefault(document, []).append(item)
    per_document = max(1, min(3, limit // max(1, len(documents))))
    for items in documents.values():
        for item in items[:per_document]:
            if item["record"] not in selected_ids:
                selected.append(item)
                selected_ids.add(item["record"])
    for item in ranked:
        if len(selected) >= limit:
            break
        if item["record"] not in selected_ids:
            selected.append(item)
            selected_ids.add(item["record"])
    return sorted(selected, key=lambda item: item["score"], reverse=True)[:limit]


def source_evidence(
    root: Path, query: Counter, limit: int, original_query: str = ""
) -> list[dict]:
    legal = legal_evidence(root, query, limit, original_query)
    if legal:
        return legal
    candidates: list[dict] = []
    for path in (root / "input").glob("*.txt"):
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = re.split(r"\n\s*\n", text)
        for position, chunk in enumerate(chunks):
            relevance = score(query, chunk)
            if relevance:
                candidates.append(
                    {
                        "kind": "source_text",
                        "source": path.name,
                        "section": position + 1,
                        "score": relevance,
                        "evidence": chunk[:4000],
                    }
                )
    return sorted(candidates, key=lambda x: x["score"], reverse=True)[:limit]


def workspace_status(root: Path) -> dict:
    sources = root / "sources"
    output = root / "output"
    return {
        "root": str(root),
        "source_files": sorted(p.name for p in sources.glob("*") if p.is_file()),
        "converted_files": sorted(p.name for p in (root / "input").glob("*.txt")),
        "graph_tables": sorted(p.name for p in output.glob("*.parquet")),
        "graph_index_available": (output / "entities.parquet").exists(),
        "local_relationship_graph": (output / "relationship_graph.json").exists(),
        "legal_graph_available": (output / "legal_relationship_graph.json").exists(),
        "legal_clause_count": len(jsonl_records(root / "legal" / "clauses.jsonl")),
        "legal_rule_count": len(jsonl_records(root / "legal" / "rules.jsonl")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if args.status:
        print(json.dumps(workspace_status(root), indent=2))
        return
    if not args.query:
        parser.error("query is required unless --status is used")
    query = query_tokens(args.query)
    graph = manual_graph_evidence(root, query, args.limit)
    graph.extend(parquet_evidence(root, query, args.limit))
    graph = sorted(graph, key=lambda x: x["score"], reverse=True)[: args.limit]
    sources = source_evidence(
        root, query, max(4, args.limit // 2), original_query=args.query
    )
    print(
        json.dumps(
            {
                "query": args.query,
                "workspace": workspace_status(root),
                "graph_evidence": graph,
                "source_evidence": sources,
                "note": "Codex should synthesize an answer from this evidence.",
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
