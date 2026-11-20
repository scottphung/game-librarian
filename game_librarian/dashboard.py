"""The website's dashboard: charts of the Trending lists, and the Power BI report's pages as server-rendered HTML.

Player reception, Awards and About the data aggregate the tables that `export-report` writes
for Power BI (report_export.collect_tables) with the report's measure definitions
(reporting/README.md), so for the same catalog and day both show the same numbers. Tags and
awards filter games through their own rows, so a game with several tags or awards is counted once.

- Trending games (/dashboard, the first and default page; /dashboard/trending redirects there):
  the games of the Trending page's lists compared one by one, with the same games, rules and
  timestamps (web._trending_lists and web._steam_activity). IGDB's Want to Play values are
  relative shares, drawn as bars against each list's highest value. The upcoming games of both
  upcoming lists are placed on a release radar by how soon each is due on each platform family
  (release_radar; only exact release days are placed). Steam's most-wishlisted order and Twitch's
  order are positions only, never drawn as amounts, and the page analyzes them instead of repeating
  them: Twitch's newest list against Steam's newest saved list and against Steam reviews (Twitch's
  data is kept 24 hours, so it has no history). Steam players
  and reviews are Steam only, and unavailable rather than zero for games without Steam data.
  Changes over time appear only once dated snapshots exist. IGDB, Twitch and Steam measures stay
  apart; none is added to another. The lists' definitions, sources and observation times, the
  platform and release-date breakdowns and Twitch's category split are in its collapsed
  "Sources and methodology" section. This page reads stored data directly; it is not in the
  Power BI export. Its Estimated Steam copies sold chart (sales_chart, Gamalytic's estimates) is drawn only for
  a browser on the computer that runs the site: Gamalytic's terms forbid sharing its data (gamalytic.py).
- Player reception (/dashboard/reception; a /dashboard link with its filters, made before 0.10,
  redirects there): review totals and both positive rates, review volume against positive share,
  the rates by production label, the five-year ranking and games per positive-share band.
  Filters: tag, production label, award status.
- Awards (/dashboard/awards): verified award entries by year and by award, how they match
  Steam, and every entry with its source. Filters: year, award, result, match to Steam.
- About the data (/dashboard/about): definitions, coverage and the loaded data.

The functions here only compute: positions are percentages of a chart's plot area, and the
templates (dashboard_*.html) draw them with plain HTML and CSS (sparklines and the radar's grid as
inline SVG), without a chart library. Only the release radar's tooltips use JavaScript (app.js), and
they repeat what the page already says. Every chart prints its values or lists them in a table.
"""

from __future__ import annotations

import math
import statistics
from calendar import monthrange
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

from . import gamalytic, list_history, ranking, trending, upcoming
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


# ---------------------------------------------------------------------- trending games (the Trending page's lists)

SPARKLINE_GAMES = 6  # games with a small chart of their player counts over time
HISTORY_DAYS = 30  # the period those small charts cover, ending at the newest snapshot
SPARK_HEIGHT, SPARK_PAD = 40, 4  # the sparkline's SVG height and inner margin, in viewBox units

# A platform's state in one game, from IGDB's per-platform dates (upcoming.Availability), least to most advanced.
# A game counts once per platform, in its most advanced state.
PLATFORM_STATES = ("undated", "announced", "early", "out")
PLATFORM_STATE_NAMES = {"out": "Out", "early": "Early access", "announced": "Announced",
                        "undated": "Listed without a date"}
NO_IGDB_RECORD, PLATFORMS_UNKNOWN = "No IGDB record", "Platforms not known"
# A game's release status in a list (Availability.summary), in this fixed order.
STATUS_ORDER = ("Out now", "Out on some platforms, more announced", "Early access", "Not out yet",
                "Release not announced", PLATFORMS_UNKNOWN, NO_IGDB_RECORD)
PRECISION_NAMES = {"day": "Exact day", "month": "Month only", "quarter": "Quarter only", "year": "Year only",
                   "tbd": "To be announced"}
