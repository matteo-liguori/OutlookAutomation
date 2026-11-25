// Write commands and their read-only reconcile twins.
// Each write performs one logical mutation, never retries, keeps no journal. The add-on
// reports facts; deciding what absence proves is the Python connector's job.

const TUNING = { movePollMs: 250, moveAttempts: 20, savePollMs: 250, saveAttempts: 24, batchCheckMs: 25_000, bulkReadFrom: 10 };

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

async function cmdMove(a: MoveArgs | TrashArgs, ctx: Ctx, trash: boolean): Promise<MoveResult> {
  await accountOf(a);
  const h = await locateOne(a, a.ref);
  const dest = await folderOf(a, a.destination_folder_id);
  const id = bare(a.ref.internet_message_id);
  check(h.folder?.id !== dest.id, "changed", "The message is already in that folder.");
  const before = await copiesIn(a, dest.id, id);
  check(before.complete, "ambiguous", "The destination could not be scanned completely.");
  if (!trash) check(before.messages.length === 0, "ambiguous", "The destination already holds this message.");
  const markRead = !trash && (a as MoveArgs).mark_read === true;
  const targetRead = markRead ? true : h.read;
  const beforeIds = before.messages.map((m) => m.id);
  const sourceId = h.folder?.id ?? a.ref.folder_id;
  markMutation(ctx);
  await messenger.messages.move([h.id], dest.id);
  for (let attempt = 0; attempt < TUNING.moveAttempts; attempt++) {
    const moved = await copiesIn(a, dest.id, id);
    const source = await copiesIn(a, sourceId, id);
    const candidates = moved.messages.filter((m) => !beforeIds.includes(m.id));
    const added = moved.messages.length === beforeIds.length + 1 && candidates.length === 1;
    // Gmail can show one logical message in Inbox and Sent, then merge those labels into an
    // existing Trash copy: no extra physical copy is needed for a recoverable delete.
    const merged = trash && !added && moved.messages.length > 0;
    if (moved.complete && (added || merged) && source.complete && source.messages.length === 0) {
      let after = added ? candidates[0] : moved.messages[0];
      if (added && after.read !== targetRead) {
        await messenger.messages.update(after.id, { read: targetRead });
        after = await messenger.messages.get(after.id);
      }
      const preserved = added ? after.read === targetRead : moved.messages.every((m) => m.read === h.read);
      if (!added && !merged) fail("not_verified", "The move could not be verified.", "possible");
      return {
        message: header(after),
        read_state_preserved: preserved,
        merged_existing_copy: merged,
        marked_read: markRead,
        original_read: h.read,
      };
    }
    await sleep(TUNING.movePollMs);
  }
  return fail("not_verified", "Exactly one copy at the destination and none at the source was not seen.", "possible");
}

interface DateWindow {
  sinceMs?: number;
  untilMs?: number;
}

/** Every message of one folder (within a date window) by bare Message-ID: one scan. */
async function byMessageId(r: Bound, folderId: string, window: DateWindow = {}): Promise<{ map: Map<string, TbHeader[]>; complete: boolean }> {
  await folderOf(r, folderId);
  const scan = await scanQuery(r, { folderId, maxScan: 200000, ...window });
  const map = new Map<string, TbHeader[]>();
  for (const h of scan.messages) {
    const id = bare(h.headerMessageId);
    const list = map.get(id);
    if (list) list.push(h);
    else map.set(id, [h]);
  }
  return { map, complete: scan.complete };
}

/** The date window a set of headers spans, with two days' margin for clocks and zones. */
function spanOf(headers: TbHeader[]): DateWindow {
  const dates = headers.map(whenMs);
  const day = 86_400_000;
  return { sinceMs: Math.min(...dates) - 2 * day, untilMs: Math.max(...dates) + 2 * day };
}

/** Many messages of one folder, filed or to Trash: find, move and verify them together.
 *  Folder reads cover only the dates the batch spans: a few hundred headers, never the whole
 *  Inbox and Trash on every round (found live, 2026-11-25: full reads overran the deadline). */
