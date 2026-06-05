#!/usr/bin/env python3
"""
UWorld Notes → Step 2 Question Generator
Pipeline: notes file → raw text → topic chunks → Gemini questions → canonical v3 JSON

Usage:
  python3 generate_uworld_questions.py [--dry-run] [--generate] [--questions-per-file N]
"""

import argparse
import json
import os
import random
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401  Optional reserved for v5.8 dispatch

# v4.79: google-genai SDK — unified client for both AI Studio and Vertex AI
# backends. Selected at runtime via the GEMINI_BACKEND env var. Falls back to
# raw urllib if SDK is not installed (legacy AI Studio path), so existing
# deployments keep working until they explicitly install the SDK.
try:
    from google import genai as _genai_sdk
    from google.genai import types as _genai_types
    _GENAI_SDK_AVAILABLE = True
except ImportError:
    _GENAI_SDK_AVAILABLE = False

# ── Optional imports (graceful degradation) ───────────────────────────────────
try:
    from striprtf.striprtf import rtf_to_text as _rtf_to_text
    RTF_AVAILABLE = True
except ImportError:
    RTF_AVAILABLE = False

try:
    import docx as _docx
    DOCX_AVAILABLE: object = True
except ImportError:
    try:
        import docx2txt as _docx2txt
        DOCX_AVAILABLE = "docx2txt"
    except ImportError:
        DOCX_AVAILABLE = False

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
INPUT_DIR   = BASE_DIR / "input_notes"
RAW_DIR     = BASE_DIR / "output_json" / "raw_text"
SEGMENT_DIR   = BASE_DIR / "output_json" / "chunks"
GEN_DIR     = BASE_DIR / "output_json" / "generated"
DEBUG_DIR   = BASE_DIR / "output_json" / "generated" / "debug"
APP_DIR     = BASE_DIR / "output_json" / "app_ready"
REPORT_DIR  = BASE_DIR / "reports"
PROMPT_FILE = BASE_DIR / "prompts" / "notes_to_questions_prompt.txt"
SCHEMA_FILE = BASE_DIR / "schema" / "uworld_generated_question_schema.json"

SUPPORTED_EXTENSIONS = {".txt", ".md", ".rtf", ".docx"}

# Matches repo convention from tools/nbme-pdf-json-generator/extract_pdfs.py
GEMINI_MODEL    = "gemini-2.5-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# v4.79: Vertex AI migration — backend selection + connection config.
# CUTOVER: default flipped from "ai_studio" → "vertex" after Stage 2 + Phase D
# validation passed end-to-end (Divine audio transcription in 42s, smoke tests
# clean on both backends, side-by-side prose-quality comparison favored Vertex
# with thinking mode enabled). User explicitly committed to Vertex regardless
# of any non-fatal output quirks; the $300 Free Trial absorbs all usage cost.
# To roll back to AI Studio for any single run, set: GEMINI_BACKEND=ai_studio.
# The AI Studio code path stays as a fallback for ~1 week post-cutover, then
# gets removed entirely.
GEMINI_BACKEND   = os.environ.get("GEMINI_BACKEND", "vertex").strip().lower()
GCP_PROJECT_ID   = os.environ.get("GCP_PROJECT_ID", "shamsulalamx").strip()
GCP_REGION       = os.environ.get("GCP_REGION", "us-central1").strip()
_gemini_client_singleton: Any = None


def _gemini_client():
    """Return a cached google-genai SDK client configured for the active backend.

    GEMINI_BACKEND=ai_studio (default)
        Uses the GEMINI_API_KEY env var. Same auth as pre-v4.79.

    GEMINI_BACKEND=vertex
        Uses Application Default Credentials (run `gcloud auth
        application-default login` once) + GCP_PROJECT_ID + GCP_REGION.
        Requires the Vertex AI API to be enabled on the project.

    Raises:
        EnvironmentError: if the SDK is not installed, or required env vars
            are missing for the chosen backend. Errors are loud-and-explicit
            rather than silent fallbacks so the user knows exactly what to fix.
    """
    global _gemini_client_singleton
    if _gemini_client_singleton is not None:
        return _gemini_client_singleton

    if not _GENAI_SDK_AVAILABLE:
        raise EnvironmentError(
            "google-genai SDK not installed. Install with: "
            "pip install google-genai"
        )

    if GEMINI_BACKEND == "vertex":
        if not GCP_PROJECT_ID:
            raise EnvironmentError(
                "GEMINI_BACKEND=vertex requires GCP_PROJECT_ID env var "
                "(or default 'shamsulalamx')."
            )
        _gemini_client_singleton = _genai_sdk.Client(
            vertexai=True,
            project=GCP_PROJECT_ID,
            location=GCP_REGION,
        )
    elif GEMINI_BACKEND == "ai_studio":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "GEMINI_BACKEND=ai_studio requires GEMINI_API_KEY env var."
            )
        _gemini_client_singleton = _genai_sdk.Client(api_key=api_key)
    else:
        raise EnvironmentError(
            f"GEMINI_BACKEND must be 'ai_studio' or 'vertex', "
            f"got: {GEMINI_BACKEND!r}"
        )

    return _gemini_client_singleton


def _reset_gemini_client() -> None:
    """Force the client to be re-constructed on next _gemini_client() call.

    Used by tests + the validation harness to swap backends mid-process.
    Should not be called from normal pipeline code.
    """
    global _gemini_client_singleton
    _gemini_client_singleton = None

FORBIDDEN_STRINGS = [
    "Here are the questions",
    "```json",
    "eftab720",
    "tightenfactor0",
]


# ── Logging ────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


# ── Text extraction ────────────────────────────────────────────────────────────
def extract_text(filepath: Path) -> str:
    ext = filepath.suffix.lower()

    if ext in (".txt", ".md"):
        return filepath.read_text(encoding="utf-8", errors="replace")

    if ext == ".rtf":
        if not RTF_AVAILABLE:
            warn(f"striprtf not installed; skipping {filepath.name}. Run: pip install striprtf")
            return ""
        raw = filepath.read_text(encoding="utf-8", errors="replace")
        try:
            return _rtf_to_text(raw)
        except Exception as exc:
            warn(f"RTF extraction failed for {filepath.name}: {exc}")
            return ""

    if ext == ".docx":
        if not DOCX_AVAILABLE:
            warn(f"python-docx not installed; skipping {filepath.name}. Run: pip install python-docx")
            return ""
        try:
            if DOCX_AVAILABLE == "docx2txt":
                return _docx2txt.process(str(filepath))
            else:
                doc = _docx.Document(str(filepath))
                return "\n".join(para.text for para in doc.paragraphs)
        except Exception as exc:
            warn(f"DOCX extraction failed for {filepath.name}: {exc}")
            return ""

    warn(f"Unsupported extension: {ext}")
    return ""


# ── Chunking ───────────────────────────────────────────────────────────────────
_HEADING_RE = re.compile(
    r"^(?:#+ .+|[A-Z][A-Z\s\-/&]{4,}:|={3,}|-{3,}|\*{3,})\s*$",
    re.MULTILINE,
)


def split_into_chunks(text: str, max_chars: int = 3000) -> List[Dict]:
    """
    Split notes into topic chunks.
    Strategy:
      1. Heading-based splits (markdown # or ALL-CAPS section labels).
      2. Paragraph-boundary fallback.
      3. Hard-cap each chunk at max_chars, splitting on paragraph boundaries.
      4. Force-slice any chunk that STILL exceeds max_chars (e.g. inputs with
         no headings and no double-newline separators — common for Anki .txt
         exports where each card is a single line). Without this final pass,
         oversized chunks reach Gemini intact and the response gets truncated
         at maxOutputTokens. Slice prefers single-newline boundaries, then
         whitespace, then a hard byte boundary as a last resort.
    """
    boundaries = [m.start() for m in _HEADING_RE.finditer(text)]
    if len(boundaries) >= 2:
        segments = []
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
            segments.append(text[start:end].strip())
    else:
        segments = [s.strip() for s in re.split(r"\n{2,}", text) if s.strip()]

    chunks: List[str] = []
    buffer = ""
    for seg in segments:
        if len(buffer) + len(seg) + 2 <= max_chars:
            buffer = (buffer + "\n\n" + seg).strip() if buffer else seg
        else:
            if buffer:
                chunks.append(buffer)
            if len(seg) > max_chars:
                paras = re.split(r"\n{2,}", seg)
                sub_buf = ""
                for p in paras:
                    if len(sub_buf) + len(p) + 2 <= max_chars:
                        sub_buf = (sub_buf + "\n\n" + p).strip() if sub_buf else p
                    else:
                        if sub_buf:
                            chunks.append(sub_buf)
                        sub_buf = p
                if sub_buf:
                    chunks.append(sub_buf)
                buffer = ""
            else:
                buffer = seg

    if buffer:
        chunks.append(buffer)

    # Force-slice any remaining oversized chunk. This handles inputs that have
    # no heading boundaries AND no double-newline paragraph boundaries (a real
    # case for Anki single-line-per-card .txt exports). Prior to this pass,
    # such inputs collapsed to one giant chunk that the Gemini call would then
    # truncate at maxOutputTokens.
    sliced: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            sliced.append(chunk)
            continue
        remaining = chunk
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            # Prefer a clean break: single newline, then whitespace, then hard.
            cut = window.rfind("\n")
            if cut < max_chars // 2:
                ws = window.rfind(" ")
                cut = ws if ws >= max_chars // 2 else max_chars
            else:
                cut = max(cut, 1)
            sliced.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining.strip():
            sliced.append(remaining.strip())

    return [
        {"chunkIndex": i + 1, "chunkText": c, "charCount": len(c)}
        for i, c in enumerate(sliced)
        if c.strip()
    ]


