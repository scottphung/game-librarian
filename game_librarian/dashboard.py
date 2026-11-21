"""The website's dashboard: charts of the Trending lists, and the Power BI report's pages as server-rendered HTML.

Player reception, Awards and About the data aggregate the tables that `export-report` writes
for Power BI (report_export.collect_tables) with the report's measure definitions
(reporting/README.md), so for the same catalog and day both show the same numbers. Tags and
awards filter games through their own rows, so a game with several tags or awards is counted once.

- Trending games (/dashboard, the first and default page; /dashboard/trending redirects there):
  three report pages about the Trending page's games (TRENDING_REPORTS, chosen with ?report=), built from
  the same stored lists, rules and timestamps (web._trending_lists and web._steam_activity):
  - Sales & reception: Gamalytic's estimated Steam copies sold as a doughnut (sales_chart, sales_ring), drawn
    only for a browser on the computer that runs the site because Gamalytic's terms forbid sharing its data
    (gamalytic.py); then the five-year window's strong-reception games as one scatterplot, this year's
    releases highlighted, where a selected game is compared with the median of its peers (reception_chart,
    peer_benchmark).
  - Upcoming releases: the tracked upcoming games on a release radar by how soon each is due on each
    platform family, with a selected-game panel and time windows (release_radar; only exact release days
    are placed), beside what a player would miss by browsing only Steam's most-wishlisted list
    (list_comparison.py). Places stay places: never drawn or summed as amounts.
  - Momentum: which games account for the change in Steam review count between two comparable snapshots
    (review_waterfall), and one game's Steam player snapshots (player_line); without enough history, one
    note on what the snapshots cover (snapshot_coverage, launch_weeks_coverage).
  Each measure keeps its own unit and none is added to another; missing data is never zero, and a chart
  without enough data says so in one line instead of drawing an empty frame. Definitions, sources and
  observation times fold under each page's "Sources and methodology". This page reads stored data
  directly; it is not in the Power BI export.
- Player reception (/dashboard/reception; a /dashboard link with its filters, made before 0.10,
  redirects there): review totals and both positive rates, review volume against positive share,
  the rates by production label, the five-year ranking and games per positive-share band.
  Filters: tag, production label, award status.
- Awards (/dashboard/awards): verified award entries by year and by award, how they match
  Steam, and every entry with its source. Filters: year, award, result, match to Steam.
- About the data (/dashboard/about): definitions, coverage and the loaded data.

The functions here only compute: positions are percentages of a chart's plot area, and the
templates (dashboard_*.html) draw them with plain HTML and CSS (the radar's grid and the player line as
inline SVG), without a chart library. JavaScript (app.js) only adds conveniences, each with a plain
link or a table behind it: the selected game beside the radar and in the comparison's groups, the radar's
time windows, the scatterplot's peers and view, the doughnut's link to it, chart tooltips, arrow keys
between a scatterplot's dots, and switching the player line. Every chart prints its values or lists
them in a table.
"""

from __future__ import annotations

import math
import statistics
from calendar import monthrange
from collections import Counter
from datetime import date, datetime, time, timedelta
from fractions import Fraction
from typing import Iterable, Mapping

from . import gamalytic, list_comparison, ranking, trending, upcoming
from .catalog import LEGACY_NO_LABEL, NO_VERIFIED_LABEL
from .prepare import FREE, PRICED
from .report_export import (
    IN_CATALOG, LABEL_JOINER, LABEL_ORDER, NO_LISTED_AWARD, NO_REVIEW_DATA_BAND, NOMINEE, NOT_MAPPED,
    ON_STEAM_NOT_IN_CATALOG, POSITIVE_BANDS, PROMOTED_SOURCE_NAME, WINNER, ReportTables,
)

AWARD_STATUSES = (WINNER, NOMINEE, NO_LISTED_AWARD)
RESULTS = (WINNER, NOMINEE)
MATCH_STATUSES = (IN_CATALOG, ON_STEAM_NOT_IN_CATALOG, NOT_MAPPED)  # also the series order of the match chart
MAX_AXIS_INTERVALS = 5  # a count axis has at most this many steps above zero


# ---------------------------------------------------------------------- formatting


def compact_number(value: float) -> str:
    """A short number for cards and axes: 950 -> '950', 1,284 -> '1.3K', 47,515,291 -> '47.5M'."""
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if round(abs(value) / divisor, 1) >= 1:
            return f"{value / divisor:.1f}".removesuffix(".0") + suffix
    return f"{value:,.0f}"


def format_rate(value: float | None, decimals: int = 1) -> str:
    """A share as a percentage: 0.8942 -> '89.4%'. None (nothing to measure) -> 'No data'."""
    return "No data" if value is None else f"{value * 100:.{decimals}f}%"


def _percent_of(part: float, whole: float) -> float:
    """`part` as a percentage of `whole`, rounded for a CSS length; 0 when `whole` is 0."""
    return round(part / whole * 100, 2) if whole else 0.0


def axis_ticks(largest: int) -> list[int]:
    """Round tick values from 0 to the first round number at or above `largest`: 37 -> [0, 10, 20, 30, 40].

    Steps are 1, 2 or 5 times a power of ten, with at most MAX_AXIS_INTERVALS steps.
    """
    magnitude = 1
    while True:
        for factor in (1, 2, 5):
            step = factor * magnitude
            intervals = math.ceil(largest / step)
            if intervals <= MAX_AXIS_INTERVALS:
                return list(range(0, max(1, intervals) * step + 1, step))
        magnitude *= 10


def change_ticks(lowest: int, highest: int) -> list[int]:
    """Round ticks from below `lowest` to above `highest`, always through zero, for an axis of signed changes:
    (-1,200, 9,000) -> [-5,000, 0, 5,000, 10,000]. Steps as in axis_ticks, at most MAX_AXIS_INTERVALS in all."""
    lowest, highest = min(lowest, 0), max(highest, 0)
    magnitude = 1
    while True:
        for factor in (1, 2, 5):
            step = factor * magnitude
            below, above = math.ceil(-lowest / step), math.ceil(highest / step)
            if below + above <= MAX_AXIS_INTERVALS:
                above = above if (above or below) else 1  # all zero: still one step, so the axis has a length
                return list(range(-below * step, above * step + 1, step))
        magnitude *= 10


def log_decades(smallest: float, largest: float) -> tuple[int, int]:
    """The powers of ten a log axis runs between, at least one decade apart: (1,796, 884,256) -> (3, 6), 1K – 1M."""
    low = math.floor(math.log10(smallest))
    return low, max(math.ceil(math.log10(largest)), low + 1)


def _selected(params: Mapping[str, str], name: str, allowed: Iterable[str]) -> str:
    """The query value `name` when it is one of `allowed`, else "" (no filter): unknown values are ignored."""
    value = (params.get(name) or "").strip()
    return value if value in set(allowed) else ""


# ---------------------------------------------------------------------- player reception


def reception_measures(games: list[dict]) -> dict:
    """The Player reception cards for `games`, defined as in the Power BI model.

    The review-weighted rate pools every review (big games dominate); the average rate gives
    every game with review data the same weight. A rate is None when no game has review data.
    """
    reviewed = [game for game in games if game["has_review_data"]]
    total = sum(game["total_reviews"] for game in reviewed)
    positive = sum(game["positive_reviews"] for game in reviewed)
    return {
        "games": len(games),
        "games_with_review_data": len(reviewed),
        "steam_reviews": total,
        "weighted_rate": positive / total if total else None,
        "average_rate": statistics.fmean(game["positive_share"] for game in reviewed) if reviewed else None,
    }


