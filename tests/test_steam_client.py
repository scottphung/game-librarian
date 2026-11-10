"""Steam client: retries with backoff, Retry-After, rate limiting and error handling."""

import httpx
import pytest

from game_librarian.steam import SteamClient, SteamError

API_URL = "https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/"
STORE_URL = "https://store.steampowered.com/api/appdetails"


class FakeTime:
    """Sleeping advances the clock instantly and records how long each wait was."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def clock(self) -> float:
        return self.now


def make_client(responses: list, fake_time: FakeTime, api_key: str | None = None) -> tuple[SteamClient, list]:
    """A client whose transport returns `responses` in order (an exception instance is raised)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        result = responses[min(len(calls), len(responses)) - 1]  # the last response repeats
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result.status_code, headers=result.headers, content=result.read())  # a fresh copy

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SteamClient(api_key=api_key, http=http, sleep=fake_time.sleep, clock=fake_time.clock), calls


def test_retries_server_errors_with_exponential_backoff():
    fake_time = FakeTime()
    client, calls = make_client([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})], fake_time)
    assert client.get_json(API_URL, {}) == {"ok": 1}
    assert len(calls) == 3
    assert fake_time.sleeps == [2.0, 4.0]


def test_honors_retry_after_on_rate_limit():
    fake_time = FakeTime()
    client, _ = make_client([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})], fake_time)
    client.get_json(API_URL, {})
    assert fake_time.sleeps == [7.0]


def test_retries_timeouts_and_connection_errors():
    fake_time = FakeTime()
    timeout = httpx.ReadTimeout("slow")
    client, calls = make_client([timeout, httpx.ConnectError("refused"), httpx.Response(200, json={})], fake_time)
    client.get_json(API_URL, {})
    assert len(calls) == 3


def test_gives_up_after_the_last_attempt():
    fake_time = FakeTime()
    client, calls = make_client([httpx.Response(503)], fake_time)
    with pytest.raises(SteamError, match="failed after 4 attempts"):
        client.get_json(API_URL, {})
    assert len(calls) == 4
    assert fake_time.sleeps == [2.0, 4.0, 8.0]


def test_does_not_retry_client_errors():
    fake_time = FakeTime()
    client, calls = make_client([httpx.Response(403)], fake_time)
    with pytest.raises(SteamError, match="HTTP 403"):
        client.get_json(API_URL, {})
    assert len(calls) == 1
    assert fake_time.sleeps == []


def test_invalid_json_is_retried_then_reported():
    fake_time = FakeTime()
    client, calls = make_client([httpx.Response(200, text="<html>maintenance</html>")], fake_time)
    with pytest.raises(SteamError, match="not valid JSON"):
        client.get_json(API_URL, {})
    assert len(calls) == 4


def test_waits_between_requests_to_the_same_host():
    fake_time = FakeTime()
    client, _ = make_client([httpx.Response(200, json={})], fake_time)
    client.get_json(API_URL, {})
    client.get_json(API_URL, {})  # api.steampowered.com: 1 second apart
    client.get_json(STORE_URL, {})  # a different host: no wait
    client.get_json(STORE_URL, {})  # store.steampowered.com: 1.5 seconds apart
    assert fake_time.sleeps == [1.0, 1.5]


def test_app_details_returns_none_when_steam_has_no_data():
    fake_time = FakeTime()
    client, _ = make_client([httpx.Response(200, json={"42": {"success": False}})], fake_time)
    assert client.get_app_details(42) is None


def test_discovery_requires_an_api_key():
    client, calls = make_client([httpx.Response(200, json={})], FakeTime())
    with pytest.raises(SteamError, match="STEAM_API_KEY"):
        client.get_app_list_page(if_modified_since=0, last_appid=0)
    assert calls == []


def test_discovery_sends_the_key_and_paging_arguments():
    client, calls = make_client([httpx.Response(200, json={"response": {"apps": []}})], FakeTime(), api_key="secret")
    client.get_app_list_page(if_modified_since=123, last_appid=456)
    params = calls[0].url.params
    assert params["key"] == "secret"
    assert '"if_modified_since": 123' in params["input_json"] and '"last_appid": 456' in params["input_json"]
