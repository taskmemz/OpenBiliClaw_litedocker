# LLM Token Diet Phase 2 Spec

**Created:** 2026-08-05
**Status:** implementation contract frozen; 2026-08-06 task-scoped rollout recorded
**Scope:** cognition prompt views, demand-driven recommendation copy, evaluator prefilter
observability/enforcement gate, cache-prefix layout, replay artifacts, and end-to-end token
accounting.

## 1. Context and measured baseline

Phase 1 landed the production `sparse-json` evaluator transport, local result IDs, the
48-interest compact evaluation profile with tail recall, reason diet, a bounded process cache,
and candidate coalescing. Generic JSON minification, row-wire, body truncation, and removing the
evaluation reason either failed quality gates or failed to produce a reliable total-token win.

The next phase therefore removes work instead of compressing punctuation. The frozen,
pre-experiment local usage-ledger snapshot for the 30 days ending 2026-08-05 contains exactly
47,310,540 total tokens:

- `discovery.evaluate_batch`: 16,775,397 (35.46%);
- `soul.preference.chunk`: 8,616,124 (18.21%);
- `recommendation.write_expression`: 7,466,196 (15.78%);
- `soul.awareness_confusions`: 6,080,157 (12.85%);
- plain `soul.awareness`: 1,388,363 (2.93%);
- `soul.insight`: 2,298,859 (4.86%).

Later real-provider validation calls are excluded from this baseline rather than allowed to move the
denominator after the experiment starts.

Three production-data diagnostics define the opportunity without becoming acceptance evidence:

1. A current 300-event awareness prompt renders 267,571 characters. A conservative event/profile
   projection renders 123,381 characters (53.9% smaller).
2. A current 200-event preference input renders 135,377 characters. The same conservative event
   projection renders 64,170 characters; under the existing 24,000-character budget this changes
   the rough split count from six to three.
3. `content_cache` contains 7,014 rows with generated copy. 2,699 were never shown (38.5%); 1,569
   are already stale or purged without ever being shown (22.37%, a hard observed row-count waste
   floor). The canonical copy-ready count is currently 300; applying only the per-topic display cap
   would leave 317 candidates before the remaining serve gates.

Character counts are tokenizer-independent diagnostics, not claimed token savings. Acceptance uses
provider-reported usage from the configured SenseTime 日日新 route.

## 2. Goals

### G1. Reduce model-visible cognition input without removing evidence

Raw events remain lossless in SQLite. Preference, awareness, and insight prompts receive a named,
deterministic LLM view that removes transport/projection bookkeeping, parses serialized metadata
once, removes duplicated profile subtrees, and orders profile blocks from stable to volatile.

### G2. Generate recommendation copy only for near-term serve inventory

The candidate pool may remain deep while the copy-ready pool is bounded by a separate high
watermark. A card can never become serveable without validated, non-empty copy. Background refill
must preserve the existing fast popup path.

### G3. Make evaluator prefilter enforcement evidence-driven

`shadow` decisions must be durable and joinable with the eventual LLM score. `enforce` remains
disabled unless the frozen replay and real-provider gates prove admission recall. Explore and
failure cases remain fail-open.

### G4. Improve cache reuse without depending on a provider cache

Stable profile material precedes volatile cognition/events. Providers without prompt caching see
the same semantic input and remain fully supported. Application-side reuse is exact-input only and
never becomes a semantic approximate cache for personalised decisions.

### G5. Produce clean, privacy-safe, reproducible evidence

Unit/integration tests, frozen local replay, real SenseTime requests, route attribution, usage, and
quality metrics are recorded without API keys, cookies, raw private events, full profile text, or
candidate bodies.

## 3. Non-goals

- No target-model-specific tokenizer or compressor model.
- No body/title truncation in discovery evaluation or recommendation expression prompts.
- No removal of successful evaluation `reason`; the Phase 1 reason contract stays binding.
- No direct semantic-cache hit for evaluator scores, awareness notes, or preferences.
- No automatic `shadow -> enforce` transition from token savings alone.
- No change to admission thresholds, ranking semantics, or recommendation-card copy quality.

## 4. Design invariants

### I1. Raw truth stays lossless

The event ledger and profile layers are not rewritten for token savings. Compact views are pure,
non-mutating projections used only at an LLM prompt boundary.

### I2. Named profile views only

Every new profile serializer lives in `soul/profile_views.py`, is deterministic, and is registered
in `docs/profile-usage.md`. Prompt call sites may not hand-roll a partial profile.

### I3. Strong signals survive byte-for-byte

The cognition event view must preserve, when present:

