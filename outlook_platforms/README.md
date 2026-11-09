# Platform adapters

Normal entry point: `python -m monitor outlook <operation>` in the project environment.
`monitor.ps1` remains a Windows launcher. The same operation/payload/result contract
is used wherever a capability is enabled. `outlook status` and `help outlook` expose
only usable commands. Missing/known-broken operations are absent from CLI choices.
`collect` handles preparation, freshness and optional all-page run ingestion internally;
the agent does not choose a refresh method or backend helper. Folder names `inbox`,
`sent`, `drafts` and `trash` are logical names resolved by the adapters.

Installation details are diagnostic-only: `outlook status --details`.

Private profile example:

```json
"platform": {
  "os": "macos",
  "outlook_backend": "native",
  "bindings": {
    "mail:work": {"selected_inbox_id": "131", "draft_folder_id": "127"},
    "calendar:work": {"calendar_id": "23", "mail_source": "mail:work"}
  }
}
```

Use IDs from the supervised discovery/UI check, not these illustrative IDs.
Windows uses `{"os":"windows","outlook_backend":"com","bindings":{}}`.
Bindings identify sources; they grant no permissions. Legacy single-mailbox
`outlook_native` bindings remain readable for upgrades; use platform.bindings for new setup. New platform capabilities
are added to the adapter registry. Coverage metadata is returned alongside data.

The commands below are compatibility/backend contracts, not a second required
interface for the agent.

The SQLite harness in `monitor/` is shared. Platform-specific access lives in two
parallel adapter paths:

| Mac module | Role |
| --- | --- |
| `native.py`, `native_outlook.js` | Native account, folder, message and calendar transport. |
| `commands.py` | Windows-style command arguments and JSON fields. |
| `calendar.py` | Expand Outlook's native ICS exports, including exceptions and timezones. |
| `collector.py`, `freshness.py` | Immutable collector pages, harness ingestion and stable Inbox watermarks. |
| `attachments.py` | Private attachment export, byte counts and SHA-256. |
| `operations.py`, `operations.js` | Exact approved drafts, sends, moves and durable attempt journals. |
| `test_actions.py`, `test_actions.js` | Uniquely marked private calendar/self-mail test fixtures. |

## Command correspondence

| Command | Windows | Mac |
| --- | --- | --- |
| `Inspect-Outlook` | Existing helper | Verified-account summary. |
| `Read-OutlookMail` | Existing helper | Accounts, Folders, Recent and bounded literal Search; unread filtering/preview. |
| `Get-OutlookMessage` | Existing helper | Exact EntryId, body, recipients, Message-ID, attachment export and case caching. |
| `Read-OutlookMonitorCalendar` | Existing helper | Bounded occurrences from the verified calendar, including recurrence/all-day/DST. |
| `Collect-OutlookMonitor` | Existing helper | Immutable pages and optional shared-harness ingestion. |
| `Test-OutlookMailboxFreshness` | Existing helper | Two stable recent Inbox watermarks; separate from sync completion. |
| `Save-OutlookStoreDraft` | Existing helper | Exact approved message and hashed attachments; optional display. |
| `Update-OutlookStoreDraft` | Existing helper | Exact approved draft identity and revised envelope. |
| `Send-OutlookStoreMail` | Existing helper | One approved submission; durable duplicate/uncertain-attempt protection. |
| `Move-OutlookMessage` | No existing helper | Implemented with verification, but live Gmail moves failed to take effect. Fails closed. |
| `List-OutlookCalendars` | Not exposed | Calendar metadata inventory. |

## Native bindings and reads

Verify the intended mailbox in Outlook and leave its Inbox selected. Obtain IDs:

```sh
.venv/bin/python mac_outlook_native.py discover --mailbox 'you@example.com'
```

Save verified IDs under `platform.bindings` as above. Backend compatibility calls
can also accept `--selected-inbox-id` for mail or `--calendar-id` for calendar.
These are native Outlook identifiers, not MAPI identifiers.

```sh
.venv/bin/python outlook.py Read-OutlookMail -Store 'you@example.com' -Start '2026-11-08T00:00:00+00:00' -End '2026-11-09T00:00:00+00:00'
.venv/bin/python outlook.py Read-OutlookMail -Store 'you@example.com' -Action Folders
.venv/bin/python outlook.py Read-OutlookMail -Store 'you@example.com' -Action Search -Query 'literal text' -Start '2026-11-08T00:00:00Z' -End '2026-11-09T00:00:00Z'
.venv/bin/python outlook.py Get-OutlookMessage -Store 'you@example.com' -EntryId NATIVE_ID -CaseId OPEN_CASE_ID
.venv/bin/python outlook.py Test-OutlookMailboxFreshness -Store 'you@example.com'
```

Mail windows are at most seven days; calendar windows at most 31 days. Start/End
use an exclusive end, while collector Since/Until retain Windows' inclusive Until.
Folder reads require ancestry under the verified mailbox root. Ambiguous names
need `--folder-id` from the Folders inventory. Search scans bounded mail with
`-MaxScan`; check native coverage receipts for limits. `--receipt` writes bounds,
IDs and state without message bodies. Message reads preserve read flags.

Get supports `--download-attachments`, `-DownloadLargeAttachments`, `-CaseId` and
`-ForceRefresh`. Case reads use the shared disposable thread cache; small
attachments are cached, and downloads over 10 MiB require the large-file flag.
Outlook's reported attachment size can be MIME-encoded; exported bytes and hashes
are recorded separately. Native `MessageClass` values explicitly say
`outlook-macos:message` or `outlook-macos:meeting-message`; they are not MAPI classes.

## Collection and permissions

Collect accepts Windows-style `-Since`, `-Until`, `-PageSize`, `-MaxScan`, `-Offset`
and `-SnapshotId`. Continuations replay one SQLite snapshot without rereading live
mail. `--ingest-run RUN_ID --source mail:work --freshness PROOF.json` imports it
through the shared harness. Without a matching fresh proof, coverage remains
unavailable even if the bounded scan is complete. Freshness currently probes Inbox
only; Windows topic-folder diagnostics are not mirrored.

Mac write commands require `--approval-id` naming an active exact one-off SQLite
grant. Store, wording, recipients, attachments and any supplied arguments must
match. Attachments must be private files approved by SHA-256; their decoded draft
exports are checked again before submission. Optional `scope.actions` restricts
save-draft, update-draft and send. Updates additionally bind `scope.draft_id` and
`scope.draft_folder_id`. Move grants bind operation `move-message`, mailbox,
source_folder_id, entry_id, destination_folder_id and internet_message_id.

A completed send approval cannot submit again. Timeouts/uncertain writes require
reconciliation before retry. `submitted` means queued by Outlook, not delivered.
The live attachment send required Outlook's Send & Receive UI before delivery;
automatic transport flushing is not established. Displayed composers can show
stale bodies after native updates; close/reopen and verify before manual action.

## Verified limits

Legacy Outlook 16.111 on macOS 15.6.1 passed the native tests documented in the
Mac guide. New Outlook with this adapter and Windows runtime behavior have not
been retested. The primary Mac calendar must have the exact verified mailbox name;
inventory alone grants no access to another calendar. Recurrence exports are
bounded by scan/result caps; invalid exports and conflicting versions report
incomplete coverage. General external-recipient sending, broad filing, contact
lookup, meeting invitations, automatic refresh and other Windows helpers remain
untested or unsupported. Portable tests do not establish full Mac compatibility.
