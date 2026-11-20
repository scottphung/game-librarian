"""Publisher names from Steam, grouped so that one publisher is one filter choice.

Steam's publisher field is free text that each game's partner types, so one publisher can
appear under several spellings in the catalog: "BANDAI NAMCO Entertainment" and "Bandai Namco
Entertainment Inc.", "Feral Interactive (Mac)" and "Feral interactive (Mac)", "SEGA" and
"SEGA (Japan)". Names are grouped conservatively, in two steps:

1. Automatic. Case, accents, punctuation and trademark symbols are ignored (normalize_for_search),
   and two trailing parts are removed: a qualifier in parentheses, which Steam pages use to say
   where a publisher publishes ("(Mac)", "(Japan)", "(Excluding Japan and Asia)"), and a company
   type ("Inc.", "Co., Ltd.", "LLC").
2. Curated. curated/publisher_aliases.csv joins names that belong to one publisher under another
   name, each with a source: for example "WB Games" -> "Warner Bros. Games".

Nothing else is merged. A parent company and its labels stay apart (Lucasfilm, LucasArts and
Disney are three publishers), because a shared owner does not make a shared publisher.
Steam's own spelling is still shown in the game details; grouping only affects the filter.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .prepare import normalize_for_search

# A trailing qualifier in parentheses: "Feral Interactive (Mac)" -> "Feral Interactive".
TRAILING_QUALIFIER = re.compile(r"\s*\([^()]*\)\s*$")
# A trailing company type, with its comma: "CAPCOM Co., Ltd." -> "CAPCOM Co." -> "CAPCOM".
# Removed repeatedly from the end only, so "Limited Run Games" or "Coffee Stain" are never cut.
COMPANY_SUFFIX = re.compile(
    r"[\s,]+(?:inc|incorporated|llc|ltd|limited|co|corp|corporation|gmbh|ag|ab|oy|plc|pty|s\.?a|s\.?r\.?l|k\.?k)\.?$",
    re.IGNORECASE,
)


def clean_name(name: str) -> str:
    """The publisher's name without a trailing qualifier or company type, keeping Steam's spelling.

    "FromSoftware, Inc. (Japan)" -> "FromSoftware"; "Wube Software LTD." -> "Wube Software".
    A name that would become empty is kept as it is.
    """
    cleaned = name.strip()
    previous = None
    while cleaned != previous:  # "Inc. (Japan)" needs two passes
        previous = cleaned
        cleaned = TRAILING_QUALIFIER.sub("", cleaned)
        cleaned = COMPANY_SUFFIX.sub("", cleaned).rstrip(" ,")
    return cleaned or name.strip()


def publisher_key(name: str) -> str:
    """The grouping key of a publisher name: "BANDAI NAMCO Entertainment Inc." -> "bandai namco entertainment"."""
    return normalize_for_search(clean_name(name))


@dataclass(frozen=True)
class Publisher:
    """One publisher choice: its key, the name shown for it, and how many catalog games list it."""

    key: str
    name: str
    games: int


class PublisherIndex:
    """Which publishers each catalog game has, after grouping, and the name shown for each publisher."""

    def __init__(self, keys_by_appid: dict[int, frozenset[str]], names: dict[str, str],
                 alias_keys: dict[str, str] | None = None) -> None:
        self._keys_by_appid = keys_by_appid
        self._names = names
        self._alias_keys = alias_keys or {}  # the key of an aliased spelling -> the key of its publisher
        counts = Counter(key for keys in keys_by_appid.values() for key in keys)
        self._publishers = {key: Publisher(key, names[key], counts[key]) for key in names}

    def keys_for(self, appid: int) -> frozenset[str]:
        """The publisher keys of one game (empty when Steam lists no publisher)."""
        return self._keys_by_appid.get(appid, frozenset())

    def find(self, text: str) -> Publisher | None:
        """The publisher a URL value or any Steam spelling refers to, or None when it is not in the catalog."""
        if not text.strip():
            return None
        key = publisher_key(text)
        return self._publishers.get(self._alias_keys.get(key, key))

    def name_of(self, key: str) -> str:
        return self._names[key]

    def choices(self) -> list[Publisher]:
        """Every publisher in the catalog, A–Z by shown name."""
        return sorted(self._publishers.values(), key=lambda publisher: (publisher.name.casefold(), publisher.key))


def build_index(names_by_appid: Mapping[int, Iterable[str]], aliases: Mapping[str, str] | None = None) -> PublisherIndex:
    """Group the Steam publisher names of the catalog games.

    `names_by_appid` holds each game's publisher names as Steam lists them. `aliases` maps a
    publisher key to the name of the publisher it belongs to (from curated/publisher_aliases.csv).
    A group is shown under its alias name, otherwise under its most common cleaned spelling
    (ties: alphabetical), so "Bandai Namco Entertainment" wins over "BANDAI NAMCO Entertainment".
    """
    aliases = aliases or {}
    spellings: dict[str, Counter] = {}
    keys_by_appid: dict[int, frozenset[str]] = {}
    for appid, names in names_by_appid.items():
        keys = set()
        for name in names:
            if not name.strip():
                continue
            own_key = publisher_key(name)
            alias = aliases.get(own_key)
            key = publisher_key(alias) if alias else own_key
            if key:
                keys.add(key)
                spellings.setdefault(key, Counter())[clean_name(name)] += 1
        keys_by_appid[appid] = frozenset(keys)
    alias_names = {publisher_key(alias): alias for alias in aliases.values()}
    names = {}
    for key, counts in spellings.items():
        names[key] = alias_names.get(key) or min(counts, key=lambda spelling: (-counts[spelling], spelling))
    alias_keys = {spelling_key: publisher_key(alias) for spelling_key, alias in aliases.items()}
    return PublisherIndex(keys_by_appid, names, alias_keys)
