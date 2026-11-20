"""Load and validate the CSV files in curated/.

These files are the source of truth for the catalog's game list, awards and
production-scale/indie classifications:

  games.csv            hand-picked games, edited by hand
  promoted_games.csv   games added through the candidate review workflow; only
                       `python -m game_librarian candidates promote` appends to it
  awards.csv           award results, edited by hand
  classifications.csv  AAA / AA / Indie labels, edited by hand
  publisher_aliases.csv  optional: Steam publisher names that belong to another publisher
                       name (for the publisher filter), edited by hand
  steam_matches.csv    optional: a reviewer's decision on the Steam app of an IGDB game
                       (upcoming.py), edited by hand
  critic_articles.csv  optional: a reviewer's decision on the Wikipedia article whose review
                       table a game's critic scores come from (critics.py), edited by hand
  stories/*.json       the Data Stories (loaded by stories.py, not here)

The sync command only reads the two game lists, and the web app reads the award
and classification files directly, so no sync can overwrite curated facts.

Invalid rows are skipped and reported (with file and line) instead of crashing.
Only rows with review_status "verified" are ever displayed.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .identity import parse_key
from .publishers import publisher_key

log = logging.getLogger(__name__)

GAMES_FILE = "games.csv"
PROMOTED_FILE = "promoted_games.csv"
AWARDS_FILE = "awards.csv"
CLASSIFICATIONS_FILE = "classifications.csv"
PUBLISHER_ALIASES_FILE = "publisher_aliases.csv"
STEAM_MATCHES_FILE = "steam_matches.csv"
CRITIC_ARTICLES_FILE = "critic_articles.csv"

# Where a catalog game comes from: games.csv or promoted_games.csv.
HAND_PICKED, PROMOTED = "hand-picked", "promoted"

REVIEW_STATUSES = {"verified", "needs_review", "rejected"}
AWARD_RESULTS = {"winner", "nominee"}
# same: the Steam app is the awarded game; edition: a remaster, bundle or expanded edition
# of it; none: not on Steam or deliberately not mapped (mapping_note says why).
STEAM_MAPPINGS = {"same", "edition", "none"}
LABELS = {"AAA", "AA", "Indie"}
SCALE_LABELS = ("AAA", "AA")

# Each organization's top award for the year's best game: the "Game of the Year winners" collection.
TOP_AWARD_CATEGORIES = {
    ("The Game Awards", "Game of the Year"),
    ("BAFTA Games Awards", "Best Game"),
    ("Game Developers Choice Awards", "Game of the Year"),
    ("The Steam Awards", "Game of the Year"),
}
# Game Librarian's editorial display order for a game's awards (README "Award display order"): wins of these
# awards first, in this order, then other wins, then nominations. The ranking's award groups follow the same
# order (ranking.py). It is an editorial choice, not a measure of prestige, and it never changes an award's
# data. curated/awards.csv has no D.I.C.E. rows today, so that place stays empty until verified rows are added.
AWARD_DISPLAY_ORDER = (
    ("The Game Awards", "Game of the Year"),
    ("BAFTA Games Awards", "Best Game"),
    ("D.I.C.E. Awards", "Game of the Year"),
    ("Game Developers Choice Awards", "Game of the Year"),
    ("The Steam Awards", "Game of the Year"),
)

# Required columns. Optional ones (title, note, mapping_note, checked_on, checked_by, ...) default to "".
GAME_COLUMNS = ["steam_appid"]
AWARD_COLUMNS = [
    "organization", "year", "category", "result", "award_title", "steam_appid", "steam_mapping",
    "source_url", "review_status",
]
CLASSIFICATION_COLUMNS = ["steam_appid", "label", "review_status", "evidence_url", "evidence_summary"]
# promoted_games.csv also has source, match_method, evidence and promoted_on (written by `candidates promote`).
PROMOTED_COLUMNS = ["steam_appid", "title", "source", "match_method", "evidence", "promoted_on"]
PUBLISHER_ALIAS_COLUMNS = ["steam_publisher", "publisher", "review_status", "source_url"]
STEAM_MATCH_COLUMNS = ["igdb_id", "steam_appid", "decision", "checked_on", "checked_by"]
STEAM_MATCH_DECISIONS = {"match", "no_match"}
CRITIC_ARTICLE_COLUMNS = ["game", "article", "decision", "checked_on", "checked_by"]
CRITIC_ARTICLE_DECISIONS = {"use", "reject"}
OPTIONAL_COLUMNS = ["title", "note", "mapping_note", "checked_on", "checked_by",
                    "source", "match_method", "evidence", "promoted_on"]


@dataclass(frozen=True)
class CuratedGame:
    """One catalog game: a Steam app to keep enriched, from games.csv or promoted_games.csv."""

    appid: int
    title: str
    note: str
    catalog_source: str = HAND_PICKED  # HAND_PICKED or PROMOTED


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

    @property
    def award_name(self) -> str:
        """The organization and category, which together name an award: "BAFTA Games Awards · Best Game"."""
        return f"{self.organization} · {self.category}"

    @property
    def is_top_award(self) -> bool:
        """True for an organization's Game of the Year (or Best Game) category."""
        return (self.organization, self.category) in TOP_AWARD_CATEGORIES