async function cmdMoveMany(a: MoveManyArgs, ctx: Ctx): Promise<MoveManyResult> {
  await accountOf(a);
  await folderOf(a, a.destination_folder_id);
  check(a.folder_id !== a.destination_folder_id, "changed", "The messages are already in that folder.");
  const source = await byMessageId(a, a.folder_id);
  check(source.complete, "ambiguous", "The folder could not be scanned completely.");
  const located = a.internet_message_ids.map((raw) => ({ raw, id: bare(raw), found: source.map.get(bare(raw)) ?? [] }));
  check(new Set(located.map((m) => m.id)).size === located.length, "bad_request", "The batch repeats a message.");
  const present = located.filter((m) => m.found.length === 1).map((m) => m.found[0]);
  if (present.length === 0) return fail("not_found", "None of the messages is in that folder.");
  const window = spanOf(present);
  // Filing leaves existing copies alone; Trash may merge into one without changing its flag.
  const before = await byMessageId(a, a.destination_folder_id, window);
  check(before.complete, "ambiguous", "The destination could not be scanned completely.");
  const items: MoveManyItem[] = [];
  const moving = new Map<string, TbHeader>();
  for (const m of located) {
    let code = m.found.length === 1 ? "" : m.found.length ? "ambiguous" : "not_found";
    if (!code && !a.trash && before.map.has(m.id)) code = "ambiguous";
    items.push({ internet_message_id: m.raw, ok: !code, code });
    if (!code) moving.set(m.id, m.found[0]);
  }
  const ids = [...moving.values()].map((h) => h.id);
  if (ids.length === 0) return fail("ambiguous", "Every message is already at the destination.");
  const helper = messenger.chamberlainMail;
  check(typeof helper?.setReadFlags === "function", "unsupported", "The bulk read flag helper is not installed.");
  markMutation(ctx);
  await messenger.messages.move(ids, a.destination_folder_id);
  const giveUpAt = Date.now() + TUNING.batchCheckMs;
  let flagsUpdated = false;
  const markRead = !a.trash && a.mark_read;
  while (Date.now() < giveUpAt) {
    const left = await byMessageId(a, a.folder_id, window);
    const there = await byMessageId(a, a.destination_folder_id, window);
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
      const targetRead = markRead || original.read;
      if (added[0].read !== targetRead) {
        (targetRead ? changedRead : changedUnread).push({ id: added[0].id, internetMessageId: id });
      }
      return true;
    });
    if (arrived) {
      const needsUpdate = changedRead.length > 0 || changedUnread.length > 0;
      if (!needsUpdate) return { items, verified: true, read_state_preserved: preserved, merged_existing_copy: merged };
      if (!flagsUpdated) {
        if (changedRead.length) {
          markMutation(ctx);
          await helper.setReadFlags(a.account_id, a.mailbox, a.destination_folder_id, changedRead, true);
        }
        if (changedUnread.length) {
          markMutation(ctx);
          await helper.setReadFlags(a.account_id, a.mailbox, a.destination_folder_id, changedUnread, false);
        }
        flagsUpdated = true;
        continue;
      }
      if (!markRead) return { items, verified: true, read_state_preserved: false, merged_existing_copy: merged };
    }
    await sleep(TUNING.movePollMs * 4);
  }
  // Not seen in time: the harness's own check (reconcile) settles it.
  return { items, verified: false, read_state_preserved: null, merged_existing_copy: false };
}

/** One read per folder that holds many of the refs; a handful are found one by one. */
async function foldersRead(r: Bound, refs: readonly MessageRef[]): Promise<Map<string, Map<string, TbHeader[]>>> {
  const counts = new Map<string, number>();
  for (const ref of refs) counts.set(ref.folder_id, (counts.get(ref.folder_id) ?? 0) + 1);
  const read = new Map<string, Map<string, TbHeader[]>>();
  for (const [folderId, n] of counts) {
    if (n < TUNING.bulkReadFrom) continue;
    const scan = await byMessageId(r, folderId);
    if (scan.complete) read.set(folderId, scan.map);
  }
  return read;
}

async function findOne(r: Bound, ref: MessageRef, read: Map<string, Map<string, TbHeader[]>>): Promise<TbHeader> {
  const folder = read.get(ref.folder_id);
  if (!folder) return locateOne(r, ref);
  const found = folder.get(bare(ref.internet_message_id)) ?? [];
  if (found.length === 0) fail("not_found", "The message is not in that folder.");
  if (found.length > 1) fail("ambiguous", "More than one copy of that message is in the folder.");
  return found[0];
}

