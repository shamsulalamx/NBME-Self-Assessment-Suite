# BATCH 5 — Organic generator quality overhaul (v5)

Branch: `phase12-vertex-migration`
Tag: `v5.9-batch5-pending-validation` (latest). v5.6.1 is the most
recent `-stable`; v5.7 + v5.8 + v5.9 await `-stable` promotion.
v5.9 WAS user-verified on a rebuilt `.app` — both the image-in-stem
fix and the task-variety fix confirmed on a live OME run — so its
promotion to `-stable` only awaits the user's explicit go-ahead.

## Scope (very important): organic-generation only

**v5 changes ONLY the lecture-slide "organic" Gemini-authorship path.
The legacy verbatim-extraction pipelines for NBME PDFs and AMBOSS are
untouched.**

| Path | Code | v5 touches it? |
|---|---|---|
| NBME PDF extraction (verbatim Q + A + explanation from the source PDF) | `tools/nbme-pdf-json-generator/nbme_dual_pdf_runner.py` + `verbatim_patcher.py` | **No.** Verbatim guarantees from Batch 4 (v4.85) remain. |
| AMBOSS extraction (`--amboss-profile`) | branches earlier inside `generate_lecture_slide_questions.py` | **No.** v5 dispatch is gated on `--v5`, which does not engage in amboss mode. |
| uWorld notes (text-only) | `tools/uworld-notes-question-generator/` | **No.** |
| Lecture-slide ORGANIC generation (Gemini authors the stem + distractors from grounded slide concepts) | `tools/lecture-slide-question-generator/` | **Yes.** This is the only path that activates v5 when `--v5` is set. |

How to be sure for any given run:
- Running NBME extraction (e.g. `nbme_dual_pdf_runner.py`): the `--v5`
  flag does not even exist in that script's argparse. Always verbatim.
- Running AMBOSS extraction (`--amboss-profile`): the amboss code path
  branches before the v5 dispatch point, so v5 never activates in that
  mode. Verbatim AMBOSS guarantees preserved.
- Running lecture / Fast Facts / "organic" Gemini generation: v5
  activates only when `--v5` is set; without that flag the legacy
  single-call generator runs.

The verbatim extraction guarantees you have for NBME (every stem,
choice, correctAnswer, and multi-paragraph explanation extracted
directly from the source PDF) and AMBOSS remain in force exactly as
shipped in `v4.85-batch4-stable`.

## Version history (this batch)

| Tag | What |
|---|---|
| `v5.0-batch5-stable` | Initial v5 multi-stage organic generator |
| `v5.0.1-batch5-stable` | Doc clarification: legacy NBME/AMBOSS verbatim paths untouched |
| `v5.2-batch5-stable` | Five distractor-quality upgrades (kernel-first, trap categories, adversarial verify, per-distractor scoring, length parity) |
| `v5.2.1-batch5-stable` | Docs-only handoff ship (BATCH5_OME_PORT_HANDOFF.md + v5.3 plan) |
| `v5.3-batch5-stable` | OME organic generator port to v5.2. Adds `--v5` to both OME runners; legacy single-call generator stays as fallback. |
| `v5.3.1-batch5-stable` | Advanced Mode UI toggle in BIC modal — surfaces v5.3's `--v5` flag to the user-facing import form. |
| `v5.4-batch5-stable` | v5.4 cost/latency optimization: 3 stacked levers (Lever 3 rolled back). Parallelism + thinking-budget caps + prompt-prefix caching. Smoke: 3 Qs in 117 s (was 376 s = 3.2× faster), all v5.2 quality gates intact. |
| `v5.5-batch5-stable` | v5.5 explanation depth: longer + mechanism-grounded. Kernel rationale 1–2 → 3–5 sentences. Distractor losingReason 1 → 2–3 sentences. ~10–15% cost increase per Q. |
| `v5.6-batch5-stable` | v5.6 cost-control + chunk knobs. Removes the fixed 15-Q-per-PDF cap. UI exposes chunk size + Q-per-chunk + live cost preview. Stem thinking 4096 → 1024, Critic 4096 → 2048, Distractors back on Flash with thinking 2048 (Lever 3 retry). Per-Q cost ~$0.21 → ~$0.14 (-33%). |
| `v5.6.1-batch5-stable` | **v5.6.1 bugfix: retrievalTag / reviewPearl / educationalObjective were all the same string.** Pre-v5.6.1 the OME adapter routed all three through `kernel.correctAnswerConcept` because the kernel didn't emit distinct fields. v5.6.1 adds them to the kernel JSON spec and reads them directly. Bug present since v5.3; user-caught during v5.6 inspection. **User verified the full v5.3 → v5.6.1 stack on the rebuilt .app and promoted all six to `-stable`.** |
| `v5.7-batch5-pending-validation` | Advanced Mode for Fast Facts PPTX + Emma Holliday PDF. Registry config for Fast Facts; registry + `--v5` flag forwarding in `emma_profile_runner.py`. UI gates chunk-size + Q-per-chunk knobs on new `supportsChunkControls` registry flag (OME-only today). Group B deferred. Code shipped; `.app` rebuild + smoke + `-stable` deferred until user's running queue completes. |
| `v5.8-batch5-pending-validation` | **Advanced Mode for all of Group B (UWorld notes + Mehlman PDF + Anki notes + Divine podcasts).** OME's v5.3 adapter promoted to shared `tools/shared-ingestion/v5_uworld_family_adapter.py` with a `process_file_v5_uworld_family()` helper. Each Group B generator + profile runner now accepts `--v5` flags. Registry: supportsAdvancedMode + advancedArgs for all 4 sources; supportsChunkControls for UWorld/Anki/Divine (Mehlman hides chunk knobs because its legacy CLI already owns `--questions-per-chunk`). Code shipped; same deferral as v5.7. |
| `v5.9-batch5-pending-validation` | **Two user-caught bugs on the organic Advanced Mode pipeline, both fixed in CODE (not prompt nudges).** (1) **Image-in-stem:** borrowed external images appeared ONLY in answer explanations, never in stems — even when the stem's discriminating clue literally cited the image ("an abdominal radiograph shows…"), producing questions referencing an invisible film. Root cause: Stage 8b set `placement` from the model's choice, which was "explanation" 100% of the time. Fix: placement now decided in code via `stem_references_image()` (regex over the stem + discriminating clue) → STEM when the vignette cites an image, EXPLANATION otherwise. (2) **Task variety:** every question was "next step in management/diagnosis"; the four low-weight tasks (mechanism, causative_agent, expected_finding, complication) never appeared. Root cause: `plan_allocation_slots` ran PER CHUNK with a small n (~3), so largest-remainder rounding funded only the top-3 weighted tasks and zeroed the other four in every chunk. Fix: plan all three dimensions GLOBALLY across the whole deck, then deal slots to chunks by cursor. Partner prompt edits separate ORDER (reasoning depth) from TASK (what is asked) and thread `{{TARGET_TASK}}` into the kernel + stem prompts so the correct answer + final question match the assigned task. Also: `.q-img-credit` attribution line under borrowed images (CC BY/BY-SA), `external_image_source.py` query-ladder + retry, and `targetTask` recorded in v5 debug artifacts. Single shared `v5_pipeline.py` — all organic generators (OME, UWorld, Mehlman, Anki, Divine, lecture-slide) inherit both fixes; NBME/AMBOSS verbatim untouched. **User verified both fixes on a rebuilt `.app` (live OME run): "everything looks good."** |

## v5.3 — OME organic generator port

Ports the v5.2 multi-stage pipeline (kernel-first design + 4 trap
categories + adversarial verification + per-distractor scoring + length
parity) to the OME-PDF organic generator. Both the direct CLI
(`tools/ome-pdf-question-generator/generate_ome_questions.py`) and the
BIC profile runner (`tools/shared-ingestion/ome_profile_runner.py`)
gain a `--v5` flag and the related mix/seed flags
(`--v5-order-mix`, `--v5-difficulty-mix`, `--v5-seed`).

Scope explicitly excluded from v5.3 (unchanged from v5.2.1 plan):
- NBME PDF verbatim extraction (unchanged since v4.85)
- AMBOSS verbatim extraction (the lecture-slide `--amboss-profile` path)
- uWorld notes / Mehlman / Anki / Divine audio / Images-tables (legacy
  single-call generators stay until a later ship)
- Multi-PDF combine-into-one-test (single PDF in → single test out)

### What v5.3 added

```
tools/ome-pdf-question-generator/
├── ome_v5_adapter.py                  # NEW — chunks→v5 allocations,
│                                       #       term/title/fact extraction,
│                                       #       v5→OME field decoration
└── generate_ome_questions.py           # MODIFIED — --v5 flag + dispatch +
                                        #            _process_file_v5() helper
tools/shared-ingestion/
└── ome_profile_runner.py               # MODIFIED — --v5 flag, forwards to
                                        #            downstream generator
```

