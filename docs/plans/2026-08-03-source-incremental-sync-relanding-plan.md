# Source Incremental Sync Relanding — Implementation Plan

> **Spec:** [`2026-08-03-source-incremental-sync-relanding-spec.md`](./2026-08-03-source-incremental-sync-relanding-spec.md)<br>
> **Base:** started from `origin/main` `15e61bc0`; synchronized through `origin/main` `4dda1bf8` before final landing verification.<br>
> **Execution:** LunaMax (`gpt-5.6-luna`, `max`) multi-agent worktrees; primary agent owns integration, review, fixes, and final verification.<br>
> **Rule:** the old `source-incremental-sync` branch is read-only reference. Never merge, rebase, or cherry-pick its implementation commits.<br>
> **Status:** implemented, independently reviewed, and fully reverified for landing on 2026-08-06.

## Execution record

- LunaMax (`gpt-5.6-luna`, `max`) agents implemented the enqueue extraction, atomic state,
  Reddit staging, scheduler/runtime wiring, and an independent read-only review. The
  primary agent integrated the commits, reconciled every confirmed finding, updated docs,
  and owned final testing.
- Pre-change focused baseline: `1259 passed` (`2004 warnings`, 131.92s).
- Final focused backend gate: `1398 passed` (`2060 warnings`, 142.80s).
- Final complete backend gate: `7056 passed, 49 skipped` (`4540 warnings`, 880.93s).
- Extension gate: `1248 passed`; TypeScript `tsc --noEmit` and Chrome build passed.
- Static gate: Ruff format checked 541 files, Ruff check passed, MyPy passed for 236 source
  files, and `git diff --check` passed.
- Final landing gate after synchronizing `origin/main` `4dda1bf8`: all 7,540 collected
  backend tests completed with exit code 0 in 828.9 seconds; all 1,256 extension tests,
  TypeScript typecheck, Chrome build, Ruff, changed-file formatting, MyPy for 240 source
  files, and `git diff --check` passed.
- Independent review reported 2 high, 5 medium, and 2 low findings against the pre-fix
  snapshot. All confirmed correctness findings are closed with regression coverage,
  including SQLite-backed global admission, init reservation ordering, crash-adoption
  timing, config inheritance, receipt ordering, Reddit identity, strict timestamps, and
  documentation contracts.
- The original 2026-08-03 cross-platform scheduler smoke was **not run** because no
  confirmed logged-in extension environment was available then; those bullets remain
  explicitly unchecked below.
- On 2026-08-06, a real logged-in installed-extension XHS session became available. The
  landing smoke verified current `/explore` login-gate detection, a 20-note real search,
  capped saved/liked bootstrap ingestion (5 accepted rows per requested scope), canonical
  URL/note correlation, 10 durable events, and terminal replay idempotency with zero new
  events. It did not perform any state-changing XHS action or write the real profile/memory.

## Invariants to re-read before every task

- Reuse the five existing extension bootstrap scopes; never call blocking `_collect_*`
  functions from runtime.
- Directly gate on `PresenceTracker.is_present`, not `background_llm_work_allowed`.
- Keep current durable `EventIngressService` ownership and staged-terminal repair.
- Never allow more than one pending/in-progress account bootstrap across the five sources.
- Advance schedule state only for a newly created non-empty task id.
- Keep init/manual payload and user-visible behavior unchanged unless a test explicitly
  records an intentional change.
- Seen-key state is atomic and capped at 5,000 per source.
- Reddit cannot enter the scheduler before its durable-ingress and staged-result parity is
  complete.
- Every implementation change gets a regression test that fails without the change.
- Do not push, open a PR, release, or delete the legacy branch as part of this plan.

## Multi-agent integration model

Wave 1 is intentionally split across independent worktrees:

- **Agent A:** Task 1 only (enqueue extraction and CLI parity).
- **Agent B:** Task 2 only (atomic/bounded state).
- **Agent C:** Task 3 only (Reddit parity), rebased or replayed after Task 2 if its tests need
  the new state helpers.

