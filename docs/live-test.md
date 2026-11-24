# Live test

`chamberlain live-test` checks that the whole harness works against your real mailboxes and
calendars. It sends marked test mail between accounts you own, files, marks and trashes it,
saves drafts, makes marked calendar events, then removes everything it made and gives a
score. It is off until you switch it on.

It uses the same tools an agent uses (`start`, `find`, `open`, `keep`, `card`, `propose`,
`do`, `reconcile`, `doctor`), so the harness itself is what gets tested, not a shortcut.

## What it touches

Only the sources you list in `live_test` (see `docs/configuration.md`).

- **Mail it makes.** Every message has a subject starting `CHAMBERLAIN LIVE TEST <run id>`.
  It sends only to your own addresses (the ones you list). It never sends to anyone else.
  One scenario tries a send to `someone@example.invalid` and checks that it is refused
  before anything leaves.
- **Events it makes.** Every title starts with the same marker. Events carry no guests
  except your own address in the invitation scenario.
- **Your other mail and events.** Never changed. The tick that runs inside tool calls may
  read your inbox, as it always does. Reading leaves mail unread.
- **The store.** One test card (`CHAMBERLAIN LIVE TEST card`) holds the run. Each run adds
  one action to it and finishes it at the end. A few extra operations, evidence rows and
  grants appear in the store and age out by the usual lifetime rules. The send to the
  stranger leaves one proposal waiting for confirmation that nobody will confirm; it lapses
  after seven days.

## Authority: how it is allowed to send

A send to an address the mailbox does not know needs your confirmation in the viewer, and
the runner never confirms anything itself.

1. Switch on `live_test` and list the sources (`docs/configuration.md`).
2. Run `chamberlain live-test grant`. It creates the test card and proposes one standing
   grant for each listed mailbox (a grant can cover only one source). Each grant covers only:
   - sends to your listed addresses,
   - draft saves, filing, trashing and marking read,
   - the test card,
   - that mailbox's own folders.
3. Run `chamberlain viewer`, read each grant and confirm it once.

If a grant is missing or waiting, `chamberlain live-test run` stops before doing anything and
says so. If you change the config (for example add an address), run `grant` again and
confirm the new grant.

**Calendar edits** cannot be covered by a standing grant. Each create, update and delete
needs its own approval. The runner will not invent one. Set
`live_test.approve_calendar_edits: true` in `connections.yaml` if you want calendar tests to
run unattended; the runner then records evidence for each operation that names that setting
and the run id. Or pass `--approve-calendar-edits` for one run. Without either, the calendar
scenarios are skipped and say why.

## Running it

```
chamberlain live-test grant                  # once, then confirm in the viewer
chamberlain live-test run                    # everything that can run
chamberlain live-test run --dry-run          # show the plan; touch nothing
chamberlain live-test run --only mail.reply  # only scenarios whose id contains this
chamberlain live-test run --skip calendar    # leave some out
chamberlain live-test run --keep             # leave the items for you to look at
chamberlain live-test sweep                  # remove marked items from earlier runs
chamberlain live-test report                 # print the latest report again
```

`--only` and `--skip` take a word (matches inside the scenario id) or a pattern with `*`.
Both can be repeated. A full run takes a few minutes: mail has to arrive.

Keep these limits in mind:

- A mailbox may send 20 messages an hour. One run sends about 14 from each mailbox in a
  two-account setup. Run it once an hour at most. If the limit is reached the scenario is
  skipped and says so.
- Thunderbird must be open and connected. A source that is not healthy is skipped.

## What each scenario checks

Mail, once per mailbox unless noted:

| Scenario | What it proves |
| --- | --- |
| `mail.self_send` | A send to yourself arrives, and Sent holds exactly one copy. |
| `mail.cross_send` (per pair) | A send to your other account arrives there. |
| `mail.reply_thread` (per pair) | A reply arrives with `In-Reply-To` naming the original. |
| `mail.draft_flow` | A draft saves, re-saves in place (one draft), and sends. |
| `mail.attachment` | An attachment comes back with the same SHA-256. |
| `mail.read_state` | Opening leaves mail unread; mark read and unread work. |
| `mail.move_back` | Filing to the scratch folder and back keeps the handle. |
| `mail.trash_restore` | Trash and restore work. |
| `mail.find_by_message_id` | `find` shows one hit per Message-ID across folders. |
| `mail.repeat_do` | Repeating `do` or `propose` never sends twice. |
| `mail.refuse_non_owned` | A send to a stranger is refused and nothing is sent. |
| `mail.watermark_suggestion` (per pair) | Arriving mail is picked up and the reply gets the test card as its suggested card. |
| `mail.fault_lost_response` | After a dropped response the send settles or stays uncertain, with exactly one Sent copy. |
| `mail.outbox_queued` | Manual: the steps are in the report. Always skipped. |

