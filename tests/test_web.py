"""Web app: catalog cards, the four-year ranking, views, search, filters, sorting, pagination, details, dashboard."""

import re
from dataclasses import replace
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from game_librarian import store
from game_librarian.curated import CuratedGame
from game_librarian.steam import SteamClient
from game_librarian.sync import SyncOptions, run_sync
from game_librarian.web import PAGE_SIZE, create_app
from tests.conftest import review, write_csv

TODAY = date(2026, 11, 9)  # the four-year window is 9 Nov 2022 – 9 Nov 2026
AWARD = {
    "organization": "The Game Awards", "year": "2023", "category": "Game of the Year", "result": "winner",
    "award_title": "Game 01", "steam_appid": "101", "steam_mapping": "same", "mapping_note": "",
    "source_url": "https://example.invalid/winners", "review_status": "verified", "checked_on": "2026-11-09",
}
LABEL = {
    "steam_appid": "101", "title": "Game 01", "label": "AAA", "review_status": "verified",
    "evidence_url": "https://example.invalid/budget", "evidence_summary": "Reported budget of $200 million.",
    "checked_on": "2026-11-09",
}
GAME_COUNT = 14  # more than one page of PAGE_SIZE cards


def make_app(settings):
    """The web app with a fixed date, so the four-year window never depends on when the tests run."""
    return create_app(settings, today=lambda: TODAY)


def run_test_sync(settings, fake_steam, clock, games, **options):
    conn = store.initialize(settings.db_path)
    try:
        with fake_steam.client() as steam:
            run_sync(conn, steam, games, SyncOptions(**options), log=lambda message: None, now=clock)
    finally:
        conn.close()


@pytest.fixture
def games(settings, fake_steam, clock):
    """Fourteen synced games, all released 1 Jan 2024 (inside the window). Game n has 100 × n positive reviews.

    Game 01 is verified AAA and Game 02 verified AA and Indie, so the four-year ranking is
    Game 01, Game 02, then Game 14 down to Game 03 (Unclassified, most positive reviews first).
    Odd games are tagged RPG + Action, even games RPG + Indie.
    """
    curated_games = []
    for number in range(1, GAME_COUNT + 1):
        appid, name = 100 + number, f"Game {number:02d}"
        tags = ((122, 900), (19, 800)) if number % 2 else ((122, 900), (492, 700))
        trailers = {5: (), 6: (("Mature Trailer", False),),
                    7: (("Launch Trailer", True), ("Gameplay Overview", True))}.get(number, (("Launch Trailer", True),))
        fake_steam.add_game(appid, name, tags=tags, totals=(100 * number, 1000 - 50 * number, "Mixed"),
                            trailers=trailers)
        curated_games.append(CuratedGame(appid, name, ""))
    run_test_sync(settings, fake_steam, clock, curated_games)

    curated = settings.curated_dir
    write_csv(curated / "games.csv", [{"steam_appid": str(g.appid), "title": g.title} for g in curated_games])
    write_csv(curated / "awards.csv", [
        AWARD,
        {**AWARD, "steam_appid": "102", "award_title": "Game 02 Original", "year": "2024", "result": "nominee",
         "category": "Best Independent Game", "steam_mapping": "edition",
         "mapping_note": "The nomination went to the original game. Steam sells a remaster."},
        {**AWARD, "steam_appid": "103", "year": "2022", "review_status": "needs_review"},
        {**AWARD, "steam_appid": "", "award_title": "Console Exclusive", "year": "2021", "steam_mapping": "none",
         "mapping_note": "Not on Steam."},
    ])
    write_csv(curated / "classifications.csv", [
        LABEL,
        {**LABEL, "steam_appid": "102", "label": "AA"},
        {**LABEL, "steam_appid": "102", "label": "Indie", "evidence_summary": "Won an independent games award."},
        {**LABEL, "steam_appid": "103", "review_status": "needs_review"},
        {**LABEL, "steam_appid": "104", "label": "Indie", "review_status": "rejected"},
    ])
    return curated_games


@pytest.fixture
def client(settings, games):
    return TestClient(make_app(settings))


