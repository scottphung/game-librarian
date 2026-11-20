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

test("wishlists saved before version 0.6 keep working unchanged", () => {
  // The exact value an earlier version wrote: the same key, the same format, Steam app ids only.
  const before = JSON.stringify([{ id: "271590", name: "Grand Theft Auto V" }, { id: "70", name: "Half-Life" }]);
  const storage = fakeStorage({ [Wishlist.KEY]: before });
  assert.equal(Wishlist.KEY, "game-librarian-wishlist-v1");
  assert.deepEqual(Wishlist.read(storage), JSON.parse(before));
  assert.equal(Wishlist.has(storage, 271590), true);
  assert.equal(storage.data[Wishlist.KEY], before); // reading never rewrites it
});

test("games without a Steam app are saved by their IGDB key", () => {
  const storage = fakeStorage({ [Wishlist.KEY]: JSON.stringify([{ id: "70", name: "Half-Life" }]) });
  assert.equal(Wishlist.toggle(storage, "igdb-81249", "The Elder Scrolls VI"), true);
  assert.deepEqual(Wishlist.read(storage), [
    { id: "70", name: "Half-Life" }, { id: "igdb-81249", name: "The Elder Scrolls VI" }]);
  const mixed = JSON.stringify([{ id: "igdb-1", name: "Ok" }, { id: "igdb-", name: "Bad" }, { id: "IGDB-2", name: "Bad" },
    { id: "igdb-3x", name: "Bad" }, { id: "steam-4", name: "Bad" }]);
  assert.deepEqual(Wishlist.read(fakeStorage({ [Wishlist.KEY]: mixed })), [{ id: "igdb-1", name: "Ok" }]);
});

test("a game saved under its IGDB key is recognized once it is a catalog game, and never saved twice", () => {
  const storage = fakeStorage();
  Wishlist.toggle(storage, "igdb-125642", "Path of Exile 2"); // saved while it was only upcoming
  // Its catalog details now use the Steam app id and list the IGDB key as an alias.
  assert.equal(Wishlist.has(storage, "2694490", ["igdb-125642"]), true);
  assert.equal(Wishlist.has(storage, "2694490"), false);
  assert.equal(Wishlist.toggle(storage, "2694490", "Path of Exile 2", ["igdb-125642"]), false); // removes it
  assert.deepEqual(Wishlist.read(storage), []);
  assert.equal(Wishlist.toggle(storage, "2694490", "Path of Exile 2", ["igdb-125642"]), true); // saved again
  assert.deepEqual(Wishlist.read(storage), [{ id: "2694490", name: "Path of Exile 2" }]); // under the current key
  // Even if both keys were saved earlier, removing the game removes both entries.
  storage.data[Wishlist.KEY] = JSON.stringify([{ id: "2694490", name: "A" }, { id: "igdb-125642", name: "A" }]);
  assert.equal(Wishlist.toggle(storage, "2694490", "A", ["igdb-125642", "not a key"]), false);
  assert.deepEqual(Wishlist.read(storage), []);
});
