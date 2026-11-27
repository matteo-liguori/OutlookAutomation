/* Narrow privileged helpers (bridge contract B-3): named functions, no generic execute.
 * Every function re-verifies that the account owns the sender identity. Runs only inside
 * Thunderbird; the fake used by the tests lives in test/fake_thunderbird.mjs. */
/* global ChromeUtils, Cc, Ci, Components, IOUtils, PathUtils, Services */
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

/* Composer windows this add-on opened, tracked by identity. Only these are ever held, edited or
 * closed; the owner's own composers are only ever counted, or read to refuse a change. */
const ownWindows = new WeakSet();

function ownWindow(context, tabId) {
  const win = composeWindow(context, tabId);
  need(win && ownWindows.has(win), "account_mismatch", "That composer was not opened by Chamberlain.");
  return win;
}

/* A Thunderbird prompt on a composer the harness holds must never wait for a click: a
 * "copy not placed in your Sent folder" dialog once held the only write slot until someone
 * clicked it (found live, 2026-11-25). Such a prompt is answered at once with its safe
 * button (cancel: "Don't Save" on that dialog; otherwise OK) and its text kept for the
 * result, so the harness reports what Thunderbird asked and checks the outcome itself. */
const answeredPrompts = new Map(); // tab id -> prompt texts

/* A headless send has no window of its own. Without a parent, Thunderbird parents its prompts on
 * the most recent composer, which may be the owner's. The add-on passes the hidden window
 * instead, and answers prompts parented there only while a headless request of its own runs. */
const headlessPrompts = new Map(); // request key -> prompt texts

function hiddenWindow() {
  return Services.appShell.hiddenDOMWindow;
}

function promptOwner(dialogWindow) {
  const hidden = headlessPrompts.size ? hiddenWindow() : null;
  let win = dialogWindow.opener;
  for (let i = 0; win && i < 3; i++) {
    if (win.__chamberlainHeld && ownWindows.has(win)) return { list: answeredPrompts, key: win.__chamberlainTab };
    if (hidden && win === hidden) return { list: headlessPrompts, key: [...headlessPrompts.keys()][0] };
    win = win.opener;
  }
  return null;
}

