"""Collections and sort orders (catalog.py): membership rules at their edges, missing data, ties."""

from datetime import date, timedelta

import pytest

from game_librarian import catalog
from game_librarian.catalog import VIEWS, CatalogGame, sort_games
from game_librarian.curated import Award

TODAY = date(2026, 11, 15)


def award(organization="The Game Awards", category="Game of the Year", result="winner", year=2023) -> Award:
    return Award(organization=organization, year=year, category=category, result=result, award_title="x", appid=1,
                 mapping="same", mapping_note="", source_url="https://example.invalid", review_status="verified",
                 checked_on="2026-11-15")


def game(appid, name="", *, labels=(), awards=(), positive=900, total=1_000, released=date(2024, 1, 1),
         upcoming=False, in_window=True, rank=None) -> CatalogGame:
    return CatalogGame(appid=appid, sort_name=name or f"game {appid:03d}", labels=tuple(labels), awards=tuple(awards),
                       positive=positive, total=total, released_on=released, upcoming=upcoming, in_window=in_window,
                       rank=rank)


def members(key, games):
    return [g.appid for g in games if VIEWS[key].includes(g, TODAY)]


# ---------------------------------------------------------------------- collections


def test_goty_winners_need_a_verified_win_in_a_top_category():
    games = [
        game(1, awards=[award()]),  # The Game Awards GOTY winner
        game(2, awards=[award("BAFTA Games Awards", "Best Game")]),  # BAFTA's equivalent
        game(3, awards=[award(result="nominee")]),  # a nomination is not a win
        game(4, awards=[award(category="Best Independent Game")]),  # a win, but not the top category
        game(5, awards=[award("The Steam Awards", "Labor of Love")]),
        game(6, awards=[award("The Steam Awards", "Game of the Year")]),
        game(7),
    ]
    assert members("goty", games) == [1, 2, 6]
    assert members("awards", games) == [1, 2, 3, 4, 5, 6]  # any verified result, win or nomination


def test_highly_rated_indies_need_the_label_500_reviews_and_90_percent():
    games = [
        game(1, labels=["Indie"], positive=450, total=500),  # exactly 90% of exactly 500
        game(2, labels=["Indie"], positive=449, total=500),  # 89.8% is shown as 89%
        game(3, labels=["Indie"], positive=499, total=499),  # too few reviews
        game(4, labels=["AA"], positive=990, total=1_000),  # no Indie label
        game(5, labels=["Indie"], positive=None, total=None),  # no usable reviews: never assumed
        game(6, labels=["AA", "Indie"], positive=950, total=1_000),
    ]
    assert members("indies", games) == [1, 6]


def test_big_budget_games_need_a_verified_aaa_or_aa_label():
    games = [game(1, labels=["AAA"]), game(2, labels=["AA", "Indie"]), game(3, labels=["Indie"]), game(4)]
    assert members("big-budget", games) == [1, 2]


def test_hidden_gems_are_well_liked_games_with_500_to_20000_reviews():
    games = [
        game(1, positive=450, total=500), game(2, positive=18_000, total=20_000),  # both edges are inside
        game(3, positive=18_001, total=20_001),  # more reviews than the cap
        game(4, positive=499, total=499),  # too few reviews to trust the share
        game(5, positive=4_400, total=5_000),  # 88% positive
        game(6, positive=None, total=None),
    ]
    assert members("hidden-gems", games) == [1, 2]


def test_recent_releases_use_the_last_365_days_including_today():
    start, end = catalog.recent_window(TODAY)
    assert (end - start).days == 364 and end == TODAY
    games = [game(1, released=TODAY), game(2, released=start), game(3, released=start - timedelta(days=1)),
             game(4, released=TODAY + timedelta(days=1), upcoming=True), game(5, released=None)]
    assert members("recent", games) == [1, 2]


def test_every_collection_documents_its_rule_and_a_valid_default_sort():
    for key in catalog.COLLECTION_KEYS:
        view = VIEWS[key]
        assert view.is_collection and len(view.description) > 40 and view.default_sort in catalog.SORTS


# ---------------------------------------------------------------------- sort orders


def ids(games):
    return [g.appid for g in games]


def test_most_positive_reviews_counts_reviews_and_lists_missing_data_last():
    games = [game(1, "b", positive=10, total=11), game(2, "a", positive=500, total=1_000),
             game(3, "c", positive=None, total=None), game(4, "a", positive=500, total=600)]
    assert ids(sort_games(games, "most-positive", TODAY)) == [2, 4, 1, 3]  # ties: title, then app id


def test_best_reception_does_not_let_a_handful_of_reviews_lead():
    games = [
        game(1, "tiny", positive=20, total=20),  # 100% of 20 reviews
        game(2, "solid", positive=540, total=600),  # 90% of 600
        game(3, "great", positive=9_500, total=10_000),  # 95% of 10,000
        game(4, "same share more reviews", positive=1_900, total=2_000),  # 95% of 2,000
        game(5, "unknown", positive=None, total=None),
        game(6, "small but loved", positive=45, total=50),
    ]
    assert ids(sort_games(games, "reception", TODAY)) == [3, 4, 2, 1, 6, 5]


def test_newest_release_uses_exact_dates_and_lists_unknown_and_upcoming_dates_after():
    games = [game(1, "old", released=date(2015, 5, 1)), game(2, "new", released=date(2026, 10, 1)),
             game(3, "b soon", released=date(2027, 1, 1), upcoming=True), game(4, "a unknown", released=None),
             game(5, "also new", released=date(2026, 10, 1))]
    assert ids(sort_games(games, "newest", TODAY)) == [5, 2, 1, 4, 3]


def test_rank_order_lists_ranked_games_first_and_name_order_is_library_order():
    games = [game(1, "c", rank=2), game(2, "b"), game(3, "a", rank=1), game(4, "a")]
    assert ids(sort_games(games, "rank", TODAY)) == [3, 1, 4, 2]
    assert ids(sort_games(games, "name", TODAY)) == [3, 4, 2, 1]


@pytest.mark.parametrize("old, new", [("reviews", "most-positive"), ("positive", "reception")])
def test_old_sort_values_have_aliases(old, new):
    assert catalog.SORT_ALIASES[old] == new and new in catalog.SORTS
