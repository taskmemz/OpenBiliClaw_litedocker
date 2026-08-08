"""Static regressions for the settings-page backend update-status line."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_web_settings_wires_update_status_line() -> None:
    html = (ROOT / "src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'id="updateStatusLine"' in html

    assert 'updateStatus: "/update-status"' in js
    assert "function describeUpdateStatus" in js
    assert "function renderUpdateStatus" in js
    assert "function refreshUpdateStatus" in js

    # Frozen desktop bundles cannot self-apply — toggle disabled with a hint.
    assert "install_mode" in js
    assert "toggle.disabled = unsupportedInstall" in js
    assert "桌面安装包不支持自动应用更新" in js

    # The blocking reasons users actually hit are localized.
    assert "dirty_worktree" in js
    assert "branch_not_fast_forwardable" in js
    assert "github_rate_limited" in js
    assert "GitHub API 限流，请稍后再试" in js

    # Status refreshes when the settings page opens AND after a config save.
    assert js.count("void refreshUpdateStatus();") >= 2


def test_desktop_web_settings_understands_background_config_apply() -> None:
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'configApplyStatus: "/config/apply-status"' in js
    assert 'let settingsSavePhase = "idle";' in js
    assert "let settingsPendingApplyRevision = 0;" in js
    assert "正在保存配置…" in js
    assert "配置已保存，正在后台应用…" in js
    assert 'bar.toggleAttribute("aria-busy",' in js
    assert "function applyConfigApplyStatus" in js
    assert "function refreshConfigApplyStatus" in js
    assert "void refreshConfigApplyStatus();" in js
    assert 'result?.apply_state === "queued"' in js
    assert "配置已进入后台应用队列，连续保存会自动合并为最新版本" in js
    assert 'event.type === "config_reloaded"' in js
    assert 'event.type === "config_reload_failed"' in js
    assert "后台应用配置失败，已恢复上一次生效配置" in js


def test_desktop_web_settings_wires_manual_update_actions() -> None:
    """The spec-required 立即检查 / 立即应用 controls exist and are wired."""
    html = (ROOT / "src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'id="updateCheckBtn"' in html
    assert 'id="updateApplyBtn"' in html
    assert "立即检查" in html
    assert "立即应用" in html

    assert 'updateCheck: "/update/check"' in js
    assert 'updateApply: "/update/apply"' in js
    assert "function wireUpdateActions" in js

    # Live refresh of the status line on backend update stream events.
    assert "backend_update_available" in js
    assert "backend_restart_pending" in js


def test_desktop_web_settings_guides_frozen_installs_to_download_new_installer() -> None:
    """Frozen bundles get check-only reminders that link to the installer release."""
    html = (ROOT / "src/openbiliclaw/web/desktop/index.html").read_text(encoding="utf-8")
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'id="updateDownloadLink"' in html
    assert "前往下载新安装包" in html

    assert "function describeFrozenUpdateStatus" in js
    assert "发现新版安装包" in js
    # The download link deep-links to the discovered desktop-v* release tag.
    assert "releases/tag/" in js
    # The toast reminder distinguishes installer releases from source releases.
    assert 'startsWith("desktop-v")' in js


def test_desktop_web_settings_guides_docker_installs_to_pull_new_image() -> None:
    """Docker containers get check-only reminders that explain the pull upgrade."""
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert "function describeDockerUpdateStatus" in js
    assert 'const isDockerInstall = mode === "docker"' in js
    assert "发现新版镜像" in js
    assert "docker compose pull && docker compose up -d" in js
    # Blocked-apply reason on docker installs is localized, not raw.
    assert "docker_install_mode" in js
    # 立即检查 stays usable on docker (check-only), like frozen.
    assert "!isFrozenInstall && !isDockerInstall && !isDesktopInstallerUpdate" in js


def test_desktop_web_update_actions_require_explicit_install_branch() -> None:
    """Only explicit git installs self-apply; desktop-v* always links to installers."""
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert 'const isGitInstall = mode === "git"' in js
    assert 'const isFrozenInstall = mode === "frozen"' in js
    assert (
        'const isDesktopInstallerUpdate = String(backend.latest_tag || "").startsWith("desktop-v")'
        in js
    )
    assert 'isGitInstall && backend.state === "update_available"' in js
    assert (
        '(isFrozenInstall || isDesktopInstallerUpdate) && backend.state === "update_available"'
        in js
    )
    assert '!unsupportedInstall && backend.state === "update_available"' not in js


def test_desktop_web_update_error_prefers_backend_last_error_detail() -> None:
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert "const errorText = backend.last_error" in js
    assert "UPDATE_REASON_TEXT[backend.last_error] || backend.last_error" in js
    assert '更新检查出错：${errorText || reasonText || "未知错误"}' in js


def test_desktop_web_settings_persists_delight_queue_limit_to_backend_config() -> None:
    """The shared delight queue size must be saved through /api/config."""
    js = (ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js").read_text(encoding="utf-8")

    assert "delight_queue_limit: getDelightQueueLimit()" in js
    assert "config.scheduler?.delight_queue_limit" in js
