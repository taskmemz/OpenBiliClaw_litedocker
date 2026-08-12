import re
from pathlib import Path

APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")
APP_CSS = Path("src/openbiliclaw/web/desktop/assets/css/app.css").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n    \}}", APP_JS, flags=re.S)
    assert match is not None, f"{name} function not found"
    return match.group("body")


def test_normalize_recommendation_parses_card_metadata_numbers() -> None:
    normalize = _function_body("normalizeRecommendation")

    assert "duration: Number(item?.duration ?? 0) || 0" in normalize
    assert "view_count: Number(item?.view_count ?? 0) || 0" in normalize
    assert "like_count: Number(item?.like_count ?? 0) || 0" in normalize
    assert "share_count: Number(item?.share_count ?? 0) || 0" in normalize
    assert "danmaku_count: Number(item?.danmaku_count ?? 0) || 0" in normalize
    assert "up_mid: Number(item?.up_mid ?? 0) || 0" in normalize
    assert 'duration: String(item?.duration ?? "")' not in normalize


def test_card_metadata_helpers_format_duration_and_cn_counts() -> None:
    duration = _function_body("formatDuration")
    count = _function_body("formatCountCn")

    assert "Math.floor(total / 3600)" in duration
    assert 'String(minutes).padStart(2, "0")' in duration
    assert 'String(secondsPart).padStart(2, "0")' in duration
    assert 'return `${minutes}:${String(secondsPart).padStart(2, "0")}`;' in duration
    assert 'if (value <= 0) return "";' in count
    assert "if (value >= 100000000)" in count
    assert "if (value >= 10000)" in count
    assert "return String(value);" in count


def test_card_template_hides_zero_metadata_and_renders_video_only_duration_badge() -> None:
    assert 'const durationBadge = item.content_type === "video" && item.duration > 0' in APP_JS
    assert (
        '<span class="duration-badge">${escapeHtml(formatDuration(item.duration))}</span>' in APP_JS
    )
    assert "${durationBadge}" in APP_JS

    stats = _function_body("recommendationStats")
    assert "if (item.view_count > 0)" in stats
    assert "if (item.like_count > 0)" in stats
    assert "if (item.share_count > 0)" in stats
    assert "if (item.danmaku_count > 0)" in stats
    assert 'return segments.join(" · ");' in stats
    assert '${stats ? `<p class="video-stats">${escapeHtml(stats)}</p>` : ""}' in APP_JS


def test_card_template_links_bilibili_up_author_only_when_mid_exists() -> None:
    assert 'item.platform === "bilibili" && item.up_mid > 0' in APP_JS
    assert 'href="https://space.bilibili.com/${item.up_mid}"' in APP_JS
    assert 'class="up-link"' in APP_JS
    assert 'target="_blank" rel="noopener noreferrer"' in APP_JS


def test_card_metadata_css_defines_duration_badge_and_stats_line() -> None:
    assert ".duration-badge" in APP_CSS
    assert ".video-stats" in APP_CSS
    assert "background: var(--overlay);" in APP_CSS
    assert "color: var(--muted);" in APP_CSS


def test_desktop_recommendation_covers_bound_eager_loading_to_first_four() -> None:
    assert "const DESKTOP_EAGER_COVER_COUNT = 4;" in APP_JS
    assert "index < DESKTOP_EAGER_COVER_COUNT" in APP_JS
    assert 'loading="${eager ? "eager" : "lazy"}"' in APP_JS
    assert 'fetchpriority="${eager ? "high" : "low"}"' in APP_JS


def test_delight_card_renders_the_same_engagement_stats_as_the_grid() -> None:
    """Field report 2026-07-07: the surprise (delight) card never showed the
    ▶/👍/💬 metadata the grid cards do. normalizeDelight now carries the counts
    and setActiveDelight fills #delightStats via the shared recommendationStats.
    """
    # normalizeDelight carries the stat fields (same parse as the grid).
    normalize = _function_body("normalizeDelight")
    for field in (
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "danmaku_count",
        "favorite_count",
    ):
        assert f"{field}: Number(item?.{field} ?? 0) || 0" in normalize, field

    # The delight card fills #delightStats from recommendationStats and hides it
    # when there are no counts (reusing the grid's .video-stats treatment).
    assert "const delightStats = recommendationStats(state.delight);" in APP_JS
    assert "delightStatsEl.textContent = delightStats;" in APP_JS
    assert "delightStatsEl.hidden = !delightStats;" in APP_JS

    # comment_count (💬) is in the shared stats renderer so both cards show it.
    stats = _function_body("recommendationStats")
    assert "if (item.comment_count > 0)" in stats
    assert "💬 " in stats

    # The DOM has the stats element on the delight card.
    index_html = Path("src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    assert '<p id="delightStats" class="video-stats" hidden></p>' in index_html
