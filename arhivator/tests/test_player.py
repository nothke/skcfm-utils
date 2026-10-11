"""Tests for wavesnip.cursor and wavesnip.player.

The audio decisions -- which clip to load, where to seek, what happens at a gap
-- live in :mod:`wavesnip.cursor`, which is Qt-free and gets tested hard here with
no sound card involved. That split is deliberate: these tests run on any
interpreter, including one without PySide6 installed.

``QMediaPlayer`` needs a QApplication and audio hardware, so the wrapper is
checked only for the things that are cheap and deterministic: that a missing
timeline is refused loudly rather than crashing on ``None``, that signals carry
microsecond values without overflowing, and that a rendered preview exists and is
cleaned up.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from wavesnip import wavio
from wavesnip.cursor import Cursor
from wavesnip.indexer import ArchiveIndex
from wavesnip.model import Archive, to_us
from wavesnip.timeline import Timeline

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - PySide6 is in requirements.txt
    QApplication = None

RATE = 8
SEC = 10
BASE = datetime(2026, 5, 12, 10, 15, 0)


def at(seconds: float) -> int:
    return to_us(BASE + timedelta(seconds=seconds))


class CursorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.archives: list[Archive] = []

    def make_archive(self, name: str, priority: int = 0) -> Archive:
        folder = self.root / name
        folder.mkdir()
        archive = Archive(path=folder, priority=priority)
        ArchiveIndex(archive).scan()
        self.archives.append(archive)
        return archive

    def add_clip(
        self,
        archive: Archive,
        name: str,
        *,
        start: float,
        seconds: float = SEC,
        framerate: int = RATE,
        nchannels: int = 1,
    ) -> Path:
        t = np.arange(int(seconds * framerate), dtype=np.float32) / framerate
        samples = (0.5 * np.sin(2 * np.pi * t, dtype=np.float32)).reshape(-1, 1)
        path = archive.path / name
        wavio.write_wav(path, samples, framerate=framerate)
        stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def timeline(self) -> Timeline:
        for archive in self.archives:
            ArchiveIndex(archive).scan()
        return Timeline(self.archives).refresh()

    def cursor(self, instant_us: int | None = None):
        return Cursor(self.timeline(), at(0) if instant_us is None else instant_us)


class SourceTests(CursorCase):
    def test_source_points_at_the_covering_clip(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        source = self.cursor().source_at(at(2))
        self.assertEqual(source.path.name, "c1.wav")
        self.assertEqual(source.offset_us, 2_000_000)
        self.assertEqual(source.start_us, at(2))

    def test_source_offset_in_milliseconds(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        source = self.cursor().source_at(at(2))
        self.assertEqual(source.offset_ms, 2000)

    def test_offset_is_truncated_to_milliseconds(self):
        """Qt has no finer unit, so sub-millisecond precision is dropped."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        source = self.cursor().source_at(at(2) + 999)  # 999 us past 2 s
        self.assertEqual(source.offset_ms, 2000)

    def test_source_end_is_the_clip_end(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.assertEqual(self.cursor().source_at(at(2)).end_us, at(SEC))

    def test_gap_has_no_source(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)
        self.assertIsNone(self.cursor().source_at(at(SEC * 2)))

    def test_start_of_clip_offsets_to_zero(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=SEC)
        self.assertEqual(self.cursor().source_at(at(SEC)).offset_us, 0)

    def test_empty_archive_has_no_source(self):
        self.make_archive("empty")
        self.assertIsNone(self.cursor().source_at(at(0)))

    def test_priority_archive_is_used(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0)
        self.assertEqual(self.cursor().source_at(at(1)).path.parent, first.path)

    def test_lower_priority_archive_covers_a_gap(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0, seconds=2)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0, seconds=10)
        self.assertEqual(self.cursor().source_at(at(5)).path.parent, second.path)


