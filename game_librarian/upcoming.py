"""The cross-platform Trending lists from IGDB and Twitch, each ranked by one named signal (no combined score).

Signals (IGDB PopScore, popularity_primitives; checked live 2026-11-19, README "Trending"):
  * WANT_TO_PLAY (type 2, source IGDB): "Additions to IGDB.com users' Want to Play lists". The value is a
    relative share (the top game had 0.00295), not a count, and IGDB does not document its time window;
    86,825 games had a value, most of them released. Only the order is shown. It ranks two lists: the
    upcoming games ("Most wanted on IGDB") and the games first released in the last RECENT_WINDOW ("Most
    wanted new releases"), so a game keeps its signal and its snapshots when it comes out.
  * STEAM_WISHLISTS (type 10, source Steam): "Most Wishlisted Upcoming". 4,863 games, all with a Steam app.
    The values step down by the same amount from one game to the next, so they encode positions in
    Steam's most-wishlisted list, not wishlist counts (Valve does not publish those). PC games on Steam only.
  * Twitch's Get Top Games order (twitch.py): categories by current viewers, order only, kept 24 hours. The refresh
    also reads IGDB's own record of each category (external_games, source Twitch), so twitch_games can tell which
    game a category stands for when Twitch gives no igdb_id (Dota 2), and leaves it out when the two disagree; a
    category that a reviewer marked as a game series (Counter-Strike) is listed as that series.
IGDB recalculates PopScore about once a day ("Updated every 24 hours", IGDB API docs).
Investigated and not used: Playing (type 3; on 2026-11-19 only 9 of its top 1,000 were released in the last
90 days, and Grand Theft Auto VI's value still predated its launch), Visits (type 1; its top 25 included
adult and novelty titles), Played (type 4; classics) and IGDB's Twitch Hours Watched (type 34; calculated the
evening before, so Grand Theft Auto VI had 0.0 on its launch day).

Upcoming rule: an IGDB main game, standalone expansion, remake, remaster or expanded game that IGDB does
not mark released, offline, cancelled, rumored or delisted, and that has no full release that has happened.
A release counts as happened when its exact day is today or earlier, or, for a month, quarter or year,
when that whole period has passed. A month, quarter, year or "TBD" is shown as given: no day is invented.
Alpha, beta, early access and advanced access dates do not end "upcoming"; they are shown as notes.
Recent rule: a game of those types (not cancelled or rumored) whose first full release on any platform has
happened, and whose stated period started at most RECENT_WINDOW ago. Platforms whose own release is still
ahead stay "announced" (Availability), so a game can be out on one platform and upcoming on another.

Identity and Steam (identity.py): an IGDB game's key is "igdb-<id>". It gets a Steam link only when the
match is verified: IGDB lists exactly one Steam app for it, Steam lists that app as a game, and Steam's
name for it equals IGDB's (ignoring case, accents and punctuation), or a reviewer confirmed it in
curated/steam_matches.csv. A similar title alone never attaches a Steam app. It opens the catalog game when
that verified app is in the catalog, or when it is the only IGDB game IGDB lists for a catalog app.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from calendar import monthrange
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

from . import igdb, store, twitch
from .curated import CuratedData, SteamMatchDecision
from .identity import igdb_key
from .prepare import normalize_for_search
from .steam import SteamClient, SteamError
from .trending import baseline_snapshot


# What a PopScore value means. SHARE: a relative share (not a count, over a period IGDB does not state), so values
# of one list can be compared with each other, as bars. POSITION: the values only step down from one place to the
# next, so they stand for list positions and are never drawn as amounts.
SHARE, POSITION = "share", "position"


@dataclass(frozen=True)
class Signal:
    """One PopScore popularity type used as a ranking, with what it means and how deep the scan goes."""

    key: str
    popularity_type: int
    title: str
    depth: int  # how many of IGDB's highest values are scanned for upcoming games
    platforms_note: str
    tracks_recent_releases: bool = False  # also keep the games first released in the last RECENT_WINDOW
    measure: str = SHARE  # SHARE or POSITION: what the values mean


WANT_TO_PLAY = Signal("want_to_play", 2, "Most wanted on IGDB", 1000,
                      "All platforms, including console-only games.", tracks_recent_releases=True)
STEAM_WISHLISTS = Signal("steam_wishlists", 10, "Most wishlisted on Steam", 200,
                         "PC games with a Steam store page only.", measure=POSITION)
SIGNALS = (WANT_TO_PLAY, STEAM_WISHLISTS)
SIGNALS_BY_KEY = {signal.key: signal for signal in SIGNALS}

LIST_SIZE = 10  # games shown per list
TRACKED_PER_SIGNAL = 25  # games per list (upcoming, and recent releases) whose full IGDB records are kept
RECENT_WINDOW = timedelta(days=90)  # "Most wanted new releases": first released at most this long ago
REFRESH_AFTER = timedelta(hours=20)  # IGDB recalculates PopScore about once a day
SOURCE = "upcoming"  # the source_runs name of this refresh (IGDB lists and Twitch's top games)
UPCOMING, RECENT = "upcoming", "recent"  # the two lists a Want to Play entry can appear in (list_kind)

GAME_TYPES = {0: "Main game", 4: "Standalone expansion", 8: "Remake", 9: "Remaster", 10: "Expanded game"}
NOT_UPCOMING_STATUSES = {0: "Released", 5: "Offline", 6: "Cancelled", 7: "Rumored", 8: "Delisted"}
FULL = "full"
PRE_RELEASE = {1: "alpha", 2: "beta", 3: "early access", 34: "advanced access"}  # release_date_statuses
# Later releases on a newer platform (IGDB's "Digital Compatibility Release" and "Next-Gen Optimization Patch
# Release"): the game can be played there, but such a date is never a first release (Grand Theft Auto V's PS5 date).
RE_RELEASE = {35: "digital compatibility release", 36: "next-gen patch"}
PRE_RELEASE_KINDS = frozenset(PRE_RELEASE.values())
RE_RELEASE_KINDS = frozenset(RE_RELEASE.values())
# IGDB date_formats (and the deprecated `category` with the same numbers).
PRECISIONS = {0: "day", 1: "month", 2: "year", 3: "quarter", 4: "quarter", 5: "quarter", 6: "quarter", 7: "tbd"}
PRECISION_ORDER = {"day": 0, "month": 1, "quarter": 2, "year": 3, "tbd": 4}
PLATFORM_NAMES = {"PC (Microsoft Windows)": "PC"}  # shorter display names; others are shown as IGDB names them
OFFICIAL = "Official website"
WEBSITE_KINDS = {1: OFFICIAL, 22: "Xbox store", 23: "PlayStation Store", 24: "Nintendo eShop",
                 16: "Epic Games Store", 17: "GOG"}  # Steam (13) is linked only through a verified match
# A store link counts as verified only on the store's own domain (or a subdomain of it).
STORE_DOMAINS = {
    "Xbox store": ("xbox.com", "microsoft.com"),
    "PlayStation Store": ("playstation.com",),
    "Nintendo eShop": ("nintendo.com", "nintendo.co.uk", "nintendo.co.jp", "nintendo.de", "nintendo.fr",
                       "nintendo.es", "nintendo.it", "nintendo.nl", "nintendo.com.au", "nintendo.co.kr",
                       "nintendo.com.hk"),
    "Epic Games Store": ("epicgames.com",),
    "GOG": ("gog.com",),
}
NOT_LISTABLE_STATUSES = {6: "Cancelled", 7: "Rumored"}  # a game IGDB marks so is never shown as out
COVER_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg"
IMAGE_ID = re.compile(r"[a-z0-9]{1,40}")  # IGDB image ids such as "co95i0"

SCREEN_FIELDS = ("game_type,game_status,first_release_date,release_dates.date,release_dates.date_format,"
                 "release_dates.category,release_dates.y,release_dates.m,release_dates.status")
RECORD_FIELDS = (
    "name,slug,url,summary,game_type,game_status,first_release_date,cover.image_id,platforms.name,"
    "release_dates.date,release_dates.date_format,release_dates.category,release_dates.y,release_dates.m,"
    "release_dates.platform.name,release_dates.release_region,release_dates.status,"
    "involved_companies.company.name,involved_companies.publisher,involved_companies.developer,"
    "involved_companies.company.websites.url,involved_companies.company.websites.type,"
    "websites.url,websites.type,external_games.uid,external_games.external_game_source"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------- release dates (pure)

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class ReleaseDate:
    """One IGDB release date, at the precision IGDB gives it, for one platform."""

    platform: str
    precision: str  # day | month | quarter | year | tbd
    status: str  # "full", or a pre-release kind such as "early access"
    year: int | None = None
    month: int | None = None
    day: int | None = None
    quarter: int | None = None
    region: int | None = None

    @property
    def label(self) -> str:
        """The date as precise as IGDB states it: "23 Feb 2027", "Feb 2027", "Q1 2027", "2027" or "TBA"."""
        if self.precision == "day":
            return f"{self.day} {MONTHS[self.month - 1]} {self.year}"
        if self.precision == "month":
            return f"{MONTHS[self.month - 1]} {self.year}"
        if self.precision == "quarter":
            return f"Q{self.quarter} {self.year}"
        if self.precision == "year":
            return str(self.year)
        return "TBA"

    def period(self) -> tuple[date, date] | None:
        """The first and last day the stated date allows, or None for "to be announced"."""
        if self.precision == "day":
            day = date(self.year, self.month, self.day)
            return day, day
        if self.precision == "month":
            return date(self.year, self.month, 1), date(self.year, self.month, monthrange(self.year, self.month)[1])
        if self.precision == "quarter":
            first_month = 3 * self.quarter - 2
            return date(self.year, first_month, 1), date(self.year, first_month + 2,
                                                         monthrange(self.year, first_month + 2)[1])
        if self.precision == "year":
            return date(self.year, 1, 1), date(self.year, 12, 31)
        return None

    def has_happened(self, today: date) -> bool:
        """True only when the whole stated period is over (an exact day: that day is today or earlier)."""
        period = self.period()
        if period is None:
            return False
        return period[0] <= today if self.precision == "day" else period[1] < today

    def sort_key(self) -> tuple:
        """Earlier periods first; for the same start, the more precise date first; TBA last."""
        period = self.period()
        return (period[0] if period else date.max, PRECISION_ORDER[self.precision], self.platform)


def _platform_name(value) -> str | None:
    name = value.get("name") if isinstance(value, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    return PLATFORM_NAMES.get(name.strip(), name.strip())


def parse_release_date(entry: dict, platform: str = "") -> ReleaseDate | None:
    """One IGDB release_dates entry, or None when its format, status or date cannot be read safely.

    The day of an exact date comes from the `date` timestamp (IGDB leaves `d` empty); a month,
    quarter or year uses only `y` (and `m`), never the placeholder timestamp IGDB stores with it.
    """
    status_id = entry.get("status")
    if status_id is None or status_id == 6:
        status = FULL
    elif status_id in PRE_RELEASE:
        status = PRE_RELEASE[status_id]
    elif status_id in RE_RELEASE:
        status = RE_RELEASE[status_id]  # availability on a newer platform, never a first release
    else:  # offline and cancelled statuses say nothing about where the game can be played
        return None
    fmt = entry.get("date_format", entry.get("category"))
    precision = PRECISIONS.get(fmt) if isinstance(fmt, int) else None
    year, month = entry.get("y"), entry.get("m")
    if precision == "tbd":
        return ReleaseDate(platform, "tbd", status, region=entry.get("release_region"))
    if precision == "day":
        stamp = entry.get("date")
        if not isinstance(stamp, int) or isinstance(stamp, bool):
            return None
        day = datetime.fromtimestamp(stamp, tz=timezone.utc).date()
        return ReleaseDate(platform, "day", status, day.year, day.month, day.day, region=entry.get("release_region"))
    if not isinstance(year, int) or isinstance(year, bool) or not 1950 <= year <= 2200:
        return None
    if precision == "month":
        if not isinstance(month, int) or not 1 <= month <= 12:
            return None
        return ReleaseDate(platform, "month", status, year, month, region=entry.get("release_region"))
    if precision == "quarter":
        return ReleaseDate(platform, "quarter", status, year, quarter=fmt - 2, region=entry.get("release_region"))
    if precision == "year":
        return ReleaseDate(platform, "year", status, year, region=entry.get("release_region"))
    return None


def release_dates(game: dict) -> list[ReleaseDate]:
    """Every readable release date of an IGDB game record, earliest first."""
    dates = []
    for entry in game.get("release_dates") or []:
        if isinstance(entry, dict):
            parsed = parse_release_date(entry, _platform_name(entry.get("platform")) or "")
            if parsed:
                dates.append(parsed)
    return sorted(dates, key=lambda item: item.sort_key())


@dataclass(frozen=True)
class PlatformRelease:
    """The full-release date of one or more platforms that share it."""

    label: str
    platforms: tuple[str, ...]
    precision: str
    regional: bool = False  # IGDB lists other dates for other regions of these platforms
    first: ReleaseDate | None = None  # the shared date itself, to tell whether it has happened

    def has_happened(self, today: date) -> bool:
        return self.first is not None and self.first.has_happened(today)


def platform_releases(dates: list[ReleaseDate]) -> list[PlatformRelease]:
    """Each platform's earliest full-release date; platforms with the same date are grouped, earliest first."""
    by_platform: dict[str, list[ReleaseDate]] = {}
    for item in dates:
        if item.status == FULL and item.platform:
            by_platform.setdefault(item.platform, []).append(item)
    groups: dict[tuple, dict] = {}
    for platform, items in by_platform.items():
        earliest = min(items, key=lambda item: item.sort_key())
        key = (earliest.sort_key()[:2], earliest.label)
        group = groups.setdefault(key, {"release": earliest, "platforms": [], "regional": False})
        group["platforms"].append(platform)
        group["regional"] = group["regional"] or len({item.label for item in items}) > 1
    return [PlatformRelease(group["release"].label, tuple(sorted(group["platforms"])), group["release"].precision,
                            group["regional"], group["release"])
            for _, group in sorted(groups.items(), key=lambda pair: pair[0])]


