// Daily Trivia storage (game_librarian/static/trivia.js), tested with Node's built-in runner: node --test tests/js
// A fake storage object stands in for the browser's localStorage, including the ways it can fail. A new fake made
// from the same data is a page reload.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const Trivia = require("../../game_librarian/static/trivia.js");

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => { data[key] = String(value); },
    removeItem: (key) => { delete data[key]; },
  };
}

// A quiz as app.js reads it from the panel: the day, its id, the question ids and each correct choice.
function quiz(day = "2026-11-21", id = "quiz-01") {
  return { day, id, ids: ["q-one", "q-two", "q-three", "q-four", "q-five"], answers: [1, 2, 1, 0, 3] };
}

// Answer every remaining question of the attempt with `choices`, moving on after each one, as the popup does.
function play(state, today, choices) {
  for (const choice of choices) {
    Trivia.choose(state, today, choice);
    Trivia.advance(state, today);
  }
}

test("a quiz is answered one question at a time, one point per correct answer", () => {
  const state = Trivia.read(fakeStorage());
  const today = quiz();
  assert.equal(Trivia.current(state, today).position, 0);
  assert.deepEqual(Trivia.choose(state, today, 1), { correct: true, finished: false, first: false });
  assert.equal(Trivia.choose(state, today, 0), null); // the question on screen is already answered
  assert.equal(Trivia.advance(state, today), 1);
  assert.equal(Trivia.advance(state, today), 1); // cannot skip an unanswered question
  assert.deepEqual(Trivia.choose(state, today, 0), { correct: false, finished: false, first: false });
  assert.equal(Trivia.choose(state, today, 7.5), null); // only whole choice numbers
  Trivia.advance(state, today);
  play(state, today, [1, 0]);
  const last = Trivia.choose(state, today, 3);
  assert.deepEqual(last, { correct: true, finished: true, first: true });
  assert.equal(Trivia.score(state.attempt, today), 4);
  assert.deepEqual(Trivia.firstScore(state, "2026-11-21"), { quiz: "quiz-01", score: 4, total: 5 });
  assert.equal(Trivia.advance(state, today), 5); // the results
  assert.equal(Trivia.advance(state, today), 5);
});

test("closing the popup or reloading the page resumes the attempt where it was", () => {
  const storage = fakeStorage();
  const today = quiz();
  let state = Trivia.read(storage);
  Trivia.choose(state, today, 1);
  Trivia.advance(state, today);
  Trivia.choose(state, today, 0); // answered, but Next not pressed yet
  state = Trivia.save(storage, state);
  const reloaded = Trivia.read(fakeStorage(storage.data));
  const attempt = Trivia.current(reloaded, today);
  assert.deepEqual(attempt.answers, [1, 0, null, null, null]);
  assert.equal(attempt.position, 1); // the second question again, with its answer and explanation
  assert.equal(attempt.practice, false);
});

test("the first finished score of the day is kept; a replay is practice and never replaces it", () => {
  const storage = fakeStorage();
  const today = quiz();
  let state = Trivia.read(storage);
  play(state, today, [0, 0, 0, 0, 0]); // 1 of 5
  state = Trivia.save(storage, state);
  assert.equal(Trivia.firstScore(state, today.day).score, 1);
  const practice = Trivia.replay(state, today);
  assert.equal(practice.practice, true);
  assert.deepEqual(practice.answers, [null, null, null, null, null]);
  play(state, today, [1, 2, 1, 0]);
  assert.deepEqual(Trivia.choose(state, today, 3), { correct: true, finished: true, first: false });
  state = Trivia.save(storage, state);
  assert.equal(Trivia.score(state.attempt, today), 5); // the practice score is shown...
  assert.equal(Trivia.firstScore(Trivia.read(fakeStorage(storage.data)), today.day).score, 1); // ...not saved
});

test("the personal best is the best first score, from the earliest day it was reached", () => {
  const storage = fakeStorage();
  let state = Trivia.read(storage);
  play(state, quiz("2026-11-21"), [1, 2, 0, 0, 0]); // 3
  play(state, quiz("2026-11-22", "quiz-02"), [1, 2, 1, 0, 0]); // 4
  play(state, quiz("2026-11-23", "quiz-03"), [1, 2, 1, 1, 0]); // 3
  play(state, quiz("2026-11-24", "quiz-04"), [1, 2, 1, 0, 1]); // 4 again: the earlier day stays
  Trivia.replay(state, quiz("2026-11-24", "quiz-04"));
  play(state, quiz("2026-11-24", "quiz-04"), [1, 2, 1, 0, 3]); // a practice 5 does not count
  state = Trivia.save(storage, state);
  assert.deepEqual(Trivia.best(state), { day: "2026-11-22", score: 4, total: 5 });
  assert.equal(Trivia.best(Trivia.read(fakeStorage())), null);
});

test("a new day starts a new quiz; yesterday's unfinished attempt is dropped", () => {
  const storage = fakeStorage();
  let state = Trivia.read(storage);
  Trivia.choose(state, quiz("2026-11-21"), 1);
  state = Trivia.save(storage, state);
  const tomorrow = quiz("2026-11-22", "quiz-02");
  const attempt = Trivia.current(Trivia.read(fakeStorage(storage.data)), tomorrow);
  assert.equal(attempt.day, "2026-11-22");
  assert.deepEqual(attempt.answers, [null, null, null, null, null]);
  assert.equal(attempt.practice, false);
});

