#!/usr/bin/env python3
"""v5 multi-stage NBME-authentic question generation pipeline.

v5.2 (current) adds five distractor-quality improvements over v5.0:
  A. KERNEL-FIRST DESIGN — design correctAnswer + 4 trap categories +
     discriminating clue BEFORE the stem prose, NBME-committee style.
  B. ADVERSARIAL VERIFICATION — embedded in the critic; each distractor
     gets an argue-for-correct mini-pass to detect competes-too-well or
     too-easily-dismissed failures.
  C. STEM-DISTRACTOR CO-DESIGN — the stem is required to contain the
     kernel's discriminating clue AND every distractor's sharedFeatures,
     verified by self-check + critic literal-stem check.
  D. PER-DISTRACTOR CRITIC SCORING — each of 4 distractors scored
     individually; revise_weakest path targets only the lowest scorer.
  E. LENGTH PARITY — deterministic post-process balances all 5
     answer-choice text lengths to within ~30% of the median, killing
     the "longest = correct" tell.

Stage flow:

    Stage 1 - PLAN          (deterministic) target (order, difficulty)
                            per slot via largest-remainder method.

    Stage 2 - KERNEL        (NEW; Pro+thinking) design correctAnswer +
                            discriminatingClueInStem + 4 trap-category
                            distractor designs.

    Stage 3 - STEM          (Pro+thinking) writes stem from kernel;
                            self-checks discriminator + sharedFeatures
                            presence.

    Stage 4 - DISTRACTORS   (Pro+thinking) polishes kernel-designed
                            distractor items into final answer-choice
                            text, preserving trapCategory.

    Stage 5 - CRITIC        (NEW PER-DISTRACTOR + ADVERSARIAL;
                            Pro+thinking) per-distractor scoring,
                            argues each as correct, verifies clue
                            in stem literally, emits per-distractor
                            verdicts + overall verdict.

    Stage 6 - TARGETED REGEN (Pro+thinking) replaces only the weakest
                             distractor when critic verdict =
                             revise_weakest.

    Stage 7 - LENGTH PARITY (NEW; deterministic) adjusts answer-choice
                            text lengths to within ~30% of median.

    Stage 8 - IMAGE ROUTING (Flash, short-circuited when imageOpportunity
                            == 'none' OR no available images).

    Stage 9 - ASSEMBLE      build canonical app-ready shape; per-Q RNG
                            shuffles correct position.

    Stage 10 - GLOBAL GATE  verify batch correctAnswer distribution in
                            14-26% per position for n>=20; reshuffle
                            outliers.

Usage:
    from v5_pipeline import generate_v5
    questions = generate_v5(...)

Each per-Q debug trace lands under output_json/v5_debug/Q####.json.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPT_DIR = SCRIPT_DIR / "prompts"

KERNEL_PROMPT_PATH = PROMPT_DIR / "v5_2_kernel_prompt.txt"
STEM_PROMPT_PATH = PROMPT_DIR / "v5_2_stem_prompt.txt"
DISTRACTOR_PROMPT_PATH = PROMPT_DIR / "v5_2_distractors_prompt.txt"
CRITIC_PROMPT_PATH = PROMPT_DIR / "v5_2_critic_prompt.txt"
REGEN_PROMPT_PATH = PROMPT_DIR / "v5_2_regen_distractor_prompt.txt"
IMAGE_PROMPT_PATH = PROMPT_DIR / "v5_image_routing_prompt.txt"
EXTERNAL_IMAGE_PROMPT_PATH = PROMPT_DIR / "v5_external_image_query_prompt.txt"

DEBUG_DIR = SCRIPT_DIR / "output_json" / "v5_debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
EXTERNAL_IMAGE_CACHE_DIR = SCRIPT_DIR / "output_json" / "external_image_cache"

# Models — v5.6 cost optimization (Lever 3 retry).
#
# v5.4's first Lever-3 attempt put Distractors+Regen on Flash with
# thinking_budget=1024 and showed two regressions: 100% length-parity
# fails and 1/3 critic rejections. Hypothesis: the budget was too
# tight to let Flash do length-parity balancing while also writing
# the polished prose. v5.6 retries with thinking_budget=2048 (see
# DISTRACTOR_THINKING_BUDGET below) — Flash needs more reasoning
# room when it doesn't have Pro's pre-trained NBME prose patterns
# to fall back on. Keep KERNEL/STEM/CRITIC on Pro — those are the
# quality stages.
#
# All overrideable via env var so the user can roll either polishing
# stage back to Pro without a code change:
#   V5_DISTRACTOR_MODEL=gemini-2.5-pro
#   V5_REGEN_MODEL=gemini-2.5-pro
KERNEL_MODEL = os.environ.get("V5_KERNEL_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
STEM_MODEL = os.environ.get("V5_STEM_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
DISTRACTOR_MODEL = os.environ.get("V5_DISTRACTOR_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
CRITIC_MODEL = os.environ.get("V5_CRITIC_MODEL", "gemini-2.5-pro").strip() or "gemini-2.5-pro"
REGEN_MODEL = os.environ.get("V5_REGEN_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
IMAGE_MODEL = "gemini-2.5-flash"

# Thinking-budget caps per stage (v5.4 Lever 2).
#
# v5.2 used thinking_budget=-1 (dynamic) on every Pro stage. The model
# often burned 10-20K thinking tokens deciding things that didn't need
# that much reasoning — particularly for the Distractor polishing pass
# (the kernel had already done the design work) and Regen (replace one
# item in a known category). Caps below match each stage's actual cog
# load:
#   - Kernel: -1 (designs the full trap structure; can't compress this)
#   - Stem: 4096 (writes vignette with verbatim clue inclusion; clear
#     spec from the kernel, ~30% latency drop with no quality loss)
#   - Distractors: 1024 (mechanical polishing; runs on Flash now too)
#   - Critic: 4096 (per-distractor adversarial pass; structured output)
#   - Regen: 1024 (one-distractor replacement in a known category)
#   - Image route: 0 (Flash, already no-thinking)
#
# All overrideable via env var so the user can tune per-test if a
# specific allocation needs more reasoning headroom.
def _env_thinking(name: str, default: int) -> int:
    try:
        v = os.environ.get(name, "").strip()
        if not v:
            return default
        return int(v)
    except ValueError:
        return default
# v5.6 budgets — chosen so the trade-off across stages matches their
# actual cognitive load:
#   - Kernel stays dynamic (-1): designs the trap structure; needs
#     unlimited room to keep 4 categories distinct + sharedFeatures
#     specific. v5.0/v5.2's quality story lives in this stage.
#   - Stem cut to 1024 (was 4096): writing task with a clear kernel
#     spec; ~10% rejection rate acceptable since v5.6's chunk controls
#     scale total question count.
#   - Distractors bumped to 2048 (was 1024): paired with the Flash
#     move above — Flash needs more headroom to nail length parity
#     and trap-category-appropriate phrasing.
#   - Critic cut to 2048 (was 4096): preserves the adversarial pass
#     on simple cases. Not 1024 — that would gut multi-correct
#     detection (would re-introduce v5.0's failure mode).
#   - Regen stays 1024: one-distractor swap in a known category.
KERNEL_THINKING_BUDGET = _env_thinking("V5_KERNEL_THINKING_BUDGET", -1)
STEM_THINKING_BUDGET = _env_thinking("V5_STEM_THINKING_BUDGET", 1024)
DISTRACTOR_THINKING_BUDGET = _env_thinking("V5_DISTRACTOR_THINKING_BUDGET", 2048)
CRITIC_THINKING_BUDGET = _env_thinking("V5_CRITIC_THINKING_BUDGET", 2048)
REGEN_THINKING_BUDGET = _env_thinking("V5_REGEN_THINKING_BUDGET", 1024)

# Per-PDF parallelism (v5.4 Lever 1).
#
# Question slots within a single PDF run concurrently via ThreadPoolExecutor.
# Cost is unchanged (same API calls), but wall time drops by ~N× where N is
# the worker count. Vertex AI's per-project rate limit on Pro is well above
# 5 × 5 = 25 concurrent calls per PDF, so a default of 5 is conservative.
# Set V5_MAX_WORKERS=1 to fall back to v5.2 sequential behavior.
V5_MAX_WORKERS = max(1, _env_thinking("V5_MAX_WORKERS", 5))

# Distribution gate.
DISTRIBUTION_TOLERANCE_HIGH = 0.26  # max share per position when n>=20
DISTRIBUTION_TOLERANCE_LOW = 0.14   # min share per position when n>=20

# Length parity band (max length / median length).
LENGTH_PARITY_BAND = 1.30

TRAP_CATEGORIES = {
    "COMPETING_DIAGNOSIS",
    "RIGHT_IDEA_WRONG_TARGET",
    "NEXT_STEP_WRONG_PHASE",
    "CONTRAINDICATED_OR_COMORBID_TRAP",
}


# ── Gemini client (Vertex AI) ────────────────────────────────────────────────

_genai_client_cache: Any = None


def _genai_client() -> Any:
    global _genai_client_cache
    if _genai_client_cache is not None:
        return _genai_client_cache
    uw_dir = SCRIPT_DIR.parent / "uworld-notes-question-generator"
    if str(uw_dir) not in sys.path:
        sys.path.insert(0, str(uw_dir))
    import generate_uworld_questions as _uw  # type: ignore
    _genai_client_cache = _uw._gemini_client()
    return _genai_client_cache


def _genai_types() -> Any:
    from google.genai import types as t  # type: ignore
    return t


def gemini_call(
    prompt: str,
    *,
    model: str,
    max_tokens: int = 8192,
    temperature: float = 0.4,
    thinking_budget: int = -1,
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
) -> str:
    client = _genai_client()
    t = _genai_types()
    contents: list[Any] = [prompt]
    if image_bytes is not None:
        contents.append(t.Part.from_bytes(data=image_bytes, mime_type=image_mime))
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=t.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max(max_tokens * 2, 16384),
            response_mime_type="application/json",
            thinking_config=t.ThinkingConfig(thinking_budget=thinking_budget),
        ),
    )
    return response.text or ""


def parse_json_loose(raw: str) -> dict | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── Stage 1: PLAN ───────────────────────────────────────────────────────────


def plan_allocation_slots(
    allocation_question_count: int,
    target_order_mix: dict[str, float],
    target_difficulty_mix: dict[str, float],
    target_task_mix: dict[str, float],
    *,
    seed: int = 0,
) -> list[tuple[str, str, str]]:
    n = max(0, int(allocation_question_count))
    if n == 0:
        return []
    rng = random.Random(seed)
    order_keys = list(target_order_mix.keys())
    order_counts = _largest_remainder(target_order_mix, n)
    diff_keys = list(target_difficulty_mix.keys())
    diff_counts = _largest_remainder(target_difficulty_mix, n)
    task_keys = list(target_task_mix.keys())
    task_counts = _largest_remainder(target_task_mix, n)
    order_seq: list[str] = []
    for k, c in zip(order_keys, order_counts):
        order_seq.extend([k] * c)
    diff_seq: list[str] = []
    for k, c in zip(diff_keys, diff_counts):
        diff_seq.extend([k] * c)
    task_seq: list[str] = []
    for k, c in zip(task_keys, task_counts):
        task_seq.extend([k] * c)
    # The three dimensions are shuffled INDEPENDENTLY so reasoning depth
    # (order), difficulty, and question TASK are decorrelated — a "most likely
    # diagnosis" slot can land on a third-order vignette, a "mechanism" slot on
    # an easy one, etc. This is what breaks the old management monoculture:
    # task is no longer a side effect of order.
    rng.shuffle(order_seq)
    rng.shuffle(diff_seq)
    rng.shuffle(task_seq)
    return list(zip(order_seq, diff_seq, task_seq))


def _largest_remainder(mix: dict[str, float], n: int) -> list[int]:
    raw = [(k, mix[k] * n) for k in mix]
    floors = [(k, int(math.floor(v))) for k, v in raw]
    remainder = n - sum(c for _, c in floors)
    fractional = sorted(
        [(k, v - math.floor(v)) for k, v in raw],
        key=lambda x: x[1],
        reverse=True,
    )
    bumps = {k for k, _ in fractional[:remainder]}
    return [c + (1 if k in bumps else 0) for k, c in floors]


# ── Stage 2: KERNEL (NEW; trap-category design BEFORE stem) ──────────────────


def stage_kernel(
    *,
    target_order: str,
    target_difficulty: str,
    target_task: str,
    allowed_terms: list[str],
    allowed_distractor_pool: list[str],
    slide_context: dict[str, Any],
    memory: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = KERNEL_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        .replace("{{TARGET_ORDER}}", target_order)
        .replace("{{TARGET_DIFFICULTY}}", target_difficulty)
        .replace("{{TARGET_TASK}}", target_task)
        .replace("{{ALLOWED_TERMS_JSON}}", json.dumps(allowed_terms, ensure_ascii=False))
        .replace("{{ALLOWED_DISTRACTOR_POOL_JSON}}", json.dumps(allowed_distractor_pool, ensure_ascii=False))
        .replace("{{SLIDE_CONTEXT_JSON}}", json.dumps(slide_context, ensure_ascii=False))
        .replace("{{MEMORY_JSON}}", json.dumps(memory, ensure_ascii=False))
    )
    raw = gemini_call(prompt, model=KERNEL_MODEL, max_tokens=4096, thinking_budget=KERNEL_THINKING_BUDGET, temperature=0.5)
    parsed = parse_json_loose(raw)
    if not parsed:
        return None
    required = ("correctAnswerConcept", "discriminatingClueInStem", "distractors")
    if not all(parsed.get(k) for k in required):
        return None
    distractors = parsed.get("distractors") or []
    if not isinstance(distractors, list) or len(distractors) < 3:
        return None
    # Verify each trap is a known category.
    for d in distractors:
        cat = (d.get("trapCategory") or "").strip().upper()
        if cat not in TRAP_CATEGORIES:
            return None
    # Reject if more than one distractor in the same category.
    cats = [d.get("trapCategory", "").upper() for d in distractors]
    if len(set(cats)) != len(cats):
        return None
    return parsed


# ── Stage 3: STEM (consumes kernel) ──────────────────────────────────────────


def stage_stem(
    *,
    kernel: dict[str, Any],
    target_order: str,
    target_difficulty: str,
    target_task: str,
    allowed_terms: list[str],
    slide_context: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = STEM_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        .replace("{{TARGET_ORDER}}", target_order)
        .replace("{{TARGET_DIFFICULTY}}", target_difficulty)
        .replace("{{TARGET_TASK}}", target_task)
        .replace("{{KERNEL_JSON}}", json.dumps(kernel, ensure_ascii=False))
        .replace("{{ALLOWED_TERMS_JSON}}", json.dumps(allowed_terms, ensure_ascii=False))
        .replace("{{SLIDE_CONTEXT_JSON}}", json.dumps(slide_context, ensure_ascii=False))
    )
    raw = gemini_call(prompt, model=STEM_MODEL, max_tokens=4096, thinking_budget=STEM_THINKING_BUDGET, temperature=0.6)
    parsed = parse_json_loose(raw)
    if not parsed or not parsed.get("stem"):
        return None
    # The author's self-check: did they actually include the clue?
    if not parsed.get("containedDiscriminatingClue", False):
        return None
    # Verify every sharedFeature was kept.
    missing = []
    for entry in parsed.get("containedSharedFeatures", []) or []:
        if entry.get("missing"):
            missing.extend(entry.get("missing"))
    if missing:
        return None
    return parsed


# ── Stage 4: DISTRACTORS (polishes kernel into answer-choice text) ──────────


def stage_distractors(
    *,
    stem: str,
    kernel: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = DISTRACTOR_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        .replace("{{STEM}}", stem)
        .replace("{{KERNEL_JSON}}", json.dumps(kernel, ensure_ascii=False))
    )
    raw = gemini_call(prompt, model=DISTRACTOR_MODEL, max_tokens=4096, thinking_budget=DISTRACTOR_THINKING_BUDGET, temperature=0.4)
    parsed = parse_json_loose(raw)
    if not parsed or not parsed.get("distractors"):
        return None
    distractors = parsed["distractors"]
    if not isinstance(distractors, list) or len(distractors) < 3:
        return None
    return parsed


# ── Stage 5: CRITIC (per-distractor + adversarial argue-each) ───────────────


def stage_critic(
    *,
    stem: str,
    correct_answer_text: str,
    correct_answer_concept: str,
    distractors: list[dict[str, Any]],
    kernel: dict[str, Any],
    target_order: str,
    target_difficulty: str,
) -> dict[str, Any] | None:
    prompt = CRITIC_PROMPT_PATH.read_text(encoding="utf-8")
    correct_payload = {
        "text": correct_answer_text,
        "concept": correct_answer_concept,
    }
    prompt = (
        prompt
        .replace("{{STEM}}", stem)
        .replace("{{CORRECT_ANSWER_JSON}}", json.dumps(correct_payload, ensure_ascii=False))
        .replace("{{DISTRACTORS_JSON}}", json.dumps(distractors, ensure_ascii=False))
        .replace("{{KERNEL_JSON}}", json.dumps(kernel, ensure_ascii=False))
        .replace("{{TARGET_ORDER}}", target_order)
        .replace("{{TARGET_DIFFICULTY}}", target_difficulty)
    )
    raw = gemini_call(prompt, model=CRITIC_MODEL, max_tokens=4096, thinking_budget=CRITIC_THINKING_BUDGET, temperature=0.2)
    return parse_json_loose(raw)


# ── Stage 6: TARGETED REGEN ──────────────────────────────────────────────────


def stage_regen_distractor(
    *,
    stem: str,
    correct_answer_text: str,
    good_distractors: list[dict[str, Any]],
    rejected_distractor: dict[str, Any],
    critic_issue: str,
    kernel: dict[str, Any],
) -> dict[str, Any] | None:
    prompt = REGEN_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        .replace("{{STEM}}", stem)
        .replace("{{CORRECT_ANSWER}}", correct_answer_text)
        .replace("{{GOOD_DISTRACTORS_JSON}}", json.dumps(good_distractors, ensure_ascii=False))
        .replace("{{REJECTED_DISTRACTOR_JSON}}", json.dumps(rejected_distractor, ensure_ascii=False))
        .replace("{{CRITIC_ISSUE}}", critic_issue or "")
        .replace("{{KERNEL_JSON}}", json.dumps(kernel, ensure_ascii=False))
    )
    raw = gemini_call(prompt, model=REGEN_MODEL, max_tokens=2048, thinking_budget=REGEN_THINKING_BUDGET, temperature=0.5)
    parsed = parse_json_loose(raw)
    if not parsed or not parsed.get("replacement"):
        return None
    return parsed["replacement"]


# ── Stage 7: LENGTH PARITY (deterministic post-process) ─────────────────────


def length_parity_balance(
    correct_text: str, distractor_texts: list[str]
) -> tuple[str, list[str], dict[str, Any]]:
    """If the correct answer is longer than 1.3x the median, attempt
    safe trimming. If the shortest distractor is much shorter than the
    median, pad with a clinically inert qualifier. This is a
    DETERMINISTIC tuning step; if it cannot bring everything into the
    band without altering meaning, it leaves the text alone and emits
    a warning."""
    all_texts = [correct_text] + list(distractor_texts)
    lens = [len(t) for t in all_texts]
    if not lens:
        return correct_text, distractor_texts, {"applied": False, "reason": "empty"}
    median = statistics.median(lens)
    if median <= 0:
        return correct_text, distractor_texts, {"applied": False, "reason": "zero_median"}
    target_max = int(median * LENGTH_PARITY_BAND)
    info = {"applied": False, "before": lens, "median": median, "warnings": []}

    # Trim only the leading parenthetical or trailing clarifier; never
    # change clinical meaning. If trimming can't get under target_max,
    # we leave the text alone (the critic accepted it as truthful).
    def safe_trim(text: str) -> str:
        if len(text) <= target_max:
            return text
        # Drop a parenthetical: "Aspirin (325 mg orally)" -> "Aspirin"
        m = re.match(r"^(.+?)\s*\([^)]+\)\s*$", text)
        if m and len(m.group(1)) <= target_max:
            return m.group(1).strip()
        # Drop a trailing comma-clause: "X, including Y and Z" -> "X"
        m = re.match(r"^(.+?),\s+[^,]+$", text)
        if m and len(m.group(1)) <= target_max:
            return m.group(1).strip()
        return text

    correct_new = safe_trim(correct_text)
    distractors_new = [safe_trim(d) for d in distractor_texts]
    changed = (correct_new != correct_text) or (distractors_new != list(distractor_texts))
    info["applied"] = changed
    info["after"] = [len(correct_new)] + [len(d) for d in distractors_new]
    info["targetMax"] = target_max
    return correct_new, distractors_new, info


# ── Stage 8: IMAGE ROUTING (short-circuited) ────────────────────────────────


def stage_image_route(
    *,
    stem: str,
    image_opportunity: str,
    available_images: list[dict[str, Any]],
) -> dict[str, Any]:
    if not available_images:
        return {"attach": False, "imageId": "", "placement": "", "reason": "no source images available"}
    if (image_opportunity or "none").strip().lower() == "none":
        return {"attach": False, "imageId": "", "placement": "", "reason": "stem author said no image opportunity"}
    prompt = IMAGE_PROMPT_PATH.read_text(encoding="utf-8")
    minimal = [
        {
            "imageId": img.get("imageId") or img.get("id") or "",
            "kind": img.get("kind") or img.get("type") or "unknown",
            "description": (img.get("description") or img.get("caption") or "")[:200],
        }
        for img in available_images
    ]
    prompt = (
        prompt
        .replace("{{STEM}}", stem)
        .replace("{{IMAGE_OPPORTUNITY}}", image_opportunity or "none")
        .replace("{{AVAILABLE_IMAGES_JSON}}", json.dumps(minimal, ensure_ascii=False))
    )
    raw = gemini_call(prompt, model=IMAGE_MODEL, max_tokens=1024, thinking_budget=0, temperature=0.2)
    parsed = parse_json_loose(raw)
    if not parsed or "attach" not in parsed:
        return {"attach": False, "imageId": "", "placement": "", "reason": "image routing parse failed"}
    return parsed


# ── Stage 8b: source-image media builder (Phase 1) ──────────────────────────


def _v5_data_url(path: Path, mime: str | None) -> str:
    actual_mime = mime or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{actual_mime};base64,{encoded}"


def _resolve_v5_asset_path(asset_path_str: str) -> Path | None:
    raw = (asset_path_str or "").strip()
    if not raw:
        return None
    for cand in (Path(raw), SCRIPT_DIR / raw, SCRIPT_DIR.parent / raw):
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def build_v5_source_media(
    image_route: dict[str, Any] | None,
    allocation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the Stage-8 routing decision into renderable image entries drawn
    from the slide's already-extracted SOURCE figures.

    The app's Landing-JSON import (_persistLandingJsonInlineImages in
    index.html) moves each entry's base64 ``dataUrl`` into FigureStore and the
    quiz renderer shows it, so all we emit here is the data URL + placement.

    Text-only sources (uWorld, Anki, Divine, OME) carry no slideImages, so
    Stage 8 already returns attach=False for them and this returns empty —
    their source-ingestion path is never touched. A missing/unreadable asset
    degrades to "no image" rather than raising: a dropped figure is
    acceptable, crashing the run is not.
    """
    images: list[dict[str, Any]] = []
    explanation_images: list[dict[str, Any]] = []
    figure_refs: list[dict[str, Any]] = []
    if not (image_route and image_route.get("attach")):
        return images, explanation_images, figure_refs
    image_id = str(image_route.get("imageId") or "").strip()
    placement = str(image_route.get("placement") or "").strip().lower()
    if not image_id or placement not in {"stem", "explanation"}:
        return images, explanation_images, figure_refs
    slide_images = allocation.get("slideImages") or []
    img = next(
        (i for i in slide_images if str(i.get("imageId") or "") == image_id),
        None,
    )
    if not img:
        return images, explanation_images, figure_refs
    asset_path = _resolve_v5_asset_path(str(img.get("assetPath") or ""))
    if not asset_path:
        print(
            f"[v5] image route chose {image_id!r} but its asset "
            f"{img.get('assetPath')!r} was not found on disk; skipping figure.",
            file=sys.stderr,
        )
        return images, explanation_images, figure_refs
    try:
        data_url = _v5_data_url(asset_path, img.get("mimeType"))
    except OSError as exc:
        print(f"[v5] failed to read image asset {asset_path}: {exc}", file=sys.stderr)
        return images, explanation_images, figure_refs
    entry = {
        "figureKey": None,
        "dataUrl": data_url,
        "isLabTable": False,
        "kind": img.get("kind") or "figure",
        "slideImageId": image_id,
        "placement": placement,
        "source": "v5-source-image",
    }
    figure_refs.append({"id": image_id, "location": placement, "visibleText": []})
    if placement == "stem":
        images.append(entry)
    else:
        explanation_images.append(entry)
    return images, explanation_images, figure_refs


