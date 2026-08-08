from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from openbiliclaw.discovery.candidate_pipeline import DiscoveryCandidatePipeline
from openbiliclaw.discovery.candidate_pool import DiscoveryCandidateWrite
from openbiliclaw.discovery.engine import ContentDiscoveryEngine
from openbiliclaw.llm.base import LLMResponse
from openbiliclaw.llm.concurrency import LLMConcurrencyGate
from openbiliclaw.llm.service import LLMService
from openbiliclaw.memory.manager import MemoryManager
from openbiliclaw.recommendation.engine import RecommendationEngine
from openbiliclaw.runtime.candidate_eval import CandidateEvalCoordinator, CandidateEvalSnapshot
from openbiliclaw.runtime.expression_copy import ExpressionCopyCoordinator
from openbiliclaw.soul.profile import InterestTag, PreferenceLayer, SoulProfile
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


def _profile() -> SoulProfile:
    return SoulProfile(
        core_traits=["curious"],
        preferences=PreferenceLayer(
            interests=[InterestTag(name="software engineering", category="technology", weight=0.9)]
        ),
    )


class _ControlledRegistry:
    """Keyed deterministic registry with provider-boundary telemetry only."""

    default_provider = "controlled"

    def __init__(self, gate: LLMConcurrencyGate) -> None:
        self.gate = gate
        self.active = 0
        self.peak_provider = 0
        self.peak_total = 0
        self.peak_background = 0
        self.active_expression = 0
        self.peak_expression = 0
        self.expression_batch_sizes: list[int] = []
        self.evaluation_batch_sizes: list[int] = []
        self.evaluation_call_count = 0
        self.expression_barrier_expected = 0
        self.expression_barrier_ready = asyncio.Event()
        self.expression_barrier_release = asyncio.Event()

    def is_chat_capable(self, name: str) -> bool:
        return name == self.default_provider

    async def complete(self, messages: list[dict[str, str]], **_kwargs: object) -> LLMResponse:
        self.active += 1
        self.peak_provider = max(self.peak_provider, self.active)
        status = self.gate.status_payload()
        self.peak_total = max(self.peak_total, int(status["llm_total_active"]))
        self.peak_background = max(self.peak_background, int(status["llm_background_active"]))
        try:
            await asyncio.sleep(0)
            raw = messages[-1]["content"]
            is_expression = '"expression"' in messages[0]["content"]
            if is_expression:
                self.active_expression += 1
                self.peak_expression = max(self.peak_expression, self.active_expression)
                rows = _tagged_json(raw, "content_batch")
                self.expression_batch_sizes.append(len(rows))
                if (
                    self.expression_barrier_expected > 0
                    and self.active_expression >= self.expression_barrier_expected
                ):
                    self.expression_barrier_ready.set()
                if self.expression_barrier_expected > 0:
                    await self.expression_barrier_release.wait()
                payload = [
                    {
                        "bvid": str(row.get("bvid", "")),
                        "expression": f"Recommendation {row.get('bvid', '')}.",
                        "topic_label": "Technology",
                    }
                    for row in rows
                ]
            else:
                rows = _tagged_json(raw, "content_batch")
                self.evaluation_batch_sizes.append(len(rows))
                self.evaluation_call_count += 1
                payload = [
                    {
                        "id": str(row["id"]),
                        "score": 0.9,
                        "reason": "relevant",
                        "topic_group": (f"technology-{self.evaluation_call_count}-{row['id']}"),
                        "style_key": "deep_dive",
                    }
                    for row in rows
                ]
            return LLMResponse(content=json.dumps(payload), provider="controlled", model="test")
        finally:
            if "is_expression" in locals() and is_expression:
                self.active_expression -= 1
            self.active -= 1

    async def complete_provider(
        self, provider_name: str, messages: list[dict[str, str]], **kwargs: object
    ) -> LLMResponse:
        assert provider_name == self.default_provider
        return await self.complete(messages, **kwargs)


