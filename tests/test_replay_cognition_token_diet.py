"""Tests for the privacy-safe Phase 2 cognition replay harness."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from scripts.replay_cognition_token_diet import (
    COMPACT_VIEW,
    LEGACY_VIEW,
    ExecutionBundle,
    FrozenCognitionCohort,
    PinnedRoute,
    PromptBuilders,
    ReplayContractError,
    build_real_artifact,
    build_render_only_artifact,
    evaluate_real_gates,
    execute_real_replay,
    exit_code_for_artifact,
    parse_structured_result,
    quality_gate,
    render_cognition_prompts,
    resolve_pinned_sensetime_route,
    route_audit,
    structural_quality_summary,
    write_artifact,
)

from openbiliclaw.llm.base import LLMResponse
from openbiliclaw.soul.cognition_cycle import _COGNITION_MAX_TOKENS

if TYPE_CHECKING:
    from pathlib import Path


def _cohort() -> FrozenCognitionCohort:
    event = {
        "id": 1,
        "event_type": "view",
        "title": "PRIVATE_EVENT_TITLE_NEVER_PERSIST",
        "url": "https://private.example/video/secret",
        "context": "PRIVATE_EVENT_CONTEXT_NEVER_PERSIST",
        "metadata": json.dumps(
            {
                "source_platform": "bilibili",
                "up_name": "PRIVATE_CREATOR_NEVER_PERSIST",
            }
        ),
    }
    preference = {
        "interests": [{"name": "PRIVATE_PROFILE_INTEREST_NEVER_PERSIST", "weight": 0.9}],
        "favorite_up_users": ["PRIVATE_CREATOR_NEVER_PERSIST"],
    }
    soul = {
        "personality_portrait": "PRIVATE_PROFILE_PORTRAIT_NEVER_PERSIST",
        "recent_awareness": [
            {
                "date": "2026-08-06",
                "observation": "PRIVATE_AWARENESS_CONTEXT_NEVER_PERSIST",
                "trend": "stable",
                "emotion_guess": "neutral",
                "source_event_ids": [1],
            }
        ],
        "active_insights": [
            {
                "hypothesis": "PRIVATE_EXISTING_HYPOTHESIS_NEVER_PERSIST",
                "evidence": ["PRIVATE_EXISTING_EVIDENCE_NEVER_PERSIST"],
                "confidence": 0.7,
                "validated": False,
            }
        ],
    }
    return FrozenCognitionCohort(
        preference_events=(dict(event),),
        awareness_events=(dict(event),),
        existing_preference=preference,
        soul_profile=soul,
        awareness_notes=tuple(dict(item) for item in soul["recent_awareness"]),
        active_insights=tuple(dict(item) for item in soul["active_insights"]),
        snapshot_digest="a" * 64,
        preference_input_digest="b" * 64,
        awareness_input_digest="c" * 64,
        insight_input_digest="f" * 64,
        preference_event_ids_digest="d" * 64,
        awareness_event_ids_digest="e" * 64,
    )


def _prompt_builder(*, input_view: str, **kwargs: Any) -> list[dict[str, str]]:
    view = input_view
    if view == LEGACY_VIEW:
        payload = json.dumps(kwargs, ensure_ascii=False, sort_keys=True)
    else:
        payload = json.dumps(
            {"view": COMPACT_VIEW, "event_count": len(kwargs.get("events", []))},
            sort_keys=True,
        )
    return [
        {"role": "system", "content": "static cognition json contract"},
        {"role": "user", "content": f"<input>\n{payload}\n</input>"},
    ]


def _rendered() -> tuple[FrozenCognitionCohort, dict[Any, Any]]:
    cohort = _cohort()
    rendered = render_cognition_prompts(
        cohort,
        builders=PromptBuilders(
            preference=_prompt_builder,
            awareness_confusions=_prompt_builder,
            insight=_prompt_builder,
        ),
    )
    return cohort, rendered


class _FakePinnedService:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    async def complete_structured_task(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        self.requests.append(dict(kwargs))
        caller = str(kwargs["caller"])
        compact = COMPACT_VIEW in str(kwargs["user_input"])
        if caller.startswith("soul.preference"):
            content = json.dumps(
                {
                    "interests": [
                        {
                            "name": "PRIVATE_RESULT_INTEREST_NEVER_PERSIST",
                            "category": "test",
                            "weight": 0.8,
                        }
                    ],
                    "style": {
                        "preferred_duration": "medium",
                        "preferred_pace": "balanced",
                        "quality_sensitivity": 0.5,
                        "humor_preference": 0.5,
                        "depth_preference": 0.8,
                    },
                    "context": {},
                    "exploration_openness": 0.5,
                    "disliked_topics": [],
                    "favorite_up_users": [],
                }
            )
            usage = {
                "prompt_tokens": 500 if compact else 800,
                "completion_tokens": 200,
                "total_tokens": 700 if compact else 1000,
                "cached_input_tokens": 100 if compact else 0,
            }
        elif caller.startswith("soul.awareness"):
            content = json.dumps(
                {
                    "notes": [
                        {
                            "date": "2026-08-06",
                            "observation": "PRIVATE_RESULT_OBSERVATION_NEVER_PERSIST",
                            "trend": "stable",
                            "emotion_guess": "neutral",
                            "source_event_ids": [1],
                        }
                    ],
                    "confusions": [],
                }
            )
            usage = {
                "prompt_tokens": 600 if compact else 1000,
                "completion_tokens": 100,
                "total_tokens": 700 if compact else 1100,
                "cached_input_tokens": 50 if compact else 0,
            }
        else:
            content = json.dumps(
                [
                    {
                        "hypothesis": "PRIVATE_RESULT_HYPOTHESIS_NEVER_PERSIST",
                        "evidence": ["PRIVATE_RESULT_EVIDENCE_NEVER_PERSIST"],
                        "confidence": 0.65,
                    }
                ]
            )
            usage = {
                "prompt_tokens": 450 if compact else 700,
                "completion_tokens": 80,
                "total_tokens": 530 if compact else 780,
                "cached_input_tokens": 25 if compact else 0,
            }
        return LLMResponse(
            content=content,
            provider="openai_compatible",
            instance_id="sensenova-prod",
            model="SenseChat-5",
            usage=usage,
        )


def _safe_call(
    *,
    task: str,
    logical_run: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> dict[str, object]:
    return {
        "task": task,
        "pair_kind": "control" if logical_run in {"A1", "A2"} else "treatment",
        "logical_run": logical_run,
        "input_view": COMPACT_VIEW if logical_run == "B" else LEGACY_VIEW,
        "status": "ok",
        "route": {
            "provider": "openai_compatible",
            "instance_id": "sensenova-prod",
            "model": "SenseChat-5",
        },
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": 0,
            "uncached_input_tokens": prompt_tokens,
        },
        "parse": {
            "success": True,
            "strict_success": True,
            "schema_valid": True,
            "repair_count": 0,
        },
        "structure": (
            {"disliked_topic_count": 0}
            if task == "preference"
            else {"out_of_cohort_citation_count": 0}
            if task == "awareness_confusions"
            else {"valid_structure_count": 1, "hypothesis_count": 1}
        ),
    }


def _passing_quality() -> dict[str, object]:
    preference_pair = {
        "comparable": True,
        "top_interest_weighted_overlap": 1.0,
        "style_drift": 0.0,
        "right_hallucinated_creator_count": 0,
        "right_creator_evidence_loss_count": 0,
        "disliked_topic_count_delta": 0,
    }
    awareness_pair = {
        "comparable": True,
        "observation_exact_overlap": 1.0,
        "source_event_id_overlap": 1.0,
        "note_count_delta": 0,
        "right_cited_event_count": 1,
        "right_out_of_cohort_citation_count": 0,
    }
    insight_pair = {
        "comparable": True,
        "hypothesis_count_delta": 0,
        "mean_evidence_count_drift": 0.0,
        "mean_confidence_drift": 0.0,
        "right_invalid_structure_count": 0,
    }
    return {
        "input_signals": {
            "contains_explicit_dislike": False,
            "contains_retraction": False,
            "creator_evidence_count": 0,
        },
        "preference": {
            "control_aa": dict(preference_pair),
            "treatment_ab": dict(preference_pair),
        },
        "awareness_confusions": {
            "control_aa": dict(awareness_pair),
            "treatment_ab": dict(awareness_pair),
        },
        "insight": {
            "control_aa": dict(insight_pair),
            "treatment_ab": dict(insight_pair),
        },
    }


def test_render_only_builds_a1_a2_ab_without_provider_calls() -> None:
    cohort, rendered = _rendered()

    artifact = build_render_only_artifact(
        cohort=cohort,
        rendered=rendered,
        git_metadata={"commit": "test", "dirty": False},
    )

    assert artifact["mode"] == "render-only"
    assert artifact["gate"]["passed"] is True  # type: ignore[index]
    assert exit_code_for_artifact(artifact) == 0
    assert [item["logical_run"] for item in artifact["planned_runs"]] == [  # type: ignore[index]
        "A1",
        "A2",
        "A",
        "B",
        "A1",
        "A2",
        "A",
        "B",
        "A1",
        "A2",
        "A",
        "B",
    ]
    assert artifact["render"]["preference"]["system_byte_invariant"] is True  # type: ignore[index]
    assert (
        artifact["render"]["preference"]["compact-v1"]["prompt_chars"]  # type: ignore[index]
        < artifact["render"]["preference"]["legacy"]["prompt_chars"]  # type: ignore[index]
    )


def test_production_insight_builder_is_rendered_with_analyzer_preprocessing() -> None:
    cohort = _cohort()

    rendered = render_cognition_prompts(cohort)

    legacy = rendered[("insight", LEGACY_VIEW)]
    compact = rendered[("insight", COMPACT_VIEW)]
    assert legacy.system_instruction == compact.system_instruction
    assert legacy.input_digest == cohort.insight_input_digest
    assert "PRIVATE_AWARENESS_CONTEXT_NEVER_PERSIST" in compact.user_input
    assert "PRIVATE_EXISTING_HYPOTHESIS_NEVER_PERSIST" in compact.user_input
    # InsightAnalyzer strips provenance and old evidence before invoking the
    # builder; replay must mirror that exact production-visible shape.
    assert "source_event_ids" not in compact.user_input
    assert "PRIVATE_EXISTING_EVIDENCE_NEVER_PERSIST" not in compact.user_input


@pytest.mark.asyncio
async def test_real_artifact_excludes_prompts_profiles_events_urls_and_responses(
    tmp_path: Path,
) -> None:
    cohort, rendered = _rendered()
    service = _FakePinnedService()
    bundle = await execute_real_replay(
        rendered=rendered,
        cohort=cohort,
        client=service,
    )
    route = PinnedRoute(
        instance_id="sensenova-prod",
        provider_type="openai_compatible",
        model="SenseChat-5",
    )
    artifact = build_real_artifact(
        cohort=cohort,
        rendered=rendered,
        bundle=bundle,
        expected_route=route,
        blind_review="pass",
        git_metadata={"commit": "test", "dirty": False},
    )
    output = tmp_path / "artifact.json"
    write_artifact(
        output,
        artifact,
        private_values=[
            cohort.preference_events,
            cohort.awareness_events,
            cohort.existing_preference,
            cohort.soul_profile,
            bundle.response_bodies,
        ],
    )

    serialized = output.read_text(encoding="utf-8")
    assert service.calls == 12
    insight_requests = [
        request for request in service.requests if request["caller"] == "soul.insight.replay"
    ]
    assert len(insight_requests) == 4
    assert {request["max_tokens"] for request in insight_requests} == {_COGNITION_MAX_TOKENS}
    assert artifact["render"]["insight"]["system_byte_invariant"] is True  # type: ignore[index]
    assert (
        artifact["structural_quality"]["insight"]["treatment_ab"][  # type: ignore[index]
            "right_invalid_structure_count"
        ]
        == 0
    )
    assert artifact["gate"]["tokens"]["insight_prompt_token_savings"] > 0  # type: ignore[index,operator]
    assert artifact["gate"]["passed"] is True  # type: ignore[index]
    task_rollout = artifact["gate"]["task_rollout"]  # type: ignore[index]
    assert task_rollout["preference"]["compact_v1_enabled"] is True  # type: ignore[index]
    assert task_rollout["awareness_confusions"]["compact_v1_enabled"] is True  # type: ignore[index]
    assert task_rollout["insight"]["compact_v1_enabled"] is False  # type: ignore[index]
    assert task_rollout["insight"]["selected_view"] == LEGACY_VIEW  # type: ignore[index]
    assert task_rollout["insight"]["token_gate_status"] == "threshold-not-declared"  # type: ignore[index]
    assert "PRIVATE_" not in serialized
    assert "https://private.example" not in serialized
    assert "system_instruction" not in serialized
    assert "user_input" not in serialized
    assert "provider_response" not in serialized
    assert '"prompt_tokens"' in serialized
    assert '"cached_input_tokens"' in serialized
    assert '"instance_id": "sensenova-prod"' in serialized
    assert '"model": "SenseChat-5"' in serialized


def test_route_drift_fails_and_maps_to_nonzero_exit() -> None:
    calls = [
        _safe_call(
            task="preference",
            logical_run="A1",
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
        )
    ]
    calls[0]["route"] = {
        "provider": "openai_compatible",
        "instance_id": "fallback-instance",
        "model": "other-model",
    }
    expected = PinnedRoute("sensenova-prod", "openai_compatible", "SenseChat-5")

    audit = route_audit(calls, expected=expected)

    assert audit["passed"] is False
    assert audit["route_drift_calls"] == ["preference:A1"]
    assert exit_code_for_artifact({"gate": {"passed": audit["passed"]}}) == 1


def test_token_gate_failure_is_blocking_even_when_route_and_quality_pass() -> None:
    calls: list[dict[str, object]] = []
    for task in ("preference", "awareness_confusions", "insight"):
        for logical_run in ("A1", "A2", "A", "B"):
            calls.append(
                _safe_call(
                    task=task,
                    logical_run=logical_run,
                    prompt_tokens=1000,
                    completion_tokens=100,
                    total_tokens=1100,
                )
            )
    expected = PinnedRoute("sensenova-prod", "openai_compatible", "SenseChat-5")

    gate = evaluate_real_gates(
        calls=calls,
        quality=_passing_quality(),
        expected_route=expected,
        blind_review="pass",
    )

    assert gate["route"]["passed"] is True  # type: ignore[index]
    assert gate["quality"]["passed"] is True  # type: ignore[index]
    assert gate["tokens"]["passed"] is False  # type: ignore[index]
    assert gate["passed"] is False
    assert gate["blocking_reasons"] == ["token-gate-failed"]


def test_task_scoped_rollout_can_enable_awareness_confusions_while_full_gate_fails() -> None:
    calls: list[dict[str, object]] = []
    for task in ("preference", "awareness_confusions", "insight"):
        for logical_run in ("A1", "A2", "A", "B"):
            compact = logical_run == "B"
            calls.append(
                _safe_call(
                    task=task,
                    logical_run=logical_run,
                    prompt_tokens=(
                        600
                        if compact and task == "awareness_confusions"
                        else 650
                        if compact and task == "preference"
                        else 700
                        if compact
                        else 1000
                    ),
                    completion_tokens=100,
                    total_tokens=(750 if compact and task == "preference" else 1100),
                )
            )
    quality = _passing_quality()
    preference_treatment = quality["preference"]["treatment_ab"]  # type: ignore[index]
    preference_treatment["top_interest_weighted_overlap"] = 0.0
    insight_treatment = quality["insight"]["treatment_ab"]  # type: ignore[index]
    insight_treatment["right_invalid_structure_count"] = 1
    expected = PinnedRoute("sensenova-prod", "openai_compatible", "SenseChat-5")

    gate = evaluate_real_gates(
        calls=calls,
        quality=quality,
        expected_route=expected,
        blind_review="pass",
    )

    assert gate["passed"] is False, "the full-compact artifact remains failed"
    rollout = gate["task_rollout"]
    assert rollout["awareness_confusions"]["compact_v1_enabled"] is True  # type: ignore[index]
    assert rollout["awareness_confusions"]["selected_view"] == COMPACT_VIEW  # type: ignore[index]
    assert rollout["preference"]["compact_v1_enabled"] is False  # type: ignore[index]
    assert rollout["preference"]["selected_view"] == LEGACY_VIEW  # type: ignore[index]
    assert rollout["preference"]["quality_gate_passed"] is False  # type: ignore[index]
    assert rollout["insight"]["compact_v1_enabled"] is False  # type: ignore[index]
    assert rollout["insight"]["selected_view"] == LEGACY_VIEW  # type: ignore[index]
    assert "token-threshold-not-declared" in rollout["insight"]["blocking_reasons"]  # type: ignore[index]


def test_insight_replay_uses_production_tolerant_parser_but_strict_schema_gate() -> None:
    item = {
        "hypothesis": "用户可能通过长期项目验证工具价值。",
        "evidence": ["连续收藏并搜索可靠性工程内容。"],
        "confidence": 0.68,
    }

    strict = parse_structured_result(
        task="insight",
        content=json.dumps([item], ensure_ascii=False),
        allowed_event_ids=set(),
    )
    recognized_wrappers = {
        key: parse_structured_result(
            task="insight",
            content=json.dumps({key: [item]}, ensure_ascii=False),
            allowed_event_ids=set(),
        )
        for key in (
            "results",
            "items",
            "insights",
            "hypotheses",
            "data",
            "output",
            "list",
            "array",
        )
    }
    invalid_structure = parse_structured_result(
        task="insight",
        content=json.dumps([{**item, "evidence": []}], ensure_ascii=False),
        allowed_event_ids=set(),
    )
    malformed_sibling = parse_structured_result(
        task="insight",
        content=json.dumps([item, "not-an-insight"], ensure_ascii=False),
        allowed_event_ids=set(),
    )
    unknown_wrapper = parse_structured_result(
        task="insight",
        content=json.dumps({"payload": [item]}, ensure_ascii=False),
        allowed_event_ids=set(),
    )
    dirty_recognized_wrapper = parse_structured_result(
        task="insight",
        content=json.dumps(
            {"hypotheses": [item], "unexpected": True},
            ensure_ascii=False,
        ),
        allowed_event_ids=set(),
    )

    assert strict.parse_success is True
    assert strict.schema_valid is True
    assert strict.metrics["structure_valid"] is True
    assert strict.metrics["mean_evidence_count"] == 1.0
    for wrapped in recognized_wrappers.values():
        assert wrapped.parse_success is True
        assert wrapped.schema_valid is True
        assert wrapped.repair_count == 0
        assert wrapped.metrics["hypothesis_count"] == 1
    assert invalid_structure.parse_success is True
    assert invalid_structure.schema_valid is False
    assert malformed_sibling.parse_success is True
    assert malformed_sibling.schema_valid is False
    # Production may salvage these shapes, but they are not exact members of
    # the declared wrapper contract and remain repairs in replay evidence.
    assert unknown_wrapper.parse_success is True
    assert unknown_wrapper.schema_valid is False
    assert unknown_wrapper.repair_count == 1
    assert dirty_recognized_wrapper.parse_success is True
    assert dirty_recognized_wrapper.schema_valid is False
    assert dirty_recognized_wrapper.repair_count == 1


def test_awareness_gate_uses_source_evidence_overlap_not_exact_paraphrase() -> None:
    left = parse_structured_result(
        task="awareness_confusions",
        content=json.dumps(
            {
                "notes": [
                    {
                        "observation": "最近连续钻研可靠性工程。",
                        "source_event_ids": [1],
                    }
                ],
                "confusions": [],
            },
            ensure_ascii=False,
        ),
        allowed_event_ids={1},
    )
    paraphrase = parse_structured_result(
        task="awareness_confusions",
        content=json.dumps(
            {
                "notes": [
                    {
                        "observation": "近期反复探索如何让系统更可靠。",
                        "source_event_ids": [1],
                    }
                ],
                "confusions": [],
            },
            ensure_ascii=False,
        ),
        allowed_event_ids={1},
    )
    bundle = ExecutionBundle(
        calls=(),
        parsed={
            ("awareness_confusions", "A1"): left,
            ("awareness_confusions", "A2"): paraphrase,
            ("awareness_confusions", "A"): left,
            ("awareness_confusions", "B"): paraphrase,
        },
        response_bodies=(),
    )

    summary = structural_quality_summary(bundle, cohort=_cohort())
    control = summary["awareness_confusions"]["control_aa"]  # type: ignore[index]
    treatment = summary["awareness_confusions"]["treatment_ab"]  # type: ignore[index]

    assert control["observation_exact_overlap"] == 0.0
    assert control["source_event_id_overlap"] == 1.0
    assert treatment["source_event_id_overlap"] == 1.0
    assert treatment["right_out_of_cohort_citation_count"] == 0


@pytest.mark.parametrize("bad_run", ["A1", "A2", "A", "B"])
def test_awareness_out_of_cohort_citation_remains_a_hard_failure(bad_run: str) -> None:
    calls: list[dict[str, object]] = []
    for task in ("preference", "awareness_confusions", "insight"):
        for logical_run in ("A1", "A2", "A", "B"):
            call = _safe_call(
                task=task,
                logical_run=logical_run,
                prompt_tokens=1000,
                completion_tokens=100,
                total_tokens=1100,
            )
            if task == "awareness_confusions" and logical_run == bad_run:
                call["structure"] = {"out_of_cohort_citation_count": 1}
            calls.append(call)
    quality = _passing_quality()

    gate = quality_gate(calls, quality, blind_review="pass")

    assert gate["passed"] is False
    assert gate["automatic_checks"]["awareness_evidence_attribution_valid"] is False  # type: ignore[index]


def test_insight_structure_and_confidence_drift_must_stay_in_control_envelope() -> None:
    calls = [
        _safe_call(
            task=task,
            logical_run=logical_run,
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
        )
        for task in ("preference", "awareness_confusions", "insight")
        for logical_run in ("A1", "A2", "A", "B")
    ]
    quality = _passing_quality()
    treatment = quality["insight"]["treatment_ab"]  # type: ignore[index]
    treatment["hypothesis_count_delta"] = 2
    treatment["mean_evidence_count_drift"] = 1.0
    treatment["mean_confidence_drift"] = 0.4

    gate = quality_gate(calls, quality, blind_review="pass")

    assert gate["passed"] is False
    checks = gate["automatic_checks"]
    assert checks["insight_hypothesis_count_within_envelope"] is False  # type: ignore[index]
    assert checks["insight_evidence_structure_within_envelope"] is False  # type: ignore[index]
    assert checks["insight_confidence_drift_within_envelope"] is False  # type: ignore[index]


def test_insight_gate_calibrates_treatment_against_control_aa_variability() -> None:
    calls = [
        _safe_call(
            task=task,
            logical_run=logical_run,
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
        )
        for task in ("preference", "awareness_confusions", "insight")
        for logical_run in ("A1", "A2", "A", "B")
    ]
    quality = _passing_quality()
    control = quality["insight"]["control_aa"]  # type: ignore[index]
    control["hypothesis_count_delta"] = 1
    control["mean_evidence_count_drift"] = 0.4
    control["mean_confidence_drift"] = 0.2
    treatment = quality["insight"]["treatment_ab"]  # type: ignore[index]
    treatment["hypothesis_count_delta"] = 2
    treatment["mean_evidence_count_drift"] = 0.8
    treatment["mean_confidence_drift"] = 0.29

    gate = quality_gate(calls, quality, blind_review="pass")

    assert gate["passed"] is True
    assert gate["automatic_checks"]["insight_hypothesis_count_within_envelope"] is True  # type: ignore[index]
    assert gate["automatic_checks"]["insight_evidence_structure_within_envelope"] is True  # type: ignore[index]
    assert gate["automatic_checks"]["insight_confidence_drift_within_envelope"] is True  # type: ignore[index]


def test_resolve_pinned_sensetime_route_requires_openai_compatible_and_exact_model() -> None:
    good = SimpleNamespace(
        enabled=True,
        provider_type="openai_compatible",
        model="SenseChat-5",
        base_url="https://api.sensenova.example/v1",
        name="日日新",
    )
    config = SimpleNamespace(llm=SimpleNamespace(instances={"SenseNova-Prod": good}))

    route = resolve_pinned_sensetime_route(config, instance_id="sensenova-prod")

    assert route == PinnedRoute("sensenova-prod", "openai_compatible", "SenseChat-5")

    bad = SimpleNamespace(
        enabled=True,
        provider_type="deepseek",
        model="SenseChat-5",
        base_url="https://api.sensenova.example/v1",
        name="日日新",
    )
    bad_config = SimpleNamespace(llm=SimpleNamespace(instances={"sensenova-prod": bad}))
    with pytest.raises(ReplayContractError, match="openai_compatible"):
        resolve_pinned_sensetime_route(bad_config, instance_id="sensenova-prod")


def test_missing_input_view_seam_fails_with_clear_adapter_message() -> None:
    cohort = _cohort()

    def old_builder(*, events: list[dict[str, object]], **kwargs: object) -> list[dict[str, str]]:
        del events, kwargs
        return [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ]

    with pytest.raises(ReplayContractError, match=r"input_view='legacy'\|'compact-v1'"):
        render_cognition_prompts(
            cohort,
            builders=PromptBuilders(
                preference=old_builder,
                awareness_confusions=old_builder,
                insight=old_builder,
            ),
        )


def test_execution_bundle_type_does_not_serialize_private_values_by_itself() -> None:
    bundle = ExecutionBundle(
        calls=(),
        parsed={},
        response_bodies=("PRIVATE_RAW_PROVIDER_RESPONSE_NEVER_PERSIST",),
    )

    assert not hasattr(bundle, "to_dict")
