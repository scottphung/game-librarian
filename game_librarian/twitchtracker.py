"""Most watched on Twitch by TwitchTracker's average viewers and rank, from its free API, for local evaluation only.

TwitchTracker (https://twitchtracker.com/) is an independent site; its footer says it "is not affiliated with Twitch or
Amazon". Its API page (https://twitchtracker.com/api, read 2026-11-21) lists two addresses and nothing else, one of them
"Category summary for 30 days: https://twitchtracker.com/api/games/summary/[twitch_id or full_name]". It documents no
fields, key, rate limit, period field or terms.

Checked live on 2026-11-21 (12 requests, 3 seconds apart, no key, Game Librarian's User-Agent):
  * An answer is one JSON object of four whole numbers, avg_viewers, avg_channels, rank and hours_watched, with no
    dates, no period and no category name. Grand Theft Auto VI had rank 1, Just Chatting 2, World of Warcraft 3 and
    League of Legends 4: the ranks count every Twitch category, games or not, so the games' ranks have gaps.
  * An unknown id or name, and a real category without a summary (The Elder Scrolls VI, 506437), got HTTP 200 with {}.
    A category with hardly any viewers got avg_viewers 0, avg_channels 1 and rank 22,485.
  * No rate-limit headers; Cache-Control: no-cache, private; served through Cloudflare.
  * The period is not confirmed. The API page says 30 days, but in all five full answers hours_watched / avg_viewers
    was 168.17 hours (1,009/6: 1,009 ten-minute samples, about 7 days), not 720 (30 days). The answer states no period,
    so none is stored, and the site calls the period unconfirmed.
  * A live refresh of 77 game categories the same afternoon: the ratio was never above 168.2 hours (lower for a game
    streamed only part of that time, and for tiny rounded averages), but the rank did not follow the same answers'
    average viewers or hours watched: 20 of 76 neighbouring ranks were out of viewer order (Fortnite #6 with 53,317,
    ROB RUN #7 with 60,497 average viewers). What the rank is based on, and over which period, is not stated.

Terms (read 2026-11-21; not legal advice): TwitchTracker publishes no terms of service or privacy policy (its /terms
address shows the page of a Twitch channel named "terms"). Its footer says "Scraping is prohibited. Use this basic
API." Its robots.txt disallows /api/* for every user agent. Nothing it publishes allows or forbids storing or showing
its figures. So this module uses the API only, never the website's pages; sends at most REQUESTS_PER_DAY requests in
any 24 hours, MIN_INTERVAL_SECONDS apart; asks about a category again only after REFRESH_AFTER; stops at the first
HTTP 403 or 429 and never works around one; and the site shows the figures only to a browser on the computer that runs
it (web.is_local_request), naming and linking TwitchTracker. The Power BI export leaves them out.

Which categories (update_selection): the game and series categories of Twitch's newest Get Top Games list (its first
100 categories, upcoming.twitch_games), plus every game and series category reviewed in curated/twitch_categories.csv,
listed by Twitch at that moment or not. The list (ranking) therefore covers only the categories Game Librarian
tracks, never every game TwitchTracker ranks: a game that ranks high over TwitchTracker's longer period but was outside
Twitch's top 100 at the refresh, and is not reviewed, is missing. No API endpoint for TwitchTracker's complete ranking
is documented. The figures stay until a newer answer replaces them, with their own time; no history is kept, so no
change between two answers is ever shown.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from . import store, twitch, upcoming
from .config import USER_AGENT

SUMMARY_URL = "https://twitchtracker.com/api/games/summary/{category_id}"
API_DOCS_URL = "https://twitchtracker.com/api"
SOURCE = "most_watched"  # the source_runs name of a refresh
SOURCE_NAME = "TwitchTracker"
REFRESH_AFTER = timedelta(hours=20)  # a category answered more recently is not asked again (without --force)
REQUESTS_PER_DAY = 200  # what this site allows itself in any 24 hours; TwitchTracker documents no limit
MIN_INTERVAL_SECONDS = 1.5
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2.0  # waits 2 s, then 4 s, before trying a request again
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})  # 403 and 429 are never retried: the run stops
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


class TwitchTrackerError(Exception):
    """A request failed in a way that stops the run: TwitchTracker refused it (HTTP 403), limited it (HTTP 429), gave
    another unexpected status, could not be reached, or answered with something other than JSON."""


class InvalidSummary(Exception):
    """TwitchTracker answered one category with JSON that lacks a valid rank or average viewers: only that category
    is skipped, and its previous figures stay."""


@dataclass(frozen=True)
class Summary:
    """TwitchTracker's summary of one Twitch category, as returned. The answer states no period."""

    rank: int  # among every Twitch category TwitchTracker ranks, games or not; 1 = most watched
    avg_viewers: int
    avg_channels: int | None  # None when the answer lacks a valid value
    hours_watched: int | None


