#!/usr/bin/env python3
"""
Divine Intervention Podcast Audio → Step 2 Question Generator

Pipeline stages:
  1. Audio ingestion      input_audio/*.{mp3,m4a,wav}
  2. Gemini transcription → transcripts/raw/<stem>_raw.txt
  3. Transcript cleanup  → transcripts/cleaned/<stem>_cleaned.txt
  4. Chunking            → output_json/chunks/<stem>_chunks.json
  5. Question generation → output_json/app_ready/<stem>_app_ready.json

Usage:
  python3 generate_divine_questions.py --dry-run
  python3 generate_divine_questions.py --transcribe-only
  python3 generate_divine_questions.py --clean-only
  python3 generate_divine_questions.py --chunk-only
  python3 generate_divine_questions.py --generate
  python3 generate_divine_questions.py --generate --questions-per-file 15

For --dry-run: place cleaned transcripts in transcripts/cleaned/ (test fixture included).
For --generate: place audio files in input_audio/. Existing transcripts are reused
  automatically — delete them to force re-transcription or re-cleaning.

Reuses from tools/uworld-notes-question-generator/generate_uworld_questions.py:
  Gemini HTTP client, JSON cleaning, 3-stage JSON parse, validation, retry/repair,
  split_into_chunks, build_app_ready_json, write_report, dry-run placeholders,
  duplicate stem detection, question renumbering.

New infrastructure in this script:
  Gemini File API upload (resumable two-step: initiate session → upload bytes),
  file state polling, audio-aware generateContent call,
  transcript cleaning (large output), incremental stage resumption.

Security:
  GEMINI_API_KEY read only from os.environ. Never logged, never stored.
  safeExportJson() is applied by the app at import time — no extra action here.
"""

import argparse
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Import stable UWorld infrastructure ───────────────────────────────────────
_UW_DIR = Path(__file__).parent.parent / "uworld-notes-question-generator"
if not _UW_DIR.is_dir():
    sys.exit(f"ERROR: UWorld generator not found at: {_UW_DIR}")
sys.path.insert(0, str(_UW_DIR))
import generate_uworld_questions as _uw  # noqa: E402

# ── v5.8: shared Advanced Mode (v5.2 multi-stage organic) adapter ─────────────
_SHARED_DIR = Path(__file__).parent.parent / "shared-ingestion"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
from v5_uworld_family_adapter import (  # noqa: E402
    add_v5_cli_args,
    process_file_v5_uworld_family,
    resolve_v5_cfg,
)
from typing import Any  # noqa: E402,F811

# v4.79: Vertex migration — google-genai SDK types for the multimodal calls
# (transcription via fileData/fileUri). When GEMINI_BACKEND=vertex, audio
# uploads land in GCS (gs:// URI) and the SDK consumes that URI directly;
# when GEMINI_BACKEND=ai_studio, the original AI Studio Files API resumable
# upload + https:// URI path is preserved as a fallback.
try:
    from google.genai import types as _genai_types  # noqa: E402
    _GENAI_SDK_AVAILABLE = True
except ImportError:
    _GENAI_SDK_AVAILABLE = False

# v4.79 Phase D: google-cloud-storage for Vertex-mode audio uploads.
# Optional — only required when GEMINI_BACKEND=vertex; AI Studio fallback
# doesn't need it.
try:
    from google.cloud import storage as _gcs_storage  # noqa: E402
    _GCS_SDK_AVAILABLE = True
except ImportError:
    _GCS_SDK_AVAILABLE = False

# GCS bucket where Divine audio gets staged. Lifecycle rule on the bucket
# auto-deletes objects after 1 day (mirrors AI Studio Files API's 48-hour
# auto-delete with a tighter window). Bucket must be in the same region
# as Vertex (us-central1) to avoid cross-region transfer costs.
_GCS_BUCKET = os.environ.get("GCS_BUCKET", "shamsulalamx-divine-audio").strip()

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE      = Path(__file__).parent
AUDIO_DIR  = _BASE / "input_audio"
RAW_DIR    = _BASE / "transcripts" / "raw"
CLEANED_DIR = _BASE / "transcripts" / "cleaned"
SEGMENT_DIR  = _BASE / "output_json" / "chunks"
GEN_DIR    = _BASE / "output_json" / "generated"
DEBUG_DIR  = _BASE / "output_json" / "generated" / "debug"
APP_DIR    = _BASE / "output_json" / "app_ready"
REPORT_DIR = _BASE / "reports"
PROMPTS_DIR = _BASE / "prompts"

SUPPORTED_AUDIO = {".mp3", ".m4a", ".wav"}
SUPPORTED_TRANSCRIPTS = {".txt", ".md"}

# Gemini File API base (separate from the generateContent endpoint)
_GEMINI_FILES_BASE = "https://generativelanguage.googleapis.com"

# ── Patch UWorld globals to point at Divine workspace ─────────────────────────
_uw.DEBUG_DIR  = DEBUG_DIR
_uw.PROMPT_FILE = PROMPTS_DIR / "divine_audio_to_questions_prompt.txt"
_uw.REPORT_DIR = REPORT_DIR

# ── Patch sourceFormat to "divine-audio" ──────────────────────────────────────
_orig_build_app_ready = _uw.build_app_ready_json


def _divine_build_app_ready_json(source_stem, questions, warnings):
    result = _orig_build_app_ready(source_stem, questions, warnings)
    result["sourceFormat"] = "divine-audio"
    return result


_uw.build_app_ready_json = _divine_build_app_ready_json


# ── Audio helpers ──────────────────────────────────────────────────────────────

_MIME_MAP = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav"}


def _detect_mime_type(filepath: Path) -> str:
    return _MIME_MAP.get(filepath.suffix.lower(), "audio/mpeg")


def _stem_from_raw(path: Path) -> str:
    """Strip '_raw' suffix from raw transcript filename stem."""
    return path.stem.removesuffix("_raw")


def _stem_from_cleaned(path: Path) -> str:
    """Strip '_cleaned' suffix from cleaned transcript filename stem."""
    return path.stem.removesuffix("_cleaned")


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
    ext = selected.suffix.lower()
    if ext not in SUPPORTED_TRANSCRIPTS and ext not in SUPPORTED_AUDIO:
        supported = ", ".join(sorted(SUPPORTED_TRANSCRIPTS | SUPPORTED_AUDIO))
        raise ValueError(f"--input-file has unsupported extension '{selected.suffix}'. Supported: {supported}")
    return selected


