"use strict";
// The calendar page: colour ribbons per calendar, bands for multi-day events, the gold border of
// an important date, and the day's reports below. Events and reports are drawn with textContent
// only; a link is made only from https, http or mailto text in a report.
(function () {
  var V = window.ChamberlainViewer;
  if (!V) return;
  var el = V.el;

  var dateFormat = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
  var monthFormat = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
  var civil = function (value) { return new Date(value + "T00:00:00Z"); };
  var iso = function (value) { return value.toISOString().slice(0, 10); };
  function plus(date, days) {
    var next = new Date(date);
    next.setUTCDate(next.getUTCDate() + days);
    return next;
  }
  function monday(date) { return plus(date, -((date.getUTCDay() + 6) % 7)); }
  function monthStart(date, offset) {
    return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + (offset || 0), 1));
  }

  function safeLink(title, raw) {
    try {
      var url = new URL(raw);
      if (["https:", "http:", "mailto:"].indexOf(url.protocol) < 0) return document.createTextNode(title);
      var a = el("a", { text: title, target: "_blank", rel: "noopener noreferrer" });
      a.href = url.href;
      return a;
    } catch (_) {
      return document.createTextNode(title);
    }
  }

  function inline(node, line) {
    var pattern = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*/g;
    var start = 0;
    var part;
    while ((part = pattern.exec(line)) !== null) {
      node.appendChild(document.createTextNode(line.slice(start, part.index)));
      node.appendChild(part[1] ? safeLink(part[1], part[2]) : el("strong", { text: part[3] }));
      start = part.index + part[0].length;
    }
    node.appendChild(document.createTextNode(line.slice(start)));
    return node;
  }

  // A small markdown subset: headings, bullets, numbered lists, tables, bold and links.
  function reportContents(body) {
    var out = document.createDocumentFragment();
    var list = null;
    var table = null;
    body.split(/\r?\n/).forEach(function (original) {
      var line = original.trim();
      if (!line) { list = table = null; return; }
      if (line.charAt(0) === "|" && line.charAt(line.length - 1) === "|") {
        if (/^\|[\s:|-]+\|$/.test(line)) return;
        var first = !table;
        if (!table) { table = el("table", {}); out.appendChild(table); }
        var row = table.insertRow();
        line.slice(1, -1).split("|").forEach(function (value) {
          row.appendChild(inline(el(first ? "th" : "td", {}), value.trim()));
        });
        return;
      }
      table = null;
      var bullet = line.match(/^([-*]|\d+\.)\s+(.*)$/);
      if (bullet) {
        var tag = /\d/.test(bullet[1]) ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag, {}); out.appendChild(list); }
        list.appendChild(inline(el("li", {}), bullet[2]));
        return;
      }
      list = null;
      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      out.appendChild(inline(el(heading ? "h" + heading[1].length : "p", {}), heading ? heading[2] : line));
    });
    return out;
  }

  function importanceClasses(mark, today) {
    var classes = " important-event";
    var away = (civil(mark.day) - civil(today)) / 86400000;
    if (away >= 0 && away <= 2) classes += " important-near";
    if (mark.due || away === 0) classes += " important-now";
    return classes;
  }

  function build() {
    var page = el("div", { class: "calendar-page" });
    var state = { selected: null, today: null, mode: "rolling", ticket: 0, reportTicket: 0, reportDays: [] };

    var mode = el("select", { "aria-label": "Calendar view" },
      el("option", { value: "rolling", text: "7 days" }), el("option", { value: "4", text: "4 days" }),
      el("option", { value: "7", text: "Week" }), el("option", { value: "month", text: "Month" }));
    var date = el("input", { type: "date", "aria-label": "Jump to date" });
    var todayButton = el("button", { text: "Today" });
    var prev = el("button", { "aria-label": "Previous calendar window", text: "‹" });
    var next = el("button", { "aria-label": "Next calendar window", text: "›" });
    var range = el("p", { id: "range", "aria-live": "polite" });
    var legend = el("ul", { class: "colour-key", "aria-label": "Calendar colour legend" });
    var grid = el("div", { id: "calendar" });
    var coverage = el("p", { class: "coverage", role: "status" });
    var failure = el("p", { class: "error", role: "alert" });
    failure.hidden = true;
    var reports = el("select", { id: "reports", "aria-label": "Select report date" });
    var reportBox = el("div", { "aria-live": "polite" });
    var dialog = el("dialog", { "aria-labelledby": "mark-title" });
    var dialogTitle = el("h2", { id: "mark-title" });
    var dialogBody = el("div", {});
    dialog.appendChild(el("p", { class: "appointment-kicker", text: "Important date" }));
    dialog.appendChild(dialogTitle);
    dialog.appendChild(dialogBody);
    dialog.appendChild(el("button", { class: "act", text: "Close", onclick: function () { dialog.close(); } }));

    page.appendChild(el("div", { class: "calendar-heading" }, el("h2", { text: "Calendar" }),
      el("div", { class: "controls" }, todayButton, prev, next, date, mode)));
    page.appendChild(range);
    page.appendChild(legend);
    page.appendChild(failure);
    page.appendChild(grid);
    page.appendChild(coverage);
    page.appendChild(el("section", { class: "report-panel", "aria-labelledby": "reports-title" },
      el("div", { class: "calendar-heading" }, el("h2", { id: "reports-title", text: "Reports" }), reports), reportBox));
    page.appendChild(dialog);

    function window_() {
      var m = mode.value;
      if (m === "month") return { start: monday(monthStart(state.selected)), end: monday(plus(monthStart(state.selected, 1), 6)) };
      if (m === "rolling") return { start: plus(state.selected, -2), end: plus(state.selected, 5) };
      var start = m === "7" ? monday(state.selected) : state.selected;
      return { start: start, end: plus(start, Number(m)) };
    }

    function showMark(mark) {
      dialogTitle.textContent = mark.title;
      while (dialogBody.firstChild) dialogBody.removeChild(dialogBody.firstChild);
      dialogBody.appendChild(el("p", { text: "Card: " + mark.card_title }));
      dialogBody.appendChild(el("p", { class: "muted", text: "Due " + mark.day + (mark.due ? " (now due)" : "") }));
      dialog.showModal();
    }

    function markButton(mark) {
      var b = el("button", { class: "milestone" + importanceClasses(mark, state.today), text: mark.title });
      b.addEventListener("click", function () { showMark(mark); });
      return b;
    }

    function eventCard(event) {
      var item = el("div", { class: "calendar-event tone-" + event.tone });
      item.appendChild(el("div", { class: "event-time", text: event.start_time + "–" + event.end_time }));
      item.appendChild(el("div", { text: event.title }));
      if (event.location) item.appendChild(el("div", { class: "event-time", text: event.location }));
      item.title = event.calendars.join(" · ");
      return item;
    }

    function week(days, data, shown) {
      var wrap = el("div", { class: "calendar-week" });
      var headers = el("div", { class: "week-grid" });
      var bands = el("div", { class: "week-grid calendar-bands" });
      var cells = el("div", { class: "week-grid" });
      var segments = [];
      var seen = {};
      var read = data.sources.filter(function (s) { return s.read; }).length;
      var status = !read ? "Not collected" : read < data.sources.length ? "Partial coverage" : "";
      days.forEach(function (d, column) {
        var head = el("div", { class: "calendar-date" + (read ? "" : " unchecked") });
        var label = dateFormat.format(d.date);
        if (state.reportDays.indexOf(d.id) >= 0) {
          var pick = el("button", { class: "has-report", text: label, title: "Show this day's reports" });
          pick.addEventListener("click", function () { reports.value = d.id; loadReports(); reports.scrollIntoView(); });
          head.appendChild(el("h3", {}, pick));
        } else {
          head.appendChild(el("h3", { text: label }));
        }
        if (d.id === state.today) head.setAttribute("aria-current", "date");
        headers.appendChild(head);
        var cell = el("div", { class: "calendar-day" + (read ? "" : " unchecked") });
        d.timed.forEach(function (event) { cell.appendChild(eventCard(event)); });
        d.marks.forEach(function (mark) { cell.appendChild(markButton(mark)); });
        var note = status || (!d.timed.length && !d.bands.length && !d.marks.length ? "No events" : "");
        if (note) cell.appendChild(el("p", { class: "day-note", text: note }));
        cells.appendChild(cell);
        d.bands.forEach(function (event) {
          var key = JSON.stringify([event.source, event.title, event.first_day, event.last_day]);
          var previous = seen[key];
          if (previous && previous.end === column) { previous.end++; return; }
          var segment = { key: key, event: event, start: column, end: column + 1 };
          seen[key] = segment;
          segments.push(segment);
        });
      });
      var lanes = [];
      var labelled = {};
      segments.forEach(function (seg) {
        var lane = lanes.findIndex(function (lastEnd) { return lastEnd <= seg.start; });
        if (lane === -1) lane = lanes.length;
        lanes[lane] = seg.end;
        var bar = el("div", { class: "event-band tone-" + seg.event.tone + (seg.event.all_day ? "" : " timed-band"), title: seg.event.calendars.join(" · ") });
        bar.style.gridColumn = (seg.start + 1) + " / " + (seg.end + 1);
        bar.style.gridRow = String(lane + 1);
        bar.setAttribute("aria-label", seg.event.title + (seg.event.all_day ? ", all day" : ", spans multiple days"));
        if (!labelled[seg.key]) { bar.appendChild(el("span", { text: seg.event.title })); labelled[seg.key] = true; }
        bands.appendChild(bar);
      });
      wrap.appendChild(headers);
      wrap.appendChild(bands);
      wrap.appendChild(cells);
      return wrap;
    }

    function draw(data, start, end) {
      grid.className = mode.value === "4" ? "four" : "week";
      var dayList = [];
      for (var d = start; d < end; d = plus(d, 1)) {
        var id = iso(d);
        var inDay = data.events.filter(function (e) { return e.first_day <= id && id <= e.last_day; });
        var isBand = function (e) { return e.all_day || e.first_day !== e.last_day; };
        dayList.push({
          id: id,
          date: d,
          bands: inDay.filter(isBand),
          timed: inDay.filter(function (e) { return !isBand(e); }),
          marks: data.important.filter(function (m) { return m.day === id; }),
        });
      }
      var width = mode.value === "4" ? 4 : 7;
      var weeks = [];
      var shown = {};
      for (var i = 0; i < dayList.length; i += width) weeks.push(week(dayList.slice(i, i + width), data, shown));
      grid.replaceChildren.apply(grid, weeks);
      range.textContent = mode.value === "month" ? monthFormat.format(state.selected) : dateFormat.format(start) + " – " + dateFormat.format(plus(end, -1));
      legend.replaceChildren();
      data.sources.forEach(function (s) { legend.appendChild(el("li", { class: "tone-" + s.tone, text: s.label })); });
      legend.appendChild(el("li", { class: "importance-key", text: "Important date" }));
      var problems = data.sources.filter(function (s) { return s.note; }).map(function (s) { return s.label + ": " + s.note; });
      coverage.textContent = "Times in " + data.timezone + "." + (data.sources.length ? "" : " No calendar is connected.") + (problems.length ? " " + problems.join(" ") : "");
    }

    function showError(error) { failure.textContent = error.message; failure.hidden = false; }

    function load() {
      var ticket = ++state.ticket;
      var w = window_();
      failure.hidden = true;
      V.api("GET", "/api/calendar?start=" + iso(w.start) + "&until=" + iso(w.end)).then(function (data) {
        if (ticket !== state.ticket) return;
        state.today = data.today;
        state.reportDays = data.report_days;
        draw(data, w.start, w.end);
        var current = reports.value;
        reports.replaceChildren.apply(reports, data.report_days.map(function (d) {
          return el("option", { value: d, text: dateFormat.format(civil(d)) + (d === data.today ? " · Today" : "") });
        }));
        if (data.report_days.indexOf(current) >= 0) reports.value = current;
        else if (data.report_days.indexOf(data.today) >= 0) reports.value = data.today;
        reports.disabled = !data.report_days.length;
        loadReports();
      }, function (error) { if (ticket === state.ticket) showError(error); });
    }

    function loadReports() {
      var ticket = ++state.reportTicket;
      var chosen = reports.value;
      if (!chosen) { reportBox.replaceChildren(el("p", { class: "muted", text: "No saved reports in the last two months." })); return; }
      V.api("GET", "/api/reports?day=" + encodeURIComponent(chosen)).then(function (data) {
        if (ticket !== state.reportTicket) return;
        var articles = [];
        data.runs.forEach(function (run) {
          run.reports.forEach(function (text, index) {
            var article = el("article", { class: "report-content" });
            article.appendChild(el("h3", { class: "report-time", text: run.schedule + " · " + (run.started_at ? V.when(run.started_at) : run.state) + (run.state === "incomplete" ? " · Partial" : "") + (index ? " · part " + (index + 1) : "") }));
            article.appendChild(reportContents(text));
            articles.push(article);
          });
        });
        reportBox.replaceChildren.apply(reportBox, articles.length ? articles : [el("p", { class: "muted", text: "The reports of this day have expired." })]);
      }, function (error) { if (ticket === state.reportTicket) reportBox.replaceChildren(V.problem(error)); });
    }

    function go(day) { state.selected = day; date.value = iso(day); load(); }
    todayButton.addEventListener("click", function () { go(civil(state.today || iso(new Date()))); });
    prev.addEventListener("click", function () { step(-1); });
    next.addEventListener("click", function () { step(1); });
    function step(direction) {
      var m = mode.value;
      var span = m === "4" ? 4 : 7;
      go(m === "month" ? monthStart(state.selected, direction) : plus(state.selected, direction * span));
    }
    mode.addEventListener("change", load);
    date.addEventListener("change", function () { if (date.value) go(civil(date.value)); });
    reports.addEventListener("change", loadReports);

    go(civil(iso(new Date())));
    return page;
  }

  V.register("calendar", "Calendar", build, "needs");
})();
