# Architecture and legal graph model

## Design invariant

AgreementAtlas models an agreement family as evidence-backed legal records, not
as a bag of similar chunks. The browser graph is a projection; it is never the
canonical store. Every extracted rule, precedence conclusion, definition
choice and amendment points to exact source spans.

The local release has two build modes:

- **Baseline** builds deterministic structure immediately and requires no
  model.
- **Deep** processes every substantive clause through an allowlisted LM Studio
  model in resumable batches, validates the result, replaces successful
  clause-level fallbacks and builds embeddings.

Schema-v2 workspaces are rejected and rebuilt from their source files. They are
not migrated because the old graph lacks the evidence needed to manufacture
schema-v3 legal conclusions safely.

## Runtime data flow

```text
Isolated upload session
  │
  ├─ quota, filename, signature and archive checks
  ├─ staging workspace + atomic swap
  ▼
MarkItDown conversion
  ▼
Deterministic legal structure (baseline)
  ├─ agreement family + instruments
  ├─ parties and contractual roles
  ├─ clauses, chapeaux and list items
  ├─ exact evidence spans
  ├─ definitions and usages
  ├─ scoped precedence and cross-references
  ├─ amendment operations
  └─ deterministic operative-rule fallbacks
  │
  ├──► canonical legal/*.jsonl
  └──► derived browser graph
             │
             ▼ optional POST /api/enrich
       LM Studio structured extraction
         ├─ context-budgeted batches
         ├─ atomic fingerprint checkpoint
         ├─ cancellation and resume
         ├─ negation/actor/modality/span validation
         ├─ resolved rule set
         └─ Nomic float32 embeddings + offset index
```

Every visitor has a random 256-bit identifier in an HTTP-only,
`SameSite=Strict` cookie. It maps to one workspace under `data/sessions/`.
Expiry is absolute. Upload conversion happens in a staging workspace and is
swapped only after every file succeeds.

## Canonical schema v3

Dedicated JSONL files store:

- `agreement_families`
- `instruments`
- `parties`
- `clauses`
- `evidence_spans`
- `defined_terms`
- `operative_rules`
- `precedence_rules`
- `cross_references`
- `amendments`
- `relationships`

`documents.jsonl` and `rules.jsonl` remain compatibility views. Deep mode adds
`lm_rules.jsonl`, `resolved_rules.jsonl`,
`relationships_enriched.jsonl`, `embeddings.f32`,
`embeddings.index.jsonl` and `deep_build_checkpoint.json`.

Important node types are `agreement_family`, `document`, `party_or_role`,
`clause`, `definition`, `rule`, `llm_rule`, `precedence_rule` and `amendment`.
Relationships currently derived from evidence include:

- structural: `BELONGS_TO`, `CONTAINS`, `HAS_LIST_ITEM`, `HAS_ROLE`;
- provenance: `SUPPORTED_BY`;
- family hierarchy: `ENTERED_UNDER`;
- definitions: `REDEFINES`, `CONTROLLING_DEFINITION`, `USES_TERM`;
- legal resolution: `CONTROLS_FOR_DEFINED_SCOPE`, `OVERRIDES`, `QUALIFIES`;
- cross-document change/reference: `AMENDS`, `SUBJECT_TO`,
  `CROSS_REFERENCES`, `INCORPORATES_BY_REFERENCE`.

An unresolved cross-reference or amendment remains a typed unresolved record;
the resolver does not invent a target.

## Rule and evidence model

An operative rule separates:

- `effect` — permission, obligation, prohibition, exclusion or remedy;
- `modality` — the preserved `MAY`, `MUST`, `SHALL`, `WILL`, `CAN` or
  `OTHER`;
- `polarity` — positive or negative;
- actor/role, action and object;
- structured product, licence-model, entity, territory and subject-matter
  scope;
- conditions, carve-outs and cross-references; and
- one or more exact evidence-span identifiers.

For `Customer shall not: (a)… (b)…`, the chapeau and each list item are
separate clauses and separate exact spans. Each item rule inherits the
chapeau’s actor, modality and negation. The browser’s **Clause Anatomy** panel
uses the same record to display this as a sentence diagram; it is not
additional model output.

Deep extraction is rejected when the clause ID does not exist, a modal/effect
is outside the vocabulary, negation is lost, an actor is outside the extracted
party/role set, or any supplied evidence is not an exact whitespace-normalised
substring of the clause or its governing chapeau.

## Retrieval and legal resolution

The default retriever:

1. creates BM25 rankings over rules, definitions and clauses;
2. optionally embeds the question with the required `search_query:` prefix and
   fuses it with persisted `search_document:` vectors using reciprocal-rank
   fusion;
3. follows relationship-specific incoming/outgoing edges;
4. never fans out through agreement-family, document, party/role, scope or
   `CONTAINS` hubs;
5. resolves competing definitions, conditions, carve-outs, amendment targets
   and scoped precedence; and
6. reranks controlling/current records above qualified, amended or overridden
   records.

The query API returns exact evidence and a typed resolution trace containing
the candidate rule, applicable scope, controlling instrument/rule, legal basis
and evidence-span IDs, final status and unresolved warnings. When there is no
meaningful content-term match, AgreementAtlas returns a deterministic
`UNRESOLVED` response without calling the answer model.

## LM Studio boundary

The server uses:

- native `GET /api/v1/models` to distinguish downloaded models and loaded
  instances;
- native `POST /api/v1/models/load` and `/unload` for optional configured
  model management;
- OpenAI-compatible `/v1/chat/completions` with strict JSON Schema and
  reasoning disabled; and
- OpenAI-compatible `/v1/embeddings`.

Only configured model IDs are accepted. Auto-management defaults off.
AgreementAtlas tracks the instance IDs it loaded and refuses to unload an
external instance. The browser never calls port 1234 or supplies arbitrary
model URLs/tokens.

Gemma 4 26B-A4B is the initial extractor acceptance model. Nomic Embed Text
v1.5 is the initial embedding baseline. Both remain configurable and must be
benchmarked rather than treated as permanent product choices.

## Microsoft GraphRAG and hosted boundaries

Microsoft GraphRAG is not the runtime beneath the browser. AgreementAtlas’s legal
resolver is canonical. The retained Microsoft configuration and proposed BYOG
adapter are an optional future benchmark after schema-v3 gold evaluation.

Likewise, Kimi or another cloud model is a future provider implementation, not
an environment-variable switch in this local release. Hosted use requires
identity, tenant-aware durable storage, queued workers, auditable deletion,
operational controls and a completed processor/retention/subprocessor/transfer
review.
