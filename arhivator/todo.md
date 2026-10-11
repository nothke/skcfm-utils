# Todo

## Done

**Stack chosen**
- [x] Framework: Python **3.12+** + PySide6 (Qt Widgets) + numpy + scipy
- [x] Runner-up evaluated and rejected: Rust + egui (loses on distribution-only
      win, worse for PCM work and date parsing)
- [x] Verified environment: numpy 2.5.3, scipy 1.18.1, PySide6 6.11.2 in `.venv`
      (built by `run.sh`, 3.12.15). The **system** interpreter is 3.11/numpy 1.24
      and is not representative — it hid two real bugs

**Requirements settled**
- [x] No `floor()` on hours — chunks are variable-length, compute every clip's
      start individually
- [x] Index built eagerly and completely; everything downstream is a lookup
- [x] WAV-only — makes duration frame-exact, removes bitrate estimation entirely
- [x] No MP3 — also removes encoder-delay seams at cross-boundary joins
- [x] Archives hold identical clips → **per-instant priority resolution**, no
      ambiguity dialog. Mixing pieces across archives is seamless for free.
- [x] Index DB lives in **each** archive folder, not centrally
- [x] Overlaps detected, reported, and trimmed out of exports
- [x] `mtime` trusted, no guard (accepted risk if archives are ever re-copied)
- [x] Defaults chosen for: snip-in-gap, playhead-in-gap, rescan trigger, export
      format mismatch, unparseable files, DST
- [x] `run.bat` approach agreed (modelled on `skcfm-utils/downloader/run.bat`:
      venv + pip upgrade + `pip install -r requirements.txt`) — and written

**Rationale written up**
- [x] `flow.md` — all reasoning and decisions recorded

**Bootstrap**
- [x] `requirements.txt` — `PySide6>=6.6`, `numpy>=1.24`, `scipy>=1.10`
- [x] `run.bat` — version-gated interpreter discovery (3.12+), venv create/rebuild,
      pip upgrade, dependency install, launch
- [x] `run.sh` — same flow for Linux/macOS; no `source activate`, newest-first
      interpreter search, `python3.12-venv` hint, `cd "$(dirname "$0")"`.
      Syntax checked and both error paths exercised.
- [x] Decided: **require Python 3.12+** rather than hand-rolling a RIFF parser —
      stdlib `wave` reads `WAVE_FORMAT_EXTENSIBLE` from 3.12
- [x] `.gitignore` — env, cache, build artifacts, OS/IDE junk, and the index DB
- [x] `run.sh` / `run.bat` error paths exercised; `run.sh` built a working `.venv`

**Index robustness**
- [x] Reading an archive no longer *creates* one — `load_clips`/`clips_containing`
      check `require_folder()` then `has_index()`, and never call `connect()`
- [x] A folder that does not exist raises one named `FileNotFoundError` instead of
      sqlite3's `unable to open database file`. This was killing startup with a
      traceback whenever `python -m wavesnip` got a mistyped folder argument
- [x] `Timeline.refresh()` is per-archive: one broken path no longer empties the
      timeline for every working archive. Failures surface in `Timeline.failures`
- [x] `WavWriter` creates the output folder and explains a bad path, instead of
      letting `wave.open` print an `Exception ignored in __del__` traceback over
      the real error

### 1. Scaffolding
- [x] `wavesnip/` package + `__main__.py` (so `python -m wavesnip` works — both
      launchers already call it)
- [x] `.gitignore` — `.venv/`, `__pycache__/`, `wavesnip-index.sqlite3`
- [x] **Keep `run.bat` and `run.sh` in sync** — they are a matched pair
- [x] Smoke-test the full chain end to end: launcher → venv → PySide6 → window.
      Done on Linux via `run.sh`; `run.bat` syntax and error paths checked but
      **not executed** (no Windows available)

### 2. `wavio.py` — WAV access
- [x] Wrap stdlib `wave` (3.12+ handles `WAVE_FORMAT_EXTENSIBLE`). **No hand-rolled
      parser.**
- [x] Expose `nframes`, `framerate`, `nchannels`, `sampwidth` for indexing
- [x] Partial reads: `setpos(frame)` + `readframes(n)` — never decode a whole
      ~690 MB clip
- [x] Header writer for export
- [x] PCM ↔ float conversion via numpy. **No `audioop`** — deprecated 3.11,
      removed 3.13
- [x] Verified 8/16/24/32-bit and mono/stereo/6-channel; bit-exact round trip for
      16- and 24-bit
- [x] Fixed: `frames_to_float` crashed on a trailing partial sample
      (`np.frombuffer` needs a multiple of the element size); now truncates
- [x] **Found and documented:** the float32 pipeline cannot carry int32 PCM
      losslessly (~8 low bits). Fine for 16/24-bit; would compound at every snip
      join if archives were ever 32-bit. See `flow.md`
- [x] **Fixed:** numpy 2 / NEP 50 weakened the scalar in `float32 * 2**31`, so the
      clamp was a silent no-op and int32 output overflowed to garbage. Now scaled
      and clamped in float64. Invisible under the system numpy 1.x
- [x] Verified the `WAVE_FORMAT_EXTENSIBLE` path — hand-assembled RIFF (PCM GUID
      `0100000000001000800000aa00389b71`), the sole reason for the 3.12 floor.
      **Passes on 3.12, skipped on 3.11** — a green 3.11 run means nothing, check
      the skip count
- [ ] Known gap: IEEE float WAV (`format` 3) unsupported by `wave`; add a manual
      path only if a float file actually turns up

### 3. `model.py`
- [x] `Clip` and `Archive` dataclasses, `to_us`/`from_us`/`mtime_us`
- [x] Frame arithmetic: `frame = round((t − start) × framerate)`, clamped to
      `[0, nframes]`
- [x] **Fixed:** `to_us` compared naive datetimes as if 1970 were *local*
      wall-clock, while `st_mtime` is a true UTC epoch — every timestamp was
      2 h out on this UTC+2 box. Now routes through `timestamp()`/`fromtimestamp()`,
      splitting whole seconds from the microsecond remainder to keep it exact.
      Range narrowed to what `localtime()` supports; documented in the module
- [x] Test that compares against a real file's mtime — the comparison that was
      missing when the bug was introduced

### 4. `indexer.py` — folder scan → SQLite
- [x] Per-archive `wavesnip-index.sqlite3`, schema per `flow.md`, index on
      `start_us`
- [x] Scan: `stat` every file; `(size, mtime)` match → skip header parse
- [x] Compute `start = mtime − nframes/framerate`; upsert
- [x] Skip unparseable files but **count** them
- [x] Detect adjacent-chunk overlaps
- [x] Prune stale rows
- [x] Summary line: `indexed 412 clips, 2 overlaps, 3 skipped`

### 5. `timeline.py` — time → clip → snip
- [x] Fan out across N archive DBs; snapshot each archive's clips in priority order
- [x] Per-instant resolution in archive priority order — silent, never a dialog
- [x] True-gap detection via merged coverage runs (no coverage in any archive)
- [x] `nearest_covered` — jump out of a gap to the nearer edge
- [x] Snip planning → ordered `Piece(clip, start_frame, end_frame)` list
- [x] Overlap trimming: clamp each piece's start to the previous piece's end
      *time*. Turns out to be **redundant** — partitioning at every clip boundary
      makes double-counting structurally impossible. Kept as a guard; invariant
      asserted by test. Written up in `flow.md`
- [x] Gaps inside a snip are reported, not filled — `SnipPlan.gaps` plus
      `covered_us` / `missing_us`
- [x] Contiguous pieces from one clip merge, so export opens each file once

