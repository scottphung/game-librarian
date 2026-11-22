"""Web pages added in version 0.5: publisher and reception filters, sale badges and prices in the details,
the player sort, the Trending page, the Trending games dashboard, Data Stories, and the navigation.

Uses the fourteen synced games of test_web.py (all released 1 Jan 2024; the fake Steam's sync saved a
$19.99 price without a discount and no player count for each)."""

import re
import shutil
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from game_librarian import store
from game_librarian.config import DEFAULT_CURATED_DIR
from game_librarian.curated import CuratedGame
from game_librarian.prepare import FREE, NOT_SOLD, PRICED, PriceFacts
from game_librarian.steam import SteamClient
from game_librarian.web import create_app
from tests.test_web import TODAY, card_html, games, run_test_sync, set_modes, shown_appids, text_of  # noqa: F401

NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)  # an hour after the fixture's sync (12:00 UTC)


def app_client(settings, now=NOW) -> TestClient:
    """The web app with fixed dates, and the shipped Data Stories copied into the test's curated folder."""
    stories = settings.curated_dir / "stories"
    if not stories.exists():
        shutil.copytree(DEFAULT_CURATED_DIR / "stories", stories)
    return TestClient(create_app(settings, today=lambda: TODAY, now=lambda: now))


def set_column(settings, column: str, values_by_appid: dict) -> None:
    conn = store.connect(settings.database)
    try:
        for appid, value in values_by_appid.items():  # column names come from the tests, never from input
            conn.execute(f"UPDATE games SET {column} = ? WHERE appid = ?", (value, appid))
        conn.commit()
    finally:
        conn.close()


def with_conn(settings, action) -> None:
    conn = store.connect(settings.database)
    try:
        action(conn)
    finally:
        conn.close()


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------- publisher and reception filters


def test_the_publisher_filter_groups_spellings_and_keeps_every_other_filter(games, settings):
    set_column(settings, "publishers", {101: '["Square Enix"]', 102: '["SQUARE ENIX"]', 103: '["Square Enix Co., Ltd."]',
                                        104: '["Konami", "Feral Interactive (Mac)"]', 105: '["Konami"]',
                                        106: '["KONAMI"]'})
    set_modes(settings, {102: ["Single-player"]})  # the others have Single-player and Online Co-op
    client = app_client(settings)
    html = client.get("/", params={"publisher": "square enix", "view": "all"}).text  # any spelling works
    assert shown_appids(html) == [101, 102, 103]
    assert '<option value="Square Enix" selected>Square Enix (3)</option>' in html
    assert '<option value="Konami">Konami (3)</option>' in html  # the most common spelling names the group
    chips = re.search(r'<ul class="active-filters".*?</ul>', html, re.S).group(0)
    assert "Publisher: Square Enix" in text_of(chips) and 'href="/?view=all"' in chips
    # Several game modes still require all of them, together with the publisher.
    both = client.get("/", params=[("publisher", "Square Enix"), ("mode", "Single-player"), ("mode", "Online Co-op"),
                                   ("view", "all")]).text
    assert shown_appids(both) == [101, 103]
    kept = "mode=Single-player&amp;mode=Online+Co-op&amp;publisher=Square+Enix"
    assert f'href="/games/101?{kept}&amp;view=all"' in both  # details keep the context
    assert f'href="/?{kept}"' in both  # so do the view tabs
    back = client.get("/games/101", params=[("publisher", "Square Enix"), ("view", "all"), ("page", "1")]).text
    assert 'href="/?publisher=Square+Enix&amp;view=all#game-101"' in back
    assert len(shown_appids(client.get("/", params={"publisher": "Nobody", "view": "all"}).text)) == 12  # ignored
    panel = client.get("/games/104/panel").text
    assert '<a class="text-link" href="/?publisher=Konami&amp;view=all"' in panel
    assert 'href="/?publisher=Feral+Interactive&amp;view=all"' in panel and "Feral Interactive (Mac)</a>" in panel


def test_the_reception_filter_uses_steams_own_review_labels(games, settings):
    set_column(settings, "review_score_desc", {101: "Overwhelmingly Positive", 102: "Very Positive",
                                               103: "Very Positive"})  # the other games stay "Mixed"
    client = app_client(settings)
    html = client.get("/", params={"reception": "Very Positive"}).text
    assert shown_appids(html) == [102, 103] and "Steam reviews: Very Positive" in text_of(html)
    for label, count in (("Overwhelmingly Positive", 1), ("Very Positive", 2), ("Mixed", 11)):
        assert re.search(rf'value="{label}"[^>]*>\s*<span class="option-name">{label}</span>\s*'
                         rf'<span class="option-count">{count}</span>', html), label
    assert 'value="Mostly Negative"' not in html  # only labels the catalog has are offered
    any_of = client.get("/", params=[("reception", "Overwhelmingly Positive"), ("reception", "Very Positive")]).text
    assert shown_appids(any_of) == [101, 102, 103]
    assert len(shown_appids(client.get("/", params={"reception": "Great"}).text)) == 12  # unknown labels are ignored


# ---------------------------------------------------------------------- prices


