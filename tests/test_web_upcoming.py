"""Web pages added in version 0.6: Upcoming & Anticipated on Trending, details of games known only from IGDB,
stable game keys, "Watch it live" and "What the critics said" in the details, and secrets never reaching a page.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114). IGDB records are saved through the
same store functions the refresh uses; Twitch is a fake that fails the test if a page calls it unexpectedly.
"""

import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from game_librarian import store, upcoming
from game_librarian.upcoming import SignalEntry, SteamMatch
from game_librarian.web import create_app
from tests.test_twitch import FakeHelix
from tests.test_upcoming import entry, igdb_game
from tests.test_web import TODAY, games, text_of  # noqa: F401

NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
SECRET_ID, SECRET_KEY, STEAM_KEY = "web-igdb-client-id-123", "web-igdb-secret-456", "web-steam-key-789"


def configured(settings):
    return replace(settings, igdb_client_id=SECRET_ID, igdb_client_secret=SECRET_KEY, steam_api_key=STEAM_KEY)


def save_upcoming(settings, *, history=True):
    """Four IGDB games, two list snapshots (a week apart) and one noticed date change."""
    records = {
        1: upcoming.record_from_igdb({**igdb_game(1, "Moon Garden", entry("PC", day=date(2027, 2, 23)),
                                                  entry("PlayStation 5", day=date(2027, 2, 23)), steam=[500],
                                                  platforms=("PC", "PlayStation 5")),
                                      "websites": [{"type": 1, "url": "https://moongarden.example/"},
                                                   {"type": 23, "url": "https://ps.example/moon"}],
                                      "summary": "Grow a garden on the moon.", "cover": {"image_id": "co1abc"}}),
        2: upcoming.record_from_igdb(igdb_game(2, "Console Quest", entry("PlayStation 5", fmt=7),
                                               platforms=("PlayStation 5",))),
        3: upcoming.record_from_igdb(igdb_game(3, "Already Out", entry("PC", day=date(2026, 11, 1)))),
        4: upcoming.record_from_igdb(igdb_game(4, "Game 01 Saga", entry("PC", day=date(2024, 1, 1), status=3),
                                               entry("PC", fmt=1, y=2027, m=3), steam=[101])),
    }
    matches = {1: SteamMatch("verified", 500, "IGDB lists Steam app 500, and Steam's name for it is the same."),
               2: SteamMatch("none", None, "IGDB lists no Steam app for this game."),
               3: SteamMatch("none", None, "IGDB lists no Steam app for this game."),
               4: SteamMatch("verified", 101, "IGDB lists Steam app 101, and Steam's name for it is the same.")}
    earlier, latest = NOW - timedelta(days=8), NOW - timedelta(hours=1)
    conn = store.connect(settings.db_path)
    try:
        store.save_igdb_games(conn, list(records.values()), matches, [], now=latest.isoformat(timespec="seconds"))
        delay = upcoming.DateChange("PC", "Q4 2026", "23 Feb 2027", "later")
        store.save_igdb_games(conn, [], {}, [(1, delay)], now=(NOW - timedelta(days=2)).isoformat(timespec="seconds"))
        calculated = NOW - timedelta(hours=10)
        if history:
            store.save_popularity_snapshot(conn, "want_to_play", [SignalEntry(1, 0.003, 9, calculated)],
                                           now=earlier.isoformat(timespec="seconds"))
        store.save_popularity_snapshot(conn, "want_to_play", [
            SignalEntry(1, 0.004, 7, calculated), SignalEntry(2, 0.003, 8, calculated),
            SignalEntry(3, 0.002, 11, calculated), SignalEntry(4, 0.001, 12, calculated)],
            now=latest.isoformat(timespec="seconds"))
        store.save_popularity_snapshot(conn, "steam_wishlists", [
            SignalEntry(4, 0.0004, 1, calculated), SignalEntry(1, 0.0003, 2, calculated)],
            now=latest.isoformat(timespec="seconds"))
        store.save_steam_igdb_links(conn, {101: ("linked", 4), 102: ("none", None)}, now=latest.isoformat())
    finally:
        conn.close()


