"""Export the catalog as CSV tables for the Power BI report (reporting/GameLibrarian.pbip).

Run `python -m game_librarian export-report` after a sync. It reads the SQLite
catalog and the curated CSV files and writes one UTF-8 CSV file per table. Nothing
is sent anywhere, and no setting (in particular the Steam API key) is written.

Tables, one row per:
  games.csv        visible catalog game: review counts, production label, four-year rank, award status, and
                   where it came from (hand-picked in games.csv, or promoted from a reviewed candidate)
  game_tags.csv    game and Steam tag (a bridge table: a game has many tags)
  tags.csv         Steam tag (the dimension behind the tag slicer)
  awards.csv       verified award result, linked to a catalog game when one matches; award_name joins the
                   organization and the category, because several organizations use "Game of the Year"
  export_info.csv  the export itself: when it ran, which sync it reflects, the ranking window

The four-year rank is calculated exactly as on the website (see ranking.py), with
the window ending on the day of the export. It does not move until the next export.

Review counts exist only in games.csv. Tags and awards filter games through their
own tables instead of being joined onto them, so a game with several tags or awards
is never counted twice in review totals.

The website's dashboard (dashboard.py) aggregates the same tables in memory
(collect_tables), so it shows the same numbers as the Power BI report.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import ranking, store
from .config import STORE_PAGE_URL, Settings
from .curated import PROMOTED, CuratedData, load_curated
from .prepare import percent_positive

LABEL_ORDER = ("AAA", "AA", "Indie")
LABEL_JOINER = " + "  # a game with several verified labels: "AA + Indie"
UNCLASSIFIED = "Unclassified"
# A game's award status and an award row's result.
WINNER, NOMINEE, NO_LISTED_AWARD = "Winner", "Nominee", "No listed award"
# Where an award result matches Steam.
IN_CATALOG, ON_STEAM_NOT_IN_CATALOG, NOT_MAPPED = "In catalog", "On Steam, not in catalog", "Not mapped to Steam"
# Ordered bands of a game's positive share, for the distribution chart: (lower bound %, label).
POSITIVE_BANDS = ((95, "95–100%"), (90, "90–94%"), (80, "80–89%"), (70, "70–79%"), (0, "Below 70%"))
NO_REVIEW_DATA_BAND = "No usable review data"
# A four-digit year in Steam's release-date text, such as "Aug 3, 2023" or "3 Aug, 2023".
RELEASE_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# The report's data-folder parameter in expressions.tmdl: expression DataFolder = "C:\...\" meta [...]
DATA_FOLDER_PARAMETER = re.compile(r'(expression DataFolder = )"(?:[^"]|"")*"')

GAME_COLUMNS = [
    "appid", "title", "steam_url", "developers", "publishers", "genres", "release_date", "release_date_exact",
    "release_year", "production_label", "ranking_tier", "ranking_tier_name", "award_status", "award_entries",
    "award_wins", "top_award_wins", "has_review_data", "positive_reviews", "negative_reviews", "total_reviews",
    "positive_share", "steam_percent_positive", "review_score_desc", "positive_band", "positive_band_order",
    "in_four_year_window", "four_year_rank", "rank_status", "rank_note", "catalog_source", "reviews_updated_at",
    "details_updated_at",
]
GAME_TAG_COLUMNS = ["appid", "tag", "tag_position"]
TAG_COLUMNS = ["tag", "games"]
AWARD_COLUMNS = [
    "award_id", "organization", "year", "category", "award_name", "is_top_award", "result", "is_winner",
    "award_title", "catalog_game", "steam_appid", "catalog_appid", "steam_mapping", "award_applies_to",
    "catalog_status", "mapping_note", "source_url", "checked_on",
]
# Where a catalog game comes from, as the report shows it.
PROMOTED_SOURCE_NAME = "Promoted candidate"
CATALOG_SOURCE_NAMES = {PROMOTED: PROMOTED_SOURCE_NAME}
HAND_PICKED_NAME = "Hand-picked"
EXPORT_INFO_COLUMNS = [
    "exported_at_utc", "last_sync_started_at_utc", "last_sync_finished_at_utc", "last_sync_status", "games",
    "games_with_review_data", "ranking_window_start", "ranking_window_end", "games_in_ranking_window",
    "ranked_games", "verified_award_rows", "excluded_unverified_award_rows", "data_note",
]
# What an award result applies to: the listed Steam app itself, an earlier version than the edition
# Steam sells (a remaster, bundle or expanded edition), or nothing on Steam.
APPLIES_TO = {"same": "This Steam app", "edition": "Original release", "none": "No Steam app"}


@dataclass
class ExportSummary:
    """What one export wrote, for the command's report."""

    out_dir: Path
    rows: dict[str, int]
    window: ranking.RankingWindow


