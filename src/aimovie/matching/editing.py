"""人工镜头选择：生成新的匹配方案版本。"""

from __future__ import annotations

import uuid

from ..models import Shot, ShotMatchPlan
from ..utils import utc_now


def apply_manual_selection(
    plan: ShotMatchPlan,
    sentence_index: int,
    shot_ids: list[int],
    shots: list[Shot] | dict[int, Shot],
) -> ShotMatchPlan:
    """把某句替换为人工选择的镜头，返回新的方案对象（不落库）。"""
    shot_by_id = shots if isinstance(shots, dict) else {shot.index: shot for shot in shots}
    selection = next(
        (item for item in plan.selections if item.sentence_index == sentence_index),
        None,
    )
    if selection is None:
        raise ValueError(f"匹配方案中没有第 {sentence_index} 句")
    ordered_ids = [shot_id for shot_id in dict.fromkeys(shot_ids) if shot_id in shot_by_id]
    if not ordered_ids:
        raise ValueError("至少选择一个有效镜头")
    durations = [shot_by_id[shot_id].duration_s for shot_id in ordered_ids]
    coverage = min(sum(durations), selection.target_duration_s)
    new_selection = selection.model_copy(
        update={
            "shot_ids": ordered_ids,
            "shot_durations_s": [round(value, 3) for value in durations],
            "coverage_s": round(coverage, 3),
            "confidence": 1.0,
            "needs_review": False,
            "manual": True,
            "reason": "人工选择镜头；导出前请再次确认时长与顺序",
        }
    )
    selections = [
        new_selection if item.sentence_index == sentence_index else item
        for item in plan.selections
    ]
    confidences = [item.confidence for item in selections]
    low_count = sum(1 for item in selections if item.needs_review)
    return plan.model_copy(
        update={
            "id": uuid.uuid4().hex[:16],
            "created_at": utc_now(),
            "selections": selections,
            "average_confidence": round(sum(confidences) / len(confidences), 3)
            if confidences
            else 0.0,
            "low_confidence_count": low_count,
            "needs_review": low_count > 0,
        }
    )
