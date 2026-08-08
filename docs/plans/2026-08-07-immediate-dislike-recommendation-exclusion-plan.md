# Immediate Dislike Recommendation Exclusion Implementation Plan

**Spec:** `docs/plans/2026-08-07-immediate-dislike-recommendation-exclusion-spec.md`
**Status:** complete — implementation, real acceptance, and full regression passed

## Wave 1 — Diagnose and reset the boundary

1. Correlate confirmed dislike writeback/pool purge with later discovery and recommendation activity
   in the incident log.
2. Preserve ordinary search behavior; remove the abandoned pre-fetch query gate, keyword revocation,
   task-dispatch revocation, and hard search-prompt changes from the feature worktree.
3. Record the product contract: card identity exclusion and confirmed topic recommendation exclusion
   are separate from fetch policy.
4. Remove the pre-existing coupling that passed ordinary profile dislikes into cross-digest pending
   keyword reconciliation; retain independent age, duplicate, cap, and supply-saturation cleanup.

## Wave 2 — Immediate effective preference

1. Make `SoulEngine.get_profile()` overlay `get_effective_disliked_topics()` after applying user
   overrides, without changing `get_raw_profile()`.
2. Add a regression proving a flat preference write is visible before Soul rebuild and respects
   explicit removals.
3. Keep existing asynchronous exact/semantic pool purge as inventory cleanup, not as the output
   correctness boundary.

## Wave 3 — Final recommendation output checks

1. Add a shared deterministic row filter and stable dislike digest matching existing serve semantics.
2. Apply it to recommendation history before franchise capping; include current dislike digest in
   snapshot validity and reload if the digest changes during the read.
3. Invalidate snapshot on explicit profile edit as well as existing feedback/mutation paths.
4. Recheck reshuffle and append batches at the final HTTP boundary.
5. Filter OpenClaw cached and generated recommendations, excluding processed history.
6. Add final single-item notification filtering with fuzzy restoration disabled.
7. Expose structured topic/tag/description fields in recommendation-history rows so the shared filter
   has the same evidence as serve-time filtering.

## Wave 4 — Automated verification and false-positive review

1. Test exact, fuzzy, total-window restoration, single-item suppression, and stable digest behavior.
2. Test recommendation cache invalidation within TTL, in-flight output race behavior, OpenClaw
   fallback, and Soul overlay timing.
3. Run focused API, Soul, recommendation, storage, runtime, and OpenClaw test files.
4. Run Ruff format/check, strict MyPy, `git diff --check`, and the full pytest suite.

## Wave 5 — Real end-to-end acceptance

1. Build an opt-in isolated harness that loads the real local configuration without mutating the
   production database/profile.
2. Send the incident-style query through the real source client and require proof that the outbound
   request actually started and returned a real response (or an explicit environmental failure).
3. Persist the query as pending under the pre-dislike profile digest, then prove it survives the
   dislike-only digest change, is claimed, and reaches the real source again.
4. Seed a matching and a safe recommendation into the isolated runtime, persist the dislike, and
   call the real FastAPI recommendation endpoint immediately without waiting for cache TTL or Soul
   rebuild.
5. Assert search still occurred, the matching card is absent, and the safe control remains.
6. Save a privacy-safe JSON artifact and repeat affected tests after any acceptance fix.

## Wave 6 — Documentation and handoff

1. Update all documents required by `CLAUDE.md#documentation-requirements` for Soul,
   recommendation, storage/API output, and cross-module data flow.
2. Report exact automated and real-request results, environmental limitations, and residual risks.
3. Leave the feature-worktree changes uncommitted unless the user explicitly requests a commit.

## Final verification

- Real Bilibili acceptance: authenticated configured route, two genuine same-query requests, three
  results each; pending query survived the dislike digest change and was claimed.
- Recommendation API: isolated Uvicorn served over real loopback TCP/HTTP; two rows before dislike,
  one immediately after, with the matching real row hidden and safe control retained without Soul
  rebuild or one-second TTL wait.
- Focused keyword planner/storage regression: `129 passed`.
- Affected API/Soul/recommendation/OpenClaw/runtime/storage regression: `1210 passed`.
- Final repository gates: touched-file Ruff format passed, full Ruff lint passed, strict MyPy passed
  for 243 source files, and pytest finished with `7618 passed, 50 skipped` in 14m09s.
- The repository-wide Ruff format baseline still proposes formatting three untouched historical E2E
  files; they were deliberately left unchanged. Every file touched by this work passes format check.
