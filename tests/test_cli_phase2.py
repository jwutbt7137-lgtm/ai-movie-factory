"""第二阶段 CLI 测试（不发起真实 API 调用）。"""

from __future__ import annotations

from pathlib import Path

from aimovie.cli import main
from aimovie.db import ProjectDB


def _project(tmp_path: Path):
    data_dir = tmp_path / "data"
    db = ProjectDB(data_dir / "app.db")
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    project = db.create_project("CLI第二阶段", source, tmp_path / "project")
    return data_dir, db, project


def test_story_without_api_key_returns_error(tmp_path: Path, monkeypatch, capsys) -> None:
    data_dir, _db, project = _project(tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    code = main(["--data-dir", str(data_dir), "story", project.id])
    captured = capsys.readouterr()
    assert code == 2
    assert "API Key" in captured.err


def test_script_without_analysis_returns_error(tmp_path: Path, monkeypatch, capsys) -> None:
    data_dir, _db, project = _project(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-only-for-cli-check")
    code = main(["--data-dir", str(data_dir), "script", project.id])
    captured = capsys.readouterr()
    assert code == 1
    assert "剧情分析" in captured.err


def test_scripts_and_costs_commands(tmp_path: Path, capsys) -> None:
    data_dir, db, project = _project(tmp_path)
    code = main(["--data-dir", str(data_dir), "scripts", project.id])
    assert code == 0
    assert "暂无文案" in capsys.readouterr().out
    code = main(["--data-dir", str(data_dir), "costs", project.id])
    output = capsys.readouterr().out
    assert code == 0
    assert "已用" in output
    assert "调用：0" in output
