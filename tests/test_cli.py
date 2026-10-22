"""命令行端到端测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimovie.cli import main


@pytest.mark.ffmpeg
def test_info_command(sample_video: Path, tmp_path: Path, capsys) -> None:
    code = main(["--data-dir", str(tmp_path / "data"), "info", str(sample_video)])
    output = capsys.readouterr().out
    assert code == 0
    assert "时长：" in output
    assert "640×360" in output


@pytest.mark.ffmpeg
def test_analyze_without_speech(sample_video: Path, tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "analyze",
            str(sample_video),
            "--name",
            "命令行测试",
            "--skip-speech",
            "--no-keyframes",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "分析完成" in captured.out
    assert '"shot_count"' in captured.out
