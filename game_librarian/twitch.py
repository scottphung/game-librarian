"""Twitch application access (shared with IGDB) and the "Watch it live" streams, server side only.

Authentication (https://dev.twitch.tv/docs/authentication/, read 2026-11-19): a Twitch application's
Client ID and Client Secret are exchanged for an app access token with the client-credentials grant.
IGDB uses the same flow, so Game Librarian reuses IGDB_CLIENT_ID and IGDB_CLIENT_SECRET for both. The
token stays in this process's memory: it is never stored, logged, shown or sent to a browser.

Helix endpoints used (https://dev.twitch.tv/docs/api/reference/, read 2026-11-19):
  * Get Games, GET /helix/games?igdb_id=...: the Twitch category whose `igdb_id` equals the game's IGDB id.
    A category is matched only by that identifier, never by name: a name lookup for "hades 2" returned an
    unrelated category on 2026-11-19, while igdb_id 228525 returned the right "Hades II" category.
  * Get Streams, GET /helix/streams?game_id=...&type=live: live streams in the category, most viewers first.
  * Get Top Games, GET /helix/games/top?first=100: "the categories and games that have the most viewers on
    Twitch", "sorted by the number of viewers, with the most popular first". Each entry has id, name,
    box_art_url and igdb_id ("" when Twitch has none); there is no viewer number, so only the order is used.
    Checked live on 2026-11-19: Grand Theft Auto VI (igdb_id 52189) first, Just Chatting second (no igdb_id).
Rate limit: a bucket of 800 points per minute per app; a 429 answer carries Ratelimit-Reset.

Which game a top category stands for (resolve_category, checked live on 2026-11-21 against the 100 categories of that
day's list): IGDB also records each game's Twitch category itself (external_games, source 14 = Twitch). For 80
categories Twitch's igdb_id and IGDB's record named the same game; IGDB alone named Dota 2 (Twitch gives it no
igdb_id) and Counter-Strike; for Call of Duty: Warzone they named different games; 17 had no game on either side
(Just Chatting, IRL, Slots and others, but also StarCraft II). A reviewer's row in curated/twitch_categories.csv
settles a category either way, or marks it as a game series (Counter-Strike, whose streams can be Counter-Strike 2 or
the re-released Counter-Strike: Global Offensive), which is listed as a series and never as one of its games.

Twitch Developer Services Agreement (https://legal.twitch.com/en/legal/developer-agreement/, last modified
12/04/2024, read 2026-11-19): Schedule 1 C, "Do not store copies of Twitch Content or Program Materials,
unless you ... cache such information for only a twenty-four hour time period"; Schedule 1 B, research
projects "that examine trends over specific time periods" need a separate agreement; VII.C, "a clear path to
the source from displays of Program Materials". Game Librarian therefore keeps streams and the top-games list
for at most 24 hours (store.purge_twitch_cache, store.purge_twitch_top_games), links every stream and
category to Twitch, and stores no history.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx

from . import store
from .config import USER_AGENT

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_BASE = "https://api.twitch.tv/helix"
TOKEN_REFRESH_MARGIN_SECONDS = 300  # renew a token this long before Twitch says it expires
STREAMS_SHOWN = 5
TOP_GAMES_PAGE = 100  # Get Top Games returns at most 100 categories per request
# Twitch user logins are letters, digits and underscores; anything else is not linked.
LOGIN = re.compile(r"[A-Za-z0-9_]{1,25}")


class TwitchError(Exception):
    """Twitch could not be reached, refused the credentials, or answered with something unexpected."""


class TwitchRateLimited(TwitchError):
    """Twitch answered 429: the app's request bucket is empty until `reset_at` (UTC), when Twitch says."""

    def __init__(self, message: str, reset_at: datetime | None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class TwitchAppClient:
    """A Twitch application's app access token, plus paced and retried requests.

    Shared by the IGDB client and the Helix client. `sleep` and `clock` are injectable so tests run
    instantly and deterministically. Subclasses set the error type, the service name used in
    messages, the pacing and which answers are retried.
    """

    error: type[Exception] = TwitchError
    service = "Twitch"
    min_interval_seconds = 0.3
    max_attempts = 4
    backoff_seconds = 2.0  # waits 2 s, 4 s, then 8 s between attempts
    retryable_status = frozenset({429, 500, 502, 503, 504})
    timeout = httpx.Timeout(20.0, connect=10.0)

    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        if not client_id or not client_secret:
            raise self.error(f"{self.service} needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET "
                             "(a Twitch application's credentials).")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=self.timeout, headers={"User-Agent": USER_AGENT})
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at: float | None = None

    def __repr__(self) -> str:  # never show the credentials or the token
        return f"{type(self).__name__}(credentials hidden)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
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
            remaining = self.min_interval_seconds - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send with pacing and retries for timeouts and retryable answers. Other answers are returned as they are."""
        last_problem = ""
        for attempt in range(1, self.max_attempts + 1):
            self._wait_for_turn()
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:  # timeouts, DNS failures, refused connections
                last_problem = type(exc).__name__
            else:
                if response.status_code not in self.retryable_status:
                    return response
                last_problem = f"HTTP {response.status_code}"
            if attempt < self.max_attempts:
                self._sleep(self.backoff_seconds * 2 ** (attempt - 1))
        raise self.error(f"{url} failed after {self.max_attempts} attempts ({last_problem})")

    @staticmethod
    def _message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("message", "")) if isinstance(body, dict) else ""

    def _access_token(self, *, renew: bool = False) -> str:
        """An app access token, requested from Twitch when there is none, it is about to expire, or `renew`.

        The client id and secret travel in the body of the token request, as Twitch documents, so they
        never appear in a URL.
        """
        if self._token and not renew and self._clock() < self._token_expires_at:
            return self._token
        response = self._send("POST", TOKEN_URL, data={
            "client_id": self._client_id, "client_secret": self._client_secret, "grant_type": "client_credentials",
        })
        if response.status_code != 200:
            detail = self._redact(self._message(response))
            raise self.error(f"Twitch refused the {self.service} credentials (HTTP {response.status_code}"
                             + (f": {detail}" if detail else "") + "). Check IGDB_CLIENT_ID and IGDB_CLIENT_SECRET.")
        try:
            body = response.json()
            token, expires_in = body["access_token"], int(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError):
            raise self.error("Twitch answered the token request without an access token.") from None
        self._token = token
        self._token_expires_at = self._clock() + max(0, expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        return token


class TwitchClient(TwitchAppClient):
    """The two Helix endpoints behind "Watch it live". Used by the web app, so it fails fast:
    one retry for a server error, short timeouts, and no retry at all for a rate limit."""

    service = "Twitch"
    min_interval_seconds = 0.1
    max_attempts = 2
    backoff_seconds = 1.0
    retryable_status = frozenset({500, 502, 503, 504})
    timeout = httpx.Timeout(8.0, connect=5.0)

    def _get(self, path: str, params: list[tuple[str, str]]) -> list[dict]:
        url = f"{HELIX_BASE}/{path}"

        def send(renew: bool) -> httpx.Response:
            headers = {"Client-Id": self._client_id, "Authorization": f"Bearer {self._access_token(renew=renew)}"}
            return self._send("GET", url, params=params, headers=headers)

        response = send(False)
        if response.status_code == 401:  # the token expired or was revoked early: renew it once
            response = send(True)
        if response.status_code == 429:
            reset = response.headers.get("Ratelimit-Reset", "")
            reset_at = datetime.fromtimestamp(int(reset), tz=timezone.utc) if reset.isdigit() else None
            raise TwitchRateLimited("Twitch rate limit reached (HTTP 429).", reset_at)
        if response.status_code != 200:
            detail = self._redact(self._message(response))
            raise TwitchError(f"Twitch {path} failed (HTTP {response.status_code}" + (f": {detail}" if detail else "") + ").")
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError):
            raise TwitchError(f"Twitch {path} answered with an unexpected shape.") from None
        if not isinstance(data, list):
            raise TwitchError(f"Twitch {path} answered with an unexpected shape.")
        return [item for item in data if isinstance(item, dict)]

    def categories_for_igdb_id(self, igdb_id: int) -> list[dict]:
        """Get Games by IGDB id: every Twitch category Twitch links to this IGDB game (normally one or none)."""
        return self._get("games", [("igdb_id", str(int(igdb_id)))])

    def live_streams(self, category_id: str, first: int = STREAMS_SHOWN) -> list[dict]:
        """Get Streams: up to `first` live streams in the category, most viewers first."""
        return self._get("streams", [("game_id", category_id), ("type", "live"), ("first", str(int(first)))])

    def top_games(self, first: int = TOP_GAMES_PAGE) -> list[dict]:
        """Get Top Games: up to `first` categories with the most viewers now, most first (no viewer numbers)."""
        return self._get("games/top", [("first", str(int(first)))])


# ---------------------------------------------------------------------- matching and display (pure)


@dataclass(frozen=True)
class CategoryMatch:
    """The outcome of matching one game to a Twitch category by its IGDB id."""

    status: str  # matched | none | ambiguous
    category_id: str | None = None
    name: str | None = None


def match_category(igdb_id: int, categories: list[dict]) -> CategoryMatch:
    """The Twitch category whose igdb_id is exactly this game's IGDB id.

    Twitch's own `igdb_id` field must equal the game's id: a category that came back for another reason
    is ignored. Two or more categories with the same IGDB id are ambiguous, and none is chosen.
    """
    found = [item for item in categories
             if str(item.get("igdb_id") or "") == str(igdb_id) and item.get("id") and item.get("name")]
    if not found:
        return CategoryMatch("none")
    if len({item["id"] for item in found}) > 1:
        return CategoryMatch("ambiguous")
    return CategoryMatch("matched", str(found[0]["id"]), str(found[0]["name"]))


@dataclass(frozen=True)
class CategoryGame:
    """What a Twitch top category stands for (resolve_category), and on whose word."""

    status: str  # matched (one game) | series (a reviewed series category) | no_game | unresolved
    igdb_id: int | None = None  # matched only
    source: str | None = None  # matched: reviewer | twitch+igdb | twitch | igdb; series or unresolved: reviewer


def resolve_category(twitch_igdb_id: int | None, igdb_games: list[int], decision=None) -> CategoryGame:
    """What a Twitch category stands for, from identifiers only, never from its name.

    `twitch_igdb_id` is the igdb_id Twitch gave the category; `igdb_games` are the IGDB games whose IGDB record lists
    the category (external_games, source Twitch); `decision` is a reviewer's curated.TwitchCategoryDecision, if any.
    A reviewer's decision comes first: `match` names the game, `series` marks a category for a whole game series
    (Counter-Strike: never matched to one of its games, even the one IGDB's record names), `no_match` keeps the
    category unmatched. Otherwise every identifier must name one and the same game: Dota 2 is matched by IGDB's record
    alone, while a category that Twitch and IGDB link to different games stays unresolved, so none of them is guessed.
    """
    if decision is not None:
        if decision.decision == "match":
            return CategoryGame("matched", decision.igdb_id, "reviewer")
        if decision.decision == "series":
            return CategoryGame("series", source="reviewer")
        return CategoryGame("unresolved", source="reviewer")
    named = set(igdb_games) | ({twitch_igdb_id} if twitch_igdb_id else set())
    if not named:
        return CategoryGame("no_game")
    if len(named) > 1:
        return CategoryGame("unresolved")
    if twitch_igdb_id and igdb_games:
        source = "twitch+igdb"
    else:
        source = "twitch" if twitch_igdb_id else "igdb"
    return CategoryGame("matched", next(iter(named)), source)


@dataclass(frozen=True)
class Stream:
    """One live stream as shown in "Watch it live"."""

    user_name: str
    user_login: str
    title: str
    viewers: int
    language: str
    started_at: str

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/{self.user_login}"


def parse_streams(items: list[dict]) -> list[Stream]:
    """The live streams Twitch returned, skipping any entry that lacks what the list needs."""
    streams = []
    for item in items:
        login = str(item.get("user_login") or "")
        viewers = item.get("viewer_count")
        if item.get("type") != "live" or not LOGIN.fullmatch(login) or not isinstance(viewers, int) or viewers < 0:
            continue
        streams.append(Stream(
            user_name=str(item.get("user_name") or login),
            user_login=login,
            title=" ".join(str(item.get("title") or "").split()),
            viewers=viewers,
            language=str(item.get("language") or ""),
            started_at=str(item.get("started_at") or ""),
        ))
    return streams


def category_url(name: str) -> str:
    """The category's directory page on Twitch. Checked 2026-11-19: /directory/game/<name> opens the category."""
    return "https://www.twitch.tv/directory/game/" + quote(name, safe="")


@dataclass(frozen=True)
class TopCategory:
    """One entry of Twitch's Get Top Games answer, in Twitch's order."""

    position: int  # place in Twitch's order of every category it returned, 1 = most viewers
    category_id: str
    name: str
    igdb_id: int | None  # the IGDB game Twitch links to the category; None for Just Chatting, Art and the like


def parse_top_games(items: list[dict]) -> list[TopCategory]:
    """Twitch's top categories in Twitch's order, skipping entries without an id or a name, and repeats.

    A position is the entry's place in Twitch's answer, so a skipped entry still takes its place. The IGDB
    id is kept only when it is a positive whole number: Twitch sends "" when it has none.
    """
    categories, seen = [], set()
    for position, item in enumerate(items, 1):
        category_id = str(item.get("id") or "").strip()
        name = " ".join(str(item.get("name") or "").split())
        if not category_id.isdigit() or not name or category_id in seen:
            continue
        seen.add(category_id)
        igdb_text = str(item.get("igdb_id") or "").strip()
        igdb_id = int(igdb_text) if igdb_text.isdigit() and int(igdb_text) > 0 else None
        categories.append(TopCategory(position, category_id, name, igdb_id))
    return categories


# ---------------------------------------------------------------------- cache rules (pure)

FRESH_FOR = timedelta(minutes=5)  # a newer check is not repeated
RETRY_AFTER_FAILURE = timedelta(minutes=2)  # a failed check is not repeated sooner than this
KEEP_FOR = timedelta(hours=24)  # Twitch's terms: cache Twitch data for at most 24 hours


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def refresh_due(row, now: datetime) -> bool:
    """Whether the stored live state of a game is old enough to ask Twitch again."""
    if row is None:
        return True
    checked = _parse(row["checked_at"])
    if checked is None:
        return True
    if row["status"] in ("error", "rate_limited"):
        retry_at = _parse(row["retry_at"]) or checked + RETRY_AFTER_FAILURE
        return now >= retry_at
    return now - checked >= FRESH_FOR


STALE_AFTER = timedelta(minutes=15)  # streams older than this are labeled "may have ended"
SHOW_PREVIOUS_FOR = timedelta(hours=1)  # after a failed check, show the last answer only if it is this recent


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def refresh_live(conn, client: TwitchClient, game_key: str, igdb_id: int, now: datetime) -> None:
    """Ask Twitch for the game's category and live streams, and store the answer or the failure.

    Answers older than 24 hours are deleted first (Twitch's caching limit). A failure keeps the last
    good answer next to it and sets when to try again (Twitch's reset time after a rate limit).
    """
    store.purge_twitch_cache(conn, _iso(now - KEEP_FOR))
    moment = _iso(now)
    try:
        match = match_category(igdb_id, client.categories_for_igdb_id(igdb_id))
        if match.status != "matched":
            store.save_twitch_live(conn, game_key, status="no_category" if match.status == "none" else "ambiguous",
                                   checked_at=moment, fetched_at=moment)
            return
        streams = parse_streams(client.live_streams(match.category_id))
        store.save_twitch_live(conn, game_key, status="ok", category_id=match.category_id, category_name=match.name,
                               streams=[asdict(stream) for stream in streams], fetched_at=moment, checked_at=moment)
    except TwitchRateLimited as exc:
        retry_at = exc.reset_at if exc.reset_at and exc.reset_at > now else now + RETRY_AFTER_FAILURE
        store.record_twitch_problem(conn, game_key, status="rate_limited", checked_at=moment,
                                    retry_at=_iso(retry_at), error=str(exc))
    except TwitchError as exc:
        store.record_twitch_problem(conn, game_key, status="error", checked_at=moment,
                                    retry_at=_iso(now + RETRY_AFTER_FAILURE), error=str(exc))


def live_view(row, now: datetime, *, configured: bool, igdb_id: int | None, upcoming: bool) -> dict:
    """What "Watch it live" shows, from the stored answer only (no network).

    `needs_check` tells the page to ask the server for a newer answer (app.js); the details panel
    itself never waits for Twitch.
    """
    view = {"state": "pending", "streams": [], "category": None, "category_url": None, "checked_at": None,
            "fetched_at": None, "stale": False, "previous": False, "retry_at": None, "upcoming": upcoming,
            "needs_check": False}
    if not configured:
        view["state"] = "not_configured"
        return view
    if igdb_id is None:
        view["state"] = "no_igdb"
        return view
    checked = _parse(row["checked_at"]) if row is not None else None
    kept = checked is not None and now - checked < KEEP_FOR  # an older answer is never shown (Twitch's 24-hour limit)
    view["needs_check"] = refresh_due(row if kept else None, now)
    if not kept:
        return view
    view["checked_at"] = checked
    fetched = _parse(row["fetched_at"])
    view["fetched_at"] = fetched
    streams = [Stream(**item) for item in json.loads(row["streams"] or "[]")]
    if row["category_name"]:
        view["category"] = row["category_name"]
        view["category_url"] = category_url(row["category_name"])
    if row["status"] in ("error", "rate_limited"):
        view["state"] = row["status"]
        view["retry_at"] = _parse(row["retry_at"])
        if fetched and now - fetched < SHOW_PREVIOUS_FOR and row["category_name"]:
            view["previous"] = True
            view["streams"] = streams
            view["stale"] = True
        return view
    if row["status"] == "ok":
        view["state"] = "ok" if streams else "empty"
    else:
        view["state"] = row["status"]  # no_category or ambiguous
    view["streams"] = streams
    view["stale"] = bool(fetched and now - fetched >= STALE_AFTER)
    return view