@dataclass
class ReportTables:
    """The report tables in memory: one dict per row, keyed by the CSV column names.

    Values keep their types (bool, int, float, or None for "no value"); only the CSV writer
    turns them into text. `export_report` writes these rows, and the dashboard aggregates them.
    """

    games: list[dict]
    game_tags: list[dict]
    tags: list[dict]
    awards: list[dict]
    last_sync: sqlite3.Row | None  # the newest sync run that saved data (success or partial)
    excluded_award_rows: int  # curated award rows left out because they are not verified


def _utc(value: str | None) -> str:
    """'2026-11-10T02:05:16+00:00' -> '2026-11-10 02:05:16' (UTC, a format Power Query reads in any locale)."""
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _names(value: str | None) -> str:
    return "; ".join(json.loads(value or "[]"))


def _positive_band(percent: int | None) -> tuple[str, int]:
    if percent is None:
        return NO_REVIEW_DATA_BAND, len(POSITIVE_BANDS) + 1
    for order, (lower_bound, label) in enumerate(POSITIVE_BANDS, start=1):
        if percent >= lower_bound:
            return label, order
    raise AssertionError("the last band starts at 0%")


def _game_rows(rows: list, curated: CuratedData, placements: dict[int, ranking.Placement]) -> list[dict]:
    games = []
    for row in rows:
        appid = row["appid"]
        labels = [name for name in LABEL_ORDER if name in curated.labels_for(appid)]
        awards = curated.awards_for(appid)
        wins = sum(award.result == "winner" for award in awards)
        top_wins = sum(award.result == "winner" and award.is_top_award for award in awards)
        positive, total = row["review_positive"], row["review_total"]
        usable = ranking.unranked_reason(positive, total) is None
        percent = percent_positive(positive, total) if usable else None
        band, band_order = _positive_band(percent)
        place = placements[appid]
        year = RELEASE_YEAR.search(row["release_date"] or "")
        games.append({
            "appid": appid,
            "title": row["name"] or row["curated_title"],
            "steam_url": STORE_PAGE_URL.format(appid=appid),
            "developers": _names(row["developers"]),
            "publishers": _names(row["publishers"]),
            "genres": _names(row["genres"]),
            "release_date": row["release_date"] or "",
            "release_date_exact": place.released_on.isoformat() if place.released_on else "",
            "release_year": year.group(0) if year else "",
            "production_label": LABEL_JOINER.join(labels) or UNCLASSIFIED,
            "ranking_tier": place.tier,
            "ranking_tier_name": place.tier_name,
            "award_status": WINNER if wins else (NOMINEE if awards else NO_LISTED_AWARD),
            "award_entries": len(awards),
            "award_wins": wins,
            "top_award_wins": top_wins,
            "has_review_data": usable,
            "positive_reviews": positive if usable else None,
            "negative_reviews": row["review_negative"] if usable else None,
            "total_reviews": total if usable else None,
            "positive_share": positive / total if usable else None,
            "steam_percent_positive": percent,
            "review_score_desc": row["review_score_desc"] or "",
            "positive_band": band,
            "positive_band_order": band_order,
            "in_four_year_window": place.in_window,
            "four_year_rank": place.rank,
            "rank_status": place.status,
            "rank_note": place.note or "",
            "catalog_source": CATALOG_SOURCE_NAMES.get(curated.catalog_source(appid), HAND_PICKED_NAME),
            "reviews_updated_at": _utc(row["reviews_updated_at"]),
            "details_updated_at": _utc(row["details_updated_at"]),
        })
    return games


def _award_rows(curated: CuratedData, catalog_titles: dict[int, str]) -> list[dict]:
    """Every verified award result in file order, including results with no Steam app (for coverage).

    `catalog_titles` maps the app id of each catalog game to its current Steam name.
    """
    awards = []
    for award in curated.awards:
        if award.review_status != "verified":
            continue
        in_catalog = award.appid in catalog_titles
        if in_catalog:
            status = IN_CATALOG
        elif award.appid is not None:
            status = ON_STEAM_NOT_IN_CATALOG
        else:
            status = NOT_MAPPED
        awards.append({
            "award_id": len(awards) + 1,
            "organization": award.organization,
            "year": award.year,
            "category": award.category,
            "award_name": award.award_name,
            "is_top_award": award.is_top_award,
            "result": WINNER if award.result == "winner" else NOMINEE,
            "is_winner": award.result == "winner",
            "award_title": award.award_title,
            "catalog_game": catalog_titles[award.appid] if in_catalog else "",
            "steam_appid": award.appid,
            "catalog_appid": award.appid if in_catalog else None,
            "steam_mapping": award.mapping,
            "award_applies_to": APPLIES_TO[award.mapping],
            "catalog_status": status,
            "mapping_note": award.mapping_note,
            "source_url": award.source_url,
            "checked_on": award.checked_on,
        })
    return awards