# appid, name, Steam release-date text, coming soon, positive reviews, verified label
DATED_GAMES = [
    (301, "Cutoff Day", "Nov 9, 2022", False, 5_000, None),  # in: exactly the first day of the window
    (302, "Day Before Cutoff", "Nov 8, 2022", False, 900_000, "AAA"),  # out: one day too old
    (303, "Released Today", "9 Nov, 2026", False, 3_000, None),  # in: today, in Steam's day-first format
    (304, "Tomorrow", "Nov 10, 2026", False, 1_000, None),  # out: a future date
    (305, "Soon", "Coming soon", True, 0, None),  # out: not released yet
    (306, "Quarter Only", "Q4 2025", False, 2_000, None),  # out: no exact release date
    (307, "Classic", "Apr 18, 2011", False, 800_000, "AA"),  # out: older
    (308, "Recent AA", "Mar 2, 2025", False, 100, "AA"),  # in: the AA tier beats games with more reviews
]


@pytest.fixture
def dated(settings, fake_steam, clock):
    """A client for games on both sides of the window, and without exact dates. The Classic won an award."""
    curated_games = []
    for appid, name, released, coming_soon, positive, _ in DATED_GAMES:
        fake_steam.add_game(appid, name, release_date={"coming_soon": coming_soon, "date": released},
                            totals=(positive, 100 if positive else 0, "Positive"))
        curated_games.append(CuratedGame(appid, name, ""))
    run_test_sync(settings, fake_steam, clock, curated_games)
    curated = settings.curated_dir
    write_csv(curated / "games.csv", [{"steam_appid": str(g.appid), "title": g.title} for g in curated_games])
    write_csv(curated / "awards.csv", [{**AWARD, "steam_appid": "307", "award_title": "Classic"}])
    write_csv(curated / "classifications.csv", [
        {**LABEL, "steam_appid": str(appid), "title": name, "label": label}
        for appid, name, _, _, _, label in DATED_GAMES if label
    ])
    return TestClient(make_app(settings))


def card_html(html: str, appid: int) -> str:
    """The HTML of one catalog card."""
    match = re.search(rf'<article class="card" id="game-{appid}".*?</article>', html, re.S)
    assert match, f"no card for app {appid}"
    return match.group(0)


def card_rank(html: str, appid: int) -> str:
    """The visible rank line of one card: '#3', '#3 in the four-year ranking', 'Unranked', ..."""
    rank = re.search(r'<p class="card-rank">(.*?)</p>', card_html(html, appid), re.S).group(1)
    return text_of(re.sub(r'<span class="visually-hidden">.*?</span>', "", rank))


def shown_appids(html: str) -> list[int]:
    return [int(appid) for appid in re.findall(r'<article class="card" id="game-(\d+)"', html)]


def text_of(html: str) -> str:
    """Visible-ish text with tags removed and whitespace collapsed, for phrase checks.

    The space a removed tag leaves before punctuation is dropped: "<strong>x</strong>:" -> "x:".
    """
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    return re.sub(r" ([.,:;)])", r"\1", text)


def view_switch(html: str) -> str:
    return re.search(r'<nav class="view-switch".*?</nav>', html, re.S).group(0)


# ---------------------------------------------------------------------- page frame


def test_empty_catalog_explains_how_to_sync(settings):
    response = TestClient(make_app(settings)).get("/")
    assert response.status_code == 200
    assert "The library is empty" in response.text
    assert "python -m game_librarian sync" in response.text


def test_header_shows_the_wordmark_discover_and_a_dashboard_link(client):
    html = client.get("/").text
    assert "<title>Game Librarian</title>" in html
    assert '<a class="wordmark" href="/">GAME LIBRARIAN</a>' in html
    assert '<h1 id="discover-title">Discover.</h1>' in html
    assert re.search(r'<a class="nav-link" href="/dashboard"\s*>Dashboard</a>', html)
    assert "Power BI Dashboard" not in html


# ---------------------------------------------------------------------- cards and the four-year ranking