- event identity, type, event time, title, human context, and inferred satisfaction;
- explicit positive/negative feedback and retraction state;
- source platform/source, creator/content identity, signal strength, completion/dwell evidence;
- search/dialogue content required to interpret the event;
- unknown semantic metadata unless it is on the explicit internal-field denylist.

The initial denylist is intentionally narrow: raw DOM/browser context, projection ownership,
namespace bookkeeping, ingest idempotency keys, and redundant classifier reason strings. A URL is
retained only when it is the sole useful content identity/context.

### I4. No hidden aggregation in the first production arm

Repeated-event aggregation is a separate replay arm. The first compact production view preserves
event count and order. This makes field projection independently reversible and prevents a token
win from hiding a temporal-quality regression.

### I5. Stable-to-volatile prompt order

System prompts remain module-level byte-static constants. User blocks are ordered:

1. stable soul identity/core/values/role;
2. stable preference/style/interests;
3. recent awareness and active insights;
4. the current event/note batch.

Alphabetical serialization of one monolithic soul object is forbidden because volatile
`active_insights` otherwise invalidates the prefix before stable material.

### I6. Copy readiness remains a hard serve gate

`pool_expression` and `pool_topic_label` must remain validated and non-empty before a row is
returned by any extension, desktop, mobile, CLI, proactive-push, or delight serve path. Watermark
logic controls scheduling only; it cannot weaken the gate.

### I7. Copy high watermark is bounded and refillable

The copy-ready target is independently configurable and clamped to the candidate pool target. A
low inventory notification schedules refill; concurrent refill remains protected by the existing
expression lock. Stale, suppressed, viewed, and purged rows do not consume copy budget.

### I8. Prefilter is fail-open

Missing embeddings, namespace/dimension drift, profile emptiness, an excessive predicted kill
rate, explore uncertainty, telemetry-write failure, or any audit inconsistency sends candidates to
the LLM. These conditions may reduce savings but may never reduce recommendation recall.

### I9. Exact reuse includes every semantic input

Any persistent exact-result key includes task contract version, normalized input digest, effective
profile digest, route/model namespace where result semantics can vary, and an explicit time bucket
for freshness-sensitive tasks. Failed, empty, repaired-from-incomplete, or degraded results are not
cached.

### I10. Provider independence

Compact views, copy watermarks, prefilter rules, and exact digests work with every configured model.
Prompt-cache savings are an optional adapter/provider bonus and are reported separately from raw
token reduction.

## 5. Design

### 5.1 `CognitionEventViewV1`

Add a single pure event projection used by preference and awareness builders. It parses a JSON
metadata string into a deterministic object, preserves unknown semantic keys, removes the narrow
internal denylist, and emits only non-empty top-level fields. It must not mutate input events.

The projected schema is intentionally readable JSON rather than a positional wire:

```json
{
  "id": 42,
  "event_type": "favorite",
  "created_at": "2026-08-05 10:00:00",
  "title": "...",
  "context": "...",
  "inferred_satisfaction": "positive",
  "metadata": {"source_platform": "bilibili", "bvid": "...", "signal_strength": 0.9}
}
```

Malformed metadata is retained as a bounded opaque value rather than silently dropped. Projection
statistics count removed fields and rendered characters, but never log their values.

### 5.2 `CognitionProfileViewV1`

Add a named view in `soul/profile_views.py` with stable and volatile blocks:

- stable soul: personality/core/values/role/surface fields that affect interpretation;
- preference: active interests, style/context, openness, disliked topics, favourite creators,
  cognitive style, source mix, and active speculative interests;
- volatile cognition: recent awareness and active insights.

The duplicated `soul.interest` subtree is omitted because the canonical preference block is sent in
the same prompt. Storage timestamps/version markers and init-only/archived bookkeeping are omitted.
The first production arm does not cap active interests or semantic negative evidence.

### 5.3 Preference, awareness-with-confusions, and insight builders

Builders accept an explicit `input_view` experiment seam (`legacy` / `compact-v1`). Production
composition selects the mode independently through `soul.preference_prompt_view`,
`soul.awareness_prompt_view`, and `soul.insight_prompt_view`; there is no aggregate
`soul.cognition_prompt_view` switch. Replay remains independent of rollout defaults and renders
both arms from the same frozen inputs. Both arms keep identical system instructions, output
schemas, max-token limits, reasoning settings, and route selection. Only the model-visible user
data projection/order may differ.

`soul.awareness_prompt_view` deliberately controls only
`AwarenessAnalyzer.analyze_with_confusions()` / `soul.awareness_confusions`, because that is the
caller exercised by the real replay. The separate `AwarenessAnalyzer.analyze()` /
`soul.awareness` path is pinned to `legacy` and cannot inherit this rollout without its own real
gate. The 2026-08-06 SenseTime gate therefore authorizes only Awareness-with-confusions for
`compact-v1`. Production defaults are Preference=`legacy`, Awareness-with-confusions=`compact-v1`,
Insight=`legacy`; each gated task can be rolled back or re-evaluated without changing the others.

