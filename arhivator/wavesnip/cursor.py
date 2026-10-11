"""Deciding what plays next, with no Qt and no audio hardware involved.

This is the decision half of playback, split from :mod:`wavesnip.player`
because every awkward case in playback is a plain function of a timeline plus
an instant: what plays now, what plays after this clip ends, where to jump
when playhead lands in a gap.

Keeping it Qt-free is not tidiness for its own sake. It's what makes these
cases testable without a sound card, display, or event loop -- which in
practice means the pure logic can be checked hundreds of times in seconds on
any interpreter, with the Qt wrapper checked separately and thinly.

**A ``Source`` is a file plus an offset, not a whole timeline.** Qt plays one
file at a time, so crossing a chunk boundary means loading the next file.
Deciding *which* file is this module's job; actually loading it is the
player's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wavesnip.timeline import Timeline

__all__ = ["Cursor", "Source", "GapSource"]


@dataclass(frozen=True)
class Source:
    """A file to play, and where in it to begin.

    ``start_us`` is the timeline instant this load resumes at; ``end_us`` is
    when it runs out. ``offset_us`` is how far into ``path`` that instant sits.
    """

    path: Path
    offset_us: int
    start_us: int
    end_us: int

    @property
    def offset_ms(self) -> int:
        """Seek offset in the units Qt wants.

        Truncating loses at most 999 µs, a small fraction of one frame at any
        realistic sample rate and far below what a listener could place.
        """
        return self.offset_us // 1000


@dataclass(frozen=True)
class GapSource:
    """A silent gap to play between clips.

    ``start_us`` is the timeline instant this gap starts; ``end_us`` is when
    the next clip begins. The player will generate and play silence for this
    duration instead of jumping over it.
    """

    start_us: int
    end_us: int


class Cursor:
    """Answers "given where we are, what should play next?"."""

    def __init__(self, timeline: Timeline, instant_us: int):
        self.timeline = timeline
        self.instant_us = instant_us

    def source_at(self, instant_us: int) -> Source | None:
        """The clip covering ``instant_us``, as a playable source.

        None means the instant is in a gap, or in nothing at all.
        """
        clip = self.timeline.resolve(instant_us).clip
        if clip is None:
            return None
        return Source(
            path=clip.path,
            offset_us=instant_us - clip.start_us,
            start_us=instant_us,
            end_us=clip.end_us,
        )

    def nearest_source(self, instant_us: int) -> tuple[Source | None, int | None]:
        """Jump a gapped instant to the nearest covered one.

        Returns ``(source, jumped_to)``, with ``jumped_to`` None when no jump
        needed. Timeline with no audio anywhere returns ``(None, None)``, which
        is how caller tells "nothing here" from "moved but still nothing".
        """
        target = self.timeline.nearest_covered(instant_us)
        if target is None:
            return None, None
        if target == instant_us:
            return self.source_at(instant_us), None
        return self.source_at(target), target

    def end_of_source(self, source: Source) -> Source | None:
        """What plays after ``source`` runs out.

        None means a gap or end of archive -- caller decides whether to skip
        forward or stop. Because intervals are half-open, this never returns
        the clip that just finished.
        """
        return self.source_at(source.end_us)

    def move(self, delta_us: int) -> int:
        """Move cursor by a signed delta and return the new instant."""
        self.instant_us += delta_us
        return self.instant_us

    def gap_after(self, source: Source) -> GapSource | None:
        """Return gap after ``source`` if one exists before next clip.

        Returns ``None`` if next clip starts immediately (no gap) or if no next
        clip at all.
        """
        if self.timeline is None:
            return None
        
        # Use timeline's coverage to find next run after source.end_us
        runs = self.timeline.coverage
        if not runs:
            return None
        
        for run_start, run_end in runs:
            if run_start > source.end_us:
                # Found next clip run - gap from source.end_us to run_start
                return GapSource(start_us=source.end_us, end_us=run_start)
            # If source.end_us within this run, no gap after it (shouldn't
            # happen since source ends at run_end)
            if run_start <= source.end_us < run_end:
                return None
        
        return None