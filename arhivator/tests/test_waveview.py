"""The wave view's own behaviour: geometry, gestures, and painting.

Split out from the window tests because none of this needs a window. A view with
no archive, no peaks and no jobs is the smallest thing that can answer "does the
zoom hold still under the pointer", and the window tests have a hundred other
things to be about.

The painting tests run under ``RuntimeWarning`` as an error, which is the whole
point of them. A 2026 timestamp in microseconds is about 1.8e15, which does not
fit the 32-bit ints that ``QRect``, ``QScrollBar`` and integer ``Signal`` all take.
Passing one produces a shiboken ``RuntimeWarning: overflow converting to
C long``, which does not raise on its own -- it prints once and the widget is left
half-drawn. Treating the warning as the error it is turns a silent wrong picture
into a failing test, and ``grab()`` forces a paint even though nothing is on
screen to trigger one.
"""

from __future__ import annotations

import os
import unittest
import warnings

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QImage, QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    from wavesnip.model import TimeBlock
    from wavesnip.peaks import Peaks
    from wavesnip.timeline import Gap
    from wavesnip.viewport import Limits
    from wavesnip.waveview import WaveView, WaveViewState

SEC = 1_000_000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
YEAR = 365 * DAY

#: A mid-2026 instant, so nothing in the tests accidentally relies on small
#: timestamps being safe to hand to Qt.
BASE = 1_778_573_700 * SEC

WIDTH = 640
HEIGHT = 200


