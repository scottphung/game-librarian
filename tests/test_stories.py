"""Data Stories (stories.py): the shipped stories are valid and sourced; dates, waits and counts are calculated right."""

import json
from datetime import date

import pytest

from game_librarian import stories
from game_librarian.config import DEFAULT_CURATED_DIR

SHIPPED = stories.load_stories(DEFAULT_CURATED_DIR)


def test_the_shipped_stories_load_without_problems_and_cite_exact_wikipedia_revisions():
    assert SHIPPED.problems == []
    assert list(SHIPPED.stories) == ["final-fantasy", "grand-theft-auto"]  # A–Z by title
    for story in SHIPPED.stories.values():
        assert story.checked_on == date(2026, 11, 19) and story.includes and story.excludes
        for source in story.sources:
            assert source.url.startswith("https://en.wikipedia.org/w/index.php?title=") and f"&oldid={source.revision}" \
                in source.url
            assert source.revision_time.date() <= story.checked_on
        for entry in story.entries:
            assert entry.platforms and entry.role in stories.ROLES


def test_grand_theft_auto_lists_the_eight_main_games_in_order_with_their_first_platforms():
    story = SHIPPED.stories["grand-theft-auto"]
    assert [entry.title for entry in story.main_entries] == [
        "Grand Theft Auto", "Grand Theft Auto 2", "Grand Theft Auto III", "Grand Theft Auto: Vice City",
        "Grand Theft Auto: San Andreas", "Grand Theft Auto IV", "Grand Theft Auto V", "Grand Theft Auto VI"]
    first, sixth = story.main_entries[0], story.main_entries[-1]
    assert (first.released.label, first.region, first.platforms) == ("28 Nov 1997", "Europe", ("MS-DOS", "Windows"))
    assert (sixth.status, sixth.released.first_day, sixth.platforms) == ("released", date(2026, 11, 19),
                                                                         ("PlayStation 5", "Xbox Series X/S"))
    # Same day: the main game comes before the handheld game.
    titles = [entry.title for entry in story.entries]
    assert titles.index("Grand Theft Auto: San Andreas") + 1 == titles.index("Grand Theft Auto Advance")
    assert [row["family"] for row in stories.platform_families(story)] == ["PlayStation", "Xbox", "PC"]
    assert [row["games"] for row in stories.platform_families(story)] == [7, 3, 2]


def test_grand_theft_auto_waits_between_main_games_are_exact_calendar_differences():
    gaps = stories.main_gaps(SHIPPED.stories["grand-theft-auto"])
    assert [gap.days for gap in gaps] == [693, 732, 371, 728, 1_281, 1_967, 4_811]
    assert gaps[0].text == "1 year, 10 months and 24 days" and gaps[0].short == "1 yr 10 mo"
    summary = stories.gap_summary(gaps)
    assert summary["longest"].text == "13 years, 2 months and 2 days"  # V (17 Sep 2013) to VI (19 Nov 2026)
    assert summary["shortest"].later.title == "Grand Theft Auto: Vice City" and summary["median_days"] == 732


def test_final_fantasy_covers_all_sixteen_main_games_and_keeps_the_upcoming_remake_out_of_the_numbers():
    story = SHIPPED.stories["final-fantasy"]
    assert len(story.main_entries) == 16 and story.main_entries[-1].title == "Final Fantasy XVI"
    revelation = next(entry for entry in story.entries if entry.title == "Final Fantasy VII Revelation")
    assert revelation.upcoming and revelation.role == "remake" and revelation.of == ("Final Fantasy VII",)
    mystic_quest = next(entry for entry in story.entries if entry.title == "Final Fantasy Mystic Quest")
    assert (mystic_quest.released.precision, mystic_quest.released.label) == ("month", "Oct 1992")
    gaps = stories.main_gaps(story)
    assert len(gaps) == 15 and all(not gap.later.upcoming for gap in gaps)
    summary = stories.gap_summary(gaps)
    assert summary["longest"].span == (6, 6, 24)  # XV (29 Nov 2016) to XVI (22 Jun 2023)
    assert summary["shortest"].span == (0, 9, 13)  # XIII (17 Dec 2009) to XIV (30 Sep 2010)
    latest, span = stories.since_latest_main(story)
    assert latest.title == "Final Fantasy XVI" and span == (3, 4, 28)