@dataclass(frozen=True)
class Classification:
    """One AAA / AA / Indie label for a game, with its evidence."""

    appid: int
    label: str
    review_status: str
    evidence_url: str
    evidence_summary: str
    checked_on: str


@dataclass(frozen=True)
class PublisherAlias:
    """A Steam publisher name that belongs to another publisher name, with the source that says so."""

    steam_publisher: str
    publisher: str
    review_status: str
    source_url: str


@dataclass(frozen=True)
class SteamMatchDecision:
    """A reviewer's decision about an IGDB game's Steam app: `match` (it is `steam_appid`) or `no_match`."""

    igdb_id: int
    steam_appid: int | None
    decision: str
    checked_on: str
    checked_by: str
    note: str = ""


@dataclass(frozen=True)
class CriticArticleDecision:
    """A reviewer's decision about a game's Wikipedia article: `use` `article`, or `reject` the automatic match."""

    game: str  # a game key: a Steam app id or "igdb-<id>"
    article: str
    decision: str
    checked_on: str
    checked_by: str
    note: str = ""


@dataclass
class CuratedData:
    """Everything loaded from curated/, plus the problems found while loading."""

    games: list[CuratedGame] = field(default_factory=list)
    awards: list[Award] = field(default_factory=list)
    classifications: list[Classification] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    awards_by_appid: dict[int, list[Award]] = field(default_factory=dict)
    labels_by_appid: dict[int, dict[str, Classification]] = field(default_factory=dict)
    sources_by_appid: dict[int, str] = field(default_factory=dict)
    publisher_alias_rows: list[PublisherAlias] = field(default_factory=list)
    steam_match_decisions: dict[int, SteamMatchDecision] = field(default_factory=dict)
    critic_article_decisions: dict[str, CriticArticleDecision] = field(default_factory=dict)

    @property
    def publisher_aliases(self) -> dict[str, str]:
        """Verified aliases: the key of a Steam publisher spelling -> the publisher name it belongs to."""
        return {publisher_key(row.steam_publisher): row.publisher
                for row in self.publisher_alias_rows if row.review_status == "verified"}

    def awards_for(self, appid: int) -> list[Award]:
        """Verified awards mapped to this app, in the editorial display order (award_display_key)."""
        return self.awards_by_appid.get(appid, [])

    def labels_for(self, appid: int) -> dict[str, Classification]:
        """Verified labels for this app, keyed by label ("AAA", "AA", "Indie")."""
        return self.labels_by_appid.get(appid, {})

    def catalog_source(self, appid: int) -> str | None:
        """HAND_PICKED or PROMOTED for a catalog game, None for an app that is not in the catalog."""
        return self.sources_by_appid.get(appid)


