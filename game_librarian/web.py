"""FastAPI web app: a read-only, server-rendered view of the stored catalog.

Page requests never call Steam. They read the SQLite catalog written by the sync
command and the curated CSV files, so the site keeps serving the last saved data
when Steam is unavailable. The four-year ranking is recalculated on every request
from the stored review counts, the verified labels and today's date (see
ranking.py), so it follows the latest sync and its window moves forward every day.

Views share the same search, filters, sort orders and pagination: "Top-ranked games"
(the default) shows only the games released in the four-year window, "All games" shows
the whole catalog, and collections (catalog.py) show the games that meet a documented
rule. A card's number is always its place in the four-year ranking of the whole catalog;
no view, filter or sort recalculates it.

The Dashboard pages (dashboard.py) summarize the same catalog with the Power BI report's
measures, drawn as HTML.

Run with `python -m game_librarian serve` (uses the `create_app` factory).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, catalog, dashboard, ranking, related, store
from .catalog import ALL_VIEW, COLLECTION_KEYS, DEFAULT_VIEW, LABEL_ORDER, SORT_ALIASES, SORT_OPTIONS, SORTS, \
    TOP_VIEW, VIEWS, CatalogGame
from .config import PACKAGE_DIR, STORE_PAGE_URL, Settings, load_settings
from .curated import PROMOTED, Award, CuratedCache, CuratedData
from .prepare import concise_description, normalize_for_search, percent_positive, shorten
from .report_export import ReportTables, collect_tables

PAGE_SIZE = 12  # two cards per row on desktop: six rows per page
SIDEBAR_TAG_LIMIT = 40
MAX_SELECTED_TAGS = 10
MAX_QUERY_LENGTH = 100
CARD_TAG_COUNT = 3
CARD_AWARD_COUNT = 3  # a card lists this many awards; the details list them all
DETAILS_TAG_COUNT = 12
DETAILS_EXCERPT_CHARS = 280  # keeps each review excerpt to a few lines in the details dialog
DETAILS_STALE_AFTER = timedelta(days=14)
REVIEWS_STALE_AFTER = timedelta(days=7)
STEAM_REVIEWS_URL = "https://steamcommunity.com/app/{appid}/reviews/"

UNCLASSIFIED = "Unclassified"
LABEL_FILTERS = (*LABEL_ORDER, UNCLASSIFIED)
ANY_AWARD = "any"
# Steam's game-mode categories, by their English names (prepare.MODE_CATEGORY_IDS selects them by id).
MODE_FILTERS = ("Single-player", "Multi-player", "Co-op", "Online Co-op", "Shared/Split Screen Co-op",
                "LAN Co-op", "PvP", "Online PvP", "Shared/Split Screen PvP", "Shared/Split Screen",
                "Cross-Platform Multiplayer", "MMO")


# ---------------------------------------------------------------------- formatting helpers


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_date(value: str | None) -> str:
    """'2026-11-09T21:47:02+00:00' -> '9 Nov 2026'."""
    moment = _parse_time(value)
    return f"{moment.day} {moment:%b %Y}" if moment else "never"


def format_datetime(value: str | None) -> str:
    """'2026-11-09T21:47:02+00:00' -> '9 Nov 2026, 21:47 UTC'."""
    moment = _parse_time(value)
    return f"{moment.day} {moment:%b %Y, %H:%M} UTC" if moment else "never"


def format_day(value: date | None) -> str:
    """date(2026, 11, 9) -> '9 Nov 2026' (the same style as format_date)."""
    return f"{value.day} {value:%b %Y}" if value else ""


def format_number(value: int | None) -> str:
    return f"{value or 0:,}"


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


@dataclass(frozen=True)
class CatalogQuery:
    """The view, search text, filters and sort order of a catalog page. Every link keeps them."""

    q: str = ""
    tags: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    award: str = ""  # "" = any category, "any" = any listed award, otherwise one award category
    award_org: str = ""  # "" = any organization
    award_year: str = ""  # "" = any award year, otherwise a four-digit year
    winners: bool = False  # only award winners (matching the other award choices)
    sort: str = ""  # "" = the view's default order
    view: str = DEFAULT_VIEW

    @classmethod
    def from_params(cls, params, choices: AwardChoices) -> CatalogQuery:
        """Read the query string. Unknown or invalid values fall back to the defaults."""
        tags: list[str] = []
        for tag in params.getlist("tag"):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
        wanted_labels = set(params.getlist("label"))
        wanted_modes = set(params.getlist("mode"))
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
        return bool(self.q or self.tags or self.modes or self.labels or self.has_award_filter)


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
    """A game matches when it has any wanted label; "Unclassified" matches games without one."""
    if not wanted:
        return True
    return any(label in wanted for label in labels) or (UNCLASSIFIED in wanted and not labels)


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
    usable = ranking.unranked_reason(positive, total) is None
    return {
        "fetched": row["reviews_updated_at"] is not None,
        "usable": usable,
        "total": total or 0,
        "positive_count": positive if usable else None,  # the number the four-year ranking sorts by
        "percent": percent_positive(positive, total) if usable else None,
        "score_desc": row["review_score_desc"],
    }


def _placement_view(place: ranking.Placement) -> dict:
    """The ranking facts shown on a card and in the details."""
    return {
        "rank": place.rank,
        "in_window": place.in_window,
        "tier_name": place.tier_name,
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
               query: CatalogQuery, page: int) -> dict:
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


def _details_view(row, tags: list[str], excerpts: dict, curated: CuratedData, place: ranking.Placement,
                  now: datetime, *, related_games: list[dict], related_note: str) -> dict:
    appid = row["appid"]
    labels = curated.labels_for(appid)
    tag_view = TOP_VIEW if place.in_window else ALL_VIEW  # a tag link always lists the game it came from
    return {
        "appid": appid,
        "name": row["name"] or row["curated_title"],
        **_placement_view(place),
        "labels": [
            {"name": name, "summary": labels[name].evidence_summary, "url": labels[name].evidence_url,
             "checked_on": labels[name].checked_on}
            for name in LABEL_ORDER if name in labels
        ],
        "tags": [{"name": tag, "url": CatalogQuery(tags=(tag,), view=tag_view).url()} for tag in tags[:DETAILS_TAG_COUNT]],
        "is_dlc": row["app_type"] == "dlc",
        "developers": json.loads(row["developers"] or "[]"),
        "publishers": json.loads(row["publishers"] or "[]"),
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


# ---------------------------------------------------------------------- app


def create_app(settings: Settings | None = None, *, today: Callable[[], date] = date.today) -> FastAPI:
    """Build the web app. Tests pass their own Settings and a fixed `today`; `serve` uses the environment.

    `today` gives the last day of the four-year ranking window (and of the "Recent releases"
    window): by default the calendar date of the computer that runs the site, read again on
    every request.
    """
    settings = settings or load_settings()
    store.initialize(settings.db_path).close()  # an empty catalog is valid: the page explains how to sync
    curated_cache = CuratedCache(settings.curated_dir)

    app = FastAPI(title="Game Librarian", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(
        date=format_date, datetime=format_datetime, number=format_number, hours=format_hours, day=format_day,
        rate=dashboard.format_rate, compact=dashboard.compact_number,
    )
    templates.env.globals["app_version"] = __version__  # cache-busts the stylesheet and script after an upgrade

    def request_query(request: Request) -> tuple[CatalogQuery, int]:
        """The catalog query and page number a details link carries, so its links keep the context."""
        query = CatalogQuery.from_params(request.query_params, award_choices(curated_cache.get()))
        return query, _page_number(request.query_params.get("page", "1"))

    def load_details(appid: int, query: CatalogQuery, page: int) -> dict | None:
        """Everything the details view needs for one visible game, or None if it is not shown."""
        curated = curated_cache.get()
        conn = store.connect(settings.db_path)
        try:
            row = store.game_rows(conn, [appid]).get(appid)
            if row is None or not row["is_curated"] or row["details_updated_at"] is None:
                return None
            window = ranking.RankingWindow.ending_on(today())
            catalog_rows = store.matching_games(conn, [], [])
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
        finally:
            conn.close()
        return _details_view(
            row, tags, excerpts, curated, placements[appid], datetime.now(timezone.utc),
            related_games=_related_view(suggestions, related_rows, query, page),
            related_note="" if suggestions else related.no_related_reason(appid, top_tags),
        )

    def report_tables() -> tuple[ReportTables, ranking.RankingWindow]:
        """The dashboard's data: the report tables for today's four-year window, read on every request."""
        window = ranking.RankingWindow.ending_on(today())
        conn = store.connect(settings.db_path)
        try:
            return collect_tables(conn, curated_cache.get(), window), window
        finally:
            conn.close()

    def render_dashboard(request: Request, section: str, tables: ReportTables, window: ranking.RankingWindow,
                         view: dict):
        context = {"active": "dashboard", "section": section, "dash": view, "window": window,
                   "last_sync": tables.last_sync}
        return templates.TemplateResponse(request, f"dashboard_{section}.html", context)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_reception(request: Request):
        """Player reception: the first page of the Power BI report, as HTML."""
        tables, window = report_tables()
        return render_dashboard(request, "reception", tables, window,
                                dashboard.reception_page(tables, request.query_params))

    @app.get("/dashboard/awards", response_class=HTMLResponse)
    def dashboard_awards(request: Request):
        """Awards: the report's Awards Explorer page, as HTML."""
        tables, window = report_tables()
        return render_dashboard(request, "awards", tables, window, dashboard.awards_page(tables, request.query_params))

    @app.get("/dashboard/about", response_class=HTMLResponse)
    def dashboard_about(request: Request):
        """About the data: definitions, coverage and the loaded data."""
        tables, window = report_tables()
        return render_dashboard(request, "about", tables, window, dashboard.about_page(tables, window))

    @app.get("/wishlist", response_class=HTMLResponse)
    def wishlist_page(request: Request):
        return templates.TemplateResponse(request, "wishlist.html", {"active": "wishlist"})

    @app.get("/games/{appid}/panel", response_class=HTMLResponse)
    def game_panel(request: Request, appid: int):
        """The details as an HTML fragment for the in-page dialog. The query string is the list's context."""
        query, page = request_query(request)
        game = load_details(appid, query, page)
        return templates.TemplateResponse(request, "panel.html", {"game": game}, status_code=200 if game else 404)

    @app.get("/games/{appid}", response_class=HTMLResponse)
    def game_page(request: Request, appid: int):
        """The details as a full page: a direct link, and the fallback when JavaScript is off."""
        query, page = request_query(request)
        back_url = query.url(page) + f"#game-{appid}"
        game = load_details(appid, query, page)
        context = {"game": game, "back_url": back_url}
        return templates.TemplateResponse(request, "game.html", context, status_code=200 if game else 404)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        curated = curated_cache.get()
        choices = award_choices(curated)
        query = CatalogQuery.from_params(request.query_params, choices)
        page_number = _page_number(request.query_params.get("page", "1"))
        words = normalize_for_search(query.q).split()
        current_day = today()
        window = ranking.RankingWindow.ending_on(current_day)

        conn = store.connect(settings.db_path)
        try:
            catalog_rows = store.matching_games(conn, [], [])
            # Placed over the whole catalog, so search, filters, views and sorts never change a game's rank.
            placements = ranking.place_catalog(catalog_rows, curated, window)
            searched_rows = store.matching_games(conn, words, list(query.tags)) if (words or query.tags) else catalog_rows
            searched = catalog.catalog_games(searched_rows, curated, placements)
            mode_ok = {game.appid: set(query.modes).issubset(game.modes) for game in searched}
            label_ok = {game.appid: _label_matches(game.labels, query.labels) for game in searched}
            award_ok = {game.appid: _award_matches(game, query) for game in searched}
            members = {key: {game.appid for game in searched if view.includes(game, current_day)}
                       for key, view in VIEWS.items()}
            in_view = members[query.view]
            matches = [game for game in searched if mode_ok[game.appid] and label_ok[game.appid] and award_ok[game.appid]]
            results = catalog.sort_games([game for game in matches if game.appid in in_view], query.order, current_day)
            total_pages = max(1, math.ceil(len(results) / PAGE_SIZE))
            if page_number > total_pages:
                return RedirectResponse(query.url(total_pages), status_code=303)
            start = (page_number - 1) * PAGE_SIZE
            page_appids = [game.appid for game in results[start : start + PAGE_SIZE]]
            rows = store.game_rows(conn, page_appids)
            tags_by_game = store.tags_for(conn, page_appids)
            tag_facets = store.tag_counts(conn, [game.appid for game in results], limit=SIDEBAR_TAG_LIMIT)
            counts = store.catalog_counts(conn)
            sync_status = _sync_status(conn)
        finally:
            conn.close()

        # Option counts follow the view and the other active filters, so each number says what choosing it would show.
        viewed = [game for game in searched if game.appid in in_view]
        label_counts: Counter = Counter()
        for game in viewed:
            if award_ok[game.appid] and mode_ok[game.appid]:
                label_counts.update(game.labels or (UNCLASSIFIED,))
        award_counts = _award_option_counts(
            [game for game in viewed if label_ok[game.appid] and mode_ok[game.appid]], query, choices)
        mode_counts = Counter(mode for game in results for mode in set(game.modes))
        facet_names = {facet["tag"] for facet in tag_facets}

        def view_link(key: str) -> dict:
            """A view or collection link that keeps the search and filters, with the games it would show."""
            return {"key": key, "label": VIEWS[key].label, "url": replace(query, view=key).url(),
                    "current": key == query.view, "games": sum(1 for game in matches if game.appid in members[key])}

        window_matches = sum(1 for game in matches if game.in_window)
        catalog_appids = [row["appid"] for row in catalog_rows]
        context = {
            "query": query,
            "view": VIEWS[query.view],
            # Each tab keeps the search and filters, and counts the games it would show with them.
            "view_links": [view_link(key) for key in (TOP_VIEW, ALL_VIEW)],
            "collection_links": [view_link(key) for key in COLLECTION_KEYS],
            "window": window,
            "recent_window": catalog.recent_window(current_day),
            "sort": SORTS[query.order],
            "sort_options": SORT_OPTIONS,
            "mode_options": [
                {"name": mode, "games": mode_counts[mode], "selected": mode in query.modes} for mode in MODE_FILTERS
            ],
            "label_options": [
                {"name": name, "games": label_counts[name], "selected": name in query.labels} for name in LABEL_FILTERS
            ],
            "award_options": [{"value": ANY_AWARD, "label": "Any listed award", "games": award_counts["any"]}]
            + [{"value": category, "label": category, "games": award_counts["categories"][category]}
               for category in choices.categories],
            "award_org_options": [{"value": org, "games": award_counts["organizations"][org]}
                                  for org in choices.organizations],
            "award_year_options": [{"value": str(year), "games": award_counts["years"][year]} for year in choices.years],
            "award_organizations": choices.organizations,
            "tag_options": [
                {"tag": facet["tag"], "games": facet["games"], "selected": facet["tag"] in query.tags}
                for facet in tag_facets
            ] + [{"tag": tag, "games": 0, "selected": True} for tag in query.tags if tag not in facet_names],
            "active_filters": _active_filters(query),
            "games": [
                _card_view(rows[appid], tags_by_game[appid], curated, placements[appid], query, page_number)
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
