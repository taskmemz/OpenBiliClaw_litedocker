from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import httpx
import pytest

from openbiliclaw.sources.v2ex_client import (
    V2EX_API_BASE_PATH,
    V2EX_BASE_URL,
    V2EX_MAX_RESPONSE_BYTES,
    V2EX_USER_AGENT,
    V2EXAPIError,
    V2EXClient,
    member_username,
    validate_v2ex_access_token,
    validate_v2ex_username,
)


def test_owned_v2ex_client_is_cn_direct_and_does_not_inherit_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubAsyncClient:
        async def aclose(self) -> None:
            return None

    def factory(**kwargs: object) -> StubAsyncClient:
        captured.update(kwargs)
        return StubAsyncClient()

    monkeypatch.setattr("openbiliclaw.sources.v2ex_client.httpx.AsyncClient", factory)

    V2EXClient()

    assert captured["base_url"] == V2EX_BASE_URL
    assert captured["trust_env"] is False
    assert "proxy" not in captured


@pytest.mark.asyncio
async def test_anonymous_client_reads_legacy_topics_and_json_feed_without_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["User-Agent"] == V2EX_USER_AGENT
        assert request.headers.get("Authorization") is None
        if request.url.path == "/api/topics/hot.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 123,
                        "title": "匿名热门主题",
                        "content": "主楼",
                        "created": 1,
                    }
                ],
            )
        if request.url.path == "/feed/programmer.json":
            return httpx.Response(
                200,
                json={
                    "version": "https://jsonfeed.org/version/1.1",
                    "items": [
                        {
                            "id": "https://www.v2ex.com/t/456",
                            "url": "https://www.v2ex.com/t/456",
                            "title": "Node 主题",
                            "content_html": "<p>正文</p>",
                            "date_published": "2026-08-09T00:00:00Z",
                            "author": {"name": "alice"},
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        hot = await client.get_hot(limit=5)
        node = await client.get_node_topics("programmer", limit=5)

    assert hot.data[0]["id"] == 123
    assert node.data[0]["id"] == "456"
    assert node.data[0]["content"] == "正文"
    assert node.data[0]["member"] == {"username": "alice"}
    assert [request.method for request in requests] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_bounded_search_relaxes_long_queries_to_distinctive_terms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path == "/api/topics/latest.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 101,
                        "title": "本地 Agent 工作流实践",
                        "content": "分享上下文压缩方案",
                    },
                    {
                        "id": 102,
                        "title": "旅行经验分享",
                        "content": "周末路线",
                    },
                ],
            )
        if request.url.path == "/api/topics/hot.json":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 103,
                        "title": "摄影器材讨论",
                        "content": "镜头选择",
                    }
                ],
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.search_topics("Agent 上下文管理 实践经验", limit=5)

    assert [row["id"] for row in page.data] == [101]


@pytest.mark.asyncio
async def test_bounded_search_prefers_exact_phrase_over_relaxed_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            {"id": 201, "title": "Agent 工具推荐", "content": ""},
            {"id": 202, "title": "Agent 上下文管理", "content": "完整实践"},
        ]
        if request.url.path == "/api/topics/latest.json":
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/topics/hot.json":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.search_topics("Agent 上下文管理", limit=5)

    assert [row["id"] for row in page.data] == [202, 201]


@pytest.mark.asyncio
async def test_bounded_search_does_not_expand_generic_only_queries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rows = [
            {"id": 301, "title": "一个经验分享", "content": "欢迎讨论"},
            {"id": 302, "title": "经验 分享", "content": "完整 query 命中仍保留"},
        ]
        if request.url.path == "/api/topics/latest.json":
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/topics/hot.json":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.search_topics("经验 分享", limit=5)

    assert [row["id"] for row in page.data] == [302]


@pytest.mark.asyncio
async def test_bounded_response_does_not_decode_gzip_twice() -> None:
    payload = json.dumps([{"id": 321, "title": "gzip topic"}]).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
        )

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.get_hot(limit=1)

    assert page.data == [{"id": 321, "title": "gzip topic"}]


@pytest.mark.asyncio
async def test_token_client_uses_api2_bearer_for_member_node_and_topic() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        assert request.method == "GET"
        if request.url.path == f"{V2EX_API_BASE_PATH}/member":
            return httpx.Response(200, json={"success": True, "result": {"username": "alice"}})
        if request.url.path == f"{V2EX_API_BASE_PATH}/nodes/python/topics":
            assert request.url.params["p"] == "2"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"id": 789, "title": "API 2.0 主题"}],
                },
            )
        if request.url.path == f"{V2EX_API_BASE_PATH}/topics/789":
            return httpx.Response(
                200,
                json={"success": True, "result": {"id": 789, "title": "详情"}},
            )
        if request.url.path == f"{V2EX_API_BASE_PATH}/topics/789/replies":
            assert request.url.params["p"] == "1"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [{"id": 1, "content": "reply"}],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(
            http_client=http_client,
            access_token="  pat-123  ",
            request_interval_seconds=0,
        )
        assert member_username(await client.get_member()) == "alice"
        page = await client.get_node_topics("python", page=2, limit=30)
        topic = await client.get_topic(789)
        replies = await client.get_topic_replies(789, limit=10)

    assert page.data == [{"id": 789, "title": "API 2.0 主题"}]
    assert page.total == 1
    assert topic["title"] == "详情"
    assert replies.data == [{"id": 1, "content": "reply"}]
    assert seen == [
        (f"{V2EX_API_BASE_PATH}/member", "Bearer pat-123"),
        (f"{V2EX_API_BASE_PATH}/nodes/python/topics", "Bearer pat-123"),
        (f"{V2EX_API_BASE_PATH}/topics/789", "Bearer pat-123"),
        (f"{V2EX_API_BASE_PATH}/topics/789/replies", "Bearer pat-123"),
    ]


