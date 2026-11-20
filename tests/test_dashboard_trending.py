"""Dashboard → Trending games (version 0.10): the first and default dashboard page. The Trending page's own lists,
with the same games, rules and timestamps, compared game by game: IGDB's Want to Play values as bars against each
list's highest value, Steam's most-wishlisted order and Twitch's order never drawn as amounts (their analyses since
0.12 are in test_list_analysis.py), Steam data
only for games that have it (unavailable, never zero), observation times kept apart, source-specific measures, the
detailed definitions folded under "Sources and methodology", and honest gaps when data or history is missing.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114) and the seeded IGDB and Twitch data of
test_web_upcoming.py and test_trending_cross_platform.py. All names and numbers are made up."""

import re
from datetime import date, datetime, timedelta, timezone
from html import unescape

from fastapi.testclient import TestClient

from game_librarian import dashboard, trending
from game_librarian.catalog import CatalogGame
from game_librarian.upcoming import POSITION, SHARE, STEAM_WISHLISTS, WANT_TO_PLAY, availability
from game_librarian.web import create_app
from tests.test_trending_cross_platform import RELEASE_DAY, at, dates_of, part, refresh, site, world  # noqa: F401
from tests.test_upcoming import entry
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_discovery import save_players
from tests.test_web_upcoming import NOW, TODAY, NoTwitch, configured, save_upcoming

DAY = date(2026, 11, 19)


def item(key, name="Game", av=None, *, steam_verified=False, **facts) -> dict:
    """A Trending list item as web._igdb_item builds it, with only what the dashboard reads."""
    return {"key": key, "name": name, "details_url": f"/games/{key}", "availability": av,
            "steam_verified": steam_verified, **facts}


def catalog_game(appid, *, positive=None, total=None, players=None, players_at=None) -> CatalogGame:
    return CatalogGame(appid=appid, sort_name=f"game {appid}", labels=(), awards=(), positive=positive, total=total,
                       released_on=None, upcoming=False, in_window=False, rank=None, players=players,
                       players_at=players_at)


def dashboard_client(settings, *, today=TODAY, now=NOW) -> TestClient:
    return TestClient(create_app(configured(settings), today=lambda: today, now=lambda: now,
                                 twitch_client=NoTwitch()))


def details_keys(html: str) -> list[str]:
    return re.findall(r'data-details="([^"]+)"', html)


def chart(html: str, title_id: str) -> str:
    """One chart card (a <figure>) of the dashboard, by its title's id."""
    start = html.rindex("<figure", 0, html.index(f'id="{title_id}"'))
    return html[start:html.index("</figure>", start)]


def methodology(html: str) -> str:
    """The collapsed "Sources and methodology" part."""
    return re.search(r'<details class="method-details" id="sources">.*?</details>\s*<dialog', html, re.S).group(0)


def steam_table(html: str) -> str:
    start = html.index(">Steam data of the cross-platform games<")
    return html[start:html.index("</table>", start)]


def steam_rows(html: str) -> dict[str, str]:
    """The Steam data table's rows as text, by game key."""
    rows = re.findall(r'<tr role="row">\s*<td role="cell" data-label="Game"><a class="text-link" '
                      r'href="/games/([^"]+)">(.*?)</tr>', steam_table(html), re.S)
    return {key: text_of(row) for key, row in rows}


def bar_rows(chart_html: str) -> list[tuple[str, str, str]]:
    """(game key, bar width, printed value) for each row of a ranked bar chart, in order."""
    rows = re.findall(r'<li class="game-bar">.*?</li>', chart_html, re.S)
    found = []
    for row in rows:
        key = re.search(r'data-details="([^"]+)"', row).group(1)
        width = re.search(r'class="gb-bar" style="width: ([\d.]+)%"', row)
        value = re.search(r'<span class="gb-value">([^<]*)', row)
        found.append((key, width.group(1) if width else "", value.group(1) if value else ""))
    return found


# ---------------------------------------------------------------------- calculations (pure)


