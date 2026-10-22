"""把 logging 记录转发到 Qt 界面（线程安全）。"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from ..logging_setup import RedactingFormatter


class LogEmitter(QObject):
    """信号中转对象，必须在主线程创建。"""

    message = Signal(str)


class QtLogHandler(logging.Handler):
    """日志处理器：格式化并脱敏后通过信号发送到界面。"""

    def __init__(self, emitter: LogEmitter):
        super().__init__(level=logging.NOTSET)
        self.emitter = emitter
        self.setFormatter(
            RedactingFormatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        try:
            self.emitter.message.emit(text)
        except RuntimeError:
            # 窗口关闭后信号对象可能已销毁
            pass
