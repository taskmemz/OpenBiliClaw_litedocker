# LLM Token Diet Phase 2 Implementation Plan

**Spec:** `docs/plans/2026-08-05-llm-token-diet-phase2-spec.md`
**Branch:** `perf/llm-token-diet-phase2`
**Rule:** a token gate cannot override a quality gate.

## Wave 0 — Baseline and seams

1. Add frozen fixtures and privacy-safe measurement helpers for rendered characters, request
   attribution, and provider-reported usage.
2. Add explicit `legacy` / `compact-v1` cognition builder seams without changing the system prompt.
3. Add independent Preference / Awareness-with-confusions / Insight constructor and config seams
   for production selection and rollback; pin plain Awareness to legacy and do not expose an
   aggregate full-compact switch.
4. Capture the current 30-day caller baseline and current copy generated/shown/stale counts in a
   sanitized artifact.

## Wave 1 — Cognition input views

1. Add `CognitionEventViewV1` as a pure deterministic projection.
2. Add named `CognitionProfileViewV1` functions/dataclass in `soul/profile_views.py`.
3. Route preference, awareness-with-confusions, and insight prompts through their selected views;
   keep plain awareness on its byte-identical legacy view until separately gated.
4. Split profile material into stable preference/soul and volatile recent-cognition blocks.
5. Register every consumer and portrait decision in `docs/profile-usage.md`.
6. Add unit/golden tests for signal preservation, no mutation, deterministic rendering, prompt
   invariance, malformed metadata, and minimum fixture-size reduction.

## Wave 2 — Demand-driven expression copy

1. Add copy-ready target/legacy-drain configuration with validation and config round-trip tests;
   inject the same clamped effective target in API RuntimeContext, CLI, and OpenClaw composition
   roots (`0` remains the explicit legacy drain-all rollback).
2. Add storage/readiness counters that distinguish ready copy from admitted pending copy.
3. Bound expression drain by current copy deficit while retaining the expression lock and durable
   pending rows.
4. Notify refill after serve/feedback/maintenance without blocking a user response.
5. Add concurrency, provider-failure recovery, decreasing-target, stale/suppressed exclusion, and
   four-surface serve-gate tests.

## Wave 3 — Prefilter evidence and exact-retry telemetry

1. Add bounded persistent shadow audit rows or an equivalent durable join between prefilter
   decisions and final LLM scores.
2. Record candidate/profile/embedding digests and sanitized strata, never content text.
3. Add aggregate queries and a replay command that computes admission recall and fail-open coverage.
4. Add exact request-digest duplicate telemetry; do not enable semantic result reuse.
5. Keep production prefilter `shadow` until the real gate passes.

## Wave 4 — Deterministic replay

1. Freeze representative preference/awareness-with-confusions/insight inputs by IDs and digests.
2. Render control A1/A2 and compact A/B from identical frozen inputs.
3. Compute structural quality, preference overlap/drift, evidence attribution, repair rate, and
   character-size metrics.
4. Produce a privacy-safe local artifact with both the unchanged aggregate full-compact gate and
   machine-readable per-task rollout verdicts; fail non-zero on any aggregate blocking gate.

## Wave 5 — Automated verification

Run, in order:

```bash
.venv/bin/ruff format src/ tests/ scripts/
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/mypy src/
.venv/bin/pytest -q <targeted tests>
.venv/bin/pytest
git diff --check
```

Any unrelated environment failure is isolated and documented; no test failure is reclassified as a
pass.

## Wave 6 — SenseTime 日日新 real requests

1. Resolve the configured SenseTime instance without printing credentials.
2. Pin soul/evaluation calls to that instance and disable fallback for the evidence run.
3. Run control A1/A2 first to establish provider nondeterminism.
4. Run matched control/treatment A/B on the same frozen cohorts.
5. Verify every attributed call used the intended instance/model and has valid usage.
6. Run a real copy-inventory refill/serve cycle and confirm no empty-copy card.
7. Run the prefilter cohort gate; leave it in shadow on any miss.
8. Store only sanitized aggregate artifacts under `data/eval/` (gitignored).

### Cognition rollout decision — 2026-08-06

The SenseTime task-scoped gate authorizes `compact-v1` only for
`soul.awareness_confusions`. Ship defaults as Preference=`legacy`,
Awareness-with-confusions=`compact-v1`, Insight=`legacy`, while plain `soul.awareness` stays pinned
to `legacy`. Preference, plain Awareness, and Insight remain blocked pending their own fresh passing
artifacts; the Awareness-with-confusions result cannot authorize a full-cognition flip. Replay uses
the machine task key `awareness_confusions`, keeps rendering both arms explicitly, and does not
inherit production rollout defaults.

### Copy and prefilter rollout decision — 2026-08-06

The real SenseTime copy E2E proved target refill without backlog drain, so the default positive
copy-ready watermark ships with `0` as the independent legacy drain-all rollback. This is a
functional/quality authorization; longitudinal tokens-per-shown-card savings remain telemetry to
observe rather than a claimed measured percentage. Prefilter enforcement does not ship: the live
database has no joinable audit cohort yet, so the read-only §6.4 gate fails closed and production
stays in `shadow` until at least 100 complete rows pass every recall/coverage/fail-open stratum.

## Wave 7 — Documentation, commit, merge

1. Update affected module docs, profile registry, config docs/example, changelog, and any changed
   architecture diagrams required by `CLAUDE.md`.
2. Re-run targeted/full verification on the exact final diff.
3. Commit with Conventional Commits on `perf/llm-token-diet-phase2`.
4. Merge into `main` only after the unrelated dirty main work is safely committed/stashed by its
   owner and after a final conflict audit.
5. Push `main`, then verify local/remote ancestry and clean status.
