import pytest

from config import load_config

KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "BASE_URL", "API_KEY", "MODEL", "MIC_THRESHOLD", "WAKE_WORD",
        "EDITH_DATA_DIR", "EDITH_LOCAL_PROVIDER", "EDITH_OLLAMA_URL", "EDITH_OLLAMA_MODEL")


@pytest.fixture(autouse=True)
def isolated_config_environment(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_load_config_reads_env_file(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=k\nBASE_URL=http://127.0.0.1:31415\nAPI_KEY=f\nMIC_THRESHOLD=0.05\n")

    cfg = load_config(tmp_path)

    assert cfg.anthropic_key == "k"
    assert cfg.claude_model == "claude-opus-5-5"
    assert (cfg.base_url, cfg.api_key, cfg.model) == ("http://127.0.0.1:31415", "f", "auto")
    assert cfg.mic_threshold == 0.05
    assert cfg.wake_word == "edith"
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.models_dir.is_dir()


def test_load_config_without_env_file(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(tmp_path)

    assert cfg.anthropic_key is None and cfg.base_url is None and cfg.api_key is None
    assert cfg.local_provider == "llama_cpp"
    assert cfg.ollama_url == "http://127.0.0.1:11434"
    assert cfg.ollama_model == "qwen3:4b-instruct"


def test_ollama_and_absolute_data_directory_can_be_selected(tmp_path, monkeypatch):
    destination = tmp_path / "separate-data"
    monkeypatch.setenv("EDITH_LOCAL_PROVIDER", "ollama")
    monkeypatch.setenv("EDITH_OLLAMA_URL", "http://127.0.0.1:12345")
    monkeypatch.setenv("EDITH_OLLAMA_MODEL", "other-model:local")
    monkeypatch.setenv("EDITH_DATA_DIR", str(destination))
    cfg = load_config(tmp_path)
    assert cfg.local_provider == "ollama"
    assert cfg.ollama_url == "http://127.0.0.1:12345" and cfg.ollama_model == "other-model:local"
    assert cfg.data_dir == destination and cfg.models_dir == destination / "models"
    assert cfg.models_dir.is_dir() and not (tmp_path / "data").exists()


@pytest.mark.parametrize("key,value", [("EDITH_DATA_DIR", "relative/private"), ("EDITH_LOCAL_PROVIDER", "unknown")])
def test_invalid_data_directory_or_provider_fails_before_creating_storage(tmp_path, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        load_config(tmp_path)
    assert not (tmp_path / "data").exists()
