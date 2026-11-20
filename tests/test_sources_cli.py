"""The commands and settings added in version 0.6: `upcoming`, `critics`, the sync's new steps, `status` and
`check-curated` with the reviewer decision files, and the Wikimedia contact setting. No secret is ever printed.
"""

from dataclasses import replace

import httpx
import pytest

from game_librarian import __main__ as cli
from game_librarian import config
from game_librarian.curated import CuratedGame, load_curated
from game_librarian.wikipedia import WikimediaClient
from tests.conftest import write_csv
from tests.test_critics import FakeWikimedia
from tests.test_twitch import FakeHelix
from tests.test_upcoming import CLIENT_ID, CLIENT_SECRET, FakeIgdbApi, entry, igdb_game
from tests.test_web import run_test_sync

CONTACT = "reviewer@example.invalid"


def with_igdb(settings):
    return replace(settings, igdb_client_id=CLIENT_ID, igdb_client_secret=CLIENT_SECRET)


def test_the_contact_setting_is_optional_and_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.delenv("GAME_LIBRARIAN_CONTACT", raising=False)
    assert config.load_settings().contact is None
    (tmp_path / ".env").write_text(f"GAME_LIBRARIAN_CONTACT=  {CONTACT}  \n", encoding="utf-8")
    settings = config.load_settings()
    assert settings.contact == CONTACT and CONTACT not in repr(settings)


def test_the_new_commands_explain_what_they_need_and_call_nothing_without_it(settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "IgdbClient", lambda *args, **kwargs: pytest.fail("IGDB must not be called"))
    monkeypatch.setattr(cli, "WikimediaClient", lambda *args, **kwargs: pytest.fail("Wikimedia must not be called"))
    assert cli.main(["upcoming"]) == 1
    assert "IGDB_CLIENT_ID and IGDB_CLIENT_SECRET" in capsys.readouterr().err
    assert cli.main(["critics"]) == 1
    assert "GAME_LIBRARIAN_CONTACT" in capsys.readouterr().err
    assert cli.main(["critics", "--review"]) == 0  # the review list works offline
    assert "0 item(s) wait for review" in capsys.readouterr().out


def test_upcoming_refreshes_prints_the_lists_and_never_a_secret(settings, fake_steam, monkeypatch, capsys):
    api = FakeIgdbApi()
    api.games = {1: igdb_game(1, "Moon Garden", entry("PC", fmt=5, y=2027), steam=[500]),
                 2: igdb_game(2, "Console Quest", entry("PlayStation 5", fmt=7), platforms=("PlayStation 5",))}
    api.popularity[2] = [{"game_id": 1, "value": 0.003}, {"game_id": 2, "value": 0.002}]
    api.popularity[10] = [{"game_id": 1, "value": 0.0004}]
    fake_steam.store_summaries = {500: {"name": "Moon Garden", "type": 0, "visible": True}}
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "101", "title": "Catalog Game"}])
    helix = FakeHelix()
    helix.top = [{"id": "509658", "name": "Just Chatting", "igdb_id": ""},
                 {"id": "777", "name": "Console Quest", "igdb_id": "2"}]
    monkeypatch.setattr(cli, "load_settings", lambda: with_igdb(settings))
    monkeypatch.setattr(cli, "IgdbClient", lambda client_id, client_secret: api.client())
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    monkeypatch.setattr(cli, "TwitchClient", lambda client_id, client_secret: helix.client())
    assert cli.main(["upcoming"]) == 0
    out = capsys.readouterr().out
    assert "Most wanted on IGDB (upcoming):" in out and "1. Moon Garden [igdb-1, Steam app 500]: Q3 2027 (PC)" in out
    assert "2. Console Quest [igdb-2]: TBA (PlayStation 5)" in out
    assert "Most wanted new releases (first released in the last 90 days):" in out
    twitch_part = out[out.index("Most watched on Twitch ("):]
    assert "1. Console Quest [igdb-2]: TBA (PlayStation 5)" in twitch_part and "Just Chatting" not in twitch_part
    assert cli.main(["upcoming"]) == 0  # a second run the same day does nothing
    assert "nothing to do (--force refreshes now)" in capsys.readouterr().out
    api.down = True
    assert cli.main(["upcoming", "--force"]) == 1  # IGDB down: failed, the saved lists stay
    output = capsys.readouterr()
    for secret in (CLIENT_ID, CLIENT_SECRET, "fake-token-123", "twitch-token-1"):
        assert secret not in output.out + output.err + out
    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "Trending lists (IGDB, Twitch): last refresh" in status and "2 IGDB games saved" in status
    assert "Twitch most-watched list: 2 categories, checked" in status
    assert "Critic scores (Wikipedia): GAME_LIBRARIAN_CONTACT not set" in status
    assert "Twitch live streams: 0 game(s) in the cache" in status


