"""Turn raw Steam payloads into small, display-ready records.

Everything here is pure (no network, no database), so it is easy to test.
Text is cleaned and shortened but never rewritten: the app shows what the
source says and nothing more.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any

from .config import ASSET_BASE_URL, TRAILER_BASE_URL

# Steam store category ids that describe how a game is played, verified against live
# appdetails data: 1 Multi-player, 2 Single-player, 9 Co-op, 20 MMO, 24 Shared/Split
# Screen, 27 Cross-Platform Multiplayer, 36 Online PvP, 37 Shared/Split Screen PvP,
# 38 Online Co-op, 39 Shared/Split Screen Co-op, 48 LAN Co-op, 49 PvP.
MODE_CATEGORY_IDS = {1, 2, 9, 20, 24, 27, 36, 37, 38, 39, 48, 49}

MIN_REVIEW_CHARS = 20  # skip near-empty reviews such as "yes" or a single emoji
EXCERPT_MAX_CHARS = 600

# ---------------------------------------------------------------------- search keys

# Trademark-style symbols. Removed before Unicode normalization, which would turn "™" into "TM".
TRADEMARK_SYMBOLS = re.compile(r"[™®©℠]")
# Apostrophes join a word ("Baldur's" -> "baldurs") instead of splitting it.
APOSTROPHES = re.compile(r"['’`´]")
# Anything that is not a letter or digit, in any script; underscores count as separators.
NON_WORD = re.compile(r"[\W_]+")
LEADING_ARTICLE = re.compile(r"^(?:the|a|an) (?=\S)")


def normalize_for_search(text: str) -> str:
    """Lowercase `text`, drop accents, symbols and punctuation: "DARK SOULS™ II" -> "dark souls ii"."""
    text = TRADEMARK_SYMBOLS.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = APOSTROPHES.sub("", text.lower().replace("&", " and "))
    return NON_WORD.sub(" ", text).strip()


def sort_key(name: str) -> str:
    """Library-style sort key: a leading "The", "A" or "An" is ignored."""
    return LEADING_ARTICLE.sub("", normalize_for_search(name))


# ---------------------------------------------------------------------- descriptions

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LIST_TAGS = {"ul", "ol"}
_PARAGRAPH_TAGS = {"p", "div", "blockquote", "table", "tr"}
_SKIPPED_TAGS = {"script", "style", "video", "audio", "iframe", "noscript"}
_REDUNDANT_FIRST_HEADINGS = {"about the game", "about this game"}


def _squash_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _DescriptionParser(HTMLParser):
    """Collect headings, paragraphs and bullet lists from Steam's description HTML.

    Images and videos are dropped and inline formatting is flattened to plain text.
    Each `<br>` line becomes its own paragraph, which matches how Steam lays them out.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._text: list[str] = []
        self._list_items: list[str] | None = None
        self._item_text: list[str] | None = None
        self._in_heading = False
        self._skip_depth = 0

    def _flush_paragraph(self) -> None:
        text = _squash_spaces("".join(self._text))
        self._text = []
        if text:
            self.blocks.append({"kind": "paragraph", "text": text})

    def _finish_list(self) -> None:
        if self._list_items:
            self.blocks.append({"kind": "list", "items": self._list_items})
        self._list_items = None
        self._item_text = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _HEADING_TAGS:
            self._flush_paragraph()
            self._in_heading = True
        elif tag in _LIST_TAGS:
            self._flush_paragraph()
            self._list_items = []
        elif tag == "li":
            self._item_text = []
        elif tag == "br":
            if self._item_text is not None:
                self._item_text.append(" ")
            elif self._in_heading:
                self._text.append(" ")
            else:
                self._flush_paragraph()
        elif tag in _PARAGRAPH_TAGS and self._item_text is None:
            self._flush_paragraph()

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        if tag in _SKIPPED_TAGS:  # a self-closed <video/> has no end tag
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _HEADING_TAGS and self._in_heading:
            heading = _squash_spaces("".join(self._text))
            self._text = []
            self._in_heading = False
            if heading:
                self.blocks.append({"kind": "heading", "text": heading})
        elif tag == "li" and self._item_text is not None:
            item = _squash_spaces("".join(self._item_text))
            if item and self._list_items is not None:
                self._list_items.append(item)
            self._item_text = None
        elif tag in _LIST_TAGS:
            self._finish_list()
        elif tag in _PARAGRAPH_TAGS and self._item_text is None:
            self._flush_paragraph()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._item_text is not None:
            self._item_text.append(data)
        elif self._list_items is None:
            self._text.append(data)

    def close(self) -> None:
        super().close()
        self._finish_list()
        self._flush_paragraph()


