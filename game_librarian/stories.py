"""Data Stories: hand-checked series timelines, and the numbers calculated from them.

Each story is one JSON file in curated/stories/ (for example grand-theft-auto.json), checked
against the Wikipedia revisions it cites. A file holds facts only: titles, roles, first-release
dates, the region and platforms of that first release, and short notes written for Game
Librarian. No Wikipedia text or image is copied. This module validates the files and calculates:

- the timeline in release order (same date: main games first, then the file's order);
- the time between consecutive main games, in calendar years, months and days, counted only
  when both dates name an exact day; an upcoming game is never counted;
- how many main games first appeared on each platform family;
- chart positions for the story page (percentages of the plot, like dashboard.py).

A story can include games that were never sold on Steam. An entry links to a catalog game only
when its file names a Steam app id (catalog_appid) and that game is in the catalog.

Images (entry_image): an entry shows the cover art of the IGDB record its file names (checked against
the entry's title, first release date and platforms), loaded from IGDB's image server, labeled as cover
art and credited to IGDB.com. Nothing is copied or stored, and the art is not licensed for reuse: it is
shown under IGDB's API terms to identify the game on this personal site. No gameplay screenshot is used,
because no source offers one whose content and rights are both known (README "Data Stories"). An entry
without a checked record shows a plain placeholder.

Invalid files and entries are skipped and reported (`check-curated` lists them), never guessed. An
invalid image record is reported and left out; its entry stays.
"""

from __future__ import annotations

import calendar
import json
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

STORIES_FOLDER = "stories"
# Roles, in the order the page explains them, with the label shown for each.
ROLES = {
    "main": "Main game",
    "expansion": "Expansion",
    "sequel": "Direct sequel",
    "remake": "Remake",
    "relaunch": "Relaunch",
    "remaster": "Remaster",
    "online": "Online mode",
    "spin-off": "Spin-off",
    "other": "Other game",
}
# The rows of the release chart: which roles each row holds.
CHART_ROWS = (
    ("Main games", ("main",)),
    ("Expansions, sequels and remakes", ("expansion", "sequel", "remake", "relaunch", "remaster", "online")),
    ("Spin-offs and other games", ("spin-off", "other")),
)
RELEASED, UPCOMING = "released", "upcoming"
# The maker family of each platform name used in the story files.
PLATFORM_FAMILIES = {
    "Famicom": "Nintendo", "Super Famicom": "Nintendo", "Super NES": "Nintendo", "Game Boy Advance": "Nintendo",
    "Nintendo DS": "Nintendo", "GameCube": "Nintendo", "Nintendo Switch": "Nintendo", "Nintendo Switch 2": "Nintendo",
    "PlayStation": "PlayStation", "PlayStation 2": "PlayStation", "PlayStation 3": "PlayStation",
    "PlayStation 4": "PlayStation", "PlayStation 5": "PlayStation", "PlayStation Portable": "PlayStation",
    "Xbox 360": "Xbox", "Xbox One": "Xbox", "Xbox Series X/S": "Xbox",
    "MS-DOS": "PC", "Windows": "PC", "PC": "PC",
    "Mobile phones": "Mobile", "iOS": "Mobile", "Android": "Mobile",
}
# A date as exact as the source gives it: "1997-11-28" (a day), "1992-10" (a month) or "1999" (a year).
RELEASE_DATE = re.compile(r"(?P<year>\d{4})(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?")
# A story's URL name: lowercase words joined by hyphens, such as "final-fantasy".
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
WIKIPEDIA_REVISION_URL = "https://en.wikipedia.org/w/index.php?title={title}&oldid={revision}"
DAYS_PER_YEAR = 365.2425  # the Gregorian average, for chart positions and bar lengths only
# Image records: only IGDB cover art is accepted in story files.
IMAGE_SOURCE, IMAGE_KIND = "IGDB", "cover"
# An IGDB image id as IGDB's API returns it, such as "co2lb0": lowercase letters and digits only.
IGDB_IMAGE_ID = re.compile(r"[a-z0-9]+")
IGDB_GAME_PAGE = "https://www.igdb.com/games/"
IGDB_COVER_URL = "https://images.igdb.com/igdb/image/upload/{size}/{image_id}.jpg"


