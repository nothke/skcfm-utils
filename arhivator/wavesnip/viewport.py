"""Zoom, pan and axis-tick arithmetic for the global timeline.

Deliberately pure and Qt-free. Waveform view is now a *window onto the whole
archive* not a fixed window around the playhead, so every zoom level, pan and
axis label is a decision about a range of microseconds. All those decisions
live here so they can be tested without a widget, and ``WaveView`` is left
doing nothing but turning numbers into pixels.

Three rules shape everything here:

**Ticks are on an absolute grid.** A tick at 10:00:00 stays at 10:00:00 while
the user pans. Ticks at "every N pixels from the left edge" would slide
continuously, and a date axis whose labels crawl is worse than no axis.

**Zoom anchor does not move.** Zooming about the pointer keeps the instant
under it under it. Zooming about the *centre* of the widget feels wrong for
anything but symmetric navigation, because the thing you aimed at slides away
underneath the cursor.

**Nothing is ever clamped to a Qt integer.** A 2026 timestamp is ~1.8e15 and
``QScrollBar``/``QRect`` take 32-bit ints, so the mapping onto a scrollbar is
done in scaled steps rather than microseconds. See :data:`SCROLL_STEPS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from wavesnip.datetimes import format_date_with_weekday
from wavesnip.model import from_us, to_us

__all__ = [
    "Limits",
    "SCROLL_STEPS",
    "MIN_SPAN_US",
    "MAX_WAVE_SPAN_US",
    "choose_ticks",
    "choose_day_ticks",
    "clamp_view",
    "fit_view",
    "pan_by",
    "pan_to",
    "scroll_from_scrollbar",
    "scroll_to_scrollbar",
    "zoom_about",
]

SEC = 1_000_000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR

#: How close the view may be zoomed: one second across the whole widget. Below
#: that a pixel column is a fraction of a sample and the drawing lies.
MIN_SPAN_US = SEC

#: Widest view that shows a waveform; wider than this draws coverage instead.
#:
#: Used to be a frame budget -- "can this window be decoded in 200M frames" --
#: which meant different seconds at different sample rates. Envelope is now
#: *sampled* (see :mod:`wavesnip.peaks`), so cost no longer grows with width,
#: and the number can be a statement about the view. Past a week the outline
#: has nothing to say that coverage blocks don't say better, and design.md asks
#: for that split -- zoomed out is coverage, zoomed in is waveform. Also keeps
#: plan-building for very wide window bounded, which matters because gate is
#: checked before any plan is built.
MAX_WAVE_SPAN_US = 7 * DAY

#: Steps in the scrollbar's range.
#:
#: Scrollbar's *natural* range is archive span in microseconds (~5e14 for a
#: year) -- far past 32-bit int a ``QScrollBar`` takes. So scrollbar counts
#: steps instead, step size derived per view.
#:
#: 2**22 rather than smaller because it sets how coarsely scrollbar can
#: position view: at 1-day window over a year, each step ~7.5 s. At widest
#: zoom range is empty; when zoomed in far enough for 7.5 s to matter a step
#: is under a pixel, so only cost is slightly sticky scrollbar on wide view
#: -- where shift-drag is better anyway.
SCROLL_STEPS = 1 << 22


@dataclass(frozen=True)
class Limits:
    """How far the view is allowed to move and zoom.

    ``start_us``/``end_us`` are archive's extent from merged coverage so a gap
    at either end still shows as a gap not cropped away.
    """

    start_us: int
    end_us: int
    min_span_us: int = MIN_SPAN_US
    #: Slack left on each side when fully zoomed out, so first and last clip
    #: not flush against widget edge.
    padding_us: int = HOUR

    @classmethod
    def for_extent(cls, start_us: int, end_us: int) -> "Limits":
        """Limits for an archive of this size, with a sensible margin.

        Padding is two percent of span, capped at an hour. Fixed padding would
        be either invisible on year-long archive (one hour = one pixel in
        twelve hundred) or absurd on short one (hour either side = 300x audio,
        pushes clips into middle). Two percent is margin on both, cap stops it
        growing into dead space on long archive.

        Archive shorter than :data:`MIN_SPAN_US` gets extra padding anyway.
        Zooming in that far not allowed -- column narrower than a sample makes
        drawing lie -- so view held at a second wide and hangs over ends.
        Without extra padding that would put view outside its own limits, which
        makes every later clamp and scroll calculation quietly wrong.
        """
        span = max(1, end_us - start_us)
        padding = min(span // 50, HOUR)
        short = cls.min_span_us - (span + 2 * padding)
        if short > 0:
            padding += short // 2 + 1
        return cls(start_us=start_us, end_us=end_us, padding_us=padding)

    @property
    def span_us(self) -> int:
        return max(1, self.end_us - self.start_us)

    @property
    def padded(self) -> tuple[int, int]:
        return self.start_us - self.padding_us, self.end_us + self.padding_us

    @property
    def padded_span(self) -> int:
        """How wide the widest legal view is."""
        low, high = self.padded
        return max(1, high - low)


def fit_view(limits: Limits) -> tuple[int, int]:
    """The widest view: everything, with a little room at each end."""
    return limits.padded


def clamp_view(start_us: int, end_us: int, limits: Limits) -> tuple[int, int]:
    """Force a range to be legal: right way up, right size, on the archive.

    Span preserved wherever possible -- zooming or panning must not silently
    change the scale user just chose. Only when span itself illegal is it
    corrected, and only position then adjusted.

    Window kept *inside* padded extent rather than allowed to hang off it.
    Half-empty view never what anyone wants, and letting it happen means drawn
    coverage silently changes meaning near edges.
    """
    low, high = limits.padded
    full = limits.padded_span

    # Backwards range repaired by swapping, not abs(). Span is window size;
    # `abs` would paper over caller passing ends wrong way round and leave it
    # passing them that way.
    if end_us < start_us:
        start_us, end_us = end_us, start_us

    span = end_us - start_us
    span = max(limits.min_span_us, min(span, full))
    start_us = max(low, min(start_us, high - span))
    return start_us, start_us + span


def zoom_about(
    start_us: int, end_us: int, anchor_us: int, factor: float, limits: Limits
) -> tuple[int, int]:
    """Zoom by ``factor`` keeping instant at ``anchor_us`` fixed.

    ``factor`` < 1 zooms in, > 1 zooms out. Anchor clamped into current range
    first: wheel event can report position just outside widget, and anchor
    outside range would slide view wrong way.
    """
    if factor <= 0:
        raise ValueError("factor must be positive")

    start_us, end_us = clamp_view(start_us, end_us, limits)
    anchor_us = max(start_us, min(anchor_us, end_us))

    span = end_us - start_us
    offset = anchor_us - start_us
    # Capped at *padded* extent, not archive itself, so zooming out really
    # does reach fit_view. Capping at bare span stops one padding width short,
    # leaving widest view unreachable by wheel.
    low, high = limits.padded
    new_span = max(limits.min_span_us, min(int(span * factor), max(1, high - low)))

    # The anchor's *fraction* across the view is what has to be preserved.
    new_start = anchor_us - (offset * new_span) // span
    return clamp_view(new_start, new_start + new_span, limits)


def pan_by(start_us: int, end_us: int, delta_us: int, limits: Limits) -> tuple[int, int]:
    """Shift the view by a time delta, keeping the span."""
    start_us, end_us = clamp_view(start_us, end_us, limits)
    span = end_us - start_us
    return clamp_view(start_us + delta_us, start_us + delta_us + span, limits)


def pan_to(start_us: int, end_us: int, centre_us: int, limits: Limits) -> tuple[int, int]:
    """Move the view so ``centre_us`` sits in the middle, keeping the span.

    Used to follow playback: the playhead drifts off screen as the audio plays,
    and a playhead outside the view is a playhead the user cannot see.
    """
    start_us, end_us = clamp_view(start_us, end_us, limits)
    span = end_us - start_us
    return clamp_view(centre_us - span // 2, centre_us - span // 2 + span, limits)


def scroll_to_scrollbar(
    start_us: int, end_us: int, limits: Limits
) -> tuple[int, int]:
    """Map a view onto ``(minimum, maximum, value)`` for a ``QScrollBar``.

    The scrollbar represents the *left edge* only, so the maximum is however far
    the left edge can travel. At the widest zoom that is zero and the bar is
    correctly disabled by its caller.
    """
    low, high = limits.padded
    travel = max(0, (high - low) - (end_us - start_us))
    if travel == 0:
        return 0, 0, 0

    fraction = (start_us - low) / travel
    value = round(fraction * SCROLL_STEPS)
    return 0, SCROLL_STEPS, max(0, min(SCROLL_STEPS, value))


def scroll_from_scrollbar(value: int, span_us: int, limits: Limits) -> tuple[int, int]:
    """The inverse of :func:`scroll_to_scrollbar`, for a scrollbar that moved.

    Takes the *span* rather than a window, deliberately. Given a window it would
    have to recompute the span, and a caller that passed a window of a different
    width than the one the value came from would get a plausible-looking but wrong
    position -- the mismatch is invisible at the call site and impossible to test
    for by reading the signature.
    """
    low, high = limits.padded
    travel = max(0, (high - low) - span_us)
    if travel == 0:
        return clamp_view(low, low + span_us, limits)

    position = low + round((max(0, min(SCROLL_STEPS, value)) / SCROLL_STEPS) * travel)
    return clamp_view(position, position + span_us, limits)


# -- axis ticks ---------------------------------------------------------

# Label kinds, in increasing coarseness. The *kind* is chosen from the visible
# span rather than from the tick interval, because what a label has to say
# depends on how much time is on screen -- not on how far apart the ticks ended
# up. A three-day view wants the year even when its ticks land every six hours,
# since the archive may span years and `12.05. 14:00` alone is ambiguous.
TIME_SECONDS = "time_seconds"
TIME = "time"
DATETIME = "datetime"
DATE = "date"
MONTH = "month"
YEAR = "year"

_FORMATS = {
    TIME_SECONDS: "%H:%M:%S",
    TIME: "%H:%M",
    DATETIME: "%d.%m.%Y. %H:%M",
    DATE: "%d.%m.%Y.",
    MONTH: "%B %Y",
    YEAR: "%Y",
}

#: Tick intervals in microseconds, smallest first. The kind is attached so a step
#: is never paired with a format that cannot express it -- a year tick labelled
#: with seconds, say.
_FIXED_STEPS: tuple[tuple[int, str], ...] = (
    (SEC, TIME_SECONDS),
    (5 * SEC, TIME_SECONDS),
    (15 * SEC, TIME_SECONDS),
    (30 * SEC, TIME_SECONDS),
    (MIN, TIME),
    (5 * MIN, TIME),
    (15 * MIN, TIME),
    (30 * MIN, TIME),
    (HOUR, DATETIME),
    (3 * HOUR, DATETIME),
    (6 * HOUR, DATETIME),
    (12 * HOUR, DATETIME),
    (DAY, DATE),
    (2 * DAY, DATE),
    (7 * DAY, DATE),
    (14 * DAY, DATE),
    (31 * DAY, MONTH),
    (93 * DAY, MONTH),
    (186 * DAY, MONTH),
    (372 * DAY, YEAR),
)

#: Never draw ticks closer together than this, whatever the span works out to.
_MIN_TICK_PX = 96


def choose_ticks(
    start_us: int, end_us: int, width_px: int, min_px: int = _MIN_TICK_PX
) -> list[tuple[int, str]]:
    """Axis ticks as ``(instant_us, label)``, on an absolute grid.

    Ticks are placed at whole multiples of the chosen interval in *absolute* time,
    so panning slides the waveform under a fixed grid instead of dragging the
    labels with it. Days and coarser go through the calendar, because "every 31
    days" is not a month and the label would be quietly wrong.

    **Days go through the calendar too**, for the same reason at a coarser
    grain: a whole multiple of a day counted from the epoch is *UTC* midnight,
    which in any other timezone is some other hour -- on this box the day ticks
    of the upper strip landed at 02:00 and an hour out again after a DST change,
    while the day strip and the Sunday wash below them marked the real midnight.
    The label said ``12.05.2026.`` and the tick sat mid-morning. See
    :func:`_day_ticks`.
    """
    if width_px <= 0 or end_us <= start_us:
        return []

    span = end_us - start_us
    wanted = max(1, width_px // max(1, min_px))

    step_us, kind = _choose_step(span, wanted)
    kind = _refine_kind(span, kind, start_us, end_us)

    # March from the first tick at or after the left edge. Bounded so a wildly
    # small step cannot spin here.
    limit = wanted * 4 + 8
    if kind in (MONTH, YEAR):
        return _calendar_ticks(start_us, end_us, step_us, kind, limit)
    if kind == DATE:
        # Every DATE tick claims to name a *day*, so every DATE tick has to sit
        # on a day boundary. A sub-day step refined to a date label (a wide view
        # wants the year, not 14:00) becomes one tick per day here rather than
        # six ticks all claiming to be the same day.
        return _day_ticks(start_us, end_us, step_us, limit)

    fmt = _FORMATS[kind]
    ticks: list[tuple[int, str]] = []
    instant = _ceil_to(start_us, step_us)
    while len(ticks) < limit:
        if instant >= end_us:
            break
        ticks.append((instant, from_us(instant).strftime(fmt)))
        instant += step_us
    return ticks


def _day_ticks(
    start_us: int, end_us: int, step_us: int, limit: int
) -> list[tuple[int, str]]:
    """Ticks at local midnights, ``step_us`` days apart, for DATE labels.

    Two things a day axis has to get right, and the absolute grid got neither:

    - **Midnight.** ``step_us`` counted from the epoch is UTC midnight, so the
      day tick of the upper strip landed at 02:00 local (01:00 in winter) while
      the day strip and the Sunday wash -- both calendar walks -- marked the
      real one. The date label was honest about the day and dishonest about
      where the day started.

    - **A grid the view does not own.** The anchor is fixed at 1970-01-01 in
      the local calendar, so panning slides the waveform under ticks that hold
      still, exactly like every other strip. Anchoring at the view's own left
      edge would drag the labels along with the waveform.

    The walk is in calendar days, not ``step_us``, so a DST change costs an
    hour of *sleep* and not a tick: midnight plus one day is the next midnight
    whether the clocks went forward or not.
    """
    days = max(1, -(-step_us // DAY))
    anchor = date(1970, 1, 1)
    first_day = (from_us(start_us).date() - anchor).days
    # First grid day whose midnight can be inside the view; its own midnight
    # may still fall before the start, in which case the loop skips it.
    cursor = anchor + timedelta(days=-((-first_day) // days) * days)
    fmt = _FORMATS[DATE]

    ticks: list[tuple[int, str]] = []
    while len(ticks) < limit:
        instant = to_us(datetime(cursor.year, cursor.month, cursor.day))
        if instant >= end_us:
            break
        if instant >= start_us:
            ticks.append((instant, cursor.strftime(fmt)))
        cursor += timedelta(days=days)
    return ticks


def choose_day_ticks(
    start_us: int, end_us: int, width_px: int, min_px: int = _MIN_TICK_PX
) -> list[tuple[int, str]]:
    """Day ticks as ``(instant_us, label)`` with day-of-week, on an absolute grid.

    The day strip exists only when the view is zoomed in to a week or less. At
    any wider zoom every day is too fine to label, so the strip shows
    *nothing* -- a wall of overlapping dates is noise, and months are the
    upper strip's job. ``design.md``: show the day axis only when up to seven
    days are visible.

    Where it does show, it places a label per day (e.g. "5.3.2025.
    Wednesday") at day boundaries in the local calendar so the date never
    shifts, and always includes the day containing the view start even when
    its midnight falls before it.
    """
    if width_px <= 0 or end_us <= start_us:
        return []
    if end_us - start_us > 7 * DAY:
        return []

    step_us = DAY  # Always daily for the day strip
    limit = max(1, width_px // max(1, min_px)) * 4 + 8
    return _calendar_day_ticks(start_us, end_us, step_us, limit)


def _calendar_day_ticks(
    start_us: int, end_us: int, step_us: int, limit: int
) -> list[tuple[int, str]]:
    """Ticks at calendar day boundaries with day-of-week labels.

    Step is always one day -- every day in the view gets a label.
    """
    start = from_us(start_us)
    # Start at the day boundary at or BEFORE start_us, so the day containing
    # the view start is always represented (even if its midnight is outside).
    cursor = datetime(start.year, start.month, start.day)

    ticks: list[tuple[int, str]] = []
    while len(ticks) < limit:
        instant = to_us(cursor)
        if instant >= end_us:
            break
        ticks.append((instant, format_date_with_weekday(cursor)))
        cursor += timedelta(days=1)
    return ticks


def _refine_kind(span: int, kind: str, start_us: int, end_us: int) -> str:
    """Add the date to a time-only label when a time alone is ambiguous.

    Two cases, both real on a long archive: a sub-day window that crosses
    midnight, and anything a day or wider where the year is what tells two
    otherwise identical `12.05. 14:00` labels apart.
    """
    if span >= 2 * DAY:
        return DATE if kind in (TIME, TIME_SECONDS, DATETIME) else kind
    if kind in (TIME, TIME_SECONDS) and from_us(start_us).date() != from_us(end_us).date():
        return DATETIME
    return kind


def _choose_step(span: int, wanted: int) -> tuple[int | None, str]:
    """The coarsest ladder step that still gives roughly ``wanted`` ticks."""
    target = max(1, span // wanted)
    # For wider spans, skip DATE steps and prefer MONTH/YEAR
    if span > 60 * DAY:
        for step_us, kind in _FIXED_STEPS:
            if step_us >= target and kind in (MONTH, YEAR):
                return step_us, kind
    for step_us, kind in _FIXED_STEPS:
        if step_us >= target:
            return step_us, kind
    return _FIXED_STEPS[-1]


def _ceil_to(value: int, step: int) -> int:
    """Smallest multiple of ``step`` at or after ``value``.

    Integer arithmetic on purpose: ``int(value // step) * step`` is exact for
    ``value`` around 1.8e15 and any step a day or more, but rounding a float
    division of two large ints is not, and an axis that is off by a step at some
    dates but not others is very hard to see and impossible to reproduce.
    """
    return -((-value) // step) * step


def _calendar_ticks(
    start_us: int, end_us: int, step_us: int, kind: str, limit: int
) -> list[tuple[int, str]]:
    """Ticks at calendar boundaries, for day-and-coarser intervals."""
    months = 1 if step_us < 93 * DAY else (3 if step_us < 186 * DAY else 6)
    yearly = kind == YEAR
    fmt = _FORMATS[kind]

    start = from_us(start_us)
    if yearly:
        cursor = datetime(start.year, 1, 1)
        while to_us(cursor) < start_us:
            cursor = datetime(cursor.year + 1, 1, 1)
    else:
        total = start.year * 12 + (start.month - 1)
        total -= total % months
        cursor = datetime(total // 12, total % 12 + 1, 1)
        while to_us(cursor) < start_us:
            total += months
            cursor = datetime(total // 12, total % 12 + 1, 1)

    ticks: list[tuple[int, str]] = []
    while len(ticks) < limit:
        instant = to_us(cursor)
        if instant >= end_us:
            break
        ticks.append((instant, cursor.strftime(fmt)))
        cursor = _add_months(cursor, 12 if yearly else months)
    return ticks


def _add_months(moment: datetime, months: int) -> datetime:
    """Add whole months, clamping the day into the target month.

    ``timedelta`` cannot do this, and letting day 31 roll into the next month
    would drift a yearly axis by a day every year.
    """
    total = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = moment.day
    while True:
        try:
            return datetime(year, month, day)
        except ValueError:
            day -= 1
            if day < 1:  # pragma: no cover - only reachable for a 31st month
                raise
