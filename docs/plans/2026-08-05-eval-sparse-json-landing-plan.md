# Evaluator Sparse JSON Production Landing Plan

**Spec:** `docs/plans/2026-08-05-eval-sparse-json-landing-spec.md`

## Task 1 — Freeze landing and rollback contracts

**Owner:** root

- [x] Record the sparse-only production decision without changing the rejected row-wire gates.
- [x] Lock the default, cache-version, static-prompt and explicit rollback behavior.
- [x] Audit all engine construction paths and existing tests against the new default.

## Task 2 — Default transport and cache namespace

**Owner:** `luna_max_cache`

- [x] Make sparse JSON the batch evaluator default through one named constant.
- [x] Keep explicit production and row transports available only as internal rollback/replay seams.
- [x] Bump the evaluator cache namespace and preserve sparse cache-key semantics.
- [x] Add focused default/rollback/cache tests and run Ruff/MyPy.

## Task 3 — Independent contract audit

**Owner:** `luna_max_audit`

- [x] Audit API, CLI, OpenClaw and nested strategy constructors for implicit/explicit transport drift.
- [x] Audit static system prompt, local-ID binding, multimodal anchors and production rollback bytes.
- [x] Report missing tests or unsafe fallback; do not change locked replay thresholds.

## Task 4 — Landing E2E coverage

**Owner:** `luna_max_replay`

- [x] Add tests proving default sparse behavior through a real engine/pipeline prompt path.
- [x] Prove explicit production rollback and row non-default behavior.
- [x] Exercise cold/warm cache, repair and privacy metadata after the default switch.

## Task 5 — Root integration and documentation

**Owner:** root

- [x] Review agent changes and resolve test assumptions that intentionally depended on the old default.
- [x] Update discovery docs, changelog and `CLAUDE.md`; confirm config/CLI/architecture remain out of scope.
- [x] Run focused E2E, Ruff, MyPy, full Pytest and `git diff --check`.
- [x] Commit a clean production landing with the rollback seam documented.
