"""Optional IGDB client: suggests catalog candidates and their Steam app ids (server side only).

IGDB (https://api-docs.igdb.com/, read 2026-11-15) is free for non-commercial use under the Twitch
Developer Services Agreement. Authentication uses a Twitch application's Client ID and Client Secret:
the client-credentials grant returns an app access token, which every API request sends with the
Client-ID header. The token is kept in memory only, never stored, logged or shown. The client ID and
secret travel in the body of the token request (as Twitch documents), so they never appear in a URL.

Limits (from the IGDB docs): 4 requests per second, 8 open requests, at most 500 results per query.
This client sends one request at a time, at least 0.3 s apart, and retries 429 and 5xx answers.

Game Librarian uses IGDB only to suggest games and to read the Steam app id IGDB records for them
(external_games with external_game_source 1 = Steam; the field replaced the deprecated `category`).
Nothing IGDB returns is displayed on the website: every displayed fact still comes from Steam or
from the curated files. See README.md, "IGDB (optional)".
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from .config import USER_AGENT

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.igdb.com/v4"
MIN_INTERVAL_SECONDS = 0.3  # IGDB allows 4 requests per second
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0  # waits 2 s, 4 s, then 8 s between attempts
TOKEN_REFRESH_MARGIN_SECONDS = 300  # renew a token this long before IGDB says it expires
MAX_RESULTS_PER_QUERY = 500
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

STEAM_SOURCE = 1  # external_game_sources id of Steam (the old external_games.category value)
PC_PLATFORM = 6  # "PC (Microsoft Windows)"
# game_type values worth suggesting: main game, standalone expansion, remake, remaster, expanded game, port.
# DLC, expansions, bundles, mods, episodes, seasons, forks, packs and updates are left out.
CANDIDATE_GAME_TYPES = (0, 4, 8, 9, 10, 11)


class IgdbError(Exception):
    """IGDB or Twitch could not be reached, refused the credentials, or kept failing."""


class IgdbClient:
    """Small IGDB client with token handling, rate limiting and retries.

    `sleep` and `clock` are injectable so tests run instantly and deterministically.
    """

    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        if not client_id or not client_secret:
            raise IgdbError("IGDB needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET (a Twitch application's credentials).")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at: float | None = None

    def __repr__(self) -> str:  # never show the credentials or the token
        return "IgdbClient(credentials hidden)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> IgdbClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ helpers

    def _redact(self, text: str) -> str:
        """Remove the client id, secret and token from a message, in case a server echoes one."""
        for secret in (self._client_secret, self._client_id, self._token):
            if secret:
                text = text.replace(secret, "[hidden]")
        return text[:300]

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            remaining = MIN_INTERVAL_SECONDS - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST with pacing and retries for timeouts, 429 and 5xx. Other answers are returned as they are."""
        last_problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn()
            try:
                response = self._http.post(url, **kwargs)
            except httpx.TransportError as exc:  # timeouts, DNS failures, refused connections
                last_problem = type(exc).__name__
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_problem = f"HTTP {response.status_code}"
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        raise IgdbError(f"{url} failed after {MAX_ATTEMPTS} attempts ({last_problem})")

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("message", "")) if isinstance(body, dict) else ""

    # ------------------------------------------------------------------ authentication

    def _access_token(self, *, renew: bool = False) -> str:
        """An app access token, requested from Twitch when there is none, it is about to expire, or `renew`."""
        if self._token and not renew and self._clock() < self._token_expires_at:
            return self._token
        response = self._post(TOKEN_URL, data={
            "client_id": self._client_id, "client_secret": self._client_secret, "grant_type": "client_credentials",
        })
        if response.status_code != 200:
            detail = self._redact(self._message(response))
            raise IgdbError(f"Twitch refused the IGDB credentials (HTTP {response.status_code}"
                            + (f": {detail}" if detail else "") + "). Check IGDB_CLIENT_ID and IGDB_CLIENT_SECRET.")
        try:
            body = response.json()
            token, expires_in = body["access_token"], int(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError):
            raise IgdbError("Twitch answered the token request without an access token.") from None
        self._token = token
        self._token_expires_at = self._clock() + max(0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        return token

    # ------------------------------------------------------------------ queries

    def _send_query(self, url: str, body: str, *, renew: bool) -> httpx.Response:
        headers = {"Client-ID": self._client_id, "Authorization": f"Bearer {self._access_token(renew=renew)}",
                   "Accept": "application/json"}
        return self._post(url, headers=headers, content=body.encode("utf-8"))

    def query(self, endpoint: str, body: str) -> list[dict]:
        """Run one Apicalypse query (`body`) against an IGDB endpoint, such as "games"."""
        url = f"{API_BASE}/{endpoint}"
        response = self._send_query(url, body, renew=False)
        if response.status_code == 401:  # the token expired or was revoked early: renew it once
            response = self._send_query(url, body, renew=True)
        if response.status_code != 200:
            detail = self._redact(self._message(response))
            raise IgdbError(f"IGDB {endpoint} query failed (HTTP {response.status_code}"
                            + (f": {detail}" if detail else "") + ").")
        try:
            data = response.json()
        except ValueError:
            raise IgdbError(f"IGDB {endpoint} answered with something other than JSON.") from None
        if not isinstance(data, list):
            raise IgdbError(f"IGDB {endpoint} answered with an unexpected shape.")
        return data

    def popular_pc_games(self, *, limit: int, offset: int = 0, min_ratings: int = 20) -> list[dict]:
        """PC games with at least `min_ratings` IGDB user and critic ratings, the most rated first.

        Returns at most `limit` games, fetched in pages of up to 500, with each game's name, first
        release date, game type, rating count, IGDB URL and Steam ids (external_games).
        """
        games: list[dict] = []
        while len(games) < limit:
            page_size = min(MAX_RESULTS_PER_QUERY, limit - len(games))
            body = (
                "fields name,first_release_date,game_type,total_rating_count,url,"
                "external_games.external_game_source,external_games.uid;"
                f" where platforms = ({PC_PLATFORM}) & game_type = ({','.join(map(str, CANDIDATE_GAME_TYPES))})"
                f" & total_rating_count >= {int(min_ratings)};"
                f" sort total_rating_count desc; limit {page_size}; offset {int(offset) + len(games)};"
            )
            page = self.query("games", body)
            games.extend(page)
            if len(page) < page_size:
                break
        return games[:limit]


def steam_appids(game: dict) -> list[int]:
    """The distinct Steam app ids IGDB records for a game (external games from the Steam source)."""
    appids = []
    for external in game.get("external_games") or []:
        if not isinstance(external, dict) or external.get("external_game_source") != STEAM_SOURCE:
            continue
        uid = str(external.get("uid") or "").strip()
        if uid.isdigit() and int(uid) not in appids:
            appids.append(int(uid))
    return appids