NO_RELEASE_DATE = "No full-release date listed"
# Where a cross-platform game's Steam facts come from: a catalog game (stored Steam data), a verified Steam app
# outside the catalog (no Steam data stored), or no verified Steam app (console-only, not on Steam, or unmatched).
STEAM_STORED, STEAM_NOT_STORED, STEAM_NONE = "stored", "not stored", "none"


def platform_states(availability) -> dict[str, str]:
    """Each platform's most advanced state for one game: out, early access, announced (dated or TBA) or undated.

    A platform can be in early access and announced at the same time; it counts as early access.
    """
    states: dict[str, str] = {}
    if availability is None:
        return states
    groups = (
        ("undated", availability.undated),
        ("announced", [name for group in availability.announced for name in group.platforms]),
        ("early", [name for group in availability.early for name in group["platforms"]]),
        ("out", [name for group in availability.available for name in group.platforms]),
    )
    for state, names in groups:
        for name in names:
            states[name] = state  # later groups are further along, so they win
    return states


def platform_rows(items: list[dict]) -> list[dict]:
    """Games per platform in one list, most first, with each platform's split by state.

    A game counts once for every platform it lists, so the rows add up to more than the list's games. A bar's
    width is the platform's share of the list's games, so a full bar means every game in the list.
    """
    counts: dict[str, Counter] = {}
    for item in items:
        for platform, state in platform_states(item.get("availability")).items():
            counts.setdefault(platform, Counter())[state] += 1
    rows = []
    for platform, states in counts.items():
        games = sum(states.values())
        rows.append({"platform": platform, "games": games, "width": _percent_of(games, len(items)),
                     **{state: states[state] for state in PLATFORM_STATES}})
    return sorted(rows, key=lambda row: (-row["games"], row["platform"].casefold()))


def release_status(item: dict) -> str:
    """A listed game's release status as the Trending page states it, or why it cannot be stated."""
    availability = item.get("availability")
    if availability is None:
        return NO_IGDB_RECORD
    return availability.summary if availability.known else PLATFORMS_UNKNOWN


def _count_rows(names: list[str], order: tuple[str, ...], whole: int, key: str) -> list[dict]:
    counts = Counter(names)
    return [{key: name, "games": counts[name], "width": _percent_of(counts[name], whole)}
            for name in order if counts[name]]


def status_rows(items: list[dict]) -> list[dict]:
    """Games per release status in one list (each game once), in STATUS_ORDER; empty statuses are left out."""
    return _count_rows([release_status(item) for item in items], STATUS_ORDER, len(items), "status")


def date_precision_rows(items: list[dict]) -> list[dict]:
    """How precisely the listed games' first full release is stated: exact day, month, quarter, year or TBA."""
    def precision(item: dict) -> str:
        first = item["state"].first if item.get("state") else None
        return PRECISION_NAMES.get(first.precision, PRECISION_NAMES["tbd"]) if first else NO_RELEASE_DATE

    order = (*PRECISION_NAMES.values(), NO_RELEASE_DATE)
    return _count_rows([precision(item) for item in items], order, len(items), "precision")


def review_counts(positive: int | None, total: int | None) -> dict | None:
    """review_split for a game's stored Steam review summary (positive of total), or None without one."""
    if positive is None or total is None:
        return None
    return review_split({"positive_reviews": positive, "negative_reviews": total - positive})


def steam_facts(item: dict, catalog: Mapping[int, object], reviews_at: Mapping[int, str]) -> dict:
    """The Steam data stored for one game of a cross-platform list, or why there is none: never a zero.

    `catalog` maps app ids to catalog games (catalog.CatalogGame); `reviews_at` gives when each catalog game's
    review summary was saved. Only catalog games have stored Steam players and reviews.
    """
    key = item["key"]
    game = catalog.get(int(key)) if key.isdigit() else None
    if game is None:
        return {"status": STEAM_NOT_STORED if item.get("steam_verified") else STEAM_NONE}
    return {"status": STEAM_STORED, "appid": game.appid, "players": game.players, "players_at": game.players_at,
            "split": review_counts(game.positive, game.total), "reviews_at": reviews_at.get(game.appid)}


