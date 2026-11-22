"""Copy the local SQLite catalog into the hosted site's PostgreSQL database (`migrate-to-postgres`).

What it copies: every catalog table of store.SCHEMA, row for row and column for column, ids included, so every
relationship (a game's tags, prices, reviews and snapshots; an IGDB game's date changes; an article's scores) stays as
it was. Run history and the sync's bookmarks come along, so the hosted sync continues where the local one stopped.

What it never copies:
* the local-evaluation tables, sales_estimates (Gamalytic) and twitchtracker_summaries (TwitchTracker): their terms
  allow no sharing, so their figures stay in the local SQLite file only (Settings.local_evaluation);
* the account tables (store.ACCOUNT_TABLES): they belong to the hosted site, and nothing here reads, empties or
  replaces them.

How, in order:
1. The SQLite file is opened read-only, in one read transaction: a consistent snapshot even while a sync is writing,
   and the file is never changed.
2. It is checked first: SQLite's own integrity check, that no row refers to a missing row (foreign keys), that every
   table and column of this version exists, and that every value has its column's type. Any problem stops the run
   before anything is written.
3. The PostgreSQL tables are created or upgraded, as init-postgres does.
4. A target that already holds catalog rows is refused, unless `replace`: then the catalog tables (and the empty
   local-evaluation tables) are emptied first. That discards the hosted catalog's own history since the last copy.
5. Each table is loaded with COPY; each id sequence then continues after the highest copied id.
6. Every copied table is compared with its source: the same number of rows and the same checksum of all their values.
7. Twitch rows older than 24 hours are then deleted (Twitch allows caching for 24 hours), as the site itself does.
8. All of it is one transaction, committed only when every comparison matched; any failure leaves the target as it
   was before the run.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import store
from .database import PostgresDatabase, connect_postgres

# Kept only in the local SQLite file: Gamalytic's and TwitchTracker's terms allow no sharing of their figures.
LOCAL_EVALUATION_TABLES = ("sales_estimates", "twitchtracker_summaries")
TWITCH_KEEP_FOR = timedelta(hours=24)  # twitch.KEEP_FOR: Twitch allows caching its data for 24 hours
# The storage classes (SQLite's typeof) each declared column type may hold; NULL is allowed everywhere.
ALLOWED_STORAGE = {"INTEGER": ("integer",), "REAL": ("real", "integer"), "TEXT": ("text",)}


class MigrationError(RuntimeError):
    """A reason the copy cannot start or finish. Nothing has been written when it is raised."""


@dataclass
class TableCopy:
    """One table's result: how many rows the source had and the target got, and whether their checksums matched."""

    table: str
    source_rows: int
    copied_rows: int = 0
    checksum_matched: bool = False


@dataclass
class MigrationReport:
    """What a run found and did, for the command's output."""

    source: str
    target: str
    checked_only: bool = False
    replaced: bool = False
    copied: list[TableCopy] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)  # table -> source rows left out (local evaluation)
    purged_twitch_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    target_rows: dict[str, int] = field(default_factory=dict)  # the target's catalog rows before the run

    @property
    def ok(self) -> bool:
        return self.checked_only or all(item.checksum_matched for item in self.copied)


def migrated_tables() -> list[str]:
    """The tables that are copied, parents before the tables that refer to them (SCHEMA's order)."""
    excluded = set(LOCAL_EVALUATION_TABLES) | set(store.ACCOUNT_TABLES)
    return [table for table in store.schema_columns() if table not in excluded]


def _open_source(path: Path) -> sqlite3.Connection:
    """The SQLite file, read-only (the file is never written), inside one read transaction (a stable snapshot)."""
    if not path.is_file():
        raise MigrationError(f"No SQLite catalog at {path}.")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("BEGIN")  # every SELECT below reads the same snapshot of the file
    return conn


def _check_source(conn: sqlite3.Connection, report: MigrationReport) -> None:
    """Stop with every problem found, before anything is written (step 2 of the module docstring)."""
    problems = []
    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        problems.append(f"SQLite's integrity check reports: {integrity}")
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        tables = sorted({row[0] for row in orphans})
        problems.append(f"{len(orphans)} row(s) refer to a row that does not exist, in {', '.join(tables)}")
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in migrated_tables():
        if table not in present:
            problems.append(f"table {table} is missing")
            continue
        declared = {row[1]: (row[2] or "").upper() for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in store.schema_columns()[table]:
            if column not in declared:
                problems.append(f"column {table}.{column} is missing")
                continue
            allowed = ALLOWED_STORAGE.get(declared[column])
            if allowed is None:
                continue
            marks = ", ".join("?" * (len(allowed) + 1))
            wrong = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE typeof({column}) NOT IN ({marks})",
                                 (*allowed, "null")).fetchone()[0]
            if wrong:
                problems.append(f"{wrong} value(s) of {table}.{column} are not {declared[column]}")
    if problems:
        raise MigrationError("The SQLite catalog cannot be copied as it is: " + "; ".join(problems) + ". Nothing was "
                             "written. A missing table or column is added by opening the catalog once with this "
                             "version, for example with `python -m game_librarian status`.")
    running = conn.execute("SELECT COUNT(*) FROM sync_runs WHERE status = 'running'").fetchone()[0]
    if running:
        report.warnings.append("A sync was running when this snapshot was taken; its run will show as failed in the "
                               "hosted site's history once the hosted sync starts.")


