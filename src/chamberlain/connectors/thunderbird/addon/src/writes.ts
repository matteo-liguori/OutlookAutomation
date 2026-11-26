// Write commands and read-only copy observations.
// Each write performs one logical mutation, never retries, keeps no journal. The add-on
// reports facts; deciding what absence proves is the Python connector's job.

const TUNING = { movePollMs: 250, moveRefreshMs: 2_000, savePollMs: 250, saveAttempts: 24, batchCheckMs: 25_000 };

interface Ctx {
  id: string;
  cancelled: boolean;
  mutationIssued: boolean;
  files: Map<string, File>;
}

/** Call immediately before a mutating `messenger` call; after the deadline it refuses. */
function markMutation(ctx: Ctx): void {
  if (ctx.cancelled) fail("timeout", "The deadline passed before the change was made.");
  ctx.mutationIssued = true;
}

function escapeHtml(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

// --- move, trash, mark read -----------------------------------------------------------------

async function refreshForChange(a: Bound, folderIds: string[], deadline: number): Promise<boolean> {
  const timeout = Math.min(8_000, deadline - Date.now());
  if (timeout < 500) return false;
  try {
    await cmdRefresh({ account_id: a.account_id, mailbox: a.mailbox, folder_ids: folderIds, timeout_ms: timeout });
  } catch {
    // A folder may not support provider refresh. The complete local scans still decide the result.
  }
  return true;
}

/** Resolve, move once and verify exact physical copies, in bulk, in both folders. */
async function moveBatch(a: ChangeArgs, group: ChangeGroup, destination: string, ctx: Ctx): Promise<ChangeResult> {
  await folderOf(a, destination);
  check(group.folder_id !== destination, "changed", "The messages are already in that folder.");
  const source = await readIdGroup(a, group);
  check(source.complete, "ambiguous", "The folder could not be scanned completely.");
  const located = changeItems(group, source);
  check(new Set(located.map((m) => m.id)).size === located.length, "bad_request", "The batch repeats a message.");
  if (!located.some((m) => m.header)) {
    if (located.some((m) => m.item.code === "ambiguous")) return fail("ambiguous", "The folder contains duplicate copies.");
    return fail("not_found", "None of the messages is in that folder.");
  }
  const scanFolder = (folderId: string) => readIdGroup(a, { folder_id: folderId, ids: group.ids });
  // Filing leaves existing copies alone; Trash may merge into one without changing its flag.
  const before = await scanFolder(destination);
  check(before.complete, "ambiguous", "The destination could not be scanned completely.");
  const items = located.map((m) => m.item);
  const moving = new Map<string, TbHeader>();
  for (const { id, header, item } of located) {
    if (!header) continue;
    if (a.trash || !before.map.has(id)) moving.set(id, header);
    else setChangeCode(item, "ambiguous");
  }
  const ids = [...moving.values()].map((h) => h.id);
  if (ids.length === 0) return fail("ambiguous", "Every message is already at the destination.");
  const helper = messenger.chamberlainMail;
  check(typeof helper?.setReadFlags === "function", "unsupported", "The bulk read flag helper is not installed.");
  markMutation(ctx);
  await messenger.messages.move(ids, destination);
  const giveUpAt = Date.now() + TUNING.batchCheckMs;
  let arrivalRefreshes = 0;
  let nextArrivalRefresh = 0;
  let staleRefRetried = false;
  const updatedStates = new Set<boolean>();
  while (!ctx.cancelled && Date.now() < giveUpAt) {
    const left = await scanFolder(group.folder_id);
    const there = await scanFolder(destination);
    if (ctx.cancelled || Date.now() >= giveUpAt) break;
    const changedRead: BulkReadRef[] = [];
    const changedUnread: BulkReadRef[] = [];
    let merged = false;
    let preserved = true;
    const arrived = left.complete && there.complete && [...moving].every(([id, original]) => {
      if (left.map.has(id)) return false;
      const previous = new Set((before.map.get(id) ?? []).map((h) => h.id));
      const copies = there.map.get(id) ?? [];
      const existing = copies.filter((h) => previous.has(h.id));
      if (existing.length !== previous.size) return false;
      const added = copies.filter((h) => !previous.has(h.id));
      if (added.length === 0 && a.trash && existing.length) {
        merged = true;
        preserved &&= existing.every((h) => h.read === original.read);
        return true;
      }
      if (added.length !== 1) return false;
      const targetRead = a.set_read ?? original.read;
      if (added[0].read !== targetRead) {
        (targetRead ? changedRead : changedUnread).push({ id: added[0].id, internetMessageId: id });
      }
      return true;
    });
    const needsUpdate = changedRead.length > 0 || changedUnread.length > 0;
    const now = Date.now();
    if ((!arrived || (arrivalRefreshes === 0 && (needsUpdate || a.set_read != null))) &&
        arrivalRefreshes < 3 && now >= nextArrivalRefresh) {
      // A provider refresh can finish before a move is visible. Refresh again while
      // arrival is unproven so the remainder of the window does not poll a stale cache.
      arrivalRefreshes++;
      if (await refreshForChange(a, [group.folder_id, destination], giveUpAt)) {
        nextArrivalRefresh = Date.now() + TUNING.moveRefreshMs;
        continue;
      }
    }
    if (arrived) {
      if (!needsUpdate) return changeResult(items, true, preserved, merged);
      let rescan = false;
      for (const [read, refs] of [[true, changedRead], [false, changedUnread]] as const) {
        if (!refs.length || updatedStates.has(read)) continue;
        markMutation(ctx);
        try {
          await helper.setReadFlags(a.account_id, a.mailbox, destination, refs, read);
          updatedStates.add(read);
        } catch (error) {
          const code = helperFailure(String((error as Error)?.message ?? error))?.code;
          // setReadFlags validates all refs before mutation. A stale ref can be resolved once.
          if (code !== "changed" || staleRefRetried) throw error;
          staleRefRetried = true;
        }
        rescan = true;
      }
      if (rescan) {
        // The native call starts the IMAP flag update, then the folder scan checks its result.
        await refreshForChange(a, [destination], giveUpAt);
        continue;
      }
      if (a.set_read == null) return changeResult(items, true, false, merged);
    }
    await sleep(TUNING.movePollMs * 4);
  }
  // Not seen in time: the harness's own check (reconcile) settles it.
  return changeResult(items, false);
}

interface IdGroup {
  map: Map<string, TbHeader[]>;
  complete: boolean;
}

async function readIdGroup(a: Bound, group: ChangeGroup): Promise<IdGroup> {
  const scan = await lookupCopies(a, group.folder_id, group.ids);
  const map = new Map<string, TbHeader[]>();
  for (const header of scan.messages) {
    const id = bare(header.headerMessageId);
    const copies = map.get(id);
    if (copies) copies.push(header);
    else map.set(id, [header]);
  }
  return { map, complete: scan.complete };
}

function changeItem(folder: string, raw: string, code: string): ChangeItem {
  return { ref: { folder_id: folder, internet_message_id: raw }, ok: !code, code };
}

function changeItems(group: ChangeGroup, found: IdGroup) {
  return group.ids.map((raw) => {
    const id = bare(raw);
    const copies = found.map.get(id) ?? [];
    const code = !found.complete || copies.length > 1 ? "ambiguous" : copies.length ? "" : "not_found";
    const header = code ? null : copies[0];
    return { id, header, item: changeItem(group.folder_id, raw, code) };
  });
}

function setChangeCode(item: ChangeItem, code: string): void {
  item.code = code;
  item.ok = !code;
}

function changeResult(
  items: ChangeItem[], verified: boolean, preserved: boolean | null = null, merged = false,
): ChangeResult {
  return { items, verified, read_state_preserved: preserved, merged_existing_copy: merged };
}

interface FlagGroup {
  group: ChangeGroup;
  items: ChangeItem[];
}

const FLAG_PRECHECK_CODES = new Set<BridgeErrorCode>([
  "account_mismatch", "bad_request", "changed", "client_offline", "not_found", "unsupported",
]);

/** The code of a failure that proves the helper changed nothing, if it is one. */
function precheckCode(error: unknown): BridgeErrorCode | undefined {
  const code = error instanceof BridgeFailure
    ? error.code
    : helperFailure(String((error as Error)?.message ?? error))?.code;
  return code && FLAG_PRECHECK_CODES.has(code) ? code : undefined;
}

async function verifyFlagGroups(a: Bound, groups: FlagGroup[], read: boolean, ctx: Ctx): Promise<void> {
  const deadline = Date.now() + TUNING.batchCheckMs;
  let pending = groups;
  let refreshed = false;
  while (pending.length && !ctx.cancelled && Date.now() < deadline) {
    const remaining: FlagGroup[] = [];
    for (const { group, items } of pending) {
      if (ctx.cancelled || Date.now() >= deadline) return;
      const after = await readIdGroup(a, group).catch(() => null);
      const unknown: ChangeItem[] = [];
      for (const item of items) {
        const copies = after?.map.get(bare(item.ref.internet_message_id)) ?? [];
        if (!after?.complete || copies.length !== 1 || copies[0].read !== read) {
          unknown.push(item);
          continue;
        }
        setChangeCode(item, "");
      }
      if (unknown.length) remaining.push({ group, items: unknown });
    }
    pending = remaining;
    if (!pending.length || ctx.cancelled || Date.now() >= deadline) break;
    if (!refreshed) {
      refreshed = true;
      const folders = pending.map(({ group }) => group.folder_id);
      for (let index = 0; index < folders.length && !ctx.cancelled; index += 8) {
        await refreshForChange(a, folders.slice(index, index + 8), deadline);
      }
    } else {
      await sleep(TUNING.movePollMs);
    }
  }
}

async function markGroups(a: ChangeArgs, read: boolean, ctx: Ctx): Promise<ChangeResult> {
  const helper = messenger.chamberlainMail;
  check(typeof helper?.setReadFlags === "function", "unsupported", "The bulk read flag helper is not installed.");
  const items: ChangeItem[] = [];
  const pending: FlagGroup[] = [];
  for (const group of a.items) {
    if (ctx.cancelled) fail("timeout", "The deadline passed while checking the folders.");
    let before;
    try {
      before = await readIdGroup(a, group);
    } catch (error) {
      const code = precheckCode(error) ?? "not_verified";
      items.push(...group.ids.map((id) => changeItem(group.folder_id, id, code)));
      continue;
    }
    const changed: BulkReadRef[] = [];
    const wanted: ChangeItem[] = [];
    for (const { id, header, item } of changeItems(group, before)) {
      items.push(item);
      if (!header) continue;
      wanted.push(item);
      if (header.read !== read) {
        setChangeCode(item, "not_verified");
        changed.push({ id: header.id, internetMessageId: id });
      }
    }
    if (!changed.length) continue;
    markMutation(ctx);
    try {
      await helper.setReadFlags(a.account_id, a.mailbox, group.folder_id, changed, read);
    } catch (error) {
      const code = precheckCode(error);
      // The native helper checks every reference before touching any flag.
      if (code) {
        for (const item of wanted) {
          if (!item.ok) item.code = code;
        }
        continue;
      }
      // An IMAP failure may arrive after changing a flag. Look again without writing.
    }
    for (const item of wanted) setChangeCode(item, "not_verified");
    pending.push({ group, items: wanted });
  }
  await verifyFlagGroups(a, pending, read, ctx);
  return changeResult(items, items.every((item) => item.code !== "not_verified"));
}

async function cmdChange(a: ChangeArgs, ctx: Ctx): Promise<ChangeResult> {
  await accountOf(a);
  // Bind every folder before issuing any changes, even when marking across several folders.
  for (const group of a.items) await folderOf(a, group.folder_id);
  if (a.move_to != null) return moveBatch(a, a.items[0], a.move_to, ctx);
  return markGroups(a, a.set_read!, ctx); // the host's ChangeArgs guarantee a read flag here
}

// --- composing ------------------------------------------------------------------------------

function uniqueIdentity(account: TbAccount, mailbox: string): TbIdentity {
  const found = account.identities.filter((i) => i.email.toLowerCase() === mailbox.toLowerCase());
  check(found.length === 1, "account_mismatch", "The sender identity is missing or ambiguous.");
  return found[0];
}

function composeDetails(r: ComposeArgs, identityId: string, sentFolder: TbFolder): any {
  // setComposeDetails cannot switch an existing HTML draft to plain text: supply both forms.
  return {
    overrideDefaultFcc: true,
    overrideDefaultFccFolder: sentFolder,
    additionalFccFolder: "",
    identityId,
    to: [...r.to],
    cc: [...r.cc],
    bcc: [...r.bcc],
    subject: r.subject,
    plainTextBody: r.body,
    body: "<pre>" + escapeHtml(r.body) + "</pre>",
    isPlainText: true,
    deliveryFormat: "plaintext",
    priority: r.high_importance ? "high" : "normal",
    customHeaders: [{ name: "X-Chamberlain-Write-Token", value: r.token }],
  };
}

async function sameAddresses(actual: string[], wanted: readonly string[]): Promise<boolean> {
  return sortedJson(await emailList(actual)) === sortedJson(wanted.map((v) => v.toLowerCase()).sort());
}

/** The composer must be exactly what was requested; a manual edit by the user fails here. */
async function verifyComposer(tabId: number, r: ComposeArgs, identityId: string, files: File[]): Promise<void> {
  const details = await messenger.compose.getComposeDetails(tabId);
  check(details.identityId === identityId, "changed", "The composer sender changed.");
  check(details.subject === r.subject, "changed", "The composer subject changed.");
  check(normaliseBody(details.plainTextBody) === normaliseBody(r.body), "changed", "The composer body changed.");
  for (const key of ["to", "cc", "bcc"] as const) {
    check(await sameAddresses(details[key] ?? [], r[key]), "changed", "The composer recipients changed.");
  }
  check(
    details.overrideDefaultFcc && details.overrideDefaultFccFolder?.id === r.sent_folder_id && !details.additionalFccFolder,
    "changed",
    "The Sent copy folder is not the bound one.",
  );
  check(details.priority === (r.high_importance ? "high" : "normal"), "changed", "The composer priority changed.");
  if (details.customHeaders) {
    check(details.customHeaders.some((h: any) => h.name === "X-Chamberlain-Write-Token" && h.value === r.token), "changed", "The composer token header changed.");
  }
  const actual = await messenger.compose.listAttachments(tabId);
  check(actual.length === files.length, "changed", "The composer attachments changed.");
  for (let i = 0; i < actual.length; i++) {
    const file: File = await messenger.compose.getAttachmentFile(actual[i].id);
    check(actual[i].name === files[i].name && file.size === files[i].size && (await sha256Hex(await file.arrayBuffer())) === (await sha256Hex(await files[i].arrayBuffer())), "changed", "The composer attachment bytes changed.");
  }
}

/** Prompts Thunderbird showed on this composer, which the helper answered at once. */
async function answered(tabId: number): Promise<string[]> {
  try {
    return (await messenger.chamberlainMail?.takePrompts(tabId))?.prompts ?? [];
  } catch {
    return [];
  }
}

async function closeQuietly(tabId: number, a: Bound): Promise<void> {
  try {
    await messenger.chamberlainMail?.closeComposer(tabId, a.account_id, a.mailbox);
  } catch {
    // Best effort: a composer that cannot be closed is reported by the next reconcile.
  }
}

async function cmdCompose(a: ComposeArgs, ctx: Ctx): Promise<ComposeResult> {
  const account = await accountOf(a);
  const identity = uniqueIdentity(account, a.mailbox);
  const helper = messenger.chamberlainMail;
  check(helper, "unsupported", "The composer helper is not installed.");
  const sentFolder = await folderOf(a, a.sent_folder_id);
  await folderOf(a, a.draft_folder_id);
  let existing: TbHeader | null = null;
  if (a.mode === "draft_update") {
    existing = await locateOne(a, a.draft!); // the host's ComposeArgs guarantee both fields here
    check(existing.folder?.id === a.draft_folder_id, "changed", "That message is not in the bound draft folder.");
    check((await rawHash(existing.id)) === a.expected_raw_sha256, "changed", "The draft changed since it was read.");
  }
  const files: File[] = [];
  for (const spec of a.attachments) {
    const file = ctx.files.get(spec.key);
    check(file && file.size === spec.size, "hash_mismatch", "An attachment was not fully transferred.");
    files.push(file);
  }
  const details = composeDetails(a, identity.id, sentFolder);
  let original: TbHeader | null = null;
  if (a.reply_to) original = await locateOne(a, a.reply_to);
  let tabId: number | null = null;
  try {
    if (existing) {
      const openingHash = await rawHash(existing.id);
      check(openingHash === a.expected_raw_sha256, "changed", "The draft changed before it was opened.");
      const opened = await helper.openDraft(a.account_id, a.mailbox, existing.id, existing.headerMessageId);
      tabId = opened.tab_id;
      check((await rawHash(existing.id)) === openingHash, "changed", "The draft changed while it was opened.");
    } else if (original) {
      tabId = (await messenger.compose.beginReply(original.id, "replyToSender", details)).id;
    } else {
      tabId = (await messenger.compose.beginNew(details)).id;
    }
    check(typeof tabId === "number", "not_verified", "The composer did not return a tab.");
    const openedFrom = existing ?? original;
    if (openedFrom) {
      if ((await messenger.messages.get(openedFrom.id)).read !== openedFrom.read) {
        markMutation(ctx);
        await messenger.messages.update(openedFrom.id, { read: openedFrom.read });
      }
      if (existing) await messenger.compose.setComposeDetails(tabId, details);
      for (const attachment of await messenger.compose.listAttachments(tabId)) await messenger.compose.removeAttachment(tabId, attachment.id);
    }
    // Autosave off and the bound Sent folder re-applied on every composer, reopened drafts and replies included.
    // A send never saves a draft, so its Drafts setting does not matter ("" skips that check).
    await helper.holdComposer(tabId, a.account_id, a.mailbox, a.mode === "send" ? "" : a.draft_folder_id, a.sent_folder_id);
    for (const file of files) await messenger.compose.addAttachment(tabId, { file });
    await verifyComposer(tabId, a, identity.id, files);
    markMutation(ctx);
    if (a.mode === "send") {
      await messenger.compose.sendMessage(tabId, { mode: "sendNow" });
      return { attachment_bytes_verified: true, dialogs: await answered(tabId), old_copies_remaining: 0 };
    }
    const saved = await messenger.compose.saveMessage(tabId, { mode: "draft" });
    return await finishDraft(a, tabId, saved, await answered(tabId));
  } catch (error) {
    if (tabId !== null && !ctx.mutationIssued) await closeQuietly(tabId, a);
    throw error;
  }
}

function unverifiedDraft(message: string): never {
  return fail("not_verified", message, "possible");
}

async function finishDraft(a: ComposeArgs, tabId: number, saved: { messages?: TbHeader[] }, dialogs: string[]): Promise<ComposeResult> {
  if (saved?.messages?.length !== 1) return unverifiedDraft("The draft save was not verified.");
  const returned = saved.messages[0];
  if (returned.folder?.accountId !== a.account_id || returned.folder?.id !== a.draft_folder_id) unverifiedDraft("The draft was saved in the wrong folder.");
  const expectedBody = await bodyDigest(a.body);
  const subject = normaliseSubject(a.subject);
  const replyId = a.reply_to?.internet_message_id;
  const attachments = sortedJson(a.attachments.map((spec) => spec.sha256).sort());
  const recipients = (["to", "cc", "bcc"] as const).map(
    (field) => [field, sortedJson(a[field].map((address) => address.toLowerCase()).sort())] as const,
  );
  let found: TbHeader | undefined;
  for (let attempt = 0; attempt < TUNING.saveAttempts && !found; attempt++) {
    try {
      const h: TbHeader = await messenger.messages.get(returned.id);
      const copy = await inspectContent(h, true);
      if (h.folder?.id === a.draft_folder_id && await isOwn(h, a.mailbox)
          && normaliseSubject(copy.subject ?? "") === subject
          && copy.body_sha256 === expectedBody
          && recipients.every(([field, want]) => sortedJson(copy[field]) === want)
          && sortedJson(copy.attachment_sha256) === attachments
          && copy.high_importance === a.high_importance
          && (!replyId || copy.in_reply_to.includes(replyId))) found = h;
    } catch (error) {
      if (!(error instanceof BridgeFailure) && !/not found/i.test(String((error as Error)?.message))) throw error;
    }
    if (!found && attempt < TUNING.saveAttempts - 1) await sleep(TUNING.savePollMs);
  }
  if (!found) return unverifiedDraft("The saved draft does not match what was requested.");
  const indexed = await indexedDraft(a, found);
  const raw = await rawHash(indexed.id);
  let remaining = 0;
  if (a.draft) {
    for (let attempt = 0; attempt < TUNING.saveAttempts; attempt++) {
      const old = await readExactCopies(a, a.draft.folder_id, a.draft.internet_message_id);
      if (!old.complete) return unverifiedDraft("The prior draft could not be checked completely.");
      remaining = old.messages.length;
      if (remaining === 0) break;
      await sleep(TUNING.savePollMs);
    }
  }
  if (!a.display_only) await closeQuietly(tabId, a);
  return { header: header(indexed), raw_sha256: raw, attachment_bytes_verified: true, old_copies_remaining: remaining, dialogs };
}

/**
 * Right after a save on IMAP (Gmail found live, 2026-11-25), Thunderbird returns its local
 * placeholder copy: no Message-ID in the header yet, and a new session id once the folder
 * syncs. The draft's handle needs that Message-ID, so take it from the stored message and
 * wait until the folder finds the draft by it.
 */
async function indexedDraft(a: ComposeArgs, saved: TbHeader): Promise<TbHeader> {
  const stableId = bare(saved.headerMessageId ?? "");
  let mid = stableId;
  if (!mid) {
    const full: TbPart = await messenger.messages.getFull(saved.id);
    mid = bare(String((full.headers ?? {})["message-id"]?.[0] ?? "").trim());
  }
  if (!mid) unverifiedDraft("The saved draft has no Message-ID.");
  for (let attempt = 0; attempt < TUNING.saveAttempts; attempt++) {
    // The WebExtension query also refreshes the placeholder emitted by the save callback.
    const copies = stableId
      ? await readExactCopies(a, a.draft_folder_id, mid)
      : await queryExact(a, a.draft_folder_id, mid);
    if (copies.complete && copies.messages.length === 1) return copies.messages[0];
    if (copies.messages.length > 1) unverifiedDraft("More than one copy of the saved draft is in the draft folder.");
    await sleep(TUNING.savePollMs);
  }
  return unverifiedDraft("The saved draft could not be found by its Message-ID.");
}

async function cmdDraftDiscard(a: DraftDiscardArgs, ctx: Ctx): Promise<DiscardResult> {
  await accountOf(a);
  const helper = messenger.chamberlainMail;
  check(helper, "unsupported", "The composer helper is not installed.");
  const draft = await locateOne(a, a.draft);
  check((await rawHash(draft.id)) === a.expected_raw_sha256, "changed", "The draft changed since it was read.");
  const found = await helper.findComposers(a.account_id, a.mailbox, draft.id, draft.headerMessageId);
  const tabs: number[] = found.tab_ids;
  for (const tabId of tabs) {
    const details = await messenger.compose.getComposeDetails(tabId);
    check(details.isModified === false, "changed", "The composer has unsaved edits by the user.");
  }
  for (const tabId of tabs) {
    markMutation(ctx);
    await helper.closeComposer(tabId, a.account_id, a.mailbox);
  }
  return { closed: tabs.length };
}

// --- copy observations (read-only) -----------------------------------------------------------

type ContentFacts = Pick<CopyFact, "subject" | "body_sha256" | "to" | "cc" | "bcc" |
  "attachment_sha256" | "high_importance" | "in_reply_to" | "token">;


/** Both duplicate policies use these observations. Only exact verification hashes attachments. */
async function inspectContent(h: TbHeader, includeHashes: boolean): Promise<ContentFacts> {
  const before: TbHeader = await messenger.messages.get(h.id);
  checkCopy(h, before);
  const full: TbPart = await messenger.messages.getFull(h.id);
  const headers = full.headers ?? {};
  const envelope = await messageEnvelope(full, before);
  const digest = await bodyDigest(await bodyText(full));
  const hashes: string[] = [];
  if (includeHashes) {
    for (const info of await messenger.messages.listAttachments(h.id)) {
      check(info.size <= MAX_ATTACHMENT_BYTES, "too_large", "An attachment is too large to verify.");
      const file: File = await messenger.messages.getAttachmentFile(h.id, info.partName);
      check(file.size <= MAX_ATTACHMENT_BYTES, "too_large", "An attachment is too large to verify.");
      hashes.push(await sha256Hex(await file.arrayBuffer()));
    }
  }
  checkCopy(before, await messenger.messages.get(h.id));
  return {
    ...envelope,
    body_sha256: digest,
    attachment_sha256: hashes.sort(),
    high_importance: ["high", "highest"].includes(before.priority ?? ""),
    token: (headers["x-chamberlain-write-token"] ?? []).join(" ").trim(),
  };
}

async function copyFact(a: Bound, scope: CopyScope, h: TbHeader, head: Header): Promise<CopyFact> {
  let content: ContentFacts = { to: [], cc: [], bcc: [], attachment_sha256: [], in_reply_to: "", token: "" };
  if (scope.ids == null) {
    check(await isOwn(h, a.mailbox), "changed", "The sender no longer matches the bound identity.");
    content = await inspectContent(h, scope.include_attachment_hashes);
  }
  if (!scope.inspect_draft) return { header: head, ...content };
  const raw = await rawHash(h.id);
  let composersOpen: number | undefined;
  if (scope.ids != null) {
    const helper = messenger.chamberlainMail;
    check(helper, "unsupported", "The composer helper is not installed.");
    const open = await helper.findComposers(a.account_id, a.mailbox, h.id, h.headerMessageId);
    composersOpen = open.tab_ids.length;
  }
  checkCopy(h, await messenger.messages.get(h.id));
  return { header: head, ...content, raw_sha256: raw, composers_open: composersOpen };
}

async function scanCopyScope(a: Bound, scope: CopyScope, folderId: string, outbox: boolean): Promise<{ messages: TbHeader[]; complete: boolean }> {
  if (scope.ids != null) {
    const scan = await lookupCopies(a, folderId, scope.ids);
    const since = scope.since_ms;
    const until = scope.until_ms;
    if (since == null || until == null) return scan;
    // Keep the prior date evidence for settlement without hiding any exact-ID copy.
    const within = scan.messages.every((message) => since <= whenMs(message) && whenMs(message) <= until);
    return { messages: scan.messages, complete: scan.complete && within };
  }
  await folderOf(a, folderId, outbox);
  const scan = await scanQuery(a, {
    folderId, foreign: outbox, sinceMs: scope.since_ms!,
    subject: scope.subject!, subjectContains: true, ownOnly: true, maxScan: 500,
  });
  const limit = 200;
  return { messages: scan.messages.slice(0, limit), complete: scan.complete && scan.messages.length <= limit };
}

async function cmdCopies(a: CopiesArgs): Promise<CopiesResult> {
  await accountOf(a);
  // Explicit folders always belong to the bound account. A separate selector discovers only
  // local Outboxes, whose headers and contents are filtered to the bound sender.
  const boundFolders = [...new Set(a.where.flatMap((scope) => scope.folder_id == null ? [] : [scope.folder_id]))];
  for (const folderId of boundFolders) await folderOf(a, folderId);
  let refreshVerified = a.refresh && boundFolders.length > 0;
  if (a.refresh) {
    for (let offset = 0; offset < boundFolders.length; offset += 8) {
      const folderIds = boundFolders.slice(offset, offset + 8);
      try {
        const result = await cmdRefresh({ account_id: a.account_id, mailbox: a.mailbox, folder_ids: folderIds, timeout_ms: 8000 });
        refreshVerified &&= result.provider_callback_verified;
      } catch {
        refreshVerified = false;
      }
    }
  }

  const scopes: CopyScopeResult[] = [];
  for (const scope of a.where) {
    const outbox = scope.folder_id == null;
    const copies: CopyFact[] = [];
    let complete = true;
    try {
      const folders = scope.folder_id == null ? (await localOutboxes()).map((f) => f.id) : [scope.folder_id];
      for (const folderId of folders) {
        const scan = await scanCopyScope(a, scope, folderId, outbox);
        complete &&= scan.complete;
        const headers = await scanHeaders(a, scan.messages, outbox);
        for (let i = 0; i < headers.length; i++) copies.push(await copyFact(a, scope, scan.messages[i], headers[i]));
      }
    } catch {
      complete = false;
    }
    scopes.push({ copies, complete });
  }
  return { scopes, refresh_verified: refreshVerified };
}
