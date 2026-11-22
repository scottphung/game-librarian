"""Storage for the prepared catalog (the storage boundary of the app): a local SQLite file, or PostgreSQL.

The sync command writes here; the web app reads, and writes only its Twitch caches and the signed-in visitors'
accounts and wishlists. Curated awards and classifications are deliberately NOT stored here: they stay in the
editable CSV files, so a Steam sync can never overwrite them.

All SQL lives in this module, written once in SQL that both SQLite and PostgreSQL accept; database.py opens the
connections and hides the remaining differences (placeholders, rows, transactions). The local SQLite file is created
and upgraded on first use. A PostgreSQL database is created and upgraded only by `init-postgres`, run with the
owner's role: the website and the sync use a role that may read and write rows but never change tables, so
initialize() only checks that every table and column is there.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache

from .database import (AnyRow as Row, Connection, PostgresDatabase, Target, connect as open_connection,
                       postgres_statements, postgres_type, quoted_name)
from .prepare import (GameDetails, PriceFacts, ReviewExcerpt, ReviewSummary, content_hash, normalize_for_search,
                      sort_key)

# "Most watched among tracked Twitch categories" by TwitchTracker (twitchtracker.py): the game and series categories
# chosen from Twitch's newest top list and from curated/twitch_categories.csv, each with TwitchTracker's newest
# figures. A newer answer replaces the figures and a failed check keeps them with their own time; no history is kept,
# so no change between two answers is ever shown. TwitchTracker publishes no terms that allow sharing its figures: the
# site shows them only to a browser on the computer that runs it. One statement: _rebuild_tracker_table runs it alone.
TRACKER_TABLE = """
CREATE TABLE IF NOT EXISTS twitchtracker_summaries (
    category_id   TEXT PRIMARY KEY,              -- the Twitch category id TwitchTracker is asked about
    category_name TEXT NOT NULL,                 -- Twitch's name for it (a reviewed title outside Twitch's top list)
    igdb_id       INTEGER,                       -- the game it stands for (twitch.resolve_category); NULL: a series
    match_source  TEXT NOT NULL,                 -- reviewer | twitch+igdb | twitch | igdb
    selected_at   TEXT,                          -- when Twitch gave the top list it was chosen from, UTC ISO 8601;
                                                 -- NULL: a reviewed category chosen outside that list
    rank          INTEGER,                       -- TwitchTracker's rank among all categories, as returned; NULL: none
    avg_viewers   INTEGER,
    avg_channels  INTEGER,
    hours_watched INTEGER,
    retrieved_at  TEXT,                          -- the start of the run whose answer these are; NULL before any
    status        TEXT NOT NULL,                 -- unchecked | ok | no_summary | invalid | error (the newest attempt)
    checked_at    TEXT,                          -- the newest attempt
    error         TEXT                           -- what went wrong at the newest attempt, if it failed
);
"""

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
-- The latest Steam price of each catalog game in one store region. A new check replaces the row;
-- a failed check leaves it, so its checked_at shows how old it is (pricing.py decides what is fresh).
CREATE TABLE IF NOT EXISTS prices (
    appid            INTEGER PRIMARY KEY REFERENCES games(appid) ON DELETE CASCADE,
    country_code     TEXT NOT NULL,              -- the Steam store region, such as "US"
    currency         TEXT,                       -- ISO 4217 code Steam reports for that region, when known
    status           TEXT NOT NULL,              -- priced | free | not_sold | unavailable (prepare.PRICE_STATUSES)
    final_cents      INTEGER,
    original_cents   INTEGER,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    final_text       TEXT,                       -- Steam's own formatting, such as "$7.49"
    original_text    TEXT,
    discount_ends_at TEXT,                       -- UTC ISO 8601, when Steam gives an end date
    discount_hidden  INTEGER NOT NULL DEFAULT 0, -- 1: Steam asks not to present the price as a discount
    purchase_option  TEXT,
    checked_at       TEXT NOT NULL               -- when Steam reported these values, UTC ISO 8601
);
-- Dated snapshots: one row per game and capture time, never updated or deleted, so a change over
-- time can only be measured between captures that exist. A snapshot is one moment, not a trend.
CREATE TABLE IF NOT EXISTS player_snapshots (
    appid       INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,                   -- when Steam reported the count, UTC ISO 8601
    players     INTEGER,                         -- people in game on Steam; NULL when Steam reported no count
    PRIMARY KEY (appid, captured_at)
);
CREATE TABLE IF NOT EXISTS review_snapshots (
    appid       INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    total       INTEGER NOT NULL,                -- Steam's review summary: all languages, Steam purchasers
    positive    INTEGER NOT NULL,
    negative    INTEGER NOT NULL,
    PRIMARY KEY (appid, captured_at)
);
-- Refreshes of other sources than Steam (upcoming = IGDB, critics = Wikipedia, sales_estimates = Gamalytic,
-- most_watched = TwitchTracker), one row per run.
CREATE TABLE IF NOT EXISTS source_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,                   -- upcoming | critics | sales_estimates | most_watched
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,                   -- running | success | partial | failed
    summary     TEXT                             -- JSON counts and the first errors
);
-- IGDB game records (upcoming.py). A record is never deleted, so an igdb-<id> key keeps working after
-- the game leaves the upcoming lists or gets released. JSON columns hold what IGDB returned, as parsed.
CREATE TABLE IF NOT EXISTS igdb_games (
    igdb_id             INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    slug                TEXT,
    igdb_url            TEXT,
    summary             TEXT,
    game_type           INTEGER,
    game_status         INTEGER,
    first_release_date  INTEGER,                 -- IGDB's own timestamp; used only when no date can be read
    cover_image_id      TEXT,
    platforms           TEXT,                    -- JSON list of platform names
    release_dates       TEXT,                    -- JSON list of upcoming.ReleaseDate fields
    release_summary     TEXT,                    -- JSON {platform: {label}}: earliest full release per platform
    publishers          TEXT,
    developers          TEXT,
    websites            TEXT,                    -- JSON list of {kind, url}
    steam_appids        TEXT,                    -- JSON list of the Steam app ids IGDB lists
    steam_appid         INTEGER,                 -- the verified Steam app, else NULL
    steam_match         TEXT NOT NULL DEFAULT 'unchecked',  -- verified | needs_review | none | rejected | unchecked
    steam_match_reason  TEXT,
    steam_checked_at    TEXT,
    first_seen_at       TEXT NOT NULL,
    checked_at          TEXT NOT NULL            -- the last refresh that got this record from IGDB
);
-- A platform's full-release date as IGDB stated it before and after one refresh.
CREATE TABLE IF NOT EXISTS release_changes (
    igdb_id     INTEGER NOT NULL REFERENCES igdb_games(igdb_id) ON DELETE CASCADE,
    platform    TEXT NOT NULL,
    before      TEXT,                            -- NULL: no date for this platform before
    after       TEXT,                            -- NULL: no date for this platform now
    kind        TEXT NOT NULL,                   -- upcoming.DateChange.kind
    noticed_at  TEXT NOT NULL,
    PRIMARY KEY (igdb_id, platform, noticed_at)
);
-- Dated PopScore snapshots of the upcoming games in each list, never updated: position changes can
-- only be measured between snapshots that exist.
CREATE TABLE IF NOT EXISTS popularity_snapshots (
    signal         TEXT NOT NULL,                -- upcoming.Signal.key
    igdb_id        INTEGER NOT NULL,
    captured_at    TEXT NOT NULL,
    position       INTEGER NOT NULL,             -- place in IGDB's order of this signal, 1 = highest value
    value          REAL NOT NULL,                -- IGDB's value (a relative share or a position code, not a count)
    calculated_at  TEXT,                         -- when IGDB calculated the value
    PRIMARY KEY (signal, igdb_id, captured_at)
);
-- The IGDB game of each catalog app: linked when exactly one IGDB game lists the Steam app.
CREATE TABLE IF NOT EXISTS steam_igdb_links (
    appid       INTEGER PRIMARY KEY,
    igdb_id     INTEGER,
    status      TEXT NOT NULL,                   -- linked | none | ambiguous
    checked_at  TEXT NOT NULL
);
-- "Watch it live": the last Twitch answer per game, deleted after 24 hours (Twitch's caching limit).
CREATE TABLE IF NOT EXISTS twitch_live (
    game_key      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,                 -- ok | no_category | ambiguous | error | rate_limited
    category_id   TEXT,
    category_name TEXT,
    streams       TEXT,                          -- JSON list of twitch.Stream fields
    fetched_at    TEXT,                          -- the last successful answer
    checked_at    TEXT NOT NULL,                 -- the last attempt
    retry_at      TEXT,                          -- after a failure: do not ask again before this
    error         TEXT
);
-- "Most watched on Twitch": Twitch's newest Get Top Games answer, replaced at each check and deleted after
-- 24 hours (Twitch's caching limit). No history is kept: trends over time would need an agreement with Twitch.
CREATE TABLE IF NOT EXISTS twitch_top_games (
    position    INTEGER PRIMARY KEY,             -- place in Twitch's order of all categories, 1 = most viewers
    category_id TEXT NOT NULL,
    name        TEXT NOT NULL,                   -- Twitch's category name
    igdb_id     INTEGER,                         -- the IGDB game Twitch links to the category, else NULL
    checked_at  TEXT NOT NULL,                   -- when Twitch gave this order, UTC ISO 8601
    igdb_links  TEXT                             -- JSON list of the IGDB games whose IGDB record lists the category
);
""" + TRACKER_TABLE + """
-- "What the critics said": the Wikipedia article chosen for a game, and the scores read from it.
CREATE TABLE IF NOT EXISTS critic_articles (
    game_key        TEXT PRIMARY KEY,
    status          TEXT NOT NULL,               -- verified | needs_review | not_found | rejected | error
    reason          TEXT,
    wikidata_ids    TEXT,                        -- JSON list of the Wikidata items found by identifier
    article_title   TEXT,
    article_section TEXT,                        -- the section a redirect points to, if any
    revision_id     INTEGER,
    revision_at     TEXT,
    table_status    TEXT,                        -- ok | no_table | unsupported | not_read
    checked_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS critic_scores (
    game_key     TEXT NOT NULL REFERENCES critic_articles(game_key) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    table_title  TEXT,
    outlet_code  TEXT,
    outlet       TEXT NOT NULL,
    kind         TEXT NOT NULL,                  -- aggregate | critic
    score        TEXT,                           -- the original score as written, such as "9/10" or "A-"
    label        TEXT,                           -- platform or edition as written in the table
    review_url   TEXT,
    status       TEXT NOT NULL,                  -- ok | needs_review
    reason       TEXT,
    PRIMARY KEY (game_key, position)
);
-- Estimated Steam copies sold (gamalytic.py): one row per game and collection, never updated, so an older estimate
-- stays next to a newer one. Gamalytic revises its model, so the difference between two collections is not a number
-- of copies sold in between. Gamalytic's terms forbid sharing its data: the site shows these rows only to a browser
-- on the computer that runs it, and the Power BI export leaves them out.
CREATE TABLE IF NOT EXISTS sales_estimates (
    appid            INTEGER NOT NULL REFERENCES games(appid) ON DELETE CASCADE,
    collected_at     TEXT NOT NULL,              -- when this site asked, UTC ISO 8601
    source           TEXT NOT NULL,              -- who made the estimate: "Gamalytic"
    copies_sold      INTEGER,                    -- the estimate; NULL when the source gave none (never stored as 0)
    source_cached_at TEXT,                       -- the answer's own time (Gamalytic's cacheTimestamp), UTC ISO 8601
    PRIMARY KEY (appid, collected_at)
);
-- Signed-in visitors of the hosted site (accounts.py): one row per sign-in provider and person, made at their first
-- visit. The provider's own id for the person identifies them; the app never sees a password or a provider token.
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,                 -- the sign-in provider, such as "github" (X-MS-CLIENT-PRINCIPAL-IDP)
    subject       TEXT NOT NULL,                 -- the provider's id for the person (X-MS-CLIENT-PRINCIPAL-ID)
    display_name  TEXT NOT NULL,                 -- what the provider calls them, as of their latest visit
    created_at    TEXT NOT NULL,
    UNIQUE (provider, subject)
);
-- Each account's private wishlist: only that account's requests read or change its rows.
CREATE TABLE IF NOT EXISTS wishlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT, -- the order of saving: the wishlist lists the oldest first
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    game_key    TEXT NOT NULL,                     -- a game key (identity.py): a Steam app id or igdb-<id>
    name        TEXT NOT NULL,                     -- the game's name when it was saved
    added_at    TEXT NOT NULL,
    UNIQUE (account_id, game_key)
);
"""

