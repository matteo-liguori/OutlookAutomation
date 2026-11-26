// Hosts the real add-on against a scripted Thunderbird, speaking native-messaging frames on
// stdin/stdout, with a loopback control socket for seeding, inspection and fault injection.
// Used by the Python rig (tests/thunderbird/rig.py). Usage: node harness.mjs <control port> <read ms> <write ms> <assembly ms>
import net from "node:net";
import { ACCOUNT, createWorld, OWNER } from "./fake_thunderbird.mjs";
import { startAddon } from "./loader.mjs";

const [controlPort, readMs, writeMs, assemblyMs] = process.argv.slice(2).map(Number);
const WRITES = new Set(["compose", "change", "draft_discard", "calendar_write"]);
const ROLES = {
  inbox: ACCOUNT + "://INBOX",
  sent: ACCOUNT + "://Sent",
  drafts: ACCOUNT + "://Drafts",
  trash: ACCOUNT + "://Trash",
  archive: ACCOUNT + "://Archive",
  filed: ACCOUNT + "://Filed",
};

const world = createWorld();
const faults = { loseBefore: 0, loseResponse: 0, busy: 0 };
const dropIds = new Set();

function writeFrame(frame) {
  const body = Buffer.from(JSON.stringify(frame), "utf8");
  const head = Buffer.alloc(4);
  head.writeUInt32LE(body.length);
  process.stdout.write(Buffer.concat([head, body]));
}

const addon = startAddon(world, {
  readMs,
  writeMs,
  assemblyMs,
  sink: (frame) => {
    if (frame.t === "response" && WRITES.has(frame.cmd) && faults.loseResponse > 0 && !(frame.error && frame.error.code === "busy")) {
      faults.loseResponse -= 1;
      return;
    }
    if (frame.t === "response" && frame.error && frame.error.code === "busy" && faults.busy > 0) {
      faults.busy -= 1;
      addon.setBusy(false);
    }
    writeFrame(frame);
  },
});

let pending = Buffer.alloc(0);
process.stdin.on("data", (data) => {
  pending = Buffer.concat([pending, data]);
  for (;;) {
    if (pending.length < 4) return;
    const size = pending.readUInt32LE(0);
    if (pending.length < 4 + size) return;
    const frame = JSON.parse(pending.subarray(4, 4 + size).toString("utf8"));
    pending = pending.subarray(4 + size);
    if (frame.t === "request" && WRITES.has(frame.cmd) && faults.loseBefore > 0) {
      faults.loseBefore -= 1;
      dropIds.add(frame.id);
      continue;
    }
    if (dropIds.has(frame.id)) continue;
    addon.deliver(frame);
  }
});
process.stdin.on("end", () => process.exit(0));

const controls = {
  seed: ({ role, id, subject, body, unread, attachments, date_ms }) => {
    const m = world.add({
      folderId: ROLES[role],
      headerMessageId: id,
      date: date_ms,
      subject,
      body,
      author: role === "sent" || role === "drafts" ? OWNER : undefined,
      read: !unread,
      attachments: (attachments ?? []).map((a) => ({ name: a.name, contentType: "application/octet-stream", bytes: Uint8Array.from(Buffer.from(a.b64, "base64")) })),
    });
    return { folder_id: m.folderId };
  },
  remove: ({ id, folder_id }) => {
    const bare = String(id).replace(/^<|>$/g, "");
    world.messages = world.messages.filter((m) => !(m.headerMessageId === bare && (!folder_id || m.folderId === folder_id)));
    return true;
  },
  attachment_sizes: ({ id, folder_id }) => {
    const m = world.find(id, folder_id);
    return m ? m.attachments.map((a) => [a.name, a.bytes.length, Buffer.from(a.bytes).toString("base64")]) : [];
  },
  copies: ({ id, folder_id }) => world.copies(id, folder_id),
  unread: ({ folder_id, id }) => {
    const m = world.find(id, folder_id);
    if (!m) throw new Error("no such message");
    return !m.read;
  },
  inject: ({ fault }) => {
    if (fault === "lose_before") faults.loseBefore = 1;
    else if (fault === "lose_response") faults.loseResponse = 1;
    else if (fault === "busy") {
      faults.busy = 1;
      addon.setBusy(true);
    } else if (fault === "slow") world.faults.slow = 1;
    else if (fault === "calendar_slow") world.faults.slowCalendar = 1;
    else if (fault === "calendar_offline") world.calendarOffline = true;
    else if (fault === "duplicate") world.faults.duplicate = 1;
    else if (fault === "stale") world.faults.stale = true;
    else if (fault === "fcc_prompt") world.faults.fccPrompt = true;
    else if (fault === "no_sent_copy") world.faults.noSentCopy = true;
    else if (fault === "queue_only") world.faults.queueOnly = true;
    else if (fault === "merge") world.faults.merge = true;
    else return false;
    return true;
  },
  restart: () => {
    world.restart();
    return true;
  },
  advance: async () => {
    world.release();
    await new Promise((r) => setTimeout(r, 150));
    return true;
  },
  contact: ({ book, id, name, emails }) => {
    world.books.push({ book, id, properties: { DisplayName: name, PrimaryEmail: emails[0], SecondEmail: emails[1] } });
    return true;
  },
  calendar_seed: ({ calendar_id, id, icalendar }) => {
    world.calendars.get(calendar_id).items.set(id, icalendar);
    return true;
  },
  calendar_occurrence: ({ calendar_id, id, recurrence_id, icalendar }) => {
    const c = world.calendars.get(calendar_id);
    c.items.set(id, icalendar);
    c.occurrences.push({ id, recurrence_id, icalendar });
    return true;
  },
  calendar_count: ({ calendar_id, id }) => (world.calendars.get(calendar_id).items.has(id) ? 1 : 0),
  calendar_text: ({ calendar_id, id }) => world.calendars.get(calendar_id).items.get(id) ?? null,
  counters: () => ({ ...world.counters }),
  owner: () => OWNER,
};

const control = net.connect(controlPort, "127.0.0.1");
let buffered = "";
control.on("data", async (chunk) => {
  buffered += chunk.toString("utf8");
  let at;
  while ((at = buffered.indexOf("\n")) >= 0) {
    const line = buffered.slice(0, at);
    buffered = buffered.slice(at + 1);
    const request = JSON.parse(line);
    let reply;
    try {
      reply = { id: request.id, result: await controls[request.op](request.args ?? {}) };
    } catch (error) {
      reply = { id: request.id, error: String(error.message ?? error) };
    }
    control.write(JSON.stringify(reply) + "\n");
  }
});
control.on("error", () => process.exit(1));
