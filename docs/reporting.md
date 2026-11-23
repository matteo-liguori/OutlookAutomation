# Reporting

Use `help reporting` for the effective template and personal reporting preferences.
Change private `customisations.yaml` rather than this shared document.

The existing local viewer's **Calendar and reports** tab opens on a seven-day
window centred two days before today. Verified calendar results build a rolling
365-day history as checks run. Uncollected dates are marked in the calendar.
Birthdays and anniversaries already present in the collected sources are visible.
All-day and multi-day entries appear above timed appointments. Important
appointments are marked once on their actual date. Acknowledgements can come due
earlier in the viewer. Hold the acknowledgement button for five seconds with a
pointer, Space or Enter to record the acknowledgement and close the due prompt.
Each run pins its calendar collection window using `schedule.calendar_days_ahead`.
Verified results update the local archive during ordinary checks. Dated actions
appear when **Show on calendar** is enabled on their card.
Imported events start neutral. Use `calendar classify RUN --input -` after review
with an assignments list containing source, event ID, start and category.
Add an optional `display_title` for a clearer dashboard label when the activity
and people are known. Use `null` to clear a label. The source invite keeps its title.
To backfill outside the usual window, use `calendar archive` for a complete
provider snapshot or `calendar archive-google` for a paged connector export.

Use **Add local event** to put an event on the harness calendar, or **Save item**
to keep an undated card. A local event is stored only in harness SQLite. For an
explicit local-only request in chat, use `calendar local-add --input -` with title,
start, end, all_day, idempotency_key and an optional location. All-day ends are
exclusive. Normal connected-calendar writes continue through their authorised
provider.

The next full briefing prompts for a read-only storage review at 1.5 GB of private
harness data. `maintenance status` shows the current count and largest folders.
Use `report publish RUN --input -` with `{"markdown": "..."}` after finalizing
to save each morning report and in-day follow-up. The viewer groups reports by
local date and displays them in run order. Publishing the same run again updates
its report in place. The delivery guide and calendar colour rules are available
in `help reporting`, with per-user overrides in private `customisations.yaml`.
In the Windows shell, use the structured stdin bridge described in `help records`.

Use `ui --dashboard` to open the same viewer directly on the calendar and reports.
For the in-app browser, start it with `--no-browser` and open the printed private
localhost URL in that browser. A scheduled agent can use `--duration-minutes`
to close the listener after the reporting day without scheduling another run.
