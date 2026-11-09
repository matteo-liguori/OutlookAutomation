# Mac integration test handoff

Work on `codex/mac-integration-test`, based on the current unified `main`.
Keep reusable fixes on this branch and open a pull request when ready.

First inspect local Git changes, preserve any unfinished work, then fetch the
latest `main` (integration commit `3e1e504` or newer). Switch to this testing
branch and merge `origin/main` if it has advanced. The old Mac branch has been
merged and deleted; develop from the unified architecture.

Follow AGENTS.md. Run the setup smoke tests, then test the public
`monitor outlook` interface against this Mac’s configured account:
account/folder discovery, search, full-message reads, attachments,
collection/ingestion, freshness and calendar recurrence/timezones. Preserve
read state and private data.

Check that the agent uses common commands, platform-specific sequencing happens
internally, and unavailable operations are absent from normal help. Verify the
`/var` versus `/private/var` path fixes under the default Mac temporary directory.

Next, use existing authorised self-mail/calendar fixtures to test writes,
duplicate prevention and interrupted-operation recovery. Measure incoming
freshness and outgoing self-send delivery without manual Send & Receive.
Investigate filing separately; keep it hidden until independently verified.
If an existing fixture authorisation has expired or does not cover the test,
prepare the exact proposed test and obtain the missing permission before writing.

Fix problems within the shared interface and platform adapters, preserving
Windows compatibility. Don’t introduce another agent-facing Mac workflow.
Report what passed, failed or remains untested, and commit reusable fixes
without personal data. Keep live receipts and account identifiers in private
local records, not in commits or pull requests.
