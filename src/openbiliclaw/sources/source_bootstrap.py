"""Database-only enqueue helpers for browser-extension bootstrap tasks.

The CLI historically owned the five bootstrap enqueue paths.  Keeping that
logic in this module lets runtime code enqueue an already-resolved database
without importing the Typer/Rich CLI surface.  The helpers deliberately stop
at the database boundary: dispatch kicks and user-facing rendering belong to
their caller.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Any, ParamSpec

INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE = 300
DEFAULT_XHS_BOOTSTRAP_DEDUPE_HOURS = 6.0
DEFAULT_DY_BOOTSTRAP_DEDUPE_HOURS = 6.0
DEFAULT_YT_BOOTSTRAP_DEDUPE_HOURS = 6.0
DEFAULT_ZHIHU_BOOTSTRAP_DEDUPE_HOURS = 6.0
DEFAULT_REDDIT_BOOTSTRAP_DEDUPE_HOURS = 6.0

_RECENT_TASK_STATUSES = ("pending", "in_progress", "completed", "failed")
Notify = Callable[[str], None]
_P = ParamSpec("_P")

# All in-process bootstrap producers (runtime, guided init, and CLI fetch)
# share this re-entrant fast-path lock. The SQLite admission transaction below
# is the cross-facade/process authority: it holds one write reservation across
# the five-table active scan and selected insert. The lock additionally closes
# cancelled-worker/hot-reload thread overlap without coupling runtime to CLI.
SOURCE_BOOTSTRAP_DECISION_LOCK = threading.RLock()
_BOOTSTRAP_TASK_TABLES: tuple[tuple[str, str, str], ...] = (
    ("xhs", "xhs_tasks", "bootstrap_profile"),
    ("dy", "dy_tasks", "bootstrap_profile"),
    ("yt", "yt_tasks", "bootstrap_profile"),
    ("zhihu", "zhihu_tasks", "bootstrap_events"),
    ("reddit", "reddit_tasks", "bootstrap_events"),
)


@contextmanager
def _bootstrap_admission_transaction(database: Any) -> Iterator[None]:
    """Serialize the five-table active scan and insert in SQLite.

    The process-local decision lock keeps ordinary callers cheap, while
    ``BEGIN IMMEDIATE`` is the authority across separate ``Database`` facades
    and processes. Queue ``enqueue_with_id`` methods detect this surrounding
    transaction and deliberately leave its commit to this context manager.
    Narrow unit-test doubles without a SQLite connection keep the historical
    non-transactional seam.
    """

    conn = getattr(database, "conn", None)
    if not isinstance(conn, sqlite3.Connection):
        yield
        return

    execute = conn.execute
    nested = bool(conn.in_transaction)
    savepoint = "source_bootstrap_admission"
    if nested:
        execute(f"SAVEPOINT {savepoint}")
    else:
        execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if nested:
            execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.rollback()
        raise
    else:
        if nested:
            execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()


@dataclass(frozen=True)
class BootstrapEnqueueResult:
    """Outcome of one bootstrap enqueue attempt.

    ``created`` is true only for a newly inserted row with a non-empty task
    id.  A task returned by the recent-task dedupe path has an id but
    ``created`` is false, which is important to periodic scheduling: reuse
    must not advance its attempt timestamp or cursor.
    """

    task_id: str | None
    created: bool
    reason: str


def _serialized_enqueue(
    function: Callable[_P, BootstrapEnqueueResult],
) -> Callable[_P, BootstrapEnqueueResult]:
    """Serialize every public bootstrap helper against scheduler decisions."""

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> BootstrapEnqueueResult:
        with SOURCE_BOOTSTRAP_DECISION_LOCK:
            return function(*args, **kwargs)

    return wrapped


def _find_active_bootstrap_task(database: Any) -> tuple[str, str] | None:
    """Return one pending/in-progress account bootstrap across all five sources."""

    conn = getattr(database, "conn", None)
    execute = getattr(conn, "execute", None)
    if not callable(execute):
        # Narrow unit-test doubles do not expose SQLite. Production Database
        # always does; keep the helpers independently testable at this seam.
        return None
    for source, table, task_type in _BOOTSTRAP_TASK_TABLES:
        try:
            row = execute(
                f"SELECT id FROM {table} "
                "WHERE type = ? AND status IN ('pending', 'in_progress') "
                "ORDER BY created_at ASC LIMIT 1",
                (task_type,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                continue
            raise
        if row is None:
            continue
        try:
            task_id = str(row["id"] or "").strip()
        except (IndexError, KeyError, TypeError):
            task_id = str(row[0] or "").strip()
        if task_id:
            return source, task_id
    return None


def _active_bootstrap_result(
    database: Any,
    *,
    notify: Notify | None,
) -> BootstrapEnqueueResult | None:
    active = _find_active_bootstrap_task(database)
    if active is None:
        return None
    source, _task_id = active
    _notify(
        notify,
        f"  [dim]已有 {source} bootstrap 任务执行中；本次不创建并行账号任务。[/dim]",
    )
    # Do not return the other source's id: CLI wrappers would otherwise kick
    # the wrong dispatcher. The durable row is already recoverable by its own
    # polling/kick path.
    return BootstrapEnqueueResult(task_id=None, created=False, reason="active_task")


def _notify(notify: Notify | None, message: str) -> None:
    if notify is not None:
        notify(message)


def _dedupe_hours(env_var: str, default: float) -> float:
    raw = os.environ.get(env_var, str(default))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _recent_reuse_result(
    recent: dict[str, Any], *, message: str, notify: Notify | None
) -> BootstrapEnqueueResult | None:
    task_id = str(recent.get("id", "")).strip()
    if not task_id:
        return None
    _notify(notify, message.format(status=str(recent.get("status", "unknown"))))
    return BootstrapEnqueueResult(task_id=task_id, created=False, reason="reused_recent")


def _created_or_budget_result(
    task_id: object,
    *,
    budget_message: str,
    notify: Notify | None,
) -> BootstrapEnqueueResult:
    normalized = str(task_id).strip() if task_id is not None else ""
    if not normalized:
        _notify(notify, budget_message)
        return BootstrapEnqueueResult(
            task_id=None,
            created=False,
            reason="enqueue_failed",
        )
    return BootstrapEnqueueResult(task_id=normalized, created=True, reason="created")


def _incremental_payload(payload: dict[str, Any], incremental: bool) -> dict[str, Any]:
    if incremental:
        payload["incremental"] = True
    return payload


def seed_guided_init_attempts(
    memory_manager: Any,
    statuses: dict[str, object],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Atomically seed scheduler timestamps for successful init collectors.

    XHS, Douyin, YouTube, and Zhihu have ambiguous ``empty`` results: the
    extension can complete the task while the site is logged out or blocked,
    so only ``ok`` is evidence that a usable bootstrap pull completed. Reddit
    is deliberately different: its bootstrap first positively resolves
    ``/api/me`` and maps an unauthenticated response to ``login_required``;
    therefore ``empty`` is a genuine successful-empty pull for Reddit.
    """
    eligible: list[str] = []
    for source, status in (
        ("xhs", statuses.get("xhs")),
        ("dy", statuses.get("dy")),
        ("yt", statuses.get("yt")),
        ("zhihu", statuses.get("zhihu")),
    ):
        if str(status or "").strip().lower() == "ok":
            eligible.append(source)
    if str(statuses.get("reddit") or "").strip().lower() in {"ok", "empty"}:
        # Reddit's ``empty`` is evidence-backed, unlike the four browser pages
        # above; keep this distinction explicit so future status refactors do
        # not turn a logged-out result into a successful schedule stamp.
        eligible.append("reddit")
    if not eligible:
        return ()

    current = now or datetime.now(UTC)
    timestamp = (
        current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    ).isoformat()
    update_state = getattr(memory_manager, "update_source_bootstrap_state", None)
    if not callable(update_state):
        raise RuntimeError("memory manager lacks atomic source bootstrap state updates")

    def _mutate(state: dict[str, object]) -> dict[str, object]:
        raw_incremental = state.get("source_incremental")
        incremental = dict(raw_incremental) if isinstance(raw_incremental, dict) else {}
        raw_attempts = incremental.get("last_attempt_at")
        attempts = dict(raw_attempts) if isinstance(raw_attempts, dict) else {}
        for source in eligible:
            attempts[source] = timestamp
        incremental["last_attempt_at"] = attempts
        state["source_incremental"] = incremental
        return state

    update_state(_mutate)
    return tuple(eligible)


