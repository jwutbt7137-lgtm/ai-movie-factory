"""剧情分析器测试：使用假客户端，验证证据校验与结果落库，不联网。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aimovie.analysis.story import StoryAnalyzer
from aimovie.config import AppConfig
from aimovie.db import ProjectDB
from aimovie.errors import StoryAnalysisError, TaskCancelled
from aimovie.llm.client import LLMResult
from aimovie.models import DialogueSegment, MediaInfo, Shot, StoryAnalysis
from aimovie.pipeline.control import TaskControl


class FakeChatClient:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def chat(self, messages, *, purpose, max_tokens, temperature=0.3):
        self.calls.append((purpose, messages))
        if not self.contents:
            raise AssertionError("假客户端没有更多响应")
        content = self.contents.pop(0)
        return LLMResult(
            content=content,
            model="fake-model",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_cny=0.001,
            cached=False,
            duration_s=0.01,
            finish_reason="stop",
        )


def _media(path: Path) -> MediaInfo:
    return MediaInfo(
        source_path=str(path),
        duration_s=24.0,
        width=640,
        height=360,
        fps=24.0,
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48000,
        has_audio=True,
    )


def _setup_project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"not a real movie")
    project = db.create_project("剧情分析测试", source, tmp_path / "project")
    db.save_media(project.id, _media(source))
    shots = [
        Shot(index=1, start_s=0, end_s=12),
        Shot(index=2, start_s=12, end_s=24),
    ]
    db.replace_shots(project.id, shots)
    dialogues = [
        DialogueSegment(
            index=index,
            start_s=(index - 1) * 3,
            end_s=(index - 1) * 3 + 2.5,
            text=f"第{index}句对白，讲述主角正在调查一件重要的事情。",
            language="zh",
        )
        for index in range(1, 9)
    ]
    db.replace_dialogues(project.id, dialogues)
    return project


def _config() -> AppConfig:
    config = AppConfig(data_dir="")
    config.story.chunk_target_chars = 500
    config.story.min_dialogue_chars = 50
    config.story.analysis_max_tokens = 500
    config.story.integration_max_tokens = 512
    return config


def _segment_payload() -> str:
    return json.dumps(
        {
            "summary": "主角发现了一条线索，并决定继续调查。",
            "events": [
                {
                    "description": "主角发现线索",
                    "evidence_dialogue_ids": [1, 999],
                    "importance": "high",
                },
                {
                    "description": "无证据的编造事件",
                    "evidence_dialogue_ids": [999],
                    "importance": "low",
                },
            ],
            "characters": [
                {"name": "主角", "description": "调查者", "evidence_dialogue_ids": [1]}
            ],
            "conflicts": [
                {"description": "调查受到阻碍", "evidence_dialogue_ids": [2]}
            ],
            "turning_points": [
                {"description": "发现关键线索", "evidence_dialogue_ids": [3]}
            ],
            "uncertainties": ["主角的真实身份尚不明确"],
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )


def _integration_payload() -> str:
    return json.dumps(
        {
            "title": "调查者",
            "logline": "主角在阻碍中调查一条线索。",
            "overall_summary": "主角发现线索，调查受到阻碍，但仍在继续。",
            "main_conflict": "调查与阻碍",
            "themes": ["真相与阻力"],
            "characters": [
                {
                    "name": "主角",
                    "description": "调查者",
                    "role": "主角",
                    "evidence_segment_ids": [1],
                    "relations": [
                        {"target": "阻碍者", "relation": "对立", "evidence_segment_ids": [1]}
                    ],
                }
            ],
            "turning_points": [
                {
                    "description": "发现关键线索",
                    "segment_index": 1,
                    "evidence_dialogue_ids": [3],
                }
            ],
            "unresolved": ["主角身份未明"],
            "review_notes": ["需要核对人物身份"],
        },
        ensure_ascii=False,
    )


def test_story_analysis_with_evidence_validation(db: ProjectDB, tmp_path: Path) -> None:
    project = _setup_project(db, tmp_path)
    client = FakeChatClient([_segment_payload(), _integration_payload()])
    progress: list[int] = []
    logs: list[str] = []
    analyzer = StoryAnalyzer(db, _config(), client=client)
    result = analyzer.run(
        project.id,
        on_progress=lambda update: progress.append(update.percent),
        on_log=logs.append,
    )
    analysis = result.analysis
    assert analysis.segment_count == 1
    assert analysis.api_calls == 2
    assert analysis.cached_calls == 0
    assert analysis.estimated_cost_cny == pytest.approx(0.002)
    assert analysis.needs_review is True
    assert len(analysis.segments[0].events) == 1, "无有效证据的事件必须被移除"
    assert analysis.segments[0].events[0].evidence_dialogue_ids == [1]
    assert analysis.segments[0].needs_review is True
    assert any("缺少有效对白证据" in warning for warning in analysis.warnings)
    assert analysis.characters[0].name == "主角"
    assert analysis.turning_points[0].segment_index == 1
    assert any("未进行画面视觉分析" in warning for warning in analysis.warnings)
    assert progress and progress[-1] == 100
    assert any("剧情分析完成" in line for line in logs)

    stored = db.get_story_analysis(project.id)
    assert stored is not None
    assert isinstance(stored, StoryAnalysis)
    assert stored.segments[0].events[0].description == "主角发现线索"
    assert "evidence_dialogue_ids" in client.calls[0][1][1]["content"]


def test_story_analysis_without_dialogues_raises(db: ProjectDB, tmp_path: Path) -> None:
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    project = db.create_project("无对白", source, tmp_path / "project2")
    db.save_media(project.id, _media(source))
    analyzer = StoryAnalyzer(db, _config(), client=FakeChatClient([]))
    with pytest.raises(StoryAnalysisError, match="没有对白"):
        analyzer.run(project.id)


def test_story_analysis_invalid_json_raises(db: ProjectDB, tmp_path: Path) -> None:
    project = _setup_project(db, tmp_path)
    analyzer = StoryAnalyzer(db, _config(), client=FakeChatClient(["这不是 JSON"]))
    with pytest.raises(StoryAnalysisError, match="不是合法 JSON"):
        analyzer.run(project.id)
    assert db.get_story_analysis(project.id) is None


def test_story_analysis_cancellation(db: ProjectDB, tmp_path: Path) -> None:
    project = _setup_project(db, tmp_path)
    control = TaskControl()
    control.cancel()
    client = FakeChatClient([_segment_payload(), _integration_payload()])
    analyzer = StoryAnalyzer(db, _config(), client=client)
    with pytest.raises(TaskCancelled):
        analyzer.run(project.id, control=control)
    assert client.calls == []
    assert db.get_story_analysis(project.id) is None
