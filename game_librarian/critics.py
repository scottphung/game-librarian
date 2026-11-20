"""What the critics said: critic scores read conservatively from English Wikipedia review tables.

Article match (by identifier, never by a similar title):
  1. Wikidata items that list the game's Steam app id (property P1733) or, for a game known only to
     IGDB, its IGDB slug (P5794), and each item's English Wikipedia article.
  2. Verified only when exactly one item is found, it has an English article, the article is not a
     redirect to a section of another article, it has a video game infobox, and its title (without a
     trailing qualifier such as "(video game)") equals the game's name, ignoring case, accents and
     punctuation. Anything else waits for review with the reason; curated/critic_articles.csv records a
     reviewer's decision to use or reject an article.
  Only scores from a verified (or reviewer-approved) article are ever shown.

Extraction: the article's {{Video game reviews}} templates (and the template's redirects). Codes come from
Module:Video game reviews/data (revision 1381666060, 2026-10-24). A score is kept as written when it is one
of these formats; anything else is stored as "needs review" and not shown:
  * a fraction such as 9/10, 9.5/10, 87/100, 4/5, 39/40 or 18/20 (the scale is never converted);
  * a percentage such as 97% (PC Gamer), or OpenCritic's "95% recommend";
  * a letter grade such as A or B+;
  * {{Rating|4.5|5}} stars, shown as 4.5/5;
  * the verdict words Recommended, Essential or Avoid (Eurogamer's former verdicts).
A platform or edition label is kept only as the table writes it: "PC: 86/100", "(PS5) 92/100",
"96/100 (PC)" or a platform column (IGN_PS4). Several scores in one cell without labels, several scores
in one line, and values filled in from Wikidata when the page renders are left for review.
Aggregates (Metacritic, OpenCritic, GameRankings) are kept apart from individual critics' reviews.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote

from . import store
from .curated import CriticArticleDecision, CuratedData
from .prepare import normalize_for_search
from .wikipedia import Article, WikimediaClient, WikimediaError

SOURCE = "critics"
MAX_AGE = timedelta(days=30)  # an article is read again after this long
RETRY_AFTER_ERROR = timedelta(days=1)
WIKIPEDIA_PAGE_URL = "https://en.wikipedia.org/wiki/{title}"
WIKIPEDIA_REVISION_URL = "https://en.wikipedia.org/w/index.php?title={title}&oldid={revision}"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

# Module:Video game reviews/data (revision 1381666060, 2026-10-24): parameter code -> publication name.
AGGREGATORS = {"GR": "GameRankings", "MC": "Metacritic", "OC": "OpenCritic"}
REVIEWERS = {
    "1UP": "1Up.com", "4P": "4Players", "ACE": "ACE", "AdvGamers": "Adventure Gamers", "Allgame": "AllGame",
    "ASM": "Aktueller Software Markt", "AmAction": "Amiga Action", "AmComputing": "Amiga Computing",
    "AmForce": "Amiga Force", "AmFormat": "Amiga Format", "AmPower": "Amiga Power",
    "AmUI": "Amiga User International", "AAction": "Amstrad Action", "Amtix": "Amtix", "AppSpy": "AppSpy",
    "AVC": "The A.V. Club", "BMD": "Beep! MegaDrive", "CNG": "CNET Gamecenter", "CU": "Commodore User",
    "CGR": "Computer Game Review", "CGM": "Computer Games Magazine", "CGSP": "Computer Games Strategy Plus",
    "CGW": "Computer Gaming World", "CP": "Consoles +", "CRASH": "Crash", "CVG": "Computer and Video Games",
    "Destruct": "Destructoid", "DT": "Digital Trends", "Dragon": "Dragon", "EZA": "Easy Allies", "Edge": "Edge",
    "EGM": "Electronic Gaming Monthly", "EPD": "EP Daily", "EuroG": "Eurogamer", "Fam": "Famitsu", "G4": "G4",
    "GI": "Game Informer", "GP": "Game Players", "GD": "GameDaily", "GameFan": "GameFan", "Gamekult": "Gamekult",
    "GamePro": "GamePro", "GameRev": "GameRevolution", "GMaster": "GamesMaster", "GSpot": "GameSpot",
    "GSpy": "GameSpy", "GRadar": "GamesRadar+", "GStar": "GameStar", "GTM": "GamesTM", "GX": "Games-X",
    "GT": "GameTrailers", "Gamezebo": "Gamezebo", "GameZone": "GameZone", "GPCE": "Gekkan PC Engine",
    "Gen4": "Génération 4", "GB": "Giant Bomb", "HCG": "Hardcore Gamer", "HC": "HobbyConsolas", "Hyper": "Hyper",
    "IGN": "IGN", "IW": "Impress Watch", "JXV": "Jeuxvideo.com", "JP": "Joypad", "JS": "Joystick",
    "Joystiq": "Joystiq", "KS": "Kill Screen", "MG": "M! Games", "ML": "MacLife", "MW": "Macworld",
    "MPCE": "Marukatsu PC Engine", "MaxPC": "Maximum PC", "MM": "Mean Machines", "MMS": "Mean Machines Sega",
    "MF": "Mega Fun", "MS": "MeriStation", "MIC": "Micromanía", "N64": "N64 Magazine", "NGen": "Next Generation",
    "NG": "NGamer", "NGC": "NGC Magazine", "NLife": "Nintendo Life", "NP": "Nintendo Power",
    "NWR": "Nintendo World Report", "NME": "NME", "ONM": "Official Nintendo Magazine",
    "OPMAU": "PlayStation Official Magazine – Australia", "OPMUK": "PlayStation Official Magazine – UK",
    "OPM": "Official U.S. PlayStation Magazine", "OXMUK": "Official Xbox Magazine (UK)",
    "OXM": "Official Xbox Magazine (US)", "PALGN": "PALGN", "PCA": "PC Accelerator", "PCEF": "PC Engine Fan",
    "PCF": "PC Format", "PCGUK": "PC Gamer (UK)", "PCGUS": "PC Gamer (US)", "PCG": "PC Games (DE)",
    "GPPCG": "PC Games (US)", "PCPP": "PC PowerPlay", "PCZone": "PC Zone", "PCGN": "PCGamesN", "PCM": "PCMag",
    "Play": "Play", "PG": "Pocket Gamer", "Poly": "Polygon", "PO": "Player One",
    "PSM": "PlayStation: The Official Magazine", "PSM3": "PSM3", "PSQ": "Push Square", "Raze": "Raze",
    "Retro": "Retro Gamer", "RPG": "RPGamer", "RPGFan": "RPGFan", "SN": "Shacknews", "SUser": "Sinclair User",
    "STAction": "ST Action", "STFormat": "ST Format", "STRev": "ST Review", "SJ": "Superjuegos",
    "SGP": "Super Game Power", "SP": "Super Play", "TX": "TeamXbox", "TR": "TechRadar", "TELE": "The Telegraph",
    "TGM": "The Games Machine (UK)", "TGMIt": "The Games Machine (Italy)", "TG": "The Guardian", "T1": "The One",
    "TILT": "Tilt", "TOT": "Total!", "TA": "TouchArcade", "TRev": "Trusted Reviews", "USG": "USgamer",
    "VB": "VentureBeat", "VGC": "Video Games Chronicle", "VG247": "VG247", "VGS": "Video Games (DE)",
    "VG": "VideoGamer.com", "VGCE": "VideoGames & Computer Entertainment", "XPlay": "X-Play",
    "YSinclair": "Your Sinclair", "Zero": "Zero", "Z64": "Zzap!64",
}
SYSTEMS = {
    "3DO": "3DO", "3DS": "3DS", "AMI": "Amiga", "CPC": "CPC", "ARC": "Arcade", "A2600": "Atari 2600",
    "JAG": "Atari Jaguar", "LYNX": "Atari Lynx", "AST": "Atari ST", "C64": "C64", "CD32": "CD32",
    "CV": "ColecoVision", "DOS": "DOS", "SDC": "Dreamcast", "DS": "DS", "GB": "Game Boy", "GBA": "GBA",
    "GBC": "GBC", "SGG": "Game Gear", "NGC": "GameCube", "INT": "Intellivision", "iOS": "iOS", "MAC": "Macintosh",
    "SMS": "Master System", "MOB": "mobile", "N-G": "N-Gage", "N64": "N64", "NGEO": "Neo Geo", "NES": "NES",
    "NS": "Switch", "NS2": "Switch 2", "PC": "PC", "VITA": "PS Vita", "PS": "PlayStation", "PS2": "PS2",
    "PS3": "PS3", "PS4": "PS4", "PS5": "PS5", "PSP": "PSP", "SSAT": "Saturn", "SMD": "Sega Genesis",
    "NSHI": "Shield", "SNES": "SNES", "TG16": "TurboGrafx-16", "VIC": "VIC-20", "WII": "Wii", "WIIU": "Wii U",
    "XBOX": "Xbox", "X360": "Xbox 360", "XONE": "Xbox One", "XSXS": "Xbox Series X/S",
    # Abbreviations editors also write inside cells.
    "XBSX": "Xbox Series X/S", "XBXS": "Xbox Series X/S", "XSX": "Xbox Series X/S", "XB1": "Xbox One",
    "NSW": "Switch",
}
# {{Video game reviews}} and its redirects (Template:Video game reviews, checked 2026-11-19).
TEMPLATE_NAMES = ("Video game reviews", "VG Reviews", "VG reviews", "Vg reviews", "Vgreviews", "Video game review",
                  "Video game multiple console reviews", "Video game multiple platforms reviews", "VG reception",
                  "Scores CVG", "Scores VG")
# Template options that are not scores.
OPTION_PARAMS = {"title", "subtitle", "state", "width", "align", "na", "showplatforms", "platforms", "systemformat",
                 "uk", "us", "edit", "noplatforms", "collapsed"}
FOOTNOTE_TEMPLATES = {"efn", "efn-ua", "efn-lr", "sfn", "sfnp", "refn", "r", "rp", "noteTag", "ntag",
                      "citation needed", "cn", "better source needed", "fact", "dead link", "update after",
                      "when", "clarify", "verify source", "notetag", "ref label", "sfnm"}
UNWRAP_TEMPLATES = {"nowrap", "nobr", "nowr", "small", "center"}
VERDICTS = ("Recommended", "Essential", "Avoid")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


# ---------------------------------------------------------------------- wikitext structure (pure)

COMMENT = re.compile(r"<!--.*?-->", re.S)
# <ref name="x">content</ref> or <ref>content</ref> (content kept) and <ref name="x" /> (a reuse).
REF_WITH_BODY = re.compile(r"<ref(?P<attrs>(?:\s[^>]*?)?)(?<!/)>(?P<body>.*?)</ref\s*>", re.S | re.I)
REF_EMPTY = re.compile(r"<ref(?P<attrs>\s[^>]*?)/\s*>", re.I)
REF_NAME = re.compile(r"""name\s*=\s*(?:"(?P<q>[^"]*)"|'(?P<s>[^']*)'|(?P<b>[^\s/>]+))""", re.I)
PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
# The `url` parameter of a citation template (not archive-url or chapter-url): "| url = https://..."
CITE_URL = re.compile(r"(?:^|\|)\s*url\s*=\s*(?P<url>https?://[^\s|}<]+)", re.I)
BARE_LINK = re.compile(r"\[(?P<url>https?://[^\s\]]+)")
BREAK = re.compile(r"<br\s*/?\s*>|\n", re.I)
WIKILINK = re.compile(r"\[\[(?:[^|\]]*\|)?(?P<text>[^\]]*)\]\]")
FORMAT_TAGS = re.compile(r"</?(?:small|big|sup|sub|span|b|i|abbr|s|u|nowiki)(?:\s[^>]*)?>", re.I)


