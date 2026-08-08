"""桌面 Web 配置保存与后台应用状态的真实浏览器回归。"""

from __future__ import annotations

import json
import mimetypes
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

playwright_api = pytest.importorskip("playwright.sync_api")
Page = playwright_api.Page
expect = playwright_api.expect
sync_playwright = playwright_api.sync_playwright

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]


def _initial_config() -> dict[str, Any]:
    return {
        "language": "zh",
        "llm": {
            "default_provider": "ollama",
            "default_chain": ["ollama"],
            "instances": {
                "ollama": {
                    "provider": "ollama",
                    "enabled": True,
                    "model": "qwen3:8b",
                }
            },
            "ollama": {"model": "qwen3:8b"},
            "embedding": {"provider": "ollama", "model": "bge-m3"},
        },
        "sources": {"bilibili": {"enabled": True}},
        "scheduler": {
            "pool_source_shares": {
                "bilibili": 5,
                "xiaohongshu": 1,
                "douyin": 1,
                "youtube": 1,
                "twitter": 1,
                "zhihu": 1,
                "reddit": 1,
                "bangumi": 1,
            }
        },
    }


class SettingsSaveStub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.revision = 6
        self.apply_state = "idle"
        self.applied_revision = 6
        self.complete_before_response = False
        self.config = _initial_config()
        self.saved_payloads: list[dict[str, Any]] = []


