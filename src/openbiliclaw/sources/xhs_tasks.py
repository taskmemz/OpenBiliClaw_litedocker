"""xhs task queue and creator subscription storage.

The task queue bridges the backend's Soul-driven scheduler to the
extension's background dispatcher. The backend enqueues search/creator
tasks; the extension polls for pending tasks, opens a tab, collects
URLs, and posts the result back.

Creator subscriptions track xhs creators the user wants to follow —
a nightly scheduler enqueues one creator task per subscription.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from openbiliclaw.storage.database import Database

logger = logging.getLogger(__name__)

XHS_RATE_LIMIT_COOLDOWN_SECONDS = 60 * 60
XHS_RATE_LIMIT_MAX_COOLDOWN_SECONDS = 24 * 60 * 60
XHS_TASK_INTERVAL_JITTER_RATIO = 0.25
_XHS_RUNTIME_STATE_ROW_ID = 1
_XHS_PACED_TASK_TYPES = frozenset({"search", "creator"})

XHS_BOOTSTRAP_SCOPE_EVENT_TYPES = {
    "saved": "favorite",
    "liked": "like",
    "xhs_history": "view",
}

XHS_BOOTSTRAP_SIGNAL_STRENGTH = {
    "saved": 1.0,
    "liked": 0.85,
    "xhs_history": 0.35,
}

XHS_BOOTSTRAP_SCOPE_LABELS = {
    "saved": "收藏",
    "liked": "点赞",
    "xhs_history": "浏览记录",
}

_DEFAULT_BOOTSTRAP_SCOPES = ("saved", "liked", "xhs_history")
_DEFAULT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE = 300

_RECENT_TASK_STATUSES = ("pending", "in_progress", "completed", "failed")


def _utc_now(value: datetime | None = None) -> datetime:
    now = value or datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _format_sqlite_timestamp(value: datetime) -> str:
    return _utc_now(value).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sqlite_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return _utc_now(parsed)


def _remaining_seconds(until: datetime | None, now: datetime) -> int:
    if until is None:
        return 0
    seconds = max(0.0, (until - now).total_seconds())
    return int(seconds) if seconds.is_integer() else int(seconds) + 1


def _jittered_interval_seconds(
    target_seconds: int,
    *,
    task_id: str,
    jitter_ratio: float,
) -> int:
    """Return a stable per-task interval around the configured target."""
    target = max(0, int(target_seconds))
    ratio = min(0.9, max(0.0, float(jitter_ratio)))
    if target == 0 or ratio == 0:
        return target
    digest = hashlib.blake2b(
        task_id.encode("utf-8"),
        digest_size=8,
        person=b"xhs-pace",
    ).digest()
    unit = int.from_bytes(digest, "big") / ((1 << 64) - 1)
    factor = (1.0 - ratio) + (2.0 * ratio * unit)
    return max(1, round(target * factor))


def _note_key(note: dict[str, Any]) -> str:
    scope = str(note.get("scope", "")).strip()
    note_id = str(note.get("note_id", "")).strip()
    url = str(note.get("url", "")).strip()
    title = str(note.get("title", "")).strip()
    key = note_id or url or title
    return f"{scope}:{key}" if key else ""


def _has_publication_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _xhs_note_url_identity(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.hostname != "www.xiaohongshu.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    if parts[0] == "explore" or parts[0] == "search_result":
        return parts[-1]
    if len(parts) >= 3 and parts[:2] == ["discovery", "item"]:
        return parts[-1]
    return ""


def _xhs_url_token(value: Any) -> str:
    if not _xhs_note_url_identity(value):
        return ""
    try:
        parsed = urlparse(str(value))
        tokens = parse_qs(parsed.query, keep_blank_values=False).get("xsec_token", [])
    except ValueError:
        return ""
    return str(tokens[0]).strip() if tokens else ""


def _enrich_xhs_access_token(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Upgrade one admitted note from a bare URL to its first tokenized URL."""
    existing_url = existing.get("url")
    incoming_url = incoming.get("url")
    existing_identity = _xhs_note_url_identity(existing_url)
    incoming_identity = _xhs_note_url_identity(incoming_url)
    if not incoming_identity or (existing_identity and existing_identity != incoming_identity):
        return False
    existing_note_id = existing.get("note_id")
    incoming_note_id = incoming.get("note_id")
    expected_identity = (existing_note_id.strip() if isinstance(existing_note_id, str) else "") or (
        incoming_note_id.strip() if isinstance(incoming_note_id, str) else ""
    )
    if expected_identity and incoming_identity != expected_identity:
        return False

    existing_raw_token = existing.get("xsec_token")
    incoming_raw_token = incoming.get("xsec_token")
    existing_field_token = existing_raw_token.strip() if isinstance(existing_raw_token, str) else ""
    existing_url_token = _xhs_url_token(existing_url)
    existing_token = existing_field_token or existing_url_token
    incoming_field_token = incoming_raw_token.strip() if isinstance(incoming_raw_token, str) else ""
    incoming_url_token = _xhs_url_token(incoming_url)
    incoming_token = incoming_field_token or incoming_url_token
    if not incoming_token or (existing_token and existing_token != incoming_token):
        return False

    enriched = False
    if not existing_field_token:
        existing["xsec_token"] = incoming_token
        enriched = True
    if not existing_url_token and incoming_url_token:
        existing["url"] = incoming_url
        enriched = True
    return enriched