The primary agent reviews and integrates each commit into
`feat/source-incremental-sync-relanding`. No agent edits the integration worktree directly.
After Wave 1 is green, one LunaMax implementation agent executes Tasks 4–5 from the
integrated branch while another LunaMax agent reviews scheduler races and test coverage.
The primary agent then performs an independent review, fixes findings, owns docs, and runs
all final gates.

## Task 0: Freeze the current-main baseline

**Owner:** primary agent<br>
**Files:** no production changes.

### Steps

- [x] Record `git status`, `git rev-parse HEAD`, Python version, and tool versions.
- [x] Run the focused pre-change contract suite:

  ```bash
  env -u OPENBILICLAW_PROJECT_ROOT PYTHONPATH=src .venv/bin/pytest \
    tests/test_source_task_completion_protocol.py \
    tests/test_api_app.py tests/test_cli.py tests/test_config.py \
    tests/test_memory_manager.py tests/test_refresh_runtime.py -q -p no:randomly
  ```

- [x] Record any pre-existing failure exactly; do not normalize a new failure as baseline.

### Acceptance

- The implementation branch is clean except for the committed spec/plan.
- Baseline results and duration are saved in the work log/commentary.

## Task 1: Extract non-CLI enqueue core and add force/incremental parity

**Owner:** LunaMax Agent A<br>
**Files:** add `src/openbiliclaw/sources/source_bootstrap.py`, modify
`src/openbiliclaw/cli.py`; add `tests/test_source_bootstrap.py`, update
`tests/test_cli.py` only where wrapper seams change.

### Produced interfaces

```python
@dataclass(frozen=True)
class BootstrapEnqueueResult:
    task_id: str | None
    created: bool
    reason: str

def enqueue_xhs_bootstrap(database, *, force=False, incremental=False, notify=None) -> BootstrapEnqueueResult: ...
def enqueue_dy_bootstrap(database, *, force=False, incremental=False, notify=None) -> BootstrapEnqueueResult: ...
def enqueue_yt_bootstrap(database, *, force=False, incremental=False, notify=None) -> BootstrapEnqueueResult: ...
def enqueue_zhihu_bootstrap(database, *, force=False, incremental=False,
                            profile_slug="", profile_update=False, notify=None) -> BootstrapEnqueueResult: ...
def enqueue_reddit_bootstrap(database, *, force=False, incremental=False,
                             profile_update=False, notify=None) -> BootstrapEnqueueResult: ...
```

Names may be refined, but the result must distinguish “created new row” from “reused
recent row”. The module takes an already-resolved database and performs no HTTP or event-hub
kick.

### Steps

- [x] Write failing tests for all five exact task types/scopes/default limits/budgets.
- [x] Write failing tests proving `force=false` preserves recent-task reuse and
  `force=true` creates a new row; retain Douyin's special retry of a degraded recent task.
- [x] Write failing tests proving `incremental=true` writes the backend payload marker and
  makes Zhihu/Reddit profile-update eligible; `incremental=false` preserves today's payload
  shape exactly.
- [x] Write an import-isolation test proving the new module does not load `cli`, Typer,
  Click, or Rich.
- [x] Move queue/payload/dedupe/budget logic into the new module. Keep environment override
  behavior and user text through an optional `notify` callback.
- [x] Convert each `_enqueue_*_bootstrap_task` into a thin CLI wrapper that resolves the
  runtime DB, maps `BootstrapEnqueueResult` back to the existing `str | None` return, prints
  the existing messages, and performs the existing kick only after a real task id.
- [x] Preserve the guided-init `kick=False -> register ownership -> kick` ordering.

### Acceptance

