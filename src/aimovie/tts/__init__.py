"""中文配音与句子级时间轴（第三阶段）。"""

from .service import TTSService
from .sentences import ScriptSentence, build_script_sentences, split_sentences

__all__ = ["TTSService", "ScriptSentence", "build_script_sentences", "split_sentences"]