def test_platforms_count_each_game_once_per_platform_in_its_furthest_state():
    out_and_announced = availability(dates_of(entry("PC", day=date(2026, 10, 1)), entry("PlayStation 5", fmt=7)),
                                     ["PC", "PlayStation 5", "Nintendo Switch 2"], DAY)
    early = availability(dates_of(entry("PC", day=date(2025, 1, 1), status=3), entry("PC", day=date(2027, 3, 1))),
                         ["PC"], DAY)
    assert dashboard.platform_states(out_and_announced) == {"Nintendo Switch 2": "undated",
                                                           "PlayStation 5": "announced", "PC": "out"}
    assert dashboard.platform_states(early) == {"PC": "early"}  # early access and announced: counts as early access
    assert dashboard.platform_states(None) == {}
    items = [item("igdb-1", av=out_and_announced), item("igdb-2", av=early), item("igdb-3", av=None)]
    rows = dashboard.platform_rows(items)
    assert [(row["platform"], row["games"], row["out"], row["early"], row["announced"], row["undated"])
            for row in rows] == [("PC", 2, 1, 1, 0, 0), ("Nintendo Switch 2", 1, 0, 0, 0, 1),
                                 ("PlayStation 5", 1, 0, 0, 1, 0)]
    assert sum(row["games"] for row in rows) == 4 > 3 - 1  # overlap: more platform counts than games with platforms
    assert rows[0]["width"] == round(2 / 3 * 100, 2)  # a share of the list's games, not of the largest bar


def test_release_status_and_date_precision_follow_igdbs_dates_and_say_when_they_are_unknown():
    out = availability(dates_of(entry("PC", day=date(2026, 10, 1))), ["PC"], DAY)
    partly = availability(dates_of(entry("PC", day=date(2026, 10, 1)), entry("PlayStation 5", fmt=7)),
                          ["PC", "PlayStation 5"], DAY)
    unknown = availability([], [], DAY)
    rows = dashboard.status_rows([item("a", av=out), item("b", av=out), item("c", av=partly), item("d", av=unknown),
                                  item("e", av=None)])
    assert [(row["status"], row["games"]) for row in rows] == [
        ("Out now", 2), ("Out on some platforms, more announced", 1), ("Platforms not known", 1),
        ("No IGDB record", 1)]

    class State:  # upcoming.ReleaseState: only `first` is read
        def __init__(self, first):
            self.first = first

    day, month, tba = (dates_of(entry("PC", day=date(2027, 2, 23)))[0], dates_of(entry("PC", fmt=1, y=2027, m=3))[0],
                       dates_of(entry("PC", fmt=7))[0])
    precision = dashboard.date_precision_rows([item("a", state=State(day)), item("b", state=State(month)),
                                               item("c", state=State(tba)), item("d", state=State(None))])
    assert [(row["precision"], row["games"]) for row in precision] == [
        ("Exact day", 1), ("Month only", 1), ("To be announced", 1), ("No full-release date listed", 1)]


def test_relative_bars_index_each_value_against_the_lists_highest_and_never_invent_a_zero():
    # IGDB's Want to Play values are relative shares: only the ratio between games of one list is meaningful.
    rows = dashboard.relative_bars([item("a", value=0.004), item("b", value=0.003), item("c", value=0.001),
                                    item("d", value=None), item("e", value=0.00002), item("f", value=0.0)])
    assert [(row["rank"], row["index_text"], row["width"]) for row in rows] == [
        (1, "100", 100.0), (2, "75", 75.0), (3, "25", 25.0),
        (4, "", 0.0),  # no value: no number and no bar, never "0"
        (5, "<1", 0.5),  # a tiny share is "<1", not rounded down to a zero
        (6, "0", 0.0)]  # IGDB's own value of 0 is shown as it is
    assert rows[3]["index"] is None and rows[0]["value_text"] == "0.004000"
    assert [row["index"] for row in dashboard.relative_bars([item("a", value=None)])] == [None]
    assert dashboard.relative_bars([]) == []


def test_the_signals_say_whether_their_values_are_shares_or_positions():
    assert WANT_TO_PLAY.measure == SHARE  # a relative share: bars may compare the values
    assert STEAM_WISHLISTS.measure == POSITION  # values that only step down one place at a time: no bars


def test_change_bars_scale_every_rise_to_the_largest():
    start, end = datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 9, tzinfo=timezone.utc)
    items = [{"appid": 1, "change": trending.Change(1, before=200, after=1_000, before_at=start, after_at=end)},
             {"appid": 2, "change": trending.Change(2, before=50, after=250, before_at=start, after_at=end)}]
    assert [row["width"] for row in dashboard.change_bars(items)] == [100.0, 25.0]  # rises of 800 and 200
    assert dashboard.change_bars([]) == []