function answerPrompt(dialogWindow) {
  const owner = promptOwner(dialogWindow);
  if (!owner) return false;
  const doc = dialogWindow.document;
  const text = String(doc.getElementById("infoBody")?.textContent ?? doc.title ?? "").trim().slice(0, 500);
  const list = owner.list.get(owner.key) ?? [];
  list.push(text);
  owner.list.set(owner.key, list);
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

/* --- headless composition (no window) -------------------------------------------------------
 * A send or a draft save is built as nsIMsgCompFields and handed to nsIMsgSend directly, so no
 * composer window opens. What the window path guaranteed is kept: the sender identity, the
 * write token header, References (Thunderbird derives In-Reply-To from them), the Replied flag
 * on the original, attachments, priority, and the Sent copy in the bound folder or none. */
const HEADLESS_TIMEOUT_MS = 40000;
const FCC_WAIT_MS = 20000;

function headlessPlan(context, accountId, mailbox, spec, files) {
  const account = ownerAccount(accountId, mailbox);
  const identities = [...account.identities].filter((i) => i.email.toLowerCase() === mailbox.toLowerCase());
  need(identities.length === 1, "account_mismatch", "The sender identity is missing or ambiguous.");
  const identity = identities[0];
  need(spec.mode === "send" || spec.mode === "draft", "bad_request");
  if (spec.mode === "send") need(!Services.io.offline, "client_offline");
  need(Array.isArray(files) && files.length <= 50, "bad_request");
  const sent = getFolder(spec.sentFolderId);
  need(sent.accountKey === accountId && sent.folder.canFileMessages, "account_mismatch", "The bound Sent folder is not in this account.");
  if (spec.mode === "draft") {
    const draft = getFolder(spec.draftFolderId);
    need(draft.accountKey === accountId && draft.folder.canFileMessages, "account_mismatch", "The bound draft folder is not in this account.");
    const uses = identity.getUnicharAttribute("draft_folder");
    need(uses === draft.folder.URI, "changed", `Thunderbird saves this identity's drafts to ${uses}, not the bound draft folder ${draft.folder.URI}. Change Drafts in the account's Copies & Folders settings, or the bound folder.`);
  }
  const header = (id) => {
    const h = context.extension.messageManager.get(id);
    need(h && h.folder.server.key === account.incomingServer.key, "account_mismatch");
    return h;
  };
  const original = spec.replyTo == null ? null : header(spec.replyTo);
  const replaced = spec.replaceDraft == null ? null : header(spec.replaceDraft);
  if (replaced) {
    need(replaced.folder.getFlag(Ci.nsMsgFolderFlags.Drafts), "account_mismatch");
    const uri = replaced.folder.getUriForMsg(replaced);
    // A draft open in any composer, the owner's included, is never replaced under it (read only).
    for (const win of Services.wm.getEnumerator("msgcompose")) {
      need(win.gMsgCompose?.originalMsgURI !== uri && win.gMsgCompose?.compFields?.draftId?.split("?")[0] !== uri, "changed", "The draft is open in a composer.");
    }
  }
  return { account, identity, sentFolder: sent.folder, original, replaced };
}

function compFieldsOf(plan, spec) {
  const fields = Cc["@mozilla.org/messengercompose/composefields;1"].createInstance(Ci.nsIMsgCompFields);
  fields.from = MailServices.headerParser.makeMimeAddress(plan.identity.fullName, plan.identity.email);
  fields.to = spec.to.join(", ");
  fields.cc = spec.cc.join(", ");
  fields.bcc = spec.bcc.join(", ");
  fields.subject = spec.subject;
  fields.body = spec.body;
  fields.forcePlainText = true;
  fields.useMultipartAlternative = false;
  fields.characterSet = "UTF-8";
  if (spec.highImportance) fields.priority = "High";
  fields.setRawHeader("X-Chamberlain-Write-Token", spec.token);
  // "nocopy://" files no client copy: the server keeps its own (sent_copy: server).
  fields.fcc = spec.mode === "send" ? (spec.sentCopy === "server" ? "nocopy://" : plan.sentFolder.URI) : "";
  fields.fcc2 = "";
  if (plan.original) {
    const references = String(plan.original.getStringProperty("references") ?? "").trim();
    const id = String(plan.original.messageId).replace(/^<|>$/g, "");
    fields.references = (references ? references + " " : "") + `<${id}>`;
  }
  return fields;
}

async function attachFiles(fields, files, key) {
  if (!files.length) return null;
  const dir = PathUtils.join(PathUtils.tempDir, `chamberlain-${key}`);
  await IOUtils.makeDirectory(dir, { ignoreExisting: true, permissions: 0o700 });
  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    const path = PathUtils.join(dir, `a${i + 1}`); // the sender's file name never becomes a path
    await IOUtils.write(path, new Uint8Array(await file.arrayBuffer()));
    const attachment = Cc["@mozilla.org/messengercompose/attachment;1"].createInstance(Ci.nsIMsgAttachment);
    attachment.url = PathUtils.toFileURI(path);
    attachment.name = file.name;
    attachment.size = file.size;
    attachment.contentType = file.type || "application/octet-stream";
    attachment.temporary = true;
    fields.addAttachment(attachment);
  }
  return dir;
}

let headlessCount = 0;

/** One submission or one draft save. Resolves after Thunderbird reports the send (or the save)
 * and, for a client Sent copy, its filing; a failure after the call may have sent. */