def description_blocks(html_text: str) -> list[dict]:
    """Split description HTML into heading, paragraph and list blocks of plain text.

    Example: '<h2>Features</h2>Explore<br><ul><li>Co-op</li></ul>' ->
    [{"kind": "heading", "text": "Features"}, {"kind": "paragraph", "text": "Explore"},
     {"kind": "list", "items": ["Co-op"]}]
    """
    parser = _DescriptionParser()
    parser.feed(html_text or "")
    parser.close()
    blocks = parser.blocks
    if blocks and blocks[0]["kind"] == "heading" and blocks[0]["text"].lower() in _REDUNDANT_FIRST_HEADINGS:
        blocks = blocks[1:]
    return blocks


def html_to_text(html_text: str) -> str:
    """Flatten a short HTML snippet (such as Steam's short description) to one line of text."""
    parts = []
    for block in description_blocks(html_text):
        parts.extend(block["items"] if block["kind"] == "list" else [block["text"]])
    return " ".join(parts)


# A sentence ends with ".", "!", "?" or "…" (optionally followed by a closing quote or bracket),
# then whitespace and a capital letter or digit, which may follow an opening quote or bracket.
# Example: "Survival is just the beginning. Six years have passed" ends a sentence before "Six".
SENTENCE_END = re.compile(r"[.!?…][\"”’)]?(?=\s+[\"“‘(]?[A-Z0-9])")
# Words whose trailing period does not end a sentence ("Dr. Doom", "Humans vs. Zombies").
ABBREVIATIONS = {"dr", "jr", "mr", "mrs", "ms", "mt", "no", "sr", "st", "vol", "vs"}
CARD_SUMMARY_MIN_CHARS = 60  # a shorter first sentence ("A storm is coming.") takes the next one too
CARD_SUMMARY_MAX_CHARS = 160


def split_sentences(text: str) -> list[str]:
    """Split plain text into sentences, keeping initials ("Leon S. Kennedy") and abbreviations intact."""
    sentences, start = [], 0
    for match in SENTENCE_END.finditer(text):
        words = text[start : match.start()].split()
        last_word = words[-1].lower() if words else ""
        if (len(last_word) == 1 and last_word.isalpha()) or last_word in ABBREVIATIONS:
            continue
        sentences.append(text[start : match.end()].strip())
        start = match.end()
    if text[start:].strip():
        sentences.append(text[start:].strip())
    return sentences


def concise_description(text: str, *, min_chars: int = CARD_SUMMARY_MIN_CHARS,
                        max_chars: int = CARD_SUMMARY_MAX_CHARS) -> str:
    """Shorten a store description to its first sentence, for compact catalog cards.

    The text is shortened, never rewritten. While the summary is shorter than
    `min_chars` ("A storm is coming."), the next sentence is added. A summary longer
    than `max_chars` is cut at a word boundary and ends with "…".
    """
    sentences = split_sentences((text or "").strip())
    if not sentences:
        return ""
    summary = sentences[0]
    for sentence in sentences[1:]:
        if len(summary) >= min_chars:
            break
        summary = f"{summary} {sentence}"
    return shorten(summary, max_chars)[0]


# ---------------------------------------------------------------------- store details


@dataclass
class GameDetails:
    """Display-ready store details for one Steam app."""

    appid: int
    name: str
    app_type: str
    short_description: str
    about_blocks: list[dict]
    header_image_url: str | None
    developers: list[str]
    publishers: list[str]
    genres: list[str]
    modes: list[str]
    platforms: list[str]
    release_date: str | None
    coming_soon: bool


def _clean_names(values: Any) -> list[str]:
    """Trim a list of names from Steam and drop blanks ("Nixxes Software " -> "Nixxes Software")."""
    if not isinstance(values, list):
        return []
    return [name.strip() for name in values if isinstance(name, str) and name.strip()]


def parse_app_details(appid: int, data: dict) -> GameDetails:
    """Build GameDetails from the `data` object of a store/api/appdetails response."""
    release = data.get("release_date") or {}
    platforms = data.get("platforms") or {}
    short_description = html_to_text(data.get("short_description") or "")
    about_blocks = description_blocks(data.get("about_the_game") or data.get("detailed_description") or "")
    # Many store pages repeat the short description as their first paragraph; it is shown once, as the lead.
    if about_blocks and about_blocks[0].get("text", "").casefold() == short_description.casefold():
        about_blocks = about_blocks[1:]
    return GameDetails(
        appid=appid,
        name=(data.get("name") or "").strip(),
        app_type=data.get("type") or "game",
        short_description=short_description,
        about_blocks=about_blocks,
        header_image_url=data.get("header_image") or None,
        developers=_clean_names(data.get("developers")),
        publishers=_clean_names(data.get("publishers")),
        genres=[g["description"] for g in data.get("genres") or [] if g.get("description")],
        modes=[c["description"] for c in data.get("categories") or [] if c.get("id") in MODE_CATEGORY_IDS],
        platforms=[label for key, label in (("windows", "Windows"), ("mac", "macOS"), ("linux", "Linux")) if platforms.get(key)],
        release_date=(release.get("date") or "").strip() or None,
        coming_soon=bool(release.get("coming_soon")),
    )


