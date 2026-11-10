"""HTTP client for the Steam endpoints Game Librarian uses.

This is the only module that talks to Steam. It adds a request timeout, a minimum
delay between requests to the same host, and retries with exponential backoff.
Every method raises SteamError when Steam cannot be reached or keeps failing.

Documented endpoints (Steamworks Web API reference):
  * IStoreService/GetAppList          - catalog discovery, needs an API key
  * IUserReviewsService/GetAppReviews - review summary and helpful reviews
Public but undocumented endpoints (may change without notice):
  * store/api/appdetails              - descriptions, header image, genres, modes
  * IStoreBrowseService/GetItems      - community tags, portrait cover, trailer clips, screenshots
  * IStoreService/GetTagList          - tag id -> tag name
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from .config import STORE_API_BASE, USER_AGENT, WEB_API_BASE

# Seconds to wait between two requests to the same host. Steam does not publish
# store limits; about 200 appdetails calls per 5 minutes is the commonly observed
# ceiling, so the store host gets the slower pace.
MIN_INTERVAL_BY_HOST = {"store.steampowered.com": 1.5, "api.steampowered.com": 1.0}
DEFAULT_MIN_INTERVAL = 1.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0  # waits 2 s, 4 s, then 8 s between attempts
MAX_RETRY_AFTER_SECONDS = 60.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# IUserReviewsService enums (see the GetAppReviews documentation).
REVIEW_FILTER_HELPFUL = 0
REVIEW_TYPE_ALL = 0
REVIEW_TYPE_POSITIVE = 1
REVIEW_TYPE_NEGATIVE = 2

APP_LIST_PAGE_SIZE = 10_000
STORE_ITEMS_TAG_COUNT = 20


class SteamError(Exception):
    """Steam could not be reached, or answered with an error, after all retries."""


class SteamClient:
    """Small Steam client with rate limiting and retries.

    `sleep` and `clock` are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self._http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> SteamClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ HTTP core

    def _wait_for_turn(self, host: str) -> None:
        """Sleep until the minimum interval since the previous request to `host` has passed."""
        interval = MIN_INTERVAL_BY_HOST.get(host, DEFAULT_MIN_INTERVAL)
        previous = self._last_request_at.get(host)
        if previous is not None:
            remaining = interval - (self._clock() - previous)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at[host] = self._clock()

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        """Return the server's Retry-After delay in seconds, if it sent a numeric one."""
        value = response.headers.get("Retry-After", "")
        if value.strip().isdigit():
            return min(float(value), MAX_RETRY_AFTER_SECONDS)
        return None

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        """GET `url` and return its JSON body, retrying timeouts, 429 and 5xx responses."""
        host = urlsplit(url).hostname or ""
        last_problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn(host)
            delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError as exc:  # timeouts, DNS failures, refused connections
                last_problem = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        last_problem = "response was not valid JSON"
                elif response.status_code in RETRYABLE_STATUS:
                    last_problem = f"HTTP {response.status_code}"
                    delay = self._retry_after_seconds(response) or delay
                else:
                    raise SteamError(f"HTTP {response.status_code} from {url}")
            if attempt < MAX_ATTEMPTS:
                self._sleep(delay)
        raise SteamError(f"{url} failed after {MAX_ATTEMPTS} attempts ({last_problem})")

    def _call_service(self, interface_method: str, payload: dict[str, Any], key: str | None = None) -> dict:
        """Call a Web API service method, which takes its arguments as `input_json`."""
        params: dict[str, Any] = {"input_json": json.dumps(payload)}
        if key:
            params["key"] = key
        data = self.get_json(f"{WEB_API_BASE}/{interface_method}/v1/", params)
        response = data.get("response") if isinstance(data, dict) else None
        return response if isinstance(response, dict) else {}

    # ------------------------------------------------------------------ endpoints

    def get_app_list_page(self, if_modified_since: int, last_appid: int) -> dict:
        """Return one page of games changed since `if_modified_since` (IStoreService/GetAppList).

        The page has `apps`, and `have_more_results` plus `last_appid` for the next page.
        """
        if not self.api_key:
            raise SteamError("Catalog discovery needs a Steam Web API key (STEAM_API_KEY).")
        payload = {
            "include_games": True,
            "max_results": APP_LIST_PAGE_SIZE,
            "if_modified_since": if_modified_since,
            "last_appid": last_appid,
        }
        return self._call_service("IStoreService/GetAppList", payload, key=self.api_key)

    def get_tag_names(self, language: str = "english") -> dict[int, str]:
        """Return Steam's tag id -> localized tag name map (IStoreService/GetTagList)."""
        response = self._call_service("IStoreService/GetTagList", {"language": language})
        return {int(tag["tagid"]): tag["name"] for tag in response.get("tags", [])}

    def get_store_items(self, appids: list[int], language: str = "english", country: str = "US") -> dict[int, dict]:
        """Return tags, asset names, trailers and screenshots for several apps in one call.

        Uses IStoreBrowseService/GetItems. Apps Steam does not know, or will not show,
        are left out of the result.
        """
        payload = {
            "ids": [{"appid": appid} for appid in appids],
            "context": {"language": language, "country_code": country},
            "data_request": {
                "include_assets": True,
                "include_tag_count": STORE_ITEMS_TAG_COUNT,
                "include_trailers": True,
                "include_screenshots": True,
            },
        }
        response = self._call_service("IStoreBrowseService/GetItems", payload)
        items = {}
        for item in response.get("store_items", []):
            if item.get("success") == 1 and item.get("appid"):
                items[int(item["appid"])] = item
        return items

    def get_app_details(self, appid: int, language: str = "english", country: str = "us") -> dict | None:
        """Return the store details for one app, or None when Steam has none (store/api/appdetails)."""
        data = self.get_json(f"{STORE_API_BASE}/appdetails", {"appids": appid, "l": language, "cc": country})
        entry = data.get(str(appid)) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not entry.get("success") or not isinstance(entry.get("data"), dict):
            return None
        return entry["data"]

    def get_reviews(
        self,
        appid: int,
        *,
        review_type: int,
        languages: list[str],
        num_per_page: int,
        day_range: int = 0,
    ) -> dict:
        """Return the first page of reviews, most helpful first (IUserReviewsService/GetAppReviews).

        Purchase type and off-topic filtering keep Steam's defaults, which match the
        store page: Steam purchasers only, review bombs excluded. An app without
        reviews returns an empty dict.
        """
        payload = {
            "appid": appid,
            "filter": REVIEW_FILTER_HELPFUL,
            "review_type": review_type,
            "languages": languages,
            "num_per_page": num_per_page,
            "day_range": day_range,
        }
        return self._call_service("IUserReviewsService/GetAppReviews", payload)
