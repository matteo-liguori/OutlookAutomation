// Calendar commands against a scripted Thunderbird calendar provider. Names carry the rule
// they enforce (docs/contracts/12-bridge-v2.md section 5, calendar; error rules in section 7).
import assert from "node:assert/strict";
import test from "node:test";
import { createWorld } from "./fake_thunderbird.mjs";
import { call, requestId, startAddon, untilResponse } from "./loader.mjs";

const CAL = "cal1";

function setup() {
  const world = createWorld();
  return { world, addon: startAddon(world) };
}

function ics(uid, { start = "20270301T100000Z", end = "20270301T103000Z", title = "Event", extra = "" } = {}) {
  return [
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//test//EN",
    "BEGIN:VEVENT",
    "UID:" + uid,
    "DTSTAMP:20270101T000000Z",
    "DTSTART:" + start,
    "DTEND:" + end,
    "SUMMARY:" + title,
    ...(extra ? [extra] : []),
    "END:VEVENT",
    "END:VCALENDAR",
    "",
  ].join("\r\n");
}

const window = { calendar_id: CAL, start: "2027-03-01T00:00:00+00:00", end: "2027-03-02T00:00:00+00:00", limit: 50 };

test("C-1 calendar is advertised only when the privileged helper is present, and mail does not need it", async () => {
  const world = createWorld();
  delete world.messenger.chamberlainCalendar;
  const addon = startAddon(world);
  await new Promise((r) => setTimeout(r, 10));
  const caps = addon.posted.find((f) => f.t === "addon_hello").capabilities;
  assert.ok(!caps.includes("calendar"));
  assert.ok(caps.includes("read"));
  const refused = await call(addon, "calendar_list", {});
  assert.equal(refused.error.code, "unsupported");
  assert.equal(refused.error.mutation, "none");
  const withIt = setup();
  await new Promise((r) => setTimeout(r, 10));
  assert.ok(withIt.addon.posted.find((f) => f.t === "addon_hello").capabilities.includes("calendar"));
});

test("C-2 calendar_list reports id, name, type, read-only and disabled flags and nothing else", async () => {
  const { addon, world } = setup();
  world.addCalendar("cal2", { name: "Shared", readOnly: true, disabled: true });
  const res = await call(addon, "calendar_list", {});
  assert.equal(res.ok, true);
  const shared = res.result.calendars.find((c) => c.id === "cal2");
  assert.deepEqual(Object.keys(shared).sort(), ["disabled", "id", "name", "read_only", "type"]);
  assert.equal(shared.read_only, true);
  assert.equal(shared.disabled, true);
});

test("C-3 calendar_query is bounded: only the window, occurrences included, the limit marks it incomplete", async () => {
  const { addon, world } = setup();
  const c = world.calendars.get(CAL);
  c.items.set("in", ics("in"));
  c.items.set("out", ics("out", { start: "20270401T100000Z", end: "20270401T103000Z" }));
  c.items.set("series", ics("series", { extra: "RRULE:FREQ=DAILY" }));
  c.occurrences.push({ id: "series", recurrence_id: "20270301T100000Z", icalendar: ics("series", { extra: "RECURRENCE-ID:20270301T100000Z" }) });
  const res = await call(addon, "calendar_query", window);
  assert.equal(res.ok, true);
  assert.deepEqual(
    res.result.items.map((i) => [i.id, i.recurrence_id]).sort(),
    [["in", null], ["series", "20270301T100000Z"]],
  );
  assert.equal(res.result.complete, true);
  assert.deepEqual(res.result.gaps, []);
  const cut = await call(addon, "calendar_query", { ...window, limit: 1 });
  assert.equal(cut.result.items.length, 1);
  assert.equal(cut.result.complete, false);
});

test("C-3 an offline Thunderbird makes a query incomplete and says why", async () => {
  const { addon, world } = setup();
  world.calendarOffline = true;
  const res = await call(addon, "calendar_query", window);
  assert.equal(res.result.complete, false);
  assert.match(res.result.gaps[0], /offline/i);
});

test("C-3 a window that is not bounded, or an unknown calendar, is refused", async () => {
  const { addon } = setup();
  const backwards = await call(addon, "calendar_query", { ...window, start: window.end, end: window.start });
  assert.equal(backwards.error.code, "bad_request");
  const unknown = await call(addon, "calendar_query", { ...window, calendar_id: "nope" });
  assert.equal(unknown.error.code, "not_found");
  const extra = await call(addon, "calendar_query", { ...window, shell: "x" });
  assert.equal(extra.error.code, "bad_request");
});

