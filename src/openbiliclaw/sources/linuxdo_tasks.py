"""Linux.do browser-task normalization and durable queue helpers.

Linux.do is a Discourse forum.  The browser extension owns network access and
returns a deliberately small topic-shaped schema; this module is the backend
boundary that validates those rows, converts bootstrap signals into unified
events, converts discovery rows into :class:`DiscoveredContent`, and stores
extension tasks using the shared staged-completion protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from openbiliclaw.published_time import normalize_published_time

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from openbiliclaw.discovery.engine import DiscoveredContent
    from openbiliclaw.storage.database import Database

logger = logging.getLogger(__name__)

LINUXDO_BOOTSTRAP_SCOPES = (
    "linuxdo_bookmarks",
    "linuxdo_likes",
    "linuxdo_read_history",
)
LINUXDO_BOOTSTRAP_SCOPE_LABELS: dict[str, str] = {
    "linuxdo_bookmarks": "书签",
    "linuxdo_likes": "点赞记录",
    "linuxdo_read_history": "阅读记录",
}
LINUXDO_BOOTSTRAP_SIGNAL_STRENGTH: dict[str, float] = {
    "linuxdo_bootstrap_bookmarks": 0.90,
    "linuxdo_bootstrap_likes": 0.85,
    "linuxdo_bootstrap_read_history": 0.35,
}
LINUXDO_DISCOVERY_SCORE_THRESHOLD = 0.60
LINUXDO_EXTENSION_TASK_TIMEOUT_CAP_SECONDS = 29 * 60.0
LINUXDO_TASK_CLAIM_LEASE_SECONDS = 35 * 60.0
LINUXDO_PENDING_PICKUP_TIMEOUT_SECONDS = 3 * 60.0
LINUXDO_TASK_RESULT_GRACE_SECONDS = 30.0
LINUXDO_MAX_TASK_INPUTS = 5
LINUXDO_MAX_DISCOVERY_PAGES = 5
LINUXDO_MAX_BOOTSTRAP_PAGES = 15
LINUXDO_MAX_FETCH_TIMEOUT_MS = 30_000
LINUXDO_DISCOVERY_SCOPE_STRATEGIES: dict[str, str] = {
    "linuxdo_search": "linuxdo-search",
    "linuxdo_hot": "linuxdo-hot",
    "linuxdo_feed": "linuxdo-feed",
    "linuxdo_creator": "linuxdo-creator",
    "linuxdo_related": "linuxdo-related",
}

_LINUXDO_ALLOWED_STRATEGIES = frozenset(LINUXDO_DISCOVERY_SCOPE_STRATEGIES.values())
_RECENT_TASK_STATUSES = ("pending", "in_progress", "completed", "failed")
_TOPIC_ID_RE = re.compile(r"^[1-9][0-9]*$")
_ACCOUNT_KEY_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOPIC_PATH_RE = re.compile(
    r"^/t/(?:(?P<bare_id>[1-9][0-9]*)|[^/?#]+/(?P<slugged_id>[1-9][0-9]*))(?:/|$)",
    re.I,
)
_IGNORED_HTML_ELEMENTS = frozenset({"script", "style", "template"})


def _bounded_float(value: Any, *, fallback: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if parsed != parsed:  # NaN
        return fallback
    return min(maximum, max(minimum, parsed))


def linuxdo_task_timeout_seconds(task_type: str, payload: dict[str, Any] | None) -> float:
    """Return the browser execution budget for one validated Linux.do task.

    Requests are paced by *start time*, so the per-request upper bound is the
    larger of the fetch timeout and configured interval rather than their sum.
    The task-shape limits mirror the extension validator and keep every task
    below the durable claim lease.
    """
    data = payload if isinstance(payload, dict) else {}
    normalized_type = str(task_type).strip()
    interval_seconds = _bounded_float(
        data.get("request_interval_seconds"), fallback=3.0, minimum=0.0, maximum=30.0
    )
    fetch_seconds = (
        _bounded_float(
            data.get("fetch_timeout_ms"),
            fallback=30_000.0,
            minimum=1.0,
            maximum=float(LINUXDO_MAX_FETCH_TIMEOUT_MS),
        )
        / 1000.0
    )
    per_request_seconds = max(1.0, interval_seconds, fetch_seconds)

    if normalized_type == "bootstrap_events":
        raw_limit = _bounded_float(
            data.get("max_items_per_scope", data.get("max_items")),
            fallback=300.0,
            minimum=1.0,
            maximum=300.0,
        )
        default_pages = max(5, (int(raw_limit) + 19) // 20)
        pages = int(
            _bounded_float(
                data.get("max_pages"),
                fallback=float(default_pages),
                minimum=1.0,
                maximum=float(LINUXDO_MAX_BOOTSTRAP_PAGES),
            )
        )
        raw_scopes = data.get("scopes")
        scope_count = (
            len({str(scope) for scope in raw_scopes if str(scope).strip()})
            if isinstance(raw_scopes, list)
            else len(LINUXDO_BOOTSTRAP_SCOPES)
        )
        request_count = 1 + max(1, min(len(LINUXDO_BOOTSTRAP_SCOPES), scope_count)) * pages
    elif normalized_type in {"search", "creator"}:
        key = "keywords" if normalized_type == "search" else "creator_urls"
        raw_inputs = data.get(key)
        breadth = len(raw_inputs) if isinstance(raw_inputs, list) else 1
        pages = int(
            _bounded_float(
                data.get("max_pages"),
                fallback=5.0,
                minimum=1.0,
                maximum=float(LINUXDO_MAX_DISCOVERY_PAGES),
            )
        )
        request_count = max(1, min(LINUXDO_MAX_TASK_INPUTS, breadth)) * (pages + 1)
    elif normalized_type == "related":
        raw_inputs = data.get("related_urls")
        breadth = len(raw_inputs) if isinstance(raw_inputs, list) else 1
        request_count = 2 * max(1, min(LINUXDO_MAX_TASK_INPUTS, breadth))
    else:
        pages = int(
            _bounded_float(
                data.get("max_pages"),
                fallback=5.0,
                minimum=1.0,
                maximum=float(LINUXDO_MAX_DISCOVERY_PAGES),
            )
        )
        # ``hot`` can retry each page through ``top`` when the primary route
        # is unavailable.  Budget both calls; ``feed`` simply finishes sooner.
        request_count = (pages + 1) * (2 if normalized_type == "hot" else 1)

    estimated = 45.0 + request_count * per_request_seconds
    return min(LINUXDO_EXTENSION_TASK_TIMEOUT_CAP_SECONDS, max(45.0, estimated))


class _PlainTextExtractor(HTMLParser):
    """Small dependency-free HTML-to-text boundary for Discourse excerpts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in _IGNORED_HTML_ELEMENTS:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _IGNORED_HTML_ELEMENTS and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0 and data:
            self.parts.append(data)


