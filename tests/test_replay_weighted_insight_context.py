"""Tests for the privacy-safe weighted-insight A/A+B replay."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from scripts.replay_cognition_token_diet import PinnedRoute, ReplayContractError, write_artifact
from scripts.replay_token_diet_phase3 import Phase3Cohort, RecordingClient
from scripts.replay_weighted_insight_context import (
    build_render_artifact,
    build_weighted_insight_plan,
    execute_real_gate,
    load_full_baseline_artifact,
)

from openbiliclaw.llm.base import LLMResponse
from openbiliclaw.soul.profile import AwarenessNote, InsightHypothesis

if TYPE_CHECKING:
    from pathlib import Path


def _cohort(*, private_marker: str = "") -> Phase3Cohort:
    insights = [
        InsightHypothesis(
            hypothesis=f"unrelated-domain-{index}",
            evidence=[f"evidence-{index}", private_marker] if private_marker else [],
            confidence=0.1,
        )
        for index in range(100)
    ]
    insights[2] = InsightHypothesis(
        hypothesis="long-standing-supported-pattern",
        evidence=["first signal", "second signal", "third signal", private_marker],
        confidence=0.99,
    )
    insights[3] = InsightHypothesis(
        hypothesis="持续比较 AI 编程助手的效率差异",
        evidence=["留意不同助手完成同一代码任务的速度", private_marker],
        confidence=0.6,
    )
    return Phase3Cohort(
        preference_events=({"id": 1, "title": private_marker},),
        existing_preference={"interests": ["software systems", private_marker]},
        soul_profile={"personality_portrait": private_marker},
        preference_awareness_tail=(),
        preference_insight_tail=(),
        insight_notes=(
            AwarenessNote(
                observation="今天连续观看 AI 编程助手效率横向测评",
                trend=private_marker,
            ),
        ),
        all_insights=tuple(insights),
        snapshot_digest="a" * 64,
        preference_input_digest="b" * 64,
        insight_input_digest="c" * 64,
        recent_expired_unused_regular={},
    )


def test_weighted_plan_recovers_relevant_and_important_old_context() -> None:
    cohort = _cohort()

    plan = build_weighted_insight_plan(cohort)

    assert len(plan.fixed_insights) == 20
    assert len(plan.weighted_insights) == 40
    assert cohort.all_insights[2] in plan.weighted_insights
    assert cohort.all_insights[3] in plan.weighted_insights
    assert all(item in plan.weighted_insights for item in cohort.all_insights[-8:])
    assert int(plan.summary["full_chars"]) > int(plan.summary["weighted_chars"])
    coverage = plan.summary["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["outside_fixed_count"] >= 2
    assert coverage["same_state_near_duplicate_pair_count"] == 0


def test_weighted_render_artifact_excludes_private_context(tmp_path: Path) -> None:
    marker = "PRIVATE_WEIGHTED_INSIGHT_CONTEXT_MUST_NOT_PERSIST"
    cohort = _cohort(private_marker=marker)
    plan = build_weighted_insight_plan(cohort)
    artifact = build_render_artifact(cohort, plan)
    output = tmp_path / "weighted.json"

    write_artifact(
        output,
        artifact,
        private_values=[
            cohort.preference_events,
            cohort.existing_preference,
            cohort.soul_profile,
            cohort.insight_notes,
            cohort.all_insights,
        ],
    )

    assert artifact["gate"]["passed"] is True  # type: ignore[index]
    assert marker not in output.read_text(encoding="utf-8")


class _DeterministicInsightProvider:
    def __init__(self) -> None:
        self.call_count = 0

    async def complete_structured_task(self, **_: object) -> LLMResponse:
        prompt_tokens = [1_000, 1_000, 1_000, 1_050, 2_000][self.call_count]
        self.call_count += 1
        completion_tokens = 100
        return LLMResponse(
            content=json.dumps(
                [
                    {
                        "hypothesis": "new-valid-hypothesis",
                        "evidence": ["new-valid-evidence"],
                        "confidence": 0.8,
                    }
                ]
            ),
            provider="openai_compatible",
            instance_id="sense-test",
            model="deepseek-v4-flash",
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cached_input_tokens": 0,
            },
        )


@pytest.mark.asyncio
async def test_real_gate_uses_fixed_controls_weighted_treatment_and_full_baseline() -> None:
    cohort = _cohort()
    plan = build_weighted_insight_plan(cohort)
    provider = _DeterministicInsightProvider()
    recorder = RecordingClient(provider, max_concurrency=1)
    route = PinnedRoute(
        instance_id="sense-test",
        provider_type="openai_compatible",
        model="deepseek-v4-flash",
    )

    result = await execute_real_gate(
        cohort=cohort,
        plan=plan,
        recorder=recorder,
        expected_route=route,
    )

    assert result["gate"]["passed"] is True  # type: ignore[index]
    assert provider.call_count == 5
    assert [call["logical_run"] for call in recorder.calls] == ["A1", "A2", "A", "B", "F"]
    usage = result["usage"]
    assert usage["weighted_prompt_token_savings_vs_full"] == 0.475  # type: ignore[index]
    assert usage["weighted_prompt_token_overhead_vs_fixed"] == 0.05  # type: ignore[index]


def test_full_baseline_artifact_requires_same_input_and_route(tmp_path: Path) -> None:
    cohort = _cohort()
    plan = build_weighted_insight_plan(cohort)
    route = PinnedRoute(
        instance_id="sense-test",
        provider_type="openai_compatible",
        model="deepseek-v4-flash",
    )
    artifact = {
        "cohort": {"durable_hypothesis_count": 100},
        "render": {"input_digest": "c" * 64},
        "expected_route": {
            "provider": "openai_compatible",
            "instance_id": "sense-test",
            "model": "deepseek-v4-flash",
        },
        "gate": {"full_baseline_valid": True, "route": {"passed": True}},
        "usage": {
            "arms": {
                "F": {
                    "call_count": 1,
                    "prompt_tokens": 2_000,
                    "completion_tokens": 100,
                    "total_tokens": 2_100,
                    "cached_input_tokens": 0,
                }
            }
        },
    }
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    usage, digest = load_full_baseline_artifact(
        path,
        cohort=cohort,
        plan=plan,
        expected_route=route,
    )

    assert usage["prompt_tokens"] == 2_000
    assert len(digest) == 64

    artifact["render"]["input_digest"] = "wrong"  # type: ignore[index]
    path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ReplayContractError, match="input digest"):
        load_full_baseline_artifact(
            path,
            cohort=cohort,
            plan=plan,
            expected_route=route,
        )


@pytest.mark.asyncio
async def test_real_gate_can_reuse_validated_full_baseline() -> None:
    cohort = _cohort()
    plan = build_weighted_insight_plan(cohort)
    provider = _DeterministicInsightProvider()
    recorder = RecordingClient(provider, max_concurrency=1)
    route = PinnedRoute(
        instance_id="sense-test",
        provider_type="openai_compatible",
        model="deepseek-v4-flash",
    )

    result = await execute_real_gate(
        cohort=cohort,
        plan=plan,
        recorder=recorder,
        expected_route=route,
        full_baseline_usage={
            "call_count": 1,
            "prompt_tokens": 2_000,
            "completion_tokens": 100,
            "total_tokens": 2_100,
            "cached_input_tokens": 0,
        },
        full_baseline_source_sha256="d" * 64,
    )

    assert result["gate"]["passed"] is True  # type: ignore[index]
    assert provider.call_count == 4
    assert result["full_baseline"]["mode"] == "validated-artifact"  # type: ignore[index]
