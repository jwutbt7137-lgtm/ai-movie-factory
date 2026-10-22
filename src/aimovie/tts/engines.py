"""TTS 引擎：Edge 在线语音与 Piper 本地语音。

Edge TTS 免费、无需 Key，但需要网络；Piper 完全本地，需要用户自行放置
中文 ONNX 语音模型。两者都可能在目标机器不可用，因此服务层提供回退。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..config import TTSConfig
from ..errors import TTSError

LOGGER = logging.getLogger(__name__)


class TTSEngineProtocol(Protocol):
    name: str
    voice_label: str
    cache_suffix: str

    def available(self) -> bool: ...

    def synthesize(self, text: str, output_path: Path) -> None: ...


class EdgeTTSEngine:
    """Microsoft Edge 在线 TTS（edge-tts）。"""

    name = "edge"
    cache_suffix = ".mp3"

    def __init__(self, config: TTSConfig):
        self.config = config
        self.voice_label = config.edge_voice
        self._sleeper: Callable[[float], None] = time.sleep

    def available(self) -> bool:
        return importlib.util.find_spec("edge_tts") is not None

    def synthesize(self, text: str, output_path: Path) -> None:
        if not self.available():
            raise TTSError("未安装 edge-tts，请运行 pip install edge-tts")
        import edge_tts  # noqa: PLC0415

        output_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = max(1, int(self.config.edge_retries) + 1)
        last_error = ""
        for attempt in range(attempts):
            if attempt:
                self._sleeper(min(8.0, 1.0 * (2 ** (attempt - 1))))
            try:
                output_path.unlink(missing_ok=True)

                async def _run() -> None:
                    communicate = edge_tts.Communicate(
                        text,
                        self.config.edge_voice,
                        rate=f"{self.config.rate_percent:+d}%",
                        volume=f"{self.config.volume_percent:+d}%",
                        pitch=f"{self.config.pitch_hz:+d}Hz",
                    )
                    await asyncio.wait_for(
                        communicate.save(str(output_path)),
                        timeout=float(self.config.edge_timeout_s),
                    )

                asyncio.run(_run())
                if not output_path.exists() or output_path.stat().st_size < 500:
                    raise TTSError("Edge TTS 未生成有效音频")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                LOGGER.warning("Edge TTS 合成失败（%d/%d）：%s", attempt + 1, attempts, last_error)
        output_path.unlink(missing_ok=True)
        raise TTSError(f"Edge TTS 合成失败：{last_error[:300]}")


class PiperTTSEngine:
    """本地 Piper ONNX 语音。"""

    name = "piper"
    cache_suffix = ".wav"

    def __init__(self, config: TTSConfig):
        self.config = config
        self.model_path = Path(config.piper_model_path).expanduser() if config.piper_model_path else None
        self.voice_label = self.model_path.stem if self.model_path else "Piper 本地语音"
        self._voice = None

    def available(self) -> bool:
        return (
            self.model_path is not None
            and self.model_path.is_file()
            and importlib.util.find_spec("piper") is not None
        )

    def _load(self):
        if self._voice is not None:
            return self._voice
        try:
            from piper import PiperVoice  # noqa: PLC0415
        except ImportError as exc:
            raise TTSError("未安装 piper-tts，无法使用本地语音") from exc
        if self.model_path is None:
            raise TTSError("未配置 Piper 模型路径")
        try:
            self._voice = PiperVoice.load(str(self.model_path))
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"加载 Piper 模型失败：{exc}") from exc
        return self._voice

    def synthesize(self, text: str, output_path: Path) -> None:
        if not self.available():
            raise TTSError("Piper 语音模型不可用；请在设置中配置 .onnx 模型路径")
        from piper import SynthesisConfig  # noqa: PLC0415

        voice = self._load()
        length_scale = 1.0 / max(0.5, 1.0 + self.config.rate_percent / 100.0)
        volume = max(0.1, 1.0 + self.config.volume_percent / 100.0)
        config = SynthesisConfig(length_scale=length_scale, volume=volume)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with wave.open(str(output_path), "wb") as wav_file:
                voice.synthesize_wav(text, wav_file, syn_config=config)
        except Exception as exc:  # noqa: BLE001
            output_path.unlink(missing_ok=True)
            raise TTSError(f"Piper 合成失败：{exc}") from exc
        if not output_path.exists() or output_path.stat().st_size < 500:
            output_path.unlink(missing_ok=True)
            raise TTSError("Piper 未生成有效音频")
