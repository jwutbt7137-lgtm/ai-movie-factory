"""成片导出页：分辨率、字幕、BGM 与进度。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import ExportConfig
from ..models import ExportResult


class ExportPanel(QWidget):
    export_requested = Signal(object)
    cancel_requested = Signal()
    open_output_requested = Signal()
    open_dir_requested = Signal()
    open_subtitle_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.export_result: ExportResult | None = None
        self._ready = False
        self._busy = False
        self._status_text = "尚无成片"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("剪辑合成")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        self.status_label = QLabel(self._status_text)
        self.status_label.setObjectName("HintLabel")
        header.addWidget(self.status_label, 1)
        self.export_button = QPushButton("开始导出")
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.clicked.connect(self._emit_export)
        header.addWidget(self.export_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.cancel_button.setEnabled(False)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)

        notice = QLabel(
            "根据当前配音时间轴和镜头方案裁切画面：逐句拼接、裁切/补帧到配音时长，"
            "混入完整配音，可选 BGM 和中文字幕（烧录或 MP4 软字幕）。不会上传影片。"
        )
        notice.setObjectName("HintLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("画面"))
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("横屏 1080p（1920×1080）", (1920, 1080))
        self.resolution_combo.addItem("横屏 720p（1280×720）", (1280, 720))
        self.resolution_combo.addItem("竖屏 1080p（1080×1920）", (1080, 1920))
        self.resolution_combo.addItem("方形 1080（1080×1080）", (1080, 1080))
        row1.addWidget(self.resolution_combo)
        row1.addWidget(QLabel("帧率"))
        self.fps_combo = QComboBox()
        for fps in (24, 25, 30, 60):
            self.fps_combo.addItem(f"{fps} fps", fps)
        row1.addWidget(self.fps_combo)
        row1.addWidget(QLabel("编码"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("高质量（CRF 18）", (18, "slow"))
        self.quality_combo.addItem("标准（CRF 20）", (20, "medium"))
        self.quality_combo.addItem("快速（CRF 23）", (23, "veryfast"))
        self.quality_combo.addItem("预览（CRF 28）", (28, "ultrafast"))
        row1.addWidget(self.quality_combo)
        row1.addWidget(QLabel("填充"))
        self.fit_combo = QComboBox()
        self.fit_combo.addItem("黑边（保留全画面）", "pad")
        self.fit_combo.addItem("裁切（铺满画面）", "crop")
        row1.addWidget(self.fit_combo)
        row1.addWidget(QLabel("字幕"))
        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItem("烧录中文字幕", "burn")
        self.subtitle_combo.addItem("MP4 软字幕", "soft")
        self.subtitle_combo.addItem("无字幕", "none")
        row1.addWidget(self.subtitle_combo)
        row1.addStretch(1)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("BGM"))
        self.bgm_edit = QLineEdit()
        self.bgm_edit.setPlaceholderText("可选：本地音频文件（循环并压低音量）")
        row2.addWidget(self.bgm_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_bgm)
        row2.addWidget(browse)
        clear = QPushButton("清除")
        clear.clicked.connect(self.bgm_edit.clear)
        row2.addWidget(clear)
        row2.addWidget(QLabel("音量"))
        self.bgm_volume = QDoubleSpinBox()
        self.bgm_volume.setRange(-60.0, 0.0)
        self.bgm_volume.setSingleStep(1.0)
        self.bgm_volume.setDecimals(1)
        self.bgm_volume.setSuffix(" dB")
        row2.addWidget(self.bgm_volume)
        self.ducking_checkbox = QCheckBox("人声时自动压低 BGM")
        self.ducking_checkbox.setChecked(True)
        row2.addWidget(self.ducking_checkbox)
        layout.addLayout(row2)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        row3 = QHBoxLayout()
        self.output_label = QLabel("成片路径：—")
        self.output_label.setObjectName("HintLabel")
        self.output_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row3.addWidget(self.output_label, 1)
        self.open_output_button = QPushButton("打开成片")
        self.open_output_button.clicked.connect(self.open_output_requested.emit)
        row3.addWidget(self.open_output_button)
        self.open_subtitle_button = QPushButton("打开字幕")
        self.open_subtitle_button.clicked.connect(self.open_subtitle_requested.emit)
        row3.addWidget(self.open_subtitle_button)
        self.open_dir_button = QPushButton("导出目录")
        self.open_dir_button.clicked.connect(self.open_dir_requested.emit)
        row3.addWidget(self.open_dir_button)
        layout.addLayout(row3)
        layout.addStretch(1)

        self._load_config(ExportConfig())
        self._update_controls()

    def _load_config(self, config: ExportConfig) -> None:
        target = (config.width, config.height)
        index = self.resolution_combo.findData(target)
        if index >= 0:
            self.resolution_combo.setCurrentIndex(index)
        fps_index = self.fps_combo.findData(config.fps)
        if fps_index >= 0:
            self.fps_combo.setCurrentIndex(fps_index)
        quality_index = self.quality_combo.findData((config.crf, config.preset))
        if quality_index >= 0:
            self.quality_combo.setCurrentIndex(quality_index)
        fit_index = self.fit_combo.findData(config.fit_mode)
        if fit_index >= 0:
            self.fit_combo.setCurrentIndex(fit_index)
        subtitle_index = self.subtitle_combo.findData(config.subtitle_mode)
        if subtitle_index >= 0:
            self.subtitle_combo.setCurrentIndex(subtitle_index)
        self.ducking_checkbox.setChecked(config.bgm_ducking)
        self.bgm_edit.setText(config.bgm_path)
        self.bgm_volume.setValue(config.bgm_volume_db)

    def set_config(self, config: ExportConfig) -> None:
        self._load_config(config)

    def export_settings(self) -> dict[str, object]:
        width, height = self.resolution_combo.currentData()
        crf, preset = self.quality_combo.currentData()
        return {
            "width": int(width),
            "height": int(height),
            "fps": int(self.fps_combo.currentData() or 30),
            "crf": int(crf),
            "preset": str(preset),
            "subtitle_mode": str(self.subtitle_combo.currentData() or "burn"),
            "fit_mode": str(self.fit_combo.currentData() or "pad"),
            "bgm_path": self.bgm_edit.text().strip(),
            "bgm_volume_db": float(self.bgm_volume.value()),
            "bgm_ducking": self.ducking_checkbox.isChecked(),
        }

    def set_ready(self, ready: bool, summary: str = "") -> None:
        self._ready = ready
        if summary:
            self._status_text = summary
            self.status_label.setText(summary)
        elif not ready:
            self._status_text = "请先完成配音和镜头匹配"
            self.status_label.setText(self._status_text)
        self._update_controls()

    def set_result(self, result: ExportResult | None) -> None:
        self.export_result = result
        if result is None:
            self.output_label.setText("成片路径：—")
            self.progress.setValue(0)
            self._update_controls()
            return
        self.progress.setValue(100)
        self.output_label.setText(f"成片路径：{result.output_path}")
        self.output_label.setToolTip(result.output_path)
        self._status_text = (
            f"成片：{result.duration_label} · {result.size_label} · "
            f"{result.width}×{result.height} · {result.subtitle_mode}"
        )
        self.status_label.setText(self._status_text)
        self._update_controls()

    def set_busy(self, busy: bool, message: str = "") -> None:
        was_busy = self._busy
        self._busy = busy
        if busy:
            if not was_busy:
                self.progress.setValue(0)
            self.status_label.setText(message or "导出中…")
        else:
            self.status_label.setText(self._status_text)
        self._update_controls()

    def set_progress(self, percent: int, message: str = "") -> None:
        self.progress.setValue(max(0, min(100, percent)))
        if message:
            self.status_label.setText(message)

    def _update_controls(self) -> None:
        self.export_button.setEnabled(self._ready and not self._busy)
        self.cancel_button.setEnabled(self._busy)
        has_result = self.export_result is not None
        self.open_output_button.setEnabled(has_result)
        self.open_subtitle_button.setEnabled(has_result)
        self.open_dir_button.setEnabled(has_result)
        for widget in (
            self.resolution_combo,
            self.fps_combo,
            self.quality_combo,
            self.fit_combo,
            self.subtitle_combo,
            self.bgm_edit,
            self.bgm_volume,
            self.ducking_checkbox,
        ):
            widget.setEnabled(not self._busy)

    def _browse_bgm(self) -> None:
        start = str(Path(self.bgm_edit.text()).parent) if self.bgm_edit.text().strip() else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择 BGM",
            start,
            "音频文件 (*.mp3 *.wav *.m4a *.aac *.flac *.ogg);;所有文件 (*.*)",
        )
        if filename:
            self.bgm_edit.setText(filename)

    def _emit_export(self) -> None:
        if self._ready and not self._busy:
            self.export_requested.emit(self.export_settings())
