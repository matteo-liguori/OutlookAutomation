# Development

Scheduled mail work checks the dated chat name against the configured local date
before opening the harness database or accessing Outlook. Direct user turns are
exempt. `monitor.ps1 guard` exposes the read-only decision (exit 3 means stop).
On a stale scheduled turn, cancel only its triggering app automation; do not retry
or create another wake-up. Detection uses local Codex turn attribution and thread
metadata; unavailable trigger metadata cannot classify a turn. The guard cannot
prevent the scheduler's initial model wake-up or directly delete app automations.
Windows helper access and restart also run this preflight.

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

When moving files, update callers, package allowlists, setup, native-host paths and
tests together. Keep runtime procedures in YAML help topics; documentation links to
them. [Update publication](updating.md) explains explicitly tagged main updates.
