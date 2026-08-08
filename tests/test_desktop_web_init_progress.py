"""Static contract for init-progress visibility on desktop web + setup wizard.

The three GUI surfaces share no module system, so desktop web and the setup
wizard mirror the popup's reference implementation
(extension/popup/popup-init-control.js — init-progress-visibility Phase 2).
These string-level assertions keep the mirrored formulas / copy from drifting.
"""

from pathlib import Path

APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js")
APP_CSS = Path("src/openbiliclaw/web/desktop/assets/css/app.css")


def _app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_desktop_init_progress_mirrors_popup_fraction_formula() -> None:
    app_js = _app_js()
    # Real sub-progress fraction (done/total) capped below stage completion.
    assert "STAGE_FRACTION_CAP" in app_js
    assert "0.95" in app_js
    # No forecast anywhere: the elapsed/eta pseudo-progress and the flat
    # half-step it fell back to are both gone, so the bar can only be moved by
    # real done/total (2026-07-20 — a duration we cannot honour is worse than
    # none). Stages without counts render indeterminate instead.
    assert "Math.exp" not in app_js
    assert "eta_seconds" not in app_js
    assert "STAGE_FRACTION_FALLBACK" not in app_js
    assert "STAGE_FRACTION_UNKNOWN = 0" in app_js
    # Sub-progress note joins the running stage label.
    assert "progress?.note" in app_js


def test_desktop_init_progress_pct_is_monotonic_per_run() -> None:
    app_js = _app_js()
    # Per-run view state with a monotonic clamp on the rendered pct.
    assert "maxPct" in app_js
    assert "Math.max(st.maxPct, pct)" in app_js
    assert "_runViewState" in app_js


def test_desktop_surfaces_stall_copy_after_90s_of_silence() -> None:
    app_js = _app_js()
    assert "INIT_STALL_THRESHOLD_SECONDS = 90" in app_js
    assert "stalenessView" in app_js
    assert "last_activity" in app_js
    assert "last_heartbeat_at" in app_js
    assert "last_progress_at" in app_js
    # Work-unit stall copy + the adaptive threshold that governs it (the 90s
    # beat window is for the CONNECTION check only).
    assert "比本轮此前的节奏慢" in app_js
    assert "INIT_PROGRESS_STALL_FLOOR_SECONDS = 300" in app_js
    assert "slowestProgressIntervalSeconds" in app_js
    assert "没有心跳" in app_js
    assert "● 后端在线" in app_js
    # Amber styling hook for the stalled state.
    assert "init-stall-hint" in app_js
    assert ".init-stall-hint" in APP_CSS.read_text(encoding="utf-8")


def test_desktop_shows_expectation_copy_and_observed_stage_facts() -> None:
    app_js = _app_js()
    # Idle expectation management near the start button.
    assert "严格按顺序生成" in app_js
    assert "取决于你勾了几个平台" in app_js
    assert "进度会保留" in app_js
    # Running stage row reports observed facts, never a prediction.
    assert "stageEtaText" not in app_js
    assert "stageDetailText" in app_js
    assert "已用时不到 1 分钟" in app_js
    assert "已完成 ${done}/${total}" in app_js
    # The one reassurance a waiting user needs, said once.
    assert "只要还在出结果就不会被打断" in app_js


def test_desktop_trusts_terminal_init_contract_and_keeps_embedding_override() -> None:
    app_js = _app_js()
    # Backend completion now means either a serviceable first pool or explicit
    # partial success. The client must not invent a second indefinite 95% wait.
    assert "pct: 95" not in app_js
    assert "initWaitingForFirstPool" not in app_js
    # Embedding pull borrows the progress bar while idle.
    assert "embeddingPull.pct" in app_js


# ── Setup wizard mirror (single-file inline JS, no test infra of its own) ────

SETUP_HTML = Path("src/openbiliclaw/web/setup/index.html")


def _setup_html() -> str:
    return SETUP_HTML.read_text(encoding="utf-8")


def test_setup_wizard_mirrors_progress_fraction_and_clamp() -> None:
    html = _setup_html()
    assert "STAGE_FRACTION_CAP" in html
    assert "Math.exp" not in html
    assert "STAGE_FRACTION_FALLBACK" not in html
    assert "eta_seconds" not in html
    assert "STAGE_FRACTION_UNKNOWN = 0" in html
    assert "progress?.note" in html
    assert "maxPct" in html
    assert "Math.max(st.maxPct, pct)" in html


