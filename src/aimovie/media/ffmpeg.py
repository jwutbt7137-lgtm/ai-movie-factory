"""FFmpeg/ffprobe 调用封装。

所有外部进程都隐藏控制台窗口，并提供一个统一的取消检查点。
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..errors import AudioExtractionError

LOGGER = logging.getLogger(__name__)

#: Windows 下隐藏子进程黑窗
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], None]


def _run_kwargs() -> dict[str, object]:
    return {
        "creationflags": CREATE_NO_WINDOW,
        "stdin": subprocess.DEVNULL,
    }


def check_tool_version(executable: str, timeout: float = 15.0) -> str:
    """运行 ``-version`` 并返回首行，用于设置页自检。"""
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FileNotFoundError(f"无法运行 {executable}: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{executable} 返回错误：{completed.stderr.strip()[:300]}")
    first_line = (completed.stdout or "").splitlines()
    return first_line[0] if first_line else executable


def parse_ffmpeg_time(value: str) -> float:
    """把 ffmpeg 的 ``HH:MM:SS.xxxxxx`` 时间转换为秒。"""
    value = value.strip()
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0


def extract_audio(
    ffmpeg_path: str,
    source: Path | str,
    target: Path | str,
    duration_s: float,
    on_progress: ProgressFn | None = None,
    check_cancel: CancelFn | None = None,
    timeout_s: float | None = None,
) -> Path:
    """提取 16kHz 单声道 PCM 音频，供 faster-whisper 使用。

    使用 ``-progress pipe:1`` 读取转码进度；取消时立即结束 ffmpeg 进程。
    """
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()

    log_path = target.with_suffix(".ffmpeg.log")
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-progress",
        "pipe:1",
        "-nostats",
        str(target),
    ]
    LOGGER.info("提取音轨：%s -> %s", source.name, target.name)
    with log_path.open("w", encoding="utf-8", errors="replace") as stderr_file:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise AudioExtractionError(f"无法启动 ffmpeg：{exc}") from exc

        assert process.stdout is not None
        try:
            for line in process.stdout:
                if check_cancel is not None:
                    check_cancel()
                line = line.strip()
                if line.startswith("out_time="):
                    seconds = parse_ffmpeg_time(line.split("=", 1)[1])
                    if on_progress is not None and duration_s > 0:
                        on_progress(min(1.0, max(0.0, seconds / duration_s)))
            code = process.wait(timeout=timeout_s)
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
        finally:
            process.stdout.close()

    if code != 0:
        detail = ""
        try:
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()[-800:]
        except OSError:
            pass
        raise AudioExtractionError(f"ffmpeg 提取音轨失败（退出码 {code}）：{detail}")
    if not target.exists() or target.stat().st_size == 0:
        raise AudioExtractionError("ffmpeg 未生成有效音频文件，影片可能没有音轨")
    log_path.unlink(missing_ok=True)
    if on_progress is not None:
        on_progress(1.0)
    return target


def probe_audio_duration(ffprobe_path: str, path: Path | str, timeout_s: float = 30.0) -> float:
    """读取音频/视频文件时长（秒）。"""
    completed = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        **_run_kwargs(),
    )
    if completed.returncode != 0:
        raise ValueError(f"ffprobe 读取时长失败：{completed.stderr.strip()[:300]}")
    try:
        duration = float((completed.stdout or "").strip())
    except ValueError as exc:
        raise ValueError("ffprobe 返回的时长不是数字") from exc
    if duration <= 0:
        raise ValueError("音频时长为 0")
    return duration


def decode_audio_to_pcm(
    ffmpeg_path: str,
    path: Path | str,
    *,
    sample_rate: int = 48000,
    timeout_s: float = 120.0,
) -> bytes:
    """把音频解码为单声道 s16le PCM 字节。"""
    completed = subprocess.run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        timeout=timeout_s,
        **_run_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", errors="replace")[:300]
        raise ValueError(f"ffmpeg 解码音频失败：{detail}")
    if not completed.stdout:
        raise ValueError("ffmpeg 没有输出 PCM 数据")
    return completed.stdout


def extract_thumbnail(
    ffmpeg_path: str,
    source: Path | str,
    target: Path | str,
    time_s: float,
    width: int = 320,
    timeout_s: float = 30.0,
) -> Path:
    """用 ffmpeg 抽取单帧缩略图（OpenCV 不可用或读取失败时的回退方案）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, time_s):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "3",
        str(target),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        **_run_kwargs(),
    )
    if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise AudioExtractionError(f"ffmpeg 抽取缩略图失败：{completed.stderr.strip()[:300]}")
    return target
