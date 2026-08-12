"""Executable exclusion gates for V2EX capabilities we intentionally do not claim."""

from __future__ import annotations

import json
from pathlib import Path

from openbiliclaw.saved_sync.adapters.extension import _EXTENSION_ADAPTER_DEFINITIONS
from openbiliclaw.sources.v2ex import v2ex_topic_to_content

ROOT = Path(__file__).resolve().parents[1]


def _topic() -> dict[str, object]:
    return {
        "id": 123456,
        "title": "Text-first V2EX topic",
        "content": "A useful discussion without a cover image.",
        "member": {"username": "alice"},
        "node": {"name": "programmer", "title": "程序员"},
    }


def test_v2ex_browser_task_does_not_install_an_early_response_tap() -> None:
    """Rendered account pages need no early response interception or replay."""

    entry = (ROOT / "extension/src/content/v2ex.ts").read_text(encoding="utf-8")
    chrome_manifest = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))
    matching_scripts = [
        script
        for script in chrome_manifest["content_scripts"]
        if any("v2ex.com" in match for match in script.get("matches", []))
    ]

    assert "fetch-tap" not in entry
    assert "response-tap" not in entry
    assert matching_scripts == [
        {
            "matches": ["*://*.v2ex.com/*"],
            "js": ["dist/content/v2ex.js"],
            "run_at": "document_idle",
        }
    ]
    assert all(script.get("world", "ISOLATED") != "MAIN" for script in matching_scripts)


def test_v2ex_recommendations_are_intentional_text_cards() -> None:
    """No image URL is invented; all three renderers have the no-cover text path."""

    item = v2ex_topic_to_content(_topic(), strategy="v2ex-node")
    assert item is not None
    image = item.cover_url
    assert image == ""

    renderers = (
        ROOT / "src/openbiliclaw/web/js/view-models.js",
        ROOT / "src/openbiliclaw/web/desktop/assets/js/app.js",
        ROOT / "extension/popup/popup-helpers.js",
    )
    for renderer in renderers:
        source = renderer.read_text(encoding="utf-8")
        assert "is-text-card" in source or 'kind: "text"' in source
        assert "cover_url" in source


def test_v2ex_mobile_uses_https_without_a_native_scheme() -> None:
    """The mobile deep link remains the canonical browser HTTPS Topic URL."""

    item = v2ex_topic_to_content(_topic(), strategy="v2ex-hot")
    assert item is not None
    deep_link = item.content_url
    assert deep_link == "https://www.v2ex.com/t/123456"
    assert not deep_link.startswith("v2ex:")

    mobile = (ROOT / "src/openbiliclaw/web/js/view-models.js").read_text(encoding="utf-8")
    assert "https://www.v2ex.com/t/" in mobile
    assert "v2ex://" not in mobile


def test_v2ex_has_no_native_save_or_upstream_write_adapter() -> None:
    """Local save is allowed, but native save must not mutate V2EX upstream."""

    native_save_platforms = {definition.platform for definition in _EXTENSION_ADAPTER_DEFINITIONS}
    assert "v2ex" not in native_save_platforms
    assert not (ROOT / "extension/src/content/native-save/v2ex.ts").exists()

    client = (ROOT / "src/openbiliclaw/sources/v2ex_client.py").read_text(encoding="utf-8")
    assert "self._bounded_get(" in client
    assert "self._client.post(" not in client
    assert "self._client.put(" not in client
    assert "self._client.delete(" not in client
