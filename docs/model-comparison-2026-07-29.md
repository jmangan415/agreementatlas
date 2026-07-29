# Hard-suite adjudication — 29 July 2026

Suite: `scripts/bench_cases_hard.json` (20 questions, 27 mechanical checks, four
published families). Three full runs of 4 configs (Gemma 4 26B local / GPT-5
cloud × structured reading on/off), same graph-retrieved evidence per question.
Raw tables: `tmp/model_bench_hard_2026-07-29.txt` (pre-tune),
`tmp/model_bench_hard_posttune.txt` (fact-application line),
`tmp/model_bench_hard_final.txt` (shipping prompt). Full answers:
`tmp/bench_hard_final_answers.json`.

## Instrument corrections applied by hand

- `okta-dr-rto` (`must_not: "hours"`): every config in every run answered
  correctly and completely ("no more than five minutes to enable read-only
  access and no more than 24 hours for full-service restoration") and was
  docked for the word "hours". Check is wrong, not the models. +1 wherever it
  fired.
- `ot-two-calendar-days` keyword `"two"` can pass an answer that refuses or
  answers "one day" while quoting text containing "two". Каught twice:
  pre-tune Gemma both configs ("undecided", false pass), final-run Gemma
  reading-off ("counted as one day", false pass). Corrected by reading.

## Final run (shipping prompt), adjudicated

| config | raw | adjudicated | nature of remaining misses |
|---|---|---|---|
| Gemma 4 + reading | 25/27 | 26/27 | Okta MSA: safe refusal, omits the Order Form pointer |
| Gemma 4 − reading | 26/27 | 26/27 | two-calendar-days answered "one day" — confidently wrong |
| GPT-5 + reading | 26/27 | 27/27 | none |
| GPT-5 − reading | 26/27 | 27/27 | none |

Wall clock: Gemma ~95s for 20 questions; GPT-5 ~320s. All retrieval and
embeddings local in every config.

## Findings

1. **Parity within variance.** Across three runs Gemma+reading scored 26–26–26
   adjudicated; GPT-5 25–27–27. Gemma beat GPT-5 in run one. Run-to-run
   variance is ±1–2 checks, dominated by two known extraction defects (the
   Salesforce B2B two-column table, the OpenText EULA clause-boundary bleed),
   not by model choice.
2. **The reading changes the failure mode, not the score.** In every run,
   Gemma without readings produced at least one confidently wrong answer
   (Work Orders inversion; "one day"). Gemma with readings produced zero
   confidently wrong answers across all three runs — its misses were explicit
   refusals. Small n; stated as an observed pattern, not a law.
3. **Prompt lines that were measured in:** fact-application (fixed the
   two-day punt with reading on; no regressions), mechanism-first opening and
   scope-bounded precedence (fixed the assign/allocate conflation live; bench
   unchanged). The licence-model-definition line fixed the Production CPU
   follow-up.
4. **Saturated suites lie.** The original 7-case suite scored 14/14 for
   everything and showed none of the above.
