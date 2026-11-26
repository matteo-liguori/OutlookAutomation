// Add-on tests against a scripted Thunderbird. Each name carries the rule it enforces
// (bridge contract B-n, port rules P-n, write rules W-n).
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ACCOUNT, createWorld, OWNER } from "./fake_thunderbird.mjs";
import { BASE, call as callWire, requestId, startAddon, untilResponse } from "./loader.mjs";

const INBOX = ACCOUNT + "://INBOX";
const SENT = ACCOUNT + "://Sent";
const DRAFTS = ACCOUNT + "://Drafts";
const TRASH = ACCOUNT + "://Trash";
const FILED = ACCOUNT + "://Filed";
const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");

function setup(options) {
  const world = createWorld();
  return { world, addon: startAddon(world, options) };
}

const ref = (folder_id, id) => ({ folder_id, internet_message_id: id });
const group = (folder_id, ...ids) => ({ folder_id, ids });
const change = (ids, extra = {}) => ({ ...BASE, items: [group(INBOX, ...ids)], trash: false, ...extra });
const lookup = (folder, ...ids) => ({ ...group(folder, ...ids), inspect_draft: false, include_attachment_hashes: false });
const subjectScope = (folder_id, subject, extra = {}) => ({
  folder_id, subject, since_ms: 0, inspect_draft: false, include_attachment_hashes: false, ...extra,
});
const copies = (where, refresh = false) => ({ ...BASE, where, refresh });
const defaults = {
  query: { max_scan: 2000, limit: 25 },
  read: { raw_hash: false },
  contacts: { limit: 10, skip: 0, max_items: 2000 },
};
const call = (addon, cmd, args, options) => callWire(addon, cmd, { ...defaults[cmd], ...args }, options);
const compose = (extra = {}) => ({
  ...BASE,
  mode: "send",
  to: [OWNER],
  cc: [],
  bcc: [],
  attachments: [],
  high_importance: false,
  display_only: false,
  subject: "Contract send",
  body: "Body.",
  sent_folder_id: SENT,
  draft_folder_id: DRAFTS,
  token: "<tok-1@chamberlain.local>",
  ...extra,
});

test("B-2 a command outside the closed list is refused and executes nothing", async () => {
  const { addon, world } = setup();
  const res = await call(addon, "eval", { code: "1" });
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "unknown_command");
  assert.equal(world.counters.move, 0);
});

test("B-2 a malformed request object is refused before anything runs", async () => {
  const { addon } = setup();
  const malformed = await callWire(addon, "folders", []);
  assert.equal(malformed.error.code, "bad_request");
  assert.equal(malformed.error.mutation, "none");
});

test("B-1 a message with another protocol version gets a refusal", async () => {
  const { addon } = setup();
  addon.deliver({ t: "request", v: 2, id: "req99999999", cmd: "status", args: {} });
  assert.equal(addon.posted.find((f) => f.t === "refusal")?.code, "version_mismatch");
});

test("B-5 a second in-flight request with the same ID is refused", async () => {
  const { addon, world } = setup();
  const m = world.add({ folderId: INBOX, headerMessageId: "a@x" });
  world.faults.slow = 1;
  const id = requestId();
  addon.deliver({ t: "request", v: 3, id, cmd: "change", args: change(["a@x"], { move_to: FILED }) });
  await new Promise((r) => setTimeout(r, 30));
  addon.deliver({ t: "request", v: 3, id, cmd: "status", args: {} });
  const second = addon.posted.filter((f) => f.id === id && f.t === "response");
  assert.equal(second[0].error.code, "bad_request");
  world.release();
  await untilResponse(addon, id);
  assert.equal(world.counters.move, 1);
  assert.ok(m);
});

test("B-1 the version fixes the command list without feature negotiation", async () => {
  const { addon } = setup();
  await new Promise((r) => setTimeout(r, 10));
  const hello = addon.posted.find((f) => f.t === "addon_hello");
  assert.equal(hello.v, 3);
  assert.equal(Object.hasOwn(hello, "capabilities"), false);
  assert.equal((await call(addon, "status", {})).result.calendar_available, hello.calendar_available);
});

test("B-6 query pages by a keyset cursor that survives new mail", async () => {
  const { addon, world } = setup();
  for (let i = 0; i < 5; i++) world.add({ folderId: INBOX, headerMessageId: "m" + i + "@x", subject: "paging" });
  const first = await call(addon, "query", { ...BASE, folder_id: INBOX, subject: "paging", limit: 2 });
  assert.deepEqual(first.result.messages.map((m) => m.internet_message_id), ["m4@x", "m3@x"]);
  assert.equal(first.result.more, true);
  world.add({ folderId: INBOX, headerMessageId: "late@x", subject: "paging" });
  const second = await call(addon, "query", { ...BASE, folder_id: INBOX, subject: "paging", limit: 2, after: first.result.next });
  assert.deepEqual(second.result.messages.map((m) => m.internet_message_id), ["m2@x", "m1@x"]);
});

test("P-4 a scan that hits max_scan says it is incomplete", async () => {
  const { addon, world } = setup();
  for (let i = 0; i < 120; i++) world.add({ folderId: INBOX, headerMessageId: "s" + i + "@x" });
  const res = await call(addon, "query", { ...BASE, folder_id: INBOX, max_scan: 60, limit: 200 });
  assert.equal(res.result.complete, false);
});

