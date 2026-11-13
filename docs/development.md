# Development

The shared core enforces the scheduled chat-date guard before database or live
access. `guard` is the read-only preflight command; `help guard` owns its procedure
and limitations. It uses each installation's configured chat-title format and timezone.

This is an alpha. Keep the common `monitor` interface stable across adapters;
internal helper paths may change. Use a development branch and preserve private data.

| Area | Owns |
| --- | --- |
| `monitor/` | Core commands, storage and UI |
| `monitor/adapters/` | Windows COM, native Mac Outlook and Thunderbird internals |
| `monitor/integrations/` | Google/iCloud integration |
| `defaults/` | Shared behaviour and instruction topics |
| `docs/` | Human setup and development guides |
| `tools/` | Testing, data relocation and source packaging |
| `tests/` | Synthetic fixtures and regression tests |
| `packaging/` | Allowlists, dependency locks and tooling metadata |

Private configuration, SQLite, executable extensions, scratch and exports belong in
the configured data root. Use `config migrate` to preview an older installation;
`config migrate --apply` makes a verified combined backup and migrates it. Keep
writers closed during migration/restore. Existing card IDs and UTC receipts remain.
Database backup files embed the connection profile and private configuration files.

Run `tools/test_harness.ps1` on Windows, or the project Python's
`-m unittest discover -s tests` with `OUTLOOK_AUTOMATION_PROFILE` set to
`tests/fixtures/profile.example.json`. Also run `node tests/Test-ThunderbirdBridge.js`
and `node tests/Test-ThunderbirdMail.js` after Thunderbird changes.
Use `python -m tools.build_share_package --output /path/to/source.zip` for a
source-only bundle. Both platforms use the same layout and bootstrap.

## Required validation after changes

After automated tests pass, run `python tools/smoke_harness.py` with the installed
runtime against the actual private configuration and database, without a synthetic
profile override. The Windows test runner does this automatically after restoring
the normal profile. It exercises configuration validation, help, capability discovery,
the task briefing and doctor without contacting or changing mail or calendars.
Then actually exercise the changed workflow and verify its result. Use a genuine
authorised local-record update when a write path needs checking; never send test
mail, change calendars or run custom actions without their applicable authority.
Report which checks passed and any unavailable external services. Automated tests
alone are not completion evidence. Run these checks again on the final installed
checkout before publishing, and after applying an update at its destination.

Prepare releases and test other branches in a separate worktree. Never switch the
checkout used by active automations to prepare a release. Offline fixtures must also
cover an existing installation moving between different sets of default settings;
a clean synthetic install cannot establish compatibility with saved configuration.

When moving files, update callers, package allowlists, setup, native-host paths and
tests together. Keep runtime procedures in YAML help topics; documentation links to
them. [Update publication](updating.md) explains explicitly tagged main updates.
