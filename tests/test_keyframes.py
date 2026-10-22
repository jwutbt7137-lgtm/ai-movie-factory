"""镜头缩略图提取测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.config import KeyframeConfig
from aimovie.media.keyframes import extract_keyframes
from aimovie.models import Shot


@pytest.mark.ffmpeg
def test_extract_keyframes(sample_video: Path, ffmpeg_bin: str, tmp_path: Path) -> None:
    shots = [
        Shot(index=1, start_s=0.0, end_s=1.2),
        Shot(index=2, start_s=1.2, end_s=2.4),
        Shot(index=3, start_s=2.4, end_s=3.6),
    ]
    config = KeyframeConfig(width=160, jpeg_quality=80)
    result = extract_keyframes(sample_video, shots, tmp_path / "keyframes", config, ffmpeg_path=ffmpeg_bin)
    assert result.count == 3
    for index in (1, 2, 3):
        path = Path(result.keyframes[index])
        assert path.exists()
        assert path.stat().st_size > 500
        assert path.read_bytes()[:2] == b"\xff\xd8"

    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        cv2 = None
    if cv2 is not None:
        image = cv2.imread(str(result.keyframes[1]))
        assert image is not None
        assert image.shape[1] <= 160
        assert image.shape[0] > 0


@pytest.mark.ffmpeg
def test_keyframe_limit(sample_video: Path, ffmpeg_bin: str, tmp_path: Path) -> None:
    shots = [Shot(index=index, start_s=(index - 1) * 0.8, end_s=index * 0.8) for index in range(1, 5)]
    config = KeyframeConfig(width=120, max_shots=2)
    result = extract_keyframes(sample_video, shots, tmp_path / "keyframes", config, ffmpeg_path=ffmpeg_bin)
    assert result.count == 2
    assert any("上限" in warning for warning in result.warnings)


def test_keyframes_disabled(tmp_path: Path) -> None:
    config = KeyframeConfig(enabled=False)
    result = extract_keyframes("missing.mp4", [Shot(index=1, start_s=0, end_s=1)], tmp_path / "kf", config)
    assert result.count == 0
