"""Privacy-safe A/A and A/B replay for Phase 2 cognition prompt views.

The default mode is render-only and never constructs an LLM provider::

    .venv/bin/python scripts/replay_cognition_token_diet.py \
        --mode render-only --output data/eval/cognition-render.json

The evidence mode requires an explicitly pinned SenseTime 日日新 instance.  A
single-instance custom ``soul`` route disables fallback by construction::

    .venv/bin/python scripts/replay_cognition_token_diet.py \
        --mode real-provider --instance sensenova-prod \
        --blind-review pass --output data/eval/cognition-real.json

Artifacts contain only digests, counts, aggregate quality metrics, sanitized
route labels, and provider-reported usage.  Prompt/profile/event text, URLs,
provider response bodies, credentials, and cookies are never serialized.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if TYPE_CHECKING:
    from openbiliclaw.config import Config
    from openbiliclaw.llm.base import LLMResponse

logger = logging.getLogger("eval.cognition_token_diet")

SCHEMA_VERSION = 2
CONTRACT_VERSION = "cognition-token-diet-v2"
LEGACY_VIEW = "legacy"
COMPACT_VIEW = "compact-v1"
_TASKS = ("preference", "awareness_confusions", "insight")
_VIEWS = (LEGACY_VIEW, COMPACT_VIEW)

_PREFERENCE_TOTAL_TOKEN_SAVINGS_MIN = 0.25
_AWARENESS_PROMPT_TOKEN_SAVINGS_MIN = 0.30
_TOP_INTEREST_OVERLAP_FLOOR = 0.70
_TOP_INTEREST_OVERLAP_TOLERANCE = 0.05
_STYLE_DRIFT_TOLERANCE = 0.10
_AWARENESS_NOTE_COUNT_TOLERANCE = 1
_AWARENESS_EVIDENCE_OVERLAP_TOLERANCE = 0.10
_INSIGHT_HYPOTHESIS_COUNT_TOLERANCE = 1
_INSIGHT_EVIDENCE_COUNT_DRIFT_TOLERANCE = 0.50
_INSIGHT_CONFIDENCE_DRIFT_TOLERANCE = 0.10

_PREFERENCE_MAX_TOKENS = 16_384
_DEFAULT_PREFERENCE_EVENTS = 200
_DEFAULT_AWARENESS_EVENTS = 300
_REPLAY_TEMPERATURE = 0.0

_ROUTE_LABEL_RE = re.compile(r"[^A-Za-z0-9._:/+@-]+")
_OPEN_TAG_RE = re.compile(r"<([a-z][a-z0-9_]*)>")
_URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://")
# Keep this whitelist aligned with InsightAnalyzer._parse_response. Replay
# accepts only a single, exact wrapper key; production's more tolerant nested
# and snippet salvage remains classified as a repair rather than strict schema.
_INSIGHT_LIST_WRAPPER_KEYS = frozenset(
    {
        "results",
        "items",
        "insights",
        "hypotheses",
        "data",
        "output",
        "list",
        "array",
    }
)
_SECRET_RE = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{8,}|SESSDATA|api[_-]?key|access[_-]?token|cookie)"
)
_SENSETIME_MARKERS = ("sense", "sensenova", "sensetime", "sensecore", "日日新", "商汤")
_FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "api_key",
        "cookie",
        "events",
        "profile",
        "prompt",
        "prompts",
        "provider_response",
        "raw_response",
        "response",
        "secret",
        "system_instruction",
        "url",
        "user_input",
    }
)


class ReplayContractError(RuntimeError):
    """Raised when the Phase 2 replay contract cannot be satisfied."""


class CompletionClient(Protocol):
    """Small completion boundary used by the real replay and test doubles."""

    async def complete_structured_task(
        self,
        *,
        system_instruction: str,
        user_input: str,
        history: list[dict[str, str]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        caller: str = "",
        reasoning_effort: str | None = None,
        inject_core_memory: bool = True,
    ) -> LLMResponse: ...


PromptBuilder = Callable[..., list[dict[str, str]]]


@dataclass(frozen=True)
class PromptBuilders:
    """Production cognition prompt-builder adapters."""

    preference: PromptBuilder
    awareness_confusions: PromptBuilder
    insight: PromptBuilder


@dataclass(frozen=True)
class FrozenCognitionCohort:
    """Private, in-memory-only cognition replay inputs."""

    preference_events: tuple[dict[str, object], ...]
    awareness_events: tuple[dict[str, object], ...]
    existing_preference: dict[str, object]
    soul_profile: dict[str, object]
    awareness_notes: tuple[dict[str, object], ...]
    active_insights: tuple[dict[str, object], ...]
    snapshot_digest: str
    preference_input_digest: str
    awareness_input_digest: str
    insight_input_digest: str
    preference_event_ids_digest: str
    awareness_event_ids_digest: str


@dataclass(frozen=True)
class RenderedPrompt:
    """One private prompt plus its public, privacy-safe measurements."""

    task: str
    view: str
    system_instruction: str
    user_input: str
    input_digest: str
    system_digest: str
    user_digest: str
    prompt_chars: int
    block_chars: dict[str, int]
    block_order: tuple[str, ...]


@dataclass(frozen=True)
class ParsedResult:
    """Parsed structured output; ``value`` is never written to the artifact."""

    task: str
    parse_success: bool
    strict_parse_success: bool
    schema_valid: bool
    repair_count: int
    value: object
    metrics: dict[str, int | float | bool]


@dataclass(frozen=True)
class ExecutionBundle:
    """Private execution results and their safe call ledger."""

    calls: tuple[dict[str, object], ...]
    parsed: Mapping[tuple[str, str], ParsedResult]
    response_bodies: tuple[str, ...]


@dataclass(frozen=True)
class PinnedRoute:
    """Validated single-instance replay route."""

    instance_id: str
    provider_type: str
    model: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sanitize_route_label(value: object) -> str:
    """Keep useful route identity while refusing endpoint/credential-shaped text."""

    text = str(value or "").strip()
    if not text:
        return ""
    if _URL_RE.search(text) or _SECRET_RE.search(text):
        return f"redacted-{_digest(text)[:12]}"
    return _ROUTE_LABEL_RE.sub("_", text)[:160]


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _fetch_event_window(
    connection: sqlite3.Connection,
    *,
    limit: int,
    max_event_id: int | None,
) -> list[dict[str, object]]:
    if limit <= 0:
        raise ReplayContractError("event cohort size must be positive")
    where = "WHERE id <= ?" if max_event_id is not None else ""
    params: tuple[object, ...] = (max_event_id, limit) if max_event_id is not None else (limit,)
    rows = connection.execute(
        f"""
        SELECT * FROM (
            SELECT * FROM events
            {where}
            ORDER BY id DESC
            LIMIT ?
        )
        ORDER BY id ASC
        """,
        params,
    ).fetchall()
    if len(rows) != limit:
        raise ReplayContractError(
            f"requested {limit} frozen events but only {len(rows)} were available"
        )
    return [dict(row) for row in rows]


def _dict_items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def freeze_cognition_cohort(
    *,
    db_path: Path,
    data_root: Path,
    preference_event_count: int = _DEFAULT_PREFERENCE_EVENTS,
    awareness_event_count: int = _DEFAULT_AWARENESS_EVENTS,
    max_event_id: int | None = None,
) -> FrozenCognitionCohort:
    """Read and freeze DB/profile inputs without mutating either source."""

    from openbiliclaw.memory.manager import MemoryManager

    connection = _read_only_connection(db_path)
    try:
        connection.execute("BEGIN")
        preference_events = _fetch_event_window(
            connection,
            limit=preference_event_count,
            max_event_id=max_event_id,
        )
        awareness_events = _fetch_event_window(
            connection,
            limit=awareness_event_count,
            max_event_id=max_event_id,
        )
    finally:
        connection.close()

    memory = MemoryManager(data_root)
    preference_layer = memory.get_layer("preference")
    soul_layer = memory.get_layer("soul")
    preference_layer.load()
    soul_layer.load()
    existing_preference = deepcopy(dict(preference_layer.data))
    soul_profile = deepcopy(dict(soul_layer.data))
    if not soul_profile:
        raise ReplayContractError(f"no soul profile found under {data_root}")

    awareness_notes = _dict_items(soul_profile.get("recent_awareness"))
    active_insights = _dict_items(soul_profile.get("active_insights"))
    preference_ids = [row.get("id") for row in preference_events]
    awareness_ids = [row.get("id") for row in awareness_events]
    preference_payload = {
        "events": preference_events,
        "existing_preference": existing_preference,
        "awareness_notes": awareness_notes,
        "active_insights": active_insights,
    }
    awareness_payload = {
        "events": awareness_events,
        "preference": existing_preference,
        "soul_profile": soul_profile,
    }
    insight_payload = {
        "awareness_notes": awareness_notes,
        "preference": existing_preference,
        "soul_profile": soul_profile,
        "active_insights": active_insights,
    }
    snapshot_payload = {
        "preference": preference_payload,
        "awareness": awareness_payload,
        "insight": insight_payload,
    }
    return FrozenCognitionCohort(
        preference_events=tuple(deepcopy(preference_events)),
        awareness_events=tuple(deepcopy(awareness_events)),
        existing_preference=existing_preference,
        soul_profile=soul_profile,
        awareness_notes=tuple(awareness_notes),
        active_insights=tuple(active_insights),
        snapshot_digest=_digest(snapshot_payload),
        preference_input_digest=_digest(preference_payload),
        awareness_input_digest=_digest(awareness_payload),
        insight_input_digest=_digest(insight_payload),
        preference_event_ids_digest=_digest(preference_ids),
        awareness_event_ids_digest=_digest(awareness_ids),
    )


def load_prompt_builders() -> PromptBuilders:
    """Resolve the Phase 2 builders lazily so other agents can land their API."""

    from openbiliclaw.llm import prompts

    return PromptBuilders(
        preference=prompts.build_preference_analysis_prompt,
        awareness_confusions=prompts.build_awareness_with_confusions_prompt,
        insight=prompts.build_insight_prompt,
    )


def _invoke_input_view_builder(
    builder: PromptBuilder,
    *,
    task: str,
    view: str,
    kwargs: Mapping[str, object],
) -> list[dict[str, str]]:
    signature = inspect.signature(builder)
    if "input_view" not in signature.parameters:
        raise ReplayContractError(
            f"{task} prompt builder does not expose input_view='legacy'|'compact-v1'; "
            "land the Phase 2 builder seam before running this replay"
        )
    messages = builder(**dict(kwargs), input_view=view)
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or any(not isinstance(message, dict) for message in messages)
    ):
        raise ReplayContractError(f"{task}/{view} builder returned an invalid message pair")
    roles = [str(message.get("role") or "") for message in messages]
    if roles != ["system", "user"]:
        raise ReplayContractError(f"{task}/{view} builder must return system then user")
    return messages


def _block_measurements(user_input: str) -> tuple[dict[str, int], tuple[str, ...]]:
    order = tuple(_OPEN_TAG_RE.findall(user_input))
    lengths: dict[str, int] = {}
    for tag in order:
        if tag in lengths:
            continue
        opening = f"<{tag}>"
        closing = f"</{tag}>"
        try:
            body = user_input.split(opening, 1)[1].split(closing, 1)[0]
        except IndexError:
            continue
        lengths[tag] = len(body.strip())
    return lengths, order


def _production_insight_prompt_kwargs(
    cohort: FrozenCognitionCohort,
) -> dict[str, object]:
    """Mirror ``InsightAnalyzer.analyze`` preprocessing before its builder."""

    from openbiliclaw.soul.insight_analyzer import InsightAnalyzer
    from openbiliclaw.soul.profile import (
        awareness_note_from_dict,
        insight_hypothesis_from_dict,
    )

    notes = [awareness_note_from_dict(dict(item)) for item in cohort.awareness_notes]
    hypotheses = [insight_hypothesis_from_dict(dict(item)) for item in cohort.active_insights]
    return {
        "awareness_notes": [InsightAnalyzer._note_to_dict(item) for item in notes],  # noqa: SLF001
        "preference_summary": deepcopy(cohort.existing_preference),
        "soul_profile": deepcopy(cohort.soul_profile),
        "existing_hypotheses": [
            InsightAnalyzer._hypothesis_to_context_dict(item)  # noqa: SLF001
            for item in hypotheses
        ],
    }


def render_cognition_prompts(
    cohort: FrozenCognitionCohort,
    *,
    builders: PromptBuilders | None = None,
) -> dict[tuple[str, str], RenderedPrompt]:
    """Render legacy and compact prompts from identical frozen inputs."""

    active_builders = builders or load_prompt_builders()
    task_inputs: dict[str, tuple[PromptBuilder, dict[str, object], str]] = {
        "preference": (
            active_builders.preference,
            {
                "events": [dict(item) for item in cohort.preference_events],
                "existing_preference": deepcopy(cohort.existing_preference),
                "awareness_notes": [dict(item) for item in cohort.awareness_notes],
                "active_insights": [dict(item) for item in cohort.active_insights],
            },
            cohort.preference_input_digest,
        ),
        "awareness_confusions": (
            active_builders.awareness_confusions,
            {
                "events": [dict(item) for item in cohort.awareness_events],
                "preference_summary": deepcopy(cohort.existing_preference),
                "soul_profile": deepcopy(cohort.soul_profile),
            },
            cohort.awareness_input_digest,
        ),
        "insight": (
            active_builders.insight,
            _production_insight_prompt_kwargs(cohort),
            cohort.insight_input_digest,
        ),
    }
    rendered: dict[tuple[str, str], RenderedPrompt] = {}
    for task in _TASKS:
        builder, kwargs, input_digest = task_inputs[task]
        for view in _VIEWS:
            messages = _invoke_input_view_builder(
                builder,
                task=task,
                view=view,
                kwargs=kwargs,
            )
            system_instruction = str(messages[0].get("content") or "")
            user_input = str(messages[1].get("content") or "")
            if not system_instruction or not user_input:
                raise ReplayContractError(f"{task}/{view} rendered an empty prompt")
            block_chars, block_order = _block_measurements(user_input)
            rendered[(task, view)] = RenderedPrompt(
                task=task,
                view=view,
                system_instruction=system_instruction,
                user_input=user_input,
                input_digest=input_digest,
                system_digest=_digest(system_instruction),
                user_digest=_digest(user_input),
                prompt_chars=len(system_instruction) + len(user_input),
                block_chars=block_chars,
                block_order=block_order,
            )
        if (
            rendered[(task, LEGACY_VIEW)].system_instruction
            != rendered[(task, COMPACT_VIEW)].system_instruction
        ):
            raise ReplayContractError(f"{task} system prompt changed between input views")
    return rendered


def _render_summary(rendered: Mapping[tuple[str, str], RenderedPrompt]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for task in _TASKS:
        legacy = rendered[(task, LEGACY_VIEW)]
        compact = rendered[(task, COMPACT_VIEW)]
        reduction = (
            (legacy.prompt_chars - compact.prompt_chars) / legacy.prompt_chars
            if legacy.prompt_chars
            else 0.0
        )
        summary[task] = {
            "input_digest": legacy.input_digest,
            "system_digest": legacy.system_digest,
            "system_byte_invariant": legacy.system_digest == compact.system_digest,
            "legacy": {
                "user_digest": legacy.user_digest,
                "prompt_chars": legacy.prompt_chars,
                "block_chars": dict(legacy.block_chars),
                "block_order": list(legacy.block_order),
            },
            "compact-v1": {
                "user_digest": compact.user_digest,
                "prompt_chars": compact.prompt_chars,
                "block_chars": dict(compact.block_chars),
                "block_order": list(compact.block_order),
            },
            "prompt_character_reduction": round(reduction, 6),
        }
    return summary


class _ParserOnlyRegistry:
    async def complete_structured_task(
        self,
        *,
        system_instruction: str,
        user_input: str,
        history: list[dict[str, str]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        caller: str = "",
        inject_core_memory: bool = True,
    ) -> LLMResponse:
        raise AssertionError("parser-only registry must never call a provider")


def _strict_json(content: str) -> object | None:
    try:
        value: object = json.loads(content)
        return value
    except (TypeError, json.JSONDecodeError):
        return None


def _strict_insight_list(value: object) -> list[object] | None:
    """Return an exact production-recognized insight list shape."""

    if isinstance(value, list):
        return value
    if not isinstance(value, dict) or len(value) != 1:
        return None
    wrapper_key, wrapped = next(iter(value.items()))
    if wrapper_key not in _INSIGHT_LIST_WRAPPER_KEYS or not isinstance(wrapped, list):
        return None
    return wrapped


def _preference_structure_metrics(value: Mapping[str, object]) -> dict[str, int | float | bool]:
    interests = _dict_items(value.get("interests"))
    raw_style = value.get("style")
    raw_context = value.get("context")
    raw_disliked = value.get("disliked_topics")
    raw_creators = value.get("favorite_up_users")
    raw_cognitive = value.get("cognitive_style")
    style = raw_style if isinstance(raw_style, Mapping) else {}
    context = raw_context if isinstance(raw_context, Mapping) else {}
    disliked = raw_disliked if isinstance(raw_disliked, list) else []
    creators = raw_creators if isinstance(raw_creators, list) else []
    cognitive = raw_cognitive if isinstance(raw_cognitive, list) else []
    valid_weights = sum(
        isinstance(item.get("weight"), int | float) and not isinstance(item.get("weight"), bool)
        for item in interests
    )
    return {
        "interest_count": len(interests),
        "valid_interest_weight_count": valid_weights,
        "style_field_count": len(style),
        "context_field_count": len(context),
        "disliked_topic_count": len(disliked),
        "favorite_creator_count": len(creators),
        "cognitive_style_count": len(cognitive),
    }


def _coerce_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | str | float):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _awareness_structure_metrics(
    notes: Sequence[object],
    confusions: Sequence[Mapping[str, object]],
    *,
    allowed_event_ids: set[int],
) -> dict[str, int | float | bool]:
    valid_notes = [item for item in notes if isinstance(item, dict)]
    nonempty = sum(bool(str(item.get("observation") or "").strip()) for item in valid_notes)
    citations = [_coerce_ids(item.get("source_event_ids")) for item in valid_notes]
    cited = [event_id for group in citations for event_id in group]
    invalid = [event_id for event_id in cited if event_id not in allowed_event_ids]
    return {
        "note_count": len(valid_notes),
        "nonempty_observation_count": nonempty,
        "confusion_count": len(confusions),
        "cited_event_count": len(cited),
        "out_of_cohort_citation_count": len(invalid),
        "evidence_attribution_valid": not invalid,
    }


def _insight_structure_metrics(
    hypotheses: Sequence[object],
) -> dict[str, int | float | bool]:
    items = [item for item in hypotheses if isinstance(item, dict)]
    nonempty_hypotheses = 0
    valid_evidence_lists = 0
    valid_confidences = 0
    valid_structure_count = 0
    evidence_counts: list[int] = []
    confidences: list[float] = []
    for item in items:
        raw_hypothesis = item.get("hypothesis")
        hypothesis_valid = isinstance(raw_hypothesis, str) and bool(raw_hypothesis.strip())
        if hypothesis_valid:
            nonempty_hypotheses += 1
        raw_evidence = item.get("evidence")
        evidence = (
            [value.strip() for value in raw_evidence if isinstance(value, str) and value.strip()]
            if isinstance(raw_evidence, list)
            else []
        )
        evidence_counts.append(len(evidence))
        evidence_valid = (
            isinstance(raw_evidence, list)
            and len(evidence) == len(raw_evidence)
            and 1 <= len(evidence) <= 3
        )
        if evidence_valid:
            valid_evidence_lists += 1
        raw_confidence = item.get("confidence")
        confidence_valid = (
            isinstance(raw_confidence, int | float)
            and not isinstance(raw_confidence, bool)
            and 0.0 <= float(raw_confidence) <= 1.0
        )
        if confidence_valid:
            valid_confidences += 1
            assert isinstance(raw_confidence, int | float)
            confidences.append(float(raw_confidence))
        else:
            confidences.append(0.0)
        if hypothesis_valid and evidence_valid and confidence_valid:
            valid_structure_count += 1
    # Count every array entry so an otherwise-valid object cannot hide a
    # malformed sibling that the tolerant production parser would skip.
    count = len(hypotheses)
    return {
        "hypothesis_count": count,
        "nonempty_hypothesis_count": nonempty_hypotheses,
        "valid_evidence_list_count": valid_evidence_lists,
        "valid_confidence_count": valid_confidences,
        "valid_structure_count": valid_structure_count,
        "mean_evidence_count": round(sum(evidence_counts) / count, 6) if count else 0.0,
        "mean_confidence": round(sum(confidences) / count, 6) if count else 0.0,
        "structure_valid": count == valid_structure_count,
    }


def parse_structured_result(
    *,
    task: str,
    content: str,
    allowed_event_ids: set[int],
) -> ParsedResult:
    """Parse with production analyzers and classify tolerant salvage as repair."""

    strict_value = _strict_json(content)
    strict_success = strict_value is not None
    try:
        if task == "preference":
            from openbiliclaw.soul.preference_analyzer import PreferenceAnalyzer

            preference_analyzer = PreferenceAnalyzer(registry=_ParserOnlyRegistry())
            raw = preference_analyzer._parse_response(content, log_error=False)  # noqa: SLF001
            normalized_preference = preference_analyzer._normalize_preference(  # noqa: SLF001
                raw
            )
            strict_schema = isinstance(strict_value, dict)
            metrics = _preference_structure_metrics(normalized_preference)
            schema_valid = strict_schema and isinstance(
                normalized_preference.get("interests"),
                list,
            )
            return ParsedResult(
                task=task,
                parse_success=True,
                strict_parse_success=strict_success,
                schema_valid=schema_valid,
                repair_count=int(not strict_success),
                value=normalized_preference,
                metrics=metrics,
            )
        if task == "awareness_confusions":
            from openbiliclaw.soul.awareness_analyzer import AwarenessAnalyzer

            awareness_analyzer = AwarenessAnalyzer(registry=_ParserOnlyRegistry())
            notes, confusions = awareness_analyzer._parse_with_confusions(  # noqa: SLF001
                content
            )
            strict_schema = (
                isinstance(strict_value, dict)
                and isinstance(strict_value.get("notes"), list)
                and isinstance(strict_value.get("confusions"), list)
            )
            metrics = _awareness_structure_metrics(
                notes,
                confusions,
                allowed_event_ids=allowed_event_ids,
            )
            schema_valid = (
                strict_schema and metrics["note_count"] == metrics["nonempty_observation_count"]
            )
            return ParsedResult(
                task=task,
                parse_success=True,
                strict_parse_success=strict_success,
                schema_valid=bool(schema_valid),
                repair_count=int(not strict_schema),
                value={"notes": notes, "confusions": confusions},
                metrics=metrics,
            )
        if task == "insight":
            from openbiliclaw.soul.insight_analyzer import InsightAnalyzer

            insight_analyzer = InsightAnalyzer(registry=_ParserOnlyRegistry())
            raw_hypotheses = insight_analyzer._parse_response(content)  # noqa: SLF001
            strict_hypotheses = _strict_insight_list(strict_value)
            strict_schema = strict_hypotheses is not None
            metrics_source = strict_hypotheses if strict_hypotheses is not None else raw_hypotheses
            metrics = _insight_structure_metrics(metrics_source)
            schema_valid = strict_schema and bool(metrics["structure_valid"])
            normalized_hypotheses: list[dict[str, object]] = []
            for item in raw_hypotheses:
                if not isinstance(item, dict):
                    continue
                hypothesis = insight_analyzer._build_hypothesis(item)  # noqa: SLF001
                normalized_hypotheses.append(
                    {
                        "hypothesis": hypothesis.hypothesis,
                        "evidence": list(hypothesis.evidence),
                        "confidence": hypothesis.confidence,
                    }
                )
            return ParsedResult(
                task=task,
                parse_success=True,
                strict_parse_success=strict_success,
                schema_valid=bool(schema_valid),
                repair_count=int(not strict_schema),
                value=normalized_hypotheses,
                metrics=metrics,
            )
        raise ReplayContractError(f"unsupported cognition task: {task}")
    except Exception:
        logger.warning("structured parse failed for %s replay", task, exc_info=True)
        return ParsedResult(
            task=task,
            parse_success=False,
            strict_parse_success=strict_success,
            schema_valid=False,
            repair_count=0,
            value={},
            metrics={},
        )


def _usage_value(usage: Mapping[object, object], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def normalize_provider_usage(response: object) -> dict[str, int | bool] | None:
    """Return only provider-reported counters; never estimate a missing total."""

    raw = getattr(response, "usage", None)
    if not isinstance(raw, Mapping):
        return None
    prompt = _usage_value(raw, "prompt_tokens", "input_tokens")
    completion = _usage_value(raw, "completion_tokens", "output_tokens")
    total = _usage_value(raw, "total_tokens")
    if prompt is None or completion is None or total is None:
        return None
    cached = _usage_value(
        raw,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "prompt_cache_hit_tokens",
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_input_tokens": cached or 0,
        "cache_metric_reported": cached is not None,
        "uncached_input_tokens": max(prompt - (cached or 0), 0),
    }


def _call_plan() -> tuple[tuple[str, str, str], ...]:
    return (
        ("control", "A1", LEGACY_VIEW),
        ("control", "A2", LEGACY_VIEW),
        ("treatment", "A", LEGACY_VIEW),
        ("treatment", "B", COMPACT_VIEW),
    )


def _event_ids(events: Sequence[Mapping[str, object]]) -> set[int]:
    result: set[int] = set()
    for event in events:
        raw_id = event.get("id")
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            result.add(raw_id)
    return result


async def execute_real_replay(
    *,
    rendered: Mapping[tuple[str, str], RenderedPrompt],
    cohort: FrozenCognitionCohort,
    client: CompletionClient,
) -> ExecutionBundle:
    """Execute matched A1/A2 and A/B calls without persisting response text."""

    # Awareness and insight run through CognitionCycle's shared output budget.
    # Import the production constant instead of copying it so replay cannot
    # silently compare arms under a stale truncation envelope.
    from openbiliclaw.soul.cognition_cycle import _COGNITION_MAX_TOKENS

    calls: list[dict[str, object]] = []
    parsed_by_run: dict[tuple[str, str], ParsedResult] = {}
    response_bodies: list[str] = []
    allowed_ids: dict[str, set[int]] = {
        "preference": _event_ids(cohort.preference_events),
        "awareness_confusions": _event_ids(cohort.awareness_events),
        "insight": set(),
    }
    max_tokens = {
        "preference": _PREFERENCE_MAX_TOKENS,
        "awareness_confusions": _COGNITION_MAX_TOKENS,
        "insight": _COGNITION_MAX_TOKENS,
    }
    caller = {
        "preference": "soul.preference.replay",
        "awareness_confusions": "soul.awareness_confusions.replay",
        "insight": "soul.insight.replay",
    }
    for task in _TASKS:
        for pair_kind, logical_run, view in _call_plan():
            prompt = rendered[(task, view)]
            record: dict[str, object] = {
                "task": task,
                "pair_kind": pair_kind,
                "logical_run": logical_run,
                "input_view": view,
                "input_digest": prompt.input_digest,
                "system_digest": prompt.system_digest,
                "user_digest": prompt.user_digest,
                "prompt_chars": prompt.prompt_chars,
                "max_tokens": max_tokens[task],
                "temperature": _REPLAY_TEMPERATURE,
            }
            try:
                response = await client.complete_structured_task(
                    system_instruction=prompt.system_instruction,
                    user_input=prompt.user_input,
                    max_tokens=max_tokens[task],
                    temperature=_REPLAY_TEMPERATURE,
                    caller=caller[task],
                    inject_core_memory=False,
                )
            except Exception as exc:
                record.update(
                    {
                        "status": "provider-error",
                        "error_kind": type(exc).__name__,
                        "route": {"provider": "", "instance_id": "", "model": ""},
                        "usage": None,
                        "parse": {
                            "success": False,
                            "strict_success": False,
                            "schema_valid": False,
                            "repair_count": 0,
                        },
                        "structure": {},
                    }
                )
                calls.append(record)
                continue
            content = str(getattr(response, "content", "") or "")
            response_bodies.append(content)
            parsed = parse_structured_result(
                task=task,
                content=content,
                allowed_event_ids=allowed_ids[task],
            )
            parsed_by_run[(task, logical_run)] = parsed
            record.update(
                {
                    "status": "ok",
                    "route": {
                        "provider": _sanitize_route_label(getattr(response, "provider", "")),
                        "instance_id": _sanitize_route_label(getattr(response, "instance_id", "")),
                        "model": _sanitize_route_label(getattr(response, "model", "")),
                    },
                    "usage": normalize_provider_usage(response),
                    "parse": {
                        "success": parsed.parse_success,
                        "strict_success": parsed.strict_parse_success,
                        "schema_valid": parsed.schema_valid,
                        "repair_count": parsed.repair_count,
                    },
                    "structure": dict(parsed.metrics),
                }
            )
            calls.append(record)
    return ExecutionBundle(
        calls=tuple(calls),
        parsed=parsed_by_run,
        response_bodies=tuple(response_bodies),
    )


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip().casefold() for item in value if str(item).strip()}


def _interest_weights(value: object, *, top_k: int = 12) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    weighted: list[tuple[str, float]] = []
    for item in _dict_items(value.get("interests")):
        name = str(item.get("name") or "").strip().casefold()
        raw_weight = item.get("weight")
        weight = (
            float(raw_weight)
            if isinstance(raw_weight, int | float) and not isinstance(raw_weight, bool)
            else 0.0
        )
        if name:
            weighted.append((name, max(0.0, min(1.0, weight))))
    weighted.sort(key=lambda entry: (-entry[1], entry[0]))
    return dict(weighted[:top_k])


def _weighted_jaccard(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    keys = set(left) | set(right)
    if not keys:
        return 1.0
    numerator = sum(min(left.get(key, 0.0), right.get(key, 0.0)) for key in keys)
    denominator = sum(max(left.get(key, 0.0), right.get(key, 0.0)) for key in keys)
    return numerator / denominator if denominator else 1.0


def _style_distance(left: object, right: object) -> float:
    left_style = left.get("style") if isinstance(left, dict) else None
    right_style = right.get("style") if isinstance(right, dict) else None
    if not isinstance(left_style, dict) or not isinstance(right_style, dict):
        return 1.0
    keys = set(left_style) | set(right_style)
    if not keys:
        return 0.0
    distances: list[float] = []
    for key in keys:
        left_value = left_style.get(key)
        right_value = right_style.get(key)
        if (
            isinstance(left_value, int | float)
            and not isinstance(left_value, bool)
            and isinstance(right_value, int | float)
            and not isinstance(right_value, bool)
        ):
            distances.append(min(1.0, abs(float(left_value) - float(right_value))))
        else:
            distances.append(float(str(left_value or "") != str(right_value or "")))
    return sum(distances) / len(distances)


def _metadata_mapping(raw: object) -> Mapping[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _creator_evidence(cohort: FrozenCognitionCohort) -> set[str]:
    creators = _string_set(cohort.existing_preference.get("favorite_up_users"))
    creator_keys = ("author", "author_name", "creator", "creator_name", "up_name")
    for event in (*cohort.preference_events, *cohort.awareness_events):
        metadata = _metadata_mapping(event.get("metadata"))
        for key in creator_keys:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                creators.add(value.strip().casefold())
        owner = metadata.get("owner")
        if isinstance(owner, dict):
            value = owner.get("name")
            if isinstance(value, str) and value.strip():
                creators.add(value.strip().casefold())
    return creators


def _preference_pair_quality(
    left: ParsedResult | None,
    right: ParsedResult | None,
    *,
    creator_evidence: set[str],
) -> dict[str, object]:
    if left is None or right is None or not left.parse_success or not right.parse_success:
        return {"comparable": False}
    left_value = left.value if isinstance(left.value, dict) else {}
    right_value = right.value if isinstance(right.value, dict) else {}
    left_creators = _string_set(left_value.get("favorite_up_users"))
    right_creators = _string_set(right_value.get("favorite_up_users"))
    supported_left = left_creators & creator_evidence
    supported_right = right_creators & creator_evidence
    return {
        "comparable": True,
        "top_interest_weighted_overlap": round(
            _weighted_jaccard(
                _interest_weights(left_value),
                _interest_weights(right_value),
            ),
            6,
        ),
        "style_drift": round(_style_distance(left_value, right_value), 6),
        "right_hallucinated_creator_count": len(right_creators - creator_evidence),
        "right_creator_evidence_loss_count": len(supported_left - supported_right),
        "disliked_topic_count_delta": len(_string_set(right_value.get("disliked_topics")))
        - len(_string_set(left_value.get("disliked_topics"))),
    }


def _normalized_observations(result: ParsedResult | None) -> set[str]:
    if result is None or not isinstance(result.value, dict):
        return set()
    notes = result.value.get("notes")
    if not isinstance(notes, list):
        return set()
    return {
        "".join(str(item.get("observation") or "").split()).casefold()
        for item in notes
        if isinstance(item, dict) and str(item.get("observation") or "").strip()
    }


def _awareness_evidence_ids(result: ParsedResult | None) -> set[int]:
    if result is None or not isinstance(result.value, dict):
        return set()
    notes = result.value.get("notes")
    if not isinstance(notes, list):
        return set()
    evidence_ids: set[int] = set()
    for item in notes:
        if isinstance(item, dict):
            evidence_ids.update(_coerce_ids(item.get("source_event_ids")))
    return evidence_ids


def _set_jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _id_set_jaccard(left: set[int], right: set[int]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _awareness_pair_quality(
    left: ParsedResult | None,
    right: ParsedResult | None,
) -> dict[str, object]:
    if left is None or right is None or not left.parse_success or not right.parse_success:
        return {"comparable": False}
    left_count = int(left.metrics.get("note_count", 0))
    right_count = int(right.metrics.get("note_count", 0))
    return {
        "comparable": True,
        # Kept as a diagnostic only. It is deliberately not a gate because
        # semantically equivalent observations are routinely paraphrased.
        "observation_exact_overlap": round(
            _set_jaccard(_normalized_observations(left), _normalized_observations(right)),
            6,
        ),
        "source_event_id_overlap": round(
            _id_set_jaccard(
                _awareness_evidence_ids(left),
                _awareness_evidence_ids(right),
            ),
            6,
        ),
        "note_count_delta": abs(left_count - right_count),
        "right_cited_event_count": int(right.metrics.get("cited_event_count", 0)),
        "right_out_of_cohort_citation_count": int(
            right.metrics.get("out_of_cohort_citation_count", 0)
        ),
    }


def _insight_pair_quality(
    left: ParsedResult | None,
    right: ParsedResult | None,
) -> dict[str, object]:
    if left is None or right is None or not left.parse_success or not right.parse_success:
        return {"comparable": False}
    left_count = int(left.metrics.get("hypothesis_count", 0))
    right_count = int(right.metrics.get("hypothesis_count", 0))
    left_evidence = _float_metric(left.metrics, "mean_evidence_count", 0.0)
    right_evidence = _float_metric(right.metrics, "mean_evidence_count", 0.0)
    left_confidence = _float_metric(left.metrics, "mean_confidence", 0.0)
    right_confidence = _float_metric(right.metrics, "mean_confidence", 0.0)
    return {
        "comparable": True,
        "hypothesis_count_delta": abs(left_count - right_count),
        "mean_evidence_count_drift": round(abs(left_evidence - right_evidence), 6),
        "mean_confidence_drift": round(abs(left_confidence - right_confidence), 6),
        "right_invalid_structure_count": max(
            0,
            right_count - int(right.metrics.get("valid_structure_count", 0)),
        ),
    }


def _contains_explicit_dislike(cohort: FrozenCognitionCohort) -> bool:
    for event in cohort.preference_events:
        event_type = str(event.get("event_type") or "").strip().lower()
        metadata = _metadata_mapping(event.get("metadata"))
        feedback = str(metadata.get("feedback_type") or "").strip().lower()
        reaction = str(metadata.get("reaction") or "").strip().lower()
        if event_type == "dislike" or feedback == "dislike" or reaction == "thumbs_down":
            return True
    return False


def _contains_retraction(cohort: FrozenCognitionCohort) -> bool:
    for event in cohort.preference_events:
        metadata = _metadata_mapping(event.get("metadata"))
        feedback = str(metadata.get("feedback_type") or "").strip().lower()
        retracted_action = str(metadata.get("retracted_action") or "").strip().lower()
        if feedback == "retraction" or bool(retracted_action):
            return True
    return False


def structural_quality_summary(
    bundle: ExecutionBundle,
    *,
    cohort: FrozenCognitionCohort,
) -> dict[str, object]:
    creators = _creator_evidence(cohort)
    preference_aa = _preference_pair_quality(
        bundle.parsed.get(("preference", "A1")),
        bundle.parsed.get(("preference", "A2")),
        creator_evidence=creators,
    )
    preference_ab = _preference_pair_quality(
        bundle.parsed.get(("preference", "A")),
        bundle.parsed.get(("preference", "B")),
        creator_evidence=creators,
    )
    awareness_aa = _awareness_pair_quality(
        bundle.parsed.get(("awareness_confusions", "A1")),
        bundle.parsed.get(("awareness_confusions", "A2")),
    )
    awareness_ab = _awareness_pair_quality(
        bundle.parsed.get(("awareness_confusions", "A")),
        bundle.parsed.get(("awareness_confusions", "B")),
    )
    insight_aa = _insight_pair_quality(
        bundle.parsed.get(("insight", "A1")),
        bundle.parsed.get(("insight", "A2")),
    )
    insight_ab = _insight_pair_quality(
        bundle.parsed.get(("insight", "A")),
        bundle.parsed.get(("insight", "B")),
    )
    return {
        "input_signals": {
            "contains_explicit_dislike": _contains_explicit_dislike(cohort),
            "contains_retraction": _contains_retraction(cohort),
            "creator_evidence_count": len(creators),
        },
        "preference": {"control_aa": preference_aa, "treatment_ab": preference_ab},
        "awareness_confusions": {
            "control_aa": awareness_aa,
            "treatment_ab": awareness_ab,
        },
        "insight": {"control_aa": insight_aa, "treatment_ab": insight_ab},
    }


def _call_by_run(
    calls: Sequence[Mapping[str, object]],
    *,
    task: str,
    logical_run: str,
) -> Mapping[str, object] | None:
    return next(
        (
            call
            for call in calls
            if call.get("task") == task and call.get("logical_run") == logical_run
        ),
        None,
    )


def route_audit(
    calls: Sequence[Mapping[str, object]],
    *,
    expected: PinnedRoute,
    expected_call_count: int | None = None,
) -> dict[str, object]:
    """Fail closed on fallback, mixed route, missing usage, or model drift."""

    expected_instance = _sanitize_route_label(expected.instance_id)
    expected_provider = _sanitize_route_label(expected.provider_type)
    expected_model = _sanitize_route_label(expected.model)
    drifted: list[str] = []
    observed_routes: set[tuple[str, str, str]] = set()
    missing_usage: list[str] = []
    for call in calls:
        label = f"{call.get('task')}:{call.get('logical_run')}"
        route = call.get("route")
        if not isinstance(route, Mapping):
            drifted.append(label)
            continue
        observed = (
            str(route.get("provider") or ""),
            str(route.get("instance_id") or ""),
            str(route.get("model") or ""),
        )
        observed_routes.add(observed)
        if (
            call.get("status") != "ok"
            or observed[0] != expected_provider
            or observed[1] != expected_instance
            or not observed[2]
            or (expected_model and observed[2] != expected_model)
        ):
            drifted.append(label)
        if not isinstance(call.get("usage"), Mapping):
            missing_usage.append(label)
    required_call_count = (
        len(_TASKS) * len(_call_plan()) if expected_call_count is None else expected_call_count
    )
    passed = (
        len(calls) == required_call_count
        and not drifted
        and not missing_usage
        and len(observed_routes) == 1
    )
    return {
        "passed": passed,
        "fallback_disabled": True,
        "expected": {
            "provider": expected_provider,
            "instance_id": expected_instance,
            "model": expected_model,
        },
        "observed_route_count": len(observed_routes),
        "route_drift_calls": drifted,
        "missing_usage_calls": missing_usage,
    }


def _token_savings(
    control: Mapping[str, object] | None,
    treatment: Mapping[str, object] | None,
    *,
    field: str,
) -> float | None:
    if control is None or treatment is None:
        return None
    control_usage = control.get("usage")
    treatment_usage = treatment.get("usage")
    if not isinstance(control_usage, Mapping) or not isinstance(treatment_usage, Mapping):
        return None
    control_value = control_usage.get(field)
    treatment_value = treatment_usage.get(field)
    if (
        not isinstance(control_value, int)
        or isinstance(control_value, bool)
        or control_value <= 0
        or not isinstance(treatment_value, int)
        or isinstance(treatment_value, bool)
    ):
        return None
    return (control_value - treatment_value) / control_value


def token_gate(calls: Sequence[Mapping[str, object]]) -> dict[str, object]:
    preference_savings = _token_savings(
        _call_by_run(calls, task="preference", logical_run="A"),
        _call_by_run(calls, task="preference", logical_run="B"),
        field="total_tokens",
    )
    awareness_legacy_tokens: list[int] = []
    for logical_run in ("A1", "A2", "A"):
        call = _call_by_run(calls, task="awareness_confusions", logical_run=logical_run)
        usage = call.get("usage") if call is not None else None
        value = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            awareness_legacy_tokens.append(value)
    awareness_b = _call_by_run(calls, task="awareness_confusions", logical_run="B")
    awareness_b_usage = awareness_b.get("usage") if awareness_b is not None else None
    awareness_b_prompt = (
        awareness_b_usage.get("prompt_tokens") if isinstance(awareness_b_usage, Mapping) else None
    )
    awareness_control_median = (
        float(median(awareness_legacy_tokens)) if len(awareness_legacy_tokens) == 3 else None
    )
    awareness_savings = (
        (awareness_control_median - awareness_b_prompt) / awareness_control_median
        if awareness_control_median is not None
        and isinstance(awareness_b_prompt, int)
        and not isinstance(awareness_b_prompt, bool)
        else None
    )
    insight_savings = _token_savings(
        _call_by_run(calls, task="insight", logical_run="A"),
        _call_by_run(calls, task="insight", logical_run="B"),
        field="prompt_tokens",
    )
    passed = (
        preference_savings is not None
        and preference_savings >= _PREFERENCE_TOTAL_TOKEN_SAVINGS_MIN
        and awareness_savings is not None
        and awareness_savings >= _AWARENESS_PROMPT_TOKEN_SAVINGS_MIN
    )
    return {
        "passed": passed,
        "preference_total_tokens_per_event_savings": (
            round(preference_savings, 6) if preference_savings is not None else None
        ),
        "awareness_prompt_token_savings": (
            round(awareness_savings, 6) if awareness_savings is not None else None
        ),
        "awareness_legacy_prompt_tokens_median": awareness_control_median,
        # The Phase 2 spec declares hard token thresholds for preference and
        # awareness only. Insight is now measured from the matched real arm but
        # remains diagnostic until a calibrated threshold is declared.
        "insight_prompt_token_savings": (
            round(insight_savings, 6) if insight_savings is not None else None
        ),
        "constants": {
            "preference_total_tokens_per_event_savings_min": (_PREFERENCE_TOTAL_TOKEN_SAVINGS_MIN),
            "awareness_prompt_token_savings_min": _AWARENESS_PROMPT_TOKEN_SAVINGS_MIN,
        },
    }


def _float_metric(metrics: Mapping[str, object], key: str, default: float) -> float:
    value = metrics.get(key)
    return (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default
    )


def _int_metric(metrics: Mapping[str, object], key: str, default: int) -> int:
    value = metrics.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _task_parse_schema_ok(
    calls: Sequence[Mapping[str, object]],
    *,
    task: str,
) -> bool:
    task_calls = [call for call in calls if call.get("task") == task]
    expected_runs = {logical_run for _, logical_run, _ in _call_plan()}
    if (
        len(task_calls) != len(_call_plan())
        or {str(call.get("logical_run") or "") for call in task_calls} != expected_runs
    ):
        return False
    for call in task_calls:
        parse_record = call.get("parse")
        if (
            call.get("status") != "ok"
            or not isinstance(parse_record, Mapping)
            or parse_record.get("success") is not True
            or parse_record.get("schema_valid") is not True
        ):
            return False
    return True


def _repair_counts(
    calls: Sequence[Mapping[str, object]],
    *,
    task: str | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for logical_run in ("A1", "A2", "A", "B"):
        repair_count = 0
        for call in calls:
            parse_record = call.get("parse")
            if (
                call.get("logical_run") == logical_run
                and (task is None or call.get("task") == task)
                and isinstance(parse_record, Mapping)
            ):
                repair_count += _int_metric(parse_record, "repair_count", 0)
        counts[logical_run] = repair_count
    return counts


def _repair_within_control_envelope(counts: Mapping[str, int]) -> bool:
    return counts.get("B", 0) <= max(
        counts.get("A1", 0),
        counts.get("A2", 0),
        counts.get("A", 0),
    )


def quality_gate(
    calls: Sequence[Mapping[str, object]],
    quality: Mapping[str, object],
    *,
    blind_review: str,
) -> dict[str, object]:
    task_parse_ok = {task: _task_parse_schema_ok(calls, task=task) for task in _TASKS}
    parse_ok = all(task_parse_ok.values())

    preference = quality.get("preference")
    awareness = quality.get("awareness_confusions")
    insight = quality.get("insight")
    preference_map = preference if isinstance(preference, Mapping) else {}
    awareness_map = awareness if isinstance(awareness, Mapping) else {}
    insight_map = insight if isinstance(insight, Mapping) else {}
    pref_aa_raw = preference_map.get("control_aa")
    pref_ab_raw = preference_map.get("treatment_ab")
    aware_aa_raw = awareness_map.get("control_aa")
    aware_ab_raw = awareness_map.get("treatment_ab")
    insight_aa_raw = insight_map.get("control_aa")
    insight_ab_raw = insight_map.get("treatment_ab")
    pref_aa = pref_aa_raw if isinstance(pref_aa_raw, Mapping) else {}
    pref_ab = pref_ab_raw if isinstance(pref_ab_raw, Mapping) else {}
    aware_aa = aware_aa_raw if isinstance(aware_aa_raw, Mapping) else {}
    aware_ab = aware_ab_raw if isinstance(aware_ab_raw, Mapping) else {}
    insight_aa = insight_aa_raw if isinstance(insight_aa_raw, Mapping) else {}
    insight_ab = insight_ab_raw if isinstance(insight_ab_raw, Mapping) else {}

    overlap_floor = max(
        _TOP_INTEREST_OVERLAP_FLOOR,
        _float_metric(pref_aa, "top_interest_weighted_overlap", 1.0)
        - _TOP_INTEREST_OVERLAP_TOLERANCE,
    )
    style_ceiling = _float_metric(pref_aa, "style_drift", 0.0) + _STYLE_DRIFT_TOLERANCE
    awareness_overlap_floor = max(
        0.0,
        _float_metric(aware_aa, "source_event_id_overlap", 1.0)
        - _AWARENESS_EVIDENCE_OVERLAP_TOLERANCE,
    )
    awareness_count_ceiling = _int_metric(aware_aa, "note_count_delta", 0) + (
        _AWARENESS_NOTE_COUNT_TOLERANCE
    )
    insight_count_ceiling = _int_metric(insight_aa, "hypothesis_count_delta", 0) + (
        _INSIGHT_HYPOTHESIS_COUNT_TOLERANCE
    )
    insight_evidence_drift_ceiling = (
        _float_metric(insight_aa, "mean_evidence_count_drift", 0.0)
        + _INSIGHT_EVIDENCE_COUNT_DRIFT_TOLERANCE
    )
    insight_confidence_drift_ceiling = (
        _float_metric(insight_aa, "mean_confidence_drift", 0.0)
        + _INSIGHT_CONFIDENCE_DRIFT_TOLERANCE
    )

    repair_counts = _repair_counts(calls)
    repair_ok = _repair_within_control_envelope(repair_counts)
    task_repair_counts = {task: _repair_counts(calls, task=task) for task in _TASKS}
    task_repair_ok = {
        task: _repair_within_control_envelope(task_repair_counts[task]) for task in _TASKS
    }

    input_signals = quality.get("input_signals")
    signals = input_signals if isinstance(input_signals, Mapping) else {}
    explicit_dislike_ok = True
    if signals.get("contains_explicit_dislike") is True:
        treatment_b = _call_by_run(calls, task="preference", logical_run="B")
        structure = treatment_b.get("structure") if treatment_b else None
        explicit_dislike_ok = (
            isinstance(structure, Mapping) and _int_metric(structure, "disliked_topic_count", 0) > 0
        )
    retraction_ok = signals.get("contains_retraction") is not True or (
        pref_ab.get("comparable") is True
        and _float_metric(pref_ab, "top_interest_weighted_overlap", 0.0) >= overlap_floor
        and _float_metric(pref_ab, "style_drift", 1.0) <= style_ceiling
    )
    awareness_citations_valid = True
    for call in calls:
        if call.get("task") != "awareness_confusions":
            continue
        structure = call.get("structure")
        if not isinstance(structure, Mapping) or (
            _int_metric(structure, "out_of_cohort_citation_count", 1) != 0
        ):
            awareness_citations_valid = False
            break

    automatic_checks = {
        "parse_and_schema_100_percent": parse_ok,
        "repair_within_control_envelope": repair_ok,
        "top_interest_overlap_within_envelope": (
            pref_ab.get("comparable") is True
            and _float_metric(pref_ab, "top_interest_weighted_overlap", 0.0) >= overlap_floor
        ),
        "style_drift_within_envelope": (
            pref_ab.get("comparable") is True
            and _float_metric(pref_ab, "style_drift", 1.0) <= style_ceiling
        ),
        "no_treatment_creator_hallucination": int(
            pref_ab.get("right_hallucinated_creator_count", 1) or 0
        )
        == 0,
        "no_treatment_creator_evidence_loss": int(
            pref_ab.get("right_creator_evidence_loss_count", 1) or 0
        )
        == 0,
        "explicit_dislike_preserved": explicit_dislike_ok,
        "retraction_handling_within_control_envelope": retraction_ok,
        "awareness_note_count_within_envelope": (
            aware_ab.get("comparable") is True
            and int(aware_ab.get("note_count_delta", awareness_count_ceiling + 1) or 0)
            <= awareness_count_ceiling
        ),
        "awareness_overlap_within_envelope": (
            aware_ab.get("comparable") is True
            and _float_metric(aware_ab, "source_event_id_overlap", 0.0) >= awareness_overlap_floor
        ),
        "awareness_evidence_attribution_valid": awareness_citations_valid
        and int(aware_ab.get("right_out_of_cohort_citation_count", 1) or 0) == 0,
        "insight_treatment_structure_valid": insight_ab.get("comparable") is True
        and int(insight_ab.get("right_invalid_structure_count", 1) or 0) == 0,
        "insight_hypothesis_count_within_envelope": insight_ab.get("comparable") is True
        and int(insight_ab.get("hypothesis_count_delta", insight_count_ceiling + 1) or 0)
        <= insight_count_ceiling,
        "insight_evidence_structure_within_envelope": insight_ab.get("comparable") is True
        and _float_metric(insight_ab, "mean_evidence_count_drift", float("inf"))
        <= insight_evidence_drift_ceiling,
        "insight_confidence_drift_within_envelope": insight_ab.get("comparable") is True
        and _float_metric(insight_ab, "mean_confidence_drift", float("inf"))
        <= insight_confidence_drift_ceiling,
    }
    task_checks = {
        "preference": {
            "parse_and_schema_100_percent": task_parse_ok["preference"],
            "repair_within_control_envelope": task_repair_ok["preference"],
            "top_interest_overlap_within_envelope": automatic_checks[
                "top_interest_overlap_within_envelope"
            ],
            "style_drift_within_envelope": automatic_checks["style_drift_within_envelope"],
            "no_treatment_creator_hallucination": automatic_checks[
                "no_treatment_creator_hallucination"
            ],
            "no_treatment_creator_evidence_loss": automatic_checks[
                "no_treatment_creator_evidence_loss"
            ],
            "explicit_dislike_preserved": automatic_checks["explicit_dislike_preserved"],
            "retraction_handling_within_control_envelope": automatic_checks[
                "retraction_handling_within_control_envelope"
            ],
            "blind_review_passed": blind_review == "pass",
        },
        "awareness_confusions": {
            "parse_and_schema_100_percent": task_parse_ok["awareness_confusions"],
            "repair_within_control_envelope": task_repair_ok["awareness_confusions"],
            "note_count_within_envelope": automatic_checks["awareness_note_count_within_envelope"],
            "source_event_id_overlap_within_envelope": automatic_checks[
                "awareness_overlap_within_envelope"
            ],
            "evidence_attribution_valid": automatic_checks["awareness_evidence_attribution_valid"],
            "blind_review_passed": blind_review == "pass",
        },
        "insight": {
            "parse_and_schema_100_percent": task_parse_ok["insight"],
            "repair_within_control_envelope": task_repair_ok["insight"],
            "treatment_structure_valid": automatic_checks["insight_treatment_structure_valid"],
            "hypothesis_count_within_envelope": automatic_checks[
                "insight_hypothesis_count_within_envelope"
            ],
            "evidence_structure_within_envelope": automatic_checks[
                "insight_evidence_structure_within_envelope"
            ],
            "confidence_drift_within_envelope": automatic_checks[
                "insight_confidence_drift_within_envelope"
            ],
            "blind_review_passed": blind_review == "pass",
        },
    }
    task_passed = {
        task: all(bool(value) for value in checks.values()) for task, checks in task_checks.items()
    }
    passed = all(automatic_checks.values()) and blind_review == "pass"
    return {
        "passed": passed,
        "automatic_checks": automatic_checks,
        "blind_review": blind_review,
        "repair_counts": repair_counts,
        "task_repair_counts": task_repair_counts,
        "task_checks": task_checks,
        "task_passed": task_passed,
        "envelope": {
            "top_interest_overlap_floor": round(overlap_floor, 6),
            "style_drift_ceiling": round(style_ceiling, 6),
            "awareness_source_event_id_overlap_floor": round(awareness_overlap_floor, 6),
            "awareness_note_count_delta_ceiling": awareness_count_ceiling,
            "insight_hypothesis_count_delta_ceiling": insight_count_ceiling,
            "insight_mean_evidence_count_drift_ceiling": round(
                insight_evidence_drift_ceiling,
                6,
            ),
            "insight_mean_confidence_drift_ceiling": round(
                insight_confidence_drift_ceiling,
                6,
            ),
        },
    }


def _task_scoped_rollout_gate(
    *,
    calls: Sequence[Mapping[str, object]],
    tokens: Mapping[str, object],
    quality: Mapping[str, object],
    expected_route: PinnedRoute,
) -> dict[str, object]:
    """Emit fail-closed, machine-readable compact rollout decisions."""

    task_quality_raw = quality.get("task_passed")
    task_quality = task_quality_raw if isinstance(task_quality_raw, Mapping) else {}
    token_results: dict[str, tuple[bool, str]] = {
        "preference": (
            _float_metric(
                tokens,
                "preference_total_tokens_per_event_savings",
                float("-inf"),
            )
            >= _PREFERENCE_TOTAL_TOKEN_SAVINGS_MIN,
            "threshold-declared",
        ),
        "awareness_confusions": (
            _float_metric(tokens, "awareness_prompt_token_savings", float("-inf"))
            >= _AWARENESS_PROMPT_TOKEN_SAVINGS_MIN,
            "threshold-declared",
        ),
        # Insight savings remain diagnostic in the frozen Phase 2 contract.
        # A missing threshold must never be interpreted as rollout approval.
        "insight": (False, "threshold-not-declared"),
    }
    config_fields = {
        "preference": "soul.preference_prompt_view",
        "awareness_confusions": "soul.awareness_prompt_view",
        "insight": "soul.insight_prompt_view",
    }
    decisions: dict[str, object] = {}
    for task in _TASKS:
        task_calls = [call for call in calls if call.get("task") == task]
        task_route = route_audit(
            task_calls,
            expected=expected_route,
            expected_call_count=len(_call_plan()),
        )
        route_passed = task_route.get("passed") is True
        token_passed, token_status = token_results[task]
        quality_passed = task_quality.get(task) is True
        blocking_reasons: list[str] = []
        if not route_passed:
            blocking_reasons.append("route-or-usage-gate-failed")
        if not token_passed:
            blocking_reasons.append(
                "token-threshold-not-declared"
                if token_status == "threshold-not-declared"
                else "token-gate-failed"
            )
        if not quality_passed:
            blocking_reasons.append("quality-gate-failed")
        enabled = not blocking_reasons
        decisions[task] = {
            "config_field": config_fields[task],
            "compact_v1_enabled": enabled,
            "selected_view": COMPACT_VIEW if enabled else LEGACY_VIEW,
            "route_gate_passed": route_passed,
            "token_gate_passed": token_passed,
            "token_gate_status": token_status,
            "quality_gate_passed": quality_passed,
            "blocking_reasons": blocking_reasons,
        }
    return decisions


def evaluate_real_gates(
    *,
    calls: Sequence[Mapping[str, object]],
    quality: Mapping[str, object],
    expected_route: PinnedRoute,
    blind_review: str,
) -> dict[str, object]:
    route = route_audit(calls, expected=expected_route)
    tokens = token_gate(calls)
    cognition_quality = quality_gate(calls, quality, blind_review=blind_review)
    task_rollout = _task_scoped_rollout_gate(
        calls=calls,
        tokens=tokens,
        quality=cognition_quality,
        expected_route=expected_route,
    )
    passed = bool(route["passed"] and tokens["passed"] and cognition_quality["passed"])
    reasons: list[str] = []
    if not route["passed"]:
        reasons.append("route-or-usage-gate-failed")
    if not tokens["passed"]:
        reasons.append("token-gate-failed")
    if not cognition_quality["passed"]:
        reasons.append("cognition-quality-gate-failed")
    return {
        "passed": passed,
        "blocking_reasons": reasons,
        "route": route,
        "tokens": tokens,
        "quality": cognition_quality,
        "task_rollout": task_rollout,
    }


def _git_metadata() -> dict[str, object]:
    def command(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    return {
        "commit": command("rev-parse", "HEAD"),
        "dirty": bool(command("status", "--porcelain")),
    }


def _cohort_summary(cohort: FrozenCognitionCohort) -> dict[str, object]:
    return {
        "snapshot_digest": cohort.snapshot_digest,
        "preference": {
            "event_count": len(cohort.preference_events),
            "event_ids_digest": cohort.preference_event_ids_digest,
            "input_digest": cohort.preference_input_digest,
        },
        "awareness_confusions": {
            "event_count": len(cohort.awareness_events),
            "event_ids_digest": cohort.awareness_event_ids_digest,
            "input_digest": cohort.awareness_input_digest,
        },
        "insight": {
            "awareness_note_count": len(cohort.awareness_notes),
            "existing_hypothesis_count": len(cohort.active_insights),
            "input_digest": cohort.insight_input_digest,
        },
        "awareness_context_count": len(cohort.awareness_notes),
        "active_insight_context_count": len(cohort.active_insights),
    }


def build_render_only_artifact(
    *,
    cohort: FrozenCognitionCohort,
    rendered: Mapping[tuple[str, str], RenderedPrompt],
    git_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "render-only",
        "git": dict(git_metadata or _git_metadata()),
        "cohort": _cohort_summary(cohort),
        "render": _render_summary(rendered),
        "planned_runs": [
            {"task": task, "pair_kind": pair, "logical_run": run, "input_view": view}
            for task in _TASKS
            for pair, run, view in _call_plan()
        ],
        "gate": {
            "passed": True,
            "status": "render-only",
            "blocking_reasons": [],
            "real_provider_required_for_token_and_quality_gates": True,
        },
    }


def build_real_artifact(
    *,
    cohort: FrozenCognitionCohort,
    rendered: Mapping[tuple[str, str], RenderedPrompt],
    bundle: ExecutionBundle,
    expected_route: PinnedRoute,
    blind_review: str,
    git_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    quality = structural_quality_summary(bundle, cohort=cohort)
    gate = evaluate_real_gates(
        calls=bundle.calls,
        quality=quality,
        expected_route=expected_route,
        blind_review=blind_review,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "real-provider",
        "git": dict(git_metadata or _git_metadata()),
        "cohort": _cohort_summary(cohort),
        "render": _render_summary(rendered),
        "expected_route": {
            "provider": _sanitize_route_label(expected_route.provider_type),
            "instance_id": _sanitize_route_label(expected_route.instance_id),
            "model": _sanitize_route_label(expected_route.model),
            "fallback_disabled": True,
        },
        "calls": [dict(call) for call in bundle.calls],
        "structural_quality": quality,
        "gate": gate,
    }


def _walk_keys(value: object) -> Sequence[str]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            keys.append(str(key).strip().lower())
            keys.extend(_walk_keys(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            keys.extend(_walk_keys(nested))
    return keys


def _private_fragments(value: object) -> set[str]:
    """Extract sufficiently identifying raw strings for a pre-write leak audit."""

    fragments: set[str] = set()
    if isinstance(value, Mapping):
        for nested in value.values():
            fragments.update(_private_fragments(nested))
    elif isinstance(value, list | tuple):
        for nested in value:
            fragments.update(_private_fragments(nested))
    elif isinstance(value, str):
        stripped = value.strip()
        if len(stripped) >= 12:
            fragments.add(stripped)
    return fragments


def assert_privacy_safe_artifact(
    artifact: Mapping[str, object],
    *,
    private_values: Sequence[object] = (),
) -> None:
    """Reject raw prompt/data/provider leaks before any artifact write."""

    forbidden = sorted(set(_walk_keys(artifact)) & _FORBIDDEN_ARTIFACT_KEYS)
    if forbidden:
        raise ReplayContractError(f"artifact contains forbidden keys: {', '.join(forbidden)}")
    serialized = _canonical_json(artifact)
    if _URL_RE.search(serialized):
        raise ReplayContractError("artifact contains a raw URL")
    if _SECRET_RE.search(serialized):
        raise ReplayContractError("artifact contains credential-shaped text")
    fragments: set[str] = set()
    for private_value in private_values:
        fragments.update(_private_fragments(private_value))
    leaked = next((fragment for fragment in fragments if fragment in serialized), None)
    if leaked is not None:
        raise ReplayContractError(
            f"artifact contains a raw private fragment (length={len(leaked)})"
        )


def write_artifact(
    output_path: Path,
    artifact: Mapping[str, object],
    *,
    private_values: Sequence[object] = (),
) -> None:
    assert_privacy_safe_artifact(artifact, private_values=private_values)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def exit_code_for_artifact(artifact: Mapping[str, object]) -> int:
    gate = artifact.get("gate")
    return 0 if isinstance(gate, Mapping) and gate.get("passed") is True else 1


def _find_instance(config: object, instance_id: str) -> tuple[str, object]:
    llm = getattr(config, "llm", None)
    instances = getattr(llm, "instances", None)
    if not isinstance(instances, Mapping):
        raise ReplayContractError("real-provider replay requires v2 [llm.instances] configuration")
    normalized = instance_id.strip().lower()
    for raw_name, instance in instances.items():
        if str(raw_name).strip().lower() == normalized:
            return normalized, instance
    raise ReplayContractError(f"pinned instance {instance_id!r} is not configured")


def resolve_pinned_sensetime_route(
    config: object,
    *,
    instance_id: str,
    expected_model: str = "",
    confirm_sensetime_route: bool = False,
) -> PinnedRoute:
    """Validate a concrete SenseTime OpenAI-compatible instance and model."""

    normalized, instance = _find_instance(config, instance_id)
    if not bool(getattr(instance, "enabled", True)):
        raise ReplayContractError(f"pinned instance {instance_id!r} is disabled")
    provider_type = str(getattr(instance, "provider_type", "") or "").strip().lower()
    if provider_type != "openai_compatible":
        raise ReplayContractError(
            "SenseTime evidence route must use provider_type='openai_compatible'"
        )
    model = str(expected_model or getattr(instance, "model", "") or "").strip()
    if not model:
        raise ReplayContractError("pinned SenseTime instance has no configured model")
    evidence = " ".join(
        (
            normalized,
            model,
            str(getattr(instance, "base_url", "") or ""),
            str(getattr(instance, "name", "") or ""),
        )
    ).lower()
    if not confirm_sensetime_route and not any(marker in evidence for marker in _SENSETIME_MARKERS):
        raise ReplayContractError(
            "the pinned OpenAI-compatible route is not recognizably SenseTime; "
            "use --confirm-sensetime-route only after verifying it out of band"
        )
    return PinnedRoute(
        instance_id=normalized,
        provider_type=provider_type,
        model=model,
    )


def build_pinned_llm_service(
    config: Config,
    *,
    data_root: Path,
    route: PinnedRoute,
) -> CompletionClient:
    """Construct a soul service with one exact route and no fallback."""

    from openbiliclaw.config import llm_concurrency_from_config
    from openbiliclaw.llm.registry import build_llm_registry
    from openbiliclaw.llm.service import LLMService, ModuleOverride
    from openbiliclaw.memory.manager import MemoryManager

    registry = build_llm_registry(config)
    if not registry.is_chat_capable(route.instance_id):
        raise ReplayContractError(
            f"pinned instance {route.instance_id!r} is not registered/chat-capable"
        )
    service = LLMService(
        registry=registry,
        memory=MemoryManager(data_root),
        module_overrides={
            "soul": ModuleOverride(chain=(route.instance_id,), custom_chain=True),
        },
        concurrency=llm_concurrency_from_config(config),
    )
    return cast("CompletionClient", service)


def _database_path(config: object) -> Path:
    data_path = getattr(config, "data_path", PROJECT_ROOT / "data")
    return Path(data_path) / "openbiliclaw.db"


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Privacy-safe Phase 2 cognition prompt A/A and A/B replay"
    )
    parser.add_argument("--mode", choices=("render-only", "real-provider"), default="render-only")
    parser.add_argument("--config", default=None, help="Explicit config.toml path")
    parser.add_argument("--db", default=None, help="Explicit read-only SQLite database path")
    parser.add_argument(
        "--data-root",
        default=None,
        help="Explicit memory/profile data root (defaults to explicit DB parent or config data_dir)",
    )
    parser.add_argument(
        "--preference-events",
        type=_positive_int,
        default=_DEFAULT_PREFERENCE_EVENTS,
    )
    parser.add_argument(
        "--awareness-events",
        type=_positive_int,
        default=_DEFAULT_AWARENESS_EVENTS,
    )
    parser.add_argument(
        "--max-event-id",
        type=_positive_int,
        default=None,
        help="Optional private freeze anchor; the raw ID is not written to the artifact",
    )
    parser.add_argument(
        "--instance",
        default="",
        help="Exact SenseTime 日日新 instance ID; required for real-provider mode",
    )
    parser.add_argument(
        "--expected-model",
        default="",
        help="Optional exact model assertion (defaults to the instance model)",
    )
    parser.add_argument(
        "--confirm-sensetime-route",
        action="store_true",
        help="Attest that an unrecognizable custom gateway is backed by SenseTime 日日新",
    )
    parser.add_argument(
        "--blind-review",
        choices=("not-run", "pass", "fail"),
        default="not-run",
        help="Manual blind rubric result; real evidence cannot pass while not-run",
    )
    parser.add_argument("--output", required=True, help="Privacy-safe JSON artifact path")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from openbiliclaw.config import load_config

    config = load_config(args.config) if args.config else load_config()
    db_path = Path(args.db) if args.db else _database_path(config)
    data_root = (
        Path(args.data_root)
        if args.data_root
        else db_path.parent
        if args.db
        else Path(getattr(config, "data_path", PROJECT_ROOT / "data"))
    )
    if not db_path.exists():
        raise ReplayContractError(f"database not found: {db_path}")
    cohort = freeze_cognition_cohort(
        db_path=db_path,
        data_root=data_root,
        preference_event_count=int(args.preference_events),
        awareness_event_count=int(args.awareness_events),
        max_event_id=args.max_event_id,
    )
    rendered = render_cognition_prompts(cohort)
    output_path = Path(args.output)
    private_inputs: list[object] = [
        cohort.preference_events,
        cohort.awareness_events,
        cohort.existing_preference,
        cohort.soul_profile,
        cohort.awareness_notes,
        cohort.active_insights,
    ]
    if args.mode == "render-only":
        artifact = build_render_only_artifact(cohort=cohort, rendered=rendered)
        write_artifact(output_path, artifact, private_values=private_inputs)
        return exit_code_for_artifact(artifact)

    if not str(args.instance).strip():
        raise ReplayContractError("--instance is required for real-provider mode")
    route = resolve_pinned_sensetime_route(
        config,
        instance_id=str(args.instance),
        expected_model=str(args.expected_model),
        confirm_sensetime_route=bool(args.confirm_sensetime_route),
    )
    client = build_pinned_llm_service(config, data_root=data_root, route=route)
    bundle = await execute_real_replay(rendered=rendered, cohort=cohort, client=client)
    artifact = build_real_artifact(
        cohort=cohort,
        rendered=rendered,
        bundle=bundle,
        expected_route=route,
        blind_review=str(args.blind_review),
    )
    write_artifact(
        output_path,
        artifact,
        private_values=[*private_inputs, bundle.response_bodies],
    )
    return exit_code_for_artifact(artifact)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        exit_code = asyncio.run(run(parse_args()))
    except Exception as exc:
        logger.error("cognition token-diet replay failed: %s", exc)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