Calendar, once per listed calendar (all need the calendar approval above):

| Scenario | What it proves |
| --- | --- |
| `calendar.create_update_delete` | Create, update (etag changes) and delete. |
| `calendar.all_day` | An all-day event keeps its dates. |
| `calendar.timezone` | Nine o'clock London keeps its instant either side of a clock change. |
| `calendar.recurring` | Skipped: recurrence not supported by v2 EventFields. |
| `calendar.invite_flow` | Invite another owned account, check it arrives, answer from its side, see the answer on the organiser's copy, cancel and check the cancellation. Skipped unless the calendar declares `calendar.invite`. |

Harness:

| Scenario | What it proves |
| --- | --- |
| `harness.guidance_budgets` | `start`, `next` and `guide` stay within the guidance and reply budgets. |
| `harness.doctor_clean` | After the run, `doctor` shows no failure and no new warning, and no operation of the run is still unsettled. |

Anything that cannot run here is **skipped with a reason**, never dropped silently: a source
that is missing or unhealthy, a missing capability, no pair configured, no scratch folder,
calendar approval off.

The lost-response test needs the Thunderbird native host to load the fault hook, so restart
Thunderbird once after updating. If the hook never fires the scenario is skipped and says so.
It works only when the bridge folder is `<data root>/bridge` (the default).

## Things to know before the first live run

- **The send to a stranger leaves a proposal.** `mail.refuse_non_owned` proposes a send to
  `someone@example.invalid`. The harness cannot withdraw a proposal, so one grant stays in
  `proposed` state (you may see it in the viewer) until it lapses after seven days. Do not
  confirm it.
- **Suggested card needs a clear match.** `mail.watermark_suggestion` fails, naming the
  reason, if another open card also sent to the same address; the harness then rightly
  suggests nothing.
- **Reading is normal.** Every tool call runs the tick, which scans your inbox for new mail
  as it always does. The run does not mark any event handled, so your real new-mail batch
  is left for your agent.
- **Gmail** keeps one message in several folders (Inbox, All Mail, Sent). The scenarios
  count copies per folder and accept the merged-copy warning on trash. The default scratch
  folder is the archive folder (All Mail on Gmail); set `scratch_folder` to a label of your
  own if you prefer.
- **Invitations** are checked on the invitee's calendar when `invitee_calendar` is set,
  otherwise only on the invitee's mailbox, and the response is read back from the
  organiser's copy. The read-only probe fills in the answer if `open` does not show it.
- **Restart Thunderbird** after updating the harness so the native host loads the
  lost-response hook.

## How it cleans up

Every scenario registers its cleanup before it creates anything, and runs it even if the
scenario fails: marked mail on both sides goes to the trash, marked events are deleted, test
attachments are released. After all scenarios a second pass tries again, then an audit looks
at every listed source and counts what is left (anything marked and not in the trash, or
any live marked event). Left-behind items count as failures in the score.

`sweep` does the same for earlier crashed or `--keep` runs. It touches only items whose
subject or title opens with `CHAMBERLAIN LIVE TEST <run id>` (after any `Re:`). It reads the
message headers through `open` and checks again before each trash. Anything else, however
a search returns it, is left alone. Trashed mail stays in the trash folder; empty it
yourself when you like.

## Reading the score

The summary table lists each scenario as `PASS`, `FAIL` or `skip` with seconds and the
reason. The last line reads like this:

```
Score 93%: 28 passed, 2 failed, 5 skipped (coverage 85%, left behind 0).
```

- **Score** is passed out of passed plus failed. Items left behind count as failures.
- **Coverage** is how many planned scenarios actually ran. Skips lower coverage, not the
  score, so look at both. A score of 100% with 40% coverage means little.
- **Exit status** is 0 when nothing failed, 1 when something failed, 2 when the run could
  not start (live test off, a grant missing or waiting).

Each run writes `logs/live-test/<run id>.json` and `.md` in the data root. They hold
handles, error codes, counts and timings, never subjects, bodies or addresses. They are kept
14 days, up to 20 MB, like other logs. `chamberlain viewer` lists recent runs on the
**Live test** page. A check that needed the read-only probe (because the tools cannot
answer it, for example the `In-Reply-To` header) is named in the report.

## Safety rules the runner keeps

- It never confirms a grant and never calls a confirmation function (a test enforces this).
- It sends only to the addresses you listed. A pair or an address that is not listed fails
  the config check.
- All writes go through `propose` and `do`. If a write is not covered by a confirmed grant,
  the scenario fails and says to run `grant`.
- A fault file is read only by the native host, only for a `send` whose subject carries
  this run's marker, only for ten minutes, and only once.

## Self-test

`tests/livetest/` runs the whole suite against the fake world: two owned fake mailboxes
that really deliver mail to each other, two fake calendars, and the same runner. The test
plays the owner and confirms the grants, because the runner never does. It must be green
in the gate.
