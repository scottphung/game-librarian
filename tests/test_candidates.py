"""Catalog expansion: candidate sources, Steam screening rules, bounded and resumable batches, review and promotion."""

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from game_librarian import __main__ as cli
from game_librarian import candidates, store
from game_librarian.candidates import ELIGIBLE, ERROR, INELIGIBLE, NEEDS_REVIEW, PENDING, EligibilityRules
from game_librarian.curated import PROMOTED, load_curated
from game_librarian.store import CandidateFacts
from tests.conftest import store_summary, write_csv

TODAY = date(2026, 11, 15)
AWARD = {
    "organization": "The Steam Awards", "year": "2024", "category": "Game of the Year", "result": "nominee",
    "award_title": "Award Game", "steam_appid": "500", "steam_mapping": "same", "mapping_note": "",
    "source_url": "https://example.invalid/steamawards/2024", "review_status": "verified", "checked_on": "2026-11-15",
}


def facts(**overrides) -> CandidateFacts:
    values = {"steam_name": "Test Game", "steam_type": 0, "visible": True, "available": True, "review_count": 1_000,
              "percent_positive": 90, "released_on": "2024-01-01", "coming_soon": False, "content_descriptors": []}
    return CandidateFacts(**{**values, **overrides})


@pytest.fixture
def conn(settings):
    connection = store.initialize(settings.db_path)
    yield connection
    connection.close()


def fixed_now():
    return datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def index_apps(conn, apps):
    """Put (appid, name) pairs in the discovery index, as a keyed sync would."""
    store.upsert_discovered_apps(conn, [{"appid": appid, "name": name, "last_modified": 1} for appid, name in apps],
                                 now="2026-11-15T00:00:00+00:00")


def statuses(conn) -> dict[int, str]:
    return {row["appid"]: row["status"] for row in store.list_candidates(conn, limit=1000)}


# ---------------------------------------------------------------------- the inclusion rules


@pytest.mark.parametrize("overrides, status, phrase", [
    ({}, ELIGIBLE, "Steam game; 1,000 reviews, 90% positive; released 2024-01-01."),
    ({"visible": False}, INELIGIBLE, "does not show this app"),
    ({"steam_type": 4}, INELIGIBLE, "Steam lists it as DLC, not a game."),
    ({"steam_type": 1}, INELIGIBLE, "Steam lists it as a demo, not a game."),
    ({"steam_type": 6}, INELIGIBLE, "Steam store type 6, not a game."),  # software, video, music: never assumed a game
    ({"available": False}, INELIGIBLE, "does not sell it or offer it for free"),
    ({"coming_soon": True}, INELIGIBLE, "not released yet"),
    ({"released_on": "2026-12-01"}, INELIGIBLE, "not released yet"),
    ({"review_count": 49}, INELIGIBLE, "49 Steam reviews, fewer than the minimum of 50."),
    ({"percent_positive": 69}, INELIGIBLE, "69% of its Steam reviews are positive, below the minimum of 70%."),
    ({"content_descriptors": [1, 5]}, ELIGIBLE, "Steam game"),  # ordinary mature content is not flagged
    ({"content_descriptors": [3]}, NEEDS_REVIEW, "adult-only sexual content"),
    ({"review_count": 50, "percent_positive": 70}, ELIGIBLE, "50 reviews, 70% positive"),  # the minimums pass
])
def test_inclusion_rules(overrides, status, phrase):
    result, reason = candidates.judge(facts(**overrides), EligibilityRules(), TODAY)
    assert result == status and phrase in reason


def test_missing_store_data_is_ineligible_and_thresholds_are_configurable():
    assert candidates.judge(None, EligibilityRules(), TODAY)[0] == INELIGIBLE
    lenient = EligibilityRules(min_reviews=10, min_positive_percent=0)
    assert candidates.judge(facts(review_count=12, percent_positive=40), lenient, TODAY)[0] == ELIGIBLE
    strict = EligibilityRules(min_reviews=5_000)
    assert "minimum of 5,000" in candidates.judge(facts(), strict, TODAY)[1]


def test_store_item_facts_are_read_like_the_live_answers():
    item = {"appid": 7, "success": 1, **store_summary("Seven", reviews=1234, percent=93, released="2023-05-04",
                                                      descriptors=(1, 2, 5))}
    parsed = candidates.facts_from_store_item(item)
    assert (parsed.steam_name, parsed.steam_type, parsed.review_count, parsed.percent_positive) == ("Seven", 0, 1234, 93)
    assert (parsed.released_on, parsed.available, parsed.content_descriptors) == ("2023-05-04", True, [1, 2, 5])
    free = candidates.facts_from_store_item(store_summary("Free", free=True))
    delisted = candidates.facts_from_store_item(store_summary("Gone", purchasable=False))
    assert free.available is True and delisted.available is False


