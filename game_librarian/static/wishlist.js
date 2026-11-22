/* Game Librarian wishlist storage: a list of {id, name} kept in the browser's localStorage.

   This is the site's own wishlist, not the Steam wishlist. It stays in one browser on one device,
   is never sent to the server by itself, and disappears when the site's browser data is cleared. A
   signed-in visitor's wishlist is kept in their account instead (app.js, /api/wishlist); this browser
   list is sent only when its owner chooses to add it to their account, and removed only when they
   choose that too (clear).
   An id is a game key (game_librarian/identity.py): a Steam app id such as "271590", as every entry
   saved before version 0.6 already is, or "igdb-<IGDB id>" for a game without a Steam app in the
   catalog. The storage key and format are unchanged, so existing wishlists keep working as they are.
   A game can have aliases (its other keys): a game saved as "igdb-52189" before it came to the Steam
   catalog still counts as saved when its catalog details list that alias, so it is never saved twice.
   Pure storage logic, shared by app.js (window.GameLibrarianWishlist) and the Node tests
   (tests/js/wishlist.test.mjs, which pass a fake storage object). */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) { module.exports = api; }
  else { root.GameLibrarianWishlist = api; }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var KEY = "game-librarian-wishlist-v1";
  // A Steam app id, or "igdb-" and an IGDB game id.
  var GAME_KEY = /^(?:\d+|igdb-\d+)$/;

  // The browser's localStorage when this site may use it, else null. Reading window.localStorage can
  // throw (blocked site data), and setItem can throw (storage full or read-only), so both are tried.
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

  // The saved games, oldest first. Damaged or foreign data counts as an empty list instead of breaking the page.
  function read(store) {
    var raw = store.getItem(KEY);
    if (!raw) { return []; }
    var value;
    try { value = JSON.parse(raw); } catch (_) { return []; }
    if (!Array.isArray(value)) { return []; }
    var seen = {};
    var items = [];
    value.forEach(function (item) {
      var id = item && String(item.id);
      if (id && GAME_KEY.test(id) && typeof item.name === "string" && !seen[id]) {
        seen[id] = true;
        items.push({ id: id, name: item.name });
      }
    });
    return items;
  }

  function write(store, items) { store.setItem(KEY, JSON.stringify(items)); }

  // The game's key and its aliases, as strings: keys(42, ["igdb-7"]) -> ["42", "igdb-7"].
  function keys(id, aliases) {
    return [String(id)].concat((aliases || []).map(String)).filter(function (key) { return GAME_KEY.test(key); });
  }

  // Whether a list of {id, name} items holds the game under its key or any of its aliases. The same rule decides
  // for the browser's list and for a signed-in account's list (/api/wishlist, kept on the server).
  function listHas(items, id, aliases) {
    var wanted = keys(id, aliases);
    return items.some(function (item) { return wanted.indexOf(String(item.id)) >= 0; });
  }

  // Whether the game is saved in this browser under its key or any of its aliases.
  function has(store, id, aliases) {
    return listHas(read(store), id, aliases);
  }

  // The browser's saved games that an account's list does not have yet (what importing would add), oldest first.
  function missingFrom(browserItems, accountItems) {
    var saved = {};
    accountItems.forEach(function (item) { saved[String(item.id)] = true; });
    return browserItems.filter(function (item) { return !saved[item.id]; });
  }

  // Remove this browser's whole wishlist. Only the visitor's own choice calls it, once every game is in an account.
  function clear(store) { store.removeItem(KEY); }

  // Add the game, or remove it when it is already saved (under its key or an alias).
  // A new entry uses the game's current key. Returns true when the game is now saved.
  function toggle(store, id, name, aliases) {
    var wanted = keys(id, aliases);
    var items = read(store);
    var saved = items.some(function (item) { return wanted.indexOf(item.id) >= 0; });
    items = items.filter(function (item) { return wanted.indexOf(item.id) < 0; });
    if (!saved) { items.push({ id: String(id), name: String(name) }); }
    write(store, items);
    return !saved;
  }

  function remove(store, id) {
    var key = String(id);
    write(store, read(store).filter(function (item) { return item.id !== key; }));
  }

  return { KEY: KEY, open: open, read: read, has: has, toggle: toggle, remove: remove,
           listHas: listHas, missingFrom: missingFrom, clear: clear };
});
