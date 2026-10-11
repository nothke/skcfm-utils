"""Tests for wavesnip.timeline.

The interesting properties here are invariants that are easy to state and easy
to break:

* adjacent pieces in a snip plan never overlap in time, so no sample is
  exported twice -- including when chunks themselves overlap;
* a snip crossing a gap exports the covered audio and reports the hole rather
  than failing or padding with silence;
* per-instant resolution silently picks one archive instead of asking.

Fixtures use real WAV files with controlled mtimes, built on the same 8 Hz /
10 s base as ``test_indexer`` so timelines stay frame-representable and tiny.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from wavesnip import wavio
from wavesnip.indexer import ArchiveIndex
from wavesnip.model import Archive, to_us
from wavesnip.timeline import Gap, Timeline, merge_intervals

RATE = 8  # frames per second
SEC = 10
BASE = datetime(2026, 5, 12, 10, 15, 0)


def at(seconds: float) -> int:
    """Timeline instant ``seconds`` away from BASE."""
    return to_us(BASE + timedelta(seconds=seconds))


class MergeIntervalsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])

    def test_disjoint_are_kept(self):
        self.assertEqual(merge_intervals([(0, 10), (20, 30)]), [(0, 10), (20, 30)])

    def test_touching_intervals_merge(self):
        """A butt-joined seam is coverage, not a gap."""
        self.assertEqual(merge_intervals([(0, 10), (10, 20)]), [(0, 20)])

    def test_overlapping_intervals_merge(self):
        self.assertEqual(merge_intervals([(0, 15), (10, 20)]), [(0, 20)])

    def test_contained_interval_is_absorbed(self):
        self.assertEqual(merge_intervals([(0, 100), (10, 20)]), [(0, 100)])

    def test_cascading_merge(self):
        self.assertEqual(
            merge_intervals([(0, 5), (5, 10), (20, 30), (10, 20)]),
            [(0, 30)],
        )

    def test_unsorted_input(self):
        self.assertEqual(
            merge_intervals([(20, 30), (0, 10), (5, 25)]), [(0, 30)]
        )

    def test_empty_intervals_dropped(self):
        self.assertEqual(merge_intervals([(10, 10), (0, 5), (7, 7)]), [(0, 5)])


class TimelineCase(unittest.TestCase):
    """Builds one or more archives out of real WAV files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.archives: list[Archive] = []
        self._cursor = 0.0

    def make_archive(self, name: str, priority: int = 0) -> Archive:
        folder = self.root / name
        folder.mkdir()
        archive = Archive(path=folder, priority=priority)
        ArchiveIndex(archive).scan()  # create the DB up front
        self.archives.append(archive)
        return archive

    def add_clip(
        self,
        archive: Archive,
        name: str,
        *,
        start: float | None = None,
        seconds: int = SEC,
        framerate: int = RATE,
        nchannels: int = 1,
        sampwidth: int = 2,
    ) -> Path:
        """Write a WAV covering ``[start, start + seconds)``.

        ``start`` defaults to 0, so successive clips with the default length
        butt-join exactly -- the normal case for a chunked recorder.
        """
        if start is None:
            start = self._cursor
        self._cursor = start + seconds

        rng = np.random.default_rng(abs(hash(f"{archive.path}{name}")) % (2**31))
        scale = 2 ** (8 * sampwidth - 1)
        ints = rng.integers(
            -(scale // 2), scale // 2, size=(framerate * seconds, nchannels)
        )
        samples = (ints / scale).astype(np.float32)

        path = archive.path / name
        wavio.write_wav(path, samples, framerate=framerate, sampwidth=sampwidth)
        ends_at = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (ends_at, ends_at))
        return path

    def timeline(self) -> Timeline:
        for archive in self.archives:
            ArchiveIndex(archive).scan()
        return Timeline(self.archives).refresh()


