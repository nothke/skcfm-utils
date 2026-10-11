"""Tests for wavesnip.filepeaks -- the per-file envelope cache.

The per-file envelope cache is the answer to "waveforms take minutes on a
spinning disk": it turns the seek-bound re-reads of the sampled path into one
sequential forward sweep per file range, served from RAM afterwards. These
tests pin down that the fold is frame-exact, that building stops at exactly
the requested range, that the cache forgets files it has no room for, and that
the assembled view is the same envelope the exact path would draw.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from wavesnip import wavio
from wavesnip.filepeaks import FilePeakCache
from wavesnip.model import Archive, Clip, to_us
from wavesnip.peaks import PEAK_COLUMNS, peaks_for
from wavesnip.timeline import Piece, Timeline

RATE = 8000
BASE = datetime(2026, 5, 12, 10, 15, 0)


def at(seconds: float) -> int:
    """Timeline instant ``seconds`` away from BASE."""
    return to_us(BASE + timedelta(seconds=seconds))


def range_reader(samples: np.ndarray):
    """A ``read`` for the cache that slices ``samples`` instead of touching a disk.

    Records every ``(path, start, count)`` so tests can assert on how much was
    read, which is the whole point of the cache.
    """
    calls: list[tuple[str, int, int]] = []

    def read(path, start, count):
        calls.append((str(path), int(start), int(count)))
        if start >= samples.shape[0]:
            return np.zeros((0, samples.shape[1]), dtype=np.float32)
        return samples[start : start + count]

    return read, calls


def reference(entry, samples: np.ndarray):
    """The envelope ``_fold_columns`` must reproduce, computed directly.

    Not through any code under test: a plain per-column loop over the source
    samples, so a mistake in chunk alignment, reshape or the ragged tail shows
    up as a mismatch rather than being baked into the reference.
    """
    fpc = entry.frames_per_col
    ncols = entry.ncols
    mins = np.full(ncols, np.inf, dtype=np.float32)
    maxs = np.full(ncols, -np.inf, dtype=np.float32)
    sumsq = np.zeros(ncols, dtype=np.float64)
    for j in range(ncols):
        seg = samples[j * fpc : (j + 1) * fpc]
        if seg.shape[0] == 0:
            continue
        mins[j] = seg.min()
        maxs[j] = seg.max()
        sumsq[j] = float(np.square(seg).sum())
    return mins, maxs, sumsq


def make_clip(path: Path, nframes: int, ch: int = 1, mtime: float = 0.0) -> Clip:
    return Clip(
        archive_path=path.parent,
        path=path,
        size=1024,
        mtime_us=int(mtime * 1_000_000),
        start_us=0,
        nframes=nframes,
        framerate=RATE,
        nchannels=ch,
        sampwidth=2,
    )


def piece_for(clip: Clip, lo: int, hi: int) -> Piece:
    return Piece(
        clip=clip,
        start_frame=max(0, min(lo, clip.nframes)),
        end_frame=max(0, min(hi, clip.nframes)),
        start_us=clip.start_us,
        end_us=clip.start_us,
    )


class CacheBuildTests(unittest.TestCase):
    """The fold itself: frame-exact reduction, correct ragged tail, no over-read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "f.wav"

    def build(self, samples: np.ndarray, *, piece_range=None, ch: int = 1):
        n, c = samples.shape
        cache = FilePeakCache(read=range_reader(samples)[0])
        clip = make_clip(self.path, n, ch=ch)
        # piece_range overrides the covered frames; default covers the whole file.
        lo = 0 if piece_range is None else piece_range[0]
        hi = n if piece_range is None else piece_range[1]
        entry = cache.envelope_for(piece_for(clip, lo, hi))
        self.assertIsNotNone(entry)
        return cache, entry

    def test_the_fold_is_frame_exact(self):
        rng = np.random.default_rng(0)
        samples = rng.uniform(-1, 1, (100_000, 1)).astype(np.float32)
        _, entry = self.build(samples)

        self.assertEqual(entry.ncols, 50)  # 100 000 frames / 2000 per 0.25 s
        ref_mins, ref_maxs, ref_sumsq = reference(entry, samples)
        np.testing.assert_array_equal(entry.mins[:50], ref_mins)
        np.testing.assert_array_equal(entry.maxs[:50], ref_maxs)
        np.testing.assert_allclose(entry.sumsq[:50], ref_sumsq, rtol=1e-5)
        self.assertTrue(np.all(entry.built))

    def test_stereo_folds_to_the_extremum_over_channels(self):
        n = 40_000
        samples = np.zeros((n, 2), dtype=np.float32)
        samples[:, 0] = np.sin(np.arange(n) / 50, dtype=np.float32)
        samples[:, 1] = -samples[:, 0]
        _, entry = self.build(samples, ch=2)
        # The two channels are opposites inside every column, so the envelope
        # must see both -1 and +1 (never an average cancelling to zero).
        self.assertTrue(np.all(entry.maxs > 0.99))
        self.assertTrue(np.all(entry.mins < -0.99))

    def test_a_ragged_file_folds_its_tail_into_the_last_column(self):
        fpc = 2000
        n = 10 * fpc + 37
        rng = np.random.default_rng(1)
        samples = rng.uniform(-1, 1, (n, 1)).astype(np.float32)
        _, entry = self.build(samples)
        self.assertEqual(entry.ncols, 11)
        ref_mins, ref_maxs, ref_sumsq = reference(entry, samples)
        np.testing.assert_array_equal(entry.mins, ref_mins)
        np.testing.assert_array_equal(entry.maxs, ref_maxs)
        np.testing.assert_allclose(entry.sumsq, ref_sumsq, rtol=1e-4)
        # The ragged last column is its 37 frames, never padded with silence.
        tail = samples[-37:]
        self.assertEqual(entry.column_counts(np.array([10]))[0], 37)

    def test_a_file_shorter_than_one_column_still_builds(self):
        samples = np.linspace(-1, 1, 50, dtype=np.float32).reshape(50, 1)
        _, entry = self.build(samples)
        self.assertEqual(entry.ncols, 1)
        self.assertEqual(float(entry.mins[0]), -1.0)
        self.assertEqual(float(entry.maxs[0]), 1.0)

    def test_building_stops_at_the_requested_range(self):
        samples = np.random.default_rng(2).uniform(-1, 1, (40_000, 1)).astype(np.float32)
        reader, calls = range_reader(samples)
        cache = FilePeakCache(read=reader)
        clip = make_clip(self.path, 40_000)
        cache.envelope_for(piece_for(clip, 2 * 2000, 5 * 2000))

        # One sequential read covering only the requested columns, no whole-file
        # sweep and no seeking back and forth.
        self.assertEqual(calls, [(str(self.path), 4000, 6000)])

    def test_a_built_range_is_not_read_again(self):
        samples = np.random.default_rng(3).uniform(-1, 1, (40_000, 1)).astype(np.float32)
        reader, calls = range_reader(samples)
        cache = FilePeakCache(read=reader)
        clip = make_clip(self.path, 40_000)
        piece = piece_for(clip, 0, 40_000)
        cache.envelope_for(piece)
        cache.envelope_for(piece)
        self.assertEqual(len(calls), 1)

    def test_an_overlapping_request_fills_only_the_gap(self):
        samples = np.random.default_rng(4).uniform(-1, 1, (60_000, 1)).astype(np.float32)
        reader, calls = range_reader(samples)
        cache = FilePeakCache(read=reader)
        clip = make_clip(self.path, 60_000)
        cache.envelope_for(piece_for(clip, 0, 4 * 2000))       # cols 0..3
        cache.envelope_for(piece_for(clip, 8 * 2000, 11 * 2000))  # cols 8..10
        cache.envelope_for(piece_for(clip, 3 * 2000, 9 * 2000))   # cols 3..8, gap is 4..7
        entry = cache.envelope_for(piece_for(clip, 0, 60_000))
        self.assertTrue(np.all(entry.built))
        # Cols 0..3, 8..10 were built by the first two requests; only the gap
        # in the middle should have been read by the third.
        self.assertEqual(calls[2], (str(self.path), 8_000, 8_000))

    def test_an_empty_file_is_not_cached(self):
        cache = FilePeakCache(read=range_reader(np.zeros((0, 1), dtype=np.float32))[0])
        clip = make_clip(self.path, 0)
        self.assertIsNone(cache.envelope_for(piece_for(clip, 0, 0)))

    def test_a_changed_file_identity_builds_again(self):
        samples = np.random.default_rng(5).uniform(-1, 1, (40_000, 1)).astype(np.float32)
        reader, calls = range_reader(samples)
        cache = FilePeakCache(read=reader)
        clip_a = make_clip(self.path, 40_000, mtime=100.0)
        clip_b = make_clip(self.path, 40_000, mtime=200.0)
        cache.envelope_for(piece_for(clip_a, 0, 40_000))
        cache.envelope_for(piece_for(clip_b, 0, 40_000))
        self.assertEqual(len(calls), 2)

    def test_lru_eviction_drops_the_oldest_file(self):
        n = 4 * 2000
        samples = np.random.default_rng(6).uniform(-1, 1, (n, 1)).astype(np.float32)
        reader, calls = range_reader(samples)
        # Room for one small entry (an envelope of 4 columns is ~52 bytes),
        # so the second build must evict the first file.
        cache = FilePeakCache(read=reader, max_bytes=100)
        clip_a = make_clip(Path(self._tmp.name) / "a.wav", n, mtime=1.0)
        clip_b = make_clip(Path(self._tmp.name) / "b.wav", n, mtime=2.0)
        cache.envelope_for(piece_for(clip_a, 0, n))
        cache.envelope_for(piece_for(clip_b, 0, n))
        # 'a' was evicted: asking for it again reads the file again.
        cache.envelope_for(piece_for(clip_a, 0, n))
        self.assertEqual(sum(1 for c in calls if c[0].endswith("a.wav")), 2)


