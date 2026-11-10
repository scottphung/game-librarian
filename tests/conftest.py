"""Shared test helpers: an in-memory fake Steam, synthetic payloads and a controllable clock.

All names, texts and numbers here are made up for tests. They are never shown by the app.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from game_librarian.config import Settings
from game_librarian.steam import SteamClient

TAG_NAMES = {19: "Action", 122: "RPG", 492: "Indie", 1695: "Open World", 3871: "2D", 1667: "Horror"}


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
        self.app_list_pages: list[dict] = []
        self.down = False  # every request fails with a connection error
        self.failing_paths: set[str] = set()  # path fragments that answer HTTP 503
        self.failing_appids: set[int] = set()  # appdetails for these app ids answers HTTP 503
        self.requests: list[tuple[str, dict]] = []

    def add_game(self, appid: int, name: str, *, tags=((122, 900), (19, 800)), positive: int = 3,
                 negative: int = 3, totals=(900, 100, "Very Positive"), trailers=(("Launch Trailer", True),),
                 **detail_overrides) -> None:
        self.details[appid] = details_payload(appid, name, **detail_overrides)
        self.tags[appid] = list(tags)
        self.trailers[appid] = list(trailers)
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

        if path == "/api/appdetails":
            appid = int(params["appids"])
            if appid in self.failing_appids:
                return httpx.Response(503)
            data = self.details.get(appid)
            entry = {"success": True, "data": data} if data else {"success": False}
            return httpx.Response(200, json={str(appid): entry})
        if path.endswith("/IStoreService/GetTagList/v1/"):
            tags = [{"tagid": tagid, "name": name} for tagid, name in TAG_NAMES.items()]
            return httpx.Response(200, json={"response": {"tags": tags}})
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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "curated").mkdir()
    return Settings(
        db_path=tmp_path / "data" / "catalog.sqlite3",
        curated_dir=tmp_path / "curated",
        steam_api_key=None,
        review_language="english",
    )
