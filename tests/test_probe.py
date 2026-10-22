"""ffprobe 影片信息读取测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.errors import VideoProbeError
from aimovie.media.probe import probe_media


@pytest.mark.ffmpeg
def test_probe_synthetic_video(sample_video: Path, ffprobe_bin: str) -> None:
    media = probe_media(sample_video, ffprobe_path=ffprobe_bin)
    assert media.source_path == str(sample_video.resolve())
    assert 3.4 <= media.duration_s <= 3.8
    assert media.width == 640
    assert media.height == 360
    assert 23.5 <= media.fps <= 24.5
    assert media.video_codec == "h264"
    assert media.has_audio is True
    assert media.audio_channels == 1
    assert media.audio_sample_rate == 16000
    assert media.subtitle_streams == []
    assert media.size_bytes and media.size_bytes > 0
    assert media.duration_label.startswith("00:00:0")


def test_probe_missing_file(tmp_path: Path, ffprobe_bin: str) -> None:
    with pytest.raises(VideoProbeError, match="不存在"):
        probe_media(tmp_path / "missing.mp4", ffprobe_path=ffprobe_bin)


@pytest.mark.ffmpeg
def test_probe_non_video_file(tmp_path: Path, ffprobe_bin: str) -> None:
    target = tmp_path / "not_video.mp4"
    target.write_text("这不是视频文件", encoding="utf-8")
    with pytest.raises(VideoProbeError):
        probe_media(target, ffprobe_path=ffprobe_bin)


def test_probe_reports_missing_ffprobe(tmp_path: Path) -> None:
    target = tmp_path / "x.mp4"
    target.write_bytes(b"0")
    with pytest.raises(VideoProbeError, match="ffprobe"):
        probe_media(target, ffprobe_path="definitely-not-a-real-ffprobe")