test("a repeated quiz on a later day counts as that day's quiz and remembers the earlier play", () => {
  const state = Trivia.read(fakeStorage());
  play(state, quiz("2026-11-21", "quiz-01"), [1, 2, 1, 0, 0]); // 4
  const repeat = quiz("2026-11-28", "quiz-01"); // the same quiz and questions, a week later
  const attempt = Trivia.current(state, repeat);
  assert.equal(attempt.day, "2026-11-28"); // a fresh attempt, not last week's finished one
  assert.deepEqual(attempt.answers, [null, null, null, null, null]);
  assert.equal(attempt.practice, false); // a new day: its first score still counts
  assert.deepEqual(Trivia.playedBefore(state, repeat), { day: "2026-11-21", score: 4, total: 5 });
  assert.equal(Trivia.playedBefore(state, quiz("2026-11-28", "quiz-02")), null);
});

test("questions changed during the day: a new attempt, practice when the day already has a first score", () => {
  const state = Trivia.read(fakeStorage());
  const today = quiz();
  play(state, today, [1, 2, 1, 0, 3]);
  const changed = { ...today, ids: ["q-one", "q-two", "q-three", "q-four", "q-new"] };
  const attempt = Trivia.current(state, changed);
  assert.deepEqual(attempt.ids, changed.ids);
  assert.equal(attempt.practice, true);
  assert.equal(Trivia.firstScore(state, today.day).score, 5);
});

test("damaged or foreign data is read as nothing saved, never as a crash", () => {
  for (const raw of ["{not json", "42", "null", "[]", '{"scores": 5, "attempt": "x"}']) {
    assert.deepEqual(Trivia.read(fakeStorage({ [Trivia.KEY]: raw })), { scores: {}, attempt: null });
  }
  const mixed = JSON.stringify({
    scores: {
      "2026-11-21": { quiz: "quiz-01", score: 4, total: 5 },
      "not-a-day": { quiz: "quiz-01", score: 4, total: 5 },
      "2026-11-22": { quiz: "quiz-02", score: 6, total: 5 },
      "2026-11-23": { quiz: "Quiz 3", score: 1, total: 5 },
    },
    attempt: { day: "2026-11-21", quiz: "quiz-01", ids: ["a", "b"], answers: [null, 1], position: 0, practice: false },
  });
  const state = Trivia.read(fakeStorage({ [Trivia.KEY]: mixed }));
  assert.deepEqual(Object.keys(state.scores), ["2026-11-21"]);
  assert.equal(state.attempt, null); // an answer after an unanswered question cannot happen
  const skipped = JSON.stringify({ attempt: { day: "2026-11-21", quiz: "quiz-01", ids: ["a", "b"],
    answers: [1, null], position: 2, practice: false } }); // past the next unanswered question
  assert.equal(Trivia.read(fakeStorage({ [Trivia.KEY]: skipped })).attempt, null);
});

test("unavailable storage is detected, and the quiz then runs on memory for the page", () => {
  assert.ok(Trivia.open({ localStorage: fakeStorage() }));
  const blocked = {};
  Object.defineProperty(blocked, "localStorage", { get() { throw new Error("SecurityError"); } });
  assert.equal(Trivia.open(blocked), null);
  const full = fakeStorage();
  full.setItem = () => { throw new Error("QuotaExceededError"); };
  assert.equal(Trivia.open({ localStorage: full }), null);
  assert.equal(Object.keys(fakeStorage().data).length, 0); // the probe leaves nothing behind

  const memory = Trivia.memoryStore();
  let state = Trivia.read(memory);
  play(state, quiz(), [1, 2, 1, 0, 3]);
  state = Trivia.save(memory, state);
  assert.equal(Trivia.firstScore(Trivia.read(memory), "2026-11-21").score, 5);
});

test("a failed save reaches the caller, which says so; the state in memory is kept", () => {
  const storage = fakeStorage();
  storage.setItem = () => { throw new Error("QuotaExceededError"); };
  const state = Trivia.read(storage);
  Trivia.choose(state, quiz(), 1);
  assert.throws(() => Trivia.save(storage, state), /QuotaExceededError/);
  assert.deepEqual(state.attempt.answers, [1, null, null, null, null]);
});

test("a first score saved in another tab is never replaced", () => {
  const storage = fakeStorage();
  const otherTab = Trivia.read(storage);
  const thisTab = Trivia.read(storage);
  play(otherTab, quiz(), [1, 2, 1, 0, 3]); // 5, finished first
  Trivia.save(storage, otherTab);
  play(thisTab, quiz(), [0, 0, 0, 0, 0]); // 0, finished later in this tab
  const saved = Trivia.save(storage, thisTab);
  assert.equal(saved.scores["2026-11-21"].score, 5);
  assert.equal(Trivia.read(fakeStorage(storage.data)).scores["2026-11-21"].score, 5);
});

test("cleared storage starts the day's quiz again from the first question", () => {
  const storage = fakeStorage();
  let state = Trivia.read(storage);
  Trivia.choose(state, quiz(), 1);
  Trivia.save(storage, state);
  storage.removeItem(Trivia.KEY); // the visitor cleared this site's data
  state = Trivia.read(storage);
  assert.deepEqual(Trivia.current(state, quiz()).answers, [null, null, null, null, null]);
  assert.equal(Trivia.firstScore(state, "2026-11-21"), null);
});
