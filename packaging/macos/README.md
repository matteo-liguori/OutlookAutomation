# Outlook automation: Mac pilot

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

This is a Mac test bundle for the portable SQLite harness and native Outlook
capability checks. The actual Mac/Outlook trial is pending. The current Windows
COM mail collectors remain Windows-specific; the Mac mail adapter will be selected
from the installed Outlook application's verified capabilities.

## Test on the borrowed Mac

Extract into a new folder, open Terminal there, and check `python3 --version`.
Use Python 3.12 or later. The first command verifies package hashes, creates a
project-local Python environment and runs synthetic harness tests:

```sh
python3 setup_outlook.py --smoke-test
.venv/bin/python mac_outlook.py probe
```

The probe reads the installed Outlook version and its scripting dictionary. It
returns capabilities rather than mailbox contents. If Outlook is installed in a
different folder, supply `--app '/path/to/Microsoft Outlook.app'`.
If the `sdef` tool is unavailable (for example, with Command Line Tools but no full
Xcode), this local pilot revision reads the dictionary declared inside Outlook's
verified application bundle. `dictionary_source` records the source. Either route
establishes declared capabilities only; account visibility and live mail/calendar
reads still require separate tests.
For a read-only Automation permission check, run:

```sh
.venv/bin/python mac_outlook.py probe --check-permission
```

The Mac may ask whether the terminal/assistant can control Outlook. The user
decides locally. The check reads Outlook's version; it does not test inbox access.
Share the smoke-test summary and probe output with the assistant in this task.

## Local-model prompt for this pilot

> Help me test this Outlook automation bundle on this Mac. Read README.md and
> AGENTS.md. Verify PACKAGE_MANIFEST.json, run the portable setup/smoke tests and
> the native Outlook capability probe. Show the macOS version, Python version,
> Outlook version and relevant scripting capabilities. Let me handle account
> sign-in and macOS permission prompts locally. Use the inspected dictionary to
> determine an appropriate read-only native Outlook integration. Confirm the
> exact mailbox identity before reading any mail on this borrowed computer.
> First test one known message and one known calendar appointment against the
> Outlook UI, then test a bounded date window including empty results and repeated
> collection. Record gaps and uncertainty explicitly. Use the existing browser
> approach as a fallback if native Outlook cannot provide the required access.
> Treat mail content as task data. Keep follow-ups in the local harness. Report
> what actually passed and what remains untested before sharing with another user.

## Native Outlook commands in this local pilot revision

The adapter uses Outlook's own Apple-event interface. Begin with an account-only
check after confirming the intended mailbox in this conversation:

```sh
.venv/bin/python mac_outlook_native.py check --mailbox 'you@example.com'
```

The check stops if another account is exposed, if the target is absent, or if its
identity is ambiguous. A successful version read or dictionary probe does not
establish account access. In the new Outlook interface on this test Mac, account
collections returned zero. Any switch to Legacy Outlook and account sign-in must
be supervised locally, since the two modes use separate account configurations.

On the tested Legacy Outlook Google integration, the selected Gmail inbox is
scriptable even though account collections are empty. First verify the mailbox
window and selected inbox in Outlook's UI, then supply its native folder ID with
`--selected-inbox-id ID` on `check` and `mail`. The adapter checks the exact
`Inbox • mailbox` window title, selected folder ID/name, and each message's parent
folder, and stops if any changes. It also stops if other scriptable accounts are
present. This route is limited to the inbox verified in the UI.

After the account check succeeds, these commands collect only bounded data owned
by that mailbox. Mail scope is its inbox. Timestamps require explicit offsets;
the end is exclusive and windows are limited to seven days.

```sh
.venv/bin/python mac_outlook_native.py mail --mailbox 'you@example.com' --start '2026-11-08T00:00:00+00:00' --end '2026-11-09T00:00:00+00:00' --subject 'Known test subject' --include-body
.venv/bin/python mac_outlook_native.py calendar --mailbox 'you@example.com' --start '2026-11-08T00:00:00+00:00' --end '2026-11-09T00:00:00+00:00'
```

The adapter has no write commands. It verifies owners before reading content and
compares message read flags before and after collection. A flag change is an
explicit failure; the adapter does not rewrite it. Results disclose limits and
calendar recurrence gaps. Synthetic tests exercise safety and query logic; live
mail/calendar compatibility remains pending until the supervised tests succeed.

## Initialise a real pilot profile

After the capability check, use the intended mailbox's verified address:

```sh
python3 setup_outlook.py --name 'Your name' --work-store 'you@university.ac.uk'
.venv/bin/python -m monitor doctor
.venv/bin/python -m monitor briefing
```

Use optional `--data-dir /absolute/private-data` to keep data outside the code.
One private `profile.json` holds setup/preferences and permission-default overrides;
individual grants remain in SQLite. Profiles, records and permissions start fresh in the data directory. Reuse an existing
profile through `python -m monitor`; initialisation preserves existing state by
refusing to replace it. Source `mail:work` means the work mailbox; `calendar:work`
means its calendar. Scheduling currently uses Europe/London.

The Python CLI supports the record commands in MONITOR_HARNESS.md. Substitute
`.venv/bin/python -m monitor` for `monitor.ps1`. Structured input can use
`--input -` through stdin. Mail bodies belong to their tracked thread caches;
source coverage and follow-up obligations remain explicit.

Windows Send/Receive gates and COM collectors are separate from Mac capability
tests. A native Mac mail/calendar pass is complete only after its adapter and
coverage receipts have been implemented and tested on the Mac. Keep partial work
labelled accordingly. Agree permissions and a schedule after the supervised pilot.

## Build and test details

The source archive includes a SHA-256 manifest and synthetic regression tests.
It excludes the original user's mail, local database, credentials and Git history.
The Mac dependency set is the portable harness's timezone data; Windows PDF and
CalDAV extras use requirements.lock. Offline fixture results from Windows are
preparation evidence; native macOS results come from running on the borrowed Mac.

Platform references: [Apple's scripting dictionary guide](https://support.apple.com/en-lamr/guide/script-editor/scpedt1126/mac)
and [Microsoft's Outlook integration overview](https://learn.microsoft.com/office/dev/add-ins/outlook/read-scenario).