def _ref_name(attrs: str) -> str | None:
    match = REF_NAME.search(attrs or "")
    if not match:
        return None
    return (match["q"] or match["s"] or match["b"] or "").strip() or None


def named_refs(wikitext: str) -> dict[str, str]:
    """The body of every named reference in the article: name -> body (for <ref name="x" /> reuses)."""
    found = {}
    for match in REF_WITH_BODY.finditer(COMMENT.sub("", wikitext)):
        name = _ref_name(match["attrs"])
        if name and name not in found:
            found[name] = match["body"]
    return found


def _protect_refs(text: str, refs: list[str], defined: dict[str, str]) -> str:
    """Replace each reference with a placeholder that remembers its body (a reuse gets the named body)."""
    def keep(body: str) -> str:
        refs.append(body)
        return f"\x00{len(refs) - 1}\x00"

    text = REF_WITH_BODY.sub(lambda match: keep(match["body"]), text)
    return REF_EMPTY.sub(lambda match: keep(defined.get(_ref_name(match["attrs"]) or "", "")), text)


def _match_close(text: str, start: int) -> int:
    """The index just after the "}}" that closes the "{{" at `start`, or -1 when it never closes."""
    depth, index = 0, start
    while index < len(text) - 1:
        pair = text[index:index + 2]
        if pair == "{{":
            depth += 1
            index += 2
        elif pair == "}}":
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return -1


