"""HTTP contract tests for the read-only V2EX browser task bridge."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from openbiliclaw.api.app import create_app
from openbiliclaw.sources.v2ex_tasks import V2EXTaskQueue
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


class RecordingMemoryManager:
    """Capture V2EX incremental events through the durable ingress fallback."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self._source_bootstrap_state: dict[str, object] = {}

    async def propagate_event(self, event: dict[str, object]) -> None:
        self.events.append(event)

    def load_source_bootstrap_state(self) -> dict[str, object]:
        return dict(self._source_bootstrap_state)

    def update_source_bootstrap_state(self, mutator: object) -> dict[str, object]:
        state = dict(self._source_bootstrap_state)
        result = mutator(state)  # type: ignore[operator]
        self._source_bootstrap_state = state if result is None else result  # type: ignore[assignment]
        return dict(self._source_bootstrap_state)


def _client(tmp_path: Path) -> tuple[TestClient, Database, V2EXTaskQueue]:
    database = Database(tmp_path / "v2ex-api.db")
    database.initialize()
    queue = V2EXTaskQueue(database)
    app = create_app(memory_manager=object(), database=database, soul_engine=object())
    return TestClient(app), database, queue


def _profile_client(
    tmp_path: Path,
) -> tuple[TestClient, Database, V2EXTaskQueue, RecordingMemoryManager]:
    database = Database(tmp_path / "v2ex-profile-api.db")
    database.initialize()
    memory = RecordingMemoryManager()
    queue = V2EXTaskQueue(database)
    app = create_app(memory_manager=memory, database=database, soul_engine=object())
    return TestClient(app), database, queue, memory


def test_v2ex_task_api_claims_and_freezes_a_read_only_result(tmp_path: Path) -> None:
    client, database, queue = _client(tmp_path)
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {
            "scopes": ["public_topics", "public_replies"],
            "username": "alice",
        },
        daily_budget=0,
    )
    assert task_id is not None

    next_response = client.get("/api/sources/v2ex/next-task")
    assert next_response.status_code == 200
    assert next_response.json() == {
        "id": task_id,
        "type": "bootstrap_profile",
        "scopes": ["public_topics", "public_replies"],
        "username": "alice",
    }

    item = {
        "scope": "public_topics",
        "topic_id": "123",
        "title": "Local-first agents",
        "node_name": "programmer",
        "cookie": "must never be persisted",
    }
    result_response = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": task_id,
            "status": "ok",
            "items": [item],
            "scope_counts": {"public_topics": 1},
            "debug": {
                "username": "alice",
                "logged_in": True,
                "html": "must not be persisted",
            },
        },
    )
    assert result_response.status_code == 200, result_response.text
    assert result_response.json() == {"ok": True}

    stored = queue.get(task_id)
    assert stored is not None
    assert stored["status"] == "completed"
    payload = json.loads(str(stored["result_json"]))
    assert payload["items"] == [
        {
            "scope": "public_topics",
            "topic_id": "123",
            "title": "Local-first agents",
            "node_name": "programmer",
        }
    ]
    assert payload["debug"] == {"username": "alice", "logged_in": True}
    assert database.get_v2ex_browser_identity()[:2] == ("alice", "observed")


def test_v2ex_smoke_only_result_skips_memory_affinity_and_snapshot_projection(
    tmp_path: Path,
) -> None:
    client, database, queue, memory = _profile_client(tmp_path)
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {
            "scopes": ["favorite_topics", "favorite_nodes"],
            "username": "alice",
            "profile_update": False,
            "smoke_only": True,
        },
        daily_budget=0,
    )
    assert task_id is not None

    response = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": task_id,
            "status": "ok",
            "items": [
                {
                    "scope": "favorite_topics",
                    "topic_id": "123",
                    "title": "Read-only smoke topic",
                    "node_name": "programmer",
                },
                {
                    "scope": "favorite_nodes",
                    "node_name": "programmer",
                    "node_title": "程序员",
                },
            ],
            "debug": {
                "username": "alice",
                "logged_in": True,
                "scope_complete": {
                    "favorite_topics": True,
                    "favorite_nodes": True,
                },
                "scope_statuses": {
                    "favorite_topics": "ok",
                    "favorite_nodes": "ok",
                },
            },
        },
    )

    assert response.status_code == 200, response.text
    assert queue.get(task_id)["status"] == "completed"  # type: ignore[index]
    assert database.get_v2ex_browser_identity()[:2] == ("alice", "observed")
    assert memory.events == []
    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    assert V2EXNodeAffinityStore(database).scores() == []
    assert (
        database.conn.execute("SELECT COUNT(*) FROM v2ex_favorite_snapshot_items").fetchone()[0]
        == 0
    )
    assert (
        database.conn.execute("SELECT COUNT(*) FROM v2ex_favorite_snapshot_runs").fetchone()[0] == 0
    )


