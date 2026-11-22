"""Most watched by TwitchTracker's average viewers and rank (versions 0.18 and 0.19), for local evaluation only.

The fake answers are shaped like the live ones of 2026-11-21 (twitchtracker.py): four whole numbers, or {} for a
category TwitchTracker has no summary of. conftest's fake_twitchtracker stands in for the API in every test, so no test
reaches it. The web tests use the fourteen synced catalog games of test_web.py (app ids 101-114). All names, ids and
numbers are made up, apart from the shipped reviewer file's Dota 2 and Counter-Strike rows.
"""

import re
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import store, twitch, twitchtracker, upcoming
from game_librarian.config import DEFAULT_CURATED_DIR, USER_AGENT
from game_librarian.curated import TwitchCategoryDecision, load_curated
from game_librarian.twitchtracker import InvalidSummary, Summary, TwitchTrackerClient, TwitchTrackerError, parse_summary
from game_librarian.upcoming import SteamMatch
from game_librarian.web import create_app
from tests.conftest import write_csv
from tests.test_dashboard_trending import chart, dashboard_client, methodology
from tests.test_list_comparison import save_captures
from tests.test_sales_estimates import board, sales_section, trending_with_estimates
from tests.test_trending_cross_platform import CATALOG, RELEASE_DAY, at, part
from tests.test_twitch import FakeHelix
from tests.test_upcoming import CLIENT_ID, CLIENT_SECRET, FakeIgdbApi, entry, igdb_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NOW, NoTwitch, configured

pytestmark = pytest.mark.local_evaluation  # TwitchTracker's figures live in the local SQLite database only

LOCAL = ("127.0.0.1", 50000)
REVIEWED = {"twitch_category_id": "1005", "title": "Shooter", "decision": "no_match", "igdb_id": "",
            "checked_on": "2026-11-21", "checked_by": "a reviewer", "note": "Covers two live games"}
# Reviewed rows like the shipped Counter-Strike (series) and Dota 2 (match) rows, and one outside Twitch's list.
SERIES = {**REVIEWED, "twitch_category_id": "1007", "title": "Strike Series", "decision": "series",
          "note": "Its streams can be either of two live games of the series"}
DOTA = {**REVIEWED, "twitch_category_id": "1008", "title": "Dota Two", "decision": "match", "igdb_id": "210",
        "note": "Twitch gives the category no igdb_id"}
OUTSIDE = {**REVIEWED, "twitch_category_id": "2001", "title": "Old Favourite", "decision": "match", "igdb_id": "211",
           "note": "Often outside Twitch's top list"}


def summary(rank: int, viewers: int, channels: int = 100) -> dict:
    """An answer of TwitchTracker's category summary, with hours watched about 168 times the average viewers."""
    return {"avg_viewers": viewers, "avg_channels": channels, "rank": rank, "hours_watched": viewers * 168}


@pytest.fixture
def world(games, settings, fake_twitchtracker):
    """IGDB, Twitch and TwitchTracker on 19 Nov 2026:

    * Launch Day (1001): Twitch's igdb_id and IGDB's own record of the category agree (201); TwitchTracker rank 1.
    * Just Chatting (509658): no game anywhere; rank 2 on TwitchTracker, but never asked about.
    * Game 01 (1002): Twitch's igdb_id 206 only, which is catalog app 101 by IGDB's Steam record; rank 4, with fewer
      average viewers than rank 6, as TwitchTracker's live answers had on 2026-11-21 (its rank is kept as returned).
    * Arena Two (1003): no igdb_id from Twitch, but IGDB's record of game 208 lists the category, as for Dota 2; rank 6.
    * Battle Mix (1004): Twitch says 204, IGDB's record says 205, as for Call of Duty: Warzone: unresolved.
    * Shooter (1005): a reviewer decided it stands for no game (curated/twitch_categories.csv).
    * Quiet Garden (1006): TwitchTracker has no summary of it ({}).
    """
    api = FakeIgdbApi()
    api.games = {201: igdb_game(201, "Launch Day", entry("PlayStation 5", day=RELEASE_DAY),
                                platforms=("PlayStation 5",)),
                 204: igdb_game(204, "Old Classic", entry("PC", day=date(2015, 5, 19))),
                 205: igdb_game(205, "Mystery Game", platforms=()),
                 206: igdb_game(206, "Game 01", entry("PC", day=date(2024, 1, 1)), steam=[101, 999]),
                 208: igdb_game(208, "Arena Two", entry("PC", day=date(2013, 7, 9))),
                 209: igdb_game(209, "Quiet Garden", entry("PC", day=date(2020, 2, 2)))}
    api.externals = [{"game": 206, "uid": "101"},  # IGDB's Steam record of catalog app 101
                     {"game": 201, "uid": "1001", "external_game_source": 14},
                     {"game": 208, "uid": "1003", "external_game_source": 14},
                     {"game": 205, "uid": "1004", "external_game_source": 14},
                     {"game": 209, "uid": "1006", "external_game_source": 14}]
    helix = FakeHelix()
    helix.top = [{"id": "1001", "name": "Launch Day", "igdb_id": "201"},
                 {"id": "509658", "name": "Just Chatting", "igdb_id": ""},
                 {"id": "1002", "name": "Game 01", "igdb_id": "206"},
                 {"id": "1003", "name": "Arena Two", "igdb_id": ""},
                 {"id": "1004", "name": "Battle Mix", "igdb_id": "204"},
                 {"id": "1005", "name": "Shooter", "igdb_id": ""},
                 {"id": "1006", "name": "Quiet Garden", "igdb_id": "209"}]
    fake_twitchtracker.summaries = {"1001": summary(1, 642_962), "509658": summary(2, 268_768),
                                    "1002": summary(4, 40_000), "1003": summary(6, 48_252),
                                    "1004": summary(5, 60_000), "1005": summary(8, 57_576)}
    write_csv(settings.curated_dir / "twitch_categories.csv", [REVIEWED])
    return api, helix


@pytest.fixture
def reviewed(world, settings, fake_twitchtracker):
    """The world plus three reviewed categories, as curated/twitch_categories.csv holds Counter-Strike and Dota 2:

    * Strike Series (1007): a series category, which IGDB's own record links to one of its games (Strike Two, 207,
      catalog app 102), as IGDB links Counter-Strike to CS:GO alone; TwitchTracker rank 3.
    * Dota Two (1008): Twitch gives no igdb_id and IGDB's record does not list the category (as in a Twitch list saved
      before 0.18): only the reviewed match names its game (210); rank 7.
    * Old Favourite (2001): reviewed (211), but not in Twitch's top list at this refresh; rank 9.
    """
    api, helix = world
    api.games |= {207: igdb_game(207, "Strike Two", entry("PC", day=date(2012, 8, 21)), steam=[102]),
                  210: igdb_game(210, "Dota Two", entry("PC", day=date(2013, 7, 9))),
                  211: igdb_game(211, "Old Favourite", entry("PC", day=date(2016, 3, 1)))}
    api.externals += [{"game": 207, "uid": "102"},  # IGDB's Steam record: catalog app 102 is Strike Two
                      {"game": 207, "uid": "1007", "external_game_source": 14}]
    helix.top += [{"id": "1007", "name": "Strike Series", "igdb_id": ""},
                  {"id": "1008", "name": "Dota Two", "igdb_id": ""}]
    fake_twitchtracker.summaries |= {"1007": summary(3, 100_000), "1008": summary(7, 30_000),
                                     "2001": summary(9, 20_000)}
    write_csv(settings.curated_dir / "twitch_categories.csv", [REVIEWED, SERIES, DOTA, OUTSIDE])
    return world


