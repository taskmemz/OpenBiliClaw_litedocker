"""Tests for the database-only extension bootstrap enqueue core."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest


class _FakeDatabase:
    conn = object()


def _queue_class(
    *,
    recent: dict[str, Any] | None = None,
    enqueue_id: str | None = "fresh-task-id",
    captured: dict[str, Any],
) -> type:
    class FakeQueue:
        def __init__(self, _database: object) -> None:
            pass

        def find_recent_task(
            self,
            task_type: str,
            *,
            recent_hours: float,
            statuses: tuple[str, ...] | None = None,
        ) -> dict[str, Any] | None:
            captured["recent_call"] = (task_type, recent_hours, statuses)
            return recent

        def enqueue_with_id(
            self,
            task_type: str,
            payload: dict[str, Any],
            *,
            daily_budget: int,
        ) -> str | None:
            captured["task_type"] = task_type
            captured["payload"] = payload
            captured["daily_budget"] = daily_budget
            return enqueue_id

    return FakeQueue


_PLATFORMS = (
    (
        "enqueue_xhs_bootstrap",
        "openbiliclaw.sources.xhs_tasks.XhsTaskQueue",
        "bootstrap_profile",
        {
            "scopes": ["saved", "liked", "xhs_history"],
            "max_items_per_scope": 300,
            "max_scroll_rounds": 15,
        },
    ),
    (
        "enqueue_dy_bootstrap",
        "openbiliclaw.sources.dy_tasks.DyTaskQueue",
        "bootstrap_profile",
        {
            "scopes": ["dy_post", "dy_collect", "dy_like", "dy_follow"],
            "max_items_per_scope": 300,
            "max_scroll_rounds": 15,
        },
    ),
    (
        "enqueue_yt_bootstrap",
        "openbiliclaw.sources.yt_tasks.YtTaskQueue",
        "bootstrap_profile",
        {
            "scopes": ["yt_history", "yt_subscriptions", "yt_likes"],
            "max_items_per_scope": 300,
            "max_scroll_rounds": 10,
        },
    ),
    (
        "enqueue_zhihu_bootstrap",
        "openbiliclaw.sources.zhihu_tasks.ZhihuTaskQueue",
        "bootstrap_events",
        {
            "scopes": ["zhihu_read_history", "zhihu_collection", "zhihu_activity"],
            "profile_slug": "",
            "max_items_per_scope": 300,
            "max_collections": 20,
            "profile_update": False,
        },
    ),
    (
        "enqueue_reddit_bootstrap",
        "openbiliclaw.sources.reddit_tasks.RedditTaskQueue",
        "bootstrap_events",
        {
            "scopes": ["reddit_saved", "reddit_upvoted", "reddit_subscribed"],
            "max_items_per_scope": 300,
            "profile_update": False,
        },
    ),
)


@pytest.mark.parametrize(
    ("helper_name", "queue_path", "task_type", "expected_payload"),
    _PLATFORMS,
)
def test_non_incremental_payloads_and_budgets_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    queue_path: str,
    task_type: str,
    expected_payload: dict[str, Any],
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        queue_path,
        _queue_class(captured=captured),
    )

    helper = getattr(source_bootstrap, helper_name)
    result = helper(_FakeDatabase(), force=True)

    assert result.task_id == "fresh-task-id"
    assert result.created is True
    assert result.reason == "created"
    assert captured["task_type"] == task_type
    assert captured["payload"] == expected_payload
    assert captured["daily_budget"] == 10


@pytest.mark.parametrize(
    ("helper_name", "queue_path", "_task_type", "_expected_payload"),
    _PLATFORMS,
)
def test_force_false_reuses_recent_task(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    queue_path: str,
    _task_type: str,
    _expected_payload: dict[str, Any],
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        queue_path,
        _queue_class(
            recent={"id": "recent-task-id", "status": "completed"},
            captured=captured,
        ),
    )

    result = getattr(source_bootstrap, helper_name)(_FakeDatabase())

    assert result == source_bootstrap.BootstrapEnqueueResult(
        task_id="recent-task-id",
        created=False,
        reason="reused_recent",
    )
    assert "task_type" not in captured


@pytest.mark.parametrize(
    ("helper_name", "queue_path", "_task_type", "_expected_payload"),
    _PLATFORMS,
)
def test_force_true_bypasses_recent_task(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    queue_path: str,
    _task_type: str,
    _expected_payload: dict[str, Any],
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}

    class ForceQueue:
        def __init__(self, _database: object) -> None:
            pass

        def find_recent_task(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
            raise AssertionError("force=True must not inspect recent tasks")

        def enqueue_with_id(
            self,
            task_type: str,
            payload: dict[str, Any],
            *,
            daily_budget: int,
        ) -> str:
            captured.update(
                task_type=task_type,
                payload=payload,
                daily_budget=daily_budget,
            )
            return "forced-task-id"

    monkeypatch.setattr(queue_path, ForceQueue)

    result = getattr(source_bootstrap, helper_name)(_FakeDatabase(), force=True)

    assert result == source_bootstrap.BootstrapEnqueueResult(
        task_id="forced-task-id",
        created=True,
        reason="created",
    )
    assert "task_type" in captured


@pytest.mark.parametrize(
    ("helper_name", "queue_path", "_task_type", "_expected_payload"),
    _PLATFORMS,
)
def test_incremental_marker_is_opt_in_and_preserves_profile_update_fields(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    queue_path: str,
    _task_type: str,
    _expected_payload: dict[str, Any],
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}
    monkeypatch.setattr(queue_path, _queue_class(captured=captured))

    result = getattr(source_bootstrap, helper_name)(
        _FakeDatabase(),
        force=True,
        incremental=True,
    )

    assert result.created is True
    assert captured["payload"]["incremental"] is True
    if helper_name in {"enqueue_zhihu_bootstrap", "enqueue_reddit_bootstrap"}:
        assert captured["payload"]["profile_update"] is False

    captured.clear()
    monkeypatch.setattr(queue_path, _queue_class(captured=captured))
    getattr(source_bootstrap, helper_name)(_FakeDatabase(), force=True, incremental=False)
    assert "incremental" not in captured["payload"]


def test_douyin_degraded_recent_task_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}
    messages: list[str] = []
    monkeypatch.setattr(
        "openbiliclaw.sources.dy_tasks.DyTaskQueue",
        _queue_class(
            recent={
                "id": "degraded-task-id",
                "status": "completed",
                "result_json": json.dumps({"status": "degraded"}),
            },
            captured=captured,
        ),
    )

    result = source_bootstrap.enqueue_dy_bootstrap(_FakeDatabase(), notify=messages.append)

    assert result.created is True
    assert result.task_id == "fresh-task-id"
    assert "task_type" in captured
    assert any("仅部分完成" in message for message in messages)


@pytest.mark.parametrize(
    ("helper_name", "queue_path", "_task_type", "_expected_payload"),
    _PLATFORMS,
)
def test_budget_exhaustion_is_a_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    queue_path: str,
    _task_type: str,
    _expected_payload: dict[str, Any],
) -> None:
    from openbiliclaw.sources import source_bootstrap

    captured: dict[str, Any] = {}
    messages: list[str] = []
    monkeypatch.setattr(
        queue_path,
        _queue_class(enqueue_id=None, captured=captured),
    )

    result = getattr(source_bootstrap, helper_name)(
        _FakeDatabase(),
        force=True,
        notify=messages.append,
    )

    assert result == source_bootstrap.BootstrapEnqueueResult(
        task_id=None,
        created=False,
        reason="enqueue_failed",
    )
    assert messages
    assert "今日任务预算已用完" in messages[-1]


def test_force_does_not_bypass_cross_source_active_work(tmp_path: Path) -> None:
    from openbiliclaw.sources import source_bootstrap
    from openbiliclaw.storage.database import Database

    database = Database(tmp_path / "active-bootstrap.db")
    database.initialize()

    first = source_bootstrap.enqueue_dy_bootstrap(database, force=True)
    blocked = source_bootstrap.enqueue_xhs_bootstrap(database, force=True)

    assert first.created is True
    assert blocked == source_bootstrap.BootstrapEnqueueResult(
        task_id=None,
        created=False,
        reason="active_task",
    )
    assert database.conn.execute("SELECT COUNT(*) FROM dy_tasks").fetchone()[0] == 1
    assert database.conn.execute("SELECT COUNT(*) FROM xhs_tasks").fetchone()[0] == 0


def test_concurrent_manual_bootstrap_helpers_create_one_global_active_task(
    tmp_path: Path,
) -> None:
    from openbiliclaw.sources import source_bootstrap
    from openbiliclaw.storage.database import Database

    database = Database(tmp_path / "concurrent-bootstrap.db")
    database.initialize()
    barrier = Barrier(2)

    def enqueue(source: str) -> source_bootstrap.BootstrapEnqueueResult:
        barrier.wait()
        helper = (
            source_bootstrap.enqueue_xhs_bootstrap
            if source == "xhs"
            else source_bootstrap.enqueue_dy_bootstrap
        )
        return helper(database, force=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, ("xhs", "dy")))

    assert sum(result.created for result in results) == 1
    assert {result.reason for result in results} == {"created", "active_task"}
    active_rows = 0
    for table in ("xhs_tasks", "dy_tasks"):
        active_rows += database.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending', 'in_progress')"
        ).fetchone()[0]
    assert active_rows == 1


def test_sqlite_admission_serializes_separate_database_facades_without_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.sources import source_bootstrap
    from openbiliclaw.sources.dy_tasks import DyTaskQueue
    from openbiliclaw.sources.xhs_tasks import XhsTaskQueue
    from openbiliclaw.storage.database import Database

    class NoopDecisionLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> None:
            return None

    path = tmp_path / "cross-facade-bootstrap.db"
    first_database = Database(path)
    first_database.initialize()
    second_database = Database(path)
    second_database.initialize()
    # Initialize both source tables before the race so schema DDL is not the
    # mechanism serializing the two admissions.
    XhsTaskQueue(first_database)
    DyTaskQueue(first_database)
    monkeypatch.setattr(
        source_bootstrap,
        "SOURCE_BOOTSTRAP_DECISION_LOCK",
        NoopDecisionLock(),
    )
    barrier = Barrier(2)

    def enqueue(source: str) -> source_bootstrap.BootstrapEnqueueResult:
        barrier.wait()
        if source == "xhs":
            return source_bootstrap.enqueue_xhs_bootstrap(first_database, force=True)
        return source_bootstrap.enqueue_dy_bootstrap(second_database, force=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, ("xhs", "dy")))

    assert sum(result.created for result in results) == 1
    assert {result.reason for result in results} == {"created", "active_task"}
    active_rows = sum(
        first_database.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending', 'in_progress')"
        ).fetchone()[0]
        for table in ("xhs_tasks", "dy_tasks")
    )
    assert active_rows == 1


def test_source_bootstrap_import_does_not_load_cli_or_ui_dependencies() -> None:
    code = (
        "import sys\n"
        "import openbiliclaw.sources.source_bootstrap\n"
        "for name in ('openbiliclaw.cli', 'typer', 'click', 'rich'):\n"
        "    assert name not in sys.modules, name\n"
        "print('ok')\n"
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": "src",
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        cwd=_repo_root(),
        env=environment,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "ok"


def test_cli_wrapper_maps_created_result_and_respects_deferred_kick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw import cli
    from openbiliclaw.sources import source_bootstrap

    kicked: list[str] = []
    calls: dict[str, Any] = {}

    def fake_enqueue(
        database: object,
        *,
        force: bool,
        incremental: bool,
        notify: Any,
    ) -> source_bootstrap.BootstrapEnqueueResult:
        calls.update(
            database=database,
            force=force,
            incremental=incremental,
            notify=notify,
        )
        return source_bootstrap.BootstrapEnqueueResult("task-id", True, "created")

    monkeypatch.setattr(cli, "_get_runtime_database", lambda: _FakeDatabase())
    monkeypatch.setattr(source_bootstrap, "enqueue_xhs_bootstrap", fake_enqueue)
    monkeypatch.setattr(cli, "_kick_task_dispatcher", kicked.append)

    assert cli._enqueue_xhs_bootstrap_task(force=True, incremental=True, kick=False) == "task-id"
    assert calls["database"].__class__ is _FakeDatabase
    assert calls["force"] is True
    assert calls["incremental"] is True
    assert kicked == []

    assert cli._enqueue_xhs_bootstrap_task(force=True, incremental=True, kick=True) == "task-id"
    assert kicked == ["xhs"]


def _repo_root() -> str:
    return str(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize("source", ("xhs", "dy", "yt", "zhihu"))
def test_guided_init_seeds_only_ok_for_ambiguous_empty_sources(source: str) -> None:
    from openbiliclaw.sources.source_bootstrap import seed_guided_init_attempts

    memory = _SeedMemory()
    statuses = {key: "skipped" for key in ("xhs", "dy", "yt", "zhihu", "reddit")}
    statuses[source] = "empty"
    assert seed_guided_init_attempts(memory, statuses) == ()
    assert memory.update_calls == 0

    statuses[source] = "ok"
    assert seed_guided_init_attempts(memory, statuses) == (source,)
    assert source in memory.state["source_incremental"]["last_attempt_at"]


@pytest.mark.parametrize("status", ("ok", "empty"))
def test_reddit_empty_is_seeded_because_login_is_resolved_before_bootstrap(status: str) -> None:
    from openbiliclaw.sources.source_bootstrap import seed_guided_init_attempts

    # Reddit's bootstrap resolves /api/me first and maps an unauthenticated
    # response to login_required, so an empty authenticated result is real
    # evidence of a completed pull. Other sources do not make that claim.
    memory = _SeedMemory()
    statuses = {key: "skipped" for key in ("xhs", "dy", "yt", "zhihu", "reddit")}
    statuses["reddit"] = status

    assert seed_guided_init_attempts(memory, statuses) == ("reddit",)
    assert memory.update_calls == 1


def test_guided_init_seeds_all_eligible_sources_with_one_atomic_update() -> None:
    from openbiliclaw.sources.source_bootstrap import seed_guided_init_attempts

    memory = _SeedMemory()
    statuses = {key: "ok" for key in ("xhs", "dy", "yt", "zhihu", "reddit")}
    statuses["reddit"] = "empty"

    assert seed_guided_init_attempts(memory, statuses) == (
        "xhs",
        "dy",
        "yt",
        "zhihu",
        "reddit",
    )
    assert memory.update_calls == 1
    assert set(memory.state["source_incremental"]["last_attempt_at"]) == set(statuses)  # type: ignore[index]


@pytest.mark.parametrize("source", ("xhs", "dy", "yt", "zhihu", "reddit"))
@pytest.mark.parametrize("status", ("degraded", "failed", "login_required", "timeout", "skipped"))
def test_guided_init_does_not_seed_non_success_statuses(source: str, status: str) -> None:
    from openbiliclaw.sources.source_bootstrap import seed_guided_init_attempts

    memory = _SeedMemory()
    statuses = {key: "skipped" for key in ("xhs", "dy", "yt", "zhihu", "reddit")}
    statuses[source] = status
    assert seed_guided_init_attempts(memory, statuses) == ()
    assert memory.update_calls == 0


class _SeedMemory:
    def __init__(self) -> None:
        from openbiliclaw.sources.bootstrap_state import default_source_bootstrap_state

        self.state = default_source_bootstrap_state()
        self.update_calls = 0

    def update_source_bootstrap_state(self, mutator: Any) -> dict[str, object]:
        self.update_calls += 1
        result = mutator(self.state)
        if result is not None:
            self.state = result
        return self.state
