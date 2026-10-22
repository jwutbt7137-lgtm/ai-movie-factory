"""镜头切分测试（PySceneDetect 与 FFmpeg 回退）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.config import SceneConfig
from aimovie.errors import SceneDetectionError
from aimovie.media.scenes import SceneDetectionResult, detect_scenes


def _assert_boundaries(result: SceneDetectionResult, duration: float) -> None:
    assert result.boundaries, "至少应检测到一个镜头"
    assert result.boundaries[0].start_s == 0.0
    assert result.boundaries[-1].end_s == pytest.approx(duration, abs=0.1)
    assert all(boundary.duration_s > 0 for boundary in result.boundaries)
    for previous, current in zip(result.boundaries, result.boundaries[1:]):
        assert current.start_s == pytest.approx(previous.end_s, abs=0.05)


@pytest.mark.ffmpeg
def test_pyscenedetect_detects_color_cuts(sample_video: Path, sample_media) -> None:
    progress: list[float] = []
    config = SceneConfig(backend="pyscenedetect", threshold=27.0, min_scene_len_frames=8)
    result = detect_scenes(
        sample_video,
        sample_media.duration_s,
        sample_media.fps,
        config,
        on_progress=progress.append,
    )
    assert result.backend == "pyscenedetect"
    assert len(result.boundaries) >= 3
    _assert_boundaries(result, sample_media.duration_s)
    cuts = [boundary.start_s for boundary in result.boundaries[1:]]
    assert any(abs(cut - 1.2) < 0.3 for cut in cuts)
    assert any(abs(cut - 2.4) < 0.3 for cut in cuts)
    assert progress and progress[-1] == pytest.approx(1.0)
    shots = result.to_shots()
    assert [shot.index for shot in shots] == list(range(1, len(shots) + 1))


@pytest.mark.ffmpeg
def test_ffmpeg_fallback_detects_cuts(sample_video: Path, sample_media, ffmpeg_bin: str) -> None:
    config = SceneConfig(
        backend="ffmpeg",
        ffmpeg_threshold=0.2,
        min_scene_len_frames=8,
        analyze_width=320,
    )
    result = detect_scenes(
        sample_video,
        sample_media.duration_s,
        sample_media.fps,
        config,
        ffmpeg_path=ffmpeg_bin,
    )
    assert result.backend == "ffmpeg"
    assert len(result.boundaries) >= 3
    _assert_boundaries(result, sample_media.duration_s)
    cuts = [boundary.start_s for boundary in result.boundaries[1:]]
    assert any(abs(cut - 1.2) < 0.4 for cut in cuts)


@pytest.mark.ffmpeg
def test_auto_backend(sample_video: Path, sample_media, ffmpeg_bin: str) -> None:
    config = SceneConfig(backend="auto", threshold=27.0, min_scene_len_frames=8)
    result = detect_scenes(
        sample_video,
        sample_media.duration_s,
        sample_media.fps,
        config,
        ffmpeg_path=ffmpeg_bin,
    )
    assert result.backend in {"pyscenedetect", "ffmpeg"}


def test_invalid_duration_raises() -> None:
    with pytest.raises(SceneDetectionError):
        detect_scenes("whatever.mp4", 0, 24, SceneConfig())
