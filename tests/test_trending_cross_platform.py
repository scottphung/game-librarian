"""Cross-platform Trending (version 0.7): platform availability, the recent-release rule, Twitch's top games,
verified links, one game identity across a release, Trending-first navigation, and the Steam-only activity
kept apart from the cross-platform lists.

All names, ids and answers are made up; their shapes follow the IGDB and Twitch answers checked live on
2026-11-19 (README "Trending"). Uses the fourteen synced catalog games of test_web.py (app ids 101-114).
"""

import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import store, twitch, upcoming
from game_librarian.curated import CuratedData
from game_librarian.upcoming import availability, recent_release, release_dates, verify_link
from game_librarian.web import create_app
from tests.conftest import store_summary
from tests.test_twitch import FakeHelix
from tests.test_upcoming import FakeIgdbApi, entry, igdb_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NoTwitch, configured

RELEASE_DAY = date(2026, 11, 19)
CATALOG = list(range(101, 115))


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def dates_of(*entries):
    return release_dates({"release_dates": list(entries)})


# ---------------------------------------------------------------------- platforms per release date (pure)


def test_a_console_only_game_is_available_only_where_it_came_out():
    dates = dates_of(entry("PlayStation 5", day=RELEASE_DAY), entry("Xbox Series X|S", day=RELEASE_DAY))
    listed = ["PlayStation 5", "Xbox Series X|S"]
    before = availability(dates, listed, RELEASE_DAY - timedelta(days=1))
    assert not before.available and [group.platforms for group in before.announced] == [tuple(listed)]
    out = availability(dates, listed, RELEASE_DAY)
    assert [(group.label, group.platforms) for group in out.available] == [("19 Nov 2026", tuple(listed))]
    assert not out.announced and not out.undated and out.summary == "Out now"  # no PC or Steam release is guessed


def test_a_game_can_be_out_on_one_platform_and_announced_for_another():
    dates = dates_of(entry("PC", day=date(2026, 10, 15)), entry("PlayStation 5", day=date(2026, 10, 15)),
                     entry("Xbox Series X|S", fmt=7), entry("Nintendo Switch 2", fmt=1, y=2026, m=11))
    found = availability(dates, ["PC", "PlayStation 5", "Xbox Series X|S", "Nintendo Switch 2"], RELEASE_DAY)
    assert [(group.label, group.platforms) for group in found.available] == [("15 Oct 2026", ("PC", "PlayStation 5"))]
    # November is not over, so its platform is still announced: no day inside the month is guessed.
    assert [(group.label, group.platforms) for group in found.announced] == [
        ("Nov 2026", ("Nintendo Switch 2",)), ("TBA", ("Xbox Series X|S",))]
    assert found.summary == "Out on some platforms, more announced"


def test_early_access_missing_ambiguous_and_regional_platform_information():
    early = availability(dates_of(entry("PC", day=date(2024, 12, 6), status=3), entry("PC", day=date(2026, 12, 11))),
                         ["PC"], RELEASE_DAY)
    assert early.early == ({"status": "early access", "label": "6 Dec 2024", "platforms": ["PC"]},)
    assert [group.label for group in early.announced] == ["11 Dec 2026"] and early.summary == "Early access"
    assert not availability([], [], RELEASE_DAY).known  # IGDB says nothing: the page says the platforms are unknown
    listed = availability(dates_of(entry("PC", fmt=2, y=2028)), ["PC", "Mac", ""], RELEASE_DAY)
    assert listed.undated == ("Mac",) and [group.label for group in listed.announced] == ["2028"]
    regional = availability(dates_of(entry("PC", day=date(2026, 11, 1), region=5),
                                     entry("PC", day=date(2026, 12, 1), region=1)), ["PC"], RELEASE_DAY)
    assert regional.available[0].label == "1 Nov 2026" and regional.available[0].regional  # out in one region


