"""Optional IGDB integration, tested only against mocked Twitch and IGDB answers (no real credentials exist here).

Checks the documented authentication (client-credentials token, Client-ID and Bearer headers), token reuse
and renewal, rate pacing and retries, bounded paging, Steam-id matching, and that no secret ever reaches a
URL, an error message, the command output, the database or repr().
"""

import json
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
import pytest

from game_librarian import __main__ as cli
from game_librarian import candidates, store
from game_librarian.candidates import NEEDS_REVIEW
from game_librarian.igdb import API_BASE, MIN_INTERVAL_SECONDS, TOKEN_URL, IgdbClient, IgdbError, steam_appids
from tests.conftest import write_csv

CLIENT_ID = "test-client-id-abc123"
CLIENT_SECRET = "test-client-secret-xyz789"
SECRETS = (CLIENT_ID, CLIENT_SECRET, "token-1", "token-2")


class FakeIgdb:
    """Twitch's token endpoint and IGDB's /games endpoint, in memory."""

    def __init__(self, games=()):
        self.games = list(games)
        self.tokens_issued = 0
        self.token_status = 200
        self.games_statuses: list[int] = []  # answers to send before a normal one, such as [429] or [401]
        self.requests: list[httpx.Request] = []
        self.expires_in = 5_000_000

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"status": self.token_status, "message": "invalid client secret"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}",
                                             "expires_in": self.expires_in, "token_type": "bearer"})
        if str(request.url) == f"{API_BASE}/games":
            if self.games_statuses:
                return httpx.Response(self.games_statuses.pop(0), json={"message": "try later"})
            body = request.content.decode()
            limit = int(body.split("limit ")[1].split(";")[0]) if "limit " in body else 10  # IGDB's default
            offset = int(body.split("offset ")[1].split(";")[0]) if "offset " in body else 0
            return httpx.Response(200, json=self.games[offset:offset + limit])
        return httpx.Response(404)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(fake: FakeIgdb, clock: Clock | None = None) -> IgdbClient:
    clock = clock or Clock()
    return IgdbClient(CLIENT_ID, CLIENT_SECRET, http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                      sleep=clock.sleep, clock=clock)


def game(igdb_id, name, *steam_ids, source=1, year=2020):
    return {"id": igdb_id, "name": name, "first_release_date": int((year - 1970) * 365.25 * 86400) + 86400 * 30,
            "external_games": [{"id": igdb_id * 10 + n, "external_game_source": source, "uid": str(uid)}
                               for n, uid in enumerate(steam_ids)]}


def test_authentication_follows_the_documented_client_credentials_flow():
    fake = FakeIgdb([game(1, "Alpha", 100)])
    with make_client(fake) as client:
        assert client.query("games", "fields name; limit 1;") == fake.games
    token_request, query = fake.requests
    assert token_request.method == "POST" and str(token_request.url) == TOKEN_URL  # no secret in the URL
    form = parse_qs(token_request.content.decode())
    assert form == {"client_id": [CLIENT_ID], "client_secret": [CLIENT_SECRET], "grant_type": ["client_credentials"]}
    assert query.method == "POST" and query.headers["Client-ID"] == CLIENT_ID
    assert query.headers["Authorization"] == "Bearer token-1" and query.content == b"fields name; limit 1;"


def test_the_token_is_reused_renewed_before_expiry_and_renewed_once_when_rejected():
    fake = FakeIgdb([game(1, "Alpha", 100)])
    clock = Clock()
    with make_client(fake, clock) as client:
        client.query("games", "fields name; limit 1; offset 0;")
        client.query("games", "fields name; limit 1; offset 0;")
        assert fake.tokens_issued == 1  # one token for both queries
        clock.now += fake.expires_in  # the token is about to expire
        client.query("games", "fields name; limit 1; offset 0;")
        assert fake.tokens_issued == 2
        fake.games_statuses = [401]  # Twitch revoked the token early
        assert client.query("games", "fields name; limit 1; offset 0;") == fake.games
        assert fake.tokens_issued == 3


def test_requests_are_paced_below_four_per_second():
    clock = Clock()  # time passes only when the client sleeps
    with make_client(FakeIgdb([game(1, "Alpha", 100)]), clock) as client:
        client.query("games", "fields name;")
        client.query("games", "fields name;")
    assert clock.sleeps == [MIN_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS]  # token, then each query 0.3 s later
    assert 1 / MIN_INTERVAL_SECONDS < 4  # the documented limit is 4 requests per second


def test_rate_limited_and_failing_answers_are_retried_with_backoff():
    fake = FakeIgdb([game(1, "Alpha", 100)])
    clock = Clock()
    with make_client(fake, clock) as client:
        fake.games_statuses = [429, 503]
        assert client.query("games", "fields name;") == fake.games
    assert len(fake.requests) == 4  # token, 429, 503, success
    assert clock.sleeps == [MIN_INTERVAL_SECONDS, 2.0, 4.0]  # pacing, then 2 s and 4 s of backoff


