"""Show a file in the system file manager.

"Open the folder" and "show me *this* file" are different requests, and only
the second is any use when the question is "which of these forty hourly wavs
was that?" -- a folder full of near-identical names is exactly the case where
being taken to the file, selected, is the whole point. Every platform can be
told to do that, but each in its own language, and one cannot be told at all.

Which is why this is three commands rather than one:

* macOS asks ``open -R``;
* Windows asks ``explorer /select,``;
* Linux has no portable answer. ``org.freedesktop.FileManager1.ShowItems`` is
  closest thing to a standard and implemented by mainstream file managers, but
  arrives over D-Bus and simply not there on minimal system -- so when
  ``dbus-send`` missing request degrades to opening containing folder. It
  degrades to a *folder* and not the file, because handing file itself to
  ``xdg-open`` would open it in audio editor, and user asked to be shown it,
  not to have it played.

``reveal_command`` exists separately from ``reveal`` precisely so all three
branches can be asserted without launching a file manager, and without test
passing merely because it ran on Linux.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

__all__ = ["reveal", "reveal_command"]


def reveal_command(path: Path, system: str | None = None) -> list[str]:
    """The argv that reveals ``path``, selecting it where platform can.

    ``system`` overrides :func:`platform.system` so branches for machines we're
    not running on can be tested on the one we are.
    """
    path = Path(path)
    system = system if system is not None else platform.system()

    if system == "Darwin":
        return ["open", "-R", str(path)]
    if system == "Windows":
        # A comma, not a space: explorer splits this switch itself.
        return ["explorer", f"/select,{path}"]

    if shutil.which("dbus-send"):
        return [
            "dbus-send",
            "--session",
            "--dest=org.freedesktop.FileManager1",
            "--type=method_call",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
            f"array:string:{path.resolve().as_uri()}",
            'string:""',
        ]
    return ["xdg-open", str(path.parent if path.is_file() else path)]


def reveal(path: Path) -> bool:
    """Reveal ``path`` in file manager. Reports whether anything was run.

    Path not existing yet -- ``time_blocks.json`` before first save -- reveals
    its folder instead, because folder is the thing being asked for. Only path
    with no folder either is a refusal.

    Never raises: file manager that won't start is an inconvenience, not an
    exception worth tearing down menu that asked for it.
    """
    target = Path(path)
    if not target.exists():
        target = target.parent
    if not target.exists():
        return False

    try:
        subprocess.Popen(
            reveal_command(target),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True
