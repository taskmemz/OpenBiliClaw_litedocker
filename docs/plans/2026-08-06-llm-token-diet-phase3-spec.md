# LLM Token Diet Phase 3 Spec

**Created:** 2026-08-06
**Status:** implemented; automated and fixed SenseTime real-provider gates passed
**Scope:** bounded insight context, request-shape-aware preference batching, and
cross-digest keyword-inventory grace.

## 1. Context and measured opportunity

Phase 2 safely authorized `compact-v1` only for `soul.awareness_confusions`. The next largest
provider-independent opportunities are repeated work rather than JSON punctuation.

The frozen steady-window ledger for 2026-07-31 through 2026-08-05 contains 4,516,233 total tokens:

- `soul.preference.chunk`: 1,056,126 (23.39%);
- `soul.awareness_confusions`: 883,132 (19.55%);
- `discovery.evaluate_batch`: 680,924 (15.08%);
- `soul.insight`: 603,176 (13.36%);
- `discovery.keyword_planner`: 442,367 (9.80%);
- `recommendation.write_expression`: 410,578 (9.09%).

Three aggregate diagnostics define this phase:

1. The insight store has 441 hypotheses. The existing-hypothesis prompt block is 79,558
   characters; the latest six are 1,075 characters and the latest twenty are 3,724 characters.
   Storage and merge need the full history, but generation only needs bounded active/judged context.
2. Forty logical preference bursts produced 136 `soul.preference.chunk` calls (3.4 calls per
   burst). Budget fallback estimates chunk size from a prompt containing the full existing
   preference, while the actual independent chunk sends `existing_preference={}`. The estimator is
   therefore measuring a different request from the one it schedules.
3. The keyword planner generated 1,432 rows in the steady window. 1,344 (93.9%) expired unused,
   only six rows produced admitted supply, and 37 profile keyword digests lived for a mean 0.78
   hours. The planner hard-expires pending rows at every digest change even though the digest is
   already quantized and most inventory remains recent.

These diagnostics are not acceptance evidence. Token acceptance uses provider-reported usage from
the configured SenseTime 日日新 route, and behaviour acceptance uses matched frozen inputs.

## 2. Goals

### G1. Bound insight prompt growth without deleting history

Keep the complete hypothesis ledger for persistence, merge, user settlement, and audit. Send only a
bounded prompt context consisting of the recent tail plus a bounded tail of user-judged/validated
hypotheses.

### G2. Size preference chunks from the request that will actually be sent

Explicit init chunk sizes remain unchanged. Budget-triggered incremental fallback computes the
largest fitting independent chunk from `existing_preference={}`, which is the exact prompt shape
used by `_run_chunk_once`. Provider overflow and invalid-output recursive splitting stay intact.

### G3. Reuse safe, recent keyword inventory across profile digest churn

Pending regular keywords from a previous profile digest remain eligible for a short grace window.
They retain their original digest/provenance. Current dislikes and current per-platform saturated
topics block reuse. Old, blocked, or above-high-water stale rows expire before due calculation.

### G4. Stay provider and tokenizer independent

No target tokenizer, compressor model, model cascade, provider-specific cache API, or assumed context
window is introduced. Character budgets remain the existing provider-independent safety boundary;
actual savings come from provider usage telemetry.

## 3. Non-goals

- No change to the Phase 2 cognition event/profile view rollout.
- No deletion, compaction, or rewriting of `awareness.json` or `insight.json` history.
- No change to preference/insight output schemas, merge rules, temperatures, reasoning settings, or
  model routes.
- No semantic or approximate result cache.
- No reuse of keywords that match an explicit dislike or the current platform avoid set.
- No change to evaluator admission, recommendation ranking, or copy quality contracts.
- No event aggregation in this phase; the measured 53.4% passive-row opportunity remains a separate
  quality-gated arm.

## 4. Design invariants

### I1. Full history remains authoritative

The complete insight list is loaded once per batch and remains the merge target. Prompt selection is
a detached bounded view only. A generated hypothesis can still update any exact historical match
through the existing full-history merge.

### I2. Insight context is deterministically bounded

The prompt includes the latest 20 hypotheses plus the latest 20 hypotheses carrying a non-empty
`user_verdict` or `validated=true`, de-duplicated by source index and emitted in original order. The
hard maximum is 40. The constants record the 2026-08-06 calibration and must be re-opened if the
hypothesis lifecycle or provider changes.

