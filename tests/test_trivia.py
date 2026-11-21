"""Daily Trivia (version 0.17): the curated questions and their checks, the daily schedule and its UTC reset, the facts
open for browsing, the popup's content (/trivia/panel), the header's star and lightbulb, and check-curated.

The visitor's side (progress, first scores, practice, personal best, broken storage) is tested in
tests/js/trivia.test.mjs; the popup's behavior in a browser (keyboard, focus, resuming, details on top) is described
in the README's "Verification status"."""

import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

import game_librarian.__main__ as cli
from game_librarian import trivia
from game_librarian.config import DEFAULT_CURATED_DIR, PACKAGE_DIR
from game_librarian.web import create_app
from tests.test_web import TODAY, games, text_of  # noqa: F401

SHIPPED = trivia.load_trivia(DEFAULT_CURATED_DIR)
START = date(2026, 11, 21)
APP_JS = (PACKAGE_DIR / "static" / "app.js").read_text(encoding="utf-8")
CSS = (PACKAGE_DIR / "static" / "style.css").read_text(encoding="utf-8")
COVER = {"source": "IGDB", "kind": "cover", "igdb_id": 980, "image_id": "co2lb0",
         "igdb_url": "https://www.igdb.com/games/grand-theft-auto", "checked_on": "2026-11-21"}


def question(qid: str, **fields) -> dict:
    """A valid question; keyword arguments replace its fields."""
    item = {"id": qid, "category": "famous", "question": f"Question {qid}?", "choices": ["Yes", "No", "Maybe"],
            "answer": "Yes", "explanation": f"The fact behind {qid}.", "checked_on": "2026-11-21",
            "source": {"title": f"Source of {qid}", "url": f"https://example.org/{qid}"},
            "game": {"title": f"Game {qid}", "steam_appid": None}}
    item.update(fields)
    return item


def quiz(qid: str, available_from: str = "2026-11-21", questions=None) -> dict:
    """A valid quiz of five questions named after the quiz."""
    return {"id": qid, "available_from": available_from,
            "questions": questions if questions is not None else [question(f"{qid}-q{n}") for n in range(1, 6)]}


def write_trivia(folder, quizzes, start_day: str = "2026-11-21") -> None:
    (folder / trivia.TRIVIA_FILE).write_text(json.dumps({"start_day": start_day, "quizzes": quizzes}),
                                             encoding="utf-8")


def numbers(collection, first: date, last: date) -> list[int]:
    """The quiz number shown on each day from `first` to `last`."""
    return [showing.number for showing in trivia.schedule(collection, last) if showing.day >= first]


def site(settings, moment: datetime) -> TestClient:
    return TestClient(create_app(settings, today=lambda: TODAY, now=lambda: moment))


# ---------------------------------------------------------------------- the shipped questions


def test_the_shipped_trivia_is_one_week_of_valid_sourced_questions():
    assert SHIPPED.problems == []
    assert SHIPPED.start_day == START
    assert len(SHIPPED.quizzes) == 7 and all(len(item.questions) == 5 for item in SHIPPED.quizzes)
    questions = SHIPPED.questions
    assert len(questions) == 35 and len({item.id for item in questions}) == 35  # stable ids, all different
    assert {item.category for item in questions} == set(trivia.CATEGORIES)
    for item in SHIPPED.quizzes:  # a mix every day: five different categories
        assert len({entry.category for entry in item.questions}) == 5, item.id
    for item in questions:
        assert item.source.url.startswith("https://") and item.source.title, item.id
        assert len(item.choices) == 4 and item.choices[item.answer] == item.answer_text, item.id
        assert item.image and item.image.igdb_url.startswith("https://www.igdb.com/games/"), item.id
        assert item.checked_on == START
    # Wikipedia is cited by exact revision, the way Data Stories cite it.
    wikipedia = [item.source.url for item in questions if "wikipedia.org" in item.source.url]
    assert len(wikipedia) == 31 and all(re.search(r"/w/index\.php\?title=[^&]+&oldid=\d+$", url) for url in wikipedia)
    positions = [item.answer for item in questions]  # the right answer is not always in the same place
    assert all(positions.count(place) >= 8 for place in range(4))


