"""The award-based ranking (version 0.9) end to end: one synced catalog, the same ranks on the website's cards
and details, the dashboard's ranking table and the Power BI export, under filters and pages. Every game here is
inside both windows or outside both, so the website's five-year ranking and the export's four-year ranking agree.

The unit rules (groups, counts, reception, ties) are tested in test_ranking.py.
"""

import csv
import re

import pytest
from fastapi.testclient import TestClient

from game_librarian.curated import CuratedGame
from game_librarian.ranking import AWARD_GROUP_NAMES
from game_librarian.report_export import export_report
from game_librarian.web import CARD_GROUP_LABELS
from tests.conftest import write_csv
from tests.test_web import TODAY, card_html, card_rank, make_app, ranking_titles, run_test_sync, shown_appids, text_of

ON_STEAM = {"coming_soon": False, "date": "Mar 1, 2024"}  # inside the website's and the export's windows
# appid: (name, (positive, negative) Steam reviews, release date)
GAMES = {
    401: ("Indie Darling", (900, 100), ON_STEAM),  # verified Indie; two wins, one of them Game of the Year
    402: ("Blockbuster", (990_000, 10_000), ON_STEAM),  # verified AAA; no award recorded; 99% of a million
    403: ("Steam Favorite", (95_000, 5_000), ON_STEAM),  # Steam Awards Game of the Year, one nomination
    404: ("Remaster Edition", (800, 200), ON_STEAM),  # BAFTA Best Game, won by the original release
    405: ("Quiet Nominee", (0, 0), ON_STEAM),  # a nomination, but no Steam reviews
    406: ("Unknown Gem", (0, 0), ON_STEAM),  # no award and no Steam reviews: Unranked
    407: ("Double Entry", (700, 300), ON_STEAM),  # one GDCA win recorded twice, one unverified win
    408: ("Few Reviews", (99, 1), ON_STEAM),  # no award; 99% of only 100 reviews
    409: ("Old Winner", (5_000, 100), {"coming_soon": False, "date": "Apr 18, 2011"}),  # too old to be ranked
    **{appid: (f"Filler {appid}", (700 + 10 * (appid - 410), 300 - 10 * (appid - 410)), ON_STEAM)
       for appid in range(411, 417)},  # no award; 71%–76% of 1,000 reviews
}
EXPECTED_ORDER = [401, 404, 407, 403, 405, 402, 416, 415, 414, 413, 412, 411, 408]  # then 406, Unranked


def award(appid, organization, category, year, result="winner", **extra):
    return {"organization": organization, "year": str(year), "category": category, "result": result,
            "award_title": GAMES[appid][0], "steam_appid": str(appid), "steam_mapping": "same", "mapping_note": "",
            "source_url": "https://example.invalid/results", "review_status": "verified", "checked_on": "2026-11-09",
            **extra}


@pytest.fixture
def ranked(settings, fake_steam, clock):
    """A client for the catalog above, synced and curated; also returns the settings for exports."""
    curated_games = []
    for appid, (name, (positive, negative), released) in GAMES.items():
        tags = ((492, 900), (122, 800)) if appid == 401 else ((122, 900), (19, 800))
        fake_steam.add_game(appid, name, tags=tags, release_date=released,
                            totals=(positive, negative, "Very Positive" if positive else None),
                            positive=1 if positive else 0, negative=1 if negative else 0)
        curated_games.append(CuratedGame(appid, name, ""))
    run_test_sync(settings, fake_steam, clock, curated_games)
    curated = settings.curated_dir
    write_csv(curated / "games.csv", [{"steam_appid": str(g.appid), "title": g.title} for g in curated_games])
    write_csv(curated / "awards.csv", [
        award(401, "The Game Awards", "Game of the Year", 2024),
        award(401, "The Game Awards", "Best Independent Game", 2024),
        award(403, "The Steam Awards", "Game of the Year", 2024),
        award(403, "The Game Awards", "Game of the Year", 2024, "nominee"),
        award(404, "BAFTA Games Awards", "Best Game", 2025, steam_mapping="edition",
              mapping_note="The award went to the original release. Steam sells this remaster."),
        award(405, "The Game Awards", "Game of the Year", 2024, "nominee"),
        award(407, "Game Developers Choice Awards", "Game of the Year", 2025),
        award(407, "Game Developers Choice Awards", "Game of the Year", 2025),  # line 9: the same result again
        award(407, "The Game Awards", "Game of the Year", 2025, review_status="needs_review"),
        award(409, "The Game Awards", "Game of the Year", 2011),
    ])
    label = {"title": "", "review_status": "verified", "evidence_url": "https://example.invalid/evidence",
             "evidence_summary": "Test evidence.", "checked_on": "2026-11-09"}
    write_csv(curated / "classifications.csv", [{"steam_appid": "401", "label": "Indie", **label},
                                                {"steam_appid": "402", "label": "AAA", **label}])
    return TestClient(make_app(settings)), settings


def website_ranks(client, **params) -> dict[int, str]:
    """The card rank text of every game a Discover list shows, over all its pages."""
    ranks, page = {}, 1
    while True:
        html = client.get("/", params={**params, "page": str(page)}).text
        ranks.update({appid: card_rank(html, appid) for appid in shown_appids(html)})
        if 'rel="next"' not in html:
            return ranks
        page += 1


