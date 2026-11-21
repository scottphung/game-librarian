"""The Upcoming Release Radar, the main chart of Trending games → Upcoming releases (versions 0.14-0.15).

The tracked upcoming games (the upcoming games saved from Most wanted on IGDB and Most wishlisted on Steam) are placed
by how soon each is due: the center is today, the rings end 1 week, 1 month, 3 months, 6 months and 1 year later, and
an outer band holds what is due later. Each section is a platform family. Only exact release days are placed; a
month, quarter, year, TBA or no date at all is listed apart, so no day is guessed. A game on several platforms has a
point in each section where it has an exact day, and only its soonest point is a tab stop. Since 0.15 the radar takes
two thirds of its card and a panel beside it shows the selected game (pointing, focusing or tapping selects one;
selecting it again opens its details); time windows end on its rings; the games without an exact day fold under one
counted control.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114); every name and date is made up.
"""

import math
import re
from datetime import date, timedelta

from game_librarian import dashboard, store, upcoming
from game_librarian.config import PACKAGE_DIR
from game_librarian.upcoming import STEAM_WISHLISTS, WANT_TO_PLAY, SignalEntry, availability
from tests.test_compact_pages import CSS, media, rule
from tests.test_dashboard_trending import chart, dashboard_client, details_keys, item, methodology
from tests.test_trending_cross_platform import dates_of, part
from tests.test_upcoming import entry, igdb_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NOW, TODAY, save_upcoming

APP_JS = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def listed(key, name, *entries, platforms=("PC",)):
    """A tracked upcoming game as web._igdb_item builds it, with only what the radar reads."""
    return item(key, name, availability(dates_of(*entries), list(platforms), TODAY))


def tracked(*items, signal=STEAM_WISHLISTS):
    return {"signal": signal, "tracked": list(items), "captured_at": "2026-11-09T12:00:00+00:00"}


def distance(point) -> float:
    """A point's distance from the center, as a share of the radius."""
    return math.hypot(point["x"] - 50, point["y"] - 50) / 50


# ---------------------------------------------------------------------- placement (pure)


def test_platforms_fall_into_five_families_by_igdbs_names():
    assert [dashboard.platform_family(name) for name in (
        "PC", "PlayStation 5", "PlayStation 4", "PlayStation VR2", "Xbox Series X|S", "Xbox One", "Nintendo Switch",
        "Nintendo Switch 2", "Mac", "Linux", "Android", "PC Engine")] == [
        "PC", "PlayStation", "PlayStation", "PlayStation", "Xbox", "Xbox", "Switch", "Switch", "Other", "Other",
        "Other", "Other"]  # "PC" only by its exact name: an old console named "PC Engine" is not a PC


def test_the_rings_end_a_week_a_month_three_and_six_months_and_a_year_after_today():
    assert dashboard.radar_bands(date(2026, 11, 20)) == [
        date(2026, 11, 27), date(2026, 12, 20), date(2027, 2, 20), date(2027, 5, 20), date(2027, 11, 20),
        date(2028, 11, 20)]  # the last one ends the outer band, "Later"
    assert dashboard.add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)  # a shorter month keeps its last day
    assert dashboard.add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert dashboard.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_sooner_is_closer_to_the_center_and_each_day_stays_inside_its_ring():
    today = date(2026, 11, 20)
    ends = dashboard.radar_bands(today)
    days = [today + timedelta(days=number) for number in range(1, 800)]
    distances = [dashboard.radar_distance(day, today, ends) for day in days]
    within = [distance for day, distance in zip(days, distances) if day <= ends[-1]]
    assert all(earlier < later for earlier, later in zip(within, within[1:]))  # a later day is always further out
    assert set(distances[len(within):]) == {(5 + 1 - dashboard.RADAR_EDGE) / 6}  # past two years: the edge

    def band(day: date) -> int:
        share = dashboard.radar_distance(day, today, ends) * 6
        assert share % 1 not in (0.0,), day  # never on a ring line
        return int(share)

    assert [band(day) for day in (date(2026, 11, 21), date(2026, 11, 27), date(2026, 11, 28), date(2026, 12, 20),
                                  date(2026, 12, 21), date(2027, 2, 20), date(2027, 5, 20), date(2027, 5, 21),
                                  date(2027, 11, 20), date(2027, 11, 21), date(2031, 1, 1))] == [
        0, 0, 1, 1, 2, 2, 3, 4, 4, 5, 5]  # each ring's last day still in it; a year and a day: "Later"


