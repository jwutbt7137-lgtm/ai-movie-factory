"""把解说文案切成适合逐句合成的句子，并保留段落证据元数据。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Script

_TERMINAL_RE = re.compile(r"(?<=[。！？!?；;…])")
_COMMA_RE = re.compile(r"(?<=[，,、：:])")


@dataclass(frozen=True)
class ScriptSentence:
    """待合成的一句文案。"""

    index: int
    paragraph_index: int
    text: str
    segment_ids: tuple[int, ...]
    evidence_dialogue_ids: tuple[int, ...]
    shot_ids: tuple[int, ...]
    purpose: str = ""
    needs_review: bool = False


def _split_long(part: str, max_chars: int) -> list[str]:
    if len(part) <= max_chars:
        return [part]
    pieces = [piece.strip() for piece in _COMMA_RE.split(part) if piece.strip()]
    result: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + len(piece) <= max_chars:
            buffer += piece
        else:
            result.append(buffer)
            buffer = piece
        while len(buffer) > max_chars:
            result.append(buffer[:max_chars])
            buffer = buffer[max_chars:]
    if buffer:
        result.append(buffer)
    return result or [part[:max_chars]]


def split_sentences(text: str, max_chars: int = 80) -> list[str]:
    """按句末标点切分；过长句按逗号/冒号再切，仍过长则硬切。

    不修改原文字符（仅去除首尾空白），从而保留语气词和标点。
    """
    text = (text or "").strip()
    if not text:
        return []
    raw_parts = [part.strip() for part in _TERMINAL_RE.split(text) if part.strip()]
    result: list[str] = []
    for part in raw_parts:
        result.extend(_split_long(part, max(10, max_chars)))
    return result


def build_script_sentences(script: Script, max_chars: int = 80) -> list[ScriptSentence]:
    """把 Script 的段落展开为逐句列表，并继承剧情/镜头证据。"""
    sentences: list[ScriptSentence] = []
    for paragraph in script.paragraphs:
        parts = split_sentences(paragraph.text, max_chars=max_chars)
        for part in parts:
            sentences.append(
                ScriptSentence(
                    index=len(sentences) + 1,
                    paragraph_index=paragraph.index,
                    text=part,
                    segment_ids=tuple(paragraph.segment_ids),
                    evidence_dialogue_ids=tuple(paragraph.evidence_dialogue_ids),
                    shot_ids=tuple(paragraph.shot_ids),
                    purpose=paragraph.purpose,
                    needs_review=paragraph.needs_review,
                )
            )
    return sentences
