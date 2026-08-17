"""Tests for search-backed discovery inspiration provider plumbing."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from openbiliclaw.discovery.inspiration import ExaPreviewItem
from openbiliclaw.discovery.inspiration_provider import (
    BangumiPlatformSearchBackend,
    BilibiliPlatformSearchBackend,
    BingRssInspirationProvider,
    DouyinPlatformSearchBackend,
    ExaInspirationProvider,
    FallbackInspirationSearchProvider,
    LocalInspirationProvider,
    McporterExaInspirationProvider,
    McporterYouInspirationProvider,
    PlatformSourceInspirationProvider,
    RedditPlatformSearchBackend,
    V2EXPlatformSearchBackend,
    WeiboPlatformSearchBackend,
    XhsPlatformSearchBackend,
    XPlatformSearchBackend,
    YouInspirationProvider,
    YoutubePlatformSearchBackend,
    ZhihuPlatformSearchBackend,
    build_inspiration_search_provider,
    build_platform_source_backends,
    parse_exa_search_payload,
    parse_you_search_payload,
)


def test_parse_exa_search_payload_accepts_result_objects() -> None:
    payload = json.dumps(
        {
            "results": [
                {
                    "title": "EUV 光刻胶产业链瓶颈",
                    "url": "https://example.test/euv",
                    "highlights": ["光刻胶", "EUV", ""],
                },
                {
                    "title": "No URL",
                    "highlights": ["ignored"],
                },
            ]
        },
        ensure_ascii=False,
    )

    assert parse_exa_search_payload(payload) == [
        ExaPreviewItem(
            title="EUV 光刻胶产业链瓶颈",
            url="https://example.test/euv",
            highlights=("光刻胶", "EUV"),
        )
    ]


def test_inspiration_providers_expose_stable_backend_aliases() -> None:
    assert McporterExaInspirationProvider().backend_alias == "exa"
    assert McporterYouInspirationProvider().backend_alias == "you"
    assert PlatformSourceInspirationProvider([]).backend_alias == "platform_sources"
    assert LocalInspirationProvider(object()).backend_alias == "local_cache"


def test_parse_exa_search_payload_accepts_mcp_text_content() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": "\n".join(
                    [
                        "Title: 空间音频工作流实践",
                        "URL: https://example.test/audio",
                        "Published: N/A",
                        "Author: example",
                        "Highlights:",
                        "Ambisonics",
                        "...",
                        "Reaper 空间音频",
                    ]
                ),
            }
        ]
    }

    assert parse_exa_search_payload(payload) == [
        ExaPreviewItem(
            title="空间音频工作流实践",
            url="https://example.test/audio",
            highlights=("Ambisonics", "Reaper 空间音频"),
        )
    ]


def test_parse_exa_search_payload_accepts_mcporter_raw_text() -> None:
    payload = """{
  content: [
    {
      type: 'text',
      text: 'Title: 城市漫步影像志\\n' +
        'URL: https://example.test/walk\\n' +
        'Published: N/A\\n' +
        'Author: example\\n' +
        'Highlights:\\n' +
        '街区观察\\n' +
        '...\\n' +
        '地方感\\n',
      _meta: [Object]
    }
  ]
}"""

    assert parse_exa_search_payload(payload) == [
        ExaPreviewItem(
            title="城市漫步影像志",
            url="https://example.test/walk",
            highlights=("街区观察", "地方感"),
        )
    ]


async def test_mcporter_exa_provider_builds_call_and_parses_output() -> None:
    calls: list[list[str]] = []

    async def fake_runner(args: list[str], timeout_seconds: float) -> str:
        calls.append(args)
        assert timeout_seconds == 8.0
        return json.dumps(
            {
                "results": [
                    {
                        "title": "环境叙事设计",
                        "url": "https://example.test/story",
                        "highlights": ["环境叙事"],
                    }
                ]
            },
            ensure_ascii=False,
        )

    provider = McporterExaInspirationProvider(
        runner=fake_runner,
        timeout_seconds=8.0,
    )

    results = await provider.search("独立游戏 叙事设计", limit=3)

    assert results == [
        ExaPreviewItem(
            title="环境叙事设计",
            url="https://example.test/story",
            highlights=("环境叙事",),
        )
    ]
    assert len(calls) == 1
    assert calls[0] == [
        "mcporter",
        "call",
        "exa.web_search_exa",
        "query=独立游戏 叙事设计",
        "numResults=3",
        "--output",
        "raw",
    ]


async def test_mcporter_exa_provider_raises_on_mcporter_error_payload() -> None:
    async def fake_runner(args: list[str], timeout_seconds: float) -> str:
        return json.dumps(
            {
                "server": "exa",
                "tool": "web_search_exa",
                "error": "You've hit Exa's free MCP rate limit.",
            }
        )

    provider = McporterExaInspirationProvider(runner=fake_runner)

    with pytest.raises(RuntimeError, match="free MCP rate limit"):
        await provider.search("switch indie game hidden gems", limit=3)


async def test_build_provider_defaults_remote_timeout_below_planner_timeout() -> None:
    seen_timeouts: list[float] = []

    async def fake_runner(args: list[str], timeout_seconds: float) -> str:
        seen_timeouts.append(timeout_seconds)
        return json.dumps(
            {
                "results": [
                    {
                        "title": "Switch repair guide",
                        "url": "https://example.test/switch",
                    }
                ]
            }
        )

    provider = build_inspiration_search_provider(["exa"], runner=fake_runner)
    assert provider is not None

    await provider.search("Switch repair", limit=1)

    assert seen_timeouts == [6.0]


def test_build_provider_skips_mcporter_backends_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    provider = build_inspiration_search_provider(["exa", "you"])

    assert provider is None


async def test_exa_direct_provider_calls_api_and_parses_results() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-exa-key"
        assert request.url.host == "api.exa.ai"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Direct Exa result",
                        "url": "https://example.test/exa",
                        "highlights": ["one", "two"],
                    }
                ]
            },
        )

    provider = ExaInspirationProvider(
        api_key="test-exa-key",
        transport=httpx.MockTransport(handler),
    )

    items = await provider.search("test query", limit=3)

    assert items == [
        ExaPreviewItem(
            title="Direct Exa result",
            url="https://example.test/exa",
            highlights=("one", "two"),
        )
    ]


async def test_you_direct_provider_calls_api_and_parses_hits() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-you-key"
        assert request.url.host == "api.ydc-index.io"
        assert request.url.params["query"] == "test query"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "title": "Direct You result",
                        "url": "https://example.test/you",
                        "snippets": ["snippet one", "snippet two"],
                    }
                ]
            },
        )

    provider = YouInspirationProvider(
        api_key="test-you-key",
        transport=httpx.MockTransport(handler),
    )

    items = await provider.search("test query", limit=3)

    assert items == [
        ExaPreviewItem(
            title="Direct You result",
            url="https://example.test/you",
            highlights=("snippet one", "snippet two"),
        )
    ]


async def test_bing_rss_provider_parses_real_shaped_rss() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.bing.com"
        assert request.url.params["format"] == "rss"
        assert request.url.params["q"] == "test query"
        return httpx.Response(
            200,
            content=(
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<rss version="2.0"><channel><title>Bing: test query</title>'
                b"<item><title>Result one</title>"
                b"<link>https://example.test/one</link>"
                b"<description><b>bold</b> snippet &amp; more</description></item>"
                b"<item><title></title><link>https://example.test/bad</link>"
                b"<description>x</description></item>"
                b"<item><title>Result three</title>"
                b"<link>https://example.test/three</link>"
                b"<description>third snippet</description></item>"
                b"</channel></rss>"
            ),
        )

    provider = BingRssInspirationProvider(transport=httpx.MockTransport(handler))

    items = await provider.search("test query", limit=3)

    assert items == [
        ExaPreviewItem(
            title="Result one",
            url="https://example.test/one",
            highlights=("bold snippet & more",),
        ),
        ExaPreviewItem(
            title="Result three",
            url="https://example.test/three",
            highlights=("third snippet",),
        ),
    ]


def test_parse_you_search_payload_accepts_structured_results() -> None:
    payload = json.dumps(
        {
            "results": {
                "web": [
                    {
                        "title": "Switch OLED shell swap guide",
                        "url": "https://example.test/switch-shell",
                        "snippets": [
                            "A practical teardown walkthrough for Switch OLED shells.",
                            "",
                            "Lists tools and common mistakes.",
                        ],
                    }
                ],
                "news": [
                    {
                        "title": "Nintendo repair policy update",
                        "url": "https://example.test/repair-news",
                        "description": "Repair coverage context.",
                    }
                ],
            }
        },
        ensure_ascii=False,
    )

    assert parse_you_search_payload(payload) == [
        ExaPreviewItem(
            title="Switch OLED shell swap guide",
            url="https://example.test/switch-shell",
            highlights=(
                "A practical teardown walkthrough for Switch OLED shells.",
                "Lists tools and common mistakes.",
            ),
        ),
        ExaPreviewItem(
            title="Nintendo repair policy update",
            url="https://example.test/repair-news",
            highlights=("Repair coverage context.",),
        ),
    ]


def test_parse_you_search_payload_accepts_mcp_json_text() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    [
                        {
                            "title": "Anime production committee explained",
                            "url": "https://example.test/anime-committee",
                            "snippets": ["Explains funding, rights, and studio incentives."],
                        }
                    ]
                ),
            }
        ]
    }

    assert parse_you_search_payload(payload) == [
        ExaPreviewItem(
            title="Anime production committee explained",
            url="https://example.test/anime-committee",
            highlights=("Explains funding, rights, and studio incentives.",),
        )
    ]


async def test_mcporter_you_provider_builds_call_and_limits_results() -> None:
    calls: list[list[str]] = []

    async def fake_runner(args: list[str], timeout_seconds: float) -> str:
        calls.append(args)
        assert timeout_seconds == 9.0
        return json.dumps(
            [
                {
                    "title": "Studio Trigger production notes",
                    "url": "https://example.test/trigger",
                    "snippets": ["Animation production notes."],
                },
                {
                    "title": "Kyoto Animation background art process",
                    "url": "https://example.test/kyoani",
                    "snippets": ["Background art process."],
                },
            ],
            ensure_ascii=False,
        )

    provider = McporterYouInspirationProvider(
        runner=fake_runner,
        timeout_seconds=9.0,
    )

    results = await provider.search("anime production workflow explained", limit=1)

    assert results == [
        ExaPreviewItem(
            title="Studio Trigger production notes",
            url="https://example.test/trigger",
            highlights=("Animation production notes.",),
        )
    ]
    assert calls == [
        [
            "mcporter",
            "call",
            "you.you-search",
            "query=anime production workflow explained",
            "--output",
            "raw",
        ]
    ]


async def test_fallback_inspiration_provider_uses_you_after_exa_failure() -> None:
    class FailingProvider:
        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            raise RuntimeError("Exa free MCP rate limit")

    class WorkingProvider:
        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            assert query == "Nintendo Switch repair guide"
            assert limit == 2
            return [
                ExaPreviewItem(
                    title="Joy-Con repair workflow",
                    url="https://example.test/joycon",
                    highlights=("Repair workflow.",),
                )
            ]

    provider = FallbackInspirationSearchProvider([FailingProvider(), WorkingProvider()])

    assert await provider.search("Nintendo Switch repair guide", limit=2) == [
        ExaPreviewItem(
            title="Joy-Con repair workflow",
            url="https://example.test/joycon",
            highlights=("Repair workflow.",),
        )
    ]


async def test_fallback_inspiration_provider_cooldowns_failed_backend() -> None:
    now = 1000.0

    class FailingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.calls += 1
            raise RuntimeError("provider offline")

    class WorkingProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.calls += 1
            return [
                ExaPreviewItem(
                    title=f"{query} result",
                    url=f"https://example.test/{self.calls}",
                    highlights=(),
                )
            ]

    failing = FailingProvider()
    working = WorkingProvider()
    provider = FallbackInspirationSearchProvider(
        [failing, working],
        error_cooldown_seconds=60.0,
        clock=lambda: now,
    )

    assert await provider.search("first", limit=1)
    assert await provider.search("second", limit=1)

    assert failing.calls == 1
    assert working.calls == 2


async def test_fallback_inspiration_provider_augments_low_diversity_platform_results() -> None:
    class PlatformOnlyProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.calls += 1
            return [
                ExaPreviewItem(
                    title="reddit only result",
                    url="https://reddit.example/switch",
                    highlights=("single platform",),
                )
            ]

        def grounding_ledger(self) -> dict[str, object]:
            return {
                "platforms": {"reddit": self.calls},
                "timeouts": self.calls,
                "skipped_cooldown": 0,
                "skipped_budget": 0,
            }

    class WebProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.calls += 1
            assert query == "Switch repair"
            assert limit == 2
            return [
                ExaPreviewItem(
                    title="web repair guide",
                    url="https://example.test/switch-repair",
                    highlights=("external grounding",),
                )
            ]

    platform = PlatformOnlyProvider()
    web = WebProvider()
    provider = FallbackInspirationSearchProvider([platform, web])

    results = await provider.search("Switch repair", limit=2)

    assert [item.url for item in results] == [
        "https://reddit.example/switch",
        "https://example.test/switch-repair",
    ]
    assert platform.calls == 1
    assert web.calls == 1
    assert provider.grounding_ledger()["provider_successes"] == {
        "PlatformOnlyProvider": 1,
        "WebProvider": 1,
    }
    assert provider.grounding_ledger()["provider_augmentations"] == 1


async def test_fallback_inspiration_provider_ledger_records_failed_supplemental_backend() -> None:
    class PlatformOnlyProvider:
        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            return [
                ExaPreviewItem(
                    title="reddit only result",
                    url="https://reddit.example/switch",
                    highlights=(),
                )
            ]

        def grounding_ledger(self) -> dict[str, object]:
            return {
                "platforms": {"reddit": 1},
                "timeouts": 1,
                "skipped_cooldown": 0,
                "skipped_budget": 0,
            }

    class FailingProvider:
        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            raise RuntimeError("rate limit")

    provider = FallbackInspirationSearchProvider(
        [PlatformOnlyProvider(), FailingProvider()],
        error_cooldown_seconds=0,
    )

    results = await provider.search("Switch repair", limit=2)

    assert [item.url for item in results] == ["https://reddit.example/switch"]
    assert provider.grounding_ledger()["provider_failures"] == {"FailingProvider": 1}


async def test_local_inspiration_provider_maps_database_rows_to_previews() -> None:
    class DB:
        def search_local_inspiration_evidence(
            self,
            query: str,
            *,
            limit: int,
            lookback_days: int,
        ) -> list[dict[str, object]]:
            assert query == "独立游戏 机制"
            assert limit == 3
            assert lookback_days == 30
            return [
                {
                    "title": "独立游戏机制拆解",
                    "url": "https://example.test/game",
                    "highlights": ["地图叙事", "关卡设计"],
                    "source_table": "content_cache",
                    "source_platform": "bilibili",
                    "topic_label": "独立游戏",
                }
            ]

    provider = LocalInspirationProvider(DB(), min_results=1)

    assert await provider.search("独立游戏 机制", limit=3) == [
        ExaPreviewItem(
            title="独立游戏机制拆解",
            url="https://example.test/game",
            highlights=("地图叙事", "关卡设计"),
        )
    ]
    ledger = provider.grounding_ledger()
    assert ledger["local_hits"] == 1
    assert ledger["local_misses"] == 0
    assert ledger["local_sources"] == {"content_cache": 1}


async def test_local_inspiration_provider_misses_when_below_sufficiency() -> None:
    class DB:
        def search_local_inspiration_evidence(
            self,
            query: str,
            *,
            limit: int,
            lookback_days: int,
        ) -> list[dict[str, object]]:
            return [
                {
                    "title": "只有一条",
                    "url": "https://example.test/one",
                    "highlights": [],
                    "source_table": "content_cache",
                    "source_platform": "bilibili",
                    "topic_label": "",
                }
            ]

    provider = LocalInspirationProvider(DB(), min_results=2)

    assert await provider.search("独立游戏 机制", limit=3) == []
    assert provider.grounding_ledger()["local_hits"] == 0
    assert provider.grounding_ledger()["local_misses"] == 1


async def test_local_insufficiency_falls_through_to_next_provider() -> None:
    class EmptyDB:
        def search_local_inspiration_evidence(
            self,
            query: str,
            *,
            limit: int,
            lookback_days: int,
        ) -> list[dict[str, object]]:
            return []

    class StubProvider:
        backend_alias = "stub"

        def begin_stage(self) -> None:
            pass

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            return [ExaPreviewItem(title="外部证据", url="https://example.test/ext")]

    provider = FallbackInspirationSearchProvider(
        [LocalInspirationProvider(EmptyDB()), StubProvider()]
    )

    result = await provider.search("独立游戏 机制", limit=3)

    assert [item.title for item in result] == ["外部证据"]
    assert provider.last_search_provider == "stub"
    assert provider.grounding_ledger()["local_misses"] == 1


async def test_last_search_provider_reports_local_when_local_serves() -> None:
    class DB:
        def search_local_inspiration_evidence(
            self,
            query: str,
            *,
            limit: int,
            lookback_days: int,
        ) -> list[dict[str, object]]:
            return [
                {
                    "title": "本地证据",
                    "url": "https://example.test/local",
                    "highlights": [],
                    "source_table": "content_cache",
                    "source_platform": "bilibili",
                    "topic_label": "",
                }
            ]

    provider = FallbackInspirationSearchProvider([LocalInspirationProvider(DB(), min_results=1)])

    await provider.search("独立游戏 机制", limit=3)

    assert provider.last_search_provider == "local_cache"


async def test_local_sufficient_hit_does_not_augment_to_external_provider() -> None:
    class DB:
        def search_local_inspiration_evidence(
            self,
            query: str,
            *,
            limit: int,
            lookback_days: int,
        ) -> list[dict[str, object]]:
            return [
                {
                    "title": "本地证据",
                    "url": "https://example.test/local",
                    "highlights": [],
                    "source_table": "content_cache",
                    "source_platform": "bilibili",
                    "topic_label": "",
                }
            ]

    class ExternalProvider:
        backend_alias = "exa"

        def __init__(self) -> None:
            self.called = False

        def begin_stage(self) -> None:
            pass

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.called = True
            return [ExaPreviewItem(title="外部证据", url="https://example.test/ext")]

    external = ExternalProvider()
    provider = FallbackInspirationSearchProvider(
        [LocalInspirationProvider(DB(), min_results=1), external]
    )

    result = await provider.search("独立游戏 机制", limit=3)

    assert [item.title for item in result] == ["本地证据"]
    assert external.called is False
    assert provider.last_search_provider == "local_cache"


async def test_platform_source_provider_rotates_enabled_sources_and_caps_fanout() -> None:
    class Backend:
        def __init__(self, platform: str) -> None:
            self.platform = platform
            self.queries: list[str] = []

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.queries.append(query)
            return [
                ExaPreviewItem(
                    title=f"{self.platform}:{query}",
                    url=f"https://example.test/{self.platform}",
                    highlights=(f"limit={limit}",),
                )
            ]

    bili = Backend("bilibili")
    youtube = Backend("youtube")
    reddit = Backend("reddit")
    provider = PlatformSourceInspirationProvider(
        [bili, youtube, reddit],
        platforms_per_query=2,
    )

    first = await provider.search("switch indie", limit=4)
    second = await provider.search("anime workflow", limit=4)

    assert [item.title for item in first] == [
        "bilibili:switch indie",
        "youtube:switch indie",
    ]
    assert [item.title for item in second] == [
        "reddit:anime workflow",
        "bilibili:anime workflow",
    ]
    assert bili.queries == ["switch indie", "anime workflow"]
    assert youtube.queries == ["switch indie"]
    assert reddit.queries == ["anime workflow"]


async def test_platform_source_provider_keeps_results_when_one_source_fails() -> None:
    class FailingBackend:
        platform = "bilibili"

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            raise RuntimeError("source failed")

    class WorkingBackend:
        platform = "youtube"

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            return [
                ExaPreviewItem(
                    title="YouTube teardown",
                    url="https://youtube.com/watch?v=abc",
                    highlights=("real platform evidence",),
                )
            ]

    provider = PlatformSourceInspirationProvider(
        [FailingBackend(), WorkingBackend()],
        platforms_per_query=2,
    )

    assert await provider.search("switch teardown", limit=3) == [
        ExaPreviewItem(
            title="YouTube teardown",
            url="https://youtube.com/watch?v=abc",
            highlights=("real platform evidence",),
        )
    ]


async def test_platform_source_provider_skips_cooldown_backend_and_fills_fanout() -> None:
    class Backend:
        def __init__(self, platform: str, cooldown: float = 0.0) -> None:
            self.platform = platform
            self.cooldown = cooldown
            self.queries: list[str] = []

        def cooldown_remaining(self) -> float:
            return self.cooldown

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.queries.append(query)
            return [
                ExaPreviewItem(
                    title=f"{self.platform}:{query}",
                    url=f"https://example.test/{self.platform}",
                    highlights=(),
                )
            ]

    bili = Backend("bilibili", cooldown=30.0)
    youtube = Backend("youtube")
    reddit = Backend("reddit")
    provider = PlatformSourceInspirationProvider(
        [bili, youtube, reddit],
        platforms_per_query=2,
    )

    result = await provider.search("switch repair", limit=4)

    assert [item.title for item in result] == [
        "youtube:switch repair",
        "reddit:switch repair",
    ]
    assert bili.queries == []
    assert provider.grounding_ledger()["skipped_cooldown"] == 1
    assert provider.bilibili_search_cooldown_remaining() == 30.0


async def test_platform_source_provider_enforces_riskcontrolled_budget_per_stage() -> None:
    class Backend:
        def __init__(self, platform: str, *, risk_controlled: bool = False) -> None:
            self.platform = platform
            self.risk_controlled = risk_controlled
            self.queries: list[str] = []

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            self.queries.append(query)
            return [
                ExaPreviewItem(
                    title=f"{self.platform}:{query}",
                    url=f"https://example.test/{self.platform}/{len(self.queries)}",
                    highlights=(),
                )
            ]

    bili = Backend("bilibili", risk_controlled=True)
    youtube = Backend("youtube")
    reddit = Backend("reddit")
    provider = PlatformSourceInspirationProvider(
        [bili, youtube, reddit],
        platforms_per_query=2,
        riskcontrolled_probe_budget=1,
    )
    provider.begin_stage()

    await provider.search("first", limit=4)
    await provider.search("second", limit=4)
    ledger = provider.grounding_ledger()

    assert bili.queries == ["first"]
    assert youtube.queries == ["first", "second"]
    assert reddit.queries == ["second"]
    assert ledger["platforms"]["bilibili"] == 1
    assert ledger["skipped_budget"] >= 1


async def test_platform_source_provider_backend_timeout_is_nonfatal() -> None:
    class SlowBackend:
        platform = "bilibili"

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            await asyncio.sleep(0.05)
            return [ExaPreviewItem(title="slow", url="https://example.test/slow")]

    class FastBackend:
        platform = "reddit"

        async def search(self, query: str, *, limit: int) -> list[ExaPreviewItem]:
            return [ExaPreviewItem(title="fast", url="https://example.test/fast")]

    provider = PlatformSourceInspirationProvider(
        [SlowBackend(), FastBackend()],
        platforms_per_query=2,
        backend_timeout_seconds=0.01,
    )

    result = await provider.search("timeout probe", limit=2)

    assert result == [ExaPreviewItem(title="fast", url="https://example.test/fast")]
    assert provider.grounding_ledger()["timeouts"] == 1


async def test_bilibili_platform_backend_maps_search_rows_to_previews() -> None:
    class BiliClient:
        async def search(self, query: str, *, page_size: int, **kwargs: object) -> list[dict]:
            assert query == "Switch 拆解"
            assert page_size == 2
            assert kwargs["page"] == 1
            return [
                {
                    "title": '<em class="keyword">Switch</em> OLED 拆解维修避坑',
                    "arcurl": "https://www.bilibili.com/video/BV123",
                    "description": "Joy-Con 漂移维修和屏幕排线注意点",
                    "author": "维修UP",
                }
            ]

    backend = BilibiliPlatformSearchBackend(BiliClient())

    assert await backend.search("Switch 拆解", limit=2) == [
        ExaPreviewItem(
            title="Switch OLED 拆解维修避坑",
            url="https://www.bilibili.com/video/BV123",
            highlights=("Joy-Con 漂移维修和屏幕排线注意点", "维修UP"),
        )
    ]


async def test_bilibili_platform_backend_fetches_multiple_pages() -> None:
    calls: list[tuple[int, int]] = []

    class BiliClient:
        async def search(self, query: str, *, page: int, page_size: int) -> list[dict]:
            assert query == "Switch 拆解"
            calls.append((page, page_size))
            return [
                {
                    "title": f"Switch 拆解 第{page}页",
                    "arcurl": f"https://www.bilibili.com/video/BV{page}",
                }
            ]

    backend = BilibiliPlatformSearchBackend(BiliClient())

    result = await backend.search("Switch 拆解", limit=4, pages=3)

    assert calls == [(1, 2), (2, 2), (3, 2)]
    assert [item.title for item in result] == [
        "Switch 拆解 第1页",
        "Switch 拆解 第2页",
        "Switch 拆解 第3页",
    ]


def test_bilibili_platform_backend_exposes_search_cooldown() -> None:
    class BiliClient:
        @classmethod
        def search_cooldown_remaining(cls) -> float:
            return 42.0

    backend = BilibiliPlatformSearchBackend(BiliClient())

    assert backend.risk_controlled is True
    assert backend.cooldown_remaining() == 42.0


async def test_youtube_platform_backend_maps_search_rows_to_previews() -> None:
    class YtClient:
        async def search_videos(self, query: str, *, limit: int) -> list[dict]:
            assert query == "anime production committee"
            assert limit == 1
            return [
                {
                    "videoId": "abc123",
                    "title": {"runs": [{"text": "Anime production committee explained"}]},
                    "ownerText": {"runs": [{"text": "Studio Notes"}]},
                    "descriptionSnippet": {
                        "runs": [{"text": "Funding, rights, and studio incentives."}]
                    },
                }
            ]

    backend = YoutubePlatformSearchBackend(YtClient())

    assert await backend.search("anime production committee", limit=1) == [
        ExaPreviewItem(
            title="Anime production committee explained",
            url="https://www.youtube.com/watch?v=abc123",
            highlights=("Funding, rights, and studio incentives.", "Studio Notes"),
        )
    ]


async def test_reddit_platform_backend_maps_command_rows_to_previews() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], *, timeout: float) -> object:
        import subprocess

        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "title": "Switch repair toolkit recommendations",
                            "url": "https://www.reddit.com/r/consolerepair/comments/abc/toolkit/",
                            "subreddit": "consolerepair",
                            "author": "repairer",
                            "selftext": "Tri-wing screwdriver and spudger notes.",
                        }
                    ]
                }
            ),
        )

    backend = RedditPlatformSearchBackend(backend="opencli", runner=runner)

    assert await backend.search("switch repair toolkit", limit=3) == [
        ExaPreviewItem(
            title="Switch repair toolkit recommendations",
            url="https://www.reddit.com/r/consolerepair/comments/abc/toolkit/",
            highlights=("Tri-wing screwdriver and spudger notes.", "r/consolerepair", "repairer"),
        )
    ]
    assert calls[0][:4] == ["opencli", "reddit", "search", "switch repair toolkit"]


async def test_platform_source_provider_passes_pages_and_returns_page_expanded_limit() -> None:
    class Backend:
        platform = "bilibili"
        risk_controlled = False

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def search(
            self,
            query: str,
            *,
            limit: int,
            pages: int = 1,
        ) -> list[ExaPreviewItem]:
            self.calls.append((limit, pages))
            return [
                ExaPreviewItem(title=f"{query} {idx}", url=f"https://example.test/{idx}")
                for idx in range(limit)
            ]

    backend = Backend()
    provider = FallbackInspirationSearchProvider(
        [
            PlatformSourceInspirationProvider(
                [backend],
                platforms_per_query=1,
                pages_per_probe=3,
            )
        ],
        pages_per_probe=3,
    )

    result = await provider.search("probe", limit=2)

    assert backend.calls == [(6, 3)]
    assert [item.title for item in result] == [
        "probe 0",
        "probe 1",
        "probe 2",
        "probe 3",
        "probe 4",
        "probe 5",
    ]


async def test_douyin_platform_backend_maps_aweme_rows_to_previews() -> None:
    class Client:
        async def search_aweme(self, query: str, *, limit: int) -> list[dict]:
            assert query == "情侣沟通"
            assert limit == 2
            return [
                {
                    "aweme_id": "7123",
                    "desc": "情侣吵架翻旧账怎么办",
                    "author": {"nickname": "关系教练"},
                    "statistics": {"digg_count": 42, "comment_count": 7},
                }
            ]

    backend = DouyinPlatformSearchBackend(Client())

    assert await backend.search("情侣沟通", limit=2) == [
        ExaPreviewItem(
            title="情侣吵架翻旧账怎么办",
            url="https://www.douyin.com/video/7123",
            highlights=("关系教练", "likes: 42, comments: 7"),
        )
    ]


async def test_xhs_platform_backend_maps_bridge_rows_to_previews() -> None:
    async def search(query: str, limit: int) -> list[dict[str, object]]:
        assert query == "寿喜烧"
        assert limit == 3
        return [
            {
                "note_id": "note1",
                "title": "寿喜烧家庭复刻不踩雷",
                "desc": "酱汁比例和肥牛选择",
                "author": "厨房实验室",
            }
        ]

    backend = XhsPlatformSearchBackend(search)

    assert await backend.search("寿喜烧", limit=3) == [
        ExaPreviewItem(
            title="寿喜烧家庭复刻不踩雷",
            url="https://www.xiaohongshu.com/explore/note1",
            highlights=("酱汁比例和肥牛选择", "厨房实验室"),
        )
    ]


async def test_x_platform_backend_maps_tweet_rows_to_previews() -> None:
    class Client:
        async def search(
            self,
            query: str,
            *,
            limit: int,
            product: str = "Top",
        ) -> list[dict[str, object]]:
            assert query == "AI agent"
            assert limit == 3
            assert product == "Top"
            return [
                {
                    "id": "1812345678901234567",
                    "text": (
                        "Agent search workflows are shifting from one-shot queries to "
                        "iterative probes."
                    ),
                    "articleText": (
                        "A longer note about probe queries, search grounding, and curator feedback."
                    ),
                    "author": {"screenName": "builder", "name": "Builder Notes"},
                    "metrics": {"likes": 42, "retweets": 5, "replies": 3, "views": 9000},
                }
            ]

    backend = XPlatformSearchBackend(Client())

    assert await backend.search("AI agent", limit=3) == [
        ExaPreviewItem(
            title="Agent search workflows are shifting from one-shot queries to iterative probes.",
            url="https://x.com/builder/status/1812345678901234567",
            highlights=(
                "@builder / Builder Notes",
                "A longer note about probe queries, search grounding, and curator feedback.",
                "likes: 42, retweets: 5, replies: 3, views: 9000",
            ),
        )
    ]


async def test_zhihu_platform_backend_maps_bridge_rows_to_previews() -> None:
    async def search(query: str, limit: int) -> list[dict[str, object]]:
        assert query == "ETF 定投"
        assert limit == 3
        return [
            {
                "content_id": "123",
                "title": "普通人如何理解 ETF 定投回撤",
                "url": "https://www.zhihu.com/question/123/answer/456",
                "summary": "从现金流和风险承受能力解释。",
                "author": "基金研究员",
            }
        ]

    backend = ZhihuPlatformSearchBackend(search)

    assert await backend.search("ETF 定投", limit=3) == [
        ExaPreviewItem(
            title="普通人如何理解 ETF 定投回撤",
            url="https://www.zhihu.com/question/123/answer/456",
            highlights=("从现金流和风险承受能力解释。", "基金研究员"),
        )
    ]


async def test_bangumi_platform_backend_maps_official_rows_to_previews() -> None:
    class Client:
        async def search_subjects(self, query: str, **kwargs: object) -> SimpleNamespace:
            assert query == "时间循环"
            assert kwargs["subject_types"] == ("anime", "game")
            return SimpleNamespace(
                data=[
                    {
                        "id": 326,
                        "name_cn": "攻壳机动队",
                        "summary": "未来社会的故事",
                        "tags": [{"name": "科幻"}],
                    }
                ]
            )

    backend = BangumiPlatformSearchBackend(Client(), subject_types=("anime", "game"))

    assert await backend.search("时间循环", limit=3) == [
        ExaPreviewItem(
            title="攻壳机动队",
            url="https://bgm.tv/subject/326",
            highlights=("未来社会的故事", "科幻"),
        )
    ]


async def test_v2ex_platform_backend_maps_public_topics_to_previews() -> None:
    class Client:
        async def search_topics(self, query: str, *, limit: int) -> SimpleNamespace:
            assert query == "Agent"
            assert limit == 3
            return SimpleNamespace(
                data=[
                    {
                        "id": 123,
                        "title": "Agent 上下文管理",
                        "content": "本地运行与隐私",
                        "node": {"name": "programmer", "title": "程序员"},
                        "member": {"username": "alice"},
                    }
                ]
            )

    backend = V2EXPlatformSearchBackend(Client())

    assert await backend.search("Agent", limit=3) == [
        ExaPreviewItem(
            title="Agent 上下文管理",
            url="https://www.v2ex.com/t/123",
            highlights=("本地运行与隐私", "程序员", "alice"),
        )
    ]


async def test_weibo_platform_backend_maps_anonymous_posts_to_previews() -> None:
    class Client:
        async def search_posts(self, query: str, **kwargs: object) -> SimpleNamespace:
            assert query == "AI Agent"
            assert kwargs == {"page": 1, "limit": 3}
            return SimpleNamespace(
                data=[
                    {
                        "id": "5190000000000001",
                        "bid": "P9Example",
                        "text": "<p>Agent 搜索工作流的新进展</p>",
                        "user": {"id": 123, "screen_name": "研究员"},
                    }
                ]
            )

    backend = WeiboPlatformSearchBackend(Client())

    assert await backend.search("AI Agent", limit=3) == [
        ExaPreviewItem(
            title="Agent 搜索工作流的新进展",
            url="https://weibo.com/123/P9Example",
            highlights=("Agent 搜索工作流的新进展", "研究员"),
        )
    ]


def test_build_platform_source_backends_uses_only_enabled_sources() -> None:
    config = SimpleNamespace(
        sources=SimpleNamespace(
            bilibili=SimpleNamespace(enabled=True),
            xiaohongshu=SimpleNamespace(enabled=True),
            douyin=SimpleNamespace(enabled=True),
            youtube=SimpleNamespace(enabled=True),
            twitter=SimpleNamespace(enabled=True),
            zhihu=SimpleNamespace(enabled=True),
            reddit=SimpleNamespace(enabled=False, backend="rdt"),
            bangumi=SimpleNamespace(enabled=True, subject_types=("anime", "book")),
            v2ex=SimpleNamespace(enabled=True),
        )
    )
    youtube_client = object()

    async def xhs_search(query: str, limit: int) -> list[dict[str, object]]:
        return []

    async def zhihu_search(query: str, limit: int) -> list[dict[str, object]]:
        return []

    backends = build_platform_source_backends(
        config,
        bilibili_client=object(),
        xhs_search=xhs_search,
        douyin_client=object(),
        youtube_client=youtube_client,
        x_client=object(),
        zhihu_search=zhihu_search,
        bangumi_client=object(),
        v2ex_client=object(),
    )

    assert [backend.platform for backend in backends] == [
        "bilibili",
        "xiaohongshu",
        "douyin",
        "youtube",
        "twitter",
        "zhihu",
        "bangumi",
        "v2ex",
    ]
