"""Command line: python -m game_librarian {sync,serve,status,check-curated,export-report,candidates}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import candidates, store
from .config import DEFAULT_REPORT_DATA_DIR, REPORT_PARAMETERS_FILE, Settings, load_settings
from .curated import PROMOTED, load_curated
from .igdb import IgdbClient, IgdbError
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
            print(f"warning: not in curated/games.csv or promoted_games.csv, skipped: {unknown}", file=sys.stderr)

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
        candidate_counts = store.candidate_counts(conn)
        runs = store.recent_sync_runs(conn, limit=args.runs)
    finally:
        conn.close()
    print(f"Database: {settings.db_path}")
    print(f"Games ready to show: {counts['ready']} of {counts['curated']} catalog games")
    print(f"Apps in the discovery index (not enriched): {counts['discovered']}")
    if candidate_counts:
        print("Candidates: " + ", ".join(f"{count} {status}" for status, count in sorted(candidate_counts.items())))
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
    promoted = sum(1 for game in curated.games if game.catalog_source == PROMOTED)
    print(f"Curated folder: {settings.curated_dir}")
    print(f"games.csv: {len(curated.games) - promoted} games; promoted_games.csv: {promoted} games")
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


# ---------------------------------------------------------------------- candidates (catalog expansion)


def _candidate_line(row) -> str:
    """One line per candidate: id, Steam app, status, reviews, title, sources and the reason."""
    reviews = (f"{row['review_count']:,} reviews, {row['percent_positive']}%" if row["screened_at"]
               and row["review_count"] is not None else "not screened")
    app = f"app {row['appid']}" if row["appid"] else "no Steam app"
    return (f"#{row['id']:<6} {app:<14} {row['status']:<12} {reviews:<24} {row['steam_name'] or row['title']}"
            f"  [{candidates.source_names(row)}]" + (f"\n         {row['reason']}" if row["reason"] else ""))


def _print_run(report: candidates.RunReport) -> None:
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))


def _candidates_add(args: argparse.Namespace, settings: Settings) -> int:
    curated = load_curated(settings.curated_dir)
    catalog_appids = {game.appid for game in curated.games}
    conn = store.initialize(settings.db_path)
    try:
        if args.source == "discovery":
            if store.catalog_counts(conn)["discovered"] == 0:
                print("The discovery index is empty: run `python -m game_librarian sync` with STEAM_API_KEY set first.",
                      file=sys.stderr)
                return 1
            report = candidates.add_from_discovery(conn, limit=args.limit, catalog_appids=catalog_appids)
        elif args.source == "awards":
            report = candidates.add_from_awards(conn, curated)
        else:
            if not settings.igdb_configured:
                print("IGDB is optional and not configured. To use it, set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET "
                      "(a Twitch application's credentials) in .env; see README.md, \"IGDB (optional)\".",
                      file=sys.stderr)
                return 1
            try:
                with IgdbClient(settings.igdb_client_id, settings.igdb_client_secret) as client:
                    report = candidates.add_from_igdb(conn, client, limit=args.limit, offset=args.offset,
                                                      catalog_appids=catalog_appids)
            except IgdbError as exc:  # messages never contain the credentials or the token
                print(f"error: {exc}", file=sys.stderr)
                return 1
    finally:
        conn.close()
    print(f"Added candidates from {args.source}: {report.status()}")
    _print_run(report)
    print("Next: python -m game_librarian candidates screen --limit 50")
    return EXIT_CODES[report.status()]


def _candidates_screen(args: argparse.Namespace, settings: Settings) -> int:
    rules = candidates.EligibilityRules(min_reviews=args.min_reviews, min_positive_percent=args.min_positive)
    conn = store.initialize(settings.db_path)
    try:
        with SteamClient() as client:  # GetItems needs no key; the key is never sent with it
            report = candidates.screen(conn, client, limit=args.limit, rules=rules, recheck_days=args.recheck_days,
                                       log=lambda message: print(message, flush=True))
        counts = store.candidate_counts(conn)
    except KeyboardInterrupt:
        print("Screening interrupted. Every finished batch is saved; run the command again to continue.",
              file=sys.stderr)
        return 130
    finally:
        conn.close()
    print(f"\nScreening finished: {report.status()} (rules: at least {rules.min_reviews} Steam reviews and "
          f"{rules.min_positive_percent}% positive)")
    _print_run(report)
    print("Candidates now: " + ", ".join(f"{count} {status}" for status, count in sorted(counts.items())))
    print("Next: python -m game_librarian candidates list --status eligible")
    return EXIT_CODES[report.status()]


def _candidates_list(args: argparse.Namespace, settings: Settings) -> int:
    conn = store.initialize(settings.db_path)
    try:
        counts = store.candidate_counts(conn)
        rows = store.list_candidates(conn, status=args.status, source=args.source, limit=args.limit)
        runs = store.recent_candidate_runs(conn, limit=3)
    finally:
        conn.close()
    if not counts:
        print("No candidates yet. Start with: python -m game_librarian candidates add --source awards")
        return 0
    print("Candidates: " + ", ".join(f"{counts[status]} {status}" for status in candidates.STATUSES if status in counts))
    for run in runs:
        print(f"  last runs: #{run['id']} {run['command']} {run['started_at']} {run['status']}")
    print(f"\nShowing {len(rows)}" + (f" {args.status}" if args.status else "")
          + (f" from {args.source}" if args.source else "") + " (most Steam reviews first):")
    for row in rows:
        print(_candidate_line(row))
    return 0


def _find_candidate(conn, args: argparse.Namespace):
    row = store.candidate(conn, appid=args.appid, candidate_id=args.id)
    if row is None:
        print(f"error: no candidate with {'Steam app ' + str(args.appid) if args.appid else 'id ' + str(args.id)}.",
              file=sys.stderr)
    return row


def _candidates_show(args: argparse.Namespace, settings: Settings) -> int:
    conn = store.initialize(settings.db_path)
    try:
        row = _find_candidate(conn, args)
    finally:
        conn.close()
    if row is None:
        return 1
    print(_candidate_line(row))
    print(f"  match method: {row['match_method']}" + (f"; IGDB id {row['igdb_id']}" if row["igdb_id"] else ""))
    for source in json.loads(row["sources"]):
        print(f"  source: {source['source']}: {source.get('detail') or source.get('ref')}")
    if row["suggested_appids"]:
        print(f"  Steam apps to check: {', '.join(map(str, json.loads(row['suggested_appids'])))}")
    if row["screened_at"]:
        print(f"  screened {row['screened_at']}: type {row['steam_type']}, visible {bool(row['visible'])}, "
              f"available {bool(row['available'])}, released {row['released_on'] or 'unknown'}, "
              f"content descriptors {row['content_descriptors']}")
    if row["last_error"]:
        print(f"  last error ({row['attempts']} in a row): {row['last_error']}")
    if row["appid"]:
        print(f"  Steam page: https://store.steampowered.com/app/{row['appid']}/")
    return 0


def _candidates_promote(args: argparse.Namespace, settings: Settings) -> int:
    curated = load_curated(settings.curated_dir)
    conn = store.initialize(settings.db_path)
    try:
        appids = list(args.appids or []) or candidates.eligible_appids(conn, args.limit)
        if not appids:
            print("Nothing to promote: no eligible candidates.")
            return 0
        rows = candidates.promote(conn, settings.curated_dir, curated, appids)
    except candidates.CandidateError as exc:
        print(f"error: {exc} Nothing was promoted.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Promoted {len(rows)} {'game' if len(rows) == 1 else 'games'} to curated/promoted_games.csv:")
    for row in rows:
        print(f"  {row['appid']}  {row['steam_name'] or row['title']}")
    print("Next: python -m game_librarian sync   (fetches their details, tags, media and reviews)")
    return 0


def _candidates_reject(args: argparse.Namespace, settings: Settings) -> int:
    conn = store.initialize(settings.db_path)
    try:
        row = _find_candidate(conn, args)
        if row is None:
            return 1
        candidates.reject(conn, row, args.reason)
    except candidates.CandidateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Rejected candidate #{row['id']} ({row['steam_name'] or row['title']}).")
    return 0


def _candidates_match(args: argparse.Namespace, settings: Settings) -> int:
    catalog_appids = {game.appid for game in load_curated(settings.curated_dir).games}
    conn = store.initialize(settings.db_path)
    try:
        row = store.candidate(conn, candidate_id=args.id)
        if row is None:
            print(f"error: no candidate with id {args.id}.", file=sys.stderr)
            return 1
        candidates.match(conn, row, args.appid, catalog_appids)
    except candidates.CandidateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Candidate #{args.id} is now matched to Steam app {args.appid}. Next: python -m game_librarian candidates screen")
    return 0


def _add_candidate_selector(parser: argparse.ArgumentParser) -> None:
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--appid", type=int, help="The candidate's Steam app id.")
    which.add_argument("--id", type=int, help="The candidate id (shown as #id by `candidates list`).")


def _add_candidates_parser(commands) -> None:
    parser = commands.add_parser("candidates", help="Grow the catalog: add, screen, review and promote candidate games.")
    actions = parser.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="Collect candidates from one source.")
    add.add_argument("--source", choices=candidates.SOURCES, required=True)
    add.add_argument("--limit", type=int, default=500,
                     help="How many discovery apps (default 500) or IGDB games (use 100 or fewer) to add.")
    add.add_argument("--offset", type=int, default=0, help="IGDB only: skip this many results (to continue a list).")
    add.set_defaults(handler=_candidates_add)

    screen = actions.add_parser("screen", help="Check due candidates against Steam, 50 apps per request.")
    screen.add_argument("--limit", type=int, default=200, help="At most this many candidates (default 200).")
    screen.add_argument("--min-reviews", type=int, default=candidates.EligibilityRules.min_reviews,
                        help="Minimum number of Steam reviews (default 50).")
    screen.add_argument("--min-positive", type=int, default=candidates.EligibilityRules.min_positive_percent,
                        help="Minimum share of positive Steam reviews, in percent (default 70).")
    screen.add_argument("--recheck-days", type=int,
                        help="Also screen again eligible and ineligible candidates screened this many days ago or more.")
    screen.set_defaults(handler=_candidates_screen)

    listing = actions.add_parser("list", help="Show candidates and the reason for their status.")
    listing.add_argument("--status", choices=candidates.STATUSES)
    listing.add_argument("--source", choices=candidates.SOURCES)
    listing.add_argument("--limit", type=int, default=25)
    listing.set_defaults(handler=_candidates_list)

    show = actions.add_parser("show", help="Show everything about one candidate.")
    _add_candidate_selector(show)
    show.set_defaults(handler=_candidates_show)

    promote = actions.add_parser("promote", help="Add screened candidates to curated/promoted_games.csv.")
    which = promote.add_mutually_exclusive_group(required=True)
    which.add_argument("--appid", type=int, action="append", dest="appids", help="A Steam app id (repeatable).")
    which.add_argument("--all-eligible", action="store_true", help="Every eligible candidate, most reviews first.")
    promote.add_argument("--limit", type=int, default=20, help="With --all-eligible: at most this many (default 20).")
    promote.set_defaults(handler=_candidates_promote)

    reject = actions.add_parser("reject", help="Record that a candidate should not be added.")
    _add_candidate_selector(reject)
    reject.add_argument("--reason", required=True)
    reject.set_defaults(handler=_candidates_reject)

    match = actions.add_parser("match", help="Confirm the Steam app of a title-only candidate.")
    match.add_argument("--id", type=int, required=True, help="The candidate id.")
    match.add_argument("--appid", type=int, required=True, help="The Steam app id you checked.")
    match.set_defaults(handler=_candidates_match)


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

    _add_candidates_parser(commands)
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
