"""Daily Trivia: curated gaming questions, five a day, the same quiz for every visitor on a given day.

The questions live in one file, curated/trivia.json (README "Daily Trivia" explains how to add more). Each question
has a stable id, a category (CATEGORIES), the question, two to four answer choices, the correct answer (one of the
choices, written the same way), a short explanation, one source link, the day it was checked, the game it is about
and, optionally, the IGDB cover record of that game, checked like a Data Story image (stories.parse_image). A game
names its Steam app id only when that Steam app is the same game; the page links the game's details when the app is
in the catalog (web.py). Questions come in quizzes of five.

The schedule (schedule):

- A quiz day is a calendar day in UTC, so the quiz changes for everyone at 00:00 UTC (quiz_day, next_reset).
- From the file's start_day on, each day shows one quiz. A quiz that has never been shown comes first, in file
  order, once its available_from day has come. When every available quiz has been shown, the quiz shown longest ago
  comes back. So the seven initial quizzes repeat in order every seven days, and a quiz added to the end of the file
  is shown from its available_from day on, before the repeats continue.
- The same file and the same day always give the same quiz. A quiz added with an available_from day in the future
  never changes the quiz of today or of an earlier day.

Browse Facts (daily_trivia) lists the questions of the quizzes shown before today, most recently shown first. Today's
five are sent with the page, marked, and app.js shows them in Browse Facts only once the visitor has finished today's
quiz. Questions of quizzes not shown yet are never sent, so browsing never gives away a future quiz.

Invalid quizzes and questions are reported (check-curated) and left out, never guessed. A quiz with an invalid
question is left out whole, so every quiz has five questions. An invalid image record is reported and dropped, and
its question stays with a plain placeholder. A missing file means no trivia, and the popup says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .stories import IGDB_COVER_URL, StoryImage, parse_image

TRIVIA_FILE = "trivia.json"
QUESTIONS_PER_QUIZ = 5
MIN_CHOICES, MAX_CHOICES = 2, 4
# The categories a question can have, as the file writes them, with the label the popup shows.
CATEGORIES = {
    "famous": "Famous games",
    "indie": "Indie games",
    "development": "Development stories",
    "music": "Music",
    "mechanics": "Mechanics",
    "awards": "Awards",
}
RESET_LABEL = "00:00 UTC"  # when a new quiz day starts (quiz_day)
# Length limits keep one question or fact readable in the compact popup.
MAX_QUESTION, MAX_CHOICE, MAX_EXPLANATION = 160, 80, 420
# A stable id, as visitors' browsers save it: lowercase words joined by hyphens, such as "final-fantasy-name".
ITEM_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


# ---------------------------------------------------------------------- the data


@dataclass(frozen=True)
class TriviaSource:
    """The page a question's answer was checked against, with the label the popup shows for its link."""

    title: str
    url: str


@dataclass(frozen=True)
class TriviaQuestion:
    """One multiple-choice question; its explanation doubles as the fact shown in Browse Facts."""

    id: str
    category: str
    question: str
    choices: tuple[str, ...]
    answer: int  # the index of the correct choice
    explanation: str
    source: TriviaSource
    checked_on: date
    game_title: str
    steam_appid: int | None = None  # set only when this Steam app is the same game
    image: StoryImage | None = None  # the game's IGDB cover record, when the file names a checked one

    @property
    def category_label(self) -> str:
        return CATEGORIES[self.category]

    @property
    def answer_text(self) -> str:
        return self.choices[self.answer]

    @property
    def cover_url(self) -> str | None:
        """The cover for a question or fact card (IGDB's t_cover_big, 264 x 374 px), or None for a placeholder."""
        return IGDB_COVER_URL.format(size="t_cover_big", image_id=self.image.image_id) if self.image else None

    @property
    def thumb_url(self) -> str | None:
        """A small cover for the list of the quiz's games (t_cover_small, 90 x 128 px), or None."""
        return IGDB_COVER_URL.format(size="t_cover_small", image_id=self.image.image_id) if self.image else None


@dataclass(frozen=True)
class TriviaQuiz:
    """Five questions shown together on a day, never before `available_from`."""

    id: str
    available_from: date
    questions: tuple[TriviaQuestion, ...]


@dataclass
class TriviaCollection:
    """The valid quizzes in file order, the first day of the schedule, and the problems found in the file."""

    start_day: date | None
    quizzes: list[TriviaQuiz]
    problems: list[str]

    @property
    def questions(self) -> list[TriviaQuestion]:
        return [question for quiz in self.quizzes for question in quiz.questions]


@dataclass(frozen=True)
class Showing:
    """The quiz of one day: its place in the file (1 is the first quiz) and the day it was first shown."""

    day: date
    quiz: TriviaQuiz
    number: int
    first_shown: date

    @property
    def repeat(self) -> bool:
        return self.first_shown < self.day


