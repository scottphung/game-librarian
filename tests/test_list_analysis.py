"""Most wishlisted on Steam and Most watched on Twitch, analyzed on the Trending games dashboard (versions 0.12-0.14).

Steam's list is read one snapshot per IGDB calculation, so a second save of the same calculation is not new data.
Twitch's list has no history (Twitch allows keeping its data 24 hours), so only its newest list is compared: with
Steam's newest saved list by IGDB id, and with Steam reviews where both measures exist. Since 0.14 the release radar
(test_release_radar.py) replaces 0.12's release timeline and 0.13's rank chart of Steam's top 10.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114). All names and numbers are made up.
"""

from datetime import date, datetime, timedelta, timezone

from game_librarian import dashboard, list_history, store, twitch, upcoming
from game_librarian.list_history import ListSnapshot
from game_librarian.upcoming import SignalEntry, SteamMatch
from tests.test_dashboard_trending import catalog_game, chart, dashboard_client, details_keys, item
from tests.test_trending_cross_platform import part
from tests.test_upcoming import entry, igdb_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NOW

UTC = timezone.utc


def moment(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def snapshot_rows(captured: datetime, calculated: datetime | None, positions: dict[int, int]) -> list[dict]:
    """Rows of one saved snapshot, as store.popularity_snapshot_rows returns them."""
    return [{"igdb_id": igdb_id, "captured_at": captured.isoformat(), "position": position,
             "calculated_at": calculated.isoformat() if calculated else None}
            for igdb_id, position in positions.items()]


def snapshot(positions: dict[int, int], day: int = 9) -> ListSnapshot:
    return ListSnapshot(moment(day), moment(day, 3), positions)


# ---------------------------------------------------------------------- comparable snapshots (pure)


def test_one_snapshot_per_igdb_calculation_the_newest_saved_of_it():
    forced = snapshot_rows(moment(7, 9), moment(7, 3), {1: 1, 2: 2})
    again = snapshot_rows(moment(7, 20), moment(7, 3), {2: 1, 3: 2})  # saved again from the same calculation
    next_day = snapshot_rows(moment(8), moment(8, 3), {1: 1})
    no_time = snapshot_rows(moment(9), None, {4: 1})  # IGDB gave no calculation time: a calculation of its own
    found = list_history.comparable_snapshots(next_day + no_time + forced + again)  # rows in any order
    assert [(found_one.captured_at, found_one.calculated_at) for found_one in found] == [
        (moment(7, 20), moment(7, 3)), (moment(8), moment(8, 3)), (moment(9), None)]
    assert found[0].positions == {2: 1, 3: 2}  # the newest save of that calculation, never a mix of two saves
    assert list_history.comparable_snapshots([]) == []


# ---------------------------------------------------------------------- the dashboard's views (pure)


def test_both_lists_are_matched_by_igdb_id_only_with_each_place_kept_apart():
    newest = snapshot({7: 1, 8: 2})
    twitch_items = [item("igdb-9", "Wish Two", igdb_id=9, twitch_position=3),  # a namesake of game 8: no match
                    item("igdb-8", "Series Category", igdb_id=8, twitch_position=12),
                    item("igdb-7", "Top Wish", igdb_id=7, twitch_position=40)]
    found = dashboard.list_overlap(newest, twitch_items, 100)
    assert [(row["key"], row["steam_position"], row["twitch_position"]) for row in found["rows"]] == [
        ("igdb-7", 1, 40), ("igdb-8", 2, 12)]  # ordered by Steam's place; neither place is changed
    assert (found["rows"][0]["steam_x"], found["rows"][0]["twitch_x"]) == (0.5, 39.5)  # one axis of places
    assert [tick["label"] for tick in found["ticks"]] == ["#1", "#25", "#50", "#75", "#100"]
    assert dashboard.list_overlap(None, twitch_items, 100)["rows"] == []  # no Steam list: nothing to match
    deep = dashboard.list_overlap(snapshot({7: 120}), twitch_items, 100)  # a deeper place widens the axis
    assert deep["depth"] == 120 and deep["rows"][0]["steam_x"] == round(119.5 / 120 * 100, 2)


def test_twitch_places_meet_steam_reviews_only_where_both_exist():
    catalog = {101: catalog_game(101, positive=900, total=1000), 102: catalog_game(102)}  # 102 has no usable reviews
    twitch_items = [item("igdb-1", "Console Hit", twitch_position=2), item("102", "No Reviews", twitch_position=3),
                    item("101", "Reviewed", twitch_position=5),
                    item("igdb-2", "On Steam, Not Stored", steam_verified=True, twitch_position=6)]
    found = dashboard.twitch_reception(twitch_items, catalog, {101: "2026-11-09T11:00:00+00:00"})
    assert [(row["key"], row["twitch_position"], row["split"]["positive"], row["split"]["negative"])
            for row in found["rows"]] == [("101", 5, 900, 100)]  # the others are left out, never counted as zero
    assert (found["compared"], found["listed"]) == (1, 4)
    assert found["rows"][0]["reviews_at"] == "2026-11-09T11:00:00+00:00"
    many = [item(str(100 + number), f"Game {number}", twitch_position=number) for number in range(1, 15)]
    reviewed = {100 + number: catalog_game(100 + number, positive=number, total=2 * number) for number in range(1, 15)}
    capped = dashboard.twitch_reception(many, reviewed, {})
    assert len(capped["rows"]) == upcoming.LIST_SIZE and capped["compared"] == 14
    assert [row["twitch_position"] for row in capped["rows"]] == list(range(1, 11))  # Twitch's order


# ---------------------------------------------------------------------- the page, from stored snapshots


def wish(number: int, *dates, platforms=("PC",)) -> dict:
    return upcoming.record_from_igdb(igdb_game(300 + number, f"Wish {number:02d}", *dates, platforms=platforms))


def entries(positions: dict[int, int], calculated: datetime) -> list[SignalEntry]:
    return [SignalEntry(igdb_id, 0.001 - position / 1e6, position, calculated)
            for igdb_id, position in positions.items()]


SAVED_BEFORE = {300 + number: number for number in range(1, 13)}  # Wish 01-12 at places 1-12
SAVED_NOW = {301: 1, 303: 2, 302: 3, 304: 4, 306: 5, 307: 6, 308: 7, 313: 8, 311: 9, 310: 10, 309: 11, 312: 12}


def save_history(settings, *, newest=True, twitch_list=True):
    """Steam's list saved three times: twice from one IGDB calculation (7 Nov), then from the next (9 Nov), plus a
    Twitch list and the IGDB records of every game. Wish 05 came out on 8 Nov; Wish 13 is new on 9 Nov."""
    tba = entry("PC", fmt=7)
    records = [wish(1, entry("PC", fmt=2, y=2027)), wish(2, entry("PC", fmt=3, y=2027)),
               wish(3, entry("PC", day=date(2026, 12, 4))), wish(4, entry("PC", fmt=1, y=2026, m=12)),
               wish(5, entry("PC", day=date(2026, 11, 8))), wish(6, tba),
               wish(7, entry("PlayStation 5", day=date(2026, 12, 1)), platforms=("PC", "PlayStation 5")),
               wish(8, entry("PC", day=date(2025, 1, 1), status=3), tba),
               *(wish(number, tba) for number in range(9, 14)),
               upcoming.record_from_igdb(igdb_game(314, "Game 14", entry("PC", day=date(2024, 1, 1)), steam=[114])),
               upcoming.record_from_igdb(igdb_game(315, "Console Only", entry("PlayStation 5", day=date(2025, 3, 1)),
                                                   platforms=("PlayStation 5",))),
               upcoming.record_from_igdb(igdb_game(316, "Game 02", entry("PC", day=date(2024, 1, 1)), steam=[102]))]
    matches = {314: SteamMatch("verified", 114, "IGDB lists Steam app 114, and Steam's name for it is the same."),
               316: SteamMatch("verified", 102, "IGDB lists Steam app 102, and Steam's name for it is the same.")}
    first_calculation, next_calculation = NOW - timedelta(days=2, hours=8), NOW - timedelta(hours=10)
    swapped = {**SAVED_BEFORE, 310: 11, 311: 10}  # the first save of the 7 Nov calculation
    conn = store.connect(settings.db_path)
    try:
        store.save_igdb_games(conn, records, matches, [], now=(NOW - timedelta(hours=1)).isoformat())
        store.save_popularity_snapshot(conn, "steam_wishlists", entries(swapped, first_calculation),
                                       now=(NOW - timedelta(days=2)).isoformat(timespec="seconds"))
        store.save_popularity_snapshot(conn, "steam_wishlists", entries(SAVED_BEFORE, first_calculation),
                                       now=(NOW - timedelta(days=1, hours=20)).isoformat(timespec="seconds"))
        if newest:
            store.save_popularity_snapshot(conn, "steam_wishlists", entries(SAVED_NOW, next_calculation),
                                           now=(NOW - timedelta(hours=1)).isoformat(timespec="seconds"))
        if twitch_list:
            store.save_twitch_top_games(conn, [
                twitch.TopCategory(1, "500", "Just Chatting", None),
                twitch.TopCategory(2, "501", "Wish 01 Early Access", 301),
                twitch.TopCategory(3, "502", "Game 14", 314),
                twitch.TopCategory(4, "503", "Console Only", 315),
                twitch.TopCategory(5, "504", "Game 02", 316)], now=(NOW - timedelta(hours=1)).isoformat())
    finally:
        conn.close()


def test_the_twitch_comparisons_use_the_newest_lists(games, settings):
    save_history(settings)
    client = dashboard_client(settings)
    board = client.get("/dashboard").text
    twitch_card = chart(board, "chart-twitch")
    assert details_keys(twitch_card) == ["114", "102"]  # catalog games with Steam reviews, in Twitch's order
    assert ("#3 on Twitch (opens in a new tab) Game 14 82.4% positive · 1,700 Steam reviews · Mixed #5 on Twitch "
            "(opens in a new tab) Game 02 18.2% positive · 1,100 Steam reviews · Mixed") in text_of(twitch_card)
    assert "2 of the 4 games in Twitch’s first 100 categories have Steam reviews here." in text_of(twitch_card)
    both = chart(board, "chart-both-lists")
    assert ("Wish 01 #1 on Steam’s list · #2 on Twitch · “Wish 01 Early Access” on Twitch (opens in a new tab)"
            in text_of(both))
    assert ("Place in Steam’s most-wishlisted upcoming games Place in Twitch’s most-watched categories"
            in text_of(both))  # the legend names both sources
    assert 'class="pair-dot is-steam" style="left: 0.5%"' in both and 'class="pair-dot is-twitch" style="left: 1.5%"' \
        in both  # #1 and #2 on one axis of 100 places
    assert "#1 top #25 #50 #75 #100" in text_of(both)  # the axis: #1, the top, at the left
    assert "Wish 01 1 2" in text_of(both)  # the table view
    assert "saved 9 Nov 2026, 12:00 UTC (IGDB calculated it 9 Nov 2026, 03:00 UTC)" in text_of(both)  # the newest
    assert 'href="https://www.twitch.tv/directory/game/Wish%2001%20Early%20Access"' in both
    assert "watched on Steam" not in text_of(board)  # Twitch's list is never called Steam's
    # Since 0.14 the dashboard has no release timeline and no rank chart of Steam's top 10.
    for gone in ('id="chart-release-timeline"', 'id="chart-wishlist-ranks"', "release timeline", "over time</h3>"):
        assert gone not in board, gone
    # The Trending page keeps browsing the list itself, in Steam's order.
    steam_list = part(client.get("/trending").text, "upcoming-steam_wishlists-title", "</section>")
    assert details_keys(steam_list)[:3] == ["igdb-301", "igdb-303", "igdb-302"]


def test_a_second_save_of_one_igdb_calculation_counts_once(games, settings):
    save_history(settings, newest=False, twitch_list=False)
    board = dashboard_client(settings).get("/dashboard").text
    both = text_of(chart(board, "chart-both-lists"))
    assert "No Twitch list from the last 24 hours is kept, so the lists cannot be matched." in both
    # Two saves of the 7 Nov calculation: the newer one is the list matched, and its time is the one shown.
    assert "saved 7 Nov 2026, 17:00 UTC (IGDB calculated it 7 Nov 2026, 05:00 UTC)" in both


def test_missing_lists_are_explained_without_charts(games, settings):
    board = dashboard_client(settings).get("/dashboard").text
    text = text_of(board)
    for phrase in ("No Twitch list from the last 24 hours is kept, so nothing is shown.",
                   "Steam’s most-wishlisted list has not been saved yet, so there is nothing to match."):
        assert phrase in text, phrase
    assert 'class="pair-dot' not in chart(board, "chart-both-lists")


def test_estimated_sales_come_first_and_this_years_reception_spans_the_page(games, settings):
    from fastapi.testclient import TestClient
    from game_librarian.web import create_app
    from tests.test_web_upcoming import TODAY, NoTwitch, configured

    local = TestClient(create_app(configured(settings), today=lambda: TODAY, now=lambda: NOW, twitch_client=NoTwitch()),
                       client=("127.0.0.1", 50000))  # Gamalytic's estimates go only to a browser on this computer
    board = local.get("/dashboard").text
    order = [board.index(f'id="{section}"') for section in ("sales", "upcoming", "now", "steam", "changes")]
    assert order == sorted(order)  # Estimated Steam copies sold first, then the Trending sections
    jump = board[board.index('class="view-switch trend-jump"'):]
    assert jump.index('href="#sales"') < jump.index('href="#upcoming"')
    this_year, since = f"Released in {TODAY.year} with strong Steam reception", f"Released since {TODAY.year - 5}"
    assert board.index(this_year) < board.index(since)  # the windows follow the calendar: nothing hard-coded
    for key in ("this-year", "four-years"):
        assert f'<figure class="chart-card is-wide" aria-labelledby="chart-{key}">' in board, key
    # The release radar leads Upcoming & Anticipated; the games in both lists stay in Trending now.
    assert board.index('id="chart-radar"') < board.index('id="chart-want_to_play"') < board.index('id="now"') \
        < board.index('id="chart-both-lists"')
    elsewhere = dashboard_client(settings).get("/dashboard").text  # any other browser: no estimates at all
    assert 'id="sales"' not in elsewhere and board.index('id="upcoming"') > board.index('id="sales"')
