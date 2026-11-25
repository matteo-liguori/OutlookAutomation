// Add-on tests against a scripted Thunderbird. Each name carries the rule it enforces
// (bridge contract B-n, port rules P-n, write rules W-n).
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { ACCOUNT, createWorld, OWNER } from "./fake_thunderbird.mjs";
import { BASE, call, requestId, startAddon, untilResponse } from "./loader.mjs";

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
const compose = (extra = {}) => ({
  ...BASE,
  to: [OWNER],
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

test("B-2 malformed or extra arguments are refused before anything runs", async () => {
  const { addon } = setup();
  const extra = await call(addon, "folders", { ...BASE, shell: "calc" });
  assert.equal(extra.error.code, "bad_request");
  const missing = await call(addon, "move", { ...BASE });
  assert.equal(missing.error.code, "bad_request");
  assert.equal(missing.error.mutation, "none");
});

test("B-1 a message with another protocol version gets a refusal", async () => {
  const { addon } = setup();
  addon.deliver({ t: "request", v: 3, id: "req99999999", cmd: "status", args: {} });
  assert.equal(addon.posted.find((f) => f.t === "refusal")?.code, "version_mismatch");
});

test("B-5 a second in-flight request with the same ID is refused", async () => {
  const { addon, world } = setup();
  const m = world.add({ folderId: INBOX, headerMessageId: "a@x" });
  world.faults.slow = 1;
  const id = requestId();
  addon.deliver({ t: "request", v: 2, id, cmd: "move", args: { ...BASE, ref: ref(INBOX, "a@x"), destination_folder_id: FILED } });
  await new Promise((r) => setTimeout(r, 30));
  addon.deliver({ t: "request", v: 2, id, cmd: "status", args: {} });
  const second = addon.posted.filter((f) => f.id === id && f.t === "response");
  assert.equal(second[0].error.code, "bad_request");
  world.release();
  await untilResponse(addon, id);
  assert.equal(world.counters.move, 1);
  assert.ok(m);
});

test("B-9 capabilities list what Thunderbird offers and drop what the helper lacks", async () => {
  const world = createWorld();
  delete world.messenger.chamberlainMail;
  const addon = startAddon(world);
  await new Promise((r) => setTimeout(r, 10));
  const hello = addon.posted.find((f) => f.t === "addon_hello");
  assert.ok(!hello.capabilities.includes("send"));
  assert.ok(!hello.capabilities.includes("refresh"));
  assert.ok(hello.capabilities.includes("read"));
});

test("B-6 query pages by a keyset cursor that survives new mail", async () => {
  const { addon, world } = setup();
  for (let i = 0; i < 5; i++) world.add({ folderId: INBOX, headerMessageId: "m" + i + "@x", subject: "paging" });
  const first = await call(addon, "query", { ...BASE, folder_id: INBOX, subject: "paging", subject_contains: true, limit: 2 });
  assert.deepEqual(first.result.messages.map((m) => m.internet_message_id), ["m4@x", "m3@x"]);
  assert.equal(first.result.more, true);
  world.add({ folderId: INBOX, headerMessageId: "late@x", subject: "paging" });
  const second = await call(addon, "query", { ...BASE, folder_id: INBOX, subject: "paging", subject_contains: true, limit: 2, after: first.result.next });
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
  const resolved = await call(addon, "resolve", { ...BASE, folder_id: INBOX, internet_message_id: "r@x" });
  assert.equal(resolved.result.messages[0].has_attachments, true);
  const att = await call(addon, "attachment", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1_000_000 });
  assert.equal(att.ok, true);
  assert.equal(m.read, false);
  assert.equal(world.counters.update, 0);
});

test("P-8 attachment downloads arrive as hashed chunks across several frames", async () => {
  const { addon, world } = setup();
  const bytes = Uint8Array.from({ length: 400_000 }, (_, i) => (i * 7) % 256);
  world.add({ folderId: INBOX, headerMessageId: "r@x", attachments: [{ name: "a.bin", contentType: "x/y", bytes }] });
  const id = requestId();
  const res = await call(addon, "attachment", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1_000_000 }, { id });
  const chunks = addon.posted.filter((f) => f.t === "chunk" && f.id === id);
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
  const res = await call(addon, "attachment", { ...BASE, folder_id: INBOX, internet_message_id: "r@x", part_name: "1.2", max_bytes: 1000 });
  assert.equal(res.error.code, "too_large");
});

