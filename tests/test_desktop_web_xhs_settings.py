"""Static regressions for desktop Xiaohongshu source safety defaults."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_web_uses_xhs_safety_defaults() -> None:
    html = (ROOT / "src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'daily_search_budget: getIntInput("xhsDailySearchBudget", 20)' in js
    assert 'task_interval_seconds: getIntInput("xhsTaskInterval", 1200)' in js
    assert 'min_interval_minutes: getIntInput("xhsMinInterval", 20)' in js
    assert 'id="xhsDailySearchBudget"' in html
    assert 'placeholder="默认 20"' in html
    assert 'id="xhsTaskInterval" inputmode="numeric" placeholder="1200"' in html
    assert 'id="xhsMinInterval" inputmode="numeric" placeholder="20"' in html
    assert "目标任务间隔秒数（±25%）" in html
