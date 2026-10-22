"""主窗口。

界面包含：项目管理、素材导入、第一阶段后台分析、镜头/对白浏览、
DeepSeek 剧情理解、解说文案生成/编辑/版本、费用统计、错误日志与设置。
"""

from __future__ import annotations

import html
import logging
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..analysis.story import StoryAnalyzer, StoryRunResult
from ..config import AppConfig, has_deepseek_api_key, resolve_executable, save_config
from ..copywriting.generator import ScriptGenerator, ScriptRunResult
from ..copywriting.styles import get_style
from ..costs.service import CostService
from ..db import ProjectDB
from ..export.video import VideoExporter
from ..matching.editing import apply_manual_selection
from ..matching.matcher import ShotMatcher
from ..models import (
    AnalysisTimeline,
    ApiCallRecord,
    DialogueSegment,
    ExportResult,
    MediaInfo,
    ProjectRecord,
    ProjectStatus,
    Script,
    ScriptSummary,
    Shot,
    ShotMatchPlan,
    StoryAnalysis,
    Voiceover,
    format_clock,
    load_timeline_json,
)
from ..pipeline.control import TaskControl
from ..pipeline.phase1 import AnalysisPipeline, AnalysisResult, ProgressUpdate
from ..tts.service import TTSService
from ..utils import open_in_explorer, open_with_default_app, truncate
from .dialogs import NewProjectDialog, SettingsDialog
from .export_panel import ExportPanel
from .log_bridge import LogEmitter, QtLogHandler
from .matching_panel import MatchingPanel
from .script_panel import ScriptPanel
from .shot_editor import ShotEditorDialog
from .story_panel import StoryPanel
from .theme import DARK_STYLESHEET
from .tts_panel import TTSPanel
from .worker import AITaskWorker, AnalysisWorker, MediaProbeWorker

LOGGER = logging.getLogger(__name__)

STAGE_LABELS = {
    "probe": "读取影片信息",
    "scenes": "镜头切分",
    "keyframes": "提取预览图",
    "speech": "语音识别",
    "finish": "保存结果",
}

