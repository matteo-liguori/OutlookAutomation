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

function load({ identityDrafts = URIS[`${ACCOUNT}://Drafts`], headers = new Map() } = {}) {
  const looked = [];
  const changes = [];
  const identity = { key: "id1", email: MAILBOX, getUnicharAttribute: () => identityDrafts };
  const win = {
    gMsgCompose: { compFields: { fcc: URIS[`${ACCOUNT}://Sent`], fcc2: "" } },
    gCurrentIdentity: identity,
    clearTimeout() {},
  };
  const modules = {
    "resource://gre/modules/ExtensionCommon.sys.mjs": { ExtensionCommon: { ExtensionAPI: class {} } },
    "resource://gre/modules/Timer.sys.mjs": { setTimeout, clearTimeout },
    "resource:///modules/MailServices.sys.mjs": {
      MailServices: { accounts: { getAccount: () => ({ identities: [identity], incomingServer: { key: "owner" } }) } },
    },
    "resource:///modules/ExtensionAccounts.sys.mjs": {
      getFolder(id) {
        looked.push(id);
        if (!URIS[id]) throw new ExtensionError(`Folder not found: ${id}`);
        return { accountKey: ACCOUNT, folder: { canFileMessages: true, URI: URIS[id], markMessagesRead: (messages, read) => changes.push({ messages: [...messages], read }) } };
      },
    },
    "resource://gre/modules/ExtensionUtils.sys.mjs": { ExtensionUtils: { ExtensionError } },
  };
  const observers = [];
  const context = vm.createContext({
    ChromeUtils: { importESModule: (url) => modules[url] },
    Ci: { nsMsgFolderFlags: { Queue: 1 }, nsMsgMessageFlags: { Attachment: 1 } },
    Components: {},
    Services: { io: { offline: false }, obs: { addObserver: (o, topic) => observers.push([o, topic]), removeObserver() {} } },
    console,
  });
  vm.runInContext(SOURCE + "\n;this.api = chamberlainMail;", context);
  const extensionContext = { extension: { tabManager: { get: () => ({ nativeTab: win }) }, messageManager: { get: (id) => headers.get(id) } } };
  const api = new context.api().getAPI(extensionContext).chamberlainMail;
  return { api, looked, observers, win, changes };
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

test("R8 a send holds the composer without looking up any draft folder", async () => {
  const { api, looked } = load({ identityDrafts: "imap://owner/Elsewhere" });
  const held = await api.holdComposer(1, ACCOUNT, MAILBOX, "", `${ACCOUNT}://Sent`);
  assert.equal(held.held, true);
  assert.deepEqual(looked, [`${ACCOUNT}://Sent`]);
});

test("R8 a draft save names both folders when the identity saves drafts elsewhere", async () => {
  const { api } = load({ identityDrafts: "imap://owner/Elsewhere" });
  await assert.rejects(api.holdComposer(1, ACCOUNT, MAILBOX, `${ACCOUNT}://Drafts`, `${ACCOUNT}://Sent`), (error) => {
    assert.ok(error instanceof ExtensionError, "an ExtensionError keeps its text in Thunderbird");
    assert.match(error.message, /^chamberlain:changed:Thunderbird saves this identity's drafts to imap:\/\/owner\/Elsewhere/);
    return true;
  });
});

test("R8 a draft save with the identity on the bound folder is held", async () => {
  const { api } = load();
  assert.equal((await api.holdComposer(1, ACCOUNT, MAILBOX, `${ACCOUNT}://Drafts`, `${ACCOUNT}://Sent`)).held, true);
});

test("R8 a Thunderbird prompt on a held composer is answered at once and reported", async () => {
  const { api, observers, win } = load();
  const [[observer, topic]] = observers;
  assert.equal(topic, "common-dialog-loaded");
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
