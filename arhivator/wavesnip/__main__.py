"""Entry point for ``python -m wavesnip``.

Kept thin on purpose: it resolves a platform-appropriate Qt style, works out which
folders the user asked for, builds the window and runs the event loop. Everything
worth testing lives in :mod:`wavesnip.mainwindow` and below, so that a failure
here can only mean "Qt would not start", which is worth a clear message rather
than a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon


def folders_from_argv(argv: list[str]) -> list[Path]:
    """Folders named on the command line, in the order given.

    Anything starting with ``-`` is an option, not a folder. A bare value that is
    *not* a directory is deliberately kept rather than filtered out here: a
    mistyped path should reach the scanner and be reported as broken, not
    disappear silently and leave an apparently empty archive.
    """
    return [Path(arg) for arg in argv[1:] if not arg.startswith("-")]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print(f"PySide6 is not installed: {exc}", file=sys.stderr)
        print("Run ./run.sh (or run.bat) to set up the environment.", file=sys.stderr)
        return 1

    try:
        from wavesnip.config import Config
        from wavesnip.mainwindow import MainWindow
    except ImportError as exc:
        print(f"Arhivator could not load: {exc}", file=sys.stderr)
        return 1

    app = QApplication(argv)
    app.setApplicationName("Arhivator")
    app.setWindowIcon(QIcon(str(Path(__file__).parent.parent / "arhivator_icon.ico")))
    # Fusion renders the same on every platform, so the waveform view looks the
    # same on Linux and Windows rather than inheriting two different themes.
    app.setStyle("Fusion")

    window = MainWindow(config=Config())

    # Remembered folders first, then anything named on the command line layered on
    # top of them, which the user almost always wants to win.
    #
    # `add_archive` re-indexes and rescans, so asking again here would start a
    # second scan of the same folders. Only scan when the command line named
    # nothing, i.e. when nothing has already triggered one.
    remembered = window.load_state()
    added = [folder for folder in folders_from_argv(argv)]
    for folder in added:
        window.add_archive(folder)

    if not added:
        if remembered:
            window.rescan()
        else:
            window.status.showMessage("No archive folders. Open Options… to add one.")

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())