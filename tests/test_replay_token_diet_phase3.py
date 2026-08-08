"""Tests for the privacy-safe Phase 3 token-diet replay harness."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from scripts.replay_cognition_token_diet import write_artifact
from scripts.replay_token_diet_phase3 import (
    Phase3Cohort,
    RecordingClient,
    _create_keyword_test_database,
    _keyword_test_config,
    _load_keyword_seed_rows,
    _run_keyword_planner_arm,
    _strict_json_envelope,
    build_phase3_plan,
    build_render_artifact,
)

from openbiliclaw.config import Config
from openbiliclaw.llm.base import LLMResponse
from openbiliclaw.soul.profile import (
    AwarenessNote,
    InsightHypothesis,
    InterestTag,
    PreferenceLayer,
    SoulProfile,
)
from openbiliclaw.storage.database import Database

if TYPE_CHECKING:
    from pathlib import Path


class _KeywordProvider:
    async def complete_structured_task(self, **_: object) -> LLMResponse:
        return LLMResponse(
            content=json.dumps({"bilibili": [f"generated-keyword-{index}" for index in range(12)]}),
            provider="openai_compatible",
            instance_id="sense-test",
            model="sense-model",
            usage={
                "prompt_tokens": 800,
                "completion_tokens": 100,
                "total_tokens": 900,
                "cached_input_tokens": 0,
            },
        )


def test_strict_json_envelope_rejects_treatment_repair_regression() -> None:
    calls = [
        {"task": "insight", "logical_run": "A1", "strict_json": True},
        {"task": "insight", "logical_run": "A2", "strict_json": True},
        {"task": "insight", "logical_run": "A", "strict_json": True},
        {"task": "insight", "logical_run": "B", "strict_json": False},
    ]

    envelope = _strict_json_envelope(calls, task="insight")

    assert envelope["passed"] is False
    assert envelope["treatment_rate_ceiling"] == 0.0


def _source_keyword_database(path: Path, *, count: int = 12) -> Database:
    database = Database(path)
    database.initialize()
    words = [f"private-stale-keyword-{index}" for index in range(count)]
    metadata = {
        word: {
            "source_interest": "private-interest",
            "generation_reason": "private-reason",
        }
        for word in words
    }
    assert (
        database.insert_pending_keywords(
            "bilibili",
            words,
            "stale-digest",
            metadata_by_keyword=metadata,
        )
        == count
    )
    database.conn.execute(
        """
        UPDATE discovery_keywords
        SET status = 'expired', used_at = NULL, created_at = CURRENT_TIMESTAMP
        WHERE platform = 'bilibili'
        """
    )
    database.conn.commit()
    return database


def _profile() -> SoulProfile:
    return SoulProfile(
        preferences=PreferenceLayer(
            interests=[InterestTag(name="software systems", category="technology", weight=0.9)]
        )
    )


def test_keyword_seed_copy_is_disposable_and_preserves_source_rows(tmp_path: Path) -> None:
    source = _source_keyword_database(tmp_path / "source.db")
    try:
        rows = _load_keyword_seed_rows(
            db_path=tmp_path / "source.db",
            current_digest="current-digest",
            limit=12,
        )
        disposable = _create_keyword_test_database(tmp_path / "disposable.db", rows)
        try:
            assert disposable.count_pending_keywords_all_digests("bilibili") == 12
            copied = disposable.conn.execute(
                "SELECT profile_kw_digest, source_interest, generation_reason, status "
                "FROM discovery_keywords ORDER BY id"
            ).fetchall()
            assert {str(row["profile_kw_digest"]) for row in copied} == {"stale-digest"}
            assert {str(row["source_interest"]) for row in copied} == {"private-interest"}
            assert {str(row["generation_reason"]) for row in copied} == {"private-reason"}
            assert {str(row["status"]) for row in copied} == {"pending"}
        finally:
            disposable.close()

        original_statuses = source.conn.execute(
            "SELECT DISTINCT status FROM discovery_keywords"
        ).fetchall()
        assert [str(row["status"]) for row in original_statuses] == ["expired"]
    finally:
        source.close()


@pytest.mark.asyncio
async def test_keyword_control_calls_provider_while_grace_reuses_without_call(
    tmp_path: Path,
) -> None:
    source = _source_keyword_database(tmp_path / "source.db")
    try:
        rows = _load_keyword_seed_rows(
            db_path=tmp_path / "source.db",
            current_digest="current-digest",
            limit=12,
        )
    finally:
        source.close()
    control_db = _create_keyword_test_database(tmp_path / "control.db", rows)
    treatment_db = _create_keyword_test_database(tmp_path / "treatment.db", rows)
    recorder = RecordingClient(_KeywordProvider())
    try:
        control, _ = await _run_keyword_planner_arm(
            database=control_db,
            config=_keyword_test_config(Config(), grace_hours=0),
            profile=_profile(),
            recorder=recorder,
            logical_run="A",
        )
        treatment, _ = await _run_keyword_planner_arm(
            database=treatment_db,
            config=_keyword_test_config(Config(), grace_hours=24),
            profile=_profile(),
            recorder=recorder,
            logical_run="B",
        )
    finally:
        control_db.close()
        treatment_db.close()

    assert control["provider_call_count"] == 1
    assert control["generated"] == 12
    assert treatment["provider_call_count"] == 0
    assert treatment["pending_after"] == 12
    assert treatment["digest_grace_ledger"] == {
        "current": 0,
        "reused": 12,
        "expired_aged": 0,
        "expired_blocked": 0,
        "expired_excess": 0,
    }
    assert len(recorder.calls) == 1


def test_render_artifact_excludes_private_context(tmp_path: Path) -> None:
    private_marker = "PRIVATE_PHASE3_CONTEXT_MUST_NOT_PERSIST"
    events = tuple(
        {
            "id": index,
            "event_type": "view",
            "title": f"{private_marker}-{index}",
            "context": {"detail": private_marker * 100},
            "metadata": {},
        }
        for index in range(1, 4)
    )
    existing_preference: dict[str, Any] = {
        "interests": [
            {
                "name": private_marker,
                "category": "test",
                "weight": 0.9,
                "evidence": [private_marker * 800],
            }
        ]
    }
    notes = (AwarenessNote(observation=private_marker),)
    insights = tuple(
        InsightHypothesis(
            hypothesis=f"{private_marker}-{index}",
            evidence=[private_marker],
            confidence=0.7,
        )
        for index in range(50)
    )
    cohort = Phase3Cohort(
        preference_events=events,
        existing_preference=existing_preference,
        soul_profile={"personality_portrait": private_marker},
        preference_awareness_tail=(),
        preference_insight_tail=(),
        insight_notes=notes,
        all_insights=insights,
        snapshot_digest="a" * 64,
        preference_input_digest="b" * 64,
        insight_input_digest="c" * 64,
        recent_expired_unused_regular={"bilibili": 12},
    )

    plan = build_phase3_plan(cohort)
    artifact = build_render_artifact(cohort, plan)
    output = tmp_path / "artifact.json"
    write_artifact(
        output,
        artifact,
        private_values=[events, existing_preference, notes, insights],
    )

    serialized = output.read_text(encoding="utf-8")
    assert artifact["gate"]["passed"] is True  # type: ignore[index]
    assert private_marker not in serialized
    assert artifact["render"]["insight"]["selected_hypothesis_count"] == 20  # type: ignore[index]
