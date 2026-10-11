"""Tests for wavesnip.peaks -- the part of the waveform view that must be right.

A wrong waveform still looks like a waveform, so these are all invariants rather
than pixel comparisons: a transient survives reduction, a column with any audio
in it is reported as covered, and the column boundaries are exact integers.
"""

from __future__ import annotations

import unittest

import numpy as np

from wavesnip.peaks import (
    Peaks,
    column_edges,
    compute_peaks,
    covered_columns,
)

SEC = 1_000_000


class ComputePeaksTests(unittest.TestCase):
    def test_bucket_count_is_respected(self):
        samples = np.zeros((48000, 1), dtype=np.float32)
        self.assertEqual(len(compute_peaks(samples, 800)), 800)

    def test_a_single_bucket_sees_the_whole_clip(self):
        samples = np.linspace(-1, 1, 1000, dtype=np.float32).reshape(-1, 1)
        peaks = compute_peaks(samples, 1)
        self.assertAlmostEqual(float(peaks.mins[0]), -1.0, places=5)
        self.assertAlmostEqual(float(peaks.maxs[0]), 1.0, places=5)

    def test_an_isolated_transient_survives(self):
        """The reason for min/max: one loud sample must not vanish."""
        samples = np.zeros((10000, 1), dtype=np.float32)
        samples[5432] = 0.95
        peaks = compute_peaks(samples, 100)
        self.assertGreater(float(peaks.maxs.max()), 0.9)

    def test_min_and_max_keep_the_sign(self):
        samples = np.full((100, 1), -0.5, dtype=np.float32)
        peaks = compute_peaks(samples, 10)
        self.assertTrue(np.all(peaks.maxs == -0.5))

    def test_channels_fold_by_extremum_not_average(self):
        """Averaging would cancel opposite channels and show silence."""
        samples = np.zeros((100, 2), dtype=np.float32)
        samples[:, 0] = 1.0
        samples[:, 1] = -1.0
        peaks = compute_peaks(samples, 10)
        self.assertTrue(np.all(peaks.maxs == 1.0))
        self.assertTrue(np.all(peaks.mins == -1.0))

    def test_mono_input_is_accepted(self):
        self.assertEqual(len(compute_peaks(np.zeros(48000, dtype=np.float32), 100)), 100)

    def test_three_dimensional_input_is_refused(self):
        with self.assertRaises(ValueError):
            compute_peaks(np.zeros((10, 2, 2), dtype=np.float32), 10)

    def test_zero_buckets_is_refused(self):
        with self.assertRaises(ValueError):
            compute_peaks(np.zeros((10, 1), dtype=np.float32), 0)

    def test_negative_buckets_is_refused(self):
        with self.assertRaises(ValueError):
            compute_peaks(np.zeros((10, 1), dtype=np.float32), -1)

    def test_empty_audio_gives_flat_columns(self):
        peaks = compute_peaks(np.zeros((0, 1), dtype=np.float32), 50)
        self.assertEqual(len(peaks), 50)
        self.assertTrue(peaks.is_silent())

    def test_silence_is_reported_as_silent(self):
        peaks = compute_peaks(np.zeros((1000, 1), dtype=np.float32), 10)
        self.assertTrue(peaks.is_silent())

    def test_loud_audio_is_not_reported_as_silent(self):
        t = np.linspace(0, 1, 1000, dtype=np.float32)
        self.assertFalse(compute_peaks(np.sin(t * 20), 10).is_silent())

    def test_more_buckets_than_frames_still_answers(self):
        """Zooming in hard must not divide by zero or drop columns."""
        peaks = compute_peaks(np.zeros((5, 1), dtype=np.float32), 500)
        self.assertEqual(len(peaks), 500)
        self.assertTrue(peaks.is_silent())

    def test_a_loud_tail_is_not_discarded(self):
        """The ragged remainder folds into the last column, not away."""
        samples = np.zeros((1001, 1), dtype=np.float32)
        samples[1000] = 0.9
        peaks = compute_peaks(samples, 10)
        self.assertGreater(float(peaks.maxs[-1]), 0.8)

    def test_odd_lengths_are_exact(self):
        """No frame may be counted twice or skipped when buckets do not divide."""
        samples = np.arange(1031, dtype=np.float32).reshape(-1, 1)
        peaks = compute_peaks(samples, 7)
        self.assertEqual(len(peaks), 7)
        self.assertAlmostEqual(float(peaks.maxs[-1]), 1030.0, places=3)

    def test_result_is_float32(self):
        peaks = compute_peaks(np.zeros((100, 1), dtype=np.float64), 10)
        self.assertEqual(peaks.mins.dtype, np.float32)
        self.assertEqual(peaks.maxs.dtype, np.float32)

    def test_frames_per_bucket_is_reported(self):
        peaks = compute_peaks(np.zeros((48000, 1), dtype=np.float32), 480)
        self.assertAlmostEqual(peaks.frames_per_bucket, 100.0)

    def test_mins_never_exceed_maxs(self):
        rng = np.random.default_rng(7)
        samples = rng.standard_normal((5000, 2)).astype(np.float32)
        peaks = compute_peaks(samples, 64)
        self.assertTrue(np.all(peaks.mins <= peaks.maxs))


