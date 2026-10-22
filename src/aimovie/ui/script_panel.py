"""解说文案标签页：生成、编辑、版本恢复与镜头关联展示。"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..models import Script, ScriptSummary
from ..copywriting.styles import STYLES, get_style


class ScriptPanel(QWidget):
    """文案生成与编辑面板。"""

    generate_requested = Signal(str, float, str, str)
    version_selected = Signal(str)
    restore_requested = Signal(str)
    save_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.current_script: Script | None = None
        self._scripts: list[ScriptSummary] = []
        self._analysis_available = False
        self._busy = False
        self._loading_versions = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("风格"))
        self.style_combo = QComboBox()
        for style in STYLES:
            self.style_combo.addItem(style.name, style.id)
            self.style_combo.setItemData(
                self.style_combo.count() - 1, style.description, Qt.ItemDataRole.ToolTipRole
            )
        controls.addWidget(self.style_combo, 2)
        controls.addWidget(QLabel("目标时长"))
        self.minutes_spin = QDoubleSpinBox()
        self.minutes_spin.setRange(1.0, 30.0)
        self.minutes_spin.setDecimals(1)
        self.minutes_spin.setSingleStep(1.0)
        self.minutes_spin.setSuffix(" 分钟")
        controls.addWidget(self.minutes_spin)
        self.generate_button = QPushButton("生成文案")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self._emit_generate)
        controls.addWidget(self.generate_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.custom_row = QWidget()
        custom_layout = QHBoxLayout(self.custom_row)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.addWidget(QLabel("风格名称"))
        self.custom_name_edit = QLineEdit()
        self.custom_name_edit.setPlaceholderText("可选，例如：冷峻旁白")
        custom_layout.addWidget(self.custom_name_edit, 1)
        custom_layout.addWidget(QLabel("自定义要求"))
        self.custom_instruction_edit = QLineEdit()
        self.custom_instruction_edit.setPlaceholderText("例如：用克制的语气，重点分析时代背景，不要剧透结局")
        custom_layout.addWidget(self.custom_instruction_edit, 3)
        layout.addWidget(self.custom_row)

        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("版本"))
        self.version_combo = QComboBox()
        self.version_combo.setMinimumWidth(320)
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)
        version_row.addWidget(self.version_combo, 1)
        self.restore_button = QPushButton("恢复为当前版本")
        self.restore_button.clicked.connect(self._emit_restore)
        version_row.addWidget(self.restore_button)
        self.status_label = QLabel("尚无文案")
        self.status_label.setObjectName("HintLabel")
        version_row.addWidget(self.status_label, 1)
        layout.addLayout(version_row)

        notice = QLabel(
            "AI 文案必须人工核对事实、原创性与平台合规；段落后的剧情片段/镜头编号来自本地证据校验。"
        )
        notice.setObjectName("HintLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.paragraph_list = QListWidget()
        self.paragraph_list.setMinimumWidth(260)
        self.paragraph_list.currentItemChanged.connect(self._on_paragraph_changed)
        splitter.addWidget(self.paragraph_list)

        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("文案标题")
        editor_layout.addWidget(self.title_edit)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("暂无文案。先运行剧情分析，再生成解说文案。")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor_layout.addWidget(self.editor, 1)
        bottom = QHBoxLayout()
        self.meta_label = QLabel("—")
        self.meta_label.setObjectName("HintLabel")
        self.meta_label.setWordWrap(True)
        bottom.addWidget(self.meta_label, 1)
        self.save_button = QPushButton("保存编辑为新版本")
        self.save_button.clicked.connect(self.save_requested.emit)
        bottom.addWidget(self.save_button)
        editor_layout.addLayout(bottom)
        splitter.addWidget(editor_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter, 1)

        self.style_combo.currentIndexChanged.connect(self._on_style_changed)
        self._on_style_changed()
        self._update_controls()

    # ------------------------------------------------------------------ 数据
    def clear(self) -> None:
        """切换项目时清空版本与编辑器。"""
        self._scripts = []
        self._loading_versions = True
        self.version_combo.clear()
        self._loading_versions = False
        self._clear_editor()
        self.status_label.setText("尚无文案")
        self._update_controls()

    def set_analysis_available(self, available: bool) -> None:
        self._analysis_available = available
        self._update_controls()
        if not available:
            self.status_label.setText("需先完成剧情分析")

    def set_scripts(self, summaries: list[ScriptSummary], current_id: str | None = None) -> None:
        self._scripts = list(summaries)
        self._loading_versions = True
        self.version_combo.clear()
        for summary in summaries:
            self.version_combo.addItem(summary.label, summary.id)
        target_id = current_id
        if target_id is None and summaries:
            target_id = summaries[0].id
        if target_id:
            index = self.version_combo.findData(target_id)
            if index >= 0:
                self.version_combo.setCurrentIndex(index)
        self._loading_versions = False
        if not summaries:
            self.status_label.setText("尚无文案")
            self._clear_editor()
        elif current_id:
            self._select_version_combo(current_id)

    def load_script(self, script: Script) -> None:
        self.current_script = script
        index = self.style_combo.findData(script.style_id)
        if index >= 0:
            self.style_combo.blockSignals(True)
            self.style_combo.setCurrentIndex(index)
            self.style_combo.blockSignals(False)
            self.minutes_spin.setValue(script.target_minutes)
            self.custom_row.setVisible(script.style_id == "custom")
        self.title_edit.setText(script.title)
        self.editor.setPlainText(script.full_text)
        self.paragraph_list.clear()
        for paragraph in script.paragraphs:
            segments = ",".join(str(index) for index in paragraph.segment_ids) or "—"
            shots = ",".join(str(index) for index in paragraph.shot_ids) or "—"
            review = " · 待核实" if paragraph.needs_review else ""
            item = QListWidgetItem(
                f"第 {paragraph.index} 段 · 剧情 {segments} · 镜头 {shots}{review}\n"
                f"{paragraph.text[:44]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, paragraph.index)
            events = "；".join(paragraph.event_descriptions[:3]) or "—"
            item.setToolTip(
                f"剧情片段：{segments}\n候选镜头：{shots}\n关联事件：{events}\n\n{paragraph.text}"
            )
            self.paragraph_list.addItem(item)
        self._select_version_combo(script.id)
        self.status_label.setText(
            f"当前 v{script.version} · {script.character_count} 字 / 目标 {script.target_chars}"
        )
        self._update_meta()
        self._update_controls()

    def _clear_editor(self) -> None:
        self.current_script = None
        self.title_edit.clear()
        self.editor.clear()
        self.paragraph_list.clear()
        self.meta_label.setText("—")

    def _select_version_combo(self, script_id: str) -> None:
        self._loading_versions = True
        index = self.version_combo.findData(script_id)
        if index >= 0:
            self.version_combo.setCurrentIndex(index)
        self._loading_versions = False

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_controls()

    def set_status(self, message: str) -> None:
        self.status_label.setText(message)

    # ------------------------------------------------------------------ 交互
    def generation_request(self) -> tuple[str, float, str, str]:
        style_id = str(self.style_combo.currentData() or "quick3")
        return (
            style_id,
            float(self.minutes_spin.value()),
            self.custom_name_edit.text().strip(),
            self.custom_instruction_edit.text().strip(),
        )

    def edited_title(self) -> str:
        return self.title_edit.text().strip()

    def edited_text(self) -> str:
        return self.editor.toPlainText()

    def selected_version_id(self) -> str | None:
        data = self.version_combo.currentData()
        return str(data) if data else None

    def _emit_generate(self) -> None:
        style_id, minutes, custom_name, instruction = self.generation_request()
        if style_id == "custom" and not instruction:
            self.status_label.setText("自定义风格需要填写“自定义要求”")
            return
        self.generate_requested.emit(style_id, minutes, custom_name, instruction)

    def _emit_restore(self) -> None:
        script_id = self.selected_version_id()
        if script_id:
            self.restore_requested.emit(script_id)

    def _on_style_changed(self) -> None:
        style_id = str(self.style_combo.currentData() or "quick3")
        try:
            style = get_style(style_id)
        except KeyError:
            return
        self.minutes_spin.setValue(style.default_minutes)
        self.custom_row.setVisible(style_id == "custom")
        self._update_controls()

    def _on_version_changed(self) -> None:
        if self._loading_versions:
            return
        script_id = self.selected_version_id()
        if script_id and (self.current_script is None or self.current_script.id != script_id):
            self.version_selected.emit(script_id)

    def _on_paragraph_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None or self.current_script is None:
            return
        try:
            index = int(current.data(Qt.ItemDataRole.UserRole))
        except (TypeError, ValueError):
            return
        paragraph = next(
            (item for item in self.current_script.paragraphs if item.index == index), None
        )
        if paragraph is None:
            return
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        found = self.editor.document().find(paragraph.text[:40], cursor)
        if not found.isNull():
            self.editor.setTextCursor(found)
            self.editor.ensureCursorVisible()

    def _update_meta(self) -> None:
        script = self.current_script
        if script is None:
            self.meta_label.setText("—")
            return
        source = "AI 生成" if script.source == "ai" else "人工编辑"
        cached = " · 缓存命中" if script.cached else ""
        model = html.escape(script.model or "—")
        self.meta_label.setText(
            f"{source} · 模型 {model} · 费用约 ¥{script.cost_cny:.4f}{cached} · "
            f"{len(script.paragraphs)} 段 · 需人工审核"
        )

    def _update_controls(self) -> None:
        can_generate = self._analysis_available and not self._busy
        self.generate_button.setEnabled(can_generate)
        self.style_combo.setEnabled(not self._busy)
        self.minutes_spin.setEnabled(not self._busy)
        self.custom_name_edit.setEnabled(not self._busy)
        self.custom_instruction_edit.setEnabled(not self._busy)
        self.version_combo.setEnabled(not self._busy and bool(self._scripts))
        self.restore_button.setEnabled(not self._busy and bool(self._scripts))
        self.save_button.setEnabled(not self._busy and self.current_script is not None)
        self.editor.setReadOnly(self._busy)
        self.title_edit.setReadOnly(self._busy)
        if self._analysis_available:
            self.generate_button.setToolTip("调用 DeepSeek 生成本地项目文案，会产生费用")
        else:
            self.generate_button.setToolTip("请先运行剧情分析")
