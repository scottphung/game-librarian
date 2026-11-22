"""The hosted site (Azure Container Apps with PostgreSQL): probes, security headers, the same pages and refreshes as
the local site, and the rules that keep a hosted process from losing data in a container or from failing quietly."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import database, store
from game_librarian.twitchtracker import Summary
from game_librarian.web import CONTENT_SECURITY_POLICY, create_app
from tests.conftest import TEST_DATABASE_URL, write_csv
from tests.test_sales_estimates import trending_with_estimates
from tests.test_twitchtracker import REVIEWED, save_tracked
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NOW, TODAY, NoTwitch, configured

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


def test_a_hosted_site_shows_gamalytic_and_twitchtracker_figures_like_any_page(games, settings):
    """Since 0.21 nothing depends on where a request comes from: a hosted process shows the stored estimates and
    TwitchTracker's figures to every visitor, as the local site does."""
    trending_with_estimates(settings)
    write_csv(settings.curated_dir / "twitch_categories.csv",
              [{**REVIEWED, "twitch_category_id": "910", "title": "Strike Series", "decision": "series"}])
    save_tracked(settings, "910", "Strike Series", None, Summary(3, 100_000, 100, None), "2026-11-09T12:00:00+00:00")
    app = create_app(replace(configured(settings), hosted=True), today=lambda: TODAY, now=lambda: NOW,
                     twitch_client=NoTwitch())
    hosted = TestClient(app, base_url="https://testserver", client=("203.0.113.9", 50000))
    board = hosted.get("/dashboard").text
    assert 'id="sales"' in board and "2,500,000" in board and "Gamalytic’s model estimates" in text_of(board)
    trending = text_of(hosted.get("/trending").text)
    assert "Most watched among tracked Twitch categories" in trending
    assert "TwitchTracker category rank #3 · 100,000 average viewers" in trending


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


def test_a_hosted_process_runs_the_estimate_and_most_watched_commands(games, settings, fake_gamalytic, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, hosted=True))
    fake_gamalytic.copies = {101: 5_000}
    assert cli.main(["sales-estimates"]) == 0
    assert "Sales estimates (Gamalytic): success" in capsys.readouterr().out and len(fake_gamalytic.requests) == 1
    assert cli.main(["most-watched"]) == 0  # no Twitch list saved yet: nothing to ask, and no refusal
    assert "Most watched (TwitchTracker): nothing to ask about" in capsys.readouterr().out


@needs_postgres
def test_on_postgresql_sync_refreshes_gamalytic_and_waits_for_no_other_refresh(
        settings, fake_steam, fake_gamalytic, fake_twitchtracker, monkeypatch, capsys):
    configured = replace(settings, igdb_client_id="id", igdb_client_secret="secret")
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    fake_steam.add_game(570, "Dota 2")
    fake_gamalytic.copies = {570: 235_080_385}
    (settings.curated_dir / "games.csv").write_text("steam_appid,title\n570,Dota 2\n", encoding="utf-8")
    assert cli.main(["sync", "--skip-upcoming", "--skip-critics"]) == 0
    out = capsys.readouterr().out
    assert "Most watched (TwitchTracker): nothing to ask about" in out  # no Twitch list without the upcoming step
    assert "Sales estimates (Gamalytic): success" in out and len(fake_gamalytic.requests) == 1
    conn = store.connect(settings.database)
    try:
        assert store.latest_sales_estimates(conn, [570])[570]["copies_sold"] == 235_080_385  # kept in PostgreSQL
    finally:
        conn.close()
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
