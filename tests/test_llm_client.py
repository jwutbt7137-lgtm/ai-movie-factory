"""DeepSeek 客户端测试：全程使用本地假 opener，不产生真实网络/费用。"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from aimovie.config import DeepSeekConfig
from aimovie.db import ProjectDB
from aimovie.errors import BudgetExceeded, LLMAuthError, LLMError
from aimovie.llm.client import DeepSeekClient
from aimovie.llm.parsing import parse_json_object
from aimovie.models import ProjectStatus
from aimovie.utils import utc_now


class FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _success_body(content: str = '{"ok": true}', prompt: int = 100, completion: int = 50) -> dict:
    return {
        "id": "fake",
        "model": "deepseek-chat",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _make_project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"not a real video")
    project = db.create_project("LLM测试", source, tmp_path / "project")
    db.update_project(project.id, status=ProjectStatus.READY)
    return project


def _config(**overrides) -> DeepSeekConfig:
    config = DeepSeekConfig(
        base_url="https://example.test",
        model="deepseek-chat",
        price_input_cny_per_mtok=2.0,
        price_output_cny_per_mtok=8.0,
        budget_cny_per_project=5.0,
        max_retries=1,
        retry_backoff_s=0.0,
        timeout_s=5.0,
        cache_enabled=True,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_success_records_usage_and_cost(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    requests: list[object] = []

    def opener(request, timeout):  # noqa: ANN001
        requests.append(request)
        return FakeResponse(_success_body())

    client = DeepSeekClient(_config(), db, project.id, api_key="sk-test-key", opener=opener)
    result = client.chat(
        [{"role": "user", "content": "你好"}],
        purpose="test_success",
        max_tokens=100,
    )
    assert result.content == '{"ok": true}'
    assert result.cached is False
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    assert result.cost_cny == pytest.approx(0.0006)
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://example.test/chat/completions"
    assert request.get_header("Authorization") == "Bearer sk-test-key"

    calls = db.recent_api_calls(project.id)
    assert len(calls) == 1
    assert calls[0].status == "success"
    assert calls[0].total_tokens == 150
    summary = db.cost_summary(project.id, budget_cny=5.0)
    assert summary.calls == 1
    assert summary.spent_cny == pytest.approx(0.0006)


def test_cache_hit_avoids_second_request_and_cost(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    calls = {"count": 0}

    def opener(request, timeout):  # noqa: ANN001
        calls["count"] += 1
        return FakeResponse(_success_body())

    first = DeepSeekClient(_config(), db, project.id, api_key="sk-test", opener=opener).chat(
        [{"role": "user", "content": "同一个请求"}],
        purpose="test_cache",
        max_tokens=50,
    )
    assert first.cached is False

    def fail_opener(request, timeout):  # noqa: ANN001
        raise AssertionError("缓存命中时不应发起 HTTP 请求")

    second = DeepSeekClient(_config(), db, project.id, api_key="", opener=fail_opener).chat(
        [{"role": "user", "content": "同一个请求"}],
        purpose="test_cache",
        max_tokens=50,
    )
    assert second.cached is True
    assert second.cost_cny == 0.0
    assert second.content == first.content
    assert calls["count"] == 1
    records = db.recent_api_calls(project.id)
    assert records[0].cached is True
    assert records[0].cost_cny == 0.0


def test_retry_then_success(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    attempts = {"count": 0}

    def opener(request, timeout):  # noqa: ANN001
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib.error.HTTPError(
                "https://example.test/chat/completions",
                500,
                "server error",
                None,
                io.BytesIO(b'{"error":"temporary"}'),
            )
        return FakeResponse(_success_body())

    client = DeepSeekClient(
        _config(max_retries=1),
        db,
        project.id,
        api_key="sk-test",
        opener=opener,
        sleeper=lambda _seconds: None,
    )
    result = client.chat([{"role": "user", "content": "重试"}], purpose="test_retry", max_tokens=50)
    assert result.content
    assert attempts["count"] == 2
    assert len(db.recent_api_calls(project.id)) == 1


def test_auth_error_is_redacted_and_recorded(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    secret = "sk-super-secret-12345678"

    def opener(request, timeout):  # noqa: ANN001
        raise urllib.error.HTTPError(
            "https://example.test/chat/completions",
            401,
            "unauthorized",
            None,
            io.BytesIO(f'{{"error":"bad key {secret}"}}'.encode("utf-8")),
        )

    client = DeepSeekClient(_config(max_retries=0), db, project.id, api_key=secret, opener=opener)
    with pytest.raises(LLMAuthError) as exc_info:
        client.chat([{"role": "user", "content": "鉴权"}], purpose="test_auth", max_tokens=20)
    assert secret not in str(exc_info.value)
    records = db.recent_api_calls(project.id)
    assert records[0].status == "auth_error"
    assert secret not in records[0].error


def test_budget_guard_stops_before_http(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    db.record_api_call(
        project_id=project.id,
        purpose="previous",
        model="deepseek-chat",
        prompt_tokens=10,
        completion_tokens=10,
        total_tokens=20,
        cost_cny=0.0049,
        cached=False,
        status="success",
        started_at=utc_now(),
        finished_at=utc_now(),
    )
    called = {"value": False}

    def opener(request, timeout):  # noqa: ANN001
        called["value"] = True
        return FakeResponse(_success_body())

    client = DeepSeekClient(
        _config(budget_cny_per_project=0.005),
        db,
        project.id,
        api_key="sk-test",
        opener=opener,
    )
    with pytest.raises(BudgetExceeded) as exc_info:
        client.chat([{"role": "user", "content": "预算"}], purpose="test_budget", max_tokens=100)
    assert called["value"] is False
    assert exc_info.value.estimated_cny > 0

    allowed_client = DeepSeekClient(
        _config(budget_cny_per_project=0.005),
        db,
        project.id,
        api_key="sk-test",
        allow_over_budget=True,
        opener=opener,
    )
    result = allowed_client.chat(
        [{"role": "user", "content": "确认超支"}], purpose="test_budget_override", max_tokens=20
    )
    assert called["value"] is True
    assert result.content


def test_missing_key_raises_without_network(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)
    client = DeepSeekClient(
        _config(cache_enabled=False),
        db,
        project.id,
        api_key="",
        opener=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应发起请求")),
    )
    with pytest.raises(LLMAuthError, match="API Key"):
        client.chat([{"role": "user", "content": "无 Key"}], purpose="test_no_key", max_tokens=20)


def test_network_error_recorded(db: ProjectDB, tmp_path: Path) -> None:
    project = _make_project(db, tmp_path)

    def opener(request, timeout):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    client = DeepSeekClient(
        _config(max_retries=0),
        db,
        project.id,
        api_key="sk-test",
        opener=opener,
    )
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "网络"}], purpose="test_network", max_tokens=20)
    records = db.recent_api_calls(project.id)
    assert records[0].status == "network_error"


def test_parse_json_object() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('解释文字 {"a": {"b": 2}} 结束') == {"a": {"b": 2}}
    with pytest.raises(Exception):
        parse_json_object("这不是 JSON")
