"""Curated CSV files: the shipped data is valid, and bad rows are reported, not shown."""

import os
import time
from collections import Counter

from game_librarian.config import DEFAULT_CURATED_DIR
from game_librarian.curated import HAND_PICKED, PROMOTED, CuratedCache, load_curated
from tests.conftest import write_csv

AWARD_ROW = {
    "organization": "The Game Awards", "year": "2023", "category": "Game of the Year", "result": "winner",
    "award_title": "Test Quest", "steam_appid": "10", "steam_mapping": "same", "mapping_note": "",
    "source_url": "https://example.invalid/winners", "review_status": "verified", "checked_on": "2026-11-09",
}
LABEL_ROW = {
    "steam_appid": "10", "title": "Test Quest", "label": "AAA", "review_status": "verified",
    "evidence_url": "https://example.invalid/budget", "evidence_summary": "Reported budget of $200 million.",
    "checked_on": "2026-11-09",
}


def test_shipped_curated_files_are_valid_and_consistent():
    data = load_curated(DEFAULT_CURATED_DIR)
    assert data.problems == []
    hand_picked = [game for game in data.games if game.catalog_source == HAND_PICKED]
    assert len(hand_picked) == 160  # the original list is kept; promoted games are listed separately
    curated_ids = {game.appid for game in data.games}
    for award in data.awards:
        assert award.source_url.startswith("https://")
        assert award.review_status == "verified"
        if award.mapping == "none":
            assert award.appid is None and award.mapping_note
        # Every Game of the Year entry of The Game Awards sold on Steam is in the catalog (the Elden Ring DLC is
        # left out on purpose). Other awards' entries can wait in the candidate queue, which is allowed.
        elif (award.organization, award.category) == ("The Game Awards", "Game of the Year") and award.appid != 2778580:
            assert award.appid in curated_ids, award
    tga_goty = [a for a in data.awards if (a.organization, a.category) == ("The Game Awards", "Game of the Year")]
    assert len(tga_goty) == 68 and sum(a.result == "winner" for a in tga_goty) == 12  # 2014-2025
    assert set(data.labels_for(1903340)) == {"AA", "Indie"}  # a game can be both
    assert data.labels_for(1868140) == {}  # rejected Indie evidence is not shown


def test_shipped_awards_cover_the_documented_organizations_years_and_categories():
    """The coverage README.md documents, and one winner for every organization, category and year."""
    awards = load_curated(DEFAULT_CURATED_DIR).awards
    coverage = {}
    for award in awards:
        coverage.setdefault((award.organization, award.category), set()).add(award.year)
    assert coverage == {
        ("The Game Awards", "Game of the Year"): set(range(2014, 2026)),
        ("The Game Awards", "Best Independent Game"): set(range(2014, 2020)) | {2023, 2024, 2025},
        ("The Game Awards", "Best Indie Game"): {2020, 2021, 2022},  # the name the sources use for those years
        ("BAFTA Games Awards", "Best Game"): set(range(2019, 2027)),
        ("Game Developers Choice Awards", "Game of the Year"): set(range(2019, 2027)),
        ("The Steam Awards", "Game of the Year"): set(range(2018, 2026)),
        ("The Steam Awards", "Labor of Love"): set(range(2018, 2026)),
        ("The Steam Awards", "Better With Friends"): set(range(2018, 2026)),
    }
    winners = Counter((a.organization, a.category, a.year) for a in awards if a.result == "winner")
    assert all(winners[(a.organization, a.category, a.year)] == 1 for a in awards)
    entries = Counter((a.organization, a.category, a.year, a.appid or a.award_title) for a in awards)
    assert max(entries.values()) == 1  # no game is listed twice in one category and year
    steam_awards = [a for a in awards if a.organization == "The Steam Awards"]
    assert len(steam_awards) == 120 and all(a.mapping in ("same", "none") for a in steam_awards)  # exact app ids
    # Awards to an app that now holds a different game are kept, but not attached to it.
    csgo = [a for a in steam_awards if a.award_title == "Counter-Strike: Global Offensive"]
    assert csgo and all(a.appid is None and "Counter-Strike 2" in a.mapping_note for a in csgo)


def test_award_names_include_the_organization_and_top_awards_are_known():
    awards = {(a.organization, a.category): a for a in load_curated(DEFAULT_CURATED_DIR).awards}
    assert awards[("BAFTA Games Awards", "Best Game")].award_name == "BAFTA Games Awards · Best Game"
    top = {key for key, award in awards.items() if award.is_top_award}
    assert top == {("The Game Awards", "Game of the Year"), ("BAFTA Games Awards", "Best Game"),
                   ("Game Developers Choice Awards", "Game of the Year"), ("The Steam Awards", "Game of the Year")}


def test_invalid_rows_are_reported_with_line_numbers_and_skipped(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}, {"steam_appid": "abc", "title": "Bad"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW, {**AWARD_ROW, "result": "champion", "source_url": "ftp://x"}])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW, {**LABEL_ROW, "label": "AAAA"}])
    data = load_curated(tmp_path)
    assert [game.appid for game in data.games] == [10]
    assert len(data.awards) == 1 and len(data.classifications) == 1
    assert any(p.startswith("games.csv:3:") for p in data.problems)
    assert any(p.startswith("awards.csv:3:") and "result" in p and "source_url" in p for p in data.problems)
    assert any(p.startswith("classifications.csv:3:") for p in data.problems)