def pre_release_groups(dates: list[ReleaseDate]) -> list[dict]:
    """Alpha, beta, early access and advanced access dates, with the platforms that share each one."""
    groups: dict[tuple, dict] = {}
    for item in sorted(dates, key=lambda item: item.sort_key()):
        if item.status not in PRE_RELEASE_KINDS:
            continue
        group = groups.setdefault((item.status, item.label), {"status": item.status, "label": item.label, "platforms": []})
        if item.platform and item.platform not in group["platforms"]:
            group["platforms"].append(item.platform)
    return list(groups.values())


def first_full_release(dates: list[ReleaseDate]) -> ReleaseDate | None:
    """The earliest full release on any platform (TBA only when no platform has a date)."""
    full = [item for item in dates if item.status == FULL]
    return min(full, key=lambda item: item.sort_key()) if full else None


@dataclass(frozen=True)
class ReleaseState:
    """Whether a game is upcoming on `today`, and what can be said about its release."""

    upcoming: bool
    reason: str  # why a game is not upcoming; empty when it is
    first: ReleaseDate | None  # its earliest full release (stated or past)
    early: ReleaseDate | None = None  # a pre-release (alpha, beta, early or advanced access) that has started


def release_state(game_type: int | None, game_status: int | None, dates: list[ReleaseDate],
                  first_release_stamp: int | None, today: date) -> ReleaseState:
    """Apply the upcoming rule (module docstring) to one game."""
    first = first_full_release(dates)
    started = [item for item in dates if item.status in PRE_RELEASE_KINDS and item.has_happened(today)]
    early = min(started, key=lambda item: item.sort_key()) if started else None
    if game_type not in GAME_TYPES:
        return ReleaseState(False, "not a main game, standalone expansion, remake, remaster or expanded game", first)
    if game_status in NOT_UPCOMING_STATUSES:
        return ReleaseState(False, f"IGDB marks it {NOT_UPCOMING_STATUSES[game_status].lower()}", first, early)
    released = [item for item in dates if item.status == FULL and item.has_happened(today)]
    if released:
        return ReleaseState(False, f"released {min(released, key=lambda item: item.sort_key()).label}", first, early)
    if not dates and isinstance(first_release_stamp, int):
        # No readable release dates: IGDB's own first_release_date decides, but only when it is in the past.
        if datetime.fromtimestamp(first_release_stamp, tz=timezone.utc).date() <= today:
            return ReleaseState(False, "IGDB's first release date has passed", first, early)
    return ReleaseState(True, "", first, early)


