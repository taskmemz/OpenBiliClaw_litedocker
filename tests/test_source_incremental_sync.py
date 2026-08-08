"""Regression tests for extension-online periodic bootstrap scheduling."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from openbiliclaw.config import SchedulerConfig
from openbiliclaw.runtime import source_incremental_sync as scheduler_module
from openbiliclaw.runtime.source_incremental_sync import (
    SOURCE_ORDER,
    SourceIncrementalSync,
    SourceIncrementalSyncResult,
)
from openbiliclaw.sources.bootstrap_state import default_source_bootstrap_state
from openbiliclaw.sources.source_bootstrap import BootstrapEnqueueResult


class _FakePresence:
    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.calls: list[int] = []

    def is_present(self, grace_seconds: int) -> bool:
        self.calls.append(grace_seconds)
        return self.present


class _FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class _FakeMemory:
    def __init__(self, state: dict[str, object] | None = None) -> None:
        self.state = deepcopy(state or default_source_bootstrap_state())
        self.update_calls = 0

    def load_source_bootstrap_state(self) -> dict[str, object]:
        return deepcopy(self.state)

    def update_source_bootstrap_state(self, mutator: Any) -> dict[str, object]:
        self.update_calls += 1
        current = deepcopy(self.state)
        result = mutator(current)
        self.state = deepcopy(current if result is None else result)
        return deepcopy(self.state)


class _FakeDatabase:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        for table in (
            "xhs_tasks",
            "dy_tasks",
            "yt_tasks",
            "zhihu_tasks",
            "reddit_tasks",
        ):
            self.conn.execute(
                f"CREATE TABLE {table} ("
                "id TEXT PRIMARY KEY, type TEXT, payload_json TEXT, "
                "status TEXT, created_at TEXT)"
            )
        self.conn.commit()


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: SchedulerConfig | None = None,
    enabled: dict[str, bool] | None = None,
    state: dict[str, object] | None = None,
) -> tuple[SourceIncrementalSync, _FakeDatabase, _FakeMemory, _FakePresence, _FakeClock, list[str]]:
    database = _FakeDatabase()
    memory = _FakeMemory(state)
    presence = _FakePresence()
    clock = _FakeClock()
    kicks: list[str] = []
    enqueue_calls: list[str] = []
    counters = {source: 0 for source in SOURCE_ORDER}
    table_types = {
        "xhs": ("xhs_tasks", "bootstrap_profile"),
        "dy": ("dy_tasks", "bootstrap_profile"),
        "yt": ("yt_tasks", "bootstrap_profile"),
        "zhihu": ("zhihu_tasks", "bootstrap_events"),
        "reddit": ("reddit_tasks", "bootstrap_events"),
    }

    def make_enqueue(source: str) -> Any:
        def enqueue(
            db: _FakeDatabase,
            *,
            force: bool,
            incremental: bool,
        ) -> BootstrapEnqueueResult:
            assert force is True
            assert incremental is True
            enqueue_calls.append(source)
            counters[source] += 1
            task_id = f"{source}-{counters[source]}"
            table, task_type = table_types[source]
            db.conn.execute(
                f"INSERT INTO {table} (id, type, payload_json, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (
                    task_id,
                    task_type,
                    '{"incremental": true}',
                    clock.value.isoformat(),
                ),
            )
            db.conn.commit()
            return BootstrapEnqueueResult(task_id=task_id, created=True, reason="created")

        return enqueue

    specs = {
        source: (table_types[source][0], table_types[source][1], make_enqueue(source))
        for source in SOURCE_ORDER
    }
    monkeypatch.setattr(scheduler_module, "_TASK_SPECS", specs)

    async def kick(source: str) -> None:
        kicks.append(source)

    scheduler = SourceIncrementalSync(
        database=database,
        memory_manager=memory,
        presence=presence,
        source_enabled=enabled or {source: True for source in SOURCE_ORDER},
        scheduler_config=config or SchedulerConfig(),
        profile_ready=lambda: True,
        init_active=lambda: False,
        kick=kick,
        clock=clock,
    )
    scheduler._test_enqueue_calls = enqueue_calls  # type: ignore[attr-defined]
    return scheduler, database, memory, presence, clock, kicks


def _complete(database: _FakeDatabase, source: str, task_id: str) -> None:
    table = {
        "xhs": "xhs_tasks",
        "dy": "dy_tasks",
        "yt": "yt_tasks",
        "zhihu": "zhihu_tasks",
        "reddit": "reddit_tasks",
    }[source]
    database.conn.execute(f"UPDATE {table} SET status = 'completed' WHERE id = ?", (task_id,))
    database.conn.commit()


@pytest.mark.asyncio
async def test_due_tick_creates_one_task_and_not_due_reconciles_after_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, database, memory, _presence, clock, kicks = _harness(monkeypatch)

    first = await scheduler.tick()
    assert (first.reason, first.source, first.created) == ("created", "xhs", True)
    assert kicks == ["xhs"]
    assert memory.state["source_incremental"]["last_attempt_at"]["xhs"]  # type: ignore[index]

    _complete(database, "xhs", "xhs-1")
    scheduler.source_enabled = {source: source == "xhs" for source in SOURCE_ORDER}
    second = await scheduler.tick()
    assert second.reason == "not_due"

    clock.advance(hours=24)
    third = await scheduler.tick()
    assert third.source == "xhs"


@pytest.mark.asyncio
async def test_global_zero_and_per_source_zero_disable_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_off = SchedulerConfig(source_incremental_hours=0)
    scheduler, _db, _memory, _presence, _clock, _kicks = _harness(monkeypatch, config=global_off)
    assert (await scheduler.tick()).reason == "not_due"

    per_source_off = SchedulerConfig(xhs_incremental_hours=0)
    scheduler, _db, _memory, _presence, _clock, _kicks = _harness(
        monkeypatch, config=per_source_off
    )
    result = await scheduler.tick()
    assert result.source == "dy"


@pytest.mark.asyncio
async def test_per_source_override_controls_actual_due_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SchedulerConfig(source_incremental_hours=24, xhs_incremental_hours=2)
    scheduler, database, _memory, _presence, clock, _kicks = _harness(
        monkeypatch,
        config=config,
        enabled={source: source == "xhs" for source in SOURCE_ORDER},
    )

    first = await scheduler.tick()
    _complete(database, "xhs", str(first.task_id))
    clock.advance(hours=1)
    assert (await scheduler.tick()).reason == "not_due"

    clock.advance(hours=1)
    assert (await scheduler.tick()).source == "xhs"


@pytest.mark.asyncio
async def test_enabled_presence_profile_and_init_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    disabled = SchedulerConfig(enabled=False)
    scheduler, _db, _memory, presence, _clock, _kicks = _harness(monkeypatch, config=disabled)
    assert (await scheduler.tick()).reason == "scheduler_disabled"
    assert presence.calls == []

    scheduler, _db, _memory, presence, _clock, _kicks = _harness(
        monkeypatch, enabled={source: False for source in SOURCE_ORDER}
    )
    assert (await scheduler.tick()).reason == "not_due"

    presence.present = False
    scheduler.source_enabled = {source: True for source in SOURCE_ORDER}
    assert (await scheduler.tick()).reason == "extension_absent"
    assert presence.calls[-1] == 90

    presence.present = True
    scheduler.profile_ready = lambda: False
    assert (await scheduler.tick()).reason == "profile_not_ready"

    scheduler.profile_ready = lambda: True
    scheduler.init_active = lambda: True
    assert (await scheduler.tick()).reason == "init_active"


@pytest.mark.asyncio
async def test_round_robin_fairness_uses_persisted_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, database, _memory, _presence, clock, _kicks = _harness(monkeypatch)
    seen: list[str] = []
    for _ in SOURCE_ORDER:
        result = await scheduler.tick()
        assert result.created is True
        seen.append(result.source)
        _complete(database, result.source, str(result.task_id))
        clock.advance(hours=24)
    assert seen == list(SOURCE_ORDER)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created", "task_id", "reason"),
    [
        (False, "reused-id", "reused_recent"),
        (False, None, "enqueue_failed"),
        (True, "", "created"),
    ],
)
async def test_reused_or_none_outcome_does_not_stamp_schedule_state(
    monkeypatch: pytest.MonkeyPatch,
    created: bool,
    task_id: str | None,
    reason: str,
) -> None:
    scheduler, _db, memory, _presence, _clock, kicks = _harness(
        monkeypatch,
        enabled={source: source == "xhs" for source in SOURCE_ORDER},
    )
    original = deepcopy(memory.state)

    def outcome(_db: Any, *, force: bool, incremental: bool) -> BootstrapEnqueueResult:
        assert force and incremental
        return BootstrapEnqueueResult(task_id=task_id, created=created, reason=reason)

    table, task_type, _ = scheduler_module._TASK_SPECS["xhs"]
    monkeypatch.setattr(
        scheduler_module,
        "_TASK_SPECS",
        {**scheduler_module._TASK_SPECS, "xhs": (table, task_type, outcome)},
    )
    result = await scheduler.tick()
    assert result.reason == reason
    assert memory.state == original
    assert kicks == []


@pytest.mark.asyncio
async def test_exhausted_source_budget_does_not_starve_next_due_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, database, memory, _presence, clock, kicks = _harness(monkeypatch)
    table, task_type, _ = scheduler_module._TASK_SPECS["xhs"]

    def exhausted(_db: Any, *, force: bool, incremental: bool) -> BootstrapEnqueueResult:
        assert force and incremental
        return BootstrapEnqueueResult(None, False, "enqueue_failed")

    monkeypatch.setattr(
        scheduler_module,
        "_TASK_SPECS",
        {**scheduler_module._TASK_SPECS, "xhs": (table, task_type, exhausted)},
    )

    result = await scheduler.tick()

    assert (result.reason, result.source, result.created) == ("created", "dy", True)
    assert memory.state["source_incremental"]["last_attempt_at"].keys() == {"dy"}  # type: ignore[index,union-attr]
    assert database.conn.execute("SELECT COUNT(*) FROM xhs_tasks").fetchone()[0] == 0
    assert database.conn.execute("SELECT COUNT(*) FROM dy_tasks").fetchone()[0] == 1
    assert kicks == ["dy"]


@pytest.mark.asyncio
async def test_enqueue_exception_does_not_stamp_schedule_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, _db, memory, _presence, _clock, kicks = _harness(monkeypatch)
    original = deepcopy(memory.state)

    def enqueue(_db: Any, *, force: bool, incremental: bool) -> BootstrapEnqueueResult:
        assert force and incremental
        raise RuntimeError("database unavailable")

    table, task_type, _ = scheduler_module._TASK_SPECS["xhs"]
    monkeypatch.setattr(
        scheduler_module,
        "_TASK_SPECS",
        {**scheduler_module._TASK_SPECS, "xhs": (table, task_type, enqueue)},
    )

    result = await scheduler.tick()

    assert result.reason == "enqueue_error"
    assert memory.state == original
    assert kicks == []


@pytest.mark.asyncio
async def test_one_pending_bootstrap_in_any_source_table_blocks_new_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for source in SOURCE_ORDER:
        scheduler, database, memory, _presence, _clock, kicks = _harness(monkeypatch)
        table, task_type, _ = scheduler_module._TASK_SPECS[source]
        database.conn.execute(
            f"INSERT INTO {table} (id, type, payload_json, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (f"manual-{source}", task_type, "{}", "2026-08-03T00:00:00+00:00"),
        )
        database.conn.commit()
        result = await scheduler.tick()
        assert result.reason == "active_task"
        assert result.source == source
        assert memory.state["source_incremental"]["active_task"] is None  # type: ignore[index]
        assert kicks == []


@pytest.mark.asyncio
async def test_concurrent_ticks_on_one_scheduler_create_only_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, database, _memory, _presence, _clock, _kicks = _harness(monkeypatch)
    results = await asyncio.gather(scheduler.tick(), scheduler.tick())

    assert sum(result.created for result in results) == 1
    assert database.conn.execute("SELECT COUNT(*) FROM xhs_tasks").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_hot_reload_scheduler_instances_share_one_decision_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, database, memory, presence, clock, kicks = _harness(monkeypatch)
    second = SourceIncrementalSync(
        database=database,
        memory_manager=memory,
        presence=presence,
        source_enabled=first.source_enabled,
        scheduler_config=first.scheduler_config,
        profile_ready=lambda: True,
        init_active=lambda: False,
        kick=first.kick,
        clock=clock,
    )
    first_scanned = threading.Event()
    release_first = threading.Event()
    original_reconcile = first._reconcile_active_state

    def pause_after_empty_scan(
        state: dict[str, object], *, now: datetime
    ) -> SourceIncrementalSyncResult | None:
        result = original_reconcile(state, now=now)
        assert result is None
        first_scanned.set()
        assert release_first.wait(timeout=2)
        return result

    monkeypatch.setattr(first, "_reconcile_active_state", pause_after_empty_scan)
    first_tick = asyncio.create_task(first.tick())
    assert await asyncio.to_thread(first_scanned.wait, 2)
    second_tick = asyncio.create_task(second.tick())
    await asyncio.sleep(0.05)
    release_first.set()
    results = await asyncio.gather(first_tick, second_tick)

    assert sum(result.created for result in results) == 1
    assert {result.reason for result in results} == {"created", "active_task"}
    assert database.conn.execute("SELECT COUNT(*) FROM xhs_tasks").fetchone()[0] == 1
    assert kicks == ["xhs"]


@pytest.mark.asyncio
async def test_terminal_active_state_is_cleared_before_next_due_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = default_source_bootstrap_state()
    state["source_incremental"] = {
        "cursor": "",
        "last_attempt_at": {},
        "active_task": {"source": "xhs", "task_id": "done"},
    }
    scheduler, database, memory, _presence, _clock, _kicks = _harness(monkeypatch, state=state)
    database.conn.execute(
        "INSERT INTO xhs_tasks (id, type, payload_json, status, created_at) "
        "VALUES ('done', 'bootstrap_profile', '{}', 'completed', '2026-08-03')"
    )
    database.conn.commit()

    result = await scheduler.tick()
    assert result.created is True
    assert memory.state["source_incremental"]["active_task"]["task_id"] == "xhs-1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_crash_window_adopts_unrecorded_incremental_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, database, memory, _presence, clock, kicks = _harness(monkeypatch)
    database.conn.execute(
        "INSERT INTO xhs_tasks (id, type, payload_json, status, created_at) "
        "VALUES ('crashed', 'bootstrap_profile', "
        "'{\"incremental\": true}', 'in_progress', '2026-08-03')"
    )
    database.conn.commit()

    result = await scheduler.tick()
    assert (result.reason, result.task_id) == ("active_task", "crashed")
    assert memory.state["source_incremental"]["active_task"] == {  # type: ignore[index]
        "source": "xhs",
        "task_id": "crashed",
    }
    assert memory.state["source_incremental"]["cursor"] == "xhs"  # type: ignore[index]
    assert (
        memory.state["source_incremental"]["last_attempt_at"]["xhs"]  # type: ignore[index]
        == "2026-08-03T00:00:00+00:00"
    )
    assert kicks == []

    scheduler.source_enabled = {source: source == "xhs" for source in SOURCE_ORDER}
    _complete(database, "xhs", "crashed")
    assert (await scheduler.tick()).reason == "not_due"

    clock.advance(hours=24)
    assert (await scheduler.tick()).source == "xhs"


@pytest.mark.asyncio
async def test_corrupt_state_and_timestamp_are_safe_and_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "source_incremental": {
            "cursor": "not-a-source",
            "last_attempt_at": {"xhs": "not-a-timestamp"},
            "active_task": "corrupt",
        }
    }
    scheduler, _db, memory, _presence, _clock, _kicks = _harness(monkeypatch, state=state)

    result = await scheduler.tick()
    assert result.created is True
    assert memory.state["source_incremental"]["cursor"] == "xhs"  # type: ignore[index]


@pytest.mark.asyncio
async def test_future_timestamp_is_treated_as_corrupt_and_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = default_source_bootstrap_state()
    state["source_incremental"] = {
        "cursor": "reddit",
        "last_attempt_at": {"xhs": "2099-01-01T00:00:00+00:00"},
        "active_task": None,
    }
    scheduler, _db, _memory, _presence, _clock, _kicks = _harness(
        monkeypatch,
        state=state,
        enabled={source: source == "xhs" for source in SOURCE_ORDER},
    )

    assert (await scheduler.tick()).source == "xhs"


@pytest.mark.asyncio
async def test_naive_recent_timestamp_is_treated_as_corrupt_and_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = default_source_bootstrap_state()
    state["source_incremental"] = {
        "cursor": "reddit",
        "last_attempt_at": {"xhs": "2026-08-03T00:00:00"},
        "active_task": None,
    }
    scheduler, _db, _memory, _presence, _clock, _kicks = _harness(
        monkeypatch,
        state=state,
        enabled={source: source == "xhs" for source in SOURCE_ORDER},
    )

    assert (await scheduler.tick()).source == "xhs"


@pytest.mark.asyncio
async def test_kick_only_runs_for_created_task_and_failure_keeps_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler, _db, memory, _presence, _clock, kicks = _harness(monkeypatch)
    assert (await scheduler.tick()).created is True
    assert kicks == ["xhs"]

    scheduler2, _db2, memory2, _presence2, _clock2, _kicks2 = _harness(monkeypatch)

    async def failing_kick(_source: str) -> None:
        raise RuntimeError("stream offline")

    scheduler2.kick = failing_kick
    result = await scheduler2.tick()
    assert result.reason == "created_kick_failed"
    assert result.created is True
    assert memory2.state["source_incremental"]["active_task"]["task_id"] == "xhs-1"  # type: ignore[index]


@pytest.mark.asyncio
async def test_database_enqueue_runs_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler, _db, _memory, _presence, _clock, _kicks = _harness(monkeypatch)
    event_thread = threading.get_ident()
    worker_threads: list[int] = []
    table, task_type, _ = scheduler_module._TASK_SPECS["xhs"]

    def enqueue(
        database: _FakeDatabase,
        *,
        force: bool,
        incremental: bool,
    ) -> BootstrapEnqueueResult:
        assert force and incremental and table and task_type
        worker_threads.append(threading.get_ident())
        database.conn.execute(
            "INSERT INTO xhs_tasks (id, type, payload_json, status, created_at) "
            "VALUES ('threaded', 'bootstrap_profile', '{\"incremental\": true}', 'pending', 'now')"
        )
        database.conn.commit()
        return BootstrapEnqueueResult("threaded", True, "created")

    monkeypatch.setattr(
        scheduler_module,
        "_TASK_SPECS",
        {**scheduler_module._TASK_SPECS, "xhs": ("xhs_tasks", "bootstrap_profile", enqueue)},
    )
    await scheduler.tick()
    assert worker_threads and worker_threads[0] != event_thread


def test_scheduler_has_no_cli_or_localhost_http_dependency() -> None:
    import inspect

    source = inspect.getsource(scheduler_module)
    assert "openbiliclaw.cli" not in source
    assert "localhost" not in source
    assert "httpx" not in source
