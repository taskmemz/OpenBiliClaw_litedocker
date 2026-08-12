"""Browser-backed Weibo bootstrap tasks and event conversion.

The backend never receives a Weibo cookie.  The extension runs these bounded
read-only requests in a logged-in ``m.weibo.cn`` task tab and sends normalized
rows back through the same staged task-result protocol used by Zhihu/Reddit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from openbiliclaw.sources.event_format import SOURCE_WEIBO, build_event

if TYPE_CHECKING:
    from openbiliclaw.storage.database import Database

logger = logging.getLogger(__name__)

WEIBO_BOOTSTRAP_SCOPES = (
    "weibo_favorites",
    "weibo_following",
    "weibo_mentions",
)
_RECENT_TASK_STATUSES = ("pending", "in_progress", "completed", "failed")
_TAG_RE = re.compile(r"<[^>]+>")
_ACCOUNT_KEY_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_WEIBO_SCOPE_EVENT: dict[str, tuple[str, float, str]] = {
    "weibo_favorites": ("favorite", 0.90, "收藏"),
    "weibo_following": ("follow", 0.65, "关注"),
    "weibo_mentions": ("comment", 0.75, "互动"),
}


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = _TAG_RE.sub(" ", value)
    return " ".join(value.replace("&nbsp;", " ").split()).strip()


def _scalar(value: Any) -> str:
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _item_id(item: dict[str, Any]) -> str:
    for key in ("content_id", "status_id", "id", "uid", "user_id"):
        value = _scalar(item.get(key))
        if value:
            return value
    return ""


def weibo_account_key(user_id: Any) -> str:
    """Derive a stable, non-reversible account partition key from a uid.

    The browser must prove a current uid, but the uid itself is not needed by
    the profile/event projection.  Keep the durable account binding in the
    same ``sha256:<hex>`` shape used by the other browser-backed sources so a
    task result cannot accidentally turn a public identifier into profile
    metadata.
    """
    normalized = _scalar(user_id)
    if not normalized or not normalized.isdigit():
        return ""
    digest = hashlib.sha256(f"weibo:id:{normalized}".encode()).hexdigest()
    return f"sha256:{digest}"


def is_weibo_account_key(value: Any) -> bool:
    """Return whether *value* is a canonical opaque Weibo account key."""
    return bool(_ACCOUNT_KEY_RE.fullmatch(str(value or "").strip()))


def weibo_bootstrap_item_key(item: dict[str, Any], *, account_key: str = "") -> str:
    """Return a stable identity namespaced by account and signal scope."""
    scope = _scalar(item.get("scope"))
    identity = _item_id(item) or _scalar(item.get("url")) or _text(item.get("title"))
    if not scope or not identity:
        return ""
    prefix = f"{account_key}:" if is_weibo_account_key(account_key) else ""
    return f"{prefix}{scope}:{identity}"


def _status_title(item: dict[str, Any]) -> str:
    return _text(
        item.get("title")
        or item.get("text_raw")
        or item.get("raw_text")
        or item.get("text")
        or item.get("name")
    )


def weibo_bootstrap_items_to_events(
    items: list[dict[str, Any]],
    *,
    account_key: str = "",
) -> list[dict[str, Any]]:
    """Convert normalized logged-in Weibo rows into unified profile events."""
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        scope = _scalar(item.get("scope"))
        mapping = _WEIBO_SCOPE_EVENT.get(scope)
        if mapping is None:
            continue
        event_type, strength, label = mapping
        title = _status_title(item)
        content_id = _scalar(item.get("content_id")) or _scalar(item.get("status_id"))
        uid = _scalar(item.get("uid")) or _scalar(item.get("user_id"))
        author = _text(item.get("author") or item.get("screen_name") or item.get("name"))
        url = _scalar(item.get("url"))
        if not title and not url and not author:
            continue
        if scope == "weibo_following":
            title = title or author or uid
            context = f"微博关注：{author or title}"
        else:
            context = f"微博{label}：{title or url}"
            if author:
                context += f" 作者：{author}"
        metadata: dict[str, Any] = {
            "source_platform": SOURCE_WEIBO,
            "content_type": _scalar(item.get("content_type")) or "post",
            "content_id": content_id,
            "user_id": uid,
            "scope": scope,
            "import_source": f"weibo_bootstrap_{scope.removeprefix('weibo_')}",
            "signal_strength": strength,
        }
        if account_key:
            metadata["account_key"] = account_key
        for source_key, target_key in (
            ("interaction_time", "interaction_time"),
            ("published_at", "published_at"),
            ("summary", "summary"),
            ("like_count", "like_count"),
            ("comment_count", "comment_count"),
            ("share_count", "share_count"),
        ):
            value = item.get(source_key)
            if value not in (None, ""):
                metadata[target_key] = value
        events.append(
            build_event(
                event_type=event_type,
                source_platform=SOURCE_WEIBO,
                title=title or url or author,
                url=url,
                author=author,
                context=context,
                metadata=metadata,
            )
        )
    return events


def _merge_result_payload(
    current: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    scope_counts: dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in current.get("items") or []:
        if not isinstance(value, dict):
            continue
        key = weibo_bootstrap_item_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(value)
    added: list[dict[str, Any]] = []
    for value in items or []:
        if not isinstance(value, dict):
            continue
        key = weibo_bootstrap_item_key(value)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(value)
        added.append(value)
    merged: dict[str, Any] = {}
    if merged_items:
        merged["items"] = merged_items
    counts: dict[str, Any] = {}
    if isinstance(current.get("scope_counts"), dict):
        counts.update(current["scope_counts"])
    if isinstance(scope_counts, dict):
        for key, value in scope_counts.items():
            previous = counts.get(key, 0)
            if isinstance(previous, int) and isinstance(value, int):
                counts[key] = max(previous, value)
            else:
                counts[key] = value
    if counts:
        merged["scope_counts"] = counts
    if isinstance(current.get("debug"), dict) or isinstance(debug, dict):
        merged_debug: dict[str, Any] = {}
        if isinstance(current.get("debug"), dict):
            merged_debug.update(current["debug"])
        if isinstance(debug, dict):
            merged_debug.update(debug)
        merged["debug"] = merged_debug
    return merged, added


class WeiboTaskQueue:
    """Durable queue for read-only Weibo bootstrap tasks."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS weibo_tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                claimed_at TIMESTAMP,
                claim_token TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_weibo_tasks_status
                ON weibo_tasks (status, created_at);
            CREATE INDEX IF NOT EXISTS idx_weibo_tasks_type_created
                ON weibo_tasks (type, created_at);
            """
        )
        columns = {str(row[1]) for row in self._db.conn.execute("PRAGMA table_info(weibo_tasks)")}
        if "claim_token" not in columns:
            self._db.conn.execute(
                "ALTER TABLE weibo_tasks ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"
            )
        self._db.conn.commit()

    def enqueue_with_id(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        daily_budget: int = 10,
    ) -> str | None:
        conn = self._db.conn
        participating_in_transaction = bool(conn.in_transaction)
        count_today = self._budgeted_count_today(task_type) if daily_budget > 0 else 0
        if daily_budget > 0 and count_today >= daily_budget:
            logger.info(
                "weibo task budget exhausted: type=%s used_today=%d budget=%d",
                task_type,
                count_today,
                daily_budget,
            )
            return None
        task_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO weibo_tasks (id, type, payload_json) VALUES (?, ?, ?)",
            (task_id, task_type, json.dumps(payload, ensure_ascii=False)),
        )
        if not participating_in_transaction:
            conn.commit()
        return task_id

    def _budgeted_count_today(self, task_type: str) -> int:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        rows = self._db.conn.execute(
            "SELECT status, result_json FROM weibo_tasks WHERE type = ? AND created_at >= ?",
            (task_type, today),
        ).fetchall()
        count = 0
        for row in rows:
            status = str(row["status"] if hasattr(row, "keys") else row[0])
            result = row["result_json"] if hasattr(row, "keys") else row[1]
            try:
                payload = json.loads(str(result or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if (
                status == "failed"
                and isinstance(payload, dict)
                and payload.get("error") == "stale_pending"
            ):
                continue
            count += 1
        return count

    def next_pending(self, only_ids: set[str] | None = None) -> dict[str, Any] | None:
        stale_before = (datetime.now(UTC) - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        where = (
            "(status = 'pending' OR "
            "(status = 'in_progress' AND (claimed_at IS NULL OR claimed_at <= ?)))"
        )
        params: list[Any] = [stale_before]
        if only_ids is not None:
            ids = [str(value) for value in only_ids]
            if not ids:
                return None
            where += f" AND id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM weibo_tasks WHERE {where} ORDER BY created_at ASC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = str(row["id"])
            claim_token = str(uuid.uuid4())
            conn.execute(
                "UPDATE weibo_tasks SET status='in_progress', "
                "claimed_at=CURRENT_TIMESTAMP, claim_token=? WHERE id=?",
                (claim_token, task_id),
            )
            claimed = conn.execute("SELECT * FROM weibo_tasks WHERE id=?", (task_id,)).fetchone()
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        return dict(claimed) if claimed is not None else None

    def claim_token_matches(self, task_id: str, claim_token: str) -> bool:
        """Return whether a result still owns the task's current lease."""
        token = str(claim_token or "").strip()
        if not token:
            return False
        row = self._db.conn.execute(
            "SELECT claim_token FROM weibo_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        expected = str(row["claim_token"] or "")
        return bool(expected) and secrets.compare_digest(expected, token)

    def find_recent_task(
        self,
        task_type: str,
        *,
        recent_hours: float,
        statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if recent_hours <= 0:
            return None
        selected = statuses or _RECENT_TASK_STATUSES
        placeholders = ",".join("?" for _ in selected)
        cutoff = (datetime.now(UTC) - timedelta(hours=recent_hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self._db.conn.execute(
            f"SELECT * FROM weibo_tasks WHERE type=? AND created_at>=? "
            f"AND status IN ({placeholders}) "
            "ORDER BY CASE WHEN status IN ('pending','in_progress') THEN 0 "
            "WHEN status='completed' THEN 1 ELSE 2 END, created_at DESC LIMIT 1",
            (task_type, cutoff, *selected),
        ).fetchone()
        return dict(row) if row is not None else None

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute("SELECT * FROM weibo_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def merge_result(
        self,
        task_id: str,
        *,
        claim_token: str | None = None,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        complete: bool = False,
    ) -> list[dict[str, Any]]:
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        added: list[dict[str, Any]] = []

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal added
            merged, added = _merge_result_payload(
                current, items=items, scope_counts=scope_counts, debug=debug
            )
            return merged

        mutated, _ = mutate_unstaged_result(
            self._db,
            table="weibo_tasks",
            task_id=task_id,
            mutate=mutate,
            terminal_status="completed" if complete else None,
            expected_claim_token=claim_token,
        )
        return added if mutated else []

    def stage_final_result(
        self,
        task_id: str,
        *,
        terminal_status: str,
        claim_token: str | None = None,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from openbiliclaw.sources.task_result_protocol import stage_terminal_result

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            merged, _ = _merge_result_payload(
                current, items=items, scope_counts=scope_counts, debug=debug
            )
            return merged

        return stage_terminal_result(
            self._db,
            table="weibo_tasks",
            task_id=task_id,
            terminal_status=terminal_status,
            merge=merge,
            expected_claim_token=claim_token,
        )

    def complete_staged_result(self, task_id: str, *, claim_token: str | None = None) -> bool:
        from openbiliclaw.sources.task_result_protocol import complete_staged_result

        return complete_staged_result(
            self._db,
            table="weibo_tasks",
            task_id=task_id,
            expected_claim_token=claim_token,
        )

    def fail(
        self,
        task_id: str,
        *,
        claim_token: str | None = None,
        error: str = "",
        debug: dict[str, Any] | None = None,
    ) -> bool:
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        result: dict[str, Any] = {"error": error}
        if debug is not None:
            result["debug"] = debug
        mutated, _ = mutate_unstaged_result(
            self._db,
            table="weibo_tasks",
            task_id=task_id,
            mutate=lambda _current: result,
            terminal_status="failed",
            expected_claim_token=claim_token,
        )
        return mutated