def test_card_shows_rank_image_title_label_release_tags_summary_reviews_awards_and_steam_link(client):
    card = card_html(client.get("/").text, 101)
    assert card_rank(card, 101) == "#1"
    assert 'src="https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/101/library_600x900.jpg?t=1"' in card
    assert 'id="card-title-101">Game 01</h3>' in card
    assert '<li class="label">AAA</li>' in card
    assert '<p class="card-release">Released 1 Jan 2024</p>' in card
    assert "<li>RPG</li>" in card and "<li>Action</li>" in card
    assert "Game 01 is a test game about testing &amp; more." in card  # the first sentence of the short description
    # The positive count decides the rank; the share (rounded down like Steam) and the total stay visible.
    assert "100 positive · 9% of 1,050 Steam reviews" in text_of(card)
    assert "The Game Awards 2023 · Game of the Year · Winner" in text_of(card)
    assert 'href="https://store.steampowered.com/app/101/"' in card and "View on Steam" in card
    assert 'data-details="101"' in card and 'href="/games/101"' in card
    assert "nofollow" not in card  # the Steam Web API terms forbid nofollow links to Valve


def test_labels_are_verified_or_unclassified_and_edition_awards_say_so(client):
    html = client.get("/?sort=name").text
    both = card_html(html, 102)
    assert '<li class="label">AA</li>' in both and '<li class="label">Indie</li>' in both
    assert "Best Independent Game · Nominee · for the original release" in text_of(both)
    for appid in (103, 104):  # needs_review AAA label and award; rejected Indie label
        card = card_html(html, appid)
        assert '<li class="label is-unclassified">Unclassified</li>' in card
        assert "award-list" not in card


def test_games_are_ranked_by_tier_then_positive_reviews(client):
    html = client.get("/").text
    assert shown_appids(html) == [101, 102, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105]
    assert card_rank(html, 101) == "#1"  # AAA, although it has the fewest positive reviews (100)
    assert card_rank(html, 102) == "#2"  # AA (and Indie): the AA tier is next, with 200 positive reviews
    assert card_rank(html, 114) == "#3"  # the Unclassified game with the most positive reviews (1,400)
    assert "<h2>Top-ranked games</h2>" in html
    assert "Showing 1–12 of 14 games · 14 of 14 games in the window are ranked." in text_of(html)
    second = client.get("/?page=2").text
    assert shown_appids(second) == [104, 103] and card_rank(second, 103) == "#14"


def test_search_and_filters_keep_each_games_four_year_rank(client):
    html = client.get("/", params={"q": "game 07"}).text
    assert shown_appids(html) == [107]
    assert card_rank(html, 107) == "#10"  # its place in the whole four-year ranking, not #1 of the results
    indie = client.get("/", params=[("tag", "Indie")]).text
    assert [card_rank(indie, appid) for appid in shown_appids(indie)] == ["#2", "#3", "#5", "#7", "#9", "#11", "#13"]
    assert card_rank(client.get("/", params={"label": "Unclassified"}).text, 114) == "#3"
    assert card_rank(client.get("/", params={"award": "Best Independent Game"}).text, 102) == "#2"
    assert card_rank(client.get("/", params={"sort": "name"}).text, 105) == "#12"  # another order, same rank


def test_the_window_and_the_ranking_rules_are_explained_on_the_page(client):
    html = client.get("/").text
    scope = text_of(re.search(r'<p class="results-scope">.*?</p>', html, re.S).group(0))
    assert scope.startswith("Games released 9 Nov 2022 – 9 Nov 2026: the last four years, up to today.")
    assert "are in All games" in scope and 'href="/?view=all">All games</a>' in html
    tip = re.search(r'<details class="info-tip">.*?</details>', html, re.S).group(0)
    assert 'aria-label="How the ranking works"' in tip
    text = text_of(tip)
    for phrase in (
        "Only games released in the last four years: from 9 Nov 2022 to today, 9 Nov 2026.",
        "The window moves forward every day.",
        "Verified AAA games come first, then verified AA games, then Indie and Unclassified games together.",
        "Within each tier, the game with more positive Steam reviews ranks higher.",
        "an AAA game can rank above an AA game, and an AA game above an Indie or Unclassified game, "
        "even when it has fewer positive reviews.",
        "go to the title A–Z, then the Steam app ID.",
        "It is a discovery order, not a judgment of quality.",
        "games released earlier in the window, because they have had more time to collect reviews.",
        "Production labels and awards come from this project’s hand-curated data",
        "Awards are not part of the rank.",
        "Searching and filtering never change a game’s rank.",
    ):
        assert phrase in text, phrase
    assert "adjusted score" not in html.lower() and "500 ×" not in html  # the old formula is gone


