"""FastAPI web app: a read-only, server-rendered view of the stored catalog.

Page requests never call Steam. They read the SQLite catalog written by the sync
command and the curated files, so the site keeps serving the last saved data
when Steam is unavailable. The five-year ranking is recalculated on every request
from the verified awards, the stored review counts and today's date (see
ranking.py), so it follows the latest sync and its window moves forward every 1 January.

Pages, in the order of the site navigation:
- Discover (/): views share the same search, filters, sort orders and pagination.
  "Top-ranked games" (the default) shows only the games released in the five-year window,
  "All games" shows the whole catalog, and collections (catalog.py) show the games that meet
  a documented rule. A card's number is always its place in the five-year ranking of the
  whole catalog; no view, filter or sort recalculates it. Details open in a dialog
  (/games/{key}/panel) or as a full page (/games/{key}); a key is a Steam app id or igdb-<id> (identity.py).
  The details read only stored data. "Watch it live" asks /games/{key}/live afterwards, which calls Twitch
  at most every few minutes per game (twitch.py); nothing else in a page request calls an outside service.
- Trending (/trending): three parts, each list ranked by one named signal. Upcoming & Anticipated and
  Trending now & recently released cover every platform (IGDB PopScore and Twitch's top games, saved by
  the upcoming refresh; upcoming.py, twitch.py). Steam activity covers the Steam catalog only (stored Steam
  snapshots, trending.py). Every game shows where it is out and where it is only announced (IGDB).
- Data Stories (/stories): hand-checked series timelines (stories.py).
- Dashboard (/dashboard...): Trending games first (/dashboard), charting the Trending lists' games from the
  stored snapshots, placing the tracked upcoming games on a release radar (dashboard.release_radar), and
  analyzing Most watched on Twitch against Most wishlisted on Steam and Steam reviews instead of repeating the
  lists (_list_analysis); then the Power BI report's measures as HTML (dashboard.py): Player reception
  (/dashboard/reception), Awards and About the data. Old addresses redirect (see dashboard_home).
  Trending games' Estimated Steam copies sold chart (Gamalytic) is sent only to a browser on the computer that
  runs the site (is_local_request): Gamalytic's terms forbid sharing its data.
- Wishlist (/wishlist): the browser-local Game Librarian wishlist.

Run with `python -m game_librarian serve` (uses the `create_app` factory).
"""

from __future__ import annotations

import ipaddress
import json
import math
import threading
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, catalog, critics, dashboard, gamalytic, list_history, pricing, ranking, related, stories, \
    store, trending, twitch, upcoming
from .catalog import ALL_VIEW, COLLECTION_KEYS, DEFAULT_VIEW, LABEL_ORDER, LEGACY_NO_LABEL, NO_VERIFIED_LABEL, \
    SORT_ALIASES, SORT_OPTIONS, SORTS, STEAM_REVIEW_LABELS, TOP_VIEW, VIEWS, CatalogGame
from .config import PACKAGE_DIR, STORE_PAGE_URL, Settings, load_settings
from .curated import PROMOTED, Award, CuratedCache, CuratedData, award_display_group
from .identity import igdb_key, parse_key
from .prepare import concise_description, normalize_for_search, percent_positive, shorten
from .publishers import PublisherIndex, build_index
from .report_export import ReportTables, collect_tables

PAGE_SIZE = 12  # two cards per row on desktop: six rows per page
SIDEBAR_TAG_LIMIT = 40
MAX_SELECTED_TAGS = 10
MAX_QUERY_LENGTH = 100
CARD_TAG_COUNT = 3
CARD_AWARD_COUNT = 3  # a card lists this many awards; the details list them all
# Short names of the five-year ranking's award groups for the cards; the details and the ranking explanation
# use the full names (ranking.AWARD_GROUP_NAMES), and a card's first listed award is the one that sets its group.
CARD_GROUP_LABELS = {
    1: "The Game Awards GOTY", 2: "BAFTA Best Game", 3: "D.I.C.E. GOTY", 4: "Game Developers Choice GOTY",
    5: "Steam Awards GOTY", 6: "Other award win", 7: "Nominated, no win", ranking.NO_AWARD_GROUP: "No award recorded",
}
DETAILS_TAG_COUNT = 12
DETAILS_EXCERPT_CHARS = 280  # keeps each review excerpt to a few lines in the details dialog
DETAILS_STALE_AFTER = timedelta(days=14)
REVIEWS_STALE_AFTER = timedelta(days=7)
STEAM_REVIEWS_URL = "https://steamcommunity.com/app/{appid}/reviews/"

LABEL_FILTERS = (*LABEL_ORDER, NO_VERIFIED_LABEL)
# The Player reception page's filters. That page was /dashboard before 0.10; a /dashboard link with one of these
# is redirected to /dashboard/reception, so bookmarks and shared filter links keep working.
RECEPTION_FILTERS = ("tag", "label", "award_status")
ANY_AWARD = "any"
# Steam's game-mode categories, by their English names (prepare.MODE_CATEGORY_IDS selects them by id).
MODE_FILTERS = ("Single-player", "Multi-player", "Co-op", "Online Co-op", "Shared/Split Screen Co-op",
                "LAN Co-op", "PvP", "Online PvP", "Shared/Split Screen PvP", "Shared/Split Screen",
                "Cross-Platform Multiplayer", "MMO")


# ---------------------------------------------------------------------- formatting helpers