class RmsBandTests(unittest.TestCase):
    """The inner band: the level the outline encloses.

    A min/max envelope alone saturates when zoomed out -- nearly every wide
    column holds a transient, so the outline is full height everywhere and a
    loud hour and a quiet hour become the same solid block. The band is what
    keeps them distinguishable, so its arithmetic has its own invariants.
    """

    def test_a_constant_level_bands_at_that_level(self):
        peaks = compute_peaks(np.full((1000, 1), 0.3, dtype=np.float32), 10)
        self.assertIsNotNone(peaks.rms)
        self.assertTrue(np.allclose(peaks.rms, 0.3, atol=1e-5))

    def test_silence_bands_at_zero_not_nan(self):
        """NaN would read as "no audio here"; silence is flat zero."""
        peaks = compute_peaks(np.zeros((1000, 1), dtype=np.float32), 10)
        self.assertTrue(np.all(peaks.rms == 0))

    def test_a_sine_bands_near_its_rms_not_its_peak(self):
        t = np.linspace(0, 1, 4800, dtype=np.float32)
        peaks = compute_peaks(np.sin(2 * np.pi * 100 * t), 100)
        self.assertAlmostEqual(float(peaks.rms.mean()), 0.707, places=2)
        self.assertLess(float(peaks.rms.max()), float(peaks.maxs.max()))

    def test_the_band_never_reaches_past_the_outline(self):
        """No column may draw a core brighter than the spikes around it."""
        rng = np.random.default_rng(5)
        samples = (rng.standard_normal((5000, 2)) * 0.4).astype(np.float32)
        peaks = compute_peaks(samples, 64)
        self.assertTrue(np.all(peaks.rms >= 0))
        reach = np.maximum(np.abs(peaks.mins), np.abs(peaks.maxs))
        self.assertTrue(np.all(peaks.rms <= reach + 1e-6))

    def test_the_ragged_tail_counts_toward_the_band(self):
        """A loud last frame must raise the last column's level, not vanish."""
        samples = np.zeros((1001, 1), dtype=np.float32)
        samples[1000] = 0.9
        peaks = compute_peaks(samples, 10)
        self.assertGreater(float(peaks.rms[-1]), 0.05)
        self.assertTrue(np.all(peaks.rms[:-1] == 0))

    def test_channels_fold_into_the_band(self):
        """Opposite channels are not silence to the ear, nor to the band."""
        samples = np.zeros((100, 2), dtype=np.float32)
        samples[:, 0] = 1.0
        samples[:, 1] = -1.0
        peaks = compute_peaks(samples, 10)
        self.assertTrue(np.allclose(peaks.rms, 1.0, atol=1e-5))

    def test_empty_audio_bands_at_zero(self):
        peaks = compute_peaks(np.zeros((0, 1), dtype=np.float32), 50)
        self.assertEqual(len(peaks.rms), 50)
        self.assertTrue(np.all(peaks.rms == 0))

    def test_a_hand_built_envelope_needs_no_band(self):
        """``rms=None`` means outline-only, which is legal -- not silence."""
        peaks = Peaks(np.zeros(4, dtype=np.float32), np.zeros(4, dtype=np.float32), 1.0)
        self.assertIsNone(peaks.rms)


class ColumnEdgeTests(unittest.TestCase):
    def test_width_plus_one_edges(self):
        self.assertEqual(len(column_edges(0, 1000, 100)), 101)

    def test_first_and_last_are_the_bounds(self):
        edges = column_edges(500, 1500, 100)
        self.assertEqual(edges[0], 500)
        self.assertEqual(edges[-1], 1500)

    def test_edges_are_non_decreasing(self):
        edges = column_edges(0, 10**15, 500)
        self.assertTrue(np.all(np.diff(edges) >= 0))

    def test_remainder_is_distributed_not_lost(self):
        """Exact integers: the edges tile the range with no gap at the end."""
        edges = column_edges(0, 10, 3)
        self.assertEqual(list(edges), [0, 3, 6, 10])

    def test_edges_are_stable_under_resize(self):
        """The same instant must not land in different columns after a resize."""
        edges = column_edges(0, 1000, 100)
        self.assertEqual(int(np.searchsorted(edges, 500, side="right")) - 1, 50)

    def test_zero_width_is_refused(self):
        with self.assertRaises(ValueError):
            column_edges(0, 100, 0)

    def test_inverted_range_is_refused(self):
        with self.assertRaises(ValueError):
            column_edges(100, 100, 10)

    def test_backwards_range_is_refused(self):
        with self.assertRaises(ValueError):
            column_edges(100, 50, 10)


