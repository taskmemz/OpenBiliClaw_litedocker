"""Read-only anonymous client for Weibo discovery endpoints.

The mobile H5 API is public but requires an anonymous visitor ``SUB`` cookie.
This client obtains that cookie from Weibo's own visitor flow, keeps it only in
memory, and never accepts or replays a user's account cookies.  All owned HTTP
clients connect directly (``trust_env=False``), matching the other domestic
source clients in this repository.
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

WEIBO_MOBILE_CONTAINER_URL = "https://m.weibo.cn/api/container/getIndex"
WEIBO_VISITOR_ENTRY_URL = "https://visitor.passport.weibo.cn/visitor/visitor"
WEIBO_VISITOR_GENERATE_URL = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
WEIBO_VISITOR_DOMAIN = ".weibo.cn"
WEIBO_VISITOR_SDK_USER_AGENT = "php-sso_sdk_client-0.6.36"
WEIBO_HOT_SEARCH_URL = "https://weibo.com/ajax/side/hotSearch"
WEIBO_HOT_SEARCH_REFERER = "https://weibo.com/"
WEIBO_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

_VISITOR_REQUEST_ID_RE = re.compile(r"\bvar\s+request_id\s*=\s*([\"'])(?P<value>.+?)\1\s*;")
_VISITOR_RETURN_URL_RE = re.compile(r"\bvar\s+return_url\s*=\s*([\"'])(?P<value>.+?)\1\s*;")
_VISITOR_GENERATE_CALL_RE = re.compile(
    r"/visitor/genvisitor2.*?\bcb=(?P<callback>[A-Za-z_$][A-Za-z0-9_$]{0,127})"
    r"&ver=(?P<version>[A-Za-z0-9_.-]{1,32})&request_id\b",
    re.DOTALL,
)
_VISITOR_REFRESH_STATUSES = frozenset({302, 403, 432})
_MAX_PAGE = 10_000
_MAX_LIMIT = 50
_JSON_MIME_TYPES = frozenset({"application/json"})
_VISITOR_ENTRY_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_VISITOR_SCRIPT_MIME_TYPES = frozenset({"text/javascript", "application/javascript"})


@dataclass(frozen=True)
class WeiboPage:
    """One validated page of raw upstream rows.

    ``data`` is the canonical attribute.  ``items`` and ``rows`` are aliases
    for producer compatibility while the source wiring settles.
    """

    data: list[dict[str, Any]]
    page: int
    next_page: int | None
    total: int | None

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.data

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.data


class WeiboClientError(RuntimeError):
    """Stable, credential-safe failure raised by :class:`WeiboClient`."""

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


def _bounded_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        candidate = value.strip().replace(",", "")
        if candidate.isdigit():
            return int(candidate)
    return None


def _page_number(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"Weibo {name} must be an integer")
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"Weibo {name} must be an integer") from exc
    if number < 1 or number > _MAX_PAGE:
        raise ValueError(f"Weibo {name} must be between 1 and {_MAX_PAGE}")
    return number


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError("Weibo limit must be an integer")
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("Weibo limit must be an integer") from exc
    return min(_MAX_LIMIT, max(0, number))


def _creator_uid(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("Weibo creator uid must be a positive integer")
    uid = str(value or "").strip()
    if not uid.isdigit() or uid.startswith("0") or len(uid) > 24:
        raise ValueError("Weibo creator uid must be a positive integer")
    return uid


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


def _safe_js_string(value: str) -> str:
    """Decode a quoted JavaScript string body without executing JavaScript."""

    try:
        decoded = json.loads(f'"{value}"')
    except (TypeError, ValueError) as exc:
        raise WeiboClientError(
            "visitor_bootstrap_failed", "Weibo visitor page contained an invalid string"
        ) from exc
    if not isinstance(decoded, str):
        raise WeiboClientError(
            "visitor_bootstrap_failed", "Weibo visitor page contained an invalid string"
        )
    return html.unescape(decoded)


def _safe_visitor_cookie(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > 4096:
        return ""
    if ";" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return ""
    return candidate


def _response_mime_type(response: httpx.Response) -> str:
    """Return a normalized media type without trusting parameters."""

    return str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().casefold()


def _require_mime_type(
    response: httpx.Response,
    allowed: frozenset[str],
    *,
    code: str,
    context: str,
) -> None:
    """Fail closed when an endpoint unexpectedly returns another document type."""

    if _response_mime_type(response) not in allowed:
        raise WeiboClientError(
            code,
            f"Weibo {context} returned an unexpected content type",
            status_code=response.status_code,
        )


def _visitor_jsonp_payload(value: str, *, callback: str) -> dict[str, Any]:
    """Parse the visitor service's callback without evaluating JavaScript.

    The live endpoint currently guards the callback with
    ``window.<cb> && <cb>(...)``.  Older responses used a direct ``<cb>(...)``
    call, so both inert wrappers are accepted while the callback name itself
    must exactly match the one advertised by the entry page.
    """

    escaped_callback = re.escape(callback)
    match = re.fullmatch(
        rf"(?:window\.{escaped_callback}\s*&&\s*)?{escaped_callback}"
        r"\s*\(\s*(?P<payload>\{.*\})\s*\)\s*;?\s*",
        value.strip(),
        re.DOTALL,
    )
    if match is None:
        raise WeiboClientError(
            "visitor_bootstrap_failed", "Weibo visitor service returned invalid JSONP"
        )
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise WeiboClientError(
            "visitor_bootstrap_failed", "Weibo visitor service returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise WeiboClientError(
            "visitor_bootstrap_failed", "Weibo visitor service returned invalid JSON"
        )
    return payload


def _extract_mblog_rows(cards: object, *, limit: int) -> list[dict[str, Any]]:
    """Extract direct and ``card_group``-nested mblogs from an H5 page."""

    if not isinstance(cards, list):
        raise WeiboClientError("schema_changed", "Weibo page cards changed shape")
    rows: list[dict[str, Any]] = []
    stack: list[object] = list(reversed(cards))
    while stack and len(rows) < limit:
        card = stack.pop()
        if not isinstance(card, Mapping):
            continue
        mblog = card.get("mblog")
        if isinstance(mblog, Mapping):
            rows.append(dict(mblog))
            continue
        card_group = card.get("card_group")
        if isinstance(card_group, list):
            stack.extend(reversed(card_group))
    return rows


class WeiboClient:
    """Anonymous read client for search, creator posts, and hot topics.

    An injected :class:`httpx.AsyncClient` remains owned by its caller.  Even
    for injected clients, every request strips ambient ``Authorization`` and
    ``Cookie`` headers before adding only this instance's anonymous visitor
    ``SUB``.  This prevents an accidentally authenticated test/client session
    from turning anonymous discovery into account-cookie replay.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        request_interval_seconds: float = 1.0,
        transient_retry_delay_seconds: float = 0.5,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._owns_http_client = http_client is None
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._http = http_client or httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._request_interval_seconds = max(0.0, float(request_interval_seconds))
        self._transient_retry_delay_seconds = max(0.0, float(transient_retry_delay_seconds))
        self._request_lock = asyncio.Lock()
        self._visitor_lock = asyncio.Lock()
        self._last_request_started_at = 0.0
        self._visitor_sub: str | None = None

    async def __aenter__(self) -> WeiboClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()

    async def search_posts(
        self,
        keyword: str,
        *,
        page: int = 1,
        limit: int = 20,
    ) -> WeiboPage:
        """Fetch one anonymous H5 search page."""

        query = str(keyword or "").strip()
        if not query:
            raise ValueError("Weibo search keyword is required")
        page_number = _page_number(page, name="page")
        page_limit = _limit(limit)
        if page_limit == 0:
            return WeiboPage([], page_number, None, 0)
        url = httpx.URL(
            WEIBO_MOBILE_CONTAINER_URL,
            params={"containerid": f"100103type=1&q={query}", "page": page_number},
        )
        return await self._mobile_page(url, page=page_number, limit=page_limit)

    async def creator_posts(
        self,
        uid: object,
        *,
        page: int = 1,
        limit: int = 20,
    ) -> WeiboPage:
        """Fetch recent public posts for a numeric creator uid."""

        normalized_uid = _creator_uid(uid)
        page_number = _page_number(page, name="page")
        page_limit = _limit(limit)
        if page_limit == 0:
            return WeiboPage([], page_number, None, 0)
        url = httpx.URL(
            WEIBO_MOBILE_CONTAINER_URL,
            params={"containerid": f"107603{normalized_uid}", "page": page_number},
        )
        return await self._mobile_page(url, page=page_number, limit=page_limit)

    async def hot_topics(self, *, limit: int = 20) -> WeiboPage:
        """Fetch the public realtime hot-search topic list (no cookie)."""

        page_limit = _limit(limit)
        if page_limit == 0:
            return WeiboPage([], 1, None, 0)
        response = await self._send(
            "GET",
            WEIBO_HOT_SEARCH_URL,
            headers={"Accept": "application/json", "Referer": WEIBO_HOT_SEARCH_REFERER},
        )
        self._raise_for_status(response, context="hot search")
        payload = self._json_object(response)
        self._require_ok(payload, context="hot search")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise WeiboClientError("schema_changed", "Weibo hot-search data changed shape")
        realtime = data.get("realtime")
        if not isinstance(realtime, list):
            raise WeiboClientError("schema_changed", "Weibo hot-search rows changed shape")
        rows = [dict(row) for row in realtime if isinstance(row, Mapping)]
        if realtime and not rows:
            raise WeiboClientError("schema_changed", "Weibo hot-search rows changed shape")
        return WeiboPage(rows[:page_limit], 1, None, len(rows))

    async def search(self, keyword: str, *, page: int = 1, limit: int = 20) -> WeiboPage:
        """Compatibility alias for :meth:`search_posts`."""

        return await self.search_posts(keyword, page=page, limit=limit)

    async def hot_search(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Compatibility alias returning only hot-topic rows."""

        return (await self.hot_topics(limit=limit)).data

    async def _mobile_page(self, url: httpx.URL, *, page: int, limit: int) -> WeiboPage:
        payload = await self._mobile_json(url)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise WeiboClientError("schema_changed", "Weibo page data changed shape")
        cards = data.get("cards")
        rows = _extract_mblog_rows(cards, limit=limit)
        cardlist_info = data.get("cardlistInfo")
        if cardlist_info is None:
            info: Mapping[str, Any] = {}
        elif isinstance(cardlist_info, Mapping):
            info = cardlist_info
        else:
            raise WeiboClientError("schema_changed", "Weibo page metadata changed shape")
        total = _bounded_non_negative_int(info.get("total"))
        if not rows:
            cards_are_empty = isinstance(cards, list) and not cards
            affirmative_empty = total == 0
            if not affirmative_empty and not (cards_are_empty and total is None):
                raise WeiboClientError(
                    "schema_changed",
                    "Weibo page did not contain posts or an affirmative empty result",
                )
        parsed_next = _bounded_non_negative_int(info.get("page"))
        next_page = parsed_next if parsed_next is not None and parsed_next > page else None
        return WeiboPage(rows, page, next_page, total)

    async def _mobile_json(self, url: httpx.URL) -> dict[str, Any]:
        await self._ensure_visitor(str(url))
        stale_sub = self._visitor_sub
        for visitor_attempt in range(2):
            response = await self._send(
                "GET",
                url,
                headers={"Accept": "application/json"},
                visitor_sub=self._visitor_sub,
            )
            if self._visitor_response_rejected(response):
                if visitor_attempt == 0:
                    await self._refresh_visitor(str(url), stale_sub=stale_sub)
                    stale_sub = self._visitor_sub
                    continue
                raise WeiboClientError(
                    "visitor_rejected",
                    "Weibo rejected a freshly generated anonymous visitor session",
                    status_code=response.status_code,
                )
            self._raise_for_status(response, context="mobile API")
            payload = self._json_object(response)
            if self._payload_requests_visitor(payload):
                if visitor_attempt == 0:
                    await self._refresh_visitor(str(url), stale_sub=stale_sub)
                    stale_sub = self._visitor_sub
                    continue
                raise WeiboClientError(
                    "visitor_rejected",
                    "Weibo rejected a freshly generated anonymous visitor session",
                    status_code=response.status_code,
                )
            self._require_ok(payload, context="mobile API")
            return payload
        raise WeiboClientError("visitor_rejected", "Weibo anonymous visitor refresh failed")

    async def _ensure_visitor(self, return_url: str) -> None:
        if self._visitor_sub is not None:
            return
        async with self._visitor_lock:
            if self._visitor_sub is None:
                await self._bootstrap_visitor_locked(return_url)

    async def _refresh_visitor(self, return_url: str, *, stale_sub: str | None) -> None:
        async with self._visitor_lock:
            # A concurrent request may already have replaced the rejected SUB.
            if self._visitor_sub is not None and self._visitor_sub != stale_sub:
                return
            self._visitor_sub = None
            await self._bootstrap_visitor_locked(return_url)

    async def _bootstrap_visitor_locked(self, return_url: str) -> None:
        entry = await self._send(
            "GET",
            WEIBO_VISITOR_ENTRY_URL,
            params={
                "entry": "sinawap",
                "a": "enter",
                "url": return_url,
                "domain": WEIBO_VISITOR_DOMAIN,
                "sudaref": "",
                "ua": WEIBO_VISITOR_SDK_USER_AGENT,
                "_rand": f"{time.time():.16f}",
            },
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        self._raise_for_status(entry, context="visitor bootstrap")
        _require_mime_type(
            entry,
            _VISITOR_ENTRY_MIME_TYPES,
            code="visitor_bootstrap_failed",
            context="visitor bootstrap",
        )
        request_id_match = _VISITOR_REQUEST_ID_RE.search(entry.text)
        return_url_match = _VISITOR_RETURN_URL_RE.search(entry.text)
        generate_call_match = _VISITOR_GENERATE_CALL_RE.search(entry.text)
        if not (request_id_match and return_url_match and generate_call_match):
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor page fields changed shape"
            )
        request_id = _safe_js_string(request_id_match.group("value"))
        parsed_return_url = _safe_js_string(return_url_match.group("value"))
        callback = generate_call_match.group("callback")
        version = generate_call_match.group("version")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor request id was malformed"
            )
        try:
            parsed_target = httpx.URL(parsed_return_url)
        except (httpx.InvalidURL, ValueError) as exc:
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor return URL was malformed"
            ) from exc
        if (
            parsed_return_url != return_url
            or parsed_target.scheme != "https"
            or parsed_target.host != "m.weibo.cn"
        ):
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor return URL did not match the request"
            )
        generated = await self._send(
            "POST",
            WEIBO_VISITOR_GENERATE_URL,
            data={
                "cb": callback,
                "ver": version,
                "request_id": request_id,
                "tid": "",
                "from": "weibo",
                "webdriver": "false",
                "rid": str(int(time.time() * 1000)),
                "return_url": parsed_return_url,
            },
            headers={"Accept": "text/javascript,application/javascript,*/*;q=0.1"},
        )
        self._raise_for_status(generated, context="visitor bootstrap")
        _require_mime_type(
            generated,
            _VISITOR_SCRIPT_MIME_TYPES,
            code="visitor_bootstrap_failed",
            context="visitor bootstrap",
        )
        payload = _visitor_jsonp_payload(generated.text, callback=callback)
        if not isinstance(payload, dict) or payload.get("retcode") not in (20000000, "20000000"):
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor service rejected the anonymous session"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping) or str(data.get("alt") or ""):
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor service requested account recovery"
            )
        visitor_sub = _safe_visitor_cookie(data.get("sub"))
        if not visitor_sub:
            raise WeiboClientError(
                "visitor_bootstrap_failed", "Weibo visitor service omitted the anonymous cookie"
            )
        self._visitor_sub = visitor_sub

    async def _pace(self) -> None:
        if self._request_interval_seconds <= 0:
            return
        now = time.monotonic()
        remaining = self._request_interval_seconds - (now - self._last_request_started_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_started_at = time.monotonic()

    async def _send(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        visitor_sub: str | None = None,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(2):
            async with self._request_lock:
                await self._pace()
                request_headers = {"User-Agent": WEIBO_USER_AGENT}
                if headers:
                    request_headers.update(headers)
                request = self._http.build_request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                    timeout=self._timeout_seconds,
                )
                # Never inherit account credentials from an injected client.
                request.headers.pop("authorization", None)
                request.headers.pop("proxy-authorization", None)
                request.headers.pop("cookie", None)
                if visitor_sub:
                    request.headers["Cookie"] = f"SUB={visitor_sub}"
                try:
                    # ``build_request`` materializes header defaults, but an
                    # injected ``AsyncClient(auth=...)`` applies its auth
                    # handler later, inside ``send``.  Passing ``auth=None``
                    # is therefore required in addition to stripping the
                    # already-built headers: anonymous discovery must never
                    # inherit an embedding application's account identity.
                    response = await self._http.send(
                        request,
                        auth=None,
                        follow_redirects=False,
                    )
                except httpx.TimeoutException as exc:
                    if attempt == 1:
                        raise WeiboClientError("timeout", "Weibo request timed out") from exc
                    response = None
                except httpx.TransportError as exc:
                    if attempt == 1:
                        raise WeiboClientError(
                            "network_error", "Weibo network request failed"
                        ) from exc
                    response = None
            if response is None or (response.status_code >= 500 and attempt == 0):
                if self._transient_retry_delay_seconds:
                    await asyncio.sleep(self._transient_retry_delay_seconds)
                continue
            return response
        if response is None:
            raise WeiboClientError("network_error", "Weibo network request failed")
        return response

    @staticmethod
    def _visitor_response_rejected(response: httpx.Response) -> bool:
        if response.status_code in _VISITOR_REFRESH_STATUSES - {302}:
            return True
        if response.status_code != 302:
            return False
        location = response.headers.get("Location", "")
        try:
            return httpx.URL(location).host in {
                "visitor.passport.weibo.cn",
                "passport.weibo.cn",
            }
        except (httpx.InvalidURL, ValueError):
            return False

    @staticmethod
    def _payload_requests_visitor(payload: Mapping[str, Any]) -> bool:
        ok = payload.get("ok")
        if ok in (-100, "-100"):
            return True
        message = str(payload.get("msg") or payload.get("message") or "").casefold()
        return any(marker in message for marker in ("游客", "visitor session", "登录后"))

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        _require_mime_type(
            response,
            _JSON_MIME_TYPES,
            code="schema_changed",
            context="JSON API",
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeiboClientError(
                "schema_changed",
                "Weibo returned invalid JSON",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise WeiboClientError(
                "schema_changed",
                "Weibo response shape changed",
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    def _require_ok(payload: Mapping[str, Any], *, context: str) -> None:
        ok = payload.get("ok")
        if isinstance(ok, bool) or ok not in (1, "1"):
            raise WeiboClientError(
                "upstream_rejected", f"Weibo {context} returned an unsuccessful result"
            )

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, context: str) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        if status == 429:
            raise WeiboClientError(
                "rate_limited",
                f"Weibo {context} rate limited this client",
                status_code=status,
                retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
            )
        if status == 400:
            raise WeiboClientError(
                "invalid_request", f"Weibo {context} rejected the request", status_code=status
            )
        if status == 404:
            raise WeiboClientError(
                "not_found", f"Weibo {context} was not found", status_code=status
            )
        if status in {401, 403}:
            raise WeiboClientError(
                "blocked", f"Weibo {context} denied anonymous access", status_code=status
            )
        if status >= 500:
            raise WeiboClientError(
                "upstream_error",
                f"Weibo {context} is temporarily unavailable",
                status_code=status,
            )
        raise WeiboClientError(
            "http_error", f"Weibo {context} returned HTTP {status}", status_code=status
        )