def label_sort_key(label: str) -> tuple[int, ...]:
    """Production labels in label order: AAA, AA, Indie, each combination after its first label, "No verified label" last."""
    if label == NO_VERIFIED_LABEL:
        return (len(LABEL_ORDER),)
    return tuple(LABEL_ORDER.index(part) for part in label.split(LABEL_JOINER))


def rates_by_label(games: list[dict]) -> list[dict]:
    """Both positive rates for each production label that has games with review data."""
    groups: dict[str, list[dict]] = {}
    for game in games:
        if game["has_review_data"]:
            groups.setdefault(game["production_label"], []).append(game)
    rows = []
    for label in sorted(groups, key=label_sort_key):
        measures = reception_measures(groups[label])
        rows.append({"label": label, "games": len(groups[label]),
                     "weighted_rate": measures["weighted_rate"], "average_rate": measures["average_rate"]})
    return rows


def _rank_text(game: dict) -> str:
    rank = game["four_year_rank"]
    return f"#{rank} in the five-year ranking" if rank is not None else game["rank_status"].lower()


def scatter_chart(games: list[dict]) -> dict | None:
    """Dot positions for the games with review data, or None when there are none.

    x is the total number of reviews on a log scale, because totals run from tens to millions;
    y is the positive share, on an axis that starts at the round value at or below the lowest share
    (steps of 10 points, or 20 when a share is under 50%).
    """
    reviewed = [game for game in games if game["has_review_data"]]
    if not reviewed:
        return None
    totals = [game["total_reviews"] for game in reviewed]
    low, high = log_decades(min(totals), max(totals))
    lowest_share = min(game["positive_share"] for game in reviewed) * 100
    step = 10 if lowest_share >= 50 else 20
    bottom = min(100 - step, math.floor(lowest_share / step) * step)
    points = [
        {
            "appid": game["appid"],
            "x": _percent_of(math.log10(game["total_reviews"]) - low, high - low),
            "y": _percent_of(game["positive_share"] * 100 - bottom, 100 - bottom),
            "title": f"{game['title']}: {game['total_reviews']:,} reviews, {game['steam_percent_positive']}% positive, "
                     f"{_rank_text(game)}",
            "game": game,
        }
        for game in reviewed
    ]
    return {
        "points": points,
        "x_ticks": [{"position": _percent_of(power - low, high - low), "label": compact_number(10 ** power)}
                    for power in range(low, high + 1)],
        "y_ticks": [{"position": _percent_of(value - bottom, 100 - bottom), "label": f"{value}%"}
                    for value in range(bottom, 101, step)],
    }


def band_rows(games: list[dict]) -> list[dict]:
    """Games per band of Steam's positive percentage, best band first and the no-data band last."""
    counts = Counter(game["positive_band"] for game in games)
    names = [label for _, label in POSITIVE_BANDS] + [NO_REVIEW_DATA_BAND]
    largest = max(counts.values(), default=0)
    return [
        {"band": name, "games": counts[name], "width": _percent_of(counts[name], largest),
         "tone": "none" if name == NO_REVIEW_DATA_BAND else str(position)}
        for position, name in enumerate(names, start=1)
    ]


def reception_page(tables: ReportTables, params: Mapping[str, str]) -> dict:
    """Everything the Player reception page shows, for the filters in `params`."""
    labels = sorted({game["production_label"] for game in tables.games}, key=label_sort_key)
    wanted_label = (params.get("label") or "").strip()
    if wanted_label == LEGACY_NO_LABEL:  # a link made before 0.8
        wanted_label = NO_VERIFIED_LABEL
    filters = {
        "tag": _selected(params, "tag", (row["tag"] for row in tables.tags)),
        "label": _selected({"label": wanted_label}, "label", labels),
        "award_status": _selected(params, "award_status", AWARD_STATUSES),
    }
    tagged = {row["appid"] for row in tables.game_tags if row["tag"] == filters["tag"]}
    games = [
        game for game in tables.games
        if (not filters["tag"] or game["appid"] in tagged)
        and (not filters["label"] or game["production_label"] == filters["label"])
        and (not filters["award_status"] or game["award_status"] == filters["award_status"])
    ]
    return {
        "filters": filters,
        "filtered": any(filters.values()),
        "tag_options": sorted(tables.tags, key=lambda row: row["tag"].casefold()),
        "label_options": labels,
        "award_status_options": AWARD_STATUSES,
        "catalog_games": len(tables.games),
        "measures": reception_measures(games),
        "scatter": scatter_chart(games),
        "by_label": rates_by_label(games),
        # Ranks come from the whole catalog, so a filter hides games but never renumbers them.
        "ranking": sorted((game for game in games if game["four_year_rank"] is not None),
                          key=lambda game: game["four_year_rank"]),
        "reliable_reviews": ranking.RELIABLE_REVIEW_COUNT,
        "bands": band_rows(games),
    }


# ---------------------------------------------------------------------- awards


def award_measures(entries: list[dict]) -> dict:
    """The Awards cards for `entries`, defined as in the Power BI model. A winner is listed once, as Winner."""
    winners = sum(entry["is_winner"] for entry in entries)
    in_catalog = sum(entry["catalog_status"] == IN_CATALOG for entry in entries)
    return {
        "entries": len(entries),
        "winners": winners,
        "nominees": len(entries) - winners,
        "awarded_games": len({entry["catalog_appid"] for entry in entries if entry["catalog_appid"] is not None}),
        "coverage": in_catalog / len(entries) if entries else None,
    }


def year_columns(entries: list[dict]) -> dict | None:
    """One column per award year (oldest first): winners at the bottom, nominees stacked on top."""
    if not entries:
        return None
    counts: dict[int, Counter] = {}
    for entry in entries:
        counts.setdefault(entry["year"], Counter())[entry["result"]] += 1
    ticks = axis_ticks(max(sum(count.values()) for count in counts.values()))
    top = ticks[-1]
    columns = []
    for year in sorted(counts):
        winners, nominees = counts[year][WINNER], counts[year][NOMINEE]
        # Drawn top to bottom without empty parts; the first part carries the column's rounded end.
        parts = [{"name": name, "count": count, "series": series}
                 for name, count, series in ((NOMINEE, nominees, 2), (WINNER, winners, 1)) if count]
        columns.append({"year": year, "winners": winners, "nominees": nominees, "total": winners + nominees,
                        "height": _percent_of(winners + nominees, top), "parts": parts})
    return {"columns": columns, "ticks": [{"value": tick, "position": _percent_of(tick, top)} for tick in ticks]}


def match_rows(entries: list[dict]) -> list[dict]:
    """One stacked bar per award (A–Z): entries in the catalog, on Steam outside it, and not on Steam."""
    counts: dict[str, Counter] = {}
    for entry in entries:
        counts.setdefault(entry["award_name"], Counter())[entry["catalog_status"]] += 1
    largest = max((sum(count.values()) for count in counts.values()), default=0)
    rows = []
    for award in sorted(counts):
        total = sum(counts[award].values())
        parts = [{"status": status, "count": counts[award][status], "series": series}
                 for series, status in enumerate(MATCH_STATUSES, start=1) if counts[award][status]]
        rows.append({"award": award, "total": total, "width": _percent_of(total, largest), "parts": parts,
                     "counts": [counts[award][status] for status in MATCH_STATUSES]})
    return rows


