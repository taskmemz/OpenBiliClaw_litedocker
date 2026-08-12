"""Defensive normalization for anonymous Weibo discovery payloads."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlparse

from openbiliclaw.discovery.engine import DiscoveredContent
from openbiliclaw.published_time import normalize_published_time

WEIBO_SOURCE_MODES = ("search", "hot", "creator")
WEIBO_SOURCE_STRATEGIES = {
    "search": "weibo-search",
    "hot": "weibo-hot",
    "creator": "weibo-creator",
}

_SPACE_RE = re.compile(r"[\t\r\f\v ]+")
_BLANK_LINE_RE = re.compile(r"\n{3,}")
_TOPIC_RE = re.compile(r"#([^#\n]{1,80})#")
_BLOCK_TAGS = frozenset({"br", "div", "li", "p", "section"})


class _HTMLTextExtractor(HTMLParser):
    """Extract visible text without accepting arbitrary markup as data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS - {"br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _stable_id(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value > 0 else ""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate and len(candidate) <= 128 and not any(ord(char) < 32 for char in candidate):
            return candidate
    return ""


def weibo_hot_topic_query(row: Mapping[str, Any]) -> str:
    """Return a real string query from one hot-search row.

    Weibo has used ``word``, ``note`` and ``word_scheme`` for this label over
    time.  Container-shaped drift is deliberately rejected instead of being
    stringified into a Python representation and sent back upstream.
    """

    for key in ("word", "note", "word_scheme"):
        value = row.get(key)
        if not isinstance(value, str):
            continue
        query = value.strip().strip("#").strip()
        if query:
            return query
    return ""


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else 0
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if candidate.isdigit():
            return int(candidate)
    return 0


def _duration_seconds(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0
    else:
        return 0
    if not math.isfinite(number) or number <= 0:
        return 0
    return min(86_400, int(round(number)))


def weibo_html_to_text(value: object) -> str:
    """Convert an H5 ``mblog.text`` fragment into bounded plain text."""

    raw = _text(value)
    if not raw:
        return ""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        # HTMLParser is deliberately best-effort. The fallback strips tags but
        # still decodes entities, so malformed upstream HTML cannot leak markup
        # into the evaluator or permanently poison stored body text.
        text = html.unescape(re.sub(r"<[^>]*>", " ", raw))
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    normalized = "\n".join(line for line in lines if line)
    normalized = _BLANK_LINE_RE.sub("\n\n", normalized).strip()
    # H5 appends a final 「全文」 anchor to truncated inline text. It is a UI
    # control, not part of the author's post.
    return re.sub(r"(?:\s|\.{3}|…)*全文$", "", normalized).strip()


def _published_at(value: object, *, now: datetime | None = None) -> str:
    """Normalize Weibo's timestamp to UTC and reject invalid/future values."""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    normalized = normalize_published_time(value, now=current).published_at
    if not normalized:
        return ""
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return normalized if parsed <= current else ""


def _safe_image_url(value: object) -> str:
    raw = _text(value)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    elif raw.startswith("http://"):
        raw = f"https://{raw[len('http://') :]}"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    return raw


def _cover_url(row: Mapping[str, Any]) -> str:
    page_pic = _mapping(_mapping(row.get("page_info")).get("page_pic"))
    pics = row.get("pics")
    if isinstance(pics, Mapping):
        pic_rows = [pics] if "large" in pics or "url" in pics else list(pics.values())[:3]
    elif isinstance(pics, list):
        pic_rows = pics[:3]
    else:
        pic_rows = []
    large_candidates: list[object] = []
    other_pic_candidates: list[object] = []
    for pic in pic_rows:
        pic_map = _mapping(pic)
        large_candidates.append(_mapping(pic_map.get("large")).get("url"))
        other_pic_candidates.append(pic_map.get("url"))
    candidates = [
        *large_candidates,
        *other_pic_candidates,
        page_pic.get("url"),
    ]
    candidates.extend((row.get("original_pic"), row.get("bmiddle_pic"), row.get("thumbnail_pic")))
    for candidate in candidates:
        if url := _safe_image_url(candidate):
            return url
    return ""


def _topic_tags(row: Mapping[str, Any], body_text: str) -> list[str]:
    tags: list[str] = []
    topic_struct = row.get("topic_struct")
    if isinstance(topic_struct, list):
        for item in topic_struct:
            title = _text(_mapping(item).get("topic_title"))
            if title:
                tags.append(title.strip("#"))
    darwin_tags = row.get("darwin_tags")
    if isinstance(darwin_tags, list):
        for item in darwin_tags:
            item_map = _mapping(item)
            title = _text(item_map.get("name")) or _text(item_map.get("title"))
            if title:
                tags.append(title.strip("#"))
    tags.extend(match.group(1).strip() for match in _TOPIC_RE.finditer(body_text))
    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = tag.strip()
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique.append(normalized[:80])
        if len(unique) >= 20:
            break
    return unique


def _post_url(row: Mapping[str, Any], content_id: str) -> str:
    user_id = _stable_id(_mapping(row.get("user")).get("id"))
    bid = _stable_id(row.get("bid"))
    if user_id and bid:
        return f"https://weibo.com/{quote(user_id, safe='')}/{quote(bid, safe='')}"
    return f"https://m.weibo.cn/detail/{quote(content_id, safe='')}"


def weibo_post_to_content(
    row: Mapping[str, Any],
    *,
    strategy: str = WEIBO_SOURCE_STRATEGIES["search"],
    source_keyword_id: int | None = None,
) -> DiscoveredContent | None:
    """Normalize one anonymous H5 ``mblog`` row into a discovery candidate."""

    content_id = next(
        (candidate for key in ("id", "mid", "idstr") if (candidate := _stable_id(row.get(key)))),
        "",
    )
    if not content_id:
        return None
    raw_text = _text(row.get("text_raw")) or _text(row.get("text"))
    body_text = weibo_html_to_text(raw_text)
    if not body_text:
        return None
    user = _mapping(row.get("user"))
    author_name = _text(user.get("screen_name")) or _text(user.get("name"))
    title = body_text.split("\n", 1)[0][:100].strip()
    if len(body_text) > len(title) and len(title) >= 100:
        title = f"{title.rstrip()}…"
    page_info = _mapping(row.get("page_info"))
    media_info = _mapping(page_info.get("media_info"))
    return DiscoveredContent(
        bvid=content_id,
        content_id=content_id,
        content_url=_post_url(row, content_id),
        source_platform="weibo",
        source_strategy=strategy,
        content_type="post",
        title=title,
        author_name=author_name,
        up_mid=_non_negative_int(user.get("id")),
        body_text=body_text[:8_000],
        cover_url=_cover_url(row),
        published_at=_published_at(row.get("created_at")),
        duration=_duration_seconds(media_info.get("duration")),
        tags=_topic_tags(row, body_text),
        like_count=_non_negative_int(row.get("attitudes_count")),
        comment_count=_non_negative_int(row.get("comments_count")),
        reply_count=_non_negative_int(row.get("comments_count")),
        share_count=_non_negative_int(row.get("reposts_count")),
        retweet_count=_non_negative_int(row.get("reposts_count")),
        # ``reads_count`` is present on some genuine post schemas. Public H5
        # otherwise has no reliable view count, and its observed
        # ``favorites_count`` is an always-zero placeholder rather than useful
        # engagement evidence.
        view_count=_non_negative_int(row.get("reads_count")),
        favorite_count=0,
        danmaku_count=0,
        score_threshold=0.0,
        source_keyword_id=source_keyword_id,
    )


def extract_weibo_mblogs(payload: object) -> list[dict[str, Any]]:
    """Collect direct and nested ``mblog`` rows without iterating wrong types."""

    rows: list[dict[str, Any]] = []
    stack: list[object] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            mblog = node.get("mblog")
            if isinstance(mblog, Mapping):
                rows.append(dict(mblog))
                # Do not recurse into the mblog: retweeted_status is context for
                # the post, not another search result with independent rank.
                stack.extend(value for key, value in node.items() if key != "mblog")
            else:
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return rows
