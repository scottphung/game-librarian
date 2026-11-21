"""Trending games → Momentum (version 0.15): which games account for the change in Steam review count across the
catalog, as a waterfall from two comparable snapshots, and a selected game's Steam player snapshots as a line.

A game is compared by the Trending page's rule (its newest review count against its newest one 7 to 14 days earlier),
falls and zero changes included, so the cohort is fixed: the earlier and later totals cover exactly the same games,
and the steps add up to the difference. A review count is not copies sold or players, and the page says so.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114; Game n has 1,000 + 50 × n Steam reviews). All
names and numbers are made up."""

import re
from datetime import date, datetime, timedelta, timezone

from game_librarian import dashboard, store
from game_librarian.config import PACKAGE_DIR
from tests.test_dashboard_trending import dashboard_client, methodology
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_discovery import iso, save_players, with_conn

UTC = timezone.utc
END = datetime(2026, 11, 20, 16, tzinfo=UTC)
BASE = END - timedelta(days=8)
APP_JS = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def rows(*snapshots) -> list[dict]:
    """Review snapshot rows (store.review_history) of one game, oldest first, from (moment, total) pairs."""
    return [{"captured_at": moment.isoformat(), "total": total} for moment, total in snapshots]


def history() -> dict[int, list[dict]]:
    """Game 1 rises by 12,000 and Game 2 falls by 1,000 (Steam removed reviews); Game 3 does not change; Games 6-14
    rise by 10 to 90 from 1,000. Game 4's only other count is 3 days old and Game 5's 20 days old: neither is
    comparable, so both are left out of the cohort."""
    found = {
        1: rows((END - timedelta(days=15), 90_000), (BASE, 100_000), (END - timedelta(days=2), 110_000),
                (END, 112_000)),
        2: rows((BASE - timedelta(minutes=10), 50_000), (END - timedelta(minutes=10), 49_000)),
        3: rows((BASE, 7_000), (END, 7_000)),
        4: rows((END - timedelta(days=3), 900), (END, 950)),
        5: rows((END - timedelta(days=20), 900), (END, 990)),
    }
    for number in range(6, 15):
        found[number] = rows((BASE, 1_000), (END, 1_000 + 10 * (number - 5)))
    return found


# ---------------------------------------------------------------------- the waterfall (pure)


def test_the_waterfall_compares_a_fixed_cohort_by_the_trending_pages_rule():
    waterfall = dashboard.review_waterfall(history())
    # Games 4 and 5 have no count 7 to 14 days before their newest one: they are in neither total.
    assert waterfall["games"] == 12 and (waterfall["rising"], waterfall["falling"]) == (10, 1)
    assert waterfall["before_total"] == 100_000 + 50_000 + 7_000 + 9 * 1_000
    assert waterfall["after_total"] == 112_000 + 49_000 + 7_000 + 9 * 1_000 + 10 * sum(range(1, 10))
    # Game 1's baseline is its newest count in the window (8 days back), not the older or the newer one.
    first = waterfall["steps"][0]
    assert (first["appid"], first["before"], first["after"], first["change"]) == (1, 100_000, 112_000, 12_000)
    assert (first["before_at"], first["after_at"]) == (BASE, END)
    # The dates are the actual spans of the two ends.
    assert waterfall["before"] == (BASE - timedelta(minutes=10), BASE)
    assert waterfall["after"] == (END - timedelta(minutes=10), END)


def test_the_largest_changes_either_way_get_steps_and_the_rest_is_other_tracked_games():
    waterfall = dashboard.review_waterfall(history())
    steps = waterfall["steps"]
    assert [(step["kind"], step.get("appid"), step["change"]) for step in steps] == [
        ("game", 1, 12_000), ("game", 2, -1_000),  # a fall is kept, at its size
        *(("game", number, 10 * (number - 5)) for number in range(14, 8, -1)),
        ("other", None, 60)]  # Games 6-8 (+10, +20, +30) and Game 3 (no change)
    assert len([step for step in steps if step["kind"] == "game"]) == dashboard.WATERFALL_GAMES
    assert steps[-1]["games"] == 4
    # Each step starts where the one before ended, and the steps add up to the later total minus the earlier one.
    assert steps[0]["start"] == 0 and all(after["start"] == before["end"] for before, after in zip(steps, steps[1:]))
    assert waterfall["steps_total"] == steps[-1]["end"] == waterfall["net"] == 11_450
    assert waterfall["before_total"] + waterfall["steps_total"] == waterfall["after_total"]


