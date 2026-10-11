"""Tests for wavesnip.resample.

Format reconciliation is tested against real WAV files read back through
``wavio``, so a mistake in the conversion shows up as wrong samples rather than
as a wrong-shaped array.

Fixtures use a low rate (8 Hz) where possible so files stay tiny. Where the
point of the test is the rate *ratio*, higher rates are used deliberately: 8 Hz
-> 16 Hz shares a factor of 8 and barely exercises the filter.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from wavesnip import wavio
from wavesnip.indexer import ArchiveIndex
from wavesnip.model import Archive, to_us
from wavesnip.resample import (
    AudioFormat,
    ExportError,
    FormatUnsupported,
    canonical_format,
    convert_channels,
    rate_ratio,
    render_snip,
)
from wavesnip.timeline import Timeline

RATE = 8
SEC = 10
BASE = datetime(2026, 5, 12, 10, 15, 0)


def at(seconds: float) -> int:
    return to_us(BASE + timedelta(seconds=seconds))


def tone(
    seconds: float,
    framerate: int,
    nchannels: int = 1,
    phase: float = 0.0,
    freq: float = 1.0,
) -> np.ndarray:
    """A sine, easy to reason about after conversion.

    ``freq`` defaults to 1 Hz because it is cheap at any of the rates used here.
    Pass an explicit frequency when the test is about frequency content.
    """
    nframes = int(round(seconds * framerate))
    t = np.arange(nframes, dtype=np.float32) / framerate
    wave_data = 0.5 * np.sin(2 * np.pi * freq * t + phase, dtype=np.float32)
    if nchannels == 1:
        return wave_data.reshape(-1, 1)
    return np.repeat(wave_data.reshape(-1, 1), nchannels, axis=1)


class RateRatioTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(rate_ratio(48000, 48000), (1, 1))

    def test_reduced_by_gcd(self):
        """A short filter, not a 48000-tap one."""
        self.assertEqual(rate_ratio(48000, 44100), (147, 160))

    def test_simple_upscale(self):
        self.assertEqual(rate_ratio(8000, 48000), (6, 1))

    def test_downscale(self):
        self.assertEqual(rate_ratio(48000, 8000), (1, 6))

    def test_rejects_non_positive(self):
        with self.assertRaises(ValueError):
            rate_ratio(0, 48000)

    def test_ratio_is_always_reduced(self):
        """A short filter, whatever the rates."""
        for source, target in ((8000, 11025), (44100, 48000), (48000, 96000)):
            up, down = rate_ratio(source, target)
            self.assertEqual(up * source, down * target)
            self.assertEqual(math.gcd(up, down), 1)


class ConvertChannelsTests(unittest.TestCase):
    def test_same_count_passes_through(self):
        x = tone(1, RATE, nchannels=2)
        self.assertEqual(convert_channels(x, 2).shape, x.shape)

    def test_stereo_to_mono_is_the_mean(self):
        x = np.array([[0.0, 1.0], [0.25, -0.25]], dtype=np.float32)
        out = convert_channels(x, 1)
        self.assertEqual(out.shape, (2, 1))
        np.testing.assert_allclose(out[:, 0], [0.5, 0.0])

    def test_mono_to_stereo_duplicates(self):
        x = tone(1, RATE)
        self.assertEqual(x.shape, (RATE, 1))
        out = convert_channels(x, 2)
        self.assertEqual(out.shape, (RATE, 2))
        np.testing.assert_array_equal(out[:, 0], out[:, 1])

    def test_mono_to_six_channels(self):
        out = convert_channels(tone(1, RATE), 6)
        self.assertEqual(out.shape, (RATE, 6))

    def test_refuses_multi_channel_to_multi_channel(self):
        """Mean-across-surround cancels to silence, so this must not happen."""
        six = tone(1, RATE, nchannels=6)
        with self.assertRaises(FormatUnsupported):
            convert_channels(six, 2)

    def test_refusal_explains_why(self):
        with self.assertRaises(FormatUnsupported) as ctx:
            convert_channels(tone(1, RATE, nchannels=6), 2)
        self.assertIn("6 channels", str(ctx.exception))

    def test_six_to_mono_is_allowed(self):
        """User asked for mono, so degrading is their call, not ours."""
        out = convert_channels(tone(1, RATE, nchannels=6), 1)
        self.assertEqual(out.shape, (RATE, 1))

    def test_accepts_1d_input(self):
        out = convert_channels(np.zeros(10, dtype=np.float32), 2)
        self.assertEqual(out.shape, (10, 2))


class AudioFormatTests(unittest.TestCase):
    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            AudioFormat(0, 1, 2)
        with self.assertRaises(ValueError):
            AudioFormat(48000, 0, 2)
        with self.assertRaises(ValueError):
            AudioFormat(48000, 1, 5)

    def test_readable_description(self):
        self.assertEqual(str(AudioFormat(48000, 2, 2)), "48000 Hz, 2 ch, 16-bit")


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.archive = Archive(path=self.root / "archive")
        self.archive.path.mkdir()
        ArchiveIndex(self.archive).scan()
        self.out = self.root / "out.wav"

    def add_clip(
        self,
        name: str,
        *,
        start: float,
        seconds: float = SEC,
        framerate: int = RATE,
        nchannels: int = 1,
        sampwidth: int = 2,
        phase: float = 0.0,
        freq: float = 1.0,
    ) -> None:
        """Write a tone covering ``[start, start + seconds)``."""
        samples = tone(seconds, framerate, nchannels, phase, freq)
        path = self.archive.path / name
        wavio.write_wav(path, samples, framerate=framerate, sampwidth=sampwidth)
        stamp = (BASE + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (stamp, stamp))

    def timeline(self) -> Timeline:
        ArchiveIndex(self.archive).scan()
        return Timeline([self.archive]).refresh()


class CanonicalFormatTests(ExportCase):
    def test_defaults_to_the_first_piece(self):
        """The first piece's format, even though the second disagrees."""
        self.add_clip("a.wav", start=0, framerate=8000)
        self.add_clip("b.wav", start=SEC, framerate=44100)
        plan = self.timeline().plan_snip(at(0), at(SEC * 2))

        self.assertEqual(canonical_format(plan), AudioFormat(8000, 1, 2))

    def test_override_wins(self):
        self.add_clip("a.wav", start=0)
        plan = self.timeline().plan_snip(at(0), at(SEC))
        self.assertEqual(canonical_format(plan, AudioFormat(44100, 2, 4)),
                         AudioFormat(44100, 2, 4))

    def test_empty_plan_has_no_canonical_format(self):
        self.add_clip("a.wav", start=0)
        plan = self.timeline().plan_snip(at(SEC * 4), at(SEC * 5))
        with self.assertRaises(ExportError):
            canonical_format(plan)

    def test_empty_plan_can_still_be_forced(self):
        self.add_clip("a.wav", start=0)
        plan = self.timeline().plan_snip(at(SEC * 4), at(SEC * 5))
        self.assertEqual(canonical_format(plan, AudioFormat(8000, 1, 2)).framerate, 8000)


