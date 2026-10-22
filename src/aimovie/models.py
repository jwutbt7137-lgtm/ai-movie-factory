"""核心数据结构（Pydantic）。

模块之间只通过这里定义的数据结构通信，便于测试、序列化和后续阶段扩展。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStatus(StrEnum):
    """项目状态。"""

    IMPORTED = "imported"
    ANALYZING = "analyzing"
    PAUSED = "paused"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


STATUS_LABELS: dict[str, str] = {
    ProjectStatus.IMPORTED: "已导入",
    ProjectStatus.ANALYZING: "分析中",
    ProjectStatus.PAUSED: "已暂停",
    ProjectStatus.READY: "分析完成",
    ProjectStatus.FAILED: "失败",
    ProjectStatus.CANCELLED: "已取消",
}


class SubtitleStream(BaseModel):
    """内嵌字幕轨信息（第一阶段只读取元数据，不提取内容）。"""

    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    codec: str = ""
    language: str | None = None
    title: str | None = None


class MediaInfo(BaseModel):
    """影片技术信息，由 ffprobe 读取。"""

    model_config = ConfigDict(extra="ignore")

    source_path: str
    duration_s: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    video_codec: str = ""
    pix_fmt: str = ""
    audio_codec: str | None = None
    audio_channels: int | None = Field(default=None, ge=1)
    audio_sample_rate: int | None = Field(default=None, ge=8000)
    bit_rate: int | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)
    format_name: str = ""
    has_audio: bool = False
    subtitle_streams: list[SubtitleStream] = Field(default_factory=list)
    rotation: int = 0

    @field_validator("source_path")
    @classmethod
    def _path_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("影片路径不能为空")
        return value

    @property
    def resolution_label(self) -> str:
        return f"{self.width}×{self.height}"

    @property
    def duration_label(self) -> str:
        total = int(self.duration_s + 0.5)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def audio_label(self) -> str:
        if not self.has_audio:
            return "无音轨"
        codec = self.audio_codec or "未知编码"
        channels = f"{self.audio_channels}声道" if self.audio_channels else ""
        rate = f"{self.audio_sample_rate / 1000:.1f}kHz" if self.audio_sample_rate else ""
        return " / ".join(part for part in (codec, channels, rate) if part)

    @property
    def fps_label(self) -> str:
        return f"{self.fps:.3f}".rstrip("0").rstrip(".")


class Shot(BaseModel):
    """一个镜头（场景）。时间单位：秒。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    index: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    keyframe_path: str | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> "Shot":
        if self.end_s <= self.start_s + 0.001:
            raise ValueError(f"镜头 {self.index} 的结束时间必须大于开始时间")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def time_range_label(self) -> str:
        return f"{self.start_s:.2f}s – {self.end_s:.2f}s"


