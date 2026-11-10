"""Load and validate the hand-edited CSV files in curated/.

These files are the source of truth for the curated game list, awards and
production-scale/indie classifications. Nothing in the app writes to them: the
sync command only reads games.csv, and the web app reads the award and
classification files directly. That is how curated facts survive every sync.

Invalid rows are skipped and reported (with file and line) instead of crashing.
Only rows with review_status "verified" are ever displayed.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

GAMES_FILE = "games.csv"
AWARDS_FILE = "awards.csv"
CLASSIFICATIONS_FILE = "classifications.csv"

REVIEW_STATUSES = {"verified", "needs_review", "rejected"}
AWARD_RESULTS = {"winner", "nominee"}
# same: the Steam app is the awarded game; edition: a remaster, bundle or expanded edition
# of it; none: not on Steam or deliberately not mapped (mapping_note says why).
STEAM_MAPPINGS = {"same", "edition", "none"}
LABELS = {"AAA", "AA", "Indie"}
SCALE_LABELS = ("AAA", "AA")

# Required columns. Optional ones (title, note, mapping_note, checked_on, checked_by) default to "".
GAME_COLUMNS = ["steam_appid"]
AWARD_COLUMNS = [
    "organization", "year", "category", "result", "award_title", "steam_appid", "steam_mapping",
    "source_url", "review_status",
]
CLASSIFICATION_COLUMNS = ["steam_appid", "label", "review_status", "evidence_url", "evidence_summary"]
OPTIONAL_COLUMNS = ["title", "note", "mapping_note", "checked_on", "checked_by"]


@dataclass(frozen=True)
class CuratedGame:
    """One row of games.csv: a Steam app to keep enriched."""

    appid: int
    title: str
    note: str


@dataclass(frozen=True)
class Award:
    """One award result, with the source that confirms it."""

    organization: str
    year: int
    category: str
    result: str
    award_title: str
    appid: int | None
    mapping: str
    mapping_note: str
    source_url: str
    review_status: str
    checked_on: str


@dataclass(frozen=True)
class Classification:
    """One AAA / AA / Indie label for a game, with its evidence."""

    appid: int
    label: str
    review_status: str
    evidence_url: str
    evidence_summary: str
    checked_on: str


@dataclass
class CuratedData:
    """Everything loaded from curated/, plus the problems found while loading."""

    games: list[CuratedGame] = field(default_factory=list)
    awards: list[Award] = field(default_factory=list)
    classifications: list[Classification] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    awards_by_appid: dict[int, list[Award]] = field(default_factory=dict)
    labels_by_appid: dict[int, dict[str, Classification]] = field(default_factory=dict)

    def awards_for(self, appid: int) -> list[Award]:
        """Verified awards mapped to this app: winners first, then the newest year."""
        return self.awards_by_appid.get(appid, [])

    def labels_for(self, appid: int) -> dict[str, Classification]:
        """Verified labels for this app, keyed by label ("AAA", "AA", "Indie")."""
        return self.labels_by_appid.get(appid, {})


class _Problems:
    """Collects "file:line: message" problem strings."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, file_name: str, line: int, message: str) -> None:
        self.messages.append(f"{file_name}:{line}: {message}")


def _read_rows(path: Path, columns: list[str], problems: _Problems) -> list[tuple[int, dict[str, str]]]:
    """Read a CSV file into (line number, row) pairs with stripped values.

    Every required and optional column is present in each row; missing optional ones are "".
    """
    if not path.exists():
        problems.add(path.name, 0, "file not found")
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:  # utf-8-sig accepts files saved by Excel
        reader = csv.DictReader(handle)
        missing = [column for column in columns if column not in (reader.fieldnames or [])]
        if missing:
            problems.add(path.name, 1, f"missing columns: {', '.join(missing)}")
            return []
        rows = []
        for row in reader:
            values = {column: "" for column in OPTIONAL_COLUMNS}
            values.update({key: (value or "").strip() for key, value in row.items() if key})
            rows.append((reader.line_num, values))
        return rows


def _is_url(value: str) -> bool:
    return value.startswith(("https://", "http://"))


def _is_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _load_games(path: Path, problems: _Problems) -> list[CuratedGame]:
    games, seen = [], set()
    for line, row in _read_rows(path, GAME_COLUMNS, problems):
        if not row["steam_appid"].isdigit():
            problems.add(path.name, line, f"steam_appid must be a number, got {row['steam_appid']!r}")
            continue
        appid = int(row["steam_appid"])
        if appid in seen:
            problems.add(path.name, line, f"duplicate steam_appid {appid}")
            continue
        seen.add(appid)
        games.append(CuratedGame(appid=appid, title=row["title"] or f"Steam app {appid}", note=row["note"]))
    return games


