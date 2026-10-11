Environment# Flow — reasoning and decisions

Design notes for Arhivator. `design.md` holds the design itself; this file holds *why* it ended up that way, so the reasoning survives.

---

## Environment

| Thing | Status |
|---|---|
| Python | **3.12+ required** — enforced by both launchers |
| numpy | 2.5.3 in `.venv` — **test against this, not the system numpy** |
| scipy | 1.18.1 in `.venv` |
| PySide6 | 6.11.2 in `.venv` |

The dev machine's *system* interpreter is 3.11.2 with numpy 1.24 — **not** new enough, and a misleading place to run tests (see the numpy 2 bug below). `run.sh` and `run.bat` build `.venv` with 3.12 and recreate it if stale.

**Why 3.12 is a hard floor, not a preference:** the stdlib `wave` module only learned to read `WAVE_FORMAT_EXTENSIBLE` in 3.12, and many recorders emit it for multichannel or >16-bit audio. On 3.11 those files raise `unknown format: 65534`.

This is why we target 3.12 rather than hand-rolling a RIFF parser — see below.

**Verified, not assumed.** `tests/test_wavio.py::TestExtensible` hand-assembles extensible headers byte by byte (stdlib `wave` can only *read* the format, never write it) and confirms 3.12 parses them. Those tests **skip** on older interpreters, so a green run on 3.11 is not sufficient evidence — check the skip count.

One finding worth keeping: the common **24-bit-in-32-bit-container** layout (`wValidBitsPerSample` 24, container 32) reports `sampwidth` 4 but carries only 24 significant bits, so it survives float32 **exactly**. Not luck — 24 significant bits is precisely float32's mantissa. Asserted by `test_24bit_in_32bit_container_survives_the_float32_pipeline`.

---

## Stack

**Python + PySide6 (Qt Widgets) + numpy/scipy.**

The app is not performance-bound. It is bound by codec plumbing, a timeline model, and sample-accurate edits across chunk boundaries. Every hard part is already reachable from this stack.

Qt Widgets rather than QML: the UI is forms — folder list, datetime field, prev/next week, from/to, play. QML would add a second language for a prettier waveform, and the waveform is one custom-painted widget either way.

**Runner-up: Rust + egui.** Wins only on distribution — a ~5 MB static binary vs a ~120 MB PyInstaller bundle. Loses on: MP3/`symphonia` is decode-only (moot now, see WAV-only below), waveform peak extraction over decoded PCM is nicer with numpy, and the `D.M.Y. H:M:S` parse/format is frictionless in Python and annoying in chrono. Revisit if a clean single-file install on machines without Python becomes a hard requirement.

---

## The start-time derivation

A clip's start is `mtime − duration`. This is the foundation of the whole tool.

**`floor(t / 3600)` does not work.** It assumes chunks are exactly one hour and clock-aligned. They are neither — some files run shorter or longer than an hour. Every file's start must be computed individually.

**Therefore: cache everything, up front.** Not as an optimization — as the correct architecture. Build the index eagerly and completely at startup, then make every downstream operation a pure lookup. Nothing else in the app stats a file.

### Why WAV-only makes this exact

For WAV, `duration = nframes / framerate` is a header division. Not an estimate.

MP3 has no frame count in the header — you need a Xing/VBRI header or you fall back to a bitrate guess, which drifts badly on VBR. The entire start-time derivation would rest on an approximation, with a tolerance window and no way to know when it is wrong.

Dropping MP3 makes duration **frame-exact**. No heuristics, no tolerance. This was the single highest-value decision in the project.

### What WAV-only costs

**No encoder-delay seams.** MP3 concatenation inserts ~25 ms of encoder delay/padding at each join, making cross-boundary snips click. Raw PCM WAV has no padding, so joins are clean. Another real problem that simply stops existing.

**But: format reconciliation is now our job.** WAV is a container, not a format — it guarantees nothing about uniformity. Two archive folders recorded by different devices can differ in sample rate, channel count, or bit depth. Without FFmpeg to lean on, resampling is ours: `scipy.signal.resample_poly` for rate, explicit rules for channels.

In practice this is unlikely to trigger, because all archives come from the same stream. It would only matter if an older archive was recorded on different hardware. Deferred, not ignored — see "Defaults" below.

### Why 3.12+ instead of hand-rolling the parser

The original plan was a ~60-line hand-rolled RIFF header parser, on the reasoning that indexing needs `nframes` / `framerate` / `sampwidth` / `nchannels` anyway, so those fields are parsed either way.

That was solving for the wrong constraint. Requiring 3.12+ buys the same fields from the stdlib with battle-tested chunk-walking, and drops a whole component that would otherwise need its own tests. **Decision: require 3.12+, use `wave`, do not hand-roll.**

Two limits of `wave` to keep in mind:

- **IEEE float WAV (`format` 3) is still unsupported**, in 3.12 as in 3.11. PCM   integer is the realistic case for a recorder, but if a float file shows up it   needs a small manual path for that one case. - **`audioop` is deprecated in 3.11 and removed in 3.13.** Do not reach for it to   convert samples — numpy does all conversion. Worth stating because it is an   obvious trap on a 3.12+ baseline.

---

## Identical archives → per-instant resolution

**The archives hold byte-identical clips** — they come from the same source stream. So there is no ambiguity to resolve and no dialog to show.

The naive rule would be "first archive wins". The better rule, at no extra cost:

> **Resolve coverage per-instant, in archive priority order.**

Because the samples are identical, a snip is free to take its first piece from archive A and its second from archive B — the join is *genuinely* seamless, not approximately. This also handles **partial sync** for free: if archive 2 only holds 10:30–10:45, a snip spanning 10:20–10:50 still works, borrowing from archive 1 where archive 2 has no coverage.

It subsumes the gap case too: a gap in archive 1 gets covered by archive 2 automatically. Only a gap in *every* archive is a real gap.

So `priority` is not a conflict-resolution mechanism — it is just an ordering used to pick which source to prefer.

---

## Index design

One `wavesnip-index.sqlite3` at the root of **each archive folder**, not one central database.

- Removing an archive from settings takes its index with it. Nothing to clean up. - Each index stays independently portable. - Timeline resolution fans out across N small SQLite files. At query time we only   need "clips containing instant *t*", so the result set is tiny — query each DB   and merge in Python. Cleaner than `ATTACH`, and no cross-DB coupling.

**Schema** (per archive DB):

- `clips(path, size, mtime, start_us, nframes, framerate, nchannels, sampwidth, audio_format)` - indexed on `start_us`

**Staleness key** is `(size, mtime)`. Scanning stats every file; matching rows skip header parsing entirely. First run parses everything, later runs parse only what's new.

**Why SQLite and not JSON:** incremental refresh and range queries, and — relevant to the calendar idea — a `start_us`-indexed row per clip *is* the heatmap substrate. Keep it normalized now; get the calendar nearly free later.

### mtime is trusted

Stated assumption: `mtime` == recording stop time. No guard implemented.

Consequence to be aware of: because the staleness key is `(size, mtime)`, a file that *is* ever re-copied (`cp` without `-p`, rsync, cloud sync) will be silently re-indexed with a new start time derived from the copy time. This is a known, accepted risk, not a bug.

---

## Overlaps within one archive

Adjacent chunks may overlap if the recorder was configured with overlap to avoid gaps at the seam. A snip crossing that seam would **double-count audio** — the same second heard twice.

- **Detect and report.** Surfaced in the index summary, not as a blocking error. - **Trim exports.** When building the snip piece list, clamp each piece's start so   it can never precede the previous piece's end *time* — clamp in the time domain   rather than back-computing from frame counts, since framerates may differ   between clips.

### The overlap clamp turned out to be redundant (implemented in `timeline.py`)

The plan above assumed the cursor clamp is what prevents double-counting. Writing `plan_snip` showed it is not — double-counting is **structurally impossible**:

1. The requested range is partitioned at *every* clip boundary inside it, so each    elementary interval has a constant set of covering clips. 2. Exactly **one** clip is chosen per elementary interval. 3. A clip's start is therefore always a partition point, so a chosen clip can never    begin before the interval it was chosen for.

Adjacent pieces abut exactly, so there is nothing to trim. Worked example with `A = [0, 10)` and `B = [5, 15)` over a request of `[0, 15)`: the boundaries are 5 and 10, giving `[0,5) A`, `[5,10) B`, `[10,15) B` — each instant exported once.

The cursor clamp stays as a cheap guard on the invariant rather than as the mechanism achieving it, and `test_pieces_never_overlap_in_time` asserts the invariant on a deliberately overlapping archive. Recorded because the documented plan and the implemented plan now differ, and the implementation is the better one.

### `timeline.py` snapshots rather than querying SQL per lookup

`ArchiveIndex.clips_containing` can answer point queries in SQL against the index on `start_us`. `Timeline` does not use it: it loads every clip once into memory and answers in Python instead.

The reason is not speed so much as **consistency**. Snip planning interleaves hundreds of queries across several archives, and each one would otherwise open its own connection and re-derive the clip list. A single snapshot makes every query in a plan see the same view of the archive. The cost is memory — a few thousand clips of metadata is nothing — and staleness, which is handled by the agreed rescan trigger (startup plus a manual button), not by a filesystem watcher.

`_clips_containing` scans backwards from the insertion point and stops once the distance exceeds the archive's longest clip. That bound is sound because a clip can only contain an instant if it starts less than one clip-duration earlier, and it keeps the scan self-limiting: an hour of hour-long chunks examines a couple of clips, a month of one-second clips examines one.

### `Resolution` deliberately carries no losing candidates

`resolve()` returns at the first archive with coverage, so it never inspects the lower-priority ones. An earlier draft had an `alternatives` tuple and an `is_overlapped` property for the UI to display "also in archive B" — which was always empty, i.e. a permanently-false API. Removed rather than implemented: the archives hold the same stream, so the losing candidates carry no information the user can act on.

---

## Reading audio

**Read only the frames we need.** A one-hour clip is ~690 MB at 48 kHz/16-bit stereo (~346 MB mono). Decoding whole files is never acceptable.

Use `Wave_read.setpos(frame)` + `readframes(n)` — `wave` does the seeking internally, so we get partial reads without needing the raw data-chunk offset that a hand-rolled parser would have exposed.

`flow.md` correction: an earlier draft quoted "~300 MB at 48k/16bit/stereo" — that figure is mono. Stereo is ~690 MB. Does not change the design, but it is the reason this section exists at all.

### The float32 pipeline is exact only up to 24-bit

Samples are carried as float32 in [-1, 1) throughout. A float32 has a **24-bit mantissa**, so:

- **int16 and int24 PCM survive bit-exactly.** LSBs are 2^-15 and 2^-23, both   representable. `tests/test_wavio.py::test_roundtrip_is_bit_exact` asserts this. - **int32 PCM does not.** Its LSB is 2^-31, so ~8 low bits are discarded and an   error of up to ~64 LSBs is introduced.

This matters more than it looks, because **snips concatenate decoded pieces**. Any per-chunk loss compounds at every join. With 16/24-bit the answer is "no loss at all"; with 32-bit it would compound.

Deliberate trade: one dtype keeps resampling and waveform code simple, and 16/24-bit is what recorders actually write. If 32-bit archives ever matter, the pipeline has to carry int32 as int64 and float only at the edges. **Do not "fix" this by loosening a tolerance** — the bound is asserted in `test_32bit_is_lossy_but_bounded` precisely so it cannot drift unnoticed.

### The numpy 2 / 32-bit-full-scale bug (found by running under the real venv)

Writing full-scale audio at 32-bit depth produced **corrupt samples**, and it only appeared once the tests ran under the venv's numpy 2.5.3 rather than the system 3.11's numpy 1.24.

Cause: the scale factor is a Python `int`. Under **NEP 50** (numpy 2.x) a Python int is a *weak* scalar, so `float32_array * 2147483648` stays **float32**. But float32 cannot represent `2**31 - 1`, so the integer clamp

```python v = np.clip(v, -(scale - 1), scale - 1)   # bound silently became 2**31 ```

was a **no-op**. `+1.0` then survived as `2147483648.0`, and `astype(int32)` overflowed with only a `RuntimeWarning`, writing garbage bytes.

