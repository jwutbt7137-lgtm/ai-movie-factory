"""AI 解说文案生成（第二阶段）。"""

from .generator import ScriptGenerator, ScriptRunResult
from .styles import STYLES, NarrationStyle, get_style

__all__ = ["ScriptGenerator", "ScriptRunResult", "STYLES", "NarrationStyle", "get_style"]
