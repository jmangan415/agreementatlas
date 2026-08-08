# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read first

`AGENTS.md` points at the pointer chain: `HANDOVER.md` (architecture, security, validation) and `IMPLEMENTATION_DELTA.md` (source of truth for what landed and open audit items). `GRAPH_REBUILD_PLAN.md` is historical.

## What this is

AgreementAtlas — a local-first legal GraphRAG application for families of software/cloud agreements (master terms, orders, amendments, DPAs, SLAs). A deterministic parser builds an evidence-backed legal graph immediately, with no model required; LM Studio (server-side, loopback-only) optionally enriches clause rules and answers questions with citations to exact clause text.

Stack: Python stdlib at runtime — `ThreadingHTTPServer` in `app.py`, no web framework, no database; even the LM Studio calls go through `urllib`. Third-party runtime deps are document-conversion only (`pyproject.toml`): `markitdown[docx,pdf,pptx,xls,xlsx]`, `pdfplumber`, `pandas`, `requests`, plus `python-dotenv`. Frontend is framework-free static HTML/CSS/JS under `web/`. Microsoft GraphRAG is an optional research layer (`knowledge/`, extra `graphrag` dependency) and must never become a runtime requirement for upload, graph rendering or chat.

## Commands

```bash
# setup — .venv is native arm64, Python 3.14, built with uv and pinned by
# requirements.lock (committed). Full rebuild procedure lives in DEPLOY.md.
uv venv .venv --python 3.14
uv pip install -r requirements.lock
uv pip install --no-deps -e .
cp .env.example .env

# run locally (http://127.0.0.1:8000, APP_MODE=local, persistent data/library)
./.venv/bin/python app.py

# full gate — same as CI (.github/workflows/ci.yml)
./.venv/bin/ruff format --check .        # `ruff format .` to write
./.venv/bin/ruff check .
node --check web/app.js
./.venv/bin/python -m py_compile app.py evaluation.py legal_schema.py legal_ingest.py legal_graph_service.py lmstudio_client.py session_store.py
./.venv/bin/python -m unittest discover -s tests -v

# one test module / one test by pattern
./.venv/bin/python -m unittest tests.test_legal_ingest -v
./.venv/bin/python -m unittest -k <pattern> -v

# fixture regression metrics (fictional Acme family; expected 1.00s and 0.00 unsupported-claim rate)
./.venv/bin/python evaluation.py
```

Tests and evaluation run without LM Studio (deterministic path only). API tests bind an ephemeral loopback port. Ruff: line-length 88, target py312, rules `E4,E7,E9,F,I,B`, excluding `.venv`/`data`/`knowledge`/`tmp`. Import sorting (`I`) is on, so let ruff order imports; bugbear (`B`) catches mutable defaults, loop-variable capture and `except` shadowing. Broad `except Exception` handlers are deliberate at the ingest boundary — a malformed upload must fall back, not stop the build — and carry a `# noqa: BLE001` comment explaining why, though `BLE` itself is not currently selected.

Python: `requires-python = ">=3.12"`, CI matrix is 3.12 and 3.14. The `graphrag` extra caps Python below 3.14 and is *not* installed in `.venv` — research work under `knowledge/` needs a separate 3.12/3.13 venv. Nothing at runtime may depend on it.

## The live site — agreementatlas.com runs on this machine

Two servers run side by side **on purpose** (see `DEPLOY.md`):

| port | mode | what it is |
|---|---|---|
| 8000 | `local` | dev server; `data/library` includes licensed third-party PDFs — must never be exposed |
| 8001 | `public-demo` | what the world sees, via Cloudflare Tunnel (`agreementatlas.com`) |

