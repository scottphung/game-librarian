/* Game Librarian Daily Trivia storage: the visitor's quiz progress and scores in the browser's localStorage.

   Like the wishlist (wishlist.js), it stays in one browser on one device, is never sent to the server, and
   disappears when the site's browser data is cleared. One key holds everything:

     {"scores":  {"2026-11-21": {"quiz": "quiz-01", "score": 4, "total": 5}},
      "attempt": {"day": "2026-11-21", "quiz": "quiz-01", "ids": ["final-fantasy-name", ...],
                  "answers": [1, 3, null, null, null], "position": 1, "practice": false}}

   "scores" keeps the first finished score of each quiz day (a UTC date). "attempt" is the quiz on screen: the choice
   made for each question (null = not answered yet) and the card shown (position; the number of questions means the
   results). So closing the popup or reloading the page resumes it. A replay is a practice attempt: its score is shown
   but never replaces the day's first score. Another day, or a quiz whose questions changed, starts a new attempt.

   A quiz, as app.js passes it: {day, id, ids: [question ids], answers: [index of each correct choice]}.
   Pure logic, shared by app.js (window.GameLibrarianTrivia) and the Node tests (tests/js/trivia.test.mjs, which pass
   a fake storage object). */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) { module.exports = api; }
  else { root.GameLibrarianTrivia = api; }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var KEY = "game-librarian-trivia-v1";
  var MAX_QUESTIONS = 10;
  var MAX_CHOICES = 10;
  // A quiz day as the server writes it: "2026-11-21".
  var DAY = /^\d{4}-\d{2}-\d{2}$/;
  // A quiz or question id from curated/trivia.json: lowercase words joined by hyphens.
  var ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

  // The browser's localStorage when this site may use it, else null (blocked site data, full or read-only storage).
  function open(win) {
    try {
      var store = win.localStorage;
      var probe = KEY + "-probe";
      store.setItem(probe, "1");
      store.removeItem(probe);
      return store;
    } catch (_) {
      return null;
    }
  }

  // A stand-in for localStorage that lasts until the page closes, so the quiz still works when storage is blocked.
  function memoryStore() {
    var data = {};
    return {
      getItem: function (key) { return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null; },
      setItem: function (key, value) { data[key] = String(value); },
      removeItem: function (key) { delete data[key]; }
    };
  }

  function isInteger(value, low, high) {
    return typeof value === "number" && Math.floor(value) === value && value >= low && value <= high;
  }

  function validScore(value) {
    return Boolean(value) && typeof value.quiz === "string" && ID.test(value.quiz) &&
      isInteger(value.total, 1, MAX_QUESTIONS) && isInteger(value.score, 0, value.total);
  }

  // An attempt read from storage, or null when it is damaged. Answers are given in order, so every answered
  // question comes before the unanswered ones, and the card shown is the last answered one or the next one.
  function validAttempt(value) {
    if (!value || typeof value.day !== "string" || !DAY.test(value.day) || typeof value.quiz !== "string" ||
        !ID.test(value.quiz) || !Array.isArray(value.ids) || !Array.isArray(value.answers) ||
        typeof value.practice !== "boolean") { return null; }
    var count = value.ids.length;
    if (count < 1 || count > MAX_QUESTIONS || value.answers.length !== count) { return null; }
    if (!value.ids.every(function (id) { return typeof id === "string" && ID.test(id); })) { return null; }
    var answered = 0;
    while (answered < count && value.answers[answered] !== null) { answered++; }
    for (var i = 0; i < count; i++) {
      var choice = value.answers[i];
      if (i < answered ? !isInteger(choice, 0, MAX_CHOICES - 1) : choice !== null) { return null; }
    }
    if (!isInteger(value.position, Math.max(0, answered - 1), Math.min(answered, count))) { return null; }
    return { day: value.day, quiz: value.quiz, ids: value.ids.slice(), answers: value.answers.slice(),
             position: value.position, practice: value.practice };
  }

  // The saved state. Damaged or foreign data counts as nothing saved instead of breaking the popup.
  function read(store) {
    var state = { scores: {}, attempt: null };
    var value;
    try { value = JSON.parse(store.getItem(KEY) || "null"); } catch (_) { return state; }
    if (!value || typeof value !== "object") { return state; }
    var scores = value.scores && typeof value.scores === "object" ? value.scores : {};
    Object.keys(scores).forEach(function (day) {
      var entry = scores[day];
      if (DAY.test(day) && validScore(entry)) {
        state.scores[day] = { quiz: entry.quiz, score: entry.score, total: entry.total };
      }
    });
    state.attempt = validAttempt(value.attempt);
    return state;
  }

  // Save the state and return what is now stored. Another tab may have saved a first score meanwhile: a first score
  // already in storage is never replaced. Throws when the browser refuses to save (the caller says so).
  function save(store, state) {
    var stored = read(store);
    Object.keys(state.scores).forEach(function (day) {
      if (!stored.scores[day]) { stored.scores[day] = state.scores[day]; }
    });
    stored.attempt = state.attempt;
    store.setItem(KEY, JSON.stringify(stored));
    return stored;
  }

  function sameIds(a, b) {
    return a.length === b.length && a.every(function (id, i) { return id === b[i]; });
  }

  function newAttempt(quiz, practice) {
    return { day: quiz.day, quiz: quiz.id, ids: quiz.ids.slice(), answers: quiz.ids.map(function () { return null; }),
             position: 0, practice: practice };
  }

  // The first finished score of a day ({quiz, score, total}), or null.
  function firstScore(state, day) {
    return state.scores[day] || null;
  }

  // The visitor's attempt at `quiz`: the saved one when it is for the same day and the same questions, else a new
  // one, which is practice when the day already has a first score.
  function current(state, quiz) {
    var attempt = state.attempt;
    if (!attempt || attempt.day !== quiz.day || attempt.quiz !== quiz.id || !sameIds(attempt.ids, quiz.ids)) {
      attempt = state.attempt = newAttempt(quiz, Boolean(state.scores[quiz.day]));
    }
    return attempt;
  }

  // The correct answers in an attempt so far.
  function score(attempt, quiz) {
    return attempt.answers.filter(function (choice, i) {
      return choice !== null && choice === quiz.answers[i];
    }).length;
  }

  // Record the choice for the question on screen. Returns {correct, finished, first} (first: this finished the
  // day's first attempt, whose score is now saved), or null when that question was already answered.
  function choose(state, quiz, choice) {
    var attempt = current(state, quiz);
    var at = attempt.position;
    if (at >= attempt.ids.length || attempt.answers[at] !== null || !isInteger(choice, 0, MAX_CHOICES - 1)) {
      return null;
    }
    attempt.answers[at] = choice;
    var finished = attempt.answers.every(function (answer) { return answer !== null; });
    var first = finished && !attempt.practice && !state.scores[quiz.day];
    if (first) {
      state.scores[quiz.day] = { quiz: quiz.id, score: score(attempt, quiz), total: attempt.ids.length };
    }
    return { correct: choice === quiz.answers[at], finished: finished, first: first };
  }

  // Move on from an answered question to the next one (or to the results after the last). Returns the new position.
  function advance(state, quiz) {
    var attempt = current(state, quiz);
    if (attempt.position < attempt.ids.length && attempt.answers[attempt.position] !== null) { attempt.position++; }
    return attempt.position;
  }

  // Start the same quiz again for practice.
  function replay(state, quiz) {
    state.attempt = newAttempt(quiz, true);
    return state.attempt;
  }

  // The best first score ({day, score, total}), the earliest day it was reached, or null before any.
  function best(state) {
    var top = null;
    Object.keys(state.scores).sort().forEach(function (day) {
      var entry = state.scores[day];
      if (!top || entry.score / entry.total > top.score / top.total) {
        top = { day: day, score: entry.score, total: entry.total };
      }
    });
    return top;
  }

  // The latest earlier day with a first score for this same quiz ({day, score, total}), for a repeated quiz.
  function playedBefore(state, quiz) {
    var found = null;
    Object.keys(state.scores).sort().forEach(function (day) {
      var entry = state.scores[day];
      if (day < quiz.day && entry.quiz === quiz.id) { found = { day: day, score: entry.score, total: entry.total }; }
    });
    return found;
  }

  return { KEY: KEY, open: open, memoryStore: memoryStore, read: read, save: save, firstScore: firstScore,
           current: current, score: score, choose: choose, advance: advance, replay: replay, best: best,
           playedBefore: playedBefore };
});
