"""解说文案提示词。"""

from __future__ import annotations

import json

from ..models import StoryAnalysis, StorySegment, format_clock

SYSTEM_PROMPT = """你是一名中文电影解说文案作者，擅长写出自然、有人味、有观点的原创解说。
你必须遵守：
1. 只能使用提供的“剧情分析”作为事实来源，不得新增剧情、人物、台词、结局或幕后信息。
2. 每个涉及剧情的段落必须填写 segment_ids；能引用对白证据时填写 evidence_dialogue_ids。
3. 分析、评论和观点要明确是观点，不要伪装成剧情事实；证据不足时写入 review_notes。
4. 开头钩子必须基于真实剧情，不能制造虚假悬念或标题党式谎言。
5. 不要复制或模仿任何具体解说创作者的原文；用你自己的表达。
6. 语言自然口语化，避免“首先/其次/最后”“综上所述”等模板化连接词，避免空泛排比。
7. 不要描述画面细节（本流程没有视觉模型输入）。
8. 只输出一个合法 JSON 对象，不要 Markdown 代码块、注释或额外解释。
9. 使用简体中文。"""


def _snippet(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _compact_segment(segment: StorySegment) -> dict:
    return {
        "segment_id": segment.index,
        "time": f"{format_clock(segment.start_s)}–{format_clock(segment.end_s)}",
        "summary": _snippet(segment.summary, 500),
        "events": [
            {
                "description": _snippet(event.description, 220),
                "evidence_dialogue_ids": event.evidence_dialogue_ids,
                "importance": event.importance,
            }
            for event in segment.events[:10]
        ],
        "characters": [
            {
                "name": character.name,
                "description": _snippet(character.description, 140),
            }
            for character in segment.characters[:8]
        ],
        "conflicts": [_snippet(item.description, 180) for item in segment.conflicts[:6]],
        "turning_points": [
            _snippet(item.description, 180) for item in segment.turning_points[:6]
        ],
        "uncertainties": [_snippet(item, 140) for item in segment.uncertainties[:4]],
    }


def build_script_messages(
    analysis: StoryAnalysis,
    *,
    style_name: str,
    instruction: str,
    minutes: float,
    target_chars: int,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, str]]:
    compact = {
        "title": analysis.title,
        "logline": analysis.logline,
        "overall_summary": _snippet(analysis.overall_summary, 1200),
        "main_conflict": _snippet(analysis.main_conflict, 400),
        "themes": analysis.themes[:10],
        "characters": [
            {
                "name": character.name,
                "description": _snippet(character.description, 180),
                "role": character.role,
                "relations": [
                    {"target": relation.target, "relation": relation.relation}
                    for relation in character.relations[:8]
                ],
            }
            for character in analysis.characters[:20]
        ],
        "segments": [_compact_segment(segment) for segment in analysis.segments],
        "unresolved": analysis.unresolved[:10],
        "analysis_review_notes": analysis.review_notes[:10],
    }
    analysis_json = json.dumps(compact, ensure_ascii=False, indent=1)
    user = f"""请根据下面的剧情分析，写一篇中文电影解说文案。

风格：{style_name}
风格要求：{instruction}
目标时长：{minutes:g} 分钟
目标字数：约 {target_chars} 字（可接受 {min_chars}–{max_chars} 字）

剧情分析（唯一事实来源）：
{analysis_json}

严格按以下 JSON 结构输出：
{{
  "title": "解说视频标题（不得虚假承诺或编造剧情）",
  "paragraphs": [
    {{
      "text": "一段连续的口播文案，避免书面语和模板句",
      "segment_ids": [1, 2],
      "evidence_dialogue_ids": [12, 13],
      "purpose": "hook|plot|analysis|character|theme|closing"
    }}
  ],
  "review_notes": ["需要人工核实的事实/版权/平台合规问题"]
}}

写作要求：
- 第一段就是开场钩子，直接进入，不要“大家好/今天我们来聊”。
- 段落数量建议 5–12 段；每段 60–260 字，整体接近目标字数。
- 剧情推进要按因果和冲突组织，不要逐句复述对白。
- 至少 2 段包含你自己的分析或观点，但必须与事实区分。
- 所有 segment_ids 必须来自上面的 segment_id 列表；不要引用不存在的段落。
- 不要编造人物姓名、结局、幕后故事、票房或导演意图。
- 不确定的内容不要写成事实，放入 review_notes。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
