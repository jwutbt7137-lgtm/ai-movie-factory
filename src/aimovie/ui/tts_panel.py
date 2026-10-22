"""配音标签页：语音设置、逐句时间轴与完整配音文件。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import TTSConfig
from ..models import SpeechUnit, Voiceover, format_clock

EDGE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声 · 温柔自然"),
    ("zh-CN-YunxiNeural", "云希 · 男声 · 年轻"),
    ("zh-CN-YunjianNeural", "云健 · 男声 · 有力"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声 · 活泼"),
    ("zh-CN-YunyangNeural", "云扬 · 男声 · 播音"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北 · 女声 · 东北"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮 · 女声 · 陕西"),
    ("zh-HK-HiuMaanNeural", "曉曼 · 粤语女声"),
    ("zh-TW-HsiaoChenNeural", "曉臻 · 台湾女声"),
]


class TTSPanel(QWidget):
    """配音生成与句子时间轴展示。"""

    generate_requested = Signal(object)
    open_audio_requested = Signal()
    open_dir_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.voiceover: Voiceover | None = None
        self._script_available = False
        self._busy = False
        self._status_text = "尚无配音"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("中文配音")
        title.setObjectName("PanelTitle")
        header.addWidget(title)
        self.status_label = QLabel("尚无配音")
        self.status_label.setObjectName("HintLabel")
        header.addWidget(self.status_label, 1)
        self.generate_button = QPushButton("生成配音")
        self.generate_button.setObjectName("PrimaryButton")
        self.generate_button.clicked.connect(self._emit_generate)
        header.addWidget(self.generate_button)
        layout.addLayout(header)

        notice = QLabel(
            "Edge TTS 免费但在线；Piper 完全本地，需要在设置中提供中文 ONNX 模型。"
            "配音仅发送解说文案文本，不发送影片文件。"
        )
        notice.setObjectName("HintLabel")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("引擎"))
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("自动（优先本地 Piper）", "auto")
        self.engine_combo.addItem("Edge 在线语音", "edge")
        self.engine_combo.addItem("Piper 本地语音", "piper")
        controls.addWidget(self.engine_combo)
        controls.addWidget(QLabel("声音"))
        self.voice_combo = QComboBox()
        for voice_id, label in EDGE_VOICES:
            self.voice_combo.addItem(label, voice_id)
        self.voice_combo.setMinimumWidth(250)
        controls.addWidget(self.voice_combo, 2)
        controls.addWidget(QLabel("语速"))
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(-50, 100)
        self.rate_spin.setSuffix("%")
        controls.addWidget(self.rate_spin)
        controls.addWidget(QLabel("音量"))
        self.volume_spin = QSpinBox()
        self.volume_spin.setRange(-50, 50)
        self.volume_spin.setSuffix("%")
        controls.addWidget(self.volume_spin)
        controls.addWidget(QLabel("音高"))
        self.pitch_spin = QSpinBox()
        self.pitch_spin.setRange(-50, 50)
        self.pitch_spin.setSuffix("Hz")
        controls.addWidget(self.pitch_spin)
        layout.addLayout(controls)

        pauses = QHBoxLayout()
        pauses.addWidget(QLabel("句间停顿"))
        self.sentence_pause_spin = QSpinBox()
        self.sentence_pause_spin.setRange(0, 3000)
        self.sentence_pause_spin.setSingleStep(50)
        self.sentence_pause_spin.setSuffix(" ms")
        pauses.addWidget(self.sentence_pause_spin)
        pauses.addWidget(QLabel("段间停顿"))
        self.paragraph_pause_spin = QSpinBox()
        self.paragraph_pause_spin.setRange(0, 3000)
        self.paragraph_pause_spin.setSingleStep(50)
        self.paragraph_pause_spin.setSuffix(" ms")
        pauses.addWidget(self.paragraph_pause_spin)
        pauses.addStretch(1)
        self.open_audio_button = QPushButton("打开配音文件")
        self.open_audio_button.clicked.connect(self.open_audio_requested.emit)
        pauses.addWidget(self.open_audio_button)
        self.open_dir_button = QPushButton("配音目录")
        self.open_dir_button.clicked.connect(self.open_dir_requested.emit)
        pauses.addWidget(self.open_dir_button)
        layout.addLayout(pauses)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["句", "开始", "结束", "时长", "引擎", "文本"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(self._on_row_changed)
        splitter.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择一行查看完整配音文本。")
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self._load_config(TTSConfig())
        self._update_controls()

    def _load_config(self, config: TTSConfig) -> None:
        index = self.engine_combo.findData(config.engine)
        if index >= 0:
            self.engine_combo.setCurrentIndex(index)
        voice_index = self.voice_combo.findData(config.edge_voice)
        if voice_index >= 0:
            self.voice_combo.setCurrentIndex(voice_index)
        self.rate_spin.setValue(config.rate_percent)
        self.volume_spin.setValue(config.volume_percent)
        self.pitch_spin.setValue(config.pitch_hz)
        self.sentence_pause_spin.setValue(config.sentence_pause_ms)
        self.paragraph_pause_spin.setValue(config.paragraph_pause_ms)

    def set_config(self, config: TTSConfig) -> None:
        self._load_config(config)

    def generation_settings(self) -> dict[str, object]:
        return {
            "engine": str(self.engine_combo.currentData() or "auto"),
            "edge_voice": str(self.voice_combo.currentData() or "zh-CN-XiaoxiaoNeural"),
            "rate_percent": self.rate_spin.value(),
            "volume_percent": self.volume_spin.value(),
            "pitch_hz": self.pitch_spin.value(),
            "sentence_pause_ms": self.sentence_pause_spin.value(),
            "paragraph_pause_ms": self.paragraph_pause_spin.value(),
        }

    def set_script_available(self, available: bool) -> None:
        self._script_available = available
        self._update_controls()

    def set_voiceover(self, voiceover: Voiceover | None) -> None:
        self.voiceover = voiceover
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.detail.clear()
        if voiceover is None:
            self.table.setRowCount(0)
            self._status_text = "尚无配音"
            self.status_label.setText(self._status_text)
            self.table.setUpdatesEnabled(True)
            self._update_controls()
            return
        self.table.setRowCount(len(voiceover.units))
        for row, unit in enumerate(voiceover.units):
            values = [
                str(unit.index),
                format_clock(unit.start_s),
                format_clock(unit.end_s),
                f"{unit.duration_s:.2f}s",
                unit.engine,
                unit.text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in (0, 1, 2, 3):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 4:
                    item.setToolTip("使用的语音引擎")
                self.table.setItem(row, column, item)
        self.table.setUpdatesEnabled(True)
        cache = " · 全部缓存" if voiceover.cached else ""
        self._status_text = (
            f"配音完成：{format_clock(voiceover.total_duration_s)} · "
            f"{voiceover.unit_count} 句 · {voiceover.engine} · 费用 ¥0{cache}"
        )
        self.status_label.setText(self._status_text)
        self._update_controls()
        if voiceover.units:
            self.table.setCurrentCell(0, 0)

    def set_stale(self, stale: bool) -> None:
        if stale and self.voiceover is not None:
            self._status_text = "当前配音基于旧文案版本，请重新生成配音。"
            self.status_label.setText(self._status_text)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message:
            self._status_text = message
            self.status_label.setText(message)
        elif busy:
            self.status_label.setText("配音生成中…")
        else:
            self.status_label.setText(self._status_text)
        self._update_controls()

    def _update_controls(self) -> None:
        can_generate = self._script_available and not self._busy
        self.generate_button.setEnabled(can_generate)
        for widget in (
            self.engine_combo,
            self.voice_combo,
            self.rate_spin,
            self.volume_spin,
            self.pitch_spin,
            self.sentence_pause_spin,
            self.paragraph_pause_spin,
        ):
            widget.setEnabled(not self._busy)
        has_audio = bool(self.voiceover and self.voiceover.audio_path)
        self.open_audio_button.setEnabled(has_audio)
        self.open_dir_button.setEnabled(has_audio)
        if not self._script_available:
            self.generate_button.setToolTip("请先生成解说文案")
        else:
            self.generate_button.setToolTip("逐句合成配音并生成句子级时间轴")

    def _on_row_changed(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        if self.voiceover is None or row < 0 or row >= len(self.voiceover.units):
            return
        unit: SpeechUnit = self.voiceover.units[row]
        self.detail.setPlainText(
            f"第 {unit.index} 句（段落 {unit.paragraph_index}）\n"
            f"{format_clock(unit.start_s)} → {format_clock(unit.end_s)}"
            f"（{unit.duration_s:.2f} 秒，{unit.engine}）\n"
            f"剧情片段：{unit.segment_ids or '—'}　候选镜头：{unit.shot_ids or '—'}\n\n"
            f"{unit.text}"
        )

    def _emit_generate(self) -> None:
        self.generate_requested.emit(self.generation_settings())
