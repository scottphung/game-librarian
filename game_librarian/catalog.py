"""Catalog views, collections and sort orders: which games a page lists, and in what order.

Everything here is pure: it works on the facts in `CatalogGame` (built from the stored
Steam data, the verified curated data and the five-year ranking), with no SQL and no
network, so every rule can be tested on its own.

Views. "Top-ranked" lists the games in the five-year ranking window; "All games" lists
the whole catalog. Collections are further views with a documented membership rule.
The five-year rank shown on a card never depends on the view, the filters or the sort:
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
from .publishers import PublisherIndex
from .ranking import RELIABLE_REVIEW_COUNT  # reviews needed before a positive share leads or qualifies (ranking.py)

HIGHLY_RATED_MIN_PERCENT = 90  # "Highly rated indies" and "Hidden gems": at least 90% positive
HIDDEN_GEM_MAX_REVIEWS = 20_000  # "Hidden gems": at most this many Steam reviews (a rough visibility cap)
RECENT_DAYS = 365  # "Recent releases": released in the last 365 days, today included

LABEL_ORDER = ("AAA", "AA", "Indie")
# The filter choice (and report value) for a game without a verified AAA, AA or Indie label; cards and details
# show no badge for it. Before 0.8 it was called "Unclassified": links and exports that use that word still work.
NO_VERIFIED_LABEL, LEGACY_NO_LABEL = "No verified label", "Unclassified"

# Steam's review summary labels, best first, exactly as Steam returns them with the review totals.
# Steam assigns them; Game Librarian only reads them. Other texts ("No user reviews", "3 user reviews")
# are not reception labels and match no filter.
STEAM_REVIEW_LABELS = ("Overwhelmingly Positive", "Very Positive", "Positive", "Mostly Positive", "Mixed",
                       "Mostly Negative", "Negative", "Very Negative", "Overwhelmingly Negative")
# "Strong player reception" wherever the site uses the phrase (the Trending sections).
STRONG_RECEPTION_LABELS = ("Overwhelmingly Positive", "Very Positive")


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
    in_window: bool  # released inside the five-year ranking window
    rank: int | None  # five-year rank, None when outside the window or Unranked
    modes: tuple[str, ...] = ()  # Steam game modes, such as "Single-player"
    publishers: frozenset[str] = frozenset()  # publisher keys, after grouping (publishers.py)
    review_label: str | None = None  # Steam's review summary label (STEAM_REVIEW_LABELS), when usable
    players: int | None = None  # players in game on Steam at the newest snapshot; None when unknown
    players_at: str | None = None  # when that snapshot was taken, UTC ISO 8601

    @property
    def percent(self) -> int | None:
        """Positive share as Steam shows it (rounded down), or None without usable reviews."""
        return percent_positive(self.positive, self.total) if self.total else None

    @property
    def share(self) -> float | None:
        """Exact positive share, or None without usable reviews."""
        return self.positive / self.total if self.total else None


def catalog_games(rows: Iterable, curated: CuratedData, placements: dict[int, ranking.Placement], *,
                  publishers: PublisherIndex | None = None, players: dict | None = None) -> list[CatalogGame]:
    """Build CatalogGame facts for stored rows (as returned by store.matching_games), keeping their order.

    `publishers` gives each game's grouped publishers; `players` holds the newest player snapshot
    rows (store.latest_player_counts). Without them those facts stay empty.
    """
    players = players or {}
    games = []
    for row in rows:
        appid = row["appid"]
        place = placements[appid]
        usable = ranking.review_problem(row["review_positive"], row["review_total"]) is None
        labels = curated.labels_for(appid)
        snapshot = players.get(appid)
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
            publishers=publishers.keys_for(appid) if publishers else frozenset(),
            review_label=row["review_score_desc"] if usable and row["review_score_desc"] in STEAM_REVIEW_LABELS
            else None,
            players=snapshot["players"] if snapshot else None,
            players_at=snapshot["captured_at"] if snapshot and snapshot["players"] is not None else None,
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
        # Only the games released in the five-year window, in rank order. The page fills in the window's first year.
        CatalogView(TOP_VIEW, "Top-ranked · since {start_year}", "Top-ranked games", "rank",
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
    SortOption("rank", "Five-year rank (awards, then reception)"),
    SortOption("most-positive", "Most positive Steam reviews"),
    SortOption("reception", "Best player reception"),
    SortOption("players", "Most players on Steam (latest snapshot)"),
    SortOption("newest", "Newest release"),
    SortOption("name", "Name (A–Z)"),
)
SORTS = {option.key: option for option in SORT_OPTIONS}
# Sort values used before version 0.4, so older links keep working.
SORT_ALIASES = {"reviews": "most-positive", "positive": "reception"}


def _library_order(game: CatalogGame) -> tuple:
    return (game.sort_name, game.appid)


def reception_order(game: CatalogGame) -> tuple:
    """The "Best player reception" sort key, shared with the Trending sections and the five-year ranking.

    Games with at least RELIABLE_REVIEW_COUNT reviews lead, highest positive share first (ties:
    more reviews, then A–Z); games with fewer reviews follow, then games without review data.
    """
    return (*ranking.reception_key(game.positive, game.total), *_library_order(game))


def sort_games(games: list[CatalogGame], sort: str, today: date) -> list[CatalogGame]:
    """Order games. Every order is deterministic: ties go to the title A–Z, then the Steam app id.

    - rank: games in the five-year ranking first, in rank order (award group, wins, nominations,
      then Steam reception; see ranking.py); the others follow A–Z.
    - most-positive: the most positive Steam reviews first (the count, not the share).
    - reception: games with at least RELIABLE_REVIEW_COUNT reviews first, highest positive share first
      (ties: more reviews); then games with fewer reviews, in the same order.
    - players: the most players in game at the newest player snapshot first; games without a
      count (never collected, or Steam reported none) follow A–Z.
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
            return reception_order(game)
    elif sort == "players":
        def key(game):
            return (game.players is None, -(game.players or 0), *_library_order(game))
    elif sort == "newest":
        def key(game):
            released = game.released_on is not None and not game.upcoming and game.released_on <= today
            return (not released, -game.released_on.toordinal() if released else 0, *_library_order(game))
    else:
        key = _library_order
    return sorted(games, key=key)