@pytest.mark.parametrize("start, end, expected", [
    (date(1997, 11, 28), date(1999, 10, 22), (1, 10, 24)),
    (date(1987, 12, 18), date(1988, 12, 17), (0, 11, 29)),  # a leap year in between: still 365 days
    (date(2025, 1, 31), date(2025, 3, 1), (0, 1, 1)),  # 31 Jan + 1 month is 28 Feb
    (date(2024, 2, 29), date(2025, 2, 28), (1, 0, 0)),  # a year after a leap day ends on 28 Feb
    (date(2020, 5, 5), date(2020, 5, 5), (0, 0, 0)),
])
def test_calendar_difference(start, end, expected):
    assert stories.calendar_difference(start, end) == expected


def test_calendar_difference_refuses_an_end_before_the_start():
    with pytest.raises(ValueError):
        stories.calendar_difference(date(2020, 1, 2), date(2020, 1, 1))


def entry(title, role="main", first_release="2001-01-01", status="released", **extra) -> dict:
    return {"title": title, "role": role, "of": [], "status": status, "first_release": first_release,
            "platforms": ["Windows"], "note": "", "source": {"title": title, "revision": 1,
                                                              "revision_time": "2026-01-01T00:00:00Z"}, **extra}


def write_story(tmp_path, entries, **fields) -> None:
    folder = tmp_path / "stories"
    folder.mkdir(exist_ok=True)
    story = {"slug": "test-series", "title": "Test Series", "summary": "A test.", "checked_on": "2026-11-19",
             "checked_by": "Tests", "includes": ["Everything in the test."], "excludes": [], "notes": [],
             "series_sources": [], "entries": entries, **fields}
    (folder / f"{fields.get('slug', 'test-series')}.json").write_text(json.dumps(story), encoding="utf-8")


def test_invalid_entries_are_reported_and_left_out_never_guessed(tmp_path):
    write_story(tmp_path, [
        entry("Good One"),
        entry("Good Two", first_release="2003-06"),  # month precision: shown, but no wait is calculated
        entry("Bad Role", role="prequel"),
        entry("Bad Date", first_release="2003-13-01"),
        entry("Too Late", first_release="2027-01-01"),  # released, but after the check date
        entry("Stale Upcoming", status="upcoming", first_release="2026-01-01"),
        entry("Odd Platform", platforms=["Holodeck"]),
        entry("Dangling", role="remake", first_release="2004-01-01", of=["Not In Story"]),  # kept, but reported
        entry("Good One"),  # duplicate title
    ])
    collection = stories.load_stories(tmp_path)
    story = collection.stories["test-series"]
    assert [item.title for item in story.entries] == ["Good One", "Good Two", "Dangling"]
    problems = " | ".join(collection.problems)
    for text in ("role must be one of", "first_release must be", "cannot be after checked_on",
                 "date has passed", "unknown platform names", "names games not in this story", "duplicate title"):
        assert text in problems, text
    gap = stories.main_gaps(story)[0]
    assert not gap.counted and gap.text.startswith("Not calculated") and stories.gap_summary([gap])["longest"] is None


def test_a_file_with_a_wrong_frame_is_skipped(tmp_path):
    write_story(tmp_path, [entry("Only Game")], slug="Wrong Slug")
    collection = stories.load_stories(tmp_path)
    assert collection.stories == {} and "slug must be" in collection.problems[0]
    assert stories.load_stories(tmp_path / "missing").stories == {}  # no folder: no stories, no crash


def test_release_chart_positions_stay_inside_the_plot_and_mark_upcoming_games():
    chart = stories.release_chart(SHIPPED.stories["final-fantasy"])
    assert [row["label"] for row in chart["rows"]] == [label for label, _ in stories.CHART_ROWS]
    dots = [dot for row in chart["rows"] for dot in row["dots"]]
    assert len(dots) == 35 and all(0 <= dot["x"] <= 100 for dot in dots)
    assert [dot["entry"].title for dot in dots if dot["entry"].upcoming] == ["Final Fantasy VII Revelation"]
    assert (chart["first"], chart["last"]) == (1987, 2027) and chart["ticks"][0]["year"] == 1990
