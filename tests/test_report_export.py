"""Report export: one row per game, bridge tables for tags and awards, no secrets, a repointable model."""

import csv
import re
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from game_librarian import __main__ as cli
from game_librarian import store
from game_librarian.config import REPORTING_DIR
from game_librarian.curated import CuratedGame
from game_librarian.report_export import (
    AWARD_COLUMNS,
    EXPORT_INFO_COLUMNS,
    GAME_COLUMNS,
    export_report,
    point_report_at,
)
from game_librarian.sync import SyncOptions, run_sync
from tests.conftest import write_csv

SECRET = "secret-test-key-1234567890"
AWARD = {
    "organization": "The Game Awards", "year": "2023", "category": "Game of the Year", "result": "winner",
    "award_title": "Test Quest", "steam_appid": "10", "steam_mapping": "same", "mapping_note": "",
    "source_url": "https://example.invalid/winners", "review_status": "verified", "checked_on": "2026-11-09",
}
PARAMETERS = (
    'expression DataFolder = "C:\\placeholder\\" meta [IsParameterQuery=true, Type="Text", '
    'IsParameterQueryRequired=true]\r\n\tlineageTag: 1\r\n'
)


def read(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def exported(settings, fake_steam, clock, tmp_path):
    """Three synced games, exported on 9 Nov 2026 to tmp_path/export, with awards and labels.

    Test Quest (AA + Indie, 2024) is ranked; Other Game (2011) is outside the four-year window;
    Quiet Game (2024) has no reviews, so it is Unranked.
    """
    fake_steam.add_game(10, "Test Quest", tags=((122, 900), (19, 800), (492, 700)), totals=(900, 100, "Very Positive"))
    fake_steam.add_game(20, "Other Game", tags=((19, 900),), totals=(30, 70, "Mostly Negative"),
                        release_date={"coming_soon": False, "date": "Apr 18, 2011"})
    fake_steam.add_game(30, "Quiet Game", positive=0, negative=0, totals=(0, 0, None))
    games = [CuratedGame(10, "Test Quest", ""), CuratedGame(20, "Other Game", ""), CuratedGame(30, "Quiet Game", "")]
    conn = store.initialize(settings.db_path)
    with fake_steam.client() as steam:
        run_sync(conn, steam, games, SyncOptions(), log=lambda message: None, now=clock)
    conn.close()

    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": str(g.appid), "title": g.title} for g in games])
    write_csv(settings.curated_dir / "awards.csv", [
        AWARD,
        {**AWARD, "year": "2024", "category": "Best Independent Game", "result": "nominee"},
        {**AWARD, "steam_appid": "20", "steam_mapping": "edition", "result": "nominee", "award_title": "Other Original",
         "mapping_note": "The nomination went to the original. Steam sells a remaster."},
        {**AWARD, "steam_appid": "", "steam_mapping": "none", "award_title": "Console Game", "mapping_note": "Not on Steam."},
        {**AWARD, "steam_appid": "99", "award_title": "Not Listed Here"},
        {**AWARD, "year": "2019", "review_status": "needs_review"},
    ])
    write_csv(settings.curated_dir / "classifications.csv", [
        {"steam_appid": "10", "label": "AA", "review_status": "verified", "evidence_url": "https://example.invalid/a",
         "evidence_summary": "A mid-sized team."},
        {"steam_appid": "10", "label": "Indie", "review_status": "verified", "evidence_url": "https://example.invalid/b",
         "evidence_summary": "Self-published."},
    ])
    settings = replace(settings, steam_api_key=SECRET, dashboard_url="https://app.powerbi.com/reportEmbed?reportId=x")
    out_dir = tmp_path / "export"
    summary = export_report(settings, out_dir, now=datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc),
                            today=date(2026, 11, 9))
    return summary, out_dir