- launchd services: `com.agreementatlas.app` (runs `scripts/serve_public.sh`) and `com.agreementatlas.tunnel`.
- Static/UI edits under `web/` are live immediately: HTML is served `no-store`, and JS/CSS links are rewritten at serve time with a content-hash `?v=` (see `asset_version` in `app.py`), so no restart and no CDN cache worry.
- Python changes need: `launchctl kickstart -k gui/$(id -u)/com.agreementatlas.app`
- After any deploy-affecting change run the safety check in `DEPLOY.md`: a cookieless `GET https://agreementatlas.com/api/families` must return **only sample families** (every entry `"is_sample": true`, at most a handful — never the 16-family local library), and `/` **and** `/workbench/` must return 200.
- Public sessions each hold a nested family library (`data/sessions/<id>/families/`, a per-session `LibraryStore`): the two sample bundles are auto-installed pre-enriched and read-only, visitors create up to `MAX_PUBLIC_FAMILIES` (3) of their own, and `?family=` selects the workspace in both modes. Sample bundles live uncommitted under `samples/demo_bundles/<slug>/` with a `demo.json` manifest (name, questions, `source_url`); the OpenText bundle's canonical question list is `samples/demo_questions_opentext.json`.

### UI file map (which files a page actually uses)

- `/` serves `web/welcome/index.html` → loads `/welcome.css` (no `app.js`). **This is the marketing landing page in both modes.**
- `/workbench/` serves `web/index.html` → loads `/app.js` + `/styles.css`. **This is the live tool page.** The workbench gates its graph and chat panels behind overlays until a family with documents is selected (`renderProgressiveWorkspace` in `app.js`).
- `web/app.html` → same `app.js` + `styles.css` pair; reachable at `/app.html` for old bookmarks only. Keep its element ids in lockstep with `web/index.html` — both pages load the same `app.js`, and an id missing from either page throws.
- `/demo.html` (`web/demo.html`) → `demo.js` + `demo-graph.js` + `demo.css`; kept reachable by name while the interface convergence settles.
- `web/story.html`, `web/blog/`, `web/privacy.html`, `web/terms.html` → `styles.css`.
- `web/assets/workbench-tour.png` is the annotated workbench screenshot on the landing page.
- `app.py` resolves static paths strictly inside `web/`; files elsewhere (e.g. stray `demo.*` copies at repo root) are never served.
- HTML placeholders `{{OPERATOR_NAME}}`, `{{PRIVACY_EMAIL}}`, `{{RETENTION_HOURS}}`, `{{APP_MODE}}` are substituted server-side at serve time.

## Architecture

Flat modules at repo root:

- `app.py` — HTTP layer: routing, session cookies, CSRF header (`X-AgreementAtlas-Request` on mutations), CSP/security headers, rate limits, static serving + templating, public-demo fail-closed startup checks.
- `legal_ingest.py` — deterministic parser: uploads → text extraction → canonical **schema-v3** JSONL (`legal/*.jsonl` per workspace: instruments, parties, clauses, evidence spans, defined terms, operative/precedence rules, cross-references, amendments, relationships). Emits real legal edges (`ENTERED_UNDER`, `REDEFINES`, `CONTROLLING_DEFINITION`, `CONTROLS_FOR_DEFINED_SCOPE`, `OVERRIDES`, `QUALIFIES`, `AMENDS`, …) plus deterministic operative-rule fallbacks.
- `legal_graph_service.py` — retrieval and resolution: BM25 + optional Nomic embeddings fused by reciprocal-rank fusion → relationship-specific traversal (never through family/document/party/scope/`CONTAINS` hubs) → controlling definition/scope/amendment/precedence resolution → typed resolution trace + exact evidence. Also deep-enrichment orchestration (context-budgeted, fingerprinted, checkpointed, resumable) and `answer_question`.
- `legal_schema.py` — schema vocabulary/validation (effect, modality, polarity, scope).
- `lmstudio_client.py` — the only model egress point: loopback LM Studio, allowlisted model IDs, native `/api/v1/models` for truly-loaded instances, strict JSON Schema extraction, reasoning disabled, Nomic `search_document:`/`search_query:` prefixes. Browser never talks to port 1234.
- `library_store.py` — persistent agreement families (`data/library/`, local mode). Adding a document re-runs the deterministic build but keeps validated LM rules for existing clauses (clause identity is document-derived).
- `session_store.py` — ephemeral visitor workspaces (`data/sessions/`, public-demo): 256-bit IDs, absolute 6-hour expiry, 12-file/50 MB quotas, atomic staged uploads.
- `conversation.py` — chat-turn state; `evaluation.py` — fixture regression gate.