def test_only_verified_rows_are_displayed(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW, {**AWARD_ROW, "year": "2022", "review_status": "needs_review"}])
    write_csv(tmp_path / "classifications.csv", [
        {**LABEL_ROW, "review_status": "needs_review"},
        {**LABEL_ROW, "label": "Indie", "review_status": "rejected"},
    ])
    data = load_curated(tmp_path)
    assert [award.year for award in data.awards_for(10)] == [2023]
    assert data.labels_for(10) == {}


def test_a_repeated_verified_award_result_is_reported_and_counted_once(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [
        AWARD_ROW,  # line 2
        {**AWARD_ROW, "source_url": "https://example.invalid/another-source"},  # line 3: the same result again
        {**AWARD_ROW, "organization": "the game  awards", "category": "GAME OF THE YEAR"},  # line 4: other spelling
        {**AWARD_ROW, "review_status": "needs_review"},  # line 5: not verified, so never shown or counted anyway
        {**AWARD_ROW, "result": "nominee"},  # line 6: another result
        {**AWARD_ROW, "steam_appid": "", "steam_mapping": "none", "award_title": "Console Game"},  # line 7
        {**AWARD_ROW, "steam_appid": "", "steam_mapping": "none", "award_title": "Console Game"},  # line 8: repeat
        {**AWARD_ROW, "steam_appid": "", "steam_mapping": "none", "award_title": "Other Console Game"},  # line 9
    ])
    data = load_curated(tmp_path)
    assert [problem for problem in data.problems if problem.startswith("awards.csv")] == [
        "awards.csv:3: repeats the result on line 2 (same organization, year, category, result and game); "
        "it is counted once",
        "awards.csv:4: repeats the result on line 2 (same organization, year, category, result and game); "
        "it is counted once",
        "awards.csv:8: repeats the result on line 7 (same organization, year, category, result and game); "
        "it is counted once",
    ]
    assert [(award.result, award.source_url) for award in data.awards_for(10)] == [
        ("winner", "https://example.invalid/winners"), ("nominee", "https://example.invalid/winners")]
    assert [award.award_title for award in data.awards if award.appid is None] == ["Console Game", "Other Console Game"]


def test_a_game_verified_as_both_aaa_and_aa_shows_neither(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW, {**LABEL_ROW, "label": "AA"}, {**LABEL_ROW, "label": "Indie"}])
    data = load_curated(tmp_path)
    assert set(data.labels_for(10)) == {"Indie"}
    assert any("both AAA and AA" in problem for problem in data.problems)


def test_promoted_games_join_the_catalog_with_their_source(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW])
    promoted = {"steam_appid": "20", "title": "Promoted Game", "source": "discovery", "match_method": "steam_app",
                "evidence": "Steam: game; 1,234 reviews, 95% positive.", "promoted_on": "2026-11-15"}
    write_csv(tmp_path / "promoted_games.csv", [
        promoted,
        {**promoted, "steam_appid": "10", "title": "Already Hand-Picked"},  # games.csv wins
        {**promoted, "steam_appid": "20"},  # listed twice
        {**promoted, "steam_appid": "30", "promoted_on": "15/11/2026"},  # not an ISO date
    ])
    data = load_curated(tmp_path)
    assert [(game.appid, game.catalog_source) for game in data.games] == [(10, HAND_PICKED), (20, PROMOTED)]
    assert data.games[1].note == "Steam: game; 1,234 reviews, 95% positive."
    assert data.catalog_source(20) == PROMOTED and data.catalog_source(99) is None
    assert any(p.startswith("promoted_games.csv:3:") and "already in games.csv" in p for p in data.problems)
    assert any(p.startswith("promoted_games.csv:4:") and "duplicate" in p for p in data.problems)
    assert any(p.startswith("promoted_games.csv:5:") and "promoted_on" in p for p in data.problems)


def test_a_missing_promoted_list_is_not_a_problem(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW])
    data = load_curated(tmp_path)
    assert data.problems == [] and [game.appid for game in data.games] == [10]


def test_awards_list_top_award_wins_first_then_other_wins_then_nominations(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW])
    write_csv(tmp_path / "awards.csv", [
        {**AWARD_ROW, "organization": "The Steam Awards", "category": "Labor of Love", "year": "2025"},
        {**AWARD_ROW, "organization": "BAFTA Games Awards", "category": "Best Game", "year": "2024", "result": "nominee"},
        {**AWARD_ROW, "organization": "BAFTA Games Awards", "category": "Best Game", "year": "2023"},
        {**AWARD_ROW, "year": "2023", "category": "Best Independent Game"},
    ])
    order = [(a.organization, a.category, a.year) for a in load_curated(tmp_path).awards_for(10)]
    assert order == [
        ("BAFTA Games Awards", "Best Game", 2023),  # a top-award win comes first
        ("The Steam Awards", "Labor of Love", 2025),  # then other wins, newest first
        ("The Game Awards", "Best Independent Game", 2023),
        ("BAFTA Games Awards", "Best Game", 2024),  # nominations last
    ]


def test_missing_files_do_not_crash(tmp_path):
    data = load_curated(tmp_path)
    assert data.games == [] and data.awards_for(10) == []
    assert len(data.problems) == 3


def test_cache_reloads_when_a_file_changes(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW])
    cache = CuratedCache(tmp_path)
    assert set(cache.get().labels_for(10)) == {"AAA"}
    write_csv(tmp_path / "classifications.csv", [{**LABEL_ROW, "label": "AA"}])
    later = time.time() + 5  # make sure the modification time differs on coarse file systems
    os.utime(tmp_path / "classifications.csv", (later, later))
    assert set(cache.get().labels_for(10)) == {"AA"}
