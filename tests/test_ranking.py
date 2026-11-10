"""Four-year ranking: the rolling window, production tiers, positive review counts, ties and missing data."""

from datetime import date

import pytest

from game_librarian.ranking import (
    NOT_IN_RANKING,
    TIER_AA,
    TIER_AAA,
    TIER_INDIE_AND_UNCLASSIFIED,
    GameFacts,
    RankingWindow,
    place_games,
    production_tier,
    unranked_reason,
)

TODAY = date(2026, 11, 9)
WINDOW = RankingWindow.ending_on(TODAY)  # 9 Nov 2022 to 9 Nov 2026
IN_WINDOW = "Mar 1, 2024"


def game(appid, positive, *, labels=(), total="auto", released=IN_WINDOW, name=None, coming_soon=False):
    """A game in the window with `positive` positive reviews (total = positive + 100 unless given)."""
    if total == "auto":
        total = None if positive is None else positive + 100
    return GameFacts(appid=appid, sort_name=name or f"game {appid:03d}", labels=labels, positive=positive,
                     total=total, release_date=released, coming_soon=coming_soon)


def rank_order(placements):
    """App ids of the ranked games, best first."""
    return [appid for appid, place in sorted(placements.items(), key=lambda item: item[1].rank or 0)
            if place.rank is not None]


# ---------------------------------------------------------------------- the window


def test_window_is_the_four_years_up_to_and_including_today():
    assert (WINDOW.start, WINDOW.end) == (date(2022, 11, 9), TODAY)


def test_window_moves_forward_with_today_and_handles_leap_days():
    assert RankingWindow.ending_on(date(2027, 1, 15)).start == date(2023, 1, 15)
    assert RankingWindow.ending_on(date(2028, 2, 29)).start == date(2024, 2, 29)
    assert RankingWindow.ending_on(date(2104, 2, 29)).start == date(2100, 2, 28)  # 2100 has no 29 February


def test_release_dates_on_the_cutoff_or_today_are_included():
    placements = place_games([
        game(1, 10, released="Nov 9, 2022"),  # exactly the cutoff
        game(2, 20, released="9 Nov, 2022"),  # the cutoff in Steam's day-first format
        game(3, 30, released="Nov 9, 2026"),  # released today
    ], WINDOW)
    assert all(place.in_window and place.rank for place in placements.values())
    assert placements[1].released_on == placements[2].released_on == date(2022, 11, 9)


def test_older_and_future_release_dates_are_outside_the_window():
    placements = place_games([
        game(1, 10, released="Nov 8, 2022"),  # one day before the cutoff
        game(2, 20, released="Nov 10, 2026"),  # tomorrow, although Steam does not say "coming soon"
        game(3, 30, released="Coming soon", coming_soon=True),
        game(4, 40, released="Oct 1, 2026", coming_soon=True),  # an exact past date, but Steam says not released
        game(5, 50),
    ], WINDOW)
    assert placements[1].window_note == "Released before the four-year window."
    assert placements[2].window_note == "Its release date is after today."
    assert placements[3].window_note.startswith("Not released yet")
    assert placements[4].window_note.startswith("Not released yet")
    for appid in (1, 2, 3, 4):
        assert placements[appid].rank is None and placements[appid].status == NOT_IN_RANKING
    assert placements[5].rank == 1  # the only game in the window


@pytest.mark.parametrize("text", [None, "", "   ", "Q4 2025", "2025", "Nov 2025", "Coming soon", "To be announced",
                                  "Feb 30, 2024", "03/04/2024", "Smarch 4, 2024"])
def test_missing_imprecise_or_invalid_release_dates_are_never_guessed(text):
    place = place_games([game(1, 10, released=text)], WINDOW)[1]
    assert place.released_on is None and place.rank is None and not place.in_window
    assert place.window_note in ("Steam lists no release date.", f"Steam gives no exact release date (“{(text or '').strip()}”).")


def test_the_same_game_leaves_the_window_as_time_moves_forward():
    on_cutoff = [game(1, 10, released="Nov 9, 2022")]
    assert place_games(on_cutoff, RankingWindow.ending_on(date(2026, 11, 9)))[1].rank == 1
    assert place_games(on_cutoff, RankingWindow.ending_on(date(2026, 11, 10)))[1].rank is None


def test_ranks_are_calculated_only_among_games_in_the_window():
    placements = place_games([
        game(1, 9_000_000, labels=("AAA",), released="Apr 18, 2011"),  # would be first if it were eligible
        game(2, 1_000),
        game(3, 2_000),
    ], WINDOW)
    assert rank_order(placements) == [3, 2]
    assert sorted(place.rank for place in placements.values() if place.rank) == [1, 2]  # no gap left by game 1
    assert placements[1].tier == TIER_AAA and placements[1].unranked_reason is None  # usable, just too old


