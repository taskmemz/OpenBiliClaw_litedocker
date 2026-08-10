from pathlib import Path

MOBILE_HISTORY = Path("src/openbiliclaw/web/js/views/history.js")
MOBILE_SAVED = Path("src/openbiliclaw/web/js/views/saved.js")
DESKTOP_APP = Path("src/openbiliclaw/web/desktop/assets/js/app.js")
MOBILE_CSS = Path("src/openbiliclaw/web/css/app.css")
DESKTOP_CSS = Path("src/openbiliclaw/web/desktop/assets/css/app.css")


def test_web_history_surfaces_use_opaque_cursor_without_an_empty_initial_cursor() -> None:
    mobile = MOBILE_HISTORY.read_text(encoding="utf-8")
    desktop = DESKTOP_APP.read_text(encoding="utf-8")

    for source in (mobile, desktop):
        assert "nextCursor" in source
        assert "hasMore" in source
        assert "nextOffset" not in source
        assert "incomingTotal" in source
        assert "nextCursor:" in source
        assert "hasMore:" in source

    assert 'append ? page.nextCursor : ""' in mobile
    assert 'if (append && page.nextCursor) query.set("cursor", page.nextCursor)' in desktop
    assert 'query.set("offset"' not in desktop


def test_web_history_removed_contexts_render_and_restore_independently() -> None:
    for path in (MOBILE_HISTORY, DESKTOP_APP):
        source = path.read_text(encoding="utf-8")

        assert "Array.isArray(item?.contexts)" in source
        assert '["watch_later", "favorite"].includes' in source
        assert "context.restored = true" in source
        assert "context.context" in source
        assert "item.context === context.context" in source


def test_web_history_rerenders_restore_focus_without_changing_scroll() -> None:
    mobile = MOBILE_HISTORY.read_text(encoding="utf-8")
    desktop = DESKTOP_APP.read_text(encoding="utf-8")

    assert "restoreHistoryFocus(focusToken" in mobile
    assert "preventScroll: true" in mobile
    assert "container.scrollTop = token.scrollTop" in mobile
    assert 'historyFocusToken({ action: "refresh" })' in mobile
    assert "restoreContentHistoryFocus(focusToken" in desktop
    assert "preventScroll: true" in desktop
    assert 'window.scrollTo({ top: token.scrollY, behavior: "auto" })' in desktop


def test_web_history_surfaces_keep_items_and_offer_load_more_retry() -> None:
    for path in (MOBILE_HISTORY, DESKTOP_APP):
        source = path.read_text(encoding="utf-8")
        load_start = (
            source.index("async function loadContentHistoryCategory")
            if path == DESKTOP_APP
            else source.index("async function loadCategory")
        )
        refresh_start = (
            source.index("async function refreshContentHistory", load_start)
            if path == DESKTOP_APP
            else source.index("async function refreshHistory", load_start)
        )
        load_body = source[load_start:refresh_start]

        assert "page.error" in source
        assert '"重试加载更多"' in source
        assert 'role="${page.error ? "alert" : "status"}"' in source
        assert "page.error || page.notice" in source
        assert "page.items = []" not in load_body


def test_web_history_shown_click_refreshes_after_successful_report() -> None:
    for path in (MOBILE_HISTORY, DESKTOP_APP):
        source = path.read_text(encoding="utf-8")

        assert 'if (category === "shown")' in source
        assert "clickReport.then((reported)" in source
        assert "if (reported) return refresh" in source


def test_desktop_history_broken_cover_has_an_accessible_fallback() -> None:
    source = DESKTOP_APP.read_text(encoding="utf-8")
    css = DESKTOP_CSS.read_text(encoding="utf-8")

    assert '"#historySections .history-card-media img"' in source
    assert 'image.addEventListener("error"' in source
    assert 'media.classList.add("is-fallback")' in source
    assert "media.innerHTML = HISTORY_IMAGE_ICON" in source
    assert ".history-card-media.is-fallback" in css


def test_mobile_history_broken_cover_reveals_icon_and_platform_label() -> None:
    source = MOBILE_HISTORY.read_text(encoding="utf-8")
    css = MOBILE_CSS.read_text(encoding="utf-8")

    assert "const fallbackMedia = `${IMAGE_ICON}" in source
    assert 'class="history-card-fallback-label"' in source
    assert "this.parentElement.classList.add('is-fallback');this.remove()" in source
    assert ".history-card-media:not(.is-fallback) > svg" in css
    assert ".history-card-media:not(.is-fallback) > .history-card-fallback-label" in css


def test_saved_lists_replace_failed_and_cached_broken_covers() -> None:
    mobile = MOBILE_SAVED.read_text(encoding="utf-8")
    desktop = DESKTOP_APP.read_text(encoding="utf-8")
    desktop_css = DESKTOP_CSS.read_text(encoding="utf-8")

    assert 'image.addEventListener("error", showFallback, { once: true })' in mobile
    assert 'data-cover-src="${esc(cover.src)}"' in mobile
    assert "image.src = coverSrc" in mobile
    assert "image.complete && image.naturalWidth === 0" in mobile
    assert 'fallback.className = "saved-card-cover saved-card-cover-empty"' in mobile
    assert "image.replaceWith(fallback)" in mobile

    assert "function bindSavedCoverFallback" in desktop
    assert 'image.addEventListener("error", showFallback, { once: true })' in desktop
    assert "image.complete && image.naturalWidth === 0" in desktop
    assert 'fallback.className = "saved-cover-fallback"' in desktop
    assert ".saved-cover-fallback" in desktop_css


def test_history_page_messages_are_visually_distinct_and_live_announced() -> None:
    for path in (MOBILE_CSS, DESKTOP_CSS):
        source = path.read_text(encoding="utf-8")

        assert ".history-page-message.is-error" in source
        assert ".history-page-message.is-notice" in source
