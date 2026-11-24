// GENERATED from protocol.py by typegen.py. Do not edit; run
//   python -m chamberlain.connectors.thunderbird.typegen

type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

interface AccountArgs {
  account_id: string;
  mailbox: string;
}

interface AccountInfo {
  id: string;
  type: string;
  name: string;
  identities: IdentityInfo[];
}

interface AccountsArgs {

}

interface AccountsResult {
  accounts: AccountInfo[];
}

interface AddonHello {
  t: "addon_hello";
  v: number;
  id: string;
  addon_version: string;
  browser_version: string;
  capabilities: string[];
}

interface AttachmentArgs {
  account_id: string;
  mailbox: string;
  folder_id: string;
  internet_message_id: string;
  part_name: string;
  max_bytes: number;
}

interface AttachmentInfo {
  part_name: string;
  name: string;
  size: number;
  content_type: string;
}

interface AttachmentResult {
  name: string;
  size: number;
  sha256: string;
  chunks: number;
  read_state_preserved: boolean;
}

interface AttachmentSpec {
  key: string;
  name: string;
  size: number;
  sha256: string;
  content_type: string;
  chunks: number;
}

interface BridgeError {
  code: BridgeErrorCode;
  message: string;
  mutation: "none" | "possible";
  retryable: boolean;
}

type BridgeErrorCode = "bad_request" | "unknown_command" | "account_mismatch" | "not_found" | "ambiguous" | "changed" | "busy" | "timeout" | "client_offline" | "not_verified" | "too_large" | "hash_mismatch" | "unsupported" | "internal";

interface CalendarCreateArgs {
  calendar_id: string;
  event_id: string;
  icalendar: string;
}

interface CalendarDeleteArgs {
  calendar_id: string;
  event_id: string;
  expected_icalendar: string;
}

interface CalendarInfo {
  id: string;
  name: string;
  type: string;
  read_only: boolean;
  disabled: boolean;
  fresh_as_of?: string | null;
}

interface CalendarItem {
  id: string;
  recurrence_id?: string | null;
  icalendar: string;
}

interface CalendarListArgs {

}

interface CalendarListResult {
  calendars: CalendarInfo[];
}

interface CalendarQueryArgs {
  calendar_id: string;
  start: string;
  end: string;
  limit: number;
}

interface CalendarQueryResult {
  items: CalendarItem[];
  complete: boolean;
  gaps: string[];
}

interface CalendarReadArgs {
  calendar_id: string;
  event_id: string;
}

interface CalendarReadResult {
  item?: CalendarItem | null;
}

interface CalendarRef {
  calendar_id: string;
}

interface CalendarUpdateArgs {
  calendar_id: string;
  event_id: string;
  icalendar: string;
  expected_icalendar: string;
}

interface CalendarWriteResult {
  item?: CalendarItem | null;
  timing_ms?: { [key: string]: number };
}

interface ChunkFrame {
  t: "chunk";
  v: number;
  id: string;
  name: string;
  index: number;
  data: string;
  sha256: string;
}

interface ClientHello {
  t: "client_hello";
  v: number;
  id: string;
  token: string;
  client_version: string;
  require: string[];
}

interface ComposeArgs {
  account_id: string;
  mailbox: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  high_importance: boolean;
  sent_folder_id: string;
  draft_folder_id: string;
  token: string;
  attachments: AttachmentSpec[];
  reply_to?: MessageRef | null;
  display_only: boolean;
}

interface ContactInfo {
  id: string;
  book_id: string;
  name: string;
  emails: string[];
}

interface ContactReadArgs {
  address_book_id: string;
  contact_id: string;
}

interface ContactReadResult {
  contact: ContactInfo;
}

interface ContactsArgs {
  address_book_ids: string[];
  text: string;
  limit: number;
  skip: number;
  max_items: number;
}

interface ContactsResult {
  contacts: ContactInfo[];
  more: boolean;
  complete: boolean;
  scanned: number;
}

interface DiscardResult {
  closed: number;
  saved_mail_deleted: false;
}

interface DraftDiscardArgs {
  account_id: string;
  mailbox: string;
  draft: MessageRef;
  expected_raw_sha256: string;
}

interface DraftResult {
  header: Header;
  raw_sha256: string;
  attachment_bytes_verified: boolean;
  old_copies_remaining: number;
}

interface DraftSaveArgs {
  account_id: string;
  mailbox: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  high_importance: boolean;
  sent_folder_id: string;
  draft_folder_id: string;
  token: string;
  attachments: AttachmentSpec[];
  reply_to?: MessageRef | null;
  display_only: boolean;
}

interface DraftUpdateArgs {
  account_id: string;
  mailbox: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  high_importance: boolean;
  sent_folder_id: string;
  draft_folder_id: string;
  token: string;
  attachments: AttachmentSpec[];
  reply_to?: MessageRef | null;
  display_only: boolean;
  draft: MessageRef;
  expected_raw_sha256: string;
}

interface Fingerprint {
  sender: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body_sha256: string;
  attachment_sha256: string[];
  high_importance: boolean;
  in_reply_to: string;
  token: string;
}

interface FolderCopies {
  folder_id: string;
  messages: Header[];
  complete: boolean;
}

interface FolderInfo {
  id: string;
  name: string;
  path: string;
  account_id: string;
  special_use: string[];
}

interface FolderRefresh {
  folder_id: string;
  success: boolean;
  code: string;
}

interface FoldersArgs {
  account_id: string;
  mailbox: string;
  include_outbox: boolean;
}

interface FoldersResult {
  folders: FolderInfo[];
}

interface Header {
  session_id: number;
  internet_message_id: string;
  subject: string;
  date_ms: number;
  author: string;
  recipients: string[];
  cc: string[];
  bcc: string[];
  read: boolean;
  folder_id: string;
  folder_path: string;
  special_use: string[];
  has_attachments: boolean;
  priority: string;
}

interface IdentityInfo {
  id: string;
  email: string;
  name: string;
}

interface MarkReadArgs {
  account_id: string;
  mailbox: string;
  refs: MessageRef[];
  read: boolean;
}

interface MarkReadItem {
  ref: MessageRef;
  ok: boolean;
  was_read?: boolean | null;
  code: string;
}

interface MarkReadResult {
  items: MarkReadItem[];
}

interface Match {
  header: Header;
  where: "sent" | "outbox" | "drafts" | "inbox" | "other";
  exact: boolean;
  token_match: boolean;
  raw_sha256?: string | null;
  internet_message_id_matches_token: boolean;
}

interface MessageRef {
  folder_id: string;
  internet_message_id: string;
}

interface MoveArgs {
  account_id: string;
  mailbox: string;
  ref: MessageRef;
  destination_folder_id: string;
  mark_read: boolean;
}

interface MoveResult {
  message: Header;
  read_state_preserved: boolean;
  merged_existing_copy: boolean;
  marked_read: boolean;
  original_read: boolean;
}

interface QueryArgs {
  account_id: string;
  mailbox: string;
  folder_id?: string | null;
  internet_message_id?: string | null;
  text?: string | null;
  subject?: string | null;
  subject_contains: boolean;
  unread?: boolean | null;
  since_ms?: number | null;
  until_ms?: number | null;
  limit: number;
  after?: QueryCursor | null;
  max_scan: number;
  own_only: boolean;
}

interface QueryCursor {
  date_ms: number;
  key: string;
}

interface QueryResult {
  messages: Header[];
  more: boolean;
  complete: boolean;
  scanned: number;
  next?: QueryCursor | null;
}

interface ReadArgs {
  account_id: string;
  mailbox: string;
  folder_id: string;
  internet_message_id: string;
  raw_hash: boolean;
}

