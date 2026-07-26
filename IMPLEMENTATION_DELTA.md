# AgreementAtlas legal graph rebuild — implementation delta

Prepared for independent Claude review on 26 July 2026.

## Executive result

The planned local dual-mode legal GraphRAG is implemented. Baseline mode builds
an immediate deterministic schema-v3 graph without a model. Deep mode processes
the complete substantive rule queue through LM Studio, strictly validates
evidence, resumes from atomic checkpoints, builds Nomic embeddings and retains
deterministic fallbacks wherever model output fails.

The AgreementAtlas legal resolver—not Microsoft GraphRAG—is canonical. Hosted
Kimi integration, Microsoft BYOG and marked-up Word export remain deliberate
next steps.

## Claude-plan delta

| Claude-plan item | Implemented behavior | Deviation and rationale |
|---|---|---|
| Remove confidential sample copies | OpenText PDFs were removed from `samples/`; identical local copies remain only in ignored `knowledge/sources/`. `GRAPH_REBUILD_PLAN.md` is also ignored because it contains review notes derived from those documents. | No private or derived OpenText material is publishable. |
| Correct claims and add schema v3 | README, handover and architecture identify canonical JSONL, derived browser graph and Microsoft GraphRAG's optional boundary. Versioned family, instrument, party/role, clause, span, definition, rule, precedence, cross-reference, amendment and relationship records were added. | Schema-v2 workspaces are rejected for rebuild, not migrated. |
| Deterministic legal structure | Title/structure taxonomy, validated ISO dates, parties/roles, chapeau/list inheritance, definitions, cross-references, amendments, scoped precedence and express `ENTERED_UNDER` are implemented. | Date and party extraction remain conservative deterministic heuristics. Ambiguity stays unresolved. |
| Remove hardcoded legal conclusions | Affiliate cross-product and blanket schedule/EULA precedence logic were removed. Legal edges require parsed language, scope and exact span evidence. | None. |
| Complete deep indexing | The 90-clause cap is gone. Work is context/character budgeted, fingerprinted, checkpointed, cancellable and resumable. | The live Gemma configuration uses one clause per batch because it dropped later records in multi-clause strict output. |
| Structured legal rules | Effect, modality, polarity, actor, action, object, structured scope, conditions, carve-outs, cross-references and multiple exact spans are extracted. | Live LM acceptance is limited to permission/obligation/prohibition. Deterministic baseline also models exclusion/remedy. This keeps failed model classifications on a validated deterministic fallback. |
| Strict validation and fallback | Lost negation, invalid actor/modality/effect, invented evidence and nonexistent IDs are rejected. Successful LM clauses replace duplicate deterministic rules; failed clauses retain fallbacks. | A missing literal `clause:` prefix is repaired only when unique inside the current batch. |
| Resolve legal competition | Definitions, scoped precedence and amendments produce directed, evidence-backed relationships and a typed resolution trace. Deep replacement rebases deterministic legal edges onto validated rule IDs. | Legal resolution is explainable heuristic logic, not a substitute for lawyer review. |
| Native LM Studio management | `/api/v1/models`, `/models/load` and `/models/unload` are supported. Downloaded models and loaded instances are distinguished. Only configured IDs may load; only AgreementAtlas-loaded instance IDs may unload. | Auto-management remains off by default. |
| Gemma/Qwen/Nomic setup | Gemma 4 26B-A4B and Nomic v1.5 are defaults. Nomic prefixes are enforced. Exact configured Qwen IDs receive `/no_think`. | Qwen `/no_think` is unit-tested but not yet live-tested against the downloaded MLX revision. |
| Session-local vectors and hybrid retrieval | Normalised float32 vectors use a binary file plus JSONL offset/model/hash index. BM25 and vector ranks use reciprocal-rank fusion. | No external vector database is required locally. |
| Directional graph traversal | Relationship-specific incoming/outgoing traversal excludes generic family/document/actor/scope/`CONTAINS` hubs and supports reverse override lookup. | None. |
| API compatibility | Upload, graph, enrich, status, query and delete endpoints remain. Status and query add schema/build/progress/model/retrieval/trace fields. | Additive response changes only. |
| Fictional evaluation | Six invented documents now include a second order, DPA, SLA exclusions, amendment, conflicting definition and chapeau/list negation. Eight gold questions cover the requested behaviors. | CUAD remains optional Tier 2 and no remote loader is executed. |
| Visual legal markup | Selecting a rule opens Clause Anatomy with effect/modality/polarity/actor/action/object and separate chapeau/item evidence. | Marked-up `.docx` export is not in this rebuild; the schema and browser proof-of-concept now support it. |
| Privacy/security | Existing session isolation, expiry, deletion, quotas, CSP/CSRF and rate limits remain. Deployment templates cover lawful basis, rights, retention, processors, transfers, incidents and PECR. | These are engineering controls and checklists, not a claim of GDPR compliance or legal advice. |
| GitHub documentation | README, architecture, API, hosted roadmap, privacy checklist, contributing, security, CI, release checklist and this delta are prepared. | The repository is initialised on `main`; final staging was denied by the environment's exhausted approval quota, and the private push awaits renewed `gh` authentication. |

