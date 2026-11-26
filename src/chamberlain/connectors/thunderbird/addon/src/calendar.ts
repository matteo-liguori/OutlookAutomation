// Calendar commands (optional capability `calendar`). Reads never change anything. A write
// is checked against the stored text first, issues exactly one provider call, reads the
// event back, and says `mutation: possible` only once that call has begun (section 7).
// Guest lists and invitations are never written. Owl can supply the owner's organiser on a private appointment.

const CALENDAR_SCAN_CAP = 5000;

function calendarApi(): HarnessCalendarApi {
  let api: HarnessCalendarApi | undefined;
  try {
    api = (messenger as { chamberlainCalendar?: HarnessCalendarApi }).chamberlainCalendar;
  } catch {
    api = undefined;
  }
  if (!api) fail("unsupported", "This Thunderbird has no calendar support for the add-on.");
  return api;
}

function calendarSupported(): boolean {
  try {
    return typeof (messenger as { chamberlainCalendar?: HarnessCalendarApi }).chamberlainCalendar?.listCalendars === "function";
  } catch {
    return false;
  }
}

function calendarItem(found: RawCalendarItem): CalendarItem {
  return { id: found.id, recurrence_id: found.recurrence_id, icalendar: clip(found.icalendar, 200_000), own_appointment: found.own_appointment ? true : undefined };
}

async function usableCalendar(api: HarnessCalendarApi, id: string, forWrite: boolean): Promise<RawCalendar> {
  const found = (await api.listCalendars()).find((c) => c.id === id);
  check(found && !found.disabled, "not_found", "No such calendar.");
  if (forWrite) {
    check(!found.read_only, "unsupported", "That calendar is read-only.");
    check(!(await api.isOffline()), "client_offline", "Thunderbird is offline.");
  }
  return found;
}

async function cmdCalendarList(): Promise<CalendarListResult> {
  const api = calendarApi();
  const calendars = await api.listCalendars();
  return {
    calendars: calendars.map((c) => ({ id: c.id, name: clip(c.name, 2000), type: clip(c.type, 100), read_only: c.read_only, disabled: c.disabled })),
  };
}

async function cmdCalendarQuery(a: CalendarQueryArgs): Promise<CalendarQueryResult> {
  const api = calendarApi();
  await usableCalendar(api, a.calendar_id, false);
  const start = Date.parse(a.start);
  const end = Date.parse(a.end);
  check(Number.isFinite(start) && Number.isFinite(end) && start < end, "bad_request", "A bounded window is required.");
  const limit = Math.min(a.limit, CALENDAR_SCAN_CAP);
  const found = await api.queryEvents(a.calendar_id, start, end, limit);
  const gaps: string[] = [];
  if (await api.isOffline()) gaps.push("Thunderbird is offline; the calendar may be out of date.");
  return { items: found.items.slice(0, limit).map(calendarItem), complete: !found.truncated && gaps.length === 0, gaps };
}

async function cmdCalendarRead(a: CalendarReadArgs): Promise<CalendarReadResult> {
  const api = calendarApi();
  await usableCalendar(api, a.calendar_id, false);
  const found = await api.getEvent(a.calendar_id, a.event_id);
  return { item: found ? calendarItem(found) : null };
}

async function cmdCalendarWrite(a: CalendarWriteArgs, ctx: Ctx): Promise<CalendarWriteResult> {
  const api = calendarApi();
  await usableCalendar(api, a.calendar_id, true);
  const before = await api.getEvent(a.calendar_id, a.event_id);
  if (a.kind === "create") {
    check(!before, "changed", "An event with that ID already exists.");
  } else {
    check(before, "not_found", "That event is not in the calendar.");
    check(before.icalendar === a.expected_icalendar, "changed", "The event changed since it was read.");
    check(!before.invited, "unsupported", "Events with attendees or an organiser are not written.");
  }
  let eventId = a.event_id;
  if (a.kind === "delete") {
    markMutation(ctx);
    await api.deleteEvent(a.calendar_id, a.event_id);
  } else {
    check(a.icalendar != null, "bad_request", "Event text is required.");
    const wanted = await api.inspectEvent(a.icalendar, a.calendar_id);
    check(wanted.ok, "bad_request", "The event is not a single valid event: " + clip(wanted.error, 80));
    check(!wanted.invited && (!wanted.own_appointment || (a.kind === "update" && before?.own_appointment)),
      "unsupported", "Only an existing private appointment may keep its own organiser.");
    check(wanted.id === a.event_id, "bad_request", "The event's UID is not the one named.");
    markMutation(ctx);
    if (a.kind === "create") {
      const created = await api.addEvent(a.calendar_id, a.icalendar);
      check(created?.id && !created.recurrence_id, "not_verified", "The provider did not return the created event ID.", "possible");
      eventId = created.id;
    } else {
      await api.modifyEvent(a.calendar_id, a.event_id, a.icalendar);
    }
  }
  const after = await api.getEvent(a.calendar_id, eventId);
  if (a.kind === "delete") check(!after, "not_verified", "The event is still in the calendar.", "possible");
  else check(after?.id === eventId && !after.recurrence_id && !after.invited,
    "not_verified", "The event could not be read back as a guestless appointment.", "possible");
  return { item: after ? calendarItem(after) : null };
}