class _Problems:
    """Collects "file:line: message" problem strings."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, file_name: str, line: int, message: str) -> None:
        self.messages.append(f"{file_name}:{line}: {message}")


def _read_rows(path: Path, columns: list[str], problems: _Problems, *,
               required: bool = True) -> list[tuple[int, dict[str, str]]]:
    """Read a CSV file into (line number, row) pairs with stripped values.

    Every required and optional column is present in each row; missing optional ones are "".
    A missing file is a problem unless `required` is False (promoted_games.csv starts out absent).
    """
    if not path.exists():
        if required:
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


def _load_games(path: Path, problems: _Problems, *, catalog_source: str = HAND_PICKED,
                known: set[int] | None = None) -> list[CuratedGame]:
    """Read one game list. `known` holds app ids already listed by an earlier file; they are skipped."""
    is_promoted = catalog_source == PROMOTED
    games, seen = [], set(known or ())
    for line, row in _read_rows(path, PROMOTED_COLUMNS if is_promoted else GAME_COLUMNS, problems,
                                required=not is_promoted):
        if not row["steam_appid"].isdigit():
            problems.add(path.name, line, f"steam_appid must be a number, got {row['steam_appid']!r}")
            continue
        appid = int(row["steam_appid"])
        if known and appid in known:
            problems.add(path.name, line, f"steam_appid {appid} is already in {GAMES_FILE}; that row is used")
            continue
        if appid in seen:
            problems.add(path.name, line, f"duplicate steam_appid {appid}")
            continue
        if is_promoted and row["promoted_on"] and not _is_date(row["promoted_on"]):
            problems.add(path.name, line, f"promoted_on must be a YYYY-MM-DD date, got {row['promoted_on']!r}")
            continue
        seen.add(appid)
        note = row["evidence"] if is_promoted else row["note"]
        games.append(CuratedGame(appid=appid, title=row["title"] or f"Steam app {appid}", note=note,
                                 catalog_source=catalog_source))
    return games


def _load_awards(path: Path, problems: _Problems) -> list[Award]:
    """Read awards.csv. A verified row that repeats an earlier verified result for the same game is reported
    and skipped, so no page, ranking or report counts one result twice."""
    awards = []
    verified_lines: dict[tuple, int] = {}  # a verified result -> the line that recorded it first
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
        if not errors and row["review_status"] == "verified":
            # The same organization, year, category and result for the same Steam app (unmapped: the same title).
            result = (_name_key(row["organization"]), int(row["year"]), _name_key(row["category"]), row["result"],
                      int(row["steam_appid"]) if row["steam_appid"] else _name_key(row["award_title"]))
            if result in verified_lines:
                errors.append(f"repeats the result on line {verified_lines[result]} (same organization, year, "
                              "category, result and game); it is counted once")
            else:
                verified_lines[result] = line
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


def _load_publisher_aliases(path: Path, problems: _Problems) -> list[PublisherAlias]:
    """Read publisher_aliases.csv (optional). Each Steam spelling may appear once and must name another publisher."""
    aliases, seen = [], set()
    for line, row in _read_rows(path, PUBLISHER_ALIAS_COLUMNS, problems, required=False):
        errors = []
        for column in ("steam_publisher", "publisher"):
            if not publisher_key(row[column]):
                errors.append(f"{column} is empty")
        if not errors and publisher_key(row["steam_publisher"]) == publisher_key(row["publisher"]):
            errors.append("steam_publisher and publisher are already grouped as the same name")
        if row["review_status"] not in REVIEW_STATUSES:
            errors.append(f"review_status must be one of {sorted(REVIEW_STATUSES)}, got {row['review_status']!r}")
        if not _is_url(row["source_url"]):
            errors.append("source_url must start with https:// or http://")
        if row["checked_on"] and not _is_date(row["checked_on"]):
            errors.append(f"checked_on must be a YYYY-MM-DD date, got {row['checked_on']!r}")
        if not errors and publisher_key(row["steam_publisher"]) in seen:
            errors.append(f"duplicate steam_publisher {row['steam_publisher']!r}")
        if errors:
            problems.add(path.name, line, "; ".join(errors))
            continue
        seen.add(publisher_key(row["steam_publisher"]))
        aliases.append(PublisherAlias(steam_publisher=row["steam_publisher"], publisher=row["publisher"],
                                      review_status=row["review_status"], source_url=row["source_url"]))
    return aliases


def _review_errors(row: dict[str, str]) -> list[str]:
    """Problems with the reviewer columns shared by steam_matches.csv and critic_articles.csv."""
    errors = []
    if not _is_date(row["checked_on"]):
        errors.append(f"checked_on must be a YYYY-MM-DD date, got {row['checked_on']!r}")
    if not row["checked_by"]:
        errors.append("checked_by is empty (say who reviewed it)")
    return errors


def _load_steam_matches(path: Path, problems: _Problems) -> dict[int, SteamMatchDecision]:
    """Read steam_matches.csv (optional): one decision per IGDB game."""
    decisions: dict[int, SteamMatchDecision] = {}
    for line, row in _read_rows(path, STEAM_MATCH_COLUMNS, problems, required=False):
        errors = _review_errors(row)
        igdb_id = int(row["igdb_id"]) if row["igdb_id"].isdigit() and int(row["igdb_id"]) > 0 else None
        if igdb_id is None:
            errors.append(f"igdb_id must be a positive whole number, got {row['igdb_id']!r}")
        if row["decision"] not in STEAM_MATCH_DECISIONS:
            errors.append(f"decision must be one of {sorted(STEAM_MATCH_DECISIONS)}, got {row['decision']!r}")
        appid = int(row["steam_appid"]) if row["steam_appid"].isdigit() and int(row["steam_appid"]) > 0 else None
        if row["decision"] == "match" and appid is None:
            errors.append("a match needs the Steam app id in steam_appid")
        if igdb_id is not None and igdb_id in decisions:
            errors.append(f"duplicate igdb_id {igdb_id}")
        if errors:
            problems.add(path.name, line, "; ".join(errors))
            continue
        decisions[igdb_id] = SteamMatchDecision(igdb_id, appid, row["decision"], row["checked_on"],
                                                row["checked_by"], row["note"])
    return decisions


def _load_critic_articles(path: Path, problems: _Problems) -> dict[str, CriticArticleDecision]:
    """Read critic_articles.csv (optional): one decision per game key."""
    decisions: dict[str, CriticArticleDecision] = {}
    for line, row in _read_rows(path, CRITIC_ARTICLE_COLUMNS, problems, required=False):
        errors = _review_errors(row)
        key = parse_key(row["game"])
        if key is None:
            errors.append(f"game must be a Steam app id or igdb-<IGDB id>, got {row['game']!r}")
        if row["decision"] not in CRITIC_ARTICLE_DECISIONS:
            errors.append(f"decision must be one of {sorted(CRITIC_ARTICLE_DECISIONS)}, got {row['decision']!r}")
        if row["decision"] == "use" and not row["article"]:
            errors.append("using an article needs its English Wikipedia title in article")
        if key is not None and str(key) in decisions:
            errors.append(f"duplicate game {row['game']!r}")
        if errors:
            problems.add(path.name, line, "; ".join(errors))
            continue
        decisions[str(key)] = CriticArticleDecision(str(key), row["article"], row["decision"], row["checked_on"],
                                                    row["checked_by"], row["note"])
    return decisions


def _name_key(text: str) -> str:
    """A name compared without case or extra spaces, so "Game of the year " still names the same award."""
    return " ".join(text.split()).casefold()


_DISPLAY_PLACES = {(_name_key(org), _name_key(category)): place
                   for place, (org, category) in enumerate(AWARD_DISPLAY_ORDER)}
# Each organization appears once in AWARD_DISPLAY_ORDER, so its place there also orders ties between organizations.
_ORGANIZATION_PLACES = {_name_key(org): place for place, (org, _) in enumerate(AWARD_DISPLAY_ORDER)}
OTHER_WINS, NOMINATIONS = len(AWARD_DISPLAY_ORDER), len(AWARD_DISPLAY_ORDER) + 1  # the last two groups


def award_display_group(award: Award) -> int:
    """The award's group in AWARD_DISPLAY_ORDER (0 = The Game Awards' Game of the Year win), OTHER_WINS or NOMINATIONS.

    Only a "winner" result can reach a top group: a nomination for Game of the Year is still a nomination.
    """
    if award.result != "winner":
        return NOMINATIONS
    return _DISPLAY_PLACES.get((_name_key(award.organization), _name_key(award.category)), OTHER_WINS)


def award_identity(award: Award) -> tuple[str, int, str]:
    """The award a row is about: organization, year and category, compared without case or extra spaces.

    Two rows with the same identity and result for one game record the same result, so it is counted once.
    """
    return _name_key(award.organization), award.year, _name_key(award.category)


def award_display_key(award: Award) -> tuple:
    """Sort key for a game's awards: the group, then the newest award year first.

    Ties (same group and year) go to the organizations in AWARD_DISPLAY_ORDER's order, then any other
    organization A–Z, then the category, the awarded title and the source link A–Z, so the order is the
    same on every page load.
    """
    organization = _name_key(award.organization)
    return (award_display_group(award), -award.year,
            _ORGANIZATION_PLACES.get(organization, len(_ORGANIZATION_PLACES)), organization,
            _name_key(award.category), award.award_title.casefold(), award.source_url)


def _index_awards(awards: list[Award]) -> dict[int, list[Award]]:
    """Group displayable awards (verified and mapped to a Steam app) by app id, in the editorial display order."""
    by_appid: dict[int, list[Award]] = {}
    for award in awards:
        if award.review_status == "verified" and award.appid is not None:
            by_appid.setdefault(award.appid, []).append(award)
    for appid_awards in by_appid.values():
        appid_awards.sort(key=award_display_key)
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
    hand_picked = _load_games(curated_dir / GAMES_FILE, problems)
    promoted = _load_games(curated_dir / PROMOTED_FILE, problems, catalog_source=PROMOTED,
                           known={game.appid for game in hand_picked})
    games = hand_picked + promoted
    awards = _load_awards(curated_dir / AWARDS_FILE, problems)
    classifications = _load_classifications(curated_dir / CLASSIFICATIONS_FILE, problems)
    labels_by_appid = _index_labels(classifications, problems)  # may add conflict problems
    publisher_alias_rows = _load_publisher_aliases(curated_dir / PUBLISHER_ALIASES_FILE, problems)
    steam_match_decisions = _load_steam_matches(curated_dir / STEAM_MATCHES_FILE, problems)
    critic_article_decisions = _load_critic_articles(curated_dir / CRITIC_ARTICLES_FILE, problems)
    return CuratedData(
        games=games,
        awards=awards,
        classifications=classifications,
        problems=problems.messages,
        awards_by_appid=_index_awards(awards),
        labels_by_appid=labels_by_appid,
        sources_by_appid={game.appid: game.catalog_source for game in games},
        publisher_alias_rows=publisher_alias_rows,
        steam_match_decisions=steam_match_decisions,
        critic_article_decisions=critic_article_decisions,
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
        for name in (GAMES_FILE, PROMOTED_FILE, AWARDS_FILE, CLASSIFICATIONS_FILE, PUBLISHER_ALIASES_FILE,
                     STEAM_MATCHES_FILE, CRITIC_ARTICLES_FILE):
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
