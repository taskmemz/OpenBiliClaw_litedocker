"""Regression tests for V2EX bootstrap events, durability, and Node affinity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from openbiliclaw.sources.source_bootstrap import enqueue_v2ex_bootstrap
from openbiliclaw.sources.v2ex_affinity import (
    V2EXNodeAffinityStore,
    v2ex_affinity_projection_username,
    v2ex_engaged_view_affinity_item,
)
from openbiliclaw.sources.v2ex_tasks import (
    V2EXFavoriteSnapshotStore,
    V2EXTaskQueue,
    v2ex_bootstrap_item_key,
    v2ex_bootstrap_items_to_events,
    v2ex_snapshot_effects_to_events,
)
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "v2ex.db")
    database.initialize()
    return database


def test_v2ex_bootstrap_events_aggregate_replies_without_claiming_agreement() -> None:
    events = v2ex_bootstrap_items_to_events(
        [
            {
                "scope": "public_replies",
                "topic_id": "123",
                "title": "Agent context management",
                "node_name": "programmer",
                "node_title": "程序员",
                "reply_text": "先把上下文切小。",
            },
            {
                "scope": "public_replies",
                "topic_id": "123",
                "title": "Agent context management",
                "node_name": "programmer",
                "reply_text": "我更在意本地运行。",
            },
            {
                "scope": "public_replies",
                "topic_id": "123",
                "title": "Agent context management",
                "node_name": "programmer",
                "reply_text": "第三条代表性回复。",
            },
            {
                "scope": "public_replies",
                "topic_id": "123",
                "title": "Agent context management",
                "node_name": "programmer",
                "reply_text": "第四条应被截断。",
            },
            {
                "scope": "public_topics",
                "topic_id": "456",
                "title": "My local agent project",
                "node_name": "programmer",
                "author_name": "alice",
            },
            {
                "scope": "favorite_topics",
                "topic_id": "456",
                "title": "My local agent project",
                "node_name": "programmer",
            },
            {
                "scope": "favorite_nodes",
                "node_name": "programmer",
                "node_title": "程序员",
            },
        ],
        identity_username="alice",
    )

    assert [event["event_type"] for event in events] == [
        "discussion_reply",
        "publish",
        "favorite",
        "follow",
    ]
    reply = events[0]
    assert reply["metadata"]["satisfaction"] == "unknown"
    assert reply["metadata"]["signal_strength"] == 0.75
    assert len(reply["metadata"]["reply_excerpts"]) == 3
    assert "第四条应被截断" not in reply["context"]
    assert events[1]["metadata"]["satisfaction"] == "unknown"
    assert events[2]["metadata"]["satisfaction"] == "positive"
    assert events[3]["metadata"]["content_type"] == "node"
    assert events[3]["url"] == "https://www.v2ex.com/go/programmer"
    assert all(event["metadata"]["source_identity"] == "alice" for event in events)


def test_v2ex_topic_event_url_is_always_derived_from_numeric_topic_id() -> None:
    [event] = v2ex_bootstrap_items_to_events(
        [
            {
                "scope": "public_topics",
                "topic_id": "456",
                "title": "Canonical topic",
                "url": "https://www.v2ex.com/t/456?utm_source=untrusted#reply1",
            }
        ],
        identity_username="alice",
    )
    assert event["url"] == "https://www.v2ex.com/t/456"


def test_v2ex_profile_identity_activation_isolates_historical_events(tmp_path: Path) -> None:
    database = _database(tmp_path)
    alice_id = database.insert_event(
        "publish",
        title="Alice topic",
        metadata={
            "source_platform": "v2ex",
            "source_identity": "alice",
            "profile_update_owner": "generic",
        },
    )
    bob_id = database.insert_event(
        "publish",
        title="Bob topic",
        metadata={
            "source_platform": "v2ex",
            "source_identity": "bob",
            "profile_update_owner": "generic",
        },
    )
    passive_id = database.insert_event(
        "content_page_exit",
        title="Unscoped local reading evidence",
        metadata={
            "source_platform": "v2ex",
            "content_type": "topic",
            "watch_seconds": 45,
        },
    )

    first = database.activate_v2ex_profile_identity("alice")
    assert first["username"] == "alice"
    assert {row["id"] for row in database.query_events(limit=10)} == {alice_id, passive_id}
    hidden_bob = database.conn.execute(
        "SELECT metadata FROM events WHERE id=?", (bob_id,)
    ).fetchone()
    assert hidden_bob is not None
    hidden_metadata = json.loads(str(hidden_bob[0]))
    assert hidden_metadata["profile_inactive"] is True
    assert "profile_update_owner" not in hidden_metadata

    database.activate_v2ex_profile_identity("bob")
    assert database.get_v2ex_profile_identity()[0] == "bob"
    assert {row["id"] for row in database.query_events(limit=10)} == {bob_id, passive_id}
    all_rows = database.query_events(limit=10, include_profile_inactive=True)
    assert {row["id"] for row in all_rows} == {alice_id, bob_id, passive_id}
    passive_row = database.conn.execute(
        "SELECT metadata FROM events WHERE id=?", (passive_id,)
    ).fetchone()
    assert passive_row is not None
    assert "profile_inactive" not in json.loads(str(passive_row[0]))


def test_v2ex_task_queue_freezes_first_final_result(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = V2EXTaskQueue(database)
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {"scopes": ["public_topics"]},
        daily_budget=0,
    )
    assert task_id is not None
    assert queue.next_pending()["id"] == task_id  # type: ignore[index]

    item = {
        "scope": "public_topics",
        "topic_id": "100",
        "title": "Original title",
        "node_name": "programmer",
    }
    queue.merge_result(task_id, items=[item], scope_counts={"public_topics": 1})
    staged = queue.stage_final_result(
        task_id,
        terminal_status="partial",
        items=[item],
        scope_counts={"public_topics": 1},
    )
    assert staged["_openbiliclaw_terminal_status"] == "partial"
    assert queue.get(task_id)["status"] == "in_progress"  # type: ignore[index]

    # A late callback cannot replace the immutable first final payload.
    assert (
        queue.merge_result(
            task_id,
            items=[{**item, "title": "Changed title", "topic_id": "200"}],
        )
        == []
    )
    assert queue.complete_staged_result(task_id) is True
    assert queue.complete_staged_result(task_id) is False

    row = queue.get(task_id)
    assert row is not None
    assert row["status"] == "completed"
    payload = json.loads(str(row["result_json"]))
    assert payload["items"] == [item]
    assert payload["_openbiliclaw_terminal_status"] == "partial"
    assert "cookie" not in json.dumps(payload, ensure_ascii=False)


def test_v2ex_task_queue_allows_only_one_fresh_cross_extension_lease(tmp_path: Path) -> None:
    database = _database(tmp_path)
    queue = V2EXTaskQueue(database)
    first_id = queue.enqueue_with_id("bootstrap_profile", {}, daily_budget=0)
    second_id = queue.enqueue_with_id("bootstrap_profile", {}, daily_budget=0)
    assert first_id is not None and second_id is not None

    assert queue.next_pending()["id"] == first_id  # type: ignore[index]
    assert queue.next_pending() is None

    database.conn.execute(
        "UPDATE v2ex_tasks SET claimed_at='2000-01-01 00:00:00' WHERE id=?",
        (first_id,),
    )
    database.conn.commit()
    assert queue.next_pending()["id"] == first_id  # type: ignore[index]


def test_v2ex_bootstrap_enqueue_persists_the_smoke_only_projection_gate(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)

    result = enqueue_v2ex_bootstrap(
        database,
        username="alice",
        force=True,
        profile_update=False,
        smoke_only=True,
    )

    assert result.created is True
    assert result.task_id is not None
    task = V2EXTaskQueue(database).get(result.task_id)
    assert task is not None
    payload = json.loads(str(task["payload_json"]))
    assert payload["username"] == "alice"
    assert payload["profile_update"] is False
    assert payload["smoke_only"] is True
    assert payload["scopes"] == [
        "public_topics",
        "public_replies",
        "favorite_topics",
        "favorite_nodes",
    ]


def test_v2ex_node_affinity_is_idempotent_for_retried_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = V2EXNodeAffinityStore(database)
    items = [
        {
            "scope": "public_topics",
            "topic_id": "1",
            "node_name": "Programmer",
            "node_title": "程序员",
        },
        {
            "scope": "public_replies",
            "topic_id": "1",
            "node_name": "Programmer",
        },
        {
            "scope": "favorite_nodes",
            "node_name": "Programmer",
            "node_title": "程序员",
        },
    ]

    assert store.record_items(items + [items[0]]) == 3
    assert store.record_items(items) == 0
    row = store.scores()[0]
    assert row["node_name"] == "programmer"
    assert row["score"] == 5.0
    assert row["favorite_node"] == 1
    assert row["published_topic_count"] == 1
    assert row["discussion_topic_count"] == 1
    assert row["evidence_level"] == "explicit"
    assert store.top_nodes() == ["programmer"]


def test_v2ex_engaged_topic_view_is_strict_and_counts_once(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = V2EXNodeAffinityStore(database)
    event = {
        "event_type": "click",
        "url": "https://www.v2ex.com/t/42?p=1#reply1",
        "metadata": {
            "source_platform": "v2ex",
            "content_id": "42",
            "dwell_source": "content_page_exit",
            "watch_seconds": 45,
            "node_name": "Programmer",
            "node_title": "程序员",
        },
    }
    item = v2ex_engaged_view_affinity_item(event, event_id=7)
    assert item is not None
    assert store.record_items([item], username="alice") == 1
    assert store.record_items([item], username="alice") == 0
    row = store.scores(username="alice")[0]
    assert row["score"] == 0.3
    assert row["engaged_view_count"] == 1

    assert (
        v2ex_engaged_view_affinity_item(
            {
                **event,
                "metadata": {**event["metadata"], "watch_seconds": 29.9},
            },
            event_id=8,
        )
        is None
    )
    assert v2ex_affinity_projection_username("Alice", "alice") == "Alice"
    assert v2ex_affinity_projection_username("alice", "") == "alice"
    assert v2ex_affinity_projection_username("alice", "bob") == ""
    assert v2ex_affinity_projection_username("", "alice") == ""
    assert (
        v2ex_engaged_view_affinity_item(
            {**event, "url": "https://example.com/t/42"},
            event_id=9,
        )
        is None
    )
    assert (
        v2ex_engaged_view_affinity_item(
            {
                **event,
                "metadata": {**event["metadata"], "content_id": "99"},
            },
            event_id=10,
        )
        is None
    )


def test_v2ex_bootstrap_item_keys_are_scope_aware() -> None:
    topic = {"scope": "public_topics", "topic_id": "42", "title": "old"}
    reply = {"scope": "public_replies", "topic_id": "42", "title": "old"}
    node = {"scope": "favorite_nodes", "node_name": "Programmer"}

    assert v2ex_bootstrap_item_key(topic) == "public_topics:topic:42"
    assert v2ex_bootstrap_item_key(reply) == "public_replies:topic:42"
    assert v2ex_bootstrap_item_key(node) == "favorite_nodes:node:programmer"


def test_v2ex_favorite_snapshot_requires_two_complete_misses_and_restores(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = V2EXFavoriteSnapshotStore(database)
    item = {
        "scope": "favorite_topics",
        "topic_id": "42",
        "title": "Local-first agents",
        "node_name": "programmer",
    }

    assert (
        store.prepare_complete_snapshot(
            task_id="baseline",
            username="Alice",
            scope="favorite_topics",
            items=[item],
            observed_at="2026-08-01T00:00:00+00:00",
        )
        == []
    )
    assert (
        store.prepare_complete_snapshot(
            task_id="missing-once",
            username="alice",
            scope="favorite_topics",
            items=[],
            observed_at="2026-08-02T00:00:00+00:00",
        )
        == []
    )

    effects = store.prepare_complete_snapshot(
        task_id="missing-twice",
        username="alice",
        scope="favorite_topics",
        items=[],
        observed_at="2026-08-03T00:00:00+00:00",
    )
    assert len(effects) == 1
    assert effects[0]["action"] == "retract"
    assert effects[0]["generation"] == 1
    events = v2ex_snapshot_effects_to_events(effects)
    assert events[0]["event_type"] == "feedback"
    assert events[0]["metadata"]["feedback_type"] == "retraction"
    assert events[0]["metadata"]["retracted_action"] == "favorite"
    assert events[0]["metadata"]["timestamp"] == "2026-08-03T00:00:00+00:00"

    # A lease replay sees the same pending outbox row, then no row after ack.
    assert (
        store.prepare_complete_snapshot(
            task_id="missing-twice",
            username="alice",
            scope="favorite_topics",
            items=[item],
        )
        == effects
    )
    assert store.mark_effects_emitted([effects[0]["effect_key"]]) == 1
    assert store.pending_effects("missing-twice") == []

    restored = store.prepare_complete_snapshot(
        task_id="restored",
        username="alice",
        scope="favorite_topics",
        items=[item],
        observed_at="2026-08-04T00:00:00+00:00",
    )
    assert len(restored) == 1
    assert restored[0]["action"] == "restore"
    assert restored[0]["generation"] == 2
    restored_event = v2ex_snapshot_effects_to_events(restored)[0]
    assert restored_event["event_type"] == "favorite"
    assert restored_event["metadata"]["snapshot_generation"] == 2


def test_v2ex_snapshot_affinity_effects_are_idempotent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = V2EXNodeAffinityStore(database)
    item = {
        "scope": "favorite_nodes",
        "node_name": "programmer",
        "node_title": "程序员",
    }
    assert store.record_items([item]) == 1
    retract = {
        "effect_key": "effect-retract-1",
        "scope": "favorite_nodes",
        "action": "retract",
        "item": item,
    }
    assert store.apply_snapshot_effects([retract]) == 1
    assert store.apply_snapshot_effects([retract]) == 0
    row = store.scores()[0]
    assert row["favorite_node"] == 0
    assert row["score"] == 0

    restore = {**retract, "effect_key": "effect-restore-2", "action": "restore"}
    assert store.apply_snapshot_effects([restore]) == 1
    row = store.scores()[0]
    assert row["favorite_node"] == 1
    assert row["score"] == 3.0


def test_v2ex_node_affinity_is_isolated_by_resolved_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = V2EXNodeAffinityStore(database)
    item = {
        "scope": "favorite_nodes",
        "node_name": "programmer",
        "node_title": "程序员",
    }

    assert store.record_items([item], username="alice") == 1
    assert store.record_items([item], username="bob") == 1
    assert store.top_nodes(username="alice") == ["programmer"]
    assert store.top_nodes(username="bob") == ["programmer"]
    assert store.scores() == []


def test_v2ex_node_affinity_applies_intent_discount_and_time_decay(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = V2EXNodeAffinityStore(database)
    old_jobs = [
        {
            "scope": "public_topics",
            "topic_id": str(index),
            "node_name": "jobs",
            "published_at": "2020-01-01T00:00:00Z",
        }
        for index in range(1, 5)
    ]
    stable_recent = [
        {
            "scope": "public_topics",
            "topic_id": str(100 + index),
            "node_name": "programmer",
        }
        for index in range(3)
    ]

    assert store.record_items(old_jobs + stable_recent, username="alice") == 7
    scores = store.scores(username="alice")
    assert [row["node_name"] for row in scores] == ["programmer", "jobs"]
    programmer, jobs = scores
    assert programmer["evidence_level"] == "observed_primary"
    assert json.loads(programmer["intent_mix_json"]) == {"stable_interest": 3}
    assert json.loads(jobs["intent_mix_json"]) == {"temporary_need": 4}
    assert programmer["effective_score"] > jobs["effective_score"]
