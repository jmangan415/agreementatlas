# AgreementAtlas

AgreementAtlas is a local-first legal GraphRAG application for understanding a
related family of software and cloud agreements. Upload master terms, EULAs,
order schedules, amendments, DPAs, SLAs and support policies; AgreementAtlas
builds a navigable legal-rule graph and answers questions with citations to
exact clauses.

The first release runs inference through
[LM Studio](https://lmstudio.ai/docs/developer/core/server), so agreement text
does not need to leave the machine running AgreementAtlas.

> **Alpha software — not legal advice.** Automated extraction and language
> models can be incomplete or wrong. Exact agreement text remains the source of
> truth. Do not use AgreementAtlas as the sole basis for a legal, financial or
> operational decision.

## Why this is not generic document chat

Software and cloud rights rarely live in one document. A master agreement may
grant a general right, an order may narrow it for one product, and an addendum
may control for personal-data processing. AgreementAtlas preserves that structure:

- two-level instrument class/type, versions and validated ISO dates;
- clause/list hierarchy and separately grounded chapeau/item evidence;
- parties, contractual roles, conflicting definitions and structured scope;
- effect, modality, polarity, actor, action, object, conditions and carve-outs;
- evidence-derived amendment, precedence and qualification relationships; and
- deterministic rules that are available before an LLM is called.

The application then uses LM Studio for optional structured enrichment,
embeddings and evidence-grounded answers. Validated model rules replace the
deterministic fallback for the same clause, but never replace or rewrite source
evidence.

## What makes this GraphRAG

AgreementAtlas does not simply draw a graph after ordinary document search. Its
default `AgreementAtlasGraphRetriever` uses a hybrid pipeline:

1. rank schema-v3 rules, definitions and clauses with BM25 and optional Nomic
   embeddings, fused by reciprocal-rank fusion;
2. follow relationship-specific incoming/outgoing edges without traversing
   generic document, party, scope or `CONTAINS` hubs;
3. resolve controlling definitions, scope, exceptions, amendments and scoped
   instrument precedence;
4. return a typed legal-resolution trace plus exact evidence; and
5. give only that evidence and deterministic trace to LM Studio for prose.

The API identifies the retrieval engine and exposes graph relationships and
node paths on graph-expanded evidence. Microsoft GraphRAG remains an optional
BYOG retriever for later measured comparison, not a prerequisite for calling
the local pipeline GraphRAG.

## Architecture at a glance

```text
Browser
  │  opaque six-hour session cookie
  ▼
AgreementAtlas HTTP server
  ├─ data/sessions/<random-id>/sources
  ├─ deterministic agreement parser
  ├─ canonical schema-v3 legal JSONL records
  ├─ BM25/vector retrieval + directed legal resolver
  ├─ session-local float32 embedding store
  └─ server-side LM Studio client ──► 127.0.0.1:1234
```

The browser runtime does **not** depend on Microsoft GraphRAG. The earlier
Microsoft GraphRAG configuration remains under `knowledge/` as an optional
research/interoperability layer, and `graphrag` is an optional dependency.
See [Architecture](docs/architecture.md) for the exact boundary.

## Current capabilities

- Isolated visitor workspaces with an absolute six-hour expiry
- Maximum 12 files and 50 MB per session
- PDF, DOCX, XLS/XLSX, PPTX, CSV, Markdown and text ingestion
- Filename sanitisation, type checks and Office archive expansion limits
- Atomic ingestion: an unreadable upload leaves the existing session unchanged
- Immediate deterministic legal graph
- Resumable complete-family deep indexing with deterministic fallbacks
- Evidence-backed scoped precedence, definition conflicts and amendment chains
- BM25 fallback plus Nomic vector retrieval and reciprocal-rank fusion
- Typed legal-resolution traces showing which rule controls and why
- Interactive canvas graph with pan, zoom, dragging, search and type filters
- “Clause Anatomy” view for effect, modality, polarity and exact source spans
- Overview and detailed legal-rule views
- Optional JSON-schema rule enrichment through LM Studio
- Evidence-grounded chat with cited agreement text
- Auditable graph-expanded evidence paths
- Immediate “Delete my documents” control
- Content Security Policy, safe text rendering, CSRF guard and request limits
- Public-demo mode that fails closed until privacy and secure-cookie settings exist
- Fictional, redistributable example agreement family

## Quick start

Requirements:

- Python 3.11 or 3.12
- LM Studio 0.4+ with its local server enabled
- Gemma 4 26B-A4B (tested extractor) and Nomic Embed Text v1.5 (tested
  embedding baseline), or configured alternatives

```bash
git clone https://github.com/jmangan415/agreementatlas.git
cd agreementatlas

python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e .

cp .env.example .env
lms server start --port 1234
lms load google/gemma-4-26b-a4b-qat --context-length 32768
lms load text-embedding-nomic-embed-text-v1.5 --context-length 2048
./.venv/bin/python app.py
```

Open <http://127.0.0.1:8000>, then upload the six fictional Markdown agreements
under `samples/`.
Deterministic nodes appear immediately. Load a model in LM Studio to enable
structured enrichment and chat.

The application server—not browser JavaScript—calls LM Studio. Keep LM Studio
bound to `127.0.0.1`; browser CORS is unnecessary.

## Storage

`APP_MODE=local` (the default) keeps everything. Each agreement family is a
directory under `data/library/`, holding the uploaded sources, the deterministic
records and any LLM enrichment. Families are listed in the sidebar, are created
explicitly, and persist across restarts until you delete them.

Adding a document to a family re-runs the deterministic build but **keeps rules
already extracted by the model**: clause identity is derived from the document,
not from family membership, so only the new document's clauses are enriched.
Removing a document removes what was extracted from it.

`APP_MODE=public-demo` gives each visitor an ephemeral, cookie-isolated family
library of their own: the shipped sample bundles arrive pre-enriched and
read-only, the visitor can create a few families beside them, and everything
expires together after `SESSION_TTL_HOURS`. `data/` is gitignored; never
commit it.

## Configuration

The safe default is `APP_MODE=local`. Relevant values are documented in
[`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---:|---|
| `LIBRARY_ROOT` | `data/library` | Where local mode stores agreement families (persistent) |
| `SESSION_TTL_HOURS` | `6` | Visitor-workspace lifetime, **public-demo only** |
| `MAX_FILES_PER_SESSION` | `12` | Agreement-family file limit |
| `MAX_SESSION_BYTES` | `52428800` | Total source-file allowance |
| `LMSTUDIO_BASE_URL` | `http://127.0.0.1:1234/v1` | Server-side model endpoint |
| `LMSTUDIO_MODEL` | `google/gemma-4-26b-a4b-qat` | Allowlisted extractor |
| `LMSTUDIO_EMBEDDING_MODEL` | Nomic Embed Text v1.5 | Allowlisted embedder |
| `LMSTUDIO_NO_THINK_MODELS` | `qwen3.6-27b-mlx` | Exact model IDs receiving Qwen's `/no_think` soft switch |
| `LMSTUDIO_AUTOMANAGE_MODELS` | `false` | Let the server load configured models |
| `LMSTUDIO_BATCH_CLAUSES` | `1` | Maximum clauses per resumable batch |
| `LMSTUDIO_BATCH_CHARS` | `12000` | Conservative batch character budget |
| `LMSTUDIO_MAX_CONCURRENT_JOBS` | `1` | Global inference concurrency |

AgreementAtlas reads `/api/v1/models`, so it distinguishes downloaded models
from genuinely loaded instances. Anonymous visitors cannot choose arbitrary
model identifiers. If auto-management is enabled, AgreementAtlas unloads only
instances it loaded itself.

Qwen models listed in `LMSTUDIO_NO_THINK_MODELS` receive `/no_think` at the
end of each user turn. This is a model-specific compatibility fallback in
addition to `reasoning_effort=none`; an installed model's chat template must
support Qwen's soft switch.

`APP_MODE=public-demo` additionally requires an operator name, privacy contact,
public URL and secure cookies. It refuses to start if they are missing. Read
[Privacy and demo checklist](docs/privacy-and-demo-checklist.md) before
exposing any upload route.

## API

The browser uses these session-scoped endpoints:

- `GET /api/status`
- `GET`/`POST`/`PATCH`/`DELETE /api/families`
- `POST /api/upload`
- `GET /api/graph?view=overview|detail`
- `POST /api/enrich`
- `GET /api/enrich/status`
- `POST /api/query`
- `DELETE /api/session`

Workspace-scoped calls name their agreement family with a `?family=` query
parameter in both modes; in the public demo the family list is scoped to the
visitor's session.

No response contains the absolute server workspace path. Mutation requests
require the same-origin `X-AgreementAtlas-Request` header. See [API reference](docs/api.md).

## Tests and checks

```bash
./.venv/bin/python -m unittest discover -s tests -v
./.venv/bin/python -m py_compile \
  app.py evaluation.py legal_schema.py legal_ingest.py \
  legal_graph_service.py lmstudio_client.py session_store.py
node --check web/app.js
./.venv/bin/python evaluation.py
```

The committed fictional Tier-1 fixture currently scores 1.00 on its six
controlling-source questions, instrument taxonomy, scoped precedence,
definition conflict and chapeau negation, and returns no evidence for its
unsupported insurance question. These are regression metrics for an invented
family, not external validation or legal-accuracy claims.

The tests use only the fictional agreements under `samples/`. Never add real
customer contracts, generated visitor sessions, `.env` files or API tokens to
test fixtures.

## Public and hosted paths

1. **Local portfolio version:** AgreementAtlas and LM Studio run on one machine.
2. **Invite-only demonstration:** put Cloudflare Access in front of the app,
   keep AgreementAtlas and LM Studio loopback-bound, and use the short-lived session
   controls already in this release.
3. **Professional hosted service:** add authentication, tenant-aware object
   storage, durable job state, a queue/worker boundary and an approved model
   provider.

Kimi is technically compatible with the adapter direction because its API
supports OpenAI-style chat completions and structured JSON. It is not enabled
in this release: cloud agreement content must not be sent until the operator
has confirmed no-training terms, a suitable data-processing agreement,
retention, subprocessors, hosting location and any UK/EU international-transfer
safeguards. See [Hosted roadmap](docs/hosted-roadmap.md).

## Project documentation

- [Architecture and legal graph model](docs/architecture.md)
- [GraphRAG engine decision](docs/graphrag-engine-decision.md)
- [Optional Microsoft GraphRAG adapter](docs/microsoft-graphrag-integration.md)
- [API reference](docs/api.md)
- [Live Gemma + Nomic acceptance](docs/live-model-acceptance.md)
- [Hosted/Kimi roadmap](docs/hosted-roadmap.md)
- [Privacy and public-demo checklist](docs/privacy-and-demo-checklist.md)
- [GitHub release checklist](docs/github-release-checklist.md)
- [Implementation delta and audit checklist](IMPLEMENTATION_DELTA.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## Licence

Apache-2.0. The fictional sample agreements are included under the same
licence and are not legal templates.
