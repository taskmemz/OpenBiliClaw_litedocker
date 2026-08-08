# Profile Views Spec — one façade for every profile→LLM surface

**Created:** 2026-07-18
**Scope:** profile→prompt serialization across discovery / recommendation / soul speculation,
the chat core-memory path (`memory/manager.py` → `llm/service.py` injection), core-memory
injection defaults on maintenance callers, guard tests, and the profile-usage registry doc.
**Out of scope:** profile *generation* (ProfileBuilder / analyzers / consolidator logic), the
three web UIs and `/api/profile-summary` response shape, the openclaw external schema (docs
annotation only), eval personas (`eval/` renders portraits intentionally), enforce-prefilter
rollout and model-tier downgrades (owned by the token-diet follow-ups), delight scoring
(embedding-only today — no LLM profile prompt exists there; see D8).

## Goal

The profile reaches LLM prompts through **six different serialization paths** today. Three are
designed (full / compact / query-trimmed, all funneling through `build_profile_summary`); three
grew by accident: a string-based `to_llm_context` fork in the speculators, the chat core-memory
path with three latent defects, and an unaudited `inject_core_memory=True` default that ships
the full core-memory block (portrait included) into callers that never asked for a profile.
Target outcomes:

- Every profile→LLM serialization lives in **one module** (`soul/profile_views.py`) as a named
  view; consumers import views instead of inventing serializers. Verified by a structural guard
  test (V1 below).
- `personality_portrait` reaches **only** dialogue-family prompts, the openclaw external API,
  UI responses, and eval personas — never a content-pipeline prompt. Verified by parametrized
  exclusion tests (V2).
- Chat respects user profile edits (today it reads the raw soul layer and silently ignores
  `profile_overrides.json`), and its system prompt splits into a byte-stable prefix (portrait /
  core / values / top interests) plus volatile context moved to the user message, restoring the
  provider prompt-cache discount for every chat turn.
- Maintenance callers stop paying for core-memory injections they don't use; the page-extractor
  leak is closed.

Verification metric: `openbiliclaw cost --by caller` (tokens/call and cache-hit% for
`sources.*.extract`, `soul.dialogue`, maintenance callers) before vs after, plus the new guard
test module `tests/test_profile_views.py`.

## Design invariants (MUST hold in every phase)

1. **Single serialization module:** after Phase 3, every function that turns a profile object
   into prompt-bound text/dict is defined in `soul/profile_views.py` (legacy import paths may
   re-export). Guard: a test walks `src/openbiliclaw` and asserts the known serializer names
   are defined only there and that content-pipeline modules import them from it.
2. **Portrait boundary:** `personality_portrait` appears in the output of the
   `chat_core_memory` and `external` views only. Guard: parametrized test feeds a profile with
   a sentinel portrait string into every view and asserts absence/presence per the registry.
3. **Pure, deterministic views:** every view is a pure function of the *effective* profile
   (plus explicit parameters); two calls with equal input are byte-identical
   (`json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)` for dict views). Inherited
   red line from the token-diet spec: **per-batch relevance trimming of the profile block stays
   rejected** — trimming may vary only when the profile changes.
4. **Effective profile only:** all views — chat included — consume
   `SoulEngine.get_profile()` output (AI ⊕ overrides, `soul/engine.py:449`), never raw memory
   layers. Guard: test applies a user override, renders the chat view, asserts the edit shows.
5. **Prompt-cache convention (CLAUDE.md):** static system prompts; per-call data in the user
   message, most-stable-first. The socratic exception keeps *stable* per-user state in system —
   Phase 4 narrows the injected block to actually-stable sections so the exception's own
   stability rationale holds. `test_prompt_builder_system_messages_are_call_invariant` keeps
   passing.
6. **Measure before you change model-visible input:** Phases 4–6 alter what models see, so each
   carries an explicit gate (byte-equivalence where claimed, section-parity + override tests
   for chat, dry-run op-diff for maintenance callers). No gate, no merge.
7. **Existing caps and digests survive:** view moves must not change field caps
   (`_utils.py:25-29` dislike floor, compact caps at `_utils.py:60`) or any
   `profile_digest` semantics; digests keep covering exactly the prompt-visible slice.

## Current diagnosis

### D1. Three designed serializers live in a discovery util module

`discovery/strategies/_utils.py` owns `compact_content_prompt_profile_summary` (`:60`),
`build_profile_summary` (`:595`), `build_query_generation_profile_summary` (`:1053`). They are
soul-domain logic consumed by discovery, recommendation, runtime, and sources; the module path
invites platform code to grow private variants (that is how the pre-diet forks appeared).

### D2. Speculators serialize through a divergent string path

`soul/speculator.py:1386` and `soul/avoidance_speculator.py:1271` call
`profile.to_llm_context(include_portrait=False)` (`soul/profile.py:115`, `:720`) — a second,
string-shaped serialization with its own field selection. Portrait-safe today, but nothing
guards it, and its field set drifts independently of `build_profile_summary`.

