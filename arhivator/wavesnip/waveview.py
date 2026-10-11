"""The waveform view: a window onto the whole archive.

Paints itself rather than drawing into a ``QPainter`` handed over by someone
else, so the widget owns its layout and tests can drive ``resize``/``paint``
directly. Drawing maths lives in :mod:`wavesnip.peaks`, zoom maths in
:mod:`wavesnip.viewport`; this file turns numbers into pixels.

**It is a timeline, not a clip viewer.** The view range is whatever the user has
zoomed to, up to the entire archive. The axis must work from one second to a
year, the waveform must admit when it cannot be drawn, and panning needs its
own gesture.

**Gaps are drawn, not omitted.** A hole in an archive looks identical to silence
if you only draw the audio, but they need different responses.

**Gestures.** Left-drag pans, right-drag selects a snip range, left click without
travel moves the cursor, wheel zooms about the pointer. The split between the
two buttons is deliberate: left is "drag the thing you're looking at" (panning),
right is "mark something" (selecting a range). Both were once on the left
button with a shift modifier -- and a modifier nobody can see is one nobody
remembers.

**The cursor cannot be placed precisely at every zoom level.** One pixel across
a year of audio is many hours; a click at the widest zoom lands hours away.
The view reports the exact instant it chose; the date field is the precise way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from datetime import datetime, timedelta
from wavesnip.model import TimeBlock, from_us, to_us
from wavesnip.peaks import Peaks, column_edges, covered_columns
from wavesnip.timeline import Gap
from PySide6.QtCore import Signal
from wavesnip.viewport import (
    Limits,
    DAY,
    choose_ticks,
    choose_day_ticks,
    clamp_view,
    fit_view,
    pan_to,
    zoom_about,
)

__all__ = ["WaveView", "WaveViewState"]

#: Below this width there is no horizontal padding worth reserving.
_MIN_WIDTH = 120

#: Wheel zoom step: ~1.15x per notch, fast enough to cross a year, slow enough that a stray graze doesn't throw the user.
_ZOOM_STEP = 1.15

#: One time-block row height in pixels.
_TB_ROW_HEIGHT = 30

#: Gap between time blocks and the tick strip. Design.md asks for this air; only taken when the area is tall enough.
_TB_BOTTOM_MARGIN = 6

#: Fraction of content rect height for clip blocks + waveform. Exact fraction keeps arithmetic in integers.
_WAVE_BAND = 4, 5

#: Minimum time block width in pixels.
_MIN_BLOCK_WIDTH = 3

#: Horizontal gap between axis label and tick rule / widget edge.
_LABEL_MARGIN = 4


@dataclass
class WaveViewState:
    """Everything the view draws, in microseconds.

    ``coverage`` and ``gaps`` come from the timeline; ``peaks`` is whatever audio
    was last rendered for this exact range and may be empty while a read is in
    flight.

    ``clips`` is ``(start_us, end_us, name)`` for visible clips, handed over
    rather than reached for: the view has no business knowing what an archive is.

    ``peaks_possible`` is separate from ``peaks is None`` on purpose. "Not yet"
    and "not ever, at this zoom" look identical to the painter but mean opposite
    things to the user: one is a moment's wait, the other is a reason to zoom in.

    ``time_blocks`` is a list of TimeBlock objects in the visible range with
    their assigned row for overlapping display.
    """

    start_us: int = 0
    end_us: int = 1
    coverage: list[tuple[int, int]] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    clips: list[tuple[int, int, str]] = field(default_factory=list)
    peaks: Peaks | None = None
    peaks_match: bool = False  # do the peaks actually describe this range?
    peaks_possible: bool = True  # could they ever, at this zoom?
    playhead_us: int | None = None
    snip_from_us: int | None = None
    snip_to_us: int | None = None
    label: str = ""
    time_blocks: list[tuple[int, int, str, int, int, 'TimeBlock']] = field(default_factory=list)
    selected_time_block: tuple[int, int, str, int] | None = None  # (start_us, end_us, name, episode_num)


class WaveView(QWidget):
    """Coverage map plus waveform envelope, over a zoomable range of time."""

    playheadMoved = Signal(object)  # instant, microseconds
    snipSelected = Signal(object, object)  # from_us, to_us
    snipCleared = Signal()
    #: The instant that was right-clicked, in microseconds. ``object`` and not
    #: ``int``: microseconds since 1970 is about 1.8e15, far past what a C long
    #: holds, and shiboken answers the overflow with a warning and a mangled
    #: value rather than an exception. Every other instant in here is ``object``
    #: for the same reason -- this one had been left behind.
    requestContextMenu = Signal(object)  # instant_us
    #: The view range changed. Emitted for zoom, pan, and scrollbar moves, so the
    #: window can keep the scrollbar in step without polling the widget.
    viewChanged = Signal(object, object)  # start_us, end_us

    # Time block signals
    timeBlockSelected = Signal(object)  # (start_us, end_us, name, episode_num)
    timeBlockContextMenu = Signal(object, object)  # instant_us, block_data (start_us, end_us, name, episode_num)

    #: A drag shorter than this is a click, not a range.
    _DRAG_THRESHOLD_PX = 4

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumWidth(_MIN_WIDTH)
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.StrongFocus)

        self._state = WaveViewState()
        # Memoized column grid. Computing ``_edges`` allocates a fresh
        # ``width + 1`` int64 array; ``x_of`` used to call it on every use --
        # ~12k allocations/frame with thousands of clips. Keyed by (start, end, width).
        self._edges_key: tuple[int, int, int] | None = None
        self._edges_cache: np.ndarray | None = None
        # Memoized per-view crop of archive map and packed clip boundaries.
        # Repaint is far more common than view change (playhead, hover, snip).
        # Keyed by list identity + view, so rebuilt state lands on empty caches.
        self._cover_key: tuple[object, int, int, int, int] | None = None
        self._cover_cols: np.ndarray | None = None
        self._pack_key: tuple[object, int] | None = None
        self._pack_starts: np.ndarray | None = None
        self._pack_ends: np.ndarray | None = None
        # Memoized width of narrowest drawable clip name. ``horizontalAdvance``
        # through shiboken costs ~0.2 ms/call -- nothing once, an insult per repaint.
        self._narrowest_key: tuple[object, ...] | None = None
        self._narrowest: int | None = None
        self._limits = Limits(start_us=0, end_us=1)
        # Whether the user has deliberately moved the view. Until they have,
        # the view tracks the archive's extent -- see :meth:`set_limits`.
        self._view_chosen = False
        self._drag_origin: int | None = None
        self._dragging = False
        self._pan_origin: int | None = None
        self._pan_press_x: float = 0.0
        self._pan_view: tuple[int, int] | None = None
        # Right is the selection button; Qt's default is to answer a right
        # press with a context menu. A right-drag would summon one on some
        # platforms and not others -- the gesture must be the same everywhere.
        self.setContextMenuPolicy(Qt.NoContextMenu)

        self._col_bg = QColor(28, 30, 34)
        self._col_wave = QColor(126, 199, 240)
        self._col_wave_dim = QColor(70, 104, 128)
        self._col_playhead = QColor(255, 214, 102)
        self._col_snip = QColor(255, 214, 102, 38)
        self._col_snip_edge = QColor(255, 214, 102, 150)
        self._col_text = QColor(160, 168, 178)
        self._col_hint = QColor(120, 128, 138)
        #: Barely visible red for Sunday indicators
        self._col_sunday = QColor(255, 60, 60, 25)
        #: Backing behind a clip name. Nearly opaque rather than solid, so the
        #: waveform still reads through it -- the label sits on top of the audio
        #: it is naming and hiding that audio entirely would be a poor trade.
        self._col_label_bg = QColor(16, 18, 22, 190)

        # Time block palette. Unused -- a block carries its own colours, derived
        # from its name, and the painter reads those.
        self._col_tb_bg = [
            QColor(100, 180, 100, 180),   # row 0 - greenish
            QColor(180, 100, 100, 180),   # row 1 - reddish
            QColor(100, 100, 180, 180),   # row 2 - bluish
        ]
        self._col_tb_edge = [
            QColor(60, 140, 60, 220),
            QColor(140, 60, 60, 220),
            QColor(60, 60, 140, 220),
        ]
        self._col_tb_text = QColor(255, 255, 255)
        self._col_tb_selected = QColor(255, 255, 100, 220)

        # View settings (can be changed at runtime via Options dialog)
        self._show_blocks = True
        self._show_clips = True
        self._show_waveforms = True
        self._keep_playhead_visible = False

    # -- state -----------------------------------------------------------

    def set_state(self, state: WaveViewState) -> None:
        self._state = state
        self.update()

    def state(self) -> WaveViewState:
        return self._state

    def set_playhead(self, instant_us: int | None) -> None:
        self._state.playhead_us = instant_us
        self.update()

    def set_snip(self, from_us: int | None, to_us: int | None) -> None:
        self._state.snip_from_us = from_us
        self._state.snip_to_us = to_us
        self.update()

    def clear_snip(self) -> None:
        self.set_snip(None, None)
        self.snipCleared.emit()

    def set_selected_time_block(self, tb_data: tuple[int, int, str, int] | None) -> None:
        """Set the selected time block for editing highlight."""
        self._state.selected_time_block = tb_data
        self.update()

    # -- view settings -----------------------------------------------------

    def set_block_height(self, height: int) -> None:
        """Set the time block row height in pixels."""
        global _TB_ROW_HEIGHT
        _TB_ROW_HEIGHT = max(10, min(100, height))
        self.update()

    def set_block_margin(self, margin: int) -> None:
        """Set the margin between time blocks and the tick strip."""
        global _TB_BOTTOM_MARGIN
        _TB_BOTTOM_MARGIN = max(0, min(50, margin))
        self.update()

    def set_show_blocks(self, show: bool) -> None:
        """Show or hide time blocks."""
        self._show_blocks = show
        self.update()

    def set_show_clips(self, show: bool) -> None:
        """Show or hide clip blocks."""
        self._show_clips = show
        self.update()

    def set_show_waveforms(self, show: bool) -> None:
        """Show or hide waveform envelope."""
        self._show_waveforms = show
        self.update()

    def set_keep_playhead_visible(self, keep: bool) -> None:
        """Whether to auto-scroll to keep playhead visible during playback."""
        self._keep_playhead_visible = keep

    # -- limits and view -------------------------------------------------

    def set_limits(self, limits: Limits) -> None:
        """Adopt the archive's extent, keeping the current span if it still fits.

        Adding a second archive should not throw away the zoom the user chose.
        Until the user *has* chosen one, the view follows the archive -- which
        makes every clip visible on launch.

        The test for "the user has chosen" cannot be the state's own span. An
        empty archive yields limits of ``(0, 1)``, clamped to ``min_span_us``;
        by the time the real extent arrives the state holds a legal one-second
        window, and any guess based on its size concludes the user meant to be
        zoomed in to a second. That is how the view opened on one second of
        silence instead of the whole archive.
        """
        self._limits = limits
        start, end = self._state.start_us, self._state.end_us
        if not self._view_chosen:
            start, end = fit_view(limits)
        # emit=False: the window learns from its own rebuild; a viewChanged here
        # would be answered by a _refresh_wave still halfway through.
        self.set_view(start, end, emit=False)
        self.update()

    def limits(self) -> Limits:
        return self._limits

    def view(self) -> tuple[int, int]:
        return self._state.start_us, self._state.end_us

    def set_view(
        self, start_us: int, end_us: int, *, emit: bool = True, user: bool = False
    ) -> tuple[int, int]:
        """Move the view, clamped to the archive. Emits :attr:`viewChanged`.

        ``user`` marks this as a deliberate move rather than the view arranging
        itself, and tells :meth:`set_limits` whether a later, wider archive
        should widen the view or leave the zoom alone. Scrolling to follow
        playback is *not* a user move: counting it as one would pin the view to
        the first thing played, and the archive would never appear in full.

        Returns the range actually adopted, which differs from the request when
        it was off the archive or an illegal size -- callers need the clamped one.
        """
        start_us, end_us = clamp_view(start_us, end_us, self._limits)
        if user:
            self._view_chosen = True
        if (start_us, end_us) != (self._state.start_us, self._state.end_us):
            self._state.start_us, self._state.end_us = start_us, end_us
            if emit:
                self.viewChanged.emit(start_us, end_us)
            self.update()
        return start_us, end_us

    def fit_all(self) -> None:
        """Zoom out to show every clip, which is where the app starts."""
        self.set_view(*fit_view(self._limits), user=True)

    def zoom_at(self, x: float, factor: float) -> None:
        """Zoom by ``factor`` keeping the instant under pixel ``x`` fixed."""
        self.set_view(
            *zoom_about(
                self._state.start_us,
                self._state.end_us,
                self.instant_at(x),
                factor,
                self._limits,
            ),
            user=True,
        )

    def zoom_in(self) -> None:
        self.zoom_at(self._content_rect().center().x() + 0.5, 1 / _ZOOM_STEP)

    def zoom_out(self) -> None:
        self.zoom_at(self._content_rect().center().x() + 0.5, _ZOOM_STEP)

    def pan_to(self, centre_us: int, *, user: bool = True) -> None:
        """Bring an instant into the middle of the view, keeping the zoom."""
        self.set_view(
            *pan_to(self._state.start_us, self._state.end_us, centre_us, self._limits),
            user=user,
        )

    def ensure_visible(self, instant_us: int) -> None:
        """Scroll only if ``instant_us`` has left the view.

        Following playback by always re-centring would make the waveform crawl
        continuously. Scrolling only when the playhead actually goes off screen
        keeps the picture still.
        """
        start, end = self._state.start_us, self._state.end_us
        margin = (end - start) // 20
        if start + margin <= instant_us <= end - margin:
            return
        # user=False: following the playhead is not the user choosing a range.
        self.pan_to(instant_us, user=False)

    @staticmethod
    def assign_time_block_rows(blocks: list[TimeBlock]) -> list[tuple[int, int, str, int, int, TimeBlock]]:
        """Give every block a row, growing the stack as overlap demands.

        Returns (start_us, end_us, name, episode_num, row, TimeBlock), rows
        numbered from the floor up: row 0 is earliest in time and painted lowest.

        **No ceiling on the stack.** Greedy first-fit over blocks sorted by
        start, starting a new row only when every existing one is occupied --
        four mutually overlapping shows get rows 0-3, not three rows with a
        fourth painted on top of the third (the smear separate rows prevent).

        Which rows can actually be *drawn* is the viewport's answer, not this
        one's: ``_time_block_row_rect`` drops rows the content rect is too
        short to hold. A cap here would decide which blocks are never
        considered for drawing without knowing the window height.
        """
        # Sort by start time
        sorted_blocks = sorted(blocks, key=lambda b: b.start_us)
        rows: list[list[tuple[int, int]]] = []
        result = []

        for block in sorted_blocks:
            # First row where this block does not overlap what is already in it
            for row_idx, row_blocks in enumerate(rows):
                overlaps = any(
                    block.start_us < existing_end and existing_start < block.end_us
                    for existing_start, existing_end in row_blocks
                )
                if not overlaps:
                    break
            else:
                # Every row is spoken for -- start the next one.
                rows.append([])
                row_idx = len(rows) - 1

            rows[row_idx].append((block.start_us, block.end_us))
            result.append((block.start_us, block.end_us, block.name, block.episode_num, row_idx, block))

        return result

    # -- geometry --------------------------------------------------------

    def _content_rect(self) -> QRect:
        """The drawing area, inset for the time axis (two strips) at the bottom.

        Everything the view draws lives here: clip blocks and waveform in the
        band ``_wave_rect`` returns, Sunday wash across full height, time blocks
        in rows rising from the floor, axis below.

        ``width()`` is 0 until layout; tests and signals can fire before that, so
        clamped to at least one column.
        """
        axis = 36  # two strips: upper (zoom-scaling) + lower (day-only) at very bottom
        return QRect(0, 0, max(1, self.width()), max(1, self.height() - axis))

    def _wave_rect(self, area: QRect) -> QRect:
        """The band of ``area`` that clip blocks and waveform occupy.

        Four fifths of the height, centred. Drawn edge-to-edge the clip blocks
        *are* the widget; a margin makes them objects rather than wallpaper.

        **Centred, not top-aligned.** The margin belongs to the drawing, not the
        time-block stack; taking it from the top alone would leave the map hung
        from the ceiling of its own box.

        An area too short to lose a fifth keeps what it has: the band is clamped
        to ``area`` before margins, so a tiny widget still draws everything.
        """
        numerator, denominator = _WAVE_BAND
        band = max(1, area.height() * numerator // denominator)
        band = min(band, area.height())
        return QRect(
            area.left(),
            area.top() + (area.height() - band) // 2,
            area.width(),
            band,
        )

    def _span_us(self) -> int:
        return max(1, self._state.end_us - self._state.start_us)

    def _edges(self) -> np.ndarray:
        """Column boundaries in microseconds; the same grid ``peaks`` uses.

        Memoized: pure function of view range and widget width, shared read-only
        by all callers (``x_of``, ``instant_at``, painters).
        """
        area = self._content_rect()
        key = (self._state.start_us, self._state.end_us, area.width())
        if self._edges_key != key:
            self._edges_key = key
            self._edges_cache = column_edges(*key[:2], key[2])
        return self._edges_cache

    def _covered_columns(self) -> np.ndarray:
        """Covered-columns map for the current view, memoized per view.

        ``covered_columns`` costs a numpy pass over every coverage run, but only
        changes when the view changes. A repaint over the same view was paying
        that pass again for a result already on hand.
        """
        area = self._content_rect()
        cov = self._state.coverage
        key = (
            id(cov),
            len(cov),
            self._state.start_us,
            self._state.end_us,
            area.width(),
        )
        if self._cover_key != key:
            self._cover_key = key
            self._cover_cols = covered_columns(
                cov, self._state.start_us, self._state.end_us, area.width()
            )
        return self._cover_cols

    def _pack_clips(self) -> tuple[np.ndarray, np.ndarray]:
        """``(starts, ends)`` int64 arrays for visible clips, memoized.

        Building these used to be the cost of the label pass: two ``fromiter``
        walks over clip tuples on every repaint. The clip list is stable between
        state changes, so packed arrays survive repaints.
        """
        clips = self._state.clips
        key = (id(clips), len(clips))
        if self._pack_key != key:
            self._pack_key = key
            self._pack_starts = np.fromiter(
                (c[0] for c in clips), dtype=np.int64, count=len(clips)
            )
            self._pack_ends = np.fromiter(
                (c[1] for c in clips), dtype=np.int64, count=len(clips)
            )
        return self._pack_starts, self._pack_ends

    def _label_narrowest(self) -> int:
        """Width of narrowest drawable clip name for current font."""
        font = self.font()
        key = (
            font.family(),
            font.pointSizeF(),
            font.pixelSize(),
            font.weight(),
            font.italic(),
        )
        if self._narrowest_key != key:
            self._narrowest_key = key
            self._narrowest = QFontMetrics(font).horizontalAdvance("…") + 4
        return self._narrowest

    def x_of(self, instant_us: int) -> int:
        """Column containing an instant, as pixel x.

        Defined by column grid, not by scaling a fraction, so a column boundary
        is the same pixel regardless of resize -- playhead doesn't jitter.
        """
        area = self._content_rect()
        edges = self._edges()
        index = int(np.searchsorted(edges, instant_us, side="right")) - 1
        return area.left() + min(max(index, 0), area.width() - 1)

    def instant_at(self, x: float) -> int:
        """Instant under pixel x.

        Interpolated *within* the column, so a click is as accurate as the pixel
        allows rather than snapping to column's left edge. At a year-wide zoom
        one pixel is hours; the date field is the precise way in. Snapping would
        add a whole column of error on top.

        Guarantee: ``x_of(instant_at(x))`` is the column the click landed in.
        """
        area = self._content_rect()
        width = area.width()
        offset = min(max(float(x) - area.left(), 0.0), width - 1.0)

        edges = self._edges()
        index = min(int(offset), width - 1)
        low = int(edges[index])
        high = int(edges[index + 1])
        return int(low + (high - low) * (offset - index))

    def span_per_pixel(self) -> float:
        """Time one pixel covers, in seconds -- for the status bar."""
        return self._span_us() / max(1, self._content_rect().width()) / 1_000_000

    # -- interaction -----------------------------------------------------

    def wheelEvent(self, event) -> None:
        """Zoom about the pointer.

        Not scroll: no vertical dimension, wheel is zoom in every audio editor.
        Delta is normalised -- otherwise a high-res wheel flies, a coarse one crawls.
        """
        delta = event.angleDelta().y()
        if delta == 0:
            return
        notches = delta / 120.0
        # Scrolling *up* zooms in -- what every audio editor does and what the
        # finger expects: pushing the wheel away shouldn't push the archive away.
        # Hence the negation: `zoom_at` takes a factor where > 1 zooms out, and
        # a wheel's positive delta is up. Trackpad reports fractional deltas
        # continuously, hence the power rather than fixed step.
        self.zoom_at(event.position().x(), _ZOOM_STEP ** (-notches))
        event.accept()

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        y = event.position().y()
        if event.button() == Qt.LeftButton:
            # Check if left-click is on a time block
            tb_data = self._time_block_at(int(x), int(y))
            if tb_data is not None:
                # Left-click on time block: select it for editing
                self.timeBlockSelected.emit(tb_data)
                return
            # Empty space does **not** deselect. A press here starts a region
            # drag; ending selection on it meant the pointer wandered through
            # empty canvas on its way somewhere else and the editor went with it.
            # Selection now ends in four places only: Esc, another block, the
            # menu's "Deselect block", and a delete.
            # Left button: region selection (snip)
            self._drag_origin = self.instant_at(x)
            self._dragging = False
            return
        if event.button() in (Qt.MiddleButton, Qt.RightButton):
            # Middle or right button: pan
            self._pan_origin = self.instant_at(x)
            self._pan_press_x = x
            self._pan_view = (self._state.start_us, self._state.end_us)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x = event.position().x()
        # Pan with middle/right button
        if self._pan_view is not None and self._pan_origin is not None:
            if abs(x - self._pan_press_x) < self._DRAG_THRESHOLD_PX:
                return
            self.setCursor(Qt.ClosedHandCursor)
            # Dragging left moves view right, so audio follows mouse.
            start, end = self._pan_view
            shift = self._pan_shift(x)
            self.set_view(start + shift, end + shift, user=True)
            return

        # Region selection with left button
        instant = self.instant_at(x)
        if self._drag_origin is None:
            self._set_tooltip(event, instant)
            return

        if not self._dragging:
            # Distinguish click from drag by distance, not time, so a slow
            # deliberate click is still a click.
            travelled = abs(x - self.x_of(self._drag_origin))
            if travelled < self._DRAG_THRESHOLD_PX:
                return
            self._dragging = True

        low, high = sorted((self._drag_origin, instant))
        self.set_snip(low, high)
        self._set_tooltip(event, instant)

    def _pan_shift(self, x: float) -> int:
        """How far the view moves, in microseconds, for the pointer at ``x``.

        Measured in *pixels from the press*, against the view at press time --
        both ends fixed for the gesture.

        The obvious version isn't that: take the instant under the pointer now
        and subtract the instant under the press. But the view moves on every
        move event, so the second event measures in a frame the first already
        shifted, and the shift it gets back is smaller by exactly the move that
        produced it. The two answers alternate -- zero, whole shift, zero -- and
        the view oscillates around the press. That's the jitter: not two widgets
        fighting, but one measurement taken in a frame moving underneath it.

        Neither end moves now: ``self._pan_view`` is the press-time window and
        ``self._pan_press_x`` is where the press happened, so every event
        answers the same question -- how far has the pointer travelled, and that
        is how far the view travels.
        """
        start, end = self._pan_view
        width = max(1, self._content_rect().width())
        # Span fixed for the gesture (a pan never zooms), so µs/px constant.
        # Pointer right of press (positive) sends view back in time (negative).
        return -int(round((x - self._pan_press_x) * (end - start) / width))

    def mouseReleaseEvent(self, event) -> None:
        x = event.position().x()
        if event.button() == Qt.LeftButton:
            # Left button: region selection release
            origin, self._drag_origin = self._drag_origin, None
            if origin is None:
                return
            if not self._dragging:
                # A left press that never travelled: place cursor
                self.set_playhead(origin)
                self.playheadMoved.emit(origin)
                return
            self._dragging = False
            end = self.instant_at(x)
            low, high = sorted((origin, end))
            # A drag that collapsed to a point is a click that wandered.
            if high - low <= 0:
                self.clear_snip()
                # Treat collapsed drag as a click: place cursor
                self.set_playhead(origin)
                self.playheadMoved.emit(origin)
                return
            self.set_snip(low, high)
            self.snipSelected.emit(low, high)
            return

        if event.button() in (Qt.MiddleButton, Qt.RightButton):
            # Middle/right button: pan release
            self.unsetCursor()
            origin, self._pan_origin = self._pan_origin, None
            self._pan_view = None
            if origin is None:
                return
            if abs(x - self._pan_press_x) < self._DRAG_THRESHOLD_PX:
                # A press that never travelled: right-click context menu
                if event.button() == Qt.RightButton:
                    # Check if right-click is on a time block
                    tb_data = self._time_block_at(int(event.position().x()), int(event.position().y()))
                    if tb_data is not None:
                        instant = self.instant_at(event.position().x())
                        self.timeBlockContextMenu.emit(instant, tb_data)
                        return
                    # Emit requestContextMenu for the right-click without drag.
                    #
                    # The instant comes from the *pan* origin, where the right
                    # button went down -- the menu is about that place. This
                    # used to read `_drag_origin`, only ever set by a left press
                    # and always None here, leaving the menu to announce instant
                    # 0: "seek cursor from here" jumped to 1970. The fallback is
                    # the release position rather than 0 -- a wrong instant is
                    # worse than no menu.
                    instant = origin if origin is not None else self.instant_at(x)
                    self.requestContextMenu.emit(int(instant))
                return
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Double-clicking a block makes that block the selected region.

        A block's bounds are already known and on screen, so double clicking
        "yes, *this*" selects them rather than asking the user to drag a range
        the widget can read off itself. Same gesture as click-to-select-a-file,
        applied to the one rectangle that is a thing rather than a place.

        Both signals go out: :attr:`snipSelected` carries the region, which was
        asked for; :attr:`timeBlockSelected` carries the block, which was
        double-clicked. The first press of the double-click normally emits the
        latter already, but emitting it in both places means the two never come
        apart if a platform delivers the double-click without the preceding press.
        """
        if event.button() == Qt.LeftButton:
            tb_data = self._time_block_at(int(event.position().x()), int(event.position().y()))
            if tb_data is not None:
                start_us, end_us, _name, _episode = tb_data
                self.timeBlockSelected.emit(tb_data)
                self.snipSelected.emit(start_us, end_us)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)

    def _set_tooltip(self, event, instant_us: int) -> None:
        resolved = self._state.coverage and any(
            s <= instant_us < e for s, e in self._state.coverage
        )
        kind = "audio" if resolved else "gap"
        span = self.span_per_pixel()
        precision = (
            "" if span < 1 else f"\n±{span / 3600:.1f} h per pixel"
        )
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{from_us(instant_us):%d.%m.%Y. %H:%M:%S}\n{kind}{precision}",
            self,
        )

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._col_bg)

        area = self._content_rect()
        if area.width() < 2 or area.height() < 2:
            painter.end()
            return

        try:
            # Sundays first: weekend is a property of time, washes background.
            self._paint_sundays(painter, area)
            self._paint_clips(painter, area)
            self._paint_peaks(painter, area)
            self._paint_snip(painter, area)
            self._paint_clip_labels(painter, area)
            self._paint_hint(painter, area)
            self._paint_axis(painter)
            # After axis: cursor is one unbroken line over whole widget; a tick
            # label across it would punch a hole in the column the eye follows.
            # Still below blocks -- they are content, it is a marker.
            self._paint_playhead(painter, area)
            self._paint_time_blocks(painter, area)
        finally:
            painter.end()

    def _paint_clips(self, painter: QPainter, area: QRect) -> None:
        """Draw clip blocks: runs of columns that have audio.

        **Gaps are not drawn.** A column with no audio is the background, empty,
        because that's what it is; no reason to paint a hole. The old gap fill
        washed a dark red field across the viewport and left the reader to work
        out that the *absence* of a block was the information. Now only what
        exists is drawn: a clip is a block, everything around it is background.

        Blocks are drawn in the band (:meth:`_wave_rect`), so they read as
        objects in the viewport, not the viewport's colour -- Sunday wash behind
        them shows in margins above and below.
        """
        if not getattr(self, '_show_clips', True):
            return

        cols = self._covered_columns()
        wave = self._wave_rect(area)

        # One rect per run of covered columns: 1000 one-pixel rects = 1000x work.
        #
        # Rect is in *column indices* (pixel coordinates by construction -- one
        # column per pixel). Alternative (looking up pixel edge in `column_edges`)
        # hands microseconds to QRect (32-bit ints). A 2026 timestamp is ~1.8e15,
        # so that overflows, and a paint event that raises leaves widget unpainted.
        for run_start, run_end in _true_runs(cols):
            painter.fillRect(
                QRect(
                    wave.left() + run_start,
                    wave.top(),
                    run_end - run_start,
                    wave.height(),
                ),
                QBrush(self._col_wave_dim),
            )

    def _paint_sundays(self, painter: QPainter, area: QRect) -> None:
        """Wash Sundays red across whole widget height.

        From top to bottom edge, axis included. Weekend belongs to time axis, so
        wash runs behind clip blocks -- clean, untinted blocks -- and shows in
        margins above/below them and in axis strips, where week's shape is
        readable at any zoom. ``area`` accepted for consistency but unused: only
        whole widget is painted.
        """
        start = self._state.start_us
        end = self._state.end_us
        if start >= end:
            return

        # First Sunday at or after the view start -- wall-clock Sundays, stepped
        # in real calendar days exactly as before (the absolute-clock equivalent
        # would drift by an hour at a DST transition and paint the wrong Sunday).
        start_dt = from_us(start)
        days_until_sunday = (6 - start_dt.weekday()) % 7  # Monday=0, Sunday=6
        first_sunday = datetime(start_dt.year, start_dt.month, start_dt.day)
        first_sunday += timedelta(days=days_until_sunday)
        end_dt = from_us(end)

        # Collect the Sundays once, cheaply, instead of converting a datetime and
        # searching an edge per Sunday on every repaint. A year of Sundays added
        # up to most of this layer's cost while the picture stayed a handful of
        # full-height rects.
        instants: list[int] = []
        cursor = first_sunday
        while cursor < end_dt:
            instants.append(to_us(cursor))
            cursor += timedelta(days=7)
        if not instants:
            return

        sundays = np.array(instants, dtype=np.int64)
        # The old loop drew only Sundays whose midnight instant sits inside the
        # view; mirror that condition exactly.
        visible = (sundays >= start) & (sundays <= end)
        if not visible.any():
            return

        # Map both ends of every visible Sunday at once, on the same column grid
        # x_of uses, so the rects land on the very columns the grid assigns.
        edges = self._edges()
        x0s = np.clip(
            np.searchsorted(edges, np.maximum(sundays[visible], start), side="right") - 1,
            0,
            edges.size - 2,
        )
        x1s = np.clip(
            np.searchsorted(edges, np.minimum(sundays[visible] + DAY, end), side="right") - 1,
            0,
            edges.size - 2,
        )
        keep = x1s > x0s
        if not keep.any():
            return

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(self._col_sunday))
        painter.drawRects(
            [
                QRect(int(x0), 0, max(1, int(x1) - int(x0)), self.height())
                for x0, x1 in zip(x0s[keep].tolist(), x1s[keep].tolist())
            ]
        )

    def _paint_peaks(self, painter: QPainter, area: QRect) -> None:
        """Draw envelope: min/max outline with typical level inside.

        Skipped when peaks were computed for a different range. Stale peaks
        would draw a waveform saying the wrong time, and a waveform is what
        users trust.

        **Columns don't have to line up with pixels.** Peaks computed at fixed
        resolution; widget is whatever width. Each peak drawn across pixel span
        it covers. One rect per peak would leave wide view blank, narrow smeared.

        Outline is a gradient, bright at top, dim at bottom; RMS band on top is
        solid bright, so typical level reads as lit core inside spikes. Without
        that core, wide view saturates: nearly every column holds a transient,
        outline is full height everywhere, loud and quiet hours become same
        solid block.

        NaN column = no audio behind it (gap in covered range), skipped.
        """
        if not getattr(self, '_show_waveforms', True):
            return

        peaks = self._state.peaks
        if peaks is None or not self._state.peaks_match or len(peaks) == 0:
            return

        wave = self._wave_rect(area)
        mid = wave.top() + wave.height() // 2
        half = max(1, (wave.height() - 2) // 2)
        gradient = QLinearGradient(0, mid - half, 0, mid + half)
        gradient.setColorAt(0.0, self._col_wave)
        gradient.setColorAt(1.0, self._col_wave_dim)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))

        # Pixel span per peak from *count*. Multiplying timestamp by width
        # overflows QRect (32-bit ints, 2026 timestamp ~1.8e15). Counting
        # columns keeps every number a widget dimension.
        width = wave.width()
        count = len(peaks)
        for index in range(count):
            x0 = wave.left() + width * index // count
            x1 = wave.left() + width * (index + 1) // count
            low = float(peaks.mins[index])
            high = float(peaks.maxs[index])
            if low != low or high != high:  # NaN: no audio
                continue
            top = mid - min(1.0, high) * half
            bottom = mid - min(1.0, low) * half
            height = max(1.0, bottom - top)
            painter.drawRect(QRect(x0, round(top), max(1, x1 - x0), round(height)))

        # Band symmetric about centre. Columns without one (hand-built envelope,
        # digital silence) keep outline alone rather than line down middle of nothing.
        rms = peaks.rms
        if rms is None or len(rms) != count:
            return
        painter.setBrush(QBrush(self._col_wave))
        for index in range(count):
            band = float(rms[index])
            if band != band or band <= 0.0:  # NaN: no audio, or flat zero
                continue
            x0 = wave.left() + width * index // count
            x1 = wave.left() + width * (index + 1) // count
            top = mid - min(1.0, band) * half
            bottom = mid + min(1.0, band) * half
            height = max(1.0, bottom - top)
            painter.drawRect(QRect(x0, round(top), max(1, x1 - x0), round(height)))

    def _paint_snip(self, painter: QPainter, area: QRect) -> None:
        low, high = self._state.snip_from_us, self._state.snip_to_us
        if low is None or high is None or high <= low:
            return

        # Whole height, axis included, and *below* the blocks: the selection is
        # a stretch of time, and the time runs the height of the widget whether
        # or not a picture happens to be drawn in the middle of it. Confined to
        # the wave band -- which is what this used to do -- it marked only the
        # part of the range the map was showing, and the two edges stopped at
        # the band's margins with nothing joining them to the axis ticks that
        # say what the range *is*. `area` is accepted for consistency with the
        # other paint helpers; the whole widget is painted.
        x0 = self.x_of(low)
        x1 = self.x_of(high)
        painter.fillRect(QRect(x0, 0, max(1, x1 - x0), self.height()), QBrush(self._col_snip))
        painter.setPen(QPen(self._col_snip_edge, 1))
        painter.drawLine(x0, 0, x0, self.height() - 1)
        painter.drawLine(x1, 0, x1, self.height() - 1)

    def _paint_playhead(self, painter: QPainter, area: QRect) -> None:
        instant = self._state.playhead_us
        if instant is None:
            return
        if not (self._state.start_us <= instant <= self._state.end_us):
            return
        x = self.x_of(instant)
        # Whole widget, top edge to bottom edge, margins and axis included,
        # unlike the map it crosses: this is the cursor, not content, and a
        # cursor you can only see *inside* the band is a cursor that goes
        # missing exactly where you were looking.
        painter.setPen(QPen(self._col_playhead, 1))
        painter.drawLine(x, 0, x, self.height() - 1)

    def _paint_clip_labels(self, painter: QPainter, area: QRect) -> None:
        """Name each visible clip, inside the clip's own columns.

        The point is to know which file you are about to snip, so the label has to
        mean what it says. Three rules follow, and breaking any of them makes the
        label worse than no label at all:

        - **Clipped to the clip.** Text is cut off at the clip's own edges rather
          than allowed to spill, because a name running into its neighbour's gap is
          read as the neighbour's name.
        - **Elided to fit.** A name too long for the columns it has is shortened,
          not overlapped and not wrapped onto a second line that would fall outside
          the clip entirely.
        - **Skipped when there is no room.** Below about one ellipsis of width a
          clip cannot be named at all, and a crowded label where every entry is
          ``…`` tells the user nothing. Silence is honest; a wall of ellipses is
          not.

        One pixel of slack is allowed past the clip's end, because a clip finishing
        mid-column would otherwise lose its last column to rounding.
        """
        clips = self._state.clips
        if not clips:
            return

        metrics = painter.fontMetrics()
        # The labels name the clip blocks, so they sit on the band the clip
        # blocks are drawn in -- at the top of the map, not at the top of the
        # widget above a margin's worth of empty air.
        wave = self._wave_rect(area)
        band = min(wave.height(), metrics.height() + 2)
        baseline = wave.top() + metrics.ascent() + 1
        narrowest = self._label_narrowest()

        painter.setPen(QPen(self._col_text))

        # Map every clip boundary at once, on one column grid, instead of calling
        # x_of twice per clip -- at thousands of visible clips the per-call cost
        # was the repeated _edges() allocation, not the drawing. One
        # searchsorted pass decides which clips even have room for a name; only
        # the survivors get their name looked up and painted. The survivors come
        # out in clip order, keep the "+1 slack" past the clip's end, and are
        # bounded by the view's own edges, exactly as the per-clip lookups were.
        edges = self._edges()
        starts, ends = self._pack_clips()
        width = area.width()
        lefts = np.clip(np.searchsorted(edges, starts, side="right") - 1, 0, width - 1)
        rights = np.minimum(
            np.clip(np.searchsorted(edges, ends, side="right") - 1, 0, width - 1) + 1,
            width,
        )
        rooms = rights - lefts

        clipped = False
        for idx in np.flatnonzero(rooms >= narrowest):
            left = int(lefts[idx])
            right = int(rights[idx])
            room = int(rooms[idx])
            _, _, name = clips[idx]

            text = metrics.elidedText(name, Qt.ElideRight, room - 2)
            if not text:
                continue

            if not clipped:
                painter.setClipRect(QRect(wave.left(), wave.top(), wave.width(), band))
                clipped = True
            # ReplaceClip, not the intersecting default: successive labels are at
            # different x, and intersecting would leave the last clip's label bound
            # by every clip before it.
            painter.setClipRect(
                QRect(left, wave.top(), room, band), Qt.ReplaceClip
            )
            painter.fillRect(
                QRect(left, wave.top(), room, band), QBrush(self._col_label_bg)
            )
            painter.drawText(left + 1, baseline, text)

        if clipped:
            painter.setClipping(False)

    def _paint_hint(self, painter: QPainter, area: QRect) -> None:
        """Say why there is no waveform, rather than leaving a blank band.

        Zoomed out past the point where the visible audio can be read within any
        sane budget, the honest picture is the clip blocks -- and an unexplained
        empty band reads as "the app is broken" rather than "you are zoomed
        out". The alternative, drawing whatever fraction fit, would be a lie about
        which instants have audio.

        It sits below the clip labels, because a sparse archive zoomed this far out
        still has wide clips with names on them, and two overlapping explanations
        are worse than one.
        """
        if self._state.peaks_possible or self._state.label == "no archive":
            return
        if self._state.peaks is not None and self._state.peaks_match:
            return

        span = self.span_per_pixel()
        if span < 60:
            return

        painter.setPen(QPen(self._col_hint))
        text = (
            "Scroll to zoom in."
        )
        metrics = painter.fontMetrics()
        wave = self._wave_rect(area)
        below = wave.top() + metrics.height() + 2 + metrics.ascent() + 4
        if below > wave.bottom():
            below = wave.top() + metrics.ascent() + 4
        painter.drawText(
            max(area.left() + 6, (area.width() - metrics.horizontalAdvance(text)) // 2),
            below,
            text,
        )

    def _paint_axis(self, painter: QPainter) -> None:
        """Time ticks: upper strip scales with zoom, lower strip shows day+weekday."""
        area = self._content_rect()
        axis_top = area.bottom() + 1  # first strip starts here
        strip_height = 18  # each strip is 18px

        # Upper strip: zoom-scaling ticks.
        self._paint_tick_strip(
            painter,
            area,
            choose_ticks(self._state.start_us, self._state.end_us, area.width()),
            axis_top,
            strip_height,
        )
        # Lower strip: day + weekday, which does not scale with zoom.
        self._paint_tick_strip(
            painter,
            area,
            choose_day_ticks(self._state.start_us, self._state.end_us, area.width()),
            axis_top + strip_height,
            strip_height,
        )

    def _paint_tick_strip(
        self, painter: QPainter, area: QRect, ticks: list, top: int, strip_height: int
    ) -> None:
        """One axis strip: a dotted rule and a clamped label at every tick."""
        if not ticks:
            return

        painter.setPen(QPen(self._col_text, 1, Qt.DotLine))
        for instant, _label in ticks:
            x = self.x_of(instant)
            if area.left() <= x <= area.right():
                painter.drawLine(x, top, x, top + strip_height - 1)

        painter.setPen(QPen(self._col_text))
        metrics = painter.fontMetrics()
        for instant, label in ticks:
            x = self.x_of(instant)
            text_width = metrics.horizontalAdvance(label)
            # Left-align with a small margin, clamped to the widget bounds.
            left = min(
                max(area.left() + _LABEL_MARGIN, x + _LABEL_MARGIN),
                self.width() - text_width - _LABEL_MARGIN,
            )
            painter.drawText(left, top + metrics.ascent() + 2, label)

    def _time_block_row_rect(self, area: QRect, row: int) -> QRect | None:
        """The rectangle a time-block row occupies, or ``None`` if it is off-screen.

        **Rows rise from the bottom.** Row 0 -- the first block, and the one a
        reader meets first -- sits on the floor of the content rect and each
        later row stacks above it. Stacking downwards from the top would hang the
        earliest block off the ceiling of the waveform area instead, a row's worth
        of empty space underneath it, which reads as a gap between the block and
        the axis it belongs to.

        The bottom edge is the anchor and the top is derived from it, so the
        lowest row cannot drift away from the axis as rows are added or removed.
        Only an area too short to hold the stack pushes rows up, and then they
        are clipped rather than allowed to hang over the top of the widget.

        **The floor is a few pixels up**, by ``_TB_BOTTOM_MARGIN``: the stack
        stands clear of the axis strips rather than resting on them, so a block
        is a rectangle laid over the timeline and not a shape welded to it. The
        gap is surrendered before a row is -- an area too short for both gives
        the margin up and keeps its row, which is what makes the stunted case
        keep drawing.

        Painted and hit-tested through this one function: two copies of this
        arithmetic is two chances to disagree, and the symptom of that is a block
        you can see but cannot click.
        """
        margin = _TB_BOTTOM_MARGIN if area.height() >= _TB_ROW_HEIGHT + _TB_BOTTOM_MARGIN else 0
        bottom = area.bottom() - margin - row * _TB_ROW_HEIGHT
        if bottom < area.top():
            return None  # this row's bottom is already above the drawing area
        top = max(bottom - (_TB_ROW_HEIGHT - 2) + 1, area.top())
        return QRect(area.left(), top, area.width(), bottom - top + 1)

    def _time_block_span(self, start_us: int, end_us: int, area: QRect) -> tuple[int, int] | None:
        """The horizontal span of a block across ``area``, or ``None`` if it is out of view.

        ``[x0, x1]`` is the block in pixels, for both painting and hit testing:
        Qt's ``drawRect`` takes its second pair of coordinates as the bottom-right
        *corner*, so the rectangle's own outline lands on ``x1`` -- the pixels a
        block paints are ``x0 <= x <= x1``, and that is exactly the range the hit
        test answers for. Two answers are how a block ends up drawn on a pixel
        that belongs to the block beside it.

        **No in-view block is narrower than** ``_MIN_BLOCK_WIDTH``. At any zoom
        where an hour is a pixel, a block's true extent is a hairline -- too
        thin to see, too thin to hit, and at one pixel it is also ambiguous
        whether the block is there at all. Such a block is widened to the
        minimum, to the right where there is room and back to the left at the
        right edge, and the overlap with its neighbours is accepted: blocks that
        share time are already on separate rows, and the alternative is a block
        the interface silently stops offering at exactly the zoom you would
        search for it.

        A block at least one column wide already does not move -- widening only
        ever fills in what the column grid could not resolve.
        """
        if end_us < self._state.start_us or start_us > self._state.end_us:
            return None

        x0 = self.x_of(max(start_us, self._state.start_us))
        x1 = self.x_of(min(end_us, self._state.end_us))
        if x1 < x0:
            x0, x1 = x1, x0

        right = area.left() + area.width()  # one past the last column
        x1 = min(max(x1, x0 + _MIN_BLOCK_WIDTH), right)
        if x1 - x0 < _MIN_BLOCK_WIDTH:
            x0 = max(area.left(), x1 - _MIN_BLOCK_WIDTH)
        return x0, x1

    def _paint_time_blocks(self, painter: QPainter, area: QRect) -> None:
        """Paint time blocks as rectangles with names in the bottom area of content rect."""
        if not getattr(self, '_show_blocks', True):
            return

        metrics = painter.fontMetrics()

        for start_us, end_us, name, episode_num, row, block in self._state.time_blocks:
            span = self._time_block_span(start_us, end_us, area)
            if span is None:
                continue
            x0, x1 = span

            row_rect = self._time_block_row_rect(area, row)
            if row_rect is None:
                continue
            y0 = row_rect.top()

            # Check if this block is selected
            is_selected = (
                self._state.selected_time_block is not None
                and self._state.selected_time_block[0] == start_us
                and self._state.selected_time_block[1] == end_us
                and self._state.selected_time_block[2] == name
            )

            # Draw block rectangle using TimeBlock's color
            r, g, b, a = block.color
            edge_r, edge_g, edge_b, edge_a = block.edge_color

            if is_selected:
                # Highlight selected block with thicker border and glow
                painter.setPen(QPen(QColor(255, 255, 100, 255), 3))
                painter.setBrush(QBrush(QColor(r, g, b, a)))
            else:
                painter.setPen(QPen(QColor(edge_r, edge_g, edge_b, edge_a), 1))
                painter.setBrush(QBrush(QColor(r, g, b, a)))

            rect_width = max(1, x1 - x0)
            painter.drawRect(QRect(x0, y0, rect_width, row_rect.height()))

            # Draw name + episode number, but only where one can still say
            # something: a block at the minimum width has less than an ellipsis
            # to work with, and elided-to-nothing in three pixels is ink that
            # tells the reader nothing about which block it is.
            room = rect_width - 4
            if room <= 0:
                continue
            label = f"{name} (ep {episode_num})" if episode_num else name
            text_width = metrics.horizontalAdvance(label)
            if text_width > room:
                label = metrics.elidedText(label, Qt.ElideRight, room)
                text_width = metrics.horizontalAdvance(label)

            # Text color: black or white depending on background brightness
            brightness = (r * 299 + g * 587 + b * 114) / 1000
            text_color = QColor(0, 0, 0, 255) if brightness > 128 else QColor(255, 255, 255, 255)
            painter.setPen(QPen(text_color))
            text_x = x0 + (rect_width - text_width) // 2
            text_y = y0 + (row_rect.height() - metrics.height()) // 2 + metrics.ascent()
            painter.drawText(text_x, text_y, label)

    def _time_block_at(self, x: int, y: int) -> tuple[int, int, str, int] | None:
        """Return time block data at (x, y) if any, else None."""
        if not getattr(self, '_show_blocks', True):
            return None

        area = self._content_rect()
        for start_us, end_us, name, episode_num, row, _block in self._state.time_blocks:
            span = self._time_block_span(start_us, end_us, area)
            if span is None:
                continue
            x0, x1 = span

            row_rect = self._time_block_row_rect(area, row)
            if row_rect is None:
                continue

            if x0 <= x <= x1 and row_rect.top() <= y <= row_rect.bottom():
                return (start_us, end_us, name, episode_num)
        return None


def _true_runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """``[True, True, False, True]`` -> ``[(0, 2), (3, 4)]``."""
    if flags.size == 0:
        return []
    padded = np.concatenate(([False], flags.astype(bool), [False]))
    changes = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return list(zip(starts.tolist(), ends.tolist()))
