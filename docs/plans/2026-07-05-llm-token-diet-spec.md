# LLM Token Diet Spec — heavy-user cost scaling

> **2026-08-03 landing correction:** Phase 0's original standalone gate description is
> historical. Use the production-equivalent repeated gate in
> [`2026-08-03-llm-token-diet-landing-hardening-spec.md`](./2026-08-03-llm-token-diet-landing-hardening-spec.md);
> the former independent-snapshot / absolute-threshold evidence is invalid. The proposed 200+100
> `body_text` cap failed the strict Reddit 100×3 gate and was completely reverted.
>
**Created:** 2026-07-05
**Scope:** discovery evaluation prompt/cache/fallback, recommendation expression & classification
prompts, candidate-pipeline eval batching, LLM module-tier routing, prompt content caps
**Out of scope:** keyword/inspiration redesign (owned by `feature/discovery-inspiration-mvp`),
scheduler cadences, soul-pipeline prompts, multimodal evaluation, any system-prompt text change.

## Goal

The architecture already keeps **call counts** sub-linear in usage (interval-gated soul drains,
45-item eval batches, DB-status dedup). What still scales badly for a heavy user is
**tokens-per-call**: every content-eval / expression call carries the full
`build_profile_summary` block (up to 128 interest domains × 30 specifics + 256 tags + 128
dislikes ≈ 5–12k tokens once the profile matures), and two degenerate paths (per-item fallback,
trickle batches) multiply how often that block is paid. Target outcomes:

- Hot-path (eval / classification / expression) input tokens per call **−50% or better** on a
  mature profile, with **quality regression measured, not assumed**: every change to
  model-visible inputs must pass the golden-set replay gate (Phase 0) before merge, and the
  compact profile is paired with a per-item relevant-interest recall field so long-tail
  interests stay visible to the judge exactly when they matter.
- Batch-eval failure worst case drops from *N single calls* to *O(log N) split calls*.
- Obviously-irrelevant candidates never reach the LLM (free local-embedding pre-filter on the
  batch path, matching what the single path already does).
- Every LLM caller is reachable by a `[llm.<bucket>]` model-tier override; a user can put the
  entire hot path on a flash-tier model with config only.

Verification metric: `openbiliclaw cost --by caller` (tokens/call and cache-hit% per caller)
before vs after, plus `evaluation_profile_prompt_cache_stats` (`discovery/engine.py:1618`).

## Design invariants (MUST hold in every phase)

1. **Prompt-cache convention** (CLAUDE.md): system prompts stay byte-identical constants; all
   per-call data in user message ordered most-stable-first; `json.dumps(..., ensure_ascii=False,
   indent=2, sort_keys=True)`. `tests/test_llm_prompts.py::test_prompt_builder_system_messages_are_call_invariant`
   must keep passing.
2. **Profile trimming is a pure function of profile state** — the trimmed block may only change
   when the profile changes, never per batch. *Per-batch relevance trimming is explicitly
   REJECTED*: it would vary the profile block on every call, defeating provider prefix caching
   (DeepSeek/Claude 90% off cached input makes a stable 3k-token block cheaper than a varying
   1.5k one), thrashing `PromptLayerRenderCache`, and churning `profile_digest`.
3. **Eval-cache correctness:** the `_batch_eval_cache_key` profile digest must cover **exactly**
   the prompt-visible profile slice. Churn reduction is achieved by pruning volatile fields *out
   of the prompt slice*; never by digesting less than what the model sees.
4. **Disliked topics are never cut below the store cap** (existing invariant,
   `discovery/strategies/_utils.py:25-29` — legacy entries are alphabetically ordered, so any
   cut would drop topics by codepoint, not relevance).
5. **Rate-limit errors always propagate** — no split-retry, no per-item fallback on rate limit
   (existing behavior `discovery/engine.py:1780-1787`; must survive the fallback redesign).
6. **Explore-strategy candidates are exempt from embedding pre-filter** (cross-domain discovery
   is intentionally outside the interest neighborhood, `discovery/engine.py:1187-1189`).
