import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from icalendar import Calendar

from monitor.integrations import icloud as icloud_caldav


SERIES = (
    b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
    b"UID:fixture\r\nDTSTART;VALUE=DATE:20220412\r\nDTEND;VALUE=DATE:20220413\r\n"
    b"SUMMARY:Fixture\r\nRRULE:FREQ=MONTHLY;INTERVAL=6;BYMONTHDAY=12\r\n"
    b"END:VEVENT\r\nEND:VCALENDAR\r\n"
)


class ICloudCalDavTests(unittest.TestCase):
    def test_event_record_is_minimal_and_structured(self):
        parsed = Calendar.from_ical(
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:test-123\r\n"
            "DTSTART:20260731T120000Z\r\n"
            "DTEND:20260731T130000Z\r\n"
            "SUMMARY:Shared lunch\r\n"
            "LOCATION:London\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        resource = Mock()
        resource.icalendar_component = parsed
        resource.url = "https://example.invalid/calendar/test-123.ics"

        record = icloud_caldav._event_record(resource, "Shared calendar")

        self.assertEqual(record["calendar"], "Shared calendar")
        self.assertEqual(record["uid"], "test-123")
        self.assertEqual(record["title"], "Shared lunch")
        self.assertEqual(record["start"], datetime(2026, 7, 31, 12, tzinfo=timezone.utc))
        self.assertFalse(record["allDay"])
        self.assertNotIn("description", record)

    def test_datetime_requires_an_offset(self):
        with self.assertRaisesRegex(ValueError, "explicit UTC offset"):
            icloud_caldav._parse_datetime("2026-07-31T12:00:00")

    def test_recurring_event_is_detected(self):
        parsed = Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:repeat-1\r\nDTSTART:20260731T120000Z\r\n"
            "DTEND:20260731T130000Z\r\nRRULE:FREQ=WEEKLY\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        component = parsed.walk("VEVENT")[0]
        self.assertTrue(icloud_caldav._is_recurring(component))

    def test_annual_month_keeps_history_and_only_the_selected_month_going_forward(self):
        import recurring_ical_events
        updated, plan = icloud_caldav._annual_month_plan(SERIES, 'fixture', 4, date(2023, 11, 1))
        self.assertEqual(plan['nextKept'], date(2024, 4, 12))
        self.assertEqual(plan['nextRemoved'], date(2024, 10, 12))
        self.assertEqual(plan['preservedDates'], [date(2022, 10, 12), date(2023, 10, 12)])
        occurrences = recurring_ical_events.of(updated).between(date(2022, 1, 1), date(2026, 1, 1))
        self.assertEqual(sorted(event.decoded('DTSTART') for event in occurrences),
                         [date(2022, 4, 12), date(2022, 10, 12), date(2023, 4, 12), date(2023, 10, 12),
                          date(2024, 4, 12), date(2025, 4, 12)])

    def test_annual_month_rejects_invites_and_stale_previews(self):
        effective = date(2023, 11, 1)
        invite = SERIES.replace(b"SUMMARY:Fixture", b"ATTENDEE:mailto:guest@example.invalid\r\nSUMMARY:Fixture")
        with self.assertRaisesRegex(ValueError, 'scheduling participants'):
            icloud_caldav._annual_month_plan(invite, 'fixture', 4, effective)
        current = Mock(status=200, raw=SERIES.decode(), headers={'ETag': '"revision"'})
        client = Mock()
        client.request.return_value = current
        calendar = Mock()
        calendar.url.join.return_value = 'https://example.invalid/fixture.ics'
        operation = icloud_caldav._change_annual_month
        preview = operation(client, calendar, 'fixture', 4, effective, None)
        self.assertTrue(preview['preview'])
        client.request.assert_called_once_with('https://example.invalid/fixture.ics', 'GET', '')
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            operation(client, calendar, 'fixture', 4, date(2023, 11, 2), preview['revision'])
        updated, _ = icloud_caldav._annual_month_plan(SERIES, 'fixture', 4, effective)
        client.request.side_effect = [current, Mock(status=204), Mock(status=200, raw=updated.to_ical())]
        self.assertTrue(operation(client, calendar, 'fixture', 4, effective, preview['revision'])['verified'])
        self.assertEqual(client.request.call_args_list[-2].kwargs['headers']['If-Match'], '"revision"')


if __name__ == "__main__":
    unittest.main()
