# Native Outlook for Mac pilot

The bundle has separate Windows COM and macOS Apple-event adapters and one shared
SQLite harness. See `outlook_platforms/README.md` for platform command contracts,
matching helper names, result fields and remaining capability gaps.

## Setup

Use Python 3.12 or later in this folder:

```sh
python3 setup_outlook.py --smoke-test
.venv/bin/python mac_outlook.py probe
.venv/bin/python mac_outlook.py probe --check-permission
```

The user handles sign-in and macOS permission prompts. Verify their exact mailbox
before reading live data, especially on borrowed computers. Stop if another
person's account appears. Do not search other profiles or recover old data.

The probe reads application metadata. If full Xcode's `sdef` is unavailable, it
reads the dictionary declared inside Outlook's verified application bundle and
records `dictionary_source`. A dictionary or version read establishes declared
capabilities only; neither proves mailbox access.

On the tested Legacy Outlook Google integration, account collections were empty
while native folders, messages and calendars remained accessible. After verifying
the mailbox in Outlook's UI, select its Inbox and run:

```sh
.venv/bin/python mac_outlook_native.py discover --mailbox 'you@example.com'
```

Store the verified Inbox/calendar IDs in the private profile platform.bindings;
use `python -m monitor outlook status` and `help outlook` for the shared interface.
The underlying mirrored commands remain documented in
`outlook_platforms/README.md`. Leave the verified mailbox Inbox selected during
the pilot. Bindings stop if the window, selected folder, item parent, calendar ID
or calendar name changes. The Mac pilot's primary calendar name must match the
verified mailbox address. Other listed calendars have not been granted event
access merely by appearing in the metadata inventory.

## Profile and harness

Create a fresh private profile only after verifying the intended mailbox:

```sh
python3 setup_outlook.py --name 'Your name' --work-store 'you@example.com'
.venv/bin/python -m monitor doctor
.venv/bin/python -m monitor briefing
```

Existing state is preserved by refusing replacement. Optional `--data-dir` keeps
private data separate from code. Permissions remain in SQLite; mail bodies belong
to tracked thread caches. Source IDs retain the same meanings on both platforms.
Use `.venv/bin/python -m monitor` for the portable commands in MONITOR_HARNESS.md.
Agree any recurring schedule separately.

## Observed live pilot results

The supervised November 2026 pilot used macOS 15.6.1 (arm64), Python 3.12.16 and
Outlook 16.111 in Legacy mode with the user's confirmed Gmail account.

| Result | Live evidence |
| --- | --- |
| Passed | Account/window/folder binding, known-message body and recipient reads, native Message-ID, 37-folder inventory, literal search, unread filter and preview. |
| Passed | Bounded, empty and repeated mail/calendar reads with preserved mail read flags. |
| Passed | Native recurrence expansion; three daily occurrences across spring DST; a 23-hour all-day appointment; private fixture creation/update/deletion. |
| Passed | Immutable native collector paging and shared-harness ingestion; current coverage only with two stable recent Inbox freshness samples. |
| Passed | Exact approved draft save/display/update, attachment byte-hash verification, one native self-mail submission and duplicate suppression. |
| Passed with manual synchronization | Self-mail Inbox/Sent delivery and updated body/attachment hash comparison after Outlook Send & Receive. Native submission alone had left the message queued. |
| Passed | Native attachment export, isolated case cache, repeated cached reads, cache case closure and harness doctor. |
| Failed | Native move to the verified Gmail Codex Triage folder did not take effect despite Outlook accepting the command. Equivalent native AppleScript also had no effect. The adapter reports failure and blocks a blind retry. |
| Cleaned up | All created calendar fixtures and the recorded self-test mail copies were removed from active sources; finite test approvals retired. Mail deletion was normal recoverable deletion. |

The UI tool successfully compared the new synthetic message body after reopening
its view. An already open composer had displayed stale text after native update;
the native envelope and delivered body contained the updated text. Exact original
message/appointment UI comparison remains incomplete. Private authorization,
mail, receipts and databases stay outside source control.

Still untested: New Outlook after adapter implementation; Windows runtime on a
Windows host; non-self recipients; general filing and recovery after interrupted
writes; large/complex attachments; live recurring exceptions/cancellations and
meeting invitations. Recurrence exceptions, exclusions, cancellations, timezone
conversion and conflicting exports have synthetic coverage. Calendar source and
occurrence scan caps report partial results. Portable tests alone do not establish
full Mac compatibility. See `outlook_platforms/README.md` for command contracts and
current limits.

Licence: PolyForm Noncommercial 1.0.0; see the included LICENSE.md.
Commercial uses outside its permissions require agreement with the repository owner.