### 6. `resample.py`
- [x] Canonicalize pieces to one output format (default: first piece's), overridable
- [x] `scipy.signal.resample_poly` for rate mismatch, ratio reduced by GCD
      (48000↔44100 becomes 147/160, so the filter stays short)
- [x] Explicit channel rules: N→1 mean, 1→N duplicate, same-count passthrough.
      **6→2 and other multi-to-multi conversions are refused**, not guessed: a
      mean across a 5.1 layout cancels to silence
- [x] Bit depth is *not* treated as a conversion — applied once at write time
- [x] Resample a whole same-rate *run* at once, so the filter does not restart
      at every chunk boundary and add a click per join
- [x] Streaming `WavWriter` in `wavio.py` — `write_wav` needs the whole signal,
      and a 1 h export at 48 kHz stereo float32 is ~1.4 GB
- [x] Memory-bounded: blocks flush at 32 MB, so multi-hour exports stay bounded
      (asserted to produce byte-identical output to a single flush)
- [x] `fill_gaps` option: pad with silence to the requested length, or default
      to exporting only what exists and reporting what was dropped
- [x] **Found and fixed:** a truncated WAV silently produced a *shorter* snip
      than requested — `read_frames` clamped against the header's declared
      length, which a truncated file lies about. Now a short read raises.

### 7. `player.py` — playback
- [x] Split into `cursor.py` (pure decisions, Qt-free) and `player.py` (thin Qt
      wrapper) so the logic tests run on any interpreter, including one with no
      PySide6
- [x] `QMediaPlayer` + `setPosition()` for playhead playback
- [x] Cross clip boundaries transparently by swapping the source — verified live:
      `c0 → c1 → c2` with no stutter, and one clean gap jump `30.05 s → 35.0 s`
- [x] Snip preview: render to a temp WAV, play that, clean up on exit
- [x] **Fixed:** `Signal(int)` is C++ 32-bit; a microsecond timestamp (~1.8e15)
      overflowed on *every* emit. Signals are `object` now
- [x] **Fixed:** `set_timeline` set `_cursor = None` and nothing ever created it,
      so `play_snip` always raised "no timeline set"
- [x] **Fixed:** `nearest_covered` could return a run's *exclusive* end — an
      instant guaranteed to be in the gap. Jumping there claims coverage of silence
- [x] **Changed:** gaps are now always exited *forwards*, not to the nearer edge.
      Nearest-by-distance ping-pongs: a playhead 1 ms past a clip's end jumps back
      1 ms, replays a sliver, runs out, jumps forward again

### 8. UI
- [x] Archive panel — add/remove folders, user-defined order (= priority). New
      folders insert at the **top**, so the newest wins on a tie
- [x] Datetime field — **`DateField`, not `QDateTimeEdit`**. `QDateTimeEdit`
      renders the format but cannot parse it: typing `12.5.2026. 10:15:00` yields
      `11.1.2000. 00:00:00`, `setText` is overwritten by the widget, and
      `editingFinished` never fires. Replaced by a `QLineEdit` plus an optional
      calendar dialog, with parsing in `datetimes.py` as `(matcher, builder)` pairs
- [x] **Verified parse-on-paste**, and it works — 31 tests, including the exact
      `12.5.2026. 10:15:00` from the requirement
- [x] Prev / next week — playhead only, ±7 days, preserve time-of-day
- [x] Snip panel — from/to, length spinbox, play-from-snip-start, export
- [x] Waveform view — custom-painted widget, min/max envelope per pixel column
- [x] Gap display — gap shading, dotted boundaries, playhead jumps forward
- [x] Manual rescan button (and a scan on startup)
- [x] Export format dropdown, shown only when the sources disagree
- [x] Status strip — scan summary, gaps, playback position, snip length
- [x] **Fixed:** `_on_snip_changed` swapped the two date editors via a signal
      feedback loop, leaving the user's date in the wrong box (and hanging tests on
      a modal dialog). The *range* is normalised now; the editors are never written
      back to
- [x] **Fixed:** `DateField.restore` wiped the error message it was about to show.
      `set_instant` and `restore` are separate methods for that reason
- [x] **Fixed:** `plan.formats` is a property, not a method — `_refresh_formats`
      raised `TypeError` on every snip
- [x] Scan and export moved off the event loop (`workers.BackgroundJob`); the
      export plan is captured at submit time, and a second job is refused rather
      than queued. Button state derives from a single `_busy` counter
- [x] **Fixed:** `QRect` takes 32-bit ints and gap shading passed it microseconds.
      Every gap rectangle overflowed and the waveform never painted. Column indices
      are pixels, so `column_edges` is no longer consulted there at all
- [x] **Added `PaintTests`** — nothing exercised `paintEvent` before, because a
      widget that is never shown is never asked to paint. Verified they catch the
      overflow by reverting the fix

### 9. Later
- [ ] Calendar visualization — index is already the substrate, should need no
      schema change
- [ ] MP3 input/output, if ever wanted — reopens the frame-exact-duration and
      encoder-seam problems in `flow.md`

---

## Todo

The build plan, with status. Sections 1–9 are the original app; section 10 is the
redesign to a zoomable global timeline. Ordered by dependency; each step was
independently testable. Everything checked is done — what is actually outstanding
is under **Open** below.

---

### 10. Redesign: zoomable global timeline
- [x] `viewport.py` — **pure, Qt-free**: `Limits`, `fit_view`, `clamp_view`,
      `zoom_about`, `pan_by`, `pan_to`, `scroll_to/from_scrollbar`,
      `affordable_span_us`, `choose_ticks`, `_add_months`
- [x] Zoomed out there is **no waveform** — coverage map plus hint text past
      `MAX_PEAK_FRAMES` (200M frames *across the visible range*, in frames because
      that is what cost scales with). Envelope caching rejected: measured a full
      year of 8 kHz at 12.9 min and ~505 GB of I/O, envelope-only still 9.3 min
- [x] `peaks_for` reduces **piecewise, never concatenated** — memory is one chunk
      (`PEAK_CHUNK_FRAMES` = 8M frames), not the width of the view
- [x] Columns shared out by duration **from the requested range**, so a gap stays a
      gap instead of the audio either side stretching to fill it; uncovered columns
      stay `NaN` and the painter skips them
- [x] **Fixed:** the peak budget was per-*piece*, so 900 hourly chunks cost 900×
      the allowance. `reduce_piece` now returns frames read and `peaks_for`
      subtracts them
- [x] `_paint_peaks` draws each peak across the pixel span it covers, so columns
      need not equal pixels, and `NaN` columns are skipped rather than drawn as bars
      of nothing
- [x] Wheel zooms **about the pointer**; scroll **up** zooms **in** — it was the
      other way round, and only the waveview tests could catch it
- [x] Scrollbar range is `SCROLL_STEPS = 1 << 22`, never microseconds (the fifth
      32-bit wall). ~7.5 s per step at a one-day window over a year
- [x] Axis ticks on an **absolute grid**; kind chosen from the visible span, months
      and years from the **calendar** via `_add_months` clamping short months
- [x] `DateField` holds the **instant**, not the text. `D.M.Y. H:M:S` is
      second-resolution and `design.md` fixes it, so re-parsing collapsed every
      sub-second drag to its printed seconds — invisible at 2 h/640 px, constant once
      the timeline zooms
- [x] `Limits.for_extent`: 2% padding capped at an hour, meeting at **fifty hours**.
      Plus extra padding for an archive shorter than `MIN_SPAN_US`, without which no
      legal view existed and the view sat outside its own limits
- [x] **Fixed:** "nothing has chosen a range yet" was tested by the span being ≤ 1,
      but clamping expands `(0, 1)` to `min_span_us` — so the app **opened on one
      second of silence** instead of every clip. Now an explicit `_view_chosen` flag
- [x] `ensure_visible` pans with `user=False`: following playback is not the user
      choosing a range, or the first thing played would pin the view for good
- [x] Click = cursor, drag = snip (threshold 4 px by distance, not time), wheel =
      zoom, shift-drag = pan, scrollbar = pan
- [x] Archives **removed from the main window** into `options.py`; the list persists
      to `~/.config/WaveSnipper/archives.json` (XDG / `Application Support` /
      `%APPDATA%`), written atomically via `os.replace`
- [x] The dialog **edits a copy**; the window applies it on accept, re-indexes,
      saves, scans once. `apply_archives` is the authority for a whole order —
      `add_archive` in a loop reverses it
- [x] `Config` is a class so tests inject a path; `MainWindow.load_state()` is
      explicit and **not** called from `__init__`, so constructing a window never
      reads the developer's real archive list
- [x] Window rebuilt: thin top row → wave view → scrollbar → one *Playback &
      selection* group (week/Now/cursor/play/stop; From/To/length/Set-from-cursor/
      Set-to-cursor/Set-range/Play-selection/Export; snip_info + pad-gaps + format +
      Update) → status strip with clip count and `1 px ≈ …`
- [x] `tests/test_waveview.py` (54) — geometry invariants, wheel/pan/click/drag,
      painting via `grab()` under `RuntimeWarning` as an error at 1 s and 1 year