def awards_page(tables: ReportTables, params: Mapping[str, str]) -> dict:
    """Everything the Awards page shows, for the filters in `params`."""
    years = sorted({entry["year"] for entry in tables.awards}, reverse=True)
    award_names = sorted({entry["award_name"] for entry in tables.awards})
    filters = {
        "year": _selected(params, "year", (str(year) for year in years)),
        "award": _selected(params, "award", award_names),
        "result": _selected(params, "result", RESULTS),
        "match": _selected(params, "match", MATCH_STATUSES),
    }
    entries = [
        entry for entry in tables.awards
        if (not filters["year"] or str(entry["year"]) == filters["year"])
        and (not filters["award"] or entry["award_name"] == filters["award"])
        and (not filters["result"] or entry["result"] == filters["result"])
        and (not filters["match"] or entry["catalog_status"] == filters["match"])
    ]
    return {
        "filters": filters,
        "filtered": any(filters.values()),
        "year_options": years,
        "award_options": award_names,
        "result_options": RESULTS,
        "match_options": MATCH_STATUSES,
        "all_entries": len(tables.awards),
        "measures": award_measures(entries),
        "by_year": year_columns(entries),
        "by_award": match_rows(entries),
        # Newest year first; then the award, its winner and its nominees A–Z.
        "entries": sorted(entries, key=lambda entry: (-entry["year"], entry["award_name"], not entry["is_winner"],
                                                      entry["award_title"].casefold())),
    }


# ---------------------------------------------------------------------- trending games: three report pages
# The Trending page lists the games; the Trending games dashboard answers three questions about them, one report page
# each. Each page reads the same stored lists, with their own rules and timestamps.

TRENDING_REPORTS = {"sales": "Sales & reception", "upcoming": "Upcoming releases", "momentum": "Momentum"}
DEFAULT_REPORT = next(iter(TRENDING_REPORTS))  # the first page, at /dashboard itself; the others add ?report=


# ---------------------------------------------------------------------- sales & reception: estimated copies sold

RING_NAMED = 5  # the largest estimates each get a named slice; the rest share one "Other tracked games" slice
RING_OUTER, RING_INNER = 49.0, 30.0  # the doughnut's radii in percent of its square: one ring thickness all around
RING_GAP = 0.6  # degrees left out at each end of a slice: about a 2 px gap between neighbors at the doughnut's size
RING_STEP = 3.0  # at most this many degrees between two corners of a slice's outline, so its arcs look round
RING_BADGE_MIN = 14.0  # a slice this many degrees wide or more carries its number


def sales_chart(games: list[dict], estimates: Mapping[int, object], prices: Mapping[int, object],
                now: datetime) -> dict:
    """Estimated Steam copies sold of the Trending page's catalog games, from stored Gamalytic estimates.

    `games` are those games in page order: dicts with appid, name, details_url, image_url, the Trending lists they
    are in ("lists") and their catalog.CatalogGame ("game"). `estimates` holds each game's newest collection
    (store.latest_sales_estimates) and `prices` its stored Steam price row. A game is in the cohort ("ranked") when
    it is released, Steam sells it for a price and its newest collection has an estimate. Every other game is left
    out with its reason, never counted as zero: free (a copies-sold figure misleads for a game nobody has to buy), no
    known Steam price, not released, no estimate, or not asked yet. The cohort runs from the largest estimate down
    and is drawn as a doughnut (sales_ring).
    """
    ranked = []
    left_out: dict[str, list[str]] = {"free": [], "no_price": [], "not_released": [], "no_estimate": [],
                                      "not_asked": []}
    for game in games:
        price, row = prices.get(game["appid"]), estimates.get(game["appid"])
        if game["game"].upcoming:
            left_out["not_released"].append(game["name"])
        elif price is not None and price["status"] == FREE:
            left_out["free"].append(game["name"])
        elif price is None or price["status"] != PRICED:
            left_out["no_price"].append(game["name"])
        elif row is None:
            left_out["not_asked"].append(game["name"])
        elif row["copies_sold"] is None:
            left_out["no_estimate"].append(game["name"])
        else:
            ranked.append({**game, "copies": row["copies_sold"], "collected_at": row["collected_at"],
                           "cached_at": row["source_cached_at"]})
    ranked.sort(key=lambda item: (-item["copies"], item["name"].casefold(), item["appid"]))
    collected = [item["collected_at"] for item in ranked]
    cached = [item["cached_at"] for item in ranked if item["cached_at"]]
    return {
        "ranked": ranked,
        "ring": sales_ring(ranked),
        "left_out": left_out,
        "collected": (min(collected), max(collected)) if collected else None,
        "cached": (min(cached), max(cached)) if cached else None,
        "stale": bool(collected) and now - datetime.fromisoformat(min(collected)) > gamalytic.STALE_AFTER,
    }


def _ring_point(angle: float, radius: float) -> tuple[float, float]:
    """A point of the doughnut's square, in percent: `angle` degrees clockwise from 12 o'clock, `radius` from the
    center (both radii in percent of the square's side)."""
    turn = math.radians(angle)
    return round(50 + radius * math.sin(turn), 2), round(50 - radius * math.cos(turn), 2)


def _ring_outline(start: float, end: float) -> str:
    """The CSS clip-path of the slice from `start` to `end` degrees: the outer arc clockwise, then the inner arc back,
    so every slice has the same ring thickness. A slice that is not the whole ring leaves RING_GAP degrees free at
    each end (less for a sliver), the surface gap between neighbors."""
    if end - start < 360:
        pad = min(RING_GAP, (end - start) / 4)
        start, end = start + pad, end - pad
    steps = max(1, math.ceil((end - start) / RING_STEP))
    angles = [start + (end - start) * step / steps for step in range(steps + 1)]
    corners = [_ring_point(angle, RING_OUTER) for angle in angles]
    corners += [_ring_point(angle, RING_INNER) for angle in reversed(angles)]
    return "polygon(" + ", ".join(f"{x}% {y}%" for x, y in corners) + ")"


def _ring_box(start: float, end: float) -> dict:
    """The smallest box around a slice, in percent of the square: its cover art is fitted to this box (cropped to fill
    it), so each slice shows the middle of its own cover, not a corner of a cover laid over the whole doughnut."""
    angles = [start, end] + [quarter for quarter in (0, 90, 180, 270, 360) if start < quarter < end]
    corners = [_ring_point(angle, radius) for angle in angles for radius in (RING_OUTER, RING_INNER)]
    left, top = min(x for x, _ in corners), min(y for _, y in corners)
    return {"left": left, "top": top, "width": round(max(x for x, _ in corners) - left, 2),
            "height": round(max(y for _, y in corners) - top, 2)}


def shares_in_tenths(values: list[int]) -> list[int]:
    """Each value's share of their sum, in tenths of a percent, rounded so the shares add up to exactly 1,000 (100.0%):
    every share is rounded down, then the tenths left over go to the largest remainders (ties: the earlier value).
    [1, 1, 1] -> [334, 333, 333]."""
    total = sum(values)
    exact = [Fraction(1000 * value, total) for value in values]
    shares = [math.floor(share) for share in exact]
    by_remainder = sorted(range(len(values)), key=lambda index: (shares[index] - exact[index], index))
    for index in by_remainder[:1000 - sum(shares)]:
        shares[index] += 1
    return shares