async function cmdMarkRead(a: MarkReadArgs, ctx: Ctx): Promise<MarkReadResult> {
  await accountOf(a);
  const items: MarkReadItem[] = [];
  const read = await foldersRead(a, a.refs);
  for (const ref of a.refs) {
    try {
      const h = await findOne(a, ref, read);
      if (h.read !== a.read) {
        markMutation(ctx);
        await messenger.messages.update(h.id, { read: a.read });
        const after = await messenger.messages.get(h.id);
        if (after.read !== a.read) fail("not_verified", "The flag change was not verified.", "possible");
      }
      items.push({ ref, ok: true, was_read: h.read, code: "" });
    } catch (error) {
      const code = error instanceof BridgeFailure ? error.code : "internal";
      items.push({ ref, ok: false, code });
    }
  }
  return { items };
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

async function mailRaw(id: number): Promise<string> {
  return rawHash(id);
}

async function cmdCompose(a: ComposeArgs, ctx: Ctx, kind: "send" | "draft_save" | "draft_update"): Promise<SendResult | DraftResult> {
  const account = await accountOf(a);
  const identity = uniqueIdentity(account, a.mailbox);
  const helper = messenger.chamberlainMail;
  check(helper, "unsupported", "The composer helper is not installed.");
  const sentFolder = await folderOf(a, a.sent_folder_id);
  await folderOf(a, a.draft_folder_id);
  const update = kind === "draft_update" ? (a as DraftUpdateArgs) : null;
  let existing: TbHeader | null = null;
  if (update) {
    existing = await locateOne(a, update.draft);
    check(existing.folder?.id === a.draft_folder_id, "changed", "That message is not in the bound draft folder.");
    check((await mailRaw(existing.id)) === update.expected_raw_sha256, "changed", "The draft changed since it was read.");
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
      const openingHash = await mailRaw(existing.id);
      const opened = await helper.openDraft(a.account_id, a.mailbox, existing.id, existing.headerMessageId);
      tabId = opened.tab_id;
      check((await mailRaw(existing.id)) === openingHash, "changed", "The draft changed while it was opened.");
      if ((await messenger.messages.get(existing.id)).read !== existing.read) {
        markMutation(ctx);
        await messenger.messages.update(existing.id, { read: existing.read });
      }
      await messenger.compose.setComposeDetails(tabId, details);
      for (const attachment of await messenger.compose.listAttachments(tabId)) await messenger.compose.removeAttachment(tabId, attachment.id);
    } else if (original) {
      const tab = await messenger.compose.beginReply(original.id, "replyToSender", details);
      tabId = tab.id;
      const now = await messenger.messages.get(original.id);
      if (now.read !== original.read) {
        markMutation(ctx);
        await messenger.messages.update(original.id, { read: original.read });
      }
      for (const attachment of await messenger.compose.listAttachments(tabId)) await messenger.compose.removeAttachment(tabId, attachment.id);
    } else {
      tabId = (await messenger.compose.beginNew(details)).id;
    }
    const open: number = tabId as number;
    // Autosave off and the bound Sent folder re-applied on every composer, reopened drafts and replies included.
    // A send never saves a draft, so its Drafts setting does not matter ("" skips that check).
    await helper.holdComposer(open, a.account_id, a.mailbox, kind === "send" ? "" : a.draft_folder_id, a.sent_folder_id);
    for (const file of files) await messenger.compose.addAttachment(open, { file });
    await verifyComposer(open, a, identity.id, files);
    if (kind === "send") {
      markMutation(ctx);
      await messenger.compose.sendMessage(open, { mode: "sendNow" });
      return { submitted: true, attachment_bytes_verified: true, dialogs: await answered(open) };
    }
    markMutation(ctx);
    const saved = await messenger.compose.saveMessage(open, { mode: "draft" });
    const dialogs = await answered(open);
    return { ...(await finishDraft(a, ctx, open, saved, update, existing)), dialogs };
  } catch (error) {
    if (tabId !== null && !ctx.mutationIssued) await closeQuietly(tabId, a);
    throw error;
  }
}