7. **Measure before you cut (quality gate):** any phase that changes what the model sees
   must pass the golden-set replay gate before merge. Phase 7's body-text experiment failed and
   was removed; the historical absolute thresholds below are superseded by the repeated relative gate:
   admission flip rate ≤ 3%, Spearman rank correlation ≥ 0.95 vs the full-profile baseline on
   ≥100 real evaluated candidates. The embedding pre-filter (Phase 2) ships in **shadow mode
   first** — it logs what it *would* filter without filtering; it is flipped to enforce only
   after shadow data shows the would-be-filtered set contains ~zero admission-worthy items.

## Current diagnosis

### D1. Eval profile compactor exists but is dead code

`compact_evaluation_profile_summary` (`discovery/engine.py:67`, experimental caps: 20 core / 48 interests /
32 domains × 16 specifics / 12 recent-awareness / 8 evidence / 12 speculations; dislikes
untouched) is implemented and unit-tested
(`tests/test_discovery_engine.py:181`) — but `_evaluation_profile_summary`
(`discovery/engine.py:1605-1607`) still returns the full `build_profile_summary` (128×30 / 256 /
128 caps, `_utils.py:22-29`). No production caller uses the compactor. The single-eval path
(`engine.py:1204`) also feeds the full summary.

### D2. Expression & classification feed the full summary too

`_recommendation_profile_summary` (`recommendation/engine.py:55-70`) delegates straight to
`build_profile_summary`; batch expression (`recommendation/engine.py:1365`) and pool-backlog
classification (`_classify_batch`) both carry the full block.

### D3. Embedding pre-filter only guards the near-dead path

The cosine < 0.3 pre-filter lives only in single-item `evaluate_content`
(`discovery/engine.py:1172-1199`) — which is a fallback path. The main path
`evaluate_content_batch` (`engine.py:1287`) sends every uncached candidate to the LLM.

### D4. Batch failure degrades to per-item calls

Parse failure (`engine.py:1794-1798`) and ID-less count mismatch (`engine.py:1801-1811`) both
fall back to *one LLM call per item*. Production incident 2026-06-30: 94 `evaluate_single` calls
in 2.5h (¥0.9) from one bad-output streak. The correct pattern already exists for expressions:
`_precompute_batch_with_split_retry` (`recommendation/engine.py:1510-1537`) halves the batch
recursively.

### D5. `_eval_cache` is unbounded and over-invalidated

Plain dict (`engine.py:719`). Batch keys embed
`profile_digest = digest(full build_profile_summary)` (`engine.py:1600-1603`), so any profile
churn (one new awareness note) orphans every entry. Entries are never evicted → slow leak.

### D6. Eval-drain coalescing exists but is off and unwired

`min_eval_batch_size=1`, `max_eval_wait_seconds=0.0` (`discovery/candidate_pipeline.py:60-61`);
the full wait machinery is implemented (`candidate_pipeline.py:765-800`) but neither constructor
(`cli.py:8889`, `api/runtime_context.py:642`) passes values and no config field exists. Trickle
candidates produce tiny batches, each paying the full profile-block overhead.

### D7. `body_text` is uncapped in prompts (cap proposal later rejected)

Eval batch (`engine.py:1675`), eval single (`engine.py:1210`), batch expression
(`recommendation/engine.py:1361`) all pass raw `body_text`. Empty for bilibili videos, but
unbounded for text sources (X threads, zhihu) — a latent token bomb.

### D8. Six callers bypass model-tier routing

`_ROUTE_BUCKET_PREFIXES` (`llm/service.py:244-257`) misses: `discovery.keyword_planner`,
`discovery.x.keyword_gen`, `discovery.douyin.keyword_gen`,
`runtime.bilibili_extension_search.queries`, `pool_purge.llm_agent` (`soul/pool_purge.py:201`),
`api.sentiment` (`api/app.py:4586`). They always use `[llm].default_provider` even when bucket
overrides are configured.

### D9. Pool-backlog classification batch is 10

