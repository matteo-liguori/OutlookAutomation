// Read commands and the lookups the write commands build on. Reads never change remote
// state, the read flag included: every content read is bracketed by two flag samples.

const MAX_BODY_CHARS = 200_000;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

async function emailList(values: string[]): Promise<string[]> {
  const out: string[] = [];
  for (const text of values) {
    for (const address of await messenger.messengerUtilities.parseMailboxString(text)) {
      if (address.email) out.push(String(address.email).toLowerCase());
    }
  }
  return out.sort();
}

async function isOwn(h: TbHeader, mailbox: string): Promise<boolean> {
  const authors = await emailList([h.author]);
  return authors.length === 1 && authors[0] === mailbox.toLowerCase();
}

function whenMs(h: TbHeader): number {
  return h.date instanceof Date ? h.date.getTime() : Number(h.date);
}

function header(h: TbHeader): Header {
  const folder = h.folder ?? { id: "", accountId: "" };
  return {
    session_id: h.id,
    internet_message_id: bare(h.headerMessageId),
    subject: clip(h.subject, MAX_BODY_CHARS),
    date_ms: whenMs(h),
    author: clip(h.author, 2000),
    recipients: (h.recipients ?? []).map((v) => clip(v, 2000)),
    cc: (h.ccList ?? []).map((v) => clip(v, 2000)),
    bcc: (h.bccList ?? []).map((v) => clip(v, 2000)),
    read: h.read,
    folder_id: folder.id,
    folder_path: clip((folder as TbFolder).path, 1000),
    special_use: (folder as TbFolder).specialUse ?? [],
    has_attachments: false,
    priority: h.priority ?? "",
  };
}

interface Bound {
  account_id: string;
  mailbox: string;
}

async function accountOf(r: Bound): Promise<TbAccount> {
  const account = await messenger.accounts.get(r.account_id, true);
  if (!account || !account.identities.some((i) => i.email.toLowerCase() === r.mailbox.toLowerCase())) {
    fail("account_mismatch", "The Thunderbird account or sender identity is not the bound one.");
  }
  return account;
}

/** A folder of the bound account; the local Outbox is the one foreign folder allowed (read only). */
async function folderOf(r: Bound, id: string, allowOutbox = false): Promise<TbFolder> {
  let folder: TbFolder;
  try {
    folder = await messenger.folders.get(id);
  } catch {
    return fail("not_found", "That folder does not exist.");
  }
  if (folder.accountId === r.account_id) return folder;
  if (allowOutbox && (folder.specialUse ?? []).includes("outbox")) return folder;
  return fail("account_mismatch", "That folder belongs to another account.");
}

function walkFolders(root: TbFolder | undefined, visit: (f: TbFolder) => void): void {
  if (!root) return;
  visit(root);
  for (const child of root.subFolders ?? []) walkFolders(child, visit);
}

function folderInfo(f: TbFolder): FolderInfo {
  return { id: f.id, name: clip(f.name, 500), path: clip(f.path, 1000), account_id: f.accountId, special_use: f.specialUse ?? [] };
}

async function localOutboxes(): Promise<TbFolder[]> {
  const found: TbFolder[] = [];
  for (const local of await messenger.accounts.list(false)) {
    if (local.type !== "none") continue;
    const root = await messenger.accounts.get(local.id, true);
    walkFolders(root?.rootFolder, (f) => {
      if ((f.specialUse ?? []).includes("outbox")) found.push(f);
    });
  }
  return found;
}

interface Scan {
  messages: TbHeader[];
  complete: boolean;
  scanned: number;
}

interface ScanFilter {
  folderId?: string;
  foreign?: boolean;
  internetId?: string;
  text?: string;
  unread?: boolean;
  sinceMs?: number;
  untilMs?: number;
  subject?: string;
  subjectContains?: boolean;
  words?: string[];
  ownOnly?: boolean;
  maxScan: number;
}

/** Thunderbird's full-text search is too slow on big mailboxes: words match the headers. */
function hasEveryWord(h: TbHeader, words: string[]): boolean {
  const people = [h.author, ...(h.recipients ?? []), ...(h.ccList ?? [])].join(" ");
  // The raw subject: a search for "Re: ..." must find the reply (found live, 2026-11-25).
  const haystack = (String(h.subject ?? "") + " " + people).toLowerCase();
  return words.every((w) => haystack.includes(w));
}