def _csv_cell(value):
    """A typed table value as CSV text: true/false, an empty cell for None, six decimals for a share."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):  # the only float column is positive_share
        return f"{value:.6f}"
    return value


def _write_tables(out_dir: Path, tables: dict[str, tuple[list[str], list[dict]]]) -> None:
    """Write every table to a temporary file first, then move them into place.

    A refresh in Power BI therefore never reads a half-written file.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
        for name, (columns, rows) in tables.items():
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="", dir=out_dir, prefix=f".{name}.", suffix=".tmp", delete=False
            )
            with handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows({column: _csv_cell(value) for column, value in row.items()} for row in rows)
            staged.append((Path(handle.name), out_dir / name))
        for temporary, final in staged:
            os.replace(temporary, final)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def collect_tables(conn: sqlite3.Connection, curated: CuratedData, window: ranking.RankingWindow) -> ReportTables:
    """Build the report tables for the catalog stored in `conn`, with the four-year ranking for `window`."""
    catalog = store.matching_games(conn, [], [])
    appids = [row["appid"] for row in catalog]
    rows_by_appid = store.game_rows(conn, appids)
    tags = store.tags_for(conn, appids)
    placements = ranking.place_catalog(catalog, curated, window)
    games = _game_rows([rows_by_appid[appid] for appid in appids], curated, placements)
    game_tags = [
        {"appid": appid, "tag": tag, "tag_position": position}
        for appid in appids for position, tag in enumerate(tags[appid], start=1)
    ]
    tag_games: dict[str, int] = {}
    for row in game_tags:
        tag_games[row["tag"]] = tag_games.get(row["tag"], 0) + 1
    return ReportTables(
        games=games,
        game_tags=game_tags,
        tags=[{"tag": tag, "games": count} for tag, count in sorted(tag_games.items())],
        awards=_award_rows(curated, {game["appid"]: game["title"] for game in games}),
        last_sync=store.last_successful_sync(conn),
        excluded_award_rows=sum(award.review_status != "verified" for award in curated.awards),
    )


def export_report(settings: Settings, out_dir: Path, *, now: datetime | None = None,
                  today: date | None = None) -> ExportSummary:
    """Write the report tables for the current catalog to `out_dir` and return their row counts.

    `today` ends the four-year ranking window: by default this computer's calendar date,
    the same day the website would use.
    """
    curated = load_curated(settings.curated_dir)
    window = ranking.RankingWindow.ending_on(today or date.today())
    conn = store.initialize(settings.db_path)
    try:
        report = collect_tables(conn, curated, window)
    finally:
        conn.close()

    last_sync = report.last_sync
    export_info = [{
        "exported_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "last_sync_started_at_utc": _utc(last_sync["started_at"]) if last_sync else "",
        "last_sync_finished_at_utc": _utc(last_sync["finished_at"]) if last_sync else "",
        "last_sync_status": last_sync["status"] if last_sync else "no successful sync yet",
        "games": len(report.games),
        "games_with_review_data": sum(game["has_review_data"] for game in report.games),
        "ranking_window_start": window.start.isoformat(),
        "ranking_window_end": window.end.isoformat(),
        "games_in_ranking_window": sum(game["in_four_year_window"] for game in report.games),
        "ranked_games": sum(game["four_year_rank"] is not None for game in report.games),
        "verified_award_rows": len(report.awards),
        "excluded_unverified_award_rows": report.excluded_award_rows,
        "data_note": "Latest values from the last sync only; the project keeps no history.",
    }]
    tables = {
        "games.csv": (GAME_COLUMNS, report.games),
        "game_tags.csv": (GAME_TAG_COLUMNS, report.game_tags),
        "tags.csv": (TAG_COLUMNS, report.tags),
        "awards.csv": (AWARD_COLUMNS, report.awards),
        "export_info.csv": (EXPORT_INFO_COLUMNS, export_info),
    }
    _write_tables(out_dir, tables)
    return ExportSummary(out_dir=out_dir, rows={name: len(rows) for name, (_, rows) in tables.items()}, window=window)


def point_report_at(data_dir: Path, parameters_file: Path) -> bool:
    """Set the report's DataFolder parameter to `data_dir`. Return True if the file changed.

    Power BI Desktop needs an absolute folder path, so the path of this computer is
    written into the report project's expressions.tmdl file.
    """
    with parameters_file.open(encoding="utf-8", newline="") as handle:  # newline="" keeps the line endings as they are
        text = handle.read()
    folder = str(data_dir.resolve()) + os.sep
    m_string = '"' + folder.replace('"', '""') + '"'  # an M text literal doubles its quotes
    if not DATA_FOLDER_PARAMETER.search(text):
        raise ValueError(f"no DataFolder parameter found in {parameters_file}")
    updated = DATA_FOLDER_PARAMETER.sub(lambda match: match.group(1) + m_string, text, count=1)
    if updated == text:
        return False
    with parameters_file.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return True
