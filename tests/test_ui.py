"""PySide6 界面冒烟测试（离屏）。"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from aimovie.media.probe import probe_media  # noqa: E402
from aimovie.models import (  # noqa: E402
    DialogueSegment,
    PlotEvent,
    Script,
    ScriptParagraph,
    Shot,
    ShotMatchPlan,
    ShotSelection,
    SpeechUnit,
    StoryAnalysis,
    StorySegment,
    TurningPoint,
    Voiceover,
)
from aimovie.ui.dialogs import SettingsDialog  # noqa: E402
from aimovie.ui.main_window import MainWindow  # noqa: E402
from aimovie.ui.shot_editor import ShotEditorDialog  # noqa: E402
from aimovie.utils import utc_now  # noqa: E402


@pytest.mark.ui
def test_main_window_loads_project(qtbot, app_config, db, sample_video: Path, tmp_path: Path) -> None:
    project = db.create_project("界面测试项目", sample_video, tmp_path / "ui_project")
    db.save_media(project.id, probe_media(sample_video, ffprobe_path=app_config.ffprobe_path))
    db.replace_shots(
        project.id,
        [Shot(index=1, start_s=0, end_s=1.2), Shot(index=2, start_s=1.2, end_s=2.4)],
    )
    db.replace_dialogues(
        project.id,
        [DialogueSegment(index=1, start_s=0.1, end_s=0.8, text="界面测试对白", language="zh")],
    )
    db.update_project(project.id, status="ready")

    window = MainWindow(app_config, db, log_path=tmp_path / "ui.log")
    qtbot.addWidget(window)
    window.show()
    window.refresh_projects(select_id=project.id)

    assert window.current_project is not None
    assert window.current_project.id == project.id
    assert window.project_list.count() == 1
    assert window.shot_table.rowCount() == 2
    assert window.dialogue_table.rowCount() == 1
    summary = window.summary_view.toPlainText()
    assert "界面测试项目" in summary
    assert "640×360" in summary
    assert "镜头数量：2" in summary
    assert window.start_button.isEnabled()
    assert window.start_button.text() == "重新分析"
    window.close()
    QApplication.processEvents()


@pytest.mark.ui
def test_budget_confirmation_flow(qtbot, app_config, db, sample_video: Path, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    project = db.create_project("预算界面测试", sample_video, tmp_path / "budget_project")
    db.record_api_call(
        project_id=project.id,
        purpose="previous",
        model="deepseek-chat",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_cny=0.02,
        cached=False,
        status="success",
        started_at=utc_now(),
    )
    app_config.deepseek.budget_cny_per_project = 0.01
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ui-test-budget")
    window = MainWindow(app_config, db, log_path=tmp_path / "budget.log")
    qtbot.addWidget(window)
    window.refresh_projects(select_id=project.id)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    proceed, allow = window._check_ai_preconditions(0.01, "测试任务")
    assert proceed is True
    assert allow is True
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    proceed, allow = window._check_ai_preconditions(0.01, "测试任务")
    assert proceed is False
    assert allow is False
    window.close()
    QApplication.processEvents()


@pytest.mark.ui
def test_settings_dialog_has_all_tabs(qtbot, app_config) -> None:
    dialog = SettingsDialog(app_config)
    qtbot.addWidget(dialog)
    assert dialog.tabs.count() == 7
    titles = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert "镜头检测" in titles
    assert "语音识别" in titles
    assert "DeepSeek（第二阶段）" in titles
    assert "AI 生成" in titles
    assert "配音与匹配" in titles


@pytest.mark.ui
def test_shot_editor_dialog(qtbot, tmp_path: Path) -> None:
    shots = [
        Shot(index=1, start_s=0, end_s=1.2),
        Shot(index=2, start_s=1.2, end_s=2.4),
        Shot(index=3, start_s=2.4, end_s=3.2),
    ]
    dialog = ShotEditorDialog(
        shots,
        selected_ids=[1, 3],
        target_duration_s=1.0,
        sentence_text="测试句子。",
    )
    qtbot.addWidget(dialog)
    assert dialog.selected_ids() == [1, 3]
    assert "2 个镜头" in dialog.summary.text()
    dialog._set_all(True)
    assert dialog.selected_ids() == [1, 2, 3]
    dialog._set_all(False)
    assert dialog.selected_ids() == []


@pytest.mark.ui
def test_main_window_loads_story_and_script(qtbot, app_config, db, sample_video: Path, tmp_path: Path) -> None:
    project = db.create_project("AI界面测试", sample_video, tmp_path / "ai_project")
    media = probe_media(sample_video, ffprobe_path=app_config.ffprobe_path)
    db.save_media(project.id, media)
    db.replace_shots(project.id, [Shot(index=1, start_s=0, end_s=1.2)])
    db.replace_dialogues(
        project.id,
        [DialogueSegment(index=1, start_s=0.1, end_s=0.8, text="AI界面测试对白", language="zh")],
    )
    analysis = StoryAnalysis(
        project_id=project.id,
        project_name=project.name,
        title="测试片",
        logline="一个用于界面测试的故事。",
        overall_summary="主角完成了界面测试。",
        segments=[
            StorySegment(
                index=1,
                start_s=0,
                end_s=1.2,
                summary="主角进行界面测试。",
                events=[
                    PlotEvent(
                        description="完成界面测试",
                        evidence_dialogue_ids=[1],
                        importance="high",
                    )
                ],
                dialogue_ids=[1],
                shot_ids=[1],
            )
        ],
        turning_points=[
            TurningPoint(
                description="测试通过",
                segment_index=1,
                evidence_dialogue_ids=[1],
            )
        ],
    )
    db.save_story_analysis(analysis)
    script = db.save_script_version(
        Script(
            project_id=project.id,
            style_id="quick3",
            style_name="三分钟剧情速看",
            title="界面文案",
            target_minutes=3,
            target_chars=720,
            paragraphs=[
                ScriptParagraph(
                    index=1,
                    text="这是界面测试文案。",
                    segment_ids=[1],
                    evidence_dialogue_ids=[1],
                    shot_ids=[1],
                )
            ],
        )
    )
    voiceover = db.save_voiceover(
        Voiceover(
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            engine="fake",
            voice="fake",
            audio_path=str(tmp_path / "voice.wav"),
            total_duration_s=1.0,
            units=[
                SpeechUnit(
                    index=1,
                    paragraph_index=1,
                    text="这是界面测试文案。",
                    start_s=0,
                    end_s=1.0,
                    duration_s=1.0,
                    audio_path=str(tmp_path / "sentence.wav"),
                    engine="fake",
                    segment_ids=[1],
                    shot_ids=[1],
                )
            ],
        )
    )
    plan = db.save_shot_match(
        ShotMatchPlan(
            project_id=project.id,
            script_id=script.id,
            script_version=script.version,
            voiceover_id=voiceover.id,
            selections=[
                ShotSelection(
                    sentence_index=1,
                    paragraph_index=1,
                    text="这是界面测试文案。",
                    target_start_s=0,
                    target_end_s=1.0,
                    target_duration_s=1.0,
                    shot_ids=[1],
                    shot_durations_s=[1.2],
                    segment_ids=[1],
                    coverage_s=1.0,
                    confidence=0.9,
                )
            ],
            total_duration_s=1.0,
            average_confidence=0.9,
            low_confidence_count=0,
        )
    )

    window = MainWindow(app_config, db, log_path=tmp_path / "ai_ui.log")
    qtbot.addWidget(window)
    window.refresh_projects(select_id=project.id)
    assert window.tabs.count() == 10
    assert window.story_panel.analysis is not None
    assert window.story_panel.segment_list.count() == 1
    assert window.script_panel.current_script is not None
    assert window.script_panel.current_script.id == script.id
    assert "这是界面测试文案" in window.script_panel.editor.toPlainText()
    assert window.script_panel._analysis_available is True
    assert window.voiceover is not None
    assert window.tts_panel.table.rowCount() == 1
    assert window.match_plan is not None
    assert window.match_panel.table.rowCount() == 1
    assert window.match_plan.id == plan.id
    assert window.export_panel.export_button.isEnabled() is True
    window.close()
    QApplication.processEvents()