def test_v2ex_login_state_persists_only_boolean_and_observed_public_identity(
    tmp_path: Path,
) -> None:
    client, database, _queue = _client(tmp_path)

    response = client.post(
        "/api/sources/v2ex/login-state",
        json={"logged_in": True, "username": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["logged_in"] is True
    assert response.json()["username"] == "alice"
    assert database.get_v2ex_login_state()[0] is True
    assert database.get_v2ex_browser_identity()[:2] == ("alice", "observed")

    invalid = client.post(
        "/api/sources/v2ex/login-state",
        json={"logged_in": "true", "username": "alice"},
    )
    assert invalid.status_code == 422

    accepted = client.post(
        "/api/sources/v2ex/identity",
        json={"username": "alice", "accept": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["evidence"] == "accepted"

    identity = client.post("/api/sources/v2ex/identity", json={"username": "bob"})
    assert identity.status_code == 200
    assert identity.json()["username"] == "bob"
    resolved = client.get("/api/sources/v2ex/identity")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "identity_mismatch"
    assert resolved.json()["claims"] == {"browser": "bob", "accepted": "alice"}

    logged_out = client.post(
        "/api/sources/v2ex/login-state",
        json={"logged_in": False, "username": "should-not-survive"},
    )
    assert logged_out.status_code == 200
    assert logged_out.json()["username"] == ""
    assert database.get_v2ex_browser_identity()[0] == ""


def test_v2ex_task_result_rejects_missing_status_without_completing_task(tmp_path: Path) -> None:
    client, _database, queue = _client(tmp_path)
    task_id = queue.enqueue_with_id("bootstrap_profile", {}, daily_budget=0)
    assert task_id is not None
    assert client.get("/api/sources/v2ex/next-task").status_code == 200

    response = client.post(
        "/api/sources/v2ex/task-result",
        json={"task_id": task_id, "items": []},
    )
    assert response.status_code == 200
    assert queue.get(task_id)["status"] == "failed"  # type: ignore[index]


def test_v2ex_incremental_result_projects_events_and_node_affinity(tmp_path: Path) -> None:
    client, database, queue, memory = _profile_client(tmp_path)
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {
            "scopes": ["public_topics", "public_replies", "favorite_nodes"],
            "username": "alice",
            "incremental": True,
        },
        daily_budget=0,
    )
    assert task_id is not None

    response = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": task_id,
            "status": "ok",
            "items": [
                {
                    "scope": "public_topics",
                    "topic_id": "123",
                    "title": "Local-first agents",
                    "node_name": "programmer",
                },
                {
                    "scope": "public_replies",
                    "topic_id": "123",
                    "title": "Local-first agents",
                    "node_name": "programmer",
                    "reply_text": "先关注本地运行和隐私。",
                },
                {
                    "scope": "public_replies",
                    "topic_id": "123",
                    "node_name": "programmer",
                    "reply_text": "另一个角度是可迁移性。",
                },
                {
                    "scope": "favorite_nodes",
                    "node_name": "programmer",
                    "node_title": "程序员",
                },
            ],
            "debug": {"username": "alice", "logged_in": True},
        },
    )

    assert response.status_code == 200, response.text
    assert [event["event_type"] for event in memory.events] == [
        "publish",
        "discussion_reply",
        "follow",
    ]
    assert memory.events[1]["metadata"]["topic_id"] == "123"  # type: ignore[index]
    assert all(
        event["metadata"]["source_identity"] == "alice"  # type: ignore[index]
        for event in memory.events
    )

    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    scores = V2EXNodeAffinityStore(database).scores(username="alice")
    assert len(scores) == 1
    assert scores[0]["node_name"] == "programmer"
    assert scores[0]["published_topic_count"] == 1
    assert scores[0]["discussion_topic_count"] == 1
    assert scores[0]["favorite_node"] == 1
    assert scores[0]["score"] == 5.0


def test_v2ex_incremental_complete_snapshots_emit_retraction_after_second_miss(
    tmp_path: Path,
) -> None:
    client, database, queue, memory = _profile_client(tmp_path)

    def submit(task_id: str, items: list[dict[str, object]], *, complete: bool) -> None:
        response = client.post(
            "/api/sources/v2ex/task-result",
            json={
                "task_id": task_id,
                "status": "ok" if items else "empty",
                "items": items,
                "debug": {
                    "username": "alice",
                    "logged_in": True,
                    "scope_complete": {"favorite_topics": complete},
                    "scope_statuses": {"favorite_topics": "ok" if items else "empty"},
                },
            },
        )
        assert response.status_code == 200, response.text

    favorite = {
        "scope": "favorite_topics",
        "topic_id": "88",
        "title": "Agent context",
        "node_name": "programmer",
    }
    task_ids: list[str] = []
    for suffix in ("baseline", "incomplete", "missing-once", "missing-twice"):
        task_mode = {"profile_update": True} if suffix == "baseline" else {"incremental": True}
        task_id = queue.enqueue_with_id(
            "bootstrap_profile",
            {
                "scopes": ["favorite_topics"],
                "username": "alice",
                **task_mode,
                "run": suffix,
            },
            daily_budget=0,
        )
        assert task_id is not None
        task_ids.append(task_id)

    submit(task_ids[0], [favorite], complete=True)
    submit(task_ids[1], [], complete=False)
    submit(task_ids[2], [], complete=True)
    assert [event["event_type"] for event in memory.events] == ["favorite"]

    submit(task_ids[3], [], complete=True)
    assert [event["event_type"] for event in memory.events] == ["favorite", "feedback"]
    retraction = memory.events[-1]
    assert retraction["metadata"]["feedback_type"] == "retraction"  # type: ignore[index]
    assert retraction["metadata"]["retracted_action"] == "favorite"  # type: ignore[index]
    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    affinity = V2EXNodeAffinityStore(database).scores(username="alice")[0]
    assert affinity["favorite_topic_count"] == 0
    assert affinity["score"] == 0


def test_v2ex_unproven_empty_scope_never_advances_favorite_snapshot(
    tmp_path: Path,
) -> None:
    client, database, queue, memory = _profile_client(tmp_path)

    def enqueue(run: str) -> str:
        task_id = queue.enqueue_with_id(
            "bootstrap_profile",
            {
                "scopes": ["favorite_topics"],
                "username": "alice",
                "incremental": True,
                "run": run,
            },
            daily_budget=0,
        )
        assert task_id is not None
        return task_id

    favorite = {
        "scope": "favorite_topics",
        "topic_id": "88",
        "title": "Agent context",
        "node_name": "programmer",
    }
    baseline = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": enqueue("baseline"),
            "status": "ok",
            "items": [favorite],
            "debug": {
                "username": "alice",
                "logged_in": True,
                "scope_complete": {"favorite_topics": True},
                "scope_statuses": {"favorite_topics": "ok"},
            },
        },
    )
    assert baseline.status_code == 200, baseline.text

    for run, scope_status in (("challenge", "parse_error"), ("hidden", "hidden")):
        response = client.post(
            "/api/sources/v2ex/task-result",
            json={
                "task_id": enqueue(run),
                "status": "partial",
                "items": [],
                # Even a buggy or forged completion bit must not turn an
                # unrecognized/private page into an authoritative empty snapshot.
                "debug": {
                    "username": "alice",
                    "logged_in": True,
                    "scope_complete": {"favorite_topics": True},
                    "scope_statuses": {"favorite_topics": scope_status},
                },
            },
        )
        assert response.status_code == 200, response.text

    assert [event["event_type"] for event in memory.events] == ["favorite"]
    assert (
        database.conn.execute("SELECT COUNT(*) FROM v2ex_favorite_snapshot_runs").fetchone()[0] == 1
    )
    active = database.conn.execute(
        "SELECT active, missing_streak FROM v2ex_favorite_snapshot_items "
        "WHERE username_key = ? AND scope = ? AND item_key = ?",
        ("alice", "favorite_topics", "favorite_topics:topic:88"),
    ).fetchone()
    assert active is not None
    assert tuple(active) == (1, 0)