def _plain_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if "<" not in text or ">" not in text:
        return " ".join(text.split())
    parser = _PlainTextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        logger.debug("linuxdo: failed to parse HTML excerpt", exc_info=True)
        return " ".join(re.sub(r"<[^>]*>", " ", text).split())
    return " ".join(" ".join(parser.parts).split())


def _scalar_text(item: dict[str, Any], *keys: str) -> str:
    """Read the first non-empty scalar without stringifying nested values."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, int | float) and not isinstance(value, bool):
            text = str(value).strip()
        else:
            continue
        if text:
            return text
    return ""


def _scalar_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            return value
    return None


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool | dict | list | tuple | set):
        return 0
    try:
        return max(0, int(float(str(value).strip().replace(",", ""))))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool | dict | list | tuple | set):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cursor_position(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    page = _optional_int(value.get("page"))
    offset = _optional_int(value.get("offset"))
    if page is None or offset is None or page < 0 or offset < 0:
        return None
    if page > 100_000 or offset > 10_000:
        return None
    return {"page": page, "offset": offset}


def _normalize_topic_id(value: Any) -> str:
    if isinstance(value, bool | dict | list | tuple | set) or value is None:
        return ""
    text = str(value).strip()
    for prefix in ("linuxdo:topic:", "topic:", "topic_"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    return text if _TOPIC_ID_RE.fullmatch(text) is not None else ""


def _topic_id_from_url(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.startswith("/"):
        path = text
    else:
        try:
            parsed = urlparse(text)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {"linux.do", "www.linux.do"}:
            return ""
        path = parsed.path
    match = _TOPIC_PATH_RE.match(path)
    if match is None:
        return ""
    return str(match.group("bare_id") or match.group("slugged_id") or "")


def linuxdo_topic_id(item: dict[str, Any]) -> str:
    """Resolve one stable numeric Discourse topic id from a task row."""
    if not isinstance(item, dict):
        return ""
    for key in ("topic_id", "content_id", "id"):
        topic_id = _normalize_topic_id(item.get(key))
        if topic_id:
            return topic_id
    for key in ("url", "content_url", "topic_url", "link"):
        value = item.get(key)
        if isinstance(value, str):
            topic_id = _topic_id_from_url(value)
            if topic_id:
                return topic_id
    return ""


def _canonical_topic_url(topic_id: str, item: dict[str, Any] | None = None) -> str:
    """Return a same-origin topic root, preserving a validated current slug."""
    if not topic_id:
        return ""
    row = item if isinstance(item, dict) else {}
    raw = _scalar_text(row, "url", "content_url", "topic_url", "link")
    if raw:
        try:
            parsed = urlparse(raw)
        except ValueError:
            parsed = None
        if (
            parsed is not None
            and parsed.scheme == "https"
            and (parsed.hostname or "").lower().rstrip(".") in {"linux.do", "www.linux.do"}
            and _topic_id_from_url(raw) == topic_id
        ):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 3 and parts[0].lower() == "t" and parts[2] == topic_id:
                return f"https://linux.do/t/{parts[1]}/{topic_id}"
    return f"https://linux.do/t/{topic_id}"


def _author(item: dict[str, Any]) -> str:
    return _scalar_text(item, "author", "username", "creator", "author_name")


def _author_url(item: dict[str, Any]) -> str:
    raw = _scalar_text(item, "author_url", "creator_url")
    if raw:
        try:
            parsed = urlparse(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            host = (parsed.hostname or "").lower().rstrip(".")
            if host in {"linux.do", "www.linux.do"} and parsed.path.startswith("/u/"):
                return f"https://linux.do{parsed.path.rstrip('/')}"
    username = _author(item)
    if not username:
        return ""
    return f"https://linux.do/u/{quote(username, safe='')}/activity/topics"


def linuxdo_author_url(item: dict[str, Any]) -> str:
    """Return a validated or derived Linux.do creator URL for one row."""
    return _author_url(item)


def _tags(item: dict[str, Any]) -> list[str]:
    values = item.get("tags")
    raw_tags = values if isinstance(values, list | tuple | set) else []
    out: list[str] = []
    seen: set[str] = set()
    category = _scalar_text(item, "category", "category_name")
    for value in ([category] if category else []) + list(raw_tags):
        if not isinstance(value, str):
            continue
        tag = value.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def _summary(item: dict[str, Any]) -> str:
    return _plain_text(_scalar_text(item, "summary", "excerpt", "body_text", "cooked"))


def _display_title(item: dict[str, Any], topic_id: str, summary: str) -> str:
    title = _plain_text(_scalar_text(item, "title", "topic_title", "name"))
    if title:
        return title
    if summary:
        first = re.split(r"[。！？!?\n]", summary, maxsplit=1)[0].strip() or summary
        return first[:80] + ("…" if len(first) > 80 else "")
    return f"Linux.do 主题 {topic_id}"


def _reply_count(item: dict[str, Any]) -> int:
    reply_value = _scalar_value(item, "reply_count", "replies", "comment_count")
    if reply_value is not None:
        return _safe_int(reply_value)
    posts_value = _scalar_value(item, "posts_count")
    return max(0, _safe_int(posts_value) - 1) if posts_value is not None else 0


def _engagement(item: dict[str, Any]) -> tuple[int, int, int, list[str]]:
    view_value = _scalar_value(item, "views", "view_count")
    like_value = _scalar_value(item, "like_count", "likes")
    reply_value = _scalar_value(item, "reply_count", "replies", "comment_count")
    posts_value = _scalar_value(item, "posts_count")
    available: list[str] = []
    if view_value is not None:
        available.append("view")
    if like_value is not None:
        available.append("like")
    if reply_value is not None or posts_value is not None:
        available.append("comment")
    declared = item.get("engagement_available")
    if isinstance(declared, list):
        available = [
            metric
            for metric in ("view", "like", "comment")
            if metric in declared and metric in available
        ]
    return _safe_int(view_value), _safe_int(like_value), _reply_count(item), available


def _bootstrap_event_contract(scope: str) -> tuple[str, str] | None:
    if scope == "linuxdo_bookmarks":
        return "favorite", "linuxdo_bootstrap_bookmarks"
    if scope == "linuxdo_likes":
        return "like", "linuxdo_bootstrap_likes"
    if scope == "linuxdo_read_history":
        return "view", "linuxdo_bootstrap_read_history"
    return None


def linuxdo_bootstrap_items_to_events(
    items: list[dict[str, Any]],
    *,
    account_key: str = "",
) -> list[dict[str, Any]]:
    """Convert extension-collected bookmarks, likes and reads into events."""
    from openbiliclaw.sources.event_format import SOURCE_LINUXDO, build_event

    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        scope = _scalar_text(item, "scope")
        contract = _bootstrap_event_contract(scope)
        topic_id = linuxdo_topic_id(item)
        if contract is None or not topic_id:
            continue
        event_type, import_source = contract
        summary = _summary(item)
        title = _display_title(item, topic_id, summary)
        author = _author(item)
        views, likes, replies, engagement_available = _engagement(item)
        metadata: dict[str, Any] = {
            "content_type": "post",
            "content_id": f"topic:{topic_id}",
            "topic_id": topic_id,
            "scope": scope,
            "import_source": import_source,
            "signal_strength": LINUXDO_BOOTSTRAP_SIGNAL_STRENGTH[import_source],
            "view_count": views,
            "like_count": likes,
            "comment_count": replies,
            "reply_count": replies,
            "favorite_count": 0,
            "share_count": 0,
            "danmaku_count": 0,
            "engagement_available": engagement_available,
        }
        if _ACCOUNT_KEY_RE.fullmatch(account_key):
            metadata["source_account_key"] = account_key
        if summary:
            metadata["summary"] = summary
        category = _scalar_text(item, "category", "category_name")
        if category:
            metadata["category"] = category
        tags = _tags(item)
        if tags:
            metadata["tags"] = tags
        action = _scalar_text(item, "interaction_action")
        if action:
            metadata["interaction_action"] = action
        interaction_time = _scalar_text(item, "interaction_time")
        if interaction_time:
            metadata["interaction_time"] = interaction_time

        label = LINUXDO_BOOTSTRAP_SCOPE_LABELS[scope]
        context = f"Linux.do {label}：{title}"
        if author:
            context += f" 作者：{author}"
        events.append(
            build_event(
                event_type=event_type,
                source_platform=SOURCE_LINUXDO,
                title=title,
                url=_canonical_topic_url(topic_id, item),
                author=author,
                context=context,
                metadata=metadata,
            )
        )
    return events


def _discovery_strategy(item: dict[str, Any]) -> str:
    scope = _scalar_text(item, "scope")
    if scope:
        return LINUXDO_DISCOVERY_SCOPE_STRATEGIES.get(scope, "")
    supplied = _scalar_text(item, "source_strategy")
    return supplied if supplied in _LINUXDO_ALLOWED_STRATEGIES else ""


def linuxdo_discovery_items_to_contents(
    items: list[dict[str, Any]],
    *,
    source_keyword_ids: dict[str, int] | None = None,
) -> list[DiscoveredContent]:
    """Normalize every Linux.do discovery branch to the same topic contract."""
    from openbiliclaw.discovery.engine import DiscoveredContent
    from openbiliclaw.sources.event_format import SOURCE_LINUXDO

    keyword_ids = source_keyword_ids or {}
    fallback_keyword_id = next(iter(keyword_ids.values()), None) if len(keyword_ids) == 1 else None
    contents: list[DiscoveredContent] = []
    by_content_id: dict[str, DiscoveredContent] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        strategy = _discovery_strategy(item)
        topic_id = linuxdo_topic_id(item)
        if not strategy or not topic_id:
            continue

        content_id = f"topic:{topic_id}"
        summary = _summary(item)
        author = _author(item)
        keyword = _scalar_text(item, "search_keyword")
        source_keyword_id = _optional_int(item.get("source_keyword_id"))
        if source_keyword_id is None and keyword:
            source_keyword_id = keyword_ids.get(keyword)
        if source_keyword_id is None and strategy == "linuxdo-search":
            source_keyword_id = fallback_keyword_id
        published = normalize_published_time(
            _scalar_value(item, "published_at", "created_at"),
            label=_scalar_value(item, "published_label"),
        )
        views, likes, replies, engagement_available = _engagement(item)
        candidate = DiscoveredContent(
            bvid=content_id,
            title=_display_title(item, topic_id, summary),
            up_name=author,
            author_name=author,
            description=summary,
            body_text=summary,
            content_id=content_id,
            content_url=_canonical_topic_url(topic_id, item),
            content_type="post",
            source_platform=SOURCE_LINUXDO,
            source_strategy=strategy,
            view_count=views,
            like_count=likes,
            favorite_count=0,
            comment_count=replies,
            reply_count=replies,
            share_count=0,
            danmaku_count=0,
            engagement_available=engagement_available,
            tags=_tags(item),
            source_rank=_safe_int(_scalar_value(item, "source_rank", "rank")),
            score_threshold=LINUXDO_DISCOVERY_SCORE_THRESHOLD,
            source_keyword_id=source_keyword_id,
            published_at=published.published_at,
            published_label=published.published_label,
        )
        existing = by_content_id.get(content_id)
        if existing is None:
            by_content_id[content_id] = candidate
            contents.append(candidate)
            continue

        # Cross-branch duplicates keep their first provenance while missing
        # descriptive fields and engagement counters converge monotonically.
        existing.view_count = max(existing.view_count, candidate.view_count)
        existing.like_count = max(existing.like_count, candidate.like_count)
        existing.comment_count = max(existing.comment_count, candidate.comment_count)
        existing.reply_count = max(existing.reply_count, candidate.reply_count)
        existing.engagement_available = list(
            dict.fromkeys([*existing.engagement_available, *candidate.engagement_available])
        )
        if not existing.author_name and candidate.author_name:
            existing.author_name = candidate.author_name
            existing.up_name = candidate.up_name
        if not existing.body_text and candidate.body_text:
            existing.body_text = candidate.body_text
            existing.description = candidate.description
        if not existing.published_at and candidate.published_at:
            existing.published_at = candidate.published_at
            existing.published_label = candidate.published_label
        if existing.source_keyword_id is None and candidate.source_keyword_id is not None:
            existing.source_keyword_id = candidate.source_keyword_id
        existing.tags = list(dict.fromkeys([*existing.tags, *candidate.tags]))
    return contents


def linuxdo_bootstrap_item_key(item: dict[str, Any], *, account_key: str = "") -> str:
    """Stable cross-task identity for one bootstrap row."""
    if not isinstance(item, dict):
        return ""
    scope = _scalar_text(item, "scope")
    if scope not in LINUXDO_BOOTSTRAP_SCOPES:
        return ""
    topic_id = linuxdo_topic_id(item)
    if not topic_id:
        return ""
    prefix = f"{account_key}:" if _ACCOUNT_KEY_RE.fullmatch(account_key) else ""
    return f"{prefix}{scope}:topic:{topic_id}"


def linuxdo_item_key(item: dict[str, Any]) -> str:
    """Stable queue merge identity preserving independent signal scopes."""
    bootstrap_key = linuxdo_bootstrap_item_key(item)
    if bootstrap_key:
        return bootstrap_key
    topic_id = linuxdo_topic_id(item)
    if not topic_id:
        return ""
    scope = _scalar_text(item, "scope") or _scalar_text(item, "source_strategy")
    return f"{scope}:topic:{topic_id}" if scope else f"topic:{topic_id}"


class LinuxdoTaskResultValidationError(ValueError):
    """A callback exceeded the immutable task contract issued by the backend."""


def linuxdo_task_result_contract(
    task_type: str,
    payload: dict[str, Any] | None,
) -> tuple[set[str], int, dict[str, int]]:
    """Return allowed scopes, total cap and optional per-input caps."""
    data = payload if isinstance(payload, dict) else {}
    normalized_type = str(task_type or "").strip()
    per_input_caps: dict[str, int] = {}
    if normalized_type == "bootstrap_events":
        raw_scopes = data.get("scopes")
        scopes = (
            {
                str(scope).strip()
                for scope in raw_scopes
                if str(scope).strip() in LINUXDO_BOOTSTRAP_SCOPES
            }
            if isinstance(raw_scopes, list)
            else set(LINUXDO_BOOTSTRAP_SCOPES)
        )
        if not scopes:
            scopes = set(LINUXDO_BOOTSTRAP_SCOPES)
        per_scope = int(
            _bounded_float(
                data.get("max_items_per_scope", data.get("max_items")),
                fallback=300.0,
                minimum=1.0,
                maximum=300.0,
            )
        )
        per_input_caps = {scope: per_scope for scope in scopes}
        return scopes, per_scope * len(scopes), per_input_caps

    scope = {
        "search": "linuxdo_search",
        "hot": "linuxdo_hot",
        "feed": "linuxdo_feed",
        "creator": "linuxdo_creator",
        "related": "linuxdo_related",
    }.get(normalized_type, "")
    if not scope:
        raise LinuxdoTaskResultValidationError("unsupported_task_type")
    if normalized_type == "search":
        values = data.get("keywords")
        inputs = [str(value).strip() for value in values] if isinstance(values, list) else []
        inputs = [value for value in inputs if value][:LINUXDO_MAX_TASK_INPUTS]
        per_item = int(
            _bounded_float(
                data.get("max_items_per_keyword", data.get("max_items")),
                fallback=20.0,
                minimum=1.0,
                maximum=300.0,
            )
        )
        per_input_caps = {value: per_item for value in inputs}
        total_cap = per_item * max(1, len(inputs))
        if "max_items" in data:
            total_cap = min(
                total_cap,
                int(
                    _bounded_float(
                        data.get("max_items"),
                        fallback=float(total_cap),
                        minimum=1.0,
                        maximum=300.0,
                    )
                ),
            )
        return {scope}, total_cap, per_input_caps
    key = "creator_urls" if normalized_type == "creator" else "related_urls"
    limit_key = "max_items_per_creator" if normalized_type == "creator" else "max_items_per_seed"
    if normalized_type in {"creator", "related"}:
        values = data.get(key)
        inputs = [str(value).strip() for value in values] if isinstance(values, list) else []
        inputs = [value for value in inputs if value][:LINUXDO_MAX_TASK_INPUTS]
        per_item = int(
            _bounded_float(
                data.get(limit_key, data.get("max_items")),
                fallback=20.0,
                minimum=1.0,
                maximum=300.0,
            )
        )
        per_input_caps = {value: per_item for value in inputs}
        total_cap = per_item * max(1, len(inputs))
        if "max_items" in data:
            total_cap = min(
                total_cap,
                int(
                    _bounded_float(
                        data.get("max_items"),
                        fallback=float(total_cap),
                        minimum=1.0,
                        maximum=300.0,
                    )
                ),
            )
        return {scope}, total_cap, per_input_caps
    limit = int(
        _bounded_float(
            data.get("max_items"),
            fallback=20.0,
            minimum=1.0,
            maximum=300.0,
        )
    )
    return {scope}, limit, {}


def linuxdo_task_cursor_keys(task_type: str, payload: dict[str, Any] | None) -> set[str]:
    """Return backend-owned lane keys permitted to advance durable cursors."""
    data = payload if isinstance(payload, dict) else {}
    if data.get("cursor_contract") != "page-offset-v1":
        return set()
    normalized_type = str(task_type or "").strip()
    if normalized_type in {"hot", "feed"}:
        return {"default"}
    field = "keywords" if normalized_type == "search" else "creator_urls"
    if normalized_type not in {"search", "creator"}:
        return set()
    values = data.get(field)
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values[:LINUXDO_MAX_TASK_INPUTS] if str(value).strip()}


def validate_linuxdo_task_result(
    *,
    task_type: str,
    task_payload: dict[str, Any] | None,
    status: str,
    items: list[dict[str, Any]],
    scope_counts: dict[str, Any] | None,
    account_key: str,
    response_observed: bool,
    complete_scopes: list[str],
    next_cursors: dict[str, Any] | None = None,
) -> None:
    """Reject callbacks that exceed the task's scopes, identity or caps."""
    normalized_status = str(status or "").strip()
    if normalized_status not in {"partial", "ok", "empty", "degraded", "failed"}:
        raise LinuxdoTaskResultValidationError("invalid_result_status")
    allowed_scopes, total_cap, per_input_caps = linuxdo_task_result_contract(
        task_type,
        task_payload,
    )
    if (
        task_type == "bootstrap_events"
        and (normalized_status != "failed" or bool(items))
        and not _ACCOUNT_KEY_RE.fullmatch(account_key)
    ):
        raise LinuxdoTaskResultValidationError("invalid_account_key")
    observed_counts: dict[str, int] = {}
    per_keyword_counts: dict[str, int] = {}
    per_source_input_counts: dict[str, int] = {}
    raw_keyword_ids = (
        task_payload.get("source_keyword_ids") if isinstance(task_payload, dict) else None
    )
    expected_keyword_ids = (
        {
            str(keyword).strip(): int(keyword_id)
            for keyword, keyword_id in raw_keyword_ids.items()
            if str(keyword).strip()
            and isinstance(keyword_id, int)
            and not isinstance(keyword_id, bool)
            and keyword_id > 0
        }
        if isinstance(raw_keyword_ids, dict)
        else {}
    )
    expected_actions = {
        "linuxdo_bookmarks": "favorite",
        "linuxdo_likes": "like",
        "linuxdo_read_history": "view",
    }
    seen: set[str] = set()
    for item in items:
        scope = _scalar_text(item, "scope")
        if scope not in allowed_scopes:
            raise LinuxdoTaskResultValidationError("unauthorized_scope")
        if _scalar_text(item, "content_type") != "post":
            raise LinuxdoTaskResultValidationError("invalid_content_type")
        topic_id = linuxdo_topic_id(item)
        if not topic_id:
            raise LinuxdoTaskResultValidationError("invalid_topic_id")
        supplied_content_id = _scalar_text(item, "content_id")
        if supplied_content_id and supplied_content_id != f"topic:{topic_id}":
            raise LinuxdoTaskResultValidationError("content_id_mismatch")
        declared_engagement = item.get("engagement_available")
        if declared_engagement is not None:
            if not isinstance(declared_engagement, list) or any(
                not isinstance(metric, str) or metric not in {"view", "like", "comment"}
                for metric in declared_engagement
            ):
                raise LinuxdoTaskResultValidationError("invalid_engagement_availability")
            required_values = {
                "view": _scalar_value(item, "views", "view_count"),
                "like": _scalar_value(item, "like_count", "likes"),
                "comment": _scalar_value(
                    item,
                    "reply_count",
                    "replies",
                    "comment_count",
                    "posts_count",
                ),
            }
            if any(required_values[str(metric)] is None for metric in declared_engagement):
                raise LinuxdoTaskResultValidationError("unsupported_engagement_claim")
        key = linuxdo_item_key(item)
        if not key or key in seen:
            raise LinuxdoTaskResultValidationError("duplicate_item")
        seen.add(key)
        observed_counts[scope] = observed_counts.get(scope, 0) + 1
        supplied_action = _scalar_text(item, "interaction_action")
        expected_action = expected_actions.get(scope)
        if supplied_action and supplied_action != expected_action:
            raise LinuxdoTaskResultValidationError("interaction_action_mismatch")
        if task_type == "search":
            keyword = _scalar_text(item, "search_keyword")
            if keyword not in per_input_caps:
                raise LinuxdoTaskResultValidationError("unauthorized_search_keyword")
            supplied_keyword_id = item.get("source_keyword_id")
            if supplied_keyword_id is not None and (
                not isinstance(supplied_keyword_id, int)
                or isinstance(supplied_keyword_id, bool)
                or supplied_keyword_id <= 0
                or expected_keyword_ids.get(keyword) != supplied_keyword_id
            ):
                raise LinuxdoTaskResultValidationError("source_keyword_id_mismatch")
            per_keyword_counts[keyword] = per_keyword_counts.get(keyword, 0) + 1
            if per_keyword_counts[keyword] > per_input_caps[keyword]:
                raise LinuxdoTaskResultValidationError("per_input_cap_exceeded")
        if task_type in {"creator", "related"}:
            source_input = _scalar_text(item, "source_input")
            if source_input not in per_input_caps:
                raise LinuxdoTaskResultValidationError("unauthorized_source_input")
            per_source_input_counts[source_input] = per_source_input_counts.get(source_input, 0) + 1
            if per_source_input_counts[source_input] > per_input_caps[source_input]:
                raise LinuxdoTaskResultValidationError("per_input_cap_exceeded")
    if len(items) > total_cap:
        raise LinuxdoTaskResultValidationError("task_result_cap_exceeded")
    if task_type == "bootstrap_events":
        for scope, cap in per_input_caps.items():
            if observed_counts.get(scope, 0) > cap:
                raise LinuxdoTaskResultValidationError("per_scope_cap_exceeded")

    normalized_counts: dict[str, int] = {}
    if scope_counts is not None:
        for scope, value in scope_counts.items():
            normalized_scope = str(scope).strip()
            if normalized_scope not in allowed_scopes:
                raise LinuxdoTaskResultValidationError("unauthorized_scope_count")
            normalized_counts[normalized_scope] = _safe_int(value)
        if normalized_status != "partial" and normalized_counts != observed_counts:
            raise LinuxdoTaskResultValidationError("scope_count_mismatch")

    completed = {str(scope).strip() for scope in complete_scopes if str(scope).strip()}
    if not completed.issubset(allowed_scopes):
        raise LinuxdoTaskResultValidationError("unauthorized_complete_scope")
    if normalized_status in {"ok", "empty", "degraded"}:
        if not response_observed:
            raise LinuxdoTaskResultValidationError("response_not_observed")
        if normalized_status != "degraded" and completed != allowed_scopes:
            raise LinuxdoTaskResultValidationError("incomplete_final_result")
    if normalized_status == "empty" and items:
        raise LinuxdoTaskResultValidationError("empty_result_has_items")
    if normalized_status == "ok" and not items:
        raise LinuxdoTaskResultValidationError("ok_result_has_no_items")
    allowed_cursor_keys = linuxdo_task_cursor_keys(task_type, task_payload)
    normalized_cursors: dict[str, dict[str, int]] = {}
    if next_cursors is not None:
        for key, value in next_cursors.items():
            normalized_key = str(key).strip()
            position = _cursor_position(value)
            if normalized_key not in allowed_cursor_keys:
                raise LinuxdoTaskResultValidationError("unauthorized_cursor_key")
            if position is None:
                raise LinuxdoTaskResultValidationError("invalid_cursor_position")
            normalized_cursors[normalized_key] = position
    if (
        task_type in {"search", "hot", "feed", "creator"}
        and normalized_status in {"ok", "empty"}
        and set(normalized_cursors) != allowed_cursor_keys
    ):
        raise LinuxdoTaskResultValidationError("incomplete_cursor_result")


