"""SQLite storage for the prepared catalog (the storage boundary of the app).

The sync command writes here; the web app only reads. Curated awards and
classifications are deliberately NOT stored here: they stay in the editable CSV
files, so a Steam sync can never overwrite them.

All SQL lives in this module. Serving the catalog from another store later (for
example a table exported from a warehouse) means reimplementing these functions.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .prepare import GameDetails, ReviewExcerpt, ReviewSummary, content_hash, normalize_for_search, sort_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    appid                  INTEGER PRIMARY KEY,  -- Steam app id: stable when a title changes
    curated_title          TEXT NOT NULL,        -- title from curated/games.csv, used until Steam answers
    is_curated             INTEGER NOT NULL DEFAULT 1,
    name                   TEXT,                 -- latest store name from Steam
    search_name            TEXT NOT NULL DEFAULT '',
    sort_name              TEXT NOT NULL DEFAULT '',
    app_type               TEXT,
    short_description      TEXT,
    about_blocks           TEXT,                 -- JSON list of {kind, text | items}
    header_image_url       TEXT,
    cover_image_url        TEXT,                 -- portrait library cover, when Steam has one
    preview_clip           TEXT,                 -- JSON {title, webm, mp4, poster}: Steam's trailer clip
    screenshot_url         TEXT,                 -- first all-ages store screenshot
    developers             TEXT,                 -- JSON lists from here to platforms
    publishers             TEXT,
    genres                 TEXT,
    modes                  TEXT,
    platforms              TEXT,
    release_date           TEXT,
    coming_soon            INTEGER,
    details_status         TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | unavailable | error
    details_error          TEXT,
    details_hash           TEXT,
    details_updated_at     TEXT,                 -- last successful fetch, UTC ISO 8601
    details_checked_at     TEXT,                 -- last attempt, successful or not
    details_last_modified  INTEGER,              -- Steam last_modified known at the last successful fetch
    steam_last_modified    INTEGER,              -- newest last_modified reported by catalog discovery
    reviews_status         TEXT NOT NULL DEFAULT 'pending',  -- pending | ok | error
    reviews_error          TEXT,
    reviews_updated_at     TEXT,
    reviews_checked_at     TEXT,
    review_total           INTEGER,
    review_positive        INTEGER,
    review_negative        INTEGER,
    review_score_desc      TEXT,
    reviews_day_range_used INTEGER
);
CREATE TABLE IF NOT EXISTS game_tags (
    appid INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    rank  INTEGER NOT NULL,                      -- 0 = the most voted tag
    tag   TEXT NOT NULL,
    PRIMARY KEY (appid, tag)
);
CREATE INDEX IF NOT EXISTS game_tags_by_tag ON game_tags (tag);
CREATE TABLE IF NOT EXISTS review_excerpts (
    appid             INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    polarity          TEXT NOT NULL CHECK (polarity IN ('positive', 'negative')),
    position          INTEGER NOT NULL,
    recommendation_id TEXT NOT NULL,
    text              TEXT NOT NULL,
    truncated         INTEGER NOT NULL,
    votes_up          INTEGER,
    playtime_minutes  INTEGER,
    created_at        TEXT,
    PRIMARY KEY (appid, polarity, position)
);
CREATE TABLE IF NOT EXISTS discovered_apps (
    appid               INTEGER PRIMARY KEY,
    name                TEXT,
    last_modified       INTEGER,
    price_change_number INTEGER,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                   -- running | success | partial | failed
    summary     TEXT                             -- JSON counts and the first errors
);
CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Games proposed for the catalog (see candidates.py). Nothing here is shown on the website until
-- a candidate is promoted into curated/promoted_games.csv and synced.
CREATE TABLE IF NOT EXISTS candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    appid               INTEGER UNIQUE,          -- Steam app id; NULL until a title-only candidate is matched
    igdb_id             INTEGER UNIQUE,          -- IGDB game id, when IGDB suggested it (provenance only)
    title               TEXT NOT NULL,           -- the name the first source gave
    sources             TEXT NOT NULL,           -- JSON list of {"source", "ref", "detail"}
    match_method        TEXT NOT NULL,           -- steam_app | curated_award | igdb_steam_id | title_only | manual
    suggested_appids    TEXT,                    -- JSON list of Steam app ids to check for a title-only candidate
    status              TEXT NOT NULL DEFAULT 'pending',  -- see candidates.STATUSES
    reason              TEXT,                    -- why the candidate has its status
    steam_name          TEXT,                    -- the facts from the last screening
    steam_type          INTEGER,
    visible             INTEGER,
    available           INTEGER,                 -- 1 when Steam sells it or offers it free (US store)
    review_count        INTEGER,
    percent_positive    INTEGER,
    released_on         TEXT,                    -- ISO date of the Steam release, when Steam gives one
    coming_soon         INTEGER,
    content_descriptors TEXT,                    -- JSON list of Steam content descriptor ids
    attempts            INTEGER NOT NULL DEFAULT 0,  -- failed screening attempts in a row
    last_error          TEXT,
    screened_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidates_by_status ON candidates (status);
CREATE TABLE IF NOT EXISTS candidate_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,                   -- add-discovery | add-awards | add-igdb | screen
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                   -- running | success | partial | failed
    summary     TEXT                             -- JSON counts and the first errors
);
"""