@_serialized_enqueue
def enqueue_xhs_bootstrap(
    database: Any,
    *,
    force: bool = False,
    incremental: bool = False,
    notify: Notify | None = None,
) -> BootstrapEnqueueResult:
    """Enqueue the XHS ``bootstrap_profile`` task without dispatching it."""
    from openbiliclaw.sources.xhs_tasks import XhsTaskQueue

    scroll_rounds = int(os.environ.get("OPENBILICLAW_XHS_BOOTSTRAP_SCROLL_ROUNDS", "15"))
    max_items = int(
        os.environ.get(
            "OPENBILICLAW_XHS_BOOTSTRAP_MAX_ITEMS",
            str(INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE),
        )
    )

    try:
        queue = XhsTaskQueue(database)
        with _bootstrap_admission_transaction(database):
            dedupe_hours = _dedupe_hours(
                "OPENBILICLAW_XHS_BOOTSTRAP_DEDUPE_HOURS",
                DEFAULT_XHS_BOOTSTRAP_DEDUPE_HOURS,
            )
            find_recent = getattr(queue, "find_recent_task", None)
            if not force and dedupe_hours > 0 and callable(find_recent):
                recent = find_recent(
                    "bootstrap_profile",
                    recent_hours=dedupe_hours,
                    statuses=_RECENT_TASK_STATUSES,
                )
                if recent is not None:
                    reused = _recent_reuse_result(
                        recent,
                        message=(
                            "  [dim]复用最近的小红书 bootstrap 任务"
                            "({status})；需要重新拉取可用 "
                            "`openbiliclaw fetch-xhs --force`。[/dim]"
                        ),
                        notify=notify,
                    )
                    if reused is not None:
                        return reused

            active = _active_bootstrap_result(database, notify=notify)
            if active is not None:
                return active

            payload = _incremental_payload(
                {
                    "scopes": ["saved", "liked", "xhs_history"],
                    "max_items_per_scope": max(1, max_items),
                    "max_scroll_rounds": max(0, scroll_rounds),
                },
                incremental,
            )
            task_id = queue.enqueue_with_id("bootstrap_profile", payload, daily_budget=10)
    except Exception as exc:
        _notify(notify, f"  [yellow]小红书初始化信号未导入: {exc}[/yellow]")
        return BootstrapEnqueueResult(task_id=None, created=False, reason="enqueue_error")

    return _created_or_budget_result(
        task_id,
        budget_message="  [yellow]小红书初始化信号未导入: 今日任务预算已用完。[/yellow]",
        notify=notify,
    )


