"""WAV access, built on stdlib ``wave`` module.

Requires Python 3.12+: ``wave`` only learned to read ``WAVE_FORMAT_EXTENSIBLE``
in 3.12, and many recorders emit it. See ``flow.md``.

Deliberately no hand-rolled RIFF parser, and no ``audioop`` -- deprecated in
3.11, removed in 3.13. numpy does all sample conversion.

Samples exposed as float32 in [-1, 1), shaped ``(nframes, nchannels)``.

**Precision limit of float32 pipeline.** A float32 has a 24-bit mantissa, so
int16 and int24 PCM survive round trip *bit-exactly* (LSBs are 2**-15 and
2**-23). int32 PCM does **not**: LSB is 2**-31, so decoding to float32 discards
~low 8 bits, and error of up to ~64 LSBs introduced.

This is a deliberate trade. Passing samples as one dtype keeps resampling and
waveform code simple, and 16/24-bit is what recorders actually write. But if
32-bit PCM archives ever matter, pipeline must carry int32 as int64 and float
only at edges -- do not "fix" this by loosening a tolerance.

Known gap: IEEE float WAV (``format`` 3) unsupported by ``wave`` in every
version so far, including 3.12. PCM integer is the realistic recorder case; if
a float file turns up, it needs a manual path added here.
"""

from __future__ import annotations

import secrets
import struct
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Optional

import numpy as np

__all__ = [
    "WavError",
    "WavInfo",
    "WavWriter",
    "BextChunk",
    "read_info",
    "read_frames",
    "write_wav",
    "frames_to_float",
    "float_to_frames",
]

# Full-scale value per sample width, i.e. 2 ** (8 * sampwidth - 1).
_FULL_SCALE = {1: 128, 2: 32768, 3: 8388608, 4: 2147483648}


class WavError(Exception):
    """Raised when a file cannot be read as PCM WAV."""


@dataclass(frozen=True)
class WavInfo:
    """Header facts. Everything the index needs, nothing more."""

    nchannels: int
    sampwidth: int  # bytes per sample
    framerate: int
    nframes: int

    @property
    def block_align(self) -> int:
        return self.nchannels * self.sampwidth

    @property
    def duration(self) -> float:
        """Exact, not estimated.

        This exactness is the reason the whole start-time derivation
        (``mtime - duration``) is trustworthy. Do not replace this with a
        bitrate estimate.
        """
        return self.nframes / self.framerate

    @property
    def nbytes(self) -> int:
        return self.nframes * self.block_align


@dataclass(frozen=True)
class BextChunk:
    """Broadcast Wave Format (BWF) 'bext' chunk per EBU Tech 3285.

    Fixed portion is 602 bytes. coding_history is variable-length, null-terminated.
    """

    description: str = ""                    # 256 bytes
    originator: str = "ArchiveExplorer"      # 32 bytes
    originator_reference: str = ""           # 32 bytes
    origination_date: str = ""               # 10 bytes "YYYY-MM-DD"
    origination_time: str = ""               # 8 bytes "HH:MM:SS"
    time_reference: int = 0                  # uint64, samples since midnight
    version: int = 1                         # uint16: 1 or 2
    umid: bytes = field(default_factory=lambda: secrets.token_bytes(64))  # 64 bytes
    # Version 2 loudness fields (int16, scaled by 100)
    loudness_value: Optional[int] = None       # LUFS * 100
    loudness_range: Optional[int] = None       # LU * 100
    max_true_peak_level: Optional[int] = None  # dBTP * 100
    max_momentary_loudness: Optional[int] = None  # LUFS * 100
    max_short_term_loudness: Optional[int] = None  # LUFS * 100
    coding_history: str = ""                 # variable, null-terminated

    def serialize(self) -> bytes:
        """Serialize to bytes per EBU Tech 3285 specification."""
        # Fixed fields
        desc = self.description[:256].encode("ascii", errors="ignore").ljust(256, b"\x00")
        orig = self.originator[:32].encode("ascii", errors="ignore").ljust(32, b"\x00")
        orig_ref = self.originator_reference[:32].encode("ascii", errors="ignore").ljust(32, b"\x00")
        date = self.origination_date[:10].encode("ascii", errors="ignore").ljust(10, b"\x00")
        time = self.origination_time[:8].encode("ascii", errors="ignore").ljust(8, b"\x00")
        time_ref = struct.pack("<Q", self.time_reference)
        version = struct.pack("<H", self.version)
        umid = self.umid[:64].ljust(64, b"\x00")

        # Loudness fields (version 2) - 5 * int16 = 10 bytes
        if self.version >= 2 and all(v is not None for v in (
            self.loudness_value, self.loudness_range, self.max_true_peak_level,
            self.max_momentary_loudness, self.max_short_term_loudness
        )):
            loudness = struct.pack(
                "<hhhhh",
                self.loudness_value,
                self.loudness_range,
                self.max_true_peak_level,
                self.max_momentary_loudness,
                self.max_short_term_loudness,
            )
        else:
            loudness = b"\x00" * 10

        # Reserved 180 bytes
        reserved = b"\x00" * 180

        # Coding history (variable, null-terminated)
        coding = self.coding_history.encode("ascii", errors="ignore") + b"\x00"

        # Total chunk data size
        chunk_data = desc + orig + orig_ref + date + time + time_ref + version + umid + loudness + reserved + coding
        chunk_size = len(chunk_data)

        # RIFF chunk: ID + size (little-endian) + data + padding byte if odd
        return b"bext" + struct.pack("<I", chunk_size) + chunk_data + (b"\x00" if chunk_size % 2 else b"")