test("P-3 P-4 query reports attachment flags in batches from one header scan", async () => {
  const { addon, world } = setup();
  const flagged = new Set([0, 50, 54]);
  for (let i = 0; i < 55; i++) {
    world.add({
      folderId: INBOX,
      headerMessageId: `flag-${i}@x`,
      attachments: flagged.has(i) ? [{ name: "a.bin", bytes: new Uint8Array(1) }] : [],
    });
  }
  const { messages, chamberlainMail: mail } = world.messenger;
  const query = messages.query;
  const headerAttachmentFlags = mail.headerAttachmentFlags;
  let queries = 0;
  const batchSizes = [];
  messages.query = (...args) => {
    queries++;
    return query(...args);
  };
  mail.headerAttachmentFlags = (account, mailbox, ids, allowOutbox) => {
    batchSizes.push(ids.length);
    return headerAttachmentFlags(account, mailbox, ids, allowOutbox);
  };
  for (const method of ["getFull", "listAttachments", "getAttachmentFile", "getRaw"]) {
    messages[method] = () => { assert.fail(`query called ${method}`); };
  }

  const res = await call(addon, "query", { ...BASE, folder_id: INBOX, limit: 55 });
  assert.equal(res.ok, true);
  assert.equal(res.result.complete, true);
  assert.equal(res.result.scanned, 55);
  assert.equal(res.result.messages.length, 55);
  assert.deepEqual(res.result.messages.filter((m) => m.has_attachments).map((m) => m.internet_message_id),
    ["flag-54@x", "flag-50@x", "flag-0@x"]);
  assert.equal(queries, 1);
  assert.deepEqual(batchSizes, [50, 5]);
  assert.ok(world.messages.every((m) => !m.read));
});

test("P-3 P-4 query repeats its filtered page once when IMAP replaces a header ID", async () => {
  const { addon, world } = setup();
  const add = (id, date, extra = {}) => world.add({
    folderId: INBOX, headerMessageId: id, subject: "Search marker", date, ...extra,
  });
  add("cursor@x", 8000);
  const stale = add("moving@x", 7000, {
    folderId: TRASH,
    attachments: [{ name: "test.bin", bytes: new Uint8Array(1) }],
  });
  const oldId = stale.id;
  add("second@x", 6000);
  add("later@x", 5000);
  add("read@x", 7700, { read: true });
  add("subject@x", 7600, { subject: "Another subject" });
  add("sender@x", 7500, { author: OWNER });
  add("too-old@x", 499);

  const { messages, chamberlainMail: mail } = world.messenger;
  const query = messages.query;
  const nativeFlags = mail.headerAttachmentFlags;
  const searches = [];
  const lookedUp = [];
  messages.query = (criteria) => {
    searches.push(structuredClone(criteria));
    return query(criteria);
  };
  mail.headerAttachmentFlags = (account, mailbox, ids, allowOutbox) => {
    lookedUp.push([...ids]);
    if (lookedUp.length === 1) {
      stale.id = world.nextId++;
      stale.folderId = INBOX;
      throw new Error("chamberlain:not_found:");
    }
    return nativeFlags(account, mailbox, ids, allowOutbox);
  };

  const res = await call(addon, "query", {
    ...BASE, subject: "Search marker", words: "alice", unread: true, since_ms: 500,
    until_ms: 9000, limit: 2, after: { date_ms: 8000, key: INBOX + "\u0000cursor@x" },
  });
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(searches.length, 2);
  assert.deepEqual(searches[0], searches[1]);
  assert.deepEqual(lookedUp, [[oldId, world.find("second@x").id], [stale.id, world.find("second@x").id]]);
  assert.deepEqual(res.result.messages.map((m) => [m.internet_message_id, m.folder_id, m.has_attachments]),
    [["moving@x", INBOX, true], ["second@x", INBOX, false]]);
  assert.equal(res.result.complete, true);
  assert.equal(res.result.more, true);
  assert.deepEqual(res.result.next, { date_ms: 6000, key: INBOX + "\u0000second@x" });
});

test("B-3 query stops at a flag permission error or a second flag lookup failure", async (t) => {
  for (const codes of [["account_mismatch"], ["not_found", "not_found"], ["not_found", "account_mismatch"]]) {
    await t.test(codes.join(" then "), async () => {
      const { addon, world } = setup();
      world.add({ folderId: INBOX, headerMessageId: "missing@x" });
      const { messages, chamberlainMail: mail } = world.messenger;
      const query = messages.query;
      let searches = 0;
      let attempts = 0;
      messages.query = (...args) => {
        searches++;
        return query(...args);
      };
      mail.headerAttachmentFlags = () => {
        const code = codes[attempts++];
        throw new Error(`chamberlain:${code}:lookup ${attempts}`);
      };
      const res = await call(addon, "query", BASE);
      assert.equal(res.error.code, codes.at(-1));
      assert.equal(res.error.message, `lookup ${codes.length}`);
      assert.equal(searches, codes.length);
      assert.equal(attempts, codes.length);
    });
  }
});

test("P-3 a read that would flip the unread flag fails instead of reporting", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "r@x", read: false });
  world.faults.flipReadOnFull = true;
  const res = await call(addon, "read", { ...BASE, folder_id: INBOX, internet_message_id: "r@x" });
  assert.equal(res.error.code, "changed");
});

