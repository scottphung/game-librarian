"""Catalog views, collections and sort orders: which games a page lists, and in what order.

Everything here is pure: it works on the facts in `CatalogGame` (built from the stored
Steam data, the verified curated data and the four-year ranking), with no SQL and no
network, so every rule can be tested on its own.

Views. "Top-ranked" lists the games in the four-year ranking window; "All games" lists
the whole catalog. Collections are further views with a documented membership rule.
The four-year rank shown on a card never depends on the view, the filters or the sort:
it is calculated once over the whole catalog (see ranking.py).

Review counts are Steam's (all languages, Steam purchasers). A count says roughly how
many people played and reviewed a game, so it is a rough measure of visibility, not of
sales. A game without a usable review summary is never treated as having zero reviews:
review-based collections leave it out, and review-based sorts list it last.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable

from . import ranking
from .curated import Award, CuratedData
from .prepare import percent_positive

# A review summary needs this many Steam reviews before its positive share is used to lead the
# "Best player reception" order or to qualify a game for a review-based collection. With fewer
# reviews a handful of votes can swing the share a lot.
RELIABLE_REVIEW_COUNT = 500
HIGHLY_RATED_MIN_PERCENT = 90  # "Highly rated indies" and "Hidden gems": at least 90% positive
HIDDEN_GEM_MAX_REVIEWS = 20_000  # "Hidden gems": at most this many Steam reviews (a rough visibility cap)
RECENT_DAYS = 365  # "Recent releases": released in the last 365 days, today included

LABEL_ORDER = ("AAA", "AA", "Indie")


@dataclass(frozen=True)
class CatalogGame:
    """What views, collections and sorts need to know about one catalog game."""

    appid: int
    sort_name: str
    labels: tuple[str, ...]  # verified production labels, in LABEL_ORDER
    awards: tuple[Award, ...]  # verified awards mapped to this Steam app
    positive: int | None  # positive Steam reviews; None when the review summary is not usable
    total: int | None  # all Steam reviews, from the same summary; None when not usable
    released_on: date | None  # exact Steam release date, when Steam gives one
    upcoming: bool  # not released yet: coming soon, or an exact date after today
    in_window: bool  # released inside the four-year ranking window
    rank: int | None  # four-year rank, None when outside the window or Unranked
    modes: tuple[str, ...] = ()  # Steam game modes, such as "Single-player"

    @property
    def percent(self) -> int | None:
        """Positive share as Steam shows it (rounded down), or None without usable reviews."""
        return percent_positive(self.positive, self.total) if self.total else None

    @property
    def share(self) -> float | None:
        """Exact positive share, or None without usable reviews."""
        return self.positive / self.total if self.total else None


def catalog_games(rows: Iterable, curated: CuratedData, placements: dict[int, ranking.Placement]) -> list[CatalogGame]:
    """Build CatalogGame facts for stored rows (as returned by store.matching_games), keeping their order."""
    games = []
    for row in rows:
        appid = row["appid"]
        place = placements[appid]
        usable = ranking.unranked_reason(row["review_positive"], row["review_total"]) is None
        labels = curated.labels_for(appid)
        games.append(CatalogGame(
            appid=appid,
            sort_name=row["sort_name"],
            labels=tuple(label for label in LABEL_ORDER if label in labels),
            awards=tuple(curated.awards_for(appid)),
            positive=row["review_positive"] if usable else None,
            total=row["review_total"] if usable else None,
            released_on=place.released_on,
            upcoming=place.upcoming,
            in_window=place.in_window,
            rank=place.rank,
            modes=tuple(json.loads(row["modes"] or "[]")),
        ))
    return games


# ---------------------------------------------------------------------- views and collections


@dataclass(frozen=True)
class CatalogView:
    """A set of catalog games: its URL value, link label, heading, rule and default order."""

    key: str
    label: str
    heading: str
    default_sort: str
    includes: Callable[[CatalogGame, date], bool]
    description: str = ""  # the membership rule, shown above the results of a collection
    is_collection: bool = False


def _has_reliable_reviews(game: CatalogGame) -> bool:
    return game.total is not None and game.total >= RELIABLE_REVIEW_COUNT


def is_goty_winner(game: CatalogGame, today: date | None = None) -> bool:
    """A verified win in an organization's Game of the Year or Best Game category."""
    return any(award.is_top_award and award.result == "winner" for award in game.awards)


def is_highly_rated_indie(game: CatalogGame, today: date | None = None) -> bool:
    """A verified Indie label, at least 500 Steam reviews and at least 90% of them positive."""
    return "Indie" in game.labels and _has_reliable_reviews(game) and game.percent >= HIGHLY_RATED_MIN_PERCENT


def is_big_budget(game: CatalogGame, today: date | None = None) -> bool:
    """A verified AAA or AA label."""
    return "AAA" in game.labels or "AA" in game.labels


def is_hidden_gem(game: CatalogGame, today: date | None = None) -> bool:
    """At least 90% positive, with 500 to 20,000 Steam reviews: well liked by fewer players."""
    return (_has_reliable_reviews(game) and game.total <= HIDDEN_GEM_MAX_REVIEWS
            and game.percent >= HIGHLY_RATED_MIN_PERCENT)


