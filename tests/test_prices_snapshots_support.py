"""Support for prices and snapshots: the Steam client's new reads, the store-region setting, the snapshot
seeding of an existing catalog and curated publisher aliases. (The dashboard's review splits and sparklines that
were tested here were replaced in 0.15: see test_dashboard_trending.py and test_momentum.py.)"""

import json

import httpx
import pytest

from game_librarian import config, store
from game_librarian.config import DEFAULT_CURATED_DIR
from game_librarian.curated import load_curated
from game_librarian.steam import SteamError, SteamNotFound
from tests.conftest import write_csv
from tests.test_steam_client import API_URL, FakeTime, make_client

# ---------------------------------------------------------------------- Steam client


def test_not_found_is_its_own_error_and_is_not_retried():
    client, calls = make_client([httpx.Response(404)], FakeTime())
    with pytest.raises(SteamNotFound):
        client.get_json(API_URL, {})
    assert len(calls) == 1 and issubclass(SteamNotFound, SteamError)


def test_current_players_reads_the_count_and_treats_404_or_no_result_as_no_count():
    ok, _ = make_client([httpx.Response(200, json={"response": {"player_count": 460680, "result": 1}})], FakeTime())
    assert ok.get_current_players(730) == 460680
    missing, calls = make_client([httpx.Response(404)], FakeTime())
    assert missing.get_current_players(1) is None and len(calls) == 1  # what Steam answered live for app 1
    no_result, _ = make_client([httpx.Response(200, json={"response": {"result": 42}})], FakeTime())
    assert no_result.get_current_players(5) is None


def test_purchase_options_send_no_data_request_and_the_currency_comes_from_appdetails():
    client, calls = make_client([httpx.Response(200, json={"response": {"store_items": [
        {"appid": 10, "success": 1, "is_free": True}, {"appid": 0, "success": 15}]}})], FakeTime())
    assert list(client.get_purchase_options([10, 11], country="GB")) == [10]  # unknown apps are left out
    payload = json.loads(calls[0].url.params["input_json"])
    assert payload["data_request"] == {} and payload["context"]["country_code"] == "GB"
    priced, calls = make_client([httpx.Response(200, json={"7": {"success": True, "data": {"price_overview": {
        "currency": "USD", "final": 749}}}})], FakeTime())
    assert priced.get_store_currency(7, "US") == "USD" and calls[0].url.params["filters"] == "price_overview"
    free, _ = make_client([httpx.Response(200, json={"7": {"success": True, "data": []}})], FakeTime())
    assert free.get_store_currency(7) is None  # Steam sends an empty list for a free game


# ---------------------------------------------------------------------- settings and storage


def test_the_steam_store_region_defaults_to_us_and_ignores_malformed_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("GAME_LIBRARIAN_STEAM_COUNTRY", raising=False)
    assert config.load_settings().steam_country == "US"
    for value, expected in (("gb", "GB"), (" ca ", "CA"), ("USA", "US"), ("", "US"), ("1x", "US")):
        monkeypatch.setenv("GAME_LIBRARIAN_STEAM_COUNTRY", value)
        assert config.load_settings().steam_country == expected, value


def test_initialize_seeds_review_snapshots_from_saved_review_totals_once(tmp_path):
    db_path = tmp_path / "catalog.sqlite3"
    conn = store.initialize(db_path)
    conn.executescript(
        "INSERT INTO games (appid, curated_title, reviews_updated_at, review_total, review_positive, review_negative) "
        "VALUES (1, 'Reviewed', '2026-11-16T02:00:00+00:00', 1000, 900, 100),"
        " (2, 'Zeros', '2026-11-16T02:00:00+00:00', 0, 0, 0),"
        " (3, 'Never fetched', NULL, NULL, NULL, NULL),"
        " (4, 'Inconsistent', '2026-11-16T02:00:00+00:00', 10, 50, 0);")
    conn.close()
    for _ in range(2):  # every start runs the seeding; it never adds a row twice
        conn = store.initialize(db_path)
        rows = conn.execute("SELECT appid, captured_at, total, positive, negative FROM review_snapshots").fetchall()
        conn.close()
        assert [tuple(row) for row in rows] == [(1, "2026-11-16T02:00:00+00:00", 1000, 900, 100)]


def test_publisher_aliases_are_optional_and_only_valid_verified_rows_are_used(tmp_path):
    assert load_curated(tmp_path).publisher_aliases == {}  # no file: no aliases
    alias = {"steam_publisher": "WB Games", "publisher": "Warner Bros. Games", "review_status": "verified",
             "source_url": "https://example.invalid/wb", "note": "", "checked_on": "2026-11-19"}
    write_csv(tmp_path / "publisher_aliases.csv", [
        alias,
        {**alias, "steam_publisher": "Old Name", "review_status": "needs_review"},  # loaded, not used
        {**alias, "steam_publisher": "WARNER BROS. GAMES"},  # already the same publisher
        {**alias, "steam_publisher": "Another", "source_url": "not a link"},
        {**alias, "steam_publisher": "wb games"},  # a duplicate spelling
    ])
    data = load_curated(tmp_path)
    assert data.publisher_aliases == {"wb games": "Warner Bros. Games"}
    problems = " | ".join(problem for problem in data.problems if problem.startswith("publisher_aliases.csv"))
    for text in ("already grouped as the same name", "source_url must start", "duplicate steam_publisher"):
        assert text in problems, text
    assert load_curated(DEFAULT_CURATED_DIR).publisher_aliases == {
        "warner bros interactive entertainment": "Warner Bros. Games", "wb games": "Warner Bros. Games"}