def recent_linuxdo_creator_urls(db: Database, *, limit: int = 10) -> list[str]:
    """Return recent Linux.do user URLs suitable for creator discovery."""
    return _recent_linuxdo_item_values(db, key="author_url", limit=limit)


def recent_linuxdo_related_urls(db: Database, *, limit: int = 10) -> list[str]:
    """Return recent canonical topic URLs suitable for related discovery."""
    return _recent_linuxdo_item_values(db, key="url", limit=limit)


def _recent_linuxdo_item_values(db: Database, *, key: str, limit: int) -> list[str]:
    try:
        rows = db.conn.execute(
            """
            SELECT result_json
            FROM linuxdo_tasks
            WHERE status = 'completed' AND result_json IS NOT NULL
            ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC
            LIMIT 50
            """
        ).fetchall()
    except Exception:
        return []

    out: list[str] = []
    seen: set[str] = set()
    max_items = max(1, int(limit))
    for row in rows:
        try:
            raw = row["result_json"] if hasattr(row, "keys") else row[0]
            payload = json.loads(str(raw or "{}"))
        except (json.JSONDecodeError, TypeError, KeyError, IndexError):
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            value = _author_url(item) if key == "author_url" else ""
            if key == "url":
                value = _canonical_topic_url(linuxdo_topic_id(item), item)
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
            if len(out) >= max_items:
                return out
    return out


