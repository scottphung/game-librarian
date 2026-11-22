"""Upcoming & Anticipated (upcoming.py): release-date precision, the upcoming rule, PopScore parsing,
Steam matching, date changes, position changes, and a full refresh against fake IGDB and Steam services.

All names, ids and values are made up. The shapes follow the IGDB answers checked live on 2026-11-19.
"""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from game_librarian import store, upcoming
from game_librarian.curated import CuratedData, SteamMatchDecision
from game_librarian.igdb import API_BASE, TOKEN_URL, IgdbClient
from game_librarian.upcoming import (FULL, ReleaseDate, compare_release_summaries, judge_steam_match,
                                     parse_popularity, parse_release_date, platform_releases, position_changes,
                                     record_from_igdb, release_dates, release_state)
from tests.conftest import store_summary

TODAY = date(2026, 11, 19)
CLIENT_ID, CLIENT_SECRET = "upcoming-test-client-id", "upcoming-test-client-secret"


def stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def entry(platform="PC", *, fmt=0, day=None, y=None, m=None, status=6, region=8):
    """One IGDB release_dates entry. IGDB leaves `d` empty and stores a placeholder timestamp for coarse dates."""
    value = {"date_format": fmt, "platform": {"id": 6, "name": platform}, "release_region": region, "status": status,
             "y": y, "m": m, "d": None}
    if day is not None:
        value.update(date=stamp(day), y=day.year, m=day.month)
    elif y is not None:
        value["date"] = stamp(date(y, 12, 31))  # the placeholder: never shown as a day
    return value


# ---------------------------------------------------------------------- dates


def test_dates_keep_igdb_precision_and_never_invent_a_day():
    assert parse_release_date(entry(day=date(2027, 2, 23)), "PC").label == "23 Feb 2027"
    assert parse_release_date(entry(fmt=1, y=2027, m=3)).label == "Mar 2027"
    assert parse_release_date(entry(fmt=5, y=2027)).label == "Q3 2027"
    year = parse_release_date(entry(fmt=2, y=2028, m=12))  # IGDB fills m=12 and Dec 31 for a bare year
    assert (year.label, year.precision, year.period()) == ("2028", "year", (date(2028, 1, 1), date(2028, 12, 31)))
    assert parse_release_date(entry(fmt=7)).label == "TBA"
    assert parse_release_date({"date_format": 99, "y": 2027}) is None  # an unknown format is not guessed
    assert parse_release_date(entry(fmt=1, y=2027, m=13)) is None
    assert parse_release_date(entry(fmt=0)) is None  # an exact date without its timestamp
    assert parse_release_date({"category": 3, "y": 2027, "status": None}).label == "Q1 2027"  # the old field


def test_release_statuses_cancelled_are_ignored_and_pre_and_later_releases_are_kept_apart():
    assert parse_release_date(entry(day=date(2027, 1, 1), status=5)) is None  # cancelled
    assert parse_release_date(entry(day=date(2027, 1, 1), status=4)) is None  # offline
    # Kept for "Available on", never counted as a first release (test_trending_cross_platform.py).
    assert parse_release_date(entry(day=date(2027, 1, 1), status=35)).status == "digital compatibility release"
    assert parse_release_date(entry(day=date(2025, 3, 15), status=36)).status == "next-gen patch"
    assert parse_release_date(entry(day=date(2024, 8, 21), status=1)).status == "alpha"
    assert parse_release_date(entry(day=date(2027, 2, 18), status=34)).status == "advanced access"
    assert parse_release_date(entry(day=date(2027, 2, 23), status=None)).status == FULL  # empty status: a release


def test_a_period_counts_as_released_only_when_it_is_over():
    exact = ReleaseDate("PC", "day", FULL, 2026, 11, 19)
    month = ReleaseDate("PC", "month", FULL, 2026, 11)
    last_year = ReleaseDate("PC", "year", FULL, 2025)
    quarter = ReleaseDate("PC", "quarter", FULL, 2026, quarter=4)
    assert exact.has_happened(TODAY)  # the exact day is today
    assert not month.has_happened(TODAY)  # November is not over: it may still come later this month
    assert last_year.has_happened(TODAY) and not quarter.has_happened(TODAY)
    assert not ReleaseDate("PC", "tbd", FULL).has_happened(date(2099, 1, 1))


def game_json(**overrides):
    value = {"id": 7, "name": "Moon Garden", "game_type": 0, "game_status": None, "first_release_date": None,
             "release_dates": [entry("PC", fmt=7)]}
    value.update(overrides)
    return value