class EnvelopePathTests(unittest.TestCase):
    """peaks_for with a cache: the whole tier, on plans built by hand.

    These are the invariant tests, in the same shape as PeaksPlanTests: the
    cached path must draw the same envelope as the exact path when the two can
    be compared, must not touch the exact or sampled readers, and must never
    paint into a gap.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def clip(self, name: str, *, start: float, seconds: float, amplitude: float = 0.5):
        """A WAV whose mtime puts it at ``start``, and return its Clip.

        Reproduced from PeaksPlanTests: mtime is how the app places a file in
        time, so it is the one thing that has to be set for the timeline to put
        it where the test wants it.
        """
        folder = self.root / name.split(".")[0]
        folder.mkdir(exist_ok=True)
        t = np.arange(int(seconds * RATE), dtype=np.float32) / RATE
        samples = (amplitude * np.sin(2 * np.pi * 200 * t)).reshape(-1, 1)
        path = folder / name
        wavio.write_wav(path, samples, framerate=RATE)
        stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (stamp, stamp))

        from wavesnip.indexer import ArchiveIndex

        ArchiveIndex(Archive(path=folder)).scan()
        return path

    def timeline(self, *paths):
        folders = []
        for path in paths:
            if path.parent not in folders:
                folders.append(path.parent)
        return Timeline([Archive(path=f) for f in folders]).refresh()

    def plan(self, timeline, low: float, high: float):
        return timeline.plan_snip(at(low), at(high))

    def test_an_aligned_plan_equals_the_exact_path(self):
        """When every view column wraps exactly one cache column, the two paths
        have to agree frame for frame -- not within a tolerance of a pixel."""
        path = self.clip("one.wav", start=0, seconds=20)
        plan = self.plan(self.timeline(path), 0, 200)
        cache = FilePeakCache(step_us=125_000)  # 1000-frame columns @8 kHz

        cached = peaks_for(plan, cache=cache)
        exact = peaks_for(plan, 8_000_000)

        self.assertIsNotNone(cached)
        self.assertIsNotNone(exact)
        np.testing.assert_array_equal(cached.mins, exact.mins)
        np.testing.assert_array_equal(cached.maxs, exact.maxs)
        np.testing.assert_allclose(cached.rms, exact.rms, rtol=1e-4)
        # The whole clip's columns were painted (160 columns worth of audio).
        self.assertEqual(np.isnan(cached.mins).sum(), PEAK_COLUMNS - 160)

    def test_the_cache_serves_from_ram_after_the_first_view(self):
        path = self.clip("one.wav", start=0, seconds=20)
        plan = self.plan(self.timeline(path), 0, 200)
        cache = FilePeakCache(step_us=125_000)

        orig_read = wavio.read_frames

        def fail(*args, **kwargs):  # noqa: ANN002
            raise AssertionError("a cached view must not decode audio")

        wavio.read_frames = fail
        try:
            first = peaks_for(plan, cache=cache)
            second = peaks_for(plan, cache=cache)
        finally:
            wavio.read_frames = orig_read

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        np.testing.assert_array_equal(first.mins, second.mins)

    def test_a_narrow_view_stays_on_the_exact_path(self):
        """Below the cache's narrowest served width the exact path decodes the
        range -- correctly, and without the sampled path's seeks."""
        path = self.clip("one.wav", start=0, seconds=10)
        plan = self.plan(self.timeline(path), 0, 10)
        cache = FilePeakCache()

        calls = []

        from wavesnip import wavio as wavio_mod

        orig = wavio_mod.read_frames

        def counting_read(path, start, count):
            calls.append(count)
            return orig(path, start, count)

        from wavesnip.peaks import _peaks_probed

        orig_probed = _peaks_probed
        import wavesnip.peaks as peaks_mod

        def fail_probed(*args, **kwargs):  # noqa: ANN002
            raise AssertionError("a 10-second view is cheap to read whole")

        peaks_mod._peaks_probed = fail_probed
        try:
            wavio_mod.read_frames = counting_read
            try:
                peaks = peaks_for(plan, cache=cache)
            finally:
                wavio_mod.read_frames = orig
        finally:
            peaks_mod._peaks_probed = orig_probed

        self.assertIsNotNone(peaks)
        self.assertEqual(np.isnan(peaks.mins).sum(), 0)
        self.assertTrue(calls, "the exact path decoded the range")

    def test_gaps_stay_unpainted_on_the_cache_path(self):
        """A gap between clips is drawn as silence by the cache path too, not
        smeared by the cached columns that neighbor it."""
        a = self.clip("a.wav", start=0, seconds=10)
        b = self.clip("b.wav", start=20, seconds=10)
        timeline = self.timeline(a, b)
        plan = self.plan(timeline, 0, 1000)  # span >= 400 s: cache-eligible
        cache = FilePeakCache()

        peaks = peaks_for(plan, cache=cache)
        self.assertIsNotNone(peaks)

        mins = peaks.mins
        known = np.flatnonzero(~np.isnan(mins))
        # Ten seconds of a 1000-second view is sixteen columns of audio per
        # clip; the two clips paint two runs with a pure-NaN gap between them.
        runs = np.split(known, np.flatnonzero(np.diff(known) > 1) + 1)
        self.assertEqual(len(runs), 2, "two clips in a wide view paint two runs")
        self.assertGreater(runs[1][0] - runs[0][-1], 1)
        self.assertTrue(np.all(np.isnan(mins[runs[0][-1] + 1 : runs[1][0]])))

    def test_two_clips_in_one_column_merge_extremes(self):
        """The settled-column merge: a view column straddling a seam between
        two clips must hold the extrema of *both*, not the first piece's.

        The seam sits inside column 16 of a 1000-second view (col width 625 ms,
        seam at 10.3 s), so the quiet clip writes the column first and the loud
        one has to merge into what is already there.
        """
        quiet = self.clip("a.wav", start=0, seconds=10.3, amplitude=0.1)
        loud = self.clip("b.wav", start=10.3, seconds=10.3, amplitude=0.9)
        plan = self.plan(self.timeline(quiet, loud), 0, 1000)
        cache = FilePeakCache()

        peaks = peaks_for(plan, cache=cache)
        self.assertIsNotNone(peaks)

        col = 16  # spans [10.0, 10.625) s, straddling the butt joint at 10.3 s
        self.assertTrue(np.isfinite(peaks.maxs[col]), "the seam column is painted")
        self.assertGreater(peaks.maxs[col], 0.85, "the loud clip's peak survived")
        self.assertLess(peaks.mins[col], -0.85, "the loud clip's trough survived")
        # The neighbouring column belongs to the quiet clip alone, so this is a
        # merge of two pieces into one column, not a wipe of the first by the
        # second.
        self.assertLess(peaks.maxs[col - 1], 0.2)

    def test_a_piece_far_narrower_than_the_view_keeps_a_column(self):
        """The exact path drops a piece that would get zero proportional
        columns; the cache maps by absolute time, so a lone short clip in a
        wide window still paints."""
        path = self.clip("one.wav", start=0, seconds=1)
        plan = self.plan(self.timeline(path), 0, 4000)
        cache = FilePeakCache()

        peaks = peaks_for(plan, cache=cache)
        self.assertIsNotNone(peaks)
        known = np.flatnonzero(~np.isnan(peaks.mins))
        self.assertEqual(known.tolist(), [0], "the clip lands in its own column")

    def test_gaps_stay_nan_in_a_mixed_rate_plan(self):
        """Rate does not break the time mapping: a wide view over an 8 kHz and a
        48 kHz file paints both clips and nothing between them."""
        rate = 48_000

        def clip48(name: str, *, start: float, seconds: float):
            folder = self.root / name.split(".")[0]
            folder.mkdir(exist_ok=True)
            t = np.arange(int(seconds * rate), dtype=np.float32) / rate
            samples = (0.5 * np.sin(2 * np.pi * 200 * t)).reshape(-1, 1)
            path = folder / name
            wavio.write_wav(path, samples, framerate=rate)
            stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
            os.utime(path, (stamp, stamp))

            from wavesnip.indexer import ArchiveIndex

            ArchiveIndex(Archive(path=folder)).scan()
            return path

        a = self.clip("a.wav", start=0, seconds=10)
        b = clip48("b.wav", start=20, seconds=10)
        plan = self.plan(self.timeline(a, b), 0, 1000)
        cache = FilePeakCache()

        peaks = peaks_for(plan, cache=cache)
        self.assertIsNotNone(peaks)
        known = np.flatnonzero(~np.isnan(peaks.mins))
        runs = np.split(known, np.flatnonzero(np.diff(known) > 1) + 1)
        self.assertEqual(len(runs), 2, "both clips paint, despite different rates")
        self.assertTrue(np.all(np.isnan(peaks.mins[runs[0][-1] + 1 : runs[1][0]])))


if __name__ == "__main__":
    unittest.main()