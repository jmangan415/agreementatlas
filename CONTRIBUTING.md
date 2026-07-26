# Contributing to AgreementAtlas

Contributions that improve evidence fidelity, legal-rule scope, privacy,
security, accessibility and reproducible evaluation are welcome.

## Set up

```bash
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

LM Studio is optional for deterministic ingestion and most tests.

## Before opening a pull request

```bash
ruff format .
ruff check .
node --check web/app.js
./.venv/bin/python -m py_compile \
  app.py evaluation.py legal_schema.py legal_ingest.py \
  legal_graph_service.py lmstudio_client.py session_store.py
./.venv/bin/python -m unittest discover -s tests -v
```

Add or update tests for observable behavior. Prefer small fictional clauses
that isolate one licensing issue: scope, condition, exception, precedence,
defined term, amendment or evidence citation.

## Test-data rules

Never contribute:

- customer or employer agreements;
- generated visitor workspaces;
- `.env` files, credentials or API tokens;
- document text copied from a non-redistributable agreement;
- the private OpenText source PDFs used during early development; or
- logs containing filenames, questions, answers or extracted evidence.

New examples must be original and clearly redistributable. State their licence
and fictional status.

## Engineering expectations

- Keep exact clauses as the source of truth.
- Never accept an LLM rule without a valid source-clause ID and exact evidence.
- Preserve document and product scope.
- Do not insert extracted text with `innerHTML`.
- Do not expose server paths or provider credentials through APIs.
- Keep the safe local profile working when adding hosted capabilities.
- Treat privacy copy as a deployment template, not a compliance claim.
- Document new public endpoints, storage, cookies, subprocessors or retention.

## Microsoft GraphRAG

The default application must remain independent of Microsoft GraphRAG.
Changes under `knowledge/` or the optional Codex plugin should not make
`graphrag` a required runtime dependency for upload, graph rendering or chat.
