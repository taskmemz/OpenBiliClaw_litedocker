from __future__ import annotations

import pytest

from openbiliclaw.sources.bangumi import (
    bangumi_collection_to_event,
    bangumi_subject_to_content,
    fetch_bangumi_public_collection_events,
)
from openbiliclaw.sources.bangumi_client import BangumiPage

# Verbatim ``infobox`` slices captured from the live v0 API on 2026-07-18 so the
# parser is pinned against the real payload shape (mixed bare-string and
# ``[{"v": …}]`` / ``[{"k": …, "v": …}]`` list values), not an invented one.
# Sources: GET /v0/subjects/237 (攻壳机动队 剧场版), /v0/subjects/62229
# (塞尔达传说 旷野之息), and the ranked book page (SLAM DUNK 完全版).
_ANIME_INFOBOX: list[dict[str, object]] = [
    {"key": "中文名", "value": "攻壳机动队"},
    {"key": "别名", "value": [{"v": "攻殻機動隊"}, {"v": "GHOST IN THE SHELL"}]},
    {"key": "导演", "value": "押井守"},
    {"key": "脚本", "value": "伊藤和典"},
    {"key": "原作", "value": "士郎正宗（「攻殻機動隊」講談社刊）"},
    {"key": "动画制作", "value": "Production I.G"},
    {"key": "製作", "value": "講談社、バンダイビジュアル、MANGA ENTERTAINMENT"},
]
_BOOK_INFOBOX: list[dict[str, object]] = [
    {"key": "别名", "value": [{"v": "篮球飞人 完全版"}]},
    {"key": "作者", "value": "井上雄彦"},
    {"key": "出版社", "value": "集英社"},
]
_GAME_INFOBOX: list[dict[str, object]] = [
    {"key": "平台", "value": [{"v": "Nintendo Switch"}, {"v": "Wii U"}]},
    {"key": "开发", "value": "任天堂企画制作本部"},
    {"key": "发行", "value": "任天堂"},
]


def _subject(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 326,
        "type": 2,
        "name": "Koukaku Kidoutai",
        "name_cn": "攻壳机动队",
        "summary": "未来社会的故事",
        "date": "2004-01-01",
        "nsfw": False,
        "images": {"common": "https://lain.bgm.tv/cover.jpg"},
        "meta_tags": ["TV"],
        "tags": [{"name": "科幻", "count": 99}, {"name": "tv", "count": 1}],
        "rating": {"score": 9.2, "total": 9959, "rank": 1},
        "collection": {"wish": 1, "collect": 2, "doing": 3, "on_hold": 4, "dropped": 5},
        "infobox": list(_ANIME_INFOBOX),
    }
    row.update(overrides)
    return row


def test_subject_normalization_maps_catalog_fields_without_fake_engagement() -> None:
    item = bangumi_subject_to_content(_subject(), strategy="bangumi-ranked", source_keyword_id=12)
    assert item is not None
    assert item.item_key == "bangumi:326"
    assert item.content_url == "https://bgm.tv/subject/326"
    assert item.content_type == "subject"
    assert item.title == "攻壳机动队"
    assert item.author_name == "押井守"
    assert item.body_text == "未来社会的故事"
    assert item.cover_url == "https://lain.bgm.tv/cover.jpg"
    assert item.favorite_count == 15
    assert (
        item.view_count
        == item.like_count
        == item.comment_count
        == item.share_count
        == item.danmaku_count
        == 0
    )
    assert item.rating_score == 9.2
    assert item.rating_count == 9959
    assert item.source_rank == 1
    assert item.tags == ["TV", "科幻"]
    assert item.source_keyword_id == 12


def test_slim_subject_fallbacks_and_numeric_guards() -> None:
    item = bangumi_subject_to_content(
        _subject(
            name_cn="",
            summary="",
            short_summary="short",
            images={"medium": "https://lain.bgm.tv/m.jpg"},
            rating=None,
            score="99",
            rank="-3",
            collection=None,
            collection_total="8",
        ),
        strategy="bangumi-latest",
    )
    assert item is not None
    assert item.title == "Koukaku Kidoutai"
    assert item.body_text == "short"
    assert item.cover_url.endswith("/m.jpg")
    assert item.favorite_count == 8
    assert item.rating_score == 10.0
    assert item.source_rank == 0


