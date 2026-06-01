# v4.85.2 — Quiz UI restructure + NBME lab-table renderer fix

Status: **USER-VERIFIED via click-through → promoted to `-stable` (2026-06-01).**
Shipped to branch `phase12-vertex-migration`; both
`v4.85.2-renderer-ui-labtables-pending-validation` and
`v4.85.2-renderer-ui-labtables-stable` tags point at the verified code.

Date: 2026-05-28 (shipped) · 2026-06-01 (verified + promoted)

## What this milestone contains

Two groups of changes, both renderer-side (`index.html`) plus the NBME JSON
data regeneration.

### A. Quiz UI restructure (5 user-requested changes)

All confined to `.quiz-topbar` / `.quiz-bottom-bar`, which render identically
in **normal and focus mode** (focus mode = `body.quiz-fullscreen-mode`, which
only hides `#topbar` + `#sidebar`; both quiz bars stay visible). So all 5 work
in both modes with no mode-specific duplication.

1. **Timer stops blinking on submit.** `renderQTimer()` previously kept the
   `q-timer-warning` blink class whenever `qSecs > 90`, even after submit froze
   the timer. Now gated on `_timerRunning` (qStart != null AND question not
   answered). `submitAnswer()` nulls `qStart`, so blink stops on submit.

2. **Prev/Next moved to top bar, timer between them.** `.quiz-topbar-center`
   now renders `[◀ Prev] [timer] [Next ▶] [Finish ✓]`. Buttons kept their IDs
   (`btn-prev/next/finish`) so keyboard shortcuts (ArrowLeft/Right/Enter via
   `onKeydown`) and the show/hide logic in `renderQuestion()` still work.

3. **Live score moved to bottom-right.** `#score-live` span relocated from
   top-left to the bottom bar's right cell (where Prev/Next used to be).
   `renderScoreLive()` targets it by ID — no JS change.

4. **Removed redundant bottom-left "Score 0/0".** The static `#bottom-score`
   div is gone (it had no JS updater; #3 supersedes it).

5. **SA logo / "shamsulalamx" click → dashboard.** `.topbar-brand` got
   `onclick="App.showHome()"` + pointer cursor. (Logo lives in `#topbar`, which
   is hidden in focus mode — there's nothing to click there, so behavior is
   consistent across modes.)

### B. NBME lab-table renderer crash fix (the big one)

**Root cause:** `_extractEmbeddedLabBlock` (index.html ~7330) read `m[2].trim()`
as the lab value, but `_EMBED_LAB_LINE_RE`'s value+unit group was
**non-capturing** `(?:...)`, so `m[2]` was actually the optional trailing text —
`undefined` for any lab line with no trailing. That threw
`TypeError: Cannot read properties of undefined (reading 'trim')`, which aborted
`renderQuestion()` after the item number updated but before the stem rendered —
producing the "shows previous question's content, navigator won't highlight"
symptom the user hit on NBME 8 items 5, 6, 17, 25, 26, 30, 33, 34, 37, 38, 40,
42, 45, 47.

**Fix:** made the value+unit alternation a **capturing** group (removed `?:`)
so `m[2]` = value+unit and `m[3]` = trailing; added an `m[2] ? ... : ''` guard.
Verified via Python port of the regex against every previously-crashing format
(no-trailing labs, `2+`, qualifiers, `/µm³`, comma values, multi-timepoint).

**Data regeneration:** all 6 NBME JSONs in
`/Users/shamsulalam/Desktop/v4.85-app-ready/` (NBME 3-8) were re-run through the
lab transformer (`tools/nbme-pdf-json-generator/stem_lab_transformer.py`) to
restore line-per-lab formatting (collapsed to run-on prose during the earlier
crash mitigation). Full renderer simulation over all 300 questions:
**68 lab tables render, 0 crashes, all data preserved.** NBME 8 Q6's
multi-timepoint (6-months-ago / today) values were hand-restored from the source
PDF. NBME 8 Q17 ECG image remains embedded.

> NOTE: these JSONs live OUTSIDE the git repo (they're study data, not code), so
> they are not in the commit. The user must **re-import** them to see the fix —
> the previously-imported tests carry the old broken data in IndexedDB.

### C. BIC floating status bar — partial (unverifiable tonight)

Added defensive elapsed-minutes display next to the active PDF name in
`renderJobsWidget` (`▶ Heme Onc · 12m elapsed`), computed from the job's
`startedAt` (handles both epoch-ms and ISO formats; fully guarded so it can
never throw). **Could NOT be verified** — the generation queue was idle when
this shipped. Full % / ETA needs the Python pipeline to surface chunk
done/total counts via IPC (not done; scoped for later).

## Verification checklist (DO THIS WHEN BACK)

Quit (Cmd+Q) + relaunch the .app first.

**Part A — UI (no re-import needed), test in BOTH normal and focus mode:**
- [ ] Top center shows `◀ Prev  [timer]  Next ▶`
- [ ] Bottom-right shows score `N/N (N%)`; bottom-left has no "Score 0/0"
- [ ] Let a question timer pass 1:30 (blinks) → click Submit → blink STOPS
- [ ] Click SA logo → returns to dashboard
- [ ] Arrow keys / Enter navigate Prev/Next
- [ ] Toggle Focus (⛶) → all the above still correct

**Part B — lab tables (re-import required):**
- [ ] Delete existing NBME 8 test, re-import `NBME 8_app_ready.json`
- [ ] Navigate Q5, Q6, Q17, Q33, Q34 — lab TABLES render, no freeze/wrong-content
- [ ] Q6 shows both timepoints per row
- [ ] Q17 ECG image present
- [ ] Spot-check NBME 3-7 similarly

**Part C — status bar:** only verifiable on next generation run.

## Promotion

DONE (2026-06-01): user click-through verified Parts A + B in the rebuilt .app
(quiz UI in both normal and focus mode + lab tables rendering after re-import).
`v4.85.2-renderer-ui-labtables-stable` tagged. Part C (BIC status-bar elapsed
display) remains unverified until the next live generation run, but is
crash-guarded and does not block the stable promotion.

## Known non-crashing limitations (carried, not regressions)

- Lab lines that are pure `pH 7.40` (value, no unit, not a qualifier word) do
  not match the renderer's lab-line regex, so a pH-only line won't appear as a
  table row and can break a contiguous lab streak. Pre-existing; affects a small
  number of ABG blocks. Does NOT crash.
- BIC status bar shows elapsed only, not % / ETA (needs pipeline IPC).
