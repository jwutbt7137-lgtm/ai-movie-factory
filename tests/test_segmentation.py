"""剧情分段纯本地逻辑测试。"""

from __future__ import annotations

import pytest

from aimovie.config import StoryConfig
from aimovie.errors import StoryAnalysisError
from aimovie.models import DialogueSegment, Shot
from aimovie.analysis.segmentation import build_story_chunks


def _dialogue(index: int, text: str, start: float, end: float) -> DialogueSegment:
    return DialogueSegment(index=index, start_s=start, end_s=end, text=text)


def _shots() -> list[Shot]:
    return [
        Shot(index=1, start_s=0, end_s=10),
        Shot(index=2, start_s=10, end_s=20),
        Shot(index=3, start_s=20, end_s=30),
        Shot(index=4, start_s=30, end_s=40),
    ]


def test_chunking_by_chars_and_duration() -> None:
    config = StoryConfig(chunk_target_chars=500, chunk_max_duration_s=600, max_segments=40)
    dialogues = [
        _dialogue(index, "这是一段足够长的对白内容。" * 6, index * 4, index * 4 + 3)
        for index in range(1, 13)
    ]
    chunks = build_story_chunks(dialogues, _shots(), config)
    assert len(chunks) >= 2
    assert [chunk.index for chunk in chunks] == list(range(1, len(chunks) + 1))
    assert all(chunk.dialogues for chunk in chunks)
    assert all(chunk.shots for chunk in chunks)
    all_ids = [dialogue_id for chunk in chunks for dialogue_id in chunk.dialogue_ids]
    assert all_ids == [segment.index for segment in dialogues]
    assert sum(chunk.char_count for chunk in chunks) == sum(len(segment.text) for segment in dialogues)
    for chunk in chunks:
        assert chunk.start_s < chunk.end_s
        assert chunk.duration_s <= config.chunk_max_duration_s * 1.21


def test_long_gap_forces_split() -> None:
    config = StoryConfig(chunk_target_chars=5000, chunk_max_duration_s=600)
    dialogues = [
        _dialogue(1, "第一场戏的对白。" * 5, 0, 5),
        _dialogue(2, "第二场戏的对白。" * 120, 500, 505),
    ]
    chunks = build_story_chunks(dialogues, _shots(), config)
    assert len(chunks) == 2


def test_short_tail_is_merged() -> None:
    config = StoryConfig(chunk_target_chars=1000, max_segments=40)
    dialogues = [
        _dialogue(1, "长对白。" * 300, 0, 20),
        _dialogue(2, "短对白。", 21, 22),
    ]
    chunks = build_story_chunks(dialogues, _shots(), config)
    assert len(chunks) == 1
    assert chunks[0].dialogue_ids == [1, 2]


def test_no_dialogues_raises() -> None:
    with pytest.raises(StoryAnalysisError):
        build_story_chunks([], _shots(), StoryConfig())


def test_too_many_segments_raises() -> None:
    config = StoryConfig(chunk_target_chars=500, max_segments=1)
    dialogues = [
        _dialogue(index, "足够长的对白。" * 50, index * 10, index * 10 + 9)
        for index in range(1, 6)
    ]
    with pytest.raises(StoryAnalysisError, match="超过上限"):
        build_story_chunks(dialogues, _shots(), config)
