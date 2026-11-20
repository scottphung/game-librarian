"""Publisher grouping (publishers.py): spellings of one publisher become one choice; different publishers stay apart."""

import pytest

from game_librarian.publishers import build_index, clean_name, publisher_key


@pytest.mark.parametrize("name, cleaned", [
    ("BANDAI NAMCO Entertainment", "BANDAI NAMCO Entertainment"),
    ("Bandai Namco Entertainment Inc.", "Bandai Namco Entertainment"),
    ("CAPCOM Co., Ltd.", "CAPCOM"),  # two company types in a row
    ("FromSoftware, Inc. (Japan)", "FromSoftware"),  # a qualifier, then a company type
    ("Feral interactive (Mac)", "Feral interactive"),
    ("Activision (Excluding Japan and Asia)", "Activision"),
    ("Wube Software LTD.", "Wube Software"),
    ("Hempuli Oy", "Hempuli"),
    ("方块游戏 (Asia)", "方块游戏"),  # names in any script keep their letters
    ("Limited Run Games", "Limited Run Games"),  # company words are only removed at the end
    ("Coffee Stain Publishing", "Coffee Stain Publishing"),
    ("Evil Landfall?", "Evil Landfall?"),  # Steam's spelling is kept for display
    ("Inc.", "Inc."),  # nothing would be left, so the name is kept
])
def test_clean_name_removes_only_trailing_qualifiers_and_company_types(name, cleaned):
    assert clean_name(name) == cleaned


def test_spellings_of_one_publisher_share_a_key_and_related_companies_do_not():
    assert publisher_key("BANDAI NAMCO Entertainment") == publisher_key("Bandai Namco Entertainment Inc.") \
        == "bandai namco entertainment"
    assert publisher_key("Feral Interactive (Linux)") == publisher_key("Feral interactive (Mac)") == "feral interactive"
    assert publisher_key("SQUARE ENIX") == publisher_key("Square Enix") == "square enix"
    assert publisher_key("KONAMI") == publisher_key("Konami") == "konami"
    assert publisher_key("SEGA (Japan)") == publisher_key("SEGA") == "sega"
    # A shared owner does not make one publisher: these stay separate choices.
    assert len({publisher_key(name) for name in ("Lucasfilm", "LucasArts", "Disney")}) == 3


def test_the_index_counts_each_game_once_and_shows_the_most_common_spelling():
    index = build_index({
        1: ["BANDAI NAMCO Entertainment", "FromSoftware, Inc."],
        2: ["Bandai Namco Entertainment"],
        3: ["Bandai Namco Entertainment Inc."],
        4: ["Square Enix", "Feral interactive (Mac)", "Feral Interactive (Linux)"],  # two Feral spellings, one game
        5: [],  # Steam lists no publisher
    })
    assert [(publisher.name, publisher.games) for publisher in index.choices()] == [
        ("Bandai Namco Entertainment", 3), ("Feral Interactive", 1), ("FromSoftware", 1), ("Square Enix", 1)]
    assert index.keys_for(4) == {"square enix", "feral interactive"}
    assert index.keys_for(5) == frozenset() and index.keys_for(99) == frozenset()
    assert index.find("BANDAI NAMCO ENTERTAINMENT INC.").name == "Bandai Namco Entertainment"  # any spelling
    assert index.find("Nintendo") is None and index.find("   ") is None


def test_curated_aliases_join_a_renamed_publisher_under_the_alias_name():
    aliases = {publisher_key("WB Games"): "Warner Bros. Games",
               publisher_key("Warner Bros. Interactive Entertainment"): "Warner Bros. Games"}
    names = {1: ["WB Games"], 2: ["Warner Bros. Interactive Entertainment", "Warner Bros. Games"],
             3: ["Warner Bros. Games"]}
    index = build_index(names, aliases)
    assert [(publisher.name, publisher.games) for publisher in index.choices()] == [("Warner Bros. Games", 3)]
    assert index.find("WB Games").name == "Warner Bros. Games"  # the old spelling finds the publisher
    # Without the curated rows nothing is merged: names that differ in words are different publishers.
    assert len(build_index(names).choices()) == 3