def _split_top_level(inner: str) -> list[str]:
    """Split template text at the "|" characters that are not inside nested templates or links."""
    parts, depth_t, depth_l, current, index = [], 0, 0, [], 0
    while index < len(inner):
        pair = inner[index:index + 2]
        if pair in ("{{", "}}", "[[", "]]"):
            if pair == "{{":
                depth_t += 1
            elif pair == "}}":
                depth_t = max(0, depth_t - 1)
            elif pair == "[[":
                depth_l += 1
            else:
                depth_l = max(0, depth_l - 1)
            current.append(pair)
            index += 2
            continue
        char = inner[index]
        if char == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def _template_name_pattern(names: tuple[str, ...]) -> re.Pattern:
    """Template names as MediaWiki matches them: first letter in any case, spaces or underscores."""
    alternatives = []
    for name in names:
        first, rest = name[0], name[1:]
        rest = re.escape(rest).replace(r"\ ", "[ _]+")
        alternatives.append(f"[{first.upper()}{first.lower()}]{rest}")
    return re.compile(r"\{\{\s*(?:Template:)?(?:" + "|".join(alternatives) + r")\s*(?=[|}])")


REVIEW_TEMPLATE = _template_name_pattern(TEMPLATE_NAMES)


@dataclass
class ReviewTable:
    """One review template: its title (when the article has several) and its parameters with refs protected."""

    title: str | None
    params: dict[str, str]
    refs: list[str]


