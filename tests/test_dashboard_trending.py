"""Dashboard → Trending games (versions 0.15-0.16): three report pages about the Trending page's games, one tab bar,
Sales & reception first. This file covers the pages and Sales & reception's reception chart (Gamalytic's doughnut is in
test_sales_estimates.py); Upcoming releases is in test_release_radar.py and test_list_comparison.py, Momentum in
test_momentum.py.

Since 0.16 Sales & reception draws the five-year window's strong-reception games once each on one scatterplot, this
year's releases highlighted (or shown alone, on the same axes), and compares a selected game with the median positive
share of its peers: the other comparable games of its most specific gameplay genre, strong reception or not. The long
feed of 0.10-0.14 is gone: the Want to Play bars, Twitch beside Steam reviews, the Steam player bars and review splits,
the sparklines, rises and position-change tables, and the Steam data table of the cross-platform games.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114). All names and numbers are made up."""

import re
from datetime import date, timedelta

from fastapi.testclient import TestClient

from game_librarian import dashboard, store
from game_librarian.catalog import CatalogGame
from game_librarian.config import PACKAGE_DIR
from game_librarian.web import create_app
from tests.test_trending_cross_platform import RELEASE_DAY, at, refresh, site, world  # noqa: F401
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_discovery import save_players, set_column, with_conn
from tests.test_web_upcoming import NOW, TODAY, NoTwitch, configured, save_upcoming

APP_JS = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")


def item(key, name="Game", av=None, *, steam_verified=False, **facts) -> dict:
    """A Trending list item as web._igdb_item builds it, with only what the dashboard reads."""
    return {"key": key, "name": name, "details_url": f"/games/{key}", "availability": av,
            "steam_verified": steam_verified, **facts}


def catalog_game(appid, *, positive=None, total=None, players=None, players_at=None) -> CatalogGame:
    return CatalogGame(appid=appid, sort_name=f"game {appid}", labels=(), awards=(), positive=positive, total=total,
                       released_on=None, upcoming=False, in_window=False, rank=None, players=players,
                       players_at=players_at)


def dashboard_client(settings, *, today=TODAY, now=NOW, client=("testclient", 50000)) -> TestClient:
    return TestClient(create_app(configured(settings), today=lambda: today, now=lambda: now,
                                 twitch_client=NoTwitch()), client=client)


def details_keys(html: str) -> list[str]:
    return re.findall(r'data-details="([^"]+)"', html)


def chart(html: str, title_id: str) -> str:
    """One chart card (a <figure>) of the dashboard, by its title's id."""
    start = html.rindex("<figure", 0, html.index(f'id="{title_id}"'))
    return html[start:html.index("</figure>", start)]


def methodology(html: str) -> str:
    """A report page's collapsed "Sources and methodology" part."""
    return re.search(r'<details class="method-details" id="sources">.*?</details>\s*</section>', html, re.S).group(0)


def canvas(html: str) -> str:
    """A report page's charts, without its methodology."""
    return html[html.index('<div class="report-canvas'):html.index('<details class="method-details"')]


def reception(game: dict) -> dict:
    """A game as dashboard.reception_chart and peer_benchmark take it (an RPG unless `game` says otherwise)."""
    return {"name": f"Game {game['appid']}", "details_url": f"/games/{game['appid']}", "percent": 90,
            "review_label": "Very Positive", "released_on": date(2026, 3, 1), "genres": ["RPG"], **game}


def strong_reception(settings) -> None:
    """Released this year (2026): Game 01 (85% of 2,000) and Game 02 (98% of 150,000). Earlier, in the five-year
    window: Game 03 (90% of 40,000, 2023) and Game 04 (98.3% of 600, 2024). Game 05 has strong reception but came
    out in 2020, before the window; every other game is Mixed."""
    set_column(settings, "release_date", {101: "Mar 1, 2026", 102: "Jun 5, 2026", 103: "Feb 2, 2023",
                                          105: "Dec 31, 2020"})
    set_column(settings, "review_score_desc", {101: "Very Positive", 102: "Overwhelmingly Positive",
                                               103: "Very Positive", 104: "Overwhelmingly Positive",
                                               105: "Very Positive"})
    set_column(settings, "review_total", {101: 2_000, 102: 150_000, 103: 40_000, 104: 600, 105: 9_000})
    set_column(settings, "review_positive", {101: 1_700, 102: 147_000, 103: 36_000, 104: 590, 105: 8_500})


