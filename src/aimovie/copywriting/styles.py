"""解说风格预设。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NarrationStyle:
    """一种解说风格的配置。"""

    id: str
    name: str
    default_minutes: float
    description: str
    instruction: str
    temperature: float = 0.8


STYLES: tuple[NarrationStyle, ...] = (
    NarrationStyle(
        id="quick3",
        name="三分钟剧情速看",
        default_minutes=3,
        description="快节奏梳理主线，开场有钩子，结尾有观点。",
        instruction=(
            "写三分钟剧情速看：第一段必须有基于真实剧情的钩子，迅速交代主角处境和核心冲突；"
            "中段沿主线推进，不按时间流水账；结尾给出你的判断或余味，不要喊口号。"
        ),
        temperature=0.8,
    ),
    NarrationStyle(
        id="suspense5",
        name="五分钟悬疑解说",
        default_minutes=5,
        description="用问题链制造悬念，逐层揭示，但不虚构反转。",
        instruction=(
            "写五分钟悬疑解说：开场抛出真实存在于剧情中的疑点，用问题链推进；"
            "只提前暗示已有证据支持的线索，不得编造不存在的反转或结局；"
            "结尾可以给出你的推理，但必须明确区分事实与推测。"
        ),
        temperature=0.9,
    ),
    NarrationStyle(
        id="analysis10",
        name="十分钟剧情解析",
        default_minutes=10,
        description="结构、主题、人物弧光与关键转折的深度解析。",
        instruction=(
            "写十分钟深度剧情解析：按剧情结构拆解开端、对抗、转折和收束；"
            "分析主题、人物弧光与导演表达，但不要把分析性观点写成剧情事实；"
            "每个观点尽量对应具体的剧情片段。"
        ),
        temperature=0.7,
    ),
    NarrationStyle(
        id="psychology",
        name="人物心理分析",
        default_minutes=6,
        description="从行为与对白推断人物动机，不做临床诊断。",
        instruction=(
            "写人物心理分析：聚焦主要人物的动机、防御机制、关系变化和选择代价；"
            "只能依据对白与可知行为推断，不得编造童年创伤等未出现的信息；"
            "避免医学诊断式断言，明确哪些是推测。"
        ),
        temperature=0.8,
    ),
    NarrationStyle(
        id="scifi",
        name="科幻电影深度解说",
        default_minutes=8,
        description="世界观、设定、科学概念与主题的深度解说。",
        instruction=(
            "写科幻电影深度解说：解释影片呈现的世界观、关键设定和科学概念，"
            "区分影片设定与真实科学；分析技术/社会议题；不得把设定猜测写成事实，"
            "也不要用百科知识替代剧情证据。"
        ),
        temperature=0.7,
    ),
    NarrationStyle(
        id="custom",
        name="自定义解说风格",
        default_minutes=5,
        description="由用户填写风格要求；仍然受事实与证据约束。",
        instruction="按用户在“自定义要求”中描述的风格写作，但不得违反事实与证据约束。",
        temperature=0.9,
    ),
)


def get_style(style_id: str) -> NarrationStyle:
    for style in STYLES:
        if style.id == style_id:
            return style
    raise KeyError(f"未知解说风格：{style_id}")
