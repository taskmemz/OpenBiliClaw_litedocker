from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import openbiliclaw.sources.weibo as weibo_normalizer
import openbiliclaw.sources.weibo_client as weibo_module
from openbiliclaw.sources.weibo import WEIBO_SOURCE_MODES, weibo_post_to_content
from openbiliclaw.sources.weibo_client import (
    WEIBO_HOT_SEARCH_REFERER,
    WeiboClient,
    WeiboClientError,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "weibo"


def _fixture(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _visitor_html(
    return_url: str,
    *,
    request_id: str = "request-abc",
    ver: str = "20250916",
    callback: str = "visitor_gray_callback",
) -> str:
    return f"""
    <script>
      var return_url = {json.dumps(return_url)};
      var request_id = {json.dumps(request_id)};
      post('/visitor/genvisitor2', 'cb={callback}&ver={ver}&request_id=' + request_id);
    </script>
    """


def _visitor_jsonp(
    sub: str,
    *,
    alt: str = "",
    callback: str = "visitor_gray_callback",
) -> str:
    payload = {
        "retcode": 20000000,
        "msg": "succ",
        "data": {
            "tid": "anonymous-tid",
            "new_tid": True,
            "alt": alt,
            "next": "",
            "sub": sub,
            "subp": "anonymous-subp",
            "confidence": 1,
        },
    }
    return f"window.{callback} && {callback}({json.dumps(payload)});"


def _bootstrap_response(
    request: httpx.Request,
    *,
    sub: str = "anonymous-sub",
    callback: str = "visitor_gray_callback",
) -> httpx.Response:
    if request.url.path == "/visitor/visitor":
        assert request.method == "GET"
        assert request.headers.get("Cookie") is None
        assert request.headers.get("Authorization") is None
        assert request.url.params["entry"] == "sinawap"
        assert request.url.params["a"] == "enter"
        assert request.url.params["domain"] == ".weibo.cn"
        assert request.url.params["sudaref"] == ""
        assert request.url.params["ua"] == "php-sso_sdk_client-0.6.36"
        assert float(request.url.params["_rand"]) > 0
        return_url = request.url.params["url"]
        return httpx.Response(
            200,
            text=_visitor_html(return_url, callback=callback),
            headers={"Content-Type": "text/html"},
        )
    if request.url.path == "/visitor/genvisitor2":
        assert request.method == "POST"
        assert request.headers.get("Cookie") is None
        assert request.headers.get("Authorization") is None
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        assert form["cb"] == [callback]
        assert form["ver"] == ["20250916"]
        assert form["request_id"] == ["request-abc"]
        assert form["tid"] == [""]
        assert form["from"] == ["weibo"]
        assert form["webdriver"] == ["false"]
        assert form["rid"][0].isdigit()
        assert form["return_url"][0].startswith("https://m.weibo.cn/api/container/getIndex?")
        return httpx.Response(
            200,
            text=_visitor_jsonp(sub, callback=callback),
            headers={"Content-Type": "text/javascript; charset=utf-8"},
        )
    raise AssertionError(f"unexpected bootstrap request: {request.method} {request.url}")


@pytest.mark.asyncio
async def test_search_bootstraps_anonymous_visitor_and_extracts_nested_rows() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "visitor.passport.weibo.cn":
            return _bootstrap_response(request, callback="visitor_callback_42")
        assert request.url.host == "m.weibo.cn"
        assert request.url.path == "/api/container/getIndex"
        assert request.url.params["containerid"] == "100103type=1&q=科技"
        assert request.url.params["page"] == "2"
        assert request.headers["Cookie"] == "SUB=anonymous-sub"
        assert request.headers.get("Authorization") is None
        return httpx.Response(
            200,
            json={
                "ok": 1,
                "data": {
                    "cardlistInfo": {"page": 3, "total": "123"},
                    "cards": [
                        {"card_type": 9, "mblog": {"id": "one"}},
                        "malformed-card",
                        {
                            "card_type": 11,
                            "card_group": [
                                {"card_type": 9, "mblog": {"id": "two"}},
                                {"card_type": 6, "desc": "not a post"},
                            ],
                        },
                    ],
                },
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer must-not-leak", "Cookie": "SUB=user-cookie"},
    )
    client = WeiboClient(http_client=http_client, request_interval_seconds=0)
    page = await client.search_posts("  科技  ", page=2, limit=10)
    await client.aclose()

    assert [row["id"] for row in page.data] == ["one", "two"]
    assert page.items is page.data
    assert page.rows is page.data
    assert page.page == 2
    assert page.next_page == 3
    assert page.total == 123
    assert len(requests) == 3
    assert http_client.is_closed is False
    await http_client.aclose()


@pytest.mark.asyncio
async def test_creator_posts_reuses_visitor_and_uses_creator_container() -> None:
    bootstrap_calls = 0
    creator_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bootstrap_calls, creator_calls
        if request.url.host == "visitor.passport.weibo.cn":
            bootstrap_calls += 1
            return _bootstrap_response(request)
        creator_calls += 1
        assert request.url.params["containerid"] == "1076032803301701"
        assert request.url.params["page"] == "4"
        assert request.headers["Cookie"] == "SUB=anonymous-sub"
        return httpx.Response(
            200,
            json={
                "ok": 1,
                "data": {
                    "cardlistInfo": {"page": 5, "total": 10},
                    "cards": [{"card_type": 9, "mblog": {"mid": "post-1"}}],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        first = await client.creator_posts("2803301701", page=4, limit=1)
        second = await client.creator_posts(2803301701, page=4, limit=1)

    assert first.data == second.data == [{"mid": "post-1"}]
    assert bootstrap_calls == 2  # entry GET + genvisitor2 POST, once per client lifetime
    assert creator_calls == 2


@pytest.mark.asyncio
async def test_mobile_432_refreshes_visitor_once() -> None:
    bootstrap_round = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bootstrap_round, api_calls
        if request.url.host == "visitor.passport.weibo.cn":
            if request.url.path == "/visitor/visitor":
                bootstrap_round += 1
                return_url = request.url.params["url"]
                return httpx.Response(
                    200,
                    text=_visitor_html(return_url),
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(
                200,
                text=_visitor_jsonp(f"anonymous-{bootstrap_round}"),
                headers={"Content-Type": "text/javascript; charset=utf-8"},
            )
        api_calls += 1
        if api_calls == 1:
            assert request.headers["Cookie"] == "SUB=anonymous-1"
            return httpx.Response(432)
        assert request.headers["Cookie"] == "SUB=anonymous-2"
        return httpx.Response(
            200,
            json={"ok": 1, "data": {"cardlistInfo": {}, "cards": []}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        page = await client.search_posts("科技", limit=5)

    assert page.data == []
    assert bootstrap_round == 2
    assert api_calls == 2


@pytest.mark.asyncio
async def test_repeated_visitor_rejection_is_typed_and_not_looped() -> None:
    bootstrap_round = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bootstrap_round, api_calls
        if request.url.host == "visitor.passport.weibo.cn":
            if request.url.path == "/visitor/visitor":
                bootstrap_round += 1
                return httpx.Response(
                    200,
                    text=_visitor_html(request.url.params["url"]),
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(
                200,
                text=_visitor_jsonp(f"anonymous-{bootstrap_round}"),
                headers={"Content-Type": "text/javascript; charset=utf-8"},
            )
        api_calls += 1
        return httpx.Response(403, text="unsafe upstream body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.search_posts("科技", limit=5)

    assert exc_info.value.code == "visitor_rejected"
    assert exc_info.value.status_code == 403
    assert "unsafe upstream body" not in str(exc_info.value)
    assert bootstrap_round == 2
    assert api_calls == 2


@pytest.mark.asyncio
async def test_ok_minus_100_refreshes_once_then_succeeds() -> None:
    bootstrap_round = 0
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal bootstrap_round, api_calls
        if request.url.host == "visitor.passport.weibo.cn":
            if request.url.path == "/visitor/visitor":
                bootstrap_round += 1
                return httpx.Response(
                    200,
                    text=_visitor_html(request.url.params["url"]),
                    headers={"Content-Type": "text/html"},
                )
            return httpx.Response(
                200,
                text=_visitor_jsonp(f"anonymous-{bootstrap_round}"),
                headers={"Content-Type": "text/javascript; charset=utf-8"},
            )
        api_calls += 1
        if api_calls == 1:
            return httpx.Response(200, json={"ok": -100, "msg": "请登录后查看"})
        return httpx.Response(
            200,
            json={"ok": 1, "data": {"cardlistInfo": {}, "cards": []}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        page = await client.search_posts("科技")

    assert page.data == []
    assert bootstrap_round == 2
    assert api_calls == 2


@pytest.mark.asyncio
async def test_hot_topics_is_cookie_free_referered_and_defensive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://weibo.com/ajax/side/hotSearch")
        assert request.headers["Referer"] == WEIBO_HOT_SEARCH_REFERER
        assert request.headers.get("Cookie") is None
        assert request.headers.get("Authorization") is None
        return httpx.Response(
            200,
            json={
                "ok": 1,
                "data": {
                    "realtime": [
                        {"word": "topic-one", "realpos": 1},
                        "malformed",
                        {"word": "topic-two", "realpos": 2},
                    ]
                },
                "logs": {},
                "topLogs": {},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer must-not-leak", "Cookie": "SUB=user-cookie"},
    ) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        page = await client.hot_topics(limit=1)
        alias_rows = await client.hot_search(limit=2)

    assert page.data == [{"word": "topic-one", "realpos": 1}]
    assert page.total == 2
    assert [row["word"] for row in alias_rows] == ["topic-one", "topic-two"]


@pytest.mark.asyncio
async def test_injected_client_auth_handler_is_never_applied() -> None:
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"ok": 1, "data": {"realtime": []}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=httpx.BasicAuth("account", "must-not-leak"),
    ) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        page = await client.hot_topics()

    assert page.data == []
    assert seen_authorization == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["timeout", "upstream"])
async def test_transient_hot_failure_retries_once(first_failure: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if first_failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": 1, "data": {"realtime": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(
            http_client=http_client,
            request_interval_seconds=0,
            transient_retry_delay_seconds=0,
        )
        page = await client.hot_topics()

    assert page.data == []
    assert calls == 2


@pytest.mark.asyncio
async def test_rate_limit_exposes_bounded_retry_after() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "120"})
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.hot_topics()

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (["wrong top container"], "schema_changed"),
        ({"ok": 0, "msg": "empty"}, "upstream_rejected"),
        ({"ok": 1, "data": []}, "schema_changed"),
        ({"ok": 1, "data": {"realtime": "wrong"}}, "schema_changed"),
    ],
)
async def test_hot_schema_and_unsuccessful_results_are_typed(payload: object, code: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.hot_topics()
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_json_endpoint_rejects_wrong_content_type_before_parsing() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=json.dumps({"ok": 1, "data": {"realtime": []}}),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.hot_topics()

    assert exc_info.value.code == "schema_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cards", "total", "accepted"),
    [
        ([{"card_type": 4}, {"card_type": 11, "card_group": []}], 0, True),
        ([{"card_type": 4}, {"card_type": 11, "card_group": []}], 2, False),
        ([], None, True),
        ([], 2, False),
    ],
)
async def test_mobile_empty_result_requires_affirmative_terminal_evidence(
    cards: list[object],
    total: int | None,
    accepted: bool,
) -> None:
    info = {} if total is None else {"total": total}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"ok": 1, "data": {"cards": cards, "cardlistInfo": info}},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        client._visitor_sub = "anonymous-test"  # noqa: SLF001 - isolate page semantics
        if accepted:
            page = await client.search_posts("guaranteed-empty", limit=5)
            assert page.data == []
        else:
            with pytest.raises(WeiboClientError) as exc_info:
                await client.search_posts("schema-drift", limit=5)
            assert exc_info.value.code == "schema_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected_ids", "error_code"),
    [
        ("search-success.redacted.json", ["redacted-post-id"], ""),
        ("search-empty.redacted.json", [], ""),
        ("search-schema-drift.redacted.json", [], "schema_changed"),
    ],
)
async def test_redacted_real_search_fixtures_lock_terminal_semantics(
    fixture_name: str,
    expected_ids: list[str],
    error_code: str,
) -> None:
    payload = _fixture(fixture_name)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        client._visitor_sub = "anonymous-test"  # noqa: SLF001 - isolate fixture semantics
        if error_code:
            with pytest.raises(WeiboClientError) as exc_info:
                await client.search_posts("fixture", limit=5)
            assert exc_info.value.code == error_code
        else:
            page = await client.search_posts("fixture", limit=5)
            assert [str(row.get("id", "")) for row in page.data] == expected_ids


@pytest.mark.asyncio
async def test_visitor_schema_change_is_typed_without_cookie_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Cookie") is None
        return httpx.Response(
            200,
            text="<html>changed</html>",
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"Cookie": "SUB=user-cookie"}
    ) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.search_posts("科技")
    assert exc_info.value.code == "visitor_bootstrap_failed"


@pytest.mark.asyncio
async def test_malformed_visitor_return_url_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=_visitor_html("https://[invalid"),
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(WeiboClientError) as exc_info:
            await client.search_posts("科技")

    assert exc_info.value.code == "visitor_bootstrap_failed"


@pytest.mark.asyncio
async def test_owned_client_is_direct_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_async_client = httpx.AsyncClient
    captured: dict[str, object] = {}
    created: list[httpx.AsyncClient] = []

    def factory(**kwargs: object) -> httpx.AsyncClient:
        captured.update(kwargs)
        client = real_async_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": 1, "data": {"realtime": []}})
            ),
            timeout=kwargs["timeout"],
            follow_redirects=bool(kwargs["follow_redirects"]),
            trust_env=bool(kwargs["trust_env"]),
        )
        created.append(client)
        return client

    monkeypatch.setattr(weibo_module.httpx, "AsyncClient", factory)
    client = WeiboClient(request_interval_seconds=0)
    await client.hot_topics()
    await client.aclose()

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert created[0].is_closed is True


@pytest.mark.asyncio
async def test_local_validation_rejects_unsafe_or_empty_inputs_without_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(ValueError):
            await client.creator_posts("1/../../etc", limit=1)
        with pytest.raises(ValueError):
            await client.search_posts("", limit=1)
    assert calls == 0


@pytest.mark.asyncio
async def test_limit_zero_short_circuits_without_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = WeiboClient(http_client=http_client, request_interval_seconds=0)
        search_page = await client.search_posts("科技", limit=0)
        creator_page = await client.creator_posts("2803301701", limit=0)
        hot_page = await client.hot_topics(limit=0)

    assert search_page.data == creator_page.data == hot_page.data == []
    assert calls == 0


def test_normalizer_creator_mode_text_fallback_dict_pics_and_real_reads() -> None:
    content = weibo_post_to_content(
        {
            "id": "5329000000000001",
            "bid": "RcExample",
            # A malformed truthy value must not swallow the valid HTML fallback.
            "text_raw": {"unexpected": "container"},
            "text": "<p>合法正文 &amp; 更多</p>",
            "user": {"id": 42, "screen_name": "作者"},
            "reads_count": "1,234",
            "attitudes_count": "8",
            "comments_count": 13,
            "reposts_count": "21",
            "favorites_count": 999,
            "pics": {
                "pic-a": {
                    "url": "https://wx1.sinaimg.cn/thumbnail/thumb.jpg",
                    "large": {"url": "https://wx1.sinaimg.cn/large/large.jpg"},
                }
            },
        },
        strategy="weibo-creator",
    )

    assert content is not None
    assert content.body_text == "合法正文 & 更多"
    assert content.cover_url == "https://wx1.sinaimg.cn/large/large.jpg"
    assert content.view_count == 1234
    assert content.like_count == 8
    assert content.comment_count == 13
    assert content.reply_count == 13
    assert content.share_count == 21
    assert content.retweet_count == 21
    assert content.favorite_count == 0
    assert content.source_strategy == "weibo-creator"
    assert "creator" in WEIBO_SOURCE_MODES


def test_normalizer_published_time_is_utc_and_invalid_or_future_is_neutral() -> None:
    fixed_now = weibo_normalizer.datetime(2026, 8, 10, 12, 0, tzinfo=weibo_normalizer.UTC)

    assert (
        weibo_normalizer._published_at(  # noqa: SLF001 - source boundary contract
            "Sun Aug 09 13:45:29 +0800 2026", now=fixed_now
        )
        == "2026-08-09T05:45:29Z"
    )
    assert weibo_normalizer._published_at("not-a-time", now=fixed_now) == ""  # noqa: SLF001
    assert (
        weibo_normalizer._published_at(  # noqa: SLF001
            "Tue Aug 11 13:45:29 +0800 2026", now=fixed_now
        )
        == ""
    )


def test_normalizer_accepts_single_pic_object_and_has_no_synthetic_hot_candidate() -> None:
    content = weibo_post_to_content(
        {
            "mid": "5329000000000002",
            "text": "正文",
            "pics": {
                "url": "https://wx1.sinaimg.cn/thumbnail/thumb.jpg",
                "large": {"url": "https://wx1.sinaimg.cn/large/large.jpg"},
            },
        }
    )

    assert content is not None
    assert content.cover_url == "https://wx1.sinaimg.cn/large/large.jpg"
    assert not hasattr(weibo_normalizer, "weibo_hot_topic_to_content")
