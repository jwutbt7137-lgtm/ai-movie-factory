"""业务异常类型。"""

from __future__ import annotations


class AIMovieError(Exception):
    """应用内所有可预期错误的基类。"""


class VideoProbeError(AIMovieError):
    """ffprobe 读取影片信息失败。"""


class AudioExtractionError(AIMovieError):
    """从影片中提取音轨失败。"""


class SceneDetectionError(AIMovieError):
    """镜头切分失败。"""


class TranscriptionError(AIMovieError):
    """语音识别失败。"""


class TaskCancelled(AIMovieError):
    """用户取消了后台任务。"""

    def __init__(self, message: str = "任务已取消"):
        super().__init__(message)


class BudgetExceeded(AIMovieError):
    """预计费用超过项目预算，必须先获得用户确认。"""

    def __init__(
        self,
        message: str,
        *,
        estimated_cny: float = 0.0,
        spent_cny: float = 0.0,
        budget_cny: float = 0.0,
    ):
        super().__init__(message)
        self.estimated_cny = estimated_cny
        self.spent_cny = spent_cny
        self.budget_cny = budget_cny


class LLMError(AIMovieError):
    """DeepSeek / OpenAI 兼容接口调用失败。"""


class LLMAuthError(LLMError):
    """API Key 缺失或鉴权失败。"""


class LLMResponseError(LLMError):
    """接口返回结构不符合预期。"""


class LLMFormatError(LLMError):
    """模型输出不是可解析的 JSON 或不符合约定字段。"""


class StoryAnalysisError(AIMovieError):
    """剧情分析输入不足或结果不可用。"""


class ScriptGenerationError(AIMovieError):
    """解说文案生成失败。"""


class TTSError(AIMovieError):
    """语音合成失败。"""


class ShotMatchingError(AIMovieError):
    """镜头匹配失败。"""


class ExportError(AIMovieError):
    """视频合成/导出失败。"""