def test_only_exact_days_are_placed_and_other_dates_are_listed_by_what_igdb_states():
    games = [
        listed("igdb-1", "Exact Game", entry("PC", day=date(2027, 2, 23))),
        listed("igdb-2", "Month Game", entry("PC", fmt=1, y=2027, m=3)),
        listed("igdb-3", "Quarter Game", entry("PlayStation 5", fmt=3, y=2027), platforms=("PlayStation 5",)),
        listed("igdb-4", "Year Game", entry("Xbox Series X|S", fmt=2, y=2027), platforms=("Xbox Series X|S",)),
        listed("igdb-5", "TBA Game", entry("PC", fmt=7)),
        listed("igdb-6", "Early Only", entry("PC", day=date(2025, 1, 1), status=3)),  # early access is not a release
        listed("igdb-7", "Undated"),  # IGDB lists PC for it without any date
        listed("igdb-8", "This Year", entry("PC", fmt=2, y=2026)),  # 2026 has begun but is not over
    ]
    radar = dashboard.release_radar([tracked(*games)], TODAY)
    assert [(point["game"]["name"], point["family"], point["when"]) for point in radar["points"]] == [
        ("Exact Game", "PC", "23 Feb 2027 · in 106 days · PC")]
    assert [(group["label"], [game["name"] for game in group["games"]]) for group in radar["groups"]] == [
        ("2026", ["This Year"]), ("Q1 2027", ["Quarter Game"]), ("Mar 2027", ["Month Game"]),
        ("2027", ["Year Game"]), ("TBA", ["TBA Game"]), ("No date yet", ["Early Only", "Undated"])]
    # Ordered by the last day each date allows, then TBA, then no date; the table follows the same order.
    assert [game["soonest_text"] for game in radar["table"]] == [
        "23 Feb 2027 (in 106 days)", "2026", "Q1 2027", "Mar 2027", "2027", "TBA", "No date yet", "No date yet"]
    assert [group["when"] for group in radar["groups"]][2:5] == ["Mar 2027: no exact day yet", "2027: no exact day yet",
                                                                 "Release date to be announced"]
    assert [game["sections"] for game in radar["table"][1:]] == [[]] * 7  # none of them is on the radar


def test_a_game_on_several_platforms_has_a_point_in_each_section_at_that_sections_earliest_day():
    multi = listed("igdb-1", "Many Ports", entry("PC", day=date(2027, 2, 18)),
                   entry("PlayStation 5", day=date(2027, 2, 18)),
                   entry("Xbox Series X|S", day=date(2027, 2, 18)), entry("PlayStation 4", day=date(2027, 3, 1)),
                   entry("Nintendo Switch 2", day=date(2027, 5, 20)), entry("Xbox One", fmt=7),
                   platforms=("PC", "PlayStation 5", "Xbox Series X|S", "PlayStation 4", "Nintendo Switch 2",
                              "Xbox One"))
    console = listed("igdb-2", "Console First", entry("PlayStation 5", day=date(2026, 12, 1)),
                     entry("PC", fmt=2, y=2027), platforms=("PlayStation 5", "PC"))  # PC only "2027": no PC point
    radar = dashboard.release_radar([tracked(multi, console)], TODAY)
    assert [game["name"] for game in radar["dated"]] == ["Console First", "Many Ports"]  # soonest first
    points = [(point["game"]["name"], point["family"], point["label"], point["platforms"], point["primary"])
              for point in radar["points"]]
    assert points == [
        ("Console First", "PlayStation", "1 Dec 2026", ["PlayStation 5"], True),
        ("Many Ports", "PC", "18 Feb 2027", ["PC"], True),
        ("Many Ports", "Xbox", "18 Feb 2027", ["Xbox Series X|S"], False),  # Xbox One is only TBA
        ("Many Ports", "PlayStation", "18 Feb 2027", ["PlayStation 5"], False),  # PS4 on 1 Mar is later
        ("Many Ports", "Switch", "20 May 2027", ["Nintendo Switch 2"], False)]
    many = [point for point in radar["points"] if point["game"]["name"] == "Many Ports"]
    same_day = [distance(point) for point in many[:3]]
    assert max(same_day) - min(same_day) < 0.001  # the same day: the same ring position in every section
    assert distance(many[3]) > distance(many[0])  # Switch 2 comes later, further out
    assert [(sector["name"], sector["games"]) for sector in radar["sectors"]] == [
        ("PC", 1), ("Xbox", 1), ("PlayStation", 2), ("Switch", 1)]  # 5 placements of 2 games; no "Other" platform
    assert radar["dated"][1]["sections"] == ["PC", "Xbox", "PlayStation", "Switch"]
    assert radar["dated"][0]["spoken"] == "1 Dec 2026, in 22 days, on PlayStation 5; 2027 on PC"
    assert radar["dated"][1]["spoken"] == (
        "18 Feb 2027, in 101 days, on PC, PlayStation 5, Xbox Series X|S; 1 Mar 2027, in 112 days, on PlayStation 4; "
        "20 May 2027, in 192 days, on Nintendo Switch 2; TBA on Xbox One")


