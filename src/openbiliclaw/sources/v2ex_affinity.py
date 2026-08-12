"""Small durable Node-affinity projection for V2EX bootstrap signals."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from openbiliclaw.storage.database import Database

from openbiliclaw.sources.v2ex_tasks import v2ex_bootstrap_item_key

_WEIGHTS = {
    "public_topics": 1.2,
    "public_replies": 0.8,
    "favorite_topics": 1.6,
    "favorite_nodes": 3.0,
    "engaged_view": 0.3,
}

_NODE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}", re.IGNORECASE)
_TOPIC_PATH_RE = re.compile(r"/t/(\d+)(?:/.*)?")
_ENGAGED_AFFINITY_KEY_RE = re.compile(r"engaged_view:topic:\d{1,64}")

_TRANSACTIONAL_NODES = frozenset({"deals", "free", "secondhand", "all4all"})
_TEMPORARY_NEED_NODES = frozenset(
    {"jobs", "cv", "rent", "house", "immigration", "visa", "exchange"}
)
_SELF_PROMOTION_NODES = frozenset({"promotions", "showcase"})
_SOCIAL_CHAT_NODES = frozenset({"babel", "offtopic", "random", "afterdark"})
_INTENT_DISCOUNTS = {
    "stable_interest": 1.0,
    "temporary_need": 0.55,
    "transactional": 0.45,
    "self_promotion": 0.65,
    "argument_or_correction": 0.80,
    "social_chat": 0.70,
    "unknown": 0.85,
}
_AFFINITY_HALF_LIFE_DAYS = 180.0


def v2ex_affinity_projection_username(active: object, observed: object) -> str:
    """Return the active identity only when no observed account contradicts it."""

    active_username = str(active or "").strip()
    observed_username = str(observed or "").strip()
    if not active_username:
        return ""
    if observed_username and observed_username.casefold() != active_username.casefold():
        return ""
    return active_username


def v2ex_engaged_view_affinity_item(
    event: dict[str, Any],
    *,
    event_id: int,
) -> dict[str, Any] | None:
    """Build one strict, durable Node-affinity item from a Topic dwell event.

    The event must already have a positive database receipt. A stable Topic key
    makes repeated reads of the same Topic count once, matching the affinity
    formula's distinct engaged-Topic semantics.
    """

    if event_id <= 0 or not isinstance(event, dict):
        return None
    if str(event.get("event_type") or event.get("type") or "").strip() != "click":
        return None
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return None
    if str(metadata.get("source_platform") or "").strip().casefold() != "v2ex":
        return None
    if str(metadata.get("dwell_source") or "").strip() != "content_page_exit":
        return None
    raw_watch_seconds = metadata.get("watch_seconds")
    if isinstance(raw_watch_seconds, bool) or not isinstance(raw_watch_seconds, (str, int, float)):
        return None
    try:
        watch_seconds = float(raw_watch_seconds)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(watch_seconds) or watch_seconds < 30:
        return None

    parsed = urlparse(str(event.get("url") or ""))
    hostname = str(parsed.hostname or "").casefold()
    if parsed.scheme != "https" or (hostname != "v2ex.com" and not hostname.endswith(".v2ex.com")):
        return None
    topic_match = _TOPIC_PATH_RE.fullmatch(parsed.path)
    if topic_match is None:
        return None
    topic_id = topic_match.group(1)
    metadata_topic_id = str(metadata.get("topic_id") or metadata.get("content_id") or "").strip()
    if metadata_topic_id != topic_id:
        return None
    node_name = str(metadata.get("node_name") or "").strip().lower()
    if _NODE_NAME_RE.fullmatch(node_name) is None:
        return None
    node_title = " ".join(str(metadata.get("node_title") or "").split())[:200]
    return {
        "scope": "engaged_view",
        "topic_id": topic_id,
        "node_name": node_name,
        "node_title": node_title,
        "_affinity_item_key": f"engaged_view:topic:{topic_id}",
    }


def _node_intent(node_name: str) -> str:
    key = node_name.casefold()
    if key in _TRANSACTIONAL_NODES:
        return "transactional"
    if key in _TEMPORARY_NEED_NODES:
        return "temporary_need"
    if key in _SELF_PROMOTION_NODES:
        return "self_promotion"
    if key in _SOCIAL_CHAT_NODES:
        return "social_chat"
    return "stable_interest"


def _evidence_timestamp(item: dict[str, Any], fallback: datetime) -> str:
    raw = str(item.get("published_at") or "").strip()
    if not raw:
        return fallback.isoformat()
    parsed: datetime | None = None
    try:
        if raw.replace(".", "", 1).isdigit():
            parsed = datetime.fromtimestamp(float(raw), tz=UTC)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        parsed = None
    if parsed is None:
        return fallback.isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    # A malformed future publication time must not earn negative age / extra
    # weight. Clamp it to the observation boundary.
    return min(parsed, fallback).isoformat()


def _intent_mix(raw: object, intent: str) -> dict[str, int]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    result = {
        str(key): max(0, int(value))
        for key, value in parsed.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }
    result[intent] = result.get(intent, 0) + 1
    return result


def _effective_score(row: dict[str, Any], *, now: datetime) -> float:
    raw_score = max(0.0, float(row.get("score") or 0.0))
    if raw_score <= 0:
        return 0.0
    try:
        evidence_at = datetime.fromisoformat(
            str(row.get("latest_evidence_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        evidence_at = now
    if evidence_at.tzinfo is None:
        evidence_at = evidence_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - evidence_at.astimezone(UTC)).total_seconds() / 86_400)
    decay = 0.5 ** (age_days / _AFFINITY_HALF_LIFE_DAYS)
    # An explicitly followed Node is durable preference evidence. It still
    # decays, but not below 75% until an explicit retraction removes the flag.
    if int(row.get("favorite_node") or 0) == 1:
        decay = max(0.75, decay)
    try:
        mix = json.loads(str(row.get("intent_mix_json") or "{}"))
    except json.JSONDecodeError:
        mix = {}
    weighted = 0.0
    total = 0.0
    if isinstance(mix, dict):
        for intent, count in mix.items():
            try:
                amount = max(0.0, float(count))
            except (TypeError, ValueError):
                continue
            weighted += amount * _INTENT_DISCOUNTS.get(str(intent), 0.85)
            total += amount
    intent_discount = weighted / total if total else 0.85
    return float(math.log1p(raw_score) * decay * intent_discount)


class V2EXNodeAffinityStore:
    """Persist explainable Node evidence without making it a new profile model."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.ensure_table()

    @staticmethod
    def _identity(username: object) -> tuple[str, str]:
        display = str(username or "").strip()
        return display, display.casefold() or "__default__"

    def _migrate_unscoped_tables(self) -> None:
        """Keep pre-identity preview tables as read-only legacy backups."""

        migrations = (
            ("v2ex_node_affinity", "username_key", "idx_v2ex_node_affinity_score"),
            ("v2ex_affinity_evidence", "username_key", ""),
            ("v2ex_affinity_snapshot_effects", "username_key", ""),
        )
        for table, required_column, index in migrations:
            columns = {
                str(row[1])
                for row in self.database.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns or required_column in columns:
                continue
            legacy = f"{table}_legacy"
            legacy_exists = bool(
                self.database.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (legacy,),
                ).fetchone()
            )
            if legacy_exists:
                # A partially migrated development database should fail closed
                # into a fresh identity-scoped table, while retaining both old
                # copies for manual recovery.
                legacy = f"{legacy}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            if index:
                self.database.conn.execute(f"DROP INDEX IF EXISTS {index}")
            self.database.conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        self.database.conn.commit()

    def ensure_table(self) -> None:
        self._migrate_unscoped_tables()
        self.database.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2ex_node_affinity (
                username_key TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                node_name TEXT NOT NULL,
                node_title TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                favorite_node INTEGER NOT NULL DEFAULT 0,
                favorite_topic_count INTEGER NOT NULL DEFAULT 0,
                published_topic_count INTEGER NOT NULL DEFAULT 0,
                discussion_topic_count INTEGER NOT NULL DEFAULT 0,
                engaged_view_count INTEGER NOT NULL DEFAULT 0,
                latest_evidence_at TEXT NOT NULL DEFAULT '',
                intent_mix_json TEXT NOT NULL DEFAULT '{}',
                evidence_level TEXT NOT NULL DEFAULT 'observed_secondary',
                PRIMARY KEY (username_key, node_name)
            );
            CREATE INDEX IF NOT EXISTS idx_v2ex_node_affinity_score
                ON v2ex_node_affinity (username_key, score DESC, node_name ASC);
            CREATE TABLE IF NOT EXISTS v2ex_affinity_evidence (
                username_key TEXT NOT NULL,
                item_key TEXT NOT NULL,
                node_name TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (username_key, item_key)
            );
            CREATE TABLE IF NOT EXISTS v2ex_affinity_snapshot_effects (
                effect_key TEXT PRIMARY KEY,
                username_key TEXT NOT NULL,
                node_name TEXT NOT NULL,
                scope TEXT NOT NULL,
                action TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """
        )

    def record_items(self, items: list[dict[str, Any]], *, username: str = "") -> int:
        """Upsert node evidence from canonical bootstrap rows."""

        changed = 0
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        display_username, username_key = self._identity(username)
        for item in items:
            if not isinstance(item, dict):
                continue
            scope = str(item.get("scope") or "").strip()
            node_name = str(item.get("node_name") or "").strip().lower()
            if not node_name or scope not in _WEIGHTS:
                continue
            if scope == "engaged_view":
                item_key = str(item.get("_affinity_item_key") or "").strip()
                if _ENGAGED_AFFINITY_KEY_RE.fullmatch(item_key) is None:
                    continue
            else:
                item_key = v2ex_bootstrap_item_key(item)
            if not item_key:
                continue
            evidence_insert = self.database.conn.execute(
                """
                INSERT OR IGNORE INTO v2ex_affinity_evidence
                    (username_key, item_key, node_name, scope, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username_key, item_key, node_name, scope, now),
            )
            # Task results are durably staged before profile projection. A
            # retry after a process crash must not inflate Node scores.
            if evidence_insert.rowcount != 1:
                continue
            node_title = str(item.get("node_title") or "").strip()
            existing = self.database.conn.execute(
                """
                SELECT intent_mix_json FROM v2ex_node_affinity
                WHERE username_key=? AND node_name=?
                """,
                (username_key, node_name),
            ).fetchone()
            intent = _node_intent(node_name)
            merged_intent_mix = _intent_mix(existing[0] if existing else "{}", intent)
            evidence_at = _evidence_timestamp(item, now_dt)
            self.database.conn.execute(
                """
                INSERT INTO v2ex_node_affinity (
                    username_key, username, node_name, node_title, score, favorite_node,
                    favorite_topic_count, published_topic_count,
                    discussion_topic_count, engaged_view_count,
                    latest_evidence_at, intent_mix_json, evidence_level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(username_key, node_name) DO UPDATE SET
                    username = excluded.username,
                    node_title = CASE WHEN excluded.node_title <> ''
                                      THEN excluded.node_title
                                      ELSE v2ex_node_affinity.node_title END,
                    score = v2ex_node_affinity.score + excluded.score,
                    favorite_node = MAX(v2ex_node_affinity.favorite_node, excluded.favorite_node),
                    favorite_topic_count = v2ex_node_affinity.favorite_topic_count
                        + excluded.favorite_topic_count,
                    published_topic_count = v2ex_node_affinity.published_topic_count
                        + excluded.published_topic_count,
                    discussion_topic_count = v2ex_node_affinity.discussion_topic_count
                        + excluded.discussion_topic_count,
                    engaged_view_count = v2ex_node_affinity.engaged_view_count
                        + excluded.engaged_view_count,
                    latest_evidence_at = MAX(
                        v2ex_node_affinity.latest_evidence_at,
                        excluded.latest_evidence_at
                    ),
                    intent_mix_json = excluded.intent_mix_json,
                    evidence_level = CASE
                        WHEN MAX(v2ex_node_affinity.favorite_node, excluded.favorite_node) = 1
                            THEN 'explicit'
                        WHEN v2ex_node_affinity.score + excluded.score >= 3
                            THEN 'observed_primary'
                        ELSE 'observed_secondary'
                    END
                """,
                (
                    username_key,
                    display_username,
                    node_name,
                    node_title,
                    _WEIGHTS[scope],
                    1 if scope == "favorite_nodes" else 0,
                    1 if scope == "favorite_topics" else 0,
                    1 if scope == "public_topics" else 0,
                    1 if scope == "public_replies" else 0,
                    1 if scope == "engaged_view" else 0,
                    evidence_at,
                    json.dumps(merged_intent_mix, ensure_ascii=False, sort_keys=True),
                    "explicit" if scope == "favorite_nodes" else "observed_secondary",
                ),
            )
            changed += 1
        if changed:
            self.database.conn.commit()
        return changed

    def apply_snapshot_effects(
        self,
        effects: list[dict[str, Any]],
        *,
        username: str = "",
    ) -> int:
        """Project favorite retractions/restores into current Node affinity.

        The effect key is the idempotency boundary: event ingress and this
        projection can be replayed independently after a process crash without
        decrementing or restoring a Node twice.
        """

        changed = 0
        now = datetime.now(UTC).isoformat()
        _display_username, username_key = self._identity(username)
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            effect_key = str(effect.get("effect_key") or "").strip()
            scope = str(effect.get("scope") or "").strip()
            action = str(effect.get("action") or "").strip()
            item = effect.get("item")
            if (
                not effect_key
                or scope not in {"favorite_topics", "favorite_nodes"}
                or action not in {"retract", "restore"}
                or not isinstance(item, dict)
            ):
                continue
            node_name = str(item.get("node_name") or "").strip().lower()
            if not node_name:
                continue
            inserted = self.database.conn.execute(
                """
                INSERT OR IGNORE INTO v2ex_affinity_snapshot_effects (
                    effect_key, username_key, node_name, scope, action, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (effect_key, username_key, node_name, scope, action, now),
            )
            if inserted.rowcount != 1:
                continue
            direction = -1 if action == "retract" else 1
            favorite_node_delta = direction if scope == "favorite_nodes" else 0
            favorite_topic_delta = direction if scope == "favorite_topics" else 0
            score_delta = direction * _WEIGHTS[scope]
            self.database.conn.execute(
                """
                UPDATE v2ex_node_affinity
                SET score = MAX(0, score + ?),
                    favorite_node = MIN(1, MAX(0, favorite_node + ?)),
                    favorite_topic_count = MAX(0, favorite_topic_count + ?),
                    latest_evidence_at = ?,
                    evidence_level = CASE
                        WHEN MIN(1, MAX(0, favorite_node + ?)) = 1 THEN 'explicit'
                        WHEN MAX(0, score + ?) >= 3 THEN 'observed_primary'
                        ELSE 'observed_secondary'
                    END
                WHERE username_key = ? AND node_name = ?
                """,
                (
                    score_delta,
                    favorite_node_delta,
                    favorite_topic_delta,
                    now,
                    favorite_node_delta,
                    score_delta,
                    username_key,
                    node_name,
                ),
            )
            changed += 1
        if changed:
            self.database.conn.commit()
        return changed

    def top_nodes(self, *, limit: int = 12, username: str = "") -> list[str]:
        return [
            str(row.get("node_name") or "").strip()
            for row in self.scores(limit=limit, username=username)
            if str(row.get("node_name") or "").strip()
        ]

    def scores(self, *, limit: int = 12, username: str = "") -> list[dict[str, Any]]:
        _display_username, username_key = self._identity(username)
        rows = self.database.conn.execute(
            """
            SELECT * FROM v2ex_node_affinity
            WHERE username_key=?
            ORDER BY score DESC, node_name ASC
            """,
            (username_key,),
        ).fetchall()
        now = datetime.now(UTC)
        scored = [dict(row) for row in rows]
        for row in scored:
            row["effective_score"] = _effective_score(row, now=now)
        scored.sort(
            key=lambda row: (
                -float(row.get("effective_score") or 0.0),
                str(row.get("node_name") or ""),
            )
        )
        return scored[: max(1, int(limit))]

    def clear(self) -> None:
        self.database.conn.execute("DELETE FROM v2ex_node_affinity")
        self.database.conn.execute("DELETE FROM v2ex_affinity_evidence")
        self.database.conn.execute("DELETE FROM v2ex_affinity_snapshot_effects")
        self.database.conn.commit()


__all__ = [
    "V2EXNodeAffinityStore",
    "v2ex_affinity_projection_username",
    "v2ex_engaged_view_affinity_item",
]
