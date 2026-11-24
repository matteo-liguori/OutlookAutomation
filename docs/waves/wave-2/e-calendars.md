# Wave 2, track (e): calendars

You give the harness its calendars: Google Calendar through its own API, iCloud and
any standard server through CalDAV, and the Microsoft 365 calendar through
Thunderbird with Owl. The agent never learns which one is behind a source (principle
1); every calendar is a `CalendarPort` with honest capabilities. Read `AGENTS.md`,
`docs/BOARD.md` (including the R1 to R11 rulings), then the contracts below.

## Binding contracts

| File | What binds you |
| --- | --- |
| `docs/contracts/08-ports.md` | `CalendarPort`, capabilities, verification levels, reconcile (P-1 to P-15). |
| `docs/contracts/04-writes.md` | prepare / dispatch / reconcile split, uncertain writes, immediate settle (R6). |
| `docs/contracts/05-grants.md` | Attendee calendar writes need human confirmation. |
| `docs/contracts/09-storage.md` | Bindings, secrets (S-17), migrations `0040`-`0049`. |
| `docs/contracts/12-bridge-v2.md` | The bridge calls you use for the Owl calendar. |
| `docs/contracts/11-ported-test-intent.md` | The calendar rows. |

## You own

`src/chamberlain/connectors/google_calendar/`, `src/chamberlain/connectors/caldav/`,
the calendar half of the Thunderbird connector as agreed with its owner on the board
(`connectors/thunderbird/calendar.py` and its add-on module), `tests/calendars/`,
migrations `0040`-`0049`.

## Build

1. Google Calendar `CalendarPort`: list, query (paged, singleEvents), read by iCalUID,
   create via `events.import`, update preserving fields the iCalendar text does not
   describe (reminders, colour, EXDATE/RDATE), delete; 5xx and 408 on a write mean
   "may have happened"; 4xx means nothing happened. Port from the old
   `monitor/adapters/google/calendar.py` and its tests, including the fixes made on
   2026-11-24 (merge_update, `_failed_write`, verification read failure).
2. Google OAuth: installed-app loopback flow with PKCE, stray-request tolerant, tokens
   in the OS credential store via `keyring` with a restricted file fallback. Guided
   setup command (`client`, `link`, `check`, `pages`) ported from
   `monitor/adapters/google/setup.py`.
3. CalDAV `CalendarPort` for iCloud and others, with an app-specific password in
   `keyring`; guided setup ported from `monitor/integrations/icloud_setup.py`
   (credential checked against the server before saving; profile registration).
   The contract suite runs against a local Radicale server started by a test fixture.
4. Owl / Thunderbird calendar: narrow capability set (no invitations), version
   handshake, calendar writes refused for events with attendees or an organiser.
5. Every connector passes `CalendarPortContract` and declares capabilities and
   verification levels truthfully (`implemented` until live validation).

## Done means

The gate passes; all three connectors pass the calendar contract suite (Google against
an in-memory fake of the API, CalDAV against Radicale, Owl against the fake bridge);
no secret is written to a log or a plain file when `keyring` is available.

## Do not touch

Shared files without a board entry, other tracks' directories, the old repository and
the owner's data. No live calls to Google, iCloud or Thunderbird: live validation is the
coordinator's job.
