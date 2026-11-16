from config import load_config

KEYS = ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "BASE_URL", "API_KEY", "MODEL", "MIC_THRESHOLD", "WAKE_WORD")


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
