/* Game Librarian: small progressive enhancements. Every feature also works without JavaScript:
   filters have an Apply button, and each Details link opens a full details page. */
(function () {
  "use strict";

  document.documentElement.classList.add("js");

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

  function openGame(appid, push) {
    var thisRequest = ++request;
    content.innerHTML = '<p class="dialog-status">Loading…</p>';
    if (!dialog.open) {
      dialog.showModal();
      document.documentElement.classList.add("has-dialog");
    }
    if (push) {
      window.history.pushState({ game: appid }, "", urlWithGame(appid));
      pushedHistory = true;
    }
    fetch("/games/" + encodeURIComponent(appid) + "/panel", { headers: { Accept: "text/html" } })
      .then(function (response) { return response.text(); })
      .then(function (html) {
        if (thisRequest !== request) { return; } // a newer request replaced this one
        content.innerHTML = html; // server-rendered by Jinja, which escapes every value
        setUpClip(content);
      })
      .catch(function () {
        if (thisRequest !== request) { return; }
        content.innerHTML = '<p class="dialog-status">The details could not be loaded. ' +
          '<a class="text-link" href="/games/' + encodeURIComponent(appid) + '">Open the details page</a>.</p>';
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
    opener = link;
    openGame(link.getAttribute("data-details"), true);
  });

  window.addEventListener("popstate", function () {
    var appid = currentGame();
    if (appid) { openGame(appid, false); }
    else if (dialog.open) { closingFromHistory = true; dialog.close(); }
  });

  // A link with ?game=<appid> opens straight into that game's details.
  var initial = currentGame();
  if (initial) {
    opener = document.querySelector('a[data-details="' + CSS.escape(initial) + '"]');
    openGame(initial, false);
  }
})();
