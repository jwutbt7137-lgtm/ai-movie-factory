"""第一阶段分析流水线：影片信息 → 镜头切分 → 关键帧 → 本地对白识别 → 时间轴落库。

流水线不依赖 Qt，可在命令行、测试和后台线程中复用。
所有耗时步骤都通过 :class:`TaskControl` 检查暂停/取消。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import AppConfig, resolve_executable
from ..db import ProjectDB
from ..errors import AIMovieError, TaskCancelled
from ..media.keyframes import KeyframeResult, extract_keyframes
from ..media.probe import probe_media
from ..media.scenes import SceneDetectionResult, detect_scenes
from ..models import (
    AnalysisTimeline,
    DialogueSegment,
    MediaInfo,
    ProjectStatus,
    Shot,
)
from ..speech.faster_whisper_engine import FasterWhisperEngine
from .control import TaskControl

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[["ProgressUpdate"], None]
LogFn = Callable[[str], None]
CancelFn = Callable[[], None]


@dataclass(frozen=True)
class ProgressUpdate:
    """界面进度更新。percent 为 0-100。"""

    percent: int
    stage: str
    message: str


@dataclass
class AnalysisResult:
    """一次分析完成后的摘要。"""

    project_id: str
    shot_count: int
    dialogue_count: int
    scene_backend: str
    speech_engine: str
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    timeline_path: str = ""


class TranscriberProtocol(Protocol):
    """语音识别引擎接口，便于测试时替换。"""

    engine_name: str

    def transcribe(
        self,
        video_path: Path | str,
        media: MediaInfo,
        work_dir: Path | str,
        on_progress: Callable[[float], None] | None = None,
        check_cancel: CancelFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> list[DialogueSegment]: ...


class AnalysisPipeline:
    """第一阶段分析流水线。"""

    STAGES = {
        "probe": (1.0, 8.0),
        "scenes": (8.0, 45.0),
        "keyframes": (45.0, 58.0),
        "speech": (58.0, 97.0),
        "finish": (97.0, 100.0),
    }

    def __init__(
        self,
        db: ProjectDB,
        config: AppConfig,
        *,
        probe_fn: Callable[..., MediaInfo] = probe_media,
        scene_fn: Callable[..., SceneDetectionResult] = detect_scenes,
        keyframe_fn: Callable[..., KeyframeResult] = extract_keyframes,
        transcriber_factory: Callable[[], TranscriberProtocol] | None = None,
    ):
        self.db = db
        self.config = config
        self._probe_fn = probe_fn
        self._scene_fn = scene_fn
        self._keyframe_fn = keyframe_fn
        self._transcriber_factory = transcriber_factory

    # ------------------------------------------------------------------ 内部
    def _make_progress_reporter(
        self, on_progress: ProgressFn | None
    ) -> tuple[Callable[[str, float, str], None], Callable[[str, str], None]]:
        last_percent = -1
        current_stage = "准备"

        def emit(stage: str, fraction: float, message: str) -> None:
            nonlocal last_percent, current_stage
            current_stage = stage
            start, end = self._stage_range(stage)
            fraction = max(0.0, min(1.0, fraction))
            percent = int(round(start + (end - start) * fraction))
            if percent <= last_percent and fraction < 1.0:
                return
            percent = max(percent, last_percent)
            last_percent = percent
            if on_progress is not None:
                on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))

        def status(message: str) -> None:
            if on_progress is not None and last_percent >= 0:
                on_progress(
                    ProgressUpdate(percent=last_percent, stage=current_stage, message=message)
                )

        return emit, status

    def _stage_range(self, stage: str) -> tuple[float, float]:
        for key, (start, end) in self.STAGES.items():
            if stage.startswith(key):
                return start, end
        return 0.0, 100.0

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        project_id: str,
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
        skip_speech: bool = False,
    ) -> AnalysisResult:
        """执行完整的第一阶段分析。调用方负责捕获异常并更新界面。"""
        control = control or TaskControl()
        emit, status = self._make_progress_reporter(on_progress)
        started = time.perf_counter()
        warnings: list[str] = []

        def log(message: str) -> None:
            LOGGER.info(message)
            if on_log is not None:
                on_log(message)

        project = self.db.get_project(project_id)
        if project is None:
            raise KeyError(f"项目不存在：{project_id}")
        source = Path(project.source_path)
        if not source.exists():
            message = f"影片文件不存在或已被移动：{source}"
            self.db.update_project(project_id, status=ProjectStatus.FAILED, error=message)
            raise AIMovieError(message)

        run_id = self.db.add_run(project_id, "phase1")
        self.db.update_project(project_id, status=ProjectStatus.ANALYZING, error=None)
        log(f"开始分析项目《{project.name}》：{source.name}")
        shot_count = 0
        dialogue_count = 0
        scene_backend = ""
        speech_engine = "未运行"

        try:
            control.checkpoint()
            emit("probe", 0.0, "读取影片信息…")
            ffmpeg_path = resolve_executable(self.config.ffmpeg_path, "ffmpeg")
            ffprobe_path = resolve_executable(self.config.ffprobe_path, "ffprobe")
            media = self._probe_fn(source, ffprobe_path=ffprobe_path)
            self.db.save_media(project_id, media)
            log(
                f"影片信息：{media.resolution_label} / {media.fps_label} fps / "
                f"{media.duration_label} / {media.audio_label}"
            )
            emit("probe", 1.0, "影片信息读取完成")
            control.checkpoint()

            scene_cfg = self.config.scene
            emit("scenes", 0.0, "正在检测镜头切分…")
            scene_result = self._scene_fn(
                source,
                media.duration_s,
                media.fps,
                scene_cfg,
                ffmpeg_path=ffmpeg_path,
                on_progress=lambda fraction: emit(
                    "scenes", fraction, f"镜头检测中… {min(99, int(fraction * 100))}%"
                ),
                check_cancel=control.checkpoint,
            )
            warnings.extend(scene_result.warnings)
            shots = scene_result.to_shots()
            if not shots:
                raise AIMovieError("镜头检测没有返回结果")
            self.db.replace_shots(project_id, shots)
            shot_count = len(shots)
            scene_backend = scene_result.backend
            log(f"镜头切分完成：{shot_count} 个镜头（后端：{scene_backend}）")
            emit("scenes", 1.0, f"镜头切分完成：{shot_count} 个镜头")
            control.checkpoint()

            emit("keyframes", 0.0, "提取镜头预览图…")
            if self.config.keyframes.enabled:
                keyframe_result = self._keyframe_fn(
                    source,
                    shots,
                    Path(project.project_dir) / "keyframes",
                    self.config.keyframes,
                    ffmpeg_path=ffmpeg_path,
                    on_progress=lambda fraction: emit(
                        "keyframes", fraction, f"提取缩略图… {min(99, int(fraction * 100))}%"
                    ),
                    check_cancel=control.checkpoint,
                )
                warnings.extend(keyframe_result.warnings)
                if keyframe_result.keyframes:
                    for shot in shots:
                        path = keyframe_result.keyframes.get(shot.index)
                        if path:
                            shot.keyframe_path = path
                    self.db.update_shot_keyframes(project_id, keyframe_result.keyframes)
                log(f"缩略图提取完成：{keyframe_result.count} 张")
            else:
                log("已禁用缩略图提取")
            emit("keyframes", 1.0, "预览图处理完成")
            control.checkpoint()

            emit("speech", 0.0, "准备语音识别…")
            if skip_speech:
                dialogues: list[DialogueSegment] = []
                warnings.append("按用户设置跳过语音识别")
                speech_engine = "已跳过"
                log("按设置跳过语音识别")
                emit("speech", 1.0, "已跳过语音识别")
            elif not media.has_audio:
                dialogues = []
                warnings.append("影片没有音轨，未进行对白识别")
                speech_engine = "无音轨"
                log("影片没有音轨，跳过语音识别")
                emit("speech", 1.0, "无音轨，跳过语音识别")
            else:
                transcriber = (
                    self._transcriber_factory()
                    if self._transcriber_factory is not None
                    else FasterWhisperEngine(
                        self.config.whisper,
                        self.config.whisper_download_root(),
                        ffmpeg_path,
                    )
                )
                speech_engine = getattr(transcriber, "engine_name", "unknown")
                dialogues = transcriber.transcribe(
                    source,
                    media,
                    Path(project.project_dir) / "audio",
                    on_progress=lambda fraction: emit(
                        "speech", fraction, f"语音识别中… {min(99, int(fraction * 100))}%"
                    ),
                    check_cancel=control.checkpoint,
                    on_status=status,
                )
                log(f"语音识别完成：{len(dialogues)} 句对白（{speech_engine}）")
            self.db.replace_dialogues(project_id, dialogues)
            dialogue_count = len(dialogues)
            if not dialogues and media.has_audio and not skip_speech:
                warnings.append("语音识别未返回任何对白，请人工核对影片是否以音乐/音效为主")
            emit("speech", 1.0, f"对白识别完成：{dialogue_count} 句")
            control.checkpoint()

            emit("finish", 0.2, "写入时间轴…")
            timeline = self.db.build_timeline(project_id, warnings=warnings)
            timeline.scene_backend = scene_backend
            timeline.speech_engine = speech_engine
            timeline_path = Path(project.project_dir) / "timeline.json"
            timeline.write_json(timeline_path)
            log(f"时间轴已保存：{timeline_path}")

            self.db.update_project(project_id, status=ProjectStatus.READY, error=None)
            elapsed = time.perf_counter() - started
            self.db.finish_run(
                run_id,
                "completed",
                f"镜头 {shot_count}，对白 {dialogue_count}，耗时 {elapsed:.1f}s",
            )
            emit("finish", 1.0, "分析完成")
            log(f"分析完成：{shot_count} 个镜头、{dialogue_count} 句对白，耗时 {elapsed:.1f} 秒")
            return AnalysisResult(
                project_id=project_id,
                shot_count=shot_count,
                dialogue_count=dialogue_count,
                scene_backend=scene_backend,
                speech_engine=speech_engine,
                warnings=warnings,
                elapsed_s=round(elapsed, 2),
                timeline_path=str(timeline_path),
            )
        except TaskCancelled:
            self.db.update_project(project_id, status=ProjectStatus.CANCELLED)
            self.db.finish_run(run_id, "cancelled", "用户取消")
            log("任务已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - 统一记录失败状态
            message = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("分析失败：%s", message)
            self.db.update_project(project_id, status=ProjectStatus.FAILED, error=message[:1000])
            self.db.finish_run(run_id, "failed", message[:1000])
            raise