def _json_response(
    handler: BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    with suppress(BrokenPipeError):
        handler.wfile.write(body)


@pytest.fixture()
def settings_save_server() -> Iterator[tuple[str, SettingsSaveStub]]:
    state = SettingsSaveStub()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in {"/web", "/web/", "/web/index.html"}:
                return self._serve_file(
                    ROOT / "src/openbiliclaw/web/desktop/index.html",
                    "text/html",
                )
            if path.startswith("/web/assets/"):
                relative = path.removeprefix("/web/assets/")
                return self._serve_file(ROOT / "src/openbiliclaw/web/desktop/assets" / relative)
            if path.startswith("/shared/"):
                relative = path.removeprefix("/shared/")
                return self._serve_file(ROOT / "src/openbiliclaw/web/shared" / relative)
            if path == "/api/ping":
                return _json_response(self, {"ok": True})
            if path == "/api/health":
                return _json_response(self, {"ok": True, "embedding_ready": True})
            if path == "/api/auth/status":
                return _json_response(self, {"enabled": False, "authenticated": True})
            if path == "/api/config":
                with state.lock:
                    config = json.loads(json.dumps(state.config))
                return _json_response(self, {"config": config})
            if path == "/api/config/apply-status":
                with state.lock:
                    snapshot = {
                        "state": state.apply_state,
                        "requested_revision": state.revision,
                        "applied_revision": state.applied_revision,
                        "message": "配置已保存，正在后台应用。",
                        "error": "",
                        "updated_at": "2026-08-06T12:00:00+08:00",
                    }
                return _json_response(self, snapshot)
            if path == "/api/runtime-status":
                return _json_response(
                    self,
                    {
                        "initialized": True,
                        "pool_available_count": 0,
                        "pool_size": 0,
                        "pool_refresh_state": "idle",
                        "pool_source_shares": {"bilibili": 1.0},
                        "configured_sources": {"bilibili": {"enabled": True}},
                        "unread_count": 0,
                    },
                )
            if path == "/api/init-status":
                return _json_response(
                    self,
                    {
                        "initialized": True,
                        "running": False,
                        "can_start": False,
                        "reason": "already_initialized",
                        "stages": [],
                        "prerequisites": {
                            "bilibili_logged_in": True,
                            "llm_ready": True,
                            "embedding_ready": True,
                            "enabled_platforms": ["bilibili"],
                        },
                    },
                )
            if path == "/api/recommendations":
                return _json_response(self, {"items": []})
            if path == "/api/recommendations/platform-availability":
                return _json_response(self, {"total_available": 0, "by_platform": {}})
            if path == "/api/profile-summary":
                return _json_response(self, {"initialized": True})
            if path == "/api/activity-feed":
                return _json_response(self, {"items": [], "has_more": False, "next_cursor": ""})
            if path in {"/api/delight/pending-batch", "/api/notifications/pending"}:
                return _json_response(self, {"items": []})
            if path == "/api/chat/turns":
                return _json_response(self, {"items": []})
            if path == "/api/qr-info":
                return _json_response(self, {"lan_ip": "127.0.0.1"})
            return _json_response(self, {}, 404)

        def do_PUT(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path != "/api/config":
                return _json_response(self, {}, 404)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            with state.lock:
                state.revision += 1
                state.apply_state = "applied" if state.complete_before_response else "applying"
                state.config = payload
                state.saved_payloads.append(payload)
                revision = state.revision
                if state.complete_before_response:
                    state.applied_revision = revision
            return _json_response(
                self,
                {
                    "ok": True,
                    "config": payload,
                    "message": "配置已保存，正在后台应用。",
                    "reloaded": False,
                    "rollback_applied": False,
                    "restart_required": False,
                    "apply_state": "queued",
                    "apply_revision": revision,
                },
                202,
            )

        def _serve_file(self, path: Path, content_type: str | None = None) -> None:
            if not path.exists():
                return _json_response(self, {"error": "not_found"}, 404)
            body = path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join(timeout=5)


_FAKE_WEBSOCKET = """
window.__obcSockets = [];
window.WebSocket = class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    this._listeners = {};
    window.__obcSockets.push(this);
    setTimeout(() => this._emit('open', { type: 'open' }), 0);
  }
  addEventListener(type, handler) {
    (this._listeners[type] = this._listeners[type] || []).push(handler);
  }
  removeEventListener(type, handler) {
    const listeners = this._listeners[type] || [];
    this._listeners[type] = listeners.filter((item) => item !== handler);
  }
  _emit(type, event) {
    for (const handler of (this._listeners[type] || []).slice()) handler(event);
    const inline = this['on' + type];
    if (typeof inline === 'function') inline(event);
  }
  send() {}
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this._emit('close', { type: 'close', code: 1000, reason: '', wasClean: true });
  }
};
window.__obcPushRuntime = (payload) => {
  const socket = window.__obcSockets.at(-1);
  if (!socket) throw new Error('no live runtime socket');
  socket._emit('message', { data: JSON.stringify(payload) });
};
"""


@pytest.fixture()
def chromium_page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:  # pragma: no cover - 取决于本机浏览器安装
            browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(_FAKE_WEBSOCKET)
        yield page
        browser.close()


def test_settings_save_unlocks_before_runtime_apply_finishes(
    settings_save_server: tuple[str, SettingsSaveStub],
    chromium_page: Page,
) -> None:
    base_url, stub = settings_save_server
    page = chromium_page
    page.goto(f"{base_url}/web/")

    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="平台源").click()
    share = page.get_by_label("Bilibili 候选池占比")
    save = page.get_by_role("button", name="保存配置")
    status = page.locator("#settingsSaveMsg")
    bar = page.locator("#settingsSaveBar")

    share.fill("2")
    save.click()

    expect(status).to_have_text("配置已保存，正在后台应用…")
    expect(save).to_have_text("保存配置")
    expect(bar).to_have_attribute("data-save-state", "applying")
    assert len(stub.saved_payloads) == 1
    assert stub.saved_payloads[0]["scheduler"]["pool_source_shares"]["bilibili"] == 2

    page.evaluate("window.__obcPushRuntime({type: 'config_reloaded', revision: 7})")
    expect(status).to_have_text("配置已应用")
    expect(bar).to_have_attribute("data-save-state", "applied")

    share.fill("3")
    save.click()
    expect(status).to_have_text("配置已保存，正在后台应用…")
    expect(bar).to_have_attribute("data-save-state", "applying")

    page.evaluate("window.__obcPushRuntime({type: 'config_reloaded', revision: 7})")
    expect(status).to_have_text("配置已保存，正在后台应用…")
    expect(bar).to_have_attribute("data-save-state", "applying")

    share.fill("4")
    page.evaluate("window.__obcPushRuntime({type: 'config_reloaded', revision: 8})")
    expect(status).to_have_text("已修改 1 项，未保存")
    expect(bar).to_have_attribute("data-save-state", "dirty")


def test_external_runtime_config_event_rehydrates_settings(
    settings_save_server: tuple[str, SettingsSaveStub],
    chromium_page: Page,
) -> None:
    base_url, stub = settings_save_server
    page = chromium_page
    page.goto(f"{base_url}/web/")

    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="平台源").click()
    share = page.get_by_label("Bilibili 候选池占比")
    expect(share).to_have_value("5")

    with stub.lock:
        stub.revision = 7
        stub.applied_revision = 7
        stub.apply_state = "applied"
        stub.config["scheduler"]["pool_source_shares"]["bilibili"] = 7
    page.evaluate("window.__obcPushRuntime({type: 'config_reloaded', revision: 7})")

    expect(share).to_have_value("7", timeout=3000)
    expect(page.locator("#settingsSaveMsg")).to_have_text("配置已应用")