# ── Stage 8c: external image sourcing (Phase 2) ─────────────────────────────


class ExternalImageBudget:
    """Thread-safe counter for borrowed external images in one deck.

    Default is UNLIMITED (``cap=None``): whether a question gets an image is
    Gemini's per-question decision (``stage_external_image_query``), NOT a
    deck-level quota. An optional numeric cap is honored only if explicitly
    set. ``reserve()`` claims a slot before a fetch; ``refund()`` returns it
    if the fetch found nothing, so ``used`` reflects images actually
    attached, not attempts."""

    def __init__(self, cap: int | None) -> None:
        self._cap = None if cap is None else max(0, int(cap))
        self._used = 0
        self._lock = threading.Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self._cap is None or self._used < self._cap:
                self._used += 1
                return True
            return False

    def refund(self) -> None:
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def cap(self) -> int | None:
        return self._cap


# Modality / "image is referenced" detector. Used to decide stem vs explanation
# placement IN CODE — the external-query model defaulted to "explanation" for
# 100% of questions, which buried the figure even when the stem text explicitly
# refers to it ("an abdominal radiograph shows ..."). If the stem or its
# discriminating clue names an image, the figure MUST sit in the STEM (otherwise
# the reader is told to interpret a film they can't see until after answering —
# a broken question). When neither references an image, the figure is a teaching
# illustration and belongs in the EXPLANATION. This yields images in BOTH places
# without trusting the model.
_IMAGE_REFERENCE_RE = re.compile(
    r"radiograph|x-?ray|\bfilm\b|\bkub\b|computed tomograph|\bct\b|\bct scan\b|"
    r"\bmri\b|magnetic reson|ultrasound|ultrasonograph|sonogra|\bdoppler\b|"
    r"echocardiogra|\becho\b|\becg\b|\bekg\b|electrocardiogra|rhythm strip|"
    r"\bsmear\b|blood film|biopsy|histolog|microscop|\bstain\b|"
    r"\bfundus\b|fundoscop|ophthalmoscop|\bretina|dermoscop|"
    r"gross specimen|\bspecimen\b|autopsy|angiogram|angiography|"
    r"\bscan\b|\bimaging\b|\bshown below\b|\bas shown\b|\bpictured\b|\bphotograph\b",
    re.IGNORECASE,
)