# ---------------------------------------------------------------------- sources and duplicates


def test_discovery_adds_bounded_batches_in_app_id_order_and_resumes(conn):
    index_apps(conn, [(appid, f"App {appid}") for appid in (50, 10, 40, 20, 30)])
    report = candidates.add_from_discovery(conn, limit=2, catalog_appids={20}, now=fixed_now)
    assert report.counts == {"new": 1, "skipped_in_catalog": 1}  # 10 added; 20 is a catalog game
    candidates.add_from_discovery(conn, limit=2, catalog_appids={20}, now=fixed_now)
    candidates.add_from_discovery(conn, limit=10, catalog_appids={20}, now=fixed_now)
    # Each run continued after the last one; nothing was added twice.
    assert sorted(statuses(conn)) == [10, 30, 40, 50]
    assert all(row["match_method"] == "steam_app" for row in store.list_candidates(conn))
    runs = store.recent_candidate_runs(conn)
    assert [run["command"] for run in runs] == ["add-discovery"] * 3 and runs[0]["status"] == "success"


def test_awards_add_only_verified_steam_mapped_results_outside_the_catalog(conn, settings):
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "100", "title": "Catalog Game"}])
    write_csv(settings.curated_dir / "classifications.csv", [{"steam_appid": "100", "label": "Indie",
              "review_status": "verified", "evidence_url": "https://example.invalid", "evidence_summary": "x"}])
    write_csv(settings.curated_dir / "awards.csv", [
        AWARD,
        {**AWARD, "steam_appid": "100", "award_title": "Catalog Game"},  # already in the catalog
        {**AWARD, "steam_appid": "", "steam_mapping": "none", "award_title": "Console Game", "mapping_note": "Not on Steam."},
        {**AWARD, "steam_appid": "600", "review_status": "needs_review"},  # an uncertain row is never used
        {**AWARD, "category": "Labor of Love"},  # a second award for app 500
    ])
    report = candidates.add_from_awards(conn, load_curated(settings.curated_dir), now=fixed_now)
    assert report.counts == {"new": 1, "merged": 1, "skipped_in_catalog": 1}
    row = store.candidate(conn, appid=500)
    assert row["match_method"] == "curated_award" and row["title"] == "Award Game"
    assert [source["detail"] for source in json.loads(row["sources"])] == [
        "The Steam Awards · Game of the Year", "The Steam Awards · Labor of Love"]


def test_a_game_found_by_several_sources_is_one_candidate(conn, settings):
    index_apps(conn, [(500, "Award Game")])
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "100", "title": "Catalog Game"}])
    write_csv(settings.curated_dir / "awards.csv", [AWARD])
    write_csv(settings.curated_dir / "classifications.csv", [{"steam_appid": "100", "label": "Indie",
              "review_status": "verified", "evidence_url": "https://example.invalid", "evidence_summary": "x"}])
    candidates.add_from_discovery(conn, limit=10, catalog_appids={100}, now=fixed_now)
    candidates.add_from_awards(conn, load_curated(settings.curated_dir), now=fixed_now)
    rows = store.list_candidates(conn)
    assert len(rows) == 1 and candidates.source_names(rows[0]) == "awards+discovery"


# ---------------------------------------------------------------------- screening


def add_discovered(conn, appids):
    index_apps(conn, [(appid, f"App {appid}") for appid in appids])
    candidates.add_from_discovery(conn, limit=len(appids), catalog_appids=set(), now=fixed_now)


def screen(conn, fake_steam, **options):
    with fake_steam.client() as client:
        return candidates.screen(conn, client, log=lambda message: None, now=fixed_now, **options)


def test_screening_uses_one_request_per_50_apps_and_saves_every_result(conn, fake_steam):
    add_discovered(conn, range(1, 121))
    for appid in range(1, 121):
        fake_steam.store_summaries[appid] = store_summary(f"App {appid}", reviews=10 if appid % 2 else 500)
    report = screen(conn, fake_steam, limit=500)
    assert fake_steam.count("GetItems") == 3  # 50 + 50 + 20 apps
    assert report.counts[ELIGIBLE] == 60 and report.counts[INELIGIBLE] == 60 and report.status() == "success"
    assert statuses(conn)[2] == ELIGIBLE and statuses(conn)[1] == INELIGIBLE
    assert "10 Steam reviews, fewer than the minimum" in store.candidate(conn, appid=1)["reason"]


