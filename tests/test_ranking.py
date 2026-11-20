"""The ranking: the website's five-year window, the export's rolling four-year window, award groups, award counts,
Steam reception, ties and missing data."""

from datetime import date

import pytest

from game_librarian.curated import Award, Classification, CuratedData
from game_librarian.ranking import (
    AWARD_GROUP_NAMES,
    NO_AWARD_GROUP,
    RELIABLE_REVIEW_COUNT,
    GameFacts,
    RankingWindow,
    award_summary,
    place_catalog,
    place_games,
    reception_key,
    review_problem,
)

TODAY = date(2026, 11, 9)
WINDOW = RankingWindow.ending_on(TODAY)  # the Power BI export's rolling window: 9 Nov 2022 to 9 Nov 2026
WEBSITE = RankingWindow.for_website(TODAY)  # 1 Jan 2021 to 9 Nov 2026
IN_WINDOW = "Mar 1, 2024"
NOT_IN_EXPORT_RANKING = "Not in the four-year ranking"

# (organization, category) of the awards used below.
TGA = ("The Game Awards", "Game of the Year")
BAFTA = ("BAFTA Games Awards", "Best Game")
DICE = ("D.I.C.E. Awards", "Game of the Year")
GDCA = ("Game Developers Choice Awards", "Game of the Year")
STEAM = ("The Steam Awards", "Game of the Year")
INDIE = ("The Game Awards", "Best Independent Game")
LABOR_OF_LOVE = ("The Steam Awards", "Labor of Love")


def award(appid, which=TGA, result="winner", *, year=2023, mapping="same", status="verified"):
    """A curated award row for Steam app `appid`: The Game Awards' 2023 Game of the Year, won, unless told otherwise."""
    organization, category = which
    return Award(organization=organization, year=year, category=category, result=result, award_title=f"Game {appid}",
                 appid=appid, mapping=mapping, mapping_note="", source_url="https://example.invalid/results",
                 review_status=status, checked_on="2026-11-09")


def game(appid, positive=900, *, total="auto", awards=(), released=IN_WINDOW, name=None, coming_soon=False):
    """A game in the window with `positive` positive reviews (total = positive + 100 unless given)."""
    if total == "auto":
        total = None if positive is None else positive + 100
    return GameFacts(appid=appid, sort_name=name or f"game {appid:03d}", positive=positive, total=total,
                     release_date=released, coming_soon=coming_soon, awards=tuple(awards))


def rank_order(placements):
    """App ids of the ranked games, best first."""
    return [appid for appid, place in sorted(placements.items(), key=lambda item: item[1].rank or 0)
            if place.rank is not None]


# ---------------------------------------------------------------------- the website's window


def test_website_window_is_five_full_years_before_this_year_plus_this_year_so_far():
    assert (WEBSITE.start, WEBSITE.end, WEBSITE.name) == (date(2021, 1, 1), TODAY, "five-year")


def test_website_window_moves_forward_on_1_january_only():
    assert RankingWindow.for_website(date(2026, 1, 1)).start == date(2021, 1, 1)
    assert RankingWindow.for_website(date(2026, 12, 31)).start == date(2021, 1, 1)
    assert RankingWindow.for_website(date(2027, 1, 1)) == RankingWindow(date(2022, 1, 1), date(2027, 1, 1), "five-year")
    assert RankingWindow.for_website(date(2028, 2, 29)).start == date(2023, 1, 1)  # a leap day needs no special case


def test_website_window_includes_its_first_day_and_today_and_names_the_five_year_ranking():
    placements = place_games([
        game(1, 10, released="Jan 1, 2021"),  # the first day of the window
        game(2, 20, released="Dec 31, 2020"),  # one day too old
        game(3, 30, released="Nov 9, 2026"),  # released today
        game(4, 40, released="Nov 10, 2026"),  # tomorrow
    ], WEBSITE)
    assert placements[1].rank and placements[3].rank
    assert placements[2].rank is None and placements[4].rank is None
    assert (placements[2].status, placements[2].note) == ("Not in the five-year ranking",
                                                          "Released before the five-year window.")
    assert (placements[4].status, placements[4].note) == ("Not in the five-year ranking",
                                                          "Its release date is after today.")