def test_a_next_gen_patch_shows_where_a_game_plays_but_is_never_a_first_release():
    # Grand Theft Auto V's shape on IGDB (checked 2026-11-19): PS5 and Xbox Series X|S dates have status 36.
    dates = dates_of(entry("PC", day=date(2015, 4, 13)), entry("PlayStation 5", day=date(2025, 3, 15), status=36),
                     entry("Xbox Series X|S", day=date(2025, 3, 15), status=36))
    found = availability(dates, ["PC", "PlayStation 5", "Xbox Series X|S"], RELEASE_DAY)
    assert [(group.label, group.platforms, group.first.status) for group in found.available] == [
        ("13 Apr 2015", ("PC",), "full"), ("15 Mar 2025", ("PlayStation 5", "Xbox Series X|S"), "next-gen patch")]
    assert not found.undated and not found.early and not found.announced
    patched_lately = dates_of(entry("PC", day=date(2015, 4, 13)),
                              entry("PlayStation 5", day=RELEASE_DAY - timedelta(days=30), status=35))
    assert recent_release(0, None, patched_lately, RELEASE_DAY) is None  # a later release is not a new game
    assert upcoming.pre_release_groups(patched_lately) == []  # nor early access
    assert upcoming.release_state(0, None, patched_lately, None, RELEASE_DAY).early is None


def test_the_recent_rule_counts_from_the_first_day_a_date_allows():
    def recent(*entries, game_type=0, status=None, today=RELEASE_DAY):
        return recent_release(game_type, status, dates_of(*entries), today)

    assert recent(entry("PlayStation 5", day=RELEASE_DAY)).label == "19 Nov 2026"  # out today
    assert recent(entry("PC", day=RELEASE_DAY - timedelta(days=90)))  # the 90th day still counts
    assert recent(entry("PC", day=RELEASE_DAY - timedelta(days=91))) is None
    assert recent(entry("PC", fmt=1, y=2026, m=10)).label == "Oct 2026"  # October is over and began 49 days ago
    assert recent(entry("PC", fmt=1, y=2026, m=8)) is None  # August began more than 90 days ago
    assert recent(entry("PC", fmt=2, y=2026), today=date(2027, 1, 5)) is None  # "2026" may mean January 2026
    assert recent(entry("PC", day=RELEASE_DAY + timedelta(days=1))) is None  # not out yet: still upcoming
    assert recent(entry("PC", day=RELEASE_DAY), status=6) is None  # IGDB marks it cancelled
    assert recent(entry("PC", day=RELEASE_DAY), game_type=1) is None  # DLC


# ---------------------------------------------------------------------- Twitch answers, links and identity (pure)


def test_twitch_top_games_keep_twitchs_order_and_only_real_igdb_ids():
    parsed = twitch.parse_top_games([
        {"id": "509658", "name": "Just Chatting", "igdb_id": ""},  # Twitch's answer for a category that is no game
        {"id": "1867279146", "name": "  Launch   Day ", "igdb_id": "201"},
        {"id": "", "name": "No id"}, {"id": "abc", "name": "Bad id"}, {"id": "5", "name": "  "},
        {"id": "1867279146", "name": "Launch Day again", "igdb_id": "201"},  # a repeat
        {"id": "33214", "name": "Zero id", "igdb_id": "0"}, {"id": "7", "name": "Text id", "igdb_id": "x1"},
    ])
    assert [(item.position, item.category_id, item.name, item.igdb_id) for item in parsed] == [
        (1, "509658", "Just Chatting", None), (2, "1867279146", "Launch Day", 201),
        (7, "33214", "Zero id", None), (8, "7", "Text id", None)]  # positions stay Twitch's, gaps included
    assert twitch.parse_top_games([]) == []


def test_links_are_verified_by_domain_and_never_by_a_lookalike():
    publisher = {"bigstudio.example"}
    assert verify_link("Official website", "https://launch.bigstudio.example/game", publisher)
    assert verify_link("Official website", "https://www.BigStudio.example", publisher)
    assert not verify_link("Official website", "https://bigstudio.example.evil.test/", publisher)
    assert not verify_link("Official website", "https://launchgame.example/", publisher)
    assert not verify_link("Official website", "https://launch.bigstudio.example/", set())  # nothing to compare
    assert verify_link("PlayStation Store", "https://store.playstation.com/en-us/concept/1", set())
    assert not verify_link("PlayStation Store", "https://playstation.com.evil.test/x", set())
    assert not verify_link("PlayStation Store", "https://notplaystation.com/x", set())
    assert verify_link("Nintendo eShop", "https://www.nintendo.co.uk/Games/x", set())
    assert not verify_link("Xbox store", "http://[broken/", set())  # a malformed address is never verified