def _check_comptype(wf: wave.Wave_read, path: Path) -> None:
    comptype = wf.getcomptype()
    if comptype != "NONE":
        raise WavError(f"{path}: WAV variant {comptype!r} is not PCM, not supported")


def _open(path: str | Path) -> wave.Wave_read:
    try:
        return wave.open(str(path), "rb")
    except wave.Error as exc:
        raise WavError(f"{path}: not a readable WAV ({exc})") from exc
    except EOFError as exc:
        raise WavError(f"{path}: truncated WAV header") from exc


def read_info(path: str | Path) -> WavInfo:
    """Header fields only. Cheap enough to call per file during indexing."""
    with _open(path) as wf:
        _check_comptype(wf, Path(path))
        return WavInfo(
            nchannels=wf.getnchannels(),
            sampwidth=wf.getsampwidth(),
            framerate=wf.getframerate(),
            nframes=wf.getnframes(),
        )


def _decode_24bit(raw: bytes) -> np.ndarray:
    """Little-endian packed 24-bit signed -> int32, with sign extension."""
    packed = np.frombuffer(raw, dtype=np.uint8)
    usable = (len(packed) // 3) * 3
    packed = packed[:usable].reshape(-1, 3)
    return (
        packed[:, 0].astype(np.int32)
        | (packed[:, 1].astype(np.int32) << 8)
        | (packed[:, 2].astype(np.int8).astype(np.int32) << 16)
    )


def _encode_24bit(values: np.ndarray) -> bytes:
    """int32 -> packed little-endian 24-bit signed."""
    v = values.astype(np.int32).reshape(-1)
    out = np.empty((v.size, 3), dtype=np.uint8)
    out[:, 0] = v & 0xFF
    out[:, 1] = (v >> 8) & 0xFF
    out[:, 2] = (v >> 16) & 0xFF
    return out.tobytes()


def frames_to_float(raw: bytes, sampwidth: int, nchannels: int) -> np.ndarray:
    """Interleaved PCM bytes -> float32 ``(nframes, nchannels)`` in [-1, 1)."""
    if nchannels < 1:
        raise WavError(f"invalid channel count: {nchannels}")
    if sampwidth not in _FULL_SCALE:
        raise WavError(f"unsupported sample width: {sampwidth} bytes")

    # np.frombuffer rejects a buffer whose length is not a multiple of the
    # element size. wave hands back whole frames, but truncating here turns a
    # would-be crash into simply dropping a stray partial sample.
    raw = raw[: (len(raw) // sampwidth) * sampwidth]

    if sampwidth == 1:
        # 8-bit WAV is *unsigned*, centred at 128.
        out = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 2:
        out = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        out = _decode_24bit(raw).astype(np.float32) / 8388608.0
    elif sampwidth == 4:
        out = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0

    usable = (len(out) // nchannels) * nchannels
    return out[:usable].reshape(-1, nchannels)


def float_to_frames(samples: np.ndarray, sampwidth: int) -> bytes:
    """float32 ``(nframes, nchannels)`` -> interleaved PCM bytes.

    Lossless for values that came from integer PCM of the same width.
    """
    if sampwidth not in _FULL_SCALE:
        raise WavError(f"unsupported sample width: {sampwidth} bytes")

    scale = _FULL_SCALE[sampwidth]
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    # Scale in float64, deliberately. float32 cannot represent 2**31 - 1, so
    # clamping there is a no-op and the int32 cast then overflows into garbage.
    # Under NEP 50 (numpy 2.x) a Python int is a *weak* scalar, so float32 *
    # 2147483648 stays float32 and the bug is silent. float64 represents every
    # supported width's full-scale bound exactly.
    v = np.rint(np.clip(x.astype(np.float64), -1.0, 1.0) * scale)
    v = np.clip(v, -(scale - 1), scale - 1)

    if sampwidth == 1:
        return (v + 128).astype(np.uint8).tobytes()
    if sampwidth == 3:
        return _encode_24bit(v)
    dtype = {2: "<i2", 4: "<i4"}[sampwidth]
    return v.astype(dtype).tobytes()


def _raw_at(wf, path: str | Path, start_frame: int, nframes: int) -> bytes:
    """One seek-and-read on an already-open file, with all of ``read_frames``'
    checks. Returns the raw PCM bytes.

    Shared by ``read_frames`` (one read, converted immediately) and
    ``read_probes`` (many reads, converted together) so the two cannot drift
    into different rules about clamping and truncation -- the checks are the
    same code, not the same comment.
    """
    nchannels = wf.getnchannels()
    sampwidth = wf.getsampwidth()
    total = wf.getnframes()

    if start_frame >= total:
        raise WavError(
            f"{path}: start_frame {start_frame} is past end of file ({total} frames)"
        )

    # Clamp instead of erroring: a snip near the end of an archive should
    # give whatever audio exists, not fail. This clamps against the
    # *declared* length, which is exactly what should be trusted here.
    count = min(nframes, total - start_frame)
    wf.setpos(start_frame)
    raw = wf.readframes(count)

    # ...but a short read means the file contradicts its own header, which
    # is corruption rather than a short archive. Without this check a
    # truncated file silently produces a shorter snip than was asked for,
    # which is the one failure mode nobody would notice.
    expected_bytes = count * nchannels * sampwidth
    if len(raw) < expected_bytes:
        got = len(raw) // (nchannels * sampwidth)
        raise WavError(
            f"{path}: file is truncated -- header declares {total} frames "
            f"but only {start_frame + got} are present"
        )

    return raw


def read_frames(path: str | Path, start_frame: int, nframes: int) -> np.ndarray:
    """Read ``nframes`` frames from ``start_frame`` as float32.

    Seeks rather than decoding the file. A one-hour clip is ~690 MB stereo, so
    reading a whole one to take a few seconds out of it is never acceptable.
    """
    if start_frame < 0 or nframes < 0:
        raise ValueError(f"start_frame and nframes must be >= 0, got {start_frame}, {nframes}")

    with _open(path) as wf:
        _check_comptype(wf, Path(path))
        raw = _raw_at(wf, path, start_frame, nframes)
        return frames_to_float(raw, wf.getsampwidth(), wf.getnchannels())


def read_probes(path: str | Path, probes: list[tuple[int, int]]) -> list[np.ndarray]:
    """Read several small runs from one file, opening it exactly once.

    This is what makes a sampled envelope viable. Probing ``n`` positions with
    ``read_frames`` opens the WAV ``n`` times, and the opens *dominate*:
    measured here, thousands of probes through ``read_frames`` came out slower
    than reading whole files from start to finish -- the naive version lost to
    the thing it was meant to avoid. One open plus ``setpos`` per probe is the
    whole point of this function.

    Each probe is ``(start_frame, nframes)``; result has one float32 array per
    probe, in order, each with same clamping and truncation checks as
    ``read_frames`` -- they are the same code -- and each a *view* onto one
    batched conversion. Converting 128 frames costs same ``frombuffer`` and
    ``astype`` pair whether they stand alone or are one of 12800, and in a
    large request those pairs measured more than the reads they accompany. Empty
    list reads nothing and opens nothing.
    """
    if not probes:
        return []
    for start_frame, nframes in probes:
        if start_frame < 0 or nframes < 0:
            raise ValueError(
                f"probes must be non-negative, got ({start_frame}, {nframes})"
            )

    with _open(path) as wf:
        _check_comptype(wf, Path(path))
        raws = [_raw_at(wf, path, start_frame, nframes) for start_frame, nframes in probes]
        sampwidth = wf.getsampwidth()
        nchannels = wf.getnchannels()

    merged = frames_to_float(b"".join(raws), sampwidth, nchannels)
    frame_bytes = nchannels * sampwidth
    out = []
    offset = 0
    for raw in raws:
        count = len(raw) // frame_bytes
        out.append(merged[offset : offset + count])
        offset += count
    return out


class WavWriter:
    """Streaming WAV writer with optional BEXT chunk support.

    Uses manual RIFF writing (not stdlib ``wave``) to allow custom chunks.
    Patches RIFF/data sizes on ``close()``, making streaming straightforward.
    """

    def __init__(
        self,
        path: str | Path,
        framerate: int,
        nchannels: int,
        sampwidth: int = 2,
        bext: Optional[BextChunk] = None,
    ):
        if sampwidth not in _FULL_SCALE:
            raise WavError(f"unsupported sample width: {sampwidth} bytes")
        if framerate <= 0:
            raise WavError(f"framerate must be positive, got {framerate}")
        if nchannels <= 0:
            raise WavError(f"nchannels must be positive, got {nchannels}")

        self.path = Path(path)
        self.framerate = framerate
        self.nchannels = nchannels
        self.sampwidth = sampwidth
        self.bext = bext
        self._nframes = 0
        self._closed = False
        self._file: Optional[BinaryIO] = None
        self._riff_size_pos: int = 0
        self._data_size_pos: int = 0
        self._open_for_writing()

    def _open_for_writing(self) -> None:
        """Open file and write RIFF header, fmt, bext (optional), and data chunk headers."""
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WavError(f"cannot create {parent}: {exc}") from exc

        try:
            self._file = open(self.path, "wb")
        except OSError as exc:
            raise WavError(f"cannot write {self.path}: {exc}") from exc

        # RIFF header placeholder (size patched on close)
        # RIFF: "RIFF" + size (4 bytes) + "WAVE"
        self._file.write(b"RIFF")
        self._riff_size_pos = self._file.tell()
        self._file.write(struct.pack("<I", 0))  # placeholder
        self._file.write(b"WAVE")

        # fmt chunk
        fmt_data = struct.pack(
            "<HHIIHH",
            1,                    # wFormatTag = WAVE_FORMAT_PCM
            self.nchannels,
            self.framerate,
            self.framerate * self.nchannels * self.sampwidth,  # avg bytes/sec
            self.nchannels * self.sampwidth,  # block align
            self.sampwidth * 8,  # bits per sample
        )
        self._file.write(b"fmt ")
        self._file.write(struct.pack("<I", len(fmt_data)))
        self._file.write(fmt_data)

        # bext chunk (optional)
        if self.bext is not None:
            bext_data = self.bext.serialize()
            self._file.write(bext_data)

        # data chunk header (size patched on close)
        self._file.write(b"data")
        self._data_size_pos = self._file.tell()
        self._file.write(struct.pack("<I", 0))  # placeholder

    @property
    def nframes(self) -> int:
        return self._nframes

    def write(self, samples: np.ndarray) -> int:
        """Append samples. Returns the frame count written."""
        if self._closed:
            raise WavError("cannot write to a closed WavWriter")
        if self._file is None:
            raise WavError("file not open")

        x = np.asarray(samples, dtype=np.float32)
        if x.ndim == 1:
            x = x.reshape(-1, 1)
        if x.ndim != 2:
            raise WavError(f"expected 1-D or 2-D samples, got {x.ndim}-D")
        if x.shape[1] != self.nchannels:
            raise WavError(
                f"expected {self.nchannels} channels, got {x.shape[1]}"
            )
        if x.shape[0] == 0:
            return 0

        self._file.write(float_to_frames(x, self.sampwidth))
        self._nframes += x.shape[0]
        return x.shape[0]

    def close(self) -> None:
        if not self._closed and self._file is not None:
            # Patch data chunk size
            data_size = self._nframes * self.nchannels * self.sampwidth
            current_pos = self._file.tell()
            self._file.seek(self._data_size_pos)
            self._file.write(struct.pack("<I", data_size))

            # Patch RIFF size (file size - 8)
            riff_size = current_pos - 8 + (data_size % 2)  # account for padding
            self._file.seek(self._riff_size_pos)
            self._file.write(struct.pack("<I", riff_size))

            # Write padding byte if data size is odd
            if data_size % 2:
                self._file.seek(current_pos)
                self._file.write(b"\x00")

            self._file.close()
            self._file = None
            self._closed = True

    def __enter__(self) -> "WavWriter":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def write_wav(
    path: str | Path,
    samples: np.ndarray,
    framerate: int,
    sampwidth: int = 2,
) -> Path:
    """Write float32 samples as integer PCM WAV (simple, non-streaming)."""
    if sampwidth not in _FULL_SCALE:
        raise WavError(f"unsupported sample width: {sampwidth} bytes")

    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    if x.ndim != 2:
        raise WavError(f"expected 1-D or 2-D samples, got {x.ndim}-D")

    out = Path(path)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(x.shape[1])
        wf.setsampwidth(sampwidth)
        wf.setframerate(framerate)
        wf.writeframes(float_to_frames(x, sampwidth))
    return out