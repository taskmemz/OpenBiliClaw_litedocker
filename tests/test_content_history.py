"""Bounded content-history storage, API, and browser-surface contracts."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from openbiliclaw.api.app import (
    _decode_content_history_cursor,
    _encode_content_history_cursor,
    create_app,
)
from openbiliclaw.saved_sync.models import SavedItemInput
from openbiliclaw.storage.database import CONTENT_HISTORY_RETENTION_DAYS, Database


def _cursor_payload(token: str) -> dict[str, Any]:
    padding = "=" * (-len(token) % 4)
    return json.loads(base64.urlsafe_b64decode(token + padding))


def _cursor_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "content-history.db")
    database.initialize()
    return database


def _recommendation(database: Database, content_id: str, title: str) -> int:
    database.cache_content(
        content_id,
        title=title,
        up_name="历史测试作者",
        cover_url=f"https://example.com/{content_id}.jpg",
        content_url=f"https://www.bilibili.com/video/{content_id}",
        source_platform="bilibili",
        content_id=content_id,
        content_type="video",
        relevance_score=0.9,
    )
    return database.insert_recommendation(
        content_id,
        confidence=0.9,
        expression="历史测试",
        topic="测试",
    )


def _click(database: Database, recommendation_id: int, content_id: str) -> int:
    return database.insert_event(
        "click",
        title=f"点开 {content_id}",
        url=f"https://www.bilibili.com/video/{content_id}",
        metadata={
            "source": "recommendation_click",
            "event_namespace": "recommendation",
            "recommendation_id": recommendation_id,
            "source_platform": "bilibili",
            "content_id": content_id,
            "bvid": content_id,
        },
    )


def test_history_projects_clicks_shown_cards_and_feedback_removals(tmp_path: Path) -> None:
    database = _database(tmp_path)
    clicked_id = _recommendation(database, "BV1CLICKED", "主动点开的内容")
    shown_id = _recommendation(database, "BV1SHOWN", "只出现过的内容")
    _click(database, clicked_id, "BV1CLICKED")
    # Repeated clicks must not flood the history with duplicate cards.
    _click(database, clicked_id, "BV1CLICKED")

    clicked, clicked_total = database.list_content_history("clicked")
    shown, shown_total = database.list_content_history("shown")

    assert clicked_total == 1
    assert clicked[0]["item_key"] == "bilibili:BV1CLICKED"
    assert clicked[0]["recommendation_id"] == clicked_id
    assert clicked[0]["cover_url"].endswith("BV1CLICKED.jpg")
    assert shown_total == 1
    assert shown[0]["recommendation_id"] == shown_id
    assert shown[0]["item_key"] == "bilibili:BV1SHOWN"

    database.update_recommendation_feedback(shown_id, feedback_type="dismiss")
    shown_after_feedback, shown_after_feedback_total = database.list_content_history("shown")
    removed, removed_total = database.list_content_history("removed")

    assert shown_after_feedback == []
    assert shown_after_feedback_total == 0
    assert removed_total == 1
    assert removed[0]["item_key"] == "bilibili:BV1SHOWN"
    assert removed[0]["context"] == "dismiss"


def test_legacy_click_without_platform_uses_bilibili_canonical_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    recommendation_id = _recommendation(database, "BV1LEGACY", "旧版点击事件")
    database.insert_event(
        "click",
        title="旧版点开",
        url="https://www.bilibili.com/video/BV1LEGACY",
        metadata={
            "source": "recommendation_click",
            "event_namespace": "recommendation",
            # Legacy clients supplied neither source_platform nor a
            # recommendation id, so canonical identity is the only join.
            "content_id": "BV1LEGACY",
        },
    )

    clicked, clicked_total = database.list_content_history("clicked")
    shown, shown_total = database.list_content_history("shown")

    assert clicked_total == 1
    assert clicked[0]["item_key"] == "bilibili:BV1LEGACY"
    assert clicked[0]["source_platform"] == "bilibili"
    assert clicked[0]["recommendation_id"] is None
    assert shown == []
    assert shown_total == 0
    assert recommendation_id > 0


def test_saved_removal_keeps_snapshot_and_reports_restored_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    item = SavedItemInput(
        source_platform="youtube",
        content_id="restore-me",
        content_url="https://www.youtube.com/watch?v=restore-me",
        title="可以恢复的收藏",
        author_name="History Channel",
        cover_url="https://example.com/restore-me.jpg",
    )
    database.upsert_saved_membership("favorite", item)

    assert database.remove_saved_membership("favorite", item.item_key) is True
    removed, total = database.list_content_history("removed")

    assert total == 1
    assert removed[0] == {
        "item_key": "youtube:restore-me",
        "source_platform": "youtube",
        "content_id": "restore-me",
        "content_url": "https://www.youtube.com/watch?v=restore-me",
        "content_type": "video",
        "title": "可以恢复的收藏",
        "author_name": "History Channel",
        "cover_url": "https://example.com/restore-me.jpg",
        "body_text": "",
        "recommendation_id": None,
        "occurred_at": removed[0]["occurred_at"],
        "context": "favorite",
        "restored": 0,
        "contexts": [
            {
                "context": "favorite",
                "occurred_at": removed[0]["occurred_at"],
                "restored": False,
            }
        ],
    }

    database.upsert_saved_membership("favorite", item)
    restored, _ = database.list_content_history("removed")
    assert restored[0]["restored"] == 1
    assert restored[0]["contexts"][0]["restored"] is True


def test_saved_removal_history_is_pruned_after_retention_window(tmp_path: Path) -> None:
    database = _database(tmp_path)
    item = SavedItemInput(source_platform="bilibili", content_id="BV1OLD")
    database.upsert_saved_membership("watch_later", item)
    database.remove_saved_membership("watch_later", item.item_key)
    database.conn.execute("UPDATE saved_item_removals SET removed_at = datetime('now', '-31 days')")
    database.conn.commit()

    rows, total = database.list_content_history("removed")
    pruned = database.prune_content_history()

    assert rows == []
    assert total == 0
    assert pruned == 1
    assert database.conn.execute("SELECT COUNT(*) FROM saved_item_removals").fetchone()[0] == 0


def test_removed_history_has_stable_cross_source_tie_breaker(tmp_path: Path) -> None:
    database = _database(tmp_path)
    recommendation_id = _recommendation(database, "BV1DISMISS", "被移除的推荐")
    database.update_recommendation_feedback(recommendation_id, feedback_type="dismiss")
    item = SavedItemInput(
        source_platform="youtube",
        content_id="saved-tie",
        content_url="https://www.youtube.com/watch?v=saved-tie",
        title="被移除的收藏",
    )
    database.upsert_saved_membership("favorite", item)
    database.remove_saved_membership("favorite", item.item_key)
    tied_at = str(database.conn.execute("SELECT datetime('now', '-1 minute')").fetchone()[0])
    database.conn.execute(
        "UPDATE recommendations SET feedback_at = ? WHERE id = ?",
        (tied_at, recommendation_id),
    )
    database.conn.execute("UPDATE saved_item_removals SET removed_at = ?", (tied_at,))
    database.conn.commit()

    first, first_total = database.list_content_history("removed")
    second, second_total = database.list_content_history("removed")

    assert first_total == second_total == 2
    assert [row["context"] for row in first] == ["favorite", "dismiss"]
    assert [row["item_key"] for row in first] == [row["item_key"] for row in second]


def test_history_rejects_unknown_category(tmp_path: Path) -> None:
    database = _database(tmp_path)

    try:
        database.list_content_history("everything")
    except ValueError as error:
        assert "unsupported content history category" in str(error)
    else:  # pragma: no cover - the assertion above is the intended path
        raise AssertionError("unknown history category should fail")


def test_content_history_api_paginates_and_normalizes_fallback_url() -> None:
    class FakeDatabase:
        def __init__(self) -> None:
            self.call: tuple[str, int, int, int] | None = None

        def list_content_history_page(
            self,
            category: str,
            *,
            limit: int,
            offset: int,
            cursor: tuple[str, int, int, str, int, int, int] | None,
            retention_days: int,
        ) -> tuple[
            list[dict[str, Any]],
            int,
            bool,
            tuple[str, int, int, str, int, int, int] | None,
        ]:
            self.call = (category, limit, offset, retention_days)
            return (
                [
                    {
                        "item_key": "bilibili:BV1API",
                        "source_platform": "bilibili",
                        "content_id": "BV1API",
                        "title": "API 历史",
                        "cover_url": "//i0.hdslb.com/bfs/archive/history.jpg",
                        "recommendation_id": 9,
                        "occurred_at": "2026-08-09 12:00:00",
                    }
                ],
                17,
                False,
                None,
            )

    database = FakeDatabase()
    client = TestClient(create_app(database=database))

    response = client.get("/api/content-history?category=clicked&limit=7&offset=14")

    assert response.status_code == 200
    assert database.call == ("clicked", 7, 14, CONTENT_HISTORY_RETENTION_DAYS)
    assert response.json() == {
        "category": "clicked",
        "items": [
            {
                "item_key": "bilibili:BV1API",
                "source_platform": "bilibili",
                "content_id": "BV1API",
                "content_url": "https://www.bilibili.com/video/BV1API",
                "content_type": "video",
                "title": "API 历史",
                "author_name": "",
                "cover_url": "https://i0.hdslb.com/bfs/archive/history.jpg",
                "body_text": "",
                "recommendation_id": 9,
                "occurred_at": "2026-08-09 12:00:00",
                "context": "",
                "restored": False,
                "contexts": [],
            }
        ],
        "total": 17,
        "retention_days": 30,
        "next_cursor": None,
        "has_more": False,
    }
    assert client.get("/api/content-history?category=everything").status_code == 422


def test_content_history_api_only_returns_safe_absolute_http_urls() -> None:
    class FakeDatabase:
        def list_content_history_page(
            self,
            category: str,
            *,
            limit: int,
            offset: int,
            cursor: tuple[str, int, int, str, int, int, int] | None,
            retention_days: int,
        ) -> tuple[
            list[dict[str, Any]],
            int,
            bool,
            tuple[str, int, int, str, int, int, int] | None,
        ]:
            return (
                [
                    {
                        "item_key": "bilibili:BV1RELATIVE",
                        "source_platform": "bilibili",
                        "content_id": "BV1RELATIVE",
                        "content_url": "//www.bilibili.com/video/BV1RELATIVE",
                        "cover_url": "//i0.hdslb.com/relative.jpg",
                    },
                    {
                        "item_key": "bilibili:BV1SCRIPT",
                        "source_platform": "bilibili",
                        "content_id": "BV1SCRIPT",
                        "content_url": "javascript:alert(1)",
                        "cover_url": "data:image/svg+xml,<svg onload=alert(1)></svg>",
                    },
                    {
                        "item_key": "zhihu:question:42",
                        "source_platform": "zhihu",
                        "content_id": "question:42",
                        "content_url": "file:///etc/passwd",
                        "cover_url": "https://user:secret@example.com/private.jpg",
                    },
                ],
                3,
                False,
                None,
            )

    response = TestClient(create_app(database=FakeDatabase())).get(
        "/api/content-history?category=clicked"
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["content_url"] == "https://www.bilibili.com/video/BV1RELATIVE"
    assert items[0]["cover_url"] == "https://i0.hdslb.com/relative.jpg"
    assert items[1]["content_url"] == "https://www.bilibili.com/video/BV1SCRIPT"
    assert items[1]["cover_url"] == ""
    assert items[2]["content_url"] == ""
    assert items[2]["cover_url"] == ""


def test_content_history_api_rejects_offset_outside_sqlite_integer_range() -> None:
    class FakeDatabase:
        def list_content_history(self, *_args: object, **_kwargs: object) -> tuple[list[Any], int]:
            raise AssertionError("invalid offset must be rejected before storage is called")

    client = TestClient(create_app(database=FakeDatabase()))

    assert client.get(f"/api/content-history?category=shown&offset={1 << 63}").status_code == 422


def test_history_total_and_page_share_one_sqlite_read_statement(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _recommendation(database, "BV1SNAPSHOT", "同一快照")
    statements: list[str] = []
    database.conn.set_trace_callback(statements.append)
    try:
        rows, total = database.list_content_history("shown")
    finally:
        database.conn.set_trace_callback(None)

    page_statements = [
        statement
        for statement in statements
        if "(SELECT COUNT(*) FROM entries) AS history_total" in statement
    ]
    assert total == len(rows) == 1
    assert len(page_statements) == 1
    assert (
        "LEFT JOIN content_cache AS c ON c.bvid = eligible.hydration_bvid" in (page_statements[0])
    )
    assert " OR (" not in page_statements[0].split("eligible AS MATERIALIZED", 1)[1]


def test_shown_history_legacy_hydration_is_indexed_at_scale(tmp_path: Path) -> None:
    database = _database(tmp_path)
    row_count = 3_000
    database.conn.executemany(
        """
        INSERT INTO content_cache (
            bvid, item_key, title, source_platform, content_id, content_url, last_scored_at
        ) VALUES (?, ?, ?, 'bilibili', ?, ?, CURRENT_TIMESTAMP)
        """,
        [
            (
                f"legacy-storage-{index}",
                f"bilibili:BV1SCALE{index}",
                f"规模历史 {index}",
                f"BV1SCALE{index}",
                f"https://www.bilibili.com/video/BV1SCALE{index}",
            )
            for index in range(row_count)
        ],
    )
    database.conn.executemany(
        """
        INSERT INTO recommendations (bvid, item_key, expression, topic, confidence)
        VALUES (?, '', '旧版推荐', '性能', 0.9)
        """,
        [(f"BV1SCALE{index}",) for index in range(row_count)],
    )
    database.conn.commit()
    statements: list[str] = []
    database.conn.set_trace_callback(statements.append)
    try:
        rows, total = database.list_content_history("shown")
    finally:
        database.conn.set_trace_callback(None)

    assert total == row_count
    assert len(rows) == 12
    page_sql = next(
        statement
        for statement in statements
        if "(SELECT COUNT(*) FROM entries) AS history_total" in statement
    )
    plan = database.conn.execute(f"EXPLAIN QUERY PLAN {page_sql}").fetchall()
    details = [str(row["detail"]) for row in plan]
    assert any("idx_content_cache_content_id" in detail for detail in details)
    assert not any("SCAN c LEFT-JOIN" in detail for detail in details)


def test_shown_click_exclusion_materializes_non_correlated_lookup_sets(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    row_count = 1_200
    database.conn.executemany(
        """
        INSERT INTO recommendations (bvid, item_key, expression, topic, confidence)
        VALUES (?, ?, 'scale', 'click exclusion', 0.9)
        """,
        [(f"BV1CLICKPERF{index}", f"bilibili:BV1CLICKPERF{index}") for index in range(row_count)],
    )
    database.conn.executemany(
        "INSERT INTO events (event_type, metadata) VALUES ('click', ?)",
        [
            (
                json.dumps(
                    {
                        "source": "recommendation_click",
                        "recommendation_id": index,
                        "source_platform": "bilibili",
                        "content_id": f"BV1CLICKPERF{index - 1}",
                    }
                ),
            )
            for index in range(1, row_count + 1, 2)
        ],
    )
    database.conn.commit()
    statements: list[str] = []
    database.conn.set_trace_callback(statements.append)
    started = time.perf_counter()
    try:
        rows, total = database.list_content_history("shown")
    finally:
        elapsed = time.perf_counter() - started
        database.conn.set_trace_callback(None)

    assert total == row_count // 2
    assert len(rows) == 12
    assert elapsed < 2.0
    page_sql = next(
        statement
        for statement in statements
        if "(SELECT COUNT(*) FROM entries) AS history_total" in statement
    )
    plan = database.conn.execute(f"EXPLAIN QUERY PLAN {page_sql}").fetchall()
    details = [str(row["detail"]) for row in plan]
    assert sum("LIST SUBQUERY" in detail for detail in details) >= 2
    assert "SCAN click" not in details


def test_removed_restore_lookup_normalizes_memberships_once_at_scale(tmp_path: Path) -> None:
    database = _database(tmp_path)
    row_count = 1_000
    database.conn.executemany(
        """
        INSERT INTO saved_items (item_key, source_platform, content_id)
        VALUES (?, 'bilibili', ?)
        """,
        [(f"bilibili:member-{index}", f"member-{index}") for index in range(row_count)],
    )
    database.conn.executemany(
        """
        INSERT INTO saved_memberships (list_kind, item_key)
        VALUES ('favorite', ?)
        """,
        [(f"bilibili:member-{index}",) for index in range(row_count)],
    )
    database.conn.executemany(
        """
        INSERT INTO saved_item_removals (
            list_kind, item_key, source_platform, content_id, title
        ) VALUES ('favorite', ?, 'bilibili', ?, ?)
        """,
        [
            (
                f"bilibili:removed-{index}",
                f"removed-{index}",
                f"removed {index}",
            )
            for index in range(row_count)
        ],
    )
    database.conn.commit()
    normalization_calls = 0

    def counted_normalize(value: object) -> str:
        nonlocal normalization_calls
        normalization_calls += 1
        return str(value or "")

    database.conn.create_function(
        "normalize_content_history_item_key",
        1,
        counted_normalize,
        deterministic=True,
    )

    rows, total = database.list_content_history("removed", limit=12)

    assert total == row_count
    assert len(rows) == 12
    assert all(not bool(row["restored"]) for row in rows)
    assert normalization_calls < row_count * 10


def test_invalid_removed_identity_does_not_match_another_invalid_membership(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.conn.execute(
        """
        INSERT INTO saved_items (item_key, source_platform, content_id)
        VALUES ('invalid membership', 'bilibili', 'member')
        """
    )
    database.conn.execute(
        """
        INSERT INTO saved_memberships (list_kind, item_key)
        VALUES ('favorite', 'invalid membership')
        """
    )
    database.conn.execute(
        """
        INSERT INTO saved_item_removals (
            list_kind, item_key, source_platform, content_id, title
        ) VALUES ('favorite', 'different invalid removal', '', '', 'invalid removal')
        """
    )
    database.conn.commit()

    rows, total = database.list_content_history("removed", limit=12)

    assert total == 1
    assert not bool(rows[0]["restored"])


def test_removed_restore_uses_resolved_alias_identity_for_blank_legacy_key(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    database.conn.execute(
        """
        INSERT INTO saved_items (item_key, source_platform, content_id)
        VALUES ('twitter:abc', 'twitter', 'abc')
        """
    )
    database.conn.execute(
        """
        INSERT INTO saved_memberships (list_kind, item_key)
        VALUES ('favorite', 'twitter:abc')
        """
    )
    database.conn.execute(
        """
        INSERT INTO saved_item_removals (
            list_kind, item_key, source_platform, content_id, title
        ) VALUES ('favorite', '', 'x', 'abc', 'legacy alias removal')
        """
    )
    database.conn.commit()

    rows, total = database.list_content_history("removed", limit=12)

    assert total == 1
    assert rows[0]["item_key"] == "twitter:abc"
    assert rows[0]["source_platform"] == "twitter"
    assert bool(rows[0]["restored"])


def test_cursor_walk_returns_every_card_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    for index in range(25):
        _recommendation(database, f"BV1CURSOR{index:02d}", f"游标历史 {index}")
    client = TestClient(create_app(database=database))

    cursor: str | None = None
    page_sizes: list[int] = []
    item_keys: list[str] = []
    totals: list[int] = []
    while True:
        query = "/api/content-history?category=shown&limit=12"
        if cursor is not None:
            query += f"&cursor={cursor}"
        payload = client.get(query).json()
        page_sizes.append(len(payload["items"]))
        item_keys.extend(item["item_key"] for item in payload["items"])
        totals.append(payload["total"])
        cursor = payload["next_cursor"]
        if not payload["has_more"]:
            break

    assert page_sizes == [12, 12, 1]
    assert totals == [25, 25, 25]
    assert len(item_keys) == len(set(item_keys)) == 25
    assert cursor is None


def test_cursor_avoids_constant_total_head_insert_tail_delete_drift(tmp_path: Path) -> None:
    database = _database(tmp_path)
    recommendation_ids = [
        _recommendation(database, f"BV1MUTATE{index}", f"变更历史 {index}") for index in range(1, 6)
    ]
    database.conn.execute("UPDATE recommendations SET created_at = datetime('now', '-2 minutes')")
    database.conn.commit()
    client = TestClient(create_app(database=database))

    first = client.get("/api/content-history?category=shown&limit=2").json()
    assert first["has_more"] is True
    assert first["next_cursor"]
    first_keys = [item["item_key"] for item in first["items"]]

    inserted_id = _recommendation(database, "BV1MUTATENEW", "新头部")
    database.conn.execute(
        "UPDATE recommendations SET created_at = datetime('now', '-1 minute') WHERE id = ?",
        (inserted_id,),
    )
    database.conn.execute("DELETE FROM recommendations WHERE id = ?", (recommendation_ids[0],))
    database.conn.commit()
    _current_rows, current_total = database.list_content_history("shown", limit=20)
    assert current_total == 5

    second = client.get(
        f"/api/content-history?category=shown&limit=2&cursor={first['next_cursor']}"
    ).json()
    second_keys = [item["item_key"] for item in second["items"]]

    assert first_keys == ["bilibili:BV1MUTATE5", "bilibili:BV1MUTATE4"]
    assert second_keys == ["bilibili:BV1MUTATE3", "bilibili:BV1MUTATE2"]
    assert not set(first_keys) & set(second_keys)
    assert "bilibili:BV1MUTATENEW" not in second_keys
    assert second["has_more"] is False
    assert second["next_cursor"] is None


def test_content_history_cursor_is_strictly_validated_and_category_bound(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    _recommendation(database, "BV1CURSORONE", "游标一")
    _recommendation(database, "BV1CURSORTWO", "游标二")
    client = TestClient(create_app(database=database))
    first = client.get("/api/content-history?category=shown&limit=1").json()
    valid_cursor = first["next_cursor"]
    assert isinstance(valid_cursor, str)
    assert (
        client.get(
            f"/api/content-history?category=removed&limit=1&cursor={valid_cursor}"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/content-history?category=shown&limit=1&offset=1&cursor={valid_cursor}"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/content-history?category=shown&limit=1&offset=0&cursor={valid_cursor}"
        ).status_code
        == 422
    )
    assert client.get("/api/content-history?category=shown&cursor=").status_code == 422
    assert client.get("/api/content-history?category=shown&cursor=not_json").status_code == 422

    payload = _cursor_payload(valid_cursor)
    invalid_payloads: list[dict[str, Any]] = []
    for mutate in (
        lambda value: value.update(v=99),
        lambda value: value.update(anchors=[True, 1, 1]),
        lambda value: value.update(anchors=[1 << 63, 1, 1]),
        lambda value: value.update(after=[value["after"][0], 3, 1, "bilibili:x"]),
        lambda value: value.update(after=[value["after"][0], 0, -1, "bilibili:x"]),
        lambda value: value.update(after=[value["after"][0], 0, 1, "bad\nkey"]),
    ):
        candidate = json.loads(json.dumps(payload))
        mutate(candidate)
        invalid_payloads.append(candidate)
    for invalid_payload in invalid_payloads:
        response = client.get(
            "/api/content-history?category=shown&cursor=" + _cursor_token(invalid_payload)
        )
        assert response.status_code == 422


def test_content_history_cursor_round_trips_maximum_unicode_item_key() -> None:
    item_key = "x:" + ("😀" * 2046)
    position = ("2026-08-09 12:00:00", 0, 1, item_key, 2, 3, 4)

    token = _encode_content_history_cursor("clicked", position)

    assert len(token) < 16_384
    assert _decode_content_history_cursor(token, category="clicked") == position


def test_server_generated_cursor_round_trips_untrusted_click_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "source_platform": "twitter",
            "content_id": "good-key",
        },
    )
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "source_platform": "twitter",
            "content_id": "bad key\nwith-control",
        },
    )
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "source_platform": "bilibili",
            "content_id": "otherwise-good",
        },
    )
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "source_platform": "bad platform\n",
            "content_id": "otherwise-good",
        },
    )
    long_suffix = "😀" * 2046
    long_recommendation_id = database.insert_recommendation(
        f"twitter:{long_suffix}",
        item_key=f"x:{long_suffix}",
        confidence=0.9,
        expression="canonical 前缀扩展越界",
        topic="cursor",
    )
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "recommendation_id": long_recommendation_id,
            "source_platform": "x",
        },
    )
    client = TestClient(create_app(database=database))

    cursor: str | None = None
    item_keys: list[str] = []
    while True:
        url = "/api/content-history?category=clicked&limit=1"
        if cursor is not None:
            url += f"&cursor={cursor}"
        response = client.get(url)
        assert response.status_code == 200
        payload = response.json()
        item_keys.extend(item["item_key"] for item in payload["items"])
        cursor = payload["next_cursor"]
        if not payload["has_more"]:
            break

    assert len(item_keys) == 5
    assert "twitter:good-key" in item_keys
    assert "bilibili:otherwise-good" in item_keys
    assert any(item_key.startswith("twitter:event-") for item_key in item_keys)
    assert any(item_key.startswith("unknown:event-") for item_key in item_keys)
    assert all(not any(character.isspace() for character in item_key) for item_key in item_keys)
    assert all(len(item_key) <= 2048 for item_key in item_keys)


def test_clicks_without_raw_identity_keep_unique_surrogates_without_fake_urls(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    for _index in range(2):
        database.insert_event(
            "click",
            metadata={
                "source": "recommendation_click",
                "source_platform": "youtube",
            },
        )
    client = TestClient(create_app(database=database))

    response = client.get("/api/content-history?category=clicked&limit=20")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert len({item["item_key"] for item in payload["items"]}) == 2
    assert all(item["item_key"].startswith("youtube:event-") for item in payload["items"])
    assert all(item["content_id"] == "" for item in payload["items"])
    assert all(item["content_url"] == "" for item in payload["items"])


def test_alias_clicks_share_canonical_identity_with_legacy_recommendations(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    expected: dict[str, str] = {}
    for alias, canonical in (("x", "twitter"), ("yt", "youtube"), ("xhs", "xiaohongshu")):
        content_id = f"alias-{alias}"
        storage_key = f"legacy-{alias}-storage"
        database.cache_content(
            storage_key,
            item_key=f"{alias}:{content_id}",
            title=f"{alias} legacy title",
            content_url=f"https://example.com/{alias}/{content_id}",
            source_platform=alias,
            content_id=content_id,
            content_type="video",
        )
        database.conn.execute(
            """
            INSERT INTO recommendations (bvid, item_key, expression, topic, confidence)
            VALUES (?, ?, 'alias', 'alias', 0.9)
            """,
            (storage_key, f"{alias}:{content_id}"),
        )
        database.conn.commit()
        database.insert_event(
            "click",
            title=f"clicked {alias}",
            metadata={
                "source": "recommendation_click",
                "event_namespace": "recommendation",
                "source_platform": canonical,
                "content_id": content_id,
            },
        )
        expected[f"{canonical}:{content_id}"] = f"{alias} legacy title"

    database.insert_event(
        "click",
        title="无缓存 X 点击",
        metadata={
            "source": "recommendation_click",
            "event_namespace": "recommendation",
            "source_platform": "x",
            "content_id": "orphan-x",
        },
    )
    clicked, clicked_total = database.list_content_history("clicked", limit=20)
    shown, shown_total = database.list_content_history("shown", limit=20)
    clicked_by_key = {row["item_key"]: row for row in clicked}

    assert clicked_total == 4
    assert shown == []
    assert shown_total == 0
    for item_key, title in expected.items():
        assert clicked_by_key[item_key]["title"] == title
        assert clicked_by_key[item_key]["source_platform"] == item_key.split(":", 1)[0]
        assert clicked_by_key[item_key]["content_url"].startswith("https://example.com/")
    assert clicked_by_key["twitter:orphan-x"]["source_platform"] == "twitter"


def test_no_cache_alias_recommendation_recovers_raw_content_id_and_url(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    shown_id = database.insert_recommendation(
        "twitter:no-cache-shown",
        item_key="x:no-cache-shown",
        confidence=0.9,
        expression="无缓存 alias",
        topic="测试",
    )
    clicked_id = database.insert_recommendation(
        "youtube:no-cache-clicked",
        item_key="yt:no-cache-clicked",
        confidence=0.9,
        expression="无缓存点击",
        topic="测试",
    )
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "recommendation_id": clicked_id,
            "source_platform": "yt",
        },
    )
    blank_bilibili_cursor = database.conn.execute(
        """
        INSERT INTO recommendations (bvid, item_key, expression, topic, confidence)
        VALUES ('BV1RAWLEGACY', '', 'legacy blank key', '测试', 0.9)
        """
    )
    blank_bilibili_id = int(blank_bilibili_cursor.lastrowid or 0)
    blank_twitter_cursor = database.conn.execute(
        """
        INSERT INTO recommendations (bvid, item_key, expression, topic, confidence)
        VALUES ('twitter:blank-twitter', '', 'legacy namespaced key', '测试', 0.9)
        """
    )
    blank_twitter_id = int(blank_twitter_cursor.lastrowid or 0)
    database.conn.commit()
    database.insert_event(
        "click",
        metadata={
            "source": "recommendation_click",
            "recommendation_id": blank_bilibili_id,
        },
    )
    client = TestClient(create_app(database=database))

    shown = client.get("/api/content-history?category=shown&limit=20").json()
    shown_item = next(item for item in shown["items"] if item["recommendation_id"] == shown_id)
    assert shown_item["item_key"] == "twitter:no-cache-shown"
    assert shown_item["source_platform"] == "twitter"
    assert shown_item["content_id"] == "no-cache-shown"
    assert shown_item["content_url"] == "https://x.com/i/status/no-cache-shown"
    blank_twitter_item = next(
        item for item in shown["items"] if item["recommendation_id"] == blank_twitter_id
    )
    assert blank_twitter_item["item_key"] == "twitter:blank-twitter"
    assert blank_twitter_item["content_id"] == "blank-twitter"
    assert blank_twitter_item["content_url"] == "https://x.com/i/status/blank-twitter"

    clicked = client.get("/api/content-history?category=clicked&limit=20").json()
    clicked_by_recommendation = {item["recommendation_id"]: item for item in clicked["items"]}
    assert clicked_by_recommendation[clicked_id]["item_key"] == "youtube:no-cache-clicked"
    assert clicked_by_recommendation[clicked_id]["source_platform"] == "youtube"
    assert clicked_by_recommendation[clicked_id]["content_id"] == "no-cache-clicked"
    assert clicked_by_recommendation[clicked_id]["content_url"] == (
        "https://www.youtube.com/watch?v=no-cache-clicked"
    )
    assert clicked_by_recommendation[blank_bilibili_id]["item_key"] == ("bilibili:BV1RAWLEGACY")
    assert clicked_by_recommendation[blank_bilibili_id]["content_id"] == "BV1RAWLEGACY"
    assert clicked_by_recommendation[blank_bilibili_id]["content_url"] == (
        "https://www.bilibili.com/video/BV1RAWLEGACY"
    )

    database.update_recommendation_feedback(shown_id, feedback_type="dismiss")
    database.update_recommendation_feedback(blank_twitter_id, feedback_type="dismiss")
    removed = client.get("/api/content-history?category=removed&limit=20").json()
    removed_item = next(item for item in removed["items"] if item["recommendation_id"] == shown_id)
    assert removed_item["content_id"] == "no-cache-shown"
    assert removed_item["content_url"] == "https://x.com/i/status/no-cache-shown"
    blank_twitter_removed = next(
        item for item in removed["items"] if item["recommendation_id"] == blank_twitter_id
    )
    assert blank_twitter_removed["item_key"] == "twitter:blank-twitter"
    assert blank_twitter_removed["content_id"] == "blank-twitter"
    assert blank_twitter_removed["content_url"] == "https://x.com/i/status/blank-twitter"


def test_removed_history_aggregates_all_contexts_with_independent_restore_state(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    first_recommendation = _recommendation(database, "BV1CONTEXTS", "多上下文内容")
    second_recommendation = database.insert_recommendation(
        "BV1CONTEXTS",
        confidence=0.9,
        expression="第二次推荐",
        topic="测试",
    )
    item = SavedItemInput(
        source_platform="bilibili",
        content_id="BV1CONTEXTS",
        content_url="https://www.bilibili.com/video/BV1CONTEXTS",
        title="多上下文内容",
    )
    database.upsert_saved_membership("favorite", item)
    database.upsert_saved_membership("watch_later", item)
    database.remove_saved_membership("favorite", item.item_key)
    database.remove_saved_membership("watch_later", item.item_key)
    database.update_recommendation_feedback(first_recommendation, feedback_type="dismiss")
    database.update_recommendation_feedback(second_recommendation, feedback_type="dislike")
    context_times = [
        str(
            database.conn.execute(
                "SELECT datetime('now', ?)",
                (f"-{4 - index} minutes",),
            ).fetchone()[0]
        )
        for index in range(4)
    ]
    database.conn.execute(
        """
        UPDATE saved_item_removals
        SET item_key = CASE WHEN list_kind = 'favorite' THEN 'bili:BV1CONTEXTS' ELSE item_key END,
            removed_at = CASE
                WHEN list_kind = 'favorite' THEN ?
                ELSE ?
            END
        """,
        (context_times[0], context_times[1]),
    )
    database.conn.execute(
        """
        UPDATE recommendations
        SET feedback_at = CASE
            WHEN id = ? THEN ?
            ELSE ?
        END
        WHERE id IN (?, ?)
        """,
        (
            first_recommendation,
            context_times[2],
            context_times[3],
            first_recommendation,
            second_recommendation,
        ),
    )
    database.conn.commit()

    database.upsert_saved_membership("favorite", item)
    rows, total = database.list_content_history("removed", limit=20)
    assert total == 1
    assert rows[0]["item_key"] == "bilibili:BV1CONTEXTS"
    assert rows[0]["context"] == "dislike"
    contexts = {context["context"]: context for context in rows[0]["contexts"]}
    assert set(contexts) == {"favorite", "watch_later", "dismiss", "dislike"}
    assert contexts["favorite"]["restored"] is True
    assert contexts["watch_later"]["restored"] is False
    assert contexts["dismiss"]["restored"] is False
    assert contexts["dislike"]["restored"] is False
    assert contexts["favorite"]["occurred_at"] == context_times[0]
    assert contexts["watch_later"]["occurred_at"] == context_times[1]

    database.upsert_saved_membership("watch_later", item)
    restored_rows, _ = database.list_content_history("removed", limit=20)
    restored_contexts = {context["context"]: context for context in restored_rows[0]["contexts"]}
    assert restored_contexts["favorite"]["restored"] is True
    assert restored_contexts["watch_later"]["restored"] is True


def test_all_browser_surfaces_expose_lazy_paginated_history() -> None:
    mobile_app = Path("src/openbiliclaw/web/js/app.js").read_text(encoding="utf-8")
    mobile_library = Path("src/openbiliclaw/web/js/views/library.js").read_text(encoding="utf-8")
    mobile_api = Path("src/openbiliclaw/web/js/api.js").read_text(encoding="utf-8")
    mobile_history = Path("src/openbiliclaw/web/js/views/history.js").read_text(encoding="utf-8")
    desktop_html = Path("src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    desktop_js = Path("src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")
    popup_html = Path("extension/popup/popup.html").read_text(encoding="utf-8")
    popup_js = Path("extension/popup/popup.js").read_text(encoding="utf-8")
    popup_api = Path("extension/popup/popup-api.js").read_text(encoding="utf-8")

    assert 'id: "library"' in mobile_app
    assert '{ id: "history", slug: "history"' in mobile_library
    assert "`/content-history?${params}`" in mobile_api
    assert 'params.set("cursor", cursor)' in mobile_api
    assert 'loading="lazy" fetchpriority="low"' in mobile_history
    assert "data-history-more" in mobile_history

    assert 'id="contentLibraryHistoryTab"' in desktop_html
    assert 'id="historyPage"' in desktop_html
    assert 'contentHistory: "/content-history"' in desktop_js
    assert 'query.set("cursor", page.nextCursor)' in desktop_js
    assert 'loading="lazy" fetchpriority="low"' in desktop_js
    assert "restoreContentHistoryItem" in desktop_js

    assert 'id="tabHistory"' in popup_html
    assert 'id="viewHistory"' in popup_html
    assert "requestJson(`/content-history?${params}`" in popup_api
    assert 'params.set("cursor", cursor)' in popup_api
    assert 'image.setAttribute("loading", "lazy")' in popup_js
    assert 'image.setAttribute("fetchpriority", "low")' in popup_js
    assert "loadContentHistoryCategory" in popup_js