def save_test_prices(settings) -> None:
    """101 a fresh 30% discount; 102 a stale one; 103 an ended one; 104 hidden; 105 free; 106 not sold; 107 none."""
    def price(cents, percent=0, ends=None, hidden=False):
        final = cents * (100 - percent) // 100
        return PriceFacts(status=PRICED, final_cents=final, original_cents=cents, discount_percent=percent,
                          final_text=f"${final / 100:.2f}", original_text=f"${cents / 100:.2f}" if percent else None,
                          discount_ends_at=iso(ends) if ends else None, discount_hidden=hidden, purchase_option=None)

    def action(conn):
        store.save_prices(conn, {101: price(1999, 30, NOW + timedelta(days=3)), 104: price(1999, 40, hidden=True),
                                 103: price(1999, 20, NOW - timedelta(hours=1)), 105: PriceFacts(status=FREE),
                                 106: PriceFacts(status=NOT_SOLD)},
                          country="US", currency="USD", now=iso(NOW - timedelta(hours=1)))
        store.save_prices(conn, {102: price(1999, 50)}, country="US", currency="USD", now=iso(NOW - timedelta(days=2)))
        conn.execute("DELETE FROM prices WHERE appid = 107")
        conn.commit()
    with_conn(settings, action)


def test_a_sale_badge_needs_a_fresh_current_visible_discount(games, settings):
    save_test_prices(settings)
    html = app_client(settings).get("/", params={"view": "all"}).text
    assert '<span class="sale-badge">−30% on Steam' in card_html(html, 101)
    assert "Steam US store, price checked 9 Nov 2026, 12:00 UTC" in card_html(html, 101)
    for appid in range(102, 113):  # stale, ended, hidden, free, not sold, never checked, no discount
        assert "sale-badge" not in card_html(html, appid), appid
    later = app_client(settings, now=NOW + timedelta(days=2)).get("/", params={"view": "all"}).text
    assert "sale-badge" not in later  # the same discount is no longer shown once the check is old


def test_the_details_state_the_price_store_currency_and_check_time_or_why_there_is_none(games, settings):
    save_test_prices(settings)
    client = app_client(settings)

    def price_text(appid):
        panel = client.get(f"/games/{appid}/panel").text
        return text_of(re.search(r'<section class="details-section price-block".*?</section>', panel, re.S).group(0))

    discounted = price_text(101)
    for phrase in ("$13.99 −30% Price before the discount: $19.99 · Steam US store, USD",
                   "Steam lists the discount as ending 12 Nov 2026, 13:00 UTC.", "Checked 9 Nov 2026, 12:00 UTC.",
                   "Prices differ by region and can change at any time; Steam shows the final price."):
        assert phrase in discounted, phrase
    assert "may be out of date" in price_text(102)
    assert "that ended 9 Nov 2026, 12:00 UTC" in price_text(103) and "current price is not known" in price_text(103)
    assert "$11.99 · Steam US store, USD" in price_text(104)  # Steam asked not to show a percentage
    assert "Free · Steam US store marks this game as free" in price_text(105)
    assert "No price: the Steam US store lists no way to buy this app on its own" in price_text(106)
    assert "Not checked yet" in price_text(107) and "Free" not in price_text(107)
    panel = client.get("/games/101/panel").text  # the rest of the details stays
    for phrase in ("View on Steam", "data-wishlist-toggle", "More games like this", "<dt>Modes</dt>", "<dt>Publisher</dt>"):
        assert phrase in panel, phrase


# ---------------------------------------------------------------------- players, Trending and the dashboard


def save_players(settings, counts: dict, moment: datetime = NOW - timedelta(hours=1)) -> None:
    def action(conn):
        for appid, players in counts.items():
            store.save_player_count(conn, appid, players, now=iso(moment))
    with_conn(settings, action)


def test_the_players_sort_lists_the_newest_counts_first_and_games_without_one_after(games, settings):
    save_players(settings, {105: 5_000, 108: 300, 110: None})
    html = app_client(settings).get("/", params={"sort": "players", "view": "all"}).text
    assert shown_appids(html)[:3] == [105, 108, 101]  # then A–Z: no count is never treated as zero players
    assert '<option value="players" selected>Most players on Steam (latest snapshot)</option>' in html