/** Scan Thunderbird's query pages up to a bound; never silently truncate. */
async function scanQuery(r: Bound, filter: ScanFilter): Promise<Scan> {
  const q: any = { messagesPerPage: 50 };
  if (!filter.foreign) q.accountId = r.account_id;
  if (filter.folderId) q.folderId = filter.folderId;
  if (filter.internetId) q.headerMessageId = filter.internetId;
  if (filter.text) q.fullText = filter.text;
  if (filter.unread !== undefined) q.unread = filter.unread;
  if (filter.sinceMs !== undefined) q.fromDate = new Date(filter.sinceMs);
  if (filter.untilMs !== undefined) q.toDate = new Date(filter.untilMs);
  const wanted = filter.subject === undefined ? null : normaliseSubject(filter.subject);
  const out: TbHeader[] = [];
  let scanned = 0;
  let page: TbPage = await messenger.messages.query(q);
  for (;;) {
    scanned += page.messages.length;
    for (const h of page.messages) {
      if (filter.internetId && bare(h.headerMessageId) !== filter.internetId) continue;
      if (wanted !== null) {
        const subject = normaliseSubject(h.subject);
        if (filter.subjectContains ? !subject.includes(wanted) : subject !== wanted) continue;
      }
      if (filter.words && !hasEveryWord(h, filter.words)) continue;
      if (filter.ownOnly && !(await isOwn(h, r.mailbox))) continue;
      out.push(h);
    }
    if (!page.id) return { messages: out, complete: true, scanned };
    if (scanned >= filter.maxScan) {
      await messenger.messages.abortList(page.id);
      return { messages: out, complete: false, scanned };
    }
    page = await messenger.messages.continueList(page.id);
  }
}

function sortKey(h: TbHeader): string {
  return (h.folder?.id ?? "") + "\u0000" + bare(h.headerMessageId);
}

function newestFirst(a: TbHeader, b: TbHeader): number {
  const da = whenMs(a);
  const db = whenMs(b);
  if (da !== db) return db - da;
  const ka = sortKey(a);
  const kb = sortKey(b);
  return ka < kb ? 1 : ka > kb ? -1 : 0;
}

async function cmdQuery(a: QueryArgs): Promise<QueryResult> {
  await accountOf(a);
  let foreign = false;
  if (a.folder_id) {
    const folder = await folderOf(a, a.folder_id, a.own_only);
    foreign = folder.accountId !== a.account_id;
    if (foreign) check(a.own_only, "account_mismatch", "A foreign folder may be read only for the sender's own mail.");
  }
  const scan = await scanQuery(a, {
    folderId: a.folder_id ?? undefined,
    foreign,
    internetId: a.internet_message_id ? bare(a.internet_message_id) : undefined,
    text: a.text ?? undefined,
    unread: a.unread ?? undefined,
    sinceMs: a.since_ms ?? undefined,
    untilMs: a.until_ms ?? undefined,
    subject: a.subject ?? undefined,
    subjectContains: a.subject_contains,
    words: a.words ? a.words.toLowerCase().split(/\s+/).filter((w) => w.length > 0) : undefined,
    ownOnly: a.own_only,
    maxScan: a.max_scan,
  });
  let ordered = scan.messages.sort(newestFirst);
  if (a.after) {
    const after = a.after;
    ordered = ordered.filter((h) => whenMs(h) < after.date_ms || (whenMs(h) === after.date_ms && sortKey(h) < after.key));
  }
  const page = ordered.slice(0, a.limit);
  const last = page[page.length - 1];
  const more = ordered.length > a.limit;
  return {
    messages: page.map(header),
    more,
    complete: scan.complete,
    scanned: scan.scanned,
    next: more && last ? { date_ms: whenMs(last), key: sortKey(last) } : undefined,
  };
}

/** The messages of one folder with one Message-ID; stable identity, never a session integer. */
async function copiesIn(r: Bound, folderId: string, internetId: string, allowOutbox = false): Promise<Scan> {
  const folder = await folderOf(r, folderId, allowOutbox);
  const foreign = folder.accountId !== r.account_id;
  return scanQuery(r, { folderId, foreign, internetId: bare(internetId), maxScan: 5000, ownOnly: foreign });
}

