from __future__ import annotations

import logging
import math

import pytest

from openbiliclaw.discovery.temporal import (
    TEMPORAL_POLICY_VERSION,
    TemporalEvaluation,
    normalize_temporal_class,
    normalize_temporal_confidence,
    parse_temporal_evaluation,
)


@pytest.mark.parametrize(
    "value",
    ["breaking", "current", "versioned", "evergreen", "historical", "unknown"],
)
def test_normalize_temporal_class_accepts_contract_values(value: str) -> None:
    assert normalize_temporal_class(value.upper()) == value


@pytest.mark.parametrize("value", [None, 3, "recent", "", "breaking-news"])
def test_normalize_temporal_class_fails_neutral(value: object) -> None:
    assert normalize_temporal_class(value) == "unknown"


@pytest.mark.parametrize("value", [True, "0.8", -0.1, 1.1, math.inf, math.nan, None])
def test_normalize_temporal_confidence_fails_neutral(value: object) -> None:
    assert normalize_temporal_confidence(value) == 0.0


def test_parse_temporal_evaluation_accepts_complete_valid_metadata() -> None:
    parsed = parse_temporal_evaluation(
        {
            "temporal_class": " versioned ",
            "temporal_confidence": 0.86,
            "temporal_reason": " 内容依赖具体产品版本 ",
            "temporal_policy_version": "model-must-not-own-this",
        }
    )

    assert parsed == TemporalEvaluation(
        temporal_class="versioned",
        temporal_confidence=0.86,
        temporal_reason="内容依赖具体产品版本",
        temporal_policy_version=TEMPORAL_POLICY_VERSION,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"temporal_class": "current", "temporal_confidence": 0.8},
        {
            "temporal_class": "recent",
            "temporal_confidence": 0.8,
            "temporal_reason": "invalid class",
        },
        {
            "temporal_class": "current",
            "temporal_confidence": "high",
            "temporal_reason": "invalid confidence",
        },
        {
            "temporal_class": "current",
            "temporal_confidence": 0.8,
            "temporal_reason": ["invalid reason"],
        },
        {
            "temporal_class": "current",
            "temporal_confidence": 0.8,
            "temporal_reason": "",
        },
        {
            "temporal_class": "unknown",
            "temporal_confidence": 0.9,
            "temporal_reason": "still neutral",
        },
    ],
)
def test_parse_temporal_evaluation_atomically_fails_neutral(
    payload: dict[str, object],
) -> None:
    assert parse_temporal_evaluation(payload) == TemporalEvaluation()


def test_parse_temporal_evaluation_warns_on_coercion_but_not_explicit_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="openbiliclaw.discovery.temporal")

    assert (
        parse_temporal_evaluation(
            {
                "temporal_class": "unknown",
                "temporal_confidence": 0.0,
                "temporal_reason": "",
            }
        )
        == TemporalEvaluation()
    )
    assert caplog.records == []

    assert (
        parse_temporal_evaluation(
            {
                "temporal_class": "current",
                "temporal_confidence": "high",
                "temporal_reason": "依赖近期语境",
            }
        )
        == TemporalEvaluation()
    )
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "using unknown" in caplog.records[0].getMessage()
