# LLM Token Diet Phase 3 Implementation Plan

**Spec:** `docs/plans/2026-08-06-llm-token-diet-phase3-spec.md`
**Branch:** `perf/llm-token-diet-phase2`
**Rule:** every feature lands behind an independent correctness and quality verdict.

## Wave 0 — Freeze baseline and tests

1. Record the 2026-07-31 through 2026-08-05 caller totals and aggregate insight/preference/keyword
   diagnostics in the spec.
2. Add failing tests for bounded insight context, request-shape-aware preference fan-out, keyword
   grace reconciliation, provenance, rollback, and failure fallback.
3. Keep raw private content out of fixtures and artifacts.

## Wave 1 — Bounded insight context

1. Add calibrated recent/judged context caps and a pure deterministic selector.
2. Load full history once, pass only the selected view to generation, and merge into full history.
3. Add prompt-size diagnostics and cognition-cycle regression tests.
4. Run the targeted soul/insight test suites.

## Wave 2 — Request-shape-aware preference batching

1. Add a deterministic largest-fitting independent-prefix helper.
2. Use it only for automatic budget fallback; preserve explicit init chunks.
3. Keep all existing overflow/refusal/invalid-JSON recovery paths.
4. Add tests for one-call packing, multi-chunk bounds, explicit-size invariance, and merge parity.

## Wave 3 — Keyword digest grace

1. Add `discovery.keyword_digest_grace_hours` with validation, rendering, API/config round trip, and
   a zero-hour legacy rollback.
2. Add atomic storage reconciliation for current/recent-stale regular pending inventory.
3. Add explicit dislike/current avoid filtering, pending-history novelty, and aggregate ledger logs.
4. Reconcile before due calculation and count all retained regular pending rows.
5. Add storage/planner concurrency, cap, provenance, isolation, blocked, aged, and failure tests.

## Wave 4 — Documentation and automated verification

1. Update affected module docs, config example/reference, changelog, and profile-consumer notes where
   applicable.
2. Run:

```bash
.venv/bin/ruff format src/ tests/ scripts/
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/mypy src/
.venv/bin/pytest -q tests/test_cognition_cycle.py tests/test_insight_analyzer.py
.venv/bin/pytest -q tests/test_preference_analyzer.py
.venv/bin/pytest -q tests/test_keyword_planner.py tests/test_storage.py tests/test_config.py
.venv/bin/pytest
git diff --check
```

3. Isolate only demonstrably pre-existing failures; do not relabel a new failure as unrelated.

## Wave 5 — SenseTime 日日新 gates

1. Freeze privacy-safe insight and preference cohorts from the read-only production data.
2. Pin the configured SenseTime instance/model and disallow fallback for evidence runs.
3. Run A1/A2 controls before each B arm and store provider usage plus route audit.
4. Run keyword reconciliation on a disposable database copy, then one controlled real planner E2E.
5. Run a full cognition → preference → planner → discovery/refill smoke and verify output contracts.
6. Store sanitized aggregate artifacts under `data/eval/` only.

## Wave 6 — Final verdict

1. Compute per-caller and whole-window savings without double counting.
2. Compare every structural/semantic metric against its frozen gate and A/A noise.
3. Leave failed features on their rollback setting; enable only independently passing defaults.
4. Re-run the exact final diff checks and report achieved versus projected savings separately.

## Completion record (2026-08-06)

- Waves 0–4 completed; targeted suites, Ruff, strict MyPy, diff whitespace checks, and final full
  pytest (`7,427 passed, 50 skipped`) passed on the implementation worktree.
- Wave 5 completed on pinned SenseTime `openai_compatible/deepseek-v4-flash`; cognition A/A+B,
  disposable keyword reconciliation, real Bilibili search, evaluator/admission/cache, and yield
  attribution all passed with no cross-provider fallback.
- Wave 6 verdict: ship the bounded insight context, per-offset preference packing, and 24-hour
  keyword digest grace. Achieved real A/B savings are recorded in the spec and changelog; the earlier
  whole-window `29.38%` number remains a projection rather than measured production billing.
