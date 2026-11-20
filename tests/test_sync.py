"""Sync command: incremental refresh, no duplicates, preserved data when Steam fails."""

import hashlib
from dataclasses import replace
import json

import pytest

from game_librarian import __main__ as cli
from game_librarian import store
from game_librarian.curated import CuratedGame, load_curated
from game_librarian.sync import SyncOptions, run_sync
from tests.conftest import purchase_item, write_csv

GAMES = [CuratedGame(10, "Test Quest", ""), CuratedGame(20, "Other Game", ""), CuratedGame(30, "Third Game", "")]


@pytest.fixture
def conn(settings):
    connection = store.initialize(settings.db_path)
    yield connection
    connection.close()


def sync(conn, fake_steam, clock, games=GAMES, api_key=None, **options):
    """Run one sync against the fake Steam with a fixed clock and no console output."""
    with fake_steam.client(api_key=api_key) as client:
        return run_sync(conn, client, games, SyncOptions(**options), log=lambda message: None, now=clock)


def add_three_games(fake_steam):
    fake_steam.add_game(10, "Test Quest", tags=((122, 900), (492, 800)))
    fake_steam.add_game(20, "Other Game", tags=((19, 900),), positive=1, negative=0)
    fake_steam.add_game(30, "Third Game")


def latest_run(conn):
    run = store.recent_sync_runs(conn, limit=1)[0]
    return run["status"], json.loads(run["summary"])


def test_first_sync_saves_details_tags_cover_reviews_and_records_the_run(conn, fake_steam, clock):
    add_three_games(fake_steam)
    report = sync(conn, fake_steam, clock)

    assert report.status() == "success"
    row = store.game_rows(conn, [10])[10]
    assert row["name"] == "Test Quest" and row["details_status"] == "ok"
    assert row["cover_image_url"].endswith("/steam/apps/10/library_600x900.jpg?t=1")
    assert json.loads(row["preview_clip"]) == {
        "title": "Launch Trailer",
        "webm": "https://video.akamai.steamstatic.com/store_trailers/10/0/abc0/1/microtrailer.webm",
        "mp4": "https://video.akamai.steamstatic.com/store_trailers/10/0/abc0/1/microtrailer.mp4",
        "poster": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/100/movie_full.jpg?t=1",
    }
    assert row["screenshot_url"].endswith("/steam/apps/10/ss_first.jpg?t=1")
    assert store.tags_for(conn, [10])[10] == ["RPG", "Indie"]
    assert (row["review_total"], row["review_positive"], row["review_score_desc"]) == (1000, 900, "Very Positive")
    excerpts = store.excerpts_for(conn, [10, 20])
    assert len(excerpts[10]["positive"]) == 3 and len(excerpts[10]["negative"]) == 3
    assert len(excerpts[20]["positive"]) == 1 and excerpts[20]["negative"] == []  # fewer than three: keep what exists

    status, summary = latest_run(conn)
    assert status == "success"
    assert summary["counts"]["details_new"] == 3 and summary["counts"]["reviews_refreshed"] == 3
    assert summary["discovery"].startswith("skipped")  # no API key


