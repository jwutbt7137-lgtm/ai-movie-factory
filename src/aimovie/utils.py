"""通用工具函数。"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

#: 常见可导入的视频扩展名；ffprobe 无法读取时会给出明确错误
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".m4v",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".flv",
    ".wmv",
    ".3gp",
}


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。"""
    from datetime import timezone

    return datetime.now(timezone.utc)


def format_hhmmss(seconds: float) -> str:
    """00:00:00 格式。"""
    total = int(round(max(0.0, float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_bytes(size: int | None) -> str:
    """人类可读的文件大小。"""
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "—"


def truncate(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def is_supported_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def open_in_explorer(path: Path | str, select: bool = False) -> None:
    """用资源管理器打开文件/目录；仅 Windows 有效。"""
    target = Path(path)
    if os.name != "nt":
        raise RuntimeError("仅支持在 Windows 上打开资源管理器")
    if select and target.is_file():
        subprocess.Popen(["explorer", "/select,", str(target)])
    else:
        os.startfile(str(target))  # type: ignore[attr-defined]


def open_with_default_app(path: Path | str) -> None:
    """调用系统默认程序打开文件（例如影片）。"""
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在：{target}")
    if os.name != "nt":
        raise RuntimeError("仅支持在 Windows 上调用默认程序")
    os.startfile(str(target))  # type: ignore[attr-defined]


_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]{8,})", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[=:]\s*)([A-Za-z0-9._\-]{8,})", re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    """把疑似 API Key 替换为 ***，用于日志与界面展示。"""
    redacted = text
    for index, pattern in enumerate(_SECRET_PATTERNS):
        if index == 0:
            redacted = pattern.sub("sk-***REDACTED***", redacted)
        elif index == 1:
            redacted = pattern.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
        else:
            redacted = pattern.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    return redacted
