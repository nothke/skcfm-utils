"""Archive scanning and per-archive SQLite index.

Index built eagerly and completely; everything downstream is a pure lookup.
Nothing else stats an archive file.

One SQLite file per archive folder (``wavesnip-index.sqlite3``), so removing an
archive takes its index with it. See ``flow.md`` for why SQLite not JSON, and
why a ``start_us``-indexed row per clip is the right substrate for the calendar
visualisation planned later.

Scan strategy: ``stat`` every file, only read WAV header when cached
``(size, mtime)`` no longer matches. First run parses everything; later runs
parse only new or changed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from wavesnip import wavio
from wavesnip.model import (
    Archive,
    Clip,
    INDEX_FILENAME,
    mtime_us,
)

# Extensions we will even attempt to parse. Everything else in the folder is
# ignored outright, which keeps sync droppings and editor temporaries cheap.
_AUDIO_SUFFIXES = {".wav", ".wave"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    path       TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    mtime_us   INTEGER NOT NULL,
    start_us   INTEGER NOT NULL,
    nframes    INTEGER NOT NULL,
    framerate  INTEGER NOT NULL,
    nchannels  INTEGER NOT NULL,
    sampwidth  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_start_us ON clips (start_us);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = (
    "path, size, mtime_us, start_us, nframes, framerate, nchannels, sampwidth"
)


@dataclass
class ScanReport:
    """What a scan found. Surfaced in the UI rather than logged silently."""

    indexed: int = 0
    reused: int = 0
    removed: int = 0
    skipped: int = 0
    overlaps: int = 0

    @property
    def total(self) -> int:
        return self.indexed + self.reused

    def summary(self) -> str:
        parts = [f"indexed {self.indexed + self.reused} clips"]
        if self.indexed:
            parts.append(f"{self.indexed} new")
        if self.reused:
            parts.append(f"{self.reused} cached")
        if self.overlaps:
            parts.append(f"{self.overlaps} overlaps")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.removed:
            parts.append(f"{self.removed} removed")
        return ", ".join(parts)


def _duration_us(nframes: int, framerate: int) -> int:
    """Frame count -> microseconds, rounded half-up.

    Exact to within a microsecond for any real sample rate, far below one frame.
    See note on ``mtime_us`` in model.py.
    """
    return (nframes * 1_000_000 + framerate // 2) // framerate


def missing_folder(path: Path) -> str:
    """The one message for an archive folder that is not usable.

    A folder can be missing due to typo, unmounted share, or unplugged drive.
    Worth distinguishing: non-existent path vs not-a-directory are different
    mistakes.
    """
    if path.exists():
        return f"not a folder: {path}"
    return f"archive folder not found: {path}"


def count_overlaps(clips: list[Clip]) -> int:
    """Number of adjacent pairs that share an instant.

    Clips must be sorted by ``start_us``. Touching clips don't count -- see
    ``Clip.overlaps``.
    """
    overlaps = 0
    for earlier, later in zip(clips, clips[1:]):
        if earlier.overlaps(later):
            overlaps += 1
    return overlaps


class ArchiveIndex:
    """SQLite index for a single archive folder."""

    def __init__(self, archive: Archive):
        self.archive = archive

    @property
    def path(self) -> Path:
        return self.archive.index_path

    def require_folder(self) -> Path:
        """The archive folder, or a clear error if not usable.

        Kept separate from :meth:`has_index` -- the two states mean opposite
        things and conflating them hides a real problem:

        - *no such folder* is an error -- typo, or share not mounted
        - *a folder with no index yet* is normal -- the scan about to build it
          may still be running

        Reading a missing folder as "no index yet" made a mistyped command-line
        argument show up as an app with no audio and no error.
        """
        folder = self.archive.path
        if not folder.is_dir():
            raise FileNotFoundError(missing_folder(folder))
        return folder

    def connect(self) -> sqlite3.Connection:
        """Open the index, creating file and schema if not there.

        Only for writing. A read goes through ``load_clips``/``clips_containing``,
        which don't create the file -- an index appearing in every archive folder
        merely *looked at* is litter, and sqlite3's ``unable to open database
        file`` for a missing folder says nothing about what is wrong.
        """
        self.require_folder()
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # -- reading ---------------------------------------------------------

    def has_index(self) -> bool:
        """Whether this folder has ever been scanned.

        False for a folder that doesn't exist either, so only meaningful once
        :meth:`require_folder` has passed.
        """
        return self.path.exists()

    def load_clips(self) -> list[Clip]:
        """Every clip, sorted by start time.

        Empty for folder not scanned yet, error for folder not there. Notably
        does *not* create the index file -- see :meth:`connect`.
        """
        self.require_folder()
        if not self.has_index():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM clips ORDER BY start_us"
            ).fetchall()
        return [self._to_clip(row) for row in rows]

    def _to_clip(self, row: sqlite3.Row) -> Clip:
        return Clip(
            archive_path=self.archive.path,
            path=Path(row["path"]),
            size=row["size"],
            mtime_us=row["mtime_us"],
            start_us=row["start_us"],
            nframes=row["nframes"],
            framerate=row["framerate"],
            nchannels=row["nchannels"],
            sampwidth=row["sampwidth"],
        )

    def clips_containing(self, micros: int) -> list[Clip]:
        """Clips whose ``[start, end)`` contains ``micros``.

        Half-open on both ends, matching ``Clip.contains``, so a butt-joined
        seam yields exactly one clip rather than two.
        """
        self.require_folder()
        if not self.has_index():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM clips "
                "WHERE start_us <= ? AND mtime_us > ? ORDER BY start_us",
                (micros, micros),
            ).fetchall()
        return [self._to_clip(row) for row in rows]

    # -- writing ---------------------------------------------------------

    def scan(self) -> ScanReport:
        """Rescan the folder and update the index. Cheap when little changed."""
        report = ScanReport()
        folder = self.require_folder()

        with self.connect() as conn:
            cached = {
                row["path"]: row
                for row in conn.execute(f"SELECT {_COLUMNS} FROM clips")
            }

            seen: set[str] = set()
            fresh: list[Clip] = []

            for wav_path in sorted(folder.iterdir()):
                if not wav_path.is_file():
                    continue
                if wav_path.name == INDEX_FILENAME:
                    continue
                if wav_path.suffix.lower() not in _AUDIO_SUFFIXES:
                    continue

                seen.add(str(wav_path))
                try:
                    stat_result = wav_path.stat()
                except OSError:
                    report.skipped += 1
                    continue

                row = cached.get(str(wav_path))
                if (
                    row is not None
                    and row["size"] == stat_result.st_size
                    and row["mtime_us"] == mtime_us(stat_result)
                ):
                    # Unchanged: reuse without opening the file.
                    fresh.append(self._to_clip(row))
                    report.reused += 1
                    continue

                clip = self._parse(wav_path, stat_result.st_size)
                if clip is None:
                    report.skipped += 1
                    continue
                fresh.append(clip)
                report.indexed += 1

            fresh.sort(key=lambda c: c.start_us)
            conn.executemany(
                "INSERT OR REPLACE INTO clips "
                f"({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(c.path), c.size, c.mtime_us, c.start_us,
                        c.nframes, c.framerate, c.nchannels, c.sampwidth,
                    )
                    for c in fresh
                ],
            )

            # Prune anything that vanished since the last scan.
            stale = [p for p in cached if p not in seen]
            conn.executemany("DELETE FROM clips WHERE path = ?", [(p,) for p in stale])
            report.removed = len(stale)

        report.overlaps = count_overlaps(fresh)
        return report

    def _parse(self, wav_path: Path, size: int) -> Clip | None:
        """Build a Clip from a WAV header, or None if not usable.

        Partial/corrupt file skipped rather than raising: interrupted recording
        leaves exactly that, and one bad file shouldn't stop an index build.
        Count is reported, never silent.
        """
        try:
            info = wavio.read_info(wav_path)
            stat_result = wav_path.stat()
        except (wavio.WavError, OSError, EOFError):
            return None

        if info.nframes <= 0 or info.framerate <= 0:
            return None  # zero-length, e.g. an interrupted recording

        end = mtime_us(stat_result)
        return Clip(
            archive_path=self.archive.path,
            path=wav_path,
            size=size,
            mtime_us=end,
            start_us=end - _duration_us(info.nframes, info.framerate),
            nframes=info.nframes,
            framerate=info.framerate,
            nchannels=info.nchannels,
            sampwidth=info.sampwidth,
        )
