"""Web app: catalog cards, the five-year ranking, views, search, filters, sorting, pagination, details, dashboard."""

import csv
import json
import re
from datetime import date
from html import unescape

import httpx
import pytest
from fastapi.testclient import TestClient

from game_librarian import store
from game_librarian.curated import CuratedGame
from game_librarian.dashboard import compact_number, format_rate
from game_librarian.report_export import export_report
from game_librarian.steam import SteamClient
from game_librarian.sync import SyncOptions, run_sync
from game_librarian.web import PAGE_SIZE, create_app
from tests.conftest import review, write_csv

TODAY = date(2026, 11, 9)  # the five-year window is 1 Jan 2021 – 9 Nov 2026
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
    """The web app with a fixed date, so the five-year window never depends on when the tests run."""
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
    """Fourteen synced games, all released 1 Jan 2024 (inside the window). Game n has 100 × n positive of
    1,000 + 50 × n reviews, so a higher n has a higher positive share.

    Game 01 (verified AAA) won The Game Awards' Game of the Year and Game 02 (verified AA and Indie) was
    nominated for an independent-game award (for the original release), so the five-year ranking is
    Game 01, Game 02, then Game 14 down to Game 03 (no award recorded, best Steam reception first;
    Game 03's award row is not verified). Odd games are tagged RPG + Action, even games RPG + Indie.
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


def set_modes(settings, modes_by_appid):
    conn = store.connect(settings.db_path)
    try:
        for appid, modes in modes_by_appid.items():
            conn.execute("UPDATE games SET modes = ? WHERE appid = ?", (json.dumps(modes), appid))
        conn.commit()
    finally:
        conn.close()


def test_mode_filters_match_all_and_keep_query(client, settings):
    set_modes(settings, {101: ["Single-player", "Online Co-op"], 102: ["Single-player"]})
    response = client.get('/', params=[('view', 'all'), ('mode', 'Single-player'), ('mode', 'Online Co-op')])
    assert response.status_code == 200
    assert 'Game 01' in response.text
    assert 'Game 02' not in response.text
    assert 'name="mode" value="Online Co-op" checked' in response.text
    assert 'mode=Single-player' in response.text


def test_mode_filters_are_kept_by_search_sort_views_pages_and_details(client, settings):
    # Every fixture game has Single-player and Online Co-op (the fake Steam's default categories).
    set_modes(settings, {103: ["Single-player"], 104: []})  # 104: Steam lists no mode data
    params = [("mode", "Single-player"), ("mode", "Online Co-op"), ("sort", "name")]
    html = client.get("/", params=params).text
    assert 103 not in shown_appids(html) and 104 not in shown_appids(html)  # all selected modes are required
    assert "Showing 1–12 of 12 games matching your filters" in text_of(html)
    assert re.search(r'<span class="option-name">Online Co-op</span>\s*<span class="option-count">12</span>', html)
    kept = "mode=Single-player&amp;mode=Online+Co-op"
    assert f'href="/?{kept}&amp;view=all"' in view_switch(html)  # views (A-Z is All games' own default order)
    assert f'href="/?{kept}&amp;sort=name&amp;view=goty"' in html  # collections
    assert f'href="/games/101?{kept}&amp;sort=name"' in html  # details links
    assert 'name="mode" value="Single-player" checked' in html  # the search and sort form submits them too
    back = client.get("/games/101", params=params).text
    assert f'href="/?{kept}&amp;sort=name#game-101"' in back  # and the details page links back with them
    assert "Games whose Steam page lists no mode data are left out when a mode is selected; that does not mean" in html
    panel = client.get("/games/104/panel").text
    assert "<dt>Modes</dt><dd><span class=\"muted\"" in panel and "Not listed on Steam" in panel


def test_wishlist_page_and_details_button(client):
    page = client.get('/wishlist')
    assert page.status_code == 200
    assert 'data-wishlist-list' in page.text
    details = client.get('/games/101/panel')
    assert details.status_code == 200
    assert 'data-wishlist-toggle' in details.text
    assert 'data-appid="101"' in details.text


def test_the_wishlist_explains_that_it_is_browser_local_and_not_steam(client):
    page = text_of(client.get("/wishlist").text)
    for phrase in ("saved only in this browser on this device", "does not sync to other browsers or devices",
                   "It is not your Steam wishlist", "Clearing this site's browser data", "can erase it",
                   "Nothing about it is sent to the Game Librarian server", "The wishlist needs JavaScript"):
        assert phrase in page, phrase
    assert '<a class="nav-icon" href="/wishlist" aria-label="Wishlist">' in client.get("/").text  # the header's star
    panel = client.get("/games/101/panel").text
    assert 'aria-describedby="wishlist-note-101"' in panel and "saved in this browser only" in panel
    assert '<script src="/static/wishlist.js?v=' in client.get("/").text


# ---------------------------------------------------------------------- collections


def test_collections_are_listed_with_counts_and_keep_the_filters(client):
    html = client.get("/", params={"tag": "RPG"}).text
    nav = re.search(r'<nav class="collection-switch".*?</nav>', html, re.S).group(0)
    assert text_of(nav) == ("Collections Game of the Year winners 1 game Award winners & nominees 2 games "
                            "Highly rated indies 0 games Big-budget games 2 games Hidden gems 0 games "
                            "Recent releases 0 games")
    assert 'href="/?tag=RPG&amp;view=goty"' in nav


def test_the_goty_collection_lists_verified_top_award_wins_with_global_ranks(client):
    html = client.get("/", params={"view": "goty"}).text
    assert shown_appids(html) == [101]  # 102 was only nominated, in another category; 103's row is unverified
    assert "<h2>Game of the Year winners</h2>" in html
    assert "Games with a verified win in an organization's top category" in text_of(html)
    assert "A number (#) is the game’s five-year rank among all catalog games, not its place in this collection." \
        in text_of(html)
    assert card_rank(html, 101) == "#1 in the five-year ranking"  # the same number as everywhere else
    assert '<input type="hidden" name="view" value="goty">' in html
    assert '<a class="collection-link" href="/?view=goty" aria-current="page">' in html


def test_collections_use_their_own_default_sort_and_explain_empty_results(client):
    assert shown_appids(client.get("/", params={"view": "big-budget"}).text) == [102, 101]  # most positive first
    assert shown_appids(client.get("/", params={"view": "awards"}).text) == [101, 102]
    empty = client.get("/", params={"view": "hidden-gems"}).text
    assert shown_appids(empty) == [] and "No games in this collection yet" in empty
    filtered = client.get("/", params={"view": "big-budget", "q": "nothing like this"}).text
    assert "with the selected filters in this collection" in text_of(filtered)


# ---------------------------------------------------------------------- awards from several organizations


@pytest.fixture
def multi_award_client(settings, games):
    write_csv(settings.curated_dir / "awards.csv", [
        AWARD,  # The Game Awards 2023 Game of the Year: 101 won
        {**AWARD, "organization": "BAFTA Games Awards", "category": "Best Game", "year": "2024", "result": "nominee",
         "steam_appid": "103", "award_title": "Game 03"},
        {**AWARD, "organization": "The Steam Awards", "result": "nominee", "steam_appid": "104", "award_title": "Game 04"},
        {**AWARD, "organization": "The Steam Awards", "category": "Labor of Love", "steam_appid": "105",
         "award_title": "Game 05"},
    ])
    return TestClient(make_app(settings))


def test_award_filters_distinguish_organizations_categories_and_years(multi_award_client):
    def shown(**params):
        return sorted(shown_appids(multi_award_client.get("/", params={"view": "all", **params}).text))

    assert shown(award="Game of the Year") == [101, 104]  # the category name is shared by two organizations
    assert shown(award_org="The Steam Awards", award="Game of the Year") == [104]
    assert shown(award_org="The Steam Awards") == [104, 105]
    assert shown(award_org="The Steam Awards", winners="1") == [105]
    assert shown(award_year="2024") == [103]
    assert shown(award_org="Nobody", award_year="1999") == list(range(101, 113))  # unknown values are ignored
    assert shown(view="goty") == [101]  # the Steam nominee is not a winner; Labor of Love is not a top category


def test_award_options_count_games_and_links_keep_the_award_choices(multi_award_client):
    html = multi_award_client.get("/", params={"view": "all", "award_org": "The Steam Awards"}).text
    assert '<option value="The Steam Awards" selected>The Steam Awards (2)</option>' in html
    assert '<option value="BAFTA Games Awards">BAFTA Games Awards (1)</option>' in html
    assert ">Game of the Year (1)</option>" in html and ">Labor of Love (1)</option>" in html  # Steam's categories
    assert ">2023 (2)</option>" in html and ">2024 (0)</option>" in html
    assert "Verified results from The Game Awards, BAFTA Games Awards, The Steam Awards." in text_of(html)
    chips = re.search(r'<ul class="active-filters".*?</ul>', html, re.S).group(0)
    assert "Award: The Steam Awards" in text_of(chips) and 'href="/?view=all"' in chips
    won = multi_award_client.get("/", params={"view": "all", "award_org": "The Steam Awards", "award": "Labor of Love",
                                              "winners": "1", "award_year": "2023"}).text
    assert "Won The Steam Awards · Labor of Love · 2023" in text_of(won)
    assert 'href="/games/105?award=Labor+of+Love&amp;award_org=The+Steam+Awards&amp;award_year=2023&amp;winners=1&amp;view=all"' in won


def test_cards_and_details_name_the_organization_of_every_award(multi_award_client):
    html = multi_award_client.get("/", params={"view": "all"}).text
    assert "BAFTA Games Awards 2024 · Best Game · Nominee" in text_of(card_html(html, 103))
    assert "The Steam Awards 2023 · Labor of Love · Winner" in text_of(card_html(html, 105))
    panel = text_of(multi_award_client.get("/games/104/panel").text)
    assert "Nominee The Steam Awards 2023 · Game of the Year Source" in panel


# ---------------------------------------------------------------------- more games like this


@pytest.fixture
def related_client(settings, fake_steam, clock):
    """Six games: three deckbuilder-like games, two open-world games and one unrelated game."""
    tags = {801: (122, 3871, 1667, 1664, 1625), 802: (122, 3871, 1667, 1664, 4085), 803: (122, 3871, 1667, 21),
            804: (19, 1695, 3859), 805: (19, 1695, 1742), 806: (492,)}
    games = []
    for appid, tag_ids in tags.items():
        fake_steam.add_game(appid, f"Related {appid}", tags=tuple((tag, 1000 - n) for n, tag in enumerate(tag_ids)))
        games.append(CuratedGame(appid, f"Related {appid}", ""))
    run_test_sync(settings, fake_steam, clock, games)
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": str(g.appid), "title": g.title} for g in games])
    write_csv(settings.curated_dir / "awards.csv", [{**AWARD, "steam_appid": "806"}])
    write_csv(settings.curated_dir / "classifications.csv", [{**LABEL, "steam_appid": "806"}])
    return TestClient(make_app(settings))


def related_section(html):
    return re.search(r'<section class="details-section related".*?</section>', html, re.S).group(0)


def test_details_suggest_related_games_with_reasons_and_keep_the_browsing_context(related_client):
    section = related_section(related_client.get("/games/801/panel", params={"view": "all", "tag": "RPG", "page": "1"}).text)
    assert "More games like this" in section
    assert re.findall(r'data-details="(\d+)"', section) == ["802", "803"]  # 4 shared tags, then 3; never 801 itself
    assert 'href="/games/802?tag=RPG&amp;view=all"' in section  # the list context travels with the link
    assert "Shares 4 tags, including RPG, 2D and Horror" in section and "Shares RPG, 2D and Horror tags" in section
    page = related_client.get("/games/801", params={"view": "all"}).text  # the full-page fallback has them too
    assert 'href="/games/802?view=all"' in related_section(page)
    assert "View on Steam" in page and "Steam reviews" in page  # the rest of the details stay


def test_details_say_so_when_no_game_is_similar_enough(related_client):
    section = related_section(related_client.get("/games/804/panel").text)  # 805 shares only two tags
    assert "data-details" not in section
    assert "No other catalog game shares at least 3 of this game's most-voted Steam tags" in text_of(section)


def test_promoted_games_are_counted_in_the_catalog_status(client, settings):
    write_csv(settings.curated_dir / "promoted_games.csv", [
        {"steam_appid": "114", "title": "Game 14", "source": "discovery", "match_method": "steam_app",
         "evidence": "Steam game.", "promoted_on": "2026-11-15"}])
    write_csv(settings.curated_dir / "games.csv", [{"steam_appid": str(appid), "title": f"Game {appid - 100:02d}"}
                                                    for appid in range(101, 114)])
    assert "14 games with Steam data (1 added through candidate review)." in text_of(client.get("/").text)


# appid, name, Steam release-date text, coming soon, positive reviews, verified label
DATED_GAMES = [
    (301, "Cutoff Day", "Jan 1, 2021", False, 5_000, None),  # in: exactly the first day of the window
    (302, "Day Before Cutoff", "Dec 31, 2020", False, 900_000, "AAA"),  # out: one day too old
    (303, "Released Today", "9 Nov, 2026", False, 3_000, None),  # in: today, in Steam's day-first format
    (304, "Tomorrow", "Nov 10, 2026", False, 1_000, None),  # out: a future date
    (305, "Soon", "Coming soon", True, 0, None),  # out: not released yet
    (306, "Quarter Only", "Q4 2025", False, 2_000, None),  # out: no exact release date
    (307, "Classic", "Apr 18, 2011", False, 800_000, "AA"),  # out: older
    (308, "Recent AA", "Mar 2, 2025", False, 100, "AA"),  # in: its label does not lift it; under 500 reviews
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
    """The visible rank line of one card: '#3', '#3 in the five-year ranking', 'Unranked', ..."""
    rank = re.search(r'<p class="card-rank">(.*?)</p>', card_html(html, appid), re.S).group(1)
    return text_of(re.sub(r'<span class="visually-hidden">.*?</span>', "", rank))


def shown_appids(html: str) -> list[int]:
    return [int(appid) for appid in re.findall(r'<article class="card" id="game-(\d+)"', html)]


def text_of(html: str) -> str:
    """Visible-ish text with tags removed and whitespace collapsed, for phrase checks.

    The space a removed tag leaves before punctuation is dropped: "<strong>x</strong>:" -> "x:".
    Entities are decoded, as a browser shows them: "&amp;" -> "&", "&#39;" -> "'".
    """
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    return unescape(re.sub(r" ([.,:;)])", r"\1", text))


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


# ---------------------------------------------------------------------- cards and the five-year ranking


def test_card_shows_rank_image_title_label_release_tags_summary_reviews_awards_and_steam_link(client):
    card = card_html(client.get("/").text, 101)
    assert card_rank(card, 101) == "#1"
    assert "Award group: The Game Awards GOTY · 1 win" in text_of(card)  # why it is first
    assert 'title="Award group: The Game Awards — Game of the Year winner"' in card
    assert 'src="https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/101/library_600x900.jpg?t=1"' in card
    assert 'id="card-title-101">Game 01</h3>' in card
    assert '<li class="label">AAA</li>' in card
    assert '<p class="card-release">Released 1 Jan 2024</p>' in card
    assert "<li>RPG</li>" in card and "<li>Action</li>" in card
    assert "Game 01 is a test game about testing &amp; more." in card  # the first sentence of the short description
    # The positive count, the share (rounded down like Steam) and the total are all visible.
    assert "100 positive · 9% of 1,050 Steam reviews" in text_of(card)
    assert "The Game Awards 2023 · Game of the Year · Winner" in text_of(card)
    assert 'href="https://store.steampowered.com/app/101/"' in card and "View on Steam" in card
    assert 'data-details="101"' in card and 'href="/games/101"' in card
    assert "nofollow" not in card  # the Steam Web API terms forbid nofollow links to Valve


def test_only_verified_labels_show_a_badge_and_edition_awards_say_so(client):
    html = client.get("/?sort=name").text
    both = card_html(html, 102)
    assert '<li class="label">AA</li>' in both and '<li class="label">Indie</li>' in both
    assert "Best Independent Game · Nominee · for the original release" in text_of(both)
    for appid in (103, 104):  # needs_review AAA label and award; rejected Indie label
        card = card_html(html, appid)
        assert "label-list" not in card and "Unclassified" not in card  # no badge without a verified label
        assert "award-list" not in card


def test_games_are_ranked_by_award_group_then_steam_reception(client):
    html = client.get("/").text
    assert shown_appids(html) == [101, 102, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105]
    assert card_rank(html, 101) == "#1"  # Game of the Year at The Game Awards, although only 9% positive
    assert card_rank(html, 102) == "#2"  # a verified nomination (for the original release), 18% positive
    assert card_rank(html, 114) == "#3"  # no award recorded: the best positive share (82% of 1,700 reviews)
    assert "Award group: Nominated, no win (for the original release) · 1 nomination" in text_of(card_html(html, 102))
    assert "Award group: No award recorded · ranked by Steam reception" in text_of(card_html(html, 114))
    assert "<h2>Top-ranked games</h2>" in html
    assert "Showing 1–12 of 14 games · 14 of 14 games in the window are ranked." in text_of(html)
    second = client.get("/?page=2").text
    assert shown_appids(second) == [104, 103] and card_rank(second, 103) == "#14"


def test_search_and_filters_keep_each_games_five_year_rank(client):
    html = client.get("/", params={"q": "game 07"}).text
    assert shown_appids(html) == [107]
    assert card_rank(html, 107) == "#10"  # its place in the whole five-year ranking, not #1 of the results
    indie = client.get("/", params=[("tag", "Indie")]).text
    assert [card_rank(indie, appid) for appid in shown_appids(indie)] == ["#2", "#3", "#5", "#7", "#9", "#11", "#13"]
    assert card_rank(client.get("/", params={"label": "No verified label"}).text, 114) == "#3"
    assert card_rank(client.get("/", params={"label": "Unclassified"}).text, 114) == "#3"  # a pre-0.8 link
    assert card_rank(client.get("/", params={"award": "Best Independent Game"}).text, 102) == "#2"
    assert card_rank(client.get("/", params={"sort": "name"}).text, 105) == "#12"  # another order, same rank


def test_the_window_and_the_ranking_rules_are_explained_on_the_page(client):
    html = client.get("/").text
    scope = text_of(re.search(r'<p class="results-scope">.*?</p>', html, re.S).group(0))
    assert scope == ("Games released in the last five full years and this year so far: 1 Jan 2021 – 9 Nov 2026. "
                     "Ranked by awards and player reception.")
    tip = re.search(r'<details class="info-tip">.*?</details>', html, re.S).group(0)
    # A visible label names the control (the "i" mark is hidden from screen readers), and works without JavaScript.
    assert re.search(r'<summary><span class="info-icon" aria-hidden="true">i</span>'
                     r'<span class="info-text">How the ranking works</span></summary>', tip)
    assert 'href="/?view=all">All games</a>' in tip  # the link the short text no longer carries
    groups = re.search(r'<ol class="rank-groups">(.*?)</ol>', tip, re.S).group(1)
    # The groups come from ranking.py; the fixture has verified Game Awards wins and a nomination only.
    assert [text_of(item) for item in re.findall(r"<li>(.*?)</li>", groups, re.S)] == [
        "The Game Awards — Game of the Year winner",
        "BAFTA Games Awards — Best Game winner (no verified results recorded yet)",
        "D.I.C.E. Awards — Game of the Year winner (no verified results recorded yet)",
        "Game Developers Choice Awards — Game of the Year winner (no verified results recorded yet)",
        "The Steam Awards — Game of the Year winner (no verified results recorded yet)",
        "Other verified award winner (no verified results recorded yet)",
        "Verified nominations, no win",
        "No award recorded",
    ]
    text = text_of(tip)
    for phrase in (
        "Only games released from 1 Jan 2021 to today, 9 Nov 2026: the five full years before 2026, plus 2026 so far.",
        "Older games, games not released yet and games without an exact release date on Steam are not ranked; you "
        "find them under All games.",
        "The window moves forward every 1 January.",
        "A game’s highest-priority verified award result puts it in one of these groups. Every game in a group "
        "ranks above every game in a later group, whatever its reviews",
        "Within a group, more distinct verified wins rank higher; with equal wins, more distinct verified "
        "nominations. A nomination never counts as a win, and a result recorded twice counts once.",
        "each card names its group and counts; its first listed award is the one that sets the group.",
        "Steam reception breaks the remaining ties, as in Best player reception: games with at least 500 Steam "
        "reviews first, highest positive share first, then more reviews; then games with fewer reviews, in the "
        "same order; then games without a usable review summary.",
        "Last come the title A–Z and the Steam app ID, so every rank is distinct.",
        "Missing, zero or inconsistent review counts are never read as zero reviews or 0% positive.",
        "A game with a verified award keeps its award-based place without them",
        "A game with no award recorded and no usable review summary is Unranked: it has no number and is listed "
        "after the ranked games.",
        "This is Game Librarian’s editorial discovery ranking, not an objective measure of game quality.",
        "Award coverage is incomplete: the curated data covers selected organizations, categories and years, so "
        "“No award recorded” does not mean a game has never received an award.",
        "counts for the edition Steam sells and is marked “for the original release”.",
        "Production labels (AAA, AA, Indie) do not affect the rank",
        "Searching and filtering never change a game’s rank, and neither do the other sort orders or the "
        "collections: a number is always the game’s place among all catalog games in the window.",
        "Best player reception lists games with at least 500 Steam reviews first, highest positive share first, "
        "so a handful of reviews cannot lead",
        "Newest release uses exact Steam release dates up to today; unreleased games and games without an exact "
        "date follow A–Z.",
    ):
        assert phrase in text, phrase
    assert "tier" not in text.lower() and "Awards are not part of the rank" not in text  # the old rules are gone
    assert "adjusted score" not in html.lower() and "500 ×" not in html  # so is the older formula


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


# ---------------------------------------------------------------------- the five-year window and All games


def test_top_ranked_view_shows_only_games_released_in_the_window(dated):
    html = dated.get("/").text
    # On the cutoff day (98% of 5,100 reviews), today (97% of 3,100), then the AA game: its label does not lift
    # it, and with 200 reviews it follows the games with at least 500.
    assert shown_appids(html) == [301, 303, 308]
    assert [card_rank(html, appid) for appid in (301, 303, 308)] == ["#1", "#2", "#3"]
    assert "Showing 1–3 of 3 games · 3 of 3 games in the window are ranked." in text_of(html)
    assert "Released 1 Jan 2021" in card_html(html, 301) and "Released 9 Nov 2026" in card_html(html, 303)


def test_all_games_view_lists_older_and_undated_games_without_a_number(dated):
    html = dated.get("/", params={"view": "all"}).text
    assert "<h2>All games</h2>" in html
    assert shown_appids(html) == [307, 301, 302, 306, 308, 303, 305, 304]  # A–Z: no ranking order is implied
    assert card_rank(html, 301) == "#1 in the five-year ranking" and card_rank(html, 308) == "#3 in the five-year ranking"
    for appid in (302, 304, 305, 306, 307):
        assert card_rank(html, appid) == "Not in the five-year ranking"
    assert ("A number (#) is a five-year rank (released 1 Jan 2021 – 9 Nov 2026); other games have none."
            in text_of(html))
    assert "Showing 1–8 of 8 games · 3 of 8 catalog games are in the five-year ranking." in text_of(html)
    assert "Released 18 Apr 2011" in card_html(html, 307)
    assert "Release: Coming soon" in card_html(html, 305) and "Released Q4 2025" in card_html(html, 306)
    assert "Release: 10 Nov 2026" in card_html(html, 304)  # a date after today is not "Released"
    assert "The Game Awards 2023 · Game of the Year · Winner" in text_of(card_html(html, 307))  # awards stay visible


def test_all_games_sorted_by_rank_lists_ranked_games_first_then_the_rest_a_to_z(dated):
    html = dated.get("/", params={"view": "all", "sort": "rank"}).text
    assert shown_appids(html) == [301, 303, 308, 307, 302, 306, 305, 304]  # 307 won an award, but in 2011
    assert "Ranked games come first; the others follow A–Z." in text_of(html)


def test_details_say_why_a_game_is_outside_the_five_year_ranking(dated):
    reasons = {
        302: "Released before the five-year window.",
        304: "Its release date is after today.",
        305: "Not released yet: Steam lists it as coming soon.",
        306: "Steam gives no exact release date (“Q4 2025”).",
    }
    for appid, reason in reasons.items():
        panel = text_of(dated.get(f"/games/{appid}/panel").text)
        assert f"Not in the five-year ranking · {reason}" in panel, appid
    assert "Five-year rank #3 · No award recorded · ranked by Steam reception" in text_of(
        dated.get("/games/308/panel").text)
    assert '<a href="/?tag=RPG&amp;view=all"' in dated.get("/games/307/panel").text  # the tag list includes this game


def test_search_and_filters_point_to_matching_games_outside_the_window(dated):
    html = dated.get("/", params={"q": "classic"}).text
    assert shown_appids(html) == []
    assert "No games match “classic” with the selected filters in the five-year ranking." in text_of(html)
    assert 'href="/?q=classic&amp;view=all">Show 1 matching game in All games</a>' in html
    found = dated.get("/", params={"q": "classic", "view": "all"}).text
    assert shown_appids(found) == [307] and card_rank(found, 307) == "Not in the five-year ranking"
    assert shown_appids(dated.get("/", params={"award": "any", "view": "all"}).text) == [307]
    recent = dated.get("/", params={"q": "cutoff"}).text  # "Cutoff Day" is in the window, "Day Before Cutoff" is not
    assert shown_appids(recent) == [301]
    assert 'href="/?q=cutoff&amp;view=all">1 more matching game is outside the five-year ranking</a>' in recent


def test_a_catalog_without_recent_games_points_to_all_games(settings, fake_steam, clock):
    fake_steam.add_game(700, "Old Game", release_date={"coming_soon": False, "date": "Apr 18, 2011"})
    run_test_sync(settings, fake_steam, clock, [CuratedGame(700, "Old Game", "")])
    html = TestClient(make_app(settings)).get("/").text
    assert "No games in the five-year window" in html and 'href="/?view=all">Show all games</a>' in html


def test_view_tabs_keep_the_search_and_filters_and_count_their_games(dated):
    html = dated.get("/", params={"q": "cutoff"}).text
    tabs = view_switch(html)
    assert 'href="/?q=cutoff" aria-current="page"' in tabs and 'href="/?q=cutoff&amp;view=all"' in tabs
    assert text_of(tabs) == "Top-ranked · since 2021 1 game All games 2 games"


def test_switching_views_keeps_only_a_deliberately_chosen_sort(client):
    assert 'href="/?view=all"' in view_switch(client.get("/").text)  # All games opens in its own default, A–Z
    assert 'href="/?view=all"' in view_switch(client.get("/", params={"sort": "rank"}).text)  # as sent by the form
    assert 'href="/?sort=most-positive&amp;view=all"' in view_switch(client.get("/", params={"sort": "most-positive"}).text)
    # Sort values from before version 0.4 still work, under their new names.
    assert 'href="/?sort=most-positive&amp;view=all"' in view_switch(client.get("/", params={"sort": "reviews"}).text)
    assert 'href="/?sort=reception&amp;view=all"' in view_switch(client.get("/", params={"sort": "positive"}).text)
    assert 'href="/"' in view_switch(client.get("/", params={"view": "all", "sort": "name"}).text)


def test_all_games_view_keeps_its_view_in_pagination_forms_and_details(client):
    html = client.get("/", params={"view": "all"}).text
    assert shown_appids(html) == list(range(101, 101 + PAGE_SIZE))
    assert '<input type="hidden" name="view" value="all">' in html
    assert 'href="/?view=all&amp;page=2" rel="next"' in html
    second = client.get("/?view=all&page=2").text
    assert shown_appids(second) == [113, 114] and card_rank(second, 113) == "#4 in the five-year ranking"
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


def test_label_filter_matches_any_selected_label_including_no_verified_label(client):
    indie = client.get("/", params={"label": "Indie"}).text
    assert shown_appids(indie) == [102]
    mixed = client.get("/", params=[("label", "AAA"), ("label", "No verified label")]).text
    assert 102 not in shown_appids(mixed) and shown_appids(mixed)[0] == 101
    assert re.search(r'<span class="option-name">No verified label</span>\s*<span class="option-count">12</span>', mixed)
    assert 'name="label" value="No verified label" checked' in mixed and "Unclassified" not in mixed
    # "Unclassified" was the choice's name before 0.8: an older link still selects it and shows the same games.
    legacy = client.get("/", params=[("label", "AAA"), ("label", "Unclassified")]).text
    assert shown_appids(legacy) == shown_appids(mixed)
    assert 'name="label" value="No verified label" checked' in legacy and "label=No+verified+label" in legacy
    assert client.get("/", params={"label": "Platinum"}).text.count('<article class="card"') == PAGE_SIZE  # ignored


def test_filter_counts_follow_the_view(dated):
    def count(html, name):
        return re.search(rf'<span class="option-name">{name}</span>\s*<span class="option-count">(\d+)</span>', html).group(1)

    top, everything = dated.get("/").text, dated.get("/", params={"view": "all"}).text
    assert (count(top, "AA"), count(top, "AAA"), count(top, "No verified label")) == ("1", "0", "2")
    assert (count(everything, "AA"), count(everything, "AAA"), count(everything, "No verified label")) == ("2", "1", "5")
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
    assert shown_appids(client.get("/", params={"sort": "most-positive"}).text)[0] == 114  # 1,400 positive reviews
    assert shown_appids(client.get("/", params={"sort": "reception"}).text)[0] == 114  # 82% of 1,700
    assert '<option value="name" selected>Name (A–Z)</option>' in client.get("/?sort=name").text
    assert '<option value="rank" selected>Five-year rank (awards, then reception)</option>' in client.get("/").text
    for key in ("most-positive", "reception", "newest"):
        assert f'<option value="{key}"' in client.get("/").text
    assert shown_appids(client.get("/", params={"sort": "bogus"}).text)[0] == 101  # falls back to rank


def test_pagination_keeps_search_filters_and_sort(client):
    params = [("q", "game"), ("tag", "RPG"), ("label", "No verified label"), ("sort", "name")]
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
    assert "Five-year rank #1 · The Game Awards — Game of the Year winner · 1 win" in text_of(html)
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


def kpis(html: str) -> dict[str, str]:
    """The dashboard's summary cards: label -> shown value."""
    return dict(re.findall(r'<div class="kpi"><dt>([^<]+)</dt><dd>([^<]+)</dd>', html))