def test_bars_sit_on_an_axis_of_change_that_starts_at_the_earlier_total():
    waterfall = dashboard.review_waterfall(history())
    assert [tick["label"] for tick in waterfall["ticks"]] == ["0", "+5K", "+10K", "+15K"]
    assert waterfall["zero_x"] == 0.0 and (waterfall["net_left"], waterfall["net_width"]) == (0.0, 76.33)
    rise, fall = waterfall["steps"][:2]
    assert (rise["left"], rise["width"], rise["start_x"], rise["end_x"]) == (0.0, 80.0, 0.0, 80.0)
    assert (fall["left"], fall["width"], fall["start_x"], fall["end_x"]) == (73.33, 6.67, 80.0, 73.33)  # leftward
    # Only falls: the axis ends at zero. A fall first, then larger rises: zero sits inside the axis.
    falling = dashboard.review_waterfall({1: rows((BASE, 5_000), (END, 3_800)), 2: rows((BASE, 100), (END, 400))})
    assert [tick["value"] for tick in falling["ticks"]] == [-1_500, -1_000, -500, 0]
    assert falling["zero_x"] == 100.0 and falling["net"] == -900
    assert (falling["net_left"], falling["net_width"]) == (40.0, 60.0)  # the net bar runs left from zero
    crossing = dashboard.review_waterfall({1: rows((BASE, 5_000), (END, 3_800)), 2: rows((BASE, 100), (END, 1_100)),
                                           3: rows((BASE, 200), (END, 1_000))})
    assert [tick["value"] for tick in crossing["ticks"]] == [-1_500, -1_000, -500, 0, 500, 1_000]
    assert crossing["zero_x"] == 60.0 and crossing["net"] == 600
    assert (crossing["net_left"], crossing["net_width"]) == (60.0, 24.0)


def test_no_comparable_pair_means_no_waterfall_and_no_change_means_no_game_step():
    assert dashboard.review_waterfall({}) is None
    assert dashboard.review_waterfall({1: rows((END - timedelta(days=4), 10), (END, 20))}) is None  # too close
    assert dashboard.review_waterfall({1: rows((END - timedelta(days=15), 10), (END, 20))}) is None  # too far
    still = dashboard.review_waterfall({1: rows((BASE, 10), (END, 10)), 2: rows((BASE, 5), (END, 5))})
    assert [(step["kind"], step["change"], step["games"]) for step in still["steps"]] == [("other", 0, 2)]
    assert [tick["value"] for tick in still["ticks"]] == [0, 1] and still["net"] == 0


def test_axis_labels_keep_their_sign():
    assert dashboard.change_ticks(-1_200, 9_000) == [-5_000, 0, 5_000, 10_000]
    assert dashboard.change_ticks(0, 0) == [0, 1]
    assert [dashboard.signed_compact(value) for value in (20_000, -5_000, 0, 750)] == ["+20K", "−5K", "0", "+750"]


# ---------------------------------------------------------------------- the player line (pure)


def player_rows(*snapshots) -> list[dict]:
    return [{"captured_at": moment.isoformat(), "players": players} for moment, players in snapshots]


