"""faster-whisper 中文对白识别。

完全在本地运行：影片音轨只写入本项目的项目目录，不会上传到任何第三方。
模型在首次使用时下载（可在设置中选择 tiny/base/small/medium/large-v3）。
"""

from __future__ import annotations

import logging
import wave
from collections.abc import Callable
from pathlib import Path

from ..config import WhisperConfig
from ..errors import TranscriptionError
from ..media.ffmpeg import extract_audio
from ..models import DialogueSegment, MediaInfo

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], None]
StatusFn = Callable[[str], None]


class FasterWhisperEngine:
    """faster-whisper 引擎封装。"""

    def __init__(
        self,
        config: WhisperConfig,
        download_root: Path,
        ffmpeg_path: str = "ffmpeg",
    ):
        self.config = config
        self.download_root = Path(download_root)
        self.ffmpeg_path = ffmpeg_path
        self._model = None  # 延迟到真正转写时加载

    @property
    def engine_name(self) -> str:
        return f"faster-whisper/{self.config.model_size}/{self.config.compute_type}"

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # 延迟导入，界面启动不依赖模型
        except ImportError as exc:
            raise TranscriptionError(
                "未安装 faster-whisper，请运行 pip install faster-whisper"
            ) from exc
        LOGGER.info(
            "加载 Whisper 模型：%s（device=%s, compute_type=%s）",
            self.config.model_size,
            self.config.device,
            self.config.compute_type,
        )
        try:
            self._model = WhisperModel(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type,
                download_root=str(self.download_root),
                cpu_threads=self.config.cpu_threads,
                num_workers=1,
            )
        except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不稳定
            raise TranscriptionError(
                f"加载 Whisper 模型失败：{exc}。首次使用需要联网下载模型；"
                "若使用 CUDA，请在设置中确认显卡与 compute_type 配置。"
            ) from exc
        return self._model

    def transcribe(
        self,
        video_path: Path | str,
        media: MediaInfo,
        work_dir: Path | str,
        on_progress: ProgressFn | None = None,
        check_cancel: CancelFn | None = None,
        on_status: StatusFn | None = None,
    ) -> list[DialogueSegment]:
        """识别影片对白，返回带起止时间的中文句子。"""
        if not media.has_audio:
            LOGGER.warning("影片没有音轨，跳过语音识别")
            return []
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        audio_path = work_dir / "audio_16k.wav"

        def report(value: float) -> None:
            if on_progress is not None:
                on_progress(max(0.0, min(1.0, value)))

        def status(message: str) -> None:
            LOGGER.info(message)
            if on_status is not None:
                on_status(message)

        # 音频提取可复用上次结果，避免重复解码长片
        source = Path(video_path)
        reuse = False
        if audio_path.exists() and audio_path.stat().st_size > 1024:
            try:
                reuse = audio_path.stat().st_mtime >= source.stat().st_mtime
            except OSError:
                reuse = False
        if reuse:
            status("复用已提取的 16kHz 音轨")
            report(0.08)
        else:
            status("提取影片音轨（16kHz 单声道）")
            extract_audio(
                self.ffmpeg_path,
                source,
                audio_path,
                media.duration_s,
                on_progress=lambda fraction: report(fraction * 0.08),
                check_cancel=check_cancel,
            )
        if check_cancel is not None:
            check_cancel()

        status(
            f"加载语音模型 {self.config.model_size}（首次使用需下载模型，请耐心等待）"
        )
        model = self._load_model()
        if check_cancel is not None:
            check_cancel()

        status("开始识别对白")
        transcribe_kwargs: dict[str, object] = {
            "language": self.config.language or None,
            "beam_size": self.config.beam_size,
            "vad_filter": self.config.vad_filter,
            "condition_on_previous_text": False,
            "word_timestamps": False,
        }
        if self.config.initial_prompt.strip():
            transcribe_kwargs["initial_prompt"] = self.config.initial_prompt.strip()

        # 直接传入 16kHz float32 数组，避免部分 PyAV 版本与 faster-whisper 的
        # metadata_errors 参数不兼容，同时减少一次容器解析。
        audio_input: object = str(audio_path)
        try:
            import numpy as np  # noqa: PLC0415

            with wave.open(str(audio_path), "rb") as handle:
                if (
                    handle.getnchannels() == 1
                    and handle.getsampwidth() == 2
                    and handle.getframerate() == 16000
                ):
                    raw = handle.readframes(handle.getnframes())
                    audio_input = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                    LOGGER.debug("语音识别使用内存 PCM 数组：%d 个采样点", audio_input.size)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("读取 16kHz PCM 失败，回退到文件路径输入：%s", exc)

        try:
            segments_iterator, info = model.transcribe(audio_input, **transcribe_kwargs)  # type: ignore[attr-defined]
            total_duration = float(getattr(info, "duration", 0.0) or media.duration_s)
            language = str(getattr(info, "language", "") or self.config.language or "")
            detected_duration = total_duration
            results: list[DialogueSegment] = []
            for segment in segments_iterator:
                if check_cancel is not None:
                    check_cancel()
                text = " ".join(str(segment.text or "").split())
                if not text:
                    continue
                start = max(0.0, float(segment.start))
                end = max(start + 0.01, float(segment.end))
                results.append(
                    DialogueSegment(
                        index=len(results) + 1,
                        start_s=round(start, 3),
                        end_s=round(end, 3),
                        text=text,
                        language=language,
                        avg_logprob=(
                            float(segment.avg_logprob)
                            if getattr(segment, "avg_logprob", None) is not None
                            else None
                        ),
                        no_speech_prob=(
                            float(segment.no_speech_prob)
                            if getattr(segment, "no_speech_prob", None) is not None
                            else None
                        ),
                    )
                )
                if detected_duration > 0:
                    report(0.1 + 0.89 * min(1.0, end / detected_duration))
        except Exception as exc:  # noqa: BLE001
            from ..errors import TaskCancelled

            if isinstance(exc, TaskCancelled):
                raise
            raise TranscriptionError(f"语音识别失败：{exc}") from exc

        report(1.0)
        if not results:
            LOGGER.warning("语音识别未返回任何对白，影片可能以音乐/音效为主")
        else:
            LOGGER.info("语音识别完成：%d 句对白，语言=%s", len(results), language or "未知")
        return results