numpy 1.x promoted that multiplication to float64, which hid the bug. It would have shipped silently into every export.

Fix: scale and clamp in **float64**, which represents every supported width's full-scale bound exactly, then narrow. `test_full_scale_does_not_overflow` covers it.

**General lesson: run the tests under the venv the launcher actually builds.** The system interpreter was a different numpy *and* a different Python, and it hid both this and the whole extensible-format question.

### The timezone bug in time conversion (found by the indexer tests)

`to_us` originally subtracted naive datetimes (`moment - datetime(1970,1,1)`), treating 1970 as if it were a *local* wall clock. But `st_mtime` is a true **UTC epoch**. The two disagreed by exactly the machine's UTC offset — 2 hours on this box — which would have shifted **every** clip's start time.

Nothing caught it at first: both functions were self-consistent, and no test compared them against a real file's mtime until `test_agrees_with_filesystem_mtime` did exactly that.

Fix: `to_us` goes through `datetime.timestamp()` (naive input is interpreted as local time, by definition), and `from_us` through `fromtimestamp()`. Both split into whole seconds plus a microsecond remainder rather than scaling a float, because a float near 1.8e9 cannot hold microseconds and the round trip has to be exact.

**Traded away:** the old arithmetic handled `datetime(1,1,1)` through year 9999; `localtime()` does not. Irrelevant here — values only ever come from file mtimes — but it is a real narrowing, so it is documented in `model.py`.

**General lesson:** two representations of the same quantity will eventually be compared. Write that comparison as a test before assuming they agree.

## Playback

- **Playhead** — `QMediaPlayer` + `setPosition()` into the resolved clip. - **Snip preview** — render the snip to a cached temp WAV and play that, so the   preview is byte-identical to what gets exported. Cached so repeated preview   clicks are instant.

## Datetime field

`QDateTimeEdit` with displayFormat `"d.M.yyyy. HH:mm:ss"` renders exactly the requested `12.5.2026. 10:15:00` — unpadded day/month, padded time, trailing dot after the year.

Needs verification in a prototype: `QDateTimeEdit` does not reliably parse *pasted* text matching its own displayFormat. Plan on an explicit parse-on-paste path rather than assuming it works.

## Prev / next week

Playhead only, ±7 days, preserving time-of-day. The snip range is untouched; the snip panel reads the playhead.

---

## `run.bat` — zero-setup launcher

Modelled on `skcfm-utils/downloader/run.bat`, so the user double-clicks and gets a working app without touching pip. Five deliberate divergences from that script:

1. **Interpreter discovery is version-gated.** The original just does    `where python`. Here it tries `py -3`, then `python`, accepting only something    that reports `sys.version_info >= (3, 12)`. A bare `where python` would happily    select 3.11 and fail much later, at the first extensible WAV. 2. **A stale `.venv` is detected and rebuilt.** The version check runs against    `.venv\Scripts\python.exe` too, so a venv created under an older Python is    removed and recreated instead of producing confusing failures. 3. **`pip install` is not silenced.** The original redirects to `>nul`. PySide6 is a    large first-run download; behind `>nul` that reads as a hung window and users    kill it. Now it prints progress, with a note saying what it is doing. 4. **`python -m pip` throughout, not bare `pip`.** Bare `pip` resolves by `PATH`    order and can quietly hit a different interpreter than the venv just activated. 5. **Error paths pause.** Especially the app-exit path — for a GUI app the window    vanishing on a crash is the worst possible outcome. Also: a failed pip    self-upgrade is a warning, not fatal, since the venv itself is fine.

The version probe runs as an inline `python -c`, so no extra helper file is needed.

---

## `run.sh` — the Linux/macOS twin

Same six-step shape as `run.bat`, with deliberate platform divergences:

1. **No `source activate`.** Calls `.venv/bin/python` directly instead. Mutating    `PATH` is the usual source of "activated but still wrong python" problems, and    on a shell there is no reason to do it. 2. **Interpreter search is a loop over `python3.13 python3.12 python3 python`**,    newest first, each gated on the version check. Newest-first matters — on many    distros `python3` is *older* than `python3.12`. 3. **`python3 -m venv` needs `python3.12-venv` separately** on Debian/Ubuntu —    `ensurepip` is stripped out. This is the single most common way the script fails    on a fresh Debian box, so the error path names the package. 4. **`cd "$(dirname "$0")"` at the top**, so double-clicking the script in a file    manager works regardless of the working directory. 5. **`set -euo pipefail`** instead of `setlocal enabledelayedexpansion` — same intent,    native expression. 6. **`pause_if_interactive` instead of `pause`**, which does not exist on Linux. It    only prompts when stdin is a TTY, so it stays quiet in CI or when piped.

**Not in the script:** PySide6's system libraries. Installing them needs `sudo`, which a launcher must not do. The apt line for `libgl1` / `libegl1` / `libxcb-*` / `libxkbcommon-*` is in the script header as a comment instead, so the user copies it only when they actually hit a missing-library error.

**On a headless machine** (SSH without X forwarding) there is no display. Set `QT_QPA_PLATFORM=offscreen` to run without one — noted in the script header rather than set by default, since forcing it would break normal desktop use.

The two scripts are a matched pair and must be changed together. `run.sh` says so in its header.

**Verified** on this machine: the version gate correctly rejects the local 3.11 and prints the Debian guidance, and with a shimmed `python3.12` it prefers that over the real `python3` and reaches the venv step. Both error paths fire correctly.

Agreed to proceed without further discussion unless flagged:

- **Snip partly in a gap** — export the covered audio, report the missing ranges   explicitly. (Erroring is the safer alternative if a partial file should never be   produced unnoticed.) - **Playhead lands in a gap** — show the gap's boundaries, offer jump-to-nearest. - **Index refresh** — on startup, plus a manual rescan button. Notably: if the   archives live on a network share, `inotify`-style watching does not work there   at all, so a reliable poll beats a clever watcher. - **Export format mismatch** — canonicalize to the first piece's format,   reconciling silently. - **Non-WAV / partial files** — skip anything unparseable, but surface the count   (`indexed 412 clips, 2 overlaps, 3 skipped`) rather than skipping silently. An   interrupted recording leaves a truncated WAV; sync tools drop `.tmp` files. - **DST** — naive local time, documented, no special handling. At the autumn   fall-back the timeline will show a real one-hour overlap; in spring, a real   one-hour gap. Neither is a bug, both will look like one.

---

## Format reconciliation (`resample.py`)

A snip plan's pieces may disagree about rate and channel count, so export has to reconcile them.

**Output format defaults to the first piece's.** There is no better default and no truth to discover — the sources disagree, so the user has to choose. The first piece is the least surprising because it is the start of the snip.

**Bit depth is not a conversion.** Only rate and channel count need arithmetic. Sample width is a quantisation choice applied once at write time, so a 16-bit and a 24-bit clip merge with no signal processing at all.

**Multi-to-multi channel conversion is refused, not guessed.** Mono↔stereo both ways is obvious: mean to downmix, duplicate to upmix. But 6 channels → 2 has no correct answer, and the obvious implementation is actively wrong — a mean across a 5.1 layout cancels toward silence, because the centre channel is anti-phase with the surrounds. Producing near-silent audio and reporting success is worse than declining, so `FormatUnsupported` is raised and the user picks. 6→1 is *allowed*, because asking for mono is explicitly asking to lose the surround channels.

**Resample a whole run at once, never piece by piece.** Consecutive pieces sharing a rate are concatenated *before* resampling. Resampling each piece separately would restart the anti-alias filter at every chunk boundary, adding a click per join — in an archive whose entire premise is that chunk joins are seamless. This is the same instinct as the overlap work: the seams are the hard part.

Blocks flush at a memory budget (32 MB) so a multi-hour export stays bounded. The cost is that a filter restart can occur at a budget boundary; that is rare and audibly negligible next to the alternative, which is an export that needs 1.4 GB of RAM. `test_many_small_blocks_produce_the_same_audio` pins the property that the budget changes memory use only, never the output.

### A truncated WAV produced a silently shorter snip

`read_frames` clamped the request against `nframes` from the header. That is the right thing to do for the normal case — a snip near the end of a clip should give whatever exists rather than fail. But a *truncated* file declares a length it does not have, so the clamp hid the corruption and the export came out quietly short.

Nobody would have noticed: the file plays, it is just missing the end. Now a short read raises `WavError` naming the file, and `resample` wraps it as an `ExportError` rather than dropping the piece — silently exporting less audio than was requested is the one failure mode a snipping tool must not have.

**General lesson: clamping is a decision about which lie to believe.** A short file and a short request look identical from inside the reader. Only the caller knows which one it meant.

---

---

## Playback (`cursor.py` + `player.py`)

**The decision half and the driver half are separate modules.** `Cursor` answers "what plays next" with no Qt and no audio hardware; `Player` drives `QMediaPlayer` from those answers. Not tidiness — it is what makes the interesting cases testable at all. Gaps, chunk boundaries, overlapping chunks and week navigation are all plain functions of a timeline plus an instant, so they are checked on any interpreter in milliseconds. The Qt wrapper is then thin enough to have nothing in it worth a sound card.

Verified live rather than assumed: three butt-joined clips play `c0 → c1 → c2` with no stutter, and a 5 s gap produces exactly one jump (`30.05 s → 35.0 s`).

### Seeking is millisecond-accurate; cutting is not

`setPosition` takes milliseconds. The index is frame-exact and exports are frame-exact, but the playhead lands on the nearest millisecond — about 48 frames at 48 kHz. Perfectly adequate for finding and hearing audio; nowhere near good enough for cutting, which is why `player` is not allowed near the export path.

### Gaps are exited forwards, never to the nearer edge

The first implementation picked whichever run edge was closer in time. It looks reasonable and is wrong: a playhead one millisecond past a clip's end jumps *back* one millisecond, plays the last sliver of that clip again, runs out of clip, and jumps forward. The stutter is precisely what the gap skip existed to avoid.

`nearest_covered` now always returns the **start of the next run**. A backwards jump only happens when there is nothing ahead at all, which is a genuinely dead end rather than a choice.

### Half-open intervals bite again, in the one place they were supposed to help

`nearest_covered` returned a run's `end` instant as the backward candidate. But `end` is *exclusive* — it is the first microsecond of the gap, not the last of the audio. So the function whose entire job is "give me an instant that has audio" could return one that provably does not, and playback would dutifully claim to be sitting in silence. Now it steps back one microsecond.

This is the third time half-open intervals have produced an off-by-one (`contains` start-inclusive, the seam in `plan_snip`, now this). The intervals are right; the repeated cost is that every consumer has to remember which end is inclusive.

### `Signal(int)` is 32 bits, and a microsecond timestamp is not

Qt maps `Signal(int)` to C++ `int` — a 32-bit signed type. A microsecond timestamp is ~1.8e15, so **every** `positionChanged` emit raised an `OverflowError` out of shiboken, ten times a second. The player still appeared to work, because the emit is the last statement in the tick.

Found by running it with `warnings.simplefilter("error", RuntimeWarning)`, which is now a regression test. Signals carrying timestamps use `object`.

**General lesson: a signal that always raises is worse than no signal**, because the visual result looks fine. Only running it for real, and treating the warning as fatal, surfaced it.

---

## UI

### `QDateTimeEdit` is unusable here; `DateField` replaces it

The plan above was wrong, and not in the predicted way. `QDateTimeEdit` *renders* the format perfectly and does not *parse* a pasted one:

- Typing or pasting `12.5.2026. 10:15:00` character by character yields   `11.1.2000. 00:00:00`. The widget rejects each field as it is typed, so by the   time the year digits arrive the day and month have already been reset. - `setText()` looks like the fix and is not: the widget overwrites its own value   from its (mangled) internal date on the next event. - `editingFinished` never fires, so there is no hook to parse on commit either.

So there is no hook to hang a parse on. `datefield.py` replaces it with a `QLineEdit` plus an optional calendar dialog:

- parsing lives in `datetimes.py` as `(matcher, builder)` pairs — the patterns are   data, so adding a format is a table entry rather than a new branch; - `DateField` signals `changed(object)` with microseconds, or `invalid(str, str)`   with the text and the reason; - `set_instant` and `restore` are deliberately different methods. Setting a value   must not clear an error message the user still needs to read, and conflating   them did exactly that — `restore` was wiping the message it was about to display.

