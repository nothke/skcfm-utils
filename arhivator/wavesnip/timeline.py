"""Timeline resolution and snip planning.

The index (see ``indexer.py``) answers "what covers instant t". This module
answers the two questions the app actually asks:

1. **Where do I play from?** -- :meth:`Timeline.resolve`, resolving an instant
   to a clip across every archive.
2. **What do I cut out?** -- :meth:`Timeline.plan_snip`, turning a time range
   into an ordered list of frame-exact read instructions.

Two rules shape everything here.

**Per-instant priority resolution.** All archives hold byte-identical clips
from the same stream, so there's no ambiguity to ask the user about. Coverage
resolved at each instant independently, in archive priority order. Because
samples are identical, a snip may freely take one piece from archive A and the
next from archive B and the join is genuinely seamless. This also handles
partial sync for free, and covers gaps in one archive using another.

**Overlapping chunks are structurally impossible to double-count.** Planning
partitions the requested range at *every* clip boundary, so each elementary
interval has a constant set of covering clips and exactly one clip is chosen
for it. Adjacent pieces therefore abut exactly and cannot overlap. The
``cursor`` clamp in ``plan_snip`` is a cheap belt-and-braces guard on that
invariant rather than the mechanism achieving it.
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from wavesnip.indexer import ArchiveIndex
from wavesnip.model import Archive, Clip, from_us

__all__ = [
    "Timeline",
    "Resolution",
    "Piece",
    "Gap",
    "SnipPlan",
    "merge_intervals",
]


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of ``(start, end)`` pairs as a sorted, disjoint list.

    Touching intervals merge: a butt-joined seam is not a gap. Matters for
    coverage queries: two chunks that abut leave no hole.
    """
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _clips_containing(
    clips: list[Clip], starts: list[int], moment: int, max_duration: int
) -> list[Clip]:
    """Clips whose ``[start, end)`` contains ``moment``.

    Clips must be sorted by ``start_us`` with ``starts`` the matching list of
    start values.

    Scans backwards from insertion point, bounded by ``max_duration``: a clip
    can only contain ``moment`` if it begins less than one clip-duration before
    it, so scan is self-limiting -- an hour of hour-long chunks examines a
    couple of clips, a month of one-second clips examines one.
    """
    found: list[Clip] = []
    index = bisect.bisect_right(starts, moment) - 1
    while index >= 0:
        clip = clips[index]
        if moment - clip.start_us > max_duration:
            break  # cannot possibly reach `moment`
        if clip.contains(moment):
            found.append(clip)
        index -= 1
    return found


@dataclass(frozen=True)
class Gap:
    """A stretch with no audio in any archive."""

    start_us: int
    end_us: int

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def duration(self) -> float:
        return self.duration_us / 1_000_000

    @property
    def start(self) -> datetime:
        return from_us(self.start_us)

    @property
    def end(self) -> datetime:
        return from_us(self.end_us)


@dataclass(frozen=True)
class Piece:
    """One contiguous read: ``[start_frame, end_frame)`` of ``clip``.

    ``start_us``/``end_us`` are timeline instants, already clamped for overlap.
    """

    clip: Clip
    start_frame: int
    end_frame: int
    start_us: int
    end_us: int

    @property
    def nframes(self) -> int:
        return self.end_frame - self.start_frame

    @property
    def path(self) -> Path:
        return self.clip.path

    @property
    def archive(self) -> Path:
        return self.clip.archive_path


@dataclass(frozen=True)
class SnipPlan:
    """The result of planning a snip: what to read, and what is missing."""

    requested: tuple[int, int]
    pieces: tuple[Piece, ...] = ()
    gaps: tuple[Gap, ...] = ()

    @property
    def start_us(self) -> int:
        return self.requested[0]

    @property
    def end_us(self) -> int:
        return self.requested[1]

    @property
    def is_empty(self) -> bool:
        return not self.pieces

    @property
    def covered_us(self) -> int:
        return sum(p.end_us - p.start_us for p in self.pieces)

    @property
    def missing_us(self) -> int:
        return sum(g.duration_us for g in self.gaps)

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)

    @property
    def nframes(self) -> int:
        return sum(p.nframes for p in self.pieces)

    @property
    def archives_used(self) -> list[Path]:
        seen: list[Path] = []
        for piece in self.pieces:
            if piece.archive not in seen:
                seen.append(piece.archive)
        return seen

    @property
    def formats(self) -> list[tuple[int, int, int]]:
        """Distinct ``(framerate, nchannels, sampwidth)`` across the pieces."""
        seen: list[tuple[int, int, int]] = []
        for piece in self.pieces:
            clip = piece.clip
            fmt = (clip.framerate, clip.nchannels, clip.sampwidth)
            if fmt not in seen:
                seen.append(fmt)
        return seen

    @property
    def needs_resample(self) -> bool:
        """Do the pieces disagree on rate or channel count?

        Export has to reconcile these. Bit depth alone does not require
        resampling, so it is excluded here.
        """
        rates = {(f[0], f[1]) for f in self.formats}
        return len(rates) > 1

    @property
    def duration(self) -> float:
        return self.covered_us / 1_000_000


