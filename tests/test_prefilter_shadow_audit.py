"""Phase 2 evaluator-prefilter shadow evidence and gate tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.evaluate_prefilter_shadow_gate import _read_frozen_rows
from scripts.evaluate_prefilter_shadow_gate import main as gate_main

from openbiliclaw.discovery.engine import ContentDiscoveryEngine, DiscoveredContent
from openbiliclaw.discovery.prefilter_audit import (
    PREFILTER_EXPLORE_EXEMPT_STATUS,
    PREFILTER_NO_INTERESTS_STATUS,
    PREFILTER_OK_STATUS,
    PrefilterShadowDecision,
    PrefilterShadowOutcome,
    classify_prefilter_context,
    evaluate_prefilter_gate,
    hash_prefilter_candidate_identity,
)
from openbiliclaw.soul.profile import InterestTag, SoulProfile
from openbiliclaw.storage.database import Database


def _profile() -> SoulProfile:
    profile = SoulProfile()
    profile.preferences.interests = [
        InterestTag(name="纪录片", category="知识", weight=1.0),
    ]
    return profile


class _EmbeddingService:
    embedding_fingerprint = "a" * 32

    def __init__(self, vectors: dict[str, list[float]], *, failures: set[str] | None = None):
        self.vectors = vectors
        self.failures = set(failures or ())

    async def embed(self, text: str) -> list[float]:
        if text in self.failures:
            raise RuntimeError("synthetic embedding failure")
        return self.vectors.get(text, [])


class _BatchLLM:
    def __init__(self, score: float = 0.8) -> None:
        self.score = score
        self.calls: list[str] = []

    async def complete_structured_task(self, **kwargs: object) -> object:
        user_input = str(kwargs["user_input"])
        self.calls.append(user_input)
        raw_batch = user_input.split("<content_batch>", 1)[1].split("</content_batch>", 1)[0]
        envelope = json.loads(raw_batch.strip())
        assert isinstance(envelope, dict)
        items = envelope["items"]
        assert isinstance(items, list)
        return SimpleNamespace(
            content=json.dumps(
                [
                    {
                        "id": str(item["id"]),
                        "score": self.score,
                        "reason": "ok",
                        "topic_group": "test",
                        "style_key": "deep_focus",
                        "franchise_key": "",
                    }
                    for item in items
                ]
            )
        )


def _gate_row(
    index: int,
    *,
    platform: str = "bilibili",
    context: str = "trending",
    would_filter: bool = False,
    admitted: bool = True,
    explore: bool = False,
    status: str = PREFILTER_OK_STATUS,
    fail_open: bool = False,
    strong_interest: bool = False,
) -> dict[str, object]:
    score = 0.8 if admitted else 0.2
    similarity: float | None = 0.1 if would_filter else 0.8
    if status != PREFILTER_OK_STATUS:
        similarity = None
    return {
        "decision_id": f"{index:032x}",
        "candidate_hash": hash_prefilter_candidate_identity(f"candidate:{index}"),
        "platform_class": platform,
        "context_class": context,
        "similarity": similarity,
        "threshold": 0.2,
        "explore": explore,
        "embedding_namespace": "a" * 24,
        "profile_digest": "b" * 24,
        "would_filter": would_filter,
        "embedding_status": status,
        "fail_open": fail_open,
        "explicit_strong_interest": strong_interest,
        "llm_score": score,
        "admission_threshold": 0.6,
        "admission_result": admitted,
    }


def test_context_classification_never_persists_query_text() -> None:
    assert classify_prefilter_context("search_query: 私密长查询", "search") == "search"
    assert classify_prefilter_context("mixed", "explore") == "explore"
    digest = hash_prefilter_candidate_identity("bilibili:BVPRIVATE")
    assert len(digest) == 64
    assert "BVPRIVATE" not in digest


def test_prefilter_gate_passes_exact_spec_boundaries() -> None:
    rows = [_gate_row(index) for index in range(100)]
    rows[0] = _gate_row(0, would_filter=True)
    rows[1] = _gate_row(
        1,
        context="explore",
        explore=True,
        status=PREFILTER_EXPLORE_EXEMPT_STATUS,
    )
    rows[2] = _gate_row(
        2,
        status="content_embedding_error",
        fail_open=True,
    )

    report = evaluate_prefilter_gate(rows)

    assert report.passed is True
    assert report.joinable_candidates == 100
    assert report.telemetry_coverage == 1.0
    assert report.admission_recall == 0.99
    assert report.high_score_false_negatives == 1
    assert report.explore_candidates == 1
    assert report.fail_open_cases == 1
    assert report.reasons == ()


def test_prefilter_gate_rejects_incomplete_and_protected_false_negative() -> None:
    rows = [_gate_row(index) for index in range(100)]
    rows[0] = _gate_row(0, would_filter=True, strong_interest=True)
    rows[1] = _gate_row(
        1,
        context="explore",
        explore=True,
        status=PREFILTER_EXPLORE_EXEMPT_STATUS,
    )
    rows[2] = _gate_row(
        2,
        status="content_embedding_error",
        fail_open=True,
    )
    rows[3]["llm_score"] = None

    report = evaluate_prefilter_gate(rows)

    assert report.passed is False
    assert report.joinable_candidates == 99
    assert "joinable_candidates_below_100" in report.reasons
    assert "telemetry_coverage_below_1.0" in report.reasons
    assert "explicit_strong_interest_false_negative" in report.reasons


def test_prefilter_gate_rejects_platform_recall_and_fail_open_violation() -> None:
    rows = [_gate_row(index) for index in range(100)]
    for index in range(6):
        rows[index] = _gate_row(index, would_filter=True)
    rows[6] = _gate_row(
        6,
        context="explore",
        explore=True,
        status=PREFILTER_EXPLORE_EXEMPT_STATUS,
    )
    rows[7] = _gate_row(
        7,
        would_filter=True,
        admitted=False,
        status="content_embedding_error",
        fail_open=False,
    )

    report = evaluate_prefilter_gate(rows)

    assert report.passed is False
    assert "admission_recall_below_0.99" in report.reasons
    assert "high_score_false_negatives_above_1" in report.reasons
    assert "platform_recall_below_0.95:bilibili" in report.reasons
    assert "embedding_fail_open_violation" in report.reasons


def test_prefilter_gate_rejects_explore_false_negative() -> None:
    rows = [_gate_row(index) for index in range(100)]
    rows[0] = _gate_row(
        0,
        context="explore",
        would_filter=True,
        explore=True,
        status=PREFILTER_EXPLORE_EXEMPT_STATUS,
    )
    rows[1] = _gate_row(
        1,
        status="content_embedding_error",
        fail_open=True,
    )

    report = evaluate_prefilter_gate(rows)

    assert report.passed is False
    assert report.explore_false_negatives == 1
    assert "explore_false_negative" in report.reasons


def test_profile_interests_missing_is_degraded_and_violation_closes_gate() -> None:
    rows = [_gate_row(index) for index in range(100)]
    rows[0] = _gate_row(
        0,
        context="explore",
        explore=True,
        status=PREFILTER_EXPLORE_EXEMPT_STATUS,
    )
    rows[1] = _gate_row(
        1,
        status=PREFILTER_NO_INTERESTS_STATUS,
        fail_open=False,
    )

    report = evaluate_prefilter_gate(rows)

    assert report.degraded_embedding_cases == 1
    assert report.fail_open_violations == 1
    assert "embedding_fail_open_violation" in report.reasons


def test_database_persists_join_and_prunes_privacy_safe_audit(tmp_path: Path) -> None:
    database = Database(tmp_path / "audit.db")
    database.initialize()
    private_identity = "bilibili:BV-PRIVATE-IDENTITY"
    first = PrefilterShadowDecision(
        content_index=0,
        candidate_hash=hash_prefilter_candidate_identity(private_identity),
        platform_class="bilibili",
        context_class="search",
        similarity=0.1,
        threshold=0.2,
        explore=False,
        embedding_namespace="a" * 24,
        profile_digest="b" * 24,
        would_filter=True,
        embedding_status=PREFILTER_OK_STATUS,
        fail_open=False,
        explicit_strong_interest=True,
    )
    second = PrefilterShadowDecision(
        content_index=1,
        candidate_hash=hash_prefilter_candidate_identity("youtube:private-two"),
        platform_class="youtube",
        context_class="explore",
        similarity=None,
        threshold=0.2,
        explore=True,
        embedding_namespace="a" * 24,
        profile_digest="b" * 24,
        would_filter=False,
        embedding_status=PREFILTER_EXPLORE_EXEMPT_STATUS,
        fail_open=False,
        explicit_strong_interest=False,
    )

    assert (
        database.record_prefilter_shadow_decisions(
            [first.as_storage_record(), second.as_storage_record()]
        )
        == 2
    )
    unsafe_record = first.as_storage_record()
    unsafe_record["decision_id"] = "c" * 32
    unsafe_record["candidate_hash"] = private_identity
    unsafe_record["context_class"] = "private_query"
    with pytest.raises(ValueError):
        database.record_prefilter_shadow_decisions([unsafe_record])
    assert (
        database.complete_prefilter_shadow_decisions(
            [
                PrefilterShadowOutcome(
                    decision_id=first.decision_id,
                    llm_score=0.8,
                    admission_threshold=0.6,
                    admission_result=True,
                ).as_storage_record(),
                PrefilterShadowOutcome(
                    decision_id=second.decision_id,
                    llm_score=0.7,
                    admission_threshold=0.58,
                    admission_result=True,
                ).as_storage_record(),
            ]
        )
        == 2
    )

    rows = database.query_prefilter_shadow_audit()
    assert database.prefilter_shadow_audit_counts() == {
        "total": 2,
        "joined": 2,
        "incomplete": 0,
    }
    serialized = json.dumps(rows, ensure_ascii=False)
    assert private_identity not in serialized
    assert "private-two" not in serialized
    assert all(row["llm_score"] is not None for row in rows)

    database.conn.execute(
        "UPDATE evaluator_prefilter_shadow_audit "
        "SET created_at = datetime('now', '-31 days') WHERE decision_id = ?",
        (first.decision_id,),
    )
    database.conn.commit()
    third = PrefilterShadowDecision(
        content_index=2,
        candidate_hash=hash_prefilter_candidate_identity("reddit:private-three"),
        platform_class="reddit",
        context_class="feed",
        similarity=0.8,
        threshold=0.2,
        explore=False,
        embedding_namespace="a" * 24,
        profile_digest="b" * 24,
        would_filter=False,
        embedding_status=PREFILTER_OK_STATUS,
        fail_open=False,
        explicit_strong_interest=False,
    )
    assert database.record_prefilter_shadow_decisions([third.as_storage_record()]) == 1
    remaining_ids = {str(row["decision_id"]) for row in database.query_prefilter_shadow_audit()}
    assert first.decision_id not in remaining_ids
    assert {second.decision_id, third.decision_id} == remaining_ids


def test_gate_command_reads_a_frozen_cohort_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "gate-read-only.db"
    database = Database(path)
    database.initialize()
    decision = PrefilterShadowDecision(
        content_index=0,
        candidate_hash=hash_prefilter_candidate_identity("bilibili:private"),
        platform_class="bilibili",
        context_class="trending",
        similarity=0.8,
        threshold=0.2,
        explore=False,
        embedding_namespace="a" * 24,
        profile_digest="b" * 24,
        would_filter=False,
        embedding_status=PREFILTER_OK_STATUS,
        fail_open=False,
        explicit_strong_interest=False,
    )
    database.record_prefilter_shadow_decisions([decision.as_storage_record()])
    database.complete_prefilter_shadow_decisions(
        [
            PrefilterShadowOutcome(
                decision_id=decision.decision_id,
                llm_score=0.8,
                admission_threshold=0.6,
                admission_result=True,
            ).as_storage_record()
        ]
    )
    before = database.conn.total_changes
    # Earlier API tests may configure Rich logging to stdout process-wide.
    # Discard setup output so this assertion still verifies that the gate
    # command itself emits exactly one JSON document, independent of order.
    capsys.readouterr()

    rows, through_id, source_status = _read_frozen_rows(
        path,
        after_id=0,
        through_id=None,
    )
    exit_code = gate_main(["--db", str(path), "--through-id", str(through_id)])

    assert len(rows) == 1
    assert through_id == 1
    assert source_status == "ok"
    assert exit_code == 1
    assert database.conn.total_changes == before
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["cohort"]["through_id"] == 1
    assert artifact["counts"]["joinable_candidates"] == 1
    assert artifact["production_mode_change"] == "none"


@pytest.mark.asyncio
async def test_shadow_batch_persists_every_decision_and_joins_raw_scores(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "engine-audit.db")
    database.initialize()
    failing_text = "异常候选 私密内容"
    embedding = _EmbeddingService(
        {
            "纪录片": [1.0, 0.0],
            "低相似候选 私密厨房": [0.1, 0.9949874371],
            "匹配候选 纪录片解析": [1.0, 0.0],
        },
        failures={failing_text},
    )
    llm = _BatchLLM()
    engine = ContentDiscoveryEngine(
        llm_service=llm,
        database=database,
        embedding_service=embedding,
        eval_prefilter_mode="shadow",
    )
    contents = [
        DiscoveredContent(
            bvid="BVPRIVATELOW",
            title="低相似候选",
            description="私密厨房",
            source_strategy="trending",
        ),
        DiscoveredContent(
            bvid="BVPRIVATEKEEP",
            title="匹配候选",
            description="纪录片解析",
            source_strategy="search",
            source_keyword_id=17,
        ),
        DiscoveredContent(
            bvid="BVPRIVATEEXPLORE",
            title="跨域候选",
            description="私密跨域",
            source_strategy="explore",
        ),
        DiscoveredContent(
            bvid="BVPRIVATEFAIL",
            title="异常候选",
            description="私密内容",
            source_strategy="trending",
        ),
    ]

    scores = await engine.evaluate_content_batch(
        contents,
        _profile(),
        source_context="mixed",
        batch_size=4,
    )

    assert scores == [0.8, 0.8, 0.8, 0.8]
    assert len(llm.calls) == 1
    rows = database.query_prefilter_shadow_audit()
    assert len(rows) == 4
    assert database.prefilter_shadow_audit_counts()["joined"] == 4
    assert sum(int(row["would_filter"]) for row in rows) == 1
    assert {str(row["embedding_status"]) for row in rows} == {
        PREFILTER_OK_STATUS,
        PREFILTER_EXPLORE_EXEMPT_STATUS,
        "content_embedding_error",
    }
    assert all(float(row["llm_score"]) == 0.8 for row in rows)
    assert all(int(row["admission_result"]) == 1 for row in rows)
    assert sum(int(row["explicit_strong_interest"]) for row in rows) == 1
    serialized = json.dumps(rows, ensure_ascii=False)
    for private_value in (
        "BVPRIVATELOW",
        "BVPRIVATEKEEP",
        "低相似候选",
        "私密厨房",
        "异常候选",
    ):
        assert private_value not in serialized


@pytest.mark.asyncio
async def test_shadow_telemetry_failure_keeps_candidate_on_llm_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingAuditDatabase:
        def get_latest_event_id(self) -> int:
            return 0

        def query_events(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def record_prefilter_shadow_decisions(self, _rows: object) -> int:
            raise RuntimeError("synthetic storage failure")

    embedding = _EmbeddingService(
        {
            "纪录片": [1.0, 0.0],
            "低相似候选 私密厨房": [0.1, 0.9949874371],
        }
    )
    llm = _BatchLLM()
    engine = ContentDiscoveryEngine(
        llm_service=llm,
        database=_FailingAuditDatabase(),  # type: ignore[arg-type]
        embedding_service=embedding,
        eval_prefilter_mode="shadow",
    )

    with caplog.at_level("WARNING", logger="openbiliclaw.discovery.engine"):
        scores = await engine.evaluate_content_batch(
            [
                DiscoveredContent(
                    bvid="BVFAILOPEN",
                    title="低相似候选",
                    description="私密厨房",
                    source_strategy="trending",
                )
            ],
            _profile(),
        )

    assert scores == [0.8]
    assert len(llm.calls) == 1
    assert any("telemetry insert failed" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_enforce_telemetry_failure_fails_batch_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingAuditDatabase:
        def get_latest_event_id(self) -> int:
            return 0

        def query_events(self, **_kwargs: object) -> list[dict[str, object]]:
            return []

        def record_prefilter_shadow_decisions(self, _rows: object) -> int:
            raise RuntimeError("synthetic storage failure")

    embedding = _EmbeddingService(
        {
            "纪录片": [1.0, 0.0],
            "低相似候选 私密厨房": [0.1, 0.9949874371],
        }
    )
    llm = _BatchLLM()
    engine = ContentDiscoveryEngine(
        llm_service=llm,
        database=_FailingAuditDatabase(),  # type: ignore[arg-type]
        embedding_service=embedding,
        eval_prefilter_mode="enforce",
    )

    with caplog.at_level("WARNING", logger="openbiliclaw.discovery.engine"):
        scores = await engine.evaluate_content_batch(
            [
                DiscoveredContent(
                    bvid="BVFAILOPENENFORCE",
                    title="低相似候选",
                    description="私密厨房",
                    source_strategy="trending",
                )
            ],
            _profile(),
        )

    assert scores == [0.8]
    assert len(llm.calls) == 1
    assert any(
        "decision telemetry unavailable; failing batch open" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_enforce_required_interest_embedding_failure_fails_open() -> None:
    embedding = _EmbeddingService({}, failures={"纪录片"})
    llm = _BatchLLM()
    engine = ContentDiscoveryEngine(
        llm_service=llm,
        embedding_service=embedding,
        eval_prefilter_mode="enforce",
    )

    scores = await engine.evaluate_content_batch(
        [
            DiscoveredContent(
                bvid="BVINTERESTFAIL",
                title="低相似候选",
                description="私密厨房",
                source_strategy="trending",
            )
        ],
        _profile(),
    )

    assert scores == [0.8]
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_single_provider_failure_leaves_shadow_decision_unjoined(
    tmp_path: Path,
) -> None:
    class _FailingLLM:
        async def complete_structured_task(self, **_kwargs: object) -> object:
            raise RuntimeError("synthetic provider failure")

    database = Database(tmp_path / "single-provider-failure.db")
    database.initialize()
    engine = ContentDiscoveryEngine(
        llm_service=_FailingLLM(),  # type: ignore[arg-type]
        database=database,
        embedding_service=_EmbeddingService(
            {
                "纪录片": [1.0, 0.0],
                "低相似候选 私密厨房": [0.1, 0.9949874371],
            }
        ),
        eval_prefilter_mode="shadow",
    )

    score = await engine.evaluate_content(
        DiscoveredContent(
            bvid="BVSINGLEPROVIDERFAIL",
            title="低相似候选",
            description="私密厨房",
            source_strategy="trending",
        ),
        _profile(),
    )

    assert score == 0.0
    assert database.prefilter_shadow_audit_counts() == {
        "total": 1,
        "joined": 0,
        "incomplete": 1,
    }
    assert database.query_prefilter_shadow_audit()[0]["llm_score"] is None


@pytest.mark.asyncio
async def test_batch_parse_failure_joins_only_production_valid_scores(
    tmp_path: Path,
) -> None:
    class _PartialBatchLLM:
        async def complete_structured_task(self, **kwargs: object) -> object:
            user_input = str(kwargs["user_input"])
            raw_batch = user_input.split("<content_batch>", 1)[1].split("</content_batch>", 1)[0]
            envelope = json.loads(raw_batch.strip())
            assert isinstance(envelope, dict)
            items = envelope["items"]
            assert isinstance(items, list)
            return SimpleNamespace(
                content=json.dumps(
                    [
                        {
                            "id": str(item["id"]),
                            "score": 0.8,
                            "reason": "ok",
                            "topic_group": "test",
                            "style_key": "deep_focus",
                            "franchise_key": "",
                        }
                        for item in items
                        if item.get("title") == "有效候选"
                    ]
                )
            )

    database = Database(tmp_path / "batch-parse-failure.db")
    database.initialize()
    engine = ContentDiscoveryEngine(
        llm_service=_PartialBatchLLM(),  # type: ignore[arg-type]
        database=database,
        embedding_service=_EmbeddingService(
            {
                "纪录片": [1.0, 0.0],
                "有效候选 纪录片解析": [1.0, 0.0],
                "无效候选 私密厨房": [0.1, 0.9949874371],
            }
        ),
        eval_prefilter_mode="shadow",
    )

    scores = await engine.evaluate_content_batch(
        [
            DiscoveredContent(
                bvid="BVVALIDRAW",
                title="有效候选",
                description="纪录片解析",
                source_strategy="trending",
            ),
            DiscoveredContent(
                bvid="BVINVALIDRAW",
                title="无效候选",
                description="私密厨房",
                source_strategy="trending",
            ),
        ],
        _profile(),
        batch_size=2,
    )

    assert scores == [0.8, 0.0]
    assert database.prefilter_shadow_audit_counts() == {
        "total": 2,
        "joined": 1,
        "incomplete": 1,
    }
    rows = database.query_prefilter_shadow_audit()
    assert sorted(row["llm_score"] for row in rows if row["llm_score"] is not None) == [0.8]


@pytest.mark.asyncio
async def test_missing_embedding_service_evidence_persists_joins_and_reaches_gate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "missing-embedding-service.db")
    database.initialize()
    engine = ContentDiscoveryEngine(
        llm_service=_BatchLLM(),
        database=database,
        eval_prefilter_mode="shadow",
    )

    scores = await engine.evaluate_content_batch(
        [
            DiscoveredContent(
                bvid="BVMISSINGSERVICE",
                title="候选内容",
                source_strategy="trending",
            )
        ],
        _profile(),
    )

    assert scores == [0.8]
    assert database.prefilter_shadow_audit_counts() == {
        "total": 1,
        "joined": 1,
        "incomplete": 0,
    }
    row = database.query_prefilter_shadow_audit()[0]
    assert row["embedding_status"] == "embedding_service_missing"
    assert row["fail_open"] == 1
    assert row["would_filter"] == 0
    assert len(str(row["embedding_namespace"])) == 24
    report = evaluate_prefilter_gate([row])
    assert report.degraded_embedding_cases == 1
    assert report.fail_open_cases == 1
    assert "embedding_fail_open_evidence_missing" not in report.reasons


@pytest.mark.asyncio
async def test_empty_profile_evidence_persists_as_degraded_fail_open(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "empty-profile.db")
    database.initialize()
    engine = ContentDiscoveryEngine(
        llm_service=_BatchLLM(),
        database=database,
        embedding_service=_EmbeddingService({}),
        eval_prefilter_mode="shadow",
    )

    scores = await engine.evaluate_content_batch(
        [
            DiscoveredContent(
                bvid="BVEMPTYPROFILE",
                title="候选内容",
                source_strategy="trending",
            )
        ],
        SoulProfile(),
    )

    assert scores == [0.8]
    row = database.query_prefilter_shadow_audit()[0]
    assert row["embedding_status"] == PREFILTER_NO_INTERESTS_STATUS
    assert row["fail_open"] == 1
    assert row["would_filter"] == 0
    report = evaluate_prefilter_gate([row])
    assert report.degraded_embedding_cases == 1
    assert report.fail_open_cases == 1
