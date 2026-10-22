"""第一阶段流水线测试（使用可注入的假检测器/识别器，不伪造真实 AI 输出）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.config import AppConfig
from aimovie.db import ProjectDB
from aimovie.errors import TaskCancelled
from aimovie.media.keyframes import KeyframeResult
from aimovie.media.scenes import SceneBoundary, SceneDetectionResult
from aimovie.models import DialogueSegment, MediaInfo, ProjectStatus
from aimovie.pipeline.control import TaskControl
from aimovie.pipeline.phase1 import AnalysisPipeline


def _media(source: Path) -> MediaInfo:
    return MediaInfo(
        source_path=str(source),
        duration_s=6.0,
        width=640,
        height=360,
        fps=24.0,
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48000,
        has_audio=True,
    )


class FakeTranscriber:
    engine_name = "fake-transcriber"

    def transcribe(self, video_path, media, work_dir, on_progress=None, check_cancel=None, on_status=None):
        if check_cancel is not None:
            check_cancel()
        if on_status is not None:
            on_status("fake transcribing")
        if on_progress is not None:
            on_progress(0.5)
        if check_cancel is not None:
            check_cancel()
        return [
            DialogueSegment(index=1, start_s=0.2, end_s=1.0, text="第一句对白", language="zh"),
            DialogueSegment(index=2, start_s=1.2, end_s=2.0, text="第二句对白", language="zh"),
        ]


def _fake_probe(source, ffprobe_path=None):
    return _media(Path(source))


def _fake_scenes(source, duration_s, fps, config, ffmpeg_path=None, on_progress=None, check_cancel=None):
    if on_progress is not None:
        on_progress(0.5)
    if check_cancel is not None:
        check_cancel()
    if on_progress is not None:
        on_progress(1.0)
    return SceneDetectionResult(
        boundaries=[
            SceneBoundary(0.0, 2.0),
            SceneBoundary(2.0, 4.0),
            SceneBoundary(4.0, 6.0),
        ],
        backend="fake-scene",
        warnings=["fake warning"],
    )


def _fake_keyframes(source, shots, output_dir, config, ffmpeg_path=None, on_progress=None, check_cancel=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[int, str] = {}
    for shot in shots:
        target = output_dir / f"shot_{shot.index:04d}.jpg"
        target.write_bytes(b"\xff\xd8fake-jpeg")
        mapping[shot.index] = str(target)
        if on_progress is not None:
            on_progress(shot.index / len(shots))
    return KeyframeResult(keyframes=mapping)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("aimovie.pipeline.phase1.resolve_executable", lambda configured, name: name)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"not a real movie")
    project = db.create_project("流水线测试", source, tmp_path / "project")
    return config, db, project, source


def test_pipeline_success(env, tmp_path: Path) -> None:
    config, db, project, source = env
    progress_values: list[int] = []
    logs: list[str] = []
    pipeline = AnalysisPipeline(
        db,
        config,
        probe_fn=_fake_probe,
        scene_fn=_fake_scenes,
        keyframe_fn=_fake_keyframes,
        transcriber_factory=FakeTranscriber,
    )
    result = pipeline.run(
        project.id,
        on_progress=lambda update: progress_values.append(update.percent),
        on_log=logs.append,
    )
    assert result.shot_count == 3
    assert result.dialogue_count == 2
    assert result.scene_backend == "fake-scene"
    assert result.speech_engine == "fake-transcriber"
    assert result.warnings == ["fake warning"]
    assert progress_values == sorted(progress_values)
    assert progress_values[-1] == 100
    assert any("完成" in line for line in logs)

    refreshed = db.get_project(project.id)
    assert refreshed is not None
    assert refreshed.status == ProjectStatus.READY
    media = db.get_media(project.id)
    assert media is not None and media.duration_s == 6.0
    shots = db.get_shots(project.id)
    assert len(shots) == 3
    assert all(shot.keyframe_path for shot in shots)
    dialogues = db.get_dialogues(project.id)
    assert [segment.text for segment in dialogues] == ["第一句对白", "第二句对白"]

    timeline_path = Path(result.timeline_path)
    assert timeline_path.exists()
    assert "fake-scene" in timeline_path.read_text(encoding="utf-8")


def test_pipeline_skip_speech(env) -> None:
    config, db, project, _source = env

    def factory():
        raise AssertionError("skip_speech=True 时不应加载语音引擎")

    pipeline = AnalysisPipeline(
        db,
        config,
        probe_fn=_fake_probe,
        scene_fn=_fake_scenes,
        keyframe_fn=_fake_keyframes,
        transcriber_factory=factory,
    )
    result = pipeline.run(project.id, skip_speech=True)
    assert result.dialogue_count == 0
    assert result.speech_engine == "已跳过"
    assert db.get_dialogues(project.id) == []
    assert any("跳过语音识别" in warning for warning in result.warnings)


def test_pipeline_cancelled(env) -> None:
    config, db, project, _source = env
    control = TaskControl()
    control.cancel()
    pipeline = AnalysisPipeline(
        db,
        config,
        probe_fn=_fake_probe,
        scene_fn=_fake_scenes,
        keyframe_fn=_fake_keyframes,
        transcriber_factory=FakeTranscriber,
    )
    with pytest.raises(TaskCancelled):
        pipeline.run(project.id, control=control)
    refreshed = db.get_project(project.id)
    assert refreshed is not None
    assert refreshed.status == ProjectStatus.CANCELLED


def test_pipeline_failure_marks_project_failed(env) -> None:
    config, db, project, _source = env

    def broken_scenes(*args, **kwargs):
        raise RuntimeError("场景检测引擎崩溃")

    pipeline = AnalysisPipeline(
        db,
        config,
        probe_fn=_fake_probe,
        scene_fn=broken_scenes,
        keyframe_fn=_fake_keyframes,
        transcriber_factory=FakeTranscriber,
    )
    with pytest.raises(RuntimeError, match="场景检测引擎崩溃"):
        pipeline.run(project.id)
    refreshed = db.get_project(project.id)
    assert refreshed is not None
    assert refreshed.status == ProjectStatus.FAILED
    assert "场景检测引擎崩溃" in (refreshed.error or "")