def test_settings_save_recovers_terminal_status_that_wins_the_response_race(
    settings_save_server: tuple[str, SettingsSaveStub],
    chromium_page: Page,
) -> None:
    base_url, stub = settings_save_server
    stub.complete_before_response = True
    page = chromium_page
    page.goto(f"{base_url}/web/")

    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="平台源").click()
    page.get_by_label("Bilibili 候选池占比").fill("2")
    page.get_by_role("button", name="保存配置").click()

    expect(page.locator("#settingsSaveMsg")).to_have_text("配置已应用")
    expect(page.locator("#settingsSaveBar")).to_have_attribute("data-save-state", "applied")


def test_settings_failure_rehydrates_rollback_without_overwriting_new_drafts(
    settings_save_server: tuple[str, SettingsSaveStub],
    chromium_page: Page,
) -> None:
    base_url, stub = settings_save_server
    page = chromium_page
    page.goto(f"{base_url}/web/")

    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="平台源").click()
    share = page.get_by_label("Bilibili 候选池占比")
    share.fill("2")
    page.get_by_role("button", name="保存配置").click()
    expect(page.locator("#settingsSaveMsg")).to_have_text("配置已保存，正在后台应用…")

    with stub.lock:
        stub.apply_state = "failed"
        stub.config["scheduler"]["pool_source_shares"]["bilibili"] = 5
    page.evaluate("window.__obcPushRuntime({type: 'config_reload_failed', revision: 7})")

    expect(page.locator("#settingsSaveMsg")).to_have_text("配置应用失败，已恢复上一次生效配置")
    expect(share).to_have_value("5")

    share.fill("4")
    page.evaluate("window.__obcPushRuntime({type: 'config_reload_failed', revision: 7})")
    expect(page.locator("#settingsSaveMsg")).to_have_text("已修改 1 项，未保存")
    expect(share).to_have_value("4")


def test_failed_apply_refreshes_canonical_snapshot_behind_new_draft(
    settings_save_server: tuple[str, SettingsSaveStub],
    chromium_page: Page,
) -> None:
    base_url, stub = settings_save_server
    page = chromium_page
    page.goto(f"{base_url}/web/")

    page.get_by_role("button", name="设置", exact=True).click()
    page.get_by_role("tab", name="平台源").click()
    share = page.get_by_label("Bilibili 候选池占比")
    share.fill("2")
    page.get_by_role("button", name="保存配置").click()
    expect(page.locator("#settingsSaveMsg")).to_have_text("配置已保存，正在后台应用…")

    share.fill("4")
    with stub.lock:
        stub.revision = 8
        stub.apply_state = "failed"
        stub.config["scheduler"]["pool_source_shares"]["bilibili"] = 5
    page.evaluate("window.__obcPushRuntime({type: 'config_reload_failed', revision: 8})")

    expect(share).to_have_value("4")
    page.get_by_role("button", name="放弃修改").click()
    expect(share).to_have_value("5")
