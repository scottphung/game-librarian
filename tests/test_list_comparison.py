"""What a player would miss by browsing only Steam's most-wishlisted list: IGDB's Want to Play capture against Steam's
most-wishlisted capture, the main analysis of Trending games → Upcoming releases (version 0.16, list_comparison.py).

Games are matched only by verified identifiers (IGDB id, game key, verified Steam app), each distinct game is in
exactly one of three groups, so the counts add up to the distinct comparable union, and a possible duplicate is
listed apart instead of counting once on each side. The comparison needs both captures from one refresh.

Uses the fourteen synced catalog games of test_web.py (app ids 101-114). All names and numbers are made up.
"""

import re
from datetime import date, timedelta

from game_librarian import list_comparison, store, twitch, upcoming
from game_librarian.config import PACKAGE_DIR
from game_librarian.upcoming import STEAM_WISHLISTS, WANT_TO_PLAY, SignalEntry, SteamMatch, availability
from tests.test_dashboard_trending import chart, dashboard_client, details_keys, item, methodology
from tests.test_trending_cross_platform import dates_of
from tests.test_upcoming import entry, igdb_game
from tests.test_web import games, text_of  # noqa: F401
from tests.test_web_upcoming import NOW, TODAY

APP_JS = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")
SAVED = "2026-11-09T12:00:00+00:00"


def listed(key, name, *, igdb_id=None, steam=None, candidates=None, position=None, early=False):
    """A tracked upcoming entry as web._igdb_item builds it, with only what the comparison reads: `steam` is its
    verified Steam app, `candidates` the Steam apps IGDB lists for it (by default the verified one)."""
    dates = [entry("PC", day=date(2025, 1, 1), status=3), entry("PC", fmt=7)] if early else [entry("PC", fmt=7)]
    return item(key, name, availability(dates_of(*dates), ["PC"], TODAY), steam_verified=steam is not None,
                igdb_id=igdb_id, steam_appid=steam, position=position,
                steam_candidates=list(candidates) if candidates is not None else ([steam] if steam else []))


def capture(signal, *items, captured=SAVED):
    return {"signal": signal, "tracked": list(items), "captured_at": captured, "calculated_at": None}


def names(games: list[dict]) -> list[str]:
    return [game["name"] for game in games]


def compared(wanted: list[dict], wishlisted: list[dict], **steam) -> dict:
    lists = [capture(WANT_TO_PLAY, *wanted), capture(STEAM_WISHLISTS, *wishlisted, **steam)]
    return list_comparison.compare_lists(lists, list_comparison.distinct_games(lists))


# Both captures of one refresh: a game in both, a game twice on Steam's list under two IGDB ids of one verified Steam
# app, the same game under two IGDB ids across the lists (one verified Steam app), and a namesake that is another game.
WANTED = [listed("igdb-10", "Shared Saga", igdb_id=10, steam=900, position=3),
          listed("igdb-11", "PC Wanted", igdb_id=11, steam=901, position=7, early=True),
          listed("igdb-12", "Console Wanted", igdb_id=12, position=12),
          listed("igdb-13", "Twin Record", igdb_id=13, steam=902, position=20),
          listed("igdb-15", "Namesake", igdb_id=15, position=25)]
WISHLISTED = [listed("igdb-10", "Shared Saga", igdb_id=10, steam=900, position=1),
              listed("igdb-23", "Twin Record: Steam Edition", igdb_id=23, steam=902, position=2),
              listed("igdb-24", "Steam Only", igdb_id=24, steam=903, position=3),
              listed("igdb-25", "Steam Only (second record)", igdb_id=25, steam=903, position=5),
              listed("igdb-27", "Namesake", igdb_id=27, steam=905, position=6)]


# ---------------------------------------------------------------------- matching and groups (pure)


