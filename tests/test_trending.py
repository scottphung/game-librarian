"""Trending sections (trending.py): separate signals, their windows, and changes only between real snapshots."""

from datetime import date, datetime, timedelta, timezone

from game_librarian import trending
from game_librarian.catalog import CatalogGame
from game_librarian.ranking import RankingWindow

TODAY = date(2026, 11, 19)
LATEST = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def game(appid, *, players=None, label="Very Positive", released=date(2026, 3, 1), positive=900, total=1_000,
         upcoming=False) -> CatalogGame:
    return CatalogGame(appid=appid, sort_name=f"game {appid:03d}", labels=(), awards=(), positive=positive,
                       total=total, released_on=released, upcoming=upcoming, in_window=True, rank=None,
                       review_label=label, players=players,
                       players_at=LATEST.isoformat() if players is not None else None)


def snapshots(*values, **extra) -> list[dict]:
    """Snapshot rows (oldest first) from (days before LATEST, value) pairs."""
    return [{"captured_at": (LATEST - timedelta(days=days)).isoformat(), **{key: value}, **extra}
            for days, value in values for key in ("players",)]


def test_popular_now_lists_games_with_players_most_first():
    games = [game(1, players=50), game(2, players=5_000), game(3, players=None), game(4, players=0), game(5, players=50)]
    assert [g.appid for g in trending.popular_now(games)] == [2, 1, 5]  # no count and zero are left out; ties A–Z


def test_well_received_sections_use_exact_dates_in_the_window_and_steams_labels():
    games = [
        game(1, positive=990, total=1_000),
        game(2, positive=400, total=400),  # 100% of too few reviews: after the games with 500 or more
        game(3, label="Mostly Positive"),  # not strong reception
        game(4, label=None),  # no usable review summary
        game(5, released=date(2025, 12, 31)),  # last year
        game(6, released=date(2026, 11, 20)),  # after today
        game(7, upcoming=True),
        game(8, released=None),  # no exact release date
        game(9, label="Overwhelmingly Positive", positive=950, total=1_000),
    ]
    start, end = trending.this_year(TODAY)
    assert (start, end) == (date(2026, 1, 1), TODAY)
    assert [g.appid for g in trending.well_received_released_between(games, start, end)] == [1, 9, 2]
    ranked = trending.ranking_window(RankingWindow.for_website(TODAY))
    assert ranked == (date(2021, 1, 1), TODAY)
    assert 5 in [g.appid for g in trending.well_received_released_between(games, *ranked)]


def test_a_player_change_needs_a_baseline_7_to_14_days_before_the_newest_snapshot():
    history = {
        1: snapshots((10, 900), (8, 1_000), (0, 1_500)),  # the newest eligible baseline (8 days) is used
        2: snapshots((6, 100), (0, 900)),  # 6 days apart: too close
        3: snapshots((15, 100), (0, 900)),  # 15 days apart: too far
        4: snapshots((7, 800), (0, 500)),  # fewer players: not an increase
        5: snapshots((7, 10), (0, 40)) + [{"captured_at": LATEST.isoformat(), "players": None}],  # no count: skipped
        6: snapshots((14, 0), (0, 5_000)),  # exactly 14 days and a zero baseline still count
    }
    changes = trending.player_changes(history)
    assert [(c.appid, c.before, c.after, c.difference) for c in changes] == [(6, 0, 5_000, 5_000), (1, 1_000, 1_500, 500),
                                                                           (5, 10, 40, 30)]
    assert changes[1].before_at == LATEST - timedelta(days=8) and changes[1].percent == 50
    assert changes[0].percent is None  # no percentage of zero


def test_review_changes_count_new_reviews_and_keep_the_positive_part_only_when_consistent():
    history = {
        1: [{"captured_at": (LATEST - timedelta(days=7)).isoformat(), "total": 1_000, "positive": 900},
            {"captured_at": LATEST.isoformat(), "total": 1_300, "positive": 1_150}],
        2: [{"captured_at": (LATEST - timedelta(days=9)).isoformat(), "total": 500, "positive": 480},
            {"captured_at": LATEST.isoformat(), "total": 520, "positive": 470}],  # positives fell: not shown
        3: [{"captured_at": LATEST.isoformat(), "total": 70, "positive": 60}],  # one snapshot: no change
    }
    changes = trending.review_changes(history)
    assert [(c.appid, c.difference, c.positive_added) for c in changes] == [(1, 300, 250), (2, 20, None)]
    assert trending.review_changes({}) == []


def test_history_status_says_when_a_change_can_first_exist():
    empty = trending.history_status({"games": 0, "first": None, "last": None, "days": 0})
    assert empty["first"] is None and not empty["has_window"] and empty["changes_possible_from"] is None
    young = trending.history_status({"games": 3, "first": "2026-11-16T02:00:00+00:00",
                                     "last": "2026-11-19T06:00:00+00:00", "days": 2})
    assert not young["has_window"]
    assert young["changes_possible_from"] == datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)
    old = trending.history_status({"games": 3, "first": "2026-11-01T00:00:00+00:00", "last": LATEST.isoformat(),
                                   "days": 12})
    assert old["has_window"]