The adapter does the OME-specific work the lecture-slide
`_enrich_allocations_for_v5()` does for slides: turns the raw chunk text
into the v5 allocation shape (`allowedMedicalTerms`,
`allowedDistractorPool`, `slideContext.{slideTitle, clinicalFacts,
primaryConcepts, secondaryConcepts, highYield, fullText}`,
`slideImages`). Chunks shorter than 200 chars (page headers, stray
fragments) are filtered out before question budget is distributed, so
all generated questions come from substantive content.

### Dispatch & fallback

When `--v5` is set on `generate_ome_questions.py`, the per-PDF loop
calls `_process_file_v5(filepath, questions_per_file, v5_cfg,
report_data)` instead of `_uw.process_file()`. The v5 helper:

1. Extracts raw text via the OME-patched extractor (pdfplumber).
2. Chunks via `_uw.split_into_chunks()` (same as legacy).
3. Builds v5 allocations via `build_v5_allocations()`.
4. Calls `v5_pipeline.generate_v5(...)` with the configured mixes/seed.
5. Decorates v5 questions with legacy OME fields (`id`,
   `sourceQuestionNumber`, `retrievalTag`, `reviewPearl`) so the
   resulting app-ready JSON matches the OME schema downstream
   consumers expect.
6. Wraps via the existing OME-patched `_uw.build_app_ready_json()`
   (which still sets `sourceFormat: "ome-pdf"`).
7. Writes the same `<stem>_app_ready.json` artifact at the canonical
   path. Adds `pipeline: "v5.2-organic"` so downstream consumers can
   distinguish v5 output from legacy.

If the v5 pipeline raises (no eligible chunks, Vertex transient
failure, kernel rejection cascade), the wrapper logs a warning and
falls through to the legacy `_uw.process_file()`. Default behavior
(no `--v5`) is unchanged from v5.2.1 — the legacy single-call path runs
exactly as it did before this ship.

The BIC profile runner appends `--v5 --v5-order-mix … --v5-difficulty-mix … --v5-seed …`
to the downstream `generate_ome_questions.py` subprocess command when
`--v5` is set on its own CLI. In `--mode dry-run` the v5 flag is a no-op
(no Gemini calls happen at all on the dry-run path).

### How to generate an OME test with v5

Direct CLI:
```
cd tools/ome-pdf-question-generator
GEMINI_BACKEND=vertex python3 generate_ome_questions.py \
  --input-file input_pdfs/Lesson.pdf \
  --generate --v5 \
  --questions-per-file 15 \
  --v5-order-mix 0.25,0.45,0.30 \
  --v5-difficulty-mix 0.30,0.45,0.25 \
  --v5-seed 0
```

BIC path (matches what the UI invokes):
```
GEMINI_BACKEND=vertex python3 tools/shared-ingestion/ome_profile_runner.py \
  --input-file tools/ome-pdf-question-generator/input_pdfs/Lesson.pdf \
  --mode generate --v5 --limit 0
```

Audit the result with the v5.2 gate (no OME-specific changes needed —
the audit reads `_v5_2` metadata directly from every question):
```
python3 tools/lecture-slide-question-generator/v5_audit.py \
  <output_root>/app_ready/Lesson_app_ready.json
```

### Cost & latency

Same as v5.2 lecture-slide: ~$0.18–0.22 per question, ~4–5 min
sequential. A 15-question OME test ≈ $2.70–3.30 and ~60–75 min wall
time. The legacy single-call OME path is ~10–20× cheaper but produces
NBME-quality output only intermittently — for any test the user plans
to keep, the v5 cost premium is justified.

### Smoke test (v5.3 ship)

Input: `tools/ome-pdf-question-generator/input_pdfs/test_ome_mood_disorders.pdf`
(1565 chars extracted, 1 chunk after the 200-char filter).
Command (direct CLI):
```
GEMINI_BACKEND=vertex python3 tools/ome-pdf-question-generator/generate_ome_questions.py \
  --input-file tools/ome-pdf-question-generator/input_pdfs/test_ome_mood_disorders.pdf \
  --generate --v5 --v5-seed 7 --questions-per-file 3 \
  --output-dir /tmp/ome_v5_smoke
```

Smoke results (3 questions produced in 376s — ~125 s/Q, well inside the
v5.2 envelope):

| Q | Answer | Target | Achieved | Stem | Verdict | All 4 traps | Distractor scores | Parity |
|---|---|---|---|---|---|---|---|---|
| 1 | B | third_order/difficult | third_order/difficult | 1248 chars | accept | yes | 3/3/3/3 WEAK_DEFENSE | 1.09 (ok) |
| 2 | E | first_order/easy | first_order/easy | 694 chars | accept | yes | 3/3/3/3 WEAK_DEFENSE | 1.32 (fails 1.30 cap by 0.02) |
| 3 | C | second_order/medium | second_order/medium | 876 chars | accept | yes | 3/3/3/3 WEAK_DEFENSE | 1.02 (ok) |

Answer positions B/E/C: no A-bias on this smoke. Order/difficulty
coverage: 1 question per bucket across all 3 order tiers and all 3
difficulty tiers — the v5 PLAN stage's largest-remainder allocation
worked exactly as designed.

`v5_audit.py` run on the smoke output:

- ✓ stem length: avg 939, median 876, all 3 questions ≥600 chars
- ✓ exactly 5 choices in all 3 questions
- ✓ critic verdict: 3/3 accept
- ✓ trap-category coverage: 4 categories per question (12 totals,
  0 incomplete)
- ✓ adversarial outcomes: 100% WEAK_DEFENSE (0 STRONG_DEFENSE,
  0 NO_DEFENSE — the target band)
- "FAIL" — order distribution (first_order 33% vs 25% target,
  second_order 33% vs 45% target): statistical artifact of n=3, not
  a pipeline bug. The PLAN stage produced 1 question per bucket and
  the orderAchieved field matched targetOrder for all 3 questions.
- "FAIL" — difficulty distribution (medium 33% vs 45% target): same
  n=3 artifact.
- "FAIL" — length parity 1/3 (Q2's correct-answer choice at length
  ratio 1.32 vs 1.30 cap, missed by 0.02). v5_pipeline's
  `length_parity_balance()` only trims parentheticals / comma-clauses;
  Q2's "Initiate a selective serotonin reuptake inhibitor (SSRI)"
  had no safe-trim site, and the pipeline left the text intact per
  its design ("quality > parity-by-fabrication" — see v5_pipeline.py).

Pre-existing audit bug found and fixed in v5_audit.py: the order and
difficulty gates (`[2]` and `[3]`) read `_v5` only, while the v5.2
pipeline writes the metadata under `_v5_2`. Without the fix, every
v5.2+ question silently buckets into "unknown" and the gates fail
trivially. The fix mirrors the same `_v5_2`-then-`_v5` fallback the
later gates (critic verdict, trap categories, adversarial outcomes)
already had. The fix would also apply retroactively to any audits
the prior session ran on v5.2-batch5-stable output.

BIC profile runner verification: structural only (arg-forwarding
chain confirmed by simulating argparse without invoking Gemini). A
live BIC smoke run was deferred to stay inside the v5.3 API budget —
the BIC path subprocesses into `generate_ome_questions.py` with the
same CLI flow that the direct smoke validated end-to-end.

### User verification before promoting to `-stable`

Per CLAUDE.md hard constraint #2 (no commit/tag/push without user
verification on the .app), the v5.3 tag remains
`v5.3-batch5-pending-validation` until the user runs an end-to-end OME
generation through the BIC UI and confirms the resulting questions
look correct in the renderer. The test plan:

1. Quit and relaunch the rebuilt .app.
2. From BIC, import an OME PDF (e.g. the same
   `test_ome_mood_disorders.pdf`) with `--v5` enabled.
3. Verify the generated questions render with 5 choices each, stems
   read like NBME vignettes (5–10+ sentences, NBME demographics
   opener), and the test pages through correctly.
4. Confirm a non-OME source type (any other generator) still works
   identically to v5.2.1 — `--v5` should only affect OME.
5. On ✅: tag `v5.3-batch5-stable` and push. On ❌: capture console
   output, diagnose, and reship.

## v5.2 — what changed for distractor quality

Real failure mode the user observed in v5.0: distractors were too weak
and the correct answer was identifiable without engaging the reasoning
task. Three root causes:

1. **Distractor pool was too loose.** `ALLOWED_DISTRACTOR_POOL` was just
   "other terms from the slide" — no curated set of strong competitors
   for the specific correct answer.
2. **Distractors written AFTER the stem.** Real NBME committees design
   the discriminating clue and the trap structure TOGETHER, then write
   a stem that contains exactly the clues needed. We were retrofitting
   distractors against a stem that already gave the game away.
3. **No adversarial pass.** Nothing ever tried to argue "this distractor
   could ALSO be correct, here's why." The critic scored distractor
   *quality* generically (0-3) but didn't stress-test each one.

