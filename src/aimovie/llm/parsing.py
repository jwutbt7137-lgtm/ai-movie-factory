"""模型 JSON 输出解析。"""

from __future__ import annotations

import json
import re

from ..errors import LLMFormatError

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    """从模型输出中提取第一个 JSON 对象。

    常见情况：纯 JSON、```json 代码块、JSON 前后带解释文字。
    不尝试猜测或修复字段内容；无法解析时抛出明确异常。
    """
    if not text or not text.strip():
        raise LLMFormatError("模型返回为空，无法解析 JSON")
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise LLMFormatError(f"模型返回内容不是 JSON 对象（长度 {len(text)}）")
    candidate = cleaned[start : end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LLMFormatError(
            f"模型 JSON 解析失败（长度 {len(text)}，位置 {exc.pos}）：{exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise LLMFormatError("模型返回的 JSON 顶层必须是对象")
    return payload
