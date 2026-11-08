import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from icalendar import Calendar

import icloud_caldav


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


if __name__ == "__main__":
    unittest.main()
