"""Static contract checks for V2EX settings and recommendation surfaces."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_v2ex_source_card_round_trips_config() -> None:
    html = (ROOT / "src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'data-source-status="v2ex"' in html
    assert 'data-source-credential="v2ex"' in html
    for element_id in (
        "v2exEnabled",
        "v2exAccessToken",
        "v2exClearToken",
        "v2exUsername",
        "v2exModeSearch",
        "v2exModeNode",
        "v2exModeTab",
        "v2exModeHot",
        "v2exModeLatest",
        "v2exDailySearchBudget",
        "v2exDailyNodeBudget",
        "v2exDailyTabBudget",
        "v2exDailyHotBudget",
        "v2exDailyLatestBudget",
        "v2exRequestInterval",
        "v2exMinInterval",
        "shareV2EX",
    ):
        assert f'id="{element_id}"' in html
        assert f'"{element_id}"' in js
    for element_id in (
        "v2exAcceptBrowserIdentity",
        "v2exRefreshIdentity",
        "v2exIdentityStatus",
    ):
        assert f'id="{element_id}"' in html
        assert f'"#{element_id}"' in js
    assert "V2EX_SOURCE_MODE_FIELDS" in js
    assert 'source_modes: collectCheckedValues(V2EX_SOURCE_MODE_FIELDS, ["search"])' in js
    assert 'v2ex: getIntInput("shareV2EX", 1)' in js
    assert 'requestJsonStrict("/sources/v2ex/identity"' in js
    assert "identity_switch_required" in js
    assert "采用当前浏览器账号" in html


def test_popup_v2ex_source_card_round_trips_config() -> None:
    html = (ROOT / "extension/popup/popup.html").read_text(encoding="utf-8")
    js = (ROOT / "extension/popup/popup.js").read_text(encoding="utf-8")

    assert 'data-source-card="v2ex"' in html
    for element_id in (
        "cfgV2exEnabled",
        "cfgV2exAccessToken",
        "cfgV2exClearToken",
        "cfgV2exUsername",
        "cfgV2exModeSearch",
        "cfgV2exModeNode",
        "cfgV2exModeTab",
        "cfgV2exModeHot",
        "cfgV2exModeLatest",
        "cfgV2exDailySearchBudget",
        "cfgV2exDailyNodeBudget",
        "cfgV2exDailyTabBudget",
        "cfgV2exDailyHotBudget",
        "cfgV2exDailyLatestBudget",
        "cfgV2exRequestInterval",
        "cfgV2exMinInterval",
        "cfgV2exAcceptBrowserIdentity",
        "cfgV2exRefreshIdentity",
        "cfgV2exIdentityStatus",
        "cfgPoolShareV2ex",
    ):
        assert f'id="{element_id}"' in html
        assert element_id in js
    assert "V2EX_SOURCE_MODE_FIELDS" in js
    assert "v2ex:" in js
    assert "fetchV2exIdentity" in js
    assert "acceptV2exBrowserIdentity" in js
    assert "identity_switch_required" in js
    assert "采用当前浏览器账号" in html


def test_v2ex_recommendation_labels_and_urls_are_source_aware() -> None:
    view_model = (ROOT / "src/openbiliclaw/web/js/view-models.js").read_text(encoding="utf-8")
    saved = (ROOT / "src/openbiliclaw/web/js/views/saved.js").read_text(encoding="utf-8")
    helpers = (ROOT / "extension/popup/popup-helpers.js").read_text(encoding="utf-8")
    popup = (ROOT / "extension/popup/popup.js").read_text(encoding="utf-8")

    assert 'v2ex: "V2EX"' in view_model
    assert 'v2ex: "V2EX"' in popup
    assert 'v2ex: "V2EX"' in helpers
    assert 'platform === "v2ex"' in helpers
    assert "v2ex.com" in helpers
    assert "v2ex" in saved


def test_v2ex_no_cover_cards_are_intentional_compact_text_cards() -> None:
    desktop_js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(
        encoding="utf-8"
    )
    desktop_css = (ROOT / "src/openbiliclaw/web/desktop/assets/css/app.css").read_text(
        encoding="utf-8"
    )
    mobile_js = (ROOT / "src/openbiliclaw/web/js/views/recommend.js").read_text(encoding="utf-8")
    mobile_css = (ROOT / "src/openbiliclaw/web/css/app.css").read_text(encoding="utf-8")
    popup_js = (ROOT / "extension/popup/popup.js").read_text(encoding="utf-8")
    popup_html = (ROOT / "extension/popup/popup.html").read_text(encoding="utf-8")

    assert '" is-text-card is-coverless"' in desktop_js
    assert '"video-card is-text-only"' in desktop_js
    assert ".cover.is-text-card.is-coverless { aspect-ratio: auto;" in desktop_css
    assert 'card.classList.add("is-text-only")' in mobile_js
    assert ".card-cover-frame.is-text-card" in mobile_css
    assert "aspect-ratio: auto;" in mobile_css
    assert 'cover.classList.add("is-text-card")' in popup_js
    assert 'cover.classList.add("is-fallback", "is-text-card")' not in popup_js
    assert ".recommendation-cover.is-text-card::after" in popup_html


def test_v2ex_is_available_in_guided_init_and_settings_surfaces() -> None:
    shared = (ROOT / "src/openbiliclaw/web/shared/source-status.js").read_text(encoding="utf-8")
    setup = (ROOT / "src/openbiliclaw/web/setup/index.html").read_text(encoding="utf-8")

    roster = shared.split("]);", 1)[0]
    assert '"bangumi"' in roster
    assert '"linuxdo"' in roster
    assert '"v2ex"' in roster
    assert "v2ex: Object.freeze({ guidedInit: true })" in shared
    assert "INIT_SOURCE_KEYS" in setup