def test_export_writes_one_table_per_file_with_documented_columns(exported):
    summary, out_dir = exported
    assert summary.rows == {"games.csv": 3, "game_tags.csv": 6, "tags.csv": 3, "awards.csv": 5, "export_info.csv": 1}
    assert list(read(out_dir / "games.csv")[0]) == GAME_COLUMNS
    assert list(read(out_dir / "awards.csv")[0]) == AWARD_COLUMNS
    assert not list(out_dir.glob(".*.tmp"))  # no temporary files are left behind


def test_review_counts_appear_once_per_game_however_many_tags_or_awards(exported, settings):
    _, out_dir = exported
    games = {row["appid"]: row for row in read(out_dir / "games.csv")}
    conn = store.connect(settings.db_path)
    stored_total = conn.execute("SELECT SUM(review_total) FROM games").fetchone()[0]
    conn.close()
    assert sum(int(row["total_reviews"] or 0) for row in games.values()) == stored_total == 1100
    assert "total_reviews" not in read(out_dir / "game_tags.csv")[0]  # the bridge carries no measures
    assert "total_reviews" not in read(out_dir / "awards.csv")[0]
    assert [row["tag"] for row in read(out_dir / "game_tags.csv") if row["appid"] == "10"] == ["RPG", "Action", "Indie"]
    # Quiet Game keeps the default RPG + Action tags.
    assert {row["tag"]: row["games"] for row in read(out_dir / "tags.csv")} == {"Action": "3", "Indie": "1", "RPG": "2"}


def test_games_carry_the_four_year_rank_reception_labels_and_award_status(exported):
    _, out_dir = exported
    games = {row["appid"]: row for row in read(out_dir / "games.csv")}
    quest, other, quiet = games["10"], games["20"], games["30"]
    assert (quest["four_year_rank"], quest["rank_status"], quest["rank_note"]) == ("1", "Ranked", "")
    assert (quest["ranking_tier"], quest["ranking_tier_name"], quest["in_four_year_window"]) == ("2", "AA", "true")
    assert (quest["positive_reviews"], quest["total_reviews"]) == ("900", "1000")  # the rank's sort key, and the total
    assert (quest["positive_share"], quest["steam_percent_positive"], quest["positive_band"]) == ("0.900000", "90", "90–94%")
    assert quest["production_label"] == "AA + Indie" and quest["award_status"] == "Winner"
    assert (quest["award_entries"], quest["award_wins"]) == ("2", "1")
    assert (quest["release_date"], quest["release_date_exact"], quest["release_year"]) == ("1 Jan, 2024", "2024-01-01", "2024")
    assert other["award_status"] == "Nominee" and other["production_label"] == "Unclassified"
    assert (other["four_year_rank"], other["in_four_year_window"], other["ranking_tier_name"]) == ("", "false", "Indie and Unclassified")
    assert (other["rank_status"], other["rank_note"]) == ("Not in the four-year ranking", "Released before the four-year window.")
    assert other["positive_reviews"] == "30"  # review data stays in the export for every game
    assert (quiet["four_year_rank"], quiet["rank_status"], quiet["in_four_year_window"]) == ("", "Unranked", "true")
    assert quiet["has_review_data"] == "false" and quiet["total_reviews"] == "" and quiet["rank_note"]
    assert quest["reviews_updated_at"] == "2026-11-09 12:00:00"
    assert "adjusted_score" not in quest and "catalog_rank" not in quest


def test_awards_keep_verified_results_and_say_where_each_one_applies(exported):
    _, out_dir = exported
    awards = read(out_dir / "awards.csv")
    assert len(awards) == 5  # the needs_review row is left out
    by_title = {(row["award_title"], row["year"], row["category"]): row for row in awards}
    edition = by_title[("Other Original", "2023", "Game of the Year")]
    assert edition["award_applies_to"] == "Original release"
    assert edition["catalog_appid"] == "20" and edition["catalog_status"] == "In catalog"
    assert edition["catalog_game"] == "Other Game"  # the current Steam name, beside the awarded title
    unmapped = by_title[("Console Game", "2023", "Game of the Year")]
    assert (unmapped["steam_appid"], unmapped["catalog_appid"], unmapped["catalog_status"]) == ("", "", "Not mapped to Steam")
    assert unmapped["catalog_game"] == ""
    elsewhere = by_title[("Not Listed Here", "2023", "Game of the Year")]
    assert (elsewhere["steam_appid"], elsewhere["catalog_appid"]) == ("99", "")
    assert elsewhere["catalog_status"] == "On Steam, not in catalog"
    assert sorted(row["result"] for row in awards) == ["Nominee", "Nominee", "Winner", "Winner", "Winner"]


