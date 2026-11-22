"""FastAPI web app: a server-rendered view of the stored catalog, with private wishlists for signed-in visitors.

Page requests never call Steam. They read the catalog written by the sync command (the local SQLite file, or the
hosted site's PostgreSQL database; store.py) and the curated files, so the site keeps serving the last saved data
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
  snapshots, trending.py). Every game shows where it is out and where it is only announced (IGDB). Once
  TwitchTracker has given figures, Most watched on Twitch becomes "Most watched among tracked Twitch categories":
  the tracked game and series categories in TwitchTracker's rank order, with its rank and average viewers
  (_tracked_list, twitchtracker.py); until then it keeps Twitch's own order.
- Data Stories (/stories): hand-checked series timelines (stories.py).
- Dashboard (/dashboard...): Trending games first (/dashboard): three report pages about the Trending page's
  games, from the stored lists and snapshots (?report=: Sales & reception, Upcoming releases and Momentum; see
  _sales_report, _upcoming_report and _momentum_report); then the Power BI report's measures as HTML
  (dashboard.py): Player reception (/dashboard/reception), Awards and About the data. Old addresses redirect (see
  dashboard_home). Sales & reception leads with Gamalytic's estimated Steam copies sold once they are collected.
- Wishlist (/wishlist), behind the header's star icon: in the browser (static/wishlist.js), or, for a visitor signed
  in with an email account (accounts.py, supabase_auth.py; /account), in their account on the server, read and
  changed only through /api/wishlist for that account. A list saved in the browser is imported only when its owner
  asks, and the browser's copy stays until they remove it.
- Accounts (/account, /account/..., /auth/confirm), behind the header's person icon when Supabase is configured:
  sign in, create an account, confirm the email address, forgotten password, new password and sign out. The forms
  post to this server, which alone talks to Supabase Auth.
- Daily Trivia: the header's lightbulb opens a popup on any page. Its content comes from /trivia/panel: today's
  quiz from curated/trivia.json, the same for everyone that quiz day (UTC; trivia.py), and the facts open for
  browsing. The visitor's answers and scores stay in the browser (static/trivia.js); the server never sees them.

/healthz and /readyz answer Container Apps' probes. Every response carries security headers (_security_headers).

Run with `python -m game_librarian serve` (uses the `create_app` factory).
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__, accounts, catalog, critics, dashboard, gamalytic, list_comparison, pricing, ranking, \
    related, stories, store, supabase_auth, trending, trivia, twitch, twitchtracker, upcoming
from .database import close_pools
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


# ---------------------------------------------------------------------- security headers, the JSON API, forms

LOG = logging.getLogger("game_librarian.web")
# What a page may load: its own scripts only (none inline), its own styles plus style attributes, images and video
# from https servers (the Steam, IGDB and Twitch CDNs), requests to this site only, and no framing by other sites.
CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                           "img-src 'self' https: data:; media-src 'self' https:; connect-src 'self'; "
                           "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
HSTS = "max-age=31536000"  # the hosted site answers only over HTTPS; browsers then never try plain HTTP
MAX_JSON_BODY = 256 * 1024  # a wishlist import of WISHLIST_LIMIT games is well under this
MAX_FORM_BODY = 16 * 1024  # an account form (an email address, two passwords, a token) is well under this
MAX_FORM_FIELDS = 12
# What /account says after a redirect (?notice=): fixed sentences, so nothing that a link supplies is shown.
ACCOUNT_NOTICES = {
    "confirmed": "Your email address is confirmed, and you are signed in.",
    "signed_up": "Your account is ready, and you are signed in.",
    "password_changed": "Your new password is saved.",
    "signed_out": "You are signed out.",
    "deleted": "Your account and saved games on this site were deleted, and you are signed out. Your sign-in (email "
               "address and password) is kept by Supabase, the site’s sign-in service; ask the site’s owner to remove "
               "it too.",
}


def _security_headers(response: Response, *, hosted: bool, personal: bool) -> None:
    """Add the headers every response carries. A page for a signed-in visitor (`personal`) is never cached, so the
    next person on a shared computer cannot see it from the browser's cache."""
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if hosted:
        response.headers.setdefault("Strict-Transport-Security", HSTS)
    if personal:
        response.headers["Cache-Control"] = "no-store"


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    """An API error: a stable `error` code for the page's script and a sentence for people."""
    return JSONResponse({"error": code, "message": message}, status_code=status, headers={"Cache-Control": "no-store"})


def _same_site_json(request: Request) -> JSONResponse | None:
    """None for a JSON request from this site's own pages; else the error that refuses it.

    This is the protection against cross-site request forgery: another site's page cannot send JSON here without a
    CORS preflight, which this app never approves, and every current browser marks a request from another site in
    Sec-Fetch-Site. The platform's session cookie alone is therefore never enough to change a wishlist.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return _json_error(415, "json_required", "Send the request as JSON.")
    if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
        return _json_error(403, "cross_site", "This request did not come from a Game Librarian page.")
    return None


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is larger than `limit` bytes (reading stops there)."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


async def _read_json(request: Request):
    """The request's JSON body, or None when it is not JSON or is larger than MAX_JSON_BODY."""
    body = await _read_body(request, MAX_JSON_BODY)
    try:
        return json.loads(body) if body is not None else None
    except ValueError:
        return None


async def _read_form(request: Request) -> dict[str, str] | None:
    """A posted HTML form (application/x-www-form-urlencoded) as {field: first value}, or None when the request is
    another kind, larger than MAX_FORM_BODY, or has more than MAX_FORM_FIELDS fields. The account forms have no file
    fields, so the standard library reads them."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        return None
    body = await _read_body(request, MAX_FORM_BODY)
    if body is None:
        return None
    try:
        fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False,
                          max_num_fields=MAX_FORM_FIELDS)
    except (UnicodeDecodeError, ValueError):  # not UTF-8, or too many fields
        return None
    return {name: values[0] for name, values in fields.items()}


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
    "steam_appid" is the verified Steam app (else None) and "steam_candidates" every Steam app IGDB lists, verified or
    not: the dashboard's list comparison matches games by verified apps and flags a shared unverified one.
    """
    dates = upcoming.stored_dates(row)
    publishers = json.loads(row["publishers"] or "[]")
    verified = row["steam_match"] == "verified"
    return {
        "series": False,
        "key": key,
        "igdb_id": row["igdb_id"],
        "name": row["name"],
        "details_url": f"/games/{key}",
        "image_url": upcoming.cover_url(row["cover_image_id"]),  # catalog images fill gaps later
        "state": upcoming.release_state(row["game_type"], row["game_status"], dates, row["first_release_date"], today),
        "availability": _availability(row, today),
        "publisher": publishers[0] if publishers else None,
        "steam_verified": verified,
        "steam_appid": row["steam_appid"] if verified else None,
        "steam_candidates": json.loads(row["steam_appids"] or "[]"),
        "checked_at": row["checked_at"],
        **facts,
    }


def _series_item(name: str, **facts) -> dict:
    """A reviewed series category in a Most watched list ("series" in curated/twitch_categories.csv), under its series
    name. It stands for no single game, so it has no key, IGDB id, details page, cover or platforms, and no other list
    or chart can match it to a game. `facts` adds the list's Twitch or TwitchTracker fields, as for _igdb_item."""
    return {"series": True, "key": None, "igdb_id": None, "name": name, "image_url": None, **facts}


