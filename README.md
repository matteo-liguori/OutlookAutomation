# Outlook automation

Reusable Outlook helpers and one shared SQLite mail/calendar follow-up harness.

- [Windows setup and user profile](packaging/README.md)
- [Native Mac setup and pilot results](packaging/macos/README.md)
- [Parallel platform adapters and command contracts](outlook_platforms/README.md)
- [Assistant instructions](AGENTS.md)

`outlook.py` routes matching command names through `outlook_platforms/windows/`
or `outlook_platforms/macos/`. Existing Windows PowerShell helpers remain intact;
the Mac adapter talks directly to Outlook through native Apple events.

Keep private profiles, records, credentials, live test receipts and mail outside
Git. Specific permissions remain in SQLite. Each platform reports incomplete
coverage and unsupported capabilities explicitly.

Run `Test-Harness.ps1` for Windows offline checks. On Mac, use Python 3.12 or later
and run `python3 setup_outlook.py --smoke-test`, then the supervised native checks
in the Mac guide. Portable tests alone do not establish native compatibility.