test("P-6 messages are found by stable identity after session ids are renumbered", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "stable@x" });
  const before = await call(addon, "resolve", { ...BASE, folder_id: INBOX, internet_message_id: "stable@x" });
  world.restart();
  const after = await call(addon, "resolve", { ...BASE, folder_id: INBOX, internet_message_id: "stable@x" });
  assert.notEqual(before.result.messages[0].session_id, after.result.messages[0].session_id);
  assert.equal(after.result.messages.length, 1);
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
  const own = await call(addon, "query", { ...BASE, folder_id: outbox, own_only: true });
  assert.equal(own.ok, true);
  assert.deepEqual(own.result.messages.map((m) => [m.internet_message_id, m.has_attachments]), [["own@x", true]]);
});

test("P-14 move leaves one copy, none at the source, and the flag as it was", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x", read: false });
  const res = await call(addon, "move", { ...BASE, ref: ref(INBOX, "mv@x"), destination_folder_id: FILED, mark_read: false });
  assert.equal(res.ok, true);
  assert.equal(res.result.read_state_preserved, true);
  assert.equal(res.result.message.read, false);
  assert.equal(world.copies("mv@x", INBOX), 0);
  assert.equal(world.copies("mv@x", FILED), 1);
});

test("W-23 move marks read only when asked", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x", read: false });
  const res = await call(addon, "move", { ...BASE, ref: ref(INBOX, "mv@x"), destination_folder_id: FILED, mark_read: true });
  assert.equal(res.result.marked_read, true);
  assert.equal(res.result.message.read, true);
  assert.equal(res.result.original_read, false);
});

test("P-14 a move into a folder that already holds the message is ambiguous and issues nothing", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "mv@x" });
  world.add({ folderId: FILED, headerMessageId: "mv@x" });
  const res = await call(addon, "move", { ...BASE, ref: ref(INBOX, "mv@x"), destination_folder_id: FILED });
  assert.equal(res.error.code, "ambiguous");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.counters.move, 0);
});

test("P-6 two copies at the source make a move or trash ambiguous (dropped old behaviour)", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "two@x" });
  world.add({ folderId: INBOX, headerMessageId: "two@x" });
  const res = await call(addon, "trash", { ...BASE, ref: ref(INBOX, "two@x"), destination_folder_id: TRASH });
  assert.equal(res.error.code, "ambiguous");
  assert.equal(world.counters.move, 0);
});

test("P-14 a move that leaves a copy behind is not verified and says a change may have happened", async () => {
  const { addon, world } = setup({ tuning: { moveAttempts: 3 } });
  world.add({ folderId: INBOX, headerMessageId: "dup@x" });
  world.faults.duplicate = 1;
  const res = await call(addon, "move", { ...BASE, ref: ref(INBOX, "dup@x"), destination_folder_id: FILED });
  assert.equal(res.error.code, "not_verified");
  assert.equal(res.error.mutation, "possible");
});

test("R7 trash that merges into an existing recovery copy is applied and says so", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "gm@x", read: true });
  world.add({ folderId: TRASH, headerMessageId: "gm@x", read: false });
  world.faults.merge = true;
  const res = await call(addon, "trash", { ...BASE, ref: ref(INBOX, "gm@x"), destination_folder_id: TRASH });
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
  const res = await call(addon, "move_many", {
    ...BASE, folder_id: INBOX, destination_folder_id: FILED,
    internet_message_ids: [...ids, "duplicate@x"],
  });
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
  const res = await call(addon, "move_many", {
    ...BASE, folder_id: INBOX, destination_folder_id: FILED,
    internet_message_ids: ["one@x", "two@x"], mark_read: true,
  });
  assert.equal(res.result.verified, false);
  assert.equal(res.result.read_state_preserved, null);
  assert.equal(world.counters.move, 1);
  assert.equal(world.counters.update, 1);
});

test("W-24 mark_read reports every message and can mark unread", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "a@x", read: false });
  world.add({ folderId: INBOX, headerMessageId: "b@x", read: true });
  const res = await call(addon, "mark_read", { ...BASE, refs: [ref(INBOX, "a@x"), ref(INBOX, "b@x"), ref(INBOX, "gone@x")], read: true });
  assert.deepEqual(res.result.items.map((i) => [i.ok, i.was_read ?? null, i.code]), [[true, false, ""], [true, true, ""], [false, null, "not_found"]]);
  const back = await call(addon, "mark_read", { ...BASE, refs: [ref(INBOX, "a@x")], read: false });
  assert.equal(back.result.items[0].ok, true);
  assert.equal(world.find("a@x").read, false);
});

