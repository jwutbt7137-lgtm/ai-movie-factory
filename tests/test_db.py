"""SQLite 项目数据层测试。"""

from __future__ import annotations

from pathlib import Path

from aimovie.db import ProjectDB
from aimovie.models import DialogueSegment, MediaInfo, ProjectStatus, Shot


def _media(path: Path) -> MediaInfo:
    return MediaInfo(
        source_path=str(path),
        duration_s=42.0,
        width=1280,
        height=720,
        fps=25.0,
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48000,
        has_audio=True,
        size_bytes=1234,
    )


def test_project_roundtrip(db: ProjectDB, sample_video: Path, tmp_path: Path) -> None:
    project_dir = tmp_path / "项目 目录"
    project = db.create_project("我的 项目", sample_video, project_dir)
    assert project_dir.exists()
    assert project.status == ProjectStatus.IMPORTED
    assert project.shot_count == 0

    media = _media(sample_video)
    db.save_media(project.id, media)
    loaded_media = db.get_media(project.id)
    assert loaded_media is not None
    assert loaded_media.duration_s == 42.0

    shots = [Shot(index=1, start_s=0, end_s=1.5), Shot(index=2, start_s=1.5, end_s=3.0)]
    db.replace_shots(project.id, shots)
    db.update_shot_keyframes(project.id, {1: "C:/kf/1.jpg"})
    loaded_shots = db.get_shots(project.id)
    assert [shot.index for shot in loaded_shots] == [1, 2]
    assert loaded_shots[0].keyframe_path == "C:/kf/1.jpg"

    dialogues = [
        DialogueSegment(index=1, start_s=0.1, end_s=0.8, text="第一句", language="zh"),
        DialogueSegment(index=2, start_s=1.0, end_s=1.7, text="第二句", language="zh"),
    ]
    db.replace_dialogues(project.id, dialogues)
    assert len(db.get_dialogues(project.id)) == 2

    db.update_project(project.id, status=ProjectStatus.READY)
    refreshed = db.get_project(project.id)
    assert refreshed is not None
    assert refreshed.status == ProjectStatus.READY
    assert refreshed.shot_count == 2
    assert refreshed.dialogue_count == 2
    assert refreshed.duration_s == 42.0


def test_timeline_export(db: ProjectDB, sample_video: Path, tmp_path: Path) -> None:
    project = db.create_project("导出测试", sample_video, tmp_path / "p2")
    db.save_media(project.id, _media(sample_video))
    db.replace_shots(project.id, [Shot(index=1, start_s=0, end_s=2)])
    db.replace_dialogues(
        project.id,
        [DialogueSegment(index=1, start_s=0.2, end_s=1.0, text="你好", language="zh")],
    )
    out = tmp_path / "timeline.json"
    db.export_timeline(project.id, out)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "导出测试" in text
    assert "你好" in text


def test_runs_and_delete(db: ProjectDB, sample_video: Path, tmp_path: Path) -> None:
    project = db.create_project("运行记录", sample_video, tmp_path / "p3")
    run_id = db.add_run(project.id, "phase1")
    db.finish_run(run_id, "completed", "ok")
    runs = db.recent_runs(project.id)
    assert runs[0]["status"] == "completed"
    assert runs[0]["message"] == "ok"

    db.delete_project(project.id)
    assert db.get_project(project.id) is None
    assert db.get_shots(project.id) == []


def test_list_projects_order(db: ProjectDB, sample_video: Path, tmp_path: Path) -> None:
    first = db.create_project("A", sample_video, tmp_path / "a")
    second = db.create_project("B", sample_video, tmp_path / "b")
    projects = db.list_projects()
    assert {project.id for project in projects} == {first.id, second.id}
