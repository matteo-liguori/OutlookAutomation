"use strict";
// The viewer page. It builds every element with textContent and never parses markup, so nothing
// from a mail, a calendar or a note can become markup. The token comes from the URL fragment and
// is removed from the address bar at once. It is kept in this variable and in the tab's
// sessionStorage, so a reload of the tab works; a new browser session needs the token URL again.
// calendar.js adds the calendar page through `window.ChamberlainViewer.register`; complex.js adds
// to the Needs you and Cards pages through `window.ChamberlainViewer.sections`.
(function () {
  var TOKEN_KEY = "chamberlain.viewer.token";
  var token = "";

  // --- the token for this tab (kept in sessionStorage; every access may fail) -------------
  function rememberedToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (_) {
      return "";
    }
  }

  function rememberToken(value) {
    try {
      if (value) window.sessionStorage.setItem(TOKEN_KEY, value);
      else window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (_) {
      /* storage may be blocked: the page still works until it is reloaded */
    }
  }

  var hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (hash.get("token")) {
    token = hash.get("token");
    rememberToken(token);
  } else {
    token = rememberedToken();
  }
  history.replaceState(null, "", window.location.pathname);

  var main = document.getElementById("main");
  var tabsBar = document.getElementById("tabs");
  var current = "needs";

  function el(tag, props) {
    var node = document.createElement(tag);
    var rest = Array.prototype.slice.call(arguments, 2);
    Object.keys(props || {}).forEach(function (key) {
      if (key === "text") node.textContent = props[key];
      else if (key === "class") node.className = props[key];
      else if (key === "onclick") node.addEventListener("click", props[key]);
      else node.setAttribute(key, props[key]);
    });
    rest.forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function api(method, path, body) {
    var options = { method: method, headers: { "X-Chamberlain-Token": token } };
    if (method === "POST") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
    }
    return fetch(path, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var failure = new Error(data.message || "That did not work.");
          failure.status = response.status;
          if (response.status === 403 && rememberedToken()) {
            // The viewer was restarted: the saved token is old. Forget it and ask again.
            rememberToken("");
            askToken("The saved token no longer works. Paste the current one.");
          }
          throw failure;
        }
        return data;
      });
    });
  }

  function clear() {
    while (main.firstChild) main.removeChild(main.firstChild);
  }

  function show(node) {
    clear();
    main.appendChild(node);
  }

  function problem(error) {
    return el("p", { class: "warn", text: error.message });
  }

  function when(stamp) {
    return stamp ? stamp.replace("T", " ").replace("Z", " UTC") : "never";
  }

  function day(stamp) {
    return stamp ? stamp.slice(0, 10) : "";
  }

  function post(path, body, done) {
    api("POST", path, body).then(done, function (error) {
      main.insertBefore(problem(error), main.firstChild);
    });
  }

  function empty(text) {
    return el("div", { class: "empty", text: text });
  }

  // --- grants ----------------------------------------------------------------------------

  function grantCard(summary) {
    return el(
      "section",
      { class: "card" },
      el("h3", { text: summary.title }),
      el("div", { class: "muted", text: summary.kind + ", " + summary.state + ", " + summary.source }),
      el("div", { class: "muted", text: "Review due: " + when(summary.review_at) }),
      el("button", { class: "act", text: "Open", onclick: function () { openGrant(summary.handle); } })
    );
  }

  function openGrant(handle) {
    api("GET", "/api/grants/" + encodeURIComponent(handle)).then(function (grant) {
      var box = el("section", { class: "panel" });
      box.appendChild(el("h2", { text: grant.title }));
      box.appendChild(
        el("p", { class: "muted", text: grant.kind + ", " + grant.state + ", needs " + grant.requirement + ", source " + grant.source })
      );
      box.appendChild(el("p", { text: "This is exactly what you would be allowing:" }));
      box.appendChild(el("pre", { text: grant.rendered || "(nothing to show: do not confirm)" }));
      box.appendChild(el("p", { class: "muted" }, "Content fingerprint: ", el("code", { text: grant.content_sha256 })));
      var buttons = el("p", {});
      if (grant.state === "proposed" && grant.rendered) {
        buttons.appendChild(
          el("button", {
            class: "act primary",
            text: "Confirm exactly this",
            onclick: function () {
              post("/api/grants/" + handle + "/confirm", { content_sha256: grant.content_sha256 }, function () { openGrant(handle); });
            },
          })
        );
        buttons.appendChild(
          el("button", { class: "act", text: "Decline", onclick: function () { post("/api/grants/" + handle + "/decline", {}, function () { render(); }); } })
        );
      }
      if (grant.state === "active" || grant.state === "suspended") {
        buttons.appendChild(
          el("button", { class: "act", text: "Keep (reviewed)", onclick: function () { post("/api/grants/" + handle + "/keep", {}, function () { openGrant(handle); }); } })
        );
        buttons.appendChild(
          el("button", { class: "act danger", text: "Revoke", onclick: function () { post("/api/grants/" + handle + "/revoke", {}, function () { openGrant(handle); }); } })
        );
      }
      buttons.appendChild(el("button", { class: "act", text: "Back", onclick: function () { render(); } }));
      box.appendChild(buttons);
      show(box);
    }, function (error) { show(problem(error)); });
  }

  function grantsList(states, emptyText) {
    var box = el("div", {});
    var queries = states.map(function (state) { return api("GET", "/api/grants?state=" + state); });
    Promise.all(queries).then(function (results) {
      var rows = [];
      results.forEach(function (result) { rows = rows.concat(result.grants); });
      if (!rows.length) box.appendChild(el("p", { class: "muted", text: emptyText }));
      rows.forEach(function (row) { box.appendChild(grantCard(row)); });
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  // --- uncertain operations ---------------------------------------------------------------

  var KIND_NAMES = {
    "mail.send": "Email send",
    "mail.draft_save": "Draft save",
    "mail.draft_update": "Draft update",
    "mail.draft_discard": "Draft discard",
    "mail.move": "Email move",
    "mail.trash": "Email trash",
    "mail.mark_read": "Mark read",
    "calendar.create": "Calendar event created",
    "calendar.update": "Calendar event changed",
    "calendar.delete": "Calendar event deleted",
    "calendar.respond": "Calendar invitation answered",
  };

  // What the one look after a day found, and the resend that replaced or will replace the send.
  function resendLines(op) {
    var lines = [];
    if (op.recheck) lines.push(el("p", { text: "Looked again at " + when(op.rechecked_at) + ": " + op.recheck + "." }));
    if (op.redo_of) lines.push(el("p", { class: "muted", text: "This is a second try of " + op.redo_of + "." }));
    if (op.resend) lines.push(el("p", { class: "muted", text: "Sending it again is in hand: " + op.resend.handle + " (" + op.resend.state + ")." }));
    return lines;
  }

  function uncertainCard(op) {
    var s = op.summary || {};
    var title = s.title || KIND_NAMES[op.kind] || op.kind;
    var card = el("section", { class: "card" }, el("h3", { text: title }));
    card.appendChild(el("div", { class: "muted", text: (KIND_NAMES[op.kind] || op.kind) + " on " + op.source + " (" + op.state + ")" }));
    var facts = el("ul", { class: "facts" });
    if (s.start) facts.appendChild(el("li", { text: "When: " + (s.start.length === 10 ? s.start : when(s.start)) }));
    if (s.calendar) facts.appendChild(el("li", { text: "Calendar: " + s.calendar }));
    if (s.to && s.to.length) facts.appendChild(el("li", { text: "To: " + s.to.join(", ") }));
    if (s.cc && s.cc.length) facts.appendChild(el("li", { text: "Cc: " + s.cc.join(", ") }));
    if (facts.firstChild) card.appendChild(facts);
    if (op.attempted_at) {
      card.appendChild(el("p", { text: "Tried at " + when(op.attempted_at) + (op.imported ? " (from the old journal)." : ".") }));
    } else if (op.imported) {
      card.appendChild(el("p", { class: "muted", text: "The old journal does not say when it was tried. It was imported at " + when(op.imported_at) + "." }));
    }
    if (op.rendered) card.appendChild(el("pre", { text: op.rendered }));
    card.appendChild(el("p", { class: "advice", text: op.advice }));
    resendLines(op).forEach(function (line) { card.appendChild(line); });
    card.appendChild(el("p", { class: "muted", text: op.kind === "mail.send" && !op.imported ? "The assistant decides whether to send it again, and tells you. You can settle it now:" : "The assistant looks for it itself and closes it if nothing can send it again. You can settle it now:" }));
    [["happened", "It happened"], ["did_not_happen", "It did not happen"]].forEach(function (pair) {
      card.appendChild(
        el("button", {
          class: "act",
          text: pair[1],
          onclick: function () { post("/api/operations/" + op.handle + "/resolve", { outcome: pair[0] }, render); },
        })
      );
    });
    return card;
  }

  function uncertainCards(box) {
    api("GET", "/api/uncertain").then(function (data) {
      if (!data.operations.length) box.appendChild(el("p", { class: "muted", text: "Nothing is waiting to be settled." }));
      data.operations.forEach(function (op) { box.appendChild(uncertainCard(op)); });
    }, function (error) { box.appendChild(problem(error)); });
  }

  // --- views -----------------------------------------------------------------------------

  // Pieces other scripts add to a page: `sections.needs` and `sections.card` hold functions.
  var sections = { needs: [], card: [] };

  function runSections(name, node, data) {
    sections[name].forEach(function (build) {
      try {
        build(node, data);
      } catch (_) {
        /* one broken section must not take the page down */
      }
    });
  }

  function needsView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Waiting for your decision" }));
    runSections("needs", box);
    box.appendChild(grantsList(["proposed"], "Nothing is waiting for you to confirm."));
    box.appendChild(el("h2", { text: "Operations that may or may not have happened" }));
    uncertainCards(box);
    box.appendChild(el("h2", { text: "Overview" }));
    api("GET", "/api/overview").then(function (data) {
      box.appendChild(
        el("p", { class: "muted", text:
          data.counts.active_grants + " active grants, " + data.counts.open_cards + " open cards, " +
          data.needs_you.grants_to_review + " grants to review. Version " + data.version + "." })
      );
      var sources = Object.keys(data.known_recipients || {});
      if (sources.length) {
        box.appendChild(
          el("p", { class: "muted", text: "Known recipients: " + sources.map(function (s) {
            var c = data.known_recipients[s];
            return s + " " + (c["confirmed-grant"] + c["sent-history"] + c.own) + " (" + c["sent-history"] + " from Sent history)";
          }).join("; ") + "." })
        );
      }
    });
    return box;
  }

  function grantsView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Grants in force" }));
    box.appendChild(grantsList(["active", "suspended"], "No grants are in force."));
    box.appendChild(el("h2", { text: "Recently ended" }));
    box.appendChild(grantsList(["spent", "revoked", "expired", "declined", "lapsed"], "None."));
    return box;
  }

  function cardsView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Open cards" }));
    var grid = el("div", { class: "grid" });
    box.appendChild(grid);
    api("GET", "/api/cards").then(function (data) {
      if (!data.cards.length) { box.replaceChild(empty("No open cards."), grid); return; }
      data.cards.forEach(function (c) {
        var strip = c.status === "over its size cap" ? "cap" : c.status === "action overdue" ? "overdue" : c.status === "review due" ? "review" : "";
        var card = el("section", { class: "thread-card" }, el("span", { class: "colour-strip " + strip }));
        card.appendChild(el("div", { class: "card-top" }, el("span", { class: "status-label", text: c.status }), el("span", { text: c.open_actions + (c.open_actions === 1 ? " action" : " actions") })));
        card.appendChild(el("span", { class: "thread-title", text: c.title }));
        if (c.goal) card.appendChild(el("span", { class: "purpose", text: c.goal }));
        if (c.position) card.appendChild(el("span", { class: "purpose", text: c.position }));
        if (c.actions.length) {
          var preview = el("div", { class: "action-preview" });
          c.actions.forEach(function (a) {
            preview.appendChild(el("div", { class: "action-line", text: (a.due_at ? day(a.due_at) + ": " : "") + a.title }));
          });
          if (c.open_actions > c.actions.length) preview.appendChild(el("span", { class: "more", text: "and " + (c.open_actions - c.actions.length) + " more" }));
          card.appendChild(preview);
        }
        runSections("card", card, c);
        card.appendChild(el("div", { class: "card-date" }, el("span", { text: "Updated " + day(c.updated_at) }), c.review_at ? el("span", { class: "badge", text: "Review " + day(c.review_at) }) : null));
        grid.appendChild(card);
      });
    }, function (error) { box.replaceChild(problem(error), grid); });
    return box;
  }

  function timelineView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Timeline" }));
    var list = el("div", { class: "timeline" });
    box.appendChild(list);
    api("GET", "/api/timeline?days=14").then(function (data) {
      if (!data.days.length) { list.appendChild(empty("Nothing happened in the last two weeks.")); return; }
      var names = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "short", timeZone: "UTC" });
      data.days.forEach(function (d) {
        var items = el("div", { class: "day-items" });
        d.items.forEach(function (item) {
          items.appendChild(
            el("div", { class: "event " + item.kind },
              el("div", { class: "event-top" }, el("span", { class: "kind", text: item.kind === "action" ? "Action closed" : "Harness" }), el("span", { text: when(item.at) })),
              el("h3", { text: item.title }),
              el("div", { class: "event-bottom" }, el("span", { text: item.detail }), item.card ? el("span", { text: item.card }) : null))
          );
        });
        list.appendChild(el("div", { class: "day" }, el("div", { class: "day-title" }, el("strong", { text: names.format(new Date(d.day + "T00:00:00Z")) })), items));
      });
    }, function (error) { list.appendChild(problem(error)); });
    return box;
  }

  function runsView() {
    var box = el("div", {});
    api("GET", "/api/runs").then(function (data) {
      box.appendChild(el("h2", { text: "Schedule" }));
      if (!data.schedules.length) box.appendChild(el("p", { class: "muted", text: "No passes are scheduled." }));
      data.schedules.forEach(function (s) {
        box.appendChild(el("p", { text: s.name + ": " + s.kind + " at " + s.at_local + " " + s.zone + (s.enabled ? "" : " (off)") }));
      });
      box.appendChild(el("h2", { text: "Runs and reports" }));
      data.runs.forEach(function (r) {
        var card = el("section", { class: "card" }, el("h3", { text: r.schedule + " on " + r.day }), el("div", { class: r.state === "finished" ? "ok" : "muted", text: r.state }));
        if (r.reports) {
          card.appendChild(el("button", { class: "act", text: "Read the report", onclick: function () { openReport(r.handle); } }));
        }
        box.appendChild(card);
      });
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  function openReport(handle) {
    api("GET", "/api/runs/" + handle + "/reports").then(function (data) {
      var box = el("div", {}, el("h2", { text: data.schedule + " on " + data.day }));
      data.reports.forEach(function (report) { box.appendChild(el("pre", { text: report.text })); });
      if (!data.reports.length) box.appendChild(el("p", { class: "muted", text: "The report has expired." }));
      box.appendChild(el("button", { class: "act", text: "Back", onclick: function () { render(); } }));
      show(box);
    }, function (error) { show(problem(error)); });
  }

  function storageView() {
    var box = el("div", {});
    api("GET", "/api/storage").then(function (s) {
      box.appendChild(el("h2", { text: "Storage" }));
      var table = el("table", {});
      [
        ["Stored items", s.artifacts_live],
        ["Stored bytes", s.artifact_bytes],
        ["Items nobody owns", s.artifacts_without_owner],
        ["Unknown files set aside", s.orphans_quarantined],
        ["Database bytes", s.db_bytes],
        ["Data folder bytes", s.data_root_bytes],
        ["Open cards", s.open_cards],
        ["Over budget", s.over_budget === "0" ? "no" : s.over_budget],
      ].forEach(function (row) {
        table.appendChild(el("tr", {}, el("th", { text: row[0] }), el("td", { text: String(row[1]) })));
      });
      box.appendChild(table);
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  function notesView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Leave a note for the assistant" }));
    var input = el("input", { type: "text", maxlength: "150", "aria-label": "Note", placeholder: "One short line (150 characters)" });
    box.appendChild(input);
    box.appendChild(el("button", { class: "act primary", text: "Send note", onclick: function () {
      post("/api/notes", { text: input.value }, render);
    } }));
    api("GET", "/api/notes").then(function (data) {
      data.notes.forEach(function (n) {
        var card = el("section", { class: "card" }, el("div", { text: n.text }), el("div", { class: "muted", text: when(n.created_at) + ", " + n.state }));
        if (n.state === "open") {
          card.appendChild(el("button", { class: "act", text: "Withdraw", onclick: function () { post("/api/notes/" + n.id + "/withdraw", {}, render); } }));
        }
        box.appendChild(card);
      });
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  var VIEWS = [
    ["needs", "Needs you", needsView],
    ["cards", "Cards", cardsView],
    ["timeline", "Timeline", timelineView],
    ["grants", "Grants", grantsView],
    ["runs", "Runs and reports", runsView],
    ["storage", "Storage", storageView],
    ["notes", "Notes", notesView],
  ];

  function render() {
    clear();
    VIEWS.forEach(function (entry) {
      if (entry[0] === current) main.appendChild(entry[2]());
    });
    Array.prototype.forEach.call(tabsBar.children, function (button) {
      button.setAttribute("aria-current", button.dataset.view === current ? "true" : "false");
    });
  }

  function start() {
    VIEWS.forEach(function (entry) {
      var button = el("button", { text: entry[1], "data-view": entry[0], onclick: function () { current = entry[0]; render(); } });
      tabsBar.appendChild(button);
    });
    render();
  }

  // calendar.js registers its page here, after the second tab (`after` names the tab before it).
  window.ChamberlainViewer = {
    el: el,
    api: api,
    post: post,
    render: function () { render(); },
    sections: sections,
    when: when,
    problem: problem,
    empty: empty,
    register: function (id, title, build, after) {
      var at = VIEWS.findIndex(function (entry) { return entry[0] === after; });
      VIEWS.splice(at < 0 ? VIEWS.length : at + 1, 0, [id, title, build]);
    },
  };

  function askToken(message) {
    var input = el("input", { type: "password", "aria-label": "Viewer token" });
    show(el("div", {}, el("h2", { text: "Viewer token" }),
      el("p", { class: message ? "warn" : "muted", text: message || "Paste the contents of the viewer.token file from the run folder of your data folder." }),
      input,
      el("button", { class: "act primary", text: "Open", onclick: function () {
        token = input.value.trim();
        rememberToken(token);
        tabsBar.textContent = "";
        start();
      } })));
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (token) {
      start();
    } else {
      askToken("");
    }
  });
})();
