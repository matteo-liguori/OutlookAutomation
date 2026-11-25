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

function load({ identityDrafts = URIS[`${ACCOUNT}://Drafts`] } = {}) {
  const looked = [];
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
      MailServices: { accounts: { getAccount: () => ({ identities: [identity] }) } },
    },
    "resource:///modules/ExtensionAccounts.sys.mjs": {
      getFolder(id) {
        looked.push(id);
        if (!URIS[id]) throw new ExtensionError(`Folder not found: ${id}`);
        return { accountKey: ACCOUNT, folder: { canFileMessages: true, URI: URIS[id] } };
      },
    },
    "resource://gre/modules/ExtensionUtils.sys.mjs": { ExtensionUtils: { ExtensionError } },
  };
  const context = vm.createContext({
    ChromeUtils: { importESModule: (url) => modules[url] },
    Ci: {},
    Components: {},
    Services: { io: { offline: false } },
  });
  vm.runInContext(SOURCE + "\n;this.api = chamberlainMail;", context);
  const extensionContext = { extension: { tabManager: { get: () => ({ nativeTab: win }) } } };
  const api = new context.api().getAPI(extensionContext).chamberlainMail;
  return { api, looked };
}

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
