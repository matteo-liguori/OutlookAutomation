"use strict";
// The viewer page. It builds every element with textContent and never parses markup, so nothing
// from a mail or a note can become markup. The token comes from the URL fragment, is kept only
// in this variable, and is removed from the address bar at once.
(function () {
  var token = "";
  var hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (hash.get("token")) {
    token = hash.get("token");
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

  function post(path, body, done) {
    api("POST", path, body).then(done, function (error) {
      main.insertBefore(problem(error), main.firstChild);
    });
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
      var box = el("div", {});
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

  function grantsList(states, empty) {
    var box = el("div", {});
    var queries = states.map(function (state) { return api("GET", "/api/grants?state=" + state); });
    Promise.all(queries).then(function (results) {
      var rows = [];
      results.forEach(function (result) { rows = rows.concat(result.grants); });
      if (!rows.length) box.appendChild(el("p", { class: "muted", text: empty }));
      rows.forEach(function (row) { box.appendChild(grantCard(row)); });
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  // --- views -----------------------------------------------------------------------------

  function uncertainCards(box) {
    api("GET", "/api/uncertain").then(function (data) {
      data.operations.forEach(function (op) {
        var card = el("section", { class: "card" }, el("h3", { text: op.kind + " (" + op.state + ")" }));
        card.appendChild(el("div", { class: "muted", text: op.source + ", sent at " + when(op.dispatched_at) }));
        card.appendChild(el("pre", { text: op.rendered || "(its content has been compacted)" }));
        card.appendChild(el("p", { class: "muted", text: "Check your Sent folder, then tell the harness what happened." }));
        ["happened", "did_not_happen"].forEach(function (outcome) {
          card.appendChild(
            el("button", {
              class: "act",
              text: outcome === "happened" ? "It happened" : "It did not happen",
              onclick: function () { post("/api/operations/" + op.handle + "/resolve", { outcome: outcome }, render); },
            })
          );
        });
        box.appendChild(card);
      });
    }, function (error) { box.appendChild(problem(error)); });
  }

  function needsView() {
    var box = el("div", {});
    box.appendChild(el("h2", { text: "Waiting for your decision" }));
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

  function todayView() {
    var box = el("div", {});
    api("GET", "/api/activity").then(function (data) {
      box.appendChild(el("h2", { text: "What the assistant did on " + data.day }));
      var names = Object.keys(data.tool_calls);
      box.appendChild(
        el("p", { class: "muted", text: names.length ? names.map(function (n) { return n + " x" + data.tool_calls[n]; }).join(", ") : "No tool calls." })
      );
      var table = el("table", {}, el("tr", {}, el("th", { text: "When" }), el("th", { text: "Who" }), el("th", { text: "What" })));
      data.events.forEach(function (row) {
        table.appendChild(el("tr", {}, el("td", { text: when(row.at) }), el("td", { text: row.actor }), el("td", { text: row.kind + ": " + row.summary })));
      });
      box.appendChild(table);
    }, function (error) { box.appendChild(problem(error)); });
    return box;
  }

  function workView() {
    var box = el("div", {});
    api("GET", "/api/work").then(function (data) {
      box.appendChild(el("h2", { text: "Actions due this week" }));
      if (!data.actions.length) box.appendChild(el("p", { class: "muted", text: "None." }));
      data.actions.forEach(function (a) {
        box.appendChild(el("section", { class: "card" }, el("h3", { text: a.title }), el("div", { class: "muted", text: "Due " + when(a.due_at) + ", card: " + a.card_title })));
      });
      box.appendChild(el("h2", { text: "Open cards" }));
      data.cards.forEach(function (c) {
        box.appendChild(el("section", { class: "card" }, el("h3", { text: c.title }), el("div", { class: "muted", text: c.open_actions + " open actions" + (c.over_cap ? ", over its size cap" : "") })));
      });
    }, function (error) { box.appendChild(problem(error)); });
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
    ["grants", "Grants", grantsView],
    ["today", "Today", todayView],
    ["work", "Cards", workView],
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

  if (token) {
    start();
  } else {
    var input = el("input", { type: "password", "aria-label": "Viewer token" });
    show(el("div", {}, el("h2", { text: "Viewer token" }), el("p", { class: "muted", text: "Paste the contents of the viewer.token file from the run folder of your data folder." }), input,
      el("button", { class: "act primary", text: "Open", onclick: function () { token = input.value.trim(); start(); } })));
  }
})();