def state_of(game, today=TODAY):
    return release_state(game.get("game_type"), game.get("game_status"), release_dates(game),
                         game.get("first_release_date"), today)


def test_the_upcoming_rule():
    assert state_of(game_json()).upcoming  # to be announced
    released = game_json(release_dates=[entry(day=date(2026, 11, 18))])
    assert not state_of(released).upcoming and "released 18 Nov 2026" in state_of(released).reason
    assert state_of(game_json(release_dates=[entry(day=date(2026, 11, 20))])).upcoming
    assert state_of(game_json(release_dates=[entry(fmt=1, y=2026, m=11)])).upcoming  # this month: not over yet
    assert not state_of(game_json(game_status=6)).upcoming  # cancelled
    assert not state_of(game_json(game_status=7)).upcoming  # rumored, not announced
    assert not state_of(game_json(game_type=1)).upcoming  # DLC
    assert state_of(game_json(game_type=8)).upcoming  # a remake is a new game to follow


def test_early_access_does_not_end_upcoming_but_is_reported():
    game = game_json(game_status=4, release_dates=[entry(day=date(2024, 12, 6), status=3),
                                                     entry(day=date(2026, 12, 11))])
    state = state_of(game)
    assert state.upcoming and state.early.status == "early access" and state.early.label == "6 Dec 2024"
    assert state.first.label == "11 Dec 2026"


def test_without_readable_dates_igdbs_first_release_date_only_ends_upcoming_when_past():
    past = game_json(release_dates=[], first_release_date=stamp(date(2020, 5, 1)))
    future = game_json(release_dates=[], first_release_date=stamp(date(2027, 5, 1)))
    assert not state_of(past).upcoming and state_of(future).upcoming


def test_platform_specific_dates_are_grouped_by_date_and_regional_differences_flagged():
    dates = release_dates(game_json(release_dates=[
        entry("PC", day=date(2027, 2, 18)), entry("PlayStation 5", day=date(2027, 2, 18)),
        entry("Nintendo Switch 2", day=date(2027, 5, 20)),
        entry("Xbox Series X|S", fmt=6, y=2027),
        entry("PC", day=date(2027, 3, 1), region=1),  # Europe later than worldwide
        entry("PlayStation 5", day=date(2027, 2, 10), status=34),  # advanced access is not a release
    ]))
    groups = platform_releases(dates)
    assert [(group.label, group.platforms) for group in groups] == [
        ("18 Feb 2027", ("PC", "PlayStation 5")), ("20 May 2027", ("Nintendo Switch 2",)),
        ("Q4 2027", ("Xbox Series X|S",))]
    assert groups[0].regional and not groups[1].regional
    assert upcoming.pre_release_groups(dates) == [{"status": "advanced access", "label": "10 Feb 2027",
                                                   "platforms": ["PlayStation 5"]}]


def test_date_changes_say_how_the_date_moved():
    before = {"PC": {"label": "Q4 2026"}, "PS5": {"label": "TBA"}, "Xbox": {"label": "2027"},
              "Switch 2": {"label": "Feb 2027"}, "Mac": {"label": "2027"}}
    after = {"PC": {"label": "23 Feb 2027"}, "PS5": {"label": "Mar 2027"}, "Xbox": {"label": "Jun 2027"},
             "Switch 2": {"label": "TBA"}, "Linux": {"label": "2027"}}
    changes = {change.platform: (change.kind, change.before, change.after)
               for change in compare_release_summaries(before, after)}
    assert changes == {
        "PC": ("later", "Q4 2026", "23 Feb 2027"),  # a delay: the new date is after the old period
        "PS5": ("announced", "TBA", "Mar 2027"),
        "Xbox": ("narrowed", "2027", "Jun 2027"),  # made more precise, not moved
        "Switch 2": ("withdrawn", "Feb 2027", "TBA"),
        "Mac": ("removed", "2027", None),
        "Linux": ("added", None, "2027"),
    }
    assert compare_release_summaries({"PC": {"label": "2027"}}, {"PC": {"label": "Q1 2026"}})[0].kind == "earlier"
    assert compare_release_summaries(after, after) == []


# ---------------------------------------------------------------------- PopScore values


