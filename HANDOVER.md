# AgreementAtlas — project handover

Last updated: 26 July 2026

## Product goal

AgreementAtlas is an open-source, local-first legal GraphRAG application for
software and cloud agreement families. A visitor uploads related master terms,
orders, amendments, addenda, DPAs, SLAs and policies, then inspects the legal
knowledge graph and asks evidence-grounded questions through LM Studio.

The repository is intended as portfolio evidence of software/cloud licensing
knowledge, graph-based applied AI, privacy-aware product design and secure
multi-visitor engineering. The hosted/Kimi path remains future work.

## Current legal graph

Schema v3 is implemented. Canonical records—not the browser graph—store:

- agreement families and two-level instrument taxonomy;
- validated dates, signing parties and contractual roles;
- clauses, list chapeaux/items and exact evidence spans;
- definitions, operative rules, scoped precedence and cross-references;
- amendment operations; and
- evidence-backed directed relationships.

The baseline parser immediately emits real legal edges including
`ENTERED_UNDER`, `REDEFINES`, `CONTROLLING_DEFINITION`, `USES_TERM`,
`CONTROLS_FOR_DEFINED_SCOPE`, `OVERRIDES`, `QUALIFIES` and `AMENDS`. The former
hardcoded affiliate cross-product and blanket schedule/EULA assertion are gone.

`documents.jsonl` and `rules.jsonl` remain compatibility views. The canonical
files live under `legal/`; the browser projection lives under `output/`.
Schema-v2 workspaces must be rebuilt from source.

## Retrieval

The default retriever is a true graph-augmented legal resolver:

```text
question
  -> BM25 + optional Nomic embeddings
  -> reciprocal-rank fusion
  -> relationship-specific incoming/outgoing traversal
  -> controlling definition/scope/amendment/precedence resolution
  -> controlling/current evidence rerank
  -> typed resolution trace + exact evidence
  -> grounded LM Studio answer
```

It does not traverse generic family, document, party/role, scope or `CONTAINS`
hubs. Unsupported questions return `UNRESOLVED` without an LLM call.

Microsoft GraphRAG is not the underlying runtime. It remains a later BYOG
benchmark; the AgreementAtlas legal resolver is canonical.

## Deep build and LM Studio

`POST /api/enrich` processes every substantive clause; there is no 90-clause
cap. Work is context-budgeted, fingerprinted, checkpointed atomically,
cancellable and resumable. Validated LM rules replace deterministic fallbacks
for successful clauses. Failed/empty extractions retain fallback records.

Validation rejects nonexistent clause IDs, invalid effect/modality/polarity,
lost negation, actors outside the extracted party/role set and evidence that is
not an exact whitespace-normalised source/chapeau substring.

The LM Studio client:

- uses native `/api/v1/models` for truly loaded instances;
- supports allowlisted `/models/load` and `/models/unload`;
- unloads only instance IDs it loaded;
- defaults `LMSTUDIO_AUTOMANAGE_MODELS=false`;
- disables reasoning, adds Qwen's `/no_think` soft switch only for exact IDs
  listed in `LMSTUDIO_NO_THINK_MODELS`, and uses strict JSON Schema for
  extraction; and
- adds required Nomic `search_document:`/`search_query:` prefixes.

The initial live acceptance stack is Gemma 4 26B-A4B at 32,768 context and one
parallel prediction plus Nomic Embed Text v1.5. Qwen remains a model-revision
comparison. Its exact installed ID, `qwen3.6-27b-mlx`, is configured to receive
the Qwen `/no_think` soft switch, but has not yet been rerun live after that
change.

Embeddings are normalised float32 vectors in a session-local binary file with
a JSONL byte-offset/model/hash index. BM25 works when embeddings are absent or
query embedding fails.

## Browser and API

The three-column UI retains upload, graph and chat. Selecting a rule now opens
**Clause Anatomy**, showing effect, modality, polarity, actor, action, object,
conditions/carve-outs and separately highlighted chapeau/item evidence.

Endpoints remain:

- `GET /healthz`
- `GET /api/status`
- `POST /api/upload`
- `GET /api/graph?view=overview|detail`
- `POST /api/enrich`
- `GET /api/enrich/status`
- `POST /api/query`
- `DELETE /api/session`

