"""Long-running audio work, kept off the event loop.

Reading a peak envelope means decoding gigabytes of PCM, and rendering a snip
means decoding it again and writing a new file. Both are seconds-to-minutes on
a cold cache, which is long enough that doing them inline freezes the window --
including the play button user is trying to reach for.

So every such job runs on a :class:`QThread` via :class:`Worker`, a
deliberately dumb adapter: one callable in, one result out, never two jobs at
once. Window keeps the plan it submitted rather than job's mutable state, so a
rescan mid-read cannot leave UI showing peaks for a range it has since left.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

__all__ = ["Worker", "BackgroundJob"]


class Worker(QObject):
    """Runs one callable on a thread and reports outcome once.

    Signals carry ``object`` rather than a builtin type for same reason player's
    do: Qt would map narrower type onto C++ signature and truncate values like
    microsecond timestamps.
    """

    finished = Signal(object)  # result, or None
    failed = Signal(str)  # message
    done = Signal()  # always emitted last, so UI can re-enable itself

    def __init__(self, job: Callable[[], object], parent: QObject | None = None):
        super().__init__(parent)
        self._job = job

    def run(self) -> None:
        try:
            result = self._job()
        except Exception as exc:  # noqa: BLE001 - UI needs the message
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished.emit(result)
        finally:
            self.done.emit()


class BackgroundJob(QObject):
    """A :class:`Worker` on its own :class:`QThread`, with plumbing done.

    Restarting a job while one running is refused rather than queued. UI only
    ever has one thing it wants computed -- peaks for range currently on
    screen -- so a queue would mean rendering ranges user has already scrolled
    past.
    """

    finished = Signal(object)
    failed = Signal(str)
    done = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, job: Callable[[], object]) -> bool:
        """Run ``job`` on a worker thread. False if one already running."""
        if self.busy:
            return False

        thread = QThread()
        worker = Worker(job)
        worker.moveToThread(thread)

        worker.finished.connect(self.finished)
        worker.failed.connect(self.failed)
        worker.done.connect(self._on_done)
        thread.started.connect(worker.run)

        # Clearing on finish, not on deleteLater: thread may still be unwinding
        # when `done` arrives, and deleting it there would destroy a running
        # QThread.
        self._thread = thread
        self._worker = worker
        thread.start()
        return True

    def _on_done(self) -> None:
        self.done.emit()
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._worker = None
        if thread.isRunning():
            thread.quit()
        thread.wait(5000)
        thread.deleteLater()

    def shutdown(self) -> None:
        """Wait for running job, if any. For use on window close."""
        thread = self._thread
        if thread is None:
            return
        if thread.isRunning():
            thread.quit()
            thread.wait(5000)
        self._thread = None
        self._worker = None