# ---------------------------------------------------------------------- availability per platform (pure)


@dataclass(frozen=True)
class Availability:
    """Where a game is out and where it is only announced, from IGDB's per-platform release dates on one day.

    available: platforms whose first full release has happened, then platforms the game reached later through
    a compatibility release or a next-gen patch (a group's `first.status` says which; such a date is never a
    first release). A platform out through its full release appears nowhere else;
    early: platforms where alpha, beta, early or advanced access has started but no full release has;
    announced: platforms whose full release has not happened: a later day, a month, quarter or year that is
    not over, or TBA (an early-access platform can also be here, with its announced full release);
    undated: platforms IGDB lists for the game without any full-release date or started pre-release.
    """

    available: tuple[PlatformRelease, ...] = ()
    early: tuple[dict, ...] = ()  # {"status", "label", "platforms"}, earliest first
    announced: tuple[PlatformRelease, ...] = ()
    undated: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """Whether IGDB says anything about the game's platforms."""
        return bool(self.available or self.early or self.announced or self.undated)

    @property
    def summary(self) -> str:
        """A few words for the game as a whole: "Out now", "Out on some platforms", "Early access", ..."""
        if self.available:
            return "Out on some platforms, more announced" if self.announced else "Out now"
        if self.early:
            return "Early access"
        return "Not out yet" if self.announced else "Release not announced"