# Tables whose rows belong to the hosted site's visitors, not to the catalog: migrate-to-postgres never copies,
# replaces or deletes them.
ACCOUNT_TABLES = ("accounts", "wishlist_items")

# Columns added to a table after it was first created, per table. initialize() adds them to an existing database;
# CREATE TABLE IF NOT EXISTS alone never changes a table that already exists.
ADDED_COLUMNS = {
    "games": {"preview_clip": "TEXT", "screenshot_url": "TEXT"},
    "twitch_top_games": {"igdb_links": "TEXT"},  # 0.18
}

# Review totals saved before review snapshots existed were real observations at reviews_updated_at,
# so they seed the review history. ON CONFLICT DO NOTHING makes this safe to run on every start.
SEED_REVIEW_SNAPSHOTS = """
INSERT INTO review_snapshots (appid, captured_at, total, positive, negative)
SELECT appid, reviews_updated_at, review_total, review_positive, COALESCE(review_negative, review_total - review_positive)
FROM games
WHERE reviews_updated_at IS NOT NULL AND review_total > 0 AND review_positive BETWEEN 0 AND review_total
ON CONFLICT DO NOTHING
"""


def _upsert(table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> str:
    """An INSERT that replaces the row with the same key, in SQL that SQLite and PostgreSQL both accept.

    It stands for SQLite's INSERT OR REPLACE: every column is given, so the stored row ends up the same, and none of
    these tables has another unique constraint. Names come from the constants below, never from input.
    """
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in key)
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}")


UPSERT_REVIEW_SNAPSHOT = _upsert("review_snapshots", ("appid", "captured_at", "total", "positive", "negative"),
                                 ("appid", "captured_at"))
UPSERT_PRICE = _upsert("prices", ("appid", "country_code", "currency", "status", "final_cents", "original_cents",
                                  "discount_percent", "final_text", "original_text", "discount_ends_at",
                                  "discount_hidden", "purchase_option", "checked_at"), ("appid",))
UPSERT_PLAYER_SNAPSHOT = _upsert("player_snapshots", ("appid", "captured_at", "players"), ("appid", "captured_at"))
UPSERT_RELEASE_CHANGE = _upsert("release_changes", ("igdb_id", "platform", "before", "after", "kind", "noticed_at"),
                                ("igdb_id", "platform", "noticed_at"))
UPSERT_POPULARITY = _upsert("popularity_snapshots", ("signal", "igdb_id", "captured_at", "position", "value",
                                                     "calculated_at"), ("signal", "igdb_id", "captured_at"))
UPSERT_STEAM_IGDB_LINK = _upsert("steam_igdb_links", ("appid", "igdb_id", "status", "checked_at"), ("appid",))
UPSERT_TWITCH_LIVE = _upsert("twitch_live", ("game_key", "status", "category_id", "category_name", "streams",
                                             "fetched_at", "checked_at", "retry_at", "error"), ("game_key",))
UPSERT_SALES_ESTIMATE = _upsert("sales_estimates", ("appid", "collected_at", "source", "copies_sold",
                                                    "source_cached_at"), ("appid", "collected_at"))

# Only games that are still in curated/games.csv and were fetched at least once are shown.
VISIBLE_GAMES = "g.is_curated = 1 AND g.details_updated_at IS NOT NULL"


@dataclass
class StoreExtras:
    """Tags, portrait cover, trailer clip and screenshot from IStoreBrowseService for one app."""

    tags: list[str]
    cover_url: str | None
    clip: dict | None = None
    screenshot_url: str | None = None


