"""Core value types shared across the app.

Time is handled as naive local ``datetime``, matching how the archive is recorded.
Internally, instants are integer microseconds since the UTC epoch (``*_us``) for
exact gap/overlap detection. The ``to_us``/``from_us`` pair uses the platform's
``localtime()``, so dates outside the supported range (year 1 to year 10000)
raise -- nothing in this app handles such extremes.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from wavesnip.config import app_config_dir, atomic_write_json, writable_path
from wavesnip.datetimes import format_datetime_ms, parse_datetime

INDEX_FILENAME = "wavesnip-index.sqlite3"
TIME_BLOCKS_FILENAME = "time_blocks.json"


@dataclass(frozen=True)
class TimeBlock:
    """A radio show episode time block.

    Stored in a global time_blocks.json file, not per-archive.
    """
    name: str
    start_us: int
    end_us: int
    episode_num: int
    description: str
    hosts: list[str]
    guests: list[str]
    uuid: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    @property
    def start(self) -> datetime:
        return from_us(self.start_us)

    @property
    def end(self) -> datetime:
        return from_us(self.end_us)

    @property
    def color(self) -> tuple[int, int, int, int]:
        """Pastel color derived from name hash: (r, g, b, alpha)."""
        # Use Python's built-in hash, but make it deterministic across runs
        h = hash(self.name.lower()) if self.name else 0
        # Convert to hue in 0-360 range
        hue = (h % 360) / 360.0
        # Pastel: high saturation (0.3), high value (0.95), medium alpha
        import colorsys
        r, g, b = colorsys.hsv_to_rgb(hue, 0.35, 0.95)
        return (int(r * 255), int(g * 255), int(b * 255), 180)

    @property
    def edge_color(self) -> tuple[int, int, int, int]:
        """Darker version for edges."""
        r, g, b, a = self.color
        import colorsys
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        r, g, b = colorsys.hsv_to_rgb(h, min(s * 1.5, 0.6), max(v * 0.7, 0.6))
        return (int(r * 255), int(g * 255), int(b * 255), 220)

    def contains(self, micros: int) -> bool:
        """Is this instant inside the time block? Half-open [start, end)."""
        return self.start_us <= micros < self.end_us

    def overlaps(self, other: "TimeBlock") -> bool:
        """Does this time block share any instant with other? Touching does not overlap."""
        return self.start_us < other.end_us and other.start_us < self.end_us

_US_PER_SECOND = 1_000_000


def to_us(moment: datetime) -> int:
    """Naive **local** datetime -> integer microseconds since the UTC epoch.

    The epoch here is a true UTC epoch, because that is what ``st_mtime`` is.
    The two must agree: an earlier version subtracted naive datetimes as if
    1970-01-01 were a local wall clock, which put every timestamp 2 hours out
    on a UTC+2 machine -- and silently, because nothing else disagreed.

    Naive input is interpreted as local time (the archive's own convention).
    Aware input is honoured as-is. The result is split into whole seconds plus
    a microsecond remainder rather than scaled from a float, because a float
    near 1.8e9 cannot hold microseconds and the round trip has to be exact --
    ``from_us(to_us(m)) == m``.
    """
    if moment.tzinfo is None:
        stamp = moment.timestamp()  # naive -> local, by definition
    else:
        stamp = moment.timestamp()

    whole = int(stamp // 1)
    return whole * _US_PER_SECOND + round((stamp - whole) * _US_PER_SECOND)


def from_us(micros: int) -> datetime:
    """Integer microseconds since the UTC epoch -> naive local datetime."""
    seconds, remainder = divmod(micros, _US_PER_SECOND)
    return datetime.fromtimestamp(seconds) + timedelta(microseconds=remainder)


def us_now() -> int:
    """Current local time as integer microseconds since the epoch."""
    return to_us(datetime.now())


def mtime_us(stat_result: os.stat_result) -> int:
    """File mtime as integer microseconds since the UTC epoch.

    Uses the same whole-seconds-plus-remainder split as ``to_us`` so the two
    agree exactly on a given file. ``st_mtime`` is a float with roughly 0.1 us
    of noise, which is orders of magnitude below one audio frame, so rounding
    here is safe.
    """
    return to_us(datetime.fromtimestamp(stat_result.st_mtime))


@dataclass(frozen=True)
class Clip:
    """One archive file, and the time span it covers.

    ``end`` is the file's mtime -- the assumed moment recording stopped.
    ``start`` is derived as ``end - nframes / framerate`` and is *exact* for
    WAV. That derivation is the foundation of the whole tool; see ``flow.md``.
    """

    archive_path: Path
    path: Path
    size: int
    mtime_us: int
    start_us: int
    nframes: int
    framerate: int
    nchannels: int
    sampwidth: int

    @property
    def end_us(self) -> int:
        return self.mtime_us

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    @property
    def duration(self) -> float:
        return self.duration_us / _US_PER_SECOND

    @property
    def start(self) -> datetime:
        return from_us(self.start_us)

    @property
    def end(self) -> datetime:
        return from_us(self.end_us)

    @property
    def block_align(self) -> int:
        return self.nchannels * self.sampwidth

    def contains(self, micros: int) -> bool:
        """Is this instant inside the clip?

        Half-open ``[start, end)`` so that adjacent chunks which butt-join do
        not both claim the same instant. That matters: with half-open
        intervals a seam between two chunks resolves to exactly one clip,
        which is what per-instant resolution in ``timeline.py`` relies on.
        """
        return self.start_us <= micros < self.end_us

    def overlaps(self, other: "Clip") -> bool:
        """Does this clip share any instant with ``other``?

        Touching clips do *not* overlap -- see ``contains``.
        """
        return self.start_us < other.end_us and other.start_us < self.end_us

    def frame_at(self, micros: int) -> int:
        """Frame index for an instant, clamped to ``[0, nframes]``.

        A request outside the clip gives the nearest real frame rather than
        an out-of-range index, so callers never have to bounds-check.
        """
        if micros <= self.start_us:
            return 0
        if micros >= self.end_us:
            return self.nframes
        return min(max((micros - self.start_us) * self.framerate // _US_PER_SECOND, 0), self.nframes)

    def frame_span(self, from_instant: int, to_instant: int) -> tuple[int, int]:
        """Half-open frame range covering ``[from_instant, to_instant)``.

        Clamped, and normalised: a reversed or degenerate range yields
        ``(0, 0)`` rather than a negative-length slice.
        """
        if to_instant <= from_instant:
            return 0, 0
        start = self.frame_at(from_instant)
        end = self.frame_at(to_instant)
        return (start, end) if end > start else (0, 0)


@dataclass(frozen=True)
class Archive:
    """An archive folder, plus the index kept inside it.

    Each archive owns its own SQLite file, so removing an archive from settings
    takes its index with it and no cleanup is needed. See ``flow.md``.
    """

    path: Path
    priority: int = 0

    @property
    def index_path(self) -> Path:
        return self.path / INDEX_FILENAME

    @property
    def name(self) -> str:
        return str(self.path)


def _timeblocks_config_path() -> Path:
    """Global time_blocks.json location for this platform."""
    return app_config_dir() / TIME_BLOCKS_FILENAME


def _optional_int(value: object, default: int = 0) -> int:
    """``episode_num`` or whatever it is worth -- never an exception.

    The field is optional, so "cannot be read" means "use the default", not
    "refuse the block it belongs to".
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _optional_text(value: object) -> str:
    """``description``, defaulting it to empty when absent or not text."""
    return value if isinstance(value, str) else ""


def _optional_names(value: object) -> list[str]:
    """``hosts``/``guests`` as a list of non-empty strings, always.

    A bare string is one name, not an iterable of characters -- the field is
    written by hand in ``time_blocks.json``, and ``"Alice"`` is a host.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    return []


class TimeBlocksConfig:
    """Global time blocks stored in time_blocks.json.

    Mirrors :class:`Config` pattern: tolerant load, atomic write, path injectable for tests.

    **An entry needs three fields and nothing else.** ``name``, ``start_time``
    and ``end_time`` are what it takes to put a block on the timeline;
    ``episode_num``, ``description``, ``hosts`` and ``guests`` are optional and
    take their defaults when absent -- and :meth:`save` writes them only when
    they hold something, so a file does not fill up with empty parameters.

    **One bad entry costs only that entry.** Each is read on its own, and a
    failure becomes a line in the list :meth:`load_with_report` returns rather
    than an exception. That exception is what used to happen: a missing or
    unparseable ``start_time`` raised ``AttributeError`` out of ``load``, so a
    single broken line took every block -- and the window loading them -- down
    with it.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _timeblocks_config_path()

    def load(self) -> list[TimeBlock]:
        """Load time blocks, best effort.

        Use :meth:`load_with_report` when the skipped entries should be said
        out loud; this is the caller that only wants the blocks.
        """
        blocks, _problems = self.load_with_report()
        return blocks

    def load_with_report(self) -> tuple[list[TimeBlock], list[str]]:
        """Load, and say what could not be loaded.

        Returns ``(blocks, problems)``, one problem per line, each naming the
        file and the entry so it can go verbatim to the console and the status
        bar. A file that does not exist yet is *not* a problem -- that is every
        first run -- so it is an empty list and no message.
        """
        problems: list[str] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], problems
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{self.path.name}: cannot read it ({exc})")
            return [], problems

        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(
                f"{self.path.name}: not valid JSON ({exc}); no time blocks loaded"
            )
            return [], problems

        entries = self._entries_of(document)
        if entries is None:
            problems.append(
                f"{self.path.name}: expected a list of entries, or an object with a "
                "'time_blocks' list; no time blocks loaded"
            )
            return [], problems

        blocks: list[TimeBlock] = []
        for index, entry in enumerate(entries, start=1):
            try:
                block, problem = self._block_from(entry, index)
            except Exception as exc:  # noqa: BLE001 -- one entry must never take the file down
                block = None
                problem = f"entry {index} could not be read ({type(exc).__name__}: {exc})"
            if block is None:
                problems.append(f"{self.path.name}: {problem}")
                continue
            blocks.append(block)
        return blocks, problems

    @staticmethod
    def _entries_of(document: object) -> list | None:
        """The entry list, or ``None`` when the document is the wrong shape."""
        if isinstance(document, dict):
            document = document.get("time_blocks", [])
        if not isinstance(document, list):
            return None
        return document

    @classmethod
    def _block_from(
        cls, entry: object, index: int
    ) -> tuple[TimeBlock | None, str | None]:
        """One entry -> one block, or the reason it could not be one.

        The reason is written for a person: it is shown as it stands, so it
        names the entry and says what would make it valid.
        """
        where = f"entry {index}"
        if not isinstance(entry, dict):
            return None, f"{where} is not an object"

        name = entry.get("name")
        if isinstance(name, str):
            name = name.strip()
        elif name is None:
            return None, f"{where} has no name"
        else:
            return None, f"{where} has a name that is not text: {name!r}"
        if not name:
            return None, f"{where} has no name"
        where = f'{where} "{name}"'

        start_us, problem = cls._instant(entry, "start_time", where)
        if start_us is None:
            return None, problem
        end_us, problem = cls._instant(entry, "end_time", where)
        if end_us is None:
            return None, problem

        # Optional from here on: an entry that carries only the three required
        # fields loads with every one of these at its default.
        return (
            TimeBlock(
                name=name,
                start_us=start_us,
                end_us=end_us,
                episode_num=_optional_int(entry.get("episode_num")),
                description=_optional_text(entry.get("description")),
                hosts=_optional_names(entry.get("hosts")),
                guests=_optional_names(entry.get("guests")),
                uuid=_optional_text(entry.get("uuid")) or uuid.uuid4().hex[:16],
            ),
            None,
        )

    @staticmethod
    def _instant(entry: dict, key: str, where: str) -> tuple[int | None, str | None]:
        """``key`` of one entry as microseconds, or why it is unusable."""
        raw = entry.get(key)
        if raw is None:
            return None, f"{where} has no {key}"
        if not isinstance(raw, str):
            return None, f"{where} has a {key} that is not text: {raw!r}"

        text = raw.strip()
        if not text:
            return None, f"{where} has no {key}"
        moment = parse_datetime(text)
        if moment is None:
            return None, (
                f"{where} has an unreadable {key} {text!r} "
                "(expected a date like 12.5.2026. 10:15:01:001)"
            )
        try:
            return to_us(moment), None
        except (ValueError, OSError, OverflowError) as exc:
            # Well-shaped but outside what localtime() can reach: year 1, year
            # 10000. Nothing downstream can place such a block either.
            return None, f"{where} has a {key} {text!r} outside the supported range ({exc})"

    @staticmethod
    def _entry_for(block: TimeBlock) -> dict:
        """One block as a JSON entry.

        Only the three required fields are unconditional. The rest are written
        when they hold something, so saving a block does not re-add the empty
        parameters a previous save -- or a hand -- had left out.
        """
        entry: dict[str, object] = {
            "name": block.name,
            "start_time": format_datetime_ms(block.start),
            "end_time": format_datetime_ms(block.end),
            "uuid": block.uuid,
        }
        if block.episode_num:
            entry["episode_num"] = block.episode_num
        if block.description:
            entry["description"] = block.description
        if block.hosts:
            entry["hosts"] = list(block.hosts)
        if block.guests:
            entry["guests"] = list(block.guests)
        return entry

    def save(self, blocks: list[TimeBlock]) -> None:
        """Write time blocks atomically."""
        document = {
            "version": 1,
            "time_blocks": [self._entry_for(b) for b in blocks],
        }
        atomic_write_json(self.path, document)

    def writable(self) -> bool:
        """Whether saving here is likely to work."""
        return writable_path(self.path)