def xhs_bootstrap_note_key(note: dict[str, Any]) -> str:
    """Return the stable cross-task identity key for one bootstrap note."""
    return _note_key(note)


def _bootstrap_result_policy(
    task_type: object,
    payload_json: object,
) -> tuple[frozenset[str] | None, int | None]:
    """Return immutable bootstrap scopes and per-scope cap stored on one task."""
    if str(task_type or "").strip() != "bootstrap_profile":
        return None, None

    payload: dict[str, Any] = {}
    if isinstance(payload_json, str) and payload_json:
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed

    raw_limit = payload.get(
        "max_items_per_scope",
        _DEFAULT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE,
    )
    if isinstance(raw_limit, bool):
        max_items_per_scope = _DEFAULT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE
    else:
        try:
            max_items_per_scope = max(1, int(raw_limit))
        except (TypeError, ValueError, OverflowError):
            max_items_per_scope = _DEFAULT_BOOTSTRAP_MAX_ITEMS_PER_SCOPE

    raw_scopes = payload.get("scopes")
    scopes: list[str] = []
    if isinstance(raw_scopes, list):
        for value in raw_scopes:
            scope = str(value).strip() if isinstance(value, str) else ""
            if scope in XHS_BOOTSTRAP_SCOPE_EVENT_TYPES and scope not in scopes:
                scopes.append(scope)
    if not scopes:
        scopes = list(_DEFAULT_BOOTSTRAP_SCOPES)
    return frozenset(scopes), max_items_per_scope


