"""DeepSeek / OpenAI 兼容的文本模型客户端。"""

from .client import DeepSeekClient, LLMResult
from .parsing import parse_json_object

__all__ = ["DeepSeekClient", "LLMResult", "parse_json_object"]