@_serialized_enqueue
def enqueue_dy_bootstrap(
    database: Any,
    *,
    force: bool = False,
    incremental: bool = False,
    notify: Notify | None = None,
) -> BootstrapEnqueueResult:
    """Enqueue the Douyin ``bootstrap_profile`` task without dispatching it."""
    from openbiliclaw.sources.dy_tasks import DyTaskQueue

    scroll_rounds = int(os.environ.get("OPENBILICLAW_DY_BOOTSTRAP_SCROLL_ROUNDS", "15"))
    max_items = int(
        os.environ.get(
            "OPENBILICLAW_DY_BOOTSTRAP_MAX_ITEMS",
            str(INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE),
        )
    )

    try:
        queue = DyTaskQueue(database)
        with _bootstrap_admission_transaction(database):
            dedupe_hours = _dedupe_hours(
                "OPENBILICLAW_DY_BOOTSTRAP_DEDUPE_HOURS",
                DEFAULT_DY_BOOTSTRAP_DEDUPE_HOURS,
            )
            find_recent = getattr(queue, "find_recent_task", None)
            if not force and dedupe_hours > 0 and callable(find_recent):
                recent = find_recent(
                    "bootstrap_profile",
                    recent_hours=dedupe_hours,
                    statuses=_RECENT_TASK_STATUSES,
                )
                if recent is not None:
                    raw_result = recent.get("result_json")
                    if isinstance(raw_result, dict):
                        parsed_result = raw_result
                    elif isinstance(raw_result, (str, bytes, bytearray)):
                        try:
                            parsed_result = json.loads(raw_result)
                        except (TypeError, ValueError):
                            parsed_result = None
                    else:
                        parsed_result = None
                    recent_is_degraded = (
                        isinstance(parsed_result, dict)
                        and str(parsed_result.get("status", "")).strip().lower() == "degraded"
                    )
                    if recent_is_degraded:
                        _notify(
                            notify,
                            "  [dim]最近的抖音 bootstrap 任务仅部分完成；"
                            "本次重新入队以补齐分页。[/dim]",
                        )
                    else:
                        reused = _recent_reuse_result(
                            recent,
                            message=(
                                "  [dim]复用最近的抖音 bootstrap 任务"
                                "({status})；需要重新拉取可设 "
                                "OPENBILICLAW_DY_BOOTSTRAP_DEDUPE_HOURS=0。[/dim]"
                            ),
                            notify=notify,
                        )
                        if reused is not None:
                            return reused

            active = _active_bootstrap_result(database, notify=notify)
            if active is not None:
                return active

            payload = _incremental_payload(
                {
                    "scopes": ["dy_post", "dy_collect", "dy_like", "dy_follow"],
                    "max_items_per_scope": max(1, max_items),
                    "max_scroll_rounds": max(0, scroll_rounds),
                },
                incremental,
            )
            task_id = queue.enqueue_with_id("bootstrap_profile", payload, daily_budget=10)
    except Exception as exc:
        _notify(notify, f"  [yellow]抖音初始化信号未导入: {exc}[/yellow]")
        return BootstrapEnqueueResult(task_id=None, created=False, reason="enqueue_error")

    return _created_or_budget_result(
        task_id,
        budget_message="  [yellow]抖音初始化信号未导入: 今日任务预算已用完。[/yellow]",
        notify=notify,
    )


