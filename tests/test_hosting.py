"""The hosted site (Azure Container Apps with PostgreSQL): probes, security headers, and the rules that keep a hosted
process from showing local-evaluation data, from losing data in a container, or from failing quietly."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import database, store
from game_librarian.web import CONTENT_SECURITY_POLICY, create_app
from tests.conftest import TEST_DATABASE_URL
from tests.test_web import games  # noqa: F401

LOCAL = ("127.0.0.1", 50000)
needs_postgres = pytest.mark.skipif(not TEST_DATABASE_URL, reason="GAME_LIBRARIAN_TEST_DATABASE_URL is not set")


def test_the_health_probes_answer_and_readiness_checks_the_database(settings, monkeypatch, caplog):
    client = TestClient(create_app(settings, twitch_client=lambda: None))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ok"}

    def unreachable(*args, **kwargs):
        raise OSError("connection refused (a test)")

    monkeypatch.setattr(store, "connect", unreachable)
    with caplog.at_level(logging.WARNING, logger="game_librarian.web"):
        ready = client.get("/readyz")
    assert ready.status_code == 503 and ready.json() == {"status": "unavailable"}  # no reason in the answer
    assert "connection refused (a test)" in caplog.text  # the reason is in the log
    assert client.get("/healthz").status_code == 200  # liveness never depends on the database


def test_every_response_carries_the_security_headers_and_a_hosted_one_hsts(settings):
    headers = TestClient(create_app(settings, twitch_client=lambda: None)).get("/stories").headers
    assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "script-src 'self';" in CONTENT_SECURITY_POLICY and "frame-ancestors 'none'" in CONTENT_SECURITY_POLICY
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in headers
    hosted = TestClient(create_app(replace(settings, hosted=True), twitch_client=lambda: None)).get("/stories")
    assert hosted.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.local_evaluation  # its baseline is this computer's SQLite view, which shows them
def test_a_hosted_site_never_shows_local_evaluation_figures_even_to_a_loopback_request(games, settings):
    """In a container, the authentication sidecar passes requests on from 127.0.0.1, so loopback proves nothing."""
    configured = replace(settings, igdb_client_id="id", igdb_client_secret="secret")
    local = TestClient(create_app(configured, twitch_client=lambda: None), client=LOCAL)
    assert "Most watched among tracked Twitch categories" in local.get("/trending").text  # SQLite, this computer
    hosted = TestClient(create_app(replace(configured, hosted=True), twitch_client=lambda: None), client=LOCAL)
    trending, board = hosted.get("/trending").text, hosted.get("/dashboard").text
    assert "Most watched among tracked Twitch categories" not in trending and "TwitchTracker" not in trending
    assert "Most watched on Twitch" in trending  # Twitch's own order, as every visitor sees it
    assert "Estimated Steam copies sold" not in board and "Gamalytic" not in board


def test_a_hosted_sync_fails_before_starting_when_a_source_lacks_its_settings(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, hosted=True))
    (settings.curated_dir / "games.csv").write_text("steam_appid,title\n570,Dota 2\n", encoding="utf-8")
    assert cli.main(["sync"]) == 1
    err = capsys.readouterr().err
    for name in ("STEAM_API_KEY", "IGDB_CLIENT_ID and IGDB_CLIENT_SECRET", "GAME_LIBRARIAN_CONTACT"):
        assert name in err
    conn = store.initialize(settings.database)
    try:
        assert store.recent_sync_runs(conn) == []  # nothing ran
    finally:
        conn.close()


def test_local_evaluation_commands_refuse_where_their_figures_may_not_be_kept(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, hosted=True))
    for command in ("most-watched", "sales-estimates"):
        assert cli.main([command]) == 1
        assert "a local evaluation" in capsys.readouterr().err


@needs_postgres
def test_on_postgresql_sync_skips_gamalytic_and_twitchtracker_and_waits_for_no_other_refresh(
        settings, fake_steam, fake_gamalytic, fake_twitchtracker, monkeypatch, capsys):
    configured = replace(settings, igdb_client_id="id", igdb_client_secret="secret")
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    fake_steam.add_game(570, "Dota 2")
    (settings.curated_dir / "games.csv").write_text("steam_appid,title\n570,Dota 2\n", encoding="utf-8")
    assert cli.main(["sync", "--skip-upcoming", "--skip-critics"]) == 0
    out = capsys.readouterr().out
    assert "Most watched (TwitchTracker): skipped, a local evaluation" in out
    assert "Sales estimates (Gamalytic): skipped, a local evaluation" in out
    assert fake_gamalytic.requests == [] and fake_twitchtracker.requests == []
    with database.refresh_lock(settings.database) as held:
        assert held
        assert cli.main(["sync", "--skip-upcoming", "--skip-critics"]) == cli.EXIT_BUSY  # another one is running
        assert "another refresh of this database is running" in capsys.readouterr().err
    conn = store.initialize(settings.database)
    try:
        assert [run["status"] for run in store.recent_sync_runs(conn)] == ["success"]  # the busy run wrote nothing
    finally:
        conn.close()


@needs_postgres
def test_init_postgres_creates_the_tables_and_reports_a_role_that_does_not_exist(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["init-postgres"]) == 0
    assert "tables ready (existing tables and rows kept)" in capsys.readouterr().out
    assert cli.main(["init-postgres", "--grant-to", "no_such_role_for_tests"]) == 1
    err = capsys.readouterr().err
    assert "the PostgreSQL database could not be used" in err and "no_such_role_for_tests" in err
    assert cli.main(["init-postgres", "--grant-to", 'bad"name']) == 1
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, database_url=None))
    assert cli.main(["init-postgres"]) == 1
    assert "GAME_LIBRARIAN_DATABASE_URL" in capsys.readouterr().err


def test_status_names_the_database_without_a_password(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["status"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Database: "))
    if settings.database_url:
        assert line.startswith("Database: PostgreSQL ") and "password" not in line
    else:
        assert line == f"Database: {settings.db_path}"