# Columns added after the first release. initialize() adds them to an existing database;
# CREATE TABLE IF NOT EXISTS alone never changes a table that already exists.
ADDED_GAME_COLUMNS = {"preview_clip": "TEXT", "screenshot_url": "TEXT"}

# Only games that are still in curated/games.csv and were fetched at least once are shown.
VISIBLE_GAMES = "g.is_curated = 1 AND g.details_updated_at IS NOT NULL"


@dataclass
class StoreExtras:
    """Tags, portrait cover, trailer clip and screenshot from IStoreBrowseService for one app."""

    tags: list[str]
    cover_url: str | None
    clip: dict | None = None
    screenshot_url: str | None = None


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the catalog database. Rows behave like dicts (`row["name"]`)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def initialize(db_path: Path) -> sqlite3.Connection:
    """Create the database file and tables if needed, then return an open connection.

    An existing catalog keeps all its rows; columns added in later versions are
    appended (empty) until the next sync fills them.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")  # page requests keep reading while a sync writes
    conn.executescript(SCHEMA)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    with conn:
        for column, column_type in ADDED_GAME_COLUMNS.items():
            if column not in existing:  # names come from the constant above, never from input
                conn.execute(f"ALTER TABLE games ADD COLUMN {column} {column_type}")
    return conn


# ---------------------------------------------------------------------- sync writes


def sync_curated_games(conn: sqlite3.Connection, games: list[tuple[int, str]]) -> int:
    """Make the games table follow curated/games.csv and return how many app ids are new.

    Games removed from the file are hidden (is_curated = 0) rather than deleted, so
    their data comes back if they are listed again.
    """
    known = {row["appid"] for row in conn.execute("SELECT appid FROM games")}
    with conn:
        conn.execute("UPDATE games SET is_curated = 0")
        for appid, title in games:
            conn.execute(
                """INSERT INTO games (appid, curated_title, search_name, sort_name, is_curated)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT (appid) DO UPDATE SET curated_title = excluded.curated_title, is_curated = 1""",
                (appid, title, normalize_for_search(title), sort_key(title)),
            )
    return len({appid for appid, _ in games} - known)


def game_rows(conn: sqlite3.Connection, appids: list[int]) -> dict[int, sqlite3.Row]:
    """Return the stored rows for `appids`, keyed by app id."""
    if not appids:
        return {}
    rows = conn.execute(f"SELECT * FROM games WHERE appid IN ({','.join('?' * len(appids))})", appids)
    return {row["appid"]: row for row in rows}


def save_details(
    conn: sqlite3.Connection,
    details: GameDetails,
    extras: StoreExtras | None,
    *,
    now: str,
    last_modified: int | None,
) -> str:
    """Store fresh details in one transaction. Return "new", "changed" or "unchanged".

    `extras` is None when the IStoreBrowseService request failed; the stored tags,
    cover, trailer clip and screenshot are kept.
    """
    row = conn.execute(
        """SELECT curated_title, details_hash, details_updated_at, cover_image_url, preview_clip, screenshot_url
           FROM games WHERE appid = ?""",
        (details.appid,),
    ).fetchone()
    if extras is None:
        tags = [r["tag"] for r in conn.execute("SELECT tag FROM game_tags WHERE appid = ? ORDER BY rank", (details.appid,))]
        cover_url, screenshot_url = row["cover_image_url"], row["screenshot_url"]
        clip = json.loads(row["preview_clip"]) if row["preview_clip"] else None
    else:
        tags, cover_url, clip, screenshot_url = extras.tags, extras.cover_url, extras.clip, extras.screenshot_url
    new_hash = content_hash(asdict(details), tags, cover_url, clip, screenshot_url)
    if row["details_updated_at"] is None:
        outcome = "new"
    else:
        outcome = "unchanged" if row["details_hash"] == new_hash else "changed"

    display_name = details.name or row["curated_title"]
    with conn:
        conn.execute(
            """UPDATE games SET
                 name = ?, search_name = ?, sort_name = ?, app_type = ?, short_description = ?,
                 about_blocks = ?, header_image_url = ?, cover_image_url = ?, preview_clip = ?,
                 screenshot_url = ?, developers = ?,
                 publishers = ?, genres = ?, modes = ?, platforms = ?, release_date = ?, coming_soon = ?,
                 details_status = 'ok', details_error = NULL, details_hash = ?,
                 details_updated_at = ?, details_checked_at = ?,
                 details_last_modified = COALESCE(?, details_last_modified)
               WHERE appid = ?""",
            (
                display_name,
                normalize_for_search(f"{display_name} {row['curated_title']}"),
                sort_key(display_name),
                details.app_type,
                details.short_description,
                json.dumps(details.about_blocks, ensure_ascii=False),
                details.header_image_url,
                cover_url,
                json.dumps(clip, ensure_ascii=False) if clip else None,
                screenshot_url,
                json.dumps(details.developers, ensure_ascii=False),
                json.dumps(details.publishers, ensure_ascii=False),
                json.dumps(details.genres, ensure_ascii=False),
                json.dumps(details.modes, ensure_ascii=False),
                json.dumps(details.platforms),
                details.release_date,
                int(details.coming_soon),
                new_hash,
                now,
                now,
                last_modified,
                details.appid,
            ),
        )
        if extras is not None:
            conn.execute("DELETE FROM game_tags WHERE appid = ?", (details.appid,))
            conn.executemany(
                "INSERT INTO game_tags (appid, rank, tag) VALUES (?, ?, ?)",
                [(details.appid, rank, tag) for rank, tag in enumerate(tags)],
            )
    return outcome


def record_details_problem(conn: sqlite3.Connection, appid: int, *, status: str, error: str, now: str) -> None:
    """Note a failed or empty details fetch. The last good data is left untouched."""
    with conn:
        conn.execute(
            "UPDATE games SET details_status = ?, details_error = ?, details_checked_at = ? WHERE appid = ?",
            (status, error, now, appid),
        )


def save_reviews(
    conn: sqlite3.Connection,
    appid: int,
    summary: ReviewSummary,
    positive: list[ReviewExcerpt],
    negative: list[ReviewExcerpt],
    *,
    day_range_used: int | None,
    now: str,
) -> None:
    """Replace the review summary and excerpts of one game in a single transaction."""
    with conn:
        conn.execute(
            """UPDATE games SET reviews_status = 'ok', reviews_error = NULL, reviews_updated_at = ?,
                 reviews_checked_at = ?, review_total = ?, review_positive = ?, review_negative = ?,
                 review_score_desc = ?, reviews_day_range_used = ?
               WHERE appid = ?""",
            (now, now, summary.total, summary.positive, summary.negative, summary.score_desc, day_range_used, appid),
        )
        conn.execute("DELETE FROM review_excerpts WHERE appid = ?", (appid,))
        for polarity, excerpts in (("positive", positive), ("negative", negative)):
            conn.executemany(
                """INSERT INTO review_excerpts (appid, polarity, position, recommendation_id, text,
                     truncated, votes_up, playtime_minutes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (appid, polarity, position, e.recommendation_id, e.text, int(e.truncated),
                     e.votes_up, e.playtime_minutes, e.created_at)
                    for position, e in enumerate(excerpts)
                ],
            )