async function locateOne(r: Bound, ref: MessageRef): Promise<TbHeader> {
  await accountOf(r);
  const found = await copiesIn(r, ref.folder_id, ref.internet_message_id);
  if (found.messages.length === 0) fail("not_found", "The message is not in that folder.");
  if (found.messages.length > 1) fail("ambiguous", "More than one copy of that message is in the folder.");
  if (!found.complete) fail("ambiguous", "The folder could not be scanned completely.");
  return found.messages[0];
}

async function cmdResolve(a: ResolveArgs): Promise<ResolveResult> {
  await accountOf(a);
  const found = await copiesIn(a, a.folder_id, a.internet_message_id);
  return { messages: found.messages.map(header), complete: found.complete };
}

async function htmlToText(html: string[]): Promise<string> {
  const parts = await Promise.all(html.map((body) => messenger.messengerUtilities.convertToPlainText(body, { flowed: false })));
  return parts.join("\n");
}

/** Readable body: plain text parts, else Thunderbird's own converter for HTML-only MIME. */
async function bodyText(full: TbPart): Promise<string> {
  const text: string[] = [];
  const html: string[] = [];
  const visit = (p: TbPart): void => {
    if (p.name) return;
    if (p.contentType === "text/plain" && p.body) text.push(p.body);
    if (p.contentType === "text/html" && p.body) html.push(p.body);
    for (const child of p.parts ?? []) visit(child);
  };
  visit(full);
  return normaliseBody(text.length ? text.join("\n") : await htmlToText(html));
}

async function rawHash(id: number): Promise<string> {
  const file: File = await messenger.messages.getRaw(id, { data_format: "File" });
  check(file.size <= MAX_ATTACHMENT_BYTES * 4, "too_large", "The message is too large to hash.");
  return sha256Hex(await file.arrayBuffer());
}

async function attachmentInfo(id: number): Promise<AttachmentInfo[]> {
  const list = await messenger.messages.listAttachments(id);
  return list.map((a: any) => ({ part_name: a.partName, name: clip(a.name, 500), size: a.size, content_type: a.contentType ?? "" }));
}

/** Fresh baseline immediately before the read, then require the flag unchanged (stale-flag incident). */
async function guardedFull(r: Bound, ref: MessageRef): Promise<{ full: TbPart; after: TbHeader }> {
  const before = await locateOne(r, ref);
  const full: TbPart = await messenger.messages.getFull(before.id);
  const after = await locateOne(r, ref);
  if (before.read !== after.read) fail("changed", "The unread flag changed during a read.");
  return { full, after };
}

async function cmdRead(a: ReadArgs): Promise<ReadResult> {
  const { full, after } = await guardedFull(a, { folder_id: a.folder_id, internet_message_id: a.internet_message_id });
  const headers = full.headers ?? {};
  const decoded = await messenger.messengerUtilities.decodeMimeHeader("subject", headers.subject ?? [after.subject]);
  return {
    header: header(after),
    subject: clip(decoded[0] ?? after.subject, MAX_BODY_CHARS),
    body: clip(await bodyText(full), MAX_BODY_CHARS),
    to: await emailList(after.recipients ?? []),
    cc: await emailList(after.ccList ?? []),
    bcc: await emailList(after.bccList?.length ? after.bccList : (headers.bcc ?? [])),
    attachments: await attachmentInfo(after.id),
    in_reply_to: (headers["in-reply-to"] ?? []).join(" ").trim(),
    references: (headers.references ?? []).join(" ").trim(),
    authentication_results: (headers["authentication-results"] ?? []).slice(0, 8).map((v: string) => clip(v, 2000)),
    raw_sha256: a.raw_hash ? await rawHash(after.id) : undefined,
    read_state_preserved: true,
  };
}

async function cmdAttachment(a: AttachmentArgs, id: string): Promise<{ result: AttachmentResult; chunks: ChunkFrame[] }> {
  const ref = { folder_id: a.folder_id, internet_message_id: a.internet_message_id };
  const before = await locateOne(a, ref);
  const known = await attachmentInfo(before.id);
  check(known.some((x) => x.part_name === a.part_name), "not_found", "That attachment is not on the message.");
  const file: File = await messenger.messages.getAttachmentFile(before.id, a.part_name);
  check(file.size <= a.max_bytes && file.size <= MAX_ATTACHMENT_BYTES, "too_large", "The attachment is over the size limit.");
  const bytes = new Uint8Array(await file.arrayBuffer());
  const after = await locateOne(a, ref);
  if (after.read !== before.read) fail("changed", "The unread flag changed during an attachment read.");
  const chunks: ChunkFrame[] = [];
  for (let offset = 0, index = 0; offset < bytes.length; offset += CHUNK_BYTES, index++) {
    const piece = bytes.subarray(offset, offset + CHUNK_BYTES);
    chunks.push({ t: "chunk", v: PROTOCOL_VERSION, id, name: a.part_name.slice(0, 64), index, data: bytesToBase64(piece), sha256: await sha256Hex(piece) });
  }
  return { result: { name: clip(file.name, 500), size: bytes.length, sha256: await sha256Hex(bytes), chunks: chunks.length, read_state_preserved: true }, chunks };
}

