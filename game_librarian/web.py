"""FastAPI web app: a read-only, server-rendered view of the stored catalog.

Page requests never call Steam. They read the SQLite catalog written by the sync
command and the curated CSV files, so the site keeps serving the last saved data
when Steam is unavailable. The four-year ranking is recalculated on every request
from the stored review counts, the verified labels and today's date (see
ranking.py), so it follows the latest sync and its window moves forward every day.

Two views share the same search, filters and pagination: "Top-ranked games" (the
default) shows only the games released in the four-year window, and "All games"
shows the whole catalog, where only games in the four-year ranking carry a number.

Run with `python -m game_librarian serve` (uses the `create_app` factory).
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, ranking, store
from .config import PACKAGE_DIR, STORE_PAGE_URL, Settings, load_settings
from .curated import Award, CuratedCache, CuratedData
from .prepare import concise_description, normalize_for_search, percent_positive, shorten

PAGE_SIZE = 12  # two cards per row on desktop: six rows per page
SIDEBAR_TAG_LIMIT = 40
MAX_SELECTED_TAGS = 10
MAX_QUERY_LENGTH = 100
CARD_TAG_COUNT = 3
DETAILS_TAG_COUNT = 12
DETAILS_EXCERPT_CHARS = 280  # keeps each review excerpt to a few lines in the details dialog
DETAILS_STALE_AFTER = timedelta(days=14)
REVIEWS_STALE_AFTER = timedelta(days=7)
STEAM_REVIEWS_URL = "https://steamcommunity.com/app/{appid}/reviews/"
POWER_BI_HOST = "app.powerbi.com"

LABEL_ORDER = ("AAA", "AA", "Indie")
UNCLASSIFIED = "Unclassified"
LABEL_FILTERS = (*LABEL_ORDER, UNCLASSIFIED)
ANY_AWARD = "any"


@dataclass(frozen=True)
class SortOption:
    """One way to order the catalog: its URL value and menu label."""

    key: str
    label: str


SORT_OPTIONS = (
    SortOption("rank", "Rank (tier, then positive reviews)"),
    SortOption("reviews", "Most Steam reviews"),
    SortOption("positive", "Highest positive share"),
    SortOption("name", "Name (A–Z)"),
)
SORTS = {option.key: option for option in SORT_OPTIONS}


@dataclass(frozen=True)
class CatalogView:
    """A set of catalog games: its URL value, tab label, heading and default order."""

    key: str
    label: str
    heading: str
    default_sort: str


TOP_VIEW, ALL_VIEW = "top", "all"
VIEWS = {
    # Only the games released in the four-year window, in rank order.
    TOP_VIEW: CatalogView(TOP_VIEW, "Top-ranked · last four years", "Top-ranked games", "rank"),
    # The whole catalog. A–Z by default, so older games are never listed as if they were ranked.
    ALL_VIEW: CatalogView(ALL_VIEW, "All games", "All games", "name"),
}
DEFAULT_VIEW = TOP_VIEW


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
class CatalogQuery:
    """The view, search text, filters and sort order of a catalog page. Every link keeps them."""

    q: str = ""
    tags: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    award: str = ""  # "" = no award filter, "any" = any listed award, otherwise one award category
    winners: bool = False  # only award winners (in the chosen category, if any)
    sort: str = ""  # "" = the view's default order
    view: str = DEFAULT_VIEW

    @classmethod
    def from_params(cls, params, award_categories: list[str]) -> CatalogQuery:
        """Read the query string. Unknown or invalid values fall back to the defaults."""
        tags: list[str] = []
        for tag in params.getlist("tag"):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
        wanted_labels = set(params.getlist("label"))
        award = params.get("award", "").strip()
        view = params.get("view", DEFAULT_VIEW)
        view = view if view in VIEWS else DEFAULT_VIEW
        sort = params.get("sort", "")
        # A sort equal to the view's default is stored as "", so switching views keeps only a deliberate choice.
        if sort not in SORTS or sort == VIEWS[view].default_sort:
            sort = ""
        return cls(
            q=params.get("q", "").strip()[:MAX_QUERY_LENGTH],
            tags=tuple(tags[:MAX_SELECTED_TAGS]),
            labels=tuple(label for label in LABEL_FILTERS if label in wanted_labels),
            award=award if award == ANY_AWARD or award in award_categories else "",
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
        params.extend(("label", label) for label in self.labels)
        if self.award:
            params.append(("award", self.award))
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
    def has_filters(self) -> bool:
        return bool(self.q or self.tags or self.labels or self.award or self.winners)


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
    category = "any listed award" if query.award in ("", ANY_AWARD) else query.award
    return f"Won {category}" if query.winners else f"Award: {category}"


def _active_filters(query: CatalogQuery) -> list[dict]:
    """One removable chip per active filter; each link drops just that filter."""
    chips = []
    if query.q:
        chips.append({"label": f"Name: “{query.q}”", "url": replace(query, q="").url()})
    for label in query.labels:
        chips.append({"label": label, "url": replace(query, labels=tuple(x for x in query.labels if x != label)).url()})
    if query.award or query.winners:
        chips.append({"label": _award_filter_label(query), "url": replace(query, award="", winners=False).url()})
    for tag in query.tags:
        chips.append({"label": tag, "url": replace(query, tags=tuple(x for x in query.tags if x != tag)).url()})
    return chips


# ---------------------------------------------------------------------- filtering and sorting


def _award_categories(curated: CuratedData) -> list[str]:
    """Award categories that can be shown (verified and mapped to a Steam app), in file order."""
    return list(dict.fromkeys(a.category for a in curated.awards if a.review_status == "verified" and a.appid))


def _verified_labels(curated: CuratedData, appid: int) -> list[str]:
    """A game's verified AAA / AA / Indie labels, in display order."""
    labels = curated.labels_for(appid)
    return [name for name in LABEL_ORDER if name in labels]