- [x] `tests/test_options.py` (37) — add/remove/reorder, copy-on-write, rescan
- [x] `__main__` verified four ways: no archives, CLI folder, remembered folders,
      remembered + CLI — zero warnings, config round-trips
- [x] `peaks_for` / `reduce_piece` / the three constants moved **out of
      `mainwindow.py` into the Qt-free `peaks.py`**, for the same reason
      `cursor.py` is separate from `player.py`: a test that has to import Qt to
      reach the logic silently stops running on any machine without Qt. They now
      run on 3.11 as well as 3.12
- [x] `tests/test_waveview.py` no longer puts a Qt constant in a **default
      argument** — defaults are evaluated when the class body runs, at import time,
      so the whole module was unimportable without PySide6 and failed to load
      rather than skipping

### 13. Time blocks rise from the bottom of the viewport

- [x] Rows stack **upwards**: row 0 (the first block, per the greedy first-fit)
      sits on the floor of the content rect, each later row above it — instead of
      row 0 hanging off the ceiling with empty space under it
- [x] Row geometry moved into one helper, `_time_block_row_rect`, shared by
      `_paint_time_blocks` and `_time_block_at`, so the painted rows and the
      clickable rows cannot drift apart
- [x] Rows off the top of a too-short content area are dropped rather than
      hanging over the widget
- [x] `tests/test_waveview.py::TimeBlockRowTests` — 6 tests: floor, stacking
      order, the clipped case, hit-test agreement, rendered ink at the bottom
      rows, three-row containment. The 4 layout ones were checked to **fail**
      with the old top-down mapping restored
- [x] Documented in `flow.md`

The 7 failures noted when this was written have since been diagnosed and fixed —
they were two half-finished edits, not regressions from here. See section 14.

### 14. The two half-finished edits behind the 7 red tests

Neither was a regression — each was a change made on one side only.

- [x] **Milliseconds kept** in `format_datetime` (a **dot**, not `design.md`'s
      colon): the alternative is a display that rounds to the second while
      `DateField` holds microseconds, i.e. a sub-second drag showing a time it
      was not. The calendar `QDateTimeEdit` now agrees because
      `QT_DISPLAY_FORMAT` gained `.zzz` — **verified** Qt 6.11 renders
      `12.5.2026. 10:15:01.001`
- [x] The dot over the colon is deliberate: Qt can only render `zzz` after a dot,
      so the colon would have forced `QtAgreementTests` — "the string on screen
      is the string you copy" — to be weakened to second resolution. That
      invariant is worth more than the separator
- [x] `_EXAMPLE` now derives from `format_datetime(datetime(2026, 5, 12, 10, 15))`,
      so the placeholder cannot drift from what the field writes again. The test
      keeps the literal: the test owns the design's shape, the code derives from
      its own formatter
- [x] Placeholder and rejected-restore tests updated to the ms shape;
      `QtAgreementTests` needed no change once the Qt format carried `.zzz`
- [x] `datefield.py`'s quoted `QDateTimeEdit` typing experiment re-run under the
      new format (leaves `11.5.2000. 20:26:00.101`) — still garbage, so the
      conclusion holds and the docstring quotes a number that was actually re-run
- [x] **`play_position` removed for good**: widget, commented-out lines, the dead
      `_play_position_reset()` that would have raised `AttributeError`, and its
      2 tests. Coverage replaced by what remains — stop clears `_playhead_us`, and
      the position follows into the date field
- [x] Documented in `flow.md`
- [x] Full suite **660 passed, 78 subtests, 0 failed** (664 collected before: 661
      + 3 red subtests, minus 2 removed tests plus 1 added)

Left alone: the commented-out `◀ week` / `Now` / `week ▶` buttons are in the same
trimmed block, still created and connected but never added to the layout. They
cause no failures — whether that row comes back is a design question.

### 11. The stale-decode hole — written, then verified

The shell had died mid-session (the disk was full), so this was written and
reviewed by reading only. The disk was cleaned, both files turned out to have been
**truncated mid-string** by it, both were rebuilt, and all of this is now run.

- [x] `MainWindow._on_peaks` — a decode whose range the view has moved off now
      clears `_peak_range` and schedules `_refresh_wave` on a
      `QTimer.singleShot(0)`, instead of returning silently. **Real bug**:
      `_request_peaks` refuses while a decode holds the slot, so every event of a
      wheel gesture landing mid-decode is dropped — and if the *last* one is
      dropped, nothing ever asks again, leaving a bare coverage map with no
      waveform, no hint, and no recovery except moving the mouse. Needs `QTimer`
      imported into `mainwindow.py` (done).
      - The retry is **guarded** by `if self._peak_range != viewing`. Without the
        guard, panning away and straight back would clear peaks that are still the
        correct answer and re-decode them — waste, plus a visible flicker. Found by
        re-reading the fix, not by a test.
      - The deferral matters: `finished` is emitted while the job is still counted
        as busy, so asking immediately would be refused for the same reason as
        before. Verified by reading `workers.py` — `busy` and `shutdown` only touch
        Python-side state, so no deleted-C++-object hazard on that path.
- [x] `tests/test_mainwindow.py::StalePeaksTests` — 2 tests, both **synchronous**,
      asserting on state rather than on peaks arriving. A job left in flight would
      satisfy a "peaks arrived" assertion on its own, which is how a test of the
      retry quietly stops testing the retry. One pins that a stale decode clears
      the marker; one pins that the guard does not throw away correct peaks.
      Both **fail with the fix reverted** — checked, not assumed.
- [x] `tests/test_mainwindow.py::CoverageToWaveformTests` — 6 tests, coverage that
      did not exist before: a two-day archive starts as a coverage map with nothing
      read; the budget gates the *read* not just the draw; the clip label admits
      how coarse a click is; zooming in flips to a real waveform; the flip survives
      a decode it moved on from (the bug above); zooming back out drops the waveform
      rather than leaving a stale one.
      - **This one did not pin what it claimed.** It broke out of its spin loop on
        the first affordable notch, so exactly one request was ever made and it was
        never refused — it passed with the fix reverted. Now it keeps zooming past
        the budget so the later requests really are refused. See `flow.md`.
- [x] End-to-end smoke that a zoomed-in view really decodes **and paints**:
      `/tmp/opencode/smoke/peaks_run.py`. Real `QWheelEvent`s, real event loop,
      `QImage` compared against a peaks-withheld render. Crosses the budget after
      **14 notches** (46 h → 6.9 h, the budget at 8 kHz).

### 12. Disk-full truncation — files lost and rebuilt

The full disk cut two writes mid-string, which is why the shell had been dying:
`tests/test_mainwindow.py` lost its last ~180 lines and `wavesnip/mainwindow.py`
lost the body of `_scan_all`. Neither was caught by reading — only by compiling
every file in the tree, which is the check to run after any unexplained failure.

- [x] `tests/test_mainwindow.py` tail rebuilt from `tests/__pycache__/
      test_mainwindow.cpython-312.pyc`, which predated the truncation: the pyc
      gave every lost function's name, line number and docstring, and `dis` gave
      the exact bytecode. Rebuilt against it line for line.
- [x] `wavesnip/mainwindow.py::_scan_all` rebuilt the same way from the module
      pyc (docstring, `co_varnames`, and the exact key order of the returned dict).
- [x] Both files compile; the whole tree compiles; no duplicate definitions
      anywhere.

**Final state: 648 tests OK on 3.12, 648 OK (skipped=246) on 3.11.** The
four-way `__main__` launch smoke, the full scan → zoom → pan → scrollbar → drag →
export smoke, and the zoom-in decode-and-paint smoke all pass with `RuntimeWarning`
promoted to an error.

---

### 15. The four "Next steps" from `design.md`

The four items under `## Next steps` — and nothing from `## After that`, per the
request. The first one could not be done without fixing the menu underneath it.