def refresh_lists(settings, world, fake_steam, moment):
    """The `upcoming` refresh at `moment`: IGDB, then Twitch's top list and IGDB's records of its categories."""
    api, helix = world
    conn = store.initialize(settings.database)
    try:
        with api.client() as client:
            return upcoming.refresh_upcoming(conn, client, fake_steam.client(), load_curated(settings.curated_dir),
                                             catalog_appids=CATALOG, twitch_client=helix.client(),
                                             log=lambda message: None, now=lambda: moment)
    finally:
        conn.close()


def collect(settings, fake, moment, *, force=False) -> twitchtracker.RefreshReport:
    """What `most-watched` does at `moment`: choose the categories, then ask TwitchTracker about the due ones."""
    conn = store.initialize(settings.database)
    try:
        twitchtracker.update_selection(conn, load_curated(settings.curated_dir).twitch_category_decisions, moment)
        due = twitchtracker.due_categories(conn, moment, force=force)
        with fake.client() as client:
            return twitchtracker.refresh_summaries(conn, client, due, log=lambda message: None, now=lambda: moment)
    finally:
        conn.close()


def rows(settings) -> dict:
    conn = store.connect(settings.database)
    try:
        return {row["category_id"]: dict(row) for row in store.tracker_rows(conn)}
    finally:
        conn.close()


def due(settings, moment, *, force=False) -> list[str]:
    conn = store.connect(settings.database)
    try:
        return [row["category_id"] for row in twitchtracker.due_categories(conn, moment, force=force)]
    finally:
        conn.close()


def asked(fake) -> list[str]:
    return [request.url.path.rsplit("/", 1)[1] for request in fake.requests]


def trending(settings, moment, *, client=LOCAL, headers=None) -> str:
    app = create_app(configured(settings), today=lambda: moment.date(), now=lambda: moment, twitch_client=NoTwitch())
    return TestClient(app, client=client).get("/trending", headers=headers or {}).text


def watched_part(html: str) -> str:
    return part(html, "twitch-title", 'id="steam"')


def listed(html: str) -> str:
    """The Most watched list itself (its <ol>), without the list's definition."""
    return re.search(r'<ol class="trend-list upcoming-list" aria-label="Most watched[^"]*">.*?</ol>', html,
                     re.S).group(0)


def places(html: str) -> list[str]:
    return re.findall(r'<span class="trend-pos" aria-hidden="true">(\d+)</span>', html)


def ranks(html: str) -> list[str]:
    return re.findall(r"TwitchTracker category rank #(\d+)", html)


# ---------------------------------------------------------------------- the answer and the client


def test_an_answer_is_four_whole_numbers_and_an_empty_object_means_no_summary():
    assert parse_summary({"avg_viewers": 116855, "avg_channels": 3490, "rank": 3, "hours_watched": 19651138}) == \
        Summary(3, 116855, 3490, 19651138)
    assert parse_summary({}) is None  # an unknown id, or a real category TwitchTracker has no summary of
    assert parse_summary({"avg_viewers": 0, "rank": 22485}) == Summary(22485, 0, None, None)  # optional fields missing
    assert parse_summary({"avg_viewers": 5, "rank": 7, "avg_channels": -1, "hours_watched": 2.5}) == \
        Summary(7, 5, None, None)  # a malformed optional field is no value, never a zero
    for broken in ({"rank": 3}, {"avg_viewers": 10}, {"rank": 0, "avg_viewers": 10}, {"rank": True, "avg_viewers": 10},
                   {"rank": "3", "avg_viewers": 10}, {"rank": 3.0, "avg_viewers": 10}, {"rank": 3, "avg_viewers": -4}):
        with pytest.raises(InvalidSummary, match="no valid"):
            parse_summary(broken)
    for not_an_object in ([], "text", 42, None):
        with pytest.raises(InvalidSummary, match="not a JSON object"):
            parse_summary(not_an_object)


def test_the_client_asks_by_category_id_without_a_key_and_waits_between_requests(fake_twitchtracker):
    fake_twitchtracker.summaries = {"18122": {"avg_viewers": 116855, "avg_channels": 3490, "rank": 3,
                                              "hours_watched": 19651138}}
    waits = []
    client = TwitchTrackerClient(http=httpx.Client(transport=httpx.MockTransport(fake_twitchtracker.handler)),
                                 sleep=waits.append, clock=iter([0.0, 0.5, 0.5]).__next__)
    with client:
        assert client.summary("18122") == Summary(3, 116855, 3490, 19651138)
        assert client.summary("506437") is None  # a real category without a summary answers {}
        with pytest.raises(ValueError):
            client.summary("World of Warcraft")  # names are never sent: a name can match another category
    assert waits == [1.0]  # MIN_INTERVAL_SECONDS (1.5) between two requests
    first = fake_twitchtracker.requests[0]
    assert str(first.url) == "https://twitchtracker.com/api/games/summary/18122"
    assert first.headers["User-Agent"] == USER_AGENT and len(fake_twitchtracker.requests) == 2
    assert not {"authorization", "client-id", "api-key"} & {name.lower() for name in first.headers}


def test_server_errors_are_retried_but_a_rate_limit_a_refusal_or_a_page_is_not(fake_twitchtracker):
    fake_twitchtracker.summaries = {"1": summary(9, 50)}
    fake_twitchtracker.responses = [503, "timeout"]  # then a normal answer
    with fake_twitchtracker.client() as client:
        assert client.summary("1").rank == 9
        assert client.requests_sent == 3
    for planned, message in ((429, "answered HTTP 429 (too many requests)"),
                             (403, "refused the request (HTTP 403); it is not retried or worked around"),
                             ("html", "answered with something other than JSON"), (404, "answered HTTP 404")):
        fake_twitchtracker.requests.clear()
        fake_twitchtracker.responses = [planned]
        with fake_twitchtracker.client() as client, pytest.raises(TwitchTrackerError, match=re.escape(message)):
            client.summary("1")
        assert len(fake_twitchtracker.requests) == 1  # never tried again
    fake_twitchtracker.requests.clear()
    fake_twitchtracker.responses = ["timeout"] * twitchtracker.MAX_ATTEMPTS
    with fake_twitchtracker.client() as client, pytest.raises(TwitchTrackerError, match=re.escape(
            "could not be reached after 3 attempts (ReadTimeout)")):
        client.summary("1")
    assert len(fake_twitchtracker.requests) == twitchtracker.MAX_ATTEMPTS


