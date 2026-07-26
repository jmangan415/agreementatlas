---
name: query-graphrag
description: Query a local Microsoft GraphRAG knowledge base from Codex without making a new model API call. Use when the user asks about their uploaded documents, knowledge graph, GraphRAG sources, entities, relationships, communities, or says to search/query the graph.
---

# Query GraphRAG

Use this skill to retrieve evidence from the user's local GraphRAG workspace.
Codex performs the final reasoning and writing, so querying does not require an
OpenAI API key.

## Workspace

The default workspace is:

`/Users/johnmangan/projects/graphrag_quickstart/knowledge`

If the user identifies another GraphRAG root, pass it with `--root`.

## Workflow

1. Run the retrieval script with the user's question:

   ```bash
   python3 /Users/johnmangan/plugins/graphrag-chat/scripts/query_graph.py \
     "USER QUESTION" \
     --root /Users/johnmangan/projects/graphrag_quickstart/knowledge
   ```

2. Read the JSON result completely. It contains ranked evidence from
   clause-aware legal rules, exact clauses, available GraphRAG tables, and a
   fallback search over converted source text.
3. Answer using only supported evidence. For agreements, reconcile the exact
   clause, definitions, conditions, exceptions, product-specific schedules,
   amendments, and stated precedence rules.
4. Cite sources inline using the source/document names exposed in the evidence.
   If only GraphRAG record IDs are available, call them "graph records" rather
   than pretending they are page citations.
5. Clearly say when the graph does not contain enough evidence. Suggest a more
   specific query or re-indexing when appropriate.

## Agreement interpretation

- Never silently infer a product, licence model, order form, agreement version,
  territory, or date from an earlier unrelated question.
- Treat a product-specific rule as limited to its stated scope.
- Distinguish allocation, user access, assignment of an agreement, and transfer
  of software.
- When the question is generic, give the general rule and material
  product-specific exceptions. State which missing facts could change the
  answer.
- Prefer `legal_rule` and `legal_clause` evidence because they retain section
  paths and scope, but verify conclusions against the exact evidence text.
- End agreement answers with "This is a document interpretation, not legal
  advice."

## Query strategy

- Specific fact, person, organization, or relationship: use the default query.
- Broad themes or corpus summary: add `--limit 30`.
- Follow-up question: include the important subject from the prior turn in the
  query; the retrieval script itself is stateless.
- To inspect available files and index state, run:

  ```bash
  python3 /Users/johnmangan/plugins/graphrag-chat/scripts/query_graph.py --status \
    --root /Users/johnmangan/projects/graphrag_quickstart/knowledge
  ```

Do not run `graphrag query`; that command invokes a configured model provider
and may incur API charges. Do not request or expose API keys.

## Indexing boundary

This plugin makes querying subscription-friendly, but it does not turn a Codex
subscription into a reusable API credential. A full Microsoft GraphRAG rebuild
still requires a supported model/embedding provider. If only uploaded converted
text exists, the local clause-aware ingestion still provides legal rules,
clauses, and relationships without an API call. Microsoft GraphRAG community
tables and embeddings appear only after a provider-backed index is built.