def _count(value: object) -> int | None:
    """A whole number of 0 or more from JSON, else None: a boolean, a fraction or a text is not a count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_summary(body: object) -> Summary | None:
    """TwitchTracker's summary from an answer: None for {} (it has no summary of the category), or InvalidSummary
    when a rank of 1 or more or the average viewers is missing or malformed. The other two fields are optional."""
    if not isinstance(body, dict):
        raise InvalidSummary("an answer that is not a JSON object")
    if not body:
        return None
    rank, viewers = _count(body.get("rank")), _count(body.get("avg_viewers"))
    missing = [name for name, value in (("rank", rank or None), ("avg_viewers", viewers)) if value is None]
    if missing:
        raise InvalidSummary("no valid " + " or ".join(missing))
    return Summary(rank, viewers, _count(body.get("avg_channels")), _count(body.get("hours_watched")))


class TwitchTrackerClient:
    """Paced GET requests to TwitchTracker's category summary endpoint. No key or credential is ever sent.

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

    def __enter__(self) -> TwitchTrackerClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _wait_for_turn(self) -> None:
        if self._last_request_at is not None:
            remaining = MIN_INTERVAL_SECONDS - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()

    def summary(self, category_id: str) -> Summary | None:
        """TwitchTracker's summary of one Twitch category (by its id), or None when it has none.

        Timeouts, failed connections and server errors are tried MAX_ATTEMPTS times. HTTP 403 and 429 raise
        TwitchTrackerError at once, as do other unexpected statuses and an answer that is not JSON (such as a
        challenge page); InvalidSummary means the JSON lacks what the list needs.
        """
        if not category_id.isdigit():
            raise ValueError(f"a Twitch category id is digits, got {category_id!r}")
        url = SUMMARY_URL.format(category_id=category_id)
        problem = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn()
            self.requests_sent += 1
            try:
                response = self._http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            except httpx.TransportError as exc:  # timeouts, DNS failures, refused connections
                problem = type(exc).__name__
            else:
                if response.status_code == 429:
                    raise TwitchTrackerError("TwitchTracker answered HTTP 429 (too many requests).")
                if response.status_code == 403:
                    raise TwitchTrackerError("TwitchTracker refused the request (HTTP 403); it is not retried or "
                                             "worked around.")
                if response.status_code in RETRYABLE_STATUS:
                    problem = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise TwitchTrackerError(f"TwitchTracker answered HTTP {response.status_code}.")
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        raise TwitchTrackerError("TwitchTracker answered with something other than JSON.") from None
                    return parse_summary(body)
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
        raise TwitchTrackerError(f"TwitchTracker could not be reached after {MAX_ATTEMPTS} attempts ({problem}).")


# ---------------------------------------------------------------------- which categories (the database)


@dataclass(frozen=True)
class Category:
    """A Twitch category that TwitchTracker is asked about: a game or series category of Twitch's top list, or a
    reviewed one (curated/twitch_categories.csv) outside it."""

    category_id: str
    name: str  # Twitch's category name (a reviewed title outside Twitch's list), for the Watch on Twitch link
    igdb_id: int | None  # the game it stands for (twitch.resolve_category); None for a series category
    match_source: str  # twitch.CategoryGame.source
    selected_at: str | None  # when Twitch gave the list it was chosen from, UTC ISO 8601; None outside that list


def update_selection(conn: sqlite3.Connection, decisions: dict, now: datetime) -> dict:
    """Choose the categories to ask about: the game and series categories of Twitch's newest top list when one is less
    than 24 hours old (upcoming.twitch_games, with the reviewer `decisions`), and every reviewed game or series
    category of `decisions` outside that list.

    Without such a list, the categories chosen from the last one stay, except one a reviewer has since ruled out
    (no_match). A stored category no longer chosen is dropped with its figures; the others keep theirs. Returns
    {"categories": how many are chosen, "reviewed": how many of them only a reviewed row chose, "left_out": the
    categories not chosen, by reason, "selected_at": the time of Twitch's list, or None without one}.
    """
    top = store.twitch_top_games(conn, _iso(now - twitch.KEEP_FOR))
    if top:
        listed, left_out = upcoming.twitch_games(conn, top, decisions)
        categories = [Category(row["category_id"], row["name"], record["igdb_id"] if record else None, match.source,
                               row["checked_at"]) for row, record, match in listed]
        seen = {row["category_id"] for row in top}  # a category of the list is decided there, chosen or not
    else:
        left_out = Counter()
        categories = [Category(row["category_id"], row["category_name"], row["igdb_id"], row["match_source"],
                               row["selected_at"])
                      for row in store.tracker_rows(conn) if row["selected_at"] and not _ruled_out(row, decisions)]
        seen = {category.category_id for category in categories}
    reviewed, reviewed_left_out = upcoming.twitch_games(conn, _reviewed_rows(decisions, seen), decisions)
    categories += [Category(row["category_id"], row["name"], record["igdb_id"] if record else None, match.source, None)
                   for row, record, match in reviewed]
    store.replace_tracker_selection(conn, categories)
    return {"categories": len(categories), "reviewed": len(reviewed), "left_out": left_out + reviewed_left_out,
            "selected_at": top[0]["checked_at"] if top else None}