# ---------------------------------------------------------------------- what a category stands for


def test_a_category_stands_for_a_game_only_when_its_identifiers_agree():
    resolve = twitch.resolve_category
    assert resolve(201, [201]) == twitch.CategoryGame("matched", 201, "twitch+igdb")
    assert resolve(None, [2963]) == twitch.CategoryGame("matched", 2963, "igdb")  # Dota 2: no igdb_id on Twitch
    assert resolve(206, []) == twitch.CategoryGame("matched", 206, "twitch")
    assert resolve(131800, [119177]).status == "unresolved"  # Call of Duty: Warzone on 2026-11-21
    assert resolve(None, [7, 8]).status == "unresolved"  # two IGDB games list the category: none is guessed
    assert resolve(None, []) == twitch.CategoryGame("no_game")  # Just Chatting
    no_match = TwitchCategoryDecision("32399", "no_match", None, "2026-11-21", "a reviewer")
    assert resolve(None, [1372], no_match) == twitch.CategoryGame("unresolved", source="reviewer")
    series = TwitchCategoryDecision("32399", "series", None, "2026-11-21", "a reviewer", title="Counter-Strike")
    assert resolve(None, [1372], series) == twitch.CategoryGame("series", source="reviewer")  # never CS:GO alone
    match = TwitchCategoryDecision("29595", "match", 2963, "2026-11-21", "a reviewer", title="Dota 2")
    assert resolve(None, [], match) == twitch.CategoryGame("matched", 2963, "reviewer")  # no link anywhere but this
    assert resolve(131800, [119177], match).igdb_id == 2963  # a reviewer settles a disagreement


def test_the_refresh_saves_igdbs_own_record_of_each_category_and_reads_its_game(world, settings, fake_steam):
    assert refresh_lists(settings, world, fake_steam, at(RELEASE_DAY)).status() == "success"
    conn = store.connect(settings.database)
    try:
        top = store.twitch_top_games(conn, "")
        assert {row["category_id"]: row["igdb_links"] for row in top}["1003"] == "[208]"  # IGDB's record, no igdb_id
        assert 208 in store.igdb_game_rows(conn)  # read although Twitch names no game for the category
        games, left_out = upcoming.twitch_games(conn, top, load_curated(settings.curated_dir).twitch_category_decisions)
    finally:
        conn.close()
    assert [(row["category_id"], record["igdb_id"], match.source) for row, record, match in games] == [
        ("1001", 201, "twitch+igdb"), ("1002", 206, "twitch"), ("1003", 208, "igdb"), ("1006", 209, "twitch+igdb")]
    assert left_out == {"no_game": 1, "unresolved": 2}  # Just Chatting; Battle Mix's disagreement and the reviewer's


def test_the_refresh_reads_every_reviewed_game_and_keeps_a_series_as_a_series(reviewed, settings, fake_steam):
    refresh_lists(settings, reviewed, fake_steam, at(RELEASE_DAY))
    conn = store.connect(settings.database)
    try:
        assert {210, 211} <= set(store.igdb_game_rows(conn))  # Dota Two (no link but the reviewed row), Old Favourite
        games, left_out = upcoming.twitch_games(conn, store.twitch_top_games(conn, ""),
                                                load_curated(settings.curated_dir).twitch_category_decisions)
    finally:
        conn.close()
    assert [(row["category_id"], record["igdb_id"] if record else None, match.status, match.source)
            for row, record, match in games] == [
        ("1001", 201, "matched", "twitch+igdb"), ("1002", 206, "matched", "twitch"), ("1003", 208, "matched", "igdb"),
        ("1006", 209, "matched", "twitch+igdb"), ("1007", None, "series", "reviewer"),
        ("1008", 210, "matched", "reviewer")]
    assert left_out == {"no_game": 1, "unresolved": 2}  # Just Chatting stays out