def test_an_igdb_game_linked_to_two_catalog_apps_opens_neither(tmp_path):
    conn = store.initialize(tmp_path / "links.sqlite3")
    try:
        store.save_steam_igdb_links(conn, {101: ("linked", 7), 102: ("linked", 7), 103: ("linked", 8),
                                           104: ("none", None)}, now="2026-11-19T12:00:00+00:00")
        assert store.catalog_links_by_igdb(conn, [101, 102, 103, 104]) == {8: 103}  # 7 is ambiguous
        assert store.catalog_links_by_igdb(conn, [101, 103]) == {7: 101, 8: 103}  # only visible apps count
        row = {"igdb_id": 7, "steam_match": "needs_review", "steam_appid": None}
        assert upcoming.game_key_for(row, {101, 102}, store.catalog_links_by_igdb(conn, [101, 102])) == "igdb-7"
        assert upcoming.game_key_for({**row, "igdb_id": 8}, {103}, {8: 103}) == "103"
    finally:
        conn.close()


# ---------------------------------------------------------------------- a refresh against fake IGDB, Twitch and Steam


def launch_day() -> dict:
    """A console-only game without a Steam app, out on RELEASE_DAY, with one verifiable official website."""
    game = igdb_game(201, "Launch Day", entry("PlayStation 5", day=RELEASE_DAY), entry("Xbox Series X|S", day=RELEASE_DAY),
                     platforms=("PlayStation 5", "Xbox Series X|S"))
    game["involved_companies"] = [{"company": {"name": "Big Studio", "websites": [
        {"type": 1, "url": "https://www.bigstudio.example/"}]}, "publisher": True, "developer": False}]
    game["websites"] = [{"type": 1, "url": "https://launch.bigstudio.example/"},
                        {"type": 23, "url": "https://store.playstation.com/en-us/concept/1"},
                        {"type": 22, "url": "https://xbox.example/launch"}]  # not Xbox's domain
    return game


@pytest.fixture
def world(games, fake_steam):
    api = FakeIgdbApi()
    api.games = {
        201: launch_day(),
        202: igdb_game(202, "Split Release", entry("PC", day=date(2026, 10, 15)),
                       entry("PlayStation 5", day=date(2026, 10, 15)), entry("Xbox Series X|S", fmt=7), steam=[600],
                       platforms=("PC", "PlayStation 5", "Xbox Series X|S")),
        203: igdb_game(203, "Far Future", entry("PC", fmt=2, y=2028), platforms=("PC", "Mac")),
        204: igdb_game(204, "Old Classic", entry("PC", day=date(2015, 5, 19))),
        205: igdb_game(205, "Mystery Game", platforms=()),
        206: igdb_game(206, "Game 01", entry("PC", day=date(2024, 1, 1)), steam=[101, 999]),  # two Steam apps
        207: igdb_game(207, "Expansion Pack", entry("PC", day=date(2026, 1, 1)), game_type=1),
    }
    api.popularity[2] = [{"game_id": igdb_id, "value": value, "calculated_at": 1795050874}
                         for igdb_id, value in ((201, 0.003), (203, 0.002), (202, 0.001))]
    api.popularity[10] = [{"game_id": 203, "value": 0.0004}]
    api.externals = [{"game": 206, "uid": "101"}]  # IGDB's Steam record of catalog app 101
    fake_steam.store_summaries = {600: store_summary("Split Release")}
    helix = FakeHelix()
    helix.top = [{"id": "509658", "name": "Just Chatting", "igdb_id": ""},
                 {"id": "1001", "name": "Launch Day", "igdb_id": "201"},
                 {"id": "1002", "name": "Game 01", "igdb_id": "206"},
                 {"id": "1003", "name": "Old Classic: Online", "igdb_id": "204"},
                 {"id": "1004", "name": "Mystery Game", "igdb_id": "205"},
                 {"id": "1005", "name": "Expansion Pack", "igdb_id": "207"},
                 {"id": "1006", "name": "Unknown Thing", "igdb_id": "999"}]  # IGDB returns nothing for 999
    return api, helix


