#!/usr/bin/env python3
"""
Mehlman Medical Mastery PDF → Step 2 Question Generator
Milestone 1: Deterministic extraction + chunking + dry-run output
Live Gemini generation: Milestone 2 (infrastructure wired, not yet validated)

Usage:
  python3 generate_mehlman_questions.py --dry-run
  python3 generate_mehlman_questions.py --extract-only
  python3 generate_mehlman_questions.py --extract-only --extract-assets
  python3 generate_mehlman_questions.py --chunk-only
  python3 generate_mehlman_questions.py --dry-run --extract-assets
  python3 generate_mehlman_questions.py --dry-run --max-chunks 2
  python3 generate_mehlman_questions.py --generate                       # 1 q / 1.5K chunk
  python3 generate_mehlman_questions.py --generate --questions-per-chunk 3
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Infrastructure reuse ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent / "uworld-notes-question-generator"))
import generate_uworld_questions as _uw

# ── v5.8: shared Advanced Mode (v5.2 multi-stage organic) adapter ─────────────
_SHARED_DIR = Path(__file__).parent.parent / "shared-ingestion"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
from v5_uworld_family_adapter import (  # noqa: E402
    add_v5_cli_args,
    process_file_v5_uworld_family,
    resolve_v5_cfg,
)
from typing import Any  # noqa: E402  Optional already imported above

# ── Optional imports (graceful degradation) ───────────────────────────────────
try:
    import pdfplumber as _pdfplumber
    _PDFPLUMBER = True
except ImportError:
    _PDFPLUMBER = False

try:
    import fitz as _fitz
    _FITZ = True
except ImportError:
    _FITZ = False

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE       = Path(__file__).parent
INPUT_DIR   = _BASE / "input_pdfs"
TEXT_DIR    = _BASE / "extracted_text"
FIG_DIR     = _BASE / "extracted_figures"
TABLE_DIR   = _BASE / "extracted_tables"
SEGMENT_DIR   = _BASE / "output_json" / "chunks"
GEN_DIR     = _BASE / "output_json" / "generated"
DEBUG_DIR   = _BASE / "output_json" / "generated" / "debug"
APP_DIR     = _BASE / "output_json" / "app_ready"
REPORT_DIR  = _BASE / "reports"
PROMPTS_DIR = _BASE / "prompts"

# ── Patch UWorld module globals (resolved at call time) ───────────────────────
_uw.DEBUG_DIR   = DEBUG_DIR
_uw.PROMPT_FILE = PROMPTS_DIR / "mehlman_pdf_to_questions_prompt.txt"
_uw.REPORT_DIR  = REPORT_DIR

# ── sourceFormat patch ────────────────────────────────────────────────────────
_orig_build_app_ready = _uw.build_app_ready_json

def _mehlman_build_app_ready_json(
    source_stem: str,
    questions: List[Dict],
    warnings: List[str],
) -> Dict:
    result = _orig_build_app_ready(source_stem, questions, warnings)
    result["sourceFormat"] = "mehlman-pdf"
    return result

_uw.build_app_ready_json = _mehlman_build_app_ready_json

# ── Constants ─────────────────────────────────────────────────────────────────
# v4.58: tight-focus chunking targets ~1.5K chars / chunk so each chunk maps
# to a single discrete Mehlman fact. Paired with --questions-per-chunk 1,
# this gives ~1 grounded NBME-style question per ~1-2 paragraph fact slice.
_MIN_CHUNK = 1_200
_MAX_CHUNK = 1_800
_MIN_FIG_PX   = 80
_MAX_ASPECT   = 8.0
_HIGH_CONF_PX = 200


def _apply_output_dir(raw_path: str) -> Path:
    global TEXT_DIR, FIG_DIR, TABLE_DIR, SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR

    output_root = Path(raw_path).expanduser()
    if not output_root.is_absolute():
        output_root = (Path.cwd() / output_root).resolve()
    else:
        output_root = output_root.resolve()
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"--output-dir must be a directory path: {output_root}")
    TEXT_DIR = output_root / "extracted_text"
    FIG_DIR = output_root / "extracted_figures"
    TABLE_DIR = output_root / "extracted_tables"
    SEGMENT_DIR = output_root / "output_json" / "chunks"
    GEN_DIR = output_root / "output_json" / "generated"
    DEBUG_DIR = output_root / "output_json" / "generated" / "debug"
    APP_DIR = output_root / "output_json" / "app_ready"
    REPORT_DIR = output_root / "reports"
    _uw.DEBUG_DIR = DEBUG_DIR
    _uw.REPORT_DIR = REPORT_DIR
    return output_root


# ── Asset helpers ─────────────────────────────────────────────────────────────

def _figure_disposition(w: int, h: int) -> str:
    """Returns 'ignore', 'low', or 'high' confidence for a figure."""
    if w < _MIN_FIG_PX or h < _MIN_FIG_PX:
        return "ignore"
    aspect = max(w, h) / max(min(w, h), 1)
    if aspect > _MAX_ASPECT:
        return "ignore"
    if w >= _HIGH_CONF_PX and h >= _HIGH_CONF_PX:
        return "high"
    return "low"


def _table_to_markdown(table: List[List]) -> str:
    if not table:
        return ""
    rows = []
    for i, row in enumerate(table):
        cells = [str(c or "").replace("\n", " ").strip() for c in row]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(rows)


# ── Per-page extraction ───────────────────────────────────────────────────────

def _crop_body(page) -> object:
    """Crop pdfplumber page to body zone, stripping top 8% and bottom 8%."""
    h = float(page.height)
    w = float(page.width)
    return page.crop((0, h * 0.08, w, h * 0.92))


def extract_pdf_pages(
    filepath: Path,
    extract_assets: bool,
    stats: Dict,
    max_pages: Optional[int] = None,
) -> List[Dict]:
    """
    Extract text per page with pdfplumber (body crop strips header/footer zones).
    Optionally extract figures via PyMuPDF and tables via pdfplumber.

    Returns list of page dicts: {pageNum, text, figures, tables, warnings}
    """
    if not _PDFPLUMBER:
        raise ImportError("pdfplumber is required: pip install pdfplumber")

    pages_data: List[Dict] = []
    seen_fig_hashes: set = set()

    fitz_doc = None
    if extract_assets:
        if _FITZ:
            fitz_doc = _fitz.open(str(filepath))
        else:
            _uw.warn("PyMuPDF not installed — figure extraction skipped. pip install pymupdf")

    with _pdfplumber.open(str(filepath)) as pdf:
        total_pages = len(pdf.pages)
        pages_to_process = pdf.pages[:max_pages] if max_pages else pdf.pages
        stats["totalSourcePages"] = total_pages
        stats["totalPages"] = len(pages_to_process)
        if max_pages:
            _uw.log(f"    {total_pages} pages detected; processing first {len(pages_to_process)} page(s)")
        else:
            _uw.log(f"    {total_pages} pages detected")

        for i, page in enumerate(pages_to_process):
            page_num = i + 1
            pg: Dict = {
                "pageNum": page_num,
                "text": "",
                "figures": [],
                "tables": [],
                "warnings": [],
            }

            # Text: body-crop to strip header/footer zones
            body = _crop_body(page)
            pg["text"] = body.extract_text(x_tolerance=2, y_tolerance=2) or ""

            if extract_assets:
                # ── Table extraction (lattice/lines only — avoids false positives) ──
                try:
                    tbls = body.extract_tables(
                        {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
                    )
                    for ti, tbl in enumerate(tbls or []):
                        if not tbl or len(tbl) < 2:
                            continue
                        md = _table_to_markdown(tbl)
                        nrows = len(tbl)
                        ncols = max(len(r) for r in tbl) if tbl else 0
                        tbl_name = f"{filepath.stem}_p{page_num}_t{ti + 1}.json"
                        tbl_path = TABLE_DIR / tbl_name
                        tbl_path.write_text(
                            json.dumps({"rows": tbl}, indent=2), encoding="utf-8"
                        )
                        pg["tables"].append(
                            {
                                "filename": tbl_name,
                                "rows": nrows,
                                "cols": ncols,
                                "markdown": md,
                            }
                        )
                        stats["tablesDetected"] = stats.get("tablesDetected", 0) + 1
                        stats["tablesExtracted"] = stats.get("tablesExtracted", 0) + 1
                except Exception as exc:
                    pg["warnings"].append(f"table p{page_num}: {exc}")

                # ── Figure extraction via PyMuPDF ─────────────────────────────
                if fitz_doc is not None:
                    try:
                        fitz_page = fitz_doc[i]
                        for img_info in fitz_page.get_images(full=True):
                            xref = img_info[0]
                            pix = _fitz.Pixmap(fitz_doc, xref)
                            w, h = pix.width, pix.height
                            disp = _figure_disposition(w, h)
                            stats["figuresDetected"] = stats.get("figuresDetected", 0) + 1
                            if disp == "ignore":
                                stats["figuresIgnored"] = stats.get("figuresIgnored", 0) + 1
                                pix = None
                                continue
                            if pix.n - pix.alpha > 3:
                                pix = _fitz.Pixmap(_fitz.csRGB, pix)
                            img_bytes = pix.tobytes("png")
                            img_hash = hashlib.md5(img_bytes).hexdigest()[:8]
                            if img_hash in seen_fig_hashes:
                                stats["figuresIgnored"] = (
                                    stats.get("figuresIgnored", 0) + 1
                                )
                                pix = None
                                continue
                            seen_fig_hashes.add(img_hash)
                            fig_name = f"{filepath.stem}_p{page_num}_{img_hash}.png"
                            (FIG_DIR / fig_name).write_bytes(img_bytes)
                            pg["figures"].append(
                                {
                                    "filename": fig_name,
                                    "width": w,
                                    "height": h,
                                    "confidence": disp,
                                }
                            )
                            stats["figuresKept"] = stats.get("figuresKept", 0) + 1
                            pix = None
                    except Exception as exc:
                        pg["warnings"].append(f"figure p{page_num}: {exc}")

            pages_data.append(pg)

    if fitz_doc is not None:
        fitz_doc.close()

    return pages_data


# ── Long-PDF chunking ─────────────────────────────────────────────────────────

def split_pages_into_chunks(
    pages_data: List[Dict],
    min_chars: int = _MIN_CHUNK,
    max_chars: int = _MAX_CHUNK,
) -> List[Dict]:
    """
    Merge per-page text into chunks of min_chars–max_chars.
    Splits at page boundaries when possible; falls back to paragraph splits
    when a single page exceeds max_chars. Preserves pageStart/pageEnd metadata.
    """
    chunks: List[Dict] = []
    chunk_id = 0

    buf_text  = ""
    buf_start: Optional[int] = None
    buf_end:   Optional[int] = None
    buf_figs:  List[Dict] = []
    buf_tbls:  List[Dict] = []
    buf_warns: List[str]  = []

    def _emit() -> None:
        nonlocal buf_text, buf_start, buf_end, buf_figs, buf_tbls, buf_warns, chunk_id
        if not buf_text.strip():
            return
        chunk_id += 1
        chunks.append(
            {
                "chunkId":    chunk_id,
                "sourceFile": "",   # filled by caller
                "pageStart":  buf_start,
                "pageEnd":    buf_end,
                "charCount":  len(buf_text),
                "text":       buf_text,
                "figures":    list(buf_figs),
                "tables":     list(buf_tbls),
                "warnings":   list(buf_warns),
            }
        )
        buf_text  = ""
        buf_start = None
        buf_end   = None
        buf_figs  = []
        buf_tbls  = []
        buf_warns = []

    def _emit_para_chunk(text: str, pn: int, figs, tbls, warns) -> None:
        """Emit a single paragraph-split chunk (single-page overflow)."""
        nonlocal chunk_id
        chunk_id += 1
        chunks.append(
            {
                "chunkId":    chunk_id,
                "sourceFile": "",
                "pageStart":  pn,
                "pageEnd":    pn,
                "charCount":  len(text),
                "text":       text,
                "figures":    list(figs),
                "tables":     list(tbls),
                "warnings":   list(warns),
            }
        )

    for pg in pages_data:
        pn   = pg["pageNum"]
        pt   = pg["text"].strip()
        figs = pg.get("figures", [])
        tbls = pg.get("tables", [])
        warns = pg.get("warnings", [])

        if not pt:
            continue

        if buf_start is None:
            buf_start = pn

        candidate = (buf_text + "\n\n" + pt).strip() if buf_text else pt

        if len(candidate) > max_chars and buf_text:
            # Current buffer full — emit before adding this page
            _emit()
            buf_text  = pt
            buf_start = pn
            buf_end   = pn
            buf_figs  = list(figs)
            buf_tbls  = list(tbls)
            buf_warns = list(warns)
        else:
            buf_text = candidate
            buf_end  = pn
            buf_figs.extend(figs)
            buf_tbls.extend(tbls)
            buf_warns.extend(warns)

        # Single-page text exceeds max — split at paragraphs, then sentences.
        # v4.58: PDF extraction often loses paragraph breaks on Mehlman pages,
        # so when a "paragraph" still exceeds max_chars, fall back to sentence
        # splits to actually hit the 1.5K tight-focus target.
        if len(buf_text) > max_chars and buf_start == buf_end == pn:
            paras = [p.strip() for p in re.split(r"\n{2,}", buf_text) if p.strip()]
            units: List[str] = []
            for para in paras:
                if len(para) <= max_chars:
                    units.append(para)
                else:
                    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", para)
                    cur = ""
                    for sent in sentences:
                        if not sent.strip():
                            continue
                        if len(cur) + len(sent) + 1 <= max_chars:
                            cur = (cur + " " + sent).strip() if cur else sent
                        else:
                            if cur:
                                units.append(cur)
                            cur = sent
                    if cur:
                        units.append(cur)
            sub_buf  = ""
            sub_figs: List[Dict] = list(figs)
            sub_tbls: List[Dict] = list(tbls)
            for unit in units:
                if len(sub_buf) + len(unit) + 2 <= max_chars:
                    sub_buf = (sub_buf + "\n\n" + unit).strip() if sub_buf else unit
                else:
                    if sub_buf:
                        _emit_para_chunk(sub_buf, pn, sub_figs, sub_tbls, warns)
                        sub_figs = []
                        sub_tbls = []
                    sub_buf = unit
            buf_text  = sub_buf
            buf_start = pn
            buf_end   = pn
            buf_figs  = sub_figs
            buf_tbls  = sub_tbls

    _emit()
    return chunks


# ── Deterministic page-proximity image attachment (v4.58) ─────────────────────
# Each chunk already carries the figures detected on the pages it covers.
# With 1.5K-char chunks + 1 question/chunk, every chunk maps to one question,
# so attaching the chunk's figures to that question is deterministic page
# proximity by construction (figure on page N → question whose chunk covers
# page N). When a chunk produces more than one question (questions_per_chunk>1
# or Gemini over-delivers), all figures attach to the first question so each
# figure is referenced exactly once.

def _mime_for(path: Path) -> str:
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if path.suffix.lower() == ".png":
        return "image/png"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{_mime_for(path)};base64,{encoded}"


# ── Relevance gate (v4.59): vision check before attaching a page figure ───────
# The page-proximity attachment below staples a page's figure to a question
# purely because they share a PDF page. Mehlman pages pack several distinct
# topics, so the lone figure may illustrate a DIFFERENT topic than the question
# generated from that page (observed: a PDA O2-saturation diagram landing on a
# tricuspid-regurgitation question — same page 7, different topic). This gate
# actually LOOKS at the image with a multimodal call and keeps it ONLY when it
# supports THIS question. Scope: Normal mode only — Advanced/v5 never calls the
# stapler (it does its own description-matched image sourcing). Fail-safe: any
# inability to obtain a clear "relevant" verdict drops the figure, because a
# wrong image is worse for learning than no image.

_IMAGE_GATE_ENV = "MEHLMAN_IMAGE_RELEVANCE_GATE"
_image_gate_cache: Dict[str, Dict[str, Any]] = {}


def _image_relevance_gate_enabled() -> bool:
    """Gate is ON by default; set MEHLMAN_IMAGE_RELEVANCE_GATE=0 to disable."""
    return (os.environ.get(_IMAGE_GATE_ENV, "1").strip() or "1").lower() not in (
        "0", "false", "no", "off",
    )


def _figure_relevance_verdict(image_bytes: bytes, mime: str, question: Dict) -> Dict[str, Any]:
    """Decide whether ``image_bytes`` belongs in ``question``'s explanation.

    Returns {'keep': bool, 'depicts': str, 'reason': str, 'status': str} where
    status is one of 'relevant' | 'irrelevant' | 'gate_error'. Fail-safe: every
    non-affirmative outcome (call error, empty/garbled response, parse failure)
    yields keep=False so an UNVERIFIED image is never attached. Verdicts are
    cached by (image bytes, question signature) for determinism within a run.
    """
    stem = str(question.get("stem") or "").strip()
    correct = str(question.get("correctAnswer") or "").strip()
    correct_text = ""
    for ch in (question.get("answerChoices") or []):
        if isinstance(ch, dict) and str(ch.get("label") or "").strip() == correct:
            correct_text = str(ch.get("text") or "").strip()
            break
    explanation = str(question.get("correctExplanation") or "").strip()
    concept = str(
        question.get("testedConcept")
        or question.get("educationalObjective")
        or question.get("retrievalTag")
        or ""
    ).strip()

    sig = hashlib.md5(
        (hashlib.md5(image_bytes).hexdigest() + "|" + stem[:160] + "|" + concept[:120]).encode("utf-8")
    ).hexdigest()
    if sig in _image_gate_cache:
        return _image_gate_cache[sig]

    prompt = (
        "You are a medical-education quality reviewer. A figure was AUTOMATICALLY "
        "extracted from the same PDF page as the source text for a board-style "
        "question. Same-page is NOT proof the figure belongs to THIS question — "
        "these source pages pack several unrelated topics onto one page.\n\n"
        "Decide whether the attached figure should appear in THIS question's "
        "answer explanation.\n\n"
        f"QUESTION STEM:\n{stem}\n\n"
        f"CORRECT ANSWER: {correct} — {correct_text}\n\n"
        f"ANSWER EXPLANATION:\n{explanation}\n\n"
        f"TESTED CONCEPT: {concept}\n\n"
        "Look at the image. Keep it (relevant=true) ONLY if it directly "
        "illustrates, depicts, or supports the specific concept this question "
        "tests (the diagnosis, the mechanism, or an image the explanation refers "
        "to). Reject (relevant=false) if it is about a DIFFERENT topic — even a "
        "related one (a different cardiac lesion, a different organ, an unrelated "
        "diagram). When unsure, reject.\n\n"
        "Respond with STRICT JSON only, no markdown fences:\n"
        '{"depicts": "<short phrase: what the image actually shows>", '
        '"relevant": true, "reason": "<one sentence>"}'
    )

    def _cache(verdict: Dict[str, Any]) -> Dict[str, Any]:
        _image_gate_cache[sig] = verdict
        return verdict

    try:
        client = _uw._gemini_client()
        t = _uw._genai_types
        response = client.models.generate_content(
            model=_uw.GEMINI_MODEL,
            contents=[prompt, t.Part.from_bytes(data=image_bytes, mime_type=mime)],
            config=t.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=2048,
                response_mime_type="application/json",
                # thinking_budget=0: this is a perception+matching task, not deep
                # reasoning. Keeping it at 0 also avoids the 2.5-flash empty-text
                # trap where thinking tokens consume the output budget.
                thinking_config=t.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as exc:  # network, quota, SDK, auth — never raise into attach
        return _cache({"keep": False, "depicts": "", "status": "gate_error",
                       "reason": f"vision gate call failed: {exc}"[:200]})

    raw = (getattr(response, "text", None) or "").strip()
    if not raw:
        return _cache({"keep": False, "depicts": "", "status": "gate_error",
                       "reason": "empty vision response"})

    parsed: Optional[Dict] = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        mobj = re.search(r"\{.*\}", raw, re.DOTALL)
        if mobj:
            try:
                parsed = json.loads(mobj.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict) or "relevant" not in parsed:
        return _cache({"keep": False, "depicts": "", "status": "gate_error",
                       "reason": "vision verdict parse failed"})

    keep = bool(parsed.get("relevant"))
    return _cache({
        "keep": keep,
        "depicts": str(parsed.get("depicts") or "")[:160],
        "reason": str(parsed.get("reason") or "")[:240],
        "status": "relevant" if keep else "irrelevant",
    })


def _attach_chunk_figures_to_questions(
    questions: List[Dict],
    chunk: Dict,
    source_stem: str,
    *,
    enable_gate: bool = False,
) -> Dict[str, Any]:
    """Attach the chunk's page figures to the first question in-place.

    Deterministic page-proximity: chunk knows pageStart-pageEnd; figures in
    `chunk['figures']` were extracted from those pages. When ``enable_gate`` is
    set (Normal-mode live generation), each figure first passes a multimodal
    relevance check against the target question — only figures that actually
    illustrate the question are attached; topical mismatches are dropped. With
    the gate off (dry-run), every figure attaches as before.

    Returns {'attached': int, 'suppressed': int, 'suppressions': [...]}.
    """
    summary: Dict[str, Any] = {"attached": 0, "suppressed": 0, "suppressions": []}
    figs = chunk.get("figures") or []
    if not figs or not questions:
        return summary

    target = questions[0]
    target_q_num = int(target.get("questionNumber") or 0) or 1
    explanation_images = target.setdefault("explanationImages", [])
    figure_refs = target.setdefault("figureRefs", [])
    gate_on = bool(enable_gate) and _image_relevance_gate_enabled()

    attach_warnings: List[str] = []
    for i, fig in enumerate(figs, start=1):
        fig_name = fig.get("filename") or ""
        fig_path = FIG_DIR / fig_name
        if not fig_name or not fig_path.exists():
            continue
        page_num = chunk.get("pageStart") or 0
        figure_id = (
            f"mehlman_q{target_q_num:03d}_p{page_num:03d}_{i:02d}_{fig_name[-12:-4]}"
        )
        try:
            image_bytes = fig_path.read_bytes()
        except Exception as exc:
            attach_warnings.append(
                f"figure read failed (chunk {chunk.get('chunkId')}, {fig_name}): {exc}"
            )
            continue
        mime = _mime_for(fig_path)

        # Relevance gate (Normal mode): keep only figures that actually
        # illustrate THIS question; drop topical mismatches. Fail-safe = drop.
        if gate_on:
            verdict = _figure_relevance_verdict(image_bytes, mime, target)
            if not verdict.get("keep"):
                summary["suppressed"] += 1
                summary["suppressions"].append({
                    "figure":   fig_name,
                    "figureId": figure_id,
                    "status":   verdict.get("status"),
                    "depicts":  verdict.get("depicts"),
                    "reason":   verdict.get("reason"),
                })
                attach_warnings.append(
                    f"figure suppressed by relevance gate ({fig_name}; "
                    f"{verdict.get('status')}): depicts '{verdict.get('depicts')}' "
                    f"— {verdict.get('reason')}"
                )
                continue

        try:
            # asset_path is informational only — the binary lives in dataUrl.
            # v4.58 fix: BIC runs with --output-dir write figures under the
            # job root, which is NOT under _BASE, so a naive relative_to
            # raised ValueError and killed every chunk that had figures.
            try:
                asset_path = str(fig_path.relative_to(_BASE))
            except ValueError:
                asset_path = f"extracted_figures/{fig_name}"
            data_url_str = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        except Exception as exc:
            attach_warnings.append(
                f"figure attach failed (chunk {chunk.get('chunkId')}, {fig_name}): {exc}"
            )
            continue
        entry = {
            "figureId":         figure_id,
            "figureKey":        None,
            "dataUrl":          data_url_str,
            "isLabTable":       False,
            "kind":             "figure",
            "source":           "mehlman-pdf-generator",
            "originalFileName": fig_name,
            "assetPath":        asset_path,
            "placement":        "explanation",
            "pageNum":          page_num,
            "confidence":       fig.get("confidence", "low"),
        }
        explanation_images.append(entry)
        figure_refs.append({
            "id":          figure_id,
            "placeholder": f"[FIGURE: {figure_id}]",
            "location":    "explanation",
            "visibleText": [],
        })
        summary["attached"] += 1

    if explanation_images:
        target["hasEmbeddedFigure"] = True
    if attach_warnings:
        target.setdefault("extractionWarnings", []).extend(attach_warnings)
    return summary


# ── Asset markers ─────────────────────────────────────────────────────────────

def _inject_asset_markers(chunk: Dict) -> str:
    """Prepend [DETECTED FIGURE/TABLE] markers to chunk text for Gemini prompts."""
    markers: List[str] = []
    for fig in chunk.get("figures", []):
        markers.append(
            f"[DETECTED FIGURE: {fig['filename']} | "
            f"{fig['width']}×{fig['height']} px | confidence: {fig['confidence']}]"
        )
    for tbl in chunk.get("tables", []):
        markers.append(
            f"[DETECTED TABLE: {tbl['filename']} | "
            f"{tbl['rows']} rows × {tbl['cols']} cols]"
        )
        markers.append(tbl.get("markdown", ""))

    if not markers:
        return chunk["text"]
    return "\n".join(markers) + "\n\n" + chunk["text"]


# ── Stats skeleton ────────────────────────────────────────────────────────────

def _empty_stats() -> Dict:
    return {
        "totalPages":         0,
        "totalTextChars":     0,
        "chunksCreated":      0,
        "chunksProcessed":    0,
        "figuresDetected":    0,
        "figuresKept":        0,
        "figuresIgnored":     0,
        "tablesDetected":     0,
        "tablesExtracted":    0,
        "questionsGenerated": 0,
        "figuresAttached":    0,
        "validationFailures": 0,
        "repairsSucceeded":   0,
        "repairFailures":     0,
        "warnings":           [],
        "chunkStats":         [],
    }


# ── Core processor ────────────────────────────────────────────────────────────

def process_pdf(
    filepath: Path,
    *,
    mode: str,                     # "extract_only" | "chunk_only" | "dry_run" | "generate"
    extract_assets: bool,
    max_pages: Optional[int],
    max_chunks: Optional[int],
    questions_per_chunk: int,
    resume: bool,
    report_data: Dict,
) -> None:
    t_start = time.time()
    stem    = filepath.stem
    artifact_stem = f"{stem}_p001_p{max_pages:03d}" if max_pages else stem
    _uw.log(f"Processing: {filepath.name}  [mode={mode}]")
    stats   = _empty_stats()

    text_path  = TEXT_DIR  / f"{artifact_stem}_text.json"
    chunk_path = SEGMENT_DIR / f"{artifact_stem}_chunks.json"

    # ── Stage 1: Per-page text (and asset) extraction ─────────────────────────
    if mode == "chunk_only":
        if not text_path.exists():
            _uw.warn(
                f"  {text_path.name} not found — run --extract-only first. Skipping."
            )
            report_data["files"][filepath.name] = {
                "status": "skipped",
                "reason": "missing_extracted_text",
            }
            return
        _uw.log(f"  Stage 1: Loading existing text → {text_path.name}")
        pages_data = json.loads(text_path.read_text(encoding="utf-8"))
        stats["totalPages"] = len(pages_data)
    else:
        if resume and text_path.exists():
            _uw.log(f"  Stage 1: [RESUME] Reusing {text_path.name}")
            pages_data = json.loads(text_path.read_text(encoding="utf-8"))
            stats["totalPages"] = len(pages_data)
        else:
            _uw.log(f"  Stage 1: Extracting pages from {filepath.name}…")
            pages_data = extract_pdf_pages(filepath, extract_assets, stats, max_pages=max_pages)
            text_path.write_text(
                json.dumps(pages_data, indent=2), encoding="utf-8"
            )
            _uw.log(f"    → {text_path.name} ({stats['totalPages']} pages)")

    total_chars = sum(len(pg["text"]) for pg in pages_data)
    stats["totalTextChars"] = total_chars
    _uw.log(f"    Total body chars: {total_chars:,}")

    if total_chars < 100:
        msg = "very_low_text: <100 chars extracted — PDF may be image-only or encrypted"
        _uw.warn(f"  {msg}")
        stats["warnings"].append(msg)

    if mode == "extract_only":
        elapsed = round(time.time() - t_start, 1)
        report_data["files"][filepath.name] = {
            "status":          "extracted",
            "totalPages":      stats["totalPages"],
            "totalTextChars":  total_chars,
            "figuresDetected": stats.get("figuresDetected", 0),
            "figuresKept":     stats.get("figuresKept", 0),
            "figuresIgnored":  stats.get("figuresIgnored", 0),
            "tablesDetected":  stats.get("tablesDetected", 0),
            "tablesExtracted": stats.get("tablesExtracted", 0),
            "warnings":        stats["warnings"],
            "elapsedSeconds":  elapsed,
            "outputPaths":     {"extractedText": str(text_path)},
        }
        return

    # ── Stage 2: Chunking ─────────────────────────────────────────────────────
    if resume and chunk_path.exists():
        _uw.log(f"  Stage 2: [RESUME] Reusing {chunk_path.name}")
        manifest   = json.loads(chunk_path.read_text(encoding="utf-8"))
        all_chunks = manifest["chunks"]
        stats["chunksCreated"] = len(all_chunks)
    else:
        _uw.log(f"  Stage 2: Chunking ({_MIN_CHUNK:,}–{_MAX_CHUNK:,} chars/chunk)…")
        all_chunks = split_pages_into_chunks(pages_data)
        for c in all_chunks:
            c["sourceFile"] = filepath.name
        stats["chunksCreated"] = len(all_chunks)
        manifest = {
            "sourceFile":  filepath.name,
            "totalPages":  stats["totalPages"],
            "totalChars":  total_chars,
            "chunkCount":  len(all_chunks),
            "minChunkTarget": _MIN_CHUNK,
            "maxChunkTarget": _MAX_CHUNK,
            "chunks":      all_chunks,
        }
        chunk_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _uw.log(f"    {len(all_chunks)} chunk(s) → {chunk_path.name}")

    if mode == "chunk_only":
        elapsed = round(time.time() - t_start, 1)
        report_data["files"][filepath.name] = {
            "status":         "chunked",
            "totalPages":     stats["totalPages"],
            "totalTextChars": total_chars,
            "chunksCreated":  stats["chunksCreated"],
            "warnings":       stats["warnings"],
            "elapsedSeconds": elapsed,
            "outputPaths":    {
                "extractedText": str(text_path),
                "chunks":        str(chunk_path),
            },
        }
        return

    # ── Stage 3: Question generation ──────────────────────────────────────────
    chunks_to_process = all_chunks[:max_chunks] if max_chunks else all_chunks
    stats["chunksProcessed"] = len(chunks_to_process)
    _uw.log(
        f"  Stage 3: {'[DRY-RUN]' if mode == 'dry_run' else '[GENERATE]'} "
        f"{len(chunks_to_process)} chunk(s), {questions_per_chunk} q/chunk"
    )

    all_questions: List[Dict] = []
    gen_stats = {
        "validationFailures": 0,
        "retries":            0,
        "repairsSucceeded":   0,
        "repairFailures":     0,
    }

    for ci, chunk in enumerate(chunks_to_process):
        c_stat: Dict = {
            "chunkId":    chunk["chunkId"],
            "pageStart":  chunk["pageStart"],
            "pageEnd":    chunk["pageEnd"],
            "charCount":  chunk["charCount"],
        }

        if mode == "dry_run":
            placeholder_qs = [
                _uw._placeholder_question(len(all_questions) + i + 1)
                for i in range(questions_per_chunk)
            ]
            _attach_chunk_figures_to_questions(placeholder_qs, chunk, stem)
            all_questions.extend(placeholder_qs)
            c_stat["status"]             = "dry-run"
            c_stat["questionsGenerated"] = questions_per_chunk
            c_stat["figuresAttached"]    = len(chunk.get("figures") or [])
        else:
            chunk_text = _inject_asset_markers(chunk)
            try:
                qs, chunk_warns = _uw.call_gemini_with_retry(
                    chunk_text, questions_per_chunk, chunk["chunkId"], gen_stats
                )
                qs = _uw.renumber_questions(qs, len(all_questions))
                fig_summary = _attach_chunk_figures_to_questions(
                    qs, chunk, stem, enable_gate=True
                )
                all_questions.extend(qs)
                stats["warnings"].extend(chunk_warns)
                c_stat["status"]             = "ok"
                c_stat["questionsGenerated"] = len(qs)
                c_stat["figuresAttached"]    = fig_summary["attached"]
                c_stat["figuresSuppressed"]  = fig_summary["suppressed"]
                if fig_summary["suppressions"]:
                    c_stat["figureSuppressions"] = fig_summary["suppressions"]
                _gate_note = (
                    f", {fig_summary['suppressed']} figure(s) gated out"
                    if fig_summary["suppressed"] else ""
                )
                _uw.log(
                    f"    Chunk {chunk['chunkId']}: {len(qs)} question(s) generated"
                    f"{_gate_note}"
                )
                time.sleep(1)
            except json.JSONDecodeError as exc:
                msg = f"chunk {chunk['chunkId']} JSON parse error: {exc}"
                _uw.warn(msg)
                stats["warnings"].append(msg)
                c_stat["status"] = "json_error"
                c_stat["error"]  = str(exc)
            except Exception as exc:
                msg = f"chunk {chunk['chunkId']} failed: {exc}"
                _uw.warn(msg)
                stats["warnings"].append(msg)
                c_stat["status"] = "error"
                c_stat["error"]  = str(exc)

        stats["chunkStats"].append(c_stat)

    if mode == "dry_run":
        stats["warnings"].append(
            "dry-run: questions are placeholders, not Gemini-generated"
        )

    stats["questionsGenerated"] = len(all_questions)
    stats["validationFailures"] = gen_stats["validationFailures"]
    stats["repairsSucceeded"]   = gen_stats["repairsSucceeded"]
    stats["repairFailures"]     = gen_stats["repairFailures"]
    stats["figuresAttached"]    = sum(
        len(q.get("explanationImages") or []) for q in all_questions
    )

    dup_warns = _uw.check_duplicate_stems(all_questions)
    if dup_warns:
        stats["warnings"].extend(dup_warns)

    # ── Stage 4: Write app-ready JSON ─────────────────────────────────────────
    app_json = _uw.build_app_ready_json(artifact_stem, all_questions, stats["warnings"])
    app_json["sourceFile"] = filepath.name
    app_json["metadata"] = {
        **(app_json.get("metadata") if isinstance(app_json.get("metadata"), dict) else {}),
        "sourceFile": filepath.name,
        "sourceStem": stem,
        "artifactStem": artifact_stem,
        "pageLimit": max_pages,
    }
    app_path = APP_DIR / f"{artifact_stem}_app_ready.json"
    app_path.write_text(json.dumps(app_json, indent=2), encoding="utf-8")
    _uw.log(f"  App-ready → {app_path.name} ({len(all_questions)} questions)")

    if mode != "dry_run":
        gen_path = GEN_DIR / f"{artifact_stem}_generated.json"
        gen_path.write_text(json.dumps(all_questions, indent=2), encoding="utf-8")

    elapsed = round(time.time() - t_start, 1)
    report_data["files"][filepath.name] = {
        "status":             "ok",
        "totalPages":         stats["totalPages"],
        "totalSourcePages":   stats.get("totalSourcePages", stats["totalPages"]),
        "totalTextChars":     stats["totalTextChars"],
        "chunksCreated":      stats["chunksCreated"],
        "chunksProcessed":    stats["chunksProcessed"],
        "figuresDetected":    stats.get("figuresDetected", 0),
        "figuresKept":        stats.get("figuresKept", 0),
        "figuresIgnored":     stats.get("figuresIgnored", 0),
        "tablesDetected":     stats.get("tablesDetected", 0),
        "tablesExtracted":    stats.get("tablesExtracted", 0),
        "questionsGenerated": stats["questionsGenerated"],
        "figuresAttached":    stats.get("figuresAttached", 0),
        "validationFailures": stats["validationFailures"],
        "repairsSucceeded":   stats["repairsSucceeded"],
        "repairFailures":     stats["repairFailures"],
        "warnings":           stats["warnings"],
        "chunkStats":         stats["chunkStats"],
        "elapsedSeconds":     elapsed,
        "dryRun":             (mode == "dry_run"),
        "outputPaths": {
            "appReady":      str(app_path),
            "chunks":        str(chunk_path),
            "extractedText": str(text_path),
        },
    }


# ── Discover helpers ──────────────────────────────────────────────────────────

def _discover_pdfs() -> List[Path]:
    if not INPUT_DIR.exists():
        return []
    return sorted(
        f for f in INPUT_DIR.iterdir()
        if f.is_file() and f.suffix.lower() == ".pdf"
    )


def _discover_extracted_texts() -> List[Path]:
    if not TEXT_DIR.exists():
        return []
    return sorted(f for f in TEXT_DIR.iterdir() if f.name.endswith("_text.json"))


def _stem_from_text_path(p: Path) -> str:
    return p.stem.removesuffix("_text")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mehlman Medical Mastery PDF → Step 2 Question Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Modes (mutually exclusive):
              --extract-only     Extract text (and optionally assets) per page; no questions.
              --chunk-only       Build chunk manifests from existing extracted text; no questions.
              --dry-run          Full pipeline with placeholder questions (no Gemini).
              --generate         Full pipeline with live Gemini generation (Milestone 2).

            Examples:
              python3 generate_mehlman_questions.py --dry-run
              python3 generate_mehlman_questions.py --extract-only --extract-assets
              python3 generate_mehlman_questions.py --chunk-only
              python3 generate_mehlman_questions.py --dry-run --extract-assets --max-chunks 2
              python3 generate_mehlman_questions.py --generate                       # 1 q / 1.5K chunk
              python3 generate_mehlman_questions.py --generate --questions-per-chunk 3
              python3 generate_mehlman_questions.py --dry-run --resume
              python3 generate_mehlman_questions.py --dry-run --force
            """
        ),
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--extract-only",
        action="store_true",
        help="Extract text per page and save to extracted_text/; stop before chunking.",
    )
    mode_group.add_argument(
        "--chunk-only",
        action="store_true",
        help="Build chunk manifests from existing extracted_text/ files; no questions.",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Full pipeline with placeholder questions; no Gemini API calls.",
    )
    mode_group.add_argument(
        "--generate",
        action="store_true",
        help="Full pipeline with live Gemini generation. Requires GEMINI_API_KEY.",
    )

    parser.add_argument(
        "--extract-assets",
        action="store_true",
        help="Also extract figures (PyMuPDF) and tables (pdfplumber) alongside text.",
    )
    parser.add_argument(
        "--input-file",
        default="",
        help="Process one selected PDF instead of every PDF in input_pdfs/.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional output root. Writes extracted assets, app-ready JSON, generated files, and reports under this directory.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="Limit processing to the first N pages of each PDF.",
    )
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        metavar="N",
        help="Limit the number of chunks processed per file (useful for testing).",
    )
    parser.add_argument(
        "--questions-per-chunk",
        type=int,
        default=1,
        metavar="N",
        help="Questions to generate per chunk (default: 1, paired with 1.5K-char chunks for one-fact-per-question focus).",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing extracted_text/ and chunks/ files when present.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing files and reprocess from scratch.",
    )
    # v5.8: Advanced Mode v5.2 pipeline. Mehlman already has its own
    # `--questions-per-chunk` flag (for the legacy 1-fact-per-question
    # path), so we skip the v5 shared adapter's chunk-arg group and
    # add `--v5-chunk-size` manually to avoid name collisions.
    add_v5_cli_args(parser, include_chunk_args=False)
    parser.add_argument(
        "--v5-chunk-size",
        type=int,
        default=3000,
        help=(
            "v5 only: max characters per chunk passed to the v5 kernel "
            "(default 3000). Mehlman's existing --questions-per-chunk is "
            "reused as v5's per-chunk Q count when --v5 is set."
        ),
    )

    args = parser.parse_args()

    if args.resume and args.force:
        parser.error("--resume and --force are mutually exclusive.")
    try:
        output_root = _apply_output_dir(args.output_dir) if args.output_dir else None
    except ValueError as exc:
        parser.error(str(exc))

    # Default mode when none specified
    if not any([args.extract_only, args.chunk_only, args.dry_run, args.generate]):
        args.dry_run = True

    if args.generate and not os.environ.get("GEMINI_API_KEY", "").strip():
        parser.error(
            "--generate requires GEMINI_API_KEY to be set.\n"
            "  export GEMINI_API_KEY=your_key_here"
        )

    if args.generate and not (PROMPTS_DIR / "mehlman_pdf_to_questions_prompt.txt").exists():
        parser.error(
            f"Prompt file not found: {PROMPTS_DIR / 'mehlman_pdf_to_questions_prompt.txt'}"
        )

    # Determine mode string
    if args.extract_only:
        mode = "extract_only"
    elif args.chunk_only:
        mode = "chunk_only"
    elif args.generate:
        mode = "generate"
    else:
        mode = "dry_run"

    _uw.log("=" * 64)
    _uw.log("Mehlman PDF → Question Generator")
    _uw.log(f"  Mode:                 {mode}")
    _uw.log(f"  Extract assets:       {args.extract_assets}")
    _uw.log(f"  Input file:           {args.input_file or 'input_pdfs/*.pdf'}")
    if output_root:
        _uw.log(f"  Output root:          {output_root}")
    _uw.log(f"  Max pages:            {args.max_pages or 'unlimited'}")
    _uw.log(f"  Max chunks:           {args.max_chunks or 'unlimited'}")
    _uw.log(f"  Questions per chunk:  {args.questions_per_chunk}")
    _uw.log(f"  Resume:               {args.resume}")
    _uw.log(f"  Force:                {args.force}")
    _uw.log("=" * 64)

    # Create output dirs
    for d in (
        TEXT_DIR, FIG_DIR, TABLE_DIR,
        SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Determine input files based on mode
    if mode == "chunk_only":
        text_files = _discover_extracted_texts()
        if not text_files:
            _uw.log(
                "No extracted text files found in extracted_text/. "
                "Run --extract-only first."
            )
            _uw.write_report(
                {"status": "no_input_files", "mode": mode, "files": {}},
                prefix="mehlman_generation_report",
            )
            return
        # Build fake filepath objects pointing to input_pdfs/ for report keys
        input_files = [INPUT_DIR / (_stem_from_text_path(t) + ".pdf") for t in text_files]
        _uw.log(
            f"Found {len(text_files)} extracted text file(s): "
            f"{[t.name for t in text_files]}"
        )
    else:
        input_files = [Path(args.input_file).expanduser().resolve()] if args.input_file else _discover_pdfs()
        if not input_files:
            _uw.log(
                f"No PDF files found in {INPUT_DIR}. "
                "Drop Mehlman PDFs into input_pdfs/ and re-run."
            )
            _uw.write_report(
                {"status": "no_input_files", "mode": mode, "files": {}},
                prefix="mehlman_generation_report",
            )
            return
        _uw.log(
            f"Found {len(input_files)} PDF(s): {[f.name for f in input_files]}"
        )

    # v5.8: resolve v5 config once when --v5 is passed in live mode.
    v5_cfg: Optional[Dict[str, Any]] = None
    if getattr(args, "v5", False):
        if mode != "generate":
            _uw.warn("--v5 has no effect outside --generate mode; ignoring v5 flag.")
        else:
            try:
                v5_cfg = resolve_v5_cfg(args)
                _uw.log(
                    "v5 pipeline configured — order_mix=%s difficulty_mix=%s seed=%d"
                    % (args.v5_order_mix, args.v5_difficulty_mix, args.v5_seed)
                )
            except ValueError as exc:
                _uw.warn(f"Invalid v5 mix arguments: {exc}; --v5 disabled.")
                v5_cfg = None

    report_data: Dict = {
        "runTimestamp":      datetime.now().isoformat(),
        "model":             _uw.GEMINI_MODEL,
        "mode":              mode,
        "extractAssets":     args.extract_assets,
        "maxChunks":         args.max_chunks,
        "questionsPerChunk": args.questions_per_chunk,
        "resume":            args.resume,
        "inputFiles":        [f.name for f in input_files],
        "files":             {},
    }

    # Mehlman's existing --questions-per-chunk default is 1 (paired with
    # 1.5K chunks). For v5 we treat that number as v5's per-chunk Q count
    # — same semantics, different downstream.
    # A questions_per_file budget is required by process_file_v5; estimate
    # it as ~15 × number-of-files for compatibility with the legacy
    # questions-per-file default elsewhere in the uworld family.
    legacy_qpf_estimate = 15 * max(1, len(input_files))

    t_total = time.time()
    for filepath in input_files:
        try:
            used_v5 = False
            if v5_cfg is not None:
                try:
                    process_file_v5_uworld_family(
                        uw_module=_uw,
                        filepath=filepath,
                        questions_per_file=legacy_qpf_estimate,
                        v5_cfg=v5_cfg,
                        report_data=report_data,
                        pipeline_label="v5.2-organic",
                        chunk_size=int(args.v5_chunk_size),
                        questions_per_chunk=int(args.questions_per_chunk),
                    )
                    used_v5 = True
                except Exception as v5_exc:
                    _uw.warn(
                        f"v5 pipeline failed for {filepath.name} ({v5_exc}); "
                        f"falling back to legacy Mehlman pipeline."
                    )
            if not used_v5:
                process_pdf(
                    filepath,
                    mode=mode,
                    extract_assets=args.extract_assets,
                    max_pages=args.max_pages,
                    max_chunks=args.max_chunks,
                    questions_per_chunk=args.questions_per_chunk,
                    resume=args.resume,
                    report_data=report_data,
                )
        except Exception as exc:
            _uw.warn(f"Fatal error on {filepath.name}: {exc}")
            report_data["files"][filepath.name] = {
                "status": "error",
                "error":  str(exc),
            }

    report_data["totalElapsedSeconds"] = round(time.time() - t_total, 1)
    _uw.write_report(report_data, prefix="mehlman_generation_report")
    _uw.log("Done.")


if __name__ == "__main__":
    main()