def sales_ring(ranked: list[dict]) -> dict | None:
    """The copies-sold cohort as a doughnut: each of the RING_NAMED largest estimates its own slice, largest first and
    clockwise from 12 o'clock, then every other game of the cohort in one "Other tracked games" slice.

    A slice's angle is its share of the cohort's total, so the slices add up to the total in the center ("total",
    the sum of every estimate of the cohort, and "games", its size). Each slice has its CSS outline ("clip"), the box
    its cover art fills ("box"), where its number sits ("badge", or None on a slice too narrow for it) and its share
    of the total in tenths of a percent ("share_tenths", rounded so the shares add up to 100.0%). None without a
    cohort.
    """
    if not ranked:
        return None
    total = sum(item["copies"] for item in ranked)
    named, rest = ranked[:RING_NAMED], ranked[RING_NAMED:]
    parts = [{"kind": "game", "number": number, **item} for number, item in enumerate(named, start=1)]
    if rest:
        parts.append({"kind": "other", "number": None, "members": rest, "copies": sum(item["copies"] for item in rest)})
    start = 0.0
    for part, share in zip(parts, shares_in_tenths([part["copies"] for part in parts])):
        end = 360.0 if part is parts[-1] else start + 360 * part["copies"] / total  # the last slice closes the ring
        middle = (start + end) / 2
        part.update(share_tenths=share, start=round(start, 2), end=round(end, 2),
                    clip=_ring_outline(start, end), box=_ring_box(start, end),
                    badge=(_ring_point(middle, (RING_OUTER + RING_INNER) / 2)
                           if part["kind"] == "game" and end - start >= RING_BADGE_MIN else None))
        start = end
    return {"parts": parts, "total": total, "games": len(ranked), "named": len(named),
            "other": parts[-1] if rest else None}


# ---------------------------------------------------------------------- sales & reception: reception against peers

LOG_SCALE_RATIO = 10  # review counts go on a log axis once the largest is at least ten times the smallest
STANDOUT_LABELS = 3  # games named beside their dot; a cohort of this many or fewer has every game named
LABEL_FLIP = 66  # a named dot further right than this share of the width gets its name on its left, inside the plot
# Steam's genres that say how a game plays. Its other genres describe the audience (Casual), the production (Indie),
# the price (Free To Play), the release state (Early Access) or the scale (Massively Multiplayer): sharing one of
# those never makes two games comparable.
GAMEPLAY_GENRES = ("Action", "Adventure", "Racing", "RPG", "Simulation", "Sports", "Strategy")
MIN_PEERS = 5  # a benchmark needs at least this many other comparable games
RECEPTION_VIEWS = ("all", "year")  # the scatterplot's views: every game drawn (the default), or this year's only


def _share(game: dict) -> float:
    return game["positive"] / game["total"]


def _tenths(percent: Fraction) -> int:
    """An exact percentage in tenths of a percent, rounded half up: 93.45 -> 935, 33.33… -> 333."""
    return math.floor(percent * 10 + Fraction(1, 2))


def percent_tenths(positive: int, total: int) -> int:
    """A positive share in tenths of a percent, rounded half up from the exact value: 934 of 1,000 -> 934 (93.4%)."""
    return _tenths(Fraction(100 * positive, total))


def format_tenths(value: int | None, signed: bool = False) -> str:
    """Tenths of a percent (or of a percentage point) as text: 934 -> '93.4'; signed, 15 -> '+1.5', -23 -> '−2.3'
    (a minus sign), 0 -> '0.0'. None -> '' (nothing to show)."""
    if value is None:
        return ""
    text = f"{abs(value) // 10}.{abs(value) % 10}"
    if signed and value:
        return ("+" if value > 0 else "−") + text
    return ("-" if value < 0 else "") + text


def gameplay_genres(genres: Iterable[str]) -> list[str]:
    """A game's Steam genres that say how it plays (GAMEPLAY_GENRES), in Steam's order."""
    return [genre for genre in genres if genre in GAMEPLAY_GENRES]


def peer_benchmark(game: dict, pool: list[dict]) -> dict:
    """How one game's positive share compares with similar releases: the median positive share of its peers.

    `pool` holds the comparable games: catalog games with usable Steam reviews and an exact Steam release date in the
    window, strong reception or not, each a dict with appid, positive, total and genres (Steam's); it usually holds
    `game` too, which is never its own peer. Peers share one gameplay genre with the game, and with several the most
    specific one is chosen: the genre the fewest other games of the pool share, among those that at least MIN_PEERS
    share (ties: A–Z). When none reaches MIN_PEERS, it is the genre most of them share (ties: A–Z), so the count
    shown is the largest there is. The benchmark is the median positive share of the peers, each game counting once
    whatever its number of reviews; with fewer than MIN_PEERS peers there is none ("enough" is False).

    Shares are in tenths of a percent, rounded half up from the exact values: "share" is the game's, "median" its
    peers', and "gap" the first minus the second as shown, so the three numbers on the page always add up.
    "genre" is None when Steam lists no gameplay genre for the game.
    """
    others = [other for other in pool if other["appid"] != game["appid"]]
    shared = {genre: sum(1 for other in others if genre in other["genres"])
              for genre in gameplay_genres(game["genres"])}
    found = {"genre": None, "peers": [], "count": 0, "share": percent_tenths(game["positive"], game["total"]),
             "median": None, "gap": None, "enough": False}
    if not shared:
        return found
    large_enough = [genre for genre, count in shared.items() if count >= MIN_PEERS]
    genre = (min(large_enough, key=lambda genre: (shared[genre], genre)) if large_enough
             else min(shared, key=lambda genre: (-shared[genre], genre)))
    peers = [other for other in others if genre in other["genres"]]
    found.update(genre=genre, peers=peers, count=len(peers), enough=len(peers) >= MIN_PEERS)
    if found["enough"]:
        found["median"] = _tenths(statistics.median(Fraction(100 * peer["positive"], peer["total"]) for peer in peers))
        found["gap"] = found["share"] - found["median"]
    return found


def standout_games(cohort: list[dict]) -> list[int]:
    """The app ids a scatterplot names beside their dots, most important first (where labels would overlap, the page
    keeps the first: app.js). Every game of a small cohort, most reviewed first; otherwise the most reviewed game,
    the one with the highest positive share, and the best received of those with fewer reviews than the cohort's
    median (the upper left). One game can be two of these, so at most STANDOUT_LABELS are named."""
    most_reviewed = sorted(cohort, key=lambda game: (-game["total"], game["appid"]))
    if len(cohort) <= STANDOUT_LABELS:
        return [game["appid"] for game in most_reviewed]
    by_share = sorted(cohort, key=lambda game: (-_share(game), -game["total"], game["appid"]))
    median = statistics.median(game["total"] for game in cohort)
    picks = [most_reviewed[0], by_share[0]] + [game for game in by_share if game["total"] < median][:1]
    return list(dict.fromkeys(game["appid"] for game in picks))


