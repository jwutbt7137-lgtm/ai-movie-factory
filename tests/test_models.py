"""核心数据结构校验测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aimovie.models import (
    AnalysisTimeline,
    DialogueSegment,
    MediaInfo,
    ProjectRecord,
    ProjectStatus,
    Shot,
    ShotTimeline,
    format_clock,
    load_timeline_json,
)


def _media() -> MediaInfo:
    return MediaInfo(
        source_path="C:/media/demo.mp4",
        duration_s=120.5,
        width=1920,
        height=1080,
        fps=23.976,
        video_codec="h264",
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48000,
        has_audio=True,
    )


def test_media_info_derived_labels() -> None:
    media = _media()
    assert media.resolution_label == "1920×1080"
    assert media.duration_label == "00:02:01"
    assert "aac" in media.audio_label
    assert media.fps_label == "23.976"


def test_media_requires_positive_duration() -> None:
    with pytest.raises(ValidationError):
        MediaInfo(source_path="x.mp4", duration_s=0, width=100, height=100, fps=24)


def test_shot_validation_and_duration() -> None:
    shot = Shot(index=1, start_s=1.0, end_s=3.5)
    assert shot.duration_s == 2.5
    with pytest.raises(ValidationError):
        Shot(index=1, start_s=2.0, end_s=1.0)


def test_shot_timeline_rejects_overlap_and_out_of_order() -> None:
    timeline = ShotTimeline(shots=[Shot(index=1, start_s=0, end_s=1), Shot(index=2, start_s=1, end_s=2)])
    assert len(timeline) == 2
    assert timeline.shot_at(1.5).index == 2  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        ShotTimeline(shots=[Shot(index=1, start_s=2, end_s=3), Shot(index=2, start_s=0, end_s=1)])


def test_dialogue_cleans_whitespace_and_rejects_empty() -> None:
    segment = DialogueSegment(index=1, start_s=0, end_s=1, text="  你好   世界  ")
    assert segment.text == "你好 世界"
    with pytest.raises(ValidationError):
        DialogueSegment(index=1, start_s=0, end_s=1, text="   ")


def test_project_record_status_label() -> None:
    record = ProjectRecord(
        id="abc",
        name="测试",
        source_path="C:/a.mp4",
        project_dir="C:/p",
        status=ProjectStatus.READY,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        duration_s=61,
    )
    assert record.status_label == "分析完成"
    assert record.duration_label == "00:01:01"


def test_analysis_timeline_roundtrip(tmp_path) -> None:
    timeline = AnalysisTimeline(
        project_id="abc",
        project_name="测试",
        source_path="C:/a.mp4",
        media=_media(),
        shots=[Shot(index=1, start_s=0, end_s=1.0)],
        dialogues=[DialogueSegment(index=1, start_s=0, end_s=0.8, text="你好")],
        scene_backend="fake",
        speech_engine="fake",
        warnings=["测试警告"],
    )
    path = timeline.write_json(tmp_path / "timeline.json")
    loaded = load_timeline_json(path)
    assert loaded.project_id == "abc"
    assert loaded.shots[0].end_s == 1.0
    assert loaded.warnings == ["测试警告"]


def test_format_clock() -> None:
    assert format_clock(0) == "00:00"
    assert format_clock(61.2) == "01:01"
    assert format_clock(3661) == "01:01:01"