def test_the_three_groups_reconcile_to_the_distinct_comparable_union():
    found = compared(WANTED, WISHLISTED)
    assert found["withheld"] is None
    assert {key: names(games) for key, games in found["groups"].items()} == {
        "igdb_only": ["PC Wanted", "Console Wanted", "Namesake"],
        "both": ["Shared Saga", "Twin Record"],  # Twin Record is matched by its verified Steam app, not by name
        "steam_only": ["Steam Only", "Namesake"]}  # a namesake on Steam is another game: names never match
    counts = found["counts"]
    assert counts == {"igdb_only": 3, "both": 2, "steam_only": 2} and found["total"] == 7
    # Each capture's distinct games, minus the ones in both, give the same union: 5 + 4 - 2 = 7.
    in_igdb, in_steam = counts["igdb_only"] + counts["both"], counts["both"] + counts["steam_only"]
    assert (in_igdb, in_steam, in_igdb + in_steam - counts["both"]) == (5, 4, found["total"])
    every = [game for games in found["groups"].values() for game in games]
    assert len(every) == len({id(game) for game in every}) == found["total"]  # no game is in two groups


def test_a_game_twice_in_one_capture_counts_once_at_its_best_place():
    games = list_comparison.distinct_games([capture(WANT_TO_PLAY, *WANTED), capture(STEAM_WISHLISTS, *WISHLISTED)])
    steam_only = next(game for game in games if game["name"] == "Steam Only")
    assert steam_only["places"] == {"steam_wishlists": 3} and steam_only["igdb_ids"] == [24, 25]
    assert steam_only["duplicates"] == {"Most wishlisted on Steam": [4]}
    assert steam_only["ranks"] == [{"list": "Most wishlisted on Steam", "place": 3}]
    twin = next(game for game in games if game["name"] == "Twin Record")
    assert twin["places"] == {"want_to_play": 4, "steam_wishlists": 2} and twin["positions"] == {
        "want_to_play": 20, "steam_wishlists": 2}  # its rank among upcoming games, and its place in each ranking
    assert twin["lists"] == ["Most wanted on IGDB", "Most wishlisted on Steam"]
    # A catalog game's key is a Steam app id: an IGDB record linked to that app is the same game.
    catalog = list_comparison.distinct_games([capture(WANT_TO_PLAY, listed("114", "Game 14", igdb_id=40)),
                                              capture(STEAM_WISHLISTS, listed("igdb-41", "Game 14 Again", igdb_id=41,
                                                                              steam=114))])
    assert names(catalog) == ["Game 14"] and catalog[0]["steam_listed"] and catalog[0]["igdb_ids"] == [40, 41]


def test_a_possible_duplicate_is_unresolved_and_never_inflates_a_source_only_group():
    maybe = listed("igdb-14", "Maybe Same", igdb_id=14, candidates=[904], position=30)  # IGDB's app, unverified
    deluxe = listed("igdb-26", "Maybe Same Deluxe", igdb_id=26, steam=904, position=8)
    found = compared(WANTED + [maybe], WISHLISTED + [deluxe])
    assert names(found["unresolved"]) == ["Maybe Same", "Maybe Same Deluxe"]
    assert [game["unresolved"] for game in found["unresolved"]] == [
        {"steam_app": 904, "with": ["Maybe Same Deluxe"]}, {"steam_app": 904, "with": ["Maybe Same"]}]
    assert found["counts"] == {"igdb_only": 3, "both": 2, "steam_only": 2} and found["total"] == 7  # as without them
    # Two unverified games of one capture sharing a Steam app are unresolved too; a lone unverified one is not.
    pair = list_comparison.distinct_games([capture(WANT_TO_PLAY, listed("igdb-1", "A", igdb_id=1, candidates=[7]),
                                                   listed("igdb-2", "B", igdb_id=2, candidates=[7, 8]),
                                                   listed("igdb-3", "C", igdb_id=3, candidates=[9]))])
    assert [(game["name"], bool(game["unresolved"])) for game in pair] == [("A", True), ("B", True), ("C", False)]


def test_a_verified_steam_listing_outside_the_steam_capture_is_seen_only_on_igdb_and_split_from_unknown():
    found = compared(WANTED, WISHLISTED)
    assert names(found["igdb_listed"]) == ["PC Wanted"]  # on Steam, but not among the places Steam's capture holds
    assert names(found["igdb_unknown"]) == ["Console Wanted", "Namesake"]  # no verified Steam match: unknown
    assert names(found["early_access"]) == ["PC Wanted"] and found["early_access"][0]["early_access"]["label"] == \
        "1 Jan 2025"
    assert found["steam_early_access"] == []
    assert found["finding"] == ("Of 7 distinct upcoming games in these captures, 3 appeared only on IGDB. Of those, 1 "
                                "had a verified Steam listing. It was already in early access.")
    steam = found["captures"][1]
    assert (steam["games"], steam["deepest"], steam["scanned"]) == (5, 6, 200)  # five entries saved, down to #6