def reception_chart(cohort: list[dict], pool: list[dict], this_year: set[int]) -> dict | None:
    """The window's strong-reception games as one scatterplot, each game once, with every game's peer benchmark.

    `cohort` holds the games drawn: dicts with appid, name, details_url, total (Steam reviews), positive (the positive
    ones), genres (Steam's) and what the page prints (review_label, released_on). `pool` holds the comparable games of
    peer_benchmark: the cohort and the games of the window below the strong-reception threshold. `this_year` holds the
    app ids released this year: they wear the accent color, and the "year" view shows only them, while the "all" view
    (the default) shows every game. Both views share the axes, so a game never moves when the view changes.

    x is the total number of reviews, on a log scale (powers of ten) when the largest total is at least
    LOG_SCALE_RATIO times the smallest, else on a linear scale from zero; y is the positive share, from the 5- or
    10-point step at or below the lowest share or peer median, up to 100%, so every median line fits. Points run left
    to right (fewest reviews first), the order the arrow keys follow. Each carries its "benchmark", the y of its
    peers' median ("median_y"), its gameplay genres (to light up a selected game's peers), the views that show it and
    the views that name it beside its dot (standout_games of each view). None without a game.
    """
    if not cohort:
        return None
    benchmarks = {game["appid"]: peer_benchmark(game, pool) for game in cohort}
    totals = [game["total"] for game in cohort]
    log_scale = max(totals) >= LOG_SCALE_RATIO * min(totals)
    if log_scale:
        low, high = log_decades(min(totals), max(totals))
        x_values = [10 ** power for power in range(low, high + 1)]

        def x(total: int) -> float:
            return _percent_of(math.log10(total) - low, high - low)
    else:
        x_values = axis_ticks(max(totals))

        def x(total: int) -> float:
            return _percent_of(total, x_values[-1])

    medians = [bench["median"] / 10 for bench in benchmarks.values() if bench["median"] is not None]
    lowest = min([_share(game) * 100 for game in cohort] + medians)
    step = 5 if lowest >= 70 else 10
    bottom = min(100 - step, math.floor(lowest / step) * step)

    def y(percent: float) -> float:
        return _percent_of(percent - bottom, 100 - bottom)

    recent = [game for game in cohort if game["appid"] in this_year]
    named = {"all": standout_games(cohort), "year": standout_games(recent)}
    points = []
    for game in sorted(cohort, key=lambda game: (game["total"], -_share(game), game["appid"])):
        spot, bench = x(game["total"]), benchmarks[game["appid"]]
        label_views = [view for view in RECEPTION_VIEWS if game["appid"] in named[view]]
        points.append({
            **game, "x": spot, "y": y(_share(game) * 100), "benchmark": bench,
            "median_y": y(bench["median"] / 10) if bench["median"] is not None else None,
            "gameplay_genres": gameplay_genres(game["genres"]), "this_year": game["appid"] in this_year,
            "views": ["all", "year"] if game["appid"] in this_year else ["all"],
            "label_views": label_views, "label_side": "left" if spot > LABEL_FLIP else "right",
            # Where labels would overlap, the page keeps the first one (app.js): the most important in any view.
            "label_rank": min(named[view].index(game["appid"]) for view in label_views) if label_views else None,
        })
    return {
        "log": log_scale,
        "x_ticks": [{"position": x(value), "label": compact_number(value)} for value in x_values],
        "y_ticks": [{"position": y(value), "label": f"{value}%"} for value in range(bottom, 101, step)],
        "points": points,
        "views": {"all": len(cohort), "year": len(recent)},
    }


# ---------------------------------------------------------------------- upcoming releases: the release radar
# The upcoming games of the tracked lists, placed by how soon each one is due on each platform family. Only an exact
# release day is placed: a month, quarter, year or TBA is listed apart ("Date TBA or approximate"), so no day is guessed.

# The radar's sections, clockwise from the top. With all five, the longest name, PlayStation, sits at the bottom, where
# its label runs along the edge instead of across the rings. A platform belongs to a family by IGDB's name for it (as
# upcoming.PLATFORM_NAMES shortens it): "PC" exactly, a console by the start of its name; every other platform (Mac,
# Linux, iOS, Android, VR headsets and the like) is "Other".
RADAR_FAMILIES = ("PC", "Xbox", "PlayStation", "Switch", "Other")
CONSOLE_FAMILIES = (("PlayStation", "PlayStation"), ("Xbox", "Xbox"), ("Nintendo Switch", "Switch"))
# The rings, from the center (today) out: where each one ends, as (label, months, days) after today. The outer band
# is more than a year away; it ends two years out, and a release after that sits at its edge. Every band has the same
# width, so the scale is not linear: the first week takes as much room as the half year after six months.
RADAR_RINGS = (("1 week", 0, 7), ("1 month", 1, 0), ("3 months", 3, 0), ("6 months", 6, 0), ("1 year", 12, 0))
RADAR_LATER = ("Later", 24, 0)
# The time windows the radar can be narrowed to: (key, label, the ring it ends at, as an index of RADAR_RINGS). Each
# ends on a ring, so the part of the radar a window leaves out is whole bands.
RADAR_WINDOWS = (("month", "Next month", 1), ("quarter", "Next 3 months", 2), ("all", "All tracked", None))
RADAR_EDGE = 0.12  # the share of a band kept free on each side, so a point never sits on a ring line
RADAR_SIDE_MARGIN = 0.12  # the share of a section kept free on each side, so a point never sits on its border
GOLDEN_STEP = 0.6180339887  # the golden ratio's fraction: games due close together get angles far apart


def platform_family(platform: str) -> str:
    """The radar section of one IGDB platform name: PC, PlayStation, Xbox, Switch or Other."""
    if platform == "PC":
        return "PC"
    for prefix, family in CONSOLE_FAMILIES:
        if platform.startswith(prefix):
            return family
    return "Other"


def add_months(day: date, months: int) -> date:
    """The same day `months` later, or that month's last day when it is shorter (31 Jan + 1 month = 28 Feb)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def radar_bands(today: date) -> list[date]:
    """The last day of each ring (RADAR_RINGS) and of the outer band (RADAR_LATER), counted from `today`."""
    return [add_months(today, months) + timedelta(days=days) for _, months, days in (*RADAR_RINGS, RADAR_LATER)]


def radar_distance(day: date, today: date, ends: list[date]) -> float:
    """How far from the center an exact release day sits, as a share of the radius (0 = today, 1 = the edge).

    `ends` are radar_bands(today). Every band has the same width, and inside one the distance grows with the days,
    so days keep their order. A day lies between its band's lines, never on one; a day after the outer band's end
    sits at its edge.
    """
    start, band = today, 0
    while band < len(ends) - 1 and day > ends[band]:
        start, band = ends[band], band + 1
    share = min(1.0, max(0, (day - start).days) / (ends[band] - start).days)
    return (band + RADAR_EDGE + (1 - 2 * RADAR_EDGE) * share) / len(ends)


def radar_windows(day: date, ends: list[date]) -> list[str]:
    """The time windows (RADAR_WINDOWS keys) that show an exact release day: a day within the next month is in all
    three, a day four months out only in "all"."""
    return [key for key, _, ring in RADAR_WINDOWS if ring is None or day <= ends[ring]]


def _days_text(days: int) -> str:
    return "in 1 day" if days == 1 else f"in {days} days"


def _family_releases(availability) -> dict[str, dict]:
    """Each platform family's earliest announced full release of one game: {"group", "platforms"} by family, where
    "group" is the upcoming.PlatformRelease and "platforms" the family's platforms with that date. Availability lists
    announced releases earliest first (TBA last), so the first one met for a family is its earliest."""
    found: dict[str, dict] = {}
    for group in availability.announced if availability else ():
        for platform in group.platforms:
            entry = found.setdefault(platform_family(platform), {"group": group, "platforms": []})
            if entry["group"] is group:
                entry["platforms"].append(platform)
    return found


def _spoken_dates(availability, today: date) -> str:
    """A game's announced releases in words, for its point's text: "18 Feb 2027, in 90 days, on PC, PlayStation 5;
    20 May 2027, in 181 days, on Nintendo Switch 2; TBA on Xbox One; no date yet on Mac"."""
    parts = []
    for group in availability.announced if availability else ():
        platforms = ", ".join(group.platforms)
        if group.precision == "day":
            parts.append(f"{group.label}, {_days_text((group.first.period()[0] - today).days)}, on {platforms}")
        else:
            parts.append(f"{group.label} on {platforms}")
    if availability and availability.undated:
        parts.append(f"no date yet on {', '.join(availability.undated)}")
    return "; ".join(parts) or "no release date listed"


