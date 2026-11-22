"""Shared test helpers: an in-memory fake Steam, Gamalytic and TwitchTracker, synthetic payloads and a controllable
clock.

All names, texts and numbers here are made up for tests. They are never shown by the app.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from game_librarian.config import Settings
from game_librarian.gamalytic import GamalyticClient
from game_librarian.steam import SteamClient
from game_librarian.twitchtracker import TwitchTrackerClient

TAG_NAMES = {19: "Action", 122: "RPG", 492: "Indie", 1695: "Open World", 3871: "2D", 1667: "Horror",
             1664: "Puzzle", 1625: "Platformer", 4085: "Anime", 21: "Adventure", 3859: "Multiplayer", 1742: "Story Rich"}


def details_payload(appid: int, name: str, **overrides) -> dict:
    """A store/api/appdetails `data` object shaped like Steam's (fields observed live)."""
    data = {
        "type": "game",
        "name": name,
        "steam_appid": appid,
        "short_description": f"{name} is a test game about testing &amp; more.",
        "about_the_game": (
            '<h2 class="bb_tag">About the Game</h2>You explore a test world.<br><br>'
            '<ul class="bb_ul"><li>Feature one</li><li>Feature two</li></ul>'
            '<img src="https://example.invalid/banner.png">'
        ),
        "header_image": f"https://cdn.example.invalid/{appid}/header.jpg",
        "developers": ["Test Studio "],
        "publishers": ["Test Publisher"],
        "genres": [{"id": "3", "description": "RPG"}],
        "categories": [
            {"id": 2, "description": "Single-player"},
            {"id": 22, "description": "Steam Achievements"},
            {"id": 38, "description": "Online Co-op"},
        ],
        "platforms": {"windows": True, "mac": False, "linux": True},
        "release_date": {"coming_soon": False, "date": "1 Jan, 2024"},
    }
    data.update(overrides)
    return data


def trailer_item(appid: int, position: int, name: str, all_ages: bool) -> dict:
    """One IStoreBrowseService trailer entry, shaped like Steam's (fields observed live)."""
    folder = f"{appid}/{position}/abc{position}/1"
    return {
        "trailer_name": name,
        "trailer_url_format": "steam/apps/${FILENAME}?t=1",
        "all_ages": all_ages,
        "microtrailer": [
            {"filename": f"{folder}/microtrailer.webm", "type": "video/webm"},
            {"filename": f"{folder}/microtrailer.mp4", "type": "video/mp4"},
        ],
        "screenshot_full": f"{appid}{position}/movie_full.jpg",
    }


def store_summary(name: str, *, app_type: int = 0, reviews: int = 1_000, percent: int = 90,
                  released: str | None = "2024-01-01", free: bool = False, purchasable: bool = True,
                  visible: bool = True, coming_soon: bool = False, descriptors: tuple[int, ...] = ()) -> dict:
    """An IStoreBrowseService/GetItems item as candidate screening requests it (fields observed live)."""
    item = {"name": name, "type": app_type, "visible": visible, "reviews": {"summary_filtered": {
        "review_count": reviews, "percent_positive": percent, "review_score": 8, "review_score_label": "Very Positive"}}}
    if released:
        item["release"] = {"steam_release_date": int(datetime.fromisoformat(released).replace(tzinfo=timezone.utc).timestamp())}
    if coming_soon:
        item["release"] = {**item.get("release", {}), "is_coming_soon": True}
    if free:
        item["is_free"] = True
    elif purchasable:
        item["best_purchase_option"] = {"packageid": 1, "final_price_in_cents": "1999"}
    if descriptors:
        item["content_descriptorids"] = list(descriptors)
    return item


def purchase_item(name: str, cents: int = 1999, *, discount: int = 0, ends: int | None = None,
                  hidden: bool = False) -> dict:
    """The price part of an IStoreBrowseService/GetItems item, shaped like Steam's (fields observed live on
    2026-11-19: prices in cents as strings, the discount's end as a Unix time)."""
    final = cents * (100 - discount) // 100
    option = {"packageid": 1, "purchase_option_name": name, "final_price_in_cents": str(final),
              "formatted_final_price": f"${final / 100:.2f}", "hide_discount_pct_for_compliance": hidden,
              "price_cannot_be_displayed_as_discount": False}
    if discount:
        option.update(original_price_in_cents=str(cents), formatted_original_price=f"${cents / 100:.2f}",
                      discount_pct=discount,
                      active_discounts=[{"discount_amount": str(cents - final), "discount_end_date": ends}] if ends else [])
    return {"best_purchase_option": option}


