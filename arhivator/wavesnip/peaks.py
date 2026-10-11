"""Turning audio into something a widget can draw, without any drawing.

The waveform view needs per horizontal pixel: loudest/quietest sample in that
pixel's time slice, the typical level so a wide window doesn't saturate to a
solid block, and which pixels have audio at all. Computed here as plain NumPy
because it's the part most likely to be subtly wrong and a screenshot would
never reveal.

**Min/max, not RMS, for the outline.** An envelope from RMS loses the point of
a waveform: a single transient spike and a loud square wave of the same energy
look identical, and the spike is what the eye seeks. Min/max preserves it.

**RMS inside the outline, as a band.** Min/max fails at the other zoom end:
over a wide window nearly every column contains a transient, so the outline is
full height everywhere and the one thing a wide view should show -- how
loudness varies -- disappears into a solid block. The inner band carries that.

**A column counts as covered if *any* part of it has audio.** Sampling a
column's midpoint is cheaper and wrong: zoomed out, one pixel can span a day,
and a one-hour recording in the middle would be missed if the midpoint fell in
a gap.

**Reading audio to build an envelope lives here too**, not in the window that
asks for it, for the same reason :mod:`wavesnip.cursor` is separate from
:mod:`wavesnip.player`: this is the part most worth testing, and a test needing
Qt to reach it silently stops running on machines without Qt. Plain NumPy over
a read callback needs neither.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from wavesnip.filepeaks import FilePeakCache, FilePeaks

__all__ = [
    "MAX_EXACT_FRAMES",
    "MAX_PEAK_FRAMES",
    "PEAK_CHUNK_FRAMES",
    "PEAK_COLUMNS",
    "PROBE_FRAMES_PER_COLUMN",
    "PROBE_FRAMES_PER_RUN",
    "PROBE_RUNS_PER_COLUMN",
    "Peaks",
    "column_edges",
    "compute_peaks",
    "covered_columns",
    "peaks_for",
    "reduce_piece",
]


#: Windows up to this many frames are read whole instead of sampled.
#:
#: Cost, not fidelity: a full read of this many frames is ~95 ms (16M was 63 ms),
#: expressed in frames so threshold means same at 8 kHz and 48 kHz. Also covers
#: just past the per-file envelope cache's narrowest served view
#: (``PEAK_COLUMNS * CACHE_STEP_US``): a view too narrow for the cache is read
#: whole rather than sent to sampled path (flat cost) -- so this is where reading
#: stops being cheaper than probing, not where a limit is hit.
MAX_EXACT_FRAMES = 24_000_000

#: How many frames a waveform read may touch across the whole visible range.
#:
#: Backstop on one request: exact path never exceeds ``MAX_EXACT_FRAMES`` and
#: sampled path reads at most ``PROBE_FRAMES_PER_COLUMN`` per column. It stays
#: because "whole request shares one budget" is a property worth a number --
#: handing every piece the full allowance made 900 hourly chunks cost 900x work.
MAX_PEAK_FRAMES = 200_000_000

#: Frames held in memory at once while reducing. Bounds peak RAM regardless of
#: clip length: 8M frames = 32 MB float32 mono, 64 MB stereo.
PEAK_CHUNK_FRAMES = 8_000_000

#: Columns in the drawn envelope. Wider than any plausible widget; redraw on
#: resize still has detail; painter uses one column per pixel.
PEAK_COLUMNS = 1600

#: Probes per column on sampled path: ``PROBE_FRAMES_PER_COLUMN`` frames per
#: column, whatever it covers in time.
#:
#: 8 runs of 128 frames is the measured trade middle: at 48 kHz a run spans
#: 2.7 ms, enough to catch a snare in an hour-long column; whole request tops
#: out at 1.64M frames (~3.3 MB, ~184 ms), flat with window width. Fewer probes
#: lose transients; more runs cost opens/seeks for detail nobody sees at that zoom.
PROBE_RUNS_PER_COLUMN = 8
PROBE_FRAMES_PER_RUN = 128
PROBE_FRAMES_PER_COLUMN = PROBE_RUNS_PER_COLUMN * PROBE_FRAMES_PER_RUN


@dataclass(frozen=True)
class Peaks:
    """Per-column minimum, maximum and typical level, for drawing an envelope.

    ``rms`` is ``None`` for a hand-built envelope with no band to draw; the
    paths in this module always fill it. The painter skips a column whose band
    is missing or NaN, so ``None`` means "outline only", not "silence".
    """

    mins: np.ndarray
    maxs: np.ndarray
    frames_per_bucket: float
    rms: np.ndarray | None = None

    def __len__(self) -> int:
        return int(self.mins.shape[0])

    def is_silent(self) -> bool:
        """True when every column is flat zero -- silence or a file of zeros."""
        return bool(np.all(self.mins == 0) and np.all(self.maxs == 0))


def _reduce_blocks(data: np.ndarray, buckets: int):
    """Fold ``(frames, channels)`` audio into ``buckets`` columns.

    Returns raw moments ``(mins, maxs, sumsq, counts, frames_per_bucket)`` --
    not RMS, because whoever merges chunks must merge weighted by samples per
    column, and a second implementation would eventually disagree.
    """
    nframes = int(data.shape[0])
    if nframes < buckets:
        # Zoomed in past one frame per column. Repeat each frame across columns
        # it covers instead of padding with silence: alternative would draw an
        # audio burst as spikes separated by gaps never in the recording.
        repeats = buckets // nframes
        remainder = buckets - repeats * nframes
        data = np.concatenate(
            [np.repeat(data, repeats, axis=0), data[:remainder]], axis=0
        )
        nframes = buckets

    per_bucket = max(1, nframes // buckets)
    usable = per_bucket * buckets

    blocks = data[:usable].reshape(buckets, per_bucket, -1)
    mins = blocks.min(axis=(1, 2))
    maxs = blocks.max(axis=(1, 2))
    # einsum rather than ``blocks * blocks``: same sum, no full-size temporary,
    # matters at 8M-frame chunk where copy would rival chunk.
    sumsq = np.einsum("ijk,ijk->i", blocks, blocks)
    counts = np.full(buckets, per_bucket * blocks.shape[2], dtype=np.int64)

    # Fold ragged tail into final column rather than dropping it: clip ending
    # in loud transient should not have that transient discarded.
    if usable < nframes:
        tail = data[usable:]
        mins[-1] = min(mins[-1], float(tail.min()))
        maxs[-1] = max(maxs[-1], float(tail.max()))
        sumsq[-1] += float(np.square(tail).sum())
        counts[-1] += tail.shape[0] * blocks.shape[2]

    return mins, maxs, sumsq, counts, nframes / buckets


def compute_peaks(samples: np.ndarray, buckets: int) -> Peaks:
    """Reduce ``(frames, channels)`` audio to ``buckets`` min/max pairs.

    Channels folded together, not averaged: averaging 5.1 mix can cancel a
    channel against neighbour and show silence where file is loud. View shows
    range listener could hear, so extreme over all channels is honest answer.

    Frames not dividing evenly into buckets folded into last one. Remainder <
    ``buckets`` frames (~16 ms at 48 kHz across whole clip) -- widening one
    column by that much cannot be seen.
    """
    if buckets <= 0:
        raise ValueError("buckets must be positive")

    data = np.asarray(samples)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    elif data.ndim != 2:
        raise ValueError(f"expected 1-D or 2-D samples, got {data.ndim}-D")

    if int(data.shape[0]) == 0:
        empty = np.zeros(buckets, dtype=np.float32)
        return Peaks(empty, empty.copy(), 0.0, empty.copy())

    mins, maxs, sumsq, counts, frames_per_bucket = _reduce_blocks(data, buckets)
    rms = np.sqrt(sumsq / counts).astype(np.float32, copy=False)
    return Peaks(
        mins.astype(np.float32, copy=False),
        maxs.astype(np.float32, copy=False),
        frames_per_bucket,
        rms,
    )


def column_edges(start_us: int, end_us: int, width: int) -> np.ndarray:
    """``width + 1`` integer boundaries splitting ``[start, end)`` evenly.

    Integer arithmetic, so a column boundary is the same instant no matter how
    the widget was resized, and a playhead at that instant lands in exactly one
    column instead of drifting between two.
    """
    if width <= 0:
        raise ValueError("width must be positive")
    if end_us <= start_us:
        raise ValueError("end must be after start")

    span = end_us - start_us
    return start_us + (span * np.arange(width + 1, dtype=np.int64)) // width


def covered_columns(
    coverage: list[tuple[int, int]], start_us: int, end_us: int, width: int
) -> np.ndarray:
    """A boolean per column: does any part of it have audio?

    Uses a difference array so the whole view costs one pass over the runs that
    touch it, rather than a search per column. Matters because a year of hourly
    chunks is a few thousand runs and a wide window can be thousands of columns.
    """
    edges = column_edges(start_us, end_us, width)
    covered = np.zeros(width, dtype=bool)
    if not coverage:
        return covered

    starts = np.fromiter((s for s, _ in coverage), dtype=np.int64, count=len(coverage))
    # Exclusive ends, but an end instant is still a boundary the run reaches.
    ends = np.fromiter((e for _, e in coverage), dtype=np.int64, count=len(coverage))

    touching = (ends > start_us) & (starts < end_us)
    if not touching.any():
        return covered

    run_starts = starts[touching]
    run_ends = ends[touching]

    # The last instant a run actually covers is end-1: intervals are half-open,
    # so `end` belongs to whatever comes next, and a gap in between, if any.
    first = np.searchsorted(edges, run_starts, side="right") - 1
    last = np.searchsorted(edges, np.maximum(run_ends - 1, run_starts), side="right") - 1

    first = np.clip(first, 0, width - 1)
    last = np.clip(last, 0, width - 1)

    delta = np.zeros(width + 1, dtype=np.int32)
    np.add.at(delta, first, 1)
    np.add.at(delta, last + 1, -1)
    return np.cumsum(delta)[:width] > 0


def peaks_for(
    plan,
    max_frames: int = MAX_PEAK_FRAMES,
    exact_frames: int = MAX_EXACT_FRAMES,
    cache: FilePeakCache | None = None,
) -> Peaks | None:
    """Decode a snip plan down to an envelope.

    Reads audio at native rates (no resampling first -- envelope doesn't need
    rate, resampling multi-hour window to throw away samples is wasted work).

    Three paths, chosen by window cost:

    * **Cached**, when ``cache`` given and view wide enough that every visible
      column resolves a whole cache column (``span >= PEAK_COLUMNS * CACHE_STEP_US``).
      Each piece's covered range built into file's per-file envelope if missing --
      one sequential forward sweep, read once -- envelope assembled from RAM. This
      stops spinning disk from taking minutes: never seeks, never re-reads what
      previous view cached.

    * **Exact**, up to ``exact_frames`` frames. Every frame read and min/max'd,
      zoomed-in views bit-exact. Piece's column share proportional to duration
      from *requested* range, so 900 hourly chunks and one long file look same,
      gaps drawn as silence not stretched. But floor-proportional shares can hand
      piece zero columns when window far wider than piece -- piece reduction never
      sees is missing from envelope with no signal -- so such plans go to sampled
      path even though reading would be cheap.

    * **Sampled**, above that. Reading wide window whole made waveform slow: widest
      view of 48 kHz archive was ~400 MB I/O per repaint. Probing
      ``PROBE_FRAMES_PER_COLUMN`` frames per column costs same at every zoom and
      sample rate, so gate on showing waveform can be a line in *time* not a frame
      budget meaning different seconds at 8 kHz vs 48 kHz. With ``cache`` this is
      backstop for never-seen files; without it, wide-view path.

    **Reduced piecewise, never concatenated, on all paths.** Obvious impl reads
    every piece, joins into one array before reducing -- memory scales with view
    width (48 kHz stereo hour = 690 MB float32, 7-hour window = several GB).
    Here each piece/probe/cached column folded into its columns as read, so only
    one chunk held in memory regardless of window length.

    ``max_frames`` is budget for whole request, not per piece. Handing every piece
    full allowance made 900 hourly chunks cost 900x work. Cached path deliberately
    doesn't take that budget: its reads are one sequential sweep per covered range,
    the sweep that pays for every later view; capping at per-request budget would
    re-seek a range the moment view moved past cap.
    """
    if plan.nframes == 0:
        return None

    total_us = plan.end_us - plan.start_us
    if total_us <= 0:
        return None

    if cache is not None and total_us >= PEAK_COLUMNS * cache.step_us:
        entries = [cache.envelope_for(piece) for piece in plan.pieces]
        if all(entry is not None for entry in entries):
            return _peaks_from_cache(plan, entries)

    if plan.nframes <= exact_frames and _each_piece_wins_a_column(plan, total_us):
        return _peaks_exact(plan, max_frames)
    return _peaks_probed(plan, max_frames)


def _each_piece_wins_a_column(plan, total_us: int) -> bool:
    """Could the proportional split allot some piece no columns at all?

    Shares are ``floor``s of duration ratios, so a piece shorter than one
    column's worth of the requested range can floor to zero and drop out of the
    envelope entirely. Requiring the shortest piece to span a whole column
    rules that out (and with it the quiet failure where a sparse archive's only
    recording happens to land between two boundaries).
    """
    shortest = min((piece.end_us - piece.start_us) for piece in plan.pieces)
    return shortest > 0 and total_us <= PEAK_COLUMNS * shortest


def _peaks_exact(plan, max_frames: int) -> Peaks | None:
    """Read every frame the plan covers, piece by piece, and reduce it.

    ``reduce_piece`` returns the min, max and RMS for the piece's share of the
    columns, plus what it spent; this merges them into the whole window by
    position, a column with no frames keeping the NaN that marks "skip me".
    """
    from wavesnip.wavio import read_frames

    total_us = plan.end_us - plan.start_us
    mins = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    maxs = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    rms = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    any_audio = False
    budget = max_frames

    for piece in plan.pieces:
        # How many columns this piece gets, from where in the requested range it
        # starts. Flooring both ends keeps the shares contiguous and summing to no
        # more than the total.
        first = PEAK_COLUMNS * (piece.start_us - plan.start_us) // total_us
        last = PEAK_COLUMNS * (piece.end_us - plan.start_us) // total_us
        share = last - first
        if share <= 0 or budget <= 0:
            continue

        block_min, block_max, block_rms, spent = reduce_piece(
            read_frames, piece, share, budget
        )
        budget -= spent
        if block_min is None:
            continue

        any_audio = True
        # A column with no frames keeps its NaN, which the painter skips.
        known = ~np.isnan(block_min)
        window_mins = mins[first:last]
        window_maxs = maxs[first:last]
        window_rms = rms[first:last]
        window_mins[known] = block_min[known]
        window_maxs[known] = block_max[known]
        window_rms[known] = block_rms[known]
        mins[first:last] = window_mins
        maxs[first:last] = window_maxs
        rms[first:last] = window_rms

    if not any_audio:
        return None
    return Peaks(mins, maxs, 0.0, rms)


def _peaks_from_cache(plan, entries: list[FilePeaks | None]) -> Peaks | None:
    """Assemble envelope from per-file caches, a piece at a time.

    Each cached column mapped to view column whose time range holds cache
    column's midpoint, then merged with min/max and additive sumsq/counts --
    same moments arithmetic as exact and sampled paths, so three paths cannot
    drift into different band shapes. Mapping by absolute time (not exact path's
    proportional shares) lets a piece far narrower than a view column still land
    in its own columns on this path.

    Columns attributed whole cache columns, so two adjacent pieces can write one
    view column; merging not wiping keeps transient at seam visible -- same
    reason ``_fold_probes`` merges. Cache columns for a piece arrive in frame
    order, hence view columns never decrease, so each piece folds with one
    ``reduceat`` per moment.
    """
    total_us = plan.end_us - plan.start_us
    edges = column_edges(plan.start_us, plan.end_us, PEAK_COLUMNS)
    mins = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    maxs = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    sumsq = np.zeros(PEAK_COLUMNS, dtype=np.float64)
    counts = np.zeros(PEAK_COLUMNS, dtype=np.int64)

    for piece, entry in zip(plan.pieces, entries):
        if entry is None:
            continue
        fpc = entry.frames_per_col
        f0 = min(piece.start_frame, entry.nframes)
        f1 = min(piece.end_frame, entry.nframes)
        if f1 <= f0:
            continue
        j0 = f0 // fpc
        j1 = (f1 - 1) // fpc
        if j1 < j0:
            continue

        j = np.arange(j0, j1 + 1, dtype=np.int64)
        # Map each cache column by midpoint of frames it covers. Ragged last
        # column's midpoint can lie past last frame; clamp so short file's audio
        # not flung a full column into future.
        mid = np.minimum(j * fpc + fpc // 2, entry.nframes - 1)
        mid_us = piece.clip.start_us + mid * 1_000_000 // entry.rate
        col = np.searchsorted(edges, mid_us, side="right") - 1
        np.clip(col, 0, PEAK_COLUMNS - 1, out=col)

        c_min = entry.mins[j]
        c_max = entry.maxs[j]
        c_sum = entry.sumsq[j].astype(np.float64)
        c_cnt = entry.column_counts(j).astype(np.int64)

        first = np.flatnonzero(np.r_[True, np.diff(col) != 0])
        g = col[first]
        gm = np.minimum.reduceat(c_min, first)
        gx = np.maximum.reduceat(c_max, first)
        gs = np.add.reduceat(c_sum, first)
        gc = np.add.reduceat(c_cnt, first)

        fresh = np.isnan(mins[g])
        mins[g[fresh]] = gm[fresh]
        maxs[g[fresh]] = gx[fresh]
        # Read-compute-assign, not ``out=mins[settled]``: fancy indexing yields
        # a *copy*, so ``out=`` would merge into copy and discard it. (Same
        # reason ``_fold_probes`` is written this way.)
        settled = g[~fresh]
        if settled.size:
            mins[settled] = np.minimum(mins[settled], gm[~fresh])
            maxs[settled] = np.maximum(maxs[settled], gx[~fresh])
        sumsq[g] += gs
        counts[g] += gc

    if not counts.any():
        return None

    rms = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        band = np.sqrt(sumsq / np.maximum(counts, 1))
    rms[counts > 0] = band[counts > 0].astype(np.float32)
    return Peaks(mins, maxs, 0.0, rms)


def _peaks_probed(plan, max_frames: int) -> Peaks | None:
    """Envelope built by sampling, for windows too wide to read whole.

    Three deliberate properties:

    **Cost does not scale with window.** Probes laid out *per column*,
    ``PROBE_FRAMES_PER_COLUMN`` frames of sampling wherever column points in
    time, so day-long and year-long view read same few MB. That let the gate on
    showing waveform become "is view wider than a week" not "would this view
    decode in 200M frames", which meant different seconds at different rates.

    **Column-major, not piece-major.** Exact path works piece by piece, allots
    columns by duration, drops pieces narrower than a column. Here questions
    asked the other way: for each column, is anything in it, and if so sample
    it -- so one-hour clip inside day-wide column contributes even though clip
    is rounding error against window.

    **Covered column never left blank.** Probes are stratified midpoints, which
    can straddle narrow recording on both sides; when no probe lands in piece
    intersecting column, one more probe goes at centre of overlap. Without it
    sparse archive would show coverage blocks with no envelope -- view claiming
    audio is there while drawing silence over it. Columns nothing intersects
    never probed, so gap stays a gap.

    All probes for one file read through ``wavio.read_probes``, which opens it
    once: ``read_frames`` reopens per probe, measured here made 6400 probes
    *slower* (513 ms) than reading whole file (229 ms), because opens dominate
    seeks.
    """
    from wavesnip.wavio import read_probes

    total_us = plan.end_us - plan.start_us
    columns = np.arange(PEAK_COLUMNS, dtype=np.int64)
    col_start = plan.start_us + total_us * columns // PEAK_COLUMNS
    col_end = plan.start_us + total_us * (columns + 1) // PEAK_COLUMNS

    # Stratified midpoints: spread across the column rather than packed at its
    # start, so probes do not systematically sample one side of every boundary.
    runs = (2 * np.arange(PROBE_RUNS_PER_COLUMN, dtype=np.int64) + 1)[None, :]
    instants = col_start[:, None] + ((col_end - col_start)[:, None] * runs) // (
        2 * PROBE_RUNS_PER_COLUMN
    )

    # Pieces sorted by start: plan pieces never overlap in time, so the last
    # piece starting before an instant is the only candidate it can be inside.
    pieces = sorted(plan.pieces, key=lambda piece: piece.start_us)
    starts = np.fromiter(
        (piece.start_us for piece in pieces), dtype=np.int64, count=len(pieces)
    )
    ends = np.fromiter(
        (piece.end_us for piece in pieces), dtype=np.int64, count=len(pieces)
    )

    flat = instants.ravel()
    owner = np.repeat(columns, PROBE_RUNS_PER_COLUMN)
    where = np.searchsorted(starts, flat, side="right") - 1
    landed = where >= 0
    candidate = np.where(landed, where, 0)
    landed &= flat < ends[candidate]

    keep_col = owner[landed]
    keep_t = flat[landed]
    keep_piece = candidate[landed]

    # Columns where every probe missed: one probe at the centre of whatever
    # piece overlaps them, so the envelope and the coverage blocks agree.
    missed = np.unique(owner[~landed])
    if missed.size:
        nearest = np.searchsorted(starts, col_end[missed], side="left") - 1
        intersects = nearest >= 0
        chosen = np.where(intersects, nearest, 0)
        intersects &= ends[chosen] > col_start[missed]
        fallback_col = missed[intersects]
        fallback_piece = chosen[intersects]
        low = np.maximum(starts[fallback_piece], col_start[fallback_col])
        high = np.minimum(ends[fallback_piece], col_end[fallback_col])
        fallback_t = low + (high - low) // 2
        keep_col = np.concatenate([keep_col, fallback_col])
        keep_t = np.concatenate([keep_t, fallback_t])
        keep_piece = np.concatenate([keep_piece, fallback_piece])

    # One entry per probe run, grouped by file so each file is opened once.
    # Insertion order follows column order, so two identical requests read the
    # same probes in the same order and come out bit-identical.
    groups: dict = {}
    for col, instant, index in zip(keep_col.tolist(), keep_t.tolist(), keep_piece.tolist()):
        piece = pieces[index]
        frame = piece.clip.frame_at(instant)
        frame = min(max(frame, piece.start_frame), piece.end_frame)
        count = min(PROBE_FRAMES_PER_RUN, piece.end_frame - frame)
        if count <= 0:
            continue
        groups.setdefault(piece.path, []).append((int(col), frame, count))

    mins = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    maxs = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    sumsq = np.zeros(PEAK_COLUMNS, dtype=np.float64)
    counts = np.zeros(PEAK_COLUMNS, dtype=np.int64)
    budget = max_frames

    for path, requests in groups.items():
        fits = []
        for entry in requests:
            if budget - entry[2] < 0:
                break
            fits.append(entry)
            budget -= entry[2]
        if not fits:
            break

        blocks = read_probes(path, [(frame, count) for _, frame, count in fits])
        _fold_probes(fits, blocks, mins, maxs, sumsq, counts)

    if not counts.any():
        return None

    rms = np.full(PEAK_COLUMNS, np.nan, dtype=np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        band = np.sqrt(sumsq / np.maximum(counts, 1))
    rms[counts > 0] = band[counts > 0].astype(np.float32)
    return Peaks(mins, maxs, 0.0, rms)


def _fold_probes(fits, blocks, mins, maxs, sumsq, counts) -> None:
    """Merge one file's probe runs into their columns of the envelope.

    Two shapes of merge: runs of *same* column combine into that column's
    moments; a column a *previous* file already wrote (two clips either side of
    a boundary land in one column) merges with what's there, because a wipe
    would show only later clip's samples.

    Vectorised not per-run: measured, at widest dense view the per-run
    ``min``/``max``/``sum`` calls cost more than the file read they accompany --
    12800 runs, each a handful of numpy calls. Runs arrive in column order but
    a column's runs need not be adjacent (fallback probe appended after landed
    probes), so sorted by column first; then one ``reduceat`` per moment does
    whole file. Sort is stable, so two identical requests fold identically.
    """
    keep = [
        (col, block)
        for (col, _frame, _count), block in zip(fits, blocks)
        if block.size
    ]
    if not keep:
        return

    cols = np.fromiter((col for col, _ in keep), dtype=np.int64, count=len(keep))
    order = np.argsort(cols, kind="stable")
    cols = cols[order]
    runs = [keep[int(index)][1] for index in order]
    run_counts = np.fromiter(
        (block.shape[0] for block in runs), dtype=np.int64, count=len(runs)
    )

    if all(block.shape == runs[0].shape for block in runs):
        stacked = np.stack(runs)
        run_mins = stacked.min(axis=(1, 2))
        run_maxs = stacked.max(axis=(1, 2))
        # float32 accumulation over at most PROBE_FRAMES_PER_RUN frames: plenty
        # for a level to draw, and skips a float64 copy of the stack.
        run_sumsq = np.einsum("pkc,pkc->p", stacked, stacked).astype(np.float64)
    else:
        # Last run clipped short by end of piece. Rare, so correctness first:
        # the wide-open arithmetic the per-run loop always did.
        run_mins = np.fromiter((float(b.min()) for b in runs), dtype=np.float64, count=len(runs))
        run_maxs = np.fromiter((float(b.max()) for b in runs), dtype=np.float64, count=len(runs))
        run_sumsq = np.fromiter(
            (float(np.square(b, dtype=np.float64).sum()) for b in runs),
            dtype=np.float64,
            count=len(runs),
        )

    first = np.flatnonzero(np.r_[True, np.diff(cols) != 0])
    col_ids = cols[first]
    col_mins = np.minimum.reduceat(run_mins, first)
    col_maxs = np.maximum.reduceat(run_maxs, first)
    col_sumsq = np.add.reduceat(run_sumsq, first)
    col_counts = np.add.reduceat(run_counts, first)

    fresh = np.isnan(mins[col_ids])
    mins[col_ids[fresh]] = col_mins[fresh]
    maxs[col_ids[fresh]] = col_maxs[fresh]
    settled = col_ids[~fresh]
    if settled.size:
        mins[settled] = np.minimum(mins[settled], col_mins[~fresh])
        maxs[settled] = np.maximum(maxs[settled], col_maxs[~fresh])
    # Unique after sort, so plain fancy assignment is the merge -- and fresh
    # columns above were NaN, which ``np.minimum`` would have let spread.
    sumsq[col_ids] += col_sumsq
    counts[col_ids] += col_counts


def reduce_piece(read_frames, piece, share: int, max_frames: int):
    """Min/max/RMS for one piece's share of columns, one chunk at a time.

    ``share`` columns covering ``piece.nframes`` frames. Chunking matters for
    long clips: without it a single 48 kHz stereo hour would be read whole,
    and peak memory set by longest clip in archive rather than what user chose
    to look at.

    Returns ``(mins, maxs, rms, spent)`` -- or ``None`` for all three when
    nothing to read -- **and how many frames actually read**. Last value not
    a convenience: budget belongs to whole request, so caller must subtract
    what this piece spent rather than assume it spent allowance or nothing.
    RMS merges weighted by sample count internally, so caller just places it
    like the other two.
    """
    nframes = piece.nframes
    if nframes <= 0 or share <= 0:
        return None, None, None, 0

    mins = np.full(share, np.nan, dtype=np.float32)
    maxs = np.full(share, np.nan, dtype=np.float32)
    sumsq = np.zeros(share, dtype=np.float64)
    counts = np.zeros(share, dtype=np.int64)

    budget = max_frames
    done = 0
    while done < nframes and budget > 0:
        take = min(PEAK_CHUNK_FRAMES, nframes - done, budget)
        if take <= 0:
            break
        block = read_frames(piece.clip.path, piece.start_frame + done, take)
        if block.shape[0] == 0:
            break

        # Which columns this chunk lands in, by frame position within the piece.
        chunk_first = share * done // nframes
        chunk_last = share * (done + block.shape[0]) // nframes
        if chunk_last <= chunk_first:
            # Fewer frames than columns: several columns fall inside one frame,
            # so spread what we got across them rather than dropping it.
            chunk_first = min(chunk_first, share - 1)
            chunk_last = share

        part_mins, part_maxs, part_sumsq, part_counts, _ = _reduce_blocks(
            block, chunk_last - chunk_first
        )
        # Chunks overlap a column only when fewer frames than columns, so in
        # practice each column written once; merging when it does happen keeps
        # loud transient from being clipped to chunk it started in.
        # `_reduce_blocks` fills every column given, so "not written yet" test
        # is just NaN. Moments merge additively: overlapping chunks hold
        # *different* frames of same column, so energy and sample counts both add.
        target_mins = mins[chunk_first:chunk_last]
        target_maxs = maxs[chunk_first:chunk_last]
        fresh = np.isnan(target_mins)
        target_mins[fresh] = part_mins[fresh]
        target_maxs[fresh] = part_maxs[fresh]
        settled = ~fresh
        np.minimum(target_mins, part_mins, out=target_mins, where=settled)
        np.maximum(target_maxs, part_maxs, out=target_maxs, where=settled)
        target_sumsq = sumsq[chunk_first:chunk_last]
        target_counts = counts[chunk_first:chunk_last]
        target_sumsq[fresh] = part_sumsq[fresh]
        target_counts[fresh] = part_counts[fresh]
        target_sumsq[settled] += part_sumsq[settled]
        target_counts[settled] += part_counts[settled]

        done += block.shape[0]
        budget -= block.shape[0]

    if np.all(np.isnan(mins)):
        return None, None, None, done

    rms = np.full(share, np.nan, dtype=np.float32)
    written = counts > 0
    rms[written] = np.sqrt(sumsq[written] / counts[written]).astype(np.float32)
    return mins, maxs, rms, done
