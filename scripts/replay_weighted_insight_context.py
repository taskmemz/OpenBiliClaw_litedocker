"""Privacy-safe weighted-insight render and pinned SenseTime A/A+B gate.

The control arm is the shipped Phase 3 fixed recent/judged prompt view. The
treatment arm is the weighted relevance/importance/diversity view. One extra
full-history request (``F``) supplies an exact provider-reported token baseline;
it is excluded from A/A quality-noise calibration.

Artifacts contain only hashes, counts, structural metrics, sanitized routes,
and usage. Prompts, model bodies, profile text, hypotheses, URLs, credentials,
and cookies are never persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scripts.replay_cognition_token_diet import (  # noqa: E402
    ParsedResult,
    PinnedRoute,
    ReplayContractError,
    _insight_pair_quality,
    resolve_pinned_sensetime_route,
    route_audit,
    write_artifact,
)
from scripts.replay_token_diet_phase3 import (  # noqa: E402
    Phase3Cohort,
    RecordingClient,
    _database_path,
    _merge_history_check,
    _prompt_chars,
    _run_insight_arm,
    _strict_json_envelope,
    _usage_totals,
    build_pinned_phase3_service,
    freeze_phase3_cohort,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from openbiliclaw.config import Config
    from openbiliclaw.soul.profile import InsightHypothesis

logger = logging.getLogger("eval.weighted_insight_context")

SCHEMA_VERSION = 1
CONTRACT_VERSION = "weighted-insight-context-v1"
OFFLINE_CHARACTER_SAVINGS_MIN = 0.35
FULL_PROMPT_TOKEN_SAVINGS_MIN = 0.40
FIXED_TO_WEIGHTED_PROMPT_OVERHEAD_MAX = 0.10


@dataclass(frozen=True)
class WeightedInsightPlan:
    """Frozen prompt arms and privacy-safe render measurements."""

    full_messages: tuple[dict[str, str], ...]
    fixed_messages: tuple[dict[str, str], ...]
    weighted_messages: tuple[dict[str, str], ...]
    fixed_insights: tuple[InsightHypothesis, ...]
    weighted_insights: tuple[InsightHypothesis, ...]
    summary: dict[str, object]


def _savings(base: int, treatment: int) -> float:
    return (base - treatment) / base if base > 0 else 0.0


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _duplicate_hypothesis_count(result: ParsedResult) -> int:
    items = result.value if isinstance(result.value, list) else []
    normalized = [
        "".join(str(item.get("hypothesis") or "").split()).casefold()
        for item in items
        if isinstance(item, dict) and str(item.get("hypothesis") or "").strip()
    ]
    return len(normalized) - len(set(normalized))


def _selection_coverage(
    cohort: Phase3Cohort,
    *,
    fixed: Sequence[InsightHypothesis],
    weighted: Sequence[InsightHypothesis],
) -> dict[str, object]:
    """Return count-only coverage diagnostics using the production features."""
    from openbiliclaw.soul.cognition_cycle import (
        _INSIGHT_CONTEXT_JUDGED_RESERVE,
        _INSIGHT_CONTEXT_RECENT_RESERVE,
        _INSIGHT_CONTEXT_RELEVANCE_QUOTA,
        _INSIGHT_NEAR_DUPLICATE_THRESHOLD,
        _insight_context_strings,
        _insight_overlap,
        _insight_semantic_state,
        _insight_similarity,
        _insight_text_features,
    )

    identity_to_index = {id(item): index for index, item in enumerate(cohort.all_insights)}
    fixed_indices = {identity_to_index[id(item)] for item in fixed}
    weighted_indices = {identity_to_index[id(item)] for item in weighted}

    awareness_text = " ".join(
        text
        for note in cohort.insight_notes
        for text in (note.observation, note.trend, note.emotion_guess)
        if str(text or "").strip()
    )
    profile_text = " ".join(
        [
            *_insight_context_strings(cohort.existing_preference),
            *_insight_context_strings(cohort.soul_profile),
        ]
    )
    awareness_features = _insight_text_features(awareness_text)
    profile_features = _insight_text_features(profile_text)

    relevance: list[float] = []
    quality: list[float] = []
    hypothesis_features: list[frozenset[str]] = []
    states: list[str] = []
    for item in cohort.all_insights:
        evidence = item.evidence if isinstance(item.evidence, list) else []
        hypothesis_features.append(_insight_text_features(item.hypothesis))
        candidate_features = _insight_text_features(
            " ".join(
                [
                    str(item.hypothesis or ""),
                    *(str(value) for value in evidence if str(value).strip()),
                ]
            )
        )
        awareness_match = _insight_overlap(candidate_features, awareness_features)
        profile_match = _insight_overlap(candidate_features, profile_features)
        relevance.append(
            0.8 * awareness_match + 0.2 * profile_match if awareness_features else profile_match
        )
        try:
            raw_confidence = float(item.confidence)
        except (TypeError, ValueError):
            raw_confidence = 0.0
        confidence = max(0.0, min(1.0, raw_confidence)) if math.isfinite(raw_confidence) else 0.0
        quality.append(0.6 * confidence + 0.4 * min(len(evidence), 3) / 3.0)
        states.append(_insight_semantic_state(item))

    relevant_ranked = sorted(
        (index for index, score in enumerate(relevance) if score > 0.0),
        key=lambda index: (relevance[index], index),
        reverse=True,
    )[:_INSIGHT_CONTEXT_RELEVANCE_QUOTA]
    quality_ranked = sorted(
        range(len(cohort.all_insights)),
        key=lambda index: (quality[index], index),
        reverse=True,
    )[:8]
    judged_indices = [
        index
        for index, item in enumerate(cohort.all_insights)
        if bool(item.validated) or bool(str(item.user_verdict or "").strip())
    ][-_INSIGHT_CONTEXT_JUDGED_RESERVE:]
    recent_start = max(0, len(cohort.all_insights) - _INSIGHT_CONTEXT_RECENT_RESERVE)
    recent_indices = set(range(recent_start, len(cohort.all_insights)))
    duplicate_pairs = sum(
        1
        for offset, left in enumerate(sorted(weighted_indices))
        for right in sorted(weighted_indices)[offset + 1 :]
        if states[left] == states[right]
        and _insight_similarity(hypothesis_features[left], hypothesis_features[right])
        >= _INSIGHT_NEAR_DUPLICATE_THRESHOLD
    )
    return {
        "fixed_overlap_count": len(fixed_indices & weighted_indices),
        "outside_fixed_count": len(weighted_indices - fixed_indices),
        "recent_reserve_selected": len(recent_indices & weighted_indices),
        "recent_reserve_available": len(recent_indices),
        "judged_reserve_selected": len(set(judged_indices) & weighted_indices),
        "judged_reserve_available": len(judged_indices),
        "top_current_relevance_selected": len(set(relevant_ranked) & weighted_indices),
        "top_current_relevance_available": len(relevant_ranked),
        "top_quality_selected": len(set(quality_ranked) & weighted_indices),
        "top_quality_available": len(quality_ranked),
        "same_state_near_duplicate_pair_count": duplicate_pairs,
    }


def build_weighted_insight_plan(cohort: Phase3Cohort) -> WeightedInsightPlan:
    """Render full, fixed, and weighted arms with the production prompt builder."""
    from openbiliclaw.llm.prompts import build_insight_prompt
    from openbiliclaw.soul.cognition_cycle import (
        _select_fixed_insight_prompt_context,
        _select_insight_prompt_context,
    )
    from openbiliclaw.soul.insight_analyzer import InsightAnalyzer

    fixed = tuple(_select_fixed_insight_prompt_context(list(cohort.all_insights)))
    weighted = tuple(
        _select_insight_prompt_context(
            list(cohort.all_insights),
            awareness_notes=list(cohort.insight_notes),
            preference=cohort.existing_preference,
            soul_profile=cohort.soul_profile,
        )
    )

    def _messages(insights: Sequence[InsightHypothesis]) -> tuple[dict[str, str], ...]:
        return tuple(
            build_insight_prompt(
                awareness_notes=[
                    InsightAnalyzer._note_to_dict(item) for item in cohort.insight_notes
                ],
                preference_summary=cohort.existing_preference,
                soul_profile=cohort.soul_profile,
                existing_hypotheses=[
                    InsightAnalyzer._hypothesis_to_context_dict(item) for item in insights
                ],
                input_view="legacy",
            )
        )

    full_messages = _messages(cohort.all_insights)
    fixed_messages = _messages(fixed)
    weighted_messages = _messages(weighted)
    full_chars = _prompt_chars(full_messages)
    fixed_chars = _prompt_chars(fixed_messages)
    weighted_chars = _prompt_chars(weighted_messages)
    summary: dict[str, object] = {
        "input_digest": cohort.insight_input_digest,
        "durable_hypothesis_count": len(cohort.all_insights),
        "judged_or_validated_durable_count": sum(
            bool(item.validated or item.user_verdict) for item in cohort.all_insights
        ),
        "fixed_hypothesis_count": len(fixed),
        "weighted_hypothesis_count": len(weighted),
        "full_chars": full_chars,
        "fixed_chars": fixed_chars,
        "weighted_chars": weighted_chars,
        "fixed_character_savings_vs_full": round(_savings(full_chars, fixed_chars), 6),
        "weighted_character_savings_vs_full": round(_savings(full_chars, weighted_chars), 6),
        "weighted_character_overhead_vs_fixed": round(-_savings(fixed_chars, weighted_chars), 6),
        "system_instruction_invariant": (
            full_messages[0]["content"]
            == fixed_messages[0]["content"]
            == weighted_messages[0]["content"]
        ),
        "coverage": _selection_coverage(cohort, fixed=fixed, weighted=weighted),
    }
    return WeightedInsightPlan(
        full_messages=full_messages,
        fixed_messages=fixed_messages,
        weighted_messages=weighted_messages,
        fixed_insights=fixed,
        weighted_insights=weighted,
        summary=summary,
    )


def build_render_artifact(
    cohort: Phase3Cohort,
    plan: WeightedInsightPlan,
) -> dict[str, object]:
    coverage = cast("Mapping[str, object]", plan.summary["coverage"])
    passed = bool(
        _as_int(plan.summary["weighted_hypothesis_count"]) <= 40
        and _as_float(plan.summary["weighted_character_savings_vs_full"])
        >= OFFLINE_CHARACTER_SAVINGS_MIN
        and plan.summary["system_instruction_invariant"] is True
        and _as_int(coverage["same_state_near_duplicate_pair_count"]) == 0
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "render-only",
        "cohort": {
            "snapshot_digest": cohort.snapshot_digest,
            "insight_note_count": len(cohort.insight_notes),
            "durable_hypothesis_count": len(cohort.all_insights),
        },
        "render": plan.summary,
        "gate": {
            "passed": passed,
            "offline_character_savings_min": OFFLINE_CHARACTER_SAVINGS_MIN,
        },
    }


def _quality_summary(results: Mapping[str, ParsedResult]) -> dict[str, object]:
    control_aa = _insight_pair_quality(results.get("A1"), results.get("A2"))
    treatment_ab = _insight_pair_quality(results.get("A"), results.get("B"))
    count_ceiling = max(1, _as_int(control_aa.get("hypothesis_count_delta", 0)) + 1)
    evidence_ceiling = max(
        0.50,
        _as_float(control_aa.get("mean_evidence_count_drift", 0.0)) + 0.20,
    )
    confidence_ceiling = max(
        0.10,
        _as_float(control_aa.get("mean_confidence_drift", 0.0)) + 0.05,
    )
    duplicate_ceiling = max(_duplicate_hypothesis_count(results[run]) for run in ("A1", "A2", "A"))
    treatment_duplicates = _duplicate_hypothesis_count(results["B"])
    repair_ceiling = max(results[run].repair_count for run in ("A1", "A2", "A"))
    arm_metrics = {
        run: {
            "parse_success": result.parse_success,
            "strict_parse_success": result.strict_parse_success,
            "schema_valid": result.schema_valid,
            "repair_count": result.repair_count,
            "metrics": dict(result.metrics),
        }
        for run, result in results.items()
    }
    control_evidence = _as_float(results["A"].metrics.get("mean_evidence_count"))
    treatment_evidence = _as_float(results["B"].metrics.get("mean_evidence_count"))
    passed = bool(
        control_aa.get("comparable")
        and treatment_ab.get("comparable")
        and results["B"].schema_valid
        and results["B"].repair_count <= repair_ceiling
        and _as_int(treatment_ab.get("right_invalid_structure_count", 1)) == 0
        and _as_int(treatment_ab.get("hypothesis_count_delta", 999)) <= count_ceiling
        and _as_float(treatment_ab.get("mean_evidence_count_drift", 999.0)) <= evidence_ceiling
        and _as_float(treatment_ab.get("mean_confidence_drift", 999.0)) <= confidence_ceiling
        and treatment_duplicates <= duplicate_ceiling
    )
    return {
        "passed": passed,
        "arms": arm_metrics,
        "control_aa": control_aa,
        "treatment_ab": treatment_ab,
        "treatment_mean_evidence_count_delta": round(
            treatment_evidence - control_evidence,
            6,
        ),
        "envelope": {
            "hypothesis_count_delta_ceiling": count_ceiling,
            "mean_evidence_count_drift_ceiling": round(evidence_ceiling, 6),
            "mean_confidence_drift_ceiling": round(confidence_ceiling, 6),
            "duplicate_hypothesis_count_ceiling": duplicate_ceiling,
            "repair_count_ceiling": repair_ceiling,
        },
        "treatment_duplicate_hypothesis_count": treatment_duplicates,
        "treatment_repair_count": results["B"].repair_count,
    }


async def execute_real_gate(
    *,
    cohort: Phase3Cohort,
    plan: WeightedInsightPlan,
    recorder: RecordingClient,
    expected_route: PinnedRoute,
    full_baseline_usage: Mapping[str, int] | None = None,
    full_baseline_source_sha256: str = "",
) -> dict[str, object]:
    """Execute fixed A/A/A, weighted B, and one full-history token baseline."""
    results: dict[str, ParsedResult] = {}
    for logical_run in ("A1", "A2", "A"):
        results[logical_run] = await _run_insight_arm(
            recorder=recorder,
            messages=plan.fixed_messages,
            logical_run=logical_run,
        )
    results["B"] = await _run_insight_arm(
        recorder=recorder,
        messages=plan.weighted_messages,
        logical_run="B",
    )
    if full_baseline_usage is None:
        results["F"] = await _run_insight_arm(
            recorder=recorder,
            messages=plan.full_messages,
            logical_run="F",
        )

    quality = _quality_summary(results)
    merge_history = _merge_history_check(cohort, results["B"])
    arms: dict[str, dict[str, int]] = {
        run: _usage_totals(recorder.calls, task="insight", logical_run=run)
        for run in ("A1", "A2", "A", "B")
    }
    if full_baseline_usage is None:
        arms["F"] = _usage_totals(recorder.calls, task="insight", logical_run="F")
    else:
        arms["F"] = {
            key: int(full_baseline_usage.get(key, 0))
            for key in (
                "call_count",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_input_tokens",
            )
        }
    prompt_savings_vs_full = _savings(
        int(arms["F"]["prompt_tokens"]),
        int(arms["B"]["prompt_tokens"]),
    )
    total_savings_vs_full = _savings(
        int(arms["F"]["total_tokens"]),
        int(arms["B"]["total_tokens"]),
    )
    prompt_overhead_vs_fixed = -_savings(
        int(arms["A"]["prompt_tokens"]),
        int(arms["B"]["prompt_tokens"]),
    )
    usage_complete = all(
        int(arms[run]["call_count"]) == 1
        and int(arms[run]["prompt_tokens"]) > 0
        and int(arms[run]["total_tokens"]) >= int(arms[run]["prompt_tokens"])
        for run in arms
    )
    expected_call_count = 5 if full_baseline_usage is None else 4
    route = route_audit(
        recorder.calls,
        expected=expected_route,
        expected_call_count=expected_call_count,
    )
    provider_format = _strict_json_envelope(recorder.calls, task="insight")
    if full_baseline_usage is None:
        full_call: Mapping[str, object] = next(
            (call for call in recorder.calls if call.get("logical_run") == "F"),
            {},
        )
        full_baseline_valid = bool(
            results["F"].schema_valid
            and results["F"].repair_count == 0
            and full_call.get("strict_json") is True
        )
    else:
        full_baseline_valid = bool(full_baseline_source_sha256)
    gate = {
        "passed": bool(
            route["passed"]
            and quality["passed"]
            and merge_history["passed"]
            and provider_format["passed"]
            and full_baseline_valid
            and usage_complete
            and prompt_savings_vs_full >= FULL_PROMPT_TOKEN_SAVINGS_MIN
            and prompt_overhead_vs_fixed <= FIXED_TO_WEIGHTED_PROMPT_OVERHEAD_MAX
        ),
        "route": route,
        "quality": quality,
        "merge_history": merge_history,
        "provider_format": provider_format,
        "full_baseline_valid": full_baseline_valid,
        "usage_complete": usage_complete,
        "token_checks": {
            "prompt_savings_vs_full_passed": (
                prompt_savings_vs_full >= FULL_PROMPT_TOKEN_SAVINGS_MIN
            ),
            "prompt_overhead_vs_fixed_passed": (
                prompt_overhead_vs_fixed <= FIXED_TO_WEIGHTED_PROMPT_OVERHEAD_MAX
            ),
            "full_prompt_savings_min": FULL_PROMPT_TOKEN_SAVINGS_MIN,
            "fixed_to_weighted_prompt_overhead_max": (FIXED_TO_WEIGHTED_PROMPT_OVERHEAD_MAX),
        },
    }
    return {
        "usage": {
            "arms": arms,
            "weighted_prompt_token_savings_vs_full": round(prompt_savings_vs_full, 6),
            "weighted_total_token_savings_vs_full": round(total_savings_vs_full, 6),
            "weighted_prompt_token_overhead_vs_fixed": round(prompt_overhead_vs_fixed, 6),
        },
        "full_baseline": {
            "mode": "current-run" if full_baseline_usage is None else "validated-artifact",
            "source_sha256": full_baseline_source_sha256,
        },
        "quality": quality,
        "gate": gate,
    }


def load_full_baseline_artifact(
    path: Path,
    *,
    cohort: Phase3Cohort,
    plan: WeightedInsightPlan,
    expected_route: PinnedRoute,
) -> tuple[dict[str, int], str]:
    """Load a validated same-input full-history usage arm from a safe artifact."""
    serialized = path.read_bytes()
    try:
        artifact = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ReplayContractError(f"invalid full baseline artifact: {path}") from exc
    if not isinstance(artifact, dict):
        raise ReplayContractError("full baseline artifact must be a JSON object")
    render = artifact.get("render")
    route = artifact.get("expected_route")
    gate = artifact.get("gate")
    usage = artifact.get("usage")
    if not all(isinstance(value, dict) for value in (render, route, gate, usage)):
        raise ReplayContractError("full baseline artifact is missing validated sections")
    render = cast("dict[str, object]", render)
    route = cast("dict[str, object]", route)
    gate = cast("dict[str, object]", gate)
    usage = cast("dict[str, object]", usage)
    if render.get("input_digest") != plan.summary.get("input_digest"):
        raise ReplayContractError("full baseline input digest does not match the frozen cohort")
    cohort_section = artifact.get("cohort")
    if not isinstance(cohort_section, dict) or _as_int(
        cohort_section.get("durable_hypothesis_count"), -1
    ) != len(cohort.all_insights):
        raise ReplayContractError("full baseline durable history count does not match")
    expected = {
        "provider": expected_route.provider_type,
        "instance_id": expected_route.instance_id,
        "model": expected_route.model,
    }
    if any(route.get(key) != value for key, value in expected.items()):
        raise ReplayContractError("full baseline route does not match the pinned route")
    route_gate = gate.get("route")
    if (
        gate.get("full_baseline_valid") is not True
        or not isinstance(route_gate, dict)
        or route_gate.get("passed") is not True
    ):
        raise ReplayContractError("full baseline artifact did not pass route/schema validation")
    raw_arms = usage.get("arms")
    raw_full = raw_arms.get("F") if isinstance(raw_arms, dict) else None
    if not isinstance(raw_full, dict):
        raise ReplayContractError("full baseline artifact has no F usage arm")
    full = {
        key: _as_int(raw_full.get(key))
        for key in (
            "call_count",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_input_tokens",
        )
    }
    if (
        full["call_count"] != 1
        or full["prompt_tokens"] <= 0
        or full["total_tokens"] < full["prompt_tokens"]
    ):
        raise ReplayContractError("full baseline artifact has incomplete provider usage")
    return full, hashlib.sha256(serialized).hexdigest()


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _nonnegative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if value < 0.0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weighted insight context privacy-safe replay")
    parser.add_argument("--mode", choices=("render-only", "real-provider"), default="render-only")
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--insight-notes", type=_positive_int, default=20)
    parser.add_argument("--instance", default="")
    parser.add_argument("--expected-model", default="")
    parser.add_argument("--confirm-sensetime-route", action="store_true")
    parser.add_argument(
        "--full-baseline-artifact",
        default=None,
        help="reuse a validated same-input full-history F usage arm",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=_nonnegative_float,
        default=3.0,
        help="minimum delay between real requests; execution is always single-flight",
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from openbiliclaw.config import load_config

    config: Config = load_config(args.config) if args.config else load_config()
    db_path = Path(args.db) if args.db else _database_path(config)
    data_root = Path(args.data_root) if args.data_root else db_path.parent
    if not db_path.exists():
        raise ReplayContractError(f"database not found: {db_path}")
    cohort = freeze_phase3_cohort(
        db_path=db_path,
        data_root=data_root,
        preference_event_count=1,
        insight_note_count=int(args.insight_notes),
    )
    plan = build_weighted_insight_plan(cohort)
    private_values: list[object] = [
        cohort.preference_events,
        cohort.existing_preference,
        cohort.soul_profile,
        cohort.insight_notes,
        cohort.all_insights,
    ]
    if args.mode == "render-only":
        artifact = build_render_artifact(cohort, plan)
        write_artifact(Path(args.output), artifact, private_values=private_values)
        return 0 if cast("Mapping[str, object]", artifact["gate"])["passed"] else 1

    if not str(args.instance).strip():
        raise ReplayContractError("--instance is required for real-provider mode")
    route = resolve_pinned_sensetime_route(
        config,
        instance_id=str(args.instance),
        expected_model=str(args.expected_model),
        confirm_sensetime_route=bool(args.confirm_sensetime_route),
    )
    service = build_pinned_phase3_service(config, data_root=data_root, route=route)
    recorder = RecordingClient(
        service,
        max_concurrency=1,
        request_interval_seconds=float(args.request_interval_seconds),
    )
    full_baseline_usage: dict[str, int] | None = None
    full_baseline_source_sha256 = ""
    if args.full_baseline_artifact:
        full_baseline_usage, full_baseline_source_sha256 = load_full_baseline_artifact(
            Path(args.full_baseline_artifact),
            cohort=cohort,
            plan=plan,
            expected_route=route,
        )
    real = await execute_real_gate(
        cohort=cohort,
        plan=plan,
        recorder=recorder,
        expected_route=route,
        full_baseline_usage=full_baseline_usage,
        full_baseline_source_sha256=full_baseline_source_sha256,
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "real-provider",
        "cohort": {
            "snapshot_digest": cohort.snapshot_digest,
            "insight_note_count": len(cohort.insight_notes),
            "durable_hypothesis_count": len(cohort.all_insights),
        },
        "expected_route": {
            "provider": route.provider_type,
            "instance_id": route.instance_id,
            "model": route.model,
            "fallback_disabled": True,
        },
        "render": plan.summary,
        **real,
        "calls": list(recorder.calls),
    }
    write_artifact(
        Path(args.output),
        artifact,
        private_values=[*private_values, *recorder.response_bodies],
    )
    return 0 if cast("Mapping[str, object]", artifact["gate"])["passed"] else 1


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        exit_code = asyncio.run(run(parse_args()))
    except Exception as exc:
        logger.error("Weighted insight replay failed: %s", exc)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
