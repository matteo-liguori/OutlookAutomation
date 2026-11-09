# Outlook automation

Reusable Outlook helpers and one shared SQLite mail/calendar follow-up harness.

- [Windows setup and user profile](packaging/README.md)
- [Native Mac setup and pilot results](packaging/macos/README.md)
- [Parallel platform adapters and command contracts](outlook_platforms/README.md)
- [Assistant instructions](AGENTS.md)

Use `monitor.ps1 outlook <operation>` on Windows or
`.venv/bin/python -m monitor outlook <operation>` on Mac. `outlook status` lists
capabilities; `help outlook` explains payloads. The private profile's `platform`
selects Windows COM or native macOS and holds verified native source bindings.
Existing helper scripts and `outlook.py` remain compatibility entry points.

Keep private profiles, records, credentials, live test receipts and mail outside
Git. Specific permissions remain in SQLite. Each platform reports incomplete
coverage and unsupported capabilities explicitly.

Run `Test-Harness.ps1` for Windows offline checks. On Mac, use Python 3.12 or later
and run `python3 setup_outlook.py --smoke-test`, then the supervised native checks
in the Mac guide. Portable tests alone do not establish native compatibility.

## Testing and contributions

Fork the repository and submit a pull request; include your Outlook version,
tested capabilities and synthetic reproduction steps. Keep mailbox contents,
profiles, credentials and live receipts out of issues and patches. Begin with
offline checks, then use your own account and explicitly authorised test fixtures.
Report suspected security issues through GitHub's private vulnerability reporting.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE.md) permits noncommercial use, modification
and sharing, including the educational and other organisations covered by its terms.
Commercial uses outside those permissions require a separate agreement with
[@matteo-liguori](https://github.com/matteo-liguori). This is source-available software.
Contributions are accepted under the same licence; dependency licences remain separate.
