"""项目费用、预算判断与调用前估算。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import AppConfig
from ..db import ProjectDB
from ..llm.pricing import compute_cost
from ..models import ApiCallRecord, CostSummary, DialogueSegment, StoryAnalysis


@dataclass(frozen=True)
class BudgetCheck:
    """一次 AI 任务启动前的预算判断。"""

    estimated_cny: float
    snapshot: CostSummary
    requires_confirmation: bool

    @property
    def allowed(self) -> bool:
        return not self.requires_confirmation


class CostService:
    """读取账目、估算下一次调用并做预算判断。"""

    def __init__(self, config: AppConfig, db: ProjectDB):
        self.config = config
        self.db = db

    def snapshot(self, project_id: str | None = None) -> CostSummary:
        budget = self.config.deepseek.budget_cny_per_project if project_id else 0.0
        return self.db.cost_summary(project_id, budget_cny=budget)

    def recent_calls(self, project_id: str | None = None, limit: int = 30) -> list[ApiCallRecord]:
        return self.db.recent_api_calls(project_id, limit=limit)

    def check_budget(self, project_id: str, estimated_cny: float) -> BudgetCheck:
        snapshot = self.snapshot(project_id)
        if snapshot.budget_cny <= 0:
            return BudgetCheck(estimated_cny=estimated_cny, snapshot=snapshot, requires_confirmation=False)
        requires = snapshot.spent_cny + max(0.0, estimated_cny) > snapshot.budget_cny + 1e-9
        return BudgetCheck(estimated_cny=estimated_cny, snapshot=snapshot, requires_confirmation=requires)

    def estimate_story_cost(self, dialogues: list[DialogueSegment]) -> float:
        """粗估一次分段剧情分析的费用（保守，用于启动前预算提示）。"""
        story = self.config.story
        deepseek = self.config.deepseek
        total_chars = sum(len(segment.text) for segment in dialogues)
        segments = max(1, min(story.max_segments, math.ceil(total_chars / max(1, story.chunk_target_chars))))
        # 对白 + 镜头元数据 + 提示词模板 + 每段输出上限
        prompt_chars = int(total_chars * 1.8) + segments * 1200 + 800
        prompt_tokens = prompt_chars + 64  # 中文按 1 字≈1 token 的保守估算
        completion_tokens = segments * story.analysis_max_tokens + story.integration_max_tokens
        return compute_cost(prompt_tokens, completion_tokens, deepseek)

    def estimate_script_cost(self, analysis: StoryAnalysis, minutes: float) -> float:
        """粗估一次文案生成费用。"""
        copywriting = self.config.copywriting
        prompt_chars = len(analysis.to_json()) + 3000
        output_tokens = max(800, int(float(minutes) * copywriting.max_tokens_per_minute))
        return compute_cost(prompt_chars + 64, output_tokens, self.config.deepseek)
