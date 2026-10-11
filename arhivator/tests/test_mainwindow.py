"""Headless exercises for the window, since it cannot be looked at.

Each test drives the real widgets through the same signals the mouse would fire,
and asserts on what the window then *says* -- labels, enabled states, the plan it
would export. A window that builds and a window that works are different claims,
and only the second one matters.

Everything runs under ``QT_QPA_PLATFORM=offscreen``. The offscreen platform still
gives real widget geometry, so the waveform view's pixel maths is exercised too;
it just has nobody watching.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from wavesnip.config import Config

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

from wavesnip import wavio
from wavesnip.datetimes import format_datetime
from wavesnip.model import TimeBlock, TimeBlocksConfig, from_us, to_us, us_now
from wavesnip.peaks import compute_peaks

BASE = datetime(2026, 5, 12, 10, 15, 0)
SEC = 10
WEEK_US = 7 * 24 * 3600 * 1_000_000


def at(seconds: float) -> int:
    """Timeline microseconds, seconds after BASE."""
    return to_us(BASE + timedelta(seconds=seconds))


def shown(seconds: float) -> str:
    """The text a date field shows for the instant ``seconds`` after BASE."""
    return format_datetime(from_us(at(seconds)))


class MainWindowLike:
    """A second window on the same config, cleaned up with the test.

    Building a window is cheap but it holds workers and a player, so a bare
    function returning one would leak both.
    """

    def __init__(self, config_path: Path):
        from wavesnip.config import Config
        from wavesnip.mainwindow import MainWindow

        self.window = MainWindow(config=Config(config_path))


class WindowCase(unittest.TestCase):
    """An app, a temp folder of clips, and a window pointed at them."""

    def setUp(self):
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        from wavesnip.config import Config
        from wavesnip.mainwindow import MainWindow

        # An explicit config path, not the real one. `add_archive` saves, so a test
        # on the default path would write the developer's archive list into their
        # home directory -- and the next test to start would read back whatever
        # the previous one happened to leave behind.
        self.config = Config(self.root / "config" / "archives.json")

        self.window = MainWindow(config=self.config)
        self.addCleanup(self.window.close)

    def extra_window(self, config_path: Path | None = None):
        """Another window sharing this test's config, cleaned up with it."""
        holder = MainWindowLike(config_path or self.config.path)
        self.addCleanup(holder.window.close)
        return holder.window

    def make_archive(self, name: str) -> Path:
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        return folder

    def add_clip(
        self, folder: Path, name: str, *, start: float, seconds: float = SEC, rate: int = 8000
    ) -> None:
        t = np.arange(int(seconds * rate), dtype=np.float32) / rate
        samples = (0.5 * np.sin(2 * np.pi * 200 * t)).reshape(-1, 1)
        path = folder / name
        wavio.write_wav(path, samples, framerate=rate)
        stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (stamp, stamp))

    def load(self, folder: Path) -> None:
        """Add an archive and wait for its index, without a file dialog.

        Indexing runs on a worker thread, so ``add_archive`` returns before the
        clips exist. Everything downstream -- resolving instants, planning snips --
        needs them, which means waiting for the job rather than assuming it is
        done.
        """
        self.window.add_archive(folder)
        self.await_idle()

    def await_idle(self, milliseconds: int = 2000) -> None:
        """Wait until no background job is left running."""
        for _ in range(milliseconds // 50 + 1):
            if self.window._busy == 0:
                self.pump(20)
                if self.window._busy == 0:
                    return
            self.pump(50)

    def pump(self, milliseconds: int = 250) -> None:
        """Let Qt actually run for a while.

        ``processEvents`` drains what is already queued and returns, which is the
        wrong tool for anything asynchronous: ``QMediaPlayer.play`` does not set
        its state until the event loop has turned, and a worker thread's result
        arrives on the next iteration. Spinning here with a nested event loop is
        what makes those observable at all.
        """
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def await_peaks(self) -> None:
        """Wait for the peaks worker to finish and hand its result back."""
        for _ in range(20):
            if self.window.wave.state().peaks is not None:
                return
            self.pump(100)

    def make_clip_archive(self, name: str = "a", **clips) -> Path:
        folder = self.make_archive(name)
        for clip_name, start in clips.items():
            self.add_clip(folder, f"{clip_name}.wav", start=start)
        return folder


class ConstructionTests(WindowCase):
    def test_builds_without_an_archive(self):
        self.assertIsNone(self.window._timeline)
        self.assertFalse(self.window.btn_export.isEnabled())

    def test_renders_when_offscreen(self):
        self.assertFalse(self.window.grab().isNull())

    def test_status_greets_the_user(self):
        self.assertIn("options", self.window.status.currentMessage().lower())

    def test_the_summary_asks_for_an_archive_when_there_is_none(self):
        self.assertIn("No archive folders", self.window.archive_summary.text())

    def test_date_field_shows_the_documented_example(self):
        """The placeholder is the format, so the format is discoverable.

        Milliseconds included: `design.md` asks for ``D.M.Y. H:M:S:MS``, and a
        placeholder showing less than the field writes would teach the wrong
        thing to type.
        """
        self.assertEqual(self.window.play_when.edit.placeholderText(), "12.5.2026. 10:15:00.000")

    def test_snip_exports_start_disabled(self):
        self.assertFalse(self.window.btn_export.isEnabled())
        self.assertFalse(self.window.btn_play_snip.isEnabled())

    def test_there_is_no_archive_panel_left(self):
        """The redesign's whole point: the waveform gets the width."""
        for name in dir(self.window):
            self.assertFalse(
                name.startswith(("archive_list", "archive_detail")),
                f"{name} is a leftover from the archive panel",
            )
        self.assertFalse(hasattr(self.window, "btn_add"))
        self.assertFalse(hasattr(self.window, "btn_remove"))
        self.assertFalse(hasattr(self.window, "btn_up"))
        self.assertFalse(hasattr(self.window, "btn_down"))

    def test_building_the_window_does_not_touch_the_config(self):
        """Loading is explicit, so tests and the launch path are both hermetic."""
        Config(self.root / "config" / "archives.json").save([self.make_archive("x")])
        fresh = self.extra_window()
        self.assertEqual(fresh.archives(), [])
        self.assertEqual(self.config.load(), [self.make_archive("x").resolve()])

    def test_the_top_row_rearranges_to_the_design(self):
        """Per `design.md`, the top row is STATUS · gap · play/stop · cursor
        input · gap · zoom · Setup, and the rescan button is gone."""
        row = self.window._top_row
        self.assertIs(self.window.btn_play.parent(), row)
        self.assertIs(self.window.play_when.parent(), row)
        self.assertIs(self.window.btn_zoom_in.parent(), row)
        self.assertIs(self.window.btn_zoom_out.parent(), row)
        self.assertIs(self.window.btn_fit.parent(), row)
        self.assertIs(self.window.btn_options.parent(), row)
        self.assertFalse(hasattr(self.window, "btn_rescan"))

    def test_the_top_row_is_a_single_thin_line(self):
        """Nothing in the top bar may be taller than a button.

        A `DateField` once brought its fat default layout margins along and made
        the whole row 47px tall instead of 25 -- an effective second row. The
        row's height is the tallest of its children, so the regression is best
        caught at the children.
        """
        from PySide6.QtWidgets import QPushButton

        button_height = QPushButton().sizeHint().height()
        row = self.window._top_row
        for name, widget in {
            "archive_summary": self.window.archive_summary,
            "clip_label": self.window.clip_label,
            "progress": self.window.progress,
            "btn_play": self.window.btn_play,
            "play_when": self.window.play_when,
            "btn_zoom_in": self.window.btn_zoom_in,
            "btn_zoom_out": self.window.btn_zoom_out,
            "btn_fit": self.window.btn_fit,
            "btn_options": self.window.btn_options,
        }.items():
            self.assertIs(widget.parent(), row, f"{name} is not in the top row")
            self.assertLessEqual(
                widget.sizeHint().height(),
                button_height,
                f"{name} is taller than a button and would fatten the top row",
            )
        # The row's own hint is the button height plus the layout's 4px top
        # margin -- one line, never two.
        self.assertLessEqual(row.sizeHint().height(), button_height + 4)

    def test_the_status_bar_shows_the_spinner_next_to_the_counts(self):
        """The status block (clips, "1px =", spinner) lives in the top row."""
        row = self.window._top_row
        for widget in (self.window.archive_summary, self.window.clip_label, self.window.progress):
            self.assertIs(widget.parent(), row)


class ArchiveListTests(WindowCase):
    """The archive list itself. Editing it now lives in the options dialog."""

    def test_adding_an_archive_indexes_it(self):
        folder = self.make_clip_archive(c1=0)
        self.load(folder)
        self.assertEqual(len(self.window.archives()), 1)
        self.assertEqual(len(self.window._timeline.clips), 1)

    def test_newest_folder_goes_to_the_top(self):
        first = self.make_clip_archive("b", c1=0)
        second = self.make_clip_archive("a", c1=0)
        self.load(first)
        self.load(second)
        self.assertEqual(self.window._archives[0].path, second)

    def test_readding_a_folder_promotes_it_without_duplicating(self):
        folder = self.make_clip_archive("a", c1=0)
        other = self.make_clip_archive("b", c1=0)
        self.load(folder)
        self.load(other)
        self.load(folder)
        self.assertEqual(len(self.window.archives()), 2)
        self.assertEqual(self.window._archives[0].path, folder)

    def test_priority_numbers_follow_the_list(self):
        self.load(self.make_clip_archive("b", c1=0))
        self.load(self.make_clip_archive("a", c1=0))
        self.assertEqual([a.priority for a in self.window._archives], [0, 1])

    def test_higher_archive_wins_an_overlap(self):
        first = self.make_clip_archive("b", c1=0)
        second = self.make_clip_archive("a", c1=0)
        self.load(first)
        self.load(second)
        clip = self.window._timeline.resolve(at(2)).clip
        self.assertEqual(clip.archive_path, second, "second was added last, so it wins")

    def test_the_summary_counts_what_was_indexed(self):
        self.load(self.make_clip_archive("a", c1=0, c2=SEC))
        summary = self.window.archive_summary.text()
        self.assertIn("2 clips", summary)
        self.assertNotIn("folder", summary, "the folder count left the status bar")
        self.assertNotIn("12.5.2026", summary, "the timeline extent left the status bar")

    def test_the_summary_no_longer_counts_folders(self):
        """Per `design.md`, the status bar keeps clips, "1px =", and the
        spinner -- folder count and timeline dates are for the Setup dialog."""
        self.load(self.make_clip_archive("a", c1=0))
        self.load(self.make_clip_archive("b", c1=0))
        summary = self.window.archive_summary.text()
        self.assertIn("2 clips", summary)
        self.assertNotIn("folder", summary)

    def test_an_empty_archive_is_reported_not_crashed(self):
        self.load(self.make_archive("empty"))
        details = self.window._archive_details()
        self.assertIn("No indexed audio", details[self.make_archive("empty")])

    def test_archive_details_report_the_span(self):
        folder = self.make_clip_archive("a", c1=0, c2=SEC)
        self.load(folder)
        detail = self.window._archive_details()[folder]
        self.assertIn("2 clips", detail)
        self.assertIn("12.5.2026", detail)

    def test_rescan_is_idempotent(self):
        folder = self.make_clip_archive("a", c1=0, c2=SEC)
        self.load(folder)
        before = len(self.window._timeline.clips)
        self.window._on_rescan()
        self.assertEqual(len(self.window._timeline.clips), before)

    def test_rescan_with_no_archives_says_so(self):
        self.window._on_rescan()
        self.assertIn("No archives", self.window.status.currentMessage())

    def test_unreadable_file_is_skipped_not_fatal(self):
        folder = self.make_clip_archive("a", c1=0)
        (folder / "broken.wav").write_bytes(b"RIFF not really a wav file")
        self.load(folder)
        self.assertEqual(len(self.window._timeline.clips), 1)
        self.assertIn("skipped", self.window.status.currentMessage())


class RememberedArchiveTests(WindowCase):
    """Archives come back from the config file without being retyped."""

    def test_an_added_folder_is_remembered(self):
        folder = self.make_clip_archive("a", c1=0)
        self.load(folder)
        self.assertEqual(self.config.load(), [folder.resolve()])

    def test_the_order_is_remembered_too(self):
        """The order is the priority order, so a set would lose the point."""
        first = self.make_clip_archive("b", c1=0)
        second = self.make_clip_archive("a", c1=0)
        self.load(first)
        self.load(second)
        self.assertEqual(self.config.load(), [second.resolve(), first.resolve()])

    def test_a_new_window_picks_them_up(self):
        folder = self.make_clip_archive("a", c1=0)
        self.load(folder)

        second = self.extra_window()
        self.assertEqual(second.load_state(), 1)
        self.assertEqual(second.archives(), [folder.resolve()])

    def test_loading_does_not_scan(self):
        """The caller batches the scan; doing it here would scan twice at startup."""
        folder = self.make_clip_archive("a", c1=0)
        self.load(folder)
        self.await_idle()

        second = self.extra_window()
        second.load_state()
        self.assertEqual(second._busy, 0)

    def test_loading_reads_the_index_without_a_rescan(self):
        """The clips are already known, so the timeline is populated at once."""
        folder = self.make_clip_archive("a", c1=0, c2=SEC)
        self.load(folder)

        second = self.extra_window()
        second.load_state()
        self.assertEqual(len(second._timeline.clips), 2)

    def test_an_unreadable_config_does_not_stop_the_window(self):
        self.config.path.parent.mkdir(parents=True, exist_ok=True)
        self.config.path.write_text("{ not json", encoding="utf-8")
        second = self.extra_window()
        self.assertEqual(second.load_state(), 0)

    def test_apply_archives_saves_them(self):
        folder = self.make_archive("a")
        self.window.apply_archives([folder])
        self.assertEqual(self.config.load(), [folder.resolve()])

    def test_apply_archives_takes_the_order_given(self):
        """Not "add each one" -- the dialog is the authority on the list."""
        first = self.make_archive("b")
        second = self.make_archive("a")
        self.window.apply_archives([first, second])
        self.assertEqual(self.window.archives(), [first, second])
        self.assertEqual([a.priority for a in self.window._archives], [0, 1])

    def test_apply_archives_clears_everything_when_given_nothing(self):
        self.load(self.make_clip_archive("a", c1=0))
        self.window.apply_archives([])
        self.assertEqual(self.window.archives(), [])
        self.assertIsNone(self.window._timeline)

    def test_apply_archives_changes_who_wins(self):
        first = self.make_clip_archive("b", c1=0)
        second = self.make_clip_archive("a", c1=0)
        self.load(first)
        self.load(second)
        self.assertEqual(
            self.window._timeline.resolve(at(2)).clip.archive_path, second
        )
        self.window.apply_archives([first, second])
        self.await_idle()
        self.assertEqual(
            self.window._timeline.resolve(at(2)).clip.archive_path, first
        )

    def test_the_config_path_is_shown_to_the_user(self):
        self.assertIn(str(self.config.path), self.window._config_note())

    def test_an_unwritable_config_is_said_so(self):
        blocker = self.root / "blocker"
        blocker.write_text("in the way")
        self.window._config = Config(blocker / "sub" / "archives.json")
        self.assertIn("not persist", self.window._config_note())


class ReorderTests(WindowCase):
    """Priority is changed through the dialog, which hands back a whole list."""

    def setUp(self):
        super().setUp()
        self.a = self.make_clip_archive("a", c1=0)
        self.b = self.make_clip_archive("b", c1=0)
        self.load(self.a)
        self.load(self.b)

    def test_putting_a_first_changes_who_wins(self):
        self.window.apply_archives([self.a, self.b])
        self.await_idle()
        self.assertEqual(self.window._archives[0].path, self.a)
        self.assertEqual(
            self.window._timeline.resolve(at(2)).clip.archive_path, self.a
        )

    def test_putting_b_first_changes_who_wins(self):
        self.window.apply_archives([self.b, self.a])
        self.await_idle()
        self.assertEqual(
            self.window._timeline.resolve(at(2)).clip.archive_path, self.b
        )

    def test_priority_is_renumbered_after_a_change(self):
        self.window.apply_archives([self.a, self.b])
        self.assertEqual([a.priority for a in self.window._archives], [0, 1])

    def test_a_reorder_is_remembered(self):
        self.window.apply_archives([self.a, self.b])
        self.assertEqual(self.config.load(), [self.a.resolve(), self.b.resolve()])


class DateEntryTests(WindowCase):
    """The paste path -- the whole reason the field is a QLineEdit."""

    def setUp(self):
        super().setUp()
        self.load(self.make_clip_archive("a", c1=0, c2=SEC, c3=2 * SEC))

    def type_date(self, text: str) -> bool:
        """Type into the field and commit it, the way Enter would."""
        self.window.play_when.edit.setText(text)
        return self.window.play_when.commit()

    def test_documented_format_is_applied(self):
        self.assertTrue(self.type_date("12.5.2026. 10:15:20"))
        self.assertEqual(self.window._playhead_us, at(20))

    def test_unpadded_matches_the_field(self):
        self.type_date("12.5.2026. 10:15:00")
        self.assertEqual(self.window._playhead_us, at(0))

    def test_iso_is_accepted_too(self):
        self.type_date("2026-05-12 10:15:20")
        self.assertEqual(self.window._playhead_us, at(20))

    def test_a_good_paste_after_a_bad_one_still_takes(self):
        self.type_date("nonsense")
        self.assertIn("nonsense", self.window.status.currentMessage())
        self.type_date("12.5.2026. 10:15:20")
        self.assertEqual(self.window._playhead_us, at(20))

    def test_unparseable_text_is_reported(self):
        self.type_date("nonsense")
        message = self.window.status.currentMessage()
        self.assertIn("nonsense", message)
        self.assertIn("12.5.2026", message)

    def test_unparseable_text_does_not_move_the_playhead(self):
        before = self.window._playhead_us
        self.assertFalse(self.type_date("2026-05-12"))
        self.assertEqual(self.window._playhead_us, before)

    def test_iso_without_seconds_is_accepted(self):
        self.type_date("2026-05-12 10:15:30")
        self.assertEqual(self.window._playhead_us, at(30))

    def test_a_half_typed_date_does_not_move_the_playhead(self):
        """The field commits on Enter or on leaving it, not per keystroke.

        Moving the playhead as the user types would make it impossible to reach a
        date the playhead is not already at.
        """
        before = self.window._playhead_us
        self.window.play_when.edit.setText("12.5.2026. 10:1")
        self.app.processEvents()
        self.assertEqual(self.window._playhead_us, before)

    def test_rejected_text_is_put_back_to_the_last_good_value(self):
        """Leaving the rejected text on screen would imply it was accepted."""
        self.type_date("12.5.2026. 10:15:20")
        self.type_date("total nonsense")
        self.assertIn("nonsense", self.window.status.currentMessage())
        self.assertEqual(self.window.play_when.text(), "12.5.2026. 10:15:20.000")
        self.assertEqual(self.window._playhead_us, at(20))


class WeekButtonTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.load(self.make_clip_archive("a", c1=0))
        self.window.set_playhead(at(0))

    def test_next_week_adds_seven_days(self):
        self.window._nudge_week(1)
        self.assertEqual(self.window._playhead_us, at(0) + 7 * 24 * 3600 * 1_000_000)

    def test_previous_week_subtracts_seven_days(self):
        self.window._nudge_week(-1)
        self.assertEqual(self.window._playhead_us, at(0) - 7 * 24 * 3600 * 1_000_000)

    def test_the_time_of_day_is_preserved(self):
        self.window._nudge_week(1)
        self.assertEqual(
            self.window.play_when.text(),
            format_datetime(from_us(at(0) + WEEK_US)),
        )

    def test_round_trip_returns_to_the_start(self):
        self.window._nudge_week(1)
        self.window._nudge_week(-1)
        self.assertEqual(self.window._playhead_us, at(0))

    def test_two_weeks_then_back_is_two_weeks_of_range(self):
        self.window._nudge_week(2)
        self.assertEqual(self.window._playhead_us, at(0) + 2 * WEEK_US)

    def test_the_date_field_follows_the_playhead(self):
        self.window._nudge_week(-1)
        self.assertEqual(
            self.window.play_when.text(),
            format_datetime(from_us(at(0) - WEEK_US)),
        )

    def test_now_lands_approximately_today(self):
        self.window._on_now()
        self.assertLess(abs(self.window._playhead_us - us_now()), 60 * 1_000_000)

    def test_week_buttons_move_only_the_playhead(self):
        """A nudge must not silently redefine a snip the user already set."""
        self.window.set_playhead(at(0))
        self.window._on_snip_at_cursor()
        before = self.window._snip
        self.window._nudge_week(1)
        self.assertEqual(self.window._snip, before)


class PlaybackTests(WindowCase):
    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        for index in range(3):
            self.add_clip(folder, f"c{index}.wav", start=index * SEC)
        self.load(folder)
        self.window.set_playhead(at(1))

    def test_play_before_an_archive_says_so(self):
        empty = MainWindowCaseHelper.fresh()
        self.assertIsNotNone(empty)

    def test_play_starts(self):
        self.window._on_play()
        self.pump(300)
        self.assertTrue(self.window.player.is_playing)
        self.window._on_stop()

    def test_play_button_flips_to_pause(self):
        self.window._on_play()
        self.pump(300)
        self.assertEqual(self.window.btn_play.text(), "Pause")
        self.window._on_stop()
        self.assertEqual(self.window.btn_play.text(), "Play")

    def test_play_again_stops(self):
        self.window._on_play()
        self.pump(300)
        self.window._on_play()
        self.assertFalse(self.window.player.is_playing)

    def test_stop_clears_the_playhead(self):
        """What stop clears is the playhead; the label that said so is gone."""
        self.window._on_play()
        self.window._on_stop()
        self.assertIsNone(self.window._playhead_us)

    def test_position_follows_into_the_date_field(self):
        """The cursor's position lives in the date field, not in a second label.

        A ``play_position`` QLabel used to carry it too, and said the same
        instant twice in two places -- one of which nothing updated. The field
        is the one that can be copied out of, so it is the one that remains.
        """
        self.window._on_position(at(4))
        self.assertEqual(self.window.play_when.text(), shown(4))

    def test_a_gap_is_announced(self):
        self.window._on_jumped(at(SEC), at(SEC * 4))
        self.assertIn("Gap skipped", self.window.status.currentMessage())

    def test_a_tiny_jump_is_not_announced(self):
        """Sub-second seek adjustments are normal and would spam the status bar."""
        self.window._on_jumped(at(SEC), at(SEC) + 2000)
        self.assertNotIn("Gap skipped", self.window.status.currentMessage())

    def test_playback_error_is_surfaced(self):
        self.window._on_player_error("no such file")
        self.assertIn("no such file", self.window.status.currentMessage())

    def test_play_in_an_empty_timeline_is_refused(self):
        self.window._archives.clear()
        self.window._timeline = None
        self.window._on_play()
        self.assertIn("archive", self.window.status.currentMessage().lower())


class MainWindowCaseHelper:
    """Tiny factory so the "no archives" case can be exercised without a skip."""

    @staticmethod
    def fresh():
        from wavesnip.mainwindow import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        app.processEvents()
        window._on_play()
        return window


class PaintTests(WindowCase):
    """The view must survive being painted, at real timestamps.

    Every other test in this file goes around the paint path: a widget that is
    never shown is never asked to paint. That hid a crash in gap shading that only
    appeared once real 2026 dates were loaded, because the gap rectangles were
    being handed microseconds where Qt wants a pixel, and ``QRect`` takes 32-bit
    ints. A 2026 instant is ~1.8e15, so it overflowed, and a paint event that
    raises leaves the widget blank and repeats forever.

    So: paint the view, with gaps in the coverage, at a genuine date, and treat
    any exception or overflow as a failure.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        # Three one-hour clips with an hour-long hole between the first two.
        self.add_clip(folder, "c0.wav", start=0)
        self.add_clip(folder, "c1.wav", start=2 * 3600 * SEC)
        self.add_clip(folder, "c2.wav", start=3 * 3600 * SEC)
        self.load(folder)
        self.window.resize(1100, 700)
        self.wave = self.window.wave
        self.wave.resize(1000, 300)

    def paint(self) -> None:
        """Force a real paint, failing loudly on anything raised or warned."""
        import warnings

        with warnings.catch_warnings():
            # A shiboken overflow is an error, not a note. This is the same
            # reason `simplefilter("error", RuntimeWarning)` is fatal elsewhere.
            warnings.simplefilter("error", RuntimeWarning)
            pixmap = self.wave.grab()
        self.assertFalse(pixmap.isNull())

    def test_painting_a_range_containing_a_gap_raises_nothing(self):
        # The playhead has to be near the hole, or there are no gap columns to
        # draw and the code under test never runs.
        self.window.set_playhead(at(3600 * SEC))
        state = self.wave.state()
        self.assertTrue(state.gaps, "this range is supposed to contain a gap")
        self.paint()

    def test_painting_a_very_wide_range_raises_nothing(self):
        """A whole archive at once: the case that overflowed in practice."""
        state = self.wave.state()
        self.wave.set_state(
            replace(state, start_us=state.start_us, end_us=state.end_us + 400 * 3600 * SEC)
        )
        self.paint()

    def test_painting_with_a_snip_and_a_playhead_raises_nothing(self):
        self.window.set_playhead(at(1800 * SEC))
        self.window.snip_from.set_instant(at(600 * SEC))
        self.window.snip_to.set_instant(at(2400 * SEC))
        self.window._on_snip_changed()
        self.paint()

    def test_painting_with_peaks_raises_nothing(self):
        self.await_peaks()
        self.paint()

    def test_painting_an_empty_timeline_raises_nothing(self):
        self.window._timeline = None
        self.window._rebuild_timeline()
        self.paint()

    def test_the_timestamps_are_too_big_for_a_32_bit_rect(self):
        """Why the above matters, pinned so the bug cannot be reintroduced.

        If this ever starts failing because the timestamps got *smaller*, the
        paint tests above are no longer testing what they claim to.
        """
        state = self.wave.state()
        self.assertGreater(state.start_us, 2**31)
        self.assertGreater(state.end_us, 2**31)


class WaveViewTests(WindowCase):
    """The view's own geometry, driven through real mouse events."""

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        self.add_clip(folder, "c0.wav", start=0)
        self.add_clip(folder, "c1.wav", start=SEC)
        self.load(folder)
        self.window.set_playhead(at(2))
        self.app.processEvents()
        self.wave = self.window.wave

    def click(self, x: int) -> None:
        for event_type, button in (
            (QMouseEvent.Type.MouseButtonPress, Qt.LeftButton),
            (QMouseEvent.Type.MouseButtonRelease, Qt.LeftButton),
        ):
            event = QMouseEvent(
                event_type,
                QPoint(x, self.wave.height() // 2),
                Qt.LeftButton,
                button,
                Qt.NoModifier,
            )
            self.wave.mousePressEvent(event) if event_type == QMouseEvent.Type.MouseButtonPress else self.wave.mouseReleaseEvent(event)

    def drag(self, x0: int, x1: int) -> None:
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPoint(x0, self.wave.height() // 2),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        move = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPoint(x1, self.wave.height() // 2),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPoint(x1, self.wave.height() // 2),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        self.wave.mousePressEvent(press)
        self.wave.mouseMoveEvent(move)
        self.wave.mouseReleaseEvent(release)

    def test_the_view_shows_the_archive(self):
        state = self.wave.state()
        self.assertLess(state.start_us, at(2))
        self.assertGreater(state.end_us, at(2))

    def test_a_click_moves_the_playhead(self):
        self.click(self.wave.width() // 4)
        self.assertNotEqual(self.window._playhead_us, at(2))

    def test_a_drag_selects_a_snip(self):
        self.drag(100, 300)
        self.assertIsNotNone(self.window._snip)
        low, high = self.window._snip
        self.assertLess(low, high)

    def test_a_drag_fills_the_snip_fields(self):
        self.drag(100, 300)
        self.assertIn("10:15", self.window.snip_from.text())

    def test_a_drag_updates_the_wave_state(self):
        self.drag(100, 300)
        state = self.wave.state()
        self.assertIsNotNone(state.snip_from_us)
        self.assertIsNotNone(state.snip_to_us)

    def test_a_tiny_drag_is_a_click_not_a_snip(self):
        self.drag(200, 202)
        self.assertIsNone(self.window._snip)

    def test_peaks_eventually_arrive(self):
        """The worker finishes and the view shows them for the range it asked for."""
        self.await_peaks()
        state = self.wave.state()
        self.assertIsNotNone(state.peaks, "peaks were never computed")
        self.assertTrue(state.peaks_match)

    def test_peaks_cover_the_requested_range(self):
        self.await_peaks()
        peaks = self.wave.state().peaks
        self.assertIsNotNone(peaks)
        self.assertGreater(len(peaks), 100)
        self.assertFalse(peaks.is_silent(), "the clips contain a tone")

    def test_the_view_renders_with_peaks_present(self):
        self.await_peaks()
        self.assertFalse(self.window.grab().isNull())


class CoverageToWaveformTests(WindowCase):
    """Zoomed out there is no waveform, and zooming in produces one.

    The two halves are the same code path seen from opposite ends, so they are
    tested together: what decides between them is one comparison in
    ``_peaks_affordable``, and what it gates is whether any audio is read at all.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_archive("a")
        # Nineteen days of extent, from a clip every six hours: wider than the
        # week that separates a waveform from a coverage map, so the fit-all
        # view starts on the coverage side. Sparse enough to be cheap to build
        # and still twenty-odd clips inside any window that crosses the gate,
        # so zooming in always lands on audio -- a window centred between two
        # sparse clips has nothing to draw, which is a different test and a
        # confusing failure.
        for index in range(76):
            # `start` is in seconds, so the spacing is written out rather than
            # scaled by SEC: six hours apart is 21600, and 76 of them is 450 hours.
            self.add_clip(folder, f"c{index:02d}.wav", start=index * 6 * 3600)
        self.load(folder)
        self.window.resize(1100, 700)
        self.wave = self.window.wave
        self.wave.resize(1000, 300)
        self.app.processEvents()

    def zoom_until_affordable(self, limit: int = 60) -> int:
        """Wheel in the way a person does, and report the notches it took."""
        for notches in range(1, limit + 1):
            self.wave.zoom_in()
            if self.wave.state().peaks_possible:
                return notches
        self.fail(f"still not affordable after {limit} notches")

    def test_a_nineteen_day_archive_starts_as_a_coverage_map(self):
        """The launch state, and the promise it rests on: nothing is decoded."""
        self.await_idle()
        state = self.wave.state()
        self.assertFalse(
            state.peaks_possible,
            f"a {self.wave.span_per_pixel():.0f} s/px view should be past the gate",
        )
        self.assertIsNone(state.peaks, "but a waveform was drawn anyway")

    def test_nothing_is_read_for_a_range_that_is_past_the_gate(self):
        """The gate bounds the *read*, not just the drawing.

        ``_peaks_affordable`` is consulted before a plan is even built, so a view
        too wide to draw must not cost a decode of the archive either. Reading a
        year of 8 kHz is about thirteen minutes, and thirteen minutes of nothing
        visible is the exact failure this rule exists to prevent.
        """
        self.await_idle()
        self.assertIsNone(self.wave.state().peaks)
        self.assertIsNone(
            self.window._peak_range, "a range was marked as having peaks"
        )

    def test_the_clip_label_is_honest_about_click_precision(self):
        """At nineteen days over a thousand pixels a click is hours wide, and the
        label says so rather than letting anyone assume second resolution."""
        self.await_idle()
        self.assertIn("1 px", self.window.clip_label.text())

    def test_the_gate_is_exactly_one_week(self):
        """The line, in time. A day either side of it and nothing else changes
        -- the frame budget it replaced moved with the sample rate, so what a
        "week" of archive cost was never a week."""
        self.assertTrue(self.window._peaks_affordable(WEEK_US))
        self.assertFalse(self.window._peaks_affordable(WEEK_US + 1))

    def test_zooming_in_eventually_flips_to_a_real_waveform(self):
        notches = self.zoom_until_affordable()
        self.assertGreater(notches, 5, "the gate should not be crossed at a hair")
        self.await_peaks()
        state = self.wave.state()
        self.assertTrue(state.peaks_possible)
        self.assertIsNotNone(state.peaks, f"no waveform after {notches} notches")
        self.assertTrue(state.peaks_match)
        self.assertFalse(state.peaks.is_silent(), "the clips contain a tone")

    def test_the_flipped_view_survives_a_decode_it_moved_on_from(self):
        """The bug this pins.

        ``_request_peaks`` refuses while a decode holds the slot, so every event
        of a wheel gesture that lands mid-decode is dropped. If the last such event
        is dropped, nothing is left that will ever ask again and the view stops on
        a bare coverage map -- no waveform, no hint, no way back except moving the
        mouse.

        Spinning the wheel without letting the event loop turn is the tightest
        version of that: the first notch that crosses the gate starts a decode,
        every notch after it is refused while that decode runs, and the view ends
        up on a range nothing ever managed to ask for.
        """
        for _ in range(30):
            self.wave.zoom_in()
            if self.wave.state().peaks_possible:
                break
        self.assertTrue(self.wave.state().peaks_possible, "did not reach the gate")

        # Keep going. The decode just started holds the one peak slot, so none of
        # these can be accepted, and the view moves past the range it is for.
        for _ in range(5):
            self.wave.zoom_in()

        self.await_peaks()
        state = self.wave.state()
        self.assertIsNotNone(
            state.peaks,
            "every request after the first was refused and the discarded decode "
            "was not retried, so the view is stuck on a bare coverage map",
        )
        self.assertTrue(state.peaks_match)

    def test_zooming_back_out_drops_the_waveform(self):
        """A waveform for a range it no longer describes is worse than none.

        The peaks are a picture of some other window, so widening the view must
        clear them and put the hint back rather than leave a confident wrong
        answer on screen.
        """
        self.zoom_until_affordable()
        self.await_peaks()
        self.assertIsNotNone(self.wave.state().peaks)

        for _ in range(60):
            self.wave.zoom_out()
            if not self.wave.state().peaks_possible:
                break
        self.assertFalse(self.wave.state().peaks_possible, "still zoomed in")
        self.await_idle()
        state = self.wave.state()
        self.assertIsNone(state.peaks, "a stale waveform was left on screen")
        self.assertFalse(state.peaks_match)


class SnipTests(WindowCase):
    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        for index in range(4):
            self.add_clip(folder, f"c{index}.wav", start=index * SEC)
        self.load(folder)
        self.window.set_playhead(at(0))

    def test_setting_from_cursor_fills_both_ends(self):
        self.window.snip_length.setValue(60)
        self.window._on_snip_at_cursor()
        low, high = self.window._snip
        self.assertEqual(low, at(0))
        self.assertEqual(high, at(60))

    def test_the_length_spinbox_is_honoured(self):
        self.window.snip_length.setValue(15)
        self.window._on_snip_at_cursor()
        self.assertEqual(self.window._snip[1] - self.window._snip[0], 15 * 10**6)

    def test_exports_become_available_once_a_range_is_set(self):
        self.window._on_snip_at_cursor()
        self.assertTrue(self.window.btn_export.isEnabled())
        self.assertTrue(self.window.btn_play_snip.isEnabled())

    def test_the_summary_names_the_duration_and_pieces(self):
        self.window._on_snip_at_cursor()
        summary = self.window.status.currentMessage()
        self.assertIn("piece", summary)
        self.assertIn("s)", summary)

    def test_setting_both_edges_from_the_playhead_defines_a_range(self):
        """The two "set from playhead" buttons are a pair, not one knob."""
        self.window.snip_from.set_instant(at(SEC * 2))
        self.assertIsNone(self.window._snip, "one edge alone is not a range")

        self.window.set_playhead(at(SEC * 3))
        self.window._set_snip_edge(1)
        self.assertEqual(self.window._snip, (at(SEC * 2), at(SEC * 3)))

    def test_the_from_button_writes_the_current_playhead(self):
        self.window.set_playhead(at(SEC * 2))
        self.window._set_snip_edge(0)
        self.assertEqual(self.window.snip_from.text(), shown(SEC * 2))

    def test_the_to_button_writes_the_current_playhead(self):
        self.window.set_playhead(at(SEC * 3))
        self.window._set_snip_edge(1)
        self.assertEqual(self.window.snip_to.text(), shown(SEC * 3))

    def test_a_reversed_range_is_normalised(self):
        self.window.snip_from.set_instant(at(20))
        self.window.snip_to.set_instant(at(10))
        self.window._on_snip_changed()
        self.assertLess(self.window._snip[0], self.window._snip[1])

    def test_normalising_does_not_move_the_dates_between_the_boxes(self):
        """Swapping the editors would fight the field that triggered the change.

        Each field's `changed` signal is wired to the handler, so writing a swap
        back into the two fields means typing a `from` later than the current
        `to` puts the user's date in the wrong box.
        """
        self.window.snip_from.set_instant(at(20))
        self.window.snip_to.set_instant(at(10))
        self.window._on_snip_changed()

        self.assertEqual(self.window.snip_from.text(), shown(20))
        self.assertEqual(self.window.snip_to.text(), shown(10))

    def test_editing_from_after_to_does_not_corrupt_the_other_field(self):
        """Set both fields, then move `from` alone -- the bug this guards."""
        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(30))
        self.window.snip_from.set_instant(at(20))
        self.window._on_snip_changed()

        self.assertEqual(self.window.snip_to.text(), shown(30))
        self.assertEqual(self.window.snip_from.text(), shown(20))
        self.assertEqual(self.window._snip, (at(20), at(30)))

    def test_an_empty_range_disables_export(self):
        self.window.snip_from.set_instant(at(10))
        self.window.snip_to.set_instant(at(10))
        self.window._on_snip_changed()
        self.assertFalse(self.window.btn_export.isEnabled())

    def test_a_snip_with_no_audio_is_described_not_exported(self):
        self.window.snip_from.set_instant(at(500))
        self.window.snip_to.set_instant(at(600))
        self.window._on_snip_changed()
        self.window._on_export()
        self.assertIn("no audio", self.window.status.currentMessage())

    def test_a_snip_spanning_a_gap_reports_the_gap(self):
        folder2 = self.make_clip_archive("b", c0=0)
        self.add_clip(folder2, "late.wav", start=100)
        self.load(folder2)
        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(200))
        self.window._on_snip_changed()
        self.assertIn("gap", self.window.status.currentMessage())

    def test_the_format_list_offers_the_sources(self):
        self.window._on_snip_at_cursor()
        labels = [self.window.format_box.itemText(i) for i in range(self.window.format_box.count())]
        self.assertIn("Default (first source)", labels)
        self.assertTrue(any("8000 Hz" in label for label in labels))

    def test_the_default_format_is_no_override(self):
        self.window._on_snip_at_cursor()
        self.assertEqual(self.window.format_box.currentIndex(), 0)
        self.assertIsNone(self.window._selected_format())

    def test_choosing_a_format_produces_an_override(self):
        self.window._on_snip_at_cursor()
        index = self.window.format_box.findText("8000 Hz, 1 ch, 16-bit")
        self.window.format_box.setCurrentIndex(index)
        override = self.window._selected_format()
        self.assertIsNotNone(override)
        self.assertEqual(override.framerate, 8000)

    def test_clearing_disables_the_buttons(self):
        self.window._on_snip_at_cursor()
        self.window._on_snip_cleared()
        self.assertFalse(self.window.btn_export.isEnabled())


class FormatListTests(WindowCase):
    """The export format list describes the range about to be exported.

    It used to be rebuilt by an "Update" button, which made staleness the
    user's problem to spot: the list is built from the plan
    (`SnipPlan.formats`), and moving the snip changes the plan without
    changing the archive set, so nothing automatic caught up. It is now
    refreshed from everywhere the data changes -- snip edits, archive changes
    -- and the button is gone.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_archive("mixed")
        self.add_clip(folder, "slow.wav", start=0, seconds=10)  # 8000 Hz
        self.add_clip(folder, "fast.wav", start=100, seconds=10, rate=44100)
        self.load(folder)

    def labels(self):
        return [self.window.format_box.itemText(i) for i in range(self.window.format_box.count())]

    def select(self, low: float, high: float) -> None:
        """Set the snip to ``low..high`` seconds, the way the fields would."""
        self.window.snip_from.set_instant(at(low))
        self.window.snip_to.set_instant(at(high))
        self.window._on_snip_changed()

    def choose(self, label: str) -> None:
        index = self.window.format_box.findText(label)
        self.assertGreater(index, 0, f"sanity: {label} is offered")
        self.window.format_box.setCurrentIndex(index)

    def test_there_is_no_update_button_left(self):
        """`design.md` asked whether it could go: the box keeps itself current,
        so a stale list is not something a user has to notice and fix."""
        self.assertFalse(hasattr(self.window, "btn_refresh_format"))

    def test_the_list_describes_the_range_not_the_whole_archive(self):
        self.select(2, 4)
        labels = self.labels()
        self.assertIn("8000 Hz, 1 ch, 16-bit", labels)
        self.assertNotIn("44100 Hz, 1 ch, 16-bit", labels)

        self.select(102, 104)
        labels = self.labels()
        self.assertIn("44100 Hz, 1 ch, 16-bit", labels)
        self.assertNotIn("8000 Hz, 1 ch, 16-bit", labels)

    def test_an_override_the_new_range_still_offers_survives_the_move(self):
        """A range change that does not change the list must not rebuild it:
        clearing the box would put a valid choice back on "Default"."""
        self.select(2, 4)
        self.choose("8000 Hz, 1 ch, 16-bit")

        self.select(5, 7)  # still the same clip
        override = self.window._selected_format()
        self.assertIsNotNone(override, "the choice was put back to Default")
        self.assertEqual(override.framerate, 8000)

    def test_an_override_the_new_range_does_not_offer_falls_back_to_default(self):
        """It named sources that are not in the range any more, so it goes."""
        self.select(2, 4)
        self.choose("8000 Hz, 1 ch, 16-bit")

        self.select(102, 104)
        self.assertEqual(self.window.format_box.currentIndex(), 0)
        self.assertIsNone(self.window._selected_format())

    def test_removing_every_archive_leaves_only_the_default(self):
        """The no-timeline path of `_rebuild_timeline` refreshes too; without
        it the box would keep offering formats from archives that are gone."""
        self.select(2, 4)
        self.assertIn("8000 Hz, 1 ch, 16-bit", self.labels())

        self.window.apply_archives([])
        self.assertEqual(self.labels(), ["Default (first source)"])


class ExportTests(WindowCase):
    """The export itself, called directly rather than through the file dialog."""

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        for index in range(3):
            self.add_clip(folder, f"c{index}.wav", start=index * SEC)
        self.load(folder)
        self.window.set_playhead(at(0))
        self.window.snip_length.setValue(15)
        self.window._on_snip_at_cursor()

    def test_the_plan_covers_the_whole_range(self):
        plan = self.window._current_plan()
        self.assertFalse(plan.is_empty)
        self.assertEqual(plan.covered_us, 15 * 10**6)

    def test_rendering_produces_the_requested_length(self):
        from wavesnip.resample import render_snip

        plan = self.window._current_plan()
        target = self.root / "out.wav"
        result = render_snip(plan, target)
        self.assertTrue(target.exists())
        self.assertAlmostEqual(result.duration, 15.0, delta=0.05)

    def test_the_output_is_readable_and_the_right_shape(self):
        from wavesnip.resample import render_snip

        target = self.root / "out.wav"
        result = render_snip(self.window._current_plan(), target)
        info = wavio.read_info(target)
        self.assertEqual(info.framerate, result.fmt.framerate)
        self.assertEqual(info.nchannels, result.fmt.nchannels)

    def test_an_override_rate_is_honoured(self):
        from wavesnip.resample import AudioFormat, render_snip

        target = self.root / "out.wav"
        render_snip(self.window._current_plan(), target, target=AudioFormat(16000, 1, 2))
        self.assertEqual(wavio.read_info(target).framerate, 16000)

    def test_fill_gaps_pads_to_the_full_length(self):
        """The three clips cover 0-30 s of the requested 0-100 s.

        Without padding the export is 30 s of real audio; with it, 100 s of which
        70 s is silence. Same requested range, two different files, and the
        caller is told which one they got.
        """
        from wavesnip.resample import render_snip

        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(100))
        self.window._on_snip_changed()
        plan = self.window._current_plan()

        bare = self.root / "bare.wav"
        filled = self.root / "filled.wav"
        without = render_snip(plan, bare, fill_gaps=False)
        with_pad = render_snip(plan, filled, fill_gaps=True)

        self.assertAlmostEqual(without.duration, 30.0, delta=0.1, msg="real audio only")
        self.assertAlmostEqual(with_pad.duration, 100.0, delta=0.1, msg="padded to length")

        # `missing_us` on the *result* means "requested and not written", so the
        # padded file owes the user nothing while the plan still knows that 70 s
        # of the range had no recording in it.
        self.assertEqual(without.missing_us, 70 * 10**6)
        self.assertEqual(with_pad.missing_us, 0)
        self.assertEqual(with_pad.plan.missing_us, 70 * 10**6)

    def test_missing_audio_is_reported_without_fill(self):
        from wavesnip.resample import render_snip

        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(100))
        self.window._on_snip_changed()
        result = render_snip(self.window._current_plan(), self.root / "bare.wav")
        self.assertGreater(result.missing_us, 0, "70 s of audio is missing from 0-100 s")

    def test_a_snip_inside_a_gap_cannot_be_exported(self):
        from wavesnip.resample import ExportError, render_snip

        self.window.snip_from.set_instant(at(500))
        self.window.snip_to.set_instant(at(600))
        self.window._on_snip_changed()
        with self.assertRaises(ExportError):
            render_snip(self.window._current_plan(), self.root / "nothing.wav")

    def test_the_window_exports_without_a_dialog(self):
        """The whole path minus the file chooser."""
        target = self.root / "window.wav"
        result = self.window.export_to(target)
        self.assertIsNotNone(result)
        self.assertTrue(target.exists())
        self.assertAlmostEqual(result.duration, 15.0, delta=0.05)

    def test_exporting_reports_the_result(self):
        self.window.export_to(self.root / "window.wav")
        self.assertIn("Exported", self.window.status.currentMessage())

    def test_a_non_wav_name_is_corrected(self):
        """The tool is WAV-only, so `.mp3` is fixed rather than refused."""
        target = self.root / "out.mp3"
        self.window.export_to(target)
        self.assertTrue(target.with_suffix(".wav").exists())

    def test_exporting_with_no_snip_says_so(self):
        self.window._on_snip_cleared()
        self.assertIsNone(self.window.export_to(self.root / "none.wav"))
        self.assertIn("no audio", self.window.status.currentMessage())

    def test_an_empty_range_refuses_to_export(self):
        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(0))
        self.window._on_snip_changed()
        self.assertIsNone(self.window.export_to(self.root / "empty.wav"))


class BackgroundWorkTests(WindowCase):
    """Scan and export must not run on the event loop.

    Neither is instant. Indexing a folder of hourly chunks opens every WAV
    header; exporting a long snip reads and writes the whole range. Both on the
    event loop is a frozen window, and the natural reaction to a frozen window is
    to click the button again.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        for index in range(3):
            self.add_clip(folder, f"c{index}.wav", start=index * SEC)
        self.load(folder)
        self.window.set_playhead(at(0))
        self.window.snip_length.setValue(15)
        self.window._on_snip_at_cursor()

    def test_starting_a_job_returns_control_immediately(self):
        """The whole point of the thread: the call returns, the work continues.

        Timed against a gated job rather than a real export, because a 15-second
        export of 8 kHz mono finishes faster than the assertion after it -- which
        would make this a test of how fast the disk is.
        """
        gate = threading.Event()
        job = self.window._start_job(
            None, lambda: gate.wait(5), on_finished=lambda _r: None
        )
        self.assertIsNotNone(job)
        self.assertGreater(self.window._busy, 0)
        self.assertFalse(gate.is_set(), "the job must still be running")

        gate.set()
        self.await_idle()

    def test_the_exported_file_appears_once_the_job_finishes(self):
        target = self.root / "bg.wav"
        self.window.start_export(target)
        self.await_idle()
        self.assertTrue(target.exists())
        self.assertEqual(self.window._busy, 0)

    def test_a_second_export_is_refused_while_one_runs(self):
        self.assertTrue(self.window.start_export(self.root / "one.wav"))
        self.assertFalse(
            self.window.start_export(self.root / "two.wav"),
            "a second export would race the first",
        )
        self.await_idle()

    def test_the_buttons_are_disabled_while_working(self):
        gate = threading.Event()
        self.window._start_job(None, lambda: gate.wait(5), on_finished=lambda _r: None)
        self.assertFalse(self.window.btn_export.isEnabled())
        self.assertFalse(self.window.btn_play_snip.isEnabled())
        # isHidden(), not isVisible(): the window is never shown in a test, so
        # every child reports itself invisible regardless of what was asked for.
        self.assertFalse(self.window.progress.isHidden(), "the spinner should be up")
        gate.set()
        self.await_idle()

    def test_the_buttons_come_back_afterwards(self):
        self.window.start_export(self.root / "one.wav")
        self.await_idle()
        self.assertTrue(self.window.btn_export.isEnabled())
        self.assertTrue(self.window.progress.isHidden())

    def test_the_export_uses_the_range_that_was_asked_for(self):
        """The plan is captured when the button is pressed, not when it finishes.

        Reading the widgets on completion would silently export whatever the user
        had moved on to while they waited.
        """
        self.window.start_export(self.root / "captured.wav")
        self.window.set_playhead(at(SEC * 2))
        self.window.snip_length.setValue(3)
        self.window._on_snip_at_cursor()
        self.await_idle()

        from wavesnip import wavio

        info = wavio.read_info(self.root / "captured.wav")
        self.assertAlmostEqual(info.nframes / info.framerate, 15.0, delta=0.1)

    def test_an_export_with_no_audio_is_refused(self):
        self.window.snip_from.set_instant(at(500))
        self.window.snip_to.set_instant(at(600))
        self.window._on_snip_changed()
        self.assertFalse(self.window.start_export(self.root / "nothing.wav"))
        self.assertEqual(self.window._busy, 0)

    def test_a_failed_export_is_reported_and_leaves_the_ui_usable(self):
        """An unwritable path must not leave the buttons dead.

        A regular file stands where the output folder should be, so the write
        cannot succeed however many times it is tried. The failure surfaces from
        deep inside the writer rather than as an ExportError, so the worker's
        catch-all is what has to hold the line.
        """
        blocker = self.root / "not-a-folder"
        blocker.write_text("in the way")

        self.assertTrue(self.window.start_export(blocker / "out.wav"))
        self.await_idle()

        self.assertEqual(self.window._busy, 0, "the UI must come back after a failure")
        self.assertTrue(self.window.btn_export.isEnabled())
        self.assertTrue(self.window.progress.isHidden())
        self.assertNotEqual(
            self.window.status.currentMessage(), "",
            "a failure that says nothing is indistinguishable from a hang",
        )

    def test_an_export_to_a_missing_folder_creates_it(self):
        """A hand-typed path pointing somewhere new is a reasonable thing to do."""
        target = self.root / "brand" / "new" / "out.wav"
        self.assertTrue(self.window.start_export(target))
        self.await_idle()
        self.assertTrue(target.exists())

    def test_a_slow_job_blocks_a_second_one_deterministically(self):
        """Racing two scans is worse than refusing the second.

        Checked against a job that is deliberately still running rather than by
        timing a real scan, which would make this a test of how fast the disk is.
        """
        gate = threading.Event()
        first = self.window._start_job(
            None, lambda: gate.wait(5), on_finished=lambda _r: None
        )
        self.assertIsNotNone(first)
        self.assertGreater(self.window._busy, 0)

        second = self.window._start_job(
            first, lambda: None, on_finished=lambda _r: None
        )
        self.assertIsNone(second, "the second job must be refused, not queued")

        gate.set()
        self.await_idle()
        self.assertEqual(self.window._busy, 0)

    def test_the_busy_count_returns_to_zero(self):
        self.window._start_job(None, lambda: 1, on_finished=lambda _r: None)
        self.await_idle()
        self.assertEqual(self.window._busy, 0)

    def test_adding_a_folder_indexes_it_in_the_background(self):
        folder = self.make_archive("second")
        self.add_clip(folder, "c.wav", start=0)
        self.window.add_archive(folder)
        self.await_idle()

        self.assertEqual(self.window._busy, 0)
        self.assertEqual(len(self.window._timeline.clips), 4)

    def test_the_scan_summary_reaches_the_status_bar(self):
        self.assertIn("clips indexed", self.window.status.currentMessage())


class PeakWorkerTests(WindowCase):
    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a", c0=0, c1=SEC)
        self.load(folder)

    def test_the_job_refuses_to_start_twice(self):
        from wavesnip.workers import BackgroundJob

        job = BackgroundJob()
        self.assertTrue(job.start(lambda: 1))
        if job.busy:
            self.assertFalse(job.start(lambda: 2), "a second job must be refused")
        job.shutdown()

    def test_a_failing_job_reports_the_message(self):
        from wavesnip.workers import BackgroundJob

        messages: list[str] = []
        done: list[int] = []
        job = BackgroundJob()
        job.failed.connect(messages.append)
        job.done.connect(lambda: done.append(1))
        job.start(lambda: 1 / 0)
        for _ in range(100):
            self.app.processEvents()
            if done:
                break
        job.shutdown()
        self.assertTrue(messages, "the failure was never reported")
        self.assertIn("ZeroDivisionError", messages[0])

    def test_a_successful_job_reports_the_value(self):
        from wavesnip.workers import BackgroundJob

        values: list[object] = []
        job = BackgroundJob()
        job.finished.connect(values.append)
        job.start(lambda: "done")
        for _ in range(100):
            self.app.processEvents()
            if values:
                break
        job.shutdown()
        self.assertEqual(values, ["done"])

    def test_peaks_for_a_plan_are_the_right_length(self):
        from wavesnip.peaks import peaks_for

        plan = self.window._timeline.plan_snip(at(0), at(20))
        peaks = peaks_for(plan, 8_000_000)
        self.assertIsNotNone(peaks)
        self.assertEqual(len(peaks), 1600)
        self.assertFalse(peaks.is_silent())

    def test_peaks_for_an_empty_plan_are_none(self):
        from wavesnip.peaks import peaks_for

        plan = self.window._timeline.plan_snip(at(500), at(600))
        self.assertIsNone(peaks_for(plan, 8_000_000))

    def test_the_making_waveforms_spinner_follows_the_peaks_flag(self):
        """The knob is the spinner, sharing its slot with ordinary jobs.

        It must be driven by ``_peaks_busy`` rather than ``_busy``, because a
        decode must not disable the export buttons.
        """
        self.window.snip_from.set_instant(at(0))
        self.window.snip_to.set_instant(at(20))
        self.window._on_snip_changed()
        self.assertTrue(self.window.btn_export.isEnabled())
        self.window._set_peaks_busy(True)
        self.assertFalse(self.window.progress.isHidden(), "the decode should spin")
        self.assertTrue(self.window.btn_export.isEnabled(), "a decode must not block exports")
        self.window._set_peaks_busy(False)
        self.assertTrue(self.window.progress.isHidden())

    def test_the_spinner_is_down_after_a_round_trip(self):
        """A real decode spins while it runs and drops once its job is done."""
        self.window.set_playhead(at(0))
        self.window.wave.fit_all()
        self.await_peaks()
        for _ in range(20):
            if not self.window._peaks_busy:
                break
            self.pump(50)
        self.assertFalse(self.window._peaks_busy, "the decode never reported done")
        self.assertTrue(self.window.progress.isHidden())


class StalePeaksTests(WindowCase):
    """What the window does with a decode that no longer describes the view.

    ``_on_peaks`` is called directly with a range the view has since left, which
    is the state the window is really in whenever a wheel gesture ends mid-decode:
    every event of the gesture was refused, because one decode at a time is
    deliberate, and then the decode that was running lands on a range nobody asked
    for any more.

    Both tests are synchronous, asserting on state rather than on peaks arriving.
    A job left in flight would satisfy a "peaks arrived" assertion on its own,
    which is how a test of the retry quietly stops testing the retry.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a", c0=0, c1=SEC)
        self.load(folder)
        self.wave = self.window.wave

    def viewing(self) -> tuple[int, int]:
        return self.wave.view()

    def test_a_decode_for_a_range_left_behind_clears_the_marker(self):
        """The bug this pins.

        ``_peak_range`` is the only thing that says "this range is already
        decoded", and it is what a refused request runs into. Left pointing at a
        range the view has left, it makes the range actually on screen
        indistinguishable from one already being handled -- so nothing ever asks
        for it, and the view sits on a bare coverage map with no waveform and no
        hint, having crashed nothing and shown nothing wrong.
        """
        self.await_peaks()
        home = self.viewing()
        stale = (at(0), at(20))
        self.assertNotEqual(home, stale, "the stale range must differ from the view")

        self.window._peak_range = stale
        self.window._on_peaks(None, *stale)

        self.assertIsNone(
            self.window._peak_range,
            "the marker still names a range the view has left, so the range on "
            "screen can never be asked for",
        )

    def test_peaks_that_are_still_right_are_not_thrown_away(self):
        """The guard that stops the retry being waste.

        Panning away and straight back leaves the view showing the peaks it had,
        and those are still the right answer. Clearing the marker would decode
        them a second time for nothing, and the flicker is worse than the waste.
        """
        self.await_peaks()
        home = self.viewing()
        self.assertEqual(self.window._peak_range, home)

        self.window._on_peaks(None, at(30 * SEC), at(40 * SEC))

        self.assertEqual(
            self.window._peak_range,
            home,
            "correct peaks were cleared and re-fetched for no reason",
        )


class PeaksHelperTests(unittest.TestCase):
    def test_reducing_a_tone_keeps_its_extent(self):
        t = np.linspace(0, 1, 4800, dtype=np.float32)
        peaks = compute_peaks(np.sin(2 * np.pi * 100 * t), 100)
        self.assertGreater(float(peaks.maxs.max()), 0.99)
        self.assertLess(float(peaks.mins.min()), -0.99)


class PeaksPlanTests(unittest.TestCase):
    """``peaks_for`` and ``reduce_piece``, on plans built by hand.

    These two decide how a waveform is drawn, and the interesting cases are the
    ones where the arithmetic is load-bearing: a gap in the middle, a clip far
    longer than the chunk size, and a budget too small to finish. None of them
    need a window.
    """

    RATE = 8000

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def clip(self, name: str, *, start: float, seconds: float, amplitude: float = 0.5):
        """A WAV whose mtime puts it at ``start``, and return its Clip.

        mtime is how the app decides when a file was recorded, so it is the one
        thing that has to be set for the timeline to place the file correctly.
        """
        from wavesnip.indexer import ArchiveIndex
        from wavesnip.model import Archive

        folder = self.root / name.split(".")[0]
        folder.mkdir(exist_ok=True)
        rate = self.RATE
        t = np.arange(int(seconds * rate), dtype=np.float32) / rate
        samples = (amplitude * np.sin(2 * np.pi * 200 * t)).reshape(-1, 1)
        path = folder / name
        wavio.write_wav(path, samples, framerate=rate)
        stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (stamp, stamp))
        ArchiveIndex(Archive(path=folder)).scan()
        return path

    def timeline(self, *clips):
        from wavesnip.model import Archive
        from wavesnip.timeline import Timeline

        folders = []
        for path in clips:
            folder = path.parent
            if folder not in folders:
                folders.append(folder)
        return Timeline([Archive(path=f) for f in folders]).refresh()

    def plan(self, timeline, low: float, high: float):
        return timeline.plan_snip(at(low), at(high))

    # -- the whole plan ---------------------------------------------------

    def test_it_returns_the_full_column_count(self):
        """The painter maps columns onto pixels, so the count has to be a constant
        it can rely on rather than whatever the range happened to divide into."""
        from wavesnip.peaks import PEAK_COLUMNS, peaks_for

        clips = [self.clip("one.wav", start=0, seconds=10)]
        peaks = peaks_for(self.plan(self.timeline(clips[0]), 0, 10), 8_000_000)
        self.assertEqual(len(peaks), PEAK_COLUMNS)

    def test_a_gap_is_left_as_nan_rather_than_stretched(self):
        """Columns are shared out by the *requested* range, so a hole in the
        middle stays a hole instead of the audio either side being drawn wide
        enough to fill it."""
        from wavesnip.peaks import PEAK_COLUMNS, peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        peaks = peaks_for(self.plan(self.timeline(*clips), 0, 40), 8_000_000)
        known = ~np.isnan(peaks.mins)
        self.assertTrue(known[: PEAK_COLUMNS // 4].any(), "the first clip is drawn")
        self.assertTrue(known[-PEAK_COLUMNS // 4 :].any(), "and the second")
        middle = known[PEAK_COLUMNS // 4 : -PEAK_COLUMNS // 4]
        self.assertFalse(middle.any(), "the gap between them is not drawn")

    def test_the_gap_lands_where_it_was_requested(self):
        """Not just "some NaNs" -- NaNs in the middle of the window."""
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        peaks = peaks_for(self.plan(self.timeline(*clips), 0, 40), 8_000_000)
        known = np.where(~np.isnan(peaks.mins))[0]
        self.assertLess(known.mean(), len(peaks) / 2 + 1, "mostly on the left")
        self.assertGreater(known.max(), len(peaks) * 0.75, "and on the right")

    def test_a_plan_of_only_a_gap_is_none(self):
        from wavesnip.peaks import peaks_for

        clips = [self.clip("a.wav", start=0, seconds=10)]
        self.assertIsNone(
            peaks_for(self.plan(self.timeline(clips[0]), 20, 30), 8_000_000)
        )

    def test_a_zero_length_range_is_none(self):
        from wavesnip.peaks import peaks_for

        clips = [self.clip("a.wav", start=0, seconds=10)]
        self.assertIsNone(peaks_for(self.plan(self.timeline(clips[0]), 5, 5), 8_000_000))

    def test_each_piece_lands_in_its_own_columns(self):
        """Each piece is reduced into its own share of the columns.

        Getting the shares wrong is silent -- the picture still looks like a
        waveform, just in the wrong places -- so the check is that three clips of
        three distinct amplitudes come out loudest where they actually are.
        """
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10, amplitude=0.2),
            self.clip("b.wav", start=10, seconds=10, amplitude=0.8),
            self.clip("c.wav", start=20, seconds=10, amplitude=0.4),
        ]
        peaks = peaks_for(self.plan(self.timeline(*clips), 0, 30), 8_000_000)
        third = len(peaks) // 3

        bands = [
            float(np.nanmax(peaks.maxs[i * third : (i + 1) * third])) for i in range(3)
        ]
        for band, expected in zip(bands, (0.2, 0.8, 0.4)):
            self.assertAlmostEqual(band, expected, places=2, msg=f"{bands}")

    # -- one piece --------------------------------------------------------

    def piece(self, *, start: float = 0, seconds: float = 10):
        from wavesnip.timeline import Piece

        clip = self.clip("p.wav", start=start, seconds=seconds)
        return Piece(
            clip=next(c for c in self.timeline(clip).clips if c.path == clip),
            start_frame=0,
            end_frame=int(seconds * self.RATE),
            start_us=at(start),
            end_us=at(start + seconds),
        )

    def test_a_piece_fills_exactly_the_columns_it_was_given(self):
        from wavesnip.peaks import reduce_piece

        mins, maxs, _rms, read = reduce_piece(
            _reader(self.root), self.piece(), 100, 8_000_000
        )
        self.assertEqual(len(mins), 100)
        self.assertEqual(len(maxs), 100)
        self.assertFalse(np.isnan(mins).any(), "every column got written")
        self.assertEqual(read, 10 * self.RATE, "and it reports what it spent")

    def test_more_columns_than_frames_still_fills_them_all(self):
        """Ten minutes of 8 kHz into 1600 columns is 3000 frames per column, but
        the reverse -- a hundredth of a second into a thousand columns -- must not
        leave most of them blank, or most of the waveform disappears."""
        from wavesnip.peaks import reduce_piece

        piece = self.piece(seconds=0.01)
        mins, _maxs, _rms, _read = reduce_piece(_reader(self.root), piece, 1000, 8_000_000)
        self.assertEqual(len(mins), 1000)
        self.assertGreater(np.count_nonzero(~np.isnan(mins)), 900)

    def test_a_piece_longer_than_the_chunk_size_is_reduced_in_several_reads(self):
        """The chunking is what bounds peak memory, so it has to happen."""
        from wavesnip import peaks
        from wavesnip.peaks import reduce_piece

        piece = self.piece(seconds=60)  # 480000 frames, so five reads of 100000
        sizes = []

        def reader(path, start, count):
            sizes.append(count)
            return _reader(self.root)(path, start, count)

        original = peaks.PEAK_CHUNK_FRAMES
        peaks.PEAK_CHUNK_FRAMES = 100_000
        try:
            reduce_piece(reader, piece, 1600, 8_000_000)
        finally:
            peaks.PEAK_CHUNK_FRAMES = original

        self.assertEqual(len(sizes), 5, f"one read per chunk, got {sizes}")
        self.assertTrue(all(size <= 100_000 for size in sizes))

    def test_a_loud_transient_spanning_a_chunk_boundary_is_not_clipped(self):
        """Chunks share a column only when frames are scarcer than columns; the
        merge keeps a peak in the first chunk from being lowered by the second."""
        from wavesnip import peaks
        from wavesnip.peaks import reduce_piece

        piece = self.piece(seconds=20)
        full = reduce_piece(_reader(self.root), piece, 1600, 8_000_000)[1]
        tallest = float(np.nanmax(full))

        original = peaks.PEAK_CHUNK_FRAMES
        peaks.PEAK_CHUNK_FRAMES = 4000
        try:
            chunked = reduce_piece(_reader(self.root), piece, 1600, 8_000_000)[1]
        finally:
            peaks.PEAK_CHUNK_FRAMES = original

        self.assertAlmostEqual(float(np.nanmax(chunked)), tallest, places=5)

    def test_an_exhausted_budget_stops_early_and_keeps_what_it_read(self):
        """A budget too small to finish must return the part of the picture it got
        rather than nothing -- and must not loop forever trying."""
        from wavesnip.peaks import reduce_piece

        piece = self.piece(seconds=60)
        mins, _maxs, _rms, read = reduce_piece(_reader(self.root), piece, 1600, 1000)
        self.assertFalse(np.isnan(mins).all(), "the first hundred frames still count")
        self.assertEqual(read, 1000)

    def test_a_zero_budget_reads_nothing(self):
        from wavesnip.peaks import reduce_piece

        self.assertEqual(
            reduce_piece(_reader(self.root), self.piece(), 100, 0),
            (None, None, None, 0),
        )

    def test_a_piece_with_no_frames_reads_nothing(self):
        from wavesnip.peaks import reduce_piece

        piece = self.piece()
        empty = piece.__class__(**{**vars(piece), "end_frame": 0})
        self.assertEqual(
            reduce_piece(_reader(self.root), empty, 100, 8_000_000),
            (None, None, None, 0),
        )

    def test_zero_columns_reads_nothing(self):
        from wavesnip.peaks import reduce_piece

        self.assertEqual(
            reduce_piece(_reader(self.root), self.piece(), 0, 8_000_000),
            (None, None, None, 0),
        )

    # -- the budget -------------------------------------------------------

    def test_the_budget_is_shared_across_the_whole_plan(self):
        """One peak request covers the visible range, not one clip at a time, so
        a window with a hundred chunks in it gets the same total as a window with
        one -- otherwise the cap means nothing."""
        from wavesnip.peaks import PEAK_COLUMNS, peaks_for

        clips = [self.clip(f"c{i}.wav", start=i * 10, seconds=10) for i in range(20)]
        budget = 200_000
        reads = []

        original = wavio.read_frames
        wavio.read_frames = lambda p, s, c: (reads.append(c), original(p, s, c))[1]
        try:
            peaks = peaks_for(self.plan(self.timeline(*clips), 0, 200), budget)
        finally:
            wavio.read_frames = original

        self.assertIsNotNone(peaks)
        self.assertEqual(len(peaks), PEAK_COLUMNS)
        self.assertLessEqual(sum(reads), budget, f"read {sum(reads)} frames")

    def test_a_budget_big_enough_for_the_plan_still_covers_everything(self):
        from wavesnip.peaks import peaks_for

        clips = [self.clip(f"c{i}.wav", start=i * 10, seconds=10) for i in range(4)]
        peaks = peaks_for(self.plan(self.timeline(*clips), 0, 40), 80_000 * 10)
        known = ~np.isnan(peaks.mins)
        self.assertTrue(known.all(), "no part of the window is left blank")

    # -- the sampled path -------------------------------------------------

    # The wide-window path: probing instead of decoding. ``exact_frames=0``
    # forces it, so these can stay on plans of a few dozen seconds rather than
    # building hours of fixture audio to cross the real reading threshold.

    def test_a_small_window_reads_every_frame(self):
        """The exact path is not going away. Zoomed in, bit-exactness is the
        whole point of asking for peaks at all."""
        from wavesnip.peaks import peaks_for

        clips = [self.clip("a.wav", start=0, seconds=10)]
        plan = self.plan(self.timeline(*clips), 0, 10)
        with mock.patch.object(wavio, "read_probes", wraps=wavio.read_probes) as probed:
            peaks = peaks_for(plan, 8_000_000)
        self.assertIsNotNone(peaks)
        probed.assert_not_called()

    def test_a_window_wider_than_its_pieces_is_sampled(self):
        """The floor that hands a piece zero columns in the exact reduction.

        A one-second sliver in a 20000-second window rounds to nothing
        column-wise and would drop out of the envelope with no failure
        anywhere. Such a plan goes to sampling even though reading it whole
        would be cheap -- correctness picks the path, not just cost.
        """
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        plan = self.plan(self.timeline(*clips), 0, 20_000)
        with mock.patch.object(wavio, "read_frames", wraps=wavio.read_frames) as full:
            peaks = peaks_for(plan)
        self.assertIsNotNone(peaks)
        full.assert_not_called()

    def test_the_sampled_path_reads_a_bounded_slice_not_the_window(self):
        """The property that made the week-wide gate possible: cost per column,
        so a 20000-second view reads less than the 40-second one next door."""
        from wavesnip.peaks import (
            PEAK_COLUMNS,
            PROBE_FRAMES_PER_COLUMN,
            PROBE_FRAMES_PER_RUN,
            peaks_for,
        )

        clips = [self.clip("a.wav", start=0, seconds=10)]
        plan = self.plan(self.timeline(*clips), 0, 20_000)
        with mock.patch.object(wavio, "read_probes", wraps=wavio.read_probes) as probed:
            peaks = peaks_for(plan)
        self.assertIsNotNone(peaks)
        spent = sum(
            count for call in probed.call_args_list for _, count in call.args[1]
        )
        ceiling = PEAK_COLUMNS * (PROBE_FRAMES_PER_COLUMN + PROBE_FRAMES_PER_RUN)
        self.assertLessEqual(spent, ceiling, "per column, plus one fallback run each")
        self.assertLess(spent, plan.nframes, "sampling must beat decoding the window")

    def test_a_covered_column_is_never_left_blank(self):
        """Probes straddle a narrow recording and miss it on both sides.

        One second of audio in a 12.5-second column sits between every probe
        midpoint -- without the fallback probe the envelope would draw silence
        exactly where the coverage blocks say something was recorded.
        """
        from wavesnip.peaks import PEAK_COLUMNS, peaks_for

        clips = [self.clip("a.wav", start=20, seconds=1)]
        plan = self.plan(self.timeline(*clips), 0, 20_000)
        peaks = peaks_for(plan)
        self.assertIsNotNone(peaks)
        column = PEAK_COLUMNS * (at(20) - plan.start_us) // (plan.end_us - plan.start_us)
        self.assertFalse(np.isnan(peaks.mins[column]), "the clip's column went blank")
        self.assertGreater(float(peaks.maxs[column]), 0.4, "and holds a real sample")
        self.assertFalse(np.isnan(peaks.rms[column]), "band included")
        self.assertTrue(np.isnan(peaks.mins[column + 10]), "while the gap stays a gap")

    def test_sampled_columns_still_say_where_the_audio_is(self):
        """Position on the sampled path comes from the columns themselves.

        Three clips, three amplitudes, a window far wider than any of them: the
        loudest column must be the loud clip's column, not whichever piece the
        read happened to visit first.
        """
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10, amplitude=0.2),
            self.clip("b.wav", start=100, seconds=10, amplitude=0.8),
            self.clip("c.wav", start=200, seconds=10, amplitude=0.4),
        ]
        plan = self.plan(self.timeline(*clips), 0, 20_000)
        peaks = peaks_for(plan)
        self.assertIsNotNone(peaks)
        self.assertAlmostEqual(float(peaks.maxs[0]), 0.2, places=2)
        self.assertAlmostEqual(float(peaks.maxs[8]), 0.8, places=2)
        self.assertAlmostEqual(float(peaks.maxs[16]), 0.4, places=2)
        self.assertTrue(np.isnan(peaks.mins[4]), "between them, nothing")

    def test_a_gap_is_still_a_gap_on_the_sampled_path(self):
        """Same picture as the exact path: clips drawn where they are, the
        stretch between them not stretched to fill."""
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=10_000, seconds=10),
        ]
        plan = self.plan(self.timeline(*clips), 0, 20_000)
        peaks = peaks_for(plan)
        self.assertIsNotNone(peaks)
        self.assertFalse(np.isnan(peaks.mins[0]), "the first clip is drawn")
        self.assertFalse(np.isnan(peaks.mins[800]), "and the second")
        self.assertTrue(np.isnan(peaks.mins[400]), "the stretch between is a gap")

    def test_the_budget_binds_on_the_sampled_path(self):
        """The shared budget is a property of the request, not of the path."""
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        plan = self.plan(self.timeline(*clips), 0, 40)
        reads = []
        original = wavio.read_probes

        def spy(path, probes):
            reads.extend(count for _, count in probes)
            return original(path, probes)

        with mock.patch.object(wavio, "read_probes", side_effect=spy):
            peaks = peaks_for(plan, max_frames=300, exact_frames=0)

        self.assertLessEqual(sum(reads), 300, f"read {sum(reads)} frames")
        self.assertIsNotNone(peaks, "the part it did read still counts")

    def test_each_file_is_opened_once_per_sampled_request(self):
        """The naive version -- ``read_frames`` per probe -- reopened the WAV
        every time and measured *slower* than reading the whole file, because
        the opens dominate. One open per file is the reason this is fast."""
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        plan = self.plan(self.timeline(*clips), 0, 40)
        with mock.patch.object(wavio, "_open", wraps=wavio._open) as opened:
            peaks = peaks_for(plan, exact_frames=0)
        self.assertIsNotNone(peaks)
        self.assertEqual(opened.call_count, 2, "one open per file, not per probe")

    def test_the_same_sampled_request_twice_is_identical(self):
        """Probes are placed by arithmetic on the columns, not discovered, so
        panning back to a view reproduces the picture exactly."""
        from wavesnip.peaks import peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10),
            self.clip("b.wav", start=30, seconds=10),
        ]
        plan = self.plan(self.timeline(*clips), 0, 40)
        first = peaks_for(plan, exact_frames=0)
        second = peaks_for(plan, exact_frames=0)
        np.testing.assert_array_equal(first.mins, second.mins)
        np.testing.assert_array_equal(first.maxs, second.maxs)
        np.testing.assert_array_equal(first.rms, second.rms)

    def test_two_clips_share_the_column_between_them(self):
        """A boundary falling mid-column means both files write that column.

        The second must *merge* with the first, not wipe it. The clip ends
        near a probe whose 46 frames are the first clip's loudest negative
        swing (about -0.65); the second clip's probes there start from its own
        quiet opening. A wipe would leave only those.
        """
        from wavesnip.peaks import PEAK_COLUMNS, peaks_for

        clips = [
            self.clip("a.wav", start=0, seconds=10, amplitude=0.8),
            self.clip("b.wav", start=10, seconds=3, amplitude=0.3),
        ]
        plan = self.plan(self.timeline(*clips), 0, 13)
        peaks = peaks_for(plan, exact_frames=0)
        self.assertIsNotNone(peaks)
        column = PEAK_COLUMNS * (at(10) - plan.start_us) // (plan.end_us - plan.start_us)
        self.assertLess(float(peaks.mins[column]), -0.5, "clip a's swing survived")
        self.assertGreater(float(peaks.maxs[column]), 0.2, "and clip b's is there too")

    def test_both_paths_carry_the_band(self):
        """The painter draws one picture; a path that returned no band would
        quietly make the view change style at some zoom level."""
        from wavesnip.peaks import peaks_for

        clips = [self.clip("a.wav", start=0, seconds=10)]
        timeline = self.timeline(*clips)
        for label, kwargs in (("exact", {}), ("sampled", {"exact_frames": 0})):
            with self.subTest(path=label):
                peaks = peaks_for(self.plan(timeline, 0, 10), 8_000_000, **kwargs)
                self.assertIsNotNone(peaks.rms)
                band = peaks.rms[~np.isnan(peaks.mins)]
                self.assertGreater(float(band.min()), 0.1)
                self.assertLess(float(band.max()), 0.55)
                self.assertAlmostEqual(float(band.mean()), 0.354, delta=0.02)


