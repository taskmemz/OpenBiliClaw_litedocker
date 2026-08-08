from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

import pytest

from openbiliclaw.bilibili.api import BilibiliAPIClient
from openbiliclaw.config import load_config
from openbiliclaw.discovery.candidate_pipeline import DiscoveryCandidatePipeline
from openbiliclaw.discovery.engine import ContentDiscoveryEngine, DiscoveredContent
from openbiliclaw.integrations.openclaw.bootstrap import build_openclaw_adapter_services
from openbiliclaw.integrations.openclaw.operations import OpenClawAdapter
from openbiliclaw.llm.base import classify_llm_failure_kind
from openbiliclaw.llm.concurrency import LLMConcurrencyGate
from openbiliclaw.llm.registry import build_llm_registry
from openbiliclaw.llm.service import LLMService, module_overrides_from_config
from openbiliclaw.memory.manager import MemoryManager
from openbiliclaw.recommendation.engine import RecommendationEngine
from openbiliclaw.soul.profile import InterestTag, OnionProfile, PreferenceLayer, SoulProfile
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path

    from openbiliclaw.config import Config
    from openbiliclaw.llm.base import LLMRegistry

_LIVE = os.getenv("OPENBILICLAW_REFILL_E2E", "") == "1"
_LIVE_CONFIG_ENV = "OPENBILICLAW_REFILL_CONFIG"
_LIVE_PROVIDER_ENV = "OPENBILICLAW_REFILL_PROVIDER"
_LIVE_RANKING_RID = 188
_LIVE_RANKING_LIMIT = 8
_LIVE_OPENCLAW_POOL_TARGET = 300
_LIVE_OPENCLAW_DISCOVERY_LIMIT = 30
_LIVE_OPENCLAW_INLINE_EVAL_LIMIT = 4
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _LIVE, reason="set OPENBILICLAW_REFILL_E2E=1 for live refill E2E"),
]


def _load_live_config_and_registry(
    *,
    disable_embedding: bool = False,
) -> tuple[Config, LLMRegistry]:
    """Load an opt-in live-test config without changing persisted settings.

    The optional environment controls exist only for this explicitly enabled
    integration test. They let an operator choose a known-good configured
    provider while keeping normal config loading and all on-disk settings
    untouched.
    """

    config_path = os.getenv(_LIVE_CONFIG_ENV, "").strip()
    config = load_config(config_path) if config_path else load_config()
    requested_provider = os.getenv(_LIVE_PROVIDER_ENV, "").strip().lower()
    if requested_provider:
        # A selected live provider must win over every LLMService routing
        # bucket. Retaining an old module model (for example an Ollama model)
        # would still route the selected provider to the wrong model, so each
        # override deliberately falls back to the selected provider's model.
        config = replace(
            config,
            llm=replace(
                config.llm,
                default_provider=requested_provider,
                soul=replace(config.llm.soul, provider=requested_provider, model=""),
                discovery=replace(config.llm.discovery, provider=requested_provider, model=""),
                recommendation=replace(
                    config.llm.recommendation,
                    provider=requested_provider,
                    model="",
                ),
                evaluation=replace(config.llm.evaluation, provider=requested_provider, model=""),
            ),
        )
    if disable_embedding:
        # The OpenClaw one-shot latency proof is intentionally chat-provider
        # only.  Do this before building the registry so a locally configured
        # Ollama chat/embedding endpoint cannot even be registered or reached
        # by this opt-in SenseTime test.
        config = replace(
            config,
            llm=replace(
                config.llm,
                fallback_provider="",
                ollama=replace(config.llm.ollama, api_key="", base_url="", model=""),
                embedding=replace(
                    config.llm.embedding,
                    provider="",
                    model="",
                    api_key="",
                    base_url="",
                    fallback_enabled=False,
                    fallback_provider="",
                ),
            ),
        )
    registry = build_llm_registry(config)
    if requested_provider and registry.default_provider != requested_provider:
        raise RuntimeError("Requested live refill provider is unavailable.")
    return config, registry


def _profile() -> SoulProfile:
    return SoulProfile(
        core_traits=["curious"],
        preferences=PreferenceLayer(
            interests=[InterestTag(name="software engineering", category="technology", weight=0.9)]
        ),
    )


@dataclass
class _LiveMetrics:
    gate: LLMConcurrencyGate
    peak_total: int = 0
    peak_background: int = 0
    provider_round_count: int = 0
    provider_names: list[str] = field(default_factory=list)
    transient_retry_count: int = 0
    transient_registry_failures: int = 0
    retry_pending: set[str] = field(default_factory=set)
    expression_batch_sizes: list[int] = field(default_factory=list)
    provider_active: int = 0
    peak_provider_active: int = 0
    expression_active: int = 0
    peak_expression_active: int = 0
    expression_requests: list[_LiveExpressionRequest] = field(default_factory=list)
    copy_batch_attempts: list[_LiveCopyBatchAttempt] = field(default_factory=list)

    def observe_gate(self) -> None:
        status = self.gate.status_payload()
        self.peak_total = max(self.peak_total, int(status["llm_total_active"]))
        self.peak_background = max(self.peak_background, int(status["llm_background_active"]))


