"""Data Stories: image records and their checks, the compact year-by-year timeline, labeled cover art credited once
(an IGDB link beside each game's Wikipedia link, and the full list under "Show all sources"), the placeholder for a
game without an image, the collapsed sources and method, and (0.10) no public fact-check credit or notes box."""

import re
import shutil
from html import unescape

from fastapi.testclient import TestClient

from game_librarian import stories
from game_librarian.config import DEFAULT_CURATED_DIR
from game_librarian.web import create_app
from tests.test_stories import SHIPPED, entry, write_story
from tests.test_web import TODAY, text_of

COVER = {"source": "IGDB", "kind": "cover", "igdb_id": 980, "image_id": "co2lb0",
         "igdb_url": "https://www.igdb.com/games/grand-theft-auto", "checked_on": "2026-11-19"}


def story_client(settings, entries=None) -> TestClient:
    """The site with the shipped stories, or with one test story made of `entries`."""
    if entries is None:
        shutil.copytree(DEFAULT_CURATED_DIR / "stories", settings.curated_dir / "stories")
    else:
        write_story(settings.curated_dir, entries)
    return TestClient(create_app(settings, today=lambda: TODAY))


# ---------------------------------------------------------------------- image records


def test_every_shipped_entry_names_a_checked_igdb_cover():
    for story in SHIPPED.stories.values():
        for item in story.entries:
            assert item.image is not None, item.title
            assert item.image.igdb_url.startswith("https://www.igdb.com/games/")
            assert item.image.checked_on == story.checked_on
    ff6 = next(item for item in SHIPPED.stories["final-fantasy"].entries if item.title == "Final Fantasy VI")
    assert ff6.image.note == "The North American box, where the game was titled Final Fantasy III."


def test_an_invalid_image_record_is_reported_and_dropped_but_the_game_stays(tmp_path):
    write_story(tmp_path, [
        entry("Good Cover", image=COVER),
        entry("Wiki Image", image={**COVER, "source": "Wikipedia"}),  # only IGDB cover art is accepted
        entry("Odd Id", image={**COVER, "image_id": "../x.png"}),
        entry("Bad Page", image={**COVER, "igdb_url": "https://example.com/game"}),
        entry("No Check Date", image={**COVER, "checked_on": "soon"}),
        entry("Not An Object", image="cover.jpg"),
        entry("No Image"),
    ])
    collection = stories.load_stories(tmp_path)
    story = collection.stories["test-series"]
    assert [item.title for item in story.entries] == ["Good Cover", "Wiki Image", "Odd Id", "Bad Page",
                                                      "No Check Date", "Not An Object", "No Image"]
    assert [item.title for item in story.entries if item.image] == ["Good Cover"]
    problems = " | ".join(collection.problems)
    for text in ("only source 'IGDB' with kind 'cover'", "image_id must be IGDB's image id",
                 "igdb_url must start with https://www.igdb.com/games/", "checked_on must be a YYYY-MM-DD date",
                 "image left out: it must be an object"):
        assert text in problems, text
    assert len(collection.problems) == 5


def test_an_entry_image_is_labeled_cover_art_and_credited_never_called_gameplay(tmp_path):
    write_story(tmp_path, [entry("Grand Theft Auto", image=COVER), entry("No Image", first_release="2002-02-02")])
    item, bare = stories.load_stories(tmp_path).stories["test-series"].entries
    image = stories.entry_image(item)
    assert image.url == "https://images.igdb.com/igdb/image/upload/t_cover_small/co2lb0.jpg"
    assert image.url_2x == "https://images.igdb.com/igdb/image/upload/t_cover_small_2x/co2lb0.jpg"
    assert (image.kind, image.alt, image.credit) == ("Cover art", "Cover art of Grand Theft Auto", "IGDB.com")
    assert image.credit_url == "https://www.igdb.com/games/grand-theft-auto" and "gameplay" not in image.kind.lower()
    assert stories.entry_image(bare) is None  # the page shows a plain placeholder


# ---------------------------------------------------------------------- the page


def test_the_timeline_groups_games_by_year_with_one_compact_row_each(settings):
    html = story_client(settings).get("/stories/grand-theft-auto").text
    years = re.findall(r'<p class="tl-year-label">(\d{4})</p>', html)
    assert years == ["1997", "1999", "2001", "2002", "2004", "2005", "2006", "2008", "2009", "2013", "2021", "2026"]
    year_1999 = re.search(r'<p class="tl-year-label">1999</p>.*?</ol>', html, re.S).group(0)
    assert year_1999.count('<li class="story-entry') == 3  # London 1969, London 1961, Grand Theft Auto 2
    row = text_of(re.search(r'<li class="story-entry is-main">.*?</li>', year_1999, re.S).group(0))
    assert "22 Oct Grand Theft Auto 2" in row and "Main game · First released" not in row
    assert "Main game First released on PlayStation and Windows" in row  # no region in the source: none shown
    # Facts are kept: chronology, roles, regions and platforms, the exact date for screen readers.
    text = text_of(html)
    assert "Expansion of Grand Theft Auto IV" in text and "First released in Europe on MS-DOS and Windows" in text
    assert '<span class="visually-hidden">22 Oct 1999</span>' in year_1999
    assert html.count('<li class="story-entry') == 18