# ---------------------------------------------------------------------- tiers and positive counts


def test_tiers_come_before_positive_review_counts():
    placements = place_games([
        game(1, 4_000_000),  # Unclassified, most positive reviews
        game(2, 5_000_000, labels=("Indie",)),
        game(3, 1_000_000, labels=("AA",)),
        game(4, 1_000, labels=("AAA",)),  # AAA, fewest positive reviews
    ], WINDOW)
    assert rank_order(placements) == [4, 3, 2, 1]
    assert [placements[appid].tier for appid in (4, 3, 2, 1)] == [TIER_AAA, TIER_AA, 3, 3]


def test_a_game_with_two_verified_labels_ranks_in_its_higher_tier():
    assert production_tier({"AA", "Indie"}) == TIER_AA
    assert production_tier({"AAA", "Indie"}) == TIER_AAA
    assert production_tier({"Indie"}) == production_tier(set()) == TIER_INDIE_AND_UNCLASSIFIED
    placements = place_games([game(1, 900, labels=("Indie",)), game(2, 100, labels=("AA", "Indie"))], WINDOW)
    assert rank_order(placements) == [2, 1]
    assert placements[2].tier_name == "AA" and placements[1].tier_name == "Indie and Unclassified"


def test_more_positive_reviews_rank_higher_within_a_tier():
    placements = place_games([game(1, 10, labels=("AAA",)), game(2, 30, labels=("AAA",)), game(3, 20, labels=("AAA",)),
                              game(4, 5), game(5, 50)], WINDOW)
    assert rank_order(placements) == [2, 3, 1, 5, 4]


def test_indie_and_unclassified_share_one_tier_ordered_by_positive_reviews():
    unclassified_first = place_games([game(1, 100_000, labels=("Indie",)), game(2, 200_000)], WINDOW)
    assert rank_order(unclassified_first) == [2, 1]  # Unclassified 200,000 above Indie 100,000
    indie_first = place_games([game(1, 300_000, labels=("Indie",)), game(2, 200_000)], WINDOW)
    assert rank_order(indie_first) == [1, 2]  # Indie 300,000 above Unclassified 200,000


def test_the_count_matters_not_the_positive_percentage():
    placements = place_games([game(1, 100_000, total=400_000), game(2, 90_000, total=90_000)], WINDOW)
    assert rank_order(placements) == [1, 2]  # 25% of 400,000 beats 100% of 90,000


def test_equal_counts_break_ties_by_title_then_app_id_whatever_the_input_order():
    games = [game(7, 500, name="beta"), game(5, 500, name="alpha"), game(3, 500, name="alpha"), game(9, 600, name="zeta")]
    assert rank_order(place_games(games, WINDOW)) == [9, 3, 5, 7]
    assert place_games(games, WINDOW) == place_games(list(reversed(games)), WINDOW)


# ---------------------------------------------------------------------- unusable review data


def test_missing_zero_and_inconsistent_review_counts_are_unranked_not_zero():
    placements = place_games([
        game(1, None),  # reviews never fetched
        game(2, 0, total=0),  # no reviews on Steam (or an empty answer, saved as zeros)
        game(3, 12, total=10),  # more positive reviews than reviews in total
        game(4, -1, total=10),
        game(5, 10, total=None),
        game(6, 50, labels=("AAA",), total=None),  # an AAA game without counts does not take a rank either
        game(7, 0, total=40),  # a real zero: 40 reviews, none positive
        game(8, 20),
    ], WINDOW)
    for appid in (1, 2, 3, 4, 5, 6):
        place = placements[appid]
        assert place.rank is None and place.in_window and place.status == "Unranked"
        assert place.note == place.unranked_reason and place.unranked_reason
    assert rank_order(placements) == [8, 7]
    assert "no Steam reviews" in unranked_reason(0, 0)
    assert unranked_reason(0, 40) is None


def test_placement_notes_say_why_a_game_has_no_rank():
    placements = place_games([game(1, 10), game(2, None), game(3, None, released="Apr 18, 2011")], WINDOW)
    assert (placements[1].status, placements[1].note) == ("Ranked", None)
    assert placements[2].status == "Unranked" and placements[2].note.startswith("No Steam review summary")
    assert placements[3].status == NOT_IN_RANKING and placements[3].note == "Released before the four-year window."


def test_an_empty_catalog_ranks_nothing():
    assert place_games([], WINDOW) == {}