- Existing CLI tests plus `tests/test_source_bootstrap.py` pass.
- A normalized non-incremental payload snapshot for every source is unchanged.
- `rg -n "XhsTaskQueue|DyTaskQueue|YtTaskQueue|ZhihuTaskQueue|RedditTaskQueue" src/openbiliclaw/cli.py`
  finds only collector/fetch uses, not enqueue bodies.

## Task 2: Make source bootstrap state atomic and bounded

**Owner:** LunaMax Agent B<br>
**Files:** `src/openbiliclaw/sources/bootstrap_state.py`,
`src/openbiliclaw/memory/manager.py`, `src/openbiliclaw/api/app.py`;
`tests/test_memory_manager.py`, `tests/test_api_app.py` or a focused new state test module.

### Produced interfaces

- `SOURCE_SEEN_KEY_CAP = 5000`
- `merge_seen_keys(existing, new_keys, *, cap=...)`
- `MemoryManager.update_source_bootstrap_state(mutator)` using `update_json_state`
- backward-compatible normalization for `reddit_seen_item_keys` and the nested
  `source_incremental` state block.

### Steps

- [x] Write failing normalization tests for missing, malformed, old-version, and unknown
  state fields.
- [x] Write failing cap/recency tests: insert cap+1 evicts the oldest; re-marking a key
  moves it to the newest slot; blanks and duplicates collapse.
- [x] Write a concurrent two-writer test using separate threads/managers and assert the
  final state contains both writers' keys.
- [x] Implement atomic load/update/serialize using `memory.json_state.update_json_state`.
- [x] Change `_mark_source_bootstrap_keys` to one strict atomic mutator. It must raise on
  persistence failure so the surrounding staged terminal remains repairable.
- [x] Preserve `_accept_source_profile_events` as the only event write path; do not add a
  same-source lock unless a failing test demonstrates a fact-level duplication that
  deterministic ingress keys do not already prevent.
- [x] Update the crash-repair test that currently monkeypatches
  `save_source_bootstrap_state` so it targets the new atomic projection seam.

### Acceptance

- Concurrent state tests are deterministic over at least 20 iterations.
- Every seen list remains `<=5000` after task-result projection.
- Existing XHS/Douyin/YouTube/Zhihu staged completion tests remain green.

## Task 3: Give Reddit durable-ingress and staged-terminal parity

**Owner:** LunaMax Agent C<br>
**Files:** `src/openbiliclaw/sources/reddit_tasks.py`,
`src/openbiliclaw/sources/task_result_protocol.py`,
`src/openbiliclaw/sources/bootstrap_state.py`, `src/openbiliclaw/api/app.py`;
`tests/test_reddit_tasks.py`, `tests/test_source_task_completion_protocol.py`, focused API
tests.

### Steps

- [x] Add `reddit_bootstrap_item_key()` tests for post, comment, subreddit, user, malformed
  row, stability, and mixed-batch uniqueness.
- [x] Add `reddit_tasks` to the shared staged-terminal allowlist.
- [x] Give `RedditTaskQueue` the same atomic mutation, `stage_final_result`,
  `complete_staged_result`, immutable-terminal `fail`, and stale-row protections as Zhihu.
- [x] Add Reddit to the parameterized first-final-wins and crash-repair suite.
- [x] Rewrite `reddit_task_result` to freeze the first final, read canonical accumulated
  items, and for `bootstrap_events` with `profile_update OR incremental` run:

  `stable filter -> reddit_items_to_events(import_source="reddit_bootstrap_events") -> _accept_source_profile_events(generic_owner=not init_busy) -> strict atomic key mark -> complete_staged_result`.

- [x] Preserve plain guided-init/fetch behavior when neither payload flag is present.
- [x] Test `N` old + `M` new rows, repeated callback, changed retry payload, ingress failure,
  state-checkpoint failure, and terminal-flip failure.

### Acceptance

- Reddit appears in every relevant parameterized source completion case.
- A repeated periodic Reddit result inserts zero additional event facts.
- A failed projection leaves the task staged/nonterminal and a reclaimed retry repairs it
  solely from the first canonical final.