def test_steam_facts_exist_only_for_catalog_games_and_missing_data_is_never_zero():
    catalog = {101: catalog_game(101, positive=900, total=1000, players=None),
               102: catalog_game(102, positive=50, total=60, players=7, players_at="2026-11-19T12:00:00+00:00")}
    reviews_at = {101: "2026-11-19T11:00:00+00:00"}
    stored = dashboard.steam_facts(item("101"), catalog, reviews_at)
    assert stored["status"] == "stored" and stored["players"] is None  # no count stays None, not 0
    assert (stored["split"]["positive"], stored["split"]["negative"], stored["reviews_at"]) == (900, 100,
                                                                                               reviews_at[101])
    assert dashboard.steam_facts(item("igdb-5", steam_verified=True), catalog, {}) == {"status": "not stored"}
    assert dashboard.steam_facts(item("igdb-6"), catalog, {}) == {"status": "none"}  # console-only or unmatched
    games = dashboard.cross_platform_games(
        [("List A", [item("igdb-6", "Console Game"), item("102", "Steam Game")]),
         ("List B", [item("102", "Steam Game"), item("igdb-7", "Other")])], catalog, {})
    assert [(game["key"], game["lists"]) for game in games] == [("igdb-6", ["List A"]), ("102", ["List A", "List B"]),
                                                               ("igdb-7", ["List B"])]  # one row per game


def test_player_bars_keep_each_games_own_time_and_are_never_summed():
    items = [{"appid": appid, "name": f"Game {appid}", "game": catalog_game(appid, players=players, players_at=moment)}
             for appid, players, moment in ((1, 2_000, "2026-11-19T06:00:00+00:00"),
                                            (2, 500, "2026-11-19T09:30:00+00:00"))]
    bars = dashboard.steam_player_bars(items)
    assert [(bar["appid"], bar["players"], bar["players_at"], bar["width"]) for bar in bars] == [
        (1, 2_000, "2026-11-19T06:00:00+00:00", 100.0), (2, 500, "2026-11-19T09:30:00+00:00", 25.0)]


def test_a_sparkline_needs_snapshots_at_least_a_change_window_apart():
    end = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    game = {"appid": 1, "name": "Game 1", "game": catalog_game(1, players=300, players_at=end.isoformat())}
    hours = {1: [{"captured_at": (end - timedelta(hours=6)).isoformat(), "players": 100},
                 {"captured_at": end.isoformat(), "players": 300}]}
    assert dashboard.player_sparklines([game], hours) == []  # one day's swing is not a trend
    days = {1: [{"captured_at": (end - timedelta(days=8)).isoformat(), "players": 100},
                {"captured_at": end.isoformat(), "players": 300}]}
    assert len(dashboard.player_sparklines([game], days)) == 1


def test_twitch_coverage_divides_twitchs_categories_and_places_the_listed_games():
    twitch_list = {"categories": 7, "depth": 100, "games": [item("igdb-1", "A", twitch_position=2),
                                                            item("101", "B", twitch_position=3)],
                   "left_out": {"no_igdb_game": 2, "not_read": 1, "other_kind": 1}}
    coverage = dashboard.twitch_coverage(twitch_list)
    assert [(part["name"], part["count"]) for part in coverage["parts"]] == [
        ("Games on Trending’s list", 2), ("Other games, below the list", 1), ("DLC, bundles and other kinds", 1),
        ("Not a game (such as Just Chatting)", 2), ("Game not read from IGDB yet", 1)]
    assert sum(part["count"] for part in coverage["parts"]) == 7
    assert [(dot["position"], dot["x"]) for dot in coverage["positions"]] == [(2, 1.5), (3, 2.5)]
    assert dashboard.twitch_coverage({**twitch_list, "categories": 0, "games": []}) is None


# ---------------------------------------------------------------------- navigation and old addresses