def test_screened_candidates_are_not_requested_again_unless_a_recheck_is_asked(conn, fake_steam):
    add_discovered(conn, [1, 2])
    fake_steam.store_summaries[1] = store_summary("Few Reviews", reviews=10)
    fake_steam.store_summaries[2] = store_summary("Good Game")
    screen(conn, fake_steam, limit=10)
    fake_steam.requests.clear()
    assert screen(conn, fake_steam, limit=10).counts["due"] == 0 and fake_steam.requests == []
    fake_steam.store_summaries[1] = store_summary("Few Reviews", reviews=80)  # it has collected more reviews
    report = screen(conn, fake_steam, limit=10, recheck_days=0)
    assert report.counts["due"] == 2 and statuses(conn)[1] == ELIGIBLE


def test_an_interrupted_or_failing_run_keeps_finished_batches_and_retries_the_rest(conn, fake_steam):
    add_discovered(conn, range(1, 101))
    for appid in range(1, 101):
        fake_steam.store_summaries[appid] = store_summary(f"App {appid}")
    screen(conn, fake_steam, limit=50)  # a first, bounded run
    fake_steam.failing_paths.add("GetItems")
    report = screen(conn, fake_steam, limit=50)
    assert report.status() == "failed" and report.counts["errors"] == 50
    assert sorted(set(statuses(conn).values())) == [ELIGIBLE, ERROR]  # the first batch is kept
    assert store.candidate(conn, appid=51)["attempts"] == 1
    fake_steam.failing_paths.clear()
    report = screen(conn, fake_steam, limit=500)  # the next run picks up exactly the failed ones
    assert report.counts["due"] == 50 and set(statuses(conn).values()) == {ELIGIBLE}


def test_screening_stops_after_three_failed_requests_and_leaves_candidates_due(conn, fake_steam):
    add_discovered(conn, range(1, 251))  # five batches
    fake_steam.down = True
    report = screen(conn, fake_steam, limit=500)
    assert report.stopped_early and fake_steam.count("GetItems") == 3 * 4  # three requests, four attempts each
    assert report.counts["not_attempted"] == 100 and report.counts["errors"] == 150
    assert sum(1 for status in statuses(conn).values() if status == PENDING) == 100
    run = store.recent_candidate_runs(conn, limit=1)[0]
    assert run["command"] == "screen" and run["status"] == "failed"


def test_apps_steam_does_not_return_are_ineligible(conn, fake_steam):
    add_discovered(conn, [9])
    screen(conn, fake_steam, limit=10)  # no store summary for app 9: Steam answers success 15
    row = store.candidate(conn, appid=9)
    assert row["status"] == INELIGIBLE and "returned no store data" in row["reason"]


# ---------------------------------------------------------------------- review: promote, reject, match


@pytest.fixture
def curated_files(settings):
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "100", "title": "Catalog Game", "note": "Hand-picked."}])
    write_csv(settings.curated_dir / "awards.csv", [AWARD])
    write_csv(settings.curated_dir / "classifications.csv", [{"steam_appid": "100", "label": "Indie",
              "review_status": "verified", "evidence_url": "https://example.invalid", "evidence_summary": "x"}])
    return settings.curated_dir


def test_promotion_appends_to_promoted_games_and_never_touches_hand_picked_games(conn, fake_steam, curated_files):
    games_csv = hashlib.sha256((curated_files / "games.csv").read_bytes()).hexdigest()
    add_discovered(conn, [1, 2, 3])
    fake_steam.store_summaries.update({1: store_summary("Great Game", reviews=9_000, percent=95),
                                       2: store_summary("Flagged Game", descriptors=(3,)),
                                       3: store_summary("Unloved Game", percent=40)})
    screen(conn, fake_steam, limit=10)
    curated = load_curated(curated_files)
    promoted = candidates.promote(conn, curated_files, curated, [1, 2], now=fixed_now)  # 2 needs review: a person decided
    assert [row["appid"] for row in promoted] == [1, 2]
    data = load_curated(curated_files)
    assert data.problems == []
    assert [(game.appid, game.title, game.catalog_source) for game in data.games] == [
        (100, "Catalog Game", "hand-picked"), (1, "Great Game", PROMOTED), (2, "Flagged Game", PROMOTED)]
    assert "9,000 reviews, 95% positive" in data.games[1].note and "promoted 2026-11-15" in data.games[1].note
    assert hashlib.sha256((curated_files / "games.csv").read_bytes()).hexdigest() == games_csv
    assert statuses(conn)[1] == candidates.PROMOTED
    text = (curated_files / "promoted_games.csv").read_text(encoding="utf-8")
    assert text.startswith("steam_appid,title,source,match_method,evidence,promoted_on\n") and "\r" not in text