def review_tables(wikitext: str) -> list[ReviewTable]:
    """Every {{Video game reviews}} template in the article, in page order. Comments are removed first."""
    text = COMMENT.sub("", wikitext)
    defined = named_refs(wikitext)
    tables = []
    for match in REVIEW_TEMPLATE.finditer(text):
        end = _match_close(text, match.start())
        if end < 0:
            continue
        refs: list[str] = []
        inner = _protect_refs(text[match.start() + 2:end - 2], refs, defined)
        params: dict[str, str] = {}
        for part in _split_top_level(inner)[1:]:  # the first part is the template name
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            params[name.strip()] = value.strip()
        title = clean_text(params.get("title", ""), []) or None
        tables.append(ReviewTable(title, params, refs))
    return tables


# ---------------------------------------------------------------------- cells and scores (pure)


def _replace_templates(text: str, problems: list[str]) -> str:
    """Rating stars become "N/M", formatting wrappers keep their text, footnotes go away.

    Any other template inside a score is a problem: its rendering is unknown here, so the score is
    left for review instead of being guessed.
    """
    while True:
        start = text.find("{{")
        if start < 0:
            return text
        end = _match_close(text, start)
        if end < 0:
            problems.append("an unclosed template")
            return text[:start]
        parts = [part.strip() for part in _split_top_level(text[start + 2:end - 2])]
        name = parts[0].replace("_", " ").strip()
        lower = name.lower()
        if lower in ("rating", "star rating", "rating-5"):
            numbers = [part for part in parts[1:] if "=" not in part]
            if len(numbers) >= 2 and re.fullmatch(r"\d+(?:\.\d+)?", numbers[0]) and re.fullmatch(r"\d+", numbers[1]):
                replacement = f"{numbers[0]}/{numbers[1]}"
            else:
                problems.append("a rating template without a score and a scale")
                replacement = ""
        elif lower in UNWRAP_TEMPLATES:
            replacement = "|".join(part for part in parts[1:] if "=" not in part)
        elif lower in {item.lower() for item in FOOTNOTE_TEMPLATES}:
            replacement = ""
        elif lower in ("n/a", "na"):
            replacement = "N/A"
        else:
            problems.append(f"the template {{{{{name}}}}}")
            replacement = ""
        text = text[:start] + replacement + text[end:]


def clean_text(value: str, problems: list[str]) -> str:
    """Plain text of a cell or outlet name: templates, links, formatting, refs and extra spaces removed."""
    text = PLACEHOLDER.sub("", value)
    text = _replace_templates(text, problems)
    text = WIKILINK.sub(lambda match: match["text"], text)
    text = FORMAT_TAGS.sub("", text)
    text = text.replace("'''", "").replace("''", "").replace("&nbsp;", " ").replace(" ", " ")
    return " ".join(text.split()).strip()


def review_url(segment: str, refs: list[str]) -> str | None:
    """The `url` of the first citation in a segment's references (not an archive copy), if it has one."""
    for match in PLACEHOLDER.finditer(segment):
        body = refs[int(match.group(1))]
        found = CITE_URL.search(body) or BARE_LINK.search(body)
        if found:
            return found["url"]
    return None