Text extraction (`extract_source_text` in `legal_ingest.py`) is layout-aware, not a flat conversion:

- `.txt`/`.md` are read directly; PDFs go to `pdf_text()` (pdfplumber); everything else — and any PDF that yields nothing — falls back to MarkItDown, which reads the Office formats pdfplumber cannot open.
- `pdf_text()` finds table regions and emits prose bands and tables in page order, row by row. Flattening a page instead splices multi-column tables into single sentences that are quotable, plausible and wrong.
- `detect_pdf_headings()` recovers hierarchy from PDF typography (font size vs. body median, running headers and dot-leader TOC rows excluded), because many agreements carry no section numbering and otherwise collapse into one "Preamble".
- A malformed PDF returns `""` and falls back; it never breaks the build.

Data invariants:

- Canonical `legal/*.jsonl` records are the store; the browser graph is a projection, never the source of truth.
- Every rule/conclusion points at exact evidence spans (whitespace-normalised substrings of source or governing chapeau).
- Deterministic records come first; validated LM rules replace per-clause fallbacks but never rewrite evidence. Rejected extractions (bad clause ID, vocabulary violation, lost negation, unknown actor, inexact evidence) keep the deterministic fallback.
- Chapeau and list items are separate clauses/spans; item rules inherit chapeau actor, modality and negation.
- Questions with no meaningful content-term match return `UNRESOLVED` without calling the LLM.
- Schema-v2 workspaces are rebuilt from source, not migrated.

## Operational scripts

Parser and schema changes do **not** reach existing workspaces on their own — stored `legal/*.jsonl` is the output of the ingest code that was running the day the document was added. After changing `legal_ingest.py`, rebuild before judging the result, or you are reading yesterday's parse.

- `scripts/reingest_library.py` — rebuild the deterministic layer of every library family (parsing, definitions, offerings, rules), keeping validated LM rules.
- `scripts/enrich_library.py` — batch LM enrichment across families, concurrent between families and serial within one. Long-running; the full library is hours.
- `scripts/parse_health.py` — did the documents *parse*? Ratio diagnostics over a tree of real agreements. The deterministic layer is regex-driven and fails silently: an unsupported numbering style still yields clauses, rules and a graph, just wrong ones. Run it before trusting a build.
- `scripts/graph_audit.py` — does what was built on top *hold together*? Rules citing text that does not exist, self-contradicting rules, a precedence graph asserting both directions.
- `scripts/build_demo_bundle.py` — build the pre-enriched sample bundle a public visitor gets on one click (enrichment is too slow and too rate-limited to run per visitor).
- `scripts/reset_sessions.py` — delete leftover `data/sessions/` workspaces; they still hold uploaded agreements, so removal is deliberate rather than automatic.

## Hard rules

- Never commit: `data/` (library/sessions), `.env`, tokens, the OpenText source PDFs or anything derived from them, or logs containing agreement text. Test fixtures use only the fictional `samples/` family.
- Never accept an LM rule without a valid source-clause ID and exact evidence substring.
- No `innerHTML` for extracted text in `web/`.
- Do not expose server workspace paths or provider credentials through APIs.
- Cloud inference stays disabled until the operator completes the provider/DPA/retention review (`docs/hosted-roadmap.md`).

## Overnight goal loop

`.claude/skills/goal` + `scripts/goal_driver.sh` run a headless graph-hardening loop against a frozen golden question set: state in `goal/GOAL_STATE.json`, questions in `goal/goldens.json`, narrative log in `goal/GOAL_LOG.md`, reports under `goal/reports/`. Benchmarks must exercise the shipped answering path (`answer_question`), not a parallel harness path.
