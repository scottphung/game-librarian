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

  // The game key on a wishlist button and its aliases (its other keys, such as "igdb-52189").
  function buttonKey(button) { return button.dataset.key || button.dataset.appid; }
  function buttonAliases(button) { return (button.dataset.aliases || "").split(" ").filter(Boolean); }

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
    var saved = Wishlist.has(wishlistStore, buttonKey(button), buttonAliases(button));
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
      var saved = Wishlist.toggle(wishlistStore, buttonKey(button), button.dataset.name, buttonAliases(button));
      updateWishlistButton(root);
      if (status) { status.textContent = saved ? "Added to your wishlist." : "Removed from your wishlist."; }
    } catch (_) { if (status) { status.textContent = "The wishlist could not be saved in this browser."; } }
  });
  updateWishlistButton(document);
  renderWishlist();

  // On a phone the filters start collapsed behind a "Search & filters" button, so the games come first.
  var sidebar = document.querySelector(".sidebar");
  var filtersToggle = document.querySelector(".filters-toggle");
  function setCollapsed(collapsed) {
    if (!sidebar || !filtersToggle) { return; }
    sidebar.classList.toggle("is-collapsed", collapsed);
    filtersToggle.setAttribute("aria-expanded", String(!collapsed));
  }
  if (sidebar && filtersToggle) {
    setCollapsed(window.matchMedia("(max-width: 720px)").matches);
    filtersToggle.addEventListener("click", function () {
      setCollapsed(!sidebar.classList.contains("is-collapsed"));
    });
  }

  // Apply a filter as soon as a menu or one of these checkboxes changes.
  document.querySelectorAll("[data-autosubmit]").forEach(function (control) {
    control.addEventListener("change", function () {
      if (control.form.requestSubmit) { control.form.requestSubmit(); } else { control.form.submit(); }
    });
  });

  // Genres & tags apply together: ticking a box only marks it, so several can be chosen without a page load
  // each time. The action bar says how many tag changes "Apply filters" will apply, and stays in view
  // (sticky) while any are pending. Without JavaScript the boxes behave the same way, without the note.
  var deferredFilters = document.querySelector("[data-deferred-filters]");
  var applyBar = document.querySelector("[data-apply-bar]");
  var pendingNote = applyBar ? applyBar.querySelector("[data-pending-note]") : null;
  function updatePending() {
    if (!deferredFilters || !applyBar || !pendingNote) { return; }
    var changed = 0, ticked = 0;
    deferredFilters.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
      if (box.checked !== box.defaultChecked) { changed++; } // defaultChecked: as the server applied it
      if (box.checked) { ticked++; }
      var option = box.closest(".option");
      if (option) { option.classList.toggle("is-selected", box.checked); }
    });
    var limit = Number(deferredFilters.getAttribute("data-max-selected")) || 0;
    var text = changed === 0 ? "" :
      (changed === 1 ? "1 tag change is" : changed + " tag changes are") + " not applied yet.";
    if (limit && ticked > limit) { text += " At most " + limit + " tags are used; untick some to choose which."; }
    if (pendingNote.textContent !== text) { pendingNote.textContent = text; } // role="status" announces it
    applyBar.classList.toggle("has-pending", changed > 0);
  }
  if (deferredFilters) {
    deferredFilters.addEventListener("change", updatePending);
    window.addEventListener("pageshow", updatePending); // a page restored by Back may keep ticked boxes
    updatePending();
  }

  // After the filters are applied, the new page keeps the visitor's place: the same scroll position (of the
  // page and of the filter column), the filters still open on a phone, and focus on the control that was
  // used. Only within this tab (sessionStorage) and only right after the submit.
  var POSITION_KEY = "game-librarian-filter-position";
  var POSITION_MAX_AGE = 30000; // milliseconds between the submit and the new page
  var filterForm = document.getElementById("filters");

  function controlDescription(control) {
    if (!control || !filterForm.contains(control)) { return null; }
    if (control.id) { return { id: control.id }; }
    if (control.name) { return { name: control.name, value: control.type === "checkbox" ? control.value : null }; }
    return null;
  }

  function findControl(description) {
    if (!description) { return null; }
    if (description.id) { return document.getElementById(description.id); }
    var matches = filterForm.querySelectorAll("[name]");
    for (var i = 0; i < matches.length; i++) {
      if (matches[i].name === description.name &&
          (description.value === null || matches[i].value === description.value)) { return matches[i]; }
    }
    return null;
  }

  function restoreFilterPosition() {
    var position = null;
    try {
      position = JSON.parse(window.sessionStorage.getItem(POSITION_KEY) || "null");
      window.sessionStorage.removeItem(POSITION_KEY);
    } catch (_) { return; } // storage blocked or damaged: open the page normally
    if (!position || position.path !== window.location.pathname ||
        !(Date.now() - position.at < POSITION_MAX_AGE)) { return; }
    if (position.open) { setCollapsed(false); }
    if (sidebar) { sidebar.scrollTop = position.sidebar || 0; }
    window.scrollTo(0, position.y || 0);
    var control = findControl(position.control);
    if (control) { control.focus({ preventScroll: true }); }
  }

  if (filterForm) {
    filterForm.addEventListener("submit", function (event) {
      var position = {
        path: window.location.pathname,
        at: Date.now(),
        y: window.scrollY,
        sidebar: sidebar ? sidebar.scrollTop : 0,
        open: sidebar ? !sidebar.classList.contains("is-collapsed") : true,
        control: controlDescription(event.submitter || document.activeElement)
      };
      try { window.sessionStorage.setItem(POSITION_KEY, JSON.stringify(position)); } catch (_) { /* not kept */ }
    });
    restoreFilterPosition();
  }

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

  // A link to a section inside a closed <details> (such as a Data Story's "#all-sources") opens it first.
  function openLinkedDetails() {
    var id = decodeURIComponent(window.location.hash.slice(1));
    var target = id ? document.getElementById(id) : null;
    var details = target ? target.closest("details") : null;
    if (details && !details.open) {
      details.open = true;
      target.scrollIntoView();
    }
  }
  window.addEventListener("hashchange", openLinkedDetails);
  openLinkedDetails();

  // An image that cannot be loaded (moved or removed at its source) becomes the same gray box as a game
  // without an image, instead of a broken-image icon: a Data Story's timeline frame or a dashboard thumbnail.
  // The source link written beside the game (such as a story's IGDB link) stays.
  function useImageFallback(image) {
    var frame = image.parentElement;
    frame.classList.add("is-empty");
    image.replaceWith(Object.assign(document.createElement("span"),
      { className: "visually-hidden", textContent: "Image unavailable" }));
  }
  document.querySelectorAll("img[data-image-fallback]").forEach(function (image) {
    if (image.complete && image.naturalWidth === 0 && image.getAttribute("src")) { useImageFallback(image); }
    else { image.addEventListener("error", function () { useImageFallback(image); }, { once: true }); }
  });

  // The release radar (Trending games dashboard): pointing at or focusing a game shows its cover, dates and
  // platforms in one tooltip and lights up the game's points in its other platform sections. The tooltip only
  // repeats the page (each point's own text, the "Date TBA or approximate" list and the table say the same), so it
  // is hidden from assistive technology. It stays open while the pointer moves onto it, and Escape closes it.
  document.querySelectorAll("[data-radar-card]").forEach(function (card) {
    var tip = document.createElement("div");
    tip.className = "radar-tip";
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    card.appendChild(tip);
    var current = null; // the point or name the tooltip belongs to
    var hideTimer = 0;

    function marksOf(key) { return card.querySelectorAll('[data-game="' + CSS.escape(key) + '"]'); }

    function hide() {
      window.clearTimeout(hideTimer);
      if (!current) { return; }
      marksOf(current.getAttribute("data-game")).forEach(function (mark) { mark.classList.remove("is-active"); });
      card.classList.remove("has-active");
      tip.hidden = true;
      tip.replaceChildren();
      current = null;
    }

    // Above the point, or below it when the window has no room above; never past the card's sides.
    function place(mark) {
      var box = card.getBoundingClientRect();
      var spot = mark.getBoundingClientRect();
      var left = spot.left - box.left + spot.width / 2 - tip.offsetWidth / 2;
      tip.style.left = Math.max(8, Math.min(left, box.width - tip.offsetWidth - 8)) + "px";
      var above = spot.top - tip.offsetHeight - 10 >= 0;
      tip.style.top = (above ? spot.top - box.top - tip.offsetHeight - 10 : spot.bottom - box.top + 10) + "px";
    }

    function show(mark) {
      window.clearTimeout(hideTimer);
      if (mark === current) { return; }
      var template = document.getElementById(mark.getAttribute("data-tip"));
      if (!template) { return; }
      hide();
      tip.replaceChildren(template.content.cloneNode(true)); // server-rendered by Jinja, which escapes every value
      var when = tip.querySelector("[data-tip-when]");
      if (when) { when.textContent = mark.getAttribute("data-when") || ""; }
      tip.querySelectorAll("img[data-image-fallback]").forEach(function (image) {
        image.addEventListener("error", function () { useImageFallback(image); }, { once: true });
      });
      current = mark;
      marksOf(mark.getAttribute("data-game")).forEach(function (other) { other.classList.add("is-active"); });
      card.classList.add("has-active");
      tip.hidden = false;
      place(mark);
    }

    function hideSoon() {
      window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(hide, 150); // time to move the pointer onto the tooltip
    }

    // Without JavaScript a point's title is its tooltip; with this one it would show twice.
    card.querySelectorAll("[data-tip][title]").forEach(function (mark) { mark.removeAttribute("title"); });
    card.addEventListener("pointerover", function (event) {
      var mark = event.target.closest("[data-tip]");
      if (mark) { show(mark); } else if (tip.contains(event.target)) { window.clearTimeout(hideTimer); }
    });
    card.addEventListener("pointerout", function (event) {
      var into = event.relatedTarget;
      if (current && !(into && (current.contains(into) || tip.contains(into)))) { hideSoon(); }
    });
    card.addEventListener("focusin", function (event) {
      var mark = event.target.closest("[data-tip]");
      if (mark) { show(mark); }
    });
    card.addEventListener("focusout", function (event) {
      var into = event.relatedTarget && event.relatedTarget.closest ? event.relatedTarget.closest("[data-tip]") : null;
      if (!into || !card.contains(into)) { hide(); }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && current) { hide(); }
    });
    window.addEventListener("resize", hide);
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

  // "Watch it live": when the stored Twitch answer is due for a check, ask the server for a newer one
  // and swap the section in. The server talks to Twitch; the page only receives escaped HTML.
  function refreshLive(root) {
    var section = root.querySelector("[data-live-refresh]");
    if (!section) { return; }
    fetch(section.getAttribute("data-live-refresh"), { headers: { Accept: "text/html" } })
      .then(function (response) {
        if (!response.ok) { throw new Error("HTTP " + response.status); }
        return response.text();
      })
      .then(function (html) {
        if (!section.isConnected) { return; } // the dialog has moved on to another game
        var holder = document.createElement("template");
        holder.innerHTML = html.trim(); // server-rendered by Jinja, which escapes every value
        var fresh = holder.content.querySelector("[data-live]");
        if (fresh) { section.replaceWith(fresh); }
      })
      .catch(function () {
        if (!section.isConnected) { return; }
        var status = section.querySelector("[data-live-status]");
        if (status) { status.textContent = "Live streams could not be loaded. Try again later."; }
      });
  }

  var dialog = document.getElementById("game-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    // The full details page has no dialog (and very old browsers cannot open one):
    // Details links then open that page, which sets up its own clip and live streams here.
    document.querySelectorAll(".game-sheet").forEach(function (sheet) {
      setUpClip(sheet);
      refreshLive(sheet);
    });
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
        refreshLive(content);
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
