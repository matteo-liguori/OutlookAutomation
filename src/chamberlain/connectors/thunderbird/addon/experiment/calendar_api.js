/* Narrow privileged calendar helpers (bridge contract B-3 and section 5, calendar). Eight named
 * functions, no generic execute. Each takes an explicit calendar ID; the add-on's TypeScript
 * decides what may be written and tracks whether a mutation was issued. Runs only inside
 * Thunderbird; the fake used by the tests lives in test/fake_thunderbird.mjs. */
/* global ChromeUtils, Ci, Services */
var { ExtensionCommon } = ChromeUtils.importESModule("resource://gre/modules/ExtensionCommon.sys.mjs");
var { cal } = ChromeUtils.importESModule("resource:///modules/calendar/calUtils.sys.mjs");
var { CalIcsParser } = ChromeUtils.importESModule("resource:///modules/CalIcsParser.sys.mjs");

function need(value, code) {
  if (!value) throw new Error(code);
}

function calendarOf(id) {
  const calendar = cal.manager.getCalendarById(id);
  need(calendar && !calendar.getProperty("disabled"), "not_found");
  return calendar;
}

function pad(n, width = 2) {
  return String(n).padStart(width, "0");
}

/** `YYYYMMDD` for an all-day id, `YYYYMMDDTHHMMSSZ` otherwise: the form the connector reads. */
function recurrenceText(rid) {
  if (!rid) return null;
  if (rid.isDate) return String(rid.icalString).slice(0, 8);
  const d = new Date(rid.nativeTime / 1000);
  return `${pad(d.getUTCFullYear(), 4)}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}Z`;
}

function itemOf(e) {
  return {
    id: e.id,
    recurrence_id: recurrenceText(e.recurrenceId),
    icalendar: e.icalString,
    invited: e.getAttendees().length > 0 || !!e.organizer,
  };
}

function parseOne(text) {
  const parser = new CalIcsParser();
  parser.parseString(text);
  const items = parser.getItems();
  if (items.length !== 1 || parser.getParentlessItems().length) return { error: "one_master_event_required" };
  const e = items[0];
  if (!e.isEvent() || !e.id || e.recurrenceId) return { error: "master_event_required" };
  if (!e.startDate || !e.endDate || e.startDate.compare(e.endDate) >= 0) return { error: "invalid_event_range" };
  return { event: e };
}

var harnessCalendar = class extends ExtensionCommon.ExtensionAPI {
  getAPI() {
    return {
      harnessCalendar: {
        async isOffline() {
          return !!Services.io.offline;
        },
        async listCalendars() {
          return cal.manager.getCalendars().map((c) => ({
            id: c.id,
            name: c.name,
            type: c.type,
            read_only: !!c.readOnly,
            disabled: !!c.getProperty("disabled"),
          }));
        },
        async queryEvents(calendarId, startMs, endMs, limit) {
          const calendar = calendarOf(calendarId);
          const start = cal.createDateTime();
          const end = cal.createDateTime();
          start.nativeTime = startMs * 1000;
          end.nativeTime = endMs * 1000;
          const found = await cal.iterate.streamToArray(
            calendar.getItems(Ci.calICalendar.ITEM_FILTER_TYPE_EVENT | Ci.calICalendar.ITEM_FILTER_CLASS_OCCURRENCES, limit + 1, start, end)
          );
          found.sort((a, b) => a.startDate.compare(b.startDate) || a.id.localeCompare(b.id));
          return { items: found.slice(0, limit).map(itemOf), truncated: found.length > limit };
        },
        async getEvent(calendarId, eventId) {
          const found = await calendarOf(calendarId).getItem(eventId);
          return found ? itemOf(found) : null;
        },
        async inspectEvent(icalendar) {
          const parsed = parseOne(icalendar);
          if (parsed.error) return { ok: false, error: parsed.error };
          const e = parsed.event;
          return { ok: true, id: e.id, invited: e.getAttendees().length > 0 || !!e.organizer };
        },
        async addEvent(calendarId, icalendar) {
          const parsed = parseOne(icalendar);
          need(parsed.event, "bad_request");
          await calendarOf(calendarId).addItem(parsed.event);
          return {};
        },
        async modifyEvent(calendarId, eventId, icalendar) {
          const calendar = calendarOf(calendarId);
          const parsed = parseOne(icalendar);
          need(parsed.event && parsed.event.id === eventId, "bad_request");
          const before = await calendar.getItem(eventId);
          need(before, "not_found");
          await calendar.modifyItem(parsed.event, before);
          return {};
        },
        async deleteEvent(calendarId, eventId) {
          const calendar = calendarOf(calendarId);
          const before = await calendar.getItem(eventId);
          need(before, "not_found");
          await calendar.deleteItem(before);
          return {};
        },
      },
    };
  }
};
