"""Dashboard calculations: measures, filters, chart positions and number formats (pages are tested in test_web.py)."""

import pytest

from game_librarian import dashboard
from game_librarian.report_export import ReportTables


def game(appid, positive=None, total=None, *, label="No verified label", status="No listed award", rank=None,
         band="Below 70%"):
    """A games.csv row as collect_tables builds it, with only what the dashboard reads."""
    usable = total is not None
    return {
        "appid": appid, "title": f"Game {appid}", "production_label": label, "award_status": status,
        "has_review_data": usable, "positive_reviews": positive, "total_reviews": total,
        "positive_share": positive / total if usable else None,
        "steam_percent_positive": positive * 100 // total if usable else None,
        "positive_band": band if usable else "No usable review data",
        "four_year_rank": rank, "rank_status": "Ranked" if rank else "Not in the five-year ranking",
        "in_four_year_window": rank is not None, "catalog_source": "Hand-picked",
    }


def entry(year, award, result, title, status="In catalog", appid=None):
    """An awards.csv row as collect_tables builds it."""
    return {"year": year, "award_name": award, "result": result, "is_winner": result == "Winner",
            "award_title": title, "catalog_status": status, "catalog_appid": appid}


def tables(games=(), awards=(), game_tags=()):
    return ReportTables(games=list(games), game_tags=list(game_tags), tags=[], awards=list(awards), last_sync=None,
                        excluded_award_rows=0)


def test_the_two_rates_weigh_reviews_and_games_differently():
    games = [game(1, 900, 1000), game(2, 10, 100), game(3)]  # game 3 has no usable review data
    measures = dashboard.reception_measures(games)
    assert (measures["games"], measures["games_with_review_data"], measures["steam_reviews"]) == (3, 2, 1100)
    assert measures["weighted_rate"] == pytest.approx(910 / 1100)  # every review pooled: the big game dominates
    assert measures["average_rate"] == pytest.approx((0.9 + 0.1) / 2)  # every game counts once
    empty = dashboard.reception_measures([game(4)])
    assert (empty["weighted_rate"], empty["average_rate"]) == (None, None)


def test_labels_follow_the_label_order_with_no_verified_label_last():
    labels = ["No verified label", "Indie", "AA + Indie", "AAA", "AA", "AAA + Indie"]
    assert sorted(labels, key=dashboard.label_sort_key) == ["AAA", "AAA + Indie", "AA", "AA + Indie", "Indie",
                                                           "No verified label"]
    rows = dashboard.rates_by_label([game(1, 9, 10, label="Indie"), game(2, 1, 10, label="AAA"), game(3, label="AA")])
    assert [(row["label"], row["games"]) for row in rows] == [("AAA", 1), ("Indie", 1)]  # AA has no review data


def test_the_scatter_puts_reviews_on_a_log_scale():
    chart = dashboard.scatter_chart([game(1, 9, 10, rank=4), game(2, 600_000, 1_000_000), game(3)])
    assert [(point["x"], point["y"]) for point in chart["points"]] == [(0.0, 75.0), (100.0, 0.0)]
    assert [tick["label"] for tick in chart["x_ticks"]] == ["10", "100", "1K", "10K", "100K", "1M"]
    assert [tick["label"] for tick in chart["y_ticks"]] == ["60%", "70%", "80%", "90%", "100%"]  # from the lowest share
    assert chart["points"][0]["title"] == "Game 1: 10 reviews, 90% positive, #4 in the five-year ranking"
    assert chart["points"][1]["title"].endswith("60% positive, not in the five-year ranking")
    low = dashboard.scatter_chart([game(1, 3, 10), game(2, 100, 100)])  # 30%: steps of 20 from 20%
    assert [tick["label"] for tick in low["y_ticks"]] == ["20%", "40%", "60%", "80%", "100%"]
    one = dashboard.scatter_chart([game(1, 1000, 1000)])  # a single game still gets a decade and a 10-point range
    assert ([tick["label"] for tick in one["x_ticks"]], one["points"][0]["y"]) == (["1K", "10K"], 100.0)
    assert dashboard.scatter_chart([game(1)]) is None