## Schema and API changes

Canonical `legal/*.jsonl` now includes:

- `agreement_families`, `instruments`, `parties`, `clauses`,
  `evidence_spans`, `defined_terms`, `operative_rules`, `precedence_rules`,
  `cross_references`, `amendments` and `relationships`;
- deep-only `lm_rules`, `resolved_rules`, `relationships_enriched`,
  `embeddings.index.jsonl` and an atomic deep-build checkpoint; and
- `embeddings.f32` as the session-local binary vector store.

`documents.jsonl` and `rules.jsonl` remain compatibility views.

API additions:

- status: build mode/stage/completed/total/schema and genuinely loaded
  extractor/embedder;
- graph: schema-v3 typed nodes, directed evidence-backed relationships and
  Clause Anatomy evidence segments;
- query: `resolution_trace`, graph build mode and retrieval components; and
- enrich: resumable server-side configured-model deep build.

## Test evidence

Model-free fictional metrics:

- controlling-clause precision: `1.00`;
- instrument classification accuracy: `1.00`;
- precedence-resolution accuracy: `1.00`;
- definition-conflict accuracy: `1.00`;
- chapeau-negation preservation: `1.00`; and
- unsupported-claim rate: `0.00`.

The suite covers stable IDs, schema-v2 rejection, chapeau inheritance, scoped
precedence, competing definitions, current amendments, no hub collapse,
reverse traversal, checkpoint resume, model allowlisting/managed unload,
Qwen-only `/no_think`, Nomic prefixes, BM25 fallback, strict evidence
validation and citation fallback.

Final clean-run evidence:

- Ruff formatting: 35 files formatted, check passed;
- Ruff lint: passed;
- JavaScript syntax and Python compilation: passed;
- model-free graph/deep/model-management/security/session suite: 23/23 passed;
- fictional evaluation: all eight questions passed and every published metric
  matched its target; and
- secret-pattern scan: no private-key, OpenAI-style key, GitHub token, AWS key
  or long inline credential match in Git candidates.

The full discovery command reached the existing API test class but this
environment denied its ephemeral loopback socket at `server.bind` before any
API assertion ran. Those three API tests had passed in an earlier
loopback-enabled run; a final post-format rerun and interactive browser check
remain explicitly listed in the handover.

## Live-model result

Gemma + Nomic, fictional family only:

- 31/31 work items completed in 92.417 seconds;
- 26 validated LM rules across 24/31 clauses (`77.42%`);
- eight deterministic fallback rules retained for seven failed clauses;
- 86 records embedded at 768 dimensions;
- vector-backed query completed in 5.160 seconds;
- StreamFlow order returned as the top and controlling source; and
- numbered source citation present.

See `docs/live-model-acceptance.md` for configuration and defects found.

## Known gaps

1. Live Qwen3.6 `/no_think` behavior is not yet measured.
2. The fictional gold set is intentionally small; external retrieval and legal
   accuracy require a licensed/redistributable benchmark and expert review.
3. Deterministic parsing is English-oriented and intentionally conservative.
4. DOCX Clause Anatomy export is designed but not implemented.
5. Microsoft GraphRAG BYOG/Local/DRIFT comparison is not implemented.
6. Hosted identity, tenant storage, queueing, observability and cloud-provider
   governance are not implemented.
7. Anonymous public deployment still requires conversion sandboxing, malware
   scanning, infrastructure retention tests and independent security review.
8. The final sandbox could not bind a loopback port, so API/browser interaction
   needs one repeat outside this restricted execution environment.
9. The first Git commit is not created because the same managed environment
   denied the Git-index write after its approval quota was exhausted.

## Focused Claude audit checklist

- [ ] Attempt to construct a rule with invented, partial or cross-clause
      evidence and confirm validation rejects it.
- [ ] Verify the unique stripped-prefix repair cannot map outside its batch or
      resolve an ambiguous identifier.
- [ ] Inspect chapeau/list offsets and confirm negation, actor and modality
      survive in each operative item.
- [ ] Challenge instrument classification, order back-references, definition
      control and amendment targeting with ambiguous fictional variants.
- [ ] Verify precedence scope is conjunctive across populated structured
      dimensions and does not control merely because a product name matches.
- [ ] Confirm deep edge rebasing chooses the correct LM rule when one clause
      yields multiple candidate rules.
- [ ] Confirm reverse traversal finds controlling rules but generic hub types
      cannot collapse the graph.
- [ ] Inspect binary embedding offsets, float32 normalisation, model/hash
      invalidation and query/document prefixes.
- [ ] Exercise checkpoint cancellation at every file-write boundary and with a
      changed source/model/prompt fingerprint.
- [ ] Confirm all API model choices are allowlisted and externally loaded
      instances cannot be unloaded.
- [ ] Repeat the complete security/session/API suite unchanged.
- [ ] Scan the staged Git tree and history for OpenText text, agreements,
      generated data, paths, `.env` values and tokens before publication.
