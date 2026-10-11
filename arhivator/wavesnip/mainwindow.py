"""The application window: the archive as a timeline, and what you do to it.

Wires the pieces built so far to each other and owns everything Qt-specific
about how the user drives them. Five decisions worth stating up front.

**The waveform is the whole archive, and it starts that way.** View opens
showing every clip rather than a window around the playhead, so gaps and
recording shape are visible before anything is played. Cost: a year of audio
cannot be drawn as waveform -- one pixel = hours wide -- so view falls back to
coverage map and says so. Which is on screen is a question with a budget behind
it, not a preference.

**Click, drag, wheel, shift-drag.** Click puts cursor, drag selects snip, wheel
zooms about pointer, shift-drag pans. Last two had to be given something:
left-drag is what the app exists to do.

**Cursor cannot be placed to the second by clicking.** At widest zoom a pixel
spans hours, so a click lands hours from where it looked. Rather than pretend,
exact instant always appears in date field, the precise way in and the one
``design.md`` asks for.

**Date field is a text field, not a ``QDateTimeEdit``.** That widget cannot be
typed into -- entering ``12.5.2026. 10:15:00`` key by key produces
``11.1.2000. 00:00:00`` -- and it silently overwrites pasted text it doesn't
understand. Since ``design.md`` requires a date to be copied off screen and
pasted back in, text belongs to :class:`~wavesnip.datefield.DateField`, which
parses with :func:`wavesnip.datetimes.parse_datetime` and restores last good
value if it cannot.

**Archives live in a config file and are edited in a dialog.** They are setup,
needed once and never again, and a permanent panel for them steals width from
what you actually work with. :class:`~wavesnip.config.Config` remembers them
between runs so they don't have to be typed again either.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollBar,
    QSpinBox,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wavesnip.config import Config, ViewConfig
from wavesnip.datefield import DateField
from wavesnip.datetimes import format_datetime
from wavesnip.filepeaks import FilePeakCache
from wavesnip.indexer import ArchiveIndex, count_overlaps
from wavesnip.model import Archive, TimeBlock, TimeBlocksConfig, from_us, us_now, to_us
from wavesnip.options import OptionsDialog
from wavesnip.peaks import Peaks, peaks_for
from wavesnip.player import Player, PlayerError
from wavesnip.resample import AudioFormat, ExportError, render_snip
from wavesnip.reveal import reveal
from wavesnip.timeline import Timeline
from wavesnip.viewport import Limits, MAX_WAVE_SPAN_US, scroll_from_scrollbar, scroll_to_scrollbar
from wavesnip.waveview import WaveView, WaveViewState
from wavesnip.wavio import BextChunk
from wavesnip.workers import BackgroundJob

__all__ = ["MainWindow"]

SEC = 1_000_000
MIN = 60 * SEC
HOUR = 60 * MIN
DAY = 24 * HOUR
WEEK = 7 * DAY


class MainWindow(QMainWindow):
    """The one window."""

    def __init__(self, parent: QWidget | None = None, config: Config | None = None):
        super().__init__(parent)
        self.setWindowTitle("Arhivator")

        self._archives: list[Archive] = []
        self._timeline: Timeline | None = None
        self._playhead_us: int | None = None
        self._snip: tuple[int, int] | None = None
        self._peak_job: BackgroundJob | None = None
        self._peak_range: tuple[int, int] | None = None
        self._scan_job: BackgroundJob | None = None
        self._export_job: BackgroundJob | None = None
        self._export_target: Path | None = None

        #: Jobs in flight. A counter rather than a poll of each job's `busy`,
        #: because `done` arrives before the job clears its thread and asking a
        #: still-winding-down job whether it is busy always says yes.
        self._busy = 0

        #: Whether the peaks ("making waveforms") worker has a decode in flight.
        #: Separate from ``_busy`` because a decode must not disable the export
        #: buttons -- it is frequent, short, and re-runs on every view change,
        #: so blocking the controls for it would make the app feel wedged.
        self._peaks_busy = False

        #: Per-file envelope cache shared with the peaks worker: a range read
        #: once is never read again for waveform drawing this session, which is
        #: what makes a spinning disk tolerable. Keyed by file identity, so a
        #: rescan that changes a file simply stops matching its stale entry.
        self._file_peaks = FilePeakCache()

        #: Guards the scrollbar/view two-way sync. The mapping is lossy, so
        #: without this the two can nudge each other forever.
        self._syncing_scroll = False

        self.player = Player(self)

        # Built before the UI, because the UI refers to them.
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.setVisible(False)
        self.progress.setFormat("Making waveform…")

        self._config = config if config is not None else Config()
        self._view_config = ViewConfig()
        self._view_settings = self._view_config.load()
        # Create TimeBlocksConfig with custom path if set
        custom_tb_path = self._view_settings.get("time_blocks_path", "")
        self._default_tb_path = TimeBlocksConfig().path
        if custom_tb_path:
            self._tb_config = TimeBlocksConfig(Path(custom_tb_path))
        else:
            self._tb_config = TimeBlocksConfig()
        self._time_blocks: list[TimeBlock] = []
        self._editing_block: TimeBlock | None = None
        self._status_messages: list[str] = []

        self._build_ui()
        self._wire()
        self._load_time_blocks()
        self._apply_view_settings()
        self._on_snip_cleared()
        self._update_archive_summary()
        self._push_status(
            "No archive folders yet. Open Options… to add one."
            if not self._archives
            else "Drag across the waveform to select a range to snip."
        )

        # Maximize after UI is built and window is shown
        QTimer.singleShot(100, lambda: self.setWindowState(Qt.WindowMaximized))

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)

        self.wave = WaveView()
        self.wave.setToolTip(
            "Click to set the cursor.\n"
            "Drag to select a range to snip.\n"
            "Scroll to zoom, Shift+drag to pan."
        )
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.setEnabled(False)

        outer.addWidget(self._build_top_row())
        outer.addWidget(self.wave, 1)
        outer.addWidget(self.scrollbar)
        outer.addWidget(self._build_control_box())
        self.setCentralWidget(central)

    def _build_top_row(self) -> QWidget:
        """The single line of status and controls at the top of the window.

        Layout per `design.md`: status block (clips counted, click precision,
        "making waveforms" spinner), a gap, play/stop toggle and cursor date
        field, a gap, zoom (+, -, fit all) and Setup.

        Rescan button gone: F5 and File → Rescan archives reach same scan, and
        Setup dialog rescans when changes applied.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 4, 0)

        # STATUS: what is in timeline, how precise a click is, whether
        # waveform is being decoded.
        self.archive_summary = QLabel("")
        self.archive_summary.setMinimumWidth(120)
        layout.addWidget(self.archive_summary)
        layout.addSpacing(8)
        self.clip_label = QLabel("")
        self.clip_label.setMinimumWidth(140)
        layout.addWidget(self.clip_label)
        layout.addSpacing(8)
        layout.addWidget(self.progress)

        layout.addStretch(1)  # design's gap before transport

        self.btn_play = QPushButton("Play")
        self.btn_play.setToolTip("Play from the cursor; click again to stop.")
        layout.addWidget(self.btn_play)


        self.play_when = DateField()
        self.play_when.setToolTip(
            "Type or paste a date as D.M.Y. H:M:S, e.g. 12.5.2026. 10:15:00. "
            "Waveform's click only as precise as one pixel, which at widest zoom "
            "is hours."
        )
        layout.addWidget(self.play_when, 1)

        layout.addSpacing(12)  # design's gap before zoom

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setToolTip("Zoom out.")
        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setToolTip("Zoom in about the middle of the view.")
        self.btn_fit = QPushButton("Fit all")
        self.btn_fit.setToolTip("Show every clip.")
        for widget, width in (
            (self.btn_zoom_out, 34),
            (self.btn_zoom_in, 34),
            (self.btn_fit, 76),
        ):
            widget.setFixedWidth(width)
            layout.addWidget(widget)

        layout.addSpacing(8)
        self.btn_options = QPushButton("Setup archive..")
        self.btn_options.setToolTip("Archive folders, priority order, and rescanning.")
        layout.addWidget(self.btn_options)

        self._top_row = row
        return row

    def _build_control_box(self) -> QWidget:
        """Selection and export in one box.

        Transport (play/stop and cursor field) moved to top row; this box is
        the snip range, its options, and the format. Cursor, range, and play
        stay one gesture: cursor is the from-edge half the time, range is a
        drag away, and play acts on whichever is set.
        """
        box = QGroupBox("")  # No title
        layout = QHBoxLayout(box)  # Two columns

        # =================== LEFT COLUMN: Region Selection ===================
        left_layout = QVBoxLayout(box)

        region_group = QGroupBox("Region Selection")
        region_layout = QVBoxLayout(region_group)
        region_layout.setContentsMargins(8, 8, 8, 8)

        export_group = QGroupBox("Export")
        export_layout = QVBoxLayout(export_group)
        export_layout.setContentsMargins(8, 8, 8, 8)

        # -- row 1: the range to snip
        snip_row_1 = QHBoxLayout()
        snip_row_2 = QHBoxLayout()
        snip_row_3 = QHBoxLayout()

        export_row_1 = QHBoxLayout()
        export_row_2 = QHBoxLayout()
        export_row_3 = QHBoxLayout()

        self.snip_from = DateField()
        self.snip_to = DateField()
        self.snip_length = QSpinBox()
        self.snip_length.setRange(1, 24 * 60 * 60)
        self.snip_length.setValue(300)
        self.snip_length.setSuffix(" s")
        self.snip_length.setToolTip("Length used by “Set from cursor”.")

        self.btn_set_from = QPushButton("Set ← cursor")
        self.btn_set_to = QPushButton("Set → cursor")
        self.btn_snip_at_cursor = QPushButton("Set range from cursor")
        self.btn_play_snip = QPushButton("Play selection")



        self.btn_export = QPushButton("Export Selected Region…")

        self.snip_from_label = QLabel("From")
        self.snip_to_label = QLabel("To")
        label_width = max(
            self.snip_from_label.sizeHint().width(),
            self.snip_to_label.sizeHint().width(),
        )
        self.snip_from_label.setFixedWidth(label_width)
        self.snip_to_label.setFixedWidth(label_width)

        snip_row_1.addWidget(self.snip_from_label)
        snip_row_1.addWidget(self.snip_from, alignment=Qt.AlignmentFlag.AlignBaseline)
        snip_row_2.addWidget(self.snip_to_label)
        snip_row_2.addWidget(self.snip_to)
        snip_row_3.addWidget(self.btn_play_snip)


        region_layout.addLayout(snip_row_1)
        region_layout.addLayout(snip_row_2)
        region_layout.addLayout(snip_row_3)

        # -- fade in/out row
        fade_row = QHBoxLayout()
        self.fade_in = QDoubleSpinBox()
        self.fade_in.setRange(0.0, 60.0)
        self.fade_in.setValue(0.1)
        self.fade_in.setSingleStep(0.05)
        self.fade_in.setSuffix(" s")
        self.fade_in.setToolTip("Fade in duration in seconds.")
        self.fade_out = QDoubleSpinBox()
        self.fade_out.setRange(0.0, 60.0)
        self.fade_out.setValue(0.1)
        self.fade_out.setSingleStep(0.05)
        self.fade_out.setSuffix(" s")
        self.fade_out.setToolTip("Fade out duration in seconds.")

        fade_row.addWidget(QLabel("Fade in:"))
        fade_row.addWidget(self.fade_in)
        fade_row.addWidget(QLabel("Fade out:"))
        fade_row.addWidget(self.fade_out)
        fade_row.addStretch(1)




        # -- row 3: options and format
        self.chk_fill_gaps = QCheckBox("Pad gaps with silence")
        self.chk_fill_gaps.setToolTip(
            "Off: only audio that exists is written, and the export is shorter "
            "than the requested range. On: silence is inserted so the output is "
            "exactly the requested length."
        )
        fade_row.addWidget(self.chk_fill_gaps)

        self.chk_bext = QCheckBox("Add BEXT from selected block")
        self.chk_bext.setToolTip(
            "Write BWF - Broadcast Wave Format metadata (EBU Tech 3285) using the "
            "currently selected time block. Includes originator reference for "
            "round-trip identification."
        )
        export_row_3.addWidget(self.chk_bext)

        export_row_2.addWidget(QLabel("Format bitrate:"))
        self.format_box = QComboBox()
        self.format_box.setToolTip(
            "Default follows the archive. Pick an override only when the "
            "sources disagree or you need a different rate."
        )
        self.format_box.setMinimumWidth(190)
        export_row_2.addWidget(self.format_box, 1)

        export_row_3.addWidget(self.btn_export)

        export_layout.addLayout(fade_row)
        export_layout.addLayout(export_row_1)
        export_layout.addLayout(export_row_2)
        export_layout.addLayout(export_row_3)

        # region_layout.addStretch(1)

        left_layout.addWidget(region_group)  # Left column takes 1 part
        left_layout.addWidget(export_group)

        layout.addLayout(left_layout, 1)

        # =================== RIGHT COLUMN: Block Editing ===================
        right_group = QGroupBox("Block Editing")
        right_layout = QVBoxLayout(right_group)
        right_layout.setContentsMargins(8, 8, 8, 8)

        # -- Time block editor: basic info
        tb_row1 = QHBoxLayout()
        tb_row2 = QHBoxLayout()

        self.tb_name = QLineEdit()
        self.tb_name.setPlaceholderText("Name")
        self.tb_name.setMinimumWidth(120)
        self.tb_start = DateField()
        self.tb_start.setToolTip("Start time (D.M.Y. H:M:S)")
        self.tb_end = DateField()
        self.tb_end.setToolTip("End time (D.M.Y. H:M:S)")
        self.tb_episode = QSpinBox()
        self.tb_episode.setRange(0, 9999)
        self.tb_episode.setValue(0)
        self.tb_episode.setSuffix(" ep")
        self.tb_episode.setMinimumWidth(70)

        tb_row1.addWidget(QLabel("Name"))
        tb_row1.addWidget(self.tb_name)
        tb_row1.addWidget(self.tb_episode)
        tb_row2.addWidget(QLabel("Start"))
        tb_row2.addWidget(self.tb_start)
        tb_row2.addWidget(QLabel("End"))
        tb_row2.addWidget(self.tb_end)
        right_layout.addLayout(tb_row1)
        right_layout.addLayout(tb_row2)

        # -- Description (big text area)
        tb_row_desc = QHBoxLayout()
        tb_row_desc.addWidget(QLabel("Description"))
        self.tb_desc = QTextEdit()
        self.tb_desc.setPlaceholderText("Description")
        self.tb_desc.setMinimumHeight(60)
        self.tb_desc.setMaximumHeight(100)
        tb_row_desc.addWidget(self.tb_desc, 1)
        right_layout.addLayout(tb_row_desc)

        # -- Hosts and Guests (bigger fields)
        tb_row_hosts = QHBoxLayout()
        tb_row_hosts.addWidget(QLabel("Hosts"))
        self.tb_hosts = QLineEdit()
        self.tb_hosts.setPlaceholderText("Hosts (comma-separated)")
        tb_row_hosts.addWidget(self.tb_hosts, 1)
        right_layout.addLayout(tb_row_hosts)

        tb_row_guests = QHBoxLayout()
        tb_row_guests.addWidget(QLabel("Guests"))
        self.tb_guests = QLineEdit()
        self.tb_guests.setPlaceholderText("Guests (comma-separated)")
        tb_row_guests.addWidget(self.tb_guests, 1)
        right_layout.addLayout(tb_row_guests)

        # -- Action buttons
        tb_row_actions = QHBoxLayout()
        self.btn_tb_new = QPushButton("New")
        self.btn_tb_save = QPushButton("Save")
        self.btn_tb_delete = QPushButton("Delete")
        tb_row_actions.addStretch(1)
        tb_row_actions.addWidget(self.btn_tb_new)
        tb_row_actions.addWidget(self.btn_tb_save)
        tb_row_actions.addWidget(self.btn_tb_delete)
        right_layout.addLayout(tb_row_actions)

        right_layout.addStretch(1)
        layout.addWidget(right_group, 1)  # Right column takes 1 part

        return box
        
    def _wire(self) -> None:
        self.btn_options.clicked.connect(self.open_options)
        self.btn_zoom_in.clicked.connect(self.wave.zoom_in)
        self.btn_zoom_out.clicked.connect(self.wave.zoom_out)
        self.btn_fit.clicked.connect(self.wave.fit_all)

        self.btn_play.clicked.connect(self._on_play)
        self.play_when.changed.connect(self._on_play_date_entered)
        self.play_when.invalid.connect(self._on_play_date_invalid)
        self.snip_from.changed.connect(self._on_snip_changed)
        self.snip_from.invalid.connect(self._on_snip_field_invalid)
        self.snip_to.changed.connect(self._on_snip_changed)
        self.snip_to.invalid.connect(self._on_snip_field_invalid)

        self.btn_set_from.clicked.connect(lambda: self._set_snip_edge(0))
        self.btn_set_to.clicked.connect(lambda: self._set_snip_edge(1))
        self.btn_snip_at_cursor.clicked.connect(self._on_snip_at_cursor)
        self.btn_play_snip.clicked.connect(self._on_play_snip)
        self.btn_export.clicked.connect(self._on_export)
        # Time blocks editor
        self.btn_tb_save.clicked.connect(self._on_tb_save)
        self.btn_tb_delete.clicked.connect(self._delete_selected_time_block)
        self.btn_tb_new.clicked.connect(self._on_tb_new)

        self.wave.playheadMoved.connect(self._on_playhead_from_wave)
        self.wave.snipSelected.connect(self._on_snip_from_wave)
        self.wave.snipCleared.connect(self._on_snip_cleared)
        self.wave.viewChanged.connect(self._on_view_changed)
        self.wave.requestContextMenu.connect(self._on_context_menu)
        # Time block signals
        self.wave.timeBlockContextMenu.connect(self._on_tb_context_menu)
        self.wave.timeBlockSelected.connect(self._on_tb_selected)
        self.scrollbar.valueChanged.connect(self._on_scrolled)

        self.player.positionChanged.connect(self._on_position)
        self.player.stateChanged.connect(self._on_playback_state)
        self.player.jumped.connect(self._on_jumped)
        self.player.error.connect(self._on_player_error)

        # Space toggles playback, the way it does in every other player.
        play_action = QAction("Play / Stop", self)
        play_action.setShortcut(QKeySequence(Qt.Key_Space))
        play_action.triggered.connect(self._on_play)
        self.addAction(play_action)

        # i - set region start
        set_from_action = QAction("Set region start (i)", self)
        set_from_action.setShortcut(QKeySequence(Qt.Key_I))
        set_from_action.triggered.connect(lambda: self._set_snip_edge(0))
        self.addAction(set_from_action)

        # o - set region end
        set_to_action = QAction("Set region end (o)", self)
        set_to_action.setShortcut(QKeySequence(Qt.Key_O))
        set_to_action.triggered.connect(lambda: self._set_snip_edge(1))
        self.addAction(set_to_action)

        # Delete - delete selected time block
        delete_tb_action = QAction("Delete time block (Del)", self)
        delete_tb_action.setShortcut(QKeySequence(Qt.Key_Delete))
        delete_tb_action.triggered.connect(self._delete_selected_time_block)
        self.addAction(delete_tb_action)

        # Esc - let go of the block being edited.
        deselect_tb_action = QAction("Deselect time block (Esc)", self)
        deselect_tb_action.setShortcut(QKeySequence(Qt.Key_Escape))
        deselect_tb_action.triggered.connect(self._deselect_time_block)
        self.addAction(deselect_tb_action)

    # -- config ----------------------------------------------------------

    def load_state(self) -> int:
        """Adopt archive folders from config file.

        Deliberately *not* called from ``__init__``. Reading developer's real
        config as side effect of building window would make every test
        non-hermetic, and archives on this machine are none of a test's
        business. Caller decides, and entry point does.

        Does not scan: caller batches that, so launching with folder argument
        scans once rather than twice.
        """
        folders = self._config.load()
        if not folders:
            return 0
        self._archives = [
            Archive(path=folder, priority=index) for index, folder in enumerate(folders)
        ]
        self._rebuild_timeline()
        return len(self._archives)

    def save_state(self) -> None:
        """Remember current archive folders."""
        self._config.save([archive.path for archive in self._archives])

    def archives(self) -> list[Path]:
        return [archive.path for archive in self._archives]

    # -- time blocks -------------------------------------------------------

    def _load_time_blocks(self) -> None:
        """Load time blocks from config and update wave view.

        What loads, loads. Entry with missing field or unreadable date used to
        raise straight out of load and take window with it, so blocks that
        *were* fine never shown; now only that entry dropped, and
        :meth:`_report_time_block_problems` says which and why.
        """
        blocks, problems = self._tb_config.load_with_report()
        self._time_blocks = blocks
        self._report_time_block_problems(problems)
        self._update_time_blocks_in_view()
        self._update_button_states()

    def _report_time_block_problems(self, problems: list[str]) -> None:
        """Say what did not load: every problem to console, first to bar.

        Console gets all in full because it's where "what exactly is wrong with
        this file" belongs; status bar gets one -- it's a strip, not a log --
        with count standing in for the rest.
        """
        for problem in problems:
            print(problem, file=sys.stderr)
        if not problems:
            return
        message = problems[0]
        if len(problems) > 1:
            message += f" (+{len(problems) - 1} more, see console)"
        self._push_status(message)

    def _save_time_blocks(self) -> None:
        """Save time blocks to config."""
        self._tb_config.save(self._time_blocks)

    def _update_time_blocks_in_view(self) -> None:
        """Update wave view with current time blocks.

        Selection carried across on purpose. State rebuilt here field by field,
        and it used to stop short of ``selected_time_block``, so every path
        that calls this -- a save above all, which is the moment user most
        expects block to still be theirs -- emptied editor by way of a
        highlight.

        Key taken from ``_editing_block`` rather than copied from old state,
        because saving *replaces* block: previous tuple names bounds that may
        no longer exist, and highlight keyed on stale tuple would paint nothing
        while insisting something was selected.
        """
        tb_data = WaveView.assign_time_block_rows(self._time_blocks)
        # Update just the time_blocks in the existing state
        current = self.wave._state
        self.wave.set_state(WaveViewState(
            start_us=current.start_us,
            end_us=current.end_us,
            coverage=current.coverage,
            gaps=current.gaps,
            clips=current.clips,
            peaks=current.peaks,
            peaks_match=current.peaks_match,
            peaks_possible=current.peaks_possible,
            playhead_us=current.playhead_us,
            snip_from_us=current.snip_from_us,
            snip_to_us=current.snip_to_us,
            label=current.label,
            time_blocks=tb_data,
            selected_time_block=self._selected_block_key(),
        ))

    def _selected_block_key(self) -> tuple[int, int, str, int] | None:
        """Highlight for whatever editor is holding, or ``None``.

        Membership of ``_time_blocks`` is part of answer, not a formality:
        deleted block no longer painted, so key naming it would claim selection
        that draws nothing.
        """
        editing = self._editing_block
        if editing is None or editing not in self._time_blocks:
            return None
        return (editing.start_us, editing.end_us, editing.name, editing.episode_num)

    def _replace_block(self, old: TimeBlock, new: TimeBlock) -> None:
        """Swap block for its edited copy, keeping everything pointing at it.

        Blocks immutable, so editing one means replacing it -- editor holds the
        *object*, not an index. Re-point ``_editing_block`` here rather than at
        each of four call sites, because anywhere that forgets leaves window
        editing block no longer in list, silently dropped by
        :meth:`_selected_block_key`.
        """
        self._time_blocks[self._time_blocks.index(old)] = new
        if self._editing_block is old:
            self._editing_block = new

    # -- archives --------------------------------------------------------

    def add_archive(self, path: Path) -> Archive:
        """Add a folder, or promote it to the top if already listed."""
        path = Path(path)
        existing = next((a for a in self._archives if a.path == path), None)
        if existing is not None:
            self._archives.remove(existing)
            self._archives.insert(0, existing)
        else:
            existing = Archive(path=path, priority=0)
            self._archives.insert(0, existing)

        self._renumber()
        self.save_state()
        self._rebuild_timeline()
        self._on_rescan()
        return existing

    def apply_archives(self, folders: list[Path]) -> None:
        """Adopt whole priority order -- what options dialog hands back.

        Not "add each one": dialog is authority on the list, and adding them
        one at a time would reverse the order.
        """
        self._archives = [
            Archive(path=Path(folder), priority=index)
            for index, folder in enumerate(folders)
        ]
        self.save_state()
        self._rebuild_timeline()
        if self._archives:
            self._on_rescan()
        else:
            self._update_archive_summary()
            self.status.showMessage("No archive folders. Open Options… to add one.")

    def _renumber(self) -> None:
        for index, archive in enumerate(self._archives):
            self._archives[index] = Archive(path=archive.path, priority=index)

    def open_options(self) -> None:
        """Show options dialog and adopt whatever it comes back with."""
        dialog = OptionsDialog(
            self.archives(),
            self._archive_details(),
            self,
            self._config_note(),
            view_settings=self._view_settings,
        )
        dialog.rescanRequested.connect(self._on_rescan)
        dialog.viewSettingsChanged.connect(self._on_view_settings_changed)
        if dialog.exec():
            self.apply_archives(dialog.archives)
            self._view_settings = dialog.view_settings
            self._view_config.save(self._view_settings)
            self._apply_view_settings()

    def _on_view_settings_changed(self, settings: dict) -> None:
        """Apply view settings immediately as they change in the dialog."""
        self._view_settings = settings
        self._apply_view_settings()

    def _apply_view_settings(self) -> None:
        """Apply current view settings to the wave view."""
        self.wave.set_block_height(self._view_settings.get("block_height", 30))
        self.wave.set_block_margin(self._view_settings.get("block_margin", 6))
        self.wave.set_show_blocks(self._view_settings.get("show_blocks", True))
        self.wave.set_show_clips(self._view_settings.get("show_clips", True))
        self.wave.set_show_waveforms(self._view_settings.get("show_waveforms", True))
        self.wave.set_keep_playhead_visible(self._view_settings.get("keep_playhead_visible", False))
        self.wave.update()

        # Check if time blocks path changed
        custom_tb_path = self._view_settings.get("time_blocks_path", "")
        if custom_tb_path:
            if self._tb_config.path != Path(custom_tb_path):
                self._tb_config = TimeBlocksConfig(Path(custom_tb_path))
                self._load_time_blocks()
        else:
            if self._tb_config.path != self._default_tb_path:
                self._tb_config = TimeBlocksConfig()
                self._load_time_blocks()

    def _config_note(self) -> str:
        note = f"Config saved to {self._config.path}."
        if not self._config.writable():
            note += " This location cannot be written to, so folders will not persist."
        return note

    def _archive_details(self) -> dict[Path, str]:
        """One summary line per archive, for options dialog to show."""
        out: dict[Path, str] = {}
        timeline = self._timeline
        for archive in self._archives:
            clips = []
            if timeline is not None:
                clips = [c for c in timeline.clips if c.archive_path == archive.path]

            if not clips:
                failures = timeline.failures if timeline is not None else []
                broken = any(f.startswith(f"{archive.path.name}:") for f in failures)
                out[archive.path] = (
                    "Cannot be read. Check the folder is present and reachable."
                    if broken
                    else "No indexed audio."
                )
                continue

            earliest = min(c.start_us for c in clips)
            latest = max(c.end_us for c in clips)
            hours = (latest - earliest) / HOUR
            text = (
                f"{archive.path}\n"
                f"{len(clips)} clips\n"
                f"{format_datetime(from_us(earliest))}\n"
                f"to {format_datetime(from_us(latest))}\n"
                f"{hours:.1f} h"
            )
            overlaps = count_overlaps(clips)
            if overlaps:
                text += f"\n{overlaps} overlapping pairs"
            out[archive.path] = text
        return out

    def _update_archive_summary(self) -> None:
        """Status line's lead: how many clips timeline holds.

        Per `design.md` top bar keeps clip count -- with click precision and
        "making waveforms" spinner beside it -- and drops folder count and
        timeline's extent. Those two live in Setup dialog's per-archive details.
        """
        if not self._archives:
            self.archive_summary.setText("No archive folders.")
            return

        timeline = self._timeline
        if timeline is None or not timeline.coverage:
            self.archive_summary.setText("No audio indexed yet.")
            return

        self.archive_summary.setText(f"{len(timeline.clips):,} clips")

    def rescan(self) -> None:
        """Public entry point for re-indexing, for callers outside the window.

        Named for what it does rather than how it's wired, because launch
        script needs it and reaching into a private slot from ``__main__`` is
        a smell rather than a shortcut.
        """
        self._on_rescan()

    def _on_rescan(self) -> None:
        """Re-index every folder, on a worker thread.

        A folder of hourly chunks is thousands of files, each needs its header
        opened and length read -- and archives described as possibly living on a
        network share, where that is seconds to minutes. On event loop that's a
        frozen window with no play button to reach for, so it goes to a thread
        like everything else that touches audio.
        """
        if not self._archives:
            self._push_status("No archives to scan.")
            return

        # Scan archives configured *now*, not whatever configured when job
        # finishes: folder added mid-scan must not be reported as scanned.
        archives = list(self._archives)

        job = self._start_job(
            self._scan_job,
            lambda: _scan_all(archives),
            on_finished=lambda summary: self._on_scan_finished(summary),
        )
        if job is None:
            self._push_status("A scan is already running.")
            return
        self._scan_job = job
        self._push_status("Scanning archives…")

    def _on_scan_finished(self, summary: dict) -> None:
        self._rebuild_timeline()
        self._pop_status("Scanning archives…")

        message = f"{summary['clips']} clips indexed."
        if summary["skipped"]:
            message += f" {summary['skipped']} unreadable file(s) skipped."
        if summary["overlaps"]:
            message += f" {summary['overlaps']} overlapping pair(s); the higher archive wins."
        if summary["failures"]:
            message += f" Scan failed for: {'; '.join(summary['failures'])}"
        self._push_status(message)

    def _start_job(
        self,
        existing: BackgroundJob | None,
        job: object,
        *,
        on_finished,
    ) -> BackgroundJob | None:
        """Wire a job's signals, refusing to start over a running one.

        Returns job, or None if something already in flight -- caller keeps
        ownership so it can decide whether that's an error worth reporting.
        """
        if existing is not None and existing.busy:
            return None

        background = BackgroundJob(self)
        background.finished.connect(on_finished)
        background.failed.connect(self._on_job_failed)
        background.done.connect(self._on_job_done)
        background.done.connect(background.deleteLater)
        if not background.start(job):
            background.deleteLater()
            return None
        self._busy += 1
        self._update_busy()
        return background

    def _on_job_failed(self, message: str) -> None:
        self.status.showMessage(message)
        self._pop_status("Scanning archives…")
        self._pop_status("Rendering snip preview…")

    def _on_job_done(self) -> None:
        self._busy = max(0, self._busy - 1)
        self._update_busy()

    def _update_busy(self) -> None:
        """Reflect what is running: a spinner, and buttons that would conflict.

        Spinner covers both long jobs counted in ``_busy`` and "making
        waveforms" decode tracked by ``_peaks_busy``.

        Disabling buttons is not politeness -- a second scan or export started
        while first in flight either races it or queues behind it, and both
        outcomes worse than being told to wait.
        """
        busy = self._busy > 0
        self.progress.setVisible(busy or self._peaks_busy)
        self.btn_export.setEnabled(not busy and self._snip is not None)
        self.btn_play_snip.setEnabled(not busy and self._snip is not None)

    def _set_peaks_busy(self, busy: bool) -> None:
        """Flip the "making waveforms" spinner for the peaks worker."""
        self._peaks_busy = busy
        self.progress.setVisible(self._busy > 0 or busy)

    # -- timeline --------------------------------------------------------

    def _rebuild_timeline(self) -> None:
        """Re-read indexes and tell view how big the archive is.

        Only reads -- scanning is a separate, explicit step. That's why adding
        an archive triggers both but removing one triggers only this.
        """
        if not self._archives:
            self._timeline = None
            self.player.set_timeline(Timeline([]))
            self._adopt_limits()
            self._refresh_wave()
            self._refresh_formats()  # nothing left to offer but "Default"
            self._update_archive_summary()
            return

        self._timeline = Timeline(self._archives).refresh()
        self.player.set_timeline(self._timeline)
        if self._playhead_us is None:
            self._playhead_us = self._default_playhead()

        self._adopt_limits()
        self._sync_fields_from_playhead()
        self._refresh_wave()
        self._refresh_formats()
        self._update_archive_summary()
        self._report_unreadable()

    def _adopt_limits(self) -> None:
        """Tell view archive's extent, so it knows what it may zoom to.

        View keeps whatever span it already had if that still fits, which stops
        adding a second archive from throwing away the zoom.
        """
        timeline = self._timeline
        coverage = timeline.coverage if timeline is not None else []
        if coverage:
            first = min(s for s, _ in coverage)
            last = max(e for _, e in coverage)
        else:
            first, last = 0, 1
        self.wave.set_limits(Limits.for_extent(first, last))

    def _default_playhead(self) -> int:
        """Where to look first: the newest audio, else now."""
        timeline = self._timeline
        if timeline is None or not timeline.coverage:
            return us_now()
        latest = max(end for _, end in timeline.coverage)
        # Nudge back into audio so first play doesn't jump.
        return latest - 1

    def _report_unreadable(self) -> None:
        """Say so when a configured archive could not be read at all.

        Without this symptom is an app that looks empty: every playhead lookup
        consults same list of clips, and folder that failed to load contributes
        none, so its audio simply absent. Archive stays in list on purpose --
        share not mounted yet worth keeping place for, so Rescan recovers it.
        """
        failures = self._timeline.failures if self._timeline is not None else []
        for text in failures:
            self.status.showMessage(f"Cannot read archive — {text}")

    # -- the view range --------------------------------------------------

    def _on_view_changed(self, start_us: int, end_us: int) -> None:
        """View moved: keep scrollbar honest and redraw for new range."""
        self._sync_scrollbar()
        self._refresh_wave()

    def _sync_scrollbar(self) -> None:
        """Show scrollbar where view actually is."""
        start, end = self.wave.view()
        minimum, maximum, value = scroll_to_scrollbar(start, end, self.wave.limits())
        self._syncing_scroll = True
        try:
            self.scrollbar.setRange(minimum, maximum)
            self.scrollbar.setValue(value)
            self.scrollbar.setEnabled(maximum > 0)
        finally:
            self._syncing_scroll = False

    def _on_scrolled(self, value: int) -> None:
        """Scrollbar moved: move view to match.

        Guarded because mapping is lossy. Round-tripping value would nudge it
        by a step each time and two would push each other in loop that never
        settles.
        """
        if self._syncing_scroll:
            return
        start, end = self.wave.view()
        self.wave.set_view(
            *scroll_from_scrollbar(value, end - start, self.wave.limits()),
            user=True,
        )

    # -- playhead --------------------------------------------------------

    def _on_now(self) -> None:
        self.set_playhead(us_now())

    def _nudge_week(self, weeks: int) -> None:
        """Move the playhead by whole weeks, keeping the time of day.

        ±7 days rather than a calendar step: the design asks for week buttons,
        and adding 7 days keeps the wall-clock time even across a DST change in
        a way that stepping to "the same weekday" would not.
        """
        if self._playhead_us is None:
            self.set_playhead(us_now())
            return
        self.set_playhead(self._playhead_us + weeks * WEEK)

    def set_playhead(self, instant_us: int) -> None:
        self._playhead_us = instant_us
        self._sync_fields_from_playhead()
        self.wave.set_playhead(instant_us)
        self.player.seek(instant_us)
        # An instant chosen with the week buttons or the date field can be well
        # outside the current zoom, and playing something off screen is playing
        # something the user cannot see.
        # self.wave.ensure_visible(instant_us)
        self._refresh_wave()

    def _sync_fields_from_playhead(self) -> None:
        if self._playhead_us is None:
            return
        self.play_when.set_instant(self._playhead_us)

    def _on_playhead_from_wave(self, instant_us: int) -> None:
        self.set_playhead(int(instant_us))

    def _on_position(self, instant_us: int) -> None:
        """Follow playback: move the playhead and the date field with it."""
        instant = int(instant_us)
        self._playhead_us = instant
        self.wave.set_playhead(instant)
        self.play_when.set_instant(instant)

        # Scroll behaviour depends on "Keep playhead visible" setting
        keep_visible = self._view_settings.get("keep_playhead_visible", False)
        if keep_visible:
            # Always center the playhead
            self.wave.pan_to(instant, user=False)
        else:
            # Scroll only when the playhead actually leaves the view; always
            # re-centring would make a wide waveform crawl the whole time.
            # Unless playback just started - wait for the first poll to settle.
            if getattr(self, '_playhead_just_started', False):
                self._playhead_just_started = False
            else:
                self.wave.ensure_visible(instant)

    def _on_play_date_entered(self, instant_us: int) -> None:
        """The field parsed; take it as the new cursor."""
        self.set_playhead(int(instant_us))

    def _on_play_date_invalid(self, message: str, _text: str) -> None:
        """Put the field back so the display cannot disagree with the cursor.

        The status bar carries the complaint; ``restore`` simply puts the last
        good value back so the field stops showing text that was rejected.
        """
        self.status.showMessage(f"Date: {message}")
        if self._playhead_us is not None:
            self.play_when.restore(self._playhead_us)

    def _on_snip_field_invalid(self, message: str, _text: str) -> None:
        """Report a bad snip date but keep the last valid range.

        Unlike the cursor field, a snip field is legitimately empty while the
        user is still filling it in, so there is no previous good value to put
        back. The range summary keeps describing the last range that did parse,
        and the error says why it stopped being current.
        """
        self.status.showMessage(f"Snip date: {message}")

    # -- playback --------------------------------------------------------

    def _on_play(self) -> None:
        if self.player.is_playing:
            self.player.stop()
            return
        if self._timeline is None or not self._archives:
            self.status.showMessage("Open Options… and add an archive folder first.")
            return

        moment = self._playhead_us if self._playhead_us is not None else us_now()
        try:
            self.player.play_at(moment)
        except PlayerError as exc:
            self._on_player_error(str(exc))
        # Flag to allow ensure_visible to pan on the first poll
        self._playhead_just_started = True

    def _on_stop(self) -> None:
        self.player.stop()
        self._playhead_us = None

    def _on_playback_state(self, playing: bool) -> None:
        self.btn_play.setText("Pause" if playing else "Play")

    def _on_jumped(self, start_us: int, end_us: int) -> None:
        """Say so when playback skipped a gap; silence would look like a bug."""
        start, end = int(start_us), int(end_us)
        if abs(end - start) < SEC:
            return
        self.status.showMessage(
            f"Gap skipped: {format_datetime(from_us(start))}"
            f" → {format_datetime(from_us(end))}"
        )

    def _on_player_error(self, message: str) -> None:
        self._push_status(f"Playback: {message}")

    # -- status message queue ----------------------------------------------

    def _push_status(self, message: str) -> None:
        """Add a message to the status queue and display it."""
        self._status_messages.append(message)
        self._update_status_display()

    def _pop_status(self, message: str) -> None:
        """Remove a specific message from the queue."""
        try:
            self._status_messages.remove(message)
        except ValueError:
            pass
        self._update_status_display()

    def _update_status_display(self) -> None:
        """Update the status bar with all queued messages, newest first."""
        if self._status_messages:
            display = " | ".join(reversed(self._status_messages))
            self.status.showMessage(display)
        else:
            self.status.clearMessage()

    # -- snip ------------------------------------------------------------

    def _set_snip_edge(self, which: int) -> None:
        if self._playhead_us is None:
            return
        (self.snip_from if which == 0 else self.snip_to).set_instant(self._playhead_us)
        self._on_snip_changed()

    def _on_snip_at_cursor(self) -> None:
        """The common case: a fixed length starting at the cursor."""
        if self._playhead_us is None:
            return
        length = self.snip_length.value()
        self.snip_from.set_instant(self._playhead_us)
        self.snip_to.set_instant(self._playhead_us + length * SEC)
        self._on_snip_changed()

    def _on_snip_from_wave(self, start_us: int, end_us: int) -> None:
        self.snip_from.set_instant(int(start_us))
        self.snip_to.set_instant(int(end_us))
        self._on_snip_changed()

    def _on_snip_cleared(self) -> None:
        self._snip = None
        self._refresh_formats()  # back to the whole archive's formats
        self._update_busy()

    def _on_context_menu(self, instant_us: int) -> None:
        """Show the right-click context menu at the given instant."""
        menu = self._build_context_menu(instant_us)
        menu.exec(self.mapToGlobal(self.wave.mapFromGlobal(self.cursor().pos())))

    def _build_context_menu(self, instant_us: int) -> QMenu:
        """The viewport menu for ``instant_us``, built but not shown.

        Split out from :meth:`_on_context_menu` because nearly everything in it
        is conditional on that instant -- which clip is under the pointer,
        whether a region exists, whether a block is selected -- and a menu that
        can only be inspected by exec'ing it cannot be asserted on. Here the
        conditions are testable and the exec is one line.
        """
        instant_us = int(instant_us)
        menu = QMenu(self)

        # Seek and play
        play_action = QAction("Play from here", self)
        play_action.triggered.connect(lambda: self.player.play_at(instant_us))
        menu.addAction(play_action)

        # Reveal the file under the pointer -- only where there is one. Over a
        # gap there is nothing to reveal, and offering the action anyway would
        # make the menu claim there is audio where the picture says there is
        # none. Resolving is a binary search over the already-loaded index, so
        # it costs nothing to ask on every menu.
        if self._timeline is not None:
            clip = self._timeline.resolve(instant_us).clip
            if clip is not None:
                reveal_action = QAction("Reveal clip in explorer", self)
                # A zero-argument lambda, like every other entry in this menu:
                # `triggered` passes a checked bool, and a slot with a parameter
                # of its own would receive that bool as its argument -- so the
                # default is never used and the file manager gets `False`.
                reveal_action.triggered.connect(lambda: self._reveal_clip(clip.path))
                menu.addAction(reveal_action)

        menu.addSeparator()

        # Region start/end
        set_from_action = QAction("Set selection start (I)", self)
        set_from_action.triggered.connect(lambda: self._set_snip_edge(0))
        menu.addAction(set_from_action)

        set_to_action = QAction("Set selection end (O)", self)
        set_to_action.triggered.connect(lambda: self._set_snip_edge(1))
        menu.addAction(set_to_action)

        # New time block from region (if region exists)
        if self._snip is not None:
            low, high = self._snip
            if high > low:
                new_tb_action = QAction("New block from region", self)
                new_tb_action.triggered.connect(lambda: self._create_time_block_from_region(low, high))
                menu.addAction(new_tb_action)

        # If a time block is selected, allow setting its start/end
        editing = getattr(self, '_editing_block', None)
        if editing is not None:
            menu.addSeparator()

            menu.addAction(self._deselect_block_action())

            set_block_start = QAction("Set selected block start", self)
            set_block_start.triggered.connect(lambda: self._set_selected_block_start(instant_us))
            menu.addAction(set_block_start)

            set_block_end = QAction("Set selected block end", self)
            set_block_end.triggered.connect(lambda: self._set_selected_block_end(instant_us))
            menu.addAction(set_block_end)

        return menu

    def _deselect_block_action(self) -> QAction:
        """The "Deselect block" entry, shared by both right-click menus.

        The Esc tip rides along in the **text** rather than through
        ``setShortcut``, so the key has exactly one owner: the window action
        bound in :meth:`_wire`. One owner means nothing can disagree with it
        while a menu is open, where Esc belongs to the menu alone -- closing a
        menu is not a decision about the block. The entry triggers the same
        handler, so the tip is no less true for being a label.
        """
        action = QAction("Deselect block (Esc)", self)
        action.triggered.connect(self._deselect_time_block)
        return action

    def _reveal_clip(self, path) -> None:
        """Hand a clip's file to the file manager, or say why there is none.

        The status line reports the outcome because the alternative to a message
        is silence: ``reveal`` refuses quietly when the path has gone, and a
        click that visibly does nothing reads as a broken menu rather than a
        missing file.
        """
        path = Path(path)
        if reveal(path):
            self.status.showMessage(f"Revealed {path.name}")
        else:
            self.status.showMessage(f"No file to reveal: {path}")

    def _on_snip_changed(self) -> None:
        low = self.snip_from.instant()
        high = self.snip_to.instant()
        if low is None or high is None:
            self._on_snip_cleared()
            return

        # Order the *range* rather than the editors.
        #
        # Writing the swap back into the two date fields is the obvious thing to
        # do and it is wrong: each field's `changed` signal is wired to this
        # handler, so typing a "from" later than the current "to" would fire the
        # handler, which would swap the fields, which would fire it again -- and
        # the user would end up with their date in the wrong box. It also makes
        # the common edit order impossible to type at all.
        if high < low:
            low, high = high, low

        self._snip = (low, high)
        self.wave.set_snip(low, high)
        self._describe_snip()
        self._refresh_formats()

    def _describe_snip(self) -> None:
        if self._snip is None:
            return
        low, high = self._snip
        if high <= low:
            self._snip = None
            self._update_busy()
            return

        duration = (high - low) / SEC
        self._update_busy()

        parts = [f"{duration:.1f} s"]
        timeline = self._timeline
        if timeline is not None:
            plan = timeline.plan_snip(low, high)
            parts.append(f"{len(plan.pieces)} piece(s)")
            if plan.has_gaps:
                parts.append(f"{len(plan.gaps)} gap(s), {plan.missing_us / SEC:.1f} s missing")
            if len(plan.archives_used) > 1:
                parts.append(f"{len(plan.archives_used)} archives")
            if plan.needs_resample:
                parts.append("will resample")
        self._push_status(
            f"{format_datetime(from_us(low))}  →  {format_datetime(from_us(high))}"
            f"    ({', '.join(parts)})"
        )

    def _current_plan(self):
        if self._snip is None or self._timeline is None:
            return None
        low, high = self._snip
        return self._timeline.plan_snip(low, high)

    def _on_play_snip(self) -> None:
        plan = self._current_plan()
        if plan is None:
            return
        if plan.is_empty:
            self._push_status("The snip range contains no audio.")
            return
        # Play selection by starting normal playback at the selection start,
        # with a limit at the selection end. This behaves exactly like normal
        # play, just starting at the selection.
        self.player.play_at(plan.start_us, limit_us=plan.end_us)
        # Update UI to reflect the new playhead position
        self.set_playhead(plan.start_us)

    def _on_export(self) -> None:
        """Ask where to put it, then do it. Split so the work is testable."""
        plan = self._current_plan()
        if plan is None:
            return
        if plan.is_empty:
            self.status.showMessage("The snip range contains no audio.")
            return

        suggested = f"snip_{from_us(plan.start_us):%Y%m%d_%H%M%S}.wav"
        chosen = QFileDialog.getSaveFileName(
            self, "Export snip", str(Path.home() / suggested), "WAV files (*.wav)"
        )
        if not chosen[0]:
            return
        self.start_export(Path(chosen[0]))

    def _build_bext_chunk(self, plan, fmt: AudioFormat) -> BextChunk | None:
        """Build BextChunk from currently selected time block, if any."""
        if not self.chk_bext.isChecked():
            return None

        editing = getattr(self, '_editing_block', None)
        if editing is None:
            self.status.showMessage("No time block selected for BEXT metadata.")
            return None

        block = editing
        # Build description: name + description (max 256 chars)
        desc_parts = [block.name]
        if block.description:
            desc_parts.append(block.description)
        description = ": ".join(desc_parts)[:256]

        # Originator reference: AE:{ep:04d}:{start_us:016x}:{uuid[:8]}
        originator_ref = f"AE:{block.episode_num:04d}:{block.start_us:016x}:{block.uuid[:8]}"

        # Origination date/time from block start
        start_dt = from_us(block.start_us)
        origination_date = start_dt.strftime("%Y-%m-%d")
        origination_time = start_dt.strftime("%H:%M:%S")

        # Time reference: samples since midnight at output sample rate
        # Midnight of the origination date
        midnight = datetime(start_dt.year, start_dt.month, start_dt.day)
        midnight_us = to_us(midnight)
        samples_since_midnight = ((block.start_us - midnight_us) * fmt.framerate) // 1_000_000

        # Coding history per EBU R098
        coding_parts = [
            "A=ArchiveExplorer",
            f"V=1.0",
            f"E={block.episode_num:04d}",
            f"S={fmt.framerate}/{fmt.nchannels}/{fmt.sampwidth * 8}",
            f"O={start_dt.strftime('%Y-%m-%dT%H:%M:%S')}",
        ]
        if self.chk_fill_gaps.isChecked():
            coding_parts.append("G=fill_gaps")
        coding_history = ";".join(coding_parts)

        return BextChunk(
            description=description,
            originator="ArchiveExplorer",
            originator_reference=originator_ref,
            origination_date=origination_date,
            origination_time=origination_time,
            time_reference=samples_since_midnight,
            version=2,  # Include loudness fields
            coding_history=coding_history,
        )

    def start_export(self, path: Path) -> bool:
        """Render the current snip on a worker thread.

        Exporting reads and writes the whole range at once, which for anything
        longer than a few minutes is a frozen window -- and the natural reaction
        to a frozen window is to click the export button again. So the work goes
        to a thread and the button goes dead while it runs.

        The plan, format and padding choice are all captured *now*. Reading them
        off the widgets when the job finishes would mean exporting something the
        user did not ask for if they moved the cursor meanwhile.
        """
        plan = self._current_plan()
        if plan is None or plan.is_empty:
            self.status.showMessage("The snip range contains no audio.")
            return False

        target = Path(path)
        if target.suffix.lower() != ".wav":
            target = target.with_suffix(".wav")

        try:
            override = self._selected_format()
        except ExportError as exc:
            self.status.showMessage(str(exc))
            return False

        fill_gaps = self.chk_fill_gaps.isChecked()
        fade_in = self.fade_in.value()
        fade_out = self.fade_out.value()

        # Build BEXT chunk if requested
        bext = self._build_bext_chunk(plan, override or AudioFormat(
            plan.pieces[0].clip.framerate,
            plan.pieces[0].clip.nchannels,
            plan.pieces[0].clip.sampwidth
        ))

        job = self._start_job(
            self._export_job,
            lambda: render_snip(plan, target, target=override, fill_gaps=fill_gaps, bext=bext, fade_in_s=fade_in, fade_out_s=fade_out),
            on_finished=lambda result: self._on_export_finished(result, target),
        )
        if job is None:
            self.status.showMessage("An export is already running.")
            return False

        self._export_job = job
        self._export_target = target
        self.status.showMessage(f"Exporting {target.name}…")
        return True

    def _on_export_finished(self, result, target: Path) -> None:
        message = (
            f"Exported {result.nframes} frames ({result.duration:.1f} s) to {target.name}"
        )
        if result.missing_us:
            message += f". {result.missing_us / SEC:.1f} s were missing and left out."
        elif result.plan.missing_us:
            message += f". {result.plan.missing_us / SEC:.1f} s were padded with silence."
        self.status.showMessage(message)
        if self.chk_fill_gaps.isChecked():
            self.player.play_file(result.path)

    def export_to(self, path: Path):
        """Render the current snip to ``path`` and wait for it.

        Returns the result, or None if nothing could be exported. This is the
        blocking core of :meth:`start_export`, split out so it can be tested
        without a thread in the way, and kept public because a caller that
        genuinely wants to block -- a script, a test -- should not have to
        reimplement it.
        """
        plan = self._current_plan()
        if plan is None or plan.is_empty:
            self.status.showMessage("The snip range contains no audio.")
            return None

        target = Path(path)
        if target.suffix.lower() != ".wav":
            target = target.with_suffix(".wav")

        try:
            override = self._selected_format()
        except ExportError as exc:
            self.status.showMessage(str(exc))
            return None

        # Build BEXT chunk if requested
        bext = self._build_bext_chunk(plan, override or AudioFormat(
            plan.pieces[0].clip.framerate,
            plan.pieces[0].clip.nchannels,
            plan.pieces[0].clip.sampwidth
        ))

        self.status.showMessage(f"Exporting {target.name}…")
        try:
            result = render_snip(
                plan,
                target,
                target=override,
                fill_gaps=self.chk_fill_gaps.isChecked(),
                bext=bext,
                fade_in_s=self.fade_in.value(),
                fade_out_s=self.fade_out.value(),
            )
        except ExportError as exc:
            self.status.showMessage(f"Export failed: {exc}")
            return None

        self._on_export_finished(result, target)
        return result

    def _selected_format(self) -> AudioFormat | None:
        index = self.format_box.currentIndex()
        if index <= 0:
            return None  # default
        rate, channels, width = self.format_box.currentData()
        return AudioFormat(rate, channels, width)

    def _refresh_formats(self) -> None:
        """Offer the formats actually present in the range about to be exported.

        This used to sit behind an "Update" button, because the only automatic
        refresh was `_rebuild_timeline` -- the list is built from the *plan*
        (`SnipPlan.formats`), and moving the snip changes the plan without
        touching the archive set. It is now called from wherever the data
        changes: `_on_snip_changed`, `_on_snip_cleared`, and the rebuild. The
        button asked the user to notice that the box was stale; this does not.

        A refresh that would list exactly what is already listed returns before
        touching the box: clearing and repopulating it would put a still-valid
        override back on "Default" and flicker, for no change in information.
        """
        plan = self._current_plan()
        seen: list[tuple[int, int, int]] = []
        if plan is not None:
            seen = list(plan.formats)
        elif self._timeline is not None:
            seen = sorted({(c.framerate, c.nchannels, c.sampwidth) for c in self._timeline.clips})

        entries = [("Default (first source)", None)] + [
            (f"{rate} Hz, {channels} ch, {width * 8}-bit", (rate, channels, width))
            for rate, channels, width in seen
        ]
        listed = [self.format_box.itemText(i) for i in range(self.format_box.count())]
        if listed == [label for label, _ in entries]:
            return

        previous = self.format_box.currentData()
        self.format_box.clear()
        for label, data in entries:
            self.format_box.addItem(label, data)

        # An override that is still offered survives the rebuild. One that is
        # no longer in the range does not: it named sources that are not there
        # any more, and the box falls back to "Default".
        if previous is not None:
            index = self.format_box.findData(previous)
            if index > 0:
                self.format_box.setCurrentIndex(index)

    # -- waveform --------------------------------------------------------

    def _refresh_wave(self) -> None:
        """Redraw for the current view range, and ask for its peaks if affordable."""
        timeline = self._timeline
        instant = self._playhead_us

        # Preserve the currently selected time block
        current_state = self.wave.state()
        selected_tb = current_state.selected_time_block

        if timeline is None or not timeline.coverage:
            start, end = self.wave.view()
            self.wave.set_state(
                WaveViewState(
                    start_us=start,
                    end_us=end,
                    coverage=[],
                    gaps=[],
                    label="no archive",
                    playhead_us=instant,
                    snip_from_us=self._snip[0] if self._snip else None,
                    snip_to_us=self._snip[1] if self._snip else None,
                    time_blocks=WaveView.assign_time_block_rows(self._time_blocks),
                    selected_time_block=selected_tb,
                )
            )
            self.clip_label.setText("")
            self._sync_scrollbar()
            return

        start, end = self.wave.view()
        affordable = self._peaks_affordable(end - start)
        have = self._peak_range == (start, end)

        self.wave.set_state(
            WaveViewState(
                start_us=start,
                end_us=end,
                coverage=list(timeline.coverage),
                gaps=timeline.gaps(),
                clips=[(c.start_us, c.end_us, c.path.name) for c in timeline.clips_between(start, end)],
                peaks=self.wave.state().peaks if have else None,
                peaks_match=have,
                peaks_possible=affordable,
                playhead_us=instant,
                snip_from_us=self._snip[0] if self._snip else None,
                snip_to_us=self._snip[1] if self._snip else None,
                time_blocks=WaveView.assign_time_block_rows(self._time_blocks),
                selected_time_block=selected_tb,
            )
        )
        self._update_clip_label()
        self._sync_scrollbar()

        if affordable:
            self._request_peaks(start, end)

    def _peaks_affordable(self, span_us: int) -> bool:
        """Could a real waveform be drawn for a window this wide?

        The line is in *time*, not frames, because reading a window no longer
        costs ``span * framerate``: the envelope is sampled (see
        :mod:`wavesnip.peaks`), so a day-wide view and a year-wide view read
        the same few megabytes and a frame budget would measure nothing but
        convert to different seconds at different rates. What the line is for is
        the split design.md asks for -- past a week the coverage map says more
        than a sampled outline would -- and deciding it before building a snip
        plan avoids resolving priority across every clip in a year-long archive
        to then throw the result away.
        """
        return span_us <= MAX_WAVE_SPAN_US

    def _update_clip_label(self) -> None:
        timeline = self._timeline
        if timeline is None:
            self.clip_label.setText("")
            return
        per_pixel = self.wave.span_per_pixel()
        if per_pixel >= 3600:
            precision = f"1 px ≈ {per_pixel / 3600:.1f} h"
        elif per_pixel >= 1:
            precision = f"1 px ≈ {per_pixel:.0f} s"
        else:
            precision = f"1 px ≈ {per_pixel * 1000:.0f} ms"
        self.clip_label.setText(precision)

    def _request_peaks(self, start_us: int, end_us: int) -> None:
        """Decode the visible range on a worker thread.

        Every few seconds while dragging, or while the view follows playback,
        this gets called again, so it must be cheap to refuse: a job already
        running is left alone, and the next refresh picks up whatever it is
        producing.
        """
        if self._timeline is None or self._peak_range == (start_us, end_us):
            return
        if self._peak_job is not None and self._peak_job.busy:
            return

        timeline = self._timeline
        plan = timeline.plan_snip(start_us, end_us)
        if plan.is_empty:
            self._peak_range = (start_us, end_us)
            self._refresh_wave()
            return

        job = BackgroundJob(self)
        job.finished.connect(lambda peaks, lo=start_us, hi=end_us: self._on_peaks(peaks, lo, hi))
        job.done.connect(job.deleteLater)
        self._peak_job = job

        started = job.start(lambda: peaks_for(plan, cache=self._file_peaks))
        if not started:
            job.deleteLater()
            self._peak_job = None
            return

        def _peaks_done() -> None:
            if self._peak_job is job:
                self._set_peaks_busy(False)

        job.done.connect(_peaks_done)
        self._set_peaks_busy(True)

    def _on_peaks(self, peaks: Peaks | None, start_us: int, end_us: int) -> None:
        state = self.wave.state()
        viewing = (state.start_us, state.end_us)

        if viewing != (start_us, end_us):
            # Discard it: this is a picture of a range nobody is looking at.
            #
            # But if the range now on screen still has nothing, arrange to get
            # it. Every event that moved the view while this was decoding asked
            # for peaks and was refused because this job held the slot, so with
            # the pointer now still there may be nothing left that will ever ask
            # again -- and the view sits on a bare coverage map, with no waveform
            # and no hint to explain it. Nothing crashes and nothing looks wrong
            # from the inside; there is simply nothing there.
            #
            # The "already has peaks" guard covers panning away and straight
            # back, where those peaks are still the right answer: re-decoding
            # them would be waste and a visible flicker.
            #
            # The retry waits one event-loop turn because ``finished`` arrives
            # while the job is still counted as busy, and asking immediately
            # would be refused for exactly the same reason as before.
            if self._peak_range != viewing:
                self._peak_range = None
                QTimer.singleShot(0, self._refresh_wave)
            return
        self._peak_range = viewing
        state.peaks = peaks
        state.peaks_match = True
        self.wave.set_state(state)

    # -- lifecycle -------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.player.stop()
        for job in (self._peak_job, self._scan_job, self._export_job):
            if job is not None:
                job.shutdown()
        super().closeEvent(event)

    # -- time blocks -------------------------------------------------------

    def _on_tb_context_menu(self, instant_us: int, tb_data: tuple[int, int, str, int]) -> None:
        """Show context menu for a time block right-click."""
        menu = self._build_tb_context_menu(tb_data)
        menu.exec(self.mapToGlobal(self.wave.mapFromGlobal(self.cursor().pos())))

    def _build_tb_context_menu(self, tb_data: tuple[int, int, str, int]) -> QMenu:
        """The block's menu, built but not shown.

        Split out of :meth:`_on_tb_context_menu` for the reason
        :meth:`_build_context_menu` is split out: nearly every entry is
        conditional, and a menu that can only be inspected by watching it pop
        up cannot be asserted on.
        """
        start_us, end_us, name, episode_num = tb_data
        menu = QMenu(self)

        # Set selected region to block
        set_region_action = QAction("Selection from block (double click)", self)
        set_region_action.triggered.connect(lambda: self._set_snip_to_block(start_us, end_us))
        menu.addAction(set_region_action)

        # Set block to selected region
        if self._snip is not None:
            low, high = self._snip
            if high > low:
                set_block_region_action = QAction("Set block start-end from selection", self)
                set_block_region_action.triggered.connect(lambda: self._set_block_to_snip(tb_data))
                menu.addAction(set_block_region_action)

        menu.addSeparator()

        # Delete block with confirmation
        delete_action = QAction("Delete block (Delete)", self)
        delete_action.triggered.connect(lambda: self._delete_time_block_with_confirm(tb_data))
        menu.addAction(delete_action)

        # Offered only when there is something to let go of: right-clicking a
        # block does not select it, so this menu can well appear over an idle
        # editor, and a "deselect" that deselects nothing is a lie.
        if self._editing_block is not None:
            menu.addSeparator()
            menu.addAction(self._deselect_block_action())

        return menu

    def _set_snip_to_block(self, start_us: int, end_us: int) -> None:
        """Set the snip range to match a time block."""
        self.snip_from.set_instant(start_us)
        self.snip_to.set_instant(end_us)
        self._on_snip_changed()
        self.status.showMessage(f"Snip range set to time block: {format_datetime(from_us(start_us))} → {format_datetime(from_us(end_us))}")

    def _set_block_to_snip(self, tb_data: tuple[int, int, str, int]) -> None:
        """Update a time block to match the current snip range."""
        if self._snip is None:
            self.status.showMessage("No snip range set.")
            return
        low, high = self._snip
        start_us, end_us, name, episode_num = tb_data

        # Find and update the block
        for block in self._time_blocks:
            if block.start_us == start_us and block.end_us == end_us and block.name == name:
                self._replace_block(block, TimeBlock(
                    name=block.name,
                    start_us=low,
                    end_us=high,
                    episode_num=block.episode_num,
                    description=block.description,
                    hosts=block.hosts,
                    guests=block.guests,
                ))
                self._save_time_blocks()
                self._update_time_blocks_in_view()
                self.status.showMessage(f"Time block updated to snip range.")
                return

    def _create_time_block_from_region(self, start_us: int, end_us: int) -> None:
        """Create a new time block from the current snip region."""
        new_block = TimeBlock(
            name="Untitled region",
            start_us=start_us,
            end_us=end_us,
            episode_num=0,
            description="",
            hosts=[],
            guests=[],
        )
        self._time_blocks.append(new_block)
        self._save_time_blocks()
        self._update_time_blocks_in_view()
        self.status.showMessage(f"Time block created from region.")

    def _set_selected_block_start(self, instant_us: int) -> None:
        """Set the selected time block's start to the clicked instant."""
        editing = getattr(self, '_editing_block', None)
        if editing is not None:
            if instant_us < editing.end_us:
                new_block = TimeBlock(
                    name=editing.name,
                    start_us=instant_us,
                    end_us=editing.end_us,
                    episode_num=editing.episode_num,
                    description=editing.description,
                    hosts=editing.hosts,
                    guests=editing.guests,
                )
                self._replace_block(editing, new_block)
                self._save_time_blocks()
                self._update_time_blocks_in_view()
                # Update editor fields
                self.tb_start.set_instant(instant_us)
                self.status.showMessage(f"Block start set to {format_datetime(from_us(instant_us))}")
            else:
                self.status.showMessage("Start must be before end.")

    def _set_selected_block_end(self, instant_us: int) -> None:
        """Set the selected time block's end to the clicked instant."""
        editing = getattr(self, '_editing_block', None)
        if editing is not None:
            if instant_us > editing.start_us:
                new_block = TimeBlock(
                    name=editing.name,
                    start_us=editing.start_us,
                    end_us=instant_us,
                    episode_num=editing.episode_num,
                    description=editing.description,
                    hosts=editing.hosts,
                    guests=editing.guests,
                )
                self._replace_block(editing, new_block)
                self._save_time_blocks()
                self._update_time_blocks_in_view()
                # Update editor fields
                self.tb_end.set_instant(instant_us)
                self.status.showMessage(f"Block end set to {format_datetime(from_us(instant_us))}")
            else:
                self.status.showMessage("End must be after start.")

    def _on_tb_save(self) -> None:
        """Save or update a time block from the editor."""
        name = self.tb_name.text().strip()
        if not name:
            self.status.showMessage("Time block name is required.")
            return

        start_us = self.tb_start.instant()
        end_us = self.tb_end.instant()
        if start_us is None or end_us is None:
            self.status.showMessage("Start and end times are required.")
            return
        if end_us <= start_us:
            self.status.showMessage("End time must be after start time.")
            return

        episode_num = self.tb_episode.value()
        description = self.tb_desc.toPlainText().strip()
        hosts = [h.strip() for h in self.tb_hosts.text().split(",") if h.strip()]
        guests = [g.strip() for g in self.tb_guests.text().split(",") if g.strip()]

        # Check if editing existing block
        editing = self._editing_block
        if editing is not None:
            # Update existing
            saved = TimeBlock(
                name=name,
                start_us=start_us,
                end_us=end_us,
                episode_num=episode_num,
                description=description,
                hosts=hosts,
                guests=guests,
            )
            self._replace_block(editing, saved)
            self.status.showMessage(f"Time block updated: {name}")
        else:
            # Add new
            saved = TimeBlock(
                name=name,
                start_us=start_us,
                end_us=end_us,
                episode_num=episode_num,
                description=description,
                hosts=hosts,
                guests=guests,
            )
            self._time_blocks.append(saved)
            self.status.showMessage(f"Time block added: {name}")

        # Saving does not let go of the block. Clearing the editor here was the
        # whole complaint: Save is the button you press *while* editing, so
        # using it to deselect left the fields empty and the Save button still
        # enabled over nothing -- and a second press would have added a copy
        # instead of updating. The editor keeps what it holds and keeps naming
        # the object it just wrote, so Save can be pressed repeatedly on one
        # block and the highlight follows the bounds it just changed.
        self._editing_block = saved
        self._save_time_blocks()
        self._update_time_blocks_in_view()
        self._update_button_states()

    def _on_tb_delete(self) -> None:
        """Delete the currently edited time block."""
        editing = getattr(self, '_editing_block', None)
        if editing is not None:
            self._time_blocks.remove(editing)
            self._save_time_blocks()
            self._update_time_blocks_in_view()
            self._clear_tb_editor()
            self.status.showMessage(f"Time block deleted.")
        else:
            self.status.showMessage("No time block selected for deletion.")

    def _delete_selected_time_block(self) -> None:
        """Delete the currently selected time block with confirmation."""
        editing = getattr(self, '_editing_block', None)
        if editing is not None:
            reply = QMessageBox.question(
                self,
                "Delete time block",
                f"Are you sure you want to delete {editing.name}?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self._time_blocks.remove(editing)
                self._save_time_blocks()
                self._update_time_blocks_in_view()
                self._clear_tb_editor()
                self.status.showMessage(f"Time block deleted.")
        else:
            self.status.showMessage("No time block selected for deletion.")

    def _delete_time_block_with_confirm(self, tb_data: tuple[int, int, str, int]) -> None:
        """Delete a specific time block with confirmation."""
        start_us, end_us, name, episode_num = tb_data
        for i, block in enumerate(self._time_blocks):
            if block.start_us == start_us and block.end_us == end_us and block.name == name:
                reply = QMessageBox.question(
                    self,
                    "Delete time block",
                    f"Are you sure you want to delete {block.name}?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    self._time_blocks.pop(i)
                    self._save_time_blocks()
                    self._update_time_blocks_in_view()
                    # Clear editor if it was editing this block
                    if getattr(self, '_editing_block', None) is block:
                        self._clear_tb_editor()
                    self.status.showMessage(f"Time block deleted.")
                return

    def _on_tb_new(self) -> None:
        """Create a new time block from the current snip region."""
        if self._snip is None:
            self.status.showMessage("No snip range set. Select a range first.")
            return

        low, high = self._snip
        if high <= low:
            self.status.showMessage("Snip range is empty.")
            return

        name = self.tb_name.text().strip()
        if not name:
            self.status.showMessage("Time block name is required.")
            return

        episode_num = self.tb_episode.value()
        description = self.tb_desc.toPlainText().strip()
        hosts = [h.strip() for h in self.tb_hosts.text().split(",") if h.strip()]
        guests = [g.strip() for g in self.tb_guests.text().split(",") if g.strip()]

        new_block = TimeBlock(
            name=name,
            start_us=low,
            end_us=high,
            episode_num=episode_num,
            description=description,
            hosts=hosts,
            guests=guests,
        )
        self._time_blocks.append(new_block)
        # The editor is now describing *this* block, bounds included.
        # `_on_tb_selected` fills them when a block is clicked, and without the
        # same two lines here the fields are left empty by [+ New] -- at which
        # point Save reads nothing from them, refuses with "Start and end times
        # are required", and cannot even reach the block sitting in
        # `_editing_block`.
        self.tb_start.set_instant(new_block.start_us)
        self.tb_end.set_instant(new_block.end_us)

        # Set as the currently editing block *before* the view is rebuilt, or
        # the rebuild looks for a selection that has not been declared yet and
        # the block just created comes up unhighlighted.
        self._editing_block = new_block
        self._save_time_blocks()
        self._update_time_blocks_in_view()
        self._update_button_states()
        self.status.showMessage(f"Time block created from region: {name}")

    def _clear_tb_editor(self) -> None:
        """Clear the time block editor fields."""
        self.tb_name.clear()
        self.tb_start.set_instant(0)
        self.tb_end.set_instant(0)
        self.tb_episode.setValue(0)
        self.tb_desc.setPlainText("")
        self.tb_hosts.setText("")
        self.tb_guests.setText("")
        self._editing_block = None

    def _on_tb_selected(self, tb_data: tuple[int, int, str, int]) -> None:
        """Handle time block selection from waveform click."""
        start_us, end_us, name, episode_num = tb_data
        # Find the full block data
        for block in self._time_blocks:
            if block.start_us == start_us and block.end_us == end_us and block.name == name:
                self.tb_name.setText(block.name)
                self.tb_start.set_instant(block.start_us)
                self.tb_end.set_instant(block.end_us)
                self.tb_episode.setValue(block.episode_num)
                self.tb_desc.setPlainText(block.description)
                self.tb_hosts.setText(", ".join(block.hosts))
                self.tb_guests.setText(", ".join(block.guests))
                self._editing_block = block
                # Highlight in waveform
                self.wave.set_selected_time_block(tb_data)
                self.status.showMessage(f"Selected time block: {name}")
                self._update_button_states()
                return

    def _deselect_time_block(self) -> None:
        """Let go of the block being edited.

        One of exactly four ways a selection ends -- this one (Esc and both
        menus' "Deselect block"), clicking a *different* block, and deleting
        it. Clicking empty space deliberately is not one of them: that press
        starts a region drag, and a pointer crossing the canvas on its way
        elsewhere should not empty the editor. Says nothing when nothing is
        selected, so Esc pressed into an idle window is not an event.
        """
        selected = (
            self._editing_block is not None
            or self.wave.state().selected_time_block is not None
        )
        self._clear_tb_editor()
        self.wave.set_selected_time_block(None)
        self._update_button_states()
        if selected:
            self.status.showMessage("Time block deselected.")

    def _update_button_states(self) -> None:
        """Enable/disable buttons based on selection state."""
        has_selection = self._editing_block is not None
        self.btn_tb_save.setEnabled(has_selection)
        self.btn_tb_delete.setEnabled(has_selection)

def _scan_all(archives: list[Archive]) -> dict:
    """Index every archive and summarise. Runs on a worker thread.

    One unreadable folder must not take the whole scan down: the others may be
    perfectly fine, and losing their audio because one network share hiccuped is
    the wrong trade. So a failure is collected and reported, not raised.
    """
    clips = 0
    overlaps = 0
    skipped = 0
    failures: list[str] = []

    for archive in archives:
        try:
            report = ArchiveIndex(archive).scan()
        except Exception as exc:
            failures.append(f"{archive.path.name}: {exc}")
            continue

        clips += report.total
        skipped += report.skipped

        # Overlaps are only ever reported. Priority decides the winner, so a
        # count is all the summary line needs to say about them.
        overlaps += report.overlaps

    return {
        "clips": clips,
        "overlaps": overlaps,
        "skipped": skipped,
        "failures": failures,
    }