# ---------------------------------------------------------------------- the data


@dataclass(frozen=True)
class Source:
    """One Wikipedia article revision that the story's facts were read from."""

    title: str
    revision: int
    revision_time: datetime
    used_for: str = ""

    @property
    def url(self) -> str:
        """A permanent link to exactly the revision that was read."""
        return WIKIPEDIA_REVISION_URL.format(title=quote(self.title.replace(" ", "_")), revision=self.revision)


@dataclass(frozen=True)
class ReleaseDate:
    """A first-release date with the precision the source gives: day, month or year."""

    year: int
    month: int | None = None
    day: int | None = None

    @property
    def precision(self) -> str:
        return "day" if self.day else ("month" if self.month else "year")

    @property
    def first_day(self) -> date:
        """The earliest day the date can mean; used for ordering."""
        return date(self.year, self.month or 1, self.day or 1)

    @property
    def label(self) -> str:
        """'28 Nov 1997', 'Oct 1992' or '1999'."""
        if self.day:
            return f"{self.day} {self.first_day:%b %Y}"
        return f"{self.first_day:%b %Y}" if self.month else str(self.year)

    @property
    def label_in_year(self) -> str:
        """The label without its year, for a timeline row under its year: '28 Nov', 'Oct' or '' (year only)."""
        if self.day:
            return f"{self.day} {self.first_day:%b}"
        return f"{self.first_day:%b}" if self.month else ""

    @property
    def iso(self) -> str:
        return self.first_day.isoformat()[: {"day": 10, "month": 7, "year": 4}[self.precision]]

    @property
    def position(self) -> float:
        """The date as a fractional year for charts; a month or year without a day is drawn at its middle."""
        if self.day:
            start = date(self.year, 1, 1)
            return self.year + (self.first_day - start).days / (366 if calendar.isleap(self.year) else 365)
        if self.month:
            return self.year + (self.month - 0.5) / 12
        return self.year + 0.5


@dataclass(frozen=True)
class StoryImage:
    """The IGDB record of an entry's game and that record's cover art, as the story file names them.

    Someone checked that the record is the same game (title, first release date, platforms) on `checked_on`.
    `note` says what the reader should know, such as a cover printed under another regional title.
    """

    igdb_id: int
    image_id: str
    igdb_url: str
    checked_on: date
    note: str = ""


@dataclass(frozen=True)
class EntryImage:
    """The image a timeline row shows, with what it is and where it comes from."""

    url: str
    url_2x: str
    kind: str  # what the image is ("Cover art"), never called a gameplay screenshot
    alt: str
    credit: str  # where it comes from ("IGDB.com")
    credit_url: str  # the page of the IGDB record it belongs to
    note: str = ""


@dataclass(frozen=True)
class StoryEntry:
    """One game in a story, as the story file describes its first release."""

    title: str
    role: str
    of: tuple[str, ...]  # the games this entry belongs to (an expansion's game, a remake's original)
    status: str  # RELEASED or UPCOMING
    released: ReleaseDate
    region: str  # where the first release happened, when the source says; "" otherwise
    platforms: tuple[str, ...]  # the platforms of the first release (for an upcoming game: announced)
    note: str
    source: Source
    catalog_appid: int | None = None  # a Steam app in the Game Librarian catalog, when one is the same game
    catalog_note: str = ""
    image: StoryImage | None = None  # the IGDB cover art to show, when the file names a checked record

    @property
    def role_label(self) -> str:
        return ROLES[self.role]

    @property
    def is_main(self) -> bool:
        return self.role == "main"

    @property
    def upcoming(self) -> bool:
        return self.status == UPCOMING