## Task 4: Add config and the scheduler core

**Owner:** LunaMax implementation agent after Wave 1 integration<br>
**Files:** `src/openbiliclaw/config.py`, `src/openbiliclaw/api/models.py`,
`src/openbiliclaw/api/app.py`, add
`src/openbiliclaw/runtime/source_incremental_sync.py`, add
`tests/test_source_incremental_sync.py`, update `tests/test_config.py` and config API tests.

### Steps

- [x] Add the six config fields from Spec I12 with one shared optional-int normalizer.
  Load, save, env/config filtering, typed API output, API update validation, and round-trip
  behavior must agree. Replace the current “other branch unknown key” regression with the
  new known-key contract while retaining a different unknown-key regression.
- [x] Add fake-clock scheduler tests before implementation:
  - due vs not due;
  - global and per-source `0` disable;
  - source-enabled gate;
  - profile-not-ready and init-active gates;
  - extension absent under default `pause_on_extension_disconnect=false` still skips;
  - round-robin fairness;
  - enqueue `None`, reused outcome, and created outcome stamp behavior;
  - one active task across all five sources;
  - terminal reconciliation;
  - crash-window adoption of an unrecorded `incremental=true` DB task;
  - corrupt state/timestamp recovery;
  - in-process kick only for a created task and kick failure fallback.
- [x] Implement an async `SourceIncrementalSync.tick()` that offloads synchronous DB/state
  work and awaits an injected async kick callback.
- [x] Use task tables as active-work truth. Persist cursor, last attempt, and active task in
  the normalized `source_incremental` state block.
- [x] Enqueue only one selected source with `force=true, incremental=true`.

### Acceptance

- No scheduler code imports `openbiliclaw.cli` or calls localhost HTTP.
- Two immediately repeated ticks cannot create two active tasks.
- A real created id is the only path that advances `last_attempt_at`.

## Task 5: Wire runtime ownership, init seeding, and hot reload

**Owner:** same LunaMax implementation agent; separate LunaMax reviewer audits races<br>
**Files:** `src/openbiliclaw/runtime/refresh.py`,
`src/openbiliclaw/api/runtime_context.py`, `src/openbiliclaw/cli.py`,
`config.example.toml`; `tests/test_refresh_runtime.py`, `tests/test_api_app.py`,
`tests/test_cli.py`.

### Steps

- [x] Add `source_incremental_sync` as an optional controller dependency and an independent
  loop. It must not be inside the LLM gate, but must honor scheduler enabled, profile
  readiness, and init-active checks.
- [x] Construct the scheduler in `RuntimeContext` with the current DB, memory manager,
  presence tracker, source-enabled map, scheduler config, and an async `EventHub.publish`
  kick. Do not capture an event loop in a worker-thread closure.
- [x] Include the loop in the existing `run_forever` task group so current cancellation and
  hot-reload ownership remain single-owner.
- [x] After a guided-init collector returns a genuinely successful `ok` or successful-empty
  terminal, atomically seed that platform's `last_attempt_at`. Do not seed failed,
  login-required, timeout, degraded, or skipped results. Add explicit tests for the chosen
  empty-result rule per platform; ambiguous unauthenticated empties must not be labelled
  successful without evidence.
- [x] Confirm guided init sets source enabled before the runtime can schedule, and that
  init-active/profile-ready gates prevent overlap.
- [x] Document the extension-online constraint and interval fields in `config.example.toml`.
- [x] Add hot-reload tests proving a config change replaces interval/source policy and only
  one scheduler loop remains.

### Acceptance

- Runtime starts with no profile and queues nothing.
- A just-successful init waits one full configured interval before its first periodic task.
- An init failure can self-heal on the first eligible connected post-init tick.
- A custom API port still kicks through the in-process event hub.

## Task 6: Independent review and corrective pass

**Owner:** primary agent<br>
**Files:** as findings require.

