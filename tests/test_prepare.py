"""Pure transformations: search keys, descriptions, store items and review excerpts."""

from datetime import date

import pytest

from game_librarian.prepare import (
    clean_review_text,
    concise_description,
    cover_url_from_store_item,
    description_blocks,
    normalize_for_search,
    parse_app_details,
    parse_release_date,
    parse_review_summary,
    percent_positive,
    preview_clip_from_store_item,
    screenshot_from_store_item,
    select_excerpts,
    shorten,
    sort_key,
    split_sentences,
    tags_from_store_item,
)
from tests.conftest import details_payload, review, trailer_item


def test_normalize_for_search_drops_symbols_accents_and_apostrophes():
    assert normalize_for_search("DARK SOULS™ II") == "dark souls ii"
    assert normalize_for_search("Baldur's Gate 3") == "baldurs gate 3"
    assert normalize_for_search("Marvel’s Spider-Man 2") == "marvels spider man 2"
    assert normalize_for_search("God of War Ragnarök") == "god of war ragnarok"
    assert normalize_for_search("Ratchet & Clank: Rift Apart") == "ratchet and clank rift apart"


def test_sort_key_ignores_a_leading_article_only():
    assert sort_key("The Witcher 3: Wild Hunt") == "witcher 3 wild hunt"
    assert sort_key("A Plague Tale: Requiem") == "plague tale requiem"
    assert sort_key("Theatre of War") == "theatre of war"


def test_description_blocks_keep_structure_and_drop_media():
    html = (
        '<h2 class="bb_tag">About This Game</h2>Explore a <strong>vast</strong> world.<br><br>'
        '<span class="bb_img_ctn"><video autoplay muted><source src="x.webm"></video></span>'
        '<h2 class="bb_tag">Key Features</h2><ul class="bb_ul"><li>Co-op for <i>four</i></li><li>Crafting<br></li></ul>'
        '<img src="https://example.invalid/banner.png">Fish &amp; chips.'
    )
    assert description_blocks(html) == [
        {"kind": "paragraph", "text": "Explore a vast world."},
        {"kind": "heading", "text": "Key Features"},
        {"kind": "list", "items": ["Co-op for four", "Crafting"]},
        {"kind": "paragraph", "text": "Fish & chips."},
    ]
    assert description_blocks("") == []


def test_parse_app_details_extracts_display_fields_from_steam_data():
    details = parse_app_details(10, details_payload(10, "Test Quest"))
    assert details.name == "Test Quest"
    assert details.short_description == "Test Quest is a test game about testing & more."
    assert details.about_blocks[0] == {"kind": "paragraph", "text": "You explore a test world."}
    assert details.developers == ["Test Studio"]  # trailing space from Steam removed
    assert details.modes == ["Single-player", "Online Co-op"]  # achievements are not a game mode
    assert details.platforms == ["Windows", "Linux"]
    assert details.release_date == "1 Jan, 2024"


def test_parse_app_details_does_not_repeat_the_short_description():
    data = details_payload(12, "Echo", short_description="An echo of a game.",
                           about_the_game="An echo  of a game.<br><br>More detail.")
    assert parse_app_details(12, data).about_blocks == [{"kind": "paragraph", "text": "More detail."}]


def test_parse_app_details_falls_back_to_detailed_description():
    data = details_payload(11, "Fallback", about_the_game="", detailed_description="Only here.")
    assert parse_app_details(11, data).about_blocks == [{"kind": "paragraph", "text": "Only here."}]


@pytest.mark.parametrize("text, expected", [
    ("Aug 3, 2023", date(2023, 8, 3)),  # the usual Steam store format
    ("3 Aug, 2023", date(2023, 8, 3)),  # Steam's day-first format
    ("  Sep  12 2024 ", date(2024, 9, 12)),  # extra spaces, no comma
    ("September 12, 2024", date(2024, 9, 12)),
    ("feb 29, 2024", date(2024, 2, 29)),  # a leap day
])
def test_parse_release_date_reads_exact_steam_dates(text, expected):
    assert parse_release_date(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "Coming soon", "To be announced", "Q3 2026", "Aug 2026", "2026", "Summer 2026",
    "03/08/2023",  # day or month first? never guessed
    "Feb 29, 2023", "Feb 30, 2024", "Aug 32, 2023",  # impossible dates
    "Smarch 3, 2023", "3 août 2023",  # not an English month name
    "Aug 3, 2023 (Early Access)",
])
def test_parse_release_date_never_guesses(text):
    assert parse_release_date(text) is None


def test_tags_follow_vote_weight_and_skip_unknown_ids():
    item = {"tags": [{"tagid": 19, "weight": 500}, {"tagid": 122, "weight": 900}, {"tagid": 99999, "weight": 950}]}
    assert tags_from_store_item(item, {19: "Action", 122: "RPG"}) == ["RPG", "Action"]


def test_cover_url_uses_the_asset_format_from_steam():
    item = {"assets": {"asset_url_format": "steam/apps/10/${FILENAME}?t=5", "library_capsule": "abc/library_600x900.jpg"}}
    assert cover_url_from_store_item(item) == (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/abc/library_600x900.jpg?t=5"
    )
    assert cover_url_from_store_item({"assets": {"header": "header.jpg"}}) is None