def _release_rows(availability, today: date) -> list[dict]:
    """A game's release timing by platform, for the selected-game panel: one row per announced date, earliest first
    ("when", the days still to go for an exact day as "until", and the platforms), then any early access that has
    started, then the platforms IGDB lists without a date."""
    rows = []
    for group in availability.announced if availability else ():
        exact = group.precision == "day"
        rows.append({"when": group.label, "platforms": ", ".join(group.platforms),
                     "until": _days_text((group.first.period()[0] - today).days) if exact else ""})
    for item in availability.early if availability else ():
        rows.append({"when": f"{item['status'].capitalize()} since {item['label']}",
                     "platforms": ", ".join(item["platforms"]), "until": ""})
    if availability and availability.undated:
        rows.append({"when": "No date yet", "platforms": ", ".join(availability.undated), "until": ""})
    return rows


def _undated_group(availability) -> tuple[tuple, str, str]:
    """Where a game without an exact release day is listed: (sort key, label, words for its panel). The earliest
    date IGDB states decides: a month, quarter or year as given, in order of the last day each allows ("Mar 2027"
    before "2027"), then TBA, then no full-release date at all (only an early access, or platforms without a date)."""
    first = availability.announced[0].first if availability and availability.announced else None
    if first is None:
        return (2,), "No date yet", "No release date listed yet"
    if first.precision == "tbd":
        return (1,), "TBA", "Release date to be announced"
    start, end = first.period()
    return (0, end, start), first.label, f"{first.label}: no exact day yet"


def _radar_points(dated: list[dict], drawn: list[str], today: date, ends: list[date]) -> list[dict]:
    """One point per game and section where the game has an exact day. Every game's first point ("primary", its only
    tab stop, carrying its full text) is its soonest one, so a time window never hides it while another of the game's
    points shows; primary points come first, soonest game first, so the tab order runs from the soonest release out.
    A game keeps the same angle within each of its sections, and games next to each other in time get angles far
    apart."""
    span = 360 / len(drawn)
    primary, repeated = [], []
    for order, game in enumerate(dated):
        spread = RADAR_SIDE_MARGIN + (1 - 2 * RADAR_SIDE_MARGIN) * (order * GOLDEN_STEP % 1)
        soonest = min(game["sections"], key=lambda family: game["exact"][family]["group"].first.period()[0])
        for family in game["sections"]:
            entry = game["exact"][family]
            day = entry["group"].first.period()[0]
            angle = math.radians(-90 + (drawn.index(family) + spread) * span)
            radius = radar_distance(day, today, ends)
            (primary if family == soonest else repeated).append({
                "game": game, "family": family, "label": entry["group"].label, "platforms": entry["platforms"],
                "primary": family == soonest, "windows": radar_windows(day, ends),
                "when": f"{entry['group'].label} · {_days_text((day - today).days)} · {', '.join(entry['platforms'])}",
                "x": round(50 + 50 * radius * math.cos(angle), 2), "y": round(50 + 50 * radius * math.sin(angle), 2),
            })
    return primary + repeated


def _radar_sectors(drawn: list[str], dated: list[dict], bands: int) -> list[dict]:
    """Each section's label spot, its divider line (from the center to the edge, in the grid's -100..100 units) and
    its number of placed games. A label sits inside the outer band, so a phone keeps its whole width for the radar; a
    label on the right ends at its spot and one on the left starts there, so it never sticks out of the circle."""
    span = 360 / len(drawn)
    label_radius = (bands - 0.5) / bands
    sectors = []
    for index, family in enumerate(drawn):
        middle, start = math.radians(-90 + (index + 0.5) * span), math.radians(-90 + index * span)
        side = math.cos(middle)
        sectors.append({"name": family, "games": sum(1 for game in dated if family in game["exact"]),
                        "x": round(50 + 50 * label_radius * side, 2),
                        "y": round(50 + 50 * label_radius * math.sin(middle), 2),
                        "anchor": "end" if side > 0.3 else "start" if side < -0.3 else "middle",
                        "line": (round(100 * math.cos(start), 2), round(100 * math.sin(start), 2))})
    return sectors


