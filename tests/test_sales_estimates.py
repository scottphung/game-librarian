"""Estimated Steam copies sold from Gamalytic's free list endpoint (version 0.11), for local evaluation only.

The fake answers are shaped like the live ones of 2026-11-20 (gamalytic.py); conftest's fake_gamalytic stands in for
the API in every test, so no test reaches it. The web tests use the fourteen synced catalog games of test_web.py
(app ids 101-114, all sold for $19.99). All names and numbers are made up."""

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from game_librarian import __main__ as cli
from game_librarian import dashboard, gamalytic, store
from game_librarian.config import USER_AGENT
from game_librarian.gamalytic import GamalyticError, GamalyticRateLimited, parse_list
from game_librarian.prepare import FREE, PriceFacts
from game_librarian.web import create_app
from tests.conftest import FakeGamalytic
from tests.test_dashboard_trending import catalog_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_discovery import iso, save_players, with_conn
from tests.test_web_upcoming import NOW, TODAY, NoTwitch, configured

UTC = timezone.utc
LOCAL = ("127.0.0.1", 50000)


def catalog_db(settings, appids):
    """A database whose catalog lists `appids` (estimates refer to catalog games)."""
    conn = store.initialize(settings.db_path)
    store.sync_curated_games(conn, [(appid, f"Game {appid}") for appid in appids])
    return conn


def collect(conn, fake, appids, moment):
    with fake.client() as client:
        return gamalytic.refresh_estimates(conn, client, appids, log=lambda message: None, now=lambda: moment)


def spent(conn, requests, moment):
    """A finished collection of this site that sent `requests` requests at `moment`."""
    run = store.start_source_run(conn, gamalytic.SOURCE, now=iso(moment))
    store.finish_source_run(conn, run, status="success", summary={"counts": {"requests": requests}}, now=iso(moment))


def stored(conn, appid):
    return [(row["collected_at"], row["copies_sold"]) for row in conn.execute(
        "SELECT * FROM sales_estimates WHERE appid = ? ORDER BY collected_at", (appid,))]


# ---------------------------------------------------------------------- the API answer and the client


def test_an_answer_gives_estimates_only_for_requested_games_and_never_a_zero():
    body = {"pages": 1, "total": 7, "cacheTimestamp": FakeGamalytic.CACHE_TIMESTAMP, "result": [
        {"steamId": 101, "copiesSold": 2_500_000, "revenue": 9},  # a revenue field is ignored
        {"steamId": "102", "copiesSold": 40_000},  # the docs describe steamId as a string
        {"steamId": 103, "copiesSold": 0},  # zero copies: no estimate, never a zero bar
        {"steamId": 104},  # no copiesSold at all
        {"steamId": 105, "copiesSold": True},  # malformed
        {"steamId": 106, "copiesSold": -5},
        {"steamId": 999, "copiesSold": 5},  # not asked for
        "not a game",
    ]}
    answer = parse_list(body, [101, 102, 103, 104, 105, 106])
    assert answer.copies_sold == {101: 2_500_000, 102: 40_000}
    assert answer.cached_at == datetime(2026, 11, 20, 14, 0, 38, 291000, tzinfo=UTC)
    assert parse_list({"result": []}, [101]).cached_at is None  # Gamalytic does not document cacheTimestamp
    for unexpected in ({}, {"result": {}}, [], "text"):
        with pytest.raises(GamalyticError, match="unexpected shape"):
            parse_list(unexpected, [101])
    with pytest.raises(GamalyticError, match="2 pages"):  # the other page's games would look as if they had none
        parse_list({"pages": 2, "result": [{"steamId": 101, "copiesSold": 1}]}, [101, 102])


