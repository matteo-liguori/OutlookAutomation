// The privileged helper file itself (experiment/api.js), run against stubbed Thunderbird
// modules. The bridge tests use a fake of these helpers, so only this test reads the real one.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(new URL("../experiment/api.js", import.meta.url), "utf-8");
const ACCOUNT = "account1";
const MAILBOX = "owner@example.org";
const URIS = { [`${ACCOUNT}://Drafts`]: "imap://owner/Drafts", [`${ACCOUNT}://Sent`]: "imap://owner/Sent" };

class ExtensionError extends Error {}

/** A scripted nsIMsgSend: records each call and answers through the listener as Thunderbird does. */
function scriptedSend(sends, outcome) {
  return {
    createAndSendMessage(...args) {
      const [, identity, accountKey, fields, , , mode, replaced, bodyType, body, parent, , listener] = args;
      const finish = () => {
        fields.messageId = "<generated@example.org>";
        if (mode === 0) listener.onStopSending("<generated@example.org>", outcome === "fail" ? 0x80004005 : 0, null, null);
        if (outcome !== "fail" && fields.fcc !== "nocopy://") listener.onStopCopy(0);
      };
      sends.push({ identity, accountKey, fields, mode, replaced, bodyType, body, parent, finish });
      if (outcome !== "later") setTimeout(finish, 0);
    },
  };
}

function compFields() {
  const raw = new Map();
  const attachments = [];
  return {
    raw,
    attachments,
    setRawHeader: (name, value) => raw.set(name, value),
    addAttachment: (attachment) => attachments.push(attachment),
  };
}

function load({ identityDrafts = URIS[`${ACCOUNT}://Drafts`], headers = new Map(), folderMessages = [], now = Date.now, sendOutcome = "ok", composers = [] } = {}) {
  const looked = [];
  const changes = [];
  const sends = [];
  const identity = { key: "id1", email: MAILBOX, fullName: "Owner", getUnicharAttribute: () => identityDrafts };
  const win = {
    gMsgCompose: { compFields: { fcc: URIS[`${ACCOUNT}://Sent`], fcc2: "" } },
    gCurrentIdentity: identity,
    clearTimeout() {},
    close() { this.closed = true; },
  };
  const hidden = { hidden: true };
  const modules = {
    "resource://gre/modules/ExtensionCommon.sys.mjs": { ExtensionCommon: { ExtensionAPI: class {} } },
    "resource://gre/modules/Timer.sys.mjs": { setTimeout, clearTimeout },
    "resource:///modules/MailServices.sys.mjs": {
      MailServices: {
        accounts: { getAccount: () => ({ key: ACCOUNT, identities: [identity], incomingServer: { key: "owner" } }) },
        headerParser: { makeMimeAddress: (name, email) => `${name} <${email}>` },
      },
    },
    "resource:///modules/ExtensionAccounts.sys.mjs": {
      getFolder(id) {
        looked.push(id);
        if (!URIS[id]) throw new ExtensionError(`Folder not found: ${id}`);
        return { accountKey: ACCOUNT, folder: { canFileMessages: true, URI: URIS[id], getFlag: () => false, messages: folderMessages, markMessagesRead: (messages, read) => changes.push({ messages: [...messages], read }) } };
      },
    },
    "resource://gre/modules/ExtensionUtils.sys.mjs": { ExtensionUtils: { ExtensionError } },
  };
  const observers = [];
  const context = vm.createContext({
    Date: class extends Date { static now() { return now(); } },
    ChromeUtils: { importESModule: (url) => modules[url], generateQI: () => () => undefined },
    Cc: {
      "@mozilla.org/messengercompose/composefields;1": { createInstance: () => compFields() },
      "@mozilla.org/messengercompose/send;1": { createInstance: () => scriptedSend(sends, sendOutcome) },
    },
    Ci: {
      nsMsgFolderFlags: { Queue: 1, Virtual: 2, Drafts: 4 },
      nsMsgMessageFlags: { Attachment: 1, IMAPDeleted: 2, Expunged: 4 },
      nsIMsgSend: { nsMsgDeliverNow: 0, nsMsgSaveAsDraft: 1 },
      nsIMsgCompType: { New: 0, ReplyToSender: 6 },
      nsIMsgFolder: { nsMsgDispositionState_Replied: 0 },
    },
    Components: { isSuccessCode: (status) => status === 0 },
    Services: {
      io: { offline: false },
      obs: { addObserver: (o, topic) => observers.push([o, topic]), removeObserver() {} },
      appShell: { hiddenDOMWindow: hidden },
      wm: { getEnumerator: () => [win, ...composers] },
    },
    console,
  });
  vm.runInContext(SOURCE + "\n;this.api = chamberlainMail;", context);
  const converted = [];
  const extensionContext = { extension: {
    tabManager: { get: () => ({ nativeTab: win }) },
    messageManager: {
      get: (id) => headers.get(id),
      convert(header) {
        converted.push(header.messageId);
        return {
          id: header.messageKey, headerMessageId: header.messageId,
          date: new Date(header.dateInSeconds * 1000), read: header.isRead,
          folder: { id: `${ACCOUNT}://Sent` },
        };
      },
    },
  } };
  const api = new context.api().getAPI(extensionContext).chamberlainMail;
  return { api, looked, observers, win, changes, converted, sends, hidden };
}