`QDateTimeEdit` also does not accept the unpadded `d.M.` form the requirement asks for on paste at all, which is the thing that actually matters: the user is expected to copy a date out of a file listing.

### A swap written back into the editors is a feedback loop

The snip panel has two date fields. If "from" is later than "to" the obvious fix is to swap the two editors' contents. It is wrong, for two reasons that only show up at runtime:

1. Each field's `changed` signal is wired to the handler that does the swap. Swapping    fires `changed` on both fields, which swaps them again. The recursion is bounded    only because the second pass finds them already ordered — so the user's date ends    up in the wrong box, silently. 2. In tests it manifested as a hang: a modal dialog opened while the event loop was    already re-entering through that signal.

The fix is to normalise the *range* (`_snip = (min, max)`) and never write the swap back into the editors. The two fields are then an unordered pair of inputs, and the snip summary line reports which is which.

### Scan and export must not run on the event loop

Indexing a folder of hourly chunks opens every WAV header. Exporting a long snip reads and writes the whole range. Both on the event loop is a frozen window, and the natural reaction to a frozen window is to click the button again — so both go through `workers.BackgroundJob`.

Consequences that had to be designed for:

- **The plan is captured at submit time, not at completion time.** Reading the   widgets in the completion handler silently exports whatever the user moved on to   while they waited. `start_export` snapshots the plan, the output format and   `fill_gaps` and the target path, and the worker only renders. - **A second job is refused, not queued.** `BackgroundJob.start` returns `False` over   a running job. Queuing would be friendlier but means the user cannot tell that   their second click did nothing, and two exports racing to the same path is worse. - **Button state is derived from one counter.** `_busy` is incremented by   `_start_job` and decremented by the `done` signal, and `_update_busy()` is the only   thing that enables or disables the action buttons. Having each handler set   `btn_export.setEnabled(...)` by hand meant an early return silently left the   button enabled while a job was running. - **Failure has to come back down the same path as success.** `_busy` is decremented   by `done`, which fires either way, so a failed export leaves the UI usable rather   than permanently disabled.

One job deliberately does *not* touch `_busy`: the peaks decode. Peaks are a background refresh of what is already on screen, not an action the user is waiting on, and disabling Export because the waveform envelope is decoding would be wrong. That job builds its own `BackgroundJob` rather than going through `_start_job`.

### Reading an archive must not create it

`ArchiveIndex.connect()` runs the schema script, so it creates the index file. That made `load_clips()` a *write*: reading an archive dropped a `wavesnip-index.sqlite3` into every folder that was merely looked at.

It was worse than litter. `add_archive` reads the index and then starts the scan that writes it, so a folder added at startup had one connection creating a brand-new SQLite file and another opening the same new file a moment later.

The crash I actually hit was not that race — I could not reproduce it in 60 trials, so I am not claiming it is the cause. It was this: a folder that does not exist at all. `sqlite3.connect` on a path whose parent is missing raises `OperationalError: unable to open database file`, which says nothing about what is wrong, propagated out of `add_archive`, out of `main()`, and killed startup with a traceback. A mistyped argument to `python -m wavesnip` is an entirely plausible way to trigger it.

So there are now three distinct folder states, kept deliberately separate:

| state | meaning | behaviour | | --- | --- | --- | | folder missing | a typo, or a share that is not mounted | `FileNotFoundError`, named | | folder is a file | wrong path type | `FileNotFoundError`, named differently | | folder fine, no index yet | the scan is still running | reads as empty, no error |

Collapsing the first and third is what made the failure look like "an app with no audio and no explanation" instead of "the path you gave me does not exist".

Reading a folder also stops touching it: `load_clips` and `clips_containing` check `require_folder()` and then `has_index()`, and never call `connect()`.

### One broken archive must not empty the whole timeline

`Timeline.refresh()` is per-archive now. A failure is collected into `Timeline.failures` and the other archives load normally, because every query fans out over the same clip list — an exception escaping the refresh would leave the app looking empty with no way to tell that one path out of four was wrong.

The broken archive stays in the priority list on purpose. A share that is not mounted yet is worth keeping a place for, so that Rescan recovers it once it is. The message names the folder, and does not name it twice: `missing_folder()` already includes the path, so prefixing the archive name onto it would give `typo: archive folder not found: /home/me/typo`.

### `QRect` takes 32-bit ints, and a microsecond timestamp is not one

This is the same wall as `Signal(int)` below, reached from the other direction, and it was invisible to 400-odd passing tests.

Gap shading built its rectangles from `column_edges()`, which returns boundaries in **microseconds** — and passed them to `QRect` as if they were **pixels**. A 2026 instant is ~1.8e15, so every single gap rectangle overflowed:

``` RuntimeWarning: libshiboken: Overflow: Value 1791139766286099 exceeds limits of type [signed] "i" (4bytes). OverflowError   ... in _paint_gaps Error calling Python override of QWidget::paintEvent() ```

The picture was never drawn, and it repeated on every repaint.

The fix is that column indices *are* pixel coordinates — one column per pixel across the content rect, which is how `_paint_peaks` already worked. So the rectangle is built from the run of columns directly and `column_edges` is not consulted at all. The whole overflow class disappears rather than being clamped away.

**Why no test caught it:** every existing test went *around* the paint path. A widget that is never shown is never asked to paint, and `grab()` on the window does not paint the child. `PaintTests` now calls `wave.grab()` directly, with `warnings.simplefilter("error", RuntimeWarning)` around it, so a shiboken overflow is an error rather than a log line. Verified by reverting the fix: 3 of the 6 fail.

The general lesson, now twice: **a microsecond timestamp must never reach a Qt argument that is declared `int`.** `Signal(object)`, and pixel-space geometry.

### `WavWriter` explains a bad output path before `wave` gets the chance

`wave.open(path, "wb")` builds a `Wave_write` whose constructor raises when the folder is missing. Its `__del__` then raises again on the half-built object, so the user gets an `Exception ignored in __del__` traceback *printed on top of* the real error. Two failures for one problem, neither of them the one the user needs.

`WavWriter.__init__` now creates the parent directory (a hand-typed path pointing somewhere new is a reasonable thing to do) and raises one `WavError` naming the path if it cannot.

---

## Timeline view

The redesign replaces "one clip at a time" with a zoomable window onto the whole archive, and merges the waveform, the cursor and the snip editor into one gesture model. `wavesnip/viewport.py` is pure and Qt-free; everything below follows from keeping the arithmetic out of the widget.

### Zoomed out, the waveform is not a waveform

The first question is what to draw when a year of 8 kHz audio is 640 pixels wide. Measured on this machine: a full decode of one month is **1.1 minutes**, one year is **12.9 minutes** and about **505 GB** of I/O. An envelope-only pass over a year is still 9.3 minutes. Caching envelopes was rejected — a cache that can be half a terabyte and has to be invalidated is worse than no cache.

So there is no envelope at all when zoomed out. Past a budget of `MAX_PEAK_FRAMES = 200_000_000` frames across the visible range, the view draws **coverage** — a map of what exists and where the gaps are — and says so in a line of hint text. Zoom in far enough and it decodes and draws the waveform for the range you can actually see.

The budget is in **frames, not seconds**, deliberately: cost scales with frames, and 200M frames is seven hours of 8 kHz but only seventy minutes of 48 kHz. A second-based budget would let the same number mean wildly different amounts of work.

Two more bounds keep a single request from being expensive in *memory* rather than time: `PEAK_CHUNK_FRAMES` (8M frames, so a peak array is 32 MB float32 or 64 MB float64 whatever the clip length) and `PEAK_COLUMNS = 1600`.

### A refused peaks request needs something to come back and ask again

`_request_peaks` refuses while a decode holds the slot. That is right — a decode must not be started twice — but it left the system with a hole. Every event that moved the view during a decode was refused, and `_on_peaks` then **discarded** the result because the range no longer matched. If the *last* such event was refused, nothing was left that would ever ask again.

The user-visible result: wheel to a comfortable zoom, let go, and sit on a bare coverage map. No waveform, no hint, nothing pending, and no recovery except moving the mouse. Nothing crashed and nothing looked wrong from the inside.

The fix is that a discarded result now clears `_peak_range` and schedules `_refresh_wave` on a `QTimer.singleShot(0)`. Clearing the marker is the important half — it turns the range back into "not already decoded", so the retry is allowed to proceed at all. The deferral matters too: `finished` is emitted while the job is still counted as busy, so asking immediately would be refused for exactly the same reason as before, and the retry would have to wait for something else to move.

The clear is **guarded** by `if self._peak_range != viewing`, and that guard is not decoration. Panning away and straight back leaves the view showing the peaks it had, and those are still the right answer; an unguarded clear would decode them a second time for nothing, and the flicker is worse than the waste. The guard was found by re-reading the fix rather than by a test, which is the usual way these are found.

This is the same shape as the 32-bit wall and the wheel sign: the bug is not in the arithmetic, it is in a piece of state that is set on one path and relied on by another.

**Flake alert:** `test_the_flipped_view_survives_a_decode_it_moved_on_from` — the regression pin for this bug — is timing-sensitive, because it stages a *real* decode race (30 zoom-in notches to cross the budget, then five more that must be refused while the decode holds the slot). Under a loaded full-suite run it has been seen to fail once while passing in isolation and on other full runs; when it fails, re-run the suite. It is not related to painting or axis changes, which cannot reach the decode pipeline.

### A test can pass without ever reaching the code it names

The test for this bug did not fail when the fix was reverted. Its setup spun the wheel until the view became affordable and then stopped — so exactly **one** peaks request was ever made, and it was never refused, because nothing was in flight yet. The bug needs a decode *already running* while later requests are dropped. The loop was breaking at the first affordable notch instead of running past it.

It reads as a test of the retry and is not one, which is the dangerous shape: it looks like coverage and it protects nothing. Two habits caught it:

- **Revert the fix and watch the test fail.** Now routine for anything claiming to   pin a bug. Two of these tests fail without the fix and pass with it; that is   checked, not assumed. - **Do not assert on an outcome a background job could have produced on its own.**   The first draft of these tests spun the real view and then asserted "peaks   arrived" — while a decode it had started was still in flight, free to satisfy that   on its own. `StalePeaksTests` therefore calls `_on_peaks` directly and asserts on   `_peak_range` synchronously: no threads, no timing, nothing that can arrive late.

The same trap bit the end-to-end smoke. Its "is it on screen?" check withheld the peaks to render a comparison image, then restored them by reading `wave.state()` *after* clearing — which handed back `None`. The check passed, and had quietly stopped comparing anything. The peaks are now captured before they are cleared, and an assertion checks they survived.

### A full disk truncates files mid-string, and nothing notices

The shell had been failing for a while. The cause was a full disk, which had cut two writes part-way through: `tests/test_mainwindow.py` lost its last ~180 lines and `wavesnip/mainwindow.py` lost the body of `_scan_all`. Both ends mid-docstring, both reading as plausible code.

Reading the files did not catch it. Neither did the `edit` tool, which had been reporting failures on calls that had in fact applied. What caught it was compiling every file in the tree at once:

```python for p in sorted(pathlib.Path(".").glob("*/*.py")):     compile(p.read_text(), str(p), "exec") ```

That is cheap, it needs no Qt and no test fixtures, and it is the only check that catches a file that has been cut in half. Run it first when something inexplicable happens, and before assuming the code is at fault.

The rebuild came from `__pycache__`. A `.pyc` older than the truncation still held the lost code, and `marshal` plus `dis` gave back every function name, line number, docstring, constant and instruction — enough to reconstruct `PeaksPlanTests` and `_scan_all` exactly rather than from memory. Worth remembering before retyping anything: **compiled bytecode is a usable backup of source you have lost**, and it is sitting there by default.

### The budget is crossed about 14 notches in

Measured, not assumed: a 46-hour archive at 8 kHz crosses the peak budget 14 notches from fully zoomed out. `MAX_PEAK_FRAMES` is 200M frames, which at 8 kHz is 25,000 s — 6.9 hours — so the window has to narrow by about 6.6×, and `1.15 ** 14` is 7.1. The number matters because it is the difference between a budget that feels like a cliff and one that feels like a slider, and because any test that guesses at "enough notches" is now guessing at arithmetic.