def ranking_titles(html: str) -> list[tuple[str, str]]:
    """(rank, game) for each row of the dashboard's five-year ranking table."""
    return re.findall(r'<tr><td class="num">(\d+)</td><td><a class="text-link" href="/games/\d+">([^<]+)</a>', html)


def test_dashboard_shows_player_reception_as_html_without_power_bi(client):
    html = client.get("/dashboard/reception").text
    assert "<title>Player reception | Dashboard | Game Librarian</title>" in html
    assert re.search(r'href="/dashboard/reception" aria-current="page">Player reception</a>', html)
    assert "<iframe" not in html and "GAME_LIBRARIAN_DASHBOARD_URL" not in html
    assert "14 of 14 catalog games in total." in html
    assert "Five-year ranking window: released 1 Jan 2021 – 9 Nov 2026." in text_of(html)
    # 14 games: 10,500 positive of 19,250 reviews pooled (Game n: 100 × n positive of 1,000 + 50 × n).
    assert kpis(html) == {"Games with review data": "14", "Total Steam reviews": "19.2K",
                          "Review-weighted positive rate": "54.5%", "Average game positive rate": "51.3%"}
    assert html.count('class="dot"') == 14
    assert 'title="Game 14: 1,700 reviews, 82% positive, #3 in the five-year ranking"' in html
    expected = [("1", "Game 01"), ("2", "Game 02")] + [(str(rank), f"Game {17 - rank:02d}") for rank in range(3, 15)]
    assert ranking_titles(html) == expected  # the winner, the nominee, then the best Steam reception
    bands = dict(re.findall(r'<dt>([^<]+)</dt>\s*<dd class="bar-line">.*?<span class="bar-value">(\d+)</span>', html, re.S))
    assert bands == {"95–100%": "0", "90–94%": "0", "80–89%": "1", "70–79%": "3", "Below 70%": "10",
                     "No usable review data": "0"}
    labels = re.findall(r'<dt>([^<]+?) <span class="muted">· (\d+) games?</span></dt>', html)
    assert labels == [("AAA", "1"), ("AA + Indie", "1"), ("No verified label", "12")]


