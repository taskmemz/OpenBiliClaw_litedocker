# Weighted Insight Context Implementation Plan

**Spec:** `docs/plans/2026-08-06-weighted-insight-context-spec.md`
**Branch:** `perf/llm-token-diet-phase2`
**Status:** complete; implementation, real-provider gate, and full verification passed

## Wave 1 — Pure selector

1. Add calibrated lane, weight, similarity, and recency constants beside the existing insight caps.
2. Add pure Unicode feature extraction, overlap/similarity, semantic-state, and bounded context-text
   helpers.
3. Implement judged/recent reserves, relevance lane, importance lane, diversity-aware fill, and
   stable source-order restoration.
4. Retain a private fixed-window helper as the exception fallback and replay control.

## Wave 2 — Runtime wiring and tests

1. Pass the current awareness batch, preference, and soul profile into selection.
2. Catch selector-only failures and fall back to the fixed Phase 3 view without affecting full-ledger
   merge.
3. Cover lane quotas, relevance, importance, same-state duplicate competition, conflicting verdicts,
   deterministic order, small histories, fallback, and full persistence.

## Wave 3 — Measurement and real gate

1. Extend the privacy-safe replay with fixed-bounded A/A/A and weighted B context arms.
2. Freeze only hashes/counts/usage/structural quality; persist no prompt, response, profile, or insight
   text.
3. Run the offline render and pinned SenseTime gate, then compare B against both fixed-bounded A and
   the existing full-history token baseline.

## Wave 4 — Documentation and verification

1. Update module docs, architecture/spec/README diagrams, and changelog with achieved—not projected—
   evidence.
2. Run Ruff on all changed/new Python, strict MyPy, targeted tests, full pytest, config smoke, and
   `git diff --check`.
3. Keep changes uncommitted until explicitly authorized by the user.

## Result

- Waves 1–3 complete: production selector, fixed fallback/control, runtime wiring, privacy-safe replay,
  offline snapshot, and pinned SenseTime A/A+B gate all passed.
- Final provider prompt result: `48523 → 27725` versus full history (`-42.86%`), with only `+3.75%`
  versus the fixed Phase 3 window.
- Passing artifact SHA-256:
  `932c5d955b7449b88065e8a5aec408966e40e0c02c2fd8ee506ff11b68e75932`.
- Wave 4 complete: Ruff passed for all Python and formatting passed for all 43 changed/new Python
  files; strict MyPy passed 240 source files; `config-show` and `git diff --check` passed; full pytest
  finished with `7439 passed, 50 skipped`.