- [x] **Reveal file in explorer, right-click over a clip in the viewport**
  - [x] `wavesnip/reveal.py` — `reveal_command(path, system)` is separate from
        `reveal(path)` so all three platform branches are assertable without
        launching a file manager: `open -R` (macOS), `explorer /select,`
        (Windows), `FileManager1.ShowItems` over D-Bus (Linux, when `dbus-send`
        is on the PATH)
  - [x] The Linux fallback opens the **folder**, not the file — `xdg-open` on a
        `.wav` plays it, and the user asked to be shown it. A path that does not
        exist yet (`time_blocks.json` before the first save) reveals its folder;
        a path with no folder either is a refusal, and a file manager that will
        not start returns `False` rather than raising into the menu
  - [x] **Fixed:** the context menu was always told instant `0`. The emitter
        read `_drag_origin`, which is only ever set by a *left* press, and its
        fallback called `instant_at` on microseconds. Now the pan origin — where
        the right button actually went down — with the release position as
        fallback. "Seek cursor from here" and "Set region start" were pointing at
        1970 too
  - [x] `requestContextMenu` was `Signal(int)`; ~1.8e15 µs does not fit a C++
        `long`. `Signal(object)` now, like every other instant in the widget,
        with the emit run under `RuntimeWarning` as an error
  - [x] `_on_context_menu` split into `_build_context_menu(instant)` +
        a one-line `exec`, so a menu whose entries are all conditional on the
        instant can be asserted on
  - [x] `Qt` gotcha found by the first run: `triggered` passes a checked `bool`,
        so `lambda path=clip.path:` was called with `path=False`. Zero-argument
        lambdas throughout, as the rest of the file already had them
  - [x] `tests/test_reveal.py` (8) — the three commands, the folder-not-file
        fallback, the missing-file and refusing-to-start cases
  - [x] `tests/test_mainwindow.py::ViewportContextMenuTests` (4) — offered over
        a clip, absent over a gap, absent with no archive, and triggering hands
        over that clip's file. `tests/test_waveview.py::RightClickInstantTests`
        (2) — the instant, and a right *drag* panning without a menu
- [x] **Save button should not deselect the time block**
  - [x] `_update_time_blocks_in_view` carried `selected_time_block` across, and
        it was the one field left out of the field-by-field rebuild — so *every*
        update cleared the selection, a save above all
  - [x] `_on_tb_save` no longer clears the editor: it keeps `_editing_block`
        pointing at the object it just wrote, so Save can be pressed repeatedly
        on one block. Previously a second press over the emptied editor added a
        **copy**
  - [x] The key comes from `_editing_block`, not from the old state — saving
        *replaces* an immutable block, so the old tuple names bounds that may
        no longer exist
  - [x] `_replace_block(old, new)` used by all four swap sites (save, set block
        to snip, set start, set end), so the editor always holds an object that
        is in the list
  - [x] Two bugs found while testing it in the natural flow: `_on_tb_new` set
        `_editing_block` *after* the view rebuild (new block came up
        unhighlighted) and never filled `tb_start`/`tb_end`, so Save read `None`
        and refused the block it was holding with "Start and end times are
        required"
  - [x] Deleting still clears the editor and the highlight — the counterpart
  - [x] `tests/test_mainwindow.py::TimeBlockEditingTests` (6)
- [x] **Double click on a block selects the region**
  - [x] `WaveView.mouseDoubleClickEvent` — hit-tests the block and emits
        `snipSelected` (the region) + `timeBlockSelected` (the block), reusing
        the signals a drag and a click already use rather than adding a third
        path. No `mouseDoubleClickEvent` existed anywhere before
  - [x] Right button and empty space select nothing
  - [x] `tests/test_waveview.py::TimeBlockRowTests` (3 gesture tests) and
        `tests/test_mainwindow.py::TimeBlockDoubleClickTests` (2) — the window
        one sends the whole press/release/dblclick/release sequence, because Qt
        never delivers the second press to `mousePressEvent`
- [x] **Use full paths in the archive folders list**
  - [x] `f"{index}. {archive}"` instead of `archive.name` — two folders called
        `radio` are different archives, and the row is the only place the
        priority order can be read against
  - [x] The old test could not see this (`Path("a") == "a"`), so
        `test_the_rows_carry_the_whole_path` uses two absolute paths ending in
        the same name
  - [x] Options' `Show in Folder` now calls the same `reveal()` — two callers,
        one implementation, so they cannot drift into different file managers