def _merge_result_payload(
    current: dict[str, Any],
    *,
    urls: list[str] | None = None,
    notes: list[dict[str, Any]] | None = None,
    scope_counts: dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
    allowed_scopes: frozenset[str] | None = None,
    max_items_per_scope: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    merged_urls: list[str] = []
    seen_urls: set[str] = set()
    for url in [*(current.get("urls") or []), *(urls or [])]:
        if not isinstance(url, str) or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged_urls.append(url)

    merged_notes: list[dict[str, Any]] = []
    seen_notes: set[str] = set()
    notes_by_key: dict[str, dict[str, Any]] = {}
    admitted_scope_counts: dict[str, int] = {}

    def scope_is_allowed(note: dict[str, Any]) -> bool:
        if allowed_scopes is None:
            return True
        scope = str(note.get("scope", "")).strip()
        return scope in allowed_scopes

    def scope_has_capacity(note: dict[str, Any]) -> bool:
        if max_items_per_scope is None:
            return True
        scope = str(note.get("scope", "")).strip()
        return admitted_scope_counts.get(scope, 0) < max_items_per_scope

    def record_scope(note: dict[str, Any]) -> None:
        scope = str(note.get("scope", "")).strip()
        admitted_scope_counts[scope] = admitted_scope_counts.get(scope, 0) + 1

    for note in current.get("notes") or []:
        if not isinstance(note, dict):
            continue
        key = _note_key(note)
        if (
            not key
            or key in seen_notes
            or not scope_is_allowed(note)
            or not scope_has_capacity(note)
        ):
            continue
        seen_notes.add(key)
        merged_notes.append(note)
        notes_by_key[key] = note
        record_scope(note)

    added_notes: list[dict[str, Any]] = []
    enriched_notes_by_key: dict[str, dict[str, Any]] = {}
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        key = _note_key(note)
        if not key or not scope_is_allowed(note):
            continue
        if key in seen_notes:
            existing = notes_by_key[key]
            enriched = _enrich_xhs_access_token(existing, note)
            for field in ("published_at", "published_label"):
                if not _has_publication_value(existing.get(field)) and _has_publication_value(
                    note.get(field)
                ):
                    existing[field] = note[field]
                    enriched = True
            if enriched:
                enriched_notes_by_key[key] = dict(existing)
            continue
        if not scope_has_capacity(note):
            continue
        seen_notes.add(key)
        merged_notes.append(note)
        notes_by_key[key] = note
        added_notes.append(note)
        record_scope(note)

    if allowed_scopes is not None:
        merged_urls = []
        seen_urls = set()
        for note in merged_notes:
            note_url = note.get("url")
            if not isinstance(note_url, str) or not note_url or note_url in seen_urls:
                continue
            seen_urls.add(note_url)
            merged_urls.append(note_url)

    merged: dict[str, Any] = {"urls": merged_urls}
    if merged_notes:
        merged["notes"] = merged_notes

    merged_counts: dict[str, Any] = {}
    existing_counts = current.get("scope_counts")
    if allowed_scopes is None:
        if isinstance(existing_counts, dict):
            merged_counts.update(existing_counts)
        if isinstance(scope_counts, dict):
            for scope, count in scope_counts.items():
                current_count = merged_counts.get(scope, 0)
                if isinstance(current_count, int) and isinstance(count, int):
                    merged_counts[scope] = max(current_count, count)
                else:
                    merged_counts[scope] = count
    else:
        for reported_counts in (existing_counts, scope_counts):
            if not isinstance(reported_counts, dict):
                continue
            for scope, count in reported_counts.items():
                if (
                    not isinstance(scope, str)
                    or scope not in allowed_scopes
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                ):
                    continue
                current_count = merged_counts.get(scope, 0)
                merged_counts[scope] = max(current_count, max(0, count))
    note_counts: dict[str, int] = {}
    for note in merged_notes:
        scope = str(note.get("scope", "")).strip()
        if scope:
            note_counts[scope] = note_counts.get(scope, 0) + 1
    for scope, note_count in note_counts.items():
        reported_count = merged_counts.get(scope, 0)
        merged_counts[scope] = (
            max(reported_count, note_count) if isinstance(reported_count, int) else note_count
        )
    if max_items_per_scope is not None:
        for scope, count in list(merged_counts.items()):
            if isinstance(count, int) and not isinstance(count, bool):
                merged_counts[scope] = min(max(0, count), max_items_per_scope)
    if merged_counts:
        merged["scope_counts"] = merged_counts

    if isinstance(current.get("debug"), dict) or isinstance(debug, dict):
        merged_debug: dict[str, Any] = {}
        if isinstance(current.get("debug"), dict):
            merged_debug.update(current["debug"])
        if isinstance(debug, dict):
            merged_debug.update(debug)
        merged["debug"] = merged_debug

    return merged, added_notes, list(enriched_notes_by_key.values())


def xhs_bootstrap_notes_to_events(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert extension-collected Xiaohongshu bootstrap notes into events.

    v0.3.22+ routes through ``event_format.build_event`` so the resulting
    dict is shape-identical to B站 / future-source events. The scope-aware
    natural-language ``context`` (preserving "小红书收藏" / "小红书点赞" /
    "小红书浏览记录" wording) is built explicitly here because the scope
    label carries more nuance than the generic event_type alone.
    """
    from openbiliclaw.sources.event_format import SOURCE_XIAOHONGSHU, build_event

    events: list[dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        scope = str(note.get("scope", "")).strip()
        event_type = XHS_BOOTSTRAP_SCOPE_EVENT_TYPES.get(scope)
        if event_type is None:
            continue

        title = str(note.get("title", "")).strip()
        url = str(note.get("url", "")).strip()
        if not title and not url:
            continue

        author = str(note.get("author", "")).strip()
        label = XHS_BOOTSTRAP_SCOPE_LABELS[scope]
        # Custom context — scope label ("收藏" / "点赞" / "浏览记录") is
        # more informative than the generic event_format default
        # ("收藏了" / "点赞了" / "看了"), and the prior wording was
        # already what tests / prompts grew up reading.
        context = f"小红书{label}：{title or url}"
        if author:
            context = f"{context} 作者：{author}"

        events.append(
            build_event(
                event_type=event_type,
                source_platform=SOURCE_XIAOHONGSHU,
                title=title,
                url=url,
                author=author,
                context=context,
                metadata={
                    "note_id": str(note.get("note_id", "")).strip(),
                    "xsec_token": str(note.get("xsec_token", "")).strip(),
                    "cover_url": str(note.get("cover_url", "")).strip(),
                    "import_source": f"xhs_bootstrap_{scope}",
                    "signal_strength": XHS_BOOTSTRAP_SIGNAL_STRENGTH[scope],
                },
            )
        )
    return events


class XhsTaskQueue:
    """Manages the xhs_tasks table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS xhs_tasks (
                id           TEXT PRIMARY KEY,
                type         TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status       TEXT NOT NULL DEFAULT 'pending',
                result_json  TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                claimed_at   TIMESTAMP,
                completed_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_xhs_tasks_status
                ON xhs_tasks (status, created_at);
            CREATE TABLE IF NOT EXISTS xhs_task_runtime_state (
                singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
                next_claim_at   TIMESTAMP,
                cooldown_until  TIMESTAMP,
                cooldown_reason TEXT NOT NULL DEFAULT '',
                rate_limit_strikes INTEGER NOT NULL DEFAULT 0,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO xhs_task_runtime_state(singleton)
            VALUES (1);
        """)
        columns = {
            str(row["name"])
            for row in self._db.conn.execute("PRAGMA table_info(xhs_tasks)").fetchall()
        }
        if "claimed_at" not in columns:
            self._db.conn.execute("ALTER TABLE xhs_tasks ADD COLUMN claimed_at TIMESTAMP")
        runtime_columns = {
            str(row["name"])
            for row in self._db.conn.execute("PRAGMA table_info(xhs_task_runtime_state)").fetchall()
        }
        if "rate_limit_strikes" not in runtime_columns:
            self._db.conn.execute(
                "ALTER TABLE xhs_task_runtime_state "
                "ADD COLUMN rate_limit_strikes INTEGER NOT NULL DEFAULT 0"
            )
        self._db.conn.commit()

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        daily_budget: int = 100,
    ) -> bool:
        """Enqueue a task if the daily budget for this type allows it.

        Returns True if enqueued, False if budget exhausted.
        """
        return (
            self.enqueue_with_id(
                task_type,
                payload,
                daily_budget=daily_budget,
            )
            is not None
        )

    def enqueue_with_id(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        daily_budget: int = 100,
    ) -> str | None:
        """Enqueue a task and return its id, or None when budget is exhausted.

        ``daily_budget <= 0`` disables the per-day cap; runtime producers are
        then controlled by source deficits and their per-run throttles.
        """
        conn = self._db.conn
        participating_in_transaction = bool(conn.in_transaction)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if daily_budget > 0:
            count_today = conn.execute(
                "SELECT COUNT(*) FROM xhs_tasks WHERE type = ? AND created_at >= ?",
                (task_type, today),
            ).fetchone()[0]
        else:
            count_today = 0

        if daily_budget > 0 and count_today >= daily_budget:
            logger.info(
                "xhs task budget exhausted: type=%s used_today=%d budget=%d "
                "(per-day UTC cap from config [sources.xiaohongshu] daily_*_budget; "
                "0 = unlimited)",
                task_type,
                count_today,
                daily_budget,
            )
            return None

        task_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO xhs_tasks (id, type, payload_json) VALUES (?, ?, ?)",
            (task_id, task_type, json.dumps(payload, ensure_ascii=False)),
        )
        if not participating_in_transaction:
            conn.commit()
        return task_id

    def active_task_count(self, task_type: str) -> int:
        """Return pending and in-progress task count for one task type."""
        row = self._db.conn.execute(
            """
            SELECT COUNT(*)
            FROM xhs_tasks
            WHERE type = ? AND status IN ('pending', 'in_progress')
            """,
            (task_type,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def runtime_state(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Return persisted pacing and risk-control state for diagnostics."""
        current = _utc_now(now)
        row = self._db.conn.execute(
            """
            SELECT next_claim_at, cooldown_until, cooldown_reason,
                   rate_limit_strikes, updated_at
            FROM xhs_task_runtime_state
            WHERE singleton = ?
            """,
            (_XHS_RUNTIME_STATE_ROW_ID,),
        ).fetchone()
        if row is None:
            return {
                "rate_limited": False,
                "cooldown_remaining_seconds": 0,
                "next_claim_delay_seconds": 0,
                "cooldown_until": "",
                "next_claim_at": "",
                "cooldown_reason": "",
                "rate_limit_strikes": 0,
                "updated_at": "",
            }
        next_claim_at = _parse_sqlite_timestamp(row["next_claim_at"])
        cooldown_until = _parse_sqlite_timestamp(row["cooldown_until"])
        cooldown_remaining = _remaining_seconds(cooldown_until, current)
        return {
            "rate_limited": cooldown_remaining > 0,
            "cooldown_remaining_seconds": cooldown_remaining,
            "next_claim_delay_seconds": max(
                cooldown_remaining,
                _remaining_seconds(next_claim_at, current),
            ),
            "cooldown_until": str(row["cooldown_until"] or ""),
            "next_claim_at": str(row["next_claim_at"] or ""),
            "cooldown_reason": str(row["cooldown_reason"] or ""),
            "rate_limit_strikes": max(0, int(row["rate_limit_strikes"] or 0)),
            "updated_at": str(row["updated_at"] or ""),
        }

    def cooldown_remaining_seconds(self, *, now: datetime | None = None) -> int:
        """Return the active platform cooldown, or zero when tasks may run."""
        return int(self.runtime_state(now=now)["cooldown_remaining_seconds"])

    def record_rate_limit(
        self,
        task_id: str | None = None,
        *,
        error: str = "xhs_rate_limited",
        urls: list[str] | None = None,
        notes: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        cooldown_seconds: int = XHS_RATE_LIMIT_COOLDOWN_SECONDS,
        max_cooldown_seconds: int = XHS_RATE_LIMIT_MAX_COOLDOWN_SECONDS,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a platform-wide cooldown and optionally fail its task.

        The state lives outside an individual task so it survives backend and
        MV3 service-worker restarts. Separate risk episodes back off
        exponentially; duplicate reports inside one active cooldown share the
        same strike and can only extend, never shorten, its safety window.
        """
        current = _utc_now(now)
        base_cooldown = max(1, int(cooldown_seconds))
        max_cooldown = max(base_cooldown, int(max_cooldown_seconds))
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                """
                SELECT next_claim_at, cooldown_until, rate_limit_strikes
                FROM xhs_task_runtime_state
                WHERE singleton = ?
                """,
                (_XHS_RUNTIME_STATE_ROW_ID,),
            ).fetchone()
            existing_cooldown = (
                _parse_sqlite_timestamp(state["cooldown_until"]) if state is not None else None
            )
            existing_strikes = max(
                0,
                int(state["rate_limit_strikes"] or 0) if state is not None else 0,
            )
            if _remaining_seconds(existing_cooldown, current) > 0:
                strike_count = max(1, existing_strikes)
            else:
                strike_count = existing_strikes + 1
            exponent = min(30, max(0, strike_count - 1))
            applied_cooldown = min(max_cooldown, base_cooldown * (1 << exponent))
            requested_until = current + timedelta(seconds=applied_cooldown)
            effective_until = max(
                requested_until,
                existing_cooldown or requested_until,
            )
            existing_next_claim = (
                _parse_sqlite_timestamp(state["next_claim_at"]) if state is not None else None
            )
            effective_next_claim = max(
                effective_until,
                existing_next_claim or effective_until,
            )
            conn.execute(
                """
                INSERT INTO xhs_task_runtime_state(
                    singleton, next_claim_at, cooldown_until,
                    cooldown_reason, rate_limit_strikes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    next_claim_at = excluded.next_claim_at,
                    cooldown_until = excluded.cooldown_until,
                    cooldown_reason = excluded.cooldown_reason,
                    rate_limit_strikes = excluded.rate_limit_strikes,
                    updated_at = excluded.updated_at
                """,
                (
                    _XHS_RUNTIME_STATE_ROW_ID,
                    _format_sqlite_timestamp(effective_next_claim),
                    _format_sqlite_timestamp(effective_until),
                    str(error or "xhs_rate_limited")[:128],
                    strike_count,
                    _format_sqlite_timestamp(current),
                ),
            )

            if task_id:
                row = conn.execute(
                    "SELECT type, payload_json, status, result_json FROM xhs_tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                current_result: dict[str, Any] = {}
                if row is not None and row["result_json"]:
                    try:
                        parsed = json.loads(str(row["result_json"]))
                        if isinstance(parsed, dict):
                            current_result = parsed
                    except json.JSONDecodeError:
                        current_result = {}
                from openbiliclaw.sources.task_result_protocol import staged_terminal_status

                if row is not None and (
                    str(row["status"] or "").strip() not in {"completed", "failed"}
                    and not staged_terminal_status(current_result)
                ):
                    allowed_scopes, max_items_per_scope = _bootstrap_result_policy(
                        row["type"], row["payload_json"]
                    )
                    merged, _added, _enriched = _merge_result_payload(
                        current_result,
                        urls=urls,
                        notes=notes,
                        scope_counts=scope_counts,
                        debug=debug,
                        allowed_scopes=allowed_scopes,
                        max_items_per_scope=max_items_per_scope,
                    )
                    merged["error"] = str(error or "xhs_rate_limited")
                    merged["rate_limited"] = True
                    merged["cooldown_until"] = _format_sqlite_timestamp(effective_until)
                    merged["rate_limit_strikes"] = strike_count
                    conn.execute(
                        """
                        UPDATE xhs_tasks
                        SET status = 'failed',
                            result_json = ?,
                            completed_at = ?
                        WHERE id = ?
                        """,
                        (
                            json.dumps(merged, ensure_ascii=False),
                            _format_sqlite_timestamp(current),
                            task_id,
                        ),
                    )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        logger.warning(
            "xhs task circuit breaker opened: task_id=%s reason=%s strikes=%d "
            "cooldown_seconds=%d cooldown_until=%s",
            task_id or "-",
            str(error or "xhs_rate_limited")[:128],
            strike_count,
            applied_cooldown,
            _format_sqlite_timestamp(effective_until),
        )
        return self.runtime_state(now=current)

    def next_pending(
        self,
        only_ids: set[str] | None = None,
        *,
        min_interval_seconds: int = 0,
        jitter_ratio: float = XHS_TASK_INTERVAL_JITTER_RATIO,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Claim and return the oldest runnable task, or None.

        The extension can be installed in multiple browser profiles, and
        MV3 service workers can restart mid-task. Marking the task
        ``in_progress`` as it is handed out prevents a foreground
        bootstrap task from being opened repeatedly while one extension
        instance is already working on it. Stale in-progress tasks are
        eligible again after 15 minutes so a crashed extension does not
        permanently wedge the queue.
        """
        current = _utc_now(now)
        stale_before = _format_sqlite_timestamp(current - timedelta(minutes=15))
        # ``only_ids`` restricts which tasks may be claimed (gui-init: during an
        # active init the dispatcher is only handed init-owned bootstrap tasks,
        # so a stale pending task can't starve the run). None = no restriction.
        # A staged final still follows the normal claim lease: if the result
        # POST response is lost, stale reclaim re-enters the route and repairs
        # projections from the frozen canonical payload.
        where = "(status = 'pending' OR (status = 'in_progress' AND claimed_at <= ?))"
        params: list[Any] = [stale_before]
        if only_ids is not None:
            ids = [str(i) for i in only_ids]
            if not ids:
                return None
            where += f" AND id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = conn.execute(
                """
                SELECT next_claim_at, cooldown_until
                FROM xhs_task_runtime_state
                WHERE singleton = ?
                """,
                (_XHS_RUNTIME_STATE_ROW_ID,),
            ).fetchone()
            cooldown_until = (
                _parse_sqlite_timestamp(state["cooldown_until"]) if state is not None else None
            )
            if _remaining_seconds(cooldown_until, current) > 0:
                conn.commit()
                return None
            next_claim_at = (
                _parse_sqlite_timestamp(state["next_claim_at"]) if state is not None else None
            )
            if _remaining_seconds(next_claim_at, current) > 0:
                # Pacing applies only to automatic discovery tasks. Do not let
                # an older search row hide a later init bootstrap, which is
                # intentionally exempt from the ordinary inter-task delay.
                where += " AND type NOT IN (?, ?)"
                params.extend(sorted(_XHS_PACED_TASK_TYPES))
            row = conn.execute(
                f"SELECT * FROM xhs_tasks WHERE {where} ORDER BY created_at ASC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_type = str(row["type"] or "")
            task_id = str(row["id"])
            conn.execute(
                "UPDATE xhs_tasks SET status = 'in_progress', claimed_at = ? WHERE id = ?",
                (_format_sqlite_timestamp(current), task_id),
            )
            interval_seconds = max(0, int(min_interval_seconds))
            if task_type in _XHS_PACED_TASK_TYPES and interval_seconds > 0:
                paced_seconds = _jittered_interval_seconds(
                    interval_seconds,
                    task_id=task_id,
                    jitter_ratio=jitter_ratio,
                )
                next_claim_at = current + timedelta(seconds=paced_seconds)
                conn.execute(
                    """
                    UPDATE xhs_task_runtime_state
                    SET next_claim_at = ?, updated_at = ?
                    WHERE singleton = ?
                    """,
                    (
                        _format_sqlite_timestamp(next_claim_at),
                        _format_sqlite_timestamp(current),
                        _XHS_RUNTIME_STATE_ROW_ID,
                    ),
                )
            claimed = conn.execute("SELECT * FROM xhs_tasks WHERE id = ?", (task_id,)).fetchone()
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        return dict(claimed) if claimed is not None else None

    def find_recent_task(
        self,
        task_type: str,
        *,
        recent_hours: float,
        statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        """Return a recent task of this type for idempotent enqueue paths."""
        if recent_hours <= 0:
            return None
        selected_statuses = statuses or _RECENT_TASK_STATUSES
        if not selected_statuses:
            return None
        placeholders = ",".join("?" for _ in selected_statuses)
        cutoff = (datetime.now(UTC) - timedelta(hours=recent_hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self._db.conn.execute(
            f"""
            SELECT *
            FROM xhs_tasks
            WHERE type = ?
              AND created_at >= ?
              AND status IN ({placeholders})
            ORDER BY
              CASE
                WHEN status IN ('pending', 'in_progress') THEN 0
                WHEN status = 'completed' THEN 1
                ELSE 2
              END,
              created_at DESC
            LIMIT 1
            """,
            (task_type, cutoff, *selected_statuses),
        ).fetchone()
        return dict(row) if row is not None else None

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Return a task by id, or None."""
        row = self._db.conn.execute(
            "SELECT * FROM xhs_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _result_policy(self, task_id: str) -> tuple[frozenset[str] | None, int | None]:
        row = self._db.conn.execute(
            "SELECT type, payload_json FROM xhs_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None, None
        return _bootstrap_result_policy(row["type"], row["payload_json"])

    def complete(
        self,
        task_id: str,
        *,
        urls: list[str] | None = None,
        notes: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> None:
        """Mark a task as completed with optional result payload details."""
        allowed_scopes, max_items_per_scope = self._result_policy(task_id)
        result_payload, _added, _enriched = _merge_result_payload(
            {},
            urls=urls,
            notes=notes,
            scope_counts=scope_counts,
            debug=debug,
            allowed_scopes=allowed_scopes,
            max_items_per_scope=max_items_per_scope,
        )
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        mutated, _canonical = mutate_unstaged_result(
            self._db,
            table="xhs_tasks",
            task_id=task_id,
            mutate=lambda _current: result_payload,
            terminal_status="completed",
        )
        if mutated:
            self._reset_rate_limit_strikes_after_success(task_id)

    def merge_result(
        self,
        task_id: str,
        *,
        urls: list[str] | None = None,
        notes: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        complete: bool = False,
    ) -> list[dict[str, Any]]:
        """Merge a partial/final result payload and optionally mark complete.

        Returns only notes that were newly added by this merge.
        """
        added_notes, _enriched_notes = self.merge_result_with_enrichment(
            task_id,
            urls=urls,
            notes=notes,
            scope_counts=scope_counts,
            debug=debug,
            complete=complete,
        )
        return added_notes

    def merge_result_with_enrichment(
        self,
        task_id: str,
        *,
        urls: list[str] | None = None,
        notes: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        complete: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Merge a result and return separately added and publication-enriched notes."""
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        added_notes: list[dict[str, Any]] = []
        enriched_notes: list[dict[str, Any]] = []
        allowed_scopes, max_items_per_scope = self._result_policy(task_id)

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal added_notes, enriched_notes
            merged, added_notes, enriched_notes = _merge_result_payload(
                current,
                urls=urls,
                notes=notes,
                scope_counts=scope_counts,
                debug=debug,
                allowed_scopes=allowed_scopes,
                max_items_per_scope=max_items_per_scope,
            )
            return merged

        mutated, _canonical = mutate_unstaged_result(
            self._db,
            table="xhs_tasks",
            task_id=task_id,
            mutate=mutate,
            terminal_status="completed" if complete else None,
        )
        if complete and mutated:
            self._reset_rate_limit_strikes_after_success(task_id)
        return (added_notes, enriched_notes) if mutated else ([], [])

    def stage_final_result(
        self,
        task_id: str,
        *,
        terminal_status: str,
        urls: list[str] | None = None,
        notes: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stage the immutable canonical result before downstream projection."""
        from openbiliclaw.sources.task_result_protocol import stage_terminal_result

        allowed_scopes, max_items_per_scope = self._result_policy(task_id)

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            merged, _added, _enriched = _merge_result_payload(
                current,
                urls=urls,
                notes=notes,
                scope_counts=scope_counts,
                debug=debug,
                allowed_scopes=allowed_scopes,
                max_items_per_scope=max_items_per_scope,
            )
            return merged

        return stage_terminal_result(
            self._db,
            table="xhs_tasks",
            task_id=task_id,
            terminal_status=terminal_status,
            merge=merge,
        )

    def complete_staged_result(self, task_id: str) -> bool:
        """Mark a staged canonical result complete without replacing it."""
        from openbiliclaw.sources.task_result_protocol import complete_staged_result

        completed = complete_staged_result(self._db, table="xhs_tasks", task_id=task_id)
        if completed:
            self._reset_rate_limit_strikes_after_success(task_id)
        return completed

    def _reset_rate_limit_strikes_after_success(self, task_id: str) -> bool:
        """Clear expired risk backoff after a successful paced task."""
        current = _utc_now()
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT type, status FROM xhs_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None or (
                str(task["type"] or "") not in _XHS_PACED_TASK_TYPES
                or str(task["status"] or "") != "completed"
            ):
                conn.commit()
                return False
            state = conn.execute(
                """
                SELECT cooldown_until, rate_limit_strikes
                FROM xhs_task_runtime_state
                WHERE singleton = ?
                """,
                (_XHS_RUNTIME_STATE_ROW_ID,),
            ).fetchone()
            cooldown_until = (
                _parse_sqlite_timestamp(state["cooldown_until"]) if state is not None else None
            )
            if _remaining_seconds(cooldown_until, current) > 0:
                # A different in-flight task may have opened the breaker before
                # this success arrived. Never let that late result cancel an
                # active safety window.
                conn.commit()
                return False
            strikes = int(state["rate_limit_strikes"] or 0) if state is not None else 0
            if strikes <= 0:
                conn.commit()
                return False
            conn.execute(
                """
                UPDATE xhs_task_runtime_state
                SET cooldown_until = NULL,
                    cooldown_reason = '',
                    rate_limit_strikes = 0,
                    updated_at = ?
                WHERE singleton = ?
                """,
                (
                    _format_sqlite_timestamp(current),
                    _XHS_RUNTIME_STATE_ROW_ID,
                ),
            )
            conn.commit()
            logger.info("xhs task circuit breaker reset after successful task: %s", task_id)
            return True
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            # Completion is already durable by the time this best-effort reset
            # runs. Keeping an old strike is safer than turning a successful
            # callback into a 5xx that cannot roll the terminal task back.
            logger.exception(
                "xhs task circuit breaker reset failed after successful task: %s",
                task_id,
            )
            return False
        finally:
            conn.close()

    def fail(
        self,
        task_id: str,
        *,
        error: str = "",
        debug: dict[str, Any] | None = None,
    ) -> bool:
        """Mark a task as failed."""
        result_payload: dict[str, Any] = {"error": error}
        if debug is not None:
            result_payload["debug"] = debug
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        mutated, _canonical = mutate_unstaged_result(
            self._db,
            table="xhs_tasks",
            task_id=task_id,
            mutate=lambda _current: result_payload,
            terminal_status="failed",
        )
        return mutated


class XhsCreatorStore:
    """Manages xhs_creator_subscriptions table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS xhs_creator_subscriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id      TEXT NOT NULL UNIQUE,
                creator_url     TEXT NOT NULL,
                display_name    TEXT NOT NULL DEFAULT '',
                added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_fetched_at TIMESTAMP
            );
        """)

    def add(
        self,
        creator_id: str,
        creator_url: str,
        display_name: str,
    ) -> None:
        """Add a subscription (ignore if duplicate creator_id)."""
        self._db.conn.execute(
            "INSERT OR IGNORE INTO xhs_creator_subscriptions "
            "(creator_id, creator_url, display_name) VALUES (?, ?, ?)",
            (creator_id, creator_url, display_name),
        )
        self._db.conn.commit()

    def list_all(self) -> list[dict[str, Any]]:
        """Return all subscriptions."""
        rows = self._db.conn.execute(
            "SELECT * FROM xhs_creator_subscriptions ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, sub_id: int) -> bool:
        """Delete a subscription by primary key. Returns True if deleted."""
        cursor = self._db.conn.execute(
            "DELETE FROM xhs_creator_subscriptions WHERE id = ?",
            (sub_id,),
        )
        self._db.conn.commit()
        return cursor.rowcount > 0

    def due_for_fetch(self, *, hours: int = 24) -> list[dict[str, Any]]:
        """Return subscriptions whose last_fetched_at is older than ``hours`` ago."""
        rows = self._db.conn.execute(
            "SELECT * FROM xhs_creator_subscriptions "
            "WHERE last_fetched_at IS NULL "
            "   OR last_fetched_at < datetime('now', ?)",
            (f"-{hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_fetched(self, sub_id: int) -> None:
        """Update last_fetched_at to now."""
        self._db.conn.execute(
            "UPDATE xhs_creator_subscriptions SET last_fetched_at = CURRENT_TIMESTAMP WHERE id = ?",
            (sub_id,),
        )
        self._db.conn.commit()
