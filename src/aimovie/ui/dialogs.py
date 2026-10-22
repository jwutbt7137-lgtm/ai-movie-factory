"""新建项目、设置等对话框。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, has_deepseek_api_key, resolve_executable
from ..media.ffmpeg import check_tool_version
from ..utils import SUPPORTED_VIDEO_EXTENSIONS, open_in_explorer


class NewProjectDialog(QDialog):
    """新建项目：输入项目名并选择影片文件。"""

    def __init__(self, default_dir: Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("新建项目 / 导入影片")
        self.setMinimumWidth(620)
        self._default_dir = default_dir or Path.home()

        layout = QVBoxLayout(self)
        title = QLabel("导入一部具有合法使用依据的影片")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        hint = QLabel("支持 MP4、MKV、MOV、AVI、WEBM 等常见格式。影片只在本机处理，不会上传。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(10)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：某电影解说（留空则使用文件名）")
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择影片文件…")
        browse_button = QPushButton("浏览…")
        browse_button.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_button)
        path_widget = QWidget()
        path_widget.setLayout(path_row)
        path_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("项目名称", self.name_edit)
        form.addRow("影片文件", path_widget)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _browse(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_VIDEO_EXTENSIONS))
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择影片文件",
            str(self._default_dir),
            f"视频文件 ({patterns});;所有文件 (*.*)",
        )
        if filename:
            self.path_edit.setText(filename)
            if not self.name_edit.text().strip():
                self.name_edit.setText(Path(filename).stem)

    def _on_accept(self) -> None:
        source = Path(self.path_edit.text().strip().strip('"'))
        if not source.exists() or not source.is_file():
            QMessageBox.warning(self, "无法导入", "请选择一个存在的影片文件。")
            return
        if source.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            answer = QMessageBox.question(
                self,
                "非常见格式",
                f"扩展名 {source.suffix or '（无）'} 不在常见视频格式列表中。\n仍要尝试导入吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if not self.name_edit.text().strip():
            self.name_edit.setText(source.stem)
        self.accept()

    def values(self) -> tuple[str, Path]:
        source = Path(self.path_edit.text().strip().strip('"'))
        name = self.name_edit.text().strip() or source.stem
        return name, source


class SettingsDialog(QDialog):
    """系统设置：模型、镜头检测、关键帧、路径、DeepSeek 参数。"""

    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("系统设置")
        self.setMinimumSize(680, 600)
        self._config = config.model_copy(deep=True)
        self.result_config: AppConfig | None = None

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_paths_tab()
        self._build_scene_tab()
        self._build_whisper_tab()
        self._build_keyframe_tab()
        self._build_deepseek_tab()
        self._build_ai_tab()
        self._build_tts_tab()
        # 配音与匹配字段较多，放入滚动区，避免小屏幕被截断
        tts_widget = self.tabs.widget(6)
        tts_scroll = QScrollArea()
        tts_scroll.setWidgetResizable(True)
        tts_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.tabs.removeTab(6)
        tts_scroll.setWidget(tts_widget)
        self.tabs.insertTab(6, tts_scroll, "配音与匹配")

        hint = QLabel("设置保存后立即写入配置文件；分析与配音参数在下一次任务开始时生效。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    # ------------------------------------------------------------------ 构建
    def _new_tab(self) -> tuple[QWidget, QFormLayout]:
        widget = QWidget()
        form = QFormLayout(widget)
        form.setContentsMargins(16, 16, 16, 16)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.tabs.addTab(widget, "")
        return widget, form

    def _build_paths_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(0, "路径与日志")

        self.ffmpeg_edit = QLineEdit(self._config.ffmpeg_path)
        ffmpeg_browse = QPushButton("浏览…")
        ffmpeg_browse.clicked.connect(lambda: self._browse_executable(self.ffmpeg_edit))
        ffmpeg_row = QHBoxLayout()
        ffmpeg_row.setContentsMargins(0, 0, 0, 0)
        ffmpeg_row.addWidget(self.ffmpeg_edit, 1)
        ffmpeg_row.addWidget(ffmpeg_browse)
        ffmpeg_widget = QWidget()
        ffmpeg_widget.setLayout(ffmpeg_row)
        form.addRow("FFmpeg 路径", ffmpeg_widget)

        self.ffprobe_edit = QLineEdit(self._config.ffprobe_path)
        ffprobe_browse = QPushButton("浏览…")
        ffprobe_browse.clicked.connect(lambda: self._browse_executable(self.ffprobe_edit))
        ffprobe_row = QHBoxLayout()
        ffprobe_row.setContentsMargins(0, 0, 0, 0)
        ffprobe_row.addWidget(self.ffprobe_edit, 1)
        ffprobe_row.addWidget(ffprobe_browse)
        ffprobe_widget = QWidget()
        ffprobe_widget.setLayout(ffprobe_row)
        form.addRow("ffprobe 路径", ffprobe_widget)

        self.test_ffmpeg_label = QLabel("点击右侧按钮检测 FFmpeg/ffprobe 是否可用")
        self.test_ffmpeg_label.setObjectName("HintLabel")
        test_button = QPushButton("检测")
        test_button.clicked.connect(self._test_ffmpeg)
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.addWidget(self.test_ffmpeg_label, 1)
        test_row.addWidget(test_button)
        test_widget = QWidget()
        test_widget.setLayout(test_row)
        form.addRow("工具自检", test_widget)

        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level_combo.setCurrentText(self._config.log_level)
        form.addRow("日志级别", self.log_level_combo)

        data_dir = self._config.resolved_data_dir()
        self.data_dir_edit = QLineEdit(str(data_dir))
        self.data_dir_edit.setReadOnly(True)
        open_data_button = QPushButton("打开")
        open_data_button.clicked.connect(lambda: self._open_dir(data_dir))
        data_row = QHBoxLayout()
        data_row.setContentsMargins(0, 0, 0, 0)
        data_row.addWidget(self.data_dir_edit, 1)
        data_row.addWidget(open_data_button)
        data_widget = QWidget()
        data_widget.setLayout(data_row)
        form.addRow("数据目录", data_widget)

        hint = QLabel("数据目录由环境变量 AIMOVIE_DATA_DIR 或默认位置决定，暂不支持在界面中迁移。")
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_scene_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(1, "镜头检测")
        self.scene_backend_combo = QComboBox()
        self.scene_backend_combo.addItem("自动（优先 PySceneDetect，失败回退 FFmpeg）", "auto")
        self.scene_backend_combo.addItem("PySceneDetect", "pyscenedetect")
        self.scene_backend_combo.addItem("FFmpeg scene 滤镜", "ffmpeg")
        index = self.scene_backend_combo.findData(self._config.scene.backend)
        self.scene_backend_combo.setCurrentIndex(max(0, index))
        form.addRow("检测后端", self.scene_backend_combo)

        self.scene_threshold = QDoubleSpinBox()
        self.scene_threshold.setRange(1.0, 100.0)
        self.scene_threshold.setDecimals(1)
        self.scene_threshold.setSingleStep(1.0)
        self.scene_threshold.setValue(self._config.scene.threshold)
        form.addRow("内容阈值", self.scene_threshold)

        self.min_scene_frames = QSpinBox()
        self.min_scene_frames.setRange(1, 3000)
        self.min_scene_frames.setValue(self._config.scene.min_scene_len_frames)
        form.addRow("最短镜头（帧）", self.min_scene_frames)

        self.ffmpeg_scene_threshold = QDoubleSpinBox()
        self.ffmpeg_scene_threshold.setRange(0.01, 0.99)
        self.ffmpeg_scene_threshold.setDecimals(2)
        self.ffmpeg_scene_threshold.setSingleStep(0.05)
        self.ffmpeg_scene_threshold.setValue(self._config.scene.ffmpeg_threshold)
        form.addRow("FFmpeg 回退阈值", self.ffmpeg_scene_threshold)

        self.analyze_width = QSpinBox()
        self.analyze_width.setRange(0, 1920)
        self.analyze_width.setSingleStep(80)
        self.analyze_width.setValue(self._config.scene.analyze_width)
        self.analyze_width.setSpecialValueText("不缩放")
        form.addRow("FFmpeg 分析宽度", self.analyze_width)

        hint = QLabel(
            "阈值越低切得越碎。自动模式优先使用 PySceneDetect ContentDetector，"
            "检测库不可用或失败时回退到 FFmpeg。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_whisper_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(2, "语音识别")

        self.whisper_model = QComboBox()
        self.whisper_model.setEditable(True)
        self.whisper_model.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.whisper_model.setCurrentText(self._config.whisper.model_size)
        form.addRow("Whisper 模型", self.whisper_model)

        self.whisper_device = QComboBox()
        self.whisper_device.addItems(["cpu", "cuda", "auto"])
        self.whisper_device.setCurrentText(self._config.whisper.device)
        form.addRow("计算设备", self.whisper_device)

        self.whisper_compute = QComboBox()
        self.whisper_compute.addItems(["int8", "float16", "int8_float16", "float32"])
        self.whisper_compute.setCurrentText(self._config.whisper.compute_type)
        form.addRow("计算精度", self.whisper_compute)

        self.whisper_language = QComboBox()
        self.whisper_language.addItem("中文（zh）", "zh")
        self.whisper_language.addItem("英文（en）", "en")
        self.whisper_language.addItem("日文（ja）", "ja")
        self.whisper_language.addItem("自动检测", "")
        index = self.whisper_language.findData(self._config.whisper.language)
        self.whisper_language.setCurrentIndex(max(0, index))
        form.addRow("识别语言", self.whisper_language)

        self.whisper_beam = QSpinBox()
        self.whisper_beam.setRange(1, 10)
        self.whisper_beam.setValue(self._config.whisper.beam_size)
        form.addRow("Beam size", self.whisper_beam)

        self.whisper_vad = QCheckBox("启用 VAD（过滤静音段，推荐）")
        self.whisper_vad.setChecked(self._config.whisper.vad_filter)
        form.addRow("静音过滤", self.whisper_vad)

        self.whisper_prompt = QLineEdit(self._config.whisper.initial_prompt)
        form.addRow("初始提示词", self.whisper_prompt)

        self.whisper_threads = QSpinBox()
        self.whisper_threads.setRange(0, 128)
        self.whisper_threads.setValue(self._config.whisper.cpu_threads)
        self.whisper_threads.setSpecialValueText("自动")
        form.addRow("CPU 线程数", self.whisper_threads)

        self.whisper_download_root = QLineEdit(self._config.whisper.download_root)
        self.whisper_download_root.setPlaceholderText(str(self._config.models_dir() / "whisper"))
        browse = QPushButton("浏览…")
        browse.clicked.connect(lambda: self._browse_directory(self.whisper_download_root))
        root_row = QHBoxLayout()
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.addWidget(self.whisper_download_root, 1)
        root_row.addWidget(browse)
        root_widget = QWidget()
        root_widget.setLayout(root_row)
        form.addRow("模型目录", root_widget)

        hint = QLabel(
            "首次使用所选模型时会联网下载到模型目录；识别在本机完成，不会上传影片音频。"
            "长片建议使用 small 或以上模型，CPU 上会较慢。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_keyframe_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(3, "关键帧")
        self.keyframe_enabled = QCheckBox("为镜头生成预览缩略图")
        self.keyframe_enabled.setChecked(self._config.keyframes.enabled)
        form.addRow("启用", self.keyframe_enabled)

        self.keyframe_width = QSpinBox()
        self.keyframe_width.setRange(80, 1920)
        self.keyframe_width.setSingleStep(40)
        self.keyframe_width.setValue(self._config.keyframes.width)
        form.addRow("缩略图宽度（px）", self.keyframe_width)

        self.keyframe_quality = QSpinBox()
        self.keyframe_quality.setRange(30, 100)
        self.keyframe_quality.setValue(self._config.keyframes.jpeg_quality)
        form.addRow("JPEG 质量", self.keyframe_quality)

        self.keyframe_max = QSpinBox()
        self.keyframe_max.setRange(0, 5000)
        self.keyframe_max.setValue(self._config.keyframes.max_shots)
        self.keyframe_max.setSpecialValueText("不限制")
        form.addRow("最多缩略图数量", self.keyframe_max)

        hint = QLabel(
            "缩略图仅用于界面预览，会写入项目目录。长片镜头很多时，限制数量可以明显缩短处理时间。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_deepseek_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(4, "DeepSeek（第二阶段）")
        key_status = "已检测到环境变量中的 API Key" if has_deepseek_api_key(self._config) else "未检测到 API Key"
        key_label = QLabel(key_status)
        key_label.setObjectName("HintLabel")
        key_label.setWordWrap(True)
        form.addRow("密钥状态", key_label)

        self.deepseek_key_env = QLineEdit(self._config.deepseek.api_key_env)
        form.addRow("密钥环境变量名", self.deepseek_key_env)

        self.deepseek_base_url = QLineEdit(self._config.deepseek.base_url)
        form.addRow("接口地址", self.deepseek_base_url)

        self.deepseek_model = QLineEdit(self._config.deepseek.model)
        form.addRow("文本模型", self.deepseek_model)

        self.deepseek_vision_model = QLineEdit(self._config.deepseek.vision_model)
        self.deepseek_vision_model.setPlaceholderText("留空表示不使用视觉模型")
        form.addRow("视觉模型", self.deepseek_vision_model)

        self.deepseek_vision_base_url = QLineEdit(self._config.deepseek.vision_base_url)
        self.deepseek_vision_base_url.setPlaceholderText("留空则使用上面的接口地址")
        form.addRow("视觉接口地址", self.deepseek_vision_base_url)

        self.deepseek_timeout = QDoubleSpinBox()
        self.deepseek_timeout.setRange(1.0, 1200.0)
        self.deepseek_timeout.setDecimals(1)
        self.deepseek_timeout.setValue(self._config.deepseek.timeout_s)
        form.addRow("超时（秒）", self.deepseek_timeout)

        self.deepseek_retries = QSpinBox()
        self.deepseek_retries.setRange(0, 10)
        self.deepseek_retries.setValue(self._config.deepseek.max_retries)
        form.addRow("重试次数", self.deepseek_retries)

        self.deepseek_input_price = QDoubleSpinBox()
        self.deepseek_input_price.setRange(0.0, 1000.0)
        self.deepseek_input_price.setDecimals(4)
        self.deepseek_input_price.setValue(self._config.deepseek.price_input_cny_per_mtok)
        form.addRow("输入价（元/百万 tokens）", self.deepseek_input_price)

        self.deepseek_output_price = QDoubleSpinBox()
        self.deepseek_output_price.setRange(0.0, 1000.0)
        self.deepseek_output_price.setDecimals(4)
        self.deepseek_output_price.setValue(self._config.deepseek.price_output_cny_per_mtok)
        form.addRow("输出价（元/百万 tokens）", self.deepseek_output_price)

        self.deepseek_budget = QDoubleSpinBox()
        self.deepseek_budget.setRange(0.0, 10000.0)
        self.deepseek_budget.setDecimals(2)
        self.deepseek_budget.setValue(self._config.deepseek.budget_cny_per_project)
        form.addRow("单项目预算（元）", self.deepseek_budget)

        hint = QLabel(
            "调用 DeepSeek 会产生 API 费用；API Key 只从环境变量读取，不写入配置文件或日志。"
            "价格与模型参数仅用于后续阶段的费用估算和预算控制，可随时修改。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_ai_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(5, "AI 生成")
        self.story_chunk_chars = QSpinBox()
        self.story_chunk_chars.setRange(500, 30000)
        self.story_chunk_chars.setSingleStep(500)
        self.story_chunk_chars.setValue(self._config.story.chunk_target_chars)
        form.addRow("剧情分段字符数", self.story_chunk_chars)

        self.story_duration = QSpinBox()
        self.story_duration.setRange(60, 3600)
        self.story_duration.setSingleStep(60)
        self.story_duration.setSuffix(" 秒")
        self.story_duration.setValue(int(self._config.story.chunk_max_duration_s))
        form.addRow("单段最长时长", self.story_duration)

        self.story_min_chars = QSpinBox()
        self.story_min_chars.setRange(1, 5000)
        self.story_min_chars.setValue(self._config.story.min_dialogue_chars)
        form.addRow("最少对白字符数", self.story_min_chars)

        self.story_max_tokens = QSpinBox()
        self.story_max_tokens.setRange(256, 16000)
        self.story_max_tokens.setSingleStep(256)
        self.story_max_tokens.setValue(self._config.story.analysis_max_tokens)
        form.addRow("每段输出 token 上限", self.story_max_tokens)

        self.story_temperature = QDoubleSpinBox()
        self.story_temperature.setRange(0.0, 2.0)
        self.story_temperature.setDecimals(2)
        self.story_temperature.setSingleStep(0.1)
        self.story_temperature.setValue(self._config.story.analysis_temperature)
        form.addRow("剧情分析温度", self.story_temperature)

        self.copy_cpm = QSpinBox()
        self.copy_cpm.setRange(120, 400)
        self.copy_cpm.setValue(self._config.copywriting.chars_per_minute)
        form.addRow("解说语速（字/分钟）", self.copy_cpm)

        self.copy_default_minutes = QDoubleSpinBox()
        self.copy_default_minutes.setRange(1.0, 30.0)
        self.copy_default_minutes.setDecimals(1)
        self.copy_default_minutes.setValue(self._config.copywriting.default_minutes)
        form.addRow("默认目标时长（分钟）", self.copy_default_minutes)

        self.copy_temperature = QDoubleSpinBox()
        self.copy_temperature.setRange(0.0, 2.0)
        self.copy_temperature.setDecimals(2)
        self.copy_temperature.setSingleStep(0.1)
        self.copy_temperature.setValue(self._config.copywriting.temperature)
        form.addRow("文案温度", self.copy_temperature)

        self.copy_max_paragraphs = QSpinBox()
        self.copy_max_paragraphs.setRange(1, 100)
        self.copy_max_paragraphs.setValue(self._config.copywriting.max_paragraphs)
        form.addRow("文案段落上限", self.copy_max_paragraphs)

        self.cache_enabled = QCheckBox("启用本地请求缓存（相同请求不重复计费）")
        self.cache_enabled.setChecked(self._config.deepseek.cache_enabled)
        form.addRow("缓存", self.cache_enabled)

        self.retry_backoff = QDoubleSpinBox()
        self.retry_backoff.setRange(0.0, 30.0)
        self.retry_backoff.setDecimals(1)
        self.retry_backoff.setValue(self._config.deepseek.retry_backoff_s)
        form.addRow("重试退避（秒）", self.retry_backoff)

        hint = QLabel(
            "剧情分析会按分段调用模型，缓存命中不重复计费；预算上限在“DeepSeek”页设置。"
            "长片建议增加分段字符数以降低调用次数，但单次上下文会变长。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    def _build_tts_tab(self) -> None:
        widget, form = self._new_tab()
        self.tabs.setTabText(6, "配音与匹配")

        self.tts_engine = QComboBox()
        self.tts_engine.addItem("自动（优先本地 Piper）", "auto")
        self.tts_engine.addItem("Edge 在线语音", "edge")
        self.tts_engine.addItem("Piper 本地语音", "piper")
        index = self.tts_engine.findData(self._config.tts.engine)
        self.tts_engine.setCurrentIndex(max(0, index))
        form.addRow("配音引擎", self.tts_engine)

        self.tts_voice = QComboBox()
        self.tts_voice.setEditable(True)
        voices = [
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-YunxiNeural",
            "zh-CN-YunjianNeural",
            "zh-CN-XiaoyiNeural",
            "zh-CN-YunyangNeural",
            "zh-CN-liaoning-XiaobeiNeural",
            "zh-CN-shaanxi-XiaoniNeural",
            "zh-HK-HiuMaanNeural",
            "zh-TW-HsiaoChenNeural",
        ]
        self.tts_voice.addItems(voices)
        self.tts_voice.setCurrentText(self._config.tts.edge_voice)
        form.addRow("Edge 声音", self.tts_voice)

        self.piper_path = QLineEdit(self._config.tts.piper_model_path)
        self.piper_path.setPlaceholderText("可选：选择中文 Piper .onnx 模型")
        piper_browse = QPushButton("浏览…")
        piper_browse.clicked.connect(lambda: self._browse_model(self.piper_path))
        piper_row = QHBoxLayout()
        piper_row.setContentsMargins(0, 0, 0, 0)
        piper_row.addWidget(self.piper_path, 1)
        piper_row.addWidget(piper_browse)
        piper_widget = QWidget()
        piper_widget.setLayout(piper_row)
        form.addRow("Piper 模型", piper_widget)

        self.tts_rate = QSpinBox()
        self.tts_rate.setRange(-50, 100)
        self.tts_rate.setSuffix("%")
        self.tts_rate.setValue(self._config.tts.rate_percent)
        form.addRow("语速", self.tts_rate)

        self.tts_volume = QSpinBox()
        self.tts_volume.setRange(-50, 50)
        self.tts_volume.setSuffix("%")
        self.tts_volume.setValue(self._config.tts.volume_percent)
        form.addRow("音量", self.tts_volume)

        self.tts_pitch = QSpinBox()
        self.tts_pitch.setRange(-50, 50)
        self.tts_pitch.setSuffix("Hz")
        self.tts_pitch.setValue(self._config.tts.pitch_hz)
        form.addRow("音高（Edge）", self.tts_pitch)

        self.tts_sentence_pause = QSpinBox()
        self.tts_sentence_pause.setRange(0, 3000)
        self.tts_sentence_pause.setSingleStep(50)
        self.tts_sentence_pause.setSuffix(" ms")
        self.tts_sentence_pause.setValue(self._config.tts.sentence_pause_ms)
        form.addRow("句间停顿", self.tts_sentence_pause)

        self.tts_paragraph_pause = QSpinBox()
        self.tts_paragraph_pause.setRange(0, 3000)
        self.tts_paragraph_pause.setSingleStep(50)
        self.tts_paragraph_pause.setSuffix(" ms")
        self.tts_paragraph_pause.setValue(self._config.tts.paragraph_pause_ms)
        form.addRow("段间停顿", self.tts_paragraph_pause)

        self.tts_max_chars = QSpinBox()
        self.tts_max_chars.setRange(20, 300)
        self.tts_max_chars.setValue(self._config.tts.max_sentence_chars)
        form.addRow("每句最大字符", self.tts_max_chars)

        self.tts_fallback = QCheckBox("主引擎失败时自动切换备用引擎")
        self.tts_fallback.setChecked(self._config.tts.fallback_enabled)
        form.addRow("备用引擎", self.tts_fallback)

        self.tts_cache = QCheckBox("复用本地逐句音频缓存")
        self.tts_cache.setChecked(self._config.tts.cache_enabled)
        form.addRow("缓存", self.tts_cache)

        self.match_threshold = QDoubleSpinBox()
        self.match_threshold.setRange(0.0, 1.0)
        self.match_threshold.setDecimals(2)
        self.match_threshold.setSingleStep(0.05)
        self.match_threshold.setValue(self._config.matching.confidence_threshold)
        form.addRow("匹配置信度阈值", self.match_threshold)

        self.match_margin = QDoubleSpinBox()
        self.match_margin.setRange(0.0, 5.0)
        self.match_margin.setDecimals(2)
        self.match_margin.setSingleStep(0.05)
        self.match_margin.setValue(self._config.matching.duration_margin_s)
        form.addRow("镜头时长余量（秒）", self.match_margin)

        self.match_max_shots = QSpinBox()
        self.match_max_shots.setRange(1, 20)
        self.match_max_shots.setValue(self._config.matching.max_shots_per_unit)
        form.addRow("每句最多镜头", self.match_max_shots)

        self.match_extend = QCheckBox("允许扩展到相邻镜头补足时长")
        self.match_extend.setChecked(self._config.matching.allow_extend)
        form.addRow("补镜", self.match_extend)

        hint = QLabel(
            "Edge 免费、在线，需要网络；Piper 本地运行，需要自行准备中文 ONNX 模型和许可。"
            "逐句音频会缓存在项目目录；完整配音同时生成 WAV 和句子时间轴。"
        )
        hint.setObjectName("HintLabel")
        hint.setWordWrap(True)
        form.addRow("", hint)

    # ------------------------------------------------------------------ 交互
    def _browse_model(self, edit: QLineEdit) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择 Piper ONNX 模型", str(Path.home()), "ONNX 模型 (*.onnx);;所有文件 (*.*)"
        )
        if filename:
            edit.setText(filename)

    def _browse_executable(self, edit: QLineEdit) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择可执行文件", str(Path(edit.text()).parent if edit.text() else Path.home()), "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if filename:
            edit.setText(filename)

    def _browse_directory(self, edit: QLineEdit) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or str(Path.home()))
        if directory:
            edit.setText(directory)

    def _open_dir(self, path: Path) -> None:
        try:
            open_in_explorer(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "无法打开目录", str(exc))

    def _test_ffmpeg(self) -> None:
        try:
            ffmpeg = resolve_executable(self.ffmpeg_edit.text().strip(), "ffmpeg")
            ffprobe = resolve_executable(self.ffprobe_edit.text().strip(), "ffprobe")
            ffmpeg_version = check_tool_version(ffmpeg)
            ffprobe_version = check_tool_version(ffprobe)
        except Exception as exc:  # noqa: BLE001
            self.test_ffmpeg_label.setText(f"检测失败：{exc}")
            return
        self.test_ffmpeg_label.setText(
            f"正常：{ffmpeg_version.split(' Copyright')[0][:70]} / ffprobe {ffprobe_version[:40]}"
        )

    def _on_accept(self) -> None:
        config = self._config
        try:
            config.ffmpeg_path = self.ffmpeg_edit.text().strip() or "ffmpeg"
            config.ffprobe_path = self.ffprobe_edit.text().strip() or "ffprobe"
            config.log_level = self.log_level_combo.currentText()  # type: ignore[assignment]
            config.scene.backend = self.scene_backend_combo.currentData()  # type: ignore[assignment]
            config.scene.threshold = self.scene_threshold.value()
            config.scene.min_scene_len_frames = self.min_scene_frames.value()
            config.scene.ffmpeg_threshold = self.ffmpeg_scene_threshold.value()
            config.scene.analyze_width = self.analyze_width.value()
            config.whisper.model_size = self.whisper_model.currentText().strip() or "small"
            config.whisper.device = self.whisper_device.currentText()
            config.whisper.compute_type = self.whisper_compute.currentText()
            config.whisper.language = self.whisper_language.currentData() or ""
            config.whisper.beam_size = self.whisper_beam.value()
            config.whisper.vad_filter = self.whisper_vad.isChecked()
            config.whisper.initial_prompt = self.whisper_prompt.text()
            config.whisper.cpu_threads = self.whisper_threads.value()
            config.whisper.download_root = self.whisper_download_root.text().strip()
            config.keyframes.enabled = self.keyframe_enabled.isChecked()
            config.keyframes.width = self.keyframe_width.value()
            config.keyframes.jpeg_quality = self.keyframe_quality.value()
            config.keyframes.max_shots = self.keyframe_max.value()
            config.deepseek.api_key_env = self.deepseek_key_env.text().strip() or "DEEPSEEK_API_KEY"
            config.deepseek.base_url = self.deepseek_base_url.text().strip() or "https://api.deepseek.com"
            config.deepseek.model = self.deepseek_model.text().strip() or "deepseek-chat"
            config.deepseek.vision_model = self.deepseek_vision_model.text().strip()
            config.deepseek.vision_base_url = self.deepseek_vision_base_url.text().strip()
            config.deepseek.timeout_s = self.deepseek_timeout.value()
            config.deepseek.max_retries = self.deepseek_retries.value()
            config.deepseek.price_input_cny_per_mtok = self.deepseek_input_price.value()
            config.deepseek.price_output_cny_per_mtok = self.deepseek_output_price.value()
            config.deepseek.budget_cny_per_project = self.deepseek_budget.value()
            config.deepseek.cache_enabled = self.cache_enabled.isChecked()
            config.deepseek.retry_backoff_s = self.retry_backoff.value()
            config.story.chunk_target_chars = self.story_chunk_chars.value()
            config.story.chunk_max_duration_s = float(self.story_duration.value())
            config.story.min_dialogue_chars = self.story_min_chars.value()
            config.story.analysis_max_tokens = self.story_max_tokens.value()
            config.story.analysis_temperature = self.story_temperature.value()
            config.copywriting.chars_per_minute = self.copy_cpm.value()
            config.copywriting.default_minutes = self.copy_default_minutes.value()
            config.copywriting.temperature = self.copy_temperature.value()
            config.copywriting.max_paragraphs = self.copy_max_paragraphs.value()
            config.tts.engine = self.tts_engine.currentData()  # type: ignore[assignment]
            config.tts.edge_voice = self.tts_voice.currentText().strip() or "zh-CN-XiaoxiaoNeural"
            config.tts.piper_model_path = self.piper_path.text().strip()
            config.tts.rate_percent = self.tts_rate.value()
            config.tts.volume_percent = self.tts_volume.value()
            config.tts.pitch_hz = self.tts_pitch.value()
            config.tts.sentence_pause_ms = self.tts_sentence_pause.value()
            config.tts.paragraph_pause_ms = self.tts_paragraph_pause.value()
            config.tts.max_sentence_chars = self.tts_max_chars.value()
            config.tts.fallback_enabled = self.tts_fallback.isChecked()
            config.tts.cache_enabled = self.tts_cache.isChecked()
            config.matching.confidence_threshold = self.match_threshold.value()
            config.matching.duration_margin_s = self.match_margin.value()
            config.matching.max_shots_per_unit = self.match_max_shots.value()
            config.matching.allow_extend = self.match_extend.isChecked()
            self.result_config = config.model_copy(deep=True)
        except Exception as exc:  # noqa: BLE001 - Pydantic 校验错误统一提示
            QMessageBox.warning(self, "设置有误", f"请检查输入：{exc}")
            return
        self.accept()
