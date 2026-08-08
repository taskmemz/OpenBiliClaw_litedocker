"""Task 8 audit: maintenance callers that must opt out of core-memory injection.

Each covered caller receives the material it judges in its own user prompt, so
the default ``inject_core_memory=True`` only wastes tokens (and, for the dialogue
insight analyzer, duplicates a block already serialized into the user prompt).
These tests pin ``inject_core_memory=False`` on every such call.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from openbiliclaw.soul.dialogue_insight_analyzer import DialogueInsightAnalyzer
from openbiliclaw.soul.pool_purge import _llm_judge


class _RecordingLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.call_kwargs: list[dict[str, Any]] = []

    async def complete_structured_task(self, **kwargs: Any) -> Any:
        self.call_kwargs.append(dict(kwargs))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def test_pool_purge_judge_opts_out_of_core_memory_injection() -> None:
    llm = _RecordingLLM({"purge": ["BV1"], "reason": {"BV1": "命中新厌恶"}})
    recalled = [
        ({"bvid": "BV1", "title": "标题党合集", "topic_group": "标题党"}, 0.91, "标题党"),
    ]

    result = asyncio.run(
        _llm_judge(
            recalled=recalled,
            new_topics=["标题党"],
            all_topics=["标题党"],
            llm_service=llm,
        )
    )

    assert result == ["BV1"]
    assert llm.call_kwargs
    for call in llm.call_kwargs:
        assert call.get("inject_core_memory") is False


def test_dialogue_insight_opts_out_of_duplicate_core_memory_injection() -> None:
    llm = _RecordingLLM({"candidates": []})
    analyzer = DialogueInsightAnalyzer(llm)
    core_memory = {"soul_summary": {"personality_portrait": "PORTRAIT_SENTINEL_XYZ"}}

    asyncio.run(
        analyzer.extract(
            user_message="我最近想系统地学国际局势",
            assistant_reply="好呀,那我们从地缘格局聊起",
            core_memory=core_memory,
        )
    )

    assert llm.call_kwargs
    call = llm.call_kwargs[0]
    assert call.get("inject_core_memory") is False
    # The curated core memory is still delivered — explicitly, in the user prompt.
    assert "PORTRAIT_SENTINEL_XYZ" in str(call.get("user_input", ""))