FRACTION = re.compile(r"(?P<n>\d{1,3}(?:\.\d{1,2})?)\s*/\s*(?P<d>\d{1,3})")
PERCENT = re.compile(r"(?P<n>\d{1,3}(?:\.\d)?)\s*%")
RECOMMEND = re.compile(r"(?P<n>\d{1,3})\s*%\s*(?:of critics\s+)?recommend(?:ed|s)?", re.I)
GRADE = re.compile(r"[A-F][+\-−–]?")
LABEL_FIRST = re.compile(r"\((?P<label>[^()]{1,40})\)\s*(?P<score>.+)")
LABEL_LAST = re.compile(r"(?P<score>.+?)\s*\((?P<label>[^()]{1,40})\)")
LABEL_COLON = re.compile(r"(?P<label>[A-Za-z0-9][A-Za-z0-9 .+/&-]{0,30}?(?:\s*\([^()]{1,30}\))?)\s*:\s*(?P<score>.+)")
SWITCHED_ON = {"yes", "y", "true", "1"}  # "PS4 = yes" turns on a platform column; it is not a score


def parse_score(text: str, aggregator: str | None = None) -> tuple[str | None, str]:
    """The score as written, when its format is supported, else None, with the reason.

    Nothing is converted to another scale: "9/10" stays "9/10" and "4/5" stays "4/5".
    """
    value = text.strip()
    if not value:
        return None, "the cell is empty"
    if value.lower() == "wikidata":
        return None, "the table fills this score in from Wikidata when the page is shown"
    if value.upper() == "N/A":
        return None, "the table marks this platform as not reviewed (N/A)"
    match = FRACTION.fullmatch(value)
    if match:
        if float(match["n"]) > float(match["d"]) or float(match["d"]) == 0:
            return None, f"{value!r} is not a score out of a scale"
        return f"{match['n']}/{match['d']}", ""
    if aggregator == "OC":
        match = RECOMMEND.fullmatch(value)
        if match and int(match["n"]) <= 100:
            return f"{match['n']}% recommend", ""
        if PERCENT.fullmatch(value):
            return None, ("OpenCritic shows a top-critic average and a percentage of critics who recommend the "
                          "game; this value does not say which one it is")
    match = PERCENT.fullmatch(value)
    if match:
        if float(match["n"]) > 100:
            return None, f"{value!r} is more than 100%"
        return f"{match['n']}%", ""
    if GRADE.fullmatch(value):
        return value.replace("–", "-").replace("−", "-"), ""
    for verdict in VERDICTS:
        if value.lower() == verdict.lower():
            return verdict, ""
    if len(FRACTION.findall(value)) > 1 or "," in value:
        return None, "the cell has several scores on one line (such as one per reviewer)"
    return None, f"the format {value!r} is not one this site reads"


def _label(text: str) -> str:
    """A platform label as written, with the template's platform codes spelled out (XSXS -> Xbox Series X/S)."""
    base = text.strip()
    return SYSTEMS.get(base, base)


@dataclass(frozen=True)
class Score:
    """One score from a review table, as written there."""

    outlet: str
    kind: str  # aggregate | critic
    score: str | None
    label: str | None
    status: str  # ok | needs_review
    reason: str = ""
    outlet_code: str | None = None
    review_url: str | None = None
    table_title: str | None = None

    def as_row(self) -> dict:
        return {"outlet": self.outlet, "kind": self.kind, "score": self.score, "label": self.label,
                "status": self.status, "reason": self.reason, "outlet_code": self.outlet_code,
                "review_url": self.review_url, "table_title": self.table_title}


def parse_cell(value: str, refs: list[str], *, outlet: str, kind: str, code: str | None,
               column: str | None = None, table_title: str | None = None) -> list[Score]:
    """The scores in one cell. A cell may hold one score per line, each with its own platform label."""
    segments = [segment for segment in BREAK.split(value) if PLACEHOLDER.sub("", segment).strip()]
    parsed = []
    for segment in segments:
        problems: list[str] = []
        text = clean_text(segment, problems)
        if not text and not problems:
            continue  # only references or footnotes: nothing to read
        label = _label(column) if column else None
        score_text = text
        if label is None:
            for pattern in (LABEL_FIRST, LABEL_COLON, LABEL_LAST):
                match = pattern.fullmatch(text)
                if match:
                    label, score_text = _label(match["label"]), match["score"]
                    break
        score, why = parse_score(score_text, code if kind == "aggregate" else None)
        if problems:
            score, why = None, f"the cell uses {problems[0]}, which this site does not read"
        parsed.append(Score(outlet, kind, score, label, "ok" if score else "needs_review", why, code,
                            review_url(segment, refs), table_title))
    unlabeled = [item for item in parsed if item.label is None]
    if len(parsed) > 1 and unlabeled:
        # Several scores in one cell, and at least one does not say what it is for: none is shown.
        return [Score(item.outlet, item.kind, item.score, item.label, "needs_review",
                      "the cell has several scores and not all say which platform or edition they are for",
                      item.outlet_code, item.review_url, item.table_title) for item in parsed]
    return parsed


