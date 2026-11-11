# Thunderbird validation

9 November 2026: macOS 15.6.1 arm64, Python 3.12.16, Thunderbird 159.0,
bridge 0.3.17. The Windows Outlook path and native Mac Outlook adapter remain.
The approved isolated Thunderbird profile is configured; the original harness
profile/database and Outlook profiles are preserved. No production source IDs or
existing task records were silently reassigned.

| Capability | Evidence |
| --- | --- |
| Account/folder discovery | Normal harness commands matched verified Gmail and KCL IMAP/OAuth identities and exact server folder IDs. |
| Mail search/read | Bounded literal search, exact full synthetic body read and unchanged unread flag passed. Persisted account/folder/RFC locators resolve fresh integer IDs. |
| Send | Normal harness self-send passed for Gmail and KCL; unique Inbox delivery and server Sent copy readback verified. KCL needed its actual `/Sent Items` folder. Submission alone is not delivery. |
| Filing | Gmail/KCL pilot filing passed; normal KCL filing passed source absence, unique destination, unread preservation and repeat suppression. A Gmail move-to-Trash stalled/finished asynchronously; the uncertain attempt was not reissued. Exact source/destination provider refresh now precedes reconciliation when cached headers lag. |
| Recoverable deletion | Normal `trash` commands passed Gmail and KCL, with active-source absence and recoverable Trash/Deleted Items copies. Read-only reconciliation and repeated-request suppression passed. Multiple source copies and Gmail label merging have regression coverage. |
| Calendar | Normal commands created/read/updated/deleted timed, all-day and daily recurring events in the verified Gmail CalDAV calendar. Three occurrences across 27–29 March 2027 retained noon London time across DST; all-day end remained exclusive. Repeated creates were suppressed. |
| Collection | Immutable snapshot pagination passed live and offline. Provider callback plus recent stable Inbox samples now support freshness; incomplete sync remains partial. |
| Safety/lifecycle | Exact SQLite authority, changed-scope refusal, durable uncertain writes and responsive reads during stalled writes passed. |
| Python crash | macOS report identified buffered-input finalization abort in the native host. Replaced the daemon buffered reader with cancellable raw pipe reads and joined shutdown. Six subprocess lifecycle scenarios pass: normal EOF, partial frame, oversized frame, closed output pipe, roundtrip and in-flight disconnect. No newer Python crash report appeared during final validation. |
| Attachments | Gmail/KCL synthetic binary attachments crossed multiple native frames, downloaded with exact SHA-256, and survived draft editing/sending. Shared case-cache registration and cache-hit reads passed. |
| Drafts/replies/priority | Gmail and KCL drafts were saved, version-bound updates read back, and high-priority self-mail verified. Actual reply headers and MIME subjects are checked, including Thunderbird's omitted Re: metadata and Exchange's HTML-only drafts. Saved draft outcomes are reconciled without repeating writes. |
| Sent destination | A live KCL warning confirmed SMTP succeeded but its inherited `/Sent` copy failed. Every composer now explicitly overrides and verifies the bound `/Sent Items` target before submission. A new KCL reply then saved/sent/verified without that warning. The earlier uncertain send was independently verified delivered in the self Inbox, despite its missing local Sent copy; it was not reissued. |
| Contacts/transport | Explicitly bound books plus per-account recipient-cache lookup passed on both accounts. Case-insensitive related-subject duplicate checks found prior synthetic sends; fresh exact grants were blocked before submission. Inbox provider callbacks plus two stable samples passed both sources. |
| Cleanup | Prior baseline mail/calendar fixtures remain cleaned up. All six new core mail fixtures are absent from Inbox/Sent/Drafts after 11 exact recoverable Trash commands. Twenty-five finite grants were retired; the synthetic case cache was removed on closure and harness doctor passed. No new calendar tests or KCL calendar changes were needed. |

After integrating current main, the full synthetic regression run completed 395
tests: 375 passed and 20 skipped (Windows/PowerShell and optional viewer checks). The optional iCloud CalDAV test module was
excluded because its separate `caldav` dependency is not installed. Subsequent
focused checks cover cancellation-status ingestion, final pre-dispatch busy
handling, transport lifecycle and package contents. JavaScript VM regressions cover identity isolation, unread
restoration, canonical request replay, duplicate Trash copies, merged differing
read flags, uncertain send suppression and read-only move reconciliation.

KCL calendar connectivity is unresolved; the calendar helper itself is tested
against connected Gmail CalDAV. Provider sync is not certified by a local readback.
Gmail can merge read and unread views during deletion; the Trash receipt reports
`read_state_preserved: false` instead of altering an ambiguous existing recovery
copy. Ordinary filing still requires preserved read state. External-recipient delivery, university directory coverage and Windows runtime are
not live certified. Contacts cover configured client data only. Invitation responses,
individual recurrence exceptions, COM restart and arbitrary stuck-Outbox requeue
remain outside this Thunderbird adapter. Native provider errors can still interrupt
a copy; the harness retains its uncertain-write guard and never blindly resends.

App control was used for one-off bridge installation/updates and early synthetic
mail cleanup, and to inspect the reported failure dialog. All current core mail
validation and fixture cleanup use the harness. The final send/trash/reconciliation and calendar validation used
harness commands. Routine operations require Thunderbird running, with no cursor,
screenshot or manual UI sequence. [Repeatable setup and contracts](README.md).
