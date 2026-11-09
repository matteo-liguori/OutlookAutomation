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
4. Initialise the ordinary harness private profile/database first. Select the same
   private data directory for setup and every command, using
   `OUTLOOK_AUTOMATION_DATA` or the existing `.local/data-root.txt` pointer.
5. Run `.venv/bin/python -m outlook_platforms.thunderbird.setup`. Add `--calendar`
   to include the optional calendar module. Install the returned versioned XPI via
   Thunderbird Add-ons Manager → Tools → Install Add-on From File. Mail-only uses
   normal read/move/send permissions; recoverable Trash uses the move permission. Calendar uses an Experiment API and Thunderbird
   therefore requires full, unrestricted application/computer access; obtain the
   user's explicit approval for that build. No provider connector is required.
6. The installer registers the local native host in
   `~/Library/Mozilla/NativeMessagingHosts` (verified against the official Mac build).
   Its launcher points to this checkout's Python and the selected private data
   directory. An existing installation is preserved unless its exact previous
   launcher is explicitly named with `--replace-host-path`.
7. Configure `platform.os: macos`, `platform.outlook_backend: thunderbird` and a
   binding for each mailbox. Start with `outlook discover`; match the exact email
   identity, then set its account and Inbox IDs. `outlook folders` supplies exact
   folder IDs. Never copy account IDs from another device. `outlook discover` also lists calendar metadata when the calendar module is
   installed; bind only the user's verified calendar UUID.

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
        "trash_folder_id": "account1://Deleted Items"
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
  envelope fields come from that grant. Outgoing attachments are not implemented.
  A submitted message still needs independent Sent/delivery verification.
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
  outcome checks after an uncertain result; they never repeat the mutation.
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
Local readback does not prove remote sync. Standard mail APIs also provide no
server-sync completion proof: `freshness` reports unknown, and collected runs
remain partial rather than claiming current provider coverage. Attachment file
export, outgoing attachments, draft save/update, contacts and duplicate cleanup remain
unimplemented and are not advertised as commands.

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