class CoveredColumnTests(unittest.TestCase):
    def setUp(self):
        self.coverage = [(0, 10 * SEC), (20 * SEC, 30 * SEC)]

    def test_width_columns_returned(self):
        self.assertEqual(len(covered_columns(self.coverage, 0, 40 * SEC, 100)), 100)

    def test_a_fully_covered_range_is_all_true(self):
        self.assertTrue(covered_columns([(0, 10 * SEC)], 0, 10 * SEC, 50).all())

    def test_an_empty_range_is_all_false(self):
        self.assertFalse(covered_columns([], 0, 10 * SEC, 50).any())

    def test_a_gap_shows_as_false(self):
        # 40 s over 80 columns: 0.5 s each, so column N spans [N/2, N/2+0.5) s.
        cols = covered_columns(self.coverage, 0, 40 * SEC, 80)
        self.assertTrue(cols[0], "0-0.5 s is covered")
        self.assertTrue(cols[19], "9.5-10 s is still covered")
        self.assertFalse(cols[25], "12.5-13 s is inside the 10-20 s gap")
        self.assertTrue(cols[45], "22.5-23 s is covered again")
        self.assertFalse(cols[-1], "39.5-40 s is past the end of all audio")

    def test_a_narrow_clip_inside_a_wide_column_is_not_missed(self):
        """The midpoint-sampling bug this guards against.

        One pixel spans the whole range; the single covered second sits in its
        middle, far from any midpoint a naive implementation would sample.
        """
        cols = covered_columns([(SEC, 2 * SEC)], 0, 100 * SEC, 100)
        self.assertTrue(cols.any(), "a 1-second recording must not disappear")
        self.assertLess(cols.sum(), 100, "nor may it smear across the view")

    def test_clip_touching_the_start_edge_is_covered(self):
        self.assertTrue(covered_columns([(0, SEC)], 0, 10 * SEC, 10)[0])

    def test_clip_ending_at_the_view_end_is_covered(self):
        cols = covered_columns([(9 * SEC, 10 * SEC)], 0, 10 * SEC, 10)
        self.assertTrue(cols[-1])

    def test_run_ending_exactly_at_a_boundary_does_not_leak_forward(self):
        """A half-open end is not a covered instant."""
        cols = covered_columns([(0, 5 * SEC)], 0, 10 * SEC, 10)
        self.assertTrue(cols[:5].all())
        self.assertFalse(cols[5].any())

    def test_range_entirely_before_all_coverage(self):
        cols = covered_columns([(50 * SEC, 60 * SEC)], 0, 10 * SEC, 20)
        self.assertFalse(cols.any())

    def test_range_entirely_after_all_coverage(self):
        cols = covered_columns([(0, 10 * SEC)], 50 * SEC, 60 * SEC, 20)
        self.assertFalse(cols.any())

    def test_overlapping_runs_are_counted_once_per_column(self):
        """[0,5) and [3,8) together cover [0,8) and leave 8-10 s open.

        A difference array counts columns, not frames, so the overlap between
        the two runs cannot push any column's tally negative or wrap around.
        """
        cols = covered_columns([(0, 5 * SEC), (3 * SEC, 8 * SEC)], 0, 10 * SEC, 20)
        self.assertTrue(cols[:16].all(), "0-8 s is covered")
        self.assertFalse(cols[16:].any(), "8-10 s is not")

    def test_adjacent_runs_cover_continuously(self):
        cols = covered_columns([(0, 5 * SEC), (5 * SEC, 10 * SEC)], 0, 10 * SEC, 20)
        self.assertTrue(cols.all())

    def test_one_microsecond_of_audio_still_marks_its_column(self):
        cols = covered_columns([(SEC, SEC + 1)], 0, 10 * SEC, 100)
        self.assertEqual(cols.sum(), 1)

    def test_large_range_with_many_runs(self):
        """Exercised for the difference-array path, not the correctness path."""
        coverage = [(i * SEC, i * SEC + 500_000) for i in range(5000)]
        cols = covered_columns(coverage, 0, 5000 * SEC, 2000)
        self.assertTrue(cols.all(), "half a second every second fills every column")

    def test_large_sparse_range_with_many_runs(self):
        """A run must be sparser than the column width to leave gaps visible."""
        coverage = [(i * 10 * SEC, i * 10 * SEC + 1000) for i in range(500)]
        cols = covered_columns(coverage, 0, 5000 * SEC, 2000)
        self.assertTrue(cols.any())
        self.assertFalse(cols.all(), "1 ms every 10 s leaves most of it bare")


if __name__ == "__main__":
    unittest.main()