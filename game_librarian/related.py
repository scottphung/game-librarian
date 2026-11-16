""""More games like this": other catalog games that share a game's most characteristic Steam tags.

A deterministic rule, not a model:

1. Each game is described by its TOP_TAGS most-voted Steam community tags (Steam orders tags by votes).
2. Two kinds of tags are ignored, because they say little about what a game is like:
   tags found on more than half of the catalog's games (such as "Singleplayer"), and content
   tags such as "Sexual Content" or "Gore".
3. Another game is related when it shares at least MIN_SHARED_TAGS of the remaining tags.
4. Games that share more tags come first; ties go to the game with more positive Steam reviews,
   then the title A–Z, then the Steam app id. At most MAX_RELATED games are suggested.

A game with too few matches gets fewer suggestions, or none, rather than weak ones.
"""

from __future__ import annotations

from dataclasses import dataclass

TOP_TAGS = 10
MIN_SHARED_TAGS = 3
MAX_RELATED = 4
COMMON_TAG_SHARE = 0.5  # a tag among the top tags of more than half the catalog is ignored
# Content warnings, not descriptions of play.
CONTENT_TAGS = frozenset({"Sexual Content", "Nudity", "Gore", "Violent", "Mature", "NSFW", "Hentai"})
EXPLAINED_TAGS = 3  # how many shared tags an explanation names


@dataclass(frozen=True)
class RelatedGame:
    """One suggestion and the tags it shares with the game being viewed (in that game's tag order)."""

    appid: int
    shared_tags: tuple[str, ...]

    @property
    def explanation(self) -> str:
        """'Shares Roguelike, Deckbuilding and Strategy tags', or '…, including …' when more are shared."""
        named = list(self.shared_tags[:EXPLAINED_TAGS])
        listed = named[0] if len(named) == 1 else f"{', '.join(named[:-1])} and {named[-1]}"
        if len(self.shared_tags) > EXPLAINED_TAGS:
            return f"Shares {len(self.shared_tags)} tags, including {listed}"
        return f"Shares {listed} tags"


def ignored_tags(tags_by_appid: dict[int, list[str]]) -> set[str]:
    """Content tags, plus tags among the top tags of more than half of the games."""
    counts: dict[str, int] = {}
    for tags in tags_by_appid.values():
        for tag in set(tags[:TOP_TAGS]):
            counts[tag] = counts.get(tag, 0) + 1
    games = len(tags_by_appid)
    common = {tag for tag, count in counts.items() if games and count / games > COMMON_TAG_SHARE}
    return common | CONTENT_TAGS


def related_games(appid: int, tags_by_appid: dict[int, list[str]], positive_by_appid: dict[int, int | None],
                  sort_name_by_appid: dict[int, str], *, limit: int = MAX_RELATED) -> list[RelatedGame]:
    """Suggest up to `limit` other catalog games that share at least MIN_SHARED_TAGS characteristic tags.

    `tags_by_appid` holds every catalog game's tags, most voted first; `positive_by_appid` the usable
    positive review counts (None when unknown) and `sort_name_by_appid` the library sort names, used
    only to break ties.
    """
    ignored = ignored_tags(tags_by_appid)
    mine = [tag for tag in tags_by_appid.get(appid, [])[:TOP_TAGS] if tag not in ignored]
    if len(mine) < MIN_SHARED_TAGS:
        return []
    candidates = []
    for other, tags in tags_by_appid.items():
        if other == appid:
            continue
        theirs = set(tags[:TOP_TAGS])
        shared = tuple(tag for tag in mine if tag in theirs)
        if len(shared) >= MIN_SHARED_TAGS:
            candidates.append(RelatedGame(other, shared))
    candidates.sort(key=lambda game: (-len(game.shared_tags), -(positive_by_appid.get(game.appid) or 0),
                                      sort_name_by_appid.get(game.appid, ""), game.appid))
    return candidates[:limit]


def no_related_reason(appid: int, tags_by_appid: dict[int, list[str]]) -> str:
    """Why a game has no suggestions, for the details view."""
    if not tags_by_appid.get(appid):
        return "Steam lists no community tags for this game, so no related games can be suggested."
    return (f"No other catalog game shares at least {MIN_SHARED_TAGS} of this game's most-voted Steam tags "
            "(very common tags and content tags are not counted).")
