"""后台工作线程：影片探测与分析流水线，避免阻塞界面。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..errors import BudgetExceeded, TaskCancelled
from ..media.probe import probe_media
from ..models import MediaInfo
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import AnalysisPipeline, AnalysisResult, ProgressUpdate
from ..utils import redact_secrets

LOGGER = logging.getLogger(__name__)


class MediaProbeWorker(QThread):
    """读取影片信息（导入时使用）。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, source: Path, ffprobe_path: str, parent: object | None = None):
        super().__init__(parent)  # type: ignore[arg-type]
        self.source = Path(source)
        self.ffprobe_path = ffprobe_path

    def run(self) -> None:  # noqa: D401
        try:
            media: MediaInfo = probe_media(self.source, ffprobe_path=self.ffprobe_path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取影片信息失败")
            self.failed.emit(redact_secrets(f"{type(exc).__name__}: {exc}"))
        else:
            self.succeeded.emit(media)


class AnalysisWorker(QThread):
    """执行第一阶段分析流水线。"""

    progress = Signal(object)
    logged = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        pipeline: AnalysisPipeline,
        project_id: str,
        control: TaskControl,
        skip_speech: bool = False,
        parent: object | None = None,
    ):
        super().__init__(parent)  # type: ignore[arg-type]
        self.pipeline = pipeline
        self.project_id = project_id
        self.control = control
        self.skip_speech = skip_speech

    def run(self) -> None:  # noqa: D401
        try:
            result: AnalysisResult = self.pipeline.run(
                self.project_id,
                control=self.control,
                on_progress=self._emit_progress,
                on_log=self.logged.emit,
                skip_speech=self.skip_speech,
            )
        except TaskCancelled:
            LOGGER.info("分析任务已取消")
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("分析任务失败")
            self.failed.emit(redact_secrets(f"{type(exc).__name__}: {exc}"))
        else:
            self.succeeded.emit(result)

    def _emit_progress(self, update: ProgressUpdate) -> None:
        self.progress.emit(update)


class AITaskWorker(QThread):
    """通用 AI 任务线程：剧情分析或文案生成。"""

    progress = Signal(object)
    logged = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        runner: Callable[[TaskControl, Callable[[object], None], Callable[[str], None]], object],
        control: TaskControl,
        parent: object | None = None,
    ):
        super().__init__(parent)  # type: ignore[arg-type]
        self.runner = runner
        self.control = control

    def run(self) -> None:  # noqa: D401
        try:
            result = self.runner(self.control, self.progress.emit, self.logged.emit)
        except TaskCancelled:
            LOGGER.info("AI 任务已取消")
            self.cancelled.emit()
        except BudgetExceeded as exc:
            LOGGER.warning("AI 任务被预算拦截：%s", exc)
            self.failed.emit(f"预算拦截：{exc}")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("AI 任务失败")
            self.failed.emit(redact_secrets(f"{type(exc).__name__}: {exc}"))
        else:
            self.succeeded.emit(result)