@dataclass(frozen=True)
class Story:
    """A series story: what it covers, where its facts come from, and its entries in release order."""

    slug: str
    title: str
    summary: str
    checked_on: date
    checked_by: str
    includes: tuple[str, ...]
    excludes: tuple[str, ...]
    notes: tuple[str, ...]
    series_sources: tuple[Source, ...]
    entries: tuple[StoryEntry, ...]

    @property
    def main_entries(self) -> list[StoryEntry]:
        return [entry for entry in self.entries if entry.is_main]

    @property
    def sources(self) -> list[Source]:
        """Every cited revision once: the series sources, then the entries' sources in timeline order."""
        seen, sources = set(), []
        for source in (*self.series_sources, *(entry.source for entry in self.entries)):
            if (source.title, source.revision) not in seen:
                seen.add((source.title, source.revision))
                sources.append(source)
        return sources


@dataclass
class StoryCollection:
    """The stories that loaded, by slug, plus the problems found in the files."""

    stories: dict[str, Story]
    problems: list[str]


# ---------------------------------------------------------------------- loading and validation


def parse_release_date(text: str) -> ReleaseDate | None:
    """'1997-11-28' -> day precision, '1992-10' -> month, '1999' -> year; None for anything else or an impossible date."""
    match = RELEASE_DATE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    year = int(match["year"])
    month = int(match["month"]) if match["month"] else None
    day = int(match["day"]) if match["day"] else None
    try:
        date(year, month or 1, day or 1)
    except ValueError:
        return None
    return ReleaseDate(year, month, day)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value) -> tuple[str, ...] | None:
    """A list of non-empty strings, or None when the value is not one."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        return None
    return tuple(item.strip() for item in value)


def _source(value, problems: list[str], where: str) -> Source | None:
    if not isinstance(value, dict):
        problems.append(f"{where}: source must be an object with title, revision and revision_time")
        return None
    title, revision = _text(value.get("title")), value.get("revision")
    try:
        moment = datetime.fromisoformat(_text(value.get("revision_time")).replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if not title or not isinstance(revision, int) or revision <= 0 or moment is None:
        problems.append(f"{where}: source needs a title, a positive revision number and an ISO revision_time")
        return None
    return Source(title=title, revision=revision, revision_time=moment, used_for=_text(value.get("used_for")))


def parse_image(value, problems: list[str], where: str) -> StoryImage | None:
    """An entry's image record, or None. An invalid record is reported and left out; the entry itself stays.

    Daily Trivia questions use the same record format and checks (trivia.py)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        problems.append(f"{where}: image left out: it must be an object")
        return None
    errors = []
    if value.get("source") != IMAGE_SOURCE or value.get("kind") != IMAGE_KIND:
        errors.append(f"only source {IMAGE_SOURCE!r} with kind {IMAGE_KIND!r} is supported")
    igdb_id = value.get("igdb_id")
    if not isinstance(igdb_id, int) or isinstance(igdb_id, bool) or igdb_id <= 0:
        errors.append("igdb_id must be a positive number")
    image_id, igdb_url = _text(value.get("image_id")), _text(value.get("igdb_url"))
    if not IGDB_IMAGE_ID.fullmatch(image_id):
        errors.append(f"image_id must be IGDB's image id (letters and digits), got {image_id!r}")
    if not igdb_url.startswith(IGDB_GAME_PAGE):
        errors.append(f"igdb_url must start with {IGDB_GAME_PAGE}")
    try:
        checked_on = date.fromisoformat(_text(value.get("checked_on")))
    except ValueError:
        checked_on = None
        errors.append("checked_on must be a YYYY-MM-DD date")
    if errors:
        problems.append(f"{where}: image left out: {'; '.join(errors)}")
        return None
    return StoryImage(igdb_id=igdb_id, image_id=image_id, igdb_url=igdb_url, checked_on=checked_on,
                      note=_text(value.get("note")))