def review(recommendation_id: int, text: str, *, voted_up: bool, votes: int = 10, playtime: int = 600) -> dict:
    """One GetAppReviews review entry."""
    return {
        "recommendationid": str(recommendation_id),
        "review": text,
        "voted_up": voted_up,
        "votes_up": votes,
        "timestamp_created": 1_700_000_000,
        "language": "english",
        "author": {"steamid": "76561190000000000", "playtime_at_review": playtime},
    }


class FakeSteam:
    """A tiny in-memory Steam that a real SteamClient can talk to through httpx.MockTransport."""

    def __init__(self) -> None:
        self.details: dict[int, dict | None] = {}  # None = Steam answers success: false
        self.tags: dict[int, list[tuple[int, int]]] = {}
        self.trailers: dict[int, list[tuple[str, bool]]] = {}  # (trailer name, all ages)
        self.reviews: dict[int, dict] = {}
        self.store_summaries: dict[int, dict] = {}  # what candidate screening sees (see store_summary)
        self.prices: dict[int, dict] = {}  # GetItems price fields per app (see purchase_item); missing = no store item
        self.players: dict[int, int] = {}  # current players per app; missing = HTTP 404, as Steam answers
        self.currency = "USD"  # what appdetails' price_overview reports for every store region
        self.app_list_pages: list[dict] = []
        self.down = False  # every request fails with a connection error
        self.failing_paths: set[str] = set()  # path fragments that answer HTTP 503
        self.failing_appids: set[int] = set()  # appdetails for these app ids answers HTTP 503
        self.requests: list[tuple[str, dict]] = []

    def add_game(self, appid: int, name: str, *, tags=((122, 900), (19, 800)), positive: int = 3,
                 negative: int = 3, totals=(900, 100, "Very Positive"), trailers=(("Launch Trailer", True),),
                 price: dict | None = None, players: int | None = None, **detail_overrides) -> None:
        """A game on the fake Steam: $19.99 without a discount unless `price` says otherwise, and no player
        count (HTTP 404) unless `players` is given."""
        self.details[appid] = details_payload(appid, name, **detail_overrides)
        self.tags[appid] = list(tags)
        self.trailers[appid] = list(trailers)
        self.prices[appid] = price if price is not None else purchase_item(name)
        if players is not None:
            self.players[appid] = players
        self.reviews[appid] = {
            "totals": totals,
            "positive": [review(appid * 100 + i, f"Positive review number {i} for {name}.", voted_up=True)
                         for i in range(positive)],
            "negative": [review(appid * 100 + 50 + i, f"Negative review number {i} for {name}.", voted_up=False)
                         for i in range(negative)],
        }

    def count(self, fragment: str) -> int:
        """How many requests went to a path containing `fragment`."""
        return sum(1 for path, _ in self.requests if fragment in path)

    def client(self, api_key: str | None = None) -> SteamClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return SteamClient(api_key=api_key, http=http, sleep=lambda seconds: None)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, dict(request.url.params)
        self.requests.append((path, params))
        if self.down:
            raise httpx.ConnectError("Steam is unreachable (test)", request=request)
        if any(fragment in path for fragment in self.failing_paths):
            return httpx.Response(503)
        payload = json.loads(params["input_json"]) if "input_json" in params else {}

        if path == "/api/appdetails" and params.get("filters") == "price_overview":
            appid = int(params["appids"])  # the store currency is read from one priced app
            overview = {"currency": self.currency, "initial": 1999, "final": 1999, "discount_percent": 0}
            return httpx.Response(200, json={str(appid): {"success": True, "data": {"price_overview": overview}}})
        if path == "/api/appdetails":
            appid = int(params["appids"])
            if appid in self.failing_appids:
                return httpx.Response(503)
            data = self.details.get(appid)
            entry = {"success": True, "data": data} if data else {"success": False}
            return httpx.Response(200, json={str(appid): entry})
        if path.endswith("/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"):
            appid = int(params["appid"])
            if appid not in self.players:
                return httpx.Response(404)  # what Steam answers for an app it has no count for
            return httpx.Response(200, json={"response": {"player_count": self.players[appid], "result": 1}})
        if path.endswith("/IStoreBrowseService/GetItems/v1/") and not payload["data_request"]:
            items = []  # a price request: the free flag and the best purchase option
            for ident in payload["ids"]:
                price = self.prices.get(ident["appid"])
                items.append({"appid": ident["appid"], "success": 1, "visible": True, **price} if price is not None
                             else {"appid": 0, "success": 15, "visible": False})
            return httpx.Response(200, json={"response": {"store_items": items}})
        if path.endswith("/IStoreService/GetTagList/v1/"):
            tags = [{"tagid": tagid, "name": name} for tagid, name in TAG_NAMES.items()]
            return httpx.Response(200, json={"response": {"tags": tags}})
        if path.endswith("/IStoreBrowseService/GetItems/v1/") and payload["data_request"].get("include_reviews"):
            items = []  # a screening request: review summary, release, availability
            for ident in payload["ids"]:
                summary = self.store_summaries.get(ident["appid"])
                items.append({"appid": ident["appid"], "success": 1, **summary} if summary
                             else {"appid": 0, "success": 15, "visible": False})
            return httpx.Response(200, json={"response": {"store_items": items}})
        if path.endswith("/IStoreBrowseService/GetItems/v1/"):
            items = []
            for ident in payload["ids"]:
                appid = ident["appid"]
                if appid in self.tags:
                    trailers = [trailer_item(appid, position, name, all_ages)
                                for position, (name, all_ages) in enumerate(self.trailers.get(appid, []))]
                    items.append({
                        "appid": appid, "success": 1, "visible": True,
                        "tags": [{"tagid": tagid, "weight": weight} for tagid, weight in self.tags[appid]],
                        "assets": {"asset_url_format": f"steam/apps/{appid}/${{FILENAME}}?t=1",
                                   "library_capsule": "library_600x900.jpg"},
                        "trailers": {"highlights": trailers},
                        "screenshots": {"all_ages_screenshots": [
                            {"filename": f"steam/apps/{appid}/ss_second.jpg?t=1", "ordinal": 2},
                            {"filename": f"steam/apps/{appid}/ss_first.jpg?t=1", "ordinal": 1},
                        ]},
                    })
                else:
                    items.append({"appid": 0, "success": 15, "visible": False})
            return httpx.Response(200, json={"response": {"store_items": items}})
        if path.endswith("/IUserReviewsService/GetAppReviews/v1/"):
            data = self.reviews.get(payload["appid"])
            if data is None:
                return httpx.Response(200, json={"response": {}})
            if payload["review_type"] == 0:
                positive, negative, desc = data["totals"]
                summary = {"num_reviews": 1, "review_score_desc": desc, "total_positive": positive,
                           "total_negative": negative, "total_reviews": positive + negative}
                return httpx.Response(200, json={"response": {"query_summary": summary, "reviews": []}})
            reviews = data["positive"] if payload["review_type"] == 1 else data["negative"]
            body = {"query_summary": {"num_reviews": len(reviews)}, "reviews": reviews[: payload["num_per_page"]],
                    "day_range_used": payload.get("day_range", 30), "total_matching": len(reviews)}
            return httpx.Response(200, json={"response": body})
        if path.endswith("/IStoreService/GetAppList/v1/"):
            if "key" not in params:
                return httpx.Response(403)
            after = payload.get("last_appid", 0)
            for page in self.app_list_pages:
                if page["after"] == after:
                    apps = page["apps"]
                    return httpx.Response(200, json={"response": {
                        "apps": apps, "have_more_results": page["more"],
                        "last_appid": apps[-1]["appid"] if apps else 0}})
            return httpx.Response(200, json={"response": {"apps": []}})
        return httpx.Response(404)