def test_a_game_from_2021_or_early_2022_is_ranked_on_the_website_but_not_in_the_export():
    games = [game(1, 10, released="Mar 1, 2022"), game(2, 20, released=IN_WINDOW)]
    assert place_games(games, WEBSITE)[1].rank is not None
    assert place_games(games, WINDOW)[1].status == NOT_IN_EXPORT_RANKING  # the export keeps its rolling window


def test_a_game_from_1_january_2021_leaves_the_website_window_on_1_january_2027():
    first_day = [game(1, 10, released="Jan 1, 2021", awards=[award(1)])]
    assert place_games(first_day, RankingWindow.for_website(date(2026, 12, 31)))[1].rank == 1
    assert place_games(first_day, RankingWindow.for_website(date(2027, 1, 1)))[1].rank is None


# ---------------------------------------------------------------------- the export's rolling window


def test_window_is_the_four_years_up_to_and_including_today():
    assert (WINDOW.start, WINDOW.end, WINDOW.name) == (date(2022, 11, 9), TODAY, "four-year")


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
        assert placements[appid].rank is None and placements[appid].status == NOT_IN_EXPORT_RANKING
    assert placements[5].rank == 1  # the only game in the window


@pytest.mark.parametrize("text", [None, "", "   ", "Q4 2025", "2025", "Nov 2025", "Coming soon", "To be announced",
                                  "Feb 30, 2024", "03/04/2024", "Smarch 4, 2024"])
def test_missing_imprecise_or_invalid_release_dates_are_never_guessed(text):
    place = place_games([game(1, 10, released=text)], WINDOW)[1]
    assert place.released_on is None and place.rank is None and not place.in_window
    assert place.window_note in ("Steam lists no release date.", f"Steam gives no exact release date (“{(text or '').strip()}”).")


def test_the_same_game_leaves_the_window_as_time_moves_forward():
    on_cutoff = [game(1, 10, released="Nov 9, 2022", awards=[award(1)])]
    assert place_games(on_cutoff, RankingWindow.ending_on(date(2026, 11, 9)))[1].rank == 1
    assert place_games(on_cutoff, RankingWindow.ending_on(date(2026, 11, 10)))[1].rank is None


def test_ranks_are_calculated_only_among_games_in_the_window():
    placements = place_games([
        game(1, 9_000_000, awards=[award(1)], released="Apr 18, 2011"),  # a Game of the Year winner, but too old
        game(2, 1_000),
        game(3, 2_000),
    ], WINDOW)
    assert rank_order(placements) == [3, 2]
    assert sorted(place.rank for place in placements.values() if place.rank) == [1, 2]  # no gap left by game 1
    assert placements[1].awards.group == 1 and placements[1].review_note is None  # usable, just too old


# ---------------------------------------------------------------------- award groups


def test_award_groups_follow_the_editorial_priority_whatever_the_reviews():
    # Each later group has more and better reviews, so only the award group can explain the order.
    games = [
        game(1, 1_000, total=10_000, awards=[award(1, TGA)]),  # 10% positive
        game(2, 2_000, total=10_000, awards=[award(2, BAFTA)]),
        game(3, 3_000, total=10_000, awards=[award(3, DICE)]),
        game(4, 4_000, total=10_000, awards=[award(4, GDCA)]),
        game(5, 5_000, total=10_000, awards=[award(5, STEAM)]),
        game(6, 6_000, total=10_000, awards=[award(6, INDIE)]),  # any other win
        game(7, 7_000, total=10_000, awards=[award(7, TGA, "nominee")]),  # a Game of the Year nomination
        game(8, 999_000, total=1_000_000),  # 99.9% of a million reviews, no award recorded
    ]
    placements = place_games(reversed(games), WINDOW)
    assert rank_order(placements) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [placements[appid].awards.group for appid in range(1, 9)] == list(range(1, 9)) == list(AWARD_GROUP_NAMES)
    assert [placements[appid].group_name for appid in range(1, 9)] == [
        "The Game Awards — Game of the Year winner",
        "BAFTA Games Awards — Best Game winner",
        "D.I.C.E. Awards — Game of the Year winner",
        "Game Developers Choice Awards — Game of the Year winner",
        "The Steam Awards — Game of the Year winner",
        "Other verified award winner",
        "Verified nominations, no win",
        "No award recorded",
    ]
    assert NO_AWARD_GROUP == 8


