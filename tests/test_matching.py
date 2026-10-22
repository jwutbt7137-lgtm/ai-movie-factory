"""文案-镜头匹配测试（纯本地规则）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.config import AppConfig
from aimovie.db import ProjectDB
from aimovie.errors import ShotMatchingError
from aimovie.matching.editing import apply_manual_selection
from aimovie.matching.matcher import ShotMatcher
from aimovie.models import (
    Script,
    ScriptParagraph,
    Shot,
    ShotMatchPlan,
    SpeechUnit,
    StoryAnalysis,
    StorySegment,
    Voiceover,
)


def _project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    project = db.create_project("匹配测试", source, tmp_path / "project")
    db.replace_shots(
        project.id,
        [
            Shot(index=1, start_s=0, end_s=0.6),
            Shot(index=2, start_s=0.6, end_s=1.2),
            Shot(index=3, start_s=1.2, end_s=2.0),
            Shot(index=4, start_s=2.0, end_s=3.0),
        ],
    )
    analysis = StoryAnalysis(
        project_id=project.id,
        project_name=project.name,
        segments=[
            StorySegment(
                index=1,
                start_s=0,
                end_s=3,
                summary="测试片段",
                dialogue_ids=[1],
                shot_ids=[1, 2, 3],
            )
        ],
        turning_points=[],
    )
    db.save_story_analysis(analysis)
    script = db.save_script_version(
        Script(
            project_id=project.id,
            style_id="quick3",
            style_name="三分钟剧情速看",
            title="匹配文案",
            paragraphs=[
                ScriptParagraph(
                    index=1,
                    text="第一句。第二句。",
                    segment_ids=[1],
                    shot_ids=[1, 2],
                )
            ],
        )
    )
    units = [
        SpeechUnit(
            index=1,
            paragraph_index=1,
            text="第一句。",
            start_s=0,
            end_s=0.9,
            duration_s=0.9,
            audio_path=str(tmp_path / "a.wav"),
            engine="fake",
            segment_ids=[1],
            shot_ids=[1, 2],
        ),
        SpeechUnit(
            index=2,
            paragraph_index=1,
            text="第二句。",
            start_s=0.9,
            end_s=1.7,
            duration_s=0.8,
            audio_path=str(tmp_path / "b.wav"),
            engine="fake",
            segment_ids=[1],
            shot_ids=[1, 2],
        ),
    ]
    voiceover = db.save_voiceover(
        Voiceover(
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            engine="fake",
            voice="fake",
            audio_path=str(tmp_path / "full.wav"),
            total_duration_s=1.7,
            units=units,
        )
    )
    return project, script, voiceover


def test_match_plan_links_shots(db: ProjectDB, tmp_path: Path) -> None:
    project, script, voiceover = _project(db, tmp_path)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    matcher = ShotMatcher(db, config)
    progress: list[int] = []
    plan = matcher.match(project.id, on_progress=lambda update: progress.append(update.percent))
    assert isinstance(plan, ShotMatchPlan)
    assert len(plan.selections) == 2
    assert plan.selections[0].shot_ids
    assert plan.selections[0].target_duration_s == pytest.approx(0.9)
    assert plan.average_confidence > 0
    assert progress and progress[-1] == 100
    stored = db.get_current_shot_match(project.id)
    assert stored is not None
    assert stored.script_id == script.id
    assert stored.voiceover_id == voiceover.id


def test_match_low_confidence_marked(db: ProjectDB, tmp_path: Path) -> None:
    project, script, voiceover = _project(db, tmp_path)
    # 去掉候选关联，强制全片顺序兜底
    changed = voiceover.model_copy(deep=True)
    changed.id = "voiceover-low"
    for unit in changed.units:
        unit.segment_ids = []
        unit.shot_ids = []
    db.save_voiceover(changed)
    plan = ShotMatcher(db, AppConfig(data_dir=str(tmp_path / "data"))).match(project.id)
    assert plan.selections
    assert any(selection.needs_review for selection in plan.selections)
    assert plan.low_confidence_count > 0


def test_manual_selection_creates_reviewed_version(db: ProjectDB, tmp_path: Path) -> None:
    project, _script, _voiceover = _project(db, tmp_path)
    plan = ShotMatcher(db, AppConfig(data_dir=str(tmp_path / "data"))).match(project.id)
    edited = apply_manual_selection(plan, 1, [3, 4], db.get_shots(project.id))
    assert edited.id != plan.id
    selection = edited.selections[0]
    assert selection.manual is True
    assert selection.shot_ids == [3, 4]
    assert selection.needs_review is False
    assert selection.confidence == 1.0
    db.save_shot_match(edited)
    stored = db.get_current_shot_match(project.id)
    assert stored is not None and stored.id == edited.id


def test_match_requires_voiceover(db: ProjectDB, tmp_path: Path) -> None:
    project, script, _voiceover = _project(db, tmp_path)
    with pytest.raises(ShotMatchingError, match="配音"):
        # 新项目没有配音
        source = tmp_path / "other.mp4"
        source.write_bytes(b"x")
        other = db.create_project("没有配音", source, tmp_path / "other")
        db.save_script_version(
            Script(project_id=other.id, title="x", paragraphs=[])
        )
        ShotMatcher(db, AppConfig(data_dir=str(tmp_path / "data"))).match(other.id)


def test_match_rejects_stale_voiceover_script(db: ProjectDB, tmp_path: Path) -> None:
    project, script, voiceover = _project(db, tmp_path)
    newer = db.save_script_version(
        Script(
            project_id=project.id,
            style_id="quick3",
            style_name="三分钟剧情速看",
            title="新版本",
            paragraphs=[
                ScriptParagraph(index=1, text="新文案。", segment_ids=[1], shot_ids=[1])
            ],
        )
    )
    # 手动把当前配音指向旧脚本之外的新脚本版本，模拟不匹配
    changed = voiceover.model_copy(deep=True)
    changed.id = "voiceover-old"
    changed.script_id = "old-script-id"
    db.save_voiceover(changed)
    with pytest.raises(ShotMatchingError, match="旧文案"):
        ShotMatcher(db, AppConfig(data_dir=str(tmp_path / "data"))).match(project.id)