def test_games_without_a_usable_review_summary_are_unranked_and_listed_last(settings, fake_steam, clock):
    fake_steam.add_game(201, "Reviewed Game", totals=(90, 10, "Positive"))
    fake_steam.add_game(202, "Another Reviewed Game", totals=(50, 50, "Mixed"))
    fake_steam.add_game(203, "Aaa Quiet Game", positive=0, negative=0, totals=(0, 0, None))
    fake_steam.add_game(204, "Never Reviewed")
    games = [CuratedGame(appid, f"Game {appid}", "") for appid in (201, 202, 203, 204)]
    run_test_sync(settings, fake_steam, clock, games[:3])
    run_test_sync(settings, fake_steam, clock, games, skip_reviews=True)  # 204 gets details but no reviews

    html = TestClient(make_app(settings)).get("/").text
    assert shown_appids(html) == [201, 202, 203, 204]  # unranked games last, in A-Z order
    assert card_rank(html, 201) == "#1" and card_rank(html, 202) == "#2"
    for appid in (203, 204):
        assert card_rank(html, appid) == "Unranked"  # never treated as zero positive reviews
    assert "No Steam reviews yet" in card_html(html, 203)
    assert "Steam review data unavailable" in card_html(html, 204)
    assert "2 of 4 games in the window are ranked" in text_of(html)


# ---------------------------------------------------------------------- the four-year window and All games


def test_top_ranked_view_shows_only_games_released_in_the_window(dated):
    html = dated.get("/").text
    assert shown_appids(html) == [308, 301, 303]  # on the cutoff day, today, and the AA game first
    assert [card_rank(html, appid) for appid in (308, 301, 303)] == ["#1", "#2", "#3"]
    assert "Showing 1–3 of 3 games · 3 of 3 games in the window are ranked." in text_of(html)
    assert "Released 9 Nov 2022" in card_html(html, 301) and "Released 9 Nov 2026" in card_html(html, 303)


def test_all_games_view_lists_older_and_undated_games_without_a_number(dated):
    html = dated.get("/", params={"view": "all"}).text
    assert "<h2>All games</h2>" in html
    assert shown_appids(html) == [307, 301, 302, 306, 308, 303, 305, 304]  # A–Z: no ranking order is implied
    assert card_rank(html, 308) == "#1 in the four-year ranking"
    for appid in (302, 304, 305, 306, 307):
        assert card_rank(html, appid) == "Not in the four-year ranking"
    assert ("A number (#) is a game’s place in the four-year ranking of games released 9 Nov 2022 – 9 Nov 2026; "
            "other games have no number.") in text_of(html)
    assert "Showing 1–8 of 8 games · 3 of 8 catalog games are in the four-year ranking." in text_of(html)
    assert "Released 18 Apr 2011" in card_html(html, 307)
    assert "Release: Coming soon" in card_html(html, 305) and "Released Q4 2025" in card_html(html, 306)
    assert "Release: 10 Nov 2026" in card_html(html, 304)  # a date after today is not "Released"
    assert "The Game Awards 2023 · Game of the Year · Winner" in text_of(card_html(html, 307))  # awards stay visible


def test_all_games_sorted_by_rank_lists_ranked_games_first_then_the_rest_a_to_z(dated):
    html = dated.get("/", params={"view": "all", "sort": "rank"}).text
    assert shown_appids(html) == [308, 301, 303, 307, 302, 306, 305, 304]
    assert "Ranked games come first; the others follow A–Z." in text_of(html)


def test_details_say_why_a_game_is_outside_the_four_year_ranking(dated):
    reasons = {
        302: "Released before the four-year window.",
        304: "Its release date is after today.",
        305: "Not released yet: Steam lists it as coming soon.",
        306: "Steam gives no exact release date (“Q4 2025”).",
    }
    for appid, reason in reasons.items():
        panel = text_of(dated.get(f"/games/{appid}/panel").text)
        assert f"Not in the four-year ranking · {reason}" in panel, appid
    assert "Four-year rank #1 · AA tier" in text_of(dated.get("/games/308/panel").text)
    assert '<a href="/?tag=RPG&amp;view=all"' in dated.get("/games/307/panel").text  # the tag list includes this game