def _earliest_started(dates: list[ReleaseDate], kinds: frozenset, today: date, skip: set[str]) -> dict[str, ReleaseDate]:
    """Each platform's earliest date of one of `kinds` that has happened by `today`, leaving out `skip` platforms."""
    found: dict[str, ReleaseDate] = {}
    for item in sorted(dates, key=lambda item: item.sort_key()):
        if item.status in kinds and item.platform and item.platform not in skip and item.has_happened(today):
            found.setdefault(item.platform, item)
    return found


def availability(dates: list[ReleaseDate], listed_platforms: list[str], today: date) -> Availability:
    """Split a game's platforms into out, early access, announced and undated on `today` (see Availability)."""
    groups = platform_releases(dates)
    released = [group for group in groups if group.has_happened(today)]
    announced = tuple(group for group in groups if not group.has_happened(today))
    released_platforms = {platform for group in released for platform in group.platforms}
    # A newer platform the game reached through a compatibility release or a next-gen patch is available too.
    later = _earliest_started(dates, RE_RELEASE_KINDS, today, released_platforms)
    later_groups: dict[tuple, tuple[ReleaseDate, list[str]]] = {}
    for platform, item in sorted(later.items(), key=lambda pair: pair[1].sort_key()):
        later_groups.setdefault((item.status, item.label), (item, []))[1].append(platform)
    available = tuple(sorted(
        released + [PlatformRelease(item.label, tuple(sorted(platforms)), item.precision, False, item)
                    for item, platforms in later_groups.values()],
        key=lambda group: group.first.sort_key()))
    started = _earliest_started(dates, PRE_RELEASE_KINDS, today, released_platforms | set(later))
    early: dict[tuple, dict] = {}
    for platform, item in sorted(started.items(), key=lambda pair: pair[1].sort_key()):
        group = early.setdefault((item.status, item.label), {"status": item.status, "label": item.label,
                                                             "platforms": []})
        group["platforms"].append(platform)
    dated = {platform for group in groups for platform in group.platforms} | set(started) | set(later)
    undated = tuple(sorted({platform for platform in listed_platforms if platform and platform not in dated}))
    return Availability(available, tuple(early.values()), announced, undated)


def first_release_happened(dates: list[ReleaseDate], today: date) -> ReleaseDate | None:
    """The earliest full release, on any platform, that has happened by `today`."""
    happened = [item for item in dates if item.status == FULL and item.has_happened(today)]
    return min(happened, key=lambda item: item.sort_key()) if happened else None


def recent_release(game_type: int | None, game_status: int | None, dates: list[ReleaseDate], today: date,
                   window: timedelta = RECENT_WINDOW) -> ReleaseDate | None:
    """The game's first full release when it makes the game a recent release on `today`, else None.

    The first release must have happened, and its stated period must have started at most `window` ago:
    a day counts from that day, a month, quarter or year from its first day. So a game dated only "2026"
    is never called recent, because it may have come out in January.
    """
    if game_type not in GAME_TYPES or game_status in NOT_LISTABLE_STATUSES:
        return None
    first = first_release_happened(dates, today)
    if first is None or today - first.period()[0] > window:
        return None
    return first


def list_kind(row, today: date) -> str | None:
    """Which Want to Play list a stored IGDB record belongs to on `today`: UPCOMING, RECENT or None."""
    dates = stored_dates(row)
    if release_state(row["game_type"], row["game_status"], dates, row["first_release_date"], today).upcoming:
        return UPCOMING
    if recent_release(row["game_type"], row["game_status"], dates, today):
        return RECENT
    return None


def twitch_listable(row) -> bool:
    """Whether a Twitch category's IGDB game may be listed: a game type Trending covers, not cancelled or rumored."""
    return row["game_type"] in GAME_TYPES and row["game_status"] not in NOT_LISTABLE_STATUSES


def twitch_games(conn: sqlite3.Connection, top: list, decisions: dict) -> tuple[list[tuple], Counter]:
    """The categories of a stored Twitch top list that Trending lists, in Twitch's order: games, and the categories
    a reviewer marked as a game series.

    Each is a (twitch_top_games row, IGDB record of its game, twitch.CategoryGame) triple; a series category has no
    record (None), as it stands for no single game. The game comes from twitch.resolve_category: a reviewer's decision
    (`decisions`: curated twitch_category_decisions), else Twitch's igdb_id and IGDB's own record of the category,
    which must agree. The others are counted by reason: "no_game" (nothing names a game: Just Chatting, Slots),
    "unresolved" (they name different games, or a reviewer decided the category is not a game), "not_read" (the game's
    IGDB record was not read yet) and "other_kind" (DLC, bundles, cancelled or rumored games).
    """
    matches = [(row, twitch.resolve_category(row["igdb_id"], json.loads(row["igdb_links"] or "[]"),
                                             decisions.get(row["category_id"])))
               for row in top]
    records = store.igdb_game_rows(conn, sorted({match.igdb_id for _, match in matches if match.igdb_id}))
    games, left_out = [], Counter()
    for row, match in matches:
        record = records.get(match.igdb_id) if match.status == "matched" else None
        if match.status == "series":
            games.append((row, None, match))
        elif match.status != "matched":
            left_out[match.status] += 1
        elif record is None:
            left_out["not_read"] += 1
        elif not twitch_listable(record):
            left_out["other_kind"] += 1
        else:
            games.append((row, record, match))
    return games, left_out