/** A Thunderbird common dialog opened by `opener`, with the buttons the real one has. */
function dialogFrom(opener, text) {
  const pressed = [];
  const dialog = {
    getButton: (name) => (name === "cancel" ? { hidden: false } : null),
    cancelDialog: () => pressed.push("cancel"),
    acceptDialog: () => pressed.push("accept"),
  };
  const doc = { title: "Save Message", getElementById: (id) => (id === "infoBody" ? { textContent: text } : null), querySelector: () => dialog };
  return { window: { opener, document: doc }, pressed };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

test("P-14 the privileged flag update validates the whole batch before one native call", async () => {
  const folderId = `${ACCOUNT}://Sent`;
  const folder = { URI: URIS[folderId] };
  const first = { folder, messageId: "one@x" };
  const second = { folder, messageId: "<two@x>" };
  const { api, changes } = load({ headers: new Map([[1, first], [2, second]]) });
  const firstRef = { id: 1, internetMessageId: "one@x" };
  await assert.rejects(api.setReadFlags(ACCOUNT, MAILBOX, folderId, [firstRef, { id: 2, internetMessageId: "wrong@x" }], true), /chamberlain:changed:/);
  assert.deepEqual(changes, []);
  await api.setReadFlags(ACCOUNT, MAILBOX, folderId, [firstRef, { id: 2, internetMessageId: "two@x" }], false);
  assert.deepEqual(changes, [{ messages: [first, second], read: false }]);
});

test("P-3 bulk attachment presence reads the native header flag and binds the account", async () => {
  const folder = { server: { key: "owner" } };
  const outbox = { server: { key: "local", type: "none" }, getFlag: (flag) => flag === 1 };
  const headers = new Map([[1, { folder, flags: 1 }], [2, { folder, flags: 0 }], [3, { folder: outbox, flags: 1 }]]);
  const { api } = load({ headers });
  assert.deepEqual([...(await api.headerAttachmentFlags(ACCOUNT, MAILBOX, [1, 2], false))], [true, false]);
  await assert.rejects(api.headerAttachmentFlags(ACCOUNT, MAILBOX, [3], false), /chamberlain:account_mismatch:/);
  assert.deepEqual([...(await api.headerAttachmentFlags(ACCOUNT, MAILBOX, [3], true))], [true]);
});

test("P-6 native lookup converts every visible exact match across dates, and only those matches", async () => {
  const folder = `${ACCOUNT}://Sent`;
  const folderMessages = [
    { messageId: "one@x", dateInSeconds: 100, messageKey: 1, isRead: true, flags: 0 },
    { messageId: "other@x", dateInSeconds: 80, messageKey: 2, flags: 0 },
    { messageId: "two@x", dateInSeconds: 12, messageKey: 3, flags: 2 },
    { messageId: "<one@x>", dateInSeconds: 1, messageKey: 4, isRead: false, flags: 0 },
  ];
  const { api, converted } = load({ folderMessages });
  const result = await api.lookupCopies(ACCOUNT, MAILBOX, folder, ["one@x", "two@x"]);
  assert.equal(result.complete, true);
  assert.deepEqual(Array.from(result.messages, (h) => [h.id, h.date, h.read]), [[1, 100_000, true], [4, 1_000, false]]);
  assert.deepEqual(converted, ["one@x", "<one@x>"]);
});

test("P-6 a native lookup that reaches its deadline does not claim a complete observation", async () => {
  let clock = 0;
  const folderMessages = {
    *[Symbol.iterator]() {
      yield { messageId: "other@x", flags: 0 };
      clock = 5000;
      yield { messageId: "one@x", flags: 0 };
    },
  };
  const { api, converted } = load({ folderMessages, now: () => clock });
  const result = await api.lookupCopies(ACCOUNT, MAILBOX, `${ACCOUNT}://Sent`, ["one@x"]);
  assert.equal(result.complete, false);
  assert.equal(result.messages.length, 0);
  assert.deepEqual(converted, []);
});

test("R8 a composer held with no draft folder named looks up none", async () => {
  const { api, looked } = load({ identityDrafts: "imap://owner/Elsewhere" });
  await api.claimComposer(1);
  const held = await api.holdComposer(1, ACCOUNT, MAILBOX, "", `${ACCOUNT}://Sent`);
  assert.equal(held.held, true);
  assert.deepEqual(looked, [`${ACCOUNT}://Sent`]);
});

test("R8 a draft save names both folders when the identity saves drafts elsewhere", async () => {
  const { api } = load({ identityDrafts: "imap://owner/Elsewhere" });
  await api.claimComposer(1);
  await assert.rejects(api.holdComposer(1, ACCOUNT, MAILBOX, `${ACCOUNT}://Drafts`, `${ACCOUNT}://Sent`), (error) => {
    assert.ok(error instanceof ExtensionError, "an ExtensionError keeps its text in Thunderbird");
    assert.match(error.message, /^chamberlain:changed:Thunderbird saves this identity's drafts to imap:\/\/owner\/Elsewhere/);
    return true;
  });
});

test("R8 a draft save with the identity on the bound folder is held", async () => {
  const { api } = load();
  await api.claimComposer(1);
  assert.equal((await api.holdComposer(1, ACCOUNT, MAILBOX, `${ACCOUNT}://Drafts`, `${ACCOUNT}://Sent`)).held, true);
});

test("R8 a Thunderbird prompt on a held composer is answered at once and reported", async () => {
  const { api, observers, win } = load();
  const [[observer, topic]] = observers;
  assert.equal(topic, "common-dialog-loaded");
  await api.claimComposer(7);
  await api.holdComposer(7, ACCOUNT, MAILBOX, "", `${ACCOUNT}://Sent`);
  const fcc = dialogFrom(win, "Your message was sent but a copy was not placed in your sent folder (Sent).");
  observer.observe(fcc.window);
  await settle();
  assert.deepEqual(fcc.pressed, ["cancel"], "Don't Save: the message is already sent");
  assert.deepEqual([...(await api.takePrompts(7)).prompts], ["Your message was sent but a copy was not placed in your sent folder (Sent)."]);
  assert.deepEqual([...(await api.takePrompts(7)).prompts], [], "taken once");
});

test("R8 a prompt on any other window is left alone", async () => {
  const { observers } = load();
  const [[observer]] = observers;
  const other = dialogFrom({}, "Something the user is doing.");
  observer.observe(other.window);
  await settle();
  assert.deepEqual(other.pressed, []);
});

const SENT_ID = `${ACCOUNT}://Sent`;
const DRAFTS_ID = `${ACCOUNT}://Drafts`;
const spec = (extra = {}) => ({
  mode: "send", to: ["paired@example.org"], cc: [], bcc: [], subject: "Re: Dates", body: "Exactly this.",
  highImportance: false, token: "<tok@chamberlain.invalid>", sentFolderId: SENT_ID, draftFolderId: DRAFTS_ID, sentCopy: "client", ...extra,
});

test("R8 only a composer the add-on opened can be held or closed; the owner's cannot", async () => {
  const { api, win } = load();
  await assert.rejects(api.holdComposer(1, ACCOUNT, MAILBOX, "", SENT_ID), /chamberlain:account_mismatch:/);
  await assert.rejects(api.closeComposer(1, ACCOUNT, MAILBOX), /chamberlain:account_mismatch:/);
  assert.equal(win.closed, undefined, "the owner's composer is still open");
  await api.claimComposer(1);
  await api.closeComposer(1, ACCOUNT, MAILBOX);
  assert.equal(win.closed, true);
});

test("H-1 composers are counted as the add-on's own or the owner's, read only", async () => {
  const other = { gMsgCompose: {} };
  const { api } = load({ composers: [other] });
  assert.deepEqual({ ...(await api.countComposers()) }, { own: 0, other: 2 });
  await api.claimComposer(1);
  assert.deepEqual({ ...(await api.countComposers()) }, { own: 1, other: 1 });
});

test("H-1 a headless send builds the exact message and parents prompts on the hidden window", async () => {
  const { api, sends, hidden } = load();
  const done = await api.composeHeadless(ACCOUNT, MAILBOX, spec({ highImportance: true }), [], false);
  assert.equal(sends.length, 1);
  const [{ fields, mode, parent, bodyType, body, accountKey }] = sends;
  assert.equal(mode, 0, "deliver now");
  assert.equal(parent, hidden, "never the most recent composer, which may be the owner's");
  assert.equal(accountKey, ACCOUNT);
  assert.equal(bodyType, "text/plain");
  assert.equal(body, "Exactly this.");
  assert.equal(fields.from, `Owner <${MAILBOX}>`);
  assert.equal(fields.to, "paired@example.org");
  assert.equal(fields.subject, "Re: Dates");
  assert.equal(fields.priority, "High");
  assert.equal(fields.fcc, URIS[SENT_ID]);
  assert.equal(fields.raw.get("X-Chamberlain-Write-Token"), "<tok@chamberlain.invalid>");
  assert.equal(done.filed, true);
  assert.equal(done.message_id, "generated@example.org");
});

test("H-2 sent_copy server sends with no client copy", async () => {
  const { api, sends } = load();
  const done = await api.composeHeadless(ACCOUNT, MAILBOX, spec({ sentCopy: "server" }), [], false);
  assert.equal(sends[0].fields.fcc, "nocopy://");
  assert.equal(done.filed, false);
});

test("H-3 a headless reply references the original and marks it replied", async () => {
  const marked = [];
  const folder = { server: { key: "owner" }, getUriForMsg: () => "imap-message://owner/INBOX#1", addMessageDispositionState: (h, state) => marked.push([h.messageId, state]) };
  const original = { folder, messageId: "orig@x", getStringProperty: () => "<root@x>" };
  const { api, sends } = load({ headers: new Map([[5, original]]) });
  await api.composeHeadless(ACCOUNT, MAILBOX, spec({ replyTo: 5 }), [], false);
  assert.equal(sends[0].fields.references, "<root@x> <orig@x>");
  assert.deepEqual(marked, [["orig@x", 0]]);
});

test("H-4 checkOnly runs every check and sends nothing", async () => {
  const { api, sends } = load({ identityDrafts: "imap://owner/Elsewhere" });
  assert.equal((await api.composeHeadless(ACCOUNT, MAILBOX, spec(), [], true)).checked, true);
  await assert.rejects(api.composeHeadless(ACCOUNT, MAILBOX, spec({ mode: "draft" }), [], true), /chamberlain:changed:/);
  assert.equal(sends.length, 0);
});

test("H-4 a failure Thunderbird reports after the call is never called clean", async () => {
  const { api } = load({ sendOutcome: "fail" });
  await assert.rejects(api.composeHeadless(ACCOUNT, MAILBOX, spec(), [], false), /chamberlain:timeout:/);
});

test("H-5 a prompt parented on the hidden window is answered only while a headless send runs", async () => {
  const { api, observers, hidden, sends } = load({ sendOutcome: "later" });
  const [[observer]] = observers;
  const before = dialogFrom(hidden, "Someone else's prompt.");
  observer.observe(before.window);
  await settle();
  assert.deepEqual(before.pressed, [], "nothing of the add-on's is running");
  const running = api.composeHeadless(ACCOUNT, MAILBOX, spec(), [], false);
  await settle();
  const during = dialogFrom(hidden, "Sending of the message failed.");
  observer.observe(during.window);
  await settle();
  assert.deepEqual(during.pressed, ["cancel"]);
  sends[0].finish();
  const done = await running;
  assert.deepEqual([...done.prompts], ["Sending of the message failed."], "reported with the result");
});
