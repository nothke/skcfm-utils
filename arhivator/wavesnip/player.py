"""Playing archive audio, crossing clip boundaries transparently.

Qt's ``QMediaPlayer`` plays **one file at a time**, but a timeline instant maps
to a clip, and playback stopping at every chunk boundary would be useless -- an
hour of audio would stutter twelve times an hour. Module stays thin: decisions
live in :mod:`wavesnip.cursor`, this drives Qt from them.

**Seeking is millisecond-accurate, not frame-accurate.** ``setPosition`` takes
milliseconds. Index is frame-exact and snips export frame-exact, but playhead
lands on nearest millisecond (~48 frames at 48 kHz). For finding/hearing audio
that's plenty. It wouldn't be if this module did cutting, which is why it doesn't.

**Gaps are jumped, not waited out.** When playback runs into a hole it skips to
nearest covered instant and emits ``jumped``, rather than falling silent.
Playhead in a gap should still make noise, and the skip is worth showing.

**Snip preview renders rather than streams.** A snip spans several files and may
need resampling; Qt does neither. :meth:`Player.play_snip` renders to temp WAV
via :mod:`wavesnip.resample` and plays that. Rendering is CPU work, so UI must
keep it off event loop; this module doesn't do that for you.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from wavesnip.cursor import Cursor, Source, GapSource
from wavesnip.timeline import Timeline

__all__ = ["Player", "PlayerError", "Cursor", "Source", "GapSource"]


class PlayerError(Exception):
    """Playback could not start."""


class Player(QObject):
    """Plays archive audio, crossing clip boundaries transparently.

    One ``QMediaPlayer`` reused for every clip: swapping source is exactly what
    crossing a chunk boundary amounts to.

    **Playhead moves continuously through time.** In a gap (no clip), playhead
    advances silently based on wall-clock time. When it enters a clip, clip is
    loaded and played. When it leaves a clip into a gap, audio stops but
    playhead keeps moving. Avoids "jump" behavior where gaps were skipped
    entirely.
    """

    # `object`, not `int`: Qt maps `Signal(int)` to C++ `int` (32-bit signed),
    # and microsecond timestamp ~1.8e15. Every emit overflowed with shiboken
    # RuntimeWarning before this was `object`.
    positionChanged = Signal(object)  # timeline instant, microseconds
    stateChanged = Signal(bool)  # True while playing
    jumped = Signal(object, object)  # from_us, to_us (only for explicit seeks)
    error = Signal(str)
    finished = Signal()

    #: Qt emits positionChanged while playing, but fixed poll covers seeking
    #: and paused, keeping playhead honest.
    _POLL_MS = 100

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)

        self._timeline: Timeline | None = None
        self._cursor: Cursor | None = None
        self._source: Source | None = None  # current clip source, or None in gaps
        self._limit_us: int | None = None  # stop here, for snip playback
        self._advancing = False  # guard against re-entry during source swap
        self._render_dir: TemporaryDirectory | None = None

        # Qt drops setPosition() while media still loading, so seek into freshly
        # loaded clip waits here until honoured -- otherwise playback starts at
        # file's beginning and playhead jumps back to clip start. See
        # _apply_pending_seek.
        self._pending_seek_ms: int | None = None
        # ... and play() must wait for that seek to land, or first frames out of
        # file are wrong ones.
        self._play_when_ready = False
        # True only inside setSource, to tell a LoadedMedia belonging to media
        # we're replacing from one saying our load finished.
        self._setting_source = False

        # For silent playhead advancement through gaps
        self._in_gap = False  # True when playhead in gap (no audio)
        self._gap_start_wall_us: int | None = None  # wall-clock time when gap started
        self._gap_start_timeline_us: int | None = None  # timeline instant when gap started
        self._frozen_gap_position_us: int | None = None  # frozen position when paused in gap
        self._playing = False  # True when user explicitly started playback

        self._timer = QTimer(self)
        self._timer.setInterval(self._POLL_MS)
        self._timer.timeout.connect(self._on_poll)

        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        self._player.playbackStateChanged.connect(self._on_state)

    # -- setup -----------------------------------------------------------

    def set_timeline(self, timeline: Timeline, instant_us: int = 0) -> None:
        """Attach a timeline, stopping anything already playing."""
        self.stop()
        self._timeline = timeline
        # A cursor always exists once timeline is set, so nothing downstream
        # has to cope with a None cursor that only appears later.
        self._cursor = Cursor(timeline, instant_us)

    @property
    def timeline(self) -> Timeline | None:
        return self._timeline

    @property
    def position_us(self) -> int:
        """Current timeline instant, or the cursor's when nothing is loaded."""
        if self._source is None and not self._in_gap:
            return self._cursor.instant_us if self._cursor else 0
        
        # In a gap: advance based on wall-clock time since gap started
        if self._in_gap:
            # If we have a frozen position (paused), return that
            if self._frozen_gap_position_us is not None:
                return self._frozen_gap_position_us
            import time
            now_wall_us = int(time.time() * 1_000_000)
            elapsed = now_wall_us - (self._gap_start_wall_us or 0)
            return (self._gap_start_timeline_us or 0) + elapsed
        
        # Playing a clip
        if self._source is None:
            return self._cursor.instant_us if self._cursor else 0
            
        clip_start_us = self._source.start_us - self._source.offset_us
        # While a seek is pending the backend still reports the file's own
        # start, which would put the playhead at the beginning of the clip;
        # report where the seek is going instead.
        if self._pending_seek_ms is not None:
            return clip_start_us + self._pending_seek_ms * 1000
        return clip_start_us + self._player.position() * 1000

    @property
    def is_playing(self) -> bool:
        """True if user explicitly started playback (playing clip or advancing through gap).

        Returns False when paused/stopped, even if playhead is in a gap.
        """
        return self._playing

    def _emit_state_changed(self) -> None:
        """Emit stateChanged based on overall playback state (clips + gaps)."""
        self.stateChanged.emit(self.is_playing)

    # -- playback --------------------------------------------------------

    def play_at(self, instant_us: int, *, limit_us: int | None = None) -> Source | None:
        """Play from a timeline instant.

        If the instant is in a gap, the playhead advances silently through the
        gap until it reaches a clip. ``limit_us`` stops playback there.
        """
        cursor = self._require_cursor()
        cursor.instant_us = instant_us
        self._limit_us = limit_us
        self._playing = True

        # Check if we're starting in a gap
        source = cursor.source_at(instant_us)
        if source is None:
            # Starting in a gap - begin silent playhead advancement
            self._enter_gap(instant_us)
            self._timer.start()
            return None

        # Starting in a clip - load and play it
        self._load(source)
        self._start_playback()
        self._timer.start()
        return source

    def play_snip(self, plan) -> None:
        """Preview a snip plan by rendering it, then playing the render.

        The render lives in a temp directory owned by this player and is deleted
        when the next preview starts or the player is closed — Qt reads the file
        lazily, so it has to outlive playback.
        """
        self._require_cursor()
        self.pause()

        from wavesnip.resample import ExportError, render_snip

        self._release_render()
        self._render_dir = TemporaryDirectory()
        path = Path(self._render_dir.name) / "snip.wav"

        try:
            result = render_snip(plan, path)
        except ExportError as exc:
            self._release_render()
            self.error.emit(str(exc))
            raise

        self._source = Source(
            path=path,
            offset_us=0,
            start_us=plan.start_us,
            end_us=plan.start_us + round(result.duration * 1_000_000),
        )
        self._limit_us = self._source.end_us
        # The render starts at 0 and is played directly, so nothing waits on a
        # seek -- and a seek left over from a clip load that never finished
        # must not be applied to the render when its media arrives.
        self._pending_seek_ms = None
        self._play_when_ready = False
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        self._timer.start()

    def play_file(self, path: str | Path) -> None:
        """Play an arbitrary file, e.g. a fresh export, for auditioning."""
        self.pause()
        self._limit_us = None
        self._source = Source(
            path=Path(path), offset_us=0, start_us=0, end_us=1 << 62
        )
        # Starts at 0, played directly -- see play_snip for why the pending
        # state is cleared rather than left to the media's arrival.
        self._pending_seek_ms = None
        self._play_when_ready = False
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()
        self._timer.start()

    def pause(self) -> None:
        if self.is_playing:
            self._player.pause()
        self._timer.stop()
        # Pause stops playback entirely - freeze position if in gap
        if self._in_gap:
            self._frozen_gap_position_us = self.position_us
            self._in_gap = False
        else:
            self._frozen_gap_position_us = None
        self._gap_start_wall_us = None
        self._gap_start_timeline_us = None
        self._playing = False
        # A pause cancels any play that was only waiting for the media to load.
        self._play_when_ready = False
        self.stateChanged.emit(False)

    def stop(self) -> None:
        self._player.stop()
        self._timer.stop()
        self._source = None
        self._limit_us = None
        self._advancing = False
        self._pending_seek_ms = None
        self._play_when_ready = False
        # Reset gap state on stop
        self._in_gap = False
        self._gap_start_wall_us = None
        self._gap_start_timeline_us = None
        self._playing = False
        self.stateChanged.emit(False)

    def seek(self, instant_us: int) -> None:
        """Move playhead, continuing to play if it already was.

        If currently playing (user started playback), continue through clips and
        gaps. If stopped, just move playhead without starting.
        """
        cursor = self._require_cursor()
        cursor.instant_us = instant_us
        
        was_playing = self.is_playing
        
        # Check if seeking into a gap
        source = cursor.source_at(instant_us)
        if source is None:
            # Seeking into a gap
            if was_playing:
                # Continue playing through the gap
                self._enter_gap(instant_us)
                self._timer.start()
            else:
                # Just move playhead, don't start playing
                self._in_gap = True
                import time
                self._gap_start_wall_us = int(time.time() * 1_000_000)
                self._gap_start_timeline_us = instant_us
                self._frozen_gap_position_us = instant_us
                self._source = None
                self._timer.stop()
                if self.is_playing:
                    self._player.pause()
            return
        
        # Seeking into a clip
        if self._in_gap:
            self._exit_gap(source)
        else:
            self._load(source)
        if was_playing:
            self._start_playback()

    # -- internals -------------------------------------------------------

    def _require_cursor(self) -> Cursor:
        if self._cursor is None:
            raise PlayerError("no timeline set; call set_timeline first")
        return self._cursor

    def _enter_gap(self, instant_us: int) -> None:
        """Enter gap mode: playhead advances silently through the gap."""
        import time
        self._source = None
        self._in_gap = True
        self._gap_start_wall_us = int(time.time() * 1_000_000)
        self._gap_start_timeline_us = instant_us
        self._frozen_gap_position_us = None
        # Stop any currently playing audio
        if self.is_playing:
            self._player.pause()
        self._emit_state_changed()

    def _exit_gap(self, source: Source) -> None:
        """Exit gap mode: load and play the given clip."""
        self._in_gap = False
        self._gap_start_wall_us = None
        self._gap_start_timeline_us = None
        self._frozen_gap_position_us = None
        self._load(source)
        self._start_playback()
        # State emitted by _on_state when clip starts playing

    def _load(self, source: Source | None) -> None:
        if source is None:
            self.stop()
            return
        self._source = source
        # Declare seek *before* asking for media: loading is async and
        # setPosition() issued during it is silently dropped -- how a click in
        # middle of clip used to end up playing from beginning.
        # _apply_pending_seek issues it now if media can honour it, otherwise
        # when it reports LoadedMedia.
        self._pending_seek_ms = source.offset_ms
        self._play_when_ready = False
        self._setting_source = True
        try:
            self._player.setSource(QUrl.fromLocalFile(str(source.path)))
        finally:
            self._setting_source = False
        self._apply_pending_seek()
        # Announce intended instant, not read-back: while seek pending backend
        # still reports file's own start.
        self.positionChanged.emit(source.start_us)

    def _apply_pending_seek(self) -> None:
        """Issue pending seek, but only where Qt will not drop it.

        Whether media is ready read from ``mediaStatus()`` *after* ``setSource``
        returned, separating two cases without waiting:

        - file player doesn't have yet goes to ``LoadingMedia`` synchronously
          inside call, so seek waits for that load's ``LoadedMedia``;
        - clip already loaded (seeking within it) stays ``LoadedMedia`` and
          same-URL ``setSource`` emits **no status change at all**, so seek
          waiting for event there would wait forever.

        Status re-read not trusted from event because ``setSource`` also
        re-emits leftover ``LoadedMedia`` for media it's replacing; applying
        seek on that would aim at wrong file and lose it before real load
        finishes. Events arriving while ``_setting_source`` set are ignored,
        and status check rejects stragglers arriving once call returned.
        """
        if self._pending_seek_ms is None or self._source is None:
            return
        if self._player.mediaStatus() != QMediaPlayer.MediaStatus.LoadedMedia:
            return  # applied by _on_status when media arrives
        seek_ms = self._pending_seek_ms
        self._pending_seek_ms = None
        self._player.setPosition(seek_ms)

    def _start_playback(self) -> None:
        """Play, waiting out load if seek still pending.

        ``play()`` during load would start audio at file's beginning and only
        then jump to seek point, so request waits and ``_on_status`` plays it
        after seek landed.
        """
        if self._pending_seek_ms is not None:
            self._play_when_ready = True
            return
        self._player.play()

    def _on_poll(self) -> None:
        now = self.position_us
        self.positionChanged.emit(now)

        if self._limit_us is not None and now >= self._limit_us:
            self.pause()
            self.finished.emit()
            return

        if self._in_gap:
            # In gap: check if reached a clip
            if self._cursor is not None:
                source = self._cursor.source_at(now)
                if source is not None:
                    # Reached clip - start playing it
                    self._exit_gap(source)
            return

        # Playing a clip
        if self._source is not None and now >= self._source.end_us:
            self._advance(now)

    def _advance(self, now: int) -> None:
        """Handle clip ending: either enter gap or load next clip."""
        if self._advancing or self._cursor is None:
            return

        # Check if there's a gap after this clip
        if isinstance(self._source, Source):
            gap = self._cursor.gap_after(self._source)
            if gap is not None:
                # There's a gap - enter gap mode
                self._enter_gap(gap.start_us)
                return

        # No gap - look for next clip
        following = self._cursor.source_at(now)
        if following is None:
            # No more clips - check if we're at the end of everything
            target = self._timeline.nearest_covered(now) if self._timeline else None
            # If nearest_covered returns an instant that's not actually ahead of us,
            # we're at the end of the archive
            if target is None or target <= now:
                self.pause()
                self.finished.emit()
                return
            # There's a clip ahead but we're in a gap - this shouldn't happen
            # since we handle gaps above, but just in case
            self._enter_gap(now)
            return

        # Load and play the next clip
        self._advancing = True
        try:
            was_playing = self.is_playing
            self._load(following)
            if was_playing:
                self._start_playback()
        finally:
            self._advancing = False

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            # A LoadedMedia emitted while setSource is still running belongs
            # to the media being replaced, not to the clip just requested --
            # see _apply_pending_seek for why that distinction matters.
            if self._setting_source:
                return
            # Seek first, play second: the media arriving is the moment the
            # pending seek becomes honourable, and nothing may be rendered
            # before it.
            self._apply_pending_seek()
            if self._play_when_ready:
                self._play_when_ready = False
                self._player.play()
            return
        if status != QMediaPlayer.MediaStatus.EndOfMedia:
            return
        # EndOfMedia only matters when playing a clip; in a gap there's no media
        if self._source is None or self._cursor is None or self._in_gap:
            return
        # EndOfMedia arrives on a natural clip end, possibly before the next
        # poll. _advance is a no-op while _advancing is set, so the two paths
        # cannot double-advance.
        self._advance(self._source.end_us)

    def _on_error(self, _error, message: str) -> None:
        # Nothing is going to be applied to media that failed to load.
        self._pending_seek_ms = None
        self.pause()
        self.error.emit(message or "playback failed")

    def _on_state(self, state) -> None:
        # Emit based on overall playback state (including gap mode)
        self.stateChanged.emit(self.is_playing)

    # -- convenience for the UI -----------------------------------------

    def __enter__(self) -> "Player":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
        self._release_render()

    def _release_render(self) -> None:
        """Delete the preview render, if any. Detaches the source first."""
        if self._render_dir is None:
            return
        self._player.setSource(QUrl())
        self._render_dir.cleanup()
        self._render_dir = None