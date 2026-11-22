"""Command line: python -m game_librarian
{sync,upcoming,most-watched,critics,sales-estimates,serve,status,check-curated,export-report,candidates,
init-postgres,migrate-to-postgres}."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from datetime import datetime, timezone

from . import candidates, critics, gamalytic, migrate, store, twitchtracker, upcoming
from .config import DEFAULT_REPORT_DATA_DIR, REPORT_PARAMETERS_FILE, Settings, load_settings
from .curated import PROMOTED, CuratedData, load_curated
from .database import DatabaseConfigError, PostgresDatabase, is_postgres_error, refresh_lock
from .gamalytic import GamalyticClient
from .igdb import IgdbClient, IgdbError
from .report_export import export_report, point_report_at
from .steam import SteamClient
from .stories import load_stories
from .sync import SyncOptions, run_sync
from .trivia import RESET_LABEL, load_trivia
from .twitch import KEEP_FOR as TWITCH_KEEP_FOR, TwitchClient
from .twitchtracker import TwitchTrackerClient
from .wikipedia import WikimediaClient, WikimediaError

EXIT_CODES = {"success": 0, "failed": 1, "partial": 2}
EXIT_BUSY = 3  # another refresh holds the database's refresh lock, so this one did nothing
IGDB_HELP = ("IGDB is optional and not configured. To use it, set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET "
             "(a Twitch application's credentials) in .env; see README.md, \"IGDB (optional)\".")
CONTACT_HELP = ("Critic scores come from Wikipedia, whose policy asks automated clients for contact information. "
                "Set GAME_LIBRARIAN_CONTACT in .env to an email address or website you choose; see README.md, "
                "\"What the critics said\".")
LOCAL_ONLY = ("a local evaluation: its terms allow no sharing, so its figures are kept only in the local SQLite "
              "database and shown only on the computer that runs the site, never with the PostgreSQL database or in "
              "a hosted process (see README.md, \"Data sources and terms\")")


def _worst(*codes: int) -> int:
    """The exit code of several steps: failed (1) over partial (2) over success (0)."""
    return 1 if 1 in codes else (2 if 2 in codes else 0)


def _locked(settings: Settings, work: Callable[[], int]) -> int:
    """Run a refreshing command's `work` while holding the refresh lock (database.refresh_lock). Two refreshes of the
    same PostgreSQL database, such as a scheduled sync and a manual one, never overlap: the second stops at once."""
    with refresh_lock(settings.database) as acquired:
        if not acquired:
            print("error: another refresh of this database is running (it holds the refresh lock), so this one did "
                  "nothing. Wait until it finishes, then run again.", file=sys.stderr)
            return EXIT_BUSY
        return work()


def _missing_for_hosted_sync(args: argparse.Namespace, settings: Settings) -> list[str]:
    """What a hosted sync lacks to refresh each source it was not told to skip. A hosted run fails instead of skipping
    a source quietly, so a missing secret shows as a failed run rather than as data that silently stops changing."""
    missing = []
    if not args.skip_discovery and not settings.steam_api_key:
        missing.append("STEAM_API_KEY (Steam catalog discovery; or pass --skip-discovery)")
    if not args.skip_upcoming and not settings.igdb_configured:
        missing.append("IGDB_CLIENT_ID and IGDB_CLIENT_SECRET (the cross-platform Trending lists; or pass "
                       "--skip-upcoming)")
    if not args.skip_critics and not settings.contact:
        missing.append("GAME_LIBRARIAN_CONTACT (critic scores from Wikipedia; or pass --skip-critics)")
    return missing


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
    missing = _missing_for_hosted_sync(args, settings) if settings.hosted else []
    if missing:
        print("error: this hosted sync stops before it starts, because it would skip these sources without their "
              "settings: " + "; ".join(missing) + ". Add them as secrets of the job (DEPLOYMENT.md).", file=sys.stderr)
        return 1
    return _locked(settings, lambda: _sync_steps(args, settings, curated))


def _sync_steps(args: argparse.Namespace, settings: Settings, curated: CuratedData) -> int:
    """sync's steps, under the refresh lock: Steam, then the Trending lists, Most watched, critic scores and sales
    estimates when each is configured, allowed for this database and due."""
    options = SyncOptions(
        force=args.force,
        appids=args.appids,
        limit=args.limit,
        skip_discovery=args.skip_discovery,
        skip_reviews=args.skip_reviews,
        skip_prices=args.skip_prices,
        skip_players=args.skip_players,
    )
    if not args.skip_prices:
        print(f"Prices are read from the Steam {settings.steam_country} store (GAME_LIBRARIAN_STEAM_COUNTRY).")
    conn = store.initialize(settings.database)
    try:
        with SteamClient(api_key=settings.steam_api_key) as client:
            report = run_sync(
                conn, client, curated.games, options,
                review_language=settings.review_language,
                country=settings.steam_country,
                log=lambda message: print(message, flush=True),  # progress shows up live in log files too
            )
    except KeyboardInterrupt:
        print("Sync interrupted; the run is recorded as failed. Data saved so far is kept.", file=sys.stderr)
        return 130
    finally:
        conn.close()
    status = report.status()
    print(f"\nSteam sync finished: {status}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    codes = [EXIT_CODES[status]]
    if args.skip_upcoming:
        print("\nTrending lists (IGDB, Twitch): skipped (--skip-upcoming).")
    elif not settings.igdb_configured:
        print("\nTrending lists (IGDB, Twitch): skipped, IGDB is not configured (see README.md, \"IGDB (optional)\").")
    else:
        codes.append(_run_upcoming(settings, curated, force=False))
    if args.skip_most_watched:
        print("\nMost watched (TwitchTracker): skipped (--skip-most-watched).")
    elif not settings.local_evaluation:
        print(f"\nMost watched (TwitchTracker): skipped, {LOCAL_ONLY}.")
    elif not settings.igdb_configured:
        print("\nMost watched (TwitchTracker): skipped, IGDB is not configured (its categories come from Twitch's top "
              "list, read with the same credentials).")
    else:
        codes.append(_run_most_watched(settings, curated, force=False))
    if args.skip_critics:
        print("\nCritic scores (Wikipedia): skipped (--skip-critics).")
    elif not settings.contact:
        print("\nCritic scores (Wikipedia): skipped, GAME_LIBRARIAN_CONTACT is not set (see README.md, "
              "\"What the critics said\").")
    else:
        codes.append(_run_critics(settings, curated, appids=args.appids, limit=args.limit, force=False))
    if args.skip_sales:
        print("\nSales estimates (Gamalytic): skipped (--skip-sales).")
    elif not settings.local_evaluation:
        print(f"\nSales estimates (Gamalytic): skipped, {LOCAL_ONLY}.")
    else:
        codes.append(_run_sales(settings, force=False))
    return _worst(*codes)


# ---------------------------------------------------------------------- upcoming (IGDB) and critics (Wikipedia)


def _run_upcoming(settings: Settings, curated: CuratedData, *, force: bool) -> int:
    """Refresh the cross-platform Trending lists (IGDB and Twitch) when due (or `force`), and print them."""
    conn = store.initialize(settings.database)
    try:
        if not force and not upcoming.refresh_due(conn, datetime.now(timezone.utc)):
            last = store.last_source_run(conn, upcoming.SOURCE)
            print(f"\nTrending lists (IGDB, Twitch): refreshed {last['started_at']}, less than "
                  f"{int(upcoming.REFRESH_AFTER.total_seconds() // 3600)} hours ago; nothing to do (--force refreshes now).")
            return 0
        print("\nTrending lists (IGDB, Twitch): refreshing...", flush=True)
        try:
            with IgdbClient(settings.igdb_client_id, settings.igdb_client_secret) as client, SteamClient() as steam, \
                    TwitchClient(settings.igdb_client_id, settings.igdb_client_secret) as twitch_client:
                report = upcoming.refresh_upcoming(conn, client, steam, curated,
                                                   catalog_appids=[game.appid for game in curated.games],
                                                   twitch_client=twitch_client,
                                                   log=lambda message: print(message, flush=True))
        except IgdbError as exc:  # messages never contain the credentials or the token
            print(f"error: {exc}", file=sys.stderr)
            return 1
        lists = {signal.key: store.latest_popularity(conn, signal.key) for signal in upcoming.SIGNALS}
        top_games = store.twitch_top_games(conn, "")
        games, _ = upcoming.twitch_games(conn, top_games, curated.twitch_category_decisions)
        # A reviewed series category has no IGDB record: its reviewed title stands in its place.
        watched = [record or curated.twitch_category_decisions[row["category_id"]].title for row, record, _ in games]
        rows = store.igdb_game_rows(conn)
    finally:
        conn.close()
    print(f"Trending lists (IGDB, Twitch): {report.status()}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    today = datetime.now(timezone.utc).date()

    def show(title: str, games: list) -> None:
        """Print a list's first LIST_SIZE entries: IGDB records, or a series category's name (a str)."""
        print(f"\n{title}:")
        for number, row in enumerate(games[:upcoming.LIST_SIZE], 1):
            if isinstance(row, str):
                print(f"  {number:>2}. {row} [series category: no single game]")
                continue
            available = upcoming.availability(upcoming.stored_dates(row), json.loads(row["platforms"] or "[]"), today)
            platforms = "; ".join([f"out {group.label} ({', '.join(group.platforms)})"
                                   + (f" [{group.first.status}]" if group.first.status != upcoming.FULL else "")
                                   for group in available.available]
                                  + [f"{group.label} ({', '.join(group.platforms)})" for group in available.announced])
            steam = f", Steam app {row['steam_appid']}" if row["steam_match"] == "verified" else (
                f", Steam {row['steam_match']}" if row["steam_match"] == "needs_review" else "")
            print(f"  {number:>2}. {row['name']} [igdb-{row['igdb_id']}{steam}]: {platforms or 'no platforms listed'}")

    for signal in upcoming.SIGNALS:
        listed = [rows[entry["igdb_id"]] for entry in lists[signal.key] if entry["igdb_id"] in rows]
        show(f"{signal.title} (upcoming)", [row for row in listed if upcoming.list_kind(row, today) == upcoming.UPCOMING])
        if signal.tracks_recent_releases:
            show(f"Most wanted new releases (first released in the last {upcoming.RECENT_WINDOW.days} days)",
                 [row for row in listed if upcoming.list_kind(row, today) == upcoming.RECENT])
    show(f"Most watched on Twitch ({top_games[0]['checked_at'] if top_games else 'no list from the last 24 hours'})",
         watched)
    return EXIT_CODES[report.status()]


def _upcoming(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.igdb_configured:
        print(IGDB_HELP, file=sys.stderr)
        return 1
    curated = load_curated(settings.curated_dir)
    for problem in curated.problems:
        print(f"warning: curated data: {problem}", file=sys.stderr)
    return _locked(settings, lambda: _run_upcoming(settings, curated, force=args.force))


def _run_most_watched(settings: Settings, curated: CuratedData, *, force: bool) -> int:
    """Ask TwitchTracker for the rank and average viewers of the tracked categories that are due (all with `force`),
    then print the list as the Trending page shows it on this computer (twitchtracker.ranking). The categories are the
    game and series categories of Twitch's newest top list (`upcoming` saves it) and the reviewed ones of
    curated/twitch_categories.csv; without a Twitch list from the last 24 hours, those chosen from the last one are
    asked about again."""
    decisions = curated.twitch_category_decisions
    conn = store.initialize(settings.database)
    try:
        moment = datetime.now(timezone.utc)
        chosen = twitchtracker.update_selection(conn, decisions, moment)
        selection = store.tracker_rows(conn)
        if not selection:
            print("\nMost watched (TwitchTracker): nothing to ask about. The categories come from Twitch's top list, "
                  "which `python -m game_librarian upcoming` saves (with IGDB_CLIENT_ID and IGDB_CLIENT_SECRET) and "
                  "which is kept for 24 hours, and from the game and series rows of curated/twitch_categories.csv.")
            return 0
        from_list = len(selection) - chosen["reviewed"]
        if chosen["selected_at"]:
            print(f"\nMost watched (TwitchTracker): {len(selection)} categories tracked, {from_list} game and series "
                  f"categories from Twitch's list of {chosen['selected_at']} and {chosen['reviewed']} reviewed outside "
                  "it.")
        else:
            print(f"\nMost watched (TwitchTracker): no Twitch list from the last 24 hours, so the {from_list} "
                  f"categories chosen from the last one are asked about again, with {chosen['reviewed']} reviewed "
                  "outside it.")
        due = twitchtracker.due_categories(conn, moment, force=force)
        if not due:
            print(f"Most watched (TwitchTracker): nothing is due; each category is asked again after "
                  f"{int(twitchtracker.REFRESH_AFTER.total_seconds() // 3600)} hours (--force asks now).")
            return 0
        print(f"Most watched (TwitchTracker): asking about {len(due)} of {len(selection)} categories, one request "
              f"each, {twitchtracker.MIN_INTERVAL_SECONDS:g} s apart (free API, no key)...", flush=True)
        with TwitchTrackerClient() as client:
            report = twitchtracker.refresh_summaries(conn, client, due, log=lambda message: print(message, flush=True))
        ranked, left_out = twitchtracker.ranking(conn, decisions)
    finally:
        conn.close()
    print(f"Most watched (TwitchTracker): {report.status()}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    print("\nMost watched among tracked Twitch categories, in TwitchTracker's rank order (places in this list; the "
          "rank counts every Twitch category; the period is not confirmed):")
    for place, entry in enumerate(ranked[:upcoming.LIST_SIZE], 1):
        what = f"igdb-{entry.record['igdb_id']}" if entry.record else "series category"
        print(f"  {place:>2}. {entry.name} [{what}]: TwitchTracker category rank #{entry.row['rank']}, "
              f"{entry.row['avg_viewers']:,} average viewers (retrieved {entry.row['retrieved_at']})")
    if left_out:
        print("Not listed: " + ", ".join(f"{count} {reason}" for reason, count in sorted(left_out.items())))
    print("Shown only to a browser on this computer: TwitchTracker publishes no terms that allow sharing its figures.")
    return EXIT_CODES[report.status()]


def _most_watched(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.local_evaluation:
        print(f"error: TwitchTracker's figures are {LOCAL_ONLY}.", file=sys.stderr)
        return 1
    curated = load_curated(settings.curated_dir)
    for problem in curated.problems:
        print(f"warning: curated data: {problem}", file=sys.stderr)
    return _locked(settings, lambda: _run_most_watched(settings, curated, force=args.force))


def _critic_targets(conn, curated: CuratedData) -> list[critics.CriticTarget]:
    """Every catalog game, then every released IGDB game that is not a catalog game."""
    targets = []
    visible = [row["appid"] for row in store.matching_games(conn, [], [])]
    catalog = store.game_rows(conn, visible)
    for appid in visible:
        row = catalog[appid]
        targets.append(critics.CriticTarget(str(appid), row["name"] or row["curated_title"], steam_appid=appid))
    today = datetime.now(timezone.utc).date()
    catalog_ids = set(catalog)
    linked = store.catalog_links_by_igdb(conn, catalog_ids)
    for igdb_id, row in sorted(store.igdb_game_rows(conn).items()):
        if upcoming.game_key_for(row, catalog_ids, linked).isdigit():
            continue  # the catalog game stands for it (the same rule as the website's links)
        state = upcoming.release_state(row["game_type"], row["game_status"], upcoming.stored_dates(row),
                                       row["first_release_date"], today)
        if not state.upcoming and row["slug"]:
            targets.append(critics.CriticTarget(f"igdb-{igdb_id}", row["name"], igdb_slug=row["slug"]))
    return targets


def _run_critics(settings: Settings, curated: CuratedData, *, appids: list[int] | None, limit: int | None,
                 force: bool) -> int:
    """Refresh the critic scores of the games that are due (or all with `force`), and print what happened."""
    conn = store.initialize(settings.database)
    try:
        targets = _critic_targets(conn, curated)
        if appids:
            wanted = {str(appid) for appid in appids}
            targets = [target for target in targets if target.key in wanted]
        due = critics.due_targets(conn, targets, datetime.now(timezone.utc), force=force)
        if limit is not None:
            due = due[:limit]
        if not due:
            print(f"\nCritic scores (Wikipedia): nothing is due; each game is read again after "
                  f"{critics.MAX_AGE.days} days (--force reads them now).")
            return 0
        print(f"\nCritic scores (Wikipedia): reading {len(due)} of {len(targets)} games...", flush=True)
        try:
            with WikimediaClient(settings.contact) as client:
                report = critics.refresh_critics(conn, client, due, curated,
                                                 log=lambda message: print(message, flush=True))
        except WikimediaError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        waiting = critics.review_rows(conn)
    finally:
        conn.close()
    print(f"Critic scores (Wikipedia): {report.status()}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    if waiting:
        print(f"{len(waiting)} item(s) wait for review: python -m game_librarian critics --review")
    return EXIT_CODES[report.status()]


def _critics(args: argparse.Namespace, settings: Settings) -> int:
    if args.review:
        conn = store.initialize(settings.database)
        try:
            rows = critics.review_rows(conn)
            articles = store.critic_article_rows(conn)
        finally:
            conn.close()
        for row in rows:
            title = articles[row["game"]]["article_title"] if row["game"] in articles else None
            print(f"{row['game']:<14} {row['what']:<28} {row['detail']}" + (f"  [article: {title}]" if title else ""))
        print(f"{len(rows)} item(s) wait for review. Record decisions in curated/critic_articles.csv "
              "(game, article, decision use|reject, checked_on, checked_by); see README.md.")
        return 0
    if not settings.contact:
        print(CONTACT_HELP, file=sys.stderr)
        return 1
    curated = load_curated(settings.curated_dir)
    for problem in curated.problems:
        print(f"warning: curated data: {problem}", file=sys.stderr)
    return _locked(settings, lambda: _run_critics(settings, curated, appids=args.appids, limit=args.limit,
                                                  force=args.force))


def _run_sales(settings: Settings, *, force: bool) -> int:
    """Collect Gamalytic's estimated Steam copies sold for the catalog games that are due (all with `force`)."""
    conn = store.initialize(settings.database)
    try:
        appids = [row["appid"] for row in store.matching_games(conn, [], [])]
        due = gamalytic.due_appids(conn, appids, datetime.now(timezone.utc), force=force)
        if not due:
            print(f"\nSales estimates (Gamalytic): nothing is due; each game is asked again after "
                  f"{gamalytic.MAX_AGE.days} days (--force asks now).")
            return 0
        print(f"\nSales estimates (Gamalytic): asking about {len(due)} of {len(appids)} catalog games, "
              f"{gamalytic.APPIDS_PER_REQUEST} per request (free API, no key)...", flush=True)
        with GamalyticClient() as client:
            report = gamalytic.refresh_estimates(conn, client, due, log=lambda message: print(message, flush=True))
    finally:
        conn.close()
    print(f"Sales estimates (Gamalytic): {report.status()}")
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    print("Shown only to a browser on this computer (Dashboard > Trending games): Gamalytic's terms do not allow "
          "sharing its data.")
    return EXIT_CODES[report.status()]


