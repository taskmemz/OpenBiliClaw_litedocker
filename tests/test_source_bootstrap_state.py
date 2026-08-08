"""State normalization, bounded recency, and atomic writer regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING, Any

import pytest

from openbiliclaw.memory.manager import MemoryManager
from openbiliclaw.sources.bootstrap_state import (
    SOURCE_SEEN_KEY_CAP,
    default_source_bootstrap_state,
    merge_seen_keys,
    normalize_source_bootstrap_state,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        (None, default_source_bootstrap_state()),
        (
            {
                "xhs_seen_note_keys": ["saved:old"],
                "dy_seen_video_keys": ["dy:old"],
                "bootstrap_completed": ["xhs", "reddit"],
                "unknown_field": "discarded",
            },
            {
                **default_source_bootstrap_state(),
                "xhs_seen_note_keys": ["saved:old"],
                "dy_seen_video_keys": ["dy:old"],
            },
        ),
        (
            {
                "xhs_seen_note_keys": "not-a-list",
                "reddit_seen_item_keys": {"not": "a-list"},
                "last_source_bootstrap_sync_at": None,
                "source_incremental": {
                    "cursor": 42,
                    "last_attempt_at": ["not-a-map"],
                    "active_task": "not-a-task",
                },
            },
            default_source_bootstrap_state(),
        ),
    ],
)
def test_normalize_source_bootstrap_state_is_backward_compatible_and_strict(
    loaded: Any,
    expected: dict[str, object],
) -> None:
    assert normalize_source_bootstrap_state(loaded) == expected


def test_normalize_source_bootstrap_state_caps_legacy_lists() -> None:
    state = normalize_source_bootstrap_state(
        {"reddit_seen_item_keys": [f"reddit:{index}" for index in range(SOURCE_SEEN_KEY_CAP + 1)]}
    )

    keys = state["reddit_seen_item_keys"]
    assert isinstance(keys, list)
    assert len(keys) == SOURCE_SEEN_KEY_CAP
    assert keys[0] == "reddit:1"
    assert keys[-1] == f"reddit:{SOURCE_SEEN_KEY_CAP}"


def test_merge_seen_keys_refreshes_recency_and_collapses_blanks() -> None:
    merged = merge_seen_keys(
        ["old", "refresh", "keep", "refresh", ""],
        [" ", "new", "new", "refresh"],
        cap=3,
    )

    assert merged == ["keep", "new", "refresh"]


def test_update_source_bootstrap_state_normalizes_mutator_output(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path)

    state = memory.update_source_bootstrap_state(
        lambda current: {
            **current,
            "reddit_seen_item_keys": ["t3:first", "", "t3:first"],
            "source_incremental": "malformed",
        }
    )

    assert state["reddit_seen_item_keys"] == ["t3:first"]
    assert state["source_incremental"] == {
        "cursor": "",
        "last_attempt_at": {},
        "active_task": None,
    }
    assert memory.load_source_bootstrap_state() == state


def test_concurrent_multi_writer_updates_preserve_both_sources_repeatedly(
    tmp_path: Path,
) -> None:
    first = MemoryManager(tmp_path)
    second = MemoryManager(tmp_path)

    def write_key(
        memory: MemoryManager,
        state_key: str,
        key: str,
        barrier: Barrier,
    ) -> None:
        barrier.wait()

        def mutate(state: dict[str, object]) -> dict[str, object]:
            state[state_key] = merge_seen_keys(state.get(state_key, []), [key])
            return state

        memory.update_source_bootstrap_state(mutate)

    with ThreadPoolExecutor(max_workers=2) as executor:
        for iteration in range(20):
            barrier = Barrier(2)
            first_key = f"xhs:{iteration}"
            second_key = f"reddit:{iteration}"
            futures = [
                executor.submit(write_key, first, "xhs_seen_note_keys", first_key, barrier),
                executor.submit(write_key, second, "reddit_seen_item_keys", second_key, barrier),
            ]
            for future in futures:
                future.result()

            state = first.load_source_bootstrap_state()
            assert first_key in state["xhs_seen_note_keys"]
            assert second_key in state["reddit_seen_item_keys"]