v5.2 introduces five interlocking fixes:

### A. KERNEL-FIRST DESIGN (architectural)
New `stage_kernel()` runs BEFORE the stem. It outputs:

- `correctAnswerConcept`
- `discriminatingClueInStem` (the SPECIFIC clinical detail the stem must contain)
- 4 distractors, each with: `trapCategory`, `item`, `sharedFeatures[]`,
  `ruledOutBy`, `tempt`

The kernel is the contract for the rest of the pipeline. The stem author
is now *required* to include the discriminating clue and every
distractor's `sharedFeatures` in the prose. Designing the trap structure
upfront mirrors how NBME committees actually work — discriminator and
distractors are designed in lockstep.

### B. TRAP-CATEGORY ENFORCEMENT (4 distinct categories per question)

The kernel must produce 4 distractors from 4 DIFFERENT trap categories:

1. `COMPETING_DIAGNOSIS` — shares chief complaint and 2-3 stem features,
   ruled out by ONE specific clue.
2. `RIGHT_IDEA_WRONG_TARGET` — same management/mechanism class but wrong
   specific drug / test / structure.
3. `NEXT_STEP_WRONG_PHASE` — appropriate action at the wrong point in the
   workup (premature or jumping ahead).
4. `CONTRAINDICATED_OR_COMORBID_TRAP` — textbook first-line that's ruled
   out by an explicit contraindication / comorbidity in the stem.

Hard-rejected if two distractors share a category. This forces structural
diversity rather than 4 vaguely-plausible same-kind items.

### C. STEM-DISTRACTOR CO-DESIGN (stem must satisfy the kernel contract)

The stem prompt now requires:

