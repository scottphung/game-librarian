"""Steam's most-wishlisted list as the dated snapshots this site saved (pure).

Every `upcoming` refresh saves the list's tracked games with their positions (store.popularity_snapshots), one
snapshot per refresh. Two refreshes can save the same IGDB calculation: IGDB recalculates PopScore about once a
day, and `upcoming --force` can run sooner. So the snapshots are read one per IGDB calculation, the newest one saved
of it; the dashboard's "In both lists" chart uses the newest of them.

A list's key (upcoming.Signal.key) stands for one source, definition and scope: for Steam's list, IGDB PopScore
type 10 (from Steam), its first STEAM_WISHLISTS.depth positions scanned and its first TRACKED_PER_SIGNAL upcoming
games saved. Nothing is filled in: a game missing from a snapshot has no position there.

Before 0.14 this module also compared snapshots (moves, entries and exits, time in the top 10) for the dashboard's
rank chart; that chart was replaced by the release radar, and its comparisons went with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True)
class ListSnapshot:
    """One saved list: each tracked game's position (1 = first), saved at `captured_at` from the IGDB calculation
    made at `calculated_at` (None when IGDB gave no time)."""

    captured_at: datetime
    calculated_at: datetime | None
    positions: dict[int, int]  # IGDB id -> position in the list's order


def comparable_snapshots(rows: Iterable) -> list[ListSnapshot]:
    """One snapshot per IGDB calculation, oldest first, from store.popularity_snapshot_rows rows of one list.

    The rows saved at one moment form a snapshot, and its calculation is the newest calculation time among them.
    Snapshots of the same calculation hold the same IGDB values, so only the newest is kept: it reflects the latest
    upcoming check. A snapshot whose rows have no calculation time counts as a calculation of its own.
    """
    captures: dict[str, tuple[dict[int, int], list[str]]] = {}
    for row in rows:
        positions, calculated = captures.setdefault(row["captured_at"], ({}, []))
        positions[row["igdb_id"]] = row["position"]
        if row["calculated_at"]:
            calculated.append(row["calculated_at"])
    newest: dict[str, ListSnapshot] = {}
    for captured_at, (positions, calculated) in captures.items():
        calculation = max(calculated) if calculated else None
        snapshot = ListSnapshot(datetime.fromisoformat(captured_at),
                                datetime.fromisoformat(calculation) if calculation else None, positions)
        key = calculation or f"saved {captured_at}"
        if key not in newest or snapshot.captured_at > newest[key].captured_at:
            newest[key] = snapshot
    return sorted(newest.values(), key=lambda snapshot: snapshot.captured_at)
