"""Version 0.8 display rules: the editorial award order, no badge without a verified production label,
and genre/tag filters that are applied together.

The web tests use the fourteen synced games of test_web.py: Game 01 (101) is verified AAA, Game 02 (102)
verified AA and Indie, and the other twelve have no verified label. Odd games are tagged RPG + Action, even
games RPG + Indie."""

import random
import re
from html import unescape

from fastapi.testclient import TestClient

from game_librarian.curated import NOMINATIONS, OTHER_WINS, award_display_group, load_curated
from tests.conftest import write_csv
from tests.test_curated import AWARD_ROW
from tests.test_web import AWARD, card_html, games, make_app, shown_appids, text_of  # noqa: F401


def award_order(tmp_path, rows) -> list[tuple]:
    """(organization, category, year, result) of app 10's awards, in display order."""
    write_csv(tmp_path / "games.csv", [{"steam_appid": "10", "title": "Test Quest"}])
    write_csv(tmp_path / "awards.csv", [{**AWARD_ROW, **row} for row in rows])
    return [(a.organization, a.category, a.year, a.result) for a in load_curated(tmp_path).awards_for(10)]


TOP_WINS = [  # every top award won, in the order of the file, not of the display
    {"organization": "The Steam Awards", "category": "Game of the Year", "year": "2023"},
    {"organization": "Game Developers Choice Awards", "category": "Game of the Year", "year": "2024"},
    {"organization": "BAFTA Games Awards", "category": "Best Game", "year": "2024"},
    {"organization": "The Game Awards", "category": "Game of the Year", "year": "2023"},
    {"organization": "D.I.C.E. Awards", "category": "Game of the Year", "year": "2024"},
]


# ---------------------------------------------------------------------- the editorial award order


def test_the_game_awards_goty_win_comes_first_then_the_other_top_wins_in_editorial_order(tmp_path):
    rows = TOP_WINS + [
        {"organization": "The Steam Awards", "category": "Labor of Love", "year": "2025"},  # newer, but another win
        {"organization": "BAFTA Games Awards", "category": "Best Game", "year": "2025", "result": "nominee"},
        {"organization": "The Game Awards", "category": "Best Independent Game", "year": "2025", "result": "nominee"},
    ]
    assert award_order(tmp_path, rows) == [
        ("The Game Awards", "Game of the Year", 2023, "winner"),  # first, although three wins are newer
        ("BAFTA Games Awards", "Best Game", 2024, "winner"),
        ("D.I.C.E. Awards", "Game of the Year", 2024, "winner"),
        ("Game Developers Choice Awards", "Game of the Year", 2024, "winner"),
        ("The Steam Awards", "Game of the Year", 2023, "winner"),
        ("The Steam Awards", "Labor of Love", 2025, "winner"),  # other wins
        ("The Game Awards", "Best Independent Game", 2025, "nominee"),  # nominations last; a tie in year goes
        ("BAFTA Games Awards", "Best Game", 2025, "nominee"),  # to the organizations' editorial order
    ]


def test_ties_go_to_the_newest_year_then_the_organization_then_the_category_and_never_change(tmp_path):
    rows = [
        {"organization": "Zeta Awards", "category": "Best Art", "year": "2024"},
        {"organization": "Alpha Awards", "category": "Best Music", "year": "2024"},
        {"organization": "The Steam Awards", "category": "Labor of Love", "year": "2024"},
        {"organization": "The Steam Awards", "category": "Better With Friends", "year": "2024"},
        {"organization": "The Game Awards", "category": "Best Independent Game", "year": "2022"},
        {"organization": "The Game Awards", "category": "Best Independent Game", "year": "2024"},
    ]
    expected = [
        ("The Game Awards", "Best Independent Game", 2024, "winner"),
        ("The Steam Awards", "Better With Friends", 2024, "winner"),  # same organization: category A–Z
        ("The Steam Awards", "Labor of Love", 2024, "winner"),
        ("Alpha Awards", "Best Music", 2024, "winner"),  # organizations outside the editorial list: A–Z
        ("Zeta Awards", "Best Art", 2024, "winner"),
        ("The Game Awards", "Best Independent Game", 2022, "winner"),  # older last
    ]
    assert award_order(tmp_path, rows) == expected
    for seed in range(5):  # the file's row order never changes the display order
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)
        assert award_order(tmp_path, shuffled) == expected