def test_the_game_awards_winner_ranks_above_every_lower_group_even_with_fewer_awards():
    placements = place_games([
        game(1, 500, total=1_000, awards=[award(1, TGA)]),  # one win, 50% positive
        game(2, 999_000, total=1_000_000, awards=[award(2, STEAM), award(2, INDIE), award(2, LABOR_OF_LOVE),
                                                  award(2, BAFTA, "nominee")]),
        game(3, 990_000, total=1_000_000, awards=[award(3, GDCA), award(3, INDIE, year=2024)]),
    ], WINDOW)
    assert rank_order(placements) == [1, 3, 2]  # The Game Awards, then the GDCA, then The Steam Awards


def test_the_highest_priority_result_sets_the_group_and_a_nomination_is_never_a_win():
    summary = award_summary(1, [award(1, STEAM, year=2024), award(1, TGA, "nominee"), award(1, INDIE)])
    assert (summary.group, summary.wins, summary.nominations) == (5, 2, 1)  # The Steam Awards' win, not TGA's nomination
    nominee = award_summary(1, [award(1, TGA, "nominee"), award(1, BAFTA, "nominee")])
    assert (nominee.group, nominee.wins, nominee.nominations) == (7, 0, 2)


def test_a_game_without_a_recorded_award_is_in_the_last_group():
    summary = award_summary(1, [])
    assert (summary.group, summary.wins, summary.nominations, summary.group_name) == (8, 0, 0, "No award recorded")


# ---------------------------------------------------------------------- the number of awards


def test_more_distinct_wins_then_more_nominations_rank_higher_within_a_group():
    placements = place_games([
        game(1, 990, total=1_000, awards=[award(1, GDCA)]),  # 1 win, 99% positive
        game(2, 600, total=1_000, awards=[award(2, GDCA), award(2, STEAM, year=2024), award(2, INDIE)]),  # 3 wins
        game(3, 700, total=1_000, awards=[award(3, GDCA), award(3, INDIE)]),  # 2 wins
        game(4, 800, total=1_000, awards=[award(4, GDCA), award(4, INDIE), award(4, BAFTA, "nominee"),
                                          award(4, TGA, "nominee")]),  # 2 wins and 2 nominations
    ], WINDOW)
    assert {placements[appid].awards.group for appid in (1, 2, 3, 4)} == {4}  # all GDCA Game of the Year winners
    assert rank_order(placements) == [2, 4, 3, 1]
    assert [(placements[appid].awards.wins, placements[appid].awards.nominations) for appid in (2, 4, 3, 1)] == [
        (3, 0), (2, 2), (2, 0), (1, 0)]


def test_nominated_games_without_a_win_are_ordered_by_their_number_of_nominations():
    placements = place_games([
        game(1, 990, total=1_000, awards=[award(1, TGA, "nominee")]),
        game(2, 600, total=1_000, awards=[award(2, TGA, "nominee"), award(2, BAFTA, "nominee"),
                                          award(2, GDCA, "nominee")]),
        game(3, 700, total=1_000, awards=[award(3, TGA, "nominee"), award(3, BAFTA, "nominee")]),
    ], WINDOW)
    assert rank_order(placements) == [2, 3, 1]
    assert [placements[appid].awards.nominations for appid in (2, 3, 1)] == [3, 2, 1]


def test_duplicate_records_count_once_and_a_won_award_is_not_also_a_nomination():
    duplicated = [
        award(1, TGA), award(1, TGA),  # the same result recorded twice
        award(1, ("the game awards", "Game of  the Year ")),  # the same award, written with other case and spacing
        award(1, TGA, "nominee"),  # a nomination for the award it won
        award(1, BAFTA, "nominee"), award(1, BAFTA, "nominee"),
    ]
    summary = award_summary(1, duplicated)
    assert (summary.group, summary.wins, summary.nominations) == (1, 1, 1)
    # So the duplicates win no place: with the same counts, the better reception ranks higher.
    placements = place_games([game(1, 500, total=1_000, awards=duplicated),
                              game(2, 990, total=1_000, awards=[award(2, TGA), award(2, BAFTA, "nominee")])], WINDOW)
    assert rank_order(placements) == [2, 1]
    # Different years are different results.
    assert award_summary(1, [award(1, INDIE, year=2023), award(1, INDIE, year=2024)]).wins == 2