STATUS_COLORS = {
    ProjectStatus.IMPORTED: "#c9c9c9",
    ProjectStatus.ANALYZING: "#7fb3ff",
    ProjectStatus.PAUSED: "#e0b25c",
    ProjectStatus.READY: "#6fcf97",
    ProjectStatus.FAILED: "#ff7a7a",
    ProjectStatus.CANCELLED: "#e0b25c",
}


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(
        self,
        config: AppConfig,
        db: ProjectDB,
        log_path: Path | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.config = config
        self.db = db
        self.log_path = log_path or (config.logs_dir() / "app.log")
        self.current_project: ProjectRecord | None = None
        self.media: MediaInfo | None = None
        self.shots: list[Shot] = []
        self.dialogues: list[DialogueSegment] = []
        self._timeline: AnalysisTimeline | None = None
        self.story_analysis: StoryAnalysis | None = None
        self.current_script: Script | None = None
        self.voiceover: Voiceover | None = None
        self.match_plan: ShotMatchPlan | None = None
        self.export_result: ExportResult | None = None
        self._preview_pixmap: QPixmap | None = None
        self.worker: AnalysisWorker | None = None
        self.control: TaskControl | None = None
        self.ai_worker: AITaskWorker | None = None
        self.ai_control: TaskControl | None = None
        self.ai_kind: str = ""
        self.probe_worker: MediaProbeWorker | None = None
        self._project_items: dict[str, QListWidgetItem] = {}
        self._pending_project_name: str = ""
        self._pending_source: Path | None = None
        self._pending_auto_start = False
        self._cancelling = False
        self._last_import_dir: Path = Path.home()

        self.setWindowTitle(f"{APP_NAME} v{__version__} — 本地分析 + AI 剧情/文案/配音/剪辑")
        self.resize(1320, 840)
        self.setMinimumSize(1080, 700)
        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        self._build_menu()
        self._setup_logging()
        self._update_cost_tab()
        self.refresh_projects()
        self._update_buttons()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, False)
        splitter.setSizes([290, 1000])
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)

        data_label = QLabel(f"数据目录：{self.config.resolved_data_dir()}")
        data_label.setToolTip(str(self.config.resolved_data_dir()))
        data_label.setMaximumWidth(680)
        self.statusBar().addPermanentWidget(data_label)
        self.statusBar().addPermanentWidget(QLabel(f"v{__version__}"))
        self.statusBar().showMessage("就绪")

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("LeftPanel")
        panel.setMinimumWidth(260)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("项目")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)

        self.new_button = QPushButton("＋ 新建项目")
        self.new_button.setObjectName("PrimaryButton")
        self.new_button.setToolTip("导入影片并创建项目")
        self.new_button.clicked.connect(self._new_project)
        layout.addWidget(self.new_button)

        self.project_list = QListWidget()
        self.project_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.project_list.setWordWrap(True)
        self.project_list.currentItemChanged.connect(self._on_project_selected)
        layout.addWidget(self.project_list, 1)

        self.refresh_button = QPushButton("刷新列表")
        self.refresh_button.clicked.connect(lambda: self.refresh_projects())
        layout.addWidget(self.refresh_button)

        hint = QLabel("影片只在本机处理；本工具不提供 DRM 破解、加密解密或版权规避功能。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._build_info_group())

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        progress_row.addWidget(self.progress, 1)
        layout.addLayout(progress_row)

        self.stage_label = QLabel("就绪：请新建项目或从左侧选择已有项目。")
        self.stage_label.setObjectName("HintLabel")
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_shots_tab(), "镜头")
        self.tabs.addTab(self._build_dialogues_tab(), "对白")
        self.story_panel = StoryPanel()
        self.story_panel.run_requested.connect(self._run_story_analysis)
        self.tabs.addTab(self.story_panel, "剧情理解")
        self.script_panel = ScriptPanel()
        self.script_panel.generate_requested.connect(
            lambda style_id, minutes, name, instruction: self._generate_script(
                style_id, minutes, name, instruction
            )
        )
        self.script_panel.version_selected.connect(self._preview_script_version)
        self.script_panel.restore_requested.connect(self._restore_script_version)
        self.script_panel.save_requested.connect(self._save_script_edit)
        self.tabs.addTab(self.script_panel, "解说文案")

        self.tts_panel = TTSPanel()
        self.tts_panel.generate_requested.connect(self._generate_voiceover)
        self.tts_panel.open_audio_requested.connect(self._open_voiceover_audio)
        self.tts_panel.open_dir_requested.connect(self._open_voiceover_dir)
        self.tabs.addTab(self.tts_panel, "配音")

        self.match_panel = MatchingPanel()
        self.match_panel.match_requested.connect(self._run_shot_matching)
        self.match_panel.edit_requested.connect(self._edit_shot_selection)
        self.tabs.addTab(self.match_panel, "镜头匹配")

        self.export_panel = ExportPanel()
        self.export_panel.export_requested.connect(self._start_export)
        self.export_panel.cancel_requested.connect(self._cancel_analysis)
        self.export_panel.open_output_requested.connect(self._open_export_output)
        self.export_panel.open_dir_requested.connect(self._open_export_dir)
        self.export_panel.open_subtitle_requested.connect(self._open_export_subtitle)
        self.tabs.addTab(self.export_panel, "剪辑合成")

        self.tabs.addTab(self._build_summary_tab(), "分析摘要")
        self.tabs.addTab(self._build_cost_tab(), "费用统计")
        self.tabs.addTab(self._build_log_tab(), "错误日志")
        layout.addWidget(self.tabs, 1)
        return panel

    def _build_info_group(self) -> QGroupBox:
        group = QGroupBox("项目概览")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)

        self.name_label = QLabel("未选择项目")
        self.name_label.setObjectName("ProjectName")
        grid.addWidget(self.name_label, 0, 0, 1, 4)

        self.source_label = QLabel("—")
        self.source_label.setObjectName("HintLabel")
        self.source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self.source_label, 1, 0, 1, 4)

        self.meta_label = QLabel("尚未导入影片")
        self.meta_label.setObjectName("HintLabel")
        grid.addWidget(self.meta_label, 2, 0, 1, 2)

        self.status_label = QLabel("状态：—")
        self.status_label.setObjectName("StatusBadge")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.status_label, 2, 2, 1, 2)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = QPushButton("开始分析")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setToolTip("镜头切分 + 本地对白识别")
        self.start_button.clicked.connect(self._start_analysis)
        buttons.addWidget(self.start_button)

        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self._pause_analysis)
        buttons.addWidget(self.pause_button)

        self.resume_button = QPushButton("继续")
        self.resume_button.clicked.connect(self._resume_analysis)
        buttons.addWidget(self.resume_button)

        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.clicked.connect(self._cancel_analysis)
        buttons.addWidget(self.cancel_button)

        buttons.addSpacing(16)
        self.open_video_button = QPushButton("打开影片")
        self.open_video_button.clicked.connect(self._open_source_video)
        buttons.addWidget(self.open_video_button)

        self.open_dir_button = QPushButton("项目目录")
        self.open_dir_button.clicked.connect(self._open_project_dir)
        buttons.addWidget(self.open_dir_button)

        self.export_button = QPushButton("导出时间轴")
        self.export_button.clicked.connect(self._export_timeline)
        buttons.addWidget(self.export_button)
        buttons.addStretch(1)
        grid.addLayout(buttons, 3, 0, 1, 4)
        return group

    def _build_shots_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.shot_table = QTableWidget(0, 5)
        self.shot_table.setHorizontalHeaderLabels(["序号", "开始", "结束", "时长(秒)", "关键帧"])
        self.shot_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.shot_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.shot_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.shot_table.setAlternatingRowColors(True)
        self.shot_table.verticalHeader().setVisible(False)
        header = self.shot_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.shot_table.currentCellChanged.connect(self._on_shot_row_changed)
        layout.addWidget(self.shot_table, 3)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_title = QLabel("镜头预览")
        preview_title.setObjectName("PanelTitle")
        preview_layout.addWidget(preview_title)
        self.preview_label = QLabel("选择镜头查看预览")
        self.preview_label.setObjectName("PreviewLabel")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedSize(384, 216)
        preview_layout.addWidget(self.preview_label)
        self.shot_detail_label = QLabel("—")
        self.shot_detail_label.setWordWrap(True)
        self.shot_detail_label.setObjectName("HintLabel")
        preview_layout.addWidget(self.shot_detail_label)
        preview_layout.addStretch(1)
        layout.addWidget(preview_panel, 2)
        return widget

    def _build_dialogues_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        filter_row = QHBoxLayout()
        self.dialogue_search = QLineEdit()
        self.dialogue_search.setPlaceholderText("搜索对白文本…")
        self.dialogue_search.textChanged.connect(self._filter_dialogues)
        filter_row.addWidget(self.dialogue_search, 1)
        self.dialogue_count_label = QLabel("共 0 句")
        self.dialogue_count_label.setObjectName("HintLabel")
        filter_row.addWidget(self.dialogue_count_label)
        layout.addLayout(filter_row)

        self.dialogue_table = QTableWidget(0, 4)
        self.dialogue_table.setHorizontalHeaderLabels(["序号", "开始", "结束", "对白"])
        self.dialogue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.dialogue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.dialogue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.dialogue_table.setAlternatingRowColors(True)
        self.dialogue_table.verticalHeader().setVisible(False)
        dialogue_header = self.dialogue_table.horizontalHeader()
        dialogue_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dialogue_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dialogue_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dialogue_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.dialogue_table, 1)

        hint = QLabel(
            "对白由 faster-whisper 在本机识别，可能存在错字或时间偏差；第三阶段开始用于配音与字幕前，请人工校对。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return widget

    def _build_summary_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        self.summary_view = QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setPlaceholderText("选择项目后显示影片信息、镜头/对白统计和注意事项。")
        layout.addWidget(self.summary_view, 1)
        return widget

    def _build_cost_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        self.cost_view = QTextBrowser()
        self.cost_view.setOpenExternalLinks(False)
        layout.addWidget(self.cost_view, 1)
        return widget

    def _build_log_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        row = QHBoxLayout()
        hint = QLabel("运行日志（API Key 已自动脱敏）")
        hint.setObjectName("HintLabel")
        row.addWidget(hint, 1)
        reload_button = QPushButton("重新读取")
        reload_button.clicked.connect(self._load_log_tail)
        row.addWidget(reload_button)
        clear_button = QPushButton("清空显示")
        clear_button.clicked.connect(lambda: self.log_view.clear())
        row.addWidget(clear_button)
        open_button = QPushButton("打开日志文件")
        open_button.clicked.connect(self._open_log_file)
        row.addWidget(open_button)
        layout.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view, 1)
        return widget

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        self.action_new = QAction("新建项目…", self)
        self.action_new.setShortcut("Ctrl+N")
        self.action_new.triggered.connect(self._new_project)
        file_menu.addAction(self.action_new)

        self.action_export = QAction("导出时间轴 JSON…", self)
        self.action_export.setShortcut("Ctrl+E")
        self.action_export.triggered.connect(self._export_timeline)
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.setShortcut("Ctrl+Q")
        action_quit.triggered.connect(self.close)
        file_menu.addAction(action_quit)

        tools_menu = self.menuBar().addMenu("工具(&T)")
        self.action_settings = QAction("设置…", self)
        self.action_settings.setShortcut("Ctrl+,")
        self.action_settings.triggered.connect(self._open_settings)
        tools_menu.addAction(self.action_settings)
        tools_menu.addSeparator()

        action_data_dir = QAction("打开数据目录", self)
        action_data_dir.triggered.connect(self._open_data_dir)
        tools_menu.addAction(action_data_dir)
        action_log = QAction("打开日志文件", self)
        action_log.triggered.connect(self._open_log_file)
        tools_menu.addAction(action_log)
        action_reload = QAction("重新读取日志", self)
        action_reload.triggered.connect(self._load_log_tail)
        tools_menu.addAction(action_reload)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        action_about = QAction("关于", self)
        action_about.triggered.connect(self._show_about)
        help_menu.addAction(action_about)

    # ------------------------------------------------------------------ 日志
    def _setup_logging(self) -> None:
        self.log_emitter = LogEmitter(self)
        self.log_handler = QtLogHandler(self.log_emitter)
        self.log_emitter.message.connect(self._append_log)
        logging.getLogger().addHandler(self.log_handler)
        self._load_log_tail()

    def _load_log_tail(self) -> None:
        try:
            if self.log_path.exists():
                lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = lines[-400:]
                self.log_view.setPlainText("\n".join(tail))
                self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
        except OSError as exc:
            LOGGER.warning("读取日志文件失败：%s", exc)

    def _append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def _open_log_file(self) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.touch(exist_ok=True)
            open_with_default_app(self.log_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开日志", str(exc))

    # ------------------------------------------------------------------ 项目列表
    def refresh_projects(self, select_id: str | None = None) -> None:
        try:
            projects = self.db.list_projects()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取项目列表失败")
            QMessageBox.critical(self, "读取项目失败", str(exc))
            return
        target_id = select_id or (self.current_project.id if self.current_project else None)
        self.project_list.blockSignals(True)
        self.project_list.clear()
        self._project_items.clear()
        for project in projects:
            item = QListWidgetItem(
                f"{project.name}\n"
                f"{project.status_label} · {project.duration_label} · "
                f"{project.shot_count} 镜头 / {project.dialogue_count} 句"
            )
            item.setData(Qt.ItemDataRole.UserRole, project.id)
            item.setToolTip(f"{project.source_path}\n状态：{project.status_label}")
            item.setSizeHint(QSize(0, 64))
            self.project_list.addItem(item)
            self._project_items[project.id] = item
        self.project_list.blockSignals(False)

        target_project = next((project for project in projects if project.id == target_id), None)
        if target_project is None and projects:
            target_project = projects[0]
        if target_project is not None:
            item = self._project_items.get(target_project.id)
            if item is not None:
                self.project_list.blockSignals(True)
                self.project_list.setCurrentItem(item)
                self.project_list.blockSignals(False)
            self._load_project(target_project)
        else:
            self._clear_project()

    def _on_project_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None
    ) -> None:
        if current is None:
            return
        project_id = current.data(Qt.ItemDataRole.UserRole)
        if not project_id:
            return
        try:
            project = self.db.get_project(str(project_id))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取项目失败")
            QMessageBox.critical(self, "读取项目失败", str(exc))
            return
        if project is not None:
            self._load_project(project)

    def _load_project(self, project: ProjectRecord) -> None:
        self.current_project = project
        self.name_label.setText(project.name)
        self.source_label.setText(truncate(project.source_path, 110))
        self.source_label.setToolTip(project.source_path)
        self.meta_label.setText(
            f"{project.duration_label} · {project.resolution_label} · "
            f"{project.shot_count} 镜头 · {project.dialogue_count} 句对白"
        )
        self._set_status(f"状态：{project.status_label}", STATUS_COLORS.get(project.status, "#c9c9c9"))
        self.setWindowTitle(f"{APP_NAME} — {project.name}")

        try:
            self.media = self.db.get_media(project.id)
            self.shots = self.db.get_shots(project.id)
            self.dialogues = self.db.get_dialogues(project.id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取项目数据失败")
            QMessageBox.critical(self, "读取项目数据失败", str(exc))
            self.media = None
            self.shots = []
            self.dialogues = []
        self._timeline = None
        timeline_path = Path(project.project_dir) / "timeline.json"
        if timeline_path.exists():
            try:
                self._timeline = load_timeline_json(timeline_path)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("读取时间轴快照失败：%s", exc)

        self._load_ai_data(project)
        self.preview_label.clear()
        self.preview_label.setText("选择镜头查看预览")
        self.shot_detail_label.setText("—")
        self._populate_shots()
        self._populate_dialogues()
        self._update_summary()
        self._update_cost_tab()
        self._update_buttons()
        self.start_button.setText("重新分析" if project.shot_count else "开始分析")
        if not self._busy():
            if project.status == ProjectStatus.READY:
                self.progress.setValue(100)
                self.stage_label.setText(
                    f"分析完成：{len(self.shots)} 个镜头、{len(self.dialogues)} 句对白。"
                    "可重新分析、校对结果或导出时间轴。"
                )
            elif project.status == ProjectStatus.FAILED:
                self.progress.setValue(0)
                self.stage_label.setText("上次分析失败：请查看“错误日志”，修正后重新分析。")
            else:
                self.progress.setValue(0)
                self.stage_label.setText("就绪：点击“开始分析”进行镜头切分和本地对白识别。")

    def _load_ai_data(self, project: ProjectRecord) -> None:
        """读取剧情分析、文案版本并刷新两个 AI 标签页。"""
        self.story_analysis = None
        self.current_script = None
        self.voiceover = None
        self.match_plan = None
        self.export_result = None
        summaries: list[ScriptSummary] = []
        current: Script | None = None
        try:
            self.story_analysis = self.db.get_story_analysis(project.id)
            summaries = self.db.list_scripts(project.id)
            current = self.db.get_current_script(project.id)
            if current is None and summaries:
                current = self.db.get_script(summaries[0].id)
            self.voiceover = self.db.get_current_voiceover(project.id)
            self.match_plan = self.db.get_current_shot_match(project.id)
            self.export_result = self.db.get_current_export(project.id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取剧情/文案数据失败")
            LOGGER.warning("AI 数据读取失败：%s", exc)
        self.story_panel.set_analysis(self.story_analysis)
        stale = False
        if (
            self.story_analysis is not None
            and self.story_analysis.timeline_generated_at is not None
            and self._timeline is not None
            and abs(
                (
                    self.story_analysis.timeline_generated_at
                    - self._timeline.generated_at
                ).total_seconds()
            )
            > 1.0
        ):
            stale = True
        self.story_panel.set_stale(stale)
        self.script_panel.clear()
        self.script_panel.set_analysis_available(self.story_analysis is not None)
        self.script_panel.set_scripts(
            summaries, current.id if current is not None else None
        )
        if current is not None:
            self.current_script = current
            self.script_panel.load_script(current)
            if (
                self.story_analysis is not None
                and current.analysis_created_at is not None
                and abs(
                    (current.analysis_created_at - self.story_analysis.created_at).total_seconds()
                )
                > 1.0
            ):
                self.script_panel.set_status(
                    f"当前 v{current.version} 基于旧剧情分析；建议重新生成并人工核对。"
                )

        voiceover_valid = (
            self.voiceover is not None
            and current is not None
            and self.voiceover.script_id == current.id
        )
        self.tts_panel.set_config(self.config.tts)
        self.tts_panel.set_script_available(current is not None)
        self.tts_panel.set_voiceover(self.voiceover)
        self.tts_panel.set_stale(self.voiceover is not None and not voiceover_valid)
        self.match_panel.set_plan(self.match_plan, self.shots)
        self.match_panel.set_ready(current is not None, voiceover_valid)
        match_stale = bool(
            self.match_plan is not None
            and (
                current is None
                or self.match_plan.script_id != current.id
                or self.voiceover is None
                or self.match_plan.voiceover_id != self.voiceover.id
            )
        )
        self.match_panel.set_stale(match_stale)

        self.export_panel.set_config(self.config.export)
        self.export_panel.set_result(self.export_result)
        export_ready = bool(
            current is not None
            and voiceover_valid
            and self.match_plan is not None
            and self.voiceover is not None
            and self.match_plan.voiceover_id == self.voiceover.id
            and self.match_plan.script_id == current.id
        )
        if export_ready and self.voiceover is not None and self.match_plan is not None:
            summary = (
                f"可导出：{self.voiceover.duration_label} · "
                f"{self.match_plan.selection_count} 句 · "
                f"{'字幕烧录' if self.config.export.subtitle_mode == 'burn' else '无字幕'}"
            )
            if self.export_result is not None:
                if self.export_result.plan_id == self.match_plan.id:
                    summary += f" · 上次 {self.export_result.size_label}"
                else:
                    summary += " · 当前方案已变化，建议重新导出"
            self.export_panel.set_ready(True, summary)
        else:
            self.export_panel.set_ready(False)

    def _clear_project(self) -> None:
        self.current_project = None
        self.media = None
        self.shots = []
        self.dialogues = []
        self._timeline = None
        self.story_analysis = None
        self.current_script = None
        self.voiceover = None
        self.match_plan = None
        self.export_result = None
        if hasattr(self, "story_panel"):
            self.story_panel.set_analysis(None)
            self.script_panel.clear()
            self.script_panel.set_analysis_available(False)
            self.tts_panel.set_voiceover(None)
            self.tts_panel.set_script_available(False)
            self.match_panel.set_plan(None, [])
            self.match_panel.set_ready(False, False)
            self.export_panel.set_result(None)
            self.export_panel.set_ready(False)
        self.name_label.setText("未选择项目")
        self.source_label.setText("—")
        self.meta_label.setText("尚未导入影片")
        self._set_status("状态：—", "#9aa0aa")
        self.shot_table.setRowCount(0)
        self.dialogue_table.setRowCount(0)
        self.dialogue_count_label.setText("共 0 句")
        self.preview_label.clear()
        self.preview_label.setText("选择镜头查看预览")
        self.shot_detail_label.setText("—")
        self.summary_view.setPlainText("请新建项目或从左侧选择项目。")
        self._update_buttons()
        self.setWindowTitle(f"{APP_NAME} v{__version__} — 本地分析 + AI 剧情/文案/配音/剪辑")

    def _set_status(self, text: str, color: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    # ------------------------------------------------------------------ 表格
    def _populate_shots(self) -> None:
        table = self.shot_table
        table.setUpdatesEnabled(False)
        table.clearContents()
        table.setRowCount(len(self.shots))
        for row, shot in enumerate(self.shots):
            values = [
                str(shot.index),
                f"{shot.start_s:.2f}",
                f"{shot.end_s:.2f}",
                f"{shot.duration_s:.2f}",
                "有" if shot.keyframe_path else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif column in (1, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(f"镜头 #{shot.index}\n{shot.start_s:.3f}s → {shot.end_s:.3f}s")
                table.setItem(row, column, item)
        table.setUpdatesEnabled(True)
        if self.shots:
            table.setCurrentCell(0, 0)

    def _on_shot_row_changed(self, current_row: int, _column: int, _prev_row: int, _prev_col: int) -> None:
        if current_row < 0 or current_row >= len(self.shots):
            self.preview_label.clear()
            self.preview_label.setText("选择镜头查看预览")
            self.shot_detail_label.setText("—")
            return
        shot = self.shots[current_row]
        self.shot_detail_label.setText(
            f"镜头 #{shot.index}\n"
            f"{self._format_seconds(shot.start_s)} → {self._format_seconds(shot.end_s)}\n"
            f"时长 {shot.duration_s:.2f} 秒"
        )
        self._preview_pixmap = None
        if shot.keyframe_path:
            path = Path(shot.keyframe_path)
            if path.exists():
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    self._preview_pixmap = pixmap
                    self.preview_label.setPixmap(
                        pixmap.scaled(
                            self.preview_label.size(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                    return
        self.preview_label.clear()
        self.preview_label.setText("暂无预览图\n（关键帧可能被关闭或提取失败）")

    def _populate_dialogues(self) -> None:
        table = self.dialogue_table
        table.setUpdatesEnabled(False)
        table.clearContents()
        table.setRowCount(len(self.dialogues))
        for row, segment in enumerate(self.dialogues):
            values = [
                str(segment.index),
                f"{segment.start_s:.2f}",
                f"{segment.end_s:.2f}",
                segment.text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif column in (1, 2):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(segment.text)
                table.setItem(row, column, item)
        table.setUpdatesEnabled(True)
        self.dialogue_count_label.setText(f"共 {len(self.dialogues)} 句")
        self._filter_dialogues(self.dialogue_search.text())

    def _filter_dialogues(self, text: str) -> None:
        keyword = text.strip()
        visible = 0
        for row in range(self.dialogue_table.rowCount()):
            item = self.dialogue_table.item(row, 3)
            match = not keyword or (item is not None and keyword in item.text())
            self.dialogue_table.setRowHidden(row, not match)
            if match:
                visible += 1
        if keyword:
            self.dialogue_count_label.setText(f"匹配 {visible} / {len(self.dialogues)} 句")
        else:
            self.dialogue_count_label.setText(f"共 {len(self.dialogues)} 句")

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        minutes = int(seconds // 60)
        remainder = seconds - minutes * 60
        return f"{minutes:02d}:{remainder:05.2f}"

    # ------------------------------------------------------------------ 摘要/费用
    def _update_summary(self) -> None:
        if self.current_project is None:
            self.summary_view.setPlainText("请新建项目或从左侧选择项目。")
            return
        project = self.current_project
        lines: list[str] = [
            f"项目：{project.name}",
            f"项目 ID：{project.id}",
            f"源文件：{project.source_path}",
            f"状态：{project.status_label}",
            f"创建时间：{project.created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
            f"更新时间：{project.updated_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if project.error:
            lines.extend(["", f"错误信息：{project.error}"])
        if self.media is not None:
            media = self.media
            lines.extend(
                [
                    "",
                    "── 影片信息 ──",
                    f"时长：{media.duration_label}（{media.duration_s:.2f} 秒）",
                    f"分辨率：{media.resolution_label}    帧率：{media.fps_label} fps",
                    f"视频编码：{media.video_codec or '未知'}    像素格式：{media.pix_fmt or '未知'}",
                    f"音频：{media.audio_label}",
                    f"容器：{media.format_name or '未知'}    文件大小：{media.size_bytes or 0} 字节",
                    f"内嵌字幕轨：{len(media.subtitle_streams)} 条",
                ]
            )
            for subtitle in media.subtitle_streams:
                lines.append(
                    f"  · 轨道 {subtitle.index}：{subtitle.codec or '未知编码'}"
                    f" / {subtitle.language or '未知语言'}"
                    + (f" / {subtitle.title}" if subtitle.title else "")
                )
        else:
            lines.extend(["", "影片信息尚未读取。"])
        lines.extend(
            [
                "",
                "── 分析结果 ──",
                f"镜头数量：{len(self.shots)}",
                f"对白数量：{len(self.dialogues)}",
            ]
        )
        if self.story_analysis is not None:
            script_count = 0
            try:
                script_count = self.db.count_scripts(project.id)
            except Exception:  # noqa: BLE001
                pass
            lines.extend(
                [
                    "",
                    "── AI 剧情/文案 ──",
                    f"剧情分析：{self.story_analysis.segment_count} 段，"
                    f"{len(self.story_analysis.characters)} 个人物，"
                    f"费用约 ¥{self.story_analysis.estimated_cost_cny:.4f}",
                    f"文案版本：{script_count} 个"
                    + (
                        f"，当前 v{self.current_script.version}"
                        if self.current_script is not None
                        else ""
                    ),
                ]
            )
            if self.voiceover is not None:
                lines.append(
                    f"配音：{self.voiceover.unit_count} 句 / "
                    f"{self.voiceover.duration_label} / {self.voiceover.engine}"
                )
            if self.match_plan is not None:
                lines.append(
                    f"镜头匹配：{self.match_plan.selection_count} 句，"
                    f"平均置信度 {self.match_plan.average_confidence:.2f}，"
                    f"{self.match_plan.low_confidence_count} 句需审核"
                )
        if self._timeline is not None:
            if self._timeline.scene_backend:
                lines.append(f"镜头检测后端：{self._timeline.scene_backend}")
            if self._timeline.speech_engine:
                lines.append(f"语音识别引擎：{self._timeline.speech_engine}")
            lines.append(f"时间轴快照：{Path(project.project_dir) / 'timeline.json'}")
            if self._timeline.warnings:
                lines.extend(["", "── 注意事项 ──"])
                lines.extend(f"· {warning}" for warning in self._timeline.warnings)
        else:
            lines.extend(["", "尚未生成时间轴快照，请先完成分析。"])
        try:
            runs = self.db.recent_runs(project.id, limit=5)
        except Exception:  # noqa: BLE001
            runs = []
        if runs:
            lines.extend(["", "── 最近运行 ──"])
            for run in runs:
                lines.append(
                    f"· {run.get('stage')} / {run.get('status')} / {run.get('started_at')}"
                    + (f" — {run.get('message')}" if run.get("message") else "")
                )
        self.summary_view.setPlainText("\n".join(lines))

    def _update_cost_tab(self) -> None:
        cost_service = CostService(self.config, self.db)
        project_id = self.current_project.id if self.current_project else None
        try:
            project_summary = cost_service.snapshot(project_id) if project_id else None
            global_summary = cost_service.snapshot(None)
            calls = cost_service.recent_calls(project_id, limit=20)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("读取费用统计失败：%s", exc)
            project_summary = None
            global_summary = None
            calls = []
        key_status = (
            "已配置（只显示状态，不显示内容）"
            if has_deepseek_api_key(self.config)
            else "未配置"
        )
        budget = self.config.deepseek.budget_cny_per_project
        budget_text = "不限制" if budget <= 0 else f"¥{budget:.2f}"
        project_lines = ""
        if project_summary is not None:
            remaining = project_summary.remaining_cny
            remaining_text = "不限制" if remaining is None else f"¥{remaining:.4f}"
            project_lines = f"""
            <h3>当前项目</h3>
            <table cellpadding="3">
              <tr><td>单条视频制作成本</td><td><b>¥{project_summary.spent_cny:.4f}</b></td></tr>
              <tr><td>预算 / 剩余</td><td>{budget_text} / {remaining_text}</td></tr>
              <tr><td>调用次数 / 缓存命中</td><td>{project_summary.calls} / {project_summary.cached_calls}
                  （命中率 {project_summary.cache_hit_rate:.0%}）</td></tr>
              <tr><td>Token（输入/输出）</td><td>{project_summary.prompt_tokens} / {project_summary.completion_tokens}</td></tr>
              <tr><td>失败调用</td><td>{project_summary.failed_calls}</td></tr>
            </table>
            """
        global_lines = f"""
        <p><b>全局累计：</b>¥{global_summary.spent_cny:.4f} ·
        {global_summary.calls} 次调用 · {global_summary.total_tokens} tokens</p>
        """ if global_summary is not None else ""

        rows: list[str] = []
        for call in calls:
            if call.cached:
                kind = "缓存"
            elif call.status != "success":
                kind = "失败"
            elif call.tokens_estimated:
                kind = "估算"
            else:
                kind = "实时"
            rows.append(
                "<tr>"
                f"<td>{html.escape(call.started_at.astimezone().strftime('%m-%d %H:%M:%S'))}</td>"
                f"<td>{html.escape(call.purpose)}</td>"
                f"<td>{html.escape(call.model)}</td>"
                f"<td>{call.prompt_tokens}+{call.completion_tokens}</td>"
                f"<td>¥{call.cost_cny:.4f}</td>"
                f"<td>{kind}</td>"
                "</tr>"
            )
        calls_html = (
            "<table border='0' cellspacing='0' cellpadding='4' width='100%'>"
            "<tr style='color:#aeb4bf'><th align='left'>时间</th><th align='left'>用途</th>"
            "<th align='left'>模型</th><th align='left'>Token</th><th align='left'>费用</th>"
            "<th align='left'>类型</th></tr>"
            + "".join(rows)
            + "</table>"
            if rows
            else "<p style='color:#9aa0aa'>尚无 DeepSeek 调用记录。未配置 Key 或未运行 AI 任务时不会产生费用。</p>"
        )
        content = f"""
        <h2>费用统计（DeepSeek AI）</h2>
        <p>费用根据接口返回的 token usage 与本机配置的单价估算，实际账单以 DeepSeek 为准；
        缓存命中不重复计费。本页不使用模拟数据伪造用量。<br>
        配音（Edge/Piper）和镜头匹配在本机运行，不产生 DeepSeek API 费用。</p>
        {project_lines}
        {global_lines}
        <h3>DeepSeek 配置</h3>
        <ul>
            <li>接口地址：{html.escape(self.config.deepseek.base_url)}</li>
            <li>文本模型：{html.escape(self.config.deepseek.model)}</li>
            <li>API Key：{key_status}</li>
            <li>单项目预算：{budget_text}</li>
            <li>单价：输入 ¥{self.config.deepseek.price_input_cny_per_mtok:.4f} /
                输出 ¥{self.config.deepseek.price_output_cny_per_mtok:.4f} 每百万 tokens</li>
            <li>缓存：{'开启' if self.config.deepseek.cache_enabled else '关闭'}</li>
        </ul>
        <h3>最近调用</h3>
        {calls_html}
        <p style="color:#9aa0aa">API Key 只从环境变量读取，不会写入配置文件或日志。
        预算上限为 0 时表示不限制；预计超支时程序会先暂停并要求确认。</p>
        """
        self.cost_view.setHtml(content)

    # ------------------------------------------------------------------ 新建/导入
    def _new_project(self) -> None:
        if self._busy():
            return
        dialog = NewProjectDialog(default_dir=self._last_import_dir, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, source = dialog.values()
        try:
            ffprobe_path = resolve_executable(self.config.ffprobe_path, "ffprobe")
        except FileNotFoundError as exc:
            QMessageBox.critical(self, "找不到 ffprobe", str(exc))
            return
        self._pending_project_name = name
        self._pending_source = source
        self._pending_auto_start = False
        self.progress.setRange(0, 0)
        self.stage_label.setText("正在读取影片信息…")
        self.statusBar().showMessage("正在读取影片信息…")
        self.probe_worker = MediaProbeWorker(source, ffprobe_path)
        self.probe_worker.succeeded.connect(self._on_media_probed)
        self.probe_worker.failed.connect(self._on_media_probe_failed)
        self.probe_worker.finished.connect(self._on_probe_finished)
        self.probe_worker.start()
        self._update_buttons()

    def _on_media_probed(self, media: MediaInfo) -> None:
        if not self._pending_source or not self._pending_project_name:
            return
        source = self._pending_source
        name = self._pending_project_name
        try:
            project_dir = self._make_project_dir(source)
            project = self.db.create_project(name, source, project_dir)
            self.db.save_media(project.id, media)
            LOGGER.info(
                "导入影片：%s | %s | %s | %s",
                name,
                media.resolution_label,
                media.duration_label,
                media.audio_label,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("创建项目失败")
            QMessageBox.critical(self, "创建项目失败", str(exc))
            return
        finally:
            self._pending_source = None
            self._pending_project_name = ""
        self._last_import_dir = source.parent
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText("影片导入完成，等待开始分析。")
        self.refresh_projects(select_id=project.id)
        if self.config.ui.auto_start_analysis:
            start_now = True
        else:
            answer = QMessageBox.question(
                self,
                "导入完成",
                f"已读取影片信息：\n{media.resolution_label} · {media.duration_label} · {media.audio_label}\n\n"
                "是否立即开始镜头切分和本地对白识别？\n"
                "（首次使用 Whisper 模型需要联网下载；可以在设置中更换模型。）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            start_now = answer == QMessageBox.StandardButton.Yes
        if start_now:
            self._pending_auto_start = True
            if self.probe_worker is None or not self.probe_worker.isRunning():
                QTimer.singleShot(0, self._start_analysis)

    def _on_media_probe_failed(self, message: str) -> None:
        self._pending_source = None
        self._pending_project_name = ""
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText("影片读取失败")
        LOGGER.error("读取影片信息失败：%s", message)
        QMessageBox.critical(self, "无法读取影片", message)

    def _on_probe_finished(self) -> None:
        self.probe_worker = None
        self.progress.setRange(0, 100)
        if self._pending_auto_start:
            self._pending_auto_start = False
            QTimer.singleShot(0, self._start_analysis)
        self._update_buttons()

    def _make_project_dir(self, source: Path) -> Path:
        safe_stem = "".join(
            character
            for character in source.stem
            if character not in '<>:"/\\|?*' and character.isprintable()
        )[:24]
        safe_stem = safe_stem or "movie"
        directory = (
            self.config.projects_dir()
            / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_stem}-{uuid4().hex[:6]}"
        )
        directory.mkdir(parents=True, exist_ok=False)
        return directory

    # ------------------------------------------------------------------ 分析
    def _start_analysis(self) -> None:
        if self.current_project is None or self._busy():
            return
        project = self.current_project
        if not Path(project.source_path).exists():
            QMessageBox.critical(self, "影片不存在", f"找不到源影片：\n{project.source_path}\n\n请检查文件是否被移动。")
            return
        # “开始分析/重新分析”按钮本身就是用户确认；导入时另有一次可关闭的询问。
        self.control = TaskControl()
        self.worker = AnalysisWorker(AnalysisPipeline(self.db, self.config), project.id, self.control)
        self.worker.progress.connect(self._on_analysis_progress)
        self.worker.logged.connect(self._on_analysis_log)
        self.worker.succeeded.connect(self._on_analysis_succeeded)
        self.worker.failed.connect(self._on_analysis_failed)
        self.worker.cancelled.connect(self._on_analysis_cancelled)
        self.worker.finished.connect(self._on_analysis_thread_finished)
        self._cancelling = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText("准备分析…")
        self.statusBar().showMessage(f"正在分析：{project.name}")
        self.tabs.setCurrentWidget(self.shot_table.parentWidget())
        try:
            self.db.update_project(project.id, status=ProjectStatus.ANALYZING, error=None)
            self.current_project = self.db.get_project(project.id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("更新项目状态失败：%s", exc)
        self._set_status("状态：分析中", STATUS_COLORS[ProjectStatus.ANALYZING])
        self.worker.start()
        self._update_buttons()

    def _pause_analysis(self) -> None:
        control = self._active_control()
        if control is None:
            return
        phase1_running = self.worker is not None and self.worker.isRunning()
        control.pause()
        self.stage_label.setText("已请求暂停：当前步骤/当前模型调用结束并到达检查点后暂停。")
        if phase1_running and self.current_project is not None:
            try:
                self.db.update_project(self.current_project.id, status=ProjectStatus.PAUSED)
                self.current_project = self.db.get_project(self.current_project.id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("更新暂停状态失败：%s", exc)
            self._set_status("状态：已暂停", STATUS_COLORS[ProjectStatus.PAUSED])
        self._update_buttons()

    def _resume_analysis(self) -> None:
        control = self._active_control()
        if control is None:
            return
        phase1_running = self.worker is not None and self.worker.isRunning()
        control.resume()
        if phase1_running and self.current_project is not None:
            try:
                self.db.update_project(self.current_project.id, status=ProjectStatus.ANALYZING)
                self.current_project = self.db.get_project(self.current_project.id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("更新继续状态失败：%s", exc)
            self._set_status("状态：分析中", STATUS_COLORS[ProjectStatus.ANALYZING])
        self.stage_label.setText("继续任务…")
        self._update_buttons()

    def _cancel_analysis(self) -> None:
        control = self._active_control()
        if control is None:
            return
        self._cancelling = True
        control.cancel()
        self.stage_label.setText("正在取消任务…（等待当前模型调用/步骤结束）")
        self._update_buttons()

    def _on_analysis_progress(self, update: ProgressUpdate) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(update.percent)
        stage = STAGE_LABELS.get(update.stage, update.stage)
        self.stage_label.setText(f"{stage} · {update.message}")

    def _on_analysis_log(self, message: str) -> None:
        LOGGER.debug("流水线：%s", message)

    def _on_analysis_succeeded(self, result: AnalysisResult) -> None:
        self._cancelling = False
        self.progress.setValue(100)
        self.stage_label.setText(
            f"分析完成：{result.shot_count} 个镜头、{result.dialogue_count} 句对白，"
            f"耗时 {result.elapsed_s:.1f} 秒。"
        )
        self.statusBar().showMessage("分析完成", 8000)
        self.refresh_projects(select_id=result.project_id)
        message = (
            f"镜头：{result.shot_count} 个\n"
            f"对白：{result.dialogue_count} 句\n"
            f"耗时：{result.elapsed_s:.1f} 秒\n"
            f"镜头检测：{result.scene_backend}\n"
            f"语音识别：{result.speech_engine}"
        )
        if result.warnings:
            message += "\n\n注意事项：\n" + "\n".join(
                f"· {warning}" for warning in result.warnings[:8]
            )
            if len(result.warnings) > 8:
                message += f"\n… 其余 {len(result.warnings) - 8} 条见“分析摘要”。"
        message += "\n\n请在“镜头”和“对白”页核对识别结果。"
        QMessageBox.information(self, "分析完成", message)

    def _on_analysis_failed(self, message: str) -> None:
        self._cancelling = False
        self.stage_label.setText("分析失败，请查看日志。")
        self.statusBar().showMessage("分析失败", 8000)
        if self.current_project is not None:
            self.refresh_projects(select_id=self.current_project.id)
        QMessageBox.critical(self, "分析失败", message + "\n\n详细信息见“错误日志”页。")

    def _on_analysis_cancelled(self) -> None:
        self._cancelling = False
        self.stage_label.setText("任务已取消，已保存的部分结果仍保留，可重新分析。")
        self.statusBar().showMessage("任务已取消", 8000)
        if self.current_project is not None:
            self.refresh_projects(select_id=self.current_project.id)

    def _on_analysis_thread_finished(self) -> None:
        self.worker = None
        self.control = None
        self._cancelling = False
        self._update_buttons()

    # ------------------------------------------------------------------ 第二阶段 AI
    def _check_ai_preconditions(self, estimate_cny: float, action: str) -> tuple[bool, bool]:
        """检查 Key 与预算。返回 (是否继续, 是否允许超预算)。"""
        if self.current_project is None:
            return False, False
        if not has_deepseek_api_key(self.config):
            QMessageBox.warning(
                self,
                "未配置 DeepSeek API Key",
                f"请先设置环境变量 {self.config.deepseek.api_key_env}。\n\n"
                "Key 只从环境变量读取，不会写入配置文件或日志。设置后请重启程序。",
            )
            return False, False
        snapshot = CostService(self.config, self.db).snapshot(self.current_project.id)
        budget = snapshot.budget_cny
        if budget <= 0:
            return True, False
        if snapshot.spent_cny + max(0.0, estimate_cny) <= budget + 1e-9:
            return True, False
        answer = QMessageBox.question(
            self,
            "预算预警",
            f"{action}预计费用约 ¥{estimate_cny:.4f}。\n"
            f"项目已用 ¥{snapshot.spent_cny:.4f}，预算上限 ¥{budget:.2f}。\n\n"
            "继续可能超出预算。是否确认继续（超出部分由你自行承担）？\n"
            "选择“否”则不会发起新的付费调用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            return True, True
        return False, False

    def _run_story_analysis(self) -> None:
        if self._busy() or self.current_project is None:
            return
        if not self.dialogues:
            QMessageBox.warning(
                self,
                "没有对白",
                "项目还没有对白数据，无法基于文本分析剧情。请先完成第一阶段语音识别。",
            )
            return
        cost_service = CostService(self.config, self.db)
        estimate = cost_service.estimate_story_cost(self.dialogues)
        proceed, allow_over = self._check_ai_preconditions(estimate, "剧情分析")
        if not proceed:
            return
        project_id = self.current_project.id
        self.ai_kind = "story"
        self.ai_control = TaskControl()
        analyzer = StoryAnalyzer(self.db, self.config)
        runner = lambda control, progress, log: analyzer.run(  # noqa: E731
            project_id,
            control=control,
            on_progress=progress,
            on_log=log,
            allow_over_budget=allow_over,
        )
        self._start_ai_task(runner, "剧情分析")

    def _generate_script(
        self, style_id: str, minutes: float, custom_name: str, custom_instruction: str
    ) -> None:
        if self._busy() or self.current_project is None:
            return
        if self.story_analysis is None:
            QMessageBox.warning(self, "缺少剧情分析", "请先运行“剧情分析”，再生成解说文案。")
            return
        if style_id == "custom" and not custom_instruction.strip():
            QMessageBox.warning(self, "缺少自定义要求", "自定义风格需要填写“自定义要求”。")
            return
        try:
            style = get_style(style_id)
        except KeyError as exc:
            QMessageBox.warning(self, "未知风格", str(exc))
            return
        analysis = self.story_analysis
        cost_service = CostService(self.config, self.db)
        estimate = cost_service.estimate_script_cost(analysis, minutes)
        proceed, allow_over = self._check_ai_preconditions(estimate, f"《{style.name}》文案生成")
        if not proceed:
            return
        project_id = self.current_project.id
        self.ai_kind = "script"
        self.ai_control = TaskControl()
        generator = ScriptGenerator(self.db, self.config)
        runner = lambda control, progress, log: generator.run(  # noqa: E731
            project_id,
            style_id,
            minutes=minutes,
            custom_name=custom_name,
            custom_instruction=custom_instruction,
            control=control,
            on_progress=progress,
            on_log=log,
            allow_over_budget=allow_over,
        )
        self._start_ai_task(runner, f"生成文案（{style.name}）")

    def _generate_voiceover(self, settings: dict[str, object]) -> None:
        if self._busy() or self.current_project is None or self.current_script is None:
            return
        config = self.config.model_copy(deep=True)
        try:
            for key, value in settings.items():
                setattr(config.tts, key, value)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "配音参数无效", str(exc))
            return
        self.config.tts = config.tts
        try:
            save_config(self.config)
        except OSError as exc:
            LOGGER.warning("保存配音设置失败：%s", exc)
        project_id = self.current_project.id
        script_id = self.current_script.id
        try:
            service = TTSService(self.db, config, tts_config=config.tts)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法启动配音", str(exc))
            return
        self.ai_kind = "tts"
        self.ai_control = TaskControl()
        runner = lambda control, progress, log: service.synthesize(  # noqa: E731
            project_id,
            script_id,
            control=control,
            on_progress=progress,
            on_log=log,
        )
        self._start_ai_task(runner, "生成配音")

    def _run_shot_matching(self) -> None:
        if self._busy() or self.current_project is None:
            return
        if self.current_script is None:
            QMessageBox.warning(self, "没有文案", "请先生成解说文案。")
            return
        if self.voiceover is None:
            QMessageBox.warning(self, "没有配音", "请先生成配音，再自动匹配镜头。")
            return
        if self.voiceover.script_id != self.current_script.id:
            QMessageBox.warning(
                self,
                "配音与文案不匹配",
                "当前配音基于旧文案版本。请先为当前文案重新生成配音，再做镜头匹配。",
            )
            return
        project_id = self.current_project.id
        self.ai_kind = "match"
        self.ai_control = TaskControl()
        matcher = ShotMatcher(self.db, self.config)
        runner = lambda control, progress, log: matcher.match(  # noqa: E731
            project_id,
            control=control,
            on_progress=progress,
            on_log=log,
        )
        self._start_ai_task(runner, "自动匹配镜头")

    def _edit_shot_selection(self, sentence_index: int) -> None:
        if self.current_project is None or self.match_plan is None:
            return
        selection = next(
            (item for item in self.match_plan.selections if item.sentence_index == sentence_index),
            None,
        )
        if selection is None:
            return
        dialog = ShotEditorDialog(
            self.shots,
            selection.shot_ids,
            target_duration_s=selection.target_duration_s,
            sentence_text=selection.text,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected_ids = dialog.selected_ids()
        if not selected_ids:
            QMessageBox.warning(self, "没有镜头", "至少选择一个镜头。")
            return
        try:
            updated = apply_manual_selection(
                self.match_plan, sentence_index, selected_ids, self.shots
            )
            saved = self.db.save_shot_match(updated)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.match_plan = saved
        self.match_panel.set_plan(saved, self.shots)
        self.match_panel.set_ready(
            self.current_script is not None,
            self.voiceover is not None
            and self.current_script is not None
            and saved.voiceover_id == self.voiceover.id,
        )
        self.stage_label.setText(
            f"第 {sentence_index} 句已改为人工选镜；重新匹配会覆盖自动结果。"
        )
        self.statusBar().showMessage("已保存人工镜头选择", 5000)

    def _start_export(self, settings: dict[str, object]) -> None:
        if self._busy() or self.current_project is None:
            return
        if self.match_plan is None or self.voiceover is None:
            QMessageBox.warning(self, "无法导出", "请先完成配音和镜头匹配。")
            return
        config = self.config.model_copy(deep=True)
        try:
            for key, value in settings.items():
                setattr(config.export, key, value)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "导出参数无效", str(exc))
            return
        self.config.export = config.export
        try:
            save_config(self.config)
        except OSError as exc:
            LOGGER.warning("保存导出设置失败：%s", exc)
        project_id = self.current_project.id
        try:
            exporter = VideoExporter(self.db, config)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "无法启动导出", str(exc))
            return
        self.ai_kind = "export"
        self.ai_control = TaskControl()
        runner = lambda control, progress, log: exporter.export(  # noqa: E731
            project_id,
            export_config=config.export,
            control=control,
            on_progress=progress,
            on_log=log,
        )
        self._start_ai_task(runner, "导出成片")

    def _open_export_output(self) -> None:
        if self.export_result is None:
            return
        try:
            open_with_default_app(self.export_result.output_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开成片", str(exc))

    def _open_export_dir(self) -> None:
        if self.export_result is not None:
            path = Path(self.export_result.output_path).parent
        elif self.current_project is not None:
            path = Path(self.current_project.project_dir) / "exports"
        else:
            return
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_in_explorer(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开导出目录", str(exc))

    def _open_export_subtitle(self) -> None:
        if self.export_result is None:
            return
        path = self.export_result.srt_path or self.export_result.ass_path
        if not path:
            return
        try:
            open_with_default_app(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开字幕", str(exc))

    def _open_voiceover_audio(self) -> None:
        if self.voiceover is None or not self.voiceover.audio_path:
            QMessageBox.warning(self, "没有配音", "请先生成配音。")
            return
        try:
            open_with_default_app(self.voiceover.audio_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开配音", str(exc))

    def _open_voiceover_dir(self) -> None:
        if self.current_project is None:
            return
        path = Path(self.current_project.project_dir) / "voiceover"
        try:
            path.mkdir(parents=True, exist_ok=True)
            open_in_explorer(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开配音目录", str(exc))

    def _start_ai_task(self, runner, title: str) -> None:
        self.ai_worker = AITaskWorker(runner, self.ai_control)
        self.ai_worker.progress.connect(self._on_ai_progress)
        self.ai_worker.logged.connect(self._on_ai_log)
        self.ai_worker.succeeded.connect(self._on_ai_succeeded)
        self.ai_worker.failed.connect(self._on_ai_failed)
        self.ai_worker.cancelled.connect(self._on_ai_cancelled)
        self.ai_worker.finished.connect(self._on_ai_thread_finished)
        self._cancelling = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.stage_label.setText(f"{title}：准备中…")
        self.statusBar().showMessage(f"{title}进行中…")
        self.story_panel.set_busy(True)
        self.script_panel.set_busy(True)
        self.tts_panel.set_busy(True)
        self.match_panel.set_busy(True)
        self.export_panel.set_busy(True)
        self.ai_worker.start()
        self._update_buttons()

    def _on_ai_progress(self, update: ProgressUpdate) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(update.percent)
        self.stage_label.setText(f"{update.stage} · {update.message}")
        if self.ai_kind == "export":
            self.export_panel.set_progress(update.percent, f"{update.stage} · {update.message}")

    def _on_ai_log(self, message: str) -> None:
        LOGGER.debug("AI 任务：%s", message)

    def _on_ai_succeeded(self, result: object) -> None:
        self._cancelling = False
        self.progress.setValue(100)
        cost_text = ""
        warnings: list[str] = []
        if isinstance(result, StoryRunResult):
            self.story_analysis = result.analysis
            self.story_panel.set_analysis(result.analysis)
            self.story_panel.set_stale(False)
            self.script_panel.set_analysis_available(True)
            self._reload_scripts()
            warnings = result.analysis.warnings
            cost_text = f"费用约 ¥{result.cost_cny:.4f}（调用 {result.api_calls} 次，缓存 {result.cached_calls} 次）"
            self.stage_label.setText(
                f"剧情分析完成：{result.analysis.segment_count} 段、"
                f"{len(result.analysis.characters)} 个人物，耗时 {result.elapsed_s:.1f} 秒，{cost_text}"
            )
            message = (
                f"剧情分析完成：{result.analysis.segment_count} 个片段、"
                f"{len(result.analysis.characters)} 个人物、"
                f"{len(result.analysis.turning_points)} 个转折。\n"
                f"耗时：{result.elapsed_s:.1f} 秒；{cost_text}\n\n"
                "所有 AI 结论均需人工核对；“剧情理解”页可查看证据与待核实项。"
            )
        elif isinstance(result, ScriptRunResult):
            script = result.script
            self.current_script = script
            self._reload_scripts(script.id)
            warnings = script.warnings
            cost_text = f"费用约 ¥{result.cost_cny:.4f}" + ("（缓存命中）" if result.cached else "")
            self.stage_label.setText(
                f"文案 v{script.version} 已生成：{script.character_count} 字，耗时 {result.elapsed_s:.1f} 秒，{cost_text}"
            )
            message = (
                f"文案 v{script.version} 已生成：{len(script.paragraphs)} 段、"
                f"{script.character_count} 字。\n"
                f"耗时：{result.elapsed_s:.1f} 秒；{cost_text}\n\n"
                "请人工核对事实、原创性与平台/版权合规；可在“解说文案”页编辑并保存为新版本。"
            )
        elif isinstance(result, Voiceover):
            self.voiceover = result
            self.tts_panel.set_voiceover(result)
            self.match_panel.set_ready(
                self.current_script is not None,
                self.current_script is not None and result.script_id == self.current_script.id,
            )
            warnings = result.warnings
            self.stage_label.setText(
                f"配音完成：{result.duration_label} · {result.unit_count} 句 · {result.engine}"
            )
            message = (
                f"配音完成：{result.unit_count} 句，总时长 {result.duration_label}。\n"
                f"引擎：{result.engine}；声音：{result.voice}；"
                f"耗时：{result.elapsed_s:.1f} 秒。\n"
                + ("全部命中本地缓存。" if result.cached else "已生成完整配音音轨。")
                + "\n\n请先试听/核对配音时间轴，再进行镜头匹配。"
            )
        elif isinstance(result, ShotMatchPlan):
            self.match_plan = result
            self.match_panel.set_plan(result, self.shots)
            self.match_panel.set_ready(
                self.current_script is not None,
                self.voiceover is not None
                and self.current_script is not None
                and result.voiceover_id == self.voiceover.id,
            )
            warnings = result.warnings
            self.stage_label.setText(
                f"镜头匹配完成：{result.selection_count} 句，"
                f"平均置信度 {result.average_confidence:.2f}，"
                f"{result.low_confidence_count} 句需审核"
            )
            message = (
                f"镜头匹配完成：{result.selection_count} 句，"
                f"{result.low_confidence_count} 句需要人工审核。\n"
                f"平均置信度：{result.average_confidence:.2f}；耗时 {result.elapsed_s:.1f} 秒。\n\n"
                "低置信度片段已用黄色标记；可逐句点击“编辑镜头”人工替换，"
                "确认后再到“剪辑合成”导出。"
            )
        elif isinstance(result, ExportResult):
            self.export_result = result
            self.export_panel.set_result(result)
            warnings = result.warnings
            self.stage_label.setText(
                f"成片已导出：{result.duration_label} · {result.size_label} · "
                f"{result.width}×{result.height}"
            )
            message = (
                f"成片导出完成：{result.duration_label}，{result.size_label}。\n"
                f"画面：{result.width}×{result.height} @ {result.fps}fps；"
                f"字幕：{'烧录' if result.subtitle_mode == 'burn' else '无'}；"
                f"BGM：{'有' if result.bgm_used else '无'}。\n\n"
                f"成片：{result.output_path}\n字幕：{result.srt_path}\n\n"
                "请人工完整观看成片，检查镜头顺序、字幕、音量和版权。"
            )
        else:
            self.stage_label.setText("AI 任务完成")
            message = "AI 任务完成。"
        if warnings:
            message += "\n\n注意事项：\n" + "\n".join(f"· {item}" for item in warnings[:8])
        self.statusBar().showMessage("AI 任务完成", 8000)
        if self.current_project is not None:
            self.refresh_projects(select_id=self.current_project.id)
        self._update_cost_tab()
        QMessageBox.information(self, "AI 任务完成", message)

    def _on_ai_failed(self, message: str) -> None:
        self._cancelling = False
        self.stage_label.setText("任务失败，请查看日志。")
        self.statusBar().showMessage("任务失败", 8000)
        if self.current_project is not None:
            self.refresh_projects(select_id=self.current_project.id)
        self._update_cost_tab()
        QMessageBox.critical(
            self,
            "任务失败",
            message + "\n\n如果是 DeepSeek 预算拦截，请提高项目预算或确认超支后重试；"
            "失败/取消前已完成的相同请求会命中本地缓存，重试不会重复计费。"
            "配音失败会保留已完成句的音频缓存，可直接重试。",
        )

    def _on_ai_cancelled(self) -> None:
        self._cancelling = False
        self.stage_label.setText("任务已取消；已完成的调用/逐句音频会保留，可稍后重试。")
        self.statusBar().showMessage("任务已取消", 8000)
        if self.current_project is not None:
            self.refresh_projects(select_id=self.current_project.id)
        self._update_cost_tab()

    def _on_ai_thread_finished(self) -> None:
        self.ai_worker = None
        self.ai_control = None
        self.ai_kind = ""
        self._cancelling = False
        self.story_panel.set_busy(False)
        self.script_panel.set_busy(False)
        self.tts_panel.set_busy(False)
        self.match_panel.set_busy(False)
        self.export_panel.set_busy(False)
        self._update_buttons()

    def _reload_scripts(self, current_id: str | None = None) -> None:
        if self.current_project is None:
            return
        try:
            summaries = self.db.list_scripts(self.current_project.id)
            current = self.db.get_current_script(self.current_project.id)
            if current_id:
                selected = self.db.get_script(current_id)
                if selected is not None:
                    current = selected
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("读取文案版本失败")
            QMessageBox.critical(self, "读取文案失败", str(exc))
            return
        self.current_script = current
        self.script_panel.set_scripts(
            summaries, current.id if current is not None else None
        )
        if current is not None:
            self.script_panel.load_script(current)

    def _preview_script_version(self, script_id: str) -> None:
        if self.current_project is None:
            return
        script = self.db.get_script(script_id)
        if script is None or script.project_id != self.current_project.id:
            QMessageBox.warning(self, "版本不存在", "找不到该文案版本。")
            return
        self.current_script = script
        self.script_panel.load_script(script)
        self.stage_label.setText(
            f"正在预览 v{script.version}；点击“恢复为当前版本”才会把它设为当前版本。"
        )

    def _restore_script_version(self, script_id: str) -> None:
        if self.current_project is None:
            return
        try:
            restored = ScriptGenerator(self.db, self.config).restore_version(
                self.current_project.id, script_id
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "恢复版本失败", str(exc))
            return
        self.current_script = restored
        self._reload_scripts(restored.id)
        self.tts_panel.set_stale(self.voiceover is not None)
        self.match_panel.set_stale(self.match_plan is not None)
        self.match_panel.set_ready(True, False)
        self._update_buttons()
        self.stage_label.setText(f"已恢复文案 v{restored.version} 为当前版本")
        self.statusBar().showMessage(f"已恢复 v{restored.version}", 5000)

    def _save_script_edit(self) -> None:
        if self.current_project is None or self.current_script is None:
            QMessageBox.warning(self, "没有文案", "请先生成文案，再编辑保存。")
            return
        title = self.script_panel.edited_title()
        text = self.script_panel.edited_text()
        if not text.strip():
            QMessageBox.warning(self, "内容为空", "文案内容不能为空。")
            return
        try:
            saved = ScriptGenerator(self.db, self.config).save_manual_edit(
                self.current_project.id, self.current_script.id, title, text
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.current_script = saved
        self._reload_scripts(saved.id)
        self.tts_panel.set_stale(self.voiceover is not None)
        self.match_panel.set_stale(self.match_plan is not None)
        self.match_panel.set_ready(True, False)
        self._update_buttons()
        self.stage_label.setText(f"人工编辑已保存为 v{saved.version}（旧版本保留）")
        self.statusBar().showMessage(f"已保存 v{saved.version}", 5000)
        QMessageBox.information(
            self,
            "已保存",
            f"编辑内容已保存为 v{saved.version}，原版本未被覆盖。\n"
            "人工编辑版本仍标记为需人工核对。",
        )

    def _busy(self) -> bool:
        analysis_running = self.worker is not None and self.worker.isRunning()
        ai_running = self.ai_worker is not None and self.ai_worker.isRunning()
        probe_running = self.probe_worker is not None and self.probe_worker.isRunning()
        return analysis_running or ai_running or probe_running

    def _active_control(self) -> TaskControl | None:
        if self.worker is not None and self.worker.isRunning():
            return self.control
        if self.ai_worker is not None and self.ai_worker.isRunning():
            return self.ai_control
        return None

    def _update_buttons(self) -> None:
        phase1_running = self.worker is not None and self.worker.isRunning()
        ai_running = self.ai_worker is not None and self.ai_worker.isRunning()
        running = phase1_running or ai_running
        probe_running = self.probe_worker is not None and self.probe_worker.isRunning()
        has_project = self.current_project is not None
        source_ok = has_project and Path(self.current_project.source_path).exists()  # type: ignore[union-attr]
        control = self._active_control()
        paused = bool(control and control.paused)
        cancelled = bool(control and control.cancelled)
        pause_allowed = phase1_running or (ai_running and self.ai_kind != "export")
        self.start_button.setEnabled(has_project and source_ok and not running and not probe_running)
        self.pause_button.setEnabled(pause_allowed and not paused and not cancelled)
        self.resume_button.setEnabled(pause_allowed and paused and not cancelled)
        self.cancel_button.setEnabled(running and not cancelled)
        self.open_video_button.setEnabled(source_ok)
        self.open_dir_button.setEnabled(has_project)
        self.export_button.setEnabled(has_project)
        self.new_button.setEnabled(not running and not probe_running)
        self.refresh_button.setEnabled(not running and not probe_running)
        self.project_list.setEnabled(not running and not probe_running)
        self.action_new.setEnabled(not running and not probe_running)
        self.action_export.setEnabled(has_project)
        self.action_settings.setEnabled(True)
        if has_project:
            self.story_panel.run_button.setEnabled(not running and not probe_running and bool(self.dialogues))
            self.script_panel.set_analysis_available(self.story_analysis is not None)
            self.script_panel.set_busy(running or probe_running)
            self.tts_panel.set_script_available(self.current_script is not None)
            self.tts_panel.set_busy(running or probe_running)
            voiceover_valid = bool(
                self.voiceover is not None
                and self.current_script is not None
                and self.voiceover.script_id == self.current_script.id
            )
            self.match_panel.set_ready(self.current_script is not None, voiceover_valid)
            self.match_panel.set_busy(running or probe_running)
            export_ready = bool(
                self.current_script is not None
                and voiceover_valid
                and self.match_plan is not None
                and self.voiceover is not None
                and self.match_plan.voiceover_id == self.voiceover.id
                and self.match_plan.script_id == self.current_script.id
            )
            self.export_panel.set_ready(export_ready)
            self.export_panel.set_busy(running or probe_running)
        else:
            self.story_panel.run_button.setEnabled(False)
            self.tts_panel.set_script_available(False)
            self.match_panel.set_ready(False, False)
            self.export_panel.set_ready(False)
            self.export_panel.set_busy(False)

    # ------------------------------------------------------------------ 其他操作
    def _open_source_video(self) -> None:
        if self.current_project is None:
            return
        try:
            open_with_default_app(self.current_project.source_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开影片", str(exc))

    def _open_project_dir(self) -> None:
        if self.current_project is None:
            return
        try:
            open_in_explorer(self.current_project.project_dir)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开项目目录", str(exc))

    def _open_data_dir(self) -> None:
        try:
            open_in_explorer(self.config.resolved_data_dir())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开数据目录", str(exc))

    def _export_timeline(self) -> None:
        if self.current_project is None:
            return
        project = self.current_project
        default_path = Path(project.project_dir) / "timeline.json"
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出时间轴 JSON",
            str(default_path),
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not target:
            return
        try:
            if default_path.exists():
                shutil.copyfile(default_path, target)
            else:
                self.db.export_timeline(project.id, target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage(f"时间轴已导出：{target}", 8000)
        QMessageBox.information(self, "导出完成", f"时间轴已保存到：\n{target}")

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_config is None:
            return
        self.config = dialog.result_config
        try:
            save_config(self.config)
        except OSError as exc:
            QMessageBox.critical(self, "保存配置失败", str(exc))
            return
        logging.getLogger().setLevel(self.config.log_level)
        self.tts_panel.set_config(self.config.tts)
        self._update_buttons()
        self._update_cost_tab()
        self.statusBar().showMessage("设置已保存，将在下一次任务中生效。", 8000)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {APP_NAME}",
            f"<h3>{APP_NAME} v{__version__}</h3>"
            "<p>第一阶段：素材导入、镜头切分、关键帧、本地中文对白识别与时间轴。</p>"
            "<p>第二阶段：DeepSeek 分段剧情分析、六种风格解说文案、编辑与版本管理、"
            "Token/费用统计与预算拦截。</p>"
            "<p>第三阶段：逐句中文配音、句子级时间轴、自动镜头匹配与人工审核标记。</p>"
            "<p>第四阶段：手动换镜、逐句自动剪辑、配音/BGM、中文字幕与横竖屏 MP4 导出。</p>"
            "<p>技术栈：PySide6 / FFmpeg / PySceneDetect / faster-whisper / "
            "DeepSeek API / SQLite / Pydantic。</p>"
            "<p style='color:#9aa0aa'>本软件不提供 DRM 破解、加密视频解密或版权检测规避功能；"
            "影片只在本机处理，不会自动上传。请仅导入具有合法使用依据的影片，"
            "AI 文案需人工核对原创性与平台规则后再发布。</p>",
        )

    def closeEvent(self, event: object) -> None:  # noqa: N802 - Qt 约定
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "退出确认",
                "分析任务仍在运行。退出将取消当前任务，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()  # type: ignore[attr-defined]
                return
            if self.control is not None:
                self.control.cancel()
            if not self.worker.wait(10000):
                QMessageBox.warning(self, "暂不能退出", "后台任务仍在结束后台进程，请稍后再试。")
                event.ignore()  # type: ignore[attr-defined]
                return
        if self.ai_worker is not None and self.ai_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "退出确认",
                "AI 任务仍在运行。退出将取消当前任务（已完成的调用仍会计费），是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()  # type: ignore[attr-defined]
                return
            if self.ai_control is not None:
                self.ai_control.cancel()
            if not self.ai_worker.wait(10000):
                QMessageBox.warning(self, "暂不能退出", "AI 请求仍在结束中，请稍后再试。")
                event.ignore()  # type: ignore[attr-defined]
                return
        if self.probe_worker is not None and self.probe_worker.isRunning():
            if not self.probe_worker.wait(5000):
                QMessageBox.warning(self, "暂不能退出", "正在读取影片信息，请稍后再试。")
                event.ignore()  # type: ignore[attr-defined]
                return
        try:
            logging.getLogger().removeHandler(self.log_handler)
        except Exception:  # noqa: BLE001
            pass
        event.accept()  # type: ignore[attr-defined]