@dataclass(frozen=True)
class DailyTrivia:
    """Today's quiz, the facts open for browsing before it is finished, and when the next quiz starts."""

    showing: Showing
    rotation_size: int  # the quizzes available today, new or repeated
    past_facts: list[TriviaQuestion]  # the questions of the quizzes shown before today, most recently shown first
    next_reset: datetime


# ---------------------------------------------------------------------- the day and the reset


def quiz_day(moment: datetime) -> date:
    """The quiz day of a moment: its calendar date in UTC (a moment without a time zone counts as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


def next_reset(day: date) -> datetime:
    """When the quiz after `day` starts: 00:00 UTC on the next day."""
    return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=timezone.utc)


# ---------------------------------------------------------------------- loading and validation


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _day(value) -> date | None:
    """A YYYY-MM-DD date, or None."""
    try:
        return date.fromisoformat(_text(value))
    except ValueError:
        return None


def _source(value, errors: list[str]) -> TriviaSource | None:
    if not isinstance(value, dict):
        errors.append("source must be an object with title and url")
        return None
    title, url = _text(value.get("title")), _text(value.get("url"))
    if not title:
        errors.append("source title is empty")
    if not url.startswith("https://") or any(character.isspace() for character in url):
        errors.append(f"source url must be an https:// link, got {url!r}")
    return TriviaSource(title, url) if title and url.startswith("https://") else None


def _choices(value, errors: list[str]) -> tuple[str, ...]:
    """The answer choices, in the file's order; an invalid list is reported and becomes ()."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        errors.append("choices must be a list of non-empty texts")
        return ()
    choices = tuple(item.strip() for item in value)
    if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
        errors.append(f"choices must have {MIN_CHOICES} to {MAX_CHOICES} items, got {len(choices)}")
    if len({choice.casefold() for choice in choices}) != len(choices):
        errors.append("choices must all differ")
    if any(len(choice) > MAX_CHOICE for choice in choices):
        errors.append(f"each choice must be at most {MAX_CHOICE} characters")
    return choices


def _question(value, problems: list[str], where: str) -> TriviaQuestion | None:
    """One validated question, or None (with the reasons added to `problems`)."""
    if not isinstance(value, dict):
        problems.append(f"{where}: a question must be an object")
        return None
    errors: list[str] = []
    question_id = _text(value.get("id"))
    if not ITEM_ID.fullmatch(question_id):
        errors.append(f"id must be lowercase words joined by hyphens, got {question_id!r}")
    category = _text(value.get("category"))
    if category not in CATEGORIES:
        errors.append(f"category must be one of {list(CATEGORIES)}, got {category!r}")
    question = _text(value.get("question"))
    if not question or len(question) > MAX_QUESTION:
        errors.append(f"question must be 1 to {MAX_QUESTION} characters")
    choices = _choices(value.get("choices"), errors)
    answer = _text(value.get("answer"))
    if answer not in choices:
        errors.append("answer must be one of the choices, written exactly the same way")
    explanation = _text(value.get("explanation"))
    if not explanation or len(explanation) > MAX_EXPLANATION:
        errors.append(f"explanation must be 1 to {MAX_EXPLANATION} characters")
    source = _source(value.get("source"), errors)
    checked_on = _day(value.get("checked_on"))
    if checked_on is None:
        errors.append("checked_on must be a YYYY-MM-DD date")
    game = value.get("game")
    game_title = _text(game.get("title")) if isinstance(game, dict) else ""
    steam_appid = game.get("steam_appid") if isinstance(game, dict) else None
    if not game_title:
        errors.append("game must be an object with a title")
    if steam_appid is not None and (not isinstance(steam_appid, int) or isinstance(steam_appid, bool)
                                    or steam_appid <= 0):
        errors.append("game steam_appid must be a Steam app id or null")
    label = f"{where} ({question_id or 'no id'})"
    image = parse_image(value.get("image"), problems, label)
    if errors:
        problems.append(f"{label}: {'; '.join(errors)}")
        return None
    return TriviaQuestion(id=question_id, category=category, question=question, choices=choices,
                          answer=choices.index(answer), explanation=explanation, source=source,
                          checked_on=checked_on, game_title=game_title, steam_appid=steam_appid, image=image)


def _quiz(value, problems: list[str], where: str, start_day: date, question_ids: set[str]) -> TriviaQuiz | None:
    """One validated quiz of exactly five valid questions, or None. `question_ids` collects every question id in the
    file, so a duplicate is caught even in a quiz that is left out."""
    if not isinstance(value, dict):
        problems.append(f"{where}: a quiz must be an object")
        return None
    errors: list[str] = []
    quiz_id = _text(value.get("id"))
    if not ITEM_ID.fullmatch(quiz_id):
        errors.append(f"id must be lowercase words joined by hyphens, got {quiz_id!r}")
    available_from = _day(value.get("available_from"))
    if available_from is None:
        errors.append("available_from must be a YYYY-MM-DD date")
    elif available_from < start_day:
        errors.append("available_from cannot be before start_day")
    items = value.get("questions")
    if not isinstance(items, list) or len(items) != QUESTIONS_PER_QUIZ:
        errors.append(f"questions must be a list of exactly {QUESTIONS_PER_QUIZ}")
        items = items if isinstance(items, list) else []
    label = f"{where} ({quiz_id or 'no id'})"
    questions = []
    for number, item in enumerate(items, start=1):
        question = _question(item, problems, f"{label}, question {number}")
        raw_id = _text(item.get("id")) if isinstance(item, dict) else ""
        if raw_id in question_ids:
            errors.append(f"question {number}: duplicate id {raw_id!r} (ids must be unique in the file)")
        elif raw_id:
            question_ids.add(raw_id)
        questions.append(question)
    if any(question is None for question in questions):
        errors.append("a question is invalid")
    if errors:
        problems.append(f"{label}: quiz left out: {'; '.join(errors)}")
        return None
    return TriviaQuiz(id=quiz_id, available_from=available_from, questions=tuple(questions))