@dataclass(frozen=True)
class Resolution:
    """What covers a single instant, across all archives.

    Carries no list of losing candidates: lower-priority archives are not
    consulted once an archive matches, because the archives hold the same
    stream and there is nothing to decide between them.
    """

    instant_us: int
    clip: Clip | None = None

    @property
    def is_gap(self) -> bool:
        return self.clip is None

    @property
    def is_covered(self) -> bool:
        return self.clip is not None

    @property
    def instant(self) -> datetime:
        return from_us(self.instant_us)

    @property
    def offset_us(self) -> int:
        """How far into the chosen clip the instant falls."""
        if self.clip is None:
            return 0
        return self.instant_us - self.clip.start_us


class Timeline:
    """Read-only view over every configured archive.

    Clips snapshotted from each archive's index on first use, held in memory,
    so all queries are pure lookups. Snapshot refreshed on startup and by
    manual rescan -- deliberately *not* by filesystem watcher, since watching
    is unreliable on network shares.
    """

    def __init__(self, archives: list[Archive]):
        # Stable sort by priority, then path, so ordering deterministic even
        # if two archives share a priority.
        self.archives = sorted(archives, key=lambda a: (a.priority, str(a.path)))
        self._snapshots: list[tuple[Archive, list[Clip], list[int], int]] = []
        self._failures: list[str] = []
        self._loaded = False

    # -- snapshot -------------------------------------------------------

    def refresh(self) -> "Timeline":
        """Reload every archive's clips from its index.

        One archive that cannot be read does not stop the others. Letting it
        propagate would mean a mistyped path, or a share not mounted, silently
        emptying timeline for every *working* archive too -- user would see
        empty app with no way to tell why. Broken archive left in list and
        reported in :attr:`failures`, so fixing path and pressing Rescan recovers.
        """
        snapshots = []
        failures: list[str] = []
        for archive in self.archives:
            try:
                clips = ArchiveIndex(archive).load_clips()
            except (OSError, sqlite3.Error) as exc:
                # Most errors from indexer already name folder, and
                # "typo: archive folder not found: /home/me/typo" is worse than
                # either half. Prefix only when message doesn't.
                message = str(exc)
                failures.append(
                    message if str(archive.path) in message
                    else f"{archive.name}: {message}"
                )
                continue
            clips.sort(key=lambda c: c.start_us)
            starts = [c.start_us for c in clips]
            max_duration = max((c.duration_us for c in clips), default=0)
            snapshots.append((archive, clips, starts, max_duration))
        self._snapshots = snapshots
        self._failures = failures
        self._loaded = True
        return self

    @property
    def failures(self) -> list[str]:
        """Archives that could not be read during the last refresh.

        Empty until :meth:`refresh` has run at least once.
        """
        self._ensure_loaded()
        return list(self._failures)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.refresh()

    @property
    def clips(self) -> list[Clip]:
        """Every clip across every archive, sorted by start."""
        self._ensure_loaded()
        out: list[Clip] = []
        for _, clip_list, _, _ in self._snapshots:
            out.extend(clip_list)
        out.sort(key=lambda c: c.start_us)
        return out

    def clips_between(self, start_us: int, end_us: int) -> list[Clip]:
        """Clips overlapping ``[start_us, end_us)``, sorted by start.

        View asks for this on every pan, so cannot go through :attr:`clips`:
        that concatenates and re-sorts every clip in every archive -- on a year
        of hourly recordings that's 100k objects per frame.

        Each archive keeps its clips and start times sorted, and knows its own
        longest clip, so window is binary search either side of it. Backward
        search must reach back by that longest clip, not to ``start_us`` itself
        -- a single long recording starting hours before the view is on screen
        is exactly the clip the view is there to show, and a search only looking
        forward from ``start_us`` would miss it.
        """
        self._ensure_loaded()
        out: list[Clip] = []
        for _, clip_list, starts, max_duration in self._snapshots:
            lo = bisect.bisect_left(starts, start_us - max_duration)
            hi = bisect.bisect_right(starts, end_us)
            for clip in clip_list[lo:hi]:
                if clip.end_us > start_us and clip.start_us < end_us:
                    out.append(clip)
        out.sort(key=lambda c: c.start_us)
        return out

    @property
    def coverage(self) -> list[tuple[int, int]]:
        """Merged coverage runs across all archives -- a gap-free list.

        Future calendar visualisation is a picture of this.
        """
        self._ensure_loaded()
        runs: list[tuple[int, int]] = []
        for _, clip_list, _, _ in self._snapshots:
            runs.extend((c.start_us, c.end_us) for c in clip_list)
        return merge_intervals(runs)

    def gaps(self) -> list[Gap]:
        """Every gap in the archive, in time order.

        Coverage runs merged and disjoint, so every consecutive pair separated
        by a real hole -- nothing to filter out.
        """
        self._ensure_loaded()
        runs = self.coverage
        return [Gap(end, start) for (_, end), (start, _) in zip(runs, runs[1:])]

    # -- point queries --------------------------------------------------

    def resolve(self, instant_us: int) -> Resolution:
        """Which clip should play at this instant?

        Walks archives in priority order and takes first with coverage.
        Lower-priority archives not examined -- no ambiguity to resolve, so no
        question ever put to user.
        """
        self._ensure_loaded()

        for _, clip_list, starts, max_duration in self._snapshots:
            found = _clips_containing(clip_list, starts, instant_us, max_duration)
            if not found:
                continue
            # Most forward headroom wins, so playback and forward snips don't
            # hop to a shorter clip about to end.
            best = max(found, key=lambda c: c.end_us)
            return Resolution(instant_us, best)
        return Resolution(instant_us)

    def nearest_covered(self, instant_us: int) -> int | None:
        """Closest instant with audio, or None if nothing does.

        Used to jump out of a gap: playhead lands on nearer edge so user hears
        something rather than silence. When gap roughly centred, "nearer" is
        arbitrary -- any tie-break is as good.

        Jumping *forwards* out of gap lands on next run's start. Jumping
        *backwards* lands one microsecond before previous run's end, because
        intervals are half-open and that end instant is itself in the gap.

        A gap is always exited **forwards**, not to whichever edge is nearer.
        Nearest-by-distance looks reasonable and is wrong: playhead one
        millisecond past run's end would jump *back* one millisecond, play last
        sliver again, run out of clip, jump forward — the stutter the gap was
        supposed to avoid.
        """
        self._ensure_loaded()
        runs = self.coverage
        if not runs:
            return None

        starts = [s for s, _ in runs]
        index = bisect.bisect_right(starts, instant_us) - 1

        if index >= 0 and instant_us < runs[index][1]:
            return instant_us  # already covered

        if index + 1 < len(runs):
            return runs[index + 1][0]  # forward out of gap
        if index >= 0:
            return runs[index][1] - 1  # past end of everything
        return runs[0][0]  # before first run ever

    # -- snip planning --------------------------------------------------

    def plan_snip(self, from_us: int, to_us: int) -> SnipPlan:
        """Turn a time range into ordered, frame-exact read instructions.

        Range partitioned at every clip boundary inside it, so each elementary
        interval has constant set of covering clips. Exactly one clip chosen
        per interval, so pieces cannot overlap.

        Gaps recorded not filled: per agreed default, snip running through hole
        exports covered audio and reports missing, rather than silently padding
        with silence or refusing.
        """
        self._ensure_loaded()
        requested = (from_us, to_us)
        if to_us <= from_us:
            return SnipPlan(requested)

        boundaries = {from_us, to_us}
        for _, clip_list, _, _ in self._snapshots:
            for clip in clip_list:
                if from_us < clip.start_us < to_us:
                    boundaries.add(clip.start_us)
                if from_us < clip.end_us < to_us:
                    boundaries.add(clip.end_us)

        points = sorted(boundaries)
        pieces: list[Piece] = []
        gaps: list[Gap] = []
        cursor = from_us  # end of the last emitted piece

        for low, high in zip(points, points[1:]):
            if high <= low:
                continue
            midpoint = low + (high - low) // 2

            winner: list[Clip] = []
            for _, clip_list, starts, max_duration in self._snapshots:
                winner = _clips_containing(clip_list, starts, midpoint, max_duration)
                if winner:
                    break

            if not winner:
                gaps.append(Gap(low, high))
                continue

            clip = max(winner, key=lambda c: c.end_us)
            # Guard, not mechanism: elementary intervals already abut exactly.
            start_instant = max(low, cursor)
            start_frame, end_frame = clip.frame_span(start_instant, high)
            if end_frame <= start_frame:
                continue

            if pieces and pieces[-1].clip is clip and pieces[-1].end_frame == start_frame:
                pieces[-1] = replace(pieces[-1], end_frame=end_frame, end_us=high)
            else:
                pieces.append(
                    Piece(clip, start_frame, end_frame, start_instant, high)
                )
            cursor = high

        return SnipPlan(requested, tuple(pieces), tuple(gaps))
