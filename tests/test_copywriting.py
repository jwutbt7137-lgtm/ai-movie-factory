"""解说文案生成与版本管理测试（假客户端，不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aimovie.config import AppConfig
from aimovie.copywriting.generator import ScriptGenerator, split_editable_text
from aimovie.copywriting.styles import STYLES
from aimovie.db import ProjectDB
from aimovie.errors import ScriptGenerationError
from aimovie.llm.client import LLMResult
from aimovie.models import (
    PlotEvent,
    SegmentCharacter,
    StoryAnalysis,
    StoryCharacter,
    StorySegment,
    TurningPoint,
)


class FakeChatClient:
    def __init__(self, contents: list[str]):
        self.contents = list(contents)
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    def chat(self, messages, *, purpose, max_tokens, temperature=0.3):
        self.calls.append((purpose, messages))
        if not self.contents:
            raise AssertionError("假客户端没有更多响应")
        return LLMResult(
            content=self.contents.pop(0),
            model="fake-model",
            prompt_tokens=200,
            completion_tokens=100,
            total_tokens=300,
            cost_cny=0.0012,
            cached=False,
            duration_s=0.01,
            finish_reason="stop",
        )


def _analysis(project_id: str) -> StoryAnalysis:
    segment1 = StorySegment(
        index=1,
        start_s=0,
        end_s=10,
        summary="主角接到任务并发现异常。",
        events=[
            PlotEvent(
                description="主角接到任务",
                evidence_dialogue_ids=[1, 2],
                importance="high",
            )
        ],
        characters=[
            SegmentCharacter(name="主角", description="调查者", evidence_dialogue_ids=[1])
        ],
        dialogue_ids=[1, 2],
        shot_ids=[1, 2],
        confidence=0.8,
    )
    segment2 = StorySegment(
        index=2,
        start_s=10,
        end_s=22,
        summary="主角在调查中与对手交锋。",
        events=[
            PlotEvent(
                description="主角与对手交锋",
                evidence_dialogue_ids=[3, 4],
                importance="high",
            )
        ],
        dialogue_ids=[3, 4],
        shot_ids=[3],
        confidence=0.7,
    )
    return StoryAnalysis(
        project_id=project_id,
        project_name="文案测试",
        title="调查者",
        logline="主角调查异常事件。",
        overall_summary="主角接到任务，发现异常并与对手交锋。",
        main_conflict="调查与阻挠",
        themes=["真相"],
        segments=[segment1, segment2],
        characters=[
            StoryCharacter(
                name="主角",
                description="调查者",
                role="主角",
                evidence_segment_ids=[1, 2],
            )
        ],
        turning_points=[
            TurningPoint(
                description="发现异常",
                segment_index=1,
                evidence_dialogue_ids=[2],
            )
        ],
        model="fake-model",
    )


def _project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    return db.create_project("文案测试", source, tmp_path / "project")


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(data_dir=str(tmp_path / "data"))


def _script_payload(invalid_second: bool = True) -> str:
    second_segment = [99] if invalid_second else [2]
    second_evidence = [999] if invalid_second else [3]
    return json.dumps(
        {
            "title": "调查者：异常背后的真相",
            "paragraphs": [
                {
                    "text": "如果一条看似普通的线索突然指向不该存在的人，你会继续查下去吗？",
                    "segment_ids": [1],
                    "evidence_dialogue_ids": [1],
                    "purpose": "hook",
                },
                {
                    "text": "主角没有退路，只能顺着线索追下去，而阻力也一步步逼近。",
                    "segment_ids": second_segment,
                    "evidence_dialogue_ids": second_evidence,
                    "purpose": "plot",
                },
            ],
            "review_notes": ["需要确认人物关系"],
        },
        ensure_ascii=False,
    )


def test_generate_validate_and_link_shots(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    db.save_story_analysis(_analysis(project.id))
    client = FakeChatClient([_script_payload(invalid_second=True)])
    generator = ScriptGenerator(db, _config(tmp_path), client=client)
    progress: list[int] = []
    result = generator.run(
        project.id,
        "quick3",
        minutes=3,
        on_progress=lambda update: progress.append(update.percent),
    )
    script = result.script
    assert script.version == 1
    assert script.style_name == "三分钟剧情速看"
    assert len(script.paragraphs) == 2
    assert script.paragraphs[0].segment_ids == [1]
    assert script.paragraphs[0].shot_ids == [1, 2]
    assert script.paragraphs[0].event_descriptions == ["主角接到任务"]
    assert script.paragraphs[1].needs_review is True
    assert script.paragraphs[1].shot_ids == []
    assert any("引用无效" in warning for warning in script.warnings)
    assert any("AI 生成文案" in note for note in script.review_notes)
    assert script.needs_review is True
    assert progress and progress[-1] == 100
    assert db.get_current_script(project.id).id == script.id  # type: ignore[union-attr]
    assert "三分钟剧情速看" in client.calls[0][1][1]["content"]
    assert "segment_ids" in client.calls[0][1][1]["content"]


def test_regenerate_creates_new_version_and_restore(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    db.save_story_analysis(_analysis(project.id))
    client = FakeChatClient([_script_payload(False), _script_payload(False)])
    generator = ScriptGenerator(db, _config(tmp_path), client=client)
    first = generator.run(project.id, "quick3").script
    second = generator.run(project.id, "suspense5").script
    assert (first.version, second.version) == (1, 2)
    assert db.get_current_script(project.id).id == second.id  # type: ignore[union-attr]
    versions = generator.list_versions(project.id)
    assert [item.version for item in versions] == [2, 1]
    restored = generator.restore_version(project.id, first.id)
    assert restored.version == 1
    assert db.get_current_script(project.id).id == first.id  # type: ignore[union-attr]


def test_manual_edit_creates_new_version(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    db.save_story_analysis(_analysis(project.id))
    generator = ScriptGenerator(db, _config(tmp_path), client=FakeChatClient([_script_payload(False)]))
    original = generator.run(project.id, "quick3").script
    edited = generator.save_manual_edit(
        project.id,
        original.id,
        "人工修改标题",
        "第一段人工改写。\n\n第二段人工改写。\n\n全新补充段落。",
    )
    assert edited.version == 2
    assert edited.source == "manual_edit"
    assert edited.title == "人工修改标题"
    assert [paragraph.index for paragraph in edited.paragraphs] == [1, 2, 3]
    assert edited.paragraphs[0].segment_ids == original.paragraphs[0].segment_ids
    assert edited.paragraphs[2].segment_ids == []
    assert edited.needs_review is True
    assert db.get_script(original.id) is not None, "旧版本必须保留"
    assert [item.version for item in generator.list_versions(project.id)] == [2, 1]


def test_custom_style_requires_instruction(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    db.save_story_analysis(_analysis(project.id))
    generator = ScriptGenerator(db, _config(tmp_path), client=FakeChatClient([]))
    with pytest.raises(ScriptGenerationError, match="自定义要求"):
        generator.run(project.id, "custom")


def test_missing_analysis_raises(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    generator = ScriptGenerator(db, _config(tmp_path), client=FakeChatClient([]))
    with pytest.raises(ScriptGenerationError, match="剧情分析"):
        generator.run(project.id, "quick3")


def test_invalid_json_raises(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    db.save_story_analysis(_analysis(project.id))
    generator = ScriptGenerator(db, _config(tmp_path), client=FakeChatClient(["不是 JSON"]))
    with pytest.raises(ScriptGenerationError, match="不是合法 JSON"):
        generator.run(project.id, "quick3")


def test_all_styles_defined() -> None:
    assert {style.id for style in STYLES} == {
        "quick3",
        "suspense5",
        "analysis10",
        "psychology",
        "scifi",
        "custom",
    }


def test_split_editable_text() -> None:
    assert split_editable_text("第一段\n\n第二段") == ["第一段", "第二段"]
    assert split_editable_text("第一行\n第二行") == ["第一行", "第二行"]
    assert split_editable_text("   ") == []
