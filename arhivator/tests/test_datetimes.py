"""Tests for wavesnip.datetimes.

The paste behaviour is the point of this module. `design.md` requires dates to be
copy-pastable as ``D.M.Y. H:M:S:MS``, so the round trip is asserted directly, and the
cases Qt silently refuses are pinned here so the looser fallbacks stay in place.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from wavesnip.datetimes import (
    QT_DISPLAY_FORMAT,
    describe_parse_failure,
    format_datetime,
    parse_datetime,
)


class FormatTests(unittest.TestCase):
    def test_documented_format(self):
        self.assertEqual(format_datetime(datetime(2026, 5, 12, 10, 15)), "12.5.2026. 10:15:00.000")

    def test_matches_the_example_in_the_design(self):
        """`design.md`: ``D.M.Y. H:M:S:MS``, so "12.5.2026. 10:15:00.000".

        The design's own example uses a colon before the milliseconds
        (``10:15:01:001``); the dot is what Qt's ``zzz`` renders, and the
        agreement with the widget is the property worth keeping. See
        `format_datetime`.
        """
        self.assertEqual(format_datetime(datetime(2026, 5, 12, 10, 15, 0)), "12.5.2026. 10:15:00.000")

    def test_zero_pads_the_time(self):
        self.assertEqual(format_datetime(datetime(2026, 1, 2, 3, 4, 5)), "2.1.2026. 03:04:05.000")

    def test_padded_variant_also_parses(self):
        """Qt renders unpadded, but padded input must still be understood."""
        self.assertEqual(
            parse_datetime("12.05.2026. 10:15:00"), datetime(2026, 5, 12, 10, 15)
        )

    def test_single_digit_month_is_unpadded(self):
        self.assertEqual(format_datetime(datetime(2026, 1, 2, 3, 4, 5)), "2.1.2026. 03:04:05.000")

    def test_year_is_always_four_digits(self):
        self.assertIn(".0999.", format_datetime(datetime(999, 1, 1)))
        self.assertIn(".2026.", format_datetime(datetime(2026, 1, 1)))


class QtAgreementTests(unittest.TestCase):
    """The formatter must render exactly what the widget renders.

    The string on screen is the string the user copies, so if these two ever
    disagree the app contradicts its own clipboard. That is worth a test
    rather than a comment, because Qt owns the format string and could change it.
    """

    def setUp(self):
        try:
            from PySide6.QtWidgets import QApplication, QDateTimeEdit
        except ImportError:  # pragma: no cover
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])
        self.edit = QDateTimeEdit()
        self.edit.setDisplayFormat(QT_DISPLAY_FORMAT)

    def test_formatter_matches_the_widget(self):
        from PySide6.QtCore import QDateTime

        for moment in (
            datetime(2026, 5, 12, 10, 15, 0),
            datetime(2026, 1, 2, 3, 4, 5),
            datetime(1999, 12, 31, 23, 59, 59),
        ):
            with self.subTest(moment=moment):
                self.edit.setDateTime(QDateTime(moment))
                self.assertEqual(self.edit.text(), format_datetime(moment))

    def test_widget_text_parses_back_to_the_same_moment(self):
        """The round trip the design asks for, through the real widget."""
        from PySide6.QtCore import QDateTime

        moment = datetime(2026, 5, 12, 10, 15, 0)
        self.edit.setDateTime(QDateTime(moment))
        self.assertEqual(parse_datetime(self.edit.text()), moment)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_of_the_documented_format(self):
        for moment in (
            datetime(2026, 5, 12, 10, 15, 0),
            datetime(1999, 12, 31, 23, 59, 59),
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2038, 1, 19, 3, 14, 7),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(parse_datetime(format_datetime(moment)), moment)

    def test_unpadded_day_and_month_parse(self):
        """Qt's own format is unpadded, so the parser must accept it."""
        self.assertEqual(parse_datetime("12.5.2026. 10:15:00"), datetime(2026, 5, 12, 10, 15))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(parse_datetime("  12.5.2026. 10:15:00  "), datetime(2026, 5, 12, 10, 15))


class ParseVariantTests(unittest.TestCase):
    def test_seconds_omitted(self):
        self.assertEqual(parse_datetime("12.5.2026. 10:15"), datetime(2026, 5, 12, 10, 15))

    def test_hour_only(self):
        self.assertEqual(parse_datetime("12.5.2026. 10"), datetime(2026, 5, 12, 10, 0, 0))

    def test_missing_dot_before_time(self):
        self.assertEqual(parse_datetime("12.5.2026 10:15:00"), datetime(2026, 5, 12, 10, 15))

    def test_iso_with_seconds(self):
        self.assertEqual(parse_datetime("2026-05-12 10:15:00"), datetime(2026, 5, 12, 10, 15))

    def test_iso_with_t_separator(self):
        self.assertEqual(parse_datetime("2026-05-12T10:15:00"), datetime(2026, 5, 12, 10, 15))

    def test_iso_without_seconds(self):
        self.assertEqual(parse_datetime("2026-05-12 10:15"), datetime(2026, 5, 12, 10, 15))

    def test_milliseconds(self):
        self.assertEqual(
            parse_datetime("12.5.2026. 10:15:00.500"), datetime(2026, 5, 12, 10, 15, 0, 500000)
        )

    def test_comma_milliseconds(self):
        self.assertEqual(
            parse_datetime("12.5.2026. 10:15:00,250"), datetime(2026, 5, 12, 10, 15, 0, 250000)
        )


class ParseFailureTests(unittest.TestCase):
    def assertRejected(self, text: str) -> None:
        with self.subTest(text=text):
            self.assertIsNone(parse_datetime(text), f"{text!r} should not parse")

    def test_garbage(self):
        self.assertRejected("garbage")

    def test_empty(self):
        self.assertRejected("")
        self.assertRejected("   ")

    def test_date_only_is_rejected_rather_than_defaulted(self):
        """No silent 'midnight' guess -- the time matters here."""
        self.assertRejected("12.5.2026")
        self.assertRejected("12.5.2026.")

    def test_two_digit_year_is_rejected(self):
        """Guessing 20xx is worse than asking."""
        self.assertRejected("12.5.26. 10:15:00")

    def test_impossible_calendar_date(self):
        self.assertRejected("32.1.2026. 10:15:00")
        self.assertRejected("30.2.2026. 10:15:00")
        self.assertRejected("12.13.2026. 10:15:00")

    def test_impossible_time(self):
        self.assertRejected("12.5.2026. 25:00:00")
        self.assertRejected("12.5.2026. 10:75:00")

    def test_trailing_junk_is_rejected(self):
        """`12.5.2026. 10:15:00x` must not parse as if the x were not there."""
        self.assertRejected("12.5.2026. 10:15:00x")

    def test_leap_day_only_in_leap_years(self):
        self.assertEqual(parse_datetime("29.2.2024. 12:00:00"), datetime(2024, 2, 29, 12))
        self.assertRejected("29.2.2026. 12:00:00")

    def test_leading_junk_is_rejected(self):
        self.assertRejected("x12.5.2026. 10:15:00")
        self.assertRejected("on 12.5.2026. 10:15:00")


class FailureMessageTests(unittest.TestCase):
    def test_names_the_offending_text(self):
        message = describe_parse_failure("nonsense")
        self.assertIn("nonsense", message)

    def test_shows_the_expected_shape(self):
        self.assertIn("12.5.2026. 10:15:00", describe_parse_failure("nonsense"))

    def test_empty_gets_its_own_message(self):
        self.assertIn("Enter", describe_parse_failure(""))


if __name__ == "__main__":
    unittest.main()