async function finishDraft(a: ComposeArgs, ctx: Ctx, tabId: number, saved: any, update: DraftUpdateArgs | null, existing: TbHeader | null): Promise<DraftResult> {
  const fail2 = (message: string): never => fail("not_verified", message, "possible");
  if (saved?.messages?.length !== 1) fail2("The draft save was not verified.");
  const returned: TbHeader = saved.messages[0];
  if (returned.folder?.accountId !== a.account_id || returned.folder?.id !== a.draft_folder_id) fail2("The draft was saved in the wrong folder.");
  const fp = await fingerprintOf(a);
  let found: { header: TbHeader; match: Match } | null = null;
  for (let attempt = 0; attempt < TUNING.saveAttempts && !found; attempt++) {
    try {
      const h: TbHeader = await messenger.messages.get(returned.id);
      const match = await inspectCopy(a, h, fp, "drafts", a.sent_folder_id);
      if (match.exact) found = { header: h, match };
    } catch (error) {
      if (!(error instanceof BridgeFailure) && !/not found/i.test(String((error as Error)?.message))) throw error;
    }
    if (!found && attempt < TUNING.saveAttempts - 1) await sleep(TUNING.savePollMs);
  }
  if (!found) fail2("The saved draft does not match what was requested.");
  const ok = await indexedDraft(a, (found as { header: TbHeader; match: Match }).header, fail2);
  const raw = await rawHash(ok.header.id);
  let remaining = 0;
  if (update && existing) {
    for (let attempt = 0; attempt < TUNING.saveAttempts; attempt++) {
      remaining = (await copiesIn(a, update.draft.folder_id, update.draft.internet_message_id)).messages.length;
      if (remaining === 0) break;
      await sleep(TUNING.savePollMs);
    }
  }
  if (!a.display_only) await closeQuietly(tabId, a);
  return { header: header(ok.header), raw_sha256: raw, attachment_bytes_verified: true, old_copies_remaining: remaining, dialogs: [] };
}

/**
 * Right after a save on IMAP (Gmail found live, 2026-11-25), Thunderbird returns its local
 * placeholder copy: no Message-ID in the header yet, and a new session id once the folder
 * syncs. The draft's handle needs that Message-ID, so take it from the stored message and
 * wait until the folder finds the draft by it.
 */
async function indexedDraft(a: ComposeArgs, saved: TbHeader, fail2: (message: string) => never): Promise<{ header: TbHeader }> {
  let mid = bare(saved.headerMessageId ?? "");
  if (!mid) {
    const full: TbPart = await messenger.messages.getFull(saved.id);
    mid = bare(String((full.headers ?? {})["message-id"]?.[0] ?? "").trim());
  }
  if (!mid) fail2("The saved draft has no Message-ID.");
  for (let attempt = 0; attempt < TUNING.saveAttempts; attempt++) {
    const copies = (await copiesIn(a, a.draft_folder_id, mid)).messages;
    if (copies.length === 1) return { header: copies[0] };
    if (copies.length > 1) fail2("More than one copy of the saved draft is in the draft folder.");
    await sleep(TUNING.savePollMs);
  }
  return fail2("The saved draft could not be found by its Message-ID.");
}

async function cmdDraftDiscard(a: DraftDiscardArgs): Promise<DiscardResult> {
  await accountOf(a);
  const helper = messenger.chamberlainMail;
  check(helper, "unsupported", "The composer helper is not installed.");
  const draft = await locateOne(a, a.draft);
  check((await mailRaw(draft.id)) === a.expected_raw_sha256, "changed", "The draft changed since it was read.");
  const found = await helper.findComposers(a.account_id, a.mailbox, draft.id, draft.headerMessageId);
  let closed = 0;
  for (const tabId of found.tab_ids as number[]) {
    const details = await messenger.compose.getComposeDetails(tabId);
    check(details.isModified === false, "changed", "The composer has unsaved edits by the user.");
    await helper.closeComposer(tabId, a.account_id, a.mailbox);
    closed++;
  }
  // Closing a composer never deletes saved mail (R8): there is no mutation of the mailbox itself.
  return { closed, saved_mail_deleted: false };
}

// --- reconcile (read-only) -------------------------------------------------------------------

async function fingerprintOf(a: ComposeArgs): Promise<Fingerprint> {
  const bodyDigestSync = await bodyDigest(a.body);
  return {
    sender: a.mailbox,
    to: [...a.to],
    cc: [...a.cc],
    bcc: [...a.bcc],
    subject: a.subject,
    body_sha256: bodyDigestSync,
    attachment_sha256: a.attachments.map((x) => x.sha256),
    high_importance: a.high_importance,
    in_reply_to: a.reply_to ? a.reply_to.internet_message_id : "",
    token: a.token,
  };
}