@pytest.mark.parametrize(
    "row",
    [
        _subject(nsfw=True),
        _subject(id=0),
        _subject(type=5),
        _subject(name="", name_cn=""),
    ],
)
def test_subject_normalization_drops_unsafe_or_malformed_rows(row: dict[str, object]) -> None:
    assert bangumi_subject_to_content(row, strategy="bangumi-search") is None


@pytest.mark.parametrize("meta_tags", ["TVA", {"TV": 1}, 42, True])
def test_subject_tags_ignore_non_list_meta_tags(meta_tags: object) -> None:
    # Schema drift (a bare string / dict / scalar) must not be walked
    # character-by-character; tags then come only from the ``tags`` array.
    item = bangumi_subject_to_content(
        _subject(meta_tags=meta_tags, tags=[{"name": "科幻", "count": 9}]),
        strategy="bangumi-ranked",
    )
    assert item is not None
    assert item.tags == ["科幻"]


def test_subject_tags_preserve_valid_list_meta_tags() -> None:
    item = bangumi_subject_to_content(
        _subject(meta_tags=["TV", "剧场版"], tags=[]),
        strategy="bangumi-ranked",
    )
    assert item is not None
    assert item.tags == ["TV", "剧场版"]


def _author_of(**overrides: object) -> str:
    item = bangumi_subject_to_content(_subject(**overrides), strategy="bangumi-ranked")
    assert item is not None
    return item.author_name


@pytest.mark.parametrize(
    ("subject_type", "infobox", "expected"),
    [
        # Each type leads with its own credit key; Bangumi has no shared one.
        (2, _ANIME_INFOBOX, "押井守"),
        (1, _BOOK_INFOBOX, "井上雄彦"),
        (4, _GAME_INFOBOX, "任天堂企画制作本部"),
        (3, [{"key": "艺术家", "value": "菅野よう子"}], "菅野よう子"),
        (6, [{"key": "导演", "value": "Frank Darabont"}], "Frank Darabont"),
    ],
)
def test_subject_author_name_reads_the_per_type_credit_key(
    subject_type: int, infobox: list[dict[str, object]], expected: str
) -> None:
    assert _author_of(type=subject_type, infobox=list(infobox)) == expected


@pytest.mark.parametrize(
    ("dropped_keys", "expected"),
    [
        ((), "押井守"),
        (("导演",), "士郎正宗（「攻殻機動隊」講談社刊）"),
        (("导演", "原作"), "Production I.G"),
        (("导演", "原作", "动画制作"), "講談社、バンダイビジュアル、MANGA ENTERTAINMENT"),
        (("导演", "原作", "动画制作", "製作"), ""),
    ],
)
def test_subject_author_name_walks_the_priority_ladder(
    dropped_keys: tuple[str, ...], expected: str
) -> None:
    # 导演 → 原作 → 动画制作 → 製作: a row that omits the leading credit must
    # still surface the next-best one instead of falling back to empty.
    infobox = [entry for entry in _ANIME_INFOBOX if entry["key"] not in dropped_keys]
    assert _author_of(infobox=infobox) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # ``[{"v": …}]`` and ``[{"k": …, "v": …}]`` both read through ``v``;
        # ``k`` is a sub-label (总导演 / 副导演), never a name.
        ([{"v": "今敏"}, {"v": "湯浅政明"}], "今敏、湯浅政明"),
        ([{"k": "总导演", "v": "庵野秀明"}, {"k": "副导演", "v": "摩砂雪"}], "庵野秀明、摩砂雪"),
        ([{"v": "押井守"}, {"k": "演出", "v": "西久保瑞穂"}], "押井守、西久保瑞穂"),
        # Duplicates collapse while order is preserved.
        ([{"v": "新房昭之"}, {"v": "新房昭之"}], "新房昭之"),
    ],
)
def test_subject_author_name_flattens_both_list_value_shapes(
    value: list[dict[str, object]], expected: str
) -> None:
    assert _author_of(infobox=[{"key": "导演", "value": value}]) == expected