def test_critics_reads_due_games_and_lists_what_needs_review(settings, fake_steam, clock, monkeypatch, capsys):
    fake_steam.add_game(500, "Moon Garden")
    fake_steam.add_game(700, "Twin Game")
    run_test_sync(settings, fake_steam, clock, [CuratedGame(500, "Moon Garden", ""), CuratedGame(700, "Twin Game", "")])
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "500", "title": "Moon Garden"},
                                                   {"steam_appid": "700", "title": "Twin Game"}])
    fake = FakeWikimedia()
    monkeypatch.setattr(cli, "load_settings", lambda: replace(settings, contact=CONTACT))
    monkeypatch.setattr(cli, "WikimediaClient", lambda contact: WikimediaClient(
        contact, http=httpx.Client(transport=httpx.MockTransport(fake.handler)), sleep=lambda s: None))
    assert cli.main(["critics"]) == 0
    out = capsys.readouterr().out
    assert "reading 2 of 2 games" in out and '"match_verified": 1' in out and '"match_needs_review": 1' in out
    assert fake.requests[0].headers["User-Agent"].count(CONTACT) == 1  # the contact goes only into the User-Agent
    assert cli.main(["critics"]) == 0
    assert "nothing is due" in capsys.readouterr().out
    assert cli.main(["critics", "--review"]) == 0
    review = capsys.readouterr().out
    assert "700" in review and "Several Wikidata items" in review and "Famitsu" in review
    assert CONTACT not in review


def test_sync_runs_the_new_steps_only_when_configured(settings, fake_steam, monkeypatch, capsys):
    fake_steam.add_game(101, "Catalog Game")
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": "101", "title": "Catalog Game"}])
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "SteamClient", lambda api_key=None: fake_steam.client(api_key))
    monkeypatch.setattr(cli, "IgdbClient", lambda *args, **kwargs: pytest.fail("IGDB must not be called"))
    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "Trending lists (IGDB, Twitch): skipped, IGDB is not configured" in out
    assert "Critic scores (Wikipedia): skipped, GAME_LIBRARIAN_CONTACT is not set" in out
    monkeypatch.setattr(cli, "load_settings", lambda: replace(with_igdb(settings), contact=CONTACT))
    assert cli.main(["sync", "--skip-upcoming", "--skip-critics"]) == 0
    out = capsys.readouterr().out
    assert "skipped (--skip-upcoming)" in out and "skipped (--skip-critics)" in out


def test_reviewer_decision_files_are_validated(settings, capsys, monkeypatch):
    curated = settings.curated_dir
    write_csv(curated / "games.csv", [{"steam_appid": "101", "title": "Catalog Game"}])
    write_csv(curated / "steam_matches.csv", [
        {"igdb_id": "1", "steam_appid": "500", "decision": "match", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"igdb_id": "2", "steam_appid": "", "decision": "no_match", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"igdb_id": "3", "steam_appid": "", "decision": "match", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"igdb_id": "1", "steam_appid": "501", "decision": "match", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"igdb_id": "x", "steam_appid": "", "decision": "maybe", "checked_on": "soon", "checked_by": "", "note": ""},
    ])
    write_csv(curated / "critic_articles.csv", [
        {"game": "271590", "article": "Grand Theft Auto V", "decision": "use", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"game": "igdb-5", "article": "", "decision": "reject", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"game": "igdb-6", "article": "", "decision": "use", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
        {"game": "abc", "article": "X", "decision": "use", "checked_on": "2026-11-19", "checked_by": "a reviewer", "note": ""},
    ])
    data = load_curated(curated)
    assert set(data.steam_match_decisions) == {1, 2} and data.steam_match_decisions[1].steam_appid == 500
    assert set(data.critic_article_decisions) == {"271590", "igdb-5"}
    problems = "\n".join(data.problems)
    assert "steam_matches.csv:4: a match needs the Steam app id" in problems
    assert "steam_matches.csv:5: duplicate igdb_id 1" in problems
    assert "steam_matches.csv:6:" in problems and "checked_by is empty" in problems
    assert "critic_articles.csv:4: using an article needs its English Wikipedia title" in problems
    assert "critic_articles.csv:5: game must be a Steam app id or igdb-<IGDB id>" in problems
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    assert cli.main(["check-curated"]) == 1
    out = capsys.readouterr().out
    assert "steam_matches.csv: 2 reviewed Steam matches" in out and "critic_articles.csv: 2 reviewed" in out


def test_shipped_curated_folder_has_no_decision_problems():
    data = load_curated(config.DEFAULT_CURATED_DIR)
    assert not [problem for problem in data.problems if "steam_matches" in problem or "critic_articles" in problem]