def test_a_game_of_the_year_nomination_stays_a_nomination(tmp_path):
    rows = [
        {"organization": "The Game Awards", "category": "Game of the Year", "year": "2025", "result": "nominee"},
        {"organization": "The Steam Awards", "category": "Labor of Love", "year": "2019"},
    ]
    order = award_order(tmp_path, rows)
    assert order == [("The Steam Awards", "Labor of Love", 2019, "winner"),
                     ("The Game Awards", "Game of the Year", 2025, "nominee")]
    nominee = load_curated(tmp_path).awards_for(10)[1]
    assert award_display_group(nominee) == NOMINATIONS and nominee.result == "nominee"  # not reclassified


def test_inconsistent_award_rows_are_ordered_safely_without_changing_them(tmp_path):
    rows = [
        {"organization": "Golden Joystick Awards", "category": "Game of the Year", "year": "2024"},  # not listed
        {"organization": "the game  awards", "category": "game of the year", "year": "2020"},  # other spelling
        {"organization": "BAFTA Games Awards", "category": "Best Game", "year": "TBA"},  # invalid: reported
        {"organization": "", "category": "Best Game", "year": "2021"},  # invalid: reported
    ]
    order = award_order(tmp_path, rows)
    assert order == [("the game  awards", "game of the year", 2020, "winner"),  # recognized, text kept as written
                     ("Golden Joystick Awards", "Game of the Year", 2024, "winner")]
    data = load_curated(tmp_path)
    assert award_display_group(data.awards_for(10)[1]) == OTHER_WINS
    award_problems = [problem for problem in data.problems if problem.startswith("awards.csv")]
    assert award_problems == ["awards.csv:4: year must be a four-digit year, got 'TBA'",
                              "awards.csv:5: organization is empty"]


def test_cards_show_the_highest_priority_awards_first_and_the_details_list_all_in_the_same_order(settings, games):
    write_csv(settings.curated_dir / "awards.csv", [
        {**AWARD, "organization": "The Steam Awards", "year": "2024", "category": "Labor of Love"},
        {**AWARD, "organization": "BAFTA Games Awards", "year": "2024", "category": "Best Game", "result": "nominee"},
        {**AWARD, "organization": "Game Developers Choice Awards", "year": "2024", "steam_mapping": "edition",
         "mapping_note": "The award went to the original release. Steam sells the remaster."},
        {**AWARD, "organization": "BAFTA Games Awards", "year": "2023", "category": "Best Game"},
        AWARD,  # The Game Awards 2023 Game of the Year winner
    ])
    client = TestClient(make_app(settings))
    card = text_of(card_html(client.get("/").text, 101))
    first_three = ["The Game Awards 2023 · Game of the Year · Winner",
                   "BAFTA Games Awards 2023 · Best Game · Winner",
                   "Game Developers Choice Awards 2024 · Game of the Year · Winner · for the original release"]
    positions = [card.index(text) for text in first_three]
    assert positions == sorted(positions)  # a card has room for three: the highest-priority ones
    assert "and 2 more award results in the details" in card and "Labor of Love" not in card

    panel = client.get("/games/101/panel").text
    rows = [text_of(row) for row in re.findall(r'<li class="award-row.*?</li>', panel, re.S)]
    assert [row.split(" Source")[0] for row in rows] == [
        "Winner The Game Awards 2023 · Game of the Year",
        "Winner BAFTA Games Awards 2023 · Best Game",
        "Winner Game Developers Choice Awards 2024 · Game of the Year",
        "Winner The Steam Awards 2024 · Labor of Love",
        "Nominee BAFTA Games Awards 2024 · Best Game",
    ]
    assert "For the original release. The award went to the original release. Steam sells the remaster." in rows[2]
    assert panel.count('class="award-source" href="https://example.invalid/winners"') == 5  # every source kept