class RenderBasicTests(ExportCase):
    def test_single_piece_length_and_format(self):
        self.add_clip("a.wav", start=0, seconds=4, framerate=8000)
        plan = self.timeline().plan_snip(at(1), at(3))

        result = render_snip(plan, self.out)
        self.assertEqual(result.nframes, 2 * 8000)
        self.assertEqual(result.fmt, AudioFormat(8000, 1, 2))
        self.assertAlmostEqual(result.duration, 2.0)

        info = wavio.read_info(self.out)
        self.assertEqual((info.framerate, info.nchannels, info.sampwidth), (8000, 1, 2))
        self.assertEqual(info.nframes, 2 * 8000)

    def test_samples_survive_the_round_trip(self):
        """A tone in must be the same tone out, offset included.

        The snip starts a quarter second into the clip, so the expected signal is
        the tone evaluated from t=0.25 s -- comparing against a tone starting at
        t=0 would be comparing against the wrong thing.
        """
        offset = 0.25
        self.add_clip("a.wav", start=0, seconds=2, framerate=8000)
        plan = self.timeline().plan_snip(at(offset), at(offset + 1))

        render_snip(plan, self.out)
        out = wavio.read_frames(self.out, 0, 8000)
        expected = tone(1.0, 8000, phase=2 * np.pi * offset)
        # 16-bit quantisation is the only loss.
        np.testing.assert_allclose(out[:, 0], expected[:, 0], atol=1 / 32768)

    def test_spanning_two_pieces_is_contiguous(self):
        self.add_clip("a.wav", start=0, seconds=SEC, framerate=8000)
        self.add_clip("b.wav", start=SEC, seconds=SEC, framerate=8000,
                      phase=0.5)

        plan = self.timeline().plan_snip(at(SEC - 2), at(SEC + 2))
        self.assertEqual(len(plan.pieces), 2)

        result = render_snip(plan, self.out)
        self.assertEqual(result.nframes, 4 * 8000)

    def test_snippet_of_one_frame(self):
        self.add_clip("a.wav", start=0, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(0.001))  # 8 frames at 8 kHz
        self.assertEqual(render_snip(plan, self.out).nframes, 8)

    def test_empty_plan_is_refused(self):
        self.add_clip("a.wav", start=0)
        plan = self.timeline().plan_snip(at(SEC * 4), at(SEC * 5))
        with self.assertRaises(ExportError):
            render_snip(plan, self.out)

    def test_nothing_is_written_when_refused(self):
        """A refused export must not leave a bogus file behind."""
        self.add_clip("a.wav", start=0)
        plan = self.timeline().plan_snip(at(SEC * 4), at(SEC * 5))
        with self.assertRaises(ExportError):
            render_snip(plan, self.out)
        self.assertFalse(self.out.exists())


