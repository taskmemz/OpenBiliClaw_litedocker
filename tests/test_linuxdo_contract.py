"""Executable N/A boundaries frozen by the Linux.do source contract."""

from __future__ import annotations

import inspect

from openbiliclaw.sources.linuxdo_tasks import linuxdo_discovery_items_to_contents


def _content() -> object:
    return linuxdo_discovery_items_to_contents(
        [
            {
                "scope": "linuxdo_feed",
                "content_type": "post",
                "topic_id": 42,
                "title": "Contract fixture",
            }
        ]
    )[0]


def test_linuxdo_early_response_is_not_required_when_fetch_starts_after_listener() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    executor = (root / "extension/src/content/linuxdo/task-executor.ts").read_text()
    build = (root / "extension/scripts/build.mjs").read_text()
    assert "LINUXDO_TASK_EXECUTE" in executor
    assert "fetchLinuxdoJson" in executor
    assert "linuxdo-fetch-tap" not in build
    assert 'world: "MAIN"' not in executor


def test_linuxdo_unavailable_aggregate_engagement_stays_zero() -> None:
    content = _content()
    assert content.favorite_count == 0
    assert content.share_count == 0
    assert content.danmaku_count == 0


def test_linuxdo_media_image_is_an_explicit_text_card_exclusion() -> None:
    content = _content()
    assert content.cover_url == ""


def test_linuxdo_media_deep_link_uses_canonical_https_browser_fallback() -> None:
    content = _content()
    assert content.content_url == "https://linux.do/t/42"
    assert "://" in content.content_url
    assert content.content_url.startswith("https://")


def test_linuxdo_has_no_native_save_adapter() -> None:
    from openbiliclaw.saved_sync.adapters import extension

    source = inspect.getsource(extension)
    assert '"linuxdo"' not in source
    assert "linuxdo" not in getattr(extension, "SUPPORTED_PLATFORMS", ())