def test_a_player_line_needs_a_week_of_snapshots_and_breaks_where_none_was_saved():
    day = timedelta(days=1)
    few = dashboard.player_line(player_rows((END - 3 * day, 100), (END, 300)), END)
    assert few["line"] is None and (few["count"], few["first"], few["last"]) == (2, END - 3 * day, END)
    found = dashboard.player_line(player_rows(
        (END - 40 * day, 50),  # before the 30 days the line covers
        (END - 9 * day, 3_000), (END - 8 * day, 3_200), (END - 7 * day, None),  # Steam reported no count
        (END - 6 * day, 3_100), (END - 1 * day, 3_800), (END, 4_000)), END)
    line = found["line"]
    assert found["count"] == 5 and found["first"] == END - 9 * day
    assert [(point["players"], point["x"], point["y"]) for point in line["points"]] == [
        (3_000, 0.0, 75.0), (3_200, 11.11, 80.0), (3_100, 33.33, 77.5), (3_800, 88.89, 95.0), (4_000, 100.0, 100.0)]
    assert line["gaps"] == [(END - 6 * day, END - 1 * day)]  # five days without a snapshot: the line breaks
    assert line["segments"] == ["0.0,25.0 11.11,20.0 33.33,22.5", "88.89,5.0 100.0,0.0"]
    assert [tick["label"] for tick in line["y_ticks"]] == ["0", "1K", "2K", "3K", "4K"]  # from zero
    assert [tick["label"] for tick in line["x_ticks"]] == ["12 Nov", "14 Nov", "16 Nov", "18 Nov", "20 Nov"]
    lonely = dashboard.player_line(player_rows((END - 9 * day, 10), (END, 20)), END)["line"]
    assert lonely["segments"] == [] and len(lonely["points"]) == 2  # two points, nothing joined across 9 days


def test_capture_days_list_the_days_with_a_snapshot():
    found = {1: player_rows((END - timedelta(days=1), None), (END, 5)), 2: player_rows((END, 7))}
    assert dashboard.capture_days(found) == [(END - timedelta(days=1)).date(), END.date()]
    assert dashboard.capture_days(found, "players") == [END.date()]  # a snapshot without a count is not a count


# ---------------------------------------------------------------------- the page

SYNC = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)  # the fixture's sync saved every game's review count then


