"""Tests for discovery evaluation-reason normalization."""

from __future__ import annotations

import pytest

from openbiliclaw.discovery.eval_reason import normalize_evaluation_reason


@pytest.mark.parametrize("score", [0.0, 0.49, 0.499999])
def test_normalize_evaluation_reason_forces_low_score_reason_empty(score: float) -> None:
    assert normalize_evaluation_reason(score, "  模型不该为低分项保留这段理由  ") == ""


def test_normalize_evaluation_reason_keeps_exact_floor_reason() -> None:
    normalized = normalize_evaluation_reason(0.5, "  边界分数仍保留内部诊断  ")

    assert normalized == "边界分数仍保留内部诊断"


@pytest.mark.parametrize("score", [0.2, 0.5, 1.0])
@pytest.mark.parametrize("raw_reason", [None, "", " \n\t "])
def test_normalize_evaluation_reason_accepts_missing_or_empty_reason(
    score: float,
    raw_reason: str | None,
) -> None:
    assert normalize_evaluation_reason(score, raw_reason) == ""


def test_normalize_evaluation_reason_strips_before_truncating() -> None:
    assert normalize_evaluation_reason(0.8, f"  {'中' * 31}  ") == "中" * 30


def test_normalize_evaluation_reason_counts_unicode_code_points() -> None:
    raw_reason = f"{'中' * 29}🧠🚀"

    normalized = normalize_evaluation_reason(0.8, raw_reason)

    assert normalized == f"{'中' * 29}🧠"
    assert len(normalized) == 30


@pytest.mark.parametrize(
    "raw_reason",
    [False, 1, 1.5, [], {}, object()],
)
@pytest.mark.parametrize("score", [0.2, 0.5, 0.9])
def test_normalize_evaluation_reason_rejects_non_string_reason(
    score: float,
    raw_reason: object,
) -> None:
    assert normalize_evaluation_reason(score, raw_reason) is None