test("C-4 calendar_read returns the exact stored text, or null when there is no such event", async () => {
  const { addon, world } = setup();
  const text = ics("one");
  world.calendars.get(CAL).items.set("one", text);
  const found = await call(addon, "calendar_read", { calendar_id: CAL, event_id: "one" });
  assert.deepEqual(found.result.item, { id: "one", recurrence_id: null, icalendar: text });
  const missing = await call(addon, "calendar_read", { calendar_id: CAL, event_id: "none" });
  assert.equal(missing.ok, true);
  assert.equal(missing.result.item, null);
});

test("C-5 create adds the event, reads it back and reports per-stage timing", async () => {
  const { addon, world } = setup();
  const text = ics("new1", { title: "Made" });
  const res = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "new1", icalendar: text });
  assert.equal(res.ok, true);
  assert.equal(res.result.item.icalendar, text);
  assert.deepEqual(Object.keys(res.result.timing_ms), ["calendar", "read_before", "provider", "read_after"]);
  assert.deepEqual(world.calendarLog, ["add"]);
});

test("C-5 create refuses an existing id, a UID that is not the id, and invalid text, all without mutation", async () => {
  const { addon, world } = setup();
  world.calendars.get(CAL).items.set("dup", ics("dup"));
  const exists = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "dup", icalendar: ics("dup") });
  assert.equal(exists.error.code, "changed");
  assert.equal(exists.error.mutation, "none");
  const mismatch = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "a", icalendar: ics("b") });
  assert.equal(mismatch.error.code, "bad_request");
  const backwards = await call(addon, "calendar_create", {
    calendar_id: CAL,
    event_id: "c",
    icalendar: ics("c", { start: "20270301T110000Z", end: "20270301T100000Z" }),
  });
  assert.equal(backwards.error.code, "bad_request");
  const series = await call(addon, "calendar_create", {
    calendar_id: CAL,
    event_id: "d",
    icalendar: ics("d", { extra: "RECURRENCE-ID:20270301T100000Z" }),
  });
  assert.equal(series.error.code, "bad_request");
  assert.deepEqual(world.calendarLog, []);
  for (const r of [mismatch, backwards, series]) assert.equal(r.error.mutation, "none");
});

test("C-6 an event with attendees or an organiser is never written, by create, update or delete", async () => {
  const { addon, world } = setup();
  for (const extra of ["ATTENDEE:mailto:a@example.org", "ORGANIZER:mailto:b@example.org"]) {
    const res = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "inv", icalendar: ics("inv", { extra }) });
    assert.equal(res.error.code, "unsupported");
    assert.equal(res.error.mutation, "none");
  }
  const invited = ics("held", { extra: "ORGANIZER:mailto:b@example.org" });
  world.calendars.get(CAL).items.set("held", invited);
  const update = await call(addon, "calendar_update", {
    calendar_id: CAL,
    event_id: "held",
    expected_icalendar: invited,
    icalendar: ics("held", { title: "x" }),
  });
  assert.equal(update.error.code, "unsupported");
  const remove = await call(addon, "calendar_delete", { calendar_id: CAL, event_id: "held", expected_icalendar: invited });
  assert.equal(remove.error.code, "unsupported");
  assert.deepEqual(world.calendarLog, []);
  assert.equal(world.calendars.get(CAL).items.get("held"), invited);
});

test("C-7 update needs the exact stored text; a changed or missing event is refused untouched", async () => {
  const { addon, world } = setup();
  const original = ics("u1", { title: "Before" });
  world.calendars.get(CAL).items.set("u1", original);
  const stale = await call(addon, "calendar_update", {
    calendar_id: CAL,
    event_id: "u1",
    expected_icalendar: ics("u1", { title: "Other" }),
    icalendar: ics("u1", { title: "After" }),
  });
  assert.equal(stale.error.code, "changed");
  assert.equal(stale.error.mutation, "none");
  const missing = await call(addon, "calendar_update", { calendar_id: CAL, event_id: "zz", expected_icalendar: original, icalendar: ics("zz") });
  assert.equal(missing.error.code, "not_found");
  assert.equal(world.calendars.get(CAL).items.get("u1"), original);
  const changed = ics("u1", { title: "After" });
  const ok = await call(addon, "calendar_update", { calendar_id: CAL, event_id: "u1", expected_icalendar: original, icalendar: changed });
  assert.equal(ok.ok, true);
  assert.equal(ok.result.item.icalendar, changed);
  assert.deepEqual(world.calendarLog, ["modify"]);
});

