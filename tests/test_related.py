"""'More games like this' (related.py): shared characteristic tags, deterministic order, honest gaps."""

from game_librarian.related import MAX_RELATED, RelatedGame, ignored_tags, no_related_reason, related_games

# Twenty games. "Singleplayer" is on all of them, so it says nothing and is ignored; the deckbuilder
# tags are on 5 of 20 games, so they are characteristic. Games 8-20 are filler with their own tags.
TAGS = {
    1: ["Singleplayer", "Roguelike", "Deckbuilding", "Strategy", "Card Game", "Pixel Graphics", "Sexual Content"],
    2: ["Singleplayer", "Roguelike", "Deckbuilding", "Strategy", "Card Game", "Turn-Based"],  # shares 4 with game 1
    3: ["Singleplayer", "Roguelike", "Deckbuilding", "Strategy", "Action"],  # shares 3
    4: ["Singleplayer", "Roguelike", "Deckbuilding", "Strategy", "Horror"],  # shares 3
    5: ["Singleplayer", "Roguelike", "Deckbuilding", "Sexual Content", "Racing"],  # 2, plus an ignored content tag
    6: ["Singleplayer", "Racing", "Sports"],
    7: ["Singleplayer", "Racing", "Sports", "Simulation"],  # shares only 2 with game 6: a weak match
    **{appid: ["Singleplayer", f"Filler {appid}"] for appid in range(8, 21)},
}
POSITIVE = {1: 100, 2: 50, 3: 900, 4: 10, 5: 5000, 6: 1, 7: 2}
NAMES = {appid: f"game {appid:02d}" for appid in TAGS}


def appids(suggestions):
    return [suggestion.appid for suggestion in suggestions]


def test_related_games_share_three_characteristic_tags_and_exclude_the_game_itself():
    suggestions = related_games(1, TAGS, POSITIVE, NAMES)
    assert appids(suggestions) == [2, 3, 4]  # game 5 shares only two once the content tag is ignored
    assert suggestions[0].shared_tags == ("Roguelike", "Deckbuilding", "Strategy", "Card Game")  # in game 1's order
    assert 1 not in appids(suggestions)


def test_order_is_shared_tags_then_positive_reviews_then_title():
    # 4 shared tags first; then 3 shared, the game with 900 positive reviews before the one with 10.
    assert appids(related_games(1, TAGS, POSITIVE, NAMES)) == [2, 3, 4]
    reversed_reviews = {**POSITIVE, 3: 10, 4: 900}
    assert appids(related_games(1, TAGS, reversed_reviews, NAMES)) == [2, 4, 3]
    tie = {**POSITIVE, 3: 10, 4: 10}
    assert appids(related_games(1, TAGS, tie, NAMES)) == [2, 3, 4]  # equal reviews: title A-Z ("game 03" < "game 04")


def test_explanations_name_the_shared_tags():
    assert RelatedGame(3, ("Roguelike", "Deckbuilding", "Strategy")).explanation == \
        "Shares Roguelike, Deckbuilding and Strategy tags"
    assert RelatedGame(2, ("Roguelike", "Deckbuilding", "Strategy", "Card Game")).explanation == \
        "Shares 4 tags, including Roguelike, Deckbuilding and Strategy"


def test_common_and_content_tags_are_ignored():
    ignored = ignored_tags(TAGS)
    assert {"Singleplayer", "Sexual Content", "Gore"} <= ignored
    assert "Roguelike" not in ignored and "Racing" not in ignored


def test_weak_or_missing_matches_give_no_suggestions_and_say_why():
    assert related_games(6, TAGS, POSITIVE, NAMES) == []  # game 7 shares only Racing and Sports
    assert "at least 3 of this game's most-voted Steam tags" in no_related_reason(6, TAGS)
    no_tags = {**TAGS, 21: []}
    assert related_games(21, no_tags, POSITIVE, NAMES) == []
    assert "no community tags" in no_related_reason(21, no_tags)


def test_at_most_four_suggestions():
    many = {appid: ["Roguelike", "Deckbuilding", "Strategy", "Card Game"] for appid in range(1, 12)}
    many.update({appid: ["Racing"] for appid in range(12, 30)})  # keeps the four tags on under half the catalog
    suggestions = related_games(1, many, {}, {appid: f"{appid:02d}" for appid in many})
    assert len(suggestions) == MAX_RELATED and appids(suggestions) == [2, 3, 4, 5]