def test_dashboard_filters_keep_ranks_and_ignore_unknown_values(client):
    html = client.get("/dashboard/reception?tag=Indie").text  # the even games
    assert "7 of 14 catalog games match the filters." in html
    assert '<option value="Indie" selected>Indie (7)</option>' in html
    assert ranking_titles(html) == [("2", "Game 02"), ("3", "Game 14"), ("5", "Game 12"), ("7", "Game 10"),
                                    ("9", "Game 08"), ("11", "Game 06"), ("13", "Game 04")]
    assert '<a class="text-link" href="/dashboard/reception">Clear filters</a>' in html
    assert ranking_titles(client.get("/dashboard/reception?label=AAA").text) == [("1", "Game 01")]
    assert ranking_titles(client.get("/dashboard/reception?award_status=Nominee").text) == [("2", "Game 02")]
    unknown = client.get("/dashboard/reception?tag=Nope&label=Legendary&award_status=Champion").text
    assert "14 of 14 catalog games in total." in unknown and "Clear filters" not in unknown


def test_dashboard_awards_page_lists_verified_entries_with_their_sources(client):
    html = client.get("/dashboard/awards").text
    assert re.search(r'href="/dashboard/awards" aria-current="page">Awards</a>', html)
    assert "3 of 3 verified award entries in total." in html  # the needs_review row is left out
    assert kpis(html) == {"Award entries": "3", "Winners": "2", "Nominees (did not win)": "1",
                          "Catalog games with awards": "2", "Entries matched to the catalog": "67%"}
    rows = re.findall(r"<tr>\s*<td>(\d{4})</td><td>([^<]+)</td><td>(\w+)</td><td>([^<]+)</td>\s*<td>([^<]+)</td>", html)
    assert rows == [
        ("2024", "The Game Awards · Best Independent Game", "Nominee", "Game 02 Original", "Original release"),
        ("2023", "The Game Awards · Game of the Year", "Winner", "Game 01", "This Steam app"),
        ("2021", "The Game Awards · Game of the Year", "Winner", "Console Exclusive", "No Steam app"),
    ]
    assert html.count('href="https://example.invalid/winners" target="_blank" rel="noopener noreferrer">Source') == 3
    assert '<a class="text-link" href="/games/102">Game 02</a>' in html
    assert re.search(r'title="2024: 0 winners, 1 nominee"', html)

    assert kpis(client.get("/dashboard/awards?result=Winner").text)["Award entries"] == "2"
    assert kpis(client.get("/dashboard/awards?match=Not+mapped+to+Steam").text)["Award entries"] == "1"
    assert kpis(client.get("/dashboard/awards?year=2023").text)["Award entries"] == "1"
    assert kpis(client.get("/dashboard/awards?year=1999").text)["Award entries"] == "3"  # not a listed year