def test_the_client_asks_once_per_batch_with_the_documented_parameters_and_no_key():
    fake = FakeGamalytic()
    fake.copies = {101: 2_500_000, 103: 7}
    with fake.client() as client:
        answer = client.copies_sold([101, 102, 103])
        with pytest.raises(ValueError):
            client.copies_sold([])
        with pytest.raises(ValueError):
            client.copies_sold(list(range(1, gamalytic.APPIDS_PER_REQUEST + 2)))
    assert answer.copies_sold == {101: 2_500_000, 103: 7}  # 102 is left out of the answer: no estimate
    [request] = fake.requests
    assert str(request.url).startswith("https://api.gamalytic.com/steam-games/list?")
    assert dict(request.url.params) == {"appids": "101,102,103", "fields": "steamId,copiesSold", "limit": "3"}
    assert request.headers["User-Agent"] == USER_AGENT
    assert not {"api-key", "authorization"} & {name.lower() for name in request.headers}  # never a key


def test_server_errors_are_tried_again_but_a_rate_limit_or_a_refusal_is_not():
    fake = FakeGamalytic()
    fake.copies = {101: 50}
    fake.responses = [503, "timeout"]  # then a normal answer
    with fake.client() as client:
        assert client.copies_sold([101]).copies_sold == {101: 50}
        assert client.requests_sent == 3
    fake.requests.clear()
    fake.responses = [503, 502, "timeout"]
    with fake.client() as client, pytest.raises(GamalyticError, match="after 3 attempts"):
        client.copies_sold([101])
    assert len(fake.requests) == gamalytic.MAX_ATTEMPTS
    for status, error in ((429, GamalyticRateLimited), (401, GamalyticError)):
        fake.requests.clear()
        fake.responses = [status]
        with fake.client() as client, pytest.raises(error) as raised:
            client.copies_sold([101])
        assert len(fake.requests) == 1  # never tried again
    assert "HTTP 401: planned answer 401 (test)" in str(raised.value)  # Gamalytic's own message is kept


# ---------------------------------------------------------------------- collections, caching and limits


def test_a_collection_saves_one_row_per_game_and_asks_again_only_when_due(settings, clock):
    appids = list(range(1001, 1251))  # 250 catalog games: 100 + 100 + 50
    conn = catalog_db(settings, appids)
    fake = FakeGamalytic()
    fake.copies = {appid: appid * 10 for appid in appids if appid != 1002}
    report = collect(conn, fake, appids, clock())
    assert report.status() == "success"
    assert [len(request.url.params["appids"].split(",")) for request in fake.requests] == [100, 100, 50]
    assert report.counts == {"games_asked": 250, "estimates": 249, "no_estimate": 1, "requests": 3}
    assert stored(conn, 1001) == [("2026-11-09T12:00:00+00:00", 10_010)]
    assert stored(conn, 1002) == [("2026-11-09T12:00:00+00:00", None)]  # no estimate: saved as none, never 0
    row = store.latest_sales_estimates(conn, [1001])[1001]
    assert (row["source"], row["source_cached_at"]) == ("Gamalytic", "2026-11-20T14:00:38+00:00")
    run = store.recent_source_runs(conn, gamalytic.SOURCE, limit=1)[0]
    assert run["status"] == "success" and '"requests": 3' in run["summary"]

    assert gamalytic.due_appids(conn, appids, clock() + timedelta(days=6)) == []  # the cache: nothing is asked
    assert gamalytic.due_appids(conn, [1001, 5000], clock()) == [5000]  # a game never asked is due at once
    assert gamalytic.due_appids(conn, appids, clock() + timedelta(days=7)) == appids
    assert gamalytic.due_appids(conn, [1003, 1001], clock(), force=True) == [1001, 1003]

    clock.advance(days=8)
    fake.copies[1001] = 12_000
    collect(conn, fake, [1001], clock())
    assert stored(conn, 1001) == [("2026-11-09T12:00:00+00:00", 10_010), ("2026-11-17T12:00:00+00:00", 12_000)]
    assert store.latest_sales_estimates(conn, [1001])[1001]["copies_sold"] == 12_000  # older rows are kept
    assert gamalytic.due_appids(conn, [1001, 1002], clock()) == [1002]  # the oldest collection first
    conn.close()