def test_a_platform_listed_without_any_date_still_gets_its_section_with_no_point():
    radar = dashboard.release_radar([tracked(
        listed("igdb-1", "Dated", entry("PC", day=date(2027, 2, 23))),
        listed("igdb-2", "Switch Someday", platforms=("Nintendo Switch 2",)))], TODAY)  # IGDB lists it, no date
    assert [(sector["name"], sector["games"]) for sector in radar["sectors"]] == [("PC", 1), ("Switch", 0)]
    assert [(group["label"], [game["name"] for game in group["games"]]) for group in radar["groups"]] == [
        ("No date yet", ["Switch Someday"])]
    assert radar["groups"][0]["games"][0]["spoken"] == "no date yet on Nintendo Switch 2"


def test_points_stay_inside_their_section_and_a_later_year_goes_to_the_outer_band():
    games = [listed(f"igdb-{number}", f"Game {number}",
                    entry("PC", day=date(2026, 11, 10) + timedelta(days=40 * number))) for number in range(1, 13)]
    games.append(listed("igdb-50", "Far Away", entry("Mac", day=date(2028, 2, 1)), platforms=("Mac",)))
    radar = dashboard.release_radar([tracked(*games)], TODAY)
    sectors = [sector["name"] for sector in radar["sectors"]]
    assert sectors == ["PC", "Other"] and radar["other_platforms"] == ["Mac"]
    for point in radar["points"]:
        angle = math.degrees(math.atan2(point["y"] - 50, point["x"] - 50)) % 360  # 0 = right, clockwise
        start = (270 + 180 * sectors.index(point["family"])) % 360  # two sections of 180°, the first from the top
        margin = 180 * dashboard.RADAR_SIDE_MARGIN
        assert margin - 0.1 <= (angle - start) % 360 <= 180 - margin + 0.1  # never on a section's border
        assert 0 < distance(point) < 1
    far = next(point for point in radar["points"] if point["game"]["name"] == "Far Away")
    assert 5 / 6 < distance(far) < 1 and far["when"] == "1 Feb 2028 · in 449 days · Mac"  # past a year: "Later"
    assert radar["later"]["after"] == date(2027, 11, 9)
    # Twelve games in one section: the points never share a spot.
    spots = {(point["x"], point["y"]) for point in radar["points"]}
    assert len(spots) == len(radar["points"]) == 13


def test_a_game_in_both_lists_counts_once_and_empty_lists_give_an_empty_radar():
    shared = listed("igdb-1", "Shared", entry("PC", day=date(2027, 2, 23)))
    radar = dashboard.release_radar([tracked(shared, signal=WANT_TO_PLAY), tracked(shared)], TODAY)
    assert [(game["name"], game["lists"]) for game in radar["games"]] == [
        ("Shared", ["Most wanted on IGDB", "Most wishlisted on Steam"])]
    assert len(radar["points"]) == 1
    # The radar counts games as the comparison beside it does: one verified Steam app under two keys is one game.
    twin = item("igdb-2", "Twin", shared["availability"], steam_verified=True, steam_appid=900, igdb_id=2)
    again = item("igdb-3", "Twin (second record)", shared["availability"], steam_verified=True, steam_appid=900,
                 igdb_id=3)
    merged = dashboard.release_radar([tracked(twin, signal=WANT_TO_PLAY), tracked(again)], TODAY)
    assert [(game["name"], game["igdb_ids"]) for game in merged["games"]] == [("Twin", [2, 3])]
    assert len(merged["points"]) == 1
    assert [(entry["title"], entry["games"]) for entry in radar["lists"]] == [("Most wanted on IGDB", 1),
                                                                              ("Most wishlisted on Steam", 1)]
    empty = dashboard.release_radar([tracked(signal=WANT_TO_PLAY), tracked()], TODAY)
    assert empty["games"] == empty["points"] == empty["groups"] == empty["sectors"] == []
    assert dashboard.release_radar([], TODAY)["games"] == []