def scores_from_table(table: ReviewTable) -> list[Score]:
    """Every score in one review table: aggregates first, then critics, in the table's order."""
    aggregates, critics = [], []
    params = table.params
    for name, value in params.items():
        code, _, system = name.partition("_")
        if name.lower() in OPTION_PARAMS or not value:
            continue
        if not system and code in SYSTEMS and value.lower() in SWITCHED_ON:
            continue  # a platform column switch (also when the code doubles as a magazine, such as N64)
        if system and system in SYSTEMS and (code in AGGREGATORS or code in REVIEWERS):
            kind = "aggregate" if code in AGGREGATORS else "critic"
            outlet = AGGREGATORS.get(code) or REVIEWERS[code]
            (aggregates if kind == "aggregate" else critics).extend(
                parse_cell(value, table.refs, outlet=outlet, kind=kind, code=code, column=system,
                           table_title=table.title))
        elif code in AGGREGATORS and not system:
            aggregates.extend(parse_cell(value, table.refs, outlet=AGGREGATORS[code], kind="aggregate", code=code,
                                         table_title=table.title))
        elif code in REVIEWERS and not system:
            critics.extend(parse_cell(value, table.refs, outlet=REVIEWERS[code], kind="critic", code=code,
                                      table_title=table.title))
        elif re.fullmatch(r"(rev|agg)\d+", name):
            score_value = params.get(f"{name}Score", "")
            outlet = clean_text(value, [])
            if not outlet or not score_value:
                continue
            kind = "aggregate" if name.startswith("agg") else "critic"
            (aggregates if kind == "aggregate" else critics).extend(
                parse_cell(score_value, table.refs, outlet=outlet, kind=kind, code=None, table_title=table.title))
    return aggregates + critics


def article_scores(wikitext: str) -> tuple[str, list[Score]]:
    """(table status, scores) for one article: ok, no_table, or unsupported (tables without a readable score)."""
    tables = review_tables(wikitext)
    if not tables:
        return "no_table", []
    scores = [score for table in tables for score in scores_from_table(table)]
    if not any(score.status == "ok" for score in scores):
        return "unsupported", scores
    return "ok", scores


# ---------------------------------------------------------------------- article match (pure)

INFOBOX = re.compile(r"\{\{\s*Infobox[ _]video[ _]game\s*[|}]", re.I)
# A trailing disambiguation such as "(video game)", "(2018 video game)" or "(2016 game)".
TITLE_QUALIFIER = re.compile(r"\s*\((?:\d{4}\s+)?(?:video\s+)?game\)$", re.I)


def same_title(game_name: str, article_title: str) -> bool:
    """The article's title without a "(video game)" qualifier names the game, ignoring case and punctuation."""
    return normalize_for_search(TITLE_QUALIFIER.sub("", article_title)) == normalize_for_search(game_name)


@dataclass
class ArticleMatch:
    """The article chosen for a game, or why there is none yet."""

    status: str  # verified | needs_review | not_found | rejected | error
    reason: str
    title: str | None = None
    wikidata_ids: list[str] = field(default_factory=list)


def candidate_article(items: dict[str, str | None], decision: CriticArticleDecision | None) -> ArticleMatch:
    """The article to read for a game, from the Wikidata items found by its identifier (item -> article or None)."""
    ids = sorted(items)
    if decision is not None:
        if decision.decision == "reject":
            return ArticleMatch("rejected", f"{decision.checked_by} rejected the automatic match on "
                                            f"{decision.checked_on} (curated/critic_articles.csv).", None, ids)
        return ArticleMatch("verified", f"Chosen by {decision.checked_by} on {decision.checked_on} "
                                        "(curated/critic_articles.csv).", decision.article, ids)
    if not items:
        return ArticleMatch("not_found", "No Wikidata item lists this game's identifier.", None, ids)
    if len(items) > 1:
        described = ", ".join(f"{item} ({items[item] or 'no English article'})" for item in ids)
        return ArticleMatch("needs_review", f"Several Wikidata items list this game's identifier: {described}. "
                                            "A reviewer must choose in curated/critic_articles.csv.", None, ids)
    article = items[ids[0]]
    if not article:
        return ArticleMatch("not_found", f"The Wikidata item {ids[0]} has no English Wikipedia article.", None, ids)
    return ArticleMatch("verified", "", article, ids)


