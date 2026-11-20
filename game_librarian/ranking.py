"""The ranking: release window, award group, number of awards, then Steam reception.

This is Game Librarian's editorial discovery ranking, not an objective measure of game quality.

Only games released inside a window (RankingWindow) are ranked. The website uses the
five-year ranking (RankingWindow.for_website): the five full calendar years before this
year, plus this year up to and including today. On 20 Nov 2026 that is 1 Jan 2021 to
20 Nov 2026, and the window moves forward on 1 January. The Power BI export keeps its
rolling four-year ranking (RankingWindow.ending_on, used by report_export.py): from the
same calendar day four years ago up to the export day. Within a window, games are ordered by:

1. Award group: the game's highest-priority verified award result, in the editorial order
   in which cards also list awards (curated.AWARD_DISPLAY_ORDER):
     1. The Game Awards, Game of the Year: won
     2. BAFTA Games Awards, Best Game: won
     3. D.I.C.E. Awards, Game of the Year: won (curated/awards.csv has no D.I.C.E. rows today)
     4. Game Developers Choice Awards, Game of the Year: won
     5. The Steam Awards, Game of the Year: won
     6. any other verified win
     7. verified nominations, no win
     8. no award recorded
   Every game in a group ranks above every game in a later group, whatever its reviews.
   Production labels (AAA, AA, Indie) play no part in the ranking.
2. More distinct verified wins, then more distinct verified nominations. A result recorded
   twice (same organization, year, category and result) counts once, a nomination never
   counts as a win, and a nomination for an award the game also won is not counted again.
   Only verified results mapped to the game's Steam app count. An award to the original
   release of a remaster or edition counts for the edition Steam sells, as everywhere on
   the site, and stays marked "for the original release".
3. Steam reception, the "Best player reception" order: games with at least
   RELIABLE_REVIEW_COUNT reviews, then games with fewer, then games without a usable review
   summary; within the first two, the higher positive share first, then more reviews.
4. Library sort name (title A-Z, ignoring case, accents, punctuation and a leading "The",
   "A" or "An"), then Steam app id, so every rank is distinct and repeatable.

Award coverage is incomplete (selected organizations, categories and years), so "No award
recorded" does not mean that a game never received an award.

A missing, zero or inconsistent review summary is never read as zero reviews or 0% positive.
A game with a verified award result keeps its award-based place without one: it follows the
games with the same group and award counts that have review data. A game with neither an
award nor usable reviews is "Unranked": it gets no number and is listed after the ranked
games. Games outside the window (released earlier, not released yet, or without an exact
release date on Steam) are not part of the ranking at all.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable

from .curated import AWARD_DISPLAY_ORDER, NOMINATIONS, OTHER_WINS, Award, CuratedData, award_display_group, \
    award_identity
from .prepare import parse_release_date

WEBSITE_FULL_YEARS = 5  # the website's window: this many full calendar years before this year (the "five-year ranking")
EXPORT_WINDOW_YEARS = 4  # the Power BI export's rolling window: this many years up to the export day

# A review summary needs this many Steam reviews before its positive share leads the "Best player reception"
# order (the ranking's tie-breaker, and a sort order and collection rule in catalog.py). With fewer reviews a
# handful of votes can swing the share a lot.
RELIABLE_REVIEW_COUNT = 500

# Award groups, best first. curated.award_display_group counts the same groups from 0, without "no award".
NO_AWARD_GROUP = NOMINATIONS + 2
AWARD_GROUP_NAMES = {
    **{place + 1: f"{organization} — {category} winner"
       for place, (organization, category) in enumerate(AWARD_DISPLAY_ORDER)},
    OTHER_WINS + 1: "Other verified award winner",
    NOMINATIONS + 1: "Verified nominations, no win",
    NO_AWARD_GROUP: "No award recorded",
}

# Steam reception in the tie-breaker, best first.
RECEPTION_RELIABLE, RECEPTION_FEW_REVIEWS, RECEPTION_UNAVAILABLE = 0, 1, 2

UNRANKED_NOTE = "No award recorded and no usable Steam review summary."


@dataclass(frozen=True)
class RankingWindow:
    """The release dates a ranking covers, `start` to `end` (both included), and the ranking's name."""

    start: date
    end: date
    name: str  # as in "the five-year ranking": "five-year" on the website, "four-year" in the Power BI export

    @classmethod
    def for_website(cls, today: date) -> RankingWindow:
        """The five full calendar years before `today`'s year, plus that year up to and including `today`.

        20 Nov 2026 -> 1 Jan 2021 to 20 Nov 2026. The start moves forward only on 1 January.
        """
        return cls(start=date(today.year - WEBSITE_FULL_YEARS, 1, 1), end=today, name="five-year")

    @classmethod
    def ending_on(cls, today: date) -> RankingWindow:
        """The Power BI export's rolling window, the four years up to and including `today`.

        9 Nov 2026 -> 9 Nov 2022 to 9 Nov 2026. report_export.py uses it; the website uses for_website.
        """
        try:
            start = today.replace(year=today.year - EXPORT_WINDOW_YEARS)
        except ValueError:  # 29 February in a year whose fourth-previous year has none (2104 -> 2100)
            start = today.replace(year=today.year - EXPORT_WINDOW_YEARS, day=28)
        return cls(start=start, end=today, name="four-year")


@dataclass(frozen=True)
class GameFacts:
    """What the ranking needs to know about one game."""

    appid: int
    sort_name: str
    positive: int | None  # positive Steam reviews, as stored by the last review sync
    total: int | None  # all Steam reviews, from the same summary
    release_date: str | None  # Steam's release-date text, such as "Aug 3, 2023"
    coming_soon: bool = False  # Steam's own "not released yet" flag
    awards: tuple[Award, ...] = ()  # verified award results mapped to this Steam app (curated.awards_for)