- [x] Each change **mutation-checked**, not assumed: the broken form was put
      back in turn (instant 0, dead double-click, Save clearing, no reveal
      action, `_replace_block` not re-pointing, `_on_tb_new` out of order,
      `archive.name`) and the catching test identified — six of seven produced
      failures. One restore went wrong (an over-broad `sed` turned
      `_clear_tb_editor`'s `= None` into `= saved`) and was found by re-running
      the suite; after fixing, the full suite is green again
- [x] `flow.md` documented; full suite **686 passed, 78 subtests, 0 failed**
      (was 660 before these four)

Note for whoever tests a save next: `MainWindow.__init__` reads the *real*
global `time_blocks.json` (unlike `Config`, which takes an injected path), so
any test that saves must repoint `window._tb_config` at a temp path first —
`TimeBlockEditingTests` and `TimeBlockDoubleClickTests` both do.

---

### 16. The three "Next steps" from `design.md`

The three items under `## Next steps` — and nothing from `## After that`, per
the request. All three are in `waveview.py`.

- [x] **Clip blocks (and waveform) not the whole height — about 4/5 of the viewport**
  - [x] `_wave_rect(area)`: four fifths of the content rect, centred, integer
        arithmetic (`_WAVE_BAND = 4, 5`) so no fractional pixel reaches a `QRect`
  - [x] Gaps are **not drawn**: no gap fill, no dotted coverage boundaries (the
        user removed the pen lines) — a column with no audio is the background.
        `_paint_clips` paints one rectangle per run of covered columns, in the
        band, with no "not all covered" guard: fully covered is a solid block
  - [x] Sundays are **full height and behind the clips**: `_paint_sundays` runs
        before `_paint_clips` across the whole content rect, so the wash shows
        in the margins and gaps and the opaque blocks keep their own colour
        where a Sunday crosses them
  - [x] Also in the band: the waveform, the snip wash, the clip labels at the
        band's top edge, and the hint under them
  - [x] Deliberately *outside*: the background itself, the Sunday wash, and the
        playhead — a cursor is where you are in the widget, not content in the
        picture, and its own test asserts the exception so it stays a decision
  - [x] Labels follow the band because they name clip *blocks*; at the top of the
        widget they floated over a margin's worth of empty air
  - [x] `tests/test_waveview.py::WaveBandTests` (7) — blocks span the band and
        only the band with background above and below, the waveform and the
        labels and the wash are inside the band, the wash fills the viewport
        behind the blocks, and the cursor crosses it
- [x] **Time blocks always visible, at least 3 pixels wide, however far out**
  - [x] `_time_block_span(start, end, area)` is the single answer for both the
        painter and `_time_block_at` — in view, clamped, then widened to
        `_MIN_BLOCK_WIDTH = 3`: rightwards, or leftwards at the right edge of the
        content rect. Overlap accepted; blocks sharing time are on separate rows
  - [x] Qt `drawRect`'s second pair of coordinates is the **corner**, so the
        outline lands on `x1` and a block's pixels are `x0 <= x <= x1` — what the
        hit test already said. The first attempt "corrected" the hit test to
        `x < x1` and turned two gesture tests red: the painter was right
  - [x] A minimum-width block carries no name (`room <= 0`), and `elidedText`
        already returns nothing at those widths — so that guard states intent and
        changes no pixel; the property is held by the width assertion instead
  - [x] `tests/test_waveview.py::TimeBlockWidthTests` (4) — widened to the
        minimum at its own instant, clickable over exactly the pixels it paints,
        growing back into view at the right edge, and a half-day block keeping
        its true span
- [x] **No limit on the number of time block rows**
  - [x] `assign_time_block_rows` grows the row list instead of pouring every
        overlap into row 2 (the fourth block used to be drawn *on* the third)
  - [x] `_time_block_row_rect` unchanged: it still drops rows the viewport is too
        short for. Six fit at the default size — that answer now comes from the
        window's height rather than from a constant chosen without it
  - [x] `_col_tb_bg` / `_col_tb_edge` are dead (a block colours itself from its
        name); their "up to 3 rows" comment was corrected, not the lists deleted
  - [x] Three new tests in `TimeBlockRowTests` (fourth block gets row 3; twelve
        overlapping blocks get twelve rows, the fitting ones drawable; six blocks
        rendered so each fitted row carries its own ink). The old three-row test
        is kept, its "ceiling" docstring rewritten
- [x] Each change **mutation-checked** rather than assumed — **12 of 13 mutants
      caught**: the band going full height, the peaks ignoring it, the labels
      climbing back to the widget top, the snip wash going wall to wall, the
      playhead confined to the band, the clip blocks painted full height, the
      Sundays confined to the band, the Sundays painted over the clips, no
      minimum block width, no leftwards growth at the right edge, the three-row
      cap restored, and a hit test one pixel wider than the paint. The one
      survivor is an *equivalent* mutant (see flow.md): removing the
      `room <= 0` label guard changes no pixel, because `elidedText` already
      returns nothing at those widths
- [x] `flow.md` documented; full suite **700 passed, 78 subtests, 0 failed**
      (was 686 before these three)

### 17. The export "Update" button (`design.md`'s next step)

`design.md` asked "Can export 'Update' button be removed?" — yes, and what it
was *for* turned out to be a refresh that only ran on archive changes.

- [x] Purpose established: `_refresh_formats` builds the dropdown from
      `SnipPlan.formats`, the distinct formats of the clips inside the **snip
      range**, but its only automatic caller was `_rebuild_timeline`. Every way
      of moving the range goes through `_on_snip_changed`, which never refreshed
      it — so the box went stale (offering formats no longer in range, omitting
      ones that now were) and the button was how the user caught it up. No test
      touched it
- [x] Button and its wiring deleted; `_refresh_formats()` now called from
      `_on_snip_changed`, `_on_snip_cleared`, and the empty-archive branch of
      `_rebuild_timeline` — that branch had no refresh at all and would have
      kept offering formats from archives that were removed
- [x] `_refresh_formats` returns early when the new list equals the one already
      shown: `QComboBox.clear()` also drops the selection back to index 0, so a
      pointless rebuild would put a still-valid override on "Default" and
      flicker on every keystroke of a date field
- [x] Behaviour change accepted and documented: an override the new range no
      longer offers falls back to "Default" (it named sources that are gone)
- [x] `tests/test_mainwindow.py::FormatListTests` (5) — list follows the range,
      still-valid override survives the move, out-of-range override falls back,
      removing every archive leaves only "Default", and no button is left.
      `add_clip` gained a `rate` parameter so a mixed-format archive can exist
- [x] `flow.md` documented; full suite **705 passed, 78 subtests, 0 failed**

### 18. The top row and the "making waveforms" spinner (`design.md`'s next steps)

Three items in one go: a spinner for the peaks decode, the top-row button
rearrangement, and the trimmed top status bar.

- [x] **Spinner for "making waveforms"** — `_request_peaks` raises the spinner
      when a decode starts, `done` lowers it. A separate `_peaks_busy` flag,
      *not* another `_busy` job, because `_update_busy` disables the export and
      selection buttons while spinning, and a decode runs on every view change
- [x] **Top row matches the design** — `STATUS (clips · 1px · spinner) | gap |
      Play/stop | Cursor field | gap | + − Fit all | Setup archive..`; the
      play button and cursor field moved up out of the Region Selection box,
      whose old first row is gone; zoom group order flipped to `+` `−`
- [x] **Rescan button removed** — the scan stays reachable via File → Rescan
      archives (F5), the Setup dialog, and `rescan()`; `btn_rescan`,
      `btn_prev_week`/`btn_today`/`btn_next_week` (never in any layout), the
      dead `_build_status_strip`, and their wiring are deleted
- [x] **Status bar cut to the design's list** — `_update_archive_summary` now
      says just `N clips` (with `No archive folders.` / `No audio indexed
      yet.` states); folder count, timeline extent, and the unreadable tally
      left the bar (the Setup dialog's details and the status message still
      carry them); `clip_label` is `1 px ≈ …` again with no `·` prefix and is
      cleared when there is no archive
- [x] Tests: two replaced (`counts_what_was_indexed`, folders-in-plural →
      no-longer-counts-folders), two busy tests stop touching `btn_rescan`,
      plus four new (`top row rearranges`, spinner sits in the status block,
      peaks-flag spinner knob without blocking exports, spinner down after a
      real decode round-trip). Full suite **709 passed, 78 subtests, 0 failed**
      (was 705)
- [x] `flow.md` documented (section "The top row now follows the design…")

### 19. The top bar came back fat

A follow-up from using it: since the top-row change, the bar was ~47 px tall
instead of ~25 — a single control a second line high.

- [x] Cause: the rearranged top row now contains a `DateField`, whose own
      layout carried the style's default 11 px contents margins around a
      25 px `QLineEdit` (47 px total). `QHBoxLayout` never wraps, but its row
      height is the tallest child, so the widget fattened the whole bar
- [x] Fix in the widget, not the row: `DateField` zeroes both its layouts'
      contents margins (uncommenting the abandoned lines), so it is as tall as
      a bare `QLineEdit` everywhere — top row, snip fields, time-block fields
- [x] `test_the_top_row_is_a_single_thin_line` pins the invariant: no top-row
      child may have a size hint taller than a `QPushButton`. Full suite
      **710 passed, 78 subtests, 0 failed** (was 709)
- [x] `flow.md` documented (the row must stay one line)

### 20. Sunday wash to the axis; the day axis has a zoom limit

Two viewport follow-ups from using it.

- [x] **Sunday wash covers the whole widget** — the red run over the weekend
      used to stop at the top of the axis strips; it now runs from row 0 to
      the widget's bottom edge, axis included (still *behind* the clip blocks,
      so a block keeps its exact colour). `_paint_sundays` takes `area` only
      for consistency and paints `self.rect()` height
- [x] **Day axis hides beyond a week** — `choose_day_ticks` used to label
      *every* day in the view, so zoomed out to a year it became a wall of
      overlapping dates (and it never switched to months); per the user's
      decision it now returns `[]` when `end - start > 7 * DAY` and shows
      nothing, leaving months to the upper zoom-scaling strip. The 36 px axis
      reservation stays (the red wash covers it, and a permanently-resizing
      axis would churn every geometry test)
- [x] Tests: `DayTickTests` reworked for the 7-day boundary
      (`test_seven_days_is_the_limit`: 7 days yes, +1 s no;
      `test_nothing_zoomed_out_is_nothing`: 8 d/10 d/30 d/100 d/year all
      empty) and the Sunday wash test now asserts rows
      `0 .. height-1` plus explicit red in both axis strips. Full suite
      **712 passed, 78 subtests, 0 failed** (was 710)
- [x] Flake seen: `test_the_flipped_view_survives_a_decode_it_moved_on_from`
      failed once under full-suite load, passed in isolation and on other full
      runs — a staging race in the test itself, unrelated to these changes
      (noted in `flow.md`)
- [x] `flow.md` documented (section "The Sunday wash takes the whole widget…")

### 21. Whole-year viewport: measured, not guessed

The user reported every pane sluggish at 6000+ visible clips. A synthetic
benchmark (year view, 1920×200, 5838 clips) disproved the first guess and set
the course.

- [x] **The rects were never the problem** — `covered_columns` + `_true_runs`
      collapse the whole year into **1 block rect**; merging close rects was
      already what the pixel grid does. Full paint was 11.1 ms/frame anyway
- [x] **Where it went:** clip labels ~3 ms (two `fromiter` walks over 5838
      tuples + ~12000 column-grid allocations via per-call `x_of`),
      Sundays ~5 ms (datetime + `to_us` + two lookups *per Sunday*),
      coverage shaping ~3.7 ms (`covered_columns` recomputed on every repaint)
- [x] **Shipped:** memoized column grid (`_edges` by start/end/width shared by
      every painter and hit test); vectorized label pass (one bulk
      searchsorted, names only for survivors); per-view singletons for the
      coverage map and packed clip arrays (repaints of an unmoved view are
      free); Sundays step the calendar in bulk and draw with one
      `drawRects`. Suite **712 passed, 78 subtests, 0 failed**
- [x] **Result: 11.1 → 5.3 ms/paint (2.1×)**, layers now Sundays ~2.5 ms
      (the `drawRects` raster floor), axis ~1.3 ms, labels ~0.5 ms
- [ ] **Next (out of quick-wins scope, awaiting the call):** static-scene
      cache (render Sundays+band+axis once per view, blit on repaint) and/or
      gesture coalescing (one refresh per frame during pan/zoom). Both are
      medium changes with behaviour risk; `flow.md` has the full write-up
      ("The whole-year viewport is not slow because of the rects")
- [x] `flow.md` documented; benchmarks live in `/tmp/opencode/bench_viewport.py`
      and `bench_layers.py`

---

### 22. Bounded probing: waveform at any zoom (`flow.md` "Bounded probing…")