def _ruled_out(row, decisions: dict) -> bool:
    """Whether a reviewer decided since that a stored category is not a game (no_match), so it is not asked again."""
    decision = decisions.get(row["category_id"])
    return decision is not None and decision.decision == "no_match"


def _reviewed_rows(decisions: dict, seen: set[str]) -> list[dict]:
    """The reviewed game and series categories (`decisions`) not in `seen`, shaped like twitch_top_games rows for
    upcoming.twitch_games: Twitch's list gave them no igdb_id or IGDB record, so the reviewer's decision alone says
    what each stands for, and the reviewer's title (Twitch's category name) is its name."""
    return [{"category_id": decision.category_id, "name": decision.title, "igdb_id": None, "igdb_links": None}
            for category_id, decision in sorted(decisions.items())
            if decision.decision in ("match", "series") and category_id not in seen]


def due_categories(conn: sqlite3.Connection, now: datetime, *, force: bool = False) -> list[sqlite3.Row]:
    """The chosen categories to ask about: never answered, or answered REFRESH_AFTER ago or more (every one with
    `force`). Never-answered ones come first, then the oldest answers, so a run that stops early is continued by the
    next. A failed check is not an answer, so its category stays due."""
    due = []
    for row in store.tracker_rows(conn):
        answered = datetime.fromisoformat(row["retrieved_at"]) if row["retrieved_at"] else None
        if force or answered is None or now - answered >= REFRESH_AFTER:
            due.append(row)
    return sorted(due, key=lambda row: (row["retrieved_at"] is not None, row["retrieved_at"] or "", row["category_id"]))


def requests_left(conn: sqlite3.Connection, now: datetime) -> int:
    """REQUESTS_PER_DAY minus the requests this site's refreshes sent in the 24 hours before `now`."""
    used = store.source_requests_since(conn, SOURCE, _iso(now - timedelta(days=1)))
    return max(0, REQUESTS_PER_DAY - used)


# ---------------------------------------------------------------------- the list (the database, no network)


@dataclass(frozen=True)
class Ranked:
    """One entry of the Most watched list (ranking): a chosen category with TwitchTracker figures."""

    row: sqlite3.Row  # its twitchtracker_summaries row: TwitchTracker's rank as returned, the figures and their time
    record: sqlite3.Row | None  # the IGDB record of its game; None for a series category
    key: str | None  # the game's key on this site (upcoming.game_key_for); None for a series category
    name: str  # the game's IGDB name, or the series' reviewed title


# Why a chosen category without figures is not listed, by its newest status; any other status is a failed check.
NO_FIGURES = {"no_summary": "no_summary", "unchecked": "not_asked"}


def ranking(conn: sqlite3.Connection, decisions: dict) -> tuple[list[Ranked], Counter]:
    """The chosen categories that the Most watched list shows, best TwitchTracker rank first, and the others counted
    by reason. The Trending page, the dashboard and the `most-watched` command all list these entries.

    A reviewer's decision in `decisions` applies at once (twitch.resolve_category, with the game each category was
    chosen for). A category is listed when it is a game Trending lists or a reviewed series, and TwitchTracker's
    newest answer about it gave figures (a failed check keeps the previous ones). Equal ranks go to more average
    viewers. A game reached by two categories is listed once, at its better rank. Left out, by reason: "unresolved"
    (a reviewer decided since that it is not a game), "not_read" (its game's IGDB record is missing), "other_kind",
    "repeated" (another category already stands for its game), and for a category without figures, "no_summary"
    (TwitchTracker answered {}), "not_asked" (not asked yet) or "failed" (its check failed and it has no earlier
    figures). A missing figure is never counted as zero.
    """
    rows = store.tracker_rows(conn)
    matches = {row["category_id"]: twitch.resolve_category(row["igdb_id"], [], decisions.get(row["category_id"]))
               for row in rows}
    records = store.igdb_game_rows(conn, sorted({match.igdb_id for match in matches.values() if match.igdb_id}))
    visible = {row["appid"] for row in store.matching_games(conn, [], [])}
    linked = store.catalog_links_by_igdb(conn, visible)
    entries, left_out, seen = [], Counter(), set()
    for row in sorted(rows, key=lambda row: (row["rank"] is None, row["rank"] or 0, -(row["avg_viewers"] or 0),
                                             row["category_id"])):
        match = matches[row["category_id"]]
        record = records.get(match.igdb_id) if match.status == "matched" else None
        if match.status not in ("matched", "series"):
            left_out["unresolved"] += 1
        elif match.status == "matched" and record is None:
            left_out["not_read"] += 1
        elif match.status == "matched" and not upcoming.twitch_listable(record):
            left_out["other_kind"] += 1
        elif row["rank"] is None:
            left_out[NO_FIGURES.get(row["status"], "failed")] += 1
        else:
            key = upcoming.game_key_for(record, visible, linked) if record else None
            if key in seen:
                left_out["repeated"] += 1
                continue
            if key:
                seen.add(key)
            name = record["name"] if record else decisions[row["category_id"]].title
            entries.append(Ranked(row, record, key, name))
    return entries, left_out


