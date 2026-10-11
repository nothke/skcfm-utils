"""Tests for wavesnip.model.

The frame/time arithmetic here is what every snip depends on, so the seam and
clamping cases get explicit attention rather than being left implicit.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from wavesnip.model import (
    Archive,
    Clip,
    TimeBlock,
    TimeBlocksConfig,
    from_us,
    to_us,
    us_now,
)

BASE = datetime(2026, 5, 12, 10, 15, 0)


def make_clip(
    *,
    start: datetime = BASE,
    seconds: float = 10.0,
    framerate: int = 48000,
    nchannels: int = 2,
    sampwidth: int = 2,
    name: str = "a.wav",
) -> Clip:
    start_us = to_us(start)
    nframes = int(seconds * framerate)
    return Clip(
        archive_path=Path("/archive"),
        path=Path("/archive") / name,
        size=nframes * nchannels * sampwidth,
        mtime_us=start_us + int(seconds * 1_000_000),
        start_us=start_us,
        nframes=nframes,
        framerate=framerate,
        nchannels=nchannels,
        sampwidth=sampwidth,
    )


class TestTimeConversion(unittest.TestCase):
    def test_roundtrip(self):
        for moment in (
            BASE,
            datetime(1970, 1, 1),
            datetime(2026, 12, 31, 23, 59, 59, 999999),
            datetime(1999, 6, 15, 12, 0, 0),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(from_us(to_us(moment)), moment)

    def test_to_us_is_an_integer(self):
        self.assertIsInstance(to_us(BASE), int)

    def test_microsecond_precision_survives(self):
        moment = BASE.replace(microsecond=123456)
        self.assertEqual(from_us(to_us(moment)).microsecond, 123456)

    def test_naive_epoch_is_local_not_utc(self):
        """Naive input is local time, so the naive epoch is *not* zero.

        ``1970-01-01 00:00`` read as local time on a UTC+2 machine is
        1969-12-31 22:00 UTC. This is the semantics that makes ``to_us`` agree
        with ``st_mtime``; asserting it explicitly stops anyone "fixing" it
        back into naive wall-clock subtraction.
        """
        offset_us = to_us(datetime(1970, 1, 1))
        self.assertNotEqual(offset_us, 0)
        self.assertEqual(offset_us, round(datetime(1970, 1, 1).timestamp() * 1_000_000))

    def test_utc_aware_input_is_honoured(self):
        from datetime import timezone

        moment = datetime(1970, 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(to_us(moment), 3_600_000_000)

    def test_now_is_plausible(self):
        self.assertIsInstance(us_now(), int)
        self.assertGreater(us_now(), to_us(datetime(2020, 1, 1)))

    def test_agrees_with_filesystem_mtime(self):
        """The two representations must be the same clock.

        ``to_us`` interprets naive input as local time; ``st_mtime`` is a true
        UTC epoch. Subtracting naive datetimes instead of going through
        ``timestamp()`` put every value out by the machine's UTC offset -- 2
        hours here -- and nothing else noticed until the index tests ran.
        """
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as handle:
            stamp = BASE.timestamp()
            os.utime(handle.name, (stamp, stamp))
            from wavesnip.model import mtime_us

            self.assertEqual(mtime_us(os.stat(handle.name)), to_us(BASE))

    def test_realistic_range_roundtrips(self):
        """Round trip holds across the range this app will actually see.

        ``to_us``/``from_us`` route through the platform's ``localtime()``,
        which cannot represent year 1 or year 10000. The earlier
        timedelta-only implementation could, but it was two hours wrong on this
        machine -- timezone correctness is worth far more than the extra range,
        and nothing here ever handles dates that old.
        """
        for moment in (
            datetime(1970, 1, 2),
            datetime(1999, 6, 15, 12, 0, 0),
            BASE,
            datetime(2038, 1, 19, 3, 14, 7),  # 32-bit time_t rollover
            datetime(2100, 12, 31, 23, 59, 59, 999999),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(from_us(to_us(moment)), moment)


class TestClipSpan(unittest.TestCase):
    def test_duration_is_end_minus_start(self):
        clip = make_clip(seconds=3600)
        self.assertEqual(clip.duration_us, 3_600_000_000)
        self.assertEqual(clip.duration, 3600.0)

    def test_start_and_end_properties(self):
        clip = make_clip(start=BASE, seconds=10)
        self.assertEqual(clip.start, BASE)
        self.assertEqual(clip.end, BASE + timedelta(seconds=10))

    def test_end_equals_mtime(self):
        # The whole derivation rests on this: end *is* the file mtime.
        clip = make_clip()
        self.assertEqual(clip.end_us, clip.mtime_us)

    def test_block_align(self):
        clip = make_clip(nchannels=6, sampwidth=4)
        self.assertEqual(clip.block_align, 24)


class TestContains(unittest.TestCase):
    """Half-open [start, end) so a butt-joined seam resolves to one clip."""

    def setUp(self):
        self.clip = make_clip(start=BASE, seconds=10)

    def test_interior(self):
        self.assertTrue(self.clip.contains(to_us(BASE + timedelta(seconds=5))))

    def test_includes_start(self):
        self.assertTrue(self.clip.contains(self.clip.start_us))

    def test_excludes_end(self):
        self.assertFalse(self.clip.contains(self.clip.end_us))

    def test_before_and_after(self):
        self.assertFalse(self.clip.contains(self.clip.start_us - 1))
        self.assertFalse(self.clip.contains(self.clip.end_us + 1))

    def test_butt_joined_seam_belongs_to_exactly_one(self):
        earlier = make_clip(start=BASE, seconds=10, name="a.wav")
        later = make_clip(start=BASE + timedelta(seconds=10), seconds=10, name="b.wav")
        seam = to_us(BASE + timedelta(seconds=10))
        holders = [c for c in (earlier, later) if c.contains(seam)]
        self.assertEqual(len(holders), 1)
        self.assertIs(holders[0], later)


class TestOverlap(unittest.TestCase):
    def test_touching_clips_do_not_overlap(self):
        earlier = make_clip(start=BASE, seconds=10, name="a.wav")
        later = make_clip(start=BASE + timedelta(seconds=10), seconds=10, name="b.wav")
        self.assertFalse(earlier.overlaps(later))
        self.assertFalse(later.overlaps(earlier))

    def test_genuine_overlap_detected(self):
        earlier = make_clip(start=BASE, seconds=10, name="a.wav")
        later = make_clip(start=BASE + timedelta(seconds=5), seconds=10, name="b.wav")
        self.assertTrue(earlier.overlaps(later))
        self.assertTrue(later.overlaps(earlier))

    def test_contained_clip_overlaps(self):
        big = make_clip(start=BASE, seconds=60, name="big.wav")
        small = make_clip(start=BASE + timedelta(seconds=10), seconds=5, name="small.wav")
        self.assertTrue(big.overlaps(small))

    def test_disjoint_clips(self):
        a = make_clip(start=BASE, seconds=10, name="a.wav")
        b = make_clip(start=BASE + timedelta(seconds=60), seconds=10, name="b.wav")
        self.assertFalse(a.overlaps(b))


class TestFrameArithmetic(unittest.TestCase):
    def setUp(self):
        self.clip = make_clip(start=BASE, seconds=10, framerate=48000)

    def test_frame_at_start_is_zero(self):
        self.assertEqual(self.clip.frame_at(self.clip.start_us), 0)

    def test_frame_at_end_is_nframes(self):
        self.assertEqual(self.clip.frame_at(self.clip.end_us), self.clip.nframes)

    def test_frame_at_midpoint(self):
        mid = self.clip.start_us + 5_000_000
        self.assertEqual(self.clip.frame_at(mid), 240_000)

    def test_frame_at_quarter(self):
        quarter = self.clip.start_us + 2_500_000
        self.assertEqual(self.clip.frame_at(quarter), 120_000)

    def test_clamps_outside_the_clip(self):
        self.assertEqual(self.clip.frame_at(self.clip.start_us - 10_000_000), 0)
        self.assertEqual(self.clip.frame_at(self.clip.end_us + 10_000_000), self.clip.nframes)

    def test_frame_at_is_never_out_of_range(self):
        for offset_us in range(-1_000_000, 11_000_000, 250_000):
            frame = self.clip.frame_at(self.clip.start_us + offset_us)
            self.assertGreaterEqual(frame, 0)
            self.assertLessEqual(frame, self.clip.nframes)

    def test_frame_span_full_clip(self):
        self.assertEqual(
            self.clip.frame_span(self.clip.start_us, self.clip.end_us),
            (0, self.clip.nframes),
        )

    def test_frame_span_partial(self):
        start = self.clip.start_us + 2_000_000
        end = self.clip.start_us + 4_000_000
        self.assertEqual(self.clip.frame_span(start, end), (96_000, 192_000))

    def test_frame_span_reversed_is_empty(self):
        span = self.clip.frame_span(self.clip.end_us, self.clip.start_us)
        self.assertEqual(span, (0, 0))

    def test_frame_span_degenerate_is_empty(self):
        mid = self.clip.start_us + 5_000_000
        self.assertEqual(self.clip.frame_span(mid, mid), (0, 0))

    def test_frame_span_sub_frame_is_empty(self):
        # A range shorter than one frame must not yield a 1-frame slice that
        # would duplicate audio on the next piece.
        start = self.clip.start_us + 1_000_000
        self.assertEqual(self.clip.frame_span(start, start + 1), (0, 0))


class TestArchive(unittest.TestCase):
    def test_index_lives_inside_the_archive(self):
        archive = Archive(path=Path("/data/archive"))
        self.assertEqual(archive.index_path, Path("/data/archive/wavesnip-index.sqlite3"))

    def test_priority_defaults_to_zero(self):
        self.assertEqual(Archive(path=Path("/a")).priority, 0)


class TimeBlockFileCase(unittest.TestCase):
    """One temp file per test -- a shared one would let a save become a load."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "time_blocks.json"
        self.config = TimeBlocksConfig(self.path)

    def write(self, entries) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "time_blocks": entries}), encoding="utf-8"
        )

    @staticmethod
    def entry(
        name: str,
        start: str = "12.5.2026. 10:15:01:001",
        end: str = "12.5.2026. 11:15:01:001",
        **extra,
    ) -> dict:
        given = {"name": name, "start_time": start, "end_time": end}
        given.update(extra)
        return given