def cross_platform_games(lists: list[tuple[str, list[dict]]], catalog: Mapping[int, object],
                         reviews_at: Mapping[int, str]) -> list[dict]:
    """Every game of the cross-platform lists once (by game key), in list order, with the lists it is in."""
    games: dict[str, dict] = {}
    for list_name, items in lists:
        for item in items:
            entry = games.get(item["key"])
            if entry is None:
                entry = games[item["key"]] = {"key": item["key"], "name": item["name"],
                                              "details_url": item["details_url"], "lists": [],
                                              "status": release_status(item),
                                              "steam": steam_facts(item, catalog, reviews_at)}
            entry["lists"].append(list_name)
    return list(games.values())


def twitch_coverage(twitch_list: dict) -> dict | None:
    """How the categories of Twitch's last top list divide, and where the listed games sit in it.

    None when no Twitch list is kept (none was saved in the last 24 hours, or Twitch is not configured).
    """
    total = twitch_list["categories"]
    if not total:
        return None
    left = twitch_list["left_out"]
    shown = len(twitch_list["games"])
    games = total - left["no_igdb_game"] - left["not_read"] - left["other_kind"]
    parts = (
        ("Games on Trending’s list", shown),
        ("Other games, below the list", games - shown),
        ("DLC, bundles and other kinds", left["other_kind"]),
        ("Not a game (such as Just Chatting)", left["no_igdb_game"]),
        ("Game not read from IGDB yet", left["not_read"]),
    )
    depth = twitch_list["depth"]
    return {
        "total": total,
        "games": games,
        "parts": [{"name": name, "count": count, "width": _percent_of(count, total)} for name, count in parts if count],
        "positions": [{"position": item["twitch_position"], "name": item["name"],
                       "x": round((item["twitch_position"] - 0.5) / depth * 100, 2)} for item in twitch_list["games"]],
        "depth": depth,
    }


def relative_bars(items: list[dict]) -> list[dict]:
    """A list's games in its order, with IGDB's value as an index of the list's highest value (= 100).

    For signals whose values are relative shares (upcoming.SHARE), such as Want to Play: not counts, and over a
    period IGDB does not state, so a bar only compares games within one list. A game without a value gets no bar
    and no number, never a zero; a value under 1 % of the highest reads "<1", not "0".
    """
    values = [item["value"] for item in items if item.get("value") is not None]
    highest = max(values, default=0)
    rows = []
    for rank, item in enumerate(items, start=1):
        value = item.get("value")
        index = value / highest * 100 if value is not None and highest > 0 else None
        if index is None:
            index_text = ""
        elif 0 < index < 1:
            index_text = "<1"
        else:
            index_text = f"{index:.0f}"
        rows.append({**item, "rank": rank, "index": index, "index_text": index_text,
                     "value_text": f"{value:.6f}" if value is not None else "",
                     "width": round(index, 2) if index is not None else 0.0})
    return rows


def change_bars(items: list[dict]) -> list[dict]:
    """Steam changes (items carrying a trending.Change of players or reviews, largest first) as bars of the rise.

    Only increases are listed (trending.py), so every bar grows from zero; a bar's width is its share of the
    largest rise.
    """
    largest = max((item["change"].difference for item in items), default=0)
    return [{**item, "width": _percent_of(item["change"].difference, largest)} for item in items]


def steam_player_bars(items: list[dict]) -> list[dict]:
    """Popular on Steam right now, in its own order: players in game at each game's own newest snapshot.

    The bars share one scale (the largest count) so they compare, but the counts are never added up: each was
    taken at its own moment, which the bar's label gives.
    """
    largest = max((item["game"].players or 0 for item in items), default=0)
    return [{**item, "players": item["game"].players, "players_at": item["game"].players_at,
             "width": _percent_of(item["game"].players or 0, largest)} for item in items]