def test_trending_games_is_the_first_and_default_dashboard_page(games, settings):
    client = dashboard_client(settings)
    html = client.get("/dashboard").text
    assert "<title>Trending games | Dashboard | Game Librarian</title>" in html
    tabs = re.search(r'<nav class="view-switch" aria-label="Dashboard pages">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'href="([^"]+)"', tabs) == ["/dashboard", "/dashboard/reception", "/dashboard/awards",
                                                   "/dashboard/about"]
    assert text_of(tabs) == "Trending games Player reception Awards About the data"
    assert 'href="/dashboard" aria-current="page">Trending games</a>' in tabs
    for path, title in (("/dashboard/reception", "Player reception"), ("/dashboard/awards", "Awards"),
                        ("/dashboard/about", "About the data")):  # the other pages stay one click away
        page = client.get(path)
        assert page.status_code == 200 and f"<title>{title} | Dashboard | Game Librarian</title>" in page.text
    site_nav = re.search(r'<nav class="site-nav".*?</nav>', html, re.S).group(0)
    assert 'href="/dashboard" aria-current="page">Dashboard</a>' in site_nav


def test_old_dashboard_addresses_keep_working(games, settings):
    client = dashboard_client(settings)
    moved = client.get("/dashboard/trending", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/dashboard"
    assert "<title>Trending games | Dashboard" in client.get("/dashboard/trending").text  # a bookmark still lands
    # Before 0.10, /dashboard was Player reception: its filter links now open that page, filters kept.
    old = client.get("/dashboard?tag=Indie&award_status=Nominee", follow_redirects=False)
    assert old.status_code == 307 and old.headers["location"] == "/dashboard/reception?tag=Indie&award_status=Nominee"
    reception = client.get("/dashboard?tag=Indie").text
    assert "<title>Player reception | Dashboard" in reception and "7 of 14 catalog games match the filters." in reception
    assert '<form class="dash-filters" method="get" action="/dashboard/reception"' in reception
    legacy = client.get("/dashboard", params={"label": "Unclassified"}).text  # a pre-0.8 label link, too
    assert '<option value="No verified label" selected>' in legacy
    assert "<title>Trending games" in client.get("/dashboard?unknown=1").text  # other parameters do not redirect


# ---------------------------------------------------------------------- the page


def test_the_dashboard_charts_exactly_the_games_of_the_trending_lists(games, settings):
    save_upcoming(settings)
    # After the fixture's sync (12:00 UTC), whose snapshots have no count: the newest snapshot of a game counts.
    save_players(settings, {114: 4_000, 113: 1_500}, moment=NOW - timedelta(minutes=50))
    save_players(settings, {112: 900}, moment=NOW - timedelta(minutes=20))
    client = dashboard_client(settings)
    trending_page = client.get("/trending").text
    board = client.get("/dashboard").text
    cross = part(trending_page, "upcoming", 'id="steam"')
    charted = part(board, "upcoming", 'id="steam"')
    assert set(details_keys(charted)) == set(details_keys(cross)) == {"igdb-1", "igdb-2", "igdb-3", "101"}
    assert set(steam_rows(board)) == set(details_keys(cross))
    popular = part(trending_page, "popular-title", 'id="this-year-title"')
    assert details_keys(chart(board, "chart-players")) == details_keys(popular) == ["114", "113", "112"]
    sources = text_of(methodology(board))
    assert "Games in the cross-platform lists 4 in 4 lists · 2 in more than one" in sources
    assert "Moon Garden: Most wanted on IGDB, Most wishlisted on Steam" in sources  # overlap is explained
    assert "Hand-picked (curated) games: none." in sources
    # Every game opens its details in the dialog, as on Trending.
    assert '<dialog class="game-dialog" id="game-dialog"' in board


def test_want_to_play_is_drawn_as_relative_bars_and_the_upcoming_games_on_the_release_radar(games, settings):
    save_upcoming(settings)
    board = dashboard_client(settings).get("/dashboard").text
    want = chart(board, "chart-want_to_play")
    # Values 0.004, 0.003 and 0.001: an index of the list's highest, never counts.
    assert bar_rows(want) == [("igdb-1", "100.0", "100"), ("igdb-2", "75.0", "75"), ("101", "25.0", "25")]
    assert "Bars compare IGDB’s relative values, with the first game at 100: not counts of people." in text_of(want)
    assert "Moon Garden Announced for PC, PlayStation 5 · 23 Feb 2027" in text_of(want)  # platforms beside the game
    table = text_of(re.search(r"<table.*?</table>", want, re.S).group(0))
    assert "1 Moon Garden 100 0.004000 7 Announced for PC, PlayStation 5 · 23 Feb 2027" in table  # IGDB's own value
    assert "IGDB PopScore type 2 · Saved 9 Nov 2026, 12:00 UTC; IGDB calculated it 9 Nov 2026, 03:00 UTC." in want
    # Steam's list is not repeated as ranked tiles: its games, with Want to Play's, are placed on the release radar.
    assert 'class="rank-tile"' not in board[:board.index('id="chart-both-lists"')]
    radar = chart(board, "chart-radar")
    assert "gb-bar" not in radar and "game-bar" not in radar  # positions are never drawn as amounts
    assert details_keys(radar)[:2] == ["igdb-1", "igdb-1"]  # Moon Garden's PC and PlayStation points (23 Feb 2027)
    assert sorted(set(details_keys(radar))) == ["101", "igdb-1", "igdb-2"]  # Already Out is out: not upcoming
    side = text_of(radar[radar.index('id="radar-approximate"'):])
    assert "Mar 2027 Game 01 Saga TBA Console Quest" in side  # a month and a TBA are listed, never placed
    assert "IGDB PopScore types 2 and 10" in radar
    # Each chart keeps to its own source: no Steam players, reviews or Twitch order inside an IGDB chart.
    for card in (want, radar):
        assert not re.search(r"players|positive|negative|Twitch", text_of(card)), card[:80]


def test_covers_are_small_labeled_by_the_name_and_fall_back_to_a_gray_box(games, settings):
    save_upcoming(settings)
    want = chart(dashboard_client(settings).get("/dashboard").text, "chart-want_to_play")
    rows = dict(zip(("igdb-1", "igdb-2", "101"), re.findall(r'<li class="game-bar">.*?</li>', want, re.S)))
    assert ('<span class="thumb" aria-hidden="true"><img src="https://images.igdb.com/igdb/image/upload/t_cover_big/'
            'co1abc.jpg" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" data-image-fallback>'
            '</span>') in rows["igdb-1"]  # IGDB's cover; the name beside it is the label
    assert '<span class="thumb is-empty" aria-hidden="true"></span>' in rows["igdb-2"]  # no cover: a plain box
    assert "Console Quest" in text_of(rows["igdb-2"])


def test_the_lists_keep_their_own_sources_and_observation_times(games, settings):
    save_upcoming(settings)
    save_players(settings, {114: 4_000}, moment=NOW - timedelta(minutes=50))  # after the fixture's 12:00 sync
    save_players(settings, {113: 1_500}, moment=NOW - timedelta(minutes=20))
    board = dashboard_client(settings).get("/dashboard").text
    sources = text_of(methodology(board))
    assert "Lists, measures and update times" in sources
    assert "Most wanted on IGDB Additions to IGDB.com users’ Want to Play lists: a relative share, not a count" in sources
    assert "IGDB PopScore, type 10 (from Steam)" in sources
    assert "Saved 9 Nov 2026, 12:00 UTC; IGDB calculated it 9 Nov 2026, 03:00 UTC" in sources
    assert "Most watched on Twitch Twitch’s order of categories by current viewers: order only, no viewer numbers" \
        in sources and "No list from the last 24 hours" in sources
    assert "Player snapshots 9 Nov 2026, 12:10 UTC – 9 Nov 2026, 12:40 UTC" in sources  # two moments, not one
    # Each chart keeps a short, visible source and time.
    players = chart(board, "chart-players")
    assert "Steam Web API) · snapshots 9 Nov 2026, 12:10 UTC – 9 Nov 2026, 12:40 UTC." in text_of(players)
    assert "Twitch API, Get Top Games · no list from the last 24 hours." in text_of(chart(board, "chart-twitch"))
    steam = text_of(board[board.index('id="dash-steam-title"'):board.index('id="dash-changes-title"')])
    assert "Player counts are not added up, because each was taken at its own moment." in steam
    assert "Steam data only" in steam and "people playing on consoles, in other PC stores or offline are not counted" \
        in steam  # Steam players are never presented as all of a game's players
    assert "5,500" not in steam and "Steam players in these games" not in board  # no total of the two snapshots
    assert 'title="Game 14: 4,000 players at 9 Nov 2026, 12:10 UTC"' in board
    assert 'title="Game 13: 1,500 players at 9 Nov 2026, 12:40 UTC"' in board
    assert [value for _, _, value in bar_rows(players)] == ["4K", "1.5K"]
    assert "IGDB" not in text_of(players) and "Twitch" not in text_of(players)  # Steam's own measure only


def test_platform_counts_and_release_precision_are_in_the_collapsed_methodology(games, settings):
    save_upcoming(settings)
    board = dashboard_client(settings).get("/dashboard").text
    main = board[:board.index('id="sources"')]
    assert "announced platforms" not in main and "How precisely" not in main  # not the main charts any more
    method = methodology(board)
    assert method.startswith('<details class="method-details" id="sources">\n  <summary>Sources and methodology'
                             '</summary>')
    want = method[method.index(">Most wanted on IGDB: announced platforms</h4>"):
                  method.index(">Most wishlisted on Steam: announced platforms</h4>")]
    platforms = re.findall(r'<dt>([^<]+)</dt>\s*<dd class="bar-line">.*?<span class="bar-value">(\d+)</span>', want, re.S)
    assert platforms[:2] == [("PC", "2"), ("PlayStation 5", "2")]  # Moon Garden counts for both: 4 for 3 games
    assert "so a list’s platform bars add up to more than its games" in text_of(method)
    assert ("Exact day", "1") in platforms and ("Month only", "1") in platforms and ("To be announced", "1") in platforms
    assert "2 of these 3 games have a verified Steam app." in text_of(want)
    table = re.search(r"<table class=\"data-table\">.*?</table>", want, re.S).group(0)  # the split by state
    assert "<td>PC</td><td class=\"num\">2</td><td class=\"num\">0</td><td class=\"num\">1</td>" in table


def test_missing_steam_data_is_unavailable_never_zero(games, settings):
    save_upcoming(settings)
    board = dashboard_client(settings).get("/dashboard").text
    assert '<details class="table-view steam-facts">\n    <summary>Steam data of the cross-platform games</summary>' in board
    rows = steam_rows(board)
    assert rows["igdb-2"].endswith("Unavailable No verified Steam app (console-only, not on Steam, or not matched)")
    assert rows["igdb-1"].endswith("Unavailable On Steam, but not in the catalog: no Steam data is stored")
    assert "No count (not collected, or Steam reported none)" in rows["101"]  # a catalog game without a count
    assert "100 positive · 950 negative" in rows["101"]  # Game 01's stored Steam reviews
    for key in ("igdb-1", "igdb-2", "igdb-3"):
        assert not re.search(r"\b0 (players|positive)", rows[key])


def test_changes_appear_only_with_dated_snapshots_and_explain_the_gap_otherwise(games, settings):
    save_upcoming(settings)  # two Want to Play snapshots eight days apart, and a noticed date change
    save_players(settings, {114: 4_000})
    board = text_of(dashboard_client(settings).get("/dashboard").text)
    changes = board[board.index("Changes over time Only between snapshots"):board.index("Sources and methodology",
                                                                                        board.index("Changes over time Only"))]
    assert "Most wanted on IGDB: position changes Game Places Before Now Moon Garden +2 #9 · 1 Nov 2026 #7 · 9 Nov 2026" \
        in changes
    assert "Moon Garden PC moved later (Q4 2026 → 23 Feb 2027)" in changes
    # The charts that need more history are named once, with how far the snapshots reach: one is not a trend.
    # Since 0.14 Steam's list follows the same 7-14 day rule as the other lists (0.12-0.13 compared it in its own
    # cards, which the release radar replaced): one Steam snapshot so far, so it waits like the others.
    assert ("Not available yet: Steam players over the last 30 days, most added players on Steam, most new Steam "
            "reviews, Most wishlisted on Steam position changes, Most wanted new releases position changes. Each needs "
            "snapshots of the same game or list at least 7 days apart. Player snapshots: 1 day, from 9 Nov 2026, "
            "12:00 UTC to 9 Nov 2026, 12:00 UTC.") in changes
    assert "IGDB list snapshots: 2 days, from 1 Nov 2026, 13:00 UTC to 9 Nov 2026, 12:00 UTC." in changes
    assert "Most wishlisted on Steam: position changes" not in changes and "previous snapshot" not in changes
    assert "Twitch: no change over time is shown, because Twitch’s data is kept at most 24 hours" in changes
    assert "<polyline" not in board


def test_twitch_is_compared_with_steam_reviews_and_disappears_after_24_hours(world, settings, fake_steam):
    refresh(settings, world, fake_steam, at(RELEASE_DAY))
    board = site(settings, RELEASE_DAY, at(RELEASE_DAY, 13)).get("/dashboard").text
    twitch_chart = chart(board, "chart-twitch")
    assert "Most watched on Twitch, with their Steam reviews" in text_of(twitch_chart)  # Twitch's list, named so
    assert "no viewer numbers" in text_of(twitch_chart) and "Two measures, never combined." in text_of(twitch_chart)
    assert "gb-bar" not in twitch_chart  # a Twitch place is never drawn as an amount
    # Only Game 01 (catalog app 101) has Steam reviews: Launch Day, Old Classic and Mystery Game are not compared.
    assert details_keys(twitch_chart) == ["101"]
    assert ("#3 on Twitch (opens in a new tab) Game 01 9.5% positive · 1,050 Steam reviews · Mixed"
            in text_of(twitch_chart))
    assert 'href="https://www.twitch.tv/directory/game/Game%2001"' in twitch_chart  # a clear path to the source
    assert "1 of the 4 games in Twitch’s first 100 categories has Steam reviews here." in text_of(twitch_chart)
    assert "Twitch API, Get Top Games · Twitch asked 19 Nov 2026, 12:00 UTC; deleted 24 hours later." \
        in text_of(twitch_chart)
    assert "No rank changes: Twitch allows keeping its data for 24 hours" in text_of(twitch_chart)
    method = methodology(board)
    assert "Twitch asked 19 Nov 2026, 12:00 UTC. 4 of the 7 categories are games" in text_of(method)
    parts = dict(re.findall(r'<dt>([^<]+)</dt>\s*<dd class="bar-line">.*?<span class="bar-value">(\d+)</span>',
                            method[method.index("Twitch’s top categories"):], re.S))
    assert {unescape(name): count for name, count in parts.items()} == {
        "Games on Trending’s list": "4", "DLC, bundles and other kinds": "1",
        "Not a game (such as Just Chatting)": "1", "Game not read from IGDB yet": "1"}
    assert re.findall(r'title="#(\d+): ([^"]+)"', method) == [("2", "Launch Day"), ("3", "Game 01"),
                                                             ("4", "Old Classic"), ("5", "Mystery Game")]
    # The recent releases are compared by their Want to Play values: 0.003 and 0.001.
    assert bar_rows(chart(board, "chart-recent")) == [("igdb-201", "100.0", "100"), ("igdb-202", "33.33", "33")]
    rows = steam_rows(board)
    assert rows["igdb-201"].endswith("No verified Steam app (console-only, not on Steam, or not matched)")
    assert "positive" in rows["101"]  # the Twitch list's catalog game has its Steam reviews
    later = site(settings, RELEASE_DAY + timedelta(days=1), at(RELEASE_DAY + timedelta(days=1), 13))
    gone = text_of(later.get("/dashboard").text)
    assert "No Twitch list from the last 24 hours is kept, so nothing is shown." in gone
    assert "No Twitch list from the last 24 hours is kept, so the lists cannot be matched." in gone
    assert "Launch Day" in gone  # still listed by IGDB's lists, without any Twitch data


def test_an_empty_site_explains_every_gap_without_inventing_charts(settings):
    html = TestClient(create_app(settings, today=lambda: TODAY, now=lambda: NOW)).get("/dashboard").text
    board = text_of(html)
    for phrase in ("IGDB is not set up, so there are no cross-platform lists to analyze.",
                   "No Twitch list from the last 24 hours is kept, so nothing is shown.",
                   "The cross-platform lists are empty, so there is nothing to look up on Steam.",
                   "No player counts have been collected yet.", "Player snapshots: none saved yet.",
                   "IGDB list snapshots: none saved yet.", "Not refreshed yet: run python -m game_librarian upcoming."):
        assert phrase in board, phrase
    assert "Games in the cross-platform lists 0" in board
    assert 'class="game-bar"' not in html and 'class="rank-tile"' not in html  # no chart without data
