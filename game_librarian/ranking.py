"""The four-year ranking: release window, production tier, then positive Steam reviews.

Only games released in the last four years are ranked. The window is rolling: it
runs from the same calendar day four years ago up to and including today, so it
moves forward every day. Within the window, games are ordered by:

1. Production tier. Verified AAA games come first, then verified AA games, then
   Indie and Unclassified games together (Unclassified = no verified label).
   A game with two verified labels, such as AA and Indie, ranks in its higher tier.
2. The number of positive Steam reviews, most first. This is the count itself,
   not the positive percentage.
3. Ties: library sort name (title A-Z, ignoring case, accents, punctuation and a
   leading "The", "A" or "An"), then Steam app id, so every rank is distinct
   and repeatable.

So an AAA game ranks above an AA game, and an AA game above an Indie or
Unclassified game, even when it has fewer positive reviews.

This is a discovery ordering, not a judgment of quality. Counting reviews favors
popular games and games that have been on sale longer within the window.
Awards are deliberately not part of the ranking.

Games released in the window without a usable positive review count are
"Unranked": they get no number and are listed after the ranked games, instead
of being treated as having zero positive reviews. Games outside the window
(released earlier, not released yet, or without an exact release date on
Steam) are not part of the ranking at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Collection, Iterable

from .curated import CuratedData
from .prepare import parse_release_date

WINDOW_YEARS = 4

TIER_AAA, TIER_AA, TIER_INDIE_AND_UNCLASSIFIED = 1, 2, 3
TIER_NAMES = {TIER_AAA: "AAA", TIER_AA: "AA", TIER_INDIE_AND_UNCLASSIFIED: "Indie and Unclassified"}

NOT_IN_RANKING = "Not in the four-year ranking"


@dataclass(frozen=True)
class RankingWindow:
    """The release dates that the four-year ranking covers: `start` to `end`, both included."""

    start: date
    end: date

    @classmethod
    def ending_on(cls, today: date) -> RankingWindow:
        """The four years up to and including `today`: 9 Nov 2026 -> 9 Nov 2022 to 9 Nov 2026."""
        try:
            start = today.replace(year=today.year - WINDOW_YEARS)
        except ValueError:  # 29 February in a year whose fourth-previous year has none (2104 -> 2100)
            start = today.replace(year=today.year - WINDOW_YEARS, day=28)
        return cls(start=start, end=today)


@dataclass(frozen=True)
class GameFacts:
    """What the ranking needs to know about one game."""

    appid: int
    sort_name: str
    labels: Collection[str]  # verified production labels only: "AAA", "AA" and/or "Indie"
    positive: int | None  # positive Steam reviews, as stored by the last review sync
    total: int | None  # all Steam reviews, from the same summary
    release_date: str | None  # Steam's release-date text, such as "Aug 3, 2023"
    coming_soon: bool = False  # Steam's own "not released yet" flag


@dataclass(frozen=True)
class Placement:
    """Where one game stands in the four-year ranking."""

    tier: int
    released_on: date | None  # the exact release date, when Steam's text gives one
    upcoming: bool  # not released yet: Steam says "coming soon", or the exact date is after today
    window_note: str | None  # why the game is outside the four-year ranking; None when it is inside
    unranked_reason: str | None  # why its positive review count cannot be used; None when it can
    rank: int | None = None  # 1 = first; None outside the window or when the game is Unranked

    @property
    def in_window(self) -> bool:
        """True when the game was released inside the four-year window."""
        return self.window_note is None

    @property
    def tier_name(self) -> str:
        return TIER_NAMES[self.tier]

    @property
    def status(self) -> str:
        """"Ranked", "Unranked" (in the window, no usable count) or "Not in the four-year ranking"."""
        if not self.in_window:
            return NOT_IN_RANKING
        return "Ranked" if self.rank is not None else "Unranked"

    @property
    def note(self) -> str | None:
        """Why the game has no rank, or None when it has one."""
        return self.window_note or (self.unranked_reason if self.rank is None else None)


def production_tier(labels: Collection[str]) -> int:
    """Map verified labels to a tier: AAA -> 1, AA -> 2, Indie or no verified label -> 3."""
    if "AAA" in labels:
        return TIER_AAA
    if "AA" in labels:
        return TIER_AA
    return TIER_INDIE_AND_UNCLASSIFIED


def unranked_reason(positive: int | None, total: int | None) -> str | None:
    """Explain why a stored review summary gives no usable positive count, or return None when it does.

    A stored total of zero is not used either: the sync saves a missing Steam
    summary as zeros, so a zero cannot be told apart from "no answer".
    """
    if positive is None or total is None:
        return "No Steam review summary has been saved for this game yet."
    if total == 0:
        return "This game has no Steam reviews yet."
    if positive < 0 or total < 0 or positive > total:
        return "The stored review counts are inconsistent, so the game is not ranked."
    return None


def release_window_status(release_date: str | None, coming_soon: bool,
                          window: RankingWindow) -> tuple[date | None, str | None]:
    """Return (the exact release date or None, why the game is outside `window` or None)."""
    released_on = parse_release_date(release_date)
    text = (release_date or "").strip()
    if coming_soon:
        return released_on, "Not released yet: Steam lists it as coming soon."
    if released_on is None:
        if not text:
            return None, "Steam lists no release date."
        return None, f"Steam gives no exact release date (“{text}”)."
    if released_on > window.end:
        return released_on, "Its release date is after today."
    if released_on < window.start:
        return released_on, "Released before the four-year window."
    return released_on, None


def place_games(games: Iterable[GameFacts], window: RankingWindow) -> dict[int, Placement]:
    """Place every game and rank the ones released inside `window`. Return {appid: Placement}.

    Pass the whole catalog, not a filtered subset: a game keeps the same rank
    whatever search or filters are applied later.
    """
    placements: dict[int, Placement] = {}
    rankable: list[GameFacts] = []
    for game in games:
        released_on, window_note = release_window_status(game.release_date, game.coming_soon, window)
        reason = unranked_reason(game.positive, game.total)
        placements[game.appid] = Placement(
            tier=production_tier(game.labels),
            released_on=released_on,
            upcoming=game.coming_soon or (released_on is not None and released_on > window.end),
            window_note=window_note,
            unranked_reason=reason,
        )
        if window_note is None and reason is None:
            rankable.append(game)
    # Tier first, then more positive reviews; ties go to the title A-Z, then the app id.
    rankable.sort(key=lambda game: (placements[game.appid].tier, -game.positive, game.sort_name, game.appid))
    for position, game in enumerate(rankable, start=1):
        placements[game.appid] = replace(placements[game.appid], rank=position)
    return placements


def place_catalog(rows: Iterable, curated: CuratedData, window: RankingWindow) -> dict[int, Placement]:
    """Place stored catalog rows (as returned by store.matching_games) with their verified labels."""
    return place_games(
        (
            GameFacts(
                appid=row["appid"],
                sort_name=row["sort_name"],
                labels=curated.labels_for(row["appid"]).keys(),
                positive=row["review_positive"],
                total=row["review_total"],
                release_date=row["release_date"],
                coming_soon=bool(row["coming_soon"]),
            )
            for row in rows
        ),
        window,
    )
