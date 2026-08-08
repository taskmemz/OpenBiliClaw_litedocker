# Source Incremental Sync Relanding Spec — extension-online periodic account refresh

**Created:** 2026-08-03<br>
**Base:** `origin/main` at `15e61bc0` (`v0.3.192`)<br>
**Legacy reference:** local-only branch `source-incremental-sync` at `ce5beb5e` (`v0.3.158`)<br>
**Scope:** periodically re-run the existing browser-extension account bootstrap for 小红书、抖音、YouTube、知乎、Reddit, then admit only new account signals through the current durable event ingress.<br>
**Status:** r1, approved for implementation. The legacy branch is reference material only; no merge or rebase is allowed.

## Goal and honest product boundary

The five sources above currently collect the user's private account signals through the
browser extension:

1. the backend queues a platform bootstrap task;
2. the extension uses the user's same-origin logged-in browser session;
3. the extension returns saved / liked / history / follows / subscriptions;
4. the API converts accepted rows into canonical events.

Today that path is primarily run during guided init or an explicit fetch. The account can
change afterwards while OpenBiliClaw never re-pulls it. This feature reuses the same task
path every 24 hours by default so newly saved, liked, watched, followed, or subscribed
items can update the profile without another full init.

This is **not browser-free synchronization**. It only runs while an OpenBiliClaw extension
runtime-stream client is present and the relevant site remains logged in. Building a
server-side private-account client for any of these sources is explicitly out of scope.

## Measurable outcome

- Each enabled source is independently scheduled using a global 24-hour default and an
  optional per-source override.
- At most one of the five account-bootstrap tasks is active at any time, including tasks
  started manually or by guided init.
- A repeated pull of the same account rows creates zero additional durable events.
- A pull containing `N` old rows and `M` new rows creates exactly `M` newly inserted
  source-import facts; duplicate ingress receipts are still checkpointed in seen state.
- The extension being offline creates zero queued tasks and advances no timestamp.
- A failed enqueue or exhausted task budget advances no timestamp.
- Source seen-key state remains bounded at 5,000 keys per platform and is updated through
  an atomic read-modify-write.

## Current-main diagnosis

### D1. Three handlers already use the durable ingress

`/api/sources/{xhs,dy,yt}/task-result` currently performs:

`canonical staged result -> seen-key filter -> converter -> _accept_source_profile_events -> EventIngressService -> seen-key checkpoint -> terminal flip`.

This is the correct v0.3.192 ownership path. Steady-state events receive
`profile_update_owner="generic"`; guided-init-owned events are durable facts without the
generic owner marker. The relanding must preserve that split and must not resurrect the
legacy branch's direct `memory.propagate_event()` / pipeline calls.

### D2. Zhihu is durable but explicitly gated

Zhihu has the same staged-result and durable-ingress path, but only when the backend-stored
task payload has `profile_update=true`. Periodic tasks therefore need a backend-owned
marker and must route on `profile_update OR incremental`.

### D3. Reddit is the missing parity case

Reddit currently persists task results but has none of the following:

- stable bootstrap item keys for every account scope;
- cross-task seen-key filtering;
- durable event ingress from `task-result`;
- two-phase first-final-wins completion and crash repair.

Reddit cannot join the scheduler until all four are implemented.

### D4. The enqueue implementation is trapped in `cli.py`

The five `_enqueue_*_bootstrap_task` functions own task payloads, six-hour recent-task
dedupe, budgets, environment limits, and the HTTP kick. Only XHS exposes `force`; the
other sources cannot bypass a completed recent task. Runtime code must not import the
Typer/Rich CLI module.

### D5. Source projection state is neither atomic nor bounded

`MemoryManager.save_source_bootstrap_state()` truncates and rewrites JSON directly.
`_mark_source_bootstrap_keys()` performs a separate load/merge/save and grows every source
list forever. Concurrent task-result handlers can lose a platform's keys even though the
event ingress itself remains durable.

### D6. “One enqueue per tick” is not sufficient serialization

The normal refresh cadence is around one minute, while an extension bootstrap can occupy
the foreground browser for several minutes. Merely choosing one platform per tick can
still enqueue platform B while platform A is pending or in progress. The relanding needs
a cross-platform active-task gate, not just round-robin selection.

### D7. Current init selection already has a durable opt-in source

`_persist_init_source_enabled_flags()` writes guided-init choices into
`sources.<platform>.enabled`; every affected source defaults disabled. Therefore the old
branch's new `bootstrap_completed` membership gate is unnecessary on the current base and
has bad upgrade behavior: existing users would never sync until another full init.

The current-base rule is:

- source `enabled` is the user's opt-in;
- profile readiness and init inactivity prevent pre-init work;
- a successful init pull records the platform's latest-attempt timestamp so it is not
  immediately repeated;
- a failed or timed-out init pull leaves no timestamp, allowing a later connected runtime
  to retry and self-heal;
- an upgrading user with no timestamp receives one catch-up pull.

## Required architecture