def _is_audio_input(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_AUDIO


def _apply_output_dir(raw_path: str) -> Path:
    global SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR, RAW_DIR, CLEANED_DIR

    output_root = Path(raw_path).expanduser()
    if not output_root.is_absolute():
        output_root = (Path.cwd() / output_root).resolve()
    else:
        output_root = output_root.resolve()
    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"--output-dir must be a directory path: {output_root}")
    SEGMENT_DIR = output_root / "chunks"
    GEN_DIR = output_root / "generated"
    DEBUG_DIR = output_root / "generated" / "debug"
    APP_DIR = output_root / "app_ready"
    REPORT_DIR = output_root / "reports"
    RAW_DIR = output_root / "transcripts" / "raw"
    CLEANED_DIR = output_root / "transcripts" / "cleaned"
    _uw.DEBUG_DIR = DEBUG_DIR
    _uw.REPORT_DIR = REPORT_DIR
    return output_root


# ── Gemini File API: upload ────────────────────────────────────────────────────
# v4.79 NOTE: The audio upload + transcription path below stays on the AI Studio
# Files API (raw urllib + GEMINI_API_KEY) regardless of GEMINI_BACKEND, because
# the Files API has no direct Vertex equivalent — Vertex uses GCS bucket URIs
# (gs://) for multimodal large media instead. The GCS migration is Phase D
# (tomorrow), tracked in STAGE1_VERTEX_DESIGN.md.
#
# Result: when GEMINI_BACKEND=vertex, Divine becomes mixed-backend:
#   - Audio upload + transcription: AI Studio (uses GEMINI_API_KEY directly)
#   - Transcript cleaning, chunking, question generation: Vertex (via SDK)
# Both env vars need to be set during this transitional period.

