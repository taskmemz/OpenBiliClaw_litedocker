"""Recommendation-output exclusion policy regressions."""

from openbiliclaw.recommendation.exclusion import (
    disliked_topics_digest,
    filter_recommendation_rows,
)


def test_filters_exact_structured_topic_and_keeps_unrelated_rows() -> None:
    rows = [
        {"id": 1, "title": "办公室拉伸", "topic_group": "运动康复"},
        {"id": 2, "title": "SQLite 查询优化", "topic_group": "数据库"},
    ]

    result = filter_recommendation_rows(rows, ["运动康复"])

    assert [row["id"] for row in result] == [2]


def test_filters_fuzzy_match_when_safe_alternative_exists() -> None:
    rows = [
        {"id": 1, "title": "运动康复的居家训练指南"},
        {"id": 2, "title": "从零搭建个人知识库"},
    ]

    result = filter_recommendation_rows(rows, ["运动康复"])

    assert [row["id"] for row in result] == [2]


def test_total_fuzzy_wipeout_restores_exact_safe_rows_to_avoid_false_positive_starvation() -> None:
    rows = [
        {"id": 1, "title": "视频里的数据库原理", "topic_group": "数据库"},
        {"id": 2, "title": "视频里的编译器设计", "topic_group": "编译器"},
    ]

    result = filter_recommendation_rows(rows, ["视频"])

    assert [row["id"] for row in result] == [1, 2]


def test_single_item_push_does_not_restore_a_fuzzy_dislike_match() -> None:
    rows = [{"id": 1, "title": "运动康复的居家训练指南"}]

    result = filter_recommendation_rows(
        rows,
        ["运动康复"],
        restore_on_total_fuzzy_match=False,
    )

    assert result == []


def test_dislike_digest_is_order_independent_and_changes_with_policy() -> None:
    first = disliked_topics_digest(["运动康复", "营销软文"])
    reordered = disliked_topics_digest([" 营销软文 ", "运动康复", "运动康复"])
    changed = disliked_topics_digest(["运动康复"])

    assert first == reordered
    assert first != changed