`classify_pool_backlog(batch_size=10)` (`recommendation/engine.py:1024`). Note: this is a
**legacy/recovery path** (docstring `:1026-1032`) — normal ingest classifies inside
`discovery.evaluate_batch` — so this is a cheap tweak, not a major lever (correcting the
original analysis that framed it as per-wave hot path).

## Priority classification

| Phase | Content | Tier | Why |
| --- | --- | --- | --- |
| 0 | Golden-set replay gate (A/B re-score script + numeric acceptance) | **MUST** | The quality insurance every input-changing phase is gated on; also validates future model-tier switches |
| 1 | Wire eval profile compactor + per-item interest recall + prune volatile fields | **MUST** | Biggest token lever; compactor already written & tested, this is wiring + a recall field for long-tail safety |
| 2 | Embedding pre-filter on the batch eval path (shadow → enforce) | **MUST** | Free local compute replacing paid LLM judgments; shadow rollout makes false-negative risk observable before it bites |
| 3 | Eval batch split-retry fallback (halving, floor 5) | **MUST** | Kills the 94-single-call failure mode; template already exists; zero quality surface |
| 4 | Complete `_ROUTE_BUCKET_PREFIXES` + config tiering guidance | **MUST** | Tiny change; unblocks config-only flash-tier routing for the whole hot path |
| 5 | Expression / classification profile diet (share compactor) | RECOMMENDED | Same lever as Phase 1 on the second-largest prompt family; expression side additionally gated on a side-by-side sample |
| 6 | Eval-drain coalescing config (`eval_min_batch_size` / `eval_max_wait_seconds`) | RECOMMENDED | Machinery exists; needs config plumbing + sane defaults; no quality surface (delay-only) |
| 7 | Hygiene: `_eval_cache` LRU bound, rejected `body_text` head+tail experiment, classify batch 10→30 | PARTIAL | LRU and batch-size changes remain; strict replay rejected the body-text cap |

Dependencies: Phases 1 and 5 are gated on Phase 0's replay verdict. Phase 7's body-text cap was
evaluated under that rule and rejected.
Phase 5 depends on Phase 1 (shared compactor relocation). Phases 2, 3, 4, 6 are otherwise
independent of each other and of Phase 1.

**Recommended implementation order — zero-quality-surface work first:**

- **Wave A (no change to what the model sees or decides; ship freely):** Phase 3
  (split-retry), Phase 4 (routing), Phase 6 (coalescing), Phase 7's cache LRU + classify batch
  size, and Phase 2's *code* (default `shadow` is behavior-neutral — the quality decision is
  the later `enforce` config flip, made on shadow data).
- **Wave B (quality-gated, in order):** Phase 0 (gate infra) → Phase 1 (compact + recall,
  replay-gated) → Phase 5 (expression/classification diet, replay + side-by-side). The later
  Phase 7 body-text experiment failed its text-source gate and was rolled back.

Every Wave B step lands with its gate evidence in the PR; work can stop after any wave with
all shipped value retained.

## Phase designs

### Phase 0 — Golden-set replay gate

A standalone script (`scripts/run_profile_diet_ab.py`, following the `scripts/run_*_eval.py`
conventions) quantifies quality drift from any prompt-input change using **real data from
the local DB** — no synthetic personas. The current implementation runs repeated A/A and A/B
pairs over one frozen production-mix snapshot and requires a JSON evidence artifact:

1. Sample N ≥ 100 recently `evaluated` discovery candidates (mixed strategies/platforms) plus
   the current profile.
2. Re-score them batch-wise through `evaluate_content_batch` twice — arm A: baseline profile
   input; arm B: candidate change (compacted summary, capped body_text, or a different
   `[llm.evaluation]` model) — same provider, same negative exemplars, eval cache bypassed.
3. Report: mean / p95 `|Δscore|`, **admission flip rate** (items crossing their per-strategy
   threshold, `candidate_pipeline.py:33-44`), Spearman rank correlation, plus the top-10
   largest regressions with titles for eyeballing.

