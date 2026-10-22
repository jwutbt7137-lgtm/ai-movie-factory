"""DeepSeek Chat Completions 客户端。

特性：
- 接口地址、模型、超时、重试、单价、预算全部来自配置；
- API Key 只从环境变量读取，不写入日志/配置；
- 相同请求可命中本地 SQLite 缓存，缓存命中不计费；
- 调用前做保守费用预估，预计超过项目预算时抛出 BudgetExceeded，
  必须由上层获得用户确认后以 ``allow_over_budget=True`` 重试；
- 每次成功/失败调用都写入 api_calls 账目（模型、tokens、费用、耗时、错误）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import APP_VERSION, DeepSeekConfig
from ..db import ProjectDB
from ..errors import BudgetExceeded, LLMAuthError, LLMError, LLMResponseError
from ..utils import redact_secrets, utc_now
from .pricing import compute_cost, estimate_messages_tokens

LOGGER = logging.getLogger(__name__)

Opener = Callable[..., Any]
Sleeper = Callable[[float], None]

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class LLMResult:
    """一次文本模型调用的结果。"""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_cny: float
    cached: bool
    duration_s: float
    tokens_estimated: bool = False
    finish_reason: str = ""


def _chat_completions_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.deepseek.com"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/chat/completions"


def _decode_body(body: bytes | str | None) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


class DeepSeekClient:
    """OpenAI 兼容的 Chat Completions 客户端。"""

    def __init__(
        self,
        config: DeepSeekConfig,
        db: ProjectDB,
        project_id: str,
        *,
        api_key: str | None = None,
        allow_over_budget: bool = False,
        opener: Opener | None = None,
        sleeper: Sleeper | None = None,
    ):
        self.config = config
        self.db = db
        self.project_id = project_id
        self.allow_over_budget = allow_over_budget
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get(config.api_key_env or "DEEPSEEK_API_KEY", "").strip()
        )
        self._opener: Opener = opener or urllib.request.urlopen
        self._sleeper: Sleeper = sleeper or time.sleep

    @property
    def url(self) -> str:
        return _chat_completions_url(self.config.base_url)

    # ------------------------------------------------------------------ 公共
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        max_tokens: int,
        temperature: float = 0.3,
    ) -> LLMResult:
        """发起一次对话请求。

        参数:
            messages: OpenAI 格式消息列表（system/user/assistant）。
            purpose: 账目用途，例如 ``story_segment_1`` / ``script_quick3``。
            max_tokens: 输出 token 上限。
            temperature: 采样温度。
        """
        if not purpose.strip():
            raise ValueError("purpose 不能为空")
        if not messages:
            raise ValueError("messages 不能为空")
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        for message in messages:
            if not isinstance(message, dict) or "role" not in message or "content" not in message:
                raise ValueError("messages 必须是包含 role/content 的字典列表")

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        request_hash = self._request_hash(payload, purpose)
        prompt_text = "\n".join(str(message.get("content", "")) for message in messages)

        # 1) 缓存优先：命中缓存不需要 Key，也不会再次计费
        if self.config.cache_enabled:
            cached = self.db.get_llm_cache(request_hash)
            if cached is not None:
                started = utc_now()
                prompt_tokens = int(cached["prompt_tokens"])
                completion_tokens = int(cached["completion_tokens"])
                result = LLMResult(
                    content=str(cached["response"]),
                    model=str(cached["model"]),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    cost_cny=0.0,
                    cached=True,
                    duration_s=0.0,
                )
                self._record_call(
                    purpose=purpose,
                    started_at=started,
                    finished_at=started,
                    duration_ms=0,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_cny=0.0,
                    cached=True,
                    status="success",
                    request_hash=request_hash,
                    model=str(cached["model"]) or self.config.model,
                )
                LOGGER.info(
                    "DeepSeek 缓存命中：%s，tokens=%d+%d",
                    purpose,
                    prompt_tokens,
                    completion_tokens,
                )
                return result

        # 2) Key 检查
        if not self.api_key:
            raise LLMAuthError(
                f"未配置 DeepSeek API Key。请设置环境变量 "
                f"{self.config.api_key_env or 'DEEPSEEK_API_KEY'} 后重试；Key 不会写入配置文件。"
            )

        # 3) 预算检查（保守预估；缓存命中已提前返回）
        estimated_prompt_tokens = estimate_messages_tokens(messages)
        estimated_cost = compute_cost(estimated_prompt_tokens, max_tokens, self.config)
        spent = self.db.cost_summary(self.project_id).spent_cny
        budget = float(self.config.budget_cny_per_project)
        if budget > 0 and not self.allow_over_budget and spent + estimated_cost > budget + 1e-9:
            raise BudgetExceeded(
                (
                    f"本次调用预计 ¥{estimated_cost:.4f}，项目已用 ¥{spent:.4f}，"
                    f"预算上限 ¥{budget:.2f}。已暂停新调用；请提高预算，"
                    "或在确认超支后继续。"
                ),
                estimated_cny=estimated_cost,
                spent_cny=spent,
                budget_cny=budget,
            )

        started_at = utc_now()
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": f"AIMovieFactory/{APP_VERSION}",
            },
            method="POST",
        )

        body = ""
        status_code = 0
        last_error = ""
        attempts = max(1, int(self.config.max_retries) + 1)
        for attempt in range(attempts):
            if attempt:
                delay = max(0.0, float(self.config.retry_backoff_s)) * (2 ** (attempt - 1))
                LOGGER.warning(
                    "DeepSeek 请求失败，%.1f 秒后重试（%d/%d）：%s",
                    delay,
                    attempt,
                    attempts - 1,
                    last_error[:200],
                )
                self._sleeper(delay)
            try:
                with self._opener(request, timeout=float(self.config.timeout_s)) as response:
                    status_code = int(getattr(response, "status", 200) or 200)
                    body = _decode_body(response.read())
                break
            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                try:
                    body = _decode_body(exc.read())
                except Exception:  # noqa: BLE001
                    body = ""
                detail = redact_secrets(body.strip())[:600]
                last_error = f"HTTP {status_code}: {detail or exc.reason}"
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
                if status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                    continue
                self._record_failure(
                    purpose=purpose,
                    started_at=started_at,
                    request_hash=request_hash,
                    error=last_error,
                    status="auth_error" if status_code in (401, 403) else "http_error",
                )
                if status_code in (401, 403):
                    raise LLMAuthError(
                        "DeepSeek 鉴权失败（HTTP "
                        f"{status_code}）。请检查环境变量 {self.config.api_key_env} 是否有效。"
                    ) from exc
                raise LLMError(f"DeepSeek 调用失败：{last_error}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = redact_secrets(str(reason))[:600]
                if attempt < attempts - 1:
                    continue
                self._record_failure(
                    purpose=purpose,
                    started_at=started_at,
                    request_hash=request_hash,
                    error=last_error,
                    status="network_error",
                )
                raise LLMError(f"DeepSeek 网络请求失败：{last_error}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            error = f"HTTP {status_code} 返回非 JSON 内容（长度 {len(body)}）"
            self._record_failure(
                purpose=purpose,
                started_at=started_at,
                request_hash=request_hash,
                error=error,
                status="response_error",
            )
            raise LLMResponseError(error) from exc
        if not isinstance(data, dict):
            raise LLMResponseError("DeepSeek 返回结构不是 JSON 对象")
        if "error" in data and data.get("error"):
            message = redact_secrets(json.dumps(data["error"], ensure_ascii=False))[:600]
            self._record_failure(
                purpose=purpose,
                started_at=started_at,
                request_hash=request_hash,
                error=message,
                status="api_error",
            )
            raise LLMError(f"DeepSeek 返回错误：{message}")

        choices = data.get("choices") or []
        if not choices:
            error = "DeepSeek 响应没有 choices"
            self._record_failure(
                purpose=purpose,
                started_at=started_at,
                request_hash=request_hash,
                error=error,
                status="response_error",
            )
            raise LLMResponseError(error)
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = str(message.get("content") or "").strip()
        if not content:
            error = "DeepSeek 返回内容为空"
            self._record_failure(
                purpose=purpose,
                started_at=started_at,
                request_hash=request_hash,
                error=error,
                status="response_error",
            )
            raise LLMResponseError(error)

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        tokens_estimated = False
        if prompt_tokens <= 0:
            prompt_tokens = estimate_messages_tokens(messages)
            tokens_estimated = True
        if completion_tokens <= 0:
            completion_tokens = max(1, len(content) // 2)
            tokens_estimated = True
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        cost = compute_cost(prompt_tokens, completion_tokens, self.config)
        finished_at = utc_now()
        duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        model = str(data.get("model") or self.config.model)
        finish_reason = str(choice.get("finish_reason") or "")

        if self.config.cache_enabled:
            self.db.save_llm_cache(
                request_hash, content, model, prompt_tokens, completion_tokens
            )
        self._record_call(
            purpose=purpose,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_cny=cost,
            cached=False,
            status="success",
            request_hash=request_hash,
            tokens_estimated=tokens_estimated,
            model=model,
        )
        LOGGER.info(
            "DeepSeek 调用完成：%s | %s | tokens=%d+%d | 费用约 ¥%.4f | %.1fs",
            purpose,
            model,
            prompt_tokens,
            completion_tokens,
            cost,
            duration_ms / 1000,
        )
        return LLMResult(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_cny=cost,
            cached=False,
            duration_s=duration_ms / 1000,
            tokens_estimated=tokens_estimated,
            finish_reason=finish_reason,
        )

    # ------------------------------------------------------------------ 内部
    def _request_hash(self, payload: dict[str, Any], purpose: str) -> str:
        material = json.dumps(
            {
                "base_url": self.url,
                "model": self.config.model,
                "purpose": purpose,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _record_call(
        self,
        *,
        purpose: str,
        started_at: datetime,
        finished_at: datetime | None,
        duration_ms: int | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost_cny: float,
        cached: bool,
        status: str,
        request_hash: str,
        tokens_estimated: bool = False,
        error: str = "",
        model: str | None = None,
    ) -> None:
        try:
            self.db.record_api_call(
                project_id=self.project_id,
                purpose=purpose,
                model=model or self.config.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cost_cny=cost_cny,
                cached=cached,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                request_hash=request_hash,
                tokens_estimated=tokens_estimated,
                error=error,
            )
        except Exception:  # noqa: BLE001 - 账目写入失败不应覆盖原始调用错误
            LOGGER.exception("写入 API 调用记录失败")

    def _record_failure(
        self,
        *,
        purpose: str,
        started_at: datetime,
        request_hash: str,
        error: str,
        status: str,
    ) -> None:
        finished_at = utc_now()
        self._record_call(
            purpose=purpose,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            prompt_tokens=0,
            completion_tokens=0,
            cost_cny=0.0,
            cached=False,
            status=status,
            request_hash=request_hash,
            tokens_estimated=False,
            error=redact_secrets(error)[:2000],
        )
        # 错误只写日志中的安全摘要，不写请求体/Headers
        LOGGER.error("DeepSeek 调用失败（%s）：%s", purpose, redact_secrets(error)[:400])