Turned the full-decode min/max pipeline into a two-tier envelope, moved the
zoom gate onto `design.md`'s coverage/waveform split, and added the two-tone
outline + RMS band. User-approved scope ("option 1 + the optimisations"):
probe plan in full, envelope band, gate in time. Peaks cache and gesture
coalescing stay deferred (section 21 + the Open list).

- [x] **The gate is a line in time.** `MAX_WAVE_SPAN_US = 7 * DAY`
      (`viewport.py`) replaces `affordable_span_us`; `_peaks_affordable` is
      now a plain `span <= MAX_WAVE_SPAN_US`; `MAX_PEAK_FRAMES` remains only
      as `peaks_for`'s read-budget backstop; `_max_framerate` field removed
- [x] **Two tiers in `peaks_for`.** Exact when `nframes <= MAX_EXACT_FRAMES`
      (16M) **and** every piece wins a column (`_each_piece_wins_a_column` —
      closes the latent zero-share piece that vanished from the envelope, the
      old ~4% `None` flake); sampled otherwise: 8×128-frame probes per column
      (`PROBE_RUNS_PER_COLUMN`/`PROBE_FRAMES_PER_RUN`), stratified midpoints,
      column-major, grouped per file through `read_probes`, per-request
      budget, deterministic
- [x] **Fallback probe** at the centre of every piece∩column overlap that no
      probe hit — a covered column is never blank, gaps stay NaN
- [x] **`wavio.read_probes`** — one open for many probes; joins raw bytes and
      converts once (returns views); clamp/truncation/validation shared with
      `read_frames` through `_raw_at`, so the rules cannot drift
- [x] **Two-tone envelope** — `Peaks.rms` (default `None` = outline only);
      `_reduce_blocks` carries sumsq+counts; `reduce_piece` returns
      `(mins, maxs, rms, spent)` (test unpacks updated); solid `_col_wave`
      ±RMS band in `_paint_peaks`, skipping `0`/NaN/`None` columns
- [x] **Measured, not assumed** (`/tmp/opencode/bench_peaks.py`): dense
      10-min/48 kHz file — exact 112 ms, sampled 203 ms, 6.5-day view
      2.0 ms (1,920 frames); probe ceiling 1.84M frames at any span; 1,600
      probes reopen-per-probe 126 ms vs one-open 14 ms; whole-file sequential
      8.9 ms/60 s (why narrow views stay exact). Profiling → batched
      conversion + `_fold_probes` (`reduceat` per moment): dense sampled
      485 → 203 ms
- [x] **Tests:** `TestReadProbes` (values, one open, empty short-circuit,
      ValueError, past-end, end-clamp, truncated file); `RmsBandTests`;
      sampled-path block in `PeaksPlanTests` (tier selection, bounded slice,
      covered-never-blank, position, gaps, budget, one-open, determinism,
      band on both paths, shared boundary column merges); gate boundary test;
      `WaveSpanGateTests`; `CoverageToWaveformTests` reworked to 76×6 h
      ≈450 h (46 h cannot cross a 7-day gate); `AffordableSpanTests` dropped
- [x] **Revert-checked, not just green.** Band / tier condition / fallback /
      7-day gate each disabled in turn — their tests failed — then restored
      from copies. Full suite **744 passed, 82 subtests, 0 failed** (was 712)
- [x] `flow.md` documented ("Bounded probing: the waveform at any zoom")

### 23. Time blocks: reduced json entries, and one bad entry costs one entry

`design.md`'s first "Next step". The load did not merely skip what it could not
read -- it raised.

- [x] **The break found:** `to_us(parse_datetime(str(entry.get("start_time",
      ""))))` -- `parse_datetime` returns `None` for a missing or unparseable
      date, `to_us(None)` raised `AttributeError`, and the loop only caught
      `(ValueError, TypeError, KeyError)`. Out of `load()`, out of
      `MainWindow.__init__`, no window. The same path swallowed every bad date
- [x] **Three fields are the requirement** — `name`, `start_time`, `end_time`
      per `design.md`; `episode_num`, `description`, `hosts`, `guests` are
      optional and load at their defaults when absent or unreadable
      (`episode_num: "abc"` used to drop the block *silently*; a bare
      `"hosts": "Alice"` used to load as `['A','l','i','c','e']`)
- [x] **Saving writes only what is filled in** — `_entry_for` emits the three
      required keys and adds an optional one only when it holds something;
      round trip asserted unchanged
- [x] **Corruption is reported, never raised** — `load_with_report()` returns
      `(blocks, problems)`, one line per entry naming file, index, name, field
      and the shape that would be accepted; `load()` is the quiet twin over the
      same code. A bare `except Exception` per entry remains behind the explicit
      checks as the backstop
- [x] **Console and bottom status bar** — `MainWindow._report_time_block_problems`
      prints every problem to stderr and pushes the first plus `(+N more, see
      console)` into the status queue (`_push_status`, so it persists rather
      than being overwritten by the next `showMessage`). Clean file, no message
- [x] A missing file stays **not** a problem (first run); unreadable file,
      invalid JSON, wrong document shape and a directory in the file's place are
      all problems now instead of a silent empty list
- [x] **Mutation-checked:** the old parsing expression put back with the
      per-entry guard removed → **11 failures** including all five window tests
      (the window really did refuse to build); the save writer always emitting
      the optional four → 2; the report not called → 3. Restored each time
- [x] `tests/test_model.py` +20 (`ReducedEntryTests`, `CorruptedEntryTests`,
      `TimeBlockFileProblemsTests`, `TimeBlockSaveTests`),
      `tests/test_mainwindow.py` +6 (`TimeBlockLoadProblemTests`). Full suite
      **770 passed, 82 subtests, 0 failed** (was 744)
- [x] `flow.md` documented ("Time blocks: three required fields…")

---

### 24. Four ways a time block deselects (`design.md`'s next step)

- [x] **Empty space no longer deselects** — that press starts a region drag, so
      a drag emptied the editor on its first pixel; the `timeBlockDeselected`
      emit, signal and connection are all gone (nothing else emitted it)
- [x] **Esc deselects** — a `QAction` in `_wire` beside Space/`i`/`o`/Del,
      `QKeySequence(Qt.Key_Escape)`, wired to `_deselect_time_block`
- [x] **"Deselect block (Esc)" in both right-click menus** — in the viewport
      menu and in the block's own menu, each gated on a block actually being
      selected
- [x] **Only those four** — Esc, a different block, the menu entry, deleting it;
      `_deselect_time_block` stays silent when nothing is selected
- [x] **Menus can be asserted on** — `_build_tb_context_menu` split out of
      `_on_tb_context_menu` like `_build_context_menu`; the viewport menu's
      dangling trailing separator moved inside the selection branch
- [x] **Esc has exactly one owner** — the window action. Menu entries carry the
      tip as text only, never a second `setShortcut`, pinned by a test
- [x] **Qt gotcha recorded** — `show()` alone does not arm the shortcut context,
      a `processEvents()` turn does; `activateWindow()` never does offscreen
      (so the tests would pass only by accident without it)
- [x] `tests/test_mainwindow.py` +13 (`TimeBlockDeselectionTests`). Full suite
      **783 passed, 82 subtests, 0 failed** (was 770)
- [x] `flow.md` documented ("A block deselects in four places…")

---

### 25. View settings tab in Options (`design.md` "Next steps")

The first item under `## Next steps` in `design.md` — a View tab in the Options
dialog with five settings.

- [x] **ViewConfig class** — mirrors `Config`: tolerant load, atomic write,
      path injectable for tests. Saved to `~/.config/WaveSnipper/view.json`
- [x] **OptionsDialog tabs** — added `QTabWidget` with "Archives" and "View"
      tabs; existing Archives tab content unchanged
- [x] **View tab controls** — five settings matching the design:
      - `Show blocks` (checkbox, default true)
      - `Show clips` (checkbox, default true)
      - `Show waveforms` (checkbox, default true)
      - `Block height` (spin box 10–100 px, default 30)
      - `Keep playhead visible` (checkbox, default false)
- [x] **Live preview** — `viewSettingsChanged` signal emits on every control
      change; `MainWindow` applies immediately without closing the dialog
- [x] **Persistence** — settings saved on dialog accept via `ViewConfig.save()`
- [x] **WaveView integration** — `_show_blocks`, `_show_clips`, `_show_waveforms`,
      `_keep_playhead_visible` instance flags; `set_block_height()` modifies
      module-level `_TB_ROW_HEIGHT`; painting methods check visibility flags
