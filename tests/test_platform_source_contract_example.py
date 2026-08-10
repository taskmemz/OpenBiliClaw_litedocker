"""Negative-capability locks backing the Bangumi source-contract example."""

from __future__ import annotations

from pathlib import Path

import openbiliclaw.runtime.source_incremental_sync as incremental_sync
import openbiliclaw.sources.source_bootstrap as source_bootstrap
from openbiliclaw.saved_sync.adapters.extension import _EXTENSION_ADAPTER_DEFINITIONS

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"


def test_bangumi_refresh_is_explicit_not_incremental() -> None:
    assert "bangumi" not in incremental_sync.SOURCE_ORDER
    assert "bangumi" not in incremental_sync._TASK_SPECS
    assert "bangumi" not in incremental_sync._SOURCE_CONFIG_ALIASES
    assert "bangumi" not in incremental_sync._SOURCE_INTERVAL_FIELDS
    assert all(
        source != "bangumi" for source, _table, _task in source_bootstrap._BOOTSTRAP_TASK_TABLES
    )


def test_bangumi_identity_extension_has_no_task_marker_or_task_protocol() -> None:
    assert not (EXTENSION / "src/background/bangumi-task-dispatcher.ts").exists()
    assert not (EXTENSION / "src/content/bangumi/task-executor.ts").exists()
    assert not (EXTENSION / "src/content/bangumi/task-mode.ts").exists()

    api = (ROOT / "src/openbiliclaw/api/app.py").read_text(encoding="utf-8")
    service_worker = (EXTENSION / "src/background/service-worker.ts").read_text(encoding="utf-8")
    assert "/api/sources/bangumi/next-task" not in api
    assert "/api/sources/bangumi/task-result" not in api
    assert 'apiUrl("/sources/bangumi/identity")' in service_worker


def test_bangumi_identity_does_not_use_early_response_capture() -> None:
    identity_sources = (
        EXTENSION / "src/content/bangumi.ts",
        EXTENSION / "src/main/bgm-identity-bridge.ts",
    )
    combined = "\n".join(path.read_text(encoding="utf-8") for path in identity_sources)
    assert "CHOBITS_UID" in combined
    assert "XMLHttpRequest" not in combined
    assert "fetch(" not in combined


def test_bangumi_identity_does_not_use_cookie_sync() -> None:
    cookie_sync = (EXTENSION / "src/background/cookie-sync.ts").read_text(encoding="utf-8")
    identity = (EXTENSION / "src/content/bangumi.ts").read_text(encoding="utf-8")
    assert "bangumi" not in cookie_sync.lower()
    assert "bgm.tv" not in cookie_sync.lower()
    assert "No cookies, no tokens" in identity


def test_bangumi_deep_link_is_web_fallback_without_native_scheme() -> None:
    app_launch = (ROOT / "src/openbiliclaw/web/js/app-launch.js").read_text(encoding="utf-8")
    assert "buildAppDeepLink" in app_launch
    assert "bangumi.tv" not in app_launch.lower()
    assert "bgm.tv" not in app_launch.lower()


def test_bangumi_has_no_native_save_adapter() -> None:
    platforms = {definition.platform for definition in _EXTENSION_ADAPTER_DEFINITIONS}
    executor_names = {path.stem for path in (EXTENSION / "src/content/native-save").glob("*.ts")}
    assert "bangumi" not in platforms
    assert "bangumi" not in executor_names
    assert "bgm" not in executor_names
