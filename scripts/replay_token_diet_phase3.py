"""Privacy-safe Phase 3 token-diet render and SenseTime A/A+B replay.

This harness evaluates the two LLM-facing Phase 3 changes against frozen,
read-only production inputs:

* preference automatic fallback: old proportional chunk estimate vs the
  request-shape-aware largest fitting independent prefix;
* insight generation: the full durable hypothesis ledger vs the bounded
  recent + judged prompt view.

The output artifact contains only hashes, counts, structural quality metrics,
sanitized route labels, and provider-reported usage. Raw events, profile text,
prompts, provider bodies, URLs, credentials, and cookies are never written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from scripts.replay_cognition_token_diet import (  # noqa: E402
    ParsedResult,
    PinnedRoute,
    ReplayContractError,
    _digest,
    _insight_pair_quality,
    _preference_pair_quality,
    _preference_structure_metrics,
    _sanitize_route_label,
    normalize_provider_usage,
    parse_structured_result,
    resolve_pinned_sensetime_route,
    route_audit,
    write_artifact,
)

if TYPE_CHECKING:
    from openbiliclaw.config import Config
    from openbiliclaw.llm.base import LLMResponse
    from openbiliclaw.soul.profile import AwarenessNote, InsightHypothesis

logger = logging.getLogger("eval.token_diet_phase3")

SCHEMA_VERSION = 1
CONTRACT_VERSION = "llm-token-diet-phase3-v1"
PREFERENCE_BUDGET_CHARS = 24_000
PREFERENCE_EVENTS_DEFAULT = 3
INSIGHT_NOTES_DEFAULT = 20
PREFERENCE_PROMPT_SAVINGS_MIN = 0.35
INSIGHT_PROMPT_SAVINGS_MIN = 0.35
PREFERENCE_TOP_INTEREST_OVERLAP_FLOOR = 0.826
KEYWORD_GRACE_HOURS = 24
KEYWORD_SEED_COUNT = 60
KEYWORD_CACHE_HIGH = 30
KEYWORD_CACHE_LOW = 10
KEYWORD_FETCH_COUNT = 3
KEYWORD_EVAL_LIMIT = 12

_KEYWORD_PROVENANCE_COLUMNS = (
    "profile_kw_digest",
    "aspect_id",
    "inspiration_backend",
    "inspiration_id",
    "inspiration_terms",
    "expansion_id",
    "expansion_label",
    "angle_id",
    "angle_label",
    "query_kind",
    "source_domain",
    "source_interest",
    "generation_reason",
    "normalized_keyword",
    "grounding_source",
    "created_at",
)

T = TypeVar("T")


class CompletionClient(Protocol):
    async def complete_structured_task(self, **kwargs: object) -> LLMResponse: ...


class _NoProviderService:
    async def complete_structured_task(self, **_: object) -> LLMResponse:
        raise AssertionError("render-only helper must not call a provider")


@dataclass(frozen=True)
class Phase3Cohort:
    preference_events: tuple[dict[str, object], ...]
    existing_preference: dict[str, object]
    soul_profile: dict[str, object]
    preference_awareness_tail: tuple[dict[str, object], ...]
    preference_insight_tail: tuple[dict[str, object], ...]
    insight_notes: tuple[AwarenessNote, ...]
    all_insights: tuple[InsightHypothesis, ...]
    snapshot_digest: str
    preference_input_digest: str
    insight_input_digest: str
    recent_expired_unused_regular: dict[str, int]


@dataclass(frozen=True)
class Phase3Plan:
    preference_control_chunk_size: int
    preference_treatment_chunk_size: int
    preference_treatment_chunk_sizes: tuple[int, ...]
    preference_control_chunks: tuple[tuple[dict[str, str], ...], ...]
    preference_treatment_chunks: tuple[tuple[dict[str, str], ...], ...]
    insight_control_messages: tuple[dict[str, str], ...]
    insight_treatment_messages: tuple[dict[str, str], ...]
    selected_insights: tuple[InsightHypothesis, ...]
    summary: dict[str, object]


@dataclass(frozen=True)
class _StaticSoulEngine:
    profile: object

    async def get_profile(self) -> object:
        return self.profile


@dataclass(frozen=True)
class _BilibiliOnlyDeficit:
    deficit: int = KEYWORD_CACHE_HIGH

    def keyword_planner_real_deficit(self, platform: str) -> int:
        return self.deficit if platform == "bilibili" else 0

    def keyword_planner_bilibili_catalyst(self) -> bool:
        return False

    def keyword_planner_explore_due_soon(self) -> bool:
        return False

    def keyword_planner_explore_covered_topic_groups(self) -> list[str]:
        return []

    def keyword_planner_mark_explore_planned(self) -> None:
        return None

    def _source_target_counts(self) -> dict[str, int]:
        return {"bilibili": self.deficit}


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _load_keyword_seed_rows(
    *,
    db_path: Path,
    current_digest: str,
    limit: int = KEYWORD_SEED_COUNT,
) -> tuple[dict[str, object], ...]:
    """Freeze recent unused Bilibili rows without writing to production."""
    connection = _read_only_connection(db_path)
    try:
        rows = connection.execute(
            """
            SELECT *
            FROM discovery_keywords
            WHERE platform = 'bilibili'
              AND keyword_kind = 'regular'
              AND status = 'expired'
              AND used_at IS NULL
              AND created_at >= datetime('now', '-24 hours')
              AND profile_kw_digest != ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (current_digest, max(limit * 10, limit)),
        ).fetchall()
    finally:
        connection.close()

    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        row = dict(raw)
        key = (
            str(row.get("keyword") or "").strip(),
            str(row.get("profile_kw_digest") or "").strip(),
            str(row.get("keyword_kind") or "regular").strip(),
        )
        if not key[0] or key in seen:
            continue
        seen.add(key)
        selected.append(row)
        if len(selected) >= limit:
            break
    if len(selected) < KEYWORD_CACHE_LOW:
        raise ReplayContractError(
            "keyword E2E requires at least "
            f"{KEYWORD_CACHE_LOW} recent stale unused Bilibili rows; found {len(selected)}"
        )
    return tuple(selected)


