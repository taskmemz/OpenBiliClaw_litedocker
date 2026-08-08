# Evaluator Sparse JSON Production Landing Spec

**Status:** approved for implementation after the 100-candidate × 3-repeat sparse replay passed.

## 1. Decision

Land only the canonical `sparse-json` candidate transport for batch content evaluation. Keep strict JSON
model output and the existing reason/classification contract. Do not land `row-wire-v1`: its real replay
missed the locked prompt-token and classification gates.

The accepted sparse replay measured median paired savings of `27.99%` prompt tokens and `24.05%` total
tokens while passing the A/A-relative quality, classification, repair, route, embedding, recall, usage and
privacy gates. This landing does not depend on provider caching, provider-specific tokenizers, reasoning
controls or structured-output extensions.

## 2. Production behavior

- `ContentDiscoveryEngine()` defaults `evaluation_candidate_transport` to `sparse-json`.
- All normal API, CLI, OpenClaw and source-strategy construction paths inherit that default without adding a
  user-facing config or CLI flag.
- Batch candidates use the already-tested canonical sparse envelope, request-local IDs and strict result
  binding. Multimodal anchors use the same local IDs; image bytes, MIME and order remain unchanged.
- Single-item evaluation retains the same fields and scoring contract. The merged production contract also
  carries source `published_at` in sparse candidates and exact UTC `evaluated_at` in the top-level evaluation
  context so time-sensitive scoring remains provider-independent.
- `row-wire-v1` remains callable only through the explicit internal/replay seam and is never the default.
- Explicit `evaluation_candidate_transport="production"` remains the rollback switch for the
  pretty-JSON/global-ID candidate transport while sharing the current time-semantics contract.

## 3. Cache and prompt contracts

- Bump the evaluator cache namespace from `content-eval-v2` to `content-eval-v3`. No batch result produced
  under the old global-ID/pretty-JSON prompt may be reused after the default changes.
- Sparse batch cache keys retain the transport marker and canonical prompt-visible digest. URL/global-ID
  fields omitted from the sparse prompt must not create false misses; retained body, `published_at`, metrics,
  mode, profile, negative examples, embedding namespace, prefilter mode and the UTC evaluation-hour bucket
  must continue to invalidate correctly.
- The sparse system prompt is a byte-static module-level constant shared by sparse JSON and row replay.
  Per-call candidate/profile data remains in the user message. Update `CLAUDE.md` prompt-cache documentation
  to describe the new production constant and rollback seam.
- Provider cache hit ratio remains diagnostic, not a cross-model correctness gate.

## 4. Safety and rollback

- Multi-member positional fallback remains forbidden. Unknown, duplicate or missing local IDs enter bounded
  member repair; exhausted members retain the existing `evaluation_response_missing` behavior.
- No raw URL or global candidate ID may enter the sparse candidate block or local image anchor.
- Mixed content-type sparse batches continue to bypass normal member cache where batch defaults cannot be
  reconstructed safely after partial hits.
- Rollback is one isolated default change back to `production` plus a cache namespace bump; it must not
  require reverting the canonical codec, replay evidence or parser-hardening tests.

## 5. Acceptance gates

- Tests prove the no-argument engine default is sparse, explicit production rollback preserves its candidate
  transport contract, and row wire is not selected implicitly by any production constructor.
- Candidate pipeline cold/warm/admission, member repair, mixed platform/type, multimodal local anchors and
  cache invalidation pass end to end.
- Static sparse measurement remains deterministic on the frozen cohort; no new real-provider replay is
  required unless landing changes the accepted candidate/system/output semantics.
- Ruff format/check, MyPy strict, full Pytest and documentation checks pass.
- Mandatory discovery module, changelog and `CLAUDE.md` prompt-cache documentation are updated. No config,
  CLI, installer or architecture diagram changes are required because the public surface and module wiring
  remain unchanged.