def stem_references_image(stem: str, discriminating_clue: str) -> bool:
    """True if the stem or its discriminating clue names/depicts an image, in
    which case the borrowed figure must be placed in the stem (not explanation)."""
    blob = f"{discriminating_clue or ''}\n{stem or ''}"
    return bool(_IMAGE_REFERENCE_RE.search(blob))


def stage_external_image_query(
    *,
    stem: str,
    image_opportunity: str,
    correct_answer_concept: str,
    discriminating_clue: str,
) -> dict[str, Any]:
    """Ask the model whether a borrowed external image is warranted and, if so,
    for a TEXT search query (never a URL). Short-circuits with no model call
    when the kernel saw no image opportunity."""
    if (image_opportunity or "none").strip().lower() == "none":
        return {"want": False, "query": "", "modality": "none",
                "placement": "stem", "reason": "no image opportunity"}
    prompt = EXTERNAL_IMAGE_PROMPT_PATH.read_text(encoding="utf-8")
    prompt = (
        prompt
        .replace("{{STEM}}", stem or "")
        .replace("{{IMAGE_OPPORTUNITY}}", image_opportunity or "none")
        .replace("{{CORRECT_CONCEPT}}", correct_answer_concept or "")
        .replace("{{DISCRIMINATING_CLUE}}", discriminating_clue or "")
    )
    raw = gemini_call(prompt, model=IMAGE_MODEL, max_tokens=512,
                      thinking_budget=0, temperature=0.2)
    parsed = parse_json_loose(raw)
    if not parsed or not parsed.get("want"):
        return {"want": False, "query": "", "modality": "none",
                "placement": "stem", "reason": (parsed or {}).get("reason", "")}
    placement = str(parsed.get("placement") or "stem").strip().lower()
    if placement not in ("stem", "explanation"):
        placement = "stem"
    return {
        "want": True,
        "query": str(parsed.get("query") or "").strip(),
        "modality": str(parsed.get("modality") or image_opportunity or "").strip(),
        "placement": placement,
        "reason": parsed.get("reason", ""),
    }


