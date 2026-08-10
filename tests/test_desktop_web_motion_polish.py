import re
from pathlib import Path

APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js")
APP_CSS = Path("src/openbiliclaw/web/desktop/assets/css/app.css")


def _function_body(js: str, name: str) -> str:
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n    \}}",
        js,
        flags=re.S,
    )
    assert match is not None, f"{name} function not found"
    return match.group("body")


def test_scrollbar_gutter_and_page_enter_animation_are_declared() -> None:
    app_css = APP_CSS.read_text(encoding="utf-8")

    assert "html { min-width: 0; background: var(--bg); scrollbar-gutter: stable; }" in app_css
    assert "@keyframes page-enter" in app_css
    # Page entry is intentionally opacity-only. Translating the whole page
    # changes card geometry while Playwright/the user is targeting controls,
    # which can move buttons under the pointer during initial hydration.
    assert "from { opacity: 0; }" in app_css
    assert "to { opacity: 1; }" in app_css
    assert "translate: 0 6px" not in app_css
    assert "#homePage, #contentLibraryPage, #profilePage, #chatPage, #settingsPage" in app_css
    assert "animation: page-enter" in app_css


def test_drawer_close_animation_css_and_reduced_motion_guard_exist() -> None:
    app_css = APP_CSS.read_text(encoding="utf-8")

    assert ".drawer.is-closing, .overlay.is-closing { display: block; }" in app_css
    assert ".drawer.is-closing .drawer-panel" in app_css
    assert ".drawer.is-closing .scrim" in app_css
    assert "@keyframes drawer-panel-out" in app_css
    assert "@keyframes drawer-backdrop-out" in app_css
    assert "translateX(16px)" in app_css
    assert "@media (prefers-reduced-motion: reduce)" in app_css
    assert "animation-duration: 0.01ms" in app_css
    assert ".side-drawer-panel" in app_css


def test_close_panel_uses_is_closing_with_timeout_and_message_cleanup() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    close_body = _function_body(app_js, "closePanel")

    assert '!panel.classList.contains("is-open")' in close_body
    assert 'panel.classList.contains("is-closing")' in close_body
    assert 'panel.classList.add("is-closing")' in close_body
    assert 'addEventListener("animationend"' in close_body
    assert "panel._closeTimer" in close_body
    assert "window.setTimeout(finishClose, 220)" in close_body
    assert 'panel.classList.remove("is-open", "is-closing", "from-mobile-menu")' in close_body
    assert 'if (id === "messagesDrawer")' in close_body
    assert "state.messageListSnapshot = null" in close_body
    assert "state.messageListDomLocked = false" in close_body


def test_open_panel_cancels_in_flight_close() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")
    open_body = _function_body(app_js, "openPanel")

    assert "panel._closeTimer" in open_body
    assert "window.clearTimeout(panel._closeTimer)" in open_body
    assert 'removeEventListener("animationend"' in open_body
    assert 'panel.classList.remove("is-closing")' in open_body
    assert 'panel.classList.add("is-open")' in open_body


def test_delight_drag_thresholds_and_autoload_margin_remain_calibrated() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "const _DELIGHT_DRAG_DEAD_ZONE = 10;" in app_js
    assert "Math.abs(dx) < _DELIGHT_DRAG_DEAD_ZONE" in app_js
    assert "wasActive && Math.abs(dx) >= 50" in app_js
    assert "const AUTO_LOAD_ROOT_MARGIN_PX = 50;" in app_js


def test_desktop_delight_has_available_width_container_rules() -> None:
    app_css = APP_CSS.read_text(encoding="utf-8")

    assert "container-type: inline-size" in app_css
    assert "container-name: desktop-main" in app_css
    assert "@container desktop-main (max-width:" in app_css
