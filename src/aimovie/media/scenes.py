"""镜头（场景）切分。

主后端为 PySceneDetect ContentDetector；若不可用或失败，自动回退到
FFmpeg 的 ``scene`` 分数滤镜。本模块不修改源视频，只输出时间边界。
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import SceneConfig
from ..errors import SceneDetectionError, TaskCancelled
from ..models import Shot
from .ffmpeg import CREATE_NO_WINDOW, parse_ffmpeg_time

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], None]

_PTS_TIME_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _timecode_frames(timecode: object) -> int:
    """兼容 PySceneDetect 0.6/0.7 的帧号读取（0.7 使用 frame_num 属性）。"""
    if isinstance(timecode, int):
        return timecode
    frame_num = getattr(timecode, "frame_num", None)
    if frame_num is not None:
        return int(frame_num)
    return int(timecode.get_frames())  # type: ignore[attr-defined]


def _timecode_seconds(timecode: object) -> float:
    """兼容 PySceneDetect 0.6/0.7 的秒数读取（0.7 使用 seconds 属性）。"""
    if isinstance(timecode, (int, float)):
        return float(timecode)
    seconds = getattr(timecode, "seconds", None)
    if seconds is not None:
        return float(seconds)
    return float(timecode.get_seconds())  # type: ignore[attr-defined]


@dataclass(frozen=True)
class SceneBoundary:
    """一个镜头的时间边界（秒）。"""

    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class SceneDetectionResult:
    """镜头检测结果及实际使用的后端。"""

    boundaries: list[SceneBoundary]
    backend: str
    warnings: list[str] = field(default_factory=list)

    def to_shots(self) -> list[Shot]:
        return [
            Shot(index=index, start_s=round(boundary.start_s, 3), end_s=round(boundary.end_s, 3))
            for index, boundary in enumerate(self.boundaries, start=1)
        ]


def _make_boundaries(cuts: list[float], duration_s: float) -> list[SceneBoundary]:
    """由剪切点生成连续、覆盖全片的镜头边界。"""
    usable = sorted({round(cut, 3) for cut in cuts if 0.1 < cut < duration_s - 0.1})
    edges = [0.0, *usable, duration_s]
    boundaries: list[SceneBoundary] = []
    for start, end in zip(edges, edges[1:]):
        if end - start < 0.08:
            continue
        boundaries.append(SceneBoundary(start_s=round(start, 3), end_s=round(end, 3)))
    return boundaries


def _detect_with_pyscenedetect(
    path: Path,
    duration_s: float,
    config: SceneConfig,
    on_progress: ProgressFn | None,
    check_cancel: CancelFn | None,
) -> list[SceneBoundary]:
    from scenedetect import ContentDetector, SceneManager, open_video  # 延迟导入，便于回退

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(
            threshold=config.threshold,
            min_scene_len=config.min_scene_len_frames,
        )
    )
    try:
        total_frames = max(1, _timecode_frames(video.duration))
    except Exception:
        total_frames = max(1, int(duration_s * 24))

    last_reported = -1

    def callback(_frame: object, current_time: object) -> None:
        nonlocal last_reported
        if check_cancel is not None:
            check_cancel()
        try:
            frame_number = _timecode_frames(current_time)
        except (AttributeError, TypeError):
            frame_number = 0
        if on_progress is not None and frame_number // 24 != last_reported:
            last_reported = frame_number // 24
            on_progress(min(0.99, frame_number / total_frames))

    LOGGER.info("开始 PySceneDetect 切分镜头：%s", path.name)
    try:
        manager.detect_scenes(video, show_progress=False, callback=callback)
    except TypeError as exc:
        # 兼容不同版本的 detect_scenes 签名；必须重新打开视频，否则已消费的帧流无法重放
        LOGGER.warning("PySceneDetect 进度回调不可用（%s），重新执行无进度检测", exc)
        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(
            ContentDetector(threshold=config.threshold, min_scene_len=config.min_scene_len_frames)
        )
        manager.detect_scenes(video, show_progress=False)
    scene_list = manager.get_scene_list(start_in_scene=True)
    LOGGER.debug("PySceneDetect 原始场景列表：%s", scene_list)

    boundaries: list[SceneBoundary] = []
    for start_time, end_time in scene_list:
        start_s = _timecode_seconds(start_time)
        end_s = _timecode_seconds(end_time)
        if end_s - start_s >= 0.08:
            boundaries.append(SceneBoundary(start_s=round(start_s, 3), end_s=round(end_s, 3)))
    if not boundaries:
        boundaries = [SceneBoundary(0.0, round(duration_s, 3))]
    else:
        boundaries[0] = SceneBoundary(0.0, boundaries[0].end_s)
        boundaries[-1] = SceneBoundary(boundaries[-1].start_s, round(duration_s, 3))
        # 过滤取整后长度过短的镜头；相邻镜头保持独立，不能把连续场景合并掉
        boundaries = [boundary for boundary in boundaries if boundary.duration_s >= 0.08]
    if on_progress is not None:
        on_progress(1.0)
    LOGGER.info("PySceneDetect 完成：检测到 %d 个镜头", len(boundaries))
    return boundaries


def _detect_with_ffmpeg(
    path: Path,
    duration_s: float,
    fps: float,
    config: SceneConfig,
    ffmpeg_path: str,
    on_progress: ProgressFn | None,
    check_cancel: CancelFn | None,
) -> list[SceneBoundary]:
    scale_filter = f"scale={config.analyze_width}:-2," if config.analyze_width else ""
    video_filter = f"{scale_filter}select='gt(scene,{config.ffmpeg_threshold})',showinfo"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-fps_mode",
        "vfr",
        "-f",
        "null",
        "-",
    ]
    LOGGER.info("使用 FFmpeg 回退方案切分镜头：%s", path.name)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SceneDetectionError(f"无法启动 ffmpeg 镜头检测：{exc}") from exc

    cuts: list[float] = []
    detected_duration = duration_s
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if check_cancel is not None:
                check_cancel()
            pts_match = _PTS_TIME_RE.search(line)
            if pts_match:
                cuts.append(float(pts_match.group(1)))
            duration_match = _DURATION_RE.search(line)
            if duration_match and detected_duration <= 0:
                hours, minutes, seconds = duration_match.groups()
                detected_duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            if line.startswith("out_time=") and on_progress is not None and detected_duration > 0:
                current = parse_ffmpeg_time(line.split("=", 1)[1])
                on_progress(min(0.99, current / detected_duration))
        code = process.wait(timeout=30)
    except BaseException:
        process.kill()
        process.wait(timeout=10)
        raise
    finally:
        process.stdout.close()

    if code != 0 and not cuts:
        raise SceneDetectionError(f"ffmpeg 镜头检测失败（退出码 {code}）")
    min_gap = max(0.2, config.min_scene_len_frames / max(fps, 1.0))
    filtered: list[float] = []
    for cut in sorted(cuts):
        if not filtered or cut - filtered[-1] >= min_gap:
            filtered.append(cut)
    boundaries = _make_boundaries(filtered, detected_duration)
    if not boundaries:
        boundaries = [SceneBoundary(0.0, round(detected_duration, 3))]
    if on_progress is not None:
        on_progress(1.0)
    LOGGER.info("FFmpeg 镜头检测完成：检测到 %d 个镜头", len(boundaries))
    return boundaries


def detect_scenes(
    path: Path | str,
    duration_s: float,
    fps: float,
    config: SceneConfig,
    ffmpeg_path: str = "ffmpeg",
    on_progress: ProgressFn | None = None,
    check_cancel: CancelFn | None = None,
) -> SceneDetectionResult:
    """检测镜头边界。

    ``config.backend == "auto"`` 时优先 PySceneDetect，失败则回退 FFmpeg。
    """
    if duration_s <= 0:
        raise SceneDetectionError("影片时长无效，无法进行镜头切分")
    path = Path(path)
    warnings: list[str] = []
    backends = (
        ["pyscenedetect", "ffmpeg"] if config.backend == "auto" else [config.backend]
    )
    for backend in backends:
        if check_cancel is not None:
            check_cancel()
        try:
            if backend == "pyscenedetect":
                boundaries = _detect_with_pyscenedetect(path, duration_s, config, on_progress, check_cancel)
            else:
                boundaries = _detect_with_ffmpeg(
                    path, duration_s, fps, config, ffmpeg_path, on_progress, check_cancel
                )
            if not boundaries:
                raise SceneDetectionError("没有检测到任何镜头")
            return SceneDetectionResult(boundaries=boundaries, backend=backend, warnings=warnings)
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 回退需要捕获第三方库的多种异常
            if config.backend != "auto":
                raise SceneDetectionError(f"{backend} 镜头检测失败：{exc}") from exc
            message = f"{backend} 镜头检测失败，尝试下一个方案：{exc}"
            LOGGER.warning(message)
            warnings.append(message)
    raise SceneDetectionError("所有镜头检测方案均失败：" + "；".join(warnings))