def test_popularity_rows_are_validated_and_ordered_without_guessing():
    rows = [
        {"game_id": 3, "value": 0.002, "calculated_at": 1795050874},
        {"game_id": 1, "value": 0.003},
        {"game_id": 2, "value": 0.002},  # a tie: ordered by IGDB id
        {"game_id": 4, "value": "0.009"},  # not a number
        {"game_id": 5, "value": float("nan")},
        {"game_id": 6, "value": -1},
        {"game_id": 7},  # no value
        {"game_id": True, "value": 0.5},  # not an id
        {"value": 0.5},
        {"game_id": 1, "value": 0.001},  # the same game twice: the first row counts
    ]
    entries, skipped = parse_popularity(rows)
    assert [(entry.igdb_id, entry.position) for entry in entries] == [(1, 1), (2, 2), (3, 3)]
    assert skipped == 7
    assert entries[2].calculated_at == datetime.fromtimestamp(1795050874, tz=timezone.utc)
    assert entries[0].calculated_at is None
    assert parse_popularity([]) == ([], 0)


def test_position_changes_need_a_snapshot_7_to_14_days_earlier():
    def rows(*pairs):
        return [{"captured_at": moment.isoformat(), "position": position} for moment, position in pairs]
    now = datetime(2026, 11, 26, 3, tzinfo=timezone.utc)
    history = {
        1: rows((now - timedelta(days=7), 9), (now, 4)),  # up 5 places
        2: rows((now - timedelta(days=3), 2), (now, 3)),  # only 3 days apart: no change yet
        3: rows((now - timedelta(days=20), 1), (now, 8)),  # too old a baseline
    }
    changes = position_changes(history)
    assert set(changes) == {1} and changes[1].places == 5 and (changes[1].before, changes[1].after) == (9, 4)


# ---------------------------------------------------------------------- Steam matching and records


def steam_item(name, app_type=0):
    return {"name": name, "type": app_type, "visible": True}


def test_a_steam_app_is_attached_only_when_the_match_is_verified():
    assert judge_steam_match("Moon Garden", [500], {500: steam_item("MOON GARDEN™")}, None).status == "verified"
    renamed = judge_steam_match("Moon Garden", [500], {500: steam_item("Moon Garden: Deluxe Edition")}, None)
    assert (renamed.status, renamed.appid) == ("needs_review", None) and "Deluxe" in renamed.reason
    assert judge_steam_match("Moon Garden", [500, 501], {}, None).status == "needs_review"
    assert judge_steam_match("Moon Garden", [500], {}, None).status == "needs_review"  # Steam had no data
    assert judge_steam_match("Moon Garden", [500], {500: steam_item("Moon Garden", app_type=4)}, None).status == "needs_review"
    assert judge_steam_match("Moon Garden", [], {}, None).status == "none"
    confirmed = SteamMatchDecision(7, 501, "match", "2026-11-19", "a reviewer")
    match = judge_steam_match("Moon Garden", [500, 501], {}, confirmed)
    assert (match.status, match.appid) == ("verified", 501) and "a reviewer" in match.reason
    rejected = SteamMatchDecision(7, None, "no_match", "2026-11-19", "a reviewer")
    assert judge_steam_match("Moon Garden", [500], {500: steam_item("Moon Garden")}, rejected).status == "rejected"


def test_records_keep_only_safe_links_and_named_roles():
    record = record_from_igdb(game_json(
        slug="moon-garden", url="https://www.igdb.com/games/moon-garden", summary="  A  quiet   game. ",
        cover={"image_id": "co12ab"}, platforms=[{"name": "PC (Microsoft Windows)"}, {"name": "PlayStation 5"}],
        involved_companies=[{"company": {"name": "Moon Studio"}, "developer": True, "publisher": False},
                            {"company": {"name": "Big Publisher"}, "developer": False, "publisher": True}],
        websites=[{"type": 1, "url": "https://moongarden.example/"}, {"type": 13, "url": "https://store.steampowered.com/app/500"},
                  {"type": 23, "url": "javascript:alert(1)"}, {"type": 22, "url": "https://xbox.example/moon"},
                  {"type": 5, "url": "https://social.example/moon"}],
        external_games=[{"external_game_source": 1, "uid": "500"}, {"external_game_source": 5, "uid": "x"}],
    ))
    assert record["platforms"] == ["PC", "PlayStation 5"] and record["summary"] == "A quiet game."
    assert record["publishers"] == ["Big Publisher"] and record["developers"] == ["Moon Studio"]
    # No Steam, no script URL. Neither link is verified: no company website to compare with, not Xbox's domain.
    assert record["websites"] == [{"kind": "Official website", "url": "https://moongarden.example/", "verified": False},
                                  {"kind": "Xbox store", "url": "https://xbox.example/moon", "verified": False}]
    assert record["steam_appids"] == [500] and record["cover_image_id"] == "co12ab"
    assert record_from_igdb({"id": 8, "name": "  "}) is None and record_from_igdb({"name": "No id"}) is None
    assert record_from_igdb(game_json(cover={"image_id": "../../x"}))["cover_image_id"] is None