# ---------------------------------------------------------------------- release-date changes (pure)


@dataclass(frozen=True)
class DateChange:
    """A platform's full-release date as IGDB stated it at the previous refresh and now."""

    platform: str
    before: str | None  # None: the platform had no date before
    after: str | None  # None: the platform has no date now
    kind: str  # later | earlier | narrowed | announced | withdrawn | added | removed | updated

    @property
    def words(self) -> str:
        return {
            "later": "moved later", "earlier": "moved earlier", "narrowed": "made more precise",
            "announced": "date announced", "withdrawn": "date withdrawn (now to be announced)",
            "added": "platform added", "removed": "platform no longer listed", "updated": "date changed",
        }[self.kind]


def release_summary(dates: list[ReleaseDate]) -> dict[str, dict]:
    """Each platform's earliest full-release date, as stored to notice changes between refreshes."""
    summary = {}
    for group in platform_releases(dates):
        for platform in group.platforms:
            summary[platform] = {"label": group.label}
    return summary


def _label_period(label: str) -> tuple[date, date] | None:
    """The period of a stored label ("23 Feb 2027", "Feb 2027", "Q1 2027", "2027" or "TBA")."""
    parts = label.split()
    try:
        if len(parts) == 3:
            day = date(int(parts[2]), MONTHS.index(parts[1]) + 1, int(parts[0]))
            return day, day
        if len(parts) == 2 and parts[0].startswith("Q"):
            return ReleaseDate("", "quarter", FULL, int(parts[1]), quarter=int(parts[0][1:])).period()
        if len(parts) == 2:
            return ReleaseDate("", "month", FULL, int(parts[1]), MONTHS.index(parts[0]) + 1).period()
        if len(parts) == 1 and parts[0].isdigit():
            return ReleaseDate("", "year", FULL, int(parts[0])).period()
    except ValueError:
        return None
    return None


def compare_release_summaries(before: dict[str, dict], after: dict[str, dict]) -> list[DateChange]:
    """What changed between two stored release summaries, one entry per platform that changed."""
    changes = []
    for platform in sorted(set(before) | set(after)):
        old = before.get(platform, {}).get("label")
        new = after.get(platform, {}).get("label")
        if old == new:
            continue
        if old is None:
            changes.append(DateChange(platform, None, new, "added"))
            continue
        if new is None:
            changes.append(DateChange(platform, old, None, "removed"))
            continue
        old_period, new_period = _label_period(old), _label_period(new)
        if old_period is None and new_period is not None:
            kind = "announced"
        elif new_period is None and old_period is not None:
            kind = "withdrawn"
        elif old_period is None or new_period is None:
            kind = "updated"
        elif new_period[0] > old_period[1]:
            kind = "later"
        elif new_period[1] < old_period[0]:
            kind = "earlier"
        elif old_period[0] <= new_period[0] and new_period[1] <= old_period[1]:
            kind = "narrowed"
        else:
            kind = "updated"
        changes.append(DateChange(platform, old, new, kind))
    return changes


# ---------------------------------------------------------------------- PopScore values (pure)


@dataclass(frozen=True)
class SignalEntry:
    """One game's place in a PopScore ranking as fetched: 1 is the highest value."""

    igdb_id: int
    value: float
    position: int
    calculated_at: datetime | None