### 5.4 Demand-driven expression copy

Separate these concepts:

- candidate target: raw/evaluated inventory retained for ranking diversity;
- copy-ready target: cards that can be served immediately.

The scheduler computes `needed = max(0, copy_ready_target - copy_ready_count)` and asks the existing
batch writer for no more than `needed`. Consumption and low-inventory notifications refill in the
background. The default target is large enough for multiple complete recommendation surfaces and is
clamped against `pool_target_count`; calibration provenance is documented beside the constant.

Recovery rules:

- a user request finding insufficient ready inventory triggers one immediate bounded refill signal,
  but never returns an empty-copy card;
- LLM/provider failure leaves rows durable and retryable;
- decreasing the target does not delete existing copy;
- disabling the optimization restores the legacy drain-to-backlog behaviour.

### 5.5 Prefilter audit and enforce gate

For every shadow decision, persist a privacy-safe record containing candidate identity hash,
platform/context class, similarity/threshold, explore flag, embedding namespace, profile digest,
would-filter flag, eventual LLM score, and admission result. Retention is bounded.

Enforcement may be enabled only when a frozen cohort and production-like SenseTime replay satisfy
all of §6.4. The existing kill-rate guard and explore exemptions remain.

### 5.6 Exact retry cache and cache-prefix metrics

First persist request digests and duplicate-rate telemetry. Exact result reuse is enabled only for
validated idempotent retries/restarts after duplicate incidence is measured. Provider-cache metrics
continue to record cached input separately; raw total tokens remain the primary diet metric.

## 6. Acceptance gates

### 6.1 Automated correctness

- New event/profile views are deterministic, pure, and covered by golden fixtures.
- Strong positive, explicit dislike, neutral comment, retraction, cross-platform, creator identity,
  dwell/completion, search, and dialogue fixtures preserve their semantic fields.
- Prompt system messages remain byte-invariant and compact blocks are stable-to-volatile.
- All structured outputs pass existing parser/normalization validation.
- Targeted tests, full `pytest`, Ruff, MyPy, and `git diff --check` pass.

### 6.2 Token gate

On frozen representative fixtures and real requests:

- awareness-with-confusions compact median prompt tokens improve by at least 30%;
- preference compact total tokens per consumed event improve by at least 25%;
- copy tokens per shown card improve by at least 15% after inventory reaches steady state;
- repair/fallback call count may not increase by more than the control A/A envelope.

Cached-token discounts do not count toward these raw-token gates.

### 6.3 Cognition quality gate

Compare control A1/A2 variability with control/treatment A/B on frozen event cohorts.

- parse success and schema validity: 100% for both arms;
- explicit dislike/retraction handling: no treatment miss;
- favourite creator copied from evidence: no hallucinated creator and no treatment evidence loss;
- top-interest weighted overlap, style-field drift, awareness-note count, and insight hypothesis drift
  must remain inside the control A/A envelope or a predeclared small tolerance;
- every emitted awareness `source_event_id` is in the supplied event set;
- blind rubric review finds no treatment-only critical omission or unsupported conclusion.

If a task's token savings or quality gate fails, that task remains `legacy` and the artifact records
the failed arm; another task's passing result cannot authorize it.

### 6.4 Prefilter quality gate

On at least 100 joinable candidates, stratified by platform/source and including explore:

- admission recall for would-filtered decisions is at least 99%;
- absolute high-score false negatives are at most one and none may be an explicit strong-interest
  or explore candidate;
- every platform stratum with at least 20 observations has at least 95% admission recall;
- telemetry coverage is 100% for decisions used by the gate;
- any missing/degraded embedding case is observed to fail open.

Otherwise the production mode remains `shadow`.

### 6.5 Copy readiness gate

- all four user surfaces and proactive push return only non-empty validated copy;
- a steady-state real run keeps copy-ready inventory at the configured target without draining the
  entire admitted backlog;
- refill is idempotent under concurrent notifications;
- provider failure preserves pending rows and the next successful run resumes them;
- popup/API response latency does not regress beyond the existing test envelope.

### 6.6 Real-provider route gate

The final A/A and A/B calls use the configured SenseTime 日日新 OpenAI-compatible instance directly.
Artifacts record actual instance/model for every call. Any fallback, mixed route, missing usage,
rate-limit recovery that changes the arm, or provider error invalidates that run; it is retried or
reported as failed evidence, never averaged into quality results.

### 6.7 Evidence recorded on 2026-08-06