class RenderConversionTests(ExportCase):
    def test_rate_conversion_changes_length_proportionally(self):
        self.add_clip("a.wav", start=0, seconds=2, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(2))

        result = render_snip(plan, self.out, target=AudioFormat(16000, 1, 2))
        self.assertEqual(result.fmt.framerate, 16000)
        self.assertAlmostEqual(result.nframes / 16000, 2.0, places=3)

    def test_rate_conversion_preserves_a_tone(self):
        """The point of resampling: 200 Hz in, 200 Hz out.

        Measured by FFT peak rather than sample-for-sample comparison, because
        resampling *should* change the samples -- only the frequency survives.
        """
        self.add_clip("a.wav", start=0, seconds=2, framerate=8000, freq=200.0)
        plan = self.timeline().plan_snip(at(0), at(2))

        render_snip(plan, self.out, target=AudioFormat(16000, 1, 2))
        out = wavio.read_frames(self.out, 0, 32000)[:, 0]

        # Skip the filter's edge transient, then check frequency content.
        middle = out[8000:24000]
        spectrum = np.abs(np.fft.rfft(middle))
        peak_hz = np.fft.rfftfreq(middle.size, 1 / 16000)[np.argmax(spectrum)]
        self.assertAlmostEqual(peak_hz, 200.0, delta=2.0)

    def test_rate_conversion_preserves_amplitude(self):
        """Upsampling must not change the level."""
        self.add_clip("a.wav", start=0, seconds=2, framerate=8000, freq=200.0)
        plan = self.timeline().plan_snip(at(0), at(2))

        render_snip(plan, self.out, target=AudioFormat(16000, 1, 2))
        out = wavio.read_frames(self.out, 0, 32000)[:, 0]
        self.assertAlmostEqual(np.abs(out[8000:24000]).max(), 0.5, delta=0.01)

    def test_upsampling_does_not_invent_a_silent_channel(self):
        self.add_clip("a.wav", start=0, seconds=1, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(1))
        result = render_snip(plan, self.out, target=AudioFormat(16000, 2, 2))
        self.assertEqual(result.fmt.nchannels, 2)
        self.assertGreater(np.abs(wavio.read_frames(self.out, 0, 100)[:, 1]).max(), 0.01)

    def test_stereo_to_mono(self):
        self.add_clip("a.wav", start=0, seconds=1, framerate=8000, nchannels=2)
        plan = self.timeline().plan_snip(at(0), at(1))
        result = render_snip(plan, self.out, target=AudioFormat(8000, 1, 2))
        self.assertEqual(result.fmt.nchannels, 1)
        self.assertEqual(wavio.read_info(self.out).nchannels, 1)

    def test_bit_depth_alone_needs_no_arithmetic(self):
        """16-bit and 24-bit merge, with the output at the requested depth."""
        self.add_clip("a.wav", start=0, seconds=SEC, framerate=8000, sampwidth=2)
        self.add_clip("b.wav", start=SEC, seconds=SEC, framerate=8000, sampwidth=3)

        plan = self.timeline().plan_snip(at(0), at(SEC * 2))
        self.assertFalse(plan.needs_resample)

        result = render_snip(plan, self.out)
        self.assertEqual(result.fmt.sampwidth, 2)
        self.assertEqual(result.nframes, SEC * 2 * 8000)

        render_snip(plan, self.root / "wide.wav", target=AudioFormat(8000, 1, 4))
        self.assertEqual(wavio.read_info(self.root / "wide.wav").sampwidth, 4)

    def test_mixed_rates_resample_into_one_format(self):
        self.add_clip("a.wav", start=0, seconds=SEC, framerate=8000)
        self.add_clip("b.wav", start=SEC, seconds=SEC, framerate=16000)

        plan = self.timeline().plan_snip(at(0), at(SEC * 2))
        self.assertTrue(plan.needs_resample)

        result = render_snip(plan, self.out, target=AudioFormat(16000, 1, 2))
        self.assertEqual(result.fmt.framerate, 16000)
        # 10 s at 8 kHz becomes 10 s at 16 kHz: duration, not frame count, holds.
        self.assertAlmostEqual(result.nframes / 16000, SEC * 2, places=2)

    def test_refuses_surround_downmix_during_export(self):
        self.add_clip("a.wav", start=0, seconds=1, framerate=8000, nchannels=6)
        plan = self.timeline().plan_snip(at(0), at(1))
        with self.assertRaises(FormatUnsupported):
            render_snip(plan, self.out, target=AudioFormat(8000, 2, 2))

    def test_identical_rate_run_is_not_resampled(self):
        """Same-rate pieces concatenate; no filter, so samples pass through."""
        self.add_clip("a.wav", start=0, seconds=2, framerate=8000)
        self.add_clip("b.wav", start=2, seconds=2, framerate=8000, phase=0.7)
        plan = self.timeline().plan_snip(at(0), at(4))

        render_snip(plan, self.out)
        out = wavio.read_frames(self.out, 0, 4 * 8000)[:, 0]
        np.testing.assert_allclose(out[:16000], tone(2, 8000)[:, 0], atol=1 / 32768)
        np.testing.assert_allclose(out[16000:], tone(2, 8000, phase=0.7)[:, 0],
                                   atol=1 / 32768)


