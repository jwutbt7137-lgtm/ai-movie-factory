"""日志配置：滚动文件 + 控制台，并对 API Key 做脱敏。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .utils import redact_secrets

LOG_FILE_NAME = "app.log"
_MANAGED_MARKER = "_aimovie_managed_handler"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    """最终输出前统一脱敏，防止异常信息或请求头里的 Key 进入日志。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def log_file_path(log_dir: Path) -> Path:
    return Path(log_dir) / LOG_FILE_NAME


def setup_logging(log_dir: Path, level: str | int = "INFO", console: bool = True) -> Path:
    """初始化根日志器，重复调用不会叠加处理器。返回日志文件路径。"""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_file_path(log_dir)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    numeric_level = logging.getLevelName(str(level).upper()) if isinstance(level, str) else int(level)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    root.setLevel(numeric_level)

    formatter = RedactingFormatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, _MANAGED_MARKER, True)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        try:
            stream_handler = logging.StreamHandler(sys.stderr)
            stream_handler.setFormatter(formatter)
            setattr(stream_handler, _MANAGED_MARKER, True)
            root.addHandler(stream_handler)
        except Exception:  # pragma: no cover - 极少数无 stderr 的环境（如 pythonw）
            pass

    logging.captureWarnings(True)
    return path
