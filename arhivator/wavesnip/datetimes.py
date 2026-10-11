"""Parsing and formatting the ``D.M.Y. H:M:S:MS`` dates the app shows.

Format dictated by ``design.md``: ``12.5.2026. 10:15:01.001``, chosen so a date
can be read off screen and typed/pasted straight back in. Seconds alone parse
too -- milliseconds are what field shows, not what it requires.

**``QDateTimeEdit`` cannot be trusted to parse a pasted string.** It accepts
exactly one shape -- four-digit year, all fields present, ``.`` and ``:``
separators, nothing trailing. Everything else silently *rejected*, leaving
previous value in place with no error shown. Verified against Qt 6.11: ISO
``2026-05-12 10:15:00``, ``12.05.2026 10:15:00``, ``12.5.2026. 10:15``,
``12.5.26. 10:15:00`` and trailing ``x`` all leave field unchanged.

That silent revert is worst behaviour for paste-driven workflow: user pastes,
presses play, hears audio from completely different day with no indication
anything went wrong. So pasted text goes through :func:`parse_datetime` here,
which accepts documented format *and* common variants, and reports failure
explicitly rather than returning something plausible.
"""

from __future__ import annotations

import re
from datetime import datetime

__all__ = [
    "QT_DISPLAY_FORMAT",
    "format_datetime",
    "format_datetime_ms",
    "format_date_with_weekday",
    "parse_datetime",
    "describe_parse_failure",
]

#: Qt's format string for ``12.5.2026. 10:15:00.000``. ``d`` and ``M`` are
#: deliberately unpadded, matching the example in `design.md`, and ``.zzz`` is
#: there because :func:`format_datetime` writes milliseconds -- Qt 6.11 renders
#: ``d.M.yyyy. HH:mm:ss.zzz`` correctly, which is what lets the calendar popup
#: and the line edit agree on the same string for the same instant.
QT_DISPLAY_FORMAT = "d.M.yyyy. HH:mm:ss.zzz"

def _milliseconds(text: str) -> int:
    """``"5"`` -> 500000, ``"50"`` -> 500000, ``"500"`` -> 500000.

    Fraction is a *decimal* fraction of a second, so its length decides the
    scale. ``ljust`` would pad string but leave value wrong: ``"500"`` is
    already three chars and ``int("500")`` is 500, not 500000.
    """
    return int(text) * 10 ** (6 - len(text))


def _assemble_day_first(parts: tuple[str, ...]) -> datetime:
    """Build from day, month, year, hour, minute, second?, fraction?

    Shorter patterns supply fewer groups, so sequence padded to full width
    before unpacking -- otherwise unpacking itself raises and pattern looks
    like it didn't match.
    """
    day, month, year, hour, minute, second, fraction = (*parts, "0", "0", "0", "0", "0", "")[:7]
    return datetime(
        int(year), int(month), int(day),
        int(hour or 0), int(minute or 0), int(second or 0),
        _milliseconds(fraction) if fraction else 0,
    )


def _assemble_iso(parts: tuple[str, ...]) -> datetime:
    """Build from year, month, day, hour, minute, second?"""
    year, month, day, hour, minute, second = (*parts, "0", "0", "0", "0", "0", "0")[:6]
    return datetime(
        int(year), int(month), int(day),
        int(hour or 0), int(minute or 0), int(second or 0),
    )


#: (matcher, builder) pairs, most exact first. Each pattern fully anchored and
#: each builder knows its own group's count, so adding a variant cannot
#: silently shift meaning of an existing one.
_PATTERNS = (
    # 12.5.2026. 10:15:01:001  -- documented format with milliseconds (colon separator)
    (re.compile(
        r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s*(\d{1,2}):(\d{1,2}):(\d{2}):(\d{1,3})\s*"),
     _assemble_day_first),
    # 12.5.2026. 10:15:00  -- documented format (seconds only)
    (re.compile(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s*(\d{1,2}):(\d{1,2}):(\d{2})\s*"),
     _assemble_day_first),
    # 12.5.2026. 10:15:00.500  -- with decimal fraction of a second
    (re.compile(
        r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s*(\d{1,2}):(\d{1,2}):(\d{2})"
        r"[.,](\d{1,3})\s*"),
     _assemble_day_first),
    # 12.5.2026. 10:15  -- seconds omitted
    (re.compile(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s*(\d{1,2}):(\d{1,2})\s*"),
     _assemble_day_first),
    # 12.5.2026 10:15:00  -- dot before time omitted
    (re.compile(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{1,2}):(\d{2})\s*"),
     _assemble_day_first),
    # 12.5.2026. 10  -- hour only, minute and seconds omitted
    (re.compile(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\.\s*(\d{1,2})\s*"),
     _assemble_day_first),
    # 2026-05-12 10:15:00  -- ISO, what most other tools emit
    (re.compile(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2})(?::(\d{2}))?\s*"),
     _assemble_iso),
)


def format_datetime(moment: datetime) -> str:
    """``12.5.2026. 10:15:00.000`` -- `design.md`'s shape, plus milliseconds.

    Day and month **not** zero-padded, because that's what design's example
    shows and what Qt's ``d.M.yyyy. HH:mm:ss.zzz`` renders. Matching Qt is the
    point: string on screen has to be string that gets copied, and anything
    else means app and its own clipboard disagree.

    Milliseconds are `design.md`'s ``D.M.Y. H:M:S:MS``. Without them display
    would round to second while :class:`~wavesnip.datefield.DateField` keeps
    exact microseconds internally -- so sub-second drag would show time it
    wasn't. Separator is **dot** not design's colon because Qt can only render
    ``zzz`` after a dot, and honest seconds-precision display worse than
    agreeing with widget.

    ``strftime`` cannot express this -- ``%-d`` is glibc extension not on
    Windows, and launchers support Windows -- so string assembled by hand.
    """
    return (
        f"{moment.day}.{moment.month}.{moment.year:04d}. "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{moment.microsecond // 1000:03d}"
    )


def format_datetime_ms(moment: datetime) -> str:
    """``12.5.2026. 10:15:01:001`` -- format with milliseconds using colon separator.

    Used for time blocks per design.md. Day and month not zero-padded.
    """
    return (
        f"{moment.day}.{moment.month}.{moment.year:04d}. "
        f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}:{moment.microsecond // 1000:03d}"
    )


def format_date_with_weekday(moment: datetime) -> str:
    """``5.3.2025. Wednesday`` -- date with day of week, unpadded.

    Matches lower strip format from `design.md`: "D.M.Y. DAY_IN_WEEK".
    Day and month not zero-padded. Weekday is full English name.
    """
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return f"{moment.day}.{moment.month}.{moment.year:04d}. {weekdays[moment.weekday()]}"


def parse_datetime(text: str) -> datetime | None:
    """Parse a pasted datetime, or return None.

    None means "I do not understand this", never "here is a guess". Caller
    expected to keep user's text visible and say so, rather than quietly
    substituting a different date.
    """
    if not text or not text.strip():
        return None

    for pattern, build in _PATTERNS:
        match = pattern.fullmatch(text)
        if match is None:
            continue
        try:
            return build(match.groups())
        except ValueError:
            # Well-shaped but impossible: 32.1.2026, 10:75, 30.2.2026.
            return None

    return None


def describe_parse_failure(text: str) -> str:
    """A message a user can act on, for a string we could not parse."""
    stripped = text.strip()
    if not stripped:
        return "Enter a date and time."
    return (
        f"Could not read {stripped!r}. Expected something like "
        f"12.5.2026. 10:15:00."
    )