@pytest.mark.parametrize(
    "infobox",
    [
        None,  # field absent entirely (SlimSubject rows from collections)
        "导演:押井守",  # schema drift to a bare string
        {"导演": "押井守"},  # schema drift to an object
        42,
        [],
        [{"key": "导演", "value": None}],
        [{"key": "导演", "value": []}],
        [{"key": "导演", "value": {}}],
        [{"key": "导演", "value": True}],
        [{"key": "导演", "value": 1995}],
        [{"key": "导演", "value": [{"v": None}]}],
        [{"key": "导演", "value": [{"k": "总导演"}]}],  # entry without a ``v``
        [{"key": "导演", "value": ["押井守"]}],  # bare list entries, not mappings
        ["押井守"],  # infobox entries that are not mappings
        [{"key": "", "value": "押井守"}],
        [{"key": "脚本", "value": "伊藤和典"}],  # no ladder key present
    ],
)
def test_subject_author_name_never_leaks_a_placeholder(infobox: object) -> None:
    # A missing credit must be "" — never a stringified "None" / "[]" / "{}",
    # the dirty-row class that COALESCE cannot repair once persisted.
    author = _author_of(infobox=infobox)
    assert author == ""
    assert author not in {"None", "[]", "{}", "null", "True"}


def test_subject_author_name_takes_the_first_non_empty_occurrence() -> None:
    # A duplicated key whose first value flattens to "" must not shadow the
    # later occurrence that actually carries the credit.
    infobox = [
        {"key": "导演", "value": []},
        {"key": "导演", "value": "細田守"},
    ]
    assert _author_of(infobox=infobox) == "細田守"


def test_subject_author_name_bounds_a_long_credit_roster() -> None:
    # Bangumi credits can list dozens of names (原画 rosters run 400+ chars).
    # A card field keeps the leading names and stays length-bounded.
    listed = [{"v": f"名字{index}"} for index in range(12)]
    assert _author_of(infobox=[{"key": "导演", "value": listed}]) == "名字0、名字1、名字2"

    # A single string holding a whole roster is cut on a name separator, so the
    # credit never ends mid-name or inside an unclosed bracket.
    long_credit = "、".join(f"制作公司{index:02d}" for index in range(20))
    author = _author_of(infobox=[{"key": "导演", "value": long_credit}])
    assert len(author) <= 80
    assert author.startswith("制作公司00、制作公司01")
    assert not author.endswith("、")
    assert all(part in long_credit.split("、") for part in author.split("、"))

    # Real-world shape (猫和老鼠 1965): bracketed names that a blind 80-char slice
    # would leave open mid-bracket. 61 chars fit; the 4th name pushes past the
    # cap, so the credit is cut back to the last complete name.
    bracketed = (
        "William Hanna（《猫和老鼠》）、Joseph Barbera（《猫和老鼠》）、Tex Avery（《德鲁比》）"
    )
    roster = bracketed + "、Michael Lah（《德鲁比》）、Chuck Jones（《兔八哥》）"
    assert len(bracketed) == 61 and len(roster) > 80
    author = _author_of(infobox=[{"key": "导演", "value": roster}])
    assert author == bracketed
    assert author.count("（") == author.count("）")

    # No separator within budget → hard cut, still bounded.
    author = _author_of(infobox=[{"key": "导演", "value": "名" * 200}])
    assert author == "名" * 80


def test_subject_author_name_never_ends_on_an_unclosed_bracket() -> None:
    # A hard cut with no separator in range used to stop inside a bracket and
    # render "…（総監督" on the card. Trim back to before the open bracket.
    credit = "名" * 70 + "（総監督" + "字" * 40
    author = _author_of(infobox=[{"key": "导演", "value": credit}])
    assert author == "名" * 70
    assert author.count("（") == author.count("）")

    # Same for ASCII and other bracket families, and a nested pair only counts
    # as open when it really is unbalanced.
    assert _author_of(infobox=[{"key": "导演", "value": "A" * 70 + "[credit" + "B" * 40}]) == (
        "A" * 70
    )

    # A closer only settles an opener of the SAME family. Popping on any
    # closer declared "(credit]" balanced and kept the dangling "(".
    for opener, stray in (("(", "]"), ("（", "】"), ("【", ")"), ("《", "」")):
        credit = "A" * 70 + opener + "credit" + stray + "B" * 40
        assert _author_of(infobox=[{"key": "导演", "value": credit}]) == "A" * 70

    # A stray closer with no opener at all settles nothing and is harmless.
    assert _author_of(infobox=[{"key": "导演", "value": "A" * 70 + "]tail" + "B" * 40}]) == (
        "A" * 70 + "]tail" + "B" * 5
    )
    balanced = "名" * 60 + "《作品》" + "、" + "字" * 40
    author = _author_of(infobox=[{"key": "导演", "value": balanced}])
    assert author == "名" * 60 + "《作品》"

    # Deliberate limitation: when the whole credit is one long parenthetical the
    # opener sits at index 0, so trimming would erase a real credit entirely.
    # The hard cut is kept instead — asserted so the trade-off stays visible.
    whole = "（" + "甲" * 90 + "）"
    author = _author_of(infobox=[{"key": "导演", "value": whole}])
    assert author == "（" + "甲" * 79
    assert len(author) == 80


