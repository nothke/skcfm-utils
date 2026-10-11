"""The options window: archive folders and their priority order, plus view settings.

A dialog rather than a permanent panel, because archives are setup not something
you interact with while working. Everything here needed once and never again --
whereas waveform, cursor and selection are needed constantly, and a permanent
panel for the former steals width from the latter.

**Dialog edits a copy.** Handed current list, works on its own, and only on
accept does window take result, re-index, and save it. Editing live list in
place would mean Cancel had to undo, and "undo" for list of scanned folders is
not a thing -- index and timeline already changed.

**Adding a folder does not scan it here.** Scanning belongs to window, which
owns workers and timeline; dialog just asks via :attr:`rescanRequested`. Keeps
this file free of indexing knowledge.
"""

from __future__ import annotations

from pathlib import Path
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wavesnip.reveal import reveal

__all__ = ["OptionsDialog"]


class OptionsDialog(QDialog):
    """Archive folders and view settings, in priority order, plus a way to re-index them."""

    #: Emitted when the user asks for a rescan. The window owns the workers, so it
    #: handles this rather than the dialog starting a scan it cannot report on.
    rescanRequested = Signal()

    #: Emitted when view settings change and should be applied immediately.
    viewSettingsChanged = Signal(dict)

    def __init__(
        self,
        archives: list[Path] | None = None,
        details: dict[Path, str] | None = None,
        parent: QWidget | None = None,
        config_note: str = "",
        view_settings: dict | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.setModal(True)
        self.resize(560, 520)

        self._details = dict(details or {})
        self._view_settings = dict(view_settings or {})
        self._build_ui(config_note)

        self._archives = [Path(a) for a in (archives or [])]
        self._refresh()

    # -- construction ----------------------------------------------------

    def _build_ui(self, config_note: str) -> None:
        layout = QVBoxLayout(self)

        # Tab widget with Archives and View tabs
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        # -- Archives tab --
        archives_tab = QWidget()
        archives_layout = QVBoxLayout(archives_tab)

        heading = QLabel("Archive folders, highest priority first.")
        archives_layout.addWidget(heading)

        self.archive_list = QListWidget()
        self.archive_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.archive_list.currentRowChanged.connect(self._on_selection_changed)
        archives_layout.addWidget(self.archive_list, 1)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        archives_layout.addWidget(self.detail)

        row = QHBoxLayout()
        self.btn_add = QPushButton("Add folder…")
        self.btn_remove = QPushButton("Remove")
        self.btn_up = QPushButton("Up")
        self.btn_down = QPushButton("Down")
        self.btn_rescan = QPushButton("Rescan")
        for widget in (self.btn_add, self.btn_remove, self.btn_up, self.btn_down):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(self.btn_rescan)
        archives_layout.addLayout(row)

        if config_note:
            note = QLabel(config_note)
            note.setWordWrap(True)
            archives_layout.addWidget(note)

        # Time blocks config path
        from wavesnip.model import TimeBlocksConfig
        tb_config = TimeBlocksConfig()
        self._default_tb_path = tb_config.path
        self._custom_tb_path = self._view_settings.get("time_blocks_path", "")
        
        tb_row = QHBoxLayout()
        tb_label = QLabel("Blocks saved to:")
        tb_row.addWidget(tb_label)
        self.tb_path_label = QLabel(self._get_tb_display_path())
        self.tb_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tb_row.addWidget(self.tb_path_label, 1)
        btn_show_folder = QPushButton("Show in Folder")
        btn_show_folder.setToolTip("Open the folder containing time_blocks.json")
        btn_show_folder.clicked.connect(self._show_tb_folder)
        tb_row.addWidget(btn_show_folder)
        self.btn_set_custom_tb = QPushButton("Set custom file…")
        self.btn_set_custom_tb.setToolTip("Select a custom time_blocks.json file")
        self.btn_set_custom_tb.clicked.connect(self._set_custom_tb_file)
        tb_row.addWidget(self.btn_set_custom_tb)
        self.btn_reset_tb = QPushButton("Reset to default")
        self.btn_reset_tb.setToolTip("Use the default time_blocks.json location")
        self.btn_reset_tb.clicked.connect(self._reset_tb_file)
        self.btn_reset_tb.setVisible(bool(self._custom_tb_path))
        tb_row.addWidget(self.btn_reset_tb)
        archives_layout.addLayout(tb_row)

        self.tabs.addTab(archives_tab, "Archives")

        # -- View tab --
        view_tab = QWidget()
        view_layout = QVBoxLayout(view_tab)

        view_form = QFormLayout()

        self.chk_show_blocks = QCheckBox("Show time blocks")
        self.chk_show_blocks.setToolTip("Show time blocks as colored rectangles in the timeline.")
        self.chk_show_blocks.setChecked(self._view_settings.get("show_blocks", True))
        self.chk_show_blocks.toggled.connect(self._emit_view_settings)
        view_form.addRow(self.chk_show_blocks)

        self.chk_show_clips = QCheckBox("Show clips")
        self.chk_show_clips.setToolTip("Show clip blocks (archive coverage) in the timeline.")
        self.chk_show_clips.setChecked(self._view_settings.get("show_clips", True))
        self.chk_show_clips.toggled.connect(self._emit_view_settings)
        view_form.addRow(self.chk_show_clips)

        self.chk_show_waveforms = QCheckBox("Show waveforms")
        self.chk_show_waveforms.setToolTip("Show audio waveform envelope when zoomed in.")
        self.chk_show_waveforms.setChecked(self._view_settings.get("show_waveforms", True))
        self.chk_show_waveforms.toggled.connect(self._emit_view_settings)
        view_form.addRow(self.chk_show_waveforms)

        self.spin_block_height = QSpinBox()
        self.spin_block_height.setRange(10, 100)
        self.spin_block_height.setValue(self._view_settings.get("block_height", 30))
        self.spin_block_height.setSuffix(" px")
        self.spin_block_height.setToolTip("Height of each time block row in pixels.")
        self.spin_block_height.valueChanged.connect(self._emit_view_settings)
        view_form.addRow("Block height:", self.spin_block_height)

        self.chk_keep_playhead_visible = QCheckBox("Keep playhead visible")
        self.chk_keep_playhead_visible.setToolTip("Auto-scroll to keep the playhead in view during playback.")
        self.chk_keep_playhead_visible.setChecked(self._view_settings.get("keep_playhead_visible", False))
        self.chk_keep_playhead_visible.toggled.connect(self._emit_view_settings)
        view_form.addRow(self.chk_keep_playhead_visible)

        self.spin_block_margin = QSpinBox()
        self.spin_block_margin.setRange(0, 50)
        self.spin_block_margin.setValue(self._view_settings.get("block_margin", 6))
        self.spin_block_margin.setSuffix(" px")
        self.spin_block_margin.setToolTip("Margin between time blocks and the tick strip at the bottom.")
        self.spin_block_margin.valueChanged.connect(self._emit_view_settings)
        view_form.addRow("Block margin:", self.spin_block_margin)

        view_layout.addLayout(view_form)
        view_layout.addStretch(1)

        self.tabs.addTab(view_tab, "View")

        # Buttons at the bottom
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.btn_rescan.clicked.connect(self.rescanRequested)

    def _emit_view_settings(self) -> None:
        """Emit current view settings when any control changes.

        Built by merging into existing dict rather than replacing it, so keys
        this tab doesn't own -- ``time_blocks_path`` -- survive a toggle.
        Rebuilding from scratch dropped custom blocks path the moment any other
        control touched.
        """
        self._view_settings = {
            **self._view_settings,
            "show_blocks": self.chk_show_blocks.isChecked(),
            "show_clips": self.chk_show_clips.isChecked(),
            "show_waveforms": self.chk_show_waveforms.isChecked(),
            "block_height": self.spin_block_height.value(),
            "keep_playhead_visible": self.chk_keep_playhead_visible.isChecked(),
            "block_margin": self.spin_block_margin.value(),
        }
        self.viewSettingsChanged.emit(self._view_settings)

    @property
    def view_settings(self) -> dict:
        """Return the current view settings."""
        return dict(self._view_settings)

    # -- state -----------------------------------------------------------

    @property
    def archives(self) -> list[Path]:
        """The list as it stands, highest priority first."""
        return list(self._archives)

    def set_archives(self, archives: list[Path]) -> None:
        self._archives = [Path(a) for a in archives]
        self._refresh()

    def _refresh(self, select: int | None = None) -> None:
        self.archive_list.blockSignals(True)
        self.archive_list.clear()
        for index, archive in enumerate(self._archives):
            # Whole path, not `archive.name`. Two folders called "radio" in two
            # places are different archives holding different hours, and a list
            # of nothing but "0. radio / 1. radio" cannot say which is which --
            # which is precisely the question the priority order above it exists
            # to answer. The row is the only place the answer shows.
            self.archive_list.addItem(QListWidgetItem(f"{index}. {archive}"))
        self.archive_list.blockSignals(False)

        if self._archives:
            row = min(max(select if select is not None else 0, 0), len(self._archives) - 1)
            self.archive_list.setCurrentRow(row)
        self._on_selection_changed(self.archive_list.currentRow())

    def _on_selection_changed(self, row: int) -> None:
        selected = 0 <= row < len(self._archives)
        self.btn_remove.setEnabled(selected)
        self.btn_up.setEnabled(selected and row > 0)
        self.btn_down.setEnabled(selected and row < len(self._archives) - 1)

        if selected:
            archive = self._archives[row]
            self.detail.setText(self._details.get(archive, str(archive)))
        elif self._archives:
            self.detail.setText(f"{len(self._archives)} folders configured.")
        else:
            self.detail.setText("No archive folders. Add one to begin.")

    # -- editing ---------------------------------------------------------

    def _on_add(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Add archive folder")
        if chosen:
            self.add(Path(chosen))

    def add(self, path: Path) -> None:
        """Add a folder, or promote it to the top if already listed.

        Promoting rather than duplicating matches window's behaviour, so adding
        existing folder from either place means same thing -- top is where newly
        chosen folder belongs, since point of adding one is usually to have it
        win.

        No return value: no case where this does nothing. Folder either is new
        or already listed, and both end with it at top.
        """
        path = Path(path)
        existing = next((a for a in self._archives if a == path), None)
        if existing is not None:
            self._archives.remove(existing)

        self._archives.insert(0, path)
        self._refresh(select=0)

    def remove_selected(self) -> bool:
        row = self.archive_list.currentRow()
        if not 0 <= row < len(self._archives):
            return False
        self._archives.pop(row)
        self._refresh(select=min(row, len(self._archives) - 1))
        return True

    def move_selected(self, delta: int) -> bool:
        """Move selected folder up or down priority order.

        Returns False at ends of list, where nowhere to go. Caller wants to know
        that not just to avoid pointless refresh, but so test can assert refusal
        rather than absence of change.
        """
        row = self.archive_list.currentRow()
        target = row + delta
        if not 0 <= row < len(self._archives) or not 0 <= target < len(self._archives):
            return False
        archive = self._archives.pop(row)
        self._archives.insert(target, archive)
        self._refresh(select=target)
        return True

    def _show_tb_folder(self) -> None:
        """Show the time blocks file in the system file manager (selects the file)."""
        path = self._custom_tb_path if self._custom_tb_path else self._default_tb_path
        reveal(Path(path))

    def _get_tb_display_path(self) -> str:
        """Get the display path for the time blocks file."""
        if self._custom_tb_path:
            return f"{self._custom_tb_path} (custom)"
        return str(self._default_tb_path)

    def _set_custom_tb_file(self) -> None:
        """Open a file dialog to select a custom time_blocks.json file."""
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Select custom time_blocks.json", "", "JSON files (*.json);;All files (*)"
        )
        if chosen:
            self._custom_tb_path = chosen
            self._view_settings["time_blocks_path"] = chosen
            self.tb_path_label.setText(self._get_tb_display_path())
            self.btn_reset_tb.setVisible(True)
            self.viewSettingsChanged.emit(self._view_settings)

    def _reset_tb_file(self) -> None:
        """Reset to the default time_blocks.json location."""
        self._custom_tb_path = ""
        self._view_settings["time_blocks_path"] = ""
        self.tb_path_label.setText(self._get_tb_display_path())
        self.btn_reset_tb.setVisible(False)
        self.viewSettingsChanged.emit(self._view_settings)
