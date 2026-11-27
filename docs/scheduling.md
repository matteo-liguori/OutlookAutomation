# Scheduled runs, the viewer and reports

This page is for the person setting up Chamberlain on their own computer. The contracts are
`docs/contracts/07-attention.md` (events, sessions, R10) and `docs/contracts/05-grants.md`
(the viewer's confirmation channel).

## How a scheduled pass works

1. The operating system scheduler runs `chamberlain tick` once an hour (`tick.every_minutes` in
   the profile, default 60) for housekeeping: reminders, permission timers, recovery, noticing new
   mail. It never decides *what* is due; the schedule record in the data root does (`schedules`
   table, set from your configuration through `chamberlain.runs.schedule.apply_schedules`).
   It also runs once `presync_minutes` (set per schedule, default 5) before each pass. That tick
   asks Thunderbird to sync every mail folder the scan reads and waits for it, so the agent's pass
   finds them fresh. `schedule install` creates both from the profile: re-run it after changing
   pass times. A shorter interval only makes the viewer show new mail sooner between passes. A folder that will not sync
   within the time limit (`mail_sync.time_limit_seconds`, default 5 minutes) is left out, named in
   the report ("Partially verified") and tried again at the next tick. The limit is for all
   mailboxes together within one tool call or tick: they are synced in turn, and a mailbox the
   limit cuts off is reported as not synced and tried at the next tick. When the agent starts, any folder last synced more than
   `mail_sync.fresh_minutes` (default 10) ago is synced again first.
2. When a pass reaches its time on one of its weekdays, the tick records one run for that
   local day and attention raises a `schedule.pass_due` event. A pass first noticed more than
   six hours late is recorded as `missed`; a pass that is still waiting for its session six
   hours after its time is marked `missed` too. It is never run late. A chat with the agent
   never holds a pass back: only another scheduled pass at work does.
3. `chamberlain tick` prints `{"ran": ..., "wake": ..., "due": [{"run", "schedule", "kind",
   "day"}]}` and exits with status 10 when events wait and no scheduled pass is in progress. The wake
   agent-app automations (or any wake command, below) start the agent. `due` lists one pass at a time (the
   oldest), and none while another pass is being worked, so two passes never share a session.
4. The agent starts with `purpose: scheduled` and `scheduled_day: <day>`. A scheduled start for
   a day that is not today, or a scheduled session from an earlier day, is stopped by every
   tool with `session.stale` (R10). A late wake therefore never works a stale day.
5. While the agent works the pass, the harness keeps an outline under the run (which sections are
   the agent's, in the owner's words). The last work item of the pass is `report.due`: it points at
   the outline, and the rule `report.write` rides with it. The agent keeps its written part with
   `keep` (kind `artifact`, `owner` the run handle named in the work item). The run owns that
   report; it finishes when a report exists, is `incomplete` when its session ended without one
   (the harness then stores a short note saying so), and `missed` when its day ended first.
6. When the pass ends the harness writes its own sections (access problems, important
   appointments, permission reviews, verification) from its records and keeps them with the run
   (35 days). The agent's text is a stored item with a lifetime (30 days unless the agent asks for
   less; 14 days for a note the harness writes). The sweeper removes it, and the run row goes 35
   days after its day. The report shows below the calendar, the viewer's front page; every run and
   its state is also listed under **Schedule**. The layout is `config/report-template.yaml` (see the README, "The
   report").

## Opening the viewer

`chamberlain viewer` opens the viewer in your browser, already signed in (the token travels in
the address, never in output). A viewer that is already running is reused, not started twice.
With `viewer: open_on_start: true` in the profile, every agent session start (a scheduled pass,
a follow-up or a chat) opens it too; the viewer then keeps running after the session ends.
Live-test sessions never open it.

## Installing the tick and the agent automations

```text
chamberlain schedule install [--every-minutes N]          show the plan
chamberlain schedule install --apply [--every-minutes N]  register it, then check it is there
chamberlain schedule uninstall [--apply]                  remove it
chamberlain schedule prompts                              the agent-app automation text
```

`install` without `--apply` changes nothing. With it:

- Windows: a hidden Task Scheduler task "Chamberlain tick" runs
  `pythonw.exe -m chamberlain.surface.cli --data <data root> tick --quiet` every N minutes
  (default: the profile's `tick.every_minutes`) and once before each pass, only while you are
  logged on, and skips a run while the last is still going. A task of that name is
  saved under `<data root>/backups/` and replaced.
- macOS: a LaunchAgent `~/Library/LaunchAgents/uk.chamberlain.tick.plist`, loaded with
  `launchctl`. An existing file is saved first.

`tick --quiet` prints nothing: under `pythonw` there is no console, and printing fails. Its exit
status is unchanged (10 when events wait and no session is active, else 0).

The tick records runs, syncs folders and raises reminders. It does not start your agent. Create
one automation per pass in your agent app, with the text from `chamberlain schedule prompts`:
for each enabled schedule it prints the name, the time, the working folder (this checkout) and
the prompt. The prompt tells the agent to call `start` with purpose "scheduled" and
`scheduled_day` set to today's date in the profile's time zone, follow `next`, and keep the report
when `report.due` arrives.

A schedule applied for the first time records no run for a pass whose time today has already
gone, so switching mid-day does not produce earlier passes as due and then missed.

### Another agent, or doing it by hand

Any agent that can run a command can be woken from the tick. `chamberlain tick` (without
`--quiet`) prints JSON on stdout and exits with status 10 when work is waiting:

```text
chamberlain tick                  -> JSON on stdout, exit status 10 when work is waiting
for each entry in "due":
    start the agent with: "Do the <kind> pass. Call start with purpose scheduled and
                           scheduled_day <day>, then follow next."
```

The manual equivalents of `install`: on Windows,
`schtasks /Create /TN "Chamberlain tick" /SC MINUTE /MO 5 /TR "<pythonw> -m chamberlain.surface.cli --data <root> tick --quiet"`
(set "Run only when user is logged on" and "Do not start a new instance"); on macOS a plist with
`StartInterval` 300 running the same command. If the Mac was asleep at the pass time, the tick
after it wakes records the pass (or `missed` after six hours); nothing runs on a stale day.

## The viewer

```text
chamberlain viewer            opens http://127.0.0.1:<random port>/ in your browser
chamberlain viewer --no-open  prints the address only; the page asks for the token
chamberlain viewer --print-url  opens nothing; prints a ready address with the token in its
                              fragment, for an agent's own browser (the default under Claude Code)
chamberlain viewer --browser  opens the system browser even under Claude Code
```

Under Claude Code (the `CLAUDECODE=1` environment variable) the command does not open the system
browser. It prints the ready address, and the agent opens it in the Claude app's built-in browser
pane if it has one, otherwise gives it to you. The token is in the address fragment as before, and
every token and Origin check is unchanged.

- It listens on 127.0.0.1 only, on a random port, and takes no option to change that.
- A new random token is made at every start. It is kept in memory and in `run/viewer.token` under
  your data root (owner-only where the system allows it), and removed when the viewer stops. The
  command never prints it; the browser receives it in the address fragment, which is never sent
  over the connection, and the page removes it from the address bar at once. With `--no-open`,
  paste the contents of that file when the page asks.
- Every data request must carry the token, a POST must also carry the viewer's own origin and a
  JSON body, and the Host header must be the viewer's own. A refused request leaves an audit row.
- This stops an agent that only has the tools from confirming anything. It is not a sandbox: a
  process running as you can read the token file. (G-18 says so plainly.)
- Confirming a grant shows the exact text to be authorised and sends back the fingerprint of that
  text. If what is stored differs, nothing is granted (409) and you are asked to look again.
- The page loads nothing from the internet and builds every element as plain text.

Only a person confirms: standing grants, sends to someone not yet known, and inviting a new
stranger to an event (an address that is neither yours nor one you already write to, G-12a).
Other writes, including calendar changes on events that already have guests, follow your
configuration and need only your word in chat.

## Updates

The tag checker looks at the remote's tags at most once a day, only when the configuration passes
it a lister (`build_app(..., update_lister=git_tag_lister("origin"))`). It never pulls. When a
newer well-formed tag exists, the agent gets an `update.available` event to mention to you.
