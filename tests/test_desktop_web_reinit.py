"""Static contract for the desktop-web settings re-init entry (gui-init §4).

The desktop web shares no module system with the popup's reference
implementation (extension/popup/popup-init-control.js + popup.js), so these
string-level assertions keep the settings-page「重新初始化 / 重建画像」button,
its confirm dialog and the force:true payload from drifting away from the
popup implementation.
"""

from pathlib import Path

INDEX_HTML = Path("src/openbiliclaw/web/desktop/index.html")
APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js")


def _index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_desktop_settings_page_has_reinit_entry() -> None:
    html = _index_html()
    assert 'id="reinitBtn"' in html
    assert 'id="reinitStateBadge"' in html
    assert 'id="reinitStatus"' in html
    # The entry lives in the settings page (entry-convergence rule: the
    # recommend-tab CTA stays first-run-only).
    assert "settingsPanelGeneral" in html


def test_desktop_reinit_sends_force_payload_after_confirm() -> None:
    app_js = _app_js()
    assert "handleDesktopReinitClick" in app_js
    assert 'safeBind("#reinitBtn", "click"' in app_js
    # The backend's already-initialized guard is only bypassed by force:true.
    assert "JSON.stringify(payload)" in app_js
    assert "force: true" in app_js
    # A confirm dialog guards the destructive re-pull.
    assert "window.confirm(" in app_js
    # After a successful start the user is returned to the recommend tab where
    # the existing init progress panel becomes visible.
    assert "openHomePage()" in app_js


def test_desktop_reinit_guards_running_and_requires_initialized() -> None:
    app_js = _app_js()
    assert "初始化正在进行中，请等待完成后再重新初始化" in app_js
    assert "系统尚未初始化完成；请先到「推荐」页完成初始化" in app_js
    # The status line reflects the authoritative init-status snapshot.
    assert "renderSettingsReinitStatus" in app_js


def test_desktop_reinit_offers_cognition_reset_option() -> None:
    html = _index_html()
    app_js = _app_js()
    # Optional awareness/insight reset checkbox, excluded from the settings
    # dirty tracker, wired into the force payload.
    assert 'id="reinitResetCognition"' in html
    assert "data-settings-ignore-dirty" in html
    assert "const resetCognition = " in app_js
    assert "payload.reset_cognition = true" in app_js
    assert "清空旧认知观察" in app_js
    # The confirm dialog advertises the automatic pre-re-init backup.
    assert "自动创建备份" in app_js
    assert "data/backups/" in app_js
