import datetime as dt
import os
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import time_ranges


class TimeRangeTests(unittest.TestCase):
    def test_named_and_fixed_offset_zones(self):
        expected = dt.timedelta(hours=8)
        for value in ('Asia/Shanghai', '+08:00', 'UTC+08:00'):
            zone = time_ranges.timezone(value)
            self.assertEqual(dt.datetime(2026, 11, 19, tzinfo=zone).utcoffset(), expected)
        self.assertIsNone(time_ranges.timezone(None))
        self.assertIsNone(time_ranges.timezone('system'))
        self.assertEqual(time_ranges.timezone('UTC-05:30').utcoffset(None), -dt.timedelta(hours=5, minutes=30))

    def test_bad_timezones_are_rejected(self):
        for value in ('+24:00', '+08:60', 'Unknown/Zone'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                time_ranges.timezone(value)

    def test_today_is_a_calendar_day_not_always_86400_seconds(self):
        zone = ZoneInfo('America/New_York')
        for day, hours in ((dt.date(2026, 3, 8), 23), (dt.date(2026, 11, 1), 25)):
            now = dt.datetime.combine(day, dt.time(12), zone).timestamp()
            start, end = time_ranges.resolve_range(zone=zone, now=now)
            self.assertEqual(end - start, hours * 3600)
            self.assertEqual(time_ranges.local_datetime(start, zone).hour, 0)
            self.assertEqual(time_ranges.local_datetime(end, zone).hour, 0)

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires local timezone switching')
    def test_system_timezone_preserves_historical_dst(self):
        try:
            with patch.dict(os.environ, {'TZ': 'America/New_York'}):
                time.tzset()
                now = dt.datetime(2026, 11, 1, 12, tzinfo=ZoneInfo('America/New_York')).timestamp()
                start, end = time_ranges.resolve_range(now=now)
                self.assertEqual(end - start, 25 * 3600)
                self.assertEqual(time_ranges.local_datetime(start, None).utcoffset(), -dt.timedelta(hours=4))
                self.assertEqual(time_ranges.local_datetime(end, None).utcoffset(), -dt.timedelta(hours=5))
        finally:
            time.tzset()

    def test_nonexistent_local_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '不存在'):
            time_ranges.parse_time('2026-03-08T02:30:00', ZoneInfo('America/New_York'))

    def test_explicit_offsets_disambiguate_repeated_hour(self):
        first = time_ranges.parse_time('2026-11-01T01:30:00-04:00', None)
        second = time_ranges.parse_time('2026-11-01T01:30:00-05:00', None)
        self.assertEqual(second - first, 3600)

    def test_unix_naive_and_aware_inputs_are_equivalent(self):
        zone = ZoneInfo('Asia/Shanghai')
        stamp = dt.datetime(2026, 11, 19, tzinfo=zone).timestamp()
        for value in (str(stamp), '2026-11-19', '2026-11-19T00:00:00',
                      '2026-11-19T00:00:00+08:00', '2026-11-18T16:00:00Z'):
            self.assertEqual(time_ranges.parse_time(value, zone), stamp)

    def test_invalid_or_reversed_range_is_rejected(self):
        for value in ('nan', 'inf', '-inf', 'invalid', '1e300'):
            with self.subTest(value=value), self.assertRaises((ValueError, OverflowError, OSError)):
                time_ranges.parse_time(value, None)
        with self.assertRaises(ValueError):
            time_ranges.resolve_range('2026-11-20', '2026-11-19')


if __name__ == '__main__':
    unittest.main()
