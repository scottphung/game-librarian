"""Controlled catalog expansion: candidates from several sources, screened against Steam, reviewed, promoted.

Workflow (README.md, "Growing the catalog"):

  1. add      Collect candidates. Sources: the Steam discovery index (a bounded batch per run, lowest app
              id first), verified award rows whose Steam app is not in the catalog yet, and (optionally)
              IGDB. A game found by several sources becomes one candidate that lists every source.
  2. screen   Check candidates against Steam in batches of 50 apps per request (IStoreBrowseService/GetItems)
              and apply the inclusion rules below. Progress is saved after every batch, so an interrupted
              run continues where it stopped; failed requests are retried by later runs.
  3. review   `candidates list` / `show` print each candidate's status and the reason for it.
  4. promote  A reviewer promotes candidates; promotion appends them to curated/promoted_games.csv.
              The next `sync` then enriches them like every other catalog game. Promotion never removes
              or changes a hand-picked game.

Steam match. A candidate is screened only when its Steam app id is known from a reliable identifier:
the discovery index itself, a verified curated award mapping, or the Steam id IGDB records. A match by
title alone is never accepted automatically: it waits as needs_review until a reviewer confirms the
Steam app with `candidates match`, because a title cannot tell a sequel, remaster, edition, DLC or an
unrelated game with the same name apart.

Inclusion rules (EligibilityRules; the screen command can change the two thresholds):
  * Steam returns store data for the app in the US store, and shows it;
  * Steam lists it as a game (not DLC, a demo, software, a video, a soundtrack or another product);
  * Steam sells it or offers it for free;
  * it is released (not "coming soon", and its release date is not after today);
  * it has at least 50 Steam reviews, and at least 70% of them are positive.
  Games Steam marks with adult-only or frequent sexual content (content descriptors 3 and 4) pass the
  rules but wait as needs_review, so a person decides. Nothing requires an award or a production label.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import igdb, store
from .curated import PROMOTED_COLUMNS, PROMOTED_FILE, CuratedData
from .prepare import normalize_for_search
from .steam import SteamClient, SteamError

# Candidate statuses.
PENDING = "pending"  # waiting for screening
ELIGIBLE = "eligible"  # passed every rule; ready to promote
NEEDS_REVIEW = "needs_review"  # a person must decide: a title-only match, or flagged content
INELIGIBLE = "ineligible"  # failed a rule; the reason says which
ERROR = "error"  # the last screening request failed; retried by later runs
PROMOTED = "promoted"  # added to curated/promoted_games.csv
REJECTED = "rejected"  # a reviewer decided against it
STATUSES = (PENDING, ELIGIBLE, NEEDS_REVIEW, INELIGIBLE, ERROR, PROMOTED, REJECTED)

SOURCES = ("discovery", "awards", "igdb")
SCREEN_BATCH_SIZE = 50  # apps per GetItems request
MAX_SCREEN_ATTEMPTS = 3  # a candidate whose screening failed this many times in a row is left alone
MAX_CONSECUTIVE_FAILURES = 3  # then Steam is treated as unavailable and the run stops early
MAX_ERRORS_KEPT = 25
DEFAULT_IGDB_LIMIT = 100

# Steam store app types seen in GetItems answers. Any type other than 0 is not a game.
STEAM_TYPE_NAMES = {0: "a game", 1: "a demo", 4: "DLC"}
# Steam content descriptors that send a candidate to human review instead of straight to eligible.
REVIEW_CONTENT_DESCRIPTORS = {3: "adult-only sexual content", 4: "frequent nudity or sexual content"}


@dataclass(frozen=True)
class EligibilityRules:
    """The thresholds of the automatic inclusion rules (documented in README.md)."""

    min_reviews: int = 50
    min_positive_percent: int = 70


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------- screening rules (pure)


def facts_from_store_item(item: dict) -> store.CandidateFacts:
    """Read the screening facts from one IStoreBrowseService/GetItems item."""
    release = item.get("release") or {}
    released_at = release.get("steam_release_date")
    released_on = (datetime.fromtimestamp(int(released_at), tz=timezone.utc).date().isoformat()
                   if released_at else None)
    summary = (item.get("reviews") or {}).get("summary_filtered") or {}
    return store.CandidateFacts(
        steam_name=(item.get("name") or "").strip() or None,
        steam_type=item.get("type"),
        visible=bool(item.get("visible")),
        available=bool(item.get("is_free") or item.get("best_purchase_option")),
        review_count=int(summary.get("review_count") or 0),
        percent_positive=int(summary.get("percent_positive") or 0),
        released_on=released_on,
        coming_soon=bool(release.get("is_coming_soon")),
        content_descriptors=[int(d) for d in item.get("content_descriptorids") or []],
    )


def judge(facts: store.CandidateFacts | None, rules: EligibilityRules, today: date) -> tuple[str, str]:
    """Apply the inclusion rules to one screened app. Return (status, reason)."""
    if facts is None:
        return INELIGIBLE, ("Steam returned no store data for this app in the US store "
                            "(it may be delisted, region-locked or not public).")
    if not facts.visible:
        return INELIGIBLE, "Steam does not show this app in the US store."
    if facts.steam_type != 0:
        kind = STEAM_TYPE_NAMES.get(facts.steam_type, f"Steam store type {facts.steam_type}")
        return INELIGIBLE, f"Steam lists it as {kind}, not a game."
    if not facts.available:
        return INELIGIBLE, "Steam does not sell it or offer it for free in the US store now."
    if facts.coming_soon or (facts.released_on and date.fromisoformat(facts.released_on) > today):
        return INELIGIBLE, "It is not released yet."
    if facts.review_count < rules.min_reviews:
        return INELIGIBLE, (f"{facts.review_count:,} Steam reviews, fewer than the minimum of "
                            f"{rules.min_reviews:,}.")
    if facts.percent_positive < rules.min_positive_percent:
        return INELIGIBLE, (f"{facts.percent_positive}% of its Steam reviews are positive, below the minimum "
                            f"of {rules.min_positive_percent}%.")
    summary = (f"Steam game; {facts.review_count:,} reviews, {facts.percent_positive}% positive"
               + (f"; released {facts.released_on}" if facts.released_on else "") + ".")
    flagged = [label for descriptor, label in REVIEW_CONTENT_DESCRIPTORS.items()
               if descriptor in facts.content_descriptors]
    if flagged:
        return NEEDS_REVIEW, f"{summary} Steam marks it with {' and '.join(flagged)}; promote it only if it belongs here."
    return ELIGIBLE, summary


# ---------------------------------------------------------------------- reports


@dataclass
class RunReport:
    """Counts and errors of one candidates command; saved in candidate_runs."""

    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    stopped_early: str | None = None
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0

    def succeed(self) -> None:
        self.successes += 1
        self.consecutive_failures = 0

    def fail(self, message: str) -> None:
        self.failures += 1
        self.consecutive_failures += 1
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append(message)
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self.stopped_early:
            self.stopped_early = (f"Stopped after {MAX_CONSECUTIVE_FAILURES} failed requests in a row; the "
                                  "service looks unavailable. Progress so far is saved.")

    def status(self) -> str:
        if self.failures == 0 and not self.stopped_early:
            return "success"
        return "failed" if self.successes == 0 else "partial"

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "stopped_early": self.stopped_early, "errors": self.errors}


def _record_run(conn: sqlite3.Connection, command: str, now: Callable[[], datetime],
                work: Callable[[RunReport], None]) -> RunReport:
    """Run `work` and record the run (start, end, status, counts) in candidate_runs, even if it raises."""
    run_id = store.start_candidate_run(conn, command, now=_iso(now()))
    report = RunReport()
    try:
        work(report)
    except BaseException as exc:  # also Ctrl+C: the run is recorded before stopping
        report.errors.append(f"stopped: {type(exc).__name__}")
        store.finish_candidate_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    store.finish_candidate_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report


# ---------------------------------------------------------------------- sources


def add_from_discovery(conn: sqlite3.Connection, *, limit: int, catalog_appids: set[int],
                       now: Callable[[], datetime] = utc_now) -> RunReport:
    """Add the next `limit` discovery-index apps that are not candidates or catalog games yet."""
    def work(report: RunReport) -> None:
        for row in store.discovered_apps_to_add(conn, limit):
            if row["appid"] in catalog_appids:  # listed in a curated file but not synced yet
                report.counts["skipped_in_catalog"] += 1
                continue
            outcome = store.add_candidate(
                conn, appid=row["appid"], title=row["name"] or f"Steam app {row['appid']}",
                source={"source": "discovery", "ref": f"steam:{row['appid']}"}, match_method="steam_app",
                now=_iso(now()),
            )
            report.counts[outcome] += 1
    return _record_run(conn, "add-discovery", now, work)


def add_from_awards(conn: sqlite3.Connection, curated: CuratedData,
                    now: Callable[[], datetime] = utc_now) -> RunReport:
    """Add the Steam apps of verified award results that are not in the catalog yet.

    Only rows a curator mapped to a Steam app (steam_mapping same or edition) are used, so the
    match is the curator's verified decision. Rows without a Steam app are never candidates.
    """
    catalog_appids = {game.appid for game in curated.games}

    def work(report: RunReport) -> None:
        for award in curated.awards:
            if award.review_status != "verified" or award.appid is None:
                continue
            if award.appid in catalog_appids:
                report.counts["skipped_in_catalog"] += 1
                continue
            result = "winner" if award.result == "winner" else "nominee"
            outcome = store.add_candidate(
                conn, appid=award.appid, title=award.award_title, match_method="curated_award", now=_iso(now()),
                source={"source": "awards", "ref": f"{award.organization} {award.year} {award.category} {result}",
                        "detail": award.award_name},
            )
            report.counts[outcome] += 1
    return _record_run(conn, "add-awards", now, work)


def add_from_igdb(conn: sqlite3.Connection, client: igdb.IgdbClient, *, limit: int, catalog_appids: set[int],
                  offset: int = 0, now: Callable[[], datetime] = utc_now) -> RunReport:
    """Add up to `limit` popular PC games from IGDB.

    A game with exactly one Steam id in IGDB becomes a candidate for that Steam app. A game without a
    Steam id, or with several, becomes a title-only candidate (needs_review) that lists the Steam apps
    to check: apps in the discovery index with exactly the same normalized title, or IGDB's Steam ids.
    """
    def work(report: RunReport) -> None:
        try:
            games = client.popular_pc_games(limit=limit, offset=offset)
            report.succeed()
        except igdb.IgdbError as exc:
            report.fail(f"IGDB: {exc}")
            return
        titles: dict[str, list[int]] | None = None  # normalized title -> discovery-index app ids, built once
        for game in games:
            name = str(game.get("name") or "").strip()
            if not name or not isinstance(game.get("id"), int):
                report.counts["skipped_incomplete"] += 1
                continue
            year = (datetime.fromtimestamp(int(game["first_release_date"]), tz=timezone.utc).year
                    if game.get("first_release_date") else None)
            source = {"source": "igdb", "ref": f"igdb:{game['id']}",
                      "detail": f"{name} ({year})" if year else name}
            appids = igdb.steam_appids(game)
            if len(appids) == 1:
                if appids[0] in catalog_appids:
                    report.counts["skipped_in_catalog"] += 1
                    continue
                outcome = store.add_candidate(conn, appid=appids[0], igdb_id=game["id"], title=name, source=source,
                                              match_method="igdb_steam_id", now=_iso(now()))
            else:
                if titles is None:
                    titles = {}
                    for row in store.discovered_app_names(conn):
                        titles.setdefault(normalize_for_search(row["name"]), []).append(row["appid"])
                suggested = appids or titles.get(normalize_for_search(name), [])
                if any(appid in catalog_appids for appid in suggested):
                    report.counts["skipped_in_catalog"] += 1
                    continue
                if appids:
                    reason = (f"IGDB lists {len(appids)} Steam apps for this game. Check which one is the game "
                              "itself, then run `candidates match`.")
                elif suggested:
                    reason = (f"Matched by title only: {len(suggested)} Steam {'app has' if len(suggested) == 1 else 'apps have'} "
                              "exactly this title. A title alone cannot tell sequels, remasters, editions or other "
                              "games apart: check the app, then run `candidates match`.")
                else:
                    reason = "IGDB lists no Steam app, and no app in the discovery index has exactly this title."
                outcome = store.add_candidate(conn, igdb_id=game["id"], title=name, source=source,
                                              match_method="title_only", status=NEEDS_REVIEW, reason=reason,
                                              suggested_appids=suggested, now=_iso(now()))
            report.counts[outcome] += 1
    return _record_run(conn, "add-igdb", now, work)


# ---------------------------------------------------------------------- screening


def screen(conn: sqlite3.Connection, client: SteamClient, *, limit: int, rules: EligibilityRules = EligibilityRules(),
           recheck_days: int | None = None, log: Callable[[str], None] = print,
           now: Callable[[], datetime] = utc_now) -> RunReport:
    """Screen up to `limit` due candidates against Steam, SCREEN_BATCH_SIZE apps per request.

    Each batch is saved before the next request, so stopping the run (or a failure) loses nothing.
    A failed batch marks its candidates as error; they are retried by later runs, up to
    MAX_SCREEN_ATTEMPTS times in a row. After MAX_CONSECUTIVE_FAILURES failed requests the run stops.
    """
    recheck_before = _iso(now() - timedelta(days=recheck_days)) if recheck_days is not None else None

    def work(report: RunReport) -> None:
        due = store.candidates_to_screen(conn, limit, max_attempts=MAX_SCREEN_ATTEMPTS, recheck_before=recheck_before)
        report.counts["due"] = len(due)
        for start in range(0, len(due), SCREEN_BATCH_SIZE):
            if report.stopped_early:
                report.counts["not_attempted"] += len(due) - start
                break
            batch = due[start : start + SCREEN_BATCH_SIZE]
            try:
                items = client.get_item_summaries([row["appid"] for row in batch])
                report.succeed()
            except SteamError as exc:
                report.fail(f"screening {len(batch)} apps: {exc}")
                for row in batch:
                    store.record_screening_error(conn, row["id"], error=str(exc), now=_iso(now()))
                report.counts["errors"] += len(batch)
                log(f"[screen {start + len(batch)}/{len(due)}] request failed; these candidates will be retried: {exc}")
                continue
            today = now().date()
            for row in batch:
                item = items.get(row["appid"])
                facts = facts_from_store_item(item) if item else None
                status, reason = judge(facts, rules, today)
                store.save_screening(conn, row["id"], facts, status=status, reason=reason, now=_iso(now()))
                report.counts[status] += 1
            log(f"[screen {start + len(batch)}/{len(due)}] saved")
        if report.stopped_early:
            log(report.stopped_early)
    return _record_run(conn, "screen", now, work)


# ---------------------------------------------------------------------- review decisions


class CandidateError(Exception):
    """A review command cannot be applied to this candidate (the message says why)."""


def _evidence(row: sqlite3.Row, today: date) -> str:
    """One line for promoted_games.csv: what Steam showed at screening, and where the candidate came from."""
    sources = "; ".join(source.get("detail") or source.get("ref", "") for source in _sources(row))
    released = f"; released {row['released_on']}" if row["released_on"] else ""
    return (f"Steam game; {row['review_count'] or 0:,} reviews, {row['percent_positive'] or 0}% positive{released}; "
            f"screened {(row['screened_at'] or '')[:10]}; promoted {today.isoformat()}. Sources: {sources}.")


def _sources(row: sqlite3.Row) -> list[dict]:
    return json.loads(row["sources"])


def source_names(row: sqlite3.Row) -> str:
    """'awards+discovery': the distinct source names of a candidate, sorted."""
    return "+".join(sorted({source["source"] for source in _sources(row)}))


def promote(conn: sqlite3.Connection, curated_dir: Path, curated: CuratedData, appids: list[int], *,
            now: Callable[[], datetime] = utc_now) -> list[sqlite3.Row]:
    """Append the given screened candidates to curated/promoted_games.csv and mark them promoted.

    Only eligible and needs_review candidates with a Steam app can be promoted. Every candidate is
    checked before anything is written, so one bad app id changes nothing.
    """
    catalog_appids = {game.appid for game in curated.games}
    rows = []
    for appid in dict.fromkeys(appids):
        row = store.candidate(conn, appid=appid)
        if row is None:
            raise CandidateError(f"Steam app {appid} is not a candidate. Add it first, or add it to curated/games.csv by hand.")
        if appid in catalog_appids:
            raise CandidateError(f"Steam app {appid} is already in the catalog.")
        if row["status"] not in (ELIGIBLE, NEEDS_REVIEW) or row["screened_at"] is None:
            raise CandidateError(
                f"Steam app {appid} is {row['status']}"
                + (f" ({row['reason']})" if row["reason"] else "")
                + ". Only screened eligible or needs_review candidates can be promoted; to add a game that fails "
                  "the automatic rules, list it in curated/games.csv by hand."
            )
        rows.append(row)
    path = curated_dir / PROMOTED_FILE
    new_file = not path.exists()
    today = now().date()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PROMOTED_COLUMNS, lineterminator="\n")
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({
                "steam_appid": row["appid"], "title": row["steam_name"] or row["title"], "source": source_names(row),
                "match_method": row["match_method"], "evidence": _evidence(row, today), "promoted_on": today.isoformat(),
            })
    for row in rows:
        store.set_candidate_status(conn, row["id"], status=PROMOTED,
                                   reason=f"Promoted to {PROMOTED_FILE} on {today.isoformat()}.", now=_iso(now()))
    return rows


def eligible_appids(conn: sqlite3.Connection, limit: int) -> list[int]:
    """The eligible candidates with the most Steam reviews, for `promote --all-eligible`."""
    return [row["appid"] for row in store.list_candidates(conn, status=ELIGIBLE, limit=limit)]


def reject(conn: sqlite3.Connection, row: sqlite3.Row, reason: str, *, now: Callable[[], datetime] = utc_now) -> None:
    """Record a reviewer's decision not to add a candidate. A promoted candidate cannot be rejected here."""
    if row["status"] == PROMOTED:
        raise CandidateError("This candidate is already promoted; remove its row from curated/promoted_games.csv instead.")
    store.set_candidate_status(conn, row["id"], status=REJECTED, reason=f"Rejected by a reviewer: {reason}",
                               now=_iso(now()))


def match(conn: sqlite3.Connection, row: sqlite3.Row, appid: int, catalog_appids: set[int], *,
          now: Callable[[], datetime] = utc_now) -> None:
    """Confirm the Steam app of a title-only candidate; it is screened on the next `candidates screen`."""
    if row["match_method"] != "title_only" or row["appid"] is not None:
        raise CandidateError("Only title-only candidates without a Steam app can be matched.")
    if appid in catalog_appids:
        raise CandidateError(f"Steam app {appid} is already in the catalog.")
    other = store.candidate(conn, appid=appid)
    if other is not None:
        raise CandidateError(f"Steam app {appid} is already candidate #{other['id']} ({other['status']}).")
    store.match_candidate(conn, row["id"], appid, now=_iso(now()))
