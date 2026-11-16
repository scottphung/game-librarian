"""The website's dashboard: the Power BI report's three pages as server-rendered HTML.

The pages aggregate the tables that `export-report` writes for Power BI
(report_export.collect_tables) with the report's measure definitions (reporting/README.md),
so for the same catalog and day both show the same numbers. Tags and awards filter games
through their own rows, so a game with several tags or awards is counted once.

- Player reception (/dashboard): review totals and both positive rates, review volume
  against positive share, the rates by production label, the four-year ranking and games
  per positive-share band. Filters: tag, production label, award status.
- Awards (/dashboard/awards): verified award entries by year and by award, how they match
  Steam, and every entry with its source. Filters: year, award, result, match to Steam.
- About the data (/dashboard/about): definitions, coverage and the loaded data.

The functions here only compute: positions are percentages of a chart's plot area, and the
templates (dashboard_*.html) draw them with plain HTML and CSS, without a chart library or
JavaScript. A chart that shows values only as positions also lists them in a table.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Iterable, Mapping

from . import ranking
from .report_export import (
    IN_CATALOG, LABEL_JOINER, LABEL_ORDER, NO_LISTED_AWARD, NO_REVIEW_DATA_BAND, NOMINEE, NOT_MAPPED,
    ON_STEAM_NOT_IN_CATALOG, POSITIVE_BANDS, PROMOTED_SOURCE_NAME, UNCLASSIFIED, WINNER, ReportTables,
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
    """Production labels in tier order: AAA, AA, Indie, each combination after its first label, Unclassified last."""
    if label == UNCLASSIFIED:
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
    return f"#{rank} in the four-year ranking" if rank is not None else game["rank_status"].lower()


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
    low = math.floor(math.log10(min(totals)))
    high = max(math.ceil(math.log10(max(totals))), low + 1)
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
    filters = {
        "tag": _selected(params, "tag", (row["tag"] for row in tables.tags)),
        "label": _selected(params, "label", labels),
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
        "labelled": sum(game["production_label"] != UNCLASSIFIED for game in games),
        "award_entries": len(tables.awards),
        "excluded_award_rows": tables.excluded_award_rows,
        "coverage": coverage_rows(tables.awards),
    }
