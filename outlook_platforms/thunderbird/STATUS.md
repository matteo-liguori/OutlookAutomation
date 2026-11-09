# Thunderbird validation

9 November 2026: macOS 15.6.1 arm64, Python 3.12.16, Thunderbird 159.0,
bridge 0.2.8. The Windows Outlook path and native Mac Outlook adapter remain.
The approved isolated Thunderbird profile is configured; the original harness
profile/database and Outlook profiles are preserved. No production source IDs or
existing task records were silently reassigned.

| Capability | Evidence |
| --- | --- |
| Account/folder discovery | Normal harness commands matched verified Gmail and KCL IMAP/OAuth identities and exact server folder IDs. |
| Mail search/read | Bounded literal search, exact full synthetic body read and unchanged unread flag passed. Persisted account/folder/RFC locators resolve fresh integer IDs. |
| Send | Normal harness self-send passed for Gmail and KCL; unique Inbox delivery and server Sent copy readback verified. KCL needed its actual `/Sent Items` folder. Submission alone is not delivery. |
| Filing | Gmail/KCL pilot filing passed; normal KCL filing passed source absence, unique destination, unread preservation and repeat suppression. A Gmail move-to-Trash stalled/finished asynchronously; the uncertain attempt was not reissued. |
| Recoverable deletion | Normal `trash` commands passed Gmail and KCL, with active-source absence and recoverable Trash/Deleted Items copies. Read-only reconciliation and repeated-request suppression passed. Multiple source copies and Gmail label merging have regression coverage. |
| Calendar | Normal commands created/read/updated/deleted timed, all-day and daily recurring events in the verified Gmail CalDAV calendar. Three occurrences across 27–29 March 2027 retained noon London time across DST; all-day end remained exclusive. Repeated creates were suppressed. |
| Collection | Immutable snapshot pagination passed live and offline. Provider freshness is explicitly unknown, so run coverage remains partial. |
| Safety/lifecycle | Exact SQLite authority, changed-scope refusal, durable uncertain writes and responsive reads during stalled writes passed. |
| Python crash | macOS report identified buffered-input finalization abort in the native host. Replaced the daemon buffered reader with cancellable raw pipe reads and joined shutdown. Six subprocess lifecycle scenarios pass: normal EOF, partial frame, oversized frame, closed output pipe, roundtrip and in-flight disconnect. No newer Python crash report appeared during final validation. |
| Cleanup | All five mail fixtures absent from active sources; recoverable copies retained. Three new calendar fixtures and two older native-Outlook fixtures were removed and independently read back absent. Finite test grants retired; harness doctor passed. |

After integrating current main, the full synthetic regression run completed 385
tests: 365 passed and 20 skipped (Windows/PowerShell and optional viewer checks). The optional iCloud CalDAV test module was
excluded because its separate `caldav` dependency is not installed. Subsequent
focused checks cover cancellation-status ingestion, final pre-dispatch busy
handling, transport lifecycle and package contents. JavaScript VM regressions cover identity isolation, unread
restoration, canonical request replay, duplicate Trash copies, merged differing
read flags, uncertain send suppression and read-only move reconciliation.

KCL calendar connectivity is unresolved; the calendar helper itself is tested
against connected Gmail CalDAV. Provider sync is not certified by a local readback.
Gmail can merge read and unread views during deletion; the Trash receipt reports
`read_state_preserved: false` instead of altering an ambiguous existing recovery
copy. Ordinary filing still requires preserved read state. External recipients,
attachment export/send, draft save/update, contacts, invitation responses,
individual recurrence exceptions and Windows runtime remain unimplemented or
unvalidated in this Thunderbird path and are not advertised as working commands.

App control was used for one-off bridge installation/updates and early synthetic
mail cleanup. The final send/trash/reconciliation and calendar validation used
harness commands. Routine operations require Thunderbird running, with no cursor,
screenshot or manual UI sequence. [Repeatable setup and contracts](README.md).