# ── Stem-quality helpers ───────────────────────────────────────────────────────
# Mirrors the lecture-slide generator's stem_has_explicit_final_question check.
# Shared via this module because OME, Mehlman, Divine, and Anki all wrap
# validate_question() from here — putting the check in UWorld fixes every
# wrapping generator in one place. Without this check, Gemini occasionally
# returns vignettes that end mid-narrative with no direct one-best-answer
# question sentence, leaving the user unable to tell what is being asked.

import re as _re_stem  # local alias to avoid clashing with any future top-level re usage

_STEM_QUESTION_PROMPT_RE = _re_stem.compile(
    r"(\?|"
    r"\bwhich of the following\b|"
    r"\bwhat is\b|"
    r"\bwhat are\b|"
    r"\bwhat should\b|"
    r"\bhow should\b|"
    r"\bwhich (?:response|intervention|treatment|therapy|medication|drug|screening|preventive|prevention|counseling|recommendation)\b|"
    r"\bmost likely\b|"
    r"\bmost appropriate\b|"
    r"\bbest (?:explains|describes|accounts for|represents|confirms|treats|managed)\b|"
    r"\bnext (?:best )?(?:step|test|management|treatment)\b)",
    _re_stem.I,
)


def _stem_clean(value) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    paragraphs = [
        _re_stem.sub(r"[ \t]+", " ", paragraph.strip())
        for paragraph in _re_stem.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]
    return "\n\n".join(paragraphs)


def _stem_strip_trailing_artifacts(stem: str) -> str:
    clean = _stem_clean(stem)
    clean = _re_stem.sub(r"\n*\s*\[Figure:[^\]]+\]\s*$", "", clean, flags=_re_stem.I)
    return clean.strip()


def _stem_final_sentence(stem: str) -> str:
    clean = _stem_strip_trailing_artifacts(stem)
    if not clean:
        return ""
    pieces = _re_stem.findall(r"[^.!?]+[.!?]?", clean, flags=_re_stem.S)
    for piece in reversed(pieces):
        sentence = _re_stem.sub(r"\s+", " ", piece).strip()
        if sentence:
            return sentence
    return _re_stem.sub(r"\s+", " ", clean).strip()


def stem_has_explicit_final_question(stem: str) -> bool:
    """True iff the stem ends with a recognizable one-best-answer prompt that
    closes with a question mark. Used by validate_question() below; if this
    returns False, the question is sent back to Gemini for repair."""
    final = _stem_final_sentence(stem)
    if not final:
        return False
    if not final.endswith("?"):
        return False
    return bool(_STEM_QUESTION_PROMPT_RE.search(final))


# ── Validation ─────────────────────────────────────────────────────────────────
def _first_explanation_text(q: Dict) -> str:
    """First non-empty explanation body text — a backfill source for the
    non-load-bearing teaching fields below."""
    for section in q.get("explanationSections") or []:
        if not isinstance(section, dict):
            continue
        joined = " ".join(
            b.strip()
            for b in (section.get("body") or [])
            if isinstance(b, str) and b.strip()
        )
        if joined:
            return joined
    return ""


def backfill_nonessential_question_fields(q: Dict) -> Dict:
    """Fill non-load-bearing teaching/metadata fields (reviewPearl,
    retrievalTag) in place from existing content, so a bare omission is never
    treated as a validation failure.

    Why this exists
    ---------------
    Gemini intermittently omits these display-only fields. Before this, a
    missing ``reviewPearl`` (or ``retrievalTag``) made ``validate_question``
    return an error, which forced an extra repair Gemini call and — if the
    model omitted it again — routed an otherwise-valid question to manual
    review (or dropped it). That is the same root cause as the FF-Peds
    lecture-slide failure (18 questions lost solely to a missing reviewPearl),
    and this is the uworld-family counterpart of the fix in
    generate_lecture_slide_questions._backfill_nonessential_question_keys.

    Shared by every generator that runs through ``call_gemini_with_retry`` /
    ``process_file`` (OME, Mehlman, Divine, Anki, UWorld). Load-bearing fields
    (stem, answerChoices, correctAnswer, explanationSections) are deliberately
    NOT touched here — they are still hard-validated below. A field that
    already holds content is never overwritten.
    """
    if not isinstance(q, dict):
        return q
    objective = str(q.get("educationalObjective") or "").strip()
    explanation = _first_explanation_text(q)
    if not str(q.get("reviewPearl") or "").strip():
        pearl = objective or explanation or str(q.get("retrievalTag") or "").strip()
        if pearl:
            q["reviewPearl"] = pearl
    if not str(q.get("retrievalTag") or "").strip():
        tag = objective or str(q.get("reviewPearl") or "").strip() or explanation
        if tag:
            q["retrievalTag"] = tag
    return q


def validate_question(q: Dict) -> List[str]:
    """Returns a list of error strings. Empty = valid."""
    errors: List[str] = []

    # Backfill non-load-bearing teaching/metadata fields (reviewPearl,
    # retrievalTag) BEFORE the required-field checks, so a bare omission never
    # forces a wasted repair call or routes a usable question to review. The
    # load-bearing checks below are unchanged; a question with no content to
    # backfill from still fails (its empty explanationSections also fail).
    backfill_nonessential_question_fields(q)

    if not q.get("questionNumber"):
        errors.append("missing questionNumber")

    stem = q.get("stem", "")
    if not stem or not stem.strip():
        errors.append("missing or empty stem")
    elif not stem_has_explicit_final_question(stem):
        # Forces the repair retry when Gemini returns a vignette that ends
        # mid-narrative without a one-best-answer question. Shared across all
        # generators that import this module (OME, Mehlman, Divine, Anki).
        errors.append("stem must end with an explicit one-best-answer question sentence (ending in '?')")

    choices = q.get("answerChoices", [])
    if len(choices) != 4:
        errors.append(f"expected 4 answerChoices, got {len(choices)}")
    else:
        for c in choices:
            if not c.get("text", "").strip():
                errors.append(f"empty text for choice {c.get('label', '?')}")

    labels = {c.get("label") for c in choices}
    correct = q.get("correctAnswer", "")
    if not correct or correct not in labels:
        errors.append(f"correctAnswer '{correct}' not in choice labels {labels}")

    sections = q.get("explanationSections", [])
    if not sections:
        errors.append("missing explanationSections")
    else:
        for s in sections:
            body = s.get("body", [])
            if not body or all(not b.strip() for b in body):
                errors.append(f"empty body in explanationSection '{s.get('heading', '?')}'")

    if not q.get("retrievalTag", "").strip():
        errors.append("missing or empty retrievalTag")

    if not q.get("reviewPearl", "").strip():
        errors.append("missing or empty reviewPearl")

    blob = json.dumps(q)
    for forbidden in FORBIDDEN_STRINGS:
        if forbidden in blob:
            errors.append(f"forbidden string found: '{forbidden}'")

    return errors


def check_duplicate_stems(questions: List[Dict]) -> List[str]:
    """Returns duplicate-stem warning strings."""
    warnings: List[str] = []
    seen: Dict[str, int] = {}
    for q in questions:
        stem = q.get("stem", "").strip()
        if stem and stem in seen:
            warnings.append(
                f"Duplicate stem: q{str(q.get('questionNumber','?')).zfill(3)} "
                f"matches q{str(seen[stem]).zfill(3)}"
            )
        elif stem:
            seen[stem] = q.get("questionNumber", 0)
    return warnings


# ── Failure classification + quota latch (v4.54) ───────────────────────────────
# Mirrors the v4.49 quota-aware retry stop from the lecture-slide generator.
# All five UWorld-wrapping generators (OME, Mehlman, Divine, Anki, UWorld)
# inherit these helpers via `import generate_uworld_questions as _uw`.
#
# The latch protects against runaway API spending: once any Gemini call
# returns HTTP 429 / RESOURCE_EXHAUSTED, every subsequent retry path
# (in-band repair, between chunks, the v4.54 per-chunk recovery loop)
# short-circuits to no-op instead of burning the rest of the user's
# prepayment budget. Reset at the start of each process_file() invocation
# so two runs in the same process stay independent.

