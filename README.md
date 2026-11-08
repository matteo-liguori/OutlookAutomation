# Outlook automation

Reusable Outlook helpers and a SQLite mail/calendar follow-up harness.

- [Windows setup and user profile](packaging/README.md)
- [Optional Mac pilot](packaging/macos/README.md)
- [Assistant instructions](AGENTS.md)

Keep private profiles, records, credentials and output outside Git. Code defaults
use the effective permission boundaries shown by `monitor.ps1 help permissions`;
a user's private profile may override them. Specific grants remain in SQLite.

Run `Test-Harness.ps1` for offline regression checks. Build source-only packages
with `python build_share_package.py` (`--platform macos` for the Mac option).
