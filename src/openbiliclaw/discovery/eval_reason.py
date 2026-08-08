"""Runtime normalization for LLM content-evaluation reasons."""

from __future__ import annotations

from typing import Final

EVALUATION_REASON_SCORE_FLOOR: Final = 0.5
EVALUATION_REASON_MAX_CODEPOINTS: Final = 30


def normalize_evaluation_reason(score: float, raw_reason: object) -> str | None:
    """Return a persisted evaluation reason, or ``None`` for malformed input.

    ``None`` is the accepted representation for a missing reason and normalizes
    to an empty string. Every other non-string value is malformed, regardless
    of score, so callers can fail closed instead of coercing it with ``str`` or
    ``repr``. Python string slicing counts Unicode code points, matching the
    runtime contract rather than encoded bytes.
    """

    if raw_reason is None:
        return ""
    if not isinstance(raw_reason, str):
        return None
    if score < EVALUATION_REASON_SCORE_FLOOR:
        return ""
    return raw_reason.strip()[:EVALUATION_REASON_MAX_CODEPOINTS]