@dataclass
class _LiveExpressionRequest:
    """Sanitized timing record for one real expression-provider request."""

    caller: str
    batch_size: int
    started: float
    elapsed_seconds: float = 0.0
    outcome: str = "started"
    cancelled: bool = False


@dataclass
class _LiveCopyBatchAttempt:
    """Sanitized result for one copy batch before split-retry handling."""

    batch_size: int
    source: str = "recommendation._precompute_batch"
    elapsed_seconds: float = 0.0
    outcome: str = "started"
    cancelled: bool = False


def _content_batch_size(user_input: str) -> int:
    """Return a prompt's content batch size without retaining its contents."""

    match = re.search(r"<content_batch>\s*(.*?)\s*</content_batch>", user_input, re.S)
    if match is None:
        return 0
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return 0
    return len(value) if isinstance(value, list) else 0


def _expression_request_trace_summary(
    requests: list[_LiveExpressionRequest], *, origin: float
) -> str:
    """Format provider observations without exposing prompts or responses."""

    return (
        "["
        + ";".join(
            (
                f"caller={request.caller},batch={request.batch_size},"
                f"start={request.started - origin:.2f},elapsed={request.elapsed_seconds:.2f},"
                f"outcome={request.outcome},cancelled={request.cancelled}"
            )
            for request in requests
        )
        + "]"
    )


def _copy_batch_attempt_summary(attempts: list[_LiveCopyBatchAttempt]) -> str:
    """Format copy-validation observations without exposing item data."""

    return (
        "["
        + ";".join(
            (
                f"source={attempt.source},batch={attempt.batch_size},"
                f"elapsed={attempt.elapsed_seconds:.2f},outcome={attempt.outcome},"
                f"cancelled={attempt.cancelled}"
            )
            for attempt in attempts
        )
        + "]"
    )