def _upload_audio_file(filepath: Path, api_key: str) -> Dict:
    """
    Upload audio to Gemini File API using the two-step resumable upload protocol.

    Step 1 — Initiate upload session:
      POST metadata JSON to the upload endpoint with resumable-protocol headers.
      Response header X-Goog-Upload-URL contains the per-session upload URL.

    Step 2 — Transfer file bytes:
      POST raw audio bytes to the upload URL with offset + finalize headers.
      Response body contains file metadata JSON (name, uri, state).

    Audio bytes are NEVER placed in the metadata JSON (that caused HTTP 400
    "Metadata part is too large" with the previous multipart/related approach).

    Raises: ValueError on HTTP error, RuntimeError on malformed response.
    """
    mime_type = _detect_mime_type(filepath)
    file_bytes = filepath.read_bytes()
    file_size = len(file_bytes)

    _uw.log(f"    Size: {file_size / (1024 * 1024):.1f} MB  MIME: {mime_type}")

    # ── Step 1: Initiate resumable upload session ──────────────────────────────
    metadata_json = json.dumps({"file": {"display_name": filepath.name}}).encode("utf-8")

    start_url = f"{_GEMINI_FILES_BASE}/upload/v1beta/files?key={api_key}"
    start_req = urllib.request.Request(
        start_url,
        data=metadata_json,
        method="POST",
        headers={
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(start_req, timeout=30) as start_resp:
            upload_url = start_resp.headers.get("X-Goog-Upload-URL", "")
            _uw.log(f"    Upload session initiated (HTTP {start_resp.status})")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"File API start HTTP {e.code}: {body_text[:400]}")

    if not upload_url:
        raise RuntimeError(
            "File API start response missing X-Goog-Upload-URL header"
        )

    # ── Step 2: Upload raw audio bytes to the resumable upload URL ─────────────
    upload_req = urllib.request.Request(
        upload_url,
        data=file_bytes,
        method="POST",
        headers={
            "Content-Length": str(file_size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
    )

    try:
        with urllib.request.urlopen(upload_req, timeout=600) as upload_resp:
            _uw.log(f"    Upload finalized (HTTP {upload_resp.status})")
            data = json.loads(upload_resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"File API upload HTTP {e.code}: {body_text[:400]}")

    file_meta = data.get("file", {})
    if not file_meta.get("name"):
        raise RuntimeError(
            f"File API upload response missing 'name': {str(data)[:300]}"
        )

    _uw.log(f"    File name:  {file_meta['name']}")
    _uw.log(f"    File URI:   {file_meta.get('uri', 'N/A')[:80]}")
    _uw.log(f"    File state: {file_meta.get('state', 'UNKNOWN')}")

    return file_meta


# ── Gemini File API: poll ──────────────────────────────────────────────────────

def _poll_file_active(file_name: str, api_key: str, max_wait: int = 300) -> Dict:
    """
    Poll Gemini File API until file state transitions to ACTIVE.
    file_name: e.g. "files/abc123" (from upload response).
    Uses exponential backoff: 5s, 10s, 15s, ... up to 30s.
    Raises: TimeoutError if max_wait exceeded, RuntimeError on FAILED state.
    """
    poll_url = f"{_GEMINI_FILES_BASE}/v1beta/{file_name}?key={api_key}"
    deadline = time.time() + max_wait
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(poll_url, method="GET")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise ValueError(f"File API poll HTTP {e.code}: {body_text[:300]}")

        state = data.get("state", "PROCESSING")
        if state == "ACTIVE":
            return data
        if state == "FAILED":
            raise RuntimeError(f"Gemini File API: processing FAILED for {file_name}")

        wait_s = min(5 * attempt, 30)
        _uw.log(f"    {file_name}: state={state}, waiting {wait_s}s...")
        time.sleep(wait_s)

    raise TimeoutError(
        f"File {file_name} did not become ACTIVE within {max_wait}s"
    )


# ── Gemini generateContent: audio multimodal ───────────────────────────────────

def _transcribe_with_gemini(file_uri: str, mime_type: str, api_key: str) -> str:
    """
    Call Gemini generateContent with an audio file reference (multimodal).
    Uses 65536 max output tokens (maximum for gemini-2.5-flash) and 300s timeout.
    Warns if output is truncated at MAX_TOKENS.
    Returns the transcription text.
    """
    prompt_path = PROMPTS_DIR / "transcribe_audio_prompt.txt"
    if prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    else:
        prompt = (
            "Transcribe this medical podcast completely and verbatim. "
            "Output only the spoken content as plain text paragraphs."
        )

    url = f"{_uw.GEMINI_API_BASE}/{_uw.GEMINI_MODEL}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{
            "role": "user",
            "parts": [
                {"fileData": {"mimeType": mime_type, "fileUri": file_uri}},
                {"text": prompt},
            ],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 65536,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise ValueError(f"Gemini transcription HTTP {e.code}: {body_text[:400]}")

    candidates = raw.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini transcription: no candidates returned")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise ValueError("Gemini transcription: empty parts in candidate")

    finish = candidates[0].get("finishReason", "")
    if finish == "MAX_TOKENS":
        _uw.warn(
            "Transcription output was truncated (MAX_TOKENS). "
            "Episode may be > ~3 hours. Transcript is incomplete."
        )

    return parts[0].get("text", "")


# ── v4.79 Phase D: GCS upload path (Vertex backend) ───────────────────────────

def _upload_audio_to_gcs(filepath: Path) -> str:
    """Upload audio to GCS, return its gs:// URI.

    Used when GEMINI_BACKEND=vertex. Replaces the AI Studio Files API
    two-step resumable upload with a single google-cloud-storage upload.
    GCS uploads are atomic and synchronous — no state polling needed.

    The bucket lifecycle rule (1-day delete) handles cleanup automatically.

    Raises:
        EnvironmentError: if google-cloud-storage SDK isn't installed.
        google.cloud.exceptions.NotFound: if bucket doesn't exist (one-time
            setup: `gcloud storage buckets create gs://<bucket-name>
            --location=us-central1`).
    """
    if not _GCS_SDK_AVAILABLE:
        raise EnvironmentError(
            "GEMINI_BACKEND=vertex requires google-cloud-storage. "
            "Install with: pip install google-cloud-storage"
        )
    file_size = filepath.stat().st_size
    _uw.log(f"    Size: {file_size / (1024 * 1024):.1f} MB  MIME: {_detect_mime_type(filepath)}")
    _uw.log(f"    Target: gs://{_GCS_BUCKET}/divine-audio/{filepath.name}")

    client = _gcs_storage.Client(project=_uw.GCP_PROJECT_ID)
    bucket = client.bucket(_GCS_BUCKET)
    blob_name = f"divine-audio/{filepath.name}"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(filepath), content_type=_detect_mime_type(filepath))
    gs_uri = f"gs://{_GCS_BUCKET}/{blob_name}"
    _uw.log(f"    Upload complete: {gs_uri}")
    return gs_uri


def _transcribe_with_vertex(gs_uri: str, mime_type: str) -> str:
    """Transcribe audio via Vertex AI generateContent with a gs:// fileUri.

    Used when GEMINI_BACKEND=vertex. Replaces the AI Studio
    _transcribe_with_gemini path. The SDK accepts gs:// URIs directly via
    Part.from_uri — no special handling needed beyond providing the URI.

    Preserves all the pre-v4.79 behavior:
      - 65536 max output tokens (max for gemini-2.5-flash)
      - MAX_TOKENS truncation warning
      - Custom prompt from prompts/transcribe_audio_prompt.txt with fallback
    """
    if not _GENAI_SDK_AVAILABLE:
        raise EnvironmentError(
            "google-genai SDK not installed. Install with: pip install google-genai"
        )
    prompt_path = PROMPTS_DIR / "transcribe_audio_prompt.txt"
    if prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    else:
        prompt = (
            "Transcribe this medical podcast completely and verbatim. "
            "Output only the spoken content as plain text paragraphs."
        )

    client = _uw._gemini_client()
    response = client.models.generate_content(
        model=_uw.GEMINI_MODEL,
        contents=[
            _genai_types.Part.from_uri(file_uri=gs_uri, mime_type=mime_type),
            prompt,
        ],
        config=_genai_types.GenerateContentConfig(
            temperature=0.1,
            # max output for gemini-2.5-flash. Long episodes (>~3hrs) may
            # still hit this; the MAX_TOKENS check below catches that.
            max_output_tokens=65536,
            # v4.79: thinking enabled. Transcription benefits less than
            # question-gen but doesn't hurt — model can reason about
            # ambiguous audio segments.
            thinking_config=_genai_types.ThinkingConfig(thinking_budget=-1),
        ),
    )

    # MAX_TOKENS truncation detection — preserved from pre-v4.79.
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish = str(getattr(candidates[0], "finish_reason", "") or "")
        if "MAX_TOKENS" in finish.upper():
            _uw.warn(
                "Transcription output was truncated (MAX_TOKENS). "
                "Episode may be > ~3 hours. Transcript is incomplete."
            )

    text = getattr(response, "text", None)
    return str(text or "")


def transcribe_audio(filepath: Path) -> str:
    """
    Full audio transcription pipeline: upload → generateContent → text.
    Returns raw transcription text. Does NOT save to disk — caller saves.

    v4.79: now dispatches on GEMINI_BACKEND.
      - vertex: upload to GCS bucket, transcribe via Vertex with gs:// URI
      - ai_studio: original AI Studio Files API resumable upload + transcribe

    Both paths produce identical transcript text; only the storage + auth
    layer differs. AI Studio path stays as a fallback through the cutover
    window — remove ~1 week post-Vertex-stable.
    """
    backend = _uw.GEMINI_BACKEND

    mime_type = _detect_mime_type(filepath)
    size_mb = filepath.stat().st_size / (1024 * 1024)
    _uw.log(f"  Uploading {filepath.name} ({size_mb:.1f} MB, {mime_type}) via {backend}...")

    if backend == "vertex":
        # v4.79 Phase D: GCS + Vertex path. Atomic upload (no polling).
        gs_uri = _upload_audio_to_gcs(filepath)
        _uw.log("  Transcribing via Vertex AI...")
        transcript = _transcribe_with_vertex(gs_uri, mime_type)
        _uw.log(f"  Transcription done: {len(transcript):,} chars")
        return transcript

    # AI Studio fallback path (pre-v4.79 behavior, preserved verbatim).
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY is not set (required for ai_studio backend)")

    file_meta = _upload_audio_file(filepath, api_key)
    file_name = file_meta["name"]
    file_uri = file_meta.get("uri", f"{_GEMINI_FILES_BASE}/v1beta/{file_name}")
    _uw.log(f"  Upload complete: {file_name}")

    if file_meta.get("state") != "ACTIVE":
        _uw.log("  Waiting for file processing...")
        file_meta = _poll_file_active(file_name, api_key)

    _uw.log("  Transcribing via AI Studio...")
    transcript = _transcribe_with_gemini(file_uri, mime_type, api_key)
    _uw.log(f"  Transcription done: {len(transcript):,} chars")
    return transcript


# ── Transcript cleaning ────────────────────────────────────────────────────────

def _gemini_text_call(
    api_key: str,
    prompt: str,
    max_output: int = 32768,
    timeout: int = 180,
) -> str:
    """
    Gemini text generation with configurable output token limit.
    Used for transcript cleaning — podcast transcripts require higher maxOutputTokens
    than the UWorld generator's default of 8192 (which would truncate long transcripts).
    Never logs the API key.

    v4.79: rewritten to use google-genai SDK via _uw._gemini_client(). The
    finishReason='MAX_TOKENS' truncation check is preserved — long episodes
    can blow the 32K output cap and the user needs to know.
    """
    if not _GENAI_SDK_AVAILABLE:
        raise ValueError(
            "google-genai SDK not installed. Install with: pip install google-genai"
        )
    try:
        client = _uw._gemini_client()
        response = client.models.generate_content(
            model=_uw.GEMINI_MODEL,
            contents=prompt,
            config=_genai_types.GenerateContentConfig(
                temperature=0.2,
                # v4.79: max_output already large (32K-65K default) so no
                # additional bump needed for thinking — there's plenty of
                # headroom. Thinking helps cleaning quality on transcripts
                # with mixed clinical + housekeeping content.
                max_output_tokens=max_output,
                # v4.79: Dynamic thinking. Quality > cost.
                thinking_config=_genai_types.ThinkingConfig(thinking_budget=-1),
            ),
        )
    except EnvironmentError:
        raise
    except Exception as e:
        raise ValueError(f"Gemini call failed: {e}")

    # Truncation check — preserved from pre-v4.79 because long Divine
    # episodes can hit max_output even at 32768 tokens.
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish = str(getattr(candidates[0], "finish_reason", "") or "")
        if "MAX_TOKENS" in finish.upper():
            _uw.warn(
                "Cleaning output truncated (MAX_TOKENS). "
                "Transcript may be too long for a single cleaning call."
            )

    text = getattr(response, "text", None)
    if not text:
        raise ValueError(f"Gemini returned no usable text: candidates={candidates!r}"[:400])
    return str(text)


def clean_transcript(raw_text: str, api_key: str) -> str:
    """
    Clean raw podcast transcript using Gemini.
    Removes ads, intros, housekeeping; preserves clinical teaching content.
    Returns cleaned plain text.
    Falls back to raw_text if Gemini returns empty output (defensive).

    Input is capped at 120,000 chars (~30,000 tokens) for the cleaning call.
    Episodes longer than ~90 minutes may exceed this cap.
    """
    template_path = PROMPTS_DIR / "clean_transcript_prompt.txt"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8").strip()
        # Cap raw input to avoid exceeding Gemini context limits
        transcript_input = raw_text[:120000]
        prompt = template.replace("{{TRANSCRIPT}}", transcript_input)
        if len(raw_text) > 120000:
            _uw.warn(
                f"Raw transcript ({len(raw_text):,} chars) exceeds 120,000-char cleaning cap "
                f"— only the first 120,000 chars will be cleaned."
            )
    else:
        prompt = (
            "Clean this medical podcast transcript. Remove ads, intros, housekeeping. "
            f"Preserve all clinical teaching content. Return plain text only.\n\n{raw_text[:120000]}"
        )

    _uw.log(f"  Cleaning transcript ({len(raw_text):,} chars)...")
    cleaned = _gemini_text_call(api_key, prompt, max_output=32768, timeout=180)
    _uw.log(f"  Cleaned: {len(cleaned):,} chars")

    if not cleaned.strip():
        _uw.warn("Transcript cleaning returned empty output — using raw transcript")
        return raw_text

    return cleaned


# ── Stages 4-5: chunking and question generation ───────────────────────────────

def _process_cleaned_transcript(
    stem: str,
    cleaned_text: str,
    questions_per_file: int,
    dry_run: bool,
    report_data: Dict,
) -> None:
    """
    Stages 4-5 of the Divine pipeline.
    Input: stem (base name) + cleaned transcript text (already extracted).
    Outputs: chunks JSON + app-ready JSON.
    Reuses UWorld chunker, Gemini caller, validator, and app-ready builder.
    """
    t_start = time.time()
    _uw.log(f"Generating questions: {stem}")

    if not cleaned_text.strip():
        _uw.warn(f"Empty cleaned transcript for {stem} — skipping question generation")
        report_data["files"][stem] = {
            "status": "skipped",
            "reason": "empty_transcript",
        }
        return

    # Stage 4: Chunk
    chunks = _uw.split_into_chunks(cleaned_text, max_chars=3000)
    chunk_path = SEGMENT_DIR / f"{stem}_chunks.json"
    chunk_path.write_text(
        json.dumps(
            {"sourceFile": f"{stem}_cleaned.txt", "chunks": chunks},
            indent=2,
        ),
        encoding="utf-8",
    )
    _uw.log(f"  {len(chunks)} chunk(s) → {chunk_path.name}")

    # v5.12.3: BIC progress denominator. Divine's chunker (like the shared
    # uworld-family one) floors at >=1 question per chunk, so a small
    # questions_per_file on a long transcript yields ~len(chunks) questions.
    # Use the true expected total = max(questions_per_file, len(chunks)) so the
    # bar doesn't freeze at 100% while generation keeps going.
    expected_total = max(int(questions_per_file), len(chunks))

    all_questions: List[Dict] = []
    file_warnings: List[str] = []
    chunk_stats: List[Dict] = []
    gen_stats: Dict = {
        "validationFailures": 0,
        "retries": 0,
        "repairsSucceeded": 0,
        "repairFailures": 0,
    }

    # Stage 5: Generate (or placeholders in dry-run)
    if dry_run:
        _uw.log(f"  [DRY-RUN] Generating {questions_per_file} placeholder questions.")
        all_questions = [_uw._placeholder_question(i + 1) for i in range(questions_per_file)]
        file_warnings.append("dry-run: questions are placeholders, not Gemini-generated")
        chunk_stats = [{"chunk": 1, "status": "dry-run", "questions": questions_per_file}]

    else:
        qpc = max(1, questions_per_file // max(len(chunks), 1))
        remainder = questions_per_file - qpc * len(chunks)
        q_offset = 0
        raw_generated: List[Dict] = []
        # v5.12.2/.3: live BIC progress for Divine's legacy loop. Divine has its
        # OWN chunk loop (calls _uw.call_gemini_with_retry directly) rather than
        # _uw.process_file, so it needs its own emit calls. Reuses the shared
        # emitter on the _uw module. expected_total is the true denominator.
        _uw._emit_bic_progress(
            phase="generating",
            message=f"Starting generation: 0 of {expected_total} question(s)",
            question=0,
            questionTotal=expected_total,
            chunkTotal=len(chunks),
        )

        for ci, chunk in enumerate(chunks):
            n = qpc + (1 if ci < remainder else 0)
            if n == 0:
                chunk_stats.append({"chunk": ci + 1, "status": "skipped", "requested": 0})
                continue

            _uw.log(f"  Chunk {ci+1}/{len(chunks)} → {n} question(s) from Gemini...")
            c_stat: Dict = {"chunk": ci + 1, "requested": n}

            try:
                qs, chunk_warnings = _uw.call_gemini_with_retry(
                    chunk["chunkText"], n, ci + 1, gen_stats
                )
                qs = _uw.renumber_questions(qs, q_offset)
                raw_generated.extend(qs)
                all_questions.extend(qs)
                file_warnings.extend(chunk_warnings)
                q_offset += len(qs)
                c_stat["status"] = "ok"
                c_stat["generated"] = len(qs)
                _uw.log(f"    ✓ {len(qs)} question(s)")
                time.sleep(1)

            except json.JSONDecodeError as exc:
                msg = f"Chunk {ci+1} JSON parse error: {exc}"
                _uw.warn(msg)
                file_warnings.append(msg)
                c_stat["status"] = "json_error"
                c_stat["error"] = str(exc)

            except Exception as exc:
                msg = f"Chunk {ci+1} failed: {exc}"
                _uw.warn(msg)
                file_warnings.append(msg)
                c_stat["status"] = "error"
                c_stat["error"] = str(exc)

            chunk_stats.append(c_stat)
            _uw._emit_bic_progress(
                phase="generating",
                message=f"Generated {len(all_questions)} of {expected_total} question(s)",
                question=len(all_questions),
                questionTotal=expected_total,
                chunk=ci + 1,
                chunkTotal=len(chunks),
            )

        dup_warnings = _uw.check_duplicate_stems(all_questions)
        if dup_warnings:
            file_warnings.extend(dup_warnings)
            for w in dup_warnings:
                _uw.warn(w)

        gen_path = GEN_DIR / f"{stem}_generated.json"
        gen_path.write_text(json.dumps(raw_generated, indent=2), encoding="utf-8")
        _uw.log(f"  Generated JSON → {gen_path.name} ({len(raw_generated)} questions)")

    # Build and write app-ready JSON
    _uw._emit_bic_progress(
        phase="writing",
        message=f"Finalizing {len(all_questions)} question(s)",
        question=len(all_questions),
        questionTotal=expected_total,
    )
    app_json = _uw.build_app_ready_json(stem, all_questions, file_warnings)
    app_path = APP_DIR / f"{stem}_app_ready.json"
    app_path.write_text(json.dumps(app_json, indent=2), encoding="utf-8")
    _uw.log(f"  App-ready JSON → {app_path.name} ({len(all_questions)} questions)")

    elapsed = round(time.time() - t_start, 1)
    report_data["files"][stem] = {
        "status": "ok",
        "cleanedChars": len(cleaned_text),
        "chunksProcessed": len(chunks),
        "questionsGenerated": len(all_questions),
        "validationFailures": gen_stats["validationFailures"],
        "retries": gen_stats["retries"],
        "repairsSucceeded": gen_stats["repairsSucceeded"],
        "repairFailures": gen_stats["repairFailures"],
        "warnings": file_warnings,
        "chunkStats": chunk_stats,
        "outputPaths": {
            "appReady": str(app_path),
            "chunks": str(chunk_path),
            "generated": str(GEN_DIR / f"{stem}_generated.json") if not dry_run else None,
        },
        "elapsedSeconds": elapsed,
        "dryRun": dry_run,
    }


# ── Discovery ──────────────────────────────────────────────────────────────────

def discover_audio_files() -> List[Path]:
    if not AUDIO_DIR.exists():
        return []
    return sorted(
        f for f in AUDIO_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_AUDIO
    )


def discover_raw_transcripts() -> List[Path]:
    if not RAW_DIR.exists():
        return []
    return sorted(
        f for f in RAW_DIR.iterdir()
        if f.is_file() and f.name.endswith("_raw.txt")
    )


def discover_cleaned_transcripts() -> List[Path]:
    if not CLEANED_DIR.exists():
        return []
    return sorted(
        f for f in CLEANED_DIR.iterdir()
        if f.is_file() and f.name.endswith("_cleaned.txt")
    )


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Divine Intervention Podcast Audio → Step 2 Question Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            MODES (mutually exclusive):

              --dry-run          Chunk existing cleaned transcripts → placeholder app-ready JSON.
                                 No API key required. Uses transcripts/cleaned/*.txt.
                                 Test fixture: transcripts/cleaned/test_divine_heart_failure_cleaned.txt

              --transcribe-only  Audio files → raw transcripts.
                                 Input: input_audio/. Output: transcripts/raw/.
                                 Skips files that already have a raw transcript.

              --clean-only       Raw transcripts → cleaned transcripts.
                                 Input: transcripts/raw/. Output: transcripts/cleaned/.
                                 Skips files that already have a cleaned transcript.

              --chunk-only       Cleaned transcripts → chunk JSON. No API key required.
                                 Input: transcripts/cleaned/. Output: output_json/chunks/.

              --generate         Full pipeline (all stages). Reuses existing transcripts
                                 if present — delete them to force re-transcription.
                                 Also processes standalone cleaned transcripts without
                                 corresponding audio files.

            AUDIO FORMATS:  .mp3  .m4a  .wav

            INCREMENTAL RUNS:
              --generate checks for existing transcripts/raw/<stem>_raw.txt and
              transcripts/cleaned/<stem>_cleaned.txt before invoking Gemini for those
              stages. Safe to re-run without re-billing for completed stages.

            EXAMPLES:
              python3 generate_divine_questions.py --dry-run
              python3 generate_divine_questions.py --dry-run --questions-per-file 10
              python3 generate_divine_questions.py --transcribe-only
              python3 generate_divine_questions.py --clean-only
              python3 generate_divine_questions.py --chunk-only
              python3 generate_divine_questions.py --generate
              python3 generate_divine_questions.py --generate --questions-per-file 15
        """),
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--dry-run", action="store_true",
        help="No API calls. Chunk cleaned transcripts, write placeholder questions.",
    )
    mode_group.add_argument(
        "--transcribe-only", action="store_true",
        help="Stage 2 only: upload audio to Gemini and save raw transcripts.",
    )
    mode_group.add_argument(
        "--clean-only", action="store_true",
        help="Stage 3 only: clean existing raw transcripts with Gemini.",
    )
    mode_group.add_argument(
        "--chunk-only", action="store_true",
        help="Stage 4 only: chunk cleaned transcripts. No API call.",
    )
    mode_group.add_argument(
        "--generate", action="store_true",
        help="Full pipeline. Reuses existing transcripts when present.",
    )
    parser.add_argument(
        "--questions-per-file", type=int, default=15, metavar="N",
        help="Target questions per audio file or cleaned transcript (default: 15).",
    )
    parser.add_argument(
        "--input-file",
        default="",
        help="Process one selected transcript file instead of scanning transcript folders. Supported: .txt, .md.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Optional output root. Writes chunks/, generated/, app_ready/, and reports/ under this directory.",
    )
    # v5.8: Advanced Mode (v5.2 multi-stage organic pipeline)
    add_v5_cli_args(parser)
    args = parser.parse_args()

    try:
        selected_input = _resolve_selected_input(args.input_file) if args.input_file else None
        output_root = _apply_output_dir(args.output_dir) if args.output_dir else None
    except ValueError as exc:
        parser.error(str(exc))
    if selected_input and _is_audio_input(selected_input) and not args.generate:
        parser.error("--input-file with an audio file (.mp3/.m4a/.wav) is supported only with --generate (full pipeline).")
    if selected_input and (args.transcribe_only or args.clean_only):
        parser.error("--input-file is supported only with --dry-run, --chunk-only, or --generate.")

    # ── Startup log ────────────────────────────────────────────────────────────
    mode_label = (
        "dry-run" if args.dry_run
        else "transcribe-only" if args.transcribe_only
        else "clean-only" if args.clean_only
        else "chunk-only" if args.chunk_only
        else "generate (full pipeline)"
    )
    _uw.log("=" * 60)
    _uw.log("Divine Intervention Audio → Question Generator")
    _uw.log(f"  Model:              {_uw.GEMINI_MODEL}")
    _uw.log(f"  Mode:               {mode_label}")
    _uw.log(f"  Questions per file: {args.questions_per_file}")
    _uw.log(f"  Input mode:         {'selected transcript' if selected_input else 'workspace scan'}")
    if selected_input:
        _uw.log(f"  Selected input:     {selected_input}")
    if output_root:
        _uw.log(f"  Output root:        {output_root}")
    _uw.log("=" * 60)

    # ── Create all directories ─────────────────────────────────────────────────
    for d in (
        AUDIO_DIR, RAW_DIR, CLEANED_DIR,
        SEGMENT_DIR, GEN_DIR, DEBUG_DIR, APP_DIR, REPORT_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)

    # ── API key / auth check ──────────────────────────────────────────────────
    # v4.79: backend-aware. AI Studio needs GEMINI_API_KEY env var; Vertex
    # uses Application Default Credentials (gcloud auth) instead, so the key
    # is irrelevant. We only fail-fast on missing auth when the chosen backend
    # would actually need it.
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    needs_api = not (args.dry_run or args.chunk_only)
    if needs_api:
        backend = _uw.GEMINI_BACKEND
        if backend == "ai_studio" and not api_key:
            _uw.log("ERROR: GEMINI_API_KEY is required for the ai_studio backend.")
            _uw.log("Set with: export GEMINI_API_KEY=your_key_here")
            _uw.log("Or switch to Vertex: export GEMINI_BACKEND=vertex")
            sys.exit(1)
        if backend == "vertex":
            # Sanity check: Vertex needs ADC. _gemini_client() will raise a
            # detailed error from inside, but doing one upfront probe here
            # lets us fail fast with a clear message before the user waits
            # through chunking + upload only to die at the first call.
            try:
                _uw._gemini_client()  # constructs client, doesn't call API
            except EnvironmentError as e:
                _uw.log(f"ERROR: Vertex backend not ready: {e}")
                _uw.log("Run: gcloud auth application-default login")
                sys.exit(1)

    report_data: Dict = {
        "runTimestamp": datetime.now().isoformat(),
        "model": _uw.GEMINI_MODEL,
        "mode": mode_label,
        "questionsPerFile": args.questions_per_file,
        "files": {},
    }
    t_total = time.time()

    # v5.8: resolve v5 config once when --v5 is passed in --generate mode.
    v5_cfg: Optional[Dict[str, Any]] = None
    if getattr(args, "v5", False):
        if not args.generate:
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

    def _dispatch_cleaned_transcript(stem: str, cleaned_text: str, dry_run: bool) -> None:
        """v5.8 routing: when --v5 is set and this is a live run, use the
        shared v5.2 multi-stage organic pipeline. On any v5 failure, fall
        back to the legacy single-call _process_cleaned_transcript."""
        if v5_cfg is not None and not dry_run:
            cleaned_path = CLEANED_DIR / f"{stem}_cleaned.txt"
            try:
                process_file_v5_uworld_family(
                    uw_module=_uw,
                    filepath=cleaned_path,
                    questions_per_file=args.questions_per_file,
                    v5_cfg=v5_cfg,
                    report_data=report_data,
                    pipeline_label="v5.2-organic",
                    chunk_size=int(args.chunk_size),
                    questions_per_chunk=int(args.questions_per_chunk),
                    pre_extracted_text=cleaned_text,
                    output_stem=stem,
                )
                return
            except Exception as v5_exc:
                _uw.warn(
                    f"v5 pipeline failed for {stem} ({v5_exc}); "
                    f"falling back to legacy Divine pipeline."
                )
        _process_cleaned_transcript(stem, cleaned_text, args.questions_per_file, dry_run, report_data)

    # ══════════════════════════════════════════════════════════════════════════
    # Mode: --dry-run
    # ══════════════════════════════════════════════════════════════════════════
    if args.dry_run:
        cleaned_files = [selected_input] if selected_input else discover_cleaned_transcripts()
        if not cleaned_files:
            _uw.log("No cleaned transcripts found in transcripts/cleaned/")
            _uw.log(
                "For --dry-run, place cleaned transcripts in transcripts/cleaned/ "
                "(file must end in _cleaned.txt)"
            )
            _uw.log(
                "A test fixture is committed: "
                "transcripts/cleaned/test_divine_heart_failure_cleaned.txt"
            )
            _uw.write_report(
                {"status": "no_input", "mode": mode_label, "files": {}},
                prefix="divine_generation_report",
            )
            return

        _uw.log(f"Found {len(cleaned_files)} cleaned transcript(s): "
                f"{[f.name for f in cleaned_files]}")
        for cf in cleaned_files:
            stem = cf.stem if selected_input else _stem_from_cleaned(cf)
            try:
                cleaned_text = cf.read_text(encoding="utf-8")
                # v5.8: dispatcher chooses v5 vs legacy based on --v5 flag
                _dispatch_cleaned_transcript(stem, cleaned_text, True)
            except Exception as exc:
                _uw.warn(f"Error processing {cf.name}: {exc}")
                report_data["files"][stem] = {"status": "error", "error": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # Mode: --transcribe-only
    # ══════════════════════════════════════════════════════════════════════════
    elif args.transcribe_only:
        audio_files = discover_audio_files()
        if not audio_files:
            _uw.log("No audio files found in input_audio/")
            _uw.log(
                f"Supported formats: {', '.join(sorted(SUPPORTED_AUDIO))}. "
                "Drop files into input_audio/ and re-run."
            )
            _uw.write_report(
                {"status": "no_input", "mode": mode_label, "files": {}},
                prefix="divine_generation_report",
            )
            return

        _uw.log(f"Found {len(audio_files)} audio file(s): {[f.name for f in audio_files]}")
        for af in audio_files:
            raw_path = RAW_DIR / f"{af.stem}_raw.txt"
            if raw_path.exists():
                _uw.log(f"  Skipping {af.name} — raw transcript already exists: {raw_path.name}")
                report_data["files"][af.name] = {"status": "skipped", "reason": "raw_exists"}
                continue
            try:
                transcript = transcribe_audio(af)
                if not transcript.strip():
                    _uw.warn(f"Empty transcription for {af.name} — skipping save")
                    report_data["files"][af.name] = {
                        "status": "error",
                        "error": "empty transcription output",
                    }
                    continue
                raw_path.write_text(transcript, encoding="utf-8")
                _uw.log(f"  Saved → {raw_path.name} ({len(transcript):,} chars)")
                report_data["files"][af.name] = {
                    "status": "ok",
                    "rawChars": len(transcript),
                    "outputPath": str(raw_path),
                }
            except Exception as exc:
                _uw.warn(f"Error transcribing {af.name}: {exc}")
                report_data["files"][af.name] = {"status": "error", "error": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # Mode: --clean-only
    # ══════════════════════════════════════════════════════════════════════════
    elif args.clean_only:
        raw_files = discover_raw_transcripts()
        if not raw_files:
            _uw.log("No raw transcripts found in transcripts/raw/")
            _uw.log(
                "Run --transcribe-only first to generate raw transcripts, "
                "or place _raw.txt files manually in transcripts/raw/"
            )
            _uw.write_report(
                {"status": "no_input", "mode": mode_label, "files": {}},
                prefix="divine_generation_report",
            )
            return

        _uw.log(f"Found {len(raw_files)} raw transcript(s): {[f.name for f in raw_files]}")
        for rf in raw_files:
            stem = _stem_from_raw(rf)
            cleaned_path = CLEANED_DIR / f"{stem}_cleaned.txt"
            if cleaned_path.exists():
                _uw.log(
                    f"  Skipping {rf.name} — cleaned transcript already exists: {cleaned_path.name}"
                )
                report_data["files"][rf.name] = {
                    "status": "skipped",
                    "reason": "cleaned_exists",
                }
                continue
            try:
                raw_text = rf.read_text(encoding="utf-8")
                if not raw_text.strip():
                    _uw.warn(f"Empty raw transcript: {rf.name} — skipping")
                    report_data["files"][rf.name] = {
                        "status": "skipped",
                        "reason": "empty_raw_transcript",
                    }
                    continue
                cleaned_text = clean_transcript(raw_text, api_key)
                cleaned_path.write_text(cleaned_text, encoding="utf-8")
                _uw.log(f"  Saved → {cleaned_path.name} ({len(cleaned_text):,} chars)")
                report_data["files"][rf.name] = {
                    "status": "ok",
                    "rawChars": len(raw_text),
                    "cleanedChars": len(cleaned_text),
                    "outputPath": str(cleaned_path),
                }
            except Exception as exc:
                _uw.warn(f"Error cleaning {rf.name}: {exc}")
                report_data["files"][rf.name] = {"status": "error", "error": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # Mode: --chunk-only
    # ══════════════════════════════════════════════════════════════════════════
    elif args.chunk_only:
        cleaned_files = [selected_input] if selected_input else discover_cleaned_transcripts()
        if not cleaned_files:
            _uw.log("No cleaned transcripts found in transcripts/cleaned/")
            _uw.log("Run --clean-only first, or place _cleaned.txt files manually.")
            _uw.write_report(
                {"status": "no_input", "mode": mode_label, "files": {}},
                prefix="divine_generation_report",
            )
            return

        _uw.log(f"Found {len(cleaned_files)} cleaned transcript(s): "
                f"{[f.name for f in cleaned_files]}")
        for cf in cleaned_files:
            stem = cf.stem if selected_input else _stem_from_cleaned(cf)
            try:
                cleaned_text = cf.read_text(encoding="utf-8")
                chunks = _uw.split_into_chunks(cleaned_text, max_chars=3000)
                chunk_path = SEGMENT_DIR / f"{stem}_chunks.json"
                chunk_path.write_text(
                    json.dumps(
                        {"sourceFile": cf.name, "chunks": chunks},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                _uw.log(f"  {cf.name} → {len(chunks)} chunk(s) → {chunk_path.name}")
                report_data["files"][cf.name] = {
                    "status": "ok",
                    "chunks": len(chunks),
                    "outputPath": str(chunk_path),
                }
            except Exception as exc:
                _uw.warn(f"Error chunking {cf.name}: {exc}")
                report_data["files"][cf.name] = {"status": "error", "error": str(exc)}

    # ══════════════════════════════════════════════════════════════════════════
    # Mode: --generate (full pipeline)
    # ══════════════════════════════════════════════════════════════════════════
    elif args.generate:
        if selected_input:
            stem = selected_input.stem
            try:
                if _is_audio_input(selected_input):
                    raw_path = RAW_DIR / f"{stem}_raw.txt"
                    if raw_path.exists():
                        _uw.log(f"  Reusing raw transcript: {raw_path.name}")
                        raw_text = raw_path.read_text(encoding="utf-8")
                    else:
                        raw_text = transcribe_audio(selected_input)
                        if not raw_text.strip():
                            raise RuntimeError("empty transcription output")
                        raw_path.write_text(raw_text, encoding="utf-8")
                        _uw.log(f"  Raw transcript → {raw_path.name} ({len(raw_text):,} chars)")

                    cleaned_path = CLEANED_DIR / f"{stem}_cleaned.txt"
                    if cleaned_path.exists():
                        _uw.log(f"  Reusing cleaned transcript: {cleaned_path.name}")
                        cleaned_text = cleaned_path.read_text(encoding="utf-8")
                    else:
                        cleaned_text = clean_transcript(raw_text, api_key)
                        cleaned_path.write_text(cleaned_text, encoding="utf-8")
                        _uw.log(f"  Cleaned transcript → {cleaned_path.name} ({len(cleaned_text):,} chars)")
                else:
                    cleaned_text = selected_input.read_text(encoding="utf-8")

                # v5.8: dispatcher chooses v5 vs legacy based on --v5 flag
                _dispatch_cleaned_transcript(stem, cleaned_text, False)
            except Exception as exc:
                _uw.warn(f"Error processing {selected_input.name}: {exc}")
                report_data["files"][stem] = {"status": "error", "error": str(exc)}
            audio_files = []
            processed_stems = {stem}
        else:
            audio_files = discover_audio_files()
            processed_stems: set = set()

        if audio_files:
            _uw.log(
                f"Found {len(audio_files)} audio file(s): {[f.name for f in audio_files]}"
            )

        for af in audio_files:
            stem = af.stem
            processed_stems.add(stem)
            _uw.log(f"\nProcessing audio: {af.name}")

            try:
                # Stage 2: Transcribe (skip if exists)
                raw_path = RAW_DIR / f"{stem}_raw.txt"
                if raw_path.exists():
                    _uw.log(f"  Reusing raw transcript: {raw_path.name}")
                    raw_text = raw_path.read_text(encoding="utf-8")
                else:
                    raw_text = transcribe_audio(af)
                    if not raw_text.strip():
                        _uw.warn(f"Empty transcription for {af.name} — skipping")
                        report_data["files"][stem] = {
                            "status": "error",
                            "error": "empty transcription output",
                        }
                        continue
                    raw_path.write_text(raw_text, encoding="utf-8")
                    _uw.log(f"  Raw transcript → {raw_path.name} ({len(raw_text):,} chars)")

                # Stage 3: Clean (skip if exists)
                cleaned_path = CLEANED_DIR / f"{stem}_cleaned.txt"
                if cleaned_path.exists():
                    _uw.log(f"  Reusing cleaned transcript: {cleaned_path.name}")
                    cleaned_text = cleaned_path.read_text(encoding="utf-8")
                else:
                    cleaned_text = clean_transcript(raw_text, api_key)
                    cleaned_path.write_text(cleaned_text, encoding="utf-8")
                    _uw.log(
                        f"  Cleaned transcript → {cleaned_path.name} ({len(cleaned_text):,} chars)"
                    )

                # Stages 4-5: Chunk + Generate
                # v5.8: dispatcher chooses v5 vs legacy based on --v5 flag
                _dispatch_cleaned_transcript(stem, cleaned_text, False)

            except Exception as exc:
                _uw.warn(f"Error processing {af.name}: {exc}")
                report_data["files"][stem] = {"status": "error", "error": str(exc)}

        # Also process standalone cleaned transcripts (no corresponding audio file)
        standalone_cleaned = [] if selected_input else [
            f for f in discover_cleaned_transcripts()
            if _stem_from_cleaned(f) not in processed_stems
        ]
        if standalone_cleaned:
            _uw.log(
                f"\nProcessing {len(standalone_cleaned)} standalone cleaned transcript(s): "
                f"{[f.name for f in standalone_cleaned]}"
            )
            for cf in standalone_cleaned:
                stem = _stem_from_cleaned(cf)
                try:
                    cleaned_text = cf.read_text(encoding="utf-8")
                    # v5.8: dispatcher chooses v5 vs legacy based on --v5 flag
                    _dispatch_cleaned_transcript(stem, cleaned_text, False)
                except Exception as exc:
                    _uw.warn(f"Error processing {cf.name}: {exc}")
                    report_data["files"][stem] = {"status": "error", "error": str(exc)}

        if not selected_input and not audio_files and not discover_cleaned_transcripts():
            _uw.log("No audio files found in input_audio/")
            _uw.log("No cleaned transcripts found in transcripts/cleaned/")
            _uw.log(
                "For --generate: drop audio files (.mp3/.m4a/.wav) into input_audio/ "
                "or place cleaned transcripts in transcripts/cleaned/."
            )
            _uw.write_report(
                {"status": "no_input", "mode": mode_label, "files": {}},
                prefix="divine_generation_report",
            )
            return

    # ── Write report ───────────────────────────────────────────────────────────
    report_data["totalElapsedSeconds"] = round(time.time() - t_total, 1)
    _uw.write_report(report_data, prefix="divine_generation_report")
    _uw.log("Done.")


if __name__ == "__main__":
    main()
