"""Linux.do task normalization and queue contract tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "linuxdo.db")
    db.initialize()
    return db


@pytest.mark.parametrize(
    ("task_type", "payload", "expected_seconds"),
    (
        (
            "bootstrap_events",
            {
                "scopes": [
                    "linuxdo_bookmarks",
                    "linuxdo_likes",
                    "linuxdo_read_history",
                ],
                "max_items_per_scope": 300,
            },
            1425.0,
        ),
        (
            "search",
            {"keywords": [f"keyword-{index}" for index in range(20)], "max_pages": 999},
            945.0,
        ),
        (
            "creator",
            {"creator_urls": [f"https://linux.do/u/user-{index}" for index in range(20)]},
            945.0,
        ),
        (
            "related",
            {"related_urls": [f"https://linux.do/t/{index + 1}" for index in range(20)]},
            345.0,
        ),
        ("hot", {"max_pages": 999}, 405.0),
        ("feed", {"max_pages": 999}, 225.0),
    ),
)
def test_linuxdo_task_timeout_estimator_caps_request_shape_below_claim_lease(
    task_type: str,
    payload: dict[str, object],
    expected_seconds: float,
) -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LINUXDO_TASK_CLAIM_LEASE_SECONDS,
        LINUXDO_TASK_RESULT_GRACE_SECONDS,
        linuxdo_task_timeout_seconds,
    )

    timeout = linuxdo_task_timeout_seconds(task_type, payload)

    assert timeout == expected_seconds
    assert timeout + LINUXDO_TASK_RESULT_GRACE_SECONDS < LINUXDO_TASK_CLAIM_LEASE_SECONDS


def test_linuxdo_bootstrap_items_map_scopes_to_canonical_events() -> None:
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_bootstrap_items_to_events

    events = linuxdo_bootstrap_items_to_events(
        [
            {
                "scope": "linuxdo_bookmarks",
                "topic_id": 101,
                "title": "收藏的主题",
                "url": "https://linux.do/t/old-slug/101?u=me",
                "author": "alice",
                "summary": "<p>正文 <strong>摘要</strong></p><script>secret</script>",
                "category": "开发调优",
                "tags": ["AI", "自托管"],
                "views": 123,
                "like_count": 9,
                "reply_count": 4,
            },
            {
                "scope": "linuxdo_likes",
                "content_id": "topic:102",
                "title": "点赞的主题",
                "author": "!",
            },
            {
                "scope": "linuxdo_read_history",
                "url": "https://linux.do/t/103",
                "title": "看过的主题",
            },
            {"scope": "linuxdo_unknown", "topic_id": 104, "title": "忽略"},
        ]
    )

    assert [event["event_type"] for event in events] == ["favorite", "like", "view"]
    assert [event["metadata"]["source_platform"] for event in events] == [
        "linuxdo",
        "linuxdo",
        "linuxdo",
    ]
    assert [event["metadata"]["import_source"] for event in events] == [
        "linuxdo_bootstrap_bookmarks",
        "linuxdo_bootstrap_likes",
        "linuxdo_bootstrap_read_history",
    ]
    assert [event["metadata"]["signal_strength"] for event in events] == [0.9, 0.85, 0.35]
    assert events[0]["url"] == "https://linux.do/t/old-slug/101"
    assert events[0]["metadata"]["content_id"] == "topic:101"
    assert events[0]["metadata"]["content_type"] == "post"
    assert events[0]["metadata"]["summary"] == "正文 摘要"
    assert events[0]["metadata"]["tags"] == ["开发调优", "AI", "自托管"]
    assert events[0]["metadata"]["view_count"] == 123
    assert events[0]["metadata"]["like_count"] == 9
    assert events[0]["metadata"]["comment_count"] == 4
    assert events[0]["metadata"]["favorite_count"] == 0
    # Short and punctuation-only usernames are valid identities, not placeholders.
    assert events[1]["metadata"]["author"] == "!"


def test_linuxdo_topic_identity_is_stable_and_rejects_foreign_or_nested_values() -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        linuxdo_bootstrap_item_key,
        linuxdo_item_key,
        linuxdo_topic_id,
    )

    original = {
        "scope": "linuxdo_search",
        "url": "https://linux.do/t/old-title/202?order=latest",
    }
    renamed = {**original, "url": "https://linux.do/t/new-title/202"}

    assert linuxdo_topic_id(original) == "202"
    assert linuxdo_item_key(original) == "linuxdo_search:topic:202"
    assert linuxdo_item_key(renamed) == "linuxdo_search:topic:202"
    assert linuxdo_topic_id({"content_id": "linuxdo:topic:203"}) == "203"
    assert linuxdo_topic_id({"url": "/t/local-path/204"}) == "204"
    assert linuxdo_topic_id({"url": "https://linux.do/t/204/7"}) == "204"
    assert linuxdo_topic_id({"url": "https://evil.example/t/topic/205"}) == ""
    assert linuxdo_topic_id({"topic_id": {"nested": 206}}) == ""
    assert linuxdo_topic_id({"topic_id": True}) == ""
    assert linuxdo_bootstrap_item_key({"scope": "linuxdo_likes", "topic_id": 202}) == (
        "linuxdo_likes:topic:202"
    )
    assert linuxdo_bootstrap_item_key({"scope": "linuxdo_bookmarks", "topic_id": 202}) == (
        "linuxdo_bookmarks:topic:202"
    )


def test_linuxdo_discovery_normalizes_all_modes_and_engagement() -> None:
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_discovery_items_to_contents

    contents = linuxdo_discovery_items_to_contents(
        [
            {
                "source_strategy": "linuxdo-search",
                "search_keyword": "本地大模型",
                "topic_id": 301,
                "title": "本地模型部署踩坑",
                "url": "https://linux.do/t/local-model/301/7?order=latest",
                "author": "alice",
                "summary": "过程记录",
                "category": "开发调优",
                "tags": ["LLM", "部署"],
                "views": "1,234",
                "like_count": 56,
                "reply_count": 7,
                "published_at": 1783492200,
                "published_label": "3 天前",
            },
            {
                "source_strategy": "linuxdo-hot",
                "topic_id": 302,
                "title": "热门主题",
                "views": 88,
                "likes": 6,
                "posts_count": 5,
            },
            {"scope": "linuxdo_feed", "topic_id": 303, "title": "最新主题"},
            {"scope": "linuxdo_creator", "topic_id": 304, "title": "作者主题"},
            {"scope": "linuxdo_related", "topic_id": 305, "title": "相关主题"},
            {"scope": "linuxdo_bookmarks", "topic_id": 306, "title": "不是发现候选"},
        ],
        source_keyword_ids={"本地大模型": 42},
    )

    assert [content.source_strategy for content in contents] == [
        "linuxdo-search",
        "linuxdo-hot",
        "linuxdo-feed",
        "linuxdo-creator",
        "linuxdo-related",
    ]
    first = contents[0]
    assert first.source_platform == "linuxdo"
    assert first.content_id == "topic:301"
    assert first.bvid == "topic:301"
    assert first.content_url == "https://linux.do/t/local-model/301"
    assert first.content_type == "post"
    assert first.author_name == "alice"
    assert first.body_text == "过程记录"
    assert first.tags == ["开发调优", "LLM", "部署"]
    assert first.view_count == 1234
    assert first.like_count == 56
    assert first.comment_count == 7
    assert first.reply_count == 7
    assert first.favorite_count == 0
    assert first.share_count == 0
    assert first.danmaku_count == 0
    assert first.source_keyword_id == 42
    assert first.score_threshold == 0.60
    assert first.published_at == "2026-07-08T06:30:00Z"
    assert first.published_label == "3 天前"
    assert contents[1].comment_count == 4


def test_linuxdo_discovery_merges_duplicate_topic_metrics_without_losing_search_provenance() -> (
    None
):
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_discovery_items_to_contents

    contents = linuxdo_discovery_items_to_contents(
        [
            {
                "scope": "linuxdo_search",
                "topic_id": 401,
                "title": "同一主题",
                "search_keyword": "Agent",
                "views": 10,
                "tags": ["AI"],
            },
            {
                "scope": "linuxdo_hot",
                "topic_id": 401,
                "title": "同一主题",
                "author": "bob",
                "summary": "补齐的摘要",
                "views": 99,
                "like_count": 8,
                "reply_count": 3,
                "tags": ["开源"],
            },
        ],
        source_keyword_ids={"Agent": 77},
    )

    assert len(contents) == 1
    content = contents[0]
    assert content.source_strategy == "linuxdo-search"
    assert content.source_keyword_id == 77
    assert content.author_name == "bob"
    assert content.body_text == "补齐的摘要"
    assert content.view_count == 99
    assert content.like_count == 8
    assert content.comment_count == 3
    assert content.tags == ["AI", "开源"]


def test_linuxdo_normalizer_does_not_stringify_nested_schema_values() -> None:
    from openbiliclaw.sources.linuxdo_tasks import linuxdo_discovery_items_to_contents

    contents = linuxdo_discovery_items_to_contents(
        [
            {
                "scope": "linuxdo_feed",
                "topic_id": 501,
                "title": {"cooked": "not a title"},
                "author": ["not", "an", "author"],
                "summary": {"nested": "not text"},
                "category": {"name": "not flattened"},
                "tags": "not-a-list",
                "views": {"count": 9},
                "like_count": True,
                "reply_count": [3],
                "url": "https://evil.example/t/topic/999",
            },
            {"source_strategy": "forged-strategy", "topic_id": 502},
        ]
    )

    assert len(contents) == 1
    content = contents[0]
    assert content.title == "Linux.do 主题 501"
    assert content.author_name == ""
    assert content.body_text == ""
    assert content.tags == []
    assert content.content_url == "https://linux.do/t/501"
    assert content.view_count == 0
    assert content.like_count == 0
    assert content.comment_count == 0


def test_linuxdo_queue_stages_first_final_and_completes_without_replacing_json(
    database: Database,
) -> None:
    from openbiliclaw.sources.linuxdo_tasks import LinuxdoTaskQueue
    from openbiliclaw.sources.task_result_protocol import STAGED_TERMINAL_STATUS_FIELD

    queue = LinuxdoTaskQueue(database)
    task_id = queue.enqueue_with_id("search", {"keywords": ["AI"]}, daily_budget=0)
    assert task_id is not None
    claimed = queue.next_pending()
    assert claimed is not None
    assert claimed["id"] == task_id
    assert claimed["status"] == "in_progress"

    canonical = queue.stage_final_result(
        task_id,
        terminal_status="completed",
        items=[
            {
                "scope": "linuxdo_search",
                "topic_id": 601,
                "title": "first payload wins",
            }
        ],
        scope_counts={"linuxdo_search": 1},
    )
    assert canonical[STAGED_TERMINAL_STATUS_FIELD] == "completed"

    replay = queue.stage_final_result(
        task_id,
        terminal_status="completed",
        items=[
            {
                "scope": "linuxdo_search",
                "topic_id": 999,
                "title": "must not replace",
            }
        ],
    )
    assert replay == canonical
    assert queue.merge_result(task_id, items=[{"scope": "linuxdo_search", "topic_id": 602}]) == []

    database.conn.execute(
        """
        UPDATE linuxdo_tasks
        SET created_at = '2000-01-01 00:00:00',
            claimed_at = '2000-01-01 00:00:00'
        WHERE id = ?
        """,
        (task_id,),
    )
    database.conn.commit()
    assert queue.expire_stale_pending(("search",), older_than_seconds=0) == 0
    # A staged row is logically terminal but still needs ordinary stale-lease
    # reclaim so a later callback can replay the frozen payload through any
    # projection step that crashed before the terminal DB flip.
    reclaimed = queue.next_pending()
    assert reclaimed is not None
    assert reclaimed["id"] == task_id
    assert json.loads(str(reclaimed["result_json"])) == canonical

    before = str(queue.get(task_id)["result_json"])
    assert queue.complete_staged_result(task_id) is True
    completed = queue.get(task_id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert str(completed["result_json"]) == before


def test_linuxdo_queue_merges_scope_rows_and_exposes_recent_seeds(database: Database) -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LinuxdoTaskQueue,
        recent_linuxdo_creator_urls,
        recent_linuxdo_related_urls,
    )

    queue = LinuxdoTaskQueue(database)
    task_id = queue.enqueue_with_id("hot", {"max_items": 10}, daily_budget=0)
    assert task_id is not None
    first = {
        "scope": "linuxdo_hot",
        "topic_id": 701,
        "title": "主题",
        "author": "alice",
        "url": "https://linux.do/t/local-agent/701/9?order=latest",
    }
    assert queue.merge_result(
        task_id,
        items=[first, {**first, "title": "duplicate rerender"}],
        scope_counts={"linuxdo_hot": 1},
    ) == [first]
    queue.merge_result(
        task_id,
        items=[
            {
                "scope": "linuxdo_hot",
                "topic_id": 702,
                "title": "第二个主题",
                "author_url": "https://linux.do/u/bob/activity/topics",
            }
        ],
        scope_counts={"linuxdo_hot": 2},
        complete=True,
    )

    payload = json.loads(str(queue.get(task_id)["result_json"]))
    assert len(payload["items"]) == 2
    assert payload["scope_counts"] == {"linuxdo_hot": 2}
    assert recent_linuxdo_creator_urls(database, limit=5) == [
        "https://linux.do/u/alice/activity/topics",
        "https://linux.do/u/bob/activity/topics",
    ]
    assert recent_linuxdo_related_urls(database, limit=5) == [
        "https://linux.do/t/local-agent/701",
        "https://linux.do/t/702",
    ]


def test_linuxdo_queue_reclaims_claim_only_after_35_minute_lease(database: Database) -> None:
    from openbiliclaw.sources.linuxdo_tasks import LinuxdoTaskQueue

    queue = LinuxdoTaskQueue(database)
    task_id = queue.enqueue_with_id("hot", {"max_items": 5}, daily_budget=0)
    assert task_id is not None
    assert queue.next_pending()["id"] == task_id

    database.conn.execute(
        "UPDATE linuxdo_tasks SET claimed_at = datetime('now', '-34 minutes') WHERE id = ?",
        (task_id,),
    )
    database.conn.commit()
    assert queue.next_pending() is None

    database.conn.execute(
        "UPDATE linuxdo_tasks SET claimed_at = datetime('now', '-36 minutes') WHERE id = ?",
        (task_id,),
    )
    database.conn.commit()
    reclaimed = queue.next_pending()
    assert reclaimed is not None
    assert reclaimed["id"] == task_id
    assert reclaimed["status"] == "in_progress"


def test_linuxdo_stale_pending_failure_does_not_consume_daily_budget(
    database: Database,
) -> None:
    from openbiliclaw.sources.linuxdo_tasks import LinuxdoTaskQueue

    queue = LinuxdoTaskQueue(database)
    abandoned = queue.enqueue_with_id("hot", {"max_items": 5}, daily_budget=1)
    assert abandoned is not None
    assert queue.fail(abandoned, error="stale_pending") is True

    replacement = queue.enqueue_with_id("hot", {"max_items": 5}, daily_budget=1)

    assert replacement is not None
    queue.record_retained(replacement, 1)
    assert queue.enqueue_with_id("hot", {"max_items": 5}, daily_budget=1) is None


def test_linuxdo_bootstrap_budget_counts_attempts_but_not_unclaimed_timeout(
    database: Database,
) -> None:
    from openbiliclaw.sources.linuxdo_tasks import LinuxdoTaskQueue

    queue = LinuxdoTaskQueue(database)
    unclaimed = queue.enqueue_with_id("bootstrap_events", {}, daily_budget=1)
    assert unclaimed is not None
    assert queue.fail(unclaimed, error="stale_pending") is True

    attempted = queue.enqueue_with_id("bootstrap_events", {}, daily_budget=1)
    assert attempted is not None
    assert queue.fail(attempted, error="linuxdo_login_required") is True
    assert queue.enqueue_with_id("bootstrap_events", {}, daily_budget=1) is None


def test_linuxdo_page_cursor_is_durable_and_input_partitioned(database: Database) -> None:
    from openbiliclaw.sources.linuxdo_tasks import LinuxdoTaskQueue

    queue = LinuxdoTaskQueue(database)
    assert queue.discovery_page_cursor("search", "first") == {"page": 0, "offset": 0}

    queue.set_discovery_page_cursor("search", "first", {"page": 3, "offset": 7})
    queue.set_discovery_page_cursor("search", "second", {"page": 1, "offset": 2})

    reopened = LinuxdoTaskQueue(database)
    assert reopened.discovery_page_cursor("search", "first") == {"page": 3, "offset": 7}
    assert reopened.discovery_page_cursor("search", "second") == {"page": 1, "offset": 2}
    assert reopened.discovery_page_cursor("feed", "") == {"page": 0, "offset": 0}


def test_linuxdo_creator_result_cap_is_enforced_per_backend_owned_input() -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LinuxdoTaskResultValidationError,
        validate_linuxdo_task_result,
    )

    creator_a = "https://linux.do/u/a/activity/topics"
    creator_b = "https://linux.do/u/b/activity/topics"
    common = {
        "task_type": "creator",
        "task_payload": {
            "creator_urls": [creator_a, creator_b],
            "max_items_per_creator": 1,
        },
        "status": "ok",
        "scope_counts": {"linuxdo_creator": 2},
        "account_key": "",
        "response_observed": True,
        "complete_scopes": ["linuxdo_creator"],
    }
    items = [
        {
            "scope": "linuxdo_creator",
            "content_type": "post",
            "topic_id": 1,
            "source_input": creator_a,
        },
        {
            "scope": "linuxdo_creator",
            "content_type": "post",
            "topic_id": 2,
            "source_input": creator_a,
        },
    ]

    with pytest.raises(LinuxdoTaskResultValidationError, match="per_input_cap_exceeded"):
        validate_linuxdo_task_result(items=items, **common)

    items[1]["source_input"] = creator_b
    validate_linuxdo_task_result(items=items, **common)


def test_linuxdo_result_provenance_is_bound_to_backend_task_payload() -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LinuxdoTaskResultValidationError,
        validate_linuxdo_task_result,
    )

    common = {
        "task_type": "search",
        "task_payload": {
            "keywords": ["本地大模型"],
            "source_keyword_ids": {"本地大模型": 17},
            "max_items_per_keyword": 2,
        },
        "status": "ok",
        "scope_counts": {"linuxdo_search": 1},
        "account_key": "",
        "response_observed": True,
        "complete_scopes": ["linuxdo_search"],
        "next_cursors": None,
    }
    item = {
        "scope": "linuxdo_search",
        "content_type": "post",
        "topic_id": 9,
        "search_keyword": "本地大模型",
        "source_keyword_id": 17,
    }
    validate_linuxdo_task_result(items=[item], **common)

    item["source_keyword_id"] = 999
    with pytest.raises(LinuxdoTaskResultValidationError, match="source_keyword_id_mismatch"):
        validate_linuxdo_task_result(items=[item], **common)

    item.pop("source_keyword_id")
    validate_linuxdo_task_result(items=[item], **common)

    common["task_payload"] = {
        "keywords": ["本地大模型"],
        "max_items_per_keyword": 2,
    }
    item["source_keyword_id"] = 17
    with pytest.raises(LinuxdoTaskResultValidationError, match="source_keyword_id_mismatch"):
        validate_linuxdo_task_result(items=[item], **common)


@pytest.mark.parametrize(
    ("task_type", "payload", "scope", "source_inputs"),
    (
        (
            "search",
            {
                "keywords": ["first", "second"],
                "max_items_per_keyword": 2,
                "max_items": 1,
            },
            "linuxdo_search",
            ("first", "second"),
        ),
        (
            "related",
            {
                "related_urls": [
                    "https://linux.do/t/first/1",
                    "https://linux.do/t/second/2",
                ],
                "max_items_per_seed": 2,
                "max_items": 1,
            },
            "linuxdo_related",
            ("https://linux.do/t/first/1", "https://linux.do/t/second/2"),
        ),
    ),
)
def test_linuxdo_result_global_cap_cannot_expand_across_inputs(
    task_type: str,
    payload: dict[str, object],
    scope: str,
    source_inputs: tuple[str, str],
) -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LinuxdoTaskResultValidationError,
        validate_linuxdo_task_result,
    )

    items = [
        {
            "scope": scope,
            "content_type": "post",
            "topic_id": index,
            "source_input": source_input,
            **({"search_keyword": source_input} if task_type == "search" else {}),
        }
        for index, source_input in enumerate(source_inputs, start=1)
    ]
    common = {
        "task_type": task_type,
        "task_payload": payload,
        "status": "ok",
        "scope_counts": {scope: len(items)},
        "account_key": "",
        "response_observed": True,
        "complete_scopes": [scope],
        "next_cursors": None,
    }

    validate_linuxdo_task_result(
        items=items[:1],
        **{**common, "scope_counts": {scope: 1}},
    )
    with pytest.raises(LinuxdoTaskResultValidationError, match="task_result_cap_exceeded"):
        validate_linuxdo_task_result(items=items, **common)


def test_linuxdo_bootstrap_interaction_action_cannot_change_scope_semantics() -> None:
    from openbiliclaw.sources.linuxdo_tasks import (
        LinuxdoTaskResultValidationError,
        validate_linuxdo_task_result,
    )

    common = {
        "task_type": "bootstrap_events",
        "task_payload": {
            "scopes": ["linuxdo_bookmarks"],
            "max_items_per_scope": 1,
        },
        "status": "ok",
        "scope_counts": {"linuxdo_bookmarks": 1},
        "account_key": "sha256:" + "a" * 64,
        "response_observed": True,
        "complete_scopes": ["linuxdo_bookmarks"],
        "next_cursors": None,
    }
    item = {
        "scope": "linuxdo_bookmarks",
        "content_type": "post",
        "topic_id": 10,
        "interaction_action": "favorite",
    }
    validate_linuxdo_task_result(items=[item], **common)

    item["interaction_action"] = "like"
    with pytest.raises(LinuxdoTaskResultValidationError, match="interaction_action_mismatch"):
        validate_linuxdo_task_result(items=[item], **common)