def steam_review_splits(items: list[dict]) -> list[dict]:
    """The positive and negative Steam reviews of a Steam list's games, in the list's order."""
    return [{**item, "split": review_counts(item["game"].positive, item["game"].total)} for item in items]


def player_sparklines(items: list[dict], player_history: Mapping[int, list]) -> list[dict]:
    """Small charts of the player counts of a list's first SPARKLINE_GAMES games over the last HISTORY_DAYS.

    A game gets a chart only when its snapshots in that period span at least trending.CHANGE_WINDOW, the gap a
    change needs: a few hours of one day show the time-of-day swing, not a trend, so they get no line.
    """
    times = [item["game"].players_at for item in items if item["game"].players_at]
    if not times:
        return []
    end = datetime.fromisoformat(max(times))
    start = end - timedelta(days=HISTORY_DAYS)
    lines = []
    for item in items[:SPARKLINE_GAMES]:
        snapshots = [(datetime.fromisoformat(row["captured_at"]), row["players"])
                     for row in player_history.get(item["appid"], [])
                     if row["players"] is not None and datetime.fromisoformat(row["captured_at"]) >= start]
        if len(snapshots) < 2 or snapshots[-1][0] - snapshots[0][0] < trending.CHANGE_WINDOW:
            continue
        line = sparkline(snapshots, start, end)
        if line:
            lines.append({"game": item, "line": line})
    return lines


SALES_BARS = 10  # games in the copies-sold chart: as many as a Trending list shows


def sales_chart(games: list[dict], estimates: Mapping[int, object], prices: Mapping[int, object],
                now: datetime) -> dict:
    """Estimated Steam copies sold of the Trending page's catalog games, from stored Gamalytic estimates.

    `games` are those games in page order: dicts with appid, name, details_url, image_url, the Trending lists they
    are in ("lists") and their catalog.CatalogGame ("game"). `estimates` holds each game's newest collection
    (store.latest_sales_estimates) and `prices` its stored Steam price row. A bar needs a released game that Steam
    sells for a price and an estimate in that newest collection. Every other game is left out with its reason, never
    drawn as zero: free (a copies-sold figure misleads for a game nobody has to buy), no known Steam price, not
    released, no estimate, or not asked yet. Bars run from the largest estimate down, on one scale that starts at
    zero; the first SALES_BARS are drawn.
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
    bars = ranked[:SALES_BARS]
    largest = bars[0]["copies"] if bars else 0
    for item in bars:
        item["width"] = _percent_of(item["copies"], largest)
    collected = [item["collected_at"] for item in bars]
    cached = [item["cached_at"] for item in bars if item["cached_at"]]
    return {
        "bars": bars,
        "ranked": len(ranked),
        "left_out": left_out,
        "collected": (min(collected), max(collected)) if collected else None,
        "cached": (min(cached), max(cached)) if cached else None,
        "stale": bool(collected) and now - datetime.fromisoformat(min(collected)) > gamalytic.STALE_AFTER,
    }


# ---------------------------------------------------------------------- the upcoming release radar
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
    """A game's announced releases in words, for its point's text and tooltip: "18 Feb 2027, in 90 days, on PC,
    PlayStation 5; 20 May 2027, in 181 days, on Nintendo Switch 2; TBA on Xbox One; no date yet on Mac"."""
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


