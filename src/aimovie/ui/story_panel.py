"""剧情理解标签页：展示分段剧情、人物、转折与待核实项。"""

from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..models import StoryAnalysis, StorySegment


class StoryPanel(QWidget):
    """剧情分析展示面板。"""

    run_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.analysis: StoryAnalysis | None = None
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("剧情理解")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        self.status_label = QLabel("尚未分析")
        self.status_label.setObjectName("HintLabel")
        header.addWidget(self.status_label, 1)
        self.run_button = QPushButton("运行剧情分析")
        self.run_button.setObjectName("PrimaryButton")
        self.run_button.clicked.connect(self.run_requested.emit)
        header.addWidget(self.run_button)
        layout.addLayout(header)

        notice = QLabel(
            "分析仅使用语音识别对白，未接入视觉模型；所有人物、事件和转折均需人工核对。"
        )
        notice.setObjectName("HintLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        self.overall_view = QTextBrowser()
        self.overall_view.setOpenExternalLinks(False)
        self.overall_view.setMaximumHeight(150)
        self.overall_view.setHtml(
            "<p style='color:#9aa0aa'>尚无剧情分析结果。点击“运行剧情分析”开始（需要配置 DeepSeek API Key 和预算）。</p>"
        )
        layout.addWidget(self.overall_view)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.segment_list = QListWidget()
        self.segment_list.setMinimumWidth(250)
        self.segment_list.currentItemChanged.connect(self._on_segment_changed)
        splitter.addWidget(self.segment_list)
        self.detail_view = QTextBrowser()
        self.detail_view.setOpenExternalLinks(False)
        self.detail_view.setHtml("<p style='color:#9aa0aa'>选择一个剧情片段查看详情。</p>")
        splitter.addWidget(self.detail_view)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter, 1)

    def set_analysis(self, analysis: StoryAnalysis | None) -> None:
        self.analysis = analysis
        self.segment_list.blockSignals(True)
        self.segment_list.clear()
        self.segment_list.blockSignals(False)
        if analysis is None:
            self.status_label.setText("尚未分析")
            self.run_button.setText("运行剧情分析")
            self.overall_view.setHtml(
                "<p style='color:#9aa0aa'>尚无剧情分析结果。点击“运行剧情分析”开始（需要配置 DeepSeek API Key 和预算）。</p>"
            )
            self.detail_view.setHtml("<p style='color:#9aa0aa'>选择一个剧情片段查看详情。</p>")
            return
        review_count = sum(1 for segment in analysis.segments if segment.needs_review)
        self.status_label.setText(
            f"{analysis.segment_count} 段 · {len(analysis.characters)} 人物 · "
            f"{len(analysis.turning_points)} 转折 · {review_count} 段待核实 · AI 结论需人工审核"
        )
        self.run_button.setText("重新分析剧情")
        self.overall_view.setHtml(self._overall_html(analysis))
        for segment in analysis.segments:
            item = QListWidgetItem(
                f"第 {segment.index} 段 · {segment.time_label}\n"
                f"{segment.summary[:46] or '（无摘要）'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, segment.index)
            item.setSizeHint(QSize(0, 54))
            if segment.needs_review:
                item.setForeground(QColor("#e0b25c"))
            item.setToolTip(segment.summary)
            self.segment_list.addItem(item)
        if analysis.segments:
            self.segment_list.setCurrentRow(0)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.run_button.setEnabled(not busy)
        if message:
            self.status_label.setText(message)
        elif busy:
            self.status_label.setText("AI 分析中…")

    def set_stale(self, stale: bool) -> None:
        """时间轴变化后提示旧分析可能已过期。"""
        if stale and self.analysis is not None:
            self.status_label.setText(self.status_label.text() + " · 基于旧时间轴，建议重新分析")

    def _overall_html(self, analysis: StoryAnalysis) -> str:
        themes = "、".join(html.escape(theme) for theme in analysis.themes) or "—"
        warnings_html = ""
        if analysis.warnings:
            items = "".join(f"<li>{html.escape(warning)}</li>" for warning in analysis.warnings[:8])
            warnings_html = (
                "<p style='color:#e0b25c'><b>警告：</b></p><ul>" + items + "</ul>"
            )
        notes_html = ""
        if analysis.review_notes:
            items = "".join(f"<li>{html.escape(note)}</li>" for note in analysis.review_notes[:8])
            notes_html = (
                "<p style='color:#9aa0aa'><b>人工核实：</b></p><ul>" + items + "</ul>"
            )
        title = html.escape(analysis.title or analysis.project_name)
        logline = html.escape(analysis.logline or "—")
        summary = html.escape(analysis.overall_summary or "—")
        conflict = html.escape(analysis.main_conflict or "—")
        model = html.escape(analysis.model or "—")
        return (
            f"<h2>{title}</h2>"
            f"<p><b>一句话梗概：</b>{logline}</p>"
            f"<p><b>整体剧情：</b>{summary}</p>"
            f"<p><b>主要冲突：</b>{conflict} &nbsp; <b>主题：</b>{themes}</p>"
            f"<p><b>模型：</b>{model} &nbsp; <b>费用：</b>约 ¥{analysis.estimated_cost_cny:.4f}"
            f" &nbsp; <b>调用：</b>{analysis.api_calls} 次（缓存 {analysis.cached_calls}）</p>"
            + warnings_html
            + notes_html
        )

    def _on_segment_changed(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None or self.analysis is None:
            return
        index = current.data(Qt.ItemDataRole.UserRole)
        try:
            index = int(index)
        except (TypeError, ValueError):
            return
        segment = self.analysis.segment_by_index(index)
        if segment is not None:
            self.detail_view.setHtml(self._segment_html(segment))

    @staticmethod
    def _claim_list(items: list) -> str:
        parts: list[str] = []
        for item in items:
            flag = (
                " <span style='color:#e0b25c'>[待核实]</span>" if item.needs_review else ""
            )
            evidence = html.escape(str(item.evidence_dialogue_ids))
            parts.append(
                f"<li>{html.escape(item.description)} "
                f"<span style='color:#7fb3ff'>[对白证据 {evidence}]</span>{flag}</li>"
            )
        return "".join(parts) or "<li>—</li>"

    @staticmethod
    def _segment_html(segment: StorySegment) -> str:
        characters = "".join(
            f"<li>{html.escape(character.name)}：{html.escape(character.description or '—')} "
            f"<span style='color:#7fb3ff'>[对白 {html.escape(str(character.evidence_dialogue_ids))}]</span></li>"
            for character in segment.characters
        ) or "<li>—</li>"
        uncertainties = (
            "".join(f"<li>{html.escape(item)}</li>" for item in segment.uncertainties) or "<li>—</li>"
        )
        confidence = "—" if segment.confidence is None else f"{segment.confidence:.2f}"
        review_text = "需要人工核实" if segment.needs_review else "证据引用检查通过（结论仍需整体复核）"
        review_color = "#e0b25c" if segment.needs_review else "#6fcf97"
        events = StoryPanel._claim_list(segment.events)
        conflicts = StoryPanel._claim_list(segment.conflicts)
        turning_points = StoryPanel._claim_list(segment.turning_points)
        return (
            f"<h2>第 {segment.index} 段 · {segment.time_label}</h2>"
            f"<p style='color:{review_color}'>{review_text}</p>"
            f"<p><b>摘要：</b>{html.escape(segment.summary or '—')}</p>"
            f"<p><b>镜头：</b>{html.escape(str(segment.shot_ids))} &nbsp; "
            f"<b>对白：</b>{html.escape(str(segment.dialogue_ids))} &nbsp; "
            f"<b>置信度：</b>{confidence}</p>"
            f"<h3>事件</h3><ul>{events}</ul>"
            f"<h3>冲突</h3><ul>{conflicts}</ul>"
            f"<h3>转折</h3><ul>{turning_points}</ul>"
            f"<h3>人物</h3><ul>{characters}</ul>"
            f"<h3>待核实</h3><ul>{uncertainties}</ul>"
        )
