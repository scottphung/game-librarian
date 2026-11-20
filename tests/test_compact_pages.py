"""Version 0.10: compact pages. Long explanations fold into native <details> elements behind short visible text, so
they open with a keyboard (Enter or Space on the summary) and without JavaScript; the footer spans the page in
small type and keeps every credit. The rendered layouts at 390 and 1,280 px are checked in a browser (README,
"Verification status"); the CSS rules they depend on are pinned here."""

import re

import pytest
from fastapi.testclient import TestClient

from game_librarian.config import PACKAGE_DIR
from game_librarian.web import create_app
from tests.test_web import client, games, text_of  # noqa: F401
from tests.test_web_discovery import app_client
from tests.test_web_upcoming import NOW, TODAY, NoTwitch, configured, save_upcoming

CSS = (PACKAGE_DIR / "static" / "style.css").read_text(encoding="utf-8")
PAGES = ("/", "/trending", "/stories", "/stories/grand-theft-auto", "/dashboard", "/dashboard/reception",
         "/dashboard/awards", "/dashboard/about", "/wishlist", "/games/101")


def rule(selector: str, css: str = CSS) -> str:
    """The declarations of the first rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.M)
    assert match, selector
    return match.group(1)


def media(width: int) -> str:
    """Every rule inside the `@media (max-width: <width>px)` blocks, joined."""
    blocks = re.findall(r"@media \(max-width: " + str(width) + r"px\) \{(.*?)\n\}", CSS, re.S)
    assert blocks, width
    return "\n".join(blocks)


def igdb_site(settings) -> TestClient:
    """The site with IGDB configured and the seeded upcoming lists (test_web_upcoming.save_upcoming)."""
    save_upcoming(settings)
    return TestClient(create_app(configured(settings), today=lambda: TODAY, now=lambda: NOW,
                                 twitch_client=NoTwitch()))


def without_details(html: str) -> str:
    """The page as it shows before anything is expanded: every <details> body removed, summaries kept."""
    return re.sub(r"(<details[^>]*>\s*<summary[^>]*>.*?</summary>).*?</details>", r"\1</details>", html, flags=re.S)


# ---------------------------------------------------------------------- footer


def test_the_footer_keeps_every_credit_in_short_blocks_on_every_page(settings, games):
    site = app_client(settings)
    for path in PAGES:
        html = site.get(path).text
        footer = re.search(r'<footer class="site-footer">.*?</footer>', html, re.S).group(0)
        blocks = re.findall(r"<p>\s*<strong class=\"footer-label\">([^<]+)</strong>(.*?)</p>", footer, re.S)
        assert [label for label, _ in blocks] == ["Steam", "As provided", "IGDB and Twitch", "Curated", "Trademarks"]
        text = text_of(footer)
        for phrase in ("Store details, descriptions, images, trailer clips and tags come from the Steam store",
                       "Prices differ by region and change often: Steam shows the final price.",
                       'Shown as provided ("as is"), without any warranty of accuracy or completeness',
                       "Descriptions, images and trailers belong to their publishers.",
                       "Review excerpts were written by Steam users and may be shortened here.",
                       "come from IGDB.com", "come from Twitch and are kept for 24 hours at most.",
                       "Award results and AAA / AA / Indie labels are curated by hand, and each links to its source.",
                       "Critic scores and Data Story facts come from Wikipedia (CC BY-SA 4.0)",
                       "Game Librarian is a personal project. It is not affiliated with, endorsed by or sponsored by "
                       "Valve. Steam and the Steam logo are trademarks and/or registered trademarks of Valve "
                       "Corporation in the U.S. and/or other countries."):
            assert phrase in text, (path, phrase)
        assert 'href="https://www.igdb.com/"' in footer and 'href="https://www.twitch.tv/"' in footer


def test_the_footer_spans_the_page_in_small_readable_type():
    footer = rule(".site-footer")
    assert "max-width" not in footer and "margin: 40px 0 0;" in footer  # the rule runs edge to edge
    assert "font-size: 0.75rem;" in footer  # 12 px: smaller than before (0.8rem), not below 12 px
    grid = rule(".footer-grid")
    assert "max-width: 1440px;" in grid  # the page's own width, aligned with the content above
    assert "grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));" in grid  # short lines, side by side
    assert "110ch" not in rule(".footer-grid p")  # no half-width paragraphs any more
    assert ".footer-grid { gap: 10px; padding: 14px 16px 20px; }" in media(720)  # one column with phone margins


# ---------------------------------------------------------------------- collapsible explanations


def test_trending_lists_show_a_short_subtitle_and_fold_their_definitions(games, settings):
    html = igdb_site(settings).get("/trending").text
    lists = re.findall(r'<details class="about-list">\s*<summary>About this list</summary>\s*<dl class="trend-facts">',
                       html)
    assert len(lists) == 8  # two upcoming lists, recent releases, Twitch, three Steam lists and the Steam changes
    assert html.count('<p class="list-sub">') == html.count('<p class="list-meta">') == 8
    assert html.count("<summary>About this section</summary>") == 3
    visible = without_details(html)
    assert '<dl class="trend-facts">' not in visible  # every Signal / Coverage / Source / Updated block is folded
    assert "Source: IGDB.com · updated 9 Nov 2026, 12:00 UTC" in text_of(visible)  # but the source and time show
    # Essential context stays visible next to the numbers it qualifies.
    assert "players on consoles, in other PC stores or offline are not counted, and nothing here is a sales figure." \
        in text_of(visible)
    for phrase in ("how often IGDB.com users add a game to their Want to Play list", "they stand for list positions",
                   "A game counts as upcoming until its first full release",
                   "Steam's Top Sellers chart is not collected, and nothing here is a sales figure."):
        assert phrase in text_of(html) and phrase not in text_of(visible), phrase  # kept, one click away


@pytest.mark.parametrize("path, summaries, visible_phrase", [
    ("/dashboard/about", ["Player reception", "Five-year ranking", "Refreshing",
                          "Snapshots, trends and what is not included"],
     "it is on the website only, not in the Power BI report"),
    ("/dashboard/reception", ["Show the numbers", "How the ranking works", "Definitions"],
     "An editorial order, not a quality score; filters hide games but never renumber them."),
    ("/wishlist", ["How this wishlist is stored"],
     "Saved only in this browser on this device, so clearing browser data can erase it. It is not your Steam wishlist."),
    ("/stories/final-fantasy", ["Show the numbers", "Show each main game's first platforms",
                                "What this story includes and leaves out", "Show all sources"],
     "Facts checked 19 Nov 2026 against the Wikipedia revisions listed under Sources."),
])
def test_long_explanations_fold_behind_short_visible_text(settings, games, path, summaries, visible_phrase):
    html = app_client(settings).get(path).text
    found = [text_of(summary) for summary in re.findall(r"<summary[^>]*>(.*?)</summary>", html, re.S)]
    assert found == summaries
    assert visible_phrase in text_of(without_details(html))


def test_the_discover_intro_is_two_short_lines_and_the_rules_open_from_a_labeled_control(client):
    html = client.get("/").text
    scope = re.search(r'<p class="results-scope">(.*?)</p>', html, re.S).group(1)
    assert scope == ("Games released in the last five full years and this year so far: "
                     "<strong>1 Jan 2021 – 9 Nov 2026</strong>.<br>Ranked by awards and player reception.")
    assert "How the ranking works" in text_of(without_details(html))  # the control's visible label
    assert "editorial discovery ranking, not an objective measure of game quality" in text_of(html)


def test_the_dashboard_keeps_its_details_one_click_away(games, settings):
    html = igdb_site(settings).get("/dashboard").text
    visible = text_of(without_details(html))
    assert "Lists, measures and update times" not in visible and "Snapshots, positions, trends and sales" not in visible
    assert "Sources and methodology" in visible  # the jump link and the summary
    assert 'href="#sources">Sources and methodology</a>' in html  # app.js opens the details for this link
    # Every chart keeps a one-line source and time in view: the release radar, Want to Play, new releases and the
    # games in both lists.
    assert visible.count("IGDB PopScore type") == 4 and "Twitch API, Get Top Games" in visible


# ---------------------------------------------------------------------- phone layouts


def test_charts_and_lists_reflow_for_phones():
    phone = media(720)
    for selector in (".game-bar {", ".thumb.is-wide {", ".gb-line {", ".method-body {", ".footer-grid {",
                     ".nav-link {", ".card-grid {", ".radar-dot {", ".radar-group {"):
        assert selector in phone, selector
    assert "grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));" in rule(".rank-tiles")  # 1 column on a phone
    assert ".dash-grid { grid-template-columns: minmax(0, 1fr); }" in media(1100)  # charts stack below 1,100 px
    assert "padding-right: 5.5rem;" in rule(".gb-line")  # room for the value at the longest bar's tip
    assert "overflow-wrap: anywhere;" in rule(".gb-name, .rt-name, .split-link")  # long names wrap, never overflow
