"""Tests for wavesnip.indexer.

The scan is built against real WAV files with controlled mtimes, because the
whole point of the index is the ``mtime - duration`` derivation. A fake stat
would not exercise it.

Timelines are built with a low sample rate (8 Hz) and short clips so fixtures
stay tiny while remaining exactly frame-representable.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from wavesnip import wavio
from wavesnip.indexer import ArchiveIndex, count_overlaps
from wavesnip.model import Archive, Clip, to_us

RATE = 8  # frames per second; 1 frame == 125 ms
SEC = 10
FRAMES = RATE * SEC

BASE = datetime(2026, 5, 12, 10, 15, 0)


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "archive"
        self.folder.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.index = ArchiveIndex(Archive(path=self.folder))

    def add_clip_file(
        self,
        name: str,
        *,
        ends_at: datetime,
        seconds: int = SEC,
        framerate: int = RATE,
        nchannels: int = 1,
        sampwidth: int = 2,
    ) -> Path:
        """Write a real WAV whose mtime is exactly ``ends_at``."""
        rng = np.random.default_rng(abs(hash(name)) % (2**31))
        scale = 2 ** (8 * sampwidth - 1)
        ints = rng.integers(-(scale // 2), scale // 2, size=(framerate * seconds, nchannels))
        samples = (ints / scale).astype(np.float32)

        path = self.folder / name
        wavio.write_wav(path, samples, framerate=framerate, sampwidth=sampwidth)
        stamp = ends_at.timestamp()
        os.utime(path, (stamp, stamp))
        return path


class TestScanBasics(ArchiveCase):
    def test_empty_folder(self):
        report = self.index.scan()
        self.assertEqual(report.total, 0)
        self.assertEqual(self.index.load_clips(), [])

    def test_single_clip_start_is_mtime_minus_duration(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        (clip,) = self.index.load_clips()

        self.assertEqual(clip.end_us, to_us(BASE))
        self.assertEqual(clip.duration_us, SEC * 1_000_000)
        self.assertEqual(clip.start_us, to_us(BASE - timedelta(seconds=SEC)))
        self.assertEqual(clip.start, BASE - timedelta(seconds=SEC))
        self.assertEqual(clip.nframes, FRAMES)
        self.assertEqual(clip.framerate, RATE)

    def test_scan_reports_summary(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=2 * SEC))
        report = self.index.scan()
        self.assertEqual(report.indexed, 2)
        self.assertEqual(report.reused, 0)
        self.assertIn("indexed 2 clips", report.summary())

    def test_second_scan_reuses_everything(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        report = self.index.scan()
        self.assertEqual(report.reused, 1)
        self.assertEqual(report.indexed, 0, "unchanged files must not be re-parsed")

    def test_index_file_is_not_itself_indexed(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        self.assertTrue(self.index.path.exists())
        self.index.scan()
        self.assertEqual(len(self.index.load_clips()), 1)


class TestSkipping(ArchiveCase):
    def test_non_wav_files_are_ignored_silently(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        (self.folder / "notes.txt").write_text("hello")
        (self.folder / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        report = self.index.scan()
        self.assertEqual(report.total, 1)
        self.assertEqual(report.skipped, 0, "non-wav files are not 'skipped', just ignored")

    def test_corrupt_wav_is_skipped_and_counted(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        bad = self.folder / "bad.wav"
        bad.write_bytes(b"RIFFnot really a wav at all")
        os.utime(bad, (BASE.timestamp(), BASE.timestamp()))
        report = self.index.scan()
        self.assertEqual(report.total, 1)
        self.assertEqual(report.skipped, 1)
        self.assertIn("1 skipped", report.summary())

    def test_zero_length_wav_is_skipped(self):
        # What an interrupted recording leaves behind.
        path = wavio.write_wav(self.folder / "empty.wav", np.zeros((0, 1)), RATE)
        os.utime(path, (BASE.timestamp(), BASE.timestamp()))
        report = self.index.scan()
        self.assertEqual(report.total, 0)
        self.assertEqual(report.skipped, 1)

    def test_subdirectories_are_ignored(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        (self.folder / "subdir").mkdir()
        report = self.index.scan()
        self.assertEqual(report.total, 1)


class TestIncremental(ArchiveCase):
    def test_new_file_is_picked_up(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=2 * SEC))
        report = self.index.scan()
        self.assertEqual((report.indexed, report.reused), (1, 1))
        self.assertEqual(len(self.index.load_clips()), 2)

    def test_changed_mtime_reindexes_and_moves_start(self):
        path = self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        (before,) = self.index.load_clips()

        moved = BASE + timedelta(days=1)
        os.utime(path, (moved.timestamp(), moved.timestamp()))
        report = self.index.scan()

        self.assertEqual(report.indexed, 1, "mtime change must force a re-parse")
        (after,) = self.index.load_clips()
        self.assertNotEqual(before.start_us, after.start_us)
        self.assertEqual(after.start_us, to_us(moved - timedelta(seconds=SEC)))

    def test_deleted_file_is_pruned(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=2 * SEC))
        self.index.scan()
        self.assertEqual(len(self.index.load_clips()), 2)

        (self.folder / "b.wav").unlink()
        report = self.index.scan()
        self.assertEqual(report.removed, 1)
        self.assertEqual(len(self.index.load_clips()), 1)

    def test_unchanged_size_and_mtime_is_not_reparsed(self):
        """Documents the accepted risk: the index trusts (size, mtime).

        Corrupt the audio underneath the index while leaving size *and* mtime
        untouched. The cache must win and serve the old, now-wrong entry --
        there is deliberately no integrity check, because mtime is treated as
        trustworthy. See the mtime note in ``flow.md``.
        """
        path = self.add_clip_file("a.wav", ends_at=BASE)
        self.index.scan()
        original_stat = path.stat()

        raw = bytearray(path.read_bytes())
        raw[-200:] = b"\x00" * 200
        path.write_bytes(bytes(raw))
        # write_bytes bumps mtime; put it back so only the audio is corrupt.
        os.utime(path, (original_stat.st_atime, original_stat.st_mtime))

        report = self.index.scan()
        self.assertEqual(report.reused, 1)
        self.assertEqual(report.indexed, 0)


class TestQuery(ArchiveCase):
    """Geometry used throughout, as [start, end) relative to BASE:

    butt-joined    a: [-10s,  0s)   b: [ 0s, +10s)   seam at 0
    gapped         a: [-10s,  0s)   c: [+20s, +30s)   gap [0, +20)
    overlapping    a: [-10s,  0s)   b: [-5s,  +5s)   overlap [-5, 0)
    """

    def test_butt_joined_seam_returns_exactly_one(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=SEC))
        self.index.scan()
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(to_us(BASE))], ["b.wav"]
        )

    def test_interior_of_first_clip(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=SEC))
        self.index.scan()
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(
                to_us(BASE - timedelta(seconds=5)))],
            ["a.wav"],
        )

    def test_interior_of_second_clip(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=SEC))
        self.index.scan()
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(
                to_us(BASE + timedelta(seconds=5)))],
            ["b.wav"],
        )

    def test_gap_returns_nothing(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("c.wav", ends_at=BASE + timedelta(seconds=3 * SEC))
        self.index.scan()
        for offset in (1, 5, 15, 19):
            with self.subTest(offset=offset):
                self.assertEqual(
                    self.index.clips_containing(
                        to_us(BASE + timedelta(seconds=offset))
                    ),
                    [],
                )

    def test_gap_boundaries(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("c.wav", ends_at=BASE + timedelta(seconds=3 * SEC))
        self.index.scan()
        # a: [-10, 0)  c: [+20, +30). The gap is [0, +20) -- half-open, so a's
        # end is outside it, and +20 is c's *start*, which is inside c.
        self.assertEqual(self.index.clips_containing(to_us(BASE)), [])
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(
                to_us(BASE + timedelta(seconds=20)))],
            ["c.wav"],
        )
        self.assertEqual(
            self.index.clips_containing(to_us(BASE + timedelta(seconds=20)) - 1),
            [],
        )

    def test_overlap_returns_both_clips(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=5))
        self.index.scan()
        for offset in (-5, -3, -1):  # inside the shared region [-5, 0)
            with self.subTest(offset=offset):
                self.assertEqual(
                    [c.path.name for c in self.index.clips_containing(
                        to_us(BASE + timedelta(seconds=offset)))],
                    ["a.wav", "b.wav"],
                )
        # Outside the overlap each instant still resolves uniquely.
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(
                to_us(BASE + timedelta(seconds=2)))],
            ["b.wav"],
        )
        self.assertEqual(
            [c.path.name for c in self.index.clips_containing(
                to_us(BASE - timedelta(seconds=7)))],
            ["a.wav"],
        )

    def test_clips_sorted_by_start(self):
        self.add_clip_file("late.wav", ends_at=BASE + timedelta(seconds=4 * SEC))
        self.add_clip_file("early.wav", ends_at=BASE)
        self.index.scan()
        starts = [c.start_us for c in self.index.load_clips()]
        self.assertEqual(starts, sorted(starts))


class TestOverlapReporting(ArchiveCase):
    def test_gap_produces_no_overlap(self):
        a = Clip(Path("/x"), Path("/x/a.wav"), 1, 100, 0, 10, 1, 1, 2)
        b = Clip(Path("/x"), Path("/x/b.wav"), 1, 300, 200, 10, 1, 1, 2)
        self.assertEqual(count_overlaps([a, b]), 0)

    def test_touching_produces_no_overlap(self):
        a = Clip(Path("/x"), Path("/x/a.wav"), 1, 100, 0, 10, 1, 1, 2)
        b = Clip(Path("/x"), Path("/x/b.wav"), 1, 200, 100, 10, 1, 1, 2)
        self.assertEqual(count_overlaps([a, b]), 0)

    def test_overlap_counted(self):
        a = Clip(Path("/x"), Path("/x/a.wav"), 1, 100, 0, 10, 1, 1, 2)
        b = Clip(Path("/x"), Path("/x/b.wav"), 1, 150, 50, 10, 1, 1, 2)
        self.assertEqual(count_overlaps([a, b]), 1)

    def test_scan_reports_overlaps_in_summary(self):
        # b ends 15s after a, so b covers [+5s, +15s) while a covers [-10s, 0)
        # relative to BASE -- wait, those do not meet either. Make b start
        # before a ends: b must begin at or before BASE, so end it at +5s.
        self.add_clip_file("a.wav", ends_at=BASE)
        self.add_clip_file("b.wav", ends_at=BASE + timedelta(seconds=5))
        report = self.index.scan()
        # a: [-10s, 0)   b: [-5s, +5s)   -> 5s shared
        self.assertEqual(report.overlaps, 1)
        self.assertIn("1 overlaps", report.summary())


class TestUnreadableFolders(ArchiveCase):
    """Three folder states, and they do not mean the same thing.

    Conflating "the folder is not there" with "the index has not been built yet"
    is what made a mistyped command-line argument look like an app with no audio
    and no explanation.
    """

    def test_reading_a_folder_that_does_not_exist_says_so(self):
        index = ArchiveIndex(Archive(path=self.folder / "typo"))
        with self.assertRaises(FileNotFoundError) as caught:
            index.load_clips()
        self.assertIn("typo", str(caught.exception))
        self.assertIn("not found", str(caught.exception))

    def test_a_file_where_a_folder_should_be_says_so_differently(self):
        not_a_folder = self.folder.parent / "a-file"
        not_a_folder.write_text("this is a file")

        index = ArchiveIndex(Archive(path=not_a_folder))
        with self.assertRaises(FileNotFoundError) as caught:
            index.load_clips()
        self.assertIn("not a folder", str(caught.exception))

    def test_scanning_a_missing_folder_says_the_same_thing(self):
        index = ArchiveIndex(Archive(path=self.folder / "typo"))
        with self.assertRaises(FileNotFoundError) as caught:
            index.scan()
        self.assertIn("not found", str(caught.exception))

    def test_an_unscanned_folder_reads_as_empty(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        self.assertEqual(self.index.load_clips(), [])
        self.assertEqual(self.index.clips_containing(to_us(BASE)), [])

    def test_reading_does_not_create_the_index_file(self):
        """An index appearing in a folder that was merely looked at is litter."""
        self.add_clip_file("a.wav", ends_at=BASE)
        self.assertFalse(self.index.has_index())

        self.index.load_clips()
        self.assertFalse(
            self.index.has_index(),
            "reading an archive must not write into it",
        )
        self.assertFalse(self.index.path.exists())

    def test_reading_leaves_the_folder_alone_entirely(self):
        self.add_clip_file("a.wav", ends_at=BASE)
        before = sorted(p.name for p in self.folder.iterdir())

        self.index.load_clips()
        self.index.clips_containing(to_us(BASE))

        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