def dots(html: str) -> dict[str, tuple[str, str]]:
    """Each dot of one scatterplot, by app id: its left and bottom position."""
    return {key: (left, bottom) for key, left, bottom in re.findall(
        r'<a class="plot-dot[^"]*" href="/games/\d+" data-details="(\d+)" data-game="\d+" data-roving-item '
        r'data-genres="[^"]*" data-views="[^"]*" style="left: ([\d.]+)%; bottom: ([\d.]+)%"', html)}


# ---------------------------------------------------------------------- the report pages


def test_trending_games_has_three_report_pages_behind_one_tab_bar(games, settings):
    client = dashboard_client(settings)
    html = client.get("/dashboard").text
    assert "<title>Sales &amp; reception · Trending games | Dashboard | Game Librarian</title>" in html
    tabs = re.search(r'<nav class="report-tabs" aria-label="Trending games reports">.*?</nav>', html, re.S).group(0)
    assert re.findall(r'href="([^"]+)"', tabs) == ["/dashboard", "/dashboard?report=upcoming",
                                                   "/dashboard?report=momentum"]
    assert text_of(tabs) == "Sales & reception Upcoming releases Momentum"
    assert 'href="/dashboard" aria-current="page">Sales &amp; reception</a>' in tabs  # the default report
    for report, title, question in (
            ("upcoming", "Upcoming releases", "What would a player miss by browsing only Steam’s most-wishlisted list?"),
            ("momentum", "Momentum", "Which games account for the change in review activity across our tracked games?"),
            ("unknown", "Sales & reception", "Does this game stand out among similar releases?")):
        page = client.get("/dashboard", params={"report": report}).text
        assert f"<title>{title.replace('&', '&amp;')} · Trending games | Dashboard" in page, report
        assert question in text_of(page), report
        assert page.count('aria-current="page">') == 3  # the site link, the dashboard page and the report
    # The dashboard's pages stay one click away, with Trending games first.
    pages = re.search(r'<nav class="view-switch" aria-label="Dashboard pages">.*?</nav>', html, re.S).group(0)
    assert text_of(pages) == "Trending games Player reception Awards About the data"
    assert 'href="/dashboard" aria-current="page">Trending games</a>' in pages
    for path, title in (("/dashboard/reception", "Player reception"), ("/dashboard/awards", "Awards"),
                        ("/dashboard/about", "About the data")):
        page = client.get(path)
        assert page.status_code == 200 and f"<title>{title} | Dashboard | Game Librarian</title>" in page.text
    site_nav = re.search(r'<nav class="site-nav".*?</nav>', html, re.S).group(0)
    assert 'href="/dashboard" aria-current="page">Dashboard</a>' in site_nav
    # Every page has its own details dialog and its own collapsed methodology.
    for report in ("sales", "upcoming", "momentum"):
        page = client.get("/dashboard", params={"report": report}).text
        assert '<dialog class="game-dialog" id="game-dialog"' in page and page.count('class="method-details"') == 1