### Review checklist

- [x] Compare every changed handler against current XHS staged-terminal ordering.
- [x] Audit retry boundaries: canonical stage, ingress, state checkpoint, terminal flip.
- [x] Audit task active-state races and crash windows.
- [x] Audit init ownership, generic owner metadata, and no direct pipeline calls.
- [x] Audit payload parity and Douyin degraded behavior.
- [x] Audit config load/save/API/hot-reload consistency.
- [x] Run `git diff --check`, search for `source_incremental`, `incremental`, direct
  `propagate_event`, localhost `8420`, and unexpected CLI imports.
- [x] Fix every confirmed finding and add a regression test before proceeding.

### Acceptance

- No unresolved high/medium correctness finding.
- Every correction has a focused regression test.

## Task 7: Mandatory documentation sync

**Owner:** primary agent; LunaMax may draft, primary verifies<br>
**Files:** all documents listed in the spec's Documentation obligations.

### Steps

- [x] Update module implemented-feature and public-API sections for runtime, extension,
  config, API, init, and memory.
- [x] Add one concise bullet under the current `docs/changelog.md` version block; do not
  create a release or edit README release highlights.
- [x] Update all four architecture surfaces (`docs/architecture.md`, `docs/spec.md` §3,
  README CN, README EN) with the new extension account-refresh loop and durable ingress.
- [x] Keep README diagrams semantically identical across languages.
- [x] Ensure docs say “extension-online periodic re-pull”, never “background/browser-free
  account sync”.
- [x] Document state keys, cap, intervals, per-source disable, init behavior, and failure
  retry semantics.

### Acceptance

- The CLAUDE.md documentation checklist is checked line by line.
- `rg` finds no stale statement that the five sources are only imported once at init.

## Task 8: Comprehensive verification

**Owner:** primary agent<br>
**Files:** no unreviewed production changes during this task.

### Focused gate

```bash
env -u OPENBILICLAW_PROJECT_ROOT PYTHONPATH=src .venv/bin/pytest \
  tests/test_source_bootstrap.py \
  tests/test_source_incremental_sync.py \
  tests/test_source_task_completion_protocol.py \
  tests/test_reddit_tasks.py tests/test_api_app.py tests/test_cli.py \
  tests/test_config.py tests/test_memory_manager.py tests/test_refresh_runtime.py \
  -q -p no:randomly
```

### Static gate

```bash
.venv/bin/ruff format --check src/ tests/
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/
git diff --check
```

### Full gate

```bash
env -u OPENBILICLAW_PROJECT_ROOT PYTHONPATH=src .venv/bin/pytest tests/ -q -p no:randomly
```

### Manual smoke, only when a logged-in extension is available

- [ ] Force one platform due and verify exactly one `<platform>_task_available` event.
- [ ] Verify a second platform waits until the first task is terminal.
- [ ] Submit one new saved/liked row and verify one durable event plus generic owner.
- [ ] Repeat the same pull and verify zero additional events.
- [ ] Disconnect the extension and verify no task/state advance.

These original cross-platform scheduler bullets were not run as one manual scenario. The
later logged-in XHS smoke recorded above verifies the XHS task/auth/ingestion boundary, but
does not substitute for multi-platform serialization or extension-disconnect checks.

### Final acceptance

- Focused, Ruff, MyPy, full backend suite, and diff checks pass.
- Any skipped/manual-only validation is called out precisely.
- Worktree is clean after intentional commits.
- The legacy branch remains untouched pending explicit archive/delete approval.

## Suggested commit boundaries

1. `docs: re-spec source incremental sync for current runtime`
2. `refactor: extract source bootstrap enqueue core`
3. `fix: make source bootstrap projection state atomic`
4. `feat: add durable reddit bootstrap ingestion`
5. `feat: schedule extension account signal refreshes`
6. `docs: document periodic source account refresh`
7. corrective commits only when a review finding needs isolation.