Gates (invariant 7): flip rate ≤ 3%, Spearman ≥ 0.95 — script-level module constants, so a
stricter bar (flip ≤ 1%) is a one-line change if desired. A failing gate means tune the caps
(e.g. raise `_EVAL_PROFILE_DOMAIN_CAP`) and re-run — not ship-and-hope. Cost per run: ~4-6
batch calls (~¥0.1), cheap enough to run on every candidate change and on every future
model-tier switch.

**Measured revision (2026-07-05, Phase 1 acceptance).** A/A control runs (identical inputs
both arms, same model) showed the provider's single-sample noise floor alone is flip rate
17–28%, Spearman 0.57–0.67, signed drift up to ±0.05, admission-rate swing up to ±11pp —
temperature pinning did not reduce it (gateway/model-side nondeterminism). The absolute gates
above are therefore unattainable for *any* change, including production-vs-itself. Operative
gate: **run an A/A control the same day, then require (a) the candidate change's flip rate /
Spearman / mean `|Δ|` to sit inside the A/A envelope, and (b) the noise-robust drift metrics
(mean signed delta, admission-rate delta, per-platform signed drift — symmetric noise cancels
in these) to be no worse than the A/A reference.** Phase 1 passed this gate on all metrics
(signed drift +0.007 vs noise +0.051; max platform drift +0.095 vs noise +0.097).
Follow-up insight (out of scope here): production admission decisions near threshold carry
this same single-sample randomness today; pinning evaluation temperature and/or
threshold-hysteresis is a future quality lever.

### Phase 1 — Eval prompt diet (+ long-tail interest recall)

`_evaluation_profile_summary` returns
`compact_evaluation_profile_summary(build_profile_summary(profile))`. The digest
(`_evaluation_profile_digest`) automatically follows because it digests the summary — invariant
3 holds by construction. The single-eval path (`engine.py:1204`) switches to the same compacted
summary so both paths see identical profile context.