def is_quota_failure(error) -> bool:
    text = str(error).lower()
    return (
        # AI Studio patterns (pre-v4.79, kept verbatim for backwards compat)
        "http 429" in text
        or "resource_exhausted" in text
        or "prepayment credits are depleted" in text
        or "quota exceeded" in text
        or "rate limit" in text
        or "too many requests" in text
        # v4.79: Vertex AI-specific patterns. Vertex error messages tend to
        # be more structured (mention specific quota metrics by name) and use
        # different prose for the same conditions. Conservative additions —
        # add more here as we observe real Vertex 429s in production.
        or "quota metric" in text
        or "online_prediction_requests" in text
        or "generate_content_requests" in text
        or "generate_content_input_tokens" in text
        or "exhausted the quota" in text
        or "quota_exceeded" in text
        or "429 resource exhausted" in text
        # gRPC status code text — google-genai SDK may surface these
        or "status.resource_exhausted" in text
        or "code=429" in text
    )


def is_network_failure(error) -> bool:
    text = str(error).lower()
    return (
        # AI Studio patterns (pre-v4.79)
        "urlopen error" in text
        or "nodename nor servname provided" in text
        or "name or service not known" in text
        or "network is unreachable" in text
        or "temporary failure in name resolution" in text
        or "gemini request timed out" in text
        # v4.79: SDK / gRPC failure modes
        or "connection refused" in text
        or "connection reset" in text
        or "deadline exceeded" in text
        or "service unavailable" in text
        or "status.unavailable" in text
    )


_QUOTA_EXHAUSTED = False


def quota_exhausted() -> bool:
    return _QUOTA_EXHAUSTED


def mark_quota_exhausted() -> None:
    global _QUOTA_EXHAUSTED
    _QUOTA_EXHAUSTED = True


def reset_quota_state() -> None:
    global _QUOTA_EXHAUSTED
    _QUOTA_EXHAUSTED = False


def _quota_backoff_schedule() -> List[float]:
    """Seconds to wait before each retry after a transient 429 / network error.

    The schedule deliberately crosses the ~60s free-tier per-minute (RPM)
    window so a transient rate limit resolves on its own instead of killing
    the whole run. Override with GEMINI_QUOTA_RETRY_DELAYS (comma-separated
    seconds, e.g. "30,60,90,120"); more entries = more patience before the run
    gives up on a chunk. Set it to "0"/"off"/"none" to restore the old
    fail-fast behavior. Default = 6 retries spanning ~8 minutes, enough to ride
    out per-minute limits while still latching promptly on a true daily cap.
    """
    raw = (os.environ.get("GEMINI_QUOTA_RETRY_DELAYS", "") or "").strip()
    if raw:
        if raw.lower() in ("0", "off", "none", "false"):
            return []
        try:
            vals = [max(0.0, float(x)) for x in raw.split(",") if x.strip()]
            if vals:
                return vals
        except Exception:
            pass
    return [20.0, 45.0, 75.0, 120.0, 120.0, 120.0]


# Bound on the per-chunk shortfall recovery loop in process_file(). The main
# chunk loop produces some output; if a chunk returned fewer questions than
# requested AND the quota latch isn't tripped, we make up to this many focused
# follow-up calls per chunk asking Gemini for just the missing questions.
# Worst-case extra API cost = len(chunks) * this constant. Stays small on
# purpose so a budget runaway cannot happen even when every chunk under-
# delivers.
MAX_RECOVERY_ATTEMPTS_PER_CHUNK = 2


# ── Placeholder (dry-run) ──────────────────────────────────────────────────────
def _placeholder_question(n: int) -> Dict:
    idx = str(n).zfill(3)
    return {
        "id": f"q{idx}",
        "questionNumber": n,
        "sourceQuestionNumber": n,
        "retrievalTag": "[DRY-RUN placeholder — not generated]",
        "reviewPearl": "[DRY-RUN placeholder — not generated]",
        "clinicalPearl": None,
        "stem": "[DRY-RUN] This question was not generated. Run without --dry-run to call Gemini.",
        "hasEmbeddedFigure": False,
        "figureRefs": [],
        "answerChoices": [
            {"label": "A", "text": "Placeholder A"},
            {"label": "B", "text": "Placeholder B"},
            {"label": "C", "text": "Placeholder C"},
            {"label": "D", "text": "Placeholder D"},
        ],
        "correctAnswer": "A",
        "educationalObjective": "[DRY-RUN placeholder]",
        "explanationSections": [
            {"heading": "Correct Answer Explanation", "body": ["[DRY-RUN placeholder]"]},
            {"heading": "Incorrect Answer Explanation", "body": ["[DRY-RUN placeholder]"]},
            {"heading": "Educational Objective",        "body": ["[DRY-RUN placeholder]"]},
        ],
        "tables": [],
        "sharedGroup": None,
        "extractionWarnings": ["dry-run: question not generated"],
    }


# ── Review-draft writer (v4.53) ────────────────────────────────────────────────
# When the user enables the review-survivor flow by passing a
# needs_review_collector list to call_gemini_with_retry(), questions that
# fail BOTH initial validation AND the repair retry are routed here instead
# of being silently included in the app-ready output with `extractionWarnings`.
#
# The schema below is the same contract BIC's discover_review_draft() and
# the renderer's readReviewDraft handler already use for the lecture-slide
# generator. Required: file name ends with `_review_draft.json`,
# draftVersion == 1, status == 'needs_review', candidateQuestions is a
# non-empty list, validQuestionIndexes is a list. The renderer surfaces
# candidates for accept/edit/reject; accepted ones go through the v4.50
# canonicalize-and-append-to-existing-test path in electron/main.js.
#
# OME, Mehlman, Divine, Anki, and UWorld all share this code via
# `import generate_uworld_questions as _uw`, so adding the writer here
# enables the review flow for all five wrapping generators.

def _resolve_review_dir() -> Path:
    """Resolve the review draft directory at call time.

    Always returns BIC_JOB_OUTPUT_ROOT/review when BIC sets that env var
    (the normal Batch Import path). Otherwise falls back to BASE_DIR/review
    for standalone CLI runs. Computed at call time so per-wrapper
    monkey-patching of BASE_DIR is honored without each wrapper having to
    monkey-patch a REVIEW_DIR constant too.
    """
    bic_root = os.environ.get("BIC_JOB_OUTPUT_ROOT")
    if bic_root:
        return Path(bic_root).expanduser().resolve() / "review"
    return BASE_DIR / "review"


def write_uworld_family_review_draft(
    source_label: str,
    source_type: str,
    source_format: str,
    needs_review_entries: List[Dict],
) -> Optional[Path]:
    """Write a review_draft.json for questions that failed initial validation
    AND the repair retry. Returns the path, or None if nothing to write."""
    if not needs_review_entries:
        return None
    review_dir = _resolve_review_dir()
    review_dir.mkdir(parents=True, exist_ok=True)

    candidate_questions = [entry["question"] for entry in needs_review_entries]
    review_items = [
        {
            "questionIndex": i + 1,  # 1-based; matches renderer convention
            "severity": "error",
            "scope": "question",
            "code": "REPAIR_RETRY_FAILED",
            "messages": list(entry.get("errors") or []),
            "chunkIndex": entry.get("chunkIndex"),
        }
        for i, entry in enumerate(needs_review_entries)
    ]

    draft = {
        "draftVersion": 1,
        "schemaVersion": "nbme-gemini-json-v3",
        "sourceFormat": source_format,
        "sourceType": source_type,
        "jobId": str(os.environ.get("BIC_JOB_ID") or "").strip(),
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "needs_review",
        "candidateQuestions": candidate_questions,
        "validationErrors": [],
        "validationWarnings": [],
        "semanticGroundingFindings": [],
        "reviewItems": review_items,
        "fatalRejects": [],
        # Intentionally empty: every candidate here failed repair and needs
        # human review. Clean questions never reach this draft; they go to
        # the normal app-ready output and BIC auto-imports them.
        "validQuestionIndexes": [],
        "outputPaths": {
            "reviewDraftPath": "",
            "appReadyPath": "",
            "reportPath": "",
        },
        "sourceLabel": source_label,
    }
    path = review_dir / "uworld_family_review_draft.json"
    draft["outputPaths"]["reviewDraftPath"] = str(path.resolve())
    path.write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Review draft → {path}")
    return path


# ── Gemini API (raw HTTP — no SDK dependency, matches repo convention) ─────────