class FakeGamalytic:
    """Gamalytic's free list endpoint in memory, answering like the live one did on 2026-11-20 (gamalytic.py).

    `copies` holds copiesSold per Steam app id; an app id without one is left out of the answer, as Gamalytic leaves
    out games it has no data for. `responses` are consumed one per request before normal answers: an HTTP status to
    answer with, or "timeout". Every request is kept in `requests`.
    """

    CACHE_TIMESTAMP = 1_795_183_238_291  # Unix milliseconds: 20 Nov 2026, 14:00:38 UTC, as on the live answers

    def __init__(self) -> None:
        self.copies: dict[int, int] = {}
        self.responses: list[int | str] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            planned = self.responses.pop(0)
            if planned == "timeout":
                raise httpx.ReadTimeout("Gamalytic did not answer (test)", request=request)
            if planned != 200:
                return httpx.Response(planned, json={"message": f"planned answer {planned} (test)"})
        if request.url.path != "/steam-games/list":
            return httpx.Response(404, json={"message": "not found"})
        appids = [int(value) for value in request.url.params["appids"].split(",")]
        result = [{"steamId": appid, "copiesSold": self.copies[appid]} for appid in appids if appid in self.copies]
        return httpx.Response(200, json={"pages": 1, "total": len(result), "result": result,
                                         "cacheTimestamp": self.CACHE_TIMESTAMP})

    def client(self) -> GamalyticClient:
        return GamalyticClient(http=httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda seconds: None)