def _merge_linuxdo_result_payload(
    current: dict[str, Any],
    *,
    items: list[dict[str, Any]] | None = None,
    scope_counts: dict[str, Any] | None = None,
    debug: dict[str, Any] | None = None,
    account_key: str = "",
    response_observed: bool | None = None,
    complete_scopes: list[str] | None = None,
    next_cursors: dict[str, Any] | None = None,
    error: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged_items: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_items = current.get("items")
    for item in current_items if isinstance(current_items, list) else []:
        if not isinstance(item, dict):
            continue
        key = linuxdo_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(item)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        key = linuxdo_item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged_items.append(item)
        added.append(item)

    merged: dict[str, Any] = {}
    if merged_items:
        merged["items"] = merged_items

    merged_counts: dict[str, int] = {}
    current_counts = current.get("scope_counts")
    if isinstance(current_counts, dict):
        for scope, count in current_counts.items():
            if isinstance(scope, str) and scope.strip():
                merged_counts[scope.strip()] = _safe_int(count)
    if isinstance(scope_counts, dict):
        for scope, count in scope_counts.items():
            if not isinstance(scope, str) or not scope.strip():
                continue
            normalized_scope = scope.strip()
            merged_counts[normalized_scope] = max(
                merged_counts.get(normalized_scope, 0), _safe_int(count)
            )
    if merged_counts:
        merged["scope_counts"] = merged_counts

    if isinstance(current.get("debug"), dict) or isinstance(debug, dict):
        merged_debug: dict[str, Any] = {}
        if isinstance(current.get("debug"), dict):
            merged_debug.update(current["debug"])
        if isinstance(debug, dict):
            merged_debug.update(debug)
        merged["debug"] = merged_debug
    existing_account_key = str(current.get("account_key", "") or "").strip()
    normalized_account_key = str(account_key or "").strip()
    if (
        existing_account_key
        and normalized_account_key
        and (existing_account_key != normalized_account_key)
    ):
        raise LinuxdoTaskResultValidationError("account_key_changed")
    if existing_account_key or normalized_account_key:
        merged["account_key"] = existing_account_key or normalized_account_key
    if bool(current.get("response_observed")) or response_observed is True:
        merged["response_observed"] = True
    completed: list[str] = []
    raw_completed = current.get("complete_scopes")
    completed_values = list(raw_completed) if isinstance(raw_completed, list) else []
    completed_values.extend(complete_scopes or [])
    for value in completed_values:
        scope = str(value).strip()
        if scope and scope not in completed:
            completed.append(scope)
    if completed:
        merged["complete_scopes"] = completed
    merged_cursors: dict[str, dict[str, int]] = {}
    current_cursors = current.get("next_cursors")
    if isinstance(current_cursors, dict):
        for key, value in current_cursors.items():
            position = _cursor_position(value)
            if isinstance(key, str) and key.strip() and position is not None:
                merged_cursors[key.strip()] = position
    if isinstance(next_cursors, dict):
        for key, value in next_cursors.items():
            position = _cursor_position(value)
            if not isinstance(key, str) or not key.strip() or position is None:
                raise LinuxdoTaskResultValidationError("invalid_cursor_position")
            merged_cursors[key.strip()] = position
    if merged_cursors:
        merged["next_cursors"] = merged_cursors
    current_error = str(current.get("error", "") or "").strip()
    normalized_error = str(error or "").strip()
    if current_error or normalized_error:
        merged["error"] = current_error or normalized_error
    return merged, added


class LinuxdoTaskQueue:
    """Manage durable Linux.do extension tasks in SQLite."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS linuxdo_tasks (
                id           TEXT PRIMARY KEY,
                type         TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status       TEXT NOT NULL DEFAULT 'pending',
                result_json  TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                claimed_at   TIMESTAMP,
                claim_token  TEXT,
                retained_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_linuxdo_tasks_status
                ON linuxdo_tasks (status, created_at);
            CREATE INDEX IF NOT EXISTS idx_linuxdo_tasks_type_created
                ON linuxdo_tasks (type, created_at);
            CREATE TABLE IF NOT EXISTS linuxdo_discovery_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self._db.conn.execute("PRAGMA table_info(linuxdo_tasks)").fetchall()
        }
        if "claimed_at" not in columns:
            self._db.conn.execute("ALTER TABLE linuxdo_tasks ADD COLUMN claimed_at TIMESTAMP")
        if "claim_token" not in columns:
            self._db.conn.execute("ALTER TABLE linuxdo_tasks ADD COLUMN claim_token TEXT")
        if "retained_count" not in columns:
            self._db.conn.execute(
                "ALTER TABLE linuxdo_tasks ADD COLUMN retained_count INTEGER NOT NULL DEFAULT 0"
            )
        self._db.conn.commit()

    def enqueue_with_id(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        daily_budget: int = 100,
    ) -> str | None:
        conn = self._db.conn
        participating_in_transaction = bool(conn.in_transaction)
        count_today = self._budgeted_count_today(task_type) if daily_budget > 0 else 0
        if daily_budget > 0 and count_today >= daily_budget:
            logger.info(
                "linuxdo task budget exhausted: type=%s used_today=%d budget=%d",
                task_type,
                count_today,
                daily_budget,
            )
            return None
        task_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO linuxdo_tasks (id, type, payload_json) VALUES (?, ?, ?)",
            (task_id, task_type, json.dumps(payload, ensure_ascii=False)),
        )
        if not participating_in_transaction:
            conn.commit()
        return task_id

    def _budgeted_count_today(self, task_type: str) -> int:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if task_type == "bootstrap_events":
            rows = self._db.conn.execute(
                """
                SELECT status, result_json
                FROM linuxdo_tasks
                WHERE type = ? AND created_at >= ?
                """,
                (task_type, today),
            ).fetchall()
            # Personal bootstrap budgets count actual task attempts. A row that
            # was never claimed and expired as stale_pending is explicitly not
            # an upstream attempt, so it remains retryable without burning the
            # user's daily allowance.
            return sum(
                1
                for row in rows
                if not (
                    str(row["status"] or "").strip() == "failed"
                    and _is_stale_pending_result(row["result_json"])
                )
            )
        row = self._db.conn.execute(
            """
            SELECT COALESCE(SUM(retained_count), 0) AS retained
            FROM linuxdo_tasks
            WHERE type = ? AND created_at >= ?
            """,
            (task_type, today),
        ).fetchone()
        return _safe_int(row["retained"] if row is not None else 0)

    def remaining_budget(self, task_type: str, daily_budget: int) -> int | None:
        """Return remaining retained-candidate budget (None means unlimited)."""
        budget = max(0, int(daily_budget))
        if budget == 0:
            return None
        return max(0, budget - self._budgeted_count_today(task_type))

    def record_retained(self, task_id: str, count: int) -> None:
        """Persist the final retained-candidate charge for one task."""
        self._db.conn.execute(
            "UPDATE linuxdo_tasks SET retained_count = ? WHERE id = ?",
            (max(0, int(count)), task_id),
        )
        self._db.conn.commit()

    def discovery_cursor(self) -> str:
        row = self._db.conn.execute(
            "SELECT value FROM linuxdo_discovery_state WHERE key = 'retention_cursor'"
        ).fetchone()
        return str(row["value"] or "") if row is not None else ""

    def set_discovery_cursor(self, source: str) -> None:
        self._db.conn.execute(
            """
            INSERT INTO linuxdo_discovery_state (key, value, updated_at)
            VALUES ('retention_cursor', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(source).strip(),),
        )
        self._db.conn.commit()

    @staticmethod
    def _page_cursor_key(source: str, input_value: str) -> str:
        normalized_source = str(source or "").strip().lower()
        normalized_input = str(input_value or "").strip()
        digest = hashlib.sha256(normalized_input.encode("utf-8")).hexdigest()[:24]
        return f"page_cursor:{normalized_source}:{digest}"

    def discovery_page_cursor(self, source: str, input_value: str = "") -> dict[str, int]:
        """Return the durable page+offset position for one discovery lane."""
        row = self._db.conn.execute(
            "SELECT value FROM linuxdo_discovery_state WHERE key = ?",
            (self._page_cursor_key(source, input_value),),
        ).fetchone()
        if row is None:
            return {"page": 0, "offset": 0}
        try:
            raw = json.loads(str(row["value"] or "{}"))
        except json.JSONDecodeError:
            raw = None
        return _cursor_position(raw) or {"page": 0, "offset": 0}

    def set_discovery_page_cursor(
        self,
        source: str,
        input_value: str,
        position: dict[str, Any],
    ) -> None:
        """Persist a validated page+offset only after a complete lane result."""
        normalized = _cursor_position(position)
        if normalized is None:
            raise ValueError("invalid_linuxdo_page_cursor")
        self._db.conn.execute(
            """
            INSERT INTO linuxdo_discovery_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                self._page_cursor_key(source, input_value),
                json.dumps(normalized, ensure_ascii=False),
            ),
        )
        self._db.conn.commit()

    def next_pending(self, only_ids: set[str] | None = None) -> dict[str, Any] | None:
        stale_before = (
            datetime.now(UTC) - timedelta(seconds=LINUXDO_TASK_CLAIM_LEASE_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        where = (
            "(status = 'pending' OR "
            "(status = 'in_progress' AND (claimed_at IS NULL OR claimed_at <= ?)))"
        )
        params: list[Any] = [stale_before]
        if only_ids is not None:
            ids = [str(task_id) for task_id in only_ids]
            if not ids:
                return None
            where += f" AND id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        conn = self._db.open_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # The extension-side mutex is process/profile local. This durable
            # fence is the authoritative cross-profile single-flight guard:
            # while any lease is fresh, no second extension instance may claim
            # another Linux.do task from the same backend.
            active = conn.execute(
                """
                SELECT id
                FROM linuxdo_tasks
                WHERE status = 'in_progress'
                  AND claimed_at IS NOT NULL
                  AND claimed_at > ?
                LIMIT 1
                """,
                (stale_before,),
            ).fetchone()
            if active is not None:
                conn.commit()
                return None
            row = conn.execute(
                f"SELECT * FROM linuxdo_tasks WHERE {where} ORDER BY created_at ASC LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task_id = str(row["id"])
            claim_token = secrets.token_urlsafe(32)
            conn.execute(
                "UPDATE linuxdo_tasks SET status = 'in_progress', "
                "claimed_at = CURRENT_TIMESTAMP, claim_token = ? WHERE id = ?",
                (claim_token, task_id),
            )
            claimed = conn.execute(
                "SELECT * FROM linuxdo_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        return dict(claimed) if claimed is not None else None

    def claim_token_matches(self, task_id: str, claim_token: str) -> bool:
        """Whether *claim_token* still owns the live task lease."""
        token = str(claim_token or "").strip()
        if not token:
            return False
        row = self._db.conn.execute(
            "SELECT claim_token FROM linuxdo_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        expected = str(row["claim_token"] or "")
        return bool(expected) and secrets.compare_digest(expected, token)

    def find_recent_task(
        self,
        task_type: str,
        *,
        recent_hours: float,
        statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if recent_hours <= 0:
            return None
        selected_statuses = statuses or _RECENT_TASK_STATUSES
        if not selected_statuses:
            return None
        placeholders = ",".join("?" for _ in selected_statuses)
        cutoff = (datetime.now(UTC) - timedelta(hours=recent_hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self._db.conn.execute(
            f"""
            SELECT *
            FROM linuxdo_tasks
            WHERE type = ?
              AND created_at >= ?
              AND status IN ({placeholders})
            ORDER BY
              CASE
                WHEN status IN ('pending', 'in_progress') THEN 0
                WHEN status = 'completed' THEN 1
                ELSE 2
              END,
              created_at DESC
            LIMIT 1
            """,
            (task_type, cutoff, *selected_statuses),
        ).fetchone()
        return dict(row) if row is not None else None

    def expire_stale_pending(
        self,
        task_types: Iterable[str],
        *,
        older_than_seconds: float,
        error: str = "stale_pending",
    ) -> int:
        normalized_types = tuple(
            str(task_type).strip() for task_type in task_types if str(task_type).strip()
        )
        if not normalized_types:
            return 0
        cutoff_ts = datetime.now(UTC).timestamp() - max(0.0, float(older_than_seconds))
        cutoff_text = datetime.fromtimestamp(cutoff_ts, UTC).strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ",".join("?" for _ in normalized_types)
        result_payload = json.dumps({"error": error}, ensure_ascii=False)
        cursor = self._db.conn.execute(
            f"""
            UPDATE linuxdo_tasks
            SET status = 'failed', result_json = ?, completed_at = CURRENT_TIMESTAMP
            WHERE status = 'pending'
              AND type IN ({placeholders})
              AND created_at < ?
              AND COALESCE(result_json, '') NOT LIKE
                  '%"_openbiliclaw_terminal_status"%'
            """,
            (result_payload, *normalized_types, cutoff_text),
        )
        self._db.conn.commit()
        return int(cursor.rowcount or 0)

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._db.conn.execute(
            "SELECT * FROM linuxdo_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def preview_result(
        self,
        task_id: str,
        *,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        account_key: str = "",
        response_observed: bool | None = None,
        complete_scopes: list[str] | None = None,
        next_cursors: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Build the prospective canonical result without mutating SQLite."""
        from openbiliclaw.sources.task_result_protocol import parse_task_result

        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        merged, _added = _merge_linuxdo_result_payload(
            parse_task_result(task.get("result_json")),
            items=items,
            scope_counts=scope_counts,
            debug=debug,
            account_key=account_key,
            response_observed=response_observed,
            complete_scopes=complete_scopes,
            next_cursors=next_cursors,
            error=error,
        )
        return merged

    def merge_result(
        self,
        task_id: str,
        *,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        account_key: str = "",
        response_observed: bool | None = None,
        complete_scopes: list[str] | None = None,
        next_cursors: dict[str, Any] | None = None,
        complete: bool = False,
        expected_claim_token: str | None = None,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        from openbiliclaw.sources.task_result_protocol import mutate_unstaged_result

        added: list[dict[str, Any]] = []

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal added
            merged, added = _merge_linuxdo_result_payload(
                current,
                items=items,
                scope_counts=scope_counts,
                debug=debug,
                account_key=account_key,
                response_observed=response_observed,
                complete_scopes=complete_scopes,
                next_cursors=next_cursors,
            )
            if validate is not None:
                validate(merged)
            return merged

        mutated, _canonical = mutate_unstaged_result(
            self._db,
            table="linuxdo_tasks",
            task_id=task_id,
            mutate=mutate,
            terminal_status="completed" if complete else None,
            expected_claim_token=expected_claim_token,
        )
        return added if mutated else []

    def stage_final_result(
        self,
        task_id: str,
        *,
        terminal_status: str,
        items: list[dict[str, Any]] | None = None,
        scope_counts: dict[str, Any] | None = None,
        debug: dict[str, Any] | None = None,
        account_key: str = "",
        response_observed: bool | None = None,
        complete_scopes: list[str] | None = None,
        next_cursors: dict[str, Any] | None = None,
        error: str = "",
        expected_claim_token: str | None = None,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Stage the immutable first final callback before projections."""
        from openbiliclaw.sources.task_result_protocol import stage_terminal_result

        def merge(current: dict[str, Any]) -> dict[str, Any]:
            merged, _added = _merge_linuxdo_result_payload(
                current,
                items=items,
                scope_counts=scope_counts,
                debug=debug,
                account_key=account_key,
                response_observed=response_observed,
                complete_scopes=complete_scopes,
                next_cursors=next_cursors,
                error=error,
            )
            if validate is not None:
                validate(merged)
            return merged

        return stage_terminal_result(
            self._db,
            table="linuxdo_tasks",
            task_id=task_id,
            terminal_status=terminal_status,
            merge=merge,
            expected_claim_token=expected_claim_token,
        )

    def complete_staged_result(
        self,
        task_id: str,
        *,
        expected_claim_token: str | None = None,
    ) -> bool:
        """Flip a staged task terminal without replacing its canonical JSON."""
        from openbiliclaw.sources.task_result_protocol import complete_staged_result

        return complete_staged_result(
            self._db,
            table="linuxdo_tasks",
            task_id=task_id,
            expected_claim_token=expected_claim_token,
        )

    def fail(
        self,
        task_id: str,
        *,
        error: str = "",
        debug: dict[str, Any] | None = None,
    ) -> bool:
        from openbiliclaw.sources.task_result_protocol import staged_terminal_status

        canonical = self.stage_final_result(
            task_id,
            terminal_status="failed",
            debug=debug,
            error=error,
        )
        if staged_terminal_status(canonical) != "failed":
            return False
        task = self.get(task_id) or {}
        if str(task.get("status", "")) == "failed":
            return False
        return self.complete_staged_result(task_id)


# Readable aliases for callers that preserve the product's ``Linux.do`` case.
LinuxDoTaskQueue = LinuxdoTaskQueue


def _is_stale_pending_result(result_json: Any) -> bool:
    try:
        payload = json.loads(str(result_json or "{}"))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("error") == "stale_pending"
