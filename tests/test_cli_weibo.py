"""CLI-boundary coverage for Weibo formal discovery and read-only smoke commands."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from click import unstyle
from typer.testing import CliRunner

from openbiliclaw import cli as cli_module
from openbiliclaw.cli import app
from openbiliclaw.config import Config


def _fail_if_called(name: str):
    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(f"read-only Weibo smoke called {name}")

    return _fail


def _guard_smoke_boundaries(monkeypatch: pytest.MonkeyPatch, cfg: Config) -> None:
    monkeypatch.setattr("openbiliclaw.config.load_config", lambda *_a, **_kw: cfg)
    monkeypatch.setattr(
        "openbiliclaw.config.save_config",
        _fail_if_called("save_config"),
    )
    for name in (
        "_require_runtime_config",
        "_get_runtime_database",
        "_build_soul_engine",
        "_build_discovery_engine",
        "_build_discovery_candidate_pipeline",
    ):
        monkeypatch.setattr(cli_module, name, _fail_if_called(name))


def _post(
    content_id: str,
    *,
    text: str = "公开微博正文",
    reposts: int = 0,
) -> dict[str, object]:
    return {
        "id": content_id,
        "bid": f"bid-{content_id}",
        "text": text,
        "user": {"id": 12345, "screen_name": "公开作者"},
        "attitudes_count": 12,
        "comments_count": 7,
        "reposts_count": reposts,
    }


@pytest.mark.parametrize(
    "command",
    ["discover-weibo", "discover-weibo-hot", "discover-weibo-creator"],
)
def test_weibo_smoke_commands_are_registered_and_have_help(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])
    help_text = unstyle(result.output)

    assert result.exit_code == 0, result.output
    assert "只读" in help_text
    assert "--limit" in help_text


@pytest.mark.parametrize("alias", ["weibo", "wb"])
def test_generic_discover_dispatches_weibo_to_formal_producer(
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
) -> None:
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        cli_module,
        "_run_weibo_discovery",
        lambda *, limit, force=False: calls.append((limit, force)),
    )
    monkeypatch.setattr(
        cli_module,
        "_require_runtime_config",
        _fail_if_called("bilibili runtime"),
    )

    result = CliRunner().invoke(
        app,
        ["discover", "--source", alias, "--limit", "7", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(7, True)]


def test_weibo_search_smoke_reads_posts_without_database_or_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbiliclaw.sources.weibo_client as client_module

    cfg = Config()
    cfg.sources.weibo.request_interval_seconds = 0
    _guard_smoke_boundaries(monkeypatch, cfg)
    calls: list[tuple[str, object]] = []
    previews: list[Any] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("init", kwargs))

        async def __aenter__(self) -> FakeClient:
            calls.append(("enter", ""))
            return self

        async def __aexit__(self, *_args: object) -> None:
            calls.append(("exit", ""))

        async def search_posts(self, keyword: str, *, page: int, limit: int) -> object:
            calls.append(("search", (keyword, page, limit)))
            return SimpleNamespace(rows=[_post("50230001", reposts=88)])

    monkeypatch.setattr(client_module, "WeiboClient", FakeClient)
    monkeypatch.setattr(
        cli_module,
        "_print_discovered_content_preview",
        lambda item, _index: previews.append(item),
    )

    result = CliRunner().invoke(
        app,
        ["discover-weibo", "OpenBiliClaw", "--limit", "3"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("init", {"request_interval_seconds": 0.0}),
        ("enter", ""),
        ("search", ("OpenBiliClaw", 1, 3)),
        ("exit", ""),
    ]
    assert len(previews) == 1
    assert previews[0].source_platform == "weibo"
    assert previews[0].source_strategy == "weibo-search"
    assert previews[0].share_count == 88
    assert "用户 Cookie" in result.output
    assert "未读取" in result.output
    assert "本地写入" in result.output


def test_weibo_hot_smoke_uses_topics_only_as_seeds_for_real_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbiliclaw.sources.weibo_client as client_module

    cfg = Config()
    cfg.sources.weibo.request_interval_seconds = 0
    _guard_smoke_boundaries(monkeypatch, cfg)
    calls: list[tuple[str, object]] = []
    previews: list[Any] = []

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def hot_topics(self, *, limit: int) -> object:
            calls.append(("hot", limit))
            return SimpleNamespace(
                rows=[
                    {"word": {"nested": "不能作为查询"}},
                    {"note": ["也不能作为查询"]},
                    {"word": "开源智能体", "realpos": 4},
                ]
            )

        async def search_posts(self, keyword: str, *, page: int, limit: int) -> object:
            calls.append(("search", (keyword, page, limit)))
            return SimpleNamespace(rows=[_post("50230002", text="热搜下的真实微博")])

    monkeypatch.setattr(client_module, "WeiboClient", FakeClient)
    monkeypatch.setattr(
        cli_module,
        "_print_discovered_content_preview",
        lambda item, _index: previews.append(item),
    )

    result = CliRunner().invoke(app, ["discover-weibo-hot", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert calls == [("hot", 2), ("search", ("开源智能体", 1, 1))]
    assert len(previews) == 1
    assert previews[0].content_id == "50230002"
    assert previews[0].source_strategy == "weibo-hot"
    assert previews[0].source_rank == 4


def test_weibo_creator_smoke_validates_uid_before_constructing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openbiliclaw.sources.weibo_client as client_module

    monkeypatch.setattr(
        client_module,
        "WeiboClient",
        _fail_if_called("WeiboClient"),
    )

    result = CliRunner().invoke(app, ["discover-weibo-creator", "screen-name"])

    assert result.exit_code != 0
    assert "UID" in result.output
    assert "数字" in result.output