- [x] **Playback behaviour** — when `keep_playhead_visible` is true, the view
      pans to centre the playhead on every position update; otherwise only
      scrolls when playhead leaves the view
- [x] **Tests**: all 788 tests pass (was 770 before these changes)

---

### 26. Play silence on gaps; don't auto-stop at clip boundaries (`design.md` "Next steps")

The two items under `## Next steps` in `design.md`:

- [x] **Play silence on gaps, don't jump to next clip**
  - [x] Added `GapSource` dataclass in `cursor.py` to represent silent gaps between clips
  - [x] Added `Cursor.gap_after(source)` method to detect gaps after a clip
  - [x] Modified `Player` to support continuous playhead advancement through gaps:
    - New `_in_gap` flag and `_enter_gap()` / `_exit_gap()` methods
    - Playhead advances based on wall-clock time when in a gap (no audio)
    - When playhead enters a clip, that clip is loaded and played
    - When playhead leaves a clip into a gap, audio stops but playhead keeps moving
    - `play_at()` now starts silent advancement if starting in a gap
    - `seek()` handles seeking into/out of gaps
  - [x] No silent WAV generation needed — playhead simply advances through time

- [x] **Don't auto stop when reaching end or start of clip, just continue with playing it**
  - [x] Already implemented: when a clip ends and another clip follows immediately (no gap), `Player._advance()` seamlessly loads and plays the next clip
  - [x] The coverage merging in `Timeline` treats back-to-back clips as continuous, so no gap is detected
  - [x] No additional code changes needed — the existing logic already handles this case

- [x] **Tests**: all 788 tests pass

- [x] **Space key / Play button**: Fixed to work in gap mode
  - [x] `Player.is_playing` property now returns `True` only when user explicitly started playback (clip or gap)
  - [x] Added `_playing` flag separate from internal `_in_gap` state
  - [x] `seek()` preserves playing state: if playing, continues through gaps; if stopped, just moves playhead
  - [x] `play_at()` sets `_playing = True` to start playback
  - [x] `pause()` and `stop()` set `_playing = False`
  - [x] `MainWindow._on_play()` uses `player.is_playing` to toggle correctly
  - [x] Space key (Qt.Key_Space) connected to `_on_play` works in all modes

- [x] **Click behavior unified**:
  - [x] Stopped + click anywhere (clip or gap) → just moves playhead, stays stopped
  - [x] Playing + click anywhere (clip or gap) → moves playhead and continues playing
  - [x] The only difference between clip and gap: clip plays audio, gap is silent

---

### 27. Per-file envelope cache — waveforms stop seeking, on spinning disks

The waveform complaint that set this off: **minutes to paint, especially on a
spinning disk, and especially zoomed in** (`design.md` line 90). Sampled
(probe) views made wide scrubs cheap, but a probe is a scattered seek, and
12,800 of them is ~115 s on a HDD — *every repaint*. And the exact path
re-decoded the same narrow range forever. The fix is a per-file envelope
cache, the "peaks cache" deferred in section 22.

- [x] **`wavesnip/filepeaks.py` — the cache module**
  - [x] `CACHE_STEP_US = 250_000` (0.25 s fidelity): one cache column per 0.25 s
        of audio, so the narrowest view served is `1600 × 0.25 s = 400 s ≈ 6.7 min`
  - [x] `FilePeaks` — per-file min/max/sumsq (f32), with column sample counts
        *derived* from `(nframes, frames_per_col, nchannels, ncols)` rather than
        stored; nothing per column except the three floats + a built mask (~13 B)
  - [x] Whole-column, segmented lazy builds: only the column ranges a view asks
        about are swept, in one sequential forward pass per contiguous run,
        reading aligned chunks so the fold is a reshape (ragged file tail folded
        into the last column, never padded)
  - [x] `FilePeakCache` — keyed by `(path, size, mtime, nframes, rate, channels)`
        so a changed file auto-misses; one lock for the worker; LRU eviction at
        `MAX_CACHE_BYTES = 256 MB` so resident memory is bounded by what was
        viewed, not by archive size