def _create_keyword_test_database(
    path: Path,
    seed_rows: Sequence[Mapping[str, object]],
) -> Any:
    """Create a disposable DB containing only the frozen keyword cohort."""
    from openbiliclaw.storage.database import Database

    database = Database(path)
    database.initialize()
    columns = {
        str(row["name"])
        for row in database.conn.execute("PRAGMA table_info(discovery_keywords)").fetchall()
    }
    for source_row in seed_rows:
        row = {key: value for key, value in source_row.items() if key in columns}
        row.update(
            {
                "status": "pending",
                "claimed_at": None,
                "executing_at": None,
                "used_at": None,
                "yield_count": 0,
            }
        )
        insert_columns = sorted(row)
        placeholders = ", ".join("?" for _ in insert_columns)
        database.conn.execute(
            f"INSERT INTO discovery_keywords ({', '.join(insert_columns)}) VALUES ({placeholders})",
            tuple(row[column] for column in insert_columns),
        )
    database.conn.commit()
    return database


def _keyword_provenance(row: Mapping[str, object]) -> dict[str, object]:
    return {column: row.get(column) for column in _KEYWORD_PROVENANCE_COLUMNS}


def _keyword_test_config(config: Config, *, grace_hours: int) -> Config:
    discovery = replace(
        config.discovery,
        unified_keyword_planner_enabled=True,
        kw_cache_high=KEYWORD_CACHE_HIGH,
        kw_cache_low=KEYWORD_CACHE_LOW,
        gen_batch=KEYWORD_CACHE_HIGH,
        fetch_batch=KEYWORD_FETCH_COUNT,
        keyword_digest_grace_hours=grace_hours,
        inspiration_search_enabled=False,
        inspiration_replace_merged_keywords=False,
    )
    return replace(config, discovery=discovery)


def _decode_event(row: Mapping[str, object]) -> dict[str, object]:
    event = dict(row)
    for key in ("context", "metadata"):
        raw = event.get(key)
        if not isinstance(raw, str):
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {}
        event[key] = decoded if isinstance(decoded, dict) else {}
    return event


def _prompt_chars(messages: Sequence[Mapping[str, str]]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages)


def _chunks(values: Sequence[T], size: int) -> list[list[T]]:
    width = max(1, int(size))
    return [list(values[index : index + width]) for index in range(0, len(values), width)]


def freeze_phase3_cohort(
    *,
    db_path: Path,
    data_root: Path,
    preference_event_count: int = PREFERENCE_EVENTS_DEFAULT,
    insight_note_count: int = INSIGHT_NOTES_DEFAULT,
) -> Phase3Cohort:
    """Freeze private inputs from read-only production state."""
    from openbiliclaw.memory.manager import MemoryManager
    from openbiliclaw.soul.preference_analyzer import PreferenceAnalyzer
    from openbiliclaw.soul.profile import (
        awareness_note_from_dict,
        insight_hypothesis_from_dict,
    )

    if preference_event_count <= 0 or insight_note_count <= 0:
        raise ReplayContractError("cohort sizes must be positive")
    connection = _read_only_connection(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM events ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(preference_event_count * 20, preference_event_count),),
        ).fetchall()
        recent_expired_rows = connection.execute(
            """
            SELECT platform, COUNT(*) AS n
            FROM discovery_keywords
            WHERE keyword_kind = 'regular'
              AND status = 'expired'
              AND used_at IS NULL
              AND created_at >= datetime('now', '-24 hours')
            GROUP BY platform
            ORDER BY platform
            """
        ).fetchall()
    finally:
        connection.close()

    analyzer = PreferenceAnalyzer(cast("Any", _NoProviderService()))
    decoded = [_decode_event(dict(row)) for row in rows]
    eligible = analyzer._maybe_filter_events(decoded)  # noqa: SLF001
    if len(eligible) < preference_event_count:
        raise ReplayContractError(
            f"requested {preference_event_count} eligible events but found {len(eligible)}"
        )
    # The query is newest-first; production buffers preserve chronology.
    preference_events = tuple(reversed(eligible[:preference_event_count]))

    memory = MemoryManager(data_root)
    existing_preference = deepcopy(dict(memory.get_layer("preference").data))
    soul_profile = deepcopy(dict(memory.get_layer("soul").data))
    if not soul_profile:
        raise ReplayContractError(f"no soul profile found under {data_root}")
    raw_awareness = memory.get_layer("awareness").data.get("notes", [])
    raw_insights = memory.get_layer("insight").data.get("hypotheses", [])
    awareness_items = [item for item in raw_awareness if isinstance(item, dict)]
    insight_items = [item for item in raw_insights if isinstance(item, dict)]
    insight_notes = tuple(
        awareness_note_from_dict(dict(item)) for item in awareness_items[-insight_note_count:]
    )
    all_insights = tuple(insight_hypothesis_from_dict(dict(item)) for item in insight_items)
    if not insight_notes or not all_insights:
        raise ReplayContractError("insight replay requires non-empty awareness and insight history")

    pref_awareness = tuple(
        dict(item)
        for item in list(soul_profile.get("recent_awareness") or [])[-5:]
        if isinstance(item, dict)
    )
    pref_insights = tuple(
        dict(item)
        for item in list(soul_profile.get("active_insights") or [])[-5:]
        if isinstance(item, dict)
    )
    preference_payload = {
        "event_rows": preference_events,
        "existing_preference": existing_preference,
        "awareness_tail": pref_awareness,
        "insight_tail": pref_insights,
    }
    insight_payload = {
        "awareness_rows": [item.__dict__ for item in insight_notes],
        "existing_preference": existing_preference,
        "soul_profile": soul_profile,
        "hypothesis_rows": [item.__dict__ for item in all_insights],
    }
    return Phase3Cohort(
        preference_events=preference_events,
        existing_preference=existing_preference,
        soul_profile=soul_profile,
        preference_awareness_tail=pref_awareness,
        preference_insight_tail=pref_insights,
        insight_notes=insight_notes,
        all_insights=all_insights,
        snapshot_digest=_digest(
            {"preference_input": preference_payload, "insight_input": insight_payload}
        ),
        preference_input_digest=_digest(preference_payload),
        insight_input_digest=_digest(insight_payload),
        recent_expired_unused_regular={
            str(row["platform"]): int(row["n"]) for row in recent_expired_rows
        },
    )