def _date_change_view(rows: list) -> dict | None:
    """The newest release-date change of a game (store.release_changes rows, newest first), as shown."""
    if not rows:
        return None
    newest = rows[0]
    return {"change": upcoming.DateChange(newest["platform"], newest["before"], newest["after"], newest["kind"]),
            "noticed_at": newest["noticed_at"]}


def _trending_lists(conn, today: date, moment: datetime, decisions: dict) -> dict:
    """Every cross-platform list on Trending, from stored data only (no network).

    "upcoming": one list per signal, games not out on any platform ("games" shows the first LIST_SIZE; "tracked"
    holds every upcoming game of the newest snapshot, up to TRACKED_PER_SIGNAL, for the dashboard's release radar and
    list comparison; "unread" counts the snapshot's entries without a stored IGDB record, which no rule can place);
    "recent": the Want to Play games first released in the last RECENT_WINDOW; "twitch": Twitch's newest top
    categories that are games (upcoming.twitch_games, with the reviewer `decisions` of curated/twitch_categories.csv),
    if the list is less than 24 hours old ("games" shows the first LIST_SIZE; "listable" holds all of them, for the
    dashboard's analyses), a reviewed series category as a series item (_series_item). A game released since the last
    refresh moves from the upcoming list to the recent one at the next page load, with the same key.
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
                               "unread": sum(1 for entry in latest if entry["igdb_id"] not in rows),
                               "has_changes": any(item["change"] for item in upcoming_games)})
        if signal.tracks_recent_releases:
            recent_games = by_kind[upcoming.RECENT][:upcoming.LIST_SIZE]
            recent = {**common, "games": recent_games, "has_changes": any(item["change"] for item in recent_games)}

    top = store.twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
    games, left_out = upcoming.twitch_games(conn, top, decisions)
    listable = []
    for row, record, _ in games:
        name = record["name"] if record else decisions[row["category_id"]].title  # a series: its reviewed title
        facts = {"twitch_position": row["position"], "twitch_name": row["name"],
                 "twitch_url": twitch.category_url(row["name"]),
                 "name_differs": normalize_for_search(row["name"]) != normalize_for_search(name)}
        listable.append(_igdb_item(record, upcoming.game_key_for(record, visible, linked), today, **facts) if record
                        else _series_item(name, **facts))
    twitch_list = {"games": listable[:upcoming.LIST_SIZE], "listable": listable,
                   "checked_at": top[0]["checked_at"] if top else None, "categories": len(top),
                   "left_out": left_out, "left_out_phrases": _left_out_phrases(left_out),
                   "depth": twitch.TOP_GAMES_PAGE, "keep_hours": int(twitch.KEEP_FOR.total_seconds() // 3600)}
    _fill_catalog_images(conn, [item for data in upcoming_lists for item in data["tracked"]]
                         + (recent["games"] if recent else []) + [item for item in listable if not item["series"]])
    return {"upcoming": upcoming_lists, "recent": recent, "twitch": twitch_list}


def _fill_catalog_images(conn, items: list[dict]) -> None:
    """Give each catalog game without an IGDB cover its Steam header image (items of _igdb_item, changed in place)."""
    missing = sorted({int(item["key"]) for item in items if not item["image_url"] and item["key"].isdigit()})
    images = {appid: row["header_image_url"] for appid, row in store.game_rows(conn, missing).items()}
    for item in items:
        if not item["image_url"] and item["key"].isdigit():
            item["image_url"] = images.get(int(item["key"]))


# Why a Twitch category is not in a Most watched list (upcoming.twitch_games, twitchtracker.ranking), as shown after a
# count. The last three tell apart a category without TwitchTracker figures: never a zero.
LEFT_OUT_REASONS = {
    "no_game": "that neither Twitch nor IGDB links to a game (such as Just Chatting)",
    "unresolved": "whose game is unresolved (Twitch and IGDB name different games, or a reviewer decided it stands "
                  "for no game)",
    "other_kind": "that IGDB lists as DLC, bundles or other kinds",
    "not_read": "whose IGDB record was not read yet",
    "repeated": "for a game another category already stands for",
    "no_summary": "that TwitchTracker has no summary of",
    "not_asked": "not asked yet",
    "failed": "with a failed check and no earlier figures",
}


def _left_out_phrases(left_out: Counter) -> list[str]:
    """The left-out categories as phrases such as "17 that neither Twitch nor IGDB links to a game (...)", in a fixed
    order, leaving out reasons with no category."""
    return [f"{left_out[reason]} {words}" for reason, words in LEFT_OUT_REASONS.items() if left_out[reason]]


def _most_watched(conn, decisions: dict, today: date, moment: datetime) -> dict:
    """Most watched among tracked Twitch categories, by TwitchTracker's figures (twitchtracker.py), from stored data
    only (no network).

    The entries are twitchtracker.ranking's (games and reviewed series categories, with the
    reviewer `decisions` of curated/twitch_categories.csv applied at once), in TwitchTracker's rank order. The page
    numbers them 1, 2, 3... as places in this list; each keeps TwitchTracker's own category rank as returned
    ("tracker_rank"), which counts Just Chatting and every category not tracked too, so a place is never called a
    TwitchTracker rank. "games" holds the first LIST_SIZE and "listable" all of them (for the dashboard's selected-game
    panel). Each item carries its retrieval time: "older" marks figures whose category's last check failed
    ("check_failed"), or that are REFRESH_AFTER or more older than the newest shown (a run that stopped before them);
    "stale" says the newest are REFRESH_AFTER old or more.
    """
    rows = store.tracker_rows(conn)
    ranked, left_out = twitchtracker.ranking(conn, decisions)
    items = []
    for entry in ranked:
        row = entry.row
        facts = {"tracker_rank": row["rank"], "avg_viewers": row["avg_viewers"], "avg_channels": row["avg_channels"],
                 "retrieved_at": row["retrieved_at"], "check_failed": row["status"] in ("error", "invalid"),
                 "twitch_name": row["category_name"], "twitch_url": twitch.category_url(row["category_name"]),
                 "name_differs": normalize_for_search(row["category_name"]) != normalize_for_search(entry.name)}
        items.append(_igdb_item(entry.record, entry.key, today, **facts) if entry.record
                     else _series_item(entry.name, **facts))
    _fill_catalog_images(conn, [item for item in items if not item["series"]])
    times = sorted({item["retrieved_at"] for item in items})
    newest = _parse_time(times[-1]) if times else None
    for item in items:  # a category asked hours after the others, within the cache time, leaves them current
        gap = newest - _parse_time(item["retrieved_at"])
        item["older"] = item["check_failed"] or gap >= twitchtracker.REFRESH_AFTER
    runs = store.recent_source_runs(conn, twitchtracker.SOURCE, limit=1)
    return {
        "games": items[:upcoming.LIST_SIZE],
        "listable": items,
        "list_size": upcoming.LIST_SIZE,
        "selected": len(rows),
        "reviewed": sum(1 for row in rows if not row["selected_at"]),  # chosen by a reviewed row, outside Twitch's list
        "selected_at": max((row["selected_at"] for row in rows if row["selected_at"]), default=None),
        "retrieved": (times[0], times[-1]) if times else None,
        "age_days": (moment - newest).days if newest else None,
        "stale": newest is not None and moment - newest >= twitchtracker.REFRESH_AFTER,
        "left_out": left_out,
        "left_out_phrases": _left_out_phrases(left_out),
        "last_run": runs[0] if runs else None,
        "last_problem": store.run_problem(runs[0]) if runs else None,
        "depth": twitch.TOP_GAMES_PAGE,
        "refresh_hours": int(twitchtracker.REFRESH_AFTER.total_seconds() // 3600),
        "api_url": twitchtracker.API_DOCS_URL,
    }


def _tracked_list(conn, decisions: dict, today: date, moment: datetime) -> dict | None:
    """The Most watched list by TwitchTracker's figures (_most_watched), or None while it lists no category: before
    the first TwitchTracker refresh, or when none of its answers had figures. The Trending page then shows Twitch's
    own order instead of an empty list, and the dashboard uses Twitch's list too, so the two pages always agree."""
    watched = _most_watched(conn, decisions, today, moment)
    return watched if watched["games"] else None


