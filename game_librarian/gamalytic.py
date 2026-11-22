"""Estimated Steam copies sold, from Gamalytic's free API.

Endpoint (https://api.gamalytic.com/reference/, read 2026-11-20): GET /steam-games/list returns "a list of up to
1000 games with some basic information", filtered by `appids` (comma-separated Steam app ids) and trimmed by
`fields`. No key is needed: "Free users are limited to 250 requests per day and do not have access to all data and
features"; this endpoint also allows 240 requests per minute when it is not sorted. Its copiesSold field is
"Estimated units sold. Estimated number of owners who purchased the game directly on Steam. This does not include
key-activated units." Only Gamalytic's default list is used: released games, early access included.

Checked live on 2026-11-20, without a key:
  * `appids`, `fields` and `limit` work. 100 catalog app ids in one request returned 99 games in half a second; the
    one left out, Grand Theft Auto V Legacy (271590), is no longer sold on Steam. An unknown app id is left out too,
    never returned with a zero.
  * `revenue` is left out of free answers, even when it is requested.
  * `steamId` is a number, although the docs say string. Free-to-play games get very large values with price 0
    (Counter-Strike 2: 367,242,903; Dota 2: 235,080,385).
  * Every answer carried `cacheTimestamp`, Unix milliseconds that the docs do not mention, with the same value on
    every answer that day (20 Nov 2026, 14:00:38 UTC). It is stored as the source's own time. No rate-limit headers.

How Gamalytic estimates (https://gamalytic.com/about, read 2026-11-20): Steam's top-seller rank, concurrent players,
a regression on release year, review score and playtime, and publicly disclosed sales. Gamalytic reports 77 % of
single-game estimates within 30 % of actual sales and 98 % within 50 %; smaller games and free-to-play games are
less accurate. A value is therefore an estimate of Steam purchases to date: not publisher-reported sales, not other
platforms or stores, not key activations, and not revenue.

Terms (https://gamalytic.com/tos, dated March 23, 2026, read 2026-11-20 and again, unchanged, 2026-11-22): the services
are licensed "solely for your internal business purpose"; "You shall not ... distribute, disclose, or make accessible to
any third party, or allow any third party to analyze or access the information provided"; "Resale or redistribution of
data provided by Gamalytic.com is strictly prohibited without explicit prior written approval." Nothing Gamalytic
publishes allows showing its data to the public. Since 0.21 the website shows the estimates to every visitor, by the
site owner's decision of 2026-11-22; the terms above still say what they say, and README.md ("Data sources and
terms") records it. The Power BI export leaves the estimates out.

Limits kept here: APPIDS_PER_REQUEST app ids per request (3 requests for 238 games), at most one request a second,
at most REQUESTS_PER_DAY requests in any 24 hours (counted from this site's own runs in source_runs), each game asked
again only after MAX_AGE, and an HTTP 429 answer stops the run at once.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import httpx

from . import store
from .config import USER_AGENT

LIST_URL = "https://api.gamalytic.com/steam-games/list"
API_DOCS_URL = "https://api.gamalytic.com/reference/"
ABOUT_URL = "https://gamalytic.com/about"
TERMS_URL = "https://gamalytic.com/tos"
SOURCE = "sales_estimates"  # the source_runs name of a collection
SOURCE_NAME = "Gamalytic"  # stored with every estimate and named with the chart
FIELDS = "steamId,copiesSold"
APPIDS_PER_REQUEST = 100  # a short request address; the endpoint returns up to 1,000 games per page
FREE_REQUESTS_PER_DAY = 250  # Gamalytic's free quota (API docs, 2026-11-20)
REQUESTS_PER_DAY = 50  # what this site allows itself in any 24 hours, well under the free quota
MAX_AGE = timedelta(days=7)  # a game is asked again after this long; earlier collections are kept
STALE_AFTER = timedelta(days=14)  # the dashboard says the estimates are old after this long
MIN_INTERVAL_SECONDS = 1.0
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0  # waits 2 s, then 4 s, before trying a request again
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})  # 429 is not retried: the run stops
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class GamalyticError(Exception):
    """Gamalytic could not be reached, refused the request, or answered with something unexpected."""


class GamalyticRateLimited(GamalyticError):
    """Gamalytic answered HTTP 429: its daily quota or a per-minute limit is used up for now."""


@dataclass(frozen=True)
class ListAnswer:
    """What one list request said about the requested games."""

    copies_sold: dict[int, int]  # the requested app ids with a usable estimate; the others got none
    cached_at: datetime | None  # the answer's cacheTimestamp, when it carries a valid one


def _whole_number(value: object) -> int | None:
    """An integer from JSON (a number, or a string of digits), else None. A boolean is not a number here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def parse_list(body: object, requested: Iterable[int]) -> ListAnswer:
    """The usable estimates in a /steam-games/list answer, for the requested app ids only.

    An entry counts when its steamId is a requested app id and its copiesSold is a whole number above zero; for an
    app id that appears twice, the first entry counts. A missing, zero, negative or malformed copiesSold means "no
    estimate", never zero copies. Raises GamalyticError when the answer has no result list, or when it is split
    into pages, because the games on the other pages would wrongly look as if they had no estimate.
    """
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise GamalyticError("Gamalytic answered with an unexpected shape (no result list).")
    pages = _whole_number(body.get("pages"))
    if pages is not None and pages > 1:
        raise GamalyticError(f"Gamalytic split the answer into {pages} pages, so this batch was not saved.")
    wanted = set(requested)
    estimates: dict[int, int] = {}
    for item in body["result"]:
        if not isinstance(item, dict):
            continue
        appid = _whole_number(item.get("steamId"))
        copies = _whole_number(item.get("copiesSold"))
        if appid in wanted and appid not in estimates and copies is not None and copies > 0:
            estimates[appid] = copies
    stamp = _whole_number(body.get("cacheTimestamp"))
    cached_at = None
    if stamp:
        try:
            cached_at = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):  # a value outside the dates a computer can represent
            cached_at = None
    return ListAnswer(estimates, cached_at)


