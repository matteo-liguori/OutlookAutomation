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

  V.markdown = reportContents;

  function normal(text) { return text.toLowerCase().replace(/\s+/g, " ").trim(); }

  // A real calendar event is the same appointment when it starts together with an important
  // reminder and either title holds the other.
  function matches(mark, event) {
    if (event.start !== mark.start) return false;
    var a = normal(event.display_title || event.title);
    var b = normal(mark.title);
    return !!a && !!b && (a.indexOf(b) >= 0 || b.indexOf(a) >= 0 || normal(event.title).indexOf(b) >= 0);
  }

  function importanceClasses(mark, today) {
    var classes = " important-event";
    var away = (civil(mark.day) - civil(today)) / 86400000;
    if (away >= 0 && away <= 2) classes += " important-near";
    if (mark.due || away === 0) classes += " important-now";
    return classes;
  }

  // The dialog that adds an event only this viewer keeps. A repeat of the same save (a double
  // click) carries the same key, so it saves once.
  function localEventDialog(saved) {
    var dialog = el("dialog", { "aria-labelledby": "local-title" });
    var title = el("input", { type: "text", maxlength: "180", required: "required", "aria-label": "Title" });
    var allDay = el("input", { type: "checkbox", id: "local-all-day" });
    var starts = el("input", { type: "datetime-local", required: "required", "aria-label": "Starts" });
    var ends = el("input", { type: "datetime-local", required: "required", "aria-label": "Ends" });
    var endLabel = el("span", { text: "Ends" });
    var place = el("input", { type: "text", maxlength: "600", "aria-label": "Link or location" });
    var status = el("p", { class: "muted", role: "status" });
    var save = el("button", { type: "submit", class: "act primary", text: "Save event" });
    var cancel = el("button", { type: "button", class: "act", text: "Cancel", onclick: function () { dialog.close(); } });
    var attempt = null;
    var form = el("form", { class: "local-form" },
      el("h2", { id: "local-title", text: "Add local event" }),
      el("p", { class: "muted", text: "Kept only in this viewer. No calendar is changed." }),
      el("label", {}, "Title ", title),
      el("label", {}, allDay, " All day"),
      el("label", {}, "Starts ", starts),
      el("label", {}, endLabel, " ", ends),
      el("label", {}, "Link or location ", place),
      el("div", {}, save, cancel),
      status);
    dialog.appendChild(form);

    function setKinds(day) {
      var whole = allDay.checked;
      [[starts, "09:00"], [ends, "10:00"]].forEach(function (pair) {
        var date = pair[0].value.slice(0, 10) || day;
        pair[0].type = whole ? "date" : "datetime-local";
        pair[0].value = whole ? date : date + "T" + pair[1];
      });
      endLabel.textContent = whole ? "Last day" : "Ends";
    }
    allDay.addEventListener("change", function () { setKinds(iso(new Date())); });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var whole = allDay.checked;
      var end = whole ? iso(plus(civil(ends.value), 1)) : ends.value;
      var body = { title: title.value, location: place.value, start: starts.value, end: end, all_day: whole };
      var fingerprint = JSON.stringify(body);
      if (!attempt || attempt.fingerprint !== fingerprint) {
        var key = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
        attempt = { fingerprint: fingerprint, key: key };
      }
      body.key = attempt.key;
      save.disabled = cancel.disabled = true;
      status.textContent = "Saving\u2026";
      V.api("POST", "/api/local-events", body).then(function (data) {
        attempt = null;
        form.reset();
        dialog.close();
        status.textContent = "";
        saved(data.event);
      }, function (error) {
        status.textContent = error.message;
      }).then(function () { save.disabled = cancel.disabled = false; });
    });
    return {
      node: dialog,
      open: function (day) {
        allDay.checked = false;
        starts.value = ends.value = "";
        starts.type = ends.type = "datetime-local";
        setKinds(iso(day));
        status.textContent = "";
        dialog.showModal();
      },
    };
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
    var waitingBox = el("div", {});
    var addLocal = el("button", { text: "Add local event" });
    var localDialog = localEventDialog(function (event) { go(civil(event.start.slice(0, 10))); });
    addLocal.addEventListener("click", function () { localDialog.open(state.selected); });

    page.appendChild(el("div", { class: "calendar-heading" }, el("h2", { text: "Calendar" }),
      el("div", { class: "controls" }, addLocal, todayButton, prev, next, date, mode)));
    page.appendChild(range);
    page.appendChild(legend);
    page.appendChild(failure);
    page.appendChild(grid);
    page.appendChild(coverage);
    page.appendChild(el("section", { class: "report-panel", "aria-labelledby": "reports-title" },
      el("div", { class: "calendar-heading" }, el("h2", { id: "reports-title", text: "Reports" }), reports), waitingBox, reportBox));
    page.appendChild(localDialog.node);

    function window_() {
      var m = mode.value;
      if (m === "month") return { start: monday(monthStart(state.selected)), end: monday(plus(monthStart(state.selected, 1), 6)) };
      if (m === "rolling") return { start: plus(state.selected, -2), end: plus(state.selected, 5) };
      var start = m === "7" ? monday(state.selected) : state.selected;
      return { start: start, end: plus(start, Number(m)) };
    }

    function label(event) { return event.display_title || event.title; }
    function catClass(event) { return "cat-" + (event.category || "none") + (event.local ? " local-event" : ""); }
    function tip(event) {
      var parts = event.calendars.slice();
      if (event.display_title) parts.push(event.title);
      return parts.join(" \u00b7 ");
    }

    // The event's title: a link when its location is a web link, a button when it is an important
    // appointment (which opens the reminder), else plain text.
    function titleNode(event, mark) {
      if (mark) {
        var b = el("button", { class: "appointment-event-title", text: label(event) });
        b.addEventListener("click", function () { V.appointments.show(mark); });
        return b;
      }
      if (event.url) return safeLink(label(event), event.url);
      return el("div", { text: label(event) });
    }

    function removeButton(event) {
      var b = el("button", { class: "local-remove", text: "Remove" });
      b.addEventListener("click", function () {
        if (b.dataset.sure !== "yes") { b.dataset.sure = "yes"; b.textContent = "Click again to remove"; return; }
        V.post("/api/local-events/" + encodeURIComponent(event.id) + "/remove", {}, function () { load(); });
      });
      return b;
    }

    function eventCard(event, mark) {
      var item = el("div", { class: "calendar-event " + catClass(event) + (mark ? importanceClasses(mark, state.today) : "") });
      item.appendChild(el("div", { class: "event-time", text: event.all_day ? "All day" : event.start_time + "\u2013" + event.end_time }));
      item.appendChild(titleNode(event, mark));
      if (event.repeats) item.appendChild(el("div", { class: "event-time", text: "Repeats" }));
      if (event.location && !event.url) item.appendChild(el("div", { class: "event-time", text: event.location }));
      if (event.local) {
        item.appendChild(el("div", { class: "event-time", text: "Local event, only in this viewer" }));
        item.appendChild(removeButton(event));
      }
      item.title = tip(event);
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
        // Only real calendar events are drawn. An important date's reminders are not entries of
        // their own: they only gild the matching real event and open the pop-up on their days.
        d.timed.forEach(function (event) {
          var mark = d.marks.find(function (m) { return matches(m, event); });
          cell.appendChild(eventCard(event, mark));
        });
        var note = status || (!d.timed.length && !d.bands.length ? "No events" : "");
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
        var mark = (days[seg.start] ? days[seg.start].marks : []).find(function (m) { return matches(m, seg.event); });
        var bar = el("div", { class: "event-band " + catClass(seg.event) + (seg.event.all_day ? "" : " timed-band") + (mark ? importanceClasses(mark, state.today) : ""), title: tip(seg.event) });
        bar.style.gridColumn = (seg.start + 1) + " / " + (seg.end + 1);
        bar.style.gridRow = String(lane + 1);
        bar.setAttribute("aria-label", label(seg.event) + (seg.event.all_day ? ", all day" : ", spans multiple days"));
        if (!labelled[seg.key]) {
          var inner = mark ? titleNode(seg.event, mark) : seg.event.url ? safeLink(label(seg.event), seg.event.url) : el("span", { text: label(seg.event) });
          bar.appendChild(inner);
          labelled[seg.key] = true;
        }
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
          dated: data.dated.filter(function (m) { return m.day === id; }),
        });
      }
      var width = mode.value === "4" ? 4 : 7;
      var weeks = [];
      var shown = {};
      for (var i = 0; i < dayList.length; i += width) weeks.push(week(dayList.slice(i, i + width), data, shown));
      grid.replaceChildren.apply(grid, weeks);
      range.textContent = mode.value === "month" ? monthFormat.format(state.selected) : dateFormat.format(start) + " – " + dateFormat.format(plus(end, -1));
      legend.replaceChildren();
      data.categories.forEach(function (c) { legend.appendChild(el("li", { class: "cat-" + c.id, text: c.label })); });
      legend.appendChild(el("li", { class: "cat-none", text: "Not yet categorised" }));
      legend.appendChild(el("li", { class: "importance-key", text: "Important appointment" }));
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
        V.zone = data.timezone;
        state.reportDays = data.report_days;
        draw(data, w.start, w.end);
        var current = reports.value;
        reports.replaceChildren.apply(reports, data.report_days.map(function (d) {
          return el("option", { value: d, text: dateFormat.format(civil(d)) + (d === data.today ? " · Today" : "") });
        }));
        if (data.report_days.indexOf(current) >= 0) reports.value = current;
        else if (data.report_days.indexOf(data.today) >= 0) reports.value = data.today;
        reports.disabled = !data.report_days.length;
        if (V.pendingDay && data.report_days.indexOf(V.pendingDay) >= 0) reports.value = V.pendingDay;
        V.pendingDay = null;
        loadReports();
        loadWaiting();
        if (V.appointments) V.appointments.check(data.appointments_due);
      }, function (error) { if (ticket === state.ticket) showError(error); });
    }

    // A pass that has started but not written its report yet is named above the reports.
    function loadWaiting() {
      V.api("GET", "/api/report").then(function (data) {
        waitingBox.replaceChildren.apply(waitingBox, data.waiting.map(function (w) {
          return el("p", { class: "muted waiting", text: "The " + w.schedule + " pass for " + w.day + " has no report yet (" + w.state + "). It appears here when the assistant finishes." });
        }));
      }, function () { waitingBox.replaceChildren(); });
    }

    function loadReports() {
      var ticket = ++state.reportTicket;
      var chosen = reports.value;
      if (!chosen) { reportBox.replaceChildren(el("p", { class: "muted", text: "No saved reports in the last two months." })); return; }
      V.api("GET", "/api/reports?day=" + encodeURIComponent(chosen)).then(function (data) {
        if (ticket !== state.reportTicket) return;
        var articles = [];
        data.runs.forEach(function (run) {
          if (run.report) articles.push(V.drawReport(run.report));
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

    go(civil(V.pendingDay || iso(new Date())));
    return page;
  }

  V.register("calendar", "Calendar", build, "first");
})();
