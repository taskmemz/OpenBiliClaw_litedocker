"""Regression: all keyword-generation callers feed the query-trimmed profile.

The main search / explore / keyword_planner paths have long used
``build_query_generation_profile_summary`` (MMR-reduced taste shape). These
tests pin the five previously-divergent producers (bilibili extension search,
YouTube, X, Douyin, Xiaohongshu) to the same representation so they can never
silently regress back to the full ``build_profile_summary`` block.

The query-trimmed signature on a maxed profile: interests capped at 64,
interest_domains <= 16, and no ``recent_awareness`` / ``active_insights`` /
per-entry ``first_seen`` churn fields (which the full block carries).
"""

from __future__ import annotations

import json
import re

import pytest

from openbiliclaw.soul.profile import (
    AwarenessNote,
    InsightHypothesis,
    InterestTag,
    PreferenceLayer,
    SoulProfile,
)

_QUERY_INTEREST_CAP = 64
_QUERY_DOMAIN_CAP = 16


def _maxed_profile() -> SoulProfile:
    return SoulProfile(
        core_traits=[f"trait-{i}" for i in range(40)],
        deep_needs=[f"need-{i}" for i in range(40)],
        preferences=PreferenceLayer(
            interests=[
                InterestTag(
                    name=f"兴趣-{i}",
                    category=f"类别-{i % 12}",
                    weight=max(0.0, 1.0 - i * 0.01),
                    first_seen="2026-01-01",
                    last_seen="2026-06-27",
                    source="behavior",
                )
                for i in range(120)
            ],
            disliked_topics=[f"不喜欢-{i}" for i in range(80)],
        ),
        recent_awareness=[
            AwarenessNote(
                date=f"2026-06-{i + 1:02d}",
                observation="近期观察" * 10,
                trend="趋势" * 5,
                emotion_guess="情绪" * 5,
            )
            for i in range(20)
        ],
        active_insights=[
            InsightHypothesis(
                hypothesis="假设" * 10,
                evidence=["证据" * 10 for _ in range(10)],
                confidence=0.8,
                validated=True,
            )
            for _ in range(20)
        ],
    )


def _extract_profile_json(text: str) -> dict[str, object]:
    """Pull the profile dict out of a keyword prompt's user message."""
    tagged = re.search(r"<profile_summary>\s*(\{.*?\})\s*</profile_summary>", text, re.S)
    if tagged:
        return json.loads(tagged.group(1))
    payload = json.loads(text)
    profile = payload.get("profile")
    assert isinstance(profile, dict)
    return profile


def _assert_query_trimmed(profile_json: dict[str, object]) -> None:
    interests = profile_json.get("interests")
    assert isinstance(interests, list)
    assert len(interests) == _QUERY_INTEREST_CAP  # full block would keep >64
    domains = profile_json.get("interest_domains", [])
    assert isinstance(domains, list)
    assert len(domains) <= _QUERY_DOMAIN_CAP
    # Churn fields the full block carries but query-gen deliberately drops.
    assert "recent_awareness" not in profile_json
    assert "active_insights" not in profile_json
    assert "first_seen" not in json.dumps(profile_json, ensure_ascii=False)


class _CapturingLLM:
    """Fake LLM service that records the user_input then short-circuits."""

    def __init__(self) -> None:
        self.user_input = ""

    async def complete_structured_task(self, *, user_input: str, **_: object) -> object:
        self.user_input = user_input
        raise RuntimeError("stop-after-capture")


def test_x_keyword_prompt_uses_query_trimmed_profile() -> None:
    from openbiliclaw.discovery.strategies.x import _build_keyword_user_prompt

    _assert_query_trimmed(_extract_profile_json(_build_keyword_user_prompt(_maxed_profile(), 5)))


def test_douyin_keyword_prompt_uses_query_trimmed_profile() -> None:
    from openbiliclaw.discovery.strategies.douyin_direct import _build_douyin_keyword_user_prompt

    _assert_query_trimmed(
        _extract_profile_json(_build_douyin_keyword_user_prompt(_maxed_profile(), 5))
    )


def test_xhs_keyword_prompt_uses_query_trimmed_profile() -> None:
    from openbiliclaw.sources.xhs_keyword_gen import _build_user_prompt

    _assert_query_trimmed(_extract_profile_json(_build_user_prompt(_maxed_profile(), 5)))


@pytest.mark.asyncio
async def test_bilibili_extension_keywords_use_query_trimmed_profile() -> None:
    from openbiliclaw.runtime.bilibili_producer import generate_bili_search_keywords

    llm = _CapturingLLM()
    await generate_bili_search_keywords(llm, _maxed_profile(), count=5)  # type: ignore[arg-type]
    _assert_query_trimmed(_extract_profile_json(llm.user_input))


@pytest.mark.asyncio
async def test_youtube_queries_use_query_trimmed_profile() -> None:
    from openbiliclaw.discovery.strategies.youtube import YoutubeSearchStrategy

    strategy = YoutubeSearchStrategy.__new__(YoutubeSearchStrategy)
    strategy.llm_service = _CapturingLLM()  # type: ignore[attr-defined]
    strategy.queries_per_run = 5  # type: ignore[attr-defined]
    await strategy._generate_queries(_maxed_profile())
    _assert_query_trimmed(_extract_profile_json(strategy.llm_service.user_input))  # type: ignore[union-attr]
