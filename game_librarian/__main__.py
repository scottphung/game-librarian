"""Command line: python -m game_librarian {sync,serve,status,check-curated,export-report}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import store
from .config import DEFAULT_REPORT_DATA_DIR, REPORT_PARAMETERS_FILE, Settings, load_settings
from .curated import load_curated
from .report_export import export_report, point_report_at
from .steam import SteamClient
from .sync import SyncOptions, run_sync

EXIT_CODES = {"success": 0, "failed": 1, "partial": 2}


def _sync(args: argparse.Namespace, settings: Settings) -> int:
    curated = load_curated(settings.curated_dir)
    for problem in curated.problems:
        print(f"warning: curated data: {problem}", file=sys.stderr)
    if not curated.games:
        print("error: curated/games.csv lists no valid games, so there is nothing to sync.", file=sys.stderr)
        return 1
    if args.appids:
        unknown = sorted(set(args.appids) - {game.appid for game in curated.games})
        if unknown:
            print(f"warning: not in curated/games.csv, skipped: {unknown}", file=sys.stderr)

    options = SyncOptions(
        force=args.force,
        appids=args.appids,
        limit=args.limit,
        skip_discovery=args.skip_discovery,
        skip_reviews=args.skip_reviews,
    )
    conn = store.initialize(settings.db_path)
    try:
        with SteamClient(api_key=settings.steam_api_key) as client:
            report = run_sync(
                conn, client, curated.games, options,
                review_language=settings.review_language,
                log=lambda message: print(message, flush=True),  # progress shows up live in log files too
            )
    except KeyboardInterrupt:
        print("Sync interrupted; the run is recorded as failed. Data saved so far is kept.", file=sys.stderr)
        return 130
    finally:
        conn.close()
    status = report.status()
    print(f"\nSync finished: {status}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    return EXIT_CODES[status]


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn  # imported here so the other commands do not need the web stack

    uvicorn.run("game_librarian.web:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def _status(args: argparse.Namespace, settings: Settings) -> int:
    conn = store.initialize(settings.db_path)
    try:
        counts = store.catalog_counts(conn)
        runs = store.recent_sync_runs(conn, limit=args.runs)
    finally:
        conn.close()
    print(f"Database: {settings.db_path}")
    print(f"Games ready to show: {counts['ready']} of {counts['curated']} curated")
    print(f"Apps in the discovery index (not enriched): {counts['discovered']}")
    if not runs:
        print("No sync has run yet.")
    for run in runs:
        problem = store.run_problem(run)
        print(f"#{run['id']} {run['started_at']} -> {run['finished_at'] or '...'} {run['status']}"
              + (f" | {problem}" if problem else ""))
    return 0


def _check_curated(args: argparse.Namespace, settings: Settings) -> int:
    curated = load_curated(settings.curated_dir)
    shown_awards = sum(len(awards) for awards in curated.awards_by_appid.values())
    shown_labels = sum(len(labels) for labels in curated.labels_by_appid.values())
    print(f"Curated folder: {settings.curated_dir}")
    print(f"games.csv: {len(curated.games)} games")
    print(f"awards.csv: {len(curated.awards)} rows, {shown_awards} verified and mapped to a Steam app")
    print(f"classifications.csv: {len(curated.classifications)} rows, {shown_labels} verified labels shown")
    for problem in curated.problems:
        print(f"problem: {problem}")
    print("No problems found." if not curated.problems else f"{len(curated.problems)} problem(s) found.")
    return 1 if curated.problems else 0


def _export_report(args: argparse.Namespace, settings: Settings) -> int:
    try:
        summary = export_report(settings, args.out)
    except PermissionError as exc:  # Windows refuses to replace a file another program has open
        print(f"error: could not replace an export file ({exc}). If Power BI Desktop is refreshing, "
              "wait until it finishes, then run export-report again.", file=sys.stderr)
        return 1
    rows = summary.rows
    print(f"Exported to {summary.out_dir}:")
    for name, count in rows.items():
        print(f"  {name}: {count} rows")
    print(f"Four-year ranking window in this export: {summary.window.start} to {summary.window.end}. "
          "It stays fixed in the report until export-report runs again.")
    if rows["games.csv"] == 0:
        print("warning: the catalog is empty. Run `python -m game_librarian sync` first.", file=sys.stderr)
    if REPORT_PARAMETERS_FILE.exists() and point_report_at(summary.out_dir, REPORT_PARAMETERS_FILE):
        print(f"Updated the report's DataFolder parameter in {REPORT_PARAMETERS_FILE}.")
    print("Next: open reporting/GameLibrarian.pbip in Power BI Desktop and select Refresh. "
          "A report published to the Power BI service does not change until it is refreshed or republished there.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface."""
    parser = argparse.ArgumentParser(prog="python -m game_librarian", description="Game Librarian commands.")
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="Fetch new and changed Steam data for the curated games.")
    sync.add_argument("--force", action="store_true", help="Refresh every selected game, even recent ones.")
    sync.add_argument("--appid", type=int, action="append", dest="appids", help="Only this curated app id (repeatable).")
    sync.add_argument("--limit", type=int, help="Only the first N curated games (for a quick test).")
    sync.add_argument("--skip-discovery", action="store_true", help="Do not call the catalog discovery API.")
    sync.add_argument("--skip-reviews", action="store_true", help="Do not refresh reviews.")
    sync.set_defaults(handler=_sync)

    serve = commands.add_parser("serve", help="Run the web app.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Restart when code changes (development).")
    serve.set_defaults(handler=_serve)

    status = commands.add_parser("status", help="Show catalog counts and recent sync runs.")
    status.add_argument("--runs", type=int, default=5, help="How many recent runs to list.")
    status.set_defaults(handler=_status)

    check = commands.add_parser("check-curated", help="Validate the files in curated/.")
    check.set_defaults(handler=_check_curated)

    export = commands.add_parser("export-report", help="Write the CSV tables the Power BI report reads.")
    export.add_argument("--out", type=Path, default=DEFAULT_REPORT_DATA_DIR,
                        help="Folder for the CSV files (default: reporting/data).")
    export.set_defaults(handler=_export_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Some Windows consoles cannot print every character in game titles; never crash on that.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    return args.handler(args, load_settings())


if __name__ == "__main__":
    sys.exit(main())