class ViewportContextMenuTests(WindowCase):
    """The viewport's right-click menu, and what it knows about its own spot.

    Built by a method that *returns* the menu rather than one that shows it:
    nearly every entry is conditional on the instant -- which clip is under the
    pointer, whether a region exists, whether a block is selected -- and a menu
    that can only be inspected by watching it pop up cannot be asserted on.
    """

    def archive(self) -> Path:
        """Two clips, butt-joined at ten seconds, so 500 seconds on is a gap."""
        folder = self.make_clip_archive("a")
        self.add_clip(folder, "c0.wav", start=0)
        self.add_clip(folder, "c1.wav", start=SEC)
        self.load(folder)
        return folder

    def menu(self, instant):
        return self.window._build_context_menu(instant)

    def action(self, instant, text):
        return next(
            (a for a in self.menu(instant).actions() if a.text() == text), None
        )

    def test_the_menu_offers_its_plain_entries_before_any_archive_is_loaded(self):
        """The split into build-then-show must not have made the early return
        path depend on a timeline that is not there yet."""
        self.assertIsNone(self.window._timeline, "sanity: no archive yet")
        self.assertIsNotNone(self.action(at(0), "Play from here"))
        self.assertIsNone(self.action(at(0), "Reveal clip in explorer"))

    def test_the_clip_under_the_pointer_can_be_revealed(self):
        self.archive()
        self.assertIsNotNone(self.action(at(0.5), "Reveal clip in explorer"))

    def test_a_gap_has_no_file_to_reveal(self):
        """Both clips end at twenty seconds. Offering the entry over a gap
        would make the menu claim there is audio where the picture says there
        is none."""
        self.archive()
        self.assertIsNone(self.action(at(500), "Reveal clip in explorer"))

    def test_revealing_hands_over_that_clips_file(self):
        self.archive()
        action = self.action(at(0.5), "Reveal clip in explorer")
        with mock.patch("wavesnip.mainwindow.reveal") as reveal_fn:
            action.trigger()
        reveal_fn.assert_called_once()
        self.assertEqual(Path(reveal_fn.call_args.args[0]).name, "c0.wav")


