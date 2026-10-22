"""费用统计、预算判断与调用前估算测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.config import AppConfig
from aimovie.costs.service import CostService
from aimovie.db import ProjectDB
from aimovie.models import DialogueSegment, StoryAnalysis, StorySegment
from aimovie.utils import utc_now


def _project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    return db.create_project("费用测试", source, tmp_path / "project")


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.deepseek.budget_cny_per_project = 0.01
    return config


def _record_call(db: ProjectDB, project_id: str, cost: float, cached: bool = False) -> None:
    db.record_api_call(
        project_id=project_id,
        purpose="test",
        model="deepseek-chat",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_cny=cost,
        cached=cached,
        status="success",
        started_at=utc_now(),
        finished_at=utc_now(),
    )


def test_cost_summary_and_budget(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    _record_call(db, project.id, 0.004)
    _record_call(db, project.id, 0.0, cached=True)
    db.record_api_call(
        project_id=project.id,
        purpose="failed",
        model="deepseek-chat",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_cny=0.0,
        cached=False,
        status="network_error",
        started_at=utc_now(),
        finished_at=utc_now(),
        error="timeout",
    )
    summary = db.cost_summary(project.id, budget_cny=0.01)
    assert summary.spent_cny == pytest.approx(0.004)
    assert summary.calls == 3
    assert summary.cached_calls == 1
    assert summary.failed_calls == 1
    assert summary.remaining_cny == pytest.approx(0.006)
    assert summary.cache_hit_rate == pytest.approx(1 / 3)

    global_summary = db.cost_summary(None)
    assert global_summary.calls == 3
    assert global_summary.spent_cny == pytest.approx(0.004)


def test_cost_service_budget_check(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    service = CostService(_config(tmp_path), db)
    _record_call(db, project.id, 0.008)
    check = service.check_budget(project.id, 0.004)
    assert check.requires_confirmation is True
    assert check.allowed is False
    under = service.check_budget(project.id, 0.001)
    assert under.requires_confirmation is False


def test_estimates_are_positive(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    service = CostService(_config(tmp_path), db)
    dialogues = [
        DialogueSegment(index=index, start_s=index, end_s=index + 0.8, text="对白内容" * 10)
        for index in range(1, 20)
    ]
    story_cost = service.estimate_story_cost(dialogues)
    assert story_cost > 0
    analysis = StoryAnalysis(
        project_id=project.id,
        project_name="估算",
        segments=[
            StorySegment(index=1, start_s=0, end_s=10, summary="内容" * 50, dialogue_ids=[1])
        ],
    )
    script_cost = service.estimate_script_cost(analysis, 3)
    assert script_cost > 0


def test_unlimited_budget(db: ProjectDB, tmp_path: Path) -> None:
    project = _project(db, tmp_path)
    config = _config(tmp_path)
    config.deepseek.budget_cny_per_project = 0
    service = CostService(config, db)
    _record_call(db, project.id, 10.0)
    check = service.check_budget(project.id, 100.0)
    assert check.requires_confirmation is False
    assert check.snapshot.remaining_cny is None