def test_a_rate_limit_or_a_failure_stops_the_run_and_keeps_the_stored_estimates(settings, clock):
    appids = list(range(1001, 1251))  # 250 games: three requests
    conn = catalog_db(settings, appids)
    fake = FakeGamalytic()
    fake.copies = {appid: 500 for appid in appids}
    collect(conn, fake, appids, clock())
    clock.advance(days=8)
    fake.requests.clear()
    fake.copies = {appid: 600 for appid in appids}
    fake.responses = [200, 429]
    report = collect(conn, fake, appids, clock())
    assert len(fake.requests) == 2  # the third batch is not asked after the 429
    assert report.status() == "partial"
    assert report.counts["games_asked"] == 100 and report.counts["games_not_asked"] == 150
    assert stored(conn, 1001)[-1][1] == 600 and stored(conn, 1250) == [("2026-11-09T12:00:00+00:00", 500)]
    run = store.recent_source_runs(conn, gamalytic.SOURCE, limit=1)[0]
    assert run["status"] == "partial"
    assert store.run_problem(run).startswith("Gamalytic answered 429 (too many requests), so the run stopped")

    fake.requests.clear()
    fake.responses = ["timeout"] * gamalytic.MAX_ATTEMPTS
    report = collect(conn, fake, appids[100:], clock() + timedelta(hours=1))  # two batches
    assert report.status() == "failed"
    assert len(fake.requests) == gamalytic.MAX_ATTEMPTS  # the first batch's attempts only: the run stopped there
    assert "could not be reached after 3 attempts (ReadTimeout)" in report.errors[0]
    assert stored(conn, 1250) == [("2026-11-09T12:00:00+00:00", 500)]
    conn.close()


def test_this_site_sends_at_most_its_daily_budget_in_any_24_hours(settings, clock):
    appids = list(range(1001, 1151))
    conn = catalog_db(settings, appids)
    fake = FakeGamalytic()
    spent(conn, gamalytic.REQUESTS_PER_DAY - 1, clock() - timedelta(hours=23))
    assert gamalytic.requests_left(conn, clock()) == 1
    report = collect(conn, fake, appids, clock())  # two batches, room for one request
    assert len(fake.requests) == 1 and report.status() == "partial"
    assert report.counts["games_not_asked"] == 50
    assert report.stopped_early == ("Stopped: this site sends Gamalytic at most 50 requests in 24 hours (the free "
                                    "limit is 250 a day); 50 games wait for a later run.")
    assert gamalytic.requests_left(conn, clock()) == 0
    report = collect(conn, fake, appids[100:], clock() + timedelta(minutes=5))
    assert len(fake.requests) == 1 and report.status() == "failed"  # no request at all
    # 24 hours after the first run its 49 requests no longer count; the request sent since still does.
    assert gamalytic.requests_left(conn, clock() + timedelta(hours=1, seconds=1)) == gamalytic.REQUESTS_PER_DAY - 1
    assert gamalytic.REQUESTS_PER_DAY < gamalytic.FREE_REQUESTS_PER_DAY
    conn.close()


# ---------------------------------------------------------------------- the chart (pure)


def chart_game(appid, name, *, upcoming=False):
    return {"appid": appid, "name": name, "details_url": f"/games/{appid}", "image_url": None, "lists": ["List"],
            "game": replace(catalog_game(appid), upcoming=upcoming)}


def estimate(copies, at="2026-11-09T12:30:00+00:00", cached="2026-11-09T06:00:00+00:00"):
    return {"copies_sold": copies, "collected_at": at, "source_cached_at": cached}