def _preference_chunk_messages(
    cohort: Phase3Cohort,
    *,
    chunk_size: int,
) -> tuple[tuple[dict[str, str], ...], ...]:
    from openbiliclaw.llm.prompts import build_preference_analysis_prompt

    return tuple(
        tuple(
            build_preference_analysis_prompt(
                events=list(chunk),
                existing_preference={},
                awareness_notes=list(cohort.preference_awareness_tail) or None,
                active_insights=list(cohort.preference_insight_tail) or None,
                input_view="legacy",
            )
        )
        for chunk in _chunks(cohort.preference_events, chunk_size)
    )


def _preference_planned_chunk_messages(
    cohort: Phase3Cohort,
    *,
    chunks: Sequence[Sequence[dict[str, object]]],
) -> tuple[tuple[dict[str, str], ...], ...]:
    from openbiliclaw.llm.prompts import build_preference_analysis_prompt

    return tuple(
        tuple(
            build_preference_analysis_prompt(
                events=list(chunk),
                existing_preference={},
                awareness_notes=list(cohort.preference_awareness_tail) or None,
                active_insights=list(cohort.preference_insight_tail) or None,
                input_view="legacy",
            )
        )
        for chunk in chunks
    )


def build_phase3_plan(cohort: Phase3Cohort) -> Phase3Plan:
    """Render both arms using the exact production builders."""
    from openbiliclaw.llm.prompts import (
        build_insight_prompt,
        build_preference_analysis_prompt,
    )
    from openbiliclaw.soul.cognition_cycle import _select_fixed_insight_prompt_context
    from openbiliclaw.soul.insight_analyzer import InsightAnalyzer
    from openbiliclaw.soul.preference_analyzer import PreferenceAnalyzer

    preference_analyzer = PreferenceAnalyzer(
        cast("Any", _NoProviderService()),
        max_prompt_chars=PREFERENCE_BUDGET_CHARS,
    )
    whole_messages = build_preference_analysis_prompt(
        events=list(cohort.preference_events),
        existing_preference=cohort.existing_preference,
        awareness_notes=list(cohort.preference_awareness_tail) or None,
        active_insights=list(cohort.preference_insight_tail) or None,
        input_view="legacy",
    )
    whole_chars = _prompt_chars(whole_messages)
    event_count = len(cohort.preference_events)
    if whole_chars <= PREFERENCE_BUDGET_CHARS:
        control_chunk_size = event_count
    else:
        # Frozen reproduction of the removed proportional estimator.
        control_chunk_size = max(
            1,
            min(event_count, event_count * PREFERENCE_BUDGET_CHARS // max(whole_chars, 1)),
        )
    treatment_event_chunks = preference_analyzer._plan_fitting_independent_chunks(  # noqa: SLF001
        events=list(cohort.preference_events),
        awareness_notes=list(cohort.preference_awareness_tail) or None,
        active_insights=list(cohort.preference_insight_tail) or None,
    )
    treatment_chunk_sizes = tuple(len(chunk) for chunk in treatment_event_chunks)
    treatment_chunk_size = max(treatment_chunk_sizes, default=1)
    control_chunks = _preference_chunk_messages(cohort, chunk_size=control_chunk_size)
    treatment_chunks = _preference_planned_chunk_messages(
        cohort,
        chunks=treatment_event_chunks,
    )
    control_chars = sum(_prompt_chars(messages) for messages in control_chunks)
    treatment_chars = sum(_prompt_chars(messages) for messages in treatment_chunks)

    # Phase 3 is a historical reproduction harness. Keep its treatment arm on
    # the exact fixed recent/judged policy even after production advances to
    # weighted selection; the weighted A/A+B gate lives in the Phase 4 replay.
    selected_insights = tuple(_select_fixed_insight_prompt_context(list(cohort.all_insights)))

    def insight_messages(insights: Sequence[InsightHypothesis]) -> tuple[dict[str, str], ...]:
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

    insight_control = insight_messages(cohort.all_insights)
    insight_treatment = insight_messages(selected_insights)
    insight_control_chars = _prompt_chars(insight_control)
    insight_treatment_chars = _prompt_chars(insight_treatment)
    summary: dict[str, object] = {
        "preference": {
            "input_digest": cohort.preference_input_digest,
            "event_count": event_count,
            "whole_request_chars": whole_chars,
            "budget_chars": PREFERENCE_BUDGET_CHARS,
            "control_chunk_size": control_chunk_size,
            "treatment_chunk_size": treatment_chunk_size,
            "treatment_chunk_sizes": list(treatment_chunk_sizes),
            "control_call_count": len(control_chunks),
            "treatment_call_count": len(treatment_chunks),
            "control_independent_chars": control_chars,
            "treatment_independent_chars": treatment_chars,
            "character_savings": round(
                (control_chars - treatment_chars) / control_chars if control_chars else 0.0,
                6,
            ),
        },
        "insight": {
            "input_digest": cohort.insight_input_digest,
            "durable_hypothesis_count": len(cohort.all_insights),
            "selected_hypothesis_count": len(selected_insights),
            "judged_or_validated_durable_count": sum(
                bool(item.validated or item.user_verdict) for item in cohort.all_insights
            ),
            "control_chars": insight_control_chars,
            "treatment_chars": insight_treatment_chars,
            "character_savings": round(
                (insight_control_chars - insight_treatment_chars) / insight_control_chars
                if insight_control_chars
                else 0.0,
                6,
            ),
            "system_instruction_invariant": (
                insight_control[0]["content"] == insight_treatment[0]["content"]
            ),
        },
        "keyword_inventory": {
            "recent_expired_unused_regular_by_platform": dict(cohort.recent_expired_unused_regular),
            "recent_expired_unused_regular_total": sum(
                cohort.recent_expired_unused_regular.values()
            ),
        },
    }
    return Phase3Plan(
        preference_control_chunk_size=control_chunk_size,
        preference_treatment_chunk_size=treatment_chunk_size,
        preference_treatment_chunk_sizes=treatment_chunk_sizes,
        preference_control_chunks=control_chunks,
        preference_treatment_chunks=treatment_chunks,
        insight_control_messages=insight_control,
        insight_treatment_messages=insight_treatment,
        selected_insights=selected_insights,
        summary=summary,
    )


class RecordingClient:
    """Force deterministic replay temperature and keep a sanitized usage ledger."""

    def __init__(
        self,
        delegate: CompletionClient,
        *,
        max_concurrency: int | None = None,
        request_interval_seconds: float = 0.0,
    ) -> None:
        self._delegate = delegate
        self._task = ""
        self._logical_run = ""
        self._max_concurrency = (
            max(1, int(max_concurrency)) if max_concurrency is not None else None
        )
        self._request_interval_seconds = max(0.0, float(request_interval_seconds))
        self._request_lock = asyncio.Lock() if self._max_concurrency == 1 else None
        self._last_request_started = 0.0
        self.calls: list[dict[str, object]] = []
        self.response_bodies: list[str] = []

    @property
    def concurrency(self) -> int:
        try:
            delegate_concurrency = max(1, int(getattr(self._delegate, "concurrency", 1)))
        except (TypeError, ValueError):
            delegate_concurrency = 1
        if self._max_concurrency is None:
            return delegate_concurrency
        return min(delegate_concurrency, self._max_concurrency)

    def set_context(self, task: str, logical_run: str) -> None:
        self._task = task
        self._logical_run = logical_run

    async def complete_structured_task(self, **kwargs: object) -> LLMResponse:
        if self._request_lock is not None:
            async with self._request_lock:
                return await self._complete_structured_task_once(**kwargs)
        return await self._complete_structured_task_once(**kwargs)

    async def _complete_structured_task_once(self, **kwargs: object) -> LLMResponse:
        elapsed = time.monotonic() - self._last_request_started
        remaining = self._request_interval_seconds - elapsed
        if self._last_request_started > 0.0 and remaining > 0.0:
            await asyncio.sleep(remaining)
        self._last_request_started = time.monotonic()
        request = dict(kwargs)
        request["temperature"] = 0.0
        record: dict[str, object] = {
            "task": self._task,
            "logical_run": self._logical_run,
            "caller": _sanitize_route_label(request.get("caller", "")),
            "status": "provider-error",
            "route": {"provider": "", "instance_id": "", "model": ""},
            "usage": None,
            "strict_json": False,
        }
        try:
            response = await self._delegate.complete_structured_task(**request)
        except Exception as exc:
            record["error_kind"] = type(exc).__name__
            self.calls.append(record)
            raise
        content = str(getattr(response, "content", "") or "")
        self.response_bodies.append(content)
        try:
            json.loads(content)
            strict_json = True
        except json.JSONDecodeError:
            strict_json = False
        record.update(
            {
                "status": "ok",
                "route": {
                    "provider": _sanitize_route_label(getattr(response, "provider", "")),
                    "instance_id": _sanitize_route_label(getattr(response, "instance_id", "")),
                    "model": _sanitize_route_label(getattr(response, "model", "")),
                },
                "usage": normalize_provider_usage(response),
                "strict_json": strict_json,
            }
        )
        self.calls.append(record)
        return response


def build_pinned_phase3_service(
    config: Config,
    *,
    data_root: Path,
    route: PinnedRoute,
) -> CompletionClient:
    """Build one exact SenseTime chain for both soul and discovery callers."""
    from openbiliclaw.config import llm_concurrency_from_config
    from openbiliclaw.llm.registry import build_llm_registry
    from openbiliclaw.llm.service import LLMService, ModuleOverride
    from openbiliclaw.memory.manager import MemoryManager

    registry = build_llm_registry(config)
    if not registry.is_chat_capable(route.instance_id):
        raise ReplayContractError(f"pinned instance {route.instance_id!r} is not chat-capable")
    pinned = ModuleOverride(chain=(route.instance_id,), custom_chain=True)
    return cast(
        "CompletionClient",
        LLMService(
            registry=registry,
            memory=MemoryManager(data_root),
            module_overrides={"soul": pinned, "discovery": pinned},
            concurrency=llm_concurrency_from_config(config),
        ),
    )


async def _run_preference_arm(
    *,
    recorder: RecordingClient,
    cohort: Phase3Cohort,
    chunk_size: int,
    logical_run: str,
    planned_chunk_sizes: Sequence[int] | None = None,
) -> dict[str, object]:
    from openbiliclaw.soul.preference_analyzer import PreferenceAnalyzer

    recorder.set_context("preference", logical_run)
    analyzer = PreferenceAnalyzer(recorder, max_prompt_chars=PREFERENCE_BUDGET_CHARS)
    planned_chunks: list[list[dict[str, object]]] | None = None
    if planned_chunk_sizes is not None:
        planned_chunks = []
        offset = 0
        for raw_size in planned_chunk_sizes:
            size = max(1, int(raw_size))
            planned_chunks.append(list(cohort.preference_events[offset : offset + size]))
            offset += size
        if offset != len(cohort.preference_events) or any(not chunk for chunk in planned_chunks):
            raise ReplayContractError("preference planned chunk sizes do not cover the cohort")
    return await analyzer._analyze_events_chunked(  # noqa: SLF001
        events=list(cohort.preference_events),
        existing_preference=deepcopy(cohort.existing_preference),
        chunk_size=chunk_size,
        planned_chunks=planned_chunks,
        awareness_notes=list(cohort.preference_awareness_tail) or None,
        active_insights=list(cohort.preference_insight_tail) or None,
    )


async def _run_insight_arm(
    *,
    recorder: RecordingClient,
    messages: Sequence[Mapping[str, str]],
    logical_run: str,
) -> ParsedResult:
    from openbiliclaw.soul.cognition_cycle import _COGNITION_MAX_TOKENS

    recorder.set_context("insight", logical_run)
    response = await recorder.complete_structured_task(
        system_instruction=str(messages[0]["content"]),
        user_input=str(messages[1]["content"]),
        max_tokens=_COGNITION_MAX_TOKENS,
        caller="soul.insight.phase3_replay",
        inject_core_memory=False,
    )
    return parse_structured_result(
        task="insight", content=response.content, allowed_event_ids=set()
    )


def _parsed_preference(value: dict[str, object]) -> ParsedResult:
    return ParsedResult(
        task="preference",
        parse_success=True,
        strict_parse_success=True,
        schema_valid=isinstance(value.get("interests"), list),
        repair_count=0,
        value=value,
        metrics=_preference_structure_metrics(value),
    )


def _creator_evidence(cohort: Phase3Cohort) -> set[str]:
    creators = {
        str(item).strip().casefold()
        for item in list(cohort.existing_preference.get("favorite_up_users") or [])
        if str(item).strip()
    }
    for event in cohort.preference_events:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict):
            continue
        for key in ("author", "author_name", "creator", "creator_name", "up_name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                creators.add(value.strip().casefold())
    return creators


def _usage_totals(
    calls: Sequence[Mapping[str, object]],
    *,
    task: str,
    logical_run: str,
) -> dict[str, int]:
    totals = {
        "call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
    }
    for call in calls:
        if call.get("task") != task or call.get("logical_run") != logical_run:
            continue
        totals["call_count"] += 1
        usage = call.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return totals


def _savings(control: Mapping[str, int], treatment: Mapping[str, int], key: str) -> float:
    base = int(control.get(key, 0))
    return (base - int(treatment.get(key, 0))) / base if base > 0 else 0.0


def _strict_json_envelope(
    calls: Sequence[Mapping[str, object]],
    *,
    task: str,
) -> dict[str, object]:
    """Require treatment formatting repairs to stay inside control noise."""

    def _arm(logical_run: str) -> dict[str, int | float]:
        selected = [
            call
            for call in calls
            if call.get("task") == task and call.get("logical_run") == logical_run
        ]
        non_strict = sum(call.get("strict_json") is not True for call in selected)
        return {
            "call_count": len(selected),
            "non_strict_json_count": non_strict,
            "non_strict_json_rate": round(non_strict / len(selected), 6) if selected else 1.0,
        }

    arms = {run: _arm(run) for run in ("A1", "A2", "A", "B")}
    ceiling = max(float(arms[run]["non_strict_json_rate"]) for run in ("A1", "A2", "A"))
    treatment_rate = float(arms["B"]["non_strict_json_rate"])
    return {
        "passed": treatment_rate <= ceiling,
        "arms": arms,
        "treatment_rate_ceiling": round(ceiling, 6),
    }


def _quality_summary(
    *,
    cohort: Phase3Cohort,
    preference_results: Mapping[str, dict[str, object]],
    insight_results: Mapping[str, ParsedResult],
) -> dict[str, object]:
    creators = _creator_evidence(cohort)
    pref_parsed = {key: _parsed_preference(value) for key, value in preference_results.items()}
    preference_aa = _preference_pair_quality(
        pref_parsed.get("A1"), pref_parsed.get("A2"), creator_evidence=creators
    )
    preference_ab = _preference_pair_quality(
        pref_parsed.get("A"), pref_parsed.get("B"), creator_evidence=creators
    )
    insight_aa = _insight_pair_quality(insight_results.get("A1"), insight_results.get("A2"))
    insight_ab = _insight_pair_quality(insight_results.get("A"), insight_results.get("B"))

    pref_overlap_floor = max(
        PREFERENCE_TOP_INTEREST_OVERLAP_FLOOR,
        float(preference_aa.get("top_interest_weighted_overlap", 0.0)) - 0.05,
    )
    pref_style_ceiling = float(preference_aa.get("style_drift", 0.0)) + 0.10
    preference_passed = bool(
        preference_aa.get("comparable")
        and preference_ab.get("comparable")
        and float(preference_ab.get("top_interest_weighted_overlap", 0.0)) >= pref_overlap_floor
        and float(preference_ab.get("style_drift", 1.0)) <= pref_style_ceiling
        and int(preference_ab.get("right_hallucinated_creator_count", 1))
        <= int(preference_aa.get("right_hallucinated_creator_count", 0))
        and int(preference_ab.get("right_creator_evidence_loss_count", 1))
        <= int(preference_aa.get("right_creator_evidence_loss_count", 0))
    )

    insight_count_ceiling = max(
        1,
        int(insight_aa.get("hypothesis_count_delta", 0)) + 1,
    )
    insight_evidence_ceiling = max(
        0.50,
        float(insight_aa.get("mean_evidence_count_drift", 0.0)) + 0.20,
    )
    insight_confidence_ceiling = max(
        0.10,
        float(insight_aa.get("mean_confidence_drift", 0.0)) + 0.05,
    )

    def _duplicate_hypothesis_count(result: ParsedResult) -> int:
        items = result.value if isinstance(result.value, list) else []
        normalized = [
            "".join(str(item.get("hypothesis") or "").split()).casefold()
            for item in items
            if isinstance(item, dict) and str(item.get("hypothesis") or "").strip()
        ]
        return len(normalized) - len(set(normalized))

    insight_duplicate_ceiling = max(
        _duplicate_hypothesis_count(insight_results["A1"]),
        _duplicate_hypothesis_count(insight_results["A2"]),
        _duplicate_hypothesis_count(insight_results["A"]),
    )
    insight_treatment_duplicates = _duplicate_hypothesis_count(insight_results["B"])
    insight_passed = bool(
        insight_aa.get("comparable")
        and insight_ab.get("comparable")
        and insight_results["B"].schema_valid
        and int(insight_ab.get("right_invalid_structure_count", 1)) == 0
        and int(insight_ab.get("hypothesis_count_delta", 999)) <= insight_count_ceiling
        and float(insight_ab.get("mean_evidence_count_drift", 999.0)) <= insight_evidence_ceiling
        and float(insight_ab.get("mean_confidence_drift", 999.0)) <= insight_confidence_ceiling
        and insight_treatment_duplicates <= insight_duplicate_ceiling
    )
    return {
        "passed": preference_passed and insight_passed,
        "preference": {
            "passed": preference_passed,
            "control_aa": preference_aa,
            "treatment_ab": preference_ab,
            "envelope": {
                "top_interest_overlap_floor": round(pref_overlap_floor, 6),
                "style_drift_ceiling": round(pref_style_ceiling, 6),
            },
        },
        "insight": {
            "passed": insight_passed,
            "control_aa": insight_aa,
            "treatment_ab": insight_ab,
            "envelope": {
                "hypothesis_count_delta_ceiling": insight_count_ceiling,
                "mean_evidence_count_drift_ceiling": round(insight_evidence_ceiling, 6),
                "mean_confidence_drift_ceiling": round(insight_confidence_ceiling, 6),
                "duplicate_hypothesis_count_ceiling": insight_duplicate_ceiling,
            },
            "treatment_duplicate_hypothesis_count": insight_treatment_duplicates,
        },
    }


def _merge_history_check(
    cohort: Phase3Cohort,
    treatment: ParsedResult,
) -> dict[str, object]:
    from openbiliclaw.soul.insight_analyzer import InsightAnalyzer

    analyzer = InsightAnalyzer(cast("Any", _NoProviderService()))
    incoming: list[InsightHypothesis] = []
    raw_items = treatment.value if isinstance(treatment.value, list) else []
    for item in raw_items:
        if isinstance(item, dict):
            incoming.append(analyzer._build_hypothesis(item))  # noqa: SLF001
    merged = analyzer.merge_insights(list(cohort.all_insights), incoming)
    prior_verdicts = {
        analyzer._normalize_text(item.hypothesis): item.user_verdict  # noqa: SLF001
        for item in cohort.all_insights
        if item.user_verdict
    }
    merged_verdicts = {
        analyzer._normalize_text(item.hypothesis): item.user_verdict  # noqa: SLF001
        for item in merged
        if item.user_verdict
    }
    return {
        "passed": len(merged) >= len(cohort.all_insights)
        and all(merged_verdicts.get(key) == value for key, value in prior_verdicts.items()),
        "durable_before": len(cohort.all_insights),
        "durable_after_merge": len(merged),
        "judged_before": len(prior_verdicts),
        "judged_preserved": sum(
            merged_verdicts.get(key) == value for key, value in prior_verdicts.items()
        ),
    }


async def execute_real_phase3(
    *,
    cohort: Phase3Cohort,
    plan: Phase3Plan,
    recorder: RecordingClient,
    expected_route: PinnedRoute,
) -> dict[str, object]:
    preference_results: dict[str, dict[str, object]] = {}
    for logical_run in ("A1", "A2", "A"):
        preference_results[logical_run] = await _run_preference_arm(
            recorder=recorder,
            cohort=cohort,
            chunk_size=plan.preference_control_chunk_size,
            logical_run=logical_run,
        )
    preference_results["B"] = await _run_preference_arm(
        recorder=recorder,
        cohort=cohort,
        chunk_size=plan.preference_treatment_chunk_size,
        logical_run="B",
        planned_chunk_sizes=plan.preference_treatment_chunk_sizes,
    )

    insight_results: dict[str, ParsedResult] = {}
    for logical_run in ("A1", "A2", "A"):
        insight_results[logical_run] = await _run_insight_arm(
            recorder=recorder,
            messages=plan.insight_control_messages,
            logical_run=logical_run,
        )
    insight_results["B"] = await _run_insight_arm(
        recorder=recorder,
        messages=plan.insight_treatment_messages,
        logical_run="B",
    )

    quality = _quality_summary(
        cohort=cohort,
        preference_results=preference_results,
        insight_results=insight_results,
    )
    merge_history = _merge_history_check(cohort, insight_results["B"])
    usage: dict[str, object] = {}
    for task in ("preference", "insight"):
        arms = {
            run: _usage_totals(recorder.calls, task=task, logical_run=run)
            for run in ("A1", "A2", "A", "B")
        }
        control = arms["A"]
        treatment = arms["B"]
        usage[task] = {
            "arms": arms,
            "prompt_token_savings": round(_savings(control, treatment, "prompt_tokens"), 6),
            "completion_token_savings": round(_savings(control, treatment, "completion_tokens"), 6),
            "total_token_savings": round(_savings(control, treatment, "total_tokens"), 6),
        }
    preference_usage = cast("Mapping[str, object]", usage["preference"])
    insight_usage = cast("Mapping[str, object]", usage["insight"])
    pref_savings = float(preference_usage["prompt_token_savings"])
    insight_savings = float(insight_usage["prompt_token_savings"])
    expected_core_calls = (
        len(plan.preference_control_chunks) * 3 + len(plan.preference_treatment_chunks) + 4
    )
    route = route_audit(
        recorder.calls,
        expected=expected_route,
        expected_call_count=expected_core_calls,
    )
    provider_format = {
        task: _strict_json_envelope(recorder.calls, task=task) for task in ("preference", "insight")
    }
    provider_format_passed = all(bool(item["passed"]) for item in provider_format.values())
    quality["provider_format"] = provider_format
    quality["passed"] = bool(quality["passed"] and provider_format_passed)
    gate = {
        "passed": bool(
            route["passed"]
            and quality["passed"]
            and merge_history["passed"]
            and pref_savings >= PREFERENCE_PROMPT_SAVINGS_MIN
            and insight_savings >= INSIGHT_PROMPT_SAVINGS_MIN
        ),
        "route": route,
        "quality": quality,
        "merge_history": merge_history,
        "token_checks": {
            "preference_prompt_savings_passed": pref_savings >= PREFERENCE_PROMPT_SAVINGS_MIN,
            "insight_prompt_savings_passed": insight_savings >= INSIGHT_PROMPT_SAVINGS_MIN,
            "preference_prompt_savings_min": PREFERENCE_PROMPT_SAVINGS_MIN,
            "insight_prompt_savings_min": INSIGHT_PROMPT_SAVINGS_MIN,
        },
    }
    return {"calls": list(recorder.calls), "usage": usage, "quality": quality, "gate": gate}


async def _run_keyword_planner_arm(
    *,
    database: Any,
    config: Config,
    profile: object,
    recorder: RecordingClient,
    logical_run: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    from openbiliclaw.runtime.keyword_planner import KeywordPlanner

    recorder.set_context("keyword_planner", logical_run)
    call_start = len(recorder.calls)
    planner = KeywordPlanner(
        llm_service=recorder,
        database=database,
        config=config,
        soul_engine=cast("Any", _StaticSoulEngine(profile)),
        pool_target_count=KEYWORD_CACHE_HIGH,
    )
    planner.bind_deficit_source(cast("Any", _BilibiliOnlyDeficit()))
    cycle_ledger = await planner.run_once()
    calls = list(recorder.calls[call_start:])
    pending = int(
        database.count_pending_keywords_all_digests(
            "bilibili",
            keyword_kind="regular",
        )
    )
    return (
        {
            "grace_hours": int(config.discovery.keyword_digest_grace_hours),
            "provider_call_count": len(calls),
            "pending_after": pending,
            "generated": int(cycle_ledger.get("bilibili", 0)),
            "digest_grace_ledger": dict(planner.last_digest_grace_ledger.get("bilibili", {})),
            "usage": _usage_totals(
                recorder.calls,
                task="keyword_planner",
                logical_run=logical_run,
            ),
        },
        calls,
    )


async def execute_keyword_e2e(
    *,
    config: Config,
    source_db_path: Path,
    data_root: Path,
    cohort: Phase3Cohort,
    recorder: RecordingClient,
    expected_route: PinnedRoute,
) -> tuple[dict[str, object], list[object]]:
    """Exercise stale inventory through plan → claim → Bili → eval → cache."""
    from openbiliclaw.bilibili.api import BilibiliAPIClient
    from openbiliclaw.bilibili.auth import resolve_runtime_cookie
    from openbiliclaw.discovery.engine import ContentDiscoveryEngine
    from openbiliclaw.discovery.keyword_digest import profile_kw_digest
    from openbiliclaw.discovery.strategies.search import SearchStrategy
    from openbiliclaw.runtime.keyword_fetch import KeywordFetchCoordinator
    from openbiliclaw.soul.profile import OnionProfile

    profile = OnionProfile.from_dict(deepcopy(cohort.soul_profile))
    current_digest = profile_kw_digest(profile)
    seed_rows = _load_keyword_seed_rows(
        db_path=source_db_path,
        current_digest=current_digest,
    )
    provenance_before = {
        int(row["id"]): _keyword_provenance(row)
        for row in seed_rows
        if isinstance(row.get("id"), int)
    }
    private_values: list[object] = [seed_rows]

    with TemporaryDirectory(prefix="openbiliclaw-phase3-keywords-") as temp_dir:
        temp_root = Path(temp_dir)
        control_db = _create_keyword_test_database(temp_root / "control.db", seed_rows)
        treatment_db = _create_keyword_test_database(temp_root / "treatment.db", seed_rows)
        try:
            control_config = _keyword_test_config(config, grace_hours=0)
            treatment_config = _keyword_test_config(
                config,
                grace_hours=KEYWORD_GRACE_HOURS,
            )
            control, control_calls = await _run_keyword_planner_arm(
                database=control_db,
                config=control_config,
                profile=profile,
                recorder=recorder,
                logical_run="A",
            )
            treatment, treatment_calls = await _run_keyword_planner_arm(
                database=treatment_db,
                config=treatment_config,
                profile=profile,
                recorder=recorder,
                logical_run="B",
            )

            coordinator = KeywordFetchCoordinator(
                database=treatment_db,
                discovery_config=treatment_config.discovery,
            )
            claimed = coordinator.claim("bilibili", KEYWORD_FETCH_COUNT)
            claimed_ids = {item.id for item in claimed}
            queries = [item.keyword for item in claimed]
            private_values.append(queries)
            keyword_ids = {item.keyword: item.id for item in claimed}
            raw_candidates: list[Any] = []
            evaluated_candidates: list[Any] = []
            cached_count = 0
            evaluation_error_kind = ""
            evaluation_call_start = len(recorder.calls)

            client = BilibiliAPIClient(
                cookie=resolve_runtime_cookie(
                    data_dir=data_root,
                    configured_cookie=config.bilibili.cookie,
                ),
                proxy=config.bilibili.proxy or None,
            )
            try:
                search = SearchStrategy(
                    llm_service=recorder,
                    bilibili_client=client,
                    database=treatment_db,
                    queries_per_run=KEYWORD_FETCH_COUNT,
                    page_size=5,
                    max_pages=1,
                    llm_evaluation=False,
                )
                raw_candidates = await search.discover(
                    profile,
                    limit=KEYWORD_FETCH_COUNT * 5,
                    queries=queries,
                    keyword_ids=keyword_ids,
                )
                if raw_candidates:
                    evaluated_candidates = raw_candidates[:KEYWORD_EVAL_LIMIT]
                    recorder.set_context("keyword_e2e", "B-eval")
                    evaluator = ContentDiscoveryEngine(
                        llm_service=recorder,
                        database=treatment_db,
                        eval_prefilter_mode="off",
                        evaluation_candidate_transport="sparse-json",
                    )
                    await evaluator.evaluate_content_batch(
                        evaluated_candidates,
                        profile,
                        source_context="search",
                    )
                    admitted = [
                        item
                        for item in evaluated_candidates
                        if float(item.relevance_score or 0.0) >= 0.60
                    ]
                    cached_count = evaluator.cache_evaluated_results(admitted)
                    coordinator.mark_used(claimed)
                else:
                    coordinator.mark_failed(claimed)
            except Exception as exc:
                evaluation_error_kind = type(exc).__name__
                coordinator.mark_failed(claimed)
            finally:
                await client.close()

            private_values.append(
                [
                    {
                        "title": item.title,
                        "description": item.description,
                        "content_url": item.content_url,
                        "author_name": item.author_name,
                    }
                    for item in raw_candidates
                ]
            )
            evaluation_calls = list(recorder.calls[evaluation_call_start:])
            if claimed_ids:
                placeholders = ", ".join("?" for _ in claimed_ids)
                terminal_rows = treatment_db.conn.execute(
                    f"SELECT * FROM discovery_keywords WHERE id IN ({placeholders})",
                    tuple(sorted(claimed_ids)),
                ).fetchall()
            else:
                terminal_rows = []
            terminal = [dict(row) for row in terminal_rows]
            provenance_preserved = bool(terminal) and all(
                _keyword_provenance(row) == provenance_before.get(int(row["id"]))
                for row in terminal
            )
            used_count = sum(str(row.get("status")) == "used" for row in terminal)
            credited_count = sum(int(row.get("yield_count") or 0) > 0 for row in terminal)
            candidate_provenance_count = sum(
                item.source_keyword_id in claimed_ids for item in raw_candidates
            )
            scores = [float(item.relevance_score or 0.0) for item in evaluated_candidates]
            admitted_count = sum(score >= 0.60 for score in scores)

            control_usage = cast("Mapping[str, int]", control["usage"])
            treatment_usage = cast("Mapping[str, int]", treatment["usage"])
            planning_savings = _savings(
                control_usage,
                treatment_usage,
                "prompt_tokens",
            )
            observed_route_calls = [*control_calls, *treatment_calls, *evaluation_calls]
            route = route_audit(
                observed_route_calls,
                expected=expected_route,
                expected_call_count=len(observed_route_calls),
            )
            grace_ledger = cast("Mapping[str, object]", treatment["digest_grace_ledger"])
            reused = int(grace_ledger.get("reused", 0))
            passed = bool(
                len(seed_rows) >= KEYWORD_CACHE_LOW
                and int(control["provider_call_count"]) == 1
                and int(control["generated"]) > 0
                and int(treatment["provider_call_count"]) == 0
                and reused >= KEYWORD_CACHE_LOW
                and int(treatment["pending_after"]) >= KEYWORD_CACHE_LOW
                and len(claimed) > 0
                and provenance_preserved
                and len(raw_candidates) > 0
                and candidate_provenance_count == len(raw_candidates)
                and len(evaluated_candidates) > 0
                and len(evaluation_calls) > 0
                and admitted_count > 0
                and cached_count > 0
                and used_count == len(claimed)
                and credited_count > 0
                and route["passed"]
            )
            result = {
                "seeded_recent_stale_count": len(seed_rows),
                "control": control,
                "treatment": treatment,
                "planning_prompt_token_savings": round(planning_savings, 6),
                "pipeline": {
                    "claimed_count": len(claimed),
                    "source_digest_and_metadata_preserved": provenance_preserved,
                    "api_candidate_count": len(raw_candidates),
                    "candidate_provenance_count": candidate_provenance_count,
                    "evaluated_count": len(evaluated_candidates),
                    "provider_evaluation_call_count": len(evaluation_calls),
                    "admitted_count": admitted_count,
                    "newly_cached_count": cached_count,
                    "used_keyword_count": used_count,
                    "yield_credited_keyword_count": credited_count,
                    "mean_score": round(sum(scores) / len(scores), 6) if scores else 0.0,
                    "max_score": round(max(scores), 6) if scores else 0.0,
                    "error_kind": evaluation_error_kind,
                },
                "route": route,
                "gate": {"passed": passed},
            }
        finally:
            control_db.close()
            treatment_db.close()
    return result, private_values


def build_render_artifact(cohort: Phase3Cohort, plan: Phase3Plan) -> dict[str, object]:
    preference = cast("Mapping[str, object]", plan.summary["preference"])
    insight = cast("Mapping[str, object]", plan.summary["insight"])
    passed = bool(
        int(preference["treatment_call_count"]) <= int(preference["control_call_count"])
        and float(preference["character_savings"]) > 0.0
        and int(insight["selected_hypothesis_count"]) <= 40
        and float(insight["character_savings"]) > 0.0
        and insight["system_instruction_invariant"] is True
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "render-only",
        "cohort": {
            "snapshot_digest": cohort.snapshot_digest,
            "preference_event_count": len(cohort.preference_events),
            "insight_note_count": len(cohort.insight_notes),
            "durable_hypothesis_count": len(cohort.all_insights),
        },
        "render": plan.summary,
        "gate": {"passed": passed},
    }


def _database_path(config: object) -> Path:
    return Path(getattr(config, "data_path", PROJECT_ROOT / "data")) / "openbiliclaw.db"


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
    parser = argparse.ArgumentParser(description="Phase 3 token-diet privacy-safe replay")
    parser.add_argument("--mode", choices=("render-only", "real-provider"), default="render-only")
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--preference-events", type=_positive_int, default=3)
    parser.add_argument("--insight-notes", type=_positive_int, default=20)
    parser.add_argument("--instance", default="")
    parser.add_argument("--expected-model", default="")
    parser.add_argument("--confirm-sensetime-route", action="store_true")
    parser.add_argument(
        "--keyword-e2e",
        action="store_true",
        help="also run disposable-DB keyword reuse through real Bilibili search and evaluation",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=_nonnegative_float,
        default=3.0,
        help="minimum delay between real provider requests (the replay is always single-flight)",
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    from openbiliclaw.config import load_config

    config = load_config(args.config) if args.config else load_config()
    db_path = Path(args.db) if args.db else _database_path(config)
    data_root = Path(args.data_root) if args.data_root else db_path.parent
    if not db_path.exists():
        raise ReplayContractError(f"database not found: {db_path}")
    cohort = freeze_phase3_cohort(
        db_path=db_path,
        data_root=data_root,
        preference_event_count=int(args.preference_events),
        insight_note_count=int(args.insight_notes),
    )
    plan = build_phase3_plan(cohort)
    private_values: list[object] = [
        cohort.preference_events,
        cohort.existing_preference,
        cohort.soul_profile,
        cohort.preference_awareness_tail,
        cohort.preference_insight_tail,
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
    real = await execute_real_phase3(
        cohort=cohort,
        plan=plan,
        recorder=recorder,
        expected_route=route,
    )
    keyword_result: dict[str, object] | None = None
    keyword_private_values: list[object] = []
    if bool(args.keyword_e2e):
        keyword_result, keyword_private_values = await execute_keyword_e2e(
            config=config,
            source_db_path=db_path,
            data_root=data_root,
            cohort=cohort,
            recorder=recorder,
            expected_route=route,
        )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "real-provider",
        "cohort": {
            "snapshot_digest": cohort.snapshot_digest,
            "preference_event_count": len(cohort.preference_events),
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
    if keyword_result is not None:
        cognition_gate = cast("Mapping[str, object]", real["gate"])
        keyword_gate = cast("Mapping[str, object]", keyword_result["gate"])
        artifact["keyword_e2e"] = keyword_result
        artifact["gate"] = {
            "passed": bool(cognition_gate["passed"] and keyword_gate["passed"]),
            "cognition": cognition_gate,
            "keyword_e2e": keyword_gate,
        }
    write_artifact(
        Path(args.output),
        artifact,
        private_values=[
            *private_values,
            *keyword_private_values,
            *recorder.response_bodies,
        ],
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
        logger.error("Phase 3 token-diet replay failed: %s", exc)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