def test_an_older_database_gets_the_new_column_and_table_at_start(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE twitch_top_games (position INTEGER PRIMARY KEY, category_id TEXT NOT NULL, "
                "name TEXT NOT NULL, igdb_id INTEGER, checked_at TEXT NOT NULL)")  # version 0.17's table
    old.execute("INSERT INTO twitch_top_games VALUES (1, '1001', 'Launch Day', 201, '2026-11-19T12:00:00+00:00')")
    old.commit()
    old.close()
    conn = store.initialize(path)
    try:
        [row] = store.twitch_top_games(conn, "")
        assert row["igdb_links"] is None and store.tracker_rows(conn) == []
        games, left_out = upcoming.twitch_games(conn, [row], {})
        assert games == [] and left_out == {"not_read": 1}  # Twitch's link alone still names the game
    finally:
        conn.close()


def test_a_version_0_18_table_is_rebuilt_with_its_figures_so_a_series_fits(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE twitchtracker_summaries (category_id TEXT PRIMARY KEY, category_name TEXT NOT NULL, "
                "igdb_id INTEGER NOT NULL, match_source TEXT NOT NULL, selected_at TEXT NOT NULL, rank INTEGER, "
                "avg_viewers INTEGER, avg_channels INTEGER, hours_watched INTEGER, retrieved_at TEXT, "
                "status TEXT NOT NULL, checked_at TEXT, error TEXT)")  # version 0.18's table
    old.execute("INSERT INTO twitchtracker_summaries VALUES ('29595', 'Dota 2', 2963, 'reviewer', "
                "'2026-11-21T13:43:52+00:00', 3, 116855, 3490, 19651138, '2026-11-21T17:03:46+00:00', 'ok', "
                "'2026-11-21T17:03:46+00:00', NULL)")
    old.commit()
    old.close()
    conn = store.initialize(path)
    try:
        required = {row["name"] for row in conn.execute("PRAGMA table_info(twitchtracker_summaries)") if row["notnull"]}
        assert required == {"category_name", "match_source", "status"}
        [row] = store.tracker_rows(conn)
        assert (row["category_name"], row["igdb_id"], row["rank"], row["avg_viewers"], row["retrieved_at"]) == (
            "Dota 2", 2963, 3, 116855, "2026-11-21T17:03:46+00:00")  # the figures survive the rebuild
        store.replace_tracker_selection(conn, [
            twitchtracker.Category("29595", "Dota 2", 2963, "reviewer", "2026-11-21T13:43:52+00:00"),
            twitchtracker.Category("32399", "Counter-Strike", None, "reviewer", None)])  # a series, outside the list
        assert {row["category_id"]: (row["igdb_id"], row["rank"]) for row in store.tracker_rows(conn)} == {
            "29595": (2963, 3), "32399": (None, None)}
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%0_18%'").fetchall() == []
    finally:
        conn.close()
    store.initialize(path).close()  # running it again changes nothing


# ---------------------------------------------------------------------- collection, caching and limits


def test_only_the_chosen_game_categories_are_asked_and_each_answer_is_cached(world, settings, fake_steam,
                                                                             fake_twitchtracker):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    report = collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    assert sorted(asked(fake_twitchtracker)) == ["1001", "1002", "1003", "1006"]  # never Just Chatting or the others
    assert report.status() == "success"
    assert report.counts == {"answered": 4, "summaries": 3, "no_summary": 1, "requests": 4}
    saved = rows(settings)
    assert (saved["1003"]["rank"], saved["1003"]["avg_viewers"], saved["1003"]["avg_channels"],
            saved["1003"]["hours_watched"]) == (6, 48_252, 100, 48_252 * 168)
    assert (saved["1003"]["igdb_id"], saved["1003"]["match_source"], saved["1003"]["category_name"]) == (
        208, "igdb", "Arena Two")
    assert saved["1006"]["status"] == "no_summary" and saved["1006"]["rank"] is None  # {}: no figures, never a zero
    assert saved["1001"]["retrieved_at"] == "2026-11-19T12:00:00+00:00"
    conn = store.connect(settings.database)
    try:
        run = store.recent_source_runs(conn, twitchtracker.SOURCE, limit=1)[0]
    finally:
        conn.close()
    assert run["status"] == "success" and '"requests": 4' in run["summary"]
    # The cache: a category is asked again only 20 hours after its answer, or with --force.
    assert due(settings, at(RELEASE_DAY, 12) + timedelta(hours=19, minutes=59)) == []
    assert due(settings, at(RELEASE_DAY, 12) + timedelta(hours=20)) == ["1001", "1002", "1003", "1006"]
    assert len(due(settings, at(RELEASE_DAY, 13), force=True)) == 4


def test_a_new_twitch_list_replaces_the_choice_and_without_one_the_last_choice_is_asked_again(
        world, settings, fake_steam, fake_twitchtracker):
    api, helix = world
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    next_day = RELEASE_DAY + timedelta(days=1)
    helix.top = [entry_ for entry_ in helix.top if entry_["id"] != "1006"]  # Quiet Garden left Twitch's top list
    refresh_lists(settings, world, fake_steam, at(next_day, 9))  # 21 hours later
    collect(settings, fake_twitchtracker, at(next_day, 9))
    assert sorted(rows(settings)) == ["1001", "1002", "1003"]  # dropped with its figures, the others kept
    assert rows(settings)["1001"]["selected_at"] == "2026-11-20T09:00:00+00:00"
    fake_twitchtracker.requests.clear()
    three_days_later = at(next_day + timedelta(days=2), 9)  # Twitch's list is older than 24 hours: not used
    report = collect(settings, fake_twitchtracker, three_days_later)
    assert sorted(asked(fake_twitchtracker)) == ["1001", "1002", "1003"] and report.status() == "success"
    assert rows(settings)["1001"]["retrieved_at"] == "2026-11-22T09:00:00+00:00"


def test_reviewed_categories_are_tracked_in_twitchs_list_or_not_and_each_is_asked_once(reviewed, settings, fake_steam,
                                                                                       fake_twitchtracker):
    api, helix = reviewed
    helix.top.append(dict(helix.top[-1]))  # Twitch repeating Dota Two in its answer: still one category
    refresh_lists(settings, reviewed, fake_steam, at(RELEASE_DAY))
    report = collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    # Dota Two is in Twitch's list and reviewed: asked once. Never Just Chatting, Battle Mix or Shooter.
    assert asked(fake_twitchtracker) == ["1001", "1002", "1003", "1006", "1007", "1008", "2001"]
    assert report.counts == {"answered": 7, "summaries": 6, "no_summary": 1, "requests": 7}
    saved = rows(settings)
    assert (saved["1007"]["igdb_id"], saved["1007"]["match_source"], saved["1007"]["selected_at"]) == (
        None, "reviewer", "2026-11-19T12:00:00+00:00")  # the series: no game
    assert (saved["1008"]["igdb_id"], saved["1008"]["match_source"]) == (210, "reviewer")  # by the reviewed row alone
    assert (saved["2001"]["category_name"], saved["2001"]["igdb_id"], saved["2001"]["selected_at"]) == (
        "Old Favourite", 211, None)  # outside Twitch's list: the reviewed title, no list time
    # Three days later Twitch's list is gone: the categories chosen from it are asked again with the reviewed ones,
    # except one a reviewer has since ruled out; one reviewed since is added.
    write_csv(settings.curated_dir / "twitch_categories.csv", [
        REVIEWED, SERIES, DOTA, OUTSIDE, {**REVIEWED, "twitch_category_id": "1003", "title": "Arena Two"},
        {**REVIEWED, "twitch_category_id": "3001", "title": "Old Classic", "decision": "match", "igdb_id": "204"}])
    fake_twitchtracker.requests.clear()
    collect(settings, fake_twitchtracker, at(RELEASE_DAY + timedelta(days=3)))
    assert sorted(asked(fake_twitchtracker)) == ["1001", "1002", "1006", "1007", "1008", "2001", "3001"]
    saved = rows(settings)
    assert "1003" not in saved and (saved["3001"]["igdb_id"], saved["3001"]["selected_at"]) == (204, None)


def test_a_failure_keeps_the_last_figures_and_says_why_the_run_stopped(world, settings, fake_steam,
                                                                       fake_twitchtracker):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    later = at(RELEASE_DAY + timedelta(days=1))
    fake_twitchtracker.requests.clear()
    fake_twitchtracker.summaries["1001"] = summary(1, 700_000)
    fake_twitchtracker.responses = [200, 429]  # Launch Day is answered, then the rate limit stops the run
    report = collect(settings, fake_twitchtracker, later)
    assert asked(fake_twitchtracker) == ["1001", "1002"] and report.status() == "partial"
    assert report.counts["answered"] == 1 and report.counts["not_asked"] == 2
    assert report.stopped_early == ("TwitchTracker answered HTTP 429 (too many requests). The run stopped; the stored "
                                    "figures are kept.")
    saved = rows(settings)
    assert (saved["1001"]["avg_viewers"], saved["1001"]["retrieved_at"]) == (700_000, "2026-11-20T12:00:00+00:00")
    assert (saved["1002"]["rank"], saved["1002"]["status"], saved["1002"]["retrieved_at"]) == (
        4, "error", "2026-11-19T12:00:00+00:00")  # the previous figures, with their own time
    assert saved["1003"]["retrieved_at"] == "2026-11-19T12:00:00+00:00"  # not asked: still due
    assert due(settings, later) == ["1002", "1003", "1006"]

    fake_twitchtracker.requests.clear()
    fake_twitchtracker.responses = ["timeout"] * twitchtracker.MAX_ATTEMPTS
    report = collect(settings, fake_twitchtracker, later + timedelta(hours=1))
    assert report.status() == "failed" and len(fake_twitchtracker.requests) == twitchtracker.MAX_ATTEMPTS
    assert "could not be reached after 3 attempts (ReadTimeout)" in report.errors[0]

    fake_twitchtracker.summaries["1003"] = {"avg_viewers": 9}  # no rank: only this category is skipped
    report = collect(settings, fake_twitchtracker, later + timedelta(hours=2))
    assert report.status() == "partial" and report.counts == {"answered": 2, "summaries": 1, "no_summary": 1,
                                                              "invalid": 1, "requests": 3}
    assert "Arena Two (1003): TwitchTracker's answer had no valid rank; its previous figures are kept." in report.errors
    assert (rows(settings)["1003"]["rank"], rows(settings)["1003"]["status"]) == (6, "invalid")


def test_this_site_sends_at_most_its_daily_budget_in_any_24_hours(world, settings, fake_steam, fake_twitchtracker):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    conn = store.connect(settings.database)
    try:
        run = store.start_source_run(conn, twitchtracker.SOURCE, now="2026-11-18T13:00:00+00:00")
        store.finish_source_run(conn, run, status="success", now="2026-11-18T13:00:00+00:00",
                                summary={"counts": {"requests": twitchtracker.REQUESTS_PER_DAY - 1}})
        assert twitchtracker.requests_left(conn, at(RELEASE_DAY)) == 1
    finally:
        conn.close()
    report = collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    assert len(fake_twitchtracker.requests) == 1 and report.status() == "partial"
    assert report.stopped_early == ("Stopped: this site sends TwitchTracker at most 200 requests in 24 hours; 3 "
                                    "categories wait for a later run.")


# ---------------------------------------------------------------------- the Trending page


def test_this_computer_sees_places_one_to_three_beside_twitchtrackers_own_ranks(world, settings, fake_steam,
                                                                                fake_twitchtracker):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    html = trending(settings, at(RELEASE_DAY, 13))
    ol = listed(html)
    # TwitchTracker's order; places without gaps, and TwitchTracker's own ranks as returned: #2 (Just Chatting) and
    # #5 (Battle Mix, not asked) are gaps.
    assert re.findall(r'data-details="([^"]+)"', ol) == ["igdb-201", "101", "igdb-208"]
    assert places(ol) == ["1", "2", "3"] and ranks(ol) == ["1", "4", "6"]
    text = text_of(watched_part(html))
    assert "Most watched among tracked Twitch categories All platforms Local evaluation only" in text
    assert "Source: TwitchTracker · retrieved 19 Nov 2026, 12:00 UTC · period not confirmed" in text
    assert ("Games and game series among the Twitch categories this site tracks, in TwitchTracker’s rank order. Not "
            "TwitchTracker’s complete top 10: the number beside each is its place here, and its line gives "
            "TwitchTracker’s own category rank.") in text
    assert "TwitchTracker category rank #1 · 642,962 average viewers · 100 average channels · Watch on Twitch" in text
    assert "TwitchTracker category rank #6 · 48,252 average viewers" in text  # Arena Two, by IGDB's record alone
    assert "numbered without gaps" in text and "TwitchTracker rank #" not in text  # a place is never called a rank
    assert "Not a complete ranking: only the categories this site tracks" in text and "(4 tracked)" in text
    assert "Not listed: 1 that TwitchTracker has no summary of." in text  # Quiet Garden
    assert "Quiet Garden" not in ol and " 0 average viewers" not in text  # a missing figure is never a zero
    assert "Just Chatting" not in ol
    assert "Period Not confirmed. TwitchTracker’s API page calls this a summary “for 30 days”" in text
    assert "never more than about 168 hours, which is 7 days" in text and "the rank may cover another period" in text
    assert "its rank does not always follow the average viewers" in text  # as seen live on 2026-11-21
    assert "Do not publish this list or screenshots of it." in text
    assert 'href="https://www.twitch.tv/directory/game/Arena%20Two"' in ol  # Watch on Twitch: the source
    assert 'href="https://twitchtracker.com/api"' in html
    assert "Twitch's list: #" not in text  # Twitch's own order is not shown beside TwitchTracker's


@pytest.mark.parametrize("client, headers", [
    (("testclient", 50000), None),  # the test client's default: not an address
    (("192.168.1.20", 50000), None),  # another computer on the network
    (LOCAL, {"X-Forwarded-For": "203.0.113.9"}),  # a proxy on this computer passing a visitor on
])
def test_any_other_browser_gets_twitchs_order_and_no_twitchtracker_figure(world, settings, fake_steam,
                                                                          fake_twitchtracker, client, headers):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    html = trending(settings, at(RELEASE_DAY, 13), client=client, headers=headers)
    assert "TwitchTracker" not in html and "642,962" not in html and "average viewers" not in html
    assert "Most watched among tracked Twitch categories" not in html
    watched = watched_part(html)
    # Twitch's own order. Arena Two (no igdb_id on Twitch) is kept by IGDB's record; the disagreement is left out.
    assert re.findall(r'data-details="([^"]+)"', watched) == ["igdb-201", "101", "igdb-208", "igdb-209"]
    text = text_of(watched)
    assert "Most watched on Twitch All platforms" in text
    assert "Twitch's list: #1 · Watch on Twitch" in text and "Twitch's list: #4 · Watch on Twitch" in text
    assert ("Left out at this check: 1 that neither Twitch nor IGDB links to a game (such as Just Chatting), 2 whose "
            "game is unresolved") in text


def test_reviewed_games_and_a_series_are_listed_in_twitchtrackers_order(reviewed, settings, fake_steam,
                                                                        fake_twitchtracker):
    refresh_lists(settings, reviewed, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    html = trending(settings, at(RELEASE_DAY, 13))
    ol = listed(html)
    assert places(ol) == ["1", "2", "3", "4", "5", "6"]
    assert ranks(ol) == ["1", "3", "4", "6", "7", "9"]  # as returned, gaps and all
    # The series opens no game's details; Dota Two and Old Favourite open theirs.
    assert re.findall(r'data-details="([^"]+)"', ol) == ["igdb-201", "101", "igdb-208", "igdb-210", "igdb-211"]
    series = re.search(r'<li class="trend-item is-series">.*?</li>', ol, re.S).group(0)
    assert ("Strike Series Series category Twitch category “Strike Series”: streams of any game in the series, so it "
            "is not counted as one game and opens no game’s details.") in text_of(series)
    assert ("TwitchTracker category rank #3 · 100,000 average viewers · 100 average channels · Watch on Twitch"
            in text_of(series))
    assert "data-details" not in series and "/games/" not in series and "trend-thumb is-cover is-missing" in series
    assert 'href="https://www.twitch.tv/directory/game/Strike%20Series"' in series
    watched = watched_part(html)
    assert "Strike Two" not in watched and 'data-details="102"' not in watched  # never the game IGDB's record names
    assert 'href="https://www.twitch.tv/directory/game/Old%20Favourite"' in ol  # outside Twitch's list: its title
    text = text_of(watched)
    assert ("the game and series categories among Twitch’s first 100 categories at 19 Nov 2026, 12:00 UTC, and 1 "
            "reviewed in curated/twitch_categories.csv outside that list (7 tracked)") in text
    assert "Series A category that a reviewer marked as a game series in curated/twitch_categories.csv" in text
    assert "Just Chatting" not in ol

    public = trending(settings, at(RELEASE_DAY, 13), client=("192.168.1.20", 50000))
    public_ol = listed(public)
    # Twitch's own order: the series at its place (#8), Dota Two by the reviewed row; Old Favourite is not in it.
    assert re.findall(r'data-details="([^"]+)"', public_ol) == ["igdb-201", "101", "igdb-208", "igdb-209", "igdb-210"]
    assert "Strike Series Series category" in text_of(public_ol) and "Twitch's list: #8 · Watch on Twitch" in text_of(
        public_ol)
    assert "Old Favourite" not in public and "TwitchTracker" not in public and "Strike Two" not in public_ol


def test_a_game_reached_by_two_categories_is_listed_once_and_the_list_stops_at_ten(world, settings, fake_steam,
                                                                                    fake_twitchtracker):
    api, helix = world
    helix.top.append({"id": "1009", "name": "Launch Day Classic", "igdb_id": "201"})  # a second category for 201
    fake_twitchtracker.summaries["1009"] = summary(15, 5_000)
    for number in range(12):  # twelve more games, TwitchTracker ranks 20, 23, 26, ...
        igdb_id, category_id = 300 + number, str(3000 + number)
        api.games[igdb_id] = igdb_game(igdb_id, f"Extra {number:02}", entry("PC", day=date(2020, 1, 1)))
        helix.top.append({"id": category_id, "name": f"Extra {number:02}", "igdb_id": str(igdb_id)})
        fake_twitchtracker.summaries[category_id] = summary(20 + 3 * number, 10_000 - number)
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    html = trending(settings, at(RELEASE_DAY, 13))
    ol = listed(html)
    assert places(ol) == [str(place) for place in range(1, 11)]  # the first 10 entries
    assert ranks(ol) == ["1", "4", "6", "20", "23", "26", "29", "32", "35", "38"]
    assert re.findall(r'data-details="([^"]+)"', ol).count("igdb-201") == 1  # at its better rank, #1
    assert "1 for a game another category already stands for" in text_of(watched_part(html))


def test_stale_old_or_failed_figures_say_when_they_were_retrieved(world, settings, fake_steam, fake_twitchtracker):
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    assert "Stale" not in text_of(watched_part(trending(settings, at(RELEASE_DAY, 13))))
    stale = text_of(watched_part(trending(settings, at(RELEASE_DAY, 12) + timedelta(hours=21))))
    assert ("Stale: the newest figures were retrieved 19 Nov 2026, 12:00 UTC, more than 20 hours ago. Run python -m "
            "game_librarian most-watched to refresh them.") in stale
    later = at(RELEASE_DAY + timedelta(days=3))  # no sync for three days: Twitch's list is gone, the figures stay
    fake_twitchtracker.responses = [200, 429]
    collect(settings, fake_twitchtracker, later)
    html = trending(settings, later + timedelta(days=2))
    text = text_of(watched_part(html))
    assert ("Source: TwitchTracker · retrieved 19 Nov 2026, 12:00 UTC to 22 Nov 2026, 12:00 UTC (2 days ago) · "
            "period not confirmed") in text  # the oldest and the newest figures shown
    assert ("The last TwitchTracker refresh (22 Nov 2026, 12:00 UTC) was partial: TwitchTracker answered HTTP 429 (too "
            "many requests). The run stopped; the stored figures are kept. The figures shown were retrieved "
            "22 Nov 2026, 12:00 UTC or earlier.") in text
    assert "Figures retrieved 19 Nov 2026, 12:00 UTC: its last check failed." in text  # Game 01, the 429
    assert "Figures retrieved 19 Nov 2026, 12:00 UTC: not asked again since." in text  # Arena Two
    assert "No Twitch list is saved from the last 24 hours" in text_of(trending(settings, later, client=("x", 1)))


def test_a_category_asked_hours_after_the_others_leaves_their_figures_current(reviewed, settings, fake_steam,
                                                                             fake_twitchtracker):
    refresh_lists(settings, reviewed, fake_steam, at(RELEASE_DAY))
    write_csv(settings.curated_dir / "twitch_categories.csv", [REVIEWED, SERIES, DOTA])  # Old Favourite: not yet
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))
    write_csv(settings.curated_dir / "twitch_categories.csv", [REVIEWED, SERIES, DOTA, OUTSIDE])
    fake_twitchtracker.requests.clear()
    collect(settings, fake_twitchtracker, at(RELEASE_DAY, 14))  # two hours later only the new row is due
    assert asked(fake_twitchtracker) == ["2001"]
    text = text_of(watched_part(trending(settings, at(RELEASE_DAY, 15))))
    assert ("Source: TwitchTracker · retrieved 19 Nov 2026, 12:00 UTC to 19 Nov 2026, 14:00 UTC · period not "
            "confirmed") in text
    assert "the oldest figures shown are from 19 Nov 2026, 12:00 UTC" in text
    assert "Figures retrieved" not in text  # all within the 20-hour cache: none is called old
    fake_twitchtracker.summaries["1002"] = {"avg_viewers": 9}  # an invalid answer: its earlier figures stay
    collect(settings, fake_twitchtracker, at(RELEASE_DAY, 16), force=True)
    text = text_of(watched_part(trending(settings, at(RELEASE_DAY, 17))))
    assert "Figures retrieved 19 Nov 2026, 12:00 UTC: its last check failed." in text  # Game 01, however recent
    assert text.count("Figures retrieved") == 1


def test_a_reviewers_later_decision_applies_at_once_and_the_empty_states_say_what_to_run(world, settings, fake_steam,
                                                                                       fake_twitchtracker):
    empty = text_of(watched_part(trending(settings, at(RELEASE_DAY))))
    assert "No TwitchTracker figures yet. Run python -m game_librarian most-watched after upcoming" in empty
    refresh_lists(settings, world, fake_steam, at(RELEASE_DAY))
    fake_twitchtracker.responses = [403]
    collect(settings, fake_twitchtracker, at(RELEASE_DAY))  # refused at once: nothing answered
    waiting = text_of(watched_part(trending(settings, at(RELEASE_DAY, 13))))
    # Missing figures are told apart: never asked, or asked with a failed check and no earlier figures.
    assert ("None of the 4 tracked categories has TwitchTracker figures yet (3 not asked yet, 1 with a failed check "
            "and no earlier figures). Run python -m game_librarian most-watched.") in waiting
    assert "failed: Launch Day (1001): TwitchTracker refused the request (HTTP 403)" not in waiting  # the problem alone
    assert "failed: TwitchTracker refused the request (HTTP 403); it is not retried or worked around." in waiting
    collect(settings, fake_twitchtracker, at(RELEASE_DAY, 14))
    write_csv(settings.curated_dir / "twitch_categories.csv", [
        REVIEWED, {**REVIEWED, "twitch_category_id": "1003", "title": "Arena Two", "note": "Not a game"}])
    watched = watched_part(trending(settings, at(RELEASE_DAY, 15)))
    assert re.findall(r'data-details="([^"]+)"', watched) == ["igdb-201", "101"]  # Arena Two is no longer a game
    assert "1 whose game is unresolved" in text_of(watched)
    write_csv(settings.curated_dir / "twitch_categories.csv", [
        REVIEWED, {**REVIEWED, "twitch_category_id": "1003", "title": "Arena Two", "decision": "series"}])
    ol = listed(trending(settings, at(RELEASE_DAY, 16)))
    assert re.findall(r'data-details="([^"]+)"', ol) == ["igdb-201", "101"] and places(ol) == ["1", "2", "3"]
    assert "Arena Two Series category" in text_of(ol)  # now a series, at once, with its figures


# ---------------------------------------------------------------------- the dashboard


def save_tracked(settings, category_id, name, igdb_id, figures, retrieved_at):
    """One chosen category with TwitchTracker figures, as `most-watched` saves it."""
    conn = store.connect(settings.database)
    try:
        chosen = store.tracker_rows(conn)
        categories = [twitchtracker.Category(row["category_id"], row["category_name"], row["igdb_id"],
                                             row["match_source"], row["selected_at"]) for row in chosen]
        categories.append(twitchtracker.Category(category_id, name, igdb_id, "twitch+igdb", retrieved_at))
        store.replace_tracker_selection(conn, categories)
        store.save_tracker_answer(conn, category_id, figures, retrieved_at=retrieved_at)
    finally:
        conn.close()


def shared_panel(html: str) -> str:
    """The Upcoming releases panel of Shared Saga (IGDB 501), as its <template> holds it."""
    return text_of(re.search(r'<template id="radar-game-igdb-501">(.*?)</template>', html, re.S).group(1))


def test_the_upcoming_panel_gives_twitchtrackers_rank_on_this_computer_and_twitchs_place_elsewhere(games, settings):
    save_captures(settings)  # Shared Saga (IGDB 501) is #9 in Twitch's list
    save_tracked(settings, "801", "Shared Saga", 501, Summary(37, 4_321, 210, 726_000), "2026-11-09T12:00:00+00:00")
    local = dashboard_client(settings, client=LOCAL).get("/dashboard", params={"report": "upcoming"}).text
    assert ("TwitchTracker category rank #37, 4,321 average viewers (period not confirmed) · Watch on Twitch"
            in shared_panel(local))
    assert "Twitch’s most-watched list" not in local  # one Most watched entry per game, never both side by side
    assert "TwitchTracker’s ranks retrieved 9 Nov 2026, 12:00 UTC (on this computer only)." in text_of(
        chart(local, "chart-radar"))
    assert "TwitchTracker" not in text_of(chart(local, "chart-compare"))  # not part of the comparison's denominator
    notes = text_of(methodology(local))
    assert "TwitchTracker: not part of the comparison, and shown on this computer only" in notes
    assert ("A series category, such as Counter-Strike, stands for no single game, so it is never matched to one."
            in notes)
    assert "so the period is not confirmed" in notes and "No history is kept, so no change is shown." in notes
    public = dashboard_client(settings).get("/dashboard", params={"report": "upcoming"}).text
    assert "#9 on Twitch’s most-watched list · Watch on Twitch" in shared_panel(public)
    assert "TwitchTracker" not in public and "4,321" not in public


def test_a_series_category_is_never_matched_to_a_game_on_the_dashboard(games, settings):
    # Chosen for Shared Saga (as a 0.18 database holds it), then marked a series by a reviewer: no game's entry now.
    save_captures(settings)
    save_tracked(settings, "801", "Shared Series", 501, Summary(37, 4_321, 210, 726_000), "2026-11-09T12:00:00+00:00")
    write_csv(settings.curated_dir / "twitch_categories.csv",
              [{**REVIEWED, "twitch_category_id": "801", "title": "Shared Series", "decision": "series"}])
    local = dashboard_client(settings, client=LOCAL).get("/dashboard", params={"report": "upcoming"}).text
    assert "TwitchTracker" not in shared_panel(local) and "4,321" not in shared_panel(local)
    page = text_of(listed(trending(settings, NOW)))
    assert "Shared Series Series category" in page and "TwitchTracker category rank #37 · 4,321 average viewers" in page


def test_the_sales_chart_on_this_computer_takes_most_watched_from_twitchtracker(games, settings):
    trending_with_estimates(settings)  # Game 10 has an estimate but is on no Trending list
    assert "Game 10" not in text_of(sales_section(board(settings)))
    record = upcoming.record_from_igdb(igdb_game(610, "Game 10", entry("PC", day=date(2024, 1, 1)), steam=[110]))
    conn = store.connect(settings.database)
    try:
        store.save_igdb_games(conn, [record], {610: SteamMatch("verified", 110, "The same name on Steam.")}, [],
                              now="2026-11-09T12:00:00+00:00")
    finally:
        conn.close()
    save_tracked(settings, "910", "Game 10", 610, Summary(12, 9_000, 300, None), "2026-11-09T12:00:00+00:00")
    section = sales_section(board(settings))
    table = text_of(re.search(r"<table.*?</table>", section, re.S).group(0))
    assert re.search(r"Game 10 5,000,000 [\d.]+% Most watched among tracked Twitch categories", table)
    # A reviewer marks the category a series: it stands for no single game, so Game 10 is on no list again.
    write_csv(settings.curated_dir / "twitch_categories.csv",
              [{**REVIEWED, "twitch_category_id": "910", "title": "Game 10", "decision": "series"}])
    assert "Game 10" not in text_of(sales_section(board(settings)))


# ---------------------------------------------------------------------- the command, sync and status


def use_fakes(monkeypatch, settings, world, fake_steam):
    api, helix = world
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, igdb_client_id=CLIENT_ID,
                                                              igdb_client_secret=CLIENT_SECRET))
    monkeypatch.setattr(cli, "IgdbClient", lambda client_id, client_secret: api.client())
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    monkeypatch.setattr(cli, "TwitchClient", lambda client_id, client_secret: helix.client())