def test_promotion_refuses_unscreened_ineligible_unknown_or_catalog_games_and_writes_nothing(conn, fake_steam, curated_files):
    add_discovered(conn, [1, 3, 4])
    fake_steam.store_summaries.update({1: store_summary("Great Game"), 3: store_summary("Unloved Game", percent=40)})
    screen(conn, fake_steam, limit=2)  # app 4 stays pending
    curated = load_curated(curated_files)
    for appids, phrase in (([1, 3], "is ineligible"), ([4], "is pending"), ([999], "is not a candidate"),
                           ([100], "is not a candidate")):
        with pytest.raises(candidates.CandidateError, match=phrase):
            candidates.promote(conn, curated_files, curated, appids, now=fixed_now)
    assert not (curated_files / "promoted_games.csv").exists() and statuses(conn)[1] == ELIGIBLE


def test_rejected_candidates_keep_the_reviewers_reason(conn, fake_steam):
    add_discovered(conn, [1])
    row = store.candidate(conn, appid=1)
    candidates.reject(conn, row, "Servers shut down.", now=fixed_now)
    assert store.candidate(conn, appid=1)["reason"] == "Rejected by a reviewer: Servers shut down."
    assert screen(conn, fake_steam, limit=10).counts["due"] == 0  # a decision is not re-screened


def test_title_only_candidates_wait_for_a_reviewer_to_confirm_the_steam_app(conn, fake_steam):
    store.add_candidate(conn, igdb_id=77, title="Echo Quest", source={"source": "igdb", "ref": "igdb:77"},
                        match_method="title_only", status=NEEDS_REVIEW, reason="Matched by title only.",
                        suggested_appids=[700, 701], now="2026-11-15T00:00:00+00:00")
    assert screen(conn, fake_steam, limit=10).counts["due"] == 0  # never screened without a Steam app
    row = store.candidate(conn, igdb_id=77)
    with pytest.raises(candidates.CandidateError, match="already in the catalog"):
        candidates.match(conn, row, 100, {100}, now=fixed_now)
    candidates.match(conn, row, 701, set(), now=fixed_now)
    fake_steam.store_summaries[701] = store_summary("Echo Quest")
    screen(conn, fake_steam, limit=10)
    matched = store.candidate(conn, appid=701)
    assert (matched["match_method"], matched["status"]) == ("manual", ELIGIBLE)


# ---------------------------------------------------------------------- the command line, end to end


def test_the_pilot_workflow_from_the_readme(settings, fake_steam, curated_files, monkeypatch, capsys):
    """candidates add -> screen -> list -> promote, then sync includes the promoted game."""
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    fake_steam.store_summaries[500] = store_summary("Award Game", reviews=4_000, percent=88)
    assert cli.main(["candidates", "add", "--source", "awards"]) == 0
    assert cli.main(["candidates", "add", "--source", "discovery"]) == 1  # no discovery index yet: explained
    assert "discovery index is empty" in capsys.readouterr().err
    assert cli.main(["candidates", "screen", "--limit", "10"]) == 0
    assert cli.main(["candidates", "list", "--status", "eligible"]) == 0
    listing = capsys.readouterr().out
    assert "app 500" in listing and "4,000 reviews, 88%" in listing and "[awards]" in listing
    assert cli.main(["candidates", "promote", "--all-eligible"]) == 0
    assert "Promoted 1 game" in capsys.readouterr().out
    assert cli.main(["candidates", "promote", "--appid", "500"]) == 1  # already promoted: refused, explained
    assert "already in the catalog" in capsys.readouterr().err

    fake_steam.add_game(100, "Catalog Game")
    fake_steam.add_game(500, "Award Game")
    assert cli.main(["sync", "--skip-discovery"]) == 0
    conn = store.connect(settings.db_path)
    try:
        assert [row["appid"] for row in store.matching_games(conn, [], [])] == [500, 100]  # A-Z: Award, Catalog
    finally:
        conn.close()