test("P-3 reading, resolving and fetching an attachment leave the flag alone", async () => {
  const { addon, world } = setup();
  const bytes = Uint8Array.from({ length: 500_000 }, (_, i) => i % 251);
  const m = world.add({ folderId: INBOX, headerMessageId: "r@x", read: false, attachments: [{ name: "a.bin", contentType: "application/octet-stream", bytes }] });
  const read = await call(addon, "read", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", raw_hash: true });
  assert.equal(read.result.attachments.length, 1);
  assert.equal(read.result.header.has_attachments, true);
  assert.equal(read.result.body, "Body text.");
  const resolved = await call(addon, "copies", copies([lookup(INBOX, "r@x")]));
  assert.equal(resolved.result.scopes[0].copies[0].header.has_attachments, true);
  const att = await call(addon, "attachment", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1_000_000 });
  assert.equal(att.ok, true);
  assert.equal(m.read, false);
  assert.equal(world.counters.update, 0);
});

test("P-8 attachment downloads arrive as hashed chunks across several frames", async () => {
  const { addon, world } = setup();
  const bytes = Uint8Array.from({ length: 400_000 }, (_, i) => (i * 7) % 256);
  world.add({ folderId: INBOX, headerMessageId: "r@x", attachments: [{ name: "a.bin", contentType: "x/y", bytes }] });
  world.messenger.messages.getFull = () => assert.fail("attachment read the whole message");
  world.messenger.messages.getRaw = () => assert.fail("attachment read the raw message");
  const id = requestId();
  const res = await call(addon, "attachment", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1_000_000 }, { id });
  const chunks = addon.posted.filter((f) => f.t === "chunk" && f.id === id);
  assert.equal(res.result.media_type, "x/y");
  assert.equal(chunks.length, res.result.chunks);
  assert.ok(chunks.length > 1);
  const joined = Buffer.concat(chunks.map((c) => Buffer.from(c.data, "base64")));
  assert.equal(sha(joined), res.result.sha256);
  for (const c of chunks) assert.equal(sha(Buffer.from(c.data, "base64")), c.sha256);
  assert.equal(addon.posted.indexOf(res) > addon.posted.indexOf(chunks[chunks.length - 1]), true);
});

test("P-8 an attachment over the caller's limit is refused", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "r@x", attachments: [{ name: "a.bin", contentType: "x/y", bytes: new Uint8Array(2000) }] });
  const args = { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1000 };
  const messages = world.messenger.messages;
  const { getAttachmentFile, listAttachments } = messages;
  messages.getAttachmentFile = () => assert.fail("the declared size exceeds the limit");
  const declared = await call(addon, "attachment", args);
  assert.equal(declared.error.code, "too_large");

  messages.listAttachments = async (id) => (await listAttachments(id)).map((part) => ({ ...part, size: 1 }));
  messages.getAttachmentFile = async (id, part) => {
    const file = await getAttachmentFile(id, part);
    file.arrayBuffer = () => assert.fail("the actual size exceeds the limit");
    return file;
  };
  const actual = await call(addon, "attachment", args);
  assert.equal(actual.error.code, "too_large");
});

test("P-6 messages are found by stable identity after session ids are renumbered", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "stable@x" });
  const before = await call(addon, "copies", copies([lookup(INBOX, "stable@x")]));
  world.restart();
  const after = await call(addon, "copies", copies([lookup(INBOX, "stable@x")]));
  assert.notEqual(before.result.scopes[0].copies[0].header.session_id, after.result.scopes[0].copies[0].header.session_id);
  assert.equal(after.result.scopes[0].copies.length, 1);
});

test("B-3 account and identity are verified on every command", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "a@x" });
  const wrong = await call(addon, "read", { account_id: ACCOUNT, mailbox: "foreign@example.invalid", folder_id: INBOX, internet_message_id: "a@x" });
  assert.equal(wrong.error.code, "account_mismatch");
  assert.equal(world.counters.full, 0);
  const folder = await call(addon, "query", { ...BASE, folder_id: "local://Outbox" });
  assert.equal(folder.error.code, "account_mismatch");

  const outbox = "local://Outbox";
  world.add({ folderId: outbox, headerMessageId: "foreign@x" });
  world.add({ folderId: outbox, headerMessageId: "own@x", author: OWNER,
    attachments: [{ name: "a.bin", bytes: new Uint8Array(1) }] });
  const own = await call(addon, "copies", copies([subjectScope(null, "Hello")]));
  assert.equal(own.ok, true);
  assert.deepEqual(own.result.scopes[0].copies.map(({ header: m }) => [m.internet_message_id, m.has_attachments]), [["own@x", true]]);
});

test("P-14 move leaves one copy, none at the source, and the flag as it was", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x", read: false });
  const res = await call(addon, "change", change(["mv@x"], { move_to: FILED }));
  assert.equal(res.ok, true);
  assert.equal(res.result.read_state_preserved, true);
  assert.equal(world.find("mv@x", FILED).read, false);
  assert.equal(world.copies("mv@x", INBOX), 0);
  assert.equal(world.copies("mv@x", FILED), 1);
});

test("W-23 move marks read only when asked", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x", read: false });
  const res = await call(addon, "change", change(["mv@x"], { move_to: FILED, set_read: true }));
  assert.equal(res.result.verified, true);
  assert.equal(world.find("mv@x", FILED).read, true);
  assert.equal(res.result.items[0].was_read, false);
});

test("P-14 a move into a folder that already holds the message is ambiguous and issues nothing", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x" });
  world.add({ folderId: FILED, headerMessageId: "mv@x" });
  const res = await call(addon, "change", change(["mv@x"], { move_to: FILED }));
  assert.equal(res.error.code, "ambiguous");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.counters.move, 0);
});