def test_the_final_fantasy_question_follows_sakaguchis_own_explanation():
    item = next(entry for entry in SHIPPED.questions if entry.id == "final-fantasy-name")
    assert item.answer_text == "A name that shortens to “FF”"
    assert item.source.url == "https://www.youtube.com/watch?v=HgngEQOFcWs"
    assert "Fighting Fantasy" in item.explanation and "trademark" in item.explanation
    # The "last game" story is not presented as the reason, nor declared false: it is not the reason he gives.
    assert "is not the reason he gives" in item.explanation
    assert SHIPPED.quizzes[0].questions[0] is item  # the first question of the first quiz


def test_catalog_games_are_named_by_their_steam_app_only():
    by_title = {}
    for item in SHIPPED.questions:
        by_title.setdefault(item.game_title, set()).add(item.steam_appid)
    assert all(len(appids) == 1 for appids in by_title.values())  # one game, one identifier
    assert by_title["Hades"] == {1145360} and by_title["Portal"] == {400}
    assert by_title["Final Fantasy"] == {None}  # the 1987 game is not on Steam (the Pixel Remaster is another app)


# ---------------------------------------------------------------------- checks of the file


def test_a_missing_file_means_no_trivia_and_no_problem(tmp_path):
    collection = trivia.load_trivia(tmp_path)
    assert collection.quizzes == [] and collection.problems == []
    assert trivia.daily_trivia(collection, START) is None


def test_invalid_questions_and_quizzes_are_reported_and_left_out_never_guessed(tmp_path):
    write_trivia(tmp_path, [
        quiz("good"),
        quiz("bad-answer", questions=[question("a1", answer="Perhaps")] + [question(f"a{n}") for n in range(2, 6)]),
        quiz("bad-url", questions=[question("u1", source={"title": "X", "url": "javascript:alert(1)"})]
             + [question(f"u{n}") for n in range(2, 6)]),
        quiz("bad-category", questions=[question("c1", category="trivia")] + [question(f"c{n}") for n in range(2, 6)]),
        quiz("too-many-choices", questions=[question("m1", choices=["A", "B", "C", "D", "E"], answer="A")]
             + [question(f"m{n}") for n in range(2, 6)]),
        quiz("four-questions", questions=[question(f"f{n}") for n in range(1, 5)]),
        quiz("early", available_from="2026-11-20"),
        quiz("duplicate-id", questions=[question("good-q1")] + [question(f"d{n}") for n in range(2, 6)]),
        quiz("good", questions=[question(f"g{n}") for n in range(1, 6)]),  # the same quiz id again
    ])
    collection = trivia.load_trivia(tmp_path)
    assert [item.id for item in collection.quizzes] == ["good"]  # every other quiz is left out whole
    problems = "\n".join(collection.problems)
    for text in ("answer must be one of the choices", "source url must be an https:// link",
                 "category must be one of", "choices must have 2 to 4 items", "questions must be a list of exactly 5",
                 "available_from cannot be before start_day", "duplicate id 'good-q1'", "duplicate quiz id 'good'"):
        assert text in problems, text


def test_an_invalid_cover_is_dropped_but_its_question_stays(tmp_path):
    questions = [question("q1", image=COVER), question("q2", image={**COVER, "image_id": "../x.png"}),
                 question("q3"), question("q4"), question("q5")]
    write_trivia(tmp_path, [quiz("only", questions=questions)])
    collection = trivia.load_trivia(tmp_path)
    kept = collection.quizzes[0].questions
    assert [item.id for item in kept] == ["q1", "q2", "q3", "q4", "q5"]
    assert kept[0].cover_url == "https://images.igdb.com/igdb/image/upload/t_cover_big/co2lb0.jpg"
    assert kept[1].image is None and kept[1].cover_url is None
    assert "image left out: image_id must be IGDB's image id" in "\n".join(collection.problems)


def test_an_unreadable_file_is_reported(tmp_path):
    (tmp_path / trivia.TRIVIA_FILE).write_text("{not json", encoding="utf-8")
    assert "cannot be read as JSON" in trivia.load_trivia(tmp_path).problems[0]
    write_trivia(tmp_path, [quiz("only")], start_day="soon")
    assert trivia.load_trivia(tmp_path).problems == ["trivia.json: start_day must be a YYYY-MM-DD date"]


# ---------------------------------------------------------------------- the day, the reset and the schedule