# Steam's English release dates name one day, in one of two orders:
# "Aug 3, 2023" (the usual store format) or "3 Aug, 2023". The comma is optional.
RELEASE_MONTH_FIRST = re.compile(r"(?P<month>[A-Za-z]+)\.? (?P<day>\d{1,2}),? (?P<year>\d{4})")
RELEASE_DAY_FIRST = re.compile(r"(?P<day>\d{1,2}) (?P<month>[A-Za-z]+)\.?,? (?P<year>\d{4})")
# English month names and abbreviations. A fixed table, because the calendar module follows the system locale.
MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_release_date(text: str | None) -> date | None:
    """Return the exact day in Steam's release-date text, or None when the text does not give one.

    "Aug 3, 2023" and "3 Aug, 2023" -> date(2023, 8, 3). A release date is never guessed:
    text that is less exact or unclear, such as "Q3 2026", "Aug 2026", "2026", "Coming soon"
    or "03/08/2023" (day or month first?), returns None, and so does an impossible date
    such as "Feb 30, 2024".
    """
    cleaned = " ".join((text or "").split())
    for pattern in (RELEASE_MONTH_FIRST, RELEASE_DAY_FIRST):
        match = pattern.fullmatch(cleaned)
        if match is None:
            continue
        month = MONTH_NUMBERS.get(match["month"].casefold())
        if month is None:
            return None
        try:
            return date(int(match["year"]), month, int(match["day"]))
        except ValueError:
            return None
    return None


def tags_from_store_item(item: dict, tag_names: dict[int, str]) -> list[str]:
    """Return the app's community tags, most voted first, using Steam's tag-name map."""
    weighted = item.get("tags") or [{"tagid": tagid} for tagid in item.get("tagids") or []]
    ordered = sorted(weighted, key=lambda tag: tag.get("weight", 0), reverse=True)
    names = []
    for tag in ordered:
        name = tag_names.get(int(tag.get("tagid", -1)))
        if name and name not in names:
            names.append(name)
    return names


def cover_url_from_store_item(item: dict) -> str | None:
    """Return the portrait library cover (600x900) URL when Steam lists one for the app."""
    assets = item.get("assets") or {}
    url_format, file_name = assets.get("asset_url_format"), assets.get("library_capsule")
    if not url_format or not file_name:
        return None
    return ASSET_BASE_URL + url_format.replace("${FILENAME}", file_name)


def preview_clip_from_store_item(item: dict) -> dict | None:
    """Return the short looping clip Steam cuts from one of the app's store trailers.

    Steam plays these "microtrailers" when you hover a game on its store. They come
    from publisher trailers, so they are promotional and may not show real gameplay;
    the trailer's own title is kept so the page can say exactly where a clip came from.
    A trailer whose title mentions gameplay is preferred, then Steam's first highlight.
    Only trailers Steam marks as suitable for all ages are used.

    Returns {"title", "webm", "mp4", "poster"} or None when no trailer has a clip.
    """
    trailers = item.get("trailers") or {}
    candidates = [
        trailer
        for trailer in (trailers.get("highlights") or []) + (trailers.get("other_trailers") or [])
        if trailer.get("all_ages") is True and trailer.get("microtrailer")
    ]
    gameplay_titled = [t for t in candidates if "gameplay" in (t.get("trailer_name") or "").casefold()]
    for trailer in gameplay_titled + candidates:
        files = {entry.get("type"): entry.get("filename") for entry in trailer["microtrailer"] if entry.get("filename")}
        webm, mp4 = files.get("video/webm"), files.get("video/mp4")
        if not webm and not mp4:
            continue
        poster = None
        if trailer.get("trailer_url_format") and trailer.get("screenshot_full"):
            poster = ASSET_BASE_URL + trailer["trailer_url_format"].replace("${FILENAME}", trailer["screenshot_full"])
        return {
            "title": (trailer.get("trailer_name") or "").strip() or None,
            "webm": TRAILER_BASE_URL + webm if webm else None,
            "mp4": TRAILER_BASE_URL + mp4 if mp4 else None,
            "poster": poster,
        }
    return None


