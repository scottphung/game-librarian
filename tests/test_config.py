from game_librarian import config


def test_dotenv_loads_and_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    (tmp_path / ".env").write_text('STEAM_API_KEY="file-key"\n', encoding="utf-8")
    assert config.load_settings().steam_api_key == "file-key"
    monkeypatch.setenv("STEAM_API_KEY", "environment-key")
    assert config.load_settings().steam_api_key == "environment-key"
    monkeypatch.setenv("STEAM_API_KEY", "")
    assert config.load_settings().steam_api_key is None


def test_missing_dotenv_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("STEAM_API_KEY", raising=False)
    assert config.load_settings().steam_api_key is None
