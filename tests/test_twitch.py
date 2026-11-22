"""Watch it live (twitch.py): category matching by IGDB id, stream parsing, the Helix client's errors and rate
limits, and the 24-hour cache. Only mocked Twitch answers are used here; the README says what was checked live.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from game_librarian import store, twitch
from game_librarian.twitch import (TOKEN_URL, TwitchClient, TwitchError, TwitchRateLimited, category_url,
                                   live_view, match_category, parse_streams, refresh_due, refresh_live)

CLIENT_ID, CLIENT_SECRET = "twitch-test-client-id", "twitch-test-client-secret"
SECRETS = (CLIENT_ID, CLIENT_SECRET, "twitch-token-1", "twitch-token-2")
NOW = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)


def stream(login, viewers, title="Playing", **extra):
    return {"user_login": login, "user_name": login.title(), "type": "live", "viewer_count": viewers,
            "title": title, "language": "en", "started_at": "2026-11-19T13:00:00Z", **extra}


class FakeHelix:
    """Twitch's token endpoint, Get Games, Get Streams and Get Top Games, in memory."""

    def __init__(self):
        self.categories = {1020: [{"id": "32982", "name": "Grand Theft Auto V", "igdb_id": "1020"}]}
        self.streams = {"32982": [stream("alpha", 900), stream("beta", 40)]}
        self.top: list[dict] = []  # Get Top Games entries, in Twitch's order
        self.statuses: list[int] = []  # answers to send before a normal one
        self.reset = "1795100000"
        self.tokens = 0
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"twitch-token-{self.tokens}", "expires_in": 5_000_000})
        if self.statuses:
            status = self.statuses.pop(0)
            headers = {"Ratelimit-Reset": self.reset} if status == 429 else {}
            return httpx.Response(status, json={"message": f"refused {CLIENT_ID}"}, headers=headers)
        params = parse_qs(request.url.query.decode())
        if request.url.path == "/helix/games":
            return httpx.Response(200, json={"data": self.categories.get(int(params["igdb_id"][0]), [])})
        if request.url.path == "/helix/streams":
            return httpx.Response(200, json={"data": self.streams.get(params["game_id"][0], [])[:int(params["first"][0])]})
        if request.url.path == "/helix/games/top":  # order only: Twitch sends no viewer numbers here
            return httpx.Response(200, json={"data": self.top[:int(params["first"][0])], "pagination": {}})
        return httpx.Response(404)

    def client(self) -> TwitchClient:
        return TwitchClient(CLIENT_ID, CLIENT_SECRET, http=httpx.Client(transport=httpx.MockTransport(self.handler)),
                            sleep=lambda seconds: None)


def test_a_category_is_matched_only_by_its_igdb_id():
    found = [{"id": "1", "name": "Hades II", "igdb_id": "228525"}]
    assert match_category(228525, found) == twitch.CategoryMatch("matched", "1", "Hades II")
    # A category that came back for another game (as a name lookup can return) is never used.
    assert match_category(228525, [{"id": "2", "name": "Hades 2", "igdb_id": "134619"}]).status == "none"
    assert match_category(228525, []).status == "none"
    assert match_category(228525, [{"id": "1", "name": "A", "igdb_id": "228525"},
                                   {"id": "3", "name": "B", "igdb_id": "228525"}]).status == "ambiguous"


def test_streams_are_parsed_conservatively():
    parsed = parse_streams([stream("good_name", 10, title="  lots   of\nspace "), stream("bad name", 5),
                            stream("x" * 30, 5), {**stream("rerun", 5), "type": ""}, stream("neg", -1),
                            stream("zero", 0)])
    assert [(item.user_login, item.viewers, item.title) for item in parsed] == [
        ("good_name", 10, "lots of space"), ("zero", 0, "Playing")]
    assert parsed[0].url == "https://www.twitch.tv/good_name"
    assert category_url("HλLF-LIFE") == "https://www.twitch.tv/directory/game/H%CE%BBLF-LIFE"
    assert category_url("A/B & C") == "https://www.twitch.tv/directory/game/A%2FB%20%26%20C"


def test_the_client_uses_the_app_token_and_never_leaks_credentials():
    fake = FakeHelix()
    with fake.client() as client:
        assert client.categories_for_igdb_id(1020)[0]["id"] == "32982"
        assert [item["user_login"] for item in client.live_streams("32982", first=1)] == ["alpha"]
    token_request, games_request = fake.requests[:2]
    assert parse_qs(token_request.content.decode())["grant_type"] == ["client_credentials"]
    assert CLIENT_SECRET not in str(token_request.url)  # the secret travels in the body only
    assert games_request.headers["Client-Id"] == CLIENT_ID
    assert games_request.headers["Authorization"] == "Bearer twitch-token-1"
    assert fake.tokens == 1  # one token for both calls
    assert not any(secret in repr(client) for secret in SECRETS)


