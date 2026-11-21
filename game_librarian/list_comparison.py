"""What a player would miss by browsing only Steam's most-wishlisted list (pure): the upcoming games in IGDB's Want to
Play capture, in Steam's most-wishlisted capture, or in both. The main analysis of Dashboard → Trending games →
Upcoming releases (version 0.16).

The captures are the newest saved snapshots of the two tracked upcoming lists (web._trending_lists): each refresh
saves the first upcoming.TRACKED_PER_SIGNAL upcoming games of IGDB PopScore type 2 ("Want to Play", every platform)
and type 10 (Steam's most-wishlisted upcoming games), and the page applies the upcoming rule (upcoming.list_kind)
again on the day it is served, so both lists follow the same definition, the one the release radar uses.

Identity. Entries are matched only by verified identifiers: the IGDB id, the game key (identity.py: a catalog Steam
app id or igdb-<id>) and a verified Steam app (upcoming.judge_steam_match; a catalog game is a Steam app by
definition). Entries that share one are one game, within one list (a duplicate counts once, at its best place) or
across both. Names are never compared. When IGDB lists a Steam app for a game without a verified match, and another
game of either list has that Steam app too, the two may be one game or two: both are "unresolved", listed apart and
left out of the three groups, so one possible game can never count once as "Seen only on IGDB" and again as "Seen
only on Steam".

Groups. Every resolved game is in exactly one: seen on both lists, seen only on IGDB, or seen only on Steam, so the
three counts add up to the distinct games of the two captures, the comparison's one denominator. A game seen only on
IGDB either has a verified Steam listing or an unknown Steam availability. Being absent from Steam's capture means only
that the game was not among the places that capture holds; it never means the game is not on Steam, and no game is
called console-only: IGDB lists the platforms a game is announced for, which does not prove it will never come to PC.

The comparison needs both captures, saved by the same refresh, each with at least one upcoming game. Otherwise it
reports the coverage it has and withholds the groups ("withheld").
"""

from __future__ import annotations

from .upcoming import STEAM_WISHLISTS, WANT_TO_PLAY

IGDB_ONLY, BOTH, STEAM_ONLY = "igdb_only", "both", "steam_only"
GROUP_LABELS = {BOTH: "Seen on both lists", IGDB_ONLY: "Seen only on IGDB", STEAM_ONLY: "Seen only on Steam"}
# Left to right in the page's bar, like the two circles of a Venn diagram: IGDB's capture covers the first two blocks,
# Steam's the last two.
BAR_ORDER = (IGDB_ONLY, BOTH, STEAM_ONLY)
SOURCES = (WANT_TO_PLAY, STEAM_WISHLISTS)  # the two captures compared, IGDB's first
EARLY_ACCESS = "early access"  # the IGDB release status of a game already sold before its full release


def _strong_steam_apps(item: dict) -> set[int]:
    """The Steam apps an entry verifiably is: its verified Steam match, and its catalog app (a digit key)."""
    apps = {item["steam_appid"]} if item.get("steam_verified") and item.get("steam_appid") else set()
    if str(item["key"]).isdigit():
        apps.add(int(item["key"]))
    return apps


def _identifiers(item: dict) -> set[tuple]:
    """The verified identifiers of one list entry: its game key, its IGDB id and the Steam apps it verifiably is."""
    found = {("key", str(item["key"]))}
    if item.get("igdb_id"):
        found.add(("igdb", item["igdb_id"]))
    found.update(("steam", appid) for appid in _strong_steam_apps(item))
    return found


def _early_access(item: dict) -> dict | None:
    """The started early access IGDB lists for an entry ({"status", "label", "platforms"}), if any."""
    availability = item.get("availability")
    return next((group for group in (availability.early if availability else ()) if group["status"] == EARLY_ACCESS),
                None)