# ---------------------------------------------------------------------- production labels


def test_only_verified_labels_show_a_badge_and_unlabeled_games_stay_in_every_list(settings, games):
    client = TestClient(make_app(settings))
    everything = client.get("/", params={"view": "all", "page": "1"}).text + client.get(
        "/", params={"view": "all", "page": "2"}).text
    assert sorted(shown_appids(everything)) == list(range(101, 115))  # no game is left out
    assert '<li class="label">AAA</li>' in card_html(everything, 101)
    for appid in range(103, 115):
        assert "label-list" not in card_html(everything, appid)
    assert "Unclassified" not in everything
    panel = client.get("/games/103/panel").text
    assert "label-list" not in panel and "Unclassified" not in panel
    # Its award row is only "needs_review", so no award is recorded for it.
    assert "Five-year rank #14 · No award recorded · ranked by Steam reception" in text_of(panel)
    assert 'class="label" href="https://example.invalid/budget"' in client.get("/games/101/panel").text
    # Labels do not order the ranking: the award winner, the nominee, then the others by Steam reception.
    assert shown_appids(client.get("/").text)[:3] == [101, 102, 114]
    assert sorted(shown_appids(client.get("/", params={"view": "big-budget"}).text)) == [101, 102]


def test_the_no_verified_label_choice_replaces_unclassified_and_old_links_keep_working(settings, games):
    client = TestClient(make_app(settings))
    new = client.get("/", params={"label": "No verified label", "view": "all"}).text
    old = client.get("/", params={"label": "Unclassified", "view": "all"}).text
    assert shown_appids(new) == shown_appids(old) and 101 not in shown_appids(new) and len(shown_appids(new)) == 12
    chips = text_of(re.search(r'<ul class="active-filters".*?</ul>', old, re.S).group(0))
    assert "No verified label" in chips and "Unclassified" not in old
    assert 'href="/games/103?label=No+verified+label&amp;view=all"' in old  # links carry the new name
    dashboard = client.get("/dashboard", params={"label": "Unclassified"}).text
    assert '<option value="No verified label" selected>No verified label</option>' in dashboard
    assert "12 of 14 catalog games match the filters." in dashboard


# ---------------------------------------------------------------------- genres and tags


def form_inputs(html: str) -> list[tuple[str, str]]:
    """What a browser without JavaScript sends when the filter form is submitted as rendered.

    Text and hidden inputs send their value, checkboxes only when checked, menus their selected option.
    """
    form = re.search(r'<form class="filters" id="filters" method="get" action="/".*?</form>', html, re.S).group(0)
    pairs = []
    for tag in re.findall(r"<input [^>]*>|<select .*?</select>", form, re.S):
        name = re.search(r'name="([^"]+)"', tag).group(1)
        if tag.startswith("<select"):
            chosen = re.search(r'<option value="([^"]*)"[^>]* selected', tag)
            pairs.append((name, unescape(chosen.group(1)) if chosen else ""))
        elif 'type="checkbox"' in tag:
            if " checked" in tag:
                pairs.append((name, unescape(re.search(r'value="([^"]*)"', tag).group(1))))
        else:
            pairs.append((name, unescape(re.search(r'value="([^"]*)"', tag).group(1))))
    return pairs


def tag_options(html: str) -> list[tuple[str, bool]]:
    """The tag list in page order: (tag, ticked)."""
    return [(unescape(value), bool(checked)) for value, checked in
            re.findall(r'<input type="checkbox" name="tag" value="([^"]+)"( checked)?>', html)]


