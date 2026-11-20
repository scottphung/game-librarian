"""The sync command: find changes on Steam and refresh the stored catalog.

It runs separately from the web app; page requests never call Steam. Every game
is saved in its own transaction, so when Steam is down or a request fails, the
last good data stays in place and the run is recorded as partial or failed.

Incremental rules:
  * catalog discovery (needs STEAM_API_KEY) asks only for apps modified since the
    previous successful discovery, page by page;
  * store details are refreshed when discovery reports a newer last_modified, or
    when they are older than DETAILS_MAX_AGE;
  * reviews are refreshed when they are older than REVIEWS_MAX_AGE; each refresh also
    saves a dated review snapshot;
  * prices are refreshed when they are older than PRICES_MAX_AGE or were read for
    another store region, 50 games per request;
  * player counts are read when the game's newest player snapshot is older than
    PLAYERS_MAX_AGE; each read is saved as a new dated snapshot.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import prepare, store
from .config import DEFAULT_STEAM_COUNTRY
from .curated import CuratedGame
from .steam import REVIEW_TYPE_ALL, REVIEW_TYPE_NEGATIVE, REVIEW_TYPE_POSITIVE, SteamClient, SteamError

DETAILS_MAX_AGE = timedelta(days=7)
REVIEWS_MAX_AGE = timedelta(hours=24)
# Prices and player counts change within a day. A sync run more often than this skips them,
# so running `sync` several times in a row does not repeat the same requests.
PRICES_MAX_AGE = timedelta(hours=3)
PLAYERS_MAX_AGE = timedelta(hours=3)
PLAYERS_PROGRESS_EVERY = 25  # log a progress line after this many player counts
STORE_ITEMS_BATCH_SIZE = 50
EXCERPTS_PER_SIDE = 3
REVIEWS_REQUESTED_PER_SIDE = 10  # a few spares, so near-empty reviews can be skipped
REVIEW_DAY_RANGE = 365  # most helpful reviews of the past year; Steam widens the window if too few exist
MAX_CONSECUTIVE_FAILURES = 3  # then Steam is treated as unavailable and the run stops early
MAX_ERRORS_KEPT = 25
DISCOVERY_STATE_KEY = "discovery_modified_since"


def utc_now() -> datetime:
    """Current time in UTC (injectable in tests)."""
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


@dataclass
class SyncOptions:
    """Choices for one sync run (mirrors the command-line flags)."""

    force: bool = False
    appids: list[int] | None = None
    limit: int | None = None
    skip_discovery: bool = False
    skip_reviews: bool = False
    skip_prices: bool = False
    skip_players: bool = False


@dataclass
class SyncReport:
    """Counts and errors collected during a run; saved as the run summary."""

    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    discovery: str = "not run"
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    stopped_early: str | None = None

    def succeed(self) -> None:
        self.successes += 1
        self.consecutive_failures = 0

    def fail(self, message: str) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append(message)
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.stopped_early:
            self.stopped_early = (
                f"Stopped after {MAX_CONSECUTIVE_FAILURES} failed Steam requests in a row; "
                "Steam looks unavailable. Stored data was left unchanged."
            )

    def status(self) -> str:
        """success: no failures; failed: nothing succeeded; partial: some of each."""
        if self.failures == 0 and not self.stopped_early:
            return "success"
        return "failed" if self.successes == 0 else "partial"

    def summary(self) -> dict:
        # An operation is one Steam call with its retries; refreshing one game's reviews counts as one (3 calls).
        return {
            "discovery": self.discovery,
            "counts": dict(self.counts),
            "successful_operations": self.successes,
            "failed_operations": self.failures,
            "stopped_early": self.stopped_early,
            "errors": self.errors,
        }


def _select_targets(curated_games: list[CuratedGame], options: SyncOptions) -> list[int]:
    """Curated app ids to refresh in this run, honoring --appid and --limit."""
    appids = [game.appid for game in curated_games]
    if options.appids:
        wanted = set(options.appids)
        appids = [appid for appid in appids if appid in wanted]
    if options.limit is not None:
        appids = appids[: options.limit]
    return appids


def _discover(conn: sqlite3.Connection, client: SteamClient, options: SyncOptions, report: SyncReport,
              log: Callable[[str], None], now: Callable[[], datetime]) -> None:
    """Page through apps changed since the last successful discovery (IStoreService/GetAppList)."""
    if options.skip_discovery:
        report.discovery = "skipped (--skip-discovery)"
        return
    if not client.api_key:
        report.discovery = "skipped: STEAM_API_KEY is not set; details are refreshed by age instead"
        log(f"Discovery {report.discovery}")
        return
    started = int(now().timestamp())
    since = int(store.get_state(conn, DISCOVERY_STATE_KEY) or 0)
    last_appid, pages = 0, 0
    try:
        while True:
            page = client.get_app_list_page(if_modified_since=since, last_appid=last_appid)
            report.succeed()
            apps = page.get("apps") or []
            new, changed = store.upsert_discovered_apps(conn, apps, now=_iso(now()))
            report.counts["discovered_new"] += new
            report.counts["discovered_changed"] += changed
            pages += 1
            if not page.get("have_more_results") or not apps:
                break
            last_appid = int(page.get("last_appid") or apps[-1]["appid"])
    except SteamError as exc:
        report.fail(f"discovery: {exc}")
        report.discovery = "failed; details are refreshed by age instead"
        log(f"Discovery failed: {exc}")
        return
    store.copy_discovered_last_modified(conn)
    store.set_state(conn, DISCOVERY_STATE_KEY, str(started))
    report.discovery = f"ok: {pages} page(s) of apps modified since {since or 'the beginning'}"
    log(f"Discovery {report.discovery}")


def _details_due(row: sqlite3.Row, now: datetime, force: bool) -> bool:
    if force or row["details_updated_at"] is None or row["details_status"] == "error":
        return True  # never fetched, or the last attempt failed: try again now
    if row["steam_last_modified"] and (row["details_last_modified"] or 0) < row["steam_last_modified"]:
        return True  # discovery saw a change on Steam since our last fetch
    return now - datetime.fromisoformat(row["details_updated_at"]) >= DETAILS_MAX_AGE


def _fetch_store_extras(client: SteamClient, appids: list[int], report: SyncReport,
                        log: Callable[[str], None]) -> dict[int, store.StoreExtras]:
    """Tags, covers, trailer clips and screenshots, fetched in batches. Apps missing here keep their stored values."""
    try:
        tag_names = client.get_tag_names()
        report.succeed()
    except SteamError as exc:
        report.fail(f"tag names: {exc}")
        log(f"Tag names unavailable; stored tags are kept: {exc}")
        return {}
    extras: dict[int, store.StoreExtras] = {}
    for start in range(0, len(appids), STORE_ITEMS_BATCH_SIZE):
        if report.stopped_early:
            break
        batch = appids[start : start + STORE_ITEMS_BATCH_SIZE]
        try:
            items = client.get_store_items(batch)
            report.succeed()
        except SteamError as exc:
            report.fail(f"tags and covers for {len(batch)} apps: {exc}")
            log(f"Tags and covers unavailable for {len(batch)} apps; stored values are kept: {exc}")
            continue
        for appid, item in items.items():
            extras[appid] = store.StoreExtras(
                tags=prepare.tags_from_store_item(item, tag_names),
                cover_url=prepare.cover_url_from_store_item(item),
                clip=prepare.preview_clip_from_store_item(item),
                screenshot_url=prepare.screenshot_from_store_item(item),
            )
    return extras


def _refresh_details(conn: sqlite3.Connection, client: SteamClient, targets: list[int], options: SyncOptions,
                     report: SyncReport, log: Callable[[str], None], now: Callable[[], datetime]) -> None:
    rows = store.game_rows(conn, targets)
    due = [appid for appid in targets if _details_due(rows[appid], now(), options.force)]
    report.counts["details_skipped_fresh"] += len(targets) - len(due)
    if not due:
        return
    extras = _fetch_store_extras(client, due, report, log)
    for index, appid in enumerate(due, start=1):
        if report.stopped_early:
            report.counts["details_not_attempted"] += len(due) - index + 1
            break
        label = f"[details {index}/{len(due)}] {rows[appid]['name'] or rows[appid]['curated_title']} ({appid})"
        try:
            data = client.get_app_details(appid)
            report.succeed()
        except SteamError as exc:
            store.record_details_problem(conn, appid, status="error", error=str(exc), now=_iso(now()))
            report.fail(f"details {appid}: {exc}")
            log(f"{label}: failed, previous data kept ({exc})")
            continue
        if data is None:
            store.record_details_problem(
                conn, appid, status="unavailable", error="Steam returned no store details for this app.", now=_iso(now())
            )
            report.counts["details_unavailable"] += 1
            log(f"{label}: Steam has no store details for this app")
            continue
        details = prepare.parse_app_details(appid, data)
        outcome = store.save_details(
            conn, details, extras.get(appid), now=_iso(now()), last_modified=rows[appid]["steam_last_modified"]
        )
        report.counts[f"details_{outcome}"] += 1
        log(f"{label}: {outcome}")


def _reviews_due(row: sqlite3.Row, now: datetime, force: bool) -> bool:
    if force or row["reviews_updated_at"] is None or row["reviews_status"] == "error":
        return True  # never fetched, or the last attempt failed: try again now
    return now - datetime.fromisoformat(row["reviews_updated_at"]) >= REVIEWS_MAX_AGE


def _widest_day_range(*responses: dict) -> int | None:
    """The widest review window Steam searched; 0 means no limit (all time)."""
    used = [r.get("day_range_used") for r in responses if r.get("day_range_used") is not None]
    if not used:
        return None
    return 0 if 0 in used else max(used)


def _refresh_reviews(conn: sqlite3.Connection, client: SteamClient, targets: list[int], options: SyncOptions,
                     report: SyncReport, log: Callable[[str], None], now: Callable[[], datetime],
                     language: str) -> None:
    rows = store.game_rows(conn, targets)
    due = [appid for appid in targets if _reviews_due(rows[appid], now(), options.force)]
    report.counts["reviews_skipped_fresh"] += len(targets) - len(due)
    for index, appid in enumerate(due, start=1):
        if report.stopped_early:
            report.counts["reviews_not_attempted"] += len(due) - index + 1
            break
        label = f"[reviews {index}/{len(due)}] {rows[appid]['name'] or rows[appid]['curated_title']} ({appid})"
        try:
            # Overall totals: every language, review type All (score fields only come with All).
            totals = client.get_reviews(appid, review_type=REVIEW_TYPE_ALL, languages=["all"], num_per_page=1)
            positives = client.get_reviews(appid, review_type=REVIEW_TYPE_POSITIVE, languages=[language],
                                           num_per_page=REVIEWS_REQUESTED_PER_SIDE, day_range=REVIEW_DAY_RANGE)
            negatives = client.get_reviews(appid, review_type=REVIEW_TYPE_NEGATIVE, languages=[language],
                                           num_per_page=REVIEWS_REQUESTED_PER_SIDE, day_range=REVIEW_DAY_RANGE)
            report.succeed()
        except SteamError as exc:
            store.record_reviews_problem(conn, appid, error=str(exc), now=_iso(now()))
            report.fail(f"reviews {appid}: {exc}")
            log(f"{label}: failed, previous reviews kept ({exc})")
            continue
        positive = prepare.select_excerpts(positives.get("reviews") or [], positive=True, count=EXCERPTS_PER_SIDE)
        negative = prepare.select_excerpts(negatives.get("reviews") or [], positive=False, count=EXCERPTS_PER_SIDE)
        store.save_reviews(
            conn, appid, prepare.parse_review_summary(totals), positive, negative,
            day_range_used=_widest_day_range(positives, negatives), now=_iso(now()),
        )
        report.counts["reviews_refreshed"] += 1
        log(f"{label}: {len(positive)} positive and {len(negative)} negative excerpts")


def _prices_due(row: sqlite3.Row | None, now: datetime, force: bool, country: str) -> bool:
    if force or row is None or row["country_code"] != country:
        return True  # never checked, or checked for another store region
    return now - datetime.fromisoformat(row["checked_at"]) >= PRICES_MAX_AGE


def _store_currency(client: SteamClient, appid: int, country: str, report: SyncReport,
                    log: Callable[[str], None]) -> str | None:
    """The currency code of the store region, read from one priced game; None when it cannot be read."""
    try:
        currency = client.get_store_currency(appid, country)
        report.succeed()
    except SteamError as exc:
        report.fail(f"store currency: {exc}")
        log(f"The {country} store's currency could not be read; prices are saved without it ({exc})")
        return None
    return currency


def _refresh_prices(conn: sqlite3.Connection, client: SteamClient, targets: list[int], options: SyncOptions,
                    report: SyncReport, log: Callable[[str], None], now: Callable[[], datetime], country: str) -> None:
    """Read the free flag and best purchase option of the due games, 50 per request, for one store region.

    A batch that fails keeps its stored prices; their check time then shows they are getting old.
    """
    rows = store.price_rows(conn, targets)
    due = [appid for appid in targets if _prices_due(rows.get(appid), now(), options.force, country)]
    report.counts["prices_skipped_fresh"] += len(targets) - len(due)
    currency, currency_read = None, False  # one store region has one currency: read it once per run
    for start in range(0, len(due), STORE_ITEMS_BATCH_SIZE):
        if report.stopped_early:
            report.counts["prices_not_attempted"] += len(due) - start
            break
        batch = due[start : start + STORE_ITEMS_BATCH_SIZE]
        try:
            items = client.get_purchase_options(batch, country=country)
            report.succeed()
        except SteamError as exc:
            report.fail(f"prices for {len(batch)} apps: {exc}")
            log(f"Prices unavailable for {len(batch)} apps; stored prices are kept: {exc}")
            continue
        prices = {
            appid: prepare.price_from_store_item(items[appid]) if appid in items
            else prepare.PriceFacts(status=prepare.UNAVAILABLE)
            for appid in batch
        }
        priced = next((appid for appid, facts in prices.items() if facts.status == prepare.PRICED), None)
        if not currency_read and priced is not None:
            currency, currency_read = _store_currency(client, priced, country, report, log), True
        store.save_prices(conn, prices, country=country, currency=currency, now=_iso(now()))
        report.counts["prices_refreshed"] += len(batch)
        report.counts["prices_discounted"] += sum(1 for facts in prices.values() if facts.discount_percent)
        log(f"[prices {start + len(batch)}/{len(due)}] {country} store: {len(batch)} games checked")


def _players_due(latest: str | None, now: datetime, force: bool) -> bool:
    return force or latest is None or now - datetime.fromisoformat(latest) >= PLAYERS_MAX_AGE


def _refresh_players(conn: sqlite3.Connection, client: SteamClient, targets: list[int], options: SyncOptions,
                     report: SyncReport, log: Callable[[str], None], now: Callable[[], datetime]) -> None:
    """Save a dated snapshot of the current player count of each due game, one request per game."""
    latest = store.latest_player_times(conn, targets)
    due = [appid for appid in targets if _players_due(latest.get(appid), now(), options.force)]
    report.counts["players_skipped_fresh"] += len(targets) - len(due)
    for index, appid in enumerate(due, start=1):
        if report.stopped_early:
            report.counts["players_not_attempted"] += len(due) - index + 1
            break
        try:
            players = client.get_current_players(appid)
            report.succeed()
        except SteamError as exc:
            report.fail(f"players {appid}: {exc}")
            log(f"[players {index}/{len(due)}] app {appid}: failed ({exc})")
            continue
        store.save_player_count(conn, appid, players, now=_iso(now()))
        report.counts["players_counted" if players is not None else "players_unavailable"] += 1
        if index % PLAYERS_PROGRESS_EVERY == 0 or index == len(due):
            log(f"[players {index}/{len(due)}] current player counts saved")


def run_sync(
    conn: sqlite3.Connection,
    client: SteamClient,
    curated_games: list[CuratedGame],
    options: SyncOptions,
    *,
    review_language: str = "english",
    country: str = DEFAULT_STEAM_COUNTRY,
    log: Callable[[str], None] = print,
    now: Callable[[], datetime] = utc_now,
) -> SyncReport:
    """Run one sync and record its start, end, status and counts in sync_runs.

    `country` is the Steam store region whose prices are read (GAME_LIBRARIAN_STEAM_COUNTRY).
    """
    run_id = store.start_sync_run(conn, now=_iso(now()))
    report = SyncReport()
    try:
        report.counts["curated_games"] = len(curated_games)
        report.counts["new_curated_games"] = store.sync_curated_games(conn, [(g.appid, g.title) for g in curated_games])
        targets = _select_targets(curated_games, options)
        report.counts["selected_games"] = len(targets)
        _discover(conn, client, options, report, log, now)
        if not report.stopped_early:
            _refresh_details(conn, client, targets, options, report, log, now)
        if options.skip_reviews:
            report.counts["reviews_skipped_by_option"] = len(targets)
        elif not report.stopped_early:
            _refresh_reviews(conn, client, targets, options, report, log, now, review_language)
        if options.skip_prices:
            report.counts["prices_skipped_by_option"] = len(targets)
        elif not report.stopped_early:
            _refresh_prices(conn, client, targets, options, report, log, now, country)
        if options.skip_players:
            report.counts["players_skipped_by_option"] = len(targets)
        elif not report.stopped_early:
            _refresh_players(conn, client, targets, options, report, log, now)
    except BaseException as exc:  # also Ctrl+C: the run is still recorded before stopping
        report.errors.append(f"sync stopped: {exc!r}")
        store.finish_sync_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    if report.stopped_early:
        log(report.stopped_early)
    store.finish_sync_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report
