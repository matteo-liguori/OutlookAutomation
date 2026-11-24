"use strict";
// The Live test page: recent `chamberlain live-test` runs, each with its score and, when opened,
// every scenario with its result and reason. Everything is drawn with textContent only; a report
// holds handles, codes and timings, never mail or event text.
(function () {
  var V = window.ChamberlainViewer;
  if (!V) return;
  var el = V.el;

  var LABEL = { pass: "pass", fail: "FAIL", skipped: "skipped" };

  function scoreLine(run) {
    var s = run.score;
    return s.percent + "% (" + s.passed + " passed, " + s.failed + " failed, " + s.skipped +
      " skipped; coverage " + s.coverage_percent + "%)" + (run.leftovers ? "; " + run.leftovers + " left behind" : "");
  }

  function runCard(run) {
    var rows = run.results.map(function (r) {
      return el("li", { class: r.status === "fail" ? "danger" : "", text:
        LABEL[r.status] + "  " + r.id + "  " + r.seconds.toFixed(1) + "s" + (r.reason ? "  " + r.reason : "") });
    });
    var box = el("article", { class: "card" },
      el("h3", { text: run.run_id + " (" + run.mode + (run.kept ? ", items kept" : "") + ")" }),
      el("p", { text: run.stopped ? run.stopped : scoreLine(run) }));
    if (rows.length) {
      var details = el("details", {}, el("summary", { text: "Scenarios" }));
      var list = el("ul", {});
      rows.forEach(function (row) { list.appendChild(row); });
      details.appendChild(list);
      box.appendChild(details);
    }
    return box;
  }

  function build() {
    var page = el("div", {}, el("h2", { text: "Live test" }),
      el("p", { class: "muted", text: "Recent runs of `chamberlain live-test`. Run it from a terminal; see docs/live-test.md." }));
    var box = el("div", {}, el("p", { text: "Loading..." }));
    page.appendChild(box);
    V.api("GET", "/api/live-test").then(function (data) {
      box.replaceChildren();
      if (!data.runs.length) { box.appendChild(V.empty("No live-test runs yet.")); return; }
      data.runs.forEach(function (run) { box.appendChild(runCard(run)); });
    }, function (error) { box.replaceChildren(V.problem(error)); });
    return page;
  }

  V.register("livetest", "Live test", build);
})();
