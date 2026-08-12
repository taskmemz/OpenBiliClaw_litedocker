"""Read-only client for V2EX public feeds, legacy API, and API 2.0.

V2EX has two useful public shapes: the legacy anonymous JSON endpoints and
official JSON/RSS feeds.  API 2.0 is deliberately used only when a personal
access token is present; the rest of the source remains useful without one.
No method in this module performs a write operation.
"""

from __future__ import annotations

import asyncio
import html
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import httpx

from openbiliclaw import __version__

V2EX_BASE_URL = "https://www.v2ex.com"
V2EX_API_BASE_PATH = "/api/v2"
V2EX_PROJECT_URL = "https://github.com/whiteguo233/OpenBiliClaw"
V2EX_USER_AGENT = f"whiteguo233/OpenBiliClaw/{__version__} ({V2EX_PROJECT_URL})"
V2EX_MAX_RESPONSE_BYTES = 2_000_000
_V2EX_XML_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
}
_V2EX_JSON_CONTENT_TYPES = {
    "application/feed+json",
    "application/json",
    "text/json",
}
_V2EX_SEARCH_TERM_RE = re.compile(r"[a-z0-9][a-z0-9._+#-]*|[\u3400-\u9fff]{2,}")
_V2EX_SEARCH_GENERIC_TERMS = {
    "分享",
    "讨论",
    "经验",
    "实践",
    "推荐",
    "求助",
    "指南",
    "教程",
    "避坑",
    "评测",
    "对比",
    "交流",
    "问题",
}