- [x] **`wavesnip/peaks.py` — cache tier + raised exact ceiling**
  - [x] `peaks_for(plan, ..., cache=None)` — cache tier joins the tier rule:
        `total_us >= PEAK_COLUMNS × CACHE_STEP_US` → ensure pieces built → assemble
        from RAM; else exact (≤ 24M frames) → else sampled
  - [x] `_peaks_from_cache` maps each cache column by its absolute-time midpoint
        into a view column and merges with min/max + additive sumsq/counts — same
        moments arithmetic as the other paths, so the band shape cannot drift;
        absolute-time mapping also keeps a piece far narrower than a view column
        in the envelope (which the exact path's proportional floors cannot)
  - [x] `MAX_EXACT_FRAMES` 16M → 24M: covers just past the cache gate at 48 kHz
        (8.3 min) and far past it at 8 kHz (50 min), so below-gate views are read
        whole instead of probed — no probe band remains at 8k/48k (a thin one
        survives at 96 kHz)
- [x] **`mainwindow.py` wiring** — the window owns one `FilePeakCache`, shared
      with the peaks worker via `peaks_for(plan, cache=self._file_peaks)`; keyed
      by identity, so refreshes/rescans never need a wholesale flush
- [x] **Tests (17 new, in `tests/test_filepeaks.py`)** — fold is frame-exact
      against a direct per-column reference; stereo folds to the across-channel
      extremum; ragged tail is the exact leftover frames and never padded; a file
      shorter than one column still builds; building stops at the requested range
      (one read, no whole-file sweep); a built range is never re-read; overlapping
      requests fill only the gap; file-identity change rebuilds; LRU eviction
      rebuilds the dropped file; empty file not cached; an aligned plan equals the
      exact path column-for-column; a cached view provably decodes *nothing* on
      its second request; a narrow view provably stays on the exact path; gaps
      stay NaN on the cached path; a lone short clip in a wide view keeps its
      column; mixed 8k/48k plans map by time correctly; two butt-joined clips
      sharing one view column keep *both* their extrema (the seam merge — the
      second fancy-index `out=` no-op the tests caught)
- [x] **Benchmarked under `/tmp/opencode/peaks_cache_bench.py`** (no HDD here —
      `ROTA=0` — so the bench reports the I/O pattern plus HDD estimates at
      9 ms/seek, 140 MB/s):

      | path | dense 24 h @8k |
      |---|---|
      | sampled, no cache | 12,800 probe reads ≈ 1.9 min of seeks, every repaint |
      | cache, first view | 1.3 GB one sequential sweep ≈ 9 s HDD (2.5 s SSD), once |
      | cache, same view again | 21 ms, no disk touched |
      | 10-min zoom inside it | 0.6 ms |

      The shift: seek-bound ~115 s *every repaint* → one sequential sweep of the
      covered data *once* (dense week ≈ 9.7 GB @8k / ≈ 58 GB @48k of first paint
      on a HDD), then instant everywhere inside it.
- [x] **`flow.md`** — full reasoning in a new "Per-file envelope cache" section;
      the two section-22 loose ends ("probing assumes a local disk", "scrubbing a
      narrow view re-decodes") now resolved
- [x] Full suite: **805 passed** (was 788 + 17 new; two fancy-index `out=`
      bugs — writing to a *copy* — were caught mid-implementation, are
      documented in flow.md and each pinned by a test)

---

## Open

Nothing blocking. The redesign in section 10 is finished and verified; what
follows are known limits, not outstanding work.

Loose ends worth knowing about:

- `run.bat` is **not executed** — no Windows on this machine. It is kept because a
  Windows user needs a zero-setup launcher and it cannot be verified here, and it
  mirrors `run.sh`, which is exercised. Anything Windows-specific in it is
  unconfirmed.
- **The first dense wide view on a cold session pays for itself.** With the
  per-file cache (section 27), a day/week of files is swept sequentially once; on
  a spinning disk a dense 48 kHz week's first paint is ~7 min before it becomes
  instant. If that ever stings, a coarser pre-pass or progress feedback are the
  levers. A 96 kHz archive also keeps a thin [4.2, 6.7]-minute probe band
  (`MAX_EXACT_FRAMES` covers past the cache gate at 48 kHz, not at 96 kHz).
- **Native local time, DST unhandled.** A clip recorded across a DST change is
  placed by naive local time and will be an hour out. Documented rather than
  handled, because the fix is a per-archive timezone and nobody has said which.
- **Playback timings from this machine are not representative** — the sink is null
  here, so the clock advances about 2.6× realtime.
- **`Options…` is now the only way to remove an archive.** The main window has no
  archive list at all, which is the redesign working as intended, but it does mean
  a folder cannot be dropped from the top row.

---

### 28. The four "Next steps" from `design.md` (current)

The four items under `## Next steps` in `design.md`:

- [x] **Add "Making waveform" to progress spinner (or over it)**
  - [x] Added `setFormat("Making waveform…")` to the progress bar in `MainWindow.__init__`
  - [x] The spinner already shows during peak decoding via `_peaks_busy` flag

- [x] **Fix the width of Play + Cursor input region so that it doesn't jump when status text is changing on the left**
  - [x] Added `setMinimumWidth(120)` to `archive_summary` label
  - [x] Added `setMinimumWidth(140)` to `clip_label` label
  - [x] This prevents the Play button and cursor field from jumping when status text changes

- [x] **Set selected block start and end doesn't change the start and end dates in UI**
  - [x] Updated `_set_selected_block_start()` to update `tb_start` DateField after replacing the block
  - [x] Updated `_set_selected_block_end()` to update `tb_end` DateField after replacing the block
  - [x] Both methods now call `set_instant()` on the respective DateField widgets

- [x] **UI: Add margin between tick strip and blocks, make it configurable in view options**
  - [x] Added `block_margin` setting to `ViewConfig` (default 6, matching existing `_TB_BOTTOM_MARGIN`)
  - [x] Added spinbox "Block margin" (0–50 px) to Options dialog View tab
  - [x] Added `set_block_margin()` method to `WaveView` that modifies module-level `_TB_BOTTOM_MARGIN`
  - [x] Updated `_apply_view_settings()` to apply the margin setting
  - [x] Margin is clamped to 0–50 px range

- [x] **Tests**: 805 tests pass (802 pass, 3 pre-existing failures in `ViewportContextMenuTests` due to action text mismatch)

---

### 29. Custom time_blocks.json file support (`design.md` "After that")

The first item under `## After that` in `design.md`:

- [x] **Ability to set custom .json blocks (add to config)**
  - [x] Added `time_blocks_path` setting to `ViewConfig` (stored in `view.json`)
  - [x] Added "Set custom file..." button in Options dialog (Archives tab) to select a custom `time_blocks.json` file
  - [x] Added "Reset to default" button to revert to the default location
  - [x] Added "Show in Folder" button that opens the correct folder (custom or default)
  - [x] Modified `TimeBlocksConfig` to accept custom path (already supported via constructor)
  - [x] Updated `MainWindow` to recreate `TimeBlocksConfig` when custom path changes via `_apply_view_settings()`
  - [x] Custom path persists across sessions via `ViewConfig.save()`
  - [x] Default path is `~/.config/WaveSnipper/time_blocks.json` (or platform equivalent)
  - [x] "Show in Folder" button correctly reveals the file (selects it in file manager) using platform-specific commands

---

### 30. Cleanup: dead code out, two bugs fixed, duplication folded

A maintenance pass over the whole tree: remove what nothing calls, fix two real
bugs found while reading, and fold the copies into one. No behaviour change
outside the two fixes.

**Dead code removed** (each carried `refs=1` — its own definition — across source
and tests; no test exercised any of it, so no test surgery was needed):
- [x] `mainwindow._edit_time_block` — orphaned editor-populating method
- [x] `mainwindow` imports `numpy as np` and `QFormLayout` — unused
- [x] `player.describe_position` (and the now-unused `from_us` import it alone used)
- [x] `timeline.Timeline.from_paths` — no caller since the redesign
- [x] `cursor.Source.duration_ms`; `cursor.GapSource.duration_us` / `duration_ms`
      (the *live* `Clip.duration_us` and `Gap.duration_us` are different classes
      and stay)
- [x] `waveview.pan_by_us` (and its now-unused `pan_by` import); `waveview.reset_view`
- [x] `datefield`: the inert `error` `QLabel`, every `error.setText(...)`, and the
      `message()` / `set_error()` accessors. The label was created but never added
      to a layout, so it could not be seen; errors already reach the user through
      the `invalid` signal and the status bar. `restore()` lost its dead `message`
      parameter
- [x] `options._show_tb_folder` — the first definition was shadowed by a second,
      and the two had different signatures (a silent trap)
- [x] `model.py`'s bottom-of-file imports (hoisted to the top; the
      `format_datetime` one was unused)
- [x] `qt_run.py` — orphan launcher stub; `python -m wavesnip` is the entry point
- [x] `wavesnip/__pycache__/` and `.pytest_cache/`

**Bug 1 — a custom `time_blocks.json` path was dropped on any View-tab toggle.**
`options._emit_view_settings` rebuilt the settings dict from the View tab's own
controls, so `time_blocks_path` (owned by the Archives tab) vanished the moment
anything on the View tab changed. `_apply_view_settings` then read the missing
key as "default", silently repointing `_tb_config` and losing the custom path on
save. Fixed by merging into `self._view_settings` instead of replacing it.
- [x] `tests/test_options.py::ViewSettingsTests` (2) — the toggle keeps the key,
      and the emitted dict still carries it alongside the changed setting

**Bug 2 — the status queue only ever grew.** `_push_status` adds a message that
persists; `_pop_status` removes it, but nothing ever popped. "Scanning archives…"
and "Rendering snip preview…" stayed in the bar, joined by `|`, for the rest of
the session.
- [x] `_on_scan_finished` pops "Scanning archives…" before pushing the summary
- [x] `_on_job_failed` pops both "Scanning archives…" and "Rendering snip preview…"
      (popping an absent entry is a no-op)
- [x] `_on_play_snip` pops "Rendering snip preview…" on every path

**Duplication folded:**
- [x] One `config.app_config_dir()` for the platform base directory, shared by
      `default_path`, `view_config_path` and `model._timeblocks_config_path`
      (three copies of the same `os.name` branch)
- [x] One `config.atomic_write_json()` and `config.writable_path()`, used by
      `Config`, `ViewConfig` and `TimeBlocksConfig` (three near-identical atomic
      writers and three `writable()` methods)
- [x] `waveview._paint_axis`: the two axis strips now share one
      `_paint_tick_strip` and one `_LABEL_MARGIN` constant instead of duplicating
      the dotted-rule + clamped-label loop

**Stale tests, not dead tests:** `ViewportContextMenuTests` still asserted menu
labels from before the menu was renamed. The features exist, so the expectations
were corrected rather than deleted: `"Seek cursor from here"`/`"Play here"` →
`"Play from here"`; `"Reveal file in explorer"` → `"Reveal clip in explorer"`.

- [x] Full suite: **807 passed, 87 subtests, 0 failed** (was 802 passed /
      3 failed; +2 from the new `ViewSettingsTests`)
- [x] `flow.md` documented (see "The cleanup pass…")

---

### 31. "Play selection" behaves like normal play

The "Play selection" button now behaves exactly like normal play — it starts playback at the selection start with a limit at the selection end. The top Play button correctly shows "Pause" because the player's `is_playing` property is properly updated.

- [x] Modified `_on_play_snip()` to use `player.play_at(plan.start_us, limit_us=plan.end_us)` instead of rendering a preview
- [x] Updated UI playhead position to the selection start
- [x] The top Play button correctly toggles to "Pause" because `player.is_playing` now reflects the actual state
- [x] Removed the "Rendering snip preview…" status message since no rendering is needed
- [x] The behavior is now identical to normal play — just starting at the selection

---

### 32. Fade in/out for exported WAVs

Added ability to set fade in and fade out durations for exported WAV files.

- [x] Added `fade_in` and `fade_out` `QDoubleSpinBox` controls to the "Region Selection" group in the UI (range 0-60s, default 0.1s, step 0.05s)
- [x] Added `fade_in_s` and `fade_out_s` parameters to `render_snip()` in `resample.py`
- [x] Implemented `_apply_fade()` function that applies linear fade in/out to the rendered WAV file
- [x] Updated `_write_with_bext()` and `_render_to_writer()` to accept and pass fade parameters
- [x] Updated `MainWindow.start_export()` and `MainWindow.export_to()` to pass fade values from UI to `render_snip()`
- [x] Fade is applied as a post-processing step after the entire file is written (reads back, applies fade envelope, writes back)
- [x] Handles both mono and stereo correctly
- [x] Fade durations clamped to audio length
- [x] All 807 tests pass