def distinct_games(upcoming_lists: list[dict]) -> list[dict]:
    """The distinct games of the tracked upcoming lists, each once, in the order they first appear.

    `upcoming_lists` are web._trending_lists' upcoming lists: each has its "signal" and, in "tracked", its upcoming
    games as web._igdb_item builds them, best place first. Entries that share a verified identifier are merged into one
    game (module docstring), which keeps its first entry's fields and adds:
    - "lists" and "ranks": the lists it is in, and its place in each as the Trending page numbers it (1 = the list's
      first upcoming game), its best place when a list holds it twice;
    - "places": that place by signal key; "positions": its position in the source's own ranking by signal key (the
      raw IGDB order, which also counts the games that are out); "duplicates": the other places of a list holding it
      twice, by list title;
    - "igdb_ids", "steam_listed" (a verified Steam listing), "early_access" (a started early access, or None) and
      "unresolved": None, or why it may be the same game as others ({"steam_app", "with": their names}).
    """
    entries = [(data, place, item) for data in upcoming_lists
               for place, item in enumerate(data.get("tracked", ()), start=1)]
    parent = list(range(len(entries)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    first_with: dict[tuple, int] = {}  # each identifier's first entry; a later entry with it joins that one's game
    for index, (_, _, item) in enumerate(entries):
        for identifier in _identifiers(item):
            if identifier in first_with:
                parent[root(index)] = root(first_with[identifier])
            else:
                first_with[identifier] = index
    members: dict[int, list[int]] = {}
    for index in range(len(entries)):
        members.setdefault(root(index), []).append(index)

    games = []
    for indexes in sorted(members.values(), key=min):
        items = [entries[index][2] for index in indexes]
        game = {**items[0], "lists": [], "ranks": [], "places": {}, "positions": {}, "duplicates": {},
                "igdb_ids": sorted({item["igdb_id"] for item in items if item.get("igdb_id")}),
                "steam_listed": any(_strong_steam_apps(item) for item in items),
                "early_access": next((found for found in map(_early_access, items) if found), None),
                "unresolved": None, "_items": items}
        for index in indexes:
            data, place, item = entries[index]
            signal = data["signal"]
            if signal.key in game["places"]:  # the same game twice in one list: it keeps its best place
                game["duplicates"].setdefault(signal.title, []).append(place)
                continue
            game["places"][signal.key] = place
            if item.get("position") is not None:
                game["positions"][signal.key] = item["position"]
            game["lists"].append(signal.title)
            game["ranks"].append({"list": signal.title, "place": place})
        games.append(game)
    _mark_unresolved(games)
    for game in games:
        del game["_items"]
    return games


def _mark_unresolved(games: list[dict]) -> None:
    """Flag the games that may be one game: a Steam app IGDB lists without a verified match for one of them is also a
    Steam app of another (verified or not). Two verified apps never meet here: distinct_games merged those games."""
    claims: dict[int, list[int]] = {}  # Steam app id -> the games (by index) that have it, verified or not
    for number, game in enumerate(games):
        apps = set()
        for item in game["_items"]:
            apps |= _strong_steam_apps(item)
            if not item.get("steam_verified"):
                apps.update(appid for appid in item.get("steam_candidates") or () if isinstance(appid, int))
        for appid in apps:
            claims.setdefault(appid, []).append(number)
    for appid, numbers in sorted(claims.items()):
        if len(numbers) < 2:
            continue  # only one game has this Steam app: nothing to confuse it with
        for number in numbers:
            games[number]["unresolved"] = {"steam_app": appid,
                                           "with": [games[other]["name"] for other in numbers if other != number]}


def _capture(data: dict | None, signal) -> dict:
    """One list's capture: when it was saved and calculated, and its depth (how many upcoming games it holds, down to
    which place of the source's own ranking, out of how many places a refresh reads)."""
    tracked = (data or {}).get("tracked") or []
    positions = [item["position"] for item in tracked if item.get("position") is not None]
    return {
        "signal": signal,
        "captured_at": (data or {}).get("captured_at"),
        "calculated_at": (data or {}).get("calculated_at"),
        "games": len(tracked),
        "deepest": max(positions) if positions else None,
        "scanned": signal.depth,
        "unread": (data or {}).get("unread", 0),
    }


def _withheld(captures: list[dict]) -> str | None:
    """Why the groups cannot be compared ("missing", "different_refreshes" or "empty"), or None when they can."""
    if any(not capture["captured_at"] for capture in captures):
        return "missing"
    if len({capture["captured_at"] for capture in captures}) > 1:
        return "different_refreshes"
    if any(not capture["games"] for capture in captures):
        return "empty"
    return None


def _platforms(availability) -> list[str]:
    """Every platform IGDB lists for an upcoming game: announced, in early access or without a date, A–Z."""
    if not availability:
        return []
    names = {name for group in (*availability.available, *availability.announced) for name in group.platforms}
    names.update(name for item in availability.early for name in item["platforms"])
    names.update(availability.undated)
    return sorted(names)


def compare_lists(upcoming_lists: list[dict], games: list[dict]) -> dict:
    """The three groups of the two captures and what the page needs around them.

    `upcoming_lists` are web._trending_lists' upcoming lists and `games` their distinct_games (as the release radar
    returns them, so the radar and the comparison share one list of games). "captures" holds IGDB's and Steam's
    coverage (_capture) whether or not they can be compared; "withheld" says why they cannot. When they can:
    "groups" maps each group key to its games, "counts" to their numbers, "total" is the distinct comparable union
    (the three counts' sum), "igdb_listed" / "igdb_unknown" split the games seen only on IGDB by a verified Steam
    listing, "early_access" are the listed ones already in early access, and "unresolved" the games left out of the
    groups. "bar" and "zeros" lay out the page's blocks (block_layout).
    """
    by_signal = {data["signal"].key: data for data in upcoming_lists}
    captures = [_capture(by_signal.get(signal.key), signal) for signal in SOURCES]
    for game in games:
        game["platform_names"] = _platforms(game.get("availability"))
    found = {"captures": captures, "withheld": _withheld(captures), "games": games}
    if found["withheld"]:
        return found
    groups: dict[str, list[dict]] = {key: [] for key in BAR_ORDER}
    unresolved = []
    for game in games:
        if game["unresolved"]:
            unresolved.append(game)
            continue
        in_igdb, in_steam = WANT_TO_PLAY.key in game["places"], STEAM_WISHLISTS.key in game["places"]
        groups[BOTH if in_igdb and in_steam else IGDB_ONLY if in_igdb else STEAM_ONLY].append(game)
    counts = {key: len(members) for key, members in groups.items()}
    listed = [game for game in groups[IGDB_ONLY] if game["steam_listed"]]
    found.update(
        groups=groups, counts=counts, total=sum(counts.values()), unresolved=unresolved,
        igdb_listed=listed, igdb_unknown=[game for game in groups[IGDB_ONLY] if not game["steam_listed"]],
        early_access=[game for game in listed if game["early_access"]],
        steam_early_access=[game for game in groups[BOTH] + groups[STEAM_ONLY] if game["early_access"]],
        duplicates=[game for game in games if game["duplicates"]],
        **block_layout(counts),
    )
    found["finding"] = finding(found)
    return found


def block_layout(counts: dict[str, int]) -> dict:
    """The page's three blocks: the groups with games in BAR_ORDER ("bar", each block's width following its count),
    and the empty groups ("zeros"), shown with their zero but given no width. "spans" are the grid lines (1-based,
    end excluded) of the bar's columns that each capture covers: IGDB's the blocks of the games it holds (only on
    IGDB, and on both lists), Steam's likewise."""
    bar = [{"key": key, "label": GROUP_LABELS[key], "count": counts[key]} for key in BAR_ORDER if counts[key]]
    columns = {block["key"]: number for number, block in enumerate(bar, start=1)}

    def span(keys: tuple[str, str]) -> tuple[int, int] | None:
        covered = [columns[key] for key in keys if key in columns]
        return (min(covered), max(covered) + 1) if covered else None

    return {
        "bar": bar,
        "zeros": [{"key": key, "label": GROUP_LABELS[key], "count": 0} for key in BAR_ORDER if not counts[key]],
        "spans": {WANT_TO_PLAY.key: span((IGDB_ONLY, BOTH)), STEAM_WISHLISTS.key: span((BOTH, STEAM_ONLY))},
    }


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def finding(found: dict) -> str:
    """The comparison's one plain-language finding, from its counts: "Of 44 distinct upcoming games in these captures,
    19 appeared only on IGDB. Of those, 14 had a verified Steam listing. 9 of the 14 were already in early access."
    The last sentence appears only when such a game exists."""
    total, only, listed = found["total"], found["counts"][IGDB_ONLY], len(found["igdb_listed"])
    early = len(found["early_access"])
    text = f"Of {_count(total, 'distinct upcoming game', 'distinct upcoming games')} in these captures, "
    if not only:
        return text + "none appeared only on IGDB."
    text += f"{only} appeared only on IGDB."
    if only == 1:
        text += " It had a verified Steam listing." if listed else " Its Steam availability is unknown."
    elif not listed:
        text += " None of them had a verified Steam listing."
    else:
        text += f" Of those, {listed} had a verified Steam listing."
    if early:
        text += (" It was already in early access." if listed == 1 else
                 f" {early} of the {listed} {'was' if early == 1 else 'were'} already in early access.")
    return text
