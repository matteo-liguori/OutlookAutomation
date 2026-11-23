'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  const key = 'outlook-viewer-token';
  if (fragment.has('token')) {
    sessionStorage.setItem(key, fragment.get('token'));
    history.replaceState(null, '', location.pathname);
  }
  const token = sessionStorage.getItem(key) || '';
  const calendarDate = new Intl.DateTimeFormat('en-GB', {weekday:'short', day:'numeric', month:'short', timeZone:'UTC'});
  const calendarMonth = new Intl.DateTimeFormat('en-GB', {month:'long', year:'numeric', timeZone:'UTC'});
  const civil = value => new Date(value + 'T00:00:00Z');
  const iso = value => value.toISOString().slice(0, 10);
  function plus(date, days) {
    const next = new Date(date);
    next.setUTCDate(next.getUTCDate() + days);
    return next;
  }
  function monday(date) { return plus(date, -((date.getUTCDay() + 6) % 7)); }
  function monthStart(date, offset = 0) {
    return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + offset, 1));
  }
  function node(tag, content, className) {
    const el = document.createElement(tag);
    if (content !== undefined) el.textContent = content;
    if (className) el.className = className;
    return el;
  }
  function link(title, raw) {
    try {
      const url = new URL(raw);
      if (!['https:', 'http:', 'mailto:'].includes(url.protocol)) return document.createTextNode(title);
      const el = node('a', title);
      el.href = url.href;
      el.target = '_blank';
      el.rel = 'noopener noreferrer';
      return el;
    } catch (_) {
      return document.createTextNode(title);
    }
  }
  async function get(path, payload) {
    const options = {headers: {'X-Viewer-Token': token}, signal: AbortSignal.timeout(10000)};
    if (payload) {
      options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(payload);
    }
    const res = await fetch(path, options);
    if (res.status === 401) throw new Error('Open the private dashboard link printed by the harness.');
    if (!res.ok) {
      const result = await res.json().catch(() => ({}));
      throw new Error(result.error || 'The local dashboard could not complete this request.');
    }
    return res.json();
  }
  function inline(el, line) {
    const pattern = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*/g;
    let start = 0;
    for (const part of line.matchAll(pattern)) {
      el.append(document.createTextNode(line.slice(start, part.index)));
      el.append(part[1] ? link(part[1], part[2]) : node('strong', part[3]));
      start = part.index + part[0].length;
    }
    el.append(document.createTextNode(line.slice(start)));
    return el;
  }
  function reportContents(body) {
    const contents = document.createDocumentFragment();
    let list, table, calendarSection, calendarTable;
    for (const original of body.split(/\r?\n/)) {
      const line = original.trim();
      if (/^##\s+Calendar\b/i.test(line)) { calendarSection = true; calendarTable = false; list = table = null; continue; }
      if (calendarSection) {
        if (line.startsWith('|')) calendarTable = true;
        if (!line || line.startsWith('|') || (!calendarTable && !line.startsWith('## '))) continue;
        calendarSection = false;
      }
      if (!line || line.startsWith('::')) { list = table = null; continue; }
      if (line.startsWith('|') && line.endsWith('|')) {
        if (/^\|[\s:|-]+\|$/.test(line)) continue;
        const first = !table;
        if (!table) { table = node('table'); contents.append(table); }
        const row = table.insertRow();
        for (const value of line.slice(1, -1).split('|')) row.append(inline(node(first ? 'th' : 'td'), value.trim()));
        continue;
      }
      table = null;
      const bullet = line.match(/^([-*]|\d+\.)\s+(.*)$/);
      if (bullet) {
        const tag = /\d/.test(bullet[1]) ? 'OL' : 'UL';
        if (list?.tagName !== tag) { list = node(tag.toLowerCase()); contents.append(list); }
        list.append(inline(node('li'), bullet[2]));
        continue;
      }
      list = null;
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      contents.append(inline(node(heading ? 'h' + heading[1].length : 'p'), heading ? heading[2] : line));
    }
    return contents;
  }

  let today, timezone, selected, request = 0, reportRequest = 0;
  function windowDates() {
    const mode = $('mode').value;
    if (mode === 'month') {
      const start = monday(monthStart(selected));
      return {start, end: monday(plus(monthStart(selected, 1), 6))};
    }
    if (mode === 'rolling') return {start: plus(selected, -2), end: plus(selected, 5)};
    const start = mode === '7' ? monday(selected) : selected;
    return {start, end: plus(start, Number(mode))};
  }
  function eventCard(event, formatTime, appointment) {
    const item = node('div', undefined, 'calendar-event ' + eventStyle(event));
    const label = eventLabel(event);
    const text = event.all_day ? 'All day' : formatTime.format(new Date(event.start)) + '–' + formatTime.format(new Date(event.end));
    item.append(node('div', text, 'event-time'));
    const url = event.url || (/^https?:\/\//i.test(event.location || '') ? event.location : '');
    if (appointment) {
      const title = node('button', label, 'appointment-event-title');
      title.addEventListener('click', () => displayAppointment(appointment));
      item.append(title);
      styleImportance(item, appointment);
    } else {
      item.append(url ? link(label, url) : node('div', label));
    }
    if (event.location && event.location !== url) item.append(node('div', event.location, 'event-time'));
    item.title = [...event.calendars, ...(event.display_title ? [event.title] : [])].join(' · ');
    return item;
  }
  function eventLabel(event) { return event.display_title || event.title; }
  function eventStyle(event) {
    const types = ['work', 'personal', 'social', 'travel', 'health', 'occasion', 'home'];
    return 'type-' + (types.includes(event.category) ? event.category : 'neutral');
  }
  function eventKey(event) {
    return event.occurrence_key || JSON.stringify([event.source, event.id, event.start, event.end]);
  }
  function appointmentButton(appointment) {
    const marker = node('button', appointment.title, 'milestone');
    marker.addEventListener('click', () => displayAppointment(appointment));
    styleImportance(marker, appointment);
    return marker;
  }
  function styleImportance(element, appointment) {
    element.classList.add('important-event');
    const daysAway = (civil(appointment.day) - civil(today)) / 86400000;
    if (daysAway >= 0 && daysAway <= 2) element.classList.add('important-near');
    if (appointment.due || daysAway === 0) element.classList.add('important-now');
  }
  function matchingAppointment(event, day) {
    return day.appointments.find(value => value.match_title &&
      Date.parse(value.start) === Date.parse(event.start) &&
      event.title.toLocaleLowerCase().includes(value.match_title.toLocaleLowerCase()));
  }
  function multiDay(event, formatDay) {
    return formatDay.format(new Date(event.start)) !== formatDay.format(new Date(Date.parse(event.end) - 1));
  }
  function calendarWeek(days, shown) {
    const week = node('div', undefined, 'calendar-week');
    const headers = node('div', undefined, 'week-grid');
    const bands = node('div', undefined, 'week-grid calendar-bands');
    const appointments = node('div', undefined, 'week-grid');
    const tracks = [];
    const eventsSeen = new Map();
    for (const [column, day] of days.entries()) {
      const head = node('div', undefined, 'calendar-date' + (day.available ? '' : ' unchecked'));
      head.append(node('h3', calendarDate.format(day.date)));
      if (day.id === today) head.setAttribute('aria-current', 'date');
      headers.append(head);
      const cell = node('div', undefined, 'calendar-day' + (day.available ? '' : ' unchecked'));
      const matched = new Set();
      for (const event of day.timed) {
        const appointment = matchingAppointment(event, day);
        if (appointment) matched.add(appointment.id);
        cell.append(eventCard(event, day.formatTime, appointment));
      }
      for (const event of day.bands) {
        const appointment = matchingAppointment(event, day);
        if (appointment) matched.add(appointment.id);
      }
      for (const appointment of day.appointments) {
        if (!matched.has(appointment.id)) cell.append(appointmentButton(appointment));
      }
      for (const reminder of day.localReminders) {
        const item = node('a', reminder.category + ' · ' + reminder.title, 'local-reminder');
        item.href = '/#' + new URLSearchParams({card:reminder.case_id});
        item.title = reminder.text;
        cell.append(item);
      }
      if (day.status) cell.append(node('p', day.status, 'day-note'));
      appointments.append(cell);
      for (const event of day.bands) {
        const key = eventKey(event);
        const previous = eventsSeen.get(key);
        if (previous && previous.end === column) { previous.end++; continue; }
        const segment = {key, event, start:column, end:column + 1, appointment:matchingAppointment(event, day)};
        eventsSeen.set(key, segment);
        tracks.push(segment);
      }
    }
    const lanes = [];
    for (const {key, event, start, end, appointment} of tracks) {
      let lane = lanes.findIndex(lastEnd => lastEnd <= start);
      if (lane === -1) lane = lanes.length;
      lanes[lane] = end;
      const bar = node('div', undefined, 'event-band ' + eventStyle(event) + (event.all_day ? ' all-day' : ' timed-band'));
      bar.style.gridColumn = (start + 1) + ' / ' + (end + 1);
      bar.style.gridRow = lane + 1;
      bar.title = event.title;
      const label = eventLabel(event);
      bar.setAttribute('aria-label', label + (event.all_day ? ', all day' : ', spans multiple days'));
      if (appointment) styleImportance(bar, appointment);
      if (!shown.has(key)) {
        const url = event.url || (/^https?:\/\//i.test(event.location || '') ? event.location : '');
        if (appointment) {
          const button = node('button', label, 'appointment-event-title');
          button.addEventListener('click', () => displayAppointment(appointment));
          bar.append(button);
        } else {
          bar.append(url ? link(label, url) : node('span', label));
        }
        shown.add(key);
      }
      bands.append(bar);
    }
    week.append(headers, bands, appointments);
    return week;
  }
  function combineEvents(events) {
    const grouped = new Map();
    for (const event of events) {
      const title = event.title.trim().toLocaleLowerCase();
      const venue = (event.location || '').split(',')[0].trim().toLocaleLowerCase();
      const suffix = ' at ' + venue;
      const name = venue && title.endsWith(suffix) ? title.slice(0, -suffix.length) : title;
      const calendar = event.calendar || event.source.replace(/^calendar:/, '');
      const signature = JSON.stringify([name, new Date(event.start).getTime(), new Date(event.end).getTime(), event.all_day]);
      const other = grouped.get(signature);
      if (!other || other.source === event.source) {
        grouped.set(other ? signature + event.id : signature, {...event, calendars:[calendar]});
      } else {
        if (!other.calendars.includes(calendar)) other.calendars.push(calendar);
        if (event.title.length < other.title.length) other.title = event.title;
        if (!other.url && event.url) other.url = event.url;
        if (!other.location && event.location) other.location = event.location;
        if (!other.category && event.category) other.category = event.category;
        if (!other.display_title && event.display_title) other.display_title = event.display_title;
      }
    }
    return [...grouped.values()];
  }
  function renderCalendar(data, start, end) {
    const formatTime = new Intl.DateTimeFormat('en-GB', {timeZone: data.timezone, hour:'2-digit', minute:'2-digit'});
    const container = $('calendar');
    container.className = $('mode').value === '4' ? 'four' : 'week';
    const byDay = new Map();
    for (const row of data.calendar) {
      if (!byDay.has(row.day)) byDay.set(row.day, []);
      byDay.get(row.day).push(row);
    }
    const days = [];
    const formatDay = new Intl.DateTimeFormat('en-CA', {timeZone:data.timezone, year:'numeric', month:'numeric', day:'numeric'});
    for (let day = start; day < end; day = plus(day, 1)) {
      const id = iso(day);
      const rows = byDay.get(id) || [];
      const events = combineEvents(rows.flatMap(row => row.events.map(event => ({...event, source: row.source}))));
      events.sort((a, b) => new Date(a.start) - new Date(b.start) || eventLabel(a).localeCompare(eventLabel(b)));
      const bands = events.filter(event => event.all_day || multiDay(event, formatDay));
      const timed = events.filter(event => !bands.includes(event));
      const online = new Set(rows.filter(row => data.sources.includes(row.source)).map(row => row.source)).size;
      const status = !online ? 'Not collected' : online < data.sources.length ? 'Partial coverage' : !events.length ? 'No events' : '';
      const appointments = (data.appointments || []).filter(appointment => appointment.day === id);
      const localReminders = (data.local_reminders || []).filter(reminder => reminder.day === id);
      days.push({id, date:day, available:online > 0, status, bands, timed, formatTime, appointments, localReminders});
    }
    const width = $('mode').value === '4' ? 4 : 7;
    const shown = new Set(), weeks = [];
    for (let i = 0; i < days.length; i += width) weeks.push(calendarWeek(days.slice(i, i + width), shown));
    container.replaceChildren(...weeks);
    $('range').textContent = $('mode').value === 'month' ? calendarMonth.format(selected) :
      calendarDate.format(start) + ' – ' + calendarDate.format(plus(end, -1));
    $('coverage').textContent = 'Times in ' + data.timezone + '. Shaded dates have no locally collected calendar.';
  }
  async function loadReport() {
    const version = ++reportRequest;
    const day = $('reports').value;
    if (!day) { $('report').replaceChildren(node('p', 'No saved mail reports in this window.')); return; }
    const clock = new Intl.DateTimeFormat('en-GB', {timeStyle:'short', timeZone:timezone});
    try {
      const reports = await get('/api/daily-reports/' + encodeURIComponent(day));
      if (version !== reportRequest) return;
      $('report').replaceChildren(...reports.map((report, index) => {
        const article = node('article', undefined, 'report-content');
        const label = (index ? 'Follow-up' : 'Report') + ' · ' + clock.format(new Date(report.started_at));
        article.append(node('h3', label + (report.status === 'partial' ? ' · Partial' : ''), 'report-time'),
                       reportContents(report.markdown));
        return article;
      }));
    } catch (error) { if (version === reportRequest) showError(error); }
  }
  async function load() {
    const version = ++request;
    const {start, end} = windowDates();
    $('error').hidden = true;
    try {
      const data = await get('/api/dashboard?start=' + iso(start) + '&until=' + iso(end));
      if (version !== request) return;
      if (!today && iso(selected) !== data.today) {
        today = data.today;
        return go(civil(today));
      }
      today = data.today;
      timezone = data.timezone;
      renderCalendar(data, start, end);
      const current = $('reports').value;
      $('reports').replaceChildren(...data.report_days.map(day => {
        const option = node('option', calendarDate.format(civil(day)) + (day === today ? ' · Today' : ''));
        option.value = day;
        return option;
      }));
      if (data.report_days.includes(current)) $('reports').value = current;
      else if (data.report_days.includes(today)) $('reports').value = today;
      $('reports').disabled = !data.report_days.length;
      await loadReport();
      if (version === request && !$('local-dialog').open && !$('appointment-dialog').open) {
        const pending = data.reminders.find(item => item.due);
        if (pending) displayAppointment(pending);
      }
    } catch (error) { if (version === request) showError(error); }
  }
  function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
  function go(date) {
    selected = date;
    $('date').value = iso(selected);
    return load();
  }
  function move(direction) {
    const mode = $('mode').value;
    const days = mode === '4' ? 4 : 7;
    return go(mode === 'month' ? monthStart(selected, direction) : plus(selected, direction * days));
  }
  $('today').addEventListener('click', () => go(civil(today || iso(new Date()))));
  $('previous').addEventListener('click', () => move(-1));
  $('next').addEventListener('click', () => move(1));
  $('mode').addEventListener('change', load);
  $('date').addEventListener('change', () => { if ($('date').value) go(civil($('date').value)); });
  $('reports').addEventListener('change', loadReport);
  const ackButton = $('ack-appointment');
  const appointmentDialog = $('appointment-dialog');
  const progress = $('appointment-progress');
  const holdDuration = 5000;
  let appointment, hold, frame;
  function stopHold() {
    cancelAnimationFrame(frame);
    hold = null;
    if (!ackButton.disabled) progress.value = 0;
  }
  function displayAppointment(reminder) {
    stopHold();
    appointment = reminder;
    $('appointment-title').textContent = reminder.title;
    $('appointment-message').replaceChildren(inline(node('p'), reminder.text));
    $('appointment-status').textContent = '';
    $('appointment-acknowledgement').hidden = !reminder.due;
    $('close-appointment').hidden = reminder.due;
    appointmentDialog.classList.toggle('acknowledgement-due', reminder.due);
    appointmentDialog.showModal();
  }
  function closePreview() {
    if (!appointment.due) appointmentDialog.close();
  }
  $('close-appointment').addEventListener('click', closePreview);
  appointmentDialog.addEventListener('cancel', event => {
    event.preventDefault();
    stopHold();
    closePreview();
  });
  async function acknowledgeAppointment() {
    ackButton.disabled = true;
    $('appointment-status').textContent = 'Saving acknowledgement…';
    try {
      const {id, case_id, revision} = appointment;
      await get('/api/appointment-ack', {id, case_id, revision});
      appointmentDialog.close();
      ackButton.disabled = false;
      await load();
    } catch (error) {
      $('appointment-status').textContent = error.message;
    } finally {
      ackButton.disabled = false;
      progress.value = 0;
    }
  }
  function startHold(input) {
    if (hold || ackButton.disabled || !appointment?.due) return;
    hold = {...input, started:performance.now()};
    $('appointment-status').textContent = '';
    function tick(now) {
      if (!hold) return;
      const elapsed = now - hold.started;
      progress.value = Math.min(elapsed, holdDuration) / 1000;
      if (elapsed < holdDuration) {
        frame = requestAnimationFrame(tick);
      } else {
        hold = null;
        acknowledgeAppointment();
      }
    }
    frame = requestAnimationFrame(tick);
  }
  ackButton.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || hold) return;
    event.preventDefault();
    ackButton.focus();
    ackButton.setPointerCapture(event.pointerId);
    startHold({pointerId:event.pointerId});
  });
  ackButton.addEventListener('pointermove', event => {
    if (hold?.pointerId !== event.pointerId) return;
    const rect = ackButton.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom) stopHold();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    ackButton.addEventListener(type, event => { if (hold?.pointerId === event.pointerId) stopHold(); });
  }
  ackButton.addEventListener('keydown', event => {
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault();
    if (!event.repeat) startHold({key:event.key});
  });
  ackButton.addEventListener('keyup', event => {
    if (hold?.key === event.key) stopHold();
  });
  ackButton.addEventListener('blur', stopHold);
  window.addEventListener('blur', stopHold);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopHold(); });

  let localAttempt;
  function setLocalDateInputs() {
    const allDay = $('all-day').checked;
    for (const [id, hour] of [['local-start', '09:00'], ['local-end', '10:00']]) {
      const field = $(id), date = field.value.slice(0, 10) || iso(selected);
      field.type = allDay ? 'date' : 'datetime-local';
      field.value = allDay ? date : date + 'T' + hour;
    }
    $('local-end-label').textContent = allDay ? 'Last day' : 'Ends';
  }
  $('add-local').addEventListener('click', () => {
    setLocalDateInputs();
    $('local-dialog').showModal();
  });
  $('all-day').addEventListener('change', setLocalDateInputs);
  $('cancel-local').addEventListener('click', () => $('local-dialog').close());
  $('local-dialog').addEventListener('cancel', event => { if ($('save-local').disabled) event.preventDefault(); });
  $('local-form').addEventListener('submit', async event => {
    event.preventDefault();
    const all_day = $('all-day').checked;
    const start = $('local-start').value;
    const until = $('local-end').value;
    const end = all_day ? iso(plus(civil(until), 1)) : until;
    const payload = {title:$('local-name').value, location:$('local-location').value, start, end, all_day};
    const fingerprint = JSON.stringify(payload);
    if (localAttempt?.fingerprint !== fingerprint) localAttempt = {fingerprint, key:crypto.randomUUID()};
    $('save-local').disabled = $('cancel-local').disabled = true;
    $('local-status').textContent = 'Saving…';
    try {
      await get('/api/local-events', {...payload, idempotency_key:localAttempt.key});
      localAttempt = null;
      $('local-form').reset();
      $('local-dialog').close();
      $('local-status').textContent = '';
      await go(civil(start.slice(0, 10)));
    } catch (error) {
      $('local-status').textContent = error.message;
    } finally {
      $('save-local').disabled = $('cancel-local').disabled = false;
    }
  });
  go(civil(iso(new Date())));
})();