@pytest.mark.parametrize(
    "value",
    [
        # Stringified nulls — the only class this filter targets. A literal
        # "None" in the source data is exactly as unusable as one we produced
        # ourselves. Punctuation is NOT in scope; see the keep test for why.
        "None",
        "none",
        "NULL",
        "NaN",
        "undefined",
        "   ",
        # ...and the same values arriving through either list shape.
        [{"v": "None"}],
        [{"k": "总导演", "v": "null"}],
        # Non-string ``v``: str() would have manufactured "['押井守']" / "42".
        [{"v": ["押井守"]}],
        [{"v": {"name": "押井守"}}],
        [{"v": 42}],
        [{"v": True}],
    ],
)
def test_subject_author_name_rejects_absence_spellings_and_non_string_values(
    value: object,
) -> None:
    author = _author_of(infobox=[{"key": "导演", "value": value}])
    assert author == ""
    assert "押井守" not in author  # never reconstructed via str() on a drift


def test_subject_author_name_keeps_ambiguous_short_credits() -> None:
    """The placeholder filter must not delete plausible real names.

    Every entry here was either a real false positive we shipped or is one
    waiting to happen. Three separate attempts to widen the filter each
    deleted a real artist, so the scope is now only stringified nulls.
    """
    credits = (
        # Shipped false positive: a real Japanese rock band, and no
        # Python/JS/JSON path emits that spelling (it is Ruby/Lisp).
        "nil",
        "NIL",
        # Editor prose that merely *means* absence — same class as the CJK
        # words below, and not something this stack can emit.
        "N/A",
        "n/a",
        "(none)",
        # Romanised 나 / 娜 surname; single letters and digits are stage names.
        "Na",
        "na",
        "0",
        "X",
        "N",
        "无",
        "未知",
        "暂无",
        "不明",
    )
    for credit in credits:
        assert _author_of(infobox=[{"key": "导演", "value": credit}]) == credit
        assert _author_of(infobox=[{"key": "导演", "value": [{"v": credit}]}]) == credit


def test_subject_author_name_keeps_punctuation_only_artist_names() -> None:
    """Punctuation-only credits are kept — they can be real artist names.

    A "real names contain a letter or digit" rule looked safe and deleted
    both of these. A punctuation-only credit only renders oddly on a card;
    erasing a real artist is data loss, so the asymmetry decides it.
    """
    for credit in (
        "・・・・・・・・・",  # real idol group, all U+30FB
        "!!!",  # real band (chk chk chk)
        "△",
        "-",
        "——",
        "…",
        "?",
        "（）",
    ):
        assert _author_of(infobox=[{"key": "导演", "value": credit}]) == credit
        assert _author_of(infobox=[{"key": "导演", "value": [{"v": credit}]}]) == credit


def test_subject_author_name_skips_placeholder_entries_within_a_list() -> None:
    # One junk entry must not poison the rest of the roster, and a key whose
    # value is entirely placeholders still falls through to the next ladder key.
    value = [{"v": "None"}, {"v": "今敏"}, {"v": "null"}, {"v": "湯浅政明"}]
    assert _author_of(infobox=[{"key": "导演", "value": value}]) == "今敏、湯浅政明"
    assert (
        _author_of(
            infobox=[
                {"key": "导演", "value": [{"v": "None"}, {"v": None}]},
                {"key": "原作", "value": "士郎正宗"},
            ]
        )
        == "士郎正宗"
    )


