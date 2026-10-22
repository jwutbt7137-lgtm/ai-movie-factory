"""按对白证据把影片切成可分析的剧情片段。

不调用模型，纯本地计算；测试可直接验证分段的边界、字符数与镜头映射。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StoryConfig
from ..errors import StoryAnalysisError
from ..models import DialogueSegment, Shot, format_clock


@dataclass(frozen=True)
class StoryChunk:
    """一段送去做剧情分析的对白与镜头上下文。"""

    index: int
    start_s: float
    end_s: float
    dialogues: tuple[DialogueSegment, ...]
    shots: tuple[Shot, ...]

    @property
    def dialogue_ids(self) -> list[int]:
        return [segment.index for segment in self.dialogues]

    @property
    def shot_ids(self) -> list[int]:
        return [shot.index for shot in self.shots]

    @property
    def char_count(self) -> int:
        return sum(len(segment.text) for segment in self.dialogues)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def time_label(self) -> str:
        return f"{format_clock(self.start_s)} – {format_clock(self.end_s)}"


def _shots_in_range(shots: list[Shot], start_s: float, end_s: float) -> tuple[Shot, ...]:
    """返回与时间段有交集的镜头；没有交集时取最近镜头作为上下文。"""
    overlapping = [
        shot for shot in shots if shot.end_s > start_s + 0.01 and shot.start_s < end_s - 0.01
    ]
    if overlapping:
        return tuple(overlapping)
    nearest: Shot | None = None
    best_distance = float("inf")
    for shot in shots:
        if shot.end_s <= start_s:
            distance = start_s - shot.end_s
        elif shot.start_s >= end_s:
            distance = shot.start_s - end_s
        else:
            distance = 0.0
        if distance < best_distance:
            best_distance = distance
            nearest = shot
    return (nearest,) if nearest is not None else ()


def _make_chunk(
    index: int,
    dialogues: list[DialogueSegment],
    shots: list[Shot],
) -> StoryChunk:
    start_s = min(segment.start_s for segment in dialogues)
    end_s = max(segment.end_s for segment in dialogues)
    return StoryChunk(
        index=index,
        start_s=round(start_s, 3),
        end_s=round(end_s, 3),
        dialogues=tuple(dialogues),
        shots=_shots_in_range(shots, start_s, end_s),
    )


def build_story_chunks(
    dialogues: list[DialogueSegment],
    shots: list[Shot],
    config: StoryConfig,
) -> list[StoryChunk]:
    """按字数、时长和长静默间隙切分剧情片段。

    规则：
    - 每段对白字符数达到 ``chunk_target_chars`` 后，在下一句前切分；
    - 单段覆盖时长不超过 ``chunk_max_duration_s``；
    - 对白之间超过 90 秒静默时切分（通常意味着场景/时间跳变）；
    - 过短的尾段合并回上一段，避免证据不足的碎片。
    """
    usable = sorted(
        [segment for segment in dialogues if segment.text.strip()],
        key=lambda segment: (segment.start_s, segment.index),
    )
    if not usable:
        raise StoryAnalysisError("没有可用于剧情分析的对白；请先完成语音识别并核对结果。")

    chunks: list[StoryChunk] = []
    current: list[DialogueSegment] = []
    current_chars = 0
    current_start = usable[0].start_s
    gap_limit = 90.0

    def flush() -> None:
        nonlocal current, current_chars, current_start
        if current:
            chunks.append(_make_chunk(len(chunks) + 1, current, shots))
            current = []
            current_chars = 0

    for segment in usable:
        if current:
            current_end = current[-1].end_s
            would_exceed_duration = (
                segment.end_s - current_start > config.chunk_max_duration_s
            )
            long_gap = segment.start_s - current_end > gap_limit
            if current_chars >= config.chunk_target_chars or would_exceed_duration or long_gap:
                flush()
                current_start = segment.start_s
        current.append(segment)
        current_chars += len(segment.text)
    flush()

    # 尾段过短则并回上一段，避免“最后 3 句单独分析”导致噪声
    if len(chunks) >= 2:
        last = chunks[-1]
        previous = chunks[-2]
        short_threshold = max(80, config.chunk_target_chars // 6)
        if last.char_count < short_threshold:
            merged_dialogues = list(previous.dialogues) + list(last.dialogues)
            merged = _make_chunk(previous.index, merged_dialogues, shots)
            if (
                merged.char_count <= config.chunk_target_chars * 1.4
                and merged.duration_s <= config.chunk_max_duration_s * 1.2
            ):
                chunks = chunks[:-2] + [merged]

    # 重新编号，保证连续
    chunks = [
        StoryChunk(
            index=index,
            start_s=chunk.start_s,
            end_s=chunk.end_s,
            dialogues=chunk.dialogues,
            shots=chunk.shots,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    if len(chunks) > config.max_segments:
        raise StoryAnalysisError(
            f"对白被切分为 {len(chunks)} 段，超过上限 {config.max_segments}。"
            "请在设置中增大每段字符数或调高分段上限。"
        )
    return chunks
