# Set up Chamberlain with the owner

Use this guide for a fresh installation or after importing a copy of a v1 data
folder. Work in one private data root outside the source checkout. Pass its
absolute path as `--data ROOT` before each command, or set `CHAMBERLAIN_DATA`
for the setup terminal. Start with the [installation instructions](../README.md).

## Choose and resume

Use the owner's provider and account choices. Ask only for choices still open.
Save those choices
with `chamberlain setup select thunderbird google icloud`, omitting providers
they do not need. Use `--google-account EMAIL` or `--apple-id EMAIL` to identify
an account before you have bound its calendar. Give email addresses only.

Run `chamberlain setup status` or just `chamberlain setup` after each step and
when resuming. `done` lists evidence observed on this run. `next` lists a step
you can do. `waiting` names the choice, credential or GUI action still needed.
Account choices survive
interruption. The status records local bindings, the local bridge handshake
and saved credentials. Live reads below establish reachability and whether
the selected bindings work.

The owner enters credentials in the provider's terminal prompt or browser.
If an account choice, sign-in or consent screen needs their attention, open
the relevant step and let them take over there. Keep passwords, refresh tokens,
client secrets and viewer tokens out of messages, command arguments and
connection files.

## Connect the selected providers

### Thunderbird

1. Use `chamberlain setup thunderbird --manifest-dir MANIFEST_DIR` to build the
   add-on and write the native-host launcher and manifest. The bridge directory
   defaults to `ROOT/bridge`. Follow the command's output and the native
   messaging instructions for the host platform.
2. Use computer control for the Add-ons Manager and account setup if the owner
   has granted it. Install the XPI, finish any sign-in with the owner and leave
   Thunderbird running. Register the native manifest as indicated for the
   platform. Do not replace another installation's host until its owner has
   chosen the switch. For an existing add-on, use `chamberlain addon update`
   and arrange the restart with the owner.
3. Check the exact Inbox, Sent, Drafts and Trash folders for each chosen
   account. Ask if the folder choice is ambiguous. Computer control can help
   inspect folders or change a name the owner requests. Copy the matching
   account and folder IDs into the private profile. Add Owl calendar IDs or
   address book IDs only if chosen.
   Use [Configuration](configuration.md) for the binding fields. Validate the
   profile and check setup status again.

### Google Calendar

1. Use `chamberlain setup google client` with the owner's Google Desktop OAuth
   client. The provider command also accepts `--from-json PATH` for the
   downloaded desktop client file. Keep that file private.
2. Use `chamberlain setup google link EMAIL` for the chosen account. Give the
   owner control of the consent screen. The command lists calendar IDs after
   a successful link.
3. Add the selected account and calendar ID to the profile under a
   `calendar:` source. Confirm the choice if several calendars fit. Use
   `chamberlain setup google check` if the owner wants to check linked accounts.
   It can check every linked Google account in the provider's credential store.

### iCloud Calendar

1. Use `chamberlain setup icloud credential --apple-id EMAIL`. Have the owner
   generate the app-specific password in Apple Account settings and enter it
   in the hidden prompt.
2. Use `chamberlain setup icloud register --apple-id EMAIL`. If there is more
   than one candidate, ask which calendar to add and repeat with
   `--calendar NAME` and the chosen `--source calendar:NAME`. The command
   keeps a backup if it changes the profile.

## Verify first use

1. Run `chamberlain config validate` and `chamberlain status`. Check the
   enabled sources, capabilities and any missing access. Use `chamberlain doctor`
   for detail. Compare the observed accounts and calendars with the owner's
   selections.
2. Supervise a read-only pass using the [scheduled pass procedure](scheduling.md).
   Set up one due run in the validation profile, run `chamberlain tick`, and
   pass the returned day to `start` with `purpose` set to `scheduled`. Read its
   coverage and find a known message or event in each agreed source. Open only
   the agreed items and report stale, incomplete or unavailable sources.
   When the next tick attaches the session to its run, `next` offers the
   `report.due` work item. Follow it, keep the report under that run, and
   verify it in the viewer. Keep read-state changes and automatic cleanup
   off in this profile.
3. Use a disposable mailbox and test calendar for the short write proof. Use
   existing test authority where it covers the selected sources and actions.
   Ask the owner about missing grants or calendar edit authority. Start with
   `chamberlain live-test run --dry-run` and repeat `--only` for each intended
   case, including its source in the pattern. For example, use
   `--only 'mail.self_send*mail:test*'` for a self-send on the test mailbox.
   Include `mail.batch_file` and `mail.trash_restore` for that mailbox and
   `calendar.create_update_delete` for a supported test calendar. Check that
   the dry run selects at least one case and exactly the intended sources
   before running those filters. Verify the results and cleanup in the report.
   See [Live test](live-test.md) for the exact scope and grant flow.

## Move an existing installation or recover a step

Use [Cutover](cutover.md) for v1. Run `chamberlain migrate detect OLD_ROOT`
to identify the installation, then `chamberlain migrate prepare OLD_ROOT`
to make the backup and import from it. Use `chamberlain migrate status OLD_ROOT`
to resume and review the import receipt. Compare a supervised read-only pass,
then run `chamberlain migrate verify OLD_ROOT` with the available verification
evidence. Pass a source-binding snapshot with `--report-snapshot PATH` if one
is available to compare.
When the owner chooses the switch, review `chamberlain migrate switch --help`
for the scheduler and sandbox arguments. Keep the old installation available
for rollback and keep only one installation writing.

For a profile version change, preview with `chamberlain config migrate`.
Use `chamberlain config migrate --apply` after reviewing the plan, keep its
backup and run `chamberlain config validate` before using the profile. Read
the notes for a published tag with `chamberlain updates notes TAG` before
applying a release or restarting the add-on.

If setup is interrupted, reopen the same data root and run setup status again.
For a bridge that does not answer, check Thunderbird, the installed add-on
and native host registration. For expired consent, repeat the provider's
sign-in and recheck it. For an invalid profile, use config validation to locate
the field and repair it or restore the last backup before retrying.