def save_baselines(settings, totals: dict[int, int], moment: datetime = SYNC - timedelta(days=8)) -> None:
    """Review counts saved earlier, as an earlier sync would have saved them."""
    def action(conn):
        conn.executemany("INSERT INTO review_snapshots (appid, captured_at, total, positive, negative) "
                         "VALUES (?, ?, ?, ?, ?)", [(appid, iso(moment), total, total // 2, total - total // 2)
                                                    for appid, total in totals.items()])
        conn.commit()
    with_conn(settings, action)


def momentum_history(settings) -> None:
    """Eight days before the sync: Game 14 had 500 fewer reviews, Game 10 200 fewer, Game 13 40 more (Steam removed
    some since), Games 02-09 10 fewer each and Game 01 the same; Games 11 and 12 have no earlier count. Game 14's
    players were saved over nine days with a six-day gap, Game 10's on two days nine days apart, Game 13's once."""
    save_baselines(settings, {114: 1_200, 110: 1_300, 113: 1_690, 101: 1_050,
                              **{100 + number: 1_000 + 50 * number - 10 for number in range(2, 10)}})
    for days, players in ((9, 3_000), (8, 3_200), (7, 3_100), (1, 3_800)):
        save_players(settings, {114: players}, moment=SYNC - timedelta(days=days))
    save_players(settings, {110: 500}, moment=SYNC - timedelta(days=9))
    save_players(settings, {114: 4_000, 110: 800, 113: 900}, moment=SYNC + timedelta(minutes=10))


def rows_of(html: str) -> list[str]:
    return [text_of(row) for row in re.findall(r'<li class="wf-row.*?</li>', html, re.S)]


def players_card(html: str) -> str:
    start = html.index('<section class="chart-card players-card"')
    return html[start:html.index("</section>", start)]


def test_the_waterfall_runs_from_the_earlier_to_the_later_total_of_the_same_games(games, settings):
    momentum_history(settings)
    html = dashboard_client(settings).get("/dashboard", params={"report": "momentum"}).text
    card = html[html.index('<figure class="chart-card wf-card"'):html.index("</figure>")]
    words = text_of(card)
    assert "Change in Steam review count Steam only" in words
    assert ("From 1 Nov 2026 to 9 Nov 2026, for the 12 catalog games with a count at both dates: 10 gained reviews, 1 "
            "lost some. A review count says nothing about copies sold or people playing.") in words
    assert rows_of(card) == [
        "Earlier total 1 Nov 2026 15,360", "Game 14 +500", "Game 10 +200", "Game 13 −40",
        *(f"Game {number:02d} +10" for number in range(2, 7)), "Other tracked games 4 games +30",
        "Later total 9 Nov 2026 16,100 +740"]
    assert 15_360 + 500 + 200 - 40 + 5 * 10 + 30 == 16_100  # the steps reconcile to the later total
    table = text_of(re.search(r"<table.*?</table>", card, re.S).group(0))
    assert "Earlier total (12 games) 15,360" in table and "Game 13 1,690 1,650 −40" in table
    assert "Sum of the changes +740 Later total (12 games) 16,100 +740" in table
    assert "Fewer reviews (removed or corrected)" in words  # the legend names a fall for what it is
    assert 'class="wf-row is-down"' in card and card.count('class="wf-row is-up"') == 7  # the games that rose
    assert 'class="wf-row is-up is-other"' in card
    # Every game's name opens its details; only games with a player line have a bar that switches the line.
    assert re.findall(r'data-details="(\d+)"', card) == ["114", "110", "113", "102", "103", "104", "105", "106"]
    assert re.findall(r'<a class="wf-bar" href="([^"]+)" data-player="(\d+)"', card) == [
        ("/dashboard?report=momentum&amp;player=114#players", "114"),
        ("/dashboard?report=momentum&amp;player=110#players", "110")]
    assert "each game’s newest count against its newest one 7 to 14 days earlier" in words


def test_the_selected_games_players_show_their_actual_times_and_gaps(games, settings):
    momentum_history(settings)
    client = dashboard_client(settings)
    html = client.get("/dashboard", params={"report": "momentum"}).text
    players = players_card(html)
    panels = dict(re.findall(r'<div class="pl-panel" data-player-panel="(\d+)"( hidden)?>', players))
    assert panels == {"114": "", "110": " hidden"}  # the largest change with a line is shown first
    shown = players[players.index('data-player-panel="114"'):players.index('data-player-panel="110"')]
    assert "Game 14: concurrent players at each snapshot this site saved, 31 Oct 2026, 12:00 UTC – 9 Nov 2026, " \
           "12:10 UTC." in text_of(shown)
    assert shown.count("<polyline") == 2  # a six-day gap breaks the line in two
    assert "the line breaks where none was saved for more than 2 days (2 Nov 2026 – 8 Nov 2026)" in text_of(shown)
    assert 'data-tip-name="4,000 players" data-tip-text="9 Nov 2026, 12:10 UTC"' in shown
    assert re.findall(r'<span class="tick-label">([^<]+)</span>', shown) == ["0", "1K", "2K", "3K", "4K"]
    assert "9 Nov 2026, 12:10 UTC 4,000" in text_of(re.search(r"<table.*?</table>", shown, re.S).group(0))
    assert 'aria-current="true"' in html[html.index('data-player="114"') - 200:html.index('data-player="114"') + 300]
    # ?player= picks another line without JavaScript; a game without a line, or anything else, keeps the default.
    other = client.get("/dashboard", params={"report": "momentum", "player": "110"}).text
    assert re.search(r'data-player-panel="110">', other) and re.search(r'data-player-panel="114" hidden>', other)
    for value in ("113", "abc", "999"):
        page = client.get("/dashboard", params={"report": "momentum", "player": value}).text
        assert re.search(r'data-player-panel="114">', page), value
    assert "Counts also follow the time of day." in text_of(players)


def test_without_comparable_history_one_coverage_note_comes_from_the_database(games, settings):
    html = dashboard_client(settings).get("/dashboard", params={"report": "momentum"}).text
    report = html[html.index('<section class="report"'):html.index('<details class="method-details"')]
    assert re.findall(r'class="chart-card[^"]*"', report) == []  # no empty frames: one note
    assert text_of(report).endswith(
        "Not enough snapshot history yet. Steam review counts of 14 games on 9 Nov 2026 (1 per game). A change needs "
        "one game’s counts 7 to 14 days apart, and no game has such a pair yet: the first can appear from 16 Nov 2026 if "
        "the sync runs then. A comparison of games’ first 4 weeks after launch is planned for games released on Steam "
        "since 9 Nov 2026: none has been released yet.")  # the sync saved no player count
    save_players(settings, {114: 4_000}, moment=SYNC + timedelta(minutes=10))
    later = text_of(dashboard_client(settings).get("/dashboard", params={"report": "momentum"}).text)
    assert "(1 per game), and player counts of 1 game on 9 Nov 2026." in later
    method = text_of(methodology(html))
    assert "Planned: the first 4 weeks after launch" in method
    for phrase in ("Catalog games released since then: 0; with every weekly count: 0.",
                   "on its release day and 7, 14, 21 and 28 days later, each on that day or the next",
                   "a lifetime review total saved later never stands for the reviews of a launch week"):
        assert phrase in method, phrase


def test_snapshot_coverage_counts_each_games_snapshots_and_comparable_pairs():
    day = END - timedelta(days=11)  # 9 Nov 2026, 16:00 UTC
    found = dashboard.snapshot_coverage({1: rows((day - timedelta(days=8), 100), (day, 150)),
                                         2: rows((day - timedelta(days=1), 10), (day, 12)), 3: []}, "total")
    assert (found["games"], found["most"], found["fewest"], found["comparable"]) == (2, 2, 2, 1)  # only game 1: 8 days
    assert found["days"] == [date(2026, 11, 1), date(2026, 11, 8), date(2026, 11, 9)]
    assert found["longest"] == timedelta(days=8)
    empty = dashboard.snapshot_coverage({}, "total")
    assert (empty["games"], empty["comparable"], empty["days"]) == (0, 0, [])


def test_a_launch_week_needs_a_real_count_at_every_weekly_boundary():
    release = date(2026, 11, 20)
    every = [datetime(2026, 11, 20, 18, tzinfo=UTC) + timedelta(days=days) for days in (0, 7, 14, 21, 28)]
    late = [datetime(2026, 11, 21, 2, tzinfo=UTC) + timedelta(days=days) for days in (0, 7, 14, 21, 28)]
    history = {1: rows(*((moment, 10) for moment in every)), 2: rows(*((moment, 10) for moment in late)),
               3: rows(*((moment, 10) for moment in every[:2] + every[3:])),  # day 14 missing: never filled in
               4: rows((datetime(2026, 11, 1, 12, tzinfo=UTC), 5000))}  # an old game with only a lifetime total
    days = {1: release, 2: release, 3: release, 4: date(2020, 5, 1), 5: release}  # game 5: no snapshot at all
    found = dashboard.launch_weeks_coverage(days, history)
    assert found["first"] == date(2026, 11, 1)
    assert found["candidates"] == [1, 2, 3, 5] and found["eligible"] == [1, 2]  # the next day still counts
    assert dashboard.launch_weeks_coverage(days, {}) == {"first": None, "candidates": [], "eligible": []}
    assert dashboard.LAUNCH_BOUNDARIES == (0, 7, 14, 21, 28)


def test_without_a_player_line_the_waterfall_stands_alone_with_a_one_line_note(games, settings):
    save_baselines(settings, {114: 1_600, 113: 1_600})
    html = dashboard_client(settings).get("/dashboard", params={"report": "momentum"}).text
    assert 'class="wf-row' in html and 'data-player-panel' not in html and '<a class="wf-bar"' not in html
    assert "Fewer reviews" not in text_of(html)  # no step fell, so the legend has no key for a fall
    players = text_of(players_card(html))
    assert players == "Players in game on Steam Steam only No player counts have been collected yet. python -m " \
                      "game_librarian sync saves one per game."
    method = text_of(methodology(html))
    assert "A fall is kept as it is: Steam removes reviews" in method
    assert "It cannot tell how many copies sold or how many people played" in method


def test_the_momentum_script_switches_the_line_in_place():
    script = APP_JS[APP_JS.index('querySelectorAll("[data-momentum]")'):APP_JS.index("var reducedMotion")]
    for needed in ('closest("a[data-player]")', "isPlainClick(event)", "event.preventDefault()",
                   "other.hidden = other !== panel", 'setAttribute("aria-current", "true")', "history.replaceState"):
        assert needed in script, needed
