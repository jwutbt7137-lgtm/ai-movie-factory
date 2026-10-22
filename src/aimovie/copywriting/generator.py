"""解说文案生成、手动编辑与版本管理。"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..config import AppConfig
from ..db import ProjectDB
from ..errors import LLMFormatError, ScriptGenerationError, TaskCancelled
from ..llm.client import DeepSeekClient, LLMResult
from ..llm.parsing import parse_json_object
from ..models import Script, ScriptParagraph, ScriptSummary, StoryAnalysis
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import ProgressUpdate
from ..utils import utc_now
from .prompts import build_script_messages
from .styles import NarrationStyle, get_style

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[ProgressUpdate], None]
LogFn = Callable[[str], None]


class ChatClientProtocol(Protocol):
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        max_tokens: int,
        temperature: float = 0.3,
    ) -> LLMResult: ...


@dataclass
class ScriptRunResult:
    """一次文案生成的统计。"""

    script: Script
    elapsed_s: float
    cost_cny: float
    cached: bool
    api_calls: int = 1


def _as_str(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def split_editable_text(text: str) -> list[str]:
    """把编辑器里的全文按空行拆成段落；没有空行时按单行拆分。"""
    stripped = (text or "").strip()
    if not stripped:
        return []
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", stripped) if chunk.strip()]
    if len(chunks) <= 1:
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        if len(lines) > 1:
            return lines
    return chunks


class ScriptGenerator:
    """基于剧情分析生成/编辑解说文案。"""

    def __init__(
        self,
        db: ProjectDB,
        config: AppConfig,
        *,
        client: ChatClientProtocol | None = None,
    ):
        self.db = db
        self.config = config
        self._client = client

    # ------------------------------------------------------------------ 生成
    def run(
        self,
        project_id: str,
        style_id: str,
        *,
        minutes: float | None = None,
        custom_name: str = "",
        custom_instruction: str = "",
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
        allow_over_budget: bool = False,
    ) -> ScriptRunResult:
        control = control or TaskControl()
        started = time.perf_counter()
        project = self.db.get_project(project_id)
        if project is None:
            raise ScriptGenerationError(f"项目不存在：{project_id}")
        analysis = self.db.get_story_analysis(project_id)
        if analysis is None:
            raise ScriptGenerationError("项目还没有剧情分析结果，请先运行“剧情分析”。")
        style, style_name, instruction, minutes_value = self._resolve_style(
            style_id, minutes, custom_name, custom_instruction
        )
        target_chars = max(100, int(round(minutes_value * self.config.copywriting.chars_per_minute)))
        min_chars = int(target_chars * self.config.copywriting.min_chars_ratio)
        max_chars = int(target_chars * self.config.copywriting.max_chars_ratio)
        max_tokens = max(800, min(8000, int(minutes_value * self.config.copywriting.max_tokens_per_minute)))
        messages = build_script_messages(
            analysis,
            style_name=style_name,
            instruction=instruction,
            minutes=minutes_value,
            target_chars=target_chars,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        self._emit(on_progress, 5, "准备", f"准备生成《{style_name}》文案（约 {minutes_value:g} 分钟）")
        self._log(on_log, f"文案风格：{style_name}；目标 {target_chars} 字")

        control.checkpoint()
        client = self._client
        if client is None:
            client = DeepSeekClient(
                self.config.deepseek,
                self.db,
                project_id,
                allow_over_budget=allow_over_budget,
            )
        self._emit(on_progress, 20, "生成文案", "正在调用模型生成解说文案…")
        result = client.chat(
            messages,
            purpose=f"script_{style.id}",
            max_tokens=max_tokens,
            temperature=style.temperature,
        )
        control.checkpoint()
        self._emit(on_progress, 72, "校验文案", "校验段落证据与镜头关联…")
        try:
            payload = parse_json_object(result.content)
        except LLMFormatError as exc:
            raise ScriptGenerationError(f"模型返回的文案不是合法 JSON：{exc}") from exc

        script, warnings, review_notes = self._parse_script(
            payload=payload,
            analysis=analysis,
            project_name=project.name,
            style=style,
            style_name=style_name,
            minutes=minutes_value,
            target_chars=target_chars,
            min_chars=min_chars,
            max_chars=max_chars,
            result=result,
        )
        self._emit(on_progress, 90, "保存版本", "保存文案新版本…")
        script = self.db.save_script_version(script)
        elapsed = time.perf_counter() - started
        self._emit(on_progress, 100, "完成", f"文案 v{script.version} 已生成")
        self._log(
            on_log,
            f"文案生成完成：v{script.version}，{len(script.paragraphs)} 段，"
            f"{script.character_count} 字，费用约 ¥{script.cost_cny:.4f}"
            + ("（缓存命中）" if script.cached else ""),
        )
        LOGGER.info(
            "文案生成完成：project=%s version=%d chars=%d cost=%.6f cached=%s",
            project_id,
            script.version,
            script.character_count,
            script.cost_cny,
            script.cached,
        )
        return ScriptRunResult(
            script=script,
            elapsed_s=round(elapsed, 2),
            cost_cny=script.cost_cny,
            cached=script.cached,
            api_calls=1,
        )

    # ------------------------------------------------------------------ 编辑
    def save_manual_edit(
        self,
        project_id: str,
        script_id: str,
        title: str,
        text: str,
        *,
        on_log: LogFn | None = None,
    ) -> Script:
        """把人工编辑保存为新版本，保留原版本不变。"""
        original = self.db.get_script(script_id)
        if original is None or original.project_id != project_id:
            raise ScriptGenerationError("找不到要编辑的文案版本。")
        chunks = split_editable_text(text)
        if not chunks:
            raise ScriptGenerationError("文案内容为空，无法保存。")
        paragraphs: list[ScriptParagraph] = []
        dropped_metadata = 0
        for index, chunk in enumerate(chunks, start=1):
            source_paragraph = (
                original.paragraphs[index - 1] if index - 1 < len(original.paragraphs) else None
            )
            paragraphs.append(
                ScriptParagraph(
                    index=index,
                    text=chunk,
                    segment_ids=source_paragraph.segment_ids if source_paragraph else [],
                    evidence_dialogue_ids=(
                        source_paragraph.evidence_dialogue_ids if source_paragraph else []
                    ),
                    shot_ids=source_paragraph.shot_ids if source_paragraph else [],
                    event_descriptions=(
                        source_paragraph.event_descriptions if source_paragraph else []
                    ),
                    purpose="manual_edit",
                    needs_review=True,
                )
            )
            if source_paragraph is None:
                dropped_metadata += 1
        edited = original.model_copy(deep=True)
        edited.id = ""
        edited.version = 0
        edited.title = (title or "").strip() or original.title
        edited.paragraphs = paragraphs
        edited.source = "manual_edit"
        edited.cost_cny = 0.0
        edited.cached = False
        edited.created_at = utc_now()
        edited.needs_review = True
        edited.review_notes = list(
            dict.fromkeys(
                [
                    *original.review_notes,
                    "人工编辑版本，需重新核对事实、原创性与平台合规。",
                ]
            )
        )
        edited.warnings = list(original.warnings)
        if dropped_metadata:
            edited.warnings.append(
                f"人工编辑新增了 {dropped_metadata} 个段落，这些段落没有原剧情段落/镜头关联。"
            )
        edited.target_chars = max(original.target_chars, edited.character_count)
        saved = self.db.save_script_version(edited)
        self._log(on_log, f"人工编辑已保存为 v{saved.version}（{len(saved.paragraphs)} 段）")
        return saved

    def restore_version(self, project_id: str, script_id: str) -> Script:
        if not self.db.set_current_script(project_id, script_id):
            raise ScriptGenerationError("找不到该文案版本，无法恢复。")
        script = self.db.get_script(script_id)
        if script is None:
            raise ScriptGenerationError("恢复后读取文案失败。")
        return script

    def list_versions(self, project_id: str) -> list[ScriptSummary]:
        return self.db.list_scripts(project_id)

    def current(self, project_id: str) -> Script | None:
        return self.db.get_current_script(project_id)

    # ------------------------------------------------------------------ 内部
    def _resolve_style(
        self,
        style_id: str,
        minutes: float | None,
        custom_name: str,
        custom_instruction: str,
    ) -> tuple[NarrationStyle, str, str, float]:
        try:
            style = get_style(style_id)
        except KeyError as exc:
            raise ScriptGenerationError(str(exc)) from exc
        if style.id == "custom":
            instruction = custom_instruction.strip()
            if not instruction:
                raise ScriptGenerationError("自定义风格需要填写“自定义要求”。")
            style_name = custom_name.strip() or "自定义解说风格"
            minutes_value = float(minutes or self.config.copywriting.custom_default_minutes)
        else:
            instruction = style.instruction
            style_name = style.name
            minutes_value = float(minutes or style.default_minutes)
        minutes_value = max(1.0, min(30.0, minutes_value))
        return style, style_name, instruction, minutes_value

    def _parse_script(
        self,
        *,
        payload: dict,
        analysis: StoryAnalysis,
        project_name: str,
        style: NarrationStyle,
        style_name: str,
        minutes: float,
        target_chars: int,
        min_chars: int,
        max_chars: int,
        result: LLMResult,
    ) -> tuple[Script, list[str], list[str]]:
        warnings: list[str] = []
        review_notes: list[str] = [
            "AI 生成文案，发布前必须人工核对事实、原创性与平台/版权合规。",
            "文案基于对白文本分析，未进行画面视觉核验。",
        ]
        raw_notes = payload.get("review_notes") or []
        if isinstance(raw_notes, list):
            review_notes.extend(_as_str(item) for item in raw_notes if _as_str(item))
        segment_map = {segment.index: segment for segment in analysis.segments}
        valid_segments = set(segment_map)
        valid_dialogue_ids: set[int] = set()
        for segment in analysis.segments:
            valid_dialogue_ids.update(segment.dialogue_ids)

        raw_paragraphs = payload.get("paragraphs") or []
        if not isinstance(raw_paragraphs, list):
            raise ScriptGenerationError("模型返回的 paragraphs 不是列表")
        paragraphs: list[ScriptParagraph] = []
        invalid_reference_count = 0
        for item in raw_paragraphs:
            if not isinstance(item, dict):
                continue
            text = _as_str(item.get("text"))
            if not text:
                continue
            segment_ids_raw = _as_int_list(item.get("segment_ids"))
            segment_ids = [number for number in segment_ids_raw if number in valid_segments]
            invalid_segments = len(segment_ids_raw) - len(segment_ids)
            evidence_raw = _as_int_list(item.get("evidence_dialogue_ids"))
            evidence = [number for number in evidence_raw if number in valid_dialogue_ids]
            invalid_reference_count += invalid_segments
            shot_ids: list[int] = []
            event_descriptions: list[str] = []
            for segment_id in segment_ids:
                segment = segment_map[segment_id]
                for shot_id in segment.shot_ids:
                    if shot_id not in shot_ids:
                        shot_ids.append(shot_id)
                for event in segment.events:
                    if event.description not in event_descriptions:
                        event_descriptions.append(event.description)
            needs_review = bool(invalid_segments or not segment_ids)
            paragraphs.append(
                ScriptParagraph(
                    index=len(paragraphs) + 1,
                    text=text,
                    segment_ids=segment_ids,
                    evidence_dialogue_ids=evidence,
                    shot_ids=shot_ids[:200],
                    event_descriptions=event_descriptions[:8],
                    purpose=_as_str(item.get("purpose")),
                    needs_review=needs_review,
                )
            )
        if not paragraphs:
            raise ScriptGenerationError("模型未返回有效文案段落。")
        if len(paragraphs) > self.config.copywriting.max_paragraphs:
            warnings.append(
                f"段落数 {len(paragraphs)} 超过上限 {self.config.copywriting.max_paragraphs}，已截断。"
            )
            paragraphs = paragraphs[: self.config.copywriting.max_paragraphs]
            for index, paragraph in enumerate(paragraphs, start=1):
                paragraph.index = index
        if invalid_reference_count:
            warnings.append(
                f"文案中有 {invalid_reference_count} 个剧情片段引用无效，已移除并标记待核实。"
            )
        short_count = sum(
            1 for paragraph in paragraphs if paragraph.char_count < self.config.copywriting.min_paragraph_chars
        )
        if short_count:
            warnings.append(f"有 {short_count} 个段落字数偏少，建议人工补充。")
        character_count = sum(paragraph.char_count for paragraph in paragraphs)
        if character_count < min_chars:
            warnings.append(
                f"成稿 {character_count} 字，低于目标区间下限 {min_chars} 字，建议重新生成或扩写。"
            )
        elif character_count > max_chars:
            warnings.append(
                f"成稿 {character_count} 字，高于目标区间上限 {max_chars} 字，建议压缩。"
            )
        analysis_purposes = {"analysis", "character", "theme", "closing"}
        analysis_count = sum(1 for paragraph in paragraphs if paragraph.purpose in analysis_purposes)
        if analysis_count < 2:
            warnings.append(
                "文案中明确的分析/观点段落少于 2 段，风格要求中的个人观点可能不足。"
            )
        if result.finish_reason == "length":
            warnings.append("模型输出达到 token 上限，文案可能被截断。")
        title = _as_str(payload.get("title"))
        if not title:
            title = f"{style_name}｜{analysis.title or project_name}"
        title = title[:120]
        script = Script(
            project_id=analysis.project_id,
            style_id=style.id,
            style_name=style_name,
            title=title,
            target_minutes=minutes,
            target_chars=target_chars,
            paragraphs=paragraphs,
            model=result.model,
            created_at=utc_now(),
            source="ai",
            analysis_created_at=analysis.created_at,
            needs_review=True,
            review_notes=list(dict.fromkeys(review_notes)),
            warnings=warnings,
            cost_cny=result.cost_cny,
            cached=result.cached,
        )
        return script, warnings, review_notes

    @staticmethod
    def _emit(on_progress: ProgressFn | None, percent: int, stage: str, message: str) -> None:
        if on_progress is not None:
            on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))

    @staticmethod
    def _log(on_log: LogFn | None, message: str) -> None:
        LOGGER.info(message)
        if on_log is not None:
            on_log(message)