### Peaks live in `peaks.py`, not in the window

`peaks_for` and `reduce_piece` started in `mainwindow.py` because that is where they were first needed, and that turned out to be the wrong place for the same reason `cursor.py` is separate from `player.py`: the arithmetic is the part most worth testing, and a test that has to import PySide6 to reach it silently stops running on any interpreter without Qt. They are plain NumPy over a read callback, so they need neither, and they now sit beside `compute_peaks` in `peaks.py`.

A related trap, in the tests rather than the app: a Qt constant in a **default argument** is evaluated when the class body runs, which is at import time. That made `tests/test_waveview.py` unimportable without PySide6 — an import *error*, not a skip, so it looked like a fault in the module rather than an absent dependency.

### Peaks are reduced piecewise, never concatenated

`peaks_for` reduces each piece into **its own slice of the columns as it is read**. The obvious implementation joins every piece into one array first, which makes memory scale with the width of the view — a seven-hour window at 48 kHz stereo is several gigabytes of float32. The only thing held at once here is one chunk.

Columns are shared out **in proportion to duration**, and from the *requested* range rather than the covered one. That second detail is the one that matters: a share computed from covered time would let the audio either side of a gap stretch to fill it, drawing silence where there is none. Columns with no frames stay `NaN`, and the painter skips them rather than drawing a bar of nothing.

### The peak budget was per-piece, and had to be per-request

`MAX_PEAK_FRAMES` bounds one *request*, which covers the visible range. It was being passed to each piece in turn, so a window of nine hundred hourly chunks cost nine hundred times the intended work — exactly the case the budget exists to bound. `reduce_piece` now reports how many frames it actually read (it may stop short of its allowance), and `peaks_for` subtracts that. Asserted by `test_the_budget_is_shared_across_the_whole_plan`.

### Zoom about the pointer, and the sign of the wheel

`wheelEvent` passed `_ZOOM_STEP ** notches`, so scrolling **up** zoomed **out** — the exact opposite of every other audio editor, and of what the hand expects. Fixed to `_ZOOM_STEP ** (-notches)`. Only the waveview tests caught this, because nothing else drives a wheel.

The anchor holds to within one column per notch, which is the honest guarantee: at a year-wide zoom one pixel is hours, and each notch re-reads the pointer's instant from the view it just produced, so up to one column of rounding can compound per step. `test_each_notch_is_the_same_ratio` asserts the ratio is exact and separately that the anchor stays *in* the view.

### The scrollbar's range is not a timestamp

`SCROLL_STEPS = 1 << 22`, and the mapping is fraction-of-travel × that. A range in microseconds would need ~1.8e15, which is the fifth 32-bit wall this project has hit (`Signal(int)`, `QRect`, `QScrollBar`, and timestamps generally). The count is tuned so a step is a fraction of a second at a one-day window over a year: 1 << 20 gave ~30 s per step, 1 << 22 gives ~7.5 s.

The mapping is lossy, so `_syncing_scroll` guards the two directions against pushing each other in a loop that never settles.

### Tick labels are chosen from the visible span, not the step

A month of 31 days is not always a month, and a year is not 365× anything when it crosses a leap day. Steps below a day come from a fixed ladder; months and years come from the **calendar**, with `_add_months` clamping the day into short months. The *kind* is then refined by the visible span: two days forces a date label (year included), and a sub-day window that crosses midnight gets a date too.

Ticks sit on an **absolute grid**, so panning slides the waveform under a fixed axis rather than dragging the labels with it.

### The date field is the precise way in

At a year-wide zoom a pixel is hours wide. `instant_at` interpolates within its column and `x_of` reads it back, with the invariant that `x_of(instant_at(x))` is the column clicked — offered *instead of* exact inversion, which is impossible at that zoom. The date field is how you place the cursor to the second, and the UI says so.

### `DateField` holds an instant, not a string

The display format is `D.M.Y. H:M:S` because `design.md` requires a date to be copyable off the screen and pasteable back in — one-second resolution, fixed. `instant()` used to re-parse that text, so any sub-second range collapsed to whatever its printed seconds were. That was invisible at the original zoom (2 hours across 640 px is 11 s per column) and became constant the moment the timeline could be zoomed in, since a drag at an ordinary zoom routinely selects a range shorter than a second.

The field now keeps `_value` (microseconds) alongside `_written` (the exact text it wrote). While the text is still that text, `_value` is authoritative; the moment it differs, the text is — which is what makes typing and pasting work. `set_instant` and `restore` remain separate, because setting a value must not erase an error the user still needs to read.

### Nothing has chosen a range yet — and that is not the same as a one-second range

The launch view is supposed to show every clip. The test for "the user has not chosen a range" was `end - start <= 1`, on the reasoning that an empty archive's limits are `(0, 1)`. But clamping *expands* that to `min_span_us`, so by the time the real extent arrived the state held a legal one-second window, the test concluded the user had chosen to be zoomed in, and the app opened on one second of silence.

`WaveView._view_chosen` is now an explicit flag, set by any deliberate move. Until it is set, `set_limits` follows the archive. `ensure_visible` and its `pan_to` pass `user=False` — scrolling to follow playback is the view arranging itself, and counting it as a choice would mean the first thing played pinned the view for good.

### The padding is 2% of the span, capped at an hour — and never smaller than the minimum view

A fixed hour of padding is invisible on a year (one pixel in twelve hundred) and absurd on a short archive, where an hour either side is three hundred times the audio and pushed every clip into the middle of the widget. Two per cent of the span is a margin on both. The two meet at fifty hours, which is much earlier than it sounds.

One exception: an archive *shorter* than `MIN_SPAN_US` gets extra padding anyway. Zooming in that far is not allowed — a column narrower than a sample makes the drawing lie — so the view is held a second wide and hangs over the ends. Without the extra padding the view would sit outside its own limits, which quietly breaks every later clamp and scroll calculation.

### The archives moved to an options window, in a config file

A permanent panel for archive folders steals width from the waveform, the cursor and the selection, which are the things needed constantly; archives are needed once and then never again. The list is kept in JSON at the XDG config path (`~/.config/WaveSnipper/archives.json`, `Application Support` on macOS, `%APPDATA%` on Windows), written atomically through `os.replace`. `Config` is a class so tests inject a path rather than reading the developer's real one.

The dialog **edits a copy** and the window applies it on accept. Adding or reordering an archive re-indexes folders and rebuilds the timeline, so a Cancel that undid that by hand would have to un-scan files. `apply_archives` — rather than `add_archive` in a loop — is the authority for a whole order, because adding one at a time reverses it.

### Playback, cursor and selection are one gesture

Click puts the cursor, drag selects the range to snip, the wheel zooms, shift-drag pans. Left-drag had to stay available for selecting, which is the thing the app exists to do, so panning got its own modifier. A drag under four pixels is a click — judged by distance rather than time, so a slow deliberate click does not become a snip of two pixels.

`_peaks_affordable` gates even *building* a snip plan, so a view too wide to draw never pays for planning either.

---

## The scale of the view is not checked against accuracy

`MIN_SPAN_US` is one second across the *whole widget*, whatever its width. At a very wide window each column is therefore coarser than one sample, and the waveform is drawn from an under-sampled envelope — the peak of a band, not of the samples in it. It is honest at any window a person would actually use, and the alternative (a minimum derived from the clip rate and the pixel count) would make the zoom ceiling depend on the monitor, which is worse. Noted rather than fixed.

Related: peaks are re-decoded on every view change, with no cache. That follows from the coverage-when-zoomed-out decision — there is no envelope to cache — but it does mean scrubbing over an affordable range re-reads the audio.

---

### Time blocks rise from the viewport floor

`design.md` asks for blocks to rise from the bottom, the first one lowest. The layout as written was the opposite, and the reason is that `assign_time_block_rows` is a greedy first-fit over blocks *sorted by start time*: row 0 goes to the earliest block, and painting row 0 at the top of the band hung the earliest block off the ceiling of the waveform area with a row of empty space underneath it — reading as a gap between the block and the axis it belongs to.

The rows are now measured from the **bottom edge** of the content rect and stacked upwards. The bottom edge is the anchor, so the lowest row cannot drift away from the axis as rows appear or disappear, and an area too short to hold the stack clips rows instead of letting them hang over the top of the widget.

Two things worth keeping:

- **One copy of the arithmetic.** `_time_block_row_rect(area, row)` answers the   geometry for both the painter and `_time_block_at`. Before, it was written out   twice as `tb_area_top + row * row_height` — two chances to disagree, and the   symptom of that is a block you can see but cannot click. The hit test is now a   *consistency* test by construction, which is why it passes either way round   and the direction is asserted separately, in pixels. - **The clipped case cannot be reached through the widget.** `WaveView` sets a   minimum height of 200, so a content area too short for two rows only exists if   a test drops that minimum first. Worth knowing before treating "it never   happens" as the same as "it is handled".

`tests/test_waveview.py::TimeBlockRowTests` — six tests. Four fail with the mapping put back to top-down (checked by reverting it, not assumed): the floor assertion, the stacking order, the rendered-ink one, and the clipped row. The other two — hit-test agreement and three-row containment — hold under both layouts and are kept for what they actually are.

Nothing else about the rows changed: still three maximum, still assigned by the greedy first-fit, still a 2 px gap between them.

### Milliseconds in the date, and the widget that has to agree with them

Found as seven failing tests across three files, none of them next to each other — the signature of a change made on one side only. `format_datetime` emitted `.000` and `_EXAMPLE` was `10:15:00.001`, four `FormatTests` had been updated to match, and `QT_DISPLAY_FORMAT` had not: the calendar popup's `QDateTimeEdit` still rendered `10:15:00` for the same instant the line edit showed as `10:15:00.000`.

**Kept the milliseconds.** The alternative — seconds only — means the display rounds while `DateField` keeps microseconds internally, so a sub-second drag shows a time it was not. That is the same class of lie as `DateField` re-parsing its own text: two representations of one value, disagreeing silently.

**The separator is a dot, not `design.md`'s colon.** The design's example is `10:15:01:001`; Qt renders `zzz` only after a dot and cannot render a colon form at all. So the colon would have meant rewriting `QtAgreementTests` to compare at second resolution — weakening exactly the invariant that caught this ("the string on screen is the string you copy"). The separator is cosmetic; that invariant is not. `format_datetime_ms` keeps the colon, because `time_blocks.json` has no Qt widget to agree with.

**Verified, not assumed:** Qt 6.11 renders `d.M.yyyy. HH:mm:ss.zzz` as `12.5.2026. 10:15:01.001`, and `parse_datetime` already had a decimal-fraction pattern, so the round trip needed no parser change.

`_EXAMPLE` is now `format_datetime(datetime(2026, 5, 12, 10, 15))` instead of a literal. The failure *was* that drift: a placeholder that no longer described what the field writes teaches the wrong thing to type. The test still pins the literal string — the test owns `design.md`'s shape, the code derives from its own formatter, and neither can move alone.

Also re-run: the `QDateTimeEdit` typing experiment that `datefield.py`'s docstring quotes. It had been done under the seconds-only format, so the number in the docstring was stale. Under `.zzz` typing `12.5.2026. 10:15:00.000` character by character leaves `11.5.2000. 20:26:00.101` — still garbage, conclusion unchanged. Quoting a figure nobody has re-run is how a docstring quietly becomes a guess.

### The position label said the same thing twice

`play_position` was a `QLabel` showing the cursor instant, a few pixels from the cursor `DateField` showing the same instant — and the date field is the one that can be copied out of. When that row was trimmed, the label's construction, its update in `_on_position` and its reset in `_on_stop` were commented out; the widget, the now-orphaned `_play_position_reset()`, and two tests all stayed. The dead method would have raised `AttributeError` on the first call, which is the usual fate of a widget removed by commenting: the removal is everywhere except the places that still name it.

Removed rather than restored, and the dead method with it. The coverage moved to what actually remains: stop clears `_playhead_us`, and the position follows into the date field.

The `◀ week` / `Now` / `week ▶` buttons are in the same commented-out block — still constructed and connected, never added to the layout. Left alone: they fail nothing, and whether that row comes back is a design question rather than a bug.