class ResolveTests(TimelineCase):
    def test_resolves_to_the_covering_clip(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC)

        resolution = self.timeline().resolve(at(SEC + 2))
        self.assertFalse(resolution.is_gap)
        self.assertEqual(resolution.clip.path.name, "c2.wav")
        self.assertEqual(resolution.offset_us, 2_000_000)

    def test_start_is_inside_and_end_is_outside(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0, seconds=SEC)

        timeline = self.timeline()
        self.assertEqual(timeline.resolve(at(0)).clip.path.name, "c1.wav")
        self.assertTrue(timeline.resolve(at(SEC - 0.001)).is_covered)
        self.assertTrue(timeline.resolve(at(SEC)).is_gap)

    def test_gap_when_nothing_covers(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)

        timeline = self.timeline()
        self.assertTrue(timeline.resolve(at(SEC * 2)).is_gap)
        self.assertIsNone(timeline.resolve(at(SEC * 2)).clip)

    def test_lower_priority_archive_fills_a_gap(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0, seconds=SEC * 2)

        resolution = self.timeline().resolve(at(SEC + 1))
        self.assertEqual(resolution.clip.archive_path, second.path)

    def test_highest_priority_archive_wins_without_asking(self):
        """Two archives, same coverage, no dialog.

        The archives hold the same stream, so there is nothing to decide. The
        resolution is silent and total.
        """
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0)

        resolution = self.timeline().resolve(at(1))
        self.assertEqual(resolution.clip.archive_path, first.path)
        self.assertTrue(resolution.is_covered)

    def test_priority_order_beats_insertion_order(self):
        """Priority, not the order archives were added, decides."""
        first = self.make_archive("first", priority=5)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0)

        resolution = self.timeline().resolve(at(1))
        self.assertEqual(resolution.clip.archive_path, second.path)

    def test_overlapping_chunks_prefer_most_forward_headroom(self):
        """Within one archive, pick the clip that reaches furthest ahead.

        Two chunks that overlap are near-identical, so this is about not
        hopping to a clip that is about to end -- not about correctness of
        content.
        """
        a = self.make_archive("a")
        self.add_clip(a, "short.wav", start=0, seconds=SEC)
        self.add_clip(a, "long.wav", start=SEC - 2, seconds=SEC * 2)

        resolution = self.timeline().resolve(at(SEC))
        self.assertEqual(resolution.clip.path.name, "long.wav")

    def test_empty_archive_resolves_to_gap(self):
        self.make_archive("empty")
        self.assertTrue(self.timeline().resolve(at(0)).is_gap)

    def test_instant_property_round_trips(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.assertEqual(self.timeline().resolve(at(0)).instant, BASE)


class NearestCoveredTests(TimelineCase):
    def test_already_covered_returns_itself(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.assertEqual(self.timeline().nearest_covered(at(3)), at(3))

    def test_jumps_forward_to_the_next_run(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)
        # 25 s: 5 s short of the next run, 15 s past the end of the last one.
        self.assertEqual(self.timeline().nearest_covered(at(SEC * 2.5)), at(SEC * 3))

    def test_jumps_forward_across_a_long_gap(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 5)
        # 20 s, 40 s short of the next run. Gaps are exited forwards.
        self.assertEqual(self.timeline().nearest_covered(at(SEC * 2)), at(SEC * 5))

    def test_jumped_to_instant_is_always_covered(self):
        """The bug this pins: a run's exclusive end is not covered."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 5)
        timeline = self.timeline()

        for probe in range(SEC, SEC * 5):
            target = timeline.nearest_covered(at(probe))
            self.assertIsNotNone(target)
            self.assertTrue(
                timeline.resolve(target).is_covered,
                f"nearest_covered({probe}s) landed on uncovered {target}",
            )

    def test_gaps_are_always_exited_forwards(self):
        """Not to the nearer edge -- that direction is arbitrary and can ping-pong.

        Jumping back out of a gap near its start means replaying a sliver of the
        previous clip, running out of it, and jumping forward again.
        """
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)
        timeline = self.timeline()

        # 11 s: only 1 s past the previous run's end, but still forwards.
        self.assertEqual(timeline.nearest_covered(at(SEC + 1)), at(SEC * 3))
        # 29 s: 1 s short of the next run, still forwards.
        self.assertEqual(timeline.nearest_covered(at(SEC * 3 - 1)), at(SEC * 3))
        # Dead centre, forwards.
        self.assertEqual(timeline.nearest_covered(at(SEC * 2)), at(SEC * 3))

    def test_before_the_first_run(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=SEC * 4)
        self.assertEqual(self.timeline().nearest_covered(at(0)), at(SEC * 4))

    def test_after_the_last_run(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.assertEqual(self.timeline().nearest_covered(at(SEC * 9)), at(SEC) - 1)

    def test_no_coverage_at_all(self):
        self.make_archive("empty")
        self.assertIsNone(self.timeline().nearest_covered(at(0)))

    def test_joins_across_archives(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0, seconds=SEC)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=SEC)
        self.assertEqual(self.timeline().nearest_covered(at(SEC)), at(SEC))  # covered

    def test_nearest_covered_before_first_run_is_the_first_instant(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=SEC * 4)
        self.assertEqual(self.timeline().nearest_covered(at(0)), at(SEC * 4))


class CoverageAndGapTests(TimelineCase):
    def test_butt_joined_chunks_are_one_run(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC)
        self.add_clip(a, "c3.wav", start=SEC * 2)

        timeline = self.timeline()
        self.assertEqual(timeline.coverage, [(at(0), at(SEC * 3))])
        self.assertEqual(timeline.gaps(), [])

    def test_gap_is_detected_between_runs(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 4)

        gaps = self.timeline().gaps()
        self.assertEqual([(g.start_us, g.end_us) for g in gaps], [(at(SEC), at(SEC * 4))])
        self.assertEqual(gaps[0].duration, SEC * 3)

    def test_a_second_archive_closes_the_gap(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "hole.wav", start=SEC)

        timeline = self.timeline()
        self.assertEqual(timeline.coverage, [(at(0), at(SEC * 2))])
        self.assertEqual(timeline.gaps(), [])

    def test_no_gaps_when_archive_is_empty(self):
        self.make_archive("empty")
        self.assertEqual(self.timeline().gaps(), [])

    def test_clips_property_spans_all_archives(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=SEC)

        clips = self.timeline().clips
        self.assertEqual(len(clips), 2)
        self.assertEqual([c.start_us for c in clips], sorted(c.start_us for c in clips))


class PlanSnipTests(TimelineCase):
    def test_inverted_range_yields_nothing(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)

        plan = self.timeline().plan_snip(at(SEC), at(0))
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.requested, (at(SEC), at(0)))

    def test_single_clip(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0, seconds=SEC * 2)

        plan = self.timeline().plan_snip(at(2), at(SEC * 2 - 2))
        self.assertEqual(len(plan.pieces), 1)
        self.assertEqual(plan.pieces[0].start_frame, 2 * RATE)
        self.assertEqual(plan.pieces[0].nframes, (SEC * 2 - 4) * RATE)
        self.assertEqual(plan.nframes, (SEC * 2 - 4) * RATE)
        self.assertFalse(plan.has_gaps)
        self.assertAlmostEqual(plan.duration, SEC * 2 - 4)

    def test_spanning_two_chunks(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC)

        plan = self.timeline().plan_snip(at(SEC - 2), at(SEC + 2))
        self.assertEqual([p.clip.path.name for p in plan.pieces], ["c1.wav", "c2.wav"])
        # Each piece starts where the previous ended -- in time, not just in order.
        self.assertEqual(plan.pieces[0].end_us, plan.pieces[1].start_us)
        self.assertAlmostEqual(plan.duration, 4)

    def test_pieces_never_overlap_in_time(self):
        """The core invariant, checked on a deliberately messy archive."""
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0, seconds=SEC)
        self.add_clip(a, "c2.wav", start=SEC - 3, seconds=SEC)      # overlaps c1
        self.add_clip(a, "c3.wav", start=SEC * 2 - 1, seconds=SEC)  # overlaps c2
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c1.wav", start=0, seconds=SEC * 3)    # spans it all

        plan = self.timeline().plan_snip(at(0), at(SEC * 3))
        for earlier, later in zip(plan.pieces, plan.pieces[1:]):
            self.assertLessEqual(
                earlier.end_us, later.start_us,
                f"{earlier.clip.path.name} overlaps {later.clip.path.name} in time",
            )
        self.assertFalse(plan.has_gaps)
        self.assertEqual(plan.covered_us, SEC * 3 * 1_000_000)

    def test_gap_inside_the_snip_is_reported_not_filled(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)

        plan = self.timeline().plan_snip(at(0), at(SEC * 4))
        self.assertEqual([p.clip.path.name for p in plan.pieces], ["c1.wav", "c2.wav"])
        self.assertEqual([(g.start_us, g.end_us) for g in plan.gaps],
                         [(at(SEC), at(SEC * 3))])
        self.assertTrue(plan.has_gaps)
        self.assertAlmostEqual(plan.duration, SEC * 2)
        self.assertAlmostEqual(plan.missing_us / 1_000_000, SEC * 2)

    def test_snip_entirely_inside_a_gap(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 4)

        plan = self.timeline().plan_snip(at(SEC), at(SEC * 3))
        self.assertTrue(plan.is_empty)
        self.assertEqual(len(plan.gaps), 1)
        self.assertAlmostEqual(plan.missing_us / 1_000_000, SEC * 2)

    def test_ranges_bleeding_off_both_ends_are_clipped(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0, seconds=SEC)

        plan = self.timeline().plan_snip(at(-SEC * 5), at(SEC * 5))
        self.assertEqual(len(plan.pieces), 1)
        self.assertEqual(plan.pieces[0].start_frame, 0)
        self.assertEqual(plan.pieces[0].end_frame, SEC * RATE)

    def test_contiguous_pieces_from_one_clip_are_merged(self):
        """Only one read per clip, so export does not reopen the same file."""
        a = self.make_archive("a")
        self.add_clip(a, "long.wav", start=0, seconds=SEC * 4)
        b = self.make_archive("b", priority=1)
        # A boundary in a lower-priority archive must not split the winning one.
        self.add_clip(b, "split.wav", start=SEC, seconds=SEC)

        plan = self.timeline().plan_snip(at(0), at(SEC * 4))
        self.assertEqual(len(plan.pieces), 1)
        self.assertEqual(plan.pieces[0].clip.path.name, "long.wav")

    def test_mixes_archives_across_a_priority_gap(self):
        first = self.make_archive("first", priority=0)
        self.add_clip(first, "c1.wav", start=0)
        second = self.make_archive("second", priority=1)
        self.add_clip(second, "c2.wav", start=SEC)

        plan = self.timeline().plan_snip(at(0), at(SEC * 2))
        self.assertEqual([p.clip.archive_path for p in plan.pieces],
                         [first.path, second.path])
        self.assertEqual(plan.archives_used, [first.path, second.path])

    def test_formats_and_resampling_need(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0, framerate=RATE)
        self.add_clip(a, "c2.wav", start=SEC, framerate=RATE * 2)

        plan = self.timeline().plan_snip(at(0), at(SEC * 2))
        self.assertEqual(plan.formats, [(RATE, 1, 2), (RATE * 2, 1, 2)])
        self.assertTrue(plan.needs_resample)

    def test_matching_formats_need_no_resample(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC, sampwidth=3)

        plan = self.timeline().plan_snip(at(0), at(SEC * 2))
        self.assertFalse(plan.needs_resample, "bit depth alone is not a rate mismatch")

    def test_sub_frame_range_yields_no_frames(self):
        """A range finer than one sample frame cannot produce audio.

        ``frame_span`` rounds both ends independently, so a 1 us range at 8 Hz
        collapses to zero frames. The plan reports nothing rather than silently
        widening the snip to a whole frame the user did not ask for. Unreachable
        from the UI, where times are entered to the second.
        """
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)

        plan = self.timeline().plan_snip(at(0), at(0) + 1)
        self.assertEqual(plan.pieces, ())
        self.assertFalse(plan.has_gaps, "covered, just too short to emit a frame")

    def test_single_frame_range_is_kept(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)

        plan = self.timeline().plan_snip(at(0), at(0.5))  # 0.5 s == 4 frames at 8 Hz
        self.assertEqual(len(plan.pieces), 1)
        self.assertEqual(plan.nframes, 4)
        self.assertEqual(plan.covered_us, 500_000)

    def test_gap_reports_readable_datetimes(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        self.add_clip(a, "c2.wav", start=SEC * 3)

        gap = self.timeline().plan_snip(at(0), at(SEC * 4)).gaps[0]
        self.assertEqual(gap.start, BASE + timedelta(seconds=SEC))
        self.assertEqual(gap.end, BASE + timedelta(seconds=SEC * 3))


class RefreshTests(TimelineCase):
    def test_refresh_picks_up_a_new_clip(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)

        timeline = self.timeline()
        self.assertEqual(len(timeline.clips), 1)

        self.add_clip(a, "c2.wav", start=SEC)
        ArchiveIndex(a).scan()
        timeline.refresh()
        self.assertEqual(len(timeline.clips), 2)

    def test_queries_load_lazily(self):
        a = self.make_archive("a")
        self.add_clip(a, "c1.wav", start=0)
        ArchiveIndex(a).scan()

        timeline = Timeline([a])
        self.assertFalse(timeline._loaded)
        timeline.resolve(at(0))
        self.assertTrue(timeline._loaded)


class UnreadableArchiveTests(TimelineCase):
    """A broken archive must not take the working ones down with it.

    Archives are folders on disk, which means they can be mistyped on a command
    line, on a share that is not mounted, or not there any more. When that
    happened the failure escaped as sqlite3's ``unable to open database file``,
    which killed startup outright when the bad folder came from ``argv``.
    """

    def make_broken(self, name: str) -> Archive:
        """An Archive whose folder is not usable, without registering it."""
        return Archive(path=self.root / name)

    def test_a_missing_folder_is_reported_not_raised(self):
        timeline = Timeline([self.make_broken("typo")]).refresh()
        self.assertEqual(timeline.clips, [])
        self.assertEqual(len(timeline.failures), 1)
        self.assertIn("not found", timeline.failures[0])

    def test_a_file_where_a_folder_should_be_is_reported(self):
        not_a_folder = self.root / "a-file"
        not_a_folder.write_text("this is a file")

        timeline = Timeline([Archive(path=not_a_folder)]).refresh()
        self.assertEqual(len(timeline.failures), 1)
        self.assertIn("not a folder", timeline.failures[0])

    def test_the_working_archives_still_load(self):
        """The whole point: one bad path must not empty the timeline."""
        good = self.make_archive("good")
        self.add_clip(good, "c1.wav", start=0)
        ArchiveIndex(good).scan()

        timeline = Timeline([good, self.make_broken("typo")]).refresh()
        self.assertEqual(len(timeline.clips), 1)
        self.assertEqual(len(timeline.failures), 1)

    def test_an_unscanned_folder_is_empty_not_broken(self):
        """Scanning has not happened yet is a normal state, not an error."""
        folder = self.root / "unscanned"
        folder.mkdir()

        timeline = Timeline([Archive(path=folder)]).refresh()
        self.assertEqual(timeline.clips, [])
        self.assertEqual(timeline.failures, [])

    def test_the_message_names_the_folder_without_saying_it_twice(self):
        timeline = Timeline([self.make_broken("typo")]).refresh()
        message = timeline.failures[0]
        self.assertIn("typo", message)
        self.assertEqual(message.count("typo"), 1)

    def test_failures_load_lazily_like_every_other_query(self):
        """Asking about failures is itself enough to trigger the read.

        Same contract as ``clips`` and ``coverage``: nothing works before the
        first query, and the first query does the work. The alternative -- an
        empty list until something else happened to refresh -- would report "no
        problems" for an archive that was never even looked at.
        """
        timeline = Timeline([self.make_broken("typo")])
        self.assertFalse(timeline._loaded)

        self.assertEqual(len(timeline.failures), 1)
        self.assertTrue(timeline._loaded)


class GapModelTests(unittest.TestCase):
    def test_duration(self):
        gap = Gap(at(0), at(5))
        self.assertEqual(gap.duration_us, 5_000_000)
        self.assertAlmostEqual(gap.duration, 5.0)


if __name__ == "__main__":
    unittest.main()