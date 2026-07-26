# GitHub release checklist

The working directory was prepared for publication but should not be pushed
until every item below is checked.

## Repository identity

- [x] Use `AgreementAtlas` as the product name and `agreementatlas` as the proposed
      repository name.
- [x] Replace `OWNER` in `README.md` and `pyproject.toml` with the GitHub
      account or organisation.
- [ ] Replace the generic project author and add the copyright owner/year if
      desired.
- [ ] Confirm Apache-2.0 is the intended licence.
- [ ] Add a concise repository description and topics such as
      `legal-rag`, `knowledge-graph`, `software-licensing`, `cloud-contracts`
      and `lm-studio`.

## Sensitive-data review

- [ ] Confirm `git status` contains no `.env`, `data/`, `knowledge/sources/`,
      `knowledge/legal/`, `knowledge/output/`, `tmp/` or real agreement files.
- [ ] Search the complete history for API keys, email addresses, customer
      names and source-document text before the first push.
- [ ] Confirm the OpenText PDFs and their generated clauses are absent.
- [ ] Confirm screenshots, terminal output and issue examples use only the
      fictional `samples/` family.
- [ ] Run a secret scanner before publishing.

## Product evidence

- [ ] Capture a desktop screenshot of the fictional agreement graph.
- [ ] Capture the evidence inspector showing a product-specific restriction.
- [ ] Capture a chat answer with exact source citations and the disclaimer.
- [ ] Record a short local demo: upload family → inspect graph → ask question →
      delete session.
- [ ] Add selected media under a clearly named `docs/assets/` directory and
      reference it from the README.

## GitHub controls

- [x] Initialise the repository and review the first commit file by file.
- [ ] Enable branch protection and require the CI workflow.
- [ ] Enable Dependabot or an equivalent dependency-update process.
- [ ] Enable code scanning and secret scanning if available.
- [ ] Enable private vulnerability reporting.
- [ ] Replace the placeholder reporting paragraph in `SECURITY.md` with a
      monitored contact.
- [ ] Add issue and pull-request templates after the contribution workflow is
      settled.

## Release validation

```bash
ruff format --check .
ruff check .
node --check web/app.js
python -m py_compile \
  app.py evaluation.py legal_schema.py legal_ingest.py \
  legal_graph_service.py lmstudio_client.py session_store.py
python -m unittest discover -s tests -v
```

- [ ] Run AgreementAtlas from a fresh clone and clean virtual environment.
- [ ] Upload only `samples/acme-*.md`.
- [ ] Confirm two browser profiles receive separate documents and graphs.
- [ ] Confirm automatic and manual deletion on the target operating system.
- [ ] Confirm LM Studio remains loopback-bound.
- [ ] Review privacy/terms text for the actual operator before any public demo.
- [ ] Use authenticated reviewer access rather than an anonymous permanent
      upload URL.
