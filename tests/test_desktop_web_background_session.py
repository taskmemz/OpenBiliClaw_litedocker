import re
from pathlib import Path

APP_JS = Path("src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")


def _function_body(name: str, *, async_function: bool = False) -> str:
    prefix = "async function" if async_function else "function"
    match = re.search(
        rf"{prefix} {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)\n    \}}",
        APP_JS,
        flags=re.S,
    )
    assert match is not None, f"desktop {name} not found"
    return match.group("body")


def test_hidden_desktop_tab_does_not_hydrate_or_open_runtime_stream() -> None:
    start = _function_body("startDesktopBackendSession", async_function=True)
    stream = _function_body("connectRuntimeStream")

    assert "if (document.hidden || desktopBackendSessionInFlight) return;" in start
    # Resume hydration never replaces the recommendation list; only the boot
    # path (forceHydrate) is allowed to seed/replace it.
    assert "await hydrateFromBackend({ replaceRecommendations: forceHydrate });" in start
    assert "if (!document.hidden) connectRuntimeStream();" in start
    assert "if (document.hidden) return;" in stream
    assert "WebSocket.CONNECTING" in stream
    assert "WebSocket.OPEN" in stream
    # Boot uses the visibility-aware session boundary instead of directly
    # hydrating and connecting every restored browser tab.
    assert "void startDesktopBackendSession({ forceHydrate: true });" in APP_JS
    assert ".then(() => hydrateFromBackend())" not in APP_JS


def test_backgrounding_closes_stream_and_cancels_retry_loops() -> None:
    pause = _function_body("pauseDesktopBackendSession")

    for timer in (
        "backendHydrationTimer",
        "desktopRecommendationRecoveryTimer",
        "desktopRuntimeRecoveryTimer",
        "platformAvailabilityRetryTimer",
        "configSnapshotRetryTimer",
        "activityPageRefreshTimer",
        "desktopRuntimeReconnectTimer",
    ):
        assert timer in pause
    assert "clearInitPolling();" in pause
    assert "state.runtimeSocket = null;" in pause
    assert "if (socket) socket.close();" in pause
    assert "if (document.hidden) {\n        pauseDesktopBackendSession();" in APP_JS
    assert "void startDesktopBackendSession();" in APP_JS


def test_runtime_stream_heartbeat_and_close_use_reconnecting_state() -> None:
    stream = _function_body("connectRuntimeStream")

    assert 'payload?.type === "runtime.heartbeat"' in stream
    assert '"实时连接正常"' in stream
    assert '"实时流重连中"' in stream
    assert "event.code" in stream
    assert "event.reason" in stream
    assert "scheduleDesktopRuntimeReconnect();" in stream
    assert '"实时流断开"' not in stream


def test_retry_schedulers_are_visibility_gated() -> None:
    for name in (
        "schedulePlatformAvailabilityRetry",
        "scheduleConfigSnapshotRetry",
        "scheduleDesktopRecommendationRecovery",
        "scheduleDesktopRuntimeRecovery",
        "scheduleInitStatusRefresh",
    ):
        assert "document.hidden" in _function_body(name), name

    assert "document.hidden" in _function_body("scheduleBackendHydration")
    assert "document.hidden" in _function_body("scheduleActivityPageRefresh")