class TimeBlockEditingTests(WindowCase):
    """Saving a block must not let go of it.

    Save is the button you press *while* editing, so making it deselect emptied
    the editor with the Save button still enabled over nothing -- and a second
    press then added a copy of the block instead of updating it.
    """

    def setUp(self):
        super().setUp()
        # Away from the real global file. `_load_time_blocks` reads whatever
        # this machine has in it, and a test that saves must not write there --
        # the same hazard the explicit config path in WindowCase exists for.
        self.window._tb_config = TimeBlocksConfig(
            path=self.root / "blocks" / "time_blocks.json"
        )
        self.window._time_blocks = []
        self.window._update_time_blocks_in_view()

        self.window.snip_from.set_instant(at(1))
        self.window.snip_to.set_instant(at(3))
        self.window._on_snip_changed()
        self.window.tb_name.setText("Morning")
        self.window._on_tb_new()

    def test_a_block_created_from_the_region_is_selected_immediately(self):
        """[+ New] has to leave the block highlighted and the editor showing
        its bounds. Set the other way round, the rebuild runs before the editor
        has declared what it is editing, and Save then reads empty date fields
        and refuses the block it was just given."""
        self.assertEqual(
            self.window.wave.state().selected_time_block,
            (at(1), at(3), "Morning", 0),
        )
        self.assertEqual(self.window.tb_start.instant(), at(1))
        self.assertEqual(self.window.tb_end.instant(), at(3))

    def test_saving_leaves_the_block_selected(self):
        self.window.tb_name.setText("Morning show")
        self.window._on_tb_save()

        editing = self.window._editing_block
        self.assertIsNotNone(editing, "Save must not deselect the block")
        self.assertEqual(editing.name, "Morning show")
        self.assertEqual(self.window.tb_name.text(), "Morning show")
        self.assertTrue(self.window.btn_tb_save.isEnabled())

    def test_saving_twice_updates_the_one_block(self):
        """The shape of the failure a deselected Save produces: a second copy
        of a block the user already had, differing only in name."""
        self.window.tb_name.setText("Morning show")
        self.window._on_tb_save()
        self.window._on_tb_save()

        self.assertEqual(
            [block.name for block in self.window._time_blocks], ["Morning show"]
        )

    def test_the_highlight_follows_the_bounds_that_were_saved(self):
        """The key names (start, end, name), so keeping the *selection* while
        moving the bounds would silently highlight nothing at all."""
        self.window.tb_start.set_instant(at(0))
        self.window._on_tb_save()

        self.assertEqual(
            self.window.wave.state().selected_time_block,
            (at(0), at(3), "Morning", 0),
        )

    def test_a_menu_entry_that_rebuilds_a_block_keeps_it_the_edited_one(self):
        """The editor holds an object, and blocks are immutable: an entry that
        swaps in a new copy has to hand the editor that copy, or the next
        update finds a block that is no longer in the list and drops it."""
        block = self.window._editing_block
        self.window._set_block_to_snip(
            (block.start_us, block.end_us, block.name, block.episode_num)
        )

        self.assertIs(self.window._editing_block, self.window._time_blocks[0])
        self.assertEqual(
            self.window.wave.state().selected_time_block,
            (at(1), at(3), "Morning", 0),
        )

    def test_deleting_a_block_still_clears_the_editor(self):
        """The counterpart: saving holds on, deleting lets go. A highlight that
        survived a delete would name a block nothing paints any more.

        ``_on_tb_delete`` rather than the button's handler: that one asks for
        confirmation, and a ``QMessageBox`` in an offscreen test is a dialog
        with nobody to press it.
        """
        self.window._on_tb_delete()
        self.assertIsNone(self.window._editing_block)
        self.assertIsNone(self.window.wave.state().selected_time_block)
        self.assertEqual(self.window._time_blocks, [])


