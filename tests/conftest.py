"""pytest 公共夹具：合成测试影片、临时配置与数据库。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Qt 界面测试必须在导入 PySide6 之前设置离屏平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aimovie.config import AppConfig, ensure_data_dirs  # noqa: E402
from aimovie.db import ProjectDB  # noqa: E402
from aimovie.media.probe import probe_media  # noqa: E402


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        pytest.skip(f"未找到 {name}，跳过需要 FFmpeg 的测试")
    return path


@pytest.fixture(scope="session")
def ffmpeg_bin() -> str:
    return _require_tool("ffmpeg")


@pytest.fixture(scope="session")
def ffprobe_bin() -> str:
    return _require_tool("ffprobe")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory: pytest.TempPathFactory, ffmpeg_bin: str) -> Path:
    """生成 3 个不同静态图案镜头、带正弦音轨的 3.6 秒测试影片。"""
    output_dir = tmp_path_factory.mktemp("sample_media")
    output = output_dir / "sample_three_shots.mp4"
    sources = [
        "smptebars=s=640x360:r=24:d=1.2",
        "rgbtestsrc=s=640x360:r=24:d=1.2",
        "color=c=navy:s=640x360:r=24:d=1.2,drawgrid=w=80:h=45:t=2:c=white@0.4",
    ]
    command = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        command += ["-f", "lavfi", "-i", source]
    command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3.6"]
    command += [
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-map",
        "3:a",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        pytest.skip(f"无法生成测试影片：{completed.stderr[-500:]}")
    return output


@pytest.fixture()
def sample_media(sample_video: Path, ffprobe_bin: str):
    return probe_media(sample_video, ffprobe_path=ffprobe_bin)


@pytest.fixture()
def sample_speech_wav(tmp_path: Path) -> Path:
    """提供中文测试语音：优先使用仓库内合成的 narration.wav，其次尝试 SAPI。"""
    bundled = ROOT / "sample_media" / "narration.wav"
    if bundled.exists() and bundled.stat().st_size > 1024:
        target = tmp_path / "speech.wav"
        shutil.copyfile(bundled, target)
        return target
    if os.name != "nt":
        pytest.skip("SAPI 语音合成仅在 Windows 上可用")
    target = tmp_path / "speech.wav"
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$voices = $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Culture.Name };"
        "if (-not ($voices -match '^zh')) { Write-Output 'NO_ZH_VOICE'; exit 3 };"
        "$zh = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1;"
        "$s.SelectVoice($zh.VoiceInfo.Name);"
        f"$s.SetOutputToWaveFile('{target}');"
        "$s.Speak('今天天气很好，我们一起去电影院看电影吧。');"
        "$s.Dispose();"
        "Write-Output 'OK'"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0 or not target.exists():
        pytest.skip(f"系统没有可用的中文 SAPI 语音：{completed.stdout} {completed.stderr}")
    return target


@pytest.fixture()
def app_config(tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> AppConfig:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.ffmpeg_path = ffmpeg_bin
    config.ffprobe_path = ffprobe_bin
    config.whisper.model_size = "tiny"
    config.whisper.download_root = str(tmp_path / "models")
    config.keyframes.max_shots = 20
    ensure_data_dirs(config)
    return config


@pytest.fixture()
def db(tmp_path: Path) -> ProjectDB:
    data_dir = tmp_path / "db_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return ProjectDB(data_dir / "app.db")


@pytest.fixture()
def project(db: ProjectDB, sample_video: Path, tmp_path: Path):
    return db.create_project("测试项目", sample_video, tmp_path / "project")