def test_search_and_filters_point_to_matching_games_outside_the_window(dated):
    html = dated.get("/", params={"q": "classic"}).text
    assert shown_appids(html) == []
    assert "No games match “classic” with the selected filters in the four-year ranking." in text_of(html)
    assert 'href="/?q=classic&amp;view=all">Show 1 matching game in All games</a>' in html
    found = dated.get("/", params={"q": "classic", "view": "all"}).text
    assert shown_appids(found) == [307] and card_rank(found, 307) == "Not in the four-year ranking"
    assert shown_appids(dated.get("/", params={"award": "any", "view": "all"}).text) == [307]
    recent = dated.get("/", params={"q": "cutoff"}).text  # "Cutoff Day" is in the window, "Day Before Cutoff" is not
    assert shown_appids(recent) == [301]
    assert 'href="/?q=cutoff&amp;view=all">1 more matching game is outside the four-year ranking</a>' in recent


def test_a_catalog_without_recent_games_points_to_all_games(settings, fake_steam, clock):
    fake_steam.add_game(700, "Old Game", release_date={"coming_soon": False, "date": "Apr 18, 2011"})
    run_test_sync(settings, fake_steam, clock, [CuratedGame(700, "Old Game", "")])
    html = TestClient(make_app(settings)).get("/").text
    assert "No games in the four-year window" in html and 'href="/?view=all">Show all games</a>' in html


def test_view_tabs_keep_the_search_and_filters_and_count_their_games(dated):
    html = dated.get("/", params={"q": "cutoff"}).text
    tabs = view_switch(html)
    assert 'href="/?q=cutoff" aria-current="page"' in tabs and 'href="/?q=cutoff&amp;view=all"' in tabs
    assert text_of(tabs) == "Top-ranked · last four years 1 game All games 2 games"


def test_switching_views_keeps_only_a_deliberately_chosen_sort(client):
    assert 'href="/?view=all"' in view_switch(client.get("/").text)  # All games opens in its own default, A–Z
    assert 'href="/?view=all"' in view_switch(client.get("/", params={"sort": "rank"}).text)  # as sent by the form
    assert 'href="/?sort=reviews&amp;view=all"' in view_switch(client.get("/", params={"sort": "reviews"}).text)
    assert 'href="/"' in view_switch(client.get("/", params={"view": "all", "sort": "name"}).text)


def test_all_games_view_keeps_its_view_in_pagination_forms_and_details(client):
    html = client.get("/", params={"view": "all"}).text
    assert shown_appids(html) == list(range(101, 101 + PAGE_SIZE))
    assert '<input type="hidden" name="view" value="all">' in html
    assert 'href="/?view=all&amp;page=2" rel="next"' in html
    second = client.get("/?view=all&page=2").text
    assert shown_appids(second) == [113, 114] and card_rank(second, 113) == "#4 in the four-year ranking"
    assert 'href="/games/113?view=all&amp;page=2"' in second
    back = client.get("/games/113?view=all&page=2").text
    assert '<a class="text-link" href="/?view=all&amp;page=2#game-113">← Back to results</a>' in back


# ---------------------------------------------------------------------- search, filters, sorting


def test_search_ignores_case_and_punctuation(client):
    html = client.get("/", params={"q": "  GAME-07 "}).text
    assert "of 1 game matching your filters" in html
    assert shown_appids(html) == [107]


def test_tag_filters_require_every_selected_tag_and_count_current_results(client):
    html = client.get("/", params=[("tag", "RPG"), ("tag", "Indie")]).text
    assert "of 7 games matching your filters" in html
    assert set(shown_appids(html)) == {102, 104, 106, 108, 110, 112, 114}
    assert 'value="Indie" checked' in html and 'value="RPG" checked' in html
    assert re.search(r'<span class="option-name">Indie</span>\s*<span class="option-count">7</span>', html)
    assert 'value="Action"' not in html  # no game in these results has the Action tag


def test_label_filter_matches_any_selected_label_including_unclassified(client):
    indie = client.get("/", params={"label": "Indie"}).text
    assert shown_appids(indie) == [102]
    mixed = client.get("/", params=[("label", "AAA"), ("label", "Unclassified")]).text
    assert 102 not in shown_appids(mixed) and shown_appids(mixed)[0] == 101
    assert re.search(r'<span class="option-name">Unclassified</span>\s*<span class="option-count">12</span>', mixed)
    assert client.get("/", params={"label": "Platinum"}).text.count('<article class="card"') == PAGE_SIZE  # ignored