def test_dashboard_about_page_describes_the_loaded_data(client):
    html = unescape(client.get("/dashboard/about").text)
    assert "<title>About the data | Dashboard | Game Librarian</title>" in html
    assert "They cover the 14 catalog games (14 hand-picked, 0 promoted from reviewed candidates)" in html
    assert "it is on the website only, not in the Power BI report" in html  # Trending games is not in Power BI
    assert ("The website ranks the five full years before 2026 plus 2026 so far. The Power BI report keeps its own "
            "window, the four years up to the export day, so its ranks can differ.") in html
    assert "<dt>Award rows left out (not verified)</dt><dd>1</dd>" in html
    coverage = re.findall(r"<tr><td>([^<]+)</td><td>([^<]+)</td><td class=\"num\">(\d+)</td><td class=\"num\">(\d+)</td></tr>", html)
    assert coverage == [("The Game Awards · Best Independent Game", "2024", "1", "0"),
                        ("The Game Awards · Game of the Year", "2021–2023", "2", "2")]


def test_dashboard_numbers_match_the_power_bi_export(client, settings, tmp_path):
    """The dashboard aggregates the rows export-report writes, so its cards equal the report's measures."""
    export_report(settings, tmp_path, today=TODAY)
    with (tmp_path / "games.csv").open(encoding="utf-8", newline="") as handle:
        games = [row for row in csv.DictReader(handle) if row["has_review_data"] == "true"]
    with (tmp_path / "awards.csv").open(encoding="utf-8", newline="") as handle:
        awards = list(csv.DictReader(handle))
    positive = sum(int(row["positive_reviews"]) for row in games)
    total = sum(int(row["total_reviews"]) for row in games)
    average = sum(float(row["positive_share"]) for row in games) / len(games)
    in_catalog = sum(row["catalog_status"] == "In catalog" for row in awards)
    assert kpis(client.get("/dashboard/reception").text) == {
        "Games with review data": str(len(games)), "Total Steam reviews": compact_number(total),
        "Review-weighted positive rate": format_rate(positive / total), "Average game positive rate": format_rate(average)}
    assert kpis(client.get("/dashboard/awards").text) == {
        "Award entries": str(len(awards)), "Winners": str(sum(row["is_winner"] == "true" for row in awards)),
        "Nominees (did not win)": str(sum(row["is_winner"] == "false" for row in awards)),
        "Catalog games with awards": str(len({row["catalog_appid"] for row in awards if row["catalog_appid"]})),
        "Entries matched to the catalog": format_rate(in_catalog / len(awards), 0)}