def _steam_activity(conn, curated: CuratedData, current_day: date, moment: datetime) -> dict:
    """The Steam activity part of Trending (catalog games, Steam only), from stored data (no network).

    The Trending page and the Trending games dashboard both use this, so they show the same games: each list
    keeps its first trending.SECTION_SIZE games, and each item carries its platforms from IGDB when known.
    The player history reaches dashboard.HISTORY_DAYS further back than the changes need, for the dashboard's
    player line; a change only ever compares snapshots CHANGE_WINDOW to MAX_BASELINE_AGE apart, and the review
    history it reads is returned too, for the dashboard's review waterfall.
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
        "review_history": review_history,
        "catalog": by_appid,  # every catalog game, for the dashboard's sales and reception charts
    }


def _trending_catalog_games(lists: dict, steam: dict) -> dict[int, list[str]]:
    """The catalog games shown on the Trending page, each once in page order, with the titles of its lists there.

    `lists` and `steam` are the answers of _trending_lists and _steam_activity: the games each list shows. Most
    watched is TwitchTracker's ranking when `lists` holds it ("watched", _tracked_list). A series category stands
    for no single game, so it never names a catalog game here.
    """
    sections = [(data["signal"].title, data["games"]) for data in lists["upcoming"]]
    if lists["recent"]:
        sections.append(("Most wanted new releases", lists["recent"]["games"]))
    watched = lists.get("watched")
    sections += [
        ("Most watched among tracked Twitch categories", watched["games"]) if watched is not None
        else ("Most watched on Twitch", lists["twitch"]["games"]),
        ("Popular on Steam right now", steam["popular"]),
        (f"Released in {steam['this_year_window'][0].year} with strong Steam reception", steam["this_year"]),
        (f"Released since {steam['four_years_window'][0].year} with strong Steam reception", steam["four_years"]),
        ("Most new Steam reviews", steam["review_changes"]),
        ("Most added players on Steam", steam["player_changes"]),
    ]
    games: dict[int, list[str]] = {}
    for title, items in sections:
        for item in items:
            if item.get("series"):
                continue
            key = str(item.get("appid") or item["key"])  # Steam lists give app ids; IGDB and Twitch lists give keys
            if key.isdigit() and title not in games.setdefault(int(key), []):
                games[int(key)].append(title)
    return games


def _sales_view(conn, lists: dict, steam: dict, moment: datetime) -> dict | None:
    """The Estimated Steam copies sold chart of the Trending page's catalog games, from stored estimates (no network),
    or None before Gamalytic was ever asked: the page then compares Steam reception only, and says why."""
    summary = store.sales_estimate_summary(conn)
    if not summary["collections"]:
        return None
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
        "named": dashboard.RING_NAMED,  # the largest estimates with a named slice each
        "summary": summary,
        "last_run": runs[0] if runs else None,
        "last_problem": store.run_problem(runs[0]) if runs else None,
        "catalog_games": catalog_games,
        "requests_needed": math.ceil(catalog_games / gamalytic.APPIDS_PER_REQUEST),
        "max_age_days": gamalytic.MAX_AGE.days,
        "stale_days": gamalytic.STALE_AFTER.days,
        "about_url": gamalytic.ABOUT_URL,
    }


# ---------------------------------------------------------------------- the Trending games dashboard's report pages


def _reception_view(conn, steam: dict, view: str | None) -> dict:
    """The strong-reception scatterplot of Sales & reception (dashboard.reception_chart), in `view` (a
    dashboard.RECEPTION_VIEWS key; any other value shows every game).

    It draws every catalog game that meets the Trending page's rule (trending.well_received_released_between) in the
    five-year window, not only the first 10 the Trending page lists, each once, this year's releases highlighted. The
    peer benchmarks compare them with every comparable game: each catalog game with usable Steam reviews and an exact
    Steam release date in the same window, strong reception or not ("below" lists the ones not drawn).
    """
    games = steam["catalog"].values()
    start, end = steam["four_years_window"]
    cohort = trending.well_received_released_between(games, start, end)
    this_year = {game.appid for game in trending.well_received_released_between(games, *steam["this_year_window"])}
    comparable = [game for game in games if game.total and trending.released_between(game, start, end)]
    rows = store.game_rows(conn, sorted(game.appid for game in comparable))

    def point(game: CatalogGame) -> dict:
        row = rows[game.appid]
        return {"appid": game.appid, "name": row["name"] or row["curated_title"], "details_url": f"/games/{game.appid}",
                "total": game.total, "positive": game.positive, "percent": game.percent,
                "share_tenths": dashboard.percent_tenths(game.positive, game.total),
                "review_label": game.review_label, "released_on": game.released_on,
                "genres": json.loads(row["genres"] or "[]")}

    pool = [point(game) for game in comparable]
    drawn = {game.appid for game in cohort}
    by_appid = {game["appid"]: game for game in pool}
    times = [rows[game.appid]["reviews_updated_at"] for game in comparable if rows[game.appid]["reviews_updated_at"]]
    chart = dashboard.reception_chart([by_appid[game.appid] for game in cohort], pool, this_year)
    views = list(dashboard.RECEPTION_VIEWS)
    return {
        "chart": chart,
        "view": view if view in views and chart and chart["views"]["year"] else views[0],
        "games": len(cohort),
        "this_year": len(this_year),
        "comparable": len(pool),
        "below": sorted((game for game in pool if game["appid"] not in drawn),
                        key=lambda game: (game["name"].casefold(), game["appid"])),
        "window": (start, end),
        "this_year_window": steam["this_year_window"],
        "saved": (min(times), max(times)) if times else None,  # each game's summary at its own moment of the sync
        "listed": trending.SECTION_SIZE,  # the games each list shows on the Trending page
        "min_peers": dashboard.MIN_PEERS,
        "gameplay_genres": dashboard.GAMEPLAY_GENRES,
    }


def _chart_status(game: CatalogGame, drawn: set[int], start: date) -> str:
    """Why a game of the copies-sold doughnut is or is not on the reception chart below it."""
    if game.appid in drawn:
        return "drawn"
    if game.total is None:
        return "no_reviews"
    if game.released_on is None:
        return "no_date"
    return "before_window" if game.released_on < start else "below_threshold"


def _sales_report(conn, curated: CuratedData, current_day: date, moment: datetime, view: str | None) -> dict:
    """Sales & reception: Gamalytic's estimated Steam copies sold (once collected), then the strong-reception
    scatterplot in `view`. Each doughnut game says whether it is on that chart."""
    steam = _steam_activity(conn, curated, current_day, moment)
    reception = _reception_view(conn, steam, view)
    # The doughnut's games are those of the Trending page, its Most watched list included.
    decisions = curated.twitch_category_decisions
    lists = {**_trending_lists(conn, current_day, moment, decisions),
             "watched": _tracked_list(conn, decisions, current_day, moment)}
    sales = _sales_view(conn, lists, steam, moment)
    if sales:
        drawn = {point["appid"] for point in reception["chart"]["points"]} if reception["chart"] else set()
        named = [part for part in sales["ring"]["parts"] if part["kind"] == "game"] if sales["ring"] else []
        for item in sales["ranked"] + named:  # a named slice is its own copy of its game's row
            item["chart_status"] = _chart_status(item["game"], drawn, reception["window"][0])
    return {"sales": sales, "reception": reception, "strong_labels": catalog.STRONG_RECEPTION_LABELS}


def _upcoming_report(conn, current_day: date, moment: datetime, window: str | None, group: str | None, *,
                     configured: bool, decisions: dict) -> dict:
    """Upcoming releases: the release radar narrowed to `window` (a dashboard.RADAR_WINDOWS key; any other value shows
    every tracked game), beside the comparison of IGDB's and Steam's captures (list_comparison) with `group` open (a
    list_comparison group key; by default the games seen only on IGDB). Both share one list of distinct games. For its
    panel, each game that the Most watched list names by the same IGDB id carries its entry there: TwitchTracker's rank
    and average viewers when TwitchTracker has given figures ("watched", _tracked_list), else its place in Twitch's
    newest list ("twitch"). A series category has no IGDB id, so it is never any game's entry."""
    lists = _trending_lists(conn, current_day, moment, decisions)
    tracker = _tracked_list(conn, decisions, current_day, moment)
    runs = store.recent_source_runs(conn, upcoming.SOURCE, limit=1)
    windows = [key for key, _, _ in dashboard.RADAR_WINDOWS]
    radar = dashboard.release_radar(lists["upcoming"], current_day)
    entries = {item["igdb_id"]: item for item in (tracker or lists["twitch"])["listable"] if not item["series"]}
    for game in radar["games"]:
        game["watched" if tracker else "twitch"] = next(
            (entries[igdb_id] for igdb_id in game["igdb_ids"] if igdb_id in entries), None)
    comparison = list_comparison.compare_lists(lists["upcoming"], radar["games"])
    groups = list(list_comparison.BAR_ORDER)
    return {
        "radar": radar,
        "window": window if window in windows else windows[-1],
        "comparison": comparison,
        "group": group if group in groups else list_comparison.IGDB_ONLY,
        "group_labels": list_comparison.GROUP_LABELS,
        "twitch": lists["twitch"],
        "watched": tracker,
        "igdb": {"configured": configured, "last_run": runs[0] if runs else None,
                 "last_problem": store.run_problem(runs[0]) if runs else None, "list_size": upcoming.LIST_SIZE},
    }