function headlessOutcome(plan, spec, fields) {
  const sending = spec.mode === "send";
  return new Promise((resolve, reject) => {
    let stopped = null;
    let copied = !sending || fields.fcc === "nocopy://";
    let fccTimer = null;
    const failed = (code, text) => {
      clearTimeout(timer);
      clearTimeout(fccTimer);
      reject(new ExtensionUtils.ExtensionError(`chamberlain:${code}:${text}`));
    };
    const timer = setTimeout(() => failed("timeout", "Thunderbird did not report the result in time."), HEADLESS_TIMEOUT_MS);
    const finish = () => {
      if (stopped === null || !copied) return;
      clearTimeout(timer);
      clearTimeout(fccTimer);
      resolve(stopped);
    };
    const listener = {
      QueryInterface: ChromeUtils.generateQI(["nsIMsgSendListener", "nsIMsgCopyServiceListener"]),
      onStartSending() {},
      onSendProgress() {},
      onProgress() {},
      onStatus() {},
      onGetDraftFolderURI() {},
      onSendNotPerformed(msgId, status) {
        failed("timeout", `Thunderbird did not send (${status}).`);
      },
      onTransportSecurityError() {},
      onStopSending(msgId, status) {
        if (!Components.isSuccessCode(status)) {
          failed("timeout", `Thunderbird reported a failure (${status}); the server may still have the message.`);
          return;
        }
        stopped = { messageId: String(msgId || fields.messageId || ""), filed: copied && sending && fields.fcc !== "nocopy://" };
        if (!copied) {
          fccTimer = setTimeout(() => {
            copied = true;
            stopped.filed = false;
            finish();
          }, FCC_WAIT_MS);
        }
        finish();
      },
      onStartCopy() {},
      setMessageKey() {},
      getMessageId() {
        return null;
      },
      onStopCopy(status) {
        const ok = Components.isSuccessCode(status);
        if (!sending && stopped === null) {
          // A draft save reports its result through the copy into the Drafts folder.
          if (!ok) {
            failed("timeout", `The draft was not saved (${status}).`);
            return;
          }
          stopped = { messageId: String(fields.messageId || ""), filed: true };
        } else if (stopped) {
          stopped.filed = ok;
        }
        copied = true;
        finish();
      },
    };
    const sender = Cc["@mozilla.org/messengercompose/send;1"].createInstance(Ci.nsIMsgSend);
    const originalUri = plan.original ? plan.original.folder.getUriForMsg(plan.original) : "";
    const type = plan.original ? Ci.nsIMsgCompType.ReplyToSender : Ci.nsIMsgCompType.New;
    const mode = sending ? Ci.nsIMsgSend.nsMsgDeliverNow : Ci.nsIMsgSend.nsMsgSaveAsDraft;
    Promise.resolve()
      .then(() => sender.createAndSendMessage(null, plan.identity, plan.account.key, fields, false, false, mode, plan.replaced, "text/plain", spec.body, hiddenWindow(), null, listener, null, originalUri, type))
      .catch((error) => failed("timeout", String(error?.message ?? error).slice(0, 200)));
  });
}