def test_only_verified_results_mapped_to_the_game_count():
    summary = award_summary(1, [
        award(1, TGA, status="needs_review"),
        award(1, BAFTA, status="rejected"),
        award(2, GDCA),  # mapped to another Steam app
        award(None, STEAM, mapping="none"),  # not mapped to any Steam app
        award(1, INDIE, "nominee"),
    ])
    assert (summary.group, summary.wins, summary.nominations) == (7, 0, 1)
    assert award_summary(1, [award(1, TGA, status="needs_review")]).group == NO_AWARD_GROUP


def test_an_award_to_the_original_release_counts_for_the_edition_and_says_so():
    edition = award_summary(1, [award(1, TGA, mapping="edition"), award(1, BAFTA, "nominee", mapping="edition")])
    assert (edition.group, edition.wins, edition.nominations, edition.for_original_release) == (1, 1, 1, True)
    assert award_summary(2, [award(2, TGA)]).for_original_release is False
    # Only when every result of the group went to the original release: here a win of this app's own also counts.
    mixed = award_summary(3, [award(3, INDIE, mapping="edition"), award(3, LABOR_OF_LOVE), award(3, TGA, "nominee")])
    assert (mixed.group, mixed.wins, mixed.for_original_release) == (6, 2, False)
    nominated = award_summary(4, [award(4, TGA, "nominee", mapping="edition")])
    assert (nominated.group, nominated.for_original_release) == (7, True)
    # It ranks like any other result of its group.
    placements = place_games([game(1, 500, total=1_000, awards=[award(1, TGA, mapping="edition")]),
                              game(2, 900, total=1_000, awards=[award(2, TGA)])], WINDOW)
    assert rank_order(placements) == [2, 1] and placements[1].awards.group == placements[2].awards.group == 1


# ---------------------------------------------------------------------- Steam reception


def test_reception_breaks_ties_with_500_reviews_first_then_share_then_review_count():
    placements = place_games([
        game(1, 499, total=499),  # 100% of 499 reviews: fewer than 500
        game(2, 450, total=500),  # 90% of exactly 500 reviews: enough
        game(3, 900, total=1_000),  # 90% of 1,000: the same share, more reviews
        game(4, 950, total=1_000),  # 95%
        game(5, 9, total=10),  # 90% of 10
        game(6, 1, total=1),  # 100% of 1
    ], WINDOW)
    assert RELIABLE_REVIEW_COUNT == 500
    assert rank_order(placements) == [4, 3, 2, 1, 6, 5]


def test_the_positive_share_counts_not_the_positive_count():
    placements = place_games([game(1, 100_000, total=400_000), game(2, 90_000, total=90_000)], WINDOW)
    assert rank_order(placements) == [2, 1]  # 100% of 90,000 beats 25% of 400,000


def test_reception_key_is_the_best_player_reception_rule():
    assert reception_key(450, 500) < reception_key(499, 499) < reception_key(1, 1) < reception_key(None, None)
    assert reception_key(None, None) == reception_key(12, 10) == reception_key(0, 0) == (2, 0.0, 0)
    assert reception_key(900, 1_000) < reception_key(450, 500)  # the same share: more reviews first


