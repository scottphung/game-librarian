"""The Trending page: separate, labeled signals about what players pay attention to. No combined score.

Every section works on the catalog games only (not all of Steam), from data the sync stored:

- Popular right now: the most players in game on Steam at the newest player snapshot. One
  moment per game, so it varies with the hour and the day it was taken.
- Released this year with strong player reception, and released in the window of the five-year
  ranking with strong player reception: an exact Steam release date in the window, and a Steam review
  summary label of Overwhelmingly Positive or Very Positive (catalog.STRONG_RECEPTION_LABELS),
  in "Best player reception" order.
- Recent changes: the difference between a game's newest snapshot and its newest snapshot from
  CHANGE_WINDOW to MAX_BASELINE_AGE earlier. Without two such snapshots there is no change to
  show, and the page says when one can exist; nothing is estimated or filled in.

Everything here is pure: the web layer passes the stored facts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from .catalog import STRONG_RECEPTION_LABELS, CatalogGame, reception_order
from .ranking import RankingWindow

SECTION_SIZE = 10
CHANGE_WINDOW = timedelta(days=7)  # a change compares snapshots at least this far apart...
MAX_BASELINE_AGE = timedelta(days=14)  # ...and at most this far apart


def has_strong_reception(game: CatalogGame) -> bool:
    """Steam's review summary for the game is Overwhelmingly Positive or Very Positive."""
    return game.review_label in STRONG_RECEPTION_LABELS


def popular_now(games: Iterable[CatalogGame]) -> list[CatalogGame]:
    """Games with players in game at their newest snapshot, the most first (ties: A–Z)."""
    playing = [game for game in games if game.players]
    return sorted(playing, key=lambda game: (-game.players, game.sort_name, game.appid))


def _released_between(game: CatalogGame, start: date, end: date) -> bool:
    return game.released_on is not None and not game.upcoming and start <= game.released_on <= end


def well_received_released_between(games: Iterable[CatalogGame], start: date, end: date) -> list[CatalogGame]:
    """Games with an exact Steam release date from `start` to `end` and strong reception, best reception first."""
    chosen = [game for game in games if _released_between(game, start, end) and has_strong_reception(game)]
    return sorted(chosen, key=reception_order)


def this_year(today: date) -> tuple[date, date]:
    """1 January of the current year to today, both included."""
    return date(today.year, 1, 1), today


def ranking_window(window: RankingWindow) -> tuple[date, date]:
    """The ranking's window (on the website, the five-year window), so this section and the Top-ranked view cover
    the same games."""
    return window.start, window.end


# ---------------------------------------------------------------------- changes between snapshots


@dataclass(frozen=True)
class Change:
    """One game's value at two snapshots: the newest one and the baseline about a week earlier."""

    appid: int
    before: int
    after: int
    before_at: datetime
    after_at: datetime
    positive_added: int | None = None  # for reviews: how many of the added reviews are positive

    @property
    def difference(self) -> int:
        return self.after - self.before

    @property
    def percent(self) -> float | None:
        """The change relative to the baseline, or None when the baseline is zero."""
        return self.difference / self.before * 100 if self.before else None


def baseline_snapshot(snapshots: Sequence[tuple[datetime, int]], latest_at: datetime) -> tuple[datetime, int] | None:
    """The newest snapshot taken CHANGE_WINDOW to MAX_BASELINE_AGE before `latest_at`, or None."""
    eligible = [(moment, value) for moment, value in snapshots
                if latest_at - MAX_BASELINE_AGE <= moment <= latest_at - CHANGE_WINDOW]
    return max(eligible, key=lambda snapshot: snapshot[0]) if eligible else None


def player_changes(history: dict[int, list]) -> list[Change]:
    """Games whose player count rose between a baseline snapshot and the newest one, the largest rise first.

    `history` maps app ids to player snapshot rows (captured_at, players), oldest first; snapshots
    without a count are skipped. Counts at two moments also differ by time of day, which the page says.
    """
    changes = []
    for appid, rows in history.items():
        snapshots = [(datetime.fromisoformat(row["captured_at"]), row["players"]) for row in rows
                     if row["players"] is not None]
        if not snapshots:
            continue
        latest_at, latest = snapshots[-1]
        start = baseline_snapshot(snapshots, latest_at)
        if start and latest > start[1]:
            changes.append(Change(appid, start[1], latest, start[0], latest_at))
    return sorted(changes, key=lambda change: (-change.difference, change.appid))


def review_changes(history: dict[int, list]) -> list[Change]:
    """Games that gained Steam reviews between a baseline snapshot and the newest one, the most first.

    `history` maps app ids to review snapshot rows (captured_at, total, positive), oldest first.
    The positive part of the new reviews is kept only when it is consistent (0 to all of them).
    """
    changes = []
    for appid, rows in history.items():
        if not rows:
            continue
        snapshots = [(datetime.fromisoformat(row["captured_at"]), row["total"]) for row in rows]
        positives = {datetime.fromisoformat(row["captured_at"]): row["positive"] for row in rows}
        latest_at, latest = snapshots[-1]
        start = baseline_snapshot(snapshots, latest_at)
        if not start or latest <= start[1]:
            continue
        added = latest - start[1]
        positive_added = positives[latest_at] - positives[start[0]]
        changes.append(Change(appid, start[1], latest, start[0], latest_at,
                              positive_added if 0 <= positive_added <= added else None))
    return sorted(changes, key=lambda change: (-change.difference, change.appid))


def history_status(summary: dict) -> dict:
    """What the snapshot history allows, from store.snapshot_summary()[kind].

    Returns the first and newest capture, the number of UTC days with captures, and the earliest
    moment a change can be measured (CHANGE_WINDOW after the first capture).
    """
    first = datetime.fromisoformat(summary["first"]) if summary.get("first") else None
    last = datetime.fromisoformat(summary["last"]) if summary.get("last") else None
    return {
        "first": first,
        "last": last,
        "days": summary.get("days") or 0,
        "games": summary.get("games") or 0,
        "changes_possible_from": first + CHANGE_WINDOW if first else None,
        "has_window": bool(first and last and last - first >= CHANGE_WINDOW),
    }