test("P-6 two copies at the source make a move or trash ambiguous (dropped old behaviour)", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "two@x" });
  world.add({ folderId: INBOX, headerMessageId: "two@x" });
  const res = await call(addon, "change", change(["two@x"], { move_to: TRASH, trash: true }));
  assert.equal(res.error.code, "ambiguous");
  assert.equal(world.counters.move, 0);
});

test("P-14 a move that leaves a copy behind is not verified and says a change may have happened", async () => {
  const { addon, world } = setup({ tuning: { batchCheckMs: 35, movePollMs: 1 } });
  world.add({ folderId: INBOX, headerMessageId: "dup@x" });
  world.faults.duplicate = 1;
  const res = await call(addon, "change", change(["dup@x"], { move_to: FILED }));
  assert.equal(res.result.verified, false);
});

test("R7 trash that merges into an existing recovery copy is applied and says so", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "gm@x", read: true });
  world.add({ folderId: TRASH, headerMessageId: "gm@x", read: false });
  world.faults.merge = true;
  const res = await call(addon, "change", change(["gm@x"], { move_to: TRASH, trash: true }));
  assert.equal(res.ok, true);
  assert.equal(res.result.merged_existing_copy, true);
  assert.equal(res.result.read_state_preserved, false);
  assert.equal(world.copies("gm@x", TRASH), 1);
});

test("P-14 batch filing restores both read states with two bulk updates and skips duplicates", async () => {
  const { addon, world } = setup();
  const ids = ["read-1@x", "read-2@x", "unread-1@x", "unread-2@x"];
  for (const id of ids) world.add({ folderId: INBOX, headerMessageId: id, read: id.startsWith("read") });
  world.add({ folderId: INBOX, headerMessageId: "duplicate@x" });
  const existing = world.add({ folderId: FILED, headerMessageId: "duplicate@x", read: true });
  world.faults.flipReadOnMove = true;
  const res = await call(addon, "change", change([...ids, "duplicate@x"], { move_to: FILED }));
  assert.equal(res.result.verified, true);
  assert.equal(res.result.read_state_preserved, true);
  assert.equal(res.result.merged_existing_copy, false);
  assert.deepEqual(res.result.items.map((item) => item.code), ["", "", "", "", "ambiguous"]);
  assert.equal(world.counters.move, 1);
  assert.equal(world.counters.update, 2);
  for (const id of ids) assert.equal(world.find(id).read, id.startsWith("read"));
  assert.equal(existing.read, true);
});

test("W-23 batch filing verifies an explicitly requested read flag", async () => {
  const { addon, world } = setup({ tuning: { batchCheckMs: 30, movePollMs: 1 } });
  world.add({ folderId: INBOX, headerMessageId: "one@x" });
  world.add({ folderId: INBOX, headerMessageId: "two@x" });
  world.faults.ignoreBulkRead = true;
  const res = await call(addon, "change", change(["one@x", "two@x"], { move_to: FILED, set_read: true }));
  assert.equal(res.result.verified, false);
  assert.equal(res.result.read_state_preserved, null);
  assert.equal(world.counters.move, 1);
  assert.equal(world.counters.update, 1);
});

test("W-23 restore checks refreshed folders and re-resolves a stale flag reference without repeating the move", async () => {
  const { addon, world } = setup({ tuning: { batchCheckMs: 1500, movePollMs: 1 } });
  const id = "restored@x";
  const message = world.add({ folderId: TRASH, headerMessageId: id, read: false });
  const helper = world.messenger.chamberlainMail;
  const lookupCopies = helper.lookupCopies;
  const sync = helper.syncFolders;
  const setRead = helper.setReadFlags;
  const refreshed = [];
  let queuedFlags;
  const attemptedIds = [];

  world.messenger.messages.query = () => assert.fail("restore must use exact native copy lookups");
  helper.lookupCopies = async (...args) => {
    if (args[2] === INBOX && !refreshed.length) return { messages: [], complete: true };
    return lookupCopies(...args);
  };
  helper.syncFolders = async (...args) => {
    refreshed.push([...args[2]]);
    if (queuedFlags) {
      await setRead(...queuedFlags);
      queuedFlags = undefined;
    }
    return sync(...args);
  };
  helper.setReadFlags = async (...args) => {
    attemptedIds.push(args[3][0].id);
    if (attemptedIds.length === 1) {
      message.id = world.nextId++;
      throw new Error("chamberlain:changed:");
    }
    queuedFlags = args;
  };

  const result = await call(addon, "change", {
    ...BASE, items: [group(TRASH, id)], move_to: INBOX, trash: false, set_read: true,
  });
  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(result.result.verified, true);
  assert.equal(world.copies(id, TRASH), 0);
  assert.equal(world.copies(id, INBOX), 1);
  assert.equal(message.read, true);
  assert.equal(world.counters.move, 1);
  assert.equal(world.counters.update, 1);
  assert.notEqual(attemptedIds[0], attemptedIds[1]);
  assert.deepEqual(refreshed, [[TRASH, INBOX], [INBOX], [INBOX]]);
});