# ---------------------------------------------------------------------- refresh against fake services


class FakeIgdbApi:
    """Twitch's token endpoint and the IGDB endpoints the refresh uses, in memory."""

    def __init__(self):
        self.games: dict[int, dict] = {}
        self.popularity: dict[int, list[dict]] = {2: [], 10: []}
        self.externals: list[dict] = []
        self.down = False
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "fake-token-123", "expires_in": 5_000_000})
        if self.down:
            return httpx.Response(503)
        body = request.content.decode()
        if url == f"{API_BASE}/popularity_primitives":
            kind = int(body.split("popularity_type = ")[1].split(";")[0])
            return httpx.Response(200, json=self.popularity[kind])
        if url == f"{API_BASE}/games":
            ids = [int(part) for part in body.split("where id = (")[1].split(")")[0].split(",")]
            return httpx.Response(200, json=[self.games[i] for i in ids if i in self.games])
        if url == f"{API_BASE}/external_games":  # a row without external_game_source is IGDB's Steam record
            wanted = set(body.split("uid = (")[1].split(")")[0].replace('"', "").split(","))
            source = int(body.split("external_game_source = ")[1].split(" ")[0])
            return httpx.Response(200, json=[row for row in self.externals if row["uid"] in wanted
                                             and row.get("external_game_source", 1) == source])
        return httpx.Response(404)

    def client(self) -> IgdbClient:
        return IgdbClient(CLIENT_ID, CLIENT_SECRET, http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                          sleep=lambda seconds: None)


def igdb_game(igdb_id, name, *dates, steam=(), game_type=0, status=None, platforms=("PC",)):
    return {"id": igdb_id, "name": name, "game_type": game_type, "game_status": status, "slug": f"slug-{igdb_id}",
            "url": f"https://www.igdb.com/games/slug-{igdb_id}", "platforms": [{"name": p} for p in platforms],
            "release_dates": list(dates), "external_games": [{"external_game_source": 1, "uid": str(uid)} for uid in steam],
            "involved_companies": [{"company": {"name": "Publisher Co"}, "publisher": True, "developer": False}]}


@pytest.fixture
def world(fake_steam):
    api = FakeIgdbApi()
    api.games = {
        1: igdb_game(1, "Moon Garden", entry("PC", fmt=6, y=2026), steam=[500]),
        2: igdb_game(2, "Console Quest", entry("PlayStation 5", fmt=7), platforms=("PlayStation 5",)),
        3: igdb_game(3, "Old Hit", entry("PC", day=date(2015, 5, 19))),  # released: never listed
        4: igdb_game(4, "Renamed Saga", entry("PC", fmt=2, y=2027), steam=[600]),
        5: igdb_game(5, "Catalog Early Access", entry("PC", day=date(2024, 1, 1), status=3),
                     entry("PC", day=date(2027, 1, 1)), steam=[700]),
    }
    api.popularity[2] = [{"game_id": i, "value": v, "calculated_at": 1795050874}
                         for i, v in ((3, 0.009), (2, 0.004), (1, 0.003), (4, 0.002), (5, 0.001))]
    api.popularity[10] = [{"game_id": 1, "value": 0.0004}, {"game_id": 4, "value": 0.0003}]
    api.externals = [{"game": 5, "uid": "700"}, {"game": 9, "uid": "800"}, {"game": 10, "uid": "800"}]
    fake_steam.store_summaries = {500: store_summary("Moon Garden"), 600: store_summary("Renamed Saga Deluxe"),
                                  700: store_summary("Catalog Early Access")}
    return api


def refresh(conn, api, fake_steam, now, curated=None):
    with api.client() as client:
        return upcoming.refresh_upcoming(conn, client, fake_steam.client(), curated or CuratedData(),
                                         catalog_appids=[700, 800, 900], log=lambda message: None,
                                         now=lambda: now)