### D3. Chat ignores user profile edits

`MemoryManager.get_core_memory()` (`memory/manager.py:593`) reads `self._layers["soul"].data`
(raw persisted layer, `:599`) and never applies `profile_overrides.json`. Content pipelines go
through `get_profile()` (`soul/engine.py:449`, AI ⊕ overrides). Confirmed consequence: a user
edit to portrait/traits shows in UI and in evaluation prompts but chat keeps speaking from the
pre-edit profile.

### D4. Chat's cache prefix is broken by volatile sections

`render_core_memory_prompt()` (`memory/manager.py:677`) embeds `recent_awareness` and
`active_insights` alongside portrait/traits; `complete_with_core_memory` injects the whole
block into the **system** prompt (`llm/service.py:360`, defaults `inject_core_memory=True` at
`:372`, `:464`, `:535`). Every awareness/insight churn (12h cognition cycle, ongoing events)
rewrites the system prefix, defeating provider prompt caching on the highest-frequency
interactive caller.

### D5. Dead plumbing in the socratic path

`build_socratic_dialogue_prompt` has a `core_memory_text` parameter (`llm/prompts.py:177`,
`:180`, rendered at `:212`) that both call sites pass as `""`; the real injection happens in
the `complete_with_core_memory` wrapper. `soul/dialogue.py:147` getattr-references
`service._build_core_memory_block`, which does not exist anywhere → silently returns None.
CLAUDE.md's prompt-cache exception attributes the injection to the builder — wrong place.

### D6. Page extractor leaks core memory (confirmed bug)

`sources/llm_extractor.py:82` calls `complete_structured_task` without
`inject_core_memory=False`. Every other content-pipeline call site opts out. Result: page
extraction prompts carry the full core-memory block (portrait included) they never read, and
the caller's cache prefix churns with the profile.

### D7. Maintenance callers inherit full core-memory injection unaudited

Un-opted `complete_structured_task` calls: `soul/consolidator.py:809` (12h judge batches),
`soul/layer_updaters.py:320/:415/:526`, `soul/category_migration.py:141`,
`soul/pool_purge.py:196`, `soul/dialogue_insight_analyzer.py:64`, and probe sentiment at
`api/app.py:6241`. Some plausibly want profile context (dialogue-insight, sentiment); the
mechanical ones (consolidator judge, category migration, pool purge) already receive the data
they judge in their user prompts. Nobody has decided per-caller; the default decides.

### D8. Delight needs no phase (stale memory corrected)

`recommendation/delight.py` scores via embeddings only (`:226`, `:259`, `:334`, `:382`) — no
LLM call, no profile prompt. The earlier "fold delight's compact summary into the unified
serializer" follow-up is moot; recorded here so it stops resurfacing.

### D9. No guard tests on the core invariant

No test asserts `build_profile_summary` excludes `personality_portrait` (shape tests set a
portrait on input and never check the output); openclaw's `[:5]` trims
(`integrations/openclaw/operations.py:113`) are never exercised with >5 items.

## Priority classification

| Phase | Content | Tier | Why |
| --- | --- | --- | --- |
| 0 | Land `perf/llm-token-diet` on main (+release, replay-gate rerun) | **MUST (gate)** | every phase below edits code that exists only on that branch |
| 1 | Extractor opt-out, guard tests, dead-plumbing cleanup, CLAUDE.md fix | **MUST** | confirmed leak + unguarded core invariant |
| 2 | Profile-usage registry doc | RECOMMENDED | the map that makes per-stage tailoring deliberate |
| 3 | `soul/profile_views.py` façade (mechanical move) | RECOMMENDED | physical convergence; byte-equivalent, zero model-visible change |
| 4 | Chat core-memory view (overrides + stable-prefix split) | RECOMMENDED | correctness bug + biggest interactive-path cache win |
| 5 | Speculator serializer convergence | OPTIONAL | fork removal; low traffic |
| 6 | Maintenance-caller injection audit | OPTIONAL | token savings on 12h/purge paths |

Dependencies: 1–2 need 0; 3 needs 1 (guards catch regressions during the move); 4–6 need 3.
**Wave A** = Phases 1–2. **Wave B** = Phase 3. **Wave C** = Phases 4–6, individually shippable;
work may safely stop after any phase.

## Phase designs

### Phase 0 — Land the diet branch (external gate)

Operational, user-triggered: ff-merge `perf/llm-token-diet` (at `db04117e`) into main, release,
rerun `scripts/run_profile_diet_ab.py` A/A + A/B per the token-diet spec gate. No code here.

### Phase 1 — Close the leak, guard the boundary

- `sources/llm_extractor.py:82`: add `inject_core_memory=False` (pattern:
  `without_core_memory_kwargs`, as in sibling callers).
- New `tests/test_profile_views_guards.py`: sentinel-portrait exclusion for the three dict
  serializers and the speculator context; determinism (two-call byte-equality); openclaw trim
  test with 8 traits/interests asserting `[:5]`.
- Delete the dead `core_memory_text` parameter path: drop the getattr probe at
  `soul/dialogue.py:147`, pass-through removal in `llm/prompts.py:177-212` **only if** the
  builder keeps a documented seam for tests; otherwise keep the parameter and document it as
  the injection seam. Update CLAUDE.md's exception paragraph to name
  `complete_with_core_memory` as the injection point.
- Acceptance: new guard tests pass; `grep -rn "inject_core_memory" src/openbiliclaw/sources/`
  shows the extractor opted out; `openbiliclaw cost --by caller` shows `sources.*.extract`
  cache-hit% recovering after a day of runtime (observation, not merge gate).

### Phase 2 — Profile-usage registry

`docs/profile-usage.md` (linked from `docs/modules/soul.md`): one row per consumer surface —
trigger cadence, view used, fields, caps, portrait yes/no, LLM yes/no — seeded from this spec's
D-table and the 2026-07-16 audit. Doc-only; acceptance is review.

### Phase 3 — `soul/profile_views.py` façade

Move the three serializers from `_utils.py` verbatim; `_utils.py` re-exports (deprecation
comment, no behavior change). Views named `full`, `compact_content`, `query_taste`. Byte-
equivalence acceptance: golden test renders three representative profiles (young / mature /
legacy-flat) through old import path and new module — outputs byte-identical; full suite +
mypy/ruff green. Grep-based structural guard (invariant V1) lands here.

### Phase 4 — `chat_core_memory` view

Rebuild `render_core_memory_prompt()` content from `get_profile()` via the façade:
`stable_block` (portrait + core traits + values + deep needs + MBTI + top-5 interests + top-5
dislikes + favorite UPs) and `volatile_block` (recent awareness, active insights).
`complete_with_core_memory` injects only `stable_block` into system; `volatile_block` moves to
the user message ahead of the turn content (most-stable-first ordering preserved).
- Acceptance: (a) override test — edit portrait via `apply_user_edit`, chat system prompt
  reflects it; (b) prefix-stability test — mutate awareness notes between two renders, system
  bytes identical, user message differs; (c) section-parity golden — stable sections match the
  pre-change render field-for-field on the same profile; (d) existing dialogue tests green.
  Manual smoke by the user before release (single-user product; the user is the judge).

### Phase 5 — Speculation view

Replace `to_llm_context(include_portrait=False)` at the two call sites with a façade view
(`speculation`, string-rendered from the same dict pipeline). Acceptance: speculator prompt
snapshot test shows same sections (order may normalize); speculation structured-output
validation pass rate unchanged on `tests/test_speculator*`; portrait exclusion guard extends to
the new view.

### Phase 6 — Maintenance injection audit

Per-caller decision table (consolidator judge, layer updaters ×3, category migration, pool
purge, dialogue-insight, probe sentiment): needs-nothing → `inject_core_memory=False`;
needs-taste → `query_taste`/`compact_content` view in the user prompt; dialogue-family → keep
core memory. Acceptance per changed caller: existing validation tests green; for the
consolidator, `openbiliclaw profile-consolidate --dry-run` on the real DB before/after produces
an identical op list (merge/keep decisions unchanged); `openbiliclaw cost --by caller` records
the tokens/call drop in the PR.

## Expected impact

| Lever | Measured effect |
| --- | --- |
| P1 extractor opt-out | `sources.*.extract` stops carrying `len(render_core_memory_prompt())` chars/call (measure: `python -c` render on the live profile; expect ~0.5–1.5k tokens on a mature profile) + prefix cache restored |
| P4 stable-prefix chat | system prefix survives awareness churn → provider cached-input discount (90% DeepSeek/Claude) applies to the largest block on every chat turn; overrides honored (correctness) |
| P6 injection audit | each audited caller drops the same block/call on 12h + purge cadences |
| P3/P5 façade | zero token change (byte-equivalence gated); fork count 6 → 1 module |

## Documentation obligations

- `docs/modules/soul.md` — new "profile views" section + link to registry; portrait-boundary
  statement.
- `docs/modules/memory.md` — core-memory stable/volatile split (Phase 4).
- `docs/modules/discovery.md` / `recommendation.md` / `llm.md` — import-path note (Phase 3),
  injection-default table (Phase 6).
- `docs/modules/integrations.md` — annotate the deliberate portrait re-exposure
  (`operations.py:113`).
- `CLAUDE.md` — fix the socratic exception attribution (Phase 1); add "new profile-bearing
  prompts must consume a `profile_views` view" to the prompt conventions (Phase 3).
- `docs/changelog.md` — bullet per landed phase. `docs/profile-usage.md` — new (Phase 2).
- Architecture diagrams unaffected (no cross-module wiring change; façade is intra-soul).