def _momentum_report(conn, curated: CuratedData, current_day: date, moment: datetime, player: str | None) -> dict:
    """Momentum: the review waterfall of the catalog games (dashboard.review_waterfall), and the Steam player line of
    each game with a step of its own (dashboard.player_line). `player` (an app id) picks the line shown first when
    that game has one; otherwise the largest change with a line is shown. "coverage" says what the saved snapshots
    cover, game by game, and which games the planned launch-weeks comparison could include (its only real data)."""
    steam = _steam_activity(conn, curated, current_day, moment)
    all_reviews = store.review_history(conn, "")  # every saved review count: launch weeks reach back to a release
    release_days = {appid: game.released_on for appid, game in steam["catalog"].items()
                    if game.released_on and not game.upcoming}
    waterfall = dashboard.review_waterfall(steam["review_history"])
    games = [step for step in waterfall["steps"] if step["kind"] == "game"] if waterfall else []
    rows = store.game_rows(conn, [game["appid"] for game in games])
    for game in games:
        row = rows[game["appid"]]
        game.update(name=row["name"] or row["curated_title"], details_url=f"/games/{game['appid']}",
                    # a portrait cover, as in the copies-sold chart; the wide Steam header when Steam has no cover
                    image_url=row["cover_image_url"] or row["header_image_url"])
    newest = steam["players_status"]["last"]
    lines = {game["appid"]: {**dashboard.player_line(steam["player_history"].get(game["appid"], []), newest),
                             "name": game["name"]}
             for game in games} if newest else {}
    charted = [appid for appid, found in lines.items() if found["line"]]
    chosen = int(player) if player and player.isdigit() and int(player) in charted else None

    def first_possible(status: dict) -> datetime | None:
        """When a first comparison can exist, if that is still ahead (a snapshot CHANGE_WINDOW after the first)."""
        moment_from = status["changes_possible_from"]
        return moment_from if moment_from and moment_from > moment else None

    return {
        "waterfall": waterfall,
        "waterfall_games": dashboard.WATERFALL_GAMES,
        "lines": lines,
        "player": chosen or (charted[0] if charted else None),
        "review_days": dashboard.capture_days(steam["review_history"]),
        "player_days": dashboard.capture_days(steam["player_history"], "players"),
        "reviews_from": first_possible(steam["reviews_status"]),
        "players_from": first_possible(steam["players_status"]),
        "coverage": {"reviews": dashboard.snapshot_coverage(steam["review_history"], "total"),
                     "players": dashboard.snapshot_coverage(steam["player_history"], "players"),
                     "launch": dashboard.launch_weeks_coverage(release_days, all_reviews)},
        "launch_weeks": dashboard.LAUNCH_WEEKS,
        "launch_days": dashboard.LAUNCH_BOUNDARIES[1:],  # the weekly boundaries after the release day
        "change_window_days": trending.CHANGE_WINDOW.days,
        "max_baseline_days": trending.MAX_BASELINE_AGE.days,
        "history_days": dashboard.HISTORY_DAYS,
        "line_gap_days": dashboard.PLAYER_LINE_GAP.days,
    }


# ---------------------------------------------------------------------- app