def parse_popularity(rows: list[dict]) -> tuple[list[SignalEntry], int]:
    """Valid PopScore rows in IGDB's order, highest value first, and how many rows were skipped.

    A row is skipped when its game id is not a positive whole number, its value is missing, not a
    number, negative or not finite, or the game already appeared. Equal values are ordered by IGDB id,
    so the order never depends on how IGDB happened to return ties.
    """
    seen: set[int] = set()
    valid = []
    skipped = 0
    for row in rows:
        game_id, value = row.get("game_id"), row.get("value")
        if (not isinstance(game_id, int) or isinstance(game_id, bool) or game_id <= 0 or game_id in seen
                or not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            skipped += 1
            continue
        seen.add(game_id)
        stamp = row.get("calculated_at")
        calculated = (datetime.fromtimestamp(stamp, tz=timezone.utc)
                      if isinstance(stamp, int) and not isinstance(stamp, bool) else None)
        valid.append((game_id, float(value), calculated))
    valid.sort(key=lambda item: (-item[1], item[0]))
    return [SignalEntry(game_id, value, position, calculated)
            for position, (game_id, value, calculated) in enumerate(valid, 1)], skipped


@dataclass(frozen=True)
class PositionChange:
    """A game's position in a signal's ranking at the newest snapshot and at a baseline about a week earlier."""

    before: int
    after: int
    before_at: datetime
    after_at: datetime

    @property
    def places(self) -> int:
        """Places gained (positive) or lost (negative): a smaller position number is higher."""
        return self.before - self.after


def position_changes(history: dict[int, list]) -> dict[int, PositionChange]:
    """Position changes per IGDB id, from snapshot rows (captured_at, position), oldest first.

    The same rule as the Trending changes: the newest snapshot against the newest one taken
    CHANGE_WINDOW to MAX_BASELINE_AGE earlier. Without such a pair there is no change.
    """
    changes = {}
    for igdb_id, rows in history.items():
        snapshots = [(datetime.fromisoformat(row["captured_at"]), row["position"]) for row in rows]
        if not snapshots:
            continue
        latest_at, latest = snapshots[-1]
        found = baseline_snapshot(snapshots, latest_at)
        if found:
            changes[igdb_id] = PositionChange(found[1], latest, found[0], latest_at)
    return changes


# ---------------------------------------------------------------------- Steam matching (pure)


@dataclass(frozen=True)
class SteamMatch:
    """Whether an IGDB game has a verified Steam app."""

    status: str  # verified | needs_review | none | rejected
    appid: int | None
    reason: str


def judge_steam_match(name: str, steam_uids: list[int], steam_items: dict[int, dict],
                      decision: SteamMatchDecision | None) -> SteamMatch:
    """Decide the Steam app of one IGDB game from IGDB's Steam ids and what Steam says about them.

    A reviewer's decision (curated/steam_matches.csv) wins. Otherwise only one Steam id, a Steam game
    of that id, and the same normalized name make a verified match; anything else waits for review.
    """
    if decision is not None:
        if decision.decision == "match":
            return SteamMatch("verified", decision.steam_appid,
                              f"Confirmed by {decision.checked_by} on {decision.checked_on} (curated/steam_matches.csv).")
        return SteamMatch("rejected", None, f"{decision.checked_by} decided on {decision.checked_on} that no Steam "
                                            "app is this game (curated/steam_matches.csv).")
    if not steam_uids:
        return SteamMatch("none", None, "IGDB lists no Steam app for this game.")
    if len(steam_uids) > 1:
        return SteamMatch("needs_review", None, f"IGDB lists {len(steam_uids)} Steam apps ({', '.join(map(str, steam_uids))}); "
                                                "a reviewer must choose in curated/steam_matches.csv.")
    appid = steam_uids[0]
    item = steam_items.get(appid)
    if item is None:
        return SteamMatch("needs_review", None, f"Steam returned no store data for app {appid}, so the match cannot be checked.")
    if item.get("type") != 0:
        return SteamMatch("needs_review", None, f"Steam lists app {appid} as something other than a game.")
    steam_name = str(item.get("name") or "")
    if not steam_name or normalize_for_search(steam_name) != normalize_for_search(name):
        return SteamMatch("needs_review", None, f"Steam calls app {appid} {steam_name!r}, but IGDB calls the game {name!r}.")
    return SteamMatch("verified", appid, f"IGDB lists Steam app {appid}, and Steam's name for it is the same.")


# ---------------------------------------------------------------------- IGDB records (pure)


def _safe_url(value) -> str | None:
    """An http(s) URL, or None: links shown on the page never use another scheme."""
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) and " " not in value else None


