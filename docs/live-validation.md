# Live validation checklist

Run once before cutover, with the owner present. Writes go only to the disposable
test account `mail:test` (and its Google calendar); real accounts are read-only.
Every capability stays at `implemented` until its row here passes; then it is raised
to `live_verified`. Record each result on the board with the date.

## 0. Set up beside the old harness (no disruption to the daily agent)

1. Create a separate data root for the new harness (never the old one).
2. `python -m chamberlain.connectors.thunderbird.setup` into that root: it registers
   the `chamberlain_thunderbird_bridge` native host and builds the add-on.
3. Install the add-on in Thunderbird from file. It has its own ID, host name and
   privileged APIs (`chamberlainMail`, `chamberlainCalendar`), so the old add-on keeps
   working for the daily agent.
4. `chamberlain import <copy of old data root>` into the new root (a trial import
   against a copy already passed: 170 cards, 42 grants, 81 operations, 7 uncertain).
   Then `chamberlain config validate` and `chamberlain doctor --metrics`.

## 1. Mail (Thunderbird bridge v2)

| # | Check | Account |
| --- | --- | --- |
| 1.1 | Read, search, collect; read flag unchanged by reads, attachment reads (`getFull`) | real (read-only) and test |
| 1.2 | Mark read; move and trash with verified read-back; Gmail merged trash warning | test |
| 1.3 | Send to self: duplicate scan, `X-Chamberlain-Write-Token` survives into Sent, Thunderbird does not keep a supplied Message-ID, reconcile by content | test |
| 1.4 | Queued send visible in Outbox reports `queued`, never frees the grant | test |
| 1.5 | Draft save, reopen and re-save: old copy replaced; which Message-ID the new copy has | test |
| 1.6 | Lost response during a send (stop Thunderbird mid-send): one Sent copy, operation settles by reconcile | test |

## 2. Calendars

| # | Check | Calendar |
| --- | --- | --- |
| 2.1 | Google: link the test account (one consent click), create/update/delete an attendee-less event, If-Match on update, `events.import` behaviour, what a deleted event reads as | test account's Google calendar |
| 2.2 | iCloud CalDAV: `credential`, `check`, `register`; read-only query of real calendars; `<C:expand>` for series | owner's iCloud (read-only) |
| 2.3 | Owl add-on: `calendar_api.js` loads (module paths), occurrence `icalString` is the occurrence, add/modify/delete complete with prompt read-back, etag stable across a restart, any `fresh_as_of` source | KCL calendar: read-only, plus one self-created test event deleted afterwards |

### 2b. Invitations (the harness cannot see delivery; these are checked by eye or by the live suite)

| # | Check | Calendar |
| --- | --- | --- |
| 2.4 | Google: invite the owner's other address; the invitation arrives in that account's calendar and mail; accept it there with `calendar.respond`; the organiser's copy shows the answer; change the time (answer resets); cancel (the copy shows cancelled) | test account to the owner's second Google account |
| 2.5 | Google: `events.insert` with a client-chosen `id` (32 hex characters) is accepted, `sendUpdates=all` sends the mail, a repeat id answers 409, `iCalUID` becomes `<id>@google.com` | test account |
| 2.6 | iCloud: invite the owner's other address; whether iCloud delivers it into the calendar or the inbox; whether it rewrites ATTENDEE parameters (SCHEDULE-STATUS) so the etag moves after the write; accept it as the invitee and see the organiser's copy change; delete as organiser sends a cancellation | owner's iCloud to another account |
| 2.7 | iCloud: the address the account sends as (`calendar-user-address-set`) equals the configured `address` | owner's iCloud (read-only) |
| 2.8 | Owl: a write with attendees refuses at propose with `invitations-not-supported` and changes nothing | KCL calendar (nothing written) |

### 2c. Repeating events (`chamberlain live-test run --only calendar.recurring` runs the write side)

| # | Check | Calendar |
| --- | --- | --- |
| 2.9 | Google: `events.import` accepts `recurrence` with an explicit `timeZone`; a window read with `singleEvents` lists the dates; PUT and DELETE on an instance id make an exception; a PUT of the series keeps an exception when only the title changes; deleting the series cancels its dates | test account |
| 2.10 | iCloud: a series written with `DTSTART;TZID=Europe/London` and the generated VTIMEZONE is accepted; `<C:expand>` keeps nine o'clock across a clock change, the first date included (Radicale does not, so the contract test checks only the zone and the first date as written); an override VEVENT and an EXDATE are kept and read back; `If-Match` on the resource covers both | owner's iCloud: one self-created series, deleted afterwards |
| 2.11 | Owl: a window read lists the dates of a series with `recurrence_id`; opening the series' own id returns the master; no write is offered (no `calendar.recurrence` or `calendar.occurrence`) | KCL calendar (read-only) |

## 3. Harness behaviour

| # | Check |
| --- | --- |
| 3.1 | `chamberlain viewer`: token, confirm page shows exact content, 409 on changed content |
| 3.2 | A standing grant and an unknown-recipient send both stop for viewer confirmation |
| 3.3 | Scheduled pass via `chamberlain tick`; wrong-day run stops; report artifact kept and later swept |
| 3.4 | One full mail pass by an agent through the MCP or CLI surface; compare tokens with the old harness on the same mailbox |

## 4. After

Remove `mail:test` from both profiles and the account from Thunderbird; parallel
read-only running for a day; then the cutover steps in [cutover](cutover.md).
