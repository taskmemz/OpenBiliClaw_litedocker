"""Static regression tests for the mobile web engagement-stats row.

The stats row (▶ views · 👍 likes · 💬 comments · 🔁 shares · ⭐ favorites · 弹幕 danmaku)
must render on both recommendation cards and the delight tray, mirroring the
desktop web surface. These are byte-level source contracts, not runtime tests.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECOMMEND_JS = ROOT / "src/openbiliclaw/web/js/views/recommend.js"
VIEW_MODELS_JS = ROOT / "src/openbiliclaw/web/js/view-models.js"
APP_CSS = ROOT / "src/openbiliclaw/web/css/app.css"


def _function_source(name: str) -> str:
    js = RECOMMEND_JS.read_text()
    marker = f"function {name}("
    start = js.find(marker)
    assert start >= 0, f"{name} function not found"
    opening_brace = js.find("{", start)
    assert opening_brace >= 0, f"{name} opening brace not found"
    depth = 0
    for index in range(opening_brace, len(js)):
        if js[index] == "{":
            depth += 1
        elif js[index] == "}":
            depth -= 1
            if depth == 0:
                return js[start : index + 1]
    raise AssertionError(f"{name} closing brace not found")


def test_view_models_exposes_stats_formatter() -> None:
    """view-models.js owns the shared formatter + stats builder."""

    js = VIEW_MODELS_JS.read_text()

    assert "export function formatCountCn(" in js
    assert "export function recommendationStats(" in js
    # Chinese unit condensation.
    assert "亿" in js
    assert "万" in js
    # Every engagement segment, gated on > 0, joined with " · ".
    assert "▶ " in js
    assert "👍 " in js
    assert "💬 " in js
    assert "🔁 " in js
    assert "⭐ " in js
    assert "弹幕 " in js
    assert 'segments.join(" · ")' in js
    assert "formatCountCn(item.source_rank)" not in js
    assert "segments.push(`排名 #${sourceRank}`)" in js


def test_normalizers_thread_count_fields() -> None:
    """Both recommendation + delight normalizers carry the raw count fields."""

    js = VIEW_MODELS_JS.read_text()

    for field in (
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "favorite_count",
        "danmaku_count",
        "rating_score",
        "rating_count",
        "source_rank",
    ):
        # Appears in both normalizeRecommendation and normalizeDelightCandidate.
        assert js.count(f"{field}: Number(item?.{field}") >= 2, field


def test_recommend_card_renders_stats() -> None:
    """The recommendation card renders the stats line only when non-empty."""

    js = RECOMMEND_JS.read_text()

    assert "recommendationStats" in js
    assert 'class="card-stats"' in js
    # Guarded so an empty string paints nothing.
    assert "recommendationStats(item) ?" in js


def test_delight_tray_renders_stats() -> None:
    """The delight tray renders the same stats line near the reason."""

    js = RECOMMEND_JS.read_text()

    assert "const statsText = recommendationStats(d);" in js
    assert "delight-stats" in js
    assert "statsText ?" in js


def test_card_stats_css_is_muted() -> None:
    """The stats row uses the muted meta styling."""

    css = APP_CSS.read_text()

    assert ".card-stats {" in css
    assert "var(--text-muted)" in css


def test_recommendation_publication_time_is_rendered_only_when_non_empty() -> None:
    """Recommendation metadata uses the shared publication formatter."""

    js = RECOMMEND_JS.read_text()

    render_card = _function_source("renderCard")

    assert "const publishedHtml = publishedTimeHtml(item);" in render_card
    assert "${publishedHtml}" in js


def test_delight_publication_time_is_rendered_only_when_non_empty() -> None:
    """Delight metadata follows the same optional publication contract."""

    js = RECOMMEND_JS.read_text()

    render_delight = _function_source("renderDelightTray")

    assert "const publishedHtml = publishedTimeHtml(d);" in render_delight
    assert js.count("${publishedHtml}") >= 3


def test_publication_html_escapes_text_and_exact_tooltip() -> None:
    helper = _function_source("publishedTimeHtml")

    assert "const display = getPublishedTimeDisplay(item);" in helper
    assert 'if (!display) return "";' in helper
    assert "esc(display.text)" in helper
    assert "esc(display.title)" in helper
    assert 'title="${esc(display.title)}"' in helper
    assert '<span class="card-published-time"' in helper


def test_publication_time_css_is_muted() -> None:
    """Publication time remains secondary metadata."""

    css = APP_CSS.read_text()

    assert ".card-published-time {" in css
    assert "var(--text-muted)" in css