def release_radar(upcoming_lists: list[dict], today: date) -> dict:
    """The upcoming games of the tracked lists on a radar: the center is today, each ring a span of time until release,
    each section a platform family (RADAR_FAMILIES).

    `upcoming_lists` are web._trending_lists' upcoming lists, whose "tracked" holds every upcoming game of the list's
    newest saved snapshot (up to upcoming.TRACKED_PER_SIGNAL), as web._igdb_item builds them. The games are their
    list_comparison.distinct_games, matched by verified identifiers only, so a game in both lists (or twice in one)
    counts once, with both titles and its rank in each ("ranks": the place the Trending page gives it), exactly as the
    comparison beside the radar counts it. In every family where a game has an exact release day, it gets one point
    at that family's earliest day, so a game on several platforms appears in each of its sections and the sections'
    counts add up to more than the games. A family whose earliest date is a month, quarter, year or TBA gets no
    point; a game without any point is listed in "groups" by the earliest date IGDB states. Within a section a
    point's angle only keeps games apart. Sections are the families the tracked games are announced for; "x" and "y"
    are percentages of the square plot.

    For the page around the radar: "windows" (RADAR_WINDOWS) with the number of games each shows, the windows that
    show each point, each game's release rows for the selected-game panel ("timing"), and "selected", the game the
    panel shows first (the soonest one, or the first listed game when none has an exact day).
    """
    games = list_comparison.distinct_games(upcoming_lists)
    ends = radar_bands(today)
    present, dated, groups = set(), [], {}
    for game in games:
        availability = game.get("availability")
        families = _family_releases(availability)
        present.update(families)
        present.update(platform_family(platform) for platform in (availability.undated if availability else ()))
        game["spoken"] = _spoken_dates(availability, today)
        game["timing"] = _release_rows(availability, today)
        exact = {family: entry for family, entry in families.items() if entry["group"].precision == "day"}
        if exact:
            soonest = min(exact.values(), key=lambda entry: entry["group"].first.sort_key())["group"]
            days = (soonest.first.period()[0] - today).days
            game.update(exact=exact, soonest=soonest, soonest_text=f"{soonest.label} ({_days_text(days)})")
            dated.append(game)
        else:
            key, label, words = _undated_group(availability)
            game.update(exact={}, soonest=None, soonest_text=label, sections=[], when=words)
            groups.setdefault(key, {"label": label, "when": words, "games": []})["games"].append(game)
    drawn = [family for family in RADAR_FAMILIES if family in present]
    bands = len(ends)
    dated.sort(key=lambda game: (game["soonest"].first.sort_key(), game["name"].casefold()))
    for game in dated:
        game["sections"] = [family for family in drawn if family in game["exact"]]
    ordered_groups = [group for _, group in sorted(groups.items())]
    for group in ordered_groups:
        group["games"].sort(key=lambda game: game["name"].casefold())
    undated = [game for group in ordered_groups for game in group["games"]]
    other = sorted({platform for game in games if game.get("availability")
                    for platform in (*(name for group in game["availability"].announced for name in group.platforms),
                                     *game["availability"].undated)
                    if platform_family(platform) == "Other"})
    return {
        "games": games,
        "dated": dated,
        "points": _radar_points(dated, drawn, today, ends) if dated else [],
        "sectors": _radar_sectors(drawn, dated, bands) if drawn else [],
        "rings": [{"label": label, "until": end, "radius": round(100 * (index + 1) / bands, 2),
                   "top": round(50 - 50 * (index + 1) / bands, 2)}
                  for index, ((label, _, _), end) in enumerate(zip(RADAR_RINGS, ends))],
        "later": {"label": RADAR_LATER[0], "after": ends[-2], "top": round(50 - 50 * (bands - 0.5) / bands, 2)},
        "windows": [{"key": key, "label": label, "until": ends[ring] if ring is not None else None,
                     "radius": round(100 * (ring + 1) / bands, 2) if ring is not None else 100.0,
                     "games": sum(1 for game in dated
                                  if ring is None or game["soonest"].first.period()[0] <= ends[ring])}
                    for key, label, ring in RADAR_WINDOWS],
        "groups": ordered_groups,
        "undated": undated,
        "table": dated + undated,
        "selected": dated[0] if dated else (undated[0] if undated else None),
        "lists": [{"title": data["signal"].title, "games": len(data.get("tracked", ())),
                   "captured_at": data.get("captured_at")} for data in upcoming_lists],
        "other_platforms": other,
        "tracked": upcoming.TRACKED_PER_SIGNAL,
        "today": today,
    }


# ---------------------------------------------------------------------- momentum

WATERFALL_GAMES = 8  # games with a step of their own in the waterfall; the rest are one "Other tracked games" step
HISTORY_DAYS = 30  # the player line covers this period, ending at the newest player snapshot
PLAYER_LINE_GAP = timedelta(days=2)  # snapshots further apart are not joined: the line breaks at the gap


def signed_compact(value: int) -> str:
    """A change for an axis, short and with its sign: 20,000 -> '+20K', -5,000 -> '−5K' (a minus sign), 0 -> '0'."""
    if value == 0:
        return "0"
    return ("+" if value > 0 else "−") + compact_number(abs(value))


def review_waterfall(history: Mapping[int, list]) -> dict | None:
    """Which games account for the change in Steam review count between two comparable snapshots, as a waterfall.

    `history` maps app ids to review snapshot rows (captured_at, total), oldest first (store.review_history). Each
    game is compared by the Trending page's rule (trending.review_changes): its newest snapshot against its newest one
    CHANGE_WINDOW to MAX_BASELINE_AGE earlier. Unlike that list, a fall (Steam removed or corrected reviews) and no
    change are kept: the cohort is every game with a count at both ends, and both totals cover exactly those games.

    The steps run from the earlier total: the WATERFALL_GAMES largest changes either way, each starting where the one
    before ended, then all other games together ("Other tracked games"). The steps add up to the later total minus
    the earlier one ("net"). Positions are percentages of an axis of change since the earlier total, so zero is the
    earlier total and every bar's length is its change. None when no game has two comparable snapshots.
    """
    cohort = []
    for appid, rows in history.items():
        snapshots = [(datetime.fromisoformat(row["captured_at"]), row["total"]) for row in rows]
        if not snapshots:
            continue
        after_at, after = snapshots[-1]
        baseline = trending.baseline_snapshot(snapshots, after_at)
        if baseline is not None:
            cohort.append({"appid": appid, "before": baseline[1], "after": after, "before_at": baseline[0],
                           "after_at": after_at, "change": after - baseline[1]})
    if not cohort:
        return None
    ranked = sorted(cohort, key=lambda game: (-abs(game["change"]), -game["change"], game["appid"]))
    shown = [game for game in ranked[:WATERFALL_GAMES] if game["change"]]  # a zero change never gets its own step
    rest = ranked[len(shown):]
    steps, running = [], 0
    for game in shown:
        steps.append({**game, "kind": "game", "start": running, "end": running + game["change"]})
        running += game["change"]
    if rest:
        change = sum(game["change"] for game in rest)
        steps.append({"kind": "other", "games": len(rest), "change": change, "start": running, "end": running + change})
        running += change
    before_total = sum(game["before"] for game in cohort)
    after_total = sum(game["after"] for game in cohort)
    edges = [0, *(step["end"] for step in steps)]
    ticks = change_ticks(min(edges), max(edges))

    def x(value: int) -> float:
        return _percent_of(value - ticks[0], ticks[-1] - ticks[0])

    for step in steps:
        low, high = sorted((step["start"], step["end"]))
        step.update(left=x(low), width=round(x(high) - x(low), 2), start_x=x(step["start"]), end_x=x(step["end"]))
    net = after_total - before_total
    return {
        "steps": steps,
        "games": len(cohort),
        "rising": sum(1 for game in cohort if game["change"] > 0),
        "falling": sum(1 for game in cohort if game["change"] < 0),
        "before_total": before_total,
        "after_total": after_total,
        "net": net,
        "steps_total": running,  # equals net: each game's change is counted once, in its own step or in "Other"
        "net_left": x(min(0, net)),
        "net_width": round(x(max(0, net)) - x(min(0, net)), 2),
        "zero_x": x(0),
        "ticks": [{"value": tick, "x": x(tick), "label": signed_compact(tick)} for tick in ticks],
        "before": (min(game["before_at"] for game in cohort), max(game["before_at"] for game in cohort)),
        "after": (min(game["after_at"] for game in cohort), max(game["after_at"] for game in cohort)),
    }


def _day_ticks(first: datetime, last: datetime) -> list[datetime]:
    """UTC midnights after `first` up to `last`, at most six: every day, or every 2, 3, 7 or 14 days for longer."""
    days = (last.date() - first.date()).days
    step = next(step for step in (1, 2, 3, 7, 14) if days / step <= 6)
    moment = datetime.combine(first.date() + timedelta(days=1), time(), tzinfo=first.tzinfo)
    ticks = []
    while moment <= last:
        ticks.append(moment)
        moment += timedelta(days=step)
    return ticks


