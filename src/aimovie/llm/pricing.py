"""Token 估算与费用计算。

单价来自 :class:`aimovie.config.DeepSeekConfig`，不写死在业务代码里。
预算是“预计费用”而不是账单：调用成功后按接口返回的 usage 记录实际用量。
"""

from __future__ import annotations

import math

from ..config import DeepSeekConfig


def estimate_tokens(text: str) -> int:
    """保守估算 token 数：中文按 1 字≈1 token，其他字符约 4 个≈1 token。"""
    if not text:
        return 1
    cjk = 0
    for character in text:
        if "\u3400" <= character <= "\u9fff":
            cjk += 1
    other = max(0, len(text) - cjk)
    return max(1, cjk + math.ceil(other / 4) + 8)


def estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(estimate_tokens(str(message.get("content", ""))) for message in messages) + 16


def compute_cost(
    prompt_tokens: int,
    completion_tokens: int,
    config: DeepSeekConfig,
) -> float:
    """按配置的每百万 tokens 单价计算费用（CNY）。"""
    cost = (
        max(0, int(prompt_tokens)) / 1_000_000 * float(config.price_input_cny_per_mtok)
        + max(0, int(completion_tokens)) / 1_000_000 * float(config.price_output_cny_per_mtok)
    )
    return round(cost, 6)


def estimate_request_cost(
    prompt_text: str,
    max_output_tokens: int,
    config: DeepSeekConfig,
) -> float:
    """调用前的保守费用预估，用于预算拦截和界面提示。"""
    return compute_cost(
        estimate_tokens(prompt_text),
        max(1, int(max_output_tokens)),
        config,
    )