def build_v5_external_media(
    external_media: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn a fetched external image (from external_image_source) into the same
    renderable image-entry shape Phase 1 uses, carrying license/attribution
    metadata so a later UI pass can credit the source."""
    images: list[dict[str, Any]] = []
    explanation_images: list[dict[str, Any]] = []
    figure_refs: list[dict[str, Any]] = []
    if not external_media or not external_media.get("dataUrl"):
        return images, explanation_images, figure_refs
    placement = str(external_media.get("placement") or "stem").strip().lower()
    if placement not in ("stem", "explanation"):
        placement = "stem"
    fid = "ext_" + hashlib.sha1(
        (external_media.get("sourceUrl") or external_media.get("dataUrl", "")[:64])
        .encode("utf-8")
    ).hexdigest()[:10]
    entry = {
        "figureKey": None,
        "dataUrl": external_media["dataUrl"],
        "isLabTable": False,
        "kind": external_media.get("modality") or "figure",
        "placement": placement,
        "source": "v5-external-image",
        "external": True,
        "sourceName": external_media.get("sourceName") or "",
        "sourceUrl": external_media.get("pageUrl") or external_media.get("sourceUrl") or "",
        "license": external_media.get("license") or "",
        "attribution": external_media.get("attribution") or "",
        "title": external_media.get("title") or "",
    }
    figure_refs.append({"id": fid, "location": placement, "visibleText": [], "external": True})
    if placement == "stem":
        images.append(entry)
    else:
        explanation_images.append(entry)
    return images, explanation_images, figure_refs


# ── Stage 9: ASSEMBLE ───────────────────────────────────────────────────────


def assemble_question(
    *,
    question_number: int,
    kernel: dict[str, Any],
    stem_obj: dict[str, Any],
    correct_text: str,
    distractor_texts: list[str],
    distractors_meta: list[dict[str, Any]],
    critic_obj: dict[str, Any] | None,
    image_route: dict[str, Any] | None,
    allocation: dict[str, Any],
    external_media: dict[str, Any] | None = None,
    target_order: str,
    target_difficulty: str,
    target_task: str = "",
    length_parity_info: dict[str, Any],
    rng: random.Random,
) -> dict[str, Any]:
    """Build canonical question. Correct-answer position is randomized
    per-Q via the RNG; stage 10 then verifies global distribution."""
    stem = stem_obj.get("stem", "")
    choices_text = [correct_text] + list(distractor_texts)
    rng.shuffle(choices_text)
    correct_index = choices_text.index(correct_text)
    labels = ["A", "B", "C", "D", "E"][: len(choices_text)]
    answer_choices = [
        {"label": labels[i], "text": choices_text[i]} for i in range(len(choices_text))
    ]
    correct_label = labels[correct_index]
    # Reattach each distractor's losingReason to its final label
    distractor_label_map: dict[str, dict[str, Any]] = {}
    distractor_lookup = {d.get("text", ""): d for d in distractors_meta}
    for i, text in enumerate(choices_text):
        if i == correct_index:
            continue
        if text in distractor_lookup:
            distractor_label_map[labels[i]] = distractor_lookup[text]
    # v5.6.1: read the three retrieval/study fields directly from the
    # kernel. Pre-v5.6.1 these all defaulted to correctAnswerConcept,
    # which made retrievalTag / reviewPearl / educationalObjective
    # identical in every question. The kernel prompt now spec'd them
    # as three distinct fields with explicit examples; this strip()-
    # and-fallback chain preserves backward compat with cached kernels
    # that don't have the new fields yet.
    kernel_concept = (kernel.get("correctAnswerConcept") or "").strip()
    kernel_edu = (kernel.get("educationalObjective") or "").strip()
    kernel_tag = (kernel.get("retrievalTag") or "").strip()
    kernel_pearl = (kernel.get("reviewPearl") or "").strip()
    media_images, media_explanation_images, media_figure_refs = build_v5_source_media(
        image_route, allocation
    )
    if not (media_images or media_explanation_images):
        ext_images, ext_explanation_images, ext_refs = build_v5_external_media(external_media)
        media_images += ext_images
        media_explanation_images += ext_explanation_images
        media_figure_refs += ext_refs
    return {
        "questionNumber": question_number,
        "slideId": allocation.get("slideId", ""),
        "questionKind": "clinical_vignette",
        "testedConcept": kernel_concept,
        "diagnosisOrTarget": kernel_concept,
        "stem": stem,
        "hasEmbeddedFigure": bool(media_images or media_explanation_images),
        "figureRefs": media_figure_refs,
        "images": media_images,
        "explanationImages": media_explanation_images,
        "answerChoices": answer_choices,
        "correctAnswer": correct_label,
        "educationalObjective": kernel_edu or kernel_concept,
        "retrievalTag": kernel_tag or kernel_concept,
        "reviewPearl": kernel_pearl,
        "explanationSections": _build_explanation_sections(
            kernel=kernel,
            distractor_label_map=distractor_label_map,
        ),
        "tables": [],
        "sharedGroup": None,
        "extractionWarnings": [],
        "_v5_2": {
            "targetOrder": target_order,
            "targetDifficulty": target_difficulty,
            "targetTask": target_task,
            "orderAchieved": stem_obj.get("orderAchieved", ""),
            "difficultyAchieved": stem_obj.get("difficultyAchieved", ""),
            "criticOverallTotal": (critic_obj or {}).get("overallTotal"),
            "criticVerdict": (critic_obj or {}).get("verdict"),
            "criticAntiPatterns": (critic_obj or {}).get("antiPatternsFound", []),
            "criticDistractorScores": (critic_obj or {}).get("distractorScores", []),
            "discriminatingClueInStem": (critic_obj or {}).get("discriminatingClueInStem", None),
            "trapCategoriesUsed": [d.get("trapCategory") for d in distractors_meta],
            "imageRoute": image_route or {},
            "lengthParity": length_parity_info,
            "sourceFactIds": stem_obj.get("sourceFactIds", []),
            "discriminatingClue": kernel.get("discriminatingClueInStem", ""),
        },
    }


def _build_explanation_sections(
    *, kernel: dict[str, Any], distractor_label_map: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    rationale = (kernel.get("rationale") or "").strip()
    if rationale:
        sections.append({"heading": "Correct Answer Explanation", "body": [rationale]})
    if distractor_label_map:
        lines = []
        for label in sorted(distractor_label_map.keys()):
            d = distractor_label_map[label]
            text = (d.get("text") or "").strip()
            losing = (d.get("losingReason") or "").strip()
            cat = (d.get("trapCategory") or "").strip()
            line = f"{label}. {text} — {losing}"
            if cat:
                line += f"  [trap: {cat.lower().replace('_', ' ')}]"
            lines.append(line)
        if lines:
            sections.append({"heading": "Incorrect Answer Explanation", "body": lines})
    edu = (kernel.get("correctAnswerConcept") or "").strip()
    if edu:
        sections.append({"heading": "Educational Objective", "body": [edu]})
    return sections


# ── Stage 10: GLOBAL DISTRIBUTION GATE ───────────────────────────────────────


def randomize_global_distribution(
    questions: list[dict[str, Any]], *, seed: int = 0
) -> list[dict[str, Any]]:
    if len(questions) < 20:
        return questions
    rng = random.Random(seed + 17)
    for _attempt in range(3):
        dist = Counter(q.get("correctAnswer", "") for q in questions)
        n = len(questions)
        over = [k for k, v in dist.items() if v / n > DISTRIBUTION_TOLERANCE_HIGH]
        under = [k for k in "ABCDE" if dist.get(k, 0) / n < DISTRIBUTION_TOLERANCE_LOW]
        if not over and not under:
            return questions
        candidates = [q for q in questions if q.get("correctAnswer") in over]
        rng.shuffle(candidates)
        for q in candidates[: max(1, len(candidates) // 2)]:
            choices = q.get("answerChoices", []) or []
            if not choices:
                continue
            correct_text = next(
                (c["text"] for c in choices if c["label"] == q.get("correctAnswer")),
                None,
            )
            if correct_text is None:
                continue
            rng.shuffle(choices)
            for i, c in enumerate(choices):
                c["label"] = "ABCDE"[i]
            new_correct = next(c["label"] for c in choices if c["text"] == correct_text)
            q["correctAnswer"] = new_correct
            q["answerChoices"] = choices
    return questions


# ── Orchestrator: one question end-to-end ───────────────────────────────────


def generate_one_question(
    *,
    question_number: int,
    allocation: dict[str, Any],
    target_order: str,
    target_difficulty: str,
    target_task: str,
    memory: dict[str, Any],
    available_images: list[dict[str, Any]],
    rng: random.Random,
    external_budget: "ExternalImageBudget | None" = None,
) -> dict[str, Any] | None:
    allowed_terms = allocation.get("allowedMedicalTerms") or []
    allowed_distractor_pool = allocation.get("allowedDistractorPool") or []
    slide_context = allocation.get("slideContext") or {}

    # Stage 2: KERNEL
    kernel = stage_kernel(
        target_order=target_order,
        target_difficulty=target_difficulty,
        target_task=target_task,
        allowed_terms=allowed_terms,
        allowed_distractor_pool=allowed_distractor_pool,
        slide_context=slide_context,
        memory=memory,
    )
    if not kernel:
        return None

    # Stage 3: STEM
    stem_obj = stage_stem(
        kernel=kernel,
        target_order=target_order,
        target_difficulty=target_difficulty,
        target_task=target_task,
        allowed_terms=allowed_terms,
        slide_context=slide_context,
    )
    if not stem_obj:
        return None

    # Stage 4: DISTRACTORS
    distractors_obj = stage_distractors(stem=stem_obj["stem"], kernel=kernel)
    if not distractors_obj:
        return None
    correct_text = (distractors_obj.get("correctAnswerText") or "").strip()
    distractors = distractors_obj.get("distractors") or []
    if not correct_text or not distractors:
        return None

    # Stage 5: CRITIC (per-distractor + adversarial)
    critic = stage_critic(
        stem=stem_obj["stem"],
        correct_answer_text=correct_text,
        correct_answer_concept=kernel.get("correctAnswerConcept", ""),
        distractors=distractors,
        kernel=kernel,
        target_order=target_order,
        target_difficulty=target_difficulty,
    )
    verdict = (critic or {}).get("verdict", "")

    # Stage 6: TARGETED REGEN
    # revise_weakest: regen just the lowest-scoring distractor.
    # revise_full: regen EVERY distractor scoring below 2 (often 2-3 of
    # them) — each in its kernel-defined trap category — then re-critic
    # the whole set once. This avoids letting questions through with
    # multiple NO_DEFENSE distractors (a real failure mode the smoke
    # test surfaced) while preserving the kernel structure.
    if critic and verdict in ("revise_weakest", "revise_full"):
        scores = critic.get("distractorScores") or []
        # Pick indices to regen
        if verdict == "revise_weakest":
            weakest = critic.get("weakestDistractorIndex")
            to_regen = [int(weakest)] if isinstance(weakest, int) else []
        else:  # revise_full
            to_regen = [
                int(ds.get("index"))
                for ds in scores
                if isinstance(ds.get("index"), int) and int(ds.get("score", 0)) < 2
            ]
        # Defensive: ignore out-of-range indices
        to_regen = [i for i in to_regen if 0 <= i < len(distractors)]
        issues_by_index = {int(ds.get("index")): ds.get("issue", "") for ds in scores if isinstance(ds.get("index"), int)}
        for weak_idx in to_regen:
            rejected = distractors[weak_idx]
            issue = issues_by_index.get(weak_idx, "")
            good = [d for i, d in enumerate(distractors) if i != weak_idx]
            replacement = stage_regen_distractor(
                stem=stem_obj["stem"],
                correct_answer_text=correct_text,
                good_distractors=good,
                rejected_distractor=rejected,
                critic_issue=issue,
                kernel=kernel,
            )
            if replacement:
                distractors[weak_idx] = replacement
        if to_regen:
            # Re-critic the patched set ONCE.
            critic = stage_critic(
                stem=stem_obj["stem"],
                correct_answer_text=correct_text,
                correct_answer_concept=kernel.get("correctAnswerConcept", ""),
                distractors=distractors,
                kernel=kernel,
                target_order=target_order,
                target_difficulty=target_difficulty,
            )
            verdict = (critic or {}).get("verdict", "")
    if critic and verdict == "reject":
        return None
    # After the optional regen pass, if ANY distractor still scores < 2
    # OR adversarialOutcome == NO_DEFENSE OR STRONG_DEFENSE, refuse the
    # question. We accept "accept" or "revise_weakest"/"revise_full"
    # whose re-critic raised all distractors to >= 2 (and no critical
    # outcomes).
    if critic:
        for ds in critic.get("distractorScores") or []:
            score = int(ds.get("score", 0) or 0)
            outcome = ds.get("adversarialOutcome") or ""
            if score < 2 or outcome in ("NO_DEFENSE", "STRONG_DEFENSE"):
                return None

    # Stage 7: LENGTH PARITY (deterministic)
    distractor_texts = [d.get("text", "") for d in distractors]
    correct_text, distractor_texts, parity_info = length_parity_balance(
        correct_text, distractor_texts
    )
    # Reattach the (possibly-trimmed) texts back into distractors meta
    for d, new_text in zip(distractors, distractor_texts):
        d["text"] = new_text

    # Stage 8: IMAGE ROUTING
    image_route = stage_image_route(
        stem=stem_obj["stem"],
        image_opportunity=kernel.get("imageOpportunity", "none"),
        available_images=available_images,
    )

    # Per-question image-decision trace (surfaces in the app's pipeline log) so
    # a run is self-documenting: which task this slot got, what modality the
    # kernel flagged, and whether a SOURCE figure was attached (a source figure
    # pre-empts any external borrow). Lets us diagnose "why no images" from the
    # log alone, without a rebuild.
    img_opp = (kernel.get("imageOpportunity") or "none").strip().lower()
    _src_attached = bool((image_route or {}).get("attach"))
    print(
        f"[v5-img] Q{question_number} task={target_task} order={target_order} "
        f"imageOpportunity={img_opp} sourceImageAttached={_src_attached} "
        f"externalSourcing={'on' if external_budget is not None else 'off'}",
        file=sys.stderr,
    )

    # Stage 8b: EXTERNAL IMAGE SOURCING (Phase 2)
    # Only when the source had no suitable figure (source routing declined) and
    # the kernel flagged a real image opportunity. Whether to borrow — and stem
    # vs explanation — is Gemini's per-question call (stage_external_image_query);
    # there is no deck-level quota unless V5_MAX_EXTERNAL_IMAGES forces one.
    # Borrows ONE license-clean image from an open-access library. Applies to ALL
    # Advanced Mode sources (incl. uWorld / Anki / Divine / OME) — this is the
    # generated QUESTION's image, never the source ingestion path. Degrades
    # silently to no image on any failure.
    external_media = None
    if external_budget is not None and not _src_attached and img_opp != "none":
        try:
            eq = stage_external_image_query(
                stem=stem_obj["stem"],
                image_opportunity=img_opp,
                correct_answer_concept=kernel.get("correctAnswerConcept", ""),
                discriminating_clue=kernel.get("discriminatingClueInStem", ""),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[v5-ext] Q{question_number} query stage failed: {exc}", file=sys.stderr)
            eq = {"want": False}
        if not (eq.get("want") and eq.get("query")):
            print(
                f"[v5-ext] Q{question_number} no external borrow "
                f"(model want={bool(eq.get('want'))}, opp={img_opp})",
                file=sys.stderr,
            )
        elif not external_budget.reserve():
            print(
                f"[v5-ext] Q{question_number} external budget exhausted — skipping borrow",
                file=sys.stderr,
            )
        else:
            ext = None
            try:
                import external_image_source as _eis
                ext = _eis.fetch_external_image(
                    eq["query"],
                    eq.get("modality") or img_opp,
                    cache_dir=EXTERNAL_IMAGE_CACHE_DIR,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[v5-ext] Q{question_number} fetch failed: {exc}", file=sys.stderr)
            if ext:
                # Placement is decided in CODE, not by the model: if the stem
                # or its discriminating clue references an image, the figure
                # MUST go in the stem (else the stem cites an invisible film);
                # otherwise it's a teaching illustration → explanation. (The
                # model's own placement is honored only as a tiebreak toward
                # stem, never to override a stem reference.)
                _stem_ref = stem_references_image(
                    stem_obj["stem"], kernel.get("discriminatingClueInStem", "")
                )
                _placement = "stem" if (_stem_ref or eq.get("placement") == "stem") else "explanation"
                external_media = {**ext, "placement": _placement}
                print(
                    f"[v5-ext] Q{question_number} attached external image "
                    f"from {ext.get('sourceName')} (q={eq['query']!r}, "
                    f"license={ext.get('license')!r}, placement={_placement}, "
                    f"stemRefsImage={_stem_ref}, modelSaid={eq.get('placement')!r})",
                    file=sys.stderr,
                )
            else:
                external_budget.refund()
                print(
                    f"[v5-ext] Q{question_number} fetch returned no image "
                    f"(q={eq['query']!r}) — degraded to no image",
                    file=sys.stderr,
                )

    # Stage 9: ASSEMBLE
    q = assemble_question(
        question_number=question_number,
        kernel=kernel,
        stem_obj=stem_obj,
        correct_text=correct_text,
        distractor_texts=distractor_texts,
        distractors_meta=distractors,
        critic_obj=critic,
        image_route=image_route,
        allocation=allocation,
        external_media=external_media,
        target_order=target_order,
        target_difficulty=target_difficulty,
        target_task=target_task,
        length_parity_info=parity_info,
        rng=rng,
    )

    # Debug artifact
    try:
        (DEBUG_DIR / f"Q{question_number:04d}.json").write_text(
            json.dumps(
                {
                    "kernel": kernel,
                    "stem_obj": stem_obj,
                    "correct_text": correct_text,
                    "distractors": distractors,
                    "critic": critic,
                    "image_route": image_route,
                    "length_parity": parity_info,
                    "final": q,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    except Exception:
        pass
    return q


def generate_v5(
    *,
    normalized_payload: dict[str, Any],
    allocations: list[dict[str, Any]],
    memory: dict[str, Any],
    target_order_mix: dict[str, float] | None = None,
    target_difficulty_mix: dict[str, float] | None = None,
    target_task_mix: dict[str, float] | None = None,
    available_images: list[dict[str, Any]] | None = None,
    seed: int = 0,
    max_workers: int | None = None,
    external_images: bool | None = None,
    max_external_images: int | None = None,
) -> list[dict[str, Any]]:
    target_order_mix = target_order_mix or {
        "first_order": 0.25,
        "second_order": 0.45,
        "third_order": 0.30,
    }
    target_difficulty_mix = target_difficulty_mix or {
        "easy": 0.30,
        "medium": 0.45,
        "difficult": 0.25,
    }
    # Question TASK mix (what the question ASKS), independent of reasoning depth
    # (order) above. Demotes "next step in management" from the old ~75% (it was
    # baked into 2nd+3rd order) to 20%, spreading the rest across diagnosis,
    # diagnostic workup, mechanism, etiology, associated findings, and
    # complications. Keys must match the TASK DEFINITIONS in the kernel prompt.
    target_task_mix = target_task_mix or {
        "diagnosis":        0.22,
        "next_dx_step":     0.16,
        "next_mgmt_step":   0.20,
        "mechanism":        0.16,
        "causative_agent":  0.10,
        "expected_finding": 0.10,
        "complication":     0.06,
    }
    # Phase 2: external borrowed-image sourcing. ON by default for Advanced
    # Mode (the user explicitly asked to bake it in); roll back with
    # V5_EXTERNAL_IMAGES=0. NO deck-level cap by default — whether a question
    # gets an image is Gemini's per-question want+placement decision, not a
    # quota. An optional ceiling can still be forced with V5_MAX_EXTERNAL_IMAGES=N.
    if external_images is None:
        external_images = (
            os.environ.get("V5_EXTERNAL_IMAGES", "1").strip().lower()
            not in ("0", "false", "no", "off", "")
        )
    # v5.4 Lever 1: build the full per-slot task list first (the question
    # number, allocation, target order/difficulty, per-Q deterministic seed)
    # then dispatch to a ThreadPoolExecutor for concurrent generation. Each
    # task is independent — kernel/stem/distractors/critic/regen/image-route
    # all run within generate_one_question() — so a thread can own one Q
    # end-to-end without coordination with peers.
    # v5.5 — TASK VARIETY FIX: plan all three dimensions GLOBALLY across the
    # whole deck, then deal the planned slots out to chunks in order. The old
    # code called plan_allocation_slots PER CHUNK with that chunk's (small)
    # count, so largest-remainder rounding ran inside each allocation: any task
    # whose weight * chunkSize was below the rounding cutoff got ZERO slots
    # every time. With ~3-question chunks that erased all four low-weight tasks
    # (mechanism / causative_agent / expected_finding / complication) and left
    # every deck a diagnosis + next_dx + next_mgmt monoculture. Planning once
    # over the deck total lets those tasks claim their slots (e.g. 21 Qs →
    # ~3 mechanism, 2 causative, 2 finding, 1 complication).
    chunk_counts = [max(0, int(a.get("questionCount") or 0)) for a in allocations]
    total_slots = sum(chunk_counts)
    global_plan = plan_allocation_slots(
        total_slots, target_order_mix, target_difficulty_mix, target_task_mix,
        seed=seed,
    )
    tasks: list[dict[str, Any]] = []
    qn = 0
    cursor = 0
    for alloc_idx, allocation in enumerate(allocations):
        count = chunk_counts[alloc_idx]
        if count <= 0:
            continue
        slot_plan = global_plan[cursor:cursor + count]
        cursor += count
        slide_images = allocation.get("slideImages") or available_images or []
        for (target_order, target_difficulty, target_task) in slot_plan:
            qn += 1
            tasks.append({
                "qn":                  qn,
                "allocation":          allocation,
                "target_order":        target_order,
                "target_difficulty":   target_difficulty,
                "target_task":         target_task,
                "slide_images":        slide_images,
                # Each Q gets a deterministic per-slot RNG so the position
                # shuffle in stage_assemble is reproducible regardless of
                # which thread runs it. The +q n * 7919 offset is large
                # enough to push consecutive Qs into separate RNG streams.
                "rng":                 random.Random(seed + qn * 7919),
            })

    # Build the shared external-image budget. Default is UNLIMITED: Gemini's
    # per-question want+placement decision is the only gate. A hard ceiling is
    # applied ONLY if V5_MAX_EXTERNAL_IMAGES=N (or max_external_images) is set.
    external_budget = None
    if external_images:
        if max_external_images is None:
            env_cap = os.environ.get("V5_MAX_EXTERNAL_IMAGES", "").strip()
            max_external_images = int(env_cap) if env_cap.isdigit() else None
        external_budget = ExternalImageBudget(max_external_images)
    if external_budget:
        cap_label = "uncapped" if external_budget.cap is None else f"cap {external_budget.cap}"
        print(f"[v5-ext] external images ON ({cap_label})", file=sys.stderr)
    else:
        print("[v5-ext] external images OFF", file=sys.stderr)

    # Memory is a shared mutable dict. With parallel question generation,
    # all Qs in a batch see the SAME initial memory (since they all start
    # before any complete), so the dedup signal it carries is largely lost
    # within a single batch. Updates are guarded by a lock anyway to keep
    # the dict consistent for any later sequential code paths.
    memory_lock = threading.Lock()

    def _run_task(task: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        try:
            q = generate_one_question(
                question_number=task["qn"],
                allocation=task["allocation"],
                target_order=task["target_order"],
                target_difficulty=task["target_difficulty"],
                target_task=task["target_task"],
                memory=memory,
                available_images=task["slide_images"],
                rng=task["rng"],
                external_budget=external_budget,
            )
        except Exception as exc:
            print(f"[v5.4] Q{task['qn']} pipeline error: {exc}", file=sys.stderr)
            q = None
        return task["qn"], q

    effective_workers = max(1, int(max_workers if max_workers is not None else V5_MAX_WORKERS))
    if effective_workers == 1 or len(tasks) <= 1:
        # Single-worker path keeps the v5.2-style sequential behavior so
        # an env-var rollback (V5_MAX_WORKERS=1) skips the executor entirely.
        results: dict[int, dict[str, Any] | None] = {}
        for task in tasks:
            results[task["qn"]] = _run_task(task)[1]
    else:
        print(
            f"[v5.4] generating {len(tasks)} question(s) across "
            f"{min(effective_workers, len(tasks))} worker(s)...",
            file=sys.stderr,
        )
        results = {}
        with ThreadPoolExecutor(max_workers=min(effective_workers, len(tasks))) as executor:
            futures = {executor.submit(_run_task, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                _, q = future.result()
                results[task["qn"]] = q

    # Reassemble in original (question-number) order so the global gate
    # and downstream consumers see the same sequencing they would have
    # gotten from the sequential v5.2 implementation.
    questions: list[dict[str, Any]] = []
    for task in tasks:
        q = results.get(task["qn"])
        if q:
            questions.append(q)
            with memory_lock:
                _update_memory(memory, q)
        else:
            print(f"[v5.4] Q{task['qn']} skipped (pipeline rejected)", file=sys.stderr)
    questions = randomize_global_distribution(questions, seed=seed)
    return questions


def _update_memory(memory: dict[str, Any], q: dict[str, Any]) -> None:
    diagnoses = memory.setdefault("diagnoses", [])
    target = q.get("diagnosisOrTarget", "").strip()
    if target and target not in diagnoses:
        diagnoses.append(target)
    stems = memory.setdefault("recentStemStarts", [])
    start = (q.get("stem") or "")[:120]
    if start:
        stems.append(start)
        memory["recentStemStarts"] = stems[-50:]


# ── Smoke-test CLI ──────────────────────────────────────────────────────────


def _smoke_test_cli() -> int:
    parser = argparse.ArgumentParser(description="v5.2 pipeline smoke test")
    parser.add_argument("--allocation-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    allocations = json.loads(Path(args.allocation_file).read_text(encoding="utf-8"))
    if not isinstance(allocations, list):
        allocations = allocations.get("allocations") or []
    normalized_payload = {"sourceFile": "smoke_test_v5_2"}
    memory: dict[str, Any] = {}
    started = time.time()
    questions = generate_v5(
        normalized_payload=normalized_payload,
        allocations=allocations,
        memory=memory,
        seed=args.seed,
    )
    elapsed = time.time() - started
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "schemaVersion": "v5_2-organic",
                "sourceFormat": "lecture-slide-v5_2",
                "testTitle": "v5.2 smoke test",
                "expectedQuestionCount": sum(int(a.get("questionCount") or 0) for a in allocations),
                "actualExtractedQuestionCount": len(questions),
                "extractionWarnings": [],
                "questions": questions,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"v5.2 smoke test: produced {len(questions)} questions in {elapsed:.1f}s")
    print(f"  Output: {out_path}")
    print(f"  Debug per-Q traces under: {DEBUG_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test_cli())
