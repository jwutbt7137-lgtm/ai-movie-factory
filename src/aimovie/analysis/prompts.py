"""剧情分析的提示词。

提示词要求模型只使用对白证据，并返回严格 JSON。解析后的结果还会经过本地
证据校验：引用不存在的对白编号、缺少证据的断言会被移除并标记人工核实。
"""

from __future__ import annotations

import json

from ..config import StoryConfig
from ..models import StorySegment, format_clock
from .segmentation import StoryChunk

SEGMENT_SYSTEM_PROMPT = """你是一名严谨的中文电影剧情分析助手。
你只能依据用户提供的“对白证据”和镜头时间范围分析剧情，必须遵守：
1. 不得编造对白中没有出现的情节、人物身份、人物关系、动机、结局或台词。
2. 每个事件、冲突、转折都必须填写 evidence_dialogue_ids，且编号必须来自本段对白列表。
3. 如果证据不足，不要把常识推断写成事实，应写入 uncertainties；宁可少写，不可臆造。
4. 人名无法从对白确认时，使用“未具名男性/女性/角色A”等描述，不要猜测真实姓名。
5. 没有画面输入，不要描述画面、服装、表情、镜头运动等视觉信息。
6. 只输出一个合法 JSON 对象，不要 Markdown 代码块、注释或额外解释。
7. 使用简体中文。"""

INTEGRATION_SYSTEM_PROMPT = """你是一名严谨的中文电影剧情结构分析助手。
你将收到多个剧情分段的分析结果，请整合为整片分析，必须遵守：
1. 只能使用分段结果中已有的事实，不得新增人物、事件、结局或对白。
2. 引用人物、转折时尽量保留 evidence_segment_ids / evidence_dialogue_ids。
3. 无法确认的信息写入 unresolved 或 review_notes，不要当作事实。
4. 没有画面输入，不要编造视觉细节。
5. 只输出一个合法 JSON 对象，不要 Markdown 代码块、注释或额外解释。
6. 使用简体中文。"""


def _snippet(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _format_shots(chunk: StoryChunk, limit: int = 80) -> str:
    if not chunk.shots:
        return "（无镜头元数据）"
    shots = list(chunk.shots)
    truncated = len(shots) > limit
    shots = shots[:limit]
    lines = [
        f"#{shot.index} {format_clock(shot.start_s)}–{format_clock(shot.end_s)}"
        for shot in shots
    ]
    if truncated:
        lines.append(f"……其余 {len(chunk.shots) - limit} 个镜头省略")
    return "\n".join(lines)


def build_segment_messages(chunk: StoryChunk, total: int, config: StoryConfig) -> list[dict[str, str]]:
    """构造单段剧情分析消息。"""
    dialogue_lines = [
        (
            f"#{segment.index} [{format_clock(segment.start_s)}–{format_clock(segment.end_s)}] "
            f"{_snippet(segment.text, config.dialogue_snippet_chars)}"
        )
        for segment in chunk.dialogues
    ]
    user = f"""请分析影片第 {chunk.index}/{total} 段。

时间段：{chunk.time_label}
本段镜头（仅时间元数据，无画面）：
{_format_shots(chunk)}

本段对白证据（编号是唯一可引用证据）：
{chr(10).join(dialogue_lines)}

请严格按以下 JSON 结构输出：
{{
  "summary": "本段剧情概述，200 字以内，只基于对白",
  "events": [
    {{"description": "发生了什么", "evidence_dialogue_ids": [1, 2], "importance": "high|medium|low"}}
  ],
  "characters": [
    {{"name": "可确认的人物称谓", "description": "本段中可确认的信息", "evidence_dialogue_ids": [1]}}
  ],
  "conflicts": [
    {{"description": "冲突或矛盾", "evidence_dialogue_ids": [1]}}
  ],
  "turning_points": [
    {{"description": "重要转折", "evidence_dialogue_ids": [1]}}
  ],
  "uncertainties": ["无法从对白确认、需要人工核实的内容"],
  "confidence": 0.0
}}

注意：
- evidence_dialogue_ids 只能使用上面列出的编号；没有证据的事件不要写。
- 如果本段没有足够信息，events 可以为空数组，并在 uncertainties 说明。
- confidence 取 0 到 1，表示你对本段结论的整体把握。"""
    return [
        {"role": "system", "content": SEGMENT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _compact_segment(segment: StorySegment) -> dict:
    return {
        "segment_index": segment.index,
        "time": f"{format_clock(segment.start_s)}–{format_clock(segment.end_s)}",
        "summary": _snippet(segment.summary, 500),
        "events": [
            {
                "description": _snippet(event.description, 240),
                "evidence_dialogue_ids": event.evidence_dialogue_ids,
                "importance": event.importance,
            }
            for event in segment.events[:12]
        ],
        "characters": [
            {
                "name": character.name,
                "description": _snippet(character.description, 160),
                "evidence_dialogue_ids": character.evidence_dialogue_ids,
            }
            for character in segment.characters[:10]
        ],
        "conflicts": [
            {
                "description": _snippet(conflict.description, 200),
                "evidence_dialogue_ids": conflict.evidence_dialogue_ids,
            }
            for conflict in segment.conflicts[:8]
        ],
        "turning_points": [
            {
                "description": _snippet(point.description, 200),
                "evidence_dialogue_ids": point.evidence_dialogue_ids,
            }
            for point in segment.turning_points[:8]
        ],
        "uncertainties": [_snippet(item, 160) for item in segment.uncertainties[:5]],
    }


def build_integration_messages(segments: list[StorySegment]) -> list[dict[str, str]]:
    """构造整片整合消息。"""
    compact = [_compact_segment(segment) for segment in segments]
    payload = json.dumps(compact, ensure_ascii=False, indent=1)
    user = f"""下面是按时间顺序排列的分段剧情分析结果（JSON）：

{payload}

请整合为整片剧情分析，严格按以下 JSON 结构输出：
{{
  "title": "能概括影片内容的中文标题（不要编造片名）",
  "logline": "一句话故事梗概，必须有分段证据支持",
  "overall_summary": "整片剧情概述，600 字以内",
  "main_conflict": "主要冲突",
  "themes": ["主题/议题，属于分析性判断，避免伪装成事实"],
  "characters": [
    {{
      "name": "人物称谓",
      "description": "人物描述",
      "role": "主角/反派/配角等，无法判断则留空",
      "evidence_segment_ids": [1, 2],
      "relations": [
        {{"target": "另一个人物", "relation": "关系描述", "evidence_segment_ids": [1]}}
      ]
    }}
  ],
  "turning_points": [
    {{"description": "重要转折", "segment_index": 1, "evidence_dialogue_ids": [1, 2]}}
  ],
  "unresolved": ["整片中仍无法确认的问题"],
  "review_notes": ["需要人工核实或可能被误读的点"]
}}

注意：
- 不得出现分段结果中没有的人物或事件；不要根据电影常识补全结局。
- segment_index 必须是上面出现过的分段序号。
- 如果人物/关系证据不足，保留空数组或写入 review_notes。"""
    return [
        {"role": "system", "content": INTEGRATION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