def test_setup_wizard_surfaces_stall_and_expectation_copy() -> None:
    html = _setup_html()
    assert "INIT_STALL_THRESHOLD_SECONDS = 90" in html
    assert "stalenessView" in html
    assert "last_activity" in html
    assert "last_heartbeat_at" in html
    assert "last_progress_at" in html
    assert "比本轮此前的节奏慢" in html
    assert "INIT_PROGRESS_STALL_FLOOR_SECONDS = 300" in html
    assert "slowestProgressIntervalSeconds" in html
    assert "没有心跳" in html
    assert "● 后端在线" in html
    assert "严格按顺序生成" in html
    assert "取决于你勾了几个平台" in html
    assert "stageEtaText" not in html
    assert "stageDetailText" in html
    assert "已用时不到 1 分钟" in html
    assert "已完成 ${done}/${total}" in html
    assert "只要还在出结果就不会被打断" in html
    assert "initStallHint" in html


def test_web_surfaces_indeterminate_progress_timeouts_and_cancel() -> None:
    app_js = _app_js()
    app_css = APP_CSS.read_text(encoding="utf-8")
    html = _setup_html()
    for source in (app_js, html):
        assert 'mode === "indeterminate"' in source
        assert "取消初始化" in source
        assert "暂时无法连接初始化后台" in source
        assert 'indeterminate ? "100%"' in source
    assert 'cancelInit: "/init/cancel"' in app_js
    assert 'fetchWithTimeout("/api/init/cancel"' in html
    assert ".init-progress-fill.indeterminate" in app_css
    assert ".progress-fill.indeterminate" in html


def test_setup_wizard_uses_terminal_contract_and_keeps_embedding_override() -> None:
    html = _setup_html()
    assert '"95%"' not in html
    assert "renderWaitingForFirstPool" not in html
    assert "showInitCompletion" in html
    # Embedding pull borrows the progress bar while idle.
    assert "pull.active && !status?.running" in html


def test_timeout_reason_is_actionable_and_announced_across_web_surfaces() -> None:
    app_js = _app_js()
    setup_html = _setup_html()

    for source in (app_js, setup_html):
        assert "initStatusReasonText" in source
        assert 'detail.startsWith("画像分析失败：")' in source
        assert '"discovery_timeout"' in source
        assert 'aria-live="polite"' in source
        assert '"assertive"' in source

    # Partial success retains the backend explanation while letting the user
    # enter the application instead of waiting behind a synthetic 95% state.
    assert "status?.partial_success ? initStatusReasonText(status)" in app_js
    assert "showInitCompletion(status)" in setup_html
    assert "renderWaitingForFirstPool" not in setup_html


def test_desktop_reattaches_init_poll_when_a_run_is_live_at_load() -> None:
    """A page opened/refreshed mid-init must start polling from hydrate.

    Hydrate fetches init-status once; without a boot re-attach the progress
    bar freezes on that single frame whenever SSE is unavailable, and — since
    the touch() heartbeat publishes no SSE event — a hung backend would never
    drive the stall detector either. The poll is the only observer of
    last_activity in that case.
    """
    app_js = _app_js()
    assert "function applyInitStatusSnapshot(snapshot)" in app_js
    apply_snapshot = app_js.split("function applyInitStatusSnapshot(snapshot)", 1)[1]
    apply_snapshot = apply_snapshot.split("\n      }", 1)[0]

    # The init-status resource owner must attach the poll for every live state.
    assert "state.initStatus = snapshot;" in apply_snapshot
    assert "snapshot.running" in apply_snapshot
    assert "embeddingPullNeedsPolling(snapshot)" in apply_snapshot
    assert "initWaitingForFirstPool" not in apply_snapshot
    assert "scheduleInitStatusRefresh(INIT_STATUS_POLL_MS)" in apply_snapshot


def test_desktop_keeps_watching_a_startup_embedding_pull_before_init() -> None:
    """A slow first-run bge-m3 pull is live work even when init is still idle."""
    app_js = _app_js()
    assert "function embeddingPullNeedsPolling(status)" in app_js
    assert "embeddingPullNeedsPolling(status)" in app_js
    apply_snapshot = app_js.split("function applyInitStatusSnapshot(snapshot)", 1)[1]
    apply_snapshot = apply_snapshot.split("\n      }", 1)[0]
    assert "embeddingPullNeedsPolling(snapshot)" in apply_snapshot


def test_desktop_terminal_init_status_wins_over_stale_runtime_snapshot() -> None:
    app_js = _app_js()
    decision = app_js.split("function shouldShowInitOnboarding(status)", 1)[1]
    decision = decision.split("\n    }", 1)[0]
    assert "state.initStatus?.initialized === true" in decision
    assert "return false" in decision