test("W-23 restore refreshes again when the first provider callback precedes arrival", async () => {
  const { addon, world } = setup({ tuning: { batchCheckMs: 1500, movePollMs: 1, moveRefreshMs: 1 } });
  const id = "delayed-restore@x";
  const message = world.add({ folderId: TRASH, headerMessageId: id, read: false });
  const helper = world.messenger.chamberlainMail;
  const lookup = helper.lookupCopies;
  const sync = helper.syncFolders;
  let arrivalRefreshes = 0;
  helper.lookupCopies = (account, mailbox, folder, ids) => {
    if (folder === INBOX && arrivalRefreshes < 2) return { messages: [], complete: true };
    return lookup(account, mailbox, folder, ids);
  };
  helper.syncFolders = (...args) => {
    if (args[2].includes(TRASH)) arrivalRefreshes++;
    return sync(...args);
  };

  const result = await call(addon, "change", {
    ...BASE, items: [group(TRASH, id)], move_to: INBOX, trash: false, set_read: true,
  });
  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(result.result.verified, true);
  assert.equal(arrivalRefreshes, 2);
  assert.equal(world.copies(id, TRASH), 0);
  assert.equal(world.copies(id, INBOX), 1);
  assert.equal(message.read, true);
  assert.equal(world.counters.move, 1);
  assert.equal(world.counters.update, 1);
});

test("W-24 mark_read reports every message and can mark unread", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "a@x", read: false });
  world.add({ folderId: INBOX, headerMessageId: "b@x", read: true });
  const res = await call(addon, "change", change(["a@x", "b@x", "gone@x"], { set_read: true }));
  assert.deepEqual(res.result.items.map((i) => [i.ok, i.was_read ?? null, i.code]), [[true, false, ""], [true, true, ""], [false, null, "not_found"]]);
  const back = await call(addon, "change", change(["a@x"], { set_read: false }));
  assert.equal(back.result.items[0].ok, true);
  assert.equal(world.find("a@x").read, false);
});

test("W-24 bulk flag changes isolate a refused folder and wait for a delayed folder", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "delayed@x", read: false });
  world.add({ folderId: INBOX, headerMessageId: "second@x", read: false });
  world.add({ folderId: FILED, headerMessageId: "refused@x", read: false });
  const mail = world.messenger.chamberlainMail;
  const changeFlags = mail.setReadFlags;
  const updateFolders = mail.syncFolders;
  const attempts = [];
  const refreshed = [];
  let delayed;
  mail.setReadFlags = async (...args) => {
    attempts.push([args[2], args[3].length]);
    if (args[2] === FILED) throw new Error("chamberlain:changed:");
    delayed = args;
  };
  mail.syncFolders = async (account, mailbox, folders) => {
    refreshed.push([...folders]);
    await changeFlags(...delayed);
    delayed = undefined;
    return updateFolders(account, mailbox, folders);
  };

  const result = await call(addon, "change", {
    ...BASE, items: [group(INBOX, "delayed@x", "second@x"), group(FILED, "refused@x")],
    trash: false, set_read: true,
  });
  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(result.result.verified, true);
  assert.deepEqual(result.result.items.map((item) => [item.ok, item.code]), [[true, ""], [true, ""], [false, "changed"]]);
  assert.deepEqual(attempts, [[INBOX, 2], [FILED, 1]]);
  assert.deepEqual(refreshed, [[INBOX]]);
  assert.equal(world.find("refused@x").read, false);
});

test("P-6 the same exact lookup finds single and batch IDs across dates", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "current@x", date: 120_000 });
  world.add({ folderId: INBOX, headerMessageId: "another@x", date: 130_000 });
  world.add({ folderId: INBOX, headerMessageId: "current@x", date: 1_000 });
  world.messenger.messages.query = () => assert.fail("ID lookups must not scan through WebExtension search");
  const result = await call(addon, "copies", copies([
    lookup(INBOX, "current@x"),
    { ...lookup(INBOX, "current@x", "another@x"), since_ms: 100_000, until_ms: 150_000 },
  ]));
  assert.equal(result.ok, true, JSON.stringify(result.error));
  const [single, bounded] = result.result.scopes;
  assert.equal(single.copies.length, 2);
  assert.equal(single.complete, true);
  assert.deepEqual(bounded.copies.map((copy) => copy.header.date_ms), [120_000, 130_000, 1_000]);
  assert.equal(bounded.complete, false);
});

test("P-10 an incomplete native lookup cannot prove absence or issue a move", async () => {
  const { addon, world } = setup();
  world.messenger.chamberlainMail.lookupCopies = async () => ({ messages: [], complete: false });
  const facts = await call(addon, "copies", copies([lookup(INBOX, "late@x")]));
  assert.equal(facts.result.scopes[0].complete, false);
  const result = await call(addon, "change", change(["late@x"], { move_to: TRASH, trash: true }));
  assert.equal(result.error.code, "ambiguous");
  assert.equal(result.error.mutation, "none");
  assert.equal(world.counters.move, 0);
});

test("P-17 a write that stalls past its deadline reports possible, keeps writes busy, and reads still answer", async () => {
  const { addon, world } = setup({ writeMs: 40 });
  world.add({ folderId: INBOX, headerMessageId: "slow@x" });
  world.faults.slow = 1;
  const id = requestId();
  addon.deliver({ t: "request", v: 3, id, cmd: "change", args: change(["slow@x"], { move_to: FILED }) });
  const res = await untilResponse(addon, id);
  assert.equal(res.error.code, "timeout");
  assert.equal(res.error.mutation, "possible");
  const status = await call(addon, "status", {});
  assert.equal(status.result.write_busy, true);
  const other = await call(addon, "change", change(["slow@x"], { set_read: true }));
  assert.equal(other.error.code, "busy");
  assert.equal(other.error.mutation, "none");
  world.release();
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(world.counters.move, 1);
  assert.equal(world.copies("slow@x", FILED), 1);
});