def screenshot_from_store_item(item: dict) -> str | None:
    """Return the app's first store screenshot that Steam marks as suitable for all ages."""
    screenshots = (item.get("screenshots") or {}).get("all_ages_screenshots") or []
    for screenshot in sorted(screenshots, key=lambda shot: shot.get("ordinal", 0)):
        if screenshot.get("filename"):
            return ASSET_BASE_URL + screenshot["filename"]
    return None


def content_hash(*parts: Any) -> str:
    """Stable fingerprint of prepared data, used to count which games actually changed."""
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- reviews

# Steam hides spoilers until clicked, so they are removed from excerpts: "[spoiler]…[/spoiler]".
SPOILER_BBCODE = re.compile(r"\[spoiler\].*?\[/spoiler\]", re.IGNORECASE | re.DOTALL)
# Embedded media carry no readable text: "[img]https://…[/img]", "[previewyoutube=…][/previewyoutube]".
MEDIA_BBCODE = re.compile(r"\[(img|previewyoutube)\b[^\]]*\].*?\[/\1\]", re.IGNORECASE | re.DOTALL)
# A list item marker: "[*]".
BULLET_BBCODE = re.compile(r"\[\*\]")
# Formatting tags whose text is kept: "[b]", "[/h1]", "[url=https://…]", "[list]".
FORMAT_BBCODE = re.compile(
    r"\[/?(?:h[1-6]|b|i|u|s|strike|noparse|hr|code|quote|list|olist|table|tr|th|td|url|spoiler)(?:=[^\]]*)?\]",
    re.IGNORECASE,
)


@dataclass
class ReviewExcerpt:
    """One review shortened for display. The author's identity is deliberately not kept."""

    recommendation_id: str
    text: str
    truncated: bool
    votes_up: int
    playtime_minutes: int | None
    created_at: str | None


@dataclass
class ReviewSummary:
    """Overall review totals as Steam computes them (all languages, Steam purchasers)."""

    total: int
    positive: int
    negative: int
    score_desc: str | None


def clean_review_text(text: str) -> str:
    """Remove BBCode markup and spoilers, keep paragraphs, and tidy whitespace."""
    text = SPOILER_BBCODE.sub(" (spoiler removed) ", text or "")
    text = MEDIA_BBCODE.sub(" ", text)
    text = BULLET_BBCODE.sub("\n• ", text)
    text = FORMAT_BBCODE.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def shorten(text: str, max_chars: int = EXCERPT_MAX_CHARS) -> tuple[str, bool]:
    """Cut `text` at the last word boundary before `max_chars`; return (text, was_cut)."""
    if len(text) <= max_chars:
        return text, False
    cut = max(text.rfind(" ", 0, max_chars), text.rfind("\n", 0, max_chars))
    if cut < max_chars // 2:  # one very long word: cut mid-word rather than lose most of the text
        cut = max_chars
    return text[:cut].rstrip(" ,;:-\n") + "…", True


def iso_from_unix(timestamp: Any) -> str | None:
    """Convert a Unix timestamp from Steam to an ISO 8601 UTC string."""
    if not timestamp:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat(timespec="seconds")


def select_excerpts(reviews: list[dict], *, positive: bool, count: int) -> list[ReviewExcerpt]:
    """Keep the first `count` reviews (already sorted by helpfulness) that have readable text."""
    excerpts: list[ReviewExcerpt] = []
    for review in reviews:
        if bool(review.get("voted_up")) != positive:
            continue  # guard: the request already filters by review type
        text = clean_review_text(review.get("review") or "")
        if len(text) < MIN_REVIEW_CHARS:
            continue
        short_text, truncated = shorten(text)
        author = review.get("author") or {}
        excerpts.append(
            ReviewExcerpt(
                recommendation_id=str(review.get("recommendationid", "")),
                text=short_text,
                truncated=truncated,
                votes_up=int(review.get("votes_up") or 0),
                playtime_minutes=author.get("playtime_at_review"),
                created_at=iso_from_unix(review.get("timestamp_created")),
            )
        )
        if len(excerpts) == count:
            break
    return excerpts


def parse_review_summary(response: dict) -> ReviewSummary:
    """Read the totals from a GetAppReviews response requested with review_type All."""
    summary = response.get("query_summary") or {}
    return ReviewSummary(
        total=int(summary.get("total_reviews") or 0),
        positive=int(summary.get("total_positive") or 0),
        negative=int(summary.get("total_negative") or 0),
        score_desc=summary.get("review_score_desc") or None,
    )


def percent_positive(positive: int, total: int) -> int | None:
    """Share of positive reviews, rounded down like the Steam store (96.8% -> 96)."""
    if not total:
        return None
    return math.floor(100 * positive / total)