class GamalyticClient:
    """Paced GET requests to Gamalytic's free list endpoint. No API key is ever sent.

    `sleep` and `clock` are injectable, so tests run instantly. `requests_sent` counts every HTTP request, retries
    included, for the 24-hour budget.
    """

    def __init__(self, http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._http = http or httpx.Client(timeout=TIMEOUT)
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None
        self.requests_sent = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GamalyticClient:
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
    def _message(response: httpx.Response) -> str:
        """The `message` or `error` text of an error answer, shortened; "" when it has none."""
        try:
            body = response.json()
        except ValueError:
            return ""
        text = (body.get("message") or body.get("error")) if isinstance(body, dict) else ""
        return " ".join(str(text or "").split())[:200]

    def copies_sold(self, appids: list[int]) -> ListAnswer:
        """Gamalytic's copiesSold for up to APPIDS_PER_REQUEST Steam apps, in one request.

        Timeouts, failed connections and server errors are tried MAX_ATTEMPTS times; HTTP 429 raises
        GamalyticRateLimited at once, and any other answer than 200 raises GamalyticError.
        """
        if not 0 < len(appids) <= APPIDS_PER_REQUEST:
            raise ValueError(f"ask for 1 to {APPIDS_PER_REQUEST} app ids at a time")
        params = {"appids": ",".join(str(int(appid)) for appid in appids), "fields": FIELDS,
                  "limit": str(len(appids))}
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn()
            self.requests_sent += 1
            try:
                response = self._http.get(LIST_URL, params=params, headers={"User-Agent": USER_AGENT})
            except httpx.TransportError as exc:  # timeouts, DNS failures, refused connections
                problem = type(exc).__name__
            else:
                if response.status_code == 429:
                    raise GamalyticRateLimited("Gamalytic answered HTTP 429 (too many requests).")
                if response.status_code in RETRYABLE_STATUS:
                    problem = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    detail = self._message(response)
                    raise GamalyticError(f"Gamalytic answered HTTP {response.status_code}"
                                         + (f": {detail}" if detail else "") + ".")
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        raise GamalyticError("Gamalytic answered with something other than JSON.") from None
                    return parse_list(body, appids)
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        raise GamalyticError(f"Gamalytic could not be reached after {MAX_ATTEMPTS} attempts ({problem}).")


# ---------------------------------------------------------------------- collection (network, then the database)


@dataclass
class RefreshReport:
    """What one collection did: counts, the first errors, and why it stopped early, if it did."""

    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    stopped_early: str | None = None

    def status(self) -> str:
        """success: every due game was asked; failed: none was; partial: some were."""
        if not self.errors and not self.stopped_early:
            return "success"
        return "partial" if self.counts["games_asked"] else "failed"

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "stopped_early": self.stopped_early, "errors": self.errors[:25]}


