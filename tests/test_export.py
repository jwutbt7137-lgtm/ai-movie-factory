"""视频合成/字幕/导出测试（真实 FFmpeg，使用合成素材）。"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from aimovie.config import ExportConfig
from aimovie.db import ProjectDB
from aimovie.errors import TaskCancelled
from aimovie.export.subtitles import build_ass, build_srt
from aimovie.export.video import VideoExporter
from aimovie.models import (
    Script,
    ScriptParagraph,
    Shot,
    ShotMatchPlan,
    ShotSelection,
    SpeechUnit,
    Voiceover,
)
from aimovie.pipeline.control import TaskControl


def _make_audio(path: Path, seconds: float, frequency: float = 440.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        t = np.arange(int(48000 * seconds)) / 48000
        handle.writeframes((np.sin(2 * np.pi * frequency * t) * 2500).astype(np.int16).tobytes())
    return path


def _setup(db: ProjectDB, project, source, tmp_path: Path, *, bgm: bool = False):
    db.replace_shots(
        project.id,
        [
            Shot(index=1, start_s=0, end_s=1.2),
            Shot(index=2, start_s=1.2, end_s=2.4),
            Shot(index=3, start_s=2.4, end_s=3.6),
        ],
    )
    script = db.save_script_version(
        Script(
            project_id=project.id,
            style_id="quick3",
            style_name="测试",
            title="导出测试",
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
    voice = _make_audio(tmp_path / "voice.wav", 2.0)
    units = [
        SpeechUnit(
            index=1,
            paragraph_index=1,
            text="第一句。",
            start_s=0,
            end_s=1.0,
            duration_s=1.0,
            audio_path=str(voice),
            engine="fake",
            segment_ids=[1],
            shot_ids=[1, 2],
        ),
        SpeechUnit(
            index=2,
            paragraph_index=1,
            text="第二句。",
            start_s=1.0,
            end_s=2.0,
            duration_s=1.0,
            audio_path=str(voice),
            engine="fake",
            segment_ids=[1],
            shot_ids=[2, 3],
        ),
    ]
    voiceover = db.save_voiceover(
        Voiceover(
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            engine="fake",
            voice="fake",
            audio_path=str(voice),
            total_duration_s=2.0,
            units=units,
        )
    )
    db.save_shot_match(
        ShotMatchPlan(
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            voiceover_id=voiceover.id,
            selections=[
                ShotSelection(
                    sentence_index=1,
                    paragraph_index=1,
                    text="第一句。",
                    target_start_s=0,
                    target_end_s=1,
                    target_duration_s=1,
                    shot_ids=[1, 2],
                    shot_durations_s=[1.2, 1.2],
                    segment_ids=[1],
                    coverage_s=1,
                    confidence=0.7,
                    needs_review=True,
                ),
                ShotSelection(
                    sentence_index=2,
                    paragraph_index=1,
                    text="第二句。",
                    target_start_s=1,
                    target_end_s=2,
                    target_duration_s=1,
                    shot_ids=[2, 3],
                    shot_durations_s=[1.2, 1.2],
                    segment_ids=[1],
                    coverage_s=1,
                    confidence=0.7,
                    needs_review=True,
                ),
            ],
            total_duration_s=2.0,
            average_confidence=0.7,
            low_confidence_count=2,
        )
    )
    return script, voiceover


def _config(app_config, tmp_path: Path, *, bgm: Path | None = None) -> ExportConfig:
    del app_config
    return ExportConfig(
        width=320,
        height=240,
        fps=24,
        crf=30,
        preset="ultrafast",
        subtitle_mode="burn",
        font_size=18,
        margin_v=16,
        bgm_path=str(bgm) if bgm else "",
    )


def test_subtitle_builders() -> None:
    voiceover = Voiceover(
        project_id="p",
        script_id="s",
        audio_path="x",
        total_duration_s=2,
        units=[
            SpeechUnit(
                index=1,
                paragraph_index=1,
                text="第一句 {测试}。",
                start_s=0,
                end_s=1.234,
                duration_s=1.234,
                audio_path="x",
                engine="fake",
            ),
            SpeechUnit(
                index=2,
                paragraph_index=1,
                text="第二句。",
                start_s=1.5,
                end_s=2.0,
                duration_s=0.5,
                audio_path="x",
                engine="fake",
            ),
        ],
    )
    srt = build_srt(voiceover)
    assert "00:00:00,000 --> 00:00:01,234" in srt
    assert "第一句 {测试}。" in srt
    ass = build_ass(voiceover, width=320, height=240)
    assert "PlayResX: 320" in ass
    assert "Dialogue: 0,0:00:00.00,0:00:01.23" in ass
    assert "｛测试｝" in ass


@pytest.mark.ffmpeg
def test_export_burn_and_sidecars(db: ProjectDB, project, sample_video: Path, tmp_path: Path, app_config) -> None:
    script, voiceover = _setup(db, project, sample_video, tmp_path)
    cfg = _config(app_config, tmp_path)
    exporter = VideoExporter(db, app_config)
    progress: list[int] = []
    result = exporter.export(
        project.id,
        export_config=cfg,
        on_progress=lambda update: progress.append(update.percent),
    )
    assert Path(result.output_path).exists()
    assert Path(result.output_path).stat().st_size > 1024
    assert Path(result.srt_path).exists()
    assert Path(result.ass_path).exists()
    assert result.duration_s == pytest.approx(2.0, abs=0.15)
    assert result.subtitle_mode == "burn"
    assert result.bgm_used is False
    assert progress and progress[-1] == 100
    stored = db.get_current_export(project.id)
    assert stored is not None and stored.id == result.id


@pytest.mark.ffmpeg
def test_export_no_burn_and_bgm(db: ProjectDB, project, sample_video: Path, tmp_path: Path, app_config) -> None:
    script, voiceover = _setup(db, project, sample_video, tmp_path)
    bgm = _make_audio(tmp_path / "bgm.wav", 3.0, frequency=220.0)
    cfg = _config(app_config, tmp_path, bgm=bgm)
    cfg.subtitle_mode = "none"
    result = VideoExporter(db, app_config).export(project.id, export_config=cfg)
    assert Path(result.output_path).exists()
    assert result.subtitle_mode == "none"
    assert result.bgm_used is True
    assert result.duration_s == pytest.approx(2.0, abs=0.2)


@pytest.mark.ffmpeg
def test_export_center_crop(
    db: ProjectDB, project, sample_video: Path, tmp_path: Path, app_config, ffprobe_bin: str
) -> None:
    _setup(db, project, sample_video, tmp_path)
    cfg = _config(app_config, tmp_path)
    cfg.width = 320
    cfg.height = 320
    cfg.fit_mode = "crop"
    cfg.subtitle_mode = "none"
    result = VideoExporter(db, app_config).export(project.id, export_config=cfg)
    probe = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            result.output_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (320, 320)


@pytest.mark.ffmpeg
def test_export_soft_subtitle(
    db: ProjectDB, project, sample_video: Path, tmp_path: Path, app_config, ffprobe_bin: str
) -> None:
    _setup(db, project, sample_video, tmp_path)
    cfg = _config(app_config, tmp_path)
    cfg.subtitle_mode = "soft"
    result = VideoExporter(db, app_config).export(project.id, export_config=cfg)
    assert Path(result.output_path).exists()
    probe = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            result.output_path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    assert any(stream.get("codec_type") == "subtitle" and stream.get("codec_name") == "mov_text" for stream in streams)


@pytest.mark.ffmpeg
def test_export_cancel(db: ProjectDB, project, sample_video: Path, tmp_path: Path, app_config) -> None:
    _setup(db, project, sample_video, tmp_path)
    control = TaskControl()
    control.cancel()
    with pytest.raises(TaskCancelled):
        VideoExporter(db, app_config).export(
            project.id, export_config=_config(app_config, tmp_path), control=control
        )
    assert db.get_current_export(project.id) is None