def test_v2ex_identity_mismatch_completes_task_but_pauses_account_projection(
    tmp_path: Path,
) -> None:
    from openbiliclaw.api.source_auth.probe_cache import LIVE_PROBES

    LIVE_PROBES.clear("v2ex")
    client, database, queue, memory = _profile_client(tmp_path)
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {"scopes": ["public_topics"], "username": "alice", "incremental": True},
        daily_budget=0,
    )
    assert task_id is not None

    response = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": task_id,
            "status": "ok",
            "items": [
                {
                    "scope": "public_topics",
                    "topic_id": "99",
                    "title": "Must not be attributed",
                    "node_name": "programmer",
                }
            ],
            "debug": {"username": "bob", "logged_in": True},
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["profile_paused"] is True
    assert payload["identity"]["status"] == "identity_mismatch"
    assert payload["identity"]["claims"] == {"browser": "bob", "configured": "alice"}
    assert memory.events == []
    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    assert V2EXNodeAffinityStore(database).scores() == []
    assert queue.get(task_id)["status"] == "completed"  # type: ignore[index]


def test_v2ex_incremental_cannot_silently_switch_profile_identity(tmp_path: Path) -> None:
    from openbiliclaw.api.source_auth.probe_cache import LIVE_PROBES

    LIVE_PROBES.clear("v2ex")
    client, database, queue, memory = _profile_client(tmp_path)
    item = {
        "scope": "public_topics",
        "topic_id": "same-topic",
        "title": "Shared topic id",
        "node_name": "programmer",
    }
    # Topic ids are normally numeric; this fixture uses a canonical URL so the
    # event remains valid while making the identity-scoping assertion obvious.
    item["topic_id"] = "123"
    responses: list[dict[str, object]] = []
    for username in ("alice", "bob"):
        task_id = queue.enqueue_with_id(
            "bootstrap_profile",
            {"scopes": ["public_topics"], "username": username, "incremental": True},
            daily_budget=0,
        )
        assert task_id is not None
        response = client.post(
            "/api/sources/v2ex/task-result",
            json={
                "task_id": task_id,
                "status": "ok",
                "items": [item],
                "debug": {"username": username, "logged_in": True},
            },
        )
        assert response.status_code == 200, response.text
        responses.append(response.json())

    assert [event["event_type"] for event in memory.events] == ["publish"]
    assert responses[1]["profile_paused"] is True
    assert responses[1]["identity_switch_required"] is True
    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    store = V2EXNodeAffinityStore(database)
    assert store.scores(username="alice")[0]["published_topic_count"] == 1
    assert store.scores(username="bob") == []


def test_v2ex_profile_rebuild_stages_new_identity_without_early_activation(
    tmp_path: Path,
) -> None:
    from openbiliclaw.api.source_auth.probe_cache import LIVE_PROBES
    from openbiliclaw.sources.v2ex_affinity import V2EXNodeAffinityStore

    LIVE_PROBES.clear("v2ex")
    client, database, queue, memory = _profile_client(tmp_path)
    database.activate_v2ex_profile_identity("alice")
    task_id = queue.enqueue_with_id(
        "bootstrap_profile",
        {
            "scopes": ["public_topics"],
            "username": "bob",
            "profile_rebuild": True,
        },
        daily_budget=0,
    )
    assert task_id is not None

    response = client.post(
        "/api/sources/v2ex/task-result",
        json={
            "task_id": task_id,
            "status": "ok",
            "items": [
                {
                    "scope": "public_topics",
                    "topic_id": "321",
                    "title": "Bob profile rebuild",
                    "node_name": "python",
                }
            ],
            "debug": {"username": "bob", "logged_in": True},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert database.get_v2ex_profile_identity()[0] == "alice"
    assert V2EXNodeAffinityStore(database).scores(username="bob")[0]["published_topic_count"] == 1
    assert memory.events == []