def test_every_image_is_labeled_and_credited_once_and_a_game_without_one_gets_a_plain_box(settings):
    html = story_client(settings, [entry("Covered Game", image=COVER), entry("Bare Game", first_release="2002-02-02")]
                        ).get("/stories/test-series").text
    covered = re.search(r'<li class="story-entry is-main">(?:(?!</li>).)*Covered Game.*?</li>', html, re.S).group(0)
    assert 'src="https://images.igdb.com/igdb/image/upload/t_cover_small/co2lb0.jpg"' in covered
    assert 'srcset="https://images.igdb.com/igdb/image/upload/t_cover_small_2x/co2lb0.jpg 2x"' in covered
    assert 'alt="Cover art of Covered Game"' in covered and 'loading="lazy"' in covered
    assert 'data-image-fallback' in covered  # a broken image becomes the gray box (app.js)
    # No caption under the image: the credit is a short IGDB link right after the Wikipedia link.
    assert "<figcaption" not in covered and "Cover art · IGDB.com" not in text_of(covered)
    links = re.search(r'<p class="entry-links">.*?</p>', covered, re.S).group(0)
    assert re.search(r'>Wikipedia<span class="visually-hidden">[^<]*</span></a> · <a class="text-link" '
                     r'href="https://www.igdb.com/games/grand-theft-auto" target="_blank" rel="noopener">IGDB'
                     r'<span class="visually-hidden">\.com: the game’s record, source of its cover art', links)
    bare = re.search(r'<li class="story-entry is-main">(?:(?!</li>).)*Bare Game.*?</li>', html, re.S).group(0)
    assert '<span class="tl-frame is-empty"><span class="visually-hidden">No suitable image</span></span>' in bare
    assert "<img" not in bare and "IGDB" not in bare
    note = text_of(re.search(r'<section class="chart-card story-sources".*?</p>', html, re.S).group(0))
    assert ("Images: cover art from IGDB.com for 1 of 2 games (1 without a suitable image); each game links to its "
            "IGDB record.") in note
    # Every credit is still listed in full, inside "Show all sources".
    credits = re.search(r'<ol class="source-list image-credits">.*?</ol>', html, re.S).group(0)
    assert [text_of(item) for item in re.findall(r"<li>.*?</li>", credits, re.S)] == [
        "Covered Game: Cover art · IGDB.com", "Bare Game: no suitable image"]
    assert 'href="https://www.igdb.com/games/grand-theft-auto"' in credits
    assert "gameplay screenshot" not in text_of(covered)


def test_a_cover_with_another_regional_title_says_so(settings):
    html = story_client(settings).get("/stories/final-fantasy").text
    row = re.search(r'<li class="story-entry is-main">(?:(?!</li>).)*Final Fantasy VI</span>.*?</li>', html, re.S)
    assert "Cover art: The North American box, where the game was titled Final Fantasy III." in text_of(row.group(0))


def test_sources_and_method_are_collapsed_behind_a_short_visible_note_and_keep_every_revision(settings):
    client = story_client(settings)
    html = client.get("/stories/final-fantasy").text
    story = SHIPPED.stories["final-fantasy"]
    section = re.search(r'<section class="chart-card story-sources" id="sources".*?</section>', html, re.S).group(0)
    note = text_of(re.search(r'<p class="chart-sub">.*?</p>', section, re.S).group(0))
    assert note.startswith(f"Facts from {len(story.sources)} Wikipedia article revisions read on 19 Nov 2026")
    assert "CC BY-SA 4.0" in note and "cover art from IGDB.com for 35 of 35 games" in note
    details = re.search(r'<details class="source-details" id="all-sources">.*?</details>', section, re.S).group(0)
    assert "<summary>Show all sources</summary>" in details  # exactly this label: no count in it
    for source in story.sources:  # every revision link and attribution is still there, inside the details
        assert f'href="{source.url.replace("&", "&amp;")}"' in details
    text = text_of(details)
    for phrase in ("Method", "First release: the earliest date in the game's Wikipedia infobox",
                   "Waits between main games:", "Images", "No gameplay screenshots.",
                   "not licensed for reuse", "Image credits", "Attribution and license",
                   "Creative Commons Attribution-ShareAlike 4.0 License",
                   "Notes on this story", *story.notes):  # the old notes box, kept with the method
        assert phrase in text, phrase
    assert 'href="#all-sources">Sources</a>' in html  # the header's link opens the details (app.js)
    assert "<details" not in html[:html.index('id="timeline-title"')].split("story-chart")[0]  # facts stay visible


def test_the_stories_name_no_ai_fact_checker_and_show_no_notes_box(settings):
    client = story_client(settings)
    for slug, story in SHIPPED.stories.items():
        html = client.get(f"/stories/{slug}").text
        text = text_of(html)
        assert "Claude" not in html and "AI assistant" not in text and story.checked_by not in html
        header = text_of(re.search(r'<header class="story-header">.*?</header>', html, re.S).group(0))
        assert "Facts checked 19 Nov 2026 against the Wikipedia revisions listed under Sources." in header
        # The yellow box is gone; its notes are kept once, inside "Show all sources", and the timeline still
        # carries each game's own note.
        assert "data-note" not in html and "story-notes" not in html
        sources = re.search(r'<details class="source-details" id="all-sources">.*?</details>', html, re.S).group(0)
        for note in story.notes:
            assert html.count(note) == 1 and note in text_of(sources), note
    index = client.get("/stories").text
    assert "Claude" not in index and "AI assistant" not in index


def test_the_scope_lists_fold_into_one_card_that_works_without_javascript(settings):
    html = story_client(settings).get("/stories/final-fantasy").text
    scope = re.search(r'<details class="chart-card story-scope" id="scope">.*?</details>', html, re.S).group(0)
    assert '<summary class="card-summary-title">What this story includes and leaves out</summary>' in scope
    story = SHIPPED.stories["final-fantasy"]
    for line in (*story.includes, *story.excludes):  # nothing is dropped, only folded
        assert line in unescape(scope), line
    # The charts keep the short notes needed to read them.
    text = text_of(html)
    assert "Blue: main games · gray: other entries · hollow: upcoming." in text
    assert "Time from one main game’s first release to the next. Upcoming games are not counted." in text