def refresh(settings, world, fake_steam, moment: datetime) -> upcoming.RefreshReport:
    api, helix = world
    conn = store.initialize(settings.database)
    try:
        with api.client() as client:
            return upcoming.refresh_upcoming(conn, client, fake_steam.client(), CuratedData(), catalog_appids=CATALOG,
                                             twitch_client=helix.client(), log=lambda message: None,
                                             now=lambda: moment)
    finally:
        conn.close()


def site(settings, day: date, moment: datetime) -> TestClient:
    """The website on `day` at `moment`; a page that calls Twitch fails the test (NoTwitch)."""
    return TestClient(create_app(configured(settings), today=lambda: day, now=lambda: moment,
                                 twitch_client=NoTwitch()))


def part(html: str, start_id: str, end_marker: str) -> str:
    start = html.index(f'id="{start_id}"')
    return html[start:html.index(end_marker, start)]


def test_a_game_moves_from_upcoming_to_recent_on_its_release_day_with_the_same_key(world, settings, fake_steam):
    eve = RELEASE_DAY - timedelta(days=1)
    assert refresh(settings, world, fake_steam, at(eve)).status() == "success"
    before = site(settings, eve, at(eve, 13))
    upcoming_part = part(before.get("/trending").text, "upcoming", 'id="now"')
    assert "Launch Day Announced for PlayStation 5, Xbox Series X|S · 19 Nov 2026" in text_of(upcoming_part)
    assert 'data-wishlist-toggle data-key="igdb-201"' in before.get("/games/igdb-201/panel").text
    # On the release day, before any new refresh, the next page load already moves it.
    early_page = site(settings, RELEASE_DAY, at(RELEASE_DAY, 1)).get("/trending").text
    assert 'data-details="igdb-201"' not in part(early_page, "upcoming", 'id="now"')
    recent = text_of(part(early_page, "recent-title", 'id="twitch-title"'))
    assert "Launch Day Available on PlayStation 5, Xbox Series X|S since 19 Nov 2026" in recent
    # The release-day refresh keeps one record and one signal history: nothing is duplicated.
    assert refresh(settings, world, fake_steam, at(RELEASE_DAY)).status() == "success"
    conn = store.connect(settings.database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM igdb_games WHERE igdb_id = 201").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM popularity_snapshots WHERE signal = 'want_to_play' "
                            "AND igdb_id = 201").fetchone()[0] == 2  # upcoming, then recent: one history
    finally:
        conn.close()
    after = site(settings, RELEASE_DAY, at(RELEASE_DAY, 13))
    html = after.get("/trending").text
    assert set(re.findall(r'data-details="([^"]+)">Launch Day<', html)) == {"igdb-201"}  # both lists, one key
    assert 'data-wishlist-toggle data-key="igdb-201"' in after.get("/games/igdb-201/panel").text  # still saved
    want = text_of(part(html, "upcoming-want_to_play-title", 'id="upcoming-steam_wishlists-title"'))
    assert "Far Future Announced for PC · 2028 Listed without a release date: Mac" in want
    assert "Split Release Available on PC, PlayStation 5 since 15 Oct 2026 Announced for Xbox Series X|S · TBA" in recent