def due_appids(conn, appids: list[int], now: datetime, *, force: bool = False) -> list[int]:
    """The games to ask about: never asked, or last asked MAX_AGE ago or more (every game with `force`).

    Never-asked games come first, then the oldest collections, so a run that stops early is continued by the next.
    """
    latest = store.latest_sales_estimates(conn, appids)
    asked = {appid: datetime.fromisoformat(row["collected_at"]) for appid, row in latest.items()}
    due = [appid for appid in appids if force or appid not in asked or now - asked[appid] >= MAX_AGE]
    return sorted(due, key=lambda appid: (appid in asked, asked.get(appid, now), appid))


def requests_left(conn, now: datetime) -> int:
    """REQUESTS_PER_DAY minus the requests this site's collections sent in the 24 hours before `now`."""
    used = store.source_requests_since(conn, SOURCE, _iso(now - timedelta(days=1)))
    return max(0, REQUESTS_PER_DAY - used)


def refresh_estimates(conn, client: GamalyticClient, appids: list[int], *, log: Callable[[str], None] = print,
                      now: Callable[[], datetime] = utc_now) -> RefreshReport:
    """Ask Gamalytic about `appids`, APPIDS_PER_REQUEST per request, and save one row per game asked.

    Every row of the run has the run's start time. A game without an estimate is saved without one (and asked again
    after MAX_AGE), never as zero. The run stops at the first failed request, at an HTTP 429 answer, and when the
    24-hour budget is used; the games not asked keep their stored estimates and stay due. The run, with its request
    count, is recorded in source_runs.
    """
    started = now()
    allowed = requests_left(conn, started)
    run_id = store.start_source_run(conn, SOURCE, now=_iso(started))
    report = RefreshReport()
    sent_before = client.requests_sent
    try:
        _collect(conn, client, appids, allowed, report, log, started)
    except BaseException as exc:  # also Ctrl+C: the run and its requests are still recorded
        report.counts["requests"] = client.requests_sent - sent_before
        report.errors.append(f"collection stopped: {exc!r}")
        store.finish_source_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    report.counts["requests"] = client.requests_sent - sent_before
    store.finish_source_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report


def _collect(conn, client: GamalyticClient, appids: list[int], allowed: int, report: RefreshReport,
             log: Callable[[str], None], started: datetime) -> None:
    moment = _iso(started)
    sent_before = client.requests_sent
    for start in range(0, len(appids), APPIDS_PER_REQUEST):
        batch = appids[start:start + APPIDS_PER_REQUEST]
        waiting = len(appids) - start
        if client.requests_sent - sent_before >= allowed:
            report.stopped_early = (f"Stopped: this site sends Gamalytic at most {REQUESTS_PER_DAY} requests in 24 "
                                    f"hours (the free limit is {FREE_REQUESTS_PER_DAY} a day); {waiting} games wait "
                                    "for a later run.")
            report.counts["games_not_asked"] += waiting
            break
        try:
            answer = client.copies_sold(batch)
        except GamalyticError as exc:
            report.errors.append(str(exc))
            reason = ("Gamalytic answered 429 (too many requests), so the run stopped; try again later."
                      if isinstance(exc, GamalyticRateLimited) else "A Gamalytic request failed, so the run stopped.")
            report.stopped_early = f"{reason} The stored estimates are kept."
            report.counts["games_not_asked"] += waiting
            break
        cached_at = _iso(answer.cached_at) if answer.cached_at else None
        store.save_sales_estimates(conn, {appid: answer.copies_sold.get(appid) for appid in batch},
                                   source=SOURCE_NAME, collected_at=moment, source_cached_at=cached_at)
        report.counts["games_asked"] += len(batch)
        report.counts["estimates"] += len(answer.copies_sold)
        report.counts["no_estimate"] += len(batch) - len(answer.copies_sold)
        log(f"[sales estimates {start + len(batch)}/{len(appids)}] {len(answer.copies_sold)} of {len(batch)} games "
            "have a Gamalytic estimate")
