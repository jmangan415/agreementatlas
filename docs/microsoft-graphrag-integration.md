# Optional Microsoft GraphRAG retrieval adapter

AgreementAtlas has its own graph-aware legal retrieval path and does not require
Microsoft GraphRAG. Microsoft’s query engine can nevertheless be used as an
optional second retriever over AgreementAtlas’s canonical legal graph.

This is a useful integration boundary because it preserves AgreementAtlas’s specialist
agreement parsing and evidence controls while allowing a measured comparison
with Microsoft’s Local Search, DRIFT and community-report approaches.

## Why “bring your own graph” is the right route

Microsoft GraphRAG’s official custom-graph workflow accepts:

- `entities.parquet` for graph nodes;
- `relationships.parquet` for graph edges; and
- optionally `text_units.parquet` for source chunks.

The documentation states that Local, DRIFT and Basic Search also require text
units and embeddings. That maps cleanly to AgreementAtlas:

| AgreementAtlas canonical record | Microsoft GraphRAG projection |
|---|---|
| document, rule, scope and party nodes | entities |
| legal relationships | relationships |
| exact clause records | text units |
| clause/document/rule IDs | stable source mappings |

Sources:

- [Microsoft GraphRAG: Bring Your Own Graph](https://microsoft.github.io/graphrag/index/byog/)
- [Microsoft GraphRAG output schemas](https://microsoft.github.io/graphrag/index/outputs/)
- [Microsoft GraphRAG Local Search](https://microsoft.github.io/graphrag/query/local_search/)

## Proposed exporter

Add an optional `ms_graphrag_adapter.py` in a later milestone. It should read
only a visitor’s existing AgreementAtlas JSON/JSONL output and write:

```text
data/sessions/<session-id>/ms_graphrag/
  output/
    entities.parquet
    relationships.parquet
    text_units.parquet
  lancedb/
  cache/
  logs/
```

Everything remains inside the same expiring visitor workspace and is deleted
by the existing session lifecycle.

Projection rules:

- Use deterministic UUIDv5 values derived from AgreementAtlas IDs so repeated exports
  are stable.
- Keep the AgreementAtlas ID in projection metadata for citation round-tripping.
- Use exact clause text as each text unit; never replace it with an LLM
  summary.
- Link a clause text unit to its document, clause, deterministic rules,
  enriched rules and scope entities.
- Use the controlled AgreementAtlas relationship label as the edge description.
- Start relationship weight at `1.0`; use `3.0` for `OVERRIDES`, `AMENDS`,
  `SUPERSEDES` and `CONTROLS_FOR_DEFINED_SCOPE`, and `2.0` for `QUALIFIES`,
  `EXCEPTION_TO` and `CONDITIONED_ON`. Treat these as clustering weights, not
  legal priority.
- Preserve source filename, section, scope and evidence in AgreementAtlas metadata even
  if Microsoft’s query context omits them.

The exporter must have contract tests against the installed GraphRAG version’s
table schemas. GraphRAG uses breaking-version migrations, so the adapter should
pin a compatible minor version and fail with a clear message on schema drift.

## Minimal Microsoft workflows

The custom-graph documentation recommends skipping text chunking and graph
extraction because AgreementAtlas has already done them. For community summaries:

```yaml
workflows:
  - create_communities
  - create_community_reports
```

For Local, DRIFT or Basic Search, add:

```yaml
  - generate_text_embeddings
```

This avoids paying an LLM to rediscover a weaker generic graph from agreement
text.

## Local-model route

GraphRAG 3.1 uses provider factories for chat and embeddings, and the official
architecture allows custom `chat` and `embed` implementations. The local
adapter should use:

- the existing LM Studio chat endpoint for completion; and
- a separately loaded LM Studio embedding model for entity/text-unit
  embeddings.

LM Studio exposes OpenAI-compatible chat and embedding endpoints, but the exact
GraphRAG provider configuration must be integration-tested rather than assumed.
If the built-in LiteLLM wrapper cannot target both local models cleanly, register
a small provider implementation using the GraphRAG model factory.

Sources:

- [Microsoft GraphRAG provider architecture](https://microsoft.github.io/graphrag/index/architecture/)
- [LM Studio OpenAI-compatible endpoints](https://lmstudio.ai/docs/developer/openai-compat)

## Query integration

Do not let Microsoft GraphRAG write the final browser response directly.
Introduce a retriever protocol:

```python
class EvidenceRetriever(Protocol):
    def retrieve(self, root: Path, question: str, limit: int) -> list[Evidence]: ...
```

Implementations:

- `AgreementAtlasGraphRetriever` — current deterministic + graph-traversal retriever;
- `MicrosoftLocalRetriever` — Microsoft Local Search context/candidates;
- `MicrosoftDriftRetriever` — optional broader multi-hop exploration; and
- `HybridRetriever` — reciprocal-rank fusion of AgreementAtlas and Microsoft candidates.

Every Microsoft candidate must round-trip to a AgreementAtlas clause ID. The final
answer continues through `legal_graph_service.answer_question()`, using exact
AgreementAtlas clauses and the existing “not legal advice” behavior. Drop any result
that cannot be mapped to exact evidence.

Expose the experiment only through server configuration:

```text
AGREEMENTATLAS_RETRIEVER=agreementatlas
AGREEMENTATLAS_RETRIEVER=microsoft-local
AGREEMENTATLAS_RETRIEVER=hybrid
```

Do not let anonymous browsers select arbitrary providers.

## Evaluation gate

Before making Microsoft retrieval visible in the UI, compare it against the
current retriever on the fictional family and a permissioned evaluation set:

- general permission plus product-specific prohibition;
- definition in one document used by another;
- order/DPA/master precedence;
- amendment of an earlier clause;
- service-level obligation and exclusion;
- assignment versus affiliate access; and
- a question with no supporting clause.

Score exact-clause recall, controlling-document recall, unsupported claims,
latency, model calls and context size. Keep Microsoft retrieval optional unless
it improves controlling-evidence recall without weakening citation fidelity.