def test_most_watched_on_twitch_lists_games_in_twitchs_order_with_their_platforms(world, settings, fake_steam):
    refresh(settings, world, fake_steam, at(RELEASE_DAY))
    html = site(settings, RELEASE_DAY, at(RELEASE_DAY, 13)).get("/trending").text
    watched = part(html, "twitch-title", 'id="steam"')
    # Game 01's IGDB record (two Steam apps, so no verified match) is catalog app 101 by IGDB's own Steam record.
    assert re.findall(r'data-details="([^"]+)"', watched) == ["igdb-201", "101", "igdb-204", "igdb-205"]
    text = text_of(watched)
    listed = text_of(watched[watched.index("<ol"):])
    assert "Just Chatting" not in listed and "Expansion Pack" not in listed and "Unknown Thing" not in listed
    assert ("Left out at this check: 1 that neither Twitch nor IGDB links to a game (such as Just Chatting), 1 that "
            "IGDB lists as DLC, bundles or other kinds, 1 whose IGDB record was not read yet.") in text
    assert "Twitch's list: #2 · Watch on Twitch" in text and "Twitch's list: #4 as “Old Classic: Online” ·" in text
    assert "Old Classic Available on PC since 19 May 2015" in text
    assert "Mystery Game Platforms: not known (IGDB lists none for this game)" in text  # missing platform data
    assert 'href="https://www.twitch.tv/directory/game/Launch%20Day"' in watched  # a clear path to the source
    assert "Twitch was asked 19 Nov 2026, 12:00 UTC" in text and "no viewer numbers" in text
    assert "https://cdn.example.invalid/101/header.jpg" in watched  # a catalog game without an IGDB cover


def test_console_only_and_partly_released_games_have_honest_details_and_one_identity(world, settings, fake_steam):
    refresh(settings, world, fake_steam, at(RELEASE_DAY))
    client = site(settings, RELEASE_DAY, at(RELEASE_DAY, 13))
    console = client.get("/games/igdb-201").text
    text = text_of(console)
    assert "Released · not in the Steam catalog" in text
    assert "Platforms Available on PlayStation 5, Xbox Series X|S since 19 Nov 2026" in text
    assert "on PC" not in text and "for PC" not in text and "View on Steam" not in text
    assert 'class="button button-primary button-large" href="https://launch.bigstudio.example/"' in console
    assert text.index("PlayStation Store") < text.index("Also listed by IGDB, not verified: Xbox store")
    assert "Steam: no Steam store page is known for this game." in text
    for heading in ("Price on Steam", "Steam reviews", "Verified awards", "More games like this"):
        assert f">{heading}</h3>" not in console  # no Steam data without a verified Steam game
    split = text_of(client.get("/games/igdb-202/panel").text)
    assert "Released on some platforms · not in the Steam catalog" in split
    assert "Available on PC, PlayStation 5 since 15 Oct 2026 Announced for Xbox Series X|S · TBA" in split
    assert "View on Steam" in split  # IGDB's only Steam app for it has the same name on Steam
    merged = client.get("/games/igdb-206/panel")
    assert merged.status_code == 200 and 'data-key="101" data-aliases="igdb-206"' in merged.text
    catalog = text_of(client.get("/games/101").text)
    assert "Steam platforms Windows · Linux" in catalog
    assert "All platforms Available on PC since 1 Jan 2024 From IGDB.com, checked 19 Nov 2026" in catalog
    assert "Price on Steam" in catalog and "Steam reviews" in catalog  # the catalog details keep every section
    conn = store.connect(settings.database)
    try:
        keys = {target.key for target in cli._critic_targets(conn, CuratedData())}
    finally:
        conn.close()
    assert "101" in keys and "igdb-206" not in keys  # the catalog game stands for its IGDB record
    assert {"igdb-201", "igdb-202", "igdb-204"} <= keys and "igdb-203" not in keys


def test_cross_platform_lists_and_steam_activity_stay_apart(world, settings, fake_steam):
    refresh(settings, world, fake_steam, at(RELEASE_DAY))
    html = site(settings, RELEASE_DAY, at(RELEASE_DAY, 13)).get("/trending").text
    cross = part(html, "upcoming", 'id="steam"')
    steam = html[html.index('id="steam"'):html.index("</main>")]
    assert cross.count('<span class="scope-badge">All platforms</span>') == 5
    assert ('Most wishlisted on Steam</h3><span class="scope-badge is-steam">Steam only</span>' in cross)
    assert 'Steam activity</h2><span class="scope-badge is-steam">Steam only</span>' in steam
    assert "no number here is all of a game's players" in text_of(steam)
    assert "players in game" not in text_of(cross)  # no Steam player count in a cross-platform list
    assert "Launch Day" not in steam  # a console-only game never appears in Steam activity
    nav = re.search(r'<nav class="view-switch trend-jump".*?</nav>', html, re.S).group(0)
    assert text_of(nav) == "Upcoming & Anticipated Trending now & recently released Steam activity"


