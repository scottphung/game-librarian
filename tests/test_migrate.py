"""migrate-to-postgres (migrate.py): a SQLite catalog copied into PostgreSQL, checked, and the rules around it.

These tests need a PostgreSQL server (GAME_LIBRARIAN_TEST_DATABASE_URL; see conftest.py), so they are skipped when
the suite runs on SQLite only. All names and figures here are made up.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from game_librarian import __main__ as cli
from game_librarian import migrate, store
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="GAME_LIBRARIAN_TEST_DATABASE_URL is not set")
NOW = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
RECENT = "2026-11-21T12:00:00+00:00"  # within Twitch's 24 hours of NOW
OLD = "2026-11-19T12:00:00+00:00"  # older: deleted after the copy


def representative_catalog(path) -> None:
    """A small catalog with every kind of value: text with quotes, tabs, new lines, backslashes and other scripts,
    NULLs, floats, JSON, a gap in the run ids, and old and new Twitch rows."""
    store.initialize(path).close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        conn.executemany(
            "INSERT INTO games (appid, curated_title, name, search_name, sort_name, developers, release_date, "
            "details_updated_at, review_total, review_positive) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(570, "Dota 2", "Dota 2", "dota 2", "dota 2", '["Valve"]', "9 Jul, 2013", RECENT, 2000, 1600),
             (101, "Café \"Quote\" O'Neil", "Café \"Quote\" O'Neil", "cafe quote oneil", "cafe quote oneil",
              '["Studio\\\\Tab\\tName"]', None, RECENT, None, None)])
        conn.executemany("INSERT INTO game_tags (appid, rank, tag) VALUES (?, ?, ?)",
                         [(570, 0, "MOBA"), (570, 1, "Free to Play"), (101, 0, "日本語")])
        conn.execute("INSERT INTO review_excerpts (appid, polarity, position, recommendation_id, text, truncated) "
                     "VALUES (570, 'positive', 0, '1', ?, 0)", ("Line one\nLine\ttwo \\ back\\slash 100% ✓",))
        conn.execute("INSERT INTO prices (appid, country_code, status, discount_percent, discount_hidden, checked_at) "
                     "VALUES (570, 'US', 'free', 0, 0, ?)", (RECENT,))
        conn.executemany("INSERT INTO player_snapshots (appid, captured_at, players) VALUES (?, ?, ?)",
                         [(570, RECENT, 612_345), (101, RECENT, None)])
        conn.executemany("INSERT INTO discovered_apps (appid, name, last_modified, first_seen_at, last_seen_at) "
                         "VALUES (?, ?, ?, ?, ?)", [(appid, f"App {appid}", 1_700_000_000 + appid, RECENT, RECENT)
                                                    for appid in range(1, 251)])
        conn.executemany("INSERT INTO sync_runs (id, started_at, finished_at, status, summary) VALUES (?, ?, ?, ?, ?)",
                         [(1, OLD, OLD, "success", '{"counts": {}}'), (2, RECENT, RECENT, "partial", None),
                          (5, RECENT, RECENT, "success", '{"errors": ["x"]}')])
        conn.execute("INSERT INTO sync_state (key, value) VALUES ('discovery_modified_since', '1795288352')")
        conn.execute("INSERT INTO candidates (appid, title, sources, match_method, status, created_at, updated_at) "
                     "VALUES (730, 'Counter-Strike 2', '[{\"source\": \"awards\"}]', 'steam_app', 'eligible', ?, ?)",
                     (RECENT, RECENT))
        conn.execute("INSERT INTO igdb_games (igdb_id, name, first_seen_at, checked_at) VALUES (2963, 'Dota 2', ?, ?)",
                     (RECENT, RECENT))
        conn.execute("INSERT INTO release_changes (igdb_id, platform, before, after, kind, noticed_at) "
                     "VALUES (2963, 'PC', NULL, '2013-07-09', 'new', ?)", (RECENT,))
        conn.execute("INSERT INTO popularity_snapshots (signal, igdb_id, captured_at, position, value) "
                     "VALUES ('want_to_play', 2963, ?, 1, 0.000123456789012345)", (RECENT,))
        conn.execute("INSERT INTO steam_igdb_links (appid, igdb_id, status, checked_at) "
                     "VALUES (570, 2963, 'linked', ?)", (RECENT,))
        conn.executemany("INSERT INTO twitch_live (game_key, status, streams, fetched_at, checked_at) "
                         "VALUES (?, 'ok', '[]', ?, ?)", [("570", RECENT, RECENT), ("101", OLD, OLD)])
        conn.execute("INSERT INTO twitch_top_games (position, category_id, name, checked_at) VALUES (1, '29595', "
                     "'Dota 2', ?)", (OLD,))
        conn.execute("INSERT INTO critic_articles (game_key, status, checked_at) VALUES ('570', 'verified', ?)",
                     (RECENT,))
        conn.execute("INSERT INTO critic_scores (game_key, position, outlet, kind, score, status) "
                     "VALUES ('570', 1, 'Metacritic', 'aggregate', '90/100', 'ok')")
        conn.execute("INSERT INTO sales_estimates (appid, collected_at, source, copies_sold) "
                     "VALUES (570, ?, 'Gamalytic', 1000)", (RECENT,))
        conn.execute("INSERT INTO twitchtracker_summaries (category_id, category_name, igdb_id, match_source, "
                     "status) VALUES ('29595', 'Dota 2', 2963, 'reviewer', 'unchecked')")
    conn.close()


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(conn, table: str) -> list[tuple]:
    columns = ", ".join(store.schema_columns()[table])
    return sorted(tuple(row) for row in conn.execute(f"SELECT {columns} FROM {table}"))


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "local.sqlite3"
    representative_catalog(path)
    return path


def test_every_catalog_row_and_relationship_is_copied_exactly_and_the_sqlite_file_is_left_as_it_was(settings, source):
    before = digest(source)
    report = migrate.migrate(source, settings.database, now=lambda: NOW, log=lambda message: None)
    assert digest(source) == before  # opened read-only
    assert report.ok and {item.table for item in report.copied} == set(migrate.migrated_tables())
    assert report.skipped == {"sales_estimates": 1, "twitchtracker_summaries": 1}
    assert report.purged_twitch_rows == 2  # the old live answer and the old top list
    local = sqlite3.connect(source)
    target = store.initialize(settings.database)
    try:
        for table in migrate.migrated_tables():
            if table not in ("twitch_live", "twitch_top_games"):
                assert rows(target, table) == rows(local, table), table
        assert [row[0] for row in target.execute("SELECT game_key FROM twitch_live")] == ["570"]
        assert target.execute("SELECT COUNT(*) FROM twitch_top_games").fetchone()[0] == 0
        for table in ("sales_estimates", "twitchtracker_summaries", "accounts", "wishlist_items"):
            assert target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
        assert target.execute("SELECT text FROM review_excerpts").fetchone()[0] == \
            "Line one\nLine\ttwo \\ back\\slash 100% ✓"
        assert target.execute("SELECT value FROM popularity_snapshots").fetchone()[0] == 0.000123456789012345
        assert target.execute("SELECT COUNT(*) FROM game_tags t JOIN games g ON g.appid = t.appid").fetchone()[0] == 3
        assert store.start_sync_run(target, now=NOW.isoformat()) == 6  # ids continue after the highest copied one
        assert store.get_state(target, "discovery_modified_since") == "1795288352"  # discovery resumes there
    finally:
        local.close()
        target.close()


def test_a_second_copy_is_refused_unless_replace_which_keeps_accounts_and_wishlists(settings, source):
    migrate.migrate(source, settings.database, now=lambda: NOW, log=lambda message: None)
    conn = store.initialize(settings.database)
    try:
        account = store.account_id(conn, "github", "12345", "octocat", now=NOW.isoformat())
        store.toggle_wishlist_item(conn, account, "570", "Dota 2", [], now=NOW.isoformat(), limit=10)
        store.set_state(conn, "discovery_modified_since", "1800000000")  # the hosted sync moved on
    finally:
        conn.close()
    with pytest.raises(migrate.MigrationError, match="already holds catalog rows.*--replace"):
        migrate.migrate(source, settings.database, now=lambda: NOW, log=lambda message: None)
    conn = store.initialize(settings.database)
    try:
        assert store.get_state(conn, "discovery_modified_since") == "1800000000"  # nothing was written
    finally:
        conn.close()
    report = migrate.migrate(source, settings.database, replace=True, now=lambda: NOW, log=lambda message: None)
    assert report.ok and report.replaced
    conn = store.initialize(settings.database)
    try:
        assert store.get_state(conn, "discovery_modified_since") == "1795288352"  # the SQLite catalog again
        assert [tuple(row)[:2] for row in store.wishlist_items(conn, account)] == [("570", "Dota 2")]  # kept
    finally:
        conn.close()


def test_a_check_writes_nothing_and_reports_what_would_be_copied(settings, source):
    report = migrate.migrate(source, settings.database, check_only=True, log=lambda message: None)
    assert report.checked_only and report.ok
    assert {item.table: item.source_rows for item in report.copied}["discovered_apps"] == 250
    conn = store.initialize(settings.database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM discovered_apps").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_damaged_catalog_is_refused_before_anything_is_written(settings, source):
    damaged = sqlite3.connect(source)
    with damaged:
        damaged.execute("PRAGMA foreign_keys = OFF")
        damaged.execute("INSERT INTO game_tags (appid, rank, tag) VALUES (999, 0, 'Orphan')")  # no game 999
        damaged.execute("UPDATE games SET review_total = 'many' WHERE appid = 570")  # text in an INTEGER column
    damaged.close()
    with pytest.raises(migrate.MigrationError) as error:
        migrate.migrate(source, settings.database, log=lambda message: None)
    message = str(error.value)
    assert "1 row(s) refer to a row that does not exist, in game_tags" in message
    assert "1 value(s) of games.review_total are not INTEGER" in message and "Nothing was written" in message
    conn = store.initialize(settings.database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_command_copies_reports_and_refuses_without_a_postgresql_database(settings, source, monkeypatch,
                                                                              capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["migrate-to-postgres", "--from", str(source), "--check"]) == 0
    output = capsys.readouterr().out
    assert "Check passed" in output and "holds no catalog rows yet" in output and "Nothing was written" in output
    assert cli.main(["migrate-to-postgres", "--from", str(source)]) == 0
    output = capsys.readouterr().out
    assert "every table's row count and checksum match the SQLite file" in output
    assert "Not copied: sales_estimates (1 rows), a local evaluation" in output
    assert "The SQLite file was only read; it is unchanged." in output
    assert cli.main(["migrate-to-postgres", "--from", str(source)]) == 1
    assert "--replace" in capsys.readouterr().err
    sqlite_only = replace(settings, database_url=None)
    monkeypatch.setattr(cli, "load_settings", lambda: sqlite_only)
    assert cli.main(["migrate-to-postgres", "--from", str(source)]) == 1
    assert "GAME_LIBRARIAN_DATABASE_URL" in capsys.readouterr().err
