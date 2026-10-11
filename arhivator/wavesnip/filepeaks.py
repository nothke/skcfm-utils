"""Per-file envelope caches: read a range once, answer a thousand views.

``wavesnip.peaks`` re-reads audio every time view changes: exact path
re-decodes its range and sampled path re-seeks it, and on spinning disk the
*seeks* take minutes (see ``flow.md``, "Bounded probing"). This module caches
each file's per-column min/max/energy envelope at fixed time fidelity, so once
a file's covered range seen, view over it answered entirely from RAM.

**Read once, sequential; seek never.** Building a range is one forward sweep
of its frames, folded straight into cache -- exactly what spinning disk is
fast at. Afterwards file not touched again for waveform drawing until its
identity changes. Seek-bound sampled path remains only as backstop for files
never viewed.

**Lazy, segmented, whole columns.** Only column ranges a plan actually asks
about are built. View reaching into middle of file builds that slice, not
whole file. Whole columns keep per-column sample counts *derivable* from file
facts -- every column holds ``frames_per_col * nchannels`` samples except
ragged last one -- so cache stores only three floats an envelope needs, not
counts.

**In-memory and bounded.** Nothing persists: envelope cheap to rebuild, and
built ranges evicted LRU once total grows past cap. Resident memory set by
what's actually viewed, not archive size. (Sizing for 6,000 x 1-hour archive:
at 0.25 s fidelity each hour file caches to ~170 KB, so even *whole* archive
would be ~1 GB only if every file cached; LRU cap stops that mattering.)

**Approximate at column edges, exact within.** Piece's frame range rounded out
to whole cache columns it touches, so column straddling piece boundary carries
sliver that belongs beside it. Acceptable because cache only served for views
wide enough that one view column holds a whole cache column (tier rule in
``peaks_for``): there the sliver is rounding error against what a pixel can
show. For narrower views exact path decodes range instead of cache resolving
it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import numpy as np

__all__ = [
    "BUILD_CHUNK_FRAMES",
    "CACHE_STEP_US",
    "MAX_CACHE_BYTES",
    "FilePeakCache",
    "FilePeaks",
]


#: Fidelity of per-file envelope: one cache column per this much audio.
#:
#: Number that matters downstream is *time*, not frames: plan can use cache
#: once each of its 1600 columns covers a whole cache column, so this fidelity
#: also sets narrowest view cache serves. 0.25 s means ~6.7-min view and
#: wider from RAM; narrower goes to exact path, which at those widths reads at
#: most a few tens of MB. Finer fidelity serves wider zoomed-in views from RAM
#: at cost of larger envelope; coarser shrinks envelope but pushes more views
#: onto exact path. 0.25 s is middle that keeps zoomed-in case -- reported
#: pain -- off seek-bound sampled path.
CACHE_STEP_US = 250_000

#: Frames read at once while building one file's range. Made multiple of
#: column length when possible so fold is one reshape instead of a loop.
BUILD_CHUNK_FRAMES = 8_000_000

#: Total cache size after which LRU files dropped.
#: Bounds resident memory independently of archive size, which keeps 6,000-hour
#: archive at 0.25 s fidelity (~1 GB if fully cached) "light": the cap, not
#: the archive, sets the ceiling.
MAX_CACHE_BYTES = 256 * 2**20


@dataclass
class FilePeaks:
    """One file's envelope: per-column min/max/sumsq at fixed time fidelity.

    ``mins``/``maxs``/``sumsq`` run one float32 array per cache column, folded
    over all channels. ``built`` marks columns folded in so far, because a view
    builds only the slice it needs; rest remain ``+inf``/``-inf``/``0`` until
    asked for.

    Column sample counts derived, not stored: every column holds
    ``frames_per_col * nchannels`` samples except ragged last one, which holds
    whatever file has left (``nframes - (ncols - 1) * frames_per_col`` frames).
    See :func:`FilePeakCache.column_counts`.
    """

    nframes: int
    rate: int
    nchannels: int
    frames_per_col: int
    mins: np.ndarray  # float32, +inf until built
    maxs: np.ndarray  # float32, -inf until built
    sumsq: np.ndarray  # float32
    built: np.ndarray  # bool

    @property
    def ncols(self) -> int:
        return int(self.mins.shape[0])

    @property
    def nbytes(self) -> int:
        return self.mins.nbytes + self.maxs.nbytes + self.sumsq.nbytes + self.built.nbytes

    def column_counts(self, cols: np.ndarray) -> np.ndarray:
        """Sample counts for ``cols``, summing across channels.

        ``cols`` is a set of cache-column indices for this file; count is
        uniform ``frames_per_col * nchannels`` everywhere except ragged last
        column.
        """
        full = self.frames_per_col * self.nchannels
        last = (self.nframes - (self.ncols - 1) * self.frames_per_col) * self.nchannels
        return np.where(cols == self.ncols - 1, last, full)


def _fold_columns(entry: FilePeaks, path, read: Callable, j_start: int, j_end: int) -> None:
    """Fold file's columns ``[j_start, j_end)`` from disk into ``entry``.

    Reads whole cache columns in aligned chunks so common case is one reshape
    per chunk. Only ragged read is file's own last column (fewer frames than
    a column), folded into that column directly rather than padded with
    silence.
    """
    fpc = entry.frames_per_col
    first = j_start * fpc
    last = min(j_end * fpc, entry.nframes)
    if last <= first:
        return
    chunk = max(fpc, BUILD_CHUNK_FRAMES // fpc * fpc)

    at = first
    while at < last:
        take = min(chunk, last - at)
        block = read(path, at, take)
        n = int(block.shape[0])
        if n <= 0:
            break

        whole = n // fpc * fpc
        if whole:
            cols = block[:whole].reshape(-1, fpc, entry.nchannels)
            jj = np.arange(at // fpc, at // fpc + cols.shape[0])
            # Each column built exactly once (``built`` mask guarantees it),
            # so plain assignment is the write. Not ``minimum(out=...)``:
            # ``entry.mins[jj]`` is copied view, writing to it through ``out``
            # would change copy and leave cache untouched.
            entry.mins[jj] = cols.min(axis=(1, 2))
            entry.maxs[jj] = cols.max(axis=(1, 2))
            # einsum rather than ``cols * cols``: same sum, no full-size
            # temporary, matching `_reduce_blocks`.
            entry.sumsq[jj] += np.einsum("pkc,pkc->p", cols, cols).astype(
                np.float32, copy=False
            )
            entry.built[jj] = True
            at += whole

        if whole < n:
            # Ragged tail -- file ends part-way through its last column (a
            # read may return whole file even when requested range stops short
            # of it, because `read_frames` clamps at file end).
            tail = block[whole:]
            j = entry.ncols - 1
            entry.mins[j] = min(float(entry.mins[j]), float(tail.min()))
            entry.maxs[j] = max(float(entry.maxs[j]), float(tail.max()))
            entry.sumsq[j] += np.square(tail.astype(np.float64)).sum()  # type: ignore[assignment]
            entry.built[j] = True
            at += n - whole


class FilePeakCache:
    """Per-file envelope cache, keyed by file identity.

    ``read(path, start_frame, nframes)`` returns float32 ``(n, channels)``
    audio and defaults to ``wavio.read_frames``; injecting a reader lets cache
    be tested without real disk. Calls that mutate cache (builds, evictions)
    hold one lock, so second worker asking for file while another builds it
    sees consistent picture.
    """

    def __init__(
        self,
        read: Callable | None = None,
        step_us: int = CACHE_STEP_US,
        max_bytes: int = MAX_CACHE_BYTES,
    ):
        from wavesnip.wavio import read_frames

        self.read = read or read_frames
        self.step_us = step_us
        self.max_bytes = max_bytes
        self._entries: dict[tuple, FilePeaks] = {}
        self._used: dict[tuple, int] = {}
        self._tick = 0
        self._lock = threading.Lock()

    def key_for(self, clip) -> tuple:
        """File identity that separates one cached envelope from the next.

        Same fields indexer trusts to mean "this is the same file": path plus
        size and mtime, plus header facts. File that changes gets new key and
        is rebuilt on first view; stale entry lingers until LRU evicts it.
        """
        return (
            str(clip.path),
            clip.size,
            clip.mtime_us,
            clip.nframes,
            clip.framerate,
            clip.nchannels,
        )

    def envelope_for(self, piece) -> FilePeaks | None:
        """Ensure piece's columns are built, and return its envelope.

        Builds *whole columns only*, rounded out from piece's frame range.
        Returns ``None`` when file has nothing to envelope (no frames), which
        caller treats as "not cached, use another path".
        """
        clip = piece.clip
        key = self.key_for(clip)
        fpc = max(1, round(self.step_us * clip.framerate / 1_000_000))

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if clip.nframes <= 0:
                    return None
                ncols = (clip.nframes + fpc - 1) // fpc
                entry = FilePeaks(
                    nframes=clip.nframes,
                    rate=clip.framerate,
                    nchannels=clip.nchannels,
                    frames_per_col=fpc,
                    mins=np.full(ncols, np.inf, dtype=np.float32),
                    maxs=np.full(ncols, -np.inf, dtype=np.float32),
                    sumsq=np.zeros(ncols, dtype=np.float32),
                    built=np.zeros(ncols, dtype=bool),
                )
                self._entries[key] = entry

            self._used[key] = self._tick
            self._tick += 1

            if piece.start_frame < piece.end_frame:
                j0 = min(piece.start_frame // fpc, entry.ncols - 1)
                j1 = min((piece.end_frame - 1) // fpc, entry.ncols - 1)
                if j1 >= j0:
                    missing = np.flatnonzero(~entry.built[j0 : j1 + 1]) + j0
                    if missing.size:
                        self._build(entry, key[0], missing)
            self._evict()
            return entry

    def _build(self, entry: FilePeaks, path, missing: np.ndarray) -> None:
        """Build columns in ``missing`` (sorted), run by contiguous run.

        Contiguous runs folded in one forward sweep each, so file read from
        start to end within a run rather than seeked back and forth.
        """
        where = np.asarray(missing, dtype=np.int64)
        if where.size == 0:
            return
        run_starts = np.r_[where[0], where[1:][np.diff(where) != 1]]
        run_ends = np.r_[where[:-1][np.diff(where) != 1], where[-1]]
        for j_start, j_end in zip(run_starts.tolist(), run_ends.tolist()):
            _fold_columns(entry, path, self.read, j_start, j_end + 1)

    def _evict(self) -> None:
        total = sum(entry.nbytes for entry in self._entries.values())
        if total <= self.max_bytes:
            return
        order = sorted(self._entries, key=self._used.get)
        for key in order:
            if total <= self.max_bytes:
                break
            total -= self._entries.pop(key).nbytes
            self._used.pop(key, None)

            if piece.start_frame < piece.end_frame:
                j0 = min(piece.start_frame // fpc, entry.ncols - 1)
                j1 = min((piece.end_frame - 1) // fpc, entry.ncols - 1)
                if j1 >= j0:
                    missing = np.flatnonzero(~entry.built[j0 : j1 + 1]) + j0
                    if missing.size:
                        self._build(entry, key[0], missing)
            self._evict()
            return entry

    def _build(self, entry: FilePeaks, path, missing: np.ndarray) -> None:
        """Build the columns in ``missing`` (sorted), run by contiguous run.

        Contiguous runs are folded in one forward sweep each, so a file is read
        from start to end within a run rather than seeked back and forth.
        """
        where = np.asarray(missing, dtype=np.int64)
        if where.size == 0:
            return
        run_starts = np.r_[where[0], where[1:][np.diff(where) != 1]]
        run_ends = np.r_[where[:-1][np.diff(where) != 1], where[-1]]
        for j_start, j_end in zip(run_starts.tolist(), run_ends.tolist()):
            _fold_columns(entry, path, self.read, j_start, j_end + 1)

    def _evict(self) -> None:
        total = sum(entry.nbytes for entry in self._entries.values())
        if total <= self.max_bytes:
            return
        order = sorted(self._entries, key=self._used.get)
        for key in order:
            if total <= self.max_bytes:
                break
            total -= self._entries.pop(key).nbytes
            self._used.pop(key, None)