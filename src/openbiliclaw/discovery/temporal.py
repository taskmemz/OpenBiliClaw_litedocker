"""Temporal-semantics contract for content evaluation.

The evaluation model classifies *why* a candidate's value may expire.  This
module deliberately contains no ranking policy: it only validates the stable
wire values that discovery can persist for downstream ranking.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

TEMPORAL_POLICY_VERSION = "v1"
TEMPORAL_CLASSES = frozenset(
    {
        "breaking",
        "current",
        "versioned",
        "evergreen",
        "historical",
        "unknown",
    }
)


@dataclass(frozen=True)
class TemporalEvaluation:
    """Validated temporal metadata returned by the evaluation agent."""

    temporal_class: str = "unknown"
    temporal_confidence: float = 0.0
    temporal_reason: str = ""
    temporal_policy_version: str = TEMPORAL_POLICY_VERSION


def normalize_temporal_class(value: object) -> str:
    """Return a supported temporal class, or ``unknown`` for invalid input."""

    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    return normalized if normalized in TEMPORAL_CLASSES else "unknown"


def normalize_temporal_confidence(value: object) -> float:
    """Return a finite confidence in ``[0, 1]``, or zero when invalid."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    try:
        confidence = float(value)
    except (OverflowError, ValueError):
        return 0.0
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return 0.0
    return confidence


def parse_temporal_evaluation(payload: Mapping[str, object]) -> TemporalEvaluation:
    """Validate the three model-owned temporal fields as one atomic result.

    Missing or malformed temporal metadata must never invalidate an otherwise
    usable relevance score.  Instead, the whole temporal result fails neutral:
    ``unknown`` with zero confidence and no reason.  The policy version is
    code-owned and therefore never read from model output.
    """

    required = {"temporal_class", "temporal_confidence", "temporal_reason"}
    if not required.issubset(payload):
        missing = ",".join(sorted(required.difference(payload)))
        logger.warning(
            "Temporal evaluation metadata missing fields (%s); using unknown",
            missing,
        )
        return TemporalEvaluation()

    raw_class = payload["temporal_class"]
    raw_confidence = payload["temporal_confidence"]
    raw_reason = payload["temporal_reason"]
    if not isinstance(raw_class, str) or not isinstance(raw_reason, str):
        logger.warning("Temporal evaluation class/reason has invalid type; using unknown")
        return TemporalEvaluation()
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, int | float):
        logger.warning("Temporal evaluation confidence has invalid type; using unknown")
        return TemporalEvaluation()

    temporal_class = normalize_temporal_class(raw_class)
    try:
        raw_confidence_float = float(raw_confidence)
    except (OverflowError, ValueError):
        logger.warning("Temporal evaluation confidence is not representable; using unknown")
        return TemporalEvaluation()
    confidence = normalize_temporal_confidence(raw_confidence)
    confidence_is_valid = math.isfinite(raw_confidence_float) and 0.0 <= raw_confidence_float <= 1.0
    reason = raw_reason.strip()
    explicit_unknown = raw_class.strip().lower() == "unknown"
    if explicit_unknown and confidence_is_valid and raw_confidence_float == 0.0 and not reason:
        return TemporalEvaluation()
    if temporal_class == "unknown":
        logger.warning("Temporal evaluation class is invalid or non-neutral unknown; using unknown")
        return TemporalEvaluation()
    if not confidence_is_valid:
        logger.warning("Temporal evaluation confidence is outside [0, 1]; using unknown")
        return TemporalEvaluation()
    if not reason:
        logger.warning("Temporal evaluation reason is empty for a classified item; using unknown")
        return TemporalEvaluation()

    return TemporalEvaluation(
        temporal_class=temporal_class,
        temporal_confidence=confidence,
        temporal_reason=reason,
    )