def recent_window(today: date) -> tuple[date, date]:
    """The release dates of "Recent releases": the last 365 days, today included."""
    return today - timedelta(days=RECENT_DAYS - 1), today


def is_recent_release(game: CatalogGame, today: date) -> bool:
    """An exact Steam release date within the last 365 days, today included."""
    start, end = recent_window(today)
    return game.released_on is not None and start <= game.released_on <= end


TOP_VIEW, ALL_VIEW = "top", "all"
VIEWS: dict[str, CatalogView] = {
    view.key: view
    for view in (
        # Only the games released in the four-year window, in rank order.
        CatalogView(TOP_VIEW, "Top-ranked · last four years", "Top-ranked games", "rank",
                    lambda game, today: game.in_window),
        # The whole catalog. A–Z by default, so older games are never listed as if they were ranked.
        CatalogView(ALL_VIEW, "All games", "All games", "name", lambda game, today: True),
        CatalogView(
            "goty", "Game of the Year winners", "Game of the Year winners", "newest", is_goty_winner,
            "Games with a verified win in an organization's top category: Game of the Year at The Game Awards, "
            "the Game Developers Choice Awards and The Steam Awards, and Best Game at the BAFTA Games Awards. "
            "Each card names the organization and the award year.", True),
        CatalogView(
            "awards", "Award winners & nominees", "Award winners and nominees", "newest",
            lambda game, today: bool(game.awards),
            "Games with at least one verified award result, win or nomination. Use the Awards filters to choose "
            "an organization, a category or an award year, or winners only.", True),
        CatalogView(
            "indies", "Highly rated indies", "Highly rated indies", "reception", is_highly_rated_indie,
            f"Games with a verified Indie label, at least {RELIABLE_REVIEW_COUNT:,} Steam reviews and at least "
            f"{HIGHLY_RATED_MIN_PERCENT}% of them positive. Games without a verified label are not included, "
            "whatever their size.", True),
        CatalogView(
            "big-budget", "Big-budget games", "Big-budget games", "most-positive", is_big_budget,
            "Games with a verified AAA or AA label (evidence such as a reported budget or team size). Most catalog "
            "games have no verified label yet, so this list is short.", True),
        CatalogView(
            "hidden-gems", "Hidden gems", "Hidden gems", "reception", is_hidden_gem,
            f"Games with at least {HIGHLY_RATED_MIN_PERCENT}% positive Steam reviews and between "
            f"{RELIABLE_REVIEW_COUNT:,} and {HIDDEN_GEM_MAX_REVIEWS:,} reviews. The review count is a rough measure "
            "of how many people found a game, not of its sales.", True),
        CatalogView(
            "recent", "Recent releases", "Recent releases", "newest", is_recent_release,
            f"Games whose exact Steam release date is in the last {RECENT_DAYS} days, today included. Steam dates "
            "a game that left Early Access by its full release.", True),
    )
}
DEFAULT_VIEW = TOP_VIEW
COLLECTION_KEYS = tuple(key for key, view in VIEWS.items() if view.is_collection)


# ---------------------------------------------------------------------- sorting


@dataclass(frozen=True)
class SortOption:
    """One way to order the catalog: its URL value and menu label."""

    key: str
    label: str


SORT_OPTIONS = (
    SortOption("rank", "Production tier, then positive reviews"),
    SortOption("most-positive", "Most positive Steam reviews"),
    SortOption("reception", "Best player reception"),
    SortOption("newest", "Newest release"),
    SortOption("name", "Name (A–Z)"),
)
SORTS = {option.key: option for option in SORT_OPTIONS}
# Sort values used before version 0.4, so older links keep working.
SORT_ALIASES = {"reviews": "most-positive", "positive": "reception"}


def _library_order(game: CatalogGame) -> tuple:
    return (game.sort_name, game.appid)


def sort_games(games: list[CatalogGame], sort: str, today: date) -> list[CatalogGame]:
    """Order games. Every order is deterministic: ties go to the title A–Z, then the Steam app id.

    - rank: games in the four-year ranking first, in rank order; the others follow A–Z.
    - most-positive: the most positive Steam reviews first (the count, not the share).
    - reception: games with at least RELIABLE_REVIEW_COUNT reviews first, highest positive share first
      (ties: more reviews); then games with fewer reviews, in the same order.
    - newest: exact release dates up to today, newest first; games not released yet and games
      without an exact release date follow A–Z.
    - name: library order (title A–Z ignoring case, accents, punctuation and a leading article).
    Games without usable review data always come after games with it in the review-based orders.
    """
    if sort == "rank":
        def key(game):
            return (game.rank is None, game.rank or 0, *_library_order(game))
    elif sort == "most-positive":
        def key(game):
            return (game.positive is None, -(game.positive or 0), *_library_order(game))
    elif sort == "reception":
        def key(game):
            group = 2 if game.total is None else (0 if game.total >= RELIABLE_REVIEW_COUNT else 1)
            return (group, -(game.share or 0), -(game.total or 0), *_library_order(game))
    elif sort == "newest":
        def key(game):
            released = game.released_on is not None and not game.upcoming and game.released_on <= today
            return (not released, -game.released_on.toordinal() if released else 0, *_library_order(game))
    else:
        key = _library_order
    return sorted(games, key=key)
