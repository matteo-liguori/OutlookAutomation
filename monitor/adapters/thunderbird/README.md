# Thunderbird on Mac

The shared `monitor outlook` commands can use Thunderbird for mail and calendars.
Windows Outlook COM and the retained Mac Outlook native adapter keep their existing
paths. Each Mac source can override the default backend: Thunderbird mail can be
paired with an Outlook calendar. Keep Outlook's Legacy adapter as a compatibility
option; its Exchange Online dependency is being retired.

## One-off setup on another Mac

1. Install official Thunderbird. This bridge currently targets Thunderbird 159 or
   later; revalidate the optional calendar module when upgrading because it uses
   internal calendar APIs. Sign in directly in Thunderbird; the harness does not
   store or receive provider passwords or OAuth tokens. An isolated profile is
   useful for initial validation: `open -n /path/Thunderbird.app --args -no-remote
   -profile /absolute/private/profile`.
2. Add Gmail using IMAP, SSL/TLS and OAuth2. Add a Microsoft 365 work/school mailbox
   using IMAP `outlook.office365.com:993`, SSL/TLS, OAuth2; outgoing SMTP
   `smtp.office365.com:587`, STARTTLS, OAuth2. Let the user complete sign-in. A
   successful IMAP login proves mail access only. Tenant policy can still block
   other accounts; do not change OAuth application IDs to work around consent.
3. If server folders are missing, open Account Settings → Server Settings →
   Advanced and clear **Show only subscribed folders**, then restart that profile.
   In Copies & Folders, select the actual server Sent Items folder. In the tested
   Microsoft 365 account, the default `/Sent` failed, while `/Sent Items` worked.
   Display names alone can be ambiguous. Confirm Drafts and Trash separately.
   The harness also enforces the bound Sent destination on every composer, including
   reopened drafts and replies; a stale inherited `/Sent` override must not win.
4. Initialise the ordinary harness private profile/database first. Select the same
   private data directory for setup and every command, using
   `OUTLOOK_AUTOMATION_DATA` or the existing `.local/data-root.txt` pointer.
5. Run `.venv/bin/python -m monitor.adapters.thunderbird.setup`. Add `--calendar`
   to include the optional calendar module. Install the returned versioned XPI via
   Thunderbird Add-ons Manager → Tools → Install Add-on From File. Mail builds include a narrow Experiment API for native draft reopening, composer
   ownership and IMAP completion callbacks. Thunderbird therefore requests full,
   unrestricted application/computer access; obtain explicit approval before first
   installation. `--calendar` adds calendar access at the same permission level. No provider connector is required.
6. The installer registers the local native host in
   `~/Library/Mozilla/NativeMessagingHosts` (verified against the official Mac build).
   Its launcher points to this checkout's Python and the selected private data
   directory. An existing installation is preserved unless its exact previous
   launcher is explicitly named with `--replace-host-path`.
7. Configure `platform.os: macos`, `platform.outlook_backend: thunderbird` and a
   binding for each mailbox. Start with `outlook discover`; match the exact email
   identity, then set its account and Inbox IDs. `outlook folders` supplies exact
   folder IDs. Never copy account IDs from another device. `outlook discover` also lists calendar metadata when the calendar module is
   installed; bind only the user's verified calendar UUID. Discovery lists address-book
   metadata too. Bind only the user's verified books with `address_book_ids`; on a
   borrowed Mac, do not bind its owner's system address book.

Example private profile fragment (merge into the existing profile):

```json
{
  "platform": {
    "os": "macos",
    "outlook_backend": "thunderbird",
    "bindings": {
      "mail:work": {
        "account_id": "account1",
        "selected_inbox_id": "account1://INBOX",
        "sent_folder_id": "account1://Sent Items",
        "draft_folder_id": "account1://Drafts",
        "trash_folder_id": "account1://Deleted Items",
        "address_book_ids": ["verified-user-address-book-id"]
      },
      "calendar:work": {
        "backend": "native",
        "calendar_id": "23",
        "mail_source": "mail:work"
      },
      "calendar:primary": {
        "backend": "thunderbird",
        "calendar_id": "verified-local-calendar-uuid",
        "mail_source": "mail:work"
      }
    }
  }
}
```

