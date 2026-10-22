"""应用配置。

所有模型名称、接口地址、超时、重试、价格、音视频参数均集中在此处，
通过 ``data/config.json`` 持久化，便于后续阶段在不改代码的情况下调整。
API Key 不保存在配置文件中：只从环境变量读取（见 :func:`get_deepseek_api_key`）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_VERSION = "0.4.0"

#: DeepSeek 密钥环境变量名（配置中可改名，但值绝不会写入日志或配置文件）
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"


def project_root() -> Path:
    """返回源码项目根目录；若以非源码方式安装则返回应用数据目录。"""
    here = Path(__file__).resolve()
    # src/aimovie/config.py -> parents[1] == src
    if len(here.parents) >= 3 and here.parents[1].name.lower() == "src":
        return here.parents[2]
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "AIMovieFactory"


def default_data_dir() -> Path:
    """默认数据目录：可用环境变量 ``AIMOVIE_DATA_DIR`` 覆盖。"""
    env = os.environ.get("AIMOVIE_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (project_root() / "data").resolve()


def config_path() -> Path:
    """配置文件路径：``AIMOVIE_CONFIG`` > 数据目录下 config.json。"""
    env = os.environ.get("AIMOVIE_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return default_data_dir() / "config.json"


class SceneConfig(BaseModel):
    """镜头检测参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    backend: Literal["auto", "pyscenedetect", "ffmpeg"] = "auto"
    #: ContentDetector 阈值，越低切得越碎
    threshold: float = Field(default=27.0, ge=1.0, le=100.0)
    #: 最短镜头（帧），约等于 0.5 秒@24fps
    min_scene_len_frames: int = Field(default=15, ge=1, le=3000)
    #: 回退后端使用的 ffmpeg scene 分数阈值
    ffmpeg_threshold: float = Field(default=0.20, ge=0.01, le=0.99)
    #: 检测时缩放宽度（0 表示不缩放，使用原分辨率）
    analyze_width: int = Field(default=640, ge=0, le=1920)


class KeyframeConfig(BaseModel):
    """关键帧/缩略图提取参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    enabled: bool = True
    width: int = Field(default=320, ge=80, le=1920)
    jpeg_quality: int = Field(default=82, ge=30, le=100)
    #: 最多提取多少个镜头的缩略图，0 表示不限（长片建议限制）
    max_shots: int = Field(default=800, ge=0)


class WhisperConfig(BaseModel):
    """faster-whisper 本地语音识别参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: tiny/base/small/medium/large-v3，或本地模型目录
    model_size: str = "small"
    device: str = "cpu"  # cpu / cuda / auto
    compute_type: str = "int8"  # int8 / float16 / int8_float16 / float32
    language: str = "zh"  # 空字符串表示自动检测
    beam_size: int = Field(default=5, ge=1, le=10)
    vad_filter: bool = True
    initial_prompt: str = "以下是普通话电影对白，请使用规范的简体中文标点。"
    cpu_threads: int = Field(default=0, ge=0, le=128)
    #: 模型缓存目录；为空时使用 <数据目录>/models/whisper
    download_root: str = ""

    @field_validator("model_size")
    @classmethod
    def _model_size_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("语音模型名称不能为空")
        return value


