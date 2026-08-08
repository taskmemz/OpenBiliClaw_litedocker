# LLM Token Diet — Implementation Plan

> **2026-08-03 landing correction:** the executable replay workflow below is superseded
> by [`2026-08-03-llm-token-diet-landing-hardening-spec.md`](./2026-08-03-llm-token-diet-landing-hardening-spec.md).
> Current runs require `--repeats >=3` and `--output`, preserve production
> traffic weights, use one frozen snapshot and production `max_tokens=4096`,
> and fail on missing responses. Old standalone gate evidence is not valid for
> landing. The proposed 200+100 `body_text` cap failed the strict Reddit 100×3 gate and was
> completely reverted; only the LRU and classify-batch parts of Task 7 remain accepted.
>
> **Spec:** [`2026-07-05-llm-token-diet-spec.md`](./2026-07-05-llm-token-diet-spec.md)
> **Status:** Final r2 — 2026-07-05. r1 = cost levers; r2 added the quality-preservation layer
> (Task 0 replay gate, per-item interest recall, pre-filter shadow rollout) so recommendation
> quality is measured, not assumed. Implement task-by-task, TDD style; do not start a task
> before the previous one's tests are green.
> **Execution order (from Spec):** Wave A = Tasks 3, 4, 6, 2 (ships behavior-neutral in
> shadow), 7's LRU + classify-batch parts — zero quality surface, any order. Wave B = Task 0 →
> 1 → 5. Task 7's body-cap experiment was executed and rejected by its quality gate.
> Task 8 (docs) last.
> **Tech:** Python 3.11+, pytest (`asyncio_mode=auto`), Ruff, MyPy strict, 100-char lines.
> Interpreter is `.venv/bin/python` (plain `python`/`python3` has no deps).
> Run per task: `.venv/bin/python -m pytest <touched test files> -q`, then
> `.venv/bin/python -m ruff check` / `ruff format --check` on touched files, then
> `.venv/bin/python -m mypy src/openbiliclaw/`.

**Invariants that MUST hold (from Spec — re-read before each task):**
- No system-prompt text changes anywhere;
  `tests/test_llm_prompts.py::test_prompt_builder_system_messages_are_call_invariant` stays green.
- Profile trimming is a pure function of profile state (never per-batch); per-item
  `related_interests` recall lives on content items, never in the profile block.
- Eval-cache digest always covers exactly the prompt-visible profile slice (including the
  interest pool feeding per-item recall).
- Disliked topics never cut below store cap.
- Rate-limit errors propagate without split/single fallback.
- Explore candidates exempt from embedding pre-filter; pre-filter defaults to **shadow** mode.
- **Quality gate:** Tasks 1 and 5 require passing replay evidence. Task 7's body-text cap failed
  that gate and therefore does not merge.
- Do NOT touch `runtime/keyword_planner.py` (owned by `feature/discovery-inspiration-mvp`).

---

### Task 0: Golden-set replay gate (`scripts/run_profile_diet_ab.py`)