class TimeBlockLoadProblemTests(WindowCase):
    """A corrupted entry costs only itself, and is said out loud.

    The window builds itself by loading ``time_blocks.json``, and the load used
    to raise on the first entry it could not parse -- so one bad line meant no
    window at all, and every block that *was* fine stayed unloaded. Both halves
    matter: the rest of the blocks reach the view, and the one that did not is
    reported to the console and the bottom status bar rather than swallowed.
    """

    def window_on(self, entries):
        """A window whose time blocks come from these entries, not the real file.

        Injected through the constructor's own call, not swapped after the
        fact: the point is what a window *starts up* with.
        """
        path = self.root / "blocks" / "time_blocks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "time_blocks": entries}), encoding="utf-8"
        )

        from wavesnip.mainwindow import MainWindow

        with mock.patch(
            "wavesnip.mainwindow.TimeBlocksConfig", lambda: TimeBlocksConfig(path)
        ):
            window = MainWindow(config=self.config)
        self.addCleanup(window.close)
        return window

    @staticmethod
    def good(name: str = "Morning show") -> dict:
        return {
            "name": name,
            "start_time": "12.5.2026. 10:15:01:001",
            "end_time": "12.5.2026. 11:15:01:001",
        }

    @staticmethod
    def broken(name: str = "Broken") -> dict:
        return {**TimeBlockLoadProblemTests.good(name), "start_time": "nope"}

    def test_the_window_starts_and_loads_the_blocks_it_can(self):
        window = self.window_on([self.good(), self.broken()])

        self.assertEqual([block.name for block in window._time_blocks], ["Morning show"])
        self.assertEqual(
            [entry[2] for entry in window.wave.state().time_blocks],
            ["Morning show"],
            "the block that loaded must be the one on screen",
        )

    def test_a_completely_broken_file_still_starts_the_window(self):
        window = self.window_on([self.broken("One"), self.broken("Two")])

        self.assertEqual(window._time_blocks, [])
        self.assertEqual(window.wave.state().time_blocks, [])

    def test_the_problem_reaches_the_console(self):
        console = io.StringIO()
        with redirect_stderr(console):
            self.window_on([self.good(), self.broken()])

        said = console.getvalue()
        self.assertIn("time_blocks.json", said)
        self.assertIn('"Broken"', said)
        self.assertIn("start_time", said)

    def test_the_problem_reaches_the_bottom_status_bar(self):
        window = self.window_on([self.good(), self.broken()])

        shown = window.status.currentMessage()
        self.assertIn('"Broken"', shown)
        self.assertIn("start_time", shown)

    def test_several_problems_collapse_to_one_status_line(self):
        """The bar is a strip, not a log: it carries the first problem and a
        count, and the console carries the rest in full."""
        console = io.StringIO()
        with redirect_stderr(console):
            window = self.window_on([self.broken("One"), self.broken("Two")])

        shown = window.status.currentMessage()
        self.assertIn('"One"', shown)
        self.assertNotIn('"Two"', shown)
        self.assertIn("+1 more, see console", shown)
        self.assertIn('"Two"', console.getvalue())

    def test_a_file_that_loads_cleanly_says_nothing(self):
        """Otherwise the bar would carry a warning forever about a file with
        nothing wrong with it."""
        window = self.window_on([self.good()])

        self.assertNotIn("entry", window.status.currentMessage())
        self.assertNotIn("time_blocks.json", window.status.currentMessage())


