from __future__ import annotations

import pytest

from openbiliclaw.sources.v2ex import v2ex_topic_to_content


def _topic(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 123456,
        "url": "https://www.v2ex.com/t/123456",
        "title": "如何管理 Agent 上下文",
        "content": "<p>主楼正文</p><br>下一段",
        "created": 1_754_694_400,
        "member": {"username": "alice"},
        "node": {"name": "programmer", "title": "程序员"},
        "replies": 36,
    }
    row.update(overrides)
    return row


def test_topic_normalization_uses_shared_topic_contract() -> None:
    item = v2ex_topic_to_content(_topic(), strategy="v2ex-node", source_keyword_id=9)

    assert item is not None
    assert item.item_key == "v2ex:123456"
    assert item.content_id == "123456"
    assert item.content_type == "topic"
    assert item.source_platform == "v2ex"
    assert item.source_strategy == "v2ex-node"
    assert item.content_url == "https://www.v2ex.com/t/123456"
    assert item.author_name == "alice"
    assert item.body_text == "主楼正文\n下一段"
    assert item.tags == ["programmer", "程序员"]
    assert item.reply_count == 36
    assert item.source_keyword_id == 9
    assert item.view_count == item.like_count == item.favorite_count == item.comment_count == 0
    assert item.share_count == item.danmaku_count == 0


def test_topic_normalization_uses_canonical_url_and_feed_fields() -> None:
    item = v2ex_topic_to_content(
        {
            "id": "https://www.v2ex.com/t/88",
            "title": "Feed 主题",
            "content_html": "<p>Feed 正文</p>",
            "date_published": "2026-08-09T00:00:00Z",
            "author_name": "bob",
            "node_name": "python",
            "reply_count": "5",
        },
        strategy="v2ex-tab",
    )

    assert item is not None
    assert item.item_key == "v2ex:88"
    assert item.content_url == "https://www.v2ex.com/t/88"
    assert item.author_name == "bob"
    assert item.body_text == "Feed 正文"
    assert item.reply_count == 5


def test_topic_normalization_never_preserves_an_external_or_insecure_url() -> None:
    external = v2ex_topic_to_content(
        _topic(url="https://evil.example/t/123456?token=secret"),
        strategy="v2ex-hot",
    )
    insecure = v2ex_topic_to_content(
        _topic(url="http://v2ex.com/t/123456?once=secret"),
        strategy="v2ex-hot",
    )

    assert external is not None
    assert insecure is not None
    assert external.content_url == "https://www.v2ex.com/t/123456"
    assert insecure.content_url == "https://www.v2ex.com/t/123456"
    assert (
        v2ex_topic_to_content(
            {"url": "https://evil.example/t/999", "title": "external-only"},
            strategy="v2ex-hot",
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    [
        {"id": 123, "title": ""},
        {"id": 0, "title": "无 ID"},
        {"id": 123, "title": "已删除", "deleted": True},
        "not-a-row",
    ],
)
def test_topic_normalization_drops_malformed_or_deleted_rows(row: object) -> None:
    assert v2ex_topic_to_content(row, strategy="v2ex-hot") is None
