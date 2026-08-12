"""V2EX browser-bootstrap tasks and canonical event conversion.

The extension owns the browser session and returns only normalized public rows.
This module owns task durability, reply aggregation semantics, and the mapping
from those rows into OpenBiliClaw's unified source-event contract.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import TYPE_CHECKING, Any

from openbiliclaw.sources.event_format import build_event

if TYPE_CHECKING:
    from openbiliclaw.storage.database import Database

V2EX_BOOTSTRAP_SCOPES = (
    "public_topics",
    "public_replies",
    "favorite_topics",
    "favorite_nodes",
)
V2EX_FAVORITE_SCOPES = ("favorite_topics", "favorite_nodes")
V2EX_BOOTSTRAP_EVENT_TYPES = {
    "public_topics": "publish",
    "public_replies": "discussion_reply",
    "favorite_topics": "favorite",
    "favorite_nodes": "follow",
}
V2EX_BOOTSTRAP_SIGNAL_STRENGTH = {
    "public_topics": 0.90,
    "public_replies": 0.75,
    "favorite_topics": 1.00,
    "favorite_nodes": 0.65,
}
V2EX_BOOTSTRAP_SCOPE_LABELS = {
    "public_topics": "发布主题",
    "public_replies": "参与讨论",
    "favorite_topics": "收藏主题",
    "favorite_nodes": "收藏节点",
}

_RECENT_TASK_STATUSES = ("pending", "in_progress", "completed", "failed")
_V2EX_ITEM_TEXT_LIMITS = {
    "topic_id": 64,
    "title": 300,
    "author_name": 128,
    "url": 500,
    "node_name": 128,
    "node_title": 200,
    "reply_id": 64,
    "reply_text": 200,
    "published_at": 80,
}


def _text(value: object, *, limit: int = 6000) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return unescape(str(value)).strip()[:limit]


def _sanitize_item(raw: object) -> dict[str, Any]:
    """Keep only bounded public DOM fields from an extension result row."""

    if not isinstance(raw, dict):
        return {}
    scope = _text(raw.get("scope"), limit=64)
    if scope not in V2EX_BOOTSTRAP_SCOPES:
        return {}
    item: dict[str, Any] = {"scope": scope}
    for field, limit in _V2EX_ITEM_TEXT_LIMITS.items():
        value = _text(raw.get(field), limit=limit)
        if value:
            item[field] = value
    if not item.get("topic_id"):
        content_id = _text(raw.get("content_id"), limit=64)
        if content_id:
            item["topic_id"] = content_id
    raw_excerpts = raw.get("reply_excerpts")
    excerpts = raw_excerpts if isinstance(raw_excerpts, list) else []
    normalized_excerpts: list[str] = []
    for value in excerpts:
        excerpt = _text(value, limit=200)
        if excerpt and excerpt not in normalized_excerpts:
            normalized_excerpts.append(excerpt)
        if len(normalized_excerpts) >= 3:
            break
    if normalized_excerpts:
        item["reply_excerpts"] = normalized_excerpts
    if scope == "favorite_nodes":
        if not item.get("node_name"):
            return {}
    elif not item.get("topic_id") and not item.get("url") and not item.get("title"):
        return {}
    return item


def _sanitize_scope_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for scope in V2EX_BOOTSTRAP_SCOPES:
        try:
            value = int(raw.get(scope, 0) or 0)
        except (TypeError, ValueError):
            continue
        counts[scope] = max(0, min(value, 100_000))
    return counts


def _sanitize_debug(raw: object) -> dict[str, Any]:
    """Persist diagnostics without accepting HTML, cookies, or headers."""

    if not isinstance(raw, dict):
        return {}
    debug: dict[str, Any] = {}
    username = _text(raw.get("username"), limit=128)
    if username:
        debug["username"] = username
    if isinstance(raw.get("logged_in"), bool):
        debug["logged_in"] = raw["logged_in"]
    failures = raw.get("failures")
    if isinstance(failures, list):
        clean_failures = [_text(value, limit=160) for value in failures]
        debug["failures"] = [value for value in clean_failures if value][:20]
    scope_complete = raw.get("scope_complete")
    if isinstance(scope_complete, dict):
        debug["scope_complete"] = {
            scope: value
            for scope in V2EX_BOOTSTRAP_SCOPES
            if isinstance((value := scope_complete.get(scope)), bool)
        }
    scope_statuses = raw.get("scope_statuses")
    if isinstance(scope_statuses, dict):
        allowed_statuses = {
            "ok",
            "empty",
            "hidden",
            "login_required",
            "rate_limited",
            "parse_error",
            "failed",
        }
        debug["scope_statuses"] = {
            scope: status
            for scope in V2EX_BOOTSTRAP_SCOPES
            if (status := _text(scope_statuses.get(scope), limit=32).lower()) in allowed_statuses
        }
    return debug


def v2ex_bootstrap_item_key(item: dict[str, Any]) -> str:
    """Return a stable scope-aware identity for one bootstrap row."""

    if not isinstance(item, dict):
        return ""
    scope = _text(item.get("scope"), limit=64)
    if scope not in V2EX_BOOTSTRAP_SCOPES:
        return ""
    topic_id = _text(item.get("topic_id") or item.get("content_id"), limit=64)
    if topic_id:
        return f"{scope}:topic:{topic_id}"
    node_name = _text(item.get("node_name"), limit=128).lower()
    if scope == "favorite_nodes" and node_name:
        return f"{scope}:node:{node_name}"
    fallback = _text(item.get("url") or item.get("title"), limit=300)
    return f"{scope}:{fallback}" if fallback else ""


def _topic_id(item: dict[str, Any]) -> str:
    value = _text(item.get("topic_id") or item.get("content_id"), limit=64)
    if value.isdigit():
        return value
    url = _text(item.get("url"), limit=500)
    marker = "/t/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        candidate = tail.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return candidate if candidate.isdigit() else ""
    return ""


def _canonical_topic_url(topic_id: str, value: object) -> str:
    # Never preserve query strings or alternate hosts from DOM/search input.
    # Topic ID is the identity; the canonical URL is derived from it.
    del value
    return f"https://www.v2ex.com/t/{topic_id}" if topic_id else ""


def _reply_excerpts(item: dict[str, Any]) -> list[str]:
    raw = item.get("reply_excerpts")
    values = raw if isinstance(raw, list) else [item.get("reply_text", "")]
    result: list[str] = []
    for value in values:
        text = _text(value, limit=200)
        if text and text not in result:
            result.append(text)
        if len(result) >= 3:
            break
    return result


def v2ex_bootstrap_items_to_events(
    items: list[dict[str, Any]],
    *,
    identity_username: str = "",
) -> list[dict[str, Any]]:
    """Convert normalized V2EX bootstrap rows into unified events.

    Reply rows are deliberately aggregated by ``topic_id`` before conversion,
    so one discussion topic creates at most one event in the profile stream.
    A reply is evidence of attention, not proof that the user agrees with the
    topic's first post; its event therefore keeps satisfaction ``unknown``.
    """

    grouped_replies: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    seen_non_reply: set[str] = set()
    for raw_item in items:
        item = _sanitize_item(raw_item)
        if not item:
            continue
        scope = _text(item.get("scope"), limit=64)
        if scope not in V2EX_BOOTSTRAP_SCOPES:
            continue
        if scope == "public_replies":
            topic_id = _topic_id(item)
            if not topic_id:
                continue
            existing = grouped_replies.get(topic_id)
            if existing is None:
                existing = dict(item)
                existing["reply_excerpts"] = []
                grouped_replies[topic_id] = existing
                ordered.append(existing)
            excerpts = existing["reply_excerpts"]
            if isinstance(excerpts, list):
                for excerpt in _reply_excerpts(item):
                    if excerpt not in excerpts and len(excerpts) < 3:
                        excerpts.append(excerpt)
            continue
        key = v2ex_bootstrap_item_key(item)
        if key and key in seen_non_reply:
            continue
        if key:
            seen_non_reply.add(key)
        ordered.append(item)

    events: list[dict[str, Any]] = []
    for item in ordered:
        scope = _text(item.get("scope"), limit=64)
        event_type = V2EX_BOOTSTRAP_EVENT_TYPES.get(scope)
        if event_type is None:
            continue
        topic_id = _topic_id(item)
        node_name = _text(item.get("node_name"), limit=128)
        node_title = _text(item.get("node_title"), limit=200)
        title = _text(item.get("title"), limit=300)
        url = _canonical_topic_url(topic_id, item.get("url"))
        if scope == "favorite_nodes":
            title = node_title or node_name
            url = f"https://www.v2ex.com/go/{node_name}" if node_name else ""
            if not title and not url:
                continue
        elif not title and not url:
            continue

        author = _text(item.get("author_name") or item.get("author"), limit=128)
        excerpts = _reply_excerpts(item) if scope == "public_replies" else []
        label = V2EX_BOOTSTRAP_SCOPE_LABELS[scope]
        context = f"V2EX{label}：{title or url}"
        if node_name:
            context += f"（节点：{node_title or node_name}）"
        if author:
            context += f" 作者：{author}"
        if excerpts:
            context += f"，回复：『{'；'.join(excerpts)}』"

        metadata: dict[str, Any] = {
            "content_type": "node" if scope == "favorite_nodes" else "topic",
            "content_id": topic_id or node_name,
            "topic_id": topic_id,
            "node_name": node_name,
            "node_title": node_title,
            "import_source": f"v2ex_bootstrap_{scope}",
            "signal_strength": V2EX_BOOTSTRAP_SIGNAL_STRENGTH[scope],
            # This is intentionally separate from evidence strength. A reply
            # says the user engaged with a topic, not that they endorse it.
            "satisfaction": (
                "positive" if scope in {"favorite_topics", "favorite_nodes"} else "unknown"
            ),
        }
        normalized_identity = _text(identity_username, limit=128)
        if normalized_identity:
            metadata["source_identity"] = normalized_identity
        if excerpts:
            metadata["reply_excerpts"] = excerpts
        published_at = _text(item.get("published_at"), limit=80)
        if published_at:
            metadata["published_at"] = published_at

        events.append(
            build_event(
                event_type=event_type,
                source_platform="v2ex",
                title=title,
                url=url,
                author=author,
                context=context,
                metadata=metadata,
            )
        )
    return events


def v2ex_snapshot_effects_to_events(
    effects: list[dict[str, Any]],
    *,
    identity_username: str = "",
) -> list[dict[str, Any]]:
    """Convert durable favorite-snapshot effects into profile events.

    A restore is a new positive favorite/follow generation. A retraction is a
    weak, timestamped feedback event so the generic evidence layer discounts
    the matching historical positive event instead of deleting it.
    """

    events: list[dict[str, Any]] = []
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        action = _text(effect.get("action"), limit=32)
        scope = _text(effect.get("scope"), limit=64)
        if action not in {"restore", "retract"} or scope not in V2EX_FAVORITE_SCOPES:
            continue
        item = _sanitize_item(effect.get("item"))
        if not item or item.get("scope") != scope:
            continue
        positive_events = v2ex_bootstrap_items_to_events(
            [item],
            identity_username=identity_username,
        )
        if not positive_events:
            continue
        positive = positive_events[0]
        metadata = positive.get("metadata")
        positive_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        positive_metadata["snapshot_effect"] = action
        positive_metadata["snapshot_generation"] = int(effect.get("generation", 1) or 1)
        positive_metadata["snapshot_effect_key"] = _text(effect.get("effect_key"), limit=300)
        if action == "restore":
            positive["metadata"] = positive_metadata
            events.append(positive)
            continue

        event_type = V2EX_BOOTSTRAP_EVENT_TYPES[scope]
        title = _text(positive.get("title"), limit=300)
        url = _text(positive.get("url"), limit=500)
        label = "收藏主题" if scope == "favorite_topics" else "收藏节点"
        events.append(
            build_event(
                event_type="feedback",
                source_platform="v2ex",
                title=title,
                url=url,
                context=f"V2EX 已取消{label}：{title or url}",
                metadata={
                    **positive_metadata,
                    "feedback_type": "retraction",
                    "retracted_action": event_type,
                    "timestamp": _text(effect.get("created_at"), limit=80),
                    "signal_strength": 0.2,
                    "satisfaction": "neutral",
                },
            )
        )
    return events


class V2EXFavoriteSnapshotStore:
    """Apply complete favorite snapshots with crash-safe, idempotent effects.

    Missing rows are not immediately interpreted as an unfavorite. Only two
    consecutive *complete* snapshots create a retraction. Effects live in a
    durable outbox so a process crash between snapshot comparison and event
    ingress replays the same stable identity rather than losing or duplicating
    the transition.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2ex_favorite_snapshot_items (
                username_key TEXT NOT NULL,
                username TEXT NOT NULL,
                scope TEXT NOT NULL,
                item_key TEXT NOT NULL,
                item_json TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                missing_streak INTEGER NOT NULL DEFAULT 0,
                generation INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_missing_at TEXT NOT NULL DEFAULT '',
                retracted_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (username_key, scope, item_key)
            );
            CREATE INDEX IF NOT EXISTS idx_v2ex_favorite_snapshot_active
                ON v2ex_favorite_snapshot_items (username_key, scope, active);

            CREATE TABLE IF NOT EXISTS v2ex_favorite_snapshot_runs (
                task_id TEXT NOT NULL,
                username_key TEXT NOT NULL,
                scope TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                PRIMARY KEY (task_id, scope)
            );

            CREATE TABLE IF NOT EXISTS v2ex_favorite_snapshot_effects (
                effect_key TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                username_key TEXT NOT NULL,
                scope TEXT NOT NULL,
                item_key TEXT NOT NULL,
                action TEXT NOT NULL,
                generation INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                emitted_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_v2ex_snapshot_effects_task
                ON v2ex_favorite_snapshot_effects (task_id, scope, status);
            """
        )

    @staticmethod
    def _username(value: object) -> tuple[str, str]:
        username = _text(value, limit=128)
        if not username or any(ord(char) < 32 for char in username):
            raise ValueError("V2EX snapshot username is required")
        return username, username.casefold()

    @staticmethod
    def _effect_from_row(row: Any) -> dict[str, Any]:
        try:
            item = json.loads(str(row["item_json"] or "{}"))
        except (TypeError, ValueError):
            item = {}
        return {
            "effect_key": str(row["effect_key"]),
            "scope": str(row["scope"]),
            "action": str(row["action"]),
            "generation": int(row["generation"]),
            "item": item if isinstance(item, dict) else {},
            "created_at": str(row["created_at"]),
        }

    def pending_effects(self, task_id: str, scope: str | None = None) -> list[dict[str, Any]]:
        """Return the un-emitted outbox effects for a staged task."""

        params: list[Any] = [str(task_id)]
        where = "task_id=? AND status='pending'"
        if scope is not None:
            if scope not in V2EX_FAVORITE_SCOPES:
                return []
            where += " AND scope=?"
            params.append(scope)
        rows = self._db.conn.execute(
            f"""
            SELECT * FROM v2ex_favorite_snapshot_effects
            WHERE {where}
            ORDER BY created_at, effect_key
            """,
            params,
        ).fetchall()
        return [self._effect_from_row(row) for row in rows]

    def prepare_complete_snapshot(
        self,
        *,
        task_id: str,
        username: str,
        scope: str,
        items: list[dict[str, Any]],
        observed_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compare one proven-complete snapshot and stage transition effects."""

        normalized_task_id = _text(task_id, limit=200)
        if not normalized_task_id:
            raise ValueError("V2EX snapshot task_id is required")
        if scope not in V2EX_FAVORITE_SCOPES:
            raise ValueError("V2EX snapshot scope is invalid")
        display_username, username_key = self._username(username)
        timestamp = _text(observed_at, limit=80) or datetime.now(UTC).isoformat()
        current: dict[str, dict[str, Any]] = {}
        for raw_item in items:
            item = _sanitize_item(raw_item)
            if item.get("scope") != scope:
                continue
            item_key = v2ex_bootstrap_item_key(item)
            if item_key:
                current[item_key] = item

        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replay = conn.execute(
                """
                SELECT 1 FROM v2ex_favorite_snapshot_runs
                WHERE task_id=? AND scope=?
                """,
                (normalized_task_id, scope),
            ).fetchone()
            if replay is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM v2ex_favorite_snapshot_effects
                    WHERE task_id=? AND scope=? AND status='pending'
                    ORDER BY created_at, effect_key
                    """,
                    (normalized_task_id, scope),
                ).fetchall()
                conn.commit()
                return [self._effect_from_row(row) for row in rows]

            existing_rows = conn.execute(
                """
                SELECT * FROM v2ex_favorite_snapshot_items
                WHERE username_key=? AND scope=?
                """,
                (username_key, scope),
            ).fetchall()
            existing = {str(row["item_key"]): row for row in existing_rows}

            for item_key, item in current.items():
                item_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                row = existing.get(item_key)
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO v2ex_favorite_snapshot_items (
                            username_key, username, scope, item_key, item_json,
                            active, missing_streak, generation, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, 1, 0, 1, ?, ?)
                        """,
                        (
                            username_key,
                            display_username,
                            scope,
                            item_key,
                            item_json,
                            timestamp,
                            timestamp,
                        ),
                    )
                    continue

                generation = int(row["generation"] or 1)
                was_active = bool(row["active"])
                if not was_active:
                    generation += 1
                    effect_key = f"v2ex-snapshot:{username_key}:{item_key}:restore:{generation}"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO v2ex_favorite_snapshot_effects (
                            effect_key, task_id, username_key, scope, item_key,
                            action, generation, item_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'restore', ?, ?, ?)
                        """,
                        (
                            effect_key,
                            normalized_task_id,
                            username_key,
                            scope,
                            item_key,
                            generation,
                            item_json,
                            timestamp,
                        ),
                    )
                conn.execute(
                    """
                    UPDATE v2ex_favorite_snapshot_items
                    SET username=?, item_json=?, active=1, missing_streak=0,
                        generation=?, last_seen_at=?, last_missing_at='', retracted_at=''
                    WHERE username_key=? AND scope=? AND item_key=?
                    """,
                    (
                        display_username,
                        item_json,
                        generation,
                        timestamp,
                        username_key,
                        scope,
                        item_key,
                    ),
                )

            for item_key, row in existing.items():
                if item_key in current or not bool(row["active"]):
                    continue
                missing_streak = int(row["missing_streak"] or 0) + 1
                generation = int(row["generation"] or 1)
                if missing_streak >= 2:
                    effect_key = f"v2ex-snapshot:{username_key}:{item_key}:retract:{generation}"
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO v2ex_favorite_snapshot_effects (
                            effect_key, task_id, username_key, scope, item_key,
                            action, generation, item_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'retract', ?, ?, ?)
                        """,
                        (
                            effect_key,
                            normalized_task_id,
                            username_key,
                            scope,
                            item_key,
                            generation,
                            str(row["item_json"]),
                            timestamp,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE v2ex_favorite_snapshot_items
                        SET active=0, missing_streak=?, last_missing_at=?, retracted_at=?
                        WHERE username_key=? AND scope=? AND item_key=?
                        """,
                        (
                            missing_streak,
                            timestamp,
                            timestamp,
                            username_key,
                            scope,
                            item_key,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE v2ex_favorite_snapshot_items
                        SET missing_streak=?, last_missing_at=?
                        WHERE username_key=? AND scope=? AND item_key=?
                        """,
                        (missing_streak, timestamp, username_key, scope, item_key),
                    )

            conn.execute(
                """
                INSERT INTO v2ex_favorite_snapshot_runs (
                    task_id, username_key, scope, observed_at, item_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (normalized_task_id, username_key, scope, timestamp, len(current)),
            )
            rows = conn.execute(
                """
                SELECT * FROM v2ex_favorite_snapshot_effects
                WHERE task_id=? AND scope=? AND status='pending'
                ORDER BY created_at, effect_key
                """,
                (normalized_task_id, scope),
            ).fetchall()
            conn.commit()
            return [self._effect_from_row(row) for row in rows]
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def mark_effects_emitted(self, effect_keys: list[str]) -> int:
        """Acknowledge outbox effects after durable event ingress accepts them."""

        keys = list(dict.fromkeys(_text(key, limit=300) for key in effect_keys))
        keys = [key for key in keys if key]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        conn = self._db.open_connection()
        try:
            cursor = conn.execute(
                f"""
                UPDATE v2ex_favorite_snapshot_effects
                SET status='emitted', emitted_at=?
                WHERE status='pending' AND effect_key IN ({placeholders})
                """,
                (datetime.now(UTC).isoformat(), *keys),
            )
            conn.commit()
            return int(cursor.rowcount)
        finally:
            conn.close()


def _merge_v2ex_result_payload(
    current: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    scope_counts: dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in current.get("items") or []:
        item = _sanitize_item(raw_item)
        if not item:
            continue
        key = v2ex_bootstrap_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(item)
    added: list[dict[str, Any]] = []
    for raw_item in items or []:
        item = _sanitize_item(raw_item)
        if not item:
            continue
        key = v2ex_bootstrap_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(item)
        added.append(item)

    merged: dict[str, Any] = {}
    if merged_items:
        merged["items"] = merged_items
    counts: dict[str, Any] = {}
    counts.update(_sanitize_scope_counts(current.get("scope_counts")))
    for scope, count in _sanitize_scope_counts(scope_counts).items():
        counts[scope] = max(count, counts.get(scope, 0))
    for scope in V2EX_BOOTSTRAP_SCOPES:
        count = sum(1 for item in merged_items if _text(item.get("scope"), limit=64) == scope)
        if count or scope in counts:
            counts[scope] = max(count, int(counts.get(scope, 0) or 0))
    if counts:
        merged["scope_counts"] = counts
    if isinstance(current.get("debug"), dict) or isinstance(debug, dict):
        merged_debug = _sanitize_debug(current.get("debug"))
        merged_debug.update(_sanitize_debug(debug))
        merged["debug"] = merged_debug
    return merged, added


class V2EXTaskQueue:
    """Durable queue for read-only V2EX browser tasks."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2ex_tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                claimed_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_v2ex_tasks_status
                ON v2ex_tasks (status, created_at);
            """
        )

    def enqueue_with_id(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        daily_budget: int = 10,
    ) -> str | None:
        if daily_budget > 0 and self._budgeted_count_today(task_type) >= daily_budget:
            return None
        task_id = str(uuid.uuid4())
        participating = bool(self._db.conn.in_transaction)
        self._db.conn.execute(
            "INSERT INTO v2ex_tasks (id, type, payload_json) VALUES (?, ?, ?)",
            (task_id, task_type, json.dumps(payload, ensure_ascii=False)),
        )
        if not participating:
            self._db.conn.commit()
        return task_id

    def _budgeted_count_today(self, task_type: str) -> int:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        row = self._db.conn.execute(
            "SELECT COUNT(*) FROM v2ex_tasks WHERE type = ? AND created_at >= ?",
            (task_type, today),
        ).fetchone()
        return int(row[0] if row else 0)

    def next_pending(self, only_ids: set[str] | None = None) -> dict[str, Any] | None:
        stale_before = (datetime.now(UTC) - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        where = (
            "(status = 'pending' OR "
            "(status = 'in_progress' AND (claimed_at IS NULL OR claimed_at <= ?)))"
        )
        params: list[Any] = [stale_before]
        if only_ids is not None:
            ids = [str(value) for value in only_ids if str(value).strip()]
            if not ids:
                return None
            where += f" AND id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # The JavaScript mutex is scoped to one extension worker. Multiple
            # unpacked extension IDs/browser profiles do not share it, so the
            # queue itself must enforce one fresh V2EX lease at a time.
            active = conn.execute(
                """
                SELECT 1 FROM v2ex_tasks
                WHERE status='in_progress' AND claimed_at > ?
                LIMIT 1
                """,
                (stale_before,),
            ).fetchone()
            if active is not None:
                conn.commit()
                return None
            row = conn.execute(
                f"""
                SELECT * FROM v2ex_tasks
                WHERE {where}
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = str(row["id"])
            conn.execute(
                """
                UPDATE v2ex_tasks
                SET status='in_progress', claimed_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (task_id,),
            )
            claimed = conn.execute("SELECT * FROM v2ex_tasks WHERE id=?", (task_id,)).fetchone()
            conn.commit()
            return dict(claimed) if claimed is not None else None
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

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
        cutoff = (datetime.now(UTC) - timedelta(hours=recent_hours)).strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in selected)
        row = self._db.conn.execute(
            f"""
            SELECT * FROM v2ex_tasks
            WHERE type=? AND created_at>=? AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1
            """,
            (task_type, cutoff, *selected),
        ).fetchone()
        return dict(row) if row is not None else None

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute("SELECT * FROM v2ex_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def merge_result(
        self,
        task_id: str,
        *,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        added: list[dict[str, Any]] = []

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal added
            merged, added = _merge_v2ex_result_payload(
                current,
                items=items,
                scope_counts=scope_counts,
                debug=debug,
            )
            return merged

        mutate_unstaged_result(
            self._db,
            table="v2ex_tasks",
            task_id=task_id,
            mutate=mutate,
        )
        return added

    def stage_final_result(
        self,
        task_id: str,
        *,
        terminal_status: str,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from openbiliclaw.sources.task_result_protocol import stage_terminal_result

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            merged, _ = _merge_v2ex_result_payload(
                current,
                items=items,
                scope_counts=scope_counts,
                debug=debug,
            )
            return merged

        return stage_terminal_result(
            self._db,
            table="v2ex_tasks",
            task_id=task_id,
            terminal_status=terminal_status,
            merge=merge,
        )

    def complete_staged_result(self, task_id: str) -> bool:
        from openbiliclaw.sources.task_result_protocol import complete_staged_result

        return complete_staged_result(self._db, table="v2ex_tasks", task_id=task_id)

    def fail(self, task_id: str, *, error: str = "", debug: dict[str, Any] | None = None) -> bool:
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        payload: dict[str, Any] = {"error": _text(error, limit=240)}
        if isinstance(debug, dict):
            payload["debug"] = debug
        mutated, _ = mutate_unstaged_result(
            self._db,
            table="v2ex_tasks",
            task_id=task_id,
            mutate=lambda _current: payload,
            terminal_status="failed",
        )
        return mutated


__all__ = [
    "V2EX_BOOTSTRAP_EVENT_TYPES",
    "V2EX_BOOTSTRAP_SCOPES",
    "V2EX_FAVORITE_SCOPES",
    "V2EXFavoriteSnapshotStore",
    "V2EXTaskQueue",
    "v2ex_bootstrap_item_key",
    "v2ex_bootstrap_items_to_events",
    "v2ex_snapshot_effects_to_events",
]