def test_a_missing_or_mismatched_capture_withholds_the_comparison_but_keeps_its_coverage():
    wanted = capture(WANT_TO_PLAY, *WANTED)
    missing = list_comparison.compare_lists([wanted], list_comparison.distinct_games([wanted]))
    assert missing["withheld"] == "missing" and "groups" not in missing and "finding" not in missing
    assert [(found["signal"].key, found["captured_at"], found["games"], found["deepest"])
            for found in missing["captures"]] == [("want_to_play", SAVED, 5, 25), ("steam_wishlists", None, 0, None)]
    assert compared(WANTED, WISHLISTED, captured="2026-11-09T18:00:00+00:00")["withheld"] == "different_refreshes"
    assert compared(WANTED, [])["withheld"] == "empty"  # saved, but no game of it is still upcoming


def test_an_empty_group_keeps_its_zero_without_a_width():
    layout = list_comparison.block_layout({"igdb_only": 19, "both": 0, "steam_only": 3})
    assert [(block["key"], block["count"]) for block in layout["bar"]] == [("igdb_only", 19), ("steam_only", 3)]
    assert layout["zeros"] == [{"key": "both", "label": "Seen on both lists", "count": 0}]
    assert layout["spans"] == {"want_to_play": (1, 2), "steam_wishlists": (2, 3)}  # the brackets do not overlap
    full = list_comparison.block_layout({"igdb_only": 19, "both": 6, "steam_only": 19})
    assert full["spans"] == {"want_to_play": (1, 3), "steam_wishlists": (2, 4)} and full["zeros"] == []


def test_the_finding_is_one_plain_sentence_for_any_counts():
    def text(only, listed_games, early, total=10):
        listed_list = [{}] * listed_games
        return list_comparison.finding({"total": total, "counts": {"igdb_only": only}, "igdb_listed": listed_list,
                                        "early_access": [{}] * early})
    assert text(0, 0, 0) == "Of 10 distinct upcoming games in these captures, none appeared only on IGDB."
    assert text(1, 0, 0, total=1) == ("Of 1 distinct upcoming game in these captures, 1 appeared only on IGDB. Its Steam "
                                      "availability is unknown.")
    assert text(4, 0, 0).endswith("4 appeared only on IGDB. None of them had a verified Steam listing.")
    assert text(19, 14, 9).endswith("Of those, 14 had a verified Steam listing. 9 of the 14 were already in early access.")
    assert text(5, 2, 1).endswith("Of those, 2 had a verified Steam listing. 1 of the 2 was already in early access.")


# ---------------------------------------------------------------------- the page, from stored captures