def _label_matches(labels: list[str], wanted: tuple[str, ...]) -> bool:
    """A game matches when it has any wanted label; "Unclassified" matches games without one."""
    if not wanted:
        return True
    return any(label in wanted for label in labels) or (UNCLASSIFIED in wanted and not labels)


def _matching_awards(awards: list[Award], award: str, winners: bool) -> list[Award]:
    return [
        a for a in awards
        if (award in ("", ANY_AWARD) or a.category == award) and (not winners or a.result == "winner")
    ]


def _award_matches(awards: list[Award], award: str, winners: bool) -> bool:
    return not (award or winners) or bool(_matching_awards(awards, award, winners))


def _usable_share(row) -> float | None:
    """The exact positive share, or None when the review summary cannot be used."""
    if ranking.unranked_reason(row["review_positive"], row["review_total"]) is not None:
        return None
    return row["review_positive"] / row["review_total"]


def _sort_rows(rows: list, sort: str, placements: dict[int, ranking.Placement]) -> list:
    """Order result rows. Games without usable review data always come last; ties fall back to A-Z.

    By rank, games in the four-year ranking come first in rank order, and every
    other game (Unranked, or outside the window in the All games view) follows A-Z.
    """
    if sort == "name":
        return list(rows)  # matching_games already returns library order
    if sort == "reviews":
        def key(row):
            total = row["review_total"] if _usable_share(row) is not None else None
            return (total is None, -(total or 0), row["sort_name"], row["appid"])
    elif sort == "positive":  # the raw Steam share
        def key(row):
            share = _usable_share(row)
            return (share is None, -(share or 0), -(row["review_total"] or 0), row["sort_name"], row["appid"])
    else:
        def key(row):
            rank = placements[row["appid"]].rank
            return (rank is None, rank or 0, row["sort_name"], row["appid"])
    return sorted(rows, key=key)


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
    return {
        "appid": appid,
        "name": row["name"] or row["curated_title"],
        **_placement_view(place),
        "release": _release_shown(row, place),
        "image_url": row["cover_image_url"] or row["header_image_url"],
        "labels": _verified_labels(curated, appid),
        "tags": tags[:CARD_TAG_COUNT],
        "summary": concise_description(row["short_description"] or ""),
        "reviews": _review_summary(row),
        "awards": [_award_view(award) for award in curated.awards_for(appid)],
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


def _details_view(row, tags: list[str], excerpts: dict, curated: CuratedData, place: ranking.Placement,
                  now: datetime) -> dict:
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


def dashboard_view(url: str) -> dict:
    """Decide how the configured report URL is shown on /dashboard.

    - secure_embed: a Power BI "Embed report > Website or portal" link. The report
      loads in a frame and each viewer signs in; Power BI checks their permission.
    - public_embed: a "Publish to web" link. Anyone with it can view, without signing in.
    - link: any other HTTPS URL (for example the report's own app.powerbi.com page),
      opened in a new tab, where Power BI asks the viewer to sign in.
    """
    if not url:
        return {"state": "not_connected"}
    parts = urlsplit(url)
    path = parts.path.rstrip("/").casefold()
    if parts.hostname == POWER_BI_HOST and path == "/reportembed":
        return {"state": "secure_embed", "url": url}
    if parts.hostname == POWER_BI_HOST and path == "/view":
        return {"state": "public_embed", "url": url}
    return {"state": "link", "url": url}


# ---------------------------------------------------------------------- app


def create_app(settings: Settings | None = None, *, today: Callable[[], date] = date.today) -> FastAPI:
    """Build the web app. Tests pass their own Settings and a fixed `today`; `serve` uses the environment.

    `today` gives the last day of the four-year ranking window: by default the
    calendar date of the computer that runs the site, read again on every request.
    """
    settings = settings or load_settings()
    store.initialize(settings.db_path).close()  # an empty catalog is valid: the page explains how to sync
    curated_cache = CuratedCache(settings.curated_dir)
    dashboard_url = settings.dashboard_url or ""
    parsed = urlsplit(dashboard_url)
    if dashboard_url and (parsed.scheme != "https" or not parsed.hostname):
        raise ValueError("GAME_LIBRARIAN_DASHBOARD_URL must be an absolute HTTPS URL")
    dashboard = dashboard_view(dashboard_url)

    app = FastAPI(title="Game Librarian", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(
        date=format_date, datetime=format_datetime, number=format_number, hours=format_hours, day=format_day
    )
    templates.env.globals["app_version"] = __version__  # cache-busts the stylesheet and script after an upgrade

    def load_details(appid: int) -> dict | None:
        """Everything the details view needs for one visible game, or None if it is not shown."""
        curated = curated_cache.get()
        conn = store.connect(settings.db_path)
        try:
            row = store.game_rows(conn, [appid]).get(appid)
            if row is None or not row["is_curated"] or row["details_updated_at"] is None:
                return None
            window = ranking.RankingWindow.ending_on(today())
            placements = ranking.place_catalog(store.matching_games(conn, [], []), curated, window)
            tags = store.tags_for(conn, [appid])[appid]
            excerpts = store.excerpts_for(conn, [appid])[appid]
        finally:
            conn.close()
        return _details_view(row, tags, excerpts, curated, placements[appid], datetime.now(timezone.utc))

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_page(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {"dashboard": dashboard, "active": "dashboard"})

    @app.get("/games/{appid}/panel", response_class=HTMLResponse)
    def game_panel(request: Request, appid: int):
        """The details as an HTML fragment for the in-page dialog."""
        game = load_details(appid)
        return templates.TemplateResponse(request, "panel.html", {"game": game}, status_code=200 if game else 404)

    @app.get("/games/{appid}", response_class=HTMLResponse)
    def game_page(request: Request, appid: int):
        """The details as a full page: a direct link, and the fallback when JavaScript is off."""
        query = CatalogQuery.from_params(request.query_params, _award_categories(curated_cache.get()))
        back_url = query.url(_page_number(request.query_params.get("page", "1"))) + f"#game-{appid}"
        game = load_details(appid)
        context = {"game": game, "back_url": back_url}
        return templates.TemplateResponse(request, "game.html", context, status_code=200 if game else 404)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        curated = curated_cache.get()
        categories = _award_categories(curated)
        query = CatalogQuery.from_params(request.query_params, categories)
        page_number = _page_number(request.query_params.get("page", "1"))
        words = normalize_for_search(query.q).split()
        window = ranking.RankingWindow.ending_on(today())

        conn = store.connect(settings.db_path)
        try:
            catalog = store.matching_games(conn, [], [])
            # Placed over the whole catalog, so search and filters never change a game's rank.
            placements = ranking.place_catalog(catalog, curated, window)
            searched = store.matching_games(conn, words, list(query.tags)) if (words or query.tags) else catalog
            labels_of = {row["appid"]: _verified_labels(curated, row["appid"]) for row in searched}
            label_ok = {appid: _label_matches(labels, query.labels) for appid, labels in labels_of.items()}
            award_ok = {
                row["appid"]: _award_matches(curated.awards_for(row["appid"]), query.award, query.winners)
                for row in searched
            }
            in_view = {appid: query.view == ALL_VIEW or placements[appid].in_window for appid in labels_of}
            matches = [row for row in searched if label_ok[row["appid"]] and award_ok[row["appid"]]]
            results = _sort_rows([row for row in matches if in_view[row["appid"]]], query.order, placements)
            total_pages = max(1, math.ceil(len(results) / PAGE_SIZE))
            if page_number > total_pages:
                return RedirectResponse(query.url(total_pages), status_code=303)
            start = (page_number - 1) * PAGE_SIZE
            page_appids = [row["appid"] for row in results[start : start + PAGE_SIZE]]
            rows = store.game_rows(conn, page_appids)
            tags_by_game = store.tags_for(conn, page_appids)
            tag_facets = store.tag_counts(conn, [row["appid"] for row in results], limit=SIDEBAR_TAG_LIMIT)
            counts = store.catalog_counts(conn)
            sync_status = _sync_status(conn)
        finally:
            conn.close()

        # Option counts follow the view and the other active filters, so each number says what choosing it would show.
        label_counts: Counter = Counter()
        award_counts: Counter = Counter()
        for appid, labels in labels_of.items():
            if not in_view[appid]:
                continue
            if award_ok[appid]:
                label_counts.update(labels or [UNCLASSIFIED])
            if label_ok[appid]:
                awards = _matching_awards(curated.awards_for(appid), ANY_AWARD, query.winners)
                award_counts.update({ANY_AWARD, *(award.category for award in awards)} if awards else ())
        facet_names = {facet["tag"] for facet in tag_facets}
        window_matches = sum(1 for row in matches if placements[row["appid"]].in_window)
        context = {
            "query": query,
            "view": VIEWS[query.view],
            # Each tab keeps the search and filters, and counts the games it would show with them.
            "view_links": [
                {"label": VIEWS[key].label, "url": replace(query, view=key).url(), "current": key == query.view,
                 "games": window_matches if key == TOP_VIEW else len(matches)}
                for key in (TOP_VIEW, ALL_VIEW)
            ],
            "window": window,
            "sort": SORTS[query.order],
            "sort_options": SORT_OPTIONS,
            "label_options": [
                {"name": name, "games": label_counts[name], "selected": name in query.labels} for name in LABEL_FILTERS
            ],
            "award_options": [{"value": ANY_AWARD, "label": "Any listed award", "games": award_counts[ANY_AWARD]}]
            + [{"value": category, "label": category, "games": award_counts[category]} for category in categories],
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
            "catalog_total": len(catalog),
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
