"""使用 ffprobe 读取影片技术信息。"""

from __future__ import annotations

import json
import logging
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..errors import VideoProbeError
from ..models import MediaInfo, SubtitleStream
from ..utils import redact_secrets
from .ffmpeg import CREATE_NO_WINDOW

LOGGER = logging.getLogger(__name__)


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:  # NaN 或非正数
        return None
    return number


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _parse_rate(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        rate = float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return rate if rate > 0 else 0.0


def _parse_tag_duration(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (ValueError, AttributeError):
        return 0.0


def _stream_duration(stream: dict[str, Any]) -> float:
    duration = _to_float(stream.get("duration"))
    if duration:
        return duration
    tags = stream.get("tags") or {}
    duration = _parse_tag_duration(tags.get("DURATION") or tags.get("duration"))
    if duration:
        return duration
    duration_ts = _to_float(stream.get("duration_ts"))
    time_base = stream.get("time_base") or ""
    if duration_ts and time_base:
        try:
            numerator, denominator = time_base.split("/")
            return duration_ts * float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return 0.0
    return 0.0


def probe_media(path: Path | str, ffprobe_path: str = "ffprobe", timeout_s: float = 90.0) -> MediaInfo:
    """读取影片时长、分辨率、帧率、音轨、字幕轨信息。

    对无法读取的文件抛出 :class:`VideoProbeError`，不返回伪造数据。
    """
    source = Path(path).expanduser()
    if not source.exists():
        raise VideoProbeError(f"文件不存在：{source}")
    if not source.is_file():
        raise VideoProbeError(f"不是文件：{source}")
    try:
        if source.stat().st_size == 0:
            raise VideoProbeError("文件大小为 0，可能不是有效的视频文件")
    except OSError as exc:
        raise VideoProbeError(f"无法访问文件：{exc}") from exc

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise VideoProbeError(f"找不到 ffprobe：{ffprobe_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoProbeError(f"ffprobe 读取超时（{timeout_s:.0f} 秒）：{source.name}") from exc
    except OSError as exc:
        raise VideoProbeError(f"无法启动 ffprobe：{exc}") from exc

    if completed.returncode != 0:
        detail = redact_secrets((completed.stderr or "").strip())
        raise VideoProbeError(f"无法解析视频文件（ffprobe 退出码 {completed.returncode}）：{detail[:500]}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoProbeError(f"ffprobe 返回了无法解析的 JSON：{exc}") from exc

    streams: list[dict[str, Any]] = list(payload.get("streams") or [])
    format_info: dict[str, Any] = payload.get("format") or {}

    video_stream = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not int((stream.get("disposition") or {}).get("attached_pic") or 0)
        ),
        None,
    )
    if video_stream is None:
        raise VideoProbeError("文件中没有可用的视频流，请选择包含画面的影片")

    duration = _to_float(format_info.get("duration")) or _stream_duration(video_stream)
    if not duration:
        for stream in streams:
            duration = _stream_duration(stream)
            if duration:
                break
    if not duration:
        raise VideoProbeError("无法读取影片时长，文件可能损坏或格式不受支持")

    width = _to_int(video_stream.get("width")) or 0
    height = _to_int(video_stream.get("height")) or 0
    if width <= 0 or height <= 0:
        raise VideoProbeError("无法读取影片分辨率")

    rotation = 0
    for side_data in video_stream.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = int(_to_float(side_data.get("rotation")) or 0)
            break
    tags = video_stream.get("tags") or {}
    if rotation == 0 and tags.get("rotate"):
        rotation = int(_to_float(tags.get("rotate")) or 0)
    if rotation % 180 != 0:
        width, height = height, width

    fps = _parse_rate(video_stream.get("avg_frame_rate")) or _parse_rate(video_stream.get("r_frame_rate"))
    if fps <= 0:
        raise VideoProbeError("无法读取影片帧率")
    if fps > 240:
        # 个别容器会给出错误的 rate，回退到另一字段
        alternative = _parse_rate(video_stream.get("r_frame_rate"))
        if 0 < alternative <= 240:
            fps = alternative
        else:
            raise VideoProbeError(f"帧率数据异常：{fps:.2f} fps")

    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    subtitle_streams: list[SubtitleStream] = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        subtitle_tags = stream.get("tags") or {}
        subtitle_streams.append(
            SubtitleStream(
                index=_to_int(stream.get("index")) or 0,
                codec=str(stream.get("codec_name") or ""),
                language=subtitle_tags.get("language"),
                title=subtitle_tags.get("title"),
            )
        )

    media = MediaInfo(
        source_path=str(source.resolve()),
        duration_s=round(duration, 3),
        width=width,
        height=height,
        fps=round(fps, 3),
        video_codec=str(video_stream.get("codec_name") or ""),
        pix_fmt=str(video_stream.get("pix_fmt") or ""),
        audio_codec=str(audio_stream.get("codec_name")) if audio_stream else None,
        audio_channels=_to_int(audio_stream.get("channels")) if audio_stream else None,
        audio_sample_rate=_to_int(audio_stream.get("sample_rate")) if audio_stream else None,
        bit_rate=_to_int(format_info.get("bit_rate")),
        size_bytes=_to_int(format_info.get("size")) or source.stat().st_size,
        format_name=str(format_info.get("format_name") or ""),
        has_audio=audio_stream is not None,
        subtitle_streams=subtitle_streams,
        rotation=rotation,
    )
    LOGGER.info(
        "读取影片信息成功：%s | %s | %.2f fps | %s | 音轨=%s | 字幕轨=%d",
        source.name,
        media.resolution_label,
        media.fps,
        media.duration_label,
        media.audio_label,
        len(media.subtitle_streams),
    )
    return media