def test_trending_sections_say_what_they_measure_and_explain_missing_history(games, settings):
    client = app_client(settings)
    empty = text_of(client.get("/trending").text)
    assert "No catalog game had players in game at its newest snapshot, or Steam reported no count" in empty
    assert "Steam's Top Sellers chart is not collected, and nothing here is a sales figure." in empty
    assert "A change needs two snapshots of the same game 7 to 14 days apart" in empty

    set_column(settings, "release_date", {101: "Mar 1, 2026"})
    set_column(settings, "review_score_desc", {101: "Very Positive", 102: "Overwhelmingly Positive"})
    save_players(settings, {104: 900, 103: 12_000})
    save_players(settings, {104: 100}, moment=NOW - timedelta(days=8, hours=1))  # a baseline 8 days earlier

    def older_reviews(conn):
        conn.execute("INSERT INTO review_snapshots (appid, captured_at, total, positive, negative) VALUES (?, ?, ?, ?, ?)",
                     (102, iso(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)), 1_000, 150, 850))
        conn.commit()
    with_conn(settings, older_reviews)

    html = client.get("/trending").text
    popular = re.search(r'id="popular".*?</section>', html, re.S).group(0)
    assert re.findall(r'data-details="(\d+)"', popular) == ["103", "104"]
    # Steam counts are labeled as Steam's; without an IGDB record, the platform line names the Steam version only.
    assert "12,000 players in game on Steam at 9 Nov 2026, 12:00 UTC" in text_of(popular)
    assert "Steam version for Windows, Linux Other platforms: not known yet (no IGDB record)" in text_of(popular)
    assert "ISteamUserStats/GetNumberOfCurrentPlayers" in popular
    this_year = text_of(re.search(r'id="this-year".*?</section>', html, re.S).group(0))
    assert "Released in 2026 with strong Steam reception" in this_year and "Game 01" in this_year
    four_years = re.search(r'id="four-years".*?</section>', html, re.S).group(0)
    assert re.findall(r'data-details="(\d+)"', four_years) == ["102", "101"]  # the fixture's other games are Mixed
    assert 'href="/?reception=Overwhelmingly+Positive&amp;reception=Very+Positive&amp;sort=reception"' in four_years
    # The same window as the website's five-year ranking, not the export's rolling four years.
    assert "Released since 2021 with strong Steam reception" in text_of(four_years)
    assert "Released on Steam 1 Jan 2021 – 9 Nov 2026" in text_of(four_years)
    changes = text_of(re.search(r'id="changes".*?</main>', html, re.S).group(0))
    assert "Game 02 +100 Steam reviews (50 positive) (1 Nov 2026 → 9 Nov 2026)" in changes
    assert "Game 04 +800 players in game on Steam (100 at 1 Nov 2026, 12:00 UTC → 900 at 9 Nov 2026, 12:00 UTC)" in changes
    assert '<dialog class="game-dialog" id="game-dialog"' in html  # names open the details dialog


def test_new_pages_render_without_network_access_and_the_navigation_links_them(games, settings, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("page requests must not call Steam or any other network service")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(SteamClient, "get_json", refuse)
    client = app_client(settings)
    for url in ("/trending", "/stories", "/stories/final-fantasy", "/stories/grand-theft-auto?show=main",
                "/dashboard/trending", "/?publisher=Test+Publisher&reception=Mixed&sort=players"):
        assert client.get(url).status_code == 200, url
    nav = re.search(r'<nav class="site-nav".*?</nav>', client.get("/trending").text, re.S).group(0)
    assert text_of(nav) == "Discover Trending Data Stories Dashboard Wishlist Daily Trivia"  # icons' tips since 0.17
    assert 'href="/trending" aria-current="page"' in nav and 'href="/stories"' in nav


# ---------------------------------------------------------------------- data stories


def test_story_pages_show_the_journey_sources_and_catalog_links_only_for_catalog_games(settings, fake_steam, clock):
    fake_steam.add_game(271590, "Grand Theft Auto V Legacy")
    run_test_sync(settings, fake_steam, clock, [CuratedGame(271590, "Grand Theft Auto V", "")])
    client = app_client(settings)
    index = text_of(client.get("/stories").text)
    # The span ends at the newest released game; the upcoming 2027 remake does not extend it.
    assert "Final Fantasy 1987–2024" in index and "Grand Theft Auto 1997–2026" in index
    html = client.get("/stories/grand-theft-auto").text
    text = text_of(html)
    for phrase in ("Longest wait between main games 13 yr 2 mo Grand Theft Auto V → Grand Theft Auto VI",
                   "Grand Theft Auto V (17 Sep 2013) Grand Theft Auto VI (19 Nov 2026) 13 years, 2 months and 2 days 4,811",
                   "First released in Europe on MS-DOS and Windows",
                   "Expansion of Grand Theft Auto IV",
                   "Remaster of Grand Theft Auto III, Grand Theft Auto: Vice City and Grand Theft Auto: San Andreas",
                   "What it leaves out", "Attribution and license", "Creative Commons Attribution-ShareAlike 4.0"):
        assert phrase in text, phrase
    assert "Since the latest main game" not in text  # GTA VI came out on the day the facts were checked
    assert 'href="https://en.wikipedia.org/w/index.php?title=Grand_Theft_Auto_VI&amp;oldid=1386579947"' in html
    assert html.count('href="/games/271590">In the Game Librarian catalog</a>') == 1
    assert 'href="/games/12210"' not in html  # GTA IV's Steam app is not in this catalog: no link
    main_only = client.get("/stories/final-fantasy?show=main").text
    assert main_only.count('<li class="story-entry') == 16
    ff = text_of(client.get("/stories/final-fantasy").text)
    assert "Upcoming: announced date" in ff and "Announced for Nintendo Switch 2, PlayStation 5, Windows and" in ff
    assert "Since the latest main game 3 yr 4 mo 3 years, 4 months and 28 days since Final Fantasy XVI" in ff
    assert client.get("/stories/not-a-story").status_code == 404