def _undated_group(availability) -> tuple[tuple, str, str]:
    """Where a game without an exact release day is listed: (sort key, label, words for its tooltip). The earliest
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
    """One point per game and section where the game has an exact day, the first point of every game first, soonest
    game first, so the tab order runs from the soonest release out. A game keeps the same angle within each of its
    sections, and games next to each other in time get angles far apart."""
    span = 360 / len(drawn)
    primary, repeated = [], []
    for order, game in enumerate(dated):
        spread = RADAR_SIDE_MARGIN + (1 - 2 * RADAR_SIDE_MARGIN) * (order * GOLDEN_STEP % 1)
        for number, family in enumerate(game["sections"]):
            entry = game["exact"][family]
            day = entry["group"].first.period()[0]
            angle = math.radians(-90 + (drawn.index(family) + spread) * span)
            radius = radar_distance(day, today, ends)
            (repeated if number else primary).append({
                "game": game, "family": family, "label": entry["group"].label, "platforms": entry["platforms"],
                "primary": not number,
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
    newest saved snapshot (up to upcoming.TRACKED_PER_SIGNAL), as web._igdb_item builds them. A game in both lists
    counts once, with both titles. In every family where a game has an exact release day, it gets one point at that
    family's earliest day, so a game on several platforms appears in each of its sections and the sections' counts add
    up to more than the games; its first point ("primary") carries its full text, the others repeat it for the eye. A
    family whose earliest date is a month, quarter, year or TBA gets no point; a game without any point is listed in
    "groups" by the earliest date IGDB states. Within a section a point's angle only keeps games apart. Sections are
    the families the tracked games are announced for; "x" and "y" are percentages of the square plot.
    """
    games: dict[str, dict] = {}
    for data in upcoming_lists:
        for item in data.get("tracked", ()):
            games.setdefault(item["key"], {**item, "lists": []})["lists"].append(data["signal"].title)
    ends = radar_bands(today)
    present, dated, groups = set(), [], {}
    for game in games.values():
        availability = game.get("availability")
        families = _family_releases(availability)
        present.update(families)
        present.update(platform_family(platform) for platform in (availability.undated if availability else ()))
        game["spoken"] = _spoken_dates(availability, today)
        exact = {family: entry for family, entry in families.items() if entry["group"].precision == "day"}
        if exact:
            soonest = min(exact.values(), key=lambda entry: entry["group"].first.sort_key())["group"]
            days = (soonest.first.period()[0] - today).days
            game.update(exact=exact, soonest=soonest, soonest_text=f"{soonest.label} ({_days_text(days)})")
            dated.append(game)
        else:
            key, label, words = _undated_group(availability)
            game.update(exact={}, soonest=None, soonest_text=label, sections=[])
            groups.setdefault(key, {"label": label, "when": words, "games": []})["games"].append(game)
    drawn = [family for family in RADAR_FAMILIES if family in present]
    bands = len(ends)
    dated.sort(key=lambda game: (game["soonest"].first.sort_key(), game["name"].casefold()))
    for game in dated:
        game["sections"] = [family for family in drawn if family in game["exact"]]
    ordered_groups = [group for _, group in sorted(groups.items())]
    for group in ordered_groups:
        group["games"].sort(key=lambda game: game["name"].casefold())
    other = sorted({platform for game in games.values() if game.get("availability")
                    for platform in (*(name for group in game["availability"].announced for name in group.platforms),
                                     *game["availability"].undated)
                    if platform_family(platform) == "Other"})
    return {
        "games": list(games.values()),
        "dated": dated,
        "points": _radar_points(dated, drawn, today, ends) if dated else [],
        "sectors": _radar_sectors(drawn, dated, bands) if drawn else [],
        "rings": [{"label": label, "until": end, "radius": round(100 * (index + 1) / bands, 2),
                   "top": round(50 - 50 * (index + 1) / bands, 2)}
                  for index, ((label, _, _), end) in enumerate(zip(RADAR_RINGS, ends))],
        "later": {"label": RADAR_LATER[0], "after": ends[-2], "top": round(50 - 50 * (bands - 0.5) / bands, 2)},
        "groups": ordered_groups,
        "table": dated + [game for group in ordered_groups for game in group["games"]],
        "lists": [{"title": data["signal"].title, "games": len(data.get("tracked", ())),
                   "captured_at": data.get("captured_at")} for data in upcoming_lists],
        "in_both": sum(1 for game in games.values() if len(game["lists"]) > 1),
        "other_platforms": other,
        "tracked": upcoming.TRACKED_PER_SIGNAL,
        "today": today,
    }


