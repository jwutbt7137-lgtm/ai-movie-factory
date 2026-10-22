"""逐句合成配音、生成句子级时间轴并混合完整音轨。"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
import wave
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from ..config import AppConfig, TTSConfig, resolve_executable
from ..db import ProjectDB
from ..errors import TTSError, TaskCancelled
from ..media.ffmpeg import decode_audio_to_pcm, probe_audio_duration
from ..models import Script, SpeechUnit, Voiceover, format_clock
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import ProgressUpdate
from ..utils import utc_now
from .engines import EdgeTTSEngine, PiperTTSEngine, TTSEngineProtocol
from .sentences import ScriptSentence, build_script_sentences

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[ProgressUpdate], None]
LogFn = Callable[[str], None]


class TTSService:
    """把当前文案转换为逐句音频和完整配音轨。"""

    def __init__(
        self,
        db: ProjectDB,
        config: AppConfig,
        *,
        engines: dict[str, TTSEngineProtocol] | None = None,
        tts_config: TTSConfig | None = None,
    ):
        self.db = db
        self.config = config
        self.tts = tts_config or config.tts
        self._engines = engines
        self._ffmpeg = resolve_executable(config.ffmpeg_path, "ffmpeg")
        self._ffprobe = resolve_executable(config.ffprobe_path, "ffprobe")

    # ------------------------------------------------------------------ 公共
    def synthesize(
        self,
        project_id: str,
        script_id: str | None = None,
        *,
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
    ) -> Voiceover:
        control = control or TaskControl()
        project = self.db.get_project(project_id)
        if project is None:
            raise TTSError(f"项目不存在：{project_id}")
        script = self._load_script(project_id, script_id)
        if not script.paragraphs:
            raise TTSError("当前文案为空，无法生成配音。")
        sentences = build_script_sentences(script, max_chars=self.tts.max_sentence_chars)
        if not sentences:
            raise TTSError("文案没有可合成的句子。")

        engines = self._engine_order()
        if not engines:
            raise TTSError(
                "没有可用的配音引擎。请安装/联网使用 Edge TTS，"
                "或在设置中配置本地 Piper 中文模型后重试。"
            )
        active_index = 0
        warnings: list[str] = []
        if self.tts.engine != "auto" and engines[0].name != self.tts.engine:
            warnings.append(f"首选 {self.tts.engine} 不可用，已改用 {engines[0].name}。")
        if not engines[0].available():
            if len(engines) > 1:
                warnings.append(f"{engines[0].name} 不可用，已改用 {engines[1].name}。")
                active_index = 1
            else:
                raise TTSError(f"{engines[0].name} 配音引擎不可用")

        root = Path(project.project_dir) / "voiceover"
        cache_dir = root / "cache"
        run_dir = root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        cache_dir.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        run_id = self.db.add_run(project_id, "tts")
        self._emit(on_progress, 2, "准备", f"准备合成 {len(sentences)} 句配音")

        units: list[SpeechUnit] = []
        pauses_ms: list[int] = []
        cursor = 0.0
        reused = 0
        fallback_noted = False
        try:
            for position, sentence in enumerate(sentences, start=1):
                control.checkpoint()
                unit, engine_index, reused_cache = self._synthesize_sentence(
                    sentence,
                    engines,
                    active_index,
                    cache_dir,
                    warnings,
                    on_log,
                    control,
                )
                if engine_index != active_index:
                    active_index = engine_index
                if not fallback_noted and engine_index > 0:
                    warnings.append(
                        f"第 {sentence.index} 句起切换到备用引擎 {engines[engine_index].name}。"
                    )
                    fallback_noted = True
                reused += int(reused_cache)
                duration = self._audio_duration(Path(unit.audio_path), engines[engine_index].cache_suffix)
                start = round(cursor, 3)
                end = round(start + duration, 3)
                pause = self._pause_after(sentences, position)
                unit = unit.model_copy(
                    update={
                        "start_s": start,
                        "end_s": end,
                        "duration_s": round(end - start, 3),
                        "engine": engines[engine_index].name,
                    }
                )
                units.append(unit)
                if pause:
                    pauses_ms.append(pause)
                    cursor = end + pause / 1000.0
                else:
                    pauses_ms.append(0)
                    cursor = end
                percent = int(4 + 80 * position / len(sentences))
                self._emit(
                    on_progress,
                    percent,
                    "合成语音",
                    f"已完成 {position}/{len(sentences)} 句",
                )
                if on_log is not None:
                    on_log(
                        f"配音 {position}/{len(sentences)}：{format_clock(start)}–"
                        f"{format_clock(end)}（{engines[engine_index].name}）"
                    )

            control.checkpoint()
            self._emit(on_progress, 88, "混音", "拼接逐句音频并写入完整配音轨")
            full_audio = run_dir / "voiceover.wav"
            total_duration = self._mix_units(units, pauses_ms, full_audio)
            control.checkpoint()

            used_engines = sorted({unit.engine for unit in units})
            engine_label = used_engines[0] if len(used_engines) == 1 else "mixed"
            labels = [engine.voice_label for engine in engines if engine.name in used_engines]
            voice_labels = " / ".join(dict.fromkeys(labels))
            if self.tts.pitch_hz and "piper" in used_engines:
                warnings.append("Piper 本地语音不支持音高调整，pitch 设置仅对 Edge 生效。")
            voiceover = Voiceover(
                project_id=project_id,
                script_id=script.id,
                script_version=script.version,
                engine=engine_label,
                voice=voice_labels,
                rate_percent=self.tts.rate_percent,
                volume_percent=self.tts.volume_percent,
                pitch_hz=self.tts.pitch_hz,
                sentence_pause_ms=self.tts.sentence_pause_ms,
                paragraph_pause_ms=self.tts.paragraph_pause_ms,
                created_at=utc_now(),
                audio_path=str(full_audio.resolve()),
                total_duration_s=round(total_duration, 3),
                units=units,
                warnings=warnings,
                cached=reused == len(units),
                elapsed_s=round(time.perf_counter() - started, 2),
                needs_review=True,
            )
            self._emit(on_progress, 96, "保存", "保存配音与句子时间轴")
            self.db.save_voiceover(voiceover)
            self.db.finish_run(
                run_id,
                "completed",
                f"配音 {len(units)} 句，{format_clock(total_duration)}，引擎 {engine_label}",
            )
            self._emit(on_progress, 100, "完成", f"配音完成：{format_clock(total_duration)}")
            LOGGER.info(
                "配音完成：project=%s script=v%d units=%d duration=%.1fs engine=%s cached=%s",
                project_id,
                script.version,
                len(units),
                total_duration,
                engine_label,
                voiceover.cached,
            )
            return voiceover
        except TaskCancelled:
            self.db.finish_run(run_id, "cancelled", "用户取消配音")
            raise
        except Exception as exc:  # noqa: BLE001
            self.db.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}"[:1000])
            LOGGER.exception("配音失败")
            raise

    # ------------------------------------------------------------------ 引擎
    def _engine_order(self) -> list[TTSEngineProtocol]:
        if self._engines is not None:
            order = []
            if "edge" in self._engines:
                order.append(self._engines["edge"])
            if "piper" in self._engines:
                order.append(self._engines["piper"])
            for name, engine in self._engines.items():
                if engine not in order:
                    order.append(engine)
            return [engine for engine in order if engine.available()]
        piper = PiperTTSEngine(self.tts)
        edge = EdgeTTSEngine(self.tts)
        if self.tts.engine == "piper":
            order = [piper, edge]
        elif self.tts.engine == "edge":
            order = [edge, piper]
        else:
            order = [piper, edge] if piper.available() else [edge, piper]
        if not self.tts.fallback_enabled:
            order = order[:1]
        return [engine for engine in order if engine.available()]

    def _load_script(self, project_id: str, script_id: str | None) -> Script:
        script = self.db.get_script(script_id) if script_id else self.db.get_current_script(project_id)
        if script is None:
            raise TTSError("项目还没有解说文案，请先生成文案。")
        if script.project_id != project_id:
            raise TTSError("指定文案不属于当前项目。")
        return script

    def _synthesize_sentence(
        self,
        sentence: ScriptSentence,
        engines: list[TTSEngineProtocol],
        active_index: int,
        cache_dir: Path,
        warnings: list[str],
        on_log: LogFn | None,
        control: TaskControl,
    ) -> tuple[SpeechUnit, int, bool]:
        if sentence.needs_review:
            # 文案本身的问题不应阻断配音，但传递到配音结果供后续审核
            pass
        last_error = ""
        for index in range(active_index, len(engines)):
            engine = engines[index]
            cache_key = self._cache_key(engine, sentence.text)
            audio_path = cache_dir / f"{cache_key}{engine.cache_suffix}"
            reused = bool(
                self.tts.cache_enabled
                and audio_path.exists()
                and audio_path.stat().st_size >= 500
            )
            if not reused:
                try:
                    engine.synthesize(sentence.text, audio_path)
                except TTSError as exc:
                    last_error = str(exc)
                    LOGGER.warning("%s 合成第 %d 句失败：%s", engine.name, sentence.index, last_error)
                    if index < len(engines) - 1:
                        warnings.append(f"{engine.name} 失败，尝试备用引擎。")
                        continue
                    raise TTSError(
                        f"第 {sentence.index} 句配音失败：{last_error[:300]}"
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    if index < len(engines) - 1:
                        warnings.append(f"{engine.name} 失败，尝试备用引擎。")
                        continue
                    raise TTSError(f"第 {sentence.index} 句配音失败：{exc}") from exc
            unit = SpeechUnit(
                index=sentence.index,
                paragraph_index=sentence.paragraph_index,
                text=sentence.text,
                start_s=0.0,
                end_s=1.0,
                duration_s=1.0,
                audio_path=str(audio_path.resolve()),
                engine=engine.name,
                segment_ids=list(sentence.segment_ids),
                evidence_dialogue_ids=list(sentence.evidence_dialogue_ids),
                shot_ids=list(sentence.shot_ids),
                purpose=sentence.purpose,
                needs_review=sentence.needs_review,
            )
            return unit, index, reused
        raise TTSError(f"第 {sentence.index} 句没有可用配音引擎：{last_error}")

    def _cache_key(self, engine: TTSEngineProtocol, text: str) -> str:
        material = "|".join(
            [
                engine.name,
                engine.voice_label,
                str(self.tts.rate_percent),
                str(self.tts.volume_percent),
                str(self.tts.pitch_hz),
                text,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _pause_after(self, sentences: list[ScriptSentence], position: int) -> int:
        if position >= len(sentences):
            return 0
        if sentences[position].paragraph_index != sentences[position - 1].paragraph_index:
            return self.tts.paragraph_pause_ms
        return self.tts.sentence_pause_ms

    def _audio_duration(self, path: Path, suffix: str) -> float:
        if suffix == ".wav":
            try:
                with wave.open(str(path), "rb") as handle:
                    rate = handle.getframerate()
                    frames = handle.getnframes()
                    if rate > 0 and frames > 0:
                        return frames / float(rate)
            except wave.Error:
                pass
        try:
            return probe_audio_duration(self._ffprobe, path)
        except Exception as exc:  # noqa: BLE001
            raise TTSError(f"无法读取配音时长：{path.name}: {exc}") from exc

    def _mix_units(self, units: list[SpeechUnit], pauses_ms: list[int], target: Path) -> float:
        sample_rate = int(self.tts.sample_rate)
        chunks: list[np.ndarray] = []
        for unit, pause in zip(units, pauses_ms):
            pcm = decode_audio_to_pcm(
                self._ffmpeg, unit.audio_path, sample_rate=sample_rate
            )
            chunks.append(np.frombuffer(pcm, dtype=np.int16))
            if pause:
                chunks.append(np.zeros(int(sample_rate * pause / 1000), dtype=np.int16))
        if not chunks:
            raise TTSError("没有可混音的配音片段")
        merged = np.concatenate(chunks)
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(merged.tobytes())
        return float(len(merged)) / sample_rate

    @staticmethod
    def _emit(on_progress: ProgressFn | None, percent: int, stage: str, message: str) -> None:
        if on_progress is not None:
            on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))