test("P-10 a deadline that passes before the mutation proves none was issued and cancels it", async () => {
  const { addon, world } = setup({ writeMs: 30 });
  world.add({ folderId: INBOX, headerMessageId: "late@x" });
  const mail = world.messenger.chamberlainMail;
  const real = mail.lookupCopies;
  mail.lookupCopies = async (...args) => {
    await new Promise((r) => setTimeout(r, 80));
    return real(...args);
  };
  const res = await call(addon, "change", change(["late@x"], { move_to: FILED }));
  assert.equal(res.error.code, "timeout");
  assert.equal(res.error.mutation, "none");
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(world.counters.move, 0, "the cancelled task must never issue the move");
});

test("W-3 attachments travel as chunks and are checked before anything is composed", async () => {
  const { addon, world } = setup();
  const bytes = Uint8Array.from({ length: 250_000 }, (_, i) => i % 256);
  const spec = { key: "k1", name: "café.bin", size: bytes.length, sha256: sha(bytes), content_type: "application/octet-stream", chunks: 2 };
  const piece = (from, to, index) => {
    const part = bytes.subarray(from, to);
    return { name: "k1", index, data: Buffer.from(part).toString("base64"), sha256: sha(part) };
  };
  const chunks = [piece(0, 196608, 0), piece(196608, bytes.length, 1)];
  const res = await call(addon, "compose", compose({ attachments: [spec] }), { chunks });
  assert.equal(res.ok, true, JSON.stringify(res.error));
  const sent = world.find("<tok-1@chamberlain.local>", SENT);
  assert.equal(sent.attachments[0].bytes.length, bytes.length);
  assert.equal(world.counters.send, 1);
});

test("W-3 a chunk whose hash is wrong aborts the request with nothing composed", async () => {
  const { addon, world } = setup();
  const spec = { key: "k1", name: "a.bin", size: 3, sha256: sha(Buffer.from("abc")), content_type: "x/y", chunks: 1 };
  const res = await call(addon, "compose", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 0, data: Buffer.from("abc").toString("base64"), sha256: "0".repeat(64) }] });
  assert.equal(res.error.code, "hash_mismatch");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.composers.size, 0);
  assert.equal(world.counters.send, 0);
});

test("W-3 a whole-file hash that does not match the announced one is refused", async () => {
  const { addon, world } = setup();
  const spec = { key: "k1", name: "a.bin", size: 3, sha256: "1".repeat(64), content_type: "x/y", chunks: 1 };
  const data = Buffer.from("abc");
  const res = await call(addon, "compose", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 0, data: data.toString("base64"), sha256: sha(data) }] });
  assert.equal(res.error.code, "hash_mismatch");
  assert.equal(world.counters.send, 0);
});

test("B-8 chunks out of order are refused", async () => {
  const { addon } = setup();
  const spec = { key: "k1", name: "a.bin", size: 6, sha256: sha(Buffer.from("abcdef")), content_type: "x/y", chunks: 2 };
  const d = Buffer.from("def");
  const res = await call(addon, "compose", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 1, data: d.toString("base64"), sha256: sha(d) }] });
  assert.equal(res.error.code, "bad_request");
});

test("P-15 send files the Sent copy with the bound folder and carries the token header", async () => {
  const { addon, world } = setup();
  const res = await call(addon, "compose", compose({ high_importance: true }));
  assert.equal(res.ok, true);
  const sent = world.find("<tok-1@chamberlain.local>", SENT);
  assert.equal(sent.priority, "high");
  assert.ok(sent.headers["x-chamberlain-write-token"]);
  assert.equal(world.composers.size, 0, "a submitted composer is gone");
});

test("R8 a prompt the helper answered during the send comes back with the result", async () => {
  const { addon, world } = setup();
  world.faults.fccPrompt = true;
  const res = await call(addon, "compose", compose());
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(res.result.dialogs.length, 1);
  assert.match(res.result.dialogs[0], /copy was not placed/);
});

test("R8 a manual edit of the managed composer invalidates the send", async () => {
  const { addon, world } = setup();
  const real = world.messenger.compose.getComposeDetails;
  world.messenger.compose.getComposeDetails = async (id) => ({ ...(await real(id)), plainTextBody: "edited by the user" });
  const res = await call(addon, "compose", compose());
  assert.equal(res.error.code, "changed");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.counters.send, 0);
  assert.equal(world.composers.size, 0, "the failed composer is closed");
});

test("R8 the bound Sent folder is enforced on the composer", async () => {
  const { addon, world } = setup();
  const real = world.messenger.compose.getComposeDetails;
  world.messenger.compose.getComposeDetails = async (id) => ({ ...(await real(id)), overrideDefaultFccFolder: { id: ACCOUNT + "://Wrong" } });
  const res = await call(addon, "compose", compose());
  assert.equal(res.error.code, "changed");
  assert.equal(world.counters.send, 0);
});