def _clean_llm_json(text: str) -> str:
    # Clean common LLM formatting artifacts that break json.loads().
    # All transforms are idempotent -- safe to call multiple times.

    # Strip UTF-8 BOM (\ufeff)
    text = text.lstrip(u'\ufeff')

    # Remove all markdown code-fence variants: ``` ```json ```JSON
    text = re.sub(r'```+(?:json|JSON)?\s*', '', text)

    # Normalize curly/smart double-quotes (U+201C, U+201D) -> straight ASCII
    text = text.replace(u'\u201c', '"').replace(u'\u201d', '"')
    # Normalize curly/smart single-quotes (U+2018, U+2019) -> apostrophe
    text = text.replace(u'\u2018', "'").replace(u'\u2019', "'")

    # Fix Gemini keyed-object-in-array bug:
    # Gemini sometimes writes  "SectionName": { ... }  inside a JSON array.
    # That is illegal JSON. Strip the leading key to leave just { ... }.
    # Matches all known section heading variants, case-insensitively.
    text = re.sub(
        r'"(?:Correct Answer Explanation|Incorrect Answer Explanation|'
        r'Educational Objective|Clinical Pearl|Explanation|'
        r'Correct|Incorrect)(?:\s+[A-Za-z]+)*"\s*:\s*(\{)',
        r'\1',
        text,
        flags=re.IGNORECASE,
    )

    # Remove trailing commas before } or ] -- e.g. ,\n} and ,  ]
    text = re.sub(r',(\s*[}\]])', r'\1', text)

    return text.strip()


