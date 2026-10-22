"""通过本机 HTTP 服务验证真实请求构造（不访问外网、不产生费用）。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aimovie.config import DeepSeekConfig
from aimovie.db import ProjectDB
from aimovie.llm.client import DeepSeekClient


class _Handler(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.__class__.captured = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(raw.decode("utf-8")),
        }
        response = {
            "model": "deepseek-chat",
            "choices": [
                {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        }
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def test_real_http_request_shape(db: ProjectDB, tmp_path: Path) -> None:
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    project = db.create_project("HTTP测试", source, tmp_path / "project")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        config = DeepSeekConfig(
            base_url=f"http://127.0.0.1:{port}",
            model="deepseek-chat",
            cache_enabled=False,
            max_retries=0,
            timeout_s=5,
            budget_cny_per_project=0,
        )
        client = DeepSeekClient(config, db, project.id, api_key="sk-local-test")
        result = client.chat(
            [{"role": "user", "content": "本地 HTTP 测试"}],
            purpose="http_test",
            max_tokens=100,
            temperature=0.2,
        )
        assert result.content == '{"ok": true}'
        assert _Handler.captured["path"] == "/chat/completions"
        assert _Handler.captured["authorization"] == "Bearer sk-local-test"
        assert _Handler.captured["body"]["model"] == "deepseek-chat"
        assert _Handler.captured["body"]["messages"][0]["content"] == "本地 HTTP 测试"
        assert _Handler.captured["body"]["max_tokens"] == 100
        assert len(db.recent_api_calls(project.id)) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
