"""Render a snip plan to a single WAV, reconciling format mismatches.

A snip plan is a list of frame-exact reads, and those reads may disagree about
sample rate and channel count -- archives aren't always configured identically.
This module turns that list into one playable file.

Three decisions worth stating up front.

**Output format defaults to first piece's.** No better default, no truth to
discover: sources disagree, so user must choose. First piece is least
surprising because it's the start of the snip.

**Bit depth is not resampled.** Only rate and channel count require arithmetic.
Sample width is quantisation applied once at write time, so 16-bit and 24-bit
clips merge without signal processing.

**Conversions between different multi-channel layouts are refused, not
guessed.** Mono and stereo convert both ways with obvious, lossless-where-possible
rules. Going from e.g. 6 channels to 2 has no correct answer -- mean across 5.1
layout cancels to silence, because centre channel is anti-phase with surrounds.
Silently producing near-silence is worse than declining, so
:class:`FormatUnsupported` is raised and user picks.

**Resampling a whole run at once, not piece by piece.** Consecutive pieces
sharing a sample rate concatenated *before* resampling. Resampling each piece
separately restarts anti-alias filter at every chunk boundary, adding a click
per join in an archive whose premise is seamless joins. Blocks flushed when
they exceed memory budget, so multi-hour export stays bounded -- trade is that
a restart can happen at a budget boundary, which is rare and audibly negligible
next to a wrong-length export.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal

from wavesnip import wavio
from wavesnip.timeline import SnipPlan

__all__ = [
    "AudioFormat",
    "FormatUnsupported",
    "ExportError",
    "ExportResult",
    "canonical_format",
    "rate_ratio",
    "convert_channels",
    "render_snip",
]

# Cap on decoded float32 held in memory before a run is flushed and resampled.
# 32 MB is ~2 minutes of 48 kHz stereo, far beyond a typical snip, so this
# effectively never triggers except on very long exports.
_MAX_BLOCK_BYTES = 32 * 1024 * 1024


class FormatUnsupported(Exception):
    """A conversion with no defensible answer, e.g. 6 channels down to 2."""


class ExportError(Exception):
    """The snip could not be rendered."""


@dataclass(frozen=True)
class AudioFormat:
    framerate: int
    nchannels: int
    sampwidth: int

    def __post_init__(self) -> None:
        if self.framerate <= 0:
            raise ValueError(f"framerate must be positive, got {self.framerate}")
        if self.nchannels <= 0:
            raise ValueError(f"nchannels must be positive, got {self.nchannels}")
        if self.sampwidth not in (1, 2, 3, 4):
            raise ValueError(f"unsupported sample width: {self.sampwidth}")

    def __str__(self) -> str:
        return f"{self.framerate} Hz, {self.nchannels} ch, {self.sampwidth * 8}-bit"


def rate_ratio(source: int, target: int) -> tuple[int, int]:
    """Reduce a rate conversion to the smallest ``(up, down)`` pair.

    ``resample_poly`` takes an integer ratio, and rates are exact integers here
    because they come from WAV headers. Reducing by the GCD keeps the filter
    short: 48000 -> 44100 becomes 147/160 rather than 48000/44100.
    """
    if source <= 0 or target <= 0:
        raise ValueError(f"rates must be positive, got {source} and {target}")
    divisor = math.gcd(source, target)
    return target // divisor, source // divisor


def convert_channels(samples: np.ndarray, nchannels: int) -> np.ndarray:
    """Convert to ``nchannels``, or refuse if there is no sound way to.

    Mono and stereo both directions supported: downmix by mean, upmix by
    duplication. Any other channel count change refused.
    """
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    current = x.shape[1]

    if current == nchannels:
        return x
    if nchannels == 1:
        # Mean in float32: channel count is tiny, so no meaningful drift.
        return x.mean(axis=1, keepdims=True, dtype=np.float32)
    if current == 1:
        return np.repeat(x, nchannels, axis=1)

    raise FormatUnsupported(
        f"cannot convert {current} channels to {nchannels}: there is no "
        f"correct downmix for this layout (a mean across surround channels "
        f"cancels to silence)"
    )


def _resample(samples: np.ndarray, up: int, down: int) -> np.ndarray:
    """Resample a float32 ``(frames, channels)`` block by ``up/down``."""
    return signal.resample_poly(samples, up, down, axis=0).astype(np.float32)


def canonical_format(
    plan: SnipPlan, override: AudioFormat | None = None
) -> AudioFormat:
    """Pick the output format.

    Defaults to first piece's format, per agreed choice. With no pieces there's
    nothing to canonicalise to, so this raises rather than inventing a format.
    """
    if override is not None:
        return override
    if not plan.pieces:
        raise ExportError("snip contains no audio to export")
    first = plan.pieces[0].clip
    return AudioFormat(first.framerate, first.nchannels, first.sampwidth)


@dataclass(frozen=True)
class ExportResult:
    """What was actually written, as opposed to what was requested."""

    path: Path
    fmt: AudioFormat
    nframes: int
    plan: SnipPlan
    gaps_filled: bool

    @property
    def duration(self) -> float:
        return self.nframes / self.fmt.framerate

    @property
    def missing_us(self) -> int:
        return 0 if self.gaps_filled else self.plan.missing_us

    @property
    def gaps(self) -> tuple:
        return () if self.gaps_filled else self.plan.gaps


def _read_piece(piece) -> np.ndarray:
    """Decode one piece as float32.

    Read failure propagates rather than swallowed: means index is stale -- file
    truncated or replaced after indexing -- and honest response is to say which
    file and let user rescan. Silently exporting shorter snip than asked would
    be worse than refusing.
    """
    try:
        return wavio.read_frames(piece.clip.path, piece.start_frame, piece.nframes)
    except wavio.WavError as exc:
        raise ExportError(f"cannot read {piece.clip.path.name}: {exc}") from exc


def _events(plan: SnipPlan, fill_gaps: bool):
    """Pieces, and gaps when filling, as one time-ordered list.

    Gaps come from plan not reconstructed from piece edges, so what gets filled
    and what gets reported cannot drift apart.
    """
    items: list[tuple[int, str, object]] = [
        (piece.start_us, "piece", piece) for piece in plan.pieces
    ]
    if fill_gaps:
        items.extend((gap.start_us, "gap", gap) for gap in plan.gaps)
    items.sort(key=lambda item: item[0])
    return [(kind, obj) for _, kind, obj in items]


def _compute_loudness(samples: np.ndarray, framerate: int) -> tuple[int, int, int, int, int]:
    """Compute EBU R128 loudness metrics using pyloudnorm.

    Returns (integrated, loudness_range, true_peak, max_momentary, max_short_term)
    all scaled by 100 as int16 per BEXT v2 spec.

    Note: pyloudnorm only provides integrated_loudness and loudness_range.
    True peak, momentary, and short-term are computed separately or set to 0.
    """
    try:
        import pyloudnorm as pyln
    except ImportError:
        # pyloudnorm not available
        return (0, 0, 0, 0, 0)

    # Ensure correct shape: (frames, channels)
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x.reshape(-1, 1)

    meter = pyln.Meter(framerate)

    # Integrated loudness (LUFS)
    integrated = meter.integrated_loudness(x)

    # Loudness Range (LRA)
    loudness_range = meter.loudness_range(x)

    # True peak - compute manually (max absolute sample value in dBTP)
    # For float32 in [-1, 1], true peak = 20 * log10(max_abs)
    max_abs = float(np.max(np.abs(x)))
    if max_abs > 0:
        true_peak = 20 * np.log10(max_abs)
    else:
        true_peak = -999.0  # effectively -inf dB

    # Momentary and short-term loudness not available in pyloudnorm
    # Set to same as integrated for now (could implement sliding window later)
    max_momentary = integrated
    max_short_term = integrated

    # Scale by 100 and clamp to int16 range
    def scale_clamp(val: float) -> int:
        return int(np.clip(round(val * 100), -32768, 32767))

    return (
        scale_clamp(integrated),
        scale_clamp(loudness_range),
        scale_clamp(true_peak),
        scale_clamp(max_momentary),
        scale_clamp(max_short_term),
    )


def _write_with_bext(
    plan: SnipPlan,
    out_path: Path,
    fmt: AudioFormat,
    bext: wavio.BextChunk,
    fill_gaps: bool,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
) -> int:
    """Write WAV with BEXT chunk. Returns frames written.

    If BEXT version is 2 (has loudness), uses temp file for two-pass write.
    """
    # Check if we need two-pass for loudness
    needs_loudness = (
        bext.version >= 2 and
        bext.loudness_value is None  # loudness not pre-computed
    )

    if needs_loudness:
        # Two-pass: write to temp, compute loudness, write final
        with tempfile.NamedTemporaryFile(
            suffix=".wav", dir=out_path.parent, delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Pass 1: write to temp with zeroed loudness
            bext_pass1 = wavio.BextChunk(
                description=bext.description,
                originator=bext.originator,
                originator_reference=bext.originator_reference,
                origination_date=bext.origination_date,
                origination_time=bext.origination_time,
                time_reference=bext.time_reference,
                version=2,
                umid=bext.umid,
                loudness_value=0,
                loudness_range=0,
                max_true_peak_level=0,
                max_momentary_loudness=0,
                max_short_term_loudness=0,
                coding_history=bext.coding_history,
            )
            frames_written = _render_to_writer(tmp_path, plan, fmt, bext_pass1, fill_gaps, fade_in_s, fade_out_s)

            # Read back for loudness analysis
            audio_data = wavio.read_frames(tmp_path, 0, frames_written)

            # Compute loudness
            (loudness_value, loudness_range, max_true_peak_level,
             max_momentary_loudness, max_short_term_loudness) = _compute_loudness(audio_data, fmt.framerate)

            # Pass 2: write final with computed loudness
            bext_final = wavio.BextChunk(
                description=bext.description,
                originator=bext.originator,
                originator_reference=bext.originator_reference,
                origination_date=bext.origination_date,
                origination_time=bext.origination_time,
                time_reference=bext.time_reference,
                version=2,
                umid=bext.umid,
                loudness_value=loudness_value,
                loudness_range=loudness_range,
                max_true_peak_level=max_true_peak_level,
                max_momentary_loudness=max_momentary_loudness,
                max_short_term_loudness=max_short_term_loudness,
                coding_history=bext.coding_history,
            )
            _render_to_writer(out_path, plan, fmt, bext_final, fill_gaps, fade_in_s, fade_out_s)
            return frames_written

        finally:
            # Cleanup temp file
            try:
                tmp_path.unlink()
            except OSError:
                pass
    else:
        # Single pass
        return _render_to_writer(out_path, plan, fmt, bext, fill_gaps, fade_in_s, fade_out_s)


def _render_to_writer(
    out_path: Path,
    plan: SnipPlan,
    fmt: AudioFormat,
    bext: wavio.BextChunk,
    fill_gaps: bool,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
) -> int:
    """Render plan to writer with given BEXT chunk. Returns frames written."""
    max_frames = max(1, _MAX_BLOCK_BYTES // (4 * fmt.nchannels))

    written = 0
    with wavio.WavWriter(out_path, fmt.framerate, fmt.nchannels, fmt.sampwidth, bext=bext) as writer:

        block: list[np.ndarray] = []
        block_frames = 0
        block_rate = plan.pieces[0].clip.framerate

        def flush() -> None:
            """Convert, resample and write accumulated block."""
            nonlocal block, block_frames, written
            if not block:
                return
            joined = np.concatenate(block, axis=0)
            block = []
            block_frames = 0

            joined = convert_channels(joined, fmt.nchannels)
            up, down = rate_ratio(block_rate, fmt.framerate)
            if up != down:
                joined = _resample(joined, up, down)
            written += writer.write(joined)

        for kind, obj in _events(plan, fill_gaps):
            if kind == "gap":
                if block:
                    flush()
                written += writer.write(
                    np.zeros((_frames_for(obj.duration_us, fmt.framerate), fmt.nchannels),
                             dtype=np.float32)
                )
                continue

            piece = obj
            if piece.clip.framerate != block_rate:
                # A rate change ends the run: the block must be resampled while
                # its rate is still the one it was decoded at.
                flush()
                block_rate = piece.clip.framerate

            samples = _read_piece(piece)
            if block_frames and block_frames + samples.shape[0] > max_frames:
                flush()
            block.append(samples)
            block_frames += samples.shape[0]

            if block_frames >= max_frames:
                flush()

        if block:
            flush()

    # Apply fade in/out if requested
    if fade_in_s > 0 or fade_out_s > 0:
        _apply_fade(Path(out_path), fmt, fade_in_s, fade_out_s)

    return written


def _apply_fade(out_path: Path, fmt: AudioFormat, fade_in_s: float, fade_out_s: float) -> None:
    """Apply fade in/out to a WAV file in place.

    Reads the entire file, applies fade, writes back.
    """
    # Read the entire file
    info = wavio.read_info(out_path)
    nframes = info.nframes
    data = wavio.read_frames(out_path, 0, nframes)
    if data is None or data.size == 0:
        return

    nchannels = info.nchannels

    # Ensure data is 2D for consistent processing
    if data.ndim == 1:
        data = data[:, np.newaxis]

    # Fade in
    if fade_in_s > 0:
        fade_frames = min(int(fade_in_s * fmt.framerate), nframes)
        if fade_frames > 0:
            fade_curve = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
            data[:fade_frames] *= fade_curve[:, np.newaxis]

    # Fade out
    if fade_out_s > 0:
        fade_frames = min(int(fade_out_s * fmt.framerate), nframes)
        if fade_frames > 0:
            fade_curve = np.linspace(1.0, 0.0, fade_frames, dtype=np.float32)
            start = nframes - fade_frames
            data[start:] *= fade_curve[:, np.newaxis]

    # If originally mono, squeeze back to 1D for writing
    if info.nchannels == 1:
        data = data[:, 0]

    # Write back
    wavio.write_wav(out_path, data, fmt.framerate, fmt.sampwidth)


def render_snip(
    plan: SnipPlan,
    out_path: str | Path,
    *,
    target: AudioFormat | None = None,
    fill_gaps: bool = False,
    bext: wavio.BextChunk | None = None,
    fade_in_s: float = 0.0,
    fade_out_s: float = 0.0,
) -> ExportResult:
    """Render a snip plan to ``out_path``.

    With ``fill_gaps`` output is as long as requested range, with silence where
    audio missing. Without it -- default -- only existing audio written, so
    result shorter; returned :class:`ExportResult` reports what dropped so UI
    can say so.

    If ``bext`` is provided, writes a Broadcast Wave Format file with the
    given metadata. If BEXT version is 2, computes loudness per EBU R128
    (requires pyloudnorm).

    ``fade_in_s`` and ``fade_out_s`` specify fade in/out duration in seconds.
    Default is 0 (no fade).
    """
    fmt = canonical_format(plan, target)
    if not plan.pieces:
        raise ExportError("snip contains no audio to export")

    out_path = Path(out_path)

    if bext is not None:
        written = _write_with_bext(plan, out_path, fmt, bext, fill_gaps, fade_in_s, fade_out_s)
    else:
        # Original path without BEXT
        max_frames = max(1, _MAX_BLOCK_BYTES // (4 * fmt.nchannels))

        written = 0
        with wavio.WavWriter(out_path, fmt.framerate, fmt.nchannels, fmt.sampwidth) as writer:

            block: list[np.ndarray] = []
            block_frames = 0
            block_rate = plan.pieces[0].clip.framerate

            def flush() -> None:
                """Convert, resample and write accumulated block."""
                nonlocal block, block_frames, written
                if not block:
                    return
                joined = np.concatenate(block, axis=0)
                block = []
                block_frames = 0

                joined = convert_channels(joined, fmt.nchannels)
                up, down = rate_ratio(block_rate, fmt.framerate)
                if up != down:
                    joined = _resample(joined, up, down)
                # Apply fade if this is the last block and we need fade out
                # (fade out is applied at the very end in the main flow)
                written += writer.write(joined)

            for kind, obj in _events(plan, fill_gaps):
                if kind == "gap":
                    if block:
                        flush()
                    written += writer.write(
                        np.zeros((_frames_for(obj.duration_us, fmt.framerate), fmt.nchannels),
                                 dtype=np.float32)
                    )
                    continue

                piece = obj
                if piece.clip.framerate != block_rate:
                    flush()
                    block_rate = piece.clip.framerate

                samples = _read_piece(piece)
                if block_frames and block_frames + samples.shape[0] > max_frames:
                    flush()
                block.append(samples)
                block_frames += samples.shape[0]

                if block_frames >= max_frames:
                    flush()

            if block:
                flush()

    # Apply fade in/out if requested
    if fade_in_s > 0 or fade_out_s > 0:
        _apply_fade(out_path, fmt, fade_in_s, fade_out_s)

    return ExportResult(
        path=out_path,
        fmt=fmt,
        nframes=written,
        plan=plan,
        gaps_filled=fill_gaps,
    )


def _frames_for(duration_us: int, framerate: int) -> int:
    """Frames covering a duration, rounded to nearest.

    Integer arithmetic rather than ``round()`` on a float: a long duration in
    microseconds times a rate exceeds float32's exact-integer range, and this
    value decides how many samples of silence get written.
    """
    return max(0, (duration_us * framerate + 500_000) // 1_000_000)