# ---------------------------------------------------------------------- Steam's most-wishlisted, Twitch's most-watched
# Analyses instead of the lists themselves (the Trending page lists them): Twitch's newest list against Steam's newest
# saved list and against Steam reviews. Places stay places: a place in Steam's most-wishlisted order or in Twitch's
# order is never drawn or summed as an amount.


def list_overlap(newest: list_history.ListSnapshot | None, twitch_items: list[dict], depth: int) -> dict:
    """The games in both Steam's newest saved list and Twitch's newest list, matched by IGDB id only, drawn as a
    connected-dot chart.

    Each game keeps its two places apart: in Steam's most-wishlisted order ("steam_position") and in Twitch's order of
    categories by current viewers ("twitch_position"). Both sit on one axis of places with #1 at the left, up to
    `depth` (Twitch's list depth) or a deeper place shown; "steam_x" and "twitch_x" are percentages of it. The line
    between a game's two dots only joins them: it is not a measure. Rows keep Steam's order; none when either list is
    missing.
    """
    rows = []
    if newest is not None:
        rows = [{**item, "steam_position": newest.positions[item["igdb_id"]]} for item in twitch_items
                if item.get("igdb_id") in newest.positions]
    rows.sort(key=lambda row: (row["steam_position"], row["twitch_position"]))
    depth = max([depth] + [place for row in rows for place in (row["steam_position"], row["twitch_position"])])

    def x(place: int) -> float:
        return round((place - 0.5) / depth * 100, 2)

    for row in rows:
        row["steam_x"], row["twitch_x"] = x(row["steam_position"]), x(row["twitch_position"])
    ticks = sorted({1, depth // 4, depth // 2, depth * 3 // 4, depth} - {0})
    return {"rows": rows, "depth": depth, "ticks": [{"label": f"#{place}", "x": x(place)} for place in ticks]}


def twitch_reception(twitch_items: list[dict], catalog: Mapping[int, object], reviews_at: Mapping[int, str]) -> dict:
    """The games of Twitch's newest list that have stored Steam reviews (catalog games), in Twitch's order.

    Two measures side by side and never combined: a place in Twitch's order (viewers on any platform, at one moment)
    and Steam reviews (Steam purchasers only). A game without usable review counts is left out, never shown as zero.
    The first upcoming.LIST_SIZE are shown; "compared" counts them all, of the "listed" games in Twitch's list.
    """
    rows = []
    for item in twitch_items:
        facts = steam_facts(item, catalog, reviews_at)
        if facts["status"] == STEAM_STORED and facts["split"]:
            rows.append({**item, "split": facts["split"], "reviews_at": facts["reviews_at"],
                         "review_label": catalog[facts["appid"]].review_label})
    return {"rows": rows[:upcoming.LIST_SIZE], "compared": len(rows), "listed": len(twitch_items)}


def trending_page(lists: dict, steam: dict, reviews_at: Mapping[int, str]) -> dict:
    """Everything the Trending games dashboard shows, from the Trending page's own lists (web._trending_lists and
    web._steam_activity): the same games, signals and timestamps, each measure kept apart.

    A list whose signal gives relative shares gets "bars" (relative_bars); Steam's most-wishlisted list (positions
    only) gets None there and its analyses instead (web._list_analysis). "has_changes" says whether any change
    between two snapshots exists (Steam rises, list position changes, release-date changes); the player
    sparklines are listed on their own.
    """
    recent = lists["recent"]
    twitch_list = lists["twitch"]
    now_lists = [("Most wanted new releases", recent["games"] if recent else []),
                 ("Most watched on Twitch", twitch_list["games"])]
    upcoming_lists = [(data["signal"].title, data["games"]) for data in lists["upcoming"]]
    games = cross_platform_games(upcoming_lists + now_lists, steam["catalog"], reviews_at)
    steam_lists = (steam["popular"], steam["this_year"], steam["four_years"])
    position_changes = [
        {"title": title, "history": data["history"], "games": [item for item in data["games"] if item.get("change")]}
        for title, data in ([(data["signal"].title, data) for data in lists["upcoming"]]
                            + ([("Most wanted new releases", recent)] if recent else []))
    ]
    date_changes = [item for data in lists["upcoming"] for item in data["games"] if item.get("date_change")]
    sparklines = player_sparklines(steam["popular"], steam["player_history"])
    return {
        "upcoming": [
            {**data, "bars": relative_bars(data["games"]) if data["signal"].measure == upcoming.SHARE else None,
             "platforms": platform_rows(data["games"]), "precision": date_precision_rows(data["games"]),
             "on_steam": sum(1 for item in data["games"] if item.get("steam_verified"))}
            for data in lists["upcoming"]
        ],
        "now": [
            {"title": title, "games": items, "platforms": platform_rows(items), "statuses": status_rows(items)}
            for title, items in now_lists
        ],
        "recent": recent,
        "recent_bars": relative_bars(recent["games"]) if recent else [],
        "twitch": twitch_list,
        "twitch_coverage": twitch_coverage(twitch_list),
        "games": games,
        "in_several_lists": [game for game in games if len(game["lists"]) > 1],
        "with_steam": sum(1 for game in games if game["steam"]["status"] == STEAM_STORED),
        "steam_only_games": len({item["appid"] for listed in steam_lists for item in listed}),
        "player_bars": steam_player_bars(steam["popular"]),
        "popular_splits": steam_review_splits(steam["popular"]),
        "this_year_splits": steam_review_splits(steam["this_year"]),
        "four_years_splits": steam_review_splits(steam["four_years"]),
        "player_change_bars": change_bars(steam["player_changes"]),
        "review_change_bars": change_bars(steam["review_changes"]),
        "sparklines": sparklines,
        "position_changes": position_changes,
        "date_changes": date_changes,
        "has_changes": bool(steam["player_changes"] or steam["review_changes"] or date_changes
                            or any(data["games"] for data in position_changes)),
        # The change charts that cannot exist yet, named in one sentence instead of an empty card each.
        "waiting_changes": [name for name, ready in (
            (f"Steam players over the last {HISTORY_DAYS} days", sparklines),
            ("most added players on Steam", steam["player_changes"]),
            ("most new Steam reviews", steam["review_changes"]),
            *((f"{data['title']} position changes", data["games"]) for data in position_changes),
        ) if not ready],
        "history_days": HISTORY_DAYS,
    }


def review_split(game: dict) -> dict | None:
    """A game's positive and negative Steam reviews as two parts of one bar, or None without usable counts."""
    positive, negative = game["positive_reviews"], game["negative_reviews"]
    if positive is None or negative is None or positive + negative == 0:
        return None
    total = positive + negative
    return {"positive": positive, "negative": negative, "total": total, "share": positive / total,
            "positive_width": _percent_of(positive, total), "negative_width": _percent_of(negative, total)}


def sparkline(snapshots: list[tuple[datetime, int]], start: datetime, end: datetime) -> dict | None:
    """SVG points (x 0–100, y 0–SPARK_HEIGHT) for one game's player counts from `start` to `end`.

    Each sparkline has its own vertical scale, from its lowest to its highest count (both labeled),
    because the games' counts differ by orders of magnitude. None with fewer than two snapshots.
    """
    if len(snapshots) < 2 or end <= start:
        return None
    values = [value for _, value in snapshots]
    low, high = min(values), max(values)
    seconds = (end - start).total_seconds()

    def y(value: int) -> float:
        if high == low:
            return SPARK_HEIGHT / 2
        return round(SPARK_HEIGHT - SPARK_PAD - (value - low) / (high - low) * (SPARK_HEIGHT - 2 * SPARK_PAD), 2)

    points = [(round((moment - start).total_seconds() / seconds * 100, 2), y(value)) for moment, value in snapshots]
    return {"points": " ".join(f"{x},{y_}" for x, y_ in points), "end": points[-1], "low": low, "high": high,
            "first": snapshots[0], "last": snapshots[-1], "count": len(snapshots)}


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
