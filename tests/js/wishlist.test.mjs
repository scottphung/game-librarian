// Wishlist storage (game_librarian/static/wishlist.js), tested with Node's built-in runner: node --test tests/js
// A fake storage object stands in for the browser's localStorage, including the ways it can fail.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const Wishlist = require("../../game_librarian/static/wishlist.js");

function fakeStorage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => { data[key] = String(value); },
    removeItem: (key) => { delete data[key]; },
  };
}

test("adding and removing a game, kept across page loads", () => {
  const storage = fakeStorage();
  assert.equal(Wishlist.toggle(storage, 101, "Game 01"), true); // now saved
  assert.equal(Wishlist.toggle(storage, "202", "Game 02"), true);
  assert.deepEqual(Wishlist.read(storage), [{ id: "101", name: "Game 01" }, { id: "202", name: "Game 02" }]);
  // A new page load reads the same storage again.
  const later = fakeStorage(storage.data);
  assert.equal(Wishlist.has(later, 101), true);
  assert.equal(Wishlist.toggle(later, 101, "Game 01"), false); // pressing the button again removes it
  Wishlist.remove(later, "202");
  assert.deepEqual(Wishlist.read(later), []);
  assert.equal(later.data[Wishlist.KEY], "[]");
});

test("damaged or foreign data is treated as an empty list, never as a crash", () => {
  for (const raw of ["{not json", '{"id": 1}', "42", "null"]) {
    assert.deepEqual(Wishlist.read(fakeStorage({ [Wishlist.KEY]: raw })), []);
  }
  const mixed = JSON.stringify([{ id: "1", name: "Ok" }, { id: "x", name: "Bad id" }, { id: 2 }, { id: "1", name: "Dup" }, null]);
  assert.deepEqual(Wishlist.read(fakeStorage({ [Wishlist.KEY]: mixed })), [{ id: "1", name: "Ok" }]);
});

test("unavailable storage is detected before the button is offered", () => {
  assert.ok(Wishlist.open({ localStorage: fakeStorage() })); // works
  const blocked = {}; // reading window.localStorage throws when site data is blocked
  Object.defineProperty(blocked, "localStorage", { get() { throw new Error("SecurityError"); } });
  assert.equal(Wishlist.open(blocked), null);
  const full = fakeStorage();
  full.setItem = () => { throw new Error("QuotaExceededError"); }; // full or read-only storage
  assert.equal(Wishlist.open({ localStorage: full }), null);
  assert.equal(Object.keys(fakeStorage().data).length, 0); // the probe leaves nothing behind
});

test("a failed save reaches the caller, which shows a message", () => {
  const storage = fakeStorage();
  storage.setItem = () => { throw new Error("QuotaExceededError"); };
  assert.throws(() => Wishlist.toggle(storage, 1, "Game"), /QuotaExceededError/);
});
