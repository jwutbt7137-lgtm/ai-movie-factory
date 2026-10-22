"""配音服务测试：使用假引擎生成本地 WAV，不访问网络。"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from aimovie.config import AppConfig, TTSConfig
from aimovie.db import ProjectDB
from aimovie.errors import TaskCancelled, TTSError
from aimovie.models import Script, ScriptParagraph
from aimovie.pipeline.control import TaskControl
from aimovie.tts.sentences import build_script_sentences, split_sentences
from aimovie.tts.service import TTSService


class FakeEngine:
    cache_suffix = ".wav"

    def __init__(self, name: str, duration: float = 0.5, fail: bool = False):
        self.name = name
        self.voice_label = f"{name}-voice"
        self.duration = duration
        self.fail = fail
        self.calls = 0

    def available(self) -> bool:
        return True

    def synthesize(self, text: str, output_path: Path) -> None:
        self.calls += 1
        if self.fail:
            raise TTSError("simulated failure")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(48000 * self.duration)
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"\x00\x00" * frames)


def _project(db: ProjectDB, tmp_path: Path):
    source = tmp_path / "dummy.mp4"
    source.write_bytes(b"x")
    return db.create_project("配音测试", source, tmp_path / "project")


def _script(project_id: str) -> Script:
    return Script(
        project_id=project_id,
        style_id="quick3",
        style_name="三分钟剧情速看",
        title="配音测试文案",
        target_minutes=3,
        target_chars=720,
        paragraphs=[
            ScriptParagraph(
                index=1,
                text="第一段第一句。第一段第二句！",
                segment_ids=[1],
                shot_ids=[1, 2],
                purpose="hook",
            ),
            ScriptParagraph(
                index=2,
                text="第二段只有一句。",
                segment_ids=[2],
                shot_ids=[3],
                purpose="analysis",
            ),
        ],
    )


def _config(tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> AppConfig:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.ffmpeg_path = ffmpeg_bin
    config.ffprobe_path = ffprobe_bin
    config.tts.engine = "edge"
    config.tts.sentence_pause_ms = 250
    config.tts.paragraph_pause_ms = 450
    return config


@pytest.mark.ffmpeg
def test_sentence_split() -> None:
    assert split_sentences("第一句。第二句！第三句？", 80) == ["第一句。", "第二句！", "第三句？"]
    long = "这是一个很长的句子，" * 20 + "结束。"
    parts = split_sentences(long, max_chars=40)
    assert parts
    assert all(len(part) <= 40 for part in parts)
    script = _script("p")
    sentences = build_script_sentences(script, max_chars=80)
    assert [item.index for item in sentences] == [1, 2, 3]
    assert [item.paragraph_index for item in sentences] == [1, 1, 2]
    assert sentences[0].shot_ids == (1, 2)


@pytest.mark.ffmpeg
def test_synthesize_timeline_and_mix(db: ProjectDB, tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> None:
    project = _project(db, tmp_path)
    script = db.save_script_version(_script(project.id))
    config = _config(tmp_path, ffmpeg_bin, ffprobe_bin)
    edge = FakeEngine("edge", duration=0.5)
    service = TTSService(db, config, engines={"edge": edge}, tts_config=config.tts)
    progress: list[int] = []
    voiceover = service.synthesize(
        project.id, script.id, on_progress=lambda update: progress.append(update.percent)
    )
    assert voiceover.unit_count == 3
    assert voiceover.units[0].start_s == 0
    assert voiceover.units[0].end_s == pytest.approx(0.5, abs=0.01)
    assert voiceover.units[1].start_s == pytest.approx(0.75, abs=0.01)
    assert voiceover.units[2].start_s == pytest.approx(1.7, abs=0.02)
    assert Path(voiceover.audio_path).exists()
    assert voiceover.total_duration_s >= voiceover.units[-1].end_s
    assert progress and progress[-1] == 100
    assert db.get_current_voiceover(project.id).id == voiceover.id  # type: ignore[union-attr]
    assert edge.calls == 3


@pytest.mark.ffmpeg
def test_sentence_cache(db: ProjectDB, tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> None:
    project = _project(db, tmp_path)
    script = db.save_script_version(_script(project.id))
    config = _config(tmp_path, ffmpeg_bin, ffprobe_bin)
    first = TTSService(
        db, config, engines={"edge": FakeEngine("edge")}, tts_config=config.tts
    ).synthesize(project.id, script.id)
    blocker = FakeEngine("edge")
    blocker.synthesize = lambda text, path: (_ for _ in ()).throw(AssertionError("缓存命中时不应合成"))  # type: ignore[method-assign]
    second = TTSService(
        db, config, engines={"edge": blocker}, tts_config=config.tts
    ).synthesize(project.id, script.id)
    assert second.cached is True
    assert second.unit_count == first.unit_count


@pytest.mark.ffmpeg
def test_fallback_engine(db: ProjectDB, tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> None:
    project = _project(db, tmp_path)
    script = db.save_script_version(_script(project.id))
    config = _config(tmp_path, ffmpeg_bin, ffprobe_bin)
    config.tts.fallback_enabled = True
    edge = FakeEngine("edge", fail=True)
    piper = FakeEngine("piper")
    voiceover = TTSService(
        db, config, engines={"edge": edge, "piper": piper}, tts_config=config.tts
    ).synthesize(project.id, script.id)
    assert voiceover.engine == "piper"
    assert all(unit.engine == "piper" for unit in voiceover.units)
    assert any("备用引擎" in warning for warning in voiceover.warnings)


@pytest.mark.ffmpeg
def test_cancel_before_synthesis(db: ProjectDB, tmp_path: Path, ffmpeg_bin: str, ffprobe_bin: str) -> None:
    project = _project(db, tmp_path)
    script = db.save_script_version(_script(project.id))
    config = _config(tmp_path, ffmpeg_bin, ffprobe_bin)
    control = TaskControl()
    control.cancel()
    service = TTSService(
        db, config, engines={"edge": FakeEngine("edge")}, tts_config=config.tts
    )
    with pytest.raises(TaskCancelled):
        service.synthesize(project.id, script.id, control=control)
    assert db.get_current_voiceover(project.id) is None