def test_a_quiz_day_is_the_utc_date_so_the_quiz_changes_at_midnight_utc():
    assert trivia.quiz_day(datetime(2026, 11, 21, 23, 59, 59, tzinfo=timezone.utc)) == date(2026, 11, 21)
    assert trivia.quiz_day(datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)) == date(2026, 11, 22)
    central = timezone(timedelta(hours=-6))  # 19:30 on 21 Nov in Chicago is 01:30 on 22 Nov in UTC
    assert trivia.quiz_day(datetime(2026, 11, 21, 19, 30, tzinfo=central)) == date(2026, 11, 22)
    assert trivia.quiz_day(datetime(2026, 11, 21, 23, 0)) == date(2026, 11, 21)  # no time zone: UTC
    assert trivia.next_reset(date(2026, 11, 21)) == datetime(2026, 11, 22, tzinfo=timezone.utc)


def test_the_seven_quizzes_run_in_order_then_repeat_and_say_so():
    week = trivia.schedule(SHIPPED, START + timedelta(days=15))
    assert [showing.number for showing in week] == [1, 2, 3, 4, 5, 6, 7, 1, 2, 3, 4, 5, 6, 7, 1, 2]
    assert [showing.repeat for showing in week[:8]] == [False] * 7 + [True]
    assert week[7].first_shown == START and week[14].first_shown == START  # the repeat names its first day
    # The same file and day always give the same quiz, whenever the file is read.
    again = trivia.daily_trivia(trivia.load_trivia(DEFAULT_CURATED_DIR), START + timedelta(days=9))
    assert again.showing.quiz.id == "quiz-03" and again.showing.repeat and again.rotation_size == 7


def test_a_day_before_the_start_shows_the_first_quiz():
    early = trivia.daily_trivia(SHIPPED, START - timedelta(days=3))
    assert early.showing.number == 1 and not early.showing.repeat and early.past_facts == []


def test_a_quiz_added_later_comes_first_on_its_day_and_never_changes_an_earlier_day(tmp_path):
    write_trivia(tmp_path, [quiz("one"), quiz("two")])
    before = trivia.load_trivia(tmp_path)
    write_trivia(tmp_path, [quiz("one"), quiz("two"), quiz("three", available_from="2026-11-26")])
    after = trivia.load_trivia(tmp_path)
    last = START + timedelta(days=9)
    assert numbers(before, START, last) == [1, 2, 1, 2, 1, 2, 1, 2, 1, 2]
    # Days 0-4 are unchanged; on 26 Nov the new quiz comes first, then the quiz shown longest ago returns.
    assert numbers(after, START, last) == [1, 2, 1, 2, 1, 3, 2, 1, 3, 2]
    added = trivia.daily_trivia(after, date(2026, 11, 26))
    assert added.showing.quiz.id == "three" and not added.showing.repeat and added.rotation_size == 3
    assert trivia.daily_trivia(after, date(2026, 11, 25)).rotation_size == 2


def test_when_no_quiz_is_available_on_the_first_day_the_earliest_is_shown_and_reported(tmp_path):
    write_trivia(tmp_path, [quiz("late", available_from="2026-11-25"), quiz("later", available_from="2026-11-27")])
    collection = trivia.load_trivia(tmp_path)
    assert "no quiz is available on start_day" in collection.problems[0]
    assert trivia.daily_trivia(collection, START).showing.quiz.id == "late"  # never an empty day


# ---------------------------------------------------------------------- browse facts


def test_browse_facts_hold_earlier_quizzes_newest_first_and_never_today_or_later():
    assert trivia.daily_trivia(SHIPPED, START).past_facts == []
    third = trivia.daily_trivia(SHIPPED, START + timedelta(days=2))
    ids = [item.id for item in third.past_facts]
    second, first = SHIPPED.quizzes[1].questions, SHIPPED.quizzes[0].questions
    assert ids == [item.id for item in second] + [item.id for item in first]
    assert not {item.id for item in third.showing.quiz.questions} & set(ids)
    repeat = trivia.daily_trivia(SHIPPED, START + timedelta(days=7))  # quiz 1 again: the other six can be browsed
    assert len(repeat.past_facts) == 30
    assert not {item.id for item in SHIPPED.quizzes[0].questions} & {item.id for item in repeat.past_facts}


