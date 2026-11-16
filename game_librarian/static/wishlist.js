/* Game Librarian wishlist storage: a list of {id, name} kept in the browser's localStorage.

   This is the site's own wishlist, not the Steam wishlist. It stays in one browser on one device,
   is never sent to the server, and disappears when the site's browser data is cleared.
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
      if (id && /^\d+$/.test(id) && typeof item.name === "string" && !seen[id]) {
        seen[id] = true;
        items.push({ id: id, name: item.name });
      }
    });
    return items;
  }

  function write(store, items) { store.setItem(KEY, JSON.stringify(items)); }

  function has(store, id) {
    var key = String(id);
    return read(store).some(function (item) { return item.id === key; });
  }

  // Add the game, or remove it when it is already saved. Returns true when the game is now saved.
  function toggle(store, id, name) {
    var key = String(id);
    var items = read(store);
    var saved = items.some(function (item) { return item.id === key; });
    items = items.filter(function (item) { return item.id !== key; });
    if (!saved) { items.push({ id: key, name: String(name) }); }
    write(store, items);
    return !saved;
  }

  function remove(store, id) {
    var key = String(id);
    write(store, read(store).filter(function (item) { return item.id !== key; }));
  }

  return { KEY: KEY, open: open, read: read, has: has, toggle: toggle, remove: remove };
});