interface ReadResult {
  header: Header;
  subject: string;
  body: string;
  to: string[];
  cc: string[];
  bcc: string[];
  attachments: AttachmentInfo[];
  in_reply_to: string;
  references: string;
  raw_sha256?: string | null;
  read_state_preserved: boolean;
}

interface ReconcileComposeArgs {
  account_id: string;
  mailbox: string;
  fingerprint: Fingerprint;
  sent_folder_id: string;
  draft_folder_id: string;
  inbox_folder_id?: string | null;
  since_ms: number;
  refresh: boolean;
}

interface ReconcileComposeResult {
  matches: Match[];
  complete: boolean;
  refreshed: boolean;
  errors: string[];
  old_copies?: number | null;
}

interface ReconcileDiscardResult {
  copies: number;
  raw_sha256?: string | null;
  composers_open: number;
}

interface ReconcileDraftDiscardArgs {
  account_id: string;
  mailbox: string;
  draft: MessageRef;
  expected_raw_sha256: string;
}

interface ReconcileDraftSaveArgs {
  account_id: string;
  mailbox: string;
  fingerprint: Fingerprint;
  sent_folder_id: string;
  draft_folder_id: string;
  inbox_folder_id?: string | null;
  since_ms: number;
  refresh: boolean;
}

interface ReconcileDraftUpdateArgs {
  account_id: string;
  mailbox: string;
  fingerprint: Fingerprint;
  sent_folder_id: string;
  draft_folder_id: string;
  inbox_folder_id?: string | null;
  since_ms: number;
  refresh: boolean;
  old: MessageRef;
  old_raw_sha256: string;
}

interface ReconcileMarkReadArgs {
  account_id: string;
  mailbox: string;
  refs: MessageRef[];
  refresh: boolean;
}

interface ReconcileMarkReadResult {
  items: ReconcileReadItem[];
  refreshed: boolean;
  refresh_verified: boolean;
}

interface ReconcileMoveArgs {
  account_id: string;
  mailbox: string;
  ref: MessageRef;
  destination_folder_id: string;
  refresh: boolean;
}

interface ReconcileMoveResult {
  source: FolderCopies;
  destination: FolderCopies;
  refreshed: boolean;
  refresh_verified: boolean;
}

interface ReconcileReadItem {
  ref: MessageRef;
  copies: number;
  read?: boolean | null;
}

interface ReconcileSendArgs {
  account_id: string;
  mailbox: string;
  fingerprint: Fingerprint;
  sent_folder_id: string;
  draft_folder_id: string;
  inbox_folder_id?: string | null;
  since_ms: number;
  refresh: boolean;
}

interface ReconcileTrashArgs {
  account_id: string;
  mailbox: string;
  ref: MessageRef;
  destination_folder_id: string;
  refresh: boolean;
}

interface RefreshArgs {
  account_id: string;
  mailbox: string;
  folder_ids: string[];
  timeout_ms: number;
}

interface RefreshResult {
  folders: FolderRefresh[];
  provider_callback_verified: boolean;
}

interface Refusal {
  t: "refusal";
  v: number;
  id: string;
  code: RefusalCode;
  message: string;
}

type RefusalCode = "unauthenticated" | "version_mismatch" | "capability_missing" | "addon_not_connected" | "bad_hello" | "duplicate_id";

interface RequestFrame {
  t: "request";
  v: number;
  id: string;
  cmd: string;
  args?: { [key: string]: JsonValue };
}

interface ResolveArgs {
  account_id: string;
  mailbox: string;
  folder_id: string;
  internet_message_id: string;
}

interface ResolveResult {
  messages: Header[];
  complete: boolean;
}

interface ResponseFrame {
  t: "response";
  v: number;
  id: string;
  cmd: string;
  ok: boolean;
  result?: { [key: string]: JsonValue } | null;
  error?: BridgeError | null;
  chunks: number;
}

interface SendArgs {
  account_id: string;
  mailbox: string;
  to: string[];
  cc: string[];
  bcc: string[];
  subject: string;
  body: string;
  high_importance: boolean;
  sent_folder_id: string;
  draft_folder_id: string;
  token: string;
  attachments: AttachmentSpec[];
  reply_to?: MessageRef | null;
  display_only: boolean;
}

interface SendResult {
  submitted: true;
  attachment_bytes_verified: boolean;
}

interface StatusArgs {

}

interface StatusResult {
  addon_version: string;
  browser_version: string;
  write_busy: boolean;
  capabilities: string[];
}

interface TrashArgs {
  account_id: string;
  mailbox: string;
  ref: MessageRef;
  destination_folder_id: string;
}

interface Welcome {
  t: "welcome";
  v: number;
  id: string;
  host_version: string;
  addon_version: string;
  capabilities: string[];
}

interface CommandArgs {
  status: StatusArgs;
  accounts: AccountsArgs;
  folders: FoldersArgs;
  query: QueryArgs;
  read: ReadArgs;
  attachment: AttachmentArgs;
  resolve: ResolveArgs;
  refresh: RefreshArgs;
  contacts: ContactsArgs;
  contact_read: ContactReadArgs;
  send: SendArgs;
  draft_save: DraftSaveArgs;
  draft_update: DraftUpdateArgs;
  draft_discard: DraftDiscardArgs;
  move: MoveArgs;
  trash: TrashArgs;
  mark_read: MarkReadArgs;
  reconcile_send: ReconcileSendArgs;
  reconcile_draft_save: ReconcileDraftSaveArgs;
  reconcile_draft_update: ReconcileDraftUpdateArgs;
  reconcile_draft_discard: ReconcileDraftDiscardArgs;
  reconcile_move: ReconcileMoveArgs;
  reconcile_trash: ReconcileTrashArgs;
  reconcile_mark_read: ReconcileMarkReadArgs;
  calendar_list: CalendarListArgs;
  calendar_query: CalendarQueryArgs;
  calendar_read: CalendarReadArgs;
  calendar_create: CalendarCreateArgs;
  calendar_update: CalendarUpdateArgs;
  calendar_delete: CalendarDeleteArgs;
}

interface CommandResults {
  status: StatusResult;
  accounts: AccountsResult;
  folders: FoldersResult;
  query: QueryResult;
  read: ReadResult;
  attachment: AttachmentResult;
  resolve: ResolveResult;
  refresh: RefreshResult;
  contacts: ContactsResult;
  contact_read: ContactReadResult;
  send: SendResult;
  draft_save: DraftResult;
  draft_update: DraftResult;
  draft_discard: DiscardResult;
  move: MoveResult;
  trash: MoveResult;
  mark_read: MarkReadResult;
  reconcile_send: ReconcileComposeResult;
  reconcile_draft_save: ReconcileComposeResult;
  reconcile_draft_update: ReconcileComposeResult;
  reconcile_draft_discard: ReconcileDiscardResult;
  reconcile_move: ReconcileMoveResult;
  reconcile_trash: ReconcileMoveResult;
  reconcile_mark_read: ReconcileMarkReadResult;
  calendar_list: CalendarListResult;
  calendar_query: CalendarQueryResult;
  calendar_read: CalendarReadResult;
  calendar_create: CalendarWriteResult;
  calendar_update: CalendarWriteResult;
  calendar_delete: CalendarWriteResult;
}