test("C-8 delete needs the exact stored text and removes the event; the read-back is null", async () => {
  const { addon, world } = setup();
  const text = ics("d1");
  world.calendars.get(CAL).items.set("d1", text);
  const stale = await call(addon, "calendar_delete", { calendar_id: CAL, event_id: "d1", expected_icalendar: ics("d1", { title: "x" }) });
  assert.equal(stale.error.code, "changed");
  assert.equal(world.calendars.get(CAL).items.has("d1"), true);
  const ok = await call(addon, "calendar_delete", { calendar_id: CAL, event_id: "d1", expected_icalendar: text });
  assert.equal(ok.ok, true);
  assert.equal(ok.result.item, null);
  assert.equal(world.calendars.get(CAL).items.has("d1"), false);
});

test("C-9 a read-only calendar and an offline client refuse writes before any provider call", async () => {
  const { addon, world } = setup();
  world.addCalendar("ro", { readOnly: true });
  const readOnly = await call(addon, "calendar_create", { calendar_id: "ro", event_id: "r", icalendar: ics("r") });
  assert.equal(readOnly.error.code, "unsupported");
  assert.equal(readOnly.error.mutation, "none");
  world.addCalendar("off", { disabled: true });
  const disabled = await call(addon, "calendar_create", { calendar_id: "off", event_id: "r", icalendar: ics("r") });
  assert.equal(disabled.error.code, "not_found");
  world.calendarOffline = true;
  const offline = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "r", icalendar: ics("r") });
  assert.equal(offline.error.code, "client_offline");
  assert.equal(offline.error.mutation, "none");
  assert.deepEqual(world.calendarLog, []);
});

test("C-10 mutation is possible only after the provider call began", async () => {
  const { addon, world } = setup();
  const api = world.messenger.chamberlainCalendar;
  const original = api.addEvent;
  api.addEvent = async () => {
    world.calendarLog.push("add");
    throw new Error("provider exploded");
  };
  const failed = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "p1", icalendar: ics("p1") });
  assert.equal(failed.ok, false);
  assert.equal(failed.error.mutation, "possible");
  api.addEvent = original;
  const before = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "p2", icalendar: ics("other") });
  assert.equal(before.error.mutation, "none");
});

test("C-10 a write the provider silently dropped is not reported as done", async () => {
  const { addon, world } = setup();
  world.messenger.chamberlainCalendar.addEvent = async () => ({});
  const res = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "g1", icalendar: ics("g1") });
  assert.equal(res.error.code, "not_verified");
  assert.equal(res.error.mutation, "possible");
  const kept = ics("g2");
  world.calendars.get(CAL).items.set("g2", kept);
  world.messenger.chamberlainCalendar.deleteEvent = async () => ({});
  const stuck = await call(addon, "calendar_delete", { calendar_id: CAL, event_id: "g2", expected_icalendar: kept });
  assert.equal(stuck.error.code, "not_verified");
  assert.equal(stuck.error.mutation, "possible");
});

test("C-11 calendar writes share the single write slot with mail writes", async () => {
  const { addon, world } = setup();
  world.faults.slowCalendar = 1;
  const first = requestId();
  addon.deliver({ t: "request", v: 2, id: first, cmd: "calendar_create", args: { calendar_id: CAL, event_id: "s1", icalendar: ics("s1") } });
  await new Promise((r) => setTimeout(r, 30));
  const second = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "s2", icalendar: ics("s2") });
  assert.equal(second.error.code, "busy");
  assert.equal(second.error.mutation, "none");
  world.release();
  const done = await untilResponse(addon, first);
  assert.equal(done.ok, true);
});

test("C-12 a write that outlives its deadline reports mutation possible and still lands once", async () => {
  const world = createWorld();
  const addon = startAddon(world, { writeMs: 40 });
  world.faults.slowCalendar = 1;
  const res = await call(addon, "calendar_create", { calendar_id: CAL, event_id: "t1", icalendar: ics("t1") });
  assert.equal(res.error.code, "timeout");
  assert.equal(res.error.mutation, "possible");
  world.release();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(world.calendars.get(CAL).items.has("t1"), true);
  assert.deepEqual(world.calendarLog, ["add"]);
});
