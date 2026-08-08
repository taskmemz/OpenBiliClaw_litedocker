# Evaluator JSON Minify Implementation Plan

**Spec:** `docs/plans/2026-08-04-eval-json-minify-spec.md`

## Task 1 — Freeze the boundary

- [x] Record the real reason-off result and identify prompt input as the dominant token component.
- [x] Measure current 100-row prompt character composition without provider calls.
- [x] Define JSON-minify as a whitespace-only experiment; defer field omission and batch changes.
- [x] Complete the field-consumer audit needed for the next, separate serialization-diet phase; it
      requires batch-local string IDs and forbids multi-member positional fallback before global IDs can
      leave the LLM wire safely.

## Task 2 — Opt-in compact renderer

**Owner:** `luna_max_cache`

- [x] Add an opt-in compact renderer while preserving the existing pretty default.
- [x] Prove byte determinism, sorted keys, Unicode preservation and string-value preservation.
- [x] Run focused formatter/lint/tests; do not wire production.

## Task 3 — Replay-only arm

**Owner:** `luna_max_replay`

- [x] Add `--arm-b json-minify` with A pretty and B compact.
- [x] Scope the treatment without mutating module globals across concurrent calls.
- [x] Verify A/B JSON semantics, system prompt, route and runtime settings.
- [x] Extend privacy-safe artifact usage/cache/repair evidence and fail-closed aggregation.
- [x] Add focused replay tests; production defaults remain byte-identical.

## Task 4 — Root integration verification

- [x] Review all changes against the spec and repository prompt-cache convention.
- [x] Run JSON renderer, replay, prompt, discovery engine and candidate-pipeline focused tests.
- [x] Run Ruff, MyPy and `git diff --check`.
- [x] Run deterministic candidate-pipeline E2E and verify warm-cache zero-provider-call behavior.
- [x] Commit a clean replay experiment before calling the provider.

## Task 5 — Real replay and independent audit

- [x] Abort the first provider run after detecting unattributed successful empty-content retries whose
      usage was overwritten by the adapter's final response; do not treat the partial run as evidence.
- [x] Add and test per-wire-attempt OpenAI-protocol usage accounting before restarting the experiment.
- [x] Run the exact 100×3 command from the spec on clean commit `ad4ba670`.
- [x] Independently recompute score/admission/Spearman and paired usage deltas from raw artifact data.
- [x] Validate route/embedding/recall/cache/repair gates and artifact privacy.
- [x] Record local artifact `data/eval/json-minify-ad4ba670.json`, SHA-256
      `873d90c9d46c6b45465201a883044d49d921d54eaa18878e012f377a87c2c8c9`, runtime
      `6227.3s`, 46 accounted format fallbacks and 3 recovered rate limits.

## Task 6 — Production decision

- [x] Reject production compact JSON because relative admission quality, provider cache and complete
      token-evidence gates failed; do not tune thresholds after observing the result.
- [x] Keep production pretty JSON, the existing eval-cache version and the existing `CLAUDE.md`
      prompt-cache convention unchanged.
- [x] Rerun focused, full backend and applicable end-to-end tests before the final result commit.

## Deferred independent experiments

These remain separate so their effects can be attributed:

1. omit empty/redundant candidate fields and use batch-local short IDs;
2. harden exact JSON schema/member completeness;
3. raise text batch size from 30 to 45;
4. calibrate embedding prefilter shadow → enforce;
5. score first, classify only near/above admission;
6. semantic sentence retrieval for long text bodies.