async function cmdFolders(a: FoldersArgs): Promise<FoldersResult> {
  const account = await accountOf(a);
  const out: FolderInfo[] = [];
  walkFolders(account.rootFolder, (f) => out.push(folderInfo(f)));
  if (a.include_outbox) for (const f of await localOutboxes()) out.push(folderInfo(f));
  return { folders: out };
}

async function cmdAccounts(): Promise<AccountsResult> {
  const list = await messenger.accounts.list(false);
  return {
    accounts: list.map((acc) => ({
      id: acc.id,
      type: acc.type,
      name: clip(acc.name, 500),
      identities: acc.identities.map((i) => ({ id: i.id, email: i.email, name: clip(i.name, 500) })),
    })),
  };
}

async function cmdRefresh(a: RefreshArgs): Promise<RefreshResult> {
  await accountOf(a);
  for (const id of a.folder_ids) await folderOf(a, id);
  const sync = messenger.chamberlainMail;
  check(sync, "unsupported", "The synchronisation helper is not installed.");
  const answer = await sync.syncFolders(a.account_id, a.mailbox, [...a.folder_ids], a.timeout_ms);
  const folders: FolderRefresh[] = (answer.folders ?? []).map((f: any) => ({ folder_id: String(f.folder_id), success: f.success === true, code: f.code ?? "" }));
  return { folders, provider_callback_verified: folders.length === a.folder_ids.length && folders.every((f) => f.success) };
}

async function cmdContacts(a: ContactsArgs): Promise<ContactsResult> {
  const needle = a.text.toLowerCase();
  const contacts: ContactInfo[] = [];
  let scanned = 0;
  let complete = true;
  for (const bookId of a.address_book_ids) {
    const book = await messenger.addressBooks.get(bookId, true);
    let seen = 0;
    for (const person of book.contacts ?? []) {
      if (++seen > a.max_items) {
        complete = false;
        break;
      }
      scanned++;
      const p = person.properties ?? {};
      const emails = [p.PrimaryEmail, p.SecondEmail].filter(Boolean).map(String);
      const name = p.DisplayName || [p.FirstName, p.LastName].filter(Boolean).join(" ");
      const hay = [p.DisplayName, p.FirstName, p.LastName, ...emails].filter(Boolean).join("\n").toLowerCase();
      if (hay.includes(needle)) contacts.push({ id: String(person.id), book_id: bookId, name: clip(name, 500), emails });
    }
  }
  contacts.sort((x, y) => (x.name + x.id < y.name + y.id ? -1 : 1));
  const window = contacts.slice(a.skip, a.skip + a.limit);
  return { contacts: window, more: contacts.length > a.skip + a.limit, complete, scanned };
}

async function cmdContactRead(a: ContactReadArgs): Promise<ContactReadResult> {
  let person: any;
  try {
    person = await messenger.addressBooks.contacts.get(a.contact_id);
  } catch {
    person = null;
  }
  check(person && String(person.parentId ?? a.address_book_id) === a.address_book_id, "not_found", "That contact is not in the address book.");
  const p = person.properties ?? {};
  const emails = [p.PrimaryEmail, p.SecondEmail].filter(Boolean).map(String);
  const name = p.DisplayName || [p.FirstName, p.LastName].filter(Boolean).join(" ");
  return { contact: { id: String(person.id), book_id: a.address_book_id, name: clip(name, 500), emails } };
}

async function cmdStatus(): Promise<StatusResult> {
  const info = await messenger.runtime.getBrowserInfo();
  return { addon_version: ADDON_VERSION, browser_version: info.version, write_busy: writeBusy, capabilities: advertisedCapabilities() };
}
