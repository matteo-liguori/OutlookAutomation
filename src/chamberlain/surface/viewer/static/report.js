"use strict";
// The front page: the newest pass report, drawn from the blocks the server sends. Text is only ever
// set with textContent (the markdown renderer in calendar.js builds nodes, never markup). The red
// and orange callouts come from each block's `style`, which the server takes from the report
// layout, so nothing in the assistant's text or in an email can pose as one.
(function () {
  var V = window.ChamberlainViewer;
  if (!V) return;
  var el = V.el;

  // A stored UTC time shown in the owner's own zone (set from each API reply).
  V.local = function (stamp) {
    if (!stamp) return "never";
    try {
      return new Intl.DateTimeFormat("en-GB", { timeZone: V.zone || "UTC", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(stamp));
    } catch (_) {
      return V.when(stamp);
    }
  };

  var STYLE_WORDS = { red: "Needs your check", orange: "Needs your acknowledgement", plain: "" };

  function drawBlock(block) {
    var styled = block.style === "red" || block.style === "orange";
    var box = el("section", { class: "report-block" + (styled ? " callout callout-" + block.style : "") });
    if (styled) box.setAttribute("aria-label", block.heading);
    if (block.heading) {
      var label = (styled && block.mark ? block.mark + " " : "") + block.heading;
      box.appendChild(el("h2", { class: styled ? "callout-label" : "", text: label }));
    }
    if (styled && STYLE_WORDS[block.style]) {
      box.appendChild(el("p", { class: "callout-kind", text: STYLE_WORDS[block.style] }));
    }
    if (block.markdown && V.markdown) box.appendChild(V.markdown(block.markdown));
    return box;
  }

  // One report as an article. Used by the front page and by the calendar's report panel.
  V.drawReport = function (report) {
    var article = el("article", { class: "report-content" });
    if (!report) {
      article.appendChild(el("p", { class: "muted", text: "This report is no longer stored." }));
      return article;
    }
    article.appendChild(el("h1", { class: "report-title", text: report.title }));
    var meta = [report.schedule, report.started_at ? V.local(report.started_at) : report.state];
    var line = el("p", { class: "report-time", text: meta.join(" · ") });
    if (report.partial) line.appendChild(el("span", { class: "badge attention", text: "Partial: the pass ended early" }));
    article.appendChild(line);
    report.blocks.forEach(function (block) {
      if (block.by === "agent" && !block.markdown) return;
      article.appendChild(drawBlock(block));
    });
    if (report.note) article.appendChild(el("p", { class: "warn", text: report.note }));
    return article;
  };

  function build() {
    var page = el("div", { class: "front-page" });
    var body = el("div", { "aria-live": "polite" });
    var state = { ticket: 0 };

    function load(run) {
      var ticket = ++state.ticket;
      var path = "/api/report" + (run ? "?run=" + encodeURIComponent(run) : "");
      V.api("GET", path).then(function (data) {
        if (ticket !== state.ticket) return;
        V.zone = data.timezone;
        var nodes = [];
        data.waiting.forEach(function (w) {
          nodes.push(el("p", { class: "muted waiting", text: "The " + w.schedule + " pass for " + w.day + " has no report yet (" + w.state + "). It appears here when the assistant finishes." }));
        });
        if (data.report) {
          nodes.push(V.drawReport(data.report));
        } else {
          nodes.push(V.empty("No report has been written yet. The first appears here after a scheduled pass."));
        }
        if (data.earlier.length) {
          var list = el("ul", { class: "earlier" });
          data.earlier.forEach(function (entry) {
            list.appendChild(el("li", {}, el("button", {
              class: "act",
              text: entry.schedule + " · " + entry.day + (entry.state === "incomplete" ? " · partial" : ""),
              onclick: function () { load(entry.run); window.scrollTo(0, 0); },
            })));
          });
          nodes.push(el("section", { class: "earlier-reports" }, el("h2", { text: "Earlier reports" }), list));
        }
        body.replaceChildren.apply(body, nodes);
        if (V.appointments) V.appointments.check(data.appointments_due);
      }, function (error) { if (ticket === state.ticket) body.replaceChildren(V.problem(error)); });
    }

    page.appendChild(el("div", { class: "calendar-heading" }, el("h2", { text: "Report" }),
      el("button", { class: "act", text: "Refresh", onclick: function () { load(null); } })));
    page.appendChild(body);
    load(null);
    return page;
  }

  V.register("report", "Report", build, "first");
})();