class NoTwitch:
    """A Twitch client factory that fails the test: page requests other than /live must not call Twitch."""

    def __call__(self):
        pytest.fail("Twitch was called by a page that must only read stored data")


def client_for(settings, twitch_client=None):
    return TestClient(create_app(settings, today=lambda: TODAY, now=lambda: NOW,
                                 twitch_client=twitch_client or NoTwitch()))


def section(html: str, heading: str) -> str:
    """The text of the details section whose heading is `heading`."""
    start = html.find(f">{heading}</h3>")
    assert start >= 0, heading
    return text_of(html[start:html.find("</section>", start)])


# ---------------------------------------------------------------------- Trending: Upcoming & Anticipated


def test_trending_lists_upcoming_games_per_signal_with_dates_as_precise_as_igdb_gives_them(games, settings):
    save_upcoming(settings)
    html = client_for(configured(settings)).get("/trending").text
    text = text_of(html)
    upcoming_part = text[text.find("Upcoming & Anticipated All platforms Games not out yet on any platform"):
                         text.find("Trending now & recently released All platforms")]
    want = upcoming_part[upcoming_part.find("Most wanted on IGDB"):upcoming_part.find("Most wishlisted on Steam")]
    # A short subtitle and source line above the games; the full definition folds under "About this list".
    assert want.startswith("Most wanted on IGDB All platforms What IGDB.com users most want to play. A relative "
                           "order, not a count. Source: IGDB.com · updated 9 Nov 2026, 12:00 UTC About this list "
                           "Signal IGDB PopScore “Want to Play”")
    assert "Moon Garden Announced for PC, PlayStation 5 · 23 Feb 2027" in want
    assert "Console Quest Announced for PlayStation 5 · TBA" in want  # never a made-up day
    assert "Already Out" not in upcoming_part  # released since the snapshot: no longer listed as upcoming
    assert "Early access on PC since 1 Jan 2024" in want
    assert "Up 2 places since 1 Nov 2026 (position 9 → 7 in IGDB's order)" in want  # snapshots 8 days apart
    assert "IGDB's date for PC: moved later (Q4 2026 → 23 Feb 2027) noticed 7 Nov 2026" in want
    assert "relative share, not a count" in upcoming_part and "not wishlist counts" in upcoming_part
    assert "Data from IGDB.com" in upcoming_part
    # A game whose verified Steam app is in the catalog opens the catalog game; others keep their IGDB key.
    assert 'data-details="101"' in html and 'data-details="igdb-1"' in html and 'data-details="igdb-4"' not in html
    steam = upcoming_part[upcoming_part.find("Most wishlisted on Steam"):]
    assert steam.index("Game 01 Saga") < steam.index("Moon Garden")  # Steam's order, not IGDB's
    assert "Position changes appear once this list has snapshots 7 to 14 days apart" in steam


def test_trending_explains_missing_upcoming_data(games, settings):
    not_configured = text_of(client_for(settings).get("/trending").text)
    assert "set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET" in not_configured
    waiting = text_of(client_for(configured(settings)).get("/trending").text)
    assert "have not been refreshed yet. Run python -m game_librarian upcoming" in waiting
    assert "Popular on Steam right now" in waiting  # the Steam sections stay, labeled as Steam only


# ---------------------------------------------------------------------- details of IGDB games and keys


def test_an_upcoming_game_has_igdb_details_without_steam_only_data(games, settings):
    save_upcoming(settings)
    client = client_for(settings)
    html = client.get("/games/igdb-2/panel").text
    text = text_of(html)
    assert "Upcoming · not in the Steam catalog" in text and "Platforms Announced for PlayStation 5 · TBA" in text
    for heading in ("Price on Steam", "Steam reviews", "Verified awards", "More games like this"):
        assert f">{heading}</h3>" not in html  # Steam-only sections are not shown for a game outside the catalog
    assert "View on Steam" not in text and "players in game" not in text
    assert "Steam: no Steam store page is known for this game." in text
    assert 'data-wishlist-toggle data-key="igdb-2"' in html
    assert "Not released yet, so there are no critic reviews to show." in section(html, "What the critics said")
    assert "Live streams need Twitch access" in section(html, "Watch it live")
    page = client.get("/games/igdb-1")
    assert page.status_code == 200 and "View on Steam" in page.text  # a verified Steam match, not in the catalog
    assert 'href="https://store.steampowered.com/app/500/"' in page.text
    assert "Official website" in page.text and "PlayStation Store" in page.text and "Description from IGDB.com." in page.text
    assert "Grow a garden on the moon." in page.text and "images.igdb.com/igdb/image/upload/t_cover_big/co1abc.jpg" in page.text


