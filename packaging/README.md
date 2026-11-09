# Outlook automation starter

Keep instructions brief and positive; state the desired behaviour, give each rule
one home and link to it elsewhere.

Classic Outlook mail/calendar helpers with a local SQLite follow-up harness.
This preview supports Windows, Classic Outlook COM and Europe/London scheduling.
The source package contains reusable code and synthetic tests. Each user starts
with their own empty database, accounts, context and permissions.

## First setup

Extract into a folder you own. Open Classic Outlook, configure your work account,
and allow it to synchronise. In PowerShell in the extracted folder:

```powershell
.\Setup-OutlookAutomation.ps1
.\Initialize-OutlookAutomation.ps1 -Name 'Your name' -WorkStore 'you@university.ac.uk'
.\Test-OutlookAutomationEnvironment.ps1 -LiveOutlook
.\Test-Harness.ps1
.\monitor.ps1 doctor
```

Setup needs Python 3.12+; `-InstallGlobalTools` optionally installs the tooling
manifest's global tools. Normal setup installs the locked Python dependencies.
Use your exact Classic Outlook account/store address for `-WorkStore`.

Open this folder in your assistant and ask it to follow AGENTS.md for a supervised
mail/calendar pass. Establish your account choices, filing preferences and send
permissions there. Agree a daily schedule once that pass works. The toolkit uses
the assistant's local shell; Google calendars additionally need its connected
Google Calendar tools. Mailbox access is through the signed-in Windows user.

## Optional sources

Supply these options during first-run initialisation:

| Option | Enables |
| --- | --- |
| `-PersonalStore 'you@example.com'` | Personal Outlook mailbox |
| `-SharedStore 'household@example.com'` | Shared Outlook mailbox |
| `-GooglePrimary` | Connected Google primary calendar |
| `-GoogleSharedCalendarId 'id@group.calendar.google.com'` | Shared Google calendar |
| `-ICloud` | Private iCloud calendar; see ICLOUD_CALDAV.md for credentials |

The private profile is `.local/monitor/profile.json`. Its source IDs `work` and
`shared` mean work and shared calendar roles, respectively. Enabled sources define
required monitoring coverage. Keep the profile stable during an open run.
`profile.example.json` is a synthetic full-topology regression fixture.

Records and permissions: MONITOR_HARNESS.md. Sending: MAIL_HELPERS.md.
Reminders live in the harness; a pass surfaces them when due.

## User profile and data

One private `<data-root>/monitor/profile.json` stores the user's name, accounts,
calendar providers, routing and preferences. `instructions` overrides individual
harness help topics; `permission_defaults` overrides generic permission boundaries.
Specific approvals and grants stay in that user's SQLite database.

The default data area is `.local/`. For a separate directory, initialise with
`-DataDirectory 'C:\path\to\private-data'` on Windows, or `--data-dir /path/to/private-data`
with setup_outlook.py on Mac. A local pointer records this choice. An existing
installation can use `python manage_data.py --to /absolute/new-data-folder` with
other harness processes stopped. It preserves records, credentials and attachments.
Set `OUTLOOK_AUTOMATION_DATA` to use that data from another checkout; environment
configuration takes precedence over the local pointer. Never initialise over it.

## Updates and sharing

Update/test code independently; keep the private profile and data directory.
This release reads the current database schema only. Git and source bundles omit
private state, credentials and generated output. Existing Git history is separate
from the source-only archive; review its privacy before sharing a repository.

Build with `python build_share_package.py --output output/OutlookAutomation-starter.zip`.
Use `--platform macos` for the optional Mac pilot from the same codebase; see
[Mac guide](macos/README.md). The archive includes a SHA-256 manifest. Native Mac
mail/calendar access still requires the supervised capability trial.

Licence: PolyForm Noncommercial 1.0.0; see the included LICENSE.md.
Commercial uses outside its permissions require agreement with the repository owner.

Optional visual viewer: install `requirements-ui.lock` using the project Python,
then run `monitor.ps1 ui`. The local browser shows current cards and action dates;
click for detail. It is read-only and does not scan Outlook. Ctrl+C stops it.
