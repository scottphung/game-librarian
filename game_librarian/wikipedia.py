"""Wikimedia client for "What the critics said": Wikidata identifiers and English Wikipedia wikitext.

Policies followed (read 2026-11-19):
  * User-Agent policy (foundation.wikimedia.org, "Wikimedia Foundation User-Agent Policy"): a client name
    and version, contact information, and the library. The contact comes from GAME_LIBRARIAN_CONTACT (an
    email address or website you choose); without it this client refuses to run. On 2026-11-19 the
    Action API answered this project's Python HTTP client with HTTP 403 ("Please respect our robot
    policy") until contact information was in the User-Agent.
  * Robot policy (wikitech.wikimedia.org/wiki/Robot_policy): one request at a time and below 5 per
    second for the Action API; gzip; honor Retry-After on 429; batch requests where supported.
    This client waits at least 1 s between requests, asks for 10 articles per request, sends
    maxlag=5, and retries 429, 503 and "maxlag" answers after Retry-After (at most 60 s).
  * Wikidata Query Service (query.wikidata.org): one SPARQL query finds the items of up to 100 games.

Only the article wikitext is read, never HTML, and only through the documented APIs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

import httpx

from . import __version__

ACTION_API = "https://en.wikipedia.org/w/api.php"
SPARQL_API = "https://query.wikidata.org/sparql"
MIN_INTERVAL_SECONDS = 1.0
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 5.0
MAX_RETRY_AFTER_SECONDS = 60.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
VALUES_PER_QUERY = 100
STEAM_PROPERTY = "P1733"  # Wikidata: Steam application ID
IGDB_PROPERTY = "P5794"  # Wikidata: IGDB game ID (the slug, such as "grand-theft-auto-vi")
ARTICLE_PREFIX = "https://en.wikipedia.org/wiki/"


class WikimediaError(Exception):
    """Wikipedia or Wikidata could not be reached, refused the request, or answered unexpectedly."""


@dataclass(frozen=True)
class Article:
    """One article as the Action API returned it, after redirects."""

    requested: str
    title: str
    section: str | None  # the section a redirect points to, if any
    revision_id: int
    revision_at: str
    wikitext: str


def user_agent(contact: str) -> str:
    """The User-Agent the policy asks for: client/version (contact) library/version."""
    return f"GameLibrarian/{__version__} ({contact}; personal non-commercial game discovery) httpx/{httpx.__version__}"


def title_from_url(url: str) -> str | None:
    """The article title in an English Wikipedia article URL from Wikidata."""
    return unquote(url[len(ARTICLE_PREFIX):]).replace("_", " ") if url.startswith(ARTICLE_PREFIX) else None


class WikimediaClient:
    """Paced, retried GET requests to the Action API and the Wikidata Query Service."""

    def __init__(self, contact: str | None, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        contact = (contact or "").strip()
        if not contact:
            raise WikimediaError("Wikimedia asks automated clients for contact information in the User-Agent. "
                                 "Set GAME_LIBRARIAN_CONTACT to an email address or website you choose.")
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._headers = {"User-Agent": user_agent(contact), "Accept-Encoding": "gzip"}
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

    def __repr__(self) -> str:
        return "WikimediaClient(contact hidden)"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WikimediaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            remaining = MIN_INTERVAL_SECONDS - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After", "")
        return min(float(value), MAX_RETRY_AFTER_SECONDS) if value.isdigit() else None

    def _get(self, url: str, params: dict[str, Any], accept: str = "application/json") -> Any:
        """GET JSON with pacing; retries timeouts, 429/5xx and maxlag answers, waiting Retry-After when given."""
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn()
            wait = BACKOFF_SECONDS * 2 ** (attempt - 1)
            try:
                response = self._http.get(url, params=params, headers={**self._headers, "Accept": accept})
            except httpx.TransportError as exc:
                problem = type(exc).__name__
            else:
                if response.status_code in RETRYABLE_STATUS:
                    problem = f"HTTP {response.status_code}"
                    wait = self._retry_after(response) or wait
                elif response.status_code != 200:
                    raise WikimediaError(f"{url} answered HTTP {response.status_code}.")
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        raise WikimediaError(f"{url} answered with something other than JSON.") from None
                    error = body.get("error") if isinstance(body, dict) else None
                    if not error:
                        return body
                    if error.get("code") != "maxlag":
                        raise WikimediaError(f"{url} answered with the error {error.get('code')!r}.")
                    problem = "the servers are lagging (maxlag)"
                    wait = self._retry_after(response) or wait
            if attempt < MAX_ATTEMPTS:
                self._sleep(wait)
        raise WikimediaError(f"{url} failed after {MAX_ATTEMPTS} attempts ({problem}).")

    def wikidata_items(self, steam_appids: list[int], igdb_slugs: list[str]) -> dict[tuple[str, str], dict[str, str | None]]:
        """The Wikidata items listing these Steam app ids or IGDB slugs, with their English article titles.

        Returns {("steam", "271590") or ("igdb", "slug"): {item id: article title or None}}.
        Values are checked before they go into the query: digits for app ids, IGDB's slug characters.
        """
        values = [("steam", str(int(appid))) for appid in sorted(set(steam_appids))]
        values += [("igdb", slug) for slug in sorted(set(igdb_slugs)) if slug and _is_slug(slug)]
        found: dict[tuple[str, str], dict[str, str | None]] = {}
        for start in range(0, len(values), VALUES_PER_QUERY):
            chunk = values[start:start + VALUES_PER_QUERY]
            steam = " ".join(f'"{value}"' for kind, value in chunk if kind == "steam")
            igdb = " ".join(f'"{value}"' for kind, value in chunk if kind == "igdb")
            parts = []
            if steam:
                parts.append(f'{{ VALUES ?value {{ {steam} }} ?item wdt:{STEAM_PROPERTY} ?value . BIND("steam" AS ?kind) }}')
            if igdb:
                parts.append(f'{{ VALUES ?value {{ {igdb} }} ?item wdt:{IGDB_PROPERTY} ?value . BIND("igdb" AS ?kind) }}')
            query = ("SELECT ?item ?value ?kind ?article WHERE { " + " UNION ".join(parts) +
                     " OPTIONAL { ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> . } }")
            body = self._get(SPARQL_API, {"query": query, "format": "json"}, accept="application/sparql-results+json")
            try:
                bindings = body["results"]["bindings"]
            except (KeyError, TypeError):
                raise WikimediaError("The Wikidata Query Service answered with an unexpected shape.") from None
            for row in bindings:
                try:
                    key = (row["kind"]["value"], row["value"]["value"])
                    item = row["item"]["value"].rsplit("/", 1)[1]
                except (KeyError, TypeError, IndexError):
                    continue
                article = title_from_url(row.get("article", {}).get("value", ""))
                items = found.setdefault(key, {})
                items[item] = items.get(item) or article
        return found

    def articles(self, titles: list[str]) -> dict[str, Article | None]:
        """The current wikitext of up to 10 articles, following redirects; None for a missing article."""
        if not titles:
            return {}
        body = self._get(ACTION_API, {
            "action": "query", "prop": "revisions", "rvprop": "content|ids|timestamp", "rvslots": "main",
            "redirects": "1", "titles": "|".join(titles), "format": "json", "formatversion": "2", "maxlag": "5",
        })
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, dict):
            raise WikimediaError("The Wikipedia API answered with an unexpected shape.")
        normalized = {item["from"]: item["to"] for item in query.get("normalized", []) if "from" in item}
        redirects = {item["from"]: (item["to"], item.get("tofragment")) for item in query.get("redirects", [])
                     if "from" in item}
        pages = {page.get("title"): page for page in query.get("pages", []) if isinstance(page, dict)}
        result: dict[str, Article | None] = {}
        for requested in titles:
            title = normalized.get(requested, requested)
            section = None
            if title in redirects:
                title, section = redirects[title]
            page = pages.get(title)
            revisions = page.get("revisions") if page else None
            if not page or page.get("missing") or not revisions:
                result[requested] = None
                continue
            revision = revisions[0]
            content = revision.get("slots", {}).get("main", {}).get("content")
            if not isinstance(content, str):
                result[requested] = None
                continue
            result[requested] = Article(requested, title, section or None, int(revision.get("revid", 0)),
                                        str(revision.get("timestamp", "")), content)
        return result


def _is_slug(value: str) -> bool:
    """IGDB slugs are lowercase letters, digits and hyphens ("fable--1")."""
    return bool(value) and len(value) <= 120 and all(char.isascii() and (char.isalnum() or char == "-") for char in value) \
        and value == value.lower()