class SchemaError(RuntimeError):
    """The PostgreSQL database lacks tables or columns that this version uses (init-postgres has not run for it)."""


def connect(target: Target, *, pooled: bool = False) -> Connection:
    """Open the catalog: the SQLite file at a path, or the PostgreSQL database (Settings.database gives either).

    Rows behave like dicts (`row["name"]`). `pooled` reuses PostgreSQL connections; the website asks for it.
    """
    return open_connection(target, pooled=pooled)


def initialize(target: Target, *, pooled: bool = False) -> Connection:
    """Make sure the catalog's tables exist, then return an open connection.

    SQLite: create the file and the tables if needed. An existing catalog keeps all its rows; columns added in later
    versions are appended (empty) until the next sync fills them.
    PostgreSQL: only check the tables and columns (SchemaError if any is missing), because the roles of the website
    and the sync cannot change tables; init-postgres creates and upgrades them (create_postgres_schema).
    """
    if isinstance(target, PostgresDatabase):
        conn = connect(target, pooled=pooled)
        try:
            missing = missing_postgres_schema(conn)
        except BaseException:
            conn.close()
            raise
        if missing:
            conn.close()
            shown = ", ".join(missing[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
            raise SchemaError(f"The PostgreSQL database lacks {len(missing)} table(s) or column(s) this version uses "
                              f"({shown}). Run `python -m game_librarian init-postgres` with the database owner's "
                              "role (see DEPLOYMENT.md); the website and the sync never change tables.")
        return conn
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    conn.execute("PRAGMA journal_mode = WAL")  # page requests keep reading while a sync writes
    conn.executescript(SCHEMA)
    _rebuild_tracker_table(conn)
    with conn:
        for table, columns in ADDED_COLUMNS.items():  # names come from the constant above, never from input
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, column_type in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        conn.execute(SEED_REVIEW_SNAPSHOTS)
    return conn


@lru_cache(maxsize=1)
def schema_columns() -> dict[str, tuple[str, ...]]:
    """Every table of SCHEMA with its column names in order, read from an empty in-memory SQLite copy of it."""
    memory = sqlite3.connect(":memory:")
    try:
        memory.executescript(SCHEMA)
        tables = [row[0] for row in memory.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
        return {table: tuple(column[1] for column in memory.execute(f"PRAGMA table_info({table})"))
                for table in tables}
    finally:
        memory.close()


def missing_postgres_schema(conn: Connection) -> list[str]:
    """The tables ("games") and columns ("games.name") of SCHEMA that the PostgreSQL database lacks.

    information_schema lists only what the connected role may use, so a table it has no rights on counts as missing:
    init-postgres --grant-to grants them.
    """
    found: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema()"):
        found[row["table_name"]].add(row["column_name"])
    missing = []
    for table, columns in schema_columns().items():
        if table not in found:
            missing.append(table)
        else:
            missing.extend(f"{table}.{column}" for column in columns if column not in found[table])
    return missing


def create_postgres_schema(conn: Connection, *, grant_to: tuple[str, ...] = ()) -> str:
    """Create or upgrade the PostgreSQL tables (init-postgres), in one transaction, and return the schema's name.

    Run with a role that may create tables: the database owner, or the server administrator. Existing tables and rows
    are kept; columns added in later versions are added. Each role in `grant_to` (the website's and the sync's) gets
    the rights to read and write rows, and to use the id sequences, of every table, but not to change or drop tables.
    """
    with conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        if schema is None:
            raise SchemaError("The connection has no schema to create the tables in (check the role's search_path).")
        for statement in postgres_statements(SCHEMA):
            conn.execute(statement)
        for table, columns in ADDED_COLUMNS.items():  # names come from the constant above, never from input
            for column, column_type in columns.items():
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {postgres_type(column_type)}")
        conn.execute(SEED_REVIEW_SNAPSHOTS)
        for role in grant_to:
            name, where = quoted_name(role), quoted_name(schema)
            conn.execute(f"GRANT USAGE ON SCHEMA {where} TO {name}")
            conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {where} TO {name}")
            conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {where} TO {name}")
    return schema


def _rebuild_tracker_table(conn: Connection) -> None:
    """Upgrade version 0.18's twitchtracker_summaries, whose igdb_id and selected_at were NOT NULL, keeping every row.

    Since 0.19 a series category has no igdb_id, and a reviewed category outside Twitch's top list no selected_at.
    SQLite cannot drop a NOT NULL constraint, so the table is rebuilt from TRACKER_TABLE in one transaction: if any
    step fails, the old table stays as it was.
    """
    columns = conn.execute("PRAGMA table_info(twitchtracker_summaries)").fetchall()
    if not any(row["notnull"] and row["name"] in ("igdb_id", "selected_at") for row in columns):
        return
    names = ", ".join(row["name"] for row in columns)  # the table's own column names, never input
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE twitchtracker_summaries RENAME TO twitchtracker_summaries_0_18")
        conn.execute(TRACKER_TABLE)
        conn.execute(f"INSERT INTO twitchtracker_summaries ({names}) SELECT {names} FROM twitchtracker_summaries_0_18")
        conn.execute("DROP TABLE twitchtracker_summaries_0_18")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


# ---------------------------------------------------------------------- sync writes


def sync_curated_games(conn: Connection, games: list[tuple[int, str]]) -> int:
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


def game_rows(conn: Connection, appids: list[int]) -> dict[int, Row]:
    """Return the stored rows for `appids`, keyed by app id."""
    if not appids:
        return {}
    rows = conn.execute(f"SELECT * FROM games WHERE appid IN ({','.join('?' * len(appids))})", appids)
    return {row["appid"]: row for row in rows}


def save_details(
    conn: Connection,
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


def record_details_problem(conn: Connection, appid: int, *, status: str, error: str, now: str) -> None:
    """Note a failed or empty details fetch. The last good data is left untouched."""
    with conn:
        conn.execute(
            "UPDATE games SET details_status = ?, details_error = ?, details_checked_at = ? WHERE appid = ?",
            (status, error, now, appid),
        )


def save_reviews(
    conn: Connection,
    appid: int,
    summary: ReviewSummary,
    positive: list[ReviewExcerpt],
    negative: list[ReviewExcerpt],
    *,
    day_range_used: int | None,
    now: str,
) -> None:
    """Replace the review summary and excerpts of one game in a single transaction.

    A usable summary (some reviews, consistent counts) is also kept as a dated review snapshot.
    """
    with conn:
        conn.execute(
            """UPDATE games SET reviews_status = 'ok', reviews_error = NULL, reviews_updated_at = ?,
                 reviews_checked_at = ?, review_total = ?, review_positive = ?, review_negative = ?,
                 review_score_desc = ?, reviews_day_range_used = ?
               WHERE appid = ?""",
            (now, now, summary.total, summary.positive, summary.negative, summary.score_desc, day_range_used, appid),
        )
        if summary.total > 0 and 0 <= summary.positive <= summary.total:  # zeros stand for "no answer"
            conn.execute(UPSERT_REVIEW_SNAPSHOT, (appid, now, summary.total, summary.positive, summary.negative))
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


def record_reviews_problem(conn: Connection, appid: int, *, error: str, now: str) -> None:
    """Note a failed review fetch. The last good summary and excerpts are kept."""
    with conn:
        conn.execute(
            "UPDATE games SET reviews_status = 'error', reviews_error = ?, reviews_checked_at = ? WHERE appid = ?",
            (error, now, appid),
        )


def save_prices(conn: Connection, prices: dict[int, PriceFacts], *, country: str, currency: str | None,
                now: str) -> None:
    """Replace the stored price of each game in `prices` with what Steam reported at `now`."""
    with conn:
        conn.executemany(
            UPSERT_PRICE,
            [
                (appid, country, currency, facts.status, facts.final_cents, facts.original_cents,
                 facts.discount_percent, facts.final_text, facts.original_text, facts.discount_ends_at,
                 int(facts.discount_hidden), facts.purchase_option, now)
                for appid, facts in prices.items()
            ],
        )


def save_player_count(conn: Connection, appid: int, players: int | None, *, now: str) -> None:
    """Add one player snapshot. `players` is None when Steam reported no count for the app."""
    with conn:
        conn.execute(UPSERT_PLAYER_SNAPSHOT, (appid, now, players))


def latest_player_times(conn: Connection, appids: list[int]) -> dict[int, str]:
    """When each of `appids` last got a player snapshot (the sync's due check)."""
    if not appids:
        return {}
    rows = conn.execute(
        f"""SELECT appid, MAX(captured_at) AS captured_at FROM player_snapshots
            WHERE appid IN ({','.join('?' * len(appids))}) GROUP BY appid""",
        appids,
    )
    return {row["appid"]: row["captured_at"] for row in rows}


DISCOVERY_LOOKUP_SIZE = 500  # app ids per lookup of known apps (well under either database's parameter limit)


def upsert_discovered_apps(conn: Connection, apps: list[dict], *, now: str) -> tuple[int, int]:
    """Record one page of catalog discovery results. Return (new apps, changed apps).

    The known apps of the page are read in a few lookups and the rows are written in two batches, so a page of
    10,000 apps takes a few round trips to a PostgreSQL server instead of 20,000. An app listed twice is new the first
    time and an update the second time, as it would be one app at a time.
    """
    appids = list(dict.fromkeys(int(app["appid"]) for app in apps))
    known: dict[int, int | None] = {}
    for start in range(0, len(appids), DISCOVERY_LOOKUP_SIZE):
        batch = appids[start:start + DISCOVERY_LOOKUP_SIZE]
        rows = conn.execute("SELECT appid, last_modified FROM discovered_apps "
                            f"WHERE appid IN ({','.join('?' * len(batch))})", batch)
        known.update((row["appid"], row["last_modified"]) for row in rows)
    inserts, updates = [], []
    new = changed = 0
    for app in apps:
        appid, last_modified = int(app["appid"]), app.get("last_modified")
        if appid in known:
            changed += int(known[appid] != last_modified)
            updates.append((app.get("name"), last_modified, app.get("price_change_number"), now, appid))
        else:
            new += 1
            inserts.append((appid, app.get("name"), last_modified, app.get("price_change_number"), now, now))
        known[appid] = last_modified
    with conn:
        conn.executemany(
            """INSERT INTO discovered_apps (appid, name, last_modified, price_change_number, first_seen_at,
                 last_seen_at) VALUES (?, ?, ?, ?, ?, ?)""",
            inserts,
        )
        conn.executemany(
            """UPDATE discovered_apps SET name = ?, last_modified = ?, price_change_number = ?, last_seen_at = ?
               WHERE appid = ?""",
            updates,
        )
    return new, changed


def copy_discovered_last_modified(conn: Connection) -> None:
    """Give curated games the newest last_modified seen by discovery (used to detect changes)."""
    with conn:
        conn.execute(
            """UPDATE games SET steam_last_modified =
                 (SELECT d.last_modified FROM discovered_apps d WHERE d.appid = games.appid)
               WHERE appid IN (SELECT appid FROM discovered_apps)"""
        )


def get_state(conn: Connection, key: str) -> str | None:
    """Read a small sync bookmark, such as the time of the last successful discovery."""
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: Connection, key: str, value: str) -> None:
    """Save a small sync bookmark."""
    with conn:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def start_sync_run(conn: Connection, *, now: str) -> int:
    """Record the start of a sync. A run left 'running' by a crashed process is marked failed."""
    abandoned = json.dumps({"errors": ["The run stopped before it finished (the process ended early)."]})
    with conn:
        conn.execute(
            "UPDATE sync_runs SET status = 'failed', finished_at = ?, summary = ? WHERE status = 'running'",
            (now, abandoned),
        )
        return conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'running') RETURNING id",
                            (now,)).fetchall()[0][0]


def finish_sync_run(conn: Connection, run_id: int, *, status: str, summary: dict, now: str) -> None:
    """Record how a sync ended, with its counts and first errors."""
    with conn:
        conn.execute(
            "UPDATE sync_runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
            (status, now, json.dumps(summary, ensure_ascii=False), run_id),
        )


# ---------------------------------------------------------------------- web reads


def recent_sync_runs(conn: Connection, limit: int = 5) -> list[Row]:
    """Return the latest sync runs, newest first."""
    return conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def run_problem(run: Row) -> str | None:
    """The main problem recorded for a sync run: why it stopped early, or else its first error."""
    if not run["summary"]:
        return None
    summary = json.loads(run["summary"])
    errors = summary.get("errors") or []
    return summary.get("stopped_early") or (errors[0] if errors else None)


def last_successful_sync(conn: Connection) -> Row | None:
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


def matching_games(conn: Connection, words: list[str], tags: list[str]) -> list[Row]:
    """Return the visible games that match a name search and tag filter, in library order.

    Each row holds only what filtering, sorting and ranking need: appid, sort_name,
    review_positive, review_total, review_score_desc, release_date, coming_soon, and modes and
    publishers (JSON). With no words and no tags, this is the whole visible catalog, which the
    ranking is computed over.
    """
    where, params = _visible_filter(words, tags)
    return conn.execute(
        f"""SELECT g.appid, g.sort_name, g.review_positive, g.review_total, g.review_score_desc, g.release_date,
                   g.coming_soon, g.modes, g.publishers
            FROM games g WHERE {where} ORDER BY g.sort_name, g.appid""",
        params,
    ).fetchall()


def tag_counts(conn: Connection, appids: list[int], *, limit: int) -> list[Row]:
    """Count tags among `appids` (the current results), most common first."""
    if not appids:
        return []
    return conn.execute(
        f"""SELECT tag, COUNT(*) AS games FROM game_tags WHERE appid IN ({','.join('?' * len(appids))})
            GROUP BY tag ORDER BY games DESC, tag LIMIT ?""",
        [*appids, limit],
    ).fetchall()


def tags_for(conn: Connection, appids: list[int]) -> dict[int, list[str]]:
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


def top_tags_of_visible_games(conn: Connection, per_game: int) -> dict[int, list[str]]:
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


def excerpts_for(conn: Connection, appids: list[int]) -> dict[int, dict[str, list[Row]]]:
    """Return review excerpts as {appid: {"positive": [...], "negative": [...]}}."""
    excerpts: dict[int, dict[str, list[Row]]] = defaultdict(lambda: {"positive": [], "negative": []})
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


def catalog_counts(conn: Connection) -> dict[str, int]:
    """Counts for the status panel: curated games, games ready to show, and discovered apps."""
    row = conn.execute(
        f"""SELECT
              (SELECT COUNT(*) FROM games WHERE is_curated = 1) AS curated,
              (SELECT COUNT(*) FROM games g WHERE {VISIBLE_GAMES}) AS ready,
              (SELECT COUNT(*) FROM discovered_apps) AS discovered"""
    ).fetchone()
    return dict(row)


def price_rows(conn: Connection, appids: list[int]) -> dict[int, Row]:
    """The stored Steam price of each of `appids` that has one, keyed by app id."""
    if not appids:
        return {}
    rows = conn.execute(f"SELECT * FROM prices WHERE appid IN ({','.join('?' * len(appids))})", appids)
    return {row["appid"]: row for row in rows}


def latest_player_counts(conn: Connection) -> dict[int, Row]:
    """The newest player snapshot (appid, captured_at, players) of every visible game that has one."""
    rows = conn.execute(
        f"""SELECT p.appid, p.captured_at, p.players FROM player_snapshots p
            JOIN (SELECT appid, MAX(captured_at) AS captured_at FROM player_snapshots GROUP BY appid) newest
              ON newest.appid = p.appid AND newest.captured_at = p.captured_at
            JOIN games g ON g.appid = p.appid
            WHERE {VISIBLE_GAMES}"""
    )
    return {row["appid"]: row for row in rows}


def _history(conn: Connection, table: str, columns: str, since: str) -> dict[int, list[Row]]:
    """Snapshots of the visible games captured at or after `since`, oldest first, keyed by app id."""
    history: dict[int, list[Row]] = defaultdict(list)
    rows = conn.execute(  # table and columns come from the two fixed calls below, never from input
        f"""SELECT s.appid, s.captured_at, {columns} FROM {table} s JOIN games g ON g.appid = s.appid
            WHERE {VISIBLE_GAMES} AND s.captured_at >= ? ORDER BY s.appid, s.captured_at""",
        (since,),
    )
    for row in rows:
        history[row["appid"]].append(row)
    return dict(history)


def player_history(conn: Connection, since: str) -> dict[int, list[Row]]:
    """Player snapshots (captured_at, players) of the visible games since `since`, oldest first."""
    return _history(conn, "player_snapshots", "s.players", since)


def review_history(conn: Connection, since: str) -> dict[int, list[Row]]:
    """Review snapshots (captured_at, total, positive, negative) of the visible games since `since`, oldest first."""
    return _history(conn, "review_snapshots", "s.total, s.positive, s.negative", since)


def snapshot_summary(conn: Connection) -> dict[str, dict]:
    """For each snapshot kind: games covered, first and last capture, and distinct UTC days with captures."""
    summary = {}
    for kind, table in (("players", "player_snapshots"), ("reviews", "review_snapshots")):
        row = conn.execute(  # the table names come from the fixed tuple above
            f"""SELECT COUNT(DISTINCT s.appid) AS games, MIN(s.captured_at) AS first, MAX(s.captured_at) AS last,
                       COUNT(DISTINCT substr(s.captured_at, 1, 10)) AS days
                FROM {table} s JOIN games g ON g.appid = s.appid WHERE {VISIBLE_GAMES}"""
        ).fetchone()
        summary[kind] = dict(row)
    return summary


def price_summary(conn: Connection) -> dict:
    """Prices stored for the visible games: how many, in which regions ("US", or "GB,US"), the oldest and newest
    check, discounts."""
    row = conn.execute(
        f"""SELECT COUNT(*) AS games, MIN(p.checked_at) AS first, MAX(p.checked_at) AS last,
                   COUNT(CASE WHEN p.discount_percent > 0 THEN 1 END) AS discounted
            FROM prices p JOIN games g ON g.appid = p.appid WHERE {VISIBLE_GAMES}"""
    ).fetchone()
    regions = [region["country_code"] for region in conn.execute(
        f"""SELECT DISTINCT p.country_code FROM prices p JOIN games g ON g.appid = p.appid WHERE {VISIBLE_GAMES}
            ORDER BY p.country_code""")]
    return {"games": row["games"], "regions": ",".join(regions) or None, "first": row["first"], "last": row["last"],
            "discounted": row["discounted"]}


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


def candidate(conn: Connection, *, appid: int | None = None, candidate_id: int | None = None,
              igdb_id: int | None = None) -> Row | None:
    """One candidate, found by Steam app id, candidate id or IGDB id."""
    for column, value in (("appid", appid), ("id", candidate_id), ("igdb_id", igdb_id)):
        if value is not None:  # column names come from the fixed tuple above, never from input
            return conn.execute(f"SELECT * FROM candidates WHERE {column} = ?", (value,)).fetchone()
    return None


def add_candidate(conn: Connection, *, title: str, source: dict, match_method: str, now: str,
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


def discovered_apps_to_add(conn: Connection, limit: int) -> list[Row]:
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


def discovered_app_names(conn: Connection) -> list[Row]:
    """Every named app in the discovery index (appid, name), for title-only matching."""
    return conn.execute("SELECT appid, name FROM discovered_apps WHERE name IS NOT NULL AND name != '' "
                        "ORDER BY appid").fetchall()


def candidates_to_screen(conn: Connection, limit: int, *, max_attempts: int,
                         recheck_before: str | None = None) -> list[Row]:
    """Candidates with a Steam app id that are due for screening, oldest first.

    Due: never screened (pending), or the last attempt failed fewer than `max_attempts` times in a row.
    With `recheck_before`, eligible and ineligible candidates screened at or before that time are due
    again (for example to see whether a game has collected enough reviews since).
    """
    params: list = [max_attempts]
    recheck = ""
    if recheck_before:
        recheck = "OR (status IN ('eligible', 'ineligible') AND screened_at <= ?)"
        params.append(recheck_before)
    return conn.execute(
        f"""SELECT * FROM candidates
            WHERE appid IS NOT NULL
              AND (status = 'pending' OR (status = 'error' AND attempts < ?) {recheck})
            ORDER BY id LIMIT ?""",
        [*params, limit],
    ).fetchall()


def save_screening(conn: Connection, candidate_id: int, facts: CandidateFacts | None, *,
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


def record_screening_error(conn: Connection, candidate_id: int, *, error: str, now: str) -> None:
    """Note a failed screening request. The candidate is tried again by a later run."""
    with conn:
        conn.execute(
            """UPDATE candidates SET status = 'error', attempts = attempts + 1, last_error = ?, updated_at = ?
               WHERE id = ?""",
            (error, now, candidate_id),
        )


def set_candidate_status(conn: Connection, candidate_id: int, *, status: str, reason: str | None,
                         now: str) -> None:
    """Record a reviewer's decision (promoted, rejected) or a reset (pending)."""
    with conn:
        conn.execute("UPDATE candidates SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
                     (status, reason, now, candidate_id))


def match_candidate(conn: Connection, candidate_id: int, appid: int, *, now: str) -> None:
    """Give a title-only candidate the Steam app a reviewer confirmed; it is screened on the next run."""
    with conn:
        conn.execute(
            """UPDATE candidates SET appid = ?, match_method = 'manual', status = 'pending',
                 reason = 'Steam app confirmed by a reviewer; waiting for screening.', updated_at = ?
               WHERE id = ?""",
            (appid, now, candidate_id),
        )


def list_candidates(conn: Connection, *, status: str | None = None, source: str | None = None,
                    limit: int = 25) -> list[Row]:
    """Candidates for review: the most Steam reviews first, then the oldest.

    `source` keeps the candidates that one of their sources (the JSON list in `sources`) names; it is checked here
    rather than in SQL, because SQLite and PostgreSQL read JSON with different functions.
    """
    where, params = ("WHERE status = ?", [status]) if status else ("", [])
    rows = conn.execute(f"SELECT * FROM candidates {where} ORDER BY COALESCE(review_count, -1) DESC, id", params)
    if source:
        rows = (row for row in rows if any(item.get("source") == source for item in json.loads(row["sources"])))
    found = []
    for row in rows:
        if len(found) == limit:
            break
        found.append(row)
    return found


def candidate_counts(conn: Connection) -> dict[str, int]:
    """How many candidates have each status."""
    return {row["status"]: row["n"] for row in conn.execute("SELECT status, COUNT(*) AS n FROM candidates GROUP BY status")}


def start_candidate_run(conn: Connection, command: str, *, now: str) -> int:
    """Record the start of a candidates command. A run left 'running' by a crashed process is marked failed."""
    abandoned = json.dumps({"errors": ["The run stopped before it finished (the process ended early)."]})
    with conn:
        conn.execute(
            "UPDATE candidate_runs SET status = 'failed', finished_at = ?, summary = ? WHERE status = 'running'",
            (now, abandoned),
        )
        return conn.execute("INSERT INTO candidate_runs (command, started_at, status) VALUES (?, ?, 'running') "
                            "RETURNING id", (command, now)).fetchall()[0][0]


def finish_candidate_run(conn: Connection, run_id: int, *, status: str, summary: dict, now: str) -> None:
    with conn:
        conn.execute("UPDATE candidate_runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                     (status, now, json.dumps(summary, ensure_ascii=False), run_id))


def recent_candidate_runs(conn: Connection, limit: int = 5) -> list[Row]:
    return conn.execute("SELECT * FROM candidate_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------------- other sources: runs


def start_source_run(conn: Connection, source: str, *, now: str) -> int:
    """Record the start of an IGDB or Wikipedia refresh. A run of the same source left 'running' is marked failed."""
    abandoned = json.dumps({"errors": ["The run stopped before it finished (the process ended early)."]})
    with conn:
        conn.execute("UPDATE source_runs SET status = 'failed', finished_at = ?, summary = ? "
                     "WHERE source = ? AND status = 'running'", (now, abandoned, source))
        return conn.execute("INSERT INTO source_runs (source, started_at, status) VALUES (?, ?, 'running') "
                            "RETURNING id", (source, now)).fetchall()[0][0]


def finish_source_run(conn: Connection, run_id: int, *, status: str, summary: dict, now: str) -> None:
    with conn:
        conn.execute("UPDATE source_runs SET status = ?, finished_at = ?, summary = ? WHERE id = ?",
                     (status, now, json.dumps(summary, ensure_ascii=False), run_id))


def last_source_run(conn: Connection, source: str, *,
                    statuses: tuple[str, ...] = ("success", "partial")) -> Row | None:
    """The newest run of `source` that ended with one of `statuses` (by default: one that saved data)."""
    marks = ",".join("?" * len(statuses))
    return conn.execute(f"SELECT * FROM source_runs WHERE source = ? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
                        (source, *statuses)).fetchone()


def recent_source_runs(conn: Connection, source: str, limit: int = 5) -> list[Row]:
    return conn.execute("SELECT * FROM source_runs WHERE source = ? ORDER BY id DESC LIMIT ?",
                        (source, limit)).fetchall()


def source_requests_since(conn: Connection, source: str, since: str) -> int:
    """The requests that the runs of `source` started at or after `since` recorded (summary counts "requests")."""
    total = 0
    for row in conn.execute("SELECT summary FROM source_runs WHERE source = ? AND started_at >= ?", (source, since)):
        counts = json.loads(row["summary"]).get("counts", {}) if row["summary"] else {}
        total += int(counts.get("requests", 0))
    return total


# ---------------------------------------------------------------------- IGDB games (upcoming.py)

IGDB_JSON_COLUMNS = ("platforms", "release_dates", "release_summary", "publishers", "developers", "websites",
                     "steam_appids")
IGDB_COLUMNS = ("igdb_id", "name", "slug", "igdb_url", "summary", "game_type", "game_status", "first_release_date",
                "cover_image_id", *IGDB_JSON_COLUMNS)


def igdb_game_rows(conn: Connection, igdb_ids: list[int] | None = None) -> dict[int, Row]:
    """Stored IGDB game records keyed by IGDB id: all of them, or those in `igdb_ids`."""
    if igdb_ids is None:
        rows = conn.execute("SELECT * FROM igdb_games")
    elif not igdb_ids:
        return {}
    else:
        rows = conn.execute(f"SELECT * FROM igdb_games WHERE igdb_id IN ({','.join('?' * len(igdb_ids))})",
                            list(igdb_ids))
    return {row["igdb_id"]: row for row in rows}


def save_igdb_games(conn: Connection, records: list[dict], matches: dict, changes: list[tuple],
                    *, now: str) -> None:
    """Insert or update IGDB records, their Steam matches and the release-date changes noticed now.

    `matches` maps IGDB ids to upcoming.SteamMatch; a record without one keeps its stored match.
    `changes` holds (igdb_id, upcoming.DateChange) pairs.
    """
    columns = ", ".join(IGDB_COLUMNS)
    updates = ", ".join(f"{column} = excluded.{column}" for column in IGDB_COLUMNS if column != "igdb_id")
    with conn:
        for record in records:
            values = [json.dumps(record[column], ensure_ascii=False) if column in IGDB_JSON_COLUMNS else record[column]
                      for column in IGDB_COLUMNS]
            conn.execute(  # the column names come from the constants above, never from input
                f"""INSERT INTO igdb_games ({columns}, first_seen_at, checked_at)
                    VALUES ({', '.join('?' * len(IGDB_COLUMNS))}, ?, ?)
                    ON CONFLICT (igdb_id) DO UPDATE SET {updates}, checked_at = excluded.checked_at""",
                [*values, now, now],
            )
            match = matches.get(record["igdb_id"])
            if match is not None:
                conn.execute("""UPDATE igdb_games SET steam_match = ?, steam_appid = ?, steam_match_reason = ?,
                                  steam_checked_at = ? WHERE igdb_id = ?""",
                             (match.status, match.appid, match.reason, now, record["igdb_id"]))
        conn.executemany(
            UPSERT_RELEASE_CHANGE,
            [(igdb_id, change.platform, change.before, change.after, change.kind, now) for igdb_id, change in changes],
        )


def release_changes(conn: Connection, igdb_ids: list[int], since: str) -> dict[int, list[Row]]:
    """Release-date changes noticed at or after `since` for these games, newest first."""
    if not igdb_ids:
        return {}
    found: dict[int, list[Row]] = defaultdict(list)
    rows = conn.execute(
        f"""SELECT * FROM release_changes WHERE noticed_at >= ? AND igdb_id IN ({','.join('?' * len(igdb_ids))})
            ORDER BY noticed_at DESC, platform""",
        [since, *igdb_ids],
    )
    for row in rows:
        found[row["igdb_id"]].append(row)
    return dict(found)


def save_popularity_snapshot(conn: Connection, signal: str, entries: list, *, now: str) -> None:
    """Save one dated snapshot of a signal's tracked upcoming games (upcoming.SignalEntry objects)."""
    with conn:
        conn.executemany(
            UPSERT_POPULARITY,
            [(signal, entry.igdb_id, now, entry.position, entry.value,
              entry.calculated_at.isoformat(timespec="seconds") if entry.calculated_at else None) for entry in entries],
        )


def latest_popularity(conn: Connection, signal: str) -> list[Row]:
    """The newest snapshot of a signal, best position first."""
    return conn.execute(
        """SELECT * FROM popularity_snapshots WHERE signal = ? AND captured_at =
             (SELECT MAX(captured_at) FROM popularity_snapshots WHERE signal = ?) ORDER BY position""",
        (signal, signal),
    ).fetchall()


def popularity_history(conn: Connection, signal: str, since: str) -> dict[int, list[Row]]:
    """A signal's snapshots (captured_at, position) since `since`, oldest first, keyed by IGDB id."""
    history: dict[int, list[Row]] = defaultdict(list)
    rows = conn.execute("SELECT igdb_id, captured_at, position FROM popularity_snapshots "
                        "WHERE signal = ? AND captured_at >= ? ORDER BY igdb_id, captured_at", (signal, since))
    for row in rows:
        history[row["igdb_id"]].append(row)
    return dict(history)


def popularity_snapshot_rows(conn: Connection, signal: str) -> list[Row]:
    """Every saved row of a signal (igdb_id, captured_at, position, calculated_at), oldest snapshot first."""
    return conn.execute("SELECT igdb_id, captured_at, position, calculated_at FROM popularity_snapshots "
                        "WHERE signal = ? ORDER BY captured_at, position", (signal,)).fetchall()


def popularity_summary(conn: Connection) -> dict[str, dict]:
    """For each signal: games covered, first and last snapshot, and distinct UTC days with snapshots."""
    summary = {}
    for row in conn.execute("""SELECT signal, COUNT(DISTINCT igdb_id) AS games, MIN(captured_at) AS first,
                                      MAX(captured_at) AS last, COUNT(DISTINCT substr(captured_at, 1, 10)) AS days
                               FROM popularity_snapshots GROUP BY signal"""):
        summary[row["signal"]] = dict(row)
    return summary


def save_steam_igdb_links(conn: Connection, links: dict[int, tuple[str, int | None]], *, now: str) -> None:
    """Replace the IGDB link of each catalog app in `links` ((status, igdb_id) per app id)."""
    with conn:
        conn.executemany(UPSERT_STEAM_IGDB_LINK,
                         [(appid, igdb_id, status, now) for appid, (status, igdb_id) in links.items()])


def steam_igdb_link(conn: Connection, appid: int) -> Row | None:
    return conn.execute("SELECT * FROM steam_igdb_links WHERE appid = ?", (appid,)).fetchone()


def igdb_ids_for_appid(conn: Connection, appid: int) -> list[int]:
    """The IGDB ids known to be the same game as Steam app `appid`: its catalog link and verified matches."""
    rows = conn.execute(
        """SELECT igdb_id FROM steam_igdb_links WHERE appid = ? AND status = 'linked'
           UNION SELECT igdb_id FROM igdb_games WHERE steam_appid = ? AND steam_match = 'verified'
           ORDER BY igdb_id""",
        (appid, appid),
    )
    return [row["igdb_id"] for row in rows]


def igdb_game_for_appid(conn: Connection, appid: int) -> Row | None:
    """The stored IGDB record whose verified Steam match is `appid`, if any (lowest IGDB id first)."""
    return conn.execute("SELECT * FROM igdb_games WHERE steam_appid = ? AND steam_match = 'verified' "
                        "ORDER BY igdb_id LIMIT 1", (appid,)).fetchone()


def catalog_links_by_igdb(conn: Connection, appids) -> dict[int, int]:
    """IGDB id -> catalog app, for IGDB games that exactly one of `appids` is linked to.

    A link means IGDB's own Steam record of the app names this one IGDB game (steam_igdb_links). An IGDB
    game linked to two catalog apps (two Steam editions, say) is left out, so neither is chosen.
    """
    wanted = set(appids)
    found: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute("SELECT appid, igdb_id FROM steam_igdb_links WHERE status = 'linked'"):
        if row["appid"] in wanted:
            found[row["igdb_id"]].add(row["appid"])
    return {igdb_id: next(iter(apps)) for igdb_id, apps in found.items() if len(apps) == 1}


# ---------------------------------------------------------------------- Twitch top games (upcoming.py refresh)


def save_twitch_top_games(conn: Connection, categories: list, *, now: str,
                          igdb_links: dict[str, list[int]] | None = None) -> None:
    """Replace the stored Twitch list with a new answer (twitch.TopCategory objects, in Twitch's order).

    `igdb_links` maps a category id to the IGDB games whose IGDB record lists that category (upcoming._twitch_links).
    """
    links = igdb_links or {}
    with conn:
        conn.execute("DELETE FROM twitch_top_games")
        conn.executemany(
            "INSERT INTO twitch_top_games (position, category_id, name, igdb_id, checked_at, igdb_links) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(item.position, item.category_id, item.name, item.igdb_id, now,
              json.dumps(links.get(item.category_id, []))) for item in categories],
        )


def twitch_top_games(conn: Connection, since: str) -> list[Row]:
    """The stored Twitch list in Twitch's order, only when it was checked at or after `since`."""
    return conn.execute("SELECT * FROM twitch_top_games WHERE checked_at >= ? ORDER BY position", (since,)).fetchall()


def purge_twitch_top_games(conn: Connection, older_than: str) -> int:
    """Delete a Twitch list checked before `older_than` (Twitch allows caching its data for 24 hours)."""
    with conn:
        return conn.execute("DELETE FROM twitch_top_games WHERE checked_at < ?", (older_than,)).rowcount


# ---------------------------------------------------------------------- TwitchTracker summaries (twitchtracker.py)


def replace_tracker_selection(conn: Connection, categories: list) -> None:
    """Make the chosen categories (twitchtracker.Category objects) the ones TwitchTracker is asked about.

    A category no longer chosen is deleted with its figures; one chosen again keeps its figures and gets its new name,
    game and choice time; a new one starts unchecked.
    """
    with conn:
        wanted = [item.category_id for item in categories]
        if wanted:
            conn.execute("DELETE FROM twitchtracker_summaries "
                         f"WHERE category_id NOT IN ({','.join('?' * len(wanted))})", wanted)
        else:  # NOT IN () is SQLite-only syntax; with nothing chosen, nothing stays
            conn.execute("DELETE FROM twitchtracker_summaries")
        conn.executemany(
            """INSERT INTO twitchtracker_summaries (category_id, category_name, igdb_id, match_source, selected_at,
                 status) VALUES (?, ?, ?, ?, ?, 'unchecked')
               ON CONFLICT (category_id) DO UPDATE SET category_name = excluded.category_name,
                 igdb_id = excluded.igdb_id, match_source = excluded.match_source,
                 selected_at = excluded.selected_at""",
            [(item.category_id, item.name, item.igdb_id, item.match_source, item.selected_at) for item in categories],
        )


def tracker_rows(conn: Connection) -> list[Row]:
    """Every chosen category with its newest TwitchTracker figures (NULL before any answer), by category id."""
    return conn.execute("SELECT * FROM twitchtracker_summaries ORDER BY category_id").fetchall()


def save_tracker_answer(conn: Connection, category_id: str, summary, *, retrieved_at: str) -> None:
    """Store TwitchTracker's answer about one category: a twitchtracker.Summary, or None when it has no summary.

    None clears the figures (TwitchTracker's newest answer gave none), so an older rank is never shown as current.
    """
    figures = ((summary.rank, summary.avg_viewers, summary.avg_channels, summary.hours_watched) if summary
               else (None, None, None, None))
    with conn:
        conn.execute(
            """UPDATE twitchtracker_summaries SET rank = ?, avg_viewers = ?, avg_channels = ?, hours_watched = ?,
                 retrieved_at = ?, status = ?, checked_at = ?, error = NULL WHERE category_id = ?""",
            (*figures, retrieved_at, "ok" if summary else "no_summary", retrieved_at, category_id),
        )


def record_tracker_problem(conn: Connection, category_id: str, *, status: str, checked_at: str,
                           error: str) -> None:
    """Record a failed check of one category ("invalid" or "error"). Its last figures stay, with their own time."""
    with conn:
        conn.execute("UPDATE twitchtracker_summaries SET status = ?, checked_at = ?, error = ? WHERE category_id = ?",
                     (status, checked_at, error, category_id))


# ---------------------------------------------------------------------- Twitch cache (twitch.py)


def twitch_live_row(conn: Connection, game_key: str) -> Row | None:
    return conn.execute("SELECT * FROM twitch_live WHERE game_key = ?", (game_key,)).fetchone()


def save_twitch_live(conn: Connection, game_key: str, *, status: str, checked_at: str,
                     category_id: str | None = None, category_name: str | None = None, streams: list | None = None,
                     fetched_at: str | None = None, retry_at: str | None = None, error: str | None = None) -> None:
    """Store the outcome of one successful Twitch check (it replaces the game's previous one)."""
    with conn:
        conn.execute(
            UPSERT_TWITCH_LIVE,
            (game_key, status, category_id, category_name, json.dumps(streams or [], ensure_ascii=False), fetched_at,
             checked_at, retry_at, error),
        )


def record_twitch_problem(conn: Connection, game_key: str, *, status: str, checked_at: str,
                          retry_at: str | None, error: str) -> None:
    """Record a failed Twitch check. The game's last good answer, if any, stays next to the error."""
    with conn:
        updated = conn.execute("UPDATE twitch_live SET status = ?, checked_at = ?, retry_at = ?, error = ? "
                               "WHERE game_key = ?", (status, checked_at, retry_at, error, game_key)).rowcount
        if not updated:
            conn.execute("INSERT INTO twitch_live (game_key, status, streams, checked_at, retry_at, error) "
                         "VALUES (?, ?, '[]', ?, ?, ?)", (game_key, status, checked_at, retry_at, error))


def purge_twitch_cache(conn: Connection, older_than: str) -> int:
    """Delete Twitch answers last checked before `older_than`, and older answers kept next to newer errors."""
    with conn:
        deleted = conn.execute("DELETE FROM twitch_live WHERE checked_at < ?", (older_than,)).rowcount
        conn.execute("UPDATE twitch_live SET streams = '[]', category_id = NULL, category_name = NULL, fetched_at = NULL "
                     "WHERE fetched_at IS NOT NULL AND fetched_at < ?", (older_than,))
    return deleted


# ---------------------------------------------------------------------- critic scores (critics.py)


def critic_article_rows(conn: Connection, keys: list[str] | None = None) -> dict[str, Row]:
    if keys is None:
        rows = conn.execute("SELECT * FROM critic_articles")
    elif not keys:
        return {}
    else:
        rows = conn.execute(f"SELECT * FROM critic_articles WHERE game_key IN ({','.join('?' * len(keys))})",
                            list(keys))
    return {row["game_key"]: row for row in rows}


def save_critics(conn: Connection, game_key: str, article: dict, scores: list[dict] | None, *,
                 now: str) -> None:
    """Replace a game's article match and, when `scores` is not None, its scores."""
    with conn:
        conn.execute(
            """INSERT INTO critic_articles (game_key, status, reason, wikidata_ids, article_title, article_section,
                 revision_id, revision_at, table_status, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (game_key) DO UPDATE SET status = excluded.status, reason = excluded.reason,
                 wikidata_ids = excluded.wikidata_ids, article_title = excluded.article_title,
                 article_section = excluded.article_section, revision_id = excluded.revision_id,
                 revision_at = excluded.revision_at, table_status = excluded.table_status,
                 checked_at = excluded.checked_at""",
            (game_key, article["status"], article.get("reason"), json.dumps(article.get("wikidata_ids") or []),
             article.get("article_title"), article.get("article_section"), article.get("revision_id"),
             article.get("revision_at"), article.get("table_status"), now),
        )
        if scores is not None:
            conn.execute("DELETE FROM critic_scores WHERE game_key = ?", (game_key,))
            conn.executemany(
                """INSERT INTO critic_scores (game_key, position, table_title, outlet_code, outlet, kind, score, label,
                     review_url, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(game_key, position, score.get("table_title"), score.get("outlet_code"), score["outlet"],
                  score["kind"], score.get("score"), score.get("label"), score.get("review_url"), score["status"],
                  score.get("reason"))
                 for position, score in enumerate(scores, 1)],
            )


def critic_scores(conn: Connection, game_key: str) -> list[Row]:
    return conn.execute("SELECT * FROM critic_scores WHERE game_key = ? ORDER BY position", (game_key,)).fetchall()


def critic_summary(conn: Connection) -> dict:
    """Counts for `status`: article matches by status, and scores shown or waiting for review."""
    matches = {row["status"]: row["n"] for row in
               conn.execute("SELECT status, COUNT(*) AS n FROM critic_articles GROUP BY status")}
    scores = {row["status"]: row["n"] for row in
              conn.execute("""SELECT s.status, COUNT(*) AS n FROM critic_scores s
                              JOIN critic_articles a ON a.game_key = s.game_key AND a.status = 'verified'
                              GROUP BY s.status""")}
    last = conn.execute("SELECT MAX(checked_at) AS last FROM critic_articles").fetchone()["last"]
    return {"matches": matches, "scores": scores, "last": last}


# ---------------------------------------------------------------------- sales estimates (gamalytic.py)


def save_sales_estimates(conn: Connection, estimates: dict[int, int | None], *, source: str,
                         collected_at: str, source_cached_at: str | None) -> None:
    """Add one collection: a row per game in `estimates`, whose value is None when the source gave no estimate."""
    with conn:
        conn.executemany(
            UPSERT_SALES_ESTIMATE,
            [(appid, collected_at, source, copies, source_cached_at) for appid, copies in estimates.items()],
        )


def latest_sales_estimates(conn: Connection, appids: list[int]) -> dict[int, Row]:
    """The newest collection of each of `appids` that was ever asked about, keyed by app id.

    copies_sold is NULL when the source gave no estimate in that collection: an older estimate is not used then.
    """
    if not appids:
        return {}
    rows = conn.execute(
        f"""SELECT s.* FROM sales_estimates s
            JOIN (SELECT appid, MAX(collected_at) AS collected_at FROM sales_estimates
                  WHERE appid IN ({','.join('?' * len(appids))}) GROUP BY appid) newest
              ON newest.appid = s.appid AND newest.collected_at = s.collected_at""",
        list(appids),
    )
    return {row["appid"]: row for row in rows}


def sales_estimate_summary(conn: Connection) -> dict:
    """Visible games ever estimated, and the collections (count, first, last), for `status` and the dashboard."""
    row = conn.execute(
        f"""SELECT COUNT(DISTINCT CASE WHEN s.copies_sold IS NOT NULL THEN s.appid END) AS games,
                   COUNT(DISTINCT s.collected_at) AS collections, MIN(s.collected_at) AS first,
                   MAX(s.collected_at) AS last
            FROM sales_estimates s JOIN games g ON g.appid = s.appid WHERE {VISIBLE_GAMES}"""
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------- accounts and wishlists (accounts.py, web.py)
#
# Every function takes the account id that web.py looked up for the signed-in request, never an id from the browser,
# and every statement is limited to that account, so one person's requests can never read or change another's list.


def account_id(conn: Connection, provider: str, subject: str, display_name: str, *, now: str) -> int:
    """The id of the account of a signed-in person, made at their first visit; its name follows the provider's."""
    query = "SELECT id, display_name FROM accounts WHERE provider = ? AND subject = ?"
    row = conn.execute(query, (provider, subject)).fetchone()
    if row is None:
        with conn:  # two first requests at once: one inserts, the other finds that row
            conn.execute("INSERT INTO accounts (provider, subject, display_name, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT (provider, subject) DO NOTHING", (provider, subject, display_name, now))
        row = conn.execute(query, (provider, subject)).fetchone()
    elif row["display_name"] != display_name:
        with conn:
            conn.execute("UPDATE accounts SET display_name = ? WHERE id = ?", (display_name, row["id"]))
    return row["id"]


def find_account(conn: Connection, provider: str, subject: str) -> Row | None:
    """The account of a person, if they have one, without making one."""
    return conn.execute("SELECT * FROM accounts WHERE provider = ? AND subject = ?", (provider, subject)).fetchone()


def wishlist_items(conn: Connection, account: int) -> list[Row]:
    """One account's saved games (game_key, name, added_at), the oldest first."""
    return conn.execute("SELECT game_key, name, added_at FROM wishlist_items WHERE account_id = ? ORDER BY id",
                        (account,)).fetchall()


def toggle_wishlist_item(conn: Connection, account: int, key: str, name: str, aliases: list[str], *, now: str,
                         limit: int) -> bool | None:
    """Save a game, or remove it when it is saved under its key or one of its aliases (the browser wishlist's rule).

    Return True when the game is now saved, False when it was removed, and None when the list already holds `limit`
    games, so nothing changed.
    """
    keys = list(dict.fromkeys([key, *aliases]))
    with conn:
        removed = conn.execute(f"DELETE FROM wishlist_items WHERE account_id = ? AND game_key IN "
                               f"({','.join('?' * len(keys))})", (account, *keys)).rowcount
        if removed:
            return False
        count = conn.execute("SELECT COUNT(*) FROM wishlist_items WHERE account_id = ?", (account,)).fetchone()[0]
        if count >= limit:
            return None
        conn.execute("INSERT INTO wishlist_items (account_id, game_key, name, added_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT (account_id, game_key) DO NOTHING", (account, key, name, now))
    return True


def remove_wishlist_item(conn: Connection, account: int, key: str) -> None:
    """Remove one saved game from an account's list (nothing happens if it is not there)."""
    with conn:
        conn.execute("DELETE FROM wishlist_items WHERE account_id = ? AND game_key = ?", (account, key))


def import_wishlist_items(conn: Connection, account: int, items: list[tuple[str, str]], *, now: str,
                          limit: int) -> dict[str, int]:
    """Add games saved in a browser (key, name pairs, oldest first) that the account does not have yet.

    Games already in the account are left as they are. Once the list holds `limit` games the rest are not added.
    Return how many were added, already saved, and left out because the list was full.
    """
    counts = {"added": 0, "already_saved": 0, "not_added_full": 0}
    with conn:
        saved = {row["game_key"] for row in
                 conn.execute("SELECT game_key FROM wishlist_items WHERE account_id = ?", (account,))}
        for key, name in items:
            if key in saved:
                counts["already_saved"] += 1
            elif len(saved) >= limit:
                counts["not_added_full"] += 1
            else:
                conn.execute("INSERT INTO wishlist_items (account_id, game_key, name, added_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT (account_id, game_key) DO NOTHING", (account, key, name, now))
                saved.add(key)
                counts["added"] += 1
    return counts


def delete_account(conn: Connection, account: int) -> None:
    """Delete an account and its wishlist (the account page's "Delete my account data")."""
    with conn:
        conn.execute("DELETE FROM wishlist_items WHERE account_id = ?", (account,))
        conn.execute("DELETE FROM accounts WHERE id = ?", (account,))
