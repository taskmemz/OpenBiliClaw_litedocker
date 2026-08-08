"""Tests for prompt-layer rendering cache helpers."""

from __future__ import annotations

from openbiliclaw.llm.prompt_cache import PromptLayerRenderCache, profile_prompt_layers


def test_prompt_layer_cache_reuses_unchanged_layer_text() -> None:
    cache = PromptLayerRenderCache()

    first = cache.render_json_layer("profile_core", {"traits": ["stable"], "score": 1})
    second = cache.render_json_layer("profile_core", {"score": 1, "traits": ["stable"]})
    changed = cache.render_json_layer("profile_core", {"traits": ["changed"], "score": 1})

    assert second is first
    assert changed is not first
    assert cache.stats() == {
        "profile_core": {"digest": cache.layer_digest("profile_core"), "hits": 1, "misses": 2}
    }


def test_prompt_layer_cache_default_rendering_keeps_indented_json_bytes() -> None:
    cache = PromptLayerRenderCache()

    rendered = cache.render_json_layer(
        "profile_core",
        {"traits": ["稳定"], "score": 1},
    )

    assert rendered == (
        '<profile_core>\n\n{\n  "score": 1,\n  "traits": [\n    "稳定"\n  ]\n}\n\n</profile_core>'
    )
    assert "\\u7a33" not in rendered


def test_prompt_layer_cache_compact_rendering_is_opt_in_and_deterministic() -> None:
    cache = PromptLayerRenderCache()
    payload = {"z": {"snow": "雪"}, "a": [2, 1]}

    rendered = cache.render_json_layer("profile_interests", payload, compact=True)
    reordered = cache.render_json_layer(
        "profile_interests",
        {"a": [2, 1], "z": {"snow": "雪"}},
        compact=True,
    )

    assert rendered == (
        '<profile_interests>\n\n{"a":[2,1],"z":{"snow":"雪"}}\n\n</profile_interests>'
    )
    assert reordered is rendered
    assert cache.stats()["profile_interests"] == {
        "digest": cache.layer_digest("profile_interests"),
        "hits": 1,
        "misses": 1,
    }


def test_prompt_layer_cache_keeps_pretty_and_compact_cache_entries_distinct() -> None:
    cache = PromptLayerRenderCache()
    payload = {"nested": {"b": 2, "a": 1}}

    pretty = cache.render_json_layer("profile_core", payload)
    pretty_digest = cache.layer_digest("profile_core")
    compact = cache.render_json_layer("profile_core", payload, compact=True)
    compact_digest = cache.layer_digest("profile_core")
    compact_again = cache.render_json_layer("profile_core", payload, compact=True)
    pretty_again = cache.render_json_layer("profile_core", payload)

    assert pretty != compact
    assert pretty_digest != compact_digest
    assert compact_again is compact
    assert pretty_again == pretty
    assert pretty_again is not pretty
    assert cache.layer_digest("profile_core") == pretty_digest
    assert cache.stats()["profile_core"] == {
        "digest": pretty_digest,
        "hits": 1,
        "misses": 3,
    }


def test_prompt_layer_cache_compact_multi_layer_rendering_preserves_order() -> None:
    cache = PromptLayerRenderCache()

    rendered = cache.render_json_layers(
        [
            ("profile_core", {"b": 2, "a": 1}),
            ("profile_recent_context", {"items": ["新"]}),
        ],
        compact=True,
    )

    assert rendered == [
        '<profile_core>\n\n{"a":1,"b":2}\n\n</profile_core>',
        '<profile_recent_context>\n\n{"items":["新"]}\n\n</profile_recent_context>',
    ]


def test_profile_prompt_layers_orders_stable_profile_before_recent_context() -> None:
    layers = profile_prompt_layers(
        {
            "active_insights": ["volatile"],
            "core_traits": ["stable"],
            "interests": [{"name": "stable-interest"}],
            "style": {"depth_preference": 0.8},
            "current_phase": "semi-stable",
        }
    )

    assert [name for name, _payload in layers] == [
        "profile_core",
        "profile_life_context",
        "profile_interests",
        "profile_style_context",
        "profile_recent_context",
    ]
    assert layers[0][1] == {"core_traits": ["stable"]}
    assert layers[-1][1] == {"active_insights": ["volatile"]}
