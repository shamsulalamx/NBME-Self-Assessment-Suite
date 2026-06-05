#!/usr/bin/env python3
"""
Run UWorld notes (txt / md / rtf / docx) through shared normalized chunk
emission, then invoke the existing UWorld generator unchanged.

v4.59 — first BIC integration of the foundational UWorld generator. UWorld
notes are pure text (the user confirmed: "absolutely no images, just text")
so this runner skips any image/table machinery and goes straight to the
shared text-chunk emitter + downstream Gemini generation.

High-yield default: UWorld notes are the densest content the user has, so
questions-per-file is auto-scaled to 1 question per ~150 chars with a
floor of 8 questions. That gives ~9-10 questions on a 3-page UWorld
note, scaling up to the MAX clamp of 80 on multi-thousand-line notes.
The auto-scale can be overridden by passing --questions-per-file
explicitly when the user wants a different density for a specific file.

Supports two modes:
  --mode dry-run   (default) Invoke the existing UWorld generator in
                   dry-run mode, producing placeholder app-ready JSON
                   without calling Gemini.
  --mode generate  Invoke the existing UWorld generator in live mode,
                   calling Gemini to produce real questions from the
                   note content. Requires GEMINI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
JOB_OUTPUT_ROOT = (
    Path(os.environ["BIC_JOB_OUTPUT_ROOT"]).expanduser().resolve()
    if os.environ.get("BIC_JOB_OUTPUT_ROOT")
    else None
)
OUTPUT_DIR = JOB_OUTPUT_ROOT / "shared-ingestion" if JOB_OUTPUT_ROOT else SCRIPT_DIR / "output"
UWORLD_GENERATOR = (
    PROJECT_ROOT / "tools" / "uworld-notes-question-generator" / "generate_uworld_questions.py"
)

sys.path.insert(0, str(SCRIPT_DIR))
from chunk_pipeline import run_shared_chunk_pipeline  # noqa: E402
from recovery_contract import recovery_metadata  # noqa: E402

SOURCE_TYPE = "uworld_notes"

# v4.59 high-yield density, retuned after the user's first live import.
# The first cut was DEFAULT=500 chars/question + MIN=5: that floor was
# too conservative on small files, where the MIN clamp dominated. A 3-page
# 1,365-char UWorld note got 5 questions when the user wanted 8-10 because
# "THESE ARE HIGH YIELD MATERIAL, and I would want more rigorous testing on these."
#
# New target: 1 question per ~150 chars (roughly 1 question per 1-2
# sentences or 1 dense bullet point) with a MIN floor of 8 so even very
# short notes get rigorous coverage. ~9-10× Mehlman density at v4.58.
#
# Example density table:
#   1.4 KB note (3 UWorld pages) -> max(8, 9)  = 9   questions
#   4   KB note                   -> max(8, 27) = 27  questions
#   10  KB note                   -> max(8, 66) = 66  questions
#   12+ KB note                   -> MAX clamp  = 80  questions (cost cap)
DEFAULT_CHARS_PER_QUESTION = 150
MIN_AUTO_QUESTIONS_PER_FILE = 8
MAX_AUTO_QUESTIONS_PER_FILE = 80


def emit(event_type: str, **payload: Any) -> None:
    print(
        json.dumps(
            {"type": event_type, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload},
            ensure_ascii=False,
        ),
        flush=True,
    )


def parse_app_ready_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"App-ready output must be a JSON object: {path}")
    if not isinstance(payload.get("questions"), list):
        raise ValueError(f"App-ready output missing questions array: {path}")
    return payload


def extract_text_for_density(input_path: Path) -> tuple[str, str]:
    """Return (extracted_text, dependency_error).

    Uses the same UWorld text extractor the downstream generator uses so
    the char count reflects actual textual content, not byte size of a
    compressed container. .docx files in particular are zipped XML where
    raw byte count can be 40× larger than the real text content — the
    old auto-density math saw a 57 KB .docx file as "80 questions worth"
    when the actual text was 1.4 KB.

    If python-docx / docx2txt / striprtf are missing, extract_text returns
    empty silently. Surface that as a dependency_error string so the
    runner can emit a precise failure instead of treating it as
    legitimately-empty content.
    """
    uworld_dir = Path(__file__).resolve().parent.parent / "uworld-notes-question-generator"
    sys.path.insert(0, str(uworld_dir))
    try:
        import generate_uworld_questions as _uw  # type: ignore
    finally:
        if sys.path and sys.path[0] == str(uworld_dir):
            sys.path.pop(0)

    suffix = input_path.suffix.lower()
    if suffix == ".docx" and not getattr(_uw, "DOCX_AVAILABLE", False):
        return "", "python-docx (or docx2txt) is required for .docx input. Install with: pip install --user python-docx"
    if suffix == ".rtf" and not getattr(_uw, "RTF_AVAILABLE", False):
        return "", "striprtf is required for .rtf input. Install with: pip install --user striprtf"

    try:
        text = _uw.extract_text(input_path)
    except Exception as exc:
        return "", f"text extraction raised: {exc}"
    return text or "", ""


def auto_questions_per_file(text_chars: int) -> int:
    """Convert a known char count to a question target.

    text_chars is the count of actual extracted text, NOT raw file bytes.
    Caller is responsible for the extraction so we don't repeat the
    parse twice.

    v4.63: ceiling lifted. The 80-question cap was a cost guard that
    prevented full coverage of dense high-yield UWorld notes (12+ KB
    files were getting 66-80 questions when the real target should have
    been 80-120+). The floor of 8 is preserved so short notes still get
    rigorous coverage. MAX_AUTO_QUESTIONS_PER_FILE is kept as a soft
    reference constant but is no longer enforced.
    """
    return max(MIN_AUTO_QUESTIONS_PER_FILE, text_chars // DEFAULT_CHARS_PER_QUESTION)


def run_uworld_generator(
    input_path: Path,
    mode: str,
    questions_per_file: int,
    v5_args: list[str] | None = None,
) -> dict[str, Any]:
    """Invoke generate_uworld_questions.py in either dry-run or live mode."""
    label = "dry-run" if mode == "dry-run" else "live"
    output_root = OUTPUT_DIR / f"uworld_app_ready_{label}" / input_path.stem.replace(" ", "_")
    generator_flag = "--dry-run" if mode == "dry-run" else "--generate"
    command = [
        sys.executable,
        str(UWORLD_GENERATOR),
        "--input-file",
        str(input_path),
        generator_flag,
        "--questions-per-file",
        str(questions_per_file),
        "--output-dir",
        str(output_root),
    ]
    # v5.8: append Advanced Mode (--v5) flags when supplied. Only the
    # caller's main() decides whether to populate this list, so this
    # function stays a no-op for legacy invocations.
    if v5_args:
        command.extend(v5_args)
    emit(
        "uworld_downstream_start",
        mode=mode,
        questionsPerFile=questions_per_file,
        command=command,
        outputRoot=str(output_root),
    )
    started_at = time.time()
    # v5.12.1: stream the child live and forward its per-question BIC_PROGRESS
    # lines so the BIC progress bar shows %/ETA in real time. The old
    # subprocess.run(PIPE) buffered all output and surfaced it only AFTER the
    # child exited, so the bar never moved — only run_pipeline_job's 60s
    # heartbeat showed. stderr is merged into stdout (single drain, no
    # pipe-buffer deadlock); non-progress lines are kept as a rolling tail for
    # the post-run report. BIC_PROGRESS_SOURCE is set explicitly so the child
    # emits progress even if the inherited value is empty.
    proc = subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "BIC_PROGRESS_SOURCE": os.environ.get("BIC_PROGRESS_SOURCE") or SOURCE_TYPE},
    )
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        message = line.rstrip("\n")
        if not message:
            continue
        if message.startswith("BIC_PROGRESS "):
            print(message, flush=True)
        else:
            tail.append(message)
            if len(tail) > 400:
                tail = tail[-200:]
    proc.wait()
    runtime = round(time.time() - started_at, 3)
    combined = "\n".join(tail).strip()
    if combined:
        emit("uworld_downstream_stdout", message=combined[-4000:])
    if proc.returncode != 0:
        raise RuntimeError(f"UWorld generator ({label}) exited with code {proc.returncode}.")

    app_ready_dir = output_root / "app_ready"
    app_ready_path = app_ready_dir / f"{input_path.stem}_app_ready.json"
    if not app_ready_path.exists():
        discovered = sorted(app_ready_dir.glob("*_app_ready.json")) if app_ready_dir.exists() else []
        if len(discovered) == 1:
            app_ready_path = discovered[0]
        else:
            raise FileNotFoundError(
                f"UWorld generator ({label}) did not produce expected app-ready JSON: {app_ready_path}"
            )
    payload = parse_app_ready_json(app_ready_path)
    schema_label = f"uworld-downstream-{label}-report-v1"
    report = {
        "schemaVersion": schema_label,
        "mode": mode,
        "outputRoot": str(output_root),
        "appReadyPath": str(app_ready_path),
        "schemaVersionObserved": payload.get("schemaVersion"),
        "sourceFormat": payload.get("sourceFormat"),
        "questionCount": len(payload.get("questions") or []),
        "questionsPerFile": questions_per_file,
        "runtimeSeconds": runtime,
        "exitCode": proc.returncode,
    }
    emit("uworld_downstream_complete", ok=True, report=report, outputs=[str(app_ready_path)])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit normalized UWorld notes text chunks through shared ingestion."
    )
    parser.add_argument("--input-file", required=True, help="UWorld notes file (.txt, .md, .rtf, .docx).")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Chunk limit for the normalization run. 0 (default, v4.59) = no cap.",
    )
    parser.add_argument("--chunk-output", default="")
    parser.add_argument(
        "--mode",
        choices=["dry-run", "generate"],
        default="dry-run",
        help=(
            "dry-run: produce placeholder app-ready JSON without calling Gemini. "
            "generate: call Gemini to produce real questions (requires GEMINI_API_KEY)."
        ),
    )
    parser.add_argument(
        "--questions-per-file",
        type=int,
        default=0,
        help=(
            "Target questions per file. 0 (default) = auto-scale to ~1 question per "
            f"{DEFAULT_CHARS_PER_QUESTION} chars for high-yield UWorld density."
        ),
    )
    # v5.8: Advanced Mode forwarding. Only takes effect with --mode generate.
    parser.add_argument(
        "--v5",
        action="store_true",
        help=(
            "Run the downstream UWorld generator with the v5.2 multi-stage "
            "organic pipeline. No-op in --mode dry-run."
        ),
    )
    parser.add_argument("--v5-order-mix", default="0.25,0.45,0.30")
    parser.add_argument("--v5-difficulty-mix", default="0.30,0.45,0.25")
    parser.add_argument("--v5-seed", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=0,
                        help="v5 only: forward chunk size cap (chars). 0 = downstream default.")
    parser.add_argument("--questions-per-chunk", type=int, default=0,
                        help="v5 only: forward Q-per-chunk. 0 = downstream default.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = args.mode
    input_path = Path(args.input_file).expanduser().resolve()
    if not input_path.exists():
        emit("uworld_profile_complete", ok=False, error=f"Input file not found: {input_path}")
        return 1

    # v4.59 follow-up: extract text once up front so the density calc uses
    # actual text chars (not .docx byte count, which overcounts ~40×) AND
    # so we can fail fast with an actionable error if extraction returned
    # empty because of a missing dependency (python-docx / striprtf).
    extracted_text, dep_error = extract_text_for_density(input_path)
    if dep_error:
        emit(
            "uworld_profile_complete",
            ok=False,
            error=dep_error,
            inputPath=str(input_path),
            hint="Missing Python dependency. Install it on the Python interpreter the .app subprocesses use (system python3 on macOS).",
        )
        return 1
    extracted_chars = len(extracted_text)
    if extracted_chars == 0 and input_path.stat().st_size > 100:
        emit(
            "uworld_profile_complete",
            ok=False,
            error=f"Extracted 0 chars of text from a non-empty {input_path.suffix} file. The file may be image-only, password-protected, or use an unsupported encoding.",
            inputPath=str(input_path),
            fileBytes=input_path.stat().st_size,
        )
        return 1

    questions_per_file = (
        int(args.questions_per_file)
        if args.questions_per_file > 0
        else auto_questions_per_file(extracted_chars)
    )

    limit = max(0, int(args.limit or 0))
    chunk_output = (
        Path(args.chunk_output).expanduser().resolve()
        if args.chunk_output
        else (OUTPUT_DIR / f"{input_path.stem.replace(' ', '_')}_uworld_normalized_chunks.json")
    )
    started_at = time.time()
    emit(
        "uworld_profile_start",
        inputPath=str(input_path),
        mode=mode,
        limit=limit,
        extractedChars=extracted_chars,
        questionsPerFile=questions_per_file,
        densityMode="auto" if args.questions_per_file == 0 else "explicit",
    )
    shared = run_shared_chunk_pipeline(
        source_type=SOURCE_TYPE,
        input_path=input_path,
        output_path=chunk_output,
        limit=limit,
    )
    report = shared["report"]
    emit(
        "uworld_normalized_chunks",
        outputPath=str(chunk_output),
        chunkCount=report.get("chunkCount", 0),
        stageTimings=report.get("stageTimings", {}),
        warnings=report.get("warnings", []),
        errors=report.get("errors", []),
    )
    if not report.get("ok"):
        emit(
            "uworld_profile_complete",
            ok=False,
            error="Shared normalized chunk validation failed.",
            report=report,
        )
        return 1

    # v5.8: build forwarded --v5 args when caller set --v5 on this runner.
    v5_args: list[str] = []
    if mode == "generate" and getattr(args, "v5", False):
        v5_args = [
            "--v5",
            "--v5-order-mix", str(args.v5_order_mix),
            "--v5-difficulty-mix", str(args.v5_difficulty_mix),
            "--v5-seed", str(args.v5_seed),
        ]
        if int(getattr(args, "chunk_size", 0) or 0) > 0:
            v5_args.extend(["--chunk-size", str(int(args.chunk_size))])
        if int(getattr(args, "questions_per_chunk", 0) or 0) > 0:
            v5_args.extend(["--questions-per-chunk", str(int(args.questions_per_chunk))])
        emit("uworld_v5_enabled", orderMix=args.v5_order_mix, difficultyMix=args.v5_difficulty_mix, seed=args.v5_seed)

    downstream_report: dict[str, Any] | None = None
    try:
        downstream_report = run_uworld_generator(input_path, mode, questions_per_file, v5_args=v5_args)
    except Exception as exc:
        final_report = {
            "schemaVersion": "uworld-profile-runner-report-v1",
            "sourceType": SOURCE_TYPE,
            "mode": mode,
            "inputPath": str(input_path),
            "normalizedChunkPath": str(chunk_output),
            "normalizedChunkCount": report.get("chunkCount", 0),
            "sharedStageTimings": report.get("stageTimings", {}),
            "questionsPerFile": questions_per_file,
            "downstreamStatus": "failed",
            "downstreamError": str(exc),
            "outputPaths": [],
            "warnings": report.get("warnings", []),
            "errors": [str(exc)],
            "totalRuntimeSeconds": round(time.time() - started_at, 3),
        }
        final_report["recovery"] = recovery_metadata(
            source_type=SOURCE_TYPE,
            outcome="failed_fatal",
            warnings=final_report["warnings"],
            fatal_errors=final_report["errors"],
        )
        emit("uworld_profile_complete", ok=False, error=str(exc), report=final_report, outputs=[])
        return 1

    downstream_status = f"{mode}-completed"
    final_report = {
        "schemaVersion": "uworld-profile-runner-report-v1",
        "sourceType": SOURCE_TYPE,
        "mode": mode,
        "inputPath": str(input_path),
        "normalizedChunkPath": str(chunk_output),
        "normalizedChunkCount": report.get("chunkCount", 0),
        "sharedStageTimings": report.get("stageTimings", {}),
        "questionsPerFile": questions_per_file,
        "downstreamStatus": downstream_status,
        "downstreamReport": downstream_report,
        "outputPaths": [downstream_report["appReadyPath"]] if downstream_report else [],
        "warnings": report.get("warnings", []),
        "errors": [],
        "totalRuntimeSeconds": round(time.time() - started_at, 3),
    }
    candidate_count = int((downstream_report or {}).get("questionCount") or 0)
    final_report["recovery"] = recovery_metadata(
        source_type=SOURCE_TYPE,
        outcome="completed",
        candidate_question_count=candidate_count,
        warnings=final_report["warnings"],
        survivors_import_safe=bool(downstream_report),
        retry_from_scratch_required=False,
    )
    emit(
        "uworld_profile_complete",
        ok=True,
        report=final_report,
        outputs=final_report["outputPaths"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