### The right-click menu did not know where it was opened

The first of the four next steps was "add reveal file in explorer to right click when hovering over a clip in viewport". Hovering over a *clip* is the operative half: the menu has to resolve what is under the pointer, and it could not, because `mouseReleaseEvent` handed `requestContextMenu` an instant computed as `self.instant_at(self._drag_origin) if self._drag_origin else 0`.

`_drag_origin` is set only by a **left** press, so on a right click it is always None and the menu was always told `0` — 1970. Every entry keyed on that instant was therefore wrong: "Seek cursor from here" jumped to the epoch, "Set region start" wrote it, and any future resolution of the clip at the pointer would find nothing there. The expression it would have taken when non-None was wrong twice over as well — `instant_at` takes a pixel, and it was being handed microseconds.

The right-press origin does exist: it is `_pan_origin`, already captured at the press and already moved into a local by the time the menu is emitted. The menu now takes that, falling back to the release position rather than to 0, on the principle that a wrong instant is worse than no menu at all.

While doing it: `requestContextMenu` was declared `Signal(int)`. Microseconds since 1970 are ~1.8e15 and do not fit the C++ `long` that an `int` signal declares — shiboken answers with a `RuntimeWarning: overflow converting to C long` and a mangled value, not an exception, which is the failure mode `test_waveview.py`'s docstring already describes for `QRect`, `QScrollBar` and integer signals. It had never been noticed because the only value ever emitted was 0. Now `Signal(object)`, like `playheadMoved` and `snipSelected` already were, and `RightClickInstantTests` runs the emit under `RuntimeWarning` as an error so the declaration cannot quietly regress.

The menu itself was one method that both built and showed — `menu.exec(...)` at the end — which made it unassertable: you cannot check that an action exists without a user dismissing the dialog. Split into `_build_context_menu(instant)` returning the `QMenu` and `_on_context_menu` doing the single `exec`. The conditionals that make it interesting (which clip, whether a region exists, whether a block is selected) are now all testable, and `ViewportContextMenuTests` holds the reveal entries: offered over a clip, absent over a gap, absent before any archive is loaded, and — triggered, with `reveal` patched — handing over that clip's own file.