def save_captures(settings, *, steam_at=None, steam=True, twitch_list=True):
    """One refresh's captures: Shared Saga in both lists (exact day, on the radar), Early Bird Online only on IGDB
    with a verified Steam app in the catalog (Game 13) and a started early access, Console Wanted only on IGDB without
    a Steam app, Twin Record in both under two IGDB ids of one verified Steam app, Steam Only Quest only on Steam,
    and Maybe Same / Maybe Same Deluxe, which IGDB links to one Steam app without a verified match for the first."""
    tba = entry("PC", fmt=7)
    records = [
        upcoming.record_from_igdb(igdb_game(501, "Shared Saga", entry("PC", day=date(2027, 2, 23)),
                                            entry("PlayStation 5", day=date(2027, 2, 23)), steam=[900],
                                            platforms=("PC", "PlayStation 5"))),
        upcoming.record_from_igdb(igdb_game(502, "Early Bird Online", entry("PC", day=date(2025, 1, 1), status=3), tba,
                                            steam=[113])),
        upcoming.record_from_igdb(igdb_game(503, "Console Wanted", entry("PlayStation 5", fmt=7),
                                            platforms=("PlayStation 5",))),
        upcoming.record_from_igdb(igdb_game(504, "Twin Record", entry("PC", fmt=2, y=2027), steam=[902])),
        upcoming.record_from_igdb(igdb_game(515, "Twin Record Steam", entry("PC", fmt=2, y=2027), steam=[902])),
        upcoming.record_from_igdb(igdb_game(506, "Steam Only Quest", entry("PC", day=date(2026, 12, 1)), steam=[903])),
        upcoming.record_from_igdb(igdb_game(507, "Maybe Same", tba, steam=[904])),
        upcoming.record_from_igdb(igdb_game(517, "Maybe Same Deluxe", tba, steam=[904])),
    ]

    def verified(appid):
        return SteamMatch("verified", appid, f"IGDB lists Steam app {appid}, and Steam's name for it is the same.")
    matches = {501: verified(900), 502: verified(113), 503: SteamMatch("none", None, "IGDB lists no Steam app."),
               504: verified(902), 515: verified(902), 506: verified(903), 517: verified(904),
               507: SteamMatch("needs_review", None, "Steam calls app 904 another name.")}
    saved = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    calculated = NOW - timedelta(hours=10)
    conn = store.connect(settings.database)
    try:
        store.save_igdb_games(conn, records, matches, [], now=saved)
        store.save_popularity_snapshot(conn, "want_to_play", [
            SignalEntry(igdb_id, 0.004 - place / 1e4, place, calculated)
            for igdb_id, place in ((501, 3), (502, 7), (503, 12), (504, 20), (507, 31))], now=saved)
        if steam:
            store.save_popularity_snapshot(conn, "steam_wishlists", [
                SignalEntry(igdb_id, 0.0005 - place / 1e5, place, calculated)
                for igdb_id, place in ((501, 1), (515, 2), (506, 4), (517, 5))], now=steam_at or saved)
        if twitch_list:
            store.save_twitch_top_games(conn, [twitch.TopCategory(1, "800", "Just Chatting", None),
                                               twitch.TopCategory(9, "801", "Shared Saga", 501)], now=saved)
    finally:
        conn.close()


def upcoming_page(settings, **params) -> str:
    return dashboard_client(settings).get("/dashboard", params={"report": "upcoming", **params}).text


def panel(html: str, key: str) -> str:
    match = re.search(r'<div class="compare-group" id="group-' + key + r'" data-compare-panel="' + key + r'"([^>]*)>'
                      r'(.*?)</div>\s*(?=<div class="compare-group"|<div class="compare-unresolved"|<p class="chart-meta")',
                      html, re.S)
    return match.group(1), match.group(2)