def check_article(match: ArticleMatch, article: Article | None, game_name: str, reviewer_chosen: bool) -> ArticleMatch:
    """Confirm the article: it exists, is not a section of another article, and describes this game."""
    if article is None:
        return ArticleMatch("not_found", f"English Wikipedia has no article {match.title!r}.", match.title,
                            match.wikidata_ids)
    if reviewer_chosen:
        return ArticleMatch("verified", match.reason, article.title, match.wikidata_ids)
    if article.section:
        return ArticleMatch("needs_review", f"{match.title!r} redirects to the section {article.section!r} of "
                                            f"{article.title!r}, which may cover another edition too.",
                            article.title, match.wikidata_ids)
    if not INFOBOX.search(article.wikitext):
        return ArticleMatch("needs_review", f"{article.title!r} has no video game infobox, so it may describe a "
                                            "series or something else.", article.title, match.wikidata_ids)
    if not same_title(game_name, article.title):
        return ArticleMatch("needs_review", f"The game is called {game_name!r} here, but the article is "
                                            f"{article.title!r} (another edition, or another name?).",
                            article.title, match.wikidata_ids)
    return ArticleMatch("verified", "The Wikidata item that lists this game's identifier links to this article, "
                                    "and its title names the game.", article.title, match.wikidata_ids)


def page_url(title: str) -> str:
    return WIKIPEDIA_PAGE_URL.format(title=quote(title.replace(" ", "_"), safe="():,'!"))


def revision_url(title: str, revision: int) -> str:
    return WIKIPEDIA_REVISION_URL.format(title=quote(title.replace(" ", "_"), safe=""), revision=int(revision))


# ---------------------------------------------------------------------- display (pure)


def critics_view(article_row, score_rows, *, released: bool) -> dict:
    """What "What the critics said" shows. Only a verified article's readable scores are shown."""
    view = {"state": "not_checked", "aggregates": [], "critics": [], "review_count": 0, "hidden": 0,
            "article": None, "released": released, "reason": None, "checked_at": None, "tables": []}
    if not released:
        view["state"] = "unreleased"
        return view
    if article_row is None:
        return view
    view["checked_at"] = article_row["checked_at"]
    view["reason"] = article_row["reason"]
    if article_row["status"] != "verified":
        view["state"] = article_row["status"]
        return view
    title, revision = article_row["article_title"], article_row["revision_id"]
    view["article"] = {"title": title, "url": page_url(title), "revision_url": revision_url(title, revision) if revision else None,
                       "revision_at": article_row["revision_at"]}
    shown = [row for row in score_rows if row["status"] == "ok"]
    view["hidden"] = len(score_rows) - len(shown)
    view["aggregates"] = [row for row in shown if row["kind"] == "aggregate"]
    view["critics"] = [row for row in shown if row["kind"] == "critic"]
    view["tables"] = sorted({row["table_title"] for row in shown if row["table_title"]})
    view["review_count"] = len(view["critics"])
    view["state"] = "ok" if shown else ("unsupported" if article_row["table_status"] == "ok"
                                        else article_row["table_status"] or "no_table")
    return view


# ---------------------------------------------------------------------- refresh (network)


@dataclass(frozen=True)
class CriticTarget:
    """A game whose critic scores may be looked up: its key, name and identifiers."""

    key: str
    name: str
    steam_appid: int | None = None
    igdb_slug: str | None = None


@dataclass
class RefreshReport:
    counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    failed: bool = False

    def status(self) -> str:
        if self.failed:
            return "failed"
        return "partial" if self.errors else "success"

    def summary(self) -> dict:
        return {"counts": dict(self.counts), "errors": self.errors[:25]}


def due_targets(conn: sqlite3.Connection, targets: list[CriticTarget], now: datetime, *, force: bool = False) -> list[CriticTarget]:
    """Targets never checked, checked more than MAX_AGE ago, or (after an error) more than a day ago."""
    rows = store.critic_article_rows(conn, [target.key for target in targets])
    due = []
    for target in targets:
        row = rows.get(target.key)
        if force or row is None:
            due.append(target)
            continue
        checked = datetime.fromisoformat(row["checked_at"])
        age = RETRY_AFTER_ERROR if row["status"] == "error" else MAX_AGE
        if now - checked >= age:
            due.append(target)
    return due


