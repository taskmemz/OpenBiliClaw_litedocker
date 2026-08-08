# Evaluator Sparse Payload and Row Wire Implementation Plan

**Spec:** `docs/plans/2026-08-05-eval-sparse-row-wire-spec.md`

## Task 1 — Freeze contracts and measurements

**Owner:** root

- [x] Audit constructor fields, prompt rules, result binding and downstream classification consumers.
- [x] Measure production, compact, sparse-JSON and escaped-row character proxies on the frozen 100 rows.
- [x] Define the canonical sparse schema, local-ID safety contract, row escaping and independent A/B arms.
- [x] Review implementation diffs against this spec before any provider call.

## Task 2 — Canonical sparse payload and local IDs

**Owner:** `luna_max_cache`

- [x] Implement one canonical sparse batch builder shared by both transports.
- [x] Add request-local ID mapping and strict result-member resolution without multi-member positional binding.
- [x] Preserve production defaults; expose treatment only through an instance/replay seam.
- [x] Cover duplicate aliases, empty omission, homogeneous defaults, mixed batches and cache-key isolation.
- [x] Run focused tests, Ruff and `git diff --check`.

## Task 3 — Row-wire-v1 codec and multimodal anchors

**Owner:** `luna_max_audit`

- [x] Implement deterministic row encoding and strict decoding of the canonical sparse payload.
- [x] Cover tabs, CR/LF, backslashes, Unicode, empty cells, lists, malformed escapes and row-width failures.
- [x] Keep image bytes/order unchanged while using request-local text/image anchors.
- [x] Prove production prompt rendering remains byte-identical when the experiment seam is off.
- [x] Run focused tests, Ruff and `git diff --check`.

## Task 4 — Independent replay arms and artifact gates

**Owner:** `luna_max_replay`

- [x] Add `--arm-b sparse-json` with production A and sparse JSON B.
- [x] Add `--arm-b row-wire-v1` with sparse JSON A and row-wire B.
- [x] Audit decoded canonical equality, local-ID coverage, image pairing and privacy-safe prompt usage.
- [x] Add locked savings gates and retain score/admission/classification/repair/usage gates.
- [x] Add focused replay tests without changing production behavior.

## Task 5 — Root integration and comprehensive verification

**Owner:** root

- [x] Review all changes for schema drift, unsafe fallback, cache collisions and hidden content loss.
- [x] Resolve integration issues and update mandatory discovery/changelog documentation; cache convention
      remains unchanged while production stays on JSON.
- [x] Run focused prompt/discovery/replay/multimodal tests.
- [x] Run Ruff format/check, MyPy, full Pytest, coverage sanity and `git diff --check`.
- [x] Run deterministic candidate-pipeline E2E including warm-cache and member-repair paths.
- [x] Commit a clean experiment implementation before any real provider call.

## Task 6 — Real replays and independent audit

**Owner:** root

- [x] Run `sparse-json` on 100 candidates × 3 repeats and independently recompute all gates.
- [x] If and only if sparse JSON passes, run `row-wire-v1` on the same 100 × 3 design.
- [x] Scan artifacts against source rows for privacy leakage and verify usage completeness.
- [x] Record exact commands, commits, artifact hashes, runtimes, savings, quality deltas and incidents.

## Task 7 — Production decision

**Owner:** root

- [x] Evaluate the landing condition: sparse passed, but row-wire-v1 failed locked savings and
      classification gates, so no landing is permitted.
- [x] Keep production bytes unchanged and record the failed arm without threshold tuning.
- [x] Rerun applicable full and end-to-end tests after the landing/rejection decision (`7199 passed`,
      `93 skipped`; four candidate-wire/pipeline E2E passed).
