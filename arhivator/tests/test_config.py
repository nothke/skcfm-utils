"""Tests for wavesnip.config -- the remembered archive list.

Each test gets its own file in a temp directory. That is not just tidiness: a
module-level default path would let one test's write become another's input, and
the bug that causes is exactly the kind that passes in a fresh checkout.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from wavesnip.config import Config, default_path


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "cfg" / "archives.json"
        self.config = Config(self.path)

    def folder(self, name: str) -> Path:
        made = self.root / name
        made.mkdir(parents=True, exist_ok=True)
        return made


class MissingFileTests(ConfigCase):
    def test_a_missing_config_loads_as_empty(self):
        self.assertEqual(self.config.load(), [])

    def test_loading_does_not_create_the_file(self):
        """Reading state must not manufacture state."""
        self.config.load()
        self.assertFalse(self.path.exists())

    def test_an_empty_directory_is_also_empty(self):
        self.assertEqual(Config(self.root).load(), [])

    def test_a_directory_where_the_file_should_be_loads_as_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        self.assertEqual(self.config.load(), [])


class RoundTripTests(ConfigCase):
    def test_a_saved_list_comes_back(self):
        a, b = self.folder("a"), self.folder("b")
        self.config.save([a, b])
        self.assertEqual(self.config.load(), [a.resolve(), b.resolve()])

    def test_order_is_the_priority_order(self):
        """A list, not a set: the order is the whole point."""
        folders = [self.folder(n) for n in "dcba"]
        self.config.save(folders)
        self.assertEqual(self.config.load(), [f.resolve() for f in folders])

    def test_saving_nothing_then_loading_nothing(self):
        self.config.save([])
        self.assertEqual(self.config.load(), [])

    def test_the_file_is_human_readable(self):
        self.config.save([self.folder("a")])
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("archives", text)
        self.assertEqual(json.loads(text)["version"], 1)

    def test_saving_creates_the_directory(self):
        nested = Config(self.root / "a" / "b" / "c.json")
        nested.save([self.folder("x")])
        self.assertTrue(nested.path.exists())

    def test_saving_twice_replaces_rather_than_appends(self):
        self.config.save([self.folder("first")])
        self.config.save([self.folder("second")])
        self.assertEqual(self.config.load(), [self.folder("second").resolve()])

    def test_no_temporary_files_are_left_behind(self):
        self.config.save([self.folder("a")])
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])


class DamagedFileTests(ConfigCase):
    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_truncated_json_loads_as_empty(self):
        self.write('{"archives": ["/home/me')
        self.assertEqual(self.config.load(), [])

    def test_a_half_written_file_is_the_expected_failure(self):
        """The scenario the atomic write exists to prevent, pinned as a test."""
        self.write('{"version": 1, "archives": ["/home/me/a", "/home/me')
        self.assertEqual(self.config.load(), [])

    def test_a_json_scalar_loads_as_empty(self):
        self.write("42")
        self.assertEqual(self.config.load(), [])

    def test_archives_of_the_wrong_type_loads_as_empty(self):
        self.write('{"archives": "not a list"}')
        self.assertEqual(self.config.load(), [])

    def test_binary_junk_loads_as_empty(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"\xff\xfe\x00\x01garbage")
        self.assertEqual(self.config.load(), [])

    def test_a_damaged_file_is_replaced_on_the_next_save(self):
        self.write("{oh no")
        self.config.save([self.folder("a")])
        self.assertEqual(self.config.load(), [self.folder("a").resolve()])


class TolerantParsingTests(ConfigCase):
    def write(self, document: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def test_a_bare_list_is_accepted(self):
        """The obvious hand-editing mistake, so it is not punished."""
        a = self.folder("a")
        self.write([str(a)])
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_one_bad_entry_does_not_discard_the_others(self):
        a, c = self.folder("a"), self.folder("c")
        self.write({"archives": [str(a), 42, None, str(c)]})
        self.assertEqual(self.config.load(), [a.resolve(), c.resolve()])

    def test_empty_and_blank_entries_are_dropped(self):
        a = self.folder("a")
        self.write({"archives": ["", "   ", str(a)]})
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_duplicates_keep_the_first_position(self):
        a, b = self.folder("a"), self.folder("b")
        self.write({"archives": [str(a), str(b), str(a)]})
        self.assertEqual(self.config.load(), [a.resolve(), b.resolve()])

    def test_the_same_folder_written_two_ways_is_one_entry(self):
        """``/a/b`` and ``/a/./b`` are the same folder."""
        a = self.folder("a")
        self.write({"archives": [str(a), str(a / "." / "")]})
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_surrounding_whitespace_is_stripped(self):
        a = self.folder("a")
        self.write({"archives": [f"  {a}  "]})
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_an_absurdly_long_entry_is_dropped(self):
        a = self.folder("a")
        self.write({"archives": ["x" * 10_000, str(a)]})
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_an_unknown_version_is_still_read(self):
        """Forward compatibility: better to load extra keys than to lose the list."""
        a = self.folder("a")
        self.write({"version": 99, "archives": [str(a)], "future": "field"})
        self.assertEqual(self.config.load(), [a.resolve()])

    def test_a_home_relative_entry_is_expanded(self):
        self.write({"archives": ["~/somewhere"]})
        loaded = self.config.load()
        self.assertEqual(len(loaded), 1)
        self.assertFalse(str(loaded[0]).startswith("~"))


class UnwritableTests(ConfigCase):
    def test_an_unwritable_location_does_not_raise(self):
        """Losing the preference must not cost the user their window."""

        class Broken(Config):
            @property
            def path(self):  # type: ignore[override]
                raise OSError("no")

        # A path whose parent is a regular file cannot be created.
        blocker = self.root / "blocker"
        blocker.write_text("in the way")
        Config(blocker / "sub" / "archives.json").save([self.folder("a")])

    def test_saving_over_a_directory_is_survivable(self):
        self.path.parent.mkdir(parents=True)
        self.path.mkdir()
        self.config.save([self.folder("a")])
        self.assertEqual(self.config.load(), [])

    def test_writable_reports_a_bad_location(self):
        blocker = self.root / "blocker"
        blocker.write_text("in the way")
        self.assertFalse(Config(blocker / "sub").writable())

    def test_writable_reports_a_good_location(self):
        self.assertTrue(self.config.writable())


class DefaultPathTests(unittest.TestCase):
    def test_the_default_path_ends_in_the_right_file(self):
        self.assertEqual(default_path().name, "archives.json")

    def test_the_default_path_names_the_app(self):
        self.assertIn("WaveSnipper", str(default_path()))

    def test_xdg_is_honoured_when_set(self):
        """The convention is the variable, not a guess about the OS."""
        previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg-probe"
        try:
            if os.name != "nt":
                self.assertEqual(
                    default_path(), Path("/tmp/xdg-probe/WaveSnipper/archives.json")
                )
        finally:
            if previous is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
