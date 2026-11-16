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


def test_igdb_is_optional_and_its_secrets_never_appear_in_repr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    for name in ("STEAM_API_KEY", "IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert config.load_settings().igdb_configured is False  # the site and the Steam sync work without IGDB
    (tmp_path / ".env").write_text("STEAM_API_KEY=steam-secret\nIGDB_CLIENT_ID=igdb-id\nIGDB_CLIENT_SECRET=igdb-secret\n",
                                   encoding="utf-8")
    settings = config.load_settings()
    assert settings.igdb_configured and (settings.igdb_client_id, settings.igdb_client_secret) == ("igdb-id", "igdb-secret")
    shown = repr(settings)
    assert "steam-secret" not in shown and "igdb-id" not in shown and "igdb-secret" not in shown
    monkeypatch.setenv("IGDB_CLIENT_SECRET", "")  # an empty value in the environment switches IGDB off
    assert config.load_settings().igdb_configured is False
