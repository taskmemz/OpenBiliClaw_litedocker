"""Executable capability boundaries and field mappings for the Weibo contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from openbiliclaw.api.source_auth.forms import build_credential_form
from openbiliclaw.api.source_auth.write import CREDENTIAL_SPECS
from openbiliclaw.config import Config
from openbiliclaw.saved_sync.identity import is_native_save_local_only
from openbiliclaw.sources.weibo import weibo_post_to_content
from openbiliclaw.sources.weibo_tasks import (
    is_weibo_account_key,
    weibo_account_key,
    weibo_bootstrap_item_key,
    weibo_bootstrap_items_to_events,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = tomllib.loads(
    (ROOT / "docs/platform-source-contract.weibo.toml").read_text(encoding="utf-8")
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _extension_runtime_source() -> str:
    files = sorted((ROOT / "extension/src").rglob("*.js")) + sorted(
        (ROOT / "extension/src").rglob("*.ts")
    )
    return "\n".join(path.read_text(encoding="utf-8") for path in files)


def _mapped_post() -> object:
    return weibo_post_to_content(
        {
            "id": "5023456789012345",
            "text": "公开微博正文",
            "attitudes_count": 17,
            "comments_count": 9,
            "reposts_count": 4,
        }
    )


def test_weibo_profile_signals_require_logged_in_bootstrap() -> None:
    assert CONTRACT["integration_level"] == "full"
    assert CONTRACT["profile"]["signals"] is True
    assert CONTRACT["profile"]["refresh_mode"] == "init-only"
    tasks = _read("src/openbiliclaw/sources/weibo_tasks.py")
    assert "weibo_favorites" in tasks
    assert "weibo_following" in tasks
    assert "weibo_mentions" in tasks


def test_weibo_profile_incremental_is_init_only() -> None:
    assert CONTRACT["profile"]["incremental"] is False
    assert CONTRACT["profile"]["refresh_mode"] == "init-only"
    assert "weibo_incremental" not in _read("src/openbiliclaw/runtime/refresh.py")


def test_weibo_profile_refresh_mode_is_init_only() -> None:
    assert CONTRACT["profile"]["refresh_mode"] == "init-only"


def test_weibo_profile_account_binding_is_opaque_and_scope_partitioned() -> None:
    account_key = weibo_account_key("2803301701")
    assert is_weibo_account_key(account_key)
    assert weibo_account_key("2803301701") == account_key
    assert weibo_account_key("not-a-uid") == ""
    item = {
        "scope": "weibo_favorites",
        "content_id": "5330495517763368",
        "title": "收藏的公开微博",
    }
    assert weibo_bootstrap_item_key(item, account_key=account_key).startswith(
        f"{account_key}:weibo_favorites:"
    )
    events = weibo_bootstrap_items_to_events([item], account_key=account_key)
    assert events[0]["metadata"]["account_key"] == account_key


def test_weibo_extension_task_is_browser_backed() -> None:
    assert CONTRACT["extension"]["task"] == "browser-task"
    assert "executeWeiboTask" in _extension_runtime_source()
    assert "weibo-task-dispatcher" in _extension_runtime_source()


def test_weibo_extension_task_marker_is_present() -> None:
    assert CONTRACT["extension"]["task_marker"] is True
    assert "openbiliclaw_weibo_task" in _read("extension/src/content/weibo/task-mode.ts")


def test_weibo_extension_background_is_present() -> None:
    assert CONTRACT["extension"]["background"] is True
    manifest = json.loads(_read("extension/manifest.json"))
    assert any("weibo" in str(value).casefold() for value in manifest["host_permissions"])
    assert "dist/content/weibo.js" in json.dumps(manifest, ensure_ascii=False)


def test_weibo_extension_early_response_is_explicitly_false() -> None:
    assert CONTRACT["extension"]["early_response"] is False
    runtime = _extension_runtime_source().casefold()
    assert "webrequest" not in runtime
    assert "onbeforerequest" not in runtime


def test_weibo_extension_cookie_sync_is_present_but_boolean_only() -> None:
    assert CONTRACT["extension"]["cookie_sync"] is True
    runtime = _extension_runtime_source().casefold()
    assert "weibo_login_state_sync" in runtime
    assert "/sources/weibo/credential" in runtime
    assert "cookie" in runtime


def test_weibo_setup_surface_is_in_guided_init() -> None:
    assert CONTRACT["surfaces"]["setup"] is True
    shared = _read("src/openbiliclaw/web/shared/source-status.js")
    assert "weibo: Object.freeze({ guidedInit: true })" in shared
    assert "SOURCE_KEYS.filter((key) => SOURCE_CAPABILITIES[key]?.guidedInit === true)" in shared


def test_weibo_mobile_popup_platform_filter_is_product_level_exclusion() -> None:
    scope = CONTRACT["surface_scope"]
    assert scope["desktop_platform_filter"] is True
    assert scope["mobile_platform_filter"] is False
    assert scope["extension_popup_platform_filter"] is False
    desktop = _read("src/openbiliclaw/web/desktop/assets/js/app.js")
    assert '{ key: "weibo", label: "微博" }' in desktop
    assert "sourceFilter" in desktop
    assert "platform filter" in str(scope["platform_filter_policy"]).casefold()


def test_weibo_credentials_surface_is_login_state_only() -> None:
    assert CONTRACT["surfaces"]["credentials"] is True
    spec = CREDENTIAL_SPECS["weibo"]
    form = build_credential_form("weibo", cfg=Config())
    assert spec.kinds == ("login_state",)
    assert form.kind == "none"
    assert form.required_keys == []


def test_weibo_favorite_engagement_is_unavailable() -> None:
    assert CONTRACT["engagement"]["favorite"] == "unavailable"
    content = _mapped_post()
    assert content is not None
    assert content.favorite_count == 0


def test_weibo_danmaku_engagement_is_unavailable() -> None:
    assert CONTRACT["engagement"]["danmaku"] == "unavailable"
    content = _mapped_post()
    assert content is not None
    assert content.danmaku_count == 0


def test_weibo_like_engagement_is_mapped() -> None:
    assert CONTRACT["engagement"]["like"] == "mapped"
    content = _mapped_post()
    assert content is not None
    assert content.like_count == 17


def test_weibo_comment_engagement_is_mapped() -> None:
    assert CONTRACT["engagement"]["comment"] == "mapped"
    content = _mapped_post()
    assert content is not None
    assert content.comment_count == content.reply_count == 9


def test_weibo_deep_link_uses_browser_fallback() -> None:
    assert CONTRACT["media"]["deep_link"] == "browser-fallback"
    launch = _read("src/openbiliclaw/web/js/app-launch.js")
    node_test = _read("tests/js/mobile-app-launch.test.mjs")
    assert "weibo://" not in launch
    assert 'buildAppDeepLink("https://m.weibo.cn/detail/5023456789012345"), ""' in node_test


def test_weibo_native_save_is_excluded() -> None:
    assert CONTRACT["media"]["native_save"] is False
    assert is_native_save_local_only("weibo") is True
    adapters = _read("src/openbiliclaw/saved_sync/adapters/extension.py")
    assert 'ExtensionAdapterDefinition("weibo"' not in adapters
    assert "local_only_source" in _read("src/openbiliclaw/saved_sync/service.py")