@pytest.mark.parametrize(
    ("rate", "collection_type", "event_type", "strength", "feedback_type"),
    [
        (8, 5, "like", 0.85, None),
        (4, 1, "feedback", 1.0, "dislike"),
        (0, 1, "favorite", 1.0, None),
        (0, 3, "favorite", 0.85, None),
        (0, 2, "view", 0.35, None),
        (0, 4, "view", 0.25, None),
        (0, 5, "feedback", 0.60, "dislike"),
    ],
)
def test_public_collection_signal_matrix(
    rate: int,
    collection_type: int,
    event_type: str,
    strength: float,
    feedback_type: str | None,
) -> None:
    event = bangumi_collection_to_event(
        {
            "subject_id": 326,
            "type": collection_type,
            "rate": rate,
            "comment": "good\u0000" * 100,
            "updated_at": "2026-01-01T00:00:00Z",
            "private": False,
            "subject": {
                "id": 326,
                "type": 2,
                "name": "Title",
                "score": 9.2,
                "rank": 1,
                "meta_tags": ["TV", "剧场版"],
            },
        },
        username="sai",
    )
    assert event is not None
    assert event["event_type"] == event_type
    assert event["metadata"]["signal_strength"] == strength
    assert event["metadata"].get("feedback_type") == feedback_type
    assert event["metadata"]["source_updated_at"] == "2026-01-01T00:00:00Z"
    # Subject-type id 2 → readable "动画" so the profile LLM never decodes ints.
    assert event["metadata"]["subject_type"] == 2
    assert event["metadata"]["subject_type_label"] == "动画"
    # Subject-level meta tags travel with the collection event too.
    assert event["metadata"]["meta_tags"] == ["TV", "剧场版"]
    assert "timestamp" not in event["metadata"]
    assert "\u0000" not in event["metadata"]["collection_comment"]
    assert len(event["metadata"]["collection_comment"]) <= 200


@pytest.mark.parametrize(
    ("subject_type", "label"),
    [(1, "书籍"), (2, "动画"), (3, "音乐"), (4, "游戏"), (6, "三次元")],
)
def test_collection_event_maps_subject_type_label(subject_type: int, label: str) -> None:
    event = bangumi_collection_to_event(
        {
            "subject_id": 42,
            "type": 1,
            "subject": {"id": 42, "type": subject_type, "name": "Title"},
        },
        username="sai",
    )
    assert event is not None
    assert event["metadata"]["subject_type"] == subject_type
    assert event["metadata"]["subject_type_label"] == label


@pytest.mark.parametrize("meta_tags", ["TVA", {"TV": 1}, 42, True, None])
def test_collection_event_ignores_non_list_meta_tags(meta_tags: object) -> None:
    # Schema drift on the embedded subject must yield an empty list, never a
    # character-by-character walk of a bare string.
    event = bangumi_collection_to_event(
        {
            "subject_id": 42,
            "type": 1,
            "subject": {"id": 42, "type": 2, "name": "Title", "meta_tags": meta_tags},
        },
        username="sai",
    )
    assert event is not None
    assert event["metadata"]["meta_tags"] == []


def test_collection_event_meta_tags_dedupe_and_strip() -> None:
    event = bangumi_collection_to_event(
        {
            "subject_id": 42,
            "type": 1,
            "subject": {
                "id": 42,
                "type": 2,
                "name": "Title",
                "meta_tags": ["TV", " TV ", "剧场版", ""],
            },
        },
        username="sai",
    )
    assert event is not None
    assert event["metadata"]["meta_tags"] == ["TV", "剧场版"]


def test_private_collection_is_never_imported() -> None:
    assert (
        bangumi_collection_to_event(
            {
                "subject_id": 1,
                "type": 1,
                "private": True,
                "subject": {"id": 1, "type": 2, "name": "Private"},
            },
            username="sai",
        )
        is None
    )


def test_private_collection_is_imported_when_authenticated() -> None:
    row = {
        "subject_id": 1,
        "type": 1,
        "private": True,
        "subject": {"id": 1, "type": 2, "name": "Private"},
    }
    # Anonymous callers still skip private rows; the token owner reading their
    # own collection (include_private=True) keeps them as legitimate signal.
    assert bangumi_collection_to_event(row, username="sai") is None
    event = bangumi_collection_to_event(row, username="sai", include_private=True)
    assert event is not None
    assert event["metadata"]["subject_id"] == "1"