test("R8 an identity whose Drafts setting is another folder still sends; a draft save names the folder", async () => {
  const { addon, world } = setup();
  world.faults.identityDrafts = "imap://owner@example/Drafts";
  const sent = await call(addon, "compose", compose());
  assert.equal(sent.ok, true, JSON.stringify(sent.error));
  const draft = await call(addon, "compose", compose({ mode: "draft_save", token: "<tok-2@chamberlain.local>" }));
  assert.equal(draft.error.code, "changed");
  assert.match(draft.error.message, /saves this identity's drafts to imap:\/\/owner@example\/Drafts/);
  assert.equal(draft.error.mutation, "none");
  assert.equal(world.copies("<tok-2@chamberlain.local>", DRAFTS), 0);
});

test("R8 a reply restores the unread flag Thunderbird cleared on the original", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "orig@x", read: false, subject: "Question" });
  const res = await call(addon, "compose", compose({ mode: "draft_save", subject: "Re: Question", reply_to: ref(INBOX, "orig@x") }));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(world.find("orig@x").read, false);
  const draft = world.find("<tok-1@chamberlain.local>", DRAFTS);
  assert.ok(draft.headers["in-reply-to"]);
});

test("P-15 a stale callback from saveMessage is ignored: the draft is re-read by id", async () => {
  const { addon, world } = setup();
  world.faults.staleCallback = true;
  const res = await call(addon, "compose", compose({ mode: "draft_save" }));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(res.result.header.read, true, "the flag comes from a fresh read, not the callback");
  assert.notEqual(res.result.header.internet_message_id, "stale-callback");
});

test("P-9 draft_save returns the stored message and its raw hash", async () => {
  const { addon, world } = setup();
  const res = await call(addon, "compose", compose({ mode: "draft_save" }));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.match(res.result.raw_sha256, /^[0-9a-f]{64}$/);
  assert.equal(res.result.header.folder_id, DRAFTS);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 1);
});

test("P-9 a placeholder draft without a Message-ID is followed to its synced copy", async () => {
  const { addon, world } = setup();
  world.faults.placeholderDraft = true;
  const res = await call(addon, "compose", compose({ mode: "draft_save" }));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.ok(res.result.header.internet_message_id, "the handle needs a Message-ID");
  const stored = world.find(res.result.header.internet_message_id, DRAFTS);
  assert.equal(res.result.header.session_id, stored.id, "the synced copy, not the placeholder");
});

test("P-9 draft_update checks the old raw hash before opening anything", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "compose", compose({ mode: "draft_save" }));
  const draft = ref(DRAFTS, saved.result.header.internet_message_id);
  const stale = await call(addon, "compose", compose({ mode: "draft_update", draft, expected_raw_sha256: "0".repeat(64), token: "<tok-2@chamberlain.local>" }));
  assert.equal(stale.error.code, "changed");
  assert.equal(stale.error.mutation, "none");
  assert.equal(world.composers.size, 0);
  const good = await call(addon, "compose", compose({ mode: "draft_update", draft, expected_raw_sha256: saved.result.raw_sha256, body: "Second wording.", token: "<tok-2@chamberlain.local>" }));
  assert.equal(good.ok, true, JSON.stringify(good.error));
  assert.equal(good.result.old_copies_remaining, 0);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 0);
  assert.equal(world.copies("<tok-2@chamberlain.local>", DRAFTS), 1);
  assert.notEqual(good.result.raw_sha256, saved.result.raw_sha256);
});

test("R8 draft_discard closes the composer and never deletes saved mail", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "compose", compose({ mode: "draft_save", display_only: true }));
  assert.equal(world.composers.size, 1, "display_only leaves the composer open");
  const draft = ref(DRAFTS, saved.result.header.internet_message_id);
  const res = await call(addon, "draft_discard", { ...BASE, draft, expected_raw_sha256: saved.result.raw_sha256 });
  assert.equal(res.result.closed, 1);
  assert.equal(world.composers.size, 0);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 1);
});

test("R8 draft_discard refuses a composer the user has edited", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "compose", compose({ mode: "draft_save", display_only: true }));
  [...world.composers.values()][0].modified = true;
  const res = await call(addon, "draft_discard", { ...BASE, draft: ref(DRAFTS, saved.result.header.internet_message_id), expected_raw_sha256: saved.result.raw_sha256 });
  assert.equal(res.error.code, "changed");
  assert.equal(world.composers.size, 1);
});

test("R1 copies returns normalized body and envelope facts for exact reconciliation", async () => {
  const { addon, world } = setup();
  await call(addon, "compose", compose());
  const where = [SENT, DRAFTS, INBOX, null].map((folder) => subjectScope(folder, "Contract send", {
    inspect_draft: folder === DRAFTS, include_attachment_hashes: true,
  }));
  const res = await call(addon, "copies", copies(where, true));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  const facts = res.result.scopes.flatMap((scope) => scope.copies);
  assert.deepEqual(facts.map((copy) => copy.header.folder_id).sort(), [INBOX, SENT].sort());
  for (const copy of facts) {
    assert.equal(copy.subject, "Contract send");
    assert.equal(copy.body_sha256, sha("Body."));
    assert.equal(copy.token, "<tok-1@chamberlain.local>");
    assert.deepEqual(copy.to, [OWNER]);
    assert.deepEqual(copy.attachment_sha256, []);
  }
  assert.equal(res.result.refresh_verified, true);
  assert.ok(res.result.scopes.every((scope) => scope.complete));
  assert.equal(world.counters.send, 1);
});

test("R2 copies sees a copy waiting in the Outbox and is read-only", async () => {
  const { addon, world } = setup();
  world.faults.noSentCopy = true;
  world.faults.queueOnly = true;
  await call(addon, "compose", compose({ to: ["x@example.org"] }));
  const before = world.messages.length;
  const where = [SENT, DRAFTS, null].map((folder) => subjectScope(folder, "Contract send"));
  const res = await call(addon, "copies", copies(where, true));
  const facts = res.result.scopes.flatMap((scope) => scope.copies);
  assert.deepEqual(facts.map((copy) => copy.header.folder_id), ["local://Outbox"]);
  assert.equal(world.messages.length, before);
});