def _load_awards(path: Path, problems: _Problems) -> list[Award]:
    awards = []
    for line, row in _read_rows(path, AWARD_COLUMNS, problems):
        errors = []
        for column in ("organization", "category", "award_title"):
            if not row[column]:
                errors.append(f"{column} is empty")
        if not (row["year"].isdigit() and 1970 <= int(row["year"]) <= 2100):
            errors.append(f"year must be a four-digit year, got {row['year']!r}")
        if row["result"] not in AWARD_RESULTS:
            errors.append(f"result must be one of {sorted(AWARD_RESULTS)}, got {row['result']!r}")
        if row["steam_mapping"] not in STEAM_MAPPINGS:
            errors.append(f"steam_mapping must be one of {sorted(STEAM_MAPPINGS)}, got {row['steam_mapping']!r}")
        elif row["steam_mapping"] == "none" and row["steam_appid"]:
            errors.append("steam_appid must be empty when steam_mapping is none")
        elif row["steam_mapping"] != "none" and not row["steam_appid"].isdigit():
            errors.append("steam_appid must be a number when steam_mapping is same or edition")
        if not _is_url(row["source_url"]):
            errors.append("source_url must start with https:// or http://")
        if row["review_status"] not in REVIEW_STATUSES:
            errors.append(f"review_status must be one of {sorted(REVIEW_STATUSES)}, got {row['review_status']!r}")
        if row["checked_on"] and not _is_date(row["checked_on"]):
            errors.append(f"checked_on must be a YYYY-MM-DD date, got {row['checked_on']!r}")
        if errors:
            problems.add(path.name, line, "; ".join(errors))
            continue
        awards.append(
            Award(
                organization=row["organization"],
                year=int(row["year"]),
                category=row["category"],
                result=row["result"],
                award_title=row["award_title"],
                appid=int(row["steam_appid"]) if row["steam_appid"] else None,
                mapping=row["steam_mapping"],
                mapping_note=row["mapping_note"],
                source_url=row["source_url"],
                review_status=row["review_status"],
                checked_on=row["checked_on"],
            )
        )
    return awards


def _load_classifications(path: Path, problems: _Problems) -> list[Classification]:
    classifications, seen = [], set()
    for line, row in _read_rows(path, CLASSIFICATION_COLUMNS, problems):
        errors = []
        if not row["steam_appid"].isdigit():
            errors.append(f"steam_appid must be a number, got {row['steam_appid']!r}")
        if row["label"] not in LABELS:
            errors.append(f"label must be one of {sorted(LABELS)}, got {row['label']!r}")
        if row["review_status"] not in REVIEW_STATUSES:
            errors.append(f"review_status must be one of {sorted(REVIEW_STATUSES)}, got {row['review_status']!r}")
        if row["evidence_url"] and not _is_url(row["evidence_url"]):
            errors.append("evidence_url must start with https:// or http://")
        if row["review_status"] == "verified" and not (row["evidence_url"] and row["evidence_summary"]):
            errors.append("a verified label needs evidence_url and evidence_summary")
        if row["checked_on"] and not _is_date(row["checked_on"]):
            errors.append(f"checked_on must be a YYYY-MM-DD date, got {row['checked_on']!r}")
        if not errors and (row["steam_appid"], row["label"]) in seen:
            errors.append(f"duplicate {row['label']} row for steam_appid {row['steam_appid']}")
        if errors:
            problems.add(path.name, line, "; ".join(errors))
            continue
        seen.add((row["steam_appid"], row["label"]))
        classifications.append(
            Classification(
                appid=int(row["steam_appid"]),
                label=row["label"],
                review_status=row["review_status"],
                evidence_url=row["evidence_url"],
                evidence_summary=row["evidence_summary"],
                checked_on=row["checked_on"],
            )
        )
    return classifications


def _index_awards(awards: list[Award]) -> dict[int, list[Award]]:
    """Group displayable awards (verified and mapped to a Steam app) by app id."""
    by_appid: dict[int, list[Award]] = {}
    for award in awards:
        if award.review_status == "verified" and award.appid is not None:
            by_appid.setdefault(award.appid, []).append(award)
    for appid_awards in by_appid.values():
        appid_awards.sort(key=lambda a: (a.result != "winner", -a.year, a.category))
    return by_appid


def _index_labels(classifications: list[Classification], problems: _Problems) -> dict[int, dict[str, Classification]]:
    """Group verified labels by app id. A game verified as both AAA and AA keeps neither."""
    by_appid: dict[int, dict[str, Classification]] = {}
    for item in classifications:
        if item.review_status == "verified":
            by_appid.setdefault(item.appid, {})[item.label] = item
    for appid, labels in by_appid.items():
        if all(label in labels for label in SCALE_LABELS):
            problems.add(CLASSIFICATIONS_FILE, 0, f"steam_appid {appid} is verified as both AAA and AA; showing neither")
            for label in SCALE_LABELS:
                del labels[label]
    return by_appid


def load_curated(curated_dir: Path) -> CuratedData:
    """Load all curated files. Problems are collected in `problems`; nothing is raised."""
    problems = _Problems()
    games = _load_games(curated_dir / GAMES_FILE, problems)
    awards = _load_awards(curated_dir / AWARDS_FILE, problems)
    classifications = _load_classifications(curated_dir / CLASSIFICATIONS_FILE, problems)
    labels_by_appid = _index_labels(classifications, problems)  # may add conflict problems
    return CuratedData(
        games=games,
        awards=awards,
        classifications=classifications,
        problems=problems.messages,
        awards_by_appid=_index_awards(awards),
        labels_by_appid=labels_by_appid,
    )


class CuratedCache:
    """Reload the curated files only when one of them changes on disk.

    Lets you edit the CSV files while the web app runs, without a restart.
    """

    def __init__(self, curated_dir: Path) -> None:
        self._dir = curated_dir
        self._stamp: tuple | None = None
        self._data = CuratedData()

    def _current_stamp(self) -> tuple:
        stamp = []
        for name in (GAMES_FILE, AWARDS_FILE, CLASSIFICATIONS_FILE):
            path = self._dir / name
            stamp.append(path.stat().st_mtime_ns if path.exists() else None)
        return tuple(stamp)

    def get(self) -> CuratedData:
        """Return the curated data, reloading it if a file changed since the last call."""
        stamp = self._current_stamp()
        if stamp != self._stamp:
            self._data = load_curated(self._dir)
            self._stamp = stamp
            for problem in self._data.problems:
                log.warning("Curated data problem: %s", problem)
        return self._data