def test_a_steam_link_needs_a_verified_match(games, settings):
    save_upcoming(settings)
    conn = store.connect(settings.db_path)
    try:
        store.save_igdb_games(conn, [], {}, [], now=NOW.isoformat())
        conn.execute("UPDATE igdb_games SET steam_match = 'needs_review', steam_appid = NULL, "
                     "steam_match_reason = 'Steam calls app 500 ''Moon Garden Deluxe''.' WHERE igdb_id = 1")
        conn.commit()
    finally:
        conn.close()
    text = text_of(client_for(settings).get("/games/igdb-1/panel").text)
    assert "View on Steam" not in text and "the match is not verified, so it is not linked" in text


def test_keys_resolve_to_one_game_and_unknown_keys_are_not_found(games, settings):
    save_upcoming(settings)
    client = client_for(settings)
    merged = client.get("/games/igdb-4/panel")  # IGDB game 4 is catalog game 101 on Steam
    assert merged.status_code == 200 and "Five-year rank" in merged.text
    assert 'data-key="101" data-aliases="igdb-4"' in merged.text
    assert 'data-aliases="igdb-4"' in client.get("/games/101/panel").text  # saved as igdb-4 counts as saved
    assert client.get("/games/500/panel").status_code == 200  # Steam app of an IGDB game outside the catalog
    for key in ("igdb-999", "not-a-key", "0", "igdb-", "101a"):
        assert client.get(f"/games/{key}/panel").status_code == 404
        assert client.get(f"/games/{key}").status_code == 404


# ---------------------------------------------------------------------- critics in the details


def save_critics(settings, status="verified"):
    conn = store.connect(settings.db_path)
    try:
        store.save_critics(conn, "101", {
            "status": status, "reason": "checked", "wikidata_ids": ["Q1"], "article_title": "Game 01",
            "revision_id": 4242, "revision_at": "2026-11-01T00:00:00Z", "table_status": "ok"}, [
            {"outlet": "Metacritic", "kind": "aggregate", "score": "87/100", "label": "PC", "status": "ok",
             "review_url": "https://www.metacritic.com/game/game-01/"},
            {"outlet": "IGN", "kind": "critic", "score": "9/10", "status": "ok"},
            {"outlet": "Famitsu", "kind": "critic", "score": None, "status": "needs_review",
             "reason": "the cell has several scores on one line"},
        ], now=NOW.isoformat())
    finally:
        conn.close()


def test_critic_scores_show_scale_platform_source_and_attribution_only_when_verified(games, settings):
    save_critics(settings)
    html = client_for(settings).get("/games/101/panel").text
    critics_text = section(html, "What the critics said")
    assert "Aggregate scores Metacritic 87/100 PC" in critics_text and "Reviews IGN 9/10" in critics_text
    assert "Famitsu" not in critics_text and "1 more score is in the table" in critics_text
    assert "Game 01" in critics_text and "CC BY-SA 4.0" in critics_text
    assert "separate from Steam player reviews" in critics_text
    assert 'href="https://en.wikipedia.org/w/index.php?title=Game_01&amp;oldid=4242"' in html
    assert html.index(">Steam reviews</h3>") < html.index(">What the critics said</h3>")
    save_critics(settings, status="needs_review")
    waiting = section(client_for(settings).get("/games/101/panel").text, "What the critics said")
    assert "waiting for a person to confirm" in waiting and "87/100" not in waiting
    unchecked = section(client_for(settings).get("/games/102/panel").text, "What the critics said")
    assert "have not been looked up for this game yet" in unchecked


# ---------------------------------------------------------------------- Watch it live


