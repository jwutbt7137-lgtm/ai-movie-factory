"""配置保存/读取与密钥处理测试。"""

from __future__ import annotations

from pathlib import Path

from aimovie.config import AppConfig, get_deepseek_api_key, has_deepseek_api_key, load_config, save_config


def test_config_roundtrip(tmp_path: Path) -> None:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.whisper.model_size = "base"
    config.whisper.beam_size = 3
    config.scene.threshold = 33.5
    config.scene.backend = "ffmpeg"
    config.deepseek.model = "deepseek-reasoner"
    config.deepseek.timeout_s = 30
    config.deepseek.budget_cny_per_project = 12.5
    config.deepseek.cache_enabled = False
    config.story.chunk_target_chars = 6000
    config.story.analysis_max_tokens = 3000
    config.copywriting.chars_per_minute = 260
    config.copywriting.max_paragraphs = 24
    path = save_config(config, tmp_path / "config.json")
    loaded = load_config(path)
    assert loaded.whisper.model_size == "base"
    assert loaded.whisper.beam_size == 3
    assert loaded.scene.threshold == 33.5
    assert loaded.scene.backend == "ffmpeg"
    assert loaded.deepseek.model == "deepseek-reasoner"
    assert loaded.deepseek.timeout_s == 30
    assert loaded.deepseek.budget_cny_per_project == 12.5
    assert loaded.deepseek.cache_enabled is False
    assert loaded.story.chunk_target_chars == 6000
    assert loaded.story.analysis_max_tokens == 3000
    assert loaded.copywriting.chars_per_minute == 260
    assert loaded.copywriting.max_paragraphs == 24


def test_corrupt_config_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.whisper.model_size == "small"


def test_api_key_only_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = AppConfig(data_dir=str(tmp_path / "data"))
    assert get_deepseek_api_key(config) == ""
    assert has_deepseek_api_key(config) is False

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-1234567890")
    assert get_deepseek_api_key(config) == "sk-test-1234567890"
    assert has_deepseek_api_key(config) is True

    path = save_config(config, tmp_path / "config.json")
    text = path.read_text(encoding="utf-8")
    assert "sk-test-1234567890" not in text


def test_data_directories_created(tmp_path: Path) -> None:
    config = AppConfig(data_dir=str(tmp_path / "data"))
    assert config.projects_dir().exists()
    assert config.logs_dir().exists()
    assert config.models_dir().exists()
