"""Tests for wavesnip.wavio.

Run with:  python -m unittest discover -s tests -t . -v

Fixtures are built with stdlib ``wave`` or by hand-assembling RIFF headers,
never with ``wavio`` itself, so assertions do not merely confirm the module
round-trips with itself.

The WAVE_FORMAT_EXTENSIBLE tests are skipped below Python 3.12 — that is the
one capability the version floor exists for, so run them under 3.12+ before
trusting the rest.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from wavesnip import wavio


def write_raw(path: Path, raw: bytes, *, sampwidth, nchannels, framerate) -> Path:
    """Write a WAV using stdlib wave, bypassing wavesnip entirely."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(raw)
    return path


# KSDATAFORMAT_SUBTYPE_PCM, little-endian GUID.
_PCM_SUBFORMAT = bytes.fromhex("0100000000001000800000aa00389b71")


def write_extensible(
    path: Path,
    raw: bytes,
    *,
    sampwidth,
    nchannels,
    framerate,
    valid_bits=None,
    channel_mask=0,
) -> Path:
    """Hand-build a WAVE_FORMAT_EXTENSIBLE file.

    stdlib ``wave`` can only *read* this format (3.12+), never write it, so the
    header is assembled byte by byte. This is what many recorders emit for
    multichannel or >16-bit audio, and it is the sole reason the project
    requires Python 3.12.
    """
    valid_bits = sampwidth * 8 if valid_bits is None else valid_bits
    block_align = nchannels * sampwidth

    fmt = struct.pack(
        "<HHIIHH",
        0xFFFE,  # WAVE_FORMAT_EXTENSIBLE
        nchannels,
        framerate,
        framerate * block_align,
        block_align,
        sampwidth * 8,
    )
    fmt += struct.pack("<HHI", 22, valid_bits, channel_mask)
    fmt += _PCM_SUBFORMAT

    body = (
        b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(raw)) + raw
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def pcm_grid(rng, nframes, nchannels, sampwidth):
    """Random samples lying exactly on the integer PCM grid.

    Lets us assert bit-exact round trips instead of hiding quantisation
    behind a tolerance. Only valid for widths whose LSB fits float32's
    24-bit mantissa, i.e. 16 and 24 bit -- see the precision note in wavio.
    """
    scale = 2 ** (8 * sampwidth - 1)
    ints = rng.integers(-(scale // 2), scale // 2, size=(nframes, nchannels))
    return (ints / scale).astype(np.float32)


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def path(self, name: str) -> Path:
        return self.tmp / name


@unittest.skipUnless(
    sys.version_info >= (3, 12),
    "WAVE_FORMAT_EXTENSIBLE needs Python 3.12+ -- the reason for the version floor",
)
class TestExtensible(TempCase):
    """The 3.12 justification, actually exercised."""

    def test_multichannel_16bit_is_readable(self):
        raw = np.arange(24, dtype="<i2").tobytes()
        p = write_extensible(
            self.path("ext16.wav"), raw,
            sampwidth=2, nchannels=2, framerate=48000, channel_mask=0x3,
        )
        info = wavio.read_info(p)
        self.assertEqual((info.nchannels, info.sampwidth, info.framerate), (2, 2, 48000))
        self.assertEqual(info.nframes, 12)  # 24 int16 samples, 2 channels
        np.testing.assert_array_equal(
            wavio.read_frames(p, 0, 12).reshape(-1),
            np.arange(24).astype(np.float32) / 32768.0,
        )

    def test_24bit_in_32bit_container_survives_the_float32_pipeline(self):
        """The classic recorder output: 24 valid bits in a 32-bit container.

        wave reports sampwidth 4, but only 24 significant bits are real, so
        this must still be exact in float32. If this ever fails, the precision
        note in wavio needs revisiting -- a 24-bit archive would silently
        become lossy.
        """
        rng = np.random.default_rng(3)
        valid = rng.integers(-(2 ** 23), 2 ** 23, size=(64, 2))
        raw = (valid << 8).astype("<i4").tobytes()  # low 8 bits are padding
        p = write_extensible(
            self.path("ext24.wav"), raw,
            sampwidth=4, nchannels=2, framerate=96000,
            valid_bits=24, channel_mask=0x3,
        )
        info = wavio.read_info(p)
        self.assertEqual(info.sampwidth, 4, "container is 32-bit wide")
        self.assertEqual(info.nframes, 64)

        back = wavio.read_frames(p, 0, 64)
        np.testing.assert_array_equal(back, valid.astype(np.float32) / 2 ** 23)

    def test_partial_read_from_extensible_file(self):
        # Keep values inside int16 -- 2000 * 100 would wrap.
        values = (np.arange(2000) % 300) * 100
        p = write_extensible(
            self.path("extpart.wav"), values.astype("<i2").tobytes(),
            sampwidth=2, nchannels=1, framerate=8000,
        )
        got = wavio.read_frames(p, 500, 100).reshape(-1)
        expected = values[500:600].astype(np.float32) / 32768.0
        np.testing.assert_array_equal(got, expected)

    def test_duration_is_still_exact(self):
        # The start-time derivation depends on this being exact.
        raw = np.zeros(96000, dtype="<i2").tobytes()
        p = write_extensible(
            self.path("extdur.wav"), raw,
            sampwidth=2, nchannels=1, framerate=48000,
        )
        self.assertEqual(wavio.read_info(p).duration, 2.0)


class TestHeader(TempCase):
    def test_reads_header_fields(self):
        # 48000 frames * 2 channels * 2 bytes
        p = write_raw(
            self.path("a.wav"), b"\x00" * 48000 * 4,
            sampwidth=2, nchannels=2, framerate=48000,
        )
        info = wavio.read_info(p)
        self.assertEqual(info.nchannels, 2)
        self.assertEqual(info.sampwidth, 2)
        self.assertEqual(info.framerate, 48000)
        self.assertEqual(info.nframes, 48000)
        self.assertEqual(info.block_align, 4)
        self.assertEqual(info.nbytes, 48000 * 4)

    def test_duration_is_exact(self):
        # 44100 frames at 48000 Hz is 0.91875s exactly.
        p = write_raw(
            self.path("b.wav"), b"\x00" * 44100 * 2,
            sampwidth=2, nchannels=1, framerate=48000,
        )
        self.assertEqual(wavio.read_info(p).duration, 44100 / 48000)

    def test_rejects_non_wav(self):
        p = self.path("not.wav")
        p.write_bytes(b"this is definitely not a riff file")
        with self.assertRaises(wavio.WavError):
            wavio.read_info(p)

    def test_rejects_truncated_header(self):
        p = self.path("short.wav")
        p.write_bytes(b"RIFF")
        with self.assertRaises(wavio.WavError):
            wavio.read_info(p)


class TestConversion(TempCase):
    """Exactness checks across every integer width."""

    WIDTHS = (1, 2, 3, 4)

    def test_roundtrip_is_bit_exact(self):
        """16- and 24-bit PCM must survive untouched.

        This is the property the snip/export path depends on: a snip that
        crosses a chunk boundary concatenates decoded pieces, so any
        per-chunk loss compounds at every join.
        """
        rng = np.random.default_rng(1234)
        for sampwidth in (1, 2, 3):
            for nchannels in (1, 2, 6):
                with self.subTest(sampwidth=sampwidth, nchannels=nchannels):
                    original = pcm_grid(rng, 500, nchannels, sampwidth)
                    p = wavio.write_wav(
                        self.path(f"rt{sampwidth}_{nchannels}.wav"),
                        original, framerate=48000, sampwidth=sampwidth,
                    )
                    back = wavio.read_frames(p, 0, 500)
                    np.testing.assert_array_equal(back, original)

    def test_32bit_is_lossy_but_bounded(self):
        """Documented limitation, asserted so it cannot regress silently.

        float32's 24-bit mantissa cannot hold int32 LSBs, so ~8 low bits are
        lost. If this test ever starts failing by *more*, the pipeline got
        worse; if it starts passing, the pipeline improved and the note in
        wavio's docstring should be revisited.
        """
        rng = np.random.default_rng(99)
        scale = 2 ** 31
        ints = rng.integers(-(scale // 2), scale // 2, size=(400, 2))
        # float64 division is exact, so `exact` is the true int32 PCM value.
        exact = ints / scale
        # write_wav coerces to float32 -- this is where the bits are lost, and
        # it is the same coercion read_frames applies on the way back.
        p = wavio.write_wav(self.path("rt32.wav"), exact, 48000, sampwidth=4)
        back = wavio.read_frames(p, 0, 400)

        lsb = 1.0 / scale
        error = np.abs(back.astype(np.float64) - exact)
        self.assertGreater(error.max(), 0.0, "expected some 32-bit precision loss")
        self.assertLessEqual(error.max(), 128 * lsb)

    def test_arbitrary_floats_quantise_within_half_lsb(self):
        """Non-grid input is quantised, as it must be, but no worse."""
        rng = np.random.default_rng(5)
        for sampwidth in self.WIDTHS:
            with self.subTest(sampwidth=sampwidth):
                original = rng.uniform(-0.5, 0.5, size=(300, 2))
                p = wavio.write_wav(
                    self.path(f"q{sampwidth}.wav"), original, 48000, sampwidth=sampwidth
                )
                back = wavio.read_frames(p, 0, 300)
                tol = 0.5 / (2 ** (8 * sampwidth - 1)) + 1e-9
                np.testing.assert_allclose(back, original, atol=tol)

    def test_16bit_exact_sample_values(self):
        raw = np.array([0, 32767, -32768, 1000], dtype="<i2").tobytes()
        p = write_raw(self.path("s16.wav"), raw, sampwidth=2, nchannels=1, framerate=8000)
        got = wavio.read_frames(p, 0, 4).reshape(-1)
        # 32767/32768 is the largest representable positive value.
        np.testing.assert_allclose(got, [0.0, 32767 / 32768, -1.0, 1000 / 32768])

    def test_8bit_is_unsigned_and_centred_at_128(self):
        raw = bytes([0, 128, 255])
        p = write_raw(self.path("s8.wav"), raw, sampwidth=1, nchannels=1, framerate=8000)
        got = wavio.read_frames(p, 0, 3).reshape(-1)
        np.testing.assert_allclose(got, [-1.0, 0.0, 127 / 128])

    def test_24bit_is_signed_little_endian(self):
        # -8388608 (min), -1, 0, 1, 8388607 (max)
        values = [-8388608, -1, 0, 1, 8388607]
        packed = np.empty((len(values), 3), dtype=np.uint8)
        for i, v in enumerate(values):
            u = v & 0xFFFFFF
            packed[i] = [(u >> 0) & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF]
        p = write_raw(
            self.path("s24.wav"), packed.tobytes(),
            sampwidth=3, nchannels=1, framerate=8000,
        )
        got = wavio.read_frames(p, 0, len(values)).reshape(-1)
        np.testing.assert_allclose(got, np.array(values) / 8388608.0)

    def test_24bit_handles_interleaved_channels(self):
        left = [0, 100, -100]
        right = [8388607, -8388608, 42]
        interleaved = np.empty((3, 2, 3), dtype=np.uint8)
        for i in range(3):
            for c, v in enumerate((left[i], right[i])):
                u = v & 0xFFFFFF
                for b in range(3):
                    interleaved[i, c, b] = (u >> (8 * b)) & 0xFF
        p = write_raw(
            self.path("s24st.wav"), interleaved.tobytes(),
            sampwidth=3, nchannels=2, framerate=8000,
        )
        got = wavio.read_frames(p, 0, 3)
        self.assertEqual(got.shape, (3, 2))
        np.testing.assert_allclose(got[:, 0], np.array(left) / 8388608.0)
        np.testing.assert_allclose(got[:, 1], np.array(right) / 8388608.0)

    def test_full_scale_does_not_overflow(self):
        # +1.0 would be 32768, which does not fit in int16.
        for sampwidth in self.WIDTHS:
            with self.subTest(sampwidth=sampwidth):
                p = wavio.write_wav(
                    self.path(f"clip{sampwidth}.wav"),
                    np.array([-1.0, 1.0, 0.0]), framerate=8000, sampwidth=sampwidth,
                )
                got = wavio.read_frames(p, 0, 3).reshape(-1)
                self.assertGreaterEqual(got.min(), -1.0)
                self.assertLessEqual(got.max(), 1.0)
                self.assertAlmostEqual(float(got[1]), 1.0 - 1.0 / (2 ** (8 * sampwidth - 1)))

    def test_rejects_unsupported_width(self):
        with self.assertRaises(wavio.WavError):
            wavio.frames_to_float(b"\x00" * 4, 5, 1)
        with self.assertRaises(wavio.WavError):
            wavio.float_to_frames(np.zeros(4, dtype=np.float32), 5)

    def test_ignores_trailing_partial_sample(self):
        # 5 bytes of 16-bit is 2 whole samples plus a stray byte. np.frombuffer
        # would raise on this; frames_to_float truncates instead.
        got = wavio.frames_to_float(b"\x00" * 5, 2, 1)
        self.assertEqual(got.shape, (2, 1))
        self.assertEqual(wavio.frames_to_float(b"\x00" * 7, 3, 1).shape, (2, 1))
        self.assertEqual(wavio.frames_to_float(b"\x00" * 5, 4, 1).shape, (1, 1))
        self.assertEqual(wavio.frames_to_float(b"\x00" * 3, 4, 1).shape, (0, 1))


class TestPartialReads(TempCase):
    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(7)
        # On the PCM grid, so concatenation can be asserted exactly.
        self.samples = pcm_grid(rng, 1000, 2, 2)
        self.path_ = wavio.write_wav(self.path("p.wav"), self.samples, framerate=48000)

    def test_offset_read_matches_full_read(self):
        full = wavio.read_frames(self.path_, 0, 1000)
        part = wavio.read_frames(self.path_, 250, 100)
        np.testing.assert_array_equal(part, full[250:350])

    def test_read_clamps_at_end_of_file(self):
        got = wavio.read_frames(self.path_, 990, 100)
        self.assertEqual(got.shape, (10, 2))

    def test_read_past_end_raises(self):
        with self.assertRaises(wavio.WavError):
            wavio.read_frames(self.path_, 1000, 10)

    def test_negative_offset_raises(self):
        with self.assertRaises(ValueError):
            wavio.read_frames(self.path_, -1, 10)

    def test_concatenated_pieces_match_original(self):
        # This is the shape a cross-boundary snip takes in timeline.py.
        pieces = [
            wavio.read_frames(self.path_, 0, 300),
            wavio.read_frames(self.path_, 300, 700),
        ]
        joined = wavio.write_wav(self.path("joined.wav"), np.concatenate(pieces), 48000)
        np.testing.assert_array_equal(wavio.read_frames(joined, 0, 1000), self.samples)


class TestReadProbes(TempCase):
    """Several small reads from one file, opened exactly once.

    The single open is the entire point of ``read_probes``: going through
    ``read_frames`` per probe reopens the WAV every time, and measured here that
    made thousands of probes slower than reading the whole file, because the
    opens dominate. Everything else -- clamping, refusing a start past the end,
    rejecting a truncated file -- has to behave exactly as ``read_frames``
    does, which it does by being the same code.
    """

    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(11)
        # On the PCM grid, so slices can be asserted exactly against read_frames.
        self.samples = pcm_grid(rng, 1000, 2, 2)
        self.path_ = wavio.write_wav(self.path("pr.wav"), self.samples, framerate=48000)

    def test_each_probe_returns_its_own_run(self):
        full = wavio.read_frames(self.path_, 0, 1000)
        probes = [(0, 10), (250, 100), (990, 100)]
        parts = wavio.read_probes(self.path_, probes)
        self.assertEqual(len(parts), len(probes), "one array per probe, in order")
        for (start, count), part in zip(probes, parts):
            np.testing.assert_array_equal(part, full[start : start + count])

    def test_the_file_is_opened_once_for_many_probes(self):
        with mock.patch.object(wavio, "_open", wraps=wavio._open) as opened:
            wavio.read_probes(self.path_, [(i * 10, 4) for i in range(50)])
        self.assertEqual(opened.call_count, 1, "one open per file, not per probe")

    def test_nothing_asked_for_reads_nothing(self):
        with mock.patch.object(wavio, "_open", wraps=wavio._open) as opened:
            self.assertEqual(wavio.read_probes(self.path_, []), [])
        opened.assert_not_called()

    def test_a_negative_probe_is_refused(self):
        for bad in [(-1, 5), (10, -5)]:
            with self.subTest(probe=bad):
                with self.assertRaises(ValueError):
                    wavio.read_probes(self.path_, [bad])

    def test_a_probe_past_the_end_raises(self):
        with self.assertRaises(wavio.WavError):
            wavio.read_probes(self.path_, [(1000, 10)])

    def test_a_probe_count_clamps_at_the_end(self):
        (part,) = wavio.read_probes(self.path_, [(990, 100)])
        self.assertEqual(part.shape, (10, 2))

    def test_a_truncated_file_still_raises(self):
        """A short read contradicts the header, so it is corruption, not less
        audio -- the one failure nobody would notice otherwise."""
        self.path_.write_bytes(self.path_.read_bytes()[:-1000])
        with self.assertRaises(wavio.WavError):
            wavio.read_probes(self.path_, [(740, 20)])


class TestWriterFailures(TempCase):
    """The output path, checked before ``wave`` gets a chance to complain.

    ``wave.open(path, "wb")`` builds a ``Wave_write`` whose constructor raises
    when the folder is not there, and whose ``__del__`` then raises again on the
    half-built object -- printing an ``Exception ignored`` traceback at the user
    on top of the real error. Validating the path first means one clear message.
    """

    def test_a_missing_folder_is_created(self):
        target = self.path("new") / "deeper" / "out.wav"
        with wavio.WavWriter(target, 8000, 1, 2) as writer:
            writer.write(np.zeros((800, 1), dtype=np.float32))
        self.assertTrue(target.exists())

    def test_a_file_where_the_folder_should_be_is_explained(self):
        blocker = self.path("blocker")
        blocker.write_text("in the way")

        with self.assertRaises(wavio.WavError) as caught:
            wavio.WavWriter(blocker / "out.wav", 8000, 1, 2)
        self.assertIn("blocker", str(caught.exception))

    def test_the_failure_names_the_path_not_the_internals(self):
        """The message has to be actionable, not an errno dump."""
        blocker = self.path("blocker")
        blocker.write_text("in the way")
        with self.assertRaises(wavio.WavError) as caught:
            wavio.WavWriter(blocker / "out.wav", 8000, 1, 2)
        message = str(caught.exception)
        self.assertIn("cannot create", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("errno", message)


if __name__ == "__main__":
    unittest.main()