def test_clean_review_text_removes_markup_and_hides_spoilers():
    raw = (
        "[h1]Verdict[/h1]\r\nGreat [b]combat[/b].\n[spoiler]The king dies[/spoiler]\n"
        "[list][*]Pros[*]Cons[/list]\n\n\n\n[url=https://example.invalid]a link[/url] [img]https://x.invalid/y.png[/img]"
    )
    cleaned = clean_review_text(raw)
    assert "king dies" not in cleaned
    assert "(spoiler removed)" in cleaned
    assert "• Pros" in cleaned and "a link" in cleaned
    for leftover in ("[b]", "[/h1]", "[url", "x.invalid", "\n\n\n"):
        assert leftover not in cleaned


def test_shorten_cuts_at_a_word_boundary():
    assert shorten("short text", 50) == ("short text", False)
    assert shorten("one two three four five", 12) == ("one two…", True)


def test_select_excerpts_keeps_readable_reviews_of_the_requested_side():
    reviews = [
        review(1, "ok", voted_up=True),  # too short to be useful
        review(2, "A thoughtful positive review.", voted_up=True),
        review(3, "This one is negative and long enough.", voted_up=False),  # wrong side
        review(4, "Another positive review with detail.", voted_up=True, votes=7, playtime=90),
    ]
    excerpts = select_excerpts(reviews, positive=True, count=3)
    assert [e.recommendation_id for e in excerpts] == ["2", "4"]  # fewer than three: keep what exists
    assert excerpts[1].votes_up == 7 and excerpts[1].playtime_minutes == 90
    assert excerpts[1].created_at == "2023-11-14T22:13:20+00:00"


def test_review_summary_and_percent_match_steam_rounding():
    summary = parse_review_summary({"query_summary": {"total_reviews": 760746, "total_positive": 736469,
                                                      "total_negative": 24277, "review_score_desc": "Overwhelmingly Positive"}})
    assert (summary.total, summary.positive, summary.score_desc) == (760746, 736469, "Overwhelmingly Positive")
    assert percent_positive(summary.positive, summary.total) == 96  # 96.8% shown as 96%, like the store
    assert parse_review_summary({}).total == 0
    assert percent_positive(0, 0) is None


def test_split_sentences_keeps_initials_and_abbreviations_together():
    text = "Leon S. Kennedy tracks the daughter. Dr. Doom waits! Humans vs. Zombies? “Quoted” start. The end… is near."
    assert split_sentences(text) == [
        "Leon S. Kennedy tracks the daughter.", "Dr. Doom waits!", "Humans vs. Zombies?", "“Quoted” start.",
        "The end… is near.",
    ]
    assert split_sentences("") == []


def test_concise_description_keeps_whole_sentences_and_stays_short():
    assert concise_description("Explore a vast world. Fight monsters. Find loot.") == "Explore a vast world. Fight monsters. Find loot."
    long_first = "Explore a beautiful, vast and ruined world riddled with dangers and lost technologies. More text follows."
    assert concise_description(long_first) == "Explore a beautiful, vast and ruined world riddled with dangers and lost technologies."
    # A very short first sentence takes the next one, then the result is cut at a word boundary.
    short_first = "A storm is coming. " + "Venture into the complete game on PC and forge your own path " * 4
    summary = concise_description(short_first)
    assert summary.startswith("A storm is coming. Venture into") and summary.endswith("…") and len(summary) <= 161
    assert concise_description("") == "" and concise_description("No punctuation here") == "No punctuation here"


def test_preview_clip_prefers_a_gameplay_titled_all_ages_trailer():
    item = {"trailers": {"highlights": [trailer_item(10, 0, "Launch Trailer", True)],
                         "other_trailers": [trailer_item(10, 1, "Red Band Gameplay", False),
                                            trailer_item(10, 2, "Gameplay Overview", True)]}}
    clip = preview_clip_from_store_item(item)
    assert clip["title"] == "Gameplay Overview"
    assert clip["webm"] == "https://video.akamai.steamstatic.com/store_trailers/10/2/abc2/1/microtrailer.webm"
    assert clip["mp4"].endswith("/10/2/abc2/1/microtrailer.mp4")
    assert clip["poster"] == "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/102/movie_full.jpg?t=1"
    launch_only = preview_clip_from_store_item({"trailers": {"highlights": [trailer_item(10, 0, "Launch Trailer", True)]}})
    assert launch_only["title"] == "Launch Trailer"


def test_preview_clip_is_none_without_a_usable_trailer():
    no_files = trailer_item(10, 0, "Teaser", True)
    no_files["microtrailer"] = []
    unknown_age = trailer_item(10, 1, "Teaser", True)
    del unknown_age["all_ages"]
    for item in ({}, {"trailers": {"highlights": [no_files, unknown_age, trailer_item(10, 2, "Mature", False)]}}):
        assert preview_clip_from_store_item(item) is None


def test_screenshot_is_the_first_all_ages_one():
    item = {"screenshots": {
        "all_ages_screenshots": [{"filename": "steam/apps/10/ss_b.jpg?t=1", "ordinal": 2},
                                 {"filename": "steam/apps/10/ss_a.jpg?t=1", "ordinal": 1}],
        "mature_content_screenshots": [{"filename": "steam/apps/10/ss_m.jpg?t=1", "ordinal": 0}],
    }}
    assert screenshot_from_store_item(item) == "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/10/ss_a.jpg?t=1"
    assert screenshot_from_store_item({"screenshots": {"mature_content_screenshots": [{"filename": "x.jpg"}]}}) is None