def test_a_refresh_stores_records_snapshots_matches_and_catalog_links(settings, world, fake_steam):
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    conn = store.initialize(settings.database)
    try:
        report = refresh(conn, world, fake_steam, now)
        assert report.status() == "success"
        rows = store.igdb_game_rows(conn)
        assert set(rows) == {1, 2, 4, 5}  # the released game is not kept
        assert (rows[1]["steam_match"], rows[1]["steam_appid"]) == ("verified", 500)
        assert (rows[4]["steam_match"], rows[4]["steam_appid"]) == ("needs_review", None)  # another name on Steam
        assert rows[2]["steam_match"] == "none" and json.loads(rows[2]["platforms"]) == ["PlayStation 5"]
        want = store.latest_popularity(conn, "want_to_play")
        assert [(row["igdb_id"], row["position"]) for row in want] == [(2, 2), (1, 3), (4, 4), (5, 5)]
        assert [row["igdb_id"] for row in store.latest_popularity(conn, "steam_wishlists")] == [1, 4]
        links = {appid: (store.steam_igdb_link(conn, appid)["status"], store.steam_igdb_link(conn, appid)["igdb_id"])
                 for appid in (700, 800, 900)}
        assert links == {700: ("linked", 5), 800: ("ambiguous", None), 900: ("none", None)}
        assert store.igdb_ids_for_appid(conn, 700) == [5]
        everything = "\n".join(str(tuple(row)) for table in ("igdb_games", "source_runs", "popularity_snapshots")
                               for row in conn.execute(f"SELECT * FROM {table}"))
        assert CLIENT_SECRET not in everything and CLIENT_ID not in everything and "fake-token-123" not in everything
    finally:
        conn.close()


def test_a_later_refresh_notices_a_delay_and_keeps_games_that_left_the_lists(settings, world, fake_steam):
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    conn = store.initialize(settings.database)
    try:
        refresh(conn, world, fake_steam, now)
        world.games[1]["release_dates"] = [entry("PC", day=date(2027, 2, 23))]  # Q4 2026 -> 23 Feb 2027
        world.games[2]["release_dates"] = [entry("PlayStation 5", day=date(2026, 11, 19))]  # released today
        world.popularity[2] = [row for row in world.popularity[2] if row["game_id"] != 2]
        refresh(conn, world, fake_steam, now + timedelta(days=1))
        changes = store.release_changes(conn, [1], "2026-11-01")
        assert [(row["platform"], row["before"], row["after"], row["kind"]) for row in changes[1]] == [
            ("PC", "Q4 2026", "23 Feb 2027", "later")]
        kept = store.igdb_game_rows(conn, [2])[2]  # off the lists, still stored: its key and wishlist entry work
        assert kept["checked_at"].startswith("2026-11-20")
        state = upcoming.release_state(kept["game_type"], kept["game_status"], upcoming.stored_dates(kept),
                                       kept["first_release_date"], date(2026, 11, 20))
        assert not state.upcoming and state.first.label == "19 Nov 2026"
    finally:
        conn.close()


def test_a_reviewer_decision_wins_and_an_igdb_outage_keeps_the_saved_lists(settings, world, fake_steam):
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    curated = CuratedData(steam_match_decisions={4: SteamMatchDecision(4, 600, "match", "2026-11-19", "a reviewer")})
    conn = store.initialize(settings.database)
    try:
        refresh(conn, world, fake_steam, now, curated)
        assert store.igdb_game_rows(conn, [4])[4]["steam_appid"] == 600
        world.down = True
        report = refresh(conn, world, fake_steam, now + timedelta(days=1), curated)
        assert report.status() == "failed" and "IGDB" in report.errors[0]
        assert len(store.latest_popularity(conn, "want_to_play")) == 4  # the previous snapshot stays
        run = store.recent_source_runs(conn, "upcoming", limit=1)[0]
        assert run["status"] == "failed" and CLIENT_SECRET not in (run["summary"] or "")
        assert upcoming.refresh_due(conn, now + timedelta(days=1))  # the last good run is a day old
        assert not upcoming.refresh_due(conn, now + timedelta(hours=2))
    finally:
        conn.close()


def test_without_steam_answers_a_stored_match_is_kept(settings, world, fake_steam):
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    conn = store.initialize(settings.database)
    try:
        refresh(conn, world, fake_steam, now)
        fake_steam.down = True
        report = refresh(conn, world, fake_steam, now + timedelta(days=1))
        assert report.status() == "partial" and "Steam check" in report.errors[0]
        assert store.igdb_game_rows(conn, [1])[1]["steam_match"] == "verified"
    finally:
        conn.close()


def test_a_game_with_a_verified_catalog_app_opens_the_catalog_game():
    row = {"steam_match": "verified", "steam_appid": 700, "igdb_id": 5}
    assert upcoming.game_key_for(row, {700}) == "700"
    assert upcoming.game_key_for(row, {800}) == "igdb-5"  # not in the catalog: the IGDB key
    assert upcoming.game_key_for({**row, "steam_match": "needs_review"}, {700}) == "igdb-5"