def test_filter_counts_follow_the_view(dated):
    def count(html, name):
        return re.search(rf'<span class="option-name">{name}</span>\s*<span class="option-count">(\d+)</span>', html).group(1)

    top, everything = dated.get("/").text, dated.get("/", params={"view": "all"}).text
    assert (count(top, "AA"), count(top, "AAA"), count(top, "Unclassified")) == ("1", "0", "2")
    assert (count(everything, "AA"), count(everything, "AAA"), count(everything, "Unclassified")) == ("2", "1", "5")
    assert ">Any listed award (0)</option>" in top and ">Any listed award (1)</option>" in everything


def test_award_filter_by_category_and_winners(client):
    any_award = client.get("/", params={"award": "any"}).text
    assert sorted(shown_appids(any_award)) == [101, 102]  # the needs_review award is not shown
    assert shown_appids(client.get("/", params={"award": "Best Independent Game"}).text) == [102]
    assert shown_appids(client.get("/", params={"winners": "1"}).text) == [101]
    assert shown_appids(client.get("/", params={"award": "Best Independent Game", "winners": "1"}).text) == []
    assert ">Any listed award (2)</option>" in client.get("/").text


def test_sort_options_reorder_the_results(client):
    by_name = shown_appids(client.get("/", params={"sort": "name"}).text)
    assert by_name == list(range(101, 101 + PAGE_SIZE))
    assert shown_appids(client.get("/", params={"sort": "reviews"}).text)[0] == 114  # 1,400 + 300 reviews
    assert shown_appids(client.get("/", params={"sort": "positive"}).text)[0] == 114
    assert '<option value="name" selected>Name (A–Z)</option>' in client.get("/?sort=name").text
    assert '<option value="rank" selected>Rank (tier, then positive reviews)</option>' in client.get("/").text
    assert shown_appids(client.get("/", params={"sort": "bogus"}).text)[0] == 101  # falls back to rank


def test_pagination_keeps_search_filters_and_sort(client):
    params = [("q", "game"), ("tag", "RPG"), ("label", "Unclassified"), ("sort", "name")]
    single = client.get("/", params=params).text
    assert "Showing 1–12 of 12 games" in single and 'rel="next"' not in single  # one page: no pagination
    paged = client.get("/", params=[("q", "game"), ("tag", "RPG"), ("sort", "name")]).text
    assert "Showing 1–12 of 14 games" in paged
    assert 'href="/?q=game&amp;tag=RPG&amp;sort=name&amp;page=2" rel="next"' in paged
    second = client.get("/?q=game&tag=RPG&sort=name&page=2").text
    assert shown_appids(second) == [113, 114]
    assert 'href="/?q=game&amp;tag=RPG&amp;sort=name" rel="prev"' in second
    assert re.search(r'aria-current="page"><span class="visually-hidden">Page </span>2<', second)
    assert 'href="/games/113?q=game&amp;tag=RPG&amp;sort=name&amp;page=2"' in second  # details keep the context