def test_rate_limits_and_failures_become_clear_errors():
    fake = FakeHelix()
    fake.statuses = [429]
    with fake.client() as client, pytest.raises(TwitchRateLimited) as limited:
        client.categories_for_igdb_id(1020)
    assert limited.value.reset_at == datetime.fromtimestamp(1795100000, tz=timezone.utc)
    fake.statuses = [503, 503]  # one retry, then a clear error
    with fake.client() as client, pytest.raises(TwitchError) as failed:
        client.live_streams("32982")
    fake.statuses = [400]
    with fake.client() as client, pytest.raises(TwitchError) as refused:
        client.live_streams("32982")
    for error in (limited.value, failed.value, refused.value):
        assert not any(secret in str(error) for secret in SECRETS)
    fake.statuses = [401]  # an expired token is renewed once
    with fake.client() as client:
        assert client.categories_for_igdb_id(1020)
    assert fake.tokens >= 2


def test_refresh_and_view_cover_every_state(settings):
    fake = FakeHelix()
    fake.categories[52189] = [{"id": "777", "name": "Upcoming Saga", "igdb_id": "52189"}]
    conn = store.initialize(settings.database)
    try:
        with fake.client() as client:
            refresh_live(conn, client, "271590", 1020, NOW)
            refresh_live(conn, client, "igdb-52189", 52189, NOW)  # a category without live streams
            refresh_live(conn, client, "igdb-9", 9, NOW)  # Twitch has no category for this IGDB id
        ok = live_view(store.twitch_live_row(conn, "271590"), NOW, configured=True, igdb_id=1020, upcoming=False)
        assert ok["state"] == "ok" and [s.user_login for s in ok["streams"]] == ["alpha", "beta"]
        assert ok["category"] == "Grand Theft Auto V" and not ok["needs_check"] and not ok["stale"]
        empty = live_view(store.twitch_live_row(conn, "igdb-52189"), NOW, configured=True, igdb_id=52189, upcoming=True)
        assert (empty["state"], empty["upcoming"]) == ("empty", True)
        assert live_view(store.twitch_live_row(conn, "igdb-9"), NOW, configured=True, igdb_id=9,
                         upcoming=False)["state"] == "no_category"
        later = live_view(store.twitch_live_row(conn, "271590"), NOW + timedelta(minutes=20), configured=True,
                          igdb_id=1020, upcoming=False)
        assert later["stale"] and later["needs_check"]  # shown with "may have ended", and checked again
        assert live_view(None, NOW, configured=False, igdb_id=1020, upcoming=False)["state"] == "not_configured"
        assert live_view(None, NOW, configured=True, igdb_id=None, upcoming=False)["state"] == "no_igdb"
        pending = live_view(None, NOW, configured=True, igdb_id=1020, upcoming=False)
        assert pending["state"] == "pending" and pending["needs_check"]
    finally:
        conn.close()


def test_a_failed_check_keeps_recent_streams_and_waits_before_asking_again(settings):
    fake = FakeHelix()
    conn = store.initialize(settings.database)
    try:
        with fake.client() as client:
            refresh_live(conn, client, "271590", 1020, NOW)
            reset_at = NOW + timedelta(minutes=30)
            fake.reset = str(int(reset_at.timestamp()))
            fake.statuses = [429]
            refresh_live(conn, client, "271590", 1020, NOW + timedelta(minutes=10))
        row = store.twitch_live_row(conn, "271590")
        view = live_view(row, NOW + timedelta(minutes=10), configured=True, igdb_id=1020, upcoming=False)
        assert view["state"] == "rate_limited" and view["previous"] and view["stale"] and view["streams"]
        assert view["retry_at"] == reset_at
        assert not refresh_due(row, NOW + timedelta(minutes=29))  # not before Twitch's reset time
        assert refresh_due(row, reset_at)
        with fake.client() as client:
            fake.statuses = [503, 503]
            refresh_live(conn, client, "271590", 1020, NOW + timedelta(hours=2))
        row = store.twitch_live_row(conn, "271590")
        late = live_view(row, NOW + timedelta(hours=2), configured=True, igdb_id=1020, upcoming=False)
        assert late["state"] == "error" and not late["previous"] and not late["streams"]  # too old to show
        assert not refresh_due(row, NOW + timedelta(hours=2, minutes=1))
        assert refresh_due(row, NOW + timedelta(hours=2, minutes=3))
        assert "twitch-token" not in str(tuple(row)) and CLIENT_SECRET not in str(tuple(row))
    finally:
        conn.close()


def test_twitch_answers_are_deleted_after_24_hours(settings):
    fake = FakeHelix()
    conn = store.initialize(settings.database)
    try:
        with fake.client() as client:
            refresh_live(conn, client, "271590", 1020, NOW)
            refresh_live(conn, client, "igdb-52189", 52189, NOW + timedelta(hours=23))
            # Any later check first deletes what is older than 24 hours (Twitch's caching limit).
            refresh_live(conn, client, "igdb-1", 1, NOW + timedelta(hours=25))
        assert store.twitch_live_row(conn, "271590") is None
        assert store.twitch_live_row(conn, "igdb-52189") is not None
        assert live_view(store.twitch_live_row(conn, "igdb-52189"), NOW + timedelta(hours=48), configured=True,
                         igdb_id=52189, upcoming=True)["state"] == "pending"  # never shown after 24 hours
    finally:
        conn.close()
