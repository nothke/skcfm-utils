"""Tests for wavesnip.viewport -- zoom, pan, clamping and axis ticks.

Pure arithmetic, so no Qt and no widget: every one of these is a claim about what
the user should see on screen, and none of them should need a window to check.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from wavesnip.model import from_us, to_us
from wavesnip.viewport import (
    DAY,
    HOUR,
    MIN,
    MIN_SPAN_US,
    SCROLL_STEPS,
    SEC,
    Limits,
    MAX_WAVE_SPAN_US,
    choose_ticks,
    choose_day_ticks,
    clamp_view,
    fit_view,
    pan_by,
    pan_to,
    scroll_from_scrollbar,
    scroll_to_scrollbar,
    zoom_about,
)

BASE = to_us(datetime(2026, 5, 12, 10, 15, 0))
YEAR_US = 365 * DAY


def limits(span_us: int = YEAR_US, start_us: int | None = None) -> Limits:
    if start_us is None:
        start_us = BASE
    return Limits(start_us=start_us, end_us=start_us + span_us, padding_us=HOUR)


class LimitsTests(unittest.TestCase):
    def test_padded_leaves_room_at_both_ends(self):
        lo, hi = limits().padded
        self.assertLess(lo, BASE)
        self.assertGreater(hi, BASE + YEAR_US)

    def test_a_zero_length_archive_is_still_a_range(self):
        """One clip, or an empty folder, must not make span() zero."""
        empty = Limits(start_us=BASE, end_us=BASE, padding_us=HOUR)
        self.assertGreater(empty.span_us, 0)
        self.assertEqual(fit_view(empty), (BASE - HOUR, BASE + HOUR))


class ExtentTests(unittest.TestCase):
    """``for_extent`` picks the margin, and it is the only place that does."""

    def test_the_margin_is_two_percent(self):
        """Only where two per cent is below the cap -- see the next test.

        The two meet at fifty hours, which is a much earlier crossing than it
        sounds: two per cent of a *day* is only twenty-nine minutes.
        """
        for span in (SEC, MIN, HOUR, 10 * HOUR, 2 * DAY):
            with self.subTest(span=span // SEC):
                limits_ = Limits.for_extent(BASE, BASE + span)
                self.assertEqual(limits_.padding_us, span // 50)

    def test_the_margin_stops_growing_at_an_hour(self):
        """One hour is a pixel in twelve hundred on a year-long archive."""
        for span in (50 * HOUR, 3 * DAY, 30 * DAY * 12, YEAR_US, YEAR_US * 10):
            with self.subTest(span=span // HOUR):
                limits_ = Limits.for_extent(BASE, BASE + span)
                self.assertEqual(limits_.padding_us, HOUR)

    def test_an_hour_of_margin_on_a_short_archive_is_absurd(self):
        """The bug the two-per-cent rule exists to fix: an hour either side of
        ten minutes of audio is nearly six times the audio."""
        limits_ = Limits.for_extent(BASE, BASE + 10 * MIN)
        self.assertLess(limits_.padding_us, 10 * MIN // 4)

    def test_the_extent_is_taken_literally(self):
        limits_ = Limits.for_extent(BASE + 3 * DAY, BASE + 9 * DAY)
        self.assertEqual(limits_.start_us, BASE + 3 * DAY)
        self.assertEqual(limits_.end_us, BASE + 9 * DAY)

    def test_a_view_always_fits_inside_the_padded_extent(self):
        """The invariant that makes clamping and scrolling trustworthy.

        Without the extra room below, an archive shorter than the minimum view
        had no legal view at all: clamping held the span at a second and the
        window hung over both ends, outside the limits it was supposed to obey.
        """
        for start, end in (
            (0, 1),                                   # empty archive
            (BASE, BASE),                             # a zero-length extent
            (BASE, BASE + SEC // 10),                 # a tenth of a second
            (BASE, BASE + SEC),                       # exactly the minimum
            (BASE, BASE + YEAR_US),
        ):
            with self.subTest(span=(end - start) // SEC):
                limits_ = Limits.for_extent(start, end)
                lo, hi = clamp_view(start, end, limits_)
                low, high = limits_.padded
                self.assertGreaterEqual(lo, low)
                self.assertLessEqual(hi, high)
                self.assertGreaterEqual(hi - lo, limits_.min_span_us)

    def test_a_degenerate_extent_still_gets_a_minimum_view(self):
        limits_ = Limits.for_extent(0, 1)
        self.assertGreaterEqual(limits_.padded_span, limits_.min_span_us)


class ClampTests(unittest.TestCase):
    def test_a_legal_range_is_untouched(self):
        lo, hi = clamp_view(BASE, BASE + HOUR, limits())
        self.assertEqual((lo, hi), (BASE, BASE + HOUR))

    def test_a_backwards_range_is_put_the_right_way_up(self):
        lo, hi = clamp_view(BASE + HOUR, BASE, limits())
        self.assertLess(lo, hi)
        self.assertEqual(hi - lo, HOUR)

    def test_zoom_is_limited_to_the_closest_allowed(self):
        lo, hi = clamp_view(BASE, BASE, limits())
        self.assertEqual(hi - lo, MIN_SPAN_US)

    def test_zoom_is_limited_to_the_whole_archive(self):
        wide = YEAR_US * 10
        lo, hi = clamp_view(BASE, BASE + wide, limits())
        self.assertEqual((lo, hi), fit_view(limits()))

    def test_a_window_off_the_front_is_pulled_back_in(self):
        limits_ = limits()
        lo, hi = clamp_view(BASE - YEAR_US, BASE - YEAR_US + HOUR, limits_)
        self.assertEqual(lo, limits_.padded[0])

    def test_a_window_off_the_back_is_pulled_back_in(self):
        end = BASE + 2 * YEAR_US
        lo, hi = clamp_view(end, end + HOUR, limits())
        self.assertEqual(hi, limits().padded[1])

    def test_clamping_preserves_the_span_where_it_can(self):
        """Panning must not quietly change the zoom level."""
        _, span = fit_view(limits())
        lo, hi = clamp_view(BASE + 10 * YEAR_US, BASE + 10 * YEAR_US + HOUR, limits())
        self.assertEqual(hi - lo, HOUR)


class ZoomTests(unittest.TestCase):
    def test_zooming_in_halves_the_span(self):
        lo, hi = zoom_about(BASE, BASE + HOUR, BASE + HOUR // 2, 0.5, limits())
        self.assertAlmostEqual((hi - lo) / HOUR, 0.5, delta=0.01)

    def test_zooming_out_doubles_the_span(self):
        lo, hi = zoom_about(BASE, BASE + HOUR, BASE + HOUR // 2, 2.0, limits())
        self.assertAlmostEqual((hi - lo) / HOUR, 2.0, delta=0.01)

    def test_the_anchor_does_not_move(self):
        """The whole point of zooming about a point rather than the centre."""
        anchor = BASE + 1234 * SEC + 567_891
        for factor in (0.5, 0.1, 0.9, 1.7):
            lo, hi = zoom_about(BASE, BASE + 10 * HOUR, anchor, factor, limits())
            before = (anchor - BASE) / (10 * HOUR)
            after = (anchor - lo) / (hi - lo)
            self.assertAlmostEqual(before, after, delta=0.01, msg=f"factor={factor}")

    def test_an_anchor_left_of_the_view_is_clamped_not_refused(self):
        """A wheel event can report x < 0; the view must not fly off."""
        lo, hi = zoom_about(BASE, BASE + HOUR, BASE - 10 * HOUR, 0.5, limits())
        self.assertEqual(lo, BASE)
        self.assertAlmostEqual((hi - lo) / HOUR, 0.5, delta=0.01)

    def test_an_anchor_right_of_the_view_is_clamped(self):
        lo, hi = zoom_about(BASE, BASE + HOUR, BASE + 99 * HOUR, 0.5, limits())
        self.assertAlmostEqual((hi - lo) / HOUR, 0.5, delta=0.01)

    def test_zooming_in_further_than_possible_stops_at_the_limit(self):
        lo, hi = zoom_about(BASE, BASE + DAY, BASE, 1e-9, limits())
        self.assertEqual(hi - lo, MIN_SPAN_US)

    def test_zooming_out_further_than_possible_stops_at_the_archive(self):
        lo, hi = zoom_about(BASE, BASE + DAY, BASE + DAY // 2, 1e9, limits())
        self.assertEqual((lo, hi), fit_view(limits()))

    def test_a_nonpositive_factor_is_rejected(self):
        with self.assertRaises(ValueError):
            zoom_about(BASE, BASE + HOUR, BASE, 0, limits())
        with self.assertRaises(ValueError):
            zoom_about(BASE, BASE + HOUR, BASE, -1, limits())

    def test_zoom_is_reversible(self):
        """Out then back in must land where it started, or dragging feels wrong.

        Kept clear of the archive's edges on purpose: a zoom-out wider than the
        archive is clamped to the archive, and once clamped there is nothing to
        come back to. That is correct behaviour, just not what this is testing.
        """
        middle = BASE + YEAR_US // 2
        start = (middle, middle + 4 * HOUR)
        anchor = middle + 2 * HOUR

        out = zoom_about(*start, anchor, 4.0, limits())
        back = zoom_about(*out, anchor, 0.25, limits())
        self.assertEqual(back, start)

    def test_zooming_out_ends_exactly_at_the_widest_view(self):
        """Otherwise the wheel can never reach the view that shows every clip."""
        middle = BASE + YEAR_US // 2
        reached = zoom_about(middle, middle + HOUR, middle + HOUR // 2, 1e9, limits())
        self.assertEqual(reached, fit_view(limits()))


class PanTests(unittest.TestCase):
    def test_panning_right_moves_forward(self):
        lo, hi = pan_by(BASE, BASE + HOUR, DAY, limits())
        self.assertEqual(lo, BASE + DAY)

    def test_panning_keeps_the_span(self):
        lo, hi = pan_by(BASE, BASE + 3 * HOUR, 5 * DAY, limits())
        self.assertEqual(hi - lo, 3 * HOUR)

    def test_panning_left_stops_at_the_start(self):
        lo, hi = pan_by(BASE, BASE + HOUR, -10 * YEAR_US, limits())
        self.assertEqual(lo, limits().padded[0])

    def test_panning_right_stops_at_the_end(self):
        limits_ = limits()
        lo, hi = pan_by(BASE, BASE + HOUR, 10 * YEAR_US, limits_)
        self.assertEqual(hi, limits_.padded[1])

    def test_centring_puts_the_target_in_the_middle(self):
        target = BASE + 40 * DAY
        lo, hi = pan_to(BASE, BASE + HOUR, target, limits())
        self.assertAlmostEqual((target - lo) / (hi - lo), 0.5, delta=0.01)

    def test_centring_keeps_the_span(self):
        lo, hi = pan_to(BASE, BASE + 5 * HOUR, BASE + DAY, limits())
        self.assertEqual(hi - lo, 5 * HOUR)


class ScrollbarTests(unittest.TestCase):
    """The scrollbar is a QScrollBar: its values are 32-bit ints.

    A year of microseconds is ~3e16, so the mapping is deliberately in steps.
    """

    def test_fully_zoomed_out_has_nothing_to_scroll(self):
        start, end = fit_view(limits())
        self.assertEqual(scroll_to_scrollbar(start, end, limits()), (0, 0, 0))

    def test_the_scrollbar_range_fits_in_an_int(self):
        start, end = BASE, BASE + DAY
        _, maximum, value = scroll_to_scrollbar(start, end, limits())
        self.assertLessEqual(maximum, 2**31 - 1)
        self.assertGreaterEqual(value, 0)
        self.assertLessEqual(value, maximum)

    def test_round_trip_through_the_scrollbar(self):
        """Position survives the trip out to an integer and back.

        The scrollbar quantises to ``SCROLL_STEPS``, so the claim is "within one
        step" rather than exact: a scrollbar that has to round is not a bug, but a
        step worth far more than a pixel would be.
        """
        limits_ = limits()
        span = DAY
        one_step = (limits_.span_us + 2 * limits_.padding_us - span) / SCROLL_STEPS

        for offset in (0, DAY, 100 * DAY, 300 * DAY):
            _, _, value = scroll_to_scrollbar(BASE + offset, BASE + offset + span, limits_)
            back, _ = scroll_from_scrollbar(value, span, limits_)
            self.assertAlmostEqual(back, BASE + offset, delta=one_step)

    def test_a_step_is_finer_than_a_minute_on_a_year_long_view(self):
        """Otherwise the scrollbar visibly jumps and feels broken."""
        limits_ = limits()
        travel = limits_.span_us + 2 * limits_.padding_us - DAY
        self.assertLess(travel / SCROLL_STEPS, MIN)

    def test_a_scrollbar_at_zero_is_the_left_edge(self):
        back, _ = scroll_from_scrollbar(0, HOUR, limits())
        self.assertEqual(back, limits().padded[0])

    def test_a_scrollbar_past_the_end_is_clamped(self):
        back, hi = scroll_from_scrollbar(SCROLL_STEPS * 5, HOUR, limits())
        self.assertEqual(hi, limits().padded[1])

    def test_scrolling_is_a_no_op_when_fully_zoomed_out(self):
        limits_ = limits()
        start, end = fit_view(limits_)
        self.assertEqual(scroll_to_scrollbar(start, end, limits_), (0, 0, 0))
        self.assertEqual(
            scroll_from_scrollbar(12345, end - start, limits_), (start, end)
        )


class WaveSpanGateTests(unittest.TestCase):
    def test_the_waveform_gate_is_a_week(self):
        """The line past which a view draws coverage instead of a waveform.

        A design line, not a cost line: the envelope is sampled, so a wide view
        costs the same as a narrow one and there is no budget left for this
        number to express. What it says is the split design.md asks for -- past
        a week the coverage map tells the story the outline no longer can.
        """
        self.assertEqual(MAX_WAVE_SPAN_US, 7 * DAY)


class TickTests(unittest.TestCase):
    def labels(self, start, end, width=900):
        return choose_ticks(start, end, width)

    def test_ticks_fall_inside_the_view(self):
        start, end = BASE, BASE + 6 * HOUR
        for instant, _label in self.labels(start, end):
            self.assertGreaterEqual(instant, start)
            self.assertLess(instant, end)

    def test_ticks_are_never_too_close_together(self):
        """A label per two pixels is unreadable and slows the paint for nothing."""
        for span in (SEC, MIN, HOUR, DAY, 30 * DAY, YEAR_US):
            ticks = self.labels(BASE, BASE + span)
            self.assertLessEqual(len(ticks), 900 // 96 + 2, f"span={span}")

    def test_a_thin_view_still_gets_a_tick(self):
        self.assertTrue(self.labels(BASE, BASE + SEC))

    def test_an_empty_view_gets_no_ticks(self):
        self.assertEqual(self.labels(BASE, BASE), [])
        self.assertEqual(self.labels(BASE, BASE + HOUR, width=0), [])

    def test_a_short_span_labels_the_time_of_day(self):
        (_, label), = self.labels(BASE, BASE + 3 * HOUR)[:1]
        self.assertIn(":", label)

    def test_a_span_of_days_labels_the_date(self):
        (_, label), = self.labels(BASE, BASE + 3 * DAY)[:1]
        self.assertIn(".", label)
        self.assertIn("2026", label)

    def test_a_year_labels_the_year(self):
        ticks = self.labels(BASE, BASE + 5 * YEAR_US)
        self.assertTrue(ticks)
        for _instant, label in ticks:
            self.assertRegex(label, r"^\d{4}$")

    def test_a_view_of_days_carries_the_year(self):
        """`12.05. 14:00` is ambiguous across an archive that spans years."""
        for _instant, label in self.labels(BASE, BASE + 3 * DAY):
            self.assertIn("2026", label)

    def test_ticks_do_not_slide_when_panning(self):
        """The property that makes a date axis usable: labels sit still.

        A tick computed from the left edge would move with the waveform, so the
        same label would describe different instants on each frame. Compared as
        label -> instant in both windows, since that is the direction the claim
        is about.
        """
        span = 4 * HOUR
        first = {label: instant for instant, label in self.labels(BASE, BASE + span)}
        moved = {label: instant for instant, label in self.labels(BASE + HOUR, BASE + HOUR + span)}

        shared = set(first) & set(moved)
        self.assertTrue(shared, "no labels in common to compare")
        for label in shared:
            self.assertEqual(first[label], moved[label], f"{label} moved while panning")

    def test_ticks_are_aligned_to_the_interval(self):
        """A whole number of the step, so gaps between labels are equal."""
        for instant, _ in self.labels(BASE, BASE + 6 * HOUR):
            self.assertEqual(instant % (30 * MIN), 0)

    def test_coarse_ticks_use_calendar_boundaries(self):
        """Not "every 372 days" -- that drifts and mislabels.

        The step is months or years, so every tick is the first of a month or year.
        """
        for span in (200 * DAY, 3 * YEAR_US, 9 * YEAR_US):
            ticks = self.labels(BASE, BASE + span)
            self.assertGreaterEqual(len(ticks), 2, f"span={span}")
            for instant, _ in ticks:
                moment = from_us(instant)
                self.assertEqual(moment.day, 1, f"span={span}: {moment}")

    def test_coarse_ticks_are_a_whole_number_of_months_apart(self):
        for span in (200 * DAY, 3 * YEAR_US, 9 * YEAR_US):
            moments = [from_us(i) for i, _ in self.labels(BASE, BASE + span)]
            gaps = {
                (b.year - a.year) * 12 + (b.month - a.month)
                for a, b in zip(moments, moments[1:])
            }
            self.assertEqual(len(gaps), 1, f"span={span}: uneven steps {gaps}")
            self.assertIn(gaps.pop(), (1, 3, 6, 12))

    def test_adding_a_month_clamps_into_a_short_month(self):
        """The reason for the calendar walk: 31 Jan + 1 month is not 3 March.

        `timedelta` would roll the day over, and a yearly axis built that way
        drifts a day every year.
        """
        from wavesnip.viewport import _add_months

        jan31 = datetime(2026, 1, 31)
        self.assertEqual(_add_months(jan31, 1), datetime(2026, 2, 28))
        self.assertEqual(_add_months(jan31, 12), datetime(2027, 1, 31))

        leap = datetime(2028, 1, 31)
        self.assertEqual(_add_months(leap, 1), datetime(2028, 2, 29))

    def test_month_walking_never_reverses(self):
        from wavesnip.viewport import _add_months

        moment = datetime(2026, 1, 31)
        for _ in range(60):
            nxt = _add_months(moment, 1)
            self.assertGreater(nxt, moment, f"stalled at {moment}")
            moment = nxt

    def test_the_first_tick_is_not_before_the_left_edge(self):
        for span in (SEC, MIN, HOUR, DAY, YEAR_US):
            start = BASE + span // 3
            for instant, _ in self.labels(start, start + span):
                self.assertGreaterEqual(instant, start, f"span={span}")

    def test_ticks_stop_at_the_right_edge(self):
        start, end = BASE, BASE + 90 * MIN
        self.assertLess(self.labels(start, end)[-1][0], end)

    def test_a_date_tick_stands_at_local_midnight(self):
        """``design.md``: the upper ticks were not aligned to days at midnight.

        A whole multiple of a day counted from the epoch is *UTC* midnight,
        which in any other timezone is some other hour -- here 02:00, an hour
        of drift again after each clock change. The label named the day and the
        tick stood two hours inside it, while the day strip and the Sunday wash
        below marked the real midnight.
        """
        for span in (2 * DAY, 3 * DAY, 6 * DAY, 30 * DAY, 60 * DAY):
            with self.subTest(days=span // DAY):
                ticks = self.labels(BASE, BASE + span)
                self.assertTrue(ticks, "a view this wide has ticks")
                for instant, label in ticks:
                    moment = from_us(instant)
                    self.assertEqual(
                        (moment.hour, moment.minute, moment.second, moment.microsecond),
                        (0, 0, 0, 0),
                        f"{moment} is not midnight",
                    )
                    self.assertEqual(label, moment.strftime("%d.%m.%Y."))

    def test_day_ticks_hold_still_while_the_view_pans(self):
        """The anchor is fixed in the calendar, not at the view's left edge, so
        panning slides the waveform under the labels instead of dragging them
        along with it."""
        here = {label: i for i, label in self.labels(BASE, BASE + 3 * DAY)}
        panned = self.labels(BASE + 7 * HOUR, BASE + 7 * HOUR + 3 * DAY)
        shared = set(here) & {label for _, label in panned}

        self.assertGreaterEqual(len(shared), 2, "the two views share whole days")
        for instant, label in panned:
            if label in shared:
                self.assertEqual(here[label], instant, f"{label} moved")

    def test_a_day_walk_is_a_calendar_walk_across_a_clock_change(self):
        """Stepping in calendar days, so the spring-forward of 2026-03-29 costs
        an hour of *sleep* and not a tick: midnight is midnight either side,
        one tick per date, none skipped."""
        start = to_us(datetime(2026, 3, 27, 12, 0))
        end = to_us(datetime(2026, 4, 2, 12, 0))
        ticks = self.labels(start, end)
        dates = [from_us(instant).date() for instant, _ in ticks]

        self.assertTrue(ticks)
        self.assertEqual(len(dates), len(set(dates)), "one tick per date")
        self.assertEqual((dates[-1] - dates[0]).days, len(dates) - 1, "no date skipped")
        for instant, _label in ticks:
            moment = from_us(instant)
            self.assertEqual((moment.hour, moment.minute), (0, 0), str(moment))
            self.assertGreaterEqual(instant, start)
            self.assertLess(instant, end)


class TickOrderTests(unittest.TestCase):
    """Ticks are consumed left to right, so they must be sorted and unique."""

    def spans(self):
        return [SEC, MIN, HOUR, 6 * HOUR, DAY, 3 * DAY, 30 * DAY, 200 * DAY, YEAR_US]

    def test_ticks_increase(self):
        for span in self.spans():
            instants = [i for i, _ in choose_ticks(BASE, BASE + span, 900)]
            self.assertEqual(instants, sorted(instants), f"span={span}")

    def test_no_tick_repeats(self):
        for span in self.spans():
            instants = [i for i, _ in choose_ticks(BASE, BASE + span, 900)]
            self.assertEqual(len(instants), len(set(instants)), f"span={span}")

    def test_every_label_is_nonempty(self):
        for span in self.spans():
            for instant, label in choose_ticks(BASE, BASE + span, 900):
                self.assertTrue(label.strip(), f"span={span} at {instant}")


class DayTickTests(unittest.TestCase):
    """Tests for :func:`choose_day_ticks` -- day+weekday labels, no zoom scaling.

    The day strip shows only when the view is zoomed in to at most seven days
    (``design.md``). It always uses a daily step (one tick per day), showing
    every day in the view; past a week it shows *nothing*, rather than months
    (the upper strip's job) or a wall of overlapping dates. The first tick is
    at midnight of the day containing the view start, which may be slightly
    before the view start.
    """

    def labels(self, start, end, width=900):
        return choose_day_ticks(start, end, width)

    def test_ticks_cover_the_view(self):
        """Every day in a one-week view is labeled; first tick may be before start."""
        start, end = BASE, BASE + 7 * DAY
        ticks = self.labels(start, end)
        # First tick is at midnight of the start day (<= start)
        self.assertLessEqual(ticks[0][0], start)
        # Last tick is before end
        self.assertLess(ticks[-1][0], end)
        # Midnight of the start day plus the seven following days = 8 midnights
        self.assertEqual(len(ticks), 8)

    def test_seven_days_is_the_limit(self):
        """`design.md`: "up to 7 days". Seven days exactly, yes; a moment more, no."""
        self.assertTrue(self.labels(BASE, BASE + 7 * DAY))
        self.assertEqual(self.labels(BASE, BASE + 7 * DAY + 1), [])

    def test_nothing_zoomed_out_is_nothing(self):
        """Past a week the day strip is empty -- atoms of day labels would be a
        wall of overlapping text, and months belong to the upper strip."""
        for span in (8 * DAY, 10 * DAY, 30 * DAY, 100 * DAY, YEAR_US):
            self.assertEqual(
                self.labels(BASE, BASE + span),
                [],
                f"span={span} should hide the day strip",
            )

    def test_labels_contain_weekday(self):
        start, end = BASE, BASE + 7 * DAY
        for _instant, label in self.labels(start, end):
            self.assertIn(".", label)  # date
            self.assertTrue(any(w in label for w in (
                "Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"
            )), f"no weekday in {label}")

    def test_unpadded_day_and_month(self):
        """Format is D.M.Y. not DD.MM.YYYY. -- matches design.md example."""
        start, end = BASE, BASE + 7 * DAY
        for _instant, label in self.labels(start, end):
            # First part before space is the date
            date_part = label.split()[0]
            # Should not have leading zeros
            self.assertNotRegex(date_part, r"^0\d\.")

    def test_an_empty_view_gets_no_ticks(self):
        self.assertEqual(self.labels(BASE, BASE), [])
        self.assertEqual(self.labels(BASE, BASE + HOUR, width=0), [])

    def test_ticks_are_aligned_to_day_boundaries(self):
        for instant, _ in self.labels(BASE, BASE + 7 * DAY):
            moment = from_us(instant)
            self.assertEqual(moment.hour, 0)
            self.assertEqual(moment.minute, 0)
            self.assertEqual(moment.second, 0)

    def test_ticks_do_not_slide_when_panning(self):
        """Day ticks sit on absolute calendar grid, not relative to view edge."""
        span = 3 * DAY  # daily ticks
        first = {label: instant for instant, label in self.labels(BASE, BASE + span)}
        moved = {label: instant for instant, label in self.labels(BASE + DAY, BASE + DAY + span)}

        shared = set(first) & set(moved)
        self.assertTrue(shared, "no labels in common to compare")
        for label in shared:
            self.assertEqual(first[label], moved[label], f"{label} moved while panning")

    def test_step_is_always_daily_while_the_strip_is_shown(self):
        """Where the day strip shows, it shows every day, never skipping days."""
        for span in (2 * DAY, 5 * DAY, 7 * DAY):
            ticks = self.labels(BASE, BASE + span, width=800)
            instants = [i for i, _ in ticks]
            self.assertGreater(len(instants), 1, f"span={span}")
            for a, b in zip(instants, instants[1:]):
                self.assertEqual(b - a, DAY, f"span={span}")


if __name__ == "__main__":
    unittest.main()
