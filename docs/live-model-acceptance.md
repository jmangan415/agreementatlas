# Live LM Studio acceptance

This suite is deliberately separate from CI. It uses only the invented six-file
Acme family, writes to an automatically deleted temporary workspace and calls
the operator's loopback LM Studio server.

## Accepted stack

- extractor: `google/gemma-4-26b-a4b-qat`, MLX 4-bit;
- extractor context: 32,768 tokens, one parallel prediction;
- embedder: `text-embedding-nomic-embed-text-v1.5`;
- embedding context: 2,048 tokens; and
- extraction batch size: one substantive clause.

The one-clause batch is intentional. In diagnostic probes this Gemma revision
could omit the second clause in a strict structured-output batch. Each accepted
response still passes exact clause/chapeau substring validation, actor,
modality, polarity and negation checks.

## Final result — 26 July 2026

| Measure | Result |
|---|---:|
| Baseline documents / clauses / rules | 6 / 48 / 31 |
| Deep clause work completed | 31 / 31 |
| Validated LM rules | 26 |
| Clauses with validated LM output | 24 / 31 (77.42%) |
| Deterministic fallback rules retained | 8 |
| Strict-validation failures | 7 clauses |
| Embedded records | 86 |
| Embedding dimensions | 768 |
| Deep-build wall time | 92.417 seconds |
| Query wall time | 5.160 seconds |
| Hybrid vector retrieval used | yes |
| Top source for affiliate question | StreamFlow order schedule |
| Legal-resolution status | `RESOLVED` |
| Controlling source preserved after LM replacement | yes |
| Numbered source marker present | yes |

Strict-validation failures are covered by deterministic rules; they do not
become ungrounded LM records. The success rate is model/build specific and is
not a legal-accuracy score.

## Defects found by live acceptance

1. Gemma removed only the literal `clause:` prefix from an otherwise correct
   stable ID. AgreementAtlas now repairs this only when it uniquely maps to the
   current batch. Ambiguous and invented IDs remain rejected.
2. Validated LM rules have different IDs from deterministic rules. The deep
   graph now rebases the deterministic resolver's evidence-backed legal edges
   onto the corresponding validated clause rules, preventing precedence and
   definition paths from being severed by model wording.
3. The acceptance script originally looked specifically for `[1]`; it now
   accepts any numbered source marker. The answer layer adds
   `Sources reviewed: [1]` if a model provides no numbered marker.

## Qwen comparison

`qwen3.6-27b-mlx` is downloaded but was not loaded for this final run. The
client appends `/no_think` only to exact IDs in
`LMSTUDIO_NO_THINK_MODELS` and still sends `reasoning_effort=none`.
Unit tests verify the payload and verify that Gemma does not receive the Qwen
directive. A live comparison remains necessary because soft-switch behavior
depends on the installed chat template.

Run:

```bash
./.venv/bin/python scripts/live_lmstudio_acceptance.py
```

Never point this script at real agreements.
