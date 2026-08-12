"""V2EX topic normalization into the shared discovery content contract."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from openbiliclaw.discovery.engine import DiscoveredContent

_MAX_TITLE_CHARS = 300
_MAX_BODY_CHARS = 6000


def v2ex_topic_to_content(
    row: Mapping[str, Any] | object,
    *,
    strategy: str,
    source_keyword_id: int | None = None,
    node_name: str = "",
    node_title: str = "",
    max_body_chars: int = _MAX_BODY_CHARS,
    max_reply_digest_chars: int = 1200,
) -> DiscoveredContent | None:
    """Defensively normalize a public V2EX Topic row.

    Replies remain engagement metadata on the Topic; they are never emitted as
    independent recommendation items.
    """

    if not isinstance(row, Mapping):
        return None
    topic_id = _topic_id(row.get("id")) or _topic_id(row.get("url"))
    title = _clean(row.get("title"))[:_MAX_TITLE_CHARS]
    if not topic_id or not title or bool(row.get("deleted")):
        return None

    node = _mapping(row.get("node"))
    resolved_node = _clean(node.get("name")) or _clean(node_name)
    resolved_node_title = _clean(node.get("title")) or _clean(node_title)
    member = _mapping(row.get("member"))
    author = _clean(member.get("username")) or _clean(row.get("author_name"))
    body = _clean(
        row.get("content")
        or row.get("content_rendered")
        or row.get("content_text")
        or row.get("content_html")
        or row.get("description")
    )[: max(100, min(20_000, int(max_body_chars)))]
    discussion_digest = _clean(row.get("discussion_digest"))[
        : max(100, min(10_000, int(max_reply_digest_chars)))
    ]
    url = _canonical_topic_url(topic_id, row.get("url"))
    published_at = _timestamp_iso(row.get("created") or row.get("date_published"))
    tags = _dedupe_nonempty((resolved_node, resolved_node_title))
    reply_count = _safe_int(row.get("replies"), row.get("reply_count"))
    return DiscoveredContent(
        bvid=topic_id,
        content_id=topic_id,
        content_url=url,
        source_platform="v2ex",
        content_type="topic",
        source_strategy=strategy,
        source_keyword_id=source_keyword_id,
        title=title,
        up_name=author,
        author_name=author,
        body_text=body,
        description=discussion_digest or body[:500],
        tags=tags,
        published_at=published_at,
        reply_count=reply_count,
        # V2EX does not expose a stable cross-path view/like/favorite metric.
        view_count=0,
        like_count=0,
        favorite_count=0,
        comment_count=0,
    )


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean(value: object) -> str:
    raw = html.unescape(str(value or ""))
    raw = re.sub(r"<\s*br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _topic_id(value: object) -> str:
    raw = str(value or "").strip()
    if raw.isdigit():
        return raw
    parsed = urlparse(raw)
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "v2ex.com" or hostname.endswith(".v2ex.com")
    ):
        return ""
    match = re.search(r"/t/(\d+)", parsed.path)
    return match.group(1) if match else ""


def _canonical_topic_url(topic_id: str, value: object) -> str:
    del value
    return f"https://www.v2ex.com/t/{topic_id}"


def _timestamp_iso(value: object) -> str:
    try:
        timestamp = int(float(str(value or "0")))
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _safe_int(*values: object) -> int:
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        try:
            return max(0, int(float(str(value))))
        except (TypeError, ValueError):
            continue
    return 0


def _dedupe_nonempty(values: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