def test_export_info_describes_the_export_and_the_ranking_window(exported):
    summary, out_dir = exported
    info = read(out_dir / "export_info.csv")[0]
    assert info["exported_at_utc"] == "2026-11-10 03:00:00" and info["last_sync_status"] == "success"
    assert (info["ranking_window_start"], info["ranking_window_end"]) == ("2022-11-09", "2026-11-09")
    assert (summary.window.start, summary.window.end) == (date(2022, 11, 9), date(2026, 11, 9))
    assert (info["games"], info["games_with_review_data"]) == ("3", "2")
    assert (info["games_in_ranking_window"], info["ranked_games"]) == ("2", "1")
    assert (info["verified_award_rows"], info["excluded_unverified_award_rows"]) == ("5", "1")
    assert not any("prior" in column for column in info)


def _model_columns(table_file: str) -> tuple[set[str], set[str]]:
    """(columns the TMDL table reads from its CSV file, columns its Power Query step types)."""
    text = (REPORTING_DIR / "GameLibrarian.SemanticModel" / "definition" / "tables" / table_file).read_text(encoding="utf-8")
    source_columns = set(re.findall(r"^\t\tsourceColumn: (\S+)$", text, re.M))
    typed_columns = set(re.findall(r'\{"([a-z_]+)", (?:Int64\.Type|type \w+)\}', text.split("partition", 1)[1]))
    return source_columns, typed_columns


@pytest.mark.parametrize("table_file, columns", [("Games.tmdl", GAME_COLUMNS), ("Export Info.tmdl", EXPORT_INFO_COLUMNS)])
def test_the_power_bi_model_reads_exactly_the_exported_columns(table_file, columns):
    source_columns, typed_columns = _model_columns(table_file)
    assert source_columns == set(columns)
    assert typed_columns == set(columns)


def test_no_setting_or_secret_is_written_to_the_export(exported):
    _, out_dir = exported
    for path in out_dir.iterdir():
        text = path.read_text(encoding="utf-8")
        assert SECRET not in text and "reportEmbed" not in text and "STEAM_API_KEY" not in text, path.name


def test_point_report_at_sets_the_data_folder_parameter_once(tmp_path):
    parameters = tmp_path / "expressions.tmdl"
    parameters.write_bytes(PARAMETERS.encode("utf-8"))
    data_dir = tmp_path / "my data"
    assert point_report_at(data_dir, parameters) is True
    text = parameters.read_bytes().decode("utf-8")
    assert f'expression DataFolder = "{data_dir.resolve()}\\" meta [IsParameterQuery=true' in text
    assert "\r\n\tlineageTag: 1\r\n" in text  # the rest of the file, line endings included, is untouched
    assert point_report_at(data_dir, parameters) is False
    parameters.write_text("expression Other = 1", encoding="utf-8")
    with pytest.raises(ValueError, match="DataFolder"):
        point_report_at(data_dir, parameters)


def test_export_report_command_writes_the_files_and_repoints_the_report(exported, settings, tmp_path, monkeypatch, capsys):
    parameters = tmp_path / "expressions.tmdl"
    parameters.write_text(PARAMETERS, encoding="utf-8")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "REPORT_PARAMETERS_FILE", parameters)
    out_dir = tmp_path / "cli-export"
    assert cli.main(["export-report", "--out", str(out_dir)]) == 0
    output = capsys.readouterr().out
    assert "games.csv: 3 rows" in output and "Updated the report's DataFolder parameter" in output
    assert "does not change until it is refreshed or republished" in output
    assert str(out_dir.resolve()) in parameters.read_text(encoding="utf-8")
