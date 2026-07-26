# Session-scoped API

Every `/api` endpoint operates on the workspace selected by the random
`agreementatlas_session` cookie. Mutations require:

```http
X-AgreementAtlas-Request: 1
```

No endpoint enables CORS or returns an absolute workspace path.

## `GET /api/status`

Returns:

- session expiry and quota use;
- uploaded instrument metadata;
- baseline/deep graph availability;
- `build.mode`, `stage`, completed/total work, schema version and rebuild flag;
- enrichment job state; and
- genuinely loaded, allowlisted LM Studio extractor/embedder details.

The native LM Studio model list distinguishes downloaded models from loaded
instances. Embedding models are not offered in the chat-model selector.

## `POST /api/upload`

Accepts one or more `files` parts as `multipart/form-data`. The complete family
must stay within 12 files and 50 MB by default. Duplicate safe filenames are
rejected. Conversion and baseline schema-v3 construction happen in a staging
workspace and commit atomically.

Successful response: `201 Created`.

## `GET /api/graph?view=overview|detail`

Returns the schema-v3 browser projection while retaining the existing dynamic
canvas shape. Nodes include evidence-span IDs and `evidence_segments` used by
the Clause Anatomy inspector. Relationships are directed and carry evidence,
structured scope and resolution status.

Overview selects up to 60 operative rules; detail selects up to 180. Family,
instrument, precedence, amendment, definition and party/role nodes are
prioritised.

## `POST /api/enrich`

```json
{"model": "google/gemma-4-26b-a4b-qat"}
```

Starts or resumes a deep build and returns `202 Accepted`. The model must be
loaded and in the server allowlist. If configured auto-management is enabled,
model loading occurs server-side. Visitors cannot submit arbitrary model
identifiers.

Deep build processes the complete substantive clause set with a
source/model/prompt fingerprint, atomic checkpoints, cancellation checks,
exact-span validation and deterministic fallbacks. A globally bounded
semaphore protects local memory.

## `GET /api/enrich/status`

States are `idle`, `running`, `complete`, `error` or `cancelled`. The response
includes stage, completed/total work, extractor model and, when complete, rule,
fallback, failed-clause and embedding summaries.

## `POST /api/query`

```json
{
  "question": "Can an Affiliate use StreamFlow?",
  "model": "google/gemma-4-26b-a4b-qat"
}
```

The response preserves `answer`, `evidence`, `model` and `disclaimer` and adds:

- `graph_build_mode`;
- retrieval component flags for BM25, vector, directed graph and legal
  resolver; and
- `resolution_trace`, including candidate rules, applicable scope,
  controlling instrument/rule, legal basis/evidence, final status, definition
  decisions and unresolved warnings.

If no content-bearing term matches, the endpoint returns a deterministic
unresolved answer with no evidence and does not ask the LLM to speculate.
Conversation history is not persisted.

## `DELETE /api/session`

Marks a running deep build for cancellation, removes source files, canonical
records, embeddings, checkpoints and outputs, and expires the cookie
immediately.

## `GET /healthz`

Returns process health only:

```json
{"status": "ok"}
```

It does not imply that either configured LM Studio model is loaded.
