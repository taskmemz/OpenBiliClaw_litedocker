"""Tests for the extension-backed Linux.do discovery producer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _Soul:
    def __init__(self, interests: list[str] | None = None) -> None:
        self.interests = interests or []
        self.calls = 0

    async def get_profile(self) -> object:
        self.calls += 1
        return SimpleNamespace(
            preferences=SimpleNamespace(
                interests=[SimpleNamespace(name=name) for name in self.interests]
            )
        )


class _Pipeline:
    def __init__(
        self,
        *,
        full_for_source: bool = False,
        on_candidates_enqueued: Callable[[int], None] | None = None,
    ) -> None:
        self.items: list[Any] = []
        self.source_contexts: list[str] = []
        self.full_for_source = full_for_source
        self.full_source_calls: list[str | None] = []
        self.drained = False
        self.on_candidates_enqueued = on_candidates_enqueued

    def pool_full_for_source(self, source: str | None) -> bool:
        self.full_source_calls.append(source)
        return self.full_for_source

    def pool_full(self) -> bool:
        raise AssertionError("share-aware pool gate should be used")

    def enqueue_candidates(self, items: list[Any], *, source_context: str = "") -> int:
        self.items.extend(items)
        self.source_contexts.append(source_context)
        if items and self.on_candidates_enqueued is not None:
            self.on_candidates_enqueued(len(items))
        return len(items)

    async def drain_pending(self, *, profile: object, batch_size: int) -> dict[str, int]:
        del profile, batch_size
        self.drained = True
        return {"evaluated": 0, "cached": 0, "rejected": 0}


class _KeywordFetch:
    def __init__(self, claimed: list[Any], *, enabled: bool = True) -> None:
        self.claimed = claimed
        self.enabled = enabled
        self.platforms: list[str] = []
        self.used: list[Any] = []
        self.failed: list[Any] = []
        self.rolled_back: list[Any] = []

    def should_claim(self) -> bool:
        return self.enabled

    def claim(self, platform: str, n: int | None = None) -> list[Any]:
        del n
        self.platforms.append(platform)
        return list(self.claimed)

    def mark_used(self, claimed: list[Any]) -> None:
        self.used.extend(claimed)

    def mark_failed(self, claimed: list[Any]) -> None:
        self.failed.extend(claimed)

    def rollback(self, claimed: Any) -> None:
        self.rolled_back.append(claimed)


class _Queue:
    def __init__(
        self,
        results: dict[str, dict[str, Any]],
        *,
        rejected_types: set[str] | None = None,
    ) -> None:
        self.results = results
        self.rejected_types = rejected_types or set()
        self.enqueued: list[tuple[str, dict[str, object], int]] = []
        self.expired: list[tuple[tuple[str, ...], float]] = []
        self.failed: list[tuple[str, str]] = []
        self.retained: list[tuple[str, int]] = []
        self.page_cursors: dict[tuple[str, str], dict[str, int]] = {}
        self.cursor_updates: list[tuple[str, str, dict[str, int]]] = []

    def expire_stale_pending(
        self,
        task_types: tuple[str, ...],
        *,
        older_than_seconds: float,
    ) -> int:
        self.expired.append((task_types, older_than_seconds))
        return 0

    def enqueue_with_id(
        self,
        task_type: str,
        payload: dict[str, object],
        *,
        daily_budget: int = 100,
    ) -> str | None:
        self.enqueued.append((task_type, payload, daily_budget))
        if task_type in self.rejected_types:
            return None
        return f"{task_type}-task"

    def get(self, task_id: str) -> dict[str, object]:
        task_type = task_id.removesuffix("-task")
        result = self.results.get(task_type, {"items": []})
        status = str(result.get("_status", "completed"))
        payload = {key: value for key, value in result.items() if key != "_status"}
        task_payload = next(
            (
                enqueued_payload
                for enqueued_type, enqueued_payload, _budget in reversed(self.enqueued)
                if enqueued_type == task_type
            ),
            {},
        )
        return {
            "id": task_id,
            "type": task_type,
            "status": status,
            "payload_json": json.dumps(task_payload, ensure_ascii=False),
            "result_json": json.dumps(payload, ensure_ascii=False),
        }

    def fail(self, task_id: str, *, error: str) -> None:
        self.failed.append((task_id, error))

    def record_retained(self, task_id: str, count: int) -> None:
        self.retained.append((task_id, count))

    def discovery_page_cursor(self, source: str, input_value: str) -> dict[str, int]:
        return dict(self.page_cursors.get((source, input_value), {"page": 0, "offset": 0}))

    def set_discovery_page_cursor(
        self,
        source: str,
        input_value: str,
        position: dict[str, int],
    ) -> None:
        normalized = dict(position)
        self.page_cursors[(source, input_value)] = normalized
        self.cursor_updates.append((source, input_value, normalized))


def test_linuxdo_builder_requires_scheduler_only_for_daemon_runtime(tmp_path: Path) -> None:
    from openbiliclaw.config import Config
    from openbiliclaw.runtime.linuxdo_producer import (
        LinuxdoDiscoveryProducer,
        build_linuxdo_discovery_producer,
    )
    from openbiliclaw.storage.database import Database

    config = Config(data_dir=str(tmp_path / "data"))
    config.sources.linuxdo.enabled = True
    config.scheduler.enabled = False
    database = Database(tmp_path / "linuxdo-builder.db")
    database.initialize()

    assert (
        build_linuxdo_discovery_producer(
            config=config,
            database=database,
            soul_engine=_Soul(),
        )
        is None
    )
    explicit = build_linuxdo_discovery_producer(
        config=config,
        database=database,
        soul_engine=_Soul(),
        require_scheduler=False,
    )

    assert isinstance(explicit, LinuxdoDiscoveryProducer)
    assert explicit.poll_interval_seconds == 3


@pytest.mark.asyncio
async def test_linuxdo_producer_claims_search_keywords_and_enqueues_canonical_candidate() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    claimed = SimpleNamespace(id=11, keyword="本地大模型")
    keyword_fetch = _KeywordFetch([claimed])
    queue = _Queue(
        {
            "search": {
                "items": [
                    {
                        "source_strategy": "linuxdo-search",
                        "search_keyword": "本地大模型",
                        "topic_id": 101,
                        "title": "部署经验",
                        "author": "alice",
                        "summary": "一份经验总结",
                        "views": 50,
                        "like_count": 7,
                        "reply_count": 3,
                    }
                ]
            }
        }
    )
    pipeline = _Pipeline()
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=pipeline,
        keyword_fetch=keyword_fetch,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["reason"] == "ok"
    assert result["discovered"] == 1
    assert result["enqueued"] == 1
    assert keyword_fetch.platforms == ["linuxdo"]
    assert keyword_fetch.used == [claimed]
    assert keyword_fetch.failed == []
    assert queue.enqueued == [
        (
            "search",
            {
                "keywords": ["本地大模型"],
                "max_items_per_keyword": 5,
                "max_items": 5,
                "hydrate_topic_details": True,
                "source_keyword_ids": {"本地大模型": 11},
                "cursor_contract": "page-offset-v1",
                "start_cursors": {"本地大模型": {"page": 0, "offset": 0}},
                "request_interval_seconds": 0.5,
            },
            0,
        )
    ]
    candidate = pipeline.items[0]
    assert candidate.source_platform == "linuxdo"
    assert candidate.source_strategy == "linuxdo-search"
    assert candidate.source_keyword_id == 11
    assert candidate.content_id == "topic:101"
    assert candidate.content_type == "post"
    assert candidate.view_count == 50
    assert candidate.like_count == 7
    assert candidate.comment_count == 3
    assert candidate.score_threshold == 0.60
    assert pipeline.source_contexts == ["linuxdo-search"]
    assert pipeline.drained is True


@pytest.mark.asyncio
async def test_linuxdo_producer_schedules_all_non_search_modes_with_independent_budgets() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue(
        {
            "hot": {
                "items": [
                    {
                        "scope": "linuxdo_hot",
                        "topic_id": 201,
                        "title": "热门",
                        "author": "hot-user",
                    }
                ]
            },
            "feed": {
                "items": [
                    {
                        "scope": "linuxdo_feed",
                        "topic_id": 202,
                        "title": "最新",
                        "author": "feed-user",
                    }
                ]
            },
            "creator": {
                "items": [{"scope": "linuxdo_creator", "topic_id": 203, "title": "作者主题"}]
            },
            "related": {
                "items": [{"scope": "linuxdo_related", "topic_id": 204, "title": "相关主题"}]
            },
        }
    )
    pipeline = _Pipeline()
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=pipeline,
        sources=("hot", "feed", "creator", "related"),
        creator_seed_loader=lambda: ["https://linux.do/u/demo/activity/topics"],
        related_seed_loader=lambda: ["https://linux.do/t/199"],
        daily_hot_budget=2,
        daily_feed_budget=3,
        daily_creator_budget=4,
        daily_related_budget=5,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=8)

    assert result["reason"] == "ok"
    assert result["discovered"] == 4
    assert [task_type for task_type, _payload, _budget in queue.enqueued] == [
        "hot",
        "feed",
        "creator",
        "related",
    ]
    assert [budget for _task_type, _payload, budget in queue.enqueued] == [2, 3, 4, 5]
    assert queue.enqueued[0][1] == {
        "max_items": 8,
        "cursor_contract": "page-offset-v1",
        "start_cursors": {"default": {"page": 0, "offset": 0}},
        "request_interval_seconds": 0.5,
    }
    assert queue.enqueued[1][1] == {
        "max_items": 8,
        "cursor_contract": "page-offset-v1",
        "start_cursors": {"default": {"page": 0, "offset": 0}},
        "request_interval_seconds": 0.5,
    }
    assert queue.enqueued[2][1] == {
        "creator_urls": ["https://linux.do/u/demo/activity/topics"],
        "max_items_per_creator": 8,
        "cursor_contract": "page-offset-v1",
        "start_cursors": {"https://linux.do/u/demo/activity/topics": {"page": 0, "offset": 0}},
        "request_interval_seconds": 0.5,
    }
    assert queue.enqueued[3][1] == {
        "related_urls": ["https://linux.do/t/199"],
        "max_items_per_seed": 8,
        "max_items": 8,
        "hydrate_topic_details": True,
        "request_interval_seconds": 0.5,
    }
    assert pipeline.source_contexts == [
        "linuxdo-hot",
        "linuxdo-feed",
        "linuxdo-creator",
        "linuxdo-related",
    ]


@pytest.mark.asyncio
async def test_linuxdo_producer_passes_configured_request_interval_to_task() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue({"hot": {"items": []}})
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        sources=("hot",),
        min_interval_minutes=0,
        wait_seconds=0,
        poll_interval_seconds=1.75,
        kick=lambda: None,
    )

    await producer.produce_if_due(limit=4)

    assert queue.enqueued[0][1]["request_interval_seconds"] == 1.75


@pytest.mark.asyncio
async def test_linuxdo_producer_commits_complete_cursor_but_not_degraded_cursor() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    complete_queue = _Queue(
        {
            "feed": {
                "_openbiliclaw_terminal_status": "ok",
                "items": [{"scope": "linuxdo_feed", "topic_id": 91, "title": "cursor"}],
                "next_cursors": {"default": {"page": 4, "offset": 6}},
            }
        }
    )
    complete_queue.page_cursors[("feed", "")] = {"page": 2, "offset": 3}
    complete = LinuxdoDiscoveryProducer(
        task_queue=complete_queue,
        soul_engine=_Soul(),
        sources=("feed",),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    await complete.produce_if_due(limit=1)

    assert complete_queue.enqueued[0][1]["start_cursors"] == {"default": {"page": 2, "offset": 3}}
    assert complete_queue.cursor_updates == [("feed", "", {"page": 4, "offset": 6})]

    degraded_queue = _Queue(
        {
            "feed": {
                "_openbiliclaw_terminal_status": "degraded",
                "items": [{"scope": "linuxdo_feed", "topic_id": 92, "title": "partial"}],
                "next_cursors": {"default": {"page": 9, "offset": 9}},
                "debug": {"input_errors": {"feed": "linuxdo_rate_limited"}},
            }
        }
    )
    degraded = LinuxdoDiscoveryProducer(
        task_queue=degraded_queue,
        soul_engine=_Soul(),
        sources=("feed",),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await degraded.produce_if_due(limit=1)

    assert result["reason"] == "degraded"
    assert degraded_queue.cursor_updates == []


@pytest.mark.asyncio
async def test_linuxdo_producer_derives_creator_and_related_seeds_from_same_run() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue(
        {
            "hot": {
                "items": [
                    {
                        "scope": "linuxdo_hot",
                        "topic_id": 301,
                        "title": "热门",
                        "author": "alice",
                    }
                ]
            },
            "creator": {
                "items": [{"scope": "linuxdo_creator", "topic_id": 302, "title": "作者主题"}]
            },
            "related": {
                "items": [{"scope": "linuxdo_related", "topic_id": 303, "title": "相关主题"}]
            },
        }
    )
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=_Pipeline(),
        sources=("hot", "creator", "related"),
        creator_seed_loader=lambda: [],
        related_seed_loader=lambda: [],
        max_seed_count=1,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=6)

    assert result["reason"] == "ok"
    assert queue.enqueued[1][0] == "creator"
    assert queue.enqueued[1][1]["creator_urls"] == ["https://linux.do/u/alice/activity/topics"]
    assert queue.enqueued[2][0] == "related"
    assert queue.enqueued[2][1]["related_urls"] == ["https://linux.do/t/301"]


@pytest.mark.asyncio
async def test_linuxdo_producer_rolls_back_unfetched_keyword_when_budget_rejects_task() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    claimed = SimpleNamespace(id=21, keyword="数据库")
    keyword_fetch = _KeywordFetch([claimed])
    producer = LinuxdoDiscoveryProducer(
        task_queue=_Queue({}, rejected_types={"search"}),
        soul_engine=_Soul(),
        keyword_fetch=keyword_fetch,
        daily_search_budget=1,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result == {"discovered": 0, "reason": "budget_exhausted"}
    assert keyword_fetch.rolled_back == [claimed]
    assert keyword_fetch.used == []
    assert keyword_fetch.failed == []


@pytest.mark.asyncio
async def test_linuxdo_producer_preserves_partial_search_and_rolls_back_transient_remainder() -> (
    None
):
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    first = SimpleNamespace(id=31, keyword="Agent")
    second = SimpleNamespace(id=32, keyword="数据库")
    keyword_fetch = _KeywordFetch([first, second])
    queue = _Queue(
        {
            "search": {
                "_openbiliclaw_terminal_status": "degraded",
                "debug": {
                    "input_errors": {"search:数据库": "linuxdo_rate_limited"},
                },
                "items": [
                    {
                        "scope": "linuxdo_search",
                        "search_keyword": "Agent",
                        "topic_id": 401,
                        "title": "Agent 主题",
                    }
                ],
            }
        }
    )
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=_Pipeline(),
        keyword_fetch=keyword_fetch,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["discovered"] == 1
    assert keyword_fetch.used == [first]
    assert keyword_fetch.rolled_back == [second]
    assert keyword_fetch.failed == []


@pytest.mark.asyncio
async def test_linuxdo_producer_global_limit_controls_retained_keyword_lifecycle() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    first = SimpleNamespace(id=33, keyword="Agent")
    second = SimpleNamespace(id=34, keyword="数据库")
    keyword_fetch = _KeywordFetch([first, second])
    items = [
        {
            "scope": "linuxdo_search",
            "search_keyword": keyword,
            "topic_id": topic_id,
            "title": f"{keyword} {topic_id}",
        }
        for keyword, topic_ids in (("Agent", range(410, 415)), ("数据库", range(420, 425)))
        for topic_id in topic_ids
    ]
    producer = LinuxdoDiscoveryProducer(
        task_queue=_Queue({"search": {"items": items}}),
        soul_engine=_Soul(),
        candidate_pipeline=_Pipeline(),
        keyword_fetch=keyword_fetch,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["discovered"] == 5
    assert keyword_fetch.used == [first]
    assert keyword_fetch.failed == []
    assert keyword_fetch.rolled_back == [second]


@pytest.mark.asyncio
async def test_linuxdo_producer_applies_one_global_limit_after_cross_branch_dedup() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    hot = [
        {"scope": "linuxdo_hot", "topic_id": topic_id, "title": f"hot {topic_id}"}
        for topic_id in range(801, 806)
    ]
    feed = [
        {"scope": "linuxdo_feed", "topic_id": topic_id, "title": f"feed {topic_id}"}
        for topic_id in range(804, 809)
    ]
    pipeline = _Pipeline()
    queue = _Queue({"hot": {"items": hot}, "feed": {"items": feed}})
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=pipeline,
        sources=("hot", "feed"),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["discovered"] == 5
    assert result["enqueued"] == 5
    assert result["source_counts"] == {"linuxdo-hot": 3, "linuxdo-feed": 2}
    assert len(pipeline.items) == 5
    assert {item.content_id for item in pipeline.items} == {
        "topic:801",
        "topic:802",
        "topic:803",
        "topic:804",
        "topic:805",
    }
    assert [task_type for task_type, _payload, _budget in queue.enqueued] == ["hot", "feed"]
    assert queue.retained == [("hot-task", 3), ("feed-task", 2)]


@pytest.mark.asyncio
async def test_linuxdo_duplicate_only_candidates_do_not_consume_daily_budget() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    class _DuplicatePipeline(_Pipeline):
        def enqueue_candidates(self, items: list[Any], *, source_context: str = "") -> int:
            self.items.extend(items)
            self.source_contexts.append(source_context)
            return 0

    queue = _Queue(
        {"hot": {"items": [{"scope": "linuxdo_hot", "topic_id": 810, "title": "duplicate"}]}}
    )
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=_DuplicatePipeline(),
        sources=("hot",),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["discovered"] == 1
    assert result["enqueued"] == 0
    assert queue.retained == [("hot-task", 0)]


@pytest.mark.asyncio
async def test_linuxdo_producer_marks_definitive_empty_search_failed() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    claimed = SimpleNamespace(id=41, keyword="没有结果")
    keyword_fetch = _KeywordFetch([claimed])
    producer = LinuxdoDiscoveryProducer(
        task_queue=_Queue({"search": {"items": []}}),
        soul_engine=_Soul(),
        keyword_fetch=keyword_fetch,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result == {"discovered": 0, "reason": "empty"}
    assert keyword_fetch.failed == [claimed]
    assert keyword_fetch.rolled_back == []


@pytest.mark.asyncio
async def test_linuxdo_producer_uses_profile_fallback_when_keyword_claiming_is_disabled() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    keyword_fetch = _KeywordFetch([], enabled=False)
    queue = _Queue(
        {
            "search": {
                "items": [
                    {
                        "scope": "linuxdo_search",
                        "search_keyword": "Python",
                        "topic_id": 501,
                        "title": "Python 主题",
                    }
                ]
            }
        }
    )
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(["Python", "自托管"]),
        keyword_fetch=keyword_fetch,
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=2)

    assert result["reason"] == "ok"
    assert queue.enqueued[0][1]["keywords"] == ["Python", "自托管"]
    assert "source_keyword_ids" not in queue.enqueued[0][1]
    assert keyword_fetch.platforms == []


@pytest.mark.asyncio
async def test_linuxdo_producer_uses_share_aware_pool_gate_before_profile_or_budget() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    soul = _Soul(["AI"])
    pipeline = _Pipeline(full_for_source=True)
    queue = _Queue({})
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=soul,
        candidate_pipeline=pipeline,
        min_interval_minutes=0,
    )

    result = await producer.produce_if_due(limit=5)

    assert result == {"discovered": 0, "reason": "pool_full"}
    assert pipeline.full_source_calls == ["linuxdo"]
    assert soul.calls == 0
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_linuxdo_producer_applies_in_process_cadence_floor() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue(
        {"hot": {"items": [{"scope": "linuxdo_hot", "topic_id": 601, "title": "热门主题"}]}}
    )
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=_Pipeline(),
        sources=("hot",),
        min_interval_minutes=3,
        wait_seconds=0,
        kick=lambda: None,
    )

    first = await producer.produce_if_due(limit=5)
    second = await producer.produce_if_due(limit=5)

    assert first["reason"] == "ok"
    assert second == {"discovered": 0, "reason": "throttled"}
    assert len(queue.enqueued) == 1


@pytest.mark.asyncio
async def test_linuxdo_producer_throttles_after_executed_empty_round() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue({"hot": {"items": []}})
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        sources=("hot",),
        min_interval_minutes=3,
        wait_seconds=0,
        kick=lambda: None,
    )

    first = await producer.produce_if_due(limit=5)
    second = await producer.produce_if_due(limit=5)

    assert first == {"discovered": 0, "reason": "empty"}
    assert second == {"discovered": 0, "reason": "throttled"}
    assert len(queue.enqueued) == 1


@pytest.mark.asyncio
async def test_linuxdo_producer_fails_unclaimed_task_as_stale_without_stamping_cadence() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue({"hot": {"_status": "pending"}})
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        sources=("hot",),
        min_interval_minutes=3,
        wait_seconds=0,
        kick=lambda: None,
    )

    first = await producer.produce_if_due(limit=5)
    second = await producer.produce_if_due(limit=5)

    assert first["reason"] == "timeout"
    assert second["reason"] == "timeout"
    assert queue.failed == [
        ("hot-task", "stale_pending"),
        ("hot-task", "stale_pending"),
    ]
    assert len(queue.enqueued) == 2


@pytest.mark.asyncio
async def test_linuxdo_producer_fails_claimed_task_when_extension_result_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openbiliclaw.runtime import linuxdo_producer

    monkeypatch.setattr(linuxdo_producer, "linuxdo_task_timeout_seconds", lambda *_args: 0)
    monkeypatch.setattr(linuxdo_producer, "LINUXDO_TASK_RESULT_GRACE_SECONDS", 0)
    queue = _Queue({"hot": {"_status": "in_progress"}})
    producer = linuxdo_producer.LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        sources=("hot",),
        min_interval_minutes=3,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)
    next_result = await producer.produce_if_due(limit=5)

    assert result["reason"] == "timeout"
    assert next_result == {"discovered": 0, "reason": "throttled"}
    assert queue.failed == [("hot-task", "extension_result_timeout")]


@pytest.mark.asyncio
async def test_linuxdo_producer_preserves_partial_items_and_reports_degraded_branch() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    queue = _Queue(
        {
            "hot": {
                "_openbiliclaw_terminal_status": "degraded",
                "error": "pagination_incomplete",
                "items": [
                    {
                        "scope": "linuxdo_hot",
                        "topic_id": 901,
                        "title": "partial hot topic",
                    }
                ],
            },
            "feed": {
                "items": [
                    {
                        "scope": "linuxdo_feed",
                        "topic_id": 902,
                        "title": "complete feed topic",
                    }
                ]
            },
        }
    )
    pipeline = _Pipeline()
    producer = LinuxdoDiscoveryProducer(
        task_queue=queue,
        soul_engine=_Soul(),
        candidate_pipeline=pipeline,
        sources=("hot", "feed"),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["reason"] == "degraded"
    assert result["discovered"] == 2
    assert result["branch_errors"] == [
        {
            "source": "hot",
            "status": "degraded",
            "error": "pagination_incomplete",
        }
    ]
    assert {item.content_id for item in pipeline.items} == {"topic:901", "topic:902"}


@pytest.mark.asyncio
async def test_linuxdo_producer_can_defer_candidate_evaluation_to_shared_coordinator() -> None:
    from openbiliclaw.runtime.linuxdo_producer import LinuxdoDiscoveryProducer

    notifications: list[int] = []
    pipeline = _Pipeline(on_candidates_enqueued=notifications.append)
    producer = LinuxdoDiscoveryProducer(
        task_queue=_Queue(
            {"hot": {"items": [{"scope": "linuxdo_hot", "topic_id": 701, "title": "热门主题"}]}}
        ),
        soul_engine=_Soul(),
        candidate_pipeline=pipeline,
        candidate_evaluation_owned_by_coordinator=True,
        sources=("hot",),
        min_interval_minutes=0,
        wait_seconds=0,
        kick=lambda: None,
    )

    result = await producer.produce_if_due(limit=5)

    assert result["enqueued"] == 1
    assert notifications == [1]
    assert pipeline.drained is False
    assert "cached" not in result