@pytest.mark.asyncio
async def test_public_collection_fetch_balances_status_and_subject_type() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str]] = []

        async def get_user_collections(
            self,
            username: str,
            *,
            collection_type: int,
            subject_type: str,
            limit: int,
            offset: int,
        ) -> BangumiPage:
            self.calls.append((collection_type, subject_type))
            type_id = {"anime": 2, "book": 1}[subject_type]
            subject_id = collection_type * 10 + type_id
            return BangumiPage(
                [
                    {
                        "subject_id": subject_id,
                        "type": collection_type,
                        "private": False,
                        "subject": {
                            "id": subject_id,
                            "type": type_id,
                            "name": f"subject-{subject_id}",
                        },
                    }
                ],
                total=1,
                limit=limit,
                offset=offset,
            )

    client = _Client()
    events = await fetch_bangumi_public_collection_events(
        client,
        username="sai",
        subject_types=("anime", "book"),
        limit=10,
    )

    assert len(events) == 10
    assert set(client.calls) == {
        (collection_type, subject_type)
        for collection_type in range(1, 6)
        for subject_type in ("anime", "book")
    }


@pytest.mark.asyncio
async def test_collection_fetch_threads_include_private() -> None:
    class _PrivateClient:
        async def get_user_collections(
            self, username: str, *, collection_type: int, subject_type: str, limit: int, offset: int
        ) -> BangumiPage:
            return BangumiPage(
                [
                    {
                        "subject_id": collection_type,
                        "type": collection_type,
                        "private": True,
                        "subject": {"id": collection_type, "type": 2, "name": "priv"},
                    }
                ],
                total=1,
                limit=limit,
                offset=offset,
            )

    anon = await fetch_bangumi_public_collection_events(
        _PrivateClient(), username="sai", subject_types=("anime",), limit=5
    )
    assert anon == []
    authed = await fetch_bangumi_public_collection_events(
        _PrivateClient(), username="sai", subject_types=("anime",), limit=5, include_private=True
    )
    assert len(authed) > 0


@pytest.mark.asyncio
async def test_public_collection_fetch_requests_full_api_pages() -> None:
    class _Client:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def get_user_collections(
            self,
            username: str,
            *,
            collection_type: int,
            subject_type: str,
            limit: int,
            offset: int,
        ) -> BangumiPage:
            self.limits.append(limit)
            type_index = {"anime": 1, "book": 2, "game": 3}[subject_type]
            base = type_index * 1_000_000 + collection_type * 100_000 + offset
            rows = [
                {
                    "subject_id": base + i,
                    "type": collection_type,
                    "private": False,
                    "subject": {"id": base + i, "type": 2, "name": "x"},
                }
                for i in range(limit)
            ]
            return BangumiPage(rows, total=10_000, limit=limit, offset=offset)

    client = _Client()
    events = await fetch_bangumi_public_collection_events(
        client,
        username="sai",
        subject_types=("anime", "book", "game"),
        limit=300,
    )

    # 15 lanes → per_pair 20. Every request must ask for the 50-row API cap
    # (not the small per_pair), and the fair-share cap holds each lane to one
    # visit this round: 15 calls total, not 6 whole-page grabs.
    assert len(events) == 300
    assert set(client.limits) == {50}
    assert len(client.limits) == 15


@pytest.mark.asyncio
async def test_public_collection_fetch_buffers_full_pages_across_visits() -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[int, str, int, int]] = []

        async def get_user_collections(
            self,
            username: str,
            *,
            collection_type: int,
            subject_type: str,
            limit: int,
            offset: int,
        ) -> BangumiPage:
            self.calls.append((collection_type, subject_type, limit, offset))
            if collection_type == 2 and subject_type == "anime":
                rows = [
                    {
                        "subject_id": 500_000 + offset + i,
                        "type": 2,
                        "private": False,
                        "subject": {"id": 500_000 + offset + i, "type": 2, "name": "a"},
                    }
                    for i in range(limit)
                ]
                return BangumiPage(rows, total=10_000, limit=limit, offset=offset)
            return BangumiPage([], total=0, limit=limit, offset=offset)

    client = _Client()
    events = await fetch_bangumi_public_collection_events(
        client,
        username="sai",
        subject_types=("anime",),
        limit=100,
    )

    # A single lane holds all the data. per_pair is 20 (100 / 5 lanes), but one
    # buffered 50-row page serves 2.5 fair-share visits, so the heavy lane makes
    # only ceil(100 / 50) = 2 paced calls (offsets 0 and 50) — never over-import.
    assert len(events) == 100
    heavy = [call for call in client.calls if call[0] == 2 and call[1] == "anime"]
    assert [(limit, offset) for _, _, limit, offset in heavy] == [(50, 0), (50, 50)]
