"""Calendar recurrence, exceptions, exclusive bounds and local date semantics."""
import unittest
import platform
try:
    from monitor.adapters.macos.calendar import expand_result
except ImportError:
    if platform.system()=='Darwin':raise
    raise unittest.SkipTest('Optional Mac calendar dependencies are not installed')


def calendar(events):
    return 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Synthetic//EN\r\n' + events.replace('\n','\r\n') + '\r\nEND:VCALENDAR\r\n'


def event(extra='', start='DTSTART:20261110T120000Z', end='DTEND:20261110T130000Z', uid='fixture'):
    return f'BEGIN:VEVENT\nUID:{uid}\n{start}\n{end}\nSUMMARY:Fixture\n{extra}\nEND:VEVENT'


class CalendarExpansionTests(unittest.TestCase):
    def expand(self, text, first='2026-11-08T00:00:00+00:00', last='2026-11-15T00:00:00+00:00', limit=100, sources=None):
        request=dict(start=first,end=last,limit=limit,timezone='Europe/London')
        result=dict(status='ok',calendar_scan_complete=True,calendar_sources=sources or [dict(id='1',calendar_id='142',icalendar=calendar(text))])
        return expand_result(result,request)

    def test_daily_series_started_before_window_is_expanded(self):
        value=self.expand(event('RRULE:FREQ=DAILY;COUNT=5'),first='2026-11-12T00:00:00+00:00',last='2026-11-14T00:00:00+00:00')
        self.assertEqual([v['start'] for v in value['items']],['2026-11-12T12:00:00+00:00','2026-11-13T12:00:00+00:00'])
        self.assertTrue(value['bounded_complete'])

    def test_exdate_rdate_and_moved_exception(self):
        master=event('RRULE:FREQ=DAILY;COUNT=4\nEXDATE:20261111T120000Z\nRDATE:20261114T120000Z')
        exception=event('RECURRENCE-ID:20261112T120000Z',start='DTSTART:20261112T150000Z',end='DTEND:20261112T160000Z')
        result=self.expand(master+'\n'+exception)
        self.assertEqual([v['start'] for v in result['items']],['2026-11-10T12:00:00+00:00','2026-11-12T15:00:00+00:00','2026-11-13T12:00:00+00:00','2026-11-14T12:00:00+00:00'])

    def test_cancelled_exception_and_exclusive_end(self):
        master=event('RRULE:FREQ=DAILY;COUNT=3')
        cancellation=event('RECURRENCE-ID:20261111T120000Z\nSTATUS:CANCELLED',start='DTSTART:20261111T120000Z',end='DTEND:20261111T130000Z')
        result=self.expand(master+'\n'+cancellation,last='2026-11-12T12:00:00+00:00')
        self.assertEqual(len(result['items']),1)

    def test_dst_retains_london_wall_time(self):
        result=self.expand(event('RRULE:FREQ=DAILY;COUNT=3',start='DTSTART;TZID=Europe/London:20270327T090000',end='DTEND;TZID=Europe/London:20270327T100000'),first='2027-03-26T00:00:00+00:00',last='2027-03-30T00:00:00+00:00')
        self.assertEqual([v['start'] for v in result['items']],['2027-03-27T09:00:00+00:00','2027-03-28T08:00:00+00:00','2027-03-29T08:00:00+00:00'])

    def test_all_day_date_bounds_across_dst(self):
        result=self.expand(event(start='DTSTART;VALUE=DATE:20261025',end='DTEND;VALUE=DATE:20261026'),first='2026-10-24T00:00:00+00:00',last='2026-10-27T00:00:00+00:00')
        item=result['items'][0]
        self.assertTrue(item['all_day'])
        self.assertEqual(item['start'],'2026-10-24T23:00:00+00:00')
        self.assertEqual(item['end'],'2026-10-26T00:00:00+00:00')

    def test_floating_dates_use_profile_timezone(self):
        result=self.expand(event(start='DTSTART:20260701T090000',end='DTEND:20260701T100000'),first='2026-07-01T00:00:00+00:00',last='2026-07-02T00:00:00+00:00')
        self.assertEqual(result['items'][0]['start'],'2026-07-01T08:00:00+00:00')

    def test_duplicate_exports_deduplicate_and_limits_are_partial(self):
        sources=[dict(id='1',calendar_id='142',icalendar=calendar(event('RRULE:FREQ=DAILY;COUNT=3'))) for _ in range(2)]
        result=self.expand('',sources=sources,limit=2)
        self.assertEqual(result['matched_count'],3)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['bounded_complete'])

    def test_bad_export_never_claims_complete(self):
        result=self.expand('',sources=[dict(id='1',calendar_id='142',icalendar='invalid')])
        self.assertFalse(result['bounded_complete'])
        self.assertTrue(result['errors'])

    def test_stale_nonrecurring_export_is_excluded_and_coverage_is_incomplete(self):
        native=dict(subject='Fixture',start='2026-11-10T12:00:00+00:00',end='2026-11-10T13:00:00+00:00',recurring=False)
        source=dict(id='1',calendar_id='142',icalendar=calendar(event()),native=native)
        current=self.expand('',sources=[source])
        self.assertTrue(current['bounded_complete'])
        for change in ({'subject':'Updated'}, {'start':'2026-11-10T12:15:00+00:00'}, {'end':'2026-11-10T13:15:00+00:00'}):
            with self.subTest(change=change):
                result=self.expand('',sources=[dict(source,native=dict(native,**change))])
                self.assertEqual(result['items'],[])
                self.assertFalse(result['bounded_complete'])
                self.assertIn('disagree',result['errors'][0])

    def test_shared_uid_in_distinct_calendars_is_not_lost(self):
        sources=[dict(id='1',calendar_id=cid,icalendar=calendar(event())) for cid in ('142','143')]
        result=self.expand('',sources=sources)
        self.assertEqual(len(result['items']),2)
        self.assertEqual({item['calendar_id'] for item in result['items']},{'142','143'})
