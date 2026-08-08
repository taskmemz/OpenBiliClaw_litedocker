"""Deterministic, lossless-at-rest event projections for cognition prompts.

The event ledger remains the source of truth.  This module only builds a
request-local LLM view: semantic evidence is retained, transport/projection
bookkeeping is removed, and serialized metadata is parsed exactly once.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

CognitionInputView = Literal["legacy", "compact-v1"]

_COGNITION_INPUT_VIEWS = frozenset({"legacy", "compact-v1"})

# This is deliberately an exact-key denylist.  Unknown metadata is evidence,
# not garbage, and therefore survives unless a field is known to be internal
# transport/projection state.  Do not turn this into a prefix/substring filter.
_INTERNAL_EVENT_FIELDS = frozenset(
    {
        "browser_context",
        "browser_state",
        "classification_reason",
        "classifier_reason",
        "dom_snapshot",
        "event_namespace",
        "idempotency_key",
        "ingest_idempotency_key",
        "ingest_key",
        "page_html",
        "projection_namespace",
        "projection_owner",
        "projection_ownership",
        "raw_dom",
        "raw_html",
        "satisfaction_reason",
    }
)

_URL_FIELDS = frozenset({"canonical_url", "content_url", "page_url", "url"})
_CONTENT_IDENTITY_FIELDS = frozenset(
    {
        "aid",
        "article_id",
        "bvid",
        "content_id",
        "item_id",
        "note_id",
        "post_id",
        "tweet_id",
        "video_id",
    }
)
_HUMAN_CONTEXT_FIELDS = frozenset(
    {
        "assistant_message",
        "body_text",
        "comment_text",
        "context",
        "dialogue",
        "feedback_note",
        "message",
        "query",
        "search_query",
        "text",
        "title",
        "user_message",
    }
)

# Malformed metadata must remain visible instead of disappearing, but a broken
# producer must not make one event unbounded.  512 characters preserves a
# useful diagnostic/semantic prefix while staying below the existing 600-char
# compact-context retry cap.  Revisit after any provider/model change.
MALFORMED_METADATA_MAX_CHARS = 512


def normalize_cognition_input_view(value: str) -> CognitionInputView:
    """Validate and normalize the public cognition prompt-view seam."""

    normalized = str(value or "legacy").strip().lower()
    if normalized not in _COGNITION_INPUT_VIEWS:
        allowed = ", ".join(sorted(_COGNITION_INPUT_VIEWS))
        raise ValueError(f"cognition prompt view must be one of: {allowed}")
    return cast("CognitionInputView", normalized)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, (list, tuple, dict)) and not value


def _detached(value: object) -> object:
    """Return a detached value without narrowing unknown semantic shapes."""

    return copy.deepcopy(value)


def _json_characters(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _project_metadata_value(value: object) -> tuple[object, int]:
    """Recursively remove exact internal keys while retaining unknown data."""

    if isinstance(value, Mapping):
        projected: dict[str, object] = {}
        removed = 0
        for raw_key in sorted(value, key=str):
            key = str(raw_key)
            item = value[raw_key]
            if key in _INTERNAL_EVENT_FIELDS or _is_empty(item):
                removed += 1
                continue
            detached, nested_removed = _project_metadata_value(item)
            removed += nested_removed
            if not _is_empty(detached):
                projected[key] = detached
        return projected, removed
    if isinstance(value, list | tuple):
        projected_items: list[object] = []
        removed = 0
        for item in value:
            detached, nested_removed = _project_metadata_value(item)
            removed += nested_removed
            if not _is_empty(detached):
                projected_items.append(detached)
        return projected_items, removed
    return _detached(value), 0


def _project_metadata(value: object) -> tuple[object | None, int, bool]:
    """Return ``(metadata, removed_fields, malformed)`` for one event."""

    if value is None:
        return None, 0, False

    original_text: str | None = None
    raw: object = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, 1, False
        original_text = text
        try:
            raw = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return text[:MALFORMED_METADATA_MAX_CHARS], 0, True

    if not isinstance(raw, Mapping):
        if _is_empty(raw):
            return None, 1, False
        text = original_text or json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        text = text.strip()
        if not text:
            return None, 1, False
        return text[:MALFORMED_METADATA_MAX_CHARS], 0, True

    projected, removed = _project_metadata_value(raw)
    assert isinstance(projected, dict)
    return (projected or None), removed, False


def _has_content_identity_or_context(
    event: Mapping[str, object],
    metadata: object | None,
) -> bool:
    for key in _HUMAN_CONTEXT_FIELDS:
        if not _is_empty(event.get(key)):
            return True
    if not isinstance(metadata, Mapping):
        return False
    return any(not _is_empty(metadata.get(key)) for key in _CONTENT_IDENTITY_FIELDS)


def _remove_redundant_urls(
    projected: dict[str, object],
    metadata: object | None,
) -> tuple[object | None, int]:
    """Keep a URL only when no other useful identity/context is available."""

    if not _has_content_identity_or_context(projected, metadata):
        return metadata, 0

    removed = 0
    if "url" in projected:
        projected.pop("url", None)
        removed += 1
    if isinstance(metadata, Mapping):
        detached = dict(metadata)
        for key in _URL_FIELDS:
            if key in detached:
                detached.pop(key, None)
                removed += 1
        return (detached or None), removed
    return metadata, removed


def _project_event(event: Mapping[str, object]) -> tuple[dict[str, object], int, bool]:
    projected: dict[str, object] = {}
    removed = 0

    event_type = event.get("event_type") or event.get("type")
    if not _is_empty(event_type):
        projected["event_type"] = _detached(event_type)

    for raw_key in sorted(event, key=str):
        key = str(raw_key)
        if key in {"event_type", "metadata", "type", "url"}:
            continue
        item = event[raw_key]
        if key in _INTERNAL_EVENT_FIELDS or _is_empty(item):
            removed += 1
            continue
        projected[key] = _detached(item)

    raw_url = event.get("url")
    if not _is_empty(raw_url):
        projected["url"] = _detached(raw_url)

    metadata, metadata_removed, malformed = _project_metadata(event.get("metadata"))
    removed += metadata_removed
    metadata, url_removed = _remove_redundant_urls(projected, metadata)
    removed += url_removed
    if metadata is not None:
        projected["metadata"] = metadata
    return projected, removed, malformed


@dataclass(frozen=True)
class CognitionEventViewV1:
    """One deterministic compact projection plus privacy-safe size statistics."""

    events: tuple[dict[str, object], ...]
    removed_field_count: int
    malformed_metadata_count: int
    source_characters: int
    rendered_characters: int

    def as_list(self) -> list[dict[str, object]]:
        """Return a detached JSON-ready list for a prompt builder."""

        return [cast("dict[str, object]", _detached(event)) for event in self.events]

    @classmethod
    def from_events(cls, events: Sequence[Mapping[str, object]]) -> CognitionEventViewV1:
        """Project events without mutating the caller-owned ledger rows."""

        projected: list[dict[str, object]] = []
        removed = 0
        malformed = 0
        for event in events:
            item, removed_count, is_malformed = _project_event(event)
            projected.append(item)
            removed += removed_count
            malformed += int(is_malformed)
        return cls(
            events=tuple(projected),
            removed_field_count=removed,
            malformed_metadata_count=malformed,
            source_characters=_json_characters(list(events)),
            rendered_characters=_json_characters(projected),
        )


def build_cognition_event_view_v1(
    events: Sequence[Mapping[str, object]],
) -> CognitionEventViewV1:
    """Build the named compact event view used at cognition prompt boundaries."""

    return CognitionEventViewV1.from_events(events)