def _checksum(rows) -> str:
    """A checksum of a table's rows that does not depend on their order (each row's values, as Python reads them)."""
    digest = hashlib.sha256()
    for line in sorted(repr(tuple(row)) for row in rows):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def migrate(source_path: Path, target: PostgresDatabase, *, replace: bool = False, check_only: bool = False,
            log: Callable[[str], None] = print,
            now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> MigrationReport:
    """Copy the SQLite catalog at `source_path` into `target`, as the module docstring describes.

    `check_only` runs the checks (source and target) and writes nothing. Raises MigrationError when the copy cannot
    run, or when a comparison fails after loading, in which case the transaction is rolled back.
    """
    report = MigrationReport(source=str(source_path), target=target.describe(), checked_only=check_only,
                             replaced=replace)
    source = _open_source(source_path)
    try:
        _check_source(source, report)
        for table in LOCAL_EVALUATION_TABLES:
            report.skipped[table] = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn = connect_postgres(target)
        try:
            _run(source, conn, report, replace=replace, check_only=check_only, log=log, now=now)
        finally:
            conn.close()
    finally:
        source.close()
    return report


def _target_rows(conn) -> dict[str, int]:
    """The rows of each catalog table in the target; a table that does not exist yet counts as empty."""
    existing = {row["table_name"] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    tables = [table for table in (*migrated_tables(), *LOCAL_EVALUATION_TABLES) if table in existing]
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _run(source: sqlite3.Connection, conn, report: MigrationReport, *, replace: bool, check_only: bool,
         log: Callable[[str], None], now: Callable[[], datetime]) -> None:
    """Steps 3 to 8 in one PostgreSQL transaction (step 2's source checks have passed)."""
    report.target_rows = _target_rows(conn)
    occupied = {table: rows for table, rows in report.target_rows.items() if rows}
    if occupied and not replace:
        shown = ", ".join(f"{table} ({rows} rows)" for table, rows in occupied.items())
        raise MigrationError(f"The PostgreSQL database already holds catalog rows: {shown}. Nothing was written. "
                             "Run again with --replace to empty the catalog tables and copy the SQLite catalog "
                             "anew (this discards the hosted catalog's own history since the last copy; accounts "
                             "and wishlists are kept).")
    tables = migrated_tables()
    if check_only:
        for table in tables:
            report.copied.append(TableCopy(table, source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
        return
    with conn:
        store.create_postgres_schema(conn)
        if replace:
            # The local-evaluation tables are emptied too: they refer to games, and never belong in this database.
            conn.execute(f"TRUNCATE TABLE {', '.join((*tables, *LOCAL_EVALUATION_TABLES))}")
        for table in tables:
            columns = store.schema_columns()[table]
            rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            conn.copy_rows(table, columns, rows)
            item = TableCopy(table, len(rows))
            copied = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            item.copied_rows = len(copied)
            item.checksum_matched = len(copied) == len(rows) and _checksum(copied) == _checksum(rows)
            report.copied.append(item)
            log(f"  {table}: {item.copied_rows} of {item.source_rows} rows copied"
                + ("" if item.checksum_matched else " - MISMATCH"))
            if not item.checksum_matched:
                raise MigrationError(f"{table}: the copy differs from the SQLite rows ({item.copied_rows} of "
                                     f"{item.source_rows} rows, or different values). The transaction was rolled "
                                     "back; nothing was written.")
        for row in conn.execute("SELECT table_name, column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() AND is_identity = 'YES'").fetchall():
            if row["table_name"] in tables:  # the next new id continues after the highest copied one
                conn.execute(f"SELECT setval(pg_get_serial_sequence('{row['table_name']}', '{row['column_name']}'), "
                             f"COALESCE((SELECT MAX({row['column_name']}) FROM {row['table_name']}), 0) + 1, false)")
        older_than = (now() - TWITCH_KEEP_FOR).isoformat(timespec="seconds")
        report.purged_twitch_rows = (store.purge_twitch_cache(conn, older_than)
                                     + store.purge_twitch_top_games(conn, older_than))