test("P-17 a write that stalls past its deadline reports possible, keeps writes busy, and reads still answer", async () => {
  const { addon, world } = setup({ writeMs: 40 });
  world.add({ folderId: INBOX, headerMessageId: "slow@x" });
  world.faults.slow = 1;
  const id = requestId();
  addon.deliver({ t: "request", v: 2, id, cmd: "move", args: { ...BASE, ref: ref(INBOX, "slow@x"), destination_folder_id: FILED } });
  const res = await untilResponse(addon, id);
  assert.equal(res.error.code, "timeout");
  assert.equal(res.error.mutation, "possible");
  const status = await call(addon, "status", {});
  assert.equal(status.result.write_busy, true);
  const other = await call(addon, "mark_read", { ...BASE, refs: [ref(INBOX, "slow@x")], read: true });
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
  const real = world.messenger.messages.query;
  world.messenger.messages.query = async (q) => {
    await new Promise((r) => setTimeout(r, 80));
    return real(q);
  };
  const res = await call(addon, "move", { ...BASE, ref: ref(INBOX, "late@x"), destination_folder_id: FILED });
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
  const res = await call(addon, "send", compose({ attachments: [spec] }), { chunks });
  assert.equal(res.ok, true, JSON.stringify(res.error));
  const sent = world.find("<tok-1@chamberlain.local>", SENT);
  assert.equal(sent.attachments[0].bytes.length, bytes.length);
  assert.equal(world.counters.send, 1);
});

test("W-3 a chunk whose hash is wrong aborts the request with nothing composed", async () => {
  const { addon, world } = setup();
  const spec = { key: "k1", name: "a.bin", size: 3, sha256: sha(Buffer.from("abc")), content_type: "x/y", chunks: 1 };
  const res = await call(addon, "send", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 0, data: Buffer.from("abc").toString("base64"), sha256: "0".repeat(64) }] });
  assert.equal(res.error.code, "hash_mismatch");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.composers.size, 0);
  assert.equal(world.counters.send, 0);
});

test("W-3 a whole-file hash that does not match the announced one is refused", async () => {
  const { addon, world } = setup();
  const spec = { key: "k1", name: "a.bin", size: 3, sha256: "1".repeat(64), content_type: "x/y", chunks: 1 };
  const data = Buffer.from("abc");
  const res = await call(addon, "send", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 0, data: data.toString("base64"), sha256: sha(data) }] });
  assert.equal(res.error.code, "hash_mismatch");
  assert.equal(world.counters.send, 0);
});

test("B-8 chunks out of order are refused", async () => {
  const { addon } = setup();
  const spec = { key: "k1", name: "a.bin", size: 6, sha256: sha(Buffer.from("abcdef")), content_type: "x/y", chunks: 2 };
  const d = Buffer.from("def");
  const res = await call(addon, "send", compose({ attachments: [spec] }), { chunks: [{ name: "k1", index: 1, data: d.toString("base64"), sha256: sha(d) }] });
  assert.equal(res.error.code, "bad_request");
});

test("P-15 send files the Sent copy with the bound folder and carries the token header", async () => {
  const { addon, world } = setup();
  const res = await call(addon, "send", compose({ high_importance: true }));
  assert.equal(res.ok, true);
  const sent = world.find("<tok-1@chamberlain.local>", SENT);
  assert.equal(sent.priority, "high");
  assert.ok(sent.headers["x-chamberlain-write-token"]);
  assert.equal(world.composers.size, 0, "a submitted composer is gone");
});

test("R8 a prompt the helper answered during the send comes back with the result", async () => {
  const { addon, world } = setup();
  world.faults.fccPrompt = true;
  const res = await call(addon, "send", compose());
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(res.result.dialogs.length, 1);
  assert.match(res.result.dialogs[0], /copy was not placed/);
});

test("R8 a manual edit of the managed composer invalidates the send", async () => {
  const { addon, world } = setup();
  const real = world.messenger.compose.getComposeDetails;
  world.messenger.compose.getComposeDetails = async (id) => ({ ...(await real(id)), plainTextBody: "edited by the user" });
  const res = await call(addon, "send", compose());
  assert.equal(res.error.code, "changed");
  assert.equal(res.error.mutation, "none");
  assert.equal(world.counters.send, 0);
  assert.equal(world.composers.size, 0, "the failed composer is closed");
});