class NearestSourceTests(CursorCase):
    def setUp(self):
        super().setUp()
        self.a = self.make_archive("a")
        self.add_clip(self.a, "c1.wav", start=0)
        self.add_clip(self.a, "c2.wav", start=SEC * 3)

    def test_before_the_first_run_jumps_forward(self):
        source, jumped = self.cursor().nearest_source(at(-SEC))
        self.assertEqual(jumped, at(0))
        self.assertEqual(source.offset_us, 0)

    def test_after_the_last_run_jumps_back(self):
        """Nothing ahead, so the only way to have audio is backwards."""
        source, jumped = self.cursor().nearest_source(at(SEC * 9))
        self.assertEqual(jumped, at(SEC * 3 + SEC) - 1)
        self.assertEqual(source.path.name, "c2.wav")

    def test_covered_instant_is_not_a_jump(self):
        source, jumped = self.cursor().nearest_source(at(2))
        self.assertIsNotNone(source)
        self.assertIsNone(jumped)

    def test_gap_jumps_forward(self):
        source, jumped = self.cursor().nearest_source(at(SEC * 2 + 4))
        self.assertEqual(jumped, at(SEC * 3))
        self.assertEqual(source.path.name, "c2.wav")

    def test_gap_jumps_forward_not_back(self):
        """Past the end of c1.wav but well before c2.wav: still jumps forward."""
        source, jumped = self.cursor().nearest_source(at(SEC + 2))
        self.assertEqual(jumped, at(SEC * 3))
        self.assertEqual(source.path.name, "c2.wav")
        self.assertEqual(source.offset_us, 0)

    def test_no_audio_anywhere_returns_nothing(self):
        empty = self.make_archive("empty")
        self.archives = [empty]
        source, jumped = self.cursor().nearest_source(at(0))
        self.assertIsNone(source)
        self.assertIsNone(jumped, "must distinguish 'no audio' from 'jumped but empty'")

    def test_jump_target_is_always_covered(self):
        source, jumped = self.cursor().nearest_source(at(SEC * 2))
        self.assertIsNotNone(jumped)
        self.assertEqual(source.start_us, jumped)


