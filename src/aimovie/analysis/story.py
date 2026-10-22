"""分段剧情分析与整片整合。

设计要点：
- 先按对白切分，再逐段调用模型（每段必须引用对白编号）；
- 本地校验证据编号，移除无证据的事实性断言并标记待核实；
- 最后用分段摘要做一次整片整合（分层摘要，避免一次塞入整部电影）；
- 所有调用统一走 DeepSeekClient，自动缓存、计费、重试、预算拦截。

本模块不做画面视觉分析；text-only 模型不会被描述成“看过画面”。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import AppConfig
from ..db import ProjectDB
from ..errors import StoryAnalysisError, TaskCancelled
from ..llm.client import DeepSeekClient, LLMResult
from ..llm.parsing import parse_json_object
from ..models import (
    CharacterRelation,
    Conflict,
    DialogueSegment,
    PlotEvent,
    SegmentCharacter,
    Shot,
    StoryAnalysis,
    StoryCharacter,
    StorySegment,
    TurningPoint,
    format_clock,
    load_timeline_json,
)
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import ProgressUpdate
from ..utils import utc_now
from .prompts import build_integration_messages, build_segment_messages
from .segmentation import StoryChunk, build_story_chunks

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[ProgressUpdate], None]
LogFn = Callable[[str], None]


class ChatClientProtocol(Protocol):
    """客户端协议，测试可注入不联网的假客户端。"""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        max_tokens: int,
        temperature: float = 0.3,
    ) -> LLMResult: ...


@dataclass
class StoryRunResult:
    """一次剧情分析运行的统计。"""

    analysis: StoryAnalysis
    elapsed_s: float
    api_calls: int
    cached_calls: int
    cost_cny: float


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


def _as_float(value: object, default: float | None = None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _valid_evidence(value: object, allowed: set[int]) -> tuple[list[int], int]:
    requested = _as_int_list(value)
    valid = [number for number in requested if number in allowed]
    return valid, len(requested) - len(valid)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


class StoryAnalyzer:
    """对白证据驱动的剧情分析器。"""

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

    # ------------------------------------------------------------------ 公共
    def run(
        self,
        project_id: str,
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
        *,
        allow_over_budget: bool = False,
    ) -> StoryRunResult:
        control = control or TaskControl()
        started = time.perf_counter()
        project = self.db.get_project(project_id)
        if project is None:
            raise StoryAnalysisError(f"项目不存在：{project_id}")
        timeline = self._load_timeline(project_id)
        if not timeline.dialogues:
            raise StoryAnalysisError(
                "项目没有对白数据，无法进行基于文本的剧情分析。"
                "请先完成第一阶段语音识别，或人工补齐对白。"
            )
        total_chars = sum(len(segment.text) for segment in timeline.dialogues)
        if total_chars < self.config.story.min_dialogue_chars:
            raise StoryAnalysisError(
                f"对白总字数仅 {total_chars}，低于可靠分析下限 "
                f"{self.config.story.min_dialogue_chars}。请确认影片有足够对白，"
                "不要用模型常识补全剧情。"
            )
        chunks = build_story_chunks(timeline.dialogues, timeline.shots, self.config.story)
        self._log(on_log, f"剧情分析：{len(timeline.dialogues)} 句对白，切分为 {len(chunks)} 段")
        self._emit(on_progress, 1, "准备", f"已准备 {len(chunks)} 个剧情片段")

        client = self._client
        if client is None:
            client = DeepSeekClient(
                self.config.deepseek,
                self.db,
                project_id,
                allow_over_budget=allow_over_budget,
            )

        results: list[LLMResult] = []
        segments: list[StorySegment] = []
        warnings: list[str] = []
        review_notes: list[str] = [
            "剧情分析仅基于语音识别得到的对白文本，未使用画面视觉信息。",
            "AI 结论可能存在误读，发布前必须人工核对关键情节与人物关系。",
        ]

        for chunk in chunks:
            control.checkpoint()
            percent = int(2 + (chunk.index - 1) / max(1, len(chunks)) * 76)
            self._emit(
                on_progress,
                percent,
                "分段分析",
                f"分析第 {chunk.index}/{len(chunks)} 段（{chunk.time_label}）",
            )
            messages = build_segment_messages(chunk, len(chunks), self.config.story)
            result = client.chat(
                messages,
                purpose=f"story_segment_{chunk.index}",
                max_tokens=self.config.story.analysis_max_tokens,
                temperature=self.config.story.analysis_temperature,
            )
            results.append(result)
            control.checkpoint()
            try:
                payload = parse_json_object(result.content)
            except Exception as exc:  # noqa: BLE001
                raise StoryAnalysisError(
                    f"第 {chunk.index} 段模型输出不是合法 JSON：{exc}"
                ) from exc
            segment, segment_warnings, segment_notes = self._parse_segment(
                payload, chunk, result.model
            )
            if result.finish_reason == "length":
                segment.needs_review = True
                warnings.append(
                    f"第 {chunk.index} 段输出达到 token 上限，分析可能被截断。"
                )
            segments.append(segment)
            warnings.extend(segment_warnings)
            review_notes.extend(segment_notes)
            self._emit(
                on_progress,
                percent,
                "分段分析",
                f"第 {chunk.index} 段完成：{segment.summary[:40] or '无摘要'}",
            )
            self._log(
                on_log,
                f"第 {chunk.index} 段完成：{len(segment.events)} 个事件，"
                f"{len(segment.characters)} 个人物，{len(segment.uncertainties)} 条待核实",
            )

        control.checkpoint()
        self._emit(on_progress, 82, "整片整合", "整合分段结果，构建人物关系与重要转折")
        integration_messages = build_integration_messages(segments)
        integration_result = client.chat(
            integration_messages,
            purpose="story_integration",
            max_tokens=self.config.story.integration_max_tokens,
            temperature=self.config.story.analysis_temperature,
        )
        results.append(integration_result)
        control.checkpoint()
        try:
            integration_payload = parse_json_object(integration_result.content)
        except Exception as exc:  # noqa: BLE001
            raise StoryAnalysisError(f"整片整合结果不是合法 JSON：{exc}") from exc
        global_data, global_warnings, global_notes = self._parse_integration(
            integration_payload, segments
        )
        if integration_result.finish_reason == "length":
            global_warnings.append("整片整合输出达到 token 上限，整体摘要可能被截断。")
        warnings.extend(global_warnings)
        review_notes.extend(global_notes)

        warnings = _dedupe(
            warnings
            + [
                "未进行画面视觉分析（未配置视觉模型），动作、场景与视觉线索需人工核对。",
            ]
            + (["部分模型输出触发了截断或证据校验，请重点查看待核实项。"] if global_warnings else [])
        )
        review_notes = _dedupe(review_notes)
        cost = round(sum(result.cost_cny for result in results), 6)
        analysis = StoryAnalysis(
            project_id=project_id,
            project_name=project.name,
            created_at=utc_now(),
            timeline_generated_at=timeline.generated_at,
            model=self.config.deepseek.model,
            title=global_data["title"] or project.name,
            logline=global_data["logline"],
            overall_summary=global_data["overall_summary"],
            main_conflict=global_data["main_conflict"],
            themes=global_data["themes"],
            segments=segments,
            characters=global_data["characters"],
            turning_points=global_data["turning_points"],
            unresolved=global_data["unresolved"],
            review_notes=review_notes,
            warnings=warnings,
            visual_analysis="disabled",
            estimated_cost_cny=cost,
            api_calls=len(results),
            cached_calls=sum(1 for result in results if result.cached),
            needs_review=True,
        )
        self._emit(on_progress, 96, "保存", "保存剧情分析结果")
        self.db.save_story_analysis(analysis)
        elapsed = time.perf_counter() - started
        self._emit(on_progress, 100, "完成", f"剧情分析完成：{len(segments)} 段")
        self._log(
            on_log,
            f"剧情分析完成：{len(segments)} 段，{len(analysis.characters)} 个人物，"
            f"{len(analysis.turning_points)} 个转折，费用约 ¥{cost:.4f}",
        )
        LOGGER.info(
            "剧情分析完成：project=%s segments=%d calls=%d cached=%d cost=%.6f elapsed=%.1fs",
            project_id,
            len(segments),
            len(results),
            analysis.cached_calls,
            cost,
            elapsed,
        )
        return StoryRunResult(
            analysis=analysis,
            elapsed_s=round(elapsed, 2),
            api_calls=len(results),
            cached_calls=analysis.cached_calls,
            cost_cny=cost,
        )

    # ------------------------------------------------------------------ 解析
    def _get_client(self) -> ChatClientProtocol:
        if self._client is None:
            raise RuntimeError("剧情分析器尚未初始化客户端")
        return self._client

    def _parse_segment(
        self,
        payload: dict,
        chunk: StoryChunk,
        model: str,
    ) -> tuple[StorySegment, list[str], list[str]]:
        allowed = set(chunk.dialogue_ids)
        warnings: list[str] = []
        notes: list[str] = []
        dropped = 0
        invalid_references = 0

        events: list[PlotEvent] = []
        raw_events = payload.get("events") or []
        if isinstance(raw_events, list):
            for item in raw_events:
                if not isinstance(item, dict):
                    continue
                description = _as_str(item.get("description"))
                if not description:
                    continue
                evidence, invalid = _valid_evidence(item.get("evidence_dialogue_ids"), allowed)
                if invalid:
                    invalid_references += 1
                if not evidence:
                    dropped += 1
                    continue
                importance = _as_str(item.get("importance"), "medium")
                if importance not in {"low", "medium", "high"}:
                    importance = "medium"
                events.append(
                    PlotEvent(
                        description=description,
                        evidence_dialogue_ids=evidence,
                        importance=importance,  # type: ignore[arg-type]
                    )
                )

        conflicts: list[Conflict] = []
        for item in payload.get("conflicts") or []:
            if not isinstance(item, dict):
                continue
            description = _as_str(item.get("description"))
            if not description:
                continue
            evidence, invalid = _valid_evidence(item.get("evidence_dialogue_ids"), allowed)
            if invalid:
                invalid_references += 1
            if not evidence:
                dropped += 1
                continue
            conflicts.append(Conflict(description=description, evidence_dialogue_ids=evidence))

        turning_points: list[TurningPoint] = []
        for item in payload.get("turning_points") or []:
            if not isinstance(item, dict):
                continue
            description = _as_str(item.get("description"))
            if not description:
                continue
            evidence, invalid = _valid_evidence(item.get("evidence_dialogue_ids"), allowed)
            if invalid:
                invalid_references += 1
            if not evidence:
                dropped += 1
                continue
            turning_points.append(
                TurningPoint(description=description, evidence_dialogue_ids=evidence)
            )

        characters: list[SegmentCharacter] = []
        for item in payload.get("characters") or []:
            if not isinstance(item, dict):
                continue
            name = _as_str(item.get("name"))
            if not name:
                continue
            evidence, invalid = _valid_evidence(item.get("evidence_dialogue_ids"), allowed)
            characters.append(
                SegmentCharacter(
                    name=name,
                    description=_as_str(item.get("description")),
                    evidence_dialogue_ids=evidence,
                    needs_review=invalid > 0 or not evidence,
                )
            )

        uncertainties = [
            _as_str(item) for item in (payload.get("uncertainties") or []) if _as_str(item)
        ]
        confidence = _as_float(payload.get("confidence"))
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
        summary = _as_str(payload.get("summary"))
        needs_review = bool(
            dropped
            or invalid_references
            or not events
            or uncertainties
            or (confidence is not None and confidence < 0.5)
        )
        if dropped:
            warnings.append(
                f"第 {chunk.index} 段有 {dropped} 条断言缺少有效对白证据，已移除。"
            )
            notes.append(f"第 {chunk.index} 段移除了 {dropped} 条无证据断言，原文请查看模型缓存/日志。")
        if invalid_references:
            warnings.append(
                f"第 {chunk.index} 段有 {invalid_references} 条断言引用了不存在的对白编号，已过滤。"
            )
        if not events:
            notes.append(f"第 {chunk.index} 段未提取到有证据的剧情事件。")
        if confidence is not None and confidence < 0.5:
            notes.append(f"第 {chunk.index} 段模型置信度较低（{confidence:.2f}）。")
        if not summary:
            warnings.append(f"第 {chunk.index} 段摘要为空。")
        segment = StorySegment(
            index=chunk.index,
            start_s=chunk.start_s,
            end_s=chunk.end_s,
            summary=summary,
            events=events,
            characters=characters,
            conflicts=conflicts,
            turning_points=turning_points,
            uncertainties=uncertainties,
            dialogue_ids=chunk.dialogue_ids,
            shot_ids=chunk.shot_ids,
            confidence=confidence,
            needs_review=needs_review,
            model=model,
        )
        return segment, warnings, notes

    def _parse_integration(
        self,
        payload: dict,
        segments: list[StorySegment],
    ) -> tuple[dict, list[str], list[str]]:
        warnings: list[str] = []
        notes: list[str] = []
        segment_map = {segment.index: segment for segment in segments}
        valid_segments = set(segment_map)
        all_dialogue_ids: set[int] = set()
        for segment in segments:
            all_dialogue_ids.update(segment.dialogue_ids)

        title = _as_str(payload.get("title"))
        logline = _as_str(payload.get("logline"))
        overall_summary = _as_str(payload.get("overall_summary"))
        main_conflict = _as_str(payload.get("main_conflict"))
        themes = [_as_str(item) for item in (payload.get("themes") or []) if _as_str(item)]
        unresolved = [_as_str(item) for item in (payload.get("unresolved") or []) if _as_str(item)]
        notes.extend(_as_str(item) for item in (payload.get("review_notes") or []) if _as_str(item))

        characters: list[StoryCharacter] = []
        for item in payload.get("characters") or []:
            if not isinstance(item, dict):
                continue
            name = _as_str(item.get("name"))
            if not name:
                continue
            evidence_segments, invalid = _valid_evidence(
                item.get("evidence_segment_ids"), valid_segments
            )
            relations: list[CharacterRelation] = []
            for relation_item in item.get("relations") or []:
                if not isinstance(relation_item, dict):
                    continue
                target = _as_str(relation_item.get("target"))
                if not target:
                    continue
                relation_segments, relation_invalid = _valid_evidence(
                    relation_item.get("evidence_segment_ids"), valid_segments
                )
                relations.append(
                    CharacterRelation(
                        target=target,
                        relation=_as_str(relation_item.get("relation")),
                        evidence_segment_ids=relation_segments,
                        needs_review=relation_invalid > 0 or not relation_segments,
                    )
                )
            characters.append(
                StoryCharacter(
                    name=name,
                    description=_as_str(item.get("description")),
                    role=_as_str(item.get("role")),
                    relations=relations,
                    evidence_segment_ids=evidence_segments,
                    needs_review=invalid > 0 or not evidence_segments,
                )
            )

        turning_points: list[TurningPoint] = []
        for item in payload.get("turning_points") or []:
            if not isinstance(item, dict):
                continue
            description = _as_str(item.get("description"))
            if not description:
                continue
            segment_index = None
            try:
                candidate = int(item.get("segment_index"))  # type: ignore[arg-type]
                if candidate in valid_segments:
                    segment_index = candidate
                else:
                    warnings.append(f"整片整合引用了不存在的分段 {candidate}，已忽略。")
            except (TypeError, ValueError):
                pass
            allowed = (
                set(segment_map[segment_index].dialogue_ids)
                if segment_index is not None
                else all_dialogue_ids
            )
            evidence, invalid = _valid_evidence(item.get("evidence_dialogue_ids"), allowed)
            needs_review = invalid > 0 or not evidence or segment_index is None
            turning_points.append(
                TurningPoint(
                    description=description,
                    segment_index=segment_index,
                    evidence_dialogue_ids=evidence,
                    needs_review=needs_review,
                )
            )

        if not characters:
            characters = self._fallback_characters(segments)
            if characters:
                notes.append("整片整合未返回人物档案，已从分段结果汇总人物，建议人工核对。")
        if not turning_points:
            fallback_points: list[TurningPoint] = []
            for segment in segments:
                for point in segment.turning_points:
                    fallback_points.append(
                        TurningPoint(
                            description=point.description,
                            segment_index=segment.index,
                            evidence_dialogue_ids=point.evidence_dialogue_ids,
                            needs_review=False,
                        )
                    )
            turning_points = fallback_points[:12]
            if turning_points:
                notes.append("整片整合未返回转折点，已从分段结果汇总。")
        if not overall_summary:
            overall_summary = " ".join(segment.summary for segment in segments if segment.summary)[:1500]
            warnings.append("整片整合缺少 overall_summary，已用分段摘要拼接。")
        if not title:
            title = "剧情分析（未命名）"
        if not logline and overall_summary:
            logline = overall_summary[:80]

        return (
            {
                "title": title,
                "logline": logline,
                "overall_summary": overall_summary,
                "main_conflict": main_conflict,
                "themes": themes,
                "characters": characters,
                "turning_points": turning_points,
                "unresolved": unresolved,
            },
            warnings,
            notes,
        )

    @staticmethod
    def _fallback_characters(segments: list[StorySegment]) -> list[StoryCharacter]:
        merged: dict[str, StoryCharacter] = {}
        for segment in segments:
            for character in segment.characters:
                existing = merged.get(character.name)
                if existing is None:
                    merged[character.name] = StoryCharacter(
                        name=character.name,
                        description=character.description,
                        evidence_segment_ids=[segment.index],
                        needs_review=True,
                    )
                else:
                    merged[character.name] = existing.model_copy(
                        update={
                            "evidence_segment_ids": existing.evidence_segment_ids + [segment.index],
                            "needs_review": True,
                        }
                    )
        return list(merged.values())

    # ------------------------------------------------------------------ 工具
    def _load_timeline(self, project_id: str):
        project = self.db.get_project(project_id)
        if project is None:
            raise StoryAnalysisError(f"项目不存在：{project_id}")
        timeline_path = Path(project.project_dir) / "timeline.json"
        if timeline_path.exists():
            try:
                timeline = load_timeline_json(timeline_path)
                if timeline.project_id == project_id:
                    return timeline
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("读取时间轴快照失败，改用数据库：%s", exc)
        return self.db.build_timeline(project_id)

    @staticmethod
    def _emit(on_progress: ProgressFn | None, percent: int, stage: str, message: str) -> None:
        if on_progress is not None:
            on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))

    @staticmethod
    def _log(on_log: LogFn | None, message: str) -> None:
        LOGGER.info(message)
        if on_log is not None:
            on_log(message)