def test_the_chart_ranks_released_paid_games_and_names_every_game_it_leaves_out():
    games = [chart_game(appid, f"Game {appid}") for appid in range(1, 13)]
    games += [chart_game(20, "Free Game"), chart_game(21, "Unsold Game"), chart_game(22, "Unpriced Game"),
              chart_game(23, "Future Game", upcoming=True), chart_game(24, "Unknown Game"),
              chart_game(25, "Unasked Game")]
    estimates = {appid: estimate(appid * 1_000) for appid in range(1, 13)}
    estimates.update({20: estimate(90_000_000), 21: estimate(5), 22: estimate(5), 23: estimate(5), 24: estimate(None)})
    prices = {appid: {"status": "priced"} for appid in (*range(1, 13), 23, 24, 25)}
    prices.update({20: {"status": FREE}, 21: {"status": "not_sold"}})  # 22 has no stored price
    chart = dashboard.sales_chart(games, estimates, prices, datetime(2026, 11, 9, 13, tzinfo=UTC))
    assert [item["appid"] for item in chart["ranked"]] == list(range(12, 0, -1))  # the cohort, largest first
    ring = chart["ring"]
    # Five named slices and "Other tracked games" for the other seven; the slices add up to the total in the middle.
    assert [part["appid"] if part["kind"] == "game" else "other" for part in ring["parts"]] == [12, 11, 10, 9, 8, "other"]
    assert [item["appid"] for item in ring["other"]["members"]] == [7, 6, 5, 4, 3, 2, 1]
    assert (ring["total"], ring["games"], ring["named"]) == (78_000, 12, dashboard.RING_NAMED)
    assert sum(part["copies"] for part in ring["parts"]) == ring["total"] == sum(range(1_000, 13_000, 1_000))
    assert sum(part["share_tenths"] for part in ring["parts"]) == 1_000  # the shares shown add up to 100.0%
    # Each slice's angle is its share of the total, clockwise from the top, and the last one closes the ring.
    assert [(part["start"], part["end"]) for part in ring["parts"]][:2] == [(0.0, 55.38), (55.38, 106.15)]
    assert all(a["end"] == b["start"] for a, b in zip(ring["parts"], ring["parts"][1:])) and ring["parts"][-1]["end"] == 360
    assert ring["parts"][0]["badge"] is not None and ring["parts"][-1]["badge"] is None  # "Other" carries no number
    assert ring["parts"][0]["clip"].startswith("polygon(") and ring["parts"][0]["box"]["width"] > 0
    assert chart["left_out"] == {"free": ["Free Game"], "no_price": ["Unsold Game", "Unpriced Game"],
                                 "not_released": ["Future Game"], "no_estimate": ["Unknown Game"],
                                 "not_asked": ["Unasked Game"]}
    assert chart["collected"] == ("2026-11-09T12:30:00+00:00",) * 2 and chart["stale"] is False
    old = dashboard.sales_chart(games[:1], {1: estimate(10, at="2026-10-20T12:00:00+00:00")}, prices,
                                datetime(2026, 11, 9, 13, tzinfo=UTC))
    assert old["stale"] is True  # more than 14 days old
    empty = dashboard.sales_chart([], {}, {}, datetime(2026, 11, 9, tzinfo=UTC))
    assert empty["ranked"] == [] and empty["ring"] is None and empty["collected"] is None and empty["stale"] is False
    lone = dashboard.sales_ring([{"appid": 1, "copies": 5}])  # one game: one slice that is the whole ring
    assert [(part["start"], part["end"], part["share_tenths"]) for part in lone["parts"]] == [(0.0, 360.0, 1_000)]
    assert dashboard.shares_in_tenths([1, 1, 1]) == [334, 333, 333]


# ---------------------------------------------------------------------- the dashboard


def board(settings, client=LOCAL, headers=None) -> str:
    app = create_app(configured(settings), today=lambda: TODAY, now=lambda: NOW, twitch_client=NoTwitch())
    return TestClient(app, client=client).get("/dashboard", headers=headers or {}).text


def sales_section(html: str) -> str:
    """The Estimated Steam copies sold chart of Trending games → Sales & reception."""
    return re.search(r'<figure class="chart-card sales-chart" id="sales".*?</figure>', html, re.S).group(0)


def sources(html: str) -> str:
    """The page's collapsed "Sources and methodology", where the notes about the estimates are."""
    return text_of(re.search(r'<details class="method-details" id="sources">.*?</details>', html, re.S).group(0))


def trending_with_estimates(settings):
    """Games 14, 13, 12 and 11 are Popular on Steam right now; Game 13 is free; Game 10 is not on the Trending page."""
    save_players(settings, {114: 4_000, 113: 1_500, 112: 900, 111: 300}, moment=NOW - timedelta(minutes=50))

    def action(conn):
        store.save_prices(conn, {113: PriceFacts(status=FREE)}, country="US", currency=None, now=iso(NOW))
        store.save_sales_estimates(conn, {114: 2_500_000, 113: 90_000_000, 112: 40_000, 111: None, 110: 5_000_000},
                                   source="Gamalytic", collected_at="2026-11-09T12:30:00+00:00",
                                   source_cached_at="2026-11-09T06:00:00+00:00")
    with_conn(settings, action)