def _entry(value, checked_on: date, problems: list[str], where: str) -> StoryEntry | None:
    """One validated entry, or None (with the reasons added to `problems`)."""
    if not isinstance(value, dict):
        problems.append(f"{where}: an entry must be an object")
        return None
    errors = []
    title, role, status = _text(value.get("title")), _text(value.get("role")), _text(value.get("status"))
    if not title:
        errors.append("title is empty")
    if role not in ROLES:
        errors.append(f"role must be one of {list(ROLES)}, got {role!r}")
    if status not in (RELEASED, UPCOMING):
        errors.append(f"status must be released or upcoming, got {status!r}")
    released = parse_release_date(value.get("first_release"))
    if released is None:
        errors.append(f"first_release must be YYYY-MM-DD, YYYY-MM or YYYY, got {value.get('first_release')!r}")
    elif status == RELEASED and released.first_day > checked_on:
        errors.append("a released game's first_release cannot be after checked_on")
    elif status == UPCOMING and released.first_day <= checked_on:
        errors.append("an upcoming game's date has passed: check it again and update the status")
    platforms = _texts(value.get("platforms"))
    if not platforms:
        errors.append("platforms must be a non-empty list of names")
    else:
        unknown = [name for name in platforms if name not in PLATFORM_FAMILIES]
        if unknown:
            errors.append(f"unknown platform names {unknown}; add them to stories.PLATFORM_FAMILIES")
    of = _texts(value.get("of", []))
    if of is None:
        errors.append("of must be a list of game titles")
    appid = value.get("catalog_appid")
    if appid is not None and (not isinstance(appid, int) or appid <= 0):
        errors.append("catalog_appid must be a Steam app id or null")
    source = _source(value.get("source"), problems, f"{where} ({title or 'untitled'})")
    if errors:
        problems.append(f"{where} ({title or 'untitled'}): {'; '.join(errors)}")
    if errors or source is None:
        return None
    return StoryEntry(title=title, role=role, of=of, status=status, released=released,
                      region=_text(value.get("first_release_region")), platforms=platforms,
                      note=_text(value.get("note")), source=source, catalog_appid=appid,
                      catalog_note=_text(value.get("catalog_note")),
                      image=parse_image(value.get("image"), problems, f"{where} ({title})"))


def load_story(path: Path, problems: list[str]) -> Story | None:
    """Load and validate one story file. A file that is unreadable or misses its frame is skipped."""
    name = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{name}: cannot be read as JSON ({exc})")
        return None
    if not isinstance(data, dict):
        problems.append(f"{name}: the file must hold one JSON object")
        return None
    errors = []
    slug = _text(data.get("slug"))
    if not SLUG.fullmatch(slug) or slug != path.stem:
        errors.append(f"slug must be lowercase words joined by hyphens and match the file name, got {slug!r}")
    for field in ("title", "summary", "checked_by"):
        if not _text(data.get(field)):
            errors.append(f"{field} is empty")
    try:
        checked_on = date.fromisoformat(_text(data.get("checked_on")))
    except ValueError:
        checked_on = None
        errors.append("checked_on must be a YYYY-MM-DD date")
    lists = {field: _texts(data.get(field, [])) for field in ("includes", "excludes", "notes")}
    for field, value in lists.items():
        if value is None:
            errors.append(f"{field} must be a list of sentences")
    if not lists["includes"]:
        errors.append("includes must say what the story covers")
    if not isinstance(data.get("entries"), list) or not data["entries"]:
        errors.append("entries must be a non-empty list")
    if errors:
        problems.append(f"{name}: {'; '.join(errors)}")
        return None

    series_sources = []
    for index, value in enumerate(data.get("series_sources") or [], start=1):
        source = _source(value, problems, f"{name}: series source {index}")
        if source:
            series_sources.append(source)
    entries, titles = [], set()
    for index, value in enumerate(data["entries"], start=1):
        entry = _entry(value, checked_on, problems, f"{name}: entry {index}")
        if entry is None:
            continue
        if entry.title in titles:
            problems.append(f"{name}: entry {index}: duplicate title {entry.title!r}")
            continue
        titles.add(entry.title)
        entries.append((index, entry))
    for index, entry in entries:
        missing = [title for title in entry.of if title not in titles]
        if missing:
            problems.append(f"{name}: entry {index} ({entry.title}): 'of' names games not in this story: {missing}")
    if not any(entry.is_main for _, entry in entries):
        problems.append(f"{name}: the story has no main game")
        return None
    # Release order; on the same date, main games first, then the file's order.
    ordered = sorted(entries, key=lambda item: (item[1].released.first_day, not item[1].is_main, item[0]))
    return Story(slug=slug, title=_text(data["title"]), summary=_text(data["summary"]), checked_on=checked_on,
                 checked_by=_text(data["checked_by"]), includes=lists["includes"], excludes=lists["excludes"] or (),
                 notes=lists["notes"] or (), series_sources=tuple(series_sources),
                 entries=tuple(entry for _, entry in ordered))