test("R8 the bound Sent folder is enforced on the composer", async () => {
  const { addon, world } = setup();
  const real = world.messenger.compose.getComposeDetails;
  world.messenger.compose.getComposeDetails = async (id) => ({ ...(await real(id)), overrideDefaultFccFolder: { id: ACCOUNT + "://Wrong" } });
  const res = await call(addon, "send", compose());
  assert.equal(res.error.code, "changed");
  assert.equal(world.counters.send, 0);
});

test("R8 an identity whose Drafts setting is another folder still sends; a draft save names the folder", async () => {
  const { addon, world } = setup();
  world.faults.identityDrafts = "imap://owner@example/Drafts";
  const sent = await call(addon, "send", compose());
  assert.equal(sent.ok, true, JSON.stringify(sent.error));
  const draft = await call(addon, "draft_save", compose({ token: "<tok-2@chamberlain.local>" }));
  assert.equal(draft.error.code, "changed");
  assert.match(draft.error.message, /saves this identity's drafts to imap:\/\/owner@example\/Drafts/);
  assert.equal(draft.error.mutation, "none");
  assert.equal(world.copies("<tok-2@chamberlain.local>", DRAFTS), 0);
});

test("R8 a reply restores the unread flag Thunderbird cleared on the original", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "orig@x", read: false, subject: "Question" });
  const res = await call(addon, "draft_save", compose({ subject: "Re: Question", reply_to: ref(INBOX, "orig@x") }));
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(world.find("orig@x").read, false);
  const draft = world.find("<tok-1@chamberlain.local>", DRAFTS);
  assert.ok(draft.headers["in-reply-to"]);
});

test("P-15 a stale callback from saveMessage is ignored: the draft is re-read by id", async () => {
  const { addon, world } = setup();
  world.faults.staleCallback = true;
  const res = await call(addon, "draft_save", compose());
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.equal(res.result.header.read, true, "the flag comes from a fresh read, not the callback");
  assert.notEqual(res.result.header.internet_message_id, "stale-callback");
});

test("P-9 draft_save returns the stored message and its raw hash", async () => {
  const { addon, world } = setup();
  const res = await call(addon, "draft_save", compose());
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.match(res.result.raw_sha256, /^[0-9a-f]{64}$/);
  assert.equal(res.result.header.folder_id, DRAFTS);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 1);
});

test("P-9 a placeholder draft without a Message-ID is followed to its synced copy", async () => {
  const { addon, world } = setup();
  world.faults.placeholderDraft = true;
  const res = await call(addon, "draft_save", compose());
  assert.equal(res.ok, true, JSON.stringify(res.error));
  assert.ok(res.result.header.internet_message_id, "the handle needs a Message-ID");
  const stored = world.find(res.result.header.internet_message_id, DRAFTS);
  assert.equal(res.result.header.session_id, stored.id, "the synced copy, not the placeholder");
});

test("P-9 draft_update checks the old raw hash before opening anything", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "draft_save", compose());
  const draft = ref(DRAFTS, saved.result.header.internet_message_id);
  const stale = await call(addon, "draft_update", compose({ draft, expected_raw_sha256: "0".repeat(64), token: "<tok-2@chamberlain.local>" }));
  assert.equal(stale.error.code, "changed");
  assert.equal(stale.error.mutation, "none");
  assert.equal(world.composers.size, 0);
  const good = await call(addon, "draft_update", compose({ draft, expected_raw_sha256: saved.result.raw_sha256, body: "Second wording.", token: "<tok-2@chamberlain.local>" }));
  assert.equal(good.ok, true, JSON.stringify(good.error));
  assert.equal(good.result.old_copies_remaining, 0);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 0);
  assert.equal(world.copies("<tok-2@chamberlain.local>", DRAFTS), 1);
  assert.notEqual(good.result.raw_sha256, saved.result.raw_sha256);
});

test("R8 draft_discard closes the composer and never deletes saved mail", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "draft_save", compose({ display_only: true }));
  assert.equal(world.composers.size, 1, "display_only leaves the composer open");
  const draft = ref(DRAFTS, saved.result.header.internet_message_id);
  const res = await call(addon, "draft_discard", { ...BASE, draft, expected_raw_sha256: saved.result.raw_sha256 });
  assert.equal(res.result.closed, 1);
  assert.equal(res.result.saved_mail_deleted, false);
  assert.equal(world.composers.size, 0);
  assert.equal(world.copies("<tok-1@chamberlain.local>", DRAFTS), 1);
});