@_serialized_enqueue
def enqueue_yt_bootstrap(
    database: Any,
    *,
    force: bool = False,
    incremental: bool = False,
    notify: Notify | None = None,
) -> BootstrapEnqueueResult:
    """Enqueue the YouTube ``bootstrap_profile`` task without dispatching it."""
    from openbiliclaw.sources.yt_tasks import YtTaskQueue

    scroll_rounds = int(os.environ.get("OPENBILICLAW_YT_BOOTSTRAP_SCROLL_ROUNDS", "10"))
    max_items = int(
        os.environ.get(
            "OPENBILICLAW_YT_BOOTSTRAP_MAX_ITEMS",
            str(INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE),
        )
    )

    try:
        queue = YtTaskQueue(database)
        with _bootstrap_admission_transaction(database):
            dedupe_hours = _dedupe_hours(
                "OPENBILICLAW_YT_BOOTSTRAP_DEDUPE_HOURS",
                DEFAULT_YT_BOOTSTRAP_DEDUPE_HOURS,
            )
            find_recent = getattr(queue, "find_recent_task", None)
            if not force and dedupe_hours > 0 and callable(find_recent):
                recent = find_recent(
                    "bootstrap_profile",
                    recent_hours=dedupe_hours,
                    statuses=_RECENT_TASK_STATUSES,
                )
                if recent is not None:
                    reused = _recent_reuse_result(
                        recent,
                        message=(
                            "  [dim]复用最近的 YouTube bootstrap 任务"
                            "({status})；需要重新拉取可设 "
                            "OPENBILICLAW_YT_BOOTSTRAP_DEDUPE_HOURS=0。[/dim]"
                        ),
                        notify=notify,
                    )
                    if reused is not None:
                        return reused

            active = _active_bootstrap_result(database, notify=notify)
            if active is not None:
                return active

            payload = _incremental_payload(
                {
                    "scopes": ["yt_history", "yt_subscriptions", "yt_likes"],
                    "max_items_per_scope": max(1, max_items),
                    "max_scroll_rounds": max(0, scroll_rounds),
                },
                incremental,
            )
            task_id = queue.enqueue_with_id("bootstrap_profile", payload, daily_budget=10)
    except Exception as exc:
        _notify(notify, f"  [yellow]YouTube 初始化信号未导入: {exc}[/yellow]")
        return BootstrapEnqueueResult(task_id=None, created=False, reason="enqueue_error")

    return _created_or_budget_result(
        task_id,
        budget_message="  [yellow]YouTube 初始化信号未导入: 今日任务预算已用完。[/yellow]",
        notify=notify,
    )


