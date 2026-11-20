"""What the critics said (critics.py, wikipedia.py): review-table extraction, score formats, platform labels,
article matching and review states, the display rules, and the Wikimedia client's policy behavior.

The wikitext below is written for these tests in the shapes seen in real articles on 2026-11-19 (platform
prefixes, platform suffixes, OpenCritic's "recommend", footnotes, named references, commented-out rows).
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from game_librarian import critics, store
from game_librarian.critics import (ArticleMatch, CriticTarget, article_scores, candidate_article, check_article,
                                    critics_view, parse_score, review_tables, same_title)
from game_librarian.curated import CriticArticleDecision, CuratedData
from game_librarian.wikipedia import SPARQL_API, Article, WikimediaClient, WikimediaError, user_agent

ARTICLE = """{{Short description|2026 video game}}
{{Infobox video game
| title = Moon Garden
}}
'''Moon Garden''' is a game.<ref name="mc">{{cite web |url=https://www.metacritic.com/game/moon-garden/ |title=Moon Garden}}</ref>

==Reception==
{{Video game reviews
| MC = PC: 87/100<ref name="mc"/><br />PS5: 89/100<ref>{{cite web |url=https://www.metacritic.com/game/moon-garden-ps5/ |archive-url=https://web.archive.org/x}}</ref>
| OC = 93% recommend<ref>{{cite web|url=https://opencritic.com/game/1/moon-garden}}</ref>{{efn|Based on 120 reviews}}
| GSpot = 9/10<ref>{{cite web|url=https://www.gamespot.com/reviews/moon-garden}}</ref>
| IGN = (PC) 8/10
| EuroG = Recommended
| PCGUS = 91%
| GRadar = {{Rating|4.5|5}}
| Fam = 10/10, 9/10, 9/10, 9/10
| Edge = 9/10{{sfn|Edge|2026|p=88}}
<!-- | Poly = 2/10 removed: no score in the source -->
| rev1 = ''[[The Example Times]]''
| rev1Score = A-
}}
"""


def scores_by_outlet(scores):
    return {(score.outlet, score.label): score for score in scores}


def test_scores_keep_their_scale_label_kind_and_cited_source():
    status, scores = article_scores(ARTICLE)
    found = scores_by_outlet(scores)
    assert status == "ok"
    assert found[("Metacritic", "PC")].score == "87/100" and found[("Metacritic", "PS5")].score == "89/100"
    assert found[("Metacritic", "PC")].review_url == "https://www.metacritic.com/game/moon-garden/"  # a named reuse
    assert found[("Metacritic", "PS5")].review_url == "https://www.metacritic.com/game/moon-garden-ps5/"  # not the archive
    assert found[("OpenCritic", None)].score == "93% recommend" and found[("OpenCritic", None)].kind == "aggregate"
    assert found[("GameSpot", None)].score == "9/10" and found[("GameSpot", None)].kind == "critic"
    assert found[("IGN", "PC")].score == "8/10"
    assert found[("Eurogamer", None)].score == "Recommended"
    assert found[("PC Gamer (US)", None)].score == "91%"
    assert found[("GamesRadar+", None)].score == "4.5/5"  # star rating template, scale kept
    assert found[("Edge", None)].score == "9/10"  # the footnote is not part of the score
    assert found[("The Example Times", None)].score == "A-"  # a custom row
    assert ("Polygon", None) not in found  # a commented-out row is not read
    famitsu = found[("Famitsu", None)]
    assert famitsu.status == "needs_review" and famitsu.score is None and "several scores" in famitsu.reason
    assert [score.kind for score in scores[:3]] == ["aggregate"] * 3  # aggregates first


@pytest.mark.parametrize("text, expected", [
    ("9/10", "9/10"), ("9.5 / 10", "9.5/10"), ("87/100", "87/100"), ("4/5", "4/5"), ("39/40", "39/40"),
    ("18/20", "18/20"), ("97%", "97%"), ("A", "A"), ("B+", "B+"), ("C−", "C-"), ("Essential", "Essential"),
    ("recommended", "Recommended"),
])
def test_supported_score_formats_are_kept_as_written(text, expected):
    assert parse_score(text) == (expected, "")


@pytest.mark.parametrize("text, reason", [
    ("11/10", "not a score out of a scale"), ("Excellent", "not one this site reads"), ("wikidata", "Wikidata"),
    ("N/A", "not reviewed"), ("8/10, 9/10", "several scores"), ("", "empty"), ("120%", "more than 100%"),
])
def test_unsupported_scores_are_never_guessed(text, reason):
    score, why = parse_score(text)
    assert score is None and reason in why


def test_opencritic_needs_to_say_which_number_it_is():
    assert parse_score("95% recommend", "OC") == ("95% recommend", "")
    score, why = parse_score("99%", "OC")
    assert score is None and "does not say which" in why


def test_several_scores_in_a_cell_need_a_label_each():
    table = """{{Video game reviews
| MC = 95% recommend<br>93% recommend (Intergrade)
| GSpot = PS4: 10/10<br />PS5: 9/10
| IGN = 9/10<br/>8/10
| Destruct = {{unknown template|9}}
}}"""
    scores = article_scores(table)[1]
    by = {(s.outlet, s.label): s for s in scores}
    assert by[("GameSpot", "PS4")].status == "ok" and by[("GameSpot", "PS5")].score == "9/10"
    assert all(s.status == "needs_review" for s in scores if s.outlet in ("Metacritic", "IGN"))
    assert by[("Destructoid", None)].status == "needs_review" and "template" in by[("Destructoid", None)].reason


def test_platform_columns_and_column_switches():
    columns = """{{VG Reviews
| PS4 = yes
| N64 = yes
| title = Re-release reception
| IGN_PS4 = 9/10
| MC_XSXS = 80/100
}}"""
    assert [(s.outlet, s.label, s.score, s.table_title) for s in article_scores(columns)[1]] == [
        ("Metacritic", "Xbox Series X/S", "80/100", "Re-release reception"),
        ("IGN", "PS4", "9/10", "Re-release reception"),
    ]  # "N64 = yes" switches on a column; it is not a score from N64 Magazine
    magazine = article_scores("{{Video game reviews | N64 = 8/10 }}")[1]
    assert [(s.outlet, s.score) for s in magazine] == [("N64 Magazine", "8/10")]


def test_articles_without_a_readable_table():
    assert article_scores("No table here.") == ("no_table", [])
    status, scores = article_scores("{{Video game reviews\n| IGN = Great\n}}")
    assert status == "unsupported" and scores[0].status == "needs_review"
    assert len(review_tables("{{video game reviews|GSpot=7/10}} {{Video_game_reviews|IGN=8/10}}")) == 2


# ---------------------------------------------------------------------- article match


def test_a_match_needs_one_item_with_an_article():
    assert candidate_article({}, None).status == "not_found"
    assert candidate_article({"Q1": None}, None).status == "not_found"
    several = candidate_article({"Q1": "Game", "Q2": "Game Online"}, None)
    assert several.status == "needs_review" and "Q1 (Game)" in several.reason and "Q2 (Game Online)" in several.reason
    assert candidate_article({"Q1": "Moon Garden"}, None) == ArticleMatch("verified", "", "Moon Garden", ["Q1"])
    use = CriticArticleDecision("271590", "Moon Garden", "use", "2026-11-19", "a reviewer")
    chosen = candidate_article({"Q1": "X", "Q2": "Y"}, use)
    assert (chosen.status, chosen.title) == ("verified", "Moon Garden") and "a reviewer" in chosen.reason
    reject = CriticArticleDecision("271590", "", "reject", "2026-11-19", "a reviewer")
    assert candidate_article({"Q1": "Moon Garden"}, reject).status == "rejected"


def article(title="Moon Garden", text=ARTICLE, section=None):
    return Article(title, title, section, 123, "2026-11-19T00:00:00Z", text)


def test_the_article_must_describe_this_game_and_edition():
    match = ArticleMatch("verified", "", "Moon Garden", ["Q1"])
    assert check_article(match, article(), "MOON GARDEN™", False).status == "verified"
    assert check_article(match, None, "Moon Garden", False).status == "not_found"
    redirected = check_article(match, article(section="Deluxe Edition"), "Moon Garden", False)
    assert redirected.status == "needs_review" and "section" in redirected.reason
    series = check_article(match, article(text="{{Infobox video game series}} A series."), "Moon Garden", False)
    assert series.status == "needs_review" and "infobox" in series.reason
    edition = check_article(match, article(), "Moon Garden — Remastered", False)
    assert edition.status == "needs_review" and "another edition" in edition.reason
    assert check_article(match, article(section="Deluxe Edition"), "Other", True).status == "verified"  # reviewer
    assert same_title("Half-Life", "Half-Life (video game)") and same_title("God of War", "God of War (2018 video game)")
    assert not same_title("Half-Life 2", "Half-Life (video game)")


def row(status="verified", table_status="ok", title="Moon Garden"):
    return {"status": status, "reason": "why", "article_title": title, "revision_id": 123,
            "revision_at": "2026-11-19T00:00:00Z", "table_status": table_status, "checked_at": "2026-11-19T10:00:00+00:00"}


def score_row(outlet, score, status="ok", kind="critic", label=None):
    return {"outlet": outlet, "score": score, "status": status, "kind": kind, "label": label, "table_title": None,
            "review_url": None}


def test_only_verified_readable_scores_are_shown():
    assert critics_view(None, [], released=False)["state"] == "unreleased"
    assert critics_view(None, [], released=True)["state"] == "not_checked"
    waiting = critics_view(row(status="needs_review"), [score_row("IGN", "9/10")], released=True)
    assert waiting["state"] == "needs_review" and not waiting["critics"]  # never shown before the match is confirmed
    view = critics_view(row(), [score_row("Metacritic", "87/100", kind="aggregate", label="PC"),
                                score_row("IGN", "9/10"), score_row("Famitsu", None, status="needs_review")],
                        released=True)
    assert view["state"] == "ok" and view["hidden"] == 1 and view["review_count"] == 1
    assert [r["outlet"] for r in view["aggregates"]] == ["Metacritic"]
    assert view["article"]["revision_url"] == "https://en.wikipedia.org/w/index.php?title=Moon_Garden&oldid=123"
    assert critics_view(row(table_status="unsupported"), [score_row("IGN", None, status="needs_review")],
                        released=True)["state"] == "unsupported"
    assert critics_view(row(table_status="no_table"), [], released=True)["state"] == "no_table"


# ---------------------------------------------------------------------- Wikimedia client


class FakeWikimedia:
    def __init__(self):
        self.statuses: list[tuple[int, dict]] = []
        self.requests: list[httpx.Request] = []
        self.bindings = [
            {"item": {"value": "http://www.wikidata.org/entity/Q1"}, "value": {"value": "500"}, "kind": {"value": "steam"},
             "article": {"value": "https://en.wikipedia.org/wiki/Moon_Garden"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q2"}, "value": {"value": "600"}, "kind": {"value": "steam"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q3"}, "value": {"value": "700"}, "kind": {"value": "steam"},
             "article": {"value": "https://en.wikipedia.org/wiki/Twin_Game"}},
            {"item": {"value": "http://www.wikidata.org/entity/Q4"}, "value": {"value": "700"}, "kind": {"value": "steam"},
             "article": {"value": "https://en.wikipedia.org/wiki/Twin_Game_Online"}},
        ]
        self.pages = {"Moon Garden": ARTICLE}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            status, headers = self.statuses.pop(0)
            if status == 200:
                return httpx.Response(200, json={"error": {"code": "maxlag"}}, headers=headers)
            return httpx.Response(status, headers=headers)
        params = parse_qs(request.url.query.decode())
        if str(request.url).startswith(SPARQL_API):
            return httpx.Response(200, json={"results": {"bindings": self.bindings}})
        titles = params["titles"][0].split("|")
        pages = [{"title": title, "revisions": [{"revid": 123, "timestamp": "2026-11-19T00:00:00Z",
                                                 "slots": {"main": {"content": self.pages[title]}}}]}
                 if title in self.pages else {"title": title, "missing": True} for title in titles]
        return httpx.Response(200, json={"query": {"pages": pages}})


def make_client(fake, sleeps=None):
    return WikimediaClient("someone@example.invalid", http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                           sleep=(sleeps.append if sleeps is not None else (lambda seconds: None)),
                           clock=lambda: 0.0)


def test_the_client_follows_wikimedias_user_agent_and_pacing_rules():
    with pytest.raises(WikimediaError, match="GAME_LIBRARIAN_CONTACT"):
        WikimediaClient(None)
    assert "(someone@example.invalid;" in user_agent("someone@example.invalid") and "httpx/" in user_agent("x")
    fake = FakeWikimedia()
    sleeps: list[float] = []
    with make_client(fake, sleeps) as client:
        client.articles(["Moon Garden"])
        client.articles(["Moon Garden"])
    first = fake.requests[0]
    assert first.headers["User-Agent"].startswith("GameLibrarian/") and "someone@example.invalid" in first.headers["User-Agent"]
    params = parse_qs(first.url.query.decode())
    assert params["maxlag"] == ["5"] and params["redirects"] == ["1"] and "gzip" in first.headers["Accept-Encoding"]
    assert sleeps == [1.0]  # one request at a time, at least a second apart
    assert repr(client) == "WikimediaClient(contact hidden)"


def test_rate_limits_and_lag_are_waited_out_then_reported():
    fake = FakeWikimedia()
    fake.statuses = [(429, {"Retry-After": "7"}), (200, {"Retry-After": "3"})]  # a 429, then a maxlag answer
    sleeps: list[float] = []
    with make_client(fake, sleeps) as client:
        assert client.articles(["Moon Garden"])["Moon Garden"].revision_id == 123
    assert 7.0 in sleeps and 3.0 in sleeps
    fake.statuses = [(503, {})] * 4
    with make_client(fake) as client, pytest.raises(WikimediaError, match="after 4 attempts"):
        client.articles(["Moon Garden"])
    fake.statuses = [(403, {})]
    with make_client(fake) as client, pytest.raises(WikimediaError, match="HTTP 403"):
        client.articles(["Moon Garden"])


def test_wikidata_items_and_articles_are_read_by_identifier():
    fake = FakeWikimedia()
    with make_client(fake) as client:
        items = client.wikidata_items([500, 600, 700], ["moon-garden", "BAD SLUG"])
        pages = client.articles(["Moon Garden", "Nope"])
    assert items[("steam", "500")] == {"Q1": "Moon Garden"}
    assert items[("steam", "600")] == {"Q2": None}
    assert items[("steam", "700")] == {"Q3": "Twin Game", "Q4": "Twin Game Online"}
    query = parse_qs(fake.requests[0].url.query.decode())["query"][0]
    assert '"500" "600" "700"' in query and "wdt:P1733" in query and '"moon-garden"' in query and "BAD SLUG" not in query
    assert pages["Nope"] is None and pages["Moon Garden"].title == "Moon Garden"


def test_a_refresh_stores_matches_scores_and_review_items(settings):
    fake = FakeWikimedia()
    targets = [CriticTarget("500", "Moon Garden", steam_appid=500), CriticTarget("600", "No Article", steam_appid=600),
               CriticTarget("700", "Twin Game", steam_appid=700), CriticTarget("800", "Unknown", steam_appid=800)]
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    conn = store.initialize(settings.db_path)
    try:
        with make_client(fake) as client:
            report = critics.refresh_critics(conn, client, targets, CuratedData(), log=lambda m: None, now=lambda: now)
        assert report.status() == "success"
        articles = store.critic_article_rows(conn)
        assert {key: row["status"] for key, row in articles.items()} == {
            "500": "verified", "600": "not_found", "700": "needs_review", "800": "not_found"}
        assert articles["500"]["revision_id"] == 123 and articles["500"]["table_status"] == "ok"
        shown = [r for r in store.critic_scores(conn, "500") if r["status"] == "ok"]
        assert {(r["outlet"], r["score"], r["label"]) for r in shown} >= {("Metacritic", "87/100", "PC"), ("IGN", "8/10", "PC")}
        review = critics.review_rows(conn)
        assert {(item["game"], item["what"]) for item in review} == {("700", "article"), ("500", "score: Famitsu")}
        # Nothing is due again until 30 days later; a forced run reads everything.
        assert critics.due_targets(conn, targets, now + timedelta(days=29)) == []
        assert len(critics.due_targets(conn, targets, now + timedelta(days=30))) == 4
        assert len(critics.due_targets(conn, targets, now, force=True)) == 4
        # A failed Wikipedia request keeps what is stored.
        fake.statuses = [(503, {})] * 8
        with make_client(fake) as client:
            failed = critics.refresh_critics(conn, client, targets[:1], CuratedData(), log=lambda m: None,
                                             now=lambda: now + timedelta(days=31))
        assert failed.status() == "failed"
        assert store.critic_article_rows(conn, ["500"])["500"]["checked_at"].startswith("2026-11-19")
    finally:
        conn.close()