### I3. Explicit preference chunk sizes are frozen

`event_chunk_size > 0` keeps its existing init/rebuild contract. Only the automatic
`max_prompt_chars` fallback changes. This prevents a steady-state diet from silently changing init
fan-out and progress reporting.

### I4. Every scheduled independent preference chunk fits the local budget or uses existing recovery

Automatic sizing renders the same static system instruction, cognition tail, empty preference seed,
and event prefix used by the actual independent call. It chooses the largest fitting prefix by
deterministic binary search. If one event still exceeds the budget, the existing compact/safe retry
path remains the sole recovery owner.

### I5. Keyword provenance never changes

Grace reuse does not rewrite `profile_kw_digest`, inspiration IDs, source interests, angles, or
generation reasons. Yield continues to credit the generation cohort that produced the keyword.

### I6. Keyword grace is bounded and reversible

`discovery.keyword_digest_grace_hours` is configurable in `[0, 168]`; `0` restores hard expiration.
The rollout default is 24 hours, calibrated from the observed 0.78-hour digest lifetime and bounded
well below the existing multi-day keyword history. Total pending regular inventory remains capped at
the planner's dynamic high-water target.

### I7. Strong negative safety wins over reuse

Before due calculation, reconciliation expires stale pending keywords when their normalized text
contains a current explicit disliked topic or a current per-platform `avoid_topics` entry. Claimed,
executing, used, failed, and explore-keyword rows are untouched.

### I8. Failure preserves the legacy safe path

If keyword reconciliation/counting is unavailable or raises, the planner logs the cause, uses the
existing exact-digest count, and performs legacy stale-digest expiration. A reconciliation failure
cannot cause an unbounded cache or suppress needed generation.

### I9. Prompt-cache invariance remains intact

No system prompt changes. Stable system messages and deterministic JSON ordering remain byte
invariant. Reduced calls and shorter user blocks are raw-token wins; provider cache hits are reported
separately.

## 5. Detailed design

### 5.1 Insight prompt context selector

Add a pure selector beside the cognition-cycle constants. `_run_insight` loads the full list once,
passes the selected context to `InsightAnalyzer.analyze`, and merges the response into the unchanged
full list. Tests cover empty/small histories, a 441-row synthetic history, judged rows outside the
recent tail, stable ordering, de-duplication, and full-history merge preservation.

### 5.2 Preference independent-chunk sizing

Replace the proportional estimate used only by automatic budget fallback with a render-based largest
fitting prefix search at every remaining event offset. Rendering uses
`build_preference_analysis_prompt` with the actual independent seed and the same cognition arguments.
The outer full-profile prompt still determines whether the normal incremental call fits; the greedy
per-offset searches determine how to schedule its independent fallback without making later skewed
events inherit the first prefix's uniform width.

The existing explicit chunk path, concurrency bound, provider-context split, invalid-JSON isolation,
single-event compact retry, normalization, and merge remain unchanged.

### 5.3 Keyword pending-inventory reconciliation

Add one short `BEGIN IMMEDIATE` storage operation per platform and planner pass. For regular pending
rows it:

1. keeps all current-digest rows, subject to the existing dynamic high-water;
2. considers previous-digest rows newest-first;
3. retains only rows younger than the grace cutoff and not matching blocked terms;
4. retains no more than the remaining high-water capacity;
5. expires aged, blocked, and excess stale rows;
6. returns aggregate current/reused/expired counts without keyword text.

The planner performs reconciliation before `_due_platforms`, counts all reconciled regular pending
rows as usable inventory, and passes the already-built avoid hints into generation. The fetch owner
already claims pending rows independently of digest, so no consumer/API contract changes.

Pending rows join exact/family cooldown history while grace is enabled, preventing the LLM from
regenerating a formatting-equivalent query under the new digest.

### 5.4 Observability

Expose an in-memory `last_digest_grace_ledger` and one aggregate log line per productive
reconciliation with `current`, `reused`, `expired_aged`, `expired_blocked`, and `expired_excess`.
Never log keyword text, profile fields, or digests. Existing `llm_usage` remains the source for raw
token savings.

## 6. Acceptance gates

### 6.1 Automated correctness

- Insight prompt context never exceeds 40 and full persisted history is preserved.
- Preference automatic chunks use the largest fitting independent prefix and every rendered
  top-level chunk stays within `max_prompt_chars` before provider recovery.