The calendar source must also be enabled in the profile. Its provider describes
its service; the binding chooses which client handles access. Gmail calendars
use CalDAV independently of the IMAP mailbox. An IMAP account does not supply a
Microsoft 365 calendar. KCL calendar connectivity remains unresolved.

## Ordinary operation

Keep Thunderbird running in the configured profile; it need not be focused.
Use `.venv/bin/python -m monitor outlook status`, `help outlook`, and the normal
`outlook <operation> --input -` JSON interface. No app clicks, cursor movements,
screenshots or provider connector are used to execute these commands.

- Accounts, folder discovery, bounded recent/search, full-message read and snapshot
  collection use Thunderbird's APIs. Reads check account identity and preserve
  unread state. The case cache uses the existing shared thread-cache contract.
- Persist the returned `EntryId`. Its `tb:` locator combines account, folder and
  RFC Message-ID, avoiding Thunderbird's changing integer IDs after restart.
  `TransientId` and plain integer inputs are session-local diagnostics only.
  After filing, rediscover the message's new folder locator. Multiple physical
  copies sharing a folder/RFC ID are deliberately ambiguous; use a freshly
  discovered `TransientId` only for an immediate, exact copy-specific command.
- `send` takes an exact-message SQLite `approval_id`; omitted
  envelope fields come from that grant. Attachments need approved private file paths
  and SHA-256 hashes; the bridge verifies transferred and composed bytes. Optional
  `high_importance` and `reply_to_entry_id` belong in the same exact message grant.
  Replies preserve real In-Reply-To/References headers. The bound Sent folder is
  explicitly applied and checked before SMTP. Independent readback checks the
  envelope, normalised body, priority, reply header and attachment hashes.
  `send-reconcile` only reads outcomes; it never reissues a send. Self-Inbox delivery
  can prove submission even if the local Sent copy failed. External recipient
  delivery is never inferred from a Sent copy.
- `draft-save` uses an exact message grant whose `scope.actions` includes
  `save-draft`; `display_only` leaves its owned composer open. `draft-update` needs
  `update-draft` plus exact scope fields `draft_id` (returned stable EntryId),
  `draft_folder_id`, and `expected_raw_sha256` from `read` with `content_hash:true`.
  It edits that draft through Thunderbird's MIME loader, checks its current raw
  bytes, and returns the new EntryId/ContentHash. Saved draft IDs can change.
  Include `send` in the grant's actions to send the same verified saved draft.
- A draft save is attempted once, followed by bounded read-only polling while the
  provider processes MIME. `draft-reconcile` validates an uncertain saved result
  without saving again. `draft-inspect` reads only its journalled owned composer;
  `draft-discard` requires the returned `compose_hash` and closes that unchanged
  composer only after client save/send has stopped. It never deletes saved mail.
  The original write grant remains blocked after abandonment. Reconcile any saved
  copies and transport evidence before considering a fresh recovery grant.
  Automatic draft saving is paused in harness-owned composers to prevent a failed
  preflight from silently saving later. Manual user edits invalidate approval.
- `duplicates` scans bounded Sent/Drafts and sender-scoped local Outbox, comparing
  subjects without case/reply-prefix differences, exact normalised bodies and
  recipient overlap. Incomplete scans block normal submission. This diagnoses
  possible duplicates; it never removes arbitrary queued messages or flushes them.
- `contacts` searches explicitly bound client books, optionally adding that account's
  recent sent-recipient cache. It does not supply the university/global directory
  or synchronise missing contacts. `stores` and `session` expose client/account state.