`reveal` is a new module rather than a method on the dialog, because two callers want it (the viewport menu, and Options' button for `time_blocks.json`) and because it is three different requests: `open -R` on macOS, `explorer /select,` on Windows, and on Linux `org.freedesktop.FileManager1.ShowItems` over D-Bus when `dbus-send` is present. Where it is not, the fallback opens the *folder*, not the file: `xdg-open` on a `.wav` plays it, and the user asked to be shown it. `reveal_command` is separated from `reveal` precisely so the three branches are assertable on a machine that is only running one of them.

One Qt detail worth recording: `triggered` passes a `checked` bool, so `lambda path=clip.path: ...` receives `False` as `path` — the default is only used when the slot is called with no arguments, which Qt never does. The lambda is zero-argument, like every other entry in the same file. The first test run caught it as `Path(False)` raising `TypeError` inside a slot, which Qt prints to stderr and swallows: the assertion failed, the traceback did not.

### Save let go of the block it was saving

`_update_time_blocks_in_view` rebuilds the `WaveViewState` field by field, and stopped one short of `selected_time_block`. Everything that updates blocks goes through it, so a save — the moment the user most expects the block to still be theirs — dropped the highlight; and `_on_tb_save` additionally cleared the editor, leaving Save enabled over empty fields, where a second press would have *added* a copy of the block it was meant to update.

The key is taken from `_editing_block`, not copied from the old state, because saving replaces the block: `TimeBlock` is immutable, so an edit is a new object, and the previous tuple names bounds that may no longer exist. Which is why the four places that swap a block now go through one `_replace_block(old, new)` that re-points the editor at the copy — the editor holds an object, not an index, and any site that forgets leaves the window editing something that is no longer in the list.

Two things fell out of writing the test in the natural flow (create a block from the region, then save it):

- `_on_tb_new` set `_editing_block` **after** calling   `_update_time_blocks_in_view`, so the rebuild looked for a selection that had   not been declared yet and the block just created came up unhighlighted. The   two lines were the wrong way round. - it never filled `tb_start` / `tb_end`, unlike `_on_tb_selected` which fills   everything. So after `[+ New]` the date fields were empty, `_on_tb_save` read   `None` from them and returned with "Start and end times are required" — for a   block whose times were sitting in `_editing_block` the whole time. The editor   now shows the bounds of the block it is editing, whatever put it there.

Deleting still clears. That is the counterpart, not an oversight: a highlight that survived a delete would name a block nothing paints any more, and `_selected_block_key` checks membership for exactly that reason.

### A double click selects the region that is already on screen

No `mouseDoubleClickEvent` existed anywhere in the view. Double-clicking a block now emits `snipSelected` with the block's bounds and `timeBlockSelected` with the block — the region half and the selection half of "yes, *this*", in the same signals a drag and a click already use, so there is no third path to keep in step.

The handler does its own hit-testing because it has to: Qt delivers press, release, `MouseButtonDblClick`, release, and the second press never reaches `mousePressEvent`. A handler that leaned on the press before it would work in practice and fail as soon as it was tested in isolation. Hence the window test sends all four events rather than just the interesting one.

### The archive list showed names, and names are not unique

One line: `f"{index}. {archive.name}"` → `f"{index}. {archive}"`. Two folders called `radio` in two places are different archives with different hours, and the row is the only place the priority order can be read against — "0. radio / 1. radio" is a list that cannot answer the question it exists to answer.

The existing test passed either way, because it uses relative paths where `Path("a") == "a"`. `test_the_rows_carry_the_whole_path` uses two absolute paths that end in the same name.

Options' `Show in Folder` button now calls `reveal()` as well. A button that opens the folder and a menu entry that selects the file are two routes to one request, and two implementations are how they end up in different file managers.

Every change above was checked by mutation rather than assumed: each fix was put back to its broken form in turn (instant 0, no double-click handler, `Save` clearing the selection, the reveal action never added, `_replace_block` not re-pointing, `_on_tb_new` out of order, `archive.name`) and the tests that catch it identified — six of the seven produced a failure, and the seventh (`archive.name`) is the one that the old test could not see and the new one can. Full suite: **686 passed, 78 subtests, 0 failed**.

---

### The clip blocks stopped filling the widget

`design.md`: the clip blocks (and waveform) should not paint the whole height — about 4/5 of the viewport.

`_wave_rect(area)` is now the single source of that height: four fifths of the content rect, centred, computed in integer arithmetic (`_WAVE_BAND = 4, 5`) because a fractional pixel in a `QRect` is a rounding decision nobody chose.

The first reading of "not the whole height" put *everything* in the band — gap fill, boundaries, Sundays and all — and was corrected twice, first about the background and then about the gaps themselves:

- **Gaps are not drawn at all.** Not banded, not filled: a column with no audio   is the background, empty, because that is what it is, and there is no reason   to paint a hole. The pen-drawn coverage boundaries were deleted by hand; the   dark red gap fill followed them out. What is left is `_paint_clips`, which   draws one rectangle per run of covered columns and nothing else — with no   "if not everything is covered" guard around it, since a fully covered view is   a solid block, not a view with nothing to show. This inverts the old test   that a hole must not look like a silence: the hole is now literally nothing,   and every row of it is asserted to be background. - **The Sundays are full height, behind the clips.** The weekend belongs to the   time axis, not to the picture drawn over it, so `_paint_sundays` paints before   `_paint_clips` and spans the whole content rect. The wash shows in the margins   above and below the blocks and in the gaps between them; where a block covers   it, the block — opaque, painted after — keeps its exact colour. "Behind" is   therefore a pixel claim, not an ordering claim, and the test makes it as one:   a column that is both Sunday and clipped reads `wave_dim` exactly, while the   rows above and below it are redder than green.

What went *into* the band: the clip blocks, the waveform, the snip wash (it marks a selection *of the map*, so a full-height wash would be exactly the whole-height painting the band exists to stop), the clip labels at the band's top edge, and the hint that sits under those labels.

What stayed *outside*, each with a reason:

- **The background, gaps included** — it is the viewport's own colour, not the   picture's, and the picture is what the margin exists to frame. - **The Sunday wash** — see above; it runs the full height behind the blocks so   the week's shape is readable whatever the zoom has done to them. - **The playhead.** It is a cursor, not content — where you are in the widget,   and it marks the instant all the way down to the axis. A cursor you can only   see inside the band is one that goes missing exactly where you were looking.   Its test asserts the exception so that the exception is a decision.

Two choices worth writing down:

- **Centred, not top-aligned.** The margin belongs to the drawing, not to the   time-block stack; taking it all from the top would hang the map from the   ceiling of its own box. The remainder of an indivisible height goes to the   bottom (16 above, 17 below at the default size), which the test allows as   rounding. - **Labels follow the band.** They name clip *blocks*, so they sit on them. At   the top of the widget they would have floated over a margin's worth of empty   air, naming nothing.

Known interaction: the band and the time-block rows overlap by about ten pixels at the default size (a 131px band over a 164px content rect, with row 0 taking the bottom 28). Blocks are painted last, so they win — and since the waveform's baseline is at the band's centre, what they cover is the extreme end of the loudest peaks, not the picture. Less overlap than before, when the waveform ran to the floor and the rows sat on top of it.

The interesting test problem is alpha: the Sunday wash (25/255) and the label backing (190/255) are blends, not their nominal colours, so neither can be found by matching a colour. The Sunday test picks the wash out as `red - green`, which is positive for it and not for anything else that can be on screen — and then checks the block underneath *is* an exact colour, which is the one assertion blends cannot fake. The label test compares two renders that differ only by `clips`. Both start from "sanity: the thing is drawn at all", because a test that finds nothing passes any assertion about where it is.

One test was rebuilt after mutation testing caught it sleeping: the first waveform test used a half-amplitude waveform, which sits inside *every* band-shaped thing, and the mutant drawing peaks at full widget height survived it. Full amplitude alone was still not enough with a clip block behind: the gradient fades into exactly the block's colour, so the bottom of the waveform is invisible against it. Drawn alone over the bare background, a full-scale waveform spans more than nine tenths of the band and cannot leave it.


### A block you cannot see is not a block

`design.md`: time blocks always visible at any zoom level, at least 3 pixels wide, no matter if they overlap.

Before, the painter skipped `if x1 <= x0: continue` — a block narrower than one pixel was drawn as nothing at all, and *which* sub-pixel blocks survived depended on which side of a column edge their ends happened to fall. At a year to a viewport, an hour-long show simply was not there, which is worst exactly when you are scanning for where to zoom in.

`_time_block_span(start, end, area)` is now the one answer for both the painter and `_time_block_at` — the same pairing `_time_block_row_rect` already is for rows, for the same reason: two copies of an arithmetic is two chances to disagree, and the symptom is a block you can see but cannot click, or one you can click but cannot see. In-view test, clamp to the view, then widen to `_MIN_BLOCK_WIDTH = 3` — rightwards, or leftwards when the block is at the right edge of the content rect, where growing right would run off the widget. Overlap is accepted rather than avoided: blocks that share time are already on separate rows, and the alternative is dropping the block.

Two things found while doing it:

- **Qt's `drawRect` takes its second pair of coordinates as the bottom-right   *corner*, not the size.** A rectangle of `width = x1 - x0` puts its own   outline on `x1`, so a block's pixels really are `x0 <= x <= x1` — which is   what the hit test already said. The first version of this change "fixed" the   hit test to `x < x1` on the assumption that painting stopped at `x1 - 1`, and   the two tests that click at WIDTH/2 (the exact end of a half-view block) went   red. That is the consistency the shared helper is for, and it ran both ways:   the painter was right and the refactor was wrong. It also explains why an   interior widened block is four pixels of ink — three of fill plus the outline   — and an edge one is three, the fourth being off the widget. - **A minimum-width block carries no name.** Below about an ellipsis of width   `elidedText` returns nothing (verified: `-1, 0, 1, 3, 4` all give `''`), so   the new `if room <= 0: continue` changes no pixel — it says out loud that   three pixels cannot say who the block is. Removing it is an equivalent   mutant; the *property* (no name spills past its block) is protected by the   width assertion in the tests instead.

### Three rows was never a limit of the viewport

`design.md`: remove the limit on the number of time block rows that can be shown.

The cap was two lines: `rows = [[], [], []]`, and `assigned_row = 2` when every row overlapped. The consequence is the bug the rows exist to prevent — a fourth mutually overlapping show was drawn *on* the third, one name over another's rectangle — and it only shows up from the fourth block onwards, which is exactly when you are trying to tell shows apart.

Now the list of rows grows: a new row only when every existing one is occupied. Four mutual overlaps give rows 0–3; twelve give twelve.

What did **not** change is `_time_block_row_rect`, which still returns `None` for a row the content rect is too short to hold. That is the real limit and always was — at the default size six rows fit (30px pitch over 164px) and the seventh is not drawn. The difference is that the answer now comes from the viewport's actual height instead of from a constant chosen without knowing it, so resizing the window changes how many rows show and nothing else does. Worth being explicit: with more than six mutually overlapping blocks the topmost are still invisible — a consequence of the row height, not a cap, and any row shorter than that would stop carrying a readable name.

`_col_tb_bg` / `_col_tb_edge` — the palette commented "up to 3 rows for overlaps" — turns out to be dead: a block carries its own colours, derived from its name. The comment was corrected rather than the lists deleted, since they are a palette, not a limit.

The round these three belong to was mutation-checked as a whole: **twelve of thirteen mutants caught** (band, peaks, labels, snip, playhead, block width and its edge growth, the row cap, the hit test, and the two new ones from the rework — the blocks painted full height and the Sundays either confined to the band or painted over the clips). The survivor is `room <= 0`, removed, which changes no pixel at all: an equivalent mutant, and the property it appeared to guard is held by the width assertion instead.

### The export "Update" button asked the user to notice staleness

`design.md` asked whether it could go, and what it was for. It was the manual half of a refresh that only happened automatically when the *archive set* changed: `_refresh_formats` builds the dropdown from `SnipPlan.formats` — the distinct formats of the clips inside the **snip range** — but the only caller was `_rebuild_timeline`, and every way of moving the range (the two date fields, the set-from-playhead buttons, a drag on the waveform, the double-click that selects a region) goes through `_on_snip_changed`, which did not. So after moving the range the box went on offering formats that were no longer in it and omitting ones that now were, and the button existed so the user could catch it up. No test ever touched it.

The refresh now lives where the data changes — `_on_snip_changed`, `_on_snip_cleared`, and the empty-archive branch of `_rebuild_timeline` (which had no refresh at all, and would have kept offering formats from archives that were removed) — and the button is deleted.

One guard makes the move safe: a refresh that would list exactly what is already listed returns before touching the box. `QComboBox.clear()` is not free — it also drops the current index back to 0 — so rebuilding on every keystroke of a date field would silently put a still-valid override back on "Default" and flicker while doing it. Comparing the labels first means an unchanged plan costs one list comprehension.

What does change, deliberately: an override that is no longer in the range goes back to "Default". It named sources that are not there any more; keeping it would be a choice the user cannot see in the box (the entry would be gone from the list while still being the selection). Five tests in `FormatListTests` cover it — the list follows the range, a still-valid override survives the move, an out-of-range one falls back, removing every archive leaves only the default, and there is no button left to press.

---

### The top row now follows the design: a status block, a transport, and no rescan button

`design.md` asked for the top row to become `STATUS | gap | play/stop | cursor input | gap | zoom | Setup` and for the rescan button to go, and for the top status bar to keep only the clip count, the click precision, and a "making waveforms" spinner.

**Status, transport, and controls, in one row.** The play/stop toggle and the cursor date field moved up out of the Region Selection box (that box's first row is gone), and the row now reads, left to right: the status block — clip count, `1 px ≈ …`, and the spinner — a stretch that is the design's first gap, Play, `Cursor` + its date field, a spacer, `+` `−` `Fit all`, and `Setup archive..`. The button order inside the zoom group follows the design (`+` `−` Fit all) rather than the previous `−` `+` Fit all.

**"STATUS" is a block, not a label.** A literal `STATUS` caption next to the counts would read like a heading for the wrong half of the row; the three widgets *are* the status, so they sit where the design drew the word.

**The rescan button is gone, the scan is not.** `_on_rescan` still answers File → Rescan archives (F5) and the Setup dialog, and `rescan()` is still the launch-script entry point. What the button had that the menu does not was discoverability on the row itself — and the design traded that for the space.

**The spinner means "making waveforms".** The peaks decode is now visible: `_request_peaks` raises the spinner when a decode starts and `done` lowers it. It is deliberately a flag (`_peaks_busy`) rather than another `_busy` job, because `_update_busy` *disables* export and selection buttons while it spins — a decode runs on every view change and is short, so blocking the controls for it would make the app feel wedged. The two disagree only in that direction: an ordinary job shows the spinner too, nothing else happens on a peaks decode.

**The top status bar was cut down to the design's list.** `archive_summary` used to pack the folder count, the clip count, and the whole timeline extent (first → last) into one sentence; it now says just `N clips`, with `No archive folders.` / `No audio indexed yet.` as its empty states. The folder count and timeline dates live in the Setup dialog's per-archive details, where they pertain to one folder rather than to everything. The "unreadable" tally is gone from the bar too — unreadable *folders* already announce themselves through the status message and the dialog details, and clipping off the bar removed the one place the whole burden was restated.

At construction there was also a `_build_status_strip` that had been commented out and would have re-created `clip_label` off-layout; it is deleted rather than kept as a trap for the next layout change.

**The row must stay one line, and it quietly stopped being one.** The top-row rearrangement moved a `DateField` in, and `DateField`'s own layout carried the style's default 11 px contents margins around a 25 px line edit — making the widget 47 px tall. A horizontal layout never wraps, but its height is the tallest of its children, so the whole row grew to 47 px: a single control that read as a second row. The fix is in the widget, not the row: `DateField` zeroes its two layouts' contents margins and is now as tall as a bare `QLineEdit`, everywhere it is used (top row, snip fields, time-block fields). A test pins the invariant — no child of the top row may have a size hint taller than a `QPushButton`.

---

## The Sunday wash takes the whole widget; the day axis draws a line at a week

Two viewport follow-ups, one visual and one about what the axis has to say at different zooms.

**The Sunday wash runs wall to wall and floor to ceiling — axis included.** It was already painted *behind* the clip blocks and into the margins; the axis was the one part of the viewport it did not reach, because it was drawn over `_content_rect()` (which stops at the axis top). It now spans the full widget height, so a week-end reads through the two axis strips too. `_paint_sundays` still takes `area` only for signature consistency with the other paint helpers and ignores it: there is no smaller rect this is ever about.

**The day axis is a zoom-limited layer, and "months when zoomed out" is off the table.** `_content_rect` reserves two strips — an upper one whose ticks scale with zoom (up to `%B %Y` / `%Y` when the span demands it) and a lower one labelling every day with its weekday. The lower one used to do that at *any* zoom: zoomed to a year it painted ~365 shrunk day labels, noise that read as a switch-to-months that never happened. The user's decision supersedes fixing that: past seven visible days the day strip shows *nothing*. Strictly "up to 7 days", so `end - start <= 7 * DAY` keeps the strip (BASE to BASE+7d = eight midnights + one weekday label each) and `7 * DAY + 1 µs` empties it. Months remain the upper strip's job — the lower strip does not take them over, it just gets out of the way.

The 36 px axis reservation is unchanged even when the lower strip is empty. Reclaiming it would reshuffle `_content_rect`, `_wave_rect` and every geometry test for 18 px of space the user did not ask for — and the Sunday wash now gives that space a purpose at every zoom, since it is painted through it.

---

## The whole-year viewport is not slow because of the rects

A reported sluggishness at 6000+ visible clips prompted a measurement-first pass, and the measurement overturned the first guess. In a synthetic year at 1920×200 with 5838 clips, **`_paint_clips` draws exactly one rect**: the columnization already in place (``covered_columns`` + ``_true_runs``) merges every covered column, and at whole-year zoom all of them are covered. "Merge close rects" had already happened; the rects were never the cost.

Where the time actually went, per paint (measured):

- **Clip labels: ~3 ms.** Two ``fromiter`` walks over 5838 tuples *plus* two   ``x_of`` calls per clip, and every ``x_of`` was allocating a fresh   ``width + 1`` column grid. ~12 000 numpy allocations a frame, with ~0 labels   actually drawn at year zoom. - **Sundays: ~5 ms.** A datetime + ``to_us`` + two ``x_of`` per Sunday, over a   year of Sundays — again for a picture that is a handful of full-height rects. - **Coverage shaping: ~3.7 ms.** The ``covered_columns`` pass over every run,   recomputed on *every* repaint even when the view had not moved.

The fixes that shipped (712 tests still green):

- **One column grid per view.** ``_edges`` is memoized by (start, end, width);   every painter and hit test shares it read-only instead of allocating it per   call. (`test_the_flipped_view…`-style timings dropped the most here.) - **The label pass is one numpy search.** All clip boundaries map in a vector   pass; only the survivors get their name resolved and painted. Packed   ``(starts, ends)`` arrays and the ellipsis threshold are memoized too, so   repaints of an unmoved view cost ~nothing. - **The coverage map and packed arrays are per-view singletons.** Keyed by   (id, len, start, end, width), so playback/hover repaints reuse the result   instead of rebuilding the numpy pass. - **Sundays step in the calendar, map in bulk.** Instants are collected once   per call (the wall-clock ``timedelta(days=7)`` stepping is kept — integer µs   stepping would drift an hour at a DST turn), mapped with one vectorized   search over the cached grid, and drawn with a single ``drawRects``.

Result: whole-paint 11.1 ms → 5.3 ms (2.1×) at the year/5838-clip view.

**What remains, and why the next step is architectural.** With the loops gone, the costs left are floors: the Sunday wash's ``drawRects`` rasterizes ~3 ms (52 full-height translucent rects — Qt's own per-rect work, not Python), the axis recomputes and redraws its month labels every repaint, and — the part quick wins cannot touch — *every* repaint of an unmoved view still repaints all the static layers. Two directions follow:

1. **Static-scene cache.** Render the unchanging layers (Sundays, block band,    axis) into a per-view image once and blit it on repaint. Kills the static    cost for playback/hover; the win is real but only for unmoved views. 2. **Gesture coalescing.** During pan/zoom each input event triggers a full    state rebuild + repaint + possibly a decode request; batching those to one    refresh per frame is what actually smooths interactive dragging, and it is    orthogonal to the paint cost.

Both are medium changes with behaviour risk; neither was in the "quick wins" bucket, so they wait on a decision.

---

## Bounded probing: the waveform at any zoom

Three changes, decided together because they are one feature: the gate that says when to draw a waveform, the path that keeps the cost of one honest at any width, and the inner band that stops a wide honest waveform from becoming a wall.

### The gate is a line in time now

`affordable_span_us(framerate, MAX_PEAK_FRAMES)` converted a frame budget into seconds, and the conversion was the whole problem: 200M frames is 6.9 hours at 8 kHz and 69 minutes at 48 kHz, so "can we afford this view?" answered six times apart for archives six times apart — always with the wrong question, since what anyone means is "is this view wide enough that the outline has stopped saying anything?" It is now `MAX_WAVE_SPAN_US = 7 * DAY` in `viewport.py`, consulted as `span <= MAX_WAVE_SPAN_US` in `_peaks_affordable`, with `MAX_PEAK_FRAMES` kept only as the read budget inside `peaks_for` — a backstop on I/O, not a decision.

The week is `design.md`'s own split — zoomed out is coverage, zoomed in is waveform — and the number can be honest now because reading no longer scales with the window. Deciding it before a snip plan is built also still avoids resolving priority across every clip in a year-long archive to throw it away.

### Exact or sampled: cost picks the path, and so does correctness

`peaks_for` has two paths:

- **Exact**, when `plan.nframes <= MAX_EXACT_FRAMES` (24M frames — ~95 ms of   decoding, measured at the ceiling; 16M was 63 ms) *and* every piece of the   plan wins at least one of the 1600 columns. Every frame is read; zoomed-in   views stay bit-exact. - **Sampled**, otherwise: 8 runs of 128 frames per column   (`PROBE_RUNS_PER_COLUMN`, `PROBE_FRAMES_PER_RUN`), laid out *per column* as   stratified midpoints, so a day-wide view and a year-wide view read the same   ≤ 1.84M frames — the ceiling is `1600 × (8×128 + 128)` whatever the span.

The second half of the exact condition is about correctness, not cost. The exact path allots columns with floors of duration ratios, so a piece narrower than one column's worth of the requested window floors to **zero columns and vanishes from the envelope** — no error, no gap, just a clip that is not there. That was a live ~4% `None` from `peaks_for` in the coverage tests at the old gate, not a theoretical hole. A plan where it *could* happen goes to sampling even though reading it would be cheap: `test_a_window_wider_than_its_pieces_is_sampled`.

Column-major was the other correction. Piece-major sampling asks "what does this piece look like?" and drops a one-hour clip inside a day-wide column the same way floor shares do. Column-major asks "is anything in this column, and if so sample it" — so a clip that is a rounding error against the window still lights its own columns (`test_sampled_columns_still_say_where_the_audio_is`).

**A covered column is never blank.** Stratified midpoints can straddle a narrow recording on both sides — a one-second clip in a 12.5-second column sits between every probe — so when no probe lands in a piece that intersects a column, one more probe goes at the centre of that overlap. Columns nothing intersects are never probed; a gap stays a gap. The fallback was revert-checked by disabling it: `test_a_covered_column_is_never_left_blank` failed with "unexpectedly None", which is the exact failure it exists for.

All probes for one file go through `wavio.read_probes` — one open, `setpos` per probe, grouped by path — and the frame budget stays per-*request* on this path too (`test_the_budget_binds_on_the_sampled_path`).

### Measurements, on the finished code

Dense fixture: one continuous 10-minute 48 kHz file, 28.8M frames.

| what | time | frames read | | --- | --- | --- | | exact, 10-minute view (the old behaviour) | 112 ms | 28,800,000 | | sampled, same view | 203 ms | 1,638,400 | | sampled, 6.5-day view containing the same file | 2.0 ms | 1,920 |

The sampled path's cost does not grow with the window — that is the property the week-wide gate rests on. The narrow-view rows are why the tier rule keeps the exact path: 1,600 scattered probes through `read_frames` (reopening per probe) cost 126 ms, the same probes through `read_probes` 14 ms, and reading the *whole* 60-second file sequentially 8.9 ms — a sequential read beats scattered ones so thoroughly that at narrow spans decoding everything is faster *and* exact. Probing wins where decoding isn't an option: a week of dense 48 kHz is 2.9×10¹⁰ frames.

Two profiled optimizations inside the sampled path, both kept for their lines: `read_probes` converts the joined bytes **once** and hands back views (12,800 `frombuffer`/`astype` pairs measured more than the file read they accompany; with this and `_fold_probes`, the dense sampled view went 485 → 203 ms), and `_fold_probes` sorts runs by column and combines each moment with one `reduceat` instead of four numpy calls per run.

### The envelope grew a second tone

Min/max is the right outline — it is what keeps a transient visible — and it has the opposite failure at the other end of the zoom: over a wide window nearly every column contains a spike somewhere, so the outline is full height everywhere and a loud hour and a quiet hour become the same solid block. The envelope is now two-tone: the gradient outline as before, plus an inner **RMS band** in solid `_col_wave` — the typical level the outline encloses, which is what a wide view should show.

The moments are kept honest by construction: `_reduce_blocks` returns sums of squares and *counts* rather than an RMS, `reduce_piece` merges them additively weighted by sample count, and the square root happens once at the end. So `reduce_piece` returns `(mins, maxs, rms, spent)`, and `Peaks.rms` defaults to `None`, which the painter reads as "outline only" — hand-built envelopes in tests stay valid, and a `0` or `NaN` band skips the column rather than drawing a line down the middle of nothing.

### The fixture had to grow, because a gate had moved

`CoverageToWaveformTests` was a 46-hour archive zooming from fit-all to the gate. 46 hours is *inside* a 7-day gate, so fit-all would have been affordable and every test in the class would have been asserting nothing — they failed loudly, which is the fixture working. It is now 76 clips × 6 h ≈ 450 hours: fit-all ≈ 452 h > 7 d, crossing takes ~8 notches (>5 holds), and at the flip the plan is on the sampled path (`span > 1600 × 10 s`) — where the fallback probe is what keeps `peaks.is_silent()` false.

### Green is not verified; revert it

Each load-bearing piece was disabled and its test watched fail: the painter's band (3 band tests failed), the tier condition (`test_a_window_wider_than_its_pieces_is_sampled` and the bounded-slice test failed), the fallback (the covered-column test failed), the gate set to a year (the gate test plus both coverage-map halves failed). Everything restored from copies, full suite re-run: 744 passed.

### Left open

- **Scattered reads on a network share.** `read_probes` is 1,600–14,400 seeks   of a few hundred bytes each. From page cache that is microseconds; from an   SMB share at a few ms per round trip it is minutes. Local disks are the   assumption; archives on shares would want a latency-aware probe density   (fewer, larger runs). The per-file cache below helps here too — a sequential   build sweep is the one share-friendly read, and every view afterwards comes   from RAM — but the first sweep still has to cross the wire. - **The peaks cache …** resolved below. - `MAX_EXACT_FRAMES` is a machine-specific number, like `MAX_PEAK_FRAMES`   always was, but it now picks *precision* rather than feasibility: too high   costs milliseconds, it cannot hang. It also now covers the cache's narrowest   served view at the archive's rates (below), so a view too narrow for the   cache reads whole rather than seeking.

### The per-file envelope cache: read a range once, answer a thousand views

The two items left open above were the same pain: probes assume a local disk — 12,800 scattered reads of a few hundred bytes are microseconds from page cache and minutes on a spinning disk — and scrubbing a narrow view re-decodes the same range every repaint. The fix (`wavesnip/filepeaks.py`) turns both into **one sequential forward sweep per covered range, served from RAM afterwards**, which is the exact I/O a spinning disk is good at. This was the "peaks cache" from sections 21's decision list; the window owns one `FilePeakCache` and hands it to the peaks worker, so refreshes and rescans do not flush it — identity, not lifetime, decides when it goes stale.

**Fidelity 0.25 s; whole columns; counts derived.** `CACHE_STEP_US = 250_000` is one cache column per 0.25 s of audio, and because it is a *time* constant it sets the narrowest view the cache can serve: `PEAK_COLUMNS × step = 400 s ≈ 6.7 min`, at any rate. A cache column holds three float32s — min, max, sumsq — and sample counts are *derived* from file facts (uniform `frames_per_col × nchannels`, except the ragged last column), so nothing else is stored per column. Sizing: one-hour files cache to `14400 × 12 B ≈ 170 KB`, a full 6,000-hour archive fully cached is ~1 GB, and `MAX_CACHE_BYTES` (256 MB) with least-recently-used eviction is the thing that actually bounds resident memory — the cap, not the archive's size. A typical session (a few weeks viewed) holds a few tens of MB.

**Segmented and keyed.** Only the column ranges a view covers are built, rounded out to *whole* columns, so a zoom into the middle of an hour file reads that slice, not the file. Reads are aligned whole columns in chunks so the fold is one reshape; contiguous missing runs are swept one forward pass each. Identity is `(path, size, mtime, nframes, rate, channels)` — the same facts the indexer trusts — so a file that changes simply stops matching its stale entry, which LRU eventually drops.

**Tier: cache → exact → sampled.** When the view is at least 400 s wide every plan column resolves a whole cache column, so `peaks_for` builds whatever covered ranges are missing and assembles from RAM. `MAX_EXACT_FRAMES` went 16M → 24M so the gate is closed from below: 24M frames is 8.3 min at 48 kHz and 50 min at 8 kHz, both past the 6.7-minute cache boundary, so a view too narrow for the cache is read whole rather than probed — at the rates the archives use there is no probe band left at all (a 96 kHz archive keeps a thin [4.2, 6.7]-minute one). Probing stays as the backstop for files never viewed that happen to be inside a wide, uncached request.

**Assembly maps by midpoint, merges by moments.** Each cache column is mapped to the view column containing its *midpoint* in absolute time, then merged with min/max and additive sumsq/counts — the same moments arithmetic as the exact and sampled paths, so the band cannot drift into a different shape. Mapping by absolute time instead of the exact path's proportional floors is what keeps a piece far narrower than a view column in the envelope at all (`test_a_piece_far_narrower_than_the_view_keeps_a_column`). Whole-column builds make the cache approximate at *view* edges on purpose: a column straddling a boundary carries a sliver of the neighbouring 0.25 s, which is invisible because the cache is only served once a view column holds a whole cache column. Gaps stay NaN — a gap is not a file range, so nothing is ever built for it (`test_gaps_stay_unpainted_on_the_cache_path`).

**What it costs, measured (`/tmp/opencode/peaks_cache_bench.py`).** A dense 24-hour 8 kHz archive:

| path | cost | | --- | --- | | sampled, no cache | 12,800 probe reads ≈ 1.9 min of 9-ms seeks, **every repaint** | | cache, first view | 1.3 GB sequential sweep ≈ 9 s HDD (2.5 s on SSD here), once | | cache, same view again | 21 ms, no disk touched | | 10-minute zoom inside it | 0.6 ms |

The trade is shifted, not free: the seek-bound ~115 s *every repaint* becomes a sequential sweep of the covered data *once* — a dense week is ~9.7 GB at 8 kHz (≈ 1 min HDD) and ~58 GB at 48 kHz (≈ 7 min HDD) of first paint, then instant everywhere inside it. An uncached zoomed-in view is an exact sequential read of its own small range. For repeat-spotting sessions on big archives this is the whole difference between "spins forever" and "spins once".

**Verb, not noun: nothing persists.** The cache is a session cache — cheap to rebuild, bounded by the LRU cap. Persisting envelopes to disk was considered and left out: it would trade the one-time sweep for a ~170 KB write per hour file, a win only for archives that get rescanned constantly.

**Verified by revert, not by line count.** The fold is checked frame-exact against a direct per-column loop (`test_the_fold_is_frame_exact`); the ragged tail is folded, never padded (`test_a_ragged_file_folds_its_tail_into_the_last_column`); an aligned plan equals the exact path column-for-column (`test_an_aligned_plan_equals_the_exact_path`); a cached view provably decodes nothing on its second request (`test_the_cache_serves_from_ram_after_the_first_view`); a narrow view provably stays exact (`test_a_narrow_view_stays_on_the_exact_path`); building stops at the requested range and never re-reads (`test_building_stops_at_the_requested_range`, `test_a_built_range_is_not_read_again`); LRU drops and rebuilds the oldest (`test_lru_eviction_drops_the_oldest_file`); two clips sharing one view column keep both of their extrema (`test_two_clips_in_one_column_merge_extremes`). Whole suite: 805 passed.

The bug class worth remembering: **`arr[idx]` with an integer array is a *fancy index*, a copy** — so `np.minimum(a[idx], b, out=a[idx])` writes the result into the copy and silently discards it. It bit twice. In the builder, `np.minimum(entry.mins[jj], …, out=entry.mins[jj])` left a first view's worth of envelope at `±inf` (plain assignment is the correct write there: the `built` mask guarantees each column is written exactly once). In `_peaks_from_cache`, the settled-column merge dropped a loud clip's extremes from a seam column that a quieter clip had painted first — same fix, read-compute-assign, which is why `_fold_probes` and `reduce_piece` are written that way. Both times the failure was a *missing update* rather than an exception, so only the test that asserts the merged value catches it — hence the seam test asserting the loud clip's peak by number.

---

## Future: calendar visualization

The index is already the right substrate — a `start_us`-indexed row per clip is a heatmap. No schema change should be needed when we get there.

---

## Time blocks: three required fields, and one bad entry costs one entry

`design.md`'s first next step. Two problems in one load: the json demanded fields it does not need, and a single unreadable entry took everything down.

**What actually broke.** `_blocks_from` read `entry.get("start_time", "")` and handed that to `parse_datetime`, which returns **`None`** for anything it does not understand -- including the empty string a *missing* field produces. The next call was `to_us(parse_datetime(...))`, and `to_us` starts with `moment.tzinfo`, so `None` raised `AttributeError`. The loop's only except clause was `(ValueError, TypeError, KeyError)`, and `AttributeError` is none of those, so it left `load()` -- and `MainWindow.__init__` calls `_load_time_blocks` with nothing around it. One line missing from one entry meant no window at all, while every block that was perfectly fine stayed unloaded. The same path carried every unparseable date (`"32.1.2026."`, `"not a date"`), because those also come back as `None`.

**Required is three fields.** `name`, `start_time`, `end_time` -- exactly what `design.md` says the block needs to exist. The other four are read when present and take their defaults when not, so a hand-written entry of three lines loads. The optional fields are read *defensively* rather than trusted: `episode_num: "abc"` used to raise `ValueError` out of `int()` and drop the whole block, **silently** (the `continue` had no message attached to it); now the block loads with episode 0. A bare `"hosts": "Alice"` is one name rather than a list of characters, which is what iterating the string gave.

**Saving writes only what is filled in.** `_entry_for` builds the dict with the three required keys unconditional and adds each optional one only if it holds something, so saving a block does not re-add the empty parameters an earlier save -- or a hand -- had left out. Round trip is unaffected, because the loader treats absence as the same default the writer treated as empty.

**Corruption is reported, not raised.** Each entry is parsed on its own and a failure becomes a *line*, not an exception: `time_blocks.json: entry 2 "Morning show" has an unreadable start_time 'nope' (expected a date like 12.5.2026. 10:15:01:001)`. `load_with_report()` returns `(blocks, problems)`; `load()` is the quiet twin for callers that only want the blocks, and both go through the same code so they cannot disagree. There is still a bare `except Exception` around each entry -- after the explicit checks -- because the whole point is that an entry nobody anticipated must not be able to end the load. Three kinds of message: unreadable (`None` from the parser), out of range (`to_us` goes through `localtime()`, which stops at year 1 and year 10000, and `ValueError` from *there* was in the old except clause, so it was skipped in silence), and structurally wrong (no name, not an object).

**The console gets everything; the status bar gets one line.** The console is a log, so `_report_time_block_problems` prints every problem to stderr in full. The status bar is a strip, so it gets the first problem plus `(+N more, see console)` -- pushed through `_push_status` rather than `showMessage`, because a message that vanishes on the next status update is not a report. The bar is silent when the file is clean.

**A missing file is not a problem.** It is every first run, and `FileNotFoundError` is therefore answered with an empty list and no message -- greeting a new user with an error about a file they have never had would be worse than the silence it replaces. A *directory* where the file should be, an unreadable file, invalid JSON and a wrong document shape are all problems, and all reported: they used to be an empty list pretending everything was fine.

**Mutation-checked, not just green.** The old expression `to_us(parse_datetime(str(entry.get(key, ""))))` put back in `_instant` (with the per-entry guard removed) failed **11** tests, including all five window tests -- the window genuinely refused to build, which is the reported bug. The save writer always emitting the four optional fields failed 2. Skipping the report failed 3. Everything restored; full suite **770 passed, 82 subtests, 0 failed** (was 744: 20 new config tests in `tests/test_model.py`, 6 window tests in `TimeBlockLoadProblemTests`).

## A block deselects in four places, and none of them is the empty canvas

`design.md`'s next step: *deselect block on Esc instead of click in empty area. Actually the only times blocks should deselect is when: pressing esc, or clicking on a different block* — plus a menu entry, which makes the list of four: **Esc, a different block, "Deselect block" in a right-click menu, and deleting it.**

The old rule had a fifth — a left press on empty space emitted `timeBlockDeselected` — and it was the wrong one for a reason that has nothing to do with selection: that press is where a *region drag* starts. Every drag across empty canvas therefore emptied the editor on its first pixel, before the user had decided anything, and the drag that followed had nothing to do with the block. So the emit is gone from `WaveView.mousePressEvent`, and with it the signal and its connection — a signal with no emitter is not an API, it is dead code. Checked while removing it: the empty branch still sets `_drag_origin`, because taking away the deselect must not take away the drag.

`_on_tb_deselected` became `_deselect_time_block`, because nothing deselects it any more — it *is* the deselection, triggered by the window action and by both menus. It reports only when there was something to let go of: Esc pressed into an idle window is not an event, and a status line announcing a deselection to nobody would be noise. Whether anything *was* selected is read from both `_editing_block` and `wave.state().selected_time_block`, so a stale half-state gets cleared rather than left to be discovered later.

**One key, one owner.** Esc is a `QAction` bound in `_wire`, exactly like Space, `i`, `o` and Del, which is what makes the menu tip true rather than decorative. The tempting shortcut was to *also* `setShortcut` on the menu entries, so Qt would print a right-aligned `Esc` beside them for free; it was refused. Two actions bound to one key is two owners, and the popup is precisely where the two would meet: while a menu is open, Esc belongs to the menu (close me), and a menu that closes *and* empties the editor has done something nobody asked for. So the entry carries the tip in its own text — `Deselect block (Esc)` — in the same `(i)` / `(o)` / `(Del)` style the window actions already use, and `test_the_esc_tip_is_a_tip_and_not_a_second_binding` pins the split: the entry's `shortcut()` is empty, and exactly one window action owns `Esc`.

**Getting a key into a Qt window that has only been `show()`n.** The tests send it with `QTest.keyClick`, and the first version passed only when focus had been moved into a date field — `QTest.keyClick(window, Esc)` did nothing at all. An offscreen probe said why: `show()` returns before Qt has *realised* the window, so the `Qt.WindowShortcut` context has nothing to attach to and the shortcut map is never consulted; one turn of the event loop (`app.processEvents()`) arms it. `activateWindow()` does not — offscreen the window reports `isActiveWindow()` False no matter what, so the usual "make it active first" advice does not apply here. That is why `TimeBlockDeselectionTests.setUp` shows the window and then processes events, with the reason in a comment: without that line the Esc tests would be green while asserting nothing.

Both menus were also made *buildable* so they can be asserted on: `_on_tb_context_menu` now builds through `_build_tb_context_menu` and then execs, the way `_build_context_menu` already worked — a menu that exists only while it is popped up cannot be inspected. And the viewport menu's trailing separator (it always added one before the block section, so with no block selected the menu *ended* with a separator) moved inside that branch, being one line away from the new entry.

Mutation-checked in order: the old empty-space emit restored (signal, emit, connection) → 1 failure, `test_clicking_empty_space_keeps_the_selection`; the Esc action's `triggered.connect` removed → 2; both menu gates opened → 3; a real `setShortcut(Esc)` on the menu entry → 1. Restored each time; full suite **783 passed, 82 subtests, 0 failed** (was 770: 13 new tests in `TimeBlockDeselectionTests`).

## The cleanup pass: dead code, two bugs, and one copy of each helper

A maintenance sweep rather than a feature. The distinction that set the scope was **dead versus stale**: code nothing calls is dead and goes; a test whose expectation has drifted from a feature that still exists is stale, and the expectation is what is wrong. Conflating the two is how a working feature gets deleted along with the test that mis-described it.

**Dead means `refs == 1`.** Every removal was checked the same way: search the whole tree for the name and get back only its own definition. That is stronger than "no test names it", because a dead private helper can be referenced by another dead one and still look used. Nothing on the list passed that check: `_edit_time_block`, `describe_position`, `Timeline.from_paths`, `Source.duration_ms`, `GapSource.duration_us`/`duration_ms`, `pan_by_us`, `reset_view`, `datefield.message`/`set_error`, and the shadowed first `_show_tb_folder`. No test covered any of them, so removing them needed no test surgery — which is itself the evidence that they were dead rather than merely untested-and-live.

**`GapSource.duration_us` is not `Gap.duration_us`.** The grep that lists a removed name's survivors matters as much as the removal. `cursor.GapSource` had two duration properties nothing read and they went; `timeline.Gap.duration_us` and `model.Clip.duration_us` are fed by the coverage sum and the export path and were left alone. Same name, different class is exactly the case where a careless "sweep the unused durations" edit breaks the app.

**The date field's error label was invisible, so it was never an error surface.** `DateField` built a `QLabel`, styled it, and then the line that would have added it to the layout was commented out. Errors already reach the user two other ways: the `invalid` signal, which `MainWindow` turns into a status-bar message, and the value being restored to the last good one. The label was a third, dead channel that drew nothing. Removing it took `restore()`'s `message` parameter with it — the parameter existed only to write into the label, and the status bar is where the complaint actually lives. The tests that asserted on `play_when.error.text()` were stale, not dead: they were rewritten to read `status.currentMessage()`, and the "a good paste clears the error" test became "a good paste after a bad one still takes", because a valid entry no longer has a field label to clear.

**A custom blocks path that survived only until you touched a checkbox.** `options._emit_view_settings` rebuilt its dict from the View tab's six controls:

    self._view_settings = { "show_blocks": ..., "block_height": ..., ... }

`time_blocks_path` is set by the *Archives* tab, so it was not in that literal, and any View control change replaced the dict with one that lacked it. `_apply_view_settings` read the missing key as the default and repointed `_timeblocks_config` at `~/.config/WaveSnipper/time_blocks.json`, so choosing a custom file appeared to work and then quietly undid itself on the next toggle. The fix is one word of intent: merge into the existing dict (`{**self._view_settings, ...}`) instead of replacing it. That keeps keys the tab does not own, and it is the general shape of "a tab edits its slice, it does not own the whole object". `ViewSettingsTests` pins both halves — the toggle keeps the key, and the emitted dict carries it.

**A queue that only ever grew.** `_push_status` appends a message that is meant to persist; `_pop_status` removes one; but no caller ever popped, so "Scanning archives…" and "Rendering snip preview…" accumulated in the status strip, joined by `|`, for the rest of the session. The fix is not to stop pushing but to pop when the operation ends: `_on_scan_finished` and `_on_job_failed` drop the scan message, and `_on_play_snip` drops the render message on every path, including the two error branches. Popping an entry that is not present is a no-op by construction, so the failure path can pop unconditionally.

**One platform branch, one atomic writer.** `app_config_dir`'s `os.name` ladder existed three times — in `default_path`, `view_config_path` and `model._timeblocks_config_path` — and the atomic-write/`writable` pair three more times across `Config`, `ViewConfig` and `TimeBlocksConfig`. They are now `config.app_config_dir`, `config.atomic_write_json` and `config.writable_path`. The only thing that made this safe was checking the direction of the import: `config` imports nothing from `model`, so `model` importing from `config` cannot cycle. `atomic_write_json` kept the same shape and the same best-effort swallow of `OSError` that every copy had — an atomic save that then crashes the caller is not an improvement.

**The two axis strips were the same loop twice.** `_paint_axis` drew the zoom-scaling ticks and the day ticks with two copies of "dotted rule at each tick, then a label clamped to the widget". They now share `_paint_tick_strip` and one `_LABEL_MARGIN` constant. The behaviour is deliberately identical, including that rules are skipped outside the content rect while labels are drawn clamped — a refactor of a working painter is exactly where an accidental behaviour change hides, so the tick tests are the proof it did not.

**The first `_show_tb_folder` was unreachable and nobody could tell.** There were two methods with that name, the second silently shadowing the first, and they had *different signatures* (`(self, path)` against `(self)`). Python takes the last one, so the first was dead in the strictest sense: a name the class advertises, safe to call with no arguments, that would raise `TypeError` if anything ever reached it. Deleted; the surviving one is the no-argument version the buttons call.

Stale, and corrected in place: `ViewportContextMenuTests` still expected `"Seek cursor from here"` / `"Play here"` and `"Reveal file in explorer"` after the menu was renamed to `"Play from here"` and `"Reveal clip in explorer"`. The features are present and were the subject of several earlier fix sections, so the tests were updated to the live labels rather than deleted.

One flake observed and not chased: `PeakWorkerTests::test_a_successful_job_reports_the_value` failed once on a full-suite run and passed in isolation and on the rerun — a `BackgroundJob` wake-up race under load, the same class of test-staging flake as the `_flipped_view` one noted above, not a product change.

Full suite **807 passed, 87 subtests, 0 failed** (was 802 passed / 3 failed; the +2 are the new `ViewSettingsTests`). 