ARTICLES_PER_REQUEST = 10


def refresh_critics(conn: sqlite3.Connection, client: WikimediaClient, targets: list[CriticTarget],
                    curated: CuratedData, *, log: Callable[[str], None] = print,
                    now: Callable[[], datetime] = utc_now) -> RefreshReport:
    """Match each target to an article, read its review tables and store the result.

    A failed request leaves the stored result of the games it covered unchanged (they are tried
    again by a later run).
    """
    started = now()
    run_id = store.start_source_run(conn, SOURCE, now=_iso(started))
    report = RefreshReport()
    try:
        _refresh(conn, client, targets, curated, report, log, started)
    except BaseException as exc:
        report.failed = True
        report.errors.append(f"refresh stopped: {exc!r}")
        store.finish_source_run(conn, run_id, status="failed", summary=report.summary(), now=_iso(now()))
        raise
    if report.errors and not report.counts.get("games_saved"):
        report.failed = True
    store.finish_source_run(conn, run_id, status=report.status(), summary=report.summary(), now=_iso(now()))
    return report


def _refresh(conn, client, targets, curated, report, log, started) -> None:
    moment = _iso(started)
    try:
        items = client.wikidata_items([t.steam_appid for t in targets if t.steam_appid],
                                      [t.igdb_slug for t in targets if t.igdb_slug and not t.steam_appid])
    except WikimediaError as exc:
        report.errors.append(f"Wikidata: {exc}")
        return
    matches: dict[str, ArticleMatch] = {}
    for target in targets:
        if target.steam_appid:
            found = items.get(("steam", str(target.steam_appid)), {})
        else:
            found = items.get(("igdb", target.igdb_slug or ""), {})
        matches[target.key] = candidate_article(found, curated.critic_article_decisions.get(target.key))
    wanted = sorted({match.title for match in matches.values() if match.status == "verified" and match.title})
    articles: dict[str, Article | None] = {}
    failed_titles: set[str] = set()
    for start in range(0, len(wanted), ARTICLES_PER_REQUEST):
        chunk = wanted[start:start + ARTICLES_PER_REQUEST]
        try:
            articles.update(client.articles(chunk))
        except WikimediaError as exc:
            report.errors.append(f"Wikipedia: {exc}")
            failed_titles.update(chunk)
        log(f"[critics] read {min(start + ARTICLES_PER_REQUEST, len(wanted))}/{len(wanted)} articles")
    for target in targets:
        match = matches[target.key]
        scores: list[Score] | None = None
        table_status = "not_read"
        article: Article | None = None
        if match.status == "verified":
            if match.title in failed_titles:
                report.counts["games_not_read"] += 1
                continue  # keep what is stored; tried again later
            article = articles.get(match.title)
            reviewer_chosen = target.key in curated.critic_article_decisions
            match = check_article(match, article, target.name, reviewer_chosen)
            if article is not None:
                table_status, scores = article_scores(article.wikitext)
        report.counts[f"match_{match.status}"] += 1
        if scores:
            report.counts["scores_shown" if match.status == "verified" else "scores_waiting_for_match"] += sum(
                1 for score in scores if score.status == "ok")
            report.counts["scores_needing_review"] += sum(1 for score in scores if score.status != "ok")
        store.save_critics(conn, target.key, {
            "status": match.status, "reason": match.reason, "wikidata_ids": match.wikidata_ids,
            "article_title": article.title if article else match.title,
            "article_section": article.section if article else None,
            "revision_id": article.revision_id if article else None,
            "revision_at": article.revision_at if article else None,
            "table_status": table_status,
        }, [score.as_row() for score in scores] if scores is not None else [], now=moment)
        report.counts["games_saved"] += 1


def review_rows(conn: sqlite3.Connection) -> list[dict]:
    """Everything waiting for a person: uncertain article matches and scores the parser could not read."""
    rows = []
    for key, article in sorted(store.critic_article_rows(conn).items()):
        if article["status"] == "needs_review":
            rows.append({"game": key, "what": "article", "detail": article["reason"]})
        if article["status"] == "verified":
            for score in store.critic_scores(conn, key):
                if score["status"] != "ok":
                    rows.append({"game": key, "what": f"score: {score['outlet']}",
                                 "detail": score["reason"] + (f" (table: {score['table_title']})" if score["table_title"] else "")})
    return rows