class ViewCase(unittest.TestCase):
    def setUp(self):
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])
        self.view = WaveView()
        self.view.resize(WIDTH, HEIGHT)
        self.addCleanup(self.view.deleteLater)
        # Somewhere well inside the middle, away from both limits, so clamping
        # cannot move the anchor and make a passing test a coincidence.
        self.set_view(BASE, BASE + DAY)

    def set_view(self, start, end, within=None):
        """Show ``[start, end)``, allowed to roam within ``within``.

        The roamable extent matters more than it looks: ``clamp_view`` keeps the
        requested span and slides the window to fit inside the padded limits, so a
        view that already covers the whole archive cannot move at all. Pan and
        scroll tests have to give it somewhere to go.
        """
        low, high = within if within is not None else (start, end)
        self.view.set_limits(Limits.for_extent(low, high))
        self.view.set_view(start, end)

    def set_state(self, **kw):
        start, end = self.view.view()
        self.view.set_state(WaveViewState(start_us=start, end_us=end, **kw))

    # -- event helpers ----------------------------------------------------
    #
    # `modifiers=None` rather than `Qt.NoModifier` as a default: a default is
    # evaluated when the class body runs, which happens at *import* time, so any
    # Qt constant in one would make the whole module unimportable on an
    # interpreter without PySide6 -- and an import error looks like a code fault
    # rather than a skip.

    def _pos(self, x):
        return QPointF(float(x), self.view.height() / 2)

    def press(self, x, button=Qt.LeftButton, modifiers=None):
        self.view.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                self._pos(x),
                QPointF(float(x), 0),
                button,
                button,
                modifiers or Qt.NoModifier,
            )
        )

    def move(self, x, button=Qt.LeftButton, modifiers=None):
        self.view.mouseMoveEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseMove,
                self._pos(x),
                QPointF(float(x), 0),
                Qt.NoButton,
                button,
                modifiers or Qt.NoModifier,
            )
        )

    def release(self, x, button=Qt.LeftButton, modifiers=None):
        self.view.mouseReleaseEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                self._pos(x),
                QPointF(float(x), 0),
                button,
                Qt.NoButton,
                modifiers or Qt.NoModifier,
            )
        )

    def click(self, x, button=Qt.LeftButton, modifiers=None):
        self.press(x, button, modifiers)
        self.release(x, button, modifiers)

    def drag(self, x0, x1, button=Qt.LeftButton, modifiers=None):
        self.press(x0, button, modifiers)
        self.move(x1, button, modifiers)
        self.release(x1, button, modifiers)

    def wheel(self, x, delta_y):
        """A wheel event with a vertical angle delta, as a real wheel sends."""
        delta = QPoint(0, delta_y)
        self.view.wheelEvent(
            QWheelEvent(
                self._pos(x),
                QPointF(float(x), 0),
                delta,
                delta,
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
        )

    # -- painting ---------------------------------------------------------

    def pixels(self):
        """Render the widget and return its pixels as (h, w, 4) uint8."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            pixmap = self.view.grab()
        image = pixmap.toImage()
        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        width, height = image.width(), image.height()
        pointer = image.constBits()
        buffer = np.frombuffer(bytes(pointer), dtype=np.uint8)
        return buffer.reshape(height, image.bytesPerLine() // 4, 4)[:, :width, :]

    def colour_count(self, colour):
        pixels = self.pixels()[:, :, :3].astype(np.int32)
        target = np.array([colour.red(), colour.green(), colour.blue()])
        return int(np.all(pixels == target, axis=-1).sum())

    def render(self, **state):
        """Render with these state fields, and return just the colour channels.

        The waveform is drawn with a gradient, so no single colour identifies it --
        only the difference from a render without peaks does. That difference is
        also the honest thing to assert on: the claim is not "a colour appears" but
        "the picture changed, here, and not there".
        """
        self.set_state(**state)
        return self.pixels()[:, :, :3].copy()

    @staticmethod
    def changed(a, b, left=0, right=None):
        """Which columns differ between two renders, over a band of rows."""
        right = a.shape[1] if right is None else right
        return np.any(a[:, left:right] != b[:, left:right], axis=-1)

    # -- fixtures shared by everything that paints or clicks ---------------

    @staticmethod
    def flat_peaks(count, low=-0.5, high=0.5, band=None):
        """A waveform of one amplitude: loud and quiet are the only interesting
        kinds, and a real file's envelope would say neither clearly.

        ``band`` is an RMS level to draw inside the outline; ``None`` (the
        default) is an outline-only envelope, which is legal and must stay so.
        """
        return Peaks(
            np.full(count, low, dtype=np.float32),
            np.full(count, high, dtype=np.float32),
            0.0,
            None if band is None else np.full(count, band, dtype=np.float32),
        )

    @staticmethod
    def block(name, start, end, episode=1):
        return TimeBlock(
            name=name,
            start_us=start,
            end_us=end,
            episode_num=episode,
            description="",
            hosts=[],
            guests=[],
        )

    def assigned(self, blocks):
        """The row assignment the window would hand to the view."""
        return WaveView.assign_time_block_rows(blocks)


class GeometryTests(ViewCase):
    """``instant_at`` and ``x_of`` must describe the same grid."""

    def test_a_click_reads_back_as_the_pixel_it_was_made_at(self):
        """The invariant that makes a click mean what it looks like it means.

        Not exact inversion -- at a coarse zoom a pixel is hours and no
        arithmetic recovers a second. What must hold is that the instant read
        lands in the column that was clicked, so the number the user is shown
        always belongs to the pixel under their cursor.
        """
        for x in (0, 1, 7, 200, 319, 400, WIDTH - 1):
            self.assertEqual(self.view.x_of(self.view.instant_at(x)), x, f"x={x}")

    def test_the_invariant_holds_at_every_zoom(self):
        for span in (SEC, MIN, HOUR, DAY, YEAR):
            with self.subTest(span=span // SEC):
                self.set_view(BASE, BASE + span)
                for x in (0, 3, 100, 320, WIDTH - 2, WIDTH - 1):
                    self.assertEqual(self.view.x_of(self.view.instant_at(x)), x)

    def test_a_click_interpolates_within_its_column(self):
        """Not snapped to the column's left edge.

        Snapping would add a whole column of error on top of the click's own
        imprecision -- error the user never asked for. Halfway through the first
        column must land halfway through that column.
        """
        self.set_view(BASE, BASE + DAY)
        column = (BASE + DAY) // WIDTH
        self.assertGreater(self.view.instant_at(0.5), BASE)
        self.assertLess(abs(self.view.instant_at(0.5) - (BASE + column // 2)), column)

    def test_a_click_never_leaves_the_view(self):
        self.set_view(BASE, BASE + DAY)
        for x in (-500, -1, 0, WIDTH, WIDTH + 500):
            instant = self.view.instant_at(x)
            self.assertGreaterEqual(instant, self.view.view()[0])
            self.assertLessEqual(instant, self.view.view()[1])

    def test_span_per_pixel_is_the_zoom(self):
        self.set_view(BASE, BASE + DAY)
        self.assertAlmostEqual(self.view.span_per_pixel(), DAY / WIDTH / 1e6, places=6)
        self.view.zoom_in()
        self.assertLess(self.view.span_per_pixel(), DAY / WIDTH / 1e6)

    def test_span_per_pixel_survives_a_widget_with_no_width_yet(self):
        """Signals can arrive before layout, and a 1/0 is an exception."""
        self.view.resize(0, 0)
        self.assertGreater(self.view.span_per_pixel(), 0)

    def test_the_view_rounds_trip_through_set_view(self):
        start, end = self.view.view()
        self.assertEqual(self.view.set_view(start, end), (start, end))


class ViewRangeTests(ViewCase):
    def test_the_first_limits_show_everything(self):
        """All clips visible at launch is the redesign's whole point."""
        limits = Limits.for_extent(BASE, BASE + YEAR)
        fresh = WaveView()
        fresh.set_limits(limits)
        start, end = fresh.view()
        self.assertLessEqual(start, BASE)
        self.assertGreaterEqual(end, BASE + YEAR)

    def test_a_wider_archive_does_not_throw_away_the_zoom(self):
        """Adding a second archive should not undo the zoom the user chose."""
        self.set_view(BASE, BASE + DAY)
        self.view.zoom_in()  # a deliberate move, not just a programmatic one
        zoomed = self.view.view()
        self.view.set_limits(Limits.for_extent(BASE, BASE + YEAR))
        self.assertEqual(self.view.view(), zoomed)

    def test_the_first_archive_arriving_after_an_empty_one_is_seen_in_full(self):
        """The bug this guards.

        An empty archive gives limits of ``(0, 1)``, and clamping that opens a
        legal one-second window. If "nothing has chosen a range yet" is then judged
        by the size of that window, the real extent arrives to find a one-second
        view the user never asked for -- so the app opens on a second of silence
        instead of every clip, which is the one thing the global timeline promises.
        """
        self.view.set_limits(Limits.for_extent(0, 1))
        self.view.set_limits(Limits.for_extent(BASE, BASE + YEAR))
        start, end = self.view.view()
        limits = self.view.limits()
        self.assertLessEqual(start, limits.start_us)
        self.assertGreaterEqual(end, limits.end_us)

    def test_the_view_does_not_follow_an_empty_archive(self):
        """Nothing to show is not a reason to zoom anywhere in particular."""
        self.view.zoom_in()
        self.view.set_limits(Limits.for_extent(0, 1))
        # The archive is gone, so the wide view is no longer legal and the view
        # collapses rather than showing something outside its own limits. What
        # matters is that it stays inside them.
        start, end = self.view.view()
        limits = self.view.limits()
        self.assertGreaterEqual(start, limits.start_us - limits.padding_us)
        self.assertLessEqual(end, limits.end_us + limits.padding_us)

    def test_following_the_playhead_does_not_count_as_choosing(self):
        """Otherwise the first thing played would pin the view for good."""
        self.view.set_limits(Limits.for_extent(BASE, BASE + YEAR))
        self.view.set_limits(Limits.for_extent(BASE, BASE + 4 * YEAR))
        limits = self.view.limits()
        start, end = self.view.view()
        self.assertLessEqual(start, limits.start_us)
        self.assertGreaterEqual(end, limits.end_us)

    def test_set_view_returns_the_range_it_actually_took(self):
        """Callers that go on to use the value need the clamped one."""
        taken = self.view.set_view(BASE - 10 * YEAR, BASE - 9 * YEAR)
        self.assertEqual(taken, self.view.view())
        start, end = taken
        limits = self.view.limits()
        self.assertGreaterEqual(start, limits.start_us - limits.padding_us)

    def test_a_backwards_range_is_repaired_not_refused(self):
        start, end = self.view.set_view(BASE + HOUR, BASE)
        self.assertLess(start, end)

    def test_ensure_visible_leaves_a_visible_instant_alone(self):
        """Following playback by always re-centring would make the view crawl."""
        before = self.view.view()
        self.view.ensure_visible(BASE + DAY // 2)
        self.assertEqual(self.view.view(), before)

    def test_ensure_visible_scrolls_to_an_instant_off_screen(self):
        # A year of limits, so there is somewhere to scroll *to*. Asking to bring
        # an instant past the end of the archive into view can only fail.
        self.set_view(BASE, BASE + DAY, within=(BASE, BASE + YEAR))
        before = self.view.view()
        self.view.ensure_visible(BASE + 50 * DAY)
        self.assertNotEqual(self.view.view(), before)
        start, end = self.view.view()
        self.assertTrue(start <= BASE + 50 * DAY <= end)

    def test_fit_all_returns_to_the_whole_archive(self):
        self.view.zoom_in()
        self.view.zoom_in()
        self.view.fit_all()
        limits = self.view.limits()
        start, end = self.view.view()
        self.assertLessEqual(start, limits.start_us)
        self.assertGreaterEqual(end, limits.end_us)

    def test_view_changed_fires_on_a_move_and_not_on_a_no_op(self):
        seen = []
        self.view.viewChanged.connect(lambda a, b: seen.append((a, b)))
        self.view.set_view(BASE + HOUR, BASE + 2 * HOUR)
        self.assertEqual(len(seen), 1)
        self.view.set_view(*self.view.view())
        self.assertEqual(len(seen), 1, "setting the same range again is not a change")

    def test_view_changed_carries_the_range_that_was_adopted(self):
        seen = []
        self.view.viewChanged.connect(lambda a, b: seen.append((a, b)))
        self.view.set_view(BASE - 10 * YEAR, BASE - 9 * YEAR)
        self.assertEqual(seen, [self.view.view()])


class ZoomGestureTests(ViewCase):
    #: One notch of the wheel. Mirrors the widget's own step.
    STEP = 1.15

    def span(self):
        start, end = self.view.view()
        return end - start

    def test_the_wheel_zooms_in_when_scrolled_up(self):
        before = self.span()
        self.wheel(320, 120)
        self.assertLess(self.span(), before, "up zooms in, like every other editor")

    def test_the_wheel_zooms_out_when_scrolled_down(self):
        before = self.span()
        self.wheel(320, -120)
        self.assertGreater(self.span(), before)

    def test_the_wheel_zooms_about_the_pointer(self):
        """The instant under the cursor must not move.

        Zooming about the middle instead would walk the view sideways under a
        stationary hand, which is what makes a zoom feel broken.
        """
        x = 470
        anchor = self.view.instant_at(x)
        self.wheel(x, 120)
        self.assertLess(self.span(), DAY)
        # One notch, so at most one column of rounding between the instant read
        # and the column it is drawn in.
        self.assertLessEqual(abs(self.view.x_of(anchor) - x), 1)

    def test_each_notch_is_the_same_ratio(self):
        """What "zoom about the pointer" means over a run of notches.

        Not re-reading the same anchor, because each notch re-reads the pointer's
        instant from the view it just produced: a pixel is not a whole column, so
        up to one column of rounding can compound per step. The span, though, is
        exactly the step's ratio every time unless something clamps it.
        """
        self.set_view(BASE, BASE + YEAR, within=(BASE, BASE + 100 * YEAR))
        for step in range(6):
            before = self.span()
            self.wheel(200, 120)
            self.assertAlmostEqual(before / self.span(), self.STEP, delta=0.01 * self.STEP)

    def test_zooming_in_keeps_the_instant_under_the_pointer_in_view(self):
        x = 200
        anchor = self.view.instant_at(x)
        for _ in range(6):
            self.wheel(x, 120)
        start, end = self.view.view()
        self.assertTrue(start <= anchor <= end)

    def test_a_wheel_delta_is_normalised_by_notches(self):
        """A high-resolution wheel sends 240 per click, not 120.

        Without dividing by 120 it would fly across the archive on one turn of a
        fine wheel and crawl on a coarse one. Two notches must be two steps, not
        one step taken twice as far.
        """
        # Zooming *in*, with far more archive than view, so neither result is
        # clamped and the ratio is meaningful.
        self.set_view(BASE, BASE + DAY, within=(BASE, BASE + 100 * YEAR))
        one = self.span()
        self.wheel(320, 120)
        one_notch = one / self.span()

        self.set_view(BASE, BASE + DAY, within=(BASE, BASE + 100 * YEAR))
        self.wheel(320, 240)
        two_notches = one / self.span()

        self.assertAlmostEqual(one_notch, self.STEP, delta=0.01 * self.STEP)
        self.assertAlmostEqual(two_notches, self.STEP**2, delta=0.01 * self.STEP * self.STEP)

    def test_a_zero_delta_changes_nothing(self):
        before = self.view.view()
        self.wheel(320, 0)
        self.assertEqual(self.view.view(), before)

    def test_the_buttons_zoom_about_the_centre(self):
        before_centre = self.view.instant_at(self.view.width() / 2)
        self.view.zoom_in()
        after_centre = self.view.instant_at(self.view.width() / 2)
        self.assertLessEqual(abs(self.view.x_of(before_centre) - self.view.width() / 2), 1)
        self.assertLessEqual(abs(self.view.x_of(after_centre) - self.view.width() / 2), 1)

    def test_zoom_buttons_can_reach_the_whole_archive(self):
        """Many clicks out must reach fit_all, or the zoom has a ceiling.

        Capping every zoom at the *unpadded* span leaves the first and last clip
        permanently flush against the edge with no way back, which is worse than
        the padding it was trying to avoid.
        """
        for _ in range(200):
            self.view.zoom_out()
        limits = self.view.limits()
        widest = limits.span_us + 2 * limits.padding_us
        self.assertLessEqual(self.view.view()[1] - self.view.view()[0], widest)

    def test_zoom_buttons_can_reach_the_minimum_span(self):
        for _ in range(200):
            self.view.zoom_in()
        self.assertGreaterEqual(
            self.view.view()[1] - self.view.view()[0], self.view.limits().min_span_us
        )


class PanGestureTests(ViewCase):
    def setUp(self):
        super().setUp()
        # A year of limits and a day of view: panning is clamped to the limits,
        # and a view that already spans the archive cannot move at all.
        self.set_view(BASE, BASE + DAY, within=(BASE, BASE + YEAR))

    def test_middle_drag_moves_the_view(self):
        before = self.view.view()
        self.drag(400, 300, button=Qt.MiddleButton)
        self.assertNotEqual(self.view.view(), before)

    def test_the_audio_follows_the_pointer_during_a_pan(self):
        """Dragging left moves the view right, so what was under the cursor
        stays under the cursor."""
        anchor = self.view.instant_at(300)
        self.press(300, button=Qt.MiddleButton)
        self.move(200, button=Qt.MiddleButton)
        self.assertLessEqual(abs(self.view.x_of(anchor) - 200), 1)
        self.release(200, button=Qt.MiddleButton)

    def test_the_press_stays_under_the_pointer_over_a_whole_drag(self):
        """Not just on the first move -- every event, for the length of the drag.

        The old arithmetic took the instant under the pointer and subtracted it
        from the instant under the press, but measured both in the view as it
        was *now*, and the view had moved since the press to make the previous
        event's answer come true. So each event paid back the move that
        produced it: the anchor stood still for one move and then jumped back,
        alternating further from the pointer the longer the drag went on. That
        is the jitter -- one measurement taken in a frame that is moving
        underneath it.
        """
        anchor = self.view.instant_at(300)
        self.press(300, button=Qt.MiddleButton)
        for x in (290, 260, 220, 170, 130):
            self.move(x, button=Qt.MiddleButton)
            self.assertLessEqual(
                abs(self.view.x_of(anchor) - x),
                1,
                f"the content under the press left the pointer at x={x}",
            )
        self.release(130, button=Qt.MiddleButton)

    def test_where_a_pan_ends_does_not_depend_on_the_path_taken(self):
        """Every event answers from the press frame, so the view is a function
        of where the pointer *is*, not of where it has been."""
        self.press(300, button=Qt.MiddleButton)
        self.move(130, button=Qt.MiddleButton)
        direct = self.view.view()
        self.release(130, button=Qt.MiddleButton)

        # Back where the first gesture started, then a wander that ends short
        # of nothing: overshoot, come back, and arrive at the same pointer.
        self.set_view(BASE, BASE + DAY, within=(BASE, BASE + YEAR))
        self.press(300, button=Qt.MiddleButton)
        for x in (260, 170, 340, 130):
            self.move(x, button=Qt.MiddleButton)
        walked = self.view.view()
        self.release(130, button=Qt.MiddleButton)

        self.assertEqual(direct, walked)

    def test_a_pan_does_not_select_anything(self):
        seen = []
        self.view.snipSelected.connect(lambda a, b: seen.append((a, b)))
        self.drag(400, 200, button=Qt.MiddleButton)
        self.assertEqual(seen, [])
        self.assertIsNone(self.view.state().snip_from_us)

    def test_the_pan_stops_when_the_button_is_released(self):
        self.press(400, button=Qt.MiddleButton)
        self.move(200, button=Qt.MiddleButton)
        self.release(200, button=Qt.MiddleButton)
        settled = self.view.view()
        self.move(100)
        self.assertEqual(self.view.view(), settled)

    def test_panning_stops_at_the_start_of_the_archive(self):
        """Dragging right walks the view back, and it must stop at the first
        clip rather than scrolling into empty margin forever."""
        limits = self.view.limits()
        self.drag(40, WIDTH - 20, button=Qt.MiddleButton)
        start, _end = self.view.view()
        self.assertGreaterEqual(start, limits.start_us - limits.padding_us)
        self.assertLessEqual(
            start, limits.start_us, "the padded margin is all there is beyond it"
        )


class ClickAndDragTests(ViewCase):
    def test_a_click_moves_the_cursor_and_selects_nothing(self):
        seen = []
        self.view.playheadMoved.connect(lambda v: seen.append(v))
        snips = []
        self.view.snipSelected.connect(lambda a, b: snips.append((a, b)))

        self.click(300)
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(abs(self.view.x_of(seen[0]) - 300), 1)
        self.assertEqual(snips, [])
        self.assertIsNone(self.view.state().snip_from_us)

    def test_a_drag_selects_a_range(self):
        snips = []
        self.view.snipSelected.connect(lambda a, b: snips.append((a, b)))
        self.drag(200, 420)
        self.assertEqual(len(snips), 1)
        low, high = snips[0]
        self.assertLess(low, high)
        self.assertEqual(self.view.state().snip_from_us, low)
        self.assertEqual(self.view.state().snip_to_us, high)

    def test_a_drag_right_to_left_is_ordered_for_the_caller(self):
        snips = []
        self.view.snipSelected.connect(lambda a, b: snips.append((a, b)))
        self.drag(420, 200)
        self.assertLess(snips[0][0], snips[0][1])

    def test_a_drag_does_not_move_the_cursor(self):
        seen = []
        self.view.playheadMoved.connect(lambda v: seen.append(v))
        self.drag(200, 420)
        self.assertEqual(seen, [], "dragging selects; it does not also move the cursor")

    def test_a_tiny_drag_is_still_a_click(self):
        """Judged by distance, not by time.

        A slow deliberate click must not turn into a selection of two pixels,
        which would then be exported as a snip of almost nothing.
        """
        seen = []
        self.view.playheadMoved.connect(lambda v: seen.append(v))
        snips = []
        self.view.snipSelected.connect(lambda a, b: snips.append((a, b)))

        self.drag(300, 302)
        self.assertEqual(len(seen), 1, "a two-pixel drag is a click")
        self.assertEqual(snips, [])

    def test_the_drag_threshold_is_exactly_where_it_says(self):
        for offset, is_drag in ((3, False), (4, True), (5, True)):
            with self.subTest(offset=offset):
                fresh = WaveView()
                fresh.resize(WIDTH, HEIGHT)
                self.addCleanup(fresh.deleteLater)
                fresh.set_limits(Limits.for_extent(BASE, BASE + DAY))
                fresh.set_view(BASE, BASE + DAY)
                snips = []
                fresh.snipSelected.connect(lambda a, b: snips.append((a, b)))
                view = self.view
                self.view = fresh
                try:
                    self.drag(300, 300 + offset)
                finally:
                    self.view = view
                self.assertEqual(bool(snips), is_drag)

    def test_a_drag_that_collapses_is_a_click(self):
        """Wandered back to the origin: the user meant a click."""
        seen = []
        self.view.playheadMoved.connect(lambda v: seen.append(v))
        cleared = []
        self.view.snipCleared.connect(lambda: cleared.append(True))

        self.press(300)
        self.move(400)
        self.move(300)
        self.release(300)
        self.assertEqual(len(seen), 1)
        self.assertEqual(cleared, [True])


class PaintingTests(ViewCase):
    """``grab()`` forces a paint. Nothing else in a headless test does."""

    def test_it_paints_at_a_second_wide(self):
        self.set_view(BASE, BASE + SEC)
        self.set_state(coverage=[(BASE, BASE + SEC)], peaks_possible=True)
        self.assertFalse(self.view.grab().isNull())

    def test_it_paints_a_year_wide(self):
        """The wide case is where a timestamp would overflow a QRect."""
        self.set_view(BASE, BASE + YEAR)
        self.set_state(coverage=[(BASE, BASE + YEAR)], peaks_possible=False)
        self.assertFalse(self.view.grab().isNull())

    def test_a_day_of_gaps_and_coverage_paints_without_warning(self):
        coverage = [(BASE + i * HOUR, BASE + (i + 1) * HOUR) for i in range(0, 24, 2)]
        self.set_view(BASE, BASE + DAY)
        self.set_state(
            coverage=coverage,
            gaps=[Gap(BASE + HOUR, BASE + 2 * HOUR)],
            peaks_possible=False,
            playhead_us=BASE + 3 * HOUR,
            snip_from_us=BASE + HOUR,
            snip_to_us=BASE + 9 * HOUR,
        )
        self.pixels()  # raises if a shiboken RuntimeWarning was produced

    def test_a_playhead_far_outside_the_view_does_not_paint(self):
        """A playhead off screen is not drawn at the edge pretending to be in range."""
        self.set_view(BASE, BASE + DAY)
        with_peaks = self.render(coverage=[(BASE, BASE + DAY)], playhead_us=BASE + 3 * HOUR)
        without = self.render(coverage=[(BASE, BASE + DAY)])
        off_screen = self.render(coverage=[(BASE, BASE + DAY)], playhead_us=BASE + 90 * DAY)
        self.assertTrue(self.changed(with_peaks, without).any(), "sanity: it draws")
        self.assertFalse(self.changed(off_screen, without).any())

    def test_a_hole_is_empty_background_and_a_clip_is_a_block(self):
        """A hole must not look like content -- and it is not drawn at all.

        The clip runs are blocks in the band; the hole between them is the
        widget's background, every row of it, even where ``gaps`` says a hole
        exists. There is nothing to paint there.
        """
        self.set_view(BASE, BASE + DAY)
        coverage = [(BASE, BASE + DAY // 3), (BASE + 2 * DAY // 3, BASE + DAY)]
        self.set_state(
            coverage=coverage,
            gaps=[Gap(BASE + DAY // 3, BASE + 2 * DAY // 3)],
        )
        pixels = self.pixels()[:, :, :3].astype(np.int32)
        band = self.view._wave_rect(self.view._content_rect())
        height = self.view._content_rect().height()

        def rgb(colour):
            return np.array([colour.red(), colour.green(), colour.blue()])

        background, block = rgb(self.view._col_bg), rgb(self.view._col_wave_dim)

        clip = pixels[:height, 10:80]
        self.assertTrue(
            np.all(clip[band.top() : band.bottom() + 1] == block),
            "the covered columns are a solid block across the band",
        )
        self.assertTrue(
            np.all(clip[: band.top()] == background)
            and np.all(clip[band.bottom() + 1 :] == background),
            "and nothing above or below it: the block has edges",
        )

        hole = pixels[:height, 250:400]
        self.assertTrue(
            np.all(hole == background),
            "every row of the hole is background -- gaps are drawn as nothing",
        )

    def test_the_hint_appears_only_when_a_waveform_is_impossible(self):
        self.set_view(BASE, BASE + YEAR)
        self.set_state(coverage=[(BASE, BASE + YEAR)], peaks_possible=False)
        self.assertGreater(self.colour_count(self.view._col_hint), 0)

    def test_no_hint_while_peaks_are_still_on_their_way(self):
        """"Not yet" and "not ever" look the same on screen but mean opposite
        things, so only the second one is worth an explanation."""
        self.set_view(BASE, BASE + YEAR)
        self.set_state(coverage=[(BASE, BASE + YEAR)], peaks_possible=True)
        self.assertEqual(self.colour_count(self.view._col_hint), 0)

    def test_the_axis_is_labelled(self):
        self.set_view(BASE, BASE + DAY)
        self.set_state(coverage=[(BASE, BASE + DAY)])
        self.assertGreater(self.colour_count(self.view._col_text), 0)

    def test_the_axis_labels_change_with_the_zoom(self):
        """A fixed format would be unreadable at some zoom; the same text at every
        zoom would mean the ticks are not being recomputed."""
        self.set_view(BASE, BASE + SEC)
        self.set_state(coverage=[(BASE, BASE + SEC)])
        one_second = self.pixels()[:, self.view.height() - 18 :, :3].copy()

        self.set_view(BASE, BASE + YEAR)
        self.set_state(coverage=[(BASE, BASE + YEAR)])
        one_year = self.pixels()[:, self.view.height() - 18 :, :3]
        self.assertFalse(np.array_equal(one_second, one_year))

    def test_a_waveform_is_drawn_when_peaks_are_supplied(self):
        self.set_view(BASE, BASE + DAY)
        bare = self.render(coverage=[(BASE, BASE + DAY)])
        drawn = self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=self.flat_peaks(400),
            peaks_match=True,
        )
        self.assertTrue(self.changed(drawn, bare).any())

    def test_stale_peaks_are_not_drawn(self):
        """Peaks for another range would be a waveform that lies about the time."""
        self.set_view(BASE, BASE + DAY)
        bare = self.render(coverage=[(BASE, BASE + DAY)])
        stale = self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=self.flat_peaks(400),
            peaks_match=False,
        )
        self.assertFalse(self.changed(stale, bare).any())

    def test_fewer_peaks_than_pixels_still_fill_the_width(self):
        """Otherwise a wide widget is half blank and the waveform lies about
        where the audio is."""
        self.set_view(BASE, BASE + DAY)
        bare = self.render(coverage=[(BASE, BASE + DAY)])
        drawn = self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=self.flat_peaks(10),
            peaks_match=True,
        )
        changed = self.changed(drawn, bare)
        self.assertTrue(changed[:, :20].any(), "the left edge is covered")
        self.assertTrue(changed[:, WIDTH - 25 :].any(), "and the right edge too")

    def test_a_nan_column_is_skipped_not_drawn_as_nothing(self):
        """A NaN column is a gap inside a covered range; a bar of silence there
        would claim there is audio."""
        self.set_view(BASE, BASE + DAY)
        bare = self.render(coverage=[(BASE, BASE + DAY)])

        peaks = self.flat_peaks(100)
        peaks.mins[40:60] = np.nan
        peaks.maxs[40:60] = np.nan
        drawn = self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=peaks,
            peaks_match=True,
        )
        changed = self.changed(drawn, bare)
        middle = WIDTH // 2
        self.assertTrue(changed[:, : middle - 40].any(), "audio either side of the hole")
        self.assertFalse(changed[:, middle - 20 : middle + 20].any(), "nothing across it")

    def test_a_loud_column_is_taller_than_a_quiet_one(self):
        """How much of the picture a waveform covers is how loud it is."""
        self.set_view(BASE, BASE + DAY)
        bare = self.render(coverage=[(BASE, BASE + DAY)])
        coverage = [(BASE, BASE + DAY)]

        def ink_for(amplitude):
            peaks = self.flat_peaks(400, -amplitude, amplitude)
            drawn = self.render(coverage=coverage, peaks=peaks, peaks_match=True)
            return int(self.changed(drawn, bare).sum())

        quiet, loud = ink_for(0.2), ink_for(0.9)
        self.assertGreater(quiet, 0, "sanity: a waveform is drawn at all")
        self.assertGreater(loud, quiet)

    def test_an_empty_archive_paints_without_complaint(self):
        self.set_view(BASE, BASE + DAY)
        self.set_state(label="no archive")
        self.pixels()

    def test_a_widget_too_small_to_draw_paints(self):
        """Guarded by an early return, and a QRect of width 1 is still legal."""
        self.set_view(BASE, BASE + DAY)
        self.set_state(coverage=[(BASE, BASE + DAY)])
        self.view.resize(1, 1)
        self.pixels()


class RmsBandPaintTests(ViewCase):
    """The inner band as paint: a lit core inside the gradient envelope.

    The outline alone saturates when zoomed out -- nearly every column holds a
    transient somewhere, so the outline is full height everywhere and a loud
    stretch and a quiet one become the same solid block. The band is the fix,
    and on screen it must do three things: appear exactly where the level says,
    in the bright colour, over an outline drawn exactly as before.
    """

    def geometry(self):
        """The painter's own centre line and half-height, recomputed."""
        wave = self.view._wave_rect(self.view._content_rect())
        mid = wave.top() + wave.height() // 2
        half = max(1, (wave.height() - 2) // 2)
        return wave, mid, half

    @staticmethod
    def rows_of(ink):
        """Rows on which two renders disagree anywhere."""
        return np.flatnonzero(ink.any(axis=1))

    def plain(self, count=400):
        return self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=self.flat_peaks(count),
            peaks_match=True,
        )

    def banded(self, level, count=400, **peaks_kw):
        return self.render(
            coverage=[(BASE, BASE + DAY)],
            peaks=self.flat_peaks(count, band=level, **peaks_kw),
            peaks_match=True,
        )

    def test_the_band_is_drawn_inside_the_outline(self):
        """Same envelope with and without a band: what appears is the band's,
        and only as far across the widget as the level says."""
        self.set_view(BASE, BASE + DAY)
        rows = self.rows_of(self.changed(self.banded(0.3), self.plain()))
        _, mid, half = self.geometry()

        self.assertGreater(len(rows), 0, "sanity: the band draws something")
        self.assertGreaterEqual(rows.min(), round(mid - 0.3 * half) - 1)
        self.assertLessEqual(rows.max(), round(mid + 0.3 * half) + 1)
        self.assertLess(rows.max() - rows.min(), round(0.6 * half) + 3, "and no taller")
        self.assertIn(mid, rows, "the centre line is lit")

    def test_the_band_is_the_bright_wave_colour(self):
        """A lit core, not another grey: solid where the outline fades, which
        is what makes it read as the typical level inside the spikes rather
        than as more spikes."""
        self.set_view(BASE, BASE + DAY)
        pixels = self.banded(0.4)
        wave, mid, _ = self.geometry()
        target = np.array(
            [self.view._col_wave.red(), self.view._col_wave.green(), self.view._col_wave.blue()]
        )
        inside = pixels[mid, wave.left() : wave.right() + 1]
        self.assertTrue(np.all(inside == target), "the centre line is solid bright")
        gradient = self.plain()[mid, wave.left() : wave.right() + 1]
        self.assertFalse(
            np.all(gradient == target),
            "sanity: the outline's gradient was not already flat bright here",
        )

    def test_silence_gains_no_band(self):
        """Flat zero is not a level to light -- the outline already draws the
        one pixel line that silence is."""
        self.set_view(BASE, BASE + DAY)
        self.assertFalse(
            self.changed(self.banded(0.0), self.plain()).any(),
            "identical pictures",
        )

    def test_a_column_without_a_band_is_outline_only(self):
        """``rms=NaN`` (and ``rms=None``) mean "no band here": a gap column, or
        an envelope built by something else. Neither may draw a line of
        nothing down the middle."""
        self.set_view(BASE, BASE + DAY)
        plain = self.plain(count=100)
        peaks = self.flat_peaks(100, band=0.4)
        peaks.rms[40:60] = np.nan
        banded = self.render(
            coverage=[(BASE, BASE + DAY)], peaks=peaks, peaks_match=True
        )

        changed = self.changed(banded, plain)
        middle = WIDTH // 2
        self.assertTrue(changed.any(), "sanity: the band draws at the edges")
        self.assertFalse(
            changed[:, middle - 20 : middle + 20].any(), "none across the NaN run"
        )


class WaveBandTests(ViewCase):
    """The clip blocks and the waveform are drawn in a band, not wall to wall.

    Four fifths of the content rect, centred -- ``WaveView._wave_rect``. What is
    being defended here is not the exact fraction, which is a judgement about how
    much margin reads as margin, but its *shape*: the clips and the waveform are
    a picture inside the viewport with the viewport's own background above and
    below it, and everything drawn *on* the picture -- the labels, the hint --
    is inside the band too.

    Deliberately outside the band: the background (it is the viewport's, not the
    picture's -- gaps are drawn as nothing), the Sunday wash (the weekend belongs
    to the time axis, so it runs the full height, *behind* the clip blocks), the
    selection (a stretch of time, and time runs the height of the widget), and
    the cursor (a marker, not content). Hence their own tests below.
    """

    def area(self):
        return self.view._content_rect()

    def band(self):
        return self.view._wave_rect(self.area())

    @staticmethod
    def rows_matching(pixels, colour):
        """Rows on which ``colour`` appears anywhere."""
        target = np.array([colour.red(), colour.green(), colour.blue()])
        return np.flatnonzero(np.all(pixels == target, axis=-1).any(axis=1))

    @staticmethod
    def rows_of(ink):
        """Rows on which two renders disagree anywhere."""
        return np.flatnonzero(ink.any(axis=1))

    def test_the_clip_blocks_leave_a_margin_above_and_below(self):
        """The blocks are a picture inside the viewport: they stop short of both
        edges instead of being the whole viewport, and nothing is painted in
        the margin -- not even the gaps, which are just background."""
        self.set_view(BASE, BASE + DAY)
        coverage = [(BASE, BASE + DAY // 3), (BASE + 2 * DAY // 3, BASE + DAY)]
        self.set_state(coverage=coverage)
        pixels = self.pixels()[:, :, :3].astype(np.int32)
        band, area = self.band(), self.area()

        self.assertLess(band.height(), area.height(), "sanity: there is a margin")
        # Centred: a height the fraction does not divide leaves one spare pixel,
        # and which side it goes to is rounding, not a preference.
        self.assertLessEqual(
            abs((band.top() - area.top()) - (area.bottom() - band.bottom())),
            1,
            "the margin is on both sides, not taken from one",
        )
        self.assertGreaterEqual(
            band.height(), area.height() * 3 // 4, "most of the viewport, not half of it"
        )

        rows = self.rows_matching(pixels, self.view._col_wave_dim)
        self.assertEqual(
            (rows.min(), rows.max()),
            (band.top(), band.bottom()),
            "the blocks span the band and only the band",
        )

        background = np.array(
            [self.view._col_bg.red(), self.view._col_bg.green(), self.view._col_bg.blue()]
        )
        for row in (band.top() - 1, band.bottom() + 1):
            self.assertTrue(
                np.all(pixels[row] == background),
                f"row {row} is background: nothing paints into the margin",
            )

    def test_the_waveform_is_drawn_inside_the_band(self):
        """Peaks are the content of the map, so they inherit its margins.

        Full amplitude, and drawn alone: a small waveform would sit inside every
        band-shaped thing and prove nothing, and with a clip block behind it the
        waveform's lower edge is *invisible* -- the gradient fades into exactly
        the colour of the block -- so the block would hide how far down the
        waveform actually reaches.
        """
        self.set_view(BASE, BASE + DAY)
        bare = self.render()
        drawn = self.render(peaks=self.flat_peaks(400, -1.0, 1.0), peaks_match=True)
        rows = self.rows_of(self.changed(drawn, bare))

        self.assertGreater(len(rows), 0, "sanity: a waveform is drawn")
        band = self.band()
        self.assertGreaterEqual(rows.min(), band.top(), "nothing above the band")
        self.assertLessEqual(rows.max(), band.bottom(), "nothing below it")
        self.assertGreaterEqual(
            rows.max() - rows.min() + 1,
            band.height() * 9 // 10,
            "and it fills the band rather than sitting as a sliver inside it",
        )

    def test_the_clip_label_sits_at_the_top_of_the_band(self):
        """A label names a clip block, so it is drawn where that block is --
        on the map -- rather than at the top of the widget over empty margin."""
        self.set_view(BASE, BASE + DAY)
        coverage = [(BASE, BASE + DAY)]
        plain = self.render(coverage=coverage)
        named = self.render(
            coverage=coverage, clips=[(BASE, BASE + DAY, "c0.wav")]
        )
        rows = self.rows_of(self.changed(named, plain))
        band = self.band()

        self.assertGreater(len(rows), 0, "sanity: a label is drawn")
        self.assertEqual(rows.min(), band.top(), "the label sits on the band's top edge")
        self.assertLessEqual(rows.max(), band.bottom(), "and does not spill out of it")

    def test_the_selected_region_marks_the_whole_viewport_height(self):
        """``design.md``: the selection runs across the whole height, above the
        waveform and below the blocks.

        Confined to the wave band -- which is what it used to do -- it marked
        only the part of the range the map was showing: the two edges stopped
        in the band's margins with nothing joining them to the axis ticks that
        say what the range *is*. The blocks stay on top of it, because they are
        content where the selection is a stretch of time.
        """
        self.set_view(BASE, BASE + DAY)
        coverage = [(BASE, BASE + DAY)]
        blocks = self.assigned(
            [
                self.block("earlier", BASE, BASE + 12 * HOUR),
                self.block("later", BASE + 6 * HOUR, BASE + DAY),
            ]
        )
        plain = self.render(coverage=coverage, time_blocks=blocks)
        marked = self.render(
            coverage=coverage,
            time_blocks=blocks,
            snip_from_us=BASE + 6 * HOUR,
            snip_to_us=BASE + 12 * HOUR,
        )
        rows = self.rows_of(self.changed(marked, plain))

        self.assertEqual((rows.min(), rows.max()), (0, self.view.height() - 1))

        # Where a block crosses the selection the *block* is what you see, ink
        # included: the wash goes under it. Sampled from the block's own label
        # because that is opaque ink laid down by the block layer -- the fill
        # itself is translucent (alpha 180) and honestly does let the wash
        # through, which is the block's colour showing what is behind it, not
        # the wash being painted over the block.
        row = self.view._time_block_row_rect(self.area(), 0)
        x0, x1 = self.view._time_block_span(BASE, BASE + 12 * HOUR, self.area())
        left = max(x0, self.view.x_of(BASE + 6 * HOUR)) + 4
        wash_p = plain[row.top() : row.bottom() + 1, left:x1 - 4]
        wash_m = marked[row.top() : row.bottom() + 1, left:x1 - 4]
        ink = np.all(wash_p == (0, 0, 0), axis=-1)

        self.assertTrue(ink.any(), "sanity: the block's name is painted")
        self.assertTrue(
            np.all(wash_m[ink] == wash_p[ink]),
            "the block's own ink is untouched: nothing is painted over it",
        )

    def test_the_sunday_wash_fills_the_viewport_behind_the_clips(self):
        """The weekend is a property of the time axis, not of the picture drawn
        over it: the wash runs the whole height of the widget -- down through
        the clip blocks' margins *and over the axis strips* -- and the clip
        blocks are painted *after* it, so a block keeps its exact colour while
        the red shows above, below and around it.

        Two days straddling a Sunday midnight, fully covered so the band is one
        solid block: red-minus-green separates wash from background without
        having to guess how Qt blends alpha, and inside the band it separates
        neither, because there the wash is behind an opaque block.
        """
        start, end = BASE + 4 * DAY, BASE + 6 * DAY
        self.set_view(start, end)
        self.set_state(coverage=[(start, end)])
        pixels = self.pixels()[:, :, :3].astype(np.int32)
        band, area = self.band(), self.area()
        block = np.array(
            [self.view._col_wave_dim.red(), self.view._col_wave_dim.green(), self.view._col_wave_dim.blue()]
        )

        red = pixels[:, :, 0] - pixels[:, :, 1]
        rows = np.flatnonzero((red > 5).any(axis=1))
        self.assertGreater(len(rows), 0, "sanity: a Sunday is in this view")
        self.assertEqual(
            (rows.min(), rows.max()),
            (0, self.view.height() - 1),
            "the wash covers the whole widget by height, axis included",
        )

        washed = np.flatnonzero(red[area.top()] > 5)
        x = washed[len(washed) // 2]
        for row, where in ((band.top() - 1, "above"), (band.bottom() + 1, "below")):
            self.assertGreater(
                int(pixels[row, x, 0]) - int(pixels[row, x, 1]),
                5,
                f"the wash is visible {where} the block",
            )
        for row, where in (
            (area.bottom() + 1, "in the upper axis strip"),
            (self.view.height() - 1, "in the lower axis strip"),
        ):
            self.assertGreater(
                int(pixels[row, x, 0]) - int(pixels[row, x, 1]),
                5,
                f"the wash is visible {where}",
            )
        self.assertTrue(
            np.all(pixels[band.top() + 5, x] == block),
            "and the block it runs behind keeps its own colour",
        )

    def test_the_explanation_of_the_missing_waveform_sits_in_the_band(self):
        """The hint names the same map the labels do, so it is placed with them
        rather than with the empty margin above."""
        self.set_view(BASE, BASE + YEAR)
        self.set_state(coverage=[(BASE, BASE + YEAR)], peaks_possible=False)
        pixels = self.pixels()[:, :, :3].astype(np.int32)
        band = self.band()
        target = np.array(
            [self.view._col_hint.red(), self.view._col_hint.green(), self.view._col_hint.blue()]
        )
        rows = np.flatnonzero(np.all(pixels == target, axis=-1).any(axis=1))

        self.assertGreater(len(rows), 0, "sanity: the hint is drawn")
        self.assertGreaterEqual(rows.min(), band.top())
        self.assertLessEqual(rows.max(), band.bottom())

    def test_the_cursor_crosses_the_whole_viewport(self):
        """The exception, and the reason for it: the playhead is where *you* are
        in the widget, not content in the picture, and a cursor that vanishes
        outside the band -- or stops above the axis strips that would tell you
        what time it is -- goes missing exactly where you were looking."""
        self.set_view(BASE, BASE + DAY)
        coverage = [(BASE, BASE + DAY)]
        plain = self.render(coverage=coverage)
        marked = self.render(coverage=coverage, playhead_us=BASE + 6 * HOUR)
        rows = self.rows_of(self.changed(marked, plain))

        self.assertEqual((rows.min(), rows.max()), (0, self.view.height() - 1))


class TimeBlockRowTests(ViewCase):
    """Time blocks rise from the bottom of the viewport, the first one lowest.

    The rows are assigned top-down by nothing more than the greedy first-fit in
    ``assign_time_block_rows``, so painting them top-down would hang the earliest
    block off the *ceiling* of the waveform area with a row of empty space under
    it. The stacking direction is therefore a property of the painter, and it is
    the kind of property that no amount of "it looks right" reviewing protects --
    hence both the geometry assertions and the rendered-ink one.
    """

    def area(self):
        return self.view._content_rect()

    def two_overlapping_blocks(self):
        """Two blocks that share time, so they take rows 0 and 1."""
        return [
            self.block("earlier", BASE, BASE + 12 * HOUR),
            self.block("later", BASE + 6 * HOUR, BASE + DAY),
        ]

    # -- geometry ---------------------------------------------------------

    def test_the_first_row_stands_a_margin_above_the_floor(self):
        """``design.md``: more margin from the bottom for the blocks.

        The stack is separated from the axis rather than resting on it -- a
        block welded to the axis strip reads as part of the axis. The margin
        is exactly `_TB_BOTTOM_MARGIN`, no more and no less.
        """
        from wavesnip.waveview import _TB_BOTTOM_MARGIN

        area = self.area()
        row0 = self.view._time_block_row_rect(area, 0)
        self.assertEqual(row0.bottom(), area.bottom() - _TB_BOTTOM_MARGIN)
        # And the gap is real air: nothing paints between them.
        self.assertGreaterEqual(_TB_BOTTOM_MARGIN, 1)

    def test_each_row_is_entirely_above_the_one_below_it(self):
        area = self.area()
        rows = [self.view._time_block_row_rect(area, i) for i in range(3)]
        for lower, upper in zip(rows, rows[1:]):
            self.assertLess(upper.bottom(), lower.top())
        for row in rows:
            self.assertTrue(area.contains(row), "a row never leaves the content area")

    def test_a_row_whose_bottom_would_be_off_the_top_is_dropped(self):
        """Clipped rather than allowed to hang over the top of the widget."""
        # The view's own minimum height is 200, which is exactly what makes this
        # case unreachable through the widget -- it has to be asked for directly.
        self.view.setMinimumHeight(40)
        self.view.resize(WIDTH, 40)  # 40 - 36 axis = a 4px content area
        area = self.view._content_rect()
        self.assertEqual(area.height(), 4, "sanity: the area really is tiny")
        self.assertIsNotNone(self.view._time_block_row_rect(area, 0))
        self.assertIsNone(self.view._time_block_row_rect(area, 1))

    # -- hit testing ------------------------------------------------------

    def test_the_hit_test_agrees_with_where_the_row_is_drawn(self):
        blocks = self.assigned(self.two_overlapping_blocks())
        self.set_state(time_blocks=blocks)
        area = self.area()
        x = WIDTH // 2
        row0 = self.view._time_block_row_rect(area, 0)
        row1 = self.view._time_block_row_rect(area, 1)

        hit_low = self.view._time_block_at(x, row0.center().y())
        hit_high = self.view._time_block_at(x, row1.center().y())
        self.assertEqual(hit_low[2], "earlier")
        self.assertEqual(hit_high[2], "later")

        gap = (row0.top() + row1.bottom()) // 2
        self.assertIsNone(self.view._time_block_at(x, gap), "the gap between rows is empty")

    # -- painting ---------------------------------------------------------

    def test_the_first_block_is_painted_at_the_bottom_of_the_viewport(self):
        """The claim that matters: ink lands on the bottom rows, not the top.

        Compared against the same render without blocks, so the waveform, the
        labels and the axis -- all of which paint into the same band -- cannot
        satisfy the assertion on their own.
        """
        self.set_view(BASE, BASE + DAY)
        blocks = self.assigned(self.two_overlapping_blocks())
        with_blocks = self.render(coverage=[(BASE, BASE + DAY)], time_blocks=blocks)
        without = self.render(coverage=[(BASE, BASE + DAY)], time_blocks=[])
        ink = self.changed(with_blocks, without)

        rows = [self.view._time_block_row_rect(self.area(), i) for i in range(2)]

        # Both bounds are the *stack's own* rows rather than a fixed distance
        # from the floor: the rows move with the bottom margin, and an
        # assertion calibrated to one layout would stop meaning anything the
        # moment either changed.
        self.assertTrue(
            ink[rows[0].bottom()].any(), "the lowest row reaches its own floor"
        )

        # And the band the top-down layout used to fill with its first row is
        # above both rows now, so nothing of ours is up there any more.
        self.assertFalse(
            ink[: rows[1].top()].any(),
            "nothing above the stack: no row hangs from the top",
        )

    def test_a_third_overlapping_block_still_stays_inside_the_viewport(self):
        """Three mutually overlapping blocks still take three rows, and every
        one of them is a rectangle you can see and click.

        Not a ceiling any more -- see the tests below -- but the shape the
        stacking was designed around, and the case where dropping a row would
        be most tempting.
        """
        blocks = [
            self.block("a", BASE, BASE + 12 * HOUR),
            self.block("b", BASE + 6 * HOUR, BASE + 18 * HOUR),
            self.block("c", BASE + 3 * HOUR, BASE + DAY),
        ]
        assigned = self.assigned(blocks)
        self.assertEqual({entry[4] for entry in assigned}, {0, 1, 2})
        area = self.area()
        for entry in assigned:
            rect = self.view._time_block_row_rect(area, entry[4])
            self.assertIsNotNone(rect)
            self.assertTrue(area.contains(rect))

    def test_a_fourth_overlapping_block_gets_a_row_of_its_own(self):
        """Four mutual overlaps, four rows.

        Under a three-row cap the fourth block was drawn *on* the third, one
        name over another's rectangle -- the one thing separate rows exist to
        prevent, and the kind of bug that only shows up with four shows, which
        is exactly when you are trying to tell them apart.
        """
        blocks = [
            self.block("a", BASE, BASE + 12 * HOUR),
            self.block("b", BASE + 6 * HOUR, BASE + 18 * HOUR),
            self.block("c", BASE + 3 * HOUR, BASE + DAY),
            self.block("d", BASE + 10 * HOUR, BASE + 11 * HOUR),
        ]
        assigned = self.assigned(blocks)
        self.assertEqual({entry[4] for entry in assigned}, {0, 1, 2, 3})
        area = self.area()
        for entry in assigned:
            rect = self.view._time_block_row_rect(area, entry[4])
            self.assertIsNotNone(rect)
            self.assertTrue(area.contains(rect))

    def test_the_stack_is_cut_by_the_viewport_not_by_the_assignment(self):
        """Every block gets a row; only drawing runs out of room.

        Twelve blocks that all overlap each other are twelve rows. The
        assignment must not have an opinion about how tall the window is -- it
        cannot know -- so the cap that used to be here did nothing but decide
        which blocks would never be considered for drawing. How many fit is the
        content rect's arithmetic, and it is a different answer at every size.
        """
        blocks = [
            self.block(f"b{i}", BASE + i * MIN, BASE + DAY - i * MIN)
            for i in range(12)
        ]
        assigned = self.assigned(blocks)
        self.assertEqual({entry[4] for entry in assigned}, set(range(12)))

        area = self.area()
        rects = [self.view._time_block_row_rect(area, row) for row in range(12)]
        fitted = [rect for rect in rects if rect is not None]
        self.assertGreater(len(fitted), 3, "more rows than the old three-row cap")
        self.assertIsNone(rects[-1], "and the stack really does run out of room")
        for rect in fitted:
            self.assertTrue(area.contains(rect), "a row never leaves the content area")

    def test_every_row_that_fits_is_painted_with_its_own_block(self):
        """Rendered, not just assigned: six overlapping blocks, six rectangles.

        The paint is the claim. A row that exists in the assignment and carries
        no ink is a block the user cannot see, which is the whole complaint
        about a cap -- it failed quietly, by drawing the fourth block's
        rectangle in the third block's row.
        """
        self.set_view(BASE, BASE + DAY)
        blocks = [
            self.block(f"b{i}", BASE + i * MIN, BASE + DAY - i * MIN)
            for i in range(6)
        ]
        assigned = self.assigned(blocks)
        area = self.area()
        with_blocks = self.render(coverage=[(BASE, BASE + DAY)], time_blocks=assigned)
        without = self.render(coverage=[(BASE, BASE + DAY)], time_blocks=[])
        ink = self.changed(with_blocks, without)

        for entry in assigned:
            rect = self.view._time_block_row_rect(area, entry[4])
            self.assertIsNotNone(rect, "sanity: six rows fit in this viewport")
            self.assertTrue(
                ink[rect.top() : rect.bottom() + 1].any(),
                f"row {entry[4]} carries its own block",
            )

    # -- gestures ---------------------------------------------------------

    def double_click(self, x, y, button=Qt.LeftButton):
        """A double click at a point.

        Sent straight to ``mouseDoubleClickEvent`` because that is where Qt puts
        it: the second press of a double click never reaches
        ``mousePressEvent`` -- Qt substitutes ``MouseButtonDblClick`` -- so a
        handler that leaned on the press to do the selecting would find itself
        one event short exactly when the user was being deliberate.
        """
        position = QPointF(float(x), float(y))
        self.view.mouseDoubleClickEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonDblClick,
                position,
                position,
                button,
                button,
                Qt.NoModifier,
            )
        )

    def watch(self):
        """Collect (region, selected-block) emissions from the view."""
        regions, blocks = [], []
        self.view.snipSelected.connect(lambda lo, hi: regions.append((lo, hi)))
        self.view.timeBlockSelected.connect(blocks.append)
        return regions, blocks

    def test_a_double_click_on_a_block_makes_that_block_the_region(self):
        """The block's bounds are already on screen; asking the user to drag
        them out again would be rebuilding by hand what the widget knows."""
        blocks = self.assigned(self.two_overlapping_blocks())
        self.set_state(time_blocks=blocks, coverage=[(BASE, BASE + DAY)])
        row0 = self.view._time_block_row_rect(self.area(), 0)
        regions, selected = self.watch()

        self.double_click(WIDTH // 2, row0.center().y())

        self.assertEqual(regions, [(BASE, BASE + 12 * HOUR)])
        self.assertEqual(selected[0][2], "earlier", "the block is what was clicked")

    def test_a_double_click_off_a_block_selects_nothing(self):
        """Empty space is still empty: no region, no selection, no surprise."""
        blocks = self.assigned(self.two_overlapping_blocks())
        self.set_state(time_blocks=blocks, coverage=[(BASE, BASE + DAY)])
        regions, selected = self.watch()

        # The top of the content area: both rows sit on the floor of it.
        self.double_click(WIDTH // 2, self.area().top() + 1)

        self.assertEqual(regions, [])
        self.assertEqual(selected, [])

    def test_a_double_click_ignores_the_right_button(self):
        """Right-drag is pan. A right double click must not select a region."""
        blocks = self.assigned(self.two_overlapping_blocks())
        self.set_state(time_blocks=blocks, coverage=[(BASE, BASE + DAY)])
        row0 = self.view._time_block_row_rect(self.area(), 0)
        regions, selected = self.watch()

        self.double_click(WIDTH // 2, row0.center().y(), button=Qt.RightButton)

        self.assertEqual(regions, [])
        self.assertEqual(selected, [])


class TimeBlockWidthTests(ViewCase):
    """A block in view is at least a few pixels wide, however far out you are.

    Zoomed out to a year, an hour-long show is a fraction of a pixel. It used to
    be drawn as nothing at all -- and *unreliably* nothing, because the skip was
    ``x1 <= x0``, so whether a block existed depended on which side of a column
    edge its boundaries happened to fall. A block you cannot see is a block the
    interface does not offer, and this is worst exactly when you are looking for
    it: far out, where you are searching for where to zoom in.
    """

    def area(self):
        return self.view._content_rect()

    def painted_columns(self, ink, row):
        """Columns carrying ink anywhere in ``row``'s rectangle."""
        return np.flatnonzero(ink[row.top() : row.bottom() + 1].any(axis=0))

    def test_a_block_below_one_pixel_is_painted_minimum_width(self):
        """An hour at a year to a pixel: widened to the minimum, and no further."""
        self.set_view(BASE, BASE + YEAR)
        hour = self.block("hour", BASE + 100 * DAY, BASE + 100 * DAY + HOUR)
        assigned = self.assigned([hour])
        bare = self.render(coverage=[(BASE, BASE + YEAR)])
        drawn = self.render(coverage=[(BASE, BASE + YEAR)], time_blocks=assigned)
        ink = self.changed(drawn, bare)

        row = self.view._time_block_row_rect(self.area(), 0)
        painted = self.painted_columns(ink, row)
        self.assertEqual(
            self.view.x_of(BASE + 100 * DAY),
            self.view.x_of(BASE + 100 * DAY + HOUR),
            "sanity: the block really is narrower than one pixel here",
        )
        self.assertEqual(
            painted[0],
            self.view.x_of(BASE + 100 * DAY),
            "at the block's own instant, not somewhere else",
        )
        self.assertGreaterEqual(len(painted), 3, "three pixels of block on screen, at least")
        # One more than the minimum, because a rectangle's outline sits on the
        # pixel past its fill -- the minimum, drawn, rather than a bar.
        self.assertLessEqual(len(painted), 4, "and a hairline, not inflated")

    def test_that_minimum_width_block_is_clickable_over_exactly_its_pixels(self):
        """Visible and clickable are the same pixels.

        The claim that matters when a block has been widened: every pixel it
        paints answers for a hit on it, and the pixels just outside do not --
        so what you can see is exactly what you can choose, neither more nor
        less.
        """
        self.set_view(BASE, BASE + YEAR)
        hour = self.block("hour", BASE + 100 * DAY, BASE + 100 * DAY + HOUR)
        bare = self.render(coverage=[(BASE, BASE + YEAR)])
        drawn = self.render(
            coverage=[(BASE, BASE + YEAR)], time_blocks=self.assigned([hour])
        )
        row = self.view._time_block_row_rect(self.area(), 0)
        painted = self.painted_columns(self.changed(drawn, bare), row)
        y = row.center().y()

        for x in painted:
            hit = self.view._time_block_at(int(x), y)
            self.assertIsNotNone(hit, f"x={x} is painted, so it answers")
            self.assertEqual(hit[2], "hour")
        for x in (int(painted[0]) - 1, int(painted[-1]) + 1):
            self.assertIsNone(self.view._time_block_at(x, y), f"x={x} is not painted")

    def test_a_block_at_the_right_edge_widens_back_into_view(self):
        """Widening to the right would run off the end of the content rect; at
        that edge the block grows leftwards instead and still paints in full."""
        self.set_view(BASE, BASE + YEAR)
        last_hour = self.block("last", BASE + YEAR - HOUR, BASE + YEAR)
        assigned = self.assigned([last_hour])
        bare = self.render(coverage=[(BASE, BASE + YEAR)])
        drawn = self.render(coverage=[(BASE, BASE + YEAR)], time_blocks=assigned)
        ink = self.changed(drawn, bare)

        painted = self.painted_columns(ink, self.view._time_block_row_rect(self.area(), 0))
        self.assertEqual(painted.tolist(), [WIDTH - 3, WIDTH - 2, WIDTH - 1])

    def test_a_block_wider_than_the_minimum_keeps_its_own_width(self):
        """Widening fills in what the column grid could not resolve; it is not a
        general inflation. Half a day at half a day to a pixel is half the view,
        from the block's first column to its last and no further."""
        self.set_view(BASE, BASE + DAY)
        half = self.block("half", BASE, BASE + 12 * HOUR)
        assigned = self.assigned([half])
        bare = self.render(coverage=[(BASE, BASE + DAY)])
        drawn = self.render(coverage=[(BASE, BASE + DAY)], time_blocks=assigned)
        ink = self.changed(drawn, bare)

        painted = self.painted_columns(ink, self.view._time_block_row_rect(self.area(), 0))
        self.assertEqual(painted[0], self.view.x_of(BASE), "its own first column")
        self.assertEqual(
            painted[-1], self.view.x_of(BASE + 12 * HOUR), "and its own last"
        )
        self.assertGreater(len(painted), 3, "sanity: this one was never sub-pixel")


class RightClickInstantTests(ViewCase):
    """What the context menu is told about where it was opened.

    A right click records the *pan* origin, never the drag origin, so the menu
    used to be handed 0 in every real case -- "seek cursor from here" jumped to
    1970, and anything keyed to the spot right-clicked (which clip is under the
    pointer, for instance) found nothing there. Hence both the value and the
    warning filter: microseconds do not fit the C long that an ``int`` signal
    declares, and shiboken answers that with a RuntimeWarning and a mangled
    number rather than an exception.
    """

    def right_click(self, x, y=None):
        """A right press and release at a point, without travelling."""
        y = self.view.height() // 2 if y is None else y
        position = QPointF(float(x), float(y))
        self.view.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                position,
                position,
                Qt.RightButton,
                Qt.RightButton,
                Qt.NoModifier,
            )
        )
        self.view.mouseReleaseEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonRelease,
                position,
                position,
                Qt.RightButton,
                Qt.RightButton,
                Qt.NoModifier,
            )
        )

    def test_the_menu_is_told_the_instant_it_was_opened_at(self):
        received = []
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            self.view.requestContextMenu.connect(received.append)
            self.right_click(WIDTH // 3)

        self.assertEqual(len(received), 1, "exactly one menu")
        self.assertEqual(received[0], self.view.instant_at(WIDTH // 3))
        self.assertNotEqual(received[0], 0, "0 is 1970, not where the click was")

    def test_a_right_drag_pans_without_opening_a_menu(self):
        received = []
        self.view.requestContextMenu.connect(received.append)
        self.press(WIDTH // 3, Qt.RightButton)
        self.move(2 * WIDTH // 3, Qt.RightButton)
        self.release(2 * WIDTH // 3, Qt.RightButton)
        self.assertEqual(received, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()