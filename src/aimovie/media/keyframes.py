"""镜头关键帧/缩略图提取。

优先使用 OpenCV 随机定位到镜头中点；若 OpenCV 不可用或单帧读取失败，
回退到 ffmpeg。缩略图仅用于界面预览和后续视觉分析候选，不修改源视频。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import KeyframeConfig
from ..models import Shot
from .ffmpeg import extract_thumbnail

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]
CancelFn = Callable[[], None]


@dataclass
class KeyframeResult:
    """缩略图提取结果：镜头序号 -> 图片绝对路径。"""

    keyframes: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.keyframes)


def _write_jpeg(cv2: object, frame: object, target: Path, quality: int) -> None:
    """使用 imencode + write_bytes，兼容含中文/空格的 Windows 路径。"""
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])  # type: ignore[attr-defined]
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    target.write_bytes(buffer.tobytes())


def _resize(cv2: object, frame: object, target_width: int) -> object:
    height, width = frame.shape[:2]  # type: ignore[attr-defined]
    if width <= target_width:
        return frame
    ratio = target_width / float(width)
    target_height = max(2, int(round(height * ratio)))
    return cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)  # type: ignore[attr-defined]


def extract_keyframes(
    video_path: Path | str,
    shots: Sequence[Shot],
    output_dir: Path | str,
    config: KeyframeConfig,
    ffmpeg_path: str = "ffmpeg",
    on_progress: ProgressFn | None = None,
    check_cancel: CancelFn | None = None,
) -> KeyframeResult:
    """为每个镜头抽取中点缩略图。"""
    if not config.enabled or not shots:
        return KeyframeResult()
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("shot_*.jpg"):
        stale.unlink(missing_ok=True)

    selected = list(shots)
    warnings: list[str] = []
    if config.max_shots and len(selected) > config.max_shots:
        warnings.append(
            f"镜头数量 {len(selected)} 超过缩略图上限 {config.max_shots}，仅为前 {config.max_shots} 个镜头生成预览图。"
        )
        selected = selected[: config.max_shots]

    result = KeyframeResult(warnings=warnings)
    total = max(1, len(selected))
    try:
        import cv2  # noqa: PLC0415 - 延迟导入，缺少 OpenCV 时仍可回退
    except Exception as exc:  # noqa: BLE001
        cv2 = None
        warnings.append(f"OpenCV 不可用，改用 ffmpeg 逐帧抽取（速度较慢）：{exc}")
        LOGGER.warning(warnings[-1])

    capture = None
    if cv2 is not None:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            warnings.append("OpenCV 无法打开影片，改用 ffmpeg 抽取缩略图。")
            LOGGER.warning(warnings[-1])
            capture.release()
            capture = None

    try:
        for position, shot in enumerate(selected, start=1):
            if check_cancel is not None:
                check_cancel()
            target = output_dir / f"shot_{shot.index:04d}.jpg"
            midpoint = shot.start_s + shot.duration_s / 2.0
            extracted = False

            if capture is not None and cv2 is not None:
                try:
                    capture.set(cv2.CAP_PROP_POS_MSEC, midpoint * 1000.0)
                    ok, frame = capture.read()
                    if ok and frame is not None and getattr(frame, "size", 0) > 0:
                        frame = _resize(cv2, frame, config.width)
                        _write_jpeg(cv2, frame, target, config.jpeg_quality)
                        extracted = True
                    else:
                        LOGGER.warning("镜头 %d 在 %.2fs 处读取失败，稍后重试", shot.index, midpoint)
                        capture.release()
                        capture = cv2.VideoCapture(str(video_path))
                        capture.set(cv2.CAP_PROP_POS_MSEC, midpoint * 1000.0)
                        ok, frame = capture.read()
                        if ok and frame is not None and getattr(frame, "size", 0) > 0:
                            frame = _resize(cv2, frame, config.width)
                            _write_jpeg(cv2, frame, target, config.jpeg_quality)
                            extracted = True
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("OpenCV 提取镜头 %d 缩略图失败：%s", shot.index, exc)

            if not extracted:
                try:
                    extract_thumbnail(ffmpeg_path, video_path, target, midpoint, width=config.width)
                    extracted = True
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"镜头 {shot.index} 缩略图提取失败：{exc}")
                    LOGGER.warning(warnings[-1])

            if extracted:
                result.keyframes[shot.index] = str(target.resolve())
            if on_progress is not None:
                on_progress(position / total)
    finally:
        if capture is not None:
            capture.release()

    LOGGER.info("缩略图提取完成：%d/%d", result.count, len(selected))
    return result
