"""Shared discovery admission policy."""

from __future__ import annotations

from typing import Final

DEFAULT_ADMISSION_MIN_SCORE: Final = 0.60
MIN_ADMISSION_MIN_SCORE: Final = 0.50
EXPLORE_ADMISSION_MIN_SCORE: Final = 0.58
EXPLORE_STRATEGY: Final = "explore"


def normalize_admission_score(
    value: object,
    *,
    default: float = DEFAULT_ADMISSION_MIN_SCORE,
) -> float:
    """Return a valid admission score in ``[0.5, 1]`` or ``default``."""
    if isinstance(value, bool):
        return default
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if score < MIN_ADMISSION_MIN_SCORE or score > 1.0:
        return default
    return score


def effective_admission_threshold(
    source_strategy: object,
    admission_min_score: object = DEFAULT_ADMISSION_MIN_SCORE,
    requested_threshold: object | None = None,
) -> float:
    """Return the effective floor for one discovery candidate.

    Exact ``explore`` is the sole relaxed context. A requested threshold may
    raise a source's floor, but it can never lower the policy floor.
    """
    strategy = str(source_strategy or "").strip().lower()
    policy_floor = (
        EXPLORE_ADMISSION_MIN_SCORE
        if strategy == EXPLORE_STRATEGY
        else normalize_admission_score(admission_min_score)
    )
    if requested_threshold is None:
        return policy_floor
    requested = normalize_admission_score(requested_threshold, default=policy_floor)
    return max(policy_floor, requested)
