"""API security and staging contract for whole-user-data migration."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import zipfile
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from openbiliclaw.api.app import _MigrationArchiveStreamingResponse, create_app
from openbiliclaw.config import Config, LLMConfig, LLMProviderConfig, load_config, save_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


_LOOPBACK_ORIGIN = "http://127.0.0.1:8420"
_LOCAL_HEADERS = {"Origin": _LOOPBACK_ORIGIN, "X-OBC-Auth": "1"}


def _save_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    degraded: bool = False,
) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("OPENBILICLAW_PROJECT_ROOT", str(root))
    if degraded:
        llm = LLMConfig(
            default_provider="openai",
            openai=LLMProviderConfig(api_key="", model="gpt-4o-mini"),
            ollama=LLMProviderConfig(model="", base_url=""),
        )
    else:
        llm = LLMConfig(
            default_provider="ollama",
            ollama=LLMProviderConfig(model="qwen3:8b", base_url="http://127.0.0.1:11434"),
        )
    config = Config(llm=llm, data_dir=str(root / "data"))
    config.scheduler.enabled = False
    config.bilibili.cookie = "SESSDATA=portable-secret"
    config.api.auth.password_hash = "api-auth-password-hash-must-not-export"
    config.api.auth.session_secret = "api-auth-session-secret-must-not-export"
    config.api.auth.session_ttl_hours = 36
    config.api.auth.trust_loopback = False
    config.api.auth.trusted_proxies = ["127.0.0.1"]
    config.api.auth.allowed_bearer_origins = ["https://target.example"]
    config.api.auth.extension_access_enabled = True
    config.api.auth.extension_access_keys = ["api-auth-extension-key-must-not-export"]
    config.api.auth.extension_token_ttl_hours = 48
    save_config(config, root / "config.toml", autostart_authoritative=True)
    memory_dir = root / "data" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "profile.json").write_text('{"topic":"迁移测试"}\n', encoding="utf-8")
    return root


def _loopback(app: object) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 5010), base_url=_LOOPBACK_ORIGIN)


def test_migration_api_is_local_only_and_stages_without_live_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENBILICLAW_TEST_MIGRATION_ENV", "environment-secret")
    root = _save_runtime_config(tmp_path, monkeypatch)
    app = create_app()
    config_path = root / "config.toml"

    with _loopback(app) as local:
        assert local.get("/api/migration/status").status_code == 403
        assert (
            local.get(
                "/api/migration/status",
                headers={"Origin": "chrome-extension://abcdefghijklmnop", "X-OBC-Auth": "1"},
            ).status_code
            == 403
        )
        assert (
            local.get(
                "/api/migration/status",
                headers={"Origin": "https://attacker.example", "X-OBC-Auth": "1"},
            ).status_code
            == 403
        )
        assert local.get("/api/migration/status", headers=_LOCAL_HEADERS).json()["state"] == "idle"

        remote = TestClient(
            app,
            client=("192.168.1.50", 5011),
            base_url="http://192.168.1.10:8420",
        )
        assert (
            remote.get(
                "/api/migration/status",
                headers={"Origin": "http://192.168.1.10:8420", "X-OBC-Auth": "1"},
            ).status_code
            == 403
        )

        exported = local.post(
            "/api/migration/export",
            headers=_LOCAL_HEADERS,
            json={
                "frontend": {
                    "theme_mode": "dark",
                    "theme_hue": 210,
                    "accent_style": "modern",
                    "auto_load_on_scroll": False,
                    "side_drawer_open": True,
                    "session_token": "must-not-export",
                }
            },
        )
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith(
            "application/vnd.openbiliclaw.backup+zip"
        )
        assert exported.headers["cache-control"] == "no-store, private"
        assert ".obcbackup" in exported.headers["content-disposition"]

        archive_bytes = exported.content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            frontend = json.loads(archive.read("frontend/settings.json"))
            names = set(archive.namelist())
            portable_config_toml = archive.read("config/config.toml").decode("utf-8")
            unpacked_archive = b"\n".join(
                archive.read(name) for name in archive.namelist() if not name.endswith("/")
            )
        assert manifest["format"] == "openbiliclaw-user-data"
        assert manifest["contains_secrets"] is True
        assert "active_environment_variables" not in manifest
        assert "OPENBILICLAW_TEST_MIGRATION_ENV" in manifest["source_omitted_environment_variables"]
        assert "config/config.toml" in names
        assert "data/memory/profile.json" in names
        assert b"api-auth-password-hash-must-not-export" not in unpacked_archive
        assert b"api-auth-session-secret-must-not-export" not in unpacked_archive
        assert b"api-auth-extension-key-must-not-export" not in unpacked_archive
        assert "[api.auth]" not in portable_config_toml
        assert "password_hash" not in portable_config_toml
        assert "session_secret" not in portable_config_toml
        assert "extension_access_keys" not in portable_config_toml
        assert frontend == {
            "accent_style": "modern",
            "auto_load_on_scroll": False,
            "side_drawer_open": True,
            "theme_hue": 210,
            "theme_mode": "dark",
        }

        before_config = config_path.read_bytes()
        assert (
            local.post(
                "/api/migration/import",
                headers={**_LOCAL_HEADERS, "Content-Type": "application/octet-stream"},
                content=archive_bytes,
            ).status_code
            == 400
        )
        staged = local.post(
            "/api/migration/import",
            headers={
                **_LOCAL_HEADERS,
                "Content-Type": "application/octet-stream",
                "X-OBC-Migration-Confirm": "replace-all",
                "X-OBC-Migration-Request-ID": "123e4567-e89b-42d3-a456-426614174000",
            },
            content=archive_bytes,
        )
        assert staged.status_code == 202
        body = staged.json()
        assert body["state"] == "staged"
        assert body["request_id"] == "123e4567e89b42d3a456426614174000"
        assert body["restart_required"] is True
        assert body["frontend"]["theme_mode"] == "dark"
        assert "OPENBILICLAW_TEST_MIGRATION_ENV" in body["source_omitted_environment_variables"]
        assert "OPENBILICLAW_TEST_MIGRATION_ENV" in body["target_active_environment_variables"]
        assert config_path.read_bytes() == before_config
        migration_root = root / ".openbiliclaw-migration"
        marker = json.loads((migration_root / "pending.json").read_text(encoding="utf-8"))
        normalized = load_config(migration_root / marker["stage_dir"] / "normalized-config.toml")
        assert normalized.api.auth.password_hash == "api-auth-password-hash-must-not-export"
        assert normalized.api.auth.session_ttl_hours == 36
        assert normalized.api.auth.trust_loopback is False
        assert normalized.api.auth.trusted_proxies == ["127.0.0.1"]
        assert normalized.api.auth.allowed_bearer_origins == ["https://target.example"]
        assert normalized.api.auth.extension_token_ttl_hours == 48
        assert normalized.api.auth.session_secret != "api-auth-session-secret-must-not-export"
        assert normalized.api.auth.extension_access_enabled is False
        assert normalized.api.auth.extension_access_keys == []

        status = local.get("/api/migration/status", headers=_LOCAL_HEADERS).json()
        assert status["state"] == "staged"
        assert status["request_id"] == body["request_id"]
        assert (
            status["source_omitted_environment_variables"]
            == body["source_omitted_environment_variables"]
        )
        assert (
            status["target_active_environment_variables"]
            == body["target_active_environment_variables"]
        )

        cancelled = local.delete("/api/migration/pending", headers=_LOCAL_HEADERS)
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        assert cancelled.json()["cancelled"] is True
        assert config_path.read_bytes() == before_config
        assert not (migration_root / "pending.json").exists()
        assert local.delete("/api/migration/pending", headers=_LOCAL_HEADERS).json() == {
            "state": "idle",
            "cancelled": False,
            "restart_required": False,
            "message": "当前没有待导入迁移包。",
        }


def test_migration_status_remains_available_in_degraded_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _save_runtime_config(tmp_path, monkeypatch, degraded=True)
    migration_id = "a" * 32
    migration_root = root / ".openbiliclaw-migration"
    stage_dir = migration_root / f"pending-{migration_id}"
    stage_dir.mkdir(parents=True)
    (migration_root / "pending.json").write_text(
        json.dumps(
            {
                "migration_id": migration_id,
                "source_version": "0.3.201",
                "request_id": "b" * 32,
                "stage_dir": stage_dir.name,
            }
        ),
        encoding="utf-8",
    )
    current_config = (root / "config.toml").read_bytes()
    app = create_app()

    with _loopback(app) as client:
        response = client.get("/api/migration/status", headers=_LOCAL_HEADERS)
        cancelled = client.delete("/api/migration/pending", headers=_LOCAL_HEADERS)

    assert response.status_code == 200
    assert response.json()["state"] == "staged"
    assert response.json()["request_id"] == "b" * 32
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["cancelled"] is True
    assert (root / "config.toml").read_bytes() == current_config
    assert not stage_dir.exists()
    assert not (migration_root / "pending.json").exists()


def test_export_uses_locked_runtime_data_dir_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _save_runtime_config(tmp_path, monkeypatch)
    active_data = root / "data"
    next_data = root / "next-data"
    (next_data / "memory").mkdir(parents=True)
    (next_data / "memory" / "profile.json").write_text(
        '{"topic":"wrong-pending-directory"}\n',
        encoding="utf-8",
    )
    app = create_app()

    with _loopback(app) as client:
        pending_config = load_config(root / "config.toml")
        pending_config.data_dir = str(next_data)
        save_config(pending_config, root / "config.toml", autostart_authoritative=True)

        exported = client.post(
            "/api/migration/export",
            headers=_LOCAL_HEADERS,
            json={"frontend": {}},
        )

    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        profile = json.loads(archive.read("data/memory/profile.json"))
    assert profile == {"topic": "迁移测试"}
    assert active_data == app.state.active_runtime_data_path


def test_status_exposes_inflight_import_request_for_disconnect_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_runtime_config(tmp_path, monkeypatch)
    app = create_app()
    request_id = "123e4567e89b42d3a456426614174000"
    app.state.migration_import_request_id = request_id
    app.state.migration_import_phase = "validating"

    with _loopback(app) as client:
        response = client.get("/api/migration/status", headers=_LOCAL_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "state": "processing",
        "request_id": request_id,
        "phase": "validating",
        "restart_required": False,
        "message": "迁移包仍在上传或校验，当前数据尚未改动。",
    }


def test_export_response_cleans_plaintext_when_asgi_start_send_fails(tmp_path: Path) -> None:
    export_root = tmp_path / "openbiliclaw-export-sensitive"
    export_root.mkdir()
    (export_root / "archive.obcbackup").write_bytes(b"plaintext-secret")
    releases = 0

    def release() -> None:
        nonlocal releases
        releases += 1

    async def content() -> AsyncIterator[bytes]:
        yield b"plaintext-secret"

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def fail_send(_message: dict[str, object]) -> None:
        raise OSError("client disconnected before response start")

    response = _MigrationArchiveStreamingResponse(
        content(),
        cleanup_directory=export_root,
        release_callback=release,
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "path": "/api/migration/export",
        "headers": [],
    }

    with pytest.raises(ClientDisconnect):
        asyncio.run(response(scope, receive, fail_send))

    assert not export_root.exists()
    assert releases == 1


def test_export_response_closes_stream_before_windows_cleanup_and_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    export_root = tmp_path / "openbiliclaw-export-midstream"
    export_root.mkdir()
    (export_root / "archive.obcbackup").write_bytes(b"plaintext-secret")
    events: list[str] = []
    first_body_started = asyncio.Event()
    block_send = asyncio.Event()
    original_rmtree = shutil.rmtree

    async def content() -> AsyncIterator[bytes]:
        events.append("opened")
        try:
            yield b"plaintext-secret"
        finally:
            events.append("closed")

    async def receive() -> dict[str, object]:
        await first_body_started.wait()
        return {"type": "http.disconnect"}

    async def stalled_send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body":
            first_body_started.set()
            await block_send.wait()

    def windows_like_rmtree(path: Path, *, ignore_errors: bool = False) -> None:
        stream_closed = "closed" in events
        events.append(f"delete:{stream_closed}")
        # Windows cannot unlink this directory while the archive handle is open.
        if stream_closed:
            original_rmtree(path, ignore_errors=ignore_errors)

    def release() -> None:
        events.append("release")

    monkeypatch.setattr("openbiliclaw.api.app.shutil.rmtree", windows_like_rmtree)
    response = _MigrationArchiveStreamingResponse(
        content(),
        cleanup_directory=export_root,
        release_callback=release,
        background=BackgroundTask(
            windows_like_rmtree,
            export_root,
            ignore_errors=True,
        ),
    )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET",
        "path": "/api/migration/export",
        "headers": [],
    }

    asyncio.run(response(scope, receive, stalled_send))

    assert events == ["opened", "delete:False", "closed", "delete:True", "release"]
    assert not export_root.exists()


def test_pending_import_can_be_cancelled_while_guided_init_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _save_runtime_config(tmp_path, monkeypatch)
    migration_id = "f" * 32
    migration_root = root / ".openbiliclaw-migration"
    stage_dir = migration_root / f"pending-{migration_id}"
    stage_dir.mkdir(parents=True)
    (migration_root / "pending.json").write_text(
        json.dumps(
            {
                "migration_id": migration_id,
                "source_version": "0.3.201",
                "stage_dir": stage_dir.name,
            }
        ),
        encoding="utf-8",
    )
    app = create_app()

    with _loopback(app) as client:
        assert app.state.runtime_context.init_coordinator.try_start("active-migration-test")
        response = client.delete("/api/migration/pending", headers=_LOCAL_HEADERS)

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"
    assert not stage_dir.exists()