def test_the_chart_compares_trending_games_on_this_computer_only(games, settings):
    trending_with_estimates(settings)
    html = board(settings)
    section = sales_section(html)
    canvas = html[html.index('<div class="report-canvas"'):]
    assert canvas.index('id="sales"') < canvas.index('id="reception"')  # the first chart of Sales & reception
    assert re.findall(r'data-details="(\d+)"', section) == ["114", "112", "114", "112"]  # the slices, then the list
    assert re.findall(r'<p class="ring-figure"[^>]*>(≈ [^<]+)<', section) == ["≈ 2.5M", "≈ 40K"]
    assert "≈ 2.5M estimated copies 2 games" in text_of(section)  # the middle: the cohort's total and size
    table = text_of(re.search(r"<table.*?</table>", section, re.S).group(0))
    assert "1 Game 14 2,500,000 98.4%" in table and "2 Game 12 40,000 1.6%" in table
    assert "Total (2 games, the middle of the doughnut) 2,540,000 100.0%" in table  # the slices reconcile
    words = text_of(section)
    assert "Estimated Steam copies sold Steam only Local evaluation only Steam · cumulative estimates" in words
    assert "not sales reported by publishers" in words and "Gamalytic’s terms do not allow sharing them" in words
    assert ("Gamalytic API (free), copiesSold · collected 9 Nov 2026, 12:30 UTC; Gamalytic’s data dated "
            "9 Nov 2026, 06:00 UTC. Left out: 1 free to play, 1 without a Gamalytic estimate.") in words
    assert "Game 10" not in words  # an estimate alone does not put a game in the chart: the Trending page does
    assert "Popular on Steam right now" in words  # the Trending list each game comes from, in its tooltip and table
    assert "2,500,000" in section  # the table and the tooltip give Gamalytic's figure
    assert 'steam/apps/114/library_600x900.jpg?t=1" alt=""' in section  # the portrait cover beside the name
    # The notes on the estimates fold under the page's one "Sources and methodology".
    notes = sources(html)
    assert "Left out now: Game 13 (free to play); Game 11 (without a Gamalytic estimate)." in notes
    assert "the 5 largest estimates each have a slice" in notes and "the slices add up to it" in notes
    assert "This is not a list of Steam’s best sellers." in notes and "No change is shown" in notes
    assert "copies times today’s price would be wrong" in notes  # no revenue is calculated
    assert "Do not publish it or screenshots of it." in notes


@pytest.mark.parametrize("client, headers", [
    (("testclient", 50000), None),  # the test client's default: not an address
    (("192.168.1.20", 50000), None),  # another computer on the network
    (("127.0.0.1", 50000), {"X-Forwarded-For": "203.0.113.9"}),  # a proxy on this computer passing a visitor on
    (("127.0.0.1", 50000), {"Forwarded": "for=203.0.113.9"}),
])
def test_no_other_browser_gets_the_estimates(games, settings, client, headers):
    trending_with_estimates(settings)
    html = board(settings, client, headers)
    assert 'id="sales"' not in html and "#sales" not in html
    assert "data-sales-ring" not in html and "ring-slice" not in html and "estimated copies" not in text_of(html)  # no payload
    assert "Gamalytic" not in html and "2,500,000" not in html and "2.5M" not in html
    assert "No sales figures: Valve publishes no unit sales or revenue per game, so this page compares Steam " \
           "reception." in text_of(html)  # the reception charts lead instead


@pytest.mark.parametrize("address", ["::1", "::ffff:127.0.0.1", "127.0.0.2"])
def test_every_loopback_address_is_this_computer(games, settings, address):
    trending_with_estimates(settings)
    assert 'id="sales"' in board(settings, (address, 50000))