test("R8 draft_discard refuses a composer the user has edited", async () => {
  const { addon, world } = setup();
  const saved = await call(addon, "draft_save", compose({ display_only: true }));
  [...world.composers.values()][0].modified = true;
  const res = await call(addon, "draft_discard", { ...BASE, draft: ref(DRAFTS, saved.result.header.internet_message_id), expected_raw_sha256: saved.result.raw_sha256 });
  assert.equal(res.error.code, "changed");
  assert.equal(world.composers.size, 1);
});

async function fingerprint(body) {
  const { createHash: hash } = await import("node:crypto");
  return hash("sha256").update(body.trim()).digest("hex");
}

test("R1 reconcile_send finds the Sent copy by content, not by Message-ID", async () => {
  const { addon, world } = setup();
  await call(addon, "send", compose());
  const fp = { sender: OWNER, to: [OWNER], subject: "Contract send", body_sha256: await fingerprint("Body."), token: "<tok-1@chamberlain.local>" };
  const res = await call(addon, "reconcile_send", { ...BASE, fingerprint: fp, sent_folder_id: SENT, draft_folder_id: DRAFTS, inbox_folder_id: INBOX, since_ms: 0 });
  assert.equal(res.ok, true, JSON.stringify(res.error));
  const where = res.result.matches.map((m) => [m.where, m.exact, m.token_match]).sort();
  assert.deepEqual(where, [["inbox", true, true], ["sent", true, true]]);
  assert.equal(res.result.refreshed, true);
  assert.equal(res.result.complete, true);
  assert.equal(world.counters.send, 1);
});

test("R2 reconcile_send sees a copy waiting in the Outbox and is read-only", async () => {
  const { addon, world } = setup();
  world.faults.noSentCopy = true;
  world.faults.queueOnly = true;
  await call(addon, "send", compose({ to: ["x@example.org"] }));
  const fp = { sender: OWNER, to: ["x@example.org"], subject: "Contract send", body_sha256: await fingerprint("Body."), token: "<tok-1@chamberlain.local>" };
  const before = world.messages.length;
  const res = await call(addon, "reconcile_send", { ...BASE, fingerprint: fp, sent_folder_id: SENT, draft_folder_id: DRAFTS, since_ms: 0 });
  assert.deepEqual(res.result.matches.map((m) => m.where), ["outbox"]);
  assert.equal(world.messages.length, before);
});

test("R1 reconcile_send reports nothing when nothing matches, with coverage", async () => {
  const { addon } = setup();
  const fp = { sender: OWNER, to: [OWNER], subject: "Never sent", body_sha256: await fingerprint("x"), token: "<none@x>" };
  const res = await call(addon, "reconcile_send", { ...BASE, fingerprint: fp, sent_folder_id: SENT, draft_folder_id: DRAFTS, since_ms: 0 });
  assert.deepEqual(res.result.matches, []);
  assert.equal(res.result.complete, true);
});

test("P-12 a refresh that is not verified is reported as such", async () => {
  const { addon, world } = setup();
  world.faults.stale = true;
  world.add({ folderId: INBOX, headerMessageId: "mv@x" });
  const res = await call(addon, "reconcile_move", { ...BASE, ref: ref(INBOX, "mv@x"), destination_folder_id: FILED });
  assert.equal(res.result.refreshed, true);
  assert.equal(res.result.refresh_verified, false);
  assert.equal(res.result.source.messages.length, 1);
});

test("P-11 reconcile_move reads exactly the two folders and never moves", async () => {
  const { addon, world } = setup();
  world.add({ folderId: FILED, headerMessageId: "mv@x" });
  const res = await call(addon, "reconcile_move", { ...BASE, ref: ref(INBOX, "mv@x"), destination_folder_id: FILED });
  assert.equal(res.result.source.messages.length, 0);
  assert.equal(res.result.destination.messages.length, 1);
  assert.equal(res.result.refresh_verified, true);
  assert.equal(world.counters.move, 0);
});

test("P-11 reconcile_mark_read reports copies and flags without changing them", async () => {
  const { addon, world } = setup();
  world.add({ folderId: INBOX, headerMessageId: "a@x", read: true });
  const res = await call(addon, "reconcile_mark_read", { ...BASE, refs: [ref(INBOX, "a@x"), ref(INBOX, "b@x")] });
  assert.deepEqual(res.result.items.map((i) => [i.copies, i.read ?? null]), [[1, true], [0, null]]);
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