def test_the_comparison_sits_beside_the_radar_with_its_finding_coverage_and_three_blocks(games, settings):
    save_captures(settings)
    html = upcoming_page(settings)
    canvas = html[html.index('<div class="report-canvas is-upcoming" data-upcoming>'):]
    assert canvas.index('id="chart-radar"') < canvas.index('id="chart-compare"')  # beside it, below it on a phone
    assert "What would a player miss by browsing only Steam’s most-wishlisted list?" in text_of(html)
    card = chart(html, "chart-compare")
    words = text_of(card)
    assert words.startswith(
        "IGDB’s Want to Play and Steam’s most wishlisted, compared All platforms Of 5 distinct upcoming games in these "
        "captures, 2 appeared only on IGDB. Of those, 1 had a verified Steam listing. It was already in early access.")
    # Each capture's observation time and real depth stay in view.
    assert ("IGDB Want to Play all platforms 5 upcoming games, down to place #31 of its ranking · saved 9 Nov 2026, "
            "12:00 UTC (IGDB calculated it 9 Nov 2026, 03:00 UTC)") in words
    assert ("Steam most wishlisted Steam only 4 upcoming games, down to place #5 of its ranking · saved 9 Nov 2026, "
            "12:00 UTC (IGDB calculated it 9 Nov 2026, 03:00 UTC)") in words
    # One denominator, reconciled in words, and three blocks as wide as their counts with a visible count each.
    assert ("5 distinct upcoming games = 4 in IGDB’s capture + 3 in Steam’s − 2 in both, not counting the 2 games "
            "whose identity is unresolved (below).") in words
    assert 'style="grid-template-columns: minmax(2rem, 2fr) minmax(2rem, 2fr) minmax(2rem, 1fr)"' in card
    blocks = re.findall(r'<a class="compare-block is-(\w+)" href="([^"]+)" data-compare-group="\w+" style="grid-column: '
                        r'(\d)"( aria-current="true")?><span class="compare-count">(\d+)</span>'
                        r'<span class="visually-hidden">([^<]+)</span>', card)
    assert [(key, href, column, bool(current), count, hidden.strip()) for key, href, column, current, count, hidden
            in blocks] == [
        ("igdb_only", "/dashboard?report=upcoming&amp;group=igdb_only#compare", "1", True, "2",
         "games: Seen only on IGDB"),
        ("both", "/dashboard?report=upcoming&amp;group=both#compare", "2", False, "2", "games: Seen on both lists"),
        ("steam_only", "/dashboard?report=upcoming&amp;group=steam_only#compare", "3", False, "1",
         "game: Seen only on Steam")]
    assert 'class="compare-span is-igdb" style="grid-column: 1 / 3"' in card  # IGDB's capture: its two blocks
    assert 'class="compare-span is-steam" style="grid-column: 2 / 4"' in card
    assert "Seen only on IGDB Seen on both lists Seen only on Steam" in words  # the legend, in block order
    assert 'class="compare-zeros"' not in card
    # The games seen only on IGDB open by default: names, platforms, ranks, the Steam split and early access.
    attributes, igdb_only = panel(card, "igdb_only")
    assert "hidden" not in attributes and "hidden" in panel(card, "both")[0] and "hidden" in panel(card, "steam_only")[0]
    assert text_of(igdb_only) == (
        "Seen only on IGDB · 2 games In IGDB’s capture, not among the 4 upcoming games of Steam’s (places #1–#5). That "
        "says nothing about Steam’s places below #5. Verified Steam listing · 1 (1 already in early access) Early Bird "
        "Online PC · IGDB #2 · Early access since 1 Jan 2025 Steam availability unknown · 1 No verified Steam match in "
        "IGDB’s data. Not called console-only: the platforms are what IGDB lists as announced. Console Wanted "
        "PlayStation 5 · IGDB #3")
    assert details_keys(igdb_only) == ["113", "igdb-503"]  # a verified catalog app opens the catalog game
    assert re.findall(r'<li class="cmp-row" data-game="([^"]+)">', igdb_only) == ["113", "igdb-503"]
    both = text_of(panel(card, "both")[1])
    assert "Shared Saga PC, PlayStation 5 · IGDB #1 · Steam #1" in both and "Twin Record PC · IGDB #4 · Steam #2" in both
    assert "Steam Only Quest PC · Steam #3" in text_of(panel(card, "steam_only")[1])
    unresolved = text_of(card[card.index('class="compare-unresolved"'):])
    assert ("Not counted · identity unresolved · 2 Maybe Same ( IGDB #5): IGDB links Steam app 904 to it and to Maybe "
            "Same Deluxe, without a verified match for every one, so they may be one game.") in unresolved  # text_of
    # leaves a space after "(" where a tag starts; the page itself reads "(IGDB #5)"
    assert "(<span class=\"cmp-rank\">IGDB #5</span>)" in card
    # The table holds every comparable game with its group, both ranks and places, and the reconciled total.
    table = text_of(re.search(r"<table.*?</table>", card, re.S).group(0))
    assert "Early Bird Online Seen only on IGDB #2 (7) — Verified Since 1 Jan 2025 PC" in table
    assert "Twin Record Seen on both lists #4 (20) #2 (2) Verified" in table
    assert table.endswith("Distinct comparable games 2 + 2 + 1 4 3 5 games")
    # Every comparable game is also on the radar's panel list, so selecting it in a group can show it there.
    for key in re.findall(r'data-game="([^"]+)"', card):
        assert f'<template id="radar-game-{key}">' in html, key