class _MonitoredRegistry:
    def __init__(self, registry: Any, metrics: _LiveMetrics) -> None:
        self.registry = registry
        self.metrics = metrics
        self.default_provider = registry.default_provider

    def is_chat_capable(self, name: str) -> bool:
        return bool(self.registry.is_chat_capable(name))

    async def complete(self, messages: list[dict[str, str]], **kwargs: object) -> object:
        return await self._call("complete", None, messages, kwargs)

    async def complete_provider(
        self,
        provider_name: str,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> object:
        return await self._call("complete_provider", provider_name, messages, kwargs)

    async def _call(
        self,
        method: str,
        provider_name: str | None,
        messages: list[dict[str, str]],
        kwargs: dict[str, object],
    ) -> object:
        self.metrics.observe_gate()
        logical_caller = self._logical_caller(messages)
        request_fingerprint = self._request_fingerprint(
            logical_caller=logical_caller,
            method=method,
            provider_name=provider_name,
            messages=messages,
            kwargs=kwargs,
        )
        self.metrics.provider_round_count += 1
        self.metrics.provider_names.append(
            str(provider_name or self.default_provider).strip().lower()
        )
        if request_fingerprint in self.metrics.retry_pending:
            self.metrics.retry_pending.remove(request_fingerprint)
            self.metrics.transient_retry_count += 1
        self.metrics.provider_active += 1
        self.metrics.peak_provider_active = max(
            self.metrics.peak_provider_active, self.metrics.provider_active
        )
        is_expression = '"expression"' in messages[0]["content"]
        if is_expression:
            self.metrics.expression_active += 1
            self.metrics.peak_expression_active = max(
                self.metrics.peak_expression_active, self.metrics.expression_active
            )
            batch_size = _content_batch_size(messages[-1]["content"])
            if batch_size:
                self.metrics.expression_batch_sizes.append(batch_size)
        try:
            if method == "complete_provider":
                assert provider_name is not None
                return await self.registry.complete_provider(provider_name, messages, **kwargs)
            return await self.registry.complete(messages, **kwargs)
        except Exception as exc:
            if classify_llm_failure_kind(exc) in {
                "rate_limited",
                "timeout",
                "connection",
                "server_error",
            }:
                self.metrics.transient_registry_failures += 1
                self.metrics.retry_pending.add(request_fingerprint)
            raise
        finally:
            if is_expression:
                self.metrics.expression_active -= 1
            self.metrics.provider_active -= 1

    @staticmethod
    def _logical_caller(messages: list[dict[str, str]]) -> str:
        system = messages[0]["content"] if messages else ""
        if '"expression"' in system:
            return "refill.expression"
        if '"score"' in system:
            return "refill.evaluation"
        return "interactive_or_other"

    @staticmethod
    def _request_fingerprint(
        *,
        logical_caller: str,
        method: str,
        provider_name: str | None,
        messages: list[dict[str, str]],
        kwargs: dict[str, object],
    ) -> str:
        """Hash one exact logical request without retaining or logging its content."""

        structured_params = {
            key: value
            for key, value in kwargs.items()
            if key
            in {
                "temperature",
                "max_tokens",
                "json_mode",
                "reasoning_effort",
                "model",
            }
        }
        canonical = json.dumps(
            {
                "caller": logical_caller,
                "method": method,
                "provider": provider_name or "",
                "messages": messages,
                "params": structured_params,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: f"<{type(value).__name__}>",
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _BarrierRegistry:
    def __init__(
        self, registry: _MonitoredRegistry, metrics: _LiveMetrics, expected: int = 3
    ) -> None:
        self.registry = registry
        self.metrics = metrics
        self.default_provider = registry.default_provider
        self.expected = expected
        self.entered = 0
        self.ready = asyncio.Event()
        self.release = asyncio.Event()

    def is_chat_capable(self, name: str) -> bool:
        return bool(self.registry.is_chat_capable(name))

    async def complete(self, messages: list[dict[str, str]], **kwargs: object) -> object:
        self.metrics.observe_gate()
        self.entered += 1
        if self.entered >= self.expected:
            self.ready.set()
        await self.release.wait()
        return await self.registry.complete(messages, **kwargs)

    async def complete_provider(
        self,
        provider_name: str,
        messages: list[dict[str, str]],
        **kwargs: object,
    ) -> object:
        self.metrics.observe_gate()
        self.entered += 1
        if self.entered >= self.expected:
            self.ready.set()
        await self.release.wait()
        return await self.registry.complete_provider(provider_name, messages, **kwargs)


@pytest.mark.asyncio
async def test_real_provider_refill_and_interactive_fourth_slot(tmp_path: Path) -> None:
    print("live_refill_phase=config", flush=True)
    config, registry = _load_live_config_and_registry()
    db = Database(tmp_path / "live-refill.db")
    db.initialize()
    memory = MemoryManager(tmp_path / "memory", database=db)
    gate = LLMConcurrencyGate(total_concurrency=4)
    gate.update_inventory(available=0, target=8)
    metrics = _LiveMetrics(gate)
    monitored_registry = _MonitoredRegistry(registry, metrics)
    overrides = module_overrides_from_config(config)
    service = LLMService(
        registry=monitored_registry,
        memory=memory,
        concurrency=4,
        concurrency_gate=gate,
        module_overrides=overrides,
    )
    profile = _profile()

    client = BilibiliAPIClient(cookie="")
    try:
        async with asyncio.timeout(30):
            rows = (await client.get_ranking(rid=_LIVE_RANKING_RID))[:_LIVE_RANKING_LIMIT]
    finally:
        await client.close()
    items = [
        DiscoveredContent(
            bvid=str(row.get("bvid", "")),
            content_id=str(row.get("bvid", "")),
            source_platform="bilibili",
            source_strategy="trending",
            title=str(row.get("title", "")),
            up_name=str((row.get("owner") or {}).get("name", "")),
            description=str(row.get("desc", "")),
            view_count=int((row.get("stat") or {}).get("view", 0) or 0),
            like_count=int((row.get("stat") or {}).get("like", 0) or 0),
        )
        for row in rows
        if str(row.get("bvid", ""))
    ]
    print(
        f"live_refill_phase=ranking ranking_rid={_LIVE_RANKING_RID} fetched={len(items)}",
        flush=True,
    )
    assert items, "live fetched_count=0"
    pipeline = DiscoveryCandidatePipeline(
        database=db,
        discovery_engine=ContentDiscoveryEngine(llm_service=service, database=db),
        pool_target_count=8,
    )
    assert pipeline.enqueue_candidates(items) > 0
    claim = pipeline.claim_batch(limit=8)
    assert claim is not None
    print("live_refill_phase=evaluation_start", flush=True)
    async with asyncio.timeout(180):
        outcome = await pipeline.evaluate_claim(claim, profile)
    passing_scores = sum(
        score >= pipeline._threshold_for(row)
        for row, score in zip(claim.rows, outcome.scores, strict=True)
    )
    parser_unresolved = sum(
        item.relevance_reason == "evaluation_response_missing" for item in claim.items
    )
    result = await pipeline.complete_claim(outcome, admission_limit=8)
    print(
        f"live_refill_phase=evaluation_done evaluated={result['evaluated']} "
        f"passing_scores={passing_scores} parser_unresolved={parser_unresolved} "
        f"admitted={result['cached']} "
        f"rejected={result['rejected']}",
        flush=True,
    )
    assert result["evaluated"] > 0
    assert result["cached"] > 0, (
        f"live evaluated={result['evaluated']} passing_scores={passing_scores} "
        f"parser_unresolved={parser_unresolved} "
        f"admitted=0 rejected={result['rejected']}"
    )
    recommendation = RecommendationEngine(llm=service, database=db, expression_batch_concurrency=2)
    print("live_refill_phase=copy_start", flush=True)
    async with asyncio.timeout(180):
        copied = await recommendation._drain_expression_copy(
            profile=profile, limit=8, batch_size=30
        )
    print(f"live_refill_phase=copy_done copied={copied}", flush=True)
    assert copied > 0
    before = db.count_pool_candidates()
    assert before > 0
    maintained = db.maintain_pool_inventory(
        target=8,
        raw_ceiling=16,
        source_share_quotas={"bilibili": 8},
    )
    assert maintained.available_after >= min(before, 8)
    print(
        f"live_refill_phase=maintenance before={before} after={maintained.available_after}",
        flush=True,
    )

    consumed_rows = db.get_pool_candidates(limit=100)
    consumed_bvids = [str(row.get("bvid", "")) for row in consumed_rows]
    db.mark_pool_items_shown(consumed_bvids)
    assert db.count_pool_candidates() == 0
    replacement_items = [
        DiscoveredContent(
            bvid=f"BV1LIVE{index:04d}",
            content_id=f"BV1LIVE{index:04d}",
            source_platform="bilibili",
            source_strategy="search",
            title=title,
            up_name="OpenBiliClaw live refill test",
            description=description,
        )
        for index, (title, description) in enumerate(
            [
                ("Python asyncio 生产级并发控制实战", "讲解信号量、任务取消和背压设计。"),
                ("LLM Agent 调度器从零实现", "实现请求队列、并发池和可恢复任务状态。"),
                ("SQLite 事务与并发写入故障排查", "分析锁竞争、原子提交和崩溃恢复。"),
                ("分布式系统限流与令牌桶工程实践", "比较 RPM、TPM 和自适应退避策略。"),
            ],
            start=1,
        )
    ]
    assert pipeline.enqueue_candidates(replacement_items) == len(replacement_items)
    replacement_claim = pipeline.claim_batch(limit=len(replacement_items))
    assert replacement_claim is not None
    async with asyncio.timeout(180):
        replacement_outcome = await pipeline.evaluate_claim(replacement_claim, profile)
    replacement_result = await pipeline.complete_claim(
        replacement_outcome,
        admission_limit=len(replacement_items),
    )
    assert replacement_result["cached"] > 0
    async with asyncio.timeout(180):
        replacement_copied = await recommendation._drain_expression_copy(
            profile=profile,
            limit=len(replacement_items),
            batch_size=30,
        )
    available_after_refill = db.count_pool_candidates()
    print(
        "live_refill_consumed_cycle_summary "
        f"consumed={len(consumed_bvids)} available_after_consume=0 "
        f"evaluated={replacement_result['evaluated']} "
        f"admitted={replacement_result['cached']} copied={replacement_copied} "
        f"available_after_refill={available_after_refill}",
        flush=True,
    )
    assert replacement_copied > 0
    assert available_after_refill > 0

    barrier = _BarrierRegistry(monitored_registry, metrics)
    background = LLMService(
        registry=barrier,
        memory=memory,
        concurrency=4,
        concurrency_gate=gate,
        module_overrides=overrides,
    )
    interactive = LLMService(
        registry=monitored_registry,
        memory=memory,
        concurrency=4,
        concurrency_gate=gate,
        module_overrides=overrides,
    )
    background_tasks = [
        asyncio.create_task(
            background.complete_structured_task(
                system_instruction="Return a short JSON object.",
                user_input='Return {"ok": true}.',
                caller="discovery.evaluate_batch",
                max_tokens=32,
            )
        )
        for _ in range(3)
    ]
    print("live_refill_phase=interactive_barrier_start", flush=True)
    await asyncio.wait_for(barrier.ready.wait(), timeout=10)
    assert gate.status_payload()["llm_background_active"] == 3
    interactive_task = asyncio.create_task(
        interactive.complete_structured_task(
            system_instruction="Reply briefly.",
            user_input="Say OK.",
            caller="soul.dialogue",
            max_tokens=16,
        )
    )
    async with asyncio.timeout(10):
        while gate.status_payload()["llm_total_active"] < 4:
            await asyncio.sleep(0.01)
    print("live_refill_phase=interactive_fourth_entered", flush=True)
    barrier.release.set()
    async with asyncio.timeout(180):
        await asyncio.gather(*background_tasks, interactive_task)
    print("live_refill_phase=interactive_done", flush=True)
    status = gate.status_payload()
    assert status["llm_total_active"] == 0
    assert status["llm_background_active"] == 0
    assert metrics.peak_total <= 4
    assert metrics.peak_background <= 3
    assert metrics.peak_total == 4
    assert max(metrics.expression_batch_sizes) <= 30
    assert metrics.peak_expression_active <= 2
    assert metrics.transient_retry_count <= metrics.provider_round_count
    print(
        "live_refill_summary "
        f"fetched={len(items)} evaluated={result['evaluated']} copied={copied} "
        f"available_before={before} available_after={maintained.available_after} "
        f"peak_total={metrics.peak_total} peak_background={metrics.peak_background} "
        f"max_copy_batch={max(metrics.expression_batch_sizes)} "
        f"copy_fanout={metrics.peak_expression_active} "
        f"provider_round_count={metrics.provider_round_count} "
        f"transient_retry_count={metrics.transient_retry_count} "
        f"transient_registry_failures={metrics.transient_registry_failures}"
    )


@pytest.mark.asyncio
async def test_real_provider_copy_watermark_refills_without_draining_backlog(
    tmp_path: Path,
) -> None:
    """Exercise the §6.5 high watermark with a real configured provider."""

    config, registry = _load_live_config_and_registry()
    db = Database(tmp_path / "live-copy-watermark.db")
    db.initialize()
    memory = MemoryManager(tmp_path / "memory", database=db)
    gate = LLMConcurrencyGate(total_concurrency=4)
    metrics = _LiveMetrics(gate)
    monitored_registry = _MonitoredRegistry(registry, metrics)
    service = LLMService(
        registry=monitored_registry,
        memory=memory,
        concurrency=4,
        concurrency_gate=gate,
        module_overrides=module_overrides_from_config(config),
    )
    for index in range(5):
        db.cache_content(
            f"BV1WMARK{index:04d}",
            title=f"Production engineering practice {index}",
            up_name="OpenBiliClaw live gate",
            source="search",
            source_platform="bilibili",
            relevance_score=0.9,
            style_key="deep_focus",
            topic_group=f"watermark-{index}",
        )
    engine = RecommendationEngine(
        llm=service,
        database=db,
        copy_ready_target_count=2,
    )
    profile = _profile()

    async with asyncio.timeout(180):
        initial_copied = await engine.drain_pending_expression_copy(
            profile=profile,
            limit=60,
        )
    first = db.count_pool_readiness()
    serve_started = time.monotonic()
    served = await engine.serve_with_result(profile, limit=1)
    serve_elapsed_seconds = time.monotonic() - serve_started
    after_consume = db.count_pool_readiness()
    demand_after_consume = engine.count_pending_expression_copy_demand()
    async with asyncio.timeout(180):
        refill_copied = await engine.drain_pending_expression_copy(
            profile=profile,
            limit=60,
        )
    final = db.count_pool_readiness()

    print(
        "live_copy_watermark_summary "
        f"initial_copied={initial_copied} "
        f"initial_copy_ready={first['copy_ready']} "
        f"initial_pending={first['admitted_pending_copy']} "
        f"served={len(served.items)} "
        f"serve_elapsed_seconds={serve_elapsed_seconds:.3f} "
        f"after_consume_copy_ready={after_consume['copy_ready']} "
        f"demand_after_consume={demand_after_consume} "
        f"refill_copied={refill_copied} "
        f"final_copy_ready={final['copy_ready']} "
        f"final_pending={final['admitted_pending_copy']} "
        f"expression_batch_sizes={metrics.expression_batch_sizes} "
        f"provider_names={sorted(set(metrics.provider_names))}",
        flush=True,
    )
    assert initial_copied == 2
    assert first["copy_ready"] == 2
    assert first["admitted_pending_copy"] == 3
    assert len(served.items) == 1
    assert all(item.expression.strip() and item.topic_label.strip() for item in served.items)
    assert after_consume["copy_ready"] == 1
    assert demand_after_consume == 1
    assert refill_copied == 1
    assert final["copy_ready"] == 2
    assert final["admitted_pending_copy"] == 2
    assert metrics.expression_batch_sizes[:2] == [2, 1]
    db.close()


@pytest.mark.asyncio
async def test_real_provider_openclaw_one_shot_refill_uses_single_copy_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real OpenClaw bootstrap through its 45-second adapter path.

    This remains explicitly opt-in with the same provider controls as the
    refill integration above.  It uses a temporary database and a synthetic
    profile, while the ranking request and bounded evaluation/copy requests
    reach the configured real provider.
    """

    import openbiliclaw.integrations.openclaw.bootstrap as bootstrap_module
    import openbiliclaw.llm.registry as registry_module

    print("live_openclaw_one_shot_phase=config", flush=True)
    loaded_config, registry = _load_live_config_and_registry(disable_embedding=True)
    config = replace(
        loaded_config,
        data_dir=str(tmp_path),
        bilibili=replace(loaded_config.bilibili, cookie="", proxy=""),
        scheduler=replace(
            loaded_config.scheduler,
            enabled=True,
            pause_on_extension_disconnect=False,
            pool_target_count=_LIVE_OPENCLAW_POOL_TARGET,
            pool_source_shares={"bilibili": 1},
            discovery_limit=_LIVE_OPENCLAW_DISCOVERY_LIMIT,
        ),
    )
    assert config.llm.default_provider == "openai_compatible"
    assert config.llm.embedding.provider == ""
    assert config.llm.embedding.fallback_provider == ""
    assert config.llm.ollama.model == ""
    assert config.llm.ollama.base_url == ""
    assert "ollama" not in registry.available_providers
    gate = LLMConcurrencyGate(total_concurrency=4)
    metrics = _LiveMetrics(gate)
    monitored_registry = _MonitoredRegistry(registry, metrics)
    embedding_builder_calls = 0

    def disabled_embedding_builder(
        callback_config: Config,
        _registry: LLMRegistry,
    ) -> None:
        nonlocal embedding_builder_calls
        embedding_builder_calls += 1
        assert callback_config.llm.embedding.provider == ""
        assert callback_config.llm.embedding.fallback_provider == ""
        assert callback_config.llm.ollama.model == ""
        assert callback_config.llm.ollama.base_url == ""
        return None

    monkeypatch.setattr(bootstrap_module, "load_config", lambda: config)
    monkeypatch.setattr(
        bootstrap_module,
        "build_llm_registry",
        lambda _config: monitored_registry,
    )
    monkeypatch.setattr(registry_module, "build_embedding_service", disabled_embedding_builder)

    services = build_openclaw_adapter_services()
    try:
        assert embedding_builder_calls == 1
        assert services.recommendation_engine._embedding_service is None  # noqa: SLF001
        assert services.discovery_engine._embedding_service is None  # noqa: SLF001
        assert all(
            getattr(strategy, "embedding_service", None) is None
            for strategy in services.discovery_engine._strategies  # noqa: SLF001
        )
        trace_origin = time.monotonic()
        original_complete_structured_task = services.llm_service.complete_structured_task

        async def monitored_complete_structured_task(*args: Any, **kwargs: Any) -> Any:
            caller = str(kwargs.get("caller", "") or "")
            if caller != "recommendation.write_expression":
                return await original_complete_structured_task(*args, **kwargs)
            request = _LiveExpressionRequest(
                caller=caller,
                batch_size=_content_batch_size(str(kwargs.get("user_input", "") or "")),
                started=time.monotonic(),
            )
            metrics.expression_requests.append(request)
            try:
                response = await original_complete_structured_task(*args, **kwargs)
            except asyncio.CancelledError:
                request.cancelled = True
                request.outcome = "cancelled"
                raise
            except Exception as exc:
                request.outcome = classify_llm_failure_kind(exc)
                raise
            else:
                request.outcome = "returned"
                return response
            finally:
                request.elapsed_seconds = time.monotonic() - request.started

        original_precompute_batch = services.recommendation_engine._precompute_batch  # noqa: SLF001

        async def monitored_precompute_batch(
            batch: list[Any],
            callback_profile: Any,
            *,
            fallback_to_single: bool = True,
        ) -> int:
            attempt = _LiveCopyBatchAttempt(batch_size=len(batch))
            metrics.copy_batch_attempts.append(attempt)
            started = time.monotonic()
            try:
                result = await original_precompute_batch(
                    batch,
                    callback_profile,
                    fallback_to_single=fallback_to_single,
                )
            except asyncio.CancelledError:
                attempt.cancelled = True
                attempt.outcome = "cancelled"
                raise
            except Exception as exc:
                attempt.outcome = type(exc).__name__
                raise
            else:
                attempt.outcome = "completed"
                return result
            finally:
                attempt.elapsed_seconds = time.monotonic() - started

        monkeypatch.setattr(
            services.llm_service,
            "complete_structured_task",
            monitored_complete_structured_task,
        )
        monkeypatch.setattr(
            services.recommendation_engine,
            "_precompute_batch",
            monitored_precompute_batch,
        )
        profile = _profile()
        soul_layer = services.memory_manager.get_layer("soul")
        soul_layer.data.clear()
        soul_layer.data.update(OnionProfile.from_legacy(profile).to_dict())
        soul_layer.save()

        trending = next(
            strategy
            for strategy in services.discovery_engine._strategies  # noqa: SLF001
            if strategy.name == "trending"
        )

        async def only_technology_ranking(_profile: object) -> list[int]:
            return [_LIVE_RANKING_RID]

        monkeypatch.setattr(trending, "_select_rids", only_technology_ranking)
        trending.llm_evaluation = False

        ranking_rids: list[int] = []
        ranking_elapsed_seconds = 0.0
        original_get_ranking = services.bilibili_client.get_ranking

        async def monitored_get_ranking(rid: int = 0) -> list[dict[str, object]]:
            nonlocal ranking_elapsed_seconds
            ranking_rids.append(int(rid))
            started = time.monotonic()
            try:
                return await original_get_ranking(rid)
            finally:
                ranking_elapsed_seconds = time.monotonic() - started

        monkeypatch.setattr(services.bilibili_client, "get_ranking", monitored_get_ranking)
        controller = services.runtime_controller
        detached_controller_task_names: list[str] = []
        original_track_task = controller._track_task  # noqa: SLF001

        def monitored_track_task(name: str, coro: Any) -> Any:
            if name in {
                "prewarm_supergroup_embeddings",
                "prewarm_pool_mmr_embeddings",
            }:
                detached_controller_task_names.append(name)
            return original_track_task(name, coro)

        detached_recommendation_task_names: list[str] = []
        original_spawn_detached_task = services.recommendation_engine._spawn_detached_task  # noqa: SLF001

        def monitored_spawn_detached_task(name: str, coro: Any) -> Any:
            detached_recommendation_task_names.append(name)
            return original_spawn_detached_task(name, coro)

        monkeypatch.setattr(controller, "_track_task", monitored_track_task)
        monkeypatch.setattr(
            services.recommendation_engine,
            "_spawn_detached_task",
            monitored_spawn_detached_task,
        )
        monkeypatch.setattr(
            controller,
            "_build_source_replenishment_plan",
            lambda: [(["trending"], _LIVE_OPENCLAW_DISCOVERY_LIMIT)],
        )

        pipeline = controller.discovery_candidate_pipeline
        assert pipeline is not None
        evaluation_started = False
        evaluation_cancelled = False
        evaluation_elapsed_seconds = 0.0
        evaluation_batch_sizes: list[int] = []
        original_evaluate = services.discovery_engine.evaluate_content_batch

        async def monitored_evaluate(*args: object, **kwargs: object) -> list[float]:
            nonlocal evaluation_started, evaluation_cancelled, evaluation_elapsed_seconds
            evaluation_started = True
            if args and isinstance(args[0], list):
                evaluation_batch_sizes.append(len(args[0]))
            started = time.monotonic()
            try:
                return await original_evaluate(*args, **kwargs)
            except asyncio.CancelledError:
                evaluation_cancelled = True
                raise
            finally:
                evaluation_elapsed_seconds = time.monotonic() - started

        monkeypatch.setattr(services.discovery_engine, "evaluate_content_batch", monitored_evaluate)
        original_admission_callback = pipeline.on_candidates_admitted
        original_copy_callback = controller.one_shot_expression_copy_callback
        assert callable(original_admission_callback)
        assert callable(original_copy_callback)
        admission_callback_calls = 0
        controller_copy_callback_calls = 0
        controller_copy_callback_cancelled = False
        refresh_cancelled = False
        refresh_elapsed_seconds = 0.0

        async def await_callback(callback: object, *args: object) -> int:
            assert callable(callback)
            result = callback(*args)
            if inspect.isawaitable(result):
                result = await result
            return max(0, int(result or 0))

        async def monitored_admission_callback(
            callback_profile: object,
            admitted: int,
        ) -> int:
            nonlocal admission_callback_calls
            admission_callback_calls += 1
            return await await_callback(original_admission_callback, callback_profile, admitted)

        async def monitored_copy_callback(callback_profile: object) -> int:
            nonlocal controller_copy_callback_calls, controller_copy_callback_cancelled
            controller_copy_callback_calls += 1
            try:
                return await await_callback(original_copy_callback, callback_profile)
            except asyncio.CancelledError:
                controller_copy_callback_cancelled = True
                raise

        original_refresh = controller.refresh_if_needed

        async def monitored_refresh() -> dict[str, object]:
            nonlocal refresh_cancelled, refresh_elapsed_seconds
            started = time.monotonic()
            try:
                return await original_refresh()
            except asyncio.CancelledError:
                refresh_cancelled = True
                raise
            finally:
                refresh_elapsed_seconds = time.monotonic() - started

        pipeline.on_candidates_admitted = monitored_admission_callback
        controller.one_shot_expression_copy_callback = monitored_copy_callback
        monkeypatch.setattr(controller, "refresh_if_needed", monitored_refresh)
        adapter = OpenClawAdapter(services=services)
        # Profile/database setup deliberately sits outside this timer.  The
        # measured operation is the public adapter call itself (refresh plus
        # its final serve), which is stricter than the production refresh-only
        # ``asyncio.wait_for`` boundary and catches a slow post-refresh tail.
        started = time.monotonic()
        response = await adapter.recommend(limit=1, refresh_if_needed=True)
        total_elapsed_seconds = time.monotonic() - started
        await asyncio.sleep(0)
        readiness = controller._pool_readiness_counts()  # noqa: SLF001
        canonical_available = int(readiness.get("available", 0) or 0)
        candidate_status_counts = services.database.count_discovery_candidates_by_status()
        raw_candidate_total = sum(int(value or 0) for value in candidate_status_counts.values())
        pending_copy_rows = len(services.database.get_pool_candidates_needing_copy(limit=100))
        expression_request_traces = _expression_request_trace_summary(
            metrics.expression_requests,
            origin=trace_origin,
        )
        copy_batch_attempts = _copy_batch_attempt_summary(metrics.copy_batch_attempts)
        print(
            "live_openclaw_one_shot_summary "
            f"ranking_rid={_LIVE_RANKING_RID} ranking_calls={len(ranking_rids)} "
            f"requested_limit={_LIVE_OPENCLAW_DISCOVERY_LIMIT} "
            f"inline_eval_limit={controller.one_shot_inline_eval_limit} "
            f"ranking_elapsed_seconds={ranking_elapsed_seconds:.2f} "
            f"canonical_available={canonical_available} "
            f"evaluation_started={evaluation_started} "
            f"evaluation_cancelled={evaluation_cancelled} "
            f"evaluation_elapsed_seconds={evaluation_elapsed_seconds:.2f} "
            f"evaluation_batch_sizes={evaluation_batch_sizes} "
            f"admission_callbacks={admission_callback_calls} "
            f"controller_copy_callbacks={controller_copy_callback_calls} "
            f"controller_copy_callback_cancelled={controller_copy_callback_cancelled} "
            f"expression_provider_calls={len(metrics.expression_batch_sizes)} "
            f"provider_round_count={metrics.provider_round_count} "
            f"provider_names={sorted(set(metrics.provider_names))} "
            f"embedding_builder_calls={embedding_builder_calls} "
            "embedding_service_configured=False "
            f"detached_controller_tasks={detached_controller_task_names} "
            f"detached_recommendation_tasks={detached_recommendation_task_names} "
            f"expression_request_traces={expression_request_traces} "
            f"copy_batch_attempts={copy_batch_attempts} "
            f"candidate_cached={int(candidate_status_counts.get('cached', 0) or 0)} "
            f"candidate_pending_eval={int(candidate_status_counts.get('pending_eval', 0) or 0)} "
            f"raw_candidate_total={raw_candidate_total} "
            f"pending_copy_rows={pending_copy_rows} "
            f"refresh_elapsed_seconds={refresh_elapsed_seconds:.2f} "
            f"total_elapsed_seconds={total_elapsed_seconds:.2f} "
            f"adapter_wait_boundary_seconds={adapter.refresh_timeout_seconds:.2f} "
            f"refresh_cancelled={refresh_cancelled}",
            flush=True,
        )

        assert ranking_rids == [_LIVE_RANKING_RID]
        assert controller.one_shot_inline_eval_limit == _LIVE_OPENCLAW_INLINE_EVAL_LIMIT
        assert (
            evaluation_batch_sizes
            and max(evaluation_batch_sizes) <= _LIVE_OPENCLAW_INLINE_EVAL_LIMIT
        )
        assert raw_candidate_total <= _LIVE_OPENCLAW_INLINE_EVAL_LIMIT
        assert total_elapsed_seconds <= adapter.refresh_timeout_seconds, (
            "live OpenClaw adapter operation exceeded its 45-second endpoint SLA; "
            f"total_elapsed_seconds={total_elapsed_seconds:.2f} "
            f"adapter_wait_boundary_seconds={adapter.refresh_timeout_seconds:.2f}"
        )
        assert not refresh_cancelled, (
            "live OpenClaw refresh was cancelled at its adapter wait boundary; "
            f"refresh_elapsed_seconds={refresh_elapsed_seconds:.2f} "
            f"adapter_wait_boundary_seconds={adapter.refresh_timeout_seconds:.2f}"
        )
        assert canonical_available > 0, "live OpenClaw refill left no canonical available rows"
        assert admission_callback_calls == 1
        assert controller_copy_callback_calls == 1
        assert len(metrics.expression_batch_sizes) <= 1
        assert len(metrics.expression_requests) <= 1
        assert metrics.provider_names
        assert set(metrics.provider_names) == {"openai_compatible"}
        assert embedding_builder_calls == 1
        assert not detached_controller_task_names
        assert not detached_recommendation_task_names
        assert all(
            batch_size <= _LIVE_OPENCLAW_INLINE_EVAL_LIMIT
            for batch_size in metrics.expression_batch_sizes
        )
        assert response.items, "live OpenClaw refill returned no usable recommendation"
        assert response.items[0].bvid
        assert response.items[0].reason
    finally:
        await services.bilibili_client.close()
        services.database.close()
