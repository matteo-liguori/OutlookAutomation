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
})();