def test_the_command_and_sync_ask_only_when_due_and_say_where_the_figures_show(world, settings, fake_steam,
                                                                               fake_twitchtracker, monkeypatch, capsys):
    use_fakes(monkeypatch, settings, world, fake_steam)
    assert cli.main(["most-watched"]) == 0
    assert "nothing to ask about. The categories come from Twitch's top list" in capsys.readouterr().out
    assert cli.main(["upcoming"]) == 0
    capsys.readouterr()
    assert cli.main(["most-watched"]) == 0
    out = capsys.readouterr().out
    assert "4 categories tracked, 4 game and series categories from Twitch's list of" in out
    assert "asking about 4 of 4 categories, one request each, 1.5 s apart (free API, no key)" in out
    assert "   1. Launch Day [igdb-201]: TwitchTracker category rank #1, 642,962 average viewers" in out
    assert "   3. Arena Two [igdb-208]: TwitchTracker category rank #6, 48,252 average viewers" in out
    assert "Not listed: 1 no_summary" in out
    assert "Shown only to a browser on this computer: TwitchTracker publishes no terms" in out
    assert cli.main(["most-watched"]) == 0
    assert "nothing is due; each category is asked again after 20 hours (--force asks now)" in capsys.readouterr().out
    assert len(fake_twitchtracker.requests) == 4  # the second run asked nothing
    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "Most watched (TwitchTracker, shown on this computer only): 3 of 4 tracked categories with figures" in status
    assert cli.main(["sync", "--skip-upcoming", "--skip-critics", "--skip-sales"]) == 0
    assert "Most watched (TwitchTracker): nothing is due" in capsys.readouterr().out
    assert cli.main(["sync", "--skip-most-watched", "--skip-critics", "--skip-sales"]) == 0
    assert "Most watched (TwitchTracker): skipped (--skip-most-watched)." in capsys.readouterr().out
    fake_twitchtracker.responses = [429]
    assert cli.main(["most-watched", "--force"]) == 1  # nothing was answered: the run failed, and says why
    assert "TwitchTracker answered HTTP 429" in capsys.readouterr().out
    assert rows(settings)["1001"]["avg_viewers"] == 642_962  # kept after the 429
    output = capsys.readouterr()
    for secret in (CLIENT_ID, CLIENT_SECRET):
        assert secret not in output.out + output.err + out + status


