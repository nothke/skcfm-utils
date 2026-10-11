# Todo

## Open

Nothing blocking. The redesign is finished and verified; what follows are known limits, not outstanding work.

Loose ends worth knowing about:

- `run.bat` is **not executed** — no Windows on this machine. It is kept because a Windows user needs a zero-setup launcher and it cannot be verified here, and it mirrors `run.sh`, which is exercised. Anything Windows-specific in it is unconfirmed.

- **The first dense wide view on a cold session pays for itself.** With the per-file cache, a day/week of files is swept sequentially once; on a spinning disk a dense 48 kHz week's first paint is ~7 min before it becomes instant. If that ever stings, a coarser pre-pass or progress feedback are the levers. A 96 kHz archive also keeps a thin [4.2, 6.7]-minute probe band (`MAX_EXACT_FRAMES` covers past the cache gate at 48 kHz, not at 96 kHz).

- **Native local time, DST unhandled.** A clip recorded across a DST change is placed by naive local time and will be an hour out. Documented rather than handled, because the fix is a per-archive timezone and nobody has said which.

- **Playback timings from this machine are not representative** — the sink is null here, so the clock advances about 2.6× realtime.

- **`Options…` is now the only way to remove an archive.** The main window has no archive list at all, which is the redesign working as intended, but it does mean a folder cannot be dropped from the top row.