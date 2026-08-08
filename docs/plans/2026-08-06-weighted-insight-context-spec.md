# Weighted Insight Context Selection Spec

**Created:** 2026-08-06
**Status:** implemented; offline and pinned SenseTime gates passed
**Scope:** replace the fixed recent/judged insight prompt window with a deterministic,
provider-independent importance/relevance/diversity selector while preserving the complete durable
insight ledger.

## 1. Problem

The Phase 3 selector sends the latest 20 hypotheses plus the latest 20 judged/validated hypotheses.
It bounds prompt growth and passed the fixed SenseTime gate, but it has two blind spots:

1. an old, unjudged hypothesis highly relevant to today's awareness notes can be omitted;
2. several near-duplicate recent hypotheses can consume multiple prompt slots while an important,
   different theme remains invisible.

The production ledger currently contains 441 hypotheses. Prompt selection must improve coverage
without deleting, rewriting, summarizing, or model-merging that ledger.

## 2. Goals

- Keep a hard maximum of 40 model-visible existing hypotheses.
- Reserve space for recent hypotheses and explicit user/validation anchors.
- Prefer hypotheses relevant to the current awareness batch and effective profile.
- Preserve older high-confidence/evidence-backed hypotheses and topic diversity.
- Collapse only prompt-slot competition between semantically near-duplicate hypotheses; never
  mutate stored rows or combine evidence/verdicts.
- Stay deterministic and independent of tokenizer, embedding service, chat model, and provider.

## 3. Selection contract

### 3.1 Four bounded lanes

Selection uses source indices from the append-only durable list:

| Lane | Quota | Purpose |
|---|---:|---|
| judged/validated reserve | 8 | latest explicit user verdicts or validated anchors |
| recent reserve | 8 | newest hypotheses regardless of score |
| current relevance | 16 | strongest match to this awareness batch and effective profile |
| importance/diversity | 8 | confidence, evidence, recurrence, and uncovered themes |

Overlapping lanes de-duplicate by source index. Unused slots flow into a final weighted fill, still
bounded at 40. Histories shorter than the cap remain fully visible except for safe same-state
near-duplicate prompt competition.

### 3.2 Weighted score

The general score is calibrated as:

```text
35% current relevance
25% source-index recency
20% explicit verdict or validated status
15% confidence/evidence quality
 5% recurrence support
```

Current relevance is `80%` overlap with this awareness batch plus `20%` overlap with preference/soul
context. Text features use Unicode NFKC normalization, case folding, alphanumeric words, and CJK
bigrams with a small fixed generic-term stop set. No online embedding or model request is permitted.

Source-index recency uses a 40-hypothesis half-life because persisted `created_at` values may be
missing or legacy-formatted; the append-only order is the authoritative stable clock. Quality clamps
confidence to `[0,1]` and caps evidence contribution at three entries, matching the output contract.

### 3.3 Diversity and prompt-only grouping

Candidates receive a deterministic diversity penalty against already selected hypothesis text.
Near-duplicates above the fixed similarity threshold compete for one prompt slot only when their
semantic state is the same:

- `confirmed`: `validated=true` or `user_verdict=confirmed`;
- `rejected`: `user_verdict=rejected`;
- `unjudged`: neither condition.

Different states never collapse. In particular, a recent unjudged restatement cannot hide an older
user rejection, and a rejection cannot erase a confirmed anchor. The selected objects are original
`InsightHypothesis` instances; no synthetic summary, evidence union, support count, or field rewrite
is sent to persistence.

### 3.4 Stable output

Ranking decides membership only. The final selected rows are restored to original source order before
prompt rendering, keeping chronological context and byte determinism for identical inputs.

## 4. Storage and merge invariant

`CognitionCycle._run_insight()` loads the full ledger, passes only the detached selected view to
`InsightAnalyzer.analyze()`, then merges any output against the same full ledger and saves the full
result. A selector failure must fall back to the Phase 3 fixed recent/judged view rather than block
the cognition cycle or truncate persistence.

## 5. Acceptance gates

### Automated

- selection is deterministic, source-ordered, and never exceeds 40;
- latest and judged reserves survive unrelated high-scoring rows;
- an old hypothesis relevant to current awareness beats irrelevant middle history;
- old high-quality hypotheses receive importance slots;
- same-state near-duplicates do not monopolize slots;
- confirmed/rejected/unjudged conflicts remain separately visible;
- empty/small/malformed-text inputs are safe;
- full-ledger merge and user verdicts remain intact;
- no prompt system message, schema, storage format, provider route, or output budget changes.

### Offline token/coverage

On the frozen production snapshot, report selected lane coverage, relevance/anchor coverage,
prompt characters, and estimated provider-independent savings versus full history. Required: no more
than 40 rows and at least 35% prompt-character savings versus full history. The character proxy is a
coarse pre-provider guard: a large stable profile/awareness block is identical in every arm, while
Chinese tokenization does not track characters linearly. The authoritative real-provider prompt-token
floor remains 40% below and was not relaxed.

### SenseTime A/A+B

Use the same frozen awareness/profile/ledger snapshot:

- A1/A2/A: the shipped Phase 3 fixed recent-20 plus judged-20 selector;
- B: the weighted selector in this spec.

Pin `openai_compatible/deepseek-v4-flash`, temperature 0, single-flight, and no cross-provider
fallback. Required: complete usage, strict parse/schema, no repair or duplicate regression, structure
and evidence/confidence drift within A/A noise, and no route drift. B may spend more tokens than the
old bounded selector, but must retain at least 40% prompt-token savings versus the full-history
baseline measured on the same frozen snapshot.

## 7. Achieved evidence

The final read-only cohort contained 442 durable hypotheses and 20 awareness notes. Weighted
selection returned 40 original objects: all latest-eight anchors, 24 rows outside the fixed Phase 3
window, 13 of the lexical top-16 current-relevance rows, and zero same-state near-duplicate pairs.
Prompt rendering was `123089 → 74543` characters (`39.44%` below full history), while the extra
coverage cost only `3.27%` versus the fixed 20-row view.

The pinned SenseTime gate reported `48523 → 27725` prompt tokens (`42.86%` savings versus full) and
`26724 → 27725` (`3.75%` overhead versus fixed). Treatment B was a strict array with zero repairs,
9/9 valid structures, and zero duplicates. Confidence and mean-evidence drift versus A were
`0.019167 / 0.138889`, inside A/A noise `0.111667 / 1.5`; merging against all 442 durable rows yielded
444 without truncation. The passing privacy-safe artifact is
`data/eval/weighted-insight-context-sensetime-2026-08-06-aa-final.json`, SHA-256
`932c5d955b7449b88065e8a5aec408966e40e0c02c2fd8ee506ff11b68e75932`.

## 6. Documentation impact

Update soul/LLM module docs, architecture/spec/README diagrams, changelog, Phase 3 replay notes, and
tests. No configuration or API surface is added; the selector is a deterministic internal policy.