def _sales_estimates(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.local_evaluation:
        print(f"error: Gamalytic's estimates are {LOCAL_ONLY}.", file=sys.stderr)
        return 1
    return _locked(settings, lambda: _run_sales(settings, force=args.force))


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn  # imported here so the other commands do not need the web stack

    uvicorn.run("game_librarian.web:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def _status(args: argparse.Namespace, settings: Settings) -> int:
    conn = store.initialize(settings.database)
    try:
        counts = store.catalog_counts(conn)
        candidate_counts = store.candidate_counts(conn)
        runs = store.recent_sync_runs(conn, limit=args.runs)
        prices = store.price_summary(conn)
        snapshots = store.snapshot_summary(conn)
        upcoming_runs = store.recent_source_runs(conn, upcoming.SOURCE, limit=1)
        upcoming_run = upcoming_runs[0] if upcoming_runs else None
        igdb_games = len(store.igdb_game_rows(conn))
        popularity = store.popularity_summary(conn)
        critic_info = store.critic_summary(conn)
        sales = store.sales_estimate_summary(conn)
        twitch_cached = conn.execute("SELECT COUNT(*) FROM twitch_live").fetchone()[0]
        store.purge_twitch_top_games(conn, (datetime.now(timezone.utc) - TWITCH_KEEP_FOR).isoformat(timespec="seconds"))
        top_games = store.twitch_top_games(conn, "")
        tracker = store.tracker_rows(conn)
        tracker_runs = store.recent_source_runs(conn, twitchtracker.SOURCE, limit=1)
    finally:
        conn.close()
    database = settings.database
    print(f"Database: {database.describe() if isinstance(database, PostgresDatabase) else database}")
    print(f"Games ready to show: {counts['ready']} of {counts['curated']} catalog games")
    print(f"Apps in the discovery index (not enriched): {counts['discovered']}")
    if prices["games"]:
        print(f"Prices: {prices['games']} games, Steam store region {prices['regions']}, checked {prices['first']} to "
              f"{prices['last']}; {prices['discounted']} discounted when checked")
    else:
        print("Prices: none checked yet")
    for kind, label in (("players", "Player-count snapshots"), ("reviews", "Review snapshots")):
        info = snapshots[kind]
        print(f"{label}: " + (f"{info['games']} games, {info['days']} day(s) from {info['first']} to {info['last']}"
                               if info["games"] else "none yet"))
    if upcoming_run:
        print(f"Trending lists (IGDB, Twitch): last refresh {upcoming_run['started_at']} {upcoming_run['status']}; "
              f"{igdb_games} IGDB games saved" + "".join(
                  f"; {key}: {info['days']} day(s) of snapshots" for key, info in sorted(popularity.items())))
    else:
        print("Trending lists (IGDB, Twitch): " + ("never refreshed (python -m game_librarian upcoming)"
                                           if settings.igdb_configured else "IGDB not configured"))
    if critic_info["matches"]:
        print("Critic scores (Wikipedia): articles " + ", ".join(f"{count} {status}" for status, count in
                                                                 sorted(critic_info["matches"].items()))
              + f"; scores shown {critic_info['scores'].get('ok', 0)}, waiting for review "
              f"{critic_info['scores'].get('needs_review', 0)}; last check {critic_info['last']}")
    else:
        print("Critic scores (Wikipedia): " + ("none read yet (python -m game_librarian critics)" if settings.contact
                                               else "GAME_LIBRARIAN_CONTACT not set"))
    print("Sales estimates (Gamalytic, shown on this computer only): " + (
        f"{sales['games']} games with an estimate; {sales['collections']} collection(s) from {sales['first']} to "
        f"{sales['last']}" if sales["collections"]
        else "none collected yet (python -m game_librarian sales-estimates)"))
    print(f"Twitch live streams: {twitch_cached} game(s) in the cache (answers are deleted after 24 hours)")
    print("Twitch most-watched list: " + (f"{len(top_games)} categories, checked {top_games[0]['checked_at']} "
                                          "(deleted after 24 hours)" if top_games else "none from the last 24 hours"))
    answered = sorted(row["retrieved_at"] for row in tracker if row["rank"] is not None)
    last = tracker_runs[0] if tracker_runs else None
    print("Most watched (TwitchTracker, shown on this computer only): " + (
        f"{len(answered)} of {len(tracker)} tracked categories with figures"
        + (f", retrieved {answered[0]} to {answered[-1]}" if answered else "")
        + (f"; last run {last['started_at']} {last['status']}" if last else "")
        if tracker else "none collected yet (python -m game_librarian most-watched)"))
    if candidate_counts:
        print("Candidates: " + ", ".join(f"{count} {status}" for status, count in sorted(candidate_counts.items())))
    if not runs:
        print("No sync has run yet.")
    for run in runs:
        problem = store.run_problem(run)
        print(f"#{run['id']} {run['started_at']} -> {run['finished_at'] or '...'} {run['status']}"
              + (f" | {problem}" if problem else ""))
    return 0


# ---------------------------------------------------------------------- PostgreSQL (the hosted site's database)

NEEDS_POSTGRES = ("error: set GAME_LIBRARIAN_DATABASE_URL to the PostgreSQL database and PGPASSWORD to its password, "
                  "for this command only, with a role that may create tables (see DEPLOYMENT.md).")


def _init_postgres(args: argparse.Namespace, settings: Settings) -> int:
    """Create or upgrade the PostgreSQL tables, and let the runtime roles read and write their rows."""
    if not isinstance(settings.database, PostgresDatabase):
        print(NEEDS_POSTGRES, file=sys.stderr)
        return 1
    grant_to = tuple(args.grant_to or ())
    conn = store.connect(settings.database)
    try:
        schema = store.create_postgres_schema(conn, grant_to=grant_to)
        missing = store.missing_postgres_schema(conn)
    finally:
        conn.close()
    print(f"{settings.database.describe()}, schema {schema}: {len(store.schema_columns())} tables ready "
          "(existing tables and rows kept).")
    for role in grant_to:
        print(f"{role} may now read, add, change and delete the rows of every table, but not change or drop tables.")
    if missing:
        print(f"error: still missing: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


def _migrate_to_postgres(args: argparse.Namespace, settings: Settings) -> int:
    """Copy the SQLite catalog into the PostgreSQL database (migrate.py describes every step)."""
    if not isinstance(settings.database, PostgresDatabase):
        print(NEEDS_POSTGRES, file=sys.stderr)
        return 1
    source = args.source or settings.db_path
    print(f"{'Checking' if args.check else 'Copying'} the SQLite catalog {source} "
          f"{'against' if args.check else 'into'} {settings.database.describe()}...", flush=True)
    try:
        report = migrate.migrate(source, settings.database, replace=args.replace, check_only=args.check,
                                 log=lambda message: print(message, flush=True))
    except migrate.MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    rows = sum(item.source_rows for item in report.copied)
    if report.checked_only:
        occupied = {table: count for table, count in report.target_rows.items() if count}
        print(f"Check passed: {len(report.copied)} tables, {rows} rows would be copied. The PostgreSQL database "
              + (f"already holds catalog rows ({', '.join(f'{t}: {n}' for t, n in occupied.items())}), so the copy "
                 "needs --replace." if occupied else "holds no catalog rows yet.") + " Nothing was written.")
    else:
        print(f"Copied {len(report.copied)} tables, {rows} rows; every table's row count and checksum match the "
              "SQLite file." + (" The catalog tables were emptied first (--replace); accounts and wishlists were "
                                "kept." if report.replaced else ""))
        if report.purged_twitch_rows:
            print(f"Deleted {report.purged_twitch_rows} Twitch row(s) older than 24 hours (Twitch's caching limit).")
    for table, count in report.skipped.items():
        print(f"Not copied: {table} ({count} rows), {LOCAL_ONLY}.")
    print("The SQLite file was only read; it is unchanged.")
    return 0


def _check_curated(args: argparse.Namespace, settings: Settings) -> int:
    curated = load_curated(settings.curated_dir)
    story_collection = load_stories(settings.curated_dir)
    trivia_collection = load_trivia(settings.curated_dir)
    shown_awards = sum(len(awards) for awards in curated.awards_by_appid.values())
    shown_labels = sum(len(labels) for labels in curated.labels_by_appid.values())
    promoted = sum(1 for game in curated.games if game.catalog_source == PROMOTED)
    print(f"Curated folder: {settings.curated_dir}")
    print(f"games.csv: {len(curated.games) - promoted} games; promoted_games.csv: {promoted} games")
    print(f"awards.csv: {len(curated.awards)} rows, {shown_awards} verified and mapped to a Steam app")
    print(f"classifications.csv: {len(curated.classifications)} rows, {shown_labels} verified labels shown")
    print(f"publisher_aliases.csv: {len(curated.publisher_alias_rows)} rows, "
          f"{len(curated.publisher_aliases)} verified aliases used by the publisher filter")
    print(f"steam_matches.csv: {len(curated.steam_match_decisions)} reviewed Steam matches of IGDB games")
    print(f"critic_articles.csv: {len(curated.critic_article_decisions)} reviewed Wikipedia article choices")
    print(f"twitch_categories.csv: {len(curated.twitch_category_decisions)} reviewed Twitch category decisions")
    for story in story_collection.stories.values():
        print(f"stories/{story.slug}.json: {len(story.entries)} games ({len(story.main_entries)} main), "
              f"checked {story.checked_on}")
    if trivia_collection.quizzes:
        print(f"trivia.json: {len(trivia_collection.quizzes)} quizzes ({len(trivia_collection.questions)} questions), "
              f"one a day from {trivia_collection.start_day}, new quiz at {RESET_LABEL}")
    problems = curated.problems + story_collection.problems + trivia_collection.problems
    for problem in problems:
        print(f"problem: {problem}")
    print("No problems found." if not problems else f"{len(problems)} problem(s) found.")
    return 1 if problems else 0


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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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
    conn = store.initialize(settings.database)
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

    sync = commands.add_parser("sync", help="Fetch new and changed Steam data for the curated games, then refresh "
                                            "the upcoming lists, the Most watched ranking, critic scores and sales "
                                            "estimates when they are configured and due.")
    sync.add_argument("--force", action="store_true", help="Refresh every selected game, even recent ones.")
    sync.add_argument("--appid", type=int, action="append", dest="appids", help="Only this curated app id (repeatable).")
    sync.add_argument("--limit", type=int, help="Only the first N curated games (for a quick test).")
    sync.add_argument("--skip-discovery", action="store_true", help="Do not call the catalog discovery API.")
    sync.add_argument("--skip-reviews", action="store_true", help="Do not refresh reviews (and review snapshots).")
    sync.add_argument("--skip-prices", action="store_true", help="Do not refresh Steam prices.")
    sync.add_argument("--skip-players", action="store_true", help="Do not save player-count snapshots.")
    sync.add_argument("--skip-upcoming", action="store_true",
                      help="Do not refresh the cross-platform Trending lists from IGDB and Twitch (otherwise once a "
                           "day).")
    sync.add_argument("--skip-most-watched", action="store_true",
                      help="Do not ask TwitchTracker for the Most watched ranking (otherwise each category once in "
                           "20 hours).")
    sync.add_argument("--skip-critics", action="store_true",
                      help="Do not read critic scores from Wikipedia (otherwise each game once in 30 days).")
    sync.add_argument("--skip-sales", action="store_true",
                      help="Do not ask Gamalytic for estimated Steam copies sold (otherwise each game once in 7 days).")
    sync.set_defaults(handler=_sync)

    upcoming_parser = commands.add_parser("upcoming", help="Refresh the cross-platform Trending lists from IGDB and "
                                                           "Twitch.")
    upcoming_parser.add_argument("--force", action="store_true",
                                 help="Refresh now, even if the last refresh is less than 20 hours old.")
    upcoming_parser.set_defaults(handler=_upcoming)

    watched_parser = commands.add_parser("most-watched",
                                         help="Ask TwitchTracker's free API for the rank and average viewers of the "
                                              "game and series categories in Twitch's newest top list and the "
                                              "reviewed ones (shown on this computer only).")
    watched_parser.add_argument("--force", action="store_true",
                                help="Ask about every tracked category now, even those answered in the last 20 hours.")
    watched_parser.set_defaults(handler=_most_watched)

    critics_parser = commands.add_parser("critics", help="Read critic scores from English Wikipedia review tables.")
    critics_parser.add_argument("--appid", type=int, action="append", dest="appids",
                                help="Only this catalog app id (repeatable).")
    critics_parser.add_argument("--limit", type=int, help="At most this many games in this run.")
    critics_parser.add_argument("--force", action="store_true", help="Read the articles again even if they are recent.")
    critics_parser.add_argument("--review", action="store_true",
                                help="List the article matches and scores that wait for a person (no network).")
    critics_parser.set_defaults(handler=_critics)

    sales_parser = commands.add_parser("sales-estimates",
                                       help="Ask Gamalytic's free API for estimated Steam copies sold of the catalog "
                                            "games (shown on this computer only).")
    sales_parser.add_argument("--force", action="store_true",
                              help="Ask about every catalog game now, even those asked in the last 7 days.")
    sales_parser.set_defaults(handler=_sales_estimates)

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

    init_parser = commands.add_parser("init-postgres",
                                      help="Create or upgrade the tables of the PostgreSQL database "
                                           "(GAME_LIBRARIAN_DATABASE_URL), as a role that may create tables.")
    init_parser.add_argument("--grant-to", action="append", metavar="ROLE",
                             help="A role that may then read and write rows, but not change tables: the website's "
                                  "and the sync's (repeatable).")
    init_parser.set_defaults(handler=_init_postgres)

    migrate_parser = commands.add_parser("migrate-to-postgres",
                                         help="Copy the local SQLite catalog into the PostgreSQL database "
                                              "(GAME_LIBRARIAN_DATABASE_URL); the SQLite file is only read.")
    migrate_parser.add_argument("--from", dest="source", type=Path,
                                help="The SQLite file to copy (default: GAME_LIBRARIAN_DB or "
                                     "data/game_librarian.sqlite3).")
    migrate_parser.add_argument("--check", action="store_true",
                                help="Check the SQLite file and the PostgreSQL database, and write nothing.")
    migrate_parser.add_argument("--replace", action="store_true",
                                help="Empty the PostgreSQL catalog tables first, when they hold rows (accounts and "
                                     "wishlists are kept). Without it, a database with catalog rows is refused.")
    migrate_parser.set_defaults(handler=_migrate_to_postgres)

    _add_candidates_parser(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Some Windows consoles cannot print every character in game titles; never crash on that.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValueError as exc:  # SettingsError or DatabaseConfigError: they name the setting, never a secret
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        return args.handler(args, settings)
    except (store.SchemaError, DatabaseConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if not is_postgres_error(exc):
            raise
        # libpq's messages name the server, the database and the user, never the password.
        print(f"error: the PostgreSQL database could not be used: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
