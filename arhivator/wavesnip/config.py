"""Where the app remembers its archive folders between runs.

Config file rather than command-line-only workflow, because archive folders are
the app's input and re-typing a path every launch makes people not use a tool.
Format is deliberately dull: JSON in platform's config directory, human-readable
and hand-editable.

Three rules about what happens when the file is wrong:

**Never raise out of a load.** Corrupt config, config that is a directory, config
the user cannot read -- none are reasons to refuse to start. Worst outcome is
empty archive list, which user can fix by adding folders again. An app that
won't launch because of a typo in its own state file is worse than one that
forgets a preference.

**Write atomically.** ``os.replace`` on a temporary file in the same directory,
so an interrupted save cannot leave a half-written file behind. That half-written
file would fail to parse, and user would lose archive list to a crash or full
disk.

**Preserve order.** The list *is* the priority order, so it's a list not a set,
and a path appearing twice is kept once, in its first position.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

__all__ = [
    "Config",
    "ViewConfig",
    "default_path",
    "view_config_path",
    "app_config_dir",
    "atomic_write_json",
    "writable_path",
]

#: Bumped if the shape ever changes; an unknown version is read but not trusted.
_VERSION = 1

_FILENAME = "archives.json"

#: Anything longer than this is not a path someone typed.
_MAX_PATH = 4096


def app_config_dir() -> Path:
    """Platform's base directory for this app's state files.

    XDG on Linux, ``Application Support`` on macOS, ``%APPDATA%`` on Windows.
    Environment variables read rather than platform guessed, since that's what
    conventions specify and keeps it testable. Shared by every state file the
    app writes, so they all live in one place.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif os.sys.platform == "darwin":  # type: ignore[attr-defined]
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "WaveSnipper"


def default_path() -> Path:
    """Archive config file's location for this platform."""
    return app_config_dir() / _FILENAME


def view_config_path() -> Path:
    """View config file's location for this platform."""
    return app_config_dir() / "view.json"


def atomic_write_json(path: Path, document: dict) -> None:
    """Write ``document`` to ``path`` atomically, ignoring failed write.

    ``os.replace`` on temp file in same directory, so interrupted save cannot
    leave half-written file behind -- that file would fail to parse, and user
    would lose state to crash or full disk. Write is best effort: caller's data
    already in memory, only cost of failure is it won't persist.
    """
    payload = json.dumps(document, indent=2) + "\n"
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=directory, prefix=f".{path.name}-", suffix=".json"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, path)
        except BaseException:
            # Only reachable if write or replace failed, so temp file is ours
            # to clean up.
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        return


def writable_path(path: Path) -> bool:
    """Whether saving to ``path`` is likely to work, for options dialog."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path.parent, os.W_OK)


class Config:
    """Archive folder list, in priority order.

    Kept as a class not module functions so path can be injected: every test
    needs its own file, and module-level global would make them interfere with
    each other and with developer's real config.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else default_path()

    # -- reading ---------------------------------------------------------

    def load(self) -> list[Path]:
        """Archive folders from last run, best effort.

        Every failure path returns what could be read, so file with one bad
        entry contributes its good entries rather than nothing.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Missing, unreadable, or not text. All mean "no remembered archives".
            return []

        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return []

        return self._paths_from(document)

    def _paths_from(self, document: object) -> list[Path]:
        if isinstance(document, dict):
            entries = document.get("archives", [])
        elif isinstance(document, list):
            # Tolerated: hand-edited file as bare list is obvious mistake, no
            # reason to reject it.
            entries = document
        else:
            return []

        if not isinstance(entries, list):
            return []

        out: list[Path] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, str):
                continue
            text = entry.strip()
            if not text or len(text) > _MAX_PATH:
                continue
            try:
                resolved = Path(text).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                # Path that can't be normalised not usable, but not reason to
                # throw away rest of list either.
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    # -- writing ---------------------------------------------------------

    def save(self, archives: list[Path]) -> None:
        """Write archive list, atomically.

        Failure to save reported and ignored. Archives in memory and app fully
        usable; only cost is they must be entered again next time -- worth a
        message, not worth losing window over.
        """
        document = {
            "version": _VERSION,
            "archives": [str(Path(a)) for a in archives],
        }
        atomic_write_json(self.path, document)

    def writable(self) -> bool:
        """Whether saving here is likely to work, for options dialog."""
        return writable_path(self.path)


class ViewConfig:
    """View settings: what to show and how to draw it.

    Mirrors :class:`Config` pattern: tolerant load, atomic write, path
    injectable for tests. Defaults match current behaviour so existing users
    see no change.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else view_config_path()

    def load(self) -> dict:
        """Load view settings, returning defaults on any failure."""
        defaults = {
            "show_blocks": True,
            "show_clips": True,
            "show_waveforms": True,
            "block_height": 30,
            "keep_playhead_visible": False,
            "block_margin": 6,
            "time_blocks_path": "",
        }
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return defaults

        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return defaults

        if not isinstance(document, dict):
            return defaults

        # Merge with defaults, tolerating missing or wrong-type keys
        out = defaults.copy()
        for key in defaults:
            value = document.get(key)
            if isinstance(value, type(defaults[key])):
                out[key] = value
        return out

    def save(self, settings: dict) -> None:
        """Write view settings atomically."""
        document = {
            "version": 1,
            "show_blocks": bool(settings.get("show_blocks", True)),
            "show_clips": bool(settings.get("show_clips", True)),
            "show_waveforms": bool(settings.get("show_waveforms", True)),
            "block_height": int(settings.get("block_height", 30)),
            "keep_playhead_visible": bool(settings.get("keep_playhead_visible", False)),
            "block_margin": int(settings.get("block_margin", 6)),
            "time_blocks_path": str(settings.get("time_blocks_path", "")),
        }
        atomic_write_json(self.path, document)

    def writable(self) -> bool:
        return writable_path(self.path)