def _tagged_json(raw: str, tag: str) -> list[dict[str, Any]]:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", raw, re.S)
    assert match is not None
    value = json.loads(match.group(1))
    if isinstance(value, dict):
        value = value.get("items", [])
    assert isinstance(value, list)
    return [dict(item) for item in value if isinstance(item, dict)]


def _ready(
    db: Database,
    prefix: str,
    count: int,
    *,
    source: str = "search",
    precomputed: bool = True,
) -> list[str]:
    result: list[str] = []
    for index in range(count):
        content_id = f"{prefix}{index:04d}"
        db.cache_content(
            content_id,
            title=f"Item {index}",
            up_name="Public author",
            source=source,
            source_platform="zhihu" if source.startswith("zhihu") else "bilibili",
            relevance_score=0.9,
            relevance_reason="relevant",
            pool_expression=f"Recommendation {content_id}." if precomputed else "",
            pool_topic_label="Technology" if precomputed else "",
            style_key="deep_dive",
            topic_group=f"technology-{content_id}",
        )
        result.append(content_id)
    return result


def _raw(prefix: str, count: int, *, source: str = "search") -> list[DiscoveryCandidateWrite]:
    platform = "zhihu" if source.startswith("zhihu") else "bilibili"
    return [
        DiscoveryCandidateWrite(
            candidate_key=f"{platform}:{prefix}{index:04d}",
            source_platform=platform,
            source_strategy=source,
            content_id=f"{prefix}{index:04d}",
            title=f"Raw {index}",
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_user_a_refills_for_fifty_sustained_rounds_without_leaks(tmp_path: Path) -> None:
    db = Database(tmp_path / "user-a.db")
    db.initialize()
    memory = MemoryManager(tmp_path / "user-a-memory", database=db)
    gate = LLMConcurrencyGate(4)
    gate.update_inventory(available=8, target=16)
    registry = _ControlledRegistry(gate)
    service = LLMService(registry=registry, memory=memory, concurrency_gate=gate)
    profile = _profile()
    initial = _ready(db, "BVA", 16)
    assert db.enqueue_discovery_candidates(_raw("A-raw-", 602)) == 602
    before = db.maintain_pool_inventory(
        target=16,
        raw_ceiling=600,
        source_share_quotas={"bilibili": 600},
        max_per_topic_group=3,
    )
    assert before.available_after >= 16
    assert before.raw_after <= 600
    for content_id in initial[:8]:
        db.insert_recommendation(content_id, confidence=0.9, presented=1)

    pipeline = DiscoveryCandidatePipeline(
        database=db,
        discovery_engine=ContentDiscoveryEngine(llm_service=service, database=db),
        pool_target_count=16,
    )
    recommendation = RecommendationEngine(llm=service, database=db, expression_batch_concurrency=2)

    def snapshot() -> CandidateEvalSnapshot:
        statuses = db.count_discovery_candidates_by_status()
        return CandidateEvalSnapshot(
            available=db.count_pool_candidates(),
            target=16,
            pending_eval=statuses.get("pending_eval", 0),
            evaluating=statuses.get("evaluating", 0),
            evaluated_pending_admission=statuses.get("evaluated", 0),
            admitted_pending_copy=len(db.get_pool_candidates_needing_copy(limit=1000)),
        )

    copy = ExpressionCopyCoordinator(
        pending_count_provider=lambda: len(db.get_pool_candidates_needing_copy(limit=1000)),
        drain_callback=lambda limit: recommendation._drain_expression_copy(
            profile=profile, limit=limit, batch_size=30
        ),
    )
    evaluator = CandidateEvalCoordinator(
        pipeline=pipeline,
        snapshot_provider=snapshot,
        profile_provider=lambda: profile,
        worker_count=3,
        batch_size=30,
        on_admitted=lambda _count: copy.notify("candidate_admitted"),
        safety_wake_seconds=60,
    )
    copy_task = asyncio.create_task(copy.run_forever())
    eval_task = asyncio.create_task(evaluator.run_forever())
    evaluator.notify("initial_consumption")

    seen: set[str] = set(initial[:8])
    async with asyncio.timeout(15):
        while db.count_pool_candidates() < 16:
            await asyncio.sleep(0.01)
    for round_index in range(50):
        rows = db.get_pool_candidates(limit=100)
        assert rows
        for row in rows:
            content_id = str(row["bvid"])
            assert content_id not in seen
            seen.add(content_id)
            db.insert_recommendation(content_id, confidence=0.9, presented=1)
        db.enqueue_discovery_candidates(_raw(f"round-{round_index}-", 16))
        evaluator.notify(f"round:{round_index}")
        async with asyncio.timeout(2):
            while db.count_pool_candidates() == 0:
                await asyncio.sleep(0.005)

    after = db.maintain_pool_inventory(
        target=16,
        raw_ceiling=600,
        source_share_quotas={"bilibili": 600},
    )
    assert after.available_after >= min(after.available_before, 16)
    assert after.raw_after <= 600
    await evaluator.stop()
    await copy.stop()
    await eval_task
    await copy_task
    statuses = db.count_discovery_candidates_by_status()
    gate_status = gate.status_payload()
    assert statuses.get("evaluating", 0) == 0
    claim_count = db.conn.execute(
        "SELECT COUNT(*) FROM discovery_candidates WHERE claim_token IS NOT NULL"
    ).fetchone()[0]
    assert int(claim_count) == 0
    assert int(gate_status["llm_total_active"]) == 0
    assert int(gate_status["llm_total_waiting"]) == 0
    assert int(gate_status["llm_background_active"]) == 0
    assert int(gate_status["llm_background_waiting"]) == 0
    assert max(registry.expression_batch_sizes) <= 30
    assert registry.peak_expression <= 2
    assert registry.peak_total <= 4
    assert registry.peak_background <= 3


@pytest.mark.asyncio
async def test_sixty_pending_copy_rows_fan_out_as_two_thirty_item_requests(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "copy-fanout.db")
    db.initialize()
    memory = MemoryManager(tmp_path / "copy-fanout-memory", database=db)
    gate = LLMConcurrencyGate(4)
    gate.update_inventory(available=0, target=60)
    registry = _ControlledRegistry(gate)
    registry.expression_barrier_expected = 2
    service = LLMService(registry=registry, memory=memory, concurrency_gate=gate)
    _ready(db, "copy-", 60, precomputed=False)
    recommendation = RecommendationEngine(
        llm=service,
        database=db,
        expression_batch_concurrency=2,
    )

    task = asyncio.create_task(
        recommendation._drain_expression_copy(
            profile=_profile(),
            limit=60,
            batch_size=30,
        )
    )
    await asyncio.wait_for(registry.expression_barrier_ready.wait(), timeout=2)
    assert sorted(registry.expression_batch_sizes) == [30, 30]
    assert registry.peak_expression == 2
    assert registry.peak_total <= 4
    assert registry.peak_background <= 3
    registry.expression_barrier_release.set()
    assert await asyncio.wait_for(task, timeout=2) == 60
    status = gate.status_payload()
    assert int(status["llm_total_active"]) == 0
    assert int(status["llm_background_active"]) == 0


def test_user_b_zhihu_inventory_is_isolated_and_not_erased(tmp_path: Path) -> None:
    db = Database(tmp_path / "user-b.db")
    db.initialize()
    _ready(db, "zhihu-ready-", 10, source="zhihu-hot")
    assert (
        db.enqueue_discovery_candidates(_raw("zhihu-overflow-", 12, source="zhihu-creator")) == 12
    )
    before = db.count_pool_candidates()
    result = db.maintain_pool_inventory(
        target=10,
        raw_ceiling=20,
        source_share_quotas={"zhihu": 10},
    )
    assert before == 10
    assert result.available_after >= 10
    assert db.count_pool_candidates_by_source() == {"zhihu": 10}
    assert result.raw_after <= 20
