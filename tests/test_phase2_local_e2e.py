"""第二阶段本地端到端测试：真实 HTTP + 真实分析器/生成器，不访问外网。

本地 HTTP 服务按顺序返回“分段分析 / 整片整合 / 文案”三个 JSON，
用于验证从 StoryAnalyzer 到 DeepSeekClient 再到 SQLite 的完整链路。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aimovie.analysis.story import StoryAnalyzer
from aimovie.config import AppConfig
from aimovie.copywriting.generator import ScriptGenerator
from aimovie.db import ProjectDB
from aimovie.models import DialogueSegment, MediaInfo, Shot

SEGMENT = {
    "summary": "主角发现线索并继续调查。",
    "events": [
        {
            "description": "主角发现线索",
            "evidence_dialogue_ids": [1, 2],
            "importance": "high",
        }
    ],
    "characters": [
        {"name": "主角", "description": "调查者", "evidence_dialogue_ids": [1]}
    ],
    "conflicts": [{"description": "调查受到阻挠", "evidence_dialogue_ids": [2]}],
    "turning_points": [{"description": "发现关键线索", "evidence_dialogue_ids": [1]}],
    "uncertainties": ["人物身份仍不明确"],
    "confidence": 0.8,
}

INTEGRATION = {
    "title": "调查者",
    "logline": "主角在阻挠中调查一条线索。",
    "overall_summary": "主角发现线索，调查受阻，但仍继续。",
    "main_conflict": "调查与阻挠",
    "themes": ["真相与阻力"],
    "characters": [
        {
            "name": "主角",
            "description": "调查者",
            "role": "主角",
            "evidence_segment_ids": [1],
            "relations": [{"target": "阻挠者", "relation": "对立", "evidence_segment_ids": [1]}],
        }
    ],
    "turning_points": [
        {"description": "发现关键线索", "segment_index": 1, "evidence_dialogue_ids": [1]}
    ],
    "unresolved": ["主角身份未明"],
    "review_notes": ["需要核对人物身份"],
}

SCRIPT = {
    "title": "调查者：线索背后的阻力",
    "paragraphs": [
        {
            "text": "如果一条普通线索突然指向不该存在的人，你还会继续查吗？",
            "segment_ids": [1],
            "evidence_dialogue_ids": [1],
            "purpose": "hook",
        },
        {
            "text": "主角没有退路，只能顺着线索追下去，而阻力也一步步逼近。",
            "segment_ids": [1],
            "evidence_dialogue_ids": [2],
            "purpose": "plot",
        },
    ],
    "review_notes": ["需要确认人物关系"],
}


class _QueueHandler(BaseHTTPRequestHandler):
    responses: list[dict] = []
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.requests.append(payload)
        if not self.__class__.responses:
            body = {"error": "no response queued"}
            code = 500
        else:
            content = json.dumps(self.__class__.responses.pop(0), ensure_ascii=False)
            body = {
                "model": "deepseek-chat",
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
            code = 200
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def _setup_project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"not a real video")
    project = db.create_project("本地E2E", source, tmp_path / "project")
    db.save_media(
        project.id,
        MediaInfo(
            source_path=str(source),
            duration_s=24,
            width=640,
            height=360,
            fps=24,
            video_codec="h264",
            audio_codec="aac",
            audio_channels=2,
            audio_sample_rate=48000,
            has_audio=True,
        ),
    )
    db.replace_shots(
        project.id,
        [Shot(index=1, start_s=0, end_s=12), Shot(index=2, start_s=12, end_s=24)],
    )
    db.replace_dialogues(
        project.id,
        [
            DialogueSegment(
                index=index,
                start_s=(index - 1) * 3,
                end_s=(index - 1) * 3 + 2.5,
                text=f"第{index}句对白，主角正在调查一件重要的事情，气氛紧张。",
                language="zh",
            )
            for index in range(1, 9)
        ],
    )
    return project


def test_story_and_script_over_local_http(
    db: ProjectDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _setup_project(db, tmp_path)
    _QueueHandler.responses = [SEGMENT, INTEGRATION, SCRIPT]
    _QueueHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QueueHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        config = AppConfig(data_dir=str(tmp_path / "data"))
        config.deepseek.base_url = f"http://127.0.0.1:{port}"
        config.deepseek.cache_enabled = False
        config.deepseek.max_retries = 0
        config.deepseek.budget_cny_per_project = 0
        config.story.min_dialogue_chars = 50
        config.story.chunk_target_chars = 500
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-local-e2e")

        story_result = StoryAnalyzer(db, config).run(project.id)
        assert story_result.analysis.segment_count == 1
        assert story_result.analysis.characters[0].name == "主角"
        assert db.get_story_analysis(project.id) is not None

        script_result = ScriptGenerator(db, config).run(project.id, "quick3", minutes=3)
        assert script_result.script.version == 1
        assert script_result.script.paragraphs[0].shot_ids == [1, 2]
        assert script_result.script.paragraphs[0].event_descriptions == ["主角发现线索"]
        assert db.get_current_script(project.id).id == script_result.script.id  # type: ignore[union-attr]

        assert len(_QueueHandler.requests) == 3
        calls = db.recent_api_calls(project.id)
        assert len(calls) == 3
        assert all(call.status == "success" for call in calls)
        assert db.cost_summary(project.id).spent_cny > 0
        # 第一次请求是分段分析，第三次是文案；确保 prompt 带证据/段落结构
        first_user = _QueueHandler.requests[0]["messages"][1]["content"]
        third_user = _QueueHandler.requests[2]["messages"][1]["content"]
        assert "evidence_dialogue_ids" in first_user
        assert "segment_ids" in third_user
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