def test_bands_count_every_game_once_in_band_order():
    rows = dashboard.band_rows([game(1, 99, 100, band="95–100%"), game(2, 96, 100, band="95–100%"),
                                game(3, 50, 100), game(4)])
    assert [(row["band"], row["games"], row["width"], row["tone"]) for row in rows] == [
        ("95–100%", 2, 100.0, "1"), ("90–94%", 0, 0.0, "2"), ("80–89%", 0, 0.0, "3"), ("70–79%", 0, 0.0, "4"),
        ("Below 70%", 1, 50.0, "5"), ("No usable review data", 1, 50.0, "none")]


def test_reception_filters_combine_and_unknown_values_are_ignored():
    games = [game(1, 9, 10, label="AAA", status="Winner"), game(2, 9, 10, label="Indie"), game(3, 9, 10)]
    data = tables(games, game_tags=[{"appid": 1, "tag": "RPG"}, {"appid": 2, "tag": "RPG"}])
    data.tags = [{"tag": "RPG", "games": 2}]
    assert dashboard.reception_page(data, {"tag": "RPG"})["measures"]["games"] == 2
    page = dashboard.reception_page(data, {"tag": "RPG", "label": "Indie"})
    assert (page["measures"]["games"], page["filtered"]) == (1, True)
    assert dashboard.reception_page(data, {"award_status": "Winner"})["measures"]["games"] == 1
    unknown = dashboard.reception_page(data, {"tag": "Puzzle", "label": "Legendary", "award_status": "<b>"})
    assert (unknown["measures"]["games"], unknown["filtered"]) == (3, False)
    # "Unclassified" is the pre-0.8 name of "No verified label": an older link keeps its filter.
    for value in ("No verified label", "Unclassified"):
        unlabeled = dashboard.reception_page(data, {"label": value})
        assert (unlabeled["filters"]["label"], unlabeled["measures"]["games"]) == ("No verified label", 1)


def test_award_charts_stack_winners_under_nominees_and_sort_the_entries():
    goty, indie = "Org · Game of the Year", "Org · Best Indie"
    awards = [entry(2024, goty, "Nominee", "Beta"), entry(2024, goty, "Winner", "Zeta", appid=7),
              entry(2024, indie, "Nominee", "Alpha", status="Not mapped to Steam"),
              entry(2022, goty, "Winner", "Old", status="On Steam, not in catalog")]
    page = dashboard.awards_page(tables(awards=awards), {})
    assert page["measures"] == {"entries": 4, "winners": 2, "nominees": 2, "awarded_games": 1, "coverage": 0.5}
    columns = page["by_year"]["columns"]
    assert [(c["year"], c["winners"], c["nominees"], c["height"]) for c in columns] == [(2022, 1, 0, 33.33),
                                                                                       (2024, 1, 2, 100.0)]
    assert [part["name"] for part in columns[1]["parts"]] == ["Nominee", "Winner"]  # top to bottom
    assert [part["name"] for part in columns[0]["parts"]] == ["Winner"]  # no empty part above it
    assert [tick["value"] for tick in page["by_year"]["ticks"]] == [0, 1, 2, 3]
    assert [(row["award"], row["counts"], row["width"]) for row in page["by_award"]] == [
        (indie, [0, 0, 1], 33.33), (goty, [2, 1, 0], 100.0)]
    # Newest year first, then the award A–Z, its winner before its nominees.
    assert [e["award_title"] for e in page["entries"]] == ["Alpha", "Zeta", "Beta", "Old"]
    assert dashboard.awards_page(tables(awards=awards), {"result": "Winner", "year": "2024"})["measures"]["entries"] == 1
    assert dashboard.awards_page(tables(), {})["by_year"] is None


@pytest.mark.parametrize("largest, ticks", [(0, [0, 1]), (3, [0, 1, 2, 3]), (37, [0, 10, 20, 30, 40]),
                                            (48, [0, 10, 20, 30, 40, 50]), (120, [0, 50, 100, 150])])
def test_count_axes_use_round_steps(largest, ticks):
    assert dashboard.axis_ticks(largest) == ticks


@pytest.mark.parametrize("value, text", [(0, "0"), (950, "950"), (1_284, "1.3K"), (19_250, "19.2K"),
                                         (999_960, "1M"), (47_515_291, "47.5M"), (10_000_000, "10M")])
def test_compact_numbers(value, text):
    assert dashboard.compact_number(value) == text


def test_rates_are_percentages_and_missing_rates_say_so():
    assert (dashboard.format_rate(0.894226), dashboard.format_rate(0.856, 0), dashboard.format_rate(None)) == (
        "89.4%", "86%", "No data")