class DeepSeekConfig(BaseModel):
    """DeepSeek 接口参数。

    模型、地址、超时、重试、单价与预算均可配置，不写死在业务代码中。
    API Key 只从环境变量读取，绝不写入配置或日志。
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    #: 若使用支持图像输入的视觉模型，可在此配置；文本模型不会假装看图
    vision_model: str = ""
    vision_base_url: str = ""
    api_key_env: str = DEEPSEEK_API_KEY_ENV
    timeout_s: float = Field(default=120.0, gt=0, le=1200)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_backoff_s: float = Field(default=1.0, ge=0, le=30)
    #: 相同请求命中本地缓存时不重复计费
    cache_enabled: bool = True
    #: 参考单价（CNY / 百万 tokens），仅用于估算；用户可在设置中修改
    price_input_cny_per_mtok: float = Field(default=2.0, ge=0)
    price_output_cny_per_mtok: float = Field(default=8.0, ge=0)
    #: 单项目预算上限（CNY）；0 表示不限制。预计超过时必须先确认
    budget_cny_per_project: float = Field(default=5.0, ge=0)


class StoryConfig(BaseModel):
    """分段剧情分析参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: 每个分段目标对白字符数（越大调用越少、上下文越长）
    chunk_target_chars: int = Field(default=4500, ge=500, le=30000)
    #: 单个分段最长覆盖的影片时长
    chunk_max_duration_s: float = Field(default=600.0, ge=60, le=3600)
    #: 对白总字数低于该值时拒绝分析，避免证据不足
    min_dialogue_chars: int = Field(default=120, ge=1)
    max_segments: int = Field(default=40, ge=1, le=100)
    #: 发给模型的每句对白最多保留多少字符
    dialogue_snippet_chars: int = Field(default=160, ge=20, le=1000)
    analysis_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    analysis_max_tokens: int = Field(default=2500, ge=256, le=16000)
    integration_max_tokens: int = Field(default=4000, ge=512, le=16000)
    #: 整合时每次最多带给模型多少条分段摘要
    integration_max_segments: int = Field(default=40, ge=1, le=100)


class CopyConfig(BaseModel):
    """解说文案生成参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: 中文解说语速约 4 字/秒，240 字/分钟
    chars_per_minute: int = Field(default=240, ge=120, le=400)
    default_minutes: float = Field(default=3.0, gt=0, le=30)
    custom_default_minutes: float = Field(default=5.0, gt=0, le=30)
    temperature: float = Field(default=0.8, ge=0.0, le=2.0)
    #: 输出 token 上限（按时长折算）
    max_tokens_per_minute: int = Field(default=600, ge=200, le=3000)
    max_paragraphs: int = Field(default=40, ge=1, le=100)
    min_paragraph_chars: int = Field(default=20, ge=1, le=500)
    #: 成稿字数相对目标字数的可接受区间
    min_chars_ratio: float = Field(default=0.6, ge=0.3, le=1.0)
    max_chars_ratio: float = Field(default=1.6, ge=1.0, le=3.0)


class TTSConfig(BaseModel):
    """中文配音参数（第三阶段）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: auto：优先本地 Piper（若已配置模型），否则 Edge；edge/piper 强制指定
    engine: Literal["auto", "edge", "piper"] = "auto"
    edge_voice: str = "zh-CN-XiaoxiaoNeural"
    #: 本地 Piper 模型路径；为空表示不启用本地引擎
    piper_model_path: str = ""
    rate_percent: int = Field(default=0, ge=-50, le=100)
    volume_percent: int = Field(default=0, ge=-50, le=50)
    pitch_hz: int = Field(default=0, ge=-50, le=50)
    sentence_pause_ms: int = Field(default=250, ge=0, le=3000)
    paragraph_pause_ms: int = Field(default=450, ge=0, le=3000)
    max_sentence_chars: int = Field(default=80, ge=20, le=300)
    fallback_enabled: bool = True
    cache_enabled: bool = True
    edge_timeout_s: float = Field(default=60.0, gt=0, le=600)
    edge_retries: int = Field(default=2, ge=0, le=5)
    sample_rate: int = Field(default=48000, ge=16000, le=96000)


class MatchConfig(BaseModel):
    """文案-镜头匹配参数。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    #: 低于该置信度标记人工审核
    confidence_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    #: 目标音长之外额外留出的画面余量（秒）
    duration_margin_s: float = Field(default=0.25, ge=0.0, le=5.0)
    #: 每句最多选择的镜头数
    max_shots_per_unit: int = Field(default=6, ge=1, le=20)
    #: 是否允许扩展到片段外相邻镜头补足时长
    allow_extend: bool = True
    #: 每句最大允许复用的镜头数（0 表示不主动复用）
    max_reuse: int = Field(default=1, ge=0, le=5)


class ExportConfig(BaseModel):
    """视频导出参数（第四阶段）。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    width: int = Field(default=1920, ge=320, le=7680)
    height: int = Field(default=1080, ge=240, le=4320)
    fps: int = Field(default=30, ge=12, le=60)
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "medium"
    subtitle_mode: Literal["burn", "soft", "none"] = "burn"
    fit_mode: Literal["pad", "crop"] = "pad"
    bgm_ducking: bool = True
    font_name: str = "Microsoft YaHei"
    font_size: int = Field(default=54, ge=12, le=160)
    margin_v: int = Field(default=64, ge=0, le=500)
    bgm_path: str = ""
    bgm_volume_db: float = Field(default=-20.0, ge=-60.0, le=0.0)
    audio_bitrate: str = "192k"
    output_dir: str = ""


