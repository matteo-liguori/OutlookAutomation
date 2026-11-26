// A scripted Thunderbird: the `messenger` API subset the add-on uses, over an in-memory mailbox.
// Synthetic data only. Faults are armed through `world.faults` (see harness.mjs).
import { File } from "node:buffer";

export const OWNER = "owner@example.org";
export const ACCOUNT = "account1";

const BASE_DATE = Date.parse("2026-01-05T09:00:00Z");

function bare(id) {
  return String(id).replace(/^<|>$/g, "");
}

export function createWorld() {
  const world = {
    tick: 0,
    nextId: 1,
    nextTab: 1,
    nextGenerated: 1,
    folders: new Map(),
    messages: [],
    composers: new Map(),
    lists: new Map(),
    latches: [],
    faults: { slow: 0, duplicate: 0, stale: false, staleCallback: false, flipReadOnFull: false, hold: 0, slowCalendar: 0 },
    counters: { move: 0, send: 0, save: 0, update: 0, full: 0 },
    books: [],
    calendars: new Map(),
    calendarOffline: false,
    calendarLog: [],
  };

  const folder = (id, accountId, name, specialUse = []) => {
    const f = { id, accountId, name, path: "/" + name, specialUse };
    world.folders.set(id, f);
    return f;
  };
  folder(ACCOUNT + "://INBOX", ACCOUNT, "INBOX", ["inbox"]);
  folder(ACCOUNT + "://Sent", ACCOUNT, "Sent", ["sent"]);
  folder(ACCOUNT + "://Drafts", ACCOUNT, "Drafts", ["drafts"]);
  folder(ACCOUNT + "://Trash", ACCOUNT, "Trash", ["trash"]);
  folder(ACCOUNT + "://Archive", ACCOUNT, "Archive", ["archive"]);
  folder(ACCOUNT + "://Filed", ACCOUNT, "Filed");
  folder("local://Outbox", "local", "Outbox", ["outbox"]);

  const clone = (value) => structuredClone(value);
  const headerOf = (m) => ({
    id: m.id,
    headerMessageId: m.headerMessageId,
    subject: m.subject,
    date: new Date(m.date),
    author: m.author,
    recipients: [...m.recipients],
    ccList: [...m.ccList],
    bccList: [...m.bccList],
    read: m.read,
    priority: m.priority,
    folder: clone(world.folders.get(m.folderId)),
  });
  const byId = (id) => {
    const m = world.messages.find((x) => x.id === id);
    if (!m) throw new Error("Message not found: " + id);
    return m;
  };

  world.add = (spec) => {
    world.tick += 1;
    const m = {
      id: world.nextId++,
      headerMessageId: bare(spec.headerMessageId ?? "gen-" + world.nextGenerated++ + "@fake.example"),
      folderId: spec.folderId,
      subject: spec.subject ?? "Hello",
      author: spec.author ?? "Alice <alice@example.org>",
      recipients: spec.recipients ?? [OWNER],
      ccList: spec.ccList ?? [],
      bccList: spec.bccList ?? [],
      date: spec.date ?? BASE_DATE + world.tick * 1000,
      read: spec.read ?? false,
      priority: spec.priority ?? "normal",
      body: spec.body ?? "Body text.",
      attachments: spec.attachments ?? [],
      headers: spec.headers ?? {},
    };
    world.messages.push(m);
    return m;
  };

  const rootOf = (accountId) => {
    const make = (f) => ({ ...clone(f), subFolders: [] });
    const root = { id: accountId + "://", accountId, subFolders: [] };
    for (const f of world.folders.values()) if (f.accountId === accountId) root.subFolders.push(make(f));
    return root;
  };
  const accounts = [
    { id: ACCOUNT, type: "imap", name: "Owner", identities: [{ id: "id1", email: OWNER, name: "Owner" }] },
    { id: "local", type: "none", name: "Local Folders", identities: [] },
  ];

  const matches = (m, q) => {
    const f = world.folders.get(m.folderId);
    if (q.accountId && f.accountId !== q.accountId) return false;
    if (q.folderId && m.folderId !== q.folderId) return false;
    if (q.headerMessageId && m.headerMessageId !== bare(q.headerMessageId)) return false;
    if (q.unread !== undefined && m.read === q.unread) return false;
    if (q.fullText && !(m.subject + "\n" + m.body).toLowerCase().includes(String(q.fullText).toLowerCase())) return false;
    if (q.fromDate && m.date < q.fromDate.getTime()) return false;
    if (q.toDate && m.date > q.toDate.getTime()) return false;
    return true;
  };

  const full = (m) => {
    world.counters.full += 1;
    if (world.faults.flipReadOnFull) m.read = !m.read;
    const headers = { subject: [m.subject], ...Object.fromEntries(Object.entries(m.headers).map(([k, v]) => [k, [...v]])) };
    if (m.bccList.length) headers.bcc = [...m.bccList];
    return {
      contentType: "multipart/mixed",
      headers,
      parts: [
        { contentType: "text/plain", body: m.body },
        ...m.attachments.map((a, i) => ({ name: a.name, partName: "1." + (i + 2), contentType: a.contentType })),
      ],
    };
  };

  const rawFile = (m) =>
    new File([JSON.stringify({ s: m.subject, b: m.body, to: m.recipients, cc: m.ccList, bcc: m.bccList, a: m.attachments.map((x) => [x.name, x.bytes.length]), h: m.headers, p: m.priority })], "message.eml");

  const holdLatch = () => new Promise((resolve) => world.latches.push(resolve));

  const move = async (ids, destination) => {
    world.counters.move += 1;
    if (world.faults.slow > 0) {
      world.faults.slow -= 1;
      await holdLatch();
    }
    for (const id of ids) {
      const m = byId(id);
      const dup = world.faults.duplicate > 0;
      if (dup) {
        world.faults.duplicate -= 1;
        world.add({ ...m, folderId: m.folderId, read: m.read, attachments: m.attachments, headers: m.headers, headerMessageId: m.headerMessageId, date: m.date });
      }
      const existing = world.messages.find((x) => x !== m && x.folderId === destination && x.headerMessageId === m.headerMessageId);
      if (world.faults.merge && existing) {
        // Gmail merges labels into the copy already in Trash; no extra physical copy appears.
        world.messages = world.messages.filter((x) => x !== m);
        continue;
      }
      // An IMAP move yields a new physical copy (new session id) at the destination.
      m.id = world.nextId++;
      m.folderId = destination;
      if (world.faults.flipReadOnMove) m.read = !m.read;
    }
  };

  const tabState = (id) => {
    const c = world.composers.get(id);
    if (!c) throw new Error("No such tab " + id);
    return c;
  };

  const compose = {
    async beginNew(details) {
      const id = world.nextTab++;
      world.composers.set(id, { id, details: clone({ ...details, body: undefined }), files: [], held: false, modified: false, from: null });
      return { id };
    },
    async beginReply(messageId, type, details) {
      const original = byId(messageId);
      original.read = true; // Thunderbird marks the replied-to message read (incident)
      const id = world.nextTab++;
      world.composers.set(id, { id, details: clone({ ...details, body: undefined }), files: [], held: false, modified: false, from: null, replyTo: original.headerMessageId });
      return { id };
    },
    async setComposeDetails(tabId, details) {
      tabState(tabId).details = clone({ ...details, body: undefined });
    },
    async getComposeDetails(tabId) {
      const c = tabState(tabId);
      return { ...clone(c.details), isModified: c.modified };
    },
    async listAttachments(tabId) {
      return tabState(tabId).files.map((f, i) => ({ id: i + 1, name: f.name }));
    },
    async addAttachment(tabId, { file }) {
      tabState(tabId).files.push(file);
    },
    async removeAttachment(tabId, attachmentId) {
      tabState(tabId).files.splice(attachmentId - 1, 1);
    },
    async getAttachmentFile(attachmentId) {
      for (const c of world.composers.values()) if (c.files[attachmentId - 1]) return c.files[attachmentId - 1];
      throw new Error("no attachment");
    },
    async saveMessage(tabId) {
      world.counters.save += 1;
      if (world.faults.slow > 0) {
        world.faults.slow -= 1;
        await holdLatch();
      }
      const c = tabState(tabId);
      if (c.existingId) world.messages = world.messages.filter((m) => m.id !== c.existingId);
      const drafts = [...world.folders.values()].find((f) => f.accountId === ACCOUNT && f.specialUse.includes("drafts"));
      const m = world.add({
        folderId: drafts.id,
        subject: c.details.subject,
        author: OWNER,
        recipients: [...(c.details.to ?? [])],
        ccList: [...(c.details.cc ?? [])],
        bccList: [...(c.details.bcc ?? [])],
        body: c.details.plainTextBody,
        read: true,
        priority: c.details.priority,
        attachments: await Promise.all(c.files.map(async (f) => ({ name: f.name, contentType: f.type, bytes: new Uint8Array(await f.arrayBuffer()) }))),
        headers: Object.fromEntries((c.details.customHeaders ?? []).map((h) => [h.name.toLowerCase(), [h.value]]).concat(c.replyTo ? [["in-reply-to", ["<" + c.replyTo + ">"]]] : [])),
      });
      c.existingId = m.id;
      if (world.faults.placeholderDraft) {
        // Like Thunderbird on IMAP: a placeholder without a Message-ID; the next folder query
        // "syncs" it into the real copy under a new session id.
        const real = m.headerMessageId;
        m.headers["message-id"] = ["<" + real + ">"];
        m.headerMessageId = "";
        world.pendingSync = () => {
          m.headerMessageId = real;
          m.id = world.nextId++;
          c.existingId = m.id;
        };
      }
      const header = headerOf(m);
      return { messages: [world.faults.staleCallback ? { ...header, headerMessageId: "stale-callback", read: !header.read } : header] };
    },
    async sendMessage(tabId) {
      world.counters.send += 1;
      if (world.faults.slow > 0) {
        world.faults.slow -= 1;
        await holdLatch();
      }
      const c = tabState(tabId);
      const sentId = c.details.overrideDefaultFccFolder?.id;
      const spec = {
        subject: c.details.subject,
        author: OWNER,
        recipients: [...(c.details.to ?? [])],
        ccList: [...(c.details.cc ?? [])],
        bccList: [...(c.details.bcc ?? [])],
        body: c.details.plainTextBody,
        read: true,
        priority: c.details.priority,
        attachments: await Promise.all(c.files.map(async (f) => ({ name: f.name, contentType: f.type, bytes: new Uint8Array(await f.arrayBuffer()) }))),
        headers: Object.fromEntries((c.details.customHeaders ?? []).map((h) => [h.name.toLowerCase(), [h.value]]).concat(c.replyTo ? [["in-reply-to", ["<" + c.replyTo + ">"]]] : [])),
      };
      const generated = "sent-" + world.nextGenerated++ + "@fake.example";
      if (!world.faults.noSentCopy) world.add({ ...spec, folderId: sentId, headerMessageId: generated });
      if ([...spec.recipients, ...spec.ccList].some((v) => v.toLowerCase().includes(OWNER))) {
        world.add({ ...spec, bccList: [], read: false, folderId: ACCOUNT + "://INBOX", headerMessageId: generated });
      }
      if (world.faults.queueOnly) world.add({ ...spec, folderId: "local://Outbox", headerMessageId: generated });
      world.composers.delete(tabId);
      return { messages: [] };
    },
  };

  const messenger = {
    accounts: {
      list: async () => accounts.map((a) => ({ ...clone(a) })),
      get: async (id) => {
        const a = accounts.find((x) => x.id === id);
        return a ? { ...clone(a), rootFolder: rootOf(id) } : null;
      },
    },
    folders: {
      get: async (id) => {
        const f = world.folders.get(id);
        if (!f) throw new Error("Folder not found: " + id);
        return clone(f);
      },
    },
    messages: {
      get: async (id) => headerOf(byId(id)),
      query: async (q) => {
        if (world.pendingSync) {
          const sync = world.pendingSync;
          world.pendingSync = null;
          sync();
        }
        const found = world.messages.filter((m) => matches(m, q)).map(headerOf);
        const size = q.messagesPerPage ?? 50;
        if (found.length <= size) return { messages: found };
        const listId = "list" + world.lists.size + "-" + found.length;
        world.lists.set(listId, { found, at: size, size });
        return { id: listId, messages: found.slice(0, size) };
      },
      continueList: async (listId) => {
        const list = world.lists.get(listId);
        const slice = list.found.slice(list.at, list.at + list.size);
        list.at += list.size;
        if (list.at >= list.found.length) {
          world.lists.delete(listId);
          return { messages: slice };
        }
        return { id: listId, messages: slice };
      },
      abortList: async (listId) => {
        world.lists.delete(listId);
      },
      getFull: async (id) => full(byId(id)),
      getRaw: async (id) => rawFile(byId(id)),
      listAttachments: async (id) => byId(id).attachments.map((a, i) => ({ name: a.name, partName: "1." + (i + 2), size: a.bytes.length, contentType: a.contentType })),
      getAttachmentFile: async (id, part) => {
        const a = byId(id).attachments[Number(part.split(".")[1]) - 2];
        return new File([a.bytes], a.name, { type: a.contentType });
      },
      update: async (id, values) => {
        world.counters.update += 1;
        Object.assign(byId(id), values);
      },
      move,
    },
    messengerUtilities: {
      parseMailboxString: async (text) => {
        const m = /<([^>]+)>/.exec(text);
        return [{ email: m ? m[1] : text.trim() }];
      },
      decodeMimeHeader: async (name, values) => values,
      convertToPlainText: async (html) => html.replace(/<[^>]*>/g, "").trim(),
    },
    compose,
    addressBooks: {
      get: async (id) => ({ id, contacts: world.books.filter((c) => c.book === id).map((c) => ({ id: c.id, properties: c.properties })) }),
      contacts: {
        get: async (id) => {
          const found = world.books.find((c) => c.id === id);
          if (!found) throw new Error("No such contact");
          return { id: found.id, parentId: found.book, properties: found.properties };
        },
      },
    },
    chamberlainMail: {
      syncFolders: async (accountId, mailbox, folderIds) => ({
        folders: folderIds.map((id) => ({ folder_id: id, success: !world.faults.stale, ...(world.faults.stale ? { code: "sync-callback-timeout" } : {}) })),
      }),
      headerAttachmentFlags: async (accountId, mailbox, ids, allowOutbox) => ids.map((id) => {
        const m = byId(id);
        const folder = world.folders.get(m.folderId);
        if (mailbox !== OWNER || (folder.accountId !== accountId && !(allowOutbox && folder.specialUse.includes("outbox")))) {
          throw new Error("chamberlain:account_mismatch:");
        }
        return m.attachments.length > 0;
      }),
      lookupCopies: async (accountId, mailbox, folderId, ids) => {
        if (accountId !== ACCOUNT || mailbox.toLowerCase() !== OWNER || world.folders.get(folderId)?.accountId !== accountId) {
          throw new Error("chamberlain:account_mismatch:");
        }
        const wanted = new Set(ids);
        const messages = world.messages.filter((m) => m.folderId === folderId && wanted.has(m.headerMessageId))
          .map((m) => ({ ...headerOf(m), date: m.date }));
        return { messages, complete: true };
      },
      setReadFlags: async (accountId, mailbox, folderId, refs, read) => {
        if (mailbox !== OWNER || world.folders.get(folderId)?.accountId !== accountId) throw new Error("chamberlain:account_mismatch:");
        const messages = refs.map(({ id, internetMessageId }) => {
          const m = byId(id);
          if (m.folderId !== folderId || m.headerMessageId !== internetMessageId) throw new Error("chamberlain:changed:");
          return m;
        });
        world.counters.update += 1;
        if (!world.faults.ignoreBulkRead) for (const m of messages) m.read = read;
      },
      holdComposer: async (tabId, accountId, mailbox, draftFolderId, sentFolderId) => {
        const c = tabState(tabId);
        // Like experiment/api.js: an ExtensionError text "chamberlain:<code>:<reason>"; "" skips the drafts check.
        if (draftFolderId && world.faults.identityDrafts) throw new Error(`chamberlain:changed:Thunderbird saves this identity's drafts to ${world.faults.identityDrafts}, not the bound draft folder.`);
        if (c.details.overrideDefaultFccFolder?.id !== sentFolderId) throw new Error("chamberlain:changed:The composer files its Sent copy elsewhere.");
        c.held = true;
        return { held: true };
      },
      takePrompts: async () => {
        // A prompt the real helper would have answered (fault "fcc_prompt").
        const prompts = world.faults.fccPrompt ? ["Your message was sent but a copy was not placed in your sent folder (Sent)."] : [];
        world.faults.fccPrompt = false;
        return { prompts };
      },
      openDraft: async (accountId, mailbox, messageId) => {
        const m = byId(messageId);
        const id = world.nextTab++;
        world.composers.set(id, { id, details: { subject: m.subject, plainTextBody: m.body }, files: [], held: false, modified: false, existingId: m.id });
        return { tab_id: id };
      },
      findComposers: async (accountId, mailbox, messageId) => ({
        tab_ids: [...world.composers.values()].filter((c) => c.held && c.existingId === messageId).map((c) => c.id),
      }),
      closeComposer: async (tabId) => {
        world.composers.delete(tabId);
        return { closed: true };
      },
    },
    tabs: { remove: async (id) => world.composers.delete(id) },
  };

  // --- a scripted calendar provider: events are kept as iCalendar text, as the client's cache does ---
  const unfold = (text) => String(text).replace(/\r?\n[ \t]/g, "");
  const field = (text, name) => {
    const m = new RegExp("^" + name + "[^:\r\n]*:(.*)$", "mi").exec(unfold(text));
    return m ? m[1].trim() : null;
  };
  const stamp = (value) => {
    if (!value) return NaN;
    const m = /^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})Z?)?$/.exec(value);
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] ?? 0), +(m[5] ?? 0), +(m[6] ?? 0)) : NaN;
  };
  const invitedText = (text) => /^(ATTENDEE|ORGANIZER)[;:]/mi.test(unfold(text));
  const calendarOf = (id) => {
    const c = world.calendars.get(id);
    if (!c || c.disabled) throw new Error("not_found");
    return c;
  };
  const rawItem = (c, id, text, recurrenceId = null) => {
    const own = c.type === "owl" && c.organizerId && field(text, "CLASS") === "PRIVATE" &&
      field(text, "ORGANIZER")?.toLowerCase() === c.organizerId.toLowerCase() && !field(text, "ATTENDEE");
    return { id, recurrence_id: recurrenceId, icalendar: text, invited: invitedText(text) && !own,
      own_appointment: !!own };
  };
  world.addCalendar = (id, { name = "Calendar", type = "owl", readOnly = false, disabled = false } = {}) => {
    const c = { id, name, type, readOnly, disabled, items: new Map(), occurrences: [] };
    world.calendars.set(id, c);
    return c;
  };
  const holdCalendar = async () => {
    if (world.faults.slowCalendar > 0) {
      world.faults.slowCalendar -= 1;
      await holdLatch();
    }
  };
  const chamberlainCalendar = {
    isOffline: async () => world.calendarOffline,
    listCalendars: async () => [...world.calendars.values()].map((c) => ({ id: c.id, name: c.name, type: c.type, read_only: c.readOnly, disabled: c.disabled })),
    queryEvents: async (calendarId, startMs, endMs, limit) => {
      const c = calendarOf(calendarId);
      const inside = (text) => stamp(field(text, "DTSTART")) <= endMs && (stamp(field(text, "DTEND")) || stamp(field(text, "DTSTART"))) >= startMs;
      const found = [];
      for (const [id, text] of c.items) if (!c.occurrences.some((o) => o.id === id) && inside(text)) found.push(rawItem(c, id, text));
      for (const o of c.occurrences) if (inside(o.icalendar)) found.push(rawItem(c, o.id, o.icalendar, o.recurrence_id));
      found.sort((a, b) => stamp(field(a.icalendar, "DTSTART")) - stamp(field(b.icalendar, "DTSTART")) || a.id.localeCompare(b.id));
      return { items: found.slice(0, limit), truncated: found.length > limit };
    },
    getEvent: async (calendarId, eventId) => {
      const c = calendarOf(calendarId);
      const text = c.items.get(eventId);
      return text === undefined ? null : rawItem(c, eventId, text);
    },
    inspectEvent: async (text, calendarId) => {
      const events = (unfold(text).match(/^BEGIN:VEVENT/gim) ?? []).length;
      if (events !== 1) return { ok: false, error: "one_master_event_required" };
      if (field(text, "RECURRENCE-ID")) return { ok: false, error: "master_event_required" };
      const start = stamp(field(text, "DTSTART"));
      const end = stamp(field(text, "DTEND"));
      if (!field(text, "UID") || !(start < end)) return { ok: false, error: "invalid_event_range" };
      return { ok: true, ...rawItem(calendarOf(calendarId), field(text, "UID"), text) };
    },
    addEvent: async (calendarId, text) => {
      const c = calendarOf(calendarId);
      world.calendarLog.push("add");
      await holdCalendar();
      const id = world.faults.generatedCalendarId ?? field(text, "UID");
      const saved = text.replace(/^UID:.*$/m, `UID:${id}`);
      c.items.set(id, saved);
      return rawItem(c, id, saved);
    },
    modifyEvent: async (calendarId, eventId, text) => {
      const c = calendarOf(calendarId);
      world.calendarLog.push("modify");
      await holdCalendar();
      if (!c.items.has(eventId)) throw new Error("not_found");
      c.items.set(eventId, text);
      return {};
    },
    deleteEvent: async (calendarId, eventId) => {
      const c = calendarOf(calendarId);
      world.calendarLog.push("delete");
      await holdCalendar();
      if (!c.items.delete(eventId)) throw new Error("not_found");
      return {};
    },
  };
  messenger.chamberlainCalendar = chamberlainCalendar;
  world.addCalendar("cal1", { name: "Owner calendar" });

  world.messenger = messenger;
  world.headerOf = headerOf;
  world.release = () => {
    for (const resolve of world.latches.splice(0)) resolve();
  };
  world.restart = () => {
    for (const m of world.messages) m.id = world.nextId++;
  };
  world.copies = (id, folderId) =>
    world.messages.filter((m) => (bare(m.headerMessageId) === bare(id) || (m.headers["x-chamberlain-write-token"] ?? []).includes(id)) && (!folderId || m.folderId === folderId)).length;
  world.find = (id, folderId) =>
    world.messages.find((m) => (bare(m.headerMessageId) === bare(id) || (m.headers["x-chamberlain-write-token"] ?? []).includes(id)) && (!folderId || m.folderId === folderId));
  return world;
}
