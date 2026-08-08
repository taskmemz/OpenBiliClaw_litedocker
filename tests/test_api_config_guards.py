from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from openbiliclaw.api.app import create_app
from openbiliclaw.config import (
    Config,
    EmbeddingConfig,
    LLMConfig,
    LLMProviderConfig,
    save_config,
)
from openbiliclaw.config import (
    load_config as load_config_from_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_client(
    monkeypatch,
    tmp_path: Path,
    initial_cfg: Config,
) -> tuple[TestClient, Config, Path]:
    config_path = tmp_path / "config.toml"
    save_config(initial_cfg, config_path)

    monkeypatch.setattr("openbiliclaw.config.load_config", lambda *_a, **_kw: initial_cfg)
    monkeypatch.setattr(
        "openbiliclaw.config.save_config",
        lambda cfg, path=None: save_config(cfg, config_path),
    )

    app = create_app(memory_manager=object(), database=object(), soul_engine=object())
    return TestClient(app), initial_cfg, config_path


def _base_config() -> Config:
    return Config(
        llm=LLMConfig(
            default_provider="openai",
            openai=LLMProviderConfig(
                api_key="sk-real-key-1234567890abcdef",
                model="gpt-4o-mini",
            ),
            claude=LLMProviderConfig(api_key="claude-real-key", model="claude-3-5-haiku"),
            deepseek=LLMProviderConfig(api_key="deepseek-real-key", model="deepseek-chat"),
            openrouter=LLMProviderConfig(api_key="openrouter-real-key", model="openrouter/auto"),
            openai_compatible=LLMProviderConfig(
                api_key="compat-real-key",
                model="mimo-v2.5-pro",
                base_url="https://token-plan-sgp.xiaomimimo.com/v1",
            ),
            embedding=EmbeddingConfig(
                provider="openai",
                model="text-embedding-3-small",
                api_key="sk-embedding-real-key",
                base_url="https://embed.example.com/v1",
            ),
        )
    )


def test_put_config_ignores_masked_chat_provider_api_key(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"llm": {"openai": {"api_key": "sk-d****cdef"}}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.api_key == "sk-real-key-1234567890abcdef"


def test_put_config_ignores_empty_chat_provider_api_key(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"llm": {"openai": {"api_key": ""}}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.api_key == "sk-real-key-1234567890abcdef"


def test_put_config_writes_real_new_chat_provider_api_key(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put(
        "/api/config",
        json={"llm": {"openai": {"api_key": "sk-new-real-key-fedcba0987654321"}}},
    )

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.api_key == (
        "sk-new-real-key-fedcba0987654321"
    )


def test_put_config_ignores_empty_chat_provider_model(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"llm": {"openai": {"model": ""}}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.model == "gpt-4o-mini"


def test_put_config_writes_real_new_chat_provider_model(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"llm": {"openai": {"model": "gpt-4.1-mini"}}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.model == "gpt-4.1-mini"


def test_put_config_round_trips_openai_auth_mode(monkeypatch, tmp_path) -> None:
    from openbiliclaw.llm.codex_auth import CodexCredentials

    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())
    monkeypatch.setattr(
        "openbiliclaw.llm.codex_auth.load_codex_credentials",
        lambda: CodexCredentials("access-token", "refresh-token", 9999999999),
    )

    response = client.put(
        "/api/config",
        json={"llm": {"openai": {"auth_mode": "codex_oauth"}}},
    )

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.auth_mode == "codex_oauth"
    get_response = client.get("/api/config")
    assert get_response.status_code == 200
    assert get_response.json()["llm"]["openai"]["auth_mode"] == "codex_oauth"


def test_put_config_round_trips_explicit_fallback_providers(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    # Chat fallback must be a provider that would actually register — v0.3.156+
    # rejects a dead fallback (claude has a real key in _base_config; a keyless
    # gemini fallback is now a blocking 400 by design). Embedding fallback keeps
    # ollama (embedding side has no api_key requirement).
    response = client.put(
        "/api/config",
        json={
            "llm": {
                "fallback_provider": "claude",
                "embedding": {"fallback_provider": "ollama"},
            }
        },
    )

    assert response.status_code == 202
    loaded = load_config_from_path(config_path)
    assert loaded.llm.fallback_provider == "claude"
    assert loaded.llm.embedding.fallback_provider == "ollama"

    get_response = client.get("/api/config")
    assert get_response.status_code == 200
    body = get_response.json()
    assert body["llm"]["fallback_provider"] == "claude"
    assert body["llm"]["embedding"]["fallback_provider"] == "ollama"


def test_put_config_round_trips_embedding_multimodal_enabled(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    # Default off, and GET echoes it so the settings page can render the toggle.
    assert client.get("/api/config").json()["llm"]["embedding"]["multimodal_enabled"] is False

    response = client.put(
        "/api/config",
        json={"llm": {"embedding": {"multimodal_enabled": True}}},
    )
    assert response.status_code == 202

    loaded = load_config_from_path(config_path)
    assert loaded.llm.embedding.multimodal_enabled is True
    assert client.get("/api/config").json()["llm"]["embedding"]["multimodal_enabled"] is True


def test_put_config_accepts_dashscope_embedding_provider(monkeypatch, tmp_path) -> None:
    """Regression (found by the multimodal E2E, 2026-07-14): `dashscope` must be
    an accepted embedding provider at config-save validation, not just in the
    registry. Otherwise the settings-page dropdown option 400s on save.
    """
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put(
        "/api/config",
        json={
            "llm": {
                "embedding": {
                    "provider": "dashscope",
                    "model": "qwen3-vl-embedding",
                    "api_key": "sk-dashscope-e2e",
                    "multimodal_enabled": True,
                }
            }
        },
    )
    assert response.status_code == 202, response.json()

    loaded = load_config_from_path(config_path)
    assert loaded.llm.embedding.provider == "dashscope"
    assert loaded.llm.embedding.model == "qwen3-vl-embedding"
    assert loaded.llm.embedding.multimodal_enabled is True

    body = client.get("/api/config").json()["llm"]["embedding"]
    assert body["provider"] == "dashscope"
    assert body["multimodal_enabled"] is True


def test_put_config_round_trips_embedding_output_dimensionality(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put(
        "/api/config",
        json={"llm": {"embedding": {"output_dimensionality": 768}}},
    )

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.embedding.output_dimensionality == 768

    get_response = client.get("/api/config")
    assert get_response.status_code == 200
    body = get_response.json()
    assert body["llm"]["embedding"]["output_dimensionality"] == 768


def test_put_config_rejects_invalid_embedding_output_dimensionality(
    monkeypatch,
    tmp_path,
) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put(
        "/api/config",
        json={"llm": {"embedding": {"output_dimensionality": "wide"}}},
    )

    assert response.status_code == 400
    assert load_config_from_path(config_path).llm.embedding.output_dimensionality == 1024


def test_put_config_ignores_whitespace_only_chat_provider_api_key(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"llm": {"openai": {"api_key": "   "}}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).llm.openai.api_key == "sk-real-key-1234567890abcdef"


def test_put_config_uses_same_guard_for_other_chat_providers(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    for provider_name in ("claude", "deepseek", "openrouter", "openai_compatible"):
        before = getattr(load_config_from_path(config_path).llm, provider_name).api_key
        masked = before[:2] + "****" + before[-2:]
        response = client.put(
            "/api/config",
            json={"llm": {provider_name: {"api_key": masked}}},
        )
        assert response.status_code == 202
        assert getattr(load_config_from_path(config_path).llm, provider_name).api_key == before

        response = client.put(
            "/api/config",
            json={"llm": {provider_name: {"api_key": ""}}},
        )
        assert response.status_code == 202
        assert getattr(load_config_from_path(config_path).llm, provider_name).api_key == before


def test_put_config_explicit_reset_clears_allowlisted_secret(monkeypatch, tmp_path) -> None:
    client, cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"reset_fields": ["llm.openai.api_key"]})

    assert response.status_code == 202
    assert cfg.llm.openai.api_key == ""
    assert load_config_from_path(config_path).llm.openai.api_key == ""


def test_put_config_unknown_reset_is_rejected_without_mutation(monkeypatch, tmp_path) -> None:
    client, cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())
    before = config_path.read_text(encoding="utf-8")

    response = client.put(
        "/api/config",
        json={
            "reset_fields": ["storage.db_path"],
            "llm": {"openai": {"model": "gpt-4.1-mini"}},
        },
    )

    assert response.status_code == 400
    assert config_path.read_text(encoding="utf-8") == before
    assert cfg.llm.openai.model == "gpt-4o-mini"


def test_put_config_ignores_empty_embedding_model_and_base_url(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put(
        "/api/config",
        json={"llm": {"embedding": {"model": "", "base_url": ""}}},
    )

    assert response.status_code == 202
    embedding = load_config_from_path(config_path).llm.embedding
    assert embedding.model == "text-embedding-3-small"
    assert embedding.base_url == "https://embed.example.com/v1"


# ── Source cookie guards (bilibili masked/empty echo; dy/x file routing) ──


def _stub_live_probes(monkeypatch, *, authenticated: bool = True) -> list[tuple[str, str]]:
    """Answer the write-time live gate locally, and record what it was asked.

    ``PUT /api/config`` now runs the same probe ``POST /api/bilibili/cookie``
    has always run (spec D4 — the two paths disagreeing about the same cookie
    was the bug), so without this these tests would reach out to bilibili.com
    and douyin.com. Patching the single shared ``run_live_probe`` keeps them
    offline; returning the call list lets them assert the gate actually fired
    rather than merely that nothing blew up.
    """
    from openbiliclaw.api.source_auth import verify

    calls: list[tuple[str, str]] = []

    async def _probe(slug, *, cfg, cookie=None, probes=None, record=True):
        calls.append((slug, str(cookie or "")))
        return verify.LiveProbeOutcome(
            slug=slug,
            has_credential=True,
            authenticated=authenticated,
            network_error=False,
            message="stubbed probe",
            username="tester",
        )

    monkeypatch.setattr(verify, "run_live_probe", _probe)
    return calls


def _cookie_config(tmp_path: Path) -> Config:
    from openbiliclaw.config import BilibiliConfig

    cfg = _base_config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.bilibili = BilibiliConfig(
        auth_method="cookie",
        cookie="SESSDATA=real-sess; bili_jct=real-csrf; DedeUserID=42",
    )
    return cfg


def test_put_config_ignores_masked_bilibili_cookie_echo(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _cookie_config(tmp_path))

    response = client.put(
        "/api/config",
        json={"bilibili": {"cookie": "SESS************ID=42"}},
    )

    assert response.status_code == 202
    assert load_config_from_path(config_path).bilibili.cookie == (
        "SESSDATA=real-sess; bili_jct=real-csrf; DedeUserID=42"
    )


def test_put_config_ignores_empty_bilibili_cookie(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _cookie_config(tmp_path))

    response = client.put("/api/config", json={"bilibili": {"cookie": ""}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).bilibili.cookie == (
        "SESSDATA=real-sess; bili_jct=real-csrf; DedeUserID=42"
    )


def test_put_config_writes_real_new_bilibili_cookie(monkeypatch, tmp_path) -> None:
    probes = _stub_live_probes(monkeypatch)
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _cookie_config(tmp_path))

    response = client.put(
        "/api/config",
        json={"bilibili": {"cookie": "SESSDATA=new-sess; bili_jct=new-csrf; DedeUserID=43"}},
    )

    assert response.status_code == 202
    assert load_config_from_path(config_path).bilibili.cookie == (
        "SESSDATA=new-sess; bili_jct=new-csrf; DedeUserID=43"
    )
    # The paste was live-checked before it landed, exactly as the extension's
    # cookie-sync endpoint checks it (spec D4).
    assert probes == [("bilibili", "SESSDATA=new-sess; bili_jct=new-csrf; DedeUserID=43")]


def test_put_config_routes_douyin_cookie_to_data_file(monkeypatch, tmp_path) -> None:
    from openbiliclaw.sources.douyin_auth import DouyinCookieManager

    monkeypatch.delenv("OPENBILICLAW_DOUYIN_COOKIE", raising=False)
    probes = _stub_live_probes(monkeypatch)
    cfg = _cookie_config(tmp_path)
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, cfg)

    response = client.put(
        "/api/config",
        json={"sources": {"douyin": {"cookie": "sessionid=dy-sess; ttwid=dy-tw"}}},
    )

    assert response.status_code == 202
    # Secret lands in data/douyin_cookie.json, never in config.toml.
    assert DouyinCookieManager(cfg.data_path).load_cookie() == "sessionid=dy-sess; ttwid=dy-tw"
    assert "dy-sess" not in config_path.read_text(encoding="utf-8")
    assert probes == [("douyin", "sessionid=dy-sess; ttwid=dy-tw")]


def test_put_config_routes_x_cookie_to_data_file(monkeypatch, tmp_path) -> None:
    from openbiliclaw.sources.x_auth import XCookieManager

    monkeypatch.delenv("OPENBILICLAW_X_COOKIE", raising=False)
    cfg = _cookie_config(tmp_path)
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, cfg)

    response = client.put(
        "/api/config",
        json={"sources": {"twitter": {"cookie": "auth_token=x-at; ct0=x-csrf"}}},
    )

    assert response.status_code == 202
    assert XCookieManager(cfg.data_path).load_cookie() == "auth_token=x-at; ct0=x-csrf"
    assert "x-at" not in config_path.read_text(encoding="utf-8")


def test_put_config_ignores_masked_douyin_cookie_echo(monkeypatch, tmp_path) -> None:
    from openbiliclaw.sources.douyin_auth import DouyinCookieManager

    monkeypatch.delenv("OPENBILICLAW_DOUYIN_COOKIE", raising=False)
    cfg = _cookie_config(tmp_path)
    manager = DouyinCookieManager(cfg.data_path)
    manager.set_cookie("sessionid=dy-real", source="test")
    client, _cfg, _config_path = _make_client(monkeypatch, tmp_path, cfg)

    response = client.put(
        "/api/config",
        json={"sources": {"douyin": {"cookie": "sess************real"}}},
    )

    assert response.status_code == 202
    assert manager.load_cookie() == "sessionid=dy-real"


def test_put_config_empty_cookie_env_keeps_existing_name(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _cookie_config(tmp_path))

    response = client.put(
        "/api/config",
        json={
            "sources": {
                "douyin": {"cookie_env": ""},
                "twitter": {"cookie_env": ""},
            }
        },
    )

    assert response.status_code == 202
    saved = load_config_from_path(config_path)
    assert saved.sources.douyin.cookie_env == "OPENBILICLAW_DOUYIN_COOKIE"
    assert saved.sources.twitter.cookie_env == "OPENBILICLAW_X_COOKIE"


def test_get_config_exposes_douyin_and_x_cookies_like_bilibili(monkeypatch, tmp_path) -> None:
    from openbiliclaw.sources.douyin_auth import DouyinCookieManager
    from openbiliclaw.sources.x_auth import XCookieManager

    monkeypatch.delenv("OPENBILICLAW_DOUYIN_COOKIE", raising=False)
    monkeypatch.delenv("OPENBILICLAW_X_COOKIE", raising=False)
    cfg = _cookie_config(tmp_path)
    DouyinCookieManager(cfg.data_path).set_cookie(
        "sessionid=dy-sess-1234567890; ttwid=dy-tw", source="test"
    )
    XCookieManager(cfg.data_path).set_cookie(
        "auth_token=x-at-1234567890; ct0=x-csrf", source="test"
    )
    client, _cfg, _config_path = _make_client(monkeypatch, tmp_path, cfg)

    masked = client.get("/api/config").json()
    assert "****" in masked["sources"]["douyin"]["cookie"]
    assert "dy-sess-1234567890" not in masked["sources"]["douyin"]["cookie"]
    assert "****" in masked["sources"]["twitter"]["cookie"]
    assert "****" in masked["bilibili"]["cookie"]

    revealed = client.get("/api/config?reveal_keys=true").json()
    assert revealed["sources"]["douyin"]["cookie"] == masked["sources"]["douyin"]["cookie"]
    assert revealed["sources"]["twitter"]["cookie"] == masked["sources"]["twitter"]["cookie"]
    assert revealed["bilibili"]["cookie"] == masked["bilibili"]["cookie"]


# ── [network].proxy API exposure ────────────────────────────────────────────


def _proxy_config(proxy: str) -> Config:
    cfg = _base_config()
    cfg.network.mode = "custom" if proxy else "direct"
    cfg.network.proxy = proxy
    return cfg


def test_get_config_exposes_network_proxy(monkeypatch, tmp_path) -> None:
    client, _cfg, _path = _make_client(
        monkeypatch, tmp_path, _proxy_config("socks5://127.0.0.1:1080")
    )
    body = client.get("/api/config").json()
    assert body["network"]["mode"] == "custom"
    assert body["network"]["proxy"] == "socks5://127.0.0.1:1080"


def test_get_config_masks_proxy_userinfo(monkeypatch, tmp_path) -> None:
    client, _cfg, _path = _make_client(
        monkeypatch, tmp_path, _proxy_config("socks5://user:secret@127.0.0.1:1080")
    )
    body = client.get("/api/config").json()
    assert "secret" not in body["network"]["proxy"]
    assert body["network"]["proxy"] == "socks5://***@127.0.0.1:1080"


def test_put_config_writes_valid_network_proxy(monkeypatch, tmp_path) -> None:
    from openbiliclaw import network

    network.reset_outbound_proxy_for_tests()
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())

    response = client.put("/api/config", json={"network": {"proxy": "socks5://127.0.0.1:1080"}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).network.proxy == "socks5://127.0.0.1:1080"
    # Hot path updated the process-level source of truth.
    assert network.outbound_proxy_mode() == "custom"
    assert network.outbound_proxy_url() == "socks5://127.0.0.1:1080"
    network.reset_outbound_proxy_for_tests()


def test_put_config_rejects_invalid_network_proxy(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())
    before = config_path.read_text(encoding="utf-8")

    response = client.put("/api/config", json={"network": {"proxy": "ftp://127.0.0.1:1"}})

    assert response.status_code == 400
    # config.toml is untouched on rejection.
    assert config_path.read_text(encoding="utf-8") == before


def test_put_config_switches_to_direct_and_ignores_environment_proxy(monkeypatch, tmp_path) -> None:
    from openbiliclaw import network

    client, _cfg, config_path = _make_client(
        monkeypatch, tmp_path, _proxy_config("http://127.0.0.1:7897")
    )

    response = client.put("/api/config", json={"network": {"mode": "direct"}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).network.mode == "direct"
    assert network.outbound_httpx_kwargs() == {"trust_env": False}


def test_put_config_proxy_only_payload_clears_to_system_not_direct(monkeypatch, tmp_path) -> None:
    """Clearing the proxy without sending ``mode`` lands on the ``system`` default.

    A proxy-only payload (older UI build, third-party API client) carries no
    opinion about ``mode``, so it must resolve the way an absent
    ``[network].mode`` key resolves in ``_build_network_config`` rather than
    silently pinning the user to ``direct``.
    """
    from openbiliclaw import network

    network.reset_outbound_proxy_for_tests()
    client, _cfg, config_path = _make_client(
        monkeypatch, tmp_path, _proxy_config("socks5://127.0.0.1:1080")
    )

    response = client.put("/api/config", json={"network": {"proxy": ""}})

    assert response.status_code == 202
    saved = load_config_from_path(config_path)
    assert saved.network.mode == "system"
    assert saved.network.proxy == ""
    # Hot path mirrored the same policy into the process-level source of truth.
    assert network.outbound_proxy_mode() == "system"
    network.reset_outbound_proxy_for_tests()


def test_put_config_rejects_custom_mode_without_proxy(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(monkeypatch, tmp_path, _base_config())
    before = config_path.read_text(encoding="utf-8")

    response = client.put("/api/config", json={"network": {"mode": "custom", "proxy": ""}})

    assert response.status_code == 400
    assert config_path.read_text(encoding="utf-8") == before


def test_put_config_ignores_masked_proxy_echo(monkeypatch, tmp_path) -> None:
    client, _cfg, config_path = _make_client(
        monkeypatch, tmp_path, _proxy_config("socks5://user:secret@127.0.0.1:1080")
    )

    response = client.put("/api/config", json={"network": {"proxy": "socks5://***@127.0.0.1:1080"}})

    assert response.status_code == 202
    assert load_config_from_path(config_path).network.proxy == "socks5://user:secret@127.0.0.1:1080"
