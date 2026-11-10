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

    Each row holds only what sorting and ranking need: appid, sort_name,
    review_positive, review_total, release_date and coming_soon. With no words
    and no tags, this is the whole visible catalog, which the ranking is computed over.
    """
    where, params = _visible_filter(words, tags)
    return conn.execute(
        f"""SELECT g.appid, g.sort_name, g.review_positive, g.review_total, g.release_date, g.coming_soon
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