def test_the_website_ranks_by_award_group_counts_then_reception(ranked):
    client, _ = ranked
    first, second = client.get("/").text, client.get("/?page=2").text
    assert shown_appids(first) + shown_appids(second) == EXPECTED_ORDER + [406]
    assert [card_rank(first, appid) for appid in EXPECTED_ORDER[:12]] == [f"#{rank}" for rank in range(1, 13)]
    assert card_rank(second, 408) == "#13" and card_rank(second, 406) == "Unranked"
    assert "13 of 14 games in the window are ranked." in text_of(first)
    # An Indie winner ranks above the AAA game with the most and best reviews but no award recorded.
    assert card_rank(first, 401) == "#1" and card_rank(first, 402) == "#6"
    basis = {appid: text_of(re.search(r'<p class="card-rank-basis".*?</p>', card_html(first, appid), re.S).group(0))
             for appid in (401, 404, 407, 403, 405, 402)}
    assert basis == {
        401: "Award group: The Game Awards GOTY · 2 wins",
        404: "Award group: BAFTA Best Game (for the original release) · 1 win",
        407: "Award group: Game Developers Choice GOTY · 1 win",  # the repeated row is counted once
        403: "Award group: Steam Awards GOTY · 1 win, 1 nomination",
        405: "Award group: Nominated, no win · 1 nomination",
        402: "Award group: No award recorded · ranked by Steam reception",
    }
    assert "No Steam reviews yet" in card_html(first, 405)  # missing reviews are shown, not ranked as zero
    assert "for the original release" in text_of(card_html(first, 404))
    assert "1 curated row has a problem and is ignored." in text_of(first)  # the repeated award row


def test_every_award_group_has_a_short_card_label():
    assert list(CARD_GROUP_LABELS) == list(AWARD_GROUP_NAMES)


def test_details_explain_each_games_place(ranked):
    client, _ = ranked

    def rank_line(appid):
        return text_of(re.search(r'<p class="details-rank">.*?</p>', client.get(f"/games/{appid}/panel").text,
                                 re.S).group(0))

    assert rank_line(401) == "Five-year rank #1 · The Game Awards — Game of the Year winner · 2 wins"
    assert rank_line(404) == ("Five-year rank #2 · BAFTA Games Awards — Best Game winner (for the original release)"
                              " · 1 win")
    assert rank_line(405) == ("Five-year rank #5 · Verified nominations, no win · 1 nomination · no usable Steam "
                              "review summary (ranked on its awards)")
    assert rank_line(402) == "Five-year rank #6 · No award recorded · ranked by Steam reception"
    assert rank_line(406) == ("Unranked · No award recorded and no usable Steam review summary. This game has no "
                              "Steam reviews yet.")
    assert rank_line(409) == "Not in the five-year ranking · Released before the five-year window."


def test_filters_collections_sorts_and_pages_never_change_a_rank(ranked):
    client, _ = ranked
    full = website_ranks(client)
    assert len(full) == 14
    for params in ({"label": "Indie"}, {"label": "AAA"}, {"tag": "Indie"}, {"q": "remaster"}, {"sort": "name"},
                   {"sort": "reception"}, {"view": "goty"}, {"view": "awards", "winners": "1"},
                   {"award_org": "The Game Awards"}, {"view": "all", "sort": "newest"}):
        for appid, rank in website_ranks(client, **params).items():
            expected = full.get(appid, "Not in the five-year ranking")
            if params.get("view") and rank.startswith("#"):
                rank = rank.removesuffix(" in the five-year ranking")  # outside Top-ranked the number is labeled
            assert rank == expected, (params, appid)
    assert website_ranks(client, label="AAA") == {402: "#6"}


def test_the_dashboard_and_the_export_rank_exactly_like_the_website(ranked, tmp_path):
    client, settings = ranked
    website = {appid: int(rank.lstrip("#")) for appid, rank in website_ranks(client).items() if rank.startswith("#")}

    export_report(settings, tmp_path, today=TODAY)
    with (tmp_path / "games.csv").open(encoding="utf-8", newline="") as handle:
        exported = {int(row["appid"]): row for row in csv.DictReader(handle)}
    assert {appid: int(row["four_year_rank"]) for appid, row in exported.items() if row["four_year_rank"]} == website
    assert [(exported[appid]["ranking_tier"], exported[appid]["award_wins"], exported[appid]["award_nominations"])
            for appid in EXPECTED_ORDER[:6]] == [("1", "2", "0"), ("2", "1", "0"), ("4", "1", "0"),
                                                 ("5", "1", "1"), ("7", "0", "1"), ("8", "0", "0")]
    assert exported[404]["ranking_tier_original_release"] == "true"
    assert exported[407]["award_entries"] == "1" and exported[407]["award_status"] == "Winner"
    assert (exported[405]["has_review_data"], exported[405]["total_reviews"], exported[405]["rank_status"]) == (
        "false", "", "Ranked")
    assert (exported[406]["rank_status"], exported[409]["rank_status"]) == ("Unranked", "Not in the four-year ranking")
    assert (exported[409]["ranking_tier"], exported[409]["four_year_rank"]) == ("1", "")  # a group, but no rank
    with (tmp_path / "awards.csv").open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 8  # 10 rows: the repeat and the unverified row are left out

    html = client.get("/dashboard/reception").text
    titles = ranking_titles(html)
    assert [int(rank) for rank, _ in titles] == sorted(website.values())
    assert [title for _, title in titles] == [GAMES[appid][0] for appid in EXPECTED_ORDER]
    row = re.search(r"<tr><td class=\"num\">5</td>.*?</tr>", html, re.S).group(0)  # the nominee without reviews
    assert text_of(row) == "5 Quiet Nominee Verified nominations, no win 0 1 No data No data"
    assert "Five-year ranking: awards, then Steam reception" in html