Status adds build mode/stage/progress/schema and loaded extractor/embedder
details. Query adds build mode, retrieval components and `resolution_trace`.

## Session, security and privacy

The previous visitor-isolation and security layer remains:

- random 256-bit session IDs and one workspace per visitor;
- absolute six-hour expiry, an independent 30-second cleanup sweep, and
  immediate whole-session deletion on request;
- 12-file/50-MB limits, filename/type/archive checks and atomic uploads;
- HTTP-only `SameSite=Strict` cookie (`Secure` required in public mode);
- CSP/CSRF/framing/referrer/MIME controls and rate limits;
- server-side-only LM Studio calls; and
- public-demo configuration that fails closed.

Privacy/terms material is a deployment template and governance checklist, not
a claim of automatic GDPR compliance or legal advice. Cloud inference remains
disabled until the operator completes the provider/DPA/retention/subprocessor/
international-transfer review.

Never commit uploaded agreements, generated sessions, `.env`, tokens, logs
containing agreement text, the OpenText PDFs or OpenText-derived artifacts.
The confidential PDF copies were removed from `samples/`; identical local
copies remain only under ignored `knowledge/sources/`.

## Fictional evaluation

Six invented Acme documents cover two orders, DPA, SLA, amendment, conflicting
definition and chapeau/list negation. `samples/gold_questions.json` is the
Tier-1 question set.

Current model-free fixture metrics:

- controlling-clause precision: `1.00`;
- instrument classification: `1.00`;
- precedence and definition-conflict accuracy: `1.00`;
- chapeau negation preservation: `1.00`; and
- unsupported-claim rate: `0.00`.

These are regression metrics for a small invented family, not real-world legal
accuracy. CUAD raw JSON is the optional future Tier-2 retrieval benchmark;
never execute a remote dataset loader.

The final fictional-only Gemma + Nomic acceptance completed 31/31 clause
work items in 92.417 seconds. It accepted 26 LM rules across 24 clauses
(`77.42%` clause coverage), retained eight deterministic fallbacks, embedded
86 records at 768 dimensions and returned the StreamFlow order as controlling
through vector-backed retrieval. Seven clauses failed strict validation and
correctly retained fallback coverage. See `docs/live-model-acceptance.md`.

## Validation

```bash
./.venv/bin/ruff format --check .
./.venv/bin/ruff check .
node --check web/app.js
./.venv/bin/python -m py_compile \
  app.py evaluation.py legal_schema.py legal_ingest.py \
  legal_graph_service.py lmstudio_client.py session_store.py
./.venv/bin/python -m unittest discover -s tests -v
./.venv/bin/python evaluation.py
```

API tests bind an ephemeral loopback port. The live model acceptance is
separate from CI and must use only the fictional family.

## Git and publication

The local repository was initialised on `main`, but the managed environment
denied the final Git-index write after its approval quota was exhausted, so no
first commit exists yet. Before a private GitHub push:

1. repeat the final API/browser loopback check when socket binding is allowed;
2. review `IMPLEMENTATION_DELTA.md`;
3. scan tracked candidates for secrets, private agreements and generated data;
4. confirm the `jmangan415` repository metadata; and
5. authenticate GitHub CLI (the configured `jmangan415` token was expired at
   the last check).

## Next implementation steps

1. Independent Claude audit using `IMPLEMENTATION_DELTA.md`.
2. Repeat the three API tests and interactive Clause Anatomy browser check in
   an environment allowed to bind an ephemeral loopback port. The final
   sandbox rerun was blocked at socket creation, not by an assertion.
3. Live Qwen3.6 comparison with `/no_think`, after unloading Gemma or otherwise
   confirming adequate memory headroom.
4. Optional raw CUAD retrieval benchmark and public-contract fetch scripts,
   with no downloaded contracts committed.
5. Marked-up `.docx` export using the existing Clause Anatomy/evidence schema.
6. Microsoft GraphRAG BYOG exporter and measured Local/DRIFT comparison.
7. Portfolio screenshots/demo using only fictional agreements.
8. Hosted alpha only after authentication, tenancy, queued workers,
   observability and provider/privacy approval.