async function runHeadless(plan, spec, files) {
  const key = `${Date.now()}-${++headlessCount}`;
  const fields = compFieldsOf(plan, spec);
  headlessPrompts.set(key, []);
  let dir = null;
  try {
    dir = await attachFiles(fields, files, key);
    const outcome = await headlessOutcome(plan, spec, fields);
    if (spec.mode === "send" && plan.original) {
      // As a reply from a composer: the original is marked replied, its read state untouched.
      plan.original.folder.addMessageDispositionState(plan.original, Ci.nsIMsgFolder.nsMsgDispositionState_Replied);
    }
    return { message_id: outcome.messageId.replace(/^<|>$/g, ""), filed: outcome.filed, prompts: headlessPrompts.get(key) ?? [] };
  } finally {
    headlessPrompts.delete(key);
    if (dir) IOUtils.remove(dir, { recursive: true, ignoreAbsent: true }).catch(() => undefined);
  }
}

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
        async lookupCopies(accountId, mailbox, folderId, messageIds) {
          ownerAccount(accountId, mailbox);
          const wanted = new Set(messageIds);
          need(messageIds.length > 0 && messageIds.length <= 500 && wanted.size === messageIds.length, "bad_request");
          const target = getFolder(folderId);
          need(target.accountKey === accountId, "account_mismatch");
          const messages = [];
          const incomplete = { messages, complete: false };
          if (target.folder.isServer || target.folder.getFlag(Ci.nsMsgFolderFlags.Virtual | Ci.nsMsgFolderFlags.ImapNoselect)) return incomplete;
          const deadline = Date.now() + 5000;
          const deleted = Ci.nsMsgMessageFlags.IMAPDeleted | Ci.nsMsgMessageFlags.Expunged;
          let scanned = 0;
          // Thunderbird's list also excludes logically deleted IMAP headers. Convert only the
          // matching, visible physical copies; a first-hit ID lookup would hide duplicates.
          for (const header of target.folder.messages) {
            if (++scanned > 200000 || Date.now() >= deadline) return incomplete;
            if (scanned % 2000 === 0) await new Promise((resolve) => setTimeout(resolve, 0));
            const id = String(header.messageId ?? "").trim().replace(/^<(.*)>$/s, "$1");
            if (!wanted.has(id) || (header.flags & deleted)) continue;
            const converted = context.extension.messageManager.convert(header);
            if (!converted || converted.folder?.id !== folderId) return incomplete;
            const date = converted.date.getTime();
            if (!Number.isSafeInteger(date)) return incomplete;
            messages.push({ ...converted, date });
          }
          ownerAccount(accountId, mailbox);
          return { messages, complete: Date.now() < deadline };
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
        async claimComposer(tabId) {
          // Called at once on a composer the add-on itself just opened (beginNew, beginReply).
          const win = composeWindow(context, tabId);
          need(win?.gMsgCompose, "not_found", "No composer has that tab.");
          ownWindows.add(win);
          return { claimed: true };
        },
        async holdComposer(tabId, accountId, mailbox, draftFolderId, sentFolderId) {
          const account = ownerAccount(accountId, mailbox);
          const win = ownWindow(context, tabId);
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
          ownWindows.add(win);
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
            if (win.__chamberlainHeld && ownWindows.has(win) && (win.gMsgCompose?.originalMsgURI === uri || win.gMsgCompose?.compFields?.draftId?.split("?")[0] === uri)) {
              ids.push(context.extension.tabManager.convert(win).id);
            }
          }
          return { tab_ids: ids };
        },
        async closeComposer(tabId, accountId, mailbox) {
          ownerAccount(accountId, mailbox);
          // Only a composer this add-on opened: never one of the owner's own windows.
          const win = ownWindow(context, tabId);
          need(!win.gSendOperationInProgress && !win.gSaveOperationInProgress, "busy");
          win.gContentChanged = false;
          win.gMsgCompose.bodyModified = false;
          win.gAutoSaveKickedIn = false;
          win.close();
          return { closed: true };
        },
        async countComposers() {
          // Read-only: composers this add-on opened that are still open, and all the others.
          let own = 0;
          let other = 0;
          for (const win of Services.wm.getEnumerator("msgcompose")) {
            if (win.closed) continue;
            if (ownWindows.has(win)) own++;
            else other++;
          }
          return { own, other };
        },
        async composeHeadless(accountId, mailbox, spec, files, checkOnly) {
          // checkOnly runs every check and changes nothing, so a refusal proves no mutation.
          try {
            const plan = headlessPlan(context, accountId, mailbox, spec, files);
            if (checkOnly) return { checked: true };
            return await runHeadless(plan, spec, files);
          } catch (error) {
            if (error instanceof ExtensionUtils.ExtensionError) throw error;
            // A plain error would reach the add-on as "An unexpected error occurred": keep its text.
            const where = checkOnly ? "check" : "send";
            throw new ExtensionUtils.ExtensionError(`chamberlain:internal:${where}: ${String(error?.message ?? error).slice(0, 300)}`);
          }
        },
      },
    };
  }
};