class ReducedEntryTests(TimeBlockFileCase):
    """`design.md`: name, start_time, end_time are the whole requirement."""

    def test_an_entry_of_only_the_three_required_fields_loads(self):
        self.write([self.entry("Morning show")])

        blocks, problems = self.config.load_with_report()

        self.assertEqual(problems, [])
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertEqual(block.name, "Morning show")
        self.assertEqual(block.start, datetime(2026, 5, 12, 10, 15, 1, 1000))
        self.assertEqual(block.end, datetime(2026, 5, 12, 11, 15, 1, 1000))

    def test_the_absent_parameters_come_back_at_their_defaults(self):
        """Not ``"None"``, not ``0`` written as text: the defaults the editor
        would show for a block nobody has filled in yet."""
        self.write([self.entry("Morning show")])

        block = self.config.load()[0]

        self.assertEqual(block.episode_num, 0)
        self.assertEqual(block.description, "")
        self.assertEqual(block.hosts, [])
        self.assertEqual(block.guests, [])

    def test_the_parameters_that_are_present_are_still_read(self):
        self.write(
            [
                self.entry(
                    "Morning show",
                    episode_num=7,
                    description="Talk and music",
                    hosts=["Alice", "Bob"],
                    guests=["Carol"],
                )
            ]
        )

        block = self.config.load()[0]

        self.assertEqual(block.episode_num, 7)
        self.assertEqual(block.description, "Talk and music")
        self.assertEqual(block.hosts, ["Alice", "Bob"])
        self.assertEqual(block.guests, ["Carol"])

    def test_an_unreadable_optional_parameter_falls_back_rather_than_refusing(self):
        """The optional fields are hand-written, so they are where the typos
        land. ``episode_num: "abc"`` costs the episode number, not the block."""
        self.write([self.entry("Morning show", episode_num="abc", description=42)])

        blocks, problems = self.config.load_with_report()

        self.assertEqual(problems, [])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].episode_num, 0)
        self.assertEqual(blocks[0].description, "")

    def test_a_bare_host_string_is_one_name_not_a_list_of_letters(self):
        self.write([self.entry("Morning show", hosts="Alice")])

        self.assertEqual(self.config.load()[0].hosts, ["Alice"])