```text
ContinuousRefreshController._loop_source_incremental_sync
        |
        | profile ready + init inactive + scheduler enabled
        v
SourceIncrementalSync.tick()
        |
        | direct PresenceTracker.is_present(grace)
        | no active bootstrap task across xhs/dy/yt/zhihu/reddit
        | source enabled + interval due + round-robin
        v
sources/source_bootstrap.py
        |
        | enqueue(force=True, incremental=True), database only
        v
platform task table ---- in-process EventHub kick ----> browser extension
        ^                                               |
        |                                               v
        +--------- /api/sources/<platform>/task-result -+
                          |
                          | immutable staged final
                          | stable-key filter
                          v
                    EventIngressService
                          |
                          +--> durable event owner / generic profile-update lane
                          |
                          +--> atomic bounded seen-key checkpoint
                          |
                          +--> terminal task flip
```

## Design invariants

### I1. Reuse the extension account scopes unchanged

The periodic path must use the exact existing bootstrap task types, scopes, limits, and
extension executors. It adds no scraping scope and changes no extension content script.

### I2. Non-blocking runtime

The scheduler only enqueues and returns. It never calls `_collect_*_bootstrap_events`,
which contains blocking polling used only by guided init and CLI fetch commands. Synchronous
SQLite/enqueue work runs in `asyncio.to_thread`; the event-hub kick is awaited on the
serving loop.

### I3. Direct extension-presence gate

Eligibility must call `PresenceTracker.is_present(extension_disconnect_grace_seconds)`
directly. It must not use `background_llm_work_allowed()`: with the default
`pause_on_extension_disconnect=false`, that function intentionally allows background LLM
work while the extension is absent, which is wrong for a browser-required task.

### I4. Current durable event ownership only

All new signals go through `_accept_source_profile_events` and `EventIngressService`.
Steady-state periodic results use the generic owner. No handler may directly invoke the
profile pipeline or perform a second memory write beside durable ingress.

### I5. First final wins; projections repair before terminal completion

Every scheduled source, including Reddit, must follow the current staged-terminal
protocol:

1. atomically freeze the first terminal callback in `result_json`;
2. project its canonical rows into durable ingress and seen state;
3. flip the task to `completed` only after all strict projections succeed;
4. a reclaimed retry repairs exclusively from the frozen result and ignores changed retry
   fields.

### I6. One active browser bootstrap across all five sources

Before creating a periodic task, scan all five task tables for pending/in-progress
bootstrap tasks, regardless of whether they were created by init, a CLI command, or the
scheduler. If any exists, enqueue nothing. Persist the periodic active task for status and
crash recovery, but treat the task table as the authoritative active-work ledger.

All runtime, CLI, and guided-init enqueue helpers perform that five-table scan and the
selected queue insert under one SQLite `BEGIN IMMEDIATE` admission transaction. The
process-local lock remains a fast thread/hot-reload fence, but correctness does not depend
on it: separate `Database` facades or processes still serialize at SQLite.

If a process crashes after the DB enqueue but before JSON state updates, the next tick must
discover the row's `incremental=true` payload, adopt its persisted creation time plus
cursor/active projection into scheduler state, and not create a duplicate or immediately
reschedule it after terminal completion.

### I7. Stamp only a newly created real task

The enqueue core returns a structured outcome containing task id and whether a row was
created. The scheduler advances `last_attempt_at[platform]`, cursor, and active-task state
only when `created=true` and the id is non-empty. Recent-task reuse, budget exhaustion,
database errors, and disabled sources do not advance the schedule.

### I8. Bypass terminal dedupe, never active-work safety

Periodic enqueue escapes the normal six-hour completed/failed recent-task dedupe using
`force=true`. The cross-platform active-task check remains mandatory. CLI/manual behavior,
including XHS `--force` and Douyin's retry of a degraded recent result, remains unchanged.

### I9. Exactly one due source per successful tick

Select one platform using a persisted round-robin cursor over
`xhs -> dy -> yt -> zhihu -> reddit`. A later platform cannot starve behind an always-due
earlier one. The next platform is considered only after the active task becomes terminal.

### I10. Atomic, bounded projection state

`source_bootstrap_state.json` gains an atomic updater backed by
`memory.json_state.update_json_state`. Each source keeps the newest 5,000 stable keys in
insertion/recency order. Re-marking a key moves it to the newest position. The filter read
and durable ingress may race, but deterministic ingress keys prevent duplicate facts; the
atomic checkpoint prevents lost keys.

### I11. Stable identity before URL fallback

Reddit keys cover every bootstrap scope:

- post: `t3_<id>`;
- comment: `t1_<id>`;
- subscribed subreddit: `sr_<lowercase-name>`;
- user/account row, if returned: `usr_<lowercase-name>`.

Empty/unidentifiable rows are not admitted as account signals. Signed or mutable URLs are
not a preferred identity when a platform identifier exists.

### I12. Configuration and source independence