The cognition replay made 12 direct requests to the configured SenseTime 日日新
`openai_compatible` instance/model `deepseek-v4-flash`: A1/A2/A/B for each task. Every call stayed
on that one route, returned provider usage, and used no fallback.

| Task | Control A prompt / total | Compact B prompt / total | Prompt / total saving | Quality result | Production view |
| --- | ---: | ---: | ---: | --- | --- |
| Preference | 65,035 / 74,116 | 45,555 / 54,291 | 29.95% / 26.75% | **Fail:** weighted top-interest overlap 0.481, below the 0.826 floor | `legacy` |
| Awareness-with-confusions | 101,913 / 105,157 | 64,095 / 66,456 | 37.11% / 36.81% | **Pass:** count/attribution/schema gates and blind review pass; no critical compact-only omission | `compact-v1` |
| Insight | 23,529 / 26,177 | 11,384 / 14,487 | 51.62% / 44.66% | **Fail:** evidence-count drift 1.4 exceeds 0.7; token threshold was also not predeclared | `legacy` |

The safe measured Awareness-with-confusions change therefore removes about 2.24 million tokens from
the frozen 30-day mix if cadence/input mix remains comparable, or about **4.73% of all raw tokens**.
This is a mix projection from provider-measured per-task savings, not a promise about future traffic.

The real copy-watermark E2E started with `copy_ready=2` and three durable pending rows, served one
card, issued demand 1, generated exactly one replacement through the same SenseTime route, and ended
at `copy_ready=2` with two rows still pending. It proves bounded refill, real provider wiring, and the
non-empty serve gate without draining the backlog. The historical 22.37% stale/purged-unshown row
rate is an opportunity estimate, not yet a longitudinal token-per-shown-card result; varying copy
costs mean row share must not be reported as exact token savings.

The production database had no joinable prefilter audit cohort at evaluation time. The read-only gate
therefore failed closed for sample size, coverage, recall and degraded fail-open evidence, and
`eval_prefilter_mode` remains `shadow`. Prefilter contributes zero claimed savings until a later
cohort of at least 100 joinable decisions passes §6.4.

## 7. Metrics and artifact contract

Primary metrics:

- raw prompt/completion/total tokens;
- tokens per consumed event, evaluated candidate, admitted candidate, generated copy, and shown card;
- cached input tokens and uncached input tokens (separate cost/cache view);
- compact rendered characters by named block;
- structured parse/repair/fallback counts;
- copy generated/shown/stale-unshown counts;
- prefilter would-drop, actual score, admission recall, and fail-open counts.

Artifacts include commit, dirty flag, contract versions, sanitized route/model, frozen-input digests,
cohort sizes, aggregate metrics, gate constants, and pass/fail reasons. They exclude prompt text,
profile/event bodies, API keys, cookies, URLs, and raw provider responses.

Real cognition artifacts also include machine-readable `gate.task_rollout` entries for
`preference`, `awareness_confusions`, and `insight`. Each entry reports its config field, task-local route,
token and quality results, blocking reasons, `compact_v1_enabled`, and final `selected_view`.
Task quality includes task-local parse/schema and repair envelopes, so one failed task cannot block
another task that independently passed. The original aggregate gate remains authoritative for the
full-compact experiment and is not rewritten to pass when only one task is enabled. Insight remains
fail-closed until a token threshold is predeclared rather than inferred after seeing evidence.

## 8. Rollout and rollback

1. Land views and telemetry with explicit legacy/compact seams.
2. Run deterministic replay and targeted/full tests.
3. Run SenseTime A/A then A/B. Do not reuse a prior provider's evidence.
4. Apply §6.2–§6.3 independently per cognition task. On 2026-08-06 only
   `soul.awareness_confusions` passed its SenseTime gate, so `awareness_prompt_view` defaults to
   `compact-v1` for that caller while plain `soul.awareness`, `preference_prompt_view`, and
   `insight_prompt_view` remain `legacy`.
5. A later Preference, plain Awareness, or Insight rollout requires a fresh passing task-scoped
   artifact; the Awareness-with-confusions result cannot be reused as authorization.
6. Enable copy watermark after §6.5 passes.
7. Keep prefilter in shadow until §6.4 passes; enable gradually if it passes.
8. Each cognition config field rolls back independently to `legacy`; copy rollback remains
   independent and requires no data migration.

## 9. Documentation impact

The implementation must update:

- `docs/profile-usage.md`;
- `docs/modules/soul.md`, `docs/modules/recommendation.md`, `docs/modules/discovery.md`,
  `docs/modules/llm.md`, `docs/modules/config.md`, and `docs/modules/storage.md` as applicable;
- `docs/changelog.md`;
- architecture/spec/README diagrams only if final wiring crosses or adds module boundaries;
- `config.example.toml` for rollback/watermark settings.
