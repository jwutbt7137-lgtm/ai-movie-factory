"""基于剧情片段、镜头时长和连续性的镜头匹配。

说明：第一/二阶段没有画面语义标签，本匹配器只能利用：
- 文案段落关联的剧情片段与候选镜头；
- 镜头时长、顺序和连续性；
- 低置信度标记。
它不是“看懂了画面”的语义匹配，结果必须人工审核。
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable

from ..config import AppConfig
from ..db import ProjectDB
from ..errors import ShotMatchingError, TaskCancelled
from ..models import Shot, ShotMatchPlan, ShotSelection
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import ProgressUpdate
from ..utils import utc_now

LOGGER = logging.getLogger(__name__)

ProgressFn = Callable[[ProgressUpdate], None]
LogFn = Callable[[str], None]


class ShotMatcher:
    """把配音句子映射到连续的镜头片段。"""

    def __init__(self, db: ProjectDB, config: AppConfig):
        self.db = db
        self.config = config

    def match(
        self,
        project_id: str,
        *,
        control: TaskControl | None = None,
        on_progress: ProgressFn | None = None,
        on_log: LogFn | None = None,
    ) -> ShotMatchPlan:
        control = control or TaskControl()
        started = time.perf_counter()
        project = self.db.get_project(project_id)
        if project is None:
            raise ShotMatchingError(f"项目不存在：{project_id}")
        script = self.db.get_current_script(project_id)
        voiceover = self.db.get_current_voiceover(project_id)
        if script is None:
            raise ShotMatchingError("项目还没有文案，请先生成文案。")
        if voiceover is None:
            raise ShotMatchingError("项目还没有配音，请先生成配音。")
        if voiceover.script_id != script.id:
            raise ShotMatchingError(
                "当前配音基于旧文案版本，请先为当前文案重新生成配音，再做镜头匹配。"
            )
        shots = self.db.get_shots(project_id)
        if not shots:
            raise ShotMatchingError("项目没有镜头数据，请先完成第一阶段镜头切分。")
        analysis = self.db.get_story_analysis(project_id)
        visual_unavailable = analysis is None or analysis.visual_analysis == "disabled"
        warnings: list[str] = []
        if analysis is None:
            warnings.append("缺少剧情分析，镜头匹配只能按顺序/时长兜底，置信度会较低。")
        if visual_unavailable:
            warnings.append(
                "未接入画面视觉语义；当前置信度只代表剧情片段/时长/连续性规则，"
                "所有镜头选择均需人工确认。"
            )
        shot_by_id = {shot.index: shot for shot in shots}
        segment_shots: dict[int, list[int]] = {}
        if analysis is not None:
            for segment in analysis.segments:
                segment_shots[segment.index] = list(segment.shot_ids)

        selections: list[ShotSelection] = []
        used_counter: Counter[int] = Counter()
        cursor = 0
        self._emit(on_progress, 2, "准备", f"匹配 {voiceover.unit_count} 句配音")
        try:
            for position, unit in enumerate(voiceover.units, start=1):
                control.checkpoint()
                selection, cursor = self._match_unit(
                    unit,
                    shot_by_id,
                    segment_shots,
                    cursor,
                    used_counter,
                    warnings,
                    visual_unavailable=visual_unavailable,
                )
                selections.append(selection)
                used_counter.update(selection.shot_ids)
                self._emit(
                    on_progress,
                    5 + int(88 * position / max(1, voiceover.unit_count)),
                    "镜头匹配",
                    f"已完成 {position}/{voiceover.unit_count} 句",
                )
                if on_log is not None:
                    on_log(
                        f"第 {unit.index} 句：{len(selection.shot_ids)} 个镜头，"
                        f"置信度 {selection.confidence:.2f}"
                    )
            control.checkpoint()
            confidences = [item.confidence for item in selections]
            average = sum(confidences) / len(confidences) if confidences else 0.0
            low_count = sum(1 for item in selections if item.needs_review)
            plan = ShotMatchPlan(
                project_id=project_id,
                script_id=script.id,
                script_version=script.version,
                voiceover_id=voiceover.id,
                created_at=utc_now(),
                selections=selections,
                total_duration_s=voiceover.total_duration_s,
                average_confidence=round(average, 3),
                low_confidence_count=low_count,
                warnings=warnings,
                needs_review=low_count > 0 or average < self.config.matching.confidence_threshold,
                elapsed_s=round(time.perf_counter() - started, 2),
            )
            self._emit(on_progress, 97, "保存", "保存镜头匹配方案")
            self.db.save_shot_match(plan)
            self._emit(on_progress, 100, "完成", "镜头匹配完成，请人工审核低置信度片段")
            LOGGER.info(
                "镜头匹配完成：project=%s units=%d avg=%.3f low=%d",
                project_id,
                len(selections),
                average,
                low_count,
            )
            return plan
        except TaskCancelled:
            raise
        except Exception:
            LOGGER.exception("镜头匹配失败")
            raise

    def _match_unit(
        self,
        unit,
        shot_by_id: dict[int, Shot],
        segment_shots: dict[int, list[int]],
        cursor: int,
        used_counter: Counter[int],
        warnings: list[str],
        *,
        visual_unavailable: bool = True,
    ) -> tuple[ShotSelection, int]:
        target = max(0.1, float(unit.duration_s))
        need = target + self.config.matching.duration_margin_s
        preferred = [shot_id for shot_id in unit.shot_ids if shot_id in shot_by_id]
        segment_candidates: list[int] = []
        for segment_id in unit.segment_ids:
            segment_candidates.extend(segment_shots.get(segment_id, []))
        # 段落镜头列表可能因存储上限被截断；合并整段候选，避免游标用完后跳回片头
        candidate_ids = sorted(
            dict.fromkeys(
                shot_id
                for shot_id in [*preferred, *segment_candidates]
                if shot_id in shot_by_id
            )
        )
        if not candidate_ids:
            candidate_ids = [shot.index for shot in sorted(shot_by_id.values(), key=lambda item: item.index)]
            warnings.append(f"第 {unit.index} 句缺少剧情片段关联，使用全片镜头顺序兜底。")

        # 保持时间顺序：优先从游标之后的镜头开始；没有更晚的镜头时才回到开头
        start_pos = 0
        for index, shot_id in enumerate(candidate_ids):
            if shot_id > cursor:
                start_pos = index
                break
        else:
            start_pos = 0
        selected: list[int] = []
        total = 0.0
        for shot_id in candidate_ids[start_pos:]:
            if len(selected) >= self.config.matching.max_shots_per_unit:
                break
            selected.append(shot_id)
            total += shot_by_id[shot_id].duration_s
            if total >= need:
                break

        # 候选片段不够时，用紧邻的后续镜头补足，保持连续性
        if total < need and self.config.matching.allow_extend:
            last_index = selected[-1] if selected else (cursor - 1)
            next_index = last_index + 1
            while total < need and next_index in shot_by_id and len(selected) < self.config.matching.max_shots_per_unit:
                if next_index not in selected:
                    selected.append(next_index)
                    total += shot_by_id[next_index].duration_s
                next_index += 1
                if next_index - last_index > len(shot_by_id):
                    break

        coverage = min(total, target)
        selected = selected[: self.config.matching.max_shots_per_unit]
        if not selected:
            return (
                ShotSelection(
                    sentence_index=unit.index,
                    paragraph_index=unit.paragraph_index,
                    text=unit.text,
                    target_start_s=unit.start_s,
                    target_end_s=unit.end_s,
                    target_duration_s=target,
                    confidence=0.0,
                    needs_review=True,
                    reason="没有可用镜头",
                ),
                cursor,
            )

        preferred_set = set(preferred)
        segment_set = {shot_id for ids in segment_shots.values() for shot_id in ids}
        if preferred_set and set(selected).issubset(preferred_set):
            segment_score = 1.0
        elif set(selected).issubset(segment_set):
            segment_score = 0.8
        else:
            segment_score = 0.4
        coverage_score = min(1.0, coverage / max(0.1, target))
        adjacent = sum(
            1 for left, right in zip(selected, selected[1:]) if right == left + 1
        )
        continuity = 1.0 if len(selected) <= 1 else adjacent / (len(selected) - 1)
        confidence = round(
            max(0.0, min(1.0, 0.5 * segment_score + 0.3 * coverage_score + 0.2 * continuity)), 3
        )
        if visual_unavailable:
            # 时间/剧情规则无法证明画面语义一致，置信度上限设为 0.70
            confidence = min(confidence, 0.70)
        reuse = sum(
            1 for shot_id in selected if used_counter[shot_id] > self.config.matching.max_reuse
        )
        needs_review = (
            visual_unavailable
            or confidence < self.config.matching.confidence_threshold
            or coverage < target * 0.9
            or reuse > 0
            or not unit.shot_ids
        )
        reasons: list[str] = []
        if preferred_set and set(selected).issubset(preferred_set):
            reasons.append("使用文案段落关联镜头")
        elif segment_set and set(selected).issubset(segment_set):
            reasons.append("使用剧情片段候选镜头")
        else:
            reasons.append("候选不足，扩展到相邻镜头/顺序兜底")
        reasons.append(f"覆盖 {coverage:.2f}/{target:.2f} 秒")
        if reuse:
            reasons.append(f"有 {reuse} 个镜头复用超过限制")
        if coverage < target * 0.9:
            reasons.append("镜头总时长不足")
        if confidence < self.config.matching.confidence_threshold:
            reasons.append("置信度偏低")
        if visual_unavailable:
            reasons.append("未接入画面语义，需人工确认")
        selection = ShotSelection(
            sentence_index=unit.index,
            paragraph_index=unit.paragraph_index,
            text=unit.text,
            target_start_s=unit.start_s,
            target_end_s=unit.end_s,
            target_duration_s=target,
            shot_ids=selected,
            shot_durations_s=[round(shot_by_id[shot_id].duration_s, 3) for shot_id in selected],
            segment_ids=list(unit.segment_ids),
            coverage_s=round(coverage, 3),
            confidence=confidence,
            needs_review=needs_review,
            reason="；".join(reasons),
        )
        return selection, selected[-1]

    @staticmethod
    def _emit(on_progress: ProgressFn | None, percent: int, stage: str, message: str) -> None:
        if on_progress is not None:
            on_progress(ProgressUpdate(percent=percent, stage=stage, message=message))