**Files:** Add `scripts/run_profile_diet_ab.py`;
Test `tests/test_profile_diet_ab.py` (pure helpers only — the script's LLM runs are manual)

**Steps:**
1. Failing tests for the pure metric helpers (put them in the script or a small
   `eval/`-adjacent module so they're importable): given two aligned score lists →
   mean/p95 `|Δ|`, Spearman rank correlation, and admission flip rate using the per-strategy
   thresholds from `candidate_pipeline.py:33-44` (an item flips when A ≥ threshold > B or
   vice versa).
2. Script body (follow `scripts/run_discovery_eval.py` conventions — argparse, asyncio):
   - `--sample N` (default 100): pull the most recent `evaluated`/admitted discovery
     candidates from the real DB (mixed strategies; `--platform` filter optional), plus the
     current profile.
   - `--arm-b compact|reason-diet|model=<instance-id>` (v2; legacy routing also
     accepts `model=<provider:model>`): arm A always scores with today's
     baseline inputs; arm B applies the candidate change. Both arms bypass `_eval_cache`
     (fresh scores) and share negative exemplars.
   - Output: metrics table + required JSON artifact containing raw scores,
     snapshot digests, routes, usage, and gate inputs; exit code 1 when the
     repeated relative gate fails.
3. Keep the script read-only against the DB (no status/state writes).
4. `.venv/bin/python -m pytest tests/test_profile_diet_ab.py -q` + lint + mypy.

**Note:** the `compact` arm can only be exercised end-to-end after Task 1 exists; land the
script first with `model=` arm working (it needs nothing new), and wire `compact` in Task 1's
acceptance run.

### Task 1: Wire `compact_evaluation_profile_summary` + per-item interest recall

**Files:** Modify `src/openbiliclaw/discovery/engine.py`;
Test `tests/test_discovery_engine.py`

**Steps:**
1. Failing tests:
   - `_evaluation_profile_summary(profile)` returns
     `compact_evaluation_profile_summary(build_profile_summary(profile))` (compare dicts on a
     synthetic maxed profile: >32 interest domains with >16 specifics each, >48 interests,
     >20 core traits).
   - `disliked_topics` length is identical before/after compaction even with 100+ entries.
   - `_evaluation_profile_digest` changes when an interest domain is added, **and** when a
     tail interest (rank > 48, outside the compact block but inside the recall pool) is added —
     the digest must cover the recall pool (invariant 3).
   - Digest **unchanged** when only volatile per-entry fields change (see step 4).
   - Rendered profile block (join of
     `PromptLayerRenderCache().render_json_layers(evaluation_profile_prompt_layers(summary))`)
     shrinks ≥60% by character count on the maxed profile vs the uncompacted summary.
2. Change `_evaluation_profile_summary` (`engine.py:1605-1607`) to apply the compactor; switch
   the single-eval prompt build (`engine.py:1204`) from `build_profile_summary(profile)` to
   `self._evaluation_profile_summary(profile)` so both paths share one profile shape.
   Extend `_evaluation_profile_digest` to
   `stable_json_digest({"summary": compacted, "recall_pool": [(name, category,
   round(weight, 2)) for top-256 interests]})`.
3. **Per-item long-tail recall** (failing tests first):
   - `_related_interests_for_content(content, profile, *, top_k=3)` — reuse the
     `_select_relevant_interests` pattern (`recommendation/engine.py:564-610`): full top-256
     interest pool, embedding-similarity × weight blend, returns ≤3 `{name, category}` dicts;
     returns `[]` when `_embedding_service is None` or embed fails. Compute the content vector
     once per item; interest vectors once per batch call.
   - Batch item builder (`engine.py:1666-1684`) and single-eval content summary gain
     `"related_interests"` — test that a tail interest (outside compact caps) relevant to an
     item's title appears in that item's field, and that the profile block bytes are identical
     with/without the field (stability of the cached prefix).
4. Audit compacted output for volatile per-entry fields (timestamps / session context) inside
   `recent_awareness` and `active_insights` items. If present, strip those keys inside the
   compactor (`_compact_active_insights` / the recent-awareness cap site). If absent, add a
   code comment stating the audit result and keep only the digest-changes tests.
5. Confirm `test_compact_evaluation_profile_summary_keeps_high_signal_context`
   (`tests/test_discovery_engine.py:181`) still passes unmodified.
6. Run targeted tests + lint + mypy.
7. **Acceptance (quality gate):** run
   `.venv/bin/python scripts/run_profile_diet_ab.py --arm-b compact --sample 100 --repeats 3 --output data/eval/profile-diet-compact.json` against the
   real DB; record the artifact in the PR. The repeated A/A-relative gate decides the result. If failing, raise
   `_EVAL_PROFILE_DOMAIN_CAP` / `_EVAL_PROFILE_INTEREST_CAP` stepwise and re-run.

**Note:** in-memory `_eval_cache` entries from the old digest become unreachable — acceptable
(process-local; DB `evaluated` status is the durable dedup). Do not bump
`_EVAL_BATCH_CACHE_VERSION`; old cached scores are not semantically invalidated. The
`related_interests` prompt field must be documented in the batch-eval user-prompt builder's
docstring (system prompt text untouched — the model treats unknown item fields as context).

### Task 2: Embedding pre-filter on the batch eval path (shadow → enforce)

**Files:** Modify `src/openbiliclaw/discovery/engine.py`, `src/openbiliclaw/config.py`,
`config.example.toml`;
Test `tests/test_discovery_engine.py`, `tests/test_config.py`

**Steps:**
1. Failing tests (stub embedding service + counting stub LLM service):
   - **enforce mode:** a candidate whose `title+description` embedding has cosine < 0.3 against
     every pooled interest vector gets `relevance_score == round(max_sim * 0.5, 4)`, the
     pre-filter reason string, a `_eval_cache` entry under its **batch** cache key — and the
     LLM stub records a batch WITHOUT it.
   - **shadow mode (default):** same candidate → still sent to the LLM, but a
     `prefilter-shadow` log record (title, max_sim, strategy) is emitted; no score override.
   - **off mode:** no similarity computation at all (embed stub call count 0).
   - `source_strategy == "explore"` candidate below threshold untouched in every mode.
   - **Kill-rate guard (enforce):** when > 50% of a batch is below threshold, nothing is
     filtered, everything goes to the LLM, one WARN is logged.
   - `_embedding_service is None` or empty `profile.preferences.interests` → no filtering.
   - Embedding call raising for one item → that item goes to the LLM (fail open).
   - All candidates filtered (≤50% guard not tripped because batch of 1) → zero LLM calls,
     scores still returned in order.
2. Config: `discovery.eval_prefilter_mode: str = "shadow"` (validated against
   `{"off", "shadow", "enforce"}`), plumbed to the engine like
   `multimodal_evaluation_enabled`; `config.example.toml` entry documents the shadow→enforce
   rollout (check `grep prefilter-shadow` for would-be-filtered items that scored ≥ admission
   threshold; flip to enforce when ≈ 0).
3. Extract the single-path logic (`engine.py:1172-1199`) into
   `async def _embedding_prefilter(self, contents, profile) -> dict[int, float]` returning
   index → filtered score. Interest vector pool = the full top-256 recall-visible tags + 32 compact
   domain labels, **computed once per call** and reused across candidates (do not copy the single
   path's per-item recomputation). Threshold: module constant
   `_EMBEDDING_PREFILTER_MIN_SIMILARITY = 0.3`.
4. Call it in `evaluate_content_batch` after the cache split (`engine.py:1357-1386`), before
   `_effective_eval_batch_size`; apply per mode (step 1 semantics); one INFO log per call:
   `in / prefiltered (or would_filter) / to_llm` counts.
5. Refactor single-path `evaluate_content` to use the same helper and honor the same mode flag.
6. Run targeted tests + lint + mypy.

### Task 3: Split-retry fallback for batch evaluation

**Files:** Modify `src/openbiliclaw/discovery/engine.py`;
Test `tests/test_discovery_engine.py`

**Steps:**
1. Failing tests (stub LLM that fails on demand by call index / batch size):
   - First call for 45 items raises a parse error → exactly two follow-up calls (22 + 23), all
     scores populated, zero `evaluate_content` single calls.
   - Persistent parse failure → splits halve down to ≤5, then per-item fallback fires; total LLM
     calls ≪ N (assert an upper bound, e.g. ≤ N/2 for N=45... actually assert exact split-tree
     call count for a fixed N like 16: 1+2+4+8 batch attempts + 16 singles worst case).
   - Rate-limit error (recognized by `is_llm_rate_limit_error`) at any level → propagates
     immediately, **no** split calls, **no** single calls.
   - ID-less count mismatch (payload length ≠ batch length, no usable content keys) → same
     split-retry path as parse failure.
2. Change `_evaluate_batch` (`engine.py:1640`): replace both per-item fallback blocks
   (`:1794-1798`, `:1801-1811`) with `raise` (wrap count mismatch in `ValueError`). Keep the
   rate-limit re-raise branch exactly as is.
3. Add `_evaluate_batch_with_split_retry(batch, profile, *, source_context, negative_examples)`
   mirroring `_precompute_batch_with_split_retry` (`recommendation/engine.py:1510-1537`):
   `len(batch) <= 5` → sequential `evaluate_content` per item; else try whole batch, on
   non-rate-limit exception halve and recurse. Splits run inline in the current worker (no new
   tasks) so `eval_batch_concurrency` stays the only concurrency control.
4. Update the call site in `evaluate_content_batch` to use the split-retry wrapper.
5. Run targeted tests + lint + mypy.

### Task 4: Complete `_ROUTE_BUCKET_PREFIXES` + tiering guidance in config example

**Files:** Modify `src/openbiliclaw/llm/service.py`, `config.example.toml`;
Test `tests/test_llm_service.py` (or wherever `_route_bucket_for_caller` is covered — check
`grep -rn "route_bucket" tests/`)

**Steps:**
1. Failing test: `_route_bucket_for_caller` maps
   `discovery.keyword_planner` → `discovery` (via `discovery.keyword` prefix + `_` match),
   `discovery.x.keyword_gen` → `discovery`, `discovery.douyin.keyword_gen` → `discovery`,
   `runtime.bilibili_extension_search.queries` → `discovery`,
   `pool_purge.llm_agent` → `soul` (destructive judgment stays on the quality model),
   `api.sentiment` → `soul`;
   and existing mappings are unchanged (`recommendation.evaluate_batch` → `evaluation`,
   `soul.preference` → `soul`).
2. Add the six prefixes to `_ROUTE_BUCKET_PREFIXES` (`service.py:244-257`). Use
   `("discovery.keyword", "discovery")` — the `prefix_` match rule makes it cover
   `keyword_planner` today and the inspiration branch's `discovery.keyword_*` callers post-merge.
3. `config.example.toml`: in the `[llm.evaluation]` / `[llm.discovery]` /
   `[llm.recommendation]` comment blocks add (a) one worked flash-tier example
   (provider + model lines, commented out), (b) one line listing the caller families the bucket
   covers, (c) in `[llm.evaluation]`, a pointer to
   `scripts/run_profile_diet_ab.py --arm-b model=<instance-id> --sample 100 --repeats 3 --output data/eval/model-route.json` as the way to validate a
   downgrade before adopting it; in `[llm.soul]` note it should stay on the quality model.
4. Run targeted tests + lint + mypy.

### Task 5: Share the compactor with expression / classification

**Files:** Modify `src/openbiliclaw/discovery/strategies/_utils.py`,
`src/openbiliclaw/discovery/engine.py`, `src/openbiliclaw/recommendation/engine.py`;
Test `tests/test_recommendation_engine.py`, `tests/test_discovery_engine.py`

**Steps:**
1. Move `compact_evaluation_profile_summary` + its `_compact_*` / `_cap_*` helpers and
   `_EVAL_PROFILE_*` constants from `discovery/engine.py` to
   `discovery/strategies/_utils.py`, renamed `compact_content_prompt_profile_summary`
   (constants `_CONTENT_PROMPT_*`). Keep a thin re-export
   `compact_evaluation_profile_summary = compact_content_prompt_profile_summary` in
   `discovery/engine.py` so existing imports/tests keep working.
2. Failing tests:
   - `_recommendation_profile_summary(profile)` equals the compacted summary on a maxed profile;
     `interests=` substitution still works (substituted list appears, then compaction caps
     apply).
   - Batch expression prompt user message on a maxed profile shrinks ≥50% vs before (build via
     `build_batch_expression_prompt` with `_profile_blocks`).
3. Apply the compactor inside `_recommendation_profile_summary`
   (`recommendation/engine.py:55-70`) — single choke point; `_precompute_batch`,
   `_try_generate_expression`, and `_classify_batch` inherit it. The single-expression path's
   per-content `_select_relevant_interests` substitution (`interests=`) is preserved untouched —
   it is the expression-side long-tail protection.
4. Confirm no import cycle: `_utils.py` must not import from `recommendation/`.
5. Run both test files + lint + mypy.
6. **Acceptance (quality gates):** classification — Task 0 replay run passes. Expression — add
   a small fixture script (or pytest `-k sidebyside -s` helper) that renders 20 real pool items'
   expressions with full vs compact profile against the live provider; attach the side-by-side
   dump to the PR; merge only if the compact side shows no loss of warmth/specificity on human
   review.

### Task 6: Eval-drain coalescing config

**Files:** Modify `src/openbiliclaw/config.py`, `config.example.toml`,
`src/openbiliclaw/api/runtime_context.py` (~:642), `src/openbiliclaw/cli.py` (~:8889);
Test `tests/test_config.py`, `tests/test_candidate_pipeline.py` (names — verify with
`ls tests/ | grep -i "config\|candidate"`)

**Steps:**
1. Failing tests:
   - `SchedulerConfig` gains `eval_min_batch_size: int = 15` (valid 1–90) and
     `eval_max_wait_seconds: float = 90.0` (valid 0–600); out-of-range TOML values raise the
     same validation error type as `pool_target_count` (`config.py:1591` pattern).
   - Pipeline behavior with injected `time_fn`: pending=3 < min_batch=15 → drain defers
     (existing `_waiting_pending_eval_count` machinery); after `max_wait` elapses, drain
     proceeds with the small batch. (Machinery is already tested? — check for existing coverage
     around `candidate_pipeline.py:765-800` first; only add what's missing.)
   - Both constructors pass the config values through (assert pipeline attrs from a built
     runtime context / CLI factory, or unit-test the factory function arguments).
2. Add the two `SchedulerConfig` fields + TOML parsing/validation (follow
   `refresh_check_interval_seconds` normalization pattern, `config.py:910`).
3. `config.example.toml` `[scheduler]`: add both keys, commented, with one-line explanations
   ("coalesce trickle candidates into fuller eval batches; a lone candidate waits at most
   `eval_max_wait_seconds`").
4. Plumb into both `DiscoveryCandidatePipeline(...)` construction sites.
5. Run targeted tests + lint + mypy.

### Task 7: Hygiene — bounded eval cache, rejected `body_text` cap, classify batch size

**Files:** Modify `src/openbiliclaw/discovery/engine.py`,
`src/openbiliclaw/recommendation/engine.py`;
Test `tests/test_discovery_engine.py`, `tests/test_recommendation_engine.py`

**Steps:**
1. Historical experiment tests:
   - `_eval_cache` holds ≤ 4096 entries; inserting 4097 evicts the least-recently-used (a get
     refreshes recency); legacy 4-tuple entries still read correctly.
   - Eval batch item and single-eval `body_text` truncated **head+tail** (200 head + 100 tail,
     fixed `…` joiner — keeps thesis *and* conclusion of long posts); expression batch item
     same 200+100; text shorter than head+tail passes through byte-identical;
     `None`/empty passthrough unchanged; output is deterministic (same input → same bytes).
   - `classify_pool_backlog` default splits 60 rows into 2 batches (batch_size 30), and an
     explicit `batch_size=` argument still overrides.
2. Replace `self._eval_cache: dict` (`engine.py:719`) with an `OrderedDict`-based LRU (module
   constant `_EVAL_CACHE_MAX_ENTRIES = 4096`; wrap get/set in two small private methods so all
   five existing touch points — `:1158,1192,1256,1364,1853` — go through them).
3. The experiment added `_prompt_body_text(value: str | None, *, head: int, tail: int)` (deterministic
   head+tail slices, fixed `…` joiner) in a shared spot (`discovery/strategies/_utils.py`),
   apply at `discovery/engine.py:1675`, `:1210` and
   `recommendation/engine.py:1361` — all head 200 / tail 100 (tightened from the draft
   1600+400 / 1000+200 by user decision; title/description already carry the gist).
   Constants module-level.
   Then run the Task 0 replay with `--arm-b body-cap --platform x` (or whichever text source
   has rows) — bilibili-only DBs trivially pass (empty `body_text`); record the result. The strict
   Reddit 100×3 run failed flip-rate, Spearman and admission gates, so this helper, its constants,
   production call sites and the formal replay arm were subsequently removed. Full body text is the final contract.
4. `classify_pool_backlog` `batch_size: int = 10` → `30` (`recommendation/engine.py:1024`).
5. Run targeted tests + lint + mypy.

### Task 8: Documentation sync (mandatory, per CLAUDE.md)

**Files:** `docs/modules/discovery.md`, `docs/modules/recommendation.md`,
`docs/modules/llm.md`, `docs/modules/config.md`, `docs/changelog.md`

**Steps:**
1. `discovery.md`: compacted eval profile summary (caps table) + per-item `related_interests`
   recall, batch embedding pre-filter (off/shadow/enforce rollout), split-retry fallback
   ladder, coalescing knobs, `scripts/run_profile_diet_ab.py` quality-gate workflow.
2. `recommendation.md`: shared profile compactor, rejected `body_text` cap / full-body outcome,
   classify batch default.
3. `llm.md`: updated routing-bucket table (all callers ↔ buckets, including the six new ones).
4. `config.md`: `[scheduler].eval_min_batch_size` / `eval_max_wait_seconds`, bucket coverage
   notes for `[llm.*]` overrides.
5. `docs/changelog.md`: one bullet under the current version block, e.g.
   `perf: content-eval/expression prompts use compacted profile + per-item interest recall
   (−50%+ input tokens on mature profiles, quality-gated by golden-set replay); batch eval
   gains embedding pre-filter (shadow rollout) + split-retry fallback; all LLM callers
   routable via [llm.<bucket>] tiers`.
6. Full test suite once at the end: `.venv/bin/python -m pytest -q` + ruff + mypy.

---

## Verification after merge

1. `openbiliclaw cost --by caller` — watch `discovery.evaluate_batch` /
   `recommendation.write_expression` input-tokens-per-call drop and cache-hit% hold or rise.
2. Grep daemon log for `prefilter-shadow` over a few days of waves; count would-be-filtered
   items whose LLM score ≥ admission threshold. ≈ 0 → set
   `discovery.eval_prefilter_mode = "enforce"`; otherwise leave shadow on and revisit the
   threshold with that data.
3. Config-only follow-up (user action, not code): validate a flash-tier model with
   `scripts/run_profile_diet_ab.py --arm-b model=<instance-id> --sample 100 --repeats 3 --output data/eval/model-route.json`; if gates pass, set
   `[llm.evaluation]` / `[llm.discovery]` / `[llm.recommendation]` in local `config.toml`.
4. Recommendation feed spot-check for a week: long-tail/niche items still appear (the
   `related_interests` recall field is doing its job); expression tone unchanged.

## Explicitly out of scope

- `runtime/keyword_planner.py` and anything on `feature/discovery-inspiration-mvp` (merge-order
  note: `llm/service.py` prefix addition may conflict trivially — resolve by union).
- Scheduler cadences, pool water levels, soul-pipeline thresholds.
- Any change to system prompt constants in `llm/prompts.py`.