@pytest.mark.asyncio
async def test_json_api_rejects_unsupported_content_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='[{"id": 123, "title": "not trusted"}]',
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(V2EXAPIError, match="unsupported content type") as error:
            await client.get_hot(limit=1)

    assert error.value.code == "schema_changed"
    assert "not trusted" not in str(error.value)


@pytest.mark.asyncio
async def test_member_feed_falls_back_to_xml_parser() -> None:
    xml = """<?xml version="1.0"?>
    <rss><channel><item>
      <guid>https://www.v2ex.com/t/321</guid>
      <link>https://www.v2ex.com/t/321</link>
      <title>XML 主题</title>
      <description><![CDATA[<p>XML 正文</p>]]></description>
      <author>bob</author>
      <pubDate>Sun, 09 Aug 2026 00:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/feed/member/bob.xml"
        return httpx.Response(200, text=xml, headers={"content-type": "application/rss+xml"})

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.get_member_topics("bob", limit=5)

    assert page.data[0]["id"] == "321"
    assert page.data[0]["title"] == "XML 主题"
    assert page.data[0]["content"] == "XML 正文"
    assert page.data[0]["member"] == {"username": "bob"}
    assert page.data[0]["created"] > 0


@pytest.mark.asyncio
async def test_atom_feed_reads_nested_author_name() -> None:
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>https://www.v2ex.com/t/322</id>
      <link href="https://www.v2ex.com/t/322" />
      <title>Atom topic</title>
      <author><name>alice</name></author>
      <published>2026-08-09T00:00:00Z</published>
      <content type="html">body</content>
    </entry></feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml, headers={"content-type": "application/atom+xml"})

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        page = await client.get_member_topics("alice", limit=5)

    assert page.data[0]["member"] == {"username": "alice"}
    assert page.data[0]["content"] == "body"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body"),
    (
        ("text/html", "<html><title>challenge</title></html>"),
        ("application/rss+xml", "<!DOCTYPE rss><rss><channel /></rss>"),
        ("text/plain", "<rss><channel /></rss>"),
    ),
)
async def test_feed_rejects_html_unsafe_xml_and_unsupported_envelopes(
    content_type: str,
    body: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": content_type})

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_member_topics("alice", limit=5)

    assert exc_info.value.code == "schema_changed"
    assert "challenge" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_response_body_is_rejected_before_parsing_when_declared_too_large() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(V2EX_MAX_RESPONSE_BYTES + 1),
            },
        )

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_hot()

    assert exc_info.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_anonymous_topic_detail_accepts_legacy_single_row_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/topics/show.json"
        return httpx.Response(200, json=[{"id": 789, "title": "legacy detail"}])

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        topic = await client.get_topic(789)

    assert topic == {"id": 789, "title": "legacy detail"}


@pytest.mark.asyncio
async def test_rate_limit_is_safe_and_exposes_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "Retry-After": "7",
                "X-Rate-Limit-Limit": "600",
                "X-Rate-Limit-Remaining": "0",
                "X-Rate-Limit-Reset": "1234567890",
            },
            text="private upstream details must not escape",
        )

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_hot()

    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.retry_after_seconds == 7
    assert "private upstream" not in str(exc_info.value)
    assert client.last_rate_limit == {"limit": 600, "remaining": 0, "reset": 1234567890}


@pytest.mark.asyncio
async def test_rate_limit_reset_header_is_treated_as_epoch_timestamp() -> None:
    reset_at = int(datetime.now(UTC).timestamp()) + 42

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-Rate-Limit-Reset": str(reset_at)})

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, request_interval_seconds=0)
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_hot()

    assert exc_info.value.retry_after_seconds is not None
    assert 39 <= exc_info.value.retry_after_seconds <= 42


@pytest.mark.asyncio
async def test_api2_success_false_never_leaks_upstream_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "message": "private detail", "result": []},
        )

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(
            http_client=http_client,
            access_token="pat",
            request_interval_seconds=0,
        )
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_member()

    assert exc_info.value.code == "invalid_request"
    assert "private detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_unauthorized_api2_probe_is_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"{V2EX_API_BASE_PATH}/member"
        return httpx.Response(401)

    async with httpx.AsyncClient(
        base_url=V2EX_BASE_URL, transport=httpx.MockTransport(handler)
    ) as http_client:
        client = V2EXClient(http_client=http_client, access_token="pat", request_interval_seconds=0)
        with pytest.raises(V2EXAPIError) as exc_info:
            await client.get_member()

    assert exc_info.value.code == "unauthorized"


def test_v2ex_input_validation_is_structural_and_defensive() -> None:
    assert validate_v2ex_username("  alice  ") == "alice"
    assert validate_v2ex_username("") == ""
    assert validate_v2ex_access_token("  pat-123  ") == "pat-123"
    assert validate_v2ex_access_token(None) == ""
    assert member_username({"username": " alice "}) == "alice"
    with pytest.raises(ValueError):
        validate_v2ex_username("bad/name")
    with pytest.raises(ValueError):
        validate_v2ex_access_token("bad token\n")
    with pytest.raises(V2EXAPIError, match="missing username"):
        member_username({})