test("W-27 copies preserves the body match and changed Cc used by the different duplicate rules", async () => {
  const { addon, world } = setup();
  const bytes = new Uint8Array([1, 2, 3]);
  world.add({ folderId: SENT, headerMessageId: "near@x", author: OWNER,
    subject: "Contract send", body: "  Body.\r\n", ccList: ["different@example.org"],
    attachments: [{ name: "one.bin", bytes }] });
  const where = [subjectScope(SENT, "Contract send"), subjectScope(SENT, "Contract send", { include_attachment_hashes: true })];
  const res = await call(addon, "copies", copies(where));
  const [loose, exact] = res.result.scopes.map(({ copies: [copy] }) => copy);
  assert.equal(loose.body_sha256, sha("Body."));
  assert.equal(exact.body_sha256, loose.body_sha256);
  assert.deepEqual(exact.cc, ["different@example.org"]);
  assert.deepEqual(loose.attachment_sha256, []);
  assert.deepEqual(exact.attachment_sha256, [sha(bytes)]);
});

test("W-27 subject scan reports incomplete coverage and does not fetch attachment bytes by default", async () => {
  const { addon, world } = setup();
  for (let i = 0; i < 201; i++) {
    world.add({ folderId: SENT, headerMessageId: "related-" + i + "@x", author: OWNER,
      subject: "", attachments: [{ name: "one.bin", bytes: new Uint8Array(1) }] });
  }
  world.messenger.messages.getAttachmentFile = () => { throw new Error("unexpected download"); };
  const res = await call(addon, "copies", copies([subjectScope(SENT, "")]));
  assert.equal(res.result.scopes[0].complete, false);
  assert.equal(res.result.scopes[0].copies.length, 200);
  assert.ok(res.result.scopes[0].copies.every((copy) => copy.header.has_attachments));
});

test("P-12 a refresh that is not verified is reported as such", async () => {
  const { addon, world } = setup();
  world.faults.stale = true;
  world.add({ folderId: INBOX, headerMessageId: "mv@x" });
  const res = await call(addon, "copies", copies([lookup(INBOX, "mv@x"), lookup(FILED, "mv@x")], true));
  assert.equal(res.result.refresh_verified, false);
  assert.equal(res.result.scopes[0].copies.length, 1);
});

test("P-11 copies reads exactly the two folders and never moves", async () => {
  const { addon, world } = setup();
  world.add({ folderId: FILED, headerMessageId: "mv@x" });
  const res = await call(addon, "copies", copies([lookup(INBOX, "mv@x"), lookup(FILED, "mv@x")], true));
  assert.equal(res.result.scopes[0].copies.length, 0);
  assert.equal(res.result.scopes[1].copies.length, 1);
  assert.equal(res.result.refresh_verified, true);
  assert.equal(world.counters.move, 0);
});

test("P-11 copies reports flags and folder scan completeness without changing the flags", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "a@x", read: true });
  const res = await call(addon, "copies", copies([lookup(INBOX, "a@x", "b@x")], true));
  assert.equal(res.result.scopes[0].complete, true);
  assert.deepEqual(res.result.scopes[0].copies.map(({ header }) => [header.internet_message_id, header.read]), [["a@x", true]]);
  assert.equal(world.counters.update, 0);
});

test("a contact is read by its book and id, and only inside that book", async () => {
  const { addon, world } = setup();
  world.books.push({ book: "book1", id: "c9", properties: { DisplayName: "Ada Reader", PrimaryEmail: "ada@example.org" } });
  const got = await call(addon, "contact_read", { address_book_id: "book1", contact_id: "c9" });
  assert.deepEqual(got.result.contact, { id: "c9", book_id: "book1", name: "Ada Reader", emails: ["ada@example.org"] });
  const wrongBook = await call(addon, "contact_read", { address_book_id: "book2", contact_id: "c9" });
  assert.equal(wrongBook.error.code, "not_found");
  const missing = await call(addon, "contact_read", { address_book_id: "book1", contact_id: "nope" });
  assert.equal(missing.error.code, "not_found");
});

test("contacts are matched by name and address and paged", async () => {
  const { addon, world } = setup();
  for (let i = 0; i < 3; i++) world.books.push({ book: "book1", id: "c" + i, properties: { DisplayName: "Paging Marker " + i, PrimaryEmail: "p" + i + "@example.org" } });
  const first = await call(addon, "contacts", { address_book_ids: ["book1"], text: "paging marker", limit: 2 });
  assert.equal(first.result.contacts.length, 2);
  assert.equal(first.result.more, true);
  const second = await call(addon, "contacts", { address_book_ids: ["book1"], text: "paging marker", limit: 2, skip: 2 });
  assert.equal(second.result.contacts.length, 1);
  assert.equal(second.result.more, false);
});

test("Message-ID and subject matching follow the shared mail vectors", () => {
  const { addon } = setup();
  const vectors = JSON.parse(readFileSync(new URL("./mail-vectors.json", import.meta.url), "utf8"));
  for (const [input, want] of vectors.ids) assert.equal(addon.eval(`bare(${JSON.stringify(input)})`), want);
  for (const [input, want] of vectors.subjects) assert.equal(addon.eval(`normaliseSubject(${JSON.stringify(input)})`), want);
});