def test_the_commands_print_a_series_as_a_series(reviewed, settings, fake_steam, fake_twitchtracker, monkeypatch,
                                                 capsys):
    use_fakes(monkeypatch, settings, reviewed, fake_steam)
    assert cli.main(["upcoming"]) == 0
    assert "Strike Series [series category: no single game]" in capsys.readouterr().out
    assert cli.main(["most-watched"]) == 0
    out = capsys.readouterr().out
    assert "7 categories tracked, 6 game and series categories from Twitch's list of" in out
    assert "and 1 reviewed outside it." in out
    assert "   2. Strike Series [series category]: TwitchTracker category rank #3, 100,000 average viewers" in out
    assert "   6. Old Favourite [igdb-211]: TwitchTracker category rank #9, 20,000 average viewers" in out


def test_sync_without_igdb_skips_the_step_and_never_calls_twitchtracker(games, settings, fake_steam, fake_twitchtracker,
                                                                        monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    assert cli.main(["sync", "--skip-sales"]) == 0
    assert "Most watched (TwitchTracker): skipped, IGDB is not configured" in capsys.readouterr().out
    assert fake_twitchtracker.requests == []


# ---------------------------------------------------------------------- the reviewer file


def test_reviewer_decisions_about_twitch_categories_are_validated(settings, monkeypatch, capsys):
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "101", "title": "Catalog Game"}])
    write_csv(settings.curated_dir / "twitch_categories.csv", [
        {**REVIEWED, "twitch_category_id": "32399", "title": "Counter-Strike", "decision": "series"},
        {**REVIEWED, "twitch_category_id": "490422", "decision": "match", "igdb_id": "239"},
        {**REVIEWED, "twitch_category_id": "1", "decision": "match", "igdb_id": ""},
        {**REVIEWED, "twitch_category_id": "2", "decision": "no_match", "igdb_id": "5"},
        {**REVIEWED, "twitch_category_id": "32399"},
        {**REVIEWED, "twitch_category_id": "Dota 2", "decision": "maybe", "checked_by": ""},
        {**REVIEWED, "twitch_category_id": "3", "decision": "series", "igdb_id": "1372"},
        {**REVIEWED, "twitch_category_id": "4", "decision": "match", "igdb_id": "9", "title": ""},
        {**REVIEWED, "twitch_category_id": "5", "title": ""},  # a no_match needs no title
    ])
    data = load_curated(settings.curated_dir)
    assert set(data.twitch_category_decisions) == {"32399", "490422", "5"}
    counter_strike = data.twitch_category_decisions["32399"]
    assert (counter_strike.decision, counter_strike.igdb_id, counter_strike.title) == ("series", None, "Counter-Strike")
    assert data.twitch_category_decisions["490422"].igdb_id == 239
    problems = "\n".join(data.problems)
    assert "twitch_categories.csv:4: a match needs the IGDB game id in igdb_id" in problems
    assert "twitch_categories.csv:5: no_match takes no igdb_id" in problems
    assert "twitch_categories.csv:6: duplicate twitch_category_id 32399" in problems
    assert "twitch_categories.csv:7:" in problems and "must be Twitch's category id (digits)" in problems
    assert "checked_by is empty" in problems and "decision must be one of ['match', 'no_match', 'series']" in problems
    assert "twitch_categories.csv:8: series takes no igdb_id (the category stands for no single game)" in problems
    assert "twitch_categories.csv:9: a match needs Twitch's category name in title" in problems
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["check-curated"]) == 1
    assert "twitch_categories.csv: 3 reviewed Twitch category decisions" in capsys.readouterr().out


def test_the_shipped_reviewer_file_matches_dota_2_and_lists_counter_strike_as_a_series():
    data = load_curated(DEFAULT_CURATED_DIR)
    assert not [problem for problem in data.problems if "twitch_categories" in problem]
    dota = data.twitch_category_decisions["29595"]  # no igdb_id on Twitch; IGDB's record and Steam app 570 agree
    assert (dota.decision, dota.igdb_id, dota.title) == ("match", 2963, "Dota 2")
    counter_strike = data.twitch_category_decisions["32399"]  # Twitch's "Counter-Strike": CS2 and CS:GO are both live
    assert (counter_strike.decision, counter_strike.igdb_id, counter_strike.title) == ("series", None, "Counter-Strike")