def _host(url: str) -> str:
    """A URL's lower-case host without a leading "www.": "https://www.RockstarGames.com/VI" -> "rockstargames.com"."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:  # a malformed address, such as an unclosed IPv6 bracket
        return ""
    return host[4:] if host.startswith("www.") else host


def _on_domain(host: str, domain: str) -> bool:
    """Whether `host` is `domain` or one of its subdomains ("store.playstation.com" is on "playstation.com")."""
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def verify_link(kind: str, url: str, company_hosts: set[str]) -> bool:
    """Whether a link IGDB lists for a game can be shown as verified.

    IGDB marks community-entered websites as not trusted (its `trusted` flag was false for every official
    site checked on 2026-11-19), so Game Librarian checks them itself: a store link must point to that
    store's own domain, and an official website must be on the domain of an official website that IGDB
    lists for the game's publisher or developer (two separate IGDB records agree).
    """
    host = _host(url)
    if kind == OFFICIAL:
        return any(_on_domain(host, company) for company in company_hosts)
    return any(_on_domain(host, domain) for domain in STORE_DOMAINS.get(kind, ()))


def record_from_igdb(game: dict) -> dict | None:
    """The stored form of one IGDB game record, or None when it lacks an id or a name."""
    igdb_id, name = game.get("id"), str(game.get("name") or "").strip()
    if not isinstance(igdb_id, int) or isinstance(igdb_id, bool) or igdb_id <= 0 or not name:
        return None
    dates = release_dates(game)
    companies = [item for item in game.get("involved_companies") or [] if isinstance(item, dict)]

    def company_names(role: str) -> list[str]:
        names = []
        for item in companies:
            company = item.get("company")
            company_name = company.get("name") if isinstance(company, dict) else None
            if item.get(role) is True and isinstance(company_name, str) and company_name.strip() not in names:
                names.append(company_name.strip())
        return names

    company_hosts = set()  # official websites of the game's publishers and developers, to verify its own
    for item in companies:
        company = item.get("company")
        if (item.get("publisher") is True or item.get("developer") is True) and isinstance(company, dict):
            for site in company.get("websites") or []:
                if isinstance(site, dict) and site.get("type") == 1 and _safe_url(site.get("url")):
                    company_hosts.add(_host(site["url"]))
    company_hosts.discard("")
    websites = []
    for site in game.get("websites") or []:
        if isinstance(site, dict) and site.get("type") in WEBSITE_KINDS and _safe_url(site.get("url")):
            kind = WEBSITE_KINDS[site["type"]]
            entry = {"kind": kind, "url": site["url"], "verified": verify_link(kind, site["url"], company_hosts)}
            if entry not in websites:
                websites.append(entry)
    websites.sort(key=lambda site: list(WEBSITE_KINDS.values()).index(site["kind"]))
    cover = game.get("cover")
    image_id = cover.get("image_id") if isinstance(cover, dict) else None
    platforms = []
    for platform in game.get("platforms") or []:
        platform_name = _platform_name(platform)
        if platform_name and platform_name not in platforms:
            platforms.append(platform_name)
    return {
        "igdb_id": igdb_id,
        "name": name,
        "slug": str(game.get("slug") or "") or None,
        "igdb_url": _safe_url(game.get("url")),
        "summary": " ".join(str(game.get("summary") or "").split()) or None,
        "game_type": game.get("game_type"),
        "game_status": game.get("game_status"),
        "first_release_date": game.get("first_release_date") if isinstance(game.get("first_release_date"), int) else None,
        "cover_image_id": image_id if isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id) else None,
        "platforms": sorted(platforms),
        "release_dates": [asdict(item) for item in dates],
        "release_summary": release_summary(dates),
        "publishers": company_names("publisher"),
        "developers": company_names("developer"),
        "websites": websites,
        "steam_appids": igdb.steam_appids(game),
    }


def stored_dates(row) -> list[ReleaseDate]:
    """The release dates of a stored igdb_games row."""
    return [ReleaseDate(**item) for item in json.loads(row["release_dates"] or "[]")]


def cover_url(image_id: str | None) -> str | None:
    return COVER_URL.format(image_id=image_id) if image_id else None


# ---------------------------------------------------------------------- refresh (network)


@dataclass
class RefreshReport:
    """What one upcoming refresh did; saved in source_runs."""

    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    failed: bool = False  # nothing could be refreshed (IGDB unavailable)

    def status(self) -> str:
        if self.failed:
            return "failed"
        return "partial" if self.errors else "success"

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "errors": self.errors[:25]}


def refresh_due(conn: sqlite3.Connection, now: datetime) -> bool:
    """Whether the last successful refresh is older than REFRESH_AFTER (or there was none)."""
    last = store.last_source_run(conn, SOURCE)
    return last is None or now - datetime.fromisoformat(last["started_at"]) >= REFRESH_AFTER


def refresh_upcoming(conn: sqlite3.Connection, client: igdb.IgdbClient, steam: SteamClient | None,
                     curated: CuratedData, *, catalog_appids: list[int],
                     twitch_client: twitch.TwitchClient | None = None, log: Callable[[str], None] = print,
                     now: Callable[[], datetime] = utc_now) -> RefreshReport:
    """Refresh both signals (upcoming and recent games), Twitch's top games, the IGDB records of every listed
    and catalog game, their Steam matches and the catalog's IGDB links.

    Stored data is replaced only by what the services returned. When IGDB fails, the previous lists stay
    and the run is recorded as failed; when Twitch fails (or no `twitch_client` is given), the previous
    Twitch list stays until it is 24 hours old. Records of games that left the lists are still refreshed,
    so their pages and wishlist entries keep working (a game that got released shows its release).
    """
    started = now()
    run_id = store.start_source_run(conn, SOURCE, now=_iso(started))
    report = RefreshReport()
    try:
        _refresh(conn, client, steam, curated, catalog_appids, report, log, started, twitch_client)
    except igdb.IgdbError as exc:
        report.failed = not report.counts.get("records_refreshed")  # partial when records were saved first
        report.errors.append(f"IGDB: {exc}")
        log(f"IGDB failed: {exc}")
    except BaseException as exc:  # also Ctrl+C: the run is still recorded before stopping
        report.failed = True
        report.errors.append(f"refresh stopped: {exc!r}")
        store.finish_source_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    store.finish_source_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report


def _refresh(conn, client, steam, curated, catalog_appids, report, log, started, twitch_client) -> None:
    today = started.date()
    store.purge_twitch_top_games(conn, _iso(started - twitch.KEEP_FOR))  # Twitch's 24-hour caching limit
    rankings = {}
    for signal in SIGNALS:
        entries, skipped = parse_popularity(client.popularity(signal.popularity_type, limit=signal.depth))
        rankings[signal.key] = entries
        report.counts[f"{signal.key}_values"] = len(entries)
        if skipped:
            report.counts[f"{signal.key}_rows_skipped"] = skipped
        log(f"[upcoming] {signal.title}: {len(entries)} PopScore values read")
    # Screen every ranked game cheaply, then fetch full records for the upcoming and recent ones that are kept.
    ranked_ids = {entry.igdb_id for entries in rankings.values() for entry in entries}
    screens = {game["id"]: game for game in client.games_by_id(sorted(ranked_ids), SCREEN_FIELDS) if "id" in game}
    tracked: dict[str, list[SignalEntry]] = {}
    for signal in SIGNALS:
        upcoming_entries, recent_entries = [], []
        for entry in rankings[signal.key]:
            game = screens.get(entry.igdb_id)
            if not game:
                continue
            dates = release_dates(game)
            if release_state(game.get("game_type"), game.get("game_status"), dates, game.get("first_release_date"),
                             today).upcoming:
                upcoming_entries.append(entry)
            elif signal.tracks_recent_releases and recent_release(game.get("game_type"), game.get("game_status"),
                                                                  dates, today):
                recent_entries.append(entry)
        # One snapshot per signal holds both kinds, so a game keeps its position history when it comes out.
        kept = upcoming_entries[:TRACKED_PER_SIGNAL] + recent_entries[:TRACKED_PER_SIGNAL]
        tracked[signal.key] = sorted(kept, key=lambda entry: entry.position)
        report.counts[f"{signal.key}_upcoming"] = len(upcoming_entries)
        if signal.tracks_recent_releases:
            report.counts[f"{signal.key}_recent"] = len(recent_entries)
    top_games = _twitch_top_games(twitch_client, report, log)
    twitch_links = _twitch_links(client, top_games, report)
    links = _catalog_links(client, catalog_appids)
    wanted = {entry.igdb_id for entries in tracked.values() for entry in entries}
    wanted |= {category.igdb_id for category in top_games or [] if category.igdb_id}
    wanted |= {igdb_id for games in twitch_links.values() for igdb_id in games}  # such as Dota 2, without igdb_id
    # Every reviewed game, in Twitch's list or not: most-watched also asks about reviewed categories outside it.
    wanted |= {decision.igdb_id for decision in curated.twitch_category_decisions.values() if decision.igdb_id}
    wanted |= {igdb_id for status, igdb_id in links.values() if status == "linked"}  # platforms of catalog games
    existing = store.igdb_game_rows(conn)
    records = {}
    for game in client.games_by_id(sorted(wanted | set(existing)), RECORD_FIELDS):
        record = record_from_igdb(game)
        if record:
            records[record["igdb_id"]] = record
    matches = _steam_matches(records, steam, curated, report)
    changes = []
    for igdb_id, record in records.items():
        if igdb_id in existing:
            before = json.loads(existing[igdb_id]["release_summary"] or "{}")
            changes += [(igdb_id, change) for change in compare_release_summaries(before, record["release_summary"])]
    moment = _iso(started)
    store.save_igdb_games(conn, list(records.values()), matches, changes, now=moment)
    report.counts["records_refreshed"] = len(records)
    report.counts["release_date_changes"] = len(changes)
    for signal in SIGNALS:
        kept = [entry for entry in tracked[signal.key] if entry.igdb_id in records]
        store.save_popularity_snapshot(conn, signal.key, kept, now=moment)
    if top_games is not None:
        store.save_twitch_top_games(conn, top_games, igdb_links=twitch_links, now=moment)
    store.save_steam_igdb_links(conn, links, now=moment)
    report.counts["catalog_games_linked"] = sum(1 for status, _ in links.values() if status == "linked")
    log(f"[upcoming] {len(records)} IGDB records saved, {report.counts['catalog_games_linked']} catalog games "
        "linked to IGDB")


def _twitch_top_games(client: twitch.TwitchClient | None, report: RefreshReport,
                      log: Callable[[str], None]) -> list[twitch.TopCategory] | None:
    """Twitch's top categories now, or None without a client or when Twitch fails (the stored list then stays)."""
    if client is None:
        return None
    try:
        categories = twitch.parse_top_games(client.top_games())
    except twitch.TwitchError as exc:  # messages never contain the credentials or the token
        report.errors.append(f"Twitch: {exc}")
        log(f"[upcoming] Twitch top games failed: {exc}")
        return None
    report.counts["twitch_categories"] = len(categories)
    report.counts["twitch_categories_with_igdb_id"] = sum(1 for category in categories if category.igdb_id)
    log(f"[upcoming] Twitch: {len(categories)} top categories read")
    return categories