def test_tag_boxes_wait_for_apply_filters_while_the_other_controls_still_apply_at_once(settings, games):
    html = TestClient(make_app(settings)).get("/").text
    assert re.findall(r'<input type="checkbox" name="tag" [^>]*>', html)
    assert not re.search(r'<input type="checkbox" name="tag" [^>]*data-autosubmit', html)  # no page load per box
    for name in ("label", "reception", "mode"):  # unchanged: these still apply when changed
        assert re.search(rf'<input type="checkbox" name="{name}" [^>]*data-autosubmit', html)
    fieldset = re.search(r'<fieldset class="field field-tags".*?</fieldset>', html, re.S).group(0)
    assert 'data-deferred-filters data-max-selected="10"' in fieldset and 'aria-describedby="tag-hint"' in fieldset
    assert "Tick as many as you like (up to 10), then select Apply filters." in text_of(fieldset)
    # Keyboard users reach a real submit button right after the tag list; the pending note is announced.
    after = html[html.index("</fieldset>", html.index("field field-tags")):]
    bar = re.search(r'<div class="filter-actions" data-apply-bar>.*?</div>', after, re.S).group(0)
    assert '<p class="pending-note" data-pending-note role="status"></p>' in bar
    assert '<button class="button button-primary" type="submit" id="apply-filters">Apply filters</button>' in bar
    assert after.index("data-apply-bar") < after.index("</form>")


def test_several_tags_apply_together_and_a_game_needs_every_one(settings, games):
    client = TestClient(make_app(settings))
    html = client.get("/", params=[("tag", "Indie"), ("tag", "RPG"), ("view", "all")]).text
    assert sorted(shown_appids(html)) == [102, 104, 106, 108, 110, 112, 114]  # the even games have both
    options = tag_options(html)
    assert options[:2] == [("Indie", True), ("RPG", True)]  # selected tags lead the list, so they stay in view
    assert all(not ticked for _, ticked in options[2:])
    assert shown_appids(client.get("/", params=[("tag", "Indie"), ("tag", "Action")]).text) == []  # no game has both


def test_submitting_the_form_without_javascript_applies_every_ticked_tag_and_keeps_the_rest(settings, games):
    client = TestClient(make_app(settings))
    start = client.get("/", params=[("view", "all"), ("sort", "newest"), ("q", "game")]).text
    sent = form_inputs(start)
    assert ("view", "all") in sent and ("sort", "newest") in sent and ("q", "game") in sent
    sent += [("tag", "RPG"), ("tag", "Indie")]  # two boxes ticked, then Apply filters
    result = client.get("/", params=sent).text
    assert sorted(shown_appids(result)) == [102, 104, 106, 108, 110, 112, 114]
    again = form_inputs(result)  # the next submit sends the same choices back
    assert ("tag", "RPG") in again and ("tag", "Indie") in again and ("sort", "newest") in again


def test_selected_tags_are_kept_by_search_sort_views_pages_and_details(settings, games):
    client = TestClient(make_app(settings))
    params = [("tag", "RPG"), ("tag", "Action"), ("sort", "name")]
    html = client.get("/", params=params).text
    kept = "tag=RPG&amp;tag=Action"
    assert f'href="/?{kept}&amp;view=all"' in html  # views
    assert f'href="/?{kept}&amp;sort=name&amp;view=goty"' in html  # collections
    assert f'href="/games/101?{kept}&amp;sort=name"' in html  # details links
    chips = text_of(re.search(r'<ul class="active-filters".*?</ul>', html, re.S).group(0))
    assert "RPG" in chips and "Action" in chips
    back = client.get("/games/101", params=params).text
    assert f'href="/?{kept}&amp;sort=name#game-101"' in back  # the details page links back with them
    paged = client.get("/", params=[("tag", "RPG"), ("sort", "name")]).text
    assert 'href="/?tag=RPG&amp;sort=name&amp;page=2" rel="next"' in paged  # pagination
    second = client.get("/", params=[("tag", "RPG"), ("sort", "name"), ("page", "2")]).text
    assert ("tag", "RPG") in form_inputs(second)  # page 2's form still sends the tag