/** Compare one physical copy with a fingerprint. The flag is sampled before and after. */
async function inspectCopy(r: Bound, h: TbHeader, fp: Fingerprint, where: Match["where"], sentFolderId: string): Promise<Match> {
  const baseline: TbHeader = await messenger.messages.get(h.id);
  const full: TbPart = await messenger.messages.getFull(h.id);
  const after: TbHeader = await messenger.messages.get(h.id);
  if (baseline.read !== after.read) fail("changed", "The unread flag changed during a read.");
  const headers = full.headers ?? {};
  const decoded = await messenger.messengerUtilities.decodeMimeHeader("subject", headers.subject ?? [after.subject]);
  const subjectOk = normaliseSubject(decoded[0] ?? after.subject) === normaliseSubject(fp.subject);
  const body = await bodyText(full);
  const bodyOk = (await bodyDigest(body)) === fp.body_sha256;
  const to = await emailList(after.recipients ?? []);
  const cc = await emailList(after.ccList ?? []);
  const bcc = await emailList(after.bccList?.length ? after.bccList : (headers.bcc ?? []));
  const want = (v: readonly string[]) => sortedJson(v.map((x) => x.toLowerCase()).sort());
  const envelopeOk = sortedJson(to) === want(fp.to) && sortedJson(cc) === want(fp.cc) && (where === "inbox" || sortedJson(bcc) === want(fp.bcc));
  const infos = await messenger.messages.listAttachments(after.id);
  const hashes: string[] = [];
  for (const info of infos) {
    const file: File = await messenger.messages.getAttachmentFile(after.id, info.partName);
    hashes.push(await sha256Hex(await file.arrayBuffer()));
  }
  const attachmentsOk = sortedJson(hashes.sort()) === sortedJson([...fp.attachment_sha256].sort());
  const priorityOk = ["high", "highest"].includes(after.priority ?? "") === fp.high_importance;
  const replyHeader = (headers["in-reply-to"] ?? []).join(" ");
  const replyOk = !fp.in_reply_to || replyHeader.includes(fp.in_reply_to);
  const own = await isOwn(after, r.mailbox);
  const tokenHeader = (headers["x-chamberlain-write-token"] ?? []).join(" ").trim();
  const draftsOrSent = where === "drafts" || where === "sent";
  return {
    header: header(after),
    where,
    exact: own && subjectOk && bodyOk && envelopeOk && attachmentsOk && priorityOk && replyOk,
    token_match: tokenHeader !== "" && tokenHeader === fp.token,
    raw_sha256: where === "drafts" ? await rawHash(after.id) : undefined,
    internet_message_id_matches_token: bare(after.headerMessageId) === bare(fp.token) && draftsOrSent && sentFolderId !== "",
  };
}

async function scanCompose(a: ReconcileComposeArgs, includeInbox: boolean): Promise<{ matches: Match[]; complete: boolean; errors: string[] }> {
  const folders: { id: string; where: Match["where"]; foreign: boolean }[] = [
    { id: a.sent_folder_id, where: "sent", foreign: false },
    { id: a.draft_folder_id, where: "drafts", foreign: false },
  ];
  if (includeInbox && a.inbox_folder_id) folders.push({ id: a.inbox_folder_id, where: "inbox", foreign: false });
  if (includeInbox) for (const f of await localOutboxes()) folders.push({ id: f.id, where: "outbox", foreign: true });
  const matches: Match[] = [];
  const errors: string[] = [];
  let complete = true;
  for (const folder of folders) {
    try {
      await folderOf(a, folder.id, folder.foreign);
      const scan = await scanQuery(a, { folderId: folder.id, foreign: folder.foreign, sinceMs: a.since_ms, subject: a.fingerprint.subject, subjectContains: true, ownOnly: true, maxScan: 500 });
      if (!scan.complete) {
        complete = false;
        errors.push("scan limit: " + folder.id);
      }
      for (const h of scan.messages) matches.push(await inspectCopy(a, h, a.fingerprint, folder.where, a.sent_folder_id));
    } catch (error) {
      complete = false;
      errors.push(folder.where + ": " + String((error as Error)?.message ?? error).slice(0, 120));
    }
  }
  return { matches, complete, errors };
}

