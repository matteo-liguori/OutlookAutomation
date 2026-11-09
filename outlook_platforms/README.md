# Platform adapters

Normal entry point: `monitor.ps1 outlook <operation>` (Windows) or
`.venv/bin/python -m monitor outlook <operation>` (Mac). Use `outlook status` for
operation-specific fields and `help outlook` for the workflow. The profile chooses
the platform; the dispatcher refuses a host mismatch or an unsupported operation.

Private profile example:

```json
"platform": {
  "os": "macos",
  "outlook_backend": "native",
  "bindings": {
    "mail:work": {"selected_inbox_id": "131"},
    "calendar:work": {"calendar_id": "23", "mail_source": "mail:work"}
  }
}
```

Use IDs from the supervised discovery/UI check, not these illustrative IDs.
Windows uses `{"os":"windows","outlook_backend":"com","bindings":{}}`.
Bindings identify sources; they grant no permissions. New platform capabilities
are added to the adapter registry. Coverage metadata is returned alongside data.

The commands below are compatibility/backend contracts, not a second required
interface for the agent.

The SQLite harness in `monitor/` is shared. Platform-specific access lives in two
parallel adapter paths:

| Path | Role |
| --- | --- |
| `windows/commands.py` | Routes to the existing root PowerShell/COM helpers without changing their arguments. |
| `macos/native.py`, `macos/native_outlook.js` | Native Outlook Apple-event transport and account/folder binding. |
| `macos/commands.py` | Maps native results to the corresponding Windows helper field names. |
| `macos/test_actions.py`, `macos/test_actions.js` | Explicitly authorized, token-marked calendar and self-mail lifecycle tests. |
| `../outlook.py` | Shared command-name dispatcher, selecting the current OS by default. |

## Command correspondence

| Command | Windows | Mac pilot |
| --- | --- | --- |
| `Inspect-Outlook` | Existing helper | Verified-account summary. |
| `Read-OutlookMail` | Existing helper | `Recent`, bounded Inbox reads, optional unread filtering/preview. |
| `Get-OutlookMessage` | Existing helper | Exact native EntryId within a bounded Inbox window, body and attachment-name metadata. |
| `Read-OutlookMonitorCalendar` | Existing helper | Bounded queries on the verified mailbox-named calendar. |
| `List-OutlookCalendars` | Not yet exposed by this dispatcher | Native calendar metadata inventory. |

Mac arguments mirror Windows names where supported: `-Store`, `-Start`, `-End`,
`-Limit`, `-EntryId`, `-UnreadOnly`, `-IncludePreview`, `-Output Json`. Mac reads
also need an explicit native binding. Obtain its IDs with the read-only discovery
command after verifying the intended mailbox and selecting its Inbox in Outlook:

```sh
.venv/bin/python mac_outlook_native.py discover --mailbox 'you@example.com'
.venv/bin/python outlook.py Inspect-Outlook -Store 'you@example.com' --selected-inbox-id INBOX_ID
.venv/bin/python outlook.py Read-OutlookMail -Store 'you@example.com' -Start '2026-11-08T00:00:00+00:00' -End '2026-11-09T00:00:00+00:00' --selected-inbox-id INBOX_ID
.venv/bin/python outlook.py Read-OutlookMonitorCalendar -Store 'you@example.com' -Start '2026-11-08T00:00:00+00:00' -End '2026-11-15T00:00:00+00:00' --calendar-id CALENDAR_ID
```

IDs are platform-specific Outlook IDs, not Windows MAPI identifiers. Timestamps
must include offsets. Mac collection windows currently have a seven-day cap.
The selected Gmail Inbox stays bound to its exact mailbox window, folder ID and
message parent IDs. Other scriptable accounts cause the adapter to stop. Google
account objects can be absent even when their folders and messages are scriptable.
Mailbox-named calendar binding likewise checks the native calendar ID and name.

## Explicit capability gaps

Windows remains the existing COM implementation; its runtime was not retested on
this Mac. The Mac pilot does not implement every Windows helper or action.
`Search`/folder traversal, attachment downloads, MAPI message-class/Internet-ID
parity, collection snapshot ingestion into monitor runs, general sending/filing,
and recurrence expansion remain separate work. Unsupported actions fail before
connecting. Calendar output uses the Windows event field names but reports
`complete: false` until recurring-occurrence coverage has been verified.

Read-only native operations compare message read flags before and after reading.
The mutation test API is restricted to uniquely marked new appointments and
self-addressed test mail. Callers must record the user's exact authorization in
the harness, journal created IDs before further actions, and prevent repeated
submission. The successful local pilot used one finite SQLite grant and a private
step ledger. No mutation test runs as part of the smoke suite.