def test_live_streams_are_checked_by_the_server_and_cached(games, settings):
    save_upcoming(settings)
    fake = FakeHelix()
    fake.categories[4] = [{"id": "555", "name": "Game 01 Saga", "igdb_id": "4"}]
    fake.streams["555"] = [{"user_login": "streamer_one", "user_name": "StreamerOne", "type": "live",
                            "viewer_count": 1234, "title": "First run", "language": "en", "started_at": ""}]
    client = client_for(configured(settings), twitch_client=fake.client)
    panel = client.get("/games/101/panel").text
    assert fake.requests == []  # opening the details never waits for Twitch
    assert 'data-live-refresh="/games/101/live?heading=dialog-heading"' in panel
    live = client.get("/games/101/live?heading=dialog-heading")
    text = text_of(live.text)
    assert "StreamerOne on Twitch (opens in a new tab) 1,234 viewers First run" in text
    assert 'href="https://www.twitch.tv/streamer_one"' in live.text and "data-live-refresh" not in live.text
    assert 'id="dialog-heading-live"' in live.text
    calls = len(fake.requests)
    client.get("/games/101/live")  # within five minutes: the stored answer, no new call
    assert len(fake.requests) == calls
    upcoming_live = text_of(client.get("/games/igdb-2/live").text)  # no Twitch category for IGDB game 2
    assert "Twitch has no category linked to this game's IGDB record" in upcoming_live
    assert client.get("/games/igdb-999/live").status_code == 404
    assert 'id="live-live"' in client.get("/games/101/live?heading=<script>").text  # odd ids are not echoed


def test_live_streams_of_an_unreleased_game_are_labeled(games, settings):
    save_upcoming(settings)
    fake = FakeHelix()
    fake.categories[1] = [{"id": "777", "name": "Moon Garden", "igdb_id": "1"}]
    fake.streams["777"] = [{"user_login": "newsdesk", "user_name": "NewsDesk", "type": "live", "viewer_count": 5,
                            "title": "Trailer reactions", "language": "en", "started_at": ""}]
    text = text_of(client_for(configured(settings), twitch_client=fake.client).get("/games/igdb-1/live").text)
    assert "not released yet, so streams in its category may show trailers, news or discussion" in text


def test_twitch_failures_show_a_clear_state(games, settings):
    save_upcoming(settings)
    fake = FakeHelix()
    fake.categories[4] = [{"id": "555", "name": "Game 01 Saga", "igdb_id": "4"}]
    fake.statuses = [503, 503]
    client = client_for(configured(settings), twitch_client=fake.client)
    text = text_of(client.get("/games/101/live").text)
    assert "Twitch could not be reached just now. Try again in a few minutes." in text
    calls = len(fake.requests)
    client.get("/games/101/live")  # failures are not retried for two minutes
    assert len(fake.requests) == calls


# ---------------------------------------------------------------------- secrets


def test_no_secret_reaches_a_page(games, settings):
    save_upcoming(settings)
    save_critics(settings)
    fake = FakeHelix()
    fake.categories[4] = [{"id": "555", "name": "Game 01 Saga", "igdb_id": "4"}]
    client = client_for(configured(settings), twitch_client=fake.client)
    pages = ["/", "/trending", "/wishlist", "/dashboard", "/dashboard/trending", "/games/101", "/games/101/panel",
             "/games/101/live", "/games/igdb-1", "/games/igdb-2/live", "/static/app.js", "/static/wishlist.js"]
    for path in pages:
        body = client.get(path).text
        for secret in (SECRET_ID, SECRET_KEY, STEAM_KEY, "twitch-token-1", "Bearer"):
            assert secret not in body, (path, secret)
    assert fake.tokens == 1


def test_the_existing_catalog_details_and_pagination_still_work(games, settings):
    save_upcoming(settings)
    client = client_for(settings)
    index = client.get("/?view=all&page=2")
    assert index.status_code == 200 and re.search(r'aria-current="page"><span class="visually-hidden">Page </span>2<',
                                                  index.text)
    assert 'href="/games/113?view=all&amp;page=2"' in index.text
    details = client.get("/games/103")
    assert details.status_code == 200 and "Price on Steam" in details.text and "Steam reviews" in details.text
    assert 'data-wishlist-toggle data-key="103" data-aliases=""' in details.text
