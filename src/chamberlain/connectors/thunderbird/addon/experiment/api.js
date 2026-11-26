/* Narrow privileged helpers (bridge contract B-3): named functions, no generic execute.
 * Every function re-verifies that the account owns the sender identity. Runs only inside
 * Thunderbird; the fake used by the tests lives in test/fake_thunderbird.mjs. */
/* global ChromeUtils, Ci, Components, Services */
var { ExtensionCommon } = ChromeUtils.importESModule("resource://gre/modules/ExtensionCommon.sys.mjs");
var { setTimeout, clearTimeout } = ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");
var { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
var { getFolder } = ChromeUtils.importESModule("resource:///modules/ExtensionAccounts.sys.mjs");
var { ExtensionUtils } = ChromeUtils.importESModule("resource://gre/modules/ExtensionUtils.sys.mjs");

/* Thunderbird hides the text of a plain Error thrown here ("An unexpected error occurred").
 * An ExtensionError reaches the add-on intact: "chamberlain:<code>:<reason>". */
function need(value, code, reason = "") {
  if (!value) throw new ExtensionUtils.ExtensionError(`chamberlain:${code}:${reason}`);
}

function ownerAccount(accountId, mailbox) {
  const account = MailServices.accounts.getAccount(accountId);
  need(account && [...account.identities].some((i) => i.email.toLowerCase() === mailbox.toLowerCase()), "account_mismatch");
  return account;
}

function syncOne(id, accountId, timeoutMs) {
  const target = getFolder(id);
  need(target.accountKey === accountId && target.folder.server.type === "imap", "account_mismatch");
  // An account root, a container such as Gmail's "[Gmail]" and a saved search hold no mail:
  // there is nothing to sync, and waiting for one would never end.
  const flags = Ci.nsMsgFolderFlags;
  if (target.folder.isServer || (target.folder.flags & (flags.ImapNoselect | flags.Virtual)) !== 0) {
    return Promise.resolve({ folder_id: id, success: false, code: "not-selectable" });
  }
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (!done) {
        done = true;
        clearTimeout(timer);
        resolve(value);
      }
    };
    const timer = setTimeout(() => finish({ folder_id: id, success: false, code: "sync-callback-timeout" }), Math.min(timeoutMs || 8000, 15000));
    const listener = {
      QueryInterface: ChromeUtils.generateQI(["nsIUrlListener"]),
      OnStartRunningUrl() {},
      OnStopRunningUrl(url, status) {
        finish({ folder_id: id, success: Components.isSuccessCode(status) });
      },
    };
    try {
      target.folder.QueryInterface(Ci.nsIMsgImapMailFolder).updateFolderWithListener(null, listener);
    } catch (error) {
      finish({ folder_id: id, success: false, code: "sync-call-failed" });
    }
  });
}

function composeWindow(context, tabId) {
  return context.extension.tabManager.get(tabId).nativeTab;
}

/* A Thunderbird prompt on a composer the harness holds must never wait for a click: a
 * "copy not placed in your Sent folder" dialog once held the only write slot until someone
 * clicked it (found live, 2026-11-25). Such a prompt is answered at once with its safe
 * button (cancel: "Don't Save" on that dialog; otherwise OK) and its text kept for the
 * result, so the harness reports what Thunderbird asked and checks the outcome itself. */
const answeredPrompts = new Map(); // tab id -> prompt texts

function heldOpener(dialogWindow) {
  let win = dialogWindow.opener;
  for (let i = 0; win && i < 3; i++) {
    if (win.__chamberlainHeld) return win;
    win = win.opener;
  }
  return null;
}

function answerPrompt(dialogWindow) {
  const held = heldOpener(dialogWindow);
  if (!held) return false;
  const doc = dialogWindow.document;
  const text = String(doc.getElementById("infoBody")?.textContent ?? doc.title ?? "").trim().slice(0, 500);
  const list = answeredPrompts.get(held.__chamberlainTab) ?? [];
  list.push(text);
  answeredPrompts.set(held.__chamberlainTab, list);
  const dialog = doc.querySelector("dialog");
  const cancel = dialog?.getButton?.("cancel");
  if (cancel && !cancel.hidden) dialog.cancelDialog();
  else dialog?.acceptDialog();
  return true;
}

const promptObserver = {
  observe(subject) {
    // The dialog has just loaded; answer it once its buttons are wired.
    setTimeout(() => {
      try {
        answerPrompt(subject);
      } catch (error) {
        console.error("Chamberlain could not answer a Thunderbird prompt", error);
      }
    }, 0);
  },
};