async function maybeRefresh(a: Bound, wanted: boolean, ids: string[]): Promise<{ refreshed: boolean; verified: boolean; error?: string }> {
  if (!wanted) return { refreshed: false, verified: false };
  try {
    const done = await cmdRefresh({ account_id: a.account_id, mailbox: a.mailbox, folder_ids: ids, timeout_ms: 8000 });
    return { refreshed: true, verified: done.provider_callback_verified };
  } catch (error) {
    return { refreshed: true, verified: false, error: String((error as Error)?.message ?? error).slice(0, 120) };
  }
}

async function cmdReconcileCompose(a: ReconcileComposeArgs, kind: "send" | "draft_save" | "draft_update"): Promise<ReconcileComposeResult> {
  await accountOf(a);
  const ids = [a.sent_folder_id, a.draft_folder_id];
  if (kind === "send" && a.inbox_folder_id) ids.push(a.inbox_folder_id);
  const refresh = await maybeRefresh(a, a.refresh, ids);
  const scan = await scanCompose(a, kind === "send");
  const errors = refresh.error ? [refresh.error, ...scan.errors] : scan.errors;
  let oldCopies: number | undefined;
  if (kind === "draft_update") {
    const u = a as ReconcileDraftUpdateArgs;
    oldCopies = (await copiesIn(a, u.old.folder_id, u.old.internet_message_id)).messages.length;
  }
  return { matches: scan.matches, complete: scan.complete, refreshed: refresh.verified, errors, old_copies: oldCopies };
}

async function cmdReconcileDiscard(a: ReconcileDraftDiscardArgs): Promise<ReconcileDiscardResult> {
  await accountOf(a);
  const found = await copiesIn(a, a.draft.folder_id, a.draft.internet_message_id);
  let hash: string | undefined;
  let composers = 0;
  if (found.messages.length === 1) {
    hash = await rawHash(found.messages[0].id);
    const open = await messenger.chamberlainMail?.findComposers(a.account_id, a.mailbox, found.messages[0].id, found.messages[0].headerMessageId);
    composers = open ? open.tab_ids.length : 0;
  }
  return { copies: found.messages.length, raw_sha256: hash, composers_open: composers };
}

async function cmdReconcileMove(a: ReconcileMoveArgs): Promise<ReconcileMoveResult> {
  await accountOf(a);
  const refresh = await maybeRefresh(a, a.refresh, [...new Set([a.ref.folder_id, a.destination_folder_id])]);
  const id = bare(a.ref.internet_message_id);
  const source = await copiesIn(a, a.ref.folder_id, id);
  const destination = await copiesIn(a, a.destination_folder_id, id);
  return {
    source: { folder_id: a.ref.folder_id, messages: source.messages.map((h) => header(h)), complete: source.complete },
    destination: { folder_id: a.destination_folder_id, messages: destination.messages.map((h) => header(h)), complete: destination.complete },
    refreshed: refresh.refreshed,
    refresh_verified: refresh.verified,
  };
}

async function cmdReconcileMoveMany(a: ReconcileMoveManyArgs): Promise<ReconcileMoveManyResult> {
  await accountOf(a);
  const refresh = await maybeRefresh(a, a.refresh, [a.folder_id, a.destination_folder_id]);
  const source = await byMessageId(a, a.folder_id);
  const destination = await byMessageId(a, a.destination_folder_id);
  const items = a.internet_message_ids.map((raw) => {
    const id = bare(raw);
    const there = destination.map.get(id) ?? [];
    return {
      internet_message_id: raw,
      source: (source.map.get(id) ?? []).length,
      destination: there.length,
      unread: there.filter((h) => !h.read).length,
    };
  });
  return {
    items,
    complete: source.complete && destination.complete,
    refreshed: refresh.refreshed,
    refresh_verified: refresh.verified,
  };
}

async function cmdReconcileMarkRead(a: ReconcileMarkReadArgs): Promise<ReconcileMarkReadResult> {
  await accountOf(a);
  const refresh = await maybeRefresh(a, a.refresh, [...new Set(a.refs.map((r) => r.folder_id))].slice(0, 8));
  const items: ReconcileReadItem[] = [];
  const read = await foldersRead(a, a.refs);
  for (const ref of a.refs) {
    const folder = read.get(ref.folder_id);
    const messages = folder
      ? (folder.get(bare(ref.internet_message_id)) ?? [])
      : (await copiesIn(a, ref.folder_id, ref.internet_message_id)).messages;
    items.push({ ref, copies: messages.length, read: messages.length === 1 ? messages[0].read : undefined });
  }
  return { items, refreshed: refresh.refreshed, refresh_verified: refresh.verified };
}