def player_line(rows: list, end: datetime) -> dict:
    """One game's Steam player snapshots over the HISTORY_DAYS up to `end` (the newest player snapshot of any game).

    `rows` are the game's player snapshot rows (captured_at, players), oldest first; a snapshot without a count is
    skipped. "count", "first" and "last" describe the snapshots found. "line" is None unless they span at least
    trending.CHANGE_WINDOW, the gap the Trending page's changes need: a few days show the time-of-day swing, not a
    change. Otherwise every snapshot is a point at its actual time, on a y axis from zero; consecutive snapshots more
    than PLAYER_LINE_GAP apart are not joined, so the line breaks where no snapshot was saved ("gaps").
    """
    start = end - timedelta(days=HISTORY_DAYS)
    snapshots = []
    for row in rows:
        moment = datetime.fromisoformat(row["captured_at"])
        if row["players"] is not None and start <= moment <= end:
            snapshots.append((moment, row["players"]))
    found = {"count": len(snapshots), "first": snapshots[0][0] if snapshots else None,
             "last": snapshots[-1][0] if snapshots else None, "line": None}
    if len(snapshots) < 2 or snapshots[-1][0] - snapshots[0][0] < trending.CHANGE_WINDOW:
        return found
    first, last = snapshots[0][0], snapshots[-1][0]
    seconds = (last - first).total_seconds()
    ticks = axis_ticks(max(players for _, players in snapshots))

    def x(moment: datetime) -> float:
        return round((moment - first).total_seconds() / seconds * 100, 2)

    points = [{"at": moment, "players": players, "x": x(moment), "y": _percent_of(players, ticks[-1])}
              for moment, players in snapshots]
    segments, gaps = [[points[0]]], []
    for previous, point in zip(points, points[1:]):
        if point["at"] - previous["at"] > PLAYER_LINE_GAP:
            gaps.append((previous["at"], point["at"]))
            segments.append([point])
        else:
            segments[-1].append(point)
    return {**found, "line": {
        "points": points,
        # SVG polylines in a 0–100 square, y downward; a lone snapshot between two gaps has its point but no line.
        "segments": [" ".join(f"{point['x']},{round(100 - point['y'], 2)}" for point in segment)
                     for segment in segments if len(segment) > 1],
        "gaps": gaps,
        "y_ticks": [{"label": compact_number(tick), "y": _percent_of(tick, ticks[-1])} for tick in ticks],
        "x_ticks": [{"at": tick, "x": x(tick), "label": f"{tick.day} {tick:%b}"} for tick in _day_ticks(first, last)],
    }}


def capture_days(history: Mapping[int, list], column: str | None = None) -> list[date]:
    """The UTC days on which `history` (snapshot rows by app id) has at least one snapshot, oldest first. With
    `column`, only snapshots that hold a value there count: a player snapshot without a count is not a count."""
    return sorted({datetime.fromisoformat(row["captured_at"]).date() for rows in history.values() for row in rows
                   if column is None or row[column] is not None})


def snapshot_coverage(history: Mapping[int, list], column: str) -> dict:
    """What one kind of snapshot covers, game by game. `history` holds snapshot rows by app id, oldest first; only
    rows with a value in `column` count (a player snapshot without a count is not a count).

    "days" are the UTC days with a snapshot, "games" the games with at least one, "most" and "fewest" the snapshots
    per game, "longest" the longest time between one game's first and last snapshot, and "comparable" the games
    whose newest snapshot has one CHANGE_WINDOW to MAX_BASELINE_AGE earlier (the rule every change on the Trending
    page and in the waterfall uses). Nothing between two snapshots is estimated.
    """
    moments = {appid: [datetime.fromisoformat(row["captured_at"]) for row in rows if row[column] is not None]
               for appid, rows in history.items()}
    moments = {appid: found for appid, found in moments.items() if found}
    counts = [len(found) for found in moments.values()]
    return {
        "days": capture_days(history, column),
        "games": len(moments),
        "most": max(counts, default=0),
        "fewest": min(counts, default=0),
        "longest": max((found[-1] - found[0] for found in moments.values()), default=timedelta(0)),
        "comparable": sum(1 for found in moments.values()
                          if trending.baseline_snapshot([(moment, 0) for moment in found], found[-1]) is not None),
    }


LAUNCH_WEEKS = 4  # the planned launch comparison covers a game's first four weeks on Steam
LAUNCH_BOUNDARIES = tuple(7 * week for week in range(LAUNCH_WEEKS + 1))  # days after release: 0, 7, 14, 21, 28


def launch_weeks_coverage(release_days: Mapping[int, date], history: Mapping[int, list]) -> dict:
    """Which catalog games the planned first-four-weeks launch comparison could include. It is a future analysis:
    the page only states this coverage until real observations support it.

    A game qualifies only with a reliable Steam release day (`release_days`: the exact Steam release date of each
    released catalog game) and a review-count snapshot at every weekly boundary of its first LAUNCH_WEEKS weeks
    (LAUNCH_BOUNDARIES: the release day and 7, 14, 21 and 28 days later), each taken on that day or the next (a sync
    can run after the release moment). A missing snapshot is never filled in, and a lifetime review total saved later
    never stands for the reviews of a launch week. `history` holds review snapshot rows by app id.

    "first" is the first day with a review snapshot of any game; "candidates" are the games released since then (a
    day earlier still counts: its release-day snapshot can be the next day's); "eligible" those with every boundary
    observed, each a list of app ids.
    """
    days = {appid: {datetime.fromisoformat(row["captured_at"]).date() for row in rows}
            for appid, rows in history.items()}
    first = min((day for found in days.values() for day in found), default=None)
    if first is None:
        return {"first": None, "candidates": [], "eligible": []}
    candidates = sorted(appid for appid, released in release_days.items() if released >= first - timedelta(days=1))
    eligible = []
    for appid in candidates:
        observed = days.get(appid, set())
        boundaries = [release_days[appid] + timedelta(days=days) for days in LAUNCH_BOUNDARIES]
        if all(day in observed or day + timedelta(days=1) in observed for day in boundaries):
            eligible.append(appid)
    return {"first": first, "candidates": candidates, "eligible": eligible}


# ---------------------------------------------------------------------- about the data


def coverage_rows(awards: list[dict]) -> list[dict]:
    """Per award (A–Z): the award years it covers, its entries and its winners."""
    groups: dict[str, list[dict]] = {}
    for entry in awards:
        groups.setdefault(entry["award_name"], []).append(entry)
    return [
        {"award": award, "first_year": min(entry["year"] for entry in entries),
         "last_year": max(entry["year"] for entry in entries), "entries": len(entries),
         "winners": sum(entry["is_winner"] for entry in entries)}
        for award, entries in sorted(groups.items())
    ]


def about_page(tables: ReportTables, window: ranking.RankingWindow) -> dict:
    """The facts the About page states about the loaded data."""
    games = tables.games
    return {
        "window": window,
        "games": len(games),
        "promoted": sum(game["catalog_source"] == PROMOTED_SOURCE_NAME for game in games),
        "games_with_review_data": sum(game["has_review_data"] for game in games),
        "in_window": sum(game["in_four_year_window"] for game in games),
        "ranked": sum(game["four_year_rank"] is not None for game in games),
        "labelled": sum(game["production_label"] != NO_VERIFIED_LABEL for game in games),
        "rank_groups": [f"{group}. {name}" for group, name in sorted(ranking.AWARD_GROUP_NAMES.items())],
        "reliable_reviews": ranking.RELIABLE_REVIEW_COUNT,
        "award_entries": len(tables.awards),
        "excluded_award_rows": tables.excluded_award_rows,
        "coverage": coverage_rows(tables.awards),
    }