class ShotTimeline(BaseModel):
    """镜头序列，要求按时间升序。"""

    model_config = ConfigDict(extra="ignore")

    shots: list[Shot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _chronological(self) -> "ShotTimeline":
        previous_end = -1.0
        for shot in self.shots:
            if shot.start_s < previous_end - 0.02:
                raise ValueError(f"镜头顺序错误：第 {shot.index} 个镜头与前一个镜头重叠")
            previous_end = shot.end_s
        return self

    def __len__(self) -> int:
        return len(self.shots)

    @property
    def total_duration_s(self) -> float:
        return sum(shot.duration_s for shot in self.shots)

    def shot_at(self, time_s: float) -> Shot | None:
        """返回指定时间点所在镜头。"""
        for shot in self.shots:
            if shot.start_s <= time_s < shot.end_s:
                return shot
        return None


class DialogueSegment(BaseModel):
    """一句（或一小段）对白，由本地语音识别产生。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    index: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    text: str = Field(min_length=1)
    language: str = ""
    avg_logprob: float | None = None
    no_speech_prob: float | None = None

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("对白文本不能为空")
        return value

    @model_validator(mode="after")
    def _end_after_start(self) -> "DialogueSegment":
        if self.end_s <= self.start_s:
            raise ValueError(f"对白 {self.index} 的结束时间必须大于开始时间")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def time_label(self) -> str:
        return f"{format_clock(self.start_s)} → {format_clock(self.end_s)}"


class DialogueTimeline(BaseModel):
    """对白序列。"""

    model_config = ConfigDict(extra="ignore")

    segments: list[DialogueSegment] = Field(default_factory=list)
    language: str = ""
    engine: str = ""

    @model_validator(mode="after")
    def _chronological(self) -> "DialogueTimeline":
        previous_start = -1.0
        for segment in self.segments:
            if segment.start_s < previous_start - 0.02:
                raise ValueError(f"对白顺序错误：第 {segment.index} 句早于前一句")
            previous_start = segment.start_s
        return self

    def __len__(self) -> int:
        return len(self.segments)


class ProjectRecord(BaseModel):
    """项目记录（列表页展示用，不包含完整时间轴）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: str
    name: str
    source_path: str
    project_dir: str
    status: ProjectStatus = ProjectStatus.IMPORTED
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    shot_count: int = 0
    dialogue_count: int = 0
    error: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, str(self.status))

    @property
    def duration_label(self) -> str:
        if not self.duration_s:
            return "—"
        total = int(self.duration_s + 0.5)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def resolution_label(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"


class AnalysisTimeline(BaseModel):
    """分析结果快照，用于导出 JSON 和后续阶段读取。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    project_id: str
    project_name: str
    source_path: str
    media: MediaInfo
    shots: list[Shot] = Field(default_factory=list)
    dialogues: list[DialogueSegment] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=_utc_now)
    scene_backend: str = ""
    speech_engine: str = ""
    warnings: list[str] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path


class EvidenceClaim(BaseModel):
    """带对白证据的断言；没有证据的内容必须标记待核实。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    description: str = Field(min_length=1)
    evidence_dialogue_ids: list[int] = Field(default_factory=list)
    needs_review: bool = False


class PlotEvent(EvidenceClaim):
    """剧情事件。"""

    importance: Literal["low", "medium", "high"] = "medium"


class Conflict(EvidenceClaim):
    """冲突/矛盾。"""


class TurningPoint(EvidenceClaim):
    """重要转折。segment_index 指向分段分析中的片段。"""

    segment_index: int | None = Field(default=None, ge=1)


class SegmentCharacter(BaseModel):
    """分段分析中的人物（名称可能来自对白推断）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    name: str = Field(min_length=1)
    description: str = ""
    evidence_dialogue_ids: list[int] = Field(default_factory=list)
    needs_review: bool = False


class StorySegment(BaseModel):
    """一个剧情分段的分析结果。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    index: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    summary: str = ""
    events: list[PlotEvent] = Field(default_factory=list)
    characters: list[SegmentCharacter] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    turning_points: list[TurningPoint] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    dialogue_ids: list[int] = Field(default_factory=list)
    shot_ids: list[int] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_review: bool = False
    model: str = ""

    @model_validator(mode="after")
    def _time_order(self) -> "StorySegment":
        if self.end_s <= self.start_s + 0.001:
            raise ValueError(f"剧情分段 {self.index} 的结束时间必须大于开始时间")
        return self

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    @property
    def time_label(self) -> str:
        return f"{format_clock(self.start_s)} – {format_clock(self.end_s)}"


class CharacterRelation(BaseModel):
    """人物关系。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    target: str = Field(min_length=1)
    relation: str = ""
    evidence_segment_ids: list[int] = Field(default_factory=list)
    needs_review: bool = False


class StoryCharacter(BaseModel):
    """整片人物档案。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    name: str = Field(min_length=1)
    description: str = ""
    role: str = ""
    relations: list[CharacterRelation] = Field(default_factory=list)
    evidence_segment_ids: list[int] = Field(default_factory=list)
    needs_review: bool = False


class StoryAnalysis(BaseModel):
    """整片剧情分析结果（第二阶段）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: int = 2
    project_id: str
    project_name: str
    created_at: datetime = Field(default_factory=_utc_now)
    timeline_generated_at: datetime | None = None
    model: str = ""
    title: str = ""
    logline: str = ""
    overall_summary: str = ""
    main_conflict: str = ""
    themes: list[str] = Field(default_factory=list)
    segments: list[StorySegment] = Field(default_factory=list)
    characters: list[StoryCharacter] = Field(default_factory=list)
    turning_points: list[TurningPoint] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    visual_analysis: str = "disabled"
    estimated_cost_cny: float = Field(default=0.0, ge=0)
    api_calls: int = Field(default=0, ge=0)
    cached_calls: int = Field(default=0, ge=0)
    needs_review: bool = True

    @field_validator("created_at", "timeline_generated_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _segment_references(self) -> "StoryAnalysis":
        indexes: set[int] = set()
        for segment in self.segments:
            if segment.index in indexes:
                raise ValueError(f"剧情分段序号重复：{segment.index}")
            indexes.add(segment.index)
        for point in self.turning_points:
            if point.segment_index is not None and point.segment_index not in indexes:
                raise ValueError(f"转折点引用了不存在的分段：{point.segment_index}")
        return self

    @property
    def total_duration_s(self) -> float:
        return sum(segment.duration_s for segment in self.segments)

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def dialogue_evidence_count(self) -> int:
        return sum(len(segment.dialogue_ids) for segment in self.segments)

    def segment_by_index(self, index: int) -> StorySegment | None:
        for segment in self.segments:
            if segment.index == index:
                return segment
        return None

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class ScriptParagraph(BaseModel):
    """解说文案的一个段落，关联剧情片段与候选镜头。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    index: int = Field(ge=1)
    text: str = Field(min_length=1)
    segment_ids: list[int] = Field(default_factory=list)
    evidence_dialogue_ids: list[int] = Field(default_factory=list)
    shot_ids: list[int] = Field(default_factory=list)
    #: 从关联剧情片段中带出的事件描述，便于第三阶段做语义镜头匹配
    event_descriptions: list[str] = Field(default_factory=list)
    purpose: str = ""
    needs_review: bool = False

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("文案段落不能为空")
        return value

    @property
    def char_count(self) -> int:
        return len(self.text)


class Script(BaseModel):
    """一次生成的解说文案版本。版本内容不可变，修改会创建新版本。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: int = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    #: 0 表示尚未写入数据库，由数据库分配递增版本号
    version: int = Field(default=0, ge=0)
    style_id: str = ""
    style_name: str = ""
    title: str = ""
    target_minutes: float = Field(default=3.0, gt=0, le=60)
    target_chars: int = Field(default=0, ge=0)
    paragraphs: list[ScriptParagraph] = Field(default_factory=list)
    model: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    source: Literal["ai", "manual_edit"] = "ai"
    analysis_created_at: datetime | None = None
    needs_review: bool = True
    review_notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cost_cny: float = Field(default=0.0, ge=0)
    cached: bool = False

    @field_validator("created_at", "analysis_created_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _paragraph_order(self) -> "Script":
        indexes = [paragraph.index for paragraph in self.paragraphs]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("文案段落序号必须从 1 连续递增")
        return self

    @property
    def full_text(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)

    @property
    def character_count(self) -> int:
        return sum(paragraph.char_count for paragraph in self.paragraphs)

    @property
    def version_label(self) -> str:
        return f"v{self.version}" if self.version else "未保存"


class ScriptSummary(BaseModel):
    """文案版本列表项（不加载完整正文）。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    project_id: str
    version: int
    style_id: str = ""
    style_name: str = ""
    title: str = ""
    total_chars: int = 0
    source: str = "ai"
    is_current: bool = False
    needs_review: bool = True
    cost_cny: float = 0
    created_at: datetime = Field(default_factory=_utc_now)

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def label(self) -> str:
        title = self.title or "未命名文案"
        return f"v{self.version} · {self.style_name or self.style_id} · {title}"


class SpeechUnit(BaseModel):
    """配音中的一句话及其时间轴。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    index: int = Field(ge=1)
    paragraph_index: int = Field(ge=1)
    text: str = Field(min_length=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    audio_path: str
    engine: str = ""
    segment_ids: list[int] = Field(default_factory=list)
    evidence_dialogue_ids: list[int] = Field(default_factory=list)
    shot_ids: list[int] = Field(default_factory=list)
    purpose: str = ""
    needs_review: bool = False

    @model_validator(mode="after")
    def _time_order(self) -> "SpeechUnit":
        if self.end_s <= self.start_s:
            raise ValueError(f"语音句 {self.index} 的结束时间必须大于开始时间")
        if abs((self.end_s - self.start_s) - self.duration_s) > 0.1:
            raise ValueError(f"语音句 {self.index} 的时长字段与起止时间不一致")
        return self

    @property
    def time_label(self) -> str:
        return f"{format_clock(self.start_s)} → {format_clock(self.end_s)}"


class Voiceover(BaseModel):
    """一次完整配音结果（按句时间轴 + 合成音频）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: int = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    script_id: str
    script_version: int = 0
    engine: str = ""
    voice: str = ""
    rate_percent: int = 0
    volume_percent: int = 0
    pitch_hz: int = 0
    sentence_pause_ms: int = 0
    paragraph_pause_ms: int = 0
    created_at: datetime = Field(default_factory=_utc_now)
    audio_path: str = ""
    total_duration_s: float = Field(default=0.0, ge=0)
    units: list[SpeechUnit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cached: bool = False
    elapsed_s: float = Field(default=0.0, ge=0)
    needs_review: bool = True

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _unit_order(self) -> "Voiceover":
        previous_end = -1.0
        for unit in self.units:
            if unit.start_s < previous_end - 0.01:
                raise ValueError("配音句子时间轴顺序错误")
            previous_end = unit.end_s
        return self

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def duration_label(self) -> str:
        return format_clock(self.total_duration_s)


class ShotSelection(BaseModel):
    """一句文案对应的镜头选择结果。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    sentence_index: int = Field(ge=1)
    paragraph_index: int = Field(ge=1)
    text: str = ""
    target_start_s: float = Field(ge=0)
    target_end_s: float = Field(gt=0)
    target_duration_s: float = Field(gt=0)
    shot_ids: list[int] = Field(default_factory=list)
    shot_durations_s: list[float] = Field(default_factory=list)
    segment_ids: list[int] = Field(default_factory=list)
    coverage_s: float = Field(default=0.0, ge=0)
    confidence: float = Field(default=0.0, ge=0, le=1)
    needs_review: bool = False
    manual: bool = False
    reason: str = ""

    @property
    def coverage_ratio(self) -> float:
        return min(1.0, self.coverage_s / self.target_duration_s) if self.target_duration_s else 0.0

    @property
    def time_label(self) -> str:
        return f"{format_clock(self.target_start_s)} → {format_clock(self.target_end_s)}"


class ShotMatchPlan(BaseModel):
    """整篇文案的镜头匹配方案。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: int = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    script_id: str
    script_version: int = 0
    voiceover_id: str = ""
    created_at: datetime = Field(default_factory=_utc_now)
    selections: list[ShotSelection] = Field(default_factory=list)
    total_duration_s: float = Field(default=0.0, ge=0)
    average_confidence: float = Field(default=0.0, ge=0, le=1)
    low_confidence_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    needs_review: bool = True
    elapsed_s: float = Field(default=0.0, ge=0)

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _selections_order(self) -> "ShotMatchPlan":
        previous = -1
        for selection in self.selections:
            if selection.sentence_index <= previous:
                raise ValueError("镜头匹配的句子顺序错误")
            previous = selection.sentence_index
        return self

    @property
    def selection_count(self) -> int:
        return len(self.selections)


class ExportResult(BaseModel):
    """一次成片导出结果。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    schema_version: int = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    project_id: str
    plan_id: str
    voiceover_id: str
    created_at: datetime = Field(default_factory=_utc_now)
    output_path: str
    srt_path: str = ""
    ass_path: str = ""
    width: int = 0
    height: int = 0
    fps: int = 0
    duration_s: float = Field(default=0.0, ge=0)
    size_bytes: int = Field(default=0, ge=0)
    subtitle_mode: str = "burn"
    bgm_used: bool = False
    elapsed_s: float = Field(default=0.0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def duration_label(self) -> str:
        return format_clock(self.duration_s)

    @property
    def size_label(self) -> str:
        if self.size_bytes >= 1024 ** 3:
            return f"{self.size_bytes / (1024 ** 3):.2f} GB"
        if self.size_bytes >= 1024 ** 2:
            return f"{self.size_bytes / (1024 ** 2):.1f} MB"
        return f"{self.size_bytes / 1024:.0f} KB"


class ApiCallRecord(BaseModel):
    """一次 DeepSeek 请求（含缓存命中与失败）的账目。"""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    project_id: str = ""
    purpose: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float = 0.0
    cached: bool = False
    status: str = "success"
    started_at: datetime = Field(default_factory=_utc_now)
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error: str = ""
    request_hash: str = ""
    tokens_estimated: bool = False

    @field_validator("started_at", "finished_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class CostSummary(BaseModel):
    """项目或全局费用统计。"""

    project_id: str | None = None
    budget_cny: float = 0.0
    spent_cny: float = 0.0
    calls: int = 0
    cached_calls: int = 0
    failed_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def remaining_cny(self) -> float | None:
        if self.budget_cny <= 0:
            return None
        return max(0.0, self.budget_cny - self.spent_cny)

    @property
    def over_budget(self) -> bool:
        return self.budget_cny > 0 and self.spent_cny > self.budget_cny + 1e-9

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_calls / self.calls if self.calls else 0.0


def format_clock(seconds: float) -> str:
    """秒数格式化为 mm:ss 或 hh:mm:ss。"""
    seconds = max(0.0, float(seconds))
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def load_timeline_json(path: Path | str) -> AnalysisTimeline:
    """读取分析快照 JSON。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return AnalysisTimeline.model_validate(data)
