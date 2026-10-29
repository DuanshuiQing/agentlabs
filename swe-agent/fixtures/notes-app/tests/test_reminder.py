import unittest
from datetime import datetime, timedelta

from notes.reminder import format_duration, next_reminder, parse_duration


class TestParseDuration(unittest.TestCase):
    def test_minutes_only(self):
        self.assertEqual(2700, parse_duration("45m"))

    def test_hours_and_minutes(self):
        self.assertEqual(5400, parse_duration("1h30m"))

    def test_pure_hours(self):
        self.assertEqual(7200, parse_duration("2h"))

    def test_leading_zero(self):
        self.assertEqual(1800, parse_duration("0h30m"))

    def test_uppercase_accepted(self):
        self.assertEqual(5400, parse_duration("1H30M"))

    def test_minutes_must_be_below_sixty(self):
        with self.assertRaises(ValueError):
            parse_duration("90m")

    def test_zero_rejected(self):
        with self.assertRaises(ValueError):
            parse_duration("0h0m")

    def test_garbage_rejected(self):
        for bad in ("", "abc", "1.5h", "30", "1h30"):
            with self.assertRaises(ValueError):
                parse_duration(bad)


class TestFormatDuration(unittest.TestCase):
    def test_hours_minutes(self):
        self.assertEqual("1h30m", format_duration(5400))

    def test_seconds_only(self):
        self.assertEqual("45s", format_duration(45))

    def test_all_three(self):
        self.assertEqual("1h1m1s", format_duration(3661))


class TestNextReminder(unittest.TestCase):
    def test_adds_duration(self):
        now = datetime(2026, 10, 1, 9, 0, 0)
        self.assertEqual(now + timedelta(minutes=30), next_reminder(now, "30m"))

    def test_bad_input_propagates(self):
        with self.assertRaises(ValueError):
            next_reminder(datetime(2026, 10, 1), "明天")


if __name__ == "__main__":
    unittest.main()