- Keyword grace keeps recent safe stale rows, expires aged/blocked/excess rows, preserves provenance,
  respects high-water, isolates keyword kinds, and rolls back exactly at zero hours.
- Planner failure falls back to legacy expiration/counting.
- Existing prompt system-message invariance tests remain green.
- Ruff, MyPy, targeted tests, full `pytest`, and `git diff --check` pass.

### 6.2 SenseTime insight A/A + A/B

Run identical frozen awareness-note batches against:

- A1/A2: full historical existing-insight context;
- B: bounded recent+judged context.

Required: one direct SenseTime route, no fallback, complete provider usage, parse success, no increase
in repair rate, no evidence-count regression beyond A/A noise, no duplicate-hypothesis regression,
and a privacy-safe aggregate review pass. Raw model bodies are inspected in-memory by production
parsers but are neither persisted nor surfaced by the harness. Automated token floor: prompt tokens
at least 35% below A; stretch target: at least 50% below A median.

### 6.3 SenseTime preference A/A + A/B

Run identical incremental event bursts with unchanged prompt fields/output schema:

- A1/A2: current automatic chunk estimator;
- B: request-shape-aware largest-fit estimator.

Required: weighted top-interest overlap at least the existing 0.826 floor, disliked/favourite creator
retention, bounded style/context drift within A/A noise, parse/repair parity, one route with complete
usage, and no provider fallback. Automated token floor: prompt tokens at least 35% below A. Target:
total tokens at least 40% below A median on the fan-out cohort.

### 6.4 Keyword replay and live E2E

Run reconciliation on a disposable copy of the production database and verify aggregate reuse,
blocked-term exclusion, provenance stability, and high-water convergence. Then execute a real planner
cycle against SenseTime with a frozen current profile and controlled stale inventory.

Required: reusable inventory suppresses the LLM call when it satisfies the deficit; an explicit
dislike forces generation or safe fallback rather than reuse; claimed keywords retain their original
provenance; downstream fetch/admit remains functional. Target: at least 65% fewer planner LLM calls on
the frozen six-day replay estimate.

### 6.5 End-to-end rollout rule

Each feature has an independent rollback and verdict. No token result can override a failed quality
gate. Production defaults change only for independently passing features; failed arms stay disabled
while passing arms remain shippable.

## 7. Documentation impact

Update `docs/modules/soul.md`, `docs/modules/llm.md`, `docs/modules/discovery.md`,
`docs/modules/storage.md`, `docs/modules/config.md`, `config.example.toml`, and
`docs/changelog.md`. The data flow remains within existing module owners; the top-level architecture
description is updated to make the analyzer-to-builder prompt boundary and reconciliation order
explicit, without introducing a new cross-module owner.

## 8. Validation evidence (2026-08-06)

The privacy-safe frozen artifact is `data/eval/token-diet-phase3-sensetime-2026-08-06.json` and was
produced with one pinned `openai_compatible/deepseek-v4-flash` chain, temperature 0, single-flight
requests, and no cross-provider fallback. Its SHA-256 is
`214bba7c319a31259d39a6e7d3d21f2c5b4b1250ebdc16f88ff4ab73fa88ebca`.

- Preference A/B: 2 calls / 7,211 prompt / 11,103 total tokens became 1 call / 3,986 prompt /
  6,500 total (`44.72%` prompt and `41.46%` total savings). Weighted top-interest overlap was 1.0;
  creator loss, creator hallucination, and non-strict JSON counts were zero.
- Insight A/B: 47,321 prompt / 49,680 total tokens became 25,571 prompt / 29,135 total (`45.96%`
  prompt and `41.35%` total savings). Completion increased from 2,359 to 3,564 but did not offset the
  input reduction. Hypothesis count/evidence/confidence stayed within the measured A/A envelope,
  duplicates remained zero, and full-ledger merge preserved all 441 durable entries before adding
  one new entry.
- Keyword control/treatment: 1 planner call / 9,098 tokens became zero calls/tokens (`100%`). The
  disposable treatment retained 30 safe rows; real Bilibili search returned 15 raw / 13 unique
  candidates, 12 were evaluated, 7 admitted/cached, and all 3 claimed keywords reached used + yield
  attribution with original provenance.
- All automated gates and the aggregate real-provider gate passed. Two evaluator wire attempts
  required the same provider's `response_format` compatibility retry; they do not enter the isolated
  planner-call savings numerator and did not cause provider-route fallback.