@dataclass(frozen=True)
class V2EXPage:
    """A normalized page of topic or node rows."""

    data: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class V2EXAPIError(RuntimeError):
    """Stable, UI-safe V2EX failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message[:240])
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def validate_v2ex_username(value: object) -> str:
    """Validate an optional public V2EX username."""

    username = str(value or "").strip()
    if not username:
        return ""
    if len(username) > 128 or "/" in username:
        raise ValueError("V2EX username contains an unsupported value")
    if any(ord(char) < 32 or ord(char) == 127 for char in username):
        raise ValueError("V2EX username contains an unsupported character")
    return username


def validate_v2ex_access_token(value: object) -> str:
    """Validate a PAT structurally; live validity is checked by ``/member``."""

    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) > 512:
        raise ValueError("V2EX access token must be at most 512 characters")
    if any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or ord(char) > 126 for char in token
    ):
        raise ValueError("V2EX access token contains an unsupported character")
    return token


def member_username(payload: Mapping[str, Any]) -> str:
    """Defensively extract the username from an API 2.0 member response."""

    username = str(payload.get("username") or "").strip()
    if not username:
        raise V2EXAPIError("schema_changed", "V2EX member response is missing username")
    return username


def _retry_after_seconds(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return min(86_400, max(0, int(raw)))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return min(86_400, max(0, math.ceil((retry_at - datetime.now(UTC)).total_seconds())))


def _rate_limit_retry_after(response: httpx.Response) -> int | None:
    explicit = _retry_after_seconds(response.headers.get("Retry-After"))
    if explicit is not None:
        return explicit
    raw_reset = str(response.headers.get("X-Rate-Limit-Reset") or "").strip()
    try:
        reset_at = float(raw_reset)
    except ValueError:
        return None
    return min(86_400, max(0, math.ceil(reset_at - datetime.now(UTC).timestamp())))


def _as_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _unwrap_api2(payload: Any) -> Any:
    """Return the API 2.0 ``result`` while tolerating legacy direct fixtures."""

    if not isinstance(payload, Mapping) or "success" not in payload:
        return payload
    if payload.get("success") is not True or "result" not in payload:
        raise V2EXAPIError("invalid_request", "V2EX API 2.0 rejected the request")
    return payload.get("result")


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


def _feed_timestamp(value: object) -> int:
    if isinstance(value, int | float):
        return max(0, int(value))
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0, int(parsed.timestamp()))


def _strip_markup(value: object) -> str:
    raw = html.unescape(str(value or ""))
    raw = re.sub(r"<\s*br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _bounded_search_terms(query: str) -> tuple[str, ...]:
    """Extract distinctive terms for the bounded latest/hot fallback.

    Unified keyword generation intentionally emits natural, multi-part queries.
    Requiring that entire phrase to appear verbatim made the anonymous fallback
    effectively unusable.  Keep exact phrase matches strongest, but allow a
    bounded candidate through when at least one non-generic core term matches;
    the shared evaluator remains the final relevance gate.
    """

    terms: list[str] = []
    seen: set[str] = set()
    for term in _V2EX_SEARCH_TERM_RE.findall(query.casefold()):
        normalized = term.strip("._+#-")
        if len(normalized) < 2 or normalized in _V2EX_SEARCH_GENERIC_TERMS:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return tuple(terms[:8])


def _bounded_search_score(
    haystack: str, query: str, terms: tuple[str, ...]
) -> tuple[int, int, int]:
    if query in haystack:
        return (2, len(terms), sum(len(term) for term in terms))
    matched = [term for term in terms if term in haystack]
    if not matched:
        return (0, 0, 0)
    return (1, len(matched), sum(len(term) for term in matched))


def _feed_item_to_topic(item: Mapping[str, Any], *, node_name: str = "") -> dict[str, Any]:
    author = _as_mapping(item.get("author"))
    url = str(item.get("url") or item.get("link") or "").strip()
    topic_id = _topic_id(item.get("id")) or _topic_id(url)
    title = str(item.get("title") or "").strip()
    body = item.get("content_text") or item.get("content_html") or item.get("description") or ""
    row: dict[str, Any] = {
        "id": topic_id,
        "url": url,
        "title": title,
        "content": _strip_markup(body),
        "created": _feed_timestamp(item.get("date_published") or item.get("pubDate")),
        "member": {"username": str(author.get("name") or author.get("username") or "").strip()},
        "node": {"name": node_name} if node_name else {},
        "replies": 0,
    }
    return row


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _xml_feed_to_rows(text: str, *, node_name: str = "") -> list[dict[str, Any]]:
    lowered = text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise V2EXAPIError("schema_changed", "V2EX feed returned unsupported XML declarations")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise V2EXAPIError("schema_changed", "V2EX feed returned invalid XML") from exc
    if _xml_local_name(root.tag) not in {"feed", "rdf", "rss"}:
        raise V2EXAPIError("schema_changed", "V2EX feed returned an unexpected XML root")
    rows: list[dict[str, Any]] = []
    for entry in root.iter():
        if _xml_local_name(entry.tag) not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        for child in entry:
            name = _xml_local_name(child.tag)
            value = (child.text or "").strip()
            if name == "author" and not value:
                value = next(
                    (
                        (nested.text or "").strip()
                        for nested in child
                        if _xml_local_name(nested.tag) == "name" and (nested.text or "").strip()
                    ),
                    "",
                )
            if name == "link" and not value:
                value = str(child.attrib.get("href", ""))
            fields[name] = value
        rows.append(
            _feed_item_to_topic(
                {
                    "id": fields.get("guid", "") or fields.get("id", ""),
                    "url": fields.get("link", ""),
                    "title": fields.get("title", ""),
                    "description": (
                        fields.get("description", "")
                        or fields.get("content", "")
                        or fields.get("summary", "")
                    ),
                    "pubDate": fields.get("pubdate", "") or fields.get("published", ""),
                    "author": {"name": fields.get("author", "")},
                },
                node_name=node_name,
            )
        )
        if len(rows) >= 1000:
            break
    return rows


class V2EXClient:
    """Minimal, read-only client for public V2EX discovery."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        access_token: str | None = None,
        request_interval_seconds: float = 2.0,
        transient_retry_delay_seconds: float = 0.25,
    ) -> None:
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=V2EX_BASE_URL,
            timeout=15.0,
            # V2EX is classified as CN-direct. Do not inherit shell proxy
            # variables or the shared overseas-provider route.
            trust_env=False,
        )
        self._access_token = validate_v2ex_access_token(access_token) or None
        self._request_interval_seconds = max(0.0, float(request_interval_seconds))
        self._transient_retry_delay_seconds = max(0.0, float(transient_retry_delay_seconds))
        self._request_lock = asyncio.Lock()
        self._last_request_started_at = 0.0
        self.last_rate_limit: dict[str, int] = {}

    async def __aenter__(self) -> V2EXClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    @property
    def has_access_token(self) -> bool:
        return self._access_token is not None

    def disable_access_token(self) -> None:
        """Drop a rejected PAT while leaving public discovery usable."""

        self._access_token = None

    async def get_member(self) -> dict[str, Any]:
        """Return the account represented by the configured PAT."""

        if self._access_token is None:
            raise V2EXAPIError(
                "unauthorized", "V2EX /api/v2/member requires a personal access token"
            )
        payload = await self._request_json(f"{V2EX_API_BASE_PATH}/member", authenticated=True)
        payload = _unwrap_api2(payload)
        if not isinstance(payload, dict):
            raise V2EXAPIError("schema_changed", "V2EX member response shape changed")
        return payload

    async def get_node(self, node_name: str) -> dict[str, Any]:
        slug = _validate_slug(node_name, "node")
        if self.has_access_token:
            payload = await self._request_json(
                f"{V2EX_API_BASE_PATH}/nodes/{quote(slug, safe='')}", authenticated=True
            )
            payload = _unwrap_api2(payload)
        else:
            payload = await self._request_json(
                "/api/nodes/show.json", params={"name": slug}, authenticated=False
            )
        if not isinstance(payload, dict):
            raise V2EXAPIError("schema_changed", "V2EX node response shape changed")
        return payload

    async def get_node_topics(self, node_name: str, *, page: int = 1, limit: int = 50) -> V2EXPage:
        slug = _validate_slug(node_name, "node")
        page_number = max(1, int(page))
        page_limit = min(100, max(1, int(limit)))
        if self.has_access_token:
            payload = await self._request_json(
                f"{V2EX_API_BASE_PATH}/nodes/{quote(slug, safe='')}/topics",
                params={"p": page_number},
                authenticated=True,
            )
            payload = _unwrap_api2(payload)
            return self._page(payload, limit=page_limit, offset=(page_number - 1) * page_limit)
        return await self.get_node_feed(slug, limit=page_limit)

    async def get_node_feed(self, node_name: str, *, limit: int = 50) -> V2EXPage:
        slug = _validate_slug(node_name, "node")
        payload = await self._request_feed(f"/feed/{quote(slug, safe='')}.json", node_name=slug)
        return V2EXPage(payload[:limit], len(payload), min(100, max(1, int(limit))), 0)

    async def get_topic(self, topic_id: str | int) -> dict[str, Any]:
        normalized = _topic_id(topic_id)
        if not normalized:
            raise ValueError("V2EX topic id is required")
        if self.has_access_token:
            payload = await self._request_json(
                f"{V2EX_API_BASE_PATH}/topics/{quote(normalized, safe='')}", authenticated=True
            )
            payload = _unwrap_api2(payload)
        else:
            payload = await self._request_json(
                "/api/topics/show.json", params={"id": normalized}, authenticated=False
            )
            if isinstance(payload, list):
                payload = next((row for row in payload if isinstance(row, dict)), None)
        if not isinstance(payload, dict):
            raise V2EXAPIError("schema_changed", "V2EX topic response shape changed")
        return payload

    async def get_topic_replies(
        self,
        topic_id: str | int,
        *,
        page: int = 1,
        limit: int = 20,
    ) -> V2EXPage:
        """Read one API 2.0 reply page when a PAT is available."""

        normalized = _topic_id(topic_id)
        if not normalized:
            raise ValueError("V2EX topic id is required")
        if self._access_token is None:
            raise V2EXAPIError(
                "unauthorized",
                "V2EX reply enrichment requires a personal access token",
            )
        page_number = max(1, int(page))
        page_limit = min(100, max(1, int(limit)))
        payload = await self._request_json(
            f"{V2EX_API_BASE_PATH}/topics/{quote(normalized, safe='')}/replies",
            params={"p": page_number},
            authenticated=True,
        )
        payload = _unwrap_api2(payload)
        return self._page(
            payload,
            limit=page_limit,
            offset=(page_number - 1) * page_limit,
        )

    async def get_hot(self, *, limit: int = 50) -> V2EXPage:
        payload = await self._request_json("/api/topics/hot.json", authenticated=False)
        return self._page(payload, limit=min(100, max(1, int(limit))), offset=0)

    async def get_latest(self, *, limit: int = 50) -> V2EXPage:
        payload = await self._request_json("/api/topics/latest.json", authenticated=False)
        return self._page(payload, limit=min(100, max(1, int(limit))), offset=0)

    async def get_tab(self, tab: str, *, limit: int = 50) -> V2EXPage:
        name = _validate_slug(tab, "tab")
        try:
            rows = await self._request_feed(f"/feed/tab/{quote(name, safe='')}.json")
        except V2EXAPIError as exc:
            if exc.code != "not_found":
                raise
            rows = await self._request_feed(f"/feed/tab/{quote(name, safe='')}.xml")
        return V2EXPage(rows[:limit], len(rows), min(100, max(1, int(limit))), 0)

    async def get_member_topics(self, username: str, *, limit: int = 50) -> V2EXPage:
        member = validate_v2ex_username(username)
        if not member:
            raise ValueError("V2EX username is required")
        rows = await self._request_feed(f"/feed/member/{quote(member, safe='')}.xml")
        return V2EXPage(rows[:limit], len(rows), min(100, max(1, int(limit))), 0)

    async def search_topics(self, keyword: str, *, limit: int = 20) -> V2EXPage:
        """Search locally across bounded official latest/hot public responses.

        V2EX's documented API table does not expose a full-text search
        endpoint. Keeping the fallback bounded makes the source useful without
        turning OpenBiliClaw into a page crawler; callers can later inject an
        external search provider without changing the normalized contract.
        """

        query = str(keyword or "").strip().casefold()
        if not query:
            return V2EXPage([], 0, max(1, int(limit)), 0)
        terms = _bounded_search_terms(query)
        latest = await self.get_latest(limit=min(100, max(20, int(limit) * 3)))
        hot = await self.get_hot(limit=min(100, max(20, int(limit) * 2)))
        scored_rows: list[tuple[tuple[int, int, int], int, dict[str, Any]]] = []
        seen: set[str] = set()
        for position, row in enumerate([*latest.data, *hot.data]):
            identity = _topic_id(row.get("id")) or str(row.get("url") or "")
            if identity in seen:
                continue
            seen.add(identity)
            haystack = f"{row.get('title', '')}\n{row.get('content', '')}".casefold()
            score = _bounded_search_score(haystack, query, terms)
            if score[0] > 0:
                scored_rows.append((score, position, row))
        scored_rows.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[1]))
        rows = [row for _, _, row in scored_rows[: max(1, int(limit))]]
        return V2EXPage(rows, len(rows), max(1, int(limit)), 0)

    async def _request_feed(self, path: str, *, node_name: str = "") -> list[dict[str, Any]]:
        response = await self._request(path, authenticated=False)
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "text/html":
            raise V2EXAPIError("schema_changed", "V2EX feed returned an HTML page")
        text = response.text
        envelope = text.lstrip()[:64].casefold()
        is_xml = content_type in _V2EX_XML_CONTENT_TYPES or (
            not content_type and envelope.startswith(("<?xml", "<rss", "<feed", "<rdf"))
        )
        is_json = content_type in _V2EX_JSON_CONTENT_TYPES or (
            not content_type and envelope.startswith("{")
        )
        if is_xml:
            return _xml_feed_to_rows(text, node_name=node_name)
        if not is_json:
            raise V2EXAPIError("schema_changed", "V2EX feed returned an unsupported envelope")
        try:
            payload = response.json()
        except ValueError as exc:
            raise V2EXAPIError("schema_changed", "V2EX feed returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise V2EXAPIError("schema_changed", "V2EX feed response shape changed")
        items = payload.get("items")
        if not isinstance(items, list):
            raise V2EXAPIError("schema_changed", "V2EX JSON Feed is missing items")
        return [
            _feed_item_to_topic(item, node_name=node_name)
            for item in items[:1000]
            if isinstance(item, Mapping)
        ]

    async def _request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool,
    ) -> Any:
        response = await self._request(path, params=params, authenticated=authenticated)
        content_type = (
            str(response.headers.get("content-type", "")).split(";", 1)[0].strip().lower()
        )
        if content_type not in _V2EX_JSON_CONTENT_TYPES:
            raise V2EXAPIError(
                "schema_changed",
                "V2EX API returned an unsupported content type",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise V2EXAPIError("schema_changed", "V2EX API returned invalid JSON") from exc

    async def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        authenticated: bool,
    ) -> httpx.Response:
        if not path.startswith("/"):
            raise ValueError("V2EX paths must be absolute paths")
        headers = {"User-Agent": V2EX_USER_AGENT, "Accept": "application/json, application/xml"}
        if authenticated and self._access_token is not None:
            headers["Authorization"] = f"Bearer {self._access_token}"
        response: httpx.Response | None = None
        for attempt in range(2):
            async with self._request_lock:
                await self._pace()
                try:
                    response = await self._bounded_get(
                        path,
                        params=params,
                        headers=headers,
                    )
                except httpx.TimeoutException as exc:
                    if attempt == 0:
                        response = None
                    else:
                        raise V2EXAPIError(
                            "timeout", self._network_failure_message("V2EX API request timed out")
                        ) from exc
                except httpx.HTTPError as exc:
                    raise V2EXAPIError(
                        "network_error",
                        self._network_failure_message("V2EX API network request failed"),
                    ) from exc
            if response is None or (response.status_code >= 500 and attempt == 0):
                if self._transient_retry_delay_seconds:
                    await asyncio.sleep(self._transient_retry_delay_seconds)
                continue
            break
        if response is None:
            raise V2EXAPIError(
                "timeout", self._network_failure_message("V2EX API request timed out")
            )
        self._capture_rate_limit(response)
        status = int(response.status_code)
        if status == 429:
            raise V2EXAPIError(
                "rate_limited",
                "V2EX API rate limited this client",
                status_code=status,
                retry_after_seconds=_rate_limit_retry_after(response),
            )
        if status in (401, 403):
            raise V2EXAPIError(
                "unauthorized",
                "V2EX rejected the access token or request",
                status_code=status,
            )
        if status == 404:
            raise V2EXAPIError("not_found", "V2EX resource was not found", status_code=status)
        if status == 400:
            raise V2EXAPIError(
                "invalid_request", "V2EX API rejected the request", status_code=status
            )
        if status >= 500:
            raise V2EXAPIError(
                "upstream_error", "V2EX API is temporarily unavailable", status_code=status
            )
        if status < 200 or status >= 300:
            raise V2EXAPIError("http_error", f"V2EX API returned HTTP {status}", status_code=status)
        return response

    async def _bounded_get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> httpx.Response:
        """Read one response with a hard body cap before JSON/XML decoding."""

        async with self._http.stream(
            "GET",
            path,
            params=params,
            headers=headers,
            timeout=15.0,
        ) as streamed:
            status = int(streamed.status_code)
            # ``aiter_bytes`` yields decoded bytes. Reusing the upstream
            # Content-Encoding on the materialized response makes httpx decode
            # gzip/br a second time (V2EX JSON Feed is commonly gzip encoded).
            # Content-Length / Transfer-Encoding describe the wire body too,
            # not the decoded bounded body retained below.
            response_headers = httpx.Headers(streamed.headers)
            for header in ("content-encoding", "content-length", "transfer-encoding"):
                response_headers.pop(header, None)
            if status < 200 or status >= 300:
                return httpx.Response(
                    status,
                    headers=response_headers,
                    content=b"",
                    request=streamed.request,
                )
            content_length = streamed.headers.get("content-length")
            try:
                declared_length = int(content_length) if content_length is not None else 0
            except ValueError:
                declared_length = 0
            if declared_length > V2EX_MAX_RESPONSE_BYTES:
                raise V2EXAPIError("response_too_large", "V2EX response exceeded the safe limit")
            body = bytearray()
            async for chunk in streamed.aiter_bytes():
                body.extend(chunk)
                if len(body) > V2EX_MAX_RESPONSE_BYTES:
                    raise V2EXAPIError(
                        "response_too_large",
                        "V2EX response exceeded the safe limit",
                    )
            return httpx.Response(
                status,
                headers=response_headers,
                content=bytes(body),
                request=streamed.request,
            )

    async def _pace(self) -> None:
        if self._request_interval_seconds <= 0:
            return
        now = time.monotonic()
        remaining = self._request_interval_seconds - (now - self._last_request_started_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_started_at = time.monotonic()

    def _capture_rate_limit(self, response: httpx.Response) -> None:
        values: dict[str, int] = {}
        for key, header in (
            ("limit", "X-Rate-Limit-Limit"),
            ("remaining", "X-Rate-Limit-Remaining"),
            ("reset", "X-Rate-Limit-Reset"),
        ):
            raw = response.headers.get(header)
            try:
                if raw is not None:
                    values[key] = max(0, int(raw))
            except ValueError:
                continue
        if values:
            self.last_rate_limit = values

    @staticmethod
    def _network_failure_message(message: str) -> str:
        return message

    @staticmethod
    def _page(payload: Any, *, limit: int, offset: int) -> V2EXPage:
        rows: Any = payload
        total = 0
        if isinstance(payload, dict):
            for key in ("topics", "items", "data", "result"):
                if isinstance(payload.get(key), list):
                    rows = payload[key]
                    break
            total = _safe_int(payload.get("total"))
        if not isinstance(rows, list):
            raise V2EXAPIError("schema_changed", "V2EX topic response shape changed")
        normalized = [dict(row) for row in rows if isinstance(row, Mapping)]
        if total <= 0:
            total = len(normalized)
        return V2EXPage(normalized[:limit], total, limit, offset)


def _validate_slug(value: object, label: str) -> str:
    slug = str(value or "").strip().lower()
    if not slug or len(slug) > 128 or "/" in slug or any(ord(c) < 32 for c in slug):
        raise ValueError(f"V2EX {label} is invalid")
    return slug


def _safe_int(value: object) -> int:
    if value is None or str(value).strip() == "":
        return 0
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return 0