const PROTOCOL_VERSION = 2;
const CHUNK_BYTES = 196608;
const TO_ADDON_LIMIT = 900000;
const COMMANDS: readonly string[] = ["status", "accounts", "folders", "query", "read", "attachment", "resolve", "refresh", "contacts", "contact_read", "send", "draft_save", "draft_update", "draft_discard", "move", "trash", "mark_read", "reconcile_send", "reconcile_draft_save", "reconcile_draft_update", "reconcile_draft_discard", "reconcile_move", "reconcile_trash", "reconcile_mark_read", "calendar_list", "calendar_query", "calendar_read", "calendar_create", "calendar_update", "calendar_delete"];
const WRITE_COMMANDS: readonly string[] = ["calendar_create", "calendar_delete", "calendar_update", "draft_discard", "draft_save", "draft_update", "mark_read", "move", "send", "trash"];
const REQUIRED_CAPABILITIES: readonly string[] = ["attachments", "contacts", "draft", "mark_read", "move", "read", "refresh", "search", "send", "trash"];
const ADVERTISED_CAPABILITIES: readonly string[] = ["attachments", "calendar", "contacts", "draft", "mark_read", "move", "read", "refresh", "search", "send", "trash"];
const ARGS_SCHEMA_NAME: { [cmd: string]: string } = {
 "status": "StatusArgs",
 "accounts": "AccountsArgs",
 "folders": "FoldersArgs",
 "query": "QueryArgs",
 "read": "ReadArgs",
 "attachment": "AttachmentArgs",
 "resolve": "ResolveArgs",
 "refresh": "RefreshArgs",
 "contacts": "ContactsArgs",
 "contact_read": "ContactReadArgs",
 "send": "SendArgs",
 "draft_save": "DraftSaveArgs",
 "draft_update": "DraftUpdateArgs",
 "draft_discard": "DraftDiscardArgs",
 "move": "MoveArgs",
 "trash": "TrashArgs",
 "mark_read": "MarkReadArgs",
 "reconcile_send": "ReconcileSendArgs",
 "reconcile_draft_save": "ReconcileDraftSaveArgs",
 "reconcile_draft_update": "ReconcileDraftUpdateArgs",
 "reconcile_draft_discard": "ReconcileDraftDiscardArgs",
 "reconcile_move": "ReconcileMoveArgs",
 "reconcile_trash": "ReconcileTrashArgs",
 "reconcile_mark_read": "ReconcileMarkReadArgs",
 "calendar_list": "CalendarListArgs",
 "calendar_query": "CalendarQueryArgs",
 "calendar_read": "CalendarReadArgs",
 "calendar_create": "CalendarCreateArgs",
 "calendar_update": "CalendarUpdateArgs",
 "calendar_delete": "CalendarDeleteArgs"
};
const SCHEMA_DEFS: { [name: string]: JsonValue } = {
 "AccountArgs": {
  "additionalProperties": false,
  "description": "Every mail command names its account and sender identity; the add-on verifies both.",
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox"
  ],
  "title": "AccountArgs",
  "type": "object"
 },
 "AccountInfo": {
  "additionalProperties": false,
  "properties": {
   "id": {
    "title": "Id",
    "type": "string"
   },
   "identities": {
    "default": [],
    "items": {
     "$ref": "#/$defs/IdentityInfo"
    },
    "title": "Identities",
    "type": "array"
   },
   "name": {
    "title": "Name",
    "type": "string"
   },
   "type": {
    "title": "Type",
    "type": "string"
   }
  },
  "required": [
   "id",
   "type",
   "name"
  ],
  "title": "AccountInfo",
  "type": "object"
 },
 "AccountsArgs": {
  "additionalProperties": false,
  "properties": {},
  "title": "AccountsArgs",
  "type": "object"
 },
 "AccountsResult": {
  "additionalProperties": false,
  "properties": {
   "accounts": {
    "items": {
     "$ref": "#/$defs/AccountInfo"
    },
    "title": "Accounts",
    "type": "array"
   }
  },
  "required": [
   "accounts"
  ],
  "title": "AccountsResult",
  "type": "object"
 },
 "AddonHello": {
  "additionalProperties": false,
  "description": "First message from the add-on after it connects to the host.",
  "properties": {
   "addon_version": {
    "maxLength": 40,
    "title": "Addon Version",
    "type": "string"
   },
   "browser_version": {
    "default": "",
    "maxLength": 40,
    "title": "Browser Version",
    "type": "string"
   },
   "capabilities": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Capabilities",
    "type": "array"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "t": {
    "const": "addon_hello",
    "default": "addon_hello",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "addon_version"
  ],
  "title": "AddonHello",
  "type": "object"
 },
 "AttachmentArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "internet_message_id": {
    "title": "Internet Message Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "max_bytes": {
    "minimum": 1,
    "title": "Max Bytes",
    "type": "integer"
   },
   "part_name": {
    "title": "Part Name",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "folder_id",
   "internet_message_id",
   "part_name",
   "max_bytes"
  ],
  "title": "AttachmentArgs",
  "type": "object"
 },
 "AttachmentInfo": {
  "additionalProperties": false,
  "properties": {
   "content_type": {
    "default": "",
    "title": "Content Type",
    "type": "string"
   },
   "name": {
    "title": "Name",
    "type": "string"
   },
   "part_name": {
    "title": "Part Name",
    "type": "string"
   },
   "size": {
    "title": "Size",
    "type": "integer"
   }
  },
  "required": [
   "part_name",
   "name",
   "size"
  ],
  "title": "AttachmentInfo",
  "type": "object"
 },
 "AttachmentResult": {
  "additionalProperties": false,
  "properties": {
   "chunks": {
    "title": "Chunks",
    "type": "integer"
   },
   "name": {
    "title": "Name",
    "type": "string"
   },
   "read_state_preserved": {
    "title": "Read State Preserved",
    "type": "boolean"
   },
   "sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Sha256",
    "type": "string"
   },
   "size": {
    "title": "Size",
    "type": "integer"
   }
  },
  "required": [
   "name",
   "size",
   "sha256",
   "chunks",
   "read_state_preserved"
  ],
  "title": "AttachmentResult",
  "type": "object"
 },
 "AttachmentSpec": {
  "additionalProperties": false,
  "description": "An attachment that travels as `chunks` chunk frames after the request.",
  "properties": {
   "chunks": {
    "minimum": 0,
    "title": "Chunks",
    "type": "integer"
   },
   "content_type": {
    "default": "application/octet-stream",
    "title": "Content Type",
    "type": "string"
   },
   "key": {
    "maxLength": 64,
    "minLength": 1,
    "title": "Key",
    "type": "string"
   },
   "name": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Name",
    "type": "string"
   },
   "sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Sha256",
    "type": "string"
   },
   "size": {
    "minimum": 0,
    "title": "Size",
    "type": "integer"
   }
  },
  "required": [
   "key",
   "name",
   "size",
   "sha256",
   "chunks"
  ],
  "title": "AttachmentSpec",
  "type": "object"
 },
 "BridgeError": {
  "additionalProperties": false,
  "properties": {
   "code": {
    "$ref": "#/$defs/BridgeErrorCode"
   },
   "message": {
    "default": "",
    "maxLength": 500,
    "title": "Message",
    "type": "string"
   },
   "mutation": {
    "default": "none",
    "enum": [
     "none",
     "possible"
    ],
    "title": "Mutation",
    "type": "string"
   },
   "retryable": {
    "default": false,
    "title": "Retryable",
    "type": "boolean"
   }
  },
  "required": [
   "code"
  ],
  "title": "BridgeError",
  "type": "object"
 },
 "BridgeErrorCode": {
  "description": "Add-on error codes and what they mean to the connector (section 7).",
  "enum": [
   "bad_request",
   "unknown_command",
   "account_mismatch",
   "not_found",
   "ambiguous",
   "changed",
   "busy",
   "timeout",
   "client_offline",
   "not_verified",
   "too_large",
   "hash_mismatch",
   "unsupported",
   "internal"
  ],
  "title": "BridgeErrorCode",
  "type": "string"
 },
 "CalendarCreateArgs": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   },
   "event_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Event Id",
    "type": "string"
   },
   "icalendar": {
    "maxLength": 200000,
    "title": "Icalendar",
    "type": "string"
   }
  },
  "required": [
   "calendar_id",
   "event_id",
   "icalendar"
  ],
  "title": "CalendarCreateArgs",
  "type": "object"
 },
 "CalendarDeleteArgs": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   },
   "event_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Event Id",
    "type": "string"
   },
   "expected_icalendar": {
    "maxLength": 200000,
    "title": "Expected Icalendar",
    "type": "string"
   }
  },
  "required": [
   "calendar_id",
   "event_id",
   "expected_icalendar"
  ],
  "title": "CalendarDeleteArgs",
  "type": "object"
 },
 "CalendarInfo": {
  "additionalProperties": false,
  "properties": {
   "disabled": {
    "title": "Disabled",
    "type": "boolean"
   },
   "fresh_as_of": {
    "anyOf": [
     {
      "maxLength": 40,
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Fresh As Of"
   },
   "id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Id",
    "type": "string"
   },
   "name": {
    "maxLength": 2000,
    "title": "Name",
    "type": "string"
   },
   "read_only": {
    "title": "Read Only",
    "type": "boolean"
   },
   "type": {
    "maxLength": 100,
    "title": "Type",
    "type": "string"
   }
  },
  "required": [
   "id",
   "name",
   "type",
   "read_only",
   "disabled"
  ],
  "title": "CalendarInfo",
  "type": "object"
 },
 "CalendarItem": {
  "additionalProperties": false,
  "description": "One event, or one occurrence of a series (`recurrence_id` set), as iCalendar text.",
  "properties": {
   "icalendar": {
    "maxLength": 200000,
    "title": "Icalendar",
    "type": "string"
   },
   "id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Id",
    "type": "string"
   },
   "recurrence_id": {
    "anyOf": [
     {
      "maxLength": 40,
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Recurrence Id"
   }
  },
  "required": [
   "id",
   "icalendar"
  ],
  "title": "CalendarItem",
  "type": "object"
 },
 "CalendarListArgs": {
  "additionalProperties": false,
  "properties": {},
  "title": "CalendarListArgs",
  "type": "object"
 },
 "CalendarListResult": {
  "additionalProperties": false,
  "properties": {
   "calendars": {
    "items": {
     "$ref": "#/$defs/CalendarInfo"
    },
    "title": "Calendars",
    "type": "array"
   }
  },
  "required": [
   "calendars"
  ],
  "title": "CalendarListResult",
  "type": "object"
 },
 "CalendarQueryArgs": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   },
   "end": {
    "maxLength": 40,
    "minLength": 10,
    "title": "End",
    "type": "string"
   },
   "limit": {
    "maximum": 5000,
    "minimum": 1,
    "title": "Limit",
    "type": "integer"
   },
   "start": {
    "maxLength": 40,
    "minLength": 10,
    "title": "Start",
    "type": "string"
   }
  },
  "required": [
   "calendar_id",
   "start",
   "end",
   "limit"
  ],
  "title": "CalendarQueryArgs",
  "type": "object"
 },
 "CalendarQueryResult": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "gaps": {
    "default": [],
    "items": {
     "maxLength": 2000,
     "type": "string"
    },
    "title": "Gaps",
    "type": "array"
   },
   "items": {
    "items": {
     "$ref": "#/$defs/CalendarItem"
    },
    "title": "Items",
    "type": "array"
   }
  },
  "required": [
   "items",
   "complete"
  ],
  "title": "CalendarQueryResult",
  "type": "object"
 },
 "CalendarReadArgs": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   },
   "event_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Event Id",
    "type": "string"
   }
  },
  "required": [
   "calendar_id",
   "event_id"
  ],
  "title": "CalendarReadArgs",
  "type": "object"
 },
 "CalendarReadResult": {
  "additionalProperties": false,
  "properties": {
   "item": {
    "anyOf": [
     {
      "$ref": "#/$defs/CalendarItem"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   }
  },
  "title": "CalendarReadResult",
  "type": "object"
 },
 "CalendarRef": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   }
  },
  "required": [
   "calendar_id"
  ],
  "title": "CalendarRef",
  "type": "object"
 },
 "CalendarUpdateArgs": {
  "additionalProperties": false,
  "properties": {
   "calendar_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Calendar Id",
    "type": "string"
   },
   "event_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Event Id",
    "type": "string"
   },
   "expected_icalendar": {
    "maxLength": 200000,
    "title": "Expected Icalendar",
    "type": "string"
   },
   "icalendar": {
    "maxLength": 200000,
    "title": "Icalendar",
    "type": "string"
   }
  },
  "required": [
   "calendar_id",
   "event_id",
   "icalendar",
   "expected_icalendar"
  ],
  "title": "CalendarUpdateArgs",
  "type": "object"
 },
 "CalendarWriteResult": {
  "additionalProperties": false,
  "properties": {
   "item": {
    "anyOf": [
     {
      "$ref": "#/$defs/CalendarItem"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "timing_ms": {
    "additionalProperties": {
     "type": "integer"
    },
    "title": "Timing Ms",
    "type": "object"
   }
  },
  "title": "CalendarWriteResult",
  "type": "object"
 },
 "ChunkFrame": {
  "additionalProperties": false,
  "description": "One piece of an attachment, inside the request or response with this ID.",
  "properties": {
   "data": {
    "title": "Data",
    "type": "string"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "index": {
    "minimum": 0,
    "title": "Index",
    "type": "integer"
   },
   "name": {
    "maxLength": 64,
    "minLength": 1,
    "title": "Name",
    "type": "string"
   },
   "sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Sha256",
    "type": "string"
   },
   "t": {
    "const": "chunk",
    "default": "chunk",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "name",
   "index",
   "data",
   "sha256"
  ],
  "title": "ChunkFrame",
  "type": "object"
 },
 "ClientHello": {
  "additionalProperties": false,
  "description": "First message from a local client; carries the session token.",
  "properties": {
   "client_version": {
    "default": "",
    "maxLength": 40,
    "title": "Client Version",
    "type": "string"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "require": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Require",
    "type": "array"
   },
   "t": {
    "const": "client_hello",
    "default": "client_hello",
    "title": "T",
    "type": "string"
   },
   "token": {
    "maxLength": 200,
    "title": "Token",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "token"
  ],
  "title": "ClientHello",
  "type": "object"
 },
 "ComposeArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "attachments": {
    "default": [],
    "items": {
     "$ref": "#/$defs/AttachmentSpec"
    },
    "title": "Attachments",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body": {
    "maxLength": 200000,
    "title": "Body",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "display_only": {
    "default": false,
    "title": "Display Only",
    "type": "boolean"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "high_importance": {
    "default": false,
    "title": "High Importance",
    "type": "boolean"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "reply_to": {
    "anyOf": [
     {
      "$ref": "#/$defs/MessageRef"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "subject": {
    "maxLength": 500,
    "pattern": "^[^\\r\\n]*$",
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   },
   "token": {
    "maxLength": 998,
    "minLength": 1,
    "title": "Token",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "subject",
   "body",
   "sent_folder_id",
   "draft_folder_id",
   "token"
  ],
  "title": "ComposeArgs",
  "type": "object"
 },
 "ContactInfo": {
  "additionalProperties": false,
  "properties": {
   "book_id": {
    "title": "Book Id",
    "type": "string"
   },
   "emails": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Emails",
    "type": "array"
   },
   "id": {
    "title": "Id",
    "type": "string"
   },
   "name": {
    "title": "Name",
    "type": "string"
   }
  },
  "required": [
   "id",
   "book_id",
   "name"
  ],
  "title": "ContactInfo",
  "type": "object"
 },
 "ContactReadArgs": {
  "additionalProperties": false,
  "properties": {
   "address_book_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Address Book Id",
    "type": "string"
   },
   "contact_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Contact Id",
    "type": "string"
   }
  },
  "required": [
   "address_book_id",
   "contact_id"
  ],
  "title": "ContactReadArgs",
  "type": "object"
 },
 "ContactReadResult": {
  "additionalProperties": false,
  "properties": {
   "contact": {
    "$ref": "#/$defs/ContactInfo"
   }
  },
  "required": [
   "contact"
  ],
  "title": "ContactReadResult",
  "type": "object"
 },
 "ContactsArgs": {
  "additionalProperties": false,
  "properties": {
   "address_book_ids": {
    "items": {
     "type": "string"
    },
    "maxItems": 20,
    "minItems": 1,
    "title": "Address Book Ids",
    "type": "array"
   },
   "limit": {
    "default": 10,
    "maximum": 50,
    "minimum": 1,
    "title": "Limit",
    "type": "integer"
   },
   "max_items": {
    "default": 2000,
    "maximum": 5000,
    "minimum": 1,
    "title": "Max Items",
    "type": "integer"
   },
   "skip": {
    "default": 0,
    "minimum": 0,
    "title": "Skip",
    "type": "integer"
   },
   "text": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Text",
    "type": "string"
   }
  },
  "required": [
   "address_book_ids",
   "text"
  ],
  "title": "ContactsArgs",
  "type": "object"
 },
 "ContactsResult": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "contacts": {
    "items": {
     "$ref": "#/$defs/ContactInfo"
    },
    "title": "Contacts",
    "type": "array"
   },
   "more": {
    "title": "More",
    "type": "boolean"
   },
   "scanned": {
    "title": "Scanned",
    "type": "integer"
   }
  },
  "required": [
   "contacts",
   "more",
   "complete",
   "scanned"
  ],
  "title": "ContactsResult",
  "type": "object"
 },
 "DiscardResult": {
  "additionalProperties": false,
  "properties": {
   "closed": {
    "title": "Closed",
    "type": "integer"
   },
   "saved_mail_deleted": {
    "const": false,
    "default": false,
    "title": "Saved Mail Deleted",
    "type": "boolean"
   }
  },
  "required": [
   "closed"
  ],
  "title": "DiscardResult",
  "type": "object"
 },
 "DraftDiscardArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft": {
    "$ref": "#/$defs/MessageRef"
   },
   "expected_raw_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Expected Raw Sha256",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "draft",
   "expected_raw_sha256"
  ],
  "title": "DraftDiscardArgs",
  "type": "object"
 },
 "DraftResult": {
  "additionalProperties": false,
  "properties": {
   "attachment_bytes_verified": {
    "title": "Attachment Bytes Verified",
    "type": "boolean"
   },
   "header": {
    "$ref": "#/$defs/Header"
   },
   "old_copies_remaining": {
    "default": 0,
    "title": "Old Copies Remaining",
    "type": "integer"
   },
   "raw_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Raw Sha256",
    "type": "string"
   }
  },
  "required": [
   "header",
   "raw_sha256",
   "attachment_bytes_verified"
  ],
  "title": "DraftResult",
  "type": "object"
 },
 "DraftSaveArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "attachments": {
    "default": [],
    "items": {
     "$ref": "#/$defs/AttachmentSpec"
    },
    "title": "Attachments",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body": {
    "maxLength": 200000,
    "title": "Body",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "display_only": {
    "default": false,
    "title": "Display Only",
    "type": "boolean"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "high_importance": {
    "default": false,
    "title": "High Importance",
    "type": "boolean"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "reply_to": {
    "anyOf": [
     {
      "$ref": "#/$defs/MessageRef"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "subject": {
    "maxLength": 500,
    "pattern": "^[^\\r\\n]*$",
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   },
   "token": {
    "maxLength": 998,
    "minLength": 1,
    "title": "Token",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "subject",
   "body",
   "sent_folder_id",
   "draft_folder_id",
   "token"
  ],
  "title": "DraftSaveArgs",
  "type": "object"
 },
 "DraftUpdateArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "attachments": {
    "default": [],
    "items": {
     "$ref": "#/$defs/AttachmentSpec"
    },
    "title": "Attachments",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body": {
    "maxLength": 200000,
    "title": "Body",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "display_only": {
    "default": false,
    "title": "Display Only",
    "type": "boolean"
   },
   "draft": {
    "$ref": "#/$defs/MessageRef"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "expected_raw_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Expected Raw Sha256",
    "type": "string"
   },
   "high_importance": {
    "default": false,
    "title": "High Importance",
    "type": "boolean"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "reply_to": {
    "anyOf": [
     {
      "$ref": "#/$defs/MessageRef"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "subject": {
    "maxLength": 500,
    "pattern": "^[^\\r\\n]*$",
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   },
   "token": {
    "maxLength": 998,
    "minLength": 1,
    "title": "Token",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "subject",
   "body",
   "sent_folder_id",
   "draft_folder_id",
   "token",
   "draft",
   "expected_raw_sha256"
  ],
  "title": "DraftUpdateArgs",
  "type": "object"
 },
 "Fingerprint": {
  "additionalProperties": false,
  "description": "What a sent or saved copy must look like (no Message-ID needed, R1).",
  "properties": {
   "attachment_sha256": {
    "default": [],
    "items": {
     "pattern": "^[0-9a-f]{64}$",
     "type": "string"
    },
    "title": "Attachment Sha256",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Body Sha256",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "high_importance": {
    "default": false,
    "title": "High Importance",
    "type": "boolean"
   },
   "in_reply_to": {
    "default": "",
    "title": "In Reply To",
    "type": "string"
   },
   "sender": {
    "title": "Sender",
    "type": "string"
   },
   "subject": {
    "maxLength": 500,
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   },
   "token": {
    "title": "Token",
    "type": "string"
   }
  },
  "required": [
   "sender",
   "subject",
   "body_sha256",
   "token"
  ],
  "title": "Fingerprint",
  "type": "object"
 },
 "FolderCopies": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "messages": {
    "items": {
     "$ref": "#/$defs/Header"
    },
    "title": "Messages",
    "type": "array"
   }
  },
  "required": [
   "folder_id",
   "messages",
   "complete"
  ],
  "title": "FolderCopies",
  "type": "object"
 },
 "FolderInfo": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "title": "Account Id",
    "type": "string"
   },
   "id": {
    "title": "Id",
    "type": "string"
   },
   "name": {
    "title": "Name",
    "type": "string"
   },
   "path": {
    "title": "Path",
    "type": "string"
   },
   "special_use": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Special Use",
    "type": "array"
   }
  },
  "required": [
   "id",
   "name",
   "path",
   "account_id"
  ],
  "title": "FolderInfo",
  "type": "object"
 },
 "FolderRefresh": {
  "additionalProperties": false,
  "properties": {
   "code": {
    "default": "",
    "title": "Code",
    "type": "string"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "success": {
    "title": "Success",
    "type": "boolean"
   }
  },
  "required": [
   "folder_id",
   "success"
  ],
  "title": "FolderRefresh",
  "type": "object"
 },
 "FoldersArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "include_outbox": {
    "default": false,
    "title": "Include Outbox",
    "type": "boolean"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox"
  ],
  "title": "FoldersArgs",
  "type": "object"
 },
 "FoldersResult": {
  "additionalProperties": false,
  "properties": {
   "folders": {
    "items": {
     "$ref": "#/$defs/FolderInfo"
    },
    "title": "Folders",
    "type": "array"
   }
  },
  "required": [
   "folders"
  ],
  "title": "FoldersResult",
  "type": "object"
 },
 "Header": {
  "additionalProperties": false,
  "properties": {
   "author": {
    "maxLength": 2000,
    "title": "Author",
    "type": "string"
   },
   "bcc": {
    "default": [],
    "items": {
     "maxLength": 2000,
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "cc": {
    "default": [],
    "items": {
     "maxLength": 2000,
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "date_ms": {
    "title": "Date Ms",
    "type": "integer"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "folder_path": {
    "default": "",
    "title": "Folder Path",
    "type": "string"
   },
   "has_attachments": {
    "default": false,
    "title": "Has Attachments",
    "type": "boolean"
   },
   "internet_message_id": {
    "title": "Internet Message Id",
    "type": "string"
   },
   "priority": {
    "default": "",
    "title": "Priority",
    "type": "string"
   },
   "read": {
    "title": "Read",
    "type": "boolean"
   },
   "recipients": {
    "default": [],
    "items": {
     "maxLength": 2000,
     "type": "string"
    },
    "title": "Recipients",
    "type": "array"
   },
   "session_id": {
    "title": "Session Id",
    "type": "integer"
   },
   "special_use": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Special Use",
    "type": "array"
   },
   "subject": {
    "maxLength": 200000,
    "title": "Subject",
    "type": "string"
   }
  },
  "required": [
   "session_id",
   "internet_message_id",
   "subject",
   "date_ms",
   "author",
   "read",
   "folder_id"
  ],
  "title": "Header",
  "type": "object"
 },
 "IdentityInfo": {
  "additionalProperties": false,
  "properties": {
   "email": {
    "title": "Email",
    "type": "string"
   },
   "id": {
    "title": "Id",
    "type": "string"
   },
   "name": {
    "default": "",
    "title": "Name",
    "type": "string"
   }
  },
  "required": [
   "id",
   "email"
  ],
  "title": "IdentityInfo",
  "type": "object"
 },
 "JsonValue": {},
 "MarkReadArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "read": {
    "default": true,
    "title": "Read",
    "type": "boolean"
   },
   "refs": {
    "items": {
     "$ref": "#/$defs/MessageRef"
    },
    "maxItems": 200,
    "minItems": 1,
    "title": "Refs",
    "type": "array"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "refs"
  ],
  "title": "MarkReadArgs",
  "type": "object"
 },
 "MarkReadItem": {
  "additionalProperties": false,
  "properties": {
   "code": {
    "default": "",
    "title": "Code",
    "type": "string"
   },
   "ok": {
    "title": "Ok",
    "type": "boolean"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   },
   "was_read": {
    "anyOf": [
     {
      "type": "boolean"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Was Read"
   }
  },
  "required": [
   "ref",
   "ok"
  ],
  "title": "MarkReadItem",
  "type": "object"
 },
 "MarkReadResult": {
  "additionalProperties": false,
  "properties": {
   "items": {
    "items": {
     "$ref": "#/$defs/MarkReadItem"
    },
    "title": "Items",
    "type": "array"
   }
  },
  "required": [
   "items"
  ],
  "title": "MarkReadResult",
  "type": "object"
 },
 "Match": {
  "additionalProperties": false,
  "properties": {
   "exact": {
    "title": "Exact",
    "type": "boolean"
   },
   "header": {
    "$ref": "#/$defs/Header"
   },
   "internet_message_id_matches_token": {
    "default": false,
    "title": "Internet Message Id Matches Token",
    "type": "boolean"
   },
   "raw_sha256": {
    "anyOf": [
     {
      "pattern": "^[0-9a-f]{64}$",
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Raw Sha256"
   },
   "token_match": {
    "title": "Token Match",
    "type": "boolean"
   },
   "where": {
    "enum": [
     "sent",
     "outbox",
     "drafts",
     "inbox",
     "other"
    ],
    "title": "Where",
    "type": "string"
   }
  },
  "required": [
   "header",
   "where",
   "exact",
   "token_match"
  ],
  "title": "Match",
  "type": "object"
 },
 "MessageRef": {
  "additionalProperties": false,
  "description": "One physical copy by stable identity; session integers never cross the bridge.",
  "properties": {
   "folder_id": {
    "maxLength": 1000,
    "minLength": 1,
    "title": "Folder Id",
    "type": "string"
   },
   "internet_message_id": {
    "maxLength": 998,
    "minLength": 1,
    "title": "Internet Message Id",
    "type": "string"
   }
  },
  "required": [
   "folder_id",
   "internet_message_id"
  ],
  "title": "MessageRef",
  "type": "object"
 },
 "MoveArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "destination_folder_id": {
    "title": "Destination Folder Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "mark_read": {
    "default": false,
    "title": "Mark Read",
    "type": "boolean"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "ref",
   "destination_folder_id"
  ],
  "title": "MoveArgs",
  "type": "object"
 },
 "MoveResult": {
  "additionalProperties": false,
  "properties": {
   "marked_read": {
    "default": false,
    "title": "Marked Read",
    "type": "boolean"
   },
   "merged_existing_copy": {
    "default": false,
    "title": "Merged Existing Copy",
    "type": "boolean"
   },
   "message": {
    "$ref": "#/$defs/Header"
   },
   "original_read": {
    "title": "Original Read",
    "type": "boolean"
   },
   "read_state_preserved": {
    "title": "Read State Preserved",
    "type": "boolean"
   }
  },
  "required": [
   "message",
   "read_state_preserved",
   "original_read"
  ],
  "title": "MoveResult",
  "type": "object"
 },
 "QueryArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "after": {
    "anyOf": [
     {
      "$ref": "#/$defs/QueryCursor"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "folder_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Folder Id"
   },
   "internet_message_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Internet Message Id"
   },
   "limit": {
    "default": 25,
    "maximum": 200,
    "minimum": 1,
    "title": "Limit",
    "type": "integer"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "max_scan": {
    "default": 2000,
    "maximum": 5000,
    "minimum": 1,
    "title": "Max Scan",
    "type": "integer"
   },
   "own_only": {
    "default": false,
    "title": "Own Only",
    "type": "boolean"
   },
   "since_ms": {
    "anyOf": [
     {
      "type": "integer"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Since Ms"
   },
   "subject": {
    "anyOf": [
     {
      "maxLength": 500,
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Subject"
   },
   "subject_contains": {
    "default": false,
    "title": "Subject Contains",
    "type": "boolean"
   },
   "text": {
    "anyOf": [
     {
      "maxLength": 500,
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Text"
   },
   "unread": {
    "anyOf": [
     {
      "type": "boolean"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Unread"
   },
   "until_ms": {
    "anyOf": [
     {
      "type": "integer"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Until Ms"
   }
  },
  "required": [
   "account_id",
   "mailbox"
  ],
  "title": "QueryArgs",
  "type": "object"
 },
 "QueryCursor": {
  "additionalProperties": false,
  "properties": {
   "date_ms": {
    "title": "Date Ms",
    "type": "integer"
   },
   "key": {
    "title": "Key",
    "type": "string"
   }
  },
  "required": [
   "date_ms",
   "key"
  ],
  "title": "QueryCursor",
  "type": "object"
 },
 "QueryResult": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "messages": {
    "items": {
     "$ref": "#/$defs/Header"
    },
    "title": "Messages",
    "type": "array"
   },
   "more": {
    "title": "More",
    "type": "boolean"
   },
   "next": {
    "anyOf": [
     {
      "$ref": "#/$defs/QueryCursor"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "scanned": {
    "title": "Scanned",
    "type": "integer"
   }
  },
  "required": [
   "messages",
   "more",
   "complete",
   "scanned"
  ],
  "title": "QueryResult",
  "type": "object"
 },
 "ReadArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "internet_message_id": {
    "title": "Internet Message Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "raw_hash": {
    "default": false,
    "title": "Raw Hash",
    "type": "boolean"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "folder_id",
   "internet_message_id"
  ],
  "title": "ReadArgs",
  "type": "object"
 },
 "ReadResult": {
  "additionalProperties": false,
  "properties": {
   "attachments": {
    "default": [],
    "items": {
     "$ref": "#/$defs/AttachmentInfo"
    },
    "title": "Attachments",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body": {
    "maxLength": 200000,
    "title": "Body",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "header": {
    "$ref": "#/$defs/Header"
   },
   "in_reply_to": {
    "default": "",
    "title": "In Reply To",
    "type": "string"
   },
   "raw_sha256": {
    "anyOf": [
     {
      "pattern": "^[0-9a-f]{64}$",
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Raw Sha256"
   },
   "read_state_preserved": {
    "title": "Read State Preserved",
    "type": "boolean"
   },
   "references": {
    "default": "",
    "title": "References",
    "type": "string"
   },
   "subject": {
    "maxLength": 200000,
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   }
  },
  "required": [
   "header",
   "subject",
   "body",
   "read_state_preserved"
  ],
  "title": "ReadResult",
  "type": "object"
 },
 "ReconcileComposeArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "fingerprint": {
    "$ref": "#/$defs/Fingerprint"
   },
   "inbox_folder_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Inbox Folder Id"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "since_ms": {
    "title": "Since Ms",
    "type": "integer"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "fingerprint",
   "sent_folder_id",
   "draft_folder_id",
   "since_ms"
  ],
  "title": "ReconcileComposeArgs",
  "type": "object"
 },
 "ReconcileComposeResult": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "errors": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Errors",
    "type": "array"
   },
   "matches": {
    "items": {
     "$ref": "#/$defs/Match"
    },
    "title": "Matches",
    "type": "array"
   },
   "old_copies": {
    "anyOf": [
     {
      "type": "integer"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Old Copies"
   },
   "refreshed": {
    "title": "Refreshed",
    "type": "boolean"
   }
  },
  "required": [
   "matches",
   "complete",
   "refreshed"
  ],
  "title": "ReconcileComposeResult",
  "type": "object"
 },
 "ReconcileDiscardResult": {
  "additionalProperties": false,
  "properties": {
   "composers_open": {
    "title": "Composers Open",
    "type": "integer"
   },
   "copies": {
    "title": "Copies",
    "type": "integer"
   },
   "raw_sha256": {
    "anyOf": [
     {
      "pattern": "^[0-9a-f]{64}$",
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Raw Sha256"
   }
  },
  "required": [
   "copies",
   "composers_open"
  ],
  "title": "ReconcileDiscardResult",
  "type": "object"
 },
 "ReconcileDraftDiscardArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft": {
    "$ref": "#/$defs/MessageRef"
   },
   "expected_raw_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Expected Raw Sha256",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "draft",
   "expected_raw_sha256"
  ],
  "title": "ReconcileDraftDiscardArgs",
  "type": "object"
 },
 "ReconcileDraftSaveArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "fingerprint": {
    "$ref": "#/$defs/Fingerprint"
   },
   "inbox_folder_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Inbox Folder Id"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "since_ms": {
    "title": "Since Ms",
    "type": "integer"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "fingerprint",
   "sent_folder_id",
   "draft_folder_id",
   "since_ms"
  ],
  "title": "ReconcileDraftSaveArgs",
  "type": "object"
 },
 "ReconcileDraftUpdateArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "fingerprint": {
    "$ref": "#/$defs/Fingerprint"
   },
   "inbox_folder_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Inbox Folder Id"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "old": {
    "$ref": "#/$defs/MessageRef"
   },
   "old_raw_sha256": {
    "pattern": "^[0-9a-f]{64}$",
    "title": "Old Raw Sha256",
    "type": "string"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "since_ms": {
    "title": "Since Ms",
    "type": "integer"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "fingerprint",
   "sent_folder_id",
   "draft_folder_id",
   "since_ms",
   "old",
   "old_raw_sha256"
  ],
  "title": "ReconcileDraftUpdateArgs",
  "type": "object"
 },
 "ReconcileMarkReadArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   },
   "refs": {
    "items": {
     "$ref": "#/$defs/MessageRef"
    },
    "maxItems": 200,
    "minItems": 1,
    "title": "Refs",
    "type": "array"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "refs"
  ],
  "title": "ReconcileMarkReadArgs",
  "type": "object"
 },
 "ReconcileMarkReadResult": {
  "additionalProperties": false,
  "properties": {
   "items": {
    "items": {
     "$ref": "#/$defs/ReconcileReadItem"
    },
    "title": "Items",
    "type": "array"
   },
   "refresh_verified": {
    "title": "Refresh Verified",
    "type": "boolean"
   },
   "refreshed": {
    "title": "Refreshed",
    "type": "boolean"
   }
  },
  "required": [
   "items",
   "refreshed",
   "refresh_verified"
  ],
  "title": "ReconcileMarkReadResult",
  "type": "object"
 },
 "ReconcileMoveArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "destination_folder_id": {
    "title": "Destination Folder Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "ref",
   "destination_folder_id"
  ],
  "title": "ReconcileMoveArgs",
  "type": "object"
 },
 "ReconcileMoveResult": {
  "additionalProperties": false,
  "properties": {
   "destination": {
    "$ref": "#/$defs/FolderCopies"
   },
   "refresh_verified": {
    "title": "Refresh Verified",
    "type": "boolean"
   },
   "refreshed": {
    "title": "Refreshed",
    "type": "boolean"
   },
   "source": {
    "$ref": "#/$defs/FolderCopies"
   }
  },
  "required": [
   "source",
   "destination",
   "refreshed",
   "refresh_verified"
  ],
  "title": "ReconcileMoveResult",
  "type": "object"
 },
 "ReconcileReadItem": {
  "additionalProperties": false,
  "properties": {
   "copies": {
    "title": "Copies",
    "type": "integer"
   },
   "read": {
    "anyOf": [
     {
      "type": "boolean"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Read"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   }
  },
  "required": [
   "ref",
   "copies"
  ],
  "title": "ReconcileReadItem",
  "type": "object"
 },
 "ReconcileSendArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "fingerprint": {
    "$ref": "#/$defs/Fingerprint"
   },
   "inbox_folder_id": {
    "anyOf": [
     {
      "type": "string"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Inbox Folder Id"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "since_ms": {
    "title": "Since Ms",
    "type": "integer"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "fingerprint",
   "sent_folder_id",
   "draft_folder_id",
   "since_ms"
  ],
  "title": "ReconcileSendArgs",
  "type": "object"
 },
 "ReconcileTrashArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "destination_folder_id": {
    "title": "Destination Folder Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   },
   "refresh": {
    "default": true,
    "title": "Refresh",
    "type": "boolean"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "ref",
   "destination_folder_id"
  ],
  "title": "ReconcileTrashArgs",
  "type": "object"
 },
 "RefreshArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "folder_ids": {
    "items": {
     "type": "string"
    },
    "maxItems": 8,
    "minItems": 1,
    "title": "Folder Ids",
    "type": "array"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "timeout_ms": {
    "default": 8000,
    "maximum": 15000,
    "minimum": 500,
    "title": "Timeout Ms",
    "type": "integer"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "folder_ids"
  ],
  "title": "RefreshArgs",
  "type": "object"
 },
 "RefreshResult": {
  "additionalProperties": false,
  "properties": {
   "folders": {
    "items": {
     "$ref": "#/$defs/FolderRefresh"
    },
    "title": "Folders",
    "type": "array"
   },
   "provider_callback_verified": {
    "title": "Provider Callback Verified",
    "type": "boolean"
   }
  },
  "required": [
   "folders",
   "provider_callback_verified"
  ],
  "title": "RefreshResult",
  "type": "object"
 },
 "Refusal": {
  "additionalProperties": false,
  "description": "The host (or add-on) refuses to operate; nothing was executed.",
  "properties": {
   "code": {
    "$ref": "#/$defs/RefusalCode"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "message": {
    "default": "",
    "maxLength": 500,
    "title": "Message",
    "type": "string"
   },
   "t": {
    "const": "refusal",
    "default": "refusal",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "code"
  ],
  "title": "Refusal",
  "type": "object"
 },
 "RefusalCode": {
  "enum": [
   "unauthenticated",
   "version_mismatch",
   "capability_missing",
   "addon_not_connected",
   "bad_hello",
   "duplicate_id"
  ],
  "title": "RefusalCode",
  "type": "string"
 },
 "RequestFrame": {
  "additionalProperties": false,
  "properties": {
   "args": {
    "additionalProperties": {
     "$ref": "#/$defs/JsonValue"
    },
    "title": "Args",
    "type": "object"
   },
   "cmd": {
    "title": "Cmd",
    "type": "string"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "t": {
    "const": "request",
    "default": "request",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "cmd"
  ],
  "title": "RequestFrame",
  "type": "object"
 },
 "ResolveArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "folder_id": {
    "title": "Folder Id",
    "type": "string"
   },
   "internet_message_id": {
    "title": "Internet Message Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "folder_id",
   "internet_message_id"
  ],
  "title": "ResolveArgs",
  "type": "object"
 },
 "ResolveResult": {
  "additionalProperties": false,
  "properties": {
   "complete": {
    "title": "Complete",
    "type": "boolean"
   },
   "messages": {
    "items": {
     "$ref": "#/$defs/Header"
    },
    "title": "Messages",
    "type": "array"
   }
  },
  "required": [
   "messages",
   "complete"
  ],
  "title": "ResolveResult",
  "type": "object"
 },
 "ResponseFrame": {
  "additionalProperties": false,
  "properties": {
   "chunks": {
    "default": 0,
    "title": "Chunks",
    "type": "integer"
   },
   "cmd": {
    "title": "Cmd",
    "type": "string"
   },
   "error": {
    "anyOf": [
     {
      "$ref": "#/$defs/BridgeError"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "ok": {
    "title": "Ok",
    "type": "boolean"
   },
   "result": {
    "anyOf": [
     {
      "additionalProperties": {
       "$ref": "#/$defs/JsonValue"
      },
      "type": "object"
     },
     {
      "type": "null"
     }
    ],
    "default": null,
    "title": "Result"
   },
   "t": {
    "const": "response",
    "default": "response",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "cmd",
   "ok"
  ],
  "title": "ResponseFrame",
  "type": "object"
 },
 "SendArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "attachments": {
    "default": [],
    "items": {
     "$ref": "#/$defs/AttachmentSpec"
    },
    "title": "Attachments",
    "type": "array"
   },
   "bcc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Bcc",
    "type": "array"
   },
   "body": {
    "maxLength": 200000,
    "title": "Body",
    "type": "string"
   },
   "cc": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "Cc",
    "type": "array"
   },
   "display_only": {
    "default": false,
    "title": "Display Only",
    "type": "boolean"
   },
   "draft_folder_id": {
    "title": "Draft Folder Id",
    "type": "string"
   },
   "high_importance": {
    "default": false,
    "title": "High Importance",
    "type": "boolean"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "reply_to": {
    "anyOf": [
     {
      "$ref": "#/$defs/MessageRef"
     },
     {
      "type": "null"
     }
    ],
    "default": null
   },
   "sent_folder_id": {
    "title": "Sent Folder Id",
    "type": "string"
   },
   "subject": {
    "maxLength": 500,
    "pattern": "^[^\\r\\n]*$",
    "title": "Subject",
    "type": "string"
   },
   "to": {
    "default": [],
    "items": {
     "type": "string"
    },
    "title": "To",
    "type": "array"
   },
   "token": {
    "maxLength": 998,
    "minLength": 1,
    "title": "Token",
    "type": "string"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "subject",
   "body",
   "sent_folder_id",
   "draft_folder_id",
   "token"
  ],
  "title": "SendArgs",
  "type": "object"
 },
 "SendResult": {
  "additionalProperties": false,
  "properties": {
   "attachment_bytes_verified": {
    "title": "Attachment Bytes Verified",
    "type": "boolean"
   },
   "submitted": {
    "const": true,
    "default": true,
    "title": "Submitted",
    "type": "boolean"
   }
  },
  "required": [
   "attachment_bytes_verified"
  ],
  "title": "SendResult",
  "type": "object"
 },
 "StatusArgs": {
  "additionalProperties": false,
  "properties": {},
  "title": "StatusArgs",
  "type": "object"
 },
 "StatusResult": {
  "additionalProperties": false,
  "properties": {
   "addon_version": {
    "title": "Addon Version",
    "type": "string"
   },
   "browser_version": {
    "title": "Browser Version",
    "type": "string"
   },
   "capabilities": {
    "items": {
     "type": "string"
    },
    "title": "Capabilities",
    "type": "array"
   },
   "write_busy": {
    "title": "Write Busy",
    "type": "boolean"
   }
  },
  "required": [
   "addon_version",
   "browser_version",
   "write_busy",
   "capabilities"
  ],
  "title": "StatusResult",
  "type": "object"
 },
 "TrashArgs": {
  "additionalProperties": false,
  "properties": {
   "account_id": {
    "maxLength": 200,
    "minLength": 1,
    "title": "Account Id",
    "type": "string"
   },
   "destination_folder_id": {
    "title": "Destination Folder Id",
    "type": "string"
   },
   "mailbox": {
    "maxLength": 320,
    "minLength": 3,
    "title": "Mailbox",
    "type": "string"
   },
   "ref": {
    "$ref": "#/$defs/MessageRef"
   }
  },
  "required": [
   "account_id",
   "mailbox",
   "ref",
   "destination_folder_id"
  ],
  "title": "TrashArgs",
  "type": "object"
 },
 "Welcome": {
  "additionalProperties": false,
  "description": "The host's answer to an authenticated, compatible client.",
  "properties": {
   "addon_version": {
    "maxLength": 40,
    "title": "Addon Version",
    "type": "string"
   },
   "capabilities": {
    "items": {
     "type": "string"
    },
    "title": "Capabilities",
    "type": "array"
   },
   "host_version": {
    "maxLength": 40,
    "title": "Host Version",
    "type": "string"
   },
   "id": {
    "pattern": "^[A-Za-z0-9_-]{8,64}$",
    "title": "Id",
    "type": "string"
   },
   "t": {
    "const": "welcome",
    "default": "welcome",
    "title": "T",
    "type": "string"
   },
   "v": {
    "title": "V",
    "type": "integer"
   }
  },
  "required": [
   "v",
   "id",
   "host_version",
   "addon_version",
   "capabilities"
  ],
  "title": "Welcome",
  "type": "object"
 }
};
