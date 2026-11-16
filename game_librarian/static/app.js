/* Game Librarian: small progressive enhancements. Every feature also works without JavaScript:
   filters have an Apply button, and each Details link opens a full details page. */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

  // The Game Librarian wishlist (static/wishlist.js): browser-local, separate from Steam.
  var Wishlist = window.GameLibrarianWishlist;
  var wishlistStore = Wishlist ? Wishlist.open(window) : null;
  var STORAGE_UNAVAILABLE = "The wishlist is unavailable: this browser does not let this site store data " +
    "(for example in some private windows, or when site data is blocked).";

  // Show the details' wishlist button in its current state, or explain why there is none.
  function updateWishlistButton(root) {
    var button = root.querySelector("[data-wishlist-toggle]");
    if (!button) { return; }
    var note = root.querySelector("[data-wishlist-note]");
    var status = root.querySelector("[data-wishlist-status]");
    if (!wishlistStore) {
      button.hidden = true;
      if (status) { status.textContent = STORAGE_UNAVAILABLE; }
      return;
    }
    var saved = Wishlist.has(wishlistStore, button.dataset.appid);
    button.hidden = false;
    if (note) { note.hidden = false; }
    button.textContent = saved ? "Remove from wishlist" : "Add to wishlist";
    button.setAttribute("aria-pressed", String(saved));
  }

  // The /wishlist page: one link and one Remove button per saved game.
  function renderWishlist() {
    var list = document.querySelector("[data-wishlist-list]");
    var message = document.querySelector("[data-wishlist-message]");
    if (!list || !message) { return; }
    list.replaceChildren();
    if (!wishlistStore) { message.textContent = STORAGE_UNAVAILABLE; return; }
    var items;
    try { items = Wishlist.read(wishlistStore); } catch (_) {
      message.textContent = "The wishlist could not be read in this browser.";
      return;
    }
    message.textContent = items.length === 0
      ? "Your wishlist is empty. Open a game's details and choose Add to wishlist."
      : (items.length === 1 ? "1 saved game." : items.length + " saved games.");
    items.forEach(function (item) {
      var row = document.createElement("li");
      row.className = "wishlist-item";
      var link = document.createElement("a");
      link.className = "text-link";
      link.href = "/games/" + encodeURIComponent(item.id);
      link.textContent = item.name; // textContent, never innerHTML: names come from storage
      var remove = document.createElement("button");
      remove.className = "button button-secondary";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", "Remove " + item.name + " from the wishlist");
      remove.addEventListener("click", function () {
        try {
          Wishlist.remove(wishlistStore, item.id);
          renderWishlist();
          message.textContent = "Removed " + item.name + ". " + message.textContent;
          message.focus(); // the button is gone; keep keyboard focus on the page
        } catch (_) { message.textContent = "The wishlist could not be saved in this browser."; }
      });
      row.append(link, document.createTextNode(" "), remove);
      list.append(row);
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-wishlist-toggle]");
    if (!button || !wishlistStore) { return; }
    var root = button.closest(".details") || document;
    var status = root.querySelector("[data-wishlist-status]");
    try {
      var saved = Wishlist.toggle(wishlistStore, button.dataset.appid, button.dataset.name);
      updateWishlistButton(root);
      if (status) { status.textContent = saved ? "Added to your wishlist." : "Removed from your wishlist."; }
    } catch (_) { if (status) { status.textContent = "The wishlist could not be saved in this browser."; } }
  });
  updateWishlistButton(document);
  renderWishlist();

  // On a phone the filters start collapsed behind a "Search & filters" button, so the games come first.
  var sidebar = document.querySelector(".sidebar");
  var filtersToggle = document.querySelector(".filters-toggle");
  if (sidebar && filtersToggle) {
    var setCollapsed = function (collapsed) {
      sidebar.classList.toggle("is-collapsed", collapsed);
      filtersToggle.setAttribute("aria-expanded", String(!collapsed));
    };
    setCollapsed(window.matchMedia("(max-width: 720px)").matches);
    filtersToggle.addEventListener("click", function () {
      setCollapsed(!sidebar.classList.contains("is-collapsed"));
    });
  }

  // Apply a filter as soon as a checkbox or menu changes.
  document.querySelectorAll("[data-autosubmit]").forEach(function (control) {
    control.addEventListener("change", function () {
      if (control.form.requestSubmit) { control.form.requestSubmit(); } else { control.form.submit(); }
    });
  });

  // The ranking explanation closes on Escape or a click outside it.
  document.querySelectorAll("details.info-tip").forEach(function (tip) {
    document.addEventListener("click", function (event) {
      if (tip.open && !tip.contains(event.target)) { tip.open = false; }
    });
    tip.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && tip.open) {
        tip.open = false;
        tip.querySelector("summary").focus();
      }
    });
  });

  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // A looping trailer clip: muted, with a keyboard-accessible Pause / Play button.
  // It plays on its own only when the visitor has not asked for reduced motion.
  function setUpClip(root) {
    var video = root.querySelector("video[data-clip]");
    var toggle = root.querySelector("[data-clip-toggle]");
    if (!video || !toggle) { return; }
    var sources = video.querySelectorAll("source");
    var label = function () { toggle.textContent = video.paused ? "Play clip" : "Pause clip"; };
    video.controls = false; // the custom button replaces the native controls
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      if (video.paused) { video.play().catch(label); } else { video.pause(); }
    });
    video.addEventListener("play", label);
    video.addEventListener("pause", label);
    if (sources.length) {
      // When the last source fails, the browser keeps showing the poster: a still image fallback.
      sources[sources.length - 1].addEventListener("error", function () {
        toggle.hidden = true;
        var note = root.querySelector("[data-clip-fallback]");
        if (note) { note.hidden = false; }
      });
    }
    if (!reducedMotion.matches) { video.play().catch(label); }
    label();
  }

  var dialog = document.getElementById("game-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    // The full details page has no dialog (and very old browsers cannot open one):
    // Details links then open that page, which sets up its own clip here.
    document.querySelectorAll(".game-sheet").forEach(setUpClip);
    return;
  }
  var content = dialog.querySelector("[data-dialog-content]");
  var opener = null; // the element to focus again when the dialog closes
  var pushedHistory = false; // true when opening the dialog added a history entry
  var closingFromHistory = false;
  var request = 0;

  function currentGame() { return new URL(window.location.href).searchParams.get("game"); }

  function urlWithGame(appid) {
    var url = new URL(window.location.href);
    if (appid) { url.searchParams.set("game", appid); } else { url.searchParams.delete("game"); }
    return url.pathname + url.search + url.hash;
  }

  // The list's query string (search, filters, view, sort, page) without "game". The details panel uses it
  // so that its links, such as "More games like this", keep the browsing context.
  function listQuery() {
    var params = new URL(window.location.href).searchParams;
    params.delete("game");
    var text = params.toString();
    return text ? "?" + text : "";
  }

  // history: "push" adds an entry (opening from the list), "replace" swaps the current one (moving to a
  // related game inside the open dialog, so Back still returns to the list), anything else leaves it alone.
  function openGame(appid, history) {
    var thisRequest = ++request;
    var moving = dialog.open;
    content.innerHTML = '<p class="dialog-status">Loading…</p>';
    if (!dialog.open) {
      dialog.showModal();
      document.documentElement.classList.add("has-dialog");
    }
    if (history === "push") {
      window.history.pushState({ game: appid }, "", urlWithGame(appid));
      pushedHistory = true;
    } else if (history === "replace") {
      window.history.replaceState({ game: appid }, "", urlWithGame(appid));
    }
    var detailsPath = "/games/" + encodeURIComponent(appid);
    fetch(detailsPath + "/panel" + listQuery(), { headers: { Accept: "text/html" } })
      .then(function (response) { return response.text(); })
      .then(function (html) {
        if (thisRequest !== request) { return; } // a newer request replaced this one
        content.innerHTML = html; // server-rendered by Jinja, which escapes every value
        setUpClip(content);
        updateWishlistButton(content);
        if (moving) { // a new game in the open dialog: start at its top and announce it by focusing its title
          dialog.scrollTop = 0;
          var heading = content.querySelector("#dialog-heading");
          if (heading) { heading.focus(); }
        }
      })
      .catch(function () {
        if (thisRequest !== request) { return; }
        content.innerHTML = '<p class="dialog-status">The details could not be loaded. ' +
          '<a class="text-link" href="' + detailsPath + listQuery() + '">Open the details page</a>.</p>';
      });
  }

  // Every way of closing (button, Escape, backdrop, Back button) ends here.
  dialog.addEventListener("close", function () {
    request++;
    content.replaceChildren(); // removing the video also stops it downloading
    document.documentElement.classList.remove("has-dialog");
    if (!closingFromHistory) {
      if (pushedHistory) { window.history.back(); }
      else if (currentGame()) { window.history.replaceState(window.history.state, "", urlWithGame(null)); }
    }
    pushedHistory = false;
    closingFromHistory = false;
    if (opener && opener.isConnected) { opener.focus(); }
    opener = null;
  });

  dialog.querySelector("[data-close-dialog]").addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) { dialog.close(); } // a click on the backdrop
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-details]");
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return; // modified clicks keep their normal behavior, such as opening a new tab
    }
    event.preventDefault();
    if (dialog.open && dialog.contains(link)) {
      openGame(link.getAttribute("data-details"), "replace"); // a related game: focus later returns to the card
    } else {
      opener = link;
      openGame(link.getAttribute("data-details"), "push");
    }
  });

  window.addEventListener("popstate", function () {
    var appid = currentGame();
    if (appid) { openGame(appid, null); }
    else if (dialog.open) { closingFromHistory = true; dialog.close(); }
  });

  // A link with ?game=<appid> opens straight into that game's details.
  var initial = currentGame();
  if (initial) {
    opener = document.querySelector('a[data-details="' + CSS.escape(initial) + '"]');
    openGame(initial, null);
  }
})();