class CorruptedEntryTests(TimeBlockFileCase):
    """One bad entry costs one entry, and says so -- see `flow.md`."""

    def test_a_bad_date_costs_only_the_entry_it_belongs_to(self):
        self.write(
            [
                self.entry("Good"),
                self.entry("Broken", start="not a date"),
                self.entry("Also good"),
            ]
        )

        blocks, problems = self.config.load_with_report()

        self.assertEqual([block.name for block in blocks], ["Good", "Also good"])
        self.assertEqual(len(problems), 1)

    def test_the_problem_names_the_file_the_entry_and_what_would_fix_it(self):
        self.write([self.entry("Broken", start="not a date")])

        _, problems = self.config.load_with_report()

        problem = problems[0]
        self.assertIn("time_blocks.json", problem)
        self.assertIn('entry 1 "Broken"', problem)
        self.assertIn("start_time", problem)
        self.assertIn("not a date", problem)

    def test_a_missing_field_is_reported_instead_of_raising(self):
        """The bug this replaced: ``to_us(parse_datetime(""))`` handed ``None``
        to ``to_us``, which raised ``AttributeError`` -- outside every except
        clause the loader had -- and no block at all was loaded."""
        self.write(
            [
                {"name": "No start", "end_time": "12.5.2026. 11:15:01:001"},
                {"name": "No end", "start_time": "12.5.2026. 10:15:01:001"},
                {"start_time": "12.5.2026. 10:15:01:001", "end_time": "12.5.2026. 11:15:01:001"},
            ]
        )

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertEqual(len(problems), 3)
        self.assertIn("has no start_time", problems[0])
        self.assertIn("has no end_time", problems[1])
        self.assertIn("has no name", problems[2])

    def test_an_impossible_date_is_reported(self):
        """Well-shaped, unbuildable: 32 January. ``parse_datetime`` returns
        ``None`` rather than guessing at what was meant."""
        self.write([self.entry("Broken", start="32.1.2026. 10:00:00")])

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertIn("32.1.2026. 10:00:00", problems[0])

    def test_a_date_no_local_clock_can_reach_is_reported(self):
        """``to_us`` goes through ``localtime()``, which stops at year 1 and
        year 10000 -- and ``ValueError`` out of it took the whole load."""
        self.write([self.entry("Broken", start="0001-01-01 00:00:00")])

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertIn("outside the supported range", problems[0])

    def test_an_entry_that_is_not_an_object_is_reported(self):
        self.write(["just a string", self.entry("Good")])

        blocks, problems = self.config.load_with_report()

        self.assertEqual([block.name for block in blocks], ["Good"])
        self.assertIn("not an object", problems[0])