class UIConfig(BaseModel):
    """界面偏好。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    theme: Literal["dark", "light"] = "dark"
    #: 导入完成后是否跳过“是否立即分析”的询问，直接开始分析
    auto_start_analysis: bool = False


class AppConfig(BaseModel):
    """应用总配置。"""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    version: int = 1
    #: 实际数据目录；留空时由加载函数补全
    data_dir: str = ""
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    scene: SceneConfig = Field(default_factory=SceneConfig)
    keyframes: KeyframeConfig = Field(default_factory=KeyframeConfig)
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    story: StoryConfig = Field(default_factory=StoryConfig)
    copywriting: CopyConfig = Field(default_factory=CopyConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    matching: MatchConfig = Field(default_factory=MatchConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    def resolved_data_dir(self) -> Path:
        """返回并创建数据目录。"""
        path = Path(self.data_dir).expanduser() if self.data_dir else default_data_dir()
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def projects_dir(self) -> Path:
        path = self.resolved_data_dir() / "projects"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def logs_dir(self) -> Path:
        path = self.resolved_data_dir() / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def models_dir(self) -> Path:
        path = self.resolved_data_dir() / "models"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def whisper_download_root(self) -> Path:
        if self.whisper.download_root.strip():
            path = Path(self.whisper.download_root).expanduser().resolve()
        else:
            path = self.models_dir() / "whisper"
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_config(path: Path | None = None) -> AppConfig:
    """读取配置；文件不存在或损坏时回退到默认值。"""
    target = path or config_path()
    config = AppConfig()
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            config = AppConfig.model_validate(raw)
        except (OSError, ValueError):
            # 配置损坏不应阻止程序启动，保留默认值并让用户重新保存
            config = AppConfig()
    if not config.data_dir:
        config.data_dir = str(target.parent if path is not None else default_data_dir())
    config.resolved_data_dir()
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """原子写入配置 JSON（不含任何密钥）。"""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    # 数据目录若为空，写入解析后的绝对路径，方便下次启动
    if not payload.get("data_dir"):
        payload["data_dir"] = str(config.resolved_data_dir())
    fd, tmp_name = tempfile.mkstemp(prefix="aimovie-config-", suffix=".json", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return target


def get_deepseek_api_key(config: AppConfig | None = None) -> str:
    """仅从环境变量读取 DeepSeek API Key。"""
    env_name = (config.deepseek.api_key_env if config else DEEPSEEK_API_KEY_ENV) or DEEPSEEK_API_KEY_ENV
    return os.environ.get(env_name, "").strip()


def has_deepseek_api_key(config: AppConfig | None = None) -> bool:
    return bool(get_deepseek_api_key(config))


def resolve_executable(configured: str, default_name: str) -> str:
    """解析 ffmpeg/ffprobe 路径；配置值为空时走 PATH。"""
    candidate = (configured or "").strip().strip('"')
    if candidate:
        expanded = Path(candidate).expanduser()
        if expanded.exists():
            return str(expanded)
        found = shutil.which(candidate)
        if found:
            return found
        raise FileNotFoundError(f"找不到可执行文件：{candidate}")
    found = shutil.which(default_name)
    if found:
        return found
    raise FileNotFoundError(
        f"找不到 {default_name}。请安装 FFmpeg 并在“设置”中填写路径，或将其加入 PATH。"
    )


def ensure_data_dirs(config: AppConfig) -> None:
    """一次性创建所有运行所需目录。"""
    for path in (
        config.resolved_data_dir(),
        config.projects_dir(),
        config.logs_dir(),
        config.models_dir(),
        config.resolved_data_dir() / "cache",
    ):
        path.mkdir(parents=True, exist_ok=True)