class RenderGapTests(ExportCase):
    def build_gapped_plan(self):
        self.add_clip("a.wav", start=0, framerate=8000)
        self.add_clip("b.wav", start=SEC * 3, framerate=8000, phase=0.5)
        return self.timeline().plan_snip(at(0), at(SEC * 4))

    def test_gap_is_dropped_by_default(self):
        """The agreed default: export what exists, report what did not."""
        plan = self.build_gapped_plan()
        result = render_snip(plan, self.out)

        self.assertEqual(result.nframes, SEC * 2 * 8000)
        self.assertFalse(result.gaps_filled)
        self.assertAlmostEqual(result.missing_us / 1_000_000, SEC * 2)
        self.assertEqual(len(result.gaps), 1)

    def test_fill_gaps_pads_to_the_requested_length(self):
        plan = self.build_gapped_plan()
        result = render_snip(plan, self.out, fill_gaps=True)

        self.assertAlmostEqual(result.nframes / 8000, SEC * 4, places=2)
        self.assertTrue(result.gaps_filled)
        self.assertEqual(result.missing_us, 0)
        self.assertEqual(result.gaps, ())

    def test_filled_gap_is_actually_silent(self):
        plan = self.build_gapped_plan()
        render_snip(plan, self.out, fill_gaps=True)
        out = wavio.read_frames(self.out, 0, SEC * 4 * 8000)
        self.assertLess(np.abs(out[SEC * 8000: SEC * 3 * 8000]).max(), 1e-6)

    def test_filled_output_starts_with_audio_not_silence(self):
        """A leading gap must not shift everything later."""
        plan = self.build_gapped_plan()
        render_snip(plan, self.out, fill_gaps=True)
        out = wavio.read_frames(self.out, 0, SEC * 4 * 8000)
        self.assertGreater(np.abs(out[:1000]).max(), 0.1)

    def test_no_gaps_means_no_difference(self):
        self.add_clip("a.wav", start=0, framerate=8000)
        self.add_clip("b.wav", start=SEC, framerate=8000, phase=0.5)
        plan = self.timeline().plan_snip(at(0), at(SEC * 2))

        plain = render_snip(plan, self.root / "plain.wav")
        filled = render_snip(plan, self.root / "filled.wav", fill_gaps=True)
        self.assertEqual(plain.nframes, filled.nframes)

    def test_trailing_gap_is_filled(self):
        self.add_clip("a.wav", start=0, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(SEC * 3))
        result = render_snip(plan, self.out, fill_gaps=True)
        self.assertAlmostEqual(result.nframes / 8000, SEC * 3, places=2)


