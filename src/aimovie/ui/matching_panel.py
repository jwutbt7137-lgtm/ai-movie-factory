"""镜头匹配标签页：逐句候选镜头、置信度与人工审核标记。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..models import Shot, ShotMatchPlan, format_clock


class MatchingPanel(QWidget):
    """展示自动匹配方案，低置信度片段用颜色标记。"""

    match_requested = Signal()
    edit_requested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.plan: ShotMatchPlan | None = None
        self._shots: dict[int, Shot] = {}
        self._has_script = False
        self._has_voiceover = False
        self._busy = False
        self._status_text = "尚无匹配方案"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("镜头匹配")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        self.status_label = QLabel("尚无匹配方案")
        self.status_label.setObjectName("HintLabel")
        header.addWidget(self.status_label, 1)
        self.edit_button = QPushButton("编辑镜头")
        self.edit_button.clicked.connect(self._emit_edit)
        header.addWidget(self.edit_button)
        self.match_button = QPushButton("自动匹配镜头")
        self.match_button.setObjectName("PrimaryButton")
        self.match_button.clicked.connect(self.match_requested.emit)
        header.addWidget(self.match_button)
        layout.addLayout(header)

        notice = QLabel(
            "当前匹配依据：文案段落关联的剧情片段、镜头时长与连续性。未做画面语义识别；"
            "低置信度片段必须人工审核，手动替换镜头将在第四阶段提供。"
        )
        notice.setObjectName("HintLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["句", "配音时间", "目标时长", "候选镜头", "置信度", "审核"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header_view = self.table.horizontalHeader()
        for column in (0, 1, 2):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.currentCellChanged.connect(self._on_row_changed)
        splitter.addWidget(self.table)

        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview_label = QLabel("选择句子查看候选镜头")
        self.preview_label.setObjectName("PreviewLabel")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFixedSize(360, 203)
        preview_layout.addWidget(self.preview_label)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setHtml("<p style='color:#9aa0aa'>选择一个句子查看镜头详情。</p>")
        preview_layout.addWidget(self.detail, 1)
        splitter.addWidget(preview)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([700, 380])
        layout.addWidget(splitter, 1)

        self._update_controls()

    def set_ready(self, has_script: bool, has_voiceover: bool) -> None:
        self._has_script = has_script
        self._has_voiceover = has_voiceover
        self._update_controls()

    def set_plan(self, plan: ShotMatchPlan | None, shots: list[Shot]) -> None:
        self.plan = plan
        self._shots = {shot.index: shot for shot in shots}
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        if plan is None:
            self.table.setRowCount(0)
            self._status_text = "尚无匹配方案"
            self.status_label.setText(self._status_text)
            self.detail.setHtml("<p style='color:#9aa0aa'>选择一个句子查看镜头详情。</p>")
            self.preview_label.clear()
            self.preview_label.setText("选择句子查看候选镜头")
            self.table.setUpdatesEnabled(True)
            self._update_controls()
            return
        self.table.setRowCount(len(plan.selections))
        for row, selection in enumerate(plan.selections):
            values = [
                str(selection.sentence_index),
                selection.time_label,
                f"{selection.target_duration_s:.2f}s",
                ",".join(str(index) for index in selection.shot_ids) or "—",
                f"{selection.confidence:.2f}",
                "需审核" if selection.needs_review else "通过",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 1, 2, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if selection.needs_review:
                    item.setForeground(QColor("#e0b25c"))
                self.table.setItem(row, column, item)
        self.table.setUpdatesEnabled(True)
        self._status_text = (
            f"{plan.selection_count} 句 · 平均置信度 {plan.average_confidence:.2f} · "
            f"{plan.low_confidence_count} 句需审核"
        )
        self.status_label.setText(self._status_text)
        self._update_controls()
        if plan.selections:
            self.table.setCurrentCell(0, 0)

    def set_stale(self, stale: bool) -> None:
        if stale and self.plan is not None:
            self._status_text = "当前匹配基于旧文案或旧配音，建议重新自动匹配。"
            self.status_label.setText(self._status_text)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message:
            self._status_text = message
            self.status_label.setText(message)
        elif busy:
            self.status_label.setText("镜头匹配中…")
        else:
            self.status_label.setText(self._status_text)
        self._update_controls()

    def _update_controls(self) -> None:
        self.match_button.setEnabled(
            self._has_script and self._has_voiceover and not self._busy
        )
        self.edit_button.setEnabled(
            self.plan is not None
            and self.table.currentRow() >= 0
            and not self._busy
        )
        if not self._has_script:
            self.match_button.setToolTip("请先生成解说文案")
        elif not self._has_voiceover:
            self.match_button.setToolTip("请先生成配音")
        else:
            self.match_button.setToolTip("根据剧情片段、镜头时长与连续性自动匹配")

    def _on_row_changed(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        if self.plan is None or row < 0 or row >= len(self.plan.selections):
            return
        selection = self.plan.selections[row]
        self._update_controls()
        self.detail.setHtml(self._html(selection))
        self.preview_label.clear()
        self.preview_label.setText("没有预览图")
        for shot_id in selection.shot_ids:
            shot = self._shots.get(shot_id)
            if shot and shot.keyframe_path and Path(shot.keyframe_path).exists():
                pixmap = QPixmap(shot.keyframe_path)
                if not pixmap.isNull():
                    self.preview_label.setPixmap(
                        pixmap.scaled(
                            self.preview_label.size(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                    break

    def _emit_edit(self) -> None:
        if self.plan is None:
            return
        row = self.table.currentRow()
        if 0 <= row < len(self.plan.selections):
            self.edit_requested.emit(self.plan.selections[row].sentence_index)

    @staticmethod
    def _html(selection) -> str:
        from html import escape

        shots = escape(", ".join(str(index) for index in selection.shot_ids) or "—")
        reason = escape(selection.reason or "—")
        text = escape(selection.text or "—")
        flag = "需要人工审核" if selection.needs_review else "证据与时长检查通过（仍需整体复核）"
        color = "#e0b25c" if selection.needs_review else "#6fcf97"
        return (
            f"<h3>第 {selection.sentence_index} 句</h3>"
            f"<p style='color:{color}'><b>{flag}</b></p>"
            f"<p>配音时间：{format_clock(selection.target_start_s)} → "
            f"{format_clock(selection.target_end_s)}（{selection.target_duration_s:.2f} 秒）</p>"
            f"<p>候选镜头：{shots}<br>覆盖时长：{selection.coverage_s:.2f} 秒<br>"
            f"置信度：{selection.confidence:.2f}</p>"
            f"<p>原因：{reason}</p>"
            f"<p style='color:#9aa0aa'>{text}</p>"
        )
