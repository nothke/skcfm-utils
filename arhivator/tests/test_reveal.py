"""Revealing a file in the system file manager.

The command is the interesting half, and it is a pure function of the platform
and of what is on the PATH -- which is the only reason the macOS and Windows
branches can be asserted at all from a Linux machine. Left as one function that
runs something, they would be three untested branches and a suite that passes
because it happened to run where the answer is already known.

What cannot be tested that way is the behaviour under failure: a file manager
that will not start must return ``False`` rather than raise into the menu that
asked for it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wavesnip.reveal import reveal, reveal_command


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.file = self.folder / "c0.wav"
        self.file.write_bytes(b"")

    def test_macos_asks_the_finder_to_reveal(self):
        self.assertEqual(
            reveal_command(self.file, "Darwin"),
            ["open", "-R", str(self.file)],
        )

    def test_windows_selects_the_file(self):
        # One argument, comma included: explorer splits the switch itself, and
        # passing the path separately makes it open the file instead.
        self.assertEqual(
            reveal_command(self.file, "Windows"),
            ["explorer", f"/select,{self.file}"],
        )

    def test_linux_selects_through_the_file_manager_service(self):
        with mock.patch("wavesnip.reveal.shutil.which", return_value="/usr/bin/dbus-send"):
            command = reveal_command(self.file, "Linux")
        self.assertEqual(command[0], "dbus-send")
        self.assertIn("org.freedesktop.FileManager1.ShowItems", command)
        self.assertIn(f"array:string:{self.file.resolve().as_uri()}", command)

    def test_without_the_service_it_opens_the_folder_rather_than_the_file(self):
        """``xdg-open`` on a wav plays it. The request was to be *shown* it."""
        with mock.patch("wavesnip.reveal.shutil.which", return_value=None):
            self.assertEqual(
                reveal_command(self.file, "Linux"), ["xdg-open", str(self.folder)]
            )
            # A folder asked for directly stays itself -- the fallback must not
            # walk one level further up just because it is a fallback.
            self.assertEqual(
                reveal_command(self.folder, "Linux"), ["xdg-open", str(self.folder)]
            )


class RevealTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)
        self.file = self.folder / "c0.wav"
        self.file.write_bytes(b"")

    def test_it_runs_what_the_command_builder_returns(self):
        with mock.patch(
            "wavesnip.reveal.reveal_command", return_value=["show", "it"]
        ) as build, mock.patch("wavesnip.reveal.subprocess.Popen") as popen:
            self.assertTrue(reveal(self.file))
        build.assert_called_once_with(self.file)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["show", "it"])

    def test_a_file_not_written_yet_reveals_where_it_will_be(self):
        """``time_blocks.json`` before the first save: the folder is the answer."""
        target = self.folder / "time_blocks.json"
        self.assertFalse(target.exists())
        with mock.patch("wavesnip.reveal.reveal_command") as build, \
                mock.patch("wavesnip.reveal.subprocess.Popen"):
            self.assertTrue(reveal(target))
        build.assert_called_once_with(self.folder)

    def test_somewhere_that_exists_at_all_is_refused(self):
        nowhere = self.folder / "no" / "such" / "place.wav"
        with mock.patch("wavesnip.reveal.subprocess.Popen") as popen:
            self.assertFalse(reveal(nowhere))
        popen.assert_not_called()

    def test_a_file_manager_that_will_not_start_does_not_raise(self):
        """The menu asked, the desktop has no file manager: report, do not blow up."""
        with mock.patch(
            "wavesnip.reveal.subprocess.Popen", side_effect=OSError("no such file")
        ):
            self.assertFalse(reveal(self.file))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