`SchedulerConfig` adds:

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `source_incremental_hours` | int | `24` | Global interval; `0` disables all five |
| `xhs_incremental_hours` | int or null | null | XHS override; `0` disables only XHS |
| `douyin_incremental_hours` | int or null | null | Douyin override |
| `youtube_incremental_hours` | int or null | null | YouTube override |
| `zhihu_incremental_hours` | int or null | null | Zhihu override |
| `reddit_incremental_hours` | int or null | null | Reddit override |

All non-null values are in `0..168`. A null override inherits the global value. The source
must also be enabled. Disabling one source does not affect the other four.

### I13. Init and hot-reload safety

- The loop does not enqueue before a complete profile exists.
- The loop pauses for the entire guided-init active window.
- A successful `ok` or genuine successful-empty init task seeds that platform's
  `last_attempt_at`; failed/login-required/timeout/skipped does not.
- Config hot reload constructs a new scheduler against the new source flags and intervals;
  the old loop is cancelled by the existing runtime task lifecycle.
- Kicks use the current in-process `EventHub`, never a hard-coded localhost port.

## State contract

`memory/source_bootstrap_state.json` remains backward compatible and normalizes missing or
malformed fields:

```json
{
  "xhs_seen_note_keys": [],
  "dy_seen_video_keys": [],
  "yt_seen_item_keys": [],
  "zhihu_seen_item_keys": [],
  "reddit_seen_item_keys": [],
  "last_source_bootstrap_sync_at": "",
  "source_incremental": {
    "cursor": "",
    "last_attempt_at": {},
    "active_task": null
  }
}
```

Unknown fields continue to be handled according to the module's existing compatibility
policy. Timestamps are timezone-aware ISO-8601 UTC strings. Malformed timestamps mean due.

## Failure semantics

- **Extension absent:** skip silently at debug level; no task, no state advance.
- **Another bootstrap active:** skip; keep the active row authoritative.
- **Budget exhausted / DB enqueue error:** no timestamp; retry on the next scheduler tick.
- **Kick failure:** keep the task and timestamp; the extension's existing alarm poll is the
  fallback.
- **Task failure after enqueue:** terminal failure clears active state on the next tick;
  the platform becomes due after its configured interval from the real enqueue attempt.
- **Ingress or seen-state failure:** HTTP returns non-2xx, task remains staged/nonterminal,
  and stale-lease retry repairs from the frozen canonical result.
- **State file missing/corrupt:** normalize to defaults; durable event-ingress identity
  still prevents duplicate facts during catch-up.

## Test gates

### Feature tests

- enqueue parity for all five sources, including exact non-incremental payloads, force,
  incremental marker, budgets, recent reuse, and Douyin degraded retry;
- source-bootstrap module imports without `cli`, Typer, Click, or Rich;
- atomic concurrent state updates preserve both writers; seen-key cap and recency behavior;
- scheduler due/not-due, source enabled, override/global disable, direct presence gate,
  profile/init gate, round-robin, truthy-created stamp, active-task serialization, and
  crash-window adoption;
- XHS/Douyin/YouTube/Zhihu current staged-ingress contract remains green;
- Reddit gains stable keys, staged first-final-wins, retry repair at every projection
  boundary, and repeat-cycle zero-event behavior;
- hot reload replaces the scheduler and leaves one loop owner.

### Repository gates

```bash
env -u OPENBILICLAW_PROJECT_ROOT PYTHONPATH=src .venv/bin/pytest \
  tests/test_source_bootstrap.py \
  tests/test_source_incremental_sync.py \
  tests/test_source_task_completion_protocol.py \
  tests/test_api_app.py tests/test_cli.py tests/test_config.py \
  tests/test_memory_manager.py tests/test_refresh_runtime.py -q -p no:randomly
env -u OPENBILICLAW_PROJECT_ROOT PYTHONPATH=src .venv/bin/pytest tests/ -q -p no:randomly
.venv/bin/ruff format --check src/ tests/
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/
git diff --check
```

No real external-account fetch is required in automated CI. A manual extension-online
smoke is documented but must not be faked as an automated pass.

## Documentation obligations

Because this adds a runtime module, changes cross-module data flow, config, init behavior,
API task-result semantics, and memory state, implementation must update:

- `docs/modules/runtime.md`
- `docs/modules/extension.md`
- `docs/modules/config.md`
- `docs/modules/api.md`
- `docs/modules/init.md`
- `docs/modules/memory.md`
- `docs/changelog.md`
- `docs/architecture.md`
- `docs/spec.md` section 3 diagram
- `README.md` and `README_EN.md` architecture diagrams
- `config.example.toml`

CLI public behavior is unchanged; update `docs/modules/cli.md` only if implementation
changes a documented command, option, or output contract.

## Explicitly out of scope

- server-side private-account pulls for these five platforms;
- Bilibili and X account sync (already owned by `AccountSyncService`);
- discovery search/trending/feed behavior and budgets;
- new extension scraping scopes or UI;
- release/version bump, push, PR, or deletion of the legacy local branch.