# ---------------------------------------------------------------------- refresh (network, then the database)


@dataclass
class RefreshReport:
    """What one refresh did: counts, the first errors, and why it stopped early, if it did."""

    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    stopped_early: str | None = None

    def status(self) -> str:
        """success: every due category was answered; failed: none was; partial: some were."""
        if not self.errors and not self.stopped_early:
            return "success"
        return "partial" if self.counts["answered"] else "failed"

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "stopped_early": self.stopped_early, "errors": self.errors[:25]}


def refresh_summaries(conn: sqlite3.Connection, client: TwitchTrackerClient, due: list, *,
                      log: Callable[[str], None] = print, now: Callable[[], datetime] = utc_now) -> RefreshReport:
    """Ask TwitchTracker about each `due` category (rows of store.tracker_rows), one request each; store each answer.

    Every answer of the run gets the run's start time. {} clears a category's figures (TwitchTracker has none now); an
    invalid answer keeps its previous figures and the run goes on; any other failure (HTTP 403 or 429, unreachable, not
    JSON) stops the run, and the categories not asked keep their figures and stay due. The run stops too when the
    24-hour budget is used. It is recorded in source_runs with its request count.
    """
    started = now()
    allowed = requests_left(conn, started)
    run_id = store.start_source_run(conn, SOURCE, now=_iso(started))
    report = RefreshReport()
    sent_before = client.requests_sent
    try:
        _ask(conn, client, due, allowed, report, log, started)
    except BaseException as exc:  # also Ctrl+C: the run and its requests are still recorded
        report.counts["requests"] = client.requests_sent - sent_before
        report.errors.append(f"refresh stopped: {exc!r}")
        store.finish_source_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    report.counts["requests"] = client.requests_sent - sent_before
    store.finish_source_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report


def _ask(conn: sqlite3.Connection, client: TwitchTrackerClient, due: list, allowed: int, report: RefreshReport,
         log: Callable[[str], None], started: datetime) -> None:
    moment = _iso(started)
    sent_before = client.requests_sent
    for done, row in enumerate(due):
        waiting = len(due) - done
        if client.requests_sent - sent_before >= allowed:
            report.stopped_early = (f"Stopped: this site sends TwitchTracker at most {REQUESTS_PER_DAY} requests in 24 "
                                    f"hours; {waiting} {'category waits' if waiting == 1 else 'categories wait'} for a "
                                    "later run.")
            report.counts["not_asked"] += waiting
            break
        name = f"{row['category_name']} ({row['category_id']})"
        try:
            summary = client.summary(row["category_id"])
        except InvalidSummary as exc:
            store.record_tracker_problem(conn, row["category_id"], status="invalid", checked_at=moment,
                                         error=f"TwitchTracker's answer had {exc}")
            report.counts["invalid"] += 1
            report.errors.append(f"{name}: TwitchTracker's answer had {exc}; its previous figures are kept.")
            continue
        except TwitchTrackerError as exc:
            store.record_tracker_problem(conn, row["category_id"], status="error", checked_at=moment, error=str(exc))
            report.errors.append(f"{name}: {exc}")
            report.stopped_early = f"{exc} The run stopped; the stored figures are kept."
            report.counts["not_asked"] += waiting - 1
            break
        store.save_tracker_answer(conn, row["category_id"], summary, retrieved_at=moment)
        report.counts["answered"] += 1
        report.counts["summaries" if summary else "no_summary"] += 1
        if (done + 1) % 10 == 0 or done + 1 == len(due):
            log(f"[most watched {done + 1}/{len(due)}] {report.counts['summaries']} with TwitchTracker figures, "
                f"{report.counts['no_summary']} without")