def load_stories(curated_dir: Path) -> StoryCollection:
    """Load every story file in curated/stories/, A–Z by title. A missing folder means no stories."""
    problems: list[str] = []
    stories = {}
    folder = curated_dir / STORIES_FOLDER
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        story = load_story(path, problems)
        if story:
            stories[story.slug] = story
    ordered = dict(sorted(stories.items(), key=lambda item: item[1].title.casefold()))
    return StoryCollection(stories=ordered, problems=problems)


# ---------------------------------------------------------------------- calculations


def _add_months(start: date, months: int) -> date:
    """`start` moved by whole months; a day the target month lacks becomes its last day (31 Jan + 1 month = 28/29 Feb)."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def calendar_difference(start: date, end: date) -> tuple[int, int, int]:
    """Whole years, months and days from `start` to `end` (not before it).

    28 Nov 1997 -> 22 Oct 1999 is (1, 10, 24): one year and ten months reach 28 Sep 1999, plus 24 days.
    """
    if end < start:
        raise ValueError("end is before start")
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if _add_months(start, months) > end:
        months -= 1
    days = (end - _add_months(start, months)).days
    return months // 12, months % 12, days


def duration_text(years: int, months: int, days: int | None = None) -> str:
    """'1 year, 10 months and 24 days'; parts that are zero are left out ('2 years')."""
    parts = [f"{value} {unit}{'' if value == 1 else 's'}"
             for value, unit in ((years, "year"), (months, "month"), (days, "day")) if value]
    if not parts:
        return "0 days"
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def short_duration(years: int, months: int, days: int = 0) -> str:
    """'1 yr 10 mo' for tiles and bar labels; under a month is 'under 1 mo'."""
    parts = [text for value, text in ((years, f"{years} yr"), (months, f"{months} mo")) if value]
    return " ".join(parts) or "under 1 mo"


@dataclass(frozen=True)
class Gap:
    """The wait from one main game's first release to the next main game's first release."""

    earlier: StoryEntry
    later: StoryEntry
    span: tuple[int, int, int] | None  # (years, months, days); None when a date is not exact to the day
    days: int | None

    @property
    def counted(self) -> bool:
        return self.span is not None

    @property
    def text(self) -> str:
        if not self.counted:
            return "Not calculated: a release date is not exact to the day."
        return duration_text(*self.span)

    @property
    def short(self) -> str:
        """'1 yr 10 mo' for bar labels (days are in the full text)."""
        return short_duration(*self.span) if self.counted else "—"


def main_gaps(story: Story) -> list[Gap]:
    """The waits between consecutive released main games, in release order."""
    released = [entry for entry in story.main_entries if not entry.upcoming]
    gaps = []
    for earlier, later in zip(released, released[1:]):
        exact = earlier.released.precision == "day" and later.released.precision == "day"
        start, end = earlier.released.first_day, later.released.first_day
        gaps.append(Gap(earlier, later, calendar_difference(start, end) if exact else None,
                        (end - start).days if exact else None))
    return gaps


def gap_summary(gaps: list[Gap]) -> dict:
    """The longest and shortest counted wait and the median wait in days (None without counted waits)."""
    counted = [gap for gap in gaps if gap.counted]
    if not counted:
        return {"longest": None, "shortest": None, "median_days": None, "counted": 0, "skipped": len(gaps)}
    return {
        "longest": max(counted, key=lambda gap: gap.days),
        "shortest": min(counted, key=lambda gap: gap.days),
        "median_days": statistics.median(gap.days for gap in counted),
        "counted": len(counted),
        "skipped": len(gaps) - len(counted),
    }


def since_latest_main(story: Story) -> tuple[StoryEntry, tuple[int, int, int]] | None:
    """The newest released main game and the time from its first release to the day the story was checked."""
    released = [entry for entry in story.main_entries if not entry.upcoming]
    if not released or released[-1].released.precision != "day":
        return None
    latest = released[-1]
    return latest, calendar_difference(latest.released.first_day, story.checked_on)


def platform_families(story: Story) -> list[dict]:
    """How many main games first appeared on each platform family, most first (ties A–Z).

    A game first released on two families (PC and PlayStation) counts once for each.
    """
    families: dict[str, list[StoryEntry]] = {}
    for entry in story.main_entries:
        if entry.upcoming:
            continue
        for family in dict.fromkeys(PLATFORM_FAMILIES[name] for name in entry.platforms):
            families.setdefault(family, []).append(entry)
    rows = sorted(families.items(), key=lambda item: (-len(item[1]), item[0]))
    largest = max((len(entries) for _, entries in rows), default=0)
    return [{"family": family, "games": len(entries), "titles": [entry.title for entry in entries],
             "width": round(len(entries) / largest * 100, 2)} for family, entries in rows]


# ---------------------------------------------------------------------- the page


def _year_ticks(first: int, last: int) -> list[int]:
    """Round years for the time axis: every 5 years, or every 10 for spans over 40 years."""
    step = 5 if last - first <= 40 else 10
    return [year for year in range((first // step + 1) * step, last + 1, step)]


def release_chart(story: Story) -> dict:
    """Dot positions for the release chart: one row per CHART_ROWS group, x from 1 Jan of the first year
    to 31 Dec of the last one. Upcoming games are drawn hollow."""
    first = story.entries[0].released.year
    last = max(entry.released.year for entry in story.entries)
    start, end = first, last + 1

    def x(value: float) -> float:
        return round((value - start) / (end - start) * 100, 2)

    rows = []
    for label, roles in CHART_ROWS:
        entries = [entry for entry in story.entries if entry.role in roles]
        if entries:
            rows.append({"label": label, "games": len(entries), "main": "main" in roles, "dots": [
                {"x": x(entry.released.position), "entry": entry,
                 "title": f"{entry.title}: {entry.role_label.lower()}, "
                          f"{'scheduled for' if entry.upcoming else 'first released'} {entry.released.label}"}
                for entry in entries]})
    ticks = [{"year": year, "x": x(year)} for year in _year_ticks(first, last)]
    return {"rows": rows, "ticks": ticks, "first": first, "last": last}


def gap_bars(gaps: list[Gap]) -> list[dict]:
    """Bar lengths for the waits between main games, as a share of the longest wait."""
    longest = max((gap.days for gap in gaps if gap.counted), default=0)
    return [{"gap": gap, "width": round(gap.days / longest * 100, 2) if gap.counted and longest else 0,
             "years": round(gap.days / DAYS_PER_YEAR, 1) if gap.counted else None} for gap in gaps]


def timeline(story: Story, main_only: bool = False) -> list[dict]:
    """The entries grouped by decade of first release ("1990s"), oldest first, and within it by year."""
    decades: dict[int, list[StoryEntry]] = {}
    for entry in story.entries:
        if main_only and not entry.is_main:
            continue
        decades.setdefault(entry.released.year // 10 * 10, []).append(entry)
    groups = []
    for decade, entries in sorted(decades.items()):
        years: dict[int, list[StoryEntry]] = {}
        for entry in entries:  # already in release order
            years.setdefault(entry.released.year, []).append(entry)
        groups.append({"decade": f"{decade}s", "entries": entries,
                       "years": [{"year": year, "entries": listed} for year, listed in years.items()]})
    return groups


def entry_image(entry: StoryEntry) -> EntryImage | None:
    """The image a timeline row shows: the cover art of the entry's checked IGDB record, or None (a placeholder).

    The small size (90 x 128 px, and 180 x 256 px for sharp screens) is all a timeline row needs.
    """
    if entry.image is None:
        return None
    return EntryImage(url=IGDB_COVER_URL.format(size="t_cover_small", image_id=entry.image.image_id),
                      url_2x=IGDB_COVER_URL.format(size="t_cover_small_2x", image_id=entry.image.image_id),
                      kind="Cover art", alt=f"Cover art of {entry.title}", credit="IGDB.com",
                      credit_url=entry.image.igdb_url, note=entry.image.note)