class TimeBlockFileProblemsTests(TimeBlockFileCase):
    """File-level failures are said too, rather than an empty list pretending
    everything is fine."""

    def test_a_missing_file_is_not_a_problem(self):
        """Every first run has one. Reporting it would greet every new user
        with an error about a file they have never had."""
        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertEqual(problems, [])

    def test_invalid_json_says_so(self):
        self.path.write_text("{not json", encoding="utf-8")

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("not valid JSON", problems[0])

    def test_a_directory_where_the_file_should_be_is_said(self):
        self.path.mkdir(parents=True)

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("cannot read it", problems[0])

    def test_the_wrong_document_shape_is_said(self):
        self.path.write_text(json.dumps({"time_blocks": {"name": "x"}}), encoding="utf-8")

        blocks, problems = self.config.load_with_report()

        self.assertEqual(blocks, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("time_blocks", problems[0])

    def test_load_is_the_quiet_twin_of_load_with_report(self):
        """Both entry points must agree, or the caller that reports and the
        caller that does not would load different files."""
        self.write([self.entry("Good"), self.entry("Broken", start="nope")])

        self.assertEqual(
            [block.name for block in self.config.load()],
            [block.name for block in self.config.load_with_report()[0]],
        )


class TimeBlockSaveTests(TimeBlockFileCase):
    """The json does not need everything: empty parameters are not written."""

    def bare(self) -> TimeBlock:
        return TimeBlock(
            name="Morning show",
            start_us=to_us(datetime(2026, 5, 12, 10, 15, 1, 1000)),
            end_us=to_us(datetime(2026, 5, 12, 11, 15, 1, 1000)),
            episode_num=0,
            description="",
            hosts=[],
            guests=[],
            uuid="test-uuid-12345678",
        )

    def test_a_block_with_nothing_filled_in_writes_three_lines(self):
        self.config.save([self.bare()])

        entry = json.loads(self.path.read_text(encoding="utf-8"))["time_blocks"][0]

        self.assertEqual(
            entry,
            {
                "name": "Morning show",
                "start_time": "12.5.2026. 10:15:01:001",
                "end_time": "12.5.2026. 11:15:01:001",
                "uuid": "test-uuid-12345678",
            },
        )

    def test_a_block_with_something_filled_in_keeps_it(self):
        block = TimeBlock(
            name=self.bare().name,
            start_us=self.bare().start_us,
            end_us=self.bare().end_us,
            episode_num=7,
            description="Talk and music",
            hosts=["Alice"],
            guests=[],
        )

        self.config.save([block])

        entry = json.loads(self.path.read_text(encoding="utf-8"))["time_blocks"][0]
        self.assertEqual(entry["episode_num"], 7)
        self.assertEqual(entry["description"], "Talk and music")
        self.assertEqual(entry["hosts"], ["Alice"])
        self.assertNotIn("guests", entry)

    def test_a_saved_block_reads_back_as_it_was_written(self):
        """The point of dropping the empty parameters: the next load must not
        notice they are gone."""
        filled = TimeBlock(
            name="Evening show",
            start_us=to_us(datetime(2026, 5, 13, 20, 0, 0)),
            end_us=to_us(datetime(2026, 5, 13, 22, 0, 0)),
            episode_num=12,
            description="Late",
            hosts=["Alice", "Bob"],
            guests=["Carol"],
            uuid="test-uuid-87654321",
        )
        self.config.save([self.bare(), filled])

        blocks, problems = self.config.load_with_report()

        self.assertEqual(problems, [])
        self.assertEqual(blocks, [self.bare(), filled])

    def test_saving_is_still_atomic(self):
        self.config.save([self.bare()])

        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".timeblocks-")]

        self.assertEqual(leftovers, [])
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