- `read` supports `download_attachments`, `download_large_attachments`, `case_id`,
  `force_refresh` and `content_hash`. Binary transfers are chunked below native-frame
  limits and verified by size/SHA-256; downloads remain in private storage. Individual
  files are limited to 100 MiB; files above 10 MiB need explicit large-file opt-in.
  Case attachments use the shared disposable cache. HTML-only Exchange bodies use
  Thunderbird's parser. Search and recent previews include readable bodies and names.
- `move` takes an exact one-off scope with `operation: move-message`, `mailbox`,
  `source_folder_id`, `entry_id`, `destination_folder_id`, `internet_message_id`.
  The adapter checks the configured account, moves once, verifies source absence
  plus one destination copy, and preserves the original unread flag.
- `trash` takes the same identity fields as `move`, with `operation: trash-message`
  and the configured `trash_folder_id` as `destination_folder_id`. It moves the
  exact message into that recoverable folder; it never calls permanent deletion
  or follows a provider account preference that might delete immediately. Gmail
  can merge Inbox/Sent views into one recovery copy; if their read flags differ,
  the receipt reports `read_state_preserved: false` and leaves existing Trash
  flags unchanged. Ordinary filing still requires preserved read state.
  `move-reconcile` and `trash-reconcile` use the original `approval_id` for read-only
  outcome checks after an uncertain result; they never repeat the mutation. These
  checks refresh the exact source and destination first, because IMAP can finish
  the move before Thunderbird updates its destination cache. A failed initial
  readback gets this same bounded read-only reconciliation automatically.
- `calendar` reads a bounded window with recurrence expansion and all-day dates;
  `calendar-read` takes `event_id`. Calendar writes take `approval_id` with an
  exact scope: `operation: calendar-create|calendar-update|calendar-delete`,
  `calendar_id`, `event_id`; create/update also need a complete `icalendar` string.
  Update/delete require `expected_icalendar` copied from the current full read.
  Writes currently support whole master events/series without attendees or an
  organiser. Invitation responses and individual recurrence exceptions are not
  implemented. Timed events need explicit timezones; all-day DTEND is exclusive.
- Write attempts are journalled in SQLite and Thunderbird before mutation. A
  successful repeated request returns its receipt; an uncertain attempt blocks
  resubmission. Use `move-reconcile`/`trash-reconcile`, exact event reads and outcome evidence;
  do not create a fresh approval to blindly retry a timed-out mutation.

Calendar results describe the client's calendar, with `server_verified: false`.
Local readback does not prove remote sync. `refresh` uses the IMAP provider's completion callbacks for bound Inbox/Sent/Drafts/Trash.
A timeout or callback error returns partial coverage. `freshness` requires a successful
Inbox callback and two stable, recent samples; otherwise it reports unknown. A local
watermark alone does not certify sync. Calendar provider sync remains unverified.
Windows-specific COM restart and destructive stuck-Outbox requeue/repair are not
ported: Thunderbird diagnoses/reconciles uncertain transport without blindly
resending. Calendar invitation responses and individual recurrence exceptions
remain unsupported; KCL calendar connectivity is outside this mail work.

## Infrequent maintenance

Reauthenticate in Thunderbird when the provider requests it. After moving the
checkout, changing Python/data directories or recreating a Thunderbird profile,
rerun setup and reverify bindings. For an extension code update, build and install
the new versioned XPI; do not overwrite a cached package under the same filename.
Restart the configured Thunderbird profile after a native-host code update.
Keep the private profile, SQLite database, extension storage and native registration
together when moving an installation, preserving uncertain-write journals.
Never include them in the source package.

## Evidence and limits

See [STATUS.md](STATUS.md) for the actual live validation, including any pending
checks. A configured command in `status` means its source is bound; it does not
grant authority or certify provider availability. Windows execution still needs
Windows validation. The privileged calendar module requires revalidation against
new Thunderbird releases.