class EndOfSourceTests(CursorCase):
    def test_advances_to_the_next_chunk(self):
        """The whole point: playback does not stop at a chunk boundary."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC)

        cursor = self.cursor()
        first = cursor.source_at(at(SEC - 1))
        second = cursor.end_of_source(first)
        self.assertEqual(second.path.name, "c2.wav")
        self.assertEqual(second.offset_us, 0)

    def test_never_returns_the_clip_that_just_finished(self):
        """end_us is exclusive, so the previous clip cannot come back."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        cursor = self.cursor()
        self.assertIsNone(cursor.end_of_source(cursor.source_at(at(SEC - 1))))

    def test_end_of_archive_returns_nothing(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        cursor = self.cursor()
        self.assertIsNone(cursor.end_of_source(cursor.source_at(at(SEC - 1))))

    def test_crosses_a_gap_by_reporting_no_source(self):
        """The caller decides whether to skip; the cursor just reports."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)
        cursor = self.cursor()
        self.assertIsNone(cursor.end_of_source(cursor.source_at(at(SEC - 1))))

    def test_advances_across_archives(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=SEC)

        cursor = self.cursor()
        following = cursor.end_of_source(cursor.source_at(at(SEC - 1)))
        self.assertEqual(following.path.parent, second.path)


class MoveTests(CursorCase):
    def test_move_forward_and_backward(self):
        cursor = self.cursor(at(0))
        self.assertEqual(cursor.move(5_000_000), at(5))
        self.assertEqual(cursor.move(-3_000_000), at(2))

    def test_move_updates_the_stored_instant(self):
        cursor = self.cursor(at(0))
        cursor.move(1_000_000)
        self.assertEqual(cursor.instant_us, at(1))

    def test_move_can_be_used_for_week_navigation(self):
        week = 7 * 24 * 3600 * 1_000_000
        cursor = self.cursor(at(0))
        self.assertEqual(cursor.move(week), at(7 * 24 * 3600))
        self.assertEqual(cursor.move(-week), at(0))


class PlayerWiringTests(unittest.TestCase):
    """The Qt wrapper: importable, refuses to guess, needs no hardware."""

    def setUp(self):
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])

    def _player(self):
        from wavesnip.player import Player

        return Player()

    def test_constructs(self):
        player = self._player()
        self.assertFalse(player.is_playing)

    def test_refuses_to_play_without_a_timeline(self):
        """The first-run mistake, refused loudly instead of on None."""
        from wavesnip.player import PlayerError

        player = self._player()
        with self.assertRaises(PlayerError):
            player.play_at(at(0))
        with self.assertRaises(PlayerError):
            player.seek(at(0))

    def test_starts_idle(self):
        player = self._player()
        self.assertFalse(player.is_playing)
        self.assertEqual(player.position_us, 0)

    def test_stop_is_safe_when_idle(self):
        player = self._player()
        player.stop()
        player.pause()
        self.assertFalse(player.is_playing)

    def test_context_manager_cleans_up(self):
        with self._player() as player:
            self.assertFalse(player.is_playing)

    def test_signals_carry_microsecond_timestamps_without_overflow(self):
        """Signal(int) is C++ 32-bit; a microsecond timestamp does not fit.

        Emitting one used to raise OverflowError out of shiboken on every tick.
        """
        import warnings

        player = self._player()
        received: list[int] = []
        player.positionChanged.connect(received.append)

        player.positionChanged.emit(at(0))  # ~1.78e15 microseconds
        self.assertEqual(received, [at(0)])
        self.assertTrue(received[0] > 2**31, "must genuinely exceed 32 bits")

    def test_jumped_signal_survives_large_values(self):
        player = self._player()
        received: list[tuple] = []
        player.jumped.connect(lambda a, b: received.append((a, b)))
        player.jumped.emit(at(0), at(60))
        self.assertEqual(received, [(at(0), at(60))])

    def test_signals_are_connectable(self):
        """Signals are only useful if they can actually be connected to."""
        from PySide6.QtCore import SignalInstance

        player = self._player()
        received: list[str] = []

        for name in ("positionChanged", "stateChanged", "jumped", "error", "finished"):
            signal = getattr(player, name)
            self.assertIsInstance(signal, SignalInstance, f"{name} is not a signal")
            signal.connect(lambda *args, name=name: received.append(name))

        player.stop()
        self.assertIn("stateChanged", received)


class ExportPreviewTests(CursorCase):
    """Snip preview renders rather than streaming, because Qt will not resample."""

    def setUp(self):
        super().setUp()
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])
        self.a = self.make_archive("a")
        self.add_clip(self.a, "c1.wav", start=0, framerate=8000)
        self.add_clip(self.a, "c2.wav", start=SEC, framerate=8000)

    def _player(self):
        from wavesnip.player import Player

        player = Player()
        player.set_timeline(self.timeline())
        return player

    def test_preview_writes_a_playable_file(self):
        plan = self.timeline().plan_snip(at(2), at(SEC + 2))
        with self._player() as player:
            player.play_snip(plan)
            rendered = player._source.path
            self.assertTrue(rendered.exists())
            info = wavio.read_info(rendered)
            self.assertEqual(info.framerate, 8000)
            self.assertAlmostEqual(info.nframes / 8000, SEC, delta=0.2)

    def test_preview_offsets_timeline_to_the_snip_start(self):
        plan = self.timeline().plan_snip(at(2), at(SEC + 2))
        with self._player() as player:
            player.play_snip(plan)
            self.assertEqual(player._source.start_us, plan.start_us)

    def test_preview_cleans_up_its_render(self):
        plan = self.timeline().plan_snip(at(0), at(SEC))
        player = self._player()
        player.play_snip(plan)
        rendered = player._source.path
        player.__exit__()
        self.assertFalse(rendered.exists(), "temp render must not outlive the player")

    def test_preview_reports_an_impossible_plan(self):
        plan = self.timeline().plan_snip(at(SEC * 8), at(SEC * 9))
        with self._player() as player:
            with self.assertRaises(Exception):
                player.play_snip(plan)


class SeekAccuracyTests(CursorCase):
    """A click in the middle of a clip plays from there, not from its start.

    The bug this pins: Qt **drops** ``setPosition()`` issued while the media
    is still loading, so clicking into a clip the player had to open fresh
    started playback at the file's beginning and the playhead jumped back to
    the clip's start. The seek now waits for ``LoadedMedia``, and ``play()``
    waits for the seek.

    These drive the real ``QMediaPlayer`` (like ``ExportPreviewTests``), and
    every one of them waits for the state it is about to assert -- media
    loaded, playback running, seek landed -- before asserting; otherwise the
    assertion would be about a request still in flight, which is the trap
    recorded in ``flow.md`` ("A test can pass without ever reaching the code
    it names").
    """

    #: A local WAV loads in ~10 ms; the bound only exists so a real failure
    #: fails instead of hanging.
    WAIT = 10.0
    #: The seek is millisecond-accurate and the offset is truncated to whole
    #: milliseconds, so a correct playhead can land this far under the target.
    #: The failure being pinned is *seconds* away, so this stays tight.
    TOLERANCE_US = 20_000

    def setUp(self):
        super().setUp()
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])
        self.a = self.make_archive("a")
        self.add_clip(self.a, "c1.wav", start=0, framerate=8000)
        self.add_clip(self.a, "c2.wav", start=SEC, framerate=8000)

    # -- helpers ----------------------------------------------------------

    def _player(self):
        from wavesnip.player import Player

        player = Player()
        player.set_timeline(self.timeline())
        self.addCleanup(self._stop_quietly, player)
        return player

    def _stop_quietly(self, player) -> None:
        """Stop after the event loop has caught up with the last seek.

        ``QMediaPlayer.stop()`` (and ``pause()``) can deadlock when called in
        the few hundred milliseconds after a seek lands — a pre-existing Qt
        race, not something the fix introduced: measured at 4/30 hangs when
        stopping straight after a same-clip seek under the fixed code and
        2/30 under the original semantics, against 0/30 with 300 ms of event
        loop in between (write-up in ``flow.md``). These tests assert on a
        seek and then stop immediately, so without this spin they would
        inherit a ~10 % hang per test.
        """
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.005)
        player.stop()

    def _spin_until(self, predicate) -> bool:
        """Run the event loop until ``predicate`` holds; report whether it did."""
        end = time.monotonic() + self.WAIT
        while time.monotonic() < end:
            if predicate():
                return True
            self.app.processEvents()
            time.sleep(0.005)
        return bool(predicate())

    @staticmethod
    def _seek_reached_backend(player) -> bool:
        """The pending seek has been issued -- or never existed.

        ``getattr`` rather than a bare attribute read so that a version of the
        player without the pending mechanism fails on the *position*
        assertions below, which is the honest place for it to fail.
        """
        return getattr(player, "_pending_seek_ms", None) is None

    def _wait_until_playing_at(self, player) -> None:
        """Wait until the media is ready, playback runs, and the seek landed.

        Asserting before that would pass without the seek ever being given its
        chance -- the load window is exactly where the bug lived.

        "Ready" is deliberately more than ``LoadedMedia``: while a clip plays
        its status is ``BufferingMedia`` (the null sink in tests may never
        leave it), so insisting on ``LoadedMedia`` would wait forever and
        every test here would fail on its setup rather than on the bug.
        """
        from PySide6.QtMultimedia import QMediaPlayer

        ready = (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
        )
        settled = self._spin_until(
            lambda: player.is_playing
            and player._player.mediaStatus() in ready
            and self._seek_reached_backend(player)
        )
        self.assertTrue(settled, "media never loaded and started playing")

    def _wait_until_at(self, player, target: int, note: str) -> None:
        """Wait until the playhead sits at ``target``, playing or not.

        The companion of ``_wait_until_playing_at`` for the case where nothing
        *is* playing: a click moves the playhead, it does not start playback
        (the Play button does that), so the honest signal that the seek landed
        is the playhead itself -- read through ``position_us``, which reports
        the pending seek's target even while the backend still holds the old
        position.
        """
        landed = self._spin_until(
            lambda: abs(player.position_us - target) <= self.TOLERANCE_US
        )
        self.assertTrue(
            landed,
            f"{note}: playhead is at {(player.position_us - at(0)) / 1e6:.3f} s "
            f"of the archive, not at the clicked {(target - at(0)) / 1e6:.3f} s",
        )

    def _assert_at(self, player, target: int, note: str) -> None:
        self.assertGreaterEqual(
            player.position_us,
            target - self.TOLERANCE_US,
            f"{note}: playhead is at "
            f"{(player.position_us - at(0)) / 1e6:.3f} s of the clip, "
            f"not at the clicked {(target - at(0)) / 1e6:.3f} s",
        )

    # -- the reported bug ---------------------------------------------------

    def test_play_from_the_middle_of_a_clip_starts_there(self):
        """Click mid-clip, press Play: it played from the beginning."""
        player = self._player()
        started_at_backend_ms: list[int] = []
        player.stateChanged.connect(
            lambda on: started_at_backend_ms.append(player._player.position())
            if on
            else None
        )

        target = at(5)
        player.play_at(target)
        self._wait_until_playing_at(player)
        self._assert_at(player, target, "cold play into the middle of c1")

        # The backend itself must already be at the offset when playback
        # reports playing -- position_us would hide this, because it reports
        # the *pending* seek's target while the load is in flight.
        self.assertTrue(started_at_backend_ms, "playback never reported playing")
        self.assertGreaterEqual(
            started_at_backend_ms[0],
            (target - at(0)) // 1000 - self.TOLERANCE_US // 1000,
            "audio began at the file's start: play() was not waiting for the seek",
        )

    def test_clicking_into_a_clip_the_player_has_not_loaded(self):
        """"A new clip": crossing into a file the player has never opened."""
        player = self._player()
        player.play_at(at(1))
        self._wait_until_playing_at(player)

        target = at(SEC + 5)
        announced: list[int] = []
        player.positionChanged.connect(lambda us: announced.append(int(us)))
        player.seek(target)
        self._wait_until_playing_at(player)

        self._assert_at(player, target, "seek into a clip loaded fresh")
        self.assertTrue(announced, "the seek announced nothing")
        self.assertTrue(
            all(us >= target - self.TOLERANCE_US for us in announced),
            "the playhead fell back to the clip's beginning: "
            f"first values {announced[:5]}",
        )

    def test_clicking_elsewhere_in_the_same_clip(self):
        """The in-clip seek must keep working -- and it never leaves LoadedMedia.

        A same-URL ``setSource`` emits **no** status change at all, so this is
        the path where a seek waiting for an event would wait forever. It has
        to be issued immediately instead, which is what makes it pass both
        before and after the fix: a guard against regressing it.
        """
        player = self._player()
        player.play_at(at(1))
        self._wait_until_playing_at(player)

        target = at(8)
        player.seek(target)
        self._wait_until_playing_at(player)
        self._assert_at(player, target, "seek within the clip already loaded")

    def test_clicking_back_into_a_clip_that_finished(self):
        """Playback ran off the end of the archive; the click still lands.

        The player has just reloaded the clip's tail without playing it, so
        this covers the seek issued when the media sits at its very end.
        Nothing plays afterwards -- a click moves the playhead, the Play
        button starts playback -- so the playhead itself is what must have
        moved, and it is waited on rather than assumed.
        """
        player = self._player()
        player.play_at(at(SEC + SEC - 2))  # two seconds before the archive ends
        self._wait_until_playing_at(player)
        self.assertTrue(
            self._spin_until(lambda: not player.is_playing),
            "playback never reached the end of the archive",
        )

        target = at(SEC + 5)
        player.seek(target)
        self._wait_until_at(player, target, "seek after the clip had finished")

    def test_the_click_is_announced_synchronously(self):
        """The playhead moves at click time, not when the file finishes loading.

        The visible symptom of the bug was the playhead *jumping* to the
        clip's start the moment a clip was clicked; the window only knows
        where playback is going if the target is emitted straight away.
        """
        player = self._player()
        player.play_at(at(1))
        self._wait_until_playing_at(player)

        announced: list[int] = []
        player.positionChanged.connect(lambda us: announced.append(int(us)))
        target = at(SEC + 5)
        player.seek(target)

        self.assertTrue(announced, "the seek must announce its target at once")
        self.assertEqual(
            announced[0],
            target,
            "the first announcement must be the clicked instant itself",
        )


if __name__ == "__main__":
    unittest.main()