def test_missing_zero_and_inconsistent_review_counts_are_unavailable_not_zero():
    placements = place_games([
        game(1, None),  # reviews never fetched
        game(2, 0, total=0),  # no reviews on Steam (or an empty answer, saved as zeros)
        game(3, 12, total=10),  # more positive reviews than reviews in total
        game(4, -1, total=10),
        game(5, 10, total=None),
        game(6, 0, total=40),  # a real zero: 40 reviews, none positive
        game(7, 20),
    ], WINDOW)
    for appid in (1, 2, 3, 4, 5):
        place = placements[appid]
        assert place.rank is None and place.in_window and place.status == "Unranked"
        assert place.review_note
        assert place.note == f"No award recorded and no usable Steam review summary. {place.review_note}"
    assert rank_order(placements) == [7, 6]  # 0% of 40 reviews is real data, so it is ranked (last)
    assert "no Steam reviews" in review_problem(0, 0) and "inconsistent" in review_problem(12, 10)
    assert review_problem(0, 40) is None


def test_an_award_winner_without_usable_reviews_keeps_its_award_based_place():
    placements = place_games([
        game(1, None, awards=[award(1, TGA)]),  # The Game Awards' Game of the Year, no review summary saved
        game(2, 999_000, total=1_000_000),  # no award, 99.9% of a million reviews
        game(3, 900, total=1_000, awards=[award(3, TGA)]),  # the same group and counts, with reviews
        game(4, 12, total=10, awards=[award(4, BAFTA, "nominee")]),  # a nomination, inconsistent counts
        game(5, None, awards=[award(5, TGA), award(5, INDIE)]),  # more wins outweigh missing reviews
    ], WINDOW)
    assert rank_order(placements) == [5, 3, 1, 4, 2]
    assert (placements[1].status, placements[1].note) == ("Ranked", None)
    assert placements[1].review_note.startswith("No Steam review summary")
    assert placements[4].review_note.startswith("The stored Steam review counts are inconsistent")


# ---------------------------------------------------------------------- labels, ties and notes


def test_an_indie_winner_ranks_above_an_aaa_game_without_a_recorded_award():
    def label(appid, name):
        return Classification(appid=appid, label=name, review_status="verified", evidence_url="https://example.invalid",
                              evidence_summary="Test evidence.", checked_on="2026-11-09")

    curated = CuratedData(awards_by_appid={2: [award(2, INDIE)]},
                          labels_by_appid={1: {"AAA": label(1, "AAA")}, 2: {"Indie": label(2, "Indie")}})
    rows = [
        {"appid": 1, "sort_name": "big game", "review_positive": 990_000, "review_total": 1_000_000,
         "release_date": IN_WINDOW, "coming_soon": 0},
        {"appid": 2, "sort_name": "small game", "review_positive": 600, "review_total": 1_000,
         "release_date": IN_WINDOW, "coming_soon": 0},
    ]
    placements = place_catalog(rows, curated, WINDOW)
    assert rank_order(placements) == [2, 1]
    assert (placements[2].group_name, placements[1].group_name) == ("Other verified award winner", "No award recorded")


def test_full_ties_go_to_the_title_then_the_app_id_whatever_the_input_order():
    games = [game(7, 500, name="beta"), game(5, 500, name="alpha"), game(3, 500, name="alpha"), game(9, 600, name="zeta")]
    assert rank_order(place_games(games, WINDOW)) == [9, 3, 5, 7]
    assert place_games(games, WINDOW) == place_games(list(reversed(games)), WINDOW)
    winners = [game(4, 500, awards=[award(4)], name="beta"), game(2, 500, awards=[award(2)], name="alpha"),
               game(1, 500, awards=[award(1)], name="beta")]
    assert rank_order(place_games(winners, WINDOW)) == [2, 1, 4]


def test_placement_notes_say_why_a_game_has_no_rank():
    placements = place_games([game(1, 10), game(2, None), game(3, None, released="Apr 18, 2011"),
                              game(4, None, awards=[award(4, TGA, "nominee")])], WINDOW)
    assert (placements[1].status, placements[1].note) == ("Ranked", None)
    assert placements[2].status == "Unranked"
    assert placements[2].note == ("No award recorded and no usable Steam review summary. "
                                  "No Steam review summary has been saved for this game yet.")
    assert placements[3].status == NOT_IN_EXPORT_RANKING and placements[3].note == "Released before the four-year window."
    assert (placements[4].status, placements[4].note) == ("Ranked", None)  # a nomination is enough


def test_an_empty_catalog_ranks_nothing():
    assert place_games([], WINDOW) == {}