def create_app(settings: Settings | None = None, *, today: Callable[[], date] = date.today,
               now: Callable[[], datetime] = _utc_now,
               twitch_client: Callable[[], twitch.TwitchClient] | None = None,
               supabase: supabase_auth.SupabaseAuth | None = None) -> FastAPI:
    """Build the web app. Tests pass their own Settings, a fixed `today` and a fixed `now`; `serve` uses the clock.

    `twitch_client` makes the Twitch client for "Watch it live" (tests pass a fake). By default it is
    built from the IGDB credentials when they are set; the client and its token live in this process only.
    `supabase` is the Supabase Auth client of the email accounts (tests pass one backed by an in-memory Supabase); by
    default it is built from GAME_LIBRARIAN_SUPABASE_URL and GAME_LIBRARIAN_SUPABASE_PUBLISHABLE_KEY when both are set,
    and without them the site has no accounts.

    `today` gives the last day of the five-year ranking window (and of the "Recent releases"
    window): by default the calendar date of the computer that runs the site, read again on
    every request. `now` is the moment freshness is judged against (stale details, reviews
    and prices, and whether a sale badge may show).
    """
    settings = settings or load_settings()
    # An empty catalog is valid (the page explains how to sync); a PostgreSQL database must have its tables.
    store.initialize(settings.database).close()
    curated_cache = CuratedCache(settings.curated_dir)
    auth = supabase or (supabase_auth.SupabaseAuth(settings.supabase_url, settings.supabase_publishable_key)
                        if settings.accounts_enabled else None)
    secure_cookies = settings.hosted  # the hosted site is HTTPS only: Secure cookies with the __Host- prefix
    access_cookie = accounts.cookie_name(accounts.ACCESS_COOKIE, secure_cookies)
    refresh_cookie = accounts.cookie_name(accounts.REFRESH_COOKIE, secure_cookies)
    csrf_cookie = accounts.cookie_name(accounts.CSRF_COOKIE, secure_cookies)
    attempts = accounts.AttemptLimiter()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        close_pools()  # this process's PostgreSQL connections, at shutdown
        if auth is not None:
            auth.close()

    app = FastAPI(title="Game Librarian", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(
        date=format_date, datetime=format_datetime, number=format_number, hours=format_hours, day=format_day,
        rate=dashboard.format_rate, compact=dashboard.compact_number, signed=format_signed, listing=format_list,
        tenths=dashboard.format_tenths,
    )
    templates.env.globals["app_version"] = __version__  # cache-busts the stylesheet and script after an upgrade
    templates.env.globals.update(wishlist_limit=accounts.WISHLIST_LIMIT,
                                 min_password_length=accounts.MIN_PASSWORD_LENGTH,
                                 sign_in_unavailable=accounts.UNAVAILABLE)

    def open_db():
        """A connection to the catalog for one page request (a pooled one for PostgreSQL)."""
        return store.connect(settings.database, pooled=True)

    def session_check(request: Request) -> accounts.SessionCheck:
        """What the request's session cookies say (accounts.check_session). It may ask Supabase (its signing keys, a
        new session), so it runs in a worker thread."""
        return accounts.check_session(auth, request.cookies.get(access_cookie), request.cookies.get(refresh_cookie))

    @app.middleware("http")
    async def accounts_and_security_headers(request: Request, call_next):
        """Give each request its account state (request.state.account, which templates read; request.state.session,
        whose renewed or ended session the response's cookies then follow) and each response the security headers.

        A route that signs someone in or out writes the cookies itself and sets request.state.cookies_written."""
        path = request.url.path
        check = accounts.NO_SESSION
        if auth is not None and not path.startswith("/static/") and path not in ("/healthz", "/readyz") and (
                access_cookie in request.cookies or refresh_cookie in request.cookies):
            check = await run_in_threadpool(session_check, request)
        identity = check.identity
        request.state.session = check
        request.state.account = accounts.AccountState(
            auth is not None, identity,
            allowed=identity is None or accounts.is_allowed(identity, settings.allowed_users),
            unavailable=check.unavailable)
        response = await call_next(request)
        if not getattr(request.state, "cookies_written", False):
            if check.renewed:
                accounts.set_session_cookies(response, check.renewed, secure=secure_cookies)
            elif check.ended:
                accounts.clear_session_cookies(response, secure=secure_cookies)
        personal = identity is not None or path.startswith(("/account", "/auth/"))
        _security_headers(response, hosted=settings.hosted, personal=personal and not path.startswith("/static/"))
        return response
    # Today's Daily Trivia day (UTC) in the header, so the lightbulb's "finished" badge follows the server's quiz day.
    templates.env.globals["trivia_day"] = lambda: trivia.quiz_day(now()).isoformat()
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
        conn = open_db()
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
        conn = open_db()
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
            conn = open_db()
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
        conn = open_db()
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
        conn = open_db()
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
        conn = open_db()
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
        """Trending games: three report pages about the Trending page's games, from the same stored data and rules
        (no network). ?report= picks the page (dashboard.TRENDING_REPORTS; Sales & reception when it is missing or
        unknown); Sales & reception reads ?view=, Upcoming releases ?window= and ?group=, Momentum ?player=, all
        optional: an unknown value shows the page's default."""
        params = request.query_params
        report = params.get("report")
        if report not in dashboard.TRENDING_REPORTS:
            report = dashboard.DEFAULT_REPORT
        current_day, moment = today(), now()
        curated = curated_cache.get()
        conn = open_db()
        try:
            # Twitch allows keeping its data for 24 hours; an older list is deleted, not just hidden.
            store.purge_twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
            if report == "upcoming":
                view = _upcoming_report(conn, current_day, moment, params.get("window"), params.get("group"),
                                        configured=settings.igdb_configured,
                                        decisions=curated.twitch_category_decisions)
            elif report == "momentum":
                view = _momentum_report(conn, curated, current_day, moment, params.get("player"))
            else:
                view = _sales_report(conn, curated, current_day, moment, params.get("view"))
            last_sync = store.last_successful_sync(conn)
        finally:
            conn.close()
        view.update(report=report, reports=dashboard.TRENDING_REPORTS)
        return render_dashboard(request, "trending", last_sync, ranking.RankingWindow.for_website(current_day), view)

    @app.get("/dashboard/about", response_class=HTMLResponse)
    def dashboard_about(request: Request):
        """About the data: definitions, coverage and the loaded data."""
        tables, window = report_tables()
        return render_dashboard(request, "about", tables.last_sync, window, dashboard.about_page(tables, window))

    @app.get("/trending", response_class=HTMLResponse)
    def trending_page(request: Request):
        """Separate, labeled signals from stored data, in three parts: upcoming games and games that are out or
        watched now (all platforms, from IGDB and Twitch), then Steam activity (Steam only). No network calls.
        Most watched is TwitchTracker's ranking of the tracked Twitch categories ("watched", _tracked_list) once it has
        figures, else Twitch's own order."""
        current_day, moment = today(), now()
        curated = curated_cache.get()
        conn = open_db()
        try:
            # Twitch allows keeping its data for 24 hours; an older list is deleted, not just hidden.
            store.purge_twitch_top_games(conn, (moment - twitch.KEEP_FOR).isoformat(timespec="seconds"))
            steam = _steam_activity(conn, curated, current_day, moment)
            counts = store.catalog_counts(conn)
            sync_status = _sync_status(conn)
            lists = _trending_lists(conn, current_day, moment, curated.twitch_category_decisions)
            watched = _tracked_list(conn, curated.twitch_category_decisions, current_day, moment)
            igdb_run = store.recent_source_runs(conn, upcoming.SOURCE, limit=1)
        finally:
            conn.close()

        context = {
            "active": "trending",
            "counts": counts,
            "sync": sync_status,
            "size": trending.SECTION_SIZE,
            # The raw histories feed the dashboard's charts; this page lists the changes trending.py computed from them.
            **{key: value for key, value in steam.items()
               if key not in ("player_history", "review_history", "catalog")},
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
            "watched": watched,  # None until TwitchTracker has given figures: Twitch's own list is shown then
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
        conn = open_db()
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
        """The wishlist: app.js fills it from the browser, or from /api/wishlist for a signed-in visitor."""
        return templates.TemplateResponse(request, "wishlist.html", {"active": "wishlist"})

    # ------------------------------------------------------------------ email accounts (accounts.py, supabase_auth.py)
    #
    # Every form below posts to this server, which alone talks to Supabase. Each page carries this browser's form token
    # (account_response); each post is checked for it (read_account_form), counted per visitor (attempts), and answered
    # with words of this site, never Supabase's. A route that signs someone in or out writes the cookies itself.

    def account_response(request: Request, template: str, context: dict, status_code: int = 200):
        """An account page with this browser's form token in its forms (a new token, and its cookie, if it has none)."""
        token = request.cookies.get(csrf_cookie)
        fresh = not (token and accounts.CSRF_TOKEN.fullmatch(token))
        if fresh:
            token = accounts.new_csrf_token()
        response = templates.TemplateResponse(request, template, {"active": "account", "csrf_token": token, **context},
                                              status_code=status_code)
        if fresh:
            accounts.set_csrf_cookie(response, token, secure=secure_cookies)
        return response

    def no_accounts(request: Request):
        """Every account address without a Supabase project: this site has no accounts (404)."""
        return templates.TemplateResponse(request, "account.html", {"active": "account"}, status_code=404)

    async def read_account_form(request: Request) -> tuple[dict[str, str], int, str | None]:
        """The posted fields, or {} with the status and sentence that refuse them: a form that cannot be read, or one
        without this browser's form token, such as a post from another site's page."""
        form = await _read_form(request)
        if form is None:
            return {}, 400, "The form could not be read. Reload the page, then try again."
        if not accounts.form_from_this_site(request.headers.get("sec-fetch-site"), request.cookies.get(csrf_cookie),
                                            form.get("csrf_token")):
            return {}, 403, "This form has expired, or it did not come from this site. Reload the page, then try again."
        return form, 200, None

    def visitor(request: Request) -> str:
        return accounts.client_address(request, hosted=settings.hosted)

    def current_access_token(request: Request) -> str | None:
        """The verified access token of this request's session: the renewed one when the middleware just got it."""
        check: accounts.SessionCheck = request.state.session
        if check.identity is None:
            return None
        return check.renewed.access_token if check.renewed else request.cookies.get(access_cookie)

    def write_session(request: Request, response: Response, session: supabase_auth.Session) -> None:
        accounts.set_session_cookies(response, session, secure=secure_cookies)
        request.state.cookies_written = True

    def end_session(request: Request, response: Response) -> None:
        accounts.clear_session_cookies(response, secure=secure_cookies)
        request.state.cookies_written = True

    async def signed_in(request: Request, session: supabase_auth.Session, target: str) -> Response | None:
        """The redirect to `target` that keeps a new session in the cookies, once its access token passes the same
        check as every later request; None (logged) when it does not, so no page claims a sign-in that fails next."""
        try:
            user = await run_in_threadpool(auth.verify_access_token, session.access_token)
        except supabase_auth.AuthError as exc:
            refused("checking a new session", exc)
            return None
        if user is None:
            LOG.error("Supabase Auth: a new session's access token could not be verified with the project's published "
                      "signing keys; the project must sign with an asymmetric key (ES256 or RS256; DEPLOYMENT.md).")
            return None
        response = RedirectResponse(target, status_code=303)
        write_session(request, response, session)
        return response

    def refused(action: str, exc: supabase_auth.AuthError) -> tuple[int, str]:
        """The status and sentence for a refused Supabase request, logging what the site's owner must know about
        (Supabase not answering, email not set up) by its error code only, never with an email address."""
        if exc.service_problem or exc.code in ("email_address_not_authorized", "signup_disabled",
                                               "email_provider_disabled"):
            LOG.warning("Supabase Auth: %s failed with %s (HTTP %s).", action, exc.code, exc.status)
        return accounts.auth_problem(exc)

    def typed_email(form: dict[str, str]) -> str:
        """The address as typed, to fill the form in again after a problem (shortened; the page escapes it)."""
        return (form.get("email") or "").strip()[:accounts.MAX_EMAIL_LENGTH]

    @app.get("/account", response_class=HTMLResponse)
    def account_page(request: Request):
        """Sign in; or, signed in, the account: its wishlist's size, a new password, sign out, and deleting its data.
        ?next= is where signing in goes back to (a path on this site; the wishlist by default) and ?notice= picks one
        of ACCOUNT_NOTICES."""
        if auth is None:
            return no_accounts(request)
        state: accounts.AccountState = request.state.account
        count = None
        if state.signed_in:
            conn = open_db()
            try:
                row = store.find_account(conn, accounts.PROVIDER, state.identity.user_id)
                count = len(store.wishlist_items(conn, row["id"])) if row else 0
            finally:
                conn.close()
        context = {"count": count, "notice": ACCOUNT_NOTICES.get(request.query_params.get("notice") or ""),
                   "return_to": accounts.safe_return_path(request.query_params.get("next") or "/wishlist")}
        return account_response(request, "account.html", context)

    @app.post("/account/sign-in", response_class=HTMLResponse)
    async def account_sign_in(request: Request):
        """Sign in with an email address and a password, then go to the form's `next` (a path on this site)."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        email, password = accounts.clean_email(form.get("email")), form.get("password", "")
        context = {"email": typed_email(form), "return_to": accounts.safe_return_path(form.get("next") or "/wishlist")}
        if not problem and (email is None or not password):
            status, problem = 400, "Enter your email address and your password."
        if not problem and not attempts.allow("sign_in", visitor(request), email):
            status, problem = 429, accounts.TOO_MANY
        if problem:
            return account_response(request, "account.html", {**context, "error": problem}, status)
        try:
            session = await run_in_threadpool(auth.sign_in, email, password)
        except supabase_auth.AuthError as exc:
            status, message = refused("sign-in", exc)
            return account_response(request, "account.html", {
                **context, "error": message, "unconfirmed": exc.code == "email_not_confirmed"}, status)
        return (await signed_in(request, session, context["return_to"])
                or account_response(request, "account.html", {**context, "error": accounts.UNAVAILABLE}, 503))

    @app.get("/account/sign-up", response_class=HTMLResponse)
    def account_sign_up_page(request: Request):
        """The form that creates an account: an email address and a password, typed twice."""
        if auth is None:
            return no_accounts(request)
        return account_response(request, "account_flow.html", {"page": "sign_up"})

    @app.post("/account/sign-up", response_class=HTMLResponse)
    async def account_sign_up(request: Request):
        """Create an account; Supabase emails the link that confirms the address. The answer is the same whether or not
        the address already has an account, so this page never tells anyone who has one."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        email, password = accounts.clean_email(form.get("email")), form.get("password", "")
        context = {"page": "sign_up", "email": typed_email(form)}
        if not problem and email is None:
            status, problem = 400, "Enter a valid email address."
        if not problem:
            weak = accounts.password_problem(password, form.get("password_confirm", ""))
            status, problem = (400, weak) if weak else (status, None)
        if not problem and not attempts.allow("sign_up", visitor(request), email):
            status, problem = 429, accounts.TOO_MANY
        if problem:
            return account_response(request, "account_flow.html", {**context, "error": problem}, status)
        session = None
        try:
            session = await run_in_threadpool(auth.sign_up, email, password)
        except supabase_auth.AuthError as exc:
            if exc.code not in ("user_already_exists", "email_exists"):  # an existing account: the same answer
                status, message = refused("sign-up", exc)
                return account_response(request, "account_flow.html", {**context, "error": message}, status)
        if session is not None:  # the Supabase project does not ask for confirmation: signed in at once
            return (await signed_in(request, session, "/account?notice=signed_up")
                    or account_response(request, "account_flow.html", {**context, "error": accounts.UNAVAILABLE}, 503))
        return account_response(request, "account_flow.html", {"page": "sent", "sent": "confirmation", "email": email})

    @app.post("/account/resend", response_class=HTMLResponse)
    async def account_resend(request: Request):
        """Send a new confirmation link to an account that waits for its address to be confirmed (the same answer
        whether or not one does)."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        email = accounts.clean_email(form.get("email"))
        context = {"email": typed_email(form), "unconfirmed": True, "return_to": "/wishlist"}
        if not problem and email is None:
            status, problem = 400, "Enter the email address of your account."
        if not problem and not attempts.allow("email_link", visitor(request), email):
            status, problem = 429, accounts.TOO_MANY
        if not problem:
            try:
                await run_in_threadpool(auth.resend_confirmation, email)
            except supabase_auth.AuthError as exc:
                status, problem = refused("resend", exc)
        if problem:
            return account_response(request, "account.html", {**context, "error": problem}, status)
        return account_response(request, "account_flow.html", {"page": "sent", "sent": "resend", "email": email})

    @app.get("/account/forgot-password", response_class=HTMLResponse)
    def account_forgot_page(request: Request):
        """The form that asks for a password-reset link."""
        if auth is None:
            return no_accounts(request)
        return account_response(request, "account_flow.html", {"page": "forgot"})

    @app.post("/account/forgot-password", response_class=HTMLResponse)
    async def account_forgot(request: Request):
        """Email a link to choose a new password (the same answer whether or not the address has an account)."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        email = accounts.clean_email(form.get("email"))
        context = {"page": "forgot", "email": typed_email(form)}
        if not problem and email is None:
            status, problem = 400, "Enter the email address of your account."
        if not problem and not attempts.allow("email_link", visitor(request), email):
            status, problem = 429, accounts.TOO_MANY
        if not problem:
            try:
                await run_in_threadpool(auth.send_password_reset, email)
            except supabase_auth.AuthError as exc:
                status, problem = refused("password reset", exc)
        if problem:
            return account_response(request, "account_flow.html", {**context, "error": problem}, status)
        return account_response(request, "account_flow.html", {"page": "sent", "sent": "reset", "email": email})

    @app.get("/auth/confirm", response_class=HTMLResponse)
    def auth_confirm_page(request: Request):
        """Where the emails' links lead (?token_hash=...&type=email or recovery; DEPLOYMENT.md, "Email templates").

        The page only shows a button: the link is used when its owner presses it (a POST), because some email
        services open every link of a message in advance, and that would use up a one-time link before its owner
        sees it (Supabase's guide, "Email prefetching"). A link that Supabase sends back with an error says so."""
        if auth is None:
            return no_accounts(request)
        params = request.query_params
        token_hash, link_type = params.get("token_hash") or "", params.get("type") or ""
        if (params.get("error") or params.get("error_code") or not accounts.TOKEN_HASH.fullmatch(token_hash)
                or link_type not in supabase_auth.EMAIL_LINK_TYPES):
            return account_response(request, "account_flow.html", {"page": "link_problem"}, 400)
        return account_response(request, "account_flow.html",
                                {"page": "confirm", "token_hash": token_hash, "link_type": link_type})

    @app.post("/auth/confirm", response_class=HTMLResponse)
    async def auth_confirm(request: Request):
        """Use an email link (Supabase checks its token_hash) and sign its owner in: a confirmed address goes to the
        account page, a password reset to the form that sets the new password."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        token_hash, link_type = form.get("token_hash") or "", form.get("type") or ""
        context = {"page": "confirm", "token_hash": token_hash, "link_type": link_type}
        if not problem and (not accounts.TOKEN_HASH.fullmatch(token_hash)
                            or link_type not in supabase_auth.EMAIL_LINK_TYPES):
            return account_response(request, "account_flow.html", {"page": "link_problem"}, 400)
        if not problem and not attempts.allow("confirm", visitor(request)):
            status, problem = 429, accounts.TOO_MANY
        if problem:
            return account_response(request, "account_flow.html", {**context, "error": problem}, status)
        try:
            session = await run_in_threadpool(auth.verify_email_link, token_hash, link_type)
        except supabase_auth.AuthError as exc:
            status, message = refused("email link", exc)
            if exc.service_problem or status == 429:  # Supabase did not decide: the link may still work, so it stays
                return account_response(request, "account_flow.html", {**context, "error": message}, status)
            return account_response(request, "account_flow.html", {"page": "link_problem"}, 400)
        target = "/account/password?notice=recovery" if link_type == "recovery" else "/account?notice=confirmed"
        return (await signed_in(request, session, target)
                or account_response(request, "account_flow.html", {"page": "link_used", "link_type": link_type}, 503))

    @app.get("/account/password", response_class=HTMLResponse)
    def account_password_page(request: Request):
        """The form that sets a new password, for a signed-in person (after a password-reset link, or by choice)."""
        if auth is None:
            return no_accounts(request)
        if request.state.account.identity is None:
            return RedirectResponse("/account?next=/account/password", status_code=303)
        return account_response(request, "account_flow.html",
                                {"page": "password", "recovery": request.query_params.get("notice") == "recovery"})

    @app.post("/account/password", response_class=HTMLResponse)
    async def account_password(request: Request):
        """Save the signed-in person's new password at Supabase."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        token = current_access_token(request)
        if not problem and token is None:
            return RedirectResponse("/account?next=/account/password", status_code=303)
        password = form.get("password", "")
        if not problem:
            weak = accounts.password_problem(password, form.get("password_confirm", ""))
            status, problem = (400, weak) if weak else (status, None)
        if not problem and not attempts.allow("password", visitor(request)):
            status, problem = 429, accounts.TOO_MANY
        if not problem:
            try:
                await run_in_threadpool(auth.set_password, token, password)
            except supabase_auth.AuthError as exc:
                status, problem = refused("new password", exc)
        if problem:
            return account_response(request, "account_flow.html", {"page": "password", "error": problem}, status)
        return RedirectResponse("/account?notice=password_changed", status_code=303)

    @app.post("/account/sign-out")
    async def account_sign_out(request: Request):
        """End the session at Supabase and delete this browser's session cookies."""
        if auth is None:
            return no_accounts(request)
        form, status, problem = await read_account_form(request)
        if problem:
            return account_response(request, "account.html", {"error": problem, "return_to": "/wishlist"}, status)
        token = current_access_token(request)
        if token:
            try:
                await run_in_threadpool(auth.sign_out, token)
            except supabase_auth.AuthError as exc:  # the cookies are deleted all the same
                refused("sign-out", exc)
        response = RedirectResponse("/account?notice=signed_out", status_code=303)
        end_session(request, response)
        return response

    # ------------------------------------------------------------------ the signed-in person's wishlist (JSON API)

    def api_identity(request: Request) -> tuple[accounts.Identity | None, JSONResponse | None]:
        """The signed-in person who may keep an account, or the error that answers the API request."""
        state: accounts.AccountState = request.state.account
        if not state.enabled:
            return None, _json_error(404, "sign_in_off", "This site has no accounts; the wishlist stays in the browser.")
        if state.identity is None and state.unavailable:
            return None, _json_error(503, "sign_in_unavailable", accounts.UNAVAILABLE)
        if state.identity is None:
            return None, _json_error(401, "sign_in_required", "Sign in to keep your wishlist in your account.")
        if not state.allowed:
            return None, _json_error(403, "not_allowed", "This email address is not on this site's list of allowed "
                                                         "accounts. The wishlist stays in this browser.")
        return state.identity, None

    def for_account(identity: accounts.Identity, work: Callable):
        """`work(conn, account_id)` for the person's own account (made at first use); nothing else is reachable."""
        conn = open_db()
        try:
            account = store.account_id(conn, accounts.PROVIDER, identity.user_id, identity.email,
                                       now=now().isoformat(timespec="seconds"))
            return work(conn, account)
        finally:
            conn.close()

    def wishlist_body(conn, account: int) -> dict:
        items = store.wishlist_items(conn, account)
        return {"items": [{"id": row["game_key"], "name": row["name"]} for row in items],
                "limit": accounts.WISHLIST_LIMIT}

    def api_answer(body: dict) -> JSONResponse:
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/api/wishlist")
    def wishlist_read(request: Request):
        """The signed-in person's wishlist: {"items": [{"id", "name"}, ...] oldest first, "limit"}."""
        identity, error = api_identity(request)
        return error or api_answer(for_account(identity, wishlist_body))

    @app.post("/api/wishlist/toggle")
    async def wishlist_toggle(request: Request):
        """Save a game, or remove it when it is saved under its key or an alias: {"id", "name", "aliases"}."""
        identity, error = api_identity(request)
        error = error or _same_site_json(request)
        entry = None if error else accounts.wishlist_entry(await _read_json(request))
        if error or entry is None:
            return error or _json_error(400, "invalid", "The game could not be read from the request.")
        key, name, aliases = entry

        def work(conn, account):
            saved = store.toggle_wishlist_item(conn, account, key, name, aliases,
                                               now=now().isoformat(timespec="seconds"), limit=accounts.WISHLIST_LIMIT)
            return saved, wishlist_body(conn, account)

        saved, body = await run_in_threadpool(for_account, identity, work)
        if saved is None:
            return _json_error(409, "wishlist_full", f"Your wishlist already holds {accounts.WISHLIST_LIMIT} games, "
                                                     "the most it can keep. Remove one to add another.")
        return api_answer({"saved": saved, **body})

    @app.post("/api/wishlist/remove")
    async def wishlist_remove(request: Request):
        """Remove one saved game: {"id"}."""
        identity, error = api_identity(request)
        error = error or _same_site_json(request)
        payload = None if error else await _read_json(request)
        key = payload.get("id") if isinstance(payload, dict) else None
        if error or not isinstance(key, str) or not accounts.GAME_KEY.fullmatch(key):
            return error or _json_error(400, "invalid", "The game could not be read from the request.")

        def work(conn, account):
            store.remove_wishlist_item(conn, account, key)
            return wishlist_body(conn, account)

        return api_answer(await run_in_threadpool(for_account, identity, work))

    @app.post("/api/wishlist/import")
    async def wishlist_import(request: Request):
        """Add the games of the browser's wishlist that the account does not have: {"items": [{"id", "name"}]}.
        The browser keeps its own copy; only its owner removes it."""
        identity, error = api_identity(request)
        error = error or _same_site_json(request)
        entries = None if error else accounts.import_entries(await _read_json(request))
        if error or entries is None:
            return error or _json_error(400, "invalid", "The browser's wishlist could not be read from the request.")

        def work(conn, account):
            counts = store.import_wishlist_items(conn, account, entries, now=now().isoformat(timespec="seconds"),
                                                 limit=accounts.WISHLIST_LIMIT)
            return {**counts, **wishlist_body(conn, account)}

        return api_answer(await run_in_threadpool(for_account, identity, work))

    @app.post("/api/account/delete")
    async def account_delete(request: Request):
        """Delete the signed-in person's account and wishlist on this site, then sign them out. Their sign-in at
        Supabase stays: removing it needs Supabase's secret key, which this site does not hold (DEPLOYMENT.md)."""
        identity, error = api_identity(request)
        error = error or _same_site_json(request)
        if error:
            return error

        def work():
            conn = open_db()
            try:
                row = store.find_account(conn, accounts.PROVIDER, identity.user_id)
                if row is not None:
                    store.delete_account(conn, row["id"])
            finally:
                conn.close()

        await run_in_threadpool(work)
        token = current_access_token(request)
        if token:
            try:
                await run_in_threadpool(auth.sign_out, token)
            except supabase_auth.AuthError as exc:  # the cookies are deleted all the same
                refused("sign-out after deleting an account", exc)
        response = api_answer({"deleted": True})
        end_session(request, response)
        return response

    # ------------------------------------------------------------------ health probes (Container Apps)

    @app.get("/healthz")
    def healthz():
        """Liveness and startup probe: the process answers. It reads nothing, so a database outage never makes the
        platform restart a container that works."""
        return api_answer({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        """Readiness probe: the catalog database answers a query (503 otherwise; the reason goes to the log only)."""
        try:
            conn = open_db()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:  # any failure means "not ready"; messages name the server, never a password
            LOG.warning("Readiness check: the catalog database did not answer (%s: %s)", type(exc).__name__, exc)
            return JSONResponse({"status": "unavailable"}, status_code=503, headers={"Cache-Control": "no-store"})
        return api_answer({"status": "ok"})

    @app.get("/trivia/panel", response_class=HTMLResponse)
    def trivia_panel(request: Request):
        """Daily Trivia's content as an HTML fragment for the popup: today's quiz (the quiz day is the UTC date of
        `now`), the facts of earlier quizzes and today's facts, marked. A game links its details only when it is
        in the catalog. Reads the curated file and the catalog only: answers and scores stay in the browser."""
        daily = trivia.daily_trivia(trivia.load_trivia(settings.curated_dir), trivia.quiz_day(now()))
        conn = open_db()
        try:
            in_catalog = {row["appid"] for row in store.matching_games(conn, [], [])}
        finally:
            conn.close()
        return templates.TemplateResponse(request, "trivia_panel.html",
                                          {"daily": daily, "in_catalog": in_catalog, "reset_label": trivia.RESET_LABEL})

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
                conn = open_db()
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

        conn = open_db()
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
