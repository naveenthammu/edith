from config import load_config


def test_load_config_reads_env_file(tmp_path, monkeypatch):
    for key in ("BASE_URL", "API_KEY", "MODEL", "MIC_THRESHOLD", "WAKE_WORD"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text("BASE_URL=http://127.0.0.1:31415\nAPI_KEY=k\nMIC_THRESHOLD=0.05\n")

    cfg = load_config(tmp_path)

    assert cfg.base_url == "http://127.0.0.1:31415"
    assert cfg.api_key == "k"
    assert cfg.model == "auto"
    assert cfg.mic_threshold == 0.05
    assert cfg.wake_word == "edith"
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.models_dir.is_dir()


def test_load_config_without_env_file(tmp_path, monkeypatch):
    for key in ("BASE_URL", "API_KEY"):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config(tmp_path)

    assert cfg.base_url is None
    assert cfg.api_key is None
