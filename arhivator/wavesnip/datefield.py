"""A text field that accepts a pasted datetime, plus an optional calendar.

``QDateTimeEdit`` is the obvious widget for this and it cannot do the job.
Typed character by character it rejects almost everything: with display format
``d.M.yyyy. HH:mm:ss.zzz``, typing ``12.5.2026. 10:15:00.000`` character by
character leaves ``11.5.2000. 20:26:00.101`` (re-run under Qt 6.11) -- it
interprets each keystroke against current value rather than accumulating
text. Pasting and calling ``setText`` is no better -- widget overwrites line
edit with its own formatted value and never emits ``editingFinished``, so text
never understood disappears without a word.

Since ``design.md`` requires a date to be copied off screen and pasted back in,
text has to be under this module's control. So it is: a ``QLineEdit``, parsed
by :func:`wavesnip.datetimes.parse_datetime`, with calendar popup attached for
anyone who'd rather click.

**Rejected text is restored, not left on screen.** Field still showing what
user typed after error looks like it was accepted, and playhead would quietly
disagree with what's displayed. Restoring it and saying why is the only honest
outcome.
"""

from __future__ import annotations

from PySide6.QtCore import QDateTime, Signal
from PySide6.QtWidgets import (
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from wavesnip.datetimes import (
    QT_DISPLAY_FORMAT,
    describe_parse_failure,
    format_datetime,
    parse_datetime,
)
from wavesnip.model import from_us, to_us
from datetime import datetime

__all__ = ["DateField"]

#: Placeholder is formatter's own output, so it cannot drift from what field
#: will actually write -- example disagreeing with widget is worse than no
#: example, because it teaches wrong thing to type.
_EXAMPLE = format_datetime(datetime(2026, 5, 12, 10, 15))


class DateField(QWidget):
    """Free-text datetime entry, with a calendar popup attached.

    Signals:

    * ``changed`` -- text parsed; carries instant in microseconds.
    * ``invalid`` -- text did not parse; carries message and raw text.
    """

    changed = Signal(object)
    invalid = Signal(str, str)

    def __init__(self, parent: QWidget | None = None, *, allow_clear: bool = True):
        super().__init__(parent)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(_EXAMPLE)
        self.edit.setToolTip(
            f"Type or paste a date as {_EXAMPLE}.\n"
            "ISO (2026-05-12 10:15:00) is accepted too.\n"
            "Seconds may be omitted; the year must have four digits."
        )
        self.edit.setClearButtonEnabled(allow_clear)

        self.button = QPushButton("…")
        self.button.setToolTip("Pick a date from a calendar")
        self.button.setFixedWidth(28)

        row = QHBoxLayout()
        row.setSpacing(0)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button)
        outer = QHBoxLayout(self)
        outer.setSpacing(0)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(row)

        self.edit.returnPressed.connect(self.commit)
        self.edit.editingFinished.connect(self.commit)
        self.button.clicked.connect(self._open_calendar)

        #: Set while field is being filled in by code, so commit handler
        #: does not treat program's own write as a user edit.
        self._programmatic = False

        #: The instant this field holds, and the exact text last written for it.
        #: See :meth:`instant` for why text alone is not enough.
        self._value: int | None = None
        self._written = ""

    # -- values ----------------------------------------------------------

    def text(self) -> str:
        return self.edit.text()

    def instant(self) -> int | None:
        """Current value in microseconds, or None if text unusable.

        Not always a re-parse of text, because text cannot hold what field is
        for. ``D.M.Y. H:M:S`` has one-second resolution and ``design.md`` fixes
        it -- whole point of format is that date can be copied off screen and
        pasted back in -- but a drag across waveform routinely lands on range
        shorter than a second, and range whose two ends print identically is a
        range user cannot see or adjust.

        So field holds an instant, and text is one rendering of it. While text
        is still the one this field wrote, held instant is authoritative and
        second-resolution text only a display of it. Moment text differs, text
        is authoritative again -- that's what makes typing and pasting work.
        """
        text = self.edit.text()
        if self._value is not None and text == self._written:
            return self._value
        parsed = parse_datetime(text)
        return None if parsed is None else to_us(parsed)

    def set_instant(self, micros: int) -> None:
        """Show an instant without emitting ``changed``.

        Every programmatic write goes through here. Without guard, moving the
        playhead would write a date into the field, which would fire ``changed``,
        which would move the playhead again.
        """
        self._programmatic = True
        try:
            shown = format_datetime(from_us(micros))
            self.edit.setText(shown)
            self._value = micros
            self._written = shown
        finally:
            self._programmatic = False

    def restore(self, micros: int) -> None:
        """Put a rejected entry back without emitting ``changed``.

        Counterpart of ``invalid``: field left showing last good value rather
        than text refused. Separate name from :meth:`set_instant` because call
        site is distinct operation, not generic write -- refusal reported
        elsewhere (status bar).
        """
        self.set_instant(micros)

    def clear(self) -> None:
        self._programmatic = True
        try:
            self.edit.clear()
            self._value = None
            self._written = ""
        finally:
            self._programmatic = False

    # -- committing ------------------------------------------------------

    def commit(self) -> bool:
        """Parse the text and emit. True if it parsed."""
        if self._programmatic:
            return False

        text = self.edit.text()
        parsed = parse_datetime(text)
        if parsed is None:
            message = describe_parse_failure(text)
            self.invalid.emit(message, text)
            return False

        moment = to_us(parsed)
        # Text is now the authority: may have been typed or pasted, and is
        # what user can see. Recording that it was *this* text lets `instant`
        # hand back parsed value rather than stale one.
        self._value = moment
        self._written = text
        self.changed.emit(moment)
        return True

    # -- calendar --------------------------------------------------------

    def _open_calendar(self) -> None:
        """Offer a calendar for picking, without requiring it.

        Picked value written into text field, so from then on field behaves
        exactly as if user had typed it.
        """
        dialog = _CalendarDialog(self.instant(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        picked = dialog.value()
        if picked is None:
            return
        self._programmatic = True
        try:
            self.edit.setText(format_datetime(from_us(picked)))
        finally:
            self._programmatic = False
        self.changed.emit(picked)


class _CalendarDialog(QDialog):
    """A date/time picker. The only place ``QDateTimeEdit`` is used.

    Poking at a widget by clicking sections is perfectly good; doing it by
    typing is not, and this one is only ever driven by clicks.
    """

    def __init__(self, micros: int | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Pick date and time")

        self.editor = QDateTimeEdit()
        self.editor.setDisplayFormat(QT_DISPLAY_FORMAT)
        self.editor.setCalendarPopup(True)
        self.editor.setDateTime(
            QDateTime(from_us(micros)) if micros is not None else QDateTime.currentDateTime()
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.editor)
        layout.addWidget(buttons)

    def value(self) -> int | None:
        moment = self.editor.dateTime()
        if not moment.isValid():
            return None
        return to_us(moment.toPython())