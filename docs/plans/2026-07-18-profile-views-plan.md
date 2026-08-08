# Profile Views — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: superpowers:executing-plans (execute this plan task-by-task).
> **Spec:** [`2026-07-18-profile-views-spec.md`](./2026-07-18-profile-views-spec.md)
> **Status:** r1 — drafted 2026-07-18, awaiting user review; Phase 0 (landing
> `perf/llm-token-diet` on main) is a user-triggered gate, not a plan task.
> **Execution order:** Task 1 → 2 → 3 → 4 (Wave A) → Task 5 (Wave B) → Tasks 6 → 7 → 8
> (Wave C, individually shippable; stop after any task is safe).
> **Tech:** Python 3.11+ via each worktree's own venv — `.venv/bin/python -m pytest
> tests/<file> -q` (focused), `.venv/bin/python -m pytest -q` (full),
> `.venv/bin/ruff format src/ tests/ && .venv/bin/ruff check src/ tests/`,
> `.venv/bin/mypy src/`. Always `git -C <absolute worktree path>` (shell cwd resets between
> turns).

**Invariants that MUST hold — re-read before each task:**

- **Single serialization module:** after Task 5, profile→prompt serializers are defined only in
  `soul/profile_views.py`; legacy paths re-export. The structural guard test enforces this.
- **Portrait boundary:** `personality_portrait` appears only in the `chat_core_memory` and
  `external` views (plus UI / eval personas, out of scope). Every view is covered by the
  sentinel-portrait test.
- **Pure, deterministic views:** two calls with equal effective-profile input are
  byte-identical; per-batch relevance trimming of the profile block remains REJECTED.
- **Effective profile only:** every view consumes `SoulEngine.get_profile()` (AI ⊕ overrides,
  `soul/engine.py:449`); no view reads raw memory layers.
- **Prompt-cache convention:** static system prompts; per-call data in user messages,
  most-stable-first; `json.dumps(..., ensure_ascii=False, indent=2, sort_keys=True)`;
  `test_prompt_builder_system_messages_are_call_invariant` stays green.
- **Measure before you change model-visible input:** Tasks 6–8 gates are stated numerically in
  each acceptance block; no gate, no merge.
- **Caps and digests survive:** dislike floor (`_utils.py:25-29`), compact caps (`:60`), and
  every `profile_digest` keep covering exactly the prompt-visible slice.

### Task 1: Close the extractor core-memory leak

**Files:** modify `src/openbiliclaw/sources/llm_extractor.py`; test
`tests/test_llm_extractor.py` (extend).

**Interfaces:** Consumes: `llm_service.complete_structured_task`. Produces: extraction calls
with `inject_core_memory=False` (via `without_core_memory_kwargs`, matching sibling callers).

**Steps:**

- [x] Write one focused failing test: fake service records kwargs; assert the extractor call
      carries `inject_core_memory=False`.
- [x] Run `.venv/bin/python -m pytest tests/test_llm_extractor.py -q` and confirm FAIL for the
      missing opt-out.
- [x] Add `**without_core_memory_kwargs(complete_structured)` at `llm_extractor.py:82`.
- [x] Rerun the focused test and confirm PASS with no warnings.
- [x] Run `.venv/bin/ruff check src/ tests/` and `.venv/bin/mypy src/`.

**Acceptance:**

- Numeric gate: 0 un-opted `complete_structured_task` call sites under
  `src/openbiliclaw/sources/`; reproduce with
  `grep -rn "complete_structured_task" src/openbiliclaw/sources/ | grep -v without_core_memory`.
- Post-merge observation (not a merge gate): `openbiliclaw cost --by caller` shows
  `sources.*.extract` cache-hit% recovering.

### Task 2: Guard tests for the portrait boundary and determinism

**Files:** add `tests/test_profile_views_guards.py`; extend `tests/test_openclaw_adapter.py`.

**Interfaces:** Consumes: `build_profile_summary`, `compact_content_prompt_profile_summary`,
`build_query_generation_profile_summary`, `OnionProfile.to_llm_context`,
`openclaw operations.get_profile`. Produces: the standing guard suite later extended by
Tasks 5–7.

**Steps:**

- [x] Write failing tests: (a) sentinel portrait `"PORTRAIT_SENTINEL_XYZ"` absent from the
      serialized output of each of the three dict serializers and of
      `to_llm_context(include_portrait=False)`; (b) two-call byte-equality per serializer;
      (c) openclaw `get_profile` with 8 traits / 8 interests returns exactly 5 of each.
- [x] Run `.venv/bin/python -m pytest tests/test_profile_views_guards.py tests/test_openclaw_adapter.py -q`;
      confirm the new tests FAIL only if a real defect exists — expected result is PASS for
      (a)/(b) (behavior already correct, tests pin it) and PASS for (c); if any FAILS, stop and
      report before changing production code.
- [x] Run touched regression tests, `.venv/bin/ruff check`, `.venv/bin/mypy src/`.

**Acceptance:**

- Numeric gate: 4 serializer surfaces × sentinel test + 1 trim test, all green; mutation check
  — temporarily adding `"personality_portrait": profile.personality_portrait` to
  `build_profile_summary` must turn (a) red (verify once locally, revert).

### Task 3: Dead-plumbing cleanup + CLAUDE.md attribution fix

**Files:** modify `src/openbiliclaw/soul/dialogue.py`, `CLAUDE.md`; test
`tests/test_soul_dialogue.py` (touched paths only).

**Interfaces:** Consumes: `complete_with_core_memory` / `complete_with_tools` injection
(`llm/service.py:360`, `:611`). Produces: `dialogue.py` without the phantom
`_build_core_memory_block` probe (`:147`); `build_socratic_dialogue_prompt`'s
`core_memory_text` parameter kept but documented as the test seam, with a docstring stating
the production injection point is `complete_with_core_memory`.

**Steps:**

- [x] Write one focused test asserting `_respond_with_tools` builds its prompt without
      consulting `_build_core_memory_block` (fake service without the attr → no getattr path).
- [x] Run `.venv/bin/python -m pytest tests/test_soul_dialogue.py -q`; confirm the relevant test
      FAILS against the phantom-probe code path (or record that removal is behavior-neutral).
- [x] Remove the getattr probe; add the docstring note in `llm/prompts.py:177`; rewrite the
      CLAUDE.md exception paragraph to name `complete_with_core_memory`.
- [x] Rerun focused tests → PASS; run `.venv/bin/ruff check`, `.venv/bin/mypy src/`.

**Acceptance:**

- Numeric gate: `grep -rn "_build_core_memory_block" src/` returns 0 hits; dialogue test file
  green.

### Task 4: Profile-usage registry doc

**Files:** add `docs/profile-usage.md`; modify `docs/modules/soul.md` (link),
`docs/modules/integrations.md` (portrait re-exposure note), `docs/changelog.md` (Wave-A
bullet).

**Interfaces:** Consumes: spec D-table + 2026-07-16 audit. Produces: the registry Tasks 5–8
keep updated.

**Steps:**

- [x] Draft the table: surface / cadence / view / fields / caps / portrait? / LLM? — one row
      per consumer from spec D1–D8, including non-LLM direct reads (seeds, digests, filters).
- [x] Cross-check every row's `file:line` against the working tree (no stale citations).
- [x] Add the integrations note: portrait re-exposure at `operations.py:113` is deliberate.

**Acceptance:**

- Review gate: every row cites a live `file:line` (spot-verify 5 rows by opening the files);
  changelog bullet present under the current version block.

### Task 5: `soul/profile_views.py` façade (mechanical move)

**Files:** add `src/openbiliclaw/soul/profile_views.py`,
`tests/test_profile_views.py`; modify `src/openbiliclaw/discovery/strategies/_utils.py`
(re-export stubs), `CLAUDE.md` (new-consumer rule), `docs/modules/soul.md`,
`docs/modules/discovery.md`.

**Interfaces:** Consumes: the three serializer bodies from `_utils.py:60/:595/:1053` and their
private helpers. Produces: named views `full`, `compact_content`, `query_taste`; `_utils.py`
re-exports the old names unchanged.

**Steps:**

- [x] Write failing byte-equivalence golden test: three fixture profiles (young ~10 interests /
      mature 200+ / legacy flat `SoulProfile`) rendered via old import path and via
      `profile_views` — outputs byte-identical (test imports both paths; fails until module
      exists). *(Implemented as frozen pre-move golden snapshots under
      `tests/golden/profile_views/` vs the new module — re-export makes the two live paths the
      same object, so comparing against pre-move bytes is what actually verifies "no change".)*
- [x] Move the functions verbatim (helpers included or imported); add re-export lines in
      `_utils.py` with a deprecation comment. *(`normalize_match_text` /
      `_coerce_query_embedding_vector` moved too — soul must not import discovery — and travel
      back as re-exports alongside the `_CONTENT_PROMPT_*` caps that `discovery/engine.py`
      imports.)*
- [x] Add the structural guard: test walks `src/openbiliclaw`, asserts the serializer names are
      defined only in `profile_views.py` and that `discovery/`, `recommendation/`, `runtime/`,
      `sources/` modules referencing them import from `profile_views` or the `_utils` re-export.
- [x] Rerun `.venv/bin/python -m pytest tests/test_profile_views.py tests/test_search_strategy.py tests/test_recommendation_engine.py -q` → PASS.
- [x] Full suite `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check`, `.venv/bin/mypy src/`.

**Acceptance:**

- Numeric gate: byte-equality on 3 fixture profiles × 3 views = 9 comparisons, all identical;
  full suite has 0 new failures vs the pre-task baseline run.

### Task 6: `chat_core_memory` view — overrides + stable-prefix split

**Files:** modify `src/openbiliclaw/memory/manager.py` (`get_core_memory`,
`render_core_memory_prompt`), `src/openbiliclaw/llm/service.py` (inject stable block to
system, volatile block to user message), `src/openbiliclaw/soul/profile_views.py` (new view);
tests `tests/test_memory_manager.py`, `tests/test_llm_service.py`, `tests/test_soul_dialogue.py`;
docs `docs/modules/memory.md`, `docs/changelog.md`.

**Interfaces:** Consumes: `SoulEngine.get_profile()` (effective profile) — requires wiring the
engine (or a profile provider callback) into `MemoryManager`, which today reads its own layers.
Produces: `chat_core_memory` view returning `(stable_block, volatile_block)`; system prompt
carries only `stable_block`.

**Steps:**

- [x] Write failing test (a): apply `apply_user_edit` portrait override → rendered stable block
      contains the edited text (today FAILS — raw layer read at `manager.py:599`).
- [x] Write failing test (b): mutate awareness notes between two renders → system-bound block
      byte-identical, user-bound block differs (today FAILS — both live in one block).
- [x] Write golden test (c): stable sections (portrait / traits / values / needs / MBTI /
      top-5 interests / top-5 dislikes / favorite UPs) match the pre-change render
      field-for-field on the same profile. *(Golden asserts no-loss of the pre-change
      sections; identity fields — 核心特质/价值观/深层需求/MBTI — are additive enrichment
      per the stable-block spec, placed between 用户画像 and 偏好摘要.)*
- [x] Implement the view + injection split; keep `render_core_memory_prompt()` as a
      compatibility wrapper returning stable+volatile concatenated for non-chat readers.
      *(Overrides landed in `MemoryManager._effective_soul_data`: sync round-trip
      from_dict→apply_overrides→to_dict, short-circuited when overrides empty. Service uses a
      getattr-guarded `_core_memory_blocks` so pre-split memory doubles keep single-block
      injection.)*
- [x] Rerun focused tests → PASS; full suite, ruff, mypy.

**Acceptance:**

- Numeric gate: tests (a)/(b)/(c) green; `test_prompt_builder_system_messages_are_call_invariant`
  green; 0 new failures in `tests/test_soul_dialogue.py` + `tests/test_soul_engine.py`.
- Manual gate before release: user smoke-tests chat (single-user product — the user is the
  quality judge); rollback = revert the injection-split commit, view remains unused.

### Task 7: Speculation view convergence

**Files:** modify `src/openbiliclaw/soul/speculator.py:1386`,
`src/openbiliclaw/soul/avoidance_speculator.py:1271`, `src/openbiliclaw/soul/profile_views.py`
(new `speculation` view); tests `tests/test_speculator.py`,
`tests/test_avoidance_speculator.py`, guard suite extension.

**Interfaces:** Consumes: the dict pipeline behind `query_taste`/`full`. Produces: a
string-rendered `speculation` view replacing `to_llm_context(include_portrait=False)` at both
call sites.

**Steps:**

- [x] Snapshot current speculator prompt sections on a fixture profile (golden file).
      *(Frozen pre-move `to_llm_context(include_portrait=False)` output at
      `tests/golden/profile_views/speculation__{young,mature,legacy_flat}.txt`; the
      three fixtures reuse `tests/test_profile_views.py`, covering both the onion
      and flat-`SoulProfile` renderers.)*
- [x] Write failing test: both call sites consume `profile_views.speculation`; sentinel
      portrait absent.
      *(Guard suite extended in `tests/test_profile_views_guards.py` — sentinel
      exclusion + two-call byte-equality on both shapes; golden byte-parity + a
      delegation-identity assertion in `tests/test_profile_views.py`.)*
- [x] Implement; normalize section order only if the golden diff stays section-equivalent.
      *(Chose option (a): the `speculation` view delegates to the profile's own
      `to_llm_context(include_portrait=False)` — a pure entry-point move, zero
      section/order change. Avoidance keeps its getattr `{}` fallback; the
      `build_avoidance_generation_prompt` param widened to `str | dict` to make the
      long-standing string-at-runtime type honest.)*
- [x] Rerun `tests/test_speculator*.py -q` → PASS; ruff, mypy.

**Acceptance:**

- Numeric gate: speculation structured-output validation pass rate in the existing test
  fixtures unchanged (same pass/fail counts); golden shows the same sections present.

### Task 8: Maintenance-caller injection audit

**Files:** modify per decision table: `soul/consolidator.py:809`,
`soul/layer_updaters.py:320/:415/:526`, `soul/category_migration.py:141`,
`soul/pool_purge.py:196`, `soul/dialogue_insight_analyzer.py:64` (expected: keep),
`api/app.py:6241` (probe sentiment — decide); tests per touched module; docs
`docs/modules/llm.md` (injection-default table), `docs/changelog.md`.

**Interfaces:** Consumes: Task 5 views. Produces: each caller either opted out, switched to an
explicit light view in its user prompt, or documented as intentionally core-memory-bearing.

**Steps:**

- [x] Fill the per-caller decision table (needs-nothing / needs-taste / dialogue-family) with a
      one-line justification each; commit the table into `docs/profile-usage.md` first.
      *(4 opt-out: consolidator judge, category migration, pool purge, dialogue-insight — the
      last already serializes the full core_memory dict into its own user prompt, so injection
      was a duplicate. 4 keep + in-code comment: layer_updaters ×3 update the profile layer
      itself, probe sentiment is chat-adjacent. No needs-taste caller emerged — the opt-outs
      need nothing, the keeps want the full core memory.)*
- [x] For each needs-nothing caller: focused failing kwargs test → opt-out → PASS (Task 1
      pattern). *(`tests/test_maintenance_injection_audit.py` for pool purge + dialogue-insight;
      `test_consolidation_judge_opts_out_of_core_memory_injection` and
      `test_category_mapping_opts_out_of_core_memory_injection` in the module suites.)*
- [x] For each needs-taste caller: swap injected core memory for an explicit view in the user
      prompt; update that module's prompt-shape tests. *(N/A — no caller was needs-taste.)*
- [x] Consolidator safety check: `openbiliclaw profile-consolidate --dry-run` against the real
      DB before and after; diff the op lists. *(Real dry-run is non-deterministic at
      temperature 0.2 and the real DB lives in the forbidden main worktree, so the spec's Path B
      applies: a deterministic mocked-LLM equivalence test pins that the judged cluster payload
      and parsed ops are independent of the injection flag — removing injection cannot change any
      merge/keep decision.)*
- [x] Full suite, ruff, mypy.

**Acceptance:**

- Numeric gate: consolidator dry-run op list identical before/after (0 changed merge/keep
  decisions); all touched modules' validation tests green; record per-caller tokens/call delta
  from `openbiliclaw cost --by caller` in the PR.

## Verification after merge

- After each Wave-C task ships: 48h observation of `openbiliclaw cost --by caller` (owner:
  user's daemon; I read the numbers) — expect cache-hit% recovery on `sources.*.extract`
  (Task 1) and `soul.dialogue` (Task 6), tokens/call drop on audited maintenance callers
  (Task 8), and **no** admission-rate / pool-quality shift (`openbiliclaw recommend` spot
  check + pool snapshot diversity logs).
- Rollback trigger: any pool-quality regression report or a chat-persona complaint from the
  user → revert the corresponding task commit (each task is one commit; views are additive so
  reverts are local).

## Explicitly out of scope

- Enforce-prefilter flip and `[llm.evaluation]` model downgrade (token-diet follow-ups, own
  gates).
- Profile generation/consolidation logic, UI surfaces, `/api/profile-summary` shape, openclaw
  schema changes.
- Delight scoring (embedding-only; no LLM profile prompt exists — spec D8).
- Multi-user refactor of the socratic system-prompt exception.
