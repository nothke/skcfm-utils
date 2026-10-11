"""The options dialog: the archive list, moved out of the window.

The contract that matters most is **copy on write**. The dialog is handed a list
and works on its own copy; nothing the window has built from the old list changes
until the dialog is accepted. That is not tidiness -- adding or reordering an
archive re-indexes folders and rebuilds the timeline, and a Cancel that undid that
by hand would have to un-scan files.

So the tests here are mostly about what does *not* change while the dialog is open.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QDialog
except ImportError:  # pragma: no cover
    QApplication = None

if QApplication is not None:
    from wavesnip.options import OptionsDialog

#: Stands in for a Path without needing one on disk: the dialog never opens a
#: folder, it only carries the path around.
A = "a"
B = "b"
C = "c"


class OptionsCase(unittest.TestCase):
    def setUp(self):
        if QApplication is None:
            self.skipTest("PySide6 not installed")
        self.app = QApplication.instance() or QApplication([])

    def dialog(self, archives=(), details=None, note="", view_settings=None):
        from pathlib import Path

        dialog = OptionsDialog(
            [Path(a) for a in archives], details, None, note, view_settings
        )
        self.addCleanup(dialog.deleteLater)
        return dialog

    def rows(self, dialog):
        return [dialog.archive_list.item(i).text() for i in range(dialog.archive_list.count())]


class ListContentTests(OptionsCase):
    def test_it_starts_empty(self):
        dialog = self.dialog()
        self.assertEqual(dialog.archives, [])
        self.assertEqual(dialog.archive_list.count(), 0)

    def test_it_lists_what_it_was_given_in_order(self):
        dialog = self.dialog([A, B, C])
        self.assertEqual([a.name for a in dialog.archives], [A, B, C])
        self.assertEqual(self.rows(dialog), ["0. a", "1. b", "2. c"])

    def test_the_rows_carry_the_whole_path(self):
        """Two folders called "radio" are different archives with different
        hours, and the row is the only place the priority order above it can be
        read against. Names alone would show "0. radio / 1. radio"."""
        from pathlib import Path

        dialog = self.dialog(["/archives/one/radio", "/archives/two/radio"])
        self.assertEqual(
            self.rows(dialog),
            ["0. " + str(Path("/archives/one/radio")),
             "1. " + str(Path("/archives/two/radio"))],
        )

    def test_the_list_says_which_end_wins(self):
        """"Which one wins" is the entire point of the order, and the rows alone
        do not say it."""
        from PySide6.QtWidgets import QLabel

        dialog = self.dialog([A, B])
        headings = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertTrue(
            any("highest priority" in text for text in headings),
            f"nothing says which end wins: {headings}",
        )

    def test_it_does_not_share_the_callers_list(self):
        """Copy on write, stated as a fact about the objects rather than as a
        promise in a docstring."""
        from pathlib import Path

        original = [Path(A), Path(B)]
        dialog = self.dialog(original)
        dialog.remove_selected()
        self.assertEqual([p.name for p in original], [A, B])

    def test_archives_returns_a_copy_too(self):
        """Otherwise a caller could edit the list behind the dialog's back."""
        dialog = self.dialog([A, B])
        grabbed = dialog.archives
        grabbed.clear()
        self.assertEqual(len(dialog.archives), 2)

    def test_it_reports_no_archives(self):
        dialog = self.dialog()
        self.assertIn("No archive folders", dialog.detail.text())

    def test_it_reports_the_archives_it_has(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(-1)
        self.assertIn("2 folders", dialog.detail.text())


class DetailTests(OptionsCase):
    def test_the_selected_archives_detail_is_shown(self):
        from pathlib import Path

        dialog = self.dialog([A], {Path(A): "3 clips\n12.5.2026."})
        dialog.archive_list.setCurrentRow(0)
        self.assertEqual(dialog.detail.text(), "3 clips\n12.5.2026.")

    def test_the_detail_follows_the_selection(self):
        from pathlib import Path

        dialog = self.dialog([A, B], {Path(A): "first", Path(B): "second"})
        dialog.archive_list.setCurrentRow(1)
        self.assertEqual(dialog.detail.text(), "second")

    def test_an_archive_with_no_detail_still_shows_its_path(self):
        from pathlib import Path

        dialog = self.dialog([A])
        dialog.archive_list.setCurrentRow(0)
        self.assertEqual(dialog.detail.text(), str(Path(A)))

    def test_the_config_note_is_shown(self):
        """Where the list is kept, so a user who moves their home directory is
        not left wondering why the folders came back empty."""
        from PySide6.QtWidgets import QLabel

        dialog = self.dialog(note="Saved to /home/x/archives.json.")
        labels = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertIn("Saved to /home/x/archives.json.", labels)


class EditingTests(OptionsCase):
    def test_add_puts_the_new_folder_on_top(self):
        dialog = self.dialog([A])
        dialog.add(__import__("pathlib").Path(B))
        self.assertEqual([p.name for p in dialog.archives], [B, A])

    def test_add_selects_what_it_added(self):
        """Otherwise the detail panel describes the wrong folder straight after
        the one the user just picked."""
        from pathlib import Path

        dialog = self.dialog([A])
        dialog.add(Path(B))
        self.assertEqual(dialog.archive_list.currentRow(), 0)

    def test_adding_a_known_folder_promotes_it(self):
        """Not a duplicate. Adding a folder is how you say "this one should win",
        and a second entry would make the priority meaningless."""
        from pathlib import Path

        dialog = self.dialog([A, B, C])
        dialog.add(Path(C))
        self.assertEqual([p.name for p in dialog.archives], [C, A, B])

    def test_remove_takes_out_the_selection(self):
        dialog = self.dialog([A, B, C])
        dialog.archive_list.setCurrentRow(1)
        self.assertTrue(dialog.remove_selected())
        self.assertEqual([p.name for p in dialog.archives], [A, C])

    def test_remove_with_nothing_selected_does_nothing(self):
        dialog = self.dialog([A])
        dialog.archive_list.setCurrentRow(-1)
        self.assertFalse(dialog.remove_selected())
        self.assertEqual(len(dialog.archives), 1)

    def test_removing_the_last_one_leaves_an_empty_list(self):
        dialog = self.dialog([A])
        dialog.archive_list.setCurrentRow(0)
        dialog.remove_selected()
        self.assertEqual(dialog.archives, [])
        self.assertIn("No archive folders", dialog.detail.text())

    def test_remove_keeps_a_nearby_row_selected(self):
        """Removing the last row must not leave the selection past the end."""
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(1)
        dialog.remove_selected()
        self.assertEqual(dialog.archive_list.currentRow(), 0)

    def test_up_swaps_with_the_row_above(self):
        dialog = self.dialog([A, B, C])
        dialog.archive_list.setCurrentRow(2)
        self.assertTrue(dialog.move_selected(-1))
        self.assertEqual([p.name for p in dialog.archives], [A, C, B])

    def test_down_swaps_with_the_row_below(self):
        dialog = self.dialog([A, B, C])
        dialog.archive_list.setCurrentRow(0)
        self.assertTrue(dialog.move_selected(1))
        self.assertEqual([p.name for p in dialog.archives], [B, A, C])

    def test_a_move_follows_the_selection(self):
        dialog = self.dialog([A, B, C])
        dialog.archive_list.setCurrentRow(2)
        dialog.move_selected(-1)
        self.assertEqual(dialog.archive_list.currentRow(), 1)

    def test_the_top_row_cannot_go_up(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(0)
        self.assertFalse(dialog.move_selected(-1))
        self.assertEqual([p.name for p in dialog.archives], [A, B])

    def test_the_bottom_row_cannot_go_down(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(1)
        self.assertFalse(dialog.move_selected(1))
        self.assertEqual([p.name for p in dialog.archives], [A, B])

    def test_cannot_move_with_nothing_selected(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(-1)
        self.assertFalse(dialog.move_selected(-1))
        self.assertFalse(dialog.move_selected(1))

    def test_the_row_numbers_are_redrawn_after_every_change(self):
        """Stale numbers would make the list say something other than its order."""
        dialog = self.dialog([A, B, C])
        dialog.archive_list.setCurrentRow(2)
        dialog.move_selected(-1)
        self.assertEqual(self.rows(dialog), ["0. a", "1. c", "2. b"])

    def test_set_archives_replaces_the_list(self):
        from pathlib import Path

        dialog = self.dialog([A])
        dialog.set_archives([Path(B), Path(C)])
        self.assertEqual([p.name for p in dialog.archives], [B, C])


class ButtonStateTests(OptionsCase):
    def test_remove_is_disabled_with_nothing_selected(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(-1)
        self.assertFalse(dialog.btn_remove.isEnabled())

    def test_up_is_disabled_on_the_top_row(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(0)
        self.assertFalse(dialog.btn_up.isEnabled())
        self.assertTrue(dialog.btn_down.isEnabled())

    def test_down_is_disabled_on_the_bottom_row(self):
        dialog = self.dialog([A, B])
        dialog.archive_list.setCurrentRow(1)
        self.assertTrue(dialog.btn_up.isEnabled())
        self.assertFalse(dialog.btn_down.isEnabled())

    def test_a_single_archive_can_go_neither_way(self):
        dialog = self.dialog([A])
        dialog.archive_list.setCurrentRow(0)
        self.assertFalse(dialog.btn_up.isEnabled())
        self.assertFalse(dialog.btn_down.isEnabled())

    def test_everything_is_disabled_when_there_is_nothing(self):
        dialog = self.dialog()
        for button in (dialog.btn_remove, dialog.btn_up, dialog.btn_down):
            self.assertFalse(button.isEnabled())

    def test_the_buttons_actually_do_it(self):
        """Driven through the widgets, not the methods behind them, so the
        wiring is covered rather than just the logic."""
        from pathlib import Path

        dialog = self.dialog([A, B])
        dialog.add(Path(C))
        self.assertEqual([p.name for p in dialog.archives], [C, A, B])

        # The buttons follow the selection, so down twice walks C all the way down.
        dialog.btn_down.click()
        self.assertEqual([p.name for p in dialog.archives], [A, C, B])
        dialog.btn_down.click()
        self.assertEqual([p.name for p in dialog.archives], [A, B, C])
        self.assertEqual(dialog.archive_list.currentRow(), 2)

        dialog.btn_up.click()
        self.assertEqual([p.name for p in dialog.archives], [A, C, B])

        dialog.archive_list.setCurrentRow(0)
        dialog.btn_remove.click()
        self.assertEqual([p.name for p in dialog.archives], [C, B])


class RescanTests(OptionsCase):
    def test_asking_for_a_rescan_is_a_request_not_a_scan(self):
        """The window owns the workers and the timeline; the dialog cannot
        report on a scan it did not start."""
        seen = []
        dialog = self.dialog([A])
        dialog.rescanRequested.connect(lambda: seen.append(True))
        dialog.btn_rescan.click()
        self.assertEqual(seen, [True])

    def test_the_signal_carries_nothing(self):
        dialog = self.dialog([A])
        seen = []
        dialog.rescanRequested.connect(lambda *args: seen.append(args))
        dialog.btn_rescan.click()
        self.assertEqual(seen, [()])


class AcceptanceTests(OptionsCase):
    def test_cancel_rejects(self):
        dialog = self.dialog([A])
        dialog.reject()
        self.assertEqual(dialog.result(), int(QDialog.DialogCode.Rejected))

    def test_accept_accepts(self):
        dialog = self.dialog([A])
        dialog.accept()
        self.assertEqual(dialog.result(), int(QDialog.DialogCode.Accepted))

    def test_the_edits_survive_a_cancel(self):
        """What the caller reads after a Cancel is still the original list.

        The window only applies the result on accept, so the edits are simply
        dropped -- which is the point of working on a copy.
        """
        from pathlib import Path

        original = [Path(A), Path(B)]
        dialog = self.dialog(original)
        dialog.remove_selected()
        dialog.add(Path(C))
        dialog.reject()
        self.assertEqual([p.name for p in original], [A, B])
        self.assertEqual([p.name for p in dialog.archives], [C, B])


class ViewSettingsTests(OptionsCase):
    """The View tab owns most settings, but not all.

    ``time_blocks_path`` is set by the Archives tab. Rebuilding the settings dict
    from just the View tab's controls used to drop it, so choosing a custom
    blocks file was silently undone the moment any other control was touched.
    """

    def test_a_toggle_keeps_settings_the_tab_does_not_own(self):
        dialog = self.dialog(view_settings={"time_blocks_path": "/custom/blocks.json"})
        self.assertEqual(dialog.view_settings["time_blocks_path"], "/custom/blocks.json")

        dialog.chk_show_blocks.setChecked(False)

        self.assertEqual(
            dialog.view_settings["time_blocks_path"], "/custom/blocks.json"
        )

    def test_the_emitted_settings_keep_the_custom_path(self):
        dialog = self.dialog(view_settings={"time_blocks_path": "/custom/blocks.json"})
        seen = []
        dialog.viewSettingsChanged.connect(seen.append)

        dialog.spin_block_height.setValue(42)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["time_blocks_path"], "/custom/blocks.json")
        self.assertEqual(seen[0]["block_height"], 42)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()