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

Passed: native account/window binding; known-message full read with sender,
recipient and attachment-name metadata; bounded mail/calendar queries; empty
results; repeated reads; preserved mail read flags; native calendar inventory;
new private test appointment creation, title/time update and deletion; one
self-addressed test draft, native submission, verified Inbox delivery, body check,
preserved unread flag, and normal deletion of the recorded Inbox/Sent test copies.
All created test objects were tracked and their removal from active sources was
verified. Local authorization and results stay outside source control.

Still untested: full message/appointment body comparison against the Outlook UI,
which became unavailable through the UI tool; New Outlook after the new adapter
was implemented; recurrence expansion, all-day/timezone edge cases; general
non-self sending, attachment downloads and other Windows-only helpers. Native
source coverage is bounded and partial. Portable tests alone do not establish
full Mac compatibility.
