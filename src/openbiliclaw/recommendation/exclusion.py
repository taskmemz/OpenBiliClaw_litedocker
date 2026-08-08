"""Fresh disliked-topic filtering at recommendation output boundaries.

Discovery may keep fetching broad supply.  This module owns the separate
product invariant: once a dislike is durably visible, cached/history rows must
be rechecked before they are returned to a user-facing recommendation surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import TypeVar

_Row = TypeVar("_Row", bound=Mapping[str, object])


def normalize_disliked_topics(topics: Sequence[str]) -> list[str]:
    """Return stable, de-duplicated terms using the existing serve semantics."""

    result: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        term = normalize_dislike_match_text(topic)
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        result.append(term)
    return result


def disliked_topics_digest(topics: Sequence[str]) -> str:
    """Hash a normalized effective-dislike snapshot for cache invalidation."""

    payload = json.dumps(
        sorted(normalize_disliked_topics(topics)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def filter_recommendation_rows(
    rows: Sequence[_Row],
    disliked_topics: Sequence[str],
    *,
    restore_on_total_fuzzy_match: bool = True,
) -> list[_Row]:
    """Filter cached/history recommendation rows with current dislikes.

    This mirrors ``RecommendationEngine``'s existing serve-time policy.  Exact
    structured-topic matches are always excluded.  Fuzzy title/body/tag
    matches are also excluded, except that the existing starvation safeguard
    may restore exact-safe rows when a broad phrase fuzzily matches the entire
    window.  Single-item push surfaces disable that safeguard.
    """

    items = list(rows)
    terms = normalize_disliked_topics(disliked_topics)
    if not items or not terms:
        return items
    filtered = [row for row in items if not recommendation_row_matches(row, terms)]
    if filtered or not restore_on_total_fuzzy_match:
        return filtered
    return [row for row in items if not recommendation_row_matches_exact(row, terms)]


def recommendation_row_matches(row: Mapping[str, object], disliked_terms: Sequence[str]) -> bool:
    """Return whether a recommendation row matches exact or fuzzy dislike fields."""

    if recommendation_row_matches_exact(row, disliked_terms):
        return True
    fields = [
        normalize_dislike_match_text(row.get("title")),
        normalize_dislike_match_text(row.get("topic")),
        normalize_dislike_match_text(row.get("topic_label")),
        normalize_dislike_match_text(row.get("description")),
        normalize_dislike_match_text(row.get("body_text")),
        normalize_dislike_match_text(row.get("up_name")),
        normalize_dislike_match_text(row.get("tags")),
    ]
    return any(term in field for term in disliked_terms for field in fields if field)


def recommendation_row_matches_exact(
    row: Mapping[str, object],
    disliked_terms: Sequence[str],
) -> bool:
    """Return whether a structured topic field exactly matches a dislike."""

    exact_fields = {
        normalize_dislike_match_text(row.get("topic")),
        normalize_dislike_match_text(row.get("topic_label")),
        normalize_dislike_match_text(row.get("topic_key")),
        normalize_dislike_match_text(row.get("topic_group")),
        normalize_dislike_match_text(row.get("pool_topic_label")),
    }
    exact_fields.discard("")
    return any(term in exact_fields for term in disliked_terms)


def normalize_dislike_match_text(value: object) -> str:
    """Match the established serve-time lowercase/whitespace normalization."""

    text = str(value or "").strip().lower()
    return re.sub(r"\s+", "", text) if text else ""