def _parse_time(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if value else None


def format_date(value: str | datetime | None) -> str:
    """'2026-11-09T21:47:02+00:00' -> '9 Nov 2026'."""
    moment = _parse_time(value)
    return f"{moment.day} {moment:%b %Y}" if moment else "never"


def format_datetime(value: str | datetime | None) -> str:
    """'2026-11-09T21:47:02+00:00' -> '9 Nov 2026, 21:47 UTC'."""
    moment = _parse_time(value)
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC" if moment else "never"


def format_day(value: date | None) -> str:
    """date(2026, 11, 9) -> '9 Nov 2026' (the same style as format_date)."""
    return f"{value.day} {value:%b %Y}" if value else ""


def format_number(value: int | None) -> str:
    return f"{value or 0:,}"


def format_list(items) -> str:
    """An English list: ['A'] -> 'A', ['A', 'B'] -> 'A and B', ['A', 'B', 'C'] -> 'A, B and C'."""
    items = [str(item) for item in items]
    return items[0] if len(items) == 1 else (f"{', '.join(items[:-1])} and {items[-1]}" if items else "")


def format_signed(value: int | None) -> str:
    """A change with its sign: 1234 -> '+1,234', -56 -> '−56' (a minus sign), 0 -> '0'."""
    value = value or 0
    return f"+{value:,}" if value > 0 else (f"−{-value:,}" if value < 0 else "0")


def format_hours(minutes: int | None) -> str:
    """Playtime in minutes -> '0.5 h', '12.3 h', '140 h'."""
    if minutes is None:
        return "unknown"
    hours = minutes / 60
    return f"{hours:.1f} h" if hours < 100 else f"{hours:,.0f} h"


def review_window_label(day_range: int | None) -> str:
    """Describe the time window Steam searched for helpful reviews."""
    if day_range is None:
        return ""
    if day_range == 0:
        return "from all time"
    if day_range % 365 == 0:
        years = day_range // 365
        return "from the past year" if years == 1 else f"from the past {years} years"
    return f"from the past {day_range} days"


def _older_than(value: str | None, age: timedelta, now: datetime) -> bool:
    moment = _parse_time(value)
    return moment is not None and now - moment > age


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Headers that a proxy or tunnel adds when it passes on another computer's request.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip")


def is_local_request(request: Request) -> bool:
    """True when the browser runs on the computer that runs the site: a loopback client address, and no proxy headers.

    Only such a request gets Gamalytic's sales estimates, because Gamalytic's terms forbid making its data accessible
    to anyone else (gamalytic.py). A request passed on by a proxy is never local, even when the proxy itself runs here.
    """
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:  # no address, or a name instead of one (the test client's "testclient")
        return False
    if getattr(address, "ipv4_mapped", None):  # ::ffff:127.0.0.1 on a dual-stack socket
        address = address.ipv4_mapped
    return address.is_loopback and not any(name in request.headers for name in FORWARDING_HEADERS)


# ---------------------------------------------------------------------- catalog query


@dataclass(frozen=True)
class AwardChoices:
    """The award filter values that exist in the verified, Steam-mapped curated data."""

    organizations: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    years: tuple[int, ...] = ()  # newest first


def award_choices(curated: CuratedData) -> AwardChoices:
    """Organizations and categories in file order, and award years newest first."""
    shown = [award for award in curated.awards if award.review_status == "verified" and award.appid]
    return AwardChoices(
        organizations=tuple(dict.fromkeys(award.organization for award in shown)),
        categories=tuple(dict.fromkeys(award.category for award in shown)),
        years=tuple(sorted({award.year for award in shown}, reverse=True)),
    )


def publisher_index(rows, curated: CuratedData) -> PublisherIndex:
    """Group the Steam publisher names of catalog rows (store.matching_games), applying the curated aliases."""
    return build_index({row["appid"]: json.loads(row["publishers"] or "[]") for row in rows},
                       curated.publisher_aliases)


@dataclass(frozen=True)
class CatalogQuery:
    """The view, search text, filters and sort order of a catalog page. Every link keeps them."""

    q: str = ""
    tags: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    publisher: str = ""  # a publisher's shown name (publishers.py); "" = any publisher
    reception: tuple[str, ...] = ()  # Steam review summary labels; a game needs any one of them
    award: str = ""  # "" = any category, "any" = any listed award, otherwise one award category
    award_org: str = ""  # "" = any organization
    award_year: str = ""  # "" = any award year, otherwise a four-digit year
    winners: bool = False  # only award winners (matching the other award choices)
    sort: str = ""  # "" = the view's default order
    view: str = DEFAULT_VIEW

    @classmethod
    def from_params(cls, params, choices: AwardChoices, publishers: PublisherIndex | None = None) -> CatalogQuery:
        """Read the query string. Unknown or invalid values fall back to the defaults.

        A publisher is accepted in any spelling Steam uses ("SQUARE ENIX", "Square Enix Co., Ltd.")
        and stored under the name the filter shows; a publisher not in the catalog is ignored.
        """
        tags: list[str] = []
        for tag in params.getlist("tag"):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
        # "Unclassified" is the pre-0.8 name of "No verified label", so older links keep their filter.
        wanted_labels = {NO_VERIFIED_LABEL if label == LEGACY_NO_LABEL else label for label in params.getlist("label")}
        wanted_modes = set(params.getlist("mode"))
        wanted_reception = set(params.getlist("reception"))
        found = publishers.find(params.get("publisher", "")) if publishers else None
        award = params.get("award", "").strip()
        award_org = params.get("award_org", "").strip()
        award_year = params.get("award_year", "").strip()
        view = params.get("view", DEFAULT_VIEW)
        view = view if view in VIEWS else DEFAULT_VIEW
        sort = params.get("sort", "")
        sort = SORT_ALIASES.get(sort, sort)
        # A sort equal to the view's default is stored as "", so switching views keeps only a deliberate choice.
        if sort not in SORTS or sort == VIEWS[view].default_sort:
            sort = ""
        return cls(
            q=params.get("q", "").strip()[:MAX_QUERY_LENGTH],
            tags=tuple(tags[:MAX_SELECTED_TAGS]),
            modes=tuple(mode for mode in MODE_FILTERS if mode in wanted_modes),
            labels=tuple(label for label in LABEL_FILTERS if label in wanted_labels),
            publisher=found.name if found else "",
            reception=tuple(label for label in STEAM_REVIEW_LABELS if label in wanted_reception),
            award=award if award == ANY_AWARD or award in choices.categories else "",
            award_org=award_org if award_org in choices.organizations else "",
            award_year=award_year if award_year.isdigit() and int(award_year) in choices.years else "",
            winners=params.get("winners") in ("1", "on", "true"),
            sort=sort,
            view=view,
        )

    @property
    def order(self) -> str:
        """The sort order in effect: the chosen one, or the view's default."""
        return self.sort or VIEWS[self.view].default_sort

    def params(self, page: int = 1) -> list[tuple[str, str]]:
        """Query-string pairs; defaults are left out to keep URLs short."""
        params = [("q", self.q)] if self.q else []
        params.extend(("tag", tag) for tag in self.tags)
        params.extend(("mode", mode) for mode in self.modes)
        params.extend(("label", label) for label in self.labels)
        if self.publisher:
            params.append(("publisher", self.publisher))
        params.extend(("reception", label) for label in self.reception)
        if self.award:
            params.append(("award", self.award))
        if self.award_org:
            params.append(("award_org", self.award_org))
        if self.award_year:
            params.append(("award_year", self.award_year))
        if self.winners:
            params.append(("winners", "1"))
        if self.order != VIEWS[self.view].default_sort:
            params.append(("sort", self.order))
        if self.view != DEFAULT_VIEW:
            params.append(("view", self.view))
        if page > 1:
            params.append(("page", str(page)))
        return params

    def url(self, page: int = 1, path: str = "/") -> str:
        """Link to `path` that keeps this search, filters and sort order."""
        params = self.params(page)
        return f"{path}?{urlencode(params)}" if params else path

    @property
    def has_award_filter(self) -> bool:
        return bool(self.award or self.award_org or self.award_year or self.winners)

    @property
    def has_filters(self) -> bool:
        return bool(self.q or self.tags or self.modes or self.labels or self.publisher or self.reception
                    or self.has_award_filter)


def _page_number(value: str) -> int:
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _page_links(query: CatalogQuery, page: int, total_pages: int) -> list[dict]:
    """Numbered links: first, last, and two on each side of the current page, with gaps."""
    wanted = {1, total_pages, *range(page - 2, page + 3)}
    links, previous = [], 0
    for number in sorted(n for n in wanted if 1 <= n <= total_pages):
        if number - previous > 1:
            links.append({"gap": True})
        links.append({"number": number, "url": query.url(number), "current": number == page})
        previous = number
    return links


def _award_filter_label(query: CatalogQuery) -> str:
    """'Won BAFTA Games Awards · Best Game · 2024', 'Award: any listed award', ..."""
    parts = [query.award_org] if query.award_org else []
    if query.award not in ("", ANY_AWARD):
        parts.append(query.award)
    if query.award_year:
        parts.append(query.award_year)
    what = " · ".join(parts) or "any listed award"
    return f"Won {what}" if query.winners else f"Award: {what}"


def _active_filters(query: CatalogQuery) -> list[dict]:
    """One removable chip per active filter; each link drops just that filter."""
    chips = []
    if query.q:
        chips.append({"label": f"Name: “{query.q}”", "url": replace(query, q="").url()})
    for label in query.labels:
        chips.append({"label": label, "url": replace(query, labels=tuple(x for x in query.labels if x != label)).url()})
    if query.publisher:
        chips.append({"label": f"Publisher: {query.publisher}", "url": replace(query, publisher="").url()})
    for label in query.reception:
        chips.append({"label": f"Steam reviews: {label}",
                      "url": replace(query, reception=tuple(x for x in query.reception if x != label)).url()})
    if query.has_award_filter:
        chips.append({"label": _award_filter_label(query),
                      "url": replace(query, award="", award_org="", award_year="", winners=False).url()})
    for tag in query.tags:
        chips.append({"label": tag, "url": replace(query, tags=tuple(x for x in query.tags if x != tag)).url()})
    for mode in query.modes:
        chips.append({"label": mode, "url": replace(query, modes=tuple(x for x in query.modes if x != mode)).url()})
    return chips


# ---------------------------------------------------------------------- filtering


def _label_matches(labels: tuple[str, ...], wanted: tuple[str, ...]) -> bool:
    """A game matches when it has any wanted label; NO_VERIFIED_LABEL matches games without one."""
    if not wanted:
        return True
    return any(label in wanted for label in labels) or (NO_VERIFIED_LABEL in wanted and not labels)


def _matching_awards(awards, org: str, category: str, year: str, winners: bool) -> list[Award]:
    """The awards that fit every given choice; an empty choice ("" or "any") accepts every value."""
    return [
        award for award in awards
        if (not org or award.organization == org)
        and (category in ("", ANY_AWARD) or award.category == category)
        and (not year or str(award.year) == year)
        and (not winners or award.result == "winner")
    ]


def _award_matches(game: CatalogGame, query: CatalogQuery) -> bool:
    if not query.has_award_filter:
        return True
    return bool(_matching_awards(game.awards, query.award_org, query.award, query.award_year, query.winners))


def _award_option_counts(games: list[CatalogGame], query: CatalogQuery, choices: AwardChoices) -> dict:
    """Games each award option would show, given the other award choices (and every other filter)."""
    def count(org: str, category: str, year: str) -> int:
        return sum(1 for game in games if _matching_awards(game.awards, org, category, year, query.winners))

    return {
        "any": count(query.award_org, ANY_AWARD, query.award_year),
        "categories": {category: count(query.award_org, category, query.award_year) for category in choices.categories},
        "organizations": {org: count(org, query.award, query.award_year) for org in choices.organizations},
        "years": {year: count(query.award_org, query.award, str(year)) for year in choices.years},
    }


# ---------------------------------------------------------------------- view building


def _review_summary(row) -> dict:
    positive, total = row["review_positive"], row["review_total"]
    usable = ranking.review_problem(positive, total) is None
    return {
        "fetched": row["reviews_updated_at"] is not None,
        "usable": usable,
        "total": total or 0,
        "positive_count": positive if usable else None,
        "percent": percent_positive(positive, total) if usable else None,
        "score_desc": row["review_score_desc"],
    }


def _award_counts_text(wins: int, nominations: int) -> str:
    """'5 wins, 1 nomination', '1 win', '3 nominations', or '' without either."""
    parts = []
    if wins:
        parts.append(f"{wins} {'win' if wins == 1 else 'wins'}")
    if nominations:
        parts.append(f"{nominations} {'nomination' if nominations == 1 else 'nominations'}")
    return ", ".join(parts)


def _placement_view(place: ranking.Placement) -> dict:
    """The ranking facts shown on a card and in the details: the rank, and the award group and counts behind it."""
    summary = place.awards
    return {
        "rank": place.rank,
        "in_window": place.in_window,
        "rank_group_name": summary.group_name,  # the details and the ranking explanation
        "rank_group_label": CARD_GROUP_LABELS[summary.group],  # the cards, which have less room
        "rank_award_counts": _award_counts_text(summary.wins, summary.nominations),
        "rank_for_original_release": summary.for_original_release,
        "rank_without_reviews": place.review_note is not None,  # a ranked game then stands on its awards alone
        "rank_note": place.note,  # why there is no number: outside the window, or Unranked
        "upcoming": place.upcoming,  # "Release: ..." instead of "Released ..."
    }


def _release_shown(row, place: ranking.Placement) -> str:
    """The release date as shown: '24 Apr 2025' when Steam gives an exact day, else Steam's own text."""
    return format_day(place.released_on) if place.released_on else (row["release_date"] or "").strip()


def _award_view(award: Award) -> dict:
    return {
        "organization": award.organization,
        "year": award.year,
        "category": award.category,
        "is_winner": award.result == "winner",
        "is_top_award": award.is_top_award,
        "result": "Winner" if award.result == "winner" else "Nominee",
        "for_original_release": award.mapping == "edition",
        "award_title": award.award_title,
        "mapping_note": award.mapping_note,
        "source_url": award.source_url,
        "checked_on": award.checked_on,
    }


def _card_view(row, tags: list[str], curated: CuratedData, place: ranking.Placement,
               query: CatalogQuery, page: int, sale: pricing.SaleBadge | None = None) -> dict:
    appid = row["appid"]
    awards = curated.awards_for(appid)
    return {
        "appid": appid,
        "name": row["name"] or row["curated_title"],
        **_placement_view(place),
        "release": _release_shown(row, place),
        "image_url": row["cover_image_url"] or row["header_image_url"],
        "labels": [name for name in LABEL_ORDER if name in curated.labels_for(appid)],
        "tags": tags[:CARD_TAG_COUNT],
        "summary": concise_description(row["short_description"] or ""),
        "reviews": _review_summary(row),
        "awards": [_award_view(award) for award in awards[:CARD_AWARD_COUNT]],
        "more_awards": max(0, len(awards) - CARD_AWARD_COUNT),
        "sale": sale,  # a current Steam discount (pricing.sale_badge), or None
        "store_url": STORE_PAGE_URL.format(appid=appid),
        "details_url": query.url(page, path=f"/games/{appid}"),
    }


def _media_view(row) -> dict:
    """The preview beside the details: Steam's looping trailer clip, else a still image."""
    clip = json.loads(row["preview_clip"]) if row["preview_clip"] else None
    if clip and (clip.get("webm") or clip.get("mp4")):
        return {
            "kind": "clip",
            "webm": clip.get("webm"),
            "mp4": clip.get("mp4"),
            "poster": clip.get("poster") or row["screenshot_url"] or row["header_image_url"],
            "title": clip.get("title"),
        }
    if row["screenshot_url"]:
        return {"kind": "image", "url": row["screenshot_url"], "caption": "Screenshot from the Steam store page."}
    if row["header_image_url"]:
        return {"kind": "image", "url": row["header_image_url"], "caption": "Header image from the Steam store page."}
    return {"kind": "none"}


def _excerpt_view(excerpt) -> dict:
    text, cut = shorten(excerpt["text"], DETAILS_EXCERPT_CHARS)
    return {
        "text": text,
        "shortened": cut or bool(excerpt["truncated"]),
        "votes_up": excerpt["votes_up"],
        "playtime_minutes": excerpt["playtime_minutes"],
        "created_at": excerpt["created_at"],
    }


def _related_view(suggestions: list[related.RelatedGame], rows: dict, query: CatalogQuery, page: int) -> list[dict]:
    """Compact links to related games; each keeps the browsing context of the page it came from."""
    return [
        {
            "appid": suggestion.appid,
            "name": rows[suggestion.appid]["name"] or rows[suggestion.appid]["curated_title"],
            "image_url": rows[suggestion.appid]["header_image_url"] or rows[suggestion.appid]["cover_image_url"],
            "explanation": suggestion.explanation,
            "url": query.url(page, path=f"/games/{suggestion.appid}"),
        }
        for suggestion in suggestions
    ]


def _publisher_links(names: list[str], publishers: PublisherIndex) -> list[dict]:
    """Steam's publisher names as Steam lists them, each linked to all catalog games of its publisher."""
    links = []
    for name in names:
        found = publishers.find(name)
        links.append({"name": name, "url": CatalogQuery(publisher=found.name, view=ALL_VIEW).url() if found else None})
    return links


def _details_view(row, tags: list[str], excerpts: dict, curated: CuratedData, place: ranking.Placement,
                  now: datetime, *, related_games: list[dict], related_note: str, publishers: PublisherIndex,
                  price_row=None) -> dict:
    appid = row["appid"]
    name = row["name"] or row["curated_title"]
    labels = curated.labels_for(appid)
    tag_view = TOP_VIEW if place.in_window else ALL_VIEW  # a tag link always lists the game it came from
    return {
        "appid": appid,
        "name": name,
        **_placement_view(place),
        "labels": [
            {"name": name, "summary": labels[name].evidence_summary, "url": labels[name].evidence_url,
             "checked_on": labels[name].checked_on}
            for name in LABEL_ORDER if name in labels
        ],
        "tags": [{"name": tag, "url": CatalogQuery(tags=(tag,), view=tag_view).url()} for tag in tags[:DETAILS_TAG_COUNT]],
        "is_dlc": row["app_type"] == "dlc",
        "developers": json.loads(row["developers"] or "[]"),
        "publishers": _publisher_links(json.loads(row["publishers"] or "[]"), publishers),
        "release_date": _release_shown(row, place),
        "coming_soon": bool(row["coming_soon"]),
        "genres": json.loads(row["genres"] or "[]"),
        "modes": json.loads(row["modes"] or "[]"),
        "platforms": json.loads(row["platforms"] or "[]"),
        "description": row["short_description"],
        "about_blocks": json.loads(row["about_blocks"] or "[]"),
        "awards": [_award_view(award) for award in curated.awards_for(appid)],
        "store_url": STORE_PAGE_URL.format(appid=appid),
        "reviews_url": STEAM_REVIEWS_URL.format(appid=appid),
        "media": _media_view(row),
        "related": related_games,
        "related_note": related_note,
        "price": pricing.price_view(price_row, now, name),
        "reviews": {
            **_review_summary(row),
            "status": row["reviews_status"],
            "error": row["reviews_error"],
            "positive": [_excerpt_view(e) for e in excerpts["positive"]],
            "negative": [_excerpt_view(e) for e in excerpts["negative"]],
            "window": review_window_label(row["reviews_day_range_used"]),
            "updated_at": row["reviews_updated_at"],
            "checked_at": row["reviews_checked_at"],
            "stale": _older_than(row["reviews_updated_at"], REVIEWS_STALE_AFTER, now),
        },
        "details_status": row["details_status"],
        "details_error": row["details_error"],
        "details_updated_at": row["details_updated_at"],
        "details_checked_at": row["details_checked_at"],
        "details_stale": _older_than(row["details_updated_at"], DETAILS_STALE_AFTER, now),
    }


def _sync_status(conn) -> dict:
    """The latest sync run, its main problem (if any), and the latest run that saved data."""
    runs = store.recent_sync_runs(conn, limit=1)
    last = runs[0] if runs else None
    return {
        "last": last,
        "last_problem": store.run_problem(last) if last else None,
        "last_saved": store.last_successful_sync(conn),
    }


def _trend_item(game: CatalogGame, rows: dict, prices: dict, now: datetime, *,
                availability: upcoming.Availability | None = None, **facts) -> dict:
    """One game in a Steam activity list: its name, small image, details link, current sale badge and platforms.

    `availability` comes from the game's IGDB record (all platforms); without one, the page shows the Steam
    version's operating systems and says that other platforms are not known.
    """
    row = rows[game.appid]
    return {
        "appid": game.appid,
        "name": row["name"] or row["curated_title"],
        "image_url": row["header_image_url"] or row["cover_image_url"],
        "details_url": f"/games/{game.appid}",
        "game": game,
        "release": format_day(game.released_on) if game.released_on else (row["release_date"] or ""),
        "sale": pricing.sale_badge(prices.get(game.appid), now),
        "availability": availability,
        "steam_platforms": json.loads(row["platforms"] or "[]"),
        **facts,
    }


# ---------------------------------------------------------------------- upcoming games, live streams, critics


def _critics_view(conn, key: str, *, released: bool) -> dict:
    """"What the critics said" for one game key, from stored data only."""
    row = store.critic_article_rows(conn, [key]).get(key) if released else None
    scores = store.critic_scores(conn, key) if row is not None else []
    return critics.critics_view(row, scores, released=released)


def _catalog_igdb_id(conn, appid: int) -> int | None:
    """A catalog game's IGDB id: its link, else its only verified IGDB match.

    Used to find its Twitch category ("Watch it live") and its platforms on every system (IGDB's record).
    """
    link = store.steam_igdb_link(conn, appid)
    if link is not None and link["status"] == "linked":
        return link["igdb_id"]
    ids = store.igdb_ids_for_appid(conn, appid)
    return ids[0] if len(ids) == 1 else None


def _availability(row, today: date) -> upcoming.Availability | None:
    """A stored IGDB record's platforms on `today` (out, early access, announced, undated); None without a record."""
    if row is None:
        return None
    return upcoming.availability(upcoming.stored_dates(row), json.loads(row["platforms"] or "[]"), today)


def _live_view(conn, key: str, igdb_id: int | None, *, configured: bool, upcoming: bool, moment: datetime) -> dict:
    """"Watch it live" for one game key, from the stored Twitch answer only (no network)."""
    view = twitch.live_view(store.twitch_live_row(conn, key), moment, configured=configured, igdb_id=igdb_id,
                            upcoming=upcoming)
    view["key"] = key
    return view


def _igdb_details_view(row, today: date) -> dict:
    """The details of a game known from IGDB (upcoming or not on Steam): IGDB facts only, with their source.

    No Steam reviews, prices, awards or player counts: those exist only for catalog games on Steam.
    Links are split into verified ones (upcoming.verify_link) and the others IGDB lists, which say so.
    """
    dates = upcoming.stored_dates(row)
    state = upcoming.release_state(row["game_type"], row["game_status"], dates, row["first_release_date"], today)
    websites = json.loads(row["websites"] or "[]")
    verified = [site for site in websites if site.get("verified")]  # records saved before 0.7 have no flag
    verified_steam = row["steam_match"] == "verified" and row["steam_appid"]
    return {
        "kind": "igdb",
        "key": igdb_key(row["igdb_id"]),
        "igdb_id": row["igdb_id"],
        "name": row["name"],
        "cover_url": upcoming.cover_url(row["cover_image_id"]),
        "summary": row["summary"],
        "state": state,
        "availability": _availability(row, today),
        "game_type": upcoming.GAME_TYPES.get(row["game_type"]),
        "publishers": json.loads(row["publishers"] or "[]"),
        "developers": json.loads(row["developers"] or "[]"),
        "official": [site for site in verified if site["kind"] == upcoming.OFFICIAL],
        "stores": [site for site in verified if site["kind"] != upcoming.OFFICIAL],
        "unverified_links": [site for site in websites if not site.get("verified")],
        "igdb_url": row["igdb_url"],
        "steam_url": STORE_PAGE_URL.format(appid=row["steam_appid"]) if verified_steam else None,
        "steam_match": row["steam_match"],
        "steam_reason": row["steam_match_reason"],
        "checked_at": row["checked_at"],
        # A verified Steam app outside the catalog: a wishlist entry saved under it is the same game.
        "aliases": [str(row["steam_appid"])] if verified_steam else [],
    }


DATE_CHANGES_SHOWN_FOR = timedelta(days=30)  # a release-date change stays on Trending this long


def _igdb_item(row, key: str, today: date, **facts) -> dict:
    """One IGDB game in a Trending list: name, details link, cover, release state and platforms.

    `key` is upcoming.game_key_for's answer, so a game that is also a catalog game opens the catalog game.
    `facts` adds what the list shows besides: position and IGDB's value, change, date_change, Twitch fields.
    """
    dates = upcoming.stored_dates(row)
    publishers = json.loads(row["publishers"] or "[]")
    return {
        "key": key,
        "igdb_id": row["igdb_id"],
        "name": row["name"],
        "details_url": f"/games/{key}",
        "image_url": upcoming.cover_url(row["cover_image_id"]),  # catalog images fill gaps later
        "state": upcoming.release_state(row["game_type"], row["game_status"], dates, row["first_release_date"], today),
        "availability": _availability(row, today),
        "publisher": publishers[0] if publishers else None,
        "steam_verified": row["steam_match"] == "verified",
        "checked_at": row["checked_at"],
        **facts,
    }


def _date_change_view(rows: list) -> dict | None:
    """The newest release-date change of a game (store.release_changes rows, newest first), as shown."""
    if not rows:
        return None
    newest = rows[0]
    return {"change": upcoming.DateChange(newest["platform"], newest["before"], newest["after"], newest["kind"]),
            "noticed_at": newest["noticed_at"]}


def _trending_lists(conn, today: date, moment: datetime) -> dict:
    """Every cross-platform list on Trending, from stored data only (no network).

    "upcoming": one list per signal, games not out on any platform ("games" shows the first LIST_SIZE; "tracked"
    holds every upcoming game of the newest snapshot, up to TRACKED_PER_SIGNAL, for the dashboard's release radar);
    "recent": the Want to Play games first released in the last RECENT_WINDOW; "twitch": Twitch's newest top
    categories that are IGDB games, if the list is less than 24 hours old ("games" shows the first LIST_SIZE;
    "listable" holds all of them, for the dashboard's analyses). A game released since the last refresh moves from
    the upcoming list to the recent one at the next page load, with the same key.
    """
    visible = {row["appid"] for row in store.matching_games(conn, [], [])}
    linked = store.catalog_links_by_igdb(conn, visible)
    summary = store.popularity_summary(conn)
    changes_since = (moment - DATE_CHANGES_SHOWN_FOR).isoformat(timespec="seconds")
    upcoming_lists, recent = [], None
    for signal in upcoming.SIGNALS:
        latest = store.latest_popularity(conn, signal.key)
        rows = store.igdb_game_rows(conn, [entry["igdb_id"] for entry in latest])
        history_status = trending.history_status(summary.get(signal.key, {}))
        history = (store.popularity_history(conn, signal.key,
                                            (history_status["last"] - trending.MAX_BASELINE_AGE).isoformat())
                   if history_status["last"] else {})
        changes = upcoming.position_changes(history)
        date_changes = store.release_changes(conn, list(rows), changes_since)
        by_kind: dict[str, list[dict]] = {upcoming.UPCOMING: [], upcoming.RECENT: []}
        for entry in latest:
            row = rows.get(entry["igdb_id"])
            kind = upcoming.list_kind(row, today) if row is not None else None
            if kind in by_kind:
                by_kind[kind].append(_igdb_item(
                    row, upcoming.game_key_for(row, visible, linked), today, position=entry["position"],
                    value=entry["value"], change=changes.get(entry["igdb_id"]),
                    date_change=_date_change_view(date_changes.get(row["igdb_id"]))))
        calculated = [entry["calculated_at"] for entry in latest if entry["calculated_at"]]
        common = {"signal": signal, "captured_at": latest[0]["captured_at"] if latest else None,
                  "calculated_at": (min(calculated), max(calculated)) if calculated else None,
                  "history": history_status}
        tracked = by_kind[upcoming.UPCOMING]
        upcoming_games = tracked[:upcoming.LIST_SIZE]
        upcoming_lists.append({**common, "games": upcoming_games, "tracked": tracked,
                               "has_changes": any(item["change"] for item in upcoming_games)})
        if signal.tracks_recent_releases:
            recent_games = by_kind[upcoming.RECENT][:upcoming.LIST_SIZE]
            recent = {**common, "games": recent_games, "has_changes": any(item["change"] for item in recent_games)}

    top = store.twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
    records = store.igdb_game_rows(conn, [row["igdb_id"] for row in top if row["igdb_id"]])
    listable, left_out = [], Counter()
    for row in top:
        record = records.get(row["igdb_id"]) if row["igdb_id"] else None
        if row["igdb_id"] is None:
            left_out["no_igdb_game"] += 1  # Just Chatting, Art, Slots and other categories Twitch links to no game
        elif record is None:
            left_out["not_read"] += 1  # the IGDB record was not read (yet)
        elif not upcoming.twitch_listable(record):
            left_out["other_kind"] += 1  # DLC, bundles, mods and the like, or a game IGDB marks cancelled
        else:
            listable.append(_igdb_item(
                record, upcoming.game_key_for(record, visible, linked), today, twitch_position=row["position"],
                twitch_name=row["name"], twitch_url=twitch.category_url(row["name"]),
                name_differs=normalize_for_search(row["name"]) != normalize_for_search(record["name"])))
    twitch_list = {"games": listable[:upcoming.LIST_SIZE], "listable": listable,
                   "checked_at": top[0]["checked_at"] if top else None, "categories": len(top),
                   "left_out": left_out, "depth": twitch.TOP_GAMES_PAGE,
                   "keep_hours": int(twitch.KEEP_FOR.total_seconds() // 3600)}
    _fill_catalog_images(conn, [item for data in upcoming_lists for item in data["tracked"]]
                         + (recent["games"] if recent else []) + listable)
    return {"upcoming": upcoming_lists, "recent": recent, "twitch": twitch_list}


def _fill_catalog_images(conn, items: list[dict]) -> None:
    """Give each catalog game without an IGDB cover its Steam header image (items of _igdb_item, changed in place)."""
    missing = sorted({int(item["key"]) for item in items if not item["image_url"] and item["key"].isdigit()})
    images = {appid: row["header_image_url"] for appid, row in store.game_rows(conn, missing).items()}
    for item in items:
        if not item["image_url"] and item["key"].isdigit():
            item["image_url"] = images.get(int(item["key"]))


def _list_analysis(conn, lists: dict, steam: dict, reviews_at: dict) -> dict:
    """The dashboard's analyses of Most watched on Twitch, from stored data only.

    `lists`, `steam` and `reviews_at` are what dashboard_trending already read. Twitch's list has no history, because
    Twitch allows keeping its data for 24 hours, so only its newest list is compared: with Steam's newest saved
    most-wishlisted list (by IGDB id; list_history keeps one snapshot per IGDB calculation) and with the Steam reviews
    of its catalog games.
    """
    signal = upcoming.STEAM_WISHLISTS
    snapshots = list_history.comparable_snapshots(store.popularity_snapshot_rows(conn, signal.key))
    newest = snapshots[-1] if snapshots else None
    twitch_games = lists["twitch"]["listable"]
    return {
        "wishlists": next(data for data in lists["upcoming"] if data["signal"] is signal),
        "newest": newest,
        "overlap": dashboard.list_overlap(newest, twitch_games, lists["twitch"]["depth"]),
        "reception": dashboard.twitch_reception(twitch_games, steam["catalog"], reviews_at),
        "saved": len(newest.positions) if newest else 0,  # games in the newest saved Steam list
        "tracked": upcoming.TRACKED_PER_SIGNAL,  # the most upcoming games a refresh saves per list
    }


def _steam_activity(conn, curated: CuratedData, current_day: date, moment: datetime) -> dict:
    """The Steam activity part of Trending (catalog games, Steam only), from stored data (no network).

    The Trending page and the Trending games dashboard both use this, so they show the same games: each list
    keeps its first trending.SECTION_SIZE games, and each item carries its platforms from IGDB when known.
    The player history reaches dashboard.HISTORY_DAYS further back than the changes need, for the dashboard's
    small charts; a change only ever compares snapshots CHANGE_WINDOW to MAX_BASELINE_AGE apart.
    """
    window = ranking.RankingWindow.for_website(current_day)
    catalog_rows = store.matching_games(conn, [], [])
    placements = ranking.place_catalog(catalog_rows, curated, window)
    games = catalog.catalog_games(catalog_rows, curated, placements, publishers=publisher_index(catalog_rows, curated),
                                  players=store.latest_player_counts(conn))
    summary = store.snapshot_summary(conn)
    players_status = trending.history_status(summary["players"])
    reviews_status = trending.history_status(summary["reviews"])
    player_history = store.player_history(
        conn, (players_status["last"] - trending.MAX_BASELINE_AGE - timedelta(days=dashboard.HISTORY_DAYS)).isoformat()
    ) if players_status["last"] else {}
    review_history = store.review_history(
        conn, (reviews_status["last"] - trending.MAX_BASELINE_AGE).isoformat()
    ) if reviews_status["last"] else {}
    player_changes = trending.player_changes(player_history)
    review_changes = trending.review_changes(review_history)
    year_start, year_end = trending.this_year(current_day)
    four_start, four_end = trending.ranking_window(window)
    sections = {
        "popular": trending.popular_now(games),
        "this_year": trending.well_received_released_between(games, year_start, year_end),
        "four_years": trending.well_received_released_between(games, four_start, four_end),
    }
    by_appid = {game.appid: game for game in games}
    size = trending.SECTION_SIZE
    shown = {game.appid for listed in sections.values() for game in listed[:size]}
    shown |= {change.appid for change in (player_changes[:size] + review_changes[:size])}
    rows = store.game_rows(conn, sorted(shown))
    prices = store.price_rows(conn, sorted(shown))
    igdb_ids = {appid: _catalog_igdb_id(conn, appid) for appid in shown}  # platforms of the Steam games
    igdb_rows = store.igdb_game_rows(conn, sorted({igdb_id for igdb_id in igdb_ids.values() if igdb_id}))

    def item(game: CatalogGame, **facts) -> dict:
        igdb_row = igdb_rows.get(igdb_ids.get(game.appid))
        return _trend_item(game, rows, prices, moment, availability=_availability(igdb_row, current_day),
                           reviews_at=rows[game.appid]["reviews_updated_at"], **facts)

    player_times = [game.players_at for game in sections["popular"][:size] if game.players_at]
    return {
        "popular": [item(game) for game in sections["popular"][:size]],
        "popular_total": len(sections["popular"]),
        "popular_times": (min(player_times), max(player_times)) if player_times else None,
        "this_year": [item(game) for game in sections["this_year"][:size]],
        "this_year_total": len(sections["this_year"]),
        "this_year_window": (year_start, year_end),
        "four_years": [item(game) for game in sections["four_years"][:size]],
        "four_years_total": len(sections["four_years"]),
        "four_years_window": (four_start, four_end),
        "player_changes": [item(by_appid[change.appid], change=change) for change in player_changes[:size]],
        "review_changes": [item(by_appid[change.appid], change=change) for change in review_changes[:size]],
        "players_status": players_status,
        "reviews_status": reviews_status,
        "player_history": player_history,
        "catalog": by_appid,  # every catalog game, for the Steam facts of cross-platform games
    }


def _trending_catalog_games(lists: dict, steam: dict) -> dict[int, list[str]]:
    """The catalog games shown on the Trending page, each once in page order, with the titles of its lists there.

    `lists` and `steam` are the answers of _trending_lists and _steam_activity: the games each list shows.
    """
    sections = [(data["signal"].title, data["games"]) for data in lists["upcoming"]]
    if lists["recent"]:
        sections.append(("Most wanted new releases", lists["recent"]["games"]))
    sections += [
        ("Most watched on Twitch", lists["twitch"]["games"]),
        ("Popular on Steam right now", steam["popular"]),
        (f"Released in {steam['this_year_window'][0].year} with strong Steam reception", steam["this_year"]),
        (f"Released since {steam['four_years_window'][0].year} with strong Steam reception", steam["four_years"]),
        ("Most new Steam reviews", steam["review_changes"]),
        ("Most added players on Steam", steam["player_changes"]),
    ]
    games: dict[int, list[str]] = {}
    for title, items in sections:
        for item in items:
            key = str(item.get("appid") or item["key"])  # Steam lists give app ids; IGDB and Twitch lists give keys
            if key.isdigit() and title not in games.setdefault(int(key), []):
                games[int(key)].append(title)
    return games


def _sales_view(conn, lists: dict, steam: dict, moment: datetime) -> dict:
    """The Estimated Steam copies sold chart of the Trending page's catalog games, from stored estimates (no network).

    Call it only for a local request (is_local_request): Gamalytic's terms forbid showing its data to anyone else.
    """
    listed = _trending_catalog_games(lists, steam)
    appids = list(listed)
    rows = store.game_rows(conn, appids)
    games = [{"appid": appid, "name": rows[appid]["name"] or rows[appid]["curated_title"],
              "details_url": f"/games/{appid}", "lists": titles, "game": steam["catalog"][appid],
              # a portrait cover, like a game box; the wide Steam header image when Steam has no cover
              "image_url": rows[appid]["cover_image_url"] or rows[appid]["header_image_url"]}
             for appid, titles in listed.items() if appid in steam["catalog"] and appid in rows]
    runs = store.recent_source_runs(conn, gamalytic.SOURCE, limit=1)
    catalog_games = len(steam["catalog"])
    return {
        **dashboard.sales_chart(games, store.latest_sales_estimates(conn, appids), store.price_rows(conn, appids),
                                moment),
        "trending_games": len(games),
        "summary": store.sales_estimate_summary(conn),
        "last_run": runs[0] if runs else None,
        "last_problem": store.run_problem(runs[0]) if runs else None,
        "catalog_games": catalog_games,
        "requests_needed": math.ceil(catalog_games / gamalytic.APPIDS_PER_REQUEST),
        "max_age_days": gamalytic.MAX_AGE.days,
        "stale_days": gamalytic.STALE_AFTER.days,
        "about_url": gamalytic.ABOUT_URL,
        "terms_url": gamalytic.TERMS_URL,
    }


# ---------------------------------------------------------------------- app


def create_app(settings: Settings | None = None, *, today: Callable[[], date] = date.today,
               now: Callable[[], datetime] = _utc_now,
               twitch_client: Callable[[], twitch.TwitchClient] | None = None) -> FastAPI:
    """Build the web app. Tests pass their own Settings, a fixed `today` and a fixed `now`; `serve` uses the clock.

    `twitch_client` makes the Twitch client for "Watch it live" (tests pass a fake). By default it is
    built from the IGDB credentials when they are set; the client and its token live in this process only.

    `today` gives the last day of the five-year ranking window (and of the "Recent releases"
    window): by default the calendar date of the computer that runs the site, read again on
    every request. `now` is the moment freshness is judged against (stale details, reviews
    and prices, and whether a sale badge may show).
    """
    settings = settings or load_settings()
    store.initialize(settings.db_path).close()  # an empty catalog is valid: the page explains how to sync
    curated_cache = CuratedCache(settings.curated_dir)

    app = FastAPI(title="Game Librarian", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(
        date=format_date, datetime=format_datetime, number=format_number, hours=format_hours, day=format_day,
        rate=dashboard.format_rate, compact=dashboard.compact_number, signed=format_signed, listing=format_list,
    )
    templates.env.globals["app_version"] = __version__  # cache-busts the stylesheet and script after an upgrade
    live_lock = threading.Lock()  # one Twitch check at a time; a second request then finds the fresh answer
    live_client: list[twitch.TwitchClient] = []  # created on first use, then reused with its token

    def get_twitch_client() -> twitch.TwitchClient:
        if not live_client:
            live_client.append(twitch_client() if twitch_client else
                               twitch.TwitchClient(settings.igdb_client_id, settings.igdb_client_secret))
        return live_client[0]

    def load_details(appid: int, params) -> tuple[dict | None, CatalogQuery, int]:
        """The details view of one visible game (None if it is not shown), and the list context its links keep."""
        curated = curated_cache.get()
        conn = store.connect(settings.db_path)
        try:
            catalog_rows = store.matching_games(conn, [], [])
            publishers = publisher_index(catalog_rows, curated)
            query = CatalogQuery.from_params(params, award_choices(curated), publishers)
            page = _page_number(params.get("page", "1"))
            row = store.game_rows(conn, [appid]).get(appid)
            if row is None or not row["is_curated"] or row["details_updated_at"] is None:
                return None, query, page
            window = ranking.RankingWindow.for_website(today())
            placements = ranking.place_catalog(catalog_rows, curated, window)
            tags = store.tags_for(conn, [appid])[appid]
            excerpts = store.excerpts_for(conn, [appid])[appid]
            top_tags = store.top_tags_of_visible_games(conn, related.TOP_TAGS)
            facts = {game.appid: game for game in catalog.catalog_games(catalog_rows, curated, placements)}
            suggestions = related.related_games(
                appid, top_tags, {key: game.positive for key, game in facts.items()},
                {key: game.sort_name for key, game in facts.items()},
            )
            related_rows = store.game_rows(conn, [suggestion.appid for suggestion in suggestions])
            price_row = store.price_rows(conn, [appid]).get(appid)
            moment = now()
            game = _details_view(
                row, tags, excerpts, curated, placements[appid], moment,
                related_games=_related_view(suggestions, related_rows, query, page),
                related_note="" if suggestions else related.no_related_reason(appid, top_tags),
                publishers=publishers, price_row=price_row,
            )
            released = not game["upcoming"] and not game["coming_soon"]
            igdb_id = _catalog_igdb_id(conn, appid)
            igdb_row = store.igdb_game_rows(conn, [igdb_id]).get(igdb_id) if igdb_id else None
            game.update({
                "kind": "steam",
                "key": str(appid),
                "aliases": [igdb_key(known_id) for known_id in store.igdb_ids_for_appid(conn, appid)],
                "critics": _critics_view(conn, str(appid), released=released),
                "live": _live_view(conn, str(appid), igdb_id, configured=settings.igdb_configured,
                                   upcoming=not released, moment=moment),
                # Every platform, from the game's IGDB record (Steam's own list is "Steam platforms" above it).
                "availability": _availability(igdb_row, today()),
                "availability_checked_at": igdb_row["checked_at"] if igdb_row else None,
            })
        finally:
            conn.close()
        return game, query, page

    def list_context(params) -> tuple[CatalogQuery, int]:
        """The browsing context (search, filters, sort and page) that the details' links keep."""
        curated = curated_cache.get()
        conn = store.connect(settings.db_path)
        try:
            publishers = publisher_index(store.matching_games(conn, [], []), curated)
        finally:
            conn.close()
        return CatalogQuery.from_params(params, award_choices(curated), publishers), _page_number(params.get("page", "1"))

    def load_any(key: str, params) -> tuple[dict | None, CatalogQuery, int]:
        """The details of a game key: a catalog game, or a game known from IGDB (None when there is neither).

        An igdb-<id> key opens the catalog game when the IGDB game has a verified Steam app in the catalog,
        or when it is the only IGDB game IGDB lists for a catalog app. So a game saved before it came to
        Steam keeps working and is never shown twice.
        """
        parsed = parse_key(key)
        if parsed is not None and parsed.kind == "steam":
            game, query, page = load_details(parsed.number, params)
            if game is not None:
                return game, query, page
            conn = store.connect(settings.db_path)
            try:  # a Steam app outside the catalog may still be the verified match of an IGDB game
                match = store.igdb_game_for_appid(conn, parsed.number)
            finally:
                conn.close()
            if match is None:
                return None, query, page
            parsed = parse_key(igdb_key(match["igdb_id"]))
        query, page = list_context(params)
        if parsed is None:
            return None, query, page
        conn = store.connect(settings.db_path)
        try:
            row = store.igdb_game_rows(conn, [parsed.number]).get(parsed.number)
            visible = {item["appid"] for item in store.matching_games(conn, [], [])}
            linked_appid = store.catalog_links_by_igdb(conn, visible).get(parsed.number)
        finally:
            conn.close()
        verified_appid = row["steam_appid"] if row is not None and row["steam_match"] == "verified" else None
        for appid in (verified_appid, linked_appid):  # the same rule as upcoming.game_key_for
            if appid:
                game, _, _ = load_details(appid, params)  # None unless that app is a visible catalog game
                if game is not None:
                    return game, query, page
        if row is None:
            return None, query, page
        game = _igdb_details_view(row, today())
        released = not game["state"].upcoming
        conn = store.connect(settings.db_path)
        try:
            game["critics"] = _critics_view(conn, game["key"], released=released)
            game["live"] = _live_view(conn, game["key"], row["igdb_id"], configured=settings.igdb_configured,
                                      upcoming=not released, moment=now())
        finally:
            conn.close()
        return game, query, page

    def report_tables() -> tuple[ReportTables, ranking.RankingWindow]:
        """The dashboard's data: the report tables for today's five-year window, read on every request."""
        window = ranking.RankingWindow.for_website(today())
        conn = store.connect(settings.db_path)
        try:
            return collect_tables(conn, curated_cache.get(), window), window
        finally:
            conn.close()

    def render_dashboard(request: Request, section: str, last_sync, window: ranking.RankingWindow, view: dict):
        """One dashboard page; `last_sync` is the newest sync run that saved data (or None)."""
        context = {"active": "dashboard", "section": section, "dash": view, "window": window, "last_sync": last_sync}
        return templates.TemplateResponse(request, f"dashboard_{section}.html", context)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_home(request: Request):
        """Trending games, the dashboard's first page. Before 0.10 this address showed Player reception, so a link
        with that page's filters (/dashboard?tag=...) is redirected there and keeps working."""
        if any(name in request.query_params for name in RECEPTION_FILTERS):
            return RedirectResponse(f"/dashboard/reception?{request.url.query}", status_code=307)
        return dashboard_trending(request)

    @app.get("/dashboard/reception", response_class=HTMLResponse)
    def dashboard_reception(request: Request):
        """Player reception: the first page of the Power BI report, as HTML."""
        tables, window = report_tables()
        return render_dashboard(request, "reception", tables.last_sync, window,
                                dashboard.reception_page(tables, request.query_params))

    @app.get("/dashboard/awards", response_class=HTMLResponse)
    def dashboard_awards(request: Request):
        """Awards: the report's Awards Explorer page, as HTML."""
        tables, window = report_tables()
        return render_dashboard(request, "awards", tables.last_sync, window,
                                dashboard.awards_page(tables, request.query_params))

    @app.get("/dashboard/trending")
    def dashboard_trending_moved():
        """The Trending games page's address before 0.10; it is now /dashboard, the dashboard's default page."""
        return RedirectResponse("/dashboard", status_code=307)

    def dashboard_trending(request: Request):
        """Trending games: the Trending page's games charted, from the same stored data and rules (no network)."""
        current_day, moment = today(), now()
        conn = store.connect(settings.db_path)
        try:
            # Twitch allows keeping its data for 24 hours; an older list is deleted, not just hidden.
            store.purge_twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
            steam = _steam_activity(conn, curated_cache.get(), current_day, moment)
            lists = _trending_lists(conn, current_day, moment)
            # Twitch's whole list, not only its first games: its chart with Steam reviews looks further down.
            listed = [data["games"] for data in (*lists["upcoming"], lists["recent"] or {"games": []})]
            listed.append(lists["twitch"]["listable"])
            catalog_keys = sorted({int(item["key"]) for items in listed for item in items if item["key"].isdigit()})
            reviews_at = {appid: row["reviews_updated_at"] for appid, row in store.game_rows(conn, catalog_keys).items()}
            analysis = _list_analysis(conn, lists, steam, reviews_at)
            last_sync = store.last_successful_sync(conn)
            igdb_run = store.recent_source_runs(conn, upcoming.SOURCE, limit=1)
            # Gamalytic's estimates go only to a browser on this computer; for anyone else the chart does not exist.
            sales = _sales_view(conn, lists, steam, moment) if is_local_request(request) else None
        finally:
            conn.close()
        view = dashboard.trending_page(lists, steam, reviews_at)
        view.update({
            "analysis": analysis,
            "radar": dashboard.release_radar(lists["upcoming"], current_day),
            "sales": sales,
            "steam": {key: value for key, value in steam.items() if key not in ("player_history", "catalog")},
            "igdb": {"configured": settings.igdb_configured, "last_run": igdb_run[0] if igdb_run else None,
                     "last_problem": store.run_problem(igdb_run[0]) if igdb_run else None,
                     "list_size": upcoming.LIST_SIZE, "date_changes_days": DATE_CHANGES_SHOWN_FOR.days},
            "recent_days": upcoming.RECENT_WINDOW.days,
            "change_window_days": trending.CHANGE_WINDOW.days,
            "max_baseline_days": trending.MAX_BASELINE_AGE.days,
            "strong_labels": catalog.STRONG_RECEPTION_LABELS,
        })
        return render_dashboard(request, "trending", last_sync, ranking.RankingWindow.for_website(current_day), view)

    @app.get("/dashboard/about", response_class=HTMLResponse)
    def dashboard_about(request: Request):
        """About the data: definitions, coverage and the loaded data."""
        tables, window = report_tables()
        return render_dashboard(request, "about", tables.last_sync, window, dashboard.about_page(tables, window))

    @app.get("/trending", response_class=HTMLResponse)
    def trending_page(request: Request):
        """Separate, labeled signals from stored data, in three parts: upcoming games and games that are out or
        watched now (all platforms, from IGDB and Twitch), then Steam activity (Steam only). No network calls."""
        current_day, moment = today(), now()
        conn = store.connect(settings.db_path)
        try:
            # Twitch allows keeping its data for 24 hours; an older list is deleted, not just hidden.
            store.purge_twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
            steam = _steam_activity(conn, curated_cache.get(), current_day, moment)
            counts = store.catalog_counts(conn)
            sync_status = _sync_status(conn)
            lists = _trending_lists(conn, current_day, moment)
            igdb_run = store.recent_source_runs(conn, upcoming.SOURCE, limit=1)
        finally:
            conn.close()

        context = {
            "active": "trending",
            "counts": counts,
            "sync": sync_status,
            "size": trending.SECTION_SIZE,
            **{key: value for key, value in steam.items() if key not in ("player_history", "catalog")},
            "four_years_url": CatalogQuery(reception=catalog.STRONG_RECEPTION_LABELS, sort="reception").url(),
            "change_window_days": trending.CHANGE_WINDOW.days,
            "max_baseline_days": trending.MAX_BASELINE_AGE.days,
            "strong_labels": catalog.STRONG_RECEPTION_LABELS,
            "reliable_reviews": catalog.RELIABLE_REVIEW_COUNT,
            # The cross-platform parts, all from one refresh (python -m game_librarian upcoming).
            "igdb": {
                "configured": settings.igdb_configured,
                "last_run": igdb_run[0] if igdb_run else None,
                "last_problem": store.run_problem(igdb_run[0]) if igdb_run else None,
                "list_size": upcoming.LIST_SIZE,
                "date_changes_days": DATE_CHANGES_SHOWN_FOR.days,
            },
            "upcoming": {
                "lists": lists["upcoming"],
                "has_data": any(entry["captured_at"] for entry in lists["upcoming"]),
            },
            "recent": lists["recent"],
            "recent_window": (current_day - upcoming.RECENT_WINDOW, current_day),
            "recent_days": upcoming.RECENT_WINDOW.days,
            "twitch_top": lists["twitch"],
        }
        return templates.TemplateResponse(request, "trending.html", context)

    @app.get("/stories", response_class=HTMLResponse)
    def stories_index(request: Request):
        """The Data Stories: one card per story file."""
        collection = stories.load_stories(settings.curated_dir)
        cards = [
            {"story": story, "main": len(story.main_entries), "entries": len(story.entries),
             "first": story.entries[0].released.year,
             "last": max((entry.released.year for entry in story.entries if not entry.upcoming),
                         default=story.entries[0].released.year)}
            for story in collection.stories.values()
        ]
        return templates.TemplateResponse(request, "stories.html", {"active": "stories", "cards": cards,
                                                                    "problem_count": len(collection.problems)})

    @app.get("/stories/{slug}", response_class=HTMLResponse)
    def story_page(request: Request, slug: str):
        """One series story: release chart, waits between main games, platforms, timeline, sources and method."""
        story = stories.load_stories(settings.curated_dir).stories.get(slug)
        if story is None:
            return templates.TemplateResponse(request, "story.html", {"active": "stories", "story": None},
                                              status_code=404)
        conn = store.connect(settings.db_path)
        try:
            in_catalog = {row["appid"] for row in store.matching_games(conn, [], [])}
        finally:
            conn.close()
        gaps = stories.main_gaps(story)
        main_only = request.query_params.get("show") == "main"
        latest = stories.since_latest_main(story)
        images = {entry.title: stories.entry_image(entry) for entry in story.entries}
        context = {
            "active": "stories",
            "story": story,
            "chart": stories.release_chart(story),
            "gaps": stories.gap_bars(gaps),
            "gap_summary": stories.gap_summary(gaps),
            # Hidden on the day the latest main game came out: "0 days" says nothing the notes do not.
            "since_latest": {"entry": latest[0], "text": stories.duration_text(*latest[1]),
                             "short": stories.short_duration(*latest[1])}
            if latest and any(latest[1]) else None,
            "families": stories.platform_families(story),
            "timeline": stories.timeline(story, main_only=main_only),
            "main_only": main_only,
            "in_catalog": in_catalog,
            "upcoming": [entry for entry in story.entries if entry.upcoming],
            "roles": stories.ROLES,
            "families_map": stories.PLATFORM_FAMILIES,
            "images": images,
            "image_counts": {"covers": sum(1 for image in images.values() if image),
                             "missing": sum(1 for image in images.values() if image is None)},
        }
        return templates.TemplateResponse(request, "story.html", context)

    @app.get("/wishlist", response_class=HTMLResponse)
    def wishlist_page(request: Request):
        return templates.TemplateResponse(request, "wishlist.html", {"active": "wishlist"})

    @app.get("/games/{key}/panel", response_class=HTMLResponse)
    def game_panel(request: Request, key: str):
        """The details as an HTML fragment for the in-page dialog. The query string is the list's context."""
        game, _, _ = load_any(key, request.query_params)
        return templates.TemplateResponse(request, "panel.html", {"game": game}, status_code=200 if game else 404)

    @app.get("/games/{key}/live", response_class=HTMLResponse)
    def game_live(request: Request, key: str):
        """"Watch it live" as an HTML fragment. Asks Twitch only when the stored answer is due for a check.

        The Twitch client, its credentials and its token stay on the server; the page gets plain HTML.
        """
        game, _, _ = load_any(key, request.query_params)
        if game is None:
            return HTMLResponse("", status_code=404)
        live = game["live"]
        if live["needs_check"]:
            igdb_id = game["igdb_id"] if game["kind"] == "igdb" else None
            with live_lock:
                conn = store.connect(settings.db_path)
                try:
                    if igdb_id is None:
                        igdb_id = _catalog_igdb_id(conn, int(game["key"]))
                    if igdb_id is not None and twitch.refresh_due(store.twitch_live_row(conn, game["key"]), now()):
                        twitch.refresh_live(conn, get_twitch_client(), game["key"], igdb_id, now())
                    live = _live_view(conn, game["key"], igdb_id, configured=settings.igdb_configured,
                                      upcoming=live["upcoming"], moment=now())
                finally:
                    conn.close()
        live["needs_check"] = False  # this answer is the newest; the page does not ask again
        heading = request.query_params.get("heading", "live")
        heading_id = heading if heading.replace("-", "").isalnum() and len(heading) <= 40 else "live"
        return templates.TemplateResponse(request, "live.html", {"live": live, "heading_id": heading_id})

    @app.get("/games/{key}", response_class=HTMLResponse)
    def game_page(request: Request, key: str):
        """The details as a full page: a direct link, and the fallback when JavaScript is off."""
        game, query, page = load_any(key, request.query_params)
        back_url = query.url(page) + (f"#game-{game['key']}" if game and game["kind"] == "steam" else "")
        context = {"game": game, "back_url": back_url}
        return templates.TemplateResponse(request, "game.html", context, status_code=200 if game else 404)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        curated = curated_cache.get()
        choices = award_choices(curated)
        page_number = _page_number(request.query_params.get("page", "1"))
        current_day, moment = today(), now()
        window = ranking.RankingWindow.for_website(current_day)

        conn = store.connect(settings.db_path)
        try:
            catalog_rows = store.matching_games(conn, [], [])
            publishers = publisher_index(catalog_rows, curated)
            query = CatalogQuery.from_params(request.query_params, choices, publishers)
            words = normalize_for_search(query.q).split()
            players = store.latest_player_counts(conn)
            # Placed over the whole catalog, so search, filters, views and sorts never change a game's rank.
            placements = ranking.place_catalog(catalog_rows, curated, window)
            searched_rows = store.matching_games(conn, words, list(query.tags)) if (words or query.tags) else catalog_rows
            searched = catalog.catalog_games(searched_rows, curated, placements, publishers=publishers, players=players)
            chosen_publisher = publishers.find(query.publisher) if query.publisher else None
            # Each filter's verdict per game, so an option's count can leave out its own filter.
            checks = {
                "mode": {game.appid: set(query.modes).issubset(game.modes) for game in searched},
                "label": {game.appid: _label_matches(game.labels, query.labels) for game in searched},
                "award": {game.appid: _award_matches(game, query) for game in searched},
                "publisher": {game.appid: chosen_publisher is None or chosen_publisher.key in game.publishers
                              for game in searched},
                "reception": {game.appid: not query.reception or game.review_label in query.reception
                              for game in searched},
            }

            def passes(game: CatalogGame, *, skip: str = "") -> bool:
                """True when the game passes every filter, except the one named `skip`."""
                return all(verdicts[game.appid] for name, verdicts in checks.items() if name != skip)

            members = {key: {game.appid for game in searched if view.includes(game, current_day)}
                       for key, view in VIEWS.items()}
            in_view = members[query.view]
            matches = [game for game in searched if passes(game)]
            results = catalog.sort_games([game for game in matches if game.appid in in_view], query.order, current_day)
            total_pages = max(1, math.ceil(len(results) / PAGE_SIZE))
            if page_number > total_pages:
                return RedirectResponse(query.url(total_pages), status_code=303)
            start = (page_number - 1) * PAGE_SIZE
            page_appids = [game.appid for game in results[start : start + PAGE_SIZE]]
            rows = store.game_rows(conn, page_appids)
            tags_by_game = store.tags_for(conn, page_appids)
            prices = store.price_rows(conn, page_appids)
            tag_facets = store.tag_counts(conn, [game.appid for game in results], limit=SIDEBAR_TAG_LIMIT)
            counts = store.catalog_counts(conn)
            sync_status = _sync_status(conn)
        finally:
            conn.close()

        # Option counts follow the view and the other active filters, so each number says what choosing it would show.
        viewed = [game for game in searched if game.appid in in_view]
        label_counts: Counter = Counter()
        for game in viewed:
            if passes(game, skip="label"):
                label_counts.update(game.labels or (NO_VERIFIED_LABEL,))
        award_counts = _award_option_counts([game for game in viewed if passes(game, skip="award")], query, choices)
        publisher_counts = Counter(key for game in viewed if passes(game, skip="publisher") for key in game.publishers)
        reception_counts = Counter(game.review_label for game in viewed if passes(game, skip="reception"))
        catalog_labels = {row["review_score_desc"] for row in catalog_rows}  # offer only labels the catalog has
        mode_counts = Counter(mode for game in results for mode in set(game.modes))
        facet_names = {facet["tag"] for facet in tag_facets}

        def view_link(key: str) -> dict:
            """A view or collection link that keeps the search and filters, with the games it would show."""
            return {"key": key, "label": VIEWS[key].label.format(start_year=window.start.year),
                    "url": replace(query, view=key).url(),
                    "current": key == query.view, "games": sum(1 for game in matches if game.appid in members[key])}

        window_matches = sum(1 for game in matches if game.in_window)
        catalog_appids = [row["appid"] for row in catalog_rows]
        # Award groups with at least one verified result in the curated data; the explanation marks the others.
        recorded_groups = {award_display_group(award) + 1 for award in curated.awards
                           if award.review_status == "verified"} | {ranking.NO_AWARD_GROUP}
        context = {
            "active": "discover",
            "query": query,
            "view": VIEWS[query.view],
            # Each tab keeps the search and filters, and counts the games it would show with them.
            "view_links": [view_link(key) for key in (TOP_VIEW, ALL_VIEW)],
            "collection_links": [view_link(key) for key in COLLECTION_KEYS],
            "window": window,
            "rank_groups": [{"name": name, "recorded": group in recorded_groups}
                            for group, name in sorted(ranking.AWARD_GROUP_NAMES.items())],
            "reliable_reviews": catalog.RELIABLE_REVIEW_COUNT,
            "recent_window": catalog.recent_window(current_day),
            "sort": SORTS[query.order],
            "sort_options": SORT_OPTIONS,
            "mode_options": [
                {"name": mode, "games": mode_counts[mode], "selected": mode in query.modes} for mode in MODE_FILTERS
            ],
            "label_options": [
                {"name": name, "games": label_counts[name], "selected": name in query.labels} for name in LABEL_FILTERS
            ],
            # Publishers with a matching game, A–Z, plus the chosen one even when nothing matches now.
            "publisher_options": [
                {"name": publisher.name, "games": publisher_counts[publisher.key],
                 "selected": publisher.name == query.publisher}
                for publisher in publishers.choices()
                if publisher_counts[publisher.key] or publisher.name == query.publisher
            ],
            "reception_options": [
                {"name": label, "games": reception_counts[label], "selected": label in query.reception}
                for label in STEAM_REVIEW_LABELS if label in catalog_labels or label in query.reception
            ],
            "award_options": [{"value": ANY_AWARD, "label": "Any listed award", "games": award_counts["any"]}]
            + [{"value": category, "label": category, "games": award_counts["categories"][category]}
               for category in choices.categories],
            "award_org_options": [{"value": org, "games": award_counts["organizations"][org]}
                                  for org in choices.organizations],
            "award_year_options": [{"value": str(year), "games": award_counts["years"][year]} for year in choices.years],
            "award_organizations": choices.organizations,
            # The selected tags first (so they stay in view in the scrolling list), then the others by game count.
            "tag_options": sorted([
                {"tag": facet["tag"], "games": facet["games"], "selected": facet["tag"] in query.tags}
                for facet in tag_facets
            ] + [{"tag": tag, "games": 0, "selected": True} for tag in query.tags if tag not in facet_names],
                key=lambda option: not option["selected"]),
            "max_selected_tags": MAX_SELECTED_TAGS,
            "active_filters": _active_filters(query),
            "games": [
                _card_view(rows[appid], tags_by_game[appid], curated, placements[appid], query, page_number,
                           pricing.sale_badge(prices.get(appid), moment))
                for appid in page_appids
            ],
            "total": len(results),
            "catalog_total": len(catalog_rows),
            "promoted_total": sum(1 for appid in catalog_appids if curated.catalog_source(appid) == PROMOTED),
            "window_total": sum(1 for place in placements.values() if place.in_window),
            "ranked_total": sum(1 for place in placements.values() if place.rank is not None),
            # Matching games the Top-ranked view leaves out because they are outside the window.
            "outside_matches": len(matches) - window_matches if query.view == TOP_VIEW else 0,
            "all_games_url": replace(query, view=ALL_VIEW).url(),
            "first_shown": start + 1,
            "last_shown": start + len(page_appids),
            "page": page_number,
            "total_pages": total_pages,
            "page_links": _page_links(query, page_number, total_pages),
            "prev_url": query.url(page_number - 1) if page_number > 1 else None,
            "next_url": query.url(page_number + 1) if page_number < total_pages else None,
            "clear_url": CatalogQuery(sort=query.sort, view=query.view).url(),
            "counts": counts,
            "sync": sync_status,
            "curated_problem_count": len(curated.problems),
        }
        return templates.TemplateResponse(request, "index.html", context)

    return app