def test_old_dashboard_addresses_keep_working(games, settings):
    client = dashboard_client(settings)
    moved = client.get("/dashboard/trending", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/dashboard"
    assert "Trending games | Dashboard" in client.get("/dashboard/trending").text  # a bookmark still lands
    # Before 0.10, /dashboard was Player reception: its filter links now open that page, filters kept.
    old = client.get("/dashboard?tag=Indie&award_status=Nominee", follow_redirects=False)
    assert old.status_code == 307 and old.headers["location"] == "/dashboard/reception?tag=Indie&award_status=Nominee"
    reception_page = client.get("/dashboard?tag=Indie").text
    assert "<title>Player reception | Dashboard" in reception_page
    assert "7 of 14 catalog games match the filters." in reception_page
    legacy = client.get("/dashboard", params={"label": "Unclassified"}).text  # a pre-0.8 label link, too
    assert '<option value="No verified label" selected>' in legacy
    assert "Trending games | Dashboard" in client.get("/dashboard?unknown=1").text  # others do not redirect


def test_the_long_feed_of_earlier_versions_is_gone_from_every_page(games, settings):
    save_upcoming(settings)
    save_players(settings, {114: 4_000, 113: 1_500}, moment=NOW - timedelta(minutes=50))
    client = dashboard_client(settings)
    for report in ("sales", "upcoming", "momentum"):
        page = client.get("/dashboard", params={"report": report}).text
        words = text_of(page)
        for gone in ("Most wanted new releases", "Most watched on Twitch, with their Steam reviews",
                     "Changes over time", "Steam data of the cross-platform games",
                     "Their positive and negative Steam reviews", "position changes", "Release-date changes",
                     "Steam players over the last", "Most new Steam reviews", "Most added players on Steam",
                     "Games in the cross-platform lists", "announced platforms"):
            assert gone not in words, (report, gone)
        for markup in ('class="split-bar', 'class="spark', 'id="chart-want_to_play"', 'class="trend-jump',
                       'class="rank-tile', 'id="chart-twitch"'):
            assert markup not in page, (report, markup)
    # The Trending page still lists every list the dashboard no longer repeats.
    listing = text_of(client.get("/trending").text)
    assert "Most wanted on IGDB" in listing and "Popular on Steam right now" in listing


def test_an_empty_site_explains_every_gap_without_inventing_charts(settings):
    client = TestClient(create_app(settings, today=lambda: TODAY, now=lambda: NOW))
    sales = client.get("/dashboard").text
    assert "No catalog game released on Steam since 1 Jan 2021 has a review summary of Overwhelmingly Positive or " \
           "Very Positive yet." in text_of(sales)
    assert 'class="plot-dot' not in sales and 'class="plot"' not in sales
    upcoming_page = text_of(client.get("/dashboard?report=upcoming").text)
    assert "IGDB is not set up, so there are no tracked upcoming games to place." in upcoming_page
    momentum = text_of(client.get("/dashboard?report=momentum").text)
    assert "Not enough snapshot history yet" in momentum and "No review counts have been saved yet." in momentum
    assert 'class="wf-row' not in momentum and "<polyline" not in momentum


# ---------------------------------------------------------------------- Sales & reception: the peer benchmark (pure)


def peer(appid, positive, total=1_000, genres=("RPG",)) -> dict:
    return reception({"appid": appid, "positive": positive, "total": total, "genres": list(genres)})


def test_the_peer_median_by_hand_counts_a_lower_rated_peer_and_never_the_selected_game():
    selected = peer(1, 950, genres=("Action", "RPG"))  # 95.0%
    rpg = [peer(2, 900), peer(3, 800), peer(4, 975), peer(5, 850), peer(6, 700)]  # 3 and 6: below strong reception
    action = [peer(appid, 990, genres=("Action",)) for appid in range(7, 13)]  # six Action games, five RPG games
    found = dashboard.peer_benchmark(selected, [selected, *rpg, *action])
    # RPG is the more specific genre (5 others share it, against 6 for Action), and at least 5 share it.
    assert (found["genre"], found["count"], found["enough"]) == ("RPG", 5, True)
    assert [game["appid"] for game in found["peers"]] == [2, 3, 4, 5, 6]  # never the selected game
    # By hand: 70.0, 80.0, 85.0, 90.0, 97.5 -> the median is 85.0; with the selected game it would be 87.5.
    assert (found["share"], found["median"], found["gap"]) == (950, 850, 100)  # tenths: 95.0%, 85.0%, +10.0 points
    assert dashboard.format_tenths(found["gap"], signed=True) == "+10.0"
    # Each game counts once, whatever its number of reviews: a huge peer does not pull the median.
    heavy = dashboard.peer_benchmark(selected, [selected, *rpg[:4], peer(6, 7_000_000, total=10_000_000)])
    assert heavy["median"] == 850


def test_with_fewer_than_five_peers_there_is_no_benchmark_but_the_games_own_numbers_stay():
    racer = peer(1, 950, genres=("Racing", "Sports"))
    few = dashboard.peer_benchmark(racer, [racer, peer(2, 900, genres=("Racing",)), peer(3, 800, genres=("Racing",)),
                                           peer(4, 700, genres=("Sports",))])
    # Neither genre reaches five: the one most games share is shown, with its count and no median or gap.
    assert (few["genre"], few["count"], few["enough"], few["median"], few["gap"]) == ("Racing", 2, False, None, None)
    assert few["share"] == 950
    genre_less = dashboard.peer_benchmark(peer(5, 900, genres=("Indie", "Casual")), [peer(6, 800)])
    assert (genre_less["genre"], genre_less["count"], genre_less["enough"]) == (None, 0, False)  # nothing to compare by
    assert dashboard.gameplay_genres(["Action", "Indie", "Free To Play", "RPG", "Casual"]) == ["Action", "RPG"]


def test_ties_between_genres_are_broken_a_to_z_so_the_rule_is_deterministic():
    both = peer(1, 950, genres=("Strategy", "Adventure"))
    pool = [both, *(peer(appid, 900, genres=("Adventure", "Strategy")) for appid in range(2, 8))]
    assert dashboard.peer_benchmark(both, pool)["genre"] == "Adventure"  # six each: A–Z
    assert dashboard.peer_benchmark(both, list(reversed(pool)))["genre"] == "Adventure"  # whatever the order


def test_shares_round_half_up_exactly_and_the_gap_is_the_difference_of_the_numbers_shown():
    assert dashboard.percent_tenths(1_869, 2_000) == 935  # 93.45% -> 93.5%: half up, never to even
    assert dashboard.percent_tenths(1, 3) == 333 and dashboard.percent_tenths(2, 3) == 667
    selected = peer(1, 9_344, total=10_000)  # 93.44% -> 93.4%
    peers = [peer(appid, positive) for appid, positive in ((2, 900), (3, 910), (4, 921), (5, 922), (6, 950), (7, 960))]
    found = dashboard.peer_benchmark(selected, [selected, *peers])
    # Six peers: the median is (92.1 + 92.2) / 2 = 92.15 exactly, which rounds half up to 92.2 (float arithmetic
    # would give 92.149999… and 92.1). The gap is 93.4 - 92.2 = +1.2 points, as the strip shows it.
    assert (found["share"], found["median"], found["gap"]) == (934, 922, 12)
    assert [dashboard.format_tenths(value) for value in (934, 922)] == ["93.4", "92.2"]
    assert [dashboard.format_tenths(value, signed=True) for value in (12, -23, 0)] == ["+1.2", "−2.3", "0.0"]


# ---------------------------------------------------------------------- Sales & reception: the chart (pure)


def test_one_chart_plots_each_game_once_with_axes_that_fit_every_median_and_stay_with_the_view():
    cohort = [reception({"appid": 1, "total": 2_000, "positive": 1_700}),
              reception({"appid": 2, "total": 150_000, "positive": 147_000}),
              reception({"appid": 3, "total": 40_000, "positive": 36_000}),
              reception({"appid": 4, "total": 600, "positive": 590})]
    weak = [reception({"appid": number, "total": 1_000, "positive": 450 + 10 * number}) for number in range(10, 16)]
    chart_data = dashboard.reception_chart(cohort, cohort + weak, {1, 2})
    assert [point["appid"] for point in chart_data["points"]] == [4, 1, 3, 2]  # once each, by number of reviews
    assert chart_data["views"] == {"all": 4, "year": 2}
    assert [point["views"] for point in chart_data["points"]] == [["all"], ["all", "year"], ["all"], ["all", "year"]]
    assert [point["this_year"] for point in chart_data["points"]] == [False, True, False, True]
    # By hand: each game's 9 peers are the 3 other drawn games and the 6 weak ones (55%-60%), so every median is the
    # fifth value, 59.0%; the y axis reaches it, so every median line fits on the chart.
    assert {point["benchmark"]["median"] for point in chart_data["points"]} == {590}
    assert [tick["label"] for tick in chart_data["y_ticks"]] == ["50%", "60%", "70%", "80%", "90%", "100%"]
    assert chart_data["points"][0]["median_y"] == 18.0  # 59.0% on an axis from 50% to 100%
    assert chart_data["log"] is True and [tick["label"] for tick in chart_data["x_ticks"]] == [
        "100", "1K", "10K", "100K", "1M"]
    # Names beside the dots, per view: this year's view names both of its games.
    assert {point["appid"]: point["label_views"] for point in chart_data["points"] if point["label_views"]} == {
        4: ["all"], 1: ["year"], 2: ["all", "year"]}
    assert dashboard.reception_chart([], weak, set()) is None


def test_close_review_counts_stay_on_a_linear_axis_from_zero():
    cohort = [reception({"appid": 1, "total": 1_200, "positive": 1_000}),
              reception({"appid": 2, "total": 9_000, "positive": 8_500})]
    chart_data = dashboard.reception_chart(cohort, cohort, set())
    assert chart_data["log"] is False  # 9,000 is less than ten times 1,200
    assert [tick["label"] for tick in chart_data["x_ticks"]] == ["0", "2K", "4K", "6K", "8K", "10K"]
    assert [point["x"] for point in chart_data["points"]] == [12.0, 90.0]
    assert chart_data["views"]["year"] == 0
    low = dashboard.reception_chart([reception({"appid": 1, "total": 100, "positive": 55})], [], set())
    assert [tick["label"] for tick in low["y_ticks"]] == ["50%", "60%", "70%", "80%", "90%", "100%"]


def test_a_few_standout_games_are_named_and_a_small_cohort_names_every_game():
    small = [reception({"appid": number, "total": 1_000 * number, "positive": 900 * number}) for number in (1, 2, 3)]
    assert dashboard.standout_games(small) == [3, 2, 1]  # every game, most reviewed first
    cohort = [reception({"appid": 1, "total": 500_000, "positive": 450_000}),  # the most reviewed (90%)
              reception({"appid": 2, "total": 80_000, "positive": 78_000}),  # the highest share (97.5%)
              reception({"appid": 3, "total": 900, "positive": 870}),  # 96.7% of a smaller audience: upper left
              reception({"appid": 4, "total": 700, "positive": 600}),
              reception({"appid": 5, "total": 20_000, "positive": 17_000})]
    assert dashboard.standout_games(cohort) == [1, 2, 3]  # in the order the page keeps them if they overlap
    named = [point["appid"] for point in dashboard.reception_chart(cohort, cohort, set())["points"]
             if point["label_views"]]
    assert sorted(named) == [1, 2, 3] and len(named) <= dashboard.STANDOUT_LABELS


# ---------------------------------------------------------------------- Sales & reception: the page


def genre_world(settings) -> None:
    """strong_reception's four drawn games, with Game 01 an Action RPG and Game 02 the only Racing game; the Mixed
    Games 06-14 (46%-82% positive, all RPGs released 1 Jan 2024) are comparable but not drawn; Game 05 came out before
    the window, and Game 14's review summary is missing (saved as zeros), so it is not comparable."""
    strong_reception(settings)
    set_column(settings, "genres", {101: '["Action", "RPG"]', 102: '["Racing", "Indie"]'})
    set_column(settings, "review_total", {114: 0})
    set_column(settings, "review_positive", {114: 0})


def test_reception_leads_until_estimates_are_collected_then_the_estimated_sales_do(games, settings):
    strong_reception(settings)
    save_players(settings, {114: 4_000, 113: 1_500}, moment=NOW - timedelta(minutes=50))
    before = dashboard_client(settings).get("/dashboard").text
    charts = canvas(before)
    assert 'id="sales"' not in before  # Gamalytic was never asked: no empty chart
    assert re.findall(r'<figure class="[^"]*" id="([^"]+)"', charts) == ["reception"]  # one chart, each game once
    assert "No sales figures: Valve publishes no unit sales or revenue per game, so this page compares Steam " \
           "reception." in text_of(before)
    with_conn(settings, lambda conn: store.save_sales_estimates(
        conn, {114: 2_500_000}, source="Gamalytic", collected_at="2026-11-09T11:00:00+00:00", source_cached_at=None))
    after = dashboard_client(settings).get("/dashboard").text
    assert after.index('id="sales"') < after.index('id="reception"')  # Estimated Steam copies sold first
    assert "Steam · cumulative estimates" in text_of(chart(after, "chart-sales"))
    assert "No sales figures" not in text_of(after)


def test_every_game_is_one_dot_with_its_count_share_and_comparison_with_similar_releases(games, settings):
    genre_world(settings)
    html = dashboard_client(settings).get("/dashboard").text
    card = chart(html, "chart-reception")
    words = text_of(card)
    assert words.startswith("Strong Steam reception since 2021 Steam only Each dot is a game with a Steam review "
                            "summary of Overwhelmingly Positive or Very Positive, released 1 Jan 2021 – 9 Nov 2026: 4 "
                            "games, 2 of them released in 2026.")
    assert set(dots(card)) == {"101", "102", "103", "104"} and "105" not in dots(card)  # once each, in the window
    assert len(re.findall(r'class="plot-dot is-accent"', card)) == 2  # this year's releases in the accent color
    # Each dot names its game, share, count and comparison with its peers; screen readers get it from its label.
    assert ('aria-label="Game 01: 85.0% positive of 2,000 Steam reviews, released 1 Mar 2026; RPG peers’ median '
            '68.8%, +16.2 points"') in card
    assert 'aria-label="Game 02: 98.0% positive of 150,000 Steam reviews, released 5 Jun 2026; too few comparable ' \
           'Racing games (0)"' in card
    assert 'data-genres="Action RPG" data-views="all year"' in card and 'data-genres="Racing"' in card
    # The strip of each game, copied in by app.js: reviews, share, peer median, gap and the peers' genre and dates.
    strip = text_of(re.search(r'<template id="bench-101"[^>]*>(.*?)</template>', card, re.S).group(1))
    assert strip == ("Game 01 Steam reviews 2,000 Positive 85.0% Peer median 68.8% Gap +16.2 points Peers 10 other RPG "
                     "games released 1 Jan 2021 – 9 Nov 2026")
    # By hand: 10 RPG peers, 46.2% to 98.3%; the median is the mean of the 5th and 6th, (66.7 + 71.0) / 2 = 68.8%.
    assert '<template id="bench-101" data-peer-genre="RPG" data-median-y="22.0" data-median-label="RPG median 68.8%">' \
        in card  # 68.8% on an axis from 60% to 100%
    few = text_of(re.search(r'<template id="bench-102"[^>]*>(.*?)</template>', card, re.S).group(1))
    assert few == ("Game 02 Steam reviews 150,000 Positive 98.0% Peer median Too few comparable games Gap None Peers 0 "
                   "other Racing games released 1 Jan 2021 – 9 Nov 2026 (at least 5 needed)")
    assert '<template id="bench-102" data-peer-genre="Racing">' in card  # no median line to draw
    assert "Select a dot to compare its game with similar releases" in words
    assert "Positive reviews" in words and "Steam reviews (log scale: each step is 10 times more)" in words
    # The table gives every number and every comparison, most reviewed first.
    table = text_of(re.search(r"<table.*?</table>", card, re.S).group(0))
    assert table.startswith("Game Released on Steam Steam reviews Positive Steam’s summary Peer genre Peers Peer median "
                            "Gap (points) Game 02 5 Jun 2026 150,000 98.0% Overwhelmingly Positive Racing 0 Too few")
    assert "Game 01 1 Mar 2026 2,000 85.0% Very Positive RPG 10 68.8% +16.2" in table
    assert ("released 1 Jan 2021 – 9 Nov 2026, the five-year ranking’s window · 4 games drawn, compared with 12 "
            "comparable games") in words  # Games 01-04 and 06-13: Game 14 has no usable reviews, Game 05 is older


def test_the_view_shows_this_years_games_only_on_the_same_axes_and_works_without_javascript(games, settings):
    genre_world(settings)
    client = dashboard_client(settings)
    everything, this_year = client.get("/dashboard").text, client.get("/dashboard", params={"view": "year"}).text
    links = re.findall(r'<a class="window-link" href="([^"]+)" data-reception-view="(\w+)"([^>]*)>(.*?)</a>',
                       chart(everything, "chart-reception"), re.S)
    assert [(href, view, "aria-current" in rest, text_of(label)) for href, view, rest, label in links] == [
        ("/dashboard#reception", "all", True, "Since 2021 4 games"), ("/dashboard?view=year#reception", "year", False,
                                                                       "2026 only 2 games")]
    shown = re.findall(r'data-details="(\d+)" data-game="\d+" data-roving-item [^>]*?(hidden)?></a>',
                       chart(this_year, "chart-reception"))
    assert sorted(key for key, hidden in shown if not hidden) == ["101", "102"]  # the others stay, hidden
    assert dots(chart(this_year, "chart-reception")) == dots(chart(everything, "chart-reception"))  # the same spots
    assert 'data-reception data-view="year"' in this_year
    assert 'data-reception data-view="all"' in client.get("/dashboard", params={"view": "decade"}).text


def test_the_reception_methodology_keeps_the_rules_folded_one_click_away(games, settings):
    genre_world(settings)
    html = dashboard_client(settings).get("/dashboard").text
    method = text_of(methodology(html))
    assert method.startswith("Sources and methodology Strong Steam reception Which games:")
    assert "Every such game is drawn once, not only the first 10 the Trending page lists: 4 now, 2 of them released " \
           "this year" in method
    assert "on a log scale because the counts span more than ten times" in method
    for phrase in ("strong reception or not: 12 now", "The 8 below the strong-reception threshold are not drawn but "
                   "count in the medians", "its most specific one: the genre the fewest other comparable games share, "
                   "among those at least 5 share (ties A–Z)", "each game counting once whatever its number of reviews",
                   "the strip shows the count and “Too few comparable games”", "rounded half up to one decimal",
                   "a review count is the number of reviews Steam purchasers wrote, not copies sold"):
        assert phrase in method, phrase
    below = text_of(re.search(r"<summary>Comparable games below the strong-reception threshold \(8\)</summary>.*?"
                              r"</table>", html, re.S).group(0))
    assert "Game 06 1 Jan 2024 1,300 46.2% Mixed RPG" in below  # the lower-rated peers behind every RPG median
    assert "Game 14" not in below and "Game 05" not in below
    assert "Estimated Steam copies sold" not in method  # nothing about Gamalytic for another computer


def test_the_reception_script_rings_peers_draws_the_median_and_switches_views_in_place():
    script = APP_JS[APP_JS.index('querySelectorAll("[data-reception]")'):APP_JS.index('querySelectorAll("[data-sales-ring]")')]
    for needed in ('document.getElementById("bench-" + key)', "template.content.cloneNode(true)",
                   'classList.toggle("is-peer"', 'getAttribute("data-genres").split(" ")', "line.hidden = median === null",
                   'showView("all")', 'urlWith("view"', "fitLabels(plot)", "resetTabStop(plot)",
                   "trackSelection(plot, select", "receptionCharts.push"):
        assert needed in script, needed
    assert "innerHTML" not in script
