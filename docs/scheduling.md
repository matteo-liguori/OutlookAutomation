# Scheduled runs, the viewer and reports

This page is for the person setting up Chamberlain on their own computer. The contracts are
`docs/contracts/07-attention.md` (events, sessions, R10) and `docs/contracts/05-grants.md`
(the viewer's confirmation channel).

## How a scheduled pass works

1. The operating system scheduler runs `chamberlain tick` every few minutes. It never decides
   *what* is due; the schedule record in the data root does (`schedules` table, set from your
   configuration through `chamberlain.runs.schedule.apply_schedules`).
   Five minutes before a pass is due (`presync_minutes`, set per schedule) that tick also asks
   Thunderbird to sync every mail folder the scan reads and waits for it, so the agent's pass finds
   them fresh. The scheduler should therefore tick at least that often. A folder that will not sync
   within the time limit (`mail_sync.time_limit_seconds`, default 3 minutes) is left out, named in
   the report ("Partially verified") and tried again at the next tick; a tick waits for one mail
   source about that long at most. When the agent starts, any folder last synced more than
   `mail_sync.fresh_minutes` (default 10) ago is synced again first.
2. When a pass reaches its time on one of its weekdays, the tick records one run for that
   local day and attention raises a `schedule.pass_due` event. A pass first noticed more than
   six hours late is recorded as `missed`; it is never run late.
3. `chamberlain tick` prints `{"ran": ..., "wake": ..., "due": [{"run", "schedule", "kind",
   "day"}]}` and exits with status 10 when events wait and no session is active. The wake
   adapter below starts the agent for each entry in `due`.
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
   days after its day. The report is the viewer's front page; every run is also listed under
   **Runs and reports**. The layout is `config/report-template.yaml` (see the README, "The
   report").

## The wake adapter

The adapter is the one agent-specific piece. It only has to start your agent once per due run and
tell it the run's `day`. The shape, for any agent command, is:

```text
chamberlain tick                  -> JSON on stdout, exit status 10 when work is waiting
for each entry in "due":
    start the agent with: "Do the <kind> pass. Call start with purpose scheduled and
                           scheduled_day <day>, then follow next."
```

### Windows: Task Scheduler

`C:\Users\you\chamberlain-wake.ps1` (set your agent command on the marked line):

```powershell
$env:CHAMBERLAIN_DATA = "C:\Users\you\ChamberlainData"
$json = & chamberlain tick
$wake = $LASTEXITCODE
if ($wake -eq 10) {
    foreach ($pass in ($json | ConvertFrom-Json).due) {
        $task = "Do the $($pass.kind) pass. Call start with purpose scheduled and " +
                "scheduled_day $($pass.day), then follow next."
        & your-agent-command $task      # <- your agent's non-interactive command
    }
}
```

Register it to run every five minutes while you are signed in:

```text
schtasks /Create /TN "Chamberlain tick" /SC MINUTE /MO 5 ^
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\you\chamberlain-wake.ps1"
```

Remove it with `schtasks /Delete /TN "Chamberlain tick" /F`. Use "Run only when user is logged
on" so the task can reach your credential store.

### macOS: launchd

`~/Library/LaunchAgents/uk.chamberlain.tick.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>uk.chamberlain.tick</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>/Users/you/chamberlain-wake.sh</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CHAMBERLAIN_DATA</key><string>/Users/you/ChamberlainData</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/Users/you/ChamberlainData/logs/tick.err</string>
</dict>
</plist>
```

`/Users/you/chamberlain-wake.sh`:

```sh
#!/bin/sh
out=$(/Users/you/chamberlain/.venv/bin/chamberlain tick)
status=$?
if [ "$status" -eq 10 ]; then
  echo "$out" | /usr/bin/python3 -c '
import json, sys
for p in json.load(sys.stdin)["due"]:
    print(p["kind"], p["day"])
' | while read -r kind day; do
    your-agent-command "Do the $kind pass. Call start with purpose scheduled and scheduled_day $day, then follow next."
  done
fi
```

Load it with `launchctl load ~/Library/LaunchAgents/uk.chamberlain.tick.plist` and unload it with
`launchctl unload`. If the Mac was asleep at the pass time, the tick that runs after it wakes
records the pass (or `missed` after six hours); nothing runs on a stale day.

## The viewer

```text
chamberlain viewer            opens http://127.0.0.1:<random port>/ in your browser
chamberlain viewer --no-open  prints the address only; the page asks for the token
```

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

Only a person confirms: standing grants, sends to someone not yet known, and calendar changes on
events with attendees (G-12a). Other writes follow your configuration.

## Updates

The tag checker looks at the remote's tags at most once a day, only when the configuration passes
it a lister (`build_app(..., update_lister=git_tag_lister("origin"))`). It never pulls. When a
newer well-formed tag exists, the agent gets an `update.available` event to mention to you.