- The kernel's `discriminatingClueInStem` is literally in the prose.
- Every distractor's `sharedFeatures[]` is literally in the prose
  (otherwise the distractor isn't really tempting).
- No "decorative" data — no labs, imaging, exam findings, or
  comorbidities that aren't either the discriminator, a sharedFeature,
  or standard NBME demographic scaffolding.

The stem JSON output includes `containedDiscriminatingClue` and
`containedSharedFeatures.missing[]`. Any false / non-empty-missing is a
hard pipeline failure — the slot is rejected and a new question is
attempted.

### D. ADVERSARIAL VERIFICATION (embedded in critic)

The critic now performs, for EACH distractor independently, an attempt
to argue THIS distractor is the correct answer:

- `STRONG_DEFENSE` — a clinically valid argument exists. Two correct
  answers. **Critical failure**. Question rejected.
- `WEAK_DEFENSE` — sketchable argument but a specific stem clue rules
  it out. **Target zone**.
- `NO_DEFENSE` — too obviously wrong. **Too weak distractor**.
  Targeted regen.

This catches both failure modes the user noticed: the "obviously wrong"
distractor AND the multi-correct distractor.

### E. PER-DISTRACTOR CRITIC SCORING + LITERAL-CLUE VERIFICATION

The critic now scores each of 4 distractors individually (0-3), and
separately verifies that each `losingReason`'s claimed discriminating
clue is literally present in the stem text. Verdicts now include
`revise_weakest`: regen ONLY the lowest-scoring distractor (one focused
API call) rather than the whole set.

If verdict = `revise_weakest`, `stage_regen_distractor()` produces ONE
replacement distractor in the same `trapCategory` as the rejected one,
prompted with the critic's specific issue. The patched set is then
re-critiqued ONCE. If still failing, the question is rejected.

### F. LENGTH PARITY (deterministic post-process)

A documented test-taking heuristic: the longest answer is often correct.
NBME explicitly counters this. `length_parity_balance()` checks that no
answer choice's text length exceeds 1.30× the median. If the correct
answer or a distractor is too long, it attempts a safe trim (drop a
trailing parenthetical or comma-clause) without changing clinical
meaning. If trimming can't reach the band, the text is left alone and a
warning is recorded — quality > parity-by-fabrication.

## Updated stage flow (v5.2)

```
1.  PLAN         (deterministic)        — target (order, difficulty) per slot
2.  KERNEL       (Pro + thinking)       — correctAnswer + clue + 4 trap designs  [NEW]
3.  STEM         (Pro + thinking)       — write stem; verify clue+features in prose
4.  DISTRACTORS  (Pro + thinking)       — polish kernel items into answer-choice text
5.  CRITIC       (Pro + thinking)       — per-distractor scores + adversarial argue + clue-in-stem check  [NEW]
6.  TARGETED REGEN (Pro + thinking)     — replace ONLY the weakest distractor  [NEW]
7.  LENGTH PARITY (deterministic)       — balance answer-choice lengths to <=1.30x median  [NEW]
8.  IMAGE ROUTING (Flash)               — short-circuit when imageOpportunity=='none' OR no images
9.  ASSEMBLE     (deterministic)        — canonical shape, per-Q correctAnswer shuffle
10. GLOBAL GATE  (deterministic)        — batch-level A-E distribution gate (>=20 Qs)
```

## Files added in v5.2

```
tools/lecture-slide-question-generator/prompts/
├── v5_2_kernel_prompt.txt
├── v5_2_stem_prompt.txt
├── v5_2_distractors_prompt.txt
├── v5_2_critic_prompt.txt
└── v5_2_regen_distractor_prompt.txt
```

`v5_pipeline.py` rewritten to orchestrate the 10-stage flow. `v5_audit.py`
gained three new checks: trap-category coverage, adversarial outcome
distribution (too many STRONG_DEFENSE = multi-correct; too many
NO_DEFENSE = weak distractors), and length parity violation rate.

## Cost / latency

Per question, sequential:

| Stage | Model | Time | Cost (approx) |
|---|---|---|---|
| Kernel | Pro + thinking | ~50 sec | $0.05 |
| Stem | Pro + thinking | ~70 sec | $0.06 |
| Distractors | Pro + thinking | ~50 sec | $0.04 |
| Critic | Pro + thinking | ~60 sec | $0.05 |
| Targeted regen (sometimes) | Pro + thinking | ~30 sec | $0.03 |
| Image route | Flash | ~5 sec | $0.002 |
| **Per Q** | | **~4-5 min** | **~$0.18-0.22** |

50-Q test: ≈ $10 and ≈ 3.5-4 hr sequential. Higher cost than v5.0 but
the quality bump comes from kernel + adversarial; parallelism (the v5.1
cost/speed package we discussed) is the orthogonal lever to bring wall-
clock back down without sacrificing the v5.2 quality gains.

## Backward compatibility

- Files generated by v5.0 carry `_v5` metadata. The audit handles both
  `_v5` (legacy) and `_v5_2` (current) by falling back gracefully.
- The CLI flags (`--v5`, `--v5-order-mix`, `--v5-difficulty-mix`,
  `--v5-seed`) are unchanged. Existing scripts work without
  modification; they now get the v5.2 pipeline.
- The legacy single-call generator path is still intact for cache
  repairs and as a fallback. No code path for NBME/AMBOSS verbatim
  extraction was touched.

## Why this exists

User audit of recent lecture-slide-generator outputs:

- **135 of 136 generated questions (99.3%) had answer position `A` as the correct answer.**
- Stems were 4-5 sentences (NBME stems are 6-12).
- Questions were ~all first-order (recognize→name); no second-order
  (diagnose→manage) or third-order (multi-step layered) questions.
- No difficulty stratification.
- Distractors were weak and same-position-biased.

Root cause:
1. The prompt's JSON-schema example literally hardcodes
   `"correctAnswer": "A"` — the model copies that template at temperature
   ~0.4 for ~99% of outputs.
2. Schema example shows only 4 choices (A-D), no E.
3. Explicit rule: *"Stems must be 4 to 5 sentences."*
4. No question-order targeting in the prompt at all.
5. No difficulty stratification.
6. No critique or revision pass — single Gemini call.

## v5 multi-stage architecture

A new sibling module
`tools/lecture-slide-question-generator/v5_pipeline.py` runs ONE question
through 7 stages instead of one. The legacy single-call generator is
preserved for cache repairs but no new generation should use it.

```
┌─────────────────────────────────────────────────────────────────┐
│ Stage 1: PLAN                                                   │
│   For each allocation's N questions, pick a (order, difficulty) │
│   tuple per slot so the batch matches target mixes              │
│   (largest-remainder method).                                   │
│   Target order_mix:       25% first / 45% second / 30% third    │
│   Target difficulty_mix:  30% easy  / 45% medium / 25% difficult│
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 2: STEM    (gemini-2.5-pro, thinking_budget=-1)           │
│   Input:  ALLOWED_TERMS + slide_context + target_order +        │
│           target_difficulty + rolling memory                    │
│   Output: stem text (5-12 sentences per order spec) +           │
│           correctAnswerConcept (semantic, not a letter) +       │
│           rationale + imageOpportunity + orderAchieved +        │
│           difficultyAchieved (self-classification)              │
│   Prompt: prompts/v5_stem_prompt.txt                            │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 3: DISTRACTORS  (gemini-2.5-pro, thinking_budget=-1)      │
│   Input:  stem + correctAnswerConcept + ALLOWED_TERMS +         │
│           ALLOWED_DISTRACTOR_POOL                               │
│   Output: 4 distractors, each with `losingReason` (one specific │
│           sentence tied to a discriminating clue in the stem)   │
│           and `tempt` (why a less-prepared reader would pick it)│
│   Prompt: prompts/v5_distractors_prompt.txt                     │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 4: CRITIC  (gemini-2.5-pro, thinking_budget=-1)           │
│   Input:  stem + correct + distractors + target_order +         │
│           target_difficulty + allowed terms                     │
│   Output: 5-dimension rubric score (0-3 each) +                 │
│           anti-pattern list + verdict (accept/revise/reject)    │
│   Threshold: total >= 12 AND no dimension < 2 AND no            │
│              anti-pattern present                               │
│   Revise path: ONE distractor-regen pass with critic notes      │
│   Reject path: question slot returns None (skipped)             │
│   Prompt: prompts/v5_critic_prompt.txt                          │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 5: IMAGE ROUTING  (gemini-2.5-flash, thinking_budget=0)   │
│   Input:  stem + imageOpportunity hint + AVAILABLE_IMAGES       │
│   Output: attach (bool) + imageId + placement (stem|explanation)│
│           + reason                                              │
│   Most questions are text-only by design — images only when     │
│   they MEANINGFULLY deepen the reasoning task (ECG, x-ray,      │
│   histology, etc.).                                             │
│   Prompt: prompts/v5_image_routing_prompt.txt                   │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 6: ASSEMBLE                                               │
│   Build canonical app-ready question shape with 5 choices       │
│   (A-E). Correct answer position is shuffled PER-QUESTION via   │
│   the per-Q RNG seed. Each distractor's losingReason and tempt  │
│   land in the Incorrect Answer Explanation body, labeled by     │
│   their final A-E position.                                     │
└─────────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│ Stage 7: GLOBAL DISTRIBUTION GATE                               │
│   For batches of >= 20 questions: count correctAnswer per       │
│   position. If any position is > 26% or < 14% of the batch,     │
│   pick that position's questions and reshuffle until in band.   │
│   Up to 3 reshuffle attempts; logs any residual outliers.       │
└─────────────────────────────────────────────────────────────────┘
```

## CLI integration

The legacy entry point `tools/lecture-slide-question-generator/
generate_lecture_slide_questions.py` accepts new flags:

| Flag | Meaning |
|---|---|
| `--v5` | Use v5 multi-stage pipeline (default for all new generation) |
| `--v5-order-mix 0.25,0.45,0.30` | Override target order mix |
| `--v5-difficulty-mix 0.30,0.45,0.25` | Override target difficulty mix |
| `--v5-seed 0` | Reproducible RNG seed for position shuffling |

When `--v5` is set, the legacy `generate_questions()` function checks
`_V5_CONFIG` and dispatches to `v5_pipeline.generate_v5()` instead of
the single-Gemini-call legacy code path. If the v5 pipeline raises any
exception, the legacy path is used as a fallback (with a `warn()`).

Per-question debug artifacts land under
`output_json/v5_debug/Q####.json` capturing the stem_obj,
distractors_obj, critic_obj, image_route, and final assembled question
— so any post-hoc audit can trace exactly what each stage produced.

## Audit gate

`tools/lecture-slide-question-generator/v5_audit.py` runs after a v5
generation. Reports and gates on:

- **Answer position distribution** — each of A/B/C/D/E must be between
  14% and 26% of the batch when n >= 20.
- **Question order distribution** — each bucket within ±8 percentage
  points of the target mix.
- **Difficulty distribution** — each bucket within ±10 pp of target.
- **Stem length** — batch average >= 600 chars; <10% of stems may be
  shorter than 350 chars.
- **5 choices required** — at most 5% of questions may have fewer.
- **Critic verdict** — reports the accept/revise/reject distribution.

Audit exits 0 by default (informational); pass `--strict` to exit 2
on any failure.

## Cost & latency

Each v5 question makes ~4 API calls:

- Stem      — Pro, dynamic thinking (~$0.06)
- Distractors — Pro, dynamic thinking (~$0.05)
- Critic    — Pro, dynamic thinking (~$0.04)
- Image route — Flash, no thinking (~$0.002)

≈ $0.15 per question, ~2-4 minutes per question sequential. A 50-q
test = ~$7.50 and ~2-3 hours sequential. Parallelism is possible (the
NBME extractor already does it) but not implemented here to avoid
Vertex rate-limit pressure; if needed, run multiple v5 invocations on
different source files concurrently.

## What you come back to

- Branch `phase12-vertex-migration`, HEAD pushed.
- Tag `v5.0-batch5-stable`.
- New files:
  - `tools/lecture-slide-question-generator/v5_pipeline.py`
  - `tools/lecture-slide-question-generator/v5_audit.py`
  - `tools/lecture-slide-question-generator/prompts/v5_stem_prompt.txt`
  - `tools/lecture-slide-question-generator/prompts/v5_distractors_prompt.txt`
  - `tools/lecture-slide-question-generator/prompts/v5_critic_prompt.txt`
  - `tools/lecture-slide-question-generator/prompts/v5_image_routing_prompt.txt`
- Legacy `generate_lecture_slide_questions.py` gained `--v5` and three
  related flags. Default behavior unchanged unless you pass `--v5`.

## How to generate a new test with v5

```
cd tools/lecture-slide-question-generator
GEMINI_BACKEND=vertex python3 generate_lecture_slide_questions.py \
  --generate --v5 \
  --input-file /path/to/lecture.pdf \
  --v5-order-mix 0.25,0.45,0.30 \
  --v5-difficulty-mix 0.30,0.45,0.25 \
  --v5-seed 0
```

After it finishes, audit the output:

```
python3 v5_audit.py \
  output_json/app_ready/<test_name>_app_ready.json
```

Look for zero failures and a roughly-flat answer-position histogram.

## v5.4 — cost & latency optimization (4 stacked levers)

User feedback after the v5.3 smoke: ~$0.22/Q and ~125 s/Q is too
expensive and too slow for routine use. v5.4 keeps every v5.2 quality
gate intact (4 distinct trap categories, adversarial verification,
per-distractor scoring, length parity) but cuts the cost and latency
that quality DOESN'T depend on.

### Lever 1 — Per-question parallelism

`v5_pipeline.generate_v5()` builds a flat per-slot task list across all
allocations, then dispatches into a `ThreadPoolExecutor` (default 5
workers). Each thread runs one question end-to-end through
kernel → stem → distractors → critic → regen → image route → assemble.
Cost unchanged; wall time drops by ~`min(workers, N)`× where N is the
test's question count.

Rollback: `V5_MAX_WORKERS=1` falls back to v5.2 sequential. Memory
updates are guarded by a `threading.Lock`; in parallel mode all Qs in
a batch see the same initial memory (dedup signal partially lost — an
accepted tradeoff for the speed gain).

### Lever 2 — Per-stage thinking-budget caps

v5.2 used `thinking_budget=-1` (model decides, often 10–20K thinking
tokens) on every Pro stage. v5.4 caps the stages whose reasoning load
is bounded by the kernel's design:

| Stage | v5.2 budget | v5.4 budget | Why |
|---|---|---|---|
| Kernel | -1 | -1 | Designs the trap structure; full reasoning kept |
| Stem | -1 | 4096 | Writes vignette with verbatim clue inclusion; kernel gave the spec |
| Distractors | -1 | 1024 | Polishes kernel-designed items into prose; mechanical |
| Critic | -1 | 4096 | Structured rubric + adversarial pass; bounded output |
| Regen | -1 | 1024 | Replaces ONE distractor in a known category |
| Image route | 0 | 0 | Already no-thinking (Flash) |

Each cap is overrideable via `V5_*_THINKING_BUDGET` env vars so a
specific tough allocation can request more headroom without code
changes.

### Lever 3 — Flash for Distractors + Regen

The Distractor and Regen stages execute a plan the Kernel already
designed: take 4 trap-category items + sharedFeatures + ruledOutBy
and write NBME-style answer-choice prose. Flash handles this fine,
~17× cheaper per token than Pro. Quality stages (Kernel, Stem, Critic)
stay on Pro.

Rollback: `V5_DISTRACTOR_MODEL=gemini-2.5-pro` or
`V5_REGEN_MODEL=gemini-2.5-pro` rolls either stage back individually.

### Lever 4 — Prompt-prefix caching

The kernel and stem prompt files now place per-question variables
(`TARGET_ORDER`, `TARGET_DIFFICULTY`, `MEMORY`, `KERNEL_JSON`) AFTER
the per-allocation source material (`ALLOWED_TERMS`, `SLIDE_CONTEXT`).
For 2+ questions in the same allocation, the cacheable prefix is now
identical across calls, and Vertex AI's implicit prompt caching kicks
in at the 1024-token threshold — saving ~75% on input tokens for the
prefix portion after the first cache miss.

The distractors / critic / regen prompts already had per-Q variables
at the bottom (preserved from v5.2). No change needed there.

### Lever 3 rollback note

The v5.4 v1 smoke (Flash on Distractors+Regen at `thinking_budget=1024`)
showed two regressions vs v5.3:

- Length-parity violations on **100%** of produced questions (vs 33% on
  v5.3 Pro)
- **1 of 3 questions rejected** by the critic (vs 0 of 3 on v5.3) —
  Flash produced distractors weak enough that the adversarial pass
  scored them <2 and the orchestrator refused to ship

Rolled Distractors+Regen back to Pro by default. The env vars
`V5_DISTRACTOR_MODEL=gemini-2.5-flash` and `V5_REGEN_MODEL=gemini-2.5-flash`
are still wired so anyone who wants to opt in to Flash (e.g., for an
A/B comparison or a budget-constrained batch) can do so without code
changes — they just accept the parity/rejection tradeoff.

### Smoke test results — v5.4 final (3 questions, seed 7)

Same fixture as v5.3 smoke (`test_ome_mood_disorders.pdf`) for
like-for-like comparison.

| Metric | v5.3 smoke | v5.4 smoke | Δ |
|---|---|---|---|
| Wall time | 376 s | **117 s** | **3.2× faster** |
| Per-Q wall (sequential equiv.) | 125 s | ~80 s | ~36% faster per-Q |
| Per-Q wall (5 parallel workers) | 75 s | ~25 s | ~3× faster user-perceived |
| Per-Q cost (Vertex est.) | ~$0.22 | **~$0.18** | ~18% cheaper |
| Critic verdict (3/3 accept) | ✓ | **✓** | — |
| 5 choices per question | 3/3 | **3/3** | — |
| Trap-category coverage | 4/4 per Q | **4/4 per Q** | — |
| Adversarial WEAK_DEFENSE | 100% | **100%** | — |
| Stem average length | 939 chars | **1076 chars** | +15% longer / more detail |
| Length parity (within 1.30× median) | 2/3 | 1/3 | one extra parity miss |
| Q's rejected by pipeline | 0 | **0** | — |

Audit-flagged "failures" are the same n=3 sampling artifacts as the
v5.3 smoke (order/difficulty distribution gates apply at n≥20, not
n=3) plus the one length-parity miss above. All substantive quality
gates (critic verdict, trap categories, adversarial outcomes, stem
length minimum, 5-choice requirement) pass cleanly.

### v5.4 → v5.3 comparison (projected at scale)

| Test size | v5.3 (sequential) | **v5.4 (5 workers)** | Wall-time Δ | Cost Δ |
|---|---|---|---|---|
| 3 Q | 376 s · $0.65 | **117 s · $0.54** | **3.2× faster** | -17% |
| 11 Q | ~23 min · $2.40 | **~5 min · $2.00** | **~5× faster** | -17% |
| 50 Q | ~3.5 hr · $11 | **~25 min · $9** | **~8× faster** | -18% |

### Files changed in v5.4

```
tools/lecture-slide-question-generator/
├── v5_pipeline.py                        # MODIFIED — ThreadPoolExecutor +
│                                          #            env-var models +
│                                          #            thinking-budget caps
└── prompts/
    ├── v5_2_kernel_prompt.txt             # MODIFIED — per-Q vars at end
    └── v5_2_stem_prompt.txt               # MODIFIED — per-Q vars at end
```

No changes outside the v5 pipeline. The OME wiring (v5.3), the
Advanced Mode UI toggle (v5.3.1), and the audit fix all remain
untouched. v5.4 is a drop-in upgrade to the v5.2 pipeline core.

### User verification before promoting v5.4 to `-stable`

Same protocol as v5.3 — re-run an OME PDF through BIC with Advanced
Mode checked on the rebuilt .app, confirm:

1. The wall-clock drops sharply vs. the v5.3 baseline (3 parallel
   questions in roughly the time one question took before).
2. The generated questions still render with 5 choices, accept-verdict
   critic metadata in `_v5_2`, and the same NBME-style stem quality.
3. The cost line in the BIC report shows a meaningful drop.

On ✅: promote `v5.3 / v5.3.1 / v5.4` to `-stable` and push tags.
On ❌: capture console output, isolate which lever regressed (via the
`V5_*` env vars), re-ship.

## v5.5 — longer + mechanism-grounded explanations

Pure prompt-tuning ship. Two field-spec edits — no code changes, no
new stages, no model/wiring tweaks. v5.4 explanations were
structurally excellent but intentionally short (single-sentence
"why it loses" lines). v5.5 expands them so the generated content
teaches the mechanism alongside the stem-to-answer trap pattern.

### What changed

`v5_2_kernel_prompt.txt` — rationale field spec
  Before: "1-2 sentences: why this clue resolves to this answer"
  After:  "3-5 sentences that (1) name the mechanism or
          pathophysiology behind the correct answer, (2) restate
          the specific discriminating clue from the stem, and
          (3) explain how that clue mechanistically resolves to
          this answer."

`v5_2_distractors_prompt.txt` — losingReason field spec
  Before: "one specific sentence quoting the discriminating clue
          in the stem and the mechanism"
  After:  "2-3 full sentences: name the mechanism / typical
          indication this distractor implies + cite the specific
          stem clue + close with the trap mechanism in plain
          English"

### Smoke results (test_ome_mood_disorders.pdf, seed 7, 3 Qs)

| Metric | v5.4 | v5.5 |
|---|---|---|
| Wall time | 117 s | 134 s (+15%) |
| Per-Q cost (est.) | ~$0.18 | ~$0.21 (+17%) |
| Critic verdict | 3/3 accept | **3/3 accept** |
| Trap categories per Q | 4/4 | **4/4** |
| Adversarial WEAK_DEFENSE | 100% | **100%** |
| Stem avg length | 1076 | 1024 |
| Correct-answer explanation length | ~400 chars | **703 chars** |
| Per-distractor explanation length | ~150 chars | **~456 chars** |
| App-ready JSON size (3 Qs) | 21 KB | **24 KB** |

### Sample (Q1 correct-answer explanation)

> "The patient's current presentation with low mood, anhedonia, and
> other SIGECAPS symptoms meets the criteria for a major depressive
> episode. However, the collateral history reveals a past hypomanic
> episode, characterized by elevated energy and activity for several
> days without causing marked functional impairment. This combination
> establishes a diagnosis of Bipolar II disorder, not unipolar major
> depression. Initiating antidepressant monotherapy (e.g., an SSRI)
> in bipolar disorder is contraindicated due to the risk of
> precipitating a manic episode. The appropriate management is to
> start a mood stabilizer. Lamotrigine is an effective choice for
> the treatment and maintenance of bipolar depression."

Each distractor explanation similarly names the drug's mechanism /
indication, cites the specific stem clue that rules it out, and
explains the trap pattern in plain English.

### Files changed

```
tools/lecture-slide-question-generator/prompts/
├── v5_2_kernel_prompt.txt        # MODIFIED — rationale 1-2 → 3-5 sentences
└── v5_2_distractors_prompt.txt   # MODIFIED — losingReason 1 → 2-3 sentences
```

That's it. Zero code changes. Rollback = revert this commit.

### User verification before promoting v5.5 to `-stable`

Same protocol as v5.3/v5.4 — re-run an OME generation through BIC
with Advanced Mode enabled on the rebuilt .app, confirm:

1. Explanations are visibly longer and contain mechanism context
   (compare a question explanation against what the BIC produced
   on a v5.4 run, if you have one)
2. Every v5.2 quality gate still passes (critic verdict accept,
   4 trap categories per Q, all distractors with WEAK_DEFENSE)
3. Cost line shows roughly +11–17% (the projected bump), not more

On ✅: promote `v5.3 / v5.3.1 / v5.4 / v5.5` to `-stable` and push
the tags. On ❌: capture the regression, the rollback is a one-line
revert of this commit.

## v5.6 — cost-control + chunk knobs + UI cost preview

Two parallel asks landed here:

1. **Total-cost control.** v5.3-v5.5 had a hardcoded 15-question cap
   per file (legacy OME default). For a user merging 12 PDFs into 1
   they got 15 questions for the whole 45-page bundle — way too few
   for gold-content material. v5.6 removes the cap entirely and
   replaces it with **chunk size × questions per chunk** so total
   questions scale with text length.

2. **Per-Q cost cuts.** Targeted thinking-budget trims on the stages
   whose cognitive load is bounded by the kernel's design + Lever 3
   retry on Distractors+Regen (Flash with higher thinking budget than
   the v5.4 v1 attempt that regressed).

### What v5.6 added

**New CLI flags on `generate_ome_questions.py`:**
- `--chunk-size <N>` (default 3000, pre-v5.6 behavior). UI exposes
  500 / 1000 / 1500 / 3000.
- `--questions-per-chunk <N>` (default 0 = fall back to legacy
  `--questions-per-file` distribution; > 0 = give every eligible
  chunk that many questions, total scales with chunk count).

**Forwarded through:**
- `tools/shared-ingestion/ome_profile_runner.py` (BIC dispatch)
- `tools/batch-import-center/run_pipeline_job.py` (reads
  `manifest.advancedConfig.{chunkSize, questionsPerChunk}` and
  appends to the downstream subprocess args)

**UI changes in `index.html` (inside the Advanced Mode panel):**
- "Chunk size (chars)" dropdown — 500 / 1000 / 1500 (default) / 3000
- "Questions per chunk" dropdown — 1 / 2 (default) / 3
- Live cost preview: re-renders on file selection or knob change.
  Shows estimated question count, dollar cost, and wall-clock time
  (5 parallel workers). Uses a heuristic ~50 bytes per extracted
  char for vector OME PDFs; actual generation reads real text, so
  the preview is a magnitude signal not a contract.
- IPC: new `nbme:batch-import:file-size` handler in `electron/main.js`
  (renderer sandbox has no Node fs access).

**`v5_pipeline.py` thinking-budget trims:**

| Stage | v5.5 | **v5.6** | Why |
|---|---|---|---|
| Kernel | dynamic (-1) | **dynamic (-1)** | Trap design needs unlimited room. v5.0/v5.2 quality lives here. |
| Stem | 4096 | **1024** | Writing task with a clear kernel spec; ~10% self-check fail rate acceptable now that chunk controls scale total question count. |
| Distractors | 1024 (Pro) | **2048 (Flash)** | Lever 3 retry. v5.4 v1's Flash+1024 regressed on length parity + critic rejected 1/3; v5.6's Flash+2048 gives Flash room to nail the polishing. |
| Critic | 4096 | **2048** | Preserves the adversarial argue-each on simple cases. Not 1024 — that would gut multi-correct detection. |
| Regen | 1024 (Pro) | **1024 (Flash)** | Replaces one distractor in a known category; same Flash logic as Distractors. |
| Image route | 0 (Flash) | 0 (Flash) | Unchanged. |

All overrideable via env vars (`V5_KERNEL_THINKING_BUDGET`,
`V5_STEM_THINKING_BUDGET`, …, `V5_DISTRACTOR_MODEL`,
`V5_REGEN_MODEL`) for per-test tuning without code changes.

### Smoke test results (test_ome_mood_disorders.pdf, seed 7)

Run with `--chunk-size 1000 --questions-per-chunk 2` so the small
fixture exercises the new chunk distribution path. The 1565-char
extracted text split into 2 chunks (one ~1000, one ~565), both above
the 200-char filter → 2 chunks × 2 Q/chunk = **4 questions**.

| Metric | Result |
|---|---|
| Questions produced | **4/4** (no rejections) |
| Wall time (4 workers) | **87.5 s** |
| Critic verdict | **4/4 accept** |
| 5 choices per question | **4/4** |
| Trap categories per Q | **4 distinct (16/16 perfect)** |
| Adversarial WEAK_DEFENSE | **100% (16/16)** |
| Answer-position distribution | A=25%, B=25%, C=25%, E=25% (perfect) |
| Stem length (avg / min) | 935 / 862 chars (all ≥600) |
| Length parity within band | 2/4 (intrinsic to drug-name + "Refer for CBT" mix; same as v5.4 v2 and v5.5) |

**Flash-on-Distractors retry verification:** v5.4 v1 with Flash +
1024 budget showed 100% length-parity fails AND 1/3 critic rejections
— rolled back. v5.6 with Flash + 2048 budget shows 50% length-parity
(matches Pro baseline) AND 0/4 rejections. **The higher thinking
budget gave Flash the room it needed.** Lever 3 retry confirmed.

### v5.6 → v5.5 comparison (same fixture + seed)

| | v5.5 (3 Qs) | **v5.6 (4 Qs)** | Δ |
|---|---|---|---|
| Wall time | 134 s | **87.5 s** | ~35% faster, more questions |
| Per-Q wall (parallel) | ~45 s | **~22 s** | **~2× faster per Q** |
| Per-Q cost (Vertex est.) | ~$0.21 | **~$0.13** | **-38%** |
| Critic accept | 3/3 | **4/4** | — |
| Trap categories per Q | 4/4 | **4/4** | — |
| Adversarial WEAK_DEFENSE | 100% | **100%** | — |
| Length parity within band | 1/3 | 2/4 | — |
| Pipeline rejections | 0 | **0** | — |

### v5.6 → v5.5 projected at scale

| Test size | v5.5 | **v5.6** | Wall-time Δ | Cost Δ |
|---|---|---|---|---|
| 11-Q PDF | ~6 min / $2.30 | **~3 min / $1.45** | ~2× faster | **-37%** |
| 50-Q test | ~30 min / $10.50 | **~15 min / $6.50** | ~2× faster | **-38%** |
| 200-Q test (user's gold-content scenario) | ~2 hr / $42 | **~1 hr / $26** | ~2× faster | **-38%** |

### Files changed

```
tools/lecture-slide-question-generator/
└── v5_pipeline.py                              # MODIFIED — model + budget defaults
tools/ome-pdf-question-generator/
├── generate_ome_questions.py                   # MODIFIED — --chunk-size, --questions-per-chunk
└── ome_v5_adapter.py                           # MODIFIED — questions_per_chunk param
tools/shared-ingestion/
└── ome_profile_runner.py                       # MODIFIED — forward new flags
tools/batch-import-center/
└── run_pipeline_job.py                         # MODIFIED — manifest.advancedConfig → args
electron/
├── main.js                                     # MODIFIED — advancedConfig capture +
│                                                #            file-size IPC handler
└── preload.js                                  # MODIFIED — fileSize bridge
index.html                                       # MODIFIED — Advanced Mode panel +
                                                 #            cost preview
```

### User verification before promoting v5.6 to `-stable`

Same protocol — re-run an OME generation through BIC with Advanced
Mode enabled on the rebuilt .app and confirm:

1. The Advanced Mode panel shows the two new dropdowns + cost
   preview when checked.
2. The cost preview updates when you change chunk size or questions
   per chunk, and when you select different files.
3. A test run with default settings (1500 char chunks, 2 Q per chunk)
   produces the expected question count for the PDF length (no more
   15-Q cap).
4. Every v5.2 quality gate still passes on the generated questions
   (critic accept, 4 trap categories per Q, all distractors with
   WEAK_DEFENSE).
5. The cost line in the BIC report shows ~$0.14/Q (down from ~$0.21
   on v5.5).
6. Specifically watch the **Flash distractor parity retry** — if you
   see length-parity failures on every question (same as the v5.4 v1
   regression), roll back with `V5_DISTRACTOR_MODEL=gemini-2.5-pro`
   on the env and re-test.

On ✅: promote `v5.3 / v5.3.1 / v5.4 / v5.5 / v5.6` all to `-stable`
and push the tags. On ❌: capture which lever regressed (the env
vars make this a single-toggle bisection) and re-ship.

## v5.6.1 — bugfix: 3 study-field collapse

Bug present since v5.3, surfaced by user during v5.6 inspection.

### What was wrong

`retrievalTag`, `reviewPearl`, and `educationalObjective` ended up
as the SAME string in every generated question. Three fields meant
to serve three different purposes (search tag / high-yield rule /
reasoning task) all collapsed to whatever was in
`kernel.correctAnswerConcept`.

Root cause: the v5.3 OME adapter `decorate_v5_questions_for_ome()`
did a fallback chain through `educationalObjective` →
`testedConcept` → `correctAnswerConcept` to populate the OME-only
`retrievalTag` and `reviewPearl` fields. But `v5_pipeline.assemble_question()`
itself set ALL THREE of `testedConcept`, `diagnosisOrTarget`, and
`educationalObjective` to `kernel.get("correctAnswerConcept", "")`.
So the fallback chain pulled the same string for all three.

### The fix

1. Added explicit `retrievalTag`, `reviewPearl`, and
   `educationalObjective` fields to the kernel JSON output spec in
   `prompts/v5_2_kernel_prompt.txt` with concrete examples and
   length/style requirements.
2. Updated `v5_pipeline.assemble_question()` to read each field
   directly from the kernel output (`kernel.get("retrievalTag")`,
   etc.) with a fallback to `correctAnswerConcept` only when the
   kernel field is missing (preserves compatibility with cached
   pre-v5.6.1 kernels).
3. Updated `ome_v5_adapter.decorate_v5_questions_for_ome()` to no
   longer override the kernel-provided values — the function now
   only fills in the OME-only `id` + `sourceQuestionNumber` and
   leaves the three study fields untouched.

### Smoke test (test_ome_mood_disorders.pdf, seed 7, 2 Qs)

Cost ~$0.26 (down from a normal 4-Q smoke since the small fixture
only needs 2 Qs at chunk-size=1000 + Q-per-chunk=1).

Sample Q1 output:
```
retrievalTag:         "MDD first-line treatment with bupropion for
                       fatigue and side effect concerns"
reviewPearl:          "Select bupropion for MDD in patients with
                       prominent fatigue or concerns about weight gain
                       and sexual dysfunction, but avoid it in patients
                       with seizure or eating disorders."
educationalObjective: "Select the most appropriate initial
                       pharmacotherapy for major depressive disorder
                       based on patient-specific symptoms and side
                       effect concerns."
testedConcept:        "Initiate bupropion"
```

Four distinct fields, each serving its proper purpose: a search tag
for re-finding the question, a high-yield rule for highlighting, the
reasoning task tested, and the bare correct answer.

Audit verifies the rest of the v5.2 quality gates remain intact
(2/2 critic accept, 4 trap categories per Q, 100% WEAK_DEFENSE,
length parity 0/2 fail — actually better than v5.6 on this fixture).

### Files changed

```
tools/lecture-slide-question-generator/
├── v5_pipeline.py                              # MODIFIED — assemble_question reads
│                                                #            3 fields directly from kernel
└── prompts/
    └── v5_2_kernel_prompt.txt                  # MODIFIED — added retrievalTag,
                                                 #            reviewPearl, educationalObjective
                                                 #            specs with examples
tools/ome-pdf-question-generator/
└── ome_v5_adapter.py                           # MODIFIED — decorate function stops
                                                 #            overriding kernel-provided
                                                 #            values; fallback only fires
                                                 #            for pre-v5.6.1 kernels
```

### User verification

Same protocol as v5.3 / v5.4 / v5.5 / v5.6. Re-run OME generation
through BIC with Advanced Mode on the rebuilt .app, confirm the
three fields are now distinct and each carries its proper content.

On ✅: promote all six pending-validation tags (`v5.3`, `v5.3.1`,
`v5.4`, `v5.5`, `v5.6`, `v5.6.1`) to `-stable` together and push
the tags.

## v5.7 — Advanced Mode expansion to Emma + Fast Facts (no .app rebuild during user's queue)

User asked to expose Advanced Mode for the other organic generators
they study from. v5.7 ships the "Group A" tier — sources whose
downstream already supports `--v5`, where the work is mostly
registry config + a small flag-forwarding patch.

### What v5.7 added

Sources now gated on Advanced Mode opt-in (legacy single-call still
the default when the checkbox is off):

- **Fast Facts PPTX** — registry-only change. Its registry entry
  already invokes `generate_lecture_slide_questions.py`, which has
  had `--v5` since v5.2. Added `supportsAdvancedMode: true` +
  `advancedArgs: ["--v5"]`. Zero new code.
- **Emma Holliday PDF** — registry + `emma_profile_runner.py`
  patch. The runner now accepts `--v5` / `--v5-order-mix` /
  `--v5-difficulty-mix` / `--v5-seed` and forwards them to the
  downstream `generate_lecture_slide_questions.py` subprocess.

The OME entry gained an explicit `supportsChunkControls: true`
flag (already-implicit behavior, now declared). UI conditionally
shows the chunk-size + Q-per-chunk knobs based on this flag.

### Why the chunk knobs hide for Emma + Fast Facts

The lecture-slide pipeline uses **slide-based allocation** — each
slide is one allocation. OME's chunk-size / Q-per-chunk knobs map to
character-based chunking inside `generate_ome_questions.py`. The
lecture-slide CLI doesn't accept those flags, so forwarding them
would cause an argparse error and a hard pipeline failure.

The clean solution: gate the chunk-knob row on the registry's
`supportsChunkControls` flag. Today only OME has it. Future sources
that need character-chunking can opt in by declaring it.

### Bug-fix carry-over: every Advanced Mode path gets v5.6.1

The retrievalTag / reviewPearl / educationalObjective collapse fix
from v5.6.1 lives in `v5_pipeline.assemble_question()` and the
kernel prompt. Both Emma and Fast Facts engage the same v5_pipeline
and read the same `v5_2_kernel_prompt.txt`, so their Advanced Mode
output gets the three distinct fields automatically.

### Files changed in v5.7

```
tools/batch-import-center/
└── pipeline_registry.json         # MODIFIED — supportsChunkControls on OME;
                                    #            supportsAdvancedMode + advancedArgs
                                    #            on Fast Facts + Emma
tools/shared-ingestion/
└── emma_profile_runner.py         # MODIFIED — --v5 / mix / seed flag forwarding
electron/
└── main.js                        # MODIFIED — advancedConfig only included when
                                    #            source.supportsChunkControls
index.html                          # MODIFIED — chunk-knob row gated by registry flag
```

### Group B (UWorld notes, Mehlman, Anki, Divine podcasts) — DEFERRED

Each of these needs a full v5 port (per-source `*_v5_adapter.py`,
runner `--v5` flag, downstream dispatch, smoke test). ~4–8 hours
each + ~$0.65 smoke budget per source. v5.7 explicitly does not
touch them.

### What's NOT done in v5.7 (deferred until user's running queue completes)

Per user instruction during the v5.7 ship: a queue of ~10 generation
jobs was running through the still-loaded .app. Rebuilding the .app
during that run could overwrite files mid-queue and cause subprocess
failures. Deferred:

- `.app` rebuild
- MD5 source ↔ bundled verification
- Live smoke test on Emma or Fast Facts (would also rate-limit-conflict
  with the running queue on Vertex Pro)
- `-stable` promotion

Code edits are committed and pushed to the branch so the work is
safe on remote. Once the user's queue completes, the workflow is:

1. `npm run electron:build:mac`
2. MD5 verify all 4 modified files source ↔ bundled
3. Live smoke test on a small Emma or Fast Facts file with Advanced
   Mode (~$0.50 each)
4. Verify the three study fields are distinct, the lecture-slide
   importer accepts the new top-level fields, every v5.2 quality
   gate passes
5. `-stable` promotion + tag push

## v5.8 — Advanced Mode for Group B (UWorld, Mehlman, Anki, Divine)

Same opt-in protocol as v5.7. All four Group B sources gain
Advanced Mode toggle support — when the checkbox is on, their
runners forward `--v5` to their downstream generators, which now
dispatch into `v5_pipeline.generate_v5()` via a shared adapter.
When the checkbox is off, every source's legacy single-call
behavior is byte-for-byte preserved.

### Shared adapter

The v5.3 OME-only adapter was promoted to
`tools/shared-ingestion/v5_uworld_family_adapter.py` with a new
`process_file_v5_uworld_family()` helper that any source extending
the uworld base can call. New helpers:

- `process_file_v5_uworld_family(...)` — full v5 dispatch
  (extract → chunk → allocate → kernel→stem→distractors→critic→
  regen→length-parity→assemble → decorate → write app-ready).
  Accepts `pre_extracted_text` so Divine can pass its
  already-cleaned transcript without re-reading from disk.
- `add_v5_cli_args(parser, include_chunk_args=...)` — attaches the
  six v5 flags to any argparse parser. Optional chunk-arg group
  can be skipped (Mehlman already has its own `--questions-per-chunk`).
- `resolve_v5_cfg(args)` — parses mix strings into the dict
  `v5_pipeline` expects.
- `parse_mix_arg(arg, keys, label)` — mix-string parser shared
  across every generator's v5 wiring.

`tools/ome-pdf-question-generator/ome_v5_adapter.py` is now a thin
shim that re-exports the shared API under the original OME-specific
function names (`decorate_v5_questions_for_ome`) so the v5.3 OME
port keeps working unchanged.

### What ported in v5.8

| Source | Generator changes | Profile runner changes | UI surface |
|---|---|---|---|
| **UWorld notes** | `--v5` + chunk flags + dispatch in `main()` | `--v5` flag forwarding | Advanced Mode + chunk knobs |
| **Mehlman PDF** | `--v5` + `--v5-chunk-size` (reuses existing `--questions-per-chunk`) + dispatch | `--v5` forwarding (mehlman-specific knobs) | Advanced Mode (chunk knobs hidden — Mehlman uses its own) |
| **Anki notes** | `--v5` + chunk flags + dispatch in `main()` | `--v5` flag forwarding | Advanced Mode + chunk knobs |
| **Divine podcasts** | `--v5` + chunk flags + `_dispatch_cleaned_transcript` wrapper that picks v5 vs legacy at each of 4 call sites | `--v5` flag forwarding (live mode only) | Advanced Mode + chunk knobs |

### Why Mehlman skips `supportsChunkControls`

Mehlman's legacy CLI already owns `--questions-per-chunk`
(paired with its 1.5K-char tight-focus chunking). To avoid an
argparse collision when v5 is engaged, the shared
`add_v5_cli_args(include_chunk_args=False)` path is used and a new
`--v5-chunk-size` flag is added separately. The UI chunk knobs map
to `--chunk-size` (not `--v5-chunk-size`), so the cleanest
behavior is to hide them for Mehlman — users tune Mehlman's
chunking via its existing `--questions-per-chunk` flag on the
runner instead.

### Bug-fix carry-over: every Advanced Mode path gets v5.6.1

All four Group B paths import the same `v5_pipeline.py` and read
the same `v5_2_kernel_prompt.txt` as OME / Emma / Fast Facts —
which means the v5.6.1 retrievalTag / reviewPearl /
educationalObjective distinct-fields fix applies automatically to
every source the moment its Advanced Mode is engaged.

### Files changed in v5.8

```
tools/shared-ingestion/
├── v5_uworld_family_adapter.py             # NEW — shared adapter with
│                                            #       process_file_v5_uworld_family,
│                                            #       add_v5_cli_args, resolve_v5_cfg
├── uworld_profile_runner.py                # MODIFIED — --v5 flag forwarding
├── mehlman_profile_runner.py               # MODIFIED — --v5 forwarding (v5-chunk-size)
├── anki_profile_runner.py                  # MODIFIED — --v5 flag forwarding
└── divine_transcript_profile_runner.py     # MODIFIED — --v5 flag forwarding (live mode)
tools/ome-pdf-question-generator/
└── ome_v5_adapter.py                       # MODIFIED — thin shim re-exporting shared
tools/uworld-notes-question-generator/
└── generate_uworld_questions.py            # MODIFIED — --v5 + dispatch in main()
tools/mehlman-pdf-question-generator/
└── generate_mehlman_questions.py           # MODIFIED — --v5 + dispatch (reuses
                                             #            existing --questions-per-chunk)
tools/anki-question-generator/
└── generate_anki_questions.py              # MODIFIED — --v5 + dispatch in main()
tools/divine-audio-question-generator/
└── generate_divine_questions.py            # MODIFIED — --v5 + _dispatch_cleaned_transcript
                                             #            wrapper at 4 call sites
tools/batch-import-center/
└── pipeline_registry.json                  # MODIFIED — supportsAdvancedMode +
                                             #            advancedArgs for all 4 Group B
                                             #            + supportsChunkControls on
                                             #            UWorld/Anki/Divine
```

### What's NOT done in v5.8 (same as v5.7 deferral)

Per user instruction: `.app` rebuild + smoke + `-stable`
promotion all deferred until the user's running queue completes.

Code edits are committed and pushed to the branch so the work is
safe on remote. Once the queue completes, the workflow is:

1. `npm run electron:build:mac`
2. MD5 verify all 11 modified runtime files source ↔ bundled
3. Live smoke test on a small Anki/UWorld/Mehlman/Divine file with
   Advanced Mode (~$0.50 each, ~$2 total)
4. Verify the three study fields are distinct, the app importer
   accepts each source's v5 output, every v5.2 quality gate passes
5. `-stable` promotion + tag push

### Group C — explicitly out of scope for v5.8

Images & Tables (screenshots / clinical images) — the v5.2 pipeline
assumes text grounding (`fullText`, `clinicalFacts`,
`primaryConcepts`). Image grounding needs a redesign of the kernel
+ stem prompts to accept image-derived facts as the source signal.
Closer to a v6 design than a port. Stays out until that's spec'd.

## v5.9 — image-in-stem fix + global task-variety fix

Two bugs the user hit on a live OME run, reported with justified
frustration after two prior "fixes" of mine that were prompt nudges
and could not work. Both root causes are deterministic CODE bugs, and
both are now fixed in code and proven against the user's real run data.

### Bug 1 — borrowed images only ever landed in explanations

**Symptom (user):** "Images are appearing in answer explanation. NOT A
SINGLE IMAGE ON QUESTION STEM." Borrowed external (Wikimedia) images
showed up exclusively in the answer explanation — even when the stem's
discriminating clue explicitly referenced the image (e.g. "an abdominal
radiograph shows…"), which produced a broken question citing a film the
reader could not see.

**Root cause:** Stage 8b (`external_image_source` routing) set
`external_media["placement"]` from the model's own choice
(`eq.get("placement")`), and the model picked `"explanation"` 100% of
the time regardless of whether the stem depended on the image.

**Fix (code, not prompt):** placement is now decided in code.
`v5_pipeline.py` adds `_IMAGE_REFERENCE_RE` + `stem_references_image()`
(line ~699/711), a regex over the stem **and** the discriminating clue
covering imaging modalities (radiograph/x-ray/CT/MRI/ultrasound/ECG),
tissue (smear/biopsy/histology), and visual cues (fundus/derm/gross
specimen/"shown below"). Stage 8b (line ~1169) computes:

```python
_stem_ref = stem_references_image(stem_obj["stem"],
                                  kernel.get("discriminatingClueInStem", ""))
_placement = "stem" if (_stem_ref or eq.get("placement") == "stem") else "explanation"
external_media = {**ext, "placement": _placement}
```

So a vignette that cites an image gets the image IN THE STEM; everything
else keeps the image in the explanation. Verified against the user's real
clues: 4 → STEM, 5 → EXPLANATION (images now appear in BOTH places).

**Render side (`index.html`):** borrowed images carry license metadata,
so a `.q-img-credit` line (CC BY / CC BY-SA attribution) now renders
under any image with `img.external` — gated so the user's own source
figures never get a caption. All attribution text via `textContent`
(values come from an external API), never `innerHTML`.

### Bug 2 — every question was "next step in management/diagnosis"

**Symptom (user):** questions were ONLY next-step-in-management or
next-step-in-diagnosis. The four exotic tasks — `mechanism`,
`causative_agent`, `expected_finding`, `complication` — never appeared,
across many runs.

**Root cause:** `plan_allocation_slots` was called **per chunk** with
that chunk's small `questionCount` (~3). The largest-remainder (Hare
quota) rounding inside a small n funds only the top-3 weighted tasks; the
four low-weight tasks (mechanism 0.16, causative_agent 0.10,
expected_finding 0.10, complication 0.06) round to ZERO in every chunk.
My two earlier "fixes" were kernel-prompt nudges ("DEFEAT THE DEFAULT") —
useless, because the kernel was never even *assigned* those tasks; the
planner deleted them upstream.

**Fix (code):** plan all three independent dimensions (order, difficulty,
task) **globally over the deck total**, then deal slots to chunks by a
cursor (`v5_pipeline.py` line ~1285):

```python
total_slots = sum(chunk_counts)
global_plan = plan_allocation_slots(total_slots, target_order_mix,
                                    target_difficulty_mix, target_task_mix, seed=seed)
# ...deal global_plan[cursor:cursor+count] to each chunk...
```

`plan_allocation_slots` now has exactly ONE call site. Proven offline:
7 chunks of 3 planned per-chunk → only the top-3 tasks ever; one global
n=21 → all 7 tasks present.

**Partner prompt edits** (`v5_2_kernel_prompt.txt`, `v5_2_stem_prompt.txt`):
ORDER is redefined as reasoning depth ONLY, fully independent of TASK;
all 7 tasks get explicit definitions; the kernel must shape
`correctAnswerConcept` to match `{{TARGET_TASK}}` (mechanism → a
pathophysiology/MOA, causative_agent → an organism, etc.), and the stem
must phrase its final question to match the task. These are necessary
because the planner now actually assigns the exotic tasks — the prompts
had to learn what to do with them. `targetTask` is also now recorded in
the `_v5_2` debug artifact (was showing "?").

### Scope / cross-platform

Both fixes live in the single shared
`tools/lecture-slide-question-generator/v5_pipeline.py` (the only copy in
the tree — no per-tool forks). Every organic generator imports it: OME,
lecture-slide, the shared UWorld-family adapter (UWorld / Mehlman / Anki /
Divine), the Emma / Fast-Facts ingestion runner, and the batch-import
dispatcher. No caller overrides `target_task_mix`, so all inherit the
fixed global default. NBME and AMBOSS verbatim extraction never call
`generate_v5` and are untouched.

### Verification at ship

- `python3 -m py_compile` clean on `v5_pipeline.py` + `external_image_source.py`.
- All four fix markers present in source (helper 699/711, placement
  1169/1172, global plan 1285/1297/1308, `targetTask` 882; single
  `plan_allocation_slots` call site).
- Source ↔ packaged-bundle MD5 **match** on all six runtime files
  (`v5_pipeline.py`, `external_image_source.py`, `v5_external_image_query_prompt.txt`,
  `v5_2_kernel_prompt.txt`, `v5_2_stem_prompt.txt`, `index.html`) — the
  `.app` the user validated is byte-identical to what shipped.
- **User click-through:** verified both fixes on the rebuilt `.app` via a
  live OME run — "everything looks good." `-pending-validation` suffix
  held per CLAUDE.md until the user explicitly approves `-stable`.