class BlockFlushTests(ExportCase):
    """The memory budget must not change the output, only the memory used."""

    def test_many_small_blocks_produce_the_same_audio(self):
        import wavesnip.resample as resample_module

        self.add_clip("a.wav", start=0, seconds=4, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(4))

        render_snip(plan, self.root / "whole.wav")
        expected = wavio.read_frames(self.root / "whole.wav", 0, 4 * 8000)

        original = resample_module._MAX_BLOCK_BYTES
        # Tiny budget: forces a flush every few hundred frames.
        resample_module._MAX_BLOCK_BYTES = 4 * 8000 * 2
        try:
            result = render_snip(plan, self.root / "chunked.wav")
        finally:
            resample_module._MAX_BLOCK_BYTES = original

        self.assertEqual(result.nframes, expected.shape[0])
        np.testing.assert_array_equal(
            wavio.read_frames(self.root / "chunked.wav", 0, result.nframes), expected
        )

    def test_flush_happens_even_within_one_clip(self):
        """A single long clip still gets split, so memory stays bounded."""
        import wavesnip.resample as resample_module

        self.add_clip("a.wav", start=0, seconds=4, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(4))

        original = resample_module._MAX_BLOCK_BYTES
        resample_module._MAX_BLOCK_BYTES = 4 * 8000
        try:
            result = render_snip(plan, self.out)
        finally:
            resample_module._MAX_BLOCK_BYTES = original

        self.assertEqual(result.nframes, 4 * 8000)


class StaleIndexTests(ExportCase):
    def test_truncated_file_is_reported_not_silently_dropped(self):
        """A stale index means the file changed; say so instead of lying."""
        self.add_clip("a.wav", start=0, seconds=4, framerate=8000)
        plan = self.timeline().plan_snip(at(0), at(4))

        path = self.archive.path / "a.wav"
        path.write_bytes(path.read_bytes()[:2000])  # truncate after indexing

        with self.assertRaises(ExportError) as ctx:
            render_snip(plan, self.out)
        self.assertIn("a.wav", str(ctx.exception))


class TruncatedFileTests(unittest.TestCase):
    """A file that contradicts its own header must not be silently short."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "a.wav"
        wavio.write_wav(self.path, tone(4, 8000), framerate=8000)

    def test_truncation_is_detected(self):
        self.path.write_bytes(self.path.read_bytes()[:2000])
        with self.assertRaises(wavio.WavError) as ctx:
            wavio.read_frames(self.path, 0, 32000)
        self.assertIn("truncated", str(ctx.exception))

    def test_truncation_within_the_request_is_detected(self):
        self.path.write_bytes(self.path.read_bytes()[:8000])
        with self.assertRaises(wavio.WavError):
            wavio.read_frames(self.path, 0, 32000)

    def test_intact_file_is_unaffected(self):
        self.assertEqual(wavio.read_frames(self.path, 0, 32000).shape, (32000, 1))

    def test_reading_past_the_declared_end_still_clamps(self):
        """Clamping to the declared length is correct, not an error."""
        samples = wavio.read_frames(self.path, 31000, 5000)
        self.assertEqual(samples.shape, (1000, 1))

    def test_start_beyond_the_file_is_an_error(self):
        with self.assertRaises(wavio.WavError):
            wavio.read_frames(self.path, 32000, 10)


class WavWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "stream.wav"

    def test_multiple_writes_accumulate(self):
        with wavio.WavWriter(self.out, 8000, 1, 2) as writer:
            writer.write(tone(1, 8000))
            writer.write(tone(1, 8000))
            self.assertEqual(writer.nframes, 16000)

        info = wavio.read_info(self.out)
        self.assertEqual(info.nframes, 16000)
        self.assertEqual(info.framerate, 8000)

    def test_empty_write_is_a_no_op(self):
        with wavio.WavWriter(self.out, 8000, 2, 2) as writer:
            self.assertEqual(writer.write(np.zeros((0, 2), dtype=np.float32)), 0)
        self.assertEqual(wavio.read_info(self.out).nframes, 0)

    def test_wrong_channel_count_is_refused(self):
        with wavio.WavWriter(self.out, 8000, 2, 2) as writer:
            with self.assertRaises(wavio.WavError):
                writer.write(tone(1, 8000))

    def test_writing_after_close_is_refused(self):
        writer = wavio.WavWriter(self.out, 8000, 1, 2)
        writer.close()
        with self.assertRaises(wavio.WavError):
            writer.write(tone(1, 8000))

    def test_close_is_idempotent(self):
        writer = wavio.WavWriter(self.out, 8000, 1, 2)
        writer.close()
        writer.close()

    def test_rejects_bad_parameters(self):
        with self.assertRaises(wavio.WavError):
            wavio.WavWriter(self.out, 8000, 1, 5)
        with self.assertRaises(wavio.WavError):
            wavio.WavWriter(self.out, 0, 1, 2)
        with self.assertRaises(wavio.WavError):
            wavio.WavWriter(self.out, 8000, 0, 2)


if __name__ == "__main__":
    unittest.main()