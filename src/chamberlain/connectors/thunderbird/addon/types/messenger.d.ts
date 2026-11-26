// The slice of Thunderbird's WebExtension API (`messenger`) this add-on uses.
// Deliberately loose below the first level: Thunderbird owns those shapes.

interface TbFolder {
  id: string;
  accountId: string;
  name?: string;
  path?: string;
  specialUse?: string[];
  subFolders?: TbFolder[];
}

interface TbHeader {
  id: number;
  headerMessageId: string;
  subject: string;
  date: Date | number;
  author: string;
  recipients?: string[];
  ccList?: string[];
  bccList?: string[];
  read: boolean;
  priority?: string;
  folder?: TbFolder;
}

interface TbPart {
  contentType?: string;
  name?: string;
  partName?: string;
  body?: string;
  headers?: { [name: string]: string[] };
  parts?: TbPart[];
}

interface TbPage {
  id?: string;
  messages: TbHeader[];
}

interface TbPort {
  postMessage(value: unknown): void;
  onMessage: { addListener(listener: (value: unknown) => void): void };
  onDisconnect: { addListener(listener: () => void): void };
}

interface TbIdentity {
  id: string;
  email: string;
  name?: string;
}

interface TbAccount {
  id: string;
  type: string;
  name: string;
  identities: TbIdentity[];
  rootFolder?: TbFolder;
}

interface BulkReadRef {
  id: number;
  internetMessageId: string;
}

interface HarnessMailApi {
  syncFolders(accountId: string, mailbox: string, folderIds: string[], timeoutMs: number): Promise<any>;
  headerAttachmentFlags(accountId: string, mailbox: string, messageIds: number[], allowOutbox: boolean): Promise<boolean[]>;
  lookupCopies(accountId: string, mailbox: string, folderId: string, messageIds: string[]): Promise<{ messages: TbHeader[]; complete: boolean }>;
  setReadFlags(accountId: string, mailbox: string, folderId: string, refs: BulkReadRef[], read: boolean): Promise<void>;
  holdComposer(tabId: number, accountId: string, mailbox: string, draftFolderId: string, sentFolderId: string): Promise<any>;
  openDraft(accountId: string, mailbox: string, messageId: number, internetMessageId: string): Promise<any>;
  findComposers(accountId: string, mailbox: string, messageId: number, internetMessageId: string): Promise<any>;
  closeComposer(tabId: number, accountId: string, mailbox: string): Promise<any>;
  takePrompts(tabId: number): Promise<{ prompts: string[] }>;
}

interface RawCalendar {
  id: string;
  name: string;
  type: string;
  read_only: boolean;
  disabled: boolean;
}

interface RawCalendarItem {
  id: string;
  recurrence_id: string | null;
  icalendar: string;
  invited: boolean;
  own_appointment?: boolean;
}

interface HarnessCalendarApi {
  isOffline(): Promise<boolean>;
  listCalendars(): Promise<RawCalendar[]>;
  queryEvents(calendarId: string, startMs: number, endMs: number, limit: number): Promise<{ items: RawCalendarItem[]; truncated: boolean }>;
  getEvent(calendarId: string, eventId: string): Promise<RawCalendarItem | null>;
  inspectEvent(icalendar: string, calendarId: string): Promise<{ ok: boolean; error?: string; id?: string; invited?: boolean; own_appointment?: boolean }>;
  addEvent(calendarId: string, icalendar: string): Promise<RawCalendarItem | null>;
  modifyEvent(calendarId: string, eventId: string, icalendar: string): Promise<unknown>;
  deleteEvent(calendarId: string, eventId: string): Promise<unknown>;
}

declare const messenger: {
  runtime: { getBrowserInfo(): Promise<{ version: string }>; connectNative(name: string): TbPort };
  accounts: { list(includeFolders: boolean): Promise<TbAccount[]>; get(id: string, includeFolders: boolean): Promise<TbAccount | null> };
  folders: { get(id: string): Promise<TbFolder> };
  messages: any;
  messengerUtilities: any;
  compose: any;
  tabs: any;
  addressBooks: any;
  chamberlainMail?: HarnessMailApi;
};