@dataclass(frozen=True)
class AwardSummary:
    """The verified award results that place a game in the ranking."""

    group: int  # 1 (The Game Awards' Game of the Year won) to NO_AWARD_GROUP; see AWARD_GROUP_NAMES
    wins: int = 0  # distinct verified wins
    nominations: int = 0  # distinct verified nominations that did not win
    for_original_release: bool = False  # every result of the group went to the original release of this edition

    @property
    def group_name(self) -> str:
        return AWARD_GROUP_NAMES[self.group]


@dataclass(frozen=True)
class Placement:
    """Where one game stands in the ranking, and why."""

    awards: AwardSummary
    released_on: date | None  # the exact release date, when Steam's text gives one
    upcoming: bool  # not released yet: Steam says "coming soon", or the exact date is after today
    window_note: str | None  # why the game is outside the ranking's window; None when it is inside
    review_note: str | None  # why its Steam review summary cannot be used; None when it can
    ranking_name: str  # the window's name, such as "five-year"
    rank: int | None = None  # 1 = first; None outside the window or when the game is Unranked

    @property
    def in_window(self) -> bool:
        """True when the game was released inside the ranking's window."""
        return self.window_note is None

    @property
    def group_name(self) -> str:
        return self.awards.group_name

    @property
    def status(self) -> str:
        """"Ranked", "Unranked" (in the window, with no award and no usable reviews) or "Not in the five-year
        ranking" (the window's name: "four-year" in the Power BI export)."""
        if not self.in_window:
            return f"Not in the {self.ranking_name} ranking"
        return "Ranked" if self.rank is not None else "Unranked"

    @property
    def note(self) -> str | None:
        """Why the game has no rank, or None when it has one."""
        if self.window_note:
            return self.window_note
        return f"{UNRANKED_NOTE} {self.review_note}" if self.rank is None else None


def review_problem(positive: int | None, total: int | None) -> str | None:
    """Explain why a stored review summary cannot be used, or return None when it can.

    A stored total of zero is not used either: the sync saves a missing Steam
    summary as zeros, so a zero cannot be told apart from "no answer".
    """
    if positive is None or total is None:
        return "No Steam review summary has been saved for this game yet."
    if total == 0:
        return "This game has no Steam reviews yet."
    if positive < 0 or total < 0 or positive > total:
        return "The stored Steam review counts are inconsistent, so they are not used."
    return None


def reception_key(positive: int | None, total: int | None) -> tuple[int, float, int]:
    """The "Best player reception" order, best first: (reception group, -positive share, -total reviews).

    Games with at least RELIABLE_REVIEW_COUNT reviews come first, then games with fewer reviews, each
    by the higher positive share, then more reviews. Games without a usable summary come last.
    """
    if review_problem(positive, total) is not None:
        return RECEPTION_UNAVAILABLE, 0.0, 0
    group = RECEPTION_RELIABLE if total >= RELIABLE_REVIEW_COUNT else RECEPTION_FEW_REVIEWS
    return group, -positive / total, -total


def award_summary(appid: int, awards: Iterable[Award]) -> AwardSummary:
    """Summarize the verified results mapped to Steam app `appid`; other rows in `awards` are ignored.

    A result is identified by its organization, year and category (curated.award_identity), so a row
    recorded twice counts once, and a nomination for an award the game also won is not counted again.
    """
    counted = [award for award in awards if award.review_status == "verified" and award.appid == appid]
    if not counted:
        return AwardSummary(group=NO_AWARD_GROUP)
    won = {award_identity(award) for award in counted if award.result == "winner"}
    nominated = {award_identity(award) for award in counted if award.result == "nominee"} - won
    group = min(award_display_group(award) for award in counted) + 1
    group_results = [award for award in counted if award_display_group(award) + 1 == group]
    return AwardSummary(group=group, wins=len(won), nominations=len(nominated),
                        for_original_release=all(award.mapping == "edition" for award in group_results))


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
        return released_on, f"Released before the {window.name} window."
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
        summary = award_summary(game.appid, game.awards)
        review_note = review_problem(game.positive, game.total)
        placements[game.appid] = Placement(
            awards=summary,
            released_on=released_on,
            upcoming=game.coming_soon or (released_on is not None and released_on > window.end),
            window_note=window_note,
            review_note=review_note,
            ranking_name=window.name,
        )
        # A game needs a verified award or usable reviews to be placed; with neither it is Unranked.
        if window_note is None and (summary.group != NO_AWARD_GROUP or review_note is None):
            rankable.append(game)

    def rank_order(game: GameFacts) -> tuple:
        """Award group, more wins, more nominations, Steam reception, then title A-Z and app id."""
        summary = placements[game.appid].awards
        return (summary.group, -summary.wins, -summary.nominations, *reception_key(game.positive, game.total),
                game.sort_name, game.appid)

    rankable.sort(key=rank_order)
    for position, game in enumerate(rankable, start=1):
        placements[game.appid] = replace(placements[game.appid], rank=position)
    return placements


def place_catalog(rows: Iterable, curated: CuratedData, window: RankingWindow) -> dict[int, Placement]:
    """Place stored catalog rows (as returned by store.matching_games) with their verified awards."""
    return place_games(
        (
            GameFacts(
                appid=row["appid"],
                sort_name=row["sort_name"],
                positive=row["review_positive"],
                total=row["review_total"],
                release_date=row["release_date"],
                coming_soon=bool(row["coming_soon"]),
                awards=tuple(curated.awards_for(row["appid"])),
            )
            for row in rows
        ),
        window,
    )
