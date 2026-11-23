# Alpha updates

`harness-update/v0.1.0-alpha.10` joins multiday calendar ribbons across refreshed
imports, synchronises the gold border and background tint for important dates,
and lets a review save clearer dashboard titles while preserving source titles.
It adds previewed edits for simple iCloud all-day series and shared guidance for
card reminders. The harness creates the local title table on normal startup.

`harness-update/v0.1.0-alpha.9` adds a local calendar and report viewer with
rolling calendar history, event colours, optional card markers and appointment
acknowledgements. Same-day follow-ups appear below the first report. Account
bindings and personal preferences stay in the installation's private files.
Refresh the platform dependency lock to install the viewer. The harness creates
the new SQLite tables on normal startup.

`harness-update/v0.1.0-alpha.8` makes ordinary reminders card/action records by
default, delivered through normal mail checks. Reminder plans expose no app rules
unless --automation-requested records an explicit request for a separate wake-up.
Existing regular mail checks and pending card reminders are preserved. The shared
procedure is in help reminders; no migration or dependency change is required.

`harness-update/v0.1.0-alpha.7` requires wrong-date chats to stop unattended runs.
A saved reminder's date, urgency or previous authorisation is not an exception.
Current direct user work remains allowed. The 30-minute prose cooldown never clears
a stop decision; CLI/programmatic/Windows preflights enforce it before task access.

`harness-update/v0.1.0-alpha.6` adds explicit YAML-owned automation bindings,
durable app schedule reconciliation with supported-tool updates/readback, dynamic
instruction/report templates and configured calendar-collector timezones. Bind each
installation's existing morning/current-day follow-up IDs in schedule.automations;
shared defaults intentionally contain no user IDs. Apply app updates through the
agent's tool bridge on the next harness interaction, not through a background watcher.

`harness-update/v0.1.0-alpha.5` makes the advisory explicit that stopping a stale
or obsolete run without finishing its original task is a valid outcome. Live user
work still continues normally and the per-chat 30-minute cooldown is unchanged.

`harness-update/v0.1.0-alpha.4` consolidates the local and published work on main:
keyword watches, unchanged-default compatibility, and mandatory installed smoke
validation after automated tests. It retains the advisory date warning from alpha.3.

`harness-update/v0.1.0-alpha.3` replaces the blocking chat-date guard with an
advisory for any mismatched dated chat. Commands continue normally, and a private
SQLite cooldown limits warnings to once per chat every 30 minutes.

`harness-update/v0.1.0-alpha.2` adds the shared core scheduled chat-date guard and
its `guard` / `help guard` commands, plus the action-owned cache and retention
enforcement already on main. The guard needs no dependency or database migration;
existing installation-specific title formats, timezone and private data are preserved.

Normal briefings check for tagged updates at most once per day, using a local
cache. `update check --force` checks now; `update status` shows cached results.
Only `harness-update/vMAJOR.MINOR.PATCH` tags (optionally `-alpha.N`, `-beta.N`,
or `-rc.N`) whose commit is on GitHub `main` qualify. Offline checks preserve work
and report their status. The checker never pulls, switches branches or runs code.
Commit messages do not mark releases. A tag without a prerelease suffix (for
example `harness-update/v1.0.0`) represents a stable version; alpha, beta and release
candidate tags are also eligible for update notices. The notice appears once per
new eligible tag, and no notice is needed when that tag is already in local history.

When an update is offered, inspect local Git changes and preserve unfinished work.
Review the tagged changes, update from `main`, install the platform dependency lock,
preview `config migrate`, apply any required migration, then run the smoke tests.
Preserve the private data root. Reconcile app schedules if effective scheduling changed.
The four-layer alpha moves internal helpers, reports schedule dates as `local_date`
with a timezone, and migrates behavioural profile fields into YAML. The migration
also updates existing Thunderbird native-host launchers. Rolling back across this
migration requires the combined private backup, not just an older Git checkout.

Your edited customisations remain in effect. When a changed default overlaps an
override, the harness shows the old override and new default for a decision.

Use a separate worktree for release preparation; keep the automation checkout on
its installed branch. Follow the required real-installation validation in
[Development](development.md) after tests and before declaring the update usable.

To publish a noteworthy update, finish and test its commit on `main`, create an
annotated `harness-update/v...` tag there with a short change/migration summary,
and push that tag explicitly. Ordinary development commits do not trigger notices.

**Alpha** means features/architecture are still changing. **Beta** usually means
the intended feature set is substantially present and being stabilised. Both are
prereleases; this project currently uses alpha.
