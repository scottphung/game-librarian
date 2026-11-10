"""Curated CSV files: the shipped data is valid, and bad rows are reported, not shown."""

import os
import time

from game_librarian.config import DEFAULT_CURATED_DIR
from game_librarian.curated import CuratedCache, load_curated
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
    assert 100 <= len(data.games) <= 200
    curated_ids = {game.appid for game in data.games}
    for award in data.awards:
        assert award.source_url.startswith("https://")
        assert award.review_status == "verified"
        if award.mapping == "none":
            assert award.appid is None and award.mapping_note
        # Every Game of the Year entry sold on Steam is in the catalog (the Elden Ring DLC is left out
        # on purpose). Some independent-game nominees are mapped but not listed, which is allowed.
        elif award.category == "Game of the Year" and award.appid != 2778580:
            assert award.appid in curated_ids, award
    goty = [a for a in data.awards if a.category == "Game of the Year"]
    assert len(goty) == 68 and sum(a.result == "winner" for a in goty) == 12  # 2014-2025
    assert set(data.labels_for(1903340)) == {"AA", "Indie"}  # a game can be both
    assert data.labels_for(1868140) == {}  # rejected Indie evidence is not shown


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


def test_a_game_verified_as_both_aaa_and_aa_shows_neither(tmp_path):
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [AWARD_ROW])
    write_csv(tmp_path / "classifications.csv", [LABEL_ROW, {**LABEL_ROW, "label": "AA"}, {**LABEL_ROW, "label": "Indie"}])
    data = load_curated(tmp_path)
    assert set(data.labels_for(10)) == {"Indie"}
    assert any("both AAA and AA" in problem for problem in data.problems)


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