def test_the_chart_explains_an_empty_an_old_or_a_failed_collection(games, settings):
    save_players(settings, {114: 4_000}, moment=NOW - timedelta(minutes=50))
    words = text_of(sales_section(board(settings)))
    assert ("No estimates collected yet. Run python -m game_librarian sales-estimates: Gamalytic’s free API, no key, "
            "about 1 request for the 14 catalog games.") in words
    assert "Left out" not in words and "not collected yet." in words

    def old_collection_then_a_failure(conn):
        store.save_sales_estimates(conn, {114: 2_500_000}, source="Gamalytic",
                                   collected_at="2026-10-20T12:00:00+00:00", source_cached_at=None)
        run = store.start_source_run(conn, gamalytic.SOURCE, now="2026-11-09T12:40:00+00:00")
        store.finish_source_run(conn, run, status="failed", now="2026-11-09T12:41:00+00:00", summary={
            "counts": {"requests": 3}, "stopped_early": "A Gamalytic request failed, so the run stopped.",
            "errors": ["Gamalytic could not be reached after 3 attempts (ConnectError)."]})
    with_conn(settings, old_collection_then_a_failure)
    words = text_of(sales_section(board(settings)))
    assert ("The last collection (9 Nov 2026, 12:40 UTC) failed: A Gamalytic request failed, so the run stopped. "
            "The estimates collected before are shown.") in words
    assert "Some estimates below were collected more than 14 days ago." in words
    assert "≈ 2.5M" in words and "collected 20 Oct 2026, 12:00 UTC." in words  # no source time: none is invented

    def no_trending_estimate(conn):
        store.save_sales_estimates(conn, {114: None}, source="Gamalytic", collected_at="2026-11-09T12:45:00+00:00",
                                   source_cached_at=None)
    with_conn(settings, no_trending_estimate)
    html = board(settings)
    words = text_of(sales_section(html))
    assert ("None of the 1 catalog games on the Trending page is a released paid game with an estimate in its "
            "newest collection.") in words  # the older estimate is not used once a newer collection has none
    assert "Left out: 1 without a Gamalytic estimate." in words
    assert "Left out now: Game 14 (without a Gamalytic estimate)." in sources(html)


# ---------------------------------------------------------------------- the commands


def test_the_command_and_sync_ask_only_when_due_and_say_where_the_estimates_show(games, settings, fake_steam,
                                                                                 fake_gamalytic, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    fake_gamalytic.copies = {appid: appid * 1_000 for appid in range(101, 114)}  # Game 14 has no estimate
    assert cli.main(["sales-estimates"]) == 0
    out = capsys.readouterr().out
    assert "asking about 14 of 14 catalog games, 100 per request (free API, no key)" in out
    assert '"estimates": 13' in out and '"no_estimate": 1' in out and '"requests": 1' in out
    assert "Shown only to a browser on this computer" in out and "Gamalytic's terms do not allow sharing" in out
    assert cli.main(["sales-estimates"]) == 0
    assert "nothing is due; each game is asked again after 7 days (--force asks now)" in capsys.readouterr().out
    assert len(fake_gamalytic.requests) == 1  # the second run asked nothing
    assert cli.main(["sync", "--skip-upcoming", "--skip-critics"]) == 0
    assert "Sales estimates (Gamalytic): nothing is due" in capsys.readouterr().out
    assert cli.main(["sync", "--skip-sales"]) == 0
    assert "Sales estimates (Gamalytic): skipped (--skip-sales)." in capsys.readouterr().out
    assert cli.main(["status"]) == 0
    assert ("Sales estimates (Gamalytic, shown on this computer only): 13 games with an estimate; 1 collection(s)"
            in capsys.readouterr().out)

    fake_gamalytic.responses = [429]
    assert cli.main(["sales-estimates", "--force"]) == 1  # nothing was asked: the run failed, and says why
    out = capsys.readouterr().out
    assert "Sales estimates (Gamalytic): failed" in out and "Gamalytic answered 429" in out
    assert len(fake_gamalytic.requests) == 2
    conn = store.connect(settings.db_path)
    try:
        assert store.latest_sales_estimates(conn, [101])[101]["copies_sold"] == 101_000  # kept after the 429
    finally:
        conn.close()