@pytest.fixture(autouse=True)
def fake_gamalytic(monkeypatch) -> FakeGamalytic:
    """Every test's Gamalytic: `sync` asks for sales estimates by default (no setting needed), so without this a
    command-line test would reach the real API. The test suite never does."""
    fake = FakeGamalytic()
    monkeypatch.setattr("game_librarian.__main__.GamalyticClient", fake.client)
    return fake


class FakeTwitchTracker:
    """TwitchTracker's category summary endpoint in memory, answering like the live one did on 2026-11-21
    (twitchtracker.py): a JSON object of four whole numbers, or {} for a category it has no summary of.

    `summaries` maps a Twitch category id to its answer; an id without one gets {}. `responses` are consumed one per
    request before normal answers: an HTTP status to answer with, "timeout", or "html" (a challenge page that is not
    JSON, with HTTP 200). Every request is kept in `requests`.
    """

    def __init__(self) -> None:
        self.summaries: dict[str, dict] = {}
        self.responses: list[int | str] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            planned = self.responses.pop(0)
            if planned == "timeout":
                raise httpx.ReadTimeout("TwitchTracker did not answer (test)", request=request)
            if planned == "html":
                return httpx.Response(200, text="<html><title>Just a moment...</title></html>",
                                      headers={"Content-Type": "text/html"})
            if planned != 200:
                return httpx.Response(planned, text=f"planned answer {planned} (test)")
        prefix = "/api/games/summary/"
        if not request.url.path.startswith(prefix):
            return httpx.Response(404)
        return httpx.Response(200, json=self.summaries.get(request.url.path[len(prefix):], {}))

    def client(self) -> TwitchTrackerClient:
        return TwitchTrackerClient(http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                                   sleep=lambda seconds: None)


@pytest.fixture(autouse=True)
def fake_twitchtracker(monkeypatch) -> FakeTwitchTracker:
    """Every test's TwitchTracker: `sync` asks it by default when IGDB is configured, so without this a command-line
    test could reach the real API. The test suite never does."""
    fake = FakeTwitchTracker()
    monkeypatch.setattr("game_librarian.__main__.TwitchTrackerClient", fake.client)
    return fake


class Clock:
    """A controllable UTC clock for sync runs."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta) -> None:
        self.moment += timedelta(**delta)


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write dict rows to a CSV file, using the first row's keys as the header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def fake_steam() -> FakeSteam:
    return FakeSteam()


@pytest.fixture
def clock() -> Clock:
    return Clock()


# With GAME_LIBRARIAN_TEST_DATABASE_URL (a PostgreSQL database that the test role owns, such as
# "postgresql://gl_test@localhost:55432/gl_test", with PGPASSWORD set), every test that uses `settings` gets a new,
# empty PostgreSQL schema there instead of a SQLite file, so the whole suite runs on PostgreSQL. Without it, SQLite.
TEST_DATABASE_URL = os.environ.get("GAME_LIBRARIAN_TEST_DATABASE_URL")


@pytest.fixture
def postgres_url(request):
    """A connection string to a new, empty schema with the app's tables (None when the suite runs on SQLite).

    A test marked local_evaluation always gets SQLite: Gamalytic's and TwitchTracker's figures are kept only in the
    local SQLite database (Settings.local_evaluation), so their features do not exist with PostgreSQL.
    """
    if not TEST_DATABASE_URL or request.node.get_closest_marker("local_evaluation"):
        yield None
        return
    from psycopg.conninfo import make_conninfo

    from game_librarian import database, store

    schema = f"test_{uuid.uuid4().hex[:16]}"
    admin = database.connect(database.PostgresDatabase(TEST_DATABASE_URL))
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
    finally:
        admin.close()
    url = make_conninfo(TEST_DATABASE_URL, options=f"-c search_path={schema}")
    conn = database.connect(database.PostgresDatabase(url))
    try:
        store.create_postgres_schema(conn)
    finally:
        conn.close()
    try:
        yield url
    finally:
        database.close_pools()  # the website's pooled connections of this test
        admin = database.connect(database.PostgresDatabase(TEST_DATABASE_URL))
        try:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")
        finally:
            admin.close()


@pytest.fixture
def settings(tmp_path: Path, postgres_url) -> Settings:
    (tmp_path / "curated").mkdir()
    return Settings(
        db_path=tmp_path / "data" / "catalog.sqlite3",
        curated_dir=tmp_path / "curated",
        steam_api_key=None,
        review_language="english",
        database_url=postgres_url,
    )