# ---------------------------------------------------------------------- the popup's content


def shipped_site(settings, moment: datetime) -> TestClient:
    shutil.copy(DEFAULT_CURATED_DIR / trivia.TRIVIA_FILE, settings.curated_dir / trivia.TRIVIA_FILE)
    return site(settings, moment)


def test_the_panel_shows_todays_five_questions_with_their_answers_sources_and_covers(settings, games):
    html = shipped_site(settings, datetime(2026, 11, 21, 23, 59, tzinfo=timezone.utc)).get("/trivia/panel").text
    root = re.search(r'<div class="trivia" [^>]+>', html).group(0)
    first = SHIPPED.quizzes[0].questions
    assert 'data-day="2026-11-21" data-quiz="quiz-01"' in root
    assert f'data-ids="{" ".join(item.id for item in first)}"' in root
    assert f'data-answers="{" ".join(str(item.answer) for item in first)}"' in root
    assert 'data-next-reset="2026-11-22T00:00:00+00:00"' in root
    assert html.count('class="trivia-card" data-trivia-question=') == 5 and html.count('class="trivia-choice"') == 20
    assert "21 Nov 2026 · Quiz 1 of 7 · New today" in text_of(html)
    assert 'role="tablist"' in html and 'aria-selected="true" data-trivia-tab="quiz">Today’s Quiz' in html
    assert 'href="https://www.youtube.com/watch?v=HgngEQOFcWs" target="_blank" rel="noopener"' in html
    assert 'src="https://images.igdb.com/igdb/image/upload/t_cover_big/co2xv8.jpg" alt="Cover art of Final Fantasy"' \
        in html
    assert "Saved on this device" in html and "New quiz every day at 00:00 UTC" in html
    assert "data-details=" not in html  # none of the shipped games is in this test catalog
    assert text_of(html).count("Not in the catalog") == 5


def test_the_next_utc_day_brings_the_next_quiz_and_a_repeat_names_its_first_day(settings, games):
    client = shipped_site(settings, datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc))
    assert 'data-day="2026-11-22" data-quiz="quiz-02"' in client.get("/trivia/panel").text
    repeat = shipped_site(settings, datetime(2026, 11, 28, 12, tzinfo=timezone.utc)).get("/trivia/panel").text
    assert "28 Nov 2026 · Quiz 1 of 7 · Repeat, first shown 21 Nov 2026" in text_of(repeat)
    assert "Once all 7 have been shown, they repeat in the same order" in text_of(repeat)


def test_browse_facts_send_earlier_facts_and_todays_marked_but_never_a_future_quiz(settings, games):
    html = shipped_site(settings, datetime(2026, 11, 23, 9, tzinfo=timezone.utc)).get("/trivia/panel").text
    facts = re.findall(r'<article class="trivia-card trivia-fact" data-trivia-fact="([^"]+)"[^>]*?( data-today)?'
                       r' aria-labelledby', html)
    today = [item.id for item in SHIPPED.quizzes[2].questions]
    assert [fact for fact, marked in facts if marked] == today  # shown only after today's quiz (app.js)
    assert len(facts) == 15  # today's five, then quiz 2 and quiz 1
    future = {item.id for item in SHIPPED.quizzes[3].questions}
    assert not future & {fact for fact, _ in facts}
    assert "No facts to browse yet." in html and 'data-trivia-empty hidden' in html  # the empty state is ready


def test_a_catalog_game_opens_its_details_and_other_games_do_not(settings, games):
    first = question("in-catalog", game={"title": "Game 01", "steam_appid": 101}, image=COVER)
    second = question("outside", game={"title": "Elsewhere", "steam_appid": 999999})
    third = question("escaped", question="Is <script>alert(1)</script> text?", choices=["<b>A</b>", "B"],
                     answer="<b>A</b>")
    write_trivia(settings.curated_dir, [quiz("one", questions=[first, second, third, question("q4"),
                                                                question("q5")])])
    html = site(settings, datetime(2026, 11, 21, 12, tzinfo=timezone.utc)).get("/trivia/panel").text
    links = re.findall(r'<a class="button trivia-details" href="(/games/\d+)" data-details="(\d+)">', html)
    assert set(links) == {("/games/101", "101")} and len(links) == 3  # the answer, the results list, the fact
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;A&lt;/b&gt;" in html
    # A question without a cover gets a plain box, credited to nobody.
    outside = html[html.index('id="trivia-q-outside"') - 600:html.index('id="trivia-q-outside"')]
    assert 'class="trivia-art is-empty"><span class="visually-hidden">No cover art</span>' in outside