def test_second_sync_skips_fresh_data_and_calls_steam_only_when_due(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    calls_after_first = len(fake_steam.requests)

    clock.advance(hours=1)
    report = sync(conn, fake_steam, clock)
    assert report.status() == "success"
    assert len(fake_steam.requests) == calls_after_first  # nothing was due
    assert report.counts["details_skipped_fresh"] == 3 and report.counts["reviews_skipped_fresh"] == 3

    clock.advance(hours=24)  # reviews are now older than a day; details are not a week old yet
    report = sync(conn, fake_steam, clock)
    assert report.counts["reviews_refreshed"] == 3 and report.counts["details_skipped_fresh"] == 3


def test_title_change_updates_the_same_row_without_duplicates(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    fake_steam.details[10]["name"] = "Test Quest: Definitive Edition"

    clock.advance(days=8)  # older than the details refresh age
    report = sync(conn, fake_steam, clock)

    assert report.counts["details_changed"] == 1 and report.counts["details_unchanged"] == 2
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 3
    row = store.game_rows(conn, [10])[10]
    assert row["name"] == "Test Quest: Definitive Edition"
    assert [r["appid"] for r in store.matching_games(conn, ["definitive"], [])] == [10]
    assert [r["appid"] for r in store.matching_games(conn, ["test", "quest"], [])] == [10]  # still found by its curated title


def test_steam_unavailable_keeps_the_last_good_data_and_stops_early(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    before = dict(store.game_rows(conn, [10])[10])

    fake_steam.down = True
    fake_steam.requests.clear()
    clock.advance(days=8)
    report = sync(conn, fake_steam, clock)

    assert report.status() == "failed"
    assert report.stopped_early  # three failed requests in a row, then no more calls
    assert len(fake_steam.requests) == 3 * 4  # three operations, four attempts each
    after = store.game_rows(conn, [10])[10]
    for column in ("name", "about_blocks", "cover_image_url", "review_total", "details_updated_at"):
        assert after[column] == before[column]
    assert store.tags_for(conn, [10])[10] == ["RPG", "Indie"]
    assert len(store.excerpts_for(conn, [10])[10]["positive"]) == 3
    status, summary = latest_run(conn)
    assert status == "failed" and "Steam looks unavailable" in summary["stopped_early"]
    assert store.last_successful_sync(conn)["status"] == "success"  # the earlier run is still the last good one


def test_refreshes_that_failed_are_retried_on_the_next_sync(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    fake_steam.down = True
    clock.advance(hours=1)
    sync(conn, fake_steam, clock, force=True)  # details of games 10 and 20 fail; game 30 is not attempted

    fake_steam.down = False
    clock.advance(hours=1)
    report = sync(conn, fake_steam, clock)  # no --force: only the failed refreshes are due again

    assert report.status() == "success"
    assert report.counts["details_unchanged"] == 2 and report.counts["details_skipped_fresh"] == 1
    assert all(row["details_status"] == "ok" for row in store.game_rows(conn, [10, 20, 30]).values())


def test_one_failing_game_makes_the_run_partial(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.failing_appids.add(20)
    report = sync(conn, fake_steam, clock)

    assert report.status() == "partial"
    rows = store.game_rows(conn, [10, 20, 30])
    assert rows[10]["details_status"] == "ok" and rows[30]["details_status"] == "ok"
    assert rows[20]["details_status"] == "error" and rows[20]["details_updated_at"] is None
    assert "details 20" in report.errors[0]


def test_failing_tag_service_keeps_stored_tags_and_media_but_refreshes_details(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    before = store.game_rows(conn, [10])[10]
    fake_steam.failing_paths.add("IStoreBrowseService")
    fake_steam.details[10]["short_description"] = "A new blurb."

    clock.advance(days=8)
    report = sync(conn, fake_steam, clock, skip_reviews=True)

    assert report.status() == "partial"
    after = store.game_rows(conn, [10])[10]
    assert after["short_description"] == "A new blurb."
    assert store.tags_for(conn, [10])[10] == ["RPG", "Indie"]
    for column in ("cover_image_url", "preview_clip", "screenshot_url"):
        assert after[column] == before[column] and after[column]


def test_games_without_an_all_ages_trailer_get_no_clip(conn, fake_steam, clock):
    fake_steam.add_game(40, "No Trailer", trailers=())
    fake_steam.add_game(50, "Mature Trailer Only", trailers=(("Red Band Trailer", False),))
    sync(conn, fake_steam, clock, games=[CuratedGame(40, "No Trailer", ""), CuratedGame(50, "Mature", "")])
    rows = store.game_rows(conn, [40, 50])
    assert rows[40]["preview_clip"] is None and rows[50]["preview_clip"] is None
    assert rows[40]["screenshot_url"] and rows[50]["screenshot_url"]  # a still image is still available


def test_unavailable_store_details_keep_previous_data(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    fake_steam.details[30] = None  # e.g. the app was delisted

    clock.advance(days=8)
    report = sync(conn, fake_steam, clock, skip_reviews=True)

    assert report.counts["details_unavailable"] == 1
    row = store.game_rows(conn, [30])[30]
    assert row["details_status"] == "unavailable" and row["name"] == "Third Game"


def test_sync_command_reads_but_never_writes_curated_files(settings, fake_steam, monkeypatch):
    """End to end through `python -m game_librarian sync`, with the fake Steam behind it."""
    curated = settings.curated_dir
    write_csv(curated / "games.csv", [{"steam_appid": str(g.appid), "title": g.title, "note": ""} for g in GAMES])
    write_csv(curated / "awards.csv", [{
        "organization": "The Game Awards", "year": "2023", "category": "Game of the Year", "result": "winner",
        "award_title": "Test Quest", "steam_appid": "10", "steam_mapping": "same",
        "source_url": "https://example.invalid/winners", "review_status": "verified"}])
    write_csv(curated / "classifications.csv", [{
        "steam_appid": "10", "label": "Indie", "review_status": "verified",
        "evidence_url": "https://example.invalid/indie", "evidence_summary": "Self-published by a two-person studio."}])
    fingerprints = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in curated.glob("*.csv")}
    add_three_games(fake_steam)
    fake_steam.details[10]["name"] = "Renamed On Steam"  # Steam data differs from the curated title
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))

    assert cli.main(["sync", "--force"]) == 0

    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in curated.glob("*.csv")} == fingerprints
    data = load_curated(curated)
    assert [award.award_title for award in data.awards_for(10)] == ["Test Quest"]
    assert set(data.labels_for(10)) == {"Indie"}


def test_discovery_pages_through_changes_and_refreshes_changed_games(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.app_list_pages = [
        {"after": 0, "more": True, "apps": [{"appid": 10, "name": "Test Quest", "last_modified": 100}, {"appid": 15, "name": "Uncurated", "last_modified": 100}]},
        {"after": 15, "more": False, "apps": [{"appid": 20, "name": "Other Game", "last_modified": 100}]},
    ]
    report = sync(conn, fake_steam, clock, api_key="test-key")
    assert report.discovery.startswith("ok: 2 page(s)")
    assert report.counts["discovered_new"] == 3
    assert store.catalog_counts(conn)["discovered"] == 3
    first_since = store.get_state(conn, "discovery_modified_since")

    # Steam reports a change to game 10 only; the next run refreshes just that game's details.
    fake_steam.app_list_pages = [{"after": 0, "more": False, "apps": [{"appid": 10, "name": "Test Quest", "last_modified": 200}]}]
    fake_steam.details[10]["name"] = "Test Quest Remastered"
    fake_steam.requests.clear()
    clock.advance(hours=2)
    report = sync(conn, fake_steam, clock, api_key="test-key")

    app_list_call = [params for path, params in fake_steam.requests if "GetAppList" in path][0]
    assert json.loads(app_list_call["input_json"])["if_modified_since"] == int(first_since)
    assert report.counts["discovered_changed"] == 1
    assert report.counts["details_changed"] == 1 and report.counts["details_skipped_fresh"] == 2
    assert fake_steam.count("/api/appdetails") == 1
    assert store.game_rows(conn, [10])[10]["name"] == "Test Quest Remastered"


def test_discovery_failure_does_not_block_the_rest_of_the_sync(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.failing_paths.add("GetAppList")
    report = sync(conn, fake_steam, clock, api_key="test-key")
    assert report.status() == "partial"
    assert report.discovery.startswith("failed")
    assert report.counts["details_new"] == 3


def test_game_removed_from_the_curated_list_is_hidden_not_deleted(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    sync(conn, fake_steam, clock, games=GAMES[:2])
    assert len(store.matching_games(conn, [], [])) == 2
    assert conn.execute("SELECT is_curated FROM games WHERE appid = 30").fetchone()[0] == 0


def test_limit_and_appid_options_select_games(conn, fake_steam, clock):
    add_three_games(fake_steam)
    report = sync(conn, fake_steam, clock, limit=1)
    assert report.counts["selected_games"] == 1 and report.counts["details_new"] == 1
    report = sync(conn, fake_steam, clock, appids=[30])
    assert report.counts["selected_games"] == 1 and report.counts["details_new"] == 1


def test_an_interrupted_run_is_marked_failed_when_the_next_run_starts(conn, fake_steam, clock):
    store.start_sync_run(conn, now="2026-11-09T10:00:00+00:00")  # never finished, as if the process died
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock)
    runs = store.recent_sync_runs(conn, limit=2)
    assert [run["status"] for run in runs] == ["success", "failed"]
    assert "stopped before it finished" in store.run_problem(runs[1])


# ---------------------------------------------------------------------- prices, player and review snapshots


def priced_sync(conn, fake_steam, clock, country="US", **options):
    """Like sync(), for one store region."""
    with fake_steam.client() as client:
        return run_sync(conn, client, GAMES, SyncOptions(**options), country=country, log=lambda message: None,
                        now=clock)


def test_prices_are_saved_for_the_store_region_with_discounts_free_and_unsold_games(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.prices[10] = purchase_item("Test Quest", 4000, discount=25, ends=1_796_234_400)
    fake_steam.prices[20] = {"is_free": True}
    fake_steam.prices[30] = {}  # shown by Steam, but no purchase option (like GTA V Legacy in the US store)
    fake_steam.currency = "GBP"
    report = priced_sync(conn, fake_steam, clock, country="GB")

    assert report.status() == "success"
    rows = store.price_rows(conn, [10, 20, 30])
    assert (rows[10]["status"], rows[10]["final_cents"], rows[10]["original_cents"], rows[10]["discount_percent"]) \
        == ("priced", 3000, 4000, 25)
    assert (rows[10]["country_code"], rows[10]["currency"], rows[10]["final_text"]) == ("GB", "GBP", "$30.00")
    assert rows[10]["discount_ends_at"] == "2026-12-02T18:00:00+00:00"
    assert (rows[20]["status"], rows[20]["final_cents"]) == ("free", None)
    assert rows[30]["status"] == "not_sold"  # never stored as free
    price_calls = [json.loads(params["input_json"]) for path, params in fake_steam.requests
                   if "GetItems" in path and not json.loads(params["input_json"])["data_request"]]
    assert len(price_calls) == 1 and price_calls[0]["context"]["country_code"] == "GB"
    assert sum(1 for _, params in fake_steam.requests if params.get("filters") == "price_overview") == 1
    assert (report.counts["prices_refreshed"], report.counts["prices_discounted"]) == (3, 1)


def test_prices_refresh_by_age_or_region_and_a_failed_check_keeps_the_old_price(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.prices.pop(30)  # Steam returns no store item for it in this region
    priced_sync(conn, fake_steam, clock)
    assert store.price_rows(conn, [30])[30]["status"] == "unavailable"
    first_check = store.price_rows(conn, [10])[10]["checked_at"]

    clock.advance(hours=1)
    assert priced_sync(conn, fake_steam, clock).counts["prices_skipped_fresh"] == 3  # checked an hour ago
    assert priced_sync(conn, fake_steam, clock, country="CA").counts["prices_refreshed"] == 3  # another region

    clock.advance(hours=4)
    fake_steam.failing_paths.add("IStoreBrowseService")
    report = priced_sync(conn, fake_steam, clock, skip_players=True, skip_reviews=True)
    assert report.status() == "failed" and "prices for 3 apps" in report.errors[0]
    row = store.price_rows(conn, [10])[10]
    assert row["country_code"] == "CA" and row["checked_at"] > first_check  # the last good check stays, and ages


def test_player_counts_become_dated_snapshots_and_a_missing_count_is_not_a_failure(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.players.update({10: 5_000, 20: 0})  # 30: Steam answers 404, as it does for apps without a count
    report = sync(conn, fake_steam, clock)
    assert report.status() == "success" and report.failures == 0
    assert (report.counts["players_counted"], report.counts["players_unavailable"]) == (2, 1)
    latest = {appid: row["players"] for appid, row in store.latest_player_counts(conn).items()}
    assert latest == {10: 5_000, 20: 0, 30: None}

    clock.advance(hours=1)
    assert sync(conn, fake_steam, clock).counts["players_skipped_fresh"] == 3  # not due yet
    clock.advance(hours=4)
    fake_steam.players[10] = 7_500
    sync(conn, fake_steam, clock)
    history = store.player_history(conn, "2026-01-01T00:00:00+00:00")
    assert [row["players"] for row in history[10]] == [5_000, 7_500]  # history grows; nothing is overwritten


def test_skip_options_leave_prices_and_players_alone(conn, fake_steam, clock):
    add_three_games(fake_steam)
    report = sync(conn, fake_steam, clock, skip_prices=True, skip_players=True)
    assert fake_steam.count("GetNumberOfCurrentPlayers") == 0
    assert store.price_rows(conn, [10, 20, 30]) == {} and store.latest_player_counts(conn) == {}
    assert report.counts["prices_skipped_by_option"] == report.counts["players_skipped_by_option"] == 3


def test_steam_failing_during_player_counts_stops_the_run_after_three_failures(conn, fake_steam, clock):
    add_three_games(fake_steam)
    sync(conn, fake_steam, clock, skip_players=True)
    fake_steam.failing_paths.add("GetNumberOfCurrentPlayers")
    report = sync(conn, fake_steam, clock)
    assert report.status() == "failed" and report.stopped_early
    assert store.latest_player_counts(conn) == {}


def test_each_review_refresh_adds_a_review_snapshot_but_never_a_zero_total(conn, fake_steam, clock):
    add_three_games(fake_steam)
    fake_steam.add_game(40, "Quiet Game", positive=0, negative=0, totals=(0, 0, None))
    games = GAMES + [CuratedGame(40, "Quiet Game", "")]
    sync(conn, fake_steam, clock, games=games)
    clock.advance(hours=25)
    fake_steam.reviews[10]["totals"] = (950, 100, "Very Positive")
    sync(conn, fake_steam, clock, games=games)
    history = store.review_history(conn, "2026-01-01T00:00:00+00:00")
    assert [(row["total"], row["positive"]) for row in history[10]] == [(1_000, 900), (1_050, 950)]
    assert 40 not in history  # Steam's empty answer is stored as zeros, which are not a real total


def test_the_sync_command_reads_prices_for_the_configured_store_region(settings, fake_steam, monkeypatch):
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "10", "title": "Test Quest", "note": ""}])
    fake_steam.add_game(10, "Test Quest")
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, steam_country="JP"))
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    assert cli.main(["sync"]) == 0
    conn = store.connect(settings.db_path)
    try:
        assert store.price_rows(conn, [10])[10]["country_code"] == "JP"
    finally:
        conn.close()