def test_a_group_opens_without_javascript_and_an_unknown_group_falls_back(games, settings):
    save_captures(settings)
    card = chart(upcoming_page(settings, group="steam_only"), "chart-compare")
    assert "hidden" not in panel(card, "steam_only")[0] and "hidden" in panel(card, "igdb_only")[0]
    assert 'data-compare-group="steam_only" style="grid-column: 3" aria-current="true"' in card
    assert "hidden" not in panel(chart(upcoming_page(settings, group="none"), "chart-compare"), "igdb_only")[0]


def test_twitch_is_context_in_the_selected_game_panel_only(games, settings):
    save_captures(settings)
    html = upcoming_page(settings)
    shared = re.search(r'<template id="radar-game-igdb-501">(.*?)</template>', html, re.S).group(1)
    assert "#9 on Twitch’s most-watched list · Watch on Twitch (opens in a new tab)" in text_of(shared)
    assert 'href="https://www.twitch.tv/directory/game/Shared%20Saga"' in shared  # a clear path to Twitch's data
    assert "Twitch" not in text_of(re.search(r'<template id="radar-game-igdb-503">(.*?)</template>', html, re.S).group(1))
    card = text_of(chart(html, "chart-compare"))
    assert "Twitch" not in card  # the comparison's denominator is the two upcoming lists only
    assert "Twitch’s places from its list of 9 Nov 2026, 12:00 UTC." in text_of(chart(html, "chart-radar"))
    assert 'id="chart-both-lists"' not in html and 'class="pair-' not in html  # the old overlap card is gone
    method = text_of(methodology(html))
    assert "Twitch: not part of the comparison." in method and "no Twitch history is kept" in method


def test_a_missing_capture_shows_the_coverage_it_has_and_withholds_the_groups(games, settings):
    save_captures(settings, steam=False)
    card = chart(upcoming_page(settings), "chart-compare")
    words = text_of(card)
    assert "IGDB Want to Play all platforms 5 upcoming games, down to place #31 of its ranking" in words
    assert "Steam most wishlisted Steam only not captured yet" in words
    assert ("Most wishlisted on Steam has not been saved yet, so the lists are not compared. Run python -m "
            "game_librarian upcoming.") in words
    assert 'class="compare-block' not in card and "appeared only on IGDB" not in words
    save_captures(settings, steam_at=(NOW - timedelta(minutes=20)).isoformat(timespec="seconds"))  # a later refresh
    later = text_of(chart(upcoming_page(settings), "chart-compare"))
    assert "The newest captures come from different refreshes, so they are not compared" in later


def test_the_comparison_methodology_states_population_matching_and_limits(games, settings):
    save_captures(settings)
    method = text_of(methodology(upcoming_page(settings)))
    for phrase in ("A refresh reads the first 1,000 places of IGDB PopScore type 2", "the first 200 of type 10",
                   "keeps the first 25 upcoming games of each", "names are never compared",
                   "both are listed apart as unresolved and left out of the three groups (2 now)",
                   "their counts add up to the distinct games of both captures, the comparison’s one denominator",
                   "No game is called console-only", "Now 1 of the 1 games seen only on IGDB with a Steam listing are "
                   "in early access, and 0 of the 3 games in Steam’s capture."):
        assert phrase in method, phrase


def test_the_upcoming_script_shares_one_selection_and_opens_groups_in_place():
    tracker = APP_JS[APP_JS.index("function trackSelection"):APP_JS.index('querySelectorAll("[data-upcoming]")')]
    for needed in ('event.pointerType === "mouse" || event.pointerType === "pen"', 'addEventListener("focusin"',
                   'addEventListener("pointerdown"', "before.selected !== key", "event.preventDefault()"):
        assert needed in tracker, needed
    block = APP_JS[APP_JS.index('querySelectorAll("[data-upcoming]")'):APP_JS.index('querySelectorAll("[data-chart-tips]")')]
    for needed in ('document.getElementById("radar-game-" + key)', "template.content.cloneNode(true)",
                   "point.hidden =", 'showWindow("all")', 'part.getAttribute("data-compare-panel") !== key',
                   'link.setAttribute("aria-current", "true")', 'urlWith("group", key)', "history.replaceState",
                   "trackSelection(scope, select"):
        assert needed in block, needed
    assert "innerHTML" not in block
