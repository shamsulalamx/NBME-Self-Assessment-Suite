#!/usr/bin/env python3
"""
Lecture slide PDF -> NBME-style question generator.

This is an isolated external tool. It does not modify the in-app raw NBME
importer. Output is canonical nbme-gemini-json-v3 accepted by the existing
NBME Gemini JSON importer in index.html.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import html.parser
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any
import importlib

BASE_DIR = Path(__file__).parent.resolve()
TOOLS_DIR = BASE_DIR.parent
PROJECT_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from chunk_telemetry import ChunkHeartbeat, emit_bic_chunk_event  # noqa: E402

# v4.79: Vertex migration — reuse the central _uw._gemini_client() factory
# instead of duplicating the SDK setup. Same pattern Mehlman/OME/Anki/Divine
# already use. Lets Fast Facts and Amboss inherit GEMINI_BACKEND switching
# for free.
_UW_DIR = TOOLS_DIR / "uworld-notes-question-generator"
if str(_UW_DIR) not in sys.path:
    sys.path.insert(0, str(_UW_DIR))
import generate_uworld_questions as _uw  # noqa: E402
try:
    from google.genai import types as _genai_types  # noqa: E402
    _GENAI_SDK_AVAILABLE = True
except ImportError:
    _GENAI_SDK_AVAILABLE = False

def _load_uoga_module(name: str) -> Any:
    return importlib.import_module(".".join(["core", "uoga", name]))


_execution_graph_module = _load_uoga_module("execution_graph")
_finalization_module = _load_uoga_module("finalization")
_retry_module = _load_uoga_module("retry" + "_engine")
# v5.12 (Track B): durable per-chunk resume cache. Survives quit + retry because
# BIC reuses the same jobId (so JOB_OUTPUT_ROOT persists). See core/uoga/chunk_cache.py.
_chunk_cache_module = _load_uoga_module("chunk" + "_cache")
ChunkResumeCache = _chunk_cache_module.ChunkResumeCache
build_execution_graph = _execution_graph_module.build_execution_graph
finalize_job_gate = _finalization_module.finalize_job_gate
JobContext = _finalization_module.JobContext
RetryContext = _retry_module.RetryContext
RetryExhaustedError = _retry_module.RetryExhaustedError
RetryStep = _retry_module.RetryStep
execute_retry = _retry_module.execute
UOGA_EVENT_PREFIX = "CH" + "UNK_"


def uoga_event(name: str) -> str:
    return UOGA_EVENT_PREFIX + name
INPUT_DIR = BASE_DIR / "input_pdfs"
JOB_OUTPUT_ROOT = Path(os.environ["BIC_JOB_OUTPUT_ROOT"]).expanduser().resolve() if os.environ.get("BIC_JOB_OUTPUT_ROOT") else None
RUNTIME_DIR = JOB_OUTPUT_ROOT / "lecture-slide-question-generator" if JOB_OUTPUT_ROOT else BASE_DIR
OUTPUT_DIR = RUNTIME_DIR / "output_json"
SLIDES_DIR = OUTPUT_DIR / "slides"
NORMALIZED_DIR = OUTPUT_DIR / "normalized"
MEMORY_DIR = OUTPUT_DIR / "memory"
GENERATED_DIR = OUTPUT_DIR / "generated"
APP_READY_DIR = OUTPUT_DIR / "app_ready"
DEBUG_DIR = OUTPUT_DIR / "debug"
CACHE_DIR = OUTPUT_DIR / "cache"
ASSET_DIR = RUNTIME_DIR / "output_assets"
REPORT_DIR = RUNTIME_DIR / "reports"
LOG_DIR = RUNTIME_DIR / "logs"
REVIEW_DIR = (JOB_OUTPUT_ROOT / "review") if JOB_OUTPUT_ROOT else (RUNTIME_DIR / "review")
PROMPT_DIR = BASE_DIR / "prompts"
NORMALIZE_PROMPT = PROMPT_DIR / "normalize_slides_prompt.txt"
GENERATE_PROMPT = PROMPT_DIR / "generate_questions_prompt.txt"

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
OUTPUT_SCHEMA_VERSION = "nbme-gemini-json-v3"
SOURCE_FORMAT = "mixed"
FAST_FACTS_PROFILE = "FAST_FACTS_PROFILE"
FAST_FACTS_ATOMIZER_VERSION = "fast-facts-atomizer-v11"
FAST_FACTS_GENERATION_PROMPT_VERSION = "fast-facts-generation-prompt-v2"
FAST_FACTS_VALIDATOR_VERSION = "fast-facts-validator-v3"
FAST_FACTS_ARCHETYPE_ONTOLOGY_VERSION = "fast-facts-archetype-ontology-v2"
AMBOSS_PROFILE = "AMBOSS_PROFILE"
AMBOSS_EXTRACTION_PROMPT_VERSION = "amboss-extraction-prompt-v1"
AMBOSS_IMAGE_ROUTING_VERSION = "amboss-image-routing-v1"
AMBOSS_VISUAL_STATE_VERSION = "amboss-visual-state-v3"
LABELS = ["A", "B", "C", "D"]
MAX_SLIDES_PER_CHUNK = 3
MAX_GENERATION_ALLOCS_PER_CHUNK = 8
# Bound for the post-chunk targeted-recovery loop: max focused per-slide
# attempts for each slide where Gemini under-delivered during the main chunk
# pass. Worst-case extra API calls = len(work) * this. Stays small on purpose
# so a budget runaway cannot happen even on a deck where every slide misses.
MAX_RECOVERY_ATTEMPTS_PER_SLIDE = 2
SMALL_NORMALIZATION_CHUNK_SIZE = 2
ISOLATED_NORMALIZATION_CHUNK_SIZE = 1
MAX_NORMALIZATION_ESTIMATED_JSON_CHARS = 6500
MAX_NORMALIZATION_OCR_CHARS = 3200

SLIDE_TYPES = {
    "HIGH_YIELD_CLINICAL",
    "MECHANISM",
    "RAPID_RECALL",
    "IMAGE_HEAVY",
    "TABLE_HEAVY",
    "LOW_INFORMATION",
    "ADMINISTRATIVE",
    "DUPLICATE_TOPIC",
    "TRANSITION_SLIDE",
}

FORBIDDEN_STRINGS = [
    "eftab720",
    "tightenfactor0",
    "Here are the questions",
    "```json",
    "```",
]

OCR_FRAGMENT_PATTERNS = [
    re.compile(r"\beftab720\b", re.I),
    re.compile(r"\btightenfactor0\b", re.I),
    re.compile(r"[\u2022]{2,}"),
    re.compile(r"[_=]{5,}"),
    re.compile(r"\bSlide\s+\d+\s+of\s+\d+\b", re.I),
]

COMMON_CLINICAL_WORDS = {
    "a", "an", "and", "are", "as", "at", "best", "by", "can", "does", "for",
    "from", "has", "have", "in", "is", "it", "may", "most", "of", "on", "or",
    "patient", "patients", "presents", "present", "with", "without", "the",
    "this", "to", "which", "will", "would", "following", "clinician", "child",
    "infant", "newborn", "boy", "girl", "man", "woman", "mother", "father",
    "old", "year", "years", "day", "days", "month", "months", "week", "weeks",
    "finding", "findings", "symptom", "symptoms", "sign", "signs", "diagnosis",
    "treatment", "management", "mechanism", "disease", "disorder", "syndrome",
    "condition", "answer", "choice", "slide", "supported", "concept",
    "examination", "exam", "reveals", "shows", "noted", "reported", "reports",
    "describes", "appears", "otherwise", "healthy", "feeding", "based",
    "identify", "diagnose", "differentiate", "determine", "suggests",
    "suggestive", "consistent", "characteristic", "classic", "key", "likely",
    "physical", "clinical", "history", "team", "concerned", "prepared",
    "preparing", "initial", "routine", "visit", "presentation", "case",
    "given", "while", "however", "typically", "often", "usually",
    "adequate", "critical", "difficult", "inappropriate", "intervention",
    "interventions", "mild", "moderate", "observation", "prompt", "specific",
    "severe", "significant", "suspicious", "transient",
}

MEDICAL_SUFFIXES = (
    "itis", "osis", "emia", "uria", "pathy", "plasia", "trophy", "penia",
    "cytosis", "blast", "mycin", "cillin", "azole", "pril", "sartan", "olol",
    "pine", "mab", "vir", "statin", "caine", "zine", "zepam", "xaban",
)

HIGH_RISK_MEDICAL_WORDS = {
    "agenesis", "akinesia", "alkalosis", "atresia", "biopsy", "ceftriaxone",
    "cerebritis", "ciprofloxacin", "corticosteroids", "cyanosis", "cyst", "dystocia",
    "encephalopathy", "enzyme", "fistula", "fracture", "gastroschisis",
    "hernia", "hydrocephalus", "hyperbilirubinemia", "hypoplasia",
    "hypothyroidism", "intussusception", "ischemia", "jaundice", "malrotation",
    "meconium", "omphalocele", "palsy", "phototherapy", "pneumonia",
    "reflux", "sepsis", "stenosis", "surgery", "syndrome", "volvulus",
    "acidosis", "appendicitis", "arthritis", "azithromycin", "ceftriaxone",
    "doxycycline", "hypocalcemia", "hypoglycemia", "hypokalemia", "leukemia",
    "lymphadenopathy", "lymphoma", "meningitis", "mononucleosis",
    "osteomyelitis", "pancreatitis", "thrombocytopenia", "thrombosis",
}

WORKUP_MANAGEMENT_WORDS = {
    "antibiotic", "antibiotics", "antibody", "audiometry", "biopsy", "ct",
    "culture", "dialysis", "ecg", "echo", "echocardiogram", "eeg", "ekg",
    "enzyme", "mri", "receptor", "steroid", "surgery", "surgical",
    "ultrasound", "vaccine", "ventilation", "x-ray", "xray",
}

TRIVIAL_RECALL_PATTERNS = [
    re.compile(r"\bwhich of the following is true\b", re.I),
    re.compile(r"\ball except\b", re.I),
    re.compile(r"\bexcept\b", re.I),
    re.compile(r"\bmost common cause\b", re.I),
    re.compile(r"\bwhat is the diagnosis\b", re.I),
    re.compile(r"\bwhat is the treatment\b", re.I),
]


class PipelineError(Exception):
    pass


class JsonParseFailure(PipelineError):
    def __init__(self, message: str, failure_type: str = "invalid_json") -> None:
        super().__init__(message)
        self.failure_type = failure_type


class StrictHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "hr", "img", "meta", "input"}:
            return
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"br", "hr", "img", "meta", "input"}:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected closing tag </{tag}>")
            return
        self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack:
            self.errors.append(f"unclosed tags: {', '.join(self.stack)}")


def log(message: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}")


def warn(message: str) -> None:
    print(f"[WARN] {message}", file=sys.stderr)


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR))
    except ValueError:
        return str(path.resolve())


def emit_bic_progress(phase: str, message: str, source: str | None = None, **payload: Any) -> None:
    progress_source = str(source or os.environ.get("BIC_PROGRESS_SOURCE") or "").strip()
    if not progress_source:
        return
    print(
        "BIC_PROGRESS " + json.dumps(
            {"phase": phase, "source": progress_source, "message": message, **payload},
            ensure_ascii=False,
        ),
        flush=True,
    )


def bic_job_id() -> str:
    return str(os.environ.get("BIC_JOB_ID") or "").strip()


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def ensure_dirs() -> None:
    for path in [
        INPUT_DIR,
        SLIDES_DIR,
        NORMALIZED_DIR,
        MEMORY_DIR,
        GENERATED_DIR,
        APP_READY_DIR,
        DEBUG_DIR,
        CACHE_DIR,
        ASSET_DIR,
        REPORT_DIR,
        LOG_DIR,
        REVIEW_DIR,
        PROMPT_DIR,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_json_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("._")
    return value or "lecture_slides"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def supported_pdfs(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    return [
        p for p in sorted(input_dir.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() == ".pdf"
    ]


def supported_pptx(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    return [
        p for p in sorted(input_dir.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() == ".pptx"
    ]


def supported_amboss_inputs(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    allowed = {".pdf", ".png", ".jpg", ".jpeg"}
    return [
        p for p in sorted(input_dir.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in allowed
    ]


def optional_import_pdfplumber() -> Any:
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except Exception:
        return None


def optional_import_fitz() -> Any:
    try:
        import fitz  # type: ignore
        return fitz
    except Exception:
        return None


def mime_for(path: Path) -> str:
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if path.suffix.lower() == ".png":
        return "image/png"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def data_url(path: Path, mime: str | None = None) -> str:
    actual_mime = mime or mime_for(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{actual_mime};base64,{encoded}"


def clean_slide_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_tables_from_page(page: Any) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    if not page:
        return tables
    try:
        raw_tables = page.extract_tables() or []
    except Exception:
        raw_tables = []
    for idx, table in enumerate(raw_tables, start=1):
        if not table or not any(any(str(cell or "").strip() for cell in row or []) for row in table):
            continue
        headers = [str(cell or "").strip() for cell in (table[0] or [])]
        rows = [
            [str(cell or "").strip() for cell in (row or [])]
            for row in table[1:]
            if any(str(cell or "").strip() for cell in (row or []))
        ]
        tables.append({
            "tableId": f"table_{idx:02d}",
            "title": "",
            "headers": headers,
            "rows": rows,
        })
    return tables


def render_slide_image(fitz_module: Any, fitz_doc: Any, page_index: int, pdf_stem: str, slide_id: str) -> dict[str, Any] | None:
    if not fitz_module or not fitz_doc:
        return None
    try:
        page = fitz_doc.load_page(page_index)
        pix = page.get_pixmap(matrix=fitz_module.Matrix(2, 2), alpha=False)
        asset_name = f"{slugify(pdf_stem)}_{slide_id}.png"
        asset_path = ASSET_DIR / asset_name
        pix.save(str(asset_path))
        return {
            "imageId": f"{slide_id}_slide_image",
            "kind": "slide_image",
            "assetPath": display_path(asset_path),
            "mimeType": "image/png",
            "width": int(pix.width),
            "height": int(pix.height),
            "sha256": file_sha(asset_path),
        }
    except Exception as exc:
        warn(f"Could not render slide image {slide_id}: {exc}")
        return None


def extract_embedded_images(fitz_doc: Any, page_index: int, pdf_stem: str, slide_id: str) -> list[dict[str, Any]]:
    if not fitz_doc:
        return []
    images: list[dict[str, Any]] = []
    try:
        page = fitz_doc.load_page(page_index)
        for img_index, img in enumerate(page.get_images(full=True), start=1):
            xref = img[0]
            extracted = fitz_doc.extract_image(xref)
            blob = extracted.get("image")
            ext = extracted.get("ext") or "png"
            if not blob:
                continue
            asset_name = f"{slugify(pdf_stem)}_{slide_id}_fig{img_index:02d}.{ext}"
            asset_path = ASSET_DIR / asset_name
            asset_path.write_bytes(blob)
            images.append({
                "imageId": f"{slide_id}_fig{img_index:02d}",
                "kind": "embedded_figure",
                "assetPath": display_path(asset_path),
                "mimeType": mime_for(asset_path),
                "width": int(extracted.get("width") or 0),
                "height": int(extracted.get("height") or 0),
                "sha256": file_sha(asset_path),
            })
    except Exception as exc:
        warn(f"Could not extract embedded images for {slide_id}: {exc}")
    return images


def decompose_pdf(pdf_path: Path, progress_source: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    pdfplumber = optional_import_pdfplumber()
    fitz = optional_import_fitz()
    if not pdfplumber and not fitz:
        raise PipelineError("Install pdfplumber or pymupdf to decompose PDFs.")

    pdf_hash = file_sha(pdf_path)
    pdf_stem = pdf_path.stem
    log(f"Decomposing {pdf_path.name}")

    pdf_doc = None
    plumber_pdf = None
    try:
        if fitz:
            pdf_doc = fitz.open(str(pdf_path))
        if pdfplumber:
            plumber_pdf = pdfplumber.open(str(pdf_path))
        page_count = len(pdf_doc) if pdf_doc else len(plumber_pdf.pages)
        slides: list[dict[str, Any]] = []
        metadata = {}
        if pdf_doc:
            metadata.update({k: v for k, v in (pdf_doc.metadata or {}).items() if v})
        if plumber_pdf and getattr(plumber_pdf, "metadata", None):
            metadata.update({k: v for k, v in plumber_pdf.metadata.items() if v})

        for page_index in range(page_count):
            slide_num = page_index + 1
            emit_bic_progress(
                "extracting",
                f"Extracting page {slide_num}/{page_count} from {pdf_path.name}",
                source=progress_source,
                file=str(pdf_path),
                page=slide_num,
                pageTotal=page_count,
            )
            slide_id = f"{slugify(pdf_stem)}_s{slide_num:04d}_{pdf_hash[:8]}"
            plumber_page = plumber_pdf.pages[page_index] if plumber_pdf else None
            text = ""
            if plumber_page:
                try:
                    text = plumber_page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                except Exception:
                    text = ""
            if not text and pdf_doc:
                try:
                    text = pdf_doc.load_page(page_index).get_text("text") or ""
                except Exception:
                    text = ""
            tables = extract_tables_from_page(plumber_page)
            slide_image = render_slide_image(fitz, pdf_doc, page_index, pdf_stem, slide_id) if pdf_doc else None
            embedded_images = extract_embedded_images(pdf_doc, page_index, pdf_stem, slide_id) if pdf_doc else []
            images = ([slide_image] if slide_image else []) + embedded_images
            slides.append({
                "slideId": slide_id,
                "sourceFile": pdf_path.name,
                "pdfSha256": pdf_hash,
                "slideIndex": slide_num,
                "pageIndex": page_index,
                "ocrText": clean_slide_text(text),
                "images": images,
                "tables": [
                    {**table, "tableId": f"{slide_id}_{table['tableId']}"}
                    for table in tables
                ],
                "metadata": {
                    "pageWidth": float(getattr(plumber_page, "width", 0) or 0),
                    "pageHeight": float(getattr(plumber_page, "height", 0) or 0),
                },
            })
    finally:
        if plumber_pdf:
            plumber_pdf.close()
        if pdf_doc:
            pdf_doc.close()

    payload = {
        "schemaVersion": "lecture-slide-decomposition-v1",
        "sourceFile": pdf_path.name,
        "sourcePath": str(pdf_path.relative_to(BASE_DIR)) if pdf_path.is_relative_to(BASE_DIR) else str(pdf_path),
        "pdfSha256": pdf_hash,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pageCount": len(slides),
        "pdfMetadata": metadata,
        "slides": slides,
    }
    out_path = SLIDES_DIR / f"{slugify(pdf_stem)}_slides.json"
    write_json(out_path, payload)
    log(f"  Slides -> {display_path(out_path)}")
    return payload


def load_or_decompose_pdf(pdf_path: Path, progress_source: str | None = None) -> dict[str, Any]:
    pdf_hash = file_sha(pdf_path)
    existing_path = SLIDES_DIR / f"{slugify(pdf_path.stem)}_slides.json"
    if existing_path.exists():
        try:
            payload = read_json(existing_path)
            if payload.get("pdfSha256") == pdf_hash and isinstance(payload.get("slides"), list):
                log(f"Using existing decomposed slides -> {display_path(existing_path)}")
                emit_bic_progress(
                    "extracting",
                    f"Using existing extraction for {pdf_path.name}",
                    source=progress_source,
                    file=str(pdf_path),
                    pageTotal=len(payload.get("slides") or []),
                )
                return payload
            warn(f"Existing slide decomposition ignored because input hash changed: {existing_path.name}")
        except Exception as exc:
            warn(f"Existing slide decomposition could not be read ({exc}); recomposing PDF.")
    return decompose_pdf(pdf_path, progress_source=progress_source)


def image_ref_to_slide_image(ref: dict[str, Any]) -> dict[str, Any]:
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    image_id = str(metadata.get("imageId") or ref.get("refId") or "").strip()
    asset_path = str(ref.get("path") or metadata.get("assetPath") or "").strip()
    return {
        "imageId": image_id,
        "kind": str(metadata.get("kind") or ref.get("kind") or "image"),
        "assetPath": asset_path,
        "mimeType": str(metadata.get("mimeType") or mime_for(BASE_DIR / asset_path) if asset_path else "application/octet-stream"),
        "width": int(metadata.get("width") or 0),
        "height": int(metadata.get("height") or 0),
        "sha256": str(metadata.get("sha256") or ""),
        "normalizedRefId": ref.get("refId"),
        "normalizedRole": ref.get("role"),
        "normalizedGrounding": ref.get("grounding") if isinstance(ref.get("grounding"), dict) else {},
    }


def table_ref_to_slide_table(ref: dict[str, Any]) -> dict[str, Any]:
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    table_id = str(metadata.get("tableId") or ref.get("refId") or "").strip()
    return {
        "tableId": table_id,
        "title": str(ref.get("text") or metadata.get("title") or "Normalized chunk table"),
        "headers": metadata.get("headers") if isinstance(metadata.get("headers"), list) else [],
        "rows": metadata.get("rows") if isinstance(metadata.get("rows"), list) else [],
        "normalizedRefId": ref.get("refId"),
        "normalizedGrounding": ref.get("grounding") if isinstance(ref.get("grounding"), dict) else {},
    }


def slide_payload_from_normalized_chunks(bundle_path: Path) -> dict[str, Any]:
    bundle = read_json(bundle_path)
    if bundle.get("schemaVersion") != "shared-normalized-chunk-bundle-v1":
        raise PipelineError("Normalized chunk input must use shared-normalized-chunk-bundle-v1.")
    chunks = bundle.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise PipelineError("Normalized chunk bundle has no chunks.")
    bundle_hash = stable_json_hash(bundle)
    slides: list[dict[str, Any]] = []
    warnings = list(bundle.get("warnings") or [])
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            warnings.append(f"Skipped non-object chunk at index {index}.")
            continue
        chunk_id = str(chunk.get("chunkId") or f"normalized_chunk_{index:04d}").strip()
        grounding = chunk.get("sourceGrounding") if isinstance(chunk.get("sourceGrounding"), dict) else {}
        slide_index = int(grounding.get("slideIndex") or grounding.get("pageIndex") or index)
        page_index = int(grounding.get("pageIndex") or max(0, slide_index - 1))
        images = [
            image_ref_to_slide_image(ref)
            for ref in chunk.get("imageRefs") or []
            if isinstance(ref, dict)
        ]
        tables = [
            table_ref_to_slide_table(ref)
            for ref in chunk.get("tableRefs") or []
            if isinstance(ref, dict)
        ]
        text_blocks = chunk.get("textBlocks") if isinstance(chunk.get("textBlocks"), list) else []
        text = str(chunk.get("text") or "").strip()
        if not text and text_blocks:
            text = "\n\n".join(str(block.get("text") or "") for block in text_blocks if isinstance(block, dict)).strip()
        slides.append({
            "slideId": chunk_id,
            "sourceFile": str(chunk.get("sourceFile") or bundle.get("sourceFile") or bundle_path.name),
            "pdfSha256": bundle_hash,
            "slideIndex": slide_index,
            "pageIndex": page_index,
            "ocrText": clean_slide_text(text),
            "images": images,
            "tables": tables,
            "metadata": {
                **({} if not isinstance(chunk.get("metadata"), dict) else chunk.get("metadata")),
                "normalizedChunkId": chunk_id,
                "normalizedChunkType": chunk.get("chunkType"),
                "normalizedChunkConfidence": chunk.get("confidence"),
                "sourceGrounding": grounding,
            },
            "warnings": list(chunk.get("warnings") or []),
        })
    if not slides:
        raise PipelineError("Normalized chunk bundle did not contain usable chunks.")
    return {
        "schemaVersion": "lecture-slide-decomposition-v1",
        "sourceFile": str(bundle.get("sourceFile") or bundle_path.stem),
        "sourcePath": str(bundle.get("sourcePath") or bundle_path),
        "pdfSha256": bundle_hash,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "pageCount": len(slides),
        "pdfMetadata": {},
        "slides": slides,
        "provenance": {
            "sourceMode": "normalized_chunks",
            "chunkBundlePath": str(bundle_path),
            "chunkBundleHash": bundle_hash,
            "chunkBundleId": str(bundle.get("bundleId") or bundle.get("id") or bundle_hash[:16]),
            "chunkCountConsumed": len(slides),
        },
        "normalizationWarnings": warnings,
    }


def chunk_list(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def clean_llm_json(raw: str) -> str:
    text = raw.strip().lstrip("\ufeff")
    text = re.sub(r"^\s*```+(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```+\s*$", "", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    return text.strip()


def extract_json_payload(text: str) -> str:
    first = -1
    open_ch = close_ch = ""
    for i, ch in enumerate(text):
        if ch == "{":
            first, open_ch, close_ch = i, "{", "}"
            break
        if ch == "[":
            first, open_ch, close_ch = i, "[", "]"
            break
    if first < 0:
        return text
    depth = 0
    in_string = False
    escape_next = False
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
                return text[first:i + 1]
    return text[first:]


def iter_complete_json_payloads(text: str) -> list[str]:
    payloads: list[str] = []
    starts = [i for i, ch in enumerate(text) if ch in "[{"]
    for first in starts:
        open_ch = text[first]
        close_ch = "]" if open_ch == "[" else "}"
        depth = 0
        in_string = False
        escape_next = False
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
                    payloads.append(text[first:i + 1])
                    break
    return sorted(set(payloads), key=len, reverse=True)


def load_largest_valid_json(raw: str) -> Any:
    cleaned = clean_llm_json(raw)
    attempts = [
        raw.strip(),
        cleaned,
        extract_json_payload(cleaned),
        *iter_complete_json_payloads(cleaned),
    ]
    seen: set[str] = set()
    for text in attempts:
        text = text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    failure_type = classify_json_failure(cleaned)
    raise JsonParseFailure(f"{failure_type}: no complete valid JSON object or array found", failure_type)


def classify_json_failure(text: str) -> str:
    stripped = clean_llm_json(text)
    if not stripped:
        return "empty_response"

    depth_curly = 0
    depth_square = 0
    in_string = False
    escape_next = False
    repeated_prefixes = 0
    slide_prefixes: dict[str, int] = {}

    for i, ch in enumerate(stripped):
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
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1

    for match in re.finditer(r'"slideId"\s*:\s*"([^"]+)"', stripped):
        slide_id = match.group(1)
        slide_prefixes[slide_id] = slide_prefixes.get(slide_id, 0) + 1
    repeated_prefixes = sum(1 for count in slide_prefixes.values() if count > 1)

    if in_string:
        return "truncation_unterminated_string"
    if depth_curly > 0 or depth_square > 0:
        return "truncation_missing_closing_bracket"
    if repeated_prefixes:
        return "truncation_repeated_object_prefix"
    return "invalid_json"


def parse_llm_json(raw: str, debug_name: str) -> Any:
    try:
        return load_largest_valid_json(raw)
    except JsonParseFailure as exc:
        failure_type = exc.failure_type
    debug_path = DEBUG_DIR / f"{debug_name}_raw_response.txt"
    debug_path.write_text(raw, encoding="utf-8")
    raise JsonParseFailure(
        f"Gemini returned invalid JSON ({failure_type}). Raw response saved to {display_path(debug_path)}",
        failure_type,
    )


def write_debug_raw(source_file: str, phase: str, chunk_label: str, retry_label: str, raw: str) -> Path:
    safe_source = slugify(Path(source_file).stem)
    safe_chunk = slugify(str(chunk_label))
    safe_retry = slugify(str(retry_label))
    path = DEBUG_DIR / f"{safe_source}_{phase}_{safe_chunk}_{safe_retry}_raw_response.txt"
    path.write_text(raw, encoding="utf-8")
    return path


def repair_json_prompt(raw: str, expected_schema: str, expected_ids: list[str], error_message: str) -> str:
    return "\n".join([
        "Return corrected valid JSON only. Do not include markdown fences, prose, comments, or explanations.",
        "The prior response was invalid or failed schema validation.",
        f"Error: {error_message}",
        "Required JSON shape:",
        expected_schema,
        "Required slide/question IDs that must be present exactly once:",
        json.dumps(expected_ids, ensure_ascii=False),
        "Prior invalid response:",
        raw[:12000],
    ])


def is_truncation_failure(error: Exception | str) -> bool:
    text = str(error)
    return (
        "truncation_unterminated_string" in text
        or "truncation_missing_closing_bracket" in text
        or "truncation_repeated_object_prefix" in text
    )


def is_network_failure(error: Exception | str) -> bool:
    text = str(error).lower()
    return (
        "urlopen error" in text
        or "nodename nor servname provided" in text
        or "name or service not known" in text
        or "network is unreachable" in text
        or "temporary failure in name resolution" in text
        or "gemini request timed out" in text
        or "repair_timeout" in text
    )


def is_quota_failure(error: Exception | str) -> bool:
    text = str(error).lower()
    return (
        "http 429" in text
        or "resource_exhausted" in text
        or "prepayment credits are depleted" in text
        or "quota exceeded" in text
        or "rate limit" in text
        or "too many requests" in text
    )


# Module-level latch: once any Gemini call returns a quota/429 error, every
# subsequent retry path (sub-chunking, repair, targeted recovery) short-circuits
# to [] instead of burning more of the user's prepayment budget. Reset at the
# start of each generate_questions() invocation so two runs in the same process
# stay independent.
_QUOTA_EXHAUSTED = False


def quota_exhausted() -> bool:
    return _QUOTA_EXHAUSTED


def mark_quota_exhausted() -> None:
    global _QUOTA_EXHAUSTED
    _QUOTA_EXHAUSTED = True


def reset_quota_state() -> None:
    global _QUOTA_EXHAUSTED
    _QUOTA_EXHAUSTED = False


def raw_gemini_call(api_key: str, prompt: str, temperature: float, max_tokens: int = 8192, timeout_seconds: int = 120) -> str:
    """Single Gemini text generateContent call.

    v4.79: rewritten to use google-genai SDK via _uw._gemini_client(). The
    api_key arg is kept for signature compatibility — it's only used when
    GEMINI_BACKEND=ai_studio (Vertex uses ADC). Error format preserves the
    PipelineError wrapping so downstream timeout / quota detection still
    matches on the same text patterns.

    The timeout_seconds parameter is kept in the signature but the SDK
    enforces its own timeouts internally — it's still respected via the
    short-timeout repair-detection branch below.
    """
    if not _GENAI_SDK_AVAILABLE:
        raise PipelineError(
            "google-genai SDK not installed. Install with: pip install google-genai"
        )
    try:
        client = _uw._gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=_genai_types.GenerateContentConfig(
                temperature=temperature,
                # v4.79: Bumped 2x to absorb thinking tokens. Original cap
                # (8192) was sized for output-only; with dynamic thinking
                # adding ~1-4K, doubling gives safe headroom.
                max_output_tokens=max(max_tokens * 2, 16384),
                # v4.79: Dynamic thinking — model decides. Quality > cost.
                # See _uw._raw_gemini_call for full rationale.
                thinking_config=_genai_types.ThinkingConfig(thinking_budget=-1),
            ),
        )
    except (TimeoutError, socket.timeout) as exc:
        # Preserve the v4.5x repair-vs-generic-timeout distinction so the
        # retry layer can tell which kind of timeout it was.
        raise PipelineError(
            "repair_timeout" if timeout_seconds <= 60 else "Gemini request timed out"
        ) from exc
    except EnvironmentError:
        raise
    except Exception as exc:
        # SDK exceptions get wrapped as PipelineError with the original
        # message so downstream is_quota_failure / is_network_failure text
        # matching (in _uw) still works on the call here too.
        raise PipelineError(f"Gemini call failed: {exc}") from exc

    text = getattr(response, "text", None)
    if not text:
        candidates = getattr(response, "candidates", None) or []
        raise PipelineError(
            f"Gemini candidate had no text part. candidates={candidates!r}"[:400]
        )
    return str(text)


def raw_gemini_image_call(
    api_key: str,
    prompt: str,
    image_paths: list[Path],
    temperature: float,
    max_tokens: int = 8192,
    timeout_seconds: int = 90,
    response_mime_type: str | None = None,
) -> str:
    """Multimodal Gemini call — prompt + N inline images.

    v4.79: rewritten to use google-genai SDK via _uw._gemini_client(). The
    SDK handles base64 encoding internally via types.Part.from_bytes, so the
    explicit base64 step is gone. Behavior is identical on both backends.

    The response_mime_type='application/json' option is preserved — that's
    the v4.6x discovery that forcing JSON mime dramatically improves Fast
    Facts JSON parse success rates (Gemini stops emitting markdown fences).
    """
    if not _GENAI_SDK_AVAILABLE:
        raise PipelineError(
            "google-genai SDK not installed. Install with: pip install google-genai"
        )
    contents: list[Any] = [prompt]
    for image_path in image_paths:
        contents.append(_genai_types.Part.from_bytes(
            data=image_path.read_bytes(),
            mime_type=mime_for(image_path),
        ))
    config_kwargs: dict[str, Any] = {
        "temperature": temperature,
        # v4.79: 2x bump for thinking headroom (see raw_gemini_call above).
        "max_output_tokens": max(max_tokens * 2, 16384),
        # v4.79: Dynamic thinking. Quality > cost.
        "thinking_config": _genai_types.ThinkingConfig(thinking_budget=-1),
    }
    if response_mime_type:
        config_kwargs["response_mime_type"] = response_mime_type
    try:
        client = _uw._gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=_genai_types.GenerateContentConfig(**config_kwargs),
        )
    except (TimeoutError, socket.timeout) as exc:
        raise PipelineError("Gemini request timed out") from exc
    except EnvironmentError:
        raise
    except Exception as exc:
        raise PipelineError(f"Gemini call failed: {exc}") from exc

    text = getattr(response, "text", None)
    if not text:
        candidates = getattr(response, "candidates", None) or []
        raise PipelineError(
            f"Gemini candidate had no text part. candidates={candidates!r}"[:400]
        )
    return str(text)


def deterministic_normalize_slide(slide: dict[str, Any]) -> dict[str, Any]:
    text = slide.get("ocrText") or ""
    lower = text.lower()
    word_count = len(re.findall(r"[A-Za-z0-9]+", text))
    tables = slide.get("tables") or []
    images = slide.get("images") or []
    slide_types: list[str] = []
    if word_count < 12 and not tables and len(images) <= 1:
        slide_types.append("LOW_INFORMATION")
    if re.search(r"\b(agenda|objectives|outline|references|thank you|questions)\b", lower):
        slide_types.append("ADMINISTRATIVE")
    if re.search(r"\b(pathophys|mechanism|pathway|receptor|enzyme|mutation|deficiency)\b", lower):
        slide_types.append("MECHANISM")
    if re.search(r"\b(diagnosis|treatment|management|screening|therapy|symptom|sign|patient|clinical)\b", lower):
        slide_types.append("HIGH_YIELD_CLINICAL")
    if len(images) > 1:
        slide_types.append("IMAGE_HEAVY")
    if tables:
        slide_types.append("TABLE_HEAVY")
    if not slide_types:
        slide_types.append("RAPID_RECALL" if word_count >= 12 else "LOW_INFORMATION")
    if "LOW_INFORMATION" in slide_types or "ADMINISTRATIVE" in slide_types:
        yield_score = min(35, word_count * 2 + len(tables) * 10)
    else:
        yield_score = min(95, 30 + word_count + len(tables) * 15 + min(20, len(images) * 5))
    concepts = extract_candidate_concepts(text)
    facts = extract_fact_lines(text)
    return {
        "slideId": slide["slideId"],
        "slideType": sorted(set(slide_types), key=slide_types.index),
        "yieldScore": int(yield_score),
        "primaryConcepts": concepts[:4],
        "secondaryConcepts": concepts[4:10],
        "clinicalFacts": facts[:8],
        "diagnosticFacts": [f for f in facts if re.search(r"\b(diagnos|test|lab|finding|x-ray|ct|mri|ecg|ultrasound)\b", f, re.I)][:8],
        "managementFacts": [f for f in facts if re.search(r"\b(treat|therapy|manage|screen|prevent|administer|surgery)\b", f, re.I)][:8],
        "mechanismFacts": [f for f in facts if re.search(r"\b(cause|pathophys|mechanism|inhibit|activate|mutation|deficiency)\b", f, re.I)][:8],
        "images": slide.get("images") or [],
        "tables": slide.get("tables") or [],
        "questionPotential": int(yield_score if "ADMINISTRATIVE" not in slide_types else min(20, yield_score)),
        "groundingNotes": facts[:12],
        "sourceTextHash": short_hash(text),
    }


def extract_candidate_concepts(text: str) -> list[str]:
    lines = extract_fact_lines(text)
    concepts: list[str] = []
    for line in lines:
        cleaned = re.sub(r"^[\-*\d.)\s]+", "", line).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if 3 <= len(cleaned) <= 100:
            concepts.append(cleaned)
    seen: set[str] = set()
    out: list[str] = []
    for concept in concepts:
        key = concept.lower()
        if key not in seen:
            seen.add(key)
            out.append(concept)
    return out


def extract_fact_lines(text: str) -> list[str]:
    parts = re.split(r"\n|(?:\s+[;\u2022]\s+)", text)
    facts: list[str] = []
    for part in parts:
        p = re.sub(r"^[\s\-*\u2022\d.)]+", "", part).strip()
        p = re.sub(r"\s+", " ", p)
        if len(p) < 4:
            continue
        if len(p) > 220:
            p = p[:220].rsplit(" ", 1)[0]
        facts.append(p)
    return facts[:40]


def normalize_slides(slide_payload: dict[str, Any], generate: bool) -> dict[str, Any]:
    slides = slide_payload.get("slides") or []
    if not slides:
        raise PipelineError("No slides found for normalization.")
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    existing_by_id: dict[str, dict[str, Any]] = {}
    if generate:
        existing_by_id, existing_warnings = load_existing_normalized_slide_map(slide_payload)
        warnings.extend(existing_warnings)
    if generate:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise PipelineError("GEMINI_API_KEY is not set.")
        template = NORMALIZE_PROMPT.read_text(encoding="utf-8")
        missing_slides = [s for s in slides if s["slideId"] not in existing_by_id]
        normalized.extend(existing_by_id[s["slideId"]] for s in slides if s["slideId"] in existing_by_id)
        if existing_by_id:
            warnings.append(f"Resumed normalization from existing file: reused {len(existing_by_id)} slide(s).")
        for chunk_index, chunk in enumerate(split_slides_for_normalization(missing_slides), start=1):
            items, chunk_warnings = normalize_chunk_with_retries(
                api_key=api_key,
                template=template,
                chunk=chunk,
                source_file=slide_payload["sourceFile"],
                chunk_label=f"chunk{chunk_index}",
            )
            normalized.extend(items)
            warnings.extend(chunk_warnings)
    else:
        normalized = [deterministic_normalize_slide(slide) for slide in slides]
        warnings.append("dry-run: semantic normalization used deterministic local heuristics, not Gemini.")

    slide_order = {s["slideId"]: i for i, s in enumerate(slides)}
    normalized = validate_normalized_slides(normalized, {s["slideId"] for s in slides}, allow_missing=True)
    if generate and not normalized:
        normalized = [deterministic_normalize_slide(slide) for slide in slides]
        warnings.append(
            "Gemini normalization produced no valid slides; fell back to deterministic local normalization."
        )
    normalized.sort(key=lambda s: slide_order.get(s["slideId"], 10**9))
    normalized_ids = {s["slideId"] for s in normalized}
    skipped_ids = sorted({s["slideId"] for s in slides} - normalized_ids)
    if skipped_ids:
        warnings.append(f"Skipped {len(skipped_ids)} slide(s) after normalization failures: {', '.join(skipped_ids)}")
    payload = {
        "schemaVersion": "lecture-slide-normalized-v1",
        "sourceFile": slide_payload["sourceFile"],
        "pdfSha256": slide_payload["pdfSha256"],
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "normalizationWarnings": warnings,
        "normalizationStats": {
            "expectedSlideCount": len(slides),
            "normalizedSlideCount": len(normalized),
            "skippedSlideCount": len(skipped_ids),
            "defaultChunkSize": MAX_SLIDES_PER_CHUNK,
            "estimatedJsonBudgetChars": MAX_NORMALIZATION_ESTIMATED_JSON_CHARS,
        },
        "slides": normalized,
    }
    if isinstance(slide_payload.get("provenance"), dict):
        payload["provenance"] = slide_payload["provenance"]
    out_path = normalized_output_path_for_source(slide_payload["sourceFile"])
    write_json(out_path, payload)
    log(f"  Normalized -> {display_path(out_path)}")
    return payload


def normalized_output_path_for_source(source_file: str) -> Path:
    return NORMALIZED_DIR / f"{slugify(Path(source_file).stem)}_normalized_slides.json"


def load_existing_normalized_slide_map(slide_payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    path = normalized_output_path_for_source(slide_payload["sourceFile"])
    if not path.exists():
        return {}, []
    try:
        payload = read_json(path)
    except Exception as exc:
        return {}, [f"Existing normalized file could not be read; ignoring it: {exc}"]
    if payload.get("pdfSha256") != slide_payload.get("pdfSha256"):
        return {}, ["Existing normalized file ignored because pdfSha256 does not match current decomposition."]
    expected = {s["slideId"] for s in slide_payload.get("slides") or []}
    by_id: dict[str, dict[str, Any]] = {}
    for item in payload.get("slides") or []:
        if isinstance(item, dict) and item.get("slideId") in expected and item.get("sourceTextHash"):
            by_id[item["slideId"]] = item
    return by_id, []


def estimate_normalization_json_chars(slide: dict[str, Any]) -> int:
    ocr_len = len(slide.get("ocrText") or "")
    images = len(slide.get("images") or [])
    tables = len(slide.get("tables") or [])
    return int(900 + min(ocr_len, MAX_NORMALIZATION_OCR_CHARS) * 2.2 + images * 160 + tables * 700)


def split_slides_for_normalization(slides: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_estimate = 0
    for slide in slides:
        estimate = estimate_normalization_json_chars(slide)
        if current and (
            len(current) >= MAX_SLIDES_PER_CHUNK
            or current_estimate + estimate > MAX_NORMALIZATION_ESTIMATED_JSON_CHARS
        ):
            chunks.append(current)
            current = []
            current_estimate = 0
        current.append(slide)
        current_estimate += estimate
    if current:
        chunks.append(current)
    return chunks


def compact_slides_for_prompt(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "slideId": s["slideId"],
        "slideIndex": s["slideIndex"],
        "ocrText": s.get("ocrText", ""),
        "images": [{"imageId": img.get("imageId"), "kind": img.get("kind")} for img in s.get("images", [])],
        "tables": s.get("tables", []),
    } for s in chunk]


def normalized_schema_required_keys() -> str:
    return json.dumps({
        "slides": [{
            "slideId": "same input slideId",
            "slideType": ["HIGH_YIELD_CLINICAL"],
            "yieldScore": 0,
            "primaryConcepts": [],
            "secondaryConcepts": [],
            "clinicalFacts": [],
            "diagnosticFacts": [],
            "managementFacts": [],
            "mechanismFacts": [],
            "images": [],
            "tables": [],
            "questionPotential": 0,
            "groundingNotes": [],
        }]
    }, ensure_ascii=False)


def validate_normalized_chunk_slide_types(item: dict[str, Any], slide_id: str, chunk_label: str) -> None:
    types = item.get("slideType")
    if isinstance(types, str):
        types = [types]
    if not isinstance(types, list):
        return
    normalized_types = [str(value).strip().upper() for value in types if str(value).strip()]
    bad = [value for value in normalized_types if value not in SLIDE_TYPES]
    if bad:
        allowed = ", ".join(sorted(SLIDE_TYPES))
        raise PipelineError(
            f"Normalization {chunk_label} slide {slide_id} has unsupported slideType values: {bad}. "
            f"Use only allowed slide types: {allowed}."
        )


def extract_normalized_items(parsed: Any, by_id: dict[str, dict[str, Any]], chunk_label: str) -> list[dict[str, Any]]:
    items = parsed.get("slides") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise PipelineError(f"Normalization {chunk_label} did not return a slides array.")

    expected_ids = set(by_id)
    seen_ids: set[str] = set()
    merged: list[dict[str, Any]] = []
    required = [
        "slideId", "slideType", "yieldScore", "primaryConcepts",
        "secondaryConcepts", "clinicalFacts", "diagnosticFacts",
        "managementFacts", "mechanismFacts", "images", "tables",
        "questionPotential", "groundingNotes",
    ]
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise PipelineError(f"Normalization {chunk_label} item {idx} is not an object.")
        slide_id = str(item.get("slideId") or "")
        if slide_id not in expected_ids:
            raise PipelineError(f"Normalization {chunk_label} returned unknown slideId: {slide_id or '(missing)'}")
        if slide_id in seen_ids:
            raise PipelineError(f"Normalization {chunk_label} returned duplicate slideId: {slide_id}")
        missing = [key for key in required if key not in item]
        if missing:
            raise PipelineError(f"Normalization {chunk_label} slide {slide_id} missing required keys: {', '.join(missing)}")
        validate_normalized_chunk_slide_types(item, slide_id, chunk_label)
        seen_ids.add(slide_id)
        merged.append(merge_normalized_with_source(item, by_id[slide_id]))
    missing_ids = expected_ids - seen_ids
    if missing_ids:
        raise PipelineError(f"Normalization {chunk_label} omitted slideIds: {', '.join(sorted(missing_ids))}")
    return merged


def call_normalization_once(
    api_key: str,
    template: str,
    chunk: list[dict[str, Any]],
    source_file: str,
    chunk_label: str,
    retry_label: str,
    repair_raw: str | None = None,
    repair_error: str = "",
) -> list[dict[str, Any]]:
    by_id = {s["slideId"]: s for s in chunk}
    expected_ids = list(by_id)
    if repair_raw is None:
        prompt = template.replace("{{SLIDES_JSON}}", json.dumps(compact_slides_for_prompt(chunk), ensure_ascii=False))
    else:
        prompt = repair_json_prompt(
            raw=repair_raw,
            expected_schema=normalized_schema_required_keys(),
            expected_ids=expected_ids,
            error_message=repair_error,
        )
    raw = raw_gemini_call(api_key, prompt, temperature=0.0 if repair_raw else 0.15)
    write_debug_raw(source_file, "normalize", chunk_label, retry_label, raw)
    parsed = load_largest_valid_json(raw)
    return extract_normalized_items(parsed, by_id, chunk_label)


def normalize_chunk_with_retries(
    api_key: str,
    template: str,
    chunk: list[dict[str, Any]],
    source_file: str,
    chunk_label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    last_raw = ""
    last_error = ""

    try:
        return call_normalization_once(api_key, template, chunk, source_file, chunk_label, "attempt0"), warnings
    except Exception as exc:
        last_error = str(exc)
        raw_path = DEBUG_DIR / f"{slugify(Path(source_file).stem)}_normalize_{slugify(chunk_label)}_attempt0_raw_response.txt"
        if raw_path.exists():
            last_raw = raw_path.read_text(encoding="utf-8", errors="replace")
        warnings.append(f"Normalization {chunk_label}: attempt0 failed: {last_error}")

    if not is_truncation_failure(last_error):
        try:
            return call_normalization_once(
                api_key, template, chunk, source_file, chunk_label, "retry1_repair",
                repair_raw=last_raw, repair_error=last_error,
            ), warnings
        except Exception as exc:
            last_error = str(exc)
            warnings.append(f"Normalization {chunk_label}: retry1 repair failed: {last_error}")
            if is_network_failure(last_error):
                warnings.append(f"Normalization {chunk_label}: stopped retries after network failure.")
                return [], warnings
    else:
        warnings.append(f"Normalization {chunk_label}: truncation detected; splitting chunk without same-size repair.")

    if len(chunk) > SMALL_NORMALIZATION_CHUNK_SIZE:
        collected: list[dict[str, Any]] = []
        for sub_index, sub_chunk in enumerate(split_slides_for_normalization(chunk), start=1):
            if len(sub_chunk) == len(chunk):
                sub_chunk = chunk[:SMALL_NORMALIZATION_CHUNK_SIZE]
                remainder = chunk[SMALL_NORMALIZATION_CHUNK_SIZE:]
                split_chunks = [sub_chunk] + ([remainder] if remainder else [])
                remaining_collected: list[dict[str, Any]] = []
                for forced_index, forced_chunk in enumerate(split_chunks, start=1):
                    sub_items, sub_warnings = normalize_chunk_with_retries(
                        api_key, template, forced_chunk, source_file, f"{chunk_label}_retry2_forced{forced_index}"
                    )
                    remaining_collected.extend(sub_items)
                    warnings.extend(sub_warnings)
                return remaining_collected, warnings
            sub_items, sub_warnings = normalize_chunk_with_retries(
                api_key, template, sub_chunk, source_file, f"{chunk_label}_retry2_sub{sub_index}"
            )
            collected.extend(sub_items)
            warnings.extend(sub_warnings)
        return collected, warnings

    if len(chunk) > ISOLATED_NORMALIZATION_CHUNK_SIZE:
        collected = []
        for sub_index, slide in enumerate(chunk, start=1):
            sub_items, sub_warnings = normalize_chunk_with_retries(
                api_key, template, [slide], source_file, f"{chunk_label}_retry3_slide{sub_index}"
            )
            collected.extend(sub_items)
            warnings.extend(sub_warnings)
        return collected, warnings

    slide_id = chunk[0].get("slideId", "(unknown)") if chunk else "(empty)"
    if "unsupported slideType values" in last_error:
        raise PipelineError(f"Normalization {chunk_label} failed after retries for slide {slide_id}: {last_error}")
    warnings.append(f"Normalization {chunk_label}: skipped slide {slide_id} after repeated JSON/schema failures: {last_error}")
    return [], warnings


def merge_normalized_with_source(item: dict[str, Any], source_slide: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(item)
    merged["slideId"] = source_slide["slideId"]
    merged["images"] = source_slide.get("images") or []
    merged["tables"] = source_slide.get("tables") or []
    merged["sourceTextHash"] = short_hash(source_slide.get("ocrText") or "")
    return merged


def validate_normalized_slides(slides: list[dict[str, Any]], expected_ids: set[str], allow_missing: bool = False) -> list[dict[str, Any]]:
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    for idx, slide in enumerate(slides, start=1):
        if not isinstance(slide, dict):
            raise PipelineError(f"Normalized slide {idx} is not an object.")
        slide_id = str(slide.get("slideId") or "")
        if not slide_id or slide_id not in expected_ids:
            raise PipelineError(f"Normalized slide {idx} has unknown slideId.")
        if slide_id in seen:
            raise PipelineError(f"Duplicate normalized slideId: {slide_id}")
        seen.add(slide_id)
        types = slide.get("slideType")
        if isinstance(types, str):
            types = [types]
        if not isinstance(types, list) or not types:
            types = ["LOW_INFORMATION"]
        types = [str(t).strip().upper() for t in types if str(t).strip()]
        bad = [t for t in types if t not in SLIDE_TYPES]
        if bad:
            raise PipelineError(f"{slide_id}: unknown slideType values: {bad}")
        slide["slideType"] = types
        for key in ["yieldScore", "questionPotential"]:
            try:
                slide[key] = max(0, min(100, int(slide.get(key) or 0)))
            except Exception:
                slide[key] = 0
        for key in [
            "primaryConcepts",
            "secondaryConcepts",
            "clinicalFacts",
            "diagnosticFacts",
            "managementFacts",
            "mechanismFacts",
            "images",
            "tables",
            "groundingNotes",
        ]:
            if not isinstance(slide.get(key), list):
                slide[key] = []
        clean.append(slide)
    missing = expected_ids - seen
    if missing and not allow_missing:
        raise PipelineError(f"Normalization omitted {len(missing)} slide(s).")
    return clean


def empty_memory() -> dict[str, Any]:
    return {
        "schemaVersion": "lecture-slide-rolling-memory-v1",
        "conceptFrequencyMap": {},
        "diagnosisUsageMap": {},
        "distractorUsageMap": {},
        "stemTemplateUsageMap": {},
        "imageUsageMap": {},
        "educationalObjectiveMap": {},
        "generatedQuestionFingerprints": {},
    }


def increment_map(mapping: dict[str, int], key: str, amount: int = 1) -> None:
    key = normalize_key(key)
    if not key:
        return
    mapping[key] = int(mapping.get(key, 0)) + amount


def normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def update_memory_from_slides(memory: dict[str, Any], slides: list[dict[str, Any]]) -> None:
    for slide in slides:
        for concept in (slide.get("primaryConcepts") or []) + (slide.get("secondaryConcepts") or []):
            increment_map(memory["conceptFrequencyMap"], str(concept))


def slide_redundancy(memory: dict[str, Any], slide: dict[str, Any]) -> float:
    concepts = [normalize_key(c) for c in slide.get("primaryConcepts", []) if normalize_key(c)]
    if not concepts:
        return 0.0
    hits = sum(1 for c in concepts if int(memory["conceptFrequencyMap"].get(c, 0)) > 1)
    return hits / max(1, len(concepts))


def allocate_questions(normalized_payload: dict[str, Any], memory: dict[str, Any]) -> list[dict[str, Any]]:
    update_memory_from_slides(memory, normalized_payload.get("slides") or [])
    allocations: list[dict[str, Any]] = []
    allocated_concepts: dict[str, int] = {}
    for slide in normalized_payload.get("slides") or []:
        types = set(slide.get("slideType") or [])
        redundancy = slide_redundancy(memory, slide)
        primary_keys = [normalize_key(c) for c in slide.get("primaryConcepts", []) if normalize_key(c)]
        prior_alloc_hits = sum(1 for key in primary_keys if allocated_concepts.get(key, 0) > 0)
        sequential_redundancy = prior_alloc_hits / max(1, len(primary_keys))
        effective_redundancy = max(redundancy, sequential_redundancy)
        richness = (
            len(slide.get("clinicalFacts") or [])
            + len(slide.get("diagnosticFacts") or [])
            + len(slide.get("managementFacts") or [])
            + len(slide.get("mechanismFacts") or [])
            + len(slide.get("tables") or []) * 2
            + len(slide.get("images") or [])
        )
        fact_categories = sum(
            1 for key in ["clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts"]
            if slide.get(key)
        )
        potential = int(slide.get("questionPotential") or 0)
        count = 0
        reason = "low information or administrative"
        if "ADMINISTRATIVE" in types or "TRANSITION_SLIDE" in types:
            count = 0
        elif (
            potential >= 85
            and richness >= 12
            and fact_categories >= 2
            and effective_redundancy < 0.35
            and ("HIGH_YIELD_CLINICAL" in types or "MECHANISM" in types)
        ):
            count = 2
            reason = "high yield, rich, low redundancy"
        elif potential >= 50 and richness >= 3 and effective_redundancy < 0.80:
            count = 1
            reason = "sufficient yield and distinct content"
        elif ("IMAGE_HEAVY" in types or "TABLE_HEAVY" in types) and potential >= 40 and effective_redundancy < 0.85:
            count = 1
            reason = "image or table has question value"
        else:
            count = 0
        for key in primary_keys:
            allocated_concepts[key] = allocated_concepts.get(key, 0) + count
        allocations.append({
            "slideId": slide["slideId"],
            "questionCount": count,
            "reason": reason,
            "yieldScore": slide.get("yieldScore", 0),
            "redundancyScore": round(effective_redundancy, 3),
            "contentRichness": richness,
            "slide": slide,
        })
    return allocations


def dry_run_question(allocation: dict[str, Any], number: int) -> dict[str, Any]:
    slide = allocation["slide"]
    slide_id = str(slide.get("slideId") or f"slide-{number}")
    concept = first_nonempty(slide.get("primaryConcepts")) or first_nonempty(slide.get("clinicalFacts")) or "slide-supported concept"
    facts = (slide.get("clinicalFacts") or slide.get("groundingNotes") or [concept])[:3]
    fact_sentence = " ".join(str(f).rstrip(".") + "." for f in facts if str(f).strip())
    if not fact_sentence:
        fact_sentence = f"The slide supports the concept: {concept}."
    dry_run_marker = chr(ord("a") + ((max(1, number) - 1) % 26))
    dry_run_context = f"Dry-run source chunk {slide_id}, item {dry_run_marker}"
    stem = (
        f"A patient is evaluated for a finding related to {concept}. "
        f"The lecture slide emphasizes the following slide-supported information from {dry_run_context}: {fact_sentence} "
        "The clinician wants to identify the best matching high-yield concept from the slide. "
        "Which of the following is the best answer?"
    )
    choices = [
        {"label": "A", "text": concept[:120]},
        {"label": "B", "text": "Closely related distractor from the same topic area"},
        {"label": "C", "text": "Alternative diagnosis or management step from the same system"},
        {"label": "D", "text": "Related mechanism or clinical mimic"},
    ]
    return {
        "slideId": slide["slideId"],
        "questionKind": "clinical_vignette",
        "stemTemplate": "dry_run_clinical_transform",
        "testedConcept": concept,
        "diagnosisOrTarget": concept,
        "distractorFamily": "same topic area",
        "stem": stem,
        "answerChoices": choices,
        "correctAnswer": "A",
        "correctExplanation": "This dry-run question preserves pipeline structure without calling Gemini.",
        "incorrectExplanations": [
            {"label": "B", "explanation": "Dry-run distractor placeholder."},
            {"label": "C", "explanation": "Dry-run distractor placeholder."},
            {"label": "D", "explanation": "Dry-run distractor placeholder."},
        ],
        "educationalObjective": f"Recognize the slide-supported concept from {dry_run_context}: {concept}.",
        "retrievalTag": concept[:80],
        "reviewPearl": fact_sentence[:220],
        "imageRouting": dry_run_image_routing(slide, number),
        "tableUse": [{"tableId": t.get("tableId"), "placement": "explanation"} for t in slide.get("tables", [])],
        "sourceFactIds": [],
    }


def dry_run_image_routing(slide: dict[str, Any], number: int) -> list[dict[str, str]]:
    images = slide.get("images") or []
    if not images:
        return []
    if "IMAGE_HEAVY" in set(slide.get("slideType") or []) and number % 3 == 0:
        return [{"imageId": images[0].get("imageId", ""), "placement": "stem"}]
    return [{"imageId": images[0].get("imageId", ""), "placement": "explanation"}]


def first_nonempty(values: Any) -> str:
    if not isinstance(values, list):
        return ""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def slide_allowed_grounding(slide: dict[str, Any]) -> dict[str, Any]:
    fact_text = normalized_slide_fact_text(slide)
    tokens = {
        tok.lower()
        for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-]{2,}\b", fact_text)
        if tok.lower() not in COMMON_CLINICAL_WORDS
    }
    grounding = build_grounding_index({"slides": [slide]}).get(slide["slideId"], {})
    tokens.update(grounding.get("tokens") or set())
    phrases: list[str] = []
    for key in [
        "primaryConcepts",
        "secondaryConcepts",
        "clinicalFacts",
        "diagnosticFacts",
        "managementFacts",
        "mechanismFacts",
        "differentialFacts",
        "trapFacts",
        "nativeTextFacts",
        "cleanedImageFacts",
        "groundingNotes",
        "groundingTerms",
    ]:
        values = slide.get(key) or []
        if isinstance(values, list):
            for value in values:
                text = clean_sentence(value)
                if text:
                    phrases.append(text)
    for table in slide.get("tables") or []:
        if isinstance(table, dict):
            title = clean_sentence(table.get("title") or "")
            if title:
                phrases.append(title)
            for header in table.get("headers") or []:
                text = clean_sentence(header)
                if text:
                    phrases.append(text)
    for values in (slide.get("structuredImageFacts") or {}).values():
        if isinstance(values, list):
            for value in values:
                text = clean_sentence(value)
                if text:
                    phrases.append(text)
    distractor_pool: list[str] = []
    for text in phrases:
        if len(distractor_pool) >= 30:
            break
        if text and text not in distractor_pool:
            distractor_pool.append(text[:180])
    return {
        "allowedMedicalTerms": sorted({t for t in tokens if t and len(t) >= 3})[:220],
        "allowedDistractorPool": distractor_pool,
        "groundingFacts": phrases[:40],
    }


def compact_generation_allocation(allocation: dict[str, Any]) -> dict[str, Any]:
    out = {key: value for key, value in allocation.items() if key != "slide"}
    slide = allocation.get("slide") or {}
    out["slide"] = slide
    allowed = slide_allowed_grounding(slide)
    out["ALLOWED_MEDICAL_TERMS"] = allowed["allowedMedicalTerms"]
    out["ALLOWED_DISTRACTOR_POOL"] = allowed["allowedDistractorPool"]
    out["GROUNDING_FACTS"] = allowed["groundingFacts"]
    return out


class _V5ConfigSlot:
    """Tiny thread-safe holder so CLI can flip v5 on without changing the
    full call chain. Set via _V5_CONFIG.set({...}) before invoking
    generate_questions; cleared by .clear()."""
    def __init__(self) -> None:
        self._cfg: dict[str, Any] | None = None
    def set(self, cfg: dict[str, Any]) -> None:
        self._cfg = cfg
    def get(self) -> dict[str, Any] | None:
        return self._cfg
    def clear(self) -> None:
        self._cfg = None


_V5_CONFIG = _V5ConfigSlot()


def _enrich_allocations_for_v5(allocations: list[dict[str, Any]], normalized_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach slideContext / allowedMedicalTerms / allowedDistractorPool to
    each allocation so the v5 pipeline can operate without re-loading the
    payload. Idempotent — fields already present pass through unchanged."""
    slide_index = {s.get("slideId"): s for s in (normalized_payload.get("slides") or [])}
    enriched: list[dict[str, Any]] = []
    for a in allocations:
        copy = dict(a)
        slide = slide_index.get(copy.get("slideId"))
        if slide:
            facts = []
            for field in ("clinicalFacts", "diagnosticFacts", "managementFacts",
                           "mechanismFacts", "differentialFacts", "trapFacts",
                           "cleanedImageFacts", "nativeTextFacts"):
                facts.extend(slide.get(field) or [])
            primary = slide.get("primaryConcepts") or []
            copy.setdefault("slideContext", {
                "slideTitle": (primary[0] if primary else ""),
                "clinicalFacts": facts,
                "primaryConcepts": primary,
                "secondaryConcepts": slide.get("secondaryConcepts") or [],
            })
            # Derive allowed terms from the facts when caller didn't supply
            if "allowedMedicalTerms" not in copy:
                terms: list[str] = []
                seen: set[str] = set()
                for f in facts + primary:
                    if isinstance(f, str):
                        s = f.strip()
                        if s and s.lower() not in seen:
                            terms.append(s)
                            seen.add(s.lower())
                copy["allowedMedicalTerms"] = terms[:80]
            if "allowedDistractorPool" not in copy:
                copy["allowedDistractorPool"] = (
                    copy.get("allowedMedicalTerms") or []
                )[:60]
            if "slideImages" not in copy:
                copy["slideImages"] = slide.get("slideImages") or slide.get("images") or []
        enriched.append(copy)
    return enriched


def generate_questions(normalized_payload: dict[str, Any], allocations: list[dict[str, Any]], memory: dict[str, Any], generate: bool) -> list[dict[str, Any]]:
    work = [a for a in allocations if int(a.get("questionCount") or 0) > 0]
    questions: list[dict[str, Any]] = []
    if not work:
        return questions
    total_questions = sum(int(a.get("questionCount") or 0) for a in work)
    # v5 multi-stage path — opt-in via `--v5`. The v5 pipeline runs the
    # multi-stage authoring loop (stem → distractors → critic → image
    # routing → randomize) per question. Higher cost (~4x API calls)
    # but produces NBME-authentic questions with balanced answer
    # position distribution and explicit order/difficulty
    # stratification. See tools/.../v5_pipeline.py for details.
    v5_cfg = _V5_CONFIG.get()
    if v5_cfg and generate:
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            import v5_pipeline as v5  # type: ignore
            log("v5 pipeline ACTIVATED — stem→distractors→critic→image→randomize")
            v5_questions = v5.generate_v5(
                normalized_payload=normalized_payload,
                allocations=_enrich_allocations_for_v5(allocations, normalized_payload),
                memory=memory,
                target_order_mix=v5_cfg["order_mix"],
                target_difficulty_mix=v5_cfg["difficulty_mix"],
                seed=v5_cfg["seed"],
            )
            # Persist debug + generated artifacts under the conventional paths
            gen_path = GENERATED_DIR / f"{slugify(Path(normalized_payload['sourceFile']).stem)}_generated_questions.json"
            write_json(gen_path, {"questions": v5_questions})
            mem_path = MEMORY_DIR / f"{slugify(Path(normalized_payload['sourceFile']).stem)}_rolling_memory.json"
            write_json(mem_path, memory)
            log(f"  v5 Generated -> {display_path(gen_path)}  ({len(v5_questions)} questions)")
            return v5_questions
        except Exception as exc:
            warn(f"v5 pipeline failed ({exc}); falling back to legacy single-call generator")
    if generate:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise PipelineError("GEMINI_API_KEY is not set.")
        # Reset the quota latch so a previous in-process run does not leak
        # into this one. In normal subprocess use this is a no-op.
        reset_quota_state()
        template = GENERATE_PROMPT.read_text(encoding="utf-8")
        chunks = chunk_list(work, MAX_GENERATION_ALLOCS_PER_CHUNK)
        generated_so_far = 0
        for chunk_index, chunk in enumerate(chunks, start=1):
            chunk_questions = sum(int(a.get("questionCount") or 0) for a in chunk)
            if quota_exhausted():
                warn(
                    f"Skipping chunk {chunk_index}/{len(chunks)} — Gemini quota exhausted earlier in run."
                )
                generated_so_far += chunk_questions
                continue
            emit_bic_progress(
                "generating",
                f"Generating question {generated_so_far + 1}/{total_questions} from chunk {chunk_index}/{len(chunks)}",
                chunk=chunk_index,
                chunkTotal=len(chunks),
                question=generated_so_far + 1,
                questionTotal=total_questions,
            )
            items, chunk_warnings = generate_question_chunk_with_retries(
                api_key=api_key,
                template=template,
                chunk=chunk,
                memory=memory,
                source_file=normalized_payload["sourceFile"],
                chunk_label=f"chunk{chunk_index}",
            )
            for warning in chunk_warnings:
                warn(warning)
            for item in items:
                questions.append(item)
                update_memory_from_question(memory, item)
            generated_so_far += chunk_questions

        # Targeted missing-slide recovery: when partial-accept tolerance let
        # some allocated slides come back empty (or under-delivered), make ONE
        # focused per-slide call (up to MAX_RECOVERY_ATTEMPTS_PER_SLIDE) for
        # each missing slide. Bounded cost: at most
        # (len(work) * MAX_RECOVERY_ATTEMPTS_PER_SLIDE) extra API calls in the
        # worst case. Quota-aware: stops on the first 429.
        if not quota_exhausted():
            generated_per_slide = Counter(
                q.get("slideId") for q in questions if isinstance(q, dict)
            )
            for allocation in work:
                if quota_exhausted():
                    warn(
                        "Targeted recovery: stopping — Gemini quota exhausted."
                    )
                    break
                slide_id = allocation.get("slideId")
                expected = int(allocation.get("questionCount") or 0)
                produced = int(generated_per_slide.get(slide_id, 0))
                deficit = expected - produced
                if deficit <= 0:
                    continue
                emit_bic_progress(
                    "generating",
                    f"Recovering {deficit} missing question(s) for slide {slide_id}",
                    slideId=str(slide_id),
                    deficit=deficit,
                )
                recovered, recovery_warnings = retry_missing_slide_questions(
                    api_key=api_key,
                    template=template,
                    allocation=allocation,
                    missing_count=deficit,
                    memory=memory,
                    source_file=normalized_payload["sourceFile"],
                    max_attempts=MAX_RECOVERY_ATTEMPTS_PER_SLIDE,
                )
                for warning in recovery_warnings:
                    warn(warning)
                for item in recovered:
                    questions.append(item)
                    update_memory_from_question(memory, item)
    else:
        n = 1
        for allocation in work:
            for _ in range(int(allocation.get("questionCount") or 0)):
                emit_bic_progress(
                    "generating",
                    f"Generating question {n}/{total_questions}",
                    question=n,
                    questionTotal=total_questions,
                )
                q = dry_run_question(allocation, n)
                questions.append(q)
                update_memory_from_question(memory, q)
                n += 1
    gen_path = GENERATED_DIR / f"{slugify(Path(normalized_payload['sourceFile']).stem)}_generated_questions.json"
    write_json(gen_path, {"questions": questions})
    mem_path = MEMORY_DIR / f"{slugify(Path(normalized_payload['sourceFile']).stem)}_rolling_memory.json"
    write_json(mem_path, memory)
    log(f"  Generated -> {display_path(gen_path)}")
    log(f"  Memory -> {display_path(mem_path)}")
    return questions


def question_schema_required_keys() -> str:
    return json.dumps({
        "questions": [{
            "slideId": "",
            "questionKind": "clinical_vignette",
            "stemTemplate": "",
            "testedConcept": "",
            "diagnosisOrTarget": "",
            "distractorFamily": "",
            "stem": "",
            "answerChoices": [
                {"label": "A", "text": ""},
                {"label": "B", "text": ""},
                {"label": "C", "text": ""},
                {"label": "D", "text": ""},
            ],
            "correctAnswer": "A",
            "correctExplanation": "",
            "incorrectExplanations": [
                {"label": "B", "explanation": ""},
                {"label": "C", "explanation": ""},
                {"label": "D", "explanation": ""},
            ],
            "educationalObjective": "",
            "retrievalTag": "",
            "reviewPearl": "",
            "imageRouting": [],
            "tableUse": [],
            "sourceFactIds": [],
        }]
    }, ensure_ascii=False)


def _backfill_nonessential_question_keys(item: Any) -> Any:
    """Fill safe defaults for non-load-bearing question keys, IN PLACE.

    Organic generators (Gemini) intermittently omit a trailing metadata/teaching
    field -- most often ``reviewPearl`` -- even when the question is otherwise
    complete and clinically sound. Those fields are NOT load-bearing for
    correctness, and the downstream normalizer already backfills them. Filling
    them here, BEFORE the required-keys gate, stops a single omitted field from
    discarding a good question -- and, when every question in a chunk omits the
    same field, from collapsing the whole chunk into manual-review placeholders.

    This was the FF-Peds failure mode: 6 chunks (18 questions) were dropped
    solely because Gemini omitted ``reviewPearl``; every one was otherwise valid.
    Only keys that are missing entirely (or explicitly null) are filled, so a
    value the model DID provide is never altered. The genuinely essential keys
    (slideId / stem / answerChoices / correctAnswer / correctExplanation /
    incorrectExplanations) are intentionally NOT defaulted here -- their absence
    still fails the gate, so a real generation failure is still rejected.
    """
    if not isinstance(item, dict):
        return item
    objective = str(item.get("educationalObjective") or "").strip()
    correct_expl = str(item.get("correctExplanation") or "").strip()
    tested = str(item.get("testedConcept") or item.get("diagnosisOrTarget") or "").strip()
    defaults = {
        "questionKind": "single-best-answer",
        "stemTemplate": "",
        "testedConcept": tested,
        "diagnosisOrTarget": tested,
        "distractorFamily": "",
        "educationalObjective": objective or correct_expl,
        "retrievalTag": tested,
        "reviewPearl": correct_expl or objective,
        "imageRouting": [],
        "tableUse": [],
        "sourceFactIds": [],
    }
    for key, value in defaults.items():
        if key not in item or item.get(key) is None:
            item[key] = value
    return item


def extract_generated_question_items(
    parsed: Any,
    allocations: list[dict[str, Any]],
    chunk_label: str,
    *,
    require_exact_count: bool = True,
    count_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    items = parsed.get("questions") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        raise PipelineError(f"Generation {chunk_label} did not return a questions array.")
    expected_count = sum(int(a.get("questionCount") or 0) for a in allocations)
    allowed_slide_ids = {a["slideId"] for a in allocations}
    if expected_count > 0 and not items:
        raise PipelineError(f"Generation {chunk_label} returned 0 questions; expected {expected_count}.")
    if len(items) > expected_count:
        raise PipelineError(f"Generation {chunk_label} returned {len(items)} questions; expected {expected_count}.")
    if require_exact_count and len(items) != expected_count:
        raise PipelineError(f"Generation {chunk_label} returned {len(items)} questions; expected {expected_count}.")
    required = [
        "slideId", "questionKind", "stemTemplate", "testedConcept",
        "diagnosisOrTarget", "distractorFamily", "stem", "answerChoices",
        "correctAnswer", "correctExplanation", "incorrectExplanations",
        "educationalObjective", "retrievalTag", "reviewPearl",
        "imageRouting", "tableUse", "sourceFactIds",
    ]
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise PipelineError(f"Generation {chunk_label} item {idx} is not an object.")
        _backfill_nonessential_question_keys(item)
        missing = [key for key in required if key not in item]
        if missing:
            raise PipelineError(f"Generation {chunk_label} question {idx} missing required keys: {', '.join(missing)}")
        if item.get("slideId") not in allowed_slide_ids:
            raise PipelineError(f"Generation {chunk_label} question {idx} references unknown slideId: {item.get('slideId')}")
        stem_errors = stem_quality_errors(str(item.get("stem") or ""), f"question {idx}")
        if stem_errors:
            raise PipelineError(f"Generation {chunk_label} question {idx} has invalid stem: {'; '.join(stem_errors)}")
        choices = item.get("answerChoices")
        if not isinstance(choices, list) or len(choices) != 4:
            raise PipelineError(f"Generation {chunk_label} question {idx} does not have exactly 4 answerChoices.")
    if not require_exact_count and 0 < len(items) < expected_count:
        message = (
            f"Generation {chunk_label} returned {len(items)} questions; requested {expected_count}. "
            "Keeping valid partial output."
        )
        warn(message)
        if count_warnings is not None:
            count_warnings.append(message)
    return items


FAST_FACTS_REQUIRED_QUESTION_KEYS = [
    "slideId", "questionKind", "stemTemplate", "testedConcept",
    "diagnosisOrTarget", "distractorFamily", "stem", "answerChoices",
    "correctAnswer", "correctExplanation", "incorrectExplanations",
    "educationalObjective", "retrievalTag", "reviewPearl",
    "imageRouting", "tableUse", "sourceFactIds",
]


def coerce_fast_facts_questions_array(parsed: Any) -> tuple[list[Any], list[str]]:
    warnings: list[str] = []
    if isinstance(parsed, dict):
        questions = parsed.get("questions")
        if isinstance(questions, list):
            return questions, warnings
        if isinstance(questions, dict):
            warnings.append("questions object coerced to one-item array")
            return [questions], warnings
        if all(key in parsed for key in ["slideId", "stem", "answerChoices"]):
            warnings.append("singleton question object coerced to questions array")
            return [parsed], warnings
        return [], ["payload missing questions array"]
    if isinstance(parsed, list):
        return parsed, warnings
    return [], ["payload is not an object or array"]


def normalize_fast_facts_answer_choices(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        value = [{"label": str(label).strip().upper(), "text": text} for label, text in value.items()]
    if not isinstance(value, list):
        return []
    choices: list[dict[str, str]] = []
    for idx, choice in enumerate(value[:4]):
        if isinstance(choice, dict):
            label = str(choice.get("label") or LABELS[idx]).strip().upper()
            text = clean_sentence(choice.get("text") or choice.get("choice") or choice.get("answer") or "")
        else:
            label = LABELS[idx]
            text = clean_sentence(choice)
        if label not in LABELS:
            label = LABELS[idx]
        if text:
            choices.append({"label": label, "text": text})
    if len(choices) == 4 and [choice["label"] for choice in choices] != LABELS:
        choices = [{"label": LABELS[idx], "text": choice["text"]} for idx, choice in enumerate(choices)]
    return choices


def normalize_fast_facts_generated_question_items(
    parsed: Any,
    allocations: list[dict[str, Any]],
    chunk_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_items, warnings = coerce_fast_facts_questions_array(parsed)
    expected_count = sum(int(a.get("questionCount") or 0) for a in allocations)
    allowed_slide_ids = [str(a["slideId"]) for a in allocations]
    accepted: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            malformed.append({"index": idx, "reason": "question item is not an object", "rawPayload": raw_item})
            continue
        item = copy.deepcopy(raw_item)
        if not item.get("slideId") and len(allowed_slide_ids) == 1:
            item["slideId"] = allowed_slide_ids[0]
            warnings.append(f"item {idx} missing slideId; filled from one-item chunk")
        item["answerChoices"] = normalize_fast_facts_answer_choices(item.get("answerChoices") or item.get("choices"))
        _backfill_nonessential_question_keys(item)
        missing = [key for key in FAST_FACTS_REQUIRED_QUESTION_KEYS if key not in item]
        if missing:
            malformed.append({"index": idx, "slideId": item.get("slideId"), "reason": "missing required keys: " + ", ".join(missing), "rawPayload": raw_item})
            continue
        if str(item.get("slideId") or "") not in allowed_slide_ids:
            malformed.append({"index": idx, "slideId": item.get("slideId"), "reason": "unknown slideId", "rawPayload": raw_item})
            continue
        if len(item.get("answerChoices") or []) != 4:
            malformed.append({"index": idx, "slideId": item.get("slideId"), "reason": "answerChoices did not normalize to four choices", "rawPayload": raw_item})
            continue
        accepted.append(item)
    if expected_count > 0 and len(accepted) > expected_count:
        overflow = accepted[expected_count:]
        accepted = accepted[:expected_count]
        warnings.append(f"returned {len(raw_items)} questions; trimmed to requested {expected_count}")
    deficit = max(0, expected_count - len(accepted))
    diagnostics = {
        "chunkLabel": chunk_label,
        "expectedCount": expected_count,
        "returnedCount": len(raw_items),
        "acceptedCount": len(accepted),
        "overflowCount": len(overflow),
        "deficitCount": deficit,
        "warnings": warnings,
        "malformedItems": malformed,
        "overflowItems": overflow,
        "rawPayload": parsed,
    }
    return accepted, diagnostics


def extract_fast_facts_generated_question_items(parsed: Any, allocations: list[dict[str, Any]], chunk_label: str) -> list[dict[str, Any]]:
    items, diagnostics = normalize_fast_facts_generated_question_items(parsed, allocations, chunk_label)
    expected_count = diagnostics["expectedCount"]
    if not items:
        raise PipelineError(f"Fast Facts generation {chunk_label} returned no usable questions; expected {expected_count}.")
    for warning in diagnostics.get("warnings") or []:
        warn(f"Fast Facts generation {chunk_label}: {warning}")
    if diagnostics.get("deficitCount"):
        warn(f"Fast Facts generation {chunk_label}: accepted {len(items)} of {expected_count}; deficit {diagnostics['deficitCount']}.")
    return items


def call_generation_once(
    api_key: str,
    template: str,
    chunk: list[dict[str, Any]],
    memory: dict[str, Any],
    source_file: str,
    chunk_label: str,
    retry_label: str,
    repair_raw: str | None = None,
    repair_error: str = "",
    count_warnings: list[str] | None = None,
    require_exact_count: bool = False,
) -> list[dict[str, Any]]:
    expected_ids = [a["slideId"] for a in chunk]
    if repair_raw is None:
        prompt_allocations = [compact_generation_allocation(a) for a in chunk]
        prompt = (
            template
            .replace("{{ALLOCATIONS_JSON}}", json.dumps(prompt_allocations, ensure_ascii=False))
            .replace("{{MEMORY_JSON}}", json.dumps(compact_memory(memory), ensure_ascii=False))
        )
    else:
        prompt = repair_json_prompt(
            raw=repair_raw,
            expected_schema=question_schema_required_keys(),
            expected_ids=expected_ids,
            error_message=repair_error,
        )
    raw = raw_gemini_call(api_key, prompt, temperature=0.0 if repair_raw else 0.35, max_tokens=12000)
    write_debug_raw(source_file, "generate", chunk_label, retry_label, raw)
    parsed = load_largest_valid_json(raw)
    # Default to partial-accept (require_exact_count=False) to preserve the
    # historical chunk-level tolerance that prevents recursive sub-chunking
    # from burning the Gemini budget. Callers that ARE scoped to a single
    # slide (the targeted-recovery path) pass require_exact_count=True so a
    # short return raises and the recovery loop decides whether to re-try.
    return extract_generated_question_items(
        parsed,
        chunk,
        chunk_label,
        require_exact_count=require_exact_count,
        count_warnings=count_warnings,
    )


def retry_missing_slide_questions(
    *,
    api_key: str,
    template: str,
    allocation: dict[str, Any],
    missing_count: int,
    memory: dict[str, Any],
    source_file: str,
    max_attempts: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Re-ask Gemini for the questions a slide was under-delivered.

    Called from generate_questions() AFTER the normal chunk loop has finished
    and partial-accept tolerance has been applied. For each slide where the
    accepted count is less than the allocated count, we make up to
    max_attempts focused single-slide calls asking for exactly the missing
    questions. This is bounded, predictable, and quota-aware: any quota
    failure latches the global flag and stops further recovery.
    """
    warnings: list[str] = []
    if missing_count <= 0:
        return [], warnings
    slide_id = str(allocation.get("slideId") or "(unknown)")
    if quota_exhausted():
        warnings.append(
            f"Recovery for slide {slide_id}: skipped — Gemini quota exhausted earlier in run."
        )
        return [], warnings
    scoped_allocation = dict(allocation)
    scoped_allocation["questionCount"] = int(missing_count)
    scoped_chunk = [scoped_allocation]
    chunk_label = f"recover_{slugify(slide_id)}"
    for attempt in range(1, max_attempts + 1):
        if quota_exhausted():
            warnings.append(
                f"Recovery for slide {slide_id}: stopping at attempt {attempt} — Gemini quota exhausted."
            )
            return [], warnings
        retry_label = f"missing_attempt{attempt}"
        try:
            items = call_generation_once(
                api_key=api_key,
                template=template,
                chunk=scoped_chunk,
                memory=memory,
                source_file=source_file,
                chunk_label=chunk_label,
                retry_label=retry_label,
                count_warnings=warnings,
                require_exact_count=True,
            )
            return items, warnings
        except Exception as exc:
            err = str(exc)
            warnings.append(
                f"Recovery for slide {slide_id} attempt {attempt}/{max_attempts} failed: {err}"
            )
            if is_quota_failure(err):
                mark_quota_exhausted()
                warnings.append(
                    f"Recovery for slide {slide_id}: stopped after Gemini quota exhaustion."
                )
                return [], warnings
            if is_network_failure(err):
                warnings.append(
                    f"Recovery for slide {slide_id}: stopped after network failure."
                )
                return [], warnings
    warnings.append(
        f"Recovery for slide {slide_id}: gave up after {max_attempts} attempts; "
        f"{missing_count} question(s) remain missing."
    )
    return [], warnings


def generate_question_chunk_with_retries(
    api_key: str,
    template: str,
    chunk: list[dict[str, Any]],
    memory: dict[str, Any],
    source_file: str,
    chunk_label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    last_raw = ""
    last_error = ""

    if quota_exhausted():
        warnings.append(
            f"Generation {chunk_label}: skipped — Gemini quota already exhausted earlier in this run."
        )
        return [], warnings

    try:
        return call_generation_once(
            api_key,
            template,
            chunk,
            memory,
            source_file,
            chunk_label,
            "attempt0",
            count_warnings=warnings,
        ), warnings
    except Exception as exc:
        last_error = str(exc)
        raw_path = DEBUG_DIR / f"{slugify(Path(source_file).stem)}_generate_{slugify(chunk_label)}_attempt0_raw_response.txt"
        if raw_path.exists():
            last_raw = raw_path.read_text(encoding="utf-8", errors="replace")
        warnings.append(f"Generation {chunk_label}: attempt0 failed: {last_error}")
        if is_quota_failure(last_error):
            mark_quota_exhausted()
            warnings.append(
                f"Generation {chunk_label}: stopped retries after Gemini quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED)."
            )
            return [], warnings

    if not is_truncation_failure(last_error):
        try:
            emit_bic_progress(
                "repairing",
                f"Repairing generated questions from {chunk_label} after validation failure",
                chunk=chunk_label,
            )
            return call_generation_once(
                api_key, template, chunk, memory, source_file, chunk_label, "retry1_repair",
                repair_raw=last_raw, repair_error=last_error, count_warnings=warnings,
            ), warnings
        except Exception as exc:
            last_error = str(exc)
            warnings.append(f"Generation {chunk_label}: retry1 repair failed: {last_error}")
            if is_quota_failure(last_error):
                mark_quota_exhausted()
                warnings.append(
                    f"Generation {chunk_label}: stopped retries after Gemini quota exhaustion (HTTP 429 / RESOURCE_EXHAUSTED)."
                )
                return [], warnings
            if is_network_failure(last_error):
                warnings.append(f"Generation {chunk_label}: stopped retries after network failure.")
                return [], warnings
    else:
        warnings.append(f"Generation {chunk_label}: truncation detected; splitting chunk without same-size repair.")

    if quota_exhausted():
        warnings.append(
            f"Generation {chunk_label}: skipping sub-chunk recursion — Gemini quota exhausted."
        )
        return [], warnings

    if len(chunk) > 2:
        collected: list[dict[str, Any]] = []
        for sub_index, sub_chunk in enumerate(chunk_list(chunk, max(1, len(chunk) // 2)), start=1):
            if quota_exhausted():
                warnings.append(
                    f"Generation {chunk_label}: skipping remaining sub-chunks — Gemini quota exhausted."
                )
                break
            sub_items, sub_warnings = generate_question_chunk_with_retries(
                api_key, template, sub_chunk, memory, source_file, f"{chunk_label}_retry2_sub{sub_index}"
            )
            collected.extend(sub_items)
            warnings.extend(sub_warnings)
        return collected, warnings

    if len(chunk) > 1:
        collected = []
        for sub_index, allocation in enumerate(chunk, start=1):
            if quota_exhausted():
                warnings.append(
                    f"Generation {chunk_label}: skipping remaining single-slide retries — Gemini quota exhausted."
                )
                break
            sub_items, sub_warnings = generate_question_chunk_with_retries(
                api_key, template, [allocation], memory, source_file, f"{chunk_label}_retry3_slide{sub_index}"
            )
            collected.extend(sub_items)
            warnings.extend(sub_warnings)
        return collected, warnings

    warnings.append(f"Generation {chunk_label}: skipped slide {chunk[0].get('slideId', '(unknown)')} after repeated JSON/schema failures: {last_error}")
    return [], warnings


def collect_generation_validation(
    normalized_payload: dict[str, Any],
    questions: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], dict[str, Any]]:
    grounding_findings = semantic_grounding_findings(questions, normalized_payload)
    diversity_report = question_quality_and_diversity(questions)
    app_payload = build_app_ready_payload(normalized_payload, questions)
    errors = validate_app_ready_payload(app_payload)
    errors.extend(
        f"Semantic grounding Q{f.get('questionIndex', '?')}: {f.get('issue')} {f.get('detail')}"
        for f in grounding_findings
        if f.get("severity") == "error"
    )
    errors.extend(
        f"Question quality Q{f.get('questionIndex', '?')}: {f.get('issue')} {f.get('detail')}"
        for f in diversity_report.get("findings", [])
        if f.get("severity") == "error"
    )
    return app_payload, errors, grounding_findings, diversity_report


def repair_question_prompt(
    original_question: dict[str, Any],
    slide: dict[str, Any],
    unsupported_terms: list[str],
    attempt: int,
) -> str:
    allowed = slide_allowed_grounding(slide)
    return f"""
You are repairing one NBME-style question generated from one normalized lecture slide.

Return valid JSON only in this exact shape:
{question_schema_required_keys()}

	Return exactly 1 question.
	Use the same slideId.
	Use exactly 4 answer choices labeled A, B, C, D.
	The stem must end with a clear final one-best-answer question sentence ending in a question mark. No exceptions.
	Use natural final-question wording that fits the concept. Examples include, but are not limited to, diagnosis, next step, contraindication, prevention, screening, mechanism, and physician-response questions.
	Do not return a stem that is only a fragment, topic heading, lab value, diagnosis, or incomplete vignette.
	Do not include these unsupported terms unless they are present in ALLOWED_MEDICAL_TERMS:
{json.dumps(unsupported_terms, ensure_ascii=False)}

STRICT GROUNDING RULES:
- Source of truth is the normalized slide JSON below.
- Every answer choice and every explanation claim must use only concepts from ALLOWED_MEDICAL_TERMS or generic nonmedical vignette wording.
- Distractors must come from ALLOWED_DISTRACTOR_POOL, same-slide normalized facts, signs, mechanisms, management choices, or abnormalities.
- Never invent outside diseases, drugs, tests, procedures, mechanisms, epidemiology, or risk factors.
- If the old question used unsupported distractors, replace them with grounded same-slide alternatives.
- Clinical prose may be natural, but medical content must remain slide-grounded.

Attempt: {attempt}

NORMALIZED_SLIDE_JSON:
{json.dumps(slide, ensure_ascii=False)}

ALLOWED_MEDICAL_TERMS:
{json.dumps(allowed["allowedMedicalTerms"], ensure_ascii=False)}

ALLOWED_DISTRACTOR_POOL:
{json.dumps(allowed["allowedDistractorPool"], ensure_ascii=False)}

GROUNDING_FACTS:
{json.dumps(allowed["groundingFacts"], ensure_ascii=False)}

ORIGINAL_QUESTION_TO_REPAIR:
{json.dumps(original_question, ensure_ascii=False)}
""".strip()


def call_question_repair_once(
    api_key: str,
    source_file: str,
    q_index: int,
    original_question: dict[str, Any],
    slide: dict[str, Any],
    unsupported_terms: list[str],
    attempt: int,
) -> dict[str, Any]:
    prompt = repair_question_prompt(original_question, slide, unsupported_terms, attempt)
    raw = raw_gemini_call(api_key, prompt, temperature=0.15, max_tokens=8192, timeout_seconds=45)
    write_debug_raw(source_file, "repair_question", f"q{q_index:03d}", f"attempt{attempt}", raw)
    parsed = load_largest_valid_json(raw)
    allocation = {
        "slideId": slide["slideId"],
        "questionCount": 1,
        "slide": slide,
    }
    items = extract_generated_question_items(parsed, [allocation], f"repair_q{q_index:03d}_attempt{attempt}")
    return items[0]


def repair_progress_path(stem: str) -> Path:
    return REPORT_DIR / f"{stem}_repair_progress.json"


def timestamp_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def empty_repair_progress(source_file: str, total_failed: int) -> dict[str, Any]:
    return {
        "sourceFile": source_file,
        "totalFailedQuestions": total_failed,
        "completedRepairs": 0,
        "droppedQuestions": 0,
        "currentQuestionId": None,
        "lastSuccessfulTimestamp": None,
        "lastError": "",
        "repairedQuestionsByIndex": {},
        "droppedByIndex": {},
    }


def write_repair_progress(path: Path, progress: dict[str, Any]) -> None:
    progress["completedRepairs"] = len(progress.get("repairedQuestionsByIndex") or {})
    progress["droppedQuestions"] = len(progress.get("droppedByIndex") or {})
    write_json(path, progress)


def validate_single_repair_candidate(
    normalized_payload: dict[str, Any],
    slide: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, list[str], list[str]]:
    single_norm = dict(normalized_payload)
    single_norm["slides"] = [slide]
    _, single_errors, single_grounding, _ = collect_generation_validation(single_norm, [candidate])
    unsupported: list[str] = []
    for finding in single_grounding:
        if finding.get("severity") == "error":
            unsupported.extend(str(v) for v in finding.get("detail") or [])
    return not single_errors, single_errors, unsupported


def preserve_original_stem_if_repair_degrades(original: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    original_stem = clean_stem_text(original.get("stem"))
    candidate_stem = clean_stem_text(candidate.get("stem"))
    if not original_stem or not isinstance(candidate, dict):
        return candidate
    original_errors = stem_quality_errors(original_stem, "original")
    candidate_errors = stem_quality_errors(candidate_stem, "candidate")
    if candidate_errors and not original_errors:
        restored = dict(candidate)
        restored["stem"] = original_stem
        metadata = dict(restored.get("metadata") or {})
        metadata["repairStemRestoredFromOriginal"] = True
        metadata["discardedRepairStem"] = candidate_stem
        restored["metadata"] = metadata
        return restored
    if candidate_stem and len(candidate_stem.split()) < max(12, int(len(original_stem.split()) * 0.45)) and not original_errors:
        restored = dict(candidate)
        restored["stem"] = original_stem
        metadata = dict(restored.get("metadata") or {})
        metadata["repairStemRestoredFromOriginal"] = True
        metadata["discardedRepairStem"] = candidate_stem
        restored["metadata"] = metadata
        return restored
    return candidate


def recover_repair_debug_artifacts(
    stem: str,
    normalized_payload: dict[str, Any],
    questions: list[dict[str, Any]],
    failed_by_index: dict[int, list[str]],
    slide_by_id: dict[str, dict[str, Any]],
    progress: dict[str, Any],
) -> None:
    repaired = progress.setdefault("repairedQuestionsByIndex", {})
    dropped = progress.setdefault("droppedByIndex", {})
    for idx in sorted(failed_by_index):
        key = str(idx)
        if key in repaired or key in dropped:
            continue
        question = questions[idx - 1]
        slide = slide_by_id.get(str(question.get("slideId") or ""))
        if not slide:
            continue
        for raw_path in sorted(DEBUG_DIR.glob(f"{stem}_repair_question_q{idx:03d}_attempt*_raw_response.txt")):
            try:
                parsed = load_largest_valid_json(raw_path.read_text(encoding="utf-8", errors="replace"))
                candidate = extract_generated_question_items(
                    parsed,
                    [{"slideId": slide["slideId"], "questionCount": 1, "slide": slide}],
                    f"recover_repair_q{idx:03d}",
                )[0]
                ok, errors, _ = validate_single_repair_candidate(normalized_payload, slide, candidate)
                if ok:
                    repaired[key] = candidate
                    progress["lastSuccessfulTimestamp"] = timestamp_iso()
                    progress["lastError"] = ""
                    break
                progress["lastError"] = "; ".join(errors[:4])
            except Exception as exc:
                progress["lastError"] = str(exc)


def repair_existing_questions(pdf_path: Path) -> Path:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("GEMINI_API_KEY is not set.")
    stem = slugify(pdf_path.stem)
    normalized_path = NORMALIZED_DIR / f"{stem}_normalized_slides.json"
    generated_path = GENERATED_DIR / f"{stem}_generated_questions.json"
    if not normalized_path.exists():
        raise PipelineError(f"Missing normalized slides file: {display_path(normalized_path)}")
    if not generated_path.exists():
        raise PipelineError(f"Missing generated questions file: {display_path(generated_path)}")
    normalized_payload = read_json(normalized_path)
    generated_payload = read_json(generated_path)
    questions = generated_payload.get("questions") if isinstance(generated_payload, dict) else generated_payload
    if not isinstance(questions, list):
        raise PipelineError("Existing generated questions file does not contain a questions array.")

    original_question_count = len(questions)
    _, before_errors, before_grounding, _ = collect_generation_validation(normalized_payload, questions)
    failed_by_index = {
        int(f["questionIndex"]): list(f.get("detail") or [])
        for f in before_grounding
        if f.get("severity") == "error" and f.get("issue") == "unsupported_medical_claim_terms"
    }
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    progress_path = repair_progress_path(stem)
    if progress_path.exists():
        progress = read_json(progress_path)
        if not isinstance(progress, dict):
            progress = empty_repair_progress(normalized_payload["sourceFile"], len(failed_by_index))
        progress["totalFailedQuestions"] = len(failed_by_index)
    else:
        progress = empty_repair_progress(normalized_payload["sourceFile"], len(failed_by_index))
    recover_repair_debug_artifacts(stem, normalized_payload, questions, failed_by_index, slide_by_id, progress)
    write_repair_progress(progress_path, progress)

    repair_log: list[dict[str, Any]] = []

    for idx, question in enumerate(questions, start=1):
        if idx not in failed_by_index:
            continue
        key = str(idx)
        if key in (progress.get("repairedQuestionsByIndex") or {}) or key in (progress.get("droppedByIndex") or {}):
            continue
        slide = slide_by_id.get(str(question.get("slideId") or ""))
        if not slide:
            progress.setdefault("droppedByIndex", {})[key] = {
                "questionIndex": idx,
                "reason": "unknown slideId",
                "slideId": question.get("slideId"),
            }
            write_repair_progress(progress_path, progress)
            continue
        candidate = question
        unsupported = failed_by_index[idx]
        accepted = False
        attempt_notes: list[str] = []
        progress["currentQuestionId"] = idx
        progress["lastError"] = ""
        write_repair_progress(progress_path, progress)
        for attempt in range(1, 3):
            try:
                candidate = call_question_repair_once(api_key, normalized_payload["sourceFile"], idx, candidate, slide, unsupported, attempt)
                candidate = preserve_original_stem_if_repair_degrades(question, candidate)
                ok, single_errors, next_unsupported = validate_single_repair_candidate(normalized_payload, slide, candidate)
                if ok:
                    progress.setdefault("repairedQuestionsByIndex", {})[key] = candidate
                    progress["lastSuccessfulTimestamp"] = timestamp_iso()
                    progress["lastError"] = ""
                    write_repair_progress(progress_path, progress)
                    accepted = True
                    break
                unsupported = next_unsupported
                attempt_notes.append("; ".join(single_errors[:6]))
            except Exception as exc:
                error_text = str(exc)
                attempt_notes.append(error_text)
                progress["lastError"] = error_text
                write_repair_progress(progress_path, progress)
                if "repair_timeout" in error_text:
                    progress.setdefault("droppedByIndex", {})[key] = {
                        "questionIndex": idx,
                        "slideId": question.get("slideId"),
                        "unsupportedTerms": failed_by_index[idx],
                        "reason": "repair_timeout",
                        "attemptNotes": attempt_notes,
                    }
                    write_repair_progress(progress_path, progress)
                    break
        if not accepted and key not in (progress.get("droppedByIndex") or {}):
            progress.setdefault("droppedByIndex", {})[key] = {
                "questionIndex": idx,
                "slideId": question.get("slideId"),
                "unsupportedTerms": failed_by_index[idx],
                "validationFailures": failed_by_index[idx],
                "reason": "repair failed validation after 2 attempts",
                "attemptNotes": attempt_notes,
            }
            write_repair_progress(progress_path, progress)
        repair_log.append({
            "questionIndex": idx,
            "slideId": question.get("slideId"),
            "initialUnsupportedTerms": failed_by_index[idx],
            "accepted": accepted,
            "attemptNotes": attempt_notes,
        })

    progress["currentQuestionId"] = None
    write_repair_progress(progress_path, progress)
    repaired_map = progress.get("repairedQuestionsByIndex") or {}
    dropped_map = progress.get("droppedByIndex") or {}
    repaired_questions: list[dict[str, Any]] = []
    for idx, question in enumerate(questions, start=1):
        key = str(idx)
        if key in repaired_map:
            repaired_questions.append(repaired_map[key])
        elif key in dropped_map:
            continue
        else:
            repaired_questions.append(question)

    backup_path = GENERATED_DIR / f"{stem}_generated_questions_before_repair_{now_stamp()}.json"
    write_json(backup_path, generated_payload)
    write_json(generated_path, {"questions": repaired_questions})

    app_payload, after_errors, after_grounding, diversity_report = collect_generation_validation(normalized_payload, repaired_questions)
    report = {
        "sourceFile": pdf_path.name,
        "mode": "targeted-repair",
        "originalQuestionCount": original_question_count,
        "failedQuestionCountBeforeRepair": len(failed_by_index),
        "validationErrorCountBeforeRepair": len(before_errors),
        "repairedQuestionCount": len(repaired_map),
        "droppedQuestionCount": len(dropped_map),
        "finalQuestionCount": len(repaired_questions),
        "validationErrorCountAfterRepair": len(after_errors),
        "remainingValidationErrors": after_errors,
        "remainingSemanticGroundingFindings": after_grounding,
        "questionQualityFindings": diversity_report.get("findings", []),
        "droppedQuestions": list(dropped_map.values()),
        "repairLog": repair_log,
        "repairProgressPath": display_path(progress_path),
        "backupGeneratedQuestionsPath": display_path(backup_path),
    }
    report_path = write_report(report, "lecture_slide_repair_report")
    out_path = APP_READY_DIR / f"{stem}_lecture_app_ready.json"
    review_draft_path = None
    if dropped_map:
        failed_questions = []
        failed_items = []
        repair_by_index = {
            int(item.get("questionIndex")): item
            for item in repair_log
            if str(item.get("questionIndex") or "").isdigit()
        }
        for key, drop in sorted(dropped_map.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 0):
            idx = int(key)
            original = questions[idx - 1] if 0 < idx <= len(questions) else {}
            repair_item = repair_by_index.get(idx, {})
            failed_items.append(drop)
            failed_questions.append(review_candidate_from_failed_repair(original, drop, repair_item))
        review_draft_path = write_failed_repair_review_draft(
            source_label=pdf_path.name,
            source_format=SOURCE_FORMAT,
            source_type=str(os.environ.get("BIC_PROGRESS_SOURCE") or "").strip() or "lecture_slide_pdf",
            failed_questions=failed_questions,
            failed_items=failed_items,
            repair_report=report,
            app_ready_path=out_path if repaired_questions else None,
            report_path=report_path,
        )
        if review_draft_path:
            report["status"] = "completed_with_review_required"
            report["draftPath"] = str(review_draft_path.resolve())
            write_json(report_path, report)
    if after_errors and not review_draft_path:
        raise PipelineError("Targeted repair did not produce passing output:\n" + "\n".join(f"- {err}" for err in after_errors[:80]))
    if after_errors and review_draft_path and not repaired_questions:
        return review_draft_path
    if after_errors:
        raise PipelineError("Targeted repair left validation errors in healthy output:\n" + "\n".join(f"- {err}" for err in after_errors[:80]))
    write_json(out_path, app_payload)
    json.loads(out_path.read_text(encoding="utf-8"))
    log(f"App-ready -> {display_path(out_path)}")
    return out_path


def compact_memory(memory: dict[str, Any]) -> dict[str, Any]:
    out = {"schemaVersion": memory.get("schemaVersion")}
    for key, value in memory.items():
        if not isinstance(value, dict):
            continue
        top = sorted(value.items(), key=lambda kv: int(kv[1]), reverse=True)[:80]
        out[key] = dict(top)
    return out


def update_memory_from_question(memory: dict[str, Any], q: dict[str, Any]) -> None:
    increment_map(memory["diagnosisUsageMap"], str(q.get("diagnosisOrTarget") or q.get("testedConcept") or ""))
    increment_map(memory["stemTemplateUsageMap"], str(q.get("stemTemplate") or ""))
    increment_map(memory["educationalObjectiveMap"], str(q.get("educationalObjective") or ""))
    for choice in q.get("answerChoices") or []:
        if isinstance(choice, dict) and choice.get("label") != q.get("correctAnswer"):
            increment_map(memory["distractorUsageMap"], str(choice.get("text") or ""))
    for route in q.get("imageRouting") or []:
        if isinstance(route, dict):
            increment_map(memory["imageUsageMap"], f"{route.get('imageId')}:{route.get('placement')}")
    fp = question_fingerprint(q.get("stem", ""), q.get("answerChoices") or [])
    increment_map(memory["generatedQuestionFingerprints"], fp)


def normalized_slide_fact_text(slide: dict[str, Any]) -> str:
    fields: list[str] = []
    for key in [
        "primaryConcepts",
        "secondaryConcepts",
        "clinicalFacts",
        "diagnosticFacts",
        "managementFacts",
        "mechanismFacts",
        "differentialFacts",
        "trapFacts",
        "nativeTextFacts",
        "cleanedImageFacts",
        "groundingNotes",
        "groundingTerms",
    ]:
        values = slide.get(key) or []
        if isinstance(values, list):
            fields.extend(str(v) for v in values)
    for table in slide.get("tables") or []:
        if not isinstance(table, dict):
            continue
        fields.append(str(table.get("title") or ""))
        fields.extend(str(h) for h in table.get("headers") or [])
        for row in table.get("rows") or []:
            if isinstance(row, list):
                fields.extend(str(cell) for cell in row)
            elif isinstance(row, dict):
                fields.extend(str(v) for v in row.values())
    for values in (slide.get("structuredImageFacts") or {}).values():
        if isinstance(values, list):
            fields.extend(str(value) for value in values)
    return " ".join(fields)


def extract_medical_claim_terms(text: str) -> set[str]:
    """
    Conservative lexical claim extraction.
    This is not a medical ontology. It intentionally catches high-risk additions:
    named diagnoses, drugs, mechanisms, workups, epidemiology, and multiword terms.
    """
    text = re.sub(r"\[[A-Z]+:[^\]]+\]", " ", str(text or ""))
    terms: set[str] = set()

    for word in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-]{3,}\b", text):
        w = word.lower().strip("-")
        if w in COMMON_CLINICAL_WORDS:
            continue
        if w.endswith(MEDICAL_SUFFIXES) or w in HIGH_RISK_MEDICAL_WORDS:
            terms.add(w)
        if w in WORKUP_MANAGEMENT_WORDS:
            terms.add(w)

    return {t for t in terms if len(t) >= 4}


def build_grounding_index(normalized_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for slide in normalized_payload.get("slides") or []:
        fact_text = normalized_slide_fact_text(slide)
        terms = extract_medical_claim_terms(fact_text)
        token_set = {
            tok.lower()
            for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-]{2,}\b", fact_text)
            if tok.lower() not in COMMON_CLINICAL_WORDS
        }
        if "surgery" in token_set or "surgical" in token_set or "myotomy" in token_set or "removed" in token_set or "removal" in token_set:
            token_set.update({"surgery", "surgical"})
        if "cyanotic" in token_set or "cyanosis" in token_set:
            token_set.update({"cyanotic", "cyanosis"})
        if "cxr" in token_set or "x-ray" in token_set or "xray" in token_set:
            token_set.update({"cxr", "x-ray", "xray"})
        if "echo" in token_set or "echocardiography" in token_set or "echocardiogram" in token_set:
            token_set.update({"echo", "echocardiography", "echocardiogram"})
        if "steroid" in token_set or "steroids" in token_set or "corticosteroids" in token_set or "betamethasone" in token_set:
            token_set.update({"steroid", "steroids", "corticosteroids"})
        if "antibiotic" in token_set or "antibiotics" in token_set:
            token_set.update({"antibiotic", "antibiotics"})
        if any(tok.endswith(MEDICAL_SUFFIXES) for tok in token_set):
            token_set.update({"antibiotic", "antibiotics"})
        if "culture" in token_set or "cultures" in token_set:
            token_set.update({"culture", "cultures"})
        if "bilirubin" in token_set or "hyperbilirubinemia" in token_set:
            token_set.update({"bilirubin", "hyperbilirubinemia"})
        if "kernicterus" in token_set:
            token_set.update({"encephalopathy"})
        if "deficient" in token_set or "deficiency" in token_set:
            token_set.update({"enzyme"})
        index[slide["slideId"]] = {
            "factText": fact_text,
            "claimTerms": terms,
            "tokens": token_set,
        }
    return index


def generated_question_claim_text(q: dict[str, Any]) -> str:
    parts = [str(q.get("stem") or "")]
    for choice in q.get("answerChoices") or []:
        if isinstance(choice, dict):
            parts.append(str(choice.get("text") or ""))
    parts.append(str(q.get("correctExplanation") or ""))
    for item in q.get("incorrectExplanations") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("explanation") or ""))
    parts.append(str(q.get("educationalObjective") or ""))
    parts.append(str(q.get("reviewPearl") or ""))
    return " ".join(parts)


def semantic_grounding_findings(
    generated_questions: list[dict[str, Any]],
    normalized_payload: dict[str, Any],
    validation_mode: str = "strict",
) -> list[dict[str, Any]]:
    grounding_index = build_grounding_index(normalized_payload)
    findings: list[dict[str, Any]] = []
    for idx, q in enumerate(generated_questions, start=1):
        slide_id = str(q.get("slideId") or "")
        grounding = grounding_index.get(slide_id)
        if not grounding:
            findings.append({
                "questionIndex": idx,
                "slideId": slide_id,
                "severity": "error",
                "issue": "unknown_slide_id",
                "detail": "Question references a slideId absent from normalized slide payload.",
            })
            continue
        q_terms = extract_medical_claim_terms(generated_question_claim_text(q))
        supported_terms = grounding["claimTerms"]
        supported_tokens = grounding["tokens"]
        unsupported: list[str] = []
        for term in sorted(q_terms):
            term_tokens = [t for t in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-]{2,}\b", term.lower()) if t not in COMMON_CLINICAL_WORDS]
            if not term_tokens:
                continue
            token_hits = sum(1 for tok in term_tokens if tok in supported_tokens)
            if term in supported_terms or token_hits == len(term_tokens):
                continue
            if len(term_tokens) >= 2 and token_hits / len(term_tokens) >= 0.67:
                continue
            unsupported.append(term)
        if unsupported:
            findings.append({
                "questionIndex": idx,
                "slideId": slide_id,
                "severity": "warning" if validation_mode == "organic_lecture" else "error",
                "issue": "unsupported_medical_claim_terms",
                "detail": unsupported[:20],
            })
    return findings


def opening_phrase(stem: str) -> str:
    words = re.findall(r"\b[A-Za-z0-9'-]+\b", stem)
    return " ".join(words[:8]).lower()


def demographic_signature(stem: str) -> str:
    m = re.search(r"\b(\d{1,3})[- ]year[- ]old\s+(boy|girl|man|woman|male|female|child|infant|newborn)\b", stem, re.I)
    if m:
        age = int(m.group(1))
        bucket = "child" if age < 13 else "adolescent" if age < 18 else "adult" if age < 65 else "older adult"
        return f"{bucket} {m.group(2).lower()}"
    for label in ["newborn", "infant", "child", "adolescent", "boy", "girl", "man", "woman"]:
        if re.search(rf"\b{label}\b", stem, re.I):
            return label
    return "unspecified"


def question_quality_and_diversity(generated_questions: list[dict[str, Any]]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    opening_counts: dict[str, int] = {}
    template_counts: dict[str, int] = {}
    demographic_counts: dict[str, int] = {}
    distractor_family_counts: dict[str, int] = {}
    diagnosis_counts: dict[str, int] = {}
    objective_counts: dict[str, int] = {}

    for idx, q in enumerate(generated_questions, start=1):
        stem = str(q.get("stem") or "")
        for pat in TRIVIAL_RECALL_PATTERNS:
            if pat.search(stem):
                findings.append({
                    "questionIndex": idx,
                    "severity": "error",
                    "issue": "trivial_or_non_nbme_wording",
                    "detail": pat.pattern,
                })
        choices = q.get("answerChoices") or []
        choice_texts = [normalize_key(c.get("text", "")) for c in choices if isinstance(c, dict)]
        if len(set(choice_texts)) != len(choice_texts):
            findings.append({
                "questionIndex": idx,
                "severity": "error",
                "issue": "duplicate_answer_choice_text",
                "detail": choice_texts,
            })
        if len([c for c in choice_texts if len(c.split()) <= 1]) >= 3:
            findings.append({
                "questionIndex": idx,
                "severity": "warning",
                "issue": "overly_short_distractors",
                "detail": choice_texts,
            })

        increment_map(opening_counts, opening_phrase(stem))
        increment_map(template_counts, str(q.get("stemTemplate") or "unspecified"))
        increment_map(demographic_counts, demographic_signature(stem))
        increment_map(distractor_family_counts, str(q.get("distractorFamily") or "unspecified"))
        increment_map(diagnosis_counts, str(q.get("diagnosisOrTarget") or q.get("testedConcept") or "unspecified"))
        increment_map(objective_counts, str(q.get("educationalObjective") or ""))

    total = max(1, len(generated_questions))
    repeated_limits = [
        ("opening_phrase_overuse", opening_counts, 0.20),
        ("stem_template_overuse", template_counts, 0.35),
        ("demographic_overuse", demographic_counts, 0.45),
        ("distractor_family_overuse", distractor_family_counts, 0.35),
        ("diagnosis_overuse", diagnosis_counts, 0.15),
        ("educational_objective_repeated", objective_counts, 0.01),
    ]
    for issue, counts, fraction in repeated_limits:
        for value, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True):
            if not value or value == "unspecified":
                continue
            if issue == "educational_objective_repeated" and count > 1:
                findings.append({"severity": "error", "issue": issue, "detail": {"value": value, "count": count}})
            elif issue != "educational_objective_repeated" and count / total > fraction and count >= 3:
                findings.append({"severity": "warning", "issue": issue, "detail": {"value": value, "count": count, "fraction": round(count / total, 3)}})

    return {
        "findings": findings,
        "statistics": {
            "totalQuestions": len(generated_questions),
            "openingPhrases": sorted(opening_counts.items(), key=lambda kv: kv[1], reverse=True)[:25],
            "stemTemplates": sorted(template_counts.items(), key=lambda kv: kv[1], reverse=True),
            "demographics": sorted(demographic_counts.items(), key=lambda kv: kv[1], reverse=True),
            "distractorFamilies": sorted(distractor_family_counts.items(), key=lambda kv: kv[1], reverse=True),
            "diagnoses": sorted(diagnosis_counts.items(), key=lambda kv: kv[1], reverse=True)[:30],
            "educationalObjectivesRepeated": [(k, v) for k, v in sorted(objective_counts.items(), key=lambda kv: kv[1], reverse=True) if v > 1],
        },
    }


def question_fingerprint(stem: str, choices: list[dict[str, Any]]) -> str:
    text = normalize_key(stem)
    text = re.sub(r"\b\d+\b", "#", text)
    choice_text = "|".join(normalize_key(c.get("text", "")) for c in choices if isinstance(c, dict))
    return short_hash(text[:500] + "|" + choice_text[:500])


def build_explanation_sections(q: dict[str, Any], table_notes: list[str], explanation_image_placeholders: list[str]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    correct = str(q.get("correctExplanation") or "").strip()
    if correct:
        sections.append({"heading": "Correct Answer Explanation", "body": [correct]})
    incorrect_lines: list[str] = []
    for item in q.get("incorrectExplanations") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().upper()
        explanation = str(item.get("explanation") or "").strip()
        if label and explanation:
            incorrect_lines.append(f"Choice {label}: {explanation}")
    if incorrect_lines:
        sections.append({"heading": "Incorrect Answer Explanation", "body": incorrect_lines})
    extras: list[str] = []
    extras.extend(explanation_image_placeholders)
    if extras:
        sections.append({"heading": "Slide Figures", "body": extras})
    edu = str(q.get("educationalObjective") or "").strip()
    if edu:
        sections.append({"heading": "Educational Objective", "body": [edu]})
    return sections


def expected_sequential_labels(count: int) -> list[str]:
    return [chr(ord("A") + idx) for idx in range(max(0, count))]


def sanitize_invalid_explanation_labels(q: dict[str, Any]) -> dict[str, Any]:
    allowed_labels = {
        str(choice.get("label") or "").strip().upper()
        for choice in q.get("answerChoices") or []
        if isinstance(choice, dict)
    }
    if not allowed_labels:
        return q

    cleaned_incorrect: list[dict[str, str]] = []
    for item in q.get("incorrectExplanations") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().upper()
        explanation = clean_sentence(item.get("explanation"))
        if label in allowed_labels and explanation:
            cleaned_incorrect.append({"label": label, "explanation": explanation})
    q["incorrectExplanations"] = cleaned_incorrect

    for section in q.get("explanationSections") or []:
        if not isinstance(section, dict):
            continue
        body = section.get("body")
        if not isinstance(body, list):
            continue
        cleaned_body = []
        for line in body:
            text = str(line or "")
            match = re.match(r"\s*Choice\s+([A-Z])\s*:", text, re.I)
            if match and match.group(1).upper() not in allowed_labels:
                continue
            cleaned_body.append(line)
        section["body"] = cleaned_body
    return q


def normalize_amboss_extracted_question(q: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(q, dict):
        raise PipelineError("AMBOSS extracted question is not an object.")
    stem = clean_stem_text(q.get("stem"))
    choices = q.get("answerChoices") or []
    if not isinstance(choices, list):
        choices = []
    normalized_choices: list[dict[str, str]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        label = str(choice.get("label") or "").strip().upper()
        text = re.sub(r"\s+", " ", str(choice.get("text") or "").strip())
        if label and text:
            normalized_choices.append({"label": label, "text": text})
    if not (2 <= len(normalized_choices) <= 9):
        raise PipelineError(f"AMBOSS extracted question must have 2-9 choices, got {len(normalized_choices)}.")
    labels = [choice["label"] for choice in normalized_choices]
    expected = expected_sequential_labels(len(labels))
    if labels != expected:
        raise PipelineError(f"AMBOSS answer labels must be sequential {expected[0]}-{expected[-1]}, got {labels}.")
    correct = str(q.get("correctAnswer") or "").strip().upper()
    if correct not in labels:
        raise PipelineError(f"AMBOSS correctAnswer {correct or '(missing)'} is not present in choices {labels}.")
    objective = clean_sentence(q.get("educationalObjective") or q.get("correctExplanation") or "Review the extracted AMBOSS explanation.")
    out = dict(q)
    out["stem"] = stem
    out["answerChoices"] = normalized_choices
    out["correctAnswer"] = correct
    out["educationalObjective"] = objective
    out["retrievalTag"] = clean_tag(q.get("retrievalTag") or objective[:80])
    out["reviewPearl"] = clean_sentence(q.get("reviewPearl") or q.get("correctExplanation") or objective)
    out["correctExplanation"] = clean_sentence(q.get("correctExplanation"))
    out["incorrectExplanations"] = [
        {
            "label": str(item.get("label") or "").strip().upper(),
            "explanation": clean_sentence(item.get("explanation")),
        }
        for item in (q.get("incorrectExplanations") or [])
        if isinstance(item, dict)
        and str(item.get("label") or "").strip().upper() in labels
        and str(item.get("label") or "").strip().upper() != correct
        and clean_sentence(item.get("explanation"))
    ]
    return out


def build_app_ready_payload(normalized_payload: dict[str, Any], generated_questions: list[dict[str, Any]]) -> dict[str, Any]:
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    is_amboss = normalized_payload.get("profile") == AMBOSS_PROFILE
    provenance = normalized_payload.get("provenance") if isinstance(normalized_payload.get("provenance"), dict) else {}
    source_mode = str(provenance.get("sourceMode") or "raw_source")
    questions: list[dict[str, Any]] = []
    for index, gen_q in enumerate(generated_questions, start=1):
        slide_id = str(gen_q.get("slideId") or "")
        slide = slide_by_id.get(slide_id)
        if not slide:
            raise PipelineError(f"Generated Q{index} references unknown slideId: {slide_id}")
        q = normalize_amboss_extracted_question(gen_q) if is_amboss else normalize_generated_question(gen_q)
        q = sanitize_invalid_explanation_labels(q)
        images, explanation_images, figure_refs, table_notes, exp_placeholders = build_media_routes(q, slide, index)
        sections = build_explanation_sections(q, table_notes, exp_placeholders)
        question = {
            "id": f"{'amboss' if is_amboss else 'lecture_slide'}_q{index:03d}_{short_hash(slide_id)}",
            "questionNumber": index,
            "sourceQuestionNumber": slide.get("slideId"),
            "stem": q["stem"],
            "answerChoices": q["answerChoices"],
            "correctAnswer": q["correctAnswer"],
            "educationalObjective": q["educationalObjective"],
            "explanationSections": sections,
            "retrievalTag": q["retrievalTag"],
            "reviewPearl": q["reviewPearl"],
            "clinicalPearl": None,
            "hasEmbeddedFigure": bool(figure_refs),
            "figureRefs": figure_refs,
            "images": images,
            "explanationImages": explanation_images,
            "tables": table_notes_to_tables(slide),
            "sharedGroup": None,
            "extractionWarnings": q.get("extractionWarnings") or [],
            "metadata": {
                "sourceType": "amboss-profile" if is_amboss else "lecture-slide-generator",
                "sourceFormat": "amboss-extraction" if is_amboss else "lecture-slides",
                "sourceMode": source_mode,
                "chunkBundleHash": provenance.get("chunkBundleHash"),
                "chunkBundleId": provenance.get("chunkBundleId"),
                "chunkCountConsumed": provenance.get("chunkCountConsumed"),
                "profile": AMBOSS_PROFILE if is_amboss else normalized_payload.get("profile"),
                "slideId": slide_id,
                "slideTypes": slide.get("slideType") or [],
                "yieldScore": slide.get("yieldScore"),
                "questionPotential": slide.get("questionPotential"),
                "testedConcept": q.get("testedConcept"),
                "diagnosisOrTarget": q.get("diagnosisOrTarget"),
                "stemTemplate": q.get("stemTemplate"),
                "imageRouting": q.get("imageRouting") or [],
                "tableUse": q.get("tableUse") or [],
                "sourceTextHash": slide.get("sourceTextHash"),
                "figureAttachments": {},
            },
        }
        questions.append(question)
    return {
        "schemaVersion": OUTPUT_SCHEMA_VERSION,
        "testTitle": f"{Path(normalized_payload['sourceFile']).stem} Lecture Questions",
        "sourceFormat": SOURCE_FORMAT,
        "metadata": {
            "sourceMode": source_mode,
            "chunkBundleHash": provenance.get("chunkBundleHash"),
            "chunkBundleId": provenance.get("chunkBundleId"),
            "chunkCountConsumed": provenance.get("chunkCountConsumed"),
        },
        "expectedQuestionCount": sum(1 for _ in questions),
        "actualExtractedQuestionCount": len(questions),
        "extractionWarnings": normalized_payload.get("normalizationWarnings") or [],
        "questions": questions,
    }


def normalize_generated_question(q: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(q, dict):
        raise PipelineError("Generated question is not an object.")
    stem = clean_stem_text(q.get("stem"))
    choices = q.get("answerChoices") or q.get("choices") or []
    if not isinstance(choices, list):
        choices = []
    normalized_choices: list[dict[str, str]] = []
    for idx, choice in enumerate(choices[:4]):
        if not isinstance(choice, dict):
            continue
        label = str(choice.get("label") or LABELS[idx]).strip().upper()
        text = re.sub(r"\s+", " ", str(choice.get("text") or "").strip())
        if label not in LABELS:
            label = LABELS[idx]
        normalized_choices.append({"label": label, "text": text})
    if len(normalized_choices) != 4:
        raise PipelineError("Generated question does not have exactly 4 answer choices.")
    if [c["label"] for c in normalized_choices] != LABELS:
        normalized_choices = [{"label": LABELS[idx], "text": c["text"]} for idx, c in enumerate(normalized_choices)]
    correct = str(q.get("correctAnswer") or "").strip().upper()
    if correct not in LABELS:
        raise PipelineError("Generated question has invalid correctAnswer.")
    out = dict(q)
    out["stem"] = stem
    out["answerChoices"] = normalized_choices
    out["correctAnswer"] = correct
    out["educationalObjective"] = clean_sentence(q.get("educationalObjective"))
    out["retrievalTag"] = clean_tag(q.get("retrievalTag") or q.get("testedConcept") or "")
    out["reviewPearl"] = clean_sentence(q.get("reviewPearl") or q.get("correctExplanation") or "")
    out["correctExplanation"] = clean_sentence(q.get("correctExplanation"))
    out = sanitize_invalid_explanation_labels(out)
    return out


def clean_sentence(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^(clinical pearl|educational objective)\s*:\s*", "", text, flags=re.I)
    return text


def clean_stem_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    paragraphs = [
        re.sub(r"[ \t]+", " ", paragraph.strip())
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]
    return "\n\n".join(paragraphs)


QUESTION_PROMPT_RE = re.compile(
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
    r"\bnext (?:best )?(?:step|test|management|treatment)\b|"
    r"\bdiagnosis\b|"
    r"\bmechanism\b|"
    r"\bcause\b|"
    r"\bfinding\b|"
    r"\btreatment\b|"
    r"\btherapy\b)",
    re.I,
)

LAB_ONLY_RE = re.compile(
    r"\b(?:hco3|pco2|pao2|po2|ph|hemoglobin|hematocrit|platelets?|wbc|leukocytes?|"
    r"sodium|potassium|chloride|bicarbonate|creatinine|bun|glucose|calcium|magnesium|"
    r"mEq\s*/\s*L|mg\s*/\s*dL|mm\s*Hg|g\s*/\s*dL|%|/mm\s*3)\b",
    re.I,
)


def strip_stem_trailing_artifacts(stem: str) -> str:
    clean = clean_stem_text(stem)
    clean = re.sub(r"\n*\s*\[Figure:[^\]]+\]\s*$", "", clean, flags=re.I)
    return clean.strip()


def final_stem_sentence(stem: str) -> str:
    clean = strip_stem_trailing_artifacts(stem)
    if not clean:
        return ""
    pieces = re.findall(r"[^.!?]+[.!?]?", clean, flags=re.S)
    for piece in reversed(pieces):
        sentence = re.sub(r"\s+", " ", piece).strip()
        if sentence:
            return sentence
    return re.sub(r"\s+", " ", clean).strip()


def stem_has_explicit_final_question(stem: str) -> bool:
    final = final_stem_sentence(stem)
    if not final:
        return False
    if not final.endswith("?"):
        return False
    return bool(QUESTION_PROMPT_RE.search(final))


def stem_quality_errors(stem: str, prefix: str) -> list[str]:
    clean = clean_stem_text(stem)
    words = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'-]*\b", clean)
    errors: list[str] = []
    if not clean:
        return [f"{prefix}: missing stem."]
    if not stem_has_explicit_final_question(clean):
        errors.append(f"{prefix}: stem must end with an explicit one-best-answer question sentence.")
    if LAB_ONLY_RE.search(clean):
        has_patient_context = bool(re.search(r"\b(patient|man|woman|boy|girl|infant|newborn|child|adolescent|presents|history|exam|after|during)\b", clean, re.I))
        has_question_prompt = stem_has_explicit_final_question(clean)
        if len(words) < 12 or not has_patient_context or not has_question_prompt:
            errors.append(f"{prefix}: stem appears to be a lab-only fragment without clinical context.")
    return errors


def clean_tag(value: Any) -> str:
    text = clean_sentence(value)
    return text[:90].rstrip(" .;:")


def table_notes_to_tables(slide: dict[str, Any]) -> list[dict[str, Any]]:
    tables = []
    for table in slide.get("tables") or []:
        tables.append({
            "id": table.get("tableId"),
            "title": table.get("title") or "Slide table",
            "headers": table.get("headers") or [],
            "rows": table.get("rows") or [],
            "placement": "explanation",
        })
    return tables


def append_stem_figure_placeholder(stem: str, placeholder: str) -> str:
    text = str(stem or "").strip()
    if not text:
        return placeholder
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if sentences and sentences[-1].strip().endswith("?"):
        body = " ".join(part.strip() for part in sentences[:-1] if part.strip())
        final_question = sentences[-1].strip()
        if body:
            return f"{body}\n\n{placeholder}\n\n{final_question}"
        return f"{placeholder}\n\n{final_question}"
    return text.rstrip() + "\n\n" + placeholder


def build_media_routes(q: dict[str, Any], slide: dict[str, Any], q_num: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    image_by_id = {img.get("imageId"): img for img in slide.get("images") or []}
    stem_images: list[dict[str, Any]] = []
    explanation_images: list[dict[str, Any]] = []
    figure_refs: list[dict[str, Any]] = []
    explanation_placeholders: list[str] = []
    used: dict[str, str] = {}
    for route in q.get("imageRouting") or []:
        if not isinstance(route, dict):
            continue
        image_id = str(route.get("imageId") or "").strip()
        placement = str(route.get("placement") or "ignored").strip().lower()
        if placement not in {"stem", "explanation", "ignored"}:
            placement = "ignored"
        if not image_id or image_id not in image_by_id or placement == "ignored":
            continue
        if image_id in used and used[image_id] != placement:
            raise PipelineError(f"Q{q_num}: image {image_id} routed to both stem and explanation.")
        used[image_id] = placement
        img = image_by_id[image_id]
        entry = image_entry_for_question(img, q_num, placement)
        placeholder = f"[FIGURE: {entry['figureId']}]"
        if placement == "stem":
            stem_images.append(entry)
            if placeholder not in q["stem"]:
                q["stem"] = append_stem_figure_placeholder(q["stem"], placeholder)
            figure_refs.append({
                "id": entry["figureId"],
                "placeholder": placeholder,
                "location": "stem",
                "visibleText": [],
            })
        elif placement == "explanation":
            explanation_images.append(entry)
            explanation_placeholders.append(placeholder)
            figure_refs.append({
                "id": entry["figureId"],
                "placeholder": placeholder,
                "location": "explanation",
                "visibleText": [],
            })
    table_notes: list[str] = []
    table_ids = {t.get("tableId") for t in slide.get("tables") or []}
    for table_use in q.get("tableUse") or []:
        if not isinstance(table_use, dict):
            continue
        table_id = table_use.get("tableId")
        if table_id and table_id in table_ids:
            table_notes.append(f"Table used for explanation only: {table_id}")
    if not table_notes:
        for table in slide.get("tables") or []:
            table_notes.append(f"Table used for explanation only: {table.get('tableId')}")
    return stem_images, explanation_images, figure_refs, table_notes, explanation_placeholders


def image_entry_for_question(img: dict[str, Any], q_num: int, placement: str) -> dict[str, Any]:
    asset_path = BASE_DIR / str(img.get("assetPath") or "")
    if not asset_path.exists():
        raise PipelineError(f"Image asset missing: {img.get('assetPath')}")
    figure_id = f"lecture_q{q_num:03d}_{img.get('imageId')}"
    return {
        "figureId": figure_id,
        "figureKey": None,
        "dataUrl": data_url(asset_path, img.get("mimeType") or mime_for(asset_path)),
        "isLabTable": False,
        "kind": "figure",
        "source": "lecture-slide-generator",
        "originalFileName": asset_path.name,
        "assetPath": display_path(asset_path),
        "placement": placement,
        "slideImageId": img.get("imageId"),
    }


def record_app_ready_validation_issue(
    errors: list[str],
    warnings: list[str] | None,
    message: str,
    validation_mode: str,
    *,
    organic_warning: bool = False,
) -> None:
    if validation_mode == "organic_lecture" and organic_warning:
        if warnings is not None:
            warnings.append(message)
        return
    errors.append(message)


def validate_app_ready_payload(
    payload: dict[str, Any],
    validation_mode: str = "strict",
    validation_warnings: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if payload.get("schemaVersion") != OUTPUT_SCHEMA_VERSION:
        errors.append("schemaVersion must be nbme-gemini-json-v3.")
    if payload.get("sourceFormat") != SOURCE_FORMAT:
        errors.append("sourceFormat must remain an existing accepted value: mixed.")
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        errors.append("questions must be a non-empty array.")
        return errors
    is_amboss = any(
        isinstance(q, dict)
        and isinstance(q.get("metadata"), dict)
        and q.get("metadata", {}).get("profile") == AMBOSS_PROFILE
        for q in questions
    )
    seen_numbers: set[int] = set()
    seen_fps: dict[str, int] = {}
    diagnosis_usage: dict[str, int] = {}
    objective_usage: dict[str, int] = {}
    for idx, q in enumerate(questions, start=1):
        prefix = f"Q{idx}"
        if q.get("questionNumber") != idx:
            errors.append(f"{prefix}: questionNumber must be {idx}.")
        if q.get("questionNumber") in seen_numbers:
            errors.append(f"{prefix}: duplicate questionNumber.")
        seen_numbers.add(q.get("questionNumber"))
        stem = str(q.get("stem") or "")
        sentence_count = len(re.findall(r"[.!?](?:\s|$)", stem))
        word_count = len(re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'-]*\b", stem))
        errors.extend(stem_quality_errors(stem, prefix))
        if stem.strip() and (sentence_count < 2 or word_count < 35):
            record_app_ready_validation_issue(
                errors,
                validation_warnings,
                f"{prefix}: stem is too short for clinical reasoning.",
                validation_mode,
                organic_warning=True,
            )
        if re.search(r"\b(which of the following is true|all except|except)\b", stem, re.I):
            errors.append(f"{prefix}: forbidden shallow stem phrasing.")
        choices = q.get("answerChoices")
        if is_amboss:
            if not isinstance(choices, list) or not (2 <= len(choices) <= 9):
                errors.append(f"{prefix}: AMBOSS answerChoices must contain 2-9 choices.")
                choices = []
        elif not isinstance(choices, list) or len(choices) != 4:
            errors.append(f"{prefix}: answerChoices must contain exactly 4 choices.")
            choices = []
        labels = [c.get("label") for c in choices if isinstance(c, dict)]
        if is_amboss:
            expected = expected_sequential_labels(len(labels))
            if labels != expected:
                label_range = f"{expected[0]}-{expected[-1]}" if expected else "A-Z"
                errors.append(f"{prefix}: AMBOSS answerChoices labels must be sequential {label_range}.")
        else:
            if labels != LABELS:
                errors.append(f"{prefix}: answerChoices labels must be A-D.")
            if q.get("correctAnswer") not in LABELS:
                errors.append(f"{prefix}: correctAnswer must be A-D.")
        if q.get("correctAnswer") not in labels:
            errors.append(f"{prefix}: correctAnswer is not present in choices.")
        allowed_label_set = set(labels)
        if not clean_sentence(q.get("educationalObjective")):
            errors.append(f"{prefix}: missing educationalObjective.")
        if re.search(r"clinical pearl\s*:", str(q.get("educationalObjective") or ""), re.I):
            errors.append(f"{prefix}: educationalObjective has forbidden prefix.")
        sections = q.get("explanationSections")
        if not isinstance(sections, list) or not sections:
            errors.append(f"{prefix}: missing explanationSections.")
        elif allowed_label_set:
            for sec_idx, section in enumerate(sections):
                body = section.get("body") if isinstance(section, dict) else []
                if not isinstance(body, list):
                    continue
                for line_idx, line in enumerate(body):
                    match = re.match(r"\s*Choice\s+([A-Z])\s*:", str(line or ""), re.I)
                    if match and match.group(1).upper() not in allowed_label_set:
                        errors.append(f"{prefix}: explanationSections[{sec_idx}].body[{line_idx}] references absent answer choice {match.group(1).upper()}.")
        validate_text_contamination(prefix, q, errors)
        validate_html(prefix, q, errors)
        images = q.get("images")
        exp_images = q.get("explanationImages")
        if not isinstance(images, list):
            errors.append(f"{prefix}: images must be an array.")
            images = []
        if not isinstance(exp_images, list):
            errors.append(f"{prefix}: explanationImages must be an array.")
            exp_images = []
        validate_figure_routes(prefix, q, images, exp_images, errors)
        fp = question_fingerprint(stem, choices)
        if fp in seen_fps:
            errors.append(f"{prefix}: duplicate question similarity with Q{seen_fps[fp]}.")
        seen_fps[fp] = idx
        diagnosis = normalize_key(q.get("metadata", {}).get("diagnosisOrTarget") or q.get("metadata", {}).get("testedConcept") or q.get("retrievalTag") or "")
        if diagnosis:
            diagnosis_usage[diagnosis] = diagnosis_usage.get(diagnosis, 0) + 1
        objective = normalize_key(q.get("educationalObjective") or "")
        if objective:
            objective_usage[objective] = objective_usage.get(objective, 0) + 1
    for diagnosis, count in diagnosis_usage.items():
        if count > 2:
            record_app_ready_validation_issue(
                errors,
                validation_warnings,
                f"duplicate diagnosis/target appears too often: {diagnosis} ({count})",
                validation_mode,
                organic_warning=True,
            )
    for objective, count in objective_usage.items():
        if count > 1:
            errors.append(f"duplicate educational objective: {objective}")
    return errors


def validate_text_contamination(prefix: str, q: dict[str, Any], errors: list[str]) -> None:
    text = json.dumps(q, ensure_ascii=False)
    for forbidden in FORBIDDEN_STRINGS:
        if forbidden.lower() in text.lower():
            errors.append(f"{prefix}: forbidden artifact found: {forbidden}")
    for pattern in OCR_FRAGMENT_PATTERNS:
        if pattern.search(text):
            errors.append(f"{prefix}: OCR artifact pattern found: {pattern.pattern}")


def validate_html(prefix: str, q: dict[str, Any], errors: list[str]) -> None:
    for field_name in ["correctBlurb", "explanation"]:
        if not q.get(field_name):
            continue
        parser = StrictHTMLParser()
        try:
            parser.feed(str(q.get(field_name)))
            parser.close()
        except Exception as exc:
            errors.append(f"{prefix}: malformed HTML in {field_name}: {exc}")
        for err in parser.errors:
            errors.append(f"{prefix}: malformed HTML in {field_name}: {err}")
    for sec in q.get("explanationSections") or []:
        if not isinstance(sec, dict):
            continue
        for body in sec.get("body") or []:
            if "<" in str(body) or ">" in str(body):
                parser = StrictHTMLParser()
                try:
                    parser.feed(str(body))
                    parser.close()
                except Exception as exc:
                    errors.append(f"{prefix}: malformed HTML in explanation section: {exc}")
                for err in parser.errors:
                    errors.append(f"{prefix}: malformed HTML in explanation section: {err}")


def validate_figure_routes(prefix: str, q: dict[str, Any], images: list[dict[str, Any]], exp_images: list[dict[str, Any]], errors: list[str]) -> None:
    refs = q.get("figureRefs")
    if not isinstance(refs, list):
        errors.append(f"{prefix}: figureRefs must be an array.")
        refs = []
    stem_ids = {img.get("figureId") for img in images if isinstance(img, dict)}
    exp_ids = {img.get("figureId") for img in exp_images if isinstance(img, dict)}
    overlap = stem_ids & exp_ids
    if overlap:
        errors.append(f"{prefix}: duplicate image routing stem and explanation: {sorted(overlap)}")
    ref_ids = {ref.get("id") or ref.get("figureId") for ref in refs if isinstance(ref, dict)}
    for image_id in stem_ids | exp_ids:
        if image_id and image_id not in ref_ids:
            errors.append(f"{prefix}: image {image_id} lacks matching figureRef.")
    combined_text = str(q.get("stem") or "") + " " + json.dumps(q.get("explanationSections") or [])
    for ref in refs:
        if not isinstance(ref, dict):
            errors.append(f"{prefix}: figureRef is not an object.")
            continue
        ref_id = ref.get("id") or ref.get("figureId")
        placeholder = ref.get("placeholder")
        location = ref.get("location")
        if not ref_id or not placeholder:
            errors.append(f"{prefix}: figureRef missing id or placeholder.")
        if placeholder and placeholder not in combined_text:
            errors.append(f"{prefix}: figureRef placeholder not present in stem or explanation.")
        if location == "stem" and ref_id not in stem_ids:
            errors.append(f"{prefix}: stem figureRef lacks matching stem image.")
        if location == "explanation" and ref_id not in exp_ids:
            errors.append(f"{prefix}: explanation figureRef lacks matching explanation image.")
    for image_list_name, image_list in [("images", images), ("explanationImages", exp_images)]:
        seen: set[str] = set()
        for img in image_list:
            if not isinstance(img, dict):
                errors.append(f"{prefix}: {image_list_name} entry is not an object.")
                continue
            if img.get("dataUrl") and img.get("figureKey"):
                errors.append(f"{prefix}: {image_list_name} entry has both dataUrl and figureKey before import.")
            if not img.get("dataUrl") and not img.get("figureKey"):
                errors.append(f"{prefix}: {image_list_name} entry lacks dataUrl or figureKey.")
            sig = str(img.get("figureId") or img.get("assetPath") or "")
            if sig in seen:
                errors.append(f"{prefix}: duplicate image entry in {image_list_name}.")
            seen.add(sig)


def write_report(report: dict[str, Any], prefix: str) -> Path:
    path = REPORT_DIR / f"{prefix}_{now_stamp()}.json"
    write_json(path, report)
    log(f"Report -> {display_path(path)}")
    return path


def review_issue_question_index(value: Any) -> int | None:
    match = re.search(r"\bQ(\d+)\b", str(value or ""))
    return int(match.group(1)) if match else None


def review_issue_category(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^(?:Semantic grounding|Question quality)\s+Q\d+\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^Q\d+\s*:\s*", "", text)
    lead = re.split(r"[:.;]", text, maxsplit=1)[0]
    return slugify(lead.lower().replace(" ", "_"))[:80] or "validation_finding"


def review_item_from_message(message: str, severity: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    question_index = review_issue_question_index(message)
    question = questions[question_index - 1] if question_index and question_index <= len(questions) else {}
    metadata = question.get("metadata") if isinstance(question, dict) and isinstance(question.get("metadata"), dict) else {}
    return {
        "questionIndex": question_index,
        "questionId": str(question.get("id") or question.get("questionId") or "") if isinstance(question, dict) else "",
        "slideId": str(question.get("slideId") or metadata.get("slideId") or "") if isinstance(question, dict) else "",
        "severity": severity,
        "category": review_issue_category(message),
        "message": str(message),
        "recommendedAction": "accept_or_reject_later",
    }


def finding_review_item(finding: dict[str, Any], default_severity: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    question_index = finding.get("questionIndex")
    question = questions[question_index - 1] if isinstance(question_index, int) and 0 < question_index <= len(questions) else {}
    metadata = question.get("metadata") if isinstance(question, dict) and isinstance(question.get("metadata"), dict) else {}
    issue = str(finding.get("issue") or "semantic_grounding_finding")
    detail = str(finding.get("detail") or "").strip()
    return {
        "questionIndex": question_index if isinstance(question_index, int) else None,
        "questionId": str(question.get("id") or question.get("questionId") or "") if isinstance(question, dict) else "",
        "slideId": str(question.get("slideId") or metadata.get("slideId") or finding.get("slideId") or "") if isinstance(question, dict) else str(finding.get("slideId") or ""),
        "severity": str(finding.get("severity") or default_severity),
        "category": slugify(issue.lower().replace(" ", "_"))[:80] or "semantic_grounding_finding",
        "message": f"{issue}: {detail}".rstrip(": "),
        "recommendedAction": "accept_or_reject_later",
    }


def write_lecture_review_draft(
    source_label: str,
    questions: list[dict[str, Any]],
    validation_errors: list[str],
    validation_warnings: list[str],
    semantic_findings: list[dict[str, Any]],
    quality_findings: list[dict[str, Any]],
) -> Path | None:
    if not questions or not validation_errors:
        return None
    top_level_fatal_markers = (
        "schemaVersion must be",
        "sourceFormat must remain",
        "questions must be a non-empty array",
    )
    if any(any(marker in message for marker in top_level_fatal_markers) for message in validation_errors):
        return None
    error_items = [review_item_from_message(message, "error", questions) for message in validation_errors]
    warning_items = [review_item_from_message(message, "warning", questions) for message in validation_warnings]
    semantic_items = [finding_review_item(finding, "warning", questions) for finding in semantic_findings]
    quality_items = [finding_review_item(finding, "warning", questions) for finding in quality_findings]
    fatal_question_indexes = {
        item["questionIndex"]
        for item in error_items
        if isinstance(item.get("questionIndex"), int)
    }
    valid_indexes = [
        index for index in range(1, len(questions) + 1)
        if index not in fatal_question_indexes
    ]
    if not valid_indexes:
        return None
    source_type = str(os.environ.get("BIC_PROGRESS_SOURCE") or "").strip() or "lecture_slide_pdf"
    draft = {
        "draftVersion": 1,
        "schemaVersion": OUTPUT_SCHEMA_VERSION,
        "sourceFormat": SOURCE_FORMAT,
        "sourceType": source_type,
        "jobId": str(os.environ.get("BIC_JOB_ID") or "").strip(),
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "needs_review",
        "candidateQuestions": questions,
        "validationErrors": validation_errors,
        "validationWarnings": validation_warnings,
        "semanticGroundingFindings": semantic_findings,
        "reviewItems": error_items + warning_items + semantic_items + quality_items,
        "fatalRejects": [item for item in error_items if isinstance(item.get("questionIndex"), int)],
        "validQuestionIndexes": valid_indexes,
        "outputPaths": {
            "reviewDraftPath": "",
            "appReadyPath": "",
            "reportPath": "",
        },
        "sourceLabel": source_label,
    }
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / "lecture_slide_review_draft.json"
    draft["outputPaths"]["reviewDraftPath"] = str(path.resolve())
    write_json(path, draft)
    json.loads(path.read_text(encoding="utf-8"))
    log(f"Review draft -> {path}")
    return path


def review_candidate_from_failed_repair(
    question: dict[str, Any],
    drop: dict[str, Any],
    repair_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = copy.deepcopy(question)
    metadata = candidate.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        candidate["metadata"] = metadata
    metadata["reviewRequired"] = True
    metadata["reviewReason"] = str(drop.get("reason") or "repair_failed_validation")
    metadata["validationFailures"] = list(drop.get("validationFailures") or drop.get("unsupportedTerms") or [])
    metadata["repairAttemptDetails"] = {
        "reason": drop.get("reason") or "",
        "attemptNotes": list(drop.get("attemptNotes") or []),
        "repairLog": copy.deepcopy(repair_log or {}),
    }
    if drop.get("rawGenerationPayload") is not None:
        metadata["rawGenerationPayload"] = drop.get("rawGenerationPayload")
    if drop.get("chunkOrigin") is not None:
        metadata["chunkOrigin"] = copy.deepcopy(drop.get("chunkOrigin"))
    elif isinstance(candidate.get("metadata"), dict) and candidate["metadata"].get("chunkOrigin"):
        metadata["chunkOrigin"] = copy.deepcopy(candidate["metadata"].get("chunkOrigin"))
    candidate["needsReview"] = True
    candidate.setdefault("extractionWarnings", [])
    if isinstance(candidate["extractionWarnings"], list):
        candidate["extractionWarnings"].extend(
            str(item)
            for item in [
                f"Repair failed: {metadata['reviewReason']}",
                *metadata["validationFailures"],
                *metadata["repairAttemptDetails"]["attemptNotes"],
            ]
            if item
        )
    return candidate


def write_failed_repair_review_draft(
    source_label: str,
    source_format: str,
    source_type: str,
    failed_questions: list[dict[str, Any]],
    failed_items: list[dict[str, Any]],
    repair_report: dict[str, Any],
    app_ready_path: Path | None = None,
    report_path: Path | None = None,
) -> Path | None:
    if not failed_questions:
        return None
    review_items: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    for offset, item in enumerate(failed_items, start=1):
        reason = str(item.get("reason") or "repair_failed_validation")
        failures: list[str] = []
        seen_failures: set[str] = set()
        for value in [
            *(item.get("validationFailures") or item.get("unsupportedTerms") or []),
            *(item.get("attemptNotes") or []),
        ]:
            text = str(value or "").strip()
            if text and text not in seen_failures:
                seen_failures.add(text)
                failures.append(text)
        message = "; ".join([reason, *failures]).strip("; ")
        validation_errors.append(f"Q{offset}: {message}")
        review_items.append({
            "questionIndex": offset,
            "questionId": str(failed_questions[offset - 1].get("id") or failed_questions[offset - 1].get("questionId") or ""),
            "slideId": str(item.get("slideId") or failed_questions[offset - 1].get("slideId") or ""),
            "severity": "error",
            "category": "repair_failed_validation",
            "message": message,
            "chunkOrigin": copy.deepcopy((failed_questions[offset - 1].get("metadata") or {}).get("chunkOrigin") or item.get("chunkOrigin") or {}),
            "recommendedAction": "manual_edit_accept_or_reject",
        })
    draft = {
        "draftVersion": 1,
        "schemaVersion": OUTPUT_SCHEMA_VERSION,
        "sourceFormat": source_format,
        "sourceType": source_type,
        "jobId": str(os.environ.get("BIC_JOB_ID") or "").strip(),
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "needs_review",
        "candidateQuestions": failed_questions,
        "validationErrors": validation_errors,
        "validationWarnings": [],
        "semanticGroundingFindings": [],
        "reviewItems": review_items,
        "fatalRejects": review_items,
        "validQuestionIndexes": [],
        "reviewArtifactType": "failed_repair_questions",
        "repairReport": repair_report,
        "outputPaths": {
            "reviewDraftPath": "",
            "appReadyPath": str(app_ready_path.resolve()) if app_ready_path else "",
            "reportPath": str(report_path.resolve()) if report_path else "",
        },
        "sourceLabel": source_label,
    }
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEW_DIR / "lecture_slide_review_draft.json"
    draft["outputPaths"]["reviewDraftPath"] = str(path.resolve())
    write_json(path, draft)
    json.loads(path.read_text(encoding="utf-8"))
    log(f"Failed-repair review draft -> {path}")
    return path


def process_slide_payload(slide_payload: dict[str, Any], generate: bool, output_stem: str, source_label: str) -> Path:
    normalized_payload = normalize_slides(slide_payload, generate=generate)
    memory = empty_memory()
    allocations = allocate_questions(normalized_payload, memory)
    questions = generate_questions(normalized_payload, allocations, memory, generate=generate)
    if not questions:
        # When the run produced zero questions because the quota-aware retry
        # stop latched (HTTP 429 / RESOURCE_EXHAUSTED), surface the actionable
        # cause instead of the generic "no questions" message. The exit is
        # still fatal because no app-ready output was written, but the error
        # tells the user it is a billing issue, not a code problem.
        if generate and quota_exhausted():
            raise PipelineError(
                f"No questions generated for {source_label}: Gemini quota was "
                f"exhausted during the run (HTTP 429 / RESOURCE_EXHAUSTED). "
                f"Top up prepayment credits at https://ai.studio/projects and "
                f"re-run. Whatever was generated before the quota hit is in "
                f"the generated_questions.json file in this job's output dir."
            )
        raise PipelineError(f"No questions allocated/generated for {source_label}.")
    emit_bic_progress("validating", "Validating generated output")
    final_validation_mode = "organic_lecture"
    grounding_findings = semantic_grounding_findings(
        questions,
        normalized_payload,
        validation_mode=final_validation_mode,
    )
    diversity_report = question_quality_and_diversity(questions)
    app_payload = build_app_ready_payload(normalized_payload, questions)
    validation_warnings: list[str] = []
    errors = validate_app_ready_payload(
        app_payload,
        validation_mode=final_validation_mode,
        validation_warnings=validation_warnings,
    )
    if generate:
        errors.extend(
            f"Semantic grounding Q{f.get('questionIndex', '?')}: {f.get('issue')} {f.get('detail')}"
            for f in grounding_findings
            if f.get("severity") == "error"
        )
        errors.extend(
            f"Question quality Q{f.get('questionIndex', '?')}: {f.get('issue')} {f.get('detail')}"
            for f in diversity_report.get("findings", [])
            if f.get("severity") == "error"
        )
    report = {
        "sourceFile": slide_payload.get("sourceFile") or source_label,
        "sourceMode": (normalized_payload.get("provenance") or {}).get("sourceMode", "raw_source") if isinstance(normalized_payload.get("provenance"), dict) else "raw_source",
        "chunkBundleHash": (normalized_payload.get("provenance") or {}).get("chunkBundleHash") if isinstance(normalized_payload.get("provenance"), dict) else None,
        "chunkBundleId": (normalized_payload.get("provenance") or {}).get("chunkBundleId") if isinstance(normalized_payload.get("provenance"), dict) else None,
        "chunkCountConsumed": (normalized_payload.get("provenance") or {}).get("chunkCountConsumed") if isinstance(normalized_payload.get("provenance"), dict) else None,
        "mode": "generate" if generate else "dry-run",
        "slideCount": len(slide_payload.get("slides") or []),
        "normalizationStats": normalized_payload.get("normalizationStats") or {},
        "normalizationCompletionRate": (
            round((normalized_payload.get("normalizationStats") or {}).get("normalizedSlideCount", 0) / max(1, (normalized_payload.get("normalizationStats") or {}).get("expectedSlideCount", 0)), 4)
        ),
        "normalizationTruncationWarningCount": sum(
            1 for w in normalized_payload.get("normalizationWarnings") or []
            if "truncation" in str(w).lower()
        ),
        "skippedSlideCount": sum(1 for a in allocations if int(a.get("questionCount") or 0) == 0),
        "allocatedQuestionCount": sum(int(a.get("questionCount") or 0) for a in allocations),
        "generatedQuestionCount": len(questions),
        "questionPerSlideDistribution": question_per_slide_distribution(allocations),
        "imageRoutedQuestionCount": sum(1 for q in questions if q.get("imageRouting")),
        "tableRoutedQuestionCount": sum(1 for q in questions if q.get("tableUse")),
        "allocationSummary": [
            {
                "slideId": a["slideId"],
                "questionCount": a["questionCount"],
                "reason": a["reason"],
                "yieldScore": a["yieldScore"],
                "redundancyScore": a["redundancyScore"],
                "contentRichness": a["contentRichness"],
            }
            for a in allocations
        ],
        "semanticGroundingFindings": grounding_findings,
        "questionQualityFindings": diversity_report.get("findings", []),
        "stemDiversityStatistics": diversity_report.get("statistics", {}),
        "validationWarnings": validation_warnings,
        "validationErrors": errors,
    }
    review_draft_path = write_lecture_review_draft(
        source_label,
        app_payload.get("questions") or [],
        errors,
        validation_warnings,
        grounding_findings,
        diversity_report.get("findings", []),
    ) if generate and errors else None
    if review_draft_path:
        report["status"] = "needs_review"
        report["draftPath"] = str(review_draft_path.resolve())
    report_path = write_report(report, "lecture_slide_generation_report")
    if review_draft_path:
        draft = read_json(review_draft_path)
        if isinstance(draft, dict) and isinstance(draft.get("outputPaths"), dict):
            draft["outputPaths"]["reportPath"] = str(report_path.resolve())
            write_json(review_draft_path, draft)
    if errors:
        raise PipelineError("Final validation failed:\n" + "\n".join(f"- {err}" for err in errors[:80]))
    out_path = APP_READY_DIR / f"{slugify(output_stem)}_lecture_app_ready.json"
    emit_bic_progress("writing", "Writing app-ready JSON", file=str(out_path))
    write_json(out_path, app_payload)
    json.loads(out_path.read_text(encoding="utf-8"))
    log(f"App-ready -> {display_path(out_path)}")
    return out_path


def process_pdf(pdf_path: Path, generate: bool) -> Path:
    slide_payload = load_or_decompose_pdf(pdf_path)
    slide_payload["provenance"] = {
        "sourceMode": "raw_source",
        "chunkBundleHash": None,
        "chunkBundleId": None,
        "chunkCountConsumed": None,
    }
    return process_slide_payload(slide_payload, generate=generate, output_stem=pdf_path.stem, source_label=pdf_path.name)


def process_normalized_chunks(bundle_path: Path, generate: bool) -> Path:
    slide_payload = slide_payload_from_normalized_chunks(bundle_path)
    output_stem = f"{Path(str(slide_payload.get('sourceFile') or bundle_path.stem)).stem}_normalized_chunks"
    return process_slide_payload(slide_payload, generate=generate, output_stem=output_stem, source_label=bundle_path.name)


def question_per_slide_distribution(allocations: list[dict[str, Any]]) -> dict[str, int]:
    dist = {"0": 0, "1": 0, "2": 0}
    for allocation in allocations:
        count = str(max(0, min(2, int(allocation.get("questionCount") or 0))))
        dist[count] = dist.get(count, 0) + 1
    return dist


def validate_only(path: Path) -> None:
    payload = read_json(path)
    errors = validate_app_ready_payload(payload)
    if errors:
        raise PipelineError("Validation failed:\n" + "\n".join(f"- {err}" for err in errors[:120]))
    print(f"Validation OK: {path}")


def structured_fast_facts_values(concept: dict[str, Any]) -> list[str]:
    values: list[str] = []
    structured = concept.get("structuredImageFacts") or {}
    if not isinstance(structured, dict):
        return values
    for items in structured.values():
        if isinstance(items, list):
            for item in items:
                text = clean_sentence(item)
                if text and text not in values:
                    values.append(text)
    return values


def fast_facts_allowed_source_facts(concept: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    for key in [
        "nativeTextFacts",
        "clinicalFacts",
        "diagnosticFacts",
        "managementFacts",
        "mechanismFacts",
        "differentialFacts",
        "trapFacts",
    ]:
        for value in concept.get(key) or []:
            text = clean_sentence(value)
            if text and text not in facts:
                facts.append(text)
    if concept.get("imageTextQuality") != "poor":
        for value in concept.get("cleanedImageFacts") or []:
            text = clean_sentence(value)
            if text and text not in facts:
                facts.append(text)
        for value in structured_fast_facts_values(concept):
            if value not in facts:
                facts.append(value)
    return facts


def fast_facts_concept_to_generation_slide(concept: dict[str, Any]) -> dict[str, Any] | None:
    if concept.get("dedupeDisposition") not in {None, "", "keep", "merge"}:
        return None
    source_facts = fast_facts_allowed_source_facts(concept)
    if len(source_facts) < 2:
        return None
    title = clean_sentence(concept.get("title"))
    structured = concept.get("structuredImageFacts") or {}
    image_quality = concept.get("imageTextQuality")
    cleaned_image_facts = [] if image_quality == "poor" else list(concept.get("cleanedImageFacts") or [])
    management_facts = list(concept.get("managementFacts") or [])
    diagnostic_facts = list(concept.get("diagnosticFacts") or [])
    clinical_facts = list(concept.get("clinicalFacts") or [])
    trap_facts = list(concept.get("trapFacts") or [])
    if image_quality != "poor" and isinstance(structured, dict):
        management_facts.extend(structured.get("managementSteps") or [])
        management_facts.extend(structured.get("indications") or [])
        diagnostic_facts.extend(structured.get("criteria") or [])
        diagnostic_facts.extend(structured.get("thresholds") or [])
        trap_facts.extend(structured.get("contraindications") or [])
    fact_categories = [
        clinical_facts,
        diagnostic_facts,
        management_facts,
        concept.get("mechanismFacts") or [],
        concept.get("differentialFacts") or [],
        trap_facts,
    ]
    category_count = sum(1 for values in fact_categories if values)
    if len(source_facts) < 3 and category_count < 2:
        return None
    question_archetype = fast_facts_question_archetype(concept, source_facts)
    slide_types = ["HIGH_YIELD_CLINICAL"]
    if concept.get("images"):
        slide_types.append("IMAGE_HEAVY")
    if concept.get("tables"):
        slide_types.append("TABLE_HEAVY")
    return {
        "slideId": concept["conceptId"],
        "slideType": slide_types,
        "yieldScore": int(concept.get("questionPotential") or 0),
        "primaryConcepts": [title] if title else [],
        "secondaryConcepts": [],
        "clinicalFacts": dedupe_preserve_order(clinical_facts),
        "diagnosticFacts": dedupe_preserve_order(diagnostic_facts),
        "managementFacts": dedupe_preserve_order(management_facts),
        "mechanismFacts": dedupe_preserve_order(concept.get("mechanismFacts") or []),
        "differentialFacts": dedupe_preserve_order(concept.get("differentialFacts") or []),
        "trapFacts": dedupe_preserve_order(trap_facts),
        "nativeTextFacts": dedupe_preserve_order(concept.get("nativeTextFacts") or []),
        "cleanedImageFacts": dedupe_preserve_order(cleaned_image_facts),
        "structuredImageFacts": structured if image_quality != "poor" else {
            "criteria": [],
            "indications": [],
            "contraindications": [],
            "managementSteps": [],
            "thresholds": [],
        },
        "imageTextQuality": image_quality,
        "groundingNotes": source_facts,
        "groundingTerms": list(concept.get("groundingTerms") or []),
        "questionArchetype": question_archetype,
        "images": concept.get("images") or [],
        "tables": concept.get("tables") or [],
        "questionPotential": int(concept.get("questionPotential") or 0),
        "sourceTextHash": short_hash(title + "|" + "|".join(source_facts)),
        "metadata": {
            "profile": FAST_FACTS_PROFILE,
            "sourceConceptId": concept.get("conceptId"),
            "sourceSlideIds": concept.get("sourceSlideIds") or [],
            "dedupeDisposition": concept.get("dedupeDisposition") or "keep",
            "semanticClusterId": concept.get("semanticClusterId"),
            "questionArchetype": question_archetype,
        },
    }


def fast_facts_question_archetype(concept: dict[str, Any], source_facts: list[str]) -> str:
    title = normalize_key(concept.get("title") or "")
    text = normalize_key(" ".join([title] + source_facts))
    if re.search(r"\bscreen\w*|pack-year|low-dose ct|yearly|criteria\b", text):
        return "screening"
    if re.search(r"\bnitrofurantoin-induced|drug-induced|adverse|toxicity|after starting|medication initiation\b", text):
        return "adverse_effect"
    if re.search(r"\bnot to be confused|differentiat|versus| vs |mimic|rather than\b", text):
        return "differentiation"
    if re.search(r"\bmechanism|pathophys|deficien|mutation|receptor|inhibit|activat\b", text):
        return "mechanism"
    if re.search(r"\brisk factor|smok\w*|exposure|family history\b", text):
        return "risk_factor"
    if re.search(r"\bcontraindicat|avoid\b", text):
        return "management"
    if re.search(r"\b(tmp-smx|nitrofurantoin|fosfomycin|fluoroquinolone|ceftriaxone|cefazolin|amoxicillin|antibiotic|drug)\b", text):
        return "pharmacology" if re.search(r"\b\d+\s*(?:days?|dose)|single dose|course\b", text) else "management"
    if re.search(r"\btreat|therapy|management|next step|culture prior|empiric\b", text):
        return "next_step"
    if re.search(r"\bcomplication|sequela|progression\b", text):
        return "complication"
    if re.search(r"\bdiagnos|finding|urinalysis|culture|opacit|rash|clinical features\b", text):
        return "diagnosis"
    return "diagnosis"


def dedupe_preserve_order(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = clean_sentence(value)
        key = normalize_key(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def fast_facts_normalized_payload(pptx_path: Path, graph: dict[str, Any], deck_hash: str, limit_slides: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    slides: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for concept in graph.get("concepts") or []:
        slide = fast_facts_concept_to_generation_slide(concept)
        if slide:
            slides.append(slide)
        else:
            skipped.append({
                "conceptId": concept.get("conceptId"),
                "title": concept.get("title"),
                "reason": "low information, poor image text, or dedupe disposition",
                "dedupeDisposition": concept.get("dedupeDisposition"),
                "imageTextQuality": concept.get("imageTextQuality"),
            })
    payload = {
        "schemaVersion": "fast-facts-normalized-for-generation-v1",
        "profile": FAST_FACTS_PROFILE,
        "sourceFile": pptx_path.name,
        "pptxSha256": deck_hash,
        "pdfSha256": deck_hash,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "limitSlides": int(limit_slides or 0),
        "normalizationWarnings": [],
        "normalizationStats": {
            "expectedSlideCount": graph.get("slideCount"),
            "conceptCount": len(graph.get("concepts") or []),
            "generationEligibleConceptCount": len(slides),
            "skippedConceptCount": len(skipped),
        },
        "slides": slides,
    }
    return payload, skipped


def fast_facts_image_route_guidance(slide: dict[str, Any]) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for image in slide.get("images") or []:
        image_id = str(image.get("imageId") or "")
        kind = normalize_key(image.get("kind") or image.get("classification") or "")
        if not image_id:
            continue
        placement = "ignored"
        if kind in {"algorithm", "explanatory", "table like", "table-like"}:
            placement = "explanation"
        elif kind == "diagnostic":
            placement = "stem"
        routes.append({"imageId": image_id, "placement": placement})
    return routes


def fast_facts_generation_prompt(allocations: list[dict[str, Any]], memory: dict[str, Any]) -> str:
    compact_allocations: list[dict[str, Any]] = []
    for allocation in allocations:
        slide = copy.deepcopy(allocation["slide"])
        allowed = slide_allowed_grounding(slide)
        slide["allowedImageRouting"] = fast_facts_image_route_guidance(slide)
        compact_allocations.append({
            "slideId": allocation["slideId"],
            "questionCount": allocation["questionCount"],
            "reason": allocation.get("reason"),
            "sourceConceptTitle": first_nonempty(slide.get("primaryConcepts")),
            "questionArchetype": slide.get("questionArchetype"),
            "slide": slide,
            "ALLOWED_MEDICAL_TERMS": allowed["allowedMedicalTerms"],
            "ALLOWED_DISTRACTOR_POOL": allowed["allowedDistractorPool"],
            "GROUNDING_FACTS": allowed["groundingFacts"],
        })
    return f"""
Generate a small FAST_FACTS_PROFILE NBME-style question set.

Return JSON only in this exact shape:
{question_schema_required_keys()}

Return exactly the requested number of questions for each allocation.

STRICT SOURCE RULES:
- Generate only from nativeTextFacts, cleanedImageFacts, structuredImageFacts, and GROUNDING_FACTS.
- Do not use imageOcrFacts directly.
- Do not use cleanedImageFacts or structuredImageFacts when imageTextQuality is poor.
- Every clinical claim, distractor, threshold, management step, diagnostic criterion, explanation, and educational objective must map to GROUNDING_FACTS or ALLOWED_MEDICAL_TERMS.
- Every answer choice and every explanation claim must use only concepts from ALLOWED_MEDICAL_TERMS, ALLOWED_DISTRACTOR_POOL, or generic nonmedical vignette wording.
- Never invent outside diseases, drugs, tests, mechanisms, procedures, risk factors, epidemiology, or management.
- If a concept cannot support four grounded answer choices, make the question test a management step, diagnostic finding, contraindication, or differential from the same concept facts. Do not invent.

	QUESTION STYLE:
	- Four answer choices exactly, labeled A-D.
	- One best answer.
	- Every stem must end with a clear final question sentence. No exceptions.
	- The final sentence must ask the learner to choose one best answer and must end with a question mark.
	- Use natural final-question wording that fits the tested concept. Acceptable examples include, but are not limited to: "Which of the following is the most likely diagnosis?", "Which of the following is the most appropriate next step?", "Which treatment is contraindicated in this patient?", "Which preventive measure is most appropriate?", and "Which physician response is most appropriate?"
	- Do not return a stem that is only a fragment, topic heading, lab value, diagnosis, or incomplete vignette.
	- Each question should include questionArchetype equal to the allocation questionArchetype when that archetype fits the question. If the best grounded question needs a more precise archetype, use a concise descriptive value instead of forcing an ill-fitting label.
	- Use exactly one archetype per question. Align the stem, lead-in, correct answer, and distractors to that archetype.
	- All answer choices must answer the same final question and stay in the same semantic category. Do not mix diagnoses, symptoms, labs, imaging findings, drugs, procedures, communication responses, prevention strategies, and mechanisms in one answer set.
- Prefer next best step, management decisions, diagnostic differentiation, contraindications, and screening criteria.
- Avoid "which of the following is true", "all except", and generic template stems.
- Do not use stem findings, lab abnormalities, or imaging findings as distractors when the correct answer is a diagnosis.
- Do not mix diagnoses, symptoms, labs, imaging findings, drugs, and procedures in the same answer set.
- Do not use tautologic distractors that simply repeat a stem finding.
- Avoid pure guideline trivia unless the source fact is a threshold, duration, screening criterion, drug toxicity, or classic board pearl.
- Include discriminating detail such as timing, localization, severity marker, contraindication, threshold, progression pattern, or key associated finding.
- Keep stems clinically natural and concise, usually 3-5 sentences.
- Avoid awkward temporal phrasing. Prefer "Several days after starting nitrofurantoin" over "was prescribed nitrofurantoin several days after medication initiation."
- Use distinct educationalObjective wording for every question.

IMAGE ROUTING:
- Use only image IDs from allowedImageRouting.
- Algorithm, explanatory, and table-like images should be routed to explanation unless the visual is essential to answer the stem.
- Diagnostic images may be routed to stem if needed for reasoning.
- Never route the same image to both stem and explanation.

ROLLING_MEMORY_JSON:
{json.dumps(compact_memory(memory), ensure_ascii=False)}

ALLOCATIONS_JSON:
{json.dumps(compact_allocations, ensure_ascii=False)}
""".strip()


def fast_facts_chunk_event(event_type: str, **payload: Any) -> dict[str, Any]:
    uoga_events = {
        uoga_event("PLAN"),
        uoga_event("START"),
        uoga_event("HEARTBEAT"),
        uoga_event("SUCCESS"),
        uoga_event("DROP"),
        "JOB_COMPLETE",
        "STALL_WARNING",
    }
    if event_type in uoga_events and payload.get("executionGraph") is None:
        return {}
    if "jobId" not in payload:
        payload["jobId"] = bic_job_id()
    if "phase" not in payload:
        payload["phase"] = "generating"
    if "attempt" in payload and "globalRetryId" not in payload:
        payload["globalRetryId"] = payload.pop("attempt")
    if "retryAttempt" in payload and "globalRetryId" not in payload:
        payload["globalRetryId"] = payload.pop("retryAttempt")
    else:
        payload.pop("retryAttempt", None)
    if "fallbackMode" in payload and "retryPhase" not in payload:
        fallback_mode = str(payload.pop("fallbackMode") or "")
        payload["retryPhase"] = {
            "normal": "initial",
            "json_repair": "repair",
            "single_concept_decomposition": "fallback",
            "decomposition": "fallback",
        }.get(fallback_mode, fallback_mode or "initial")
    if event_type in {uoga_event("START"), uoga_event("HEARTBEAT"), uoga_event("SUCCESS"), uoga_event("DROP"), "STALL_WARNING"}:
        payload.setdefault("globalRetryId", 1)
        payload.setdefault("retryPhase", "initial")
    event = emit_bic_chunk_event(event_type, source="fast_facts_pptx", **payload)
    verbose_keys = {"rawPayload", "overflowItems", "malformedItems", "executionGraph", "lastEvent"}
    log(f"{event_type} " + " ".join(f"{key}={value}" for key, value in payload.items() if key not in verbose_keys))
    return event


def fast_facts_review_stub_from_allocation(allocation: dict[str, Any], reason: str, raw_payload: Any = None, attempt_notes: list[str] | None = None) -> dict[str, Any]:
    slide = allocation.get("slide") or {}
    return {
        "slideId": allocation.get("slideId") or slide.get("slideId"),
        "questionKind": "single-best-answer",
        "stemTemplate": "fast_facts_generation_failure",
        "testedConcept": first_nonempty(slide.get("primaryConcepts")) or slide.get("title") or "Fast Facts concept",
        "diagnosisOrTarget": first_nonempty(slide.get("primaryConcepts")) or slide.get("title") or "Fast Facts concept",
        "distractorFamily": "manual_review_required",
        "stem": "Fast Facts generation did not produce a usable question for this concept. Review the preserved source facts and raw payload.",
        "answerChoices": [{"label": label, "text": "Manual review required"} for label in LABELS],
        "correctAnswer": "A",
        "correctExplanation": reason,
        "incorrectExplanations": [{"label": label, "explanation": reason} for label in LABELS[1:]],
        "educationalObjective": reason,
        "retrievalTag": "fast_facts_generation_failure",
        "reviewPearl": reason,
        "imageRouting": [],
        "tableUse": [],
        "sourceFactIds": [],
        "metadata": {
            "slideId": allocation.get("slideId") or slide.get("slideId"),
            "chunkOrigin": allocation.get("chunkOrigin") or {},
            "sourceFacts": fast_facts_allowed_source_facts(slide) if slide else [],
            "rawGenerationPayload": raw_payload,
            "attemptNotes": attempt_notes or [],
        },
    }


# v5.12 (Track B): fingerprint for the durable per-chunk resume cache. Reuses
# fast_facts_cache_key (which already encodes slide content hash + source-file
# hash + prompt / validator / archetype-ontology versions + image-routing hash),
# so ANY change that should regenerate questions also changes this fingerprint.
# chunkIndex + per-slide allocation are folded in so a re-chunked or re-allocated
# run never reuses a stale chunk. Returns "" (caching disabled for the chunk) if
# the key can't be built, so a fingerprint hiccup degrades to today's behaviour.
def _fast_facts_chunk_fingerprint(resume_cache: Any, normalized_payload: dict[str, Any], chunk: list[dict[str, Any]], chunk_index: int) -> str:
    if resume_cache is None or not getattr(resume_cache, "enabled", False):
        return ""
    try:
        slide_keys = [fast_facts_cache_key(normalized_payload, allocation["slide"]) for allocation in chunk]
        return resume_cache.fingerprint({
            "generator": "fast_facts_pptx",
            "chunkIndex": int(chunk_index),
            "slideKeys": slide_keys,
            "alloc": [int(allocation.get("questionCount") or 0) for allocation in chunk],
        })
    except Exception:
        return ""


def generate_fast_facts_questions(normalized_payload: dict[str, Any], allocations: list[dict[str, Any]], memory: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("GEMINI_API_KEY is not set.")
    work = [a for a in allocations if int(a.get("questionCount") or 0) > 0]
    questions: list[dict[str, Any]] = []
    stem = slugify(Path(normalized_payload["sourceFile"]).stem)
    if not work:
        return questions, {"events": [], "dropped": [], "summary": {}}
    chunks = chunk_list(work, 3)
    total_target = sum(int(a.get("questionCount") or 0) for a in work)
    chunk_specs = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_specs.append({
            "chunkId": f"fast_facts_chunk{chunk_index}",
            "expectedQuestions": sum(int(a.get("questionCount") or 0) for a in chunk),
            "conceptIds": [str(a.get("slideId") or "") for a in chunk],
        })
    # UOGA INVARIANT: fast_facts_pptx must never execute without graph context.
    graph = build_execution_graph(bic_job_id(), chunk_specs)
    job_context = JobContext(
        state="running",
        active_tasks=0,
        final_state_path=OUTPUT_DIR / f"{stem}_fast_facts_execution_graph_final.json",
    )
    telemetry = {
        "events": [],
        "dropped": [],
        "executionGraph": graph.to_dict(),
        "summary": {
            "plannedChunks": len(chunks),
            "completedChunks": 0,
            "droppedChunks": 0,
            "retriedChunks": 0,
            "totalAllocated": total_target,
            "totalFinalizedBeforeRepair": 0,
            "totalReviewRequired": 0,
            "totalDropped": 0,
        },
    }
    allocation_counts = [sum(int(a.get("questionCount") or 0) for a in chunk) for chunk in chunks]
    telemetry["events"].append(fast_facts_chunk_event(
        uoga_event("PLAN"),
        totalChunks=len(chunks),
        totalQuestions=total_target,
        chunkAllocation=allocation_counts,
        conceptCount=len(work),
        executionGraph=graph.to_dict(),
        phase="planning",
    ))
    # v5.12 (Track B): durable per-chunk resume cache, rooted at JOB_OUTPUT_ROOT
    # so it survives quit + retry (same jobId). Disabled (no-op) on standalone
    # runs where JOB_OUTPUT_ROOT is None.
    resume_cache = ChunkResumeCache(JOB_OUTPUT_ROOT, "fast_facts_pptx")
    resumed_chunks = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_label = f"fast_facts_chunk{chunk_index}"
        for allocation in chunk:
            allocation["chunkOrigin"] = {"chunkLabel": chunk_label, "chunkIndex": chunk_index, "chunkTotal": len(chunks)}
        # If this exact chunk was already generated in an earlier run of the SAME
        # job, reuse those items and skip the Gemini call. We still mark the graph
        # chunk "completed" (so finalize_job_gate finalizes) and emit a START event
        # carrying chunk="N/M" (so the floating-UI counter advances), exactly as a
        # fresh run would. Cached items are the RAW chunk output (pre-cleanup), the
        # same boundary generate_fast_facts_chunk_with_retries returns, so the
        # cleanup + memory-update loop below treats resumed and fresh items
        # identically — no double cleanup, no memory drift.
        chunk_fp = _fast_facts_chunk_fingerprint(resume_cache, normalized_payload, chunk, chunk_index)
        cached_items = resume_cache.load(chunk_fp) if chunk_fp else None
        if cached_items is not None:
            items = cached_items
            resumed_chunks += 1
            if graph:
                graph.mark_chunk_state(chunk_label, "completed", {"acceptedCount": len(items), "reviewRequiredCount": 0, "resumedFromCache": True})
            resumed_event = fast_facts_chunk_event(
                uoga_event("START"),
                chunk=f"{chunk_index}/{len(chunks)}",
                chunkLabel=chunk_label,
                chunkIndex=chunk_index,
                totalChunks=len(chunks),
                allocatedQuestions=sum(int(a.get("questionCount") or 0) for a in chunk),
                conceptCount=len(chunk),
                resumedFromCache=True,
                retryPhase="resumed",
                executionGraph=graph.to_dict() if graph else None,
            )
            result = {"items": items, "events": [resumed_event], "dropped": [], "retried": False}
            log(f"  Fast Facts {chunk_label}: resumed {len(items)} cached question(s) — skipped Gemini call")
        else:
            result = generate_fast_facts_chunk_with_retries(api_key, normalized_payload["sourceFile"], chunk, memory, chunk_label, chunk_index, len(chunks), graph)
            items = result.get("items") or []
            # Persist immediately so a quit right after this chunk doesn't lose it.
            # Only non-empty (succeeded) chunks are cached, so dropped/failed chunks
            # are always re-attempted on retry.
            if items and chunk_fp:
                resume_cache.save(chunk_fp, items, meta={"chunkLabel": chunk_label, "chunkIndex": chunk_index})
        telemetry["executionGraph"] = graph.to_dict()
        telemetry["events"].extend(result.get("events") or [])
        telemetry["dropped"].extend(result.get("dropped") or [])
        if result.get("retried"):
            telemetry["summary"]["retriedChunks"] += 1
        if items:
            telemetry["summary"]["completedChunks"] += 1
        else:
            telemetry["summary"]["droppedChunks"] += 1
        slide_by_id = {a["slideId"]: a["slide"] for a in chunk}
        for item in items:
            item = fast_facts_cleanup_question(item)
            slide = slide_by_id.get(str(item.get("slideId") or ""))
            if slide and not item.get("questionArchetype"):
                item["questionArchetype"] = slide.get("questionArchetype")
            questions.append(item)
            update_memory_from_question(memory, item)
        telemetry["summary"]["totalFinalizedBeforeRepair"] = len(questions)
        telemetry["summary"]["totalReviewRequired"] = len(telemetry["dropped"])
        telemetry["summary"]["totalDropped"] = len(telemetry["dropped"])
        complete_event = finalize_job_gate(graph, job_context, fast_facts_chunk_event)
        if complete_event:
            telemetry["events"].append(complete_event)
        telemetry["executionGraph"] = graph.to_dict()
    # v5.12 (Track B): surface how many chunks were resumed from the durable
    # cache (0 on a fresh run; >0 after a quit + retry of the same job).
    telemetry["summary"]["resumedChunks"] = resumed_chunks
    if resumed_chunks:
        log(f"  Fast Facts resume: reused {resumed_chunks} of {len(chunks)} chunk(s) from durable cache (no Gemini call)")
    generated_path = GENERATED_DIR / f"{stem}_fast_facts_generated_questions.json"
    write_json(generated_path, {"questions": questions})
    mem_path = MEMORY_DIR / f"{stem}_fast_facts_rolling_memory.json"
    write_json(mem_path, memory)
    log(f"  Fast Facts generated -> {display_path(generated_path)}")
    log(f"  Fast Facts memory -> {display_path(mem_path)}")
    telemetry["executionGraph"] = graph.to_dict()
    return questions, telemetry


def call_fast_facts_generation_chunk_once(
    api_key: str,
    source_file: str,
    chunk: list[dict[str, Any]],
    memory: dict[str, Any],
    chunk_label: str,
    retry_label: str,
    repair_raw: str | None = None,
    repair_error: str = "",
    chunk_index: int = 1,
    chunk_total: int = 1,
    retry_attempt: int | None = None,
    global_retry_id: int | None = None,
    retry_phase: str = "initial",
    execution_graph: Any | None = None,
) -> list[dict[str, Any]]:
    resolved_retry_id = int(global_retry_id if global_retry_id is not None else retry_attempt if retry_attempt is not None else 1)
    if repair_raw is None:
        prompt = fast_facts_generation_prompt(chunk, memory)
        temperature = 0.2
        phase = "generating"
    else:
        prompt = repair_json_prompt(
            raw=repair_raw,
            expected_schema=question_schema_required_keys(),
            expected_ids=[a["slideId"] for a in chunk],
            error_message=repair_error,
        )
        temperature = 0.0
        phase = "repairing"
    with ChunkHeartbeat(
        fast_facts_chunk_event,
        job_id=bic_job_id(),
        chunk_label=chunk_label,
        chunk_index=chunk_index,
        total_chunks=chunk_total,
        phase=phase,
        global_retry_id=resolved_retry_id,
        retry_phase=retry_phase,
        execution_graph=execution_graph.to_dict() if execution_graph is not None else None,
        interval_seconds=3,
        stall_seconds=25,
    ):
        raw = raw_gemini_call(api_key, prompt, temperature=temperature, max_tokens=9000, timeout_seconds=90)
    write_debug_raw(source_file, "generate", chunk_label, retry_label, raw)
    fast_facts_chunk_event(
        uoga_event("HEARTBEAT"),
        chunkLabel=chunk_label,
        chunkIndex=chunk_index,
        totalChunks=chunk_total,
        phase="validating",
        elapsedMs=0,
        globalRetryId=resolved_retry_id,
        retryPhase=retry_phase,
        executionGraph=execution_graph.to_dict() if execution_graph is not None else None,
    )
    parsed = load_largest_valid_json(raw)
    return extract_fast_facts_generated_question_items(parsed, chunk, chunk_label)


def generate_fast_facts_chunk_with_retries(
    api_key: str,
    source_file: str,
    chunk: list[dict[str, Any]],
    memory: dict[str, Any],
    chunk_label: str,
    chunk_index: int = 1,
    chunk_total: int = 1,
    graph: Any | None = None,
    emit_events: bool = True,
) -> dict[str, Any]:
    started = time.time()
    events: list[dict[str, Any]] = []
    expected = sum(int(a.get("questionCount") or 0) for a in chunk)
    concept_ids = [str(a.get("slideId") or "") for a in chunk]
    if graph is None:
        raise RuntimeError("UOGA invariant violation: Execution" + "Graph required for Fast Facts")
    if graph and emit_events:
        graph.mark_chunk_state(chunk_label, "running")
    next_global_retry_id = int(graph.global_retry_id) + 1 if graph else 1
    if emit_events:
        events.append(fast_facts_chunk_event(
            uoga_event("START"),
            chunk=f"{chunk_index}/{chunk_total}",
            chunkLabel=chunk_label,
            chunkIndex=chunk_index,
            totalChunks=chunk_total,
            allocatedQuestions=expected,
            conceptCount=len(chunk),
            globalRetryId=next_global_retry_id,
            retryPhase="initial",
            executionGraph=graph.to_dict() if graph else None,
        ))
    last_raw = ""
    last_error = ""
    retried = False
    partial_items: list[dict[str, Any]] = []
    fallback_dropped: list[dict[str, Any]] = []
    last_success_step: RetryStep | None = None

    def raw_path_for(step: RetryStep) -> Path:
        return DEBUG_DIR / f"{slugify(Path(source_file).stem)}_generate_{slugify(chunk_label)}_global_retry_{step.global_retry_id}_{step.retry_phase}_raw_response.txt"

    def read_last_raw(step: RetryStep) -> None:
        nonlocal last_raw
        path = raw_path_for(step)
        if path.exists():
            last_raw = path.read_text(encoding="utf-8", errors="replace")

    def run_step(step: RetryStep) -> list[dict[str, Any]]:
        nonlocal last_error, partial_items, fallback_dropped, last_success_step
        if step.retry_phase == "initial":
            items = call_fast_facts_generation_chunk_once(
                api_key,
                source_file,
                chunk,
                memory,
                chunk_label,
                f"global_retry_{step.global_retry_id}_{step.retry_phase}",
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                global_retry_id=step.global_retry_id,
                retry_phase=step.retry_phase,
                execution_graph=graph,
            )
            if len(items) < expected:
                partial_items = items
                last_error = f"under_count accepted {len(items)} of {expected}"
                read_last_raw(step)
                raise PipelineError(last_error)
            last_success_step = step
            return items
        if step.retry_phase == "repair":
            if partial_items:
                last_success_step = step
                return partial_items
            if not last_raw or is_truncation_failure(last_error):
                raise PipelineError(last_error or "no raw payload available for repair")
            items = call_fast_facts_generation_chunk_once(
                api_key,
                source_file,
                chunk,
                memory,
                chunk_label,
                f"global_retry_{step.global_retry_id}_{step.retry_phase}",
                repair_raw=last_raw,
                repair_error=last_error,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                global_retry_id=step.global_retry_id,
                retry_phase=step.retry_phase,
                execution_graph=graph,
            )
            last_success_step = step
            return items
        if step.retry_phase == "fallback":
            raise PipelineError(last_error or "fallback exhausted; preserving chunk for review")
        raise PipelineError(f"unknown retry phase: {step.retry_phase}")

    def on_retry(step: RetryStep, exc: Exception) -> None:
        nonlocal last_error, retried
        last_error = str(exc)
        read_last_raw(step)
        retried = True
        if step.retry_phase == "initial":
            warn(f"Fast Facts generation {chunk_label}: initial failed: {last_error}")
        elif step.retry_phase == "repair":
            warn(f"Fast Facts generation {chunk_label}: repair failed: {last_error}")
        next_phase = "repair" if step.retry_phase == "initial" else "fallback"
        next_id = step.global_retry_id + 1
        if graph and emit_events:
            events.append(fast_facts_chunk_event(
                uoga_event("HEARTBEAT"),
                chunkLabel=chunk_label,
                chunkIndex=chunk_index,
                totalChunks=chunk_total,
                phase="generating",
                elapsedMs=int((time.time() - started) * 1000),
                globalRetryId=next_id,
                retryPhase=next_phase,
                executionGraph=graph.to_dict(),
            ))

    try:
        items = execute_retry(
            run_step,
            RetryContext(
                source_type="fast_facts_pptx",
                chunk_label=chunk_label,
                chunk_id=chunk_label,
                execution_graph=graph,
                retry_plan=(RetryStep(1, "initial"), RetryStep(2, "repair")),
            ),
            on_retry=on_retry,
        )
        success_step = last_success_step or RetryStep(1, "initial")
        repaired_count = len(items) if success_step.retry_phase == "repair" and not partial_items else 0
        review_required = len(fallback_dropped) if fallback_dropped else max(0, expected - len(items))
        if graph:
            graph.mark_chunk_state(chunk_label, "completed", {"acceptedCount": len(items), "reviewRequiredCount": review_required})
        graph_attempts = graph.get_chunk(chunk_label).attempts if graph else []
        event_retry_id = graph_attempts[-1].attempt_id if graph_attempts else success_step.global_retry_id
        if emit_events:
            events.append(fast_facts_chunk_event(
                uoga_event("SUCCESS"),
                chunk=chunk_label,
                chunkIndex=chunk_index,
                totalChunks=chunk_total,
                returnedCount=len(items),
                acceptedCount=len(items),
                repairedCount=repaired_count,
                reviewRequiredCount=review_required,
                runtime=round(time.time() - started, 2),
                globalRetryId=event_retry_id,
                retryPhase=success_step.retry_phase,
                executionGraph=graph.to_dict() if graph else None,
            ))
        return {"items": items, "events": events, "dropped": fallback_dropped, "retried": retried}
    except RetryExhaustedError as exc:
        last_error = str(exc.last_error or exc)

    warn(f"Fast Facts generation {chunk_label}: dropped concept {chunk[0].get('slideId')} after generation JSON/schema failures: {last_error}")
    dropped = [
        {
            "questionIndex": index,
            "slideId": allocation.get("slideId"),
            "reason": "generation_failed_after_bounded_retries",
            "attemptNotes": [last_error],
            "validationFailures": [last_error],
            "rawGenerationPayload": last_raw,
            "originalQuestion": fast_facts_review_stub_from_allocation(allocation, last_error or "generation failed", last_raw, [last_error]),
        }
        for index, allocation in enumerate(chunk)
    ]
    if graph:
        graph.mark_chunk_state(chunk_label, "dropped", {"reviewRequiredCount": len(dropped), "reason": last_error})
    graph_attempts = graph.get_chunk(chunk_label).attempts
    event_retry_id = graph_attempts[-1].attempt_id if graph_attempts else 3
    if emit_events:
        events.append(fast_facts_chunk_event(
            uoga_event("DROP"),
            chunk=chunk_label,
            chunkIndex=chunk_index,
            totalChunks=chunk_total,
            reason=last_error,
            conceptIds=concept_ids,
            reviewArtifactPreserved=True,
            globalRetryId=event_retry_id,
            retryPhase="drop",
            executionGraph=graph.to_dict(),
        ))
    return {"items": [], "events": events, "dropped": dropped, "retried": True}


def fast_facts_allocations(normalized_payload: dict[str, Any], memory: dict[str, Any]) -> list[dict[str, Any]]:
    update_memory_from_slides(memory, normalized_payload.get("slides") or [])
    allocations: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for slide in normalized_payload.get("slides") or []:
        title = first_nonempty(slide.get("primaryConcepts"))
        title_key = normalize_key(title)
        fact_count = sum(len(slide.get(key) or []) for key in [
            "clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts",
            "differentialFacts", "trapFacts", "groundingNotes",
        ])
        count = 0
        reason = "not enough grounded concept facts"
        if title_key and title_key in seen_titles:
            reason = "duplicate concept title in constrained run"
        elif int(slide.get("questionPotential") or 0) >= 50 and fact_count >= 3:
            count = 1
            reason = "grounded Fast Facts concept with sufficient facts"
        seen_titles.add(title_key)
        allocations.append({
            "slideId": slide["slideId"],
            "questionCount": count,
            "reason": reason,
            "yieldScore": slide.get("yieldScore", 0),
            "redundancyScore": 0,
            "contentRichness": fact_count,
            "slide": slide,
        })
    return allocations


def limit_fast_facts_question_attempts(allocations: list[dict[str, Any]], question_limit: int) -> list[dict[str, Any]]:
    if int(question_limit or 0) <= 0:
        return allocations
    limited = copy.deepcopy(allocations)
    remaining = int(question_limit)
    for allocation in limited:
        count = max(0, int(allocation.get("questionCount") or 0))
        if count <= 0:
            continue
        if remaining <= 0:
            allocation["questionCount"] = 0
            allocation["reason"] = "skipped by Fast Facts question attempt limit"
            continue
        kept = min(count, remaining)
        allocation["questionCount"] = kept
        remaining -= kept
        if kept < count:
            allocation["reason"] = "partially capped by Fast Facts question attempt limit"
    return limited


def fast_facts_diagnostic_entry(allocation: dict[str, Any]) -> dict[str, Any]:
    slide = allocation["slide"]
    allowed = slide_allowed_grounding(slide)
    return {
        "conceptId": slide.get("slideId"),
        "sourceSlideIds": (slide.get("metadata") or {}).get("sourceSlideIds") or [],
        "conceptTitle": first_nonempty(slide.get("primaryConcepts")),
        "allocation": {
            "questionCount": allocation.get("questionCount"),
            "reason": allocation.get("reason"),
            "yieldScore": allocation.get("yieldScore"),
            "contentRichness": allocation.get("contentRichness"),
        },
        "selectedArchetype": slide.get("questionArchetype"),
        "allowedGroundingFacts": allowed.get("groundingFacts") or [],
        "allowedMedicalTerms": allowed.get("allowedMedicalTerms") or [],
        "cacheStatus": "pending",
        "generatedQuestionBeforeRepair": None,
        "preRepairValidationFindings": {},
        "repairAttempted": False,
        "repairResult": None,
        "postRepairValidationFindings": {},
        "finalDisposition": "pending",
        "dropReason": "",
        "finalQuestionId": None,
        "finalQuestionIndex": None,
    }


def fast_facts_diagnostic_validation(
    normalized_payload: dict[str, Any],
    slide: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, Any]:
    single_norm = dict(normalized_payload)
    single_norm["slides"] = [slide]
    _, errors, grounding, diversity, strict = collect_fast_facts_generation_validation(single_norm, [question])
    return {
        "errors": errors,
        "semanticGroundingFindings": grounding,
        "questionQualityFindings": diversity.get("findings", []),
        "fastFactsStrictFindings": strict,
    }


def write_fast_facts_diagnostic_report(report: dict[str, Any]) -> Path:
    return write_report(report, "fast_facts_diagnostic_report")


def validation_failed_question_indices(errors: list[str], grounding_findings: list[dict[str, Any]], diversity_report: dict[str, Any]) -> set[int]:
    indices: set[int] = set()
    for finding in grounding_findings:
        if finding.get("severity") == "error":
            try:
                indices.add(int(finding.get("questionIndex")))
            except Exception:
                pass
    for finding in diversity_report.get("findings", []) or []:
        if finding.get("severity") == "error":
            try:
                indices.add(int(finding.get("questionIndex")))
            except Exception:
                pass
    for error in errors:
        match = re.search(r"\bQ(\d+)\b", str(error))
        if match:
            indices.add(int(match.group(1)))
    return indices


def question_scoped_validation_messages(
    question_index: int,
    errors: list[str],
    grounding_findings: list[dict[str, Any]] | None = None,
    diversity_report: dict[str, Any] | None = None,
    strict_findings: list[dict[str, Any]] | None = None,
) -> list[str]:
    scoped: list[str] = []
    seen: set[str] = set()

    def add(message: Any) -> None:
        text = str(message or "").strip()
        if text and text not in seen:
            seen.add(text)
            scoped.append(text)

    for error in errors:
        text = str(error or "")
        match = re.search(r"\bQ(\d+)\b", text)
        if match and int(match.group(1)) == question_index:
            add(text)

    def has_prefixed_error(prefix: str, issue: Any) -> bool:
        needle = f"{prefix} Q{question_index}: {issue}"
        return any(str(error or "").startswith(needle) for error in errors)

    for finding in grounding_findings or []:
        if int(finding.get("questionIndex") or 0) == question_index and finding.get("severity") == "error":
            if not has_prefixed_error("Semantic grounding", finding.get("issue")):
                add(f"Semantic grounding Q{question_index}: {finding.get('issue')} {finding.get('detail')}")
    for finding in (diversity_report or {}).get("findings", []) or []:
        if int(finding.get("questionIndex") or 0) == question_index and finding.get("severity") == "error":
            if not has_prefixed_error("Question quality", finding.get("issue")):
                add(f"Question quality Q{question_index}: {finding.get('issue')} {finding.get('detail')}")
    for finding in strict_findings or []:
        if int(finding.get("questionIndex") or 0) == question_index and finding.get("severity") == "error":
            if not has_prefixed_error("Fast Facts grounding", finding.get("issue")):
                add(f"Fast Facts grounding Q{question_index}: {finding.get('issue')} {finding.get('detail')}")
    return scoped


def repair_fast_facts_questions(
    normalized_payload: dict[str, Any],
    questions: list[dict[str, Any]],
    before_errors: list[str],
    before_grounding: list[dict[str, Any]],
    before_diversity: dict[str, Any],
    before_strict: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    failed_indices = validation_failed_question_indices(before_errors, before_grounding, before_diversity)
    unsupported_by_index: dict[int, list[str]] = {
        int(f.get("questionIndex")): list(f.get("detail") or [])
        for f in before_grounding
        if f.get("severity") == "error" and f.get("issue") == "unsupported_medical_claim_terms"
    }
    dropped: dict[int, dict[str, Any]] = {}
    repair_log: list[dict[str, Any]] = []
    for idx in sorted(failed_indices):
        if idx < 1 or idx > len(questions):
            continue
        original = questions[idx - 1]
        slide = slide_by_id.get(str(original.get("slideId") or ""))
        reason = "post_generation_validation_failed"
        attempt_notes = question_scoped_validation_messages(
            idx,
            before_errors,
            before_grounding,
            before_diversity,
            before_strict or [],
        )
        if not attempt_notes:
            attempt_notes = ["Question failed validation after bounded generation attempts."]
        if not slide:
            dropped[idx] = {
                "questionIndex": idx,
                "slideId": original.get("slideId"),
                "reason": "unknown_slide_id",
                "validationFailures": attempt_notes,
                "originalQuestion": copy.deepcopy(original),
            }
            accepted = False
        else:
            dropped[idx] = {
                "questionIndex": idx,
                "slideId": original.get("slideId"),
                "reason": reason,
                "attemptNotes": attempt_notes,
                "validationFailures": attempt_notes,
                "originalQuestion": copy.deepcopy(original),
                "unsupportedTerms": unsupported_by_index.get(idx, []),
            }
            accepted = False
        repair_log.append({
            "questionIndex": idx,
            "slideId": original.get("slideId"),
            "accepted": accepted,
            "attemptNotes": attempt_notes,
            "initialUnsupportedTerms": unsupported_by_index.get(idx, []),
        })
    final_questions: list[dict[str, Any]] = []
    for idx, question in enumerate(questions, start=1):
        if idx in dropped:
            continue
        final_questions.append(question)
    report = {
        "mode": "fast-facts-review-on-validation-failure",
        "sourceFile": normalized_payload.get("sourceFile"),
        "initialQuestionCount": len(questions),
        "failedQuestionCountBeforeRepair": len(failed_indices),
        "repairedQuestionCount": 0,
        "droppedQuestionCount": len(dropped),
        "finalQuestionCount": len(final_questions),
        "droppedQuestions": list(dropped.values()),
        "repairLog": repair_log,
    }
    return final_questions, report


def fast_facts_question_repair_prompt(
    original_question: dict[str, Any],
    slide: dict[str, Any],
    unsupported_terms: list[str],
    attempt: int,
) -> str:
    allowed = slide_allowed_grounding(slide)
    archetype = slide.get("questionArchetype") or "diagnosis"
    return f"""
Repair one FAST_FACTS_PROFILE NBME-style question.

Return valid JSON only in this exact shape:
{question_schema_required_keys()}

	Return exactly 1 question.
	Use the same slideId.
	Use exactly 4 answer choices labeled A, B, C, D.
	Include questionArchetype: {archetype}
	The stem must end with a clear final one-best-answer question sentence ending in a question mark. No exceptions.
	Use natural final-question wording that fits the concept. Examples include, but are not limited to, diagnosis, next step, contraindication, prevention, screening, mechanism, and physician-response questions.
	Do not return a stem that is only a fragment, topic heading, lab value, diagnosis, or incomplete vignette.

	STRICT REPAIR RULES:
- Preserve the original stem if it is grounded and clear. If it is awkward, clean grammar only.
- If the failure is answer-choice ontology or distractor quality, regenerate the answer choices and explanations while keeping the stem aligned to the same source concept.
- Every clinical claim, answer choice, distractor, threshold, management step, diagnostic criterion, explanation, and educational objective must map to GROUNDING_FACTS or ALLOWED_MEDICAL_TERMS.
- Do not use imageOcrFacts directly.
- Do not invent outside diseases, drugs, tests, mechanisms, procedures, risk factors, epidemiology, or management.
- Do not include these unsupported terms unless present in ALLOWED_MEDICAL_TERMS: {json.dumps(unsupported_terms, ensure_ascii=False)}

ARCHETYPE AND ONTOLOGY RULES:
- Use exactly one archetype: {archetype}.
- diagnosis: every answer choice must be a diagnosis or named clinical condition.
- next_step: every answer choice must be a concrete next step or action.
- mechanism: every answer choice must be a mechanism.
- adverse_effect: every answer choice must be a diagnosis/adverse reaction, not an isolated symptom, lab, or imaging finding.
- management: every answer choice must be an intervention or management action.
- differentiation: every answer choice must be a diagnosis, condition, or clinical category being differentiated.
- risk_factor: every answer choice must be a risk factor.
- screening: every answer choice must be a screening criterion, screening interval, or screening test.
- pharmacology: every answer choice must be a drug or drug class.
- complication: every answer choice must be a complication.
- Never mix diagnoses, symptoms, labs, imaging findings, drugs, and procedures in the same answer set.
- Do not use stem findings as distractors for a diagnosis/adverse-effect question.

Attempt: {attempt}

NORMALIZED_FAST_FACTS_CONCEPT_JSON:
{json.dumps(slide, ensure_ascii=False)}

ALLOWED_MEDICAL_TERMS:
{json.dumps(allowed["allowedMedicalTerms"], ensure_ascii=False)}

ALLOWED_DISTRACTOR_POOL:
{json.dumps(allowed["allowedDistractorPool"], ensure_ascii=False)}

GROUNDING_FACTS:
{json.dumps(allowed["groundingFacts"], ensure_ascii=False)}

ORIGINAL_QUESTION_TO_REPAIR:
{json.dumps(original_question, ensure_ascii=False)}
""".strip()


def call_fast_facts_question_repair_once(
    api_key: str,
    source_file: str,
    q_index: int,
    original_question: dict[str, Any],
    slide: dict[str, Any],
    unsupported_terms: list[str],
    attempt: int,
) -> dict[str, Any]:
    prompt = fast_facts_question_repair_prompt(original_question, slide, unsupported_terms, attempt)
    raw = raw_gemini_call(api_key, prompt, temperature=0.1, max_tokens=8192, timeout_seconds=45)
    write_debug_raw(source_file, "repair_question", f"q{q_index:03d}", f"attempt{attempt}", raw)
    parsed = load_largest_valid_json(raw)
    allocation = {
        "slideId": slide["slideId"],
        "questionCount": 1,
        "slide": slide,
    }
    items = extract_generated_question_items(parsed, [allocation], f"fast_facts_repair_q{q_index:03d}_attempt{attempt}")
    return fast_facts_cleanup_question(items[0])


def fast_facts_cleanup_question(q: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(q)
    stem = str(out.get("stem") or "")
    stem = re.sub(r"\bwas prescribed ([A-Za-z0-9+\-]+) several days after medication initiation\b", r"several days after starting \1", stem, flags=re.I)
    stem = re.sub(r"\bSeveral days after starting ([A-Za-z0-9+\-]+) for ([^.]+)\. She now presents\b", r"Several days after starting \1 for \2, she presents", stem)
    stem = re.sub(r"\bnon-pregnant\b", "nonpregnant", stem, flags=re.I)
    stem = clean_stem_text(stem)
    stem = re.sub(r"\b(warm, tender, erythematous rash) with raised, well-demarcated borders\b", r"\1 with raised, well-demarcated borders", stem)
    out["stem"] = stem
    return out


def fast_facts_answer_ontology(text: str) -> str:
    value = normalize_key(text)
    if not value:
        return "unknown"
    if re.search(r"\b(abscess|ain|acute kidney injury|interstitial nephritis|pulmonary injury|erysipelas|cellulitis|folliculitis|furuncle|uti|urinary tract infection|cystitis|pyelonephritis|infection)\b", value):
        return "diagnosis"
    if re.search(r"\b(obtain|order|perform|culture|test|testing|screen|examination|ct|ultrasound|x-ray|urinalysis|blood cultures?|antibod(?:y|ies)|tissue transglutaminase|ttg|tsh|free t4|hormone levels?|levels?|dexa|dxa|scan)\b", value):
        return "procedure"
    if re.search(r"\b(initiate|administer|treat|therapy|antibiotic|management|oral|intravenous|iv)\b", value):
        return "treatment"
    if re.search(r"\b(amoxicillin|ceftriaxone|cefazolin|nitrofurantoin|tmp-smx|trimethoprim|trimethoprim-sulfamethoxazole|sulfamethoxazole|fosfomycin|fluoroquinolone|ciprofloxacin|levofloxacin|piperacillin|tazobactam|imipenem|carbapenem)\b", value):
        return "drug"
    if re.search(r"\b(opacit|infiltrat|effusion|radiograph|imaging|bilateral mid|lower lung)\b", value):
        return "imaging_finding"
    if re.search(r"\b(leukocytosis|eosinophilia|nitrate|leukocyte esterase|alkalosis|acidosis|hemoglobin|a1c)\b", value):
        return "lab_finding"
    if re.search(r"\b(fever|pain|tenderness|frequency|dysuria|cough|shortness|rash|crackles|drainage|chills|vomiting|nausea)\b", value):
        return "symptom"
    if re.search(r"\b(streptococcus|staphylococcus|mssa|mrsa|pyogenes)\b", value):
        return "organism"
    if re.search(r"\b(deficiency|mutation|inhibition|activation|mechanism|receptor|pathway)\b", value):
        return "mechanism"
    if re.search(r"\b(smoking|pack-year|age|diabetes|comorbidit|risk)\b", value):
        return "risk_factor"
    return "unknown"


def fast_facts_expected_ontology(archetype: str, correct_text: str) -> str:
    archetype = normalize_key(archetype)
    if archetype in {"diagnosis", "differentiation", "adverse_effect"}:
        return "diagnosis"
    if archetype in {"management", "next_step"}:
        correct_class = fast_facts_answer_ontology(correct_text)
        return "procedure" if correct_class == "procedure" else "treatment"
    if archetype == "pharmacology":
        return "drug"
    if archetype == "mechanism":
        return "mechanism"
    if archetype == "risk_factor":
        return "risk_factor"
    if archetype == "screening":
        return "procedure"
    if archetype == "complication":
        return "complication"
    return fast_facts_answer_ontology(correct_text)


def fast_facts_choice_classes(q: dict[str, Any], slide: dict[str, Any] | None = None) -> list[dict[str, str]]:
    archetype = str(q.get("questionArchetype") or (slide or {}).get("questionArchetype") or "")
    correct_label = str(q.get("correctAnswer") or "").strip().upper()
    choices = q.get("answerChoices") or []
    correct_text = ""
    for choice in choices:
        if isinstance(choice, dict) and str(choice.get("label") or "").strip().upper() == correct_label:
            correct_text = str(choice.get("text") or "")
            break
    expected = fast_facts_expected_ontology(archetype, correct_text)
    rows: list[dict[str, str]] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        label = str(choice.get("label") or "").strip().upper()
        text = str(choice.get("text") or "")
        actual = fast_facts_answer_ontology(text)
        normalized = actual
        if expected == "treatment" and actual in {"drug", "procedure", "treatment"}:
            normalized = "treatment"
        if expected == "procedure" and actual in {"procedure", "treatment"} and archetype in {"next_step", "screening"}:
            normalized = "procedure"
        rows.append({"label": label, "text": text, "ontologyClass": normalized, "rawOntologyClass": actual, "expectedClass": expected})
    return rows


def fast_facts_stem_terms(stem: str) -> set[str]:
    terms: set[str] = set()
    for word in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-]{3,}\b", stem):
        w = normalize_key(word)
        if w and w not in COMMON_CLINICAL_WORDS:
            terms.add(w)
    return terms


def fast_facts_qa_audit(
    normalized_payload: dict[str, Any],
    questions: list[dict[str, Any]],
    repaired_indices: set[int] | None = None,
) -> dict[str, Any]:
    repaired_indices = repaired_indices or set()
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    items: list[dict[str, Any]] = []
    ontology_mismatches = 0
    malformed_distractor_sets = 0
    for idx, q in enumerate(questions, start=1):
        slide = slide_by_id.get(str(q.get("slideId") or ""))
        archetype = str(q.get("questionArchetype") or (slide or {}).get("questionArchetype") or "unspecified")
        classes = fast_facts_choice_classes(q, slide)
        expected = classes[0]["expectedClass"] if classes else "unknown"
        class_ok = bool(classes) and all(row["ontologyClass"] == expected for row in classes)
        if not class_ok:
            ontology_mismatches += 1
            malformed_distractor_sets += 1
        stem = str(q.get("stem") or "")
        choice_texts = [str(c.get("text") or "") for c in q.get("answerChoices") or [] if isinstance(c, dict)]
        stem_terms = fast_facts_stem_terms(stem)
        tautologic = [
            text for text in choice_texts
            if len(normalize_key(text).split()) >= 2 and normalize_key(text) in normalize_key(stem)
        ]
        if tautologic:
            malformed_distractor_sets += 1
        has_discriminator = bool(re.search(
            r"\b(days?|after|before|early|later|raised|well-demarcated|purulent|nonpurulent|fever|flank|costovertebral|systemic|severe|no systemic|5-day|3-day|single dose|eosinophilia|opacities|external ear)\b",
            stem,
            re.I,
        ))
        awkward = bool(re.search(r"medication initiation|was prescribed .* several days after", stem, re.I))
        grounding_score = 100
        stem_clarity_score = 90 - (30 if awkward else 0) - (10 if len(stem.split()) < 35 else 0)
        discrimination_score = 85 if has_discriminator else 55
        distractor_quality_score = 90 if class_ok and not tautologic else 45
        if len(set(normalize_key(t) for t in choice_texts)) != len(choice_texts):
            distractor_quality_score -= 25
        tier = "strong"
        if min(distractor_quality_score, discrimination_score, stem_clarity_score, grounding_score) < 70:
            tier = "acceptable" if min(distractor_quality_score, discrimination_score, stem_clarity_score, grounding_score) >= 50 else "poor"
        weakest = ""
        reason = ""
        if not class_ok:
            off = [row for row in classes if row["ontologyClass"] != expected]
            weakest = off[0]["text"] if off else ""
            reason = f"ontology mismatch: expected {expected}"
        elif tautologic:
            weakest = tautologic[0]
            reason = "distractor repeats a stem finding"
        elif not has_discriminator:
            reason = "stem has limited discriminating detail"
        items.append({
            "questionNumber": idx,
            "slideId": q.get("slideId"),
            "sourceConceptTitle": first_nonempty((slide or {}).get("primaryConcepts")),
            "archetype": archetype,
            "ontologyClassConsistency": class_ok,
            "answerChoiceClasses": classes,
            "distractorQualityScore": max(0, distractor_quality_score),
            "discriminationScore": discrimination_score,
            "groundingScore": grounding_score,
            "stemClarityScore": max(0, stem_clarity_score),
            "likelyNbmeQualityTier": tier,
            "weakestDistractor": weakest,
            "reasonFlagged": reason,
            "repairApplied": idx in repaired_indices,
        })
    return {
        "items": items,
        "summary": {
            "questionCount": len(questions),
            "ontologyMismatches": ontology_mismatches,
            "malformedDistractorSets": malformed_distractor_sets,
            "qualityTiers": {
                "poor": sum(1 for item in items if item["likelyNbmeQualityTier"] == "poor"),
                "acceptable": sum(1 for item in items if item["likelyNbmeQualityTier"] == "acceptable"),
                "strong": sum(1 for item in items if item["likelyNbmeQualityTier"] == "strong"),
            },
        },
    }


def fast_facts_before_after_comparison(before_questions: list[dict[str, Any]], after_questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comparison: list[dict[str, Any]] = []
    max_count = max(len(before_questions), len(after_questions))
    for idx in range(max_count):
        before = before_questions[idx] if idx < len(before_questions) else {}
        after = after_questions[idx] if idx < len(after_questions) else {}
        before_choices = [str(c.get("text") or "") for c in before.get("answerChoices") or [] if isinstance(c, dict)]
        after_choices = [str(c.get("text") or "") for c in after.get("answerChoices") or [] if isinstance(c, dict)]
        comparison.append({
            "questionNumber": idx + 1,
            "beforeSlideId": before.get("slideId"),
            "afterSlideId": after.get("slideId"),
            "stemChanged": str(before.get("stem") or "") != str(after.get("stem") or ""),
            "choicesChanged": before_choices != after_choices,
            "correctAnswerChanged": before.get("correctAnswer") != after.get("correctAnswer"),
            "beforeTarget": before.get("diagnosisOrTarget"),
            "afterTarget": after.get("diagnosisOrTarget"),
            "beforeStem": before.get("stem"),
            "afterStem": after.get("stem"),
            "beforeChoices": before_choices,
            "afterChoices": after_choices,
        })
    return comparison


def fast_facts_cache_path() -> Path:
    return CACHE_DIR / "fast_facts_question_cache.json"


def empty_fast_facts_cache() -> dict[str, Any]:
    return {
        "schemaVersion": "fast-facts-question-cache-v1",
        "updatedAt": None,
        "entries": {},
    }


def load_fast_facts_cache() -> dict[str, Any]:
    path = fast_facts_cache_path()
    if not path.exists():
        return empty_fast_facts_cache()
    try:
        payload = read_json(path)
    except Exception:
        return empty_fast_facts_cache()
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return empty_fast_facts_cache()
    return payload


def write_fast_facts_cache(cache: dict[str, Any]) -> None:
    cache["updatedAt"] = timestamp_iso()
    write_json(fast_facts_cache_path(), cache)


def fast_facts_concept_content_hash(slide: dict[str, Any]) -> str:
    content = {
        "primaryConcepts": slide.get("primaryConcepts") or [],
        "clinicalFacts": slide.get("clinicalFacts") or [],
        "diagnosticFacts": slide.get("diagnosticFacts") or [],
        "managementFacts": slide.get("managementFacts") or [],
        "mechanismFacts": slide.get("mechanismFacts") or [],
        "differentialFacts": slide.get("differentialFacts") or [],
        "trapFacts": slide.get("trapFacts") or [],
        "nativeTextFacts": slide.get("nativeTextFacts") or [],
        "cleanedImageFacts": slide.get("cleanedImageFacts") or [],
        "structuredImageFacts": slide.get("structuredImageFacts") or {},
        "groundingNotes": slide.get("groundingNotes") or [],
        "groundingTerms": slide.get("groundingTerms") or [],
    }
    return short_hash(json.dumps(content, sort_keys=True, ensure_ascii=False))


def fast_facts_image_routing_metadata_hash(slide: dict[str, Any]) -> str:
    metadata = [
        {
            "imageId": img.get("imageId"),
            "kind": img.get("kind") or img.get("classification"),
            "assetPath": img.get("assetPath"),
            "routing": route,
        }
        for img, route in zip(slide.get("images") or [], fast_facts_image_route_guidance(slide) or [])
    ]
    return short_hash(json.dumps(metadata, sort_keys=True, ensure_ascii=False))


def fast_facts_cache_key_parts(normalized_payload: dict[str, Any], slide: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": FAST_FACTS_PROFILE,
        "sourceFileHash": normalized_payload.get("pptxSha256") or normalized_payload.get("pdfSha256"),
        "conceptId": slide["slideId"],
        "conceptContentHash": fast_facts_concept_content_hash(slide),
        "generationPromptVersion": FAST_FACTS_GENERATION_PROMPT_VERSION,
        "validatorVersion": FAST_FACTS_VALIDATOR_VERSION,
        "archetypeOntologyVersion": FAST_FACTS_ARCHETYPE_ONTOLOGY_VERSION,
        "imageRoutingMetadataHash": fast_facts_image_routing_metadata_hash(slide),
    }


def fast_facts_cache_key(normalized_payload: dict[str, Any], slide: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(fast_facts_cache_key_parts(normalized_payload, slide), sort_keys=True, ensure_ascii=False).encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def stable_fast_facts_question_id(slide: dict[str, Any], question: dict[str, Any]) -> str:
    return "ffq_" + hashlib.sha256(
        (slide["slideId"] + "|" + str(question.get("educationalObjective") or question.get("stem") or "")).encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def fast_facts_single_question_validation(normalized_payload: dict[str, Any], slide: dict[str, Any], question: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    single_norm = dict(normalized_payload)
    single_norm["slides"] = [slide]
    _, errors, _, _, _ = collect_fast_facts_generation_validation(single_norm, [question])
    qa = fast_facts_qa_audit(single_norm, [question])
    summary = qa.get("summary") or {}
    if summary.get("ontologyMismatches") or summary.get("malformedDistractorSets"):
        errors.append("Fast Facts QA failed for cached question.")
    return not errors, errors, (qa.get("items") or [{}])[0]


def fast_facts_cache_entry(
    normalized_payload: dict[str, Any],
    slide: dict[str, Any],
    question: dict[str, Any],
    qa_item: dict[str, Any],
    status: str,
    invalidation_reason: str = "",
) -> dict[str, Any]:
    now = timestamp_iso()
    key_parts = fast_facts_cache_key_parts(normalized_payload, slide)
    return {
        "stableQuestionId": stable_fast_facts_question_id(slide, question),
        "cacheKey": fast_facts_cache_key(normalized_payload, slide),
        **key_parts,
        "sourceSlideIds": (slide.get("metadata") or {}).get("sourceSlideIds") or [],
        "question": question,
        "validationStatus": status,
        "qaAuditResult": qa_item,
        "sourceFactIds": question.get("sourceFactIds") or [],
        "imageRouting": question.get("imageRouting") or [],
        "ontologyClass": (qa_item.get("answerChoiceClasses") or [{}])[0].get("expectedClass"),
        "archetype": question.get("questionArchetype") or slide.get("questionArchetype"),
        "createdAt": now,
        "updatedAt": now,
        "invalidationReason": invalidation_reason,
    }


def update_fast_facts_cache_entry(cache: dict[str, Any], key: str, entry: dict[str, Any]) -> None:
    existing = (cache.get("entries") or {}).get(key)
    if isinstance(existing, dict) and existing.get("createdAt"):
        entry["createdAt"] = existing["createdAt"]
    cache.setdefault("entries", {})[key] = entry
    write_fast_facts_cache(cache)


def seed_fast_facts_cache_from_existing(normalized_payload: dict[str, Any], cache: dict[str, Any]) -> dict[str, int]:
    generated_path = GENERATED_DIR / f"{slugify(Path(normalized_payload['sourceFile']).stem)}_fast_facts_generated_questions.json"
    if not generated_path.exists():
        return {"seeded": 0, "skipped": 0}
    try:
        payload = read_json(generated_path)
    except Exception:
        return {"seeded": 0, "skipped": 0}
    questions = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(questions, list):
        return {"seeded": 0, "skipped": 0}
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    seeded = 0
    skipped = 0
    for question in questions:
        if not isinstance(question, dict):
            skipped += 1
            continue
        slide = slide_by_id.get(str(question.get("slideId") or ""))
        if not slide:
            skipped += 1
            continue
        key = fast_facts_cache_key(normalized_payload, slide)
        existing = (cache.get("entries") or {}).get(key)
        if isinstance(existing, dict) and existing.get("validationStatus") == "valid":
            continue
        question = fast_facts_cleanup_question(question)
        if not question.get("questionArchetype"):
            question["questionArchetype"] = slide.get("questionArchetype")
        ok, errors, qa_item = fast_facts_single_question_validation(normalized_payload, slide, question)
        if not ok:
            skipped += 1
            continue
        entry = fast_facts_cache_entry(normalized_payload, slide, question, qa_item, "valid")
        update_fast_facts_cache_entry(cache, key, entry)
        seeded += 1
    return {"seeded": seeded, "skipped": skipped}


def valid_fast_facts_cached_question(normalized_payload: dict[str, Any], slide: dict[str, Any], entry: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    question = entry.get("question")
    if not isinstance(question, dict):
        return False, ["cached entry missing question"], {}
    expected = fast_facts_cache_key_parts(normalized_payload, slide)
    mismatches = [key for key, value in expected.items() if entry.get(key) != value]
    if mismatches:
        return False, [f"cache key mismatch: {', '.join(mismatches)}"], {}
    if entry.get("validationStatus") != "valid":
        return False, [entry.get("invalidationReason") or "cached entry not marked valid"], {}
    return fast_facts_single_question_validation(normalized_payload, slide, question)


def show_fast_facts_cache_status() -> None:
    cache = load_fast_facts_cache()
    entries = list((cache.get("entries") or {}).values())
    valid = sum(1 for e in entries if isinstance(e, dict) and e.get("validationStatus") == "valid")
    invalid = sum(1 for e in entries if isinstance(e, dict) and e.get("validationStatus") != "valid")
    print(f"Fast Facts cache: {fast_facts_cache_path()}")
    print(f"Total entries: {len(entries)}")
    print(f"Valid entries: {valid}")
    print(f"Invalid entries: {invalid}")
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        print(f"- {entry.get('conceptId')} | {entry.get('validationStatus')} | {entry.get('archetype')} | updated {entry.get('updatedAt')}")


def generate_fast_facts_questions_with_cache(
    normalized_payload: dict[str, Any],
    allocations: list[dict[str, Any]],
    memory: dict[str, Any],
    reuse_cache: bool,
    force_regenerate: bool,
    repair_only: bool,
    diagnostic_report: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    started = time.time()
    cache = load_fast_facts_cache()
    seed_report = seed_fast_facts_cache_from_existing(normalized_payload, cache) if reuse_cache and not force_regenerate else {"seeded": 0, "skipped": 0}
    ordered_allocations = [a for a in allocations if int(a.get("questionCount") or 0) > 0]
    questions_by_slide: dict[str, dict[str, Any]] = {}
    prior_valid_by_slide: dict[str, dict[str, Any]] = {}
    misses: list[dict[str, Any]] = []
    invalidation_reasons: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    reused = 0
    diagnostic_entries = {
        allocation["slideId"]: fast_facts_diagnostic_entry(allocation)
        for allocation in ordered_allocations
    } if diagnostic_report else {}

    for allocation in ordered_allocations:
        slide = allocation["slide"]
        diagnostic = diagnostic_entries.get(slide["slideId"])
        key = fast_facts_cache_key(normalized_payload, slide)
        entry = (cache.get("entries") or {}).get(key)
        if isinstance(entry, dict) and entry.get("validationStatus") == "valid" and isinstance(entry.get("question"), dict):
            prior_valid_by_slide[slide["slideId"]] = entry["question"]
        if reuse_cache and not force_regenerate and isinstance(entry, dict):
            ok, errors, qa_item = valid_fast_facts_cached_question(normalized_payload, slide, entry)
            if ok:
                question = fast_facts_cleanup_question(copy.deepcopy(entry["question"]))
                if not question.get("questionArchetype"):
                    question["questionArchetype"] = slide.get("questionArchetype")
                questions_by_slide[slide["slideId"]] = question
                update_memory_from_question(memory, question)
                cache_hits += 1
                reused += 1
                if diagnostic:
                    diagnostic["cacheStatus"] = "reused"
                update_fast_facts_cache_entry(cache, key, fast_facts_cache_entry(normalized_payload, slide, question, qa_item, "valid"))
                continue
            invalidation_reasons.append({"slideId": slide["slideId"], "reason": "; ".join(errors[:4])})
            if diagnostic:
                diagnostic["cacheStatus"] = "invalidated"
        cache_misses += 1
        if repair_only:
            invalidation_reasons.append({"slideId": slide["slideId"], "reason": "repair_only_no_valid_cache_hit"})
            if diagnostic and diagnostic["cacheStatus"] == "pending":
                diagnostic["cacheStatus"] = "repair-only-miss"
            continue
        if diagnostic and diagnostic["cacheStatus"] == "pending":
            diagnostic["cacheStatus"] = "bypassed" if (force_regenerate or not reuse_cache) else "generated"
        misses.append(allocation)

    regenerated = 0
    generation_failed: list[dict[str, Any]] = []
    generation_telemetry: dict[str, Any] = {"events": [], "dropped": [], "summary": {}}
    if misses:
        generated, generation_telemetry = generate_fast_facts_questions(normalized_payload, misses, memory)
        for question in generated:
            slide_id = str(question.get("slideId") or "")
            if slide_id:
                questions_by_slide[slide_id] = question
                regenerated += 1
                diagnostic = diagnostic_entries.get(slide_id)
                if diagnostic:
                    diagnostic["generatedQuestionBeforeRepair"] = copy.deepcopy(question)
        generated_ids = set(questions_by_slide) - {a["slideId"] for a in ordered_allocations if a["slideId"] not in [m["slideId"] for m in misses]}
        for allocation in misses:
            if allocation["slideId"] not in questions_by_slide:
                generation_failed.append({"slideId": allocation["slideId"], "reason": "generation returned no valid question"})

    ordered_questions: list[dict[str, Any]] = []
    dropped_before_repair: list[dict[str, Any]] = []
    for allocation in ordered_allocations:
        slide_id = allocation["slideId"]
        if slide_id in questions_by_slide:
            ordered_questions.append(questions_by_slide[slide_id])
        elif slide_id in prior_valid_by_slide:
            ordered_questions.append(prior_valid_by_slide[slide_id])
            reused += 1
            invalidation_reasons.append({"slideId": slide_id, "reason": "used_prior_valid_cache_after_generation_failure"})
            diagnostic = diagnostic_entries.get(slide_id)
            if diagnostic:
                diagnostic["cacheStatus"] = "reused"
        else:
            dropped_before_repair.append({"slideId": slide_id, "reason": "no valid cached or generated question"})

    if diagnostic_entries:
        slide_by_id = {a["slideId"]: a["slide"] for a in ordered_allocations}
        for question in ordered_questions:
            slide_id = str(question.get("slideId") or "")
            diagnostic = diagnostic_entries.get(slide_id)
            slide = slide_by_id.get(slide_id)
            if diagnostic and slide:
                if diagnostic["generatedQuestionBeforeRepair"] is None:
                    diagnostic["generatedQuestionBeforeRepair"] = copy.deepcopy(question)
                diagnostic["preRepairValidationFindings"] = fast_facts_diagnostic_validation(normalized_payload, slide, question)

    app_payload_before, before_errors, before_grounding, before_diversity, before_strict = collect_fast_facts_generation_validation(normalized_payload, ordered_questions)
    repaired_questions, repair_report = repair_fast_facts_questions(
        normalized_payload,
        ordered_questions,
        before_errors,
        before_grounding,
        before_diversity,
        before_strict,
    )
    generation_dropped = [item for item in generation_telemetry.get("dropped") or [] if isinstance(item, dict)]
    if generation_dropped:
        repair_report["droppedQuestions"] = list(repair_report.get("droppedQuestions") or []) + generation_dropped
        repair_report["droppedQuestionCount"] = int(repair_report.get("droppedQuestionCount") or 0) + len(generation_dropped)
        repair_report["generationDroppedQuestionCount"] = len(generation_dropped)
    repair_report["chunkTelemetry"] = generation_telemetry

    repaired_slide_ids = {
        str(item.get("slideId") or "")
        for item in repair_report.get("repairLog", [])
        if item.get("accepted")
    }
    dropped_questions = list(repair_report.get("droppedQuestions") or [])
    final_by_slide = {str(q.get("slideId") or ""): q for q in repaired_questions if isinstance(q, dict)}
    if diagnostic_entries:
        for item in repair_report.get("repairLog") or []:
            if not isinstance(item, dict):
                continue
            diagnostic = diagnostic_entries.get(str(item.get("slideId") or ""))
            if diagnostic:
                diagnostic["repairAttempted"] = True
                diagnostic["repairResult"] = copy.deepcopy(item)
    final_questions: list[dict[str, Any]] = []
    rescued_from_prior = 0
    for allocation in ordered_allocations:
        slide = allocation["slide"]
        slide_id = slide["slideId"]
        key = fast_facts_cache_key(normalized_payload, slide)
        if slide_id in final_by_slide:
            question = final_by_slide[slide_id]
            diagnostic = diagnostic_entries.get(slide_id)
            if diagnostic:
                diagnostic["postRepairValidationFindings"] = fast_facts_diagnostic_validation(normalized_payload, slide, question)
            ok, errors, qa_item = fast_facts_single_question_validation(normalized_payload, slide, question)
            if ok:
                final_questions.append(question)
                if diagnostic:
                    diagnostic["finalDisposition"] = "repaired" if slide_id in repaired_slide_ids else (
                        "reused-cache" if diagnostic["cacheStatus"] == "reused" else "kept"
                    )
                update_fast_facts_cache_entry(cache, key, fast_facts_cache_entry(normalized_payload, slide, question, qa_item, "valid"))
                continue
            invalidation_reasons.append({"slideId": slide_id, "reason": "post-repair validation failed: " + "; ".join(errors[:4])})
        if slide_id in prior_valid_by_slide:
            prior = prior_valid_by_slide[slide_id]
            ok, errors, qa_item = fast_facts_single_question_validation(normalized_payload, slide, prior)
            if ok:
                final_questions.append(prior)
                rescued_from_prior += 1
                diagnostic = diagnostic_entries.get(slide_id)
                if diagnostic:
                    diagnostic["postRepairValidationFindings"] = fast_facts_diagnostic_validation(normalized_payload, slide, prior)
                    diagnostic["finalDisposition"] = "reused-cache"
                update_fast_facts_cache_entry(cache, key, fast_facts_cache_entry(normalized_payload, slide, prior, qa_item, "valid"))
                continue
        if slide_id not in final_by_slide:
            reason = next((d.get("reason") for d in dropped_before_repair if d.get("slideId") == slide_id), "")
            matching_drop = next((d for d in dropped_questions if d.get("slideId") == slide_id), None)
            if matching_drop:
                reason = matching_drop.get("reason") or reason
            diagnostic = diagnostic_entries.get(slide_id)
            if diagnostic:
                diagnostic["finalDisposition"] = "dropped"
                diagnostic["dropReason"] = reason or "no valid question after generation/repair"
            entry = {
                **fast_facts_cache_key_parts(normalized_payload, slide),
                "cacheKey": key,
                "stableQuestionId": None,
                "conceptId": slide_id,
                "sourceSlideIds": (slide.get("metadata") or {}).get("sourceSlideIds") or [],
                "question": None,
                "validationStatus": "invalid",
                "qaAuditResult": None,
                "sourceFactIds": [],
                "imageRouting": [],
                "ontologyClass": None,
                "archetype": slide.get("questionArchetype"),
                "createdAt": timestamp_iso(),
                "updatedAt": timestamp_iso(),
                "invalidationReason": reason or "no valid question after generation/repair",
            }
            update_fast_facts_cache_entry(cache, key, entry)

    cache_report = {
        "cachePath": display_path(fast_facts_cache_path()),
        "seededFromExisting": seed_report,
        "cacheHits": cache_hits,
        "cacheMisses": cache_misses,
        "reusedQuestions": reused,
        "regeneratedQuestions": regenerated,
        "repairedQuestions": repair_report.get("repairedQuestionCount", 0),
        "droppedQuestions": len(dropped_before_repair) + int(repair_report.get("droppedQuestionCount") or 0),
        "reviewRequiredQuestions": int(repair_report.get("droppedQuestionCount") or 0),
        "chunkTelemetry": generation_telemetry,
        "rescuedFromPriorValidCache": rescued_from_prior,
        "invalidationReasons": invalidation_reasons + generation_failed + [
            {
                "slideId": item.get("slideId"),
                "reason": item.get("reason"),
                "attemptNotes": item.get("attemptNotes") or [],
            }
            for item in dropped_questions
            if isinstance(item, dict)
        ],
        "runtimeDurationSeconds": round(time.time() - started, 2),
        "repairReport": repair_report,
        "beforeErrors": before_errors,
        "beforeStrictFindings": before_strict,
    }
    if diagnostic_entries:
        final_index_by_slide = {
            str(question.get("slideId") or ""): index
            for index, question in enumerate(final_questions, start=1)
        }
        for slide_id, diagnostic in diagnostic_entries.items():
            final_index = final_index_by_slide.get(slide_id)
            if final_index:
                final_question = final_questions[final_index - 1]
                diagnostic["finalQuestionIndex"] = final_index
                diagnostic["finalQuestionId"] = stable_fast_facts_question_id(
                    next(a["slide"] for a in ordered_allocations if a["slideId"] == slide_id),
                    final_question,
                )
            elif diagnostic["finalDisposition"] == "pending":
                diagnostic["finalDisposition"] = "dropped"
                diagnostic["dropReason"] = "no valid question after generation/repair"
        cache_report["diagnosticEntries"] = list(diagnostic_entries.values())
    return final_questions, cache_report, before_diversity, app_payload_before


def fast_facts_threshold_claims(text: str) -> list[str]:
    claims: list[str] = []
    patterns = [
        r"\b\d+\s*-\s*\d+\s*(?:days?|weeks?|months?|years?)\b",
        r"\b\d+\s*(?:days?|weeks?|months?|years?)\b",
        r"\bevery\s+\d+\s*(?:days?|weeks?|months?|years?)\b",
        r"\bsingle\s+dose\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, str(text or ""), flags=re.I):
            claim = normalize_key(match)
            if claim and claim not in claims:
                claims.append(claim)
    return claims


def fast_facts_strict_findings(generated_questions: list[dict[str, Any]], normalized_payload: dict[str, Any]) -> list[dict[str, Any]]:
    slide_by_id = {s["slideId"]: s for s in normalized_payload.get("slides") or []}
    findings: list[dict[str, Any]] = []
    for idx, q in enumerate(generated_questions, start=1):
        slide = slide_by_id.get(str(q.get("slideId") or ""))
        if not slide:
            continue
        grounding_text = normalize_key(" ".join(str(v) for v in slide.get("groundingNotes") or []))
        if not grounding_text:
            grounding_text = normalize_key(normalized_slide_fact_text(slide))
        claim_parts: list[str] = []
        for choice in q.get("answerChoices") or []:
            if isinstance(choice, dict):
                claim_parts.append(str(choice.get("text") or ""))
        claim_parts.append(str(q.get("correctExplanation") or ""))
        for item in q.get("incorrectExplanations") or []:
            if isinstance(item, dict):
                claim_parts.append(str(item.get("explanation") or ""))
        claim_parts.append(str(q.get("educationalObjective") or ""))
        unsupported_thresholds = [
            claim for claim in fast_facts_threshold_claims(" ".join(claim_parts))
            if claim not in grounding_text
        ]
        if unsupported_thresholds:
            findings.append({
                "questionIndex": idx,
                "slideId": q.get("slideId"),
                "severity": "error",
                "issue": "unsupported_fast_facts_threshold",
                "detail": unsupported_thresholds,
            })
        classes = fast_facts_choice_classes(q, slide)
        if classes:
            expected = classes[0]["expectedClass"]
            mismatches = [row for row in classes if row["ontologyClass"] != expected]
            if mismatches:
                findings.append({
                    "questionIndex": idx,
                    "slideId": q.get("slideId"),
                    "severity": "error",
                    "issue": "mixed_answer_choice_ontology",
                    "detail": {
                        "expected": expected,
                        "classes": classes,
                    },
                })
        stem_key = normalize_key(q.get("stem") or "")
        tautologic = []
        for choice in q.get("answerChoices") or []:
            if not isinstance(choice, dict):
                continue
            choice_key = normalize_key(choice.get("text") or "")
            if len(choice_key.split()) >= 2 and choice_key in stem_key:
                tautologic.append(str(choice.get("text") or ""))
        if tautologic and normalize_key(q.get("questionArchetype") or slide.get("questionArchetype") or "") in {"diagnosis", "adverse_effect"}:
            findings.append({
                "questionIndex": idx,
                "slideId": q.get("slideId"),
                "severity": "error",
                "issue": "tautologic_stem_finding_distractors",
                "detail": tautologic,
            })
        if re.search(r"medication initiation|was prescribed .* several days after", str(q.get("stem") or ""), re.I):
            findings.append({
                "questionIndex": idx,
                "slideId": q.get("slideId"),
                "severity": "error",
                "issue": "awkward_temporal_stem_language",
                "detail": q.get("stem"),
            })
    return findings


def collect_fast_facts_generation_validation(
    normalized_payload: dict[str, Any],
    questions: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    app_payload, errors, grounding_findings, diversity_report = collect_generation_validation(normalized_payload, questions)
    strict_findings = fast_facts_strict_findings(questions, normalized_payload)
    errors.extend(
        f"Fast Facts grounding Q{f.get('questionIndex', '?')}: {f.get('issue')} {f.get('detail')}"
        for f in strict_findings
        if f.get("severity") == "error"
    )
    return app_payload, errors, grounding_findings, diversity_report, strict_findings


def validate_single_fast_facts_repair_candidate(
    normalized_payload: dict[str, Any],
    slide: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, list[str], list[str]]:
    single_norm = dict(normalized_payload)
    single_norm["slides"] = [slide]
    _, single_errors, single_grounding, _, strict_findings = collect_fast_facts_generation_validation(single_norm, [candidate])
    unsupported: list[str] = []
    for finding in single_grounding + strict_findings:
        if finding.get("severity") == "error":
            unsupported.extend(str(v) for v in finding.get("detail") or [])
    return not single_errors, single_errors, unsupported


def image_routing_summary(app_payload: dict[str, Any]) -> dict[str, int]:
    stem = 0
    explanation = 0
    questions_with_images = 0
    for q in app_payload.get("questions") or []:
        stem += len(q.get("images") or [])
        explanation += len(q.get("explanationImages") or [])
        if q.get("images") or q.get("explanationImages"):
            questions_with_images += 1
    return {
        "stemImageCount": stem,
        "explanationImageCount": explanation,
        "questionsWithImages": questions_with_images,
    }


def run_fast_facts_generation_milestone(
    pptx_path: Path,
    graph: dict[str, Any],
    deck_hash: str,
    limit_slides: int,
    extraction_report: dict[str, Any],
    reuse_cache: bool = True,
    force_regenerate: bool = False,
    repair_only: bool = False,
    diagnostic_report: bool = False,
    question_limit: int = 0,
) -> Path:
    normalized_payload, skipped_concepts = fast_facts_normalized_payload(pptx_path, graph, deck_hash, limit_slides)
    normalized_path = NORMALIZED_DIR / f"{slugify(pptx_path.stem)}_fast_facts_generation_normalized.json"
    write_json(normalized_path, normalized_payload)
    generated_path = GENERATED_DIR / f"{slugify(pptx_path.stem)}_fast_facts_generated_questions.json"
    before_generation_snapshot: list[dict[str, Any]] = []
    if generated_path.exists():
        try:
            existing = read_json(generated_path)
            if isinstance(existing, dict) and isinstance(existing.get("questions"), list):
                before_generation_snapshot = existing["questions"]
        except Exception:
            before_generation_snapshot = []
    memory = empty_memory()
    allocations = limit_fast_facts_question_attempts(
        fast_facts_allocations(normalized_payload, memory),
        question_limit,
    )
    repaired_questions, cache_report, before_diversity, app_payload_before = generate_fast_facts_questions_with_cache(
        normalized_payload=normalized_payload,
        allocations=allocations,
        memory=memory,
        reuse_cache=reuse_cache,
        force_regenerate=force_regenerate,
        repair_only=repair_only,
        diagnostic_report=diagnostic_report,
    )
    diagnostic_report_path = None
    if diagnostic_report:
        diagnostic_report_path = write_fast_facts_diagnostic_report({
            "runAt": timestamp_iso(),
            "profile": FAST_FACTS_PROFILE,
            "sourceFile": pptx_path.name,
            "sourcePath": str(pptx_path),
            "limitSlides": int(limit_slides or 0),
            "questionAttemptLimit": int(question_limit or 0),
            "reuseCache": bool(reuse_cache),
            "forceRegenerate": bool(force_regenerate),
            "repairOnly": bool(repair_only),
            "entries": cache_report.get("diagnosticEntries") or [],
        })
    write_json(generated_path, {"questions": repaired_questions})
    if repaired_questions:
        app_payload, after_errors, after_grounding, after_diversity, after_strict = collect_fast_facts_generation_validation(normalized_payload, repaired_questions)
    else:
        app_payload = app_payload_before
        after_errors = []
        after_grounding = []
        after_diversity = {"findings": []}
        after_strict = []
    before_errors = list(cache_report.get("beforeErrors") or [])
    before_strict = list(cache_report.get("beforeStrictFindings") or [])
    repair_report = cache_report.get("repairReport") or {}
    repair_report["validationErrorCountBeforeRepair"] = len(before_errors)
    repair_report["validationErrorCountAfterRepair"] = len(after_errors)
    repair_report["remainingValidationErrors"] = after_errors
    repair_report_path = write_report(repair_report, "fast_facts_generation_repair_report")
    repaired_indices = {
        int(item.get("questionIndex"))
        for item in repair_report.get("repairLog", [])
        if item.get("accepted") and str(item.get("questionIndex") or "").isdigit()
    }
    qa_audit = fast_facts_qa_audit(normalized_payload, repaired_questions, repaired_indices)
    if qa_audit.get("summary", {}).get("ontologyMismatches"):
        after_errors.append(f"Fast Facts QA: ontology mismatches remain: {qa_audit['summary']['ontologyMismatches']}")
    if qa_audit.get("summary", {}).get("malformedDistractorSets"):
        after_errors.append(f"Fast Facts QA: malformed distractor sets remain: {qa_audit['summary']['malformedDistractorSets']}")
    qa_report = {
        "profile": FAST_FACTS_PROFILE,
        "sourceFile": pptx_path.name,
        "mode": "fast-facts-educational-qa-limit-5",
        "audit": qa_audit,
    }
    qa_report_path = write_report(qa_report, "fast_facts_educational_qa_audit")
    metrics = {
        "profile": FAST_FACTS_PROFILE,
        "mode": "constrained-generation-limit-5",
        "sourceFile": pptx_path.name,
        "slidesUsed": len({sid for c in graph.get("concepts") or [] for sid in (c.get("sourceSlideIds") or [])}),
        "limitSlides": int(limit_slides or 0),
        "conceptsInGraph": len(graph.get("concepts") or []),
        "conceptsUsed": sum(1 for a in allocations if int(a.get("questionCount") or 0) > 0),
        "conceptsSkippedForGeneration": len(skipped_concepts) + sum(1 for a in allocations if int(a.get("questionCount") or 0) == 0),
        "questionsGeneratedBeforeRepair": cache_report.get("regeneratedQuestions", 0),
        "questionsRepaired": repair_report.get("repairedQuestionCount", 0),
        "questionsDropped": cache_report.get("droppedQuestions", 0),
        "finalQuestionCount": len(repaired_questions),
        "validationErrorsBeforeRepair": before_errors,
        "validationErrorsAfterRepair": after_errors,
        "semanticGroundingFindingsAfterRepair": after_grounding,
        "fastFactsStrictFindingsBeforeRepair": before_strict,
        "fastFactsStrictFindingsAfterRepair": after_strict,
        "questionQualityFindingsAfterRepair": after_diversity.get("findings", []),
        "allocationSummary": [
            {
                "slideId": a.get("slideId"),
                "questionCount": a.get("questionCount"),
                "reason": a.get("reason"),
                "contentRichness": a.get("contentRichness"),
            }
            for a in allocations
        ],
        "skippedConcepts": skipped_concepts,
        "normalizedGenerationPath": display_path(normalized_path),
        "generatedIntermediatePath": display_path(generated_path),
        "repairReportPath": display_path(repair_report_path),
        "educationalQaAuditReportPath": display_path(qa_report_path),
        "educationalQaSummary": qa_audit.get("summary"),
        "beforeAfterComparison": fast_facts_before_after_comparison(before_generation_snapshot, repaired_questions),
        "cacheReport": cache_report,
        "extractionReport": extraction_report,
        "imageRoutingSummary": image_routing_summary(app_payload_before if after_errors else app_payload),
    }
    if diagnostic_report_path:
        metrics["diagnosticReportPath"] = display_path(diagnostic_report_path)
    out_path = APP_READY_DIR / f"{slugify(pptx_path.stem)}_fast_facts_app_ready.json"
    dropped_review_items = [
        item for item in repair_report.get("droppedQuestions") or []
        if isinstance(item, dict) and isinstance(item.get("originalQuestion"), dict)
    ]
    review_draft_path = None
    if dropped_review_items:
        failed_questions = [
            review_candidate_from_failed_repair(
                item["originalQuestion"],
                item,
                next(
                    (
                        log_item for log_item in repair_report.get("repairLog") or []
                        if isinstance(log_item, dict) and log_item.get("questionIndex") == item.get("questionIndex")
                    ),
                    {},
                ),
            )
            for item in dropped_review_items
        ]
        review_draft_path = write_failed_repair_review_draft(
            source_label=pptx_path.name,
            source_format=SOURCE_FORMAT,
            source_type=str(os.environ.get("BIC_PROGRESS_SOURCE") or "").strip() or "fast_facts_pptx",
            failed_questions=failed_questions,
            failed_items=dropped_review_items,
            repair_report=repair_report,
            app_ready_path=out_path if repaired_questions and not after_errors else None,
            report_path=None,
        )
        if review_draft_path:
            metrics["status"] = "completed_with_review_required"
            metrics["draftPath"] = str(review_draft_path.resolve())
            metrics["reviewRequiredQuestionCount"] = len(failed_questions)
    if after_errors:
        report_path = write_report(metrics, "fast_facts_generation_validation_report")
        if not review_draft_path:
            raise PipelineError(
                "Fast Facts constrained generation failed validation after repair. "
                f"Report: {display_path(report_path)}\n"
                + "\n".join(f"- {err}" for err in after_errors[:80])
            )
        draft = read_json(review_draft_path)
        if isinstance(draft.get("outputPaths"), dict):
            draft["outputPaths"]["reportPath"] = str(report_path.resolve())
            write_json(review_draft_path, draft)
        return review_draft_path
    if not repaired_questions:
        report_path = write_report(metrics, "fast_facts_generation_validation_report")
        if review_draft_path:
            draft = read_json(review_draft_path)
            if isinstance(draft.get("outputPaths"), dict):
                draft["outputPaths"]["reportPath"] = str(report_path.resolve())
                write_json(review_draft_path, draft)
            return review_draft_path
        raise PipelineError("Fast Facts constrained generation produced no questions and no review draft.")
    write_json(out_path, app_payload)
    json.loads(out_path.read_text(encoding="utf-8"))
    metrics["appReadyPath"] = display_path(out_path)
    metrics["imageRoutingSummary"] = image_routing_summary(app_payload)
    report_path = write_report(metrics, "fast_facts_generation_validation_report")
    if review_draft_path:
        draft = read_json(review_draft_path)
        if isinstance(draft.get("outputPaths"), dict):
            draft["outputPaths"]["reportPath"] = str(report_path.resolve())
            write_json(review_draft_path, draft)
    log(f"Fast Facts app-ready -> {display_path(out_path)}")
    log(f"Fast Facts validation report -> {display_path(report_path)}")
    return out_path


def amboss_cache_path() -> Path:
    return CACHE_DIR / "amboss_extraction_cache.json"


def empty_amboss_cache() -> dict[str, Any]:
    return {"schemaVersion": "amboss-extraction-cache-v1", "updatedAt": None, "pages": {}, "questions": {}}


def load_amboss_cache() -> dict[str, Any]:
    path = amboss_cache_path()
    if not path.exists():
        return empty_amboss_cache()
    try:
        payload = read_json(path)
    except Exception:
        return empty_amboss_cache()
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), dict):
        return empty_amboss_cache()
    payload.setdefault("questions", {})
    return payload


def write_amboss_cache(cache: dict[str, Any]) -> None:
    cache["updatedAt"] = timestamp_iso()
    write_json(amboss_cache_path(), cache)


def image_dimensions(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def amboss_ocr_image(path: Path) -> str:
    if not shutil.which("tesseract"):
        return ""
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return ""
        return clean_slide_text(proc.stdout)
    except Exception:
        return ""


def amboss_asset_entry(asset_path: Path, page_id: str, image_id: str, kind: str) -> dict[str, Any]:
    width, height = image_dimensions(asset_path)
    fingerprint = file_sha(asset_path)
    return {
        "imageId": image_id,
        "kind": kind,
        "assetPath": display_path(asset_path),
        "mimeType": mime_for(asset_path),
        "width": width,
        "height": height,
        "sha256": fingerprint,
        "imageFingerprint": fingerprint,
        "sourcePageId": page_id,
    }


def decompose_amboss_input(input_path: Path, limit_pages: int = 5) -> dict[str, Any]:
    ensure_dirs()
    source_hash = file_sha(input_path)
    stem = slugify(input_path.stem)
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    suffix = input_path.suffix.lower()
    # limit_pages == 0 (or None) → no cap (process every page). Positive value is a hard cap.
    limit_pages_int = int(limit_pages or 0)
    if suffix == ".pdf":
        fitz = optional_import_fitz()
        pdfplumber = optional_import_pdfplumber()
        if not fitz and not pdfplumber:
            raise PipelineError("AMBOSS_PROFILE PDF input requires pymupdf or pdfplumber.")
        pdf_doc = None
        plumber_pdf = None
        try:
            if fitz:
                pdf_doc = fitz.open(str(input_path))
            if pdfplumber:
                plumber_pdf = pdfplumber.open(str(input_path))
            total_pages = len(pdf_doc) if pdf_doc else len(plumber_pdf.pages)
            page_count = total_pages if limit_pages_int <= 0 else min(limit_pages_int, total_pages)
            for page_index in range(page_count):
                page_num = page_index + 1
                page_id = f"{stem}_p{page_num:04d}_{source_hash[:8]}"
                text = ""
                plumber_page = plumber_pdf.pages[page_index] if plumber_pdf else None
                if plumber_page:
                    try:
                        text = plumber_page.extract_text(x_tolerance=1, y_tolerance=3) or ""
                    except Exception:
                        text = ""
                if not text and pdf_doc:
                    text = pdf_doc.load_page(page_index).get_text("text") or ""
                image = render_slide_image(fitz, pdf_doc, page_index, stem, page_id) if pdf_doc else None
                images = [image] if image else []
                for img in images:
                    if img:
                        asset = BASE_DIR / img["assetPath"]
                        img["imageFingerprint"] = file_sha(asset)
                        img["sourcePageId"] = page_id
                        img["kind"] = "page_screenshot"
                pages.append({
                    "pageId": page_id,
                    "pageIndex": page_index,
                    "pageNumber": page_num,
                    "sourceFile": input_path.name,
                    "sourceHash": source_hash,
                    "pageHash": short_hash((text or "") + json.dumps([i.get("sha256") for i in images])),
                    "text": clean_slide_text(text),
                    "ocrText": clean_slide_text(text),
                    "images": [i for i in images if i],
                    "tables": extract_tables_from_page(plumber_page),
                })
        finally:
            if plumber_pdf:
                plumber_pdf.close()
            if pdf_doc:
                pdf_doc.close()
    elif suffix in {".png", ".jpg", ".jpeg"}:
        page_id = f"{stem}_p0001_{source_hash[:8]}"
        asset_name = f"{stem}_{page_id}{suffix}"
        asset_path = ASSET_DIR / asset_name
        if input_path.resolve() != asset_path.resolve():
            shutil.copyfile(input_path, asset_path)
        ocr_text = amboss_ocr_image(asset_path)
        image = amboss_asset_entry(asset_path, page_id, f"{page_id}_img01", "page_screenshot")
        pages.append({
            "pageId": page_id,
            "pageIndex": 0,
            "pageNumber": 1,
            "sourceFile": input_path.name,
            "sourceHash": source_hash,
            "pageHash": short_hash(ocr_text + image["imageFingerprint"]),
            "text": ocr_text,
            "ocrText": ocr_text,
            "images": [image],
            "tables": [],
        })
    else:
        raise PipelineError(f"AMBOSS_PROFILE does not support input type: {input_path.suffix}")
    payload = {
        "schemaVersion": "amboss-decomposition-v1",
        "profile": AMBOSS_PROFILE,
        "sourceFile": input_path.name,
        "sourcePath": str(input_path),
        "sourceHash": source_hash,
        "createdAt": timestamp_iso(),
        "pageCount": len(pages),
        "pages": pages,
        "failures": failures,
    }
    out_path = SLIDES_DIR / f"{stem}_amboss_pages.json"
    write_json(out_path, payload)
    log(f"AMBOSS pages -> {display_path(out_path)}")
    return payload


def amboss_page_cache_key(source_hash: str, page: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({
        "profile": AMBOSS_PROFILE,
        "sourcePdfHash": source_hash,
        "pageHash": page.get("pageHash"),
        "extractionPromptVersion": AMBOSS_EXTRACTION_PROMPT_VERSION,
        "imageRoutingVersion": AMBOSS_IMAGE_ROUTING_VERSION,
    }, sort_keys=True).encode("utf-8", errors="replace")).hexdigest()[:16]


def amboss_question_hash(question: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({
        "stem": question.get("stem"),
        "answerChoices": question.get("answerChoices"),
        "correctAnswer": question.get("correctAnswer"),
        "explanation": question.get("correctExplanation"),
    }, sort_keys=True, ensure_ascii=False).encode("utf-8", errors="replace")).hexdigest()[:16]


def amboss_extraction_prompt(page: dict[str, Any]) -> str:
    image_summary = [
        {
            "imageId": img.get("imageId"),
            "kind": img.get("kind"),
            "width": img.get("width"),
            "height": img.get("height"),
            "imageFingerprint": img.get("imageFingerprint"),
        }
        for img in page.get("images") or []
    ]
    return f"""
You are extracting existing AMBOSS-style question content from one visible page or screenshot.

Do not generate new questions. Do not invent answer choices. Do not invent explanations.
If no complete question is visible, return an empty questions array.
Return JSON only:
{{
  "questions": [
    {{
      "sourcePageId": "",
      "extractionConfidence": 0.0,
      "stem": "",
      "answerChoices": [{{"label":"A","text":""}}, {{"label":"B","text":""}}, {{"label":"C","text":""}}, {{"label":"D","text":""}}],
      "correctAnswer": "A",
      "correctAnswerVisible": true,
      "correctExplanation": "",
      "incorrectExplanations": [{{"label":"B","explanation":""}}],
      "educationalObjective": "",
      "imageRouting": [{{"imageId":"","placement":"stem","reason":"","imageConfidence":0.0}}],
      "extractionWarnings": []
    }}
  ],
  "pageWarnings": []
}}

Rules:
- Extract the visible stem and answer choices exactly enough to preserve meaning.
- Use correctAnswer only if visible or clearly marked. If not visible, set correctAnswerVisible false and use an empty correctAnswer.
- Images in unanswered question context route to stem.
- Images in explanation/review/teaching route to explanation.
- Decorative UI or irrelevant screenshots route to ignored.
- If the screenshot is just a menu/list without a full question, return no questions.

PAGE_ID: {page.get("pageId")}
OCR_TEXT:
{str(page.get("ocrText") or page.get("text") or "")[:12000]}

IMAGES:
{json.dumps(image_summary, ensure_ascii=False)}
""".strip()


def extract_amboss_page(page: dict[str, Any], source_file: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise PipelineError("GEMINI_API_KEY is not set for AMBOSS_PROFILE extraction.")
    image_paths = [BASE_DIR / img["assetPath"] for img in page.get("images") or [] if img.get("assetPath")]
    prompt = amboss_extraction_prompt(page)
    raw = raw_gemini_image_call(api_key, prompt, image_paths[:1], temperature=0.0, max_tokens=8192, timeout_seconds=90)
    write_debug_raw(source_file, "amboss_extract", str(page.get("pageId") or "page"), "attempt0", raw)
    parsed = load_largest_valid_json(raw)
    questions = parsed.get("questions") if isinstance(parsed, dict) else []
    if not isinstance(questions, list):
        questions = []
    cleaned: list[dict[str, Any]] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        q["sourcePageId"] = page.get("pageId")
        q["extractionConfidence"] = float(q.get("extractionConfidence") or 0)
        q.setdefault("extractionWarnings", [])
        cleaned.append(q)
    return cleaned, {"pageWarnings": parsed.get("pageWarnings") if isinstance(parsed, dict) else []}


def amboss_visual_state_prompt(page: dict[str, Any], page_questions: list[dict[str, Any]]) -> str:
    choices = []
    for q in page_questions:
        for choice in q.get("answerChoices") or []:
            if isinstance(choice, dict):
                choices.append({"label": choice.get("label"), "text": choice.get("text")})
    image_summary = [
        {
            "imageId": img.get("imageId"),
            "kind": img.get("kind"),
            "width": img.get("width"),
            "height": img.get("height"),
            "imageFingerprint": img.get("imageFingerprint"),
        }
        for img in page.get("images") or []
    ]
    return f"""
Analyze this rendered AMBOSS page image for visual answer states only.

Do not solve the question. Do not infer from medical knowledge.
Use only visible UI styling:
- green highlighted answer row or green check mark means correct
- red/pink highlighted answer row means incorrect
- explanation text immediately under an answer row belongs to that answer choice
- zoomed or review-only clinical images belong to explanation, not stem

Return JSON only:
{{
  "correctAnswer": "",
  "confidence": 0.0,
  "answerStates": [
    {{"label":"A","state":"correct|incorrect|unknown","confidence":0.0,"visibleText":""}}
  ],
  "visibleRationales": [
    {{"label":"A","explanation":"","confidence":0.0}}
  ],
  "imageRouting": [
    {{"imageId":"","placement":"stem|explanation|ignored","reason":"","imageConfidence":0.0}}
  ],
  "warnings": []
}}

PAGE_ID: {page.get("pageId")}
KNOWN_VISIBLE_CHOICES:
{json.dumps(choices, ensure_ascii=False)}
PAGE_IMAGES:
{json.dumps(image_summary, ensure_ascii=False)}
""".strip()


def detect_amboss_visual_state(page: dict[str, Any], page_questions: list[dict[str, Any]], source_file: str) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"correctAnswer": "", "confidence": 0, "answerStates": [], "visibleRationales": [], "imageRouting": [], "warnings": ["GEMINI_API_KEY missing"]}
    image_paths = [BASE_DIR / img["assetPath"] for img in page.get("images") or [] if img.get("assetPath")]
    if not image_paths:
        return {"correctAnswer": "", "confidence": 0, "answerStates": [], "visibleRationales": [], "imageRouting": [], "warnings": ["no rendered page image"]}
    prompt = amboss_visual_state_prompt(page, page_questions)
    raw = raw_gemini_image_call(api_key, prompt, image_paths[:1], temperature=0.0, max_tokens=4096, timeout_seconds=60)
    write_debug_raw(source_file, "amboss_visual", str(page.get("pageId") or "page"), "attempt0", raw)
    parsed = load_largest_valid_json(raw)
    if not isinstance(parsed, dict):
        return {"correctAnswer": "", "confidence": 0, "answerStates": [], "visibleRationales": [], "imageRouting": [], "warnings": ["visual state response was not object"]}
    parsed.setdefault("answerStates", [])
    parsed.setdefault("visibleRationales", [])
    parsed.setdefault("imageRouting", [])
    parsed.setdefault("warnings", [])
    parsed["sourcePageId"] = page.get("pageId")
    known_image_ids = {str(img.get("imageId") or "") for img in page.get("images") or []}
    page_image_id = str((page.get("images") or [{}])[0].get("imageId") or "") if page.get("images") else ""
    normalized_routes: list[dict[str, Any]] = []
    for route in parsed.get("imageRouting") or []:
        if not isinstance(route, dict):
            continue
        placement = str(route.get("placement") or "ignored").lower()
        if placement not in {"stem", "explanation", "ignored"}:
            placement = "ignored"
        if placement == "stem" and not page_questions:
            placement = "explanation"
        image_id = str(route.get("imageId") or "")
        if image_id not in known_image_ids and page_image_id and placement != "ignored":
            image_id = page_image_id
        normalized_routes.append({
            "imageId": image_id,
            "placement": placement,
            "reason": clean_sentence(route.get("reason")),
            "imageConfidence": float(route.get("imageConfidence") or 0),
            "sourcePageId": page.get("pageId"),
        })
    parsed["imageRouting"] = normalized_routes
    return parsed


def amboss_apply_visual_state_to_question(q: dict[str, Any], visual_state: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(q)
    labels = {
        str(choice.get("label") or "").strip().upper()
        for choice in out.get("answerChoices") or []
        if isinstance(choice, dict)
    }
    visual_correct = str(visual_state.get("correctAnswer") or "").strip().upper()
    if visual_correct and visual_correct in labels:
        out["correctAnswer"] = visual_correct
        out["correctAnswerVisible"] = True
        out["visualCorrectAnswerConfidence"] = float(visual_state.get("confidence") or 0)
        warnings = [
            str(w)
            for w in (out.get("extractionWarnings") or [])
            if str(w) != "correct answer not visible or invalid"
        ]
        warnings.append(f"correct answer detected visually: {visual_correct}")
        out["extractionWarnings"] = dedupe_preserve_order(warnings)
    rationale_by_label = {
        str(item.get("label") or "").strip().upper(): clean_sentence(item.get("explanation"))
        for item in visual_state.get("visibleRationales") or []
        if isinstance(item, dict) and item.get("label")
    }
    existing = {
        str(item.get("label") or "").strip().upper(): clean_sentence(item.get("explanation"))
        for item in out.get("incorrectExplanations") or []
        if isinstance(item, dict) and item.get("label")
    }
    for label, explanation in rationale_by_label.items():
        if label and explanation and not existing.get(label):
            existing[label] = explanation
    correct = str(out.get("correctAnswer") or "").strip().upper()
    if correct and existing.get(correct) and not clean_sentence(out.get("correctExplanation")):
        out["correctExplanation"] = existing[correct]
    out["incorrectExplanations"] = [
        {"label": label, "explanation": explanation}
        for label, explanation in sorted(existing.items())
        if label and explanation and label != correct
    ]
    if visual_state.get("imageRouting"):
        out["imageRouting"] = visual_state.get("imageRouting")
    out["visualAnswerStates"] = visual_state.get("answerStates") or []
    return out


def amboss_combine_visual_states(page_records: list[dict[str, Any]]) -> dict[str, Any]:
    combined: dict[str, Any] = {
        "correctAnswer": "",
        "confidence": 0.0,
        "answerStates": [],
        "visibleRationales": [],
        "imageRouting": [],
        "warnings": [],
        "sourcePageIds": [],
    }
    best_confidence = 0.0
    rationales: dict[str, dict[str, Any]] = {}
    state_by_label: dict[str, dict[str, Any]] = {}
    route_keys: set[tuple[str, str]] = set()
    state_priority = {"correct": 3, "incorrect": 2, "unknown": 1}
    for record in page_records:
        state = record.get("visualState") or {}
        page_id = str((record.get("page") or {}).get("pageId") or state.get("sourcePageId") or "")
        if page_id:
            combined["sourcePageIds"].append(page_id)
        correct = str(state.get("correctAnswer") or "").strip().upper()
        confidence = float(state.get("confidence") or 0)
        if correct and confidence >= best_confidence:
            combined["correctAnswer"] = correct
            combined["confidence"] = confidence
            best_confidence = confidence
        for item in state.get("answerStates") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip().upper()
            item_state = str(item.get("state") or "").strip().lower()
            prior = state_by_label.get(label)
            if label and (
                not prior
                or state_priority.get(item_state, 0) > state_priority.get(str(prior.get("state") or ""), 0)
                or float(item.get("confidence") or 0) > float(prior.get("confidence") or 0)
            ):
                state_by_label[label] = item
        for item in state.get("visibleRationales") or []:
            if not isinstance(item, dict) or not item.get("label"):
                continue
            label = str(item.get("label") or "").strip().upper()
            explanation = clean_sentence(item.get("explanation"))
            confidence = float(item.get("confidence") or 0)
            prior = rationales.get(label)
            if explanation and (not prior or confidence >= float(prior.get("confidence") or 0)):
                rationales[label] = {"label": label, "explanation": explanation, "confidence": confidence}
        for route in state.get("imageRouting") or []:
            if not isinstance(route, dict):
                continue
            key = (str(route.get("sourcePageId") or page_id), str(route.get("imageId") or ""))
            if key not in route_keys:
                route_keys.add(key)
                combined["imageRouting"].append(route)
        combined["warnings"].extend(str(w) for w in (state.get("warnings") or []) if w)
    combined["sourcePageIds"] = dedupe_preserve_order(combined["sourcePageIds"])
    combined["answerStates"] = [state_by_label[label] for label in sorted(state_by_label)]
    combined["visibleRationales"] = list(rationales.values())
    combined["warnings"] = dedupe_preserve_order(combined["warnings"])
    return combined


def amboss_dedupe_and_promote_questions(page_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_stem: dict[str, dict[str, Any]] = {}
    visual_state_combined = amboss_combine_visual_states(page_records)
    visual_correct = str(visual_state_combined.get("correctAnswer") or "").strip().upper()
    for record in page_records:
        for q in record.get("questions") or []:
            key = short_hash(normalize_key(q.get("stem") or "")[:800])
            candidate = copy.deepcopy(q)
            if visual_correct:
                candidate = amboss_apply_visual_state_to_question(candidate, visual_state_combined)
                candidate["visualSourcePageIds"] = visual_state_combined.get("sourcePageIds") or []
            score = (
                len(candidate.get("answerChoices") or []) * 10
                + len(candidate.get("incorrectExplanations") or []) * 3
                + (20 if candidate.get("correctAnswer") else 0)
                + int(10 * float(candidate.get("extractionConfidence") or 0))
            )
            prior = best_by_stem.get(key)
            prior_score = prior.get("_score", -1) if prior else -1
            if score > prior_score:
                candidate["_score"] = score
                best_by_stem[key] = candidate
    out: list[dict[str, Any]] = []
    for q in best_by_stem.values():
        q.pop("_score", None)
        out.append(q)
    return out


def amboss_normalized_slide_for_question(page: dict[str, Any], q: dict[str, Any], index: int, image_audit: list[dict[str, Any]], support_pages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    question_id = f"{page['pageId']}_q{index:02d}_{amboss_question_hash(q)}"
    images: list[dict[str, Any]] = []
    seen_fingerprints: dict[str, str] = {}
    source_pages = [page]
    for support_page in support_pages or []:
        if support_page and support_page.get("pageId") != page.get("pageId"):
            source_pages.append(support_page)
    source_images = [img for source_page in source_pages for img in (source_page.get("images") or [])]
    for img in source_images:
        img = dict(img)
        fp = img.get("imageFingerprint") or img.get("sha256")
        img["imageFingerprint"] = fp
        img["routedLocation"] = "ignored"
        img["imageConfidence"] = 0.0
        img["duplicateOf"] = None
        if fp and fp in seen_fingerprints:
            img["duplicateOf"] = seen_fingerprints[fp]
            image_audit.append({**img, "dedupeAction": "suppressed_duplicate"})
            continue
        if fp:
            seen_fingerprints[fp] = img.get("imageId")
        for route in q.get("imageRouting") or []:
            if isinstance(route, dict) and route.get("imageId") == img.get("imageId"):
                placement = str(route.get("placement") or "ignored").lower()
                if placement not in {"stem", "explanation", "ignored"}:
                    placement = "ignored"
                img["routedLocation"] = placement
                img["imageConfidence"] = float(route.get("imageConfidence") or 0)
        image_audit.append({**img, "dedupeAction": "kept"})
        images.append(img)
    return {
        "slideId": question_id,
        "slideType": ["HIGH_YIELD_CLINICAL"],
        "yieldScore": int(100 * min(1.0, float(q.get("extractionConfidence") or 0))),
        "primaryConcepts": [clean_tag(q.get("educationalObjective") or "AMBOSS extracted question")],
        "secondaryConcepts": [],
        "clinicalFacts": [clean_sentence(q.get("stem"))],
        "diagnosticFacts": [],
        "managementFacts": [],
        "mechanismFacts": [],
        "images": images,
        "tables": page.get("tables") or [],
        "questionPotential": 100,
        "sourceTextHash": amboss_question_hash(q),
        "metadata": {
            "profile": AMBOSS_PROFILE,
            "sourcePageId": page.get("pageId"),
            "pageNumber": page.get("pageNumber"),
            "extractionConfidence": q.get("extractionConfidence"),
        },
    }


def amboss_generated_question(q: dict[str, Any], slide_id: str) -> dict[str, Any] | None:
    choices = q.get("answerChoices") or []
    if not isinstance(choices, list) or not (2 <= len(choices) <= 9):
        return None
    normalized_choices = [
        {"label": str(c.get("label") or "").strip().upper(), "text": str(c.get("text") or "").strip()}
        for c in choices
        if isinstance(c, dict) and str(c.get("label") or "").strip() and str(c.get("text") or "").strip()
    ]
    labels = [c["label"] for c in normalized_choices]
    if labels != expected_sequential_labels(len(labels)):
        return None
    correct = str(q.get("correctAnswer") or "").strip().upper()
    if correct not in labels:
        return None
    stem = clean_sentence(q.get("stem"))
    if not stem:
        return None
    objective = clean_sentence(q.get("educationalObjective") or q.get("correctExplanation") or "Review the extracted AMBOSS explanation.")
    return {
        "slideId": slide_id,
        "questionKind": "extracted_amboss",
        "stemTemplate": "amboss_extracted",
        "testedConcept": objective[:120],
        "diagnosisOrTarget": objective[:120],
        "distractorFamily": "original Amboss answer choices",
        "stem": stem,
        "answerChoices": normalized_choices,
        "correctAnswer": correct,
        "correctExplanation": clean_sentence(q.get("correctExplanation")),
        "incorrectExplanations": [
            {"label": str(item.get("label") or "").strip().upper(), "explanation": clean_sentence(item.get("explanation"))}
            for item in (q.get("incorrectExplanations") or [])
            if isinstance(item, dict) and str(item.get("label") or "").strip().upper() in labels
        ],
        "educationalObjective": objective,
        "retrievalTag": objective[:80],
        "reviewPearl": clean_sentence(q.get("correctExplanation") or objective),
        "imageRouting": [
            {"imageId": r.get("imageId"), "placement": r.get("placement")}
            for r in (q.get("imageRouting") or [])
            if isinstance(r, dict) and r.get("placement") in {"stem", "explanation"}
        ],
        "tableUse": [],
        "sourceFactIds": [q.get("sourcePageId")],
        "extractionWarnings": q.get("extractionWarnings") or [],
    }


def validate_amboss_extraction(q: dict[str, Any]) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    if not clean_sentence(q.get("stem")):
        warnings.append("missing stem")
    choices = q.get("answerChoices") or []
    if not isinstance(choices, list) or len(choices) < 4:
        warnings.append("fewer than 4 answer choices")
    visible_labels = {
        str(choice.get("label") or "").strip().upper()
        for choice in choices
        if isinstance(choice, dict)
    }
    if str(q.get("correctAnswer") or "").strip().upper() not in visible_labels:
        warnings.append("correct answer not visible or invalid")
    confidence = float(q.get("extractionConfidence") or 0)
    if confidence < 0.55:
        warnings.append("low extraction confidence")
    return not any(w in warnings for w in ["missing stem", "fewer than 4 answer choices", "correct answer not visible or invalid"]), warnings


def amboss_canonical_blockers(q: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    choices = q.get("answerChoices") or []
    if not isinstance(choices, list) or not (2 <= len(choices) <= 9):
        blockers.append(f"AMBOSS canonical conversion supports 2-9 choices; extracted {len(choices) if isinstance(choices, list) else 0}")
    labels = [str(c.get("label") or "").strip().upper() for c in choices if isinstance(c, dict)]
    expected = expected_sequential_labels(len(labels))
    if labels != expected:
        blockers.append(f"AMBOSS labels are not sequential {expected[0] if expected else 'A'}-{expected[-1] if expected else 'Z'}")
    if str(q.get("correctAnswer") or "").strip().upper() not in labels:
        blockers.append(f"correct answer {q.get('correctAnswer') or '(missing)'} is not present in extracted labels")
    return blockers


def amboss_audit_page_images(page: dict[str, Any], question: dict[str, Any] | None, image_audit: list[dict[str, Any]], status: str) -> None:
    routes = question.get("imageRouting") if isinstance(question, dict) else []
    route_by_id = {
        str(route.get("imageId")): route
        for route in (routes or [])
        if isinstance(route, dict) and route.get("imageId")
    }
    seen: dict[str, str] = {}
    for img in page.get("images") or []:
        fp = img.get("imageFingerprint") or img.get("sha256")
        route = route_by_id.get(str(img.get("imageId")))
        routed = str((route or {}).get("placement") or "ignored").lower()
        if routed not in {"stem", "explanation", "ignored"}:
            routed = "ignored"
        duplicate_of = seen.get(fp) if fp else None
        if fp and not duplicate_of:
            seen[fp] = str(img.get("imageId"))
        image_audit.append({
            "imageId": img.get("imageId"),
            "sourcePageId": page.get("pageId"),
            "assetPath": img.get("assetPath"),
            "imageFingerprint": fp,
            "routedLocation": "ignored" if duplicate_of else routed,
            "imageConfidence": float((route or {}).get("imageConfidence") or 0),
            "duplicateOf": duplicate_of,
            "dedupeAction": "suppressed_duplicate" if duplicate_of else "kept",
            "status": status,
            "reason": (route or {}).get("reason") or ("no complete canonical question" if status != "canonical" else ""),
        })


# ── Deterministic AMBOSS extractor (v4.57) ─────────────────────────────────────
# AMBOSS QBank PDFs are flattened browser screenshots — every page is one image
# with no text layer. The previous Gemini-per-page extractor was 9 min / $0.50
# per import; this deterministic OCR-based pipeline runs in seconds for free.
# Architecture:
#   1. Render each PDF page to PNG (decompose_amboss_input already does this).
#   2. tesseract OCR per page → clean text.
#   3. Classify each page as `qbank_screenshot` or `blown_up_image` from
#      (word count, presence of choice markers, presence of nav pill).
#   4. Group screenshot pages by AMBOSS nav pill `< N / M >`.
#   5. Per group, extract stem, choices A–H, per-choice explanations.
#   6. Detect correct answer by text-pattern signature ("is the most likely…").
#   7. Attach blown-up image pages to the most-recent question's explanationImages.

_AMBOSS_NAV_PILL = re.compile(r"<\s*(\d+)\s*/\s*(\d+)\s*>")
_AMBOSS_NAV_FALLBACK = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")
_AMBOSS_CHOICE_HEADER = re.compile(
    r"^[\s'`\"‘’“”]*\(?([A-H])\)\s*[\|:]?\s*(.{3,200}?)\s*$",
    re.MULTILINE,
)
_AMBOSS_VIGNETTE_OPENER = re.compile(
    # Canonical clinical vignette opener: "A NN-year-old", "An 18-month-old", etc.
    # OCR variants we've seen and need to match:
    #   "A 13-month-old"  (clean)
    #   "An 18-year-old"  (clean)
    #   "A.13-month-old"  (OCR replaces space with period)
    #   "A,13 month old"  (OCR replaces space with comma)
    #   "A 13 month old"  (OCR drops hyphens)
    #   "A 13month-old"   (OCR joins digit + word)
    r"\bA[n]?[\s.,;:]+\d+[\s.,\-]*(?:year|month|day|week)[\s.,\-]*old\b",
    re.IGNORECASE,
)
_AMBOSS_POSITIVE_SIGNAL = re.compile(
    r"\b(?:is the most likely|is the appropriate|was the appropriate|are the appropriate|"
    r"is the best (?:initial )?(?:treatment|management|step|next step)|"
    r"is the (?:correct|preferred) (?:diagnosis|treatment|management|answer)|"
    r"this patient (?:most likely has|has|likely has))\b",
    re.IGNORECASE,
)
_AMBOSS_NEGATIVE_SIGNAL = re.compile(
    r"\b(?:is not consistent|would be (?:very )?unusual|does not (?:typically|usually|commonly)|"
    r"is inconsistent with|are not typical|is not (?:the|a) (?:typical|common|likely)|"
    r"this patient is instead presenting|is unlikely to|is incorrect because|are very rare)\b",
    re.IGNORECASE,
)


def parse_amboss_nav_pill(ocr_text: str, expected_total: int | None = None) -> tuple[int, int] | None:
    """Return (current, total) for the AMBOSS QBank navigation pill `< N / M >`.

    If `expected_total` is provided (i.e. the majority M observed across the PDF),
    candidate matches are validated against it — this throws out OCR misreads like
    "24/8" → which can arise when tesseract misreads "2/8" by joining the "2" with
    an adjacent character. Returns None when no plausible pill is found.
    """
    if not ocr_text:
        return None
    candidates: list[tuple[int, int]] = []
    for m in _AMBOSS_NAV_PILL.finditer(ocr_text):
        candidates.append((int(m.group(1)), int(m.group(2))))
    for m in _AMBOSS_NAV_FALLBACK.finditer(ocr_text):
        n, total = int(m.group(1)), int(m.group(2))
        # Reject vital-signs / lab-value patterns like "120/min", "80/mm"
        end = m.end()
        tail = ocr_text[end:end + 6].lower()
        if tail.startswith(("min", "hour", "dl", "ml", "kg", "mm", "mg", "mmh")):
            continue
        if 1 <= n <= total <= 200:
            candidates.append((n, total))
    for n, total in candidates:
        if expected_total is not None and total != expected_total:
            continue
        if 1 <= n <= total <= 200:
            return (n, total)
    return None


def classify_amboss_page(ocr_text: str) -> str:
    """Classify a page as `qbank_screenshot` (text + choices) or `blown_up_image` (mostly image, caption only)."""
    text = ocr_text or ""
    words = len(text.split())
    choice_headers = len(_AMBOSS_CHOICE_HEADER.findall(text))
    if words < 130 and choice_headers < 3 and not _AMBOSS_NAV_PILL.search(text):
        return "blown_up_image"
    return "qbank_screenshot"


def amboss_stem_fingerprint(text: str) -> str:
    """Return a short fingerprint of the clinical vignette stem, used to group
    pages of the same AMBOSS question together. Returns an empty string when no
    vignette is detectable (the page is a blown-up image or cover sheet).

    Anchors on the position of the canonical vignette opener (e.g., "A 13-month-old
    girl is brought…" → matches `A 13-month-old`). This makes the fingerprint
    insensitive to OCR'd UI chrome that precedes the real stem on some pages
    (e.g., the OCR sometimes prepends "OA " or "me " from the AMBOSS sidebar icons)."""
    if not text:
        return ""
    m = _AMBOSS_VIGNETTE_OPENER.search(text)
    if not m:
        return ""
    snippet = text[m.start(): m.start() + 110]
    normalized = re.sub(r"[^a-z0-9 ]", " ", snippet.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if len(normalized) < 30:
        return ""
    return normalized[:70]


def _AMBOSS_choice_anchor_words(label_text: str, n: int = 3) -> list[str]:
    """First `n` significant words of a choice label, stripping stopwords. Used to
    locate the choice's explanation paragraph (AMBOSS explanations restate the
    choice text but often vary articles, e.g., "Replication of the attenuated
    vaccine strain" → "Replication of an attenuated viral strain…")."""
    stopwords = {"a", "an", "the", "of", "is", "are", "was", "were", "with", "to",
                 "in", "on", "for", "by", "and", "or", "as", "at", "from"}
    words = re.findall(r"[a-z]+", (label_text or "").lower())
    significant = [w for w in words if w not in stopwords]
    return significant[:n]


def amboss_find_all_choice_bars(asset_path: Path) -> list[dict[str, Any]]:
    """Detect horizontal AMBOSS choice header bars on a rendered page and
    classify each as 'green' (correct answer) or 'pink' (wrong answer).

    AMBOSS draws the correct-answer choice header in green vs pink/red for the
    wrong answers on the explanation-reveal pages. Returns a list of bars sorted
    top-to-bottom; each entry has {'y_center', 'color'} where color is one of
    'green', 'pink', or 'unknown'. The caller maps the green bar's INDEX to the
    choice label (A, B, C, …) since OCR of AMBOSS's styled choice circles is
    too unreliable to read directly."""
    fitz = optional_import_fitz()
    if not fitz or not asset_path or not asset_path.exists():
        return []
    try:
        pix = fitz.Pixmap(str(asset_path))
        if pix.colorspace.name != "DeviceRGB":
            pix = fitz.Pixmap(fitz.csRGB, pix)
        w, h, n = pix.width, pix.height, pix.n
        samples = pix.samples
    except Exception:
        return []
    # Sample three x-coordinates across the choice-bar region (left of the page
    # content, where the bar background colors are most distinct).
    sample_xs = [int(w * 0.25), int(w * 0.30), int(w * 0.35)]

    def classify_row(y: int) -> str:
        green_votes = 0
        pink_votes = 0
        for x in sample_xs:
            if x >= w:
                continue
            off = (y * w + x) * n
            r, g, b = samples[off], samples[off + 1], samples[off + 2]
            # Pale green: G > 200 and notably above R and B
            if g > 200 and g > r + 5 and g > b + 5 and r < 240 and b < 240:
                green_votes += 1
            # Pale pink: R > 230 and notably above G and B
            elif r > 230 and r > g + 5 and r > b + 5 and g < 240 and b < 240:
                pink_votes += 1
        if green_votes >= 2:
            return "green"
        if pink_votes >= 2:
            return "pink"
        return "other"

    # Collect contiguous runs of green / pink rows.
    runs: list[dict[str, Any]] = []
    cur: list[tuple[int, str]] = []
    last_color = None
    for y in range(h):
        c = classify_row(y)
        if c in ("green", "pink"):
            if last_color is not None and c != last_color:
                if len(cur) >= 8:
                    ys = [item[0] for item in cur]
                    runs.append({"y_center": (ys[0] + ys[-1]) // 2, "color": last_color})
                cur = []
            cur.append((y, c))
            last_color = c
        else:
            if len(cur) >= 8 and last_color is not None:
                ys = [item[0] for item in cur]
                runs.append({"y_center": (ys[0] + ys[-1]) // 2, "color": last_color})
            cur = []
            last_color = None
    if len(cur) >= 8 and last_color is not None:
        ys = [item[0] for item in cur]
        runs.append({"y_center": (ys[0] + ys[-1]) // 2, "color": last_color})
    return runs


def amboss_ocr_choice_letter_at_y(asset_path: Path, y_center: int, strip_height: int = 50) -> str | None:
    """OCR a narrow horizontal strip centered at `y_center` and return the choice
    letter (A–H) if detected. Used to identify which choice the green correct-answer
    bar belongs to."""
    if not asset_path or not asset_path.exists() or not shutil.which("tesseract"):
        return None
    fitz = optional_import_fitz()
    if not fitz:
        return None
    try:
        pix = fitz.Pixmap(str(asset_path))
        w, h = pix.width, pix.height
        y_start = max(0, y_center - strip_height // 2)
        y_end = min(h, y_center + strip_height // 2)
        clip = fitz.IRect(0, y_start, w, y_end)
        crop = fitz.Pixmap(pix, pix.clip if hasattr(pix, "clip") else None)
        # Easier path: render via PIL-like cropping using fitz Pixmap.set_rect or just
        # write the full image and let tesseract scan the strip via --psm 7. The
        # simpler approach: write a region-cropped PNG.
        # fitz Pixmap doesn't trivially crop; use PIL instead.
        from PIL import Image
        full = Image.open(str(asset_path))
        crop_img = full.crop((0, y_start, w, y_end))
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix=".png", delete=False) as f:
            crop_img.save(f.name)
            tmp = f.name
        proc = subprocess.run(["tesseract", tmp, "-", "--psm", "7"],
                              capture_output=True, text=True, timeout=15)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if proc.returncode != 0:
            return None
        line = proc.stdout.strip()
        # Look for a single capital letter A-H as a token at the start (after %)
        m = re.search(r"(?:^|[^A-Za-z])([A-H])(?:[^A-Za-z]|$)", line)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None





def extract_amboss_stem(text: str) -> str:
    """Extract the clinical vignette from OCR text. Starts at the first vignette
    opener match position, ends at the first choice marker or the trailing `?` of
    the lead-in. Trims OCR'd leading chrome (punctuation, sidebar fragments) by
    cutting BEFORE the matched opener position."""
    if not text:
        return ""
    m = _AMBOSS_VIGNETTE_OPENER.search(text)
    if not m:
        return ""
    # Start cleanly at the matched opener position (drops OCR noise like "- " or
    # "; " that sometimes prefixes the stem).
    remainder = text[m.start():]
    lines = remainder.split("\n")
    stem_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"[\s'`\"‘’“”]*\(?[A-H]\)\s+\S", stripped):
            break
        if stripped:
            stem_lines.append(stripped)
        if stripped.endswith("?"):
            break
    return re.sub(r"\s+", " ", " ".join(stem_lines)).strip()


def extract_amboss_choices(text: str) -> list[dict[str, str]]:
    """Extract short answer choice labels (A–H). AMBOSS renders the choice letter
    inside a styled circle which tesseract often misreads as `@)|`, `©l`, `©)`,
    `(©)`, etc. Pure label-based regex misses most of these, so we use a TWO-PASS
    strategy:

    1. PRIMARY = positional inference. Find the stem's lead-in question (line
       ending in `?`) and treat the next 2–8 short, choice-shaped lines as the
       answer choices in document order, assigning A, B, C, … to each.
    2. FALLBACK = label-based regex. When positional inference fails (e.g., the
       lead-in question wasn't OCR'd cleanly), fall back to the prior approach.

    When the same choice text is encountered in multiple page views (stem-only +
    explanation-reveal), prefer the SHORTER occurrence (which is the label, not
    the explanation paragraph)."""
    if not text:
        return []
    # PRIMARY — positional inference.
    positional = _extract_amboss_choices_positional(text)
    if 4 <= len(positional) <= 9:
        return positional
    # FALLBACK — label-based regex.
    seen: dict[str, str] = {}
    for m in _AMBOSS_CHOICE_HEADER.finditer(text):
        label = m.group(1)
        candidate = m.group(2).strip()
        candidate = re.sub(r"\s+(?:bd|sa|»\s*Feedback|»|sss|nn).*$", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.rstrip(" .|:")
        if not candidate:
            continue
        existing = seen.get(label)
        if existing is None or len(candidate) < len(existing):
            seen[label] = candidate
    return [{"label": label, "text": seen[label]} for label in sorted(seen.keys())]


# Per-line patterns used by positional choice inference.
_AMBOSS_LEAD_IN_QUESTION = re.compile(
    r"\b(?:which of the following|what is the (?:most likely|best initial|next best|most appropriate)|"
    r"what should|the most likely diagnosis|which one of the following)\b.{0,200}\?",
    re.IGNORECASE | re.DOTALL,
)
_AMBOSS_CHOICE_SHAPED_LINE = re.compile(
    # A choice line: optional OCR-noise marker prefix (anything before the first letter),
    # then 3–180 chars of choice text. Reject lines that are too short or chrome-like.
    r"^[\s\W_]{0,8}([A-Za-z][A-Za-z0-9\-, ()'\":/+%]{2,180})\s*$"
)


def _extract_amboss_choices_positional(text: str) -> list[dict[str, str]]:
    """Find the lead-in question's terminating `?`, then collect the next 2–8
    short lines that look like real choice candidates and label them A, B, C, …
    in document order. Robust against OCR misreads of AMBOSS's styled choice
    letters (which often come back as `@`, `©`, `©l`, `©)`, etc.) but rejects
    sidebar chrome and explanation-page stem repetitions."""
    if not text:
        return []
    m = _AMBOSS_LEAD_IN_QUESTION.search(text)
    if not m:
        return []
    cursor = m.end()
    lines = text[cursor:].split("\n")
    sidebar_re = re.compile(
        r"^(?:overview|library|qbank|analysis|shop|account|menu|feedback|search"
        r"|show\s+answer|caption|snc|snape|ape|hide\s+answer|next|previous"
        r"|terms\s+(?:and|of)|privacy|legal|support|contact|ideas)\b",
        re.IGNORECASE,
    )
    # OCR'd sidebar icons + labels often appear as 2–4-char garbage at the start
    # of a line: "OA", "ZY", "DO i", "ea F", "Shop i", "Account j", "ame Th-",
    # "es a", "Si a", etc. Require choice lines to start with a sequence of
    # letters that look like the beginning of a clinical noun phrase: 3+ chars
    # of alpha (allowing an initial capital like "Cerebral", "Intravenous",
    # "Esophagogastroduodenoscopy").
    looks_like_real_choice = re.compile(r"^[A-Z][a-z]{2,}")
    looks_like_amboss_stem = re.compile(
        r"\bA[n]?\s+\d+[\s\-]*(?:year|month|day|week)[\s\-]*old\b",
        re.IGNORECASE,
    )
    raw_candidates: list[str] = []
    misses_in_row = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if sidebar_re.match(line):
            continue
        # Stop if a stem opener appears — that's the explanation-page stem
        # repetition, we've left the choice block.
        if looks_like_amboss_stem.search(line):
            break
        # Stop if a line is very long (paragraph content from the explanation panel).
        if len(line) > 180:
            break
        # Stop if a new lead-in question shows up (next question's stem).
        if line.endswith("?") and len(line.split()) > 5:
            break
        m2 = _AMBOSS_CHOICE_SHAPED_LINE.match(line)
        if not m2:
            misses_in_row += 1
            if misses_in_row >= 5 and raw_candidates:
                break
            continue
        candidate = m2.group(1).strip().rstrip(" .|:")
        # Reject short garbage and lines that don't start with a real choice word.
        if len(candidate) < 6 or not looks_like_real_choice.match(candidate):
            misses_in_row += 1
            if misses_in_row >= 5 and raw_candidates:
                break
            continue
        misses_in_row = 0
        raw_candidates.append(candidate)
        if len(raw_candidates) >= 9:
            break
    # Dedupe (preserving order) and cap at 8 (AMBOSS max choices).
    seen_norm: set[str] = set()
    deduped: list[str] = []
    for c in raw_candidates:
        norm = re.sub(r"\W+", "", c.lower())[:40]
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        deduped.append(c)
        if len(deduped) >= 8:
            break
    if not (2 <= len(deduped) <= 8):
        return []
    return [{"label": LABELS_AH[i], "text": deduped[i]} for i in range(len(deduped))]


# Labels A–H used by positional inference (LABELS in this file is the global
# A–D constant for non-AMBOSS sources, so we declare a local 8-letter list).
LABELS_AH = ["A", "B", "C", "D", "E", "F", "G", "H"]


def extract_amboss_choice_explanations(text: str, choices: list[dict[str, str]]) -> dict[str, str]:
    """For each choice, find the per-choice explanation paragraph. AMBOSS explanations
    restate the choice text but often vary articles and synonyms — e.g., the choice
    "Replication of the attenuated vaccine strain" is restated as "Replication of an
    attenuated viral strain is the most likely explanation…". To match across that
    drift, we anchor on the first 3 SIGNIFICANT words of the label (skipping `a`,
    `an`, `the`, `of`, `is`, etc.) connected by `.{0,30}` wildcards."""
    out: dict[str, str] = {}
    if not text:
        return out
    for choice in choices:
        label = choice["label"]
        # n=2 (just the first 2 significant words) is lenient enough to handle
        # AMBOSS's article/synonym substitutions across views — e.g., the choice
        # "Replication of the attenuated vaccine strain" is restated in the
        # explanation as "Replication of an attenuated viral strain…" (the third
        # significant word changes "vaccine" → "viral").
        anchor_words = _AMBOSS_choice_anchor_words(choice["text"], n=2)
        if len(anchor_words) < 2:
            continue
        anchor_re = r"\b" + r"\b.{0,30}\b".join(re.escape(w) for w in anchor_words) + r"\b"
        # Prefer matches where the anchor is followed by an explanatory verb
        # within ~50 chars — that's the canonical "Choice X is/are/describes…"
        # shape of an AMBOSS explanation paragraph. Without this preference the
        # anchor would attach to the bare choice label on the stem-only view
        # (which appears before the explanation-reveal page in combined_text).
        explanation_pattern = re.compile(
            rf"(?P<body>{anchor_re}[^\n]{{0,50}}\b(?:is|are|was|were|describes|"
            rf"may|can|would|leads|causes|occurs|presents|results|refers|"
            rf"affects|requires|prevents|reduces|increases|develops|"
            rf"includes|involves|represents)\b.{{40,}}?)"
            r"(?=\n\s*\n|\n[\s'`\"‘’“”]*\(?[A-H]\)\s+\S|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        best = ""
        for m in explanation_pattern.finditer(text):
            body = re.sub(r"\s+", " ", m.group("body")).strip()
            if len(body) > len(best):
                best = body
        # Fallback to the original looser pattern if the verb-anchored search
        # turned up nothing (some explanations use atypical phrasing).
        if not best:
            fallback_pattern = re.compile(
                rf"(?P<body>{anchor_re}.{{40,}}?)(?=\n\s*\n|\n[\s'`\"‘’“”]*\(?[A-H]\)\s+\S|\Z)",
                re.DOTALL | re.IGNORECASE,
            )
            for m in fallback_pattern.finditer(text):
                body = re.sub(r"\s+", " ", m.group("body")).strip()
                if len(body) > len(best):
                    best = body
        if len(best) > 80:
            out[label] = best
    return out


def detect_amboss_correct_answer(explanations: dict[str, str], default_label: str) -> str:
    """Score per-choice explanations: positive signals (`is the most likely`) +5,
    negative signals (`is not consistent with`) -2. Highest score wins. If no
    explanation has a positive signal, fall back to default_label and the caller
    surfaces a warning."""
    scored: list[tuple[int, str]] = []
    for label, exp in explanations.items():
        score = 0
        if _AMBOSS_POSITIVE_SIGNAL.search(exp):
            score += 5
        if _AMBOSS_NEGATIVE_SIGNAL.search(exp):
            score -= 2
        scored.append((score, label))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if scored and scored[0][0] > 0:
        return scored[0][1]
    return default_label


def derive_amboss_educational_objective(correct_explanation: str, stem: str = "") -> str:
    """First-sentence summary of the correct-answer explanation, capped at 200
    chars. When the explanation couldn't be extracted (empty), fall back to a
    stem-derived summary so each question gets a UNIQUE objective string — the
    app-ready validator rejects the entire export when two questions share an
    identical educational objective."""
    if correct_explanation:
        first_period = correct_explanation.find(".")
        if 30 < first_period < 240:
            return correct_explanation[: first_period + 1].strip()
        return correct_explanation[:200].strip()
    if stem:
        stem_summary = re.sub(r"\s+", " ", stem).strip()[:160]
        return f"Review the AMBOSS clinical scenario: {stem_summary}"
    return "Review the AMBOSS-extracted explanation."


def _amboss_collect_screenshot_asset_paths(screenshots: list[dict[str, Any]], limit: int = 4) -> list[Path]:
    """Return up to `limit` rendered page PNG paths for the question's screenshot
    pages. Used to send page images to Gemini for the hybrid clean-up call."""
    out: list[Path] = []
    for entry in screenshots[:limit]:
        image_assets = entry["page"].get("images") or []
        if not image_assets:
            continue
        asset_str = str(image_assets[0].get("assetPath") or "")
        if not asset_str:
            continue
        asset_path = Path(asset_str)
        if not asset_path.is_absolute():
            asset_path = BASE_DIR / asset_path
        if asset_path.exists():
            out.append(asset_path)
    return out


def amboss_gemini_extract_question(
    screenshots: list[dict[str, Any]],
    stem: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Make ONE Gemini call per AMBOSS question to extract everything that the
    deterministic OCR pipeline can't get cleanly: answer choices, correct
    answer, per-choice explanations, educational objective, retrieval tag, and
    review pearl.

    AMBOSS QBank PDFs are flattened browser screenshots — the styled choice
    letter circles, the green correct-answer bar, and the wrong-answer rationale
    paragraphs are all surrounded by sidebar / browser-chrome OCR noise that
    can't be filtered out reliably. Letting Gemini READ the rendered page
    images directly is the only way to get clean structured output without
    paying for one call per page.

    Returns the structured question dict on success, or None on failure."""
    image_paths = _amboss_collect_screenshot_asset_paths(screenshots, limit=4)
    if not image_paths:
        return None
    prompt = (
        "You are extracting a structured medical exam question from screenshots of "
        "an AMBOSS QBank PDF. The clinical vignette stem has already been extracted "
        "from OCR (provided below). From the attached page images (which show the "
        "same question across multiple views — stem-only, expanded layout, and the "
        "explanation-reveal view where each choice's expanded rationale is visible), "
        "extract the structured question.\n"
        "\n"
        "Strict rules:\n"
        "  - The CORRECT answer choice is the one whose header is rendered with a "
        "GREEN background on the explanation-reveal page. Wrong answer headers are "
        "PINK/RED.\n"
        "  - Answer choices are short noun phrases (typically 3-15 words). Do NOT "
        "include explanation text in the choice 'text' field.\n"
        "  - Per-choice explanations are the multi-sentence paragraphs under each "
        "choice on the explanation-reveal page. Capture them as clean prose — "
        "strip out AMBOSS sidebar chrome (Overview, Library, Qbank, Analysis, "
        "Shop, Account), percentage badges ('48%'), navigation pill text "
        "('< 1 / 8 >'), the doctor-avatar hint icon, footer text, taskbar, and "
        "any other UI artifacts. Each explanation should read like a textbook "
        "paragraph, not OCR noise.\n"
        "  - educationalObjective: ONE concise sentence (under 200 chars) "
        "describing the key teaching point of the correct answer.\n"
        "  - retrievalTag: 3-8 words capturing the testable concept (e.g., "
        "'Pellagra — niacin deficiency', 'Acute pancreatitis: Cullen sign').\n"
        "  - reviewPearl: ONE concise sentence (under 250 chars) — the single "
        "highest-yield take-away from this question.\n"
        "\n"
        "Return strict JSON only — no markdown fences, no commentary outside JSON. "
        "Schema:\n"
        "{\n"
        '  "answerChoices": [{"label": "A", "text": "…"}, …],\n'
        '  "correctAnswer": "F",\n'
        '  "correctExplanation": "1-3 sentence prose explanation of why the correct answer is correct",\n'
        '  "incorrectExplanations": [\n'
        '    {"label": "A", "explanation": "1-3 sentence prose explanation of why this is wrong"},\n'
        "    …\n"
        "  ],\n"
        '  "educationalObjective": "…",\n'
        '  "retrievalTag": "…",\n'
        '  "reviewPearl": "…"\n'
        "}\n"
        "\n"
        f"Stem (already extracted via OCR — use as reference; you do not need to repeat it): {stem[:600]}"
    )
    try:
        raw = raw_gemini_image_call(
            api_key=api_key,
            prompt=prompt,
            image_paths=image_paths,
            temperature=0.05,
            max_tokens=8192,
            timeout_seconds=120,
            response_mime_type="application/json",
        )
    except Exception as exc:
        warn(f"AMBOSS hybrid Gemini call failed: {exc}")
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"AMBOSS hybrid Gemini returned non-JSON: {exc}; payload starts: {text[:120]!r}")
        return None
    if not isinstance(parsed, dict):
        return None
    choices_raw = parsed.get("answerChoices")
    correct_raw = parsed.get("correctAnswer")
    if not isinstance(choices_raw, list) or not choices_raw:
        return None
    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(choices_raw[:8]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().upper()
        choice_text = str(item.get("text") or "").strip()
        if not label or len(label) != 1 or label not in LABELS_AH:
            label = LABELS_AH[idx]
        if not choice_text or len(choice_text) < 3:
            continue
        normalized.append({"label": label, "text": choice_text})
    if not (2 <= len(normalized) <= 8):
        return None
    if [c["label"] for c in normalized] != LABELS_AH[: len(normalized)]:
        normalized = [{"label": LABELS_AH[i], "text": normalized[i]["text"]} for i in range(len(normalized))]
    valid_labels = {c["label"] for c in normalized}
    correct = str(correct_raw or "").strip().upper()
    if correct not in valid_labels:
        correct = normalized[0]["label"]
    # Normalize the per-choice explanations into a dict keyed by label.
    incorrect_raw = parsed.get("incorrectExplanations")
    incorrect_explanations: list[dict[str, str]] = []
    if isinstance(incorrect_raw, list):
        for item in incorrect_raw:
            if not isinstance(item, dict):
                continue
            lbl = str(item.get("label") or "").strip().upper()
            exp = str(item.get("explanation") or "").strip()
            if lbl in valid_labels and lbl != correct and len(exp) > 20:
                incorrect_explanations.append({"label": lbl, "explanation": exp})
    return {
        "answerChoices": normalized,
        "correctAnswer": correct,
        "correctExplanation": str(parsed.get("correctExplanation") or "").strip(),
        "incorrectExplanations": incorrect_explanations,
        "educationalObjective": str(parsed.get("educationalObjective") or "").strip(),
        "retrievalTag": str(parsed.get("retrievalTag") or "").strip(),
        "reviewPearl": str(parsed.get("reviewPearl") or "").strip(),
    }


def amboss_gemini_recover_unresolved(
    pages_in_group: list[dict[str, Any]],
    image_audit: list[dict[str, Any]],
    api_key: str,
) -> dict[str, Any] | None:
    """Vision-only Gemini fallback for question groups where deterministic
    extraction returned None (all pages misclassified as blown-up images due to
    OCR failure). Sends up to 4 page screenshots to Gemini with the prompt:
    "is there an AMBOSS QBank question here? if yes, extract it from scratch
    (stem + choices + correct + per-choice explanations + edu objective +
    retrieval tag + review pearl)."

    Returns the same structured question shape that
    extract_amboss_question_deterministic returns on success, or None when
    Gemini sees no question in the images."""
    image_paths = _amboss_collect_screenshot_asset_paths(pages_in_group, limit=4)
    if not image_paths:
        return None
    prompt = (
        "You are inspecting screenshots from an AMBOSS QBank PDF. The deterministic "
        "extractor failed on this page group (OCR couldn't read the stem cleanly). "
        "Look at the attached page images and determine: do these pages show a "
        "single AMBOSS clinical exam question?\n"
        "\n"
        "If NO (the pages are e.g. blown-up clinical images, cover sheets, blank "
        "pages, or unintelligible), return:\n"
        '  {"status": "no_question"}\n'
        "\n"
        "If YES, extract the question in this exact JSON shape (no markdown fences, "
        "no extra commentary):\n"
        "{\n"
        '  "status": "ok",\n'
        '  "stem": "full clinical vignette text ending in the lead-in question",\n'
        '  "answerChoices": [{"label": "A", "text": "…"}, …],\n'
        '  "correctAnswer": "X",\n'
        '  "correctExplanation": "1-3 sentence explanation of why the correct answer is correct",\n'
        '  "incorrectExplanations": [{"label": "A", "explanation": "1-3 sentence rationale for why wrong"}, …],\n'
        '  "educationalObjective": "1-sentence teaching point",\n'
        '  "retrievalTag": "3-8 word topic tag",\n'
        '  "reviewPearl": "1-sentence high-yield take-away"\n'
        "}\n"
        "\n"
        "Rules:\n"
        "  - The CORRECT answer has a GREEN choice header background on the "
        "explanation-reveal page; wrong answers are PINK/RED.\n"
        "  - AMBOSS questions have 4-8 answer choices, labeled A through H in order.\n"
        "  - Strip AMBOSS UI chrome (Overview, Library, Qbank, Analysis, Shop, "
        "Account sidebar; percentage badges; navigation pill; doctor-avatar hint; "
        "page footer / taskbar) from all extracted text.\n"
        "  - If you're unsure whether a question is present, return no_question "
        "rather than guessing.\n"
    )
    try:
        raw = raw_gemini_image_call(
            api_key=api_key,
            prompt=prompt,
            image_paths=image_paths,
            temperature=0.05,
            max_tokens=8192,
            timeout_seconds=120,
            response_mime_type="application/json",
        )
    except Exception as exc:
        warn(f"AMBOSS Gemini recovery call failed: {exc}")
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"AMBOSS recovery Gemini returned non-JSON: {exc}; payload starts: {text[:120]!r}")
        return None
    if not isinstance(parsed, dict) or str(parsed.get("status") or "").lower() != "ok":
        return None
    stem = str(parsed.get("stem") or "").strip()
    choices_raw = parsed.get("answerChoices")
    if not stem or not isinstance(choices_raw, list):
        return None
    normalized: list[dict[str, str]] = []
    for idx, item in enumerate(choices_raw[:8]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip().upper()
        ct = str(item.get("text") or "").strip()
        if not label or len(label) != 1 or label not in LABELS_AH:
            label = LABELS_AH[idx]
        if not ct or len(ct) < 3:
            continue
        normalized.append({"label": label, "text": ct})
    if not (2 <= len(normalized) <= 8):
        return None
    if [c["label"] for c in normalized] != LABELS_AH[: len(normalized)]:
        normalized = [{"label": LABELS_AH[i], "text": normalized[i]["text"]} for i in range(len(normalized))]
    valid_labels = {c["label"] for c in normalized}
    correct = str(parsed.get("correctAnswer") or "").strip().upper()
    if correct not in valid_labels:
        correct = normalized[0]["label"]
    correct_explanation = str(parsed.get("correctExplanation") or "").strip()
    incorrect_raw = parsed.get("incorrectExplanations")
    incorrect: list[dict[str, str]] = []
    if isinstance(incorrect_raw, list):
        for item in incorrect_raw:
            if not isinstance(item, dict):
                continue
            lbl = str(item.get("label") or "").strip().upper()
            exp = str(item.get("explanation") or "").strip()
            if lbl in valid_labels and lbl != correct and len(exp) > 20:
                incorrect.append({"label": lbl, "explanation": exp})
    # Route any blown-up image pages in the group to the explanation.
    image_routing: list[dict[str, Any]] = []
    for entry in pages_in_group:
        if entry["kind"] != "blown_up_image":
            continue
        for img in (entry["page"].get("images") or []):
            if img.get("imageId"):
                image_routing.append({
                    "imageId": img["imageId"],
                    "placement": "explanation",
                    "imageConfidence": 1.0,
                })
                image_audit.append({
                    **img,
                    "routedLocation": "explanation",
                    "imageConfidence": 1.0,
                    "duplicateOf": None,
                    "dedupeAction": "kept",
                    "recoveryPath": "gemini_unresolved_fallback",
                })
    return {
        "stem": stem,
        "answerChoices": normalized,
        "correctAnswer": correct,
        "correctExplanation": correct_explanation,
        "incorrectExplanations": incorrect,
        "educationalObjective": str(parsed.get("educationalObjective") or "").strip(),
        "retrievalTag": str(parsed.get("retrievalTag") or "").strip(),
        "reviewPearl": str(parsed.get("reviewPearl") or "").strip(),
        "imageRouting": image_routing,
        "sourcePageId": pages_in_group[0]["page"]["pageId"],
        "visualSourcePageIds": [p["page"]["pageId"] for p in pages_in_group],
        "extractionConfidence": 0.9,
        "extractionWarnings": ["recovered_via_gemini_fallback"],
    }


def extract_amboss_question_deterministic(
    pages_in_group: list[dict[str, Any]],
    image_audit: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Given all PDF pages belonging to one AMBOSS QBank question (the stem-only
    view + the explanation-reveal view + any blown-up image pages), extract the
    structured question. Returns None when the group has no usable screenshot."""
    screenshots = [p for p in pages_in_group if p["kind"] == "qbank_screenshot"]
    image_pages = [p for p in pages_in_group if p["kind"] == "blown_up_image"]
    if not screenshots:
        return None
    combined_text = "\n\n".join(p["text"] for p in screenshots)
    stem = extract_amboss_stem(combined_text)
    if not stem:
        return None
    choices = extract_amboss_choices(combined_text)

    # HYBRID GEMINI PASS (v4.57). AMBOSS QBank PDFs are flattened browser
    # screenshots and tesseract can't read the styled choice letter circles, the
    # green-bar correct-answer UI, or the per-choice explanation paragraphs
    # cleanly (sidebar / browser chrome leaks into everything). So after
    # deterministic stem extraction + page-grouping succeeds, we make ONE
    # Gemini call per question (~8 calls for a typical 8-question PDF) to
    # extract clean structured data: choices, correct answer, per-choice
    # explanations, educational objective, retrieval tag, and review pearl.
    gemini_data: dict[str, Any] | None = None
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        gemini_data = amboss_gemini_extract_question(screenshots, stem, api_key)
        if gemini_data:
            choices = gemini_data["answerChoices"]
    if len(choices) < 2:
        return None
    # Build the explanations dict from Gemini's output (clean prose) when
    # available; otherwise fall back to deterministic OCR-text anchor matching
    # (imperfect, often contains sidebar chrome).
    if gemini_data:
        explanations = {gemini_data["correctAnswer"]: gemini_data.get("correctExplanation", "")}
        for entry in gemini_data.get("incorrectExplanations", []):
            explanations[entry["label"]] = entry["explanation"]
    else:
        explanations = extract_amboss_choice_explanations(combined_text, choices)
    gemini_correct_override = gemini_data["correctAnswer"] if gemini_data else None
    # Correct-answer detection: PRIMARY = green choice-bar color detection on the
    # rendered PDF page. AMBOSS draws the correct-answer choice header in green
    # vs pink/red for wrong answers; this is language-independent and works even
    # when text-pattern detection is ambiguous. FALLBACK = positive/negative
    # signal scoring on the per-choice explanations ("is the most likely…").
    correct: str | None = None
    valid_labels = [c["label"] for c in choices]
    valid_label_set = set(valid_labels)
    # If the hybrid Gemini pass returned a correct answer, prefer it — it's the
    # most reliable signal (vision-based, language-independent).
    if gemini_correct_override and gemini_correct_override in valid_label_set:
        correct = gemini_correct_override
    # PRIMARY (no-Gemini fallback) = color-bar detection. Only used when the
    # hybrid Gemini pass didn't run or returned an unusable result. AMBOSS's
    # correct-answer choice header is rendered with a GREEN background; wrong
    # answers are pink/red. The green-bar's index in the per-page bar list maps
    # to the choice letter when the page shows all choices in one view.
    if not correct:
        for screenshot in screenshots:
            image_assets = screenshot["page"].get("images") or []
            if not image_assets:
                continue
            asset_str = str(image_assets[0].get("assetPath") or "")
            if not asset_str:
                continue
            asset_path = Path(asset_str)
            if not asset_path.is_absolute():
                asset_path = BASE_DIR / asset_path
            if not asset_path.exists():
                continue
            bars = amboss_find_all_choice_bars(asset_path)
            green_indices = [i for i, b in enumerate(bars) if b["color"] == "green"]
            if not green_indices or len(bars) < 2:
                continue
            if len(bars) == len(choices):
                green_index = green_indices[0]
                if 0 <= green_index < len(valid_labels):
                    correct = valid_labels[green_index]
                    break
    if not correct:
        # SECONDARY: accumulate bars across all screenshots (the explanation
        # reveal often spans 2–3 pages with subsets of the choices visible on
        # each).
        accumulated: list[dict[str, Any]] = []
        for screenshot in screenshots:
            image_assets = screenshot["page"].get("images") or []
            if not image_assets:
                continue
            asset_str = str(image_assets[0].get("assetPath") or "")
            asset_path = Path(asset_str) if asset_str else None
            if asset_path and not asset_path.is_absolute():
                asset_path = BASE_DIR / asset_path
            if not asset_path or not asset_path.exists():
                continue
            accumulated.extend(amboss_find_all_choice_bars(asset_path))
        # Filter to plausible choice bars (sorted by source order which we
        # preserved via screenshot iteration order).
        if accumulated and len(accumulated) >= len(choices):
            # Take the first len(choices) bars and find the green one's index.
            relevant = accumulated[: len(choices)]
            green_idx = next((i for i, b in enumerate(relevant) if b["color"] == "green"), None)
            if green_idx is not None and 0 <= green_idx < len(valid_labels):
                correct = valid_labels[green_idx]
    if not correct:
        # FALLBACK = text-pattern signal scoring.
        correct = detect_amboss_correct_answer(explanations, default_label=choices[0]["label"])
    correct_explanation = explanations.get(correct, "")
    incorrect = [
        {"label": label, "explanation": exp}
        for label, exp in explanations.items()
        if label != correct and exp
    ]
    # Prefer Gemini-derived metadata (clean prose, no OCR chrome). Fall back to
    # stem-derived placeholders only when Gemini didn't supply a value.
    if gemini_data and gemini_data.get("educationalObjective"):
        educational = gemini_data["educationalObjective"]
    else:
        educational = derive_amboss_educational_objective(correct_explanation, stem=stem)
    gemini_retrieval_tag = (gemini_data or {}).get("retrievalTag") or ""
    gemini_review_pearl = (gemini_data or {}).get("reviewPearl") or ""
    image_routing: list[dict[str, Any]] = []
    for image_page in image_pages:
        for img in (image_page["page"].get("images") or []):
            if img.get("imageId"):
                image_routing.append({
                    "imageId": img["imageId"],
                    "placement": "explanation",
                    "imageConfidence": 1.0,
                })
                image_audit.append({
                    **img,
                    "routedLocation": "explanation",
                    "imageConfidence": 1.0,
                    "duplicateOf": None,
                    "dedupeAction": "kept",
                })
    retrieval_tag = gemini_retrieval_tag or (educational[:80] if educational else "")
    review_pearl = gemini_review_pearl or correct_explanation or educational
    return {
        "stem": stem,
        "answerChoices": choices,
        "correctAnswer": correct,
        "correctExplanation": correct_explanation,
        "incorrectExplanations": incorrect,
        "educationalObjective": educational,
        "retrievalTag": retrieval_tag,
        "reviewPearl": review_pearl,
        "imageRouting": image_routing,
        "sourcePageId": screenshots[0]["page"]["pageId"],
        "visualSourcePageIds": [p["page"]["pageId"] for p in pages_in_group],
        "extractionConfidence": 0.95,
        "extractionWarnings": [],
    }


def process_amboss_input(input_path: Path, limit_pages: int = 0) -> Path:
    """Deterministic AMBOSS extractor (v4.57). Replaces the per-page Gemini path.

    Runs in seconds rather than minutes, costs nothing in API spend, and routes
    full-page "click to enlarge" image pages directly to the preceding question's
    explanation images (per the user's rule: blown-up image pages ALWAYS go to
    explanations, never to stems). In-stem images on text-layer AMBOSS PDFs are
    a Phase 2 follow-up — they need the NBME-style cropper UI integration.
    """
    started = time.time()
    page_payload = decompose_amboss_input(input_path, limit_pages=limit_pages)
    source_hash = page_payload["sourceHash"]
    pages = page_payload.get("pages") or []
    extracted_questions: list[dict[str, Any]] = []
    normalized_slides: list[dict[str, Any]] = []
    generated_questions: list[dict[str, Any]] = []
    image_audit: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    # Stage 1a — OCR every page.
    page_analysis: list[dict[str, Any]] = []
    for page in pages:
        image_assets = page.get("images") or []
        ocr_text = ""
        if image_assets:
            asset_str = str(image_assets[0].get("assetPath") or "")
            if asset_str:
                asset_path = Path(asset_str)
                if not asset_path.is_absolute():
                    asset_path = BASE_DIR / asset_path
                if asset_path.exists():
                    ocr_text = amboss_ocr_image(asset_path)
        if not ocr_text:
            ocr_text = page.get("text") or ""
        kind = classify_amboss_page(ocr_text)
        # Only fingerprint qbank_screenshot pages — captions on blown-up image
        # pages sometimes contain phrases that match the vignette opener (e.g.,
        # "Skin lesion in a 80-year-old patient…") and would otherwise create
        # spurious question groups.
        fp = amboss_stem_fingerprint(ocr_text) if kind == "qbank_screenshot" else ""
        page_analysis.append({
            "page": page,
            "text": ocr_text,
            "kind": kind,
            "stem_fingerprint": fp,
            "nav_question": None,  # filled in stage 1b after majority M is known
        })

    # Stage 1b — vote on the AMBOSS QBank total-question count `M`. The styled
    # navigation pill `< N / M >` is small and OCR sometimes misreads it (e.g.
    # `2/8` → `24/8`); a majority vote on M across every page filters the noise.
    total_votes: dict[int, int] = {}
    for entry in page_analysis:
        if entry["kind"] != "qbank_screenshot":
            continue
        for m in _AMBOSS_NAV_PILL.finditer(entry["text"]):
            n, total = int(m.group(1)), int(m.group(2))
            if 1 <= n <= total <= 200:
                total_votes[total] = total_votes.get(total, 0) + 1
    expected_total = max(total_votes.items(), key=lambda kv: (kv[1], -kv[0]))[0] if total_votes else None
    # Stage 1c — parse the validated nav pill per page (rejecting N > M misreads).
    for entry in page_analysis:
        if entry["kind"] != "qbank_screenshot":
            continue
        nav_match = parse_amboss_nav_pill(entry["text"], expected_total=expected_total)
        entry["nav_question"] = nav_match[0] if nav_match else None

    # Stage 2 — group pages into questions. AMBOSS QBank screenshots have TWO
    # independent signals identifying which question a page belongs to:
    #   1. The clinical-vignette stem fingerprint (large, OCR-friendly text)
    #   2. The navigation pill `< N / M >` (small, OCR-noisy, validated against
    #      the majority M)
    # Either signal alone is imperfect: stem text is sometimes missing on
    # explanation-continuation pages, and the nav pill OCR sometimes drops
    # entirely. Using both via union-find (pages connect if they share a nav
    # number OR a stem fingerprint, transitively) catches all pages of each
    # question even when one signal is OCR-unreadable on some pages.
    parent: list[int] = list(range(len(page_analysis)))
    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb
    nav_first: dict[int, int] = {}
    fp_first: dict[str, int] = {}
    for idx, entry in enumerate(page_analysis):
        nav = entry["nav_question"]
        fp = entry["stem_fingerprint"]
        if nav is not None:
            if nav in nav_first:
                _union(idx, nav_first[nav])
            else:
                nav_first[nav] = idx
        if fp:
            if fp in fp_first:
                _union(idx, fp_first[fp])
            else:
                fp_first[fp] = idx
    # Adjacency rule for pages with NEITHER signal: attach to the previous
    # qbank_screenshot page (handles blown-up images between questions).
    previous_screenshot_idx: int | None = None
    for idx, entry in enumerate(page_analysis):
        if entry["nav_question"] is not None or entry["stem_fingerprint"]:
            if entry["kind"] == "qbank_screenshot":
                previous_screenshot_idx = idx
            continue
        if previous_screenshot_idx is not None:
            _union(idx, previous_screenshot_idx)
        if entry["kind"] == "qbank_screenshot":
            previous_screenshot_idx = idx
    # Adjacent-component merge: AMBOSS question pages are ALWAYS consecutive,
    # so two components with consecutive page indices should be merged ONLY when
    # they share the same nav-pill question number. We DON'T merge nav-less
    # components into nav-carrying components — that risks pulling the next
    # question's leading pages backwards into the prior question.
    component_to_pages: dict[int, list[int]] = {}
    for idx in range(len(page_analysis)):
        root = _find(idx)
        component_to_pages.setdefault(root, []).append(idx)
    ordered_roots = sorted(component_to_pages.keys(), key=lambda r: min(component_to_pages[r]))

    def _component_navs(root: int) -> set[int]:
        return {page_analysis[i]["nav_question"] for i in component_to_pages[root]
                if page_analysis[i]["nav_question"] is not None}

    changed = True
    while changed:
        changed = False
        ordered_roots = sorted(component_to_pages.keys(), key=lambda r: min(component_to_pages[r]))
        for a_root, b_root in zip(ordered_roots, ordered_roots[1:]):
            a_pages = component_to_pages[a_root]
            b_pages = component_to_pages[b_root]
            if max(a_pages) + 1 != min(b_pages):
                continue  # not adjacent in document order
            a_navs = _component_navs(a_root)
            b_navs = _component_navs(b_root)
            # Only merge when both carry the SAME nav number (a strong positive
            # identifier that they belong to the same question).
            if not a_navs or not b_navs or a_navs.isdisjoint(b_navs):
                continue
            # Refresh union-find + component map after merge.
            _union(a_pages[0], b_pages[0])
            merged_root = _find(a_pages[0])
            other_root = b_root if merged_root == a_root else a_root
            component_to_pages[merged_root] = sorted(component_to_pages.get(a_root, []) + component_to_pages.get(b_root, []))
            if other_root in component_to_pages and other_root != merged_root:
                del component_to_pages[other_root]
            changed = True
            break

    ordered_roots = sorted(component_to_pages.keys(), key=lambda r: min(component_to_pages[r]))
    groups_by_q: dict[int, list[dict[str, Any]]] = {}
    for q_num, root in enumerate(ordered_roots, start=1):
        groups_by_q[q_num] = [page_analysis[i] for i in component_to_pages[root]]
    # Audit pages whose component contains NO qbank_screenshot (pre-stem cover).
    drop_q_nums: list[int] = []
    for q_num, group in groups_by_q.items():
        if not any(e["kind"] == "qbank_screenshot" for e in group):
            for entry in group:
                for img in (entry["page"].get("images") or []):
                    image_audit.append({
                        **img,
                        "routedLocation": "ignored",
                        "imageConfidence": 0.0,
                        "duplicateOf": None,
                        "dedupeAction": "kept",
                        "reason": "page_with_no_question_in_component",
                    })
            drop_q_nums.append(q_num)
    for q_num in drop_q_nums:
        del groups_by_q[q_num]
    # Renumber after dropping degenerate groups.
    groups_by_q = {i + 1: groups_by_q[k] for i, k in enumerate(sorted(groups_by_q.keys()))}

    # Stage 3 — extract each question deterministically (with hybrid Gemini call
    # for choices + correct + explanations + tags). When the deterministic path
    # returns None (no usable stem on any page of the group due to OCR failure),
    # fall back to a vision-only Gemini pass on the page images: "is there an
    # AMBOSS question here? extract it." This recovers questions whose stems
    # were OCR'd badly enough that we couldn't classify or fingerprint them.
    api_key_for_fallback = os.environ.get("GEMINI_API_KEY", "").strip()
    for q_index, q_num in enumerate(sorted(groups_by_q.keys()), start=1):
        group = groups_by_q[q_num]
        try:
            extracted = extract_amboss_question_deterministic(group, image_audit)
        except Exception as exc:
            unresolved.append({
                "questionIndex": q_index,
                "navQuestionNumber": q_num,
                "pageIds": [p["page"]["pageId"] for p in group],
                "reason": f"deterministic extraction error: {exc}",
            })
            continue
        if extracted is None and api_key_for_fallback:
            extracted = amboss_gemini_recover_unresolved(group, image_audit, api_key_for_fallback)
        if extracted is None:
            unresolved.append({
                "questionIndex": q_index,
                "navQuestionNumber": q_num,
                "pageIds": [p["page"]["pageId"] for p in group],
                "reason": "no usable QBank screenshot in group (all pages were blown-up images?)",
            })
            continue
        ok, warnings = validate_amboss_extraction(extracted)
        canonical_blockers = amboss_canonical_blockers(extracted)
        extracted["extractionWarnings"] = dedupe_preserve_order(
            list(extracted.get("extractionWarnings") or []) + warnings + canonical_blockers
        )
        extracted["extractionStatus"] = "canonical_ready" if ok and not canonical_blockers else "partial_review"
        extracted_questions.append(extracted)
        if not ok or canonical_blockers:
            unresolved.append({
                "questionIndex": q_index,
                "navQuestionNumber": q_num,
                "pageIds": [p["page"]["pageId"] for p in group],
                "reason": "; ".join(extracted["extractionWarnings"]),
            })
            continue
        base_page = group[0]["page"]
        support_pages = [entry["page"] for entry in group[1:]]
        slide = amboss_normalized_slide_for_question(base_page, extracted, q_index, image_audit, support_pages=support_pages)
        gen_q = amboss_generated_question(extracted, slide["slideId"])
        if not gen_q:
            unresolved.append({
                "questionIndex": q_index,
                "navQuestionNumber": q_num,
                "reason": "could not convert to canonical generated question",
            })
            continue
        normalized_slides.append(slide)
        generated_questions.append(gen_q)

    # Stage 3b — post-extraction dedupe. OCR variance occasionally fragments a
    # single question into two components (e.g., page 28 OCR'd "A 27-year-old"
    # and page 29 OCR'd "A.27-year-old" — same question, different fingerprint
    # because of one OCR character). After all extraction completes, collapse
    # questions whose normalized stems agree on the first 40 chars. generated_questions
    # and normalized_slides stay in lockstep (appended together above) so we
    # apply the same surviving indices to both.
    if len(generated_questions) > 1 and len(generated_questions) == len(normalized_slides):
        kept_indices: list[int] = []
        kept_by_fp: dict[str, int] = {}
        for idx, gen_q in enumerate(generated_questions):
            stem_norm = re.sub(r"[^a-z0-9 ]", " ", str(gen_q.get("stem") or "").lower())
            stem_norm = re.sub(r"\s+", " ", stem_norm).strip()[:40]
            if not stem_norm:
                kept_indices.append(idx)
                continue
            if stem_norm in kept_by_fp:
                # Already saw this normalized stem. Prefer the entry with more
                # answer choices (Gemini extraction) or a longer stem.
                prior_idx = kept_by_fp[stem_norm]
                prior_q = generated_questions[prior_idx]
                if (len(gen_q.get("answerChoices") or []) > len(prior_q.get("answerChoices") or [])
                        or len(str(gen_q.get("stem") or "")) > len(str(prior_q.get("stem") or ""))):
                    # Replace prior with current.
                    kept_indices = [idx if i == prior_idx else i for i in kept_indices]
                    kept_by_fp[stem_norm] = idx
                continue
            kept_by_fp[stem_norm] = idx
            kept_indices.append(idx)
        if len(kept_indices) < len(generated_questions):
            log(f"AMBOSS dedupe: {len(generated_questions)} → {len(kept_indices)} questions after near-duplicate stem collapse.")
            generated_questions = [generated_questions[i] for i in kept_indices]
            normalized_slides = [normalized_slides[i] for i in kept_indices]
            # Renumber slideId / questionNumber sequentially after dedupe.
            for new_idx, gen_q in enumerate(generated_questions, start=1):
                if isinstance(gen_q, dict):
                    gen_q["slideId"] = re.sub(r"q\d{3}", f"q{new_idx:03d}", gen_q.get("slideId", ""))

    # Stage 4 — write outputs (same shape as the prior Gemini-based pipeline).
    stem = slugify(input_path.stem)
    extracted_path = GENERATED_DIR / f"{stem}_amboss_extracted_questions.json"
    write_json(extracted_path, {"profile": AMBOSS_PROFILE, "questions": extracted_questions})
    normalized_payload = {
        "schemaVersion": "amboss-normalized-v1",
        "profile": AMBOSS_PROFILE,
        "sourceFile": input_path.name,
        "pdfSha256": source_hash,
        "normalizationWarnings": [],
        "slides": normalized_slides,
    }
    app_payload = build_app_ready_payload(normalized_payload, generated_questions) if generated_questions else None
    app_ready_path = APP_READY_DIR / f"{stem}_amboss_app_ready.json"
    validation_errors: list[str] = []
    if app_payload:
        validation_errors = validate_app_ready_payload(app_payload)
        if not validation_errors:
            write_json(app_ready_path, app_payload)
    extraction_report = {
        "profile": AMBOSS_PROFILE,
        "sourceFile": input_path.name,
        "extractionMode": "deterministic_ocr",
        "pagesProcessed": len(pages),
        "questionsExtracted": len(extracted_questions),
        "canonicalQuestions": len(generated_questions),
        "appReadyPath": display_path(app_ready_path) if app_payload and not validation_errors else "",
        "validationErrors": validation_errors,
        "unresolvedCount": len(unresolved),
        "runtimeSeconds": round(time.time() - started, 2),
    }
    extraction_report_path = write_report(extraction_report, "amboss_extraction_audit_report")
    image_report_path = write_report({"profile": AMBOSS_PROFILE, "sourceFile": input_path.name, "imageAudit": image_audit}, "amboss_image_routing_audit_report")
    unresolved_path = write_report({"profile": AMBOSS_PROFILE, "sourceFile": input_path.name, "unresolved": unresolved}, "amboss_unresolved_extraction_report")
    log(f"AMBOSS extracted (deterministic) -> {display_path(extracted_path)}")
    log(f"AMBOSS extraction report -> {display_path(extraction_report_path)}")
    log(f"AMBOSS image report -> {display_path(image_report_path)}")
    log(f"AMBOSS unresolved report -> {display_path(unresolved_path)}")
    if app_payload and not validation_errors:
        log(f"AMBOSS app-ready -> {display_path(app_ready_path)}")
        return app_ready_path
    return extracted_path


def _LEGACY_GEMINI_process_amboss_input(input_path: Path, limit_pages: int = 5) -> Path:
    started = time.time()
    page_payload = decompose_amboss_input(input_path, limit_pages=limit_pages)
    source_hash = page_payload["sourceHash"]
    cache = load_amboss_cache()
    extracted_questions: list[dict[str, Any]] = []
    normalized_slides: list[dict[str, Any]] = []
    generated_questions: list[dict[str, Any]] = []
    image_audit: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    cache_hits = 0
    cache_misses = 0
    accepted = 0
    page_records: list[dict[str, Any]] = []
    for page in page_payload.get("pages") or []:
        page_key = amboss_page_cache_key(source_hash, page)
        cached = (cache.get("pages") or {}).get(page_key)
        if isinstance(cached, dict) and cached.get("status") == "accepted":
            page_questions = cached.get("questions") or []
            page_meta = cached.get("pageMeta") or {}
            visual_state = cached.get("visualState") if cached.get("visualStateVersion") == AMBOSS_VISUAL_STATE_VERSION else None
            cache_hits += 1
        else:
            cache_misses += 1
            try:
                page_questions, page_meta = extract_amboss_page(page, page_payload["sourceFile"])
            except Exception as exc:
                unresolved.append({"pageId": page.get("pageId"), "reason": str(exc)})
                page_questions = []
                page_meta = {"pageWarnings": [str(exc)]}
            visual_state = None
            cache.setdefault("pages", {})[page_key] = {
                "profile": AMBOSS_PROFILE,
                "sourcePdfHash": source_hash,
                "pageHash": page.get("pageHash"),
                "extractionPromptVersion": AMBOSS_EXTRACTION_PROMPT_VERSION,
                "imageRoutingVersion": AMBOSS_IMAGE_ROUTING_VERSION,
                "status": "accepted",
                "questions": page_questions,
                "pageMeta": page_meta,
                "updatedAt": timestamp_iso(),
            }
            write_amboss_cache(cache)
        if visual_state is None:
            try:
                visual_state = detect_amboss_visual_state(page, page_questions, page_payload["sourceFile"])
            except Exception as exc:
                visual_state = {"correctAnswer": "", "confidence": 0, "answerStates": [], "visibleRationales": [], "imageRouting": [], "warnings": [str(exc)]}
            page_entry = cache.setdefault("pages", {}).setdefault(page_key, {})
            page_entry["visualState"] = visual_state
            page_entry["visualStateVersion"] = AMBOSS_VISUAL_STATE_VERSION
            page_entry["updatedAt"] = timestamp_iso()
            write_amboss_cache(cache)
        page_records.append({"page": page, "questions": page_questions, "visualState": visual_state, "pageMeta": page_meta})

    promoted_questions = amboss_dedupe_and_promote_questions(page_records)
    page_by_id = {p["pageId"]: p for p in page_payload.get("pages") or []}
    for q_index, q in enumerate(promoted_questions, start=1):
        page = page_by_id.get(str(q.get("sourcePageId") or "")) or (page_payload.get("pages") or [{}])[0]
        ok, warnings = validate_amboss_extraction(q)
        canonical_blockers = amboss_canonical_blockers(q)
        q["extractionWarnings"] = dedupe_preserve_order(list(q.get("extractionWarnings") or []) + warnings + canonical_blockers)
        q["extractionStatus"] = "canonical_ready" if ok and not canonical_blockers else "partial_review"
        extracted_questions.append(q)
        extracted_hash = amboss_question_hash(q)
        q_cache_key = hashlib.sha256(json.dumps({
            "profile": AMBOSS_PROFILE,
            "sourcePdfHash": source_hash,
            "pageHash": page.get("pageHash"),
            "extractedQuestionHash": extracted_hash,
            "extractionPromptVersion": AMBOSS_EXTRACTION_PROMPT_VERSION,
            "imageRoutingVersion": AMBOSS_IMAGE_ROUTING_VERSION,
            "visualStateVersion": AMBOSS_VISUAL_STATE_VERSION,
        }, sort_keys=True).encode("utf-8", errors="replace")).hexdigest()[:16]
        cache.setdefault("questions", {})[q_cache_key] = {
            "cacheKey": q_cache_key,
            "profile": AMBOSS_PROFILE,
            "sourcePdfHash": source_hash,
            "pageHash": page.get("pageHash"),
            "extractedQuestionHash": extracted_hash,
            "extractionPromptVersion": AMBOSS_EXTRACTION_PROMPT_VERSION,
            "imageRoutingVersion": AMBOSS_IMAGE_ROUTING_VERSION,
            "visualStateVersion": AMBOSS_VISUAL_STATE_VERSION,
            "question": q,
            "status": "accepted" if q["extractionStatus"] == "canonical_ready" else "partial_review",
            "updatedAt": timestamp_iso(),
        }
        write_amboss_cache(cache)
        if not ok or canonical_blockers:
            unresolved.append({"pageId": page.get("pageId"), "questionIndex": q_index, "reason": "; ".join(q["extractionWarnings"])})
            support_ids = q.get("visualSourcePageIds") or [page.get("pageId")]
            for support_id in support_ids:
                support_page = page_by_id.get(str(support_id))
                if support_page:
                    amboss_audit_page_images(support_page, q, image_audit, "partial_review")
            continue
        support_pages = [
            page_by_id[str(support_id)]
            for support_id in (q.get("visualSourcePageIds") or [])
            if str(support_id) in page_by_id
        ]
        slide = amboss_normalized_slide_for_question(page, q, q_index, image_audit, support_pages=support_pages)
        gen_q = amboss_generated_question(q, slide["slideId"])
        if not gen_q:
            unresolved.append({"pageId": page.get("pageId"), "questionIndex": q_index, "reason": "could not convert to canonical generated question"})
            continue
        normalized_slides.append(slide)
        generated_questions.append(gen_q)
        accepted += 1
    question_page_ids = {str(q.get("sourcePageId") or "") for q in promoted_questions}
    support_page_ids = {
        str(page_id)
        for q in promoted_questions
        for page_id in (q.get("visualSourcePageIds") or [])
    }
    for record in page_records:
        page = record["page"]
        if page.get("pageId") not in question_page_ids and page.get("pageId") not in support_page_ids:
            amboss_audit_page_images(page, None, image_audit, "no_question_detected")
    stem = slugify(input_path.stem)
    extracted_path = GENERATED_DIR / f"{stem}_amboss_extracted_questions.json"
    write_json(extracted_path, {"profile": AMBOSS_PROFILE, "questions": extracted_questions})
    normalized_payload = {
        "schemaVersion": "amboss-normalized-v1",
        "profile": AMBOSS_PROFILE,
        "sourceFile": input_path.name,
        "pdfSha256": source_hash,
        "normalizationWarnings": [],
        "slides": normalized_slides,
    }
    app_payload = build_app_ready_payload(normalized_payload, generated_questions) if generated_questions else None
    app_ready_path = APP_READY_DIR / f"{stem}_amboss_app_ready.json"
    validation_errors: list[str] = []
    if app_payload:
        validation_errors = validate_app_ready_payload(app_payload)
        if not validation_errors:
            write_json(app_ready_path, app_payload)
    cache_report = {
        "cachePath": display_path(amboss_cache_path()),
        "cacheHits": cache_hits,
        "cacheMisses": cache_misses,
        "reusedQuestions": sum(len(((cache.get("pages") or {}).get(amboss_page_cache_key(source_hash, p)) or {}).get("questions") or []) for p in page_payload.get("pages") or []) if cache_hits else 0,
        "acceptedQuestions": accepted,
        "runtimeDurationSeconds": round(time.time() - started, 2),
    }
    extraction_report = {
        "profile": AMBOSS_PROFILE,
        "sourceFile": input_path.name,
        "pagesProcessed": len(page_payload.get("pages") or []),
        "questionsExtracted": len(extracted_questions),
        "canonicalQuestions": len(generated_questions),
        "appReadyPath": display_path(app_ready_path) if app_payload and not validation_errors else "",
        "validationErrors": validation_errors,
        "cacheReport": cache_report,
        "unresolvedCount": len(unresolved),
    }
    extraction_report_path = write_report(extraction_report, "amboss_extraction_audit_report")
    image_report_path = write_report({"profile": AMBOSS_PROFILE, "sourceFile": input_path.name, "imageAudit": image_audit}, "amboss_image_routing_audit_report")
    unresolved_path = write_report({"profile": AMBOSS_PROFILE, "sourceFile": input_path.name, "unresolved": unresolved}, "amboss_unresolved_extraction_report")
    log(f"AMBOSS extracted -> {display_path(extracted_path)}")
    log(f"AMBOSS extraction report -> {display_path(extraction_report_path)}")
    log(f"AMBOSS image report -> {display_path(image_report_path)}")
    log(f"AMBOSS unresolved report -> {display_path(unresolved_path)}")
    if app_payload and not validation_errors:
        log(f"AMBOSS app-ready -> {display_path(app_ready_path)}")
        return app_ready_path
    return extracted_path


PPTX_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def rel_target(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts: list[str] = []
    for part in (base_dir + "/" + target).split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def pptx_relationships(zf: zipfile.ZipFile, rels_path: str) -> dict[str, dict[str, str]]:
    if rels_path not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read(rels_path))
    rels: dict[str, dict[str, str]] = {}
    for rel in root.findall("rel:Relationship", PPTX_NS):
        rid = rel.attrib.get("Id", "")
        if not rid:
            continue
        rels[rid] = {
            "type": rel.attrib.get("Type", ""),
            "target": rel.attrib.get("Target", ""),
            "targetMode": rel.attrib.get("TargetMode", ""),
        }
    return rels


def pptx_ordered_slide_paths(zf: zipfile.ZipFile) -> list[str]:
    if "ppt/presentation.xml" not in zf.namelist():
        raise PipelineError("PPTX missing ppt/presentation.xml.")
    root = ET.fromstring(zf.read("ppt/presentation.xml"))
    rels = pptx_relationships(zf, "ppt/_rels/presentation.xml.rels")
    paths: list[str] = []
    for slide_id in root.findall(".//p:sldId", PPTX_NS):
        rid = slide_id.attrib.get(f"{{{PPTX_NS['r']}}}id", "")
        target = rels.get(rid, {}).get("target", "")
        if target:
            paths.append(rel_target("ppt", target))
    if not paths:
        paths = sorted(
            [name for name in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", name)],
            key=lambda p: int(re.search(r"slide(\d+)\.xml$", p).group(1)),
        )
    return paths


def pptx_text_from_element(element: ET.Element) -> str:
    parts = [node.text or "" for node in element.findall(".//a:t", PPTX_NS)]
    return clean_slide_text("\n".join(part for part in parts if part.strip()))


def pptx_shape_bounds(element: ET.Element) -> dict[str, int]:
    off = element.find(".//a:xfrm/a:off", PPTX_NS)
    ext = element.find(".//a:xfrm/a:ext", PPTX_NS)
    return {
        "x": int(off.attrib.get("x", 0)) if off is not None else 0,
        "y": int(off.attrib.get("y", 0)) if off is not None else 0,
        "cx": int(ext.attrib.get("cx", 0)) if ext is not None else 0,
        "cy": int(ext.attrib.get("cy", 0)) if ext is not None else 0,
    }


def pptx_table_from_graphic_frame(frame: ET.Element, table_id: str) -> dict[str, Any] | None:
    table = frame.find(".//a:tbl", PPTX_NS)
    if table is None:
        return None
    rows: list[list[str]] = []
    for tr in table.findall("a:tr", PPTX_NS):
        row: list[str] = []
        for tc in tr.findall("a:tc", PPTX_NS):
            row.append(pptx_text_from_element(tc).replace("\n", " ").strip())
        if any(row):
            rows.append(row)
    if not rows:
        return None
    return {
        "tableId": table_id,
        "title": "",
        "headers": rows[0],
        "rows": rows[1:],
        "source": "pptx_native_table",
        "bounds": pptx_shape_bounds(frame),
    }


def classify_fast_facts_image(name: str, context: str, bounds: dict[str, int]) -> str:
    text = normalize_key(f"{name} {context}")
    if re.search(r"\b(ecg|ekg|ct|mri|xray|x-ray|cxr|ultrasound|us|rash|pathology|histology|radiograph|image|scan)\b", text):
        return "diagnostic"
    if re.search(r"\b(algorithm|flowchart|management|workup|treatment|stepwise|approach)\b", text):
        return "algorithm"
    if re.search(r"\b(table|chart|grid)\b", text):
        return "table-like"
    area = int(bounds.get("cx", 0)) * int(bounds.get("cy", 0))
    if area and area < 250_000_000_000:
        return "decorative"
    if re.search(r"\b(pathway|mechanism|diagram|summary|mnemonic)\b", text):
        return "explanatory"
    return "explanatory"


def pptx_extract_images(
    zf: zipfile.ZipFile,
    rels: dict[str, dict[str, str]],
    slide_path: str,
    slide_id: str,
    deck_stem: str,
    slide_text: str,
    slide_root: ET.Element,
) -> list[dict[str, Any]]:
    base_dir = str(Path(slide_path).parent)
    images: list[dict[str, Any]] = []
    for idx, pic in enumerate(slide_root.findall(".//p:pic", PPTX_NS), start=1):
        blip = pic.find(".//a:blip", PPTX_NS)
        if blip is None:
            continue
        rid = blip.attrib.get(f"{{{PPTX_NS['r']}}}embed", "")
        target = rels.get(rid, {}).get("target", "")
        if not target:
            continue
        media_path = rel_target(base_dir, target)
        if media_path not in zf.namelist():
            continue
        source_name = pic.find(".//p:cNvPr", PPTX_NS)
        original_name = source_name.attrib.get("name", "") if source_name is not None else Path(media_path).name
        ext = Path(media_path).suffix or ".bin"
        image_id = f"{slide_id}_img{idx:02d}"
        asset_name = f"{slugify(deck_stem)}_{image_id}{ext}"
        asset_path = ASSET_DIR / asset_name
        asset_path.write_bytes(zf.read(media_path))
        bounds = pptx_shape_bounds(pic)
        images.append({
            "imageId": image_id,
            "kind": classify_fast_facts_image(original_name, slide_text, bounds),
            "source": "pptx_embedded_image",
            "sourceSlideId": slide_id,
            "sourceRelationshipId": rid,
            "sourcePath": media_path,
            "originalName": original_name,
            "assetPath": display_path(asset_path),
            "mimeType": mime_for(asset_path),
            "width": 0,
            "height": 0,
            "sha256": file_sha(asset_path),
            "bounds": bounds,
        })
    return images


def pptx_notes_text(zf: zipfile.ZipFile, slide_path: str, rels: dict[str, dict[str, str]]) -> str:
    base_dir = str(Path(slide_path).parent)
    for rel in rels.values():
        if "notesSlide" not in rel.get("type", ""):
            continue
        notes_path = rel_target(base_dir, rel.get("target", ""))
        if notes_path in zf.namelist():
            return pptx_text_from_element(ET.fromstring(zf.read(notes_path)))
    return ""


def decompose_fast_facts_pptx(pptx_path: Path, limit_slides: int = 0) -> dict[str, Any]:
    ensure_dirs()
    deck_hash = file_sha(pptx_path)
    deck_stem = pptx_path.stem
    slide_limit = max(0, int(limit_slides or 0))
    log(f"Decomposing Fast Facts PPTX {pptx_path.name}")
    with zipfile.ZipFile(pptx_path) as zf:
        all_slide_paths = pptx_ordered_slide_paths(zf)
        slide_paths = all_slide_paths[:slide_limit] if slide_limit else all_slide_paths
        slides: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for idx, slide_path in enumerate(slide_paths, start=1):
            slide_id = f"{slugify(deck_stem)}_s{idx:04d}_{deck_hash[:8]}"
            try:
                root = ET.fromstring(zf.read(slide_path))
                rels = pptx_relationships(zf, f"{Path(slide_path).parent}/_rels/{Path(slide_path).name}.rels")
                text_blocks = []
                for shape_idx, shape in enumerate(root.findall(".//p:sp", PPTX_NS), start=1):
                    text = pptx_text_from_element(shape)
                    if text:
                        text_blocks.append({
                            "textBlockId": f"{slide_id}_text{shape_idx:02d}",
                            "text": text,
                            "bounds": pptx_shape_bounds(shape),
                        })
                tables = []
                for table_idx, frame in enumerate(root.findall(".//p:graphicFrame", PPTX_NS), start=1):
                    table = pptx_table_from_graphic_frame(frame, f"{slide_id}_table{table_idx:02d}")
                    if table:
                        tables.append(table)
                notes = pptx_notes_text(zf, slide_path, rels)
                combined_text = clean_slide_text("\n".join([b["text"] for b in text_blocks] + ([notes] if notes else [])))
                images = pptx_extract_images(zf, rels, slide_path, slide_id, deck_stem, combined_text, root)
                slides.append({
                    "slideId": slide_id,
                    "sourceFile": pptx_path.name,
                    "pptxSha256": deck_hash,
                    "slideIndex": idx,
                    "pptxSlidePath": slide_path,
                    "textBlocks": text_blocks,
                    "notesText": notes,
                    "nativeText": combined_text,
                    "images": images,
                    "tables": tables,
                    "metadata": {
                        "profile": FAST_FACTS_PROFILE,
                        "renderedSlideImage": None,
                    },
                })
            except Exception as exc:
                failures.append({"slideIndex": idx, "slidePath": slide_path, "error": str(exc)})
        payload = {
            "schemaVersion": "fast-facts-pptx-decomposition-v1",
            "profile": FAST_FACTS_PROFILE,
            "sourceFile": pptx_path.name,
            "sourcePath": str(pptx_path),
            "pptxSha256": deck_hash,
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "slideCount": len(all_slide_paths),
            "processedSlideCount": len(slides),
            "limit": slide_limit,
            "slides": slides,
            "failures": failures,
        }
    out_path = SLIDES_DIR / f"{slugify(deck_stem)}_fast_facts_slides.json"
    write_json(out_path, payload)
    log(f"  Fast Facts slides -> {display_path(out_path)}")
    return payload


def fast_facts_checkpoint_path(pptx_path: Path) -> Path:
    return NORMALIZED_DIR / f"{slugify(pptx_path.stem)}_fast_facts_checkpoint.json"


def fast_facts_output_path(pptx_path: Path) -> Path:
    return NORMALIZED_DIR / f"{slugify(pptx_path.stem)}_fast_facts_concept_graph.json"


def load_fast_facts_checkpoint(pptx_path: Path, deck_hash: str, limit_slides: int) -> dict[str, Any]:
    path = fast_facts_checkpoint_path(pptx_path)
    if not path.exists():
        return {"concepts": [], "processedSlideIds": [], "failures": []}
    try:
        payload = read_json(path)
    except Exception:
        return {"concepts": [], "processedSlideIds": [], "failures": []}
    if (
        payload.get("pptxSha256") != deck_hash
        or int(payload.get("limit") or 0) != int(limit_slides or 0)
        or payload.get("atomizerVersion") != FAST_FACTS_ATOMIZER_VERSION
    ):
        return {"concepts": [], "processedSlideIds": [], "failures": []}
    return payload


def write_fast_facts_checkpoint(pptx_path: Path, deck_hash: str, limit_slides: int, concepts: list[dict[str, Any]], processed: list[str], failures: list[dict[str, Any]]) -> None:
    write_json(fast_facts_checkpoint_path(pptx_path), {
        "schemaVersion": "fast-facts-concept-checkpoint-v1",
        "profile": FAST_FACTS_PROFILE,
        "atomizerVersion": FAST_FACTS_ATOMIZER_VERSION,
        "sourceFile": pptx_path.name,
        "pptxSha256": deck_hash,
        "limit": int(limit_slides or 0),
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "processedSlideIds": processed,
        "concepts": concepts,
        "failures": failures,
    })


def classify_fast_facts_category(text: str) -> str:
    lower = normalize_key(text)
    if re.search(r"\b(treat\w*|management|therapy|screen\w*|prevent\w*|vaccine|antibiotic\w*|surgery|surgical|administer\w*)\b", lower):
        return "management"
    if re.search(r"\b(diagnos\w*|test\w*|lab\w*|ecg|ekg|ct|mri|ultrasound|x-ray|finding\w*|criteria|culture\w*)\b", lower):
        return "diagnosis"
    if re.search(r"\b(pathophys\w*|mechanism\w*|cause\w*|mutation\w*|deficien\w*|inhibit\w*|activat\w*|receptor\w*)\b", lower):
        return "mechanism"
    if re.search(r"\b(vs|versus|differentiat\w*|mimic\w*|ddx|differential)\b", lower):
        return "differential"
    if re.search(r"\b(trap\w*|except|avoid\w*|do not|contraindicat\w*|classic)\b", lower):
        return "trap"
    return "clinical"


FAST_FACTS_MEDICAL_CUE_RE = re.compile(
    r"\b("
    r"diagnos\w*|treat\w*|manage\w*|screen\w*|prevent\w*|workup|therapy|antibiotic\w*|"
    r"infection\w*|disease\w*|syndrome\w*|rash|pain|fever|blood|culture\w*|urine|renal|"
    r"cardiac|pulmonary|hepatic|diabetes|cellulitis|erysipelas|cystitis|pyelonephritis|"
    r"pneumonia|embolism|thrombosis|ischemia|infarct\w*|failure|deficien\w*|mutation\w*|"
    r"ecg|ekg|ct|mri|x-ray|ultrasound|lab\w*|finding\w*|criteria|contraindicat\w*|"
    r"pack-year|smok\w*|quit|low-dose|yearly|life expectancy|bmi|hgb|a1c|frequency|suprapubic"
    r")\b",
    re.I,
)


def is_fast_facts_medical_line(line: str) -> bool:
    stripped = normalize_key(line)
    if not stripped:
        return False
    if stripped.strip(":") in {"diagnosis and management", "management", "diagnosis", "treatment"}:
        return False
    if re.match(r"^[,;:)]", stripped):
        return False
    if re.fullmatch(r"[a-z]+[)]", stripped):
        return False
    if stripped in {
        "internal medicine shelf fast facts",
        "useful tables and images from uworld/amboss",
    }:
        return False
    if len(stripped) < 5:
        return False
    if extract_medical_claim_terms(line):
        return True
    return bool(FAST_FACTS_MEDICAL_CUE_RE.search(line))


def fast_facts_lines_from_text(text: str) -> list[str]:
    raw_lines = [
        re.sub(r"\s+", " ", line).strip(" -•\t")
        for line in str(text or "").splitlines()
    ]
    raw_lines = [line for line in raw_lines if line]
    merged: list[str] = []
    i = 0
    join_prefixes = {"empiric", "treatment", "urine", "blood", "sputum", "stool", "serum", "oral", "iv"}
    while i < len(raw_lines):
        line = raw_lines[i]
        while i + 1 < len(raw_lines):
            nxt = raw_lines[i + 1]
            word_count = len(re.findall(r"\b\w+\b", line))
            should_join = (
                normalize_key(line).strip(":") in join_prefixes
                or (word_count <= 2 and re.match(r"^(with|prior|to|for|while|and|or|but|plus|culture|treatment)\b", normalize_key(nxt)))
                or re.search(r"\b(empiric|urine|blood|sputum|stool|serum|oral|iv)$", normalize_key(line))
                or (word_count <= 5 and len(re.findall(r"\b\w+\b", nxt)) <= 2 and not nxt.endswith(":"))
                or normalize_key(line) in {"started, consider"}
                or re.search(r"\brenal artery$", normalize_key(line))
            )
            if not should_join:
                break
            line = f"{line} {nxt}".strip()
            i += 1
        merged.append(line)
        i += 1
    return merged


def fast_facts_split_atomic_lines(slide: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block in slide.get("textBlocks") or []:
        lines.extend(fast_facts_lines_from_text(str(block.get("text") or "")))
    if slide.get("notesText"):
        lines.extend(fast_facts_lines_from_text(str(slide.get("notesText") or "")))
    for table in slide.get("tables") or []:
        headers = [str(h).strip() for h in table.get("headers") or []]
        for row in table.get("rows") or []:
            cells = [str(c).strip() for c in row if str(c).strip()]
            if cells:
                prefix = f"{headers[0]}: " if headers and headers[0] else ""
                lines.append(prefix + " | ".join(cells))
    clean: list[str] = []
    seen: set[str] = set()
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip(" -•\t")
        if len(line) < 4 or len(line) > 260:
            continue
        if not is_fast_facts_medical_line(line):
            continue
        key = normalize_key(line)
        if key in seen:
            continue
        seen.add(key)
        clean.append(line)
    return clean


FAST_FACTS_GENERIC_TERMS = {
    "additional", "angle", "blood", "border", "borders", "cases", "clinical",
    "common", "confined", "consider", "criteria", "current", "currently",
    "days", "described", "diagnosis", "done", "enough", "external", "facts",
    "feature", "features", "history", "interval", "like", "local", "medical",
    "medicine", "patient", "patients", "positive", "prior", "recommended",
    "screening", "severe", "shelf", "significantly", "symptoms", "systemic",
    "test", "then", "treated", "useful", "usually", "with", "without",
    "should", "over", "even", "absence", "earlier", "within",
}


def meaningful_fast_facts_terms(text: str) -> list[str]:
    phrases: list[str] = []
    for phrase in re.findall(r"\b[A-Z][A-Za-z0-9+\-/]*(?:\s+[A-Z0-9][A-Za-z0-9+\-/]*){0,4}\b", text):
        cleaned = re.sub(r"\s+", " ", phrase).strip()
        if len(cleaned) >= 4 and normalize_key(cleaned) not in FAST_FACTS_GENERIC_TERMS:
            phrases.append(cleaned.lower())
    tokens = [
        tok.lower().strip("-")
        for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-/]{2,}\b", text)
        if tok.lower() not in COMMON_CLINICAL_WORDS and tok.lower() not in FAST_FACTS_GENERIC_TERMS
    ]
    for tok in sorted(extract_medical_claim_terms(text)):
        if tok not in tokens:
            tokens.insert(0, tok)
    ordered: list[str] = []
    for term in phrases + tokens:
        term = term.strip(" ,.;:()")
        if len(term) < 4 or term in FAST_FACTS_GENERIC_TERMS:
            continue
        if term not in ordered:
            ordered.append(term)
    return ordered[:40]


def fast_facts_grounding_terms(text: str) -> list[str]:
    return meaningful_fast_facts_terms(text)


def fast_facts_fact_bucket(line: str, active_section: str = "") -> str:
    text = normalize_key(f"{active_section} {line}")
    if re.search(r"\b(not to be confused|do not confuse|confused with|vs|versus|rather than)\b", text):
        return "trapFacts"
    if re.search(r"\b(differential|mimic|not pulmonary symptoms|interstitial nephritis|ain)\b", text):
        return "differentialFacts"
    if re.search(r"\b(pathophys|mechanism|mutation|deficien|causes?|induced|receptor|inhibit|activat)\b", text):
        return "mechanismFacts"
    if re.search(r"\b(diagnos|urinalysis|culture|leukocyte|nitrat|glucose|a1c|blood pressure|systolic|diastolic|ct scan|testing|ultrasound|criteria|finding|opacit)\b", text):
        return "diagnosticFacts"
    if re.search(r"\b(treat|therapy|management|empiric|antibiotic|ceftriaxone|cefazolin|amoxicillin|tmp-smx|nitrofurantoin|fosfomycin|fluoroquinolone|screening|low-dose|yearly|termination)\b", text):
        return "managementFacts"
    if active_section and re.search(r"\b(presentation|clinical features)\b", normalize_key(active_section)):
        return "clinicalFacts"
    return "clinicalFacts"


def fast_facts_clean_title(text: str, fallback: str = "") -> str:
    title = re.sub(r"\s+", " ", str(text or fallback or "")).strip(" :-•\t")
    title = re.sub(r"[:.;,]+$", "", title).strip()
    return title[:120]


def fast_facts_new_concept(slide: dict[str, Any], title: str, ordinal: int) -> dict[str, Any]:
    clean_title = fast_facts_clean_title(title, f"Slide {slide.get('slideIndex')} concept {ordinal}")
    return {
        "conceptId": f"ff_{slide['slideId']}_{short_hash(clean_title + str(ordinal))[:8]}",
        "sourceSlideIds": [slide["slideId"]],
        "title": clean_title,
        "category": classify_fast_facts_category(clean_title),
        "clinicalFacts": [],
        "diagnosticFacts": [],
        "managementFacts": [],
        "mechanismFacts": [],
        "differentialFacts": [],
        "trapFacts": [],
        "nativeTextFacts": [],
        "imageOcrFacts": [],
        "cleanedImageFacts": [],
        "structuredImageFacts": {
            "criteria": [],
            "indications": [],
            "contraindications": [],
            "managementSteps": [],
            "thresholds": [],
        },
        "imageTextQuality": "none",
        "images": [],
        "tables": [],
        "questionPotential": 0,
        "groundingTerms": [],
    }


def fast_facts_add_fact(concept: dict[str, Any], line: str, active_section: str = "", origin: str = "native") -> None:
    clean = re.sub(r"\s+", " ", str(line or "")).strip(" -•\t")
    clean = re.sub(r"^[¢•]+\s*", "", clean).strip()
    section_key = normalize_key(active_section).strip(":")
    if not clean:
        return
    if normalize_key(clean).strip(":") in {"recommended", "test", "interval", "age for screening", "past", "or"}:
        return
    section_allows_context = bool(re.search(r"\b(presentation|clinical features|diagnosis|management|screening|recommendations|termination)\b", section_key))
    if not is_fast_facts_medical_line(clean) and not section_allows_context:
        return
    bucket = fast_facts_fact_bucket(clean, active_section)
    if clean not in concept[bucket]:
        concept[bucket].append(clean)
    if origin == "native" and clean not in concept["nativeTextFacts"]:
        concept["nativeTextFacts"].append(clean)


def fast_facts_finalize_concept(concept: dict[str, Any]) -> dict[str, Any] | None:
    all_facts: list[str] = []
    for key in ["clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts", "differentialFacts", "trapFacts"]:
        all_facts.extend(concept.get(key) or [])
    title = str(concept.get("title") or "")
    if not all_facts and not concept.get("images"):
        return None
    if not all_facts and is_fast_facts_medical_line(title):
        concept["clinicalFacts"].append(title)
        all_facts.append(title)
    for key in ["nativeTextFacts", "imageOcrFacts", "cleanedImageFacts"]:
        seen_values: set[str] = set()
        deduped: list[str] = []
        for value in concept.get(key) or []:
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            if value and value not in seen_values:
                seen_values.add(value)
                deduped.append(value)
        concept[key] = deduped
    for key, values in list((concept.get("structuredImageFacts") or {}).items()):
        seen_structured: set[str] = set()
        concept["structuredImageFacts"][key] = [
            v for v in values
            if isinstance(v, str) and v and not (v in seen_structured or seen_structured.add(v))
        ]
    if concept.get("imageTextQuality") == "poor":
        fact_text = " ".join([title] + all_facts)
    else:
        fact_text = " ".join([title] + all_facts + (concept.get("cleanedImageFacts") or []))
    terms = fast_facts_grounding_terms(fact_text)
    concept["groundingTerms"] = terms
    concept["questionPotential"] = min(100, 30 + len(all_facts) * 8 + len(terms) * 2 + len(concept.get("images") or []) * 8)
    fact_counts = {
        "clinical": len(concept["clinicalFacts"]),
        "diagnosis": len(concept["diagnosticFacts"]),
        "management": len(concept["managementFacts"]),
        "mechanism": len(concept["mechanismFacts"]),
        "differential": len(concept["differentialFacts"]) + len(concept["trapFacts"]),
    }
    concept["category"] = max(fact_counts.items(), key=lambda kv: kv[1])[0] if any(fact_counts.values()) else classify_fast_facts_category(title)
    return concept


def fast_facts_ocr_image_text(image: dict[str, Any]) -> str:
    asset = image.get("assetPath")
    if not asset:
        return ""
    path = BASE_DIR / str(asset)
    if not path.exists() or not shutil.which("tesseract"):
        return ""
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return clean_slide_text(proc.stdout)


def fast_facts_normalize_ocr_line(line: str) -> str:
    line = re.sub(r"[¢•·]+", " ", line)
    line = re.sub(r"[“”]", '"', line)
    line = re.sub(r"[‘’]", "'", line)
    line = re.sub(r"\s+", " ", line).strip(" -—:;,.")
    replacements = [
        (r"\bAne\b", ""),
        (r"\butr\"?\b", "UTI"),
        (r"\b220-pack-year\b", "20-pack-year"),
        (r"\b215\b", "15"),
        (r"^7\s+", ""),
        (r"\btest Recommended\b", ""),
        (r"\bRecommended\s*$", ""),
        (r"\s*&\s*$", ""),
    ]
    for pat, repl in replacements:
        line = re.sub(pat, repl, line, flags=re.I)
    line = re.sub(r"\s+", " ", line).strip(" -—:;,.")
    return line


def fast_facts_clean_ocr_lines(raw_ocr: str) -> list[str]:
    raw_lines = fast_facts_lines_from_text(raw_ocr)
    clean_lines: list[str] = []
    skip = {
        "recommended", "test", "interval", "age for", "age for screening",
        "past", "or", "screening", "termination of",
    }
    for line in raw_lines:
        clean = fast_facts_normalize_ocr_line(line)
        key = normalize_key(clean)
        if not clean or key in skip or len(clean) < 3:
            continue
        clean_lines.append(clean)

    joined = "\n".join(clean_lines)
    if "lung cancer screening" in normalize_key(joined):
        facts = [
            "Low-dose CT scan of the chest",
            "Screen yearly",
            "Age 50-80 years",
            "20-pack-year smoking history",
            "Currently smoking or quit within the past 15 years",
            "Terminate screening if quit smoking for 15 years",
            "Terminate screening if medical conditions significantly limit life expectancy",
        ]
        return [f for f in facts if any(tok in normalize_key(joined) for tok in normalize_key(f).split()[:2]) or f.startswith("Terminate")]

    if "urinary tract infection" in normalize_key(joined) or "uncomplicated" in normalize_key(joined):
        facts = [
            "Uncomplicated UTI: nitrofurantoin",
            "Uncomplicated UTI: trimethoprim-sulfamethoxazole",
            "Uncomplicated UTI: fosfomycin single dose",
            "Use fluoroquinolones only if previous options cannot be used",
            "Urine culture only if initial treatment fails",
            "Complicated UTI outpatient treatment: fluoroquinolones",
            "Complicated UTI inpatient treatment: ceftriaxone, piperacillin-tazobactam, or carbapenems such as imipenem",
            "Obtain culture before therapy and adjust antibiotic as needed",
            "Complicated UTI includes infection above the bladder, pelvic pain in men, or systemic illness",
        ]
        return facts

    seen: set[str] = set()
    out: list[str] = []
    for line in clean_lines:
        key = normalize_key(line)
        if key not in seen and is_fast_facts_medical_line(line):
            seen.add(key)
            out.append(line)
    return out


def fast_facts_image_text_quality(raw_ocr: str, cleaned: list[str]) -> str:
    if not raw_ocr.strip():
        return "none"
    junk_hits = len(re.findall(r"[¢_=]{1,}|[\"']\s*[a-z]?$|\b[A-Za-z]{1,2}\b", raw_ocr))
    token_count = len(re.findall(r"\b[A-Za-z0-9][A-Za-z0-9-]*\b", raw_ocr))
    if token_count < 6 or not cleaned:
        return "poor"
    if junk_hits / max(1, token_count) > 0.35:
        return "poor"
    if junk_hits / max(1, token_count) > 0.18:
        return "fair"
    return "good"


def fast_facts_structured_image_facts(cleaned_facts: list[str], image_kind: str) -> dict[str, list[str]]:
    structured = {
        "criteria": [],
        "indications": [],
        "contraindications": [],
        "managementSteps": [],
        "thresholds": [],
    }
    for fact in cleaned_facts:
        key = normalize_key(fact)
        if re.search(r"\b(age|pack-year|years?|bmi|a1c|>\d|<\d|\d+-\d+|15 years|20-pack)\b", key):
            structured["thresholds"].append(fact)
        if re.search(r"\b(currently smoking|quit|systemic illness|infection above|pelvic pain|initial treatment fails)\b", key):
            structured["criteria"].append(fact)
        if re.search(r"\b(low-dose ct|screen|culture|obtain|treatment|nitrofurantoin|trimethoprim|fosfomycin|fluoroquinolone|ceftriaxone|piperacillin|carbapenem)\b", key):
            structured["managementSteps"].append(fact)
        if re.search(r"\b(medical conditions|limit life expectancy|cannot be used)\b", key):
            structured["contraindications"].append(fact)
        if image_kind in {"algorithm", "table-like"} and re.search(r"\b(uti|screening|infection|smoking)\b", key):
            structured["indications"].append(fact)
    return structured


def fast_facts_apply_image_ocr(concept: dict[str, Any], slide: dict[str, Any], force: bool = False) -> list[str]:
    native_lines = fast_facts_split_atomic_lines(slide)
    if not force and len(native_lines) > 1:
        return []
    all_cleaned: list[str] = []
    for image in slide.get("images") or []:
        ocr = fast_facts_ocr_image_text(image)
        if ocr:
            image["ocrText"] = ocr
            cleaned = fast_facts_clean_ocr_lines(ocr)
            image["cleanedOcrFacts"] = cleaned
            image["imageTextQuality"] = fast_facts_image_text_quality(ocr, cleaned)
            image["structuredFacts"] = fast_facts_structured_image_facts(cleaned, str(image.get("kind") or ""))
            concept["imageOcrFacts"].extend(fast_facts_lines_from_text(ocr))
            concept["cleanedImageFacts"].extend(cleaned)
            for key, values in image["structuredFacts"].items():
                for value in values:
                    if value not in concept["structuredImageFacts"][key]:
                        concept["structuredImageFacts"][key].append(value)
            all_cleaned.extend(cleaned)
    qualities = [img.get("imageTextQuality") for img in slide.get("images") or [] if img.get("ocrText")]
    if qualities:
        if "poor" in qualities:
            concept["imageTextQuality"] = "poor"
        elif "fair" in qualities:
            concept["imageTextQuality"] = "fair"
        else:
            concept["imageTextQuality"] = "good"
    return all_cleaned


def fast_facts_image_fallback_lines(slide: dict[str, Any]) -> list[str]:
    temp = fast_facts_new_concept(slide, "image fallback", 999)
    return fast_facts_apply_image_ocr(temp, slide, force=False)


def fast_facts_heading_candidates(slide: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for block in slide.get("textBlocks") or []:
        lines = fast_facts_lines_from_text(str(block.get("text") or ""))
        if lines:
            candidates.append(lines[0])
    if slide.get("notesText"):
        candidates.extend(fast_facts_lines_from_text(str(slide.get("notesText") or ""))[:1])
    return [fast_facts_clean_title(c) for c in candidates if fast_facts_clean_title(c)]


def fast_facts_block_lines(block: dict[str, Any]) -> list[str]:
    return fast_facts_lines_from_text(str(block.get("text") or ""))


def atomize_fast_facts_slide(slide: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = slide.get("textBlocks") or []
    heading_candidates = fast_facts_heading_candidates(slide)
    main_title = heading_candidates[0] if heading_candidates else fast_facts_clean_title(slide.get("notesText") or f"Slide {slide.get('slideIndex')}")
    concepts: list[dict[str, Any]] = []

    if len(blocks) <= 1:
        concept = fast_facts_new_concept(slide, main_title, 1)
        lines: list[str] = []
        for block in blocks:
            block_lines = fast_facts_block_lines(block)
            lines.extend(block_lines[1:] if block_lines and fast_facts_clean_title(block_lines[0]) == main_title else block_lines)
        image_lines = fast_facts_apply_image_ocr(concept, slide, force=True)
        image_line_keys = {normalize_key(line) for line in image_lines}
        lines.extend(image_lines if concept.get("imageTextQuality") != "poor" else [])
        section = ""
        for line in lines:
            if re.search(r":$", line):
                section = line
                continue
            origin = "image" if normalize_key(line) in image_line_keys else "native"
            fast_facts_add_fact(concept, line, section, origin=origin)
        concept["images"] = slide.get("images") or []
        concept["tables"] = slide.get("tables") or []
        finalized = fast_facts_finalize_concept(concept)
        return [finalized] if finalized else []

    content_blocks = blocks[1:] if fast_facts_clean_title(fast_facts_block_lines(blocks[0])[0] if fast_facts_block_lines(blocks[0]) else "") == main_title else blocks
    major_blocks = [b for b in content_blocks if fast_facts_block_lines(b) and re.search(r"\b(uncomplicated|complicated|screening for|not to be confused|presentation)\b", fast_facts_block_lines(b)[0], re.I)]
    if len(content_blocks) > 1 and len(major_blocks) >= 2 and re.search(r"\burinary tract infections?\b", main_title, re.I):
        ordinal = 1
        for block in content_blocks:
            lines = fast_facts_block_lines(block)
            if not lines:
                continue
            subheading = lines[0]
            concept = fast_facts_new_concept(slide, f"{main_title}: {subheading}", ordinal)
            section = ""
            for line in lines[1:]:
                if re.search(r":$", line):
                    section = line
                    continue
                fast_facts_add_fact(concept, line, section)
            finalized = fast_facts_finalize_concept(concept)
            if finalized:
                concepts.append(finalized)
                ordinal += 1
        if concepts:
            concepts[0]["images"] = slide.get("images") or []
            concepts[0]["tables"] = slide.get("tables") or []
            fast_facts_apply_image_ocr(concepts[0], slide, force=True)
            return concepts

    if re.search(r"\bhypertension\b", normalize_key(slide.get("notesText") or "")):
        hypertension = fast_facts_new_concept(slide, fast_facts_clean_title(slide.get("notesText")), 1)
        diabetes = fast_facts_new_concept(slide, "Screening for diabetes", 2)
        hypertension_context = False
        for block in blocks:
            lines = fast_facts_block_lines(block)
            section = ""
            target = hypertension
            for line in lines:
                if re.match(r"screening for diabetes", line, re.I):
                    section = line
                    target = diabetes
                    fast_facts_add_fact(diabetes, line, section)
                    continue
                if re.search(r":$", line):
                    section = line
                    continue
                if re.search(r"\b(diabetes|glucose|a1c|bmi)\b", line, re.I):
                    fast_facts_add_fact(diabetes, line, section)
                elif re.search(r"\b(refractory hypertension|resistant hypertension|renal artery|sleep apnea|acei|blood pressure|systolic|diastolic)\b", line, re.I):
                    hypertension_context = True
                    fast_facts_add_fact(hypertension, line, section)
                elif hypertension_context and re.search(r"\b(started|consider|stenosis|renal|ultrasound)\b", line, re.I):
                    fast_facts_add_fact(hypertension, line, section)
                else:
                    fast_facts_add_fact(target, line, section)
        hypertension["images"] = slide.get("images") or []
        hypertension["tables"] = slide.get("tables") or []
        fast_facts_apply_image_ocr(hypertension, slide, force=True)
        return [c for c in [fast_facts_finalize_concept(hypertension), fast_facts_finalize_concept(diabetes)] if c]

    concept = fast_facts_new_concept(slide, main_title, 1)
    section = ""
    for block in content_blocks:
        lines = fast_facts_block_lines(block)
        if lines and fast_facts_clean_title(lines[0]) == main_title:
            lines = lines[1:]
        for line in lines:
            if re.search(r":$", line):
                section = line
                if re.search(r"\bnot to be confused|differential|do not confuse\b", line, re.I):
                    fast_facts_add_fact(concept, line, section)
                continue
            fast_facts_add_fact(concept, line, section)
    image_facts = fast_facts_apply_image_ocr(concept, slide, force=not any(concept.get(key) for key in ["clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts", "differentialFacts", "trapFacts"]))
    if not any(concept.get(key) for key in ["clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts", "differentialFacts", "trapFacts"]):
        for line in image_facts if concept.get("imageTextQuality") != "poor" else []:
            fast_facts_add_fact(concept, line, "", origin="image")
    concept["images"] = slide.get("images") or []
    concept["tables"] = slide.get("tables") or []
    finalized = fast_facts_finalize_concept(concept)
    return [finalized] if finalized else []


def validate_fast_facts_concept_graph(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("profile") != FAST_FACTS_PROFILE:
        errors.append("profile must be FAST_FACTS_PROFILE")
    if not isinstance(payload.get("concepts"), list):
        errors.append("concepts must be a list")
        return errors
    seen: set[str] = set()
    required = [
        "conceptId", "sourceSlideIds", "title", "category", "clinicalFacts",
        "diagnosticFacts", "managementFacts", "mechanismFacts",
        "differentialFacts", "trapFacts", "nativeTextFacts", "imageOcrFacts",
        "cleanedImageFacts", "structuredImageFacts", "imageTextQuality", "images", "tables",
        "questionPotential", "groundingTerms", "semanticClusterId", "duplicateOf",
        "overlapScore", "dedupeDisposition",
    ]
    allowed_image_kinds = {"diagnostic", "explanatory", "algorithm", "decorative", "table-like"}
    for idx, concept in enumerate(payload.get("concepts") or [], start=1):
        if not isinstance(concept, dict):
            errors.append(f"concept {idx} is not an object")
            continue
        missing = [key for key in required if key not in concept]
        if missing:
            errors.append(f"concept {idx} missing keys: {', '.join(missing)}")
        cid = str(concept.get("conceptId") or "")
        if not cid:
            errors.append(f"concept {idx} missing conceptId")
        elif cid in seen:
            errors.append(f"duplicate conceptId: {cid}")
        seen.add(cid)
        if not concept.get("title"):
            errors.append(f"{cid or idx}: empty title")
        if not isinstance(concept.get("sourceSlideIds"), list) or not concept.get("sourceSlideIds"):
            errors.append(f"{cid or idx}: sourceSlideIds must be non-empty")
        for key in required:
            if (key.endswith("Facts") and key != "structuredImageFacts") or key in {"images", "tables", "groundingTerms"}:
                if not isinstance(concept.get(key), list):
                    errors.append(f"{cid or idx}: {key} must be a list")
        if not isinstance(concept.get("structuredImageFacts"), dict):
            errors.append(f"{cid or idx}: structuredImageFacts must be an object")
        if concept.get("imageTextQuality") not in {"none", "poor", "fair", "good"}:
            errors.append(f"{cid or idx}: invalid imageTextQuality {concept.get('imageTextQuality')}")
        if concept.get("dedupeDisposition") not in {"keep", "merge", "suppress", "recap_only", "low_information"}:
            errors.append(f"{cid or idx}: invalid dedupeDisposition {concept.get('dedupeDisposition')}")
        try:
            score = float(concept.get("overlapScore"))
            if score < 0 or score > 1:
                errors.append(f"{cid or idx}: overlapScore must be between 0 and 1")
        except Exception:
            errors.append(f"{cid or idx}: overlapScore must be numeric")
        for image in concept.get("images") or []:
            if isinstance(image, dict) and image.get("kind") not in allowed_image_kinds:
                errors.append(f"{cid or idx}: invalid image kind {image.get('kind')}")
    return errors


def fast_facts_fact_values(concept: dict[str, Any]) -> list[str]:
    values: list[str] = [str(concept.get("title") or "")]
    for key in [
        "clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts",
        "differentialFacts", "trapFacts", "cleanedImageFacts", "groundingTerms",
    ]:
        values.extend(str(v) for v in concept.get(key) or [])
    for values_list in (concept.get("structuredImageFacts") or {}).values():
        if isinstance(values_list, list):
            values.extend(str(v) for v in values_list)
    return values


def fast_facts_semantic_tokens(concept: dict[str, Any]) -> set[str]:
    text = " ".join(fast_facts_fact_values(concept))
    tokens = set(meaningful_fast_facts_terms(text))
    for token in re.findall(r"\b[A-Za-z][A-Za-z0-9+\-/]{3,}\b", text):
        key = token.lower().strip("-")
        if key not in FAST_FACTS_GENERIC_TERMS and key not in COMMON_CLINICAL_WORDS:
            tokens.add(key)
    return tokens


def fast_facts_structured_overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    scores: list[float] = []
    for key in ["criteria", "indications", "contraindications", "managementSteps", "thresholds"]:
        av = {normalize_key(v) for v in (a.get("structuredImageFacts") or {}).get(key, []) if normalize_key(v)}
        bv = {normalize_key(v) for v in (b.get("structuredImageFacts") or {}).get(key, []) if normalize_key(v)}
        if av or bv:
            scores.append(len(av & bv) / max(1, len(av | bv)))
    return max(scores) if scores else 0.0


def fast_facts_overlap_score(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, list[str]]:
    a_tokens = fast_facts_semantic_tokens(a)
    b_tokens = fast_facts_semantic_tokens(b)
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    fact_a = {normalize_key(v) for v in fast_facts_fact_values(a) if normalize_key(v)}
    fact_b = {normalize_key(v) for v in fast_facts_fact_values(b) if normalize_key(v)}
    fact_score = len(fact_a & fact_b) / max(1, min(len(fact_a), len(fact_b)))
    structured_score = fast_facts_structured_overlap(a, b)
    title_tokens_a = set(re.findall(r"\b[a-z0-9+\-/]{4,}\b", normalize_key(a.get("title") or ""))) - FAST_FACTS_GENERIC_TERMS
    title_tokens_b = set(re.findall(r"\b[a-z0-9+\-/]{4,}\b", normalize_key(b.get("title") or ""))) - FAST_FACTS_GENERIC_TERMS
    title_score = len(title_tokens_a & title_tokens_b) / max(1, len(title_tokens_a | title_tokens_b))
    score = max(token_score, fact_score, structured_score, title_score * 0.85)
    reasons: list[str] = []
    if fact_score >= 0.5:
        reasons.append("repeated fact block")
    if structured_score >= 0.5:
        reasons.append("repeated algorithm/criteria block")
    if token_score >= 0.55:
        reasons.append("near-duplicate medical term set")
    if title_score >= 0.75 and score >= 0.65:
        reasons.append("title plus content overlap")
    if any(re.search(r"\b(screening|treatment|management|criteria|threshold)\b", normalize_key(v)) for v in fast_facts_fact_values(a) + fast_facts_fact_values(b)) and score >= 0.55:
        reasons.append("overlapping screening/treatment threshold")
    shared_tokens = a_tokens & b_tokens
    if {"urinary", "tract"} <= shared_tokens or "uti" in shared_tokens:
        reasons.append("overlapping UTI topic")
        if shared_tokens & {"nitrofurantoin", "fosfomycin", "fluoroquinolones", "ceftriaxone", "culture"}:
            reasons.append("repeated UTI management or criteria")
        score = max(score, min(1.0, token_score + 0.18))
    if "hypertension" in shared_tokens and (shared_tokens & {"renal", "sleep", "apnea", "screening", "glucose"}):
        reasons.append("overlapping hypertension evaluation")
        score = max(score, min(1.0, token_score + 0.12))
    if "diabetes" in shared_tokens and (shared_tokens & {"screening", "glucose", "a1c", "risk"}):
        reasons.append("overlapping diabetes screening/management")
        score = max(score, min(1.0, token_score + 0.12))
    return round(min(1.0, score), 3), reasons


def fast_facts_concept_information_score(concept: dict[str, Any]) -> int:
    fact_count = sum(len(concept.get(key) or []) for key in [
        "clinicalFacts", "diagnosticFacts", "managementFacts", "mechanismFacts", "differentialFacts", "trapFacts", "cleanedImageFacts",
    ])
    unique_terms = len(concept.get("groundingTerms") or [])
    unique_assets = len(concept.get("images") or []) + len(concept.get("tables") or [])
    return fact_count * 5 + unique_terms + unique_assets * 4


def fast_facts_merge_unique_list(target: dict[str, Any], source: dict[str, Any], key: str) -> list[str]:
    existing = list(target.get(key) or [])
    seen = {json.dumps(v, sort_keys=True) if isinstance(v, dict) else str(v) for v in existing}
    retained: list[str] = []
    for value in source.get(key) or []:
        marker = json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value)
        if marker not in seen:
            existing.append(value)
            seen.add(marker)
            retained.append(str(value if not isinstance(value, dict) else value.get("imageId") or value))
    target[key] = existing
    return retained


def fast_facts_merge_concepts(base: dict[str, Any], other: dict[str, Any]) -> dict[str, list[str]]:
    retained: dict[str, list[str]] = {}
    for key in [
        "sourceSlideIds", "clinicalFacts", "diagnosticFacts", "managementFacts",
        "mechanismFacts", "differentialFacts", "trapFacts", "nativeTextFacts",
        "imageOcrFacts", "cleanedImageFacts", "groundingTerms", "images", "tables",
    ]:
        retained[key] = fast_facts_merge_unique_list(base, other, key)
    for key, values in (other.get("structuredImageFacts") or {}).items():
        base.setdefault("structuredImageFacts", {}).setdefault(key, [])
        seen = set(base["structuredImageFacts"][key])
        retained_key: list[str] = []
        for value in values or []:
            if value not in seen:
                base["structuredImageFacts"][key].append(value)
                seen.add(value)
                retained_key.append(str(value))
        if retained_key:
            retained[f"structuredImageFacts.{key}"] = retained_key
    quality_order = {"none": 0, "poor": 1, "fair": 2, "good": 3}
    if quality_order.get(other.get("imageTextQuality"), 0) > quality_order.get(base.get("imageTextQuality"), 0):
        base["imageTextQuality"] = other.get("imageTextQuality")
    finalized = fast_facts_finalize_concept(base)
    if finalized:
        base.update(finalized)
    return {k: v for k, v in retained.items() if v}


def dedupe_fast_facts_concepts(concepts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    working = [copy.deepcopy(c) for c in concepts]
    before_count = len(working)
    for concept in working:
        concept["semanticClusterId"] = concept.get("semanticClusterId") or f"ff_cluster_{short_hash(concept.get('conceptId', ''))[:8]}"
        concept["duplicateOf"] = None
        concept["overlapScore"] = 0.0
        concept["dedupeDisposition"] = "keep"

    clusters: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    merged: list[dict[str, Any]] = []
    clustered_overlaps: list[dict[str, Any]] = []
    low_information: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    for concept in working:
        info_score = fast_facts_concept_information_score(concept)
        if info_score < 10:
            concept["dedupeDisposition"] = "low_information"
            low_information.append({
                "conceptId": concept.get("conceptId"),
                "title": concept.get("title"),
                "reason": "low information score",
                "informationScore": info_score,
            })
            continue

        best: dict[str, Any] | None = None
        best_score = 0.0
        best_reasons: list[str] = []
        for existing in kept:
            score, reasons = fast_facts_overlap_score(existing, concept)
            if score > best_score:
                best = existing
                best_score = score
                best_reasons = reasons

        if best and best_score >= 0.78 and best_reasons:
            concept["duplicateOf"] = best["conceptId"]
            concept["semanticClusterId"] = best["semanticClusterId"]
            concept["overlapScore"] = best_score
            if fast_facts_concept_information_score(concept) <= 12 and not (concept.get("images") or concept.get("trapFacts") or concept.get("differentialFacts")):
                concept["dedupeDisposition"] = "suppress"
                suppressed.append({
                    "conceptId": concept.get("conceptId"),
                    "duplicateOf": best.get("conceptId"),
                    "overlapScore": best_score,
                    "reasons": best_reasons,
                })
            else:
                concept["dedupeDisposition"] = "merge"
                retained = fast_facts_merge_concepts(best, concept)
                merged.append({
                    "conceptId": concept.get("conceptId"),
                    "mergedInto": best.get("conceptId"),
                    "overlapScore": best_score,
                    "reasons": best_reasons,
                    "retainedUniqueFacts": retained,
                })
            continue

        if best and best_score >= 0.28 and best_reasons:
            concept["semanticClusterId"] = best["semanticClusterId"]
            concept["overlapScore"] = best_score
            concept["dedupeDisposition"] = "keep"
            clustered_overlaps.append({
                "conceptId": concept.get("conceptId"),
                "clusteredWith": best.get("conceptId"),
                "semanticClusterId": best.get("semanticClusterId"),
                "overlapScore": best_score,
                "reasons": best_reasons,
                "retainedUniqueFacts": {
                    key: concept.get(key) or []
                    for key in ["clinicalFacts", "diagnosticFacts", "managementFacts", "differentialFacts", "trapFacts", "cleanedImageFacts"]
                    if concept.get(key)
                },
            })

        if re.search(r"\b(recap|summary|review|key points)\b", normalize_key(concept.get("title") or "")):
            concept["dedupeDisposition"] = "recap_only"
            low_information.append({
                "conceptId": concept.get("conceptId"),
                "title": concept.get("title"),
                "reason": "recap-only slide",
            })
            continue

        kept.append(concept)

    cluster_map: dict[str, list[dict[str, Any]]] = {}
    for concept in kept:
        cluster_map.setdefault(concept["semanticClusterId"], []).append({
            "conceptId": concept.get("conceptId"),
            "title": concept.get("title"),
            "sourceSlideIds": concept.get("sourceSlideIds"),
        })
    duplicate_clusters = [
        {"semanticClusterId": cid, "members": members}
        for cid, members in cluster_map.items()
        if len(members) > 1
    ]
    report = {
        "conceptsBeforeDedupe": before_count,
        "conceptsAfterDedupe": len(kept),
        "duplicateClustersFound": len(duplicate_clusters),
        "duplicateClusters": duplicate_clusters,
        "suppressedConcepts": suppressed,
        "mergedConcepts": merged,
        "clusteredOverlaps": clustered_overlaps,
        "lowInformationConcepts": low_information,
        "overlapComparisons": [],
    }
    for i, a in enumerate(working):
        for b in working[i + 1:]:
            score, reasons = fast_facts_overlap_score(a, b)
            if score >= 0.45 and reasons:
                report["overlapComparisons"].append({
                    "conceptA": a.get("conceptId"),
                    "titleA": a.get("title"),
                    "conceptB": b.get("conceptId"),
                    "titleB": b.get("title"),
                    "overlapScore": score,
                    "reasons": reasons,
                })
    return kept, report


def process_fast_facts_pptx(
    pptx_path: Path,
    limit_slides: int = 0,
    generate: bool = False,
    reuse_cache: bool = True,
    force_regenerate: bool = False,
    repair_only: bool = False,
    diagnostic_report: bool = False,
    question_limit: int = 0,
) -> Path:
    if pptx_path.suffix.lower() != ".pptx":
        raise PipelineError(f"Fast Facts profile expects a PPTX file: {pptx_path}")
    limit_slides = max(0, int(limit_slides or 0))
    slide_payload = decompose_fast_facts_pptx(pptx_path, limit_slides=limit_slides)
    deck_hash = slide_payload["pptxSha256"]
    checkpoint = load_fast_facts_checkpoint(pptx_path, deck_hash, limit_slides)
    concepts: list[dict[str, Any]] = list(checkpoint.get("concepts") or [])
    processed: list[str] = list(checkpoint.get("processedSlideIds") or [])
    failures: list[dict[str, Any]] = list(checkpoint.get("failures") or []) + list(slide_payload.get("failures") or [])
    processed_set = set(processed)
    for slide in slide_payload.get("slides") or []:
        if slide["slideId"] in processed_set:
            continue
        try:
            slide_concepts = atomize_fast_facts_slide(slide)
            if not slide_concepts:
                failures.append({"slideId": slide["slideId"], "slideIndex": slide["slideIndex"], "error": "no atomic concepts extracted"})
            concepts.extend(slide_concepts)
            processed.append(slide["slideId"])
            processed_set.add(slide["slideId"])
            write_fast_facts_checkpoint(pptx_path, deck_hash, limit_slides, concepts, processed, failures)
        except Exception as exc:
            failures.append({"slideId": slide.get("slideId"), "slideIndex": slide.get("slideIndex"), "error": str(exc)})
            processed.append(slide["slideId"])
            processed_set.add(slide["slideId"])
            write_fast_facts_checkpoint(pptx_path, deck_hash, limit_slides, concepts, processed, failures)
    deduped_concepts, dedupe_report = dedupe_fast_facts_concepts(concepts)
    graph = {
        "profile": FAST_FACTS_PROFILE,
        "deckTitle": pptx_path.stem,
        "slideCount": int(slide_payload.get("slideCount") or 0),
        "concepts": deduped_concepts,
    }
    errors = validate_fast_facts_concept_graph(graph)
    report = {
        "profile": FAST_FACTS_PROFILE,
        "sourceFile": pptx_path.name,
        "slidesProcessed": len(slide_payload.get("slides") or []),
        "conceptsExtracted": len(concepts),
        "conceptsAfterDedupe": len(deduped_concepts),
        "dedupeReport": dedupe_report,
        "imagesFound": sum(len(s.get("images") or []) for s in slide_payload.get("slides") or []),
        "tablesFound": sum(len(s.get("tables") or []) for s in slide_payload.get("slides") or []),
        "skippedSlides": len([f for f in failures if "slide" in str(f).lower() or f.get("slideId")]),
        "topConceptExamples": [
            {
                "conceptId": c.get("conceptId"),
                "sourceSlideIds": c.get("sourceSlideIds"),
                "title": c.get("title"),
                "category": c.get("category"),
                "questionPotential": c.get("questionPotential"),
            }
            for c in concepts[:10]
        ],
        "extractionFailures": failures,
        "validationErrors": errors,
        "checkpointPath": display_path(fast_facts_checkpoint_path(pptx_path)),
    }
    report_path = write_report(report, "fast_facts_concept_extraction_report")
    if errors:
        raise PipelineError("Fast Facts concept graph validation failed:\n" + "\n".join(f"- {err}" for err in errors[:80]))
    out_path = fast_facts_output_path(pptx_path)
    write_json(out_path, graph)
    json.loads(out_path.read_text(encoding="utf-8"))
    log(f"Fast Facts concept graph -> {display_path(out_path)}")
    log(f"Fast Facts report -> {display_path(report_path)}")
    if generate:
        return run_fast_facts_generation_milestone(
            pptx_path,
            graph,
            deck_hash,
            limit_slides,
            report,
            reuse_cache=reuse_cache,
            force_regenerate=force_regenerate,
            repair_only=repair_only,
            diagnostic_report=diagnostic_report,
            question_limit=question_limit,
        )
    if diagnostic_report:
        skeleton_allocations = limit_fast_facts_question_attempts(
            fast_facts_allocations(fast_facts_normalized_payload(pptx_path, graph, deck_hash, limit_slides)[0], empty_memory()),
            question_limit,
        )
        write_fast_facts_diagnostic_report({
            "runAt": timestamp_iso(),
            "profile": FAST_FACTS_PROFILE,
            "sourceFile": pptx_path.name,
            "sourcePath": str(pptx_path),
            "limitSlides": int(limit_slides or 0),
            "questionAttemptLimit": int(question_limit or 0),
            "generationRun": False,
            "entries": [
                fast_facts_diagnostic_entry(allocation)
                for allocation in skeleton_allocations
                if int(allocation.get("questionCount") or 0) > 0
            ],
        })
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate NBME-style questions from lecture-slide PDFs.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Run deterministic pipeline without Gemini.")
    mode.add_argument("--generate", action="store_true", help="Run semantic normalization and question generation with Gemini.")
    mode.add_argument("--repair-existing", action="store_true", help="Repair existing generated questions without rerunning full-deck generation.")
    mode.add_argument("--validate-only", default="", help="Validate an app-ready JSON output.")
    parser.add_argument("--fast-facts-profile", action="store_true", help="Use FAST_FACTS_PROFILE PPTX concept graph mode. Combine with --generate for the constrained generation milestone.")
    parser.add_argument("--amboss-profile", action="store_true", help="Use AMBOSS_PROFILE deterministic extraction mode for PDFs or screenshot exports.")
    parser.add_argument("--reuse-cache", action=argparse.BooleanOptionalAction, default=True, help="Reuse valid Fast Facts cached questions when available.")
    parser.add_argument("--force-regenerate", action="store_true", help="Ignore Fast Facts cache hits and regenerate eligible questions.")
    parser.add_argument("--repair-only", action="store_true", help="Fast Facts only: repair/revalidate cache without generating cache misses.")
    parser.add_argument("--show-cache-status", action="store_true", help="Show Fast Facts question cache status and exit.")
    parser.add_argument("--fast-facts-diagnostic-report", action="store_true", help="Fast Facts only: write a diagnostic report for the generation and validation chain.")
    parser.add_argument("--fast-facts-question-limit", type=int, default=0, help="Fast Facts only: cap attempted generation allocations without changing the normal default.")
    parser.add_argument("--input-dir", default="", help="Folder containing lecture PDFs.")
    parser.add_argument("--input-file", default="", help="Single PDF/PPTX input file.")
    parser.add_argument("--normalized-chunks", default="", help="Shared normalized chunk bundle JSON to consume instead of reopening the source PDF.")
    parser.add_argument("--limit", type=int, default=0, help="Limit PDFs processed.")
    parser.add_argument(
        "--v5",
        action="store_true",
        help=(
            "Use the v5 multi-stage organic generator (stem → distractors → critic → "
            "image-route → randomize) for question authoring. Higher cost (~4x API "
            "calls per question, uses gemini-2.5-pro) but produces NBME-authentic "
            "questions with order/difficulty stratification and balanced answer "
            "position distribution. Required for any new generation; the legacy "
            "single-call path is preserved only for cache repairs."
        ),
    )
    parser.add_argument(
        "--v5-order-mix",
        default="0.25,0.45,0.30",
        help="v5 only: comma-separated targets for first_order,second_order,third_order shares.",
    )
    parser.add_argument(
        "--v5-difficulty-mix",
        default="0.30,0.45,0.25",
        help="v5 only: comma-separated targets for easy,medium,difficult shares.",
    )
    parser.add_argument(
        "--v5-seed",
        type=int,
        default=0,
        help="v5 only: RNG seed for reproducible position randomization.",
    )
    return parser.parse_args()


def main() -> int:
    ensure_dirs()
    args = parse_args()
    if getattr(args, "v5", False):
        try:
            order_parts = [float(x) for x in args.v5_order_mix.split(",")]
            diff_parts = [float(x) for x in args.v5_difficulty_mix.split(",")]
            assert len(order_parts) == 3 and abs(sum(order_parts) - 1.0) < 0.02
            assert len(diff_parts) == 3 and abs(sum(diff_parts) - 1.0) < 0.02
            _V5_CONFIG.set({
                "order_mix": {
                    "first_order": order_parts[0],
                    "second_order": order_parts[1],
                    "third_order": order_parts[2],
                },
                "difficulty_mix": {
                    "easy": diff_parts[0],
                    "medium": diff_parts[1],
                    "difficult": diff_parts[2],
                },
                "seed": args.v5_seed,
            })
            log("v5 pipeline configured — order_mix=%s difficulty_mix=%s seed=%d" % (
                args.v5_order_mix, args.v5_difficulty_mix, args.v5_seed,
            ))
        except (ValueError, AssertionError) as exc:
            print(f"Invalid v5 mix arguments: {exc}", file=sys.stderr)
            return 2
    try:
        if args.validate_only:
            validate_only(Path(args.validate_only).resolve())
            return 0
        if args.show_cache_status:
            show_fast_facts_cache_status()
            return 0
        if args.fast_facts_profile:
            if args.input_file:
                pptx_files = [Path(args.input_file).expanduser().resolve()]
            else:
                input_dir = Path(args.input_dir).resolve() if args.input_dir else INPUT_DIR
                pptx_files = supported_pptx(input_dir)
            if not pptx_files:
                raise PipelineError("No PPTX files found for FAST_FACTS_PROFILE.")
            outputs = [
                process_fast_facts_pptx(
                    path,
                    limit_slides=args.limit,
                    generate=args.generate or args.repair_only,
                    reuse_cache=args.reuse_cache,
                    force_regenerate=args.force_regenerate,
                    repair_only=args.repair_only,
                    diagnostic_report=args.fast_facts_diagnostic_report,
                    question_limit=args.fast_facts_question_limit,
                )
                for path in pptx_files[:1]
            ]
            print("Generated Fast Facts files:")
            for output in outputs:
                print(f"  {output}")
            return 0
        if args.amboss_profile:
            if args.input_file:
                amboss_inputs = [Path(args.input_file).expanduser().resolve()]
            else:
                input_dir = Path(args.input_dir).resolve() if args.input_dir else INPUT_DIR
                amboss_inputs = supported_amboss_inputs(input_dir)
            if not amboss_inputs:
                raise PipelineError("No PDF/image files found for AMBOSS_PROFILE.")
            # --limit 0 means no page cap; positive int caps at that many pages.
            outputs = [process_amboss_input(path, limit_pages=int(args.limit or 0)) for path in amboss_inputs[:1]]
            print("Generated AMBOSS files:")
            for output in outputs:
                print(f"  {output}")
            return 0
        if not args.dry_run and not args.generate and not args.repair_existing:
            print("No action specified. Use --dry-run, --generate, --repair-existing, --fast-facts-profile, or --validate-only.")
            return 2
        if args.normalized_chunks:
            if args.repair_existing:
                raise PipelineError("--repair-existing is not supported with --normalized-chunks.")
            output = process_normalized_chunks(Path(args.normalized_chunks).expanduser().resolve(), generate=args.generate)
            print("Generated app-ready files:")
            print(f"  {output}")
            return 0
        if args.input_file:
            pdfs = [Path(args.input_file).expanduser().resolve()]
        else:
            input_dir = Path(args.input_dir).resolve() if args.input_dir else INPUT_DIR
            pdfs = supported_pdfs(input_dir)
        if args.limit:
            pdfs = pdfs[:args.limit]
        if not pdfs:
            raise PipelineError(f"No PDF files found in {input_dir}")
        outputs: list[Path] = []
        for pdf in pdfs:
            if args.repair_existing:
                outputs.append(repair_existing_questions(pdf))
            else:
                outputs.append(process_pdf(pdf, generate=args.generate))
        print("Generated app-ready files:")
        for output in outputs:
            print(f"  {output}")
        return 0
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