def test_without_a_trivia_file_the_popup_says_so(settings, games):
    html = site(settings, datetime(2026, 11, 21, 12, tzinfo=timezone.utc)).get("/trivia/panel").text
    assert "Daily Trivia has no questions yet." in text_of(html) and "check-curated" in html


# ---------------------------------------------------------------------- the header and the page


def test_every_page_ends_the_header_with_the_wishlist_star_and_the_trivia_lightbulb(settings, games):
    client = shipped_site(settings, datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
    for path in ("/", "/trending", "/stories", "/dashboard", "/dashboard/reception", "/wishlist", "/games/101"):
        page = client.get(path).text
        nav = re.search(r'<nav class="site-nav".*?</nav>', page, re.S).group(0)
        controls = re.findall(r'<(a|button) class="(nav-[a-z -]+)"', nav)
        assert controls[-2:] == [("a", "nav-icon"), ("button", "nav-icon nav-trivia")], path  # the trivia last
        assert re.search(r'<a class="nav-icon" href="/wishlist"( aria-current="page")? aria-label="Wishlist">', nav)
        assert '<span class="nav-tip" aria-hidden="true">Wishlist</span>' in nav
        assert ('aria-label="Daily Trivia" aria-haspopup="dialog" aria-controls="trivia-dialog" data-trivia-open '
                'data-trivia-day="2026-11-21"') in nav
        assert '<span class="nav-tip" aria-hidden="true">Daily Trivia</span>' in nav
        assert ">Wishlist</a>" not in nav  # no text tab any more
        assert '<dialog class="trivia-dialog" id="trivia-dialog" aria-labelledby="trivia-title">' in page
        has_details_dialog = '<dialog class="game-dialog" id="game-dialog"' in page
        assert has_details_dialog == (path != "/games/101"), path  # the full details page keeps its own links
    assert 'href="/wishlist" aria-current="page" aria-label="Wishlist"' in client.get("/wishlist").text
    home = client.get("/").text
    assert home.index("/static/trivia.js") < home.index("/static/app.js")
    assert "<noscript><style>.nav-trivia { display: none; }</style></noscript>" in home  # it needs JavaScript


def test_the_icons_are_gold_bordered_squares_with_tips_and_the_popup_returns_focus():
    icon = re.search(r"\n\.nav-icon \{([^}]*)\}", CSS).group(1)
    assert "border: 1px solid var(--gold);" in icon and "background: var(--bg);" in icon
    assert "width: 36px;" in icon and "height: 36px;" in icon
    assert ".nav-icon:hover .nav-tip, .nav-icon:focus-visible .nav-tip { visibility: visible;" in CSS
    assert ".nav-icon.is-tip-hidden .nav-tip { visibility: hidden;" in CSS  # Escape hides a tip
    assert "html.has-dialog, html.has-trivia { overflow: hidden; }" in CSS  # the page keeps its place
    for anchor in ("button.focus(); // every way of closing", 'button.setAttribute("aria-describedby", '
                   '"trivia-done-note")', 'return done || !card.hasAttribute("data-today");',
                   'event.key === "Escape"', "(factIndex(list) + delta + list.length) % list.length"):
        assert anchor in APP_JS, anchor


# ---------------------------------------------------------------------- check-curated


def test_check_curated_lists_the_trivia_and_its_problems(settings, capsys, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    shutil.copy(DEFAULT_CURATED_DIR / trivia.TRIVIA_FILE, settings.curated_dir / trivia.TRIVIA_FILE)
    cli.main(["check-curated"])
    assert ("trivia.json: 7 quizzes (35 questions), one a day from 2026-11-21, new quiz at 00:00 UTC"
            in capsys.readouterr().out)
    write_trivia(settings.curated_dir, [quiz("one", questions=[question("q1", answer="Nope")])])
    assert cli.main(["check-curated"]) == 1
    assert "answer must be one of the choices" in capsys.readouterr().out