var chamberlainMail = class extends ExtensionCommon.ExtensionAPI {
  onShutdown() {
    if (this.watching) Services.obs.removeObserver(promptObserver, "common-dialog-loaded");
  }

  getAPI(context) {
    if (!this.watching) {
      Services.obs.addObserver(promptObserver, "common-dialog-loaded");
      this.watching = true;
    }
    return {
      chamberlainMail: {
        async syncFolders(accountId, mailbox, folderIds, timeoutMs) {
          ownerAccount(accountId, mailbox);
          need(!Services.io.offline, "client_offline");
          need(Array.isArray(folderIds) && folderIds.length > 0 && folderIds.length <= 8, "bad_request");
          const folders = await Promise.all(folderIds.map((id) => syncOne(id, accountId, timeoutMs)));
          return { folders };
        },
        async headerAttachmentFlags(accountId, mailbox, messageIds, allowOutbox) {
          const account = ownerAccount(accountId, mailbox);
          need(messageIds.length > 0 && messageIds.length <= 50, "bad_request");
          return messageIds.map((id) => {
            const h = context.extension.messageManager.get(id);
            need(h, "not_found");
            const folder = h.folder;
            need(folder && (folder.server.key === account.incomingServer.key ||
              (allowOutbox && folder.server.type === "none" && folder.getFlag(Ci.nsMsgFolderFlags.Queue))), "account_mismatch");
            return Boolean(h.flags & Ci.nsMsgMessageFlags.Attachment);
          });
        },
        async verifyCopyWindow(accountId, mailbox, folderId, messageIds, expectedCounts, sinceMs, untilMs) {
          ownerAccount(accountId, mailbox);
          need(messageIds.length > 0 && messageIds.length <= 500 &&
            messageIds.length === expectedCounts.length && new Set(messageIds).size === messageIds.length &&
            expectedCounts.every((count) => Number.isSafeInteger(count) && count >= 0) &&
            Number.isSafeInteger(sinceMs) && Number.isSafeInteger(untilMs) && sinceMs <= untilMs,
          "bad_request");
          const target = getFolder(folderId);
          need(target.accountKey === accountId, "account_mismatch");
          if (target.folder.isServer || target.folder.getFlag(Ci.nsMsgFolderFlags.Virtual)) return false;
          const missing = new Map(messageIds.map((id, i) => [id, expectedCounts[i]]));
          const deadline = Date.now() + 5000;
          let scanned = 0;
          // Validate exact IDs across dates against the local native folder data. In particular,
          // an old copy of a requested ID cannot be hidden by a WebExtension date filter.
          for (const header of target.folder.messages) {
            if (++scanned > 200000 || Date.now() >= deadline) return false;
            if (scanned % 2000 === 0) await new Promise((resolve) => setTimeout(resolve, 0));
            const id = String(header.messageId ?? "").trim().replace(/^<(.*)>$/s, "$1");
            const count = missing.get(id);
            if (count === undefined) continue;
            const date = header.dateInSeconds * 1000;
            if (!Number.isSafeInteger(date) || date < sinceMs || date > untilMs || count === 0) return false;
            missing.set(id, count - 1);
          }
          ownerAccount(accountId, mailbox);
          return Date.now() < deadline && [...missing.values()].every((count) => count === 0);
        },
        async setReadFlags(accountId, mailbox, folderId, refs, read) {
          ownerAccount(accountId, mailbox);
          need(!Services.io.offline, "client_offline");
          need(refs.length > 0 && refs.length <= 500 && new Set(refs.map((ref) => ref.id)).size === refs.length, "bad_request");
          const target = getFolder(folderId);
          need(target.accountKey === accountId, "account_mismatch");
          const headers = refs.map(({ id, internetMessageId }) => {
            const h = context.extension.messageManager.get(id);
            need(h?.folder?.URI === target.folder.URI && h.messageId.replace(/^<|>$/g, "") === internetMessageId, "changed");
            return h;
          });
          target.folder.markMessagesRead(headers, read);
        },
        async holdComposer(tabId, accountId, mailbox, draftFolderId, sentFolderId) {
          const account = ownerAccount(accountId, mailbox);
          const win = composeWindow(context, tabId);
          need(win.gMsgCompose && [...account.identities].some((i) => i.key === win.gCurrentIdentity.key && i.email.toLowerCase() === mailbox.toLowerCase()), "account_mismatch");
          need(!win.gSendOperationInProgress && !win.gSaveOperationInProgress, "busy");
          win.clearTimeout(win.gAutoSaveTimeout);
          win.gAutoSaveTimeout = null; // autosave stays off in a managed composer (R8)
          // A send never saves a draft (autosave is off), so only a draft save checks the folder.
          if (draftFolderId) {
            const draft = getFolder(draftFolderId);
            need(draft.accountKey === accountId && draft.folder.canFileMessages, "account_mismatch", "The bound draft folder is not in this account.");
            const uses = win.gCurrentIdentity.getUnicharAttribute("draft_folder");
            need(uses === draft.folder.URI, "changed", `Thunderbird saves this identity's drafts to ${uses}, not the bound draft folder ${draft.folder.URI}. Change Drafts in the account's Copies & Folders settings, or the bound folder.`);
          }
          const sent = getFolder(sentFolderId);
          need(sent.accountKey === accountId && sent.folder.canFileMessages, "account_mismatch", "The bound Sent folder is not in this account.");
          need(win.gMsgCompose.compFields.fcc === sent.folder.URI && !win.gMsgCompose.compFields.fcc2, "changed", `The composer files its Sent copy in ${win.gMsgCompose.compFields.fcc}, not the bound Sent folder ${sent.folder.URI}.`);
          win.__chamberlainHeld = true;
          win.__chamberlainTab = tabId;
          answeredPrompts.delete(tabId);
          return { held: true };
        },
        async openDraft(accountId, mailbox, messageId, internetMessageId) {
          const account = ownerAccount(accountId, mailbox);
          const h = context.extension.messageManager.get(messageId);
          need(h && h.folder.server.key === account.incomingServer.key && h.folder.getFlag(Ci.nsMsgFolderFlags.Drafts), "account_mismatch");
          need(!internetMessageId || h.messageId === internetMessageId, "changed");
          const uri = h.folder.getUriForMsg(h);
          for (const win of Services.wm.getEnumerator("msgcompose")) {
            need(win.gMsgCompose?.originalMsgURI !== uri && win.gMsgCompose?.compFields?.draftId?.split("?")[0] !== uri, "changed");
          }
          const identities = [...account.identities].filter((i) => i.email.toLowerCase() === mailbox.toLowerCase());
          need(identities.length === 1, "account_mismatch");
          const previous = new Set(Services.wm.getEnumerator("msgcompose"));
          MailServices.compose.OpenComposeWindow(null, h, uri, Ci.nsIMsgCompType.Draft, identities[0].composeHtml ? Ci.nsIMsgCompFormat.OppositeOfDefault : Ci.nsIMsgCompFormat.Default, identities[0], null, null);
          const win = await new Promise((resolve, reject) => {
            const started = Date.now();
            const look = () => {
              for (const candidate of Services.wm.getEnumerator("msgcompose")) {
                if (!previous.has(candidate) && candidate.composeEditorReady && (candidate.gMsgCompose?.originalMsgURI === uri || candidate.gMsgCompose?.compFields?.draftId?.split("?")[0] === uri)) {
                  resolve(candidate);
                  return;
                }
              }
              if (Date.now() - started >= 15000) reject(new ExtensionUtils.ExtensionError("chamberlain:timeout:The reopened draft did not open in time."));
              else setTimeout(look, 100);
            };
            look();
          });
          win.clearTimeout(win.gAutoSaveTimeout);
          win.gAutoSaveTimeout = null;
          return { tab_id: context.extension.tabManager.convert(win).id };
        },
        async takePrompts(tabId) {
          const prompts = answeredPrompts.get(tabId) ?? [];
          answeredPrompts.delete(tabId);
          return { prompts };
        },
        async findComposers(accountId, mailbox, messageId, internetMessageId) {
          const account = ownerAccount(accountId, mailbox);
          const h = context.extension.messageManager.get(messageId);
          need(h && h.folder.server.key === account.incomingServer.key, "account_mismatch");
          need(!internetMessageId || h.messageId === internetMessageId, "changed");
          const uri = h.folder.getUriForMsg(h);
          const ids = [];
          for (const win of Services.wm.getEnumerator("msgcompose")) {
            if (win.__chamberlainHeld && (win.gMsgCompose?.originalMsgURI === uri || win.gMsgCompose?.compFields?.draftId?.split("?")[0] === uri)) {
              ids.push(context.extension.tabManager.convert(win).id);
            }
          }
          return { tab_ids: ids };
        },
        async closeComposer(tabId, accountId, mailbox) {
          ownerAccount(accountId, mailbox);
          const win = composeWindow(context, tabId);
          need(win.__chamberlainHeld || win.gMsgCompose, "account_mismatch");
          need(!win.gSendOperationInProgress && !win.gSaveOperationInProgress, "busy");
          win.gContentChanged = false;
          win.gMsgCompose.bodyModified = false;
          win.gAutoSaveKickedIn = false;
          win.close();
          return { closed: true };
        },
      },
    };
  }
};