def record_reviews_problem(conn: sqlite3.Connection, appid: int, *, error: str, now: str) -> None:
    """Note a failed review fetch. The last good summary and excerpts are kept."""
    with conn:
        conn.execute(
            "UPDATE games SET reviews_status = 'error', reviews_error = ?, reviews_checked_at = ? WHERE appid = ?",
            (error, now, appid),
        )


def upsert_discovered_apps(conn: sqlite3.Connection, apps: list[dict], *, now: str) -> tuple[int, int]:
    """Record one page of catalog discovery results. Return (new apps, changed apps)."""
    new = changed = 0
    with conn:
        for app in apps:
            appid = int(app["appid"])
            last_modified = app.get("last_modified")
            row = conn.execute("SELECT last_modified FROM discovered_apps WHERE appid = ?", (appid,)).fetchone()
            if row is None:
                new += 1
                conn.execute(
                    """INSERT INTO discovered_apps (appid, name, last_modified, price_change_number, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (appid, app.get("name"), last_modified, app.get("price_change_number"), now, now),
                )
            else:
                changed += int(row["last_modified"] != last_modified)
                conn.execute(
                    """UPDATE discovered_apps SET name = ?, last_modified = ?, price_change_number = ?, last_seen_at = ?
                       WHERE appid = ?""",
                    (app.get("name"), last_modified, app.get("price_change_number"), now, appid),
                )
    return new, changed


def copy_discovered_last_modified(conn: sqlite3.Connection) -> None:
    """Give curated games the newest last_modified seen by discovery (used to detect changes)."""
    with conn:
        conn.execute(
            """UPDATE games SET steam_last_modified =
                 (SELECT d.last_modified FROM discovered_apps d WHERE d.appid = games.appid)
               WHERE appid IN (SELECT appid FROM discovered_apps)"""
        )


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a small sync bookmark, such as the time of the last successful discovery."""
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Save a small sync bookmark."""
    with conn:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def start_sync_run(conn: sqlite3.Connection, *, now: str) -> int:
    """Record the start of a sync. A run left 'running' by a crashed process is marked failed."""
    abandoned = json.dumps({"errors": ["The run stopped before it finished (the process ended early)."]})
    with conn:
        conn.execute(
            "UPDATE sync_runs SET status = 'failed', finished_at = ?, summary = ? WHERE status = 'running'",
            (now, abandoned),
        )
        cursor = conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (now,))
    return cursor.lastrowid


def finish_sync_run(conn: sqlite3.Connection, run_id: int, *, status: str, summary: dict, now: str) -> None:
    """Record how a sync ended, with its counts and first errors."""
    with conn:
        conn.execute(
            "UPDATE sync_runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
            (status, now, json.dumps(summary, ensure_ascii=False), run_id),
        )


# ---------------------------------------------------------------------- web reads


def recent_sync_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """Return the latest sync runs, newest first."""
    return conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run_problem(run: sqlite3.Row) -> str | None:
    """The main problem recorded for a sync run: why it stopped early, or else its first error."""
    if not run["summary"]:
        return None
    summary = json.loads(run["summary"])
    errors = summary.get("errors") or []
    return summary.get("stopped_early") or (errors[0] if errors else None)


def last_successful_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the newest run that finished with status success or partial."""
    return conn.execute(
        "SELECT * FROM sync_runs WHERE status IN ('success', 'partial') ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _visible_filter(words: list[str], tags: list[str]) -> tuple[str, list]:
    """Build the WHERE clause for a name search plus tag filter (games must have every tag).

    Only fixed SQL fragments are joined here; user input always travels as parameters.
    """
    clauses, params = [VISIBLE_GAMES], []
    for word in words:
        clauses.append("g.search_name LIKE ?")
        params.append(f"%{word}%")  # words come from normalize_for_search, so they hold no % or _
    if tags:
        clauses.append(
            f"""g.appid IN (SELECT appid FROM game_tags WHERE tag IN ({','.join('?' * len(tags))})
                            GROUP BY appid HAVING COUNT(DISTINCT tag) = ?)"""
        )
        params.extend([*tags, len(tags)])
    return " AND ".join(clauses), params


def matching_games(conn: sqlite3.Connection, words: list[str], tags: list[str]) -> list[sqlite3.Row]:
    """Return the visible games that match a name search and tag filter, in library order.

    Each row holds only what filtering, sorting and ranking need: appid, sort_name,
    review_positive, review_total, release_date, coming_soon and modes (JSON). With no
    words and no tags, this is the whole visible catalog, which the ranking is computed over.
    """
    where, params = _visible_filter(words, tags)
    return conn.execute(
        f"""SELECT g.appid, g.sort_name, g.review_positive, g.review_total, g.release_date, g.coming_soon, g.modes
            FROM games g WHERE {where} ORDER BY g.sort_name, g.appid""",
        params,
    ).fetchall()


def tag_counts(conn: sqlite3.Connection, appids: list[int], *, limit: int) -> list[sqlite3.Row]:
    """Count tags among `appids` (the current results), most common first."""
    if not appids:
        return []
    return conn.execute(
        f"""SELECT tag, COUNT(*) AS games FROM game_tags WHERE appid IN ({','.join('?' * len(appids))})
            GROUP BY tag ORDER BY games DESC, tag LIMIT ?""",
        [*appids, limit],
    ).fetchall()


def tags_for(conn: sqlite3.Connection, appids: list[int]) -> dict[int, list[str]]:
    """Return each game's tags, most voted first."""
    tags: dict[int, list[str]] = defaultdict(list)
    if not appids:
        return tags
    rows = conn.execute(
        f"SELECT appid, tag FROM game_tags WHERE appid IN ({','.join('?' * len(appids))}) ORDER BY appid, rank",
        appids,
    )
    for row in rows:
        tags[row["appid"]].append(row["tag"])
    return tags


def top_tags_of_visible_games(conn: sqlite3.Connection, per_game: int) -> dict[int, list[str]]:
    """Every visible game's `per_game` most-voted tags, most voted first (the input of related.py)."""
    tags: dict[int, list[str]] = defaultdict(list)
    rows = conn.execute(
        f"""SELECT t.appid, t.tag FROM game_tags t JOIN games g ON g.appid = t.appid
            WHERE {VISIBLE_GAMES} AND t.rank < ? ORDER BY t.appid, t.rank""",
        (per_game,),
    )
    for row in rows:
        tags[row["appid"]].append(row["tag"])
    return dict(tags)


def excerpts_for(conn: sqlite3.Connection, appids: list[int]) -> dict[int, dict[str, list[sqlite3.Row]]]:
    """Return review excerpts as {appid: {"positive": [...], "negative": [...]}}."""
    excerpts: dict[int, dict[str, list[sqlite3.Row]]] = defaultdict(lambda: {"positive": [], "negative": []})
    if not appids:
        return excerpts
    rows = conn.execute(
        f"""SELECT * FROM review_excerpts WHERE appid IN ({','.join('?' * len(appids))})
            ORDER BY appid, polarity, position""",
        appids,
    )
    for row in rows:
        excerpts[row["appid"]][row["polarity"]].append(row)
    return excerpts


def catalog_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts for the status panel: curated games, games ready to show, and discovered apps."""
    row = conn.execute(
        f"""SELECT
              (SELECT COUNT(*) FROM games WHERE is_curated = 1) AS curated,
              (SELECT COUNT(*) FROM games g WHERE {VISIBLE_GAMES}) AS ready,
              (SELECT COUNT(*) FROM discovered_apps) AS discovered"""
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------- candidate pipeline (candidates.py)


@dataclass
class CandidateFacts:
    """What one screening learned about a candidate's Steam app (from IStoreBrowseService/GetItems)."""

    steam_name: str | None
    steam_type: int | None
    visible: bool
    available: bool
    review_count: int
    percent_positive: int
    released_on: str | None  # ISO date
    coming_soon: bool
    content_descriptors: list[int]


def candidate(conn: sqlite3.Connection, *, appid: int | None = None, candidate_id: int | None = None,
              igdb_id: int | None = None) -> sqlite3.Row | None:
    """One candidate, found by Steam app id, candidate id or IGDB id."""
    for column, value in (("appid", appid), ("id", candidate_id), ("igdb_id", igdb_id)):
        if value is not None:  # column names come from the fixed tuple above, never from input
            return conn.execute(f"SELECT * FROM candidates WHERE {column} = ?", (value,)).fetchone()
    return None


def add_candidate(conn: sqlite3.Connection, *, title: str, source: dict, match_method: str, now: str,
                  appid: int | None = None, igdb_id: int | None = None, status: str = "pending",
                  reason: str | None = None, suggested_appids: list[int] | None = None) -> str:
    """Add a candidate, or record one more source for an existing one. Return "new" or "merged".

    A candidate is the same when it has the same Steam app id or the same IGDB id, so a game found
    by several sources appears once, with every source listed.
    """
    existing = None
    if appid is not None:
        existing = candidate(conn, appid=appid)
    if existing is None and igdb_id is not None:
        existing = candidate(conn, igdb_id=igdb_id)
    with conn:
        if existing is not None:
            sources = json.loads(existing["sources"])
            if source not in sources:
                sources.append(source)
            conn.execute(
                "UPDATE candidates SET sources = ?, igdb_id = COALESCE(igdb_id, ?), updated_at = ? WHERE id = ?",
                (json.dumps(sources, ensure_ascii=False), igdb_id, now, existing["id"]),
            )
            return "merged"
        conn.execute(
            """INSERT INTO candidates (appid, igdb_id, title, sources, match_method, suggested_appids, status,
                 reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (appid, igdb_id, title, json.dumps([source], ensure_ascii=False), match_method,
             json.dumps(suggested_appids) if suggested_appids is not None else None, status, reason, now, now),
        )
    return "new"


def discovered_apps_to_add(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Discovery-index apps that are neither candidates nor catalog games yet, lowest app id first.

    The order says nothing about quality; it only makes the work resumable: each run continues
    where the last one stopped.
    """
    return conn.execute(
        """SELECT appid, name FROM discovered_apps
           WHERE appid NOT IN (SELECT appid FROM candidates WHERE appid IS NOT NULL)
             AND appid NOT IN (SELECT appid FROM games WHERE is_curated = 1)
           ORDER BY appid LIMIT ?""",
        (limit,),
    ).fetchall()


def discovered_app_names(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every named app in the discovery index (appid, name), for title-only matching."""
    return conn.execute("SELECT appid, name FROM discovered_apps WHERE name IS NOT NULL AND name != '' "
                        "ORDER BY appid").fetchall()


def candidates_to_screen(conn: sqlite3.Connection, limit: int, *, max_attempts: int,
                         recheck_before: str | None = None) -> list[sqlite3.Row]:
    """Candidates with a Steam app id that are due for screening, oldest first.

    Due: never screened (pending), or the last attempt failed fewer than `max_attempts` times in a row.
    With `recheck_before`, eligible and ineligible candidates screened at or before that time are due
    again (for example to see whether a game has collected enough reviews since).
    """
    recheck = "OR (status IN ('eligible', 'ineligible') AND screened_at <= :before)" if recheck_before else ""
    return conn.execute(
        f"""SELECT * FROM candidates
            WHERE appid IS NOT NULL
              AND (status = 'pending' OR (status = 'error' AND attempts < :attempts) {recheck})
            ORDER BY id LIMIT :limit""",
        {"attempts": max_attempts, "before": recheck_before, "limit": limit},
    ).fetchall()


def save_screening(conn: sqlite3.Connection, candidate_id: int, facts: CandidateFacts | None, *,
                   status: str, reason: str, now: str) -> None:
    """Store a screening result. `facts` is None when Steam returned no store data for the app."""
    if facts is None:
        fact_values = (None,) * 9
    else:
        fact_values = (facts.steam_name, facts.steam_type, int(facts.visible), int(facts.available),
                       facts.review_count, facts.percent_positive, facts.released_on, int(facts.coming_soon),
                       json.dumps(facts.content_descriptors))
    with conn:
        conn.execute(
            """UPDATE candidates SET status = ?, reason = ?, steam_name = ?, steam_type = ?, visible = ?,
                 available = ?, review_count = ?, percent_positive = ?, released_on = ?, coming_soon = ?,
                 content_descriptors = ?, attempts = 0, last_error = NULL, screened_at = ?, updated_at = ?
               WHERE id = ?""",
            (status, reason, *fact_values, now, now, candidate_id),
        )


def record_screening_error(conn: sqlite3.Connection, candidate_id: int, *, error: str, now: str) -> None:
    """Note a failed screening request. The candidate is tried again by a later run."""
    with conn:
        conn.execute(
            """UPDATE candidates SET status = 'error', attempts = attempts + 1, last_error = ?, updated_at = ?
               WHERE id = ?""",
            (error, now, candidate_id),
        )


def set_candidate_status(conn: sqlite3.Connection, candidate_id: int, *, status: str, reason: str | None,
                         now: str) -> None:
    """Record a reviewer's decision (promoted, rejected) or a reset (pending)."""
    with conn:
        conn.execute("UPDATE candidates SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                     (status, reason, now, candidate_id))


def match_candidate(conn: sqlite3.Connection, candidate_id: int, appid: int, *, now: str) -> None:
    """Give a title-only candidate the Steam app a reviewer confirmed; it is screened on the next run."""
    with conn:
        conn.execute(
            """UPDATE candidates SET appid = ?, match_method = 'manual', status = 'pending',
                 reason = 'Steam app confirmed by a reviewer; waiting for screening.', updated_at = ?
               WHERE id = ?""",
            (appid, now, candidate_id),
        )


def list_candidates(conn: sqlite3.Connection, *, status: str | None = None, source: str | None = None,
                    limit: int = 25) -> list[sqlite3.Row]:
    """Candidates for review: the most Steam reviews first, then the oldest."""
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("EXISTS (SELECT 1 FROM json_each(candidates.sources) s WHERE json_extract(s.value, '$.source') = ?)")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM candidates {where} ORDER BY COALESCE(review_count, -1) DESC, id LIMIT ?",
        [*params, limit],
    ).fetchall()


def candidate_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many candidates have each status."""
    return {row["status"]: row["n"] for row in conn.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")}


def start_candidate_run(conn: sqlite3.Connection, command: str, *, now: str) -> int:
    """Record the start of a candidates command. A run left 'running' by a crashed process is marked failed."""
    abandoned = json.dumps({"errors": ["The run stopped before it finished (the process ended early)."]})
    with conn:
        conn.execute(
            "UPDATE candidate_runs SET status = 'failed', finished_at = ?, summary = ? WHERE status = 'running'",
            (now, abandoned),
        )
        cursor = conn.execute("INSERT INTO candidate_runs (command, started_at, status) VALUES (?, ?, 'running')",
                              (command, now))
    return cursor.lastrowid


def finish_candidate_run(conn: sqlite3.Connection, run_id: int, *, status: str, summary: dict, now: str) -> None:
    with conn:
        conn.execute("UPDATE candidate_runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                     (status, now, json.dumps(summary, ensure_ascii=False), run_id))


def recent_candidate_runs(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM candidate_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
