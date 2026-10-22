"""真实 faster-whisper 转写集成测试。

默认跳过：需要下载模型且耗时较长。运行方式：

    $env:AIMF_RUN_INTEGRATION=1
    .venv\\Scripts\\python.exe -m pytest tests\\test_speech_integration.py -v -s
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")

from aimovie.config import WhisperConfig  # noqa: E402
from aimovie.models import MediaInfo  # noqa: E402
from aimovie.speech.faster_whisper_engine import FasterWhisperEngine  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        os.environ.get("AIMF_RUN_INTEGRATION") != "1",
        reason="设置 AIMF_RUN_INTEGRATION=1 才运行真实语音识别",
    ),
]


def test_faster_whisper_transcribes_chinese(sample_speech_wav: Path, ffmpeg_bin: str, tmp_path: Path) -> None:
    with wave.open(str(sample_speech_wav), "rb") as handle:
        duration = handle.getnframes() / float(handle.getframerate())
        channels = handle.getnchannels()
        rate = handle.getframerate()
    media = MediaInfo(
        source_path=str(sample_speech_wav),
        duration_s=duration,
        width=640,
        height=360,
        fps=25.0,
        audio_codec="pcm_s16le",
        audio_channels=channels,
        audio_sample_rate=rate,
        has_audio=True,
    )
    model_root = Path(os.environ.get("AIMF_MODEL_DIR", str(tmp_path / "models")))
    config = WhisperConfig(
        model_size="tiny",
        device="cpu",
        compute_type="int8",
        language="zh",
        beam_size=1,
        vad_filter=False,
        initial_prompt="以下是普通话电影对白。",
    )
    progress: list[float] = []
    engine = FasterWhisperEngine(config, model_root, ffmpeg_bin)
    segments = engine.transcribe(
        sample_speech_wav,
        media,
        tmp_path / "work",
        on_progress=progress.append,
        on_status=lambda message: print(message),
    )
    text = "".join(segment.text for segment in segments)
    assert segments, "Whisper 应至少识别出一句对白"
    assert any("\u4e00" <= character <= "\u9fff" for character in text), f"识别文本应包含中文：{text}"
    assert progress and progress[-1] == pytest.approx(1.0)
    assert all(segment.end_s > segment.start_s for segment in segments)