def test_dashboard_pages_explain_an_empty_catalog(settings):
    app = TestClient(make_app(settings))
    reception = app.get("/dashboard/reception").text
    assert "No sync has saved data yet" in reception and "0 of 0 catalog games in total." in reception
    assert kpis(reception)["Review-weighted positive rate"] == "No data"
    assert "No game with review data matches the filters." in reception and "No ranked game matches" in reception
    assert "No award entry matches the filters." in app.get("/dashboard/awards").text
    assert "No verified award entries yet." in app.get("/dashboard/about").text


# ---------------------------------------------------------------------- robustness


def test_pages_render_without_any_network_access(client, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("page requests must not call Steam or any other network service")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(SteamClient, "get_json", refuse)
    for url in ("/", "/?q=game&tag=RPG&page=2", "/?view=all&sort=rank", "/games/101/panel", "/games/101", "/dashboard",
                "/dashboard/reception", "/dashboard?tag=RPG", "/dashboard/awards?year=2023", "/dashboard/about"):
        assert client.get(url).status_code == 200, url


def test_the_window_follows_the_date_each_request_is_served(settings, games):
    days = iter([date(2029, 12, 31), date(2030, 1, 1)])  # 1 Jan 2024 leaves the window on 1 Jan 2030
    app = TestClient(create_app(settings, today=lambda: next(days)))
    assert len(shown_appids(app.get("/").text)) == PAGE_SIZE
    later = app.get("/").text
    assert shown_appids(later) == [] and "1 Jan 2025 – 1 Jan 2030" in later


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
    for url in ("/", "/games/500/panel", "/dashboard/reception"):  # a dot's title and a table show the name
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
    assert ("<strong>Unranked</strong> · No award recorded and no usable Steam review summary. "
            "This game has no Steam reviews yet.") in panel
