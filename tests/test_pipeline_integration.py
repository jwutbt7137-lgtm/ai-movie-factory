"""真实（非模拟）端到端分析测试：ffprobe + PySceneDetect + faster-whisper。

默认跳过。运行：

    $env:AIMF_RUN_INTEGRATION=1
    .venv\\Scripts\\python.exe -m pytest tests\\test_pipeline_integration.py -v -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")

from aimovie.config import AppConfig, ensure_data_dirs  # noqa: E402
from aimovie.db import ProjectDB  # noqa: E402
from aimovie.pipeline.phase1 import AnalysisPipeline  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ffmpeg,
    pytest.mark.skipif(
        os.environ.get("AIMF_RUN_INTEGRATION") != "1",
        reason="设置 AIMF_RUN_INTEGRATION=1 才运行真实语音识别",
    ),
]


def test_full_pipeline_with_real_speech(tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> None:
    source = ROOT / "sample_media" / "sample_movie.mp4"
    if not source.exists():
        pytest.skip("缺少 sample_media/sample_movie.mp4，请先运行 scripts/make_sample_video.py")
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.ffmpeg_path = ffmpeg_bin
    config.ffprobe_path = ffprobe_bin
    config.whisper.model_size = "tiny"
    config.whisper.download_root = os.environ.get(
        "AIMF_MODEL_DIR", str(ROOT / "data" / "models" / "whisper")
    )
    config.keyframes.enabled = True
    ensure_data_dirs(config)
    db = ProjectDB(config.resolved_data_dir() / "app.db")
    project = db.create_project("真实端到端测试", source, tmp_path / "project")
    pipeline = AnalysisPipeline(db, config)
    result = pipeline.run(project.id)
    assert result.shot_count >= 3
    assert result.dialogue_count >= 1
    text = "".join(segment.text for segment in db.get_dialogues(project.id))
    assert any("\u4e00" <= character <= "\u9fff" for character in text), text
    assert Path(result.timeline_path).exists()
    assert db.get_project(project.id).status.value == "ready"  # type: ignore[union-attr]
    print(f"端到端识别文本：{text}")