**Long-tail recall field (the quality counterweight to the cut).** Compaction keeps the top-32
domains / top-48 tags by weight; the tail beyond that is exactly where niche-content matches
live. Rather than re-fattening the stable block, each **content item** in the eval prompt gains
a `related_interests` field: up to 3 interests selected from the **full 256-interest pool** by
embedding similarity to that item's title+description — the exact mechanism
`_select_relevant_interests` already uses for expressions (`recommendation/engine.py:564-610`,
whose comment states the design intent: *"a niche interest outside the head ranks should still
be selectable when it's the best semantic match for this content"*). Properties:

- Cache-safe: content items vary per call anyway; the stable profile prefix is untouched.
- Cheap: ~3 short labels per item ≈ 1–2k tokens per 45-batch, a fraction of the 4–8k saved.
- Digest-correct (invariant 3): the recall selection is a function of the full interest pool,
  so `_evaluation_profile_digest` covers `{compacted summary, interest-pool digest}` — the pool
  digest built from `(name, category, round(weight, 2))` tuples of the top-256 interests.
- Degrades cleanly: no embedding service → field omitted (compact block alone, still up to 48 tags).

Volatile-field pruning: audit the compacted summary for per-entry timestamps / session context
inside `recent_awareness` and `active_insights` items; drop those keys in the compactor so two
profiles differing only in timestamps produce identical digests. (If no such fields exist,
document that finding in the module doc instead.)

Layering is unaffected: `evaluation_profile_prompt_layers` (`engine.py:103`) splits whatever
dict it receives; keys are unchanged, only list lengths shrink.

Acceptance: on a synthetic maxed profile (128 domains × 30 specifics, 256 tags), the rendered
profile block shrinks ≥60% by character count; dislikes list length is unchanged; prompt-cache
invariant test passes; the corrected repeated A/A-relative gate passes with a JSON artifact.

### Phase 2 — Batch-path embedding pre-filter (shadow → enforce)

Extract the single-path pre-filter into a helper and run it in `evaluate_content_batch` after
the cache split (`engine.py:1357-1386`), before batching. Quality-first rollout:

- **Three modes** via config `discovery.eval_prefilter_mode = "off" | "shadow" | "enforce"`,
  **default `shadow`**. Shadow computes similarities and logs what *would* be filtered
  (`title`, `max_sim`, source strategy) but sends everything to the LLM — so every shadow-mode
  candidate later gets a real LLM score, making false negatives directly countable:
  `grep prefilter-shadow`, then count would-be-filtered items with LLM score ≥ admission
  threshold.
  Flip to `enforce` only after shadow shows that count ≈ 0 over a few days of waves.
- **Wider interest coverage than the compact prompt block**: compare against the top-256
  recall-visible interest tags + the 32 compact domain labels — false negatives shrink as
  coverage grows, and vectors are computed **once per call** and reused across all candidates
  (the single path recomputes per item — do not copy that). If per-call embedding latency
  measures too high, fall back to the compact-visible interest block only.
- **Kill-rate fail-open guard**: if the filter would drop > 50% of a batch, send the whole
  batch to the LLM and log a WARN — a degraded/cold embedding index must never silently gut a
  wave.
- Filtered items (enforce mode) get `score = round(max_sim * 0.5, 4)`, the existing reason
  string, and a `_eval_cache` write under the batch cache key, exactly like a real evaluation.
- Skip the filter entirely when `_embedding_service is None` or the profile has no interests.
- Embedding failure for an item → send it to the LLM (fail open).
- Log one INFO line per call: candidates in / filtered (or would-filter) / sent to LLM.

Threshold stays 0.3 (parity with the single path); shadow data is the evidence for ever moving
it. Explore exempt (invariant 6).

### Phase 3 — Split-retry eval fallback

Restructure so `_evaluate_batch` raises on parse failure / unrecoverable count mismatch instead
of looping per-item, and a new `_evaluate_batch_with_split_retry` (mirroring
`recommendation/engine.py:1510-1537`) halves the batch recursively. Floor: when `len(batch) <= 5`,
fall back to per-item `evaluate_content` (which now also benefits from Phase 1 + 2). Rate-limit
errors re-raise at every level without splitting (invariant 5). Split retries run inside the
current eval worker slot — `eval_batch_concurrency` remains the single concurrency control.

Worst case for a 45-item batch with persistently bad output: 45→22→11→5-ish → ~8 split calls +
≤10 singles, versus 45 singles today; transient one-off failures cost 2 extra calls total.

### Phase 4 — Routing completion + tiering guidance

Add to `_ROUTE_BUCKET_PREFIXES` (matching supports `prefix.` and `prefix_`, `service.py:310-313`):

| Prefix | Bucket | Note |
| --- | --- | --- |
| `discovery.keyword` | `discovery` | covers `keyword_planner` today and the inspiration branch's `keyword_*` callers after merge |
| `discovery.x` | `discovery` | |
| `discovery.douyin` | `discovery` | |
| `runtime.bilibili_extension_search` | `discovery` | |
| `pool_purge` | `soul` | purge *removes* pool items — destructive judgment stays on the quality model even when `[llm.evaluation]` is downgraded |
| `api.sentiment` | `soul` | user-facing quality-sensitive |

Plus: `config.example.toml` `[llm.evaluation]` / `[llm.discovery]` / `[llm.recommendation]`
comment blocks gain a worked "flash-tier" example and one line stating which callers each bucket
now covers. `[llm.soul]` comment states it should stay on the quality model. The
`[llm.evaluation]` comment additionally points at the Phase 0 replay script as the way to
**validate a model downgrade before adopting it** — run the replay with arm B = the cheaper
model; if the gates hold, the flash tier judges as well as the flagship for this task.

### Phase 5 — Expression / classification prompt diet

Relocate the compactor to the canonical profile module
(`discovery/strategies/_utils.py`) as `compact_content_prompt_profile_summary` (re-export from
`discovery/engine.py` for backcompat), then apply it inside `_recommendation_profile_summary`
(preserving the `interests=` substitution parameter). Batch expression, single expression, and
`_classify_batch` all inherit the diet through that single choke point. `_profile_blocks` layer
caching (`recommendation/engine.py:229`) is unaffected (same keys, shorter lists).

Quality notes: classification is score/label judgment → covered by the Phase 0 replay gate.
Expression is *subjective copywriting* the replay can't score, so it gets its own gate: a
20-sample **side-by-side dump** (same items, full-profile vs compact-profile expressions,
generated by a small fixture script) attached to the PR for human review — merge only if the
compact side shows no loss of warmth/specificity. The compact block keeps the fields tone
actually draws on (20 core traits, values, style, 48 interests), and the single-expression
path's per-content `_select_relevant_interests` substitution is preserved, so the expected
delta is nil — but it gets looked at, not assumed.

### Phase 6 — Eval-drain coalescing

New `SchedulerConfig` fields (`config.py:224`): `eval_min_batch_size: int = 15` (1–90),
`eval_max_wait_seconds: float = 90.0` (0–600). Plumb into both `DiscoveryCandidatePipeline`
constructors. Behavior (already implemented, `candidate_pipeline.py:765-800`): the drain waits
only while `pending < min_batch` **and** less than `max_wait` has elapsed since the trickle
started — a lone candidate is delayed at most 90s (1.5 refresh ticks), never dropped. Defaults
chosen so first-run init (large pending backlog) is never delayed.

### Phase 7 — Hygiene bundle

- `_eval_cache` → bounded LRU (cap 4096 entries; `OrderedDict` move-to-end on hit, evict oldest
  on insert). Keep the 4/5-tuple legacy tolerance (`engine.py:1370-1374`).
- Historical proposal: `body_text` caps via one shared **head+tail** helper (keep the opening *and* the conclusion —
  long posts put the thesis up front and the takeaway at the end; a head-only slice loses the
  latter): eval and expression paths 200 head + 100 tail chars (tightened from the draft 1600+400 by user decision - title/description already carry the gist), with a
  fixed `…` joiner. Deterministic (plain slices — cache convention). Cap values are module
  constants, and the eval cap change rides the Phase 0 replay gate on a text-source sample
  (bilibili items have empty `body_text`, so bilibili-only replays trivially pass). The required
  Reddit 100×3 replay later failed all quality dimensions; the helper and all production call sites
  were removed, leaving full body text as the accepted behavior.
- `classify_pool_backlog` default `batch_size` 10 → 30.

## Expected impact (heavy user, mature profile)

| Lever | Effect |
| --- | --- |
| Phase 0 | No direct saving; converts every other phase's quality risk into a measured pass/fail; also de-risks flash-tier adoption (often the single biggest ¥ lever) |
| Phase 1 + 5 | Eval/classify/expression input tokens per call −50–70% (profile block is the dominant component); the per-item recall field gives ~1–2k of that back per batch to protect long-tail matching |
| Phase 2 | 10–30% of trending/related candidates skipped before the LLM (search candidates mostly pass — expected); zero skips until shadow data justifies enforce |
| Phase 3 | Failure-mode cost: O(N) singles → O(log N) splits |
| Phase 4 | Entire hot path routable to flash-tier by config; remaining 6 callers stop leaking to the default model |
| Phase 6 | Trickle batches coalesced → fewer calls each paying the fixed profile block |

## Coordination note

`feature/discovery-inspiration-mvp` (active worktree) is rewriting `runtime/keyword_planner.py`
and will remove its LLM brainstorm callers. This spec does not touch `keyword_planner.py`; the
only adjacency is the `discovery.keyword` routing prefix (deliberately future-compatible with
that branch's caller names) and a possible trivial merge conflict in `llm/service.py`.

## Documentation obligations (per CLAUDE.md)

- `docs/modules/discovery.md` — compacted eval summary, batch pre-filter, split-retry, coalescing
- `docs/modules/recommendation.md` — shared compactor, rejected body-text cap, classify batch size
- `docs/modules/llm.md` — routing-bucket table update
- `docs/modules/config.md` — new `[scheduler]` fields, bucket coverage
- `docs/changelog.md` — bullet under the current version block