def _twitch_links(client: igdb.IgdbClient, top_games: list[twitch.TopCategory] | None,
                  report: RefreshReport) -> dict[str, list[int]]:
    """For each category of Twitch's list, the IGDB games whose IGDB record lists that Twitch category.

    One IGDB query for the whole list (external_games, source Twitch). It confirms the igdb_id Twitch gives, adds a
    game Twitch gives none for (Dota 2 on 2026-11-21) and exposes a disagreement (twitch.resolve_category).
    """
    if not top_games:
        return {}
    found: dict[str, set[int]] = {}
    for row in client.games_for_twitch_categories([category.category_id for category in top_games]):
        uid, game = str(row.get("uid") or ""), row.get("game")
        if uid.isdigit() and isinstance(game, int) and not isinstance(game, bool):
            found.setdefault(uid, set()).add(game)
    report.counts["twitch_categories_in_igdb"] = len(found)
    return {uid: sorted(games) for uid, games in found.items()}


def _steam_matches(records: dict[int, dict], steam: SteamClient | None, curated: CuratedData,
                   report: RefreshReport) -> dict[int, SteamMatch]:
    """Judge the Steam app of every record. Without Steam answers a record keeps its previous match."""
    wanted = sorted({record["steam_appids"][0] for record in records.values()
                     if len(record["steam_appids"]) == 1 and record["igdb_id"] not in curated.steam_match_decisions})
    items: dict[int, dict] = {}
    checked = steam is not None
    if steam is not None:
        try:
            for start in range(0, len(wanted), 50):
                items.update(steam.get_item_summaries(wanted[start:start + 50]))
        except SteamError as exc:
            checked = False
            report.errors.append(f"Steam check of IGDB matches failed: {exc}")
    matches = {}
    for igdb_id, record in records.items():
        decision = curated.steam_match_decisions.get(igdb_id)
        needs_steam = decision is None and len(record["steam_appids"]) == 1
        if needs_steam and not checked:
            continue  # keep the stored match until Steam can be asked
        matches[igdb_id] = judge_steam_match(record["name"], record["steam_appids"], items, decision)
    report.counts["steam_verified"] = sum(1 for match in matches.values() if match.status == "verified")
    return matches


def _catalog_links(client: igdb.IgdbClient, appids: list[int]) -> dict[int, tuple[str, int | None]]:
    """Each catalog app's IGDB game: linked when exactly one IGDB game lists the Steam app, else none/ambiguous."""
    games_by_appid: dict[int, set[int]] = {appid: set() for appid in appids}
    for row in client.games_for_steam_appids(appids):
        uid, game = str(row.get("uid") or ""), row.get("game")
        if uid.isdigit() and int(uid) in games_by_appid and isinstance(game, int):
            games_by_appid[int(uid)].add(game)
    links = {}
    for appid, games in games_by_appid.items():
        if len(games) == 1:
            links[appid] = ("linked", next(iter(games)))
        else:
            links[appid] = ("ambiguous" if games else "none", None)
    return links


def game_key_for(row, catalog_appids: set[int], linked: dict[int, int] | None = None) -> str:
    """The key a stored IGDB game opens, so one game never has two pages or two wishlist entries.

    Its verified Steam app when that app is in the catalog; else the catalog app whose only IGDB game it is
    (`linked`, from store.catalog_links_by_igdb: IGDB's own Steam record); else its IGDB key "igdb-<id>".
    """
    if row["steam_match"] == "verified" and row["steam_appid"] in catalog_appids:
        return str(row["steam_appid"])
    appid = (linked or {}).get(row["igdb_id"])
    if appid in catalog_appids:
        return str(appid)
    return igdb_key(row["igdb_id"])