def test_a_games_soonest_point_is_its_tab_stop_so_a_window_never_hides_it_alone():
    console_first = listed("igdb-1", "Console First", entry("PC", day=date(2027, 3, 1)),
                           entry("PlayStation 5", day=date(2026, 11, 20)), platforms=("PC", "PlayStation 5"))
    radar = dashboard.release_radar([tracked(console_first)], TODAY)
    # PC comes first among the sections, but the PlayStation 5 day is sooner: that point is the game's tab stop.
    assert [(point["family"], point["primary"], point["windows"]) for point in radar["points"]] == [
        ("PlayStation", True, ["month", "quarter", "all"]), ("PC", False, ["all"])]
    pc = radar["points"][1]
    assert distance(pc) > distance(radar["points"][0])  # a later day on PC: further out in its own section


def test_the_time_windows_end_on_rings_and_count_the_games_they_show():
    days = (date(2026, 11, 12), date(2026, 12, 9), date(2026, 12, 10), date(2027, 2, 9), date(2027, 2, 10))
    radar = dashboard.release_radar([tracked(*(listed(f"igdb-{number}", f"Game {number}", entry("PC", day=day))
                                               for number, day in enumerate(days, start=1)))], TODAY)
    assert [(window["key"], window["label"], window["until"], window["games"], window["radius"])
            for window in radar["windows"]] == [
        ("month", "Next month", date(2026, 12, 9), 2, 33.33),  # the "1 month" ring, 9 Dec, is the last day shown
        ("quarter", "Next 3 months", date(2027, 2, 9), 4, 50.0),
        ("all", "All tracked", None, 5, 100.0)]
    assert [point["windows"] for point in radar["points"]] == [
        ["month", "quarter", "all"], ["month", "quarter", "all"], ["quarter", "all"], ["quarter", "all"], ["all"]]
    assert [window[0] for window in dashboard.RADAR_WINDOWS] == ["month", "quarter", "all"]


def test_the_panel_gets_each_games_dates_by_platform_and_its_rank_in_each_list():
    shared = listed("igdb-1", "Shared", entry("PC", day=date(2027, 2, 23)),
                    entry("Nintendo Switch 2", fmt=1, y=2027, m=6),
                    entry("PC", day=date(2025, 1, 1), status=3), platforms=("PC", "Nintendo Switch 2", "Mac"))
    someday = listed("igdb-2", "Someday", entry("PC", fmt=7))
    radar = dashboard.release_radar([tracked(someday, shared, signal=WANT_TO_PLAY), tracked(shared)], TODAY)
    game = radar["selected"]
    assert game["name"] == "Shared" and game["soonest_text"] == "23 Feb 2027 (in 106 days)"  # the soonest release
    assert game["timing"] == [
        {"when": "23 Feb 2027", "platforms": "PC", "until": "in 106 days"},
        {"when": "Jun 2027", "platforms": "Nintendo Switch 2", "until": ""},  # a month: no days are counted
        {"when": "Early access since 1 Jan 2025", "platforms": "PC", "until": ""},
        {"when": "No date yet", "platforms": "Mac", "until": ""}]
    # The place the Trending page gives the game in each list it is in.
    assert game["ranks"] == [{"list": "Most wanted on IGDB", "place": 2},
                             {"list": "Most wishlisted on Steam", "place": 1}]
    assert [(undated["name"], undated["when"]) for undated in radar["undated"]] == [
        ("Someday", "Release date to be announced")]
    assert dashboard.release_radar([tracked(someday)], TODAY)["selected"]["name"] == "Someday"  # no exact day at all
    assert dashboard.release_radar([], TODAY)["selected"] is None


# ---------------------------------------------------------------------- the page


