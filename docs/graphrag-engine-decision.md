# GraphRAG engine decision

## Decision

AgreementAtlas uses its own legal-ontology graph retriever as the primary local
engine. Microsoft GraphRAG is the first optional comparison adapter. A graph
database or another general GraphRAG framework should be introduced only when
evaluation shows a concrete improvement or the hosted architecture requires
it.

This is not a decision to “draw our own graph and use ordinary RAG.” The
default retriever performs bounded multi-hop traversal across exact-evidence,
scope, qualification, precedence, amendment, exception and condition edges.
The traversed path is returned with graph-expanded evidence.

## Requirements that drive the choice

- Run privately with LM Studio and no cloud API.
- Preserve a contract-specific ontology rather than generic named entities.
- Retain exact clause provenance for every generated answer.
- Be usable from a small open-source checkout without a graph database.
- Delete all derived records with the visitor session.
- Support orders, schedules and addenda that alter a general agreement.
- Leave a credible route to larger, professional deployments.

## Options considered

| Engine | Where it is strongest | Fit for the local release | Decision |
|---|---|---|---|
| AgreementAtlas graph retriever | Controlled legal schema, exact provenance, zero extra service, transparent traversal | Strongest fit; already implemented and tested | Primary |
| Microsoft GraphRAG | Entity/community extraction, Local Search, DRIFT and corpus-wide community reports | Valuable, but indexing and embeddings add time and model requirements; its generic extraction should not replace the legal schema | Optional BYOG adapter and benchmark |
| Neo4j GraphRAG | Durable property graph, vector/Cypher retrievers and production graph queries | Strong hosted option, but a Neo4j service is unnecessary friction for the local portfolio release | Reconsider for multi-tenant hosting |
| LightRAG | Lightweight local/global graph retrieval and multiple storage/provider options | Promising experiment, but would duplicate legal extraction and requires careful exact-clause round-tripping | Benchmark only if Microsoft underperforms |
| Graphiti | Temporal facts, contradiction handling and evolving agent memory | Interesting for effective dates and amendment history, but broader than a static uploaded agreement family | Possible temporal-graph research path |

Primary project references:

- [Microsoft GraphRAG overview](https://microsoft.github.io/graphrag/index/overview/)
- [Microsoft GraphRAG custom graphs](https://microsoft.github.io/graphrag/index/byog/)
- [Microsoft GraphRAG query modes](https://microsoft.github.io/graphrag/query/overview/)
- [Neo4j GraphRAG Python RAG guide](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html)
- [LightRAG reference implementation](https://github.com/HKUDS/LightRAG)
- [Graphiti reference implementation](https://github.com/getzep/graphiti)

## Why Microsoft is additive

Microsoft's bring-your-own-graph route lets AgreementAtlas export its canonical
documents, rules, relationships and exact clause text instead of asking a
generic model to rediscover the contract structure. Microsoft can then create
communities, community reports and embeddings for Local or DRIFT retrieval.

That division of responsibility is attractive:

- AgreementAtlas owns legal meaning, scope and evidence fidelity.
- Microsoft GraphRAG can add community/global retrieval strategies.
- A hybrid ranker can compare or fuse candidate clauses.
- The answer layer still rejects any candidate that cannot map back to an
  exact AgreementAtlas clause.

See `microsoft-graphrag-integration.md` for the proposed table projection and
evaluation gate.

## Evaluation gate

No engine should become the default because of popularity or a compelling
demo. Compare engines on a permissioned contract test set and record:

- exact-clause recall;
- controlling-document recall;
- amendment/precedence recall;
- unsupported-claim rate;
- index time and model calls;
- query latency and context size;
- local memory/storage requirements; and
- deletion and provenance guarantees.

The built-in engine remains the default unless another option improves
controlling-evidence recall without weakening source fidelity or local
privacy.