def _extract_json_payload(text: str) -> str:
    """
    Locate the first complete JSON array or object in text and return it.
    Skips leading prose (e.g. 'Here are the questions:') and trailing prose.
    Uses a bracket-depth counter that respects quoted strings and escape sequences.
    Returns the original text unchanged if no JSON structure is found.
    """
    # Find first structural character
    first = -1
    open_ch = close_ch = ""
    for i, ch in enumerate(text):
        if ch == "[":
            first, open_ch, close_ch = i, "[", "]"
            break
        if ch == "{":
            first, open_ch, close_ch = i, "{", "}"
            break

    if first == -1:
        return text  # no JSON structure visible

    depth = 0
    in_string = False
    escape_next = False
    last_close = first

    for i in range(first, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                last_close = i
                break

    return text[first : last_close + 1]


def _parse_gemini_json(raw_text: str, chunk_index: int) -> list:
    """
    Three-stage JSON parse with progressive cleaning.
    Stage 1 — minimal strip only.
    Stage 2 — full _clean_llm_json().
    Stage 3 — _extract_json_payload() on cleaned text.
    On total failure: saves raw response to DEBUG_DIR, raises JSONDecodeError.
    Never prints the full raw response to the terminal.
    """
    text = raw_text.strip().lstrip("﻿")

    # Stage 1: try as-is (fast path — succeeds when Gemini behaves)
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        pass

    # Stage 2: clean fences, quotes, trailing commas
    cleaned = _clean_llm_json(text)
    try:
        result = json.loads(cleaned)
        log(f"  Chunk {chunk_index}: parsed after LLM JSON cleaning")
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError:
        pass

    # Stage 3: extract the bare JSON payload, stripping surrounding prose
    payload = _extract_json_payload(cleaned)
    try:
        result = json.loads(payload)
        log(f"  Chunk {chunk_index}: parsed after payload extraction")
        return result if isinstance(result, list) else [result]
    except json.JSONDecodeError as final_exc:
        # Save raw for offline debugging; do NOT print it to the terminal
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DEBUG_DIR / f"chunk{chunk_index}_raw_response.txt"
        debug_path.write_text(raw_text, encoding="utf-8")
        warn(
            f"Chunk {chunk_index}: JSON parse failed after all 3 cleanup stages — "
            f"raw response saved to debug/chunk{chunk_index}_raw_response.txt"
        )
        raise final_exc


def load_prompt_template() -> str:
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")
    return PROMPT_FILE.read_text(encoding="utf-8")


def _raw_gemini_call(api_key: str, prompt: str) -> str:
    """Single Gemini generateContent call. Never logs the key.

    v4.79: rewritten to use google-genai SDK via _gemini_client(). The api_key
    arg is kept for signature compatibility — all existing callers pass it in
    — but it's only actually used when GEMINI_BACKEND=ai_studio (Vertex uses
    ADC). Error formats are preserved (text-matchable by is_quota_failure /
    is_network_failure in the calling retry layer).

    The 16384 max_output_tokens cap is the v4.5x headroom for Anki .txt
    exports that ask for ~15 questions per chunk. Pre-v4.5x default was 8192;
    that caused truncation mid-JSON-string which the 3-stage JSON cleanup
    couldn't recover. Force-slice in split_into_chunks is the primary defense;
    this token bump is the secondary safety net. Same value on both backends.
    """
    # v4.81: ride out transient HTTP 429 / RESOURCE_EXHAUSTED and network blips
    # instead of giving up on the first one. Free-tier Gemini enforces a
    # per-MINUTE request ceiling that resets every ~60s; the old code treated
    # the first 429 as terminal for the whole run (latched + skipped every
    # remaining chunk), which is why a 944-chunk Anki deck produced only 44
    # questions. We back off and retry the SAME call until the schedule is
    # exhausted; only then does the exception propagate so the caller's quota
    # latch trips (true daily-cap exhaustion). Healthy runs add zero delay.
    delays = _quota_backoff_schedule()
    attempt = 0
    while True:
        try:
            client = _gemini_client()
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=_genai_types.GenerateContentConfig(
                    temperature=0.4,
                    # v4.79: Bumped from 16384 to 32768 to absorb thinking-token
                    # consumption alongside the actual output. Question-gen runs
                    # at ~12-15 questions per chunk on Anki .txt exports; with
                    # dynamic thinking enabled (typically 1-4K thinking tokens
                    # for question-gen tasks), we need ~28K headroom for the
                    # actual JSON output to land cleanly without truncation.
                    max_output_tokens=32768,
                    # v4.79: Dynamic thinking enabled (budget=-1 lets the model
                    # decide how much reasoning to do based on task complexity).
                    # User priority: quality > cost. The $300 Vertex Free Trial
                    # absorbs the higher per-call cost during validation.
                    # Earlier iteration had thinking_budget=0 to match pre-v4.79
                    # raw-HTTP behavior; flipped to -1 per explicit user request.
                    thinking_config=_genai_types.ThinkingConfig(thinking_budget=-1),
                ),
            )
            break
        except EnvironmentError:
            # _gemini_client() raises EnvironmentError on misconfig — re-raise
            # as-is so the operator sees the actionable message.
            raise
        except Exception as exc:
            # Transient quota (429) / network blip → wait and retry the SAME
            # call. Only after the backoff schedule is exhausted do we re-raise
            # in the is_quota_failure-compatible format so the caller latches.
            transient = is_quota_failure(exc) or is_network_failure(exc)
            if transient and attempt < len(delays):
                base = delays[attempt]
                delay = base + random.uniform(0.0, min(5.0, base * 0.2))
                kind = "quota/429" if is_quota_failure(exc) else "network"
                print(
                    f"[quota-backoff] {kind} on Gemini call; waiting {delay:.0f}s "
                    f"then retrying ({attempt + 1}/{len(delays)})",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                attempt += 1
                continue
            # Re-raise with a string format compatible with is_quota_failure /
            # is_network_failure text matching downstream. Preserve the original
            # exception via `from exc` for traceback fidelity.
            raise ValueError(f"Gemini call failed: {exc}") from exc

    text = getattr(response, "text", None)
    if not text:
        # Diagnostic detail — quota/safety filters can return empty candidates
        # with finish_reason='SAFETY'/'MAX_TOKENS'/etc. that calling code wants
        # to see in the error.
        candidates = getattr(response, "candidates", None) or []
        raise ValueError(
            f"Gemini returned empty text: candidates={candidates!r}"[:400]
        )

    return str(text)


def _build_repair_prompt(
    chunk_text: str,
    failed_questions: List[Dict],
    errors_per_q: List[List[str]],
) -> str:
    error_block = ""
    for i, (q, errs) in enumerate(zip(failed_questions, errors_per_q)):
        error_block += f"\nQuestion {i+1} (id={q.get('id','?')}) errors:\n"
        for e in errs:
            error_block += f"  - {e}\n"
        error_block += f"  Stem preview: {q.get('stem','')[:120]}\n"

    forbidden_list = ", ".join(f'"{s}"' for s in FORBIDDEN_STRINGS)
    return (
        "You are fixing invalid NBME-style questions. Fix ONLY the listed validation errors.\n"
        "Keep all valid fields unchanged. Return a JSON array of the corrected questions only.\n"
        "Raw JSON — no markdown fences, no extra text.\n\n"
        f"VALIDATION ERRORS TO FIX:\n{error_block}\n"
        f"ORIGINAL QUESTIONS JSON:\n{json.dumps(failed_questions, indent=2)}\n\n"
        f"ORIGINAL NOTES CONTEXT (first 2000 chars):\n{chunk_text[:2000]}\n\n"
        "Rules reminder:\n"
        "- Exactly 4 answerChoices labeled A, B, C, D\n"
        "- correctAnswer must be one of A/B/C/D and match a choice label\n"
        "- retrievalTag: hyperspecific, under 12 words\n"
        "- reviewPearl: one high-yield sentence\n"
        f"- Forbidden strings (must not appear): {forbidden_list}\n\n"
        "Return the fixed questions as a JSON array only."
    )


def call_gemini_with_retry(
    chunk_text: str,
    num_questions: int,
    chunk_index: int,
    stats: Dict,
    needs_review_collector: Optional[List[Dict]] = None,
) -> Tuple[List[Dict], List[str]]:
    """
    Call Gemini for one chunk, validate all returned questions.
    Retry invalid questions once with a repair prompt.
    Returns (questions, warnings). Always continues — never raises on partial failure.

    If needs_review_collector is passed, questions that fail BOTH initial
    validation AND repair are appended to that list (each entry:
    {question, errors, chunkIndex}) instead of being silently included in
    the returned `questions` list with extractionWarnings. The caller is
    responsible for writing those entries to a review_draft.json (see
    write_uworld_family_review_draft). When None (default), behavior is
    backward-compatible: failed-repair questions are kept with warnings.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY is not set")

    template = load_prompt_template()
    prompt = (
        template
        .replace("{{NOTES_CHUNK}}", chunk_text)
        .replace("{{NUM_QUESTIONS}}", str(num_questions))
    )

    warnings: List[str] = []

    # ── Quota-aware early exit ────────────────────────────────────────────────
    # If a prior chunk or recovery attempt in this run tripped the quota
    # latch, do not burn additional API calls. The latch is set on first
    # HTTP 429 / RESOURCE_EXHAUSTED. process_file() resets the latch at
    # the start of each file so subsequent runs in the same process are
    # independent.
    if quota_exhausted():
        msg = f"Chunk {chunk_index}: skipping — Gemini quota already exhausted earlier in this run"
        warn(msg)
        warnings.append(msg)
        return [], warnings

    # ── Attempt 1 ─────────────────────────────────────────────────────────────
    try:
        raw_text = _raw_gemini_call(api_key, prompt)
    except Exception as exc:
        if is_quota_failure(exc):
            mark_quota_exhausted()
            msg = f"Chunk {chunk_index}: stopped after Gemini quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED)"
            warn(msg)
            warnings.append(msg)
            return [], warnings
        raise

    questions = _parse_gemini_json(raw_text, chunk_index)
    if not isinstance(questions, list):
        raise ValueError(f"Gemini returned non-list JSON ({type(questions).__name__})")

    # ── Validate each question ─────────────────────────────────────────────────
    valid:        List[Dict]       = []
    need_repair:  List[Dict]       = []
    repair_errors: List[List[str]] = []

    for q in questions:
        errs = validate_question(q)
        if errs:
            need_repair.append(q)
            repair_errors.append(errs)
            stats["validationFailures"] += 1
        else:
            valid.append(q)

    if not need_repair:
        return valid, warnings

    # ── Attempt 2 — repair ────────────────────────────────────────────────────
    stats["retries"] += 1
    msg = f"Chunk {chunk_index}: {len(need_repair)} question(s) failed validation — retrying"
    warn(msg)
    warnings.append(msg)

    try:
        repair_prompt = _build_repair_prompt(chunk_text, need_repair, repair_errors)
        try:
            repair_raw = _raw_gemini_call(api_key, repair_prompt)
        except Exception as exc:
            if is_quota_failure(exc):
                mark_quota_exhausted()
                msg = f"Chunk {chunk_index}: repair stopped after Gemini quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED)"
                warn(msg)
                warnings.append(msg)
                # Surface the failed questions for human review so they
                # aren't silently dropped just because budget ran out.
                if needs_review_collector is not None:
                    for q, errs in zip(need_repair, repair_errors):
                        needs_review_collector.append({
                            "question": q,
                            "errors": list(errs) + ["quota exhausted during repair"],
                            "chunkIndex": chunk_index,
                        })
                return valid, warnings
            raise
        repaired = _parse_gemini_json(repair_raw, chunk_index)
        if not isinstance(repaired, list):
            raise ValueError("Repair response is not a JSON array")

        for q in repaired:
            errs = validate_question(q)
            if errs:
                fail_msg = f"Chunk {chunk_index}: repair still invalid — {errs[:2]}"
                warn(fail_msg)
                warnings.append(fail_msg)
                stats["repairFailures"] += 1
                q.setdefault("extractionWarnings", []).extend(errs)
                if needs_review_collector is not None:
                    # Route to human review instead of silently keeping with
                    # warnings. The caller writes these to a review_draft.json
                    # for the BIC review modal.
                    needs_review_collector.append({
                        "question": q,
                        "errors": list(errs),
                        "chunkIndex": chunk_index,
                    })
                else:
                    valid.append(q)  # legacy: include with warnings (no review wired)
            else:
                valid.append(q)
                stats["repairsSucceeded"] += 1

    except Exception as exc:
        fail_msg = f"Chunk {chunk_index}: repair call failed ({exc}) — {len(need_repair)} question(s) dropped"
        warn(fail_msg)
        warnings.append(fail_msg)
        stats["repairFailures"] += len(need_repair)

    return valid, warnings


# ── Answer-position balancer (v6.0) ─────────────────────────────────────────────
# Gemini emits answer choices as [correct, distractor, distractor, distractor],
# so ~80% of organically generated questions have the correct answer at position
# "A". This balances the correct-answer position across each file's questions
# (≈25% A/B/C/D) and relabels any answer-choice letters embedded in the
# explanation prose IN LOCKSTEP — using the exact boundary-anchored grammar that
# was proven non-corrupting over all 942 questions of the existing "Peds Anki"
# set (medical letter look-alikes like "Hemophilia A", "Vitamin C", "Group B
# Strep", "E. coli" are never touched).
#
# Scope — runs ONLY for normal-mode organic questions that route through
# build_app_ready_json (UWorld notes, Anki, OME, Divine, Mehlman). It is a no-op
# for:
#   • v5 questions  — they carry a "_v5_2" marker and are skipped per-question;
#     the v5 pipeline's own distribution gate already balances them.
#   • NBME / AMBOSS verbatim extraction — those never call build_app_ready_json.
# Any question that cannot be PROVEN safe (positional verify + inverse round-trip)
# is left EXACTLY as generated, so the worst case is a residual A-lean on a few
# questions, never a corrupted letter reference.
ANSWER_BALANCE_ENABLED = True            # master switch — set False to disable
_BAL_SEED_BASE = 0x5A1AD                  # deterministic per-file seed base
_BAL_LABELS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

_BAL_LETTERRUN = r'[A-E](?:\s*(?:,\s*and\s+|,\s*|\s+and\s+)[A-E])*'
_BAL_LETTERRUN_OR = r'[A-E](?:\s*(?:,\s*and\s+|,\s*or\s+|,\s*|\s+and\s+|\s+or\s+)[A-E])+'
_BAL_WORD = re.compile(r'\b(?:choices?|options?|answers?)(?:\s+choices?)?\s+(' + _BAL_LETTERRUN + r')', re.IGNORECASE)
_BAL_PAREN = re.compile(r'\(([A-E])\)')
_BAL_PRUN = re.compile(r'\((' + _BAL_LETTERRUN_OR + r')\)')
_BAL_MED_BEFORE = re.compile(r'(?:hemophilia|hepatitis|vitamins?)\s*$', re.I)
_BAL_BREF = re.compile(
    r'(?:^|>|\n[ \t]*|[.;:]\s+)(' + _BAL_LETTERRUN + r')'
    r'(?=\)\s|:\s|\.\s[A-Z0-9"\']|\s+(?:is|are|represent|represents|describe|describes|'
    r'do|does|omit|omits|incorrectly|correctly)\b)')


def _bal_stable_seed(s: str) -> int:
    """FNV-1a — a stable, import-free hash so balancing is reproducible per file."""
    h = 2166136261
    for ch in (s or "").encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h


def _bal_choice_ref_positions(text):
    """Set of string indices whose char is a genuine choice-reference letter."""
    if not text:
        return set()
    P = set()
    for m in _BAL_PAREN.finditer(text):
        P.add(m.start(1))
    for m in _BAL_WORD.finditer(text):
        run, base = m.group(1), m.start(1)
        for mm in re.finditer(r'[A-E]', run):
            P.add(base + mm.start())
    for m in _BAL_BREF.finditer(text):
        run, base = m.group(1), m.start(1)
        for mm in re.finditer(r'[A-E]', run):
            P.add(base + mm.start())
    for m in _BAL_PRUN.finditer(text):
        if _BAL_MED_BEFORE.search(text[:m.start()]):
            continue  # medical subtype list, e.g. "Hemophilia (A, B, C)"
        run, base = m.group(1), m.start(1)
        for mm in re.finditer(r'[A-E]', run):
            P.add(base + mm.start())
    return P


def _bal_relabel(text, M):
    if not text:
        return text
    pos = _bal_choice_ref_positions(text)
    if not pos:
        return text
    chars = list(text)
    for i in pos:
        chars[i] = M.get(chars[i], chars[i])
    return ''.join(chars)


def _bal_verify(old, new, M):
    """Length preserved + every ref letter remapped + every other char identical.
    Exact because all swaps are single-char."""
    if old is None and new is None:
        return True
    old = old or ""
    new = new or ""
    if len(old) != len(new):
        return False
    pos = _bal_choice_ref_positions(old)
    for i in range(len(old)):
        exp = M.get(old[i], old[i]) if i in pos else old[i]
        if new[i] != exp:
            return False
    return True


def _bal_text_blocks(q):
    """(key, original_text) for every relabelable explanation field of a
    GENERATED question. Stem and answer-choice texts are intentionally excluded —
    they do not reference other choices by letter."""
    blocks = []
    for f in ('educationalObjective', 'reviewPearl', 'clinicalPearl'):
        if isinstance(q.get(f), str):
            blocks.append((f, q[f]))
    for si, sec in enumerate(q.get('explanationSections') or []):
        body = sec.get('body')
        if isinstance(body, list):
            for bi, b in enumerate(body):
                if isinstance(b, str):
                    blocks.append((('ES', si, bi), b))
    return blocks


def _bal_eligible(q):
    """Only a clean 2+-choice MCQ with unique choice texts and a valid correct
    label can be safely text-mapped. v5 questions are excluded by their marker."""
    if not isinstance(q, dict) or q.get('_v5_2') is not None:
        return False
    choices = q.get('answerChoices')
    if not isinstance(choices, list) or len(choices) < 2:
        return False
    labels = [c.get('label') for c in choices if isinstance(c, dict)]
    texts = [c.get('text') for c in choices if isinstance(c, dict)]
    if len(labels) != len(choices) or len(texts) != len(choices):
        return False
    if any(not isinstance(l, str) or not l for l in labels):
        return False
    if any(not isinstance(t, str) for t in texts):
        return False
    if len(set(texts)) != len(texts):
        return False  # duplicate choice text -> cannot map old->new label by text
    return q.get('correctAnswer') in labels


def _bal_apply_one(q, target_pos, qseed):
    """Mutate q in place and return True IFF the shuffle is proven safe; else
    leave q exactly as-is and return False."""
    choices = q['answerChoices']
    n = len(choices)
    by_label = {c['label']: c['text'] for c in choices}
    correct_old = q['correctAnswer']
    correct_text = by_label[correct_old]
    distractors = [c['text'] for c in choices if c['label'] != correct_old]
    random.Random(qseed).shuffle(distractors)
    tpos = target_pos % n
    arrangement = [None] * n
    arrangement[tpos] = correct_text
    di = 0
    for p in range(n):
        if p == tpos:
            continue
        arrangement[p] = distractors[di]
        di += 1
    new_labels = _BAL_LABELS[:n]
    text_to_new = {arrangement[p]: new_labels[p] for p in range(n)}
    M = {c['label']: text_to_new[c['text']] for c in choices}   # old label -> new label
    Minv = {v: k for k, v in M.items()}

    # Relabel every explanation field; PROVE each with positional verify + an
    # inverse round-trip before committing anything.
    new_vals = []
    for key, old_text in _bal_text_blocks(q):
        new_text = _bal_relabel(old_text, M)
        if not _bal_verify(old_text, new_text, M):
            return False
        if _bal_relabel(new_text, Minv) != old_text:
            return False
        new_vals.append((key, new_text))

    q['answerChoices'] = [{'label': new_labels[p], 'text': arrangement[p]} for p in range(n)]
    q['correctAnswer'] = new_labels[tpos]
    for key, new_text in new_vals:
        if isinstance(key, tuple) and key[0] == 'ES':
            _, si, bi = key
            q['explanationSections'][si]['body'][bi] = new_text
        else:
            q[key] = new_text
    return True


def balance_answer_positions(questions, source_stem=""):
    """Balance correct-answer position across a file's eligible questions and
    relabel embedded choice-letter references in lockstep. Mutates `questions`
    in place; returns a stats dict. No-op for v5 / non-MCQ / unprovable items."""
    stats = {"balanced": 0, "skipped_v5": 0, "ineligible": 0, "unprovable": 0}
    if not ANSWER_BALANCE_ENABLED or not isinstance(questions, list):
        return stats
    eligible = []
    for i, q in enumerate(questions):
        if isinstance(q, dict) and q.get('_v5_2') is not None:
            stats["skipped_v5"] += 1
        elif _bal_eligible(q):
            eligible.append(i)
        else:
            stats["ineligible"] += 1
    base_seed = _BAL_SEED_BASE ^ _bal_stable_seed(source_stem)
    rng = random.Random(base_seed)
    by_n = {}
    for i in eligible:
        by_n.setdefault(len(questions[i]['answerChoices']), []).append(i)
    target = {}
    for nopt, idxs in by_n.items():
        seq = [k % nopt for k in range(len(idxs))]
        rng.shuffle(seq)
        for i, pos in zip(idxs, seq):
            target[i] = pos
    for i in eligible:
        if _bal_apply_one(questions[i], target[i], base_seed * 131 + i):
            stats["balanced"] += 1
        else:
            stats["unprovable"] += 1
    return stats


# ── App-ready wrapper ──────────────────────────────────────────────────────────
def build_app_ready_json(
    source_stem: str,
    questions: List[Dict],
    warnings: List[str],
) -> Dict:
    # v6.0: de-bias correct-answer position for normal-mode organic questions.
    # No-op for v5 (per-question "_v5_2" skip) and verbatim (never reaches here).
    # Mutates `questions` in place; summary goes to the console log, NOT to
    # extractionWarnings (which would surface as a warning in the import dialog).
    try:
        _bal = balance_answer_positions(questions, source_stem)
        if _bal["balanced"] or _bal["unprovable"]:
            log(f"  answer-balance: shuffled {_bal['balanced']}, "
                f"kept-as-is {_bal['unprovable']} (unprovable), "
                f"v5-skipped {_bal['skipped_v5']}, non-MCQ {_bal['ineligible']}")
    except Exception as exc:  # never let balancing break generation
        warn(f"answer-balance skipped (internal error: {exc}); questions unchanged")
    return {
        "schemaVersion": "nbme-gemini-json-v3",
        "testTitle": source_stem,
        "sourceFormat": "uworld-notes",
        "expectedQuestionCount": None,
        "actualExtractedQuestionCount": len(questions),
        "extractionWarnings": warnings,
        "questions": questions,
    }


def renumber_questions(questions: List[Dict], offset: int) -> List[Dict]:
    for i, q in enumerate(questions):
        n = offset + i + 1
        q["id"] = f"q{str(n).zfill(3)}"
        q["questionNumber"] = n
        q["sourceQuestionNumber"] = n
    return questions


# ── Report ─────────────────────────────────────────────────────────────────────
def write_report(data: Dict, prefix: str = "question_generation_report") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"{prefix}_{ts}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"Report → {path.name}")
    return path


# ── Main pipeline ──────────────────────────────────────────────────────────────
def _emit_bic_progress(**payload) -> None:
    """Emit one ``BIC_PROGRESS {json}`` line for the Batch Import Center's
    live progress bar (percentage + ETA).

    Mirrors ``v5_pipeline._emit_bic_progress`` EXACTLY — same ``BIC_PROGRESS ``
    prefix, same ``BIC_PROGRESS_SOURCE`` gate, same never-raise wrapper — so the
    renderer's ``_flwExtractProgress`` consumes ``question`` / ``questionTotal``
    identically whether a run took the v5 organic path or this legacy
    uworld-family path.

    THIS is the fix for the default (non-v5) Anki / OME / Divine / UWorld runs:
    they call ``process_file`` below, which previously emitted only
    human-readable ``Chunk N/M`` logs and NO per-question telemetry. The
    profile runners faithfully forward ``BIC_PROGRESS`` lines, but the legacy
    child never produced any — so the BIC bar sat at "Chunk progress not ready"
    and only the 60s "still running" heartbeat ever moved.

    No-op unless ``BIC_PROGRESS_SOURCE`` is set (electron/main.js sets it per
    BIC run; the profile runners also default it to the sourceType), so a
    standalone CLI run of this module prints nothing extra.
    """
    source = str(os.environ.get("BIC_PROGRESS_SOURCE") or "").strip()
    if not source:
        return
    try:
        print(
            "BIC_PROGRESS " + json.dumps({"source": source, **payload}, ensure_ascii=False),
            flush=True,
        )
    except Exception:
        pass


def process_file(
    filepath: Path,
    questions_per_file: int,
    dry_run: bool,
    report_data: Dict,
) -> Optional[Dict]:
    t_start = time.time()
    log(f"Processing: {filepath.name}")
    stem = filepath.stem

    # 1. Extract raw text
    raw_text = extract_text(filepath)
    if not raw_text.strip():
        warn(f"No text extracted from {filepath.name} — skipping.")
        report_data["files"][filepath.name] = {"status": "skipped", "reason": "empty_text"}
        return None

    raw_path = RAW_DIR / f"{stem}_raw.txt"
    raw_path.write_text(raw_text, encoding="utf-8")
    log(f"  Raw text saved → {raw_path.name} ({len(raw_text):,} chars)")

    # 2. Chunk
    chunks = split_into_chunks(raw_text)
    chunk_path = SEGMENT_DIR / f"{stem}_chunks.json"
    chunk_path.write_text(
        json.dumps({"sourceFile": filepath.name, "chunks": chunks}, indent=2),
        encoding="utf-8",
    )
    log(f"  {len(chunks)} chunk(s) → {chunk_path.name}")

    # v5.12.3: BIC progress denominator. The legacy chunker floors at >=1
    # question PER CHUNK (questions_per_chunk = max(1, qpf // n_chunks)), so a
    # small questions_per_file target (e.g. 15) on a large note (e.g. 944
    # chunks) actually yields ~944 questions. Using questions_per_file as the
    # bar denominator pinned it at 15/15 = 100% while generation kept going.
    # The real expected total == the sum of per-chunk requests, which works out
    # to exactly max(questions_per_file, len(chunks)). Defined at function scope
    # so the final "writing" emit (outside the live branch) can reuse it.
    expected_total = max(int(questions_per_file), len(chunks))

    file_warnings: List[str] = []
    all_questions: List[Dict] = []
    chunk_stats:   List[Dict] = []
    # Questions that failed initial validation AND repair land here in the
    # live path; routed to a review_draft.json for the BIC review modal
    # after all chunks finish. Always defined so the dry-run branch and
    # the per-file report can reference it safely (stays empty in dry-run).
    needs_review_entries: List[Dict] = []

    gen_stats: Dict = {
        "validationFailures": 0,
        "retries":            0,
        "repairsSucceeded":   0,
        "repairFailures":     0,
    }

    # 3. Generate questions
    if dry_run:
        log(f"  [DRY-RUN] Generating {questions_per_file} placeholder questions.")
        all_questions = [_placeholder_question(i + 1) for i in range(questions_per_file)]
        file_warnings.append("dry-run: questions are placeholders, not Gemini-generated")
        chunk_stats = [{"chunk": 1, "status": "dry-run", "questions": questions_per_file}]

    else:
        # Reset the v4.54 quota latch so a previous in-process run cannot leak
        # its "stopped after quota exhaustion" state into this one. In normal
        # BIC subprocess use this is a no-op (each run is a fresh process).
        reset_quota_state()
        questions_per_chunk = max(1, questions_per_file // max(len(chunks), 1))
        remainder   = questions_per_file - questions_per_chunk * len(chunks)
        q_offset    = 0
        raw_generated: List[Dict] = []
        # v5.12.2/.3: live BIC progress for the LEGACY path. expected_total is
        # the true denominator (computed above); question= cumulative produced.
        # Emitted once up front then after every chunk so the bar moves in real
        # time instead of sitting at "Chunk progress not ready" OR freezing at
        # 100% when the per-chunk floor overshoots questions_per_file.
        _emit_bic_progress(
            phase="generating",
            message=f"Starting generation: 0 of {expected_total} question(s)",
            question=0,
            questionTotal=expected_total,
            chunkTotal=len(chunks),
        )

        for ci, chunk in enumerate(chunks):
            n = questions_per_chunk + (1 if ci < remainder else 0)
            if n == 0:
                chunk_stats.append({"chunk": ci + 1, "status": "skipped", "requested": 0})
                continue

            log(f"  Chunk {ci+1}/{len(chunks)} → requesting {n} question(s) from Gemini…")
            c_stat: Dict = {"chunk": ci + 1, "requested": n}

            try:
                qs, chunk_warnings = call_gemini_with_retry(
                    chunk["chunkText"], n, ci + 1, gen_stats,
                    needs_review_collector=needs_review_entries,
                )
                qs = renumber_questions(qs, q_offset)
                raw_generated.extend(qs)
                all_questions.extend(qs)
                file_warnings.extend(chunk_warnings)
                q_offset += len(qs)
                c_stat["status"]    = "ok"
                c_stat["generated"] = len(qs)
                log(f"    ✓ {len(qs)} question(s) generated")
                time.sleep(1)  # courtesy pause between chunks

            except json.JSONDecodeError as exc:
                msg = f"Chunk {ci+1} JSON parse error: {exc}"
                warn(msg)
                file_warnings.append(msg)
                c_stat["status"] = "json_error"
                c_stat["error"]  = str(exc)

            except Exception as exc:
                msg = f"Chunk {ci+1} failed: {exc}"
                warn(msg)
                file_warnings.append(msg)
                c_stat["status"] = "error"
                c_stat["error"]  = str(exc)

            chunk_stats.append(c_stat)
            _emit_bic_progress(
                phase="generating",
                message=f"Generated {len(all_questions)} of {expected_total} question(s)",
                question=len(all_questions),
                questionTotal=expected_total,
                chunk=ci + 1,
                chunkTotal=len(chunks),
            )

        # Per-chunk shortfall recovery (v4.54) — port of the v4.49 lecture-slide
        # missing-question recovery to the UWorld family. For each chunk that
        # returned fewer questions than we asked for (a "silent under-delivery"
        # — Gemini just gave back fewer items, no error), make up to
        # MAX_RECOVERY_ATTEMPTS_PER_CHUNK focused follow-up calls asking only
        # for the missing questions. Quota-aware: bails immediately if the
        # latch is tripped. Bounded cost: at most chunks * cap extra calls.
        if not quota_exhausted():
            for ci, c_stat in enumerate(chunk_stats):
                if c_stat.get("status") != "ok":
                    continue
                requested = int(c_stat.get("requested") or 0)
                generated = int(c_stat.get("generated") or 0)
                deficit = requested - generated
                if deficit <= 0:
                    continue
                chunk = chunks[ci]
                recovered_total = 0
                for attempt in range(1, MAX_RECOVERY_ATTEMPTS_PER_CHUNK + 1):
                    if quota_exhausted():
                        msg = f"Chunk {ci+1} recovery: stopping at attempt {attempt} — Gemini quota exhausted"
                        warn(msg)
                        file_warnings.append(msg)
                        break
                    if deficit <= 0:
                        break
                    log(f"  Chunk {ci+1} recovery attempt {attempt}/{MAX_RECOVERY_ATTEMPTS_PER_CHUNK} → requesting {deficit} missing question(s)…")
                    try:
                        recovered_qs, recovery_warnings = call_gemini_with_retry(
                            chunk["chunkText"], deficit, ci + 1, gen_stats,
                            needs_review_collector=needs_review_entries,
                        )
                        file_warnings.extend(recovery_warnings)
                        recovered_qs = renumber_questions(recovered_qs, q_offset)
                        raw_generated.extend(recovered_qs)
                        all_questions.extend(recovered_qs)
                        q_offset += len(recovered_qs)
                        recovered_total += len(recovered_qs)
                        deficit -= len(recovered_qs)
                        if recovered_qs:
                            log(f"    ✓ recovered {len(recovered_qs)} question(s); {deficit} still missing")
                        else:
                            log(f"    (no questions recovered this attempt)")
                        time.sleep(1)  # courtesy pause
                    except json.JSONDecodeError as exc:
                        msg = f"Chunk {ci+1} recovery attempt {attempt} JSON parse error: {exc}"
                        warn(msg)
                        file_warnings.append(msg)
                    except Exception as exc:
                        msg = f"Chunk {ci+1} recovery attempt {attempt} failed: {exc}"
                        warn(msg)
                        file_warnings.append(msg)
                        if is_quota_failure(exc):
                            mark_quota_exhausted()
                            break
                        if is_network_failure(exc):
                            break
                if recovered_total:
                    c_stat["recovered"] = recovered_total
                    c_stat["generated"] = generated + recovered_total

        # Duplicate stem check across all collected questions
        dup_warnings = check_duplicate_stems(all_questions)
        if dup_warnings:
            file_warnings.extend(dup_warnings)
            for w in dup_warnings:
                warn(w)

        # Write raw generated JSON
        gen_path = GEN_DIR / f"{stem}_generated.json"
        gen_path.write_text(json.dumps(raw_generated, indent=2), encoding="utf-8")
        log(f"  Generated JSON → {gen_path.name} ({len(raw_generated)} questions)")

    # 4. Build and write app-ready JSON (clean + repair-succeeded questions only;
    # failed-repair questions are surfaced via the review draft below).
    _emit_bic_progress(
        phase="writing",
        message=f"Finalizing {len(all_questions)} question(s)",
        question=len(all_questions),
        questionTotal=expected_total,
    )
    app_json = build_app_ready_json(stem, all_questions, file_warnings)
    app_path = APP_DIR / f"{stem}_app_ready.json"
    app_path.write_text(json.dumps(app_json, indent=2), encoding="utf-8")
    log(f"  App-ready JSON → {app_path.name} ({len(all_questions)} questions)")

    # 4b. Write the review draft when any question failed initial + repair.
    # needs_review_entries is initialized to [] at the top of process_file
    # and only grows in the live path; dry_run keeps it empty, so this is a
    # no-op for dry-run. The draft lands under the BIC job's review/
    # directory (or BASE_DIR/review for standalone CLI), where the BIC
    # runner's discover_review_draft() picks it up automatically.
    review_draft_path: Optional[Path] = None
    if needs_review_entries:
        review_draft_path = write_uworld_family_review_draft(
            source_label=filepath.name,
            source_type=str(os.environ.get("BIC_PROGRESS_SOURCE") or "uworld_family").strip() or "uworld_family",
            source_format=str(app_json.get("sourceFormat") or "uworld-notes"),
            needs_review_entries=needs_review_entries,
        )

    elapsed = round(time.time() - t_start, 1)
    report_data["files"][filepath.name] = {
        "status":             "ok",
        "rawChars":           len(raw_text),
        "chunksProcessed":    len(chunks),
        "questionsGenerated": len(all_questions),
        "needsReviewCount":   len(needs_review_entries),
        "reviewDraftPath":    str(review_draft_path.resolve()) if review_draft_path else "",
        "validationFailures": gen_stats["validationFailures"],
        "retries":            gen_stats["retries"],
        "repairsSucceeded":   gen_stats["repairsSucceeded"],
        "repairFailures":     gen_stats["repairFailures"],
        "validationWarnings": [
            w for w in file_warnings
            if any(kw in w.lower() for kw in ("invalid", "failed", "duplicate", "repair", "forbidden"))
        ],
        "warnings":     file_warnings,
        "chunkStats":   chunk_stats,
        "outputPaths": {
            "appReady":  str(app_path),
            "generated": str(GEN_DIR / f"{stem}_generated.json") if not dry_run else None,
            "chunks":    str(chunk_path),
            "rawText":   str(raw_path),
        },
        "elapsedSeconds": elapsed,
        "dryRun":         dry_run,
    }
    return app_json


def discover_input_files() -> List[Path]:
    if not INPUT_DIR.exists():
        return []
    return sorted(
        f for f in INPUT_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def _resolve_selected_input(raw_path: str) -> Path:
    selected = Path(raw_path).expanduser()
    if not selected.is_absolute():
        selected = (Path.cwd() / selected).resolve()
    else:
        selected = selected.resolve()
    if not selected.exists():
        raise ValueError(f"--input-file does not exist: {selected}")
    if not selected.is_file():
        raise ValueError(f"--input-file must be a file: {selected}")
    if selected.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"--input-file has unsupported extension '{selected.suffix}'. Supported: {supported}")
    return selected


def _apply_output_dir(raw_path: str) -> Path:
    """Repoint per-module path globals at an external output root.

    v4.59: BIC's profile runner passes --output-dir <jobRoot>/uworld so all
    raw_text/, chunks/, generated/, app_ready/, and reports/ artifacts land
    inside the BIC job dir rather than the script's source tree (which lives
    inside a read-only .app bundle when run from the packaged app).
    """
    global RAW_DIR, SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR
    output_root = Path(raw_path).expanduser()
    if not output_root.is_absolute():
        output_root = (Path.cwd() / output_root).resolve()
    else:
        output_root = output_root.resolve()
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"--output-dir must be a directory path: {output_root}")
    RAW_DIR = output_root / "raw_text"
    SEGMENT_DIR = output_root / "chunks"
    GEN_DIR = output_root / "generated"
    DEBUG_DIR = output_root / "generated" / "debug"
    APP_DIR = output_root / "app_ready"
    REPORT_DIR = output_root / "reports"
    return output_root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="UWorld Notes → Step 2 Question Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python3 generate_uworld_questions.py --dry-run
              python3 generate_uworld_questions.py --generate
              python3 generate_uworld_questions.py --generate --questions-per-file 15
              python3 generate_uworld_questions.py --input-file input_notes/topic.md --dry-run
              python3 generate_uworld_questions.py --input-file input_notes/topic.md --generate --output-dir /tmp/uworld-output
        """),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Gemini calls; produce placeholder app-ready JSON only.",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help=(
            "Explicitly run live Gemini generation. "
            "Exits with error if GEMINI_API_KEY is unset (does not silently fall back)."
        ),
    )
    parser.add_argument(
        "--questions-per-file",
        type=int,
        default=15,
        metavar="N",
        help="Target number of questions to generate per input file (default: 15).",
    )
    parser.add_argument(
        "--input-file",
        default="",
        help="Process one selected UWorld notes file instead of scanning input_notes/.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional output root. Writes raw_text/, chunks/, generated/, app_ready/, and reports/ under this directory.",
    )
    # v5.8: Advanced Mode (v5.2 multi-stage organic pipeline). Optional
    # import — uworld is the base every other source extends, so we
    # only attach the v5 CLI flags when the shared adapter is actually
    # importable. This keeps generate_uworld_questions.py runnable in
    # isolation (e.g., a stripped checkout that doesn't ship
    # shared-ingestion/).
    _v5_shared = None
    try:
        _shared_dir = Path(__file__).parent.parent / "shared-ingestion"
        if _shared_dir.is_dir() and str(_shared_dir) not in sys.path:
            sys.path.insert(0, str(_shared_dir))
        import v5_uworld_family_adapter as _v5_shared  # type: ignore
        _v5_shared.add_v5_cli_args(parser)
    except ImportError:
        _v5_shared = None
    args = parser.parse_args()

    try:
        selected_input = _resolve_selected_input(args.input_file) if args.input_file else None
        _apply_output_dir(args.output_dir) if args.output_dir else None
    except ValueError as exc:
        parser.error(str(exc))

    if args.dry_run and args.generate:
        parser.error("--dry-run and --generate are mutually exclusive.")

    log("=" * 60)
    log("UWorld Notes → Question Generator")
    log(f"  Model:              {GEMINI_MODEL}")
    log(f"  Dry-run:            {args.dry_run}")
    log(f"  Generate:           {args.generate}")
    log(f"  Questions per file: {args.questions_per_file}")
    log("=" * 60)

    for d in (RAW_DIR, SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    files = [selected_input] if selected_input else discover_input_files()
    if not files:
        log("No supported input files found in input_notes/")
        log(f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        write_report({"status": "no_input_files", "files": {}})
        return

    log(f"Found {len(files)} input file(s): {[f.name for f in files]}")

    # ── Resolve generation mode ────────────────────────────────────────────────
    dry_run = args.dry_run

    if args.generate:
        # Hard fail if key is absent — do not silently degrade
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            log("ERROR: --generate requires GEMINI_API_KEY to be set.")
            log("Set it with: export GEMINI_API_KEY=your_key_here")
            sys.exit(1)
        dry_run = False

    elif not dry_run:
        # Auto mode: generate if key available, fall back gracefully otherwise
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            warn("GEMINI_API_KEY is not set — falling back to --dry-run mode.")
            warn("Pass --generate to treat a missing key as a hard error.")
            dry_run = True

    # v5.8: resolve v5 config when --v5 was passed at parse time.
    v5_cfg: Optional[Dict[str, Any]] = None
    if _v5_shared is not None and getattr(args, "v5", False):
        if dry_run:
            warn("--v5 has no effect in dry-run mode; ignoring v5 flag.")
        else:
            try:
                v5_cfg = _v5_shared.resolve_v5_cfg(args)
                log(
                    "v5 pipeline configured — order_mix=%s difficulty_mix=%s seed=%d"
                    % (args.v5_order_mix, args.v5_difficulty_mix, args.v5_seed)
                )
            except ValueError as exc:
                warn(f"Invalid v5 mix arguments: {exc}; --v5 disabled.")
                v5_cfg = None

    report_data: Dict = {
        "runTimestamp":     datetime.now().isoformat(),
        "model":            GEMINI_MODEL,
        "dryRun":           dry_run,
        "questionsPerFile": args.questions_per_file,
        "inputFiles":       [f.name for f in files],
        "files":            {},
    }
    t_total = time.time()

    for filepath in files:
        try:
            used_v5 = False
            if v5_cfg is not None:
                try:
                    _v5_shared.process_file_v5_uworld_family(
                        uw_module=sys.modules[__name__],
                        filepath=filepath,
                        questions_per_file=args.questions_per_file,
                        v5_cfg=v5_cfg,
                        report_data=report_data,
                        pipeline_label="v5.2-organic",
                        chunk_size=int(args.chunk_size),
                        questions_per_chunk=int(args.questions_per_chunk),
                    )
                    used_v5 = True
                except Exception as v5_exc:
                    warn(
                        f"v5 pipeline failed for {filepath.name} ({v5_exc}); "
                        f"falling back to legacy single-call generator."
                    )
            if not used_v5:
                process_file(filepath, args.questions_per_file, dry_run, report_data)
        except Exception as exc:
            warn(f"Fatal error processing {filepath.name}: {exc}")
            report_data["files"][filepath.name] = {"status": "error", "error": str(exc)}

    report_data["totalElapsedSeconds"] = round(time.time() - t_total, 1)
    write_report(report_data)
    log("Done.")


if __name__ == "__main__":
    main()
