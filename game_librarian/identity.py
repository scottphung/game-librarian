"""Platform-independent game identity: the key that page links, caches and the wishlist use.

A catalog game keeps its Steam app id as its key ("271590"), exactly as before, so existing links and
wishlist entries keep working. A game that has no Steam app in the catalog (an upcoming or console-only
game from IGDB) gets the key "igdb-<IGDB id>" ("igdb-52189"). An IGDB key keeps working for good: when
that game later gets a verified Steam app that is in the catalog, the key opens the catalog game, and
the catalog game's wishlist button also recognizes the IGDB key, so the game is never saved twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A Steam app id ("271590") or an IGDB key ("igdb-52189"); ids are positive and at most 10 digits.
GAME_KEY = re.compile(r"(?P<steam>[1-9]\d{0,9})|igdb-(?P<igdb>[1-9]\d{0,9})")


@dataclass(frozen=True)
class GameKey:
    """A parsed key: `kind` is "steam" or "igdb", `number` the Steam app id or the IGDB game id."""

    kind: str
    number: int

    def __str__(self) -> str:
        return str(self.number) if self.kind == "steam" else igdb_key(self.number)


def igdb_key(igdb_id: int) -> str:
    """The key of a game known by its IGDB id only."""
    return f"igdb-{int(igdb_id)}"


def parse_key(value: str) -> GameKey | None:
    """The key in `value`, or None when it is not a valid key."""
    match = GAME_KEY.fullmatch(value or "")
    if not match:
        return None
    if match["steam"]:
        return GameKey("steam", int(match["steam"]))
    return GameKey("igdb", int(match["igdb"]))