def load_trivia(curated_dir: Path) -> TriviaCollection:
    """Load and validate curated/trivia.json. A missing file means no trivia and no problem."""
    path = curated_dir / TRIVIA_FILE
    if not path.is_file():
        return TriviaCollection(start_day=None, quizzes=[], problems=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return TriviaCollection(None, [], [f"{TRIVIA_FILE}: cannot be read as JSON ({exc})"])
    if not isinstance(data, dict):
        return TriviaCollection(None, [], [f"{TRIVIA_FILE}: the file must hold one JSON object"])
    start_day = _day(data.get("start_day"))
    if start_day is None:
        return TriviaCollection(None, [], [f"{TRIVIA_FILE}: start_day must be a YYYY-MM-DD date"])
    items = data.get("quizzes")
    if not isinstance(items, list) or not items:
        return TriviaCollection(start_day, [], [f"{TRIVIA_FILE}: quizzes must be a non-empty list"])
    problems: list[str] = []
    quizzes: list[TriviaQuiz] = []
    quiz_ids: set[str] = set()
    question_ids: set[str] = set()
    for number, item in enumerate(items, start=1):
        quiz = _quiz(item, problems, f"{TRIVIA_FILE}: quiz {number}", start_day, question_ids)
        if quiz is None:
            continue
        if quiz.id in quiz_ids:
            problems.append(f"{TRIVIA_FILE}: quiz {number}: duplicate quiz id {quiz.id!r}; quiz left out")
            continue
        quiz_ids.add(quiz.id)
        quizzes.append(quiz)
    if quizzes and min(quiz.available_from for quiz in quizzes) > start_day:
        problems.append(f"{TRIVIA_FILE}: no quiz is available on start_day {start_day}; "
                        "the earliest quiz is shown until one is")
    return TriviaCollection(start_day, quizzes, problems)


# ---------------------------------------------------------------------- the schedule


def schedule(collection: TriviaCollection, last_day: date) -> list[Showing]:
    """The quiz of every day from start_day to last_day, oldest first (none before start_day).

    Each day: the first quiz in file order that has never been shown and whose available_from day has come; when
    there is none, the quiz shown longest ago. Days are counted one by one, so the result depends only on the file.
    """
    quizzes = collection.quizzes
    if collection.start_day is None or not quizzes:
        return []
    waiting = list(range(len(quizzes)))  # never shown yet, in file order
    rotation: list[int] = []  # shown at least once, the one shown longest ago first
    first_shown: dict[int, date] = {}
    showings = []
    day = collection.start_day
    while day <= last_day:
        ready = [index for index in waiting if quizzes[index].available_from <= day]
        if ready:
            pick = ready[0]
        elif rotation:
            pick = rotation[0]
        else:  # nothing is available yet (check-curated reports it): the earliest quiz, so no day is empty
            pick = min(waiting, key=lambda index: (quizzes[index].available_from, index))
        if pick in waiting:
            waiting.remove(pick)
            first_shown[pick] = day
        else:
            rotation.remove(pick)
        rotation.append(pick)
        showings.append(Showing(day=day, quiz=quizzes[pick], number=pick + 1, first_shown=first_shown[pick]))
        day += timedelta(days=1)
    return showings


def daily_trivia(collection: TriviaCollection, day: date) -> DailyTrivia | None:
    """The trivia of a quiz day, or None when the file has no valid quiz.

    A day before start_day shows the first day's quiz. Browse Facts gets the questions of every quiz shown before
    `day`, most recently shown first, each once, never today's (the page adds those after the quiz is finished).
    """
    if collection.start_day is None or not collection.quizzes:
        return None
    last_day = max(day, collection.start_day)
    showings = schedule(collection, last_day)
    today = replace(showings[-1], day=day)
    listed, past_facts = {today.quiz.id}, []
    for showing in reversed(showings[:-1]):
        if showing.quiz.id not in listed:
            listed.add(showing.quiz.id)
            past_facts.extend(showing.quiz.questions)
    rotation_size = sum(1 for quiz in collection.quizzes if quiz.available_from <= last_day)
    return DailyTrivia(showing=today, rotation_size=rotation_size, past_facts=past_facts, next_reset=next_reset(day))