@_serialized_enqueue
def enqueue_zhihu_bootstrap(
    database: Any,
    *,
    force: bool = False,
    incremental: bool = False,
    profile_slug: str = "",
    profile_update: bool = False,
    notify: Notify | None = None,
) -> BootstrapEnqueueResult:
    """Enqueue the Zhihu ``bootstrap_events`` task without dispatching it."""
    from openbiliclaw.sources.zhihu_tasks import ZhihuTaskQueue

    max_items = int(
        os.environ.get(
            "OPENBILICLAW_ZHIHU_BOOTSTRAP_MAX_ITEMS",
            str(INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE),
        )
    )
    max_collections = int(os.environ.get("OPENBILICLAW_ZHIHU_BOOTSTRAP_MAX_COLLECTIONS", "20"))

    try:
        queue = ZhihuTaskQueue(database)
        with _bootstrap_admission_transaction(database):
            dedupe_hours = _dedupe_hours(
                "OPENBILICLAW_ZHIHU_BOOTSTRAP_DEDUPE_HOURS",
                DEFAULT_ZHIHU_BOOTSTRAP_DEDUPE_HOURS,
            )
            find_recent = getattr(queue, "find_recent_task", None)
            if not force and dedupe_hours > 0 and callable(find_recent):
                recent = find_recent(
                    "bootstrap_events",
                    recent_hours=dedupe_hours,
                    statuses=_RECENT_TASK_STATUSES,
                )
                if recent is not None:
                    reused = _recent_reuse_result(
                        recent,
                        message=(
                            "  [dim]复用最近的知乎 bootstrap 任务"
                            "({status})；需要重新拉取可设 "
                            "OPENBILICLAW_ZHIHU_BOOTSTRAP_DEDUPE_HOURS=0。[/dim]"
                        ),
                        notify=notify,
                    )
                    if reused is not None:
                        return reused

            active = _active_bootstrap_result(database, notify=notify)
            if active is not None:
                return active

            scopes = ["zhihu_read_history", "zhihu_collection", "zhihu_activity"]
            if not profile_slug.strip():
                _notify(
                    notify,
                    "  [dim]未传 --profile-slug，扩展会尝试从知乎登录态识别当前用户；"
                    "识别失败时只返回浏览记录和收藏夹。[/dim]",
                )
            payload = _incremental_payload(
                {
                    "scopes": scopes,
                    "profile_slug": profile_slug.strip(),
                    "max_items_per_scope": max(1, max_items),
                    "max_collections": max(1, max_collections),
                    "profile_update": bool(profile_update),
                },
                incremental,
            )
            task_id = queue.enqueue_with_id("bootstrap_events", payload, daily_budget=10)
    except Exception as exc:
        _notify(notify, f"  [yellow]知乎事件未拉取: {exc}[/yellow]")
        return BootstrapEnqueueResult(task_id=None, created=False, reason="enqueue_error")

    return _created_or_budget_result(
        task_id,
        budget_message="  [yellow]知乎事件未拉取: 今日任务预算已用完。[/yellow]",
        notify=notify,
    )


@_serialized_enqueue
def enqueue_reddit_bootstrap(
    database: Any,
    *,
    force: bool = False,
    incremental: bool = False,
    profile_update: bool = False,
    notify: Notify | None = None,
) -> BootstrapEnqueueResult:
    """Enqueue the Reddit ``bootstrap_events`` task without dispatching it."""
    from openbiliclaw.sources.reddit_tasks import RedditTaskQueue

    max_items = int(
        os.environ.get(
            "OPENBILICLAW_REDDIT_BOOTSTRAP_MAX_ITEMS",
            str(INIT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE),
        )
    )

    try:
        queue = RedditTaskQueue(database)
        with _bootstrap_admission_transaction(database):
            dedupe_hours = _dedupe_hours(
                "OPENBILICLAW_REDDIT_BOOTSTRAP_DEDUPE_HOURS",
                DEFAULT_REDDIT_BOOTSTRAP_DEDUPE_HOURS,
            )
            find_recent = getattr(queue, "find_recent_task", None)
            if not force and dedupe_hours > 0 and callable(find_recent):
                recent = find_recent(
                    "bootstrap_events",
                    recent_hours=dedupe_hours,
                    statuses=_RECENT_TASK_STATUSES,
                )
                if recent is not None:
                    reused = _recent_reuse_result(
                        recent,
                        message=(
                            "  [dim]复用最近的 Reddit bootstrap 任务"
                            "({status})；需要重新拉取可设 "
                            "OPENBILICLAW_REDDIT_BOOTSTRAP_DEDUPE_HOURS=0。[/dim]"
                        ),
                        notify=notify,
                    )
                    if reused is not None:
                        return reused

            active = _active_bootstrap_result(database, notify=notify)
            if active is not None:
                return active

            payload = _incremental_payload(
                {
                    "scopes": ["reddit_saved", "reddit_upvoted", "reddit_subscribed"],
                    "max_items_per_scope": max(1, max_items),
                    "profile_update": bool(profile_update),
                },
                incremental,
            )
            task_id = queue.enqueue_with_id("bootstrap_events", payload, daily_budget=10)
    except Exception as exc:
        _notify(notify, f"  [yellow]Reddit 初始化事件未拉取: {exc}[/yellow]")
        return BootstrapEnqueueResult(task_id=None, created=False, reason="enqueue_error")

    return _created_or_budget_result(
        task_id,
        budget_message="  [yellow]Reddit 初始化事件未拉取: 今日任务预算已用完。[/yellow]",
        notify=notify,
    )


__all__ = [
    "BootstrapEnqueueResult",
    "SOURCE_BOOTSTRAP_DECISION_LOCK",
    "enqueue_dy_bootstrap",
    "enqueue_reddit_bootstrap",
    "enqueue_xhs_bootstrap",
    "enqueue_yt_bootstrap",
    "enqueue_zhihu_bootstrap",
    "seed_guided_init_attempts",
]