def test_out_of_range_or_invalid_page_numbers_are_handled(client):
    response = client.get("/?q=game&page=99", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?q=game&page=2"
    assert "Showing 1–12" in client.get("/?page=abc").text


def test_no_matches_offers_to_clear_the_filters(client):
    html = client.get("/", params={"q": "no such game"}).text
    assert "No matching games" in html and "No games match “no such game”" in html
    assert '<a class="text-link" href="/">Clear filters</a>' in html
    assert "matching game in All games" not in html  # nothing matches outside the window either


def test_active_filters_can_be_removed_one_at_a_time(client):
    html = client.get("/", params=[("q", "game"), ("tag", "RPG"), ("label", "Indie")]).text
    assert 'href="/?q=game&amp;label=Indie"' in html  # remove the RPG tag
    assert 'href="/?tag=RPG&amp;label=Indie"' in html  # remove the search text


# ---------------------------------------------------------------------- details


def test_details_panel_shows_everything_about_one_game(client):
    response = client.get("/games/101/panel")
    assert response.status_code == 200
    html = response.text
    assert 'id="dialog-heading" tabindex="-1">Game 01</h2>' in html
    assert "Four-year rank #1 · AAA tier" in text_of(html)
    assert re.search(r'<a class="label" href="https://example.invalid/budget"[^>]*>AAA', html)
    assert '<a href="/?tag=RPG"' in html and '<a href="/?tag=Action"' in html
    assert "<dt>Developer</dt><dd>Test Studio</dd>" in html and "<dd>1 Jan 2024</dd>" in html
    assert "Game 01 is a test game about testing &amp; more." in html
    assert "The Game Awards 2023 · Game of the Year" in html and 'href="https://example.invalid/winners"' in html
    assert 'class="button button-primary button-large" href="https://store.steampowered.com/app/101/"' in html
    assert "100 positive reviews · 9% of 1,050 Steam reviews · Mixed" in text_of(html)
    assert html.count('<li class="excerpt">') == 6
    assert "Positive review number 2 for Game 01." in html and "Negative review number 0 for Game 01." in html
    assert "balanced sample, not the overall ratio" in html
    assert 'href="https://steamcommunity.com/app/101/reviews/"' in html
    assert "You explore a test world." in html  # the full store description, collapsed
    assert "<html" not in html  # a fragment, not a page


def test_details_media_is_an_honestly_labelled_trailer_clip(client):
    html = client.get("/games/101/panel").text
    assert '<video data-clip muted loop playsinline controls preload="none"' in html
    assert 'src="https://video.akamai.steamstatic.com/store_trailers/101/0/abc0/1/microtrailer.webm"' in html
    assert 'poster="https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1010/movie_full.jpg?t=1"' in html
    assert "Looping clip from the Steam store trailer “Launch Trailer”" in html
    assert "may not show actual gameplay" in html
    assert 'data-clip-toggle hidden>Pause clip</button>' in html  # shown by the script, which handles reduced motion
    assert "Gameplay preview" not in html
    gameplay = client.get("/games/107/panel").text
    assert "“Gameplay Overview”" in gameplay and "microtrailer.webm" in gameplay  # a gameplay-titled trailer wins


def test_details_media_falls_back_to_a_still_image(client):
    for appid in (105, 106):  # no trailer; only a trailer Steam does not mark as all-ages
        html = client.get(f"/games/{appid}/panel").text
        assert "<video" not in html
        assert f'src="https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/ss_first.jpg?t=1"' in html
        assert "Screenshot from the Steam store page." in html and "No trailer clip is available" in html


def test_details_page_works_without_javascript_and_links_back_to_the_results(client):
    html = client.get("/games/102?tag=Indie&sort=name&page=1").text
    assert '<a class="text-link" href="/?tag=Indie&amp;sort=name#game-102">← Back to results</a>' in html
    assert 'id="game-heading" tabindex="-1">Game 02</h2>' in html
    assert "For the original release.</strong> The nomination went to the original game." in html
    assert "<title>Game 02 | Game Librarian</title>" in html


def test_unknown_games_return_not_found(client):
    assert client.get("/games/999/panel").status_code == 404
    assert "Game not found" in client.get("/games/999").text
    assert client.get("/games/999").status_code == 404


def test_long_review_excerpts_are_shortened_in_the_details(settings, fake_steam, clock):
    fake_steam.add_game(400, "Wordy Game")
    fake_steam.reviews[400]["positive"] = [review(1, "A long and thoughtful review. " * 30, voted_up=True)]
    run_test_sync(settings, fake_steam, clock, [CuratedGame(400, "Wordy Game", "")])
    html = TestClient(make_app(settings)).get("/games/400/panel").text
    excerpt = re.search(r'<li class="excerpt">.*?</li>', html, re.S).group(0)
    assert "· shortened" in excerpt and len(re.search(r"<p>(.*?)</p>", excerpt, re.S).group(1)) <= 281
    assert "Only 1 positive English review was available." in html


# ---------------------------------------------------------------------- dashboard


def test_dashboard_explains_setup_until_a_report_is_connected(settings):
    html = TestClient(make_app(settings)).get("/dashboard").text
    assert "The Power BI report is not connected yet" in html
    assert "python -m game_librarian export-report" in html and "reporting/GameLibrarian.pbip" in html
    assert "GAME_LIBRARIAN_DASHBOARD_URL" in html and "README.md" in html
    assert "<iframe" not in html
    assert "the ranking rules" in html and "ranking formula" not in html


def test_dashboard_embeds_secure_links_warns_about_public_ones_and_links_to_others(settings):
    secure = "https://app.powerbi.com/reportEmbed?reportId=abc&autoAuth=true&ctid=def"
    html = TestClient(make_app(replace(settings, dashboard_url=secure))).get("/dashboard").text
    assert f'<iframe title="Game Librarian Power BI report" src="{secure.replace("&", "&amp;")}"' in html
    assert "Viewers sign in to Power BI" in html

    public = TestClient(make_app(replace(settings, dashboard_url="https://app.powerbi.com/view?r=xyz")))
    html = public.get("/dashboard").text
    assert "<iframe" in html and "Publish to web" in html and "anyone who has it can view" in html

    report = "https://app.powerbi.com/groups/me/reports/abc/ReportSection"
    html = TestClient(make_app(replace(settings, dashboard_url=report))).get("/dashboard").text
    assert "<iframe" not in html and f'href="{report}"' in html and "Sign in with an account" in html

    with pytest.raises(ValueError, match="HTTPS"):
        create_app(replace(settings, dashboard_url="javascript:alert(1)"))


def test_dashboard_link_is_the_same_whether_or_not_a_report_is_configured(settings):
    configured = replace(settings, dashboard_url="https://app.powerbi.com/groups/me/reports/abc")
    for app_settings in (settings, configured):
        assert re.search(r'href="/dashboard"\s*>Dashboard</a>', TestClient(make_app(app_settings)).get("/").text)


# ---------------------------------------------------------------------- robustness


def test_pages_render_without_any_network_access(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("page requests must not call Steam or any other network service")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(SteamClient, "get_json", refuse)
    for url in ("/", "/?q=game&tag=RPG&page=2", "/?view=all&sort=rank", "/games/101/panel", "/games/101", "/dashboard"):
        assert client.get(url).status_code == 200, url


def test_the_window_follows_the_date_each_request_is_served(settings, games):
    days = iter([date(2026, 11, 9), date(2028, 1, 2)])  # 1 Jan 2024 leaves the window on 2 Jan 2028
    app = TestClient(create_app(settings, today=lambda: next(days)))
    assert len(shown_appids(app.get("/").text)) == PAGE_SIZE
    later = app.get("/").text
    assert shown_appids(later) == [] and "2 Jan 2024 – 2 Jan 2028" in later


def test_failed_sync_is_reported_while_saved_data_is_still_shown(client, settings, fake_steam, clock, games):
    fake_steam.down = True
    clock.advance(days=8)
    run_test_sync(settings, fake_steam, clock, games)

    html = client.get("/").text
    assert 'class="status-failed">failed</strong>' in html
    assert "Steam looks unavailable" in html and "Showing data saved by the sync of" in html
    assert card_rank(html, 101) == "#1"  # ranks still come from the last good review counts
    panel = client.get("/games/101/panel").text  # the sync stopped after the first games' refreshes failed
    assert "The last refresh of these store details failed" in panel
    assert "You explore a test world." in panel and "9% of 1,050" in panel


def test_game_text_is_escaped(settings, fake_steam, clock):
    fake_steam.add_game(500, "<script>alert(1)</script> Quest")
    run_test_sync(settings, fake_steam, clock, [CuratedGame(500, "Script Quest", "")])
    app = TestClient(make_app(settings))
    for url in ("/", "/games/500/panel"):
        html = app.get(url).text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt; Quest" in html


def test_reviews_unavailable_states_are_explained(settings, fake_steam, clock):
    fake_steam.add_game(600, "Quiet Game", positive=0, negative=0, totals=(0, 0, None))
    run_test_sync(settings, fake_steam, clock, [CuratedGame(600, "Quiet Game", "")])
    app = TestClient(make_app(settings))
    assert "No Steam reviews yet" in card_html(app.get("/").text, 600)
    panel = app.get("/games/600/panel").text
    assert "No Steam user reviews yet." in panel and '<li class="excerpt">' not in panel
    assert "<strong>Unranked</strong> · This game has no Steam reviews yet." in panel
