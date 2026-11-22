/* Game Librarian: small progressive enhancements. Every feature also works without JavaScript:
   filters have an Apply button, and each Details link opens a full details page. */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

  // The Game Librarian wishlist, separate from Steam: kept in this browser (static/wishlist.js), or, for a
  // signed-in visitor (body data-wishlist="account"), in their account on the server, through /api/wishlist.
  var Wishlist = window.GameLibrarianWishlist;
  var wishlistStore = Wishlist ? Wishlist.open(window) : null;
  var inAccount = Boolean(Wishlist) && document.body.getAttribute("data-wishlist") === "account";
  var accountItems = null; // the account's saved games once /api/wishlist answered: [{id, name}], oldest first
  var accountProblem = ""; // why the account's list could not be read, if it could not
  var accountLoad = null;
  var STORAGE_UNAVAILABLE = "The wishlist is unavailable: this browser does not let this site store data " +
    "(for example in some private windows, or when site data is blocked).";

  // The game key on a wishlist button and its aliases (its other keys, such as "igdb-52189").
  function buttonKey(button) { return String(button.dataset.key || button.dataset.appid); }
  function buttonAliases(button) { return (button.dataset.aliases || "").split(" ").filter(Boolean); }
  function gamesText(count) { return count === 1 ? "1 game" : count + " games"; }

  // Ask the account's wishlist API: a GET without `body`, else a JSON POST (the server refuses any other kind, so a
  // page on another site cannot change the list). Resolves with the answer, or rejects with a sentence to show.
  function accountRequest(path, body) {
    var options = { credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (answer) {
        if (!response.ok) {
          throw new Error(answer.message || "The wishlist could not be reached (HTTP " + response.status + ").");
        }
        return answer;
      });
    }, function () { throw new Error("The wishlist could not be reached. Check the connection and try again."); });
  }

  // Read the account's list once per page; every button and the wishlist page use that copy afterwards.
  function loadAccount() {
    if (!accountLoad) {
      accountLoad = accountRequest("/api/wishlist").then(function (answer) {
        accountItems = answer.items || [];
      }, function (error) { accountProblem = error.message; });
    }
    return accountLoad;
  }

  // Show the details' wishlist button in its current state, or explain why there is none.
  function updateWishlistButton(root) {
    var button = root.querySelector("[data-wishlist-toggle]");
    if (!button) { return; }
    var note = root.querySelector("[data-wishlist-note]");
    var status = root.querySelector("[data-wishlist-status]");
    var saved;
    if (inAccount) {
      if (accountItems === null) { // still loading, or it failed: no button that would act on an unknown list
        button.hidden = true;
        if (status && accountProblem) { status.textContent = accountProblem; }
        return;
      }
      saved = Wishlist.listHas(accountItems, buttonKey(button), buttonAliases(button));
    } else {
      if (!wishlistStore) {
        button.hidden = true;
        if (status) { status.textContent = STORAGE_UNAVAILABLE; }
        return;
      }
      saved = Wishlist.has(wishlistStore, buttonKey(button), buttonAliases(button));
    }
    button.hidden = false;
    if (note) { note.hidden = false; }
    button.textContent = saved ? "Remove from wishlist" : "Add to wishlist";
    button.setAttribute("aria-pressed", String(saved));
  }

  // One link and one Remove button per saved game. `removeItem(item)` removes the game where the list is kept (it
  // may return a Promise); the list is then shown again.
  function showItems(list, message, items, removeItem) {
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
        remove.disabled = true;
        var removal;
        try { removal = Promise.resolve(removeItem(item)); } catch (error) { removal = Promise.reject(error); }
        removal.then(function () {
          renderWishlist();
          message.textContent = "Removed " + item.name + ". " + message.textContent;
          message.focus(); // the button is gone; keep keyboard focus on the page
        }, function (error) {
          remove.disabled = false;
          message.textContent = inAccount ? error.message : "The wishlist could not be saved in this browser.";
        });
      });
      row.append(link, document.createTextNode(" "), remove);
      list.append(row);
    });
  }

  // The /wishlist page: the account's list for a signed-in visitor, else this browser's.
  function renderWishlist() {
    var list = document.querySelector("[data-wishlist-list]");
    var message = document.querySelector("[data-wishlist-message]");
    if (!list || !message) { return; }
    list.replaceChildren();
    if (inAccount) {
      if (accountItems === null) { message.textContent = accountProblem || "Loading your wishlist…"; return; }
      showItems(list, message, accountItems, function (item) {
        return accountRequest("/api/wishlist/remove", { id: item.id }).then(function (answer) {
          accountItems = answer.items;
        });
      });
      renderImport();
      return;
    }
    if (!wishlistStore) { message.textContent = STORAGE_UNAVAILABLE; return; }
    var items;
    try { items = Wishlist.read(wishlistStore); } catch (_) {
      message.textContent = "The wishlist could not be read in this browser.";
      return;
    }
    showItems(list, message, items, function (item) { Wishlist.remove(wishlistStore, item.id); });
  }

  // For a signed-in visitor: offer to add the games saved in this browser that the account does not have, and,
  // once the account has all of them, to remove this browser's copy. Nothing is sent or removed until they choose.
  function renderImport() {
    var panel = document.querySelector("[data-wishlist-import]");
    if (!panel || !wishlistStore || accountItems === null) { return; }
    var browserItems;
    try { browserItems = Wishlist.read(wishlistStore); } catch (_) { browserItems = []; }
    var missing = Wishlist.missingFrom(browserItems, accountItems);
    var names = missing.slice(0, 5).map(function (item) { return item.name; }).join(", ") +
      (missing.length > 5 ? " and " + (missing.length - 5) + " more" : "");
    panel.hidden = browserItems.length === 0;
    panel.querySelector("[data-wishlist-import-add]").hidden = missing.length === 0;
    panel.querySelector("[data-wishlist-import-clear]").hidden = missing.length > 0;
    panel.querySelector("[data-wishlist-import-text]").textContent = missing.length > 0
      ? "This browser also has " + gamesText(missing.length) + " saved that your account does not have: " + names + "."
      : "This browser still keeps its own copy of " + gamesText(browserItems.length) +
        " from its wishlist, and your account has every one of them.";
  }

  document.addEventListener("click", function (event) {
    var add = event.target.closest("[data-wishlist-import-add]");
    var clear = event.target.closest("[data-wishlist-import-clear]");
    var status = document.querySelector("[data-wishlist-import-status]");
    if (add && accountItems !== null) {
      add.disabled = true;
      var items = Wishlist.missingFrom(Wishlist.read(wishlistStore), accountItems);
      accountRequest("/api/wishlist/import", { items: items }).then(function (answer) {
        accountItems = answer.items;
        renderWishlist();
        status.textContent = "Added " + gamesText(answer.added) + " to your account" +
          (answer.not_added_full ? "; " + gamesText(answer.not_added_full) + " did not fit, because the wishlist is full" : "") +
          ". This browser keeps its own copy until you remove it.";
      }, function (error) { status.textContent = error.message; }).then(function () { add.disabled = false; });
    } else if (clear) {
      try { Wishlist.clear(wishlistStore); } catch (_) {
        status.textContent = "This browser's copy could not be removed.";
        return;
      }
      renderWishlist();
      status.textContent = "Removed this browser's copy. Your account keeps every game.";
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-wishlist-toggle]");
    if (!button) { return; }
    var root = button.closest(".details") || document;
    var status = root.querySelector("[data-wishlist-status]");
    if (inAccount) {
      if (accountItems === null) { return; }
      button.disabled = true;
      accountRequest("/api/wishlist/toggle", { id: buttonKey(button), name: button.dataset.name,
                                               aliases: buttonAliases(button) }).then(function (answer) {
        accountItems = answer.items;
        updateWishlistButton(root);
        if (status) { status.textContent = answer.saved ? "Added to your wishlist." : "Removed from your wishlist."; }
      }, function (error) { if (status) { status.textContent = error.message; } })
        .then(function () { button.disabled = false; });
      return;
    }
    if (!wishlistStore) { return; }
    try {
      var saved = Wishlist.toggle(wishlistStore, buttonKey(button), button.dataset.name, buttonAliases(button));
      updateWishlistButton(root);
      if (status) { status.textContent = saved ? "Added to your wishlist." : "Removed from your wishlist."; }
    } catch (_) { if (status) { status.textContent = "The wishlist could not be saved in this browser."; } }
  });

  // The account page's "Delete my account data": after a confirmation, delete it. The server signs the person out at
  // the same time, and the account page then says so.
  var deleteAccount = document.querySelector("[data-account-delete]");
  if (deleteAccount) {
    deleteAccount.addEventListener("click", function () {
      if (!window.confirm("Delete your account and its wishlist from this site? This cannot be undone.")) { return; }
      var status = document.querySelector("[data-account-delete-status]");
      deleteAccount.disabled = true;
      accountRequest("/api/account/delete", {}).then(function () {
        status.textContent = "Your account data was deleted.";
        window.location.assign("/account?notice=deleted");
      }, function (error) {
        deleteAccount.disabled = false;
        status.textContent = error.message;
      });
    });
  }

  updateWishlistButton(document);
  renderWishlist();
  if (inAccount) {
    loadAccount().then(function () {
      updateWishlistButton(document);
      renderWishlist();
    });
  }

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

  // A plain click: not a modified one (new tab, new window) and not another mouse button.
  function isPlainClick(event) {
    return event.button === 0 && !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
  }

  // The current address with one query parameter set (or removed, for an empty value), for history.replaceState: a
  // view changed in place keeps its own address, and the other parameters stay.
  function urlWith(name, value) {
    var url = new URL(window.location.href);
    if (value) { url.searchParams.set(name, value); } else { url.searchParams.delete(name); }
    return url.pathname + url.search;
  }

  // One selected game per scope, for the dashboard's linked charts: pointing at a mark ([data-game]) with a mouse or
  // pen, focusing it, or tapping it calls select(key); a click on the selected game, a second tap, or Enter opens its
  // details. `current()` gives the selected key.
  function trackSelection(scope, select, current) {
    var pressed = null; // the selection when a pointer went down on a game, for the click that follows
    scope.addEventListener("pointerover", function (event) {
      var mark = event.target.closest("[data-game]");
      if (mark && (event.pointerType === "mouse" || event.pointerType === "pen")) { select(mark.getAttribute("data-game")); }
    });
    scope.addEventListener("focusin", function (event) {
      var mark = event.target.closest("[data-game]");
      if (mark) { select(mark.getAttribute("data-game")); }
    });
    scope.addEventListener("pointerdown", function (event) {
      pressed = event.target.closest("[data-game]") ? { selected: current() } : null;
    });
    scope.addEventListener("click", function (event) {
      var mark = event.target.closest("[data-game]");
      var before = pressed;
      pressed = null;
      if (!mark || !isPlainClick(event)) { return; }
      var key = mark.getAttribute("data-game");
      if (before && before.selected !== key) { // a first tap, or a click before the pointer selected it
        event.preventDefault(); // stops the details dialog too (it skips handled clicks)
        select(key);
      }
    });
  }

  // Trending games → Upcoming releases: one game is always selected, and the panel beside the radar shows it (cover,
  // name, every platform's date, its list ranks and its Twitch place). Its marks on the radar, under "Date TBA or
  // approximate" and in the comparison's open group select it (trackSelection). The panel's copy of each game comes
  // from its server-rendered <template>. The window links show only the points due within a month or three months,
  // and the comparison's blocks open their group, in place; without JavaScript they reload with ?window= or ?group=.
  document.querySelectorAll("[data-upcoming]").forEach(function (scope) {
    var panel = scope.querySelector("[data-radar-panel]");
    var body = scope.querySelector("[data-radar-panel-body]");
    var radar = scope.querySelector("[data-radar]");
    var selected = panel ? panel.getAttribute("data-selected") : null;

    function marksOf(key) { return scope.querySelectorAll('[data-game="' + CSS.escape(key) + '"]'); }

    function showWindow(key) {
      if (!radar) { return; }
      radar.setAttribute("data-window", key);
      var first = null;
      radar.querySelectorAll("[data-windows]").forEach(function (point) {
        point.hidden = point.getAttribute("data-windows").split(" ").indexOf(key) < 0;
        if (!point.hidden && !first) { first = point; } // primary points come first, soonest game first
      });
      scope.querySelectorAll("[data-radar-window]").forEach(function (link) {
        if (link.getAttribute("data-radar-window") === key) { link.setAttribute("aria-current", "true"); }
        else { link.removeAttribute("aria-current"); }
      });
      window.history.replaceState(window.history.state, "", urlWith("window", key === "all" ? null : key));
      var shown = Array.prototype.some.call(marksOf(selected), function (mark) {
        return mark.hasAttribute("data-windows") && !mark.hidden;
      });
      if (!shown && first) { select(first.getAttribute("data-game")); } // the panel shows a game on the radar
    }

    function select(key) {
      if (!key || key === selected || !panel) { return; }
      var template = document.getElementById("radar-game-" + key);
      if (!template) { return; }
      body.replaceChildren(template.content.cloneNode(true)); // server-rendered by Jinja, which escapes every value
      body.querySelectorAll("img[data-image-fallback]").forEach(function (image) {
        image.addEventListener("error", function () { useImageFallback(image); }, { once: true });
      });
      marksOf(selected).forEach(function (mark) { mark.classList.remove("is-selected"); });
      marksOf(key).forEach(function (mark) { mark.classList.add("is-selected"); });
      panel.setAttribute("data-selected", key);
      selected = key;
      // A game picked in the comparison whose every point the window hides: show all tracked games again.
      var points = Array.prototype.filter.call(marksOf(key), function (mark) { return mark.hasAttribute("data-windows"); });
      if (points.length && points.every(function (point) { return point.hidden; })) { showWindow("all"); }
    }

    function showGroup(key) {
      scope.querySelectorAll("[data-compare-panel]").forEach(function (part) {
        part.hidden = part.getAttribute("data-compare-panel") !== key;
      });
      scope.querySelectorAll("[data-compare-group]").forEach(function (link) {
        if (link.getAttribute("data-compare-group") === key) { link.setAttribute("aria-current", "true"); }
        else { link.removeAttribute("aria-current"); }
      });
      window.history.replaceState(window.history.state, "", urlWith("group", key));
    }

    scope.addEventListener("click", function (event) {
      var windowLink = event.target.closest("[data-radar-window]");
      var groupLink = event.target.closest("[data-compare-group]");
      if (!isPlainClick(event) || !(windowLink || groupLink)) { return; }
      event.preventDefault();
      if (windowLink) { showWindow(windowLink.getAttribute("data-radar-window")); }
      else { showGroup(groupLink.getAttribute("data-compare-group")); }
    });
    trackSelection(scope, select, function () { return selected; });
  });

  // Chart tooltips (the reception scatterplots and the player line): pointing at or focusing a mark shows its name
  // or value and its figures. They repeat the mark's own text and the table view, so the tooltip is hidden from
  // assistive technology; Escape closes it. Without JavaScript a mark's title does the same.
  document.querySelectorAll("[data-chart-tips]").forEach(function (plot) {
    var holder = plot.closest(".chart-card") || plot.parentElement;
    var tip = document.createElement("div");
    var name = document.createElement("p");
    var figures = document.createElement("p");
    tip.className = "chart-tip";
    name.className = "chart-tip-name";
    figures.className = "chart-tip-text";
    tip.hidden = true;
    tip.setAttribute("aria-hidden", "true");
    tip.append(name, figures);
    holder.appendChild(tip);
    var current = null;

    function hide() { tip.hidden = true; current = null; }

    // Above the mark, or below it when the window has no room above; never past the card's sides.
    function show(mark) {
      if (mark === current) { return; }
      name.textContent = mark.getAttribute("data-tip-name"); // textContent: names come from stored data
      figures.textContent = mark.getAttribute("data-tip-text");
      tip.hidden = false;
      current = mark;
      var box = holder.getBoundingClientRect();
      var spot = mark.getBoundingClientRect();
      var left = spot.left - box.left + spot.width / 2 - tip.offsetWidth / 2;
      tip.style.left = Math.max(8, Math.min(left, box.width - tip.offsetWidth - 8)) + "px";
      var above = spot.top - tip.offsetHeight - 10 >= 0;
      tip.style.top = (above ? spot.top - box.top - tip.offsetHeight - 10 : spot.bottom - box.top + 10) + "px";
    }

    plot.querySelectorAll("[data-tip-name][title]").forEach(function (mark) { mark.removeAttribute("title"); });
    plot.addEventListener("pointerover", function (event) {
      var mark = event.target.closest("[data-tip-name]");
      if (mark) { show(mark); }
    });
    plot.addEventListener("pointerout", function (event) {
      if (current && !(event.relatedTarget && current.contains(event.relatedTarget))) { hide(); }
    });
    plot.addEventListener("focusin", function (event) {
      var mark = event.target.closest("[data-tip-name]");
      if (mark) { show(mark); }
    });
    plot.addEventListener("focusout", hide);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && current) { hide(); }
    });
    window.addEventListener("resize", hide);
  });

  // A scatterplot's names never cover each other: the page lists them most important first, and on a narrow screen
  // a name that would overlap one already shown is hidden. Its dot, tooltip and table still name the game. A name
  // that the chart's current view does not show (data-views) stays hidden.
  function fitLabels(plot) {
    var card = plot.closest("[data-view]");
    var view = card ? card.getAttribute("data-view") : null;
    var shown = [];
    plot.querySelectorAll(".plot-label").forEach(function (label) {
      var views = label.getAttribute("data-views");
      label.hidden = Boolean(view && views && views.split(" ").indexOf(view) < 0);
      if (label.hidden) { return; }
      var box = label.getBoundingClientRect();
      var clash = shown.some(function (other) {
        return box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top;
      });
      if (clash) { label.hidden = true; } else { shown.push(box); }
    });
  }
  var plots = document.querySelectorAll(".plot");
  plots.forEach(fitLabels);
  var fitTimer = 0;
  window.addEventListener("resize", function () {
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(function () { plots.forEach(fitLabels); }, 150);
  });

  // A scatterplot is one Tab stop: the left and right arrow keys move between its shown dots, which the page lists by
  // number of reviews, and Home and End go to the first and last. Without JavaScript every dot is a Tab stop.
  function shownItems(group) {
    return Array.prototype.filter.call(group.querySelectorAll("[data-roving-item]"), function (item) { return !item.hidden; });
  }
  // The tab stop of a scatterplot whose view changed: kept on its dot if that one still shows, else the first shown.
  function resetTabStop(group) {
    var items = shownItems(group);
    if (items.some(function (item) { return item.getAttribute("tabindex") === "0"; }) || !items.length) { return; }
    group.querySelectorAll("[data-roving-item]").forEach(function (item) { item.setAttribute("tabindex", "-1"); });
    items[0].setAttribute("tabindex", "0");
  }
  document.querySelectorAll("[data-roving]").forEach(function (group) {
    if (group.querySelectorAll("[data-roving-item]").length < 2) { return; }
    group.querySelectorAll("[data-roving-item]").forEach(function (item) { item.setAttribute("tabindex", "-1"); });
    resetTabStop(group);
    group.addEventListener("keydown", function (event) {
      var items = shownItems(group); // a view can hide some dots
      var index = items.indexOf(document.activeElement);
      var moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: items.length - 1 };
      if (index < 0 || !(event.key in moves)) { return; }
      event.preventDefault();
      var next = Math.max(0, Math.min(items.length - 1, moves[event.key]));
      items[index].setAttribute("tabindex", "-1");
      items[next].setAttribute("tabindex", "0");
      items[next].focus();
    });
  });

  // Trending games → Sales & reception: selecting a dot (trackSelection) fills the strip above the plot from the game's
  // server-rendered <template>, rings its peers (the other dots of its peer genre, data-genres) and draws their median
  // across the plot. The view links show every game or this year's only, in place; without JavaScript they reload
  // with ?view=. The axes never change. The doughnut above selects its games here too (receptionCharts).
  var receptionCharts = [];
  document.querySelectorAll("[data-reception]").forEach(function (card) {
    var plot = card.querySelector("[data-reception-plot]");
    var strip = card.querySelector("[data-bench-strip]");
    if (!plot || !strip) { return; }
    var dots = Array.prototype.slice.call(plot.querySelectorAll(".plot-dot[data-game]"));
    var line = plot.querySelector("[data-plot-median]");
    var lineLabel = plot.querySelector("[data-plot-median-label]");
    var selected = null;

    function dotOf(key) { return plot.querySelector('.plot-dot[data-game="' + CSS.escape(key) + '"]'); }

    function showView(view) {
      card.setAttribute("data-view", view);
      dots.forEach(function (dot) { dot.hidden = dot.getAttribute("data-views").split(" ").indexOf(view) < 0; });
      card.querySelectorAll("[data-reception-view]").forEach(function (link) {
        if (link.getAttribute("data-reception-view") === view) { link.setAttribute("aria-current", "true"); }
        else { link.removeAttribute("aria-current"); }
      });
      fitLabels(plot);
      resetTabStop(plot);
      window.history.replaceState(window.history.state, "", urlWith("view", view === "all" ? null : view));
    }

    function select(key) {
      var dot = dotOf(key);
      var template = document.getElementById("bench-" + key);
      if (!dot || !template || key === selected) { return; }
      if (dot.hidden) { showView("all"); } // a game picked in the doughnut that this year's view hides
      strip.replaceChildren(template.content.cloneNode(true)); // server-rendered by Jinja, which escapes every value
      var genre = template.getAttribute("data-peer-genre");
      dots.forEach(function (other) {
        other.classList.toggle("is-selected", other === dot);
        other.classList.toggle("is-peer", other !== dot && genre !== "" &&
          other.getAttribute("data-genres").split(" ").indexOf(genre) >= 0);
      });
      var median = template.getAttribute("data-median-y");
      line.hidden = median === null; // too few peers: no median to draw
      if (median !== null) {
        line.style.bottom = median + "%";
        lineLabel.textContent = template.getAttribute("data-median-label");
      }
      selected = key;
    }

    card.addEventListener("click", function (event) {
      var link = event.target.closest("[data-reception-view]");
      if (!link || !isPlainClick(event)) { return; }
      event.preventDefault();
      showView(link.getAttribute("data-reception-view"));
    });
    trackSelection(plot, select, function () { return selected; });
    receptionCharts.push({ has: function (key) { return Boolean(dotOf(key)); }, select: select });
  });

  // Trending games → Sales & reception: selecting a game in the doughnut or in its list dims
  // the other slices and selects the game's dot on the reception chart, when it has one. A game of "Other tracked
  // games" lights up that slice.
  document.querySelectorAll("[data-sales-ring]").forEach(function (card) {
    var ring = card.querySelector("[data-ring]");
    var selected = null;

    function select(key) {
      if (!key || key === selected || !ring) { return; }
      var mark = card.querySelector('[data-game="' + CSS.escape(key) + '"]');
      var slice = ring.querySelector('.ring-slice[data-game="' + CSS.escape(key) + '"]') ||
        (mark && mark.closest(".ring-row.is-other") ? ring.querySelector(".ring-slice.is-other") : null);
      card.querySelectorAll("[data-game]").forEach(function (other) {
        other.classList.toggle("is-selected", other === slice || (!other.classList.contains("ring-slice") &&
          other.getAttribute("data-game") === key));
      });
      ring.setAttribute("data-selected", key);
      selected = key;
      receptionCharts.forEach(function (chart) { if (chart.has(key)) { chart.select(key); } });
    }

    trackSelection(card, select, function () { return selected; });
  });

  // Trending games → Momentum: a game's bar in the waterfall shows that game's Steam players in the line chart beside
  // it, in place; without JavaScript the bar's link reloads the page with ?player=.
  document.querySelectorAll("[data-momentum]").forEach(function (canvas) {
    var chart = canvas.querySelector("[data-player-chart]");
    if (!chart) { return; }
    canvas.addEventListener("click", function (event) {
      var bar = event.target.closest("a[data-player]");
      if (!bar || !isPlainClick(event)) { return; }
      var key = bar.getAttribute("data-player");
      var panel = chart.querySelector('[data-player-panel="' + CSS.escape(key) + '"]');
      if (!panel) { return; }
      event.preventDefault();
      chart.querySelectorAll("[data-player-panel]").forEach(function (other) { other.hidden = other !== panel; });
      canvas.querySelectorAll("a[data-player]").forEach(function (other) {
        if (other === bar) { other.setAttribute("aria-current", "true"); } else { other.removeAttribute("aria-current"); }
      });
      window.history.replaceState(window.history.state, "", bar.getAttribute("href").split("#")[0]);
      if (chart.getBoundingClientRect().top > window.innerHeight) { chart.scrollIntoView({ block: "nearest" }); }
    });
  });

  // The header's icon controls (the wishlist star and the Daily Trivia lightbulb) show their name as a tip on hover
  // and keyboard focus. Escape hides a tip that is showing; the next hover or focus shows it again.
  var navIcons = document.querySelectorAll(".nav-icon");
  navIcons.forEach(function (control) {
    ["mouseenter", "mouseleave", "focus", "blur"].forEach(function (name) {
      control.addEventListener(name, function () { control.classList.remove("is-tip-hidden"); });
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { navIcons.forEach(function (control) { control.classList.add("is-tip-hidden"); }); }
  });

  // Daily Trivia: the lightbulb opens a popup with today's quiz and Browse Facts, over the current page. The server
  // renders the questions and facts (/trivia/panel); this code shows one card at a time, checks the answers, and keeps
  // progress and scores in this browser's storage (static/trivia.js). A Details link opens the game details dialog on
  // top of the popup (see below), so closing the details returns to the same card. Without JavaScript the lightbulb is
  // hidden (base.html).
  var Trivia = window.GameLibrarianTrivia;
  var triviaDialog = document.getElementById("trivia-dialog");
  var triviaButton = document.querySelector("[data-trivia-open]");
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  // "2026-11-21" -> "21 Nov 2026", the style of the server's dates.
  function dayLabel(day) {
    var parts = day.split("-");
    return Number(parts[2]) + " " + MONTHS[Number(parts[1]) - 1] + " " + parts[0];
  }

  function setUpTrivia(dialog, button) {
    var content = dialog.querySelector("[data-trivia-content]");
    var title = dialog.querySelector("#trivia-title");
    var badge = button.querySelector("[data-trivia-badge]");
    var storage = Trivia.open(window); // null when this browser does not let the site store data
    var store = storage || Trivia.memoryStore(); // then progress lasts until the page is closed
    var state = Trivia.read(store);
    var saveFailed = false;
    var view = null; // the loaded panel (setUpPanel)
    var factId = null; // the fact shown in Browse Facts, kept while this page is open

    // The lightbulb's badge: the quiz of `day` has a first score. Screen readers hear it as the button's description.
    function showBadge(day) {
      var done = Boolean(Trivia.firstScore(state, day));
      badge.hidden = !done;
      if (done) { button.setAttribute("aria-describedby", "trivia-done-note"); }
      else { button.removeAttribute("aria-describedby"); }
    }

    function save() {
      try {
        state = Trivia.save(store, state);
        saveFailed = false;
      } catch (_) { saveFailed = true; } // full or read-only storage: the quiz goes on, and the popup says so
      if (view) { view.showSaved(); }
    }

    // The quiz, its cards and its facts, once /trivia/panel is in the popup. Returns what the popup needs from it.
    function setUpPanel(root) {
      var quiz = {
        day: root.getAttribute("data-day"),
        id: root.getAttribute("data-quiz"),
        ids: root.getAttribute("data-ids").split(" "),
        answers: root.getAttribute("data-answers").split(" ").map(Number)
      };
      var nextReset = Date.parse(root.getAttribute("data-next-reset"));
      var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-trivia-tab]"));
      var panels = { quiz: root.querySelector('[data-trivia-panel="quiz"]'),
                     facts: root.querySelector('[data-trivia-panel="facts"]') };
      var cards = Array.prototype.slice.call(root.querySelectorAll("[data-trivia-question]"));
      var steps = root.querySelectorAll("[data-trivia-step]");
      var results = root.querySelector("[data-trivia-results]");
      var facts = Array.prototype.slice.call(root.querySelectorAll("[data-trivia-fact]"));
      var mode = "quiz";

      root.querySelectorAll("img[data-image-fallback]").forEach(function (image) {
        image.addEventListener("error", function () { useImageFallback(image); }, { once: true });
      });
      var localReset = new Date(nextReset).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      root.querySelectorAll("[data-trivia-local-reset]").forEach(function (span) {
        span.textContent = " (" + localReset + " your time)";
      });

      function choiceText(card, index) {
        return card.querySelector('[data-choice="' + index + '"] .trivia-choice-text').textContent;
      }

      // One question card as the attempt left it: before an answer, or with the answer marked and explained.
      function paintCard(card, index, attempt) {
        var chosen = attempt.answers[index];
        var correct = quiz.answers[index];
        var answered = chosen !== null;
        card.querySelectorAll("[data-choice]").forEach(function (choice) {
          var value = Number(choice.getAttribute("data-choice"));
          var isCorrect = answered && value === correct;
          var isWrong = answered && value === chosen && chosen !== correct;
          choice.classList.toggle("is-correct", isCorrect);
          choice.classList.toggle("is-wrong", isWrong);
          if (answered) { choice.setAttribute("aria-disabled", "true"); } else { choice.removeAttribute("aria-disabled"); }
          choice.querySelector("[data-trivia-mark]").textContent = isCorrect ?
            (value === chosen ? "Your answer, correct" : "Correct answer") : (isWrong ? "Your answer" : "");
        });
        card.querySelector("[data-trivia-feedback]").hidden = !answered;
        if (answered) {
          var verdict = card.querySelector("[data-trivia-verdict]");
          verdict.textContent = chosen === correct ? "Correct!" :
            "Not quite. The answer is “" + choiceText(card, correct) + "”.";
          verdict.classList.toggle("is-wrong", chosen !== correct);
        }
      }

      function paintResults(attempt) {
        var got = Trivia.score(attempt, quiz);
        var first = Trivia.firstScore(state, quiz.day);
        var top = Trivia.best(state);
        root.querySelector("[data-trivia-total]").textContent =
          (attempt.practice ? "Practice score: " : "Today’s score: ") + got + " of " + quiz.ids.length;
        root.querySelector("[data-trivia-first]").textContent = !first ? "" : (attempt.practice ?
          "Your first score today stays " + first.score + " of " + first.total + ": practice never changes it." :
          "Saved as today’s first score.");
        root.querySelector("[data-trivia-best]").textContent = !top ? "" : "Personal best: " + top.score + " of " +
          top.total + (top.day === quiz.day ? ", set today." : ", first set on " + dayLabel(top.day) + ".");
      }

      function renderQuiz(focus) {
        var attempt = Trivia.current(state, quiz);
        var count = quiz.ids.length;
        var finished = attempt.position >= count;
        cards.forEach(function (card, index) {
          card.hidden = index !== attempt.position;
          paintCard(card, index, attempt);
        });
        steps.forEach(function (step, index) {
          var choice = attempt.answers[index];
          step.className = choice === null ? (index === attempt.position ? "is-current" : "") :
            (choice === quiz.answers[index] ? "is-right" : "is-wrong");
        });
        root.querySelector("[data-trivia-position]").textContent = (attempt.practice ? "Practice · " : "") +
          (finished ? "All " + count + " answered" : "Question " + (attempt.position + 1) + " of " + count);
        root.querySelector("[data-trivia-score]").textContent = "Score " + Trivia.score(attempt, quiz);
        results.hidden = !finished;
        if (finished) { paintResults(attempt); }
        var before = Trivia.playedBefore(state, quiz); // a repeated quiz the visitor played on an earlier day
        var played = root.querySelector("[data-trivia-played]");
        played.hidden = !before;
        if (before) {
          played.textContent = "You played this quiz on " + dayLabel(before.day) + " and scored " + before.score +
            " of " + before.total + ".";
        }
        if (focus) { // the question, its verdict when it is answered, or the score
          var card = cards[attempt.position];
          (finished ? root.querySelector("[data-trivia-total]") : attempt.answers[attempt.position] === null ?
            card.querySelector(".trivia-question") : card.querySelector("[data-trivia-verdict]")).focus();
        }
      }

      // Browse Facts: the facts of earlier quizzes, and today's once today's quiz has a first score.
      function eligibleFacts() {
        var done = Boolean(Trivia.firstScore(state, quiz.day));
        return facts.filter(function (card) { return done || !card.hasAttribute("data-today"); });
      }

      function factIndex(list) {
        for (var i = 0; i < list.length; i++) {
          if (list[i].getAttribute("data-trivia-fact") === factId) { return i; }
        }
        return 0;
      }

      function renderFacts() {
        var list = eligibleFacts();
        var focusWasInFact = facts.some(function (card) { return card.contains(document.activeElement); });
        root.querySelector("[data-trivia-browse]").hidden = list.length === 0;
        root.querySelector("[data-trivia-empty]").hidden = list.length > 0;
        facts.forEach(function (card) { card.hidden = true; });
        if (!list.length) { factId = null; return; }
        var index = factIndex(list);
        var card = list[index];
        card.hidden = false;
        factId = card.getAttribute("data-trivia-fact");
        root.querySelector("[data-trivia-fact-count]").textContent = "Fact " + (index + 1) + " of " + list.length +
          ": " + card.getAttribute("data-game-title");
        if (focusWasInFact) { card.querySelector(".trivia-fact-title").focus(); } // its old card is hidden now
      }

      function stepFact(delta) {
        var list = eligibleFacts();
        if (!list.length) { return; }
        var next = (factIndex(list) + delta + list.length) % list.length; // the ends wrap around
        factId = list[next].getAttribute("data-trivia-fact");
        renderFacts();
      }

      // Switch the mode. `focus`: "content" (the question or the score), "tab" (the mode's tab) or nothing.
      function show(name, focus) {
        mode = name;
        tabs.forEach(function (tab) {
          var selected = tab.getAttribute("data-trivia-tab") === name;
          tab.setAttribute("aria-selected", String(selected));
          tab.tabIndex = selected ? 0 : -1;
          if (selected && focus === "tab") { tab.focus(); }
        });
        panels.quiz.hidden = name !== "quiz";
        panels.facts.hidden = name !== "facts";
        if (name === "quiz") { renderQuiz(focus === "content"); } else { renderFacts(); }
      }

      function showSaved() {
        var label = root.querySelector("[data-trivia-saved]");
        label.textContent = !storage ? "Not saved: this browser blocks site storage, so progress lasts until you " +
          "leave the page" : (saveFailed ? "Not saved: this browser could not store your progress" :
          "Saved on this device");
        label.classList.toggle("is-warning", !storage || saveFailed);
      }

      root.addEventListener("click", function (event) {
        var target = event.target;
        var choice = target.closest("[data-choice]");
        if (choice) {
          if (choice.getAttribute("aria-disabled") === "true") { return; }
          var result = Trivia.choose(state, quiz, Number(choice.getAttribute("data-choice")));
          if (!result) { return; }
          save();
          renderQuiz(false);
          choice.closest("[data-trivia-question]").querySelector("[data-trivia-verdict]").focus();
          if (result.finished) { showBadge(quiz.day); }
          return;
        }
        if (target.closest("[data-trivia-next]")) {
          Trivia.advance(state, quiz);
          save();
          renderQuiz(true);
        } else if (target.closest("[data-trivia-replay]")) {
          Trivia.replay(state, quiz);
          save();
          renderQuiz(true);
        } else if (target.closest("[data-trivia-tab]")) {
          show(target.closest("[data-trivia-tab]").getAttribute("data-trivia-tab"), null);
        } else if (target.closest("[data-trivia-goto]")) {
          show(target.closest("[data-trivia-goto]").getAttribute("data-trivia-goto"), "tab");
        } else if (target.closest("[data-trivia-step-by]")) {
          stepFact(Number(target.closest("[data-trivia-step-by]").getAttribute("data-trivia-step-by")));
        }
      });

      // The arrow keys move between the two tabs; in Browse Facts they also move between facts.
      function onKey(event) {
        if (event.altKey || event.ctrlKey || event.metaKey) { return; }
        var tab = event.target.closest && event.target.closest("[data-trivia-tab]");
        if (tab && ["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(event.key) >= 0) {
          event.preventDefault();
          var at = tabs.indexOf(tab);
          var next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
            (at + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
          show(tabs[next].getAttribute("data-trivia-tab"), "tab");
        } else if (mode === "facts" && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
          event.preventDefault();
          stepFact(event.key === "ArrowLeft" ? -1 : 1);
        }
      }

      showBadge(quiz.day);
      showSaved();
      return { nextReset: nextReset, show: show, showSaved: showSaved, onKey: onKey };
    }

    function load() {
      view = null;
      content.innerHTML = '<p class="dialog-status">Loading today’s quiz…</p>';
      fetch("/trivia/panel", { headers: { Accept: "text/html" } })
        .then(function (response) {
          if (!response.ok) { throw new Error("HTTP " + response.status); }
          return response.text();
        })
        .then(function (html) {
          content.innerHTML = html; // server-rendered by Jinja, which escapes every value
          var root = content.querySelector("[data-trivia]");
          view = root ? setUpPanel(root) : null;
          // Focus moves to the question only when it is still on the popup's title (set when it opened).
          if (view) { view.show("quiz", dialog.open && document.activeElement === title ? "content" : null); }
        })
        .catch(function () {
          content.innerHTML = '<p class="dialog-status">Daily Trivia could not be loaded. ' +
            '<button class="button" type="button" data-trivia-retry>Try again</button></p>';
        });
    }

    button.addEventListener("click", function () {
      if (dialog.open) { return; }
      dialog.showModal(); // the page stays where it was, behind the popup; focus goes to the popup's title
      document.documentElement.classList.add("has-trivia");
      // Today's Quiz first. The panel is loaded on the first open, and again once the next quiz has started.
      if (!view || Date.now() >= view.nextReset) { load(); } else { view.show("quiz", "content"); }
    });
    dialog.addEventListener("close", function () {
      document.documentElement.classList.remove("has-trivia");
      button.focus(); // every way of closing (Escape, the close button, the backdrop) returns to the lightbulb
    });
    dialog.querySelector("[data-trivia-close]").addEventListener("click", function () { dialog.close(); });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) { dialog.close(); } // a click on the backdrop
      else if (event.target.closest("[data-trivia-retry]")) { load(); }
    });
    dialog.addEventListener("keydown", function (event) { if (view) { view.onKey(event); } });
    showBadge(button.getAttribute("data-trivia-day"));
  }

  if (triviaButton && Trivia && triviaDialog && typeof triviaDialog.showModal === "function") {
    setUpTrivia(triviaDialog, triviaButton);
  } else if (triviaButton) {
    triviaButton.hidden = true; // a browser too old for <dialog>
  }

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