def test_twitch_failures_keep_the_last_list_for_24_hours_only(world, settings, fake_steam):
    api, helix = world
    refresh(settings, world, fake_steam, at(RELEASE_DAY))
    next_day = RELEASE_DAY + timedelta(days=1)
    helix.statuses = [503, 503]
    report = refresh(settings, world, fake_steam, at(next_day, 10))
    assert report.status() == "partial" and report.errors[0].startswith("Twitch:")
    helix.statuses = [429]  # a rate limit is not retried
    assert refresh(settings, world, fake_steam, at(next_day, 11)).status() == "partial"
    kept = site(settings, next_day, at(next_day, 11)).get("/trending").text
    assert "Twitch was asked 19 Nov 2026, 12:00 UTC" in text_of(kept)
    assert 'data-details="igdb-201"' in part(kept, "twitch-title", 'id="steam"')
    assert "was partial: Twitch:" in text_of(kept)
    api.down = True  # IGDB fails first: Twitch is not asked, and the saved lists stay
    assert refresh(settings, world, fake_steam, at(next_day, 11)).status() == "failed"
    assert "Launch Day" in site(settings, next_day, at(next_day, 11)).get("/trending").text
    later = site(settings, next_day, at(next_day, 13)).get("/trending").text  # 25 hours after Twitch answered
    assert "No Twitch list is saved from the last 24 hours" in text_of(later)
    conn = store.connect(settings.database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM twitch_top_games").fetchone()[0] == 0  # deleted, not just hidden
    finally:
        conn.close()


def test_empty_answers_and_a_site_without_igdb_are_explained(world, settings, fake_steam):
    api, helix = world
    helix.top = []
    report = refresh(settings, world, fake_steam, at(RELEASE_DAY))
    assert report.status() == "success" and report.counts["twitch_categories"] == 0
    text = text_of(site(settings, RELEASE_DAY, at(RELEASE_DAY, 13)).get("/trending").text)
    assert "No Twitch list is saved from the last 24 hours" in text
    api.popularity[2] = []  # an empty PopScore answer saves nothing: the last good list stays, with its own date
    assert refresh(settings, world, fake_steam, at(RELEASE_DAY, 14)).counts["want_to_play_values"] == 0
    kept = site(settings, RELEASE_DAY, at(RELEASE_DAY, 15)).get("/trending").text
    assert 'data-details="igdb-201"' in part(kept, "recent-title", 'id="twitch-title"')
    assert "saved here 19 Nov 2026, 12:00 UTC" in text_of(part(kept, "recent-title", 'id="twitch-title"'))
    years_later = date(2029, 1, 5)  # every saved game is out, and none came out in the last 90 days
    empty = text_of(site(settings, years_later, at(years_later)).get("/trending").text)
    assert "None of the games saved by the last refresh is still upcoming." in empty
    assert "No game in the newest snapshot (19 Nov 2026, 12:00 UTC) was first released in the last 90 days." in empty
    plain = TestClient(create_app(settings, today=lambda: RELEASE_DAY, twitch_client=NoTwitch())).get("/trending")
    assert "These lists come from IGDB and Twitch, which this site cannot reach yet" in text_of(plain.text)


# ---------------------------------------------------------------------- navigation


def test_discover_is_the_first_tab_and_the_home_page_and_trending_is_second(games, settings):
    client = TestClient(create_app(settings, today=lambda: RELEASE_DAY, twitch_client=NoTwitch()))
    for path in ("/", "/trending", "/stories", "/dashboard", "/wishlist"):
        nav = re.search(r'<nav class="site-nav".*?</nav>', client.get(path).text, re.S).group(0)
        assert re.findall(r'href="([^"]+)"', nav) == ["/", "/trending", "/stories", "/dashboard", "/wishlist"]
        assert re.findall(r'href="([^"]+)" aria-current="page"', nav) == [path]
    home = client.get("/").text
    assert '<h1 id="discover-title">Discover.</h1>' in home and '<a class="wordmark" href="/">' in home
    assert client.get("/?view=all&page=2").status_code == 200  # existing catalog links keep working
