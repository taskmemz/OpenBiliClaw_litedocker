"""Tests for the deterministic evaluator row-wire-v1 codec."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from openbiliclaw.discovery.eval_payload import build_canonical_evaluation_batch
from openbiliclaw.llm.evaluation_wire import (
    ROW_WIRE_V1_COLUMNS,
    ROW_WIRE_V1_HEADER,
    EvaluationWireError,
    decode_evaluation_row_wire,
    encode_evaluation_row_wire,
    validate_canonical_evaluation_envelope,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def _minimal_envelope() -> dict[str, object]:
    return {
        "defaults": {
            "mode": "normal",
            "source_platform": "twitter",
            "content_type": "tweet",
        },
        "items": [{"id": "0", "title": "标题", "author": "作者"}],
    }


def _replace_row_cell(wire: str, field: str, raw_value: str) -> str:
    lines = wire.split("\n")
    cells = lines[3].split("\t")
    cells[ROW_WIRE_V1_COLUMNS.index(field) + 1] = raw_value
    lines[3] = "\t".join(cells)
    return "\n".join(lines)


def test_row_wire_v1_has_fixed_protocol_columns_and_deterministic_order() -> None:
    first = {
        "items": [
            {
                "author": "作者",
                "title": "标题",
                "id": "0",
                "view_count": 12,
            }
        ],
        "defaults": {
            "source_platform": "twitter",
            "mode": "normal",
            "content_type": "thread",
        },
    }
    reordered = {
        "defaults": {
            "content_type": "thread",
            "mode": "normal",
            "source_platform": "twitter",
        },
        "items": [
            {
                "view_count": 12,
                "id": "0",
                "title": "标题",
                "author": "作者",
            }
        ],
    }

    first_wire = encode_evaluation_row_wire(first)
    reordered_wire = encode_evaluation_row_wire(reordered)
    lines = first_wire.split("\n")

    assert first_wire == reordered_wire
    assert lines[0] == ROW_WIRE_V1_HEADER
    assert lines[1] == "defaults\tcontent_type=thread\tmode=normal\tsource_platform=twitter"
    assert lines[2] == "\t".join(("columns", *ROW_WIRE_V1_COLUMNS))
    assert len(lines[3].split("\t")) == len(ROW_WIRE_V1_COLUMNS) + 1
    assert decode_evaluation_row_wire(first_wire) == first


def test_row_wire_round_trip_preserves_unicode_tabs_crlf_backslashes_and_pipes() -> None:
    envelope = {
        "defaults": {
            "mode": "normal",
            "source_platform": "twitter",
            "content_type": "thread",
        },
        "items": [
            {
                "id": "0",
                "title": "雪\t山\\trail",
                "author": "",
                "body_text": "第一行\r\n第二行\\n literal\tend | pipe",
                "description": "emoji 🐾 与 = 号",
                "tags": ["中文", "tab\tvalue", "crlf\r\nvalue", "slash\\value", ""],
                "related_interests": ["系统 设计", "雪"],
                "cover_image_ref": "cover:0",
            }
        ],
    }

    wire = encode_evaluation_row_wire(envelope)

    assert "雪\\t山\\\\trail" in wire
    assert "第一行\\r\\n第二行\\\\n literal\\tend | pipe" in wire
    assert "🐾" in wire
    assert "\r" not in wire
    assert decode_evaluation_row_wire(wire) == envelope


def test_row_wire_round_trip_preserves_empty_cells_lists_and_numeric_types() -> None:
    envelope = {
        "defaults": {
            "mode": "normal",
            "source_platform": "twitter",
            "content_type": "tweet",
        },
        "items": [
            {
                "id": "0",
                "title": "",
                "author": "",
                "tags": [""],
                "related_interests": ["兴趣"],
                "duration": 1,
                "view_count": 42,
                "rating_score": 8.0,
                "rating_count": 3,
            }
        ],
    }

    wire = encode_evaluation_row_wire(envelope)
    row = wire.split("\n")[3].split("\t")

    assert wire.split("\n")[1] == (
        "defaults\tcontent_type=tweet\tmode=normal\tsource_platform=twitter"
    )
    assert row[ROW_WIRE_V1_COLUMNS.index("source_platform") + 1] == ""
    assert row[ROW_WIRE_V1_COLUMNS.index("title") + 1] == ""
    assert row[ROW_WIRE_V1_COLUMNS.index("tags") + 1] == '[""]'
    assert row[ROW_WIRE_V1_COLUMNS.index("rating_score") + 1] == "8.0"
    assert decode_evaluation_row_wire(wire) == envelope


def test_row_wire_exactly_round_trips_the_shared_canonical_builder_payload() -> None:
    batch = build_canonical_evaluation_batch(
        [
            {
                "source_platform": "bilibili",
                "content_type": "video",
                "title": "雪山\t路线",
                "author_name": "作者 A",
                "body_text": "第一行\r\n第二行\\tail",
                "duration": 120,
                "tags": ["户外", "雪"],
                "cover_image_ref": "prepared-image",
            },
            {
                "source_platform": "twitter",
                "content_type": "thread",
                "source_context": "explore",
                "title": "Thread B",
                "up_name": "作者 B",
                "view_count": 42,
                "rating_score": 8.5,
                "related_interests": ["系统设计"],
            },
        ]
    )
    canonical = batch.as_payload()

    assert validate_canonical_evaluation_envelope(canonical) == canonical
    assert decode_evaluation_row_wire(encode_evaluation_row_wire(canonical)) == canonical
    assert batch.items[0]["cover_image_ref"] == "cover:0"
    assert batch.items[1]["mode"] == "explore"


def test_canonical_envelope_accepts_published_at_but_row_wire_v1_rejects_it() -> None:
    envelope = _minimal_envelope()
    items = envelope["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["published_at"] = "2026-08-04T08:00:00Z"

    assert validate_canonical_evaluation_envelope(envelope) == envelope
    with pytest.raises(EvaluationWireError, match="does not support published_at"):
        encode_evaluation_row_wire(envelope)


def test_row_wire_round_trip_supports_an_empty_batch() -> None:
    envelope: dict[str, object] = {
        "defaults": {"mode": "normal"},
        "items": [],
    }

    wire = encode_evaluation_row_wire(envelope)

    assert len(wire.split("\n")) == 3
    assert decode_evaluation_row_wire(wire) == envelope


def test_row_wire_positive_integer_rating_does_not_overflow_float_validation() -> None:
    envelope = _minimal_envelope()
    items = envelope["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["rating_score"] = 10**400

    wire = encode_evaluation_row_wire(envelope)

    assert decode_evaluation_row_wire(wire) == envelope


@pytest.mark.parametrize("raw_value", [r"bad\q", "dangling\\", r"unicode\u96ea"])
def test_row_wire_decoder_rejects_unknown_or_dangling_escapes(raw_value: str) -> None:
    wire = _replace_row_cell(encode_evaluation_row_wire(_minimal_envelope()), "title", raw_value)

    with pytest.raises(EvaluationWireError, match="escape"):
        decode_evaluation_row_wire(wire)


@pytest.mark.parametrize("width_change", ["short", "long"])
def test_row_wire_decoder_rejects_wrong_row_width(width_change: str) -> None:
    lines = encode_evaluation_row_wire(_minimal_envelope()).split("\n")
    cells = lines[3].split("\t")
    if width_change == "short":
        cells.pop()
    else:
        cells.append("")
    lines[3] = "\t".join(cells)

    with pytest.raises(EvaluationWireError, match="width or prefix"):
        decode_evaluation_row_wire("\n".join(lines))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda lines: ["ROW-WIRE-V2", *lines[1:]],
        lambda lines: [lines[0], "default", *lines[2:]],
        lambda lines: [lines[0], "defaults\tunknown=value", *lines[2:]],
        lambda lines: [
            lines[0],
            "defaults\tsource_platform=twitter\tmode=normal",
            *lines[2:],
        ],
        lambda lines: [lines[0], lines[1], "columns\tid", *lines[3:]],
        lambda lines: [*lines, ""],
    ],
)
def test_row_wire_decoder_rejects_noncanonical_framing(mutate: object) -> None:
    lines = encode_evaluation_row_wire(_minimal_envelope()).split("\n")
    assert callable(mutate)
    malformed = mutate(lines)

    with pytest.raises(EvaluationWireError):
        decode_evaluation_row_wire("\n".join(malformed))


@pytest.mark.parametrize(
    ("field", "raw_value", "expected"),
    [
        ("tags", "not-json", "valid compact JSON"),
        ("tags", "{}", "string list"),
        ("tags", "[1]", "string list"),
        ("tags", "[]", "non-empty string list"),
        ("tags", '["a", "b"]', "canonical"),
        ("duration", "1.0", "positive integer"),
        ("duration", "0", "positive integer"),
        ("duration", "-1", "positive integer"),
        ("rating_score", "NaN", "finite positive number"),
        ("rating_score", "0", "finite positive number"),
    ],
)
def test_row_wire_decoder_rejects_malformed_typed_cells(
    field: str,
    raw_value: str,
    expected: str,
) -> None:
    wire = _replace_row_cell(encode_evaluation_row_wire(_minimal_envelope()), field, raw_value)

    with pytest.raises(EvaluationWireError, match=expected):
        decode_evaluation_row_wire(wire)


@pytest.mark.parametrize("bad_id", ["", "00", "1", "global-id"])
def test_row_wire_decoder_rejects_nonlocal_or_noncontiguous_ids(bad_id: str) -> None:
    wire = _replace_row_cell(encode_evaluation_row_wire(_minimal_envelope()), "id", bad_id)

    with pytest.raises(EvaluationWireError, match="contiguous decimal strings"):
        decode_evaluation_row_wire(wire)


@pytest.mark.parametrize(
    "forbidden_field",
    ["content_id", "bvid", "item_key", "content_url", "cover_url"],
)
def test_row_wire_encoder_rejects_global_identity_and_url_fields(
    forbidden_field: str,
) -> None:
    envelope = _minimal_envelope()
    items = envelope["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item[forbidden_field] = "PRIVATE-GLOBAL-ID"

    with pytest.raises(EvaluationWireError, match="unknown field"):
        encode_evaluation_row_wire(envelope)


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [{}],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": ({"id": "0", "title": "title", "author": "author"},),
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [{"id": "1", "title": "title", "author": "author"}],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "body_text": "",
                }
            ],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "view_count": True,
                }
            ],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
                "unknown": "value",
            },
            "items": [{"id": "0", "title": "title", "author": "author"}],
        },
        {
            "defaults": {"source_platform": "twitter", "content_type": "tweet"},
            "items": [{"id": "0", "title": "title", "author": "author"}],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "",
                "content_type": "tweet",
            },
            "items": [{"id": "0", "title": "title", "author": "author"}],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "",
            },
            "items": [{"id": "0", "title": "title", "author": "author"}],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "source_platform": "twitter",
                }
            ],
        },
        {
            "defaults": {"mode": "normal"},
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "source_platform": "twitter",
                    "content_type": "tweet",
                }
            ],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "mode": "normal",
                }
            ],
        },
        {
            "defaults": {
                "mode": "normal",
                "source_platform": "twitter",
                "content_type": "tweet",
            },
            "items": [
                {
                    "id": "0",
                    "title": "title",
                    "author": "author",
                    "cover_image_ref": "cover:global-id",
                }
            ],
        },
    ],
)
def test_row_wire_encoder_rejects_noncanonical_envelopes(
    envelope: Mapping[str, object],
) -> None:
    with pytest.raises(EvaluationWireError):
        encode_evaluation_row_wire(envelope)


def test_row_wire_rejects_unsupported_ascii_control_characters() -> None:
    envelope = _minimal_envelope()
    items = envelope["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["body_text"] = "before\x1fafter"

    with pytest.raises(EvaluationWireError, match="unsupported ASCII control"):
        encode_evaluation_row_wire(envelope)