def save_radar(settings):
    """Two saved lists with 17 upcoming games (1 in both): exact days on one or several platforms, a release more
    than a year away, a month, a quarter, a year, TBA, an early access without a release, and one game already out.
    Steam's list holds 14 upcoming games, more than the 10 the Trending page shows."""
    tba = entry("PC", fmt=7)
    records = [
        upcoming.record_from_igdb(igdb_game(401, "Soon Saga", entry("PC", day=date(2026, 11, 12)))),
        upcoming.record_from_igdb({**igdb_game(
            402, "Moon Port", entry("PC", day=date(2027, 2, 23)), entry("PlayStation 5", day=date(2027, 2, 23)),
            entry("Xbox Series X|S", day=date(2027, 2, 23)), entry("Nintendo Switch 2", day=date(2027, 5, 20)),
            platforms=("PC", "PlayStation 5", "Xbox Series X|S", "Nintendo Switch 2")),
            "cover": {"image_id": "co4abc"}}),
        upcoming.record_from_igdb(igdb_game(403, "Console Dawn", entry("PlayStation 5", day=date(2026, 12, 1)),
                                            entry("PC", fmt=2, y=2027), platforms=("PlayStation 5", "PC"))),
        upcoming.record_from_igdb(igdb_game(404, "Far Frontier", entry("PC", day=date(2028, 2, 1)))),
        upcoming.record_from_igdb(igdb_game(405, "Quarter Quest", entry("Xbox Series X|S", fmt=3, y=2027),
                                            platforms=("Xbox Series X|S",))),
        upcoming.record_from_igdb(igdb_game(406, "Month Mystery", entry("PC", fmt=1, y=2027, m=3))),
        upcoming.record_from_igdb(igdb_game(407, "Year Yonder", entry("PC", fmt=2, y=2027),
                                            entry("PlayStation 5", fmt=2, y=2027), platforms=("PC", "PlayStation 5"))),
        upcoming.record_from_igdb(igdb_game(408, "Someday", tba)),
        upcoming.record_from_igdb(igdb_game(409, "Early Bird", entry("PC", day=date(2025, 1, 1), status=3))),
        upcoming.record_from_igdb(igdb_game(410, "Mac Only", entry("Mac", day=date(2027, 1, 15)), platforms=("Mac",))),
        upcoming.record_from_igdb(igdb_game(411, "Released Already", entry("PC", day=date(2026, 11, 1)))),
        *(upcoming.record_from_igdb(igdb_game(420 + number, f"Filler {number}", tba)) for number in range(1, 8)),
    ]
    calculated = NOW - timedelta(hours=10)
    steam_order = [408, 407, *range(421, 428), 402, 406, 404, 401, 409]  # Far Frontier is 12th, Soon Saga 13th
    conn = store.connect(settings.db_path)
    try:
        store.save_igdb_games(conn, records, {}, [], now=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
        store.save_popularity_snapshot(conn, "want_to_play", [
            SignalEntry(igdb_id, 0.004 - index / 1000, position, calculated)
            for index, (igdb_id, position) in enumerate(((402, 5), (403, 9), (405, 14), (410, 20), (411, 21)))],
            now=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
        store.save_popularity_snapshot(conn, "steam_wishlists", [
            SignalEntry(igdb_id, 0.0005 - place / 1e5, place, calculated)
            for place, igdb_id in enumerate(steam_order, 1)], now=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
    finally:
        conn.close()


def dots(card: str) -> list[str]:
    return re.findall(r'<a class="radar-dot[^"]*"[^>]*>.*?</a>', card, re.S)


def upcoming_page(client, **params) -> str:
    return client.get("/dashboard", params={"report": "upcoming", **params}).text


def test_the_radar_places_the_tracked_games_by_platform_and_folds_the_rest(games, settings):
    save_radar(settings)
    client = dashboard_client(settings)
    board = upcoming_page(client)
    card = chart(board, "chart-radar")
    text = text_of(card)
    assert "Upcoming Release Radar All platforms" in text
    assert ("The upcoming games of both tracked lists, placed by when each is due: not every announced game. The center "
            "is today: the closer a point, the sooner the release.") in text  # the comparison beside it counts them
    assert len(re.findall(r'<template id="radar-game-', board)) == 17  # 4 + 14 saved games, 1 in both
    # Rings and sections, each section with its number of placed games.
    rings = r'<span class="radar-ring[^"]*" style="top: ([\d.]+)%" aria-hidden="true">([^<]+)</span>'
    assert re.findall(rings, card) == [("41.67", "1 week"), ("33.33", "1 month"), ("25.0", "3 months"),
                                       ("16.67", "6 months"), ("8.33", "1 year"), ("4.17", "Later")]
    sections = r'<span class="radar-sector is-(\w+)"[^>]*>(\w+) <span class="radar-count">(\d+)</span>'
    assert re.findall(sections, card) == [
        ("end", "PC", "3"), ("end", "Xbox", "1"), ("middle", "PlayStation", "2"), ("start", "Switch", "1"),
        ("start", "Other", "1")]  # labels on the right end at their spot, on the left start there: inside the circle
    assert "5 games have an exact release day: 8 points, one in each section where the game has one" in text
    assert "Other: Mac." in text
    # Every point is a link that opens the game's details; only a game's soonest point is a tab stop.
    points = dots(card)
    assert len(points) == 8 and all('href="/games/igdb-' in point and "data-details=" in point for point in points)
    first = [point for point in points if 'tabindex="-1"' not in point]
    assert [re.search(r'data-details="([^"]+)"', point).group(1) for point in first] == [
        "igdb-401", "igdb-403", "igdb-410", "igdb-402", "igdb-404"]  # soonest first: the tab order
    assert all('aria-hidden="true"' in point for point in points if point not in first)
    assert all('aria-hidden' not in point and " hidden" not in point for point in first)  # all tracked: none hidden
    assert ("Moon Port: 23 Feb 2027, in 106 days, on PC, PlayStation 5, Xbox Series X|S; 20 May 2027, in 192 days, on "
            "Nintendo Switch 2") in text_of("".join(first))  # the full text, once per game
    assert 'title="Far Frontier: 1 Feb 2028 · in 449 days · PC"' in card  # each point's own date, as a hint
    assert 'data-windows="month quarter all"' in first[0] and 'data-windows="all"' in first[-1]
    # The selected game, at first the soonest one: its cover, dates by platform and ranks, beside the radar.
    panel = card[card.index('<aside class="radar-panel"'):card.index("</aside>")]
    assert 'data-selected="igdb-401"' in panel
    assert text_of(panel).startswith("Selected game Soon Saga 12 Nov 2026 (in 3 days) 12 Nov 2026 · in 3 days PC "
                                     "#13 in Most wishlisted on Steam")
    assert points[0].startswith('<a class="radar-dot is-selected"')
    # Each game's panel copy, for app.js; Moon Port's with its cover, every platform's date and both ranks.
    moon = re.search(r'<template id="radar-game-igdb-402">(.*?)</template>', card, re.S).group(1)
    assert 'src="https://images.igdb.com/igdb/image/upload/t_cover_big/co4abc.jpg"' in moon
    assert text_of(moon) == ("Moon Port 23 Feb 2027 (in 106 days) 23 Feb 2027 · in 106 days PC, PlayStation 5, Xbox "
                             "Series X|S 20 May 2027 · in 192 days Nintendo Switch 2 #1 in Most wanted on IGDB #10 in "
                             "Most wishlisted on Steam")
    assert len(re.findall(r'<template id="radar-game-', card)) == 17  # every tracked game
    # Date TBA or approximate: one counted control under the radar, by the date IGDB states, never placed.
    more = card[card.index('<details class="radar-more">'):card.index("</details>", card.index('class="radar-more"'))]
    assert text_of(more).startswith("Date TBA or approximate 12 games Not on the radar")
    groups = re.findall(r'<div class="radar-group"><dt>([^<]+)</dt>(.*?)</dd></div>', more, re.S)
    assert [(label, [text_of(name) for name in re.findall(r'<span class="radar-chip-name">(.*?)</span>', body)])
            for label, body in groups] == [
        ("Q1 2027", ["Quarter Quest"]), ("Mar 2027", ["Month Mystery"]), ("2027", ["Year Yonder"]),
        ("TBA", [*(f"Filler {number}" for number in range(1, 8)), "Someday"]), ("No date yet", ["Early Bird"])]
    assert "Released Already" not in card  # out since 1 Nov: not upcoming
    # The table view holds the same facts without the picture.
    table = text_of(re.search(r"<table.*?</table>", card, re.S).group(0))
    assert ("Moon Port 23 Feb 2027 (in 106 days) PC, Xbox, PlayStation, Switch Announced for PC, PlayStation 5, Xbox "
            "Series X|S · 23 Feb 2027 Announced for Nintendo Switch 2 · 20 May 2027 #1 in Most wanted on IGDB; #10 in "
            "Most wishlisted on Steam") in table
    assert "Quarter Quest Q1 2027 Not placed" in table and "Early Bird No date yet Not placed" in table
    assert ("IGDB release dates and platforms of the games in IGDB PopScore types 2 and 10 · lists saved 9 Nov 2026, "
            "12:00 UTC; days counted from 9 Nov 2026.") in text
    method = text_of(methodology(board))
    assert ("The rings end 1 week (16 Nov 2026), 1 month (9 Dec 2026), 3 months (9 Feb 2027), 6 months (9 May 2027), "
            "1 year (9 Nov 2027) later, and the outer band, “Later”, holds what is due after 9 Nov 2027") in method
    assert "“Next month” shows the exact release days up to 9 Dec 2026; “Next 3 months” shows the exact release days " \
           "up to 9 Feb 2027, each ending on a ring" in method
    assert "Other (Mac)" in method and "focusing it with Tab (on the radar, soonest game first)" in method
    # The radar reaches past the 10 games the Trending page shows: Far Frontier and Soon Saga are 12th and 13th.
    steam_list = part(client.get("/trending").text, "upcoming-steam_wishlists-title", "</section>")
    assert "igdb-404" not in details_keys(steam_list) and "igdb-404" in details_keys(card)


def test_a_time_window_keeps_only_the_points_it_covers_and_works_without_javascript(games, settings):
    save_radar(settings)
    client = dashboard_client(settings)
    links = re.findall(r'<a class="window-link" href="([^"]+)" data-radar-window="(\w+)"([^>]*)>(.*?)</a>',
                       chart(upcoming_page(client), "chart-radar"), re.S)
    assert [(href, key, "aria-current" in rest, text_of(label)) for href, key, rest, label in links] == [
        ("/dashboard?report=upcoming&amp;window=month", "month", False, "Next month 2 games"),
        ("/dashboard?report=upcoming&amp;window=quarter", "quarter", False, "Next 3 months 3 games"),
        ("/dashboard?report=upcoming", "all", True, "All tracked 5 games")]
    month = chart(upcoming_page(client, window="month"), "chart-radar")
    assert '<div class="radar" data-radar data-window="month">' in month
    shown = [point for point in dots(month) if " hidden>" not in point]  # the hidden attribute ends the tag
    assert sorted(set(re.findall(r'data-details="([^"]+)"', "".join(shown)))) == ["igdb-401", "igdb-403"]
    assert len(dots(month)) == 8  # the other points stay in the page, hidden, for app.js to show again
    assert '<span class="radar-dim is-month" style="--edge: 33.33%" aria-hidden="true"></span>' in month
    assert 'data-radar-window="month" aria-current="true"' in month
    assert "window=quarter" in month and "<template id=\"radar-game-igdb-404\">" in month  # nothing else is lost
    unknown = chart(upcoming_page(client, window="decade"), "chart-radar")
    assert 'data-window="all"' in unknown and " hidden>" not in "".join(dots(unknown))


def test_every_radar_game_opens_its_details(games, settings):
    save_radar(settings)
    client = dashboard_client(settings)
    card = chart(upcoming_page(client), "chart-radar")
    keys = sorted(set(details_keys(card)))
    assert len(keys) == 17
    for key in keys:  # what app.js loads into the dialog, and the page a link opens without JavaScript
        panel = client.get(f"/games/{key}/panel")
        assert panel.status_code == 200 and 'id="dialog-heading"' in panel.text, key
        assert client.get(f"/games/{key}").status_code == 200, key
    moon = text_of(client.get("/games/igdb-402/panel").text)
    assert "Moon Port" in moon and "23 Feb 2027" in moon


def test_the_radar_explains_missing_data(games, settings):
    client = dashboard_client(settings)
    empty = text_of(chart(upcoming_page(client), "chart-radar"))
    assert "Not refreshed yet: run python -m game_librarian upcoming." in empty and "radar-dot" not in empty
    assert "IGDB release dates and platforms of the games in IGDB PopScore types 2 and 10 · not refreshed yet." in empty
    # Saved lists whose games all lack an exact day: no points and no windows, every game listed by its stated date.
    save_upcoming(settings)
    card = chart(upcoming_page(client), "chart-radar")
    assert len(dots(card)) == 2  # Moon Garden on PC and PlayStation 5 (23 Feb 2027)
    conn = store.connect(settings.db_path)
    try:
        conn.execute("DELETE FROM popularity_snapshots WHERE igdb_id = 1")  # only the games without a day are left
        conn.commit()
    finally:
        conn.close()
    undated = chart(upcoming_page(client), "chart-radar")
    assert ("None of these games has an exact release day yet, so the radar has no points. Each is listed below by "
            "the date IGDB states.") in text_of(undated)
    assert not dots(undated) and 'class="window-switch"' not in undated
    assert '<details class="radar-more" open>' in undated  # then the list is open: it is all there is
    assert '<div class="radar-group"><dt>Mar 2027</dt>' in undated
    assert '<div class="radar-group"><dt>TBA</dt>' in undated
    assert 'data-selected="101"' in undated  # the panel shows the first listed game instead: Game 01 Saga, Mar 2027
    # Saved lists whose games are all out: nothing upcoming to place.
    conn = store.connect(settings.db_path)
    try:
        conn.execute("DELETE FROM popularity_snapshots WHERE igdb_id IN (2, 4)")
        conn.commit()
    finally:
        conn.close()
    out = text_of(chart(upcoming_page(client), "chart-radar"))
    assert "None of the games saved by the last refresh is still upcoming." in out


def test_the_radar_reflows_and_works_with_a_keyboard_pointer_and_touch():
    # Points keep a 24 px pointer target; the radar card is a size container, so the panel sits beside the radar (two
    # thirds and one third) only where the card is 700 px wide or more, and below it otherwise. The radar card and
    # the comparison share a row (3 : 2) down to 1,100 px, then the comparison goes below; on a phone the labels
    # shrink and each date sits above its games.
    assert "inset: -6px;" in rule(".radar-dot::before")
    assert "container-type: inline-size;" in rule(".radar-card")
    assert "grid-template-columns: minmax(0, 1fr);" in rule(".radar-layout")
    assert re.search(r"@container \(min-width: 700px\) \{\s*\.radar-layout \{ grid-template-columns: minmax\(0, 2fr\) "
                     r"minmax\(0, 1fr\); \}", CSS)
    assert "grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);" in rule(".report-canvas.is-upcoming")
    assert ".report-canvas.is-upcoming { grid-template-columns: minmax(0, 1fr); }" in media(1100)
    phone = media(720)
    for selector in (".radar-ring {", ".radar-sector {", ".radar-dot {", ".radar-dot::before { inset: -7px; }",
                     ".radar-group {", ".rp-head {"):
        assert selector in phone, selector
    assert "aspect-ratio: 1;" in rule(".radar") and "max-width: 560px;" in rule(".radar")
    high_contrast = re.search(r"@media \(forced-colors: active\) \{(.*?)\n\}", CSS, re.S).group(1)
    assert ".radar-dot, .plot-dot, .line-dot, .wf-mark { forced-color-adjust: none; background: CanvasText;" \
        in high_contrast  # points stay visible
    assert '.radar[data-window="month"] .radar-dim.is-month' in CSS
    # app.js: a mouse or pen selects on hover, focus selects, a first tap selects without opening, and a click on the
    # selected game opens its details; the panel's copy is cloned from Jinja's escaped <template>, never a string.
    start = APP_JS.index("function trackSelection")
    radar_js = APP_JS[start:APP_JS.index('querySelectorAll("[data-chart-tips]")')]  # the shared selection and radar
    for needed in ('event.pointerType === "mouse" || event.pointerType === "pen"', 'addEventListener("focusin"',
                   'addEventListener("pointerdown"', "before.selected !== key", "event.preventDefault()",
                   'document.getElementById("radar-game-" + key)', "template.content.cloneNode(true)",
                   "point.hidden =", 'link.setAttribute("aria-current", "true")', "history.replaceState"):
        assert needed in radar_js, needed
    assert "innerHTML" not in radar_js