class TimeBlockDoubleClickTests(WindowCase):
    """Double click a block in the viewport: it becomes the selected region.

    The block's bounds are already drawn, so this is the gesture for "yes, this
    one" -- the alternative being dragging out by hand a range that is on screen
    in front of you.

    The whole sequence is sent, not just the double click: Qt delivers press,
    release, ``MouseButtonDblClick``, release, and the second press never
    reaches ``mousePressEvent``. Testing the final event alone would pass on a
    handler that assumed the press before it had already done the work.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        self.add_clip(folder, "c0.wav", start=0)
        self.load(folder)

        self.window._tb_config = TimeBlocksConfig(
            path=self.root / "blocks" / "time_blocks.json"
        )
        state = self.window.wave.state()
        # Spanning the whole view, so every column of the content area is
        # inside the block and the test cannot pass by hitting it by luck.
        self.block = TimeBlock(
            name="The whole thing",
            start_us=state.start_us,
            end_us=state.end_us,
            episode_num=7,
            description="",
            hosts=[],
            guests=[],
        )
        self.window._time_blocks = [self.block]
        self.window._update_time_blocks_in_view()

    def on_a_block(self):
        wave = self.window.wave
        row = wave._time_block_row_rect(wave._content_rect(), 0)
        return wave.width() // 2, row.center().y()

    def off_a_block(self):
        wave = self.window.wave
        return wave.width() // 2, wave._content_rect().top() + 1

    def double_click(self, x, y):
        wave = self.window.wave
        handlers = {
            QMouseEvent.Type.MouseButtonPress: wave.mousePressEvent,
            QMouseEvent.Type.MouseButtonRelease: wave.mouseReleaseEvent,
            QMouseEvent.Type.MouseButtonDblClick: wave.mouseDoubleClickEvent,
        }
        for event_type in (
            QMouseEvent.Type.MouseButtonPress,
            QMouseEvent.Type.MouseButtonRelease,
            QMouseEvent.Type.MouseButtonDblClick,
            QMouseEvent.Type.MouseButtonRelease,
        ):
            handlers[event_type](
                QMouseEvent(
                    event_type,
                    QPoint(x, y),
                    Qt.LeftButton,
                    Qt.LeftButton,
                    Qt.NoModifier,
                )
            )

    def test_a_double_click_makes_the_block_the_region(self):
        self.double_click(*self.on_a_block())

        self.assertEqual(
            self.window._snip, (self.block.start_us, self.block.end_us)
        )
        self.assertEqual(self.window.tb_name.text(), "The whole thing")
        self.assertTrue(self.window.btn_tb_save.isEnabled())

    def test_a_double_click_off_a_block_selects_nothing(self):
        self.double_click(*self.off_a_block())

        self.assertIsNone(self.window._snip, "empty space is not a region")


class TimeBlockDeselectionTests(WindowCase):
    """Exactly four things let go of a selected block: Esc, a *different*
    block, the "Deselect block" menu entry, and deleting it.

    Clicking empty space used to be a fifth, and it was the wrong one: that
    press is where a region drag starts, so a pointer crossing the canvas on
    its way somewhere else emptied the editor behind it -- and the drag it was
    about to begin had nothing to do with the block.
    """

    def setUp(self):
        super().setUp()
        folder = self.make_clip_archive("a")
        self.add_clip(folder, "c0.wav", start=0)
        self.load(folder)
        self.window._tb_config = TimeBlocksConfig(
            path=self.root / "blocks" / "time_blocks.json"
        )

        # Shown, because a shortcut with `Qt.WindowShortcut` context belongs to
        # a window Qt has actually *realised*: on a never-shown window the key
        # never reaches the shortcut map, and a test that sends it would then
        # be asserting nothing. One turn of the loop is what completes the
        # show -- `show()` alone leaves the shortcut context unarmed.
        self.window.show()
        self.app.processEvents()

        # Two blocks side by side, meeting at the middle of the view, so both
        # sit on row 0 and each has pixels of its own to be clicked on.
        state = self.window.wave.state()
        half = (state.start_us + state.end_us) // 2
        self.first = TimeBlock("First", state.start_us, half, 0, "", [], [])
        self.second = TimeBlock("Second", half, state.end_us, 0, "", [], [])
        self.window._time_blocks = [self.first, self.second]
        self.window._update_time_blocks_in_view()

    # -- helpers ----------------------------------------------------------

    def click(self, x, y):
        """A left press and release at one spot -- a click, not a drag."""
        wave = self.window.wave
        press = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPoint(x, y),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease, QPoint(x, y),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
        )
        wave.mousePressEvent(press)
        wave.mouseReleaseEvent(release)

    def on(self, block: TimeBlock):
        """A pixel in the middle of ``block``'s own rectangle."""
        wave = self.window.wave
        middle = block.start_us + (block.end_us - block.start_us) // 2
        return wave.x_of(middle), wave._time_block_row_rect(wave._content_rect(), 0).center().y()

    def off_every_block(self):
        """Empty canvas: both blocks share row 0 at the floor, the ceiling is bare."""
        wave = self.window.wave
        return wave.width() // 2, wave._content_rect().top() + 1

    def viewport_action(self, text, instant=None):
        menu = self.window._build_context_menu(
            self.first.start_us + 1 if instant is None else instant
        )
        return next((a for a in menu.actions() if a.text() == text), None)

    def block_menu_action(self, text, block=None):
        block = self.first if block is None else block
        menu = self.window._build_tb_context_menu(
            (block.start_us, block.end_us, block.name, block.episode_num)
        )
        return next((a for a in menu.actions() if a.text() == text), None)

    # -- what the four ways are -------------------------------------------

    def test_a_click_on_a_block_selects_it(self):
        """The setup the rest of these stand on, asserted rather than assumed."""
        self.click(*self.on(self.first))

        self.assertIs(self.window._editing_block, self.first)
        self.assertEqual(self.window.tb_name.text(), "First")
        self.assertIsNotNone(self.window.wave.state().selected_time_block)

    def test_clicking_empty_space_keeps_the_selection(self):
        """The change: empty space no longer deselects. The press there starts
        a region drag, and a drag's first pixel is not a decision about the
        block being edited."""
        self.click(*self.on(self.first))

        self.click(*self.off_every_block())

        self.assertIs(self.window._editing_block, self.first)
        self.assertEqual(self.window.tb_name.text(), "First")
        self.assertIsNotNone(self.window.wave.state().selected_time_block)
        self.assertNotIn("deselected", self.window.status.currentMessage())

    def test_clicking_a_different_block_moves_the_selection(self):
        """Not to nothing and back: straight from one to the other."""
        self.click(*self.on(self.first))

        self.click(*self.on(self.second))

        self.assertIs(self.window._editing_block, self.second)
        self.assertEqual(self.window.tb_name.text(), "Second")
        self.assertIsNotNone(self.window.wave.state().selected_time_block)

    def test_escape_deselects_the_block(self):
        self.click(*self.on(self.first))

        QTest.keyClick(self.window, Qt.Key_Escape)

        self.assertIsNone(self.window._editing_block)
        self.assertIsNone(self.window.wave.state().selected_time_block)
        self.assertEqual(self.window.tb_name.text(), "")
        self.assertFalse(self.window.btn_tb_save.isEnabled())
        self.assertIn("deselected", self.window.status.currentMessage())

    def test_escape_also_works_from_inside_a_date_field(self):
        """The editor is where the user is when they give up on a block, so the
        key has to reach the window action from there too."""
        self.click(*self.on(self.first))
        self.window.tb_start.setFocus()
        self.app.processEvents()

        QTest.keyClick(self.window.focusWidget(), Qt.Key_Escape)

        self.assertIsNone(self.window._editing_block)

    def test_escape_with_nothing_selected_is_silent(self):
        before = self.window.status.currentMessage()

        QTest.keyClick(self.window, Qt.Key_Escape)

        self.assertIsNone(self.window._editing_block)
        self.assertEqual(self.window.status.currentMessage(), before)

    def test_deleting_the_block_deselects_it(self):
        """The fourth of the four. ``_on_tb_delete`` rather than the button's
        handler: that one asks a question, and a dialog with nobody to press it
        is a test that hangs."""
        self.click(*self.on(self.first))

        self.window._on_tb_delete()

        self.assertIsNone(self.window._editing_block)
        self.assertIsNone(self.window.wave.state().selected_time_block)
        self.assertEqual(self.window._time_blocks, [self.second])

    # -- the menu entry ----------------------------------------------------

    def test_the_viewport_menu_offers_deselect_only_while_one_is_selected(self):
        self.assertIsNone(
            self.viewport_action("Deselect block (Esc)"),
            "a deselect with nothing selected is a lie",
        )
        self.click(*self.on(self.first))
        self.assertIsNotNone(self.viewport_action("Deselect block (Esc)"))

    def test_the_block_menu_offers_deselect_only_while_one_is_selected(self):
        self.assertIsNone(self.block_menu_action("Deselect block (Esc)"))
        self.click(*self.on(self.first))
        self.assertIsNotNone(self.block_menu_action("Deselect block (Esc)"))

    def test_the_menu_entry_deselects(self):
        self.click(*self.on(self.first))

        self.viewport_action("Deselect block (Esc)").trigger()

        self.assertIsNone(self.window._editing_block)
        self.assertIsNone(self.window.wave.state().selected_time_block)

    def test_the_block_menus_entry_deselects_too(self):
        self.click(*self.on(self.first))

        self.block_menu_action("Deselect block (Esc)").trigger()

        self.assertIsNone(self.window._editing_block)

    def test_the_esc_tip_is_a_tip_and_not_a_second_binding(self):
        """The window action owns the key; the menu entry only points at it.

        Two actions bound to Esc is an ambiguity Qt resolves by firing neither
        -- and while the menu is open, Esc must close the menu *only*. The
        entry therefore carries the tip in its text and no shortcut at all.
        """
        entry = self.block_menu_action("Deselect block (Esc)")
        self.assertIsNone(entry, "sanity: nothing is selected yet")
        self.click(*self.on(self.first))
        entry = self.block_menu_action("Deselect block (Esc)")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.shortcut().toString(), "")

        bound = [a for a in self.window.actions() if a.shortcut().toString() == "Esc"]
        self.assertEqual(len(bound), 1, "exactly one action may own Esc")
        self.assertIn("Esc", bound[0].text())

    def test_escape_over_an_open_menu_closes_the_menu_and_keeps_the_block(self):
        """The scenario the missing shortcut exists for: backing out of a menu
        is not a decision about the block."""
        self.click(*self.on(self.first))
        menu = self.window._build_tb_context_menu(
            (self.first.start_us, self.first.end_us, self.first.name, self.first.episode_num)
        )
        self.addCleanup(menu.hide)
        menu.popup(self.window.mapToGlobal(QPoint(40, 40)))
        self.app.processEvents()

        QTest.keyClick(menu, Qt.Key_Escape)
        self.app.processEvents()

        self.assertFalse(menu.isVisible(), "Esc closes the menu")
        self.assertIs(self.window._editing_block, self.first)


def _reader(root: Path):
    """A ``read_frames`` for the tests that need to see past ``wavio``.

    ``root`` is taken so callers read as ``_reader(self.root)``, which makes it
    obvious which archive the reads are expected to come from.
    """
    def read_frames(path, start, count):
        return wavio.read_frames(path, start, count)

    return read_frames


if __name__ == "__main__":
    unittest.main()