def test_failures_explain_themselves_without_revealing_any_secret():
    fake = FakeIgdb()
    fake.token_status = 400
    with make_client(fake) as client, pytest.raises(IgdbError) as refused:
        client.query("games", "fields name;")
    assert "HTTP 400" in str(refused.value) and "IGDB_CLIENT_SECRET" in str(refused.value)
    fake = FakeIgdb()
    fake.games_statuses = [503] * 4
    with make_client(fake) as client, pytest.raises(IgdbError) as unavailable:
        client.query("games", "fields name;")
    for error in (refused.value, unavailable.value):
        assert not any(secret in str(error) for secret in SECRETS)
    assert not any(secret in repr(make_client(FakeIgdb())) for secret in SECRETS)
    with pytest.raises(IgdbError, match="IGDB_CLIENT_ID"):
        IgdbClient("", "")


def test_popular_games_are_fetched_in_bounded_pages():
    fake = FakeIgdb([game(n, f"Game {n}", 1000 + n) for n in range(1, 701)])
    with make_client(fake) as client:
        games = client.popular_pc_games(limit=650)
    assert len(games) == 650
    bodies = [request.content.decode() for request in fake.requests if str(request.url).endswith("/games")]
    assert len(bodies) == 2 and "limit 500; offset 0;" in bodies[0] and "limit 150; offset 500;" in bodies[1]
    assert "external_games.external_game_source" in bodies[0] and "platforms = (6)" in bodies[0]
    assert "game_type = (0,4,8,9,10,11)" in bodies[0]  # no DLC, bundles, mods or episodes


def test_steam_ids_come_only_from_the_steam_source():
    assert steam_appids(game(1, "Alpha", 100, 100, 200)) == [100, 200]
    assert steam_appids(game(1, "Alpha", 100, source=5)) == []  # GOG, not Steam
    assert steam_appids({"external_games": [{"external_game_source": 1, "uid": "not-a-number"}]}) == []


def test_igdb_suggestions_become_candidates_by_steam_id_or_wait_for_review(settings):
    conn = store.initialize(settings.database)
    try:
        store.upsert_discovered_apps(conn, [{"appid": 700, "name": "Echo Quest"}, {"appid": 701, "name": "ECHO QUEST"},
                                            {"appid": 702, "name": "Solo Title"}], now="2026-11-15T00:00:00+00:00")
        fake = FakeIgdb([
            game(1, "Steam Game", 300),  # one Steam id: a reliable match
            game(2, "Echo Quest"),  # no Steam id; two index apps share the title: ambiguous
            game(3, "Two Editions", 400, 401),  # several Steam ids: a person must choose
            game(4, "Catalog Game", 100),  # already in the catalog
            game(5, "Solo Title"),  # no Steam id; one index app has the title: still only a suggestion
        ])
        with make_client(fake) as client:
            report = candidates.add_from_igdb(conn, client, limit=10, catalog_appids={100})
        assert report.status() == "success" and report.counts == {"new": 4, "skipped_in_catalog": 1}
        steam_game = store.candidate(conn, appid=300)
        assert (steam_game["match_method"], steam_game["status"], steam_game["igdb_id"]) == ("igdb_steam_id", "pending", 1)
        echo = store.candidate(conn, igdb_id=2)
        assert (echo["appid"], echo["status"], json.loads(echo["suggested_appids"])) == (None, NEEDS_REVIEW, [700, 701])
        assert "Matched by title only: 2 Steam apps" in echo["reason"]
        editions = store.candidate(conn, igdb_id=3)
        assert json.loads(editions["suggested_appids"]) == [400, 401] and "lists 2 Steam apps" in editions["reason"]
        solo = store.candidate(conn, igdb_id=5)
        assert solo["status"] == NEEDS_REVIEW and json.loads(solo["suggested_appids"]) == [702]
        dumped = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM candidates"))
        assert not any(secret in dumped for secret in SECRETS)  # provenance only, never credentials
    finally:
        conn.close()


def test_the_command_explains_how_to_enable_igdb_and_never_calls_it_without_credentials(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "IgdbClient", lambda *args, **kwargs: pytest.fail("IGDB must not be called"))
    assert cli.main(["candidates", "add", "--source", "igdb"]) == 1
    assert "IGDB_CLIENT_ID and IGDB_CLIENT_SECRET" in capsys.readouterr().err


def test_the_command_with_credentials_prints_and_stores_no_secret(settings, monkeypatch, capsys):
    configured = replace(settings, igdb_client_id=CLIENT_ID, igdb_client_secret=CLIENT_SECRET)
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "100", "title": "Catalog Game"}])
    fake = FakeIgdb([game(1, "Steam Game", 300)])
    monkeypatch.setattr(cli, "load_settings", lambda: configured)
    monkeypatch.setattr(cli, "IgdbClient", lambda client_id, client_secret: make_client(fake))
    assert cli.main(["candidates", "add", "--source", "igdb", "--limit", "5"]) == 0
    assert "Added candidates from igdb: success" in capsys.readouterr().out
    fake.token_status = 401  # Twitch now refuses the credentials
    assert cli.main(["candidates", "add", "--source", "igdb", "--limit", "5"]) == 1
    output = capsys.readouterr()
    assert "Twitch refused the IGDB credentials (HTTP 401" in output.out  # recorded in the run summary
    assert not any(secret in output.out + output.err for secret in SECRETS)
    assert "Settings(" in repr(configured) and CLIENT_SECRET not in repr(configured)
    conn = store.connect(settings.database)
    try:
        runs = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM candidate_runs"))
        assert not any(secret in runs for secret in SECRETS)
    finally:
        conn.close()
