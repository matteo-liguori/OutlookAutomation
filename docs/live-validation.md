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
