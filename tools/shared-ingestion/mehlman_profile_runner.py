#!/usr/bin/env python3
"""
Mehlman PDF profile runner.

This keeps Mehlman as a profile:
shared-ingestion -> normalized text chunks -> existing Mehlman downstream generator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from chunk_pipeline import run_shared_chunk_pipeline
from recovery_contract import recovery_metadata


RUNNER_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = RUNNER_DIR.parents[1]
MEHLMAN_DIR = PROJECT_ROOT / "tools" / "mehlman-pdf-question-generator"
JOB_OUTPUT_ROOT = Path(os.environ["BIC_JOB_OUTPUT_ROOT"]).expanduser().resolve() if os.environ.get("BIC_JOB_OUTPUT_ROOT") else None
DOWNSTREAM_OUTPUT_ROOT = JOB_OUTPUT_ROOT / "mehlman-pdf-question-generator" if JOB_OUTPUT_ROOT else None
APP_READY_DIR = (
    DOWNSTREAM_OUTPUT_ROOT / "output_json" / "app_ready"
    if DOWNSTREAM_OUTPUT_ROOT else MEHLMAN_DIR / "output_json" / "app_ready"
)
NORMALIZED_OUTPUT_DIR = JOB_OUTPUT_ROOT / "shared-ingestion" if JOB_OUTPUT_ROOT else RUNNER_DIR / "output"


def emit(event_type: str, **payload: Any) -> None:
    print(json.dumps({"type": event_type, **payload}, ensure_ascii=False), flush=True)


def newest_app_ready_since(started_at: float) -> list[str]:
    if not APP_READY_DIR.exists():
        return []
    paths = [
        path for path in APP_READY_DIR.glob("*_app_ready.json")
        if path.is_file() and path.stat().st_mtime >= started_at - 1
    ]
    return [str(path.resolve()) for path in sorted(paths, key=lambda p: p.stat().st_mtime)]


def app_ready_question_count(outputs: list[str]) -> int:
    count = 0
    for output in outputs:
        try:
            payload = json.loads(Path(output).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("questions"), list):
            count += len(payload["questions"])
    return count


def run_existing_mehlman_generator(
    input_file: Path,
    mode: str,
    page_limit: int,
    v5_args: list[str] | None = None,
) -> tuple[int, float, list[str]]:
    started_at = time.time()
    # v4.58: chunk size retargeted to ~1.5K chars and questions-per-chunk
    # default lowered to 1 for one-fact-per-question tight focus. The profile
    # runner used to override to 2 from the pre-v4.58 8-12K chunk era;
    # the override is dropped so we inherit the generator's default and the
    # tight-focus contract holds end-to-end. page_limit == 0 means
    # "process every page" — we omit --max-pages entirely so the generator
    # runs across the full PDF (previously this wrapper capped at 10 pages
    # regardless of the actual document size, dropping content silently).
    command = [
        sys.executable,
        "generate_mehlman_questions.py",
        "--generate" if mode == "generate" else "--dry-run",
        "--input-file",
        str(input_file),
        "--extract-assets",
    ]
    if page_limit > 0:
        command.extend(["--max-pages", str(page_limit)])
    if DOWNSTREAM_OUTPUT_ROOT:
        command.extend(["--output-dir", str(DOWNSTREAM_OUTPUT_ROOT)])
    # v5.8: Advanced Mode flag-forwarding to the downstream Mehlman
    # generator. Note Mehlman uses `--v5-chunk-size` (not `--chunk-size`)
    # because its legacy CLI already owns `--questions-per-chunk`.
    if v5_args:
        command.extend(v5_args)
    emit("mehlman_downstream_start", command=command, cwd=str(MEHLMAN_DIR))
    proc = subprocess.Popen(
        command,
        cwd=str(MEHLMAN_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    # v4.74: parse the downstream generator's stdout for known progress
    # patterns and emit pipeline_progress events with proper counters.
    # Previously the runner only emitted mehlman_downstream_log events with
    # generic messages, which left the renderer with nothing better than
    # "shared ingestion still running after Xs" heartbeats. Now the floating
    # log shows things like "Generating questions from chunk 23/76 (page 12)".
    # v5.11: the downstream generator logs every line through uworld's log()
    # helper, which prepends a "[HH:MM:SS] " timestamp. The original
    # ^\s*-anchored patterns below therefore never matched (each line starts
    # with "["), so the per-chunk progress events silently never fired and the
    # BIC status bar got no counters for legacy (non-v5) Mehlman runs. Tolerate
    # an optional leading "[...]" stamp. Backward-compatible: the optional group
    # matches zero times when a line has no prefix.
    _TS = r"^\s*(?:\[[^\]]*\]\s*)?"
    _PAGE_TOTAL_RE = re.compile(_TS + r"(\d+)\s+pages\s+detected")
    _CHUNK_TOTAL_RE = re.compile(_TS + r"(\d+)\s+chunk\(s\)\s+→")
    _CHUNK_PROGRESS_RE = re.compile(_TS + r"Chunk\s+(\d+):\s+(\d+)\s+question\(s\)\s+generated")
    _STAGE_RE = re.compile(_TS + r"Stage\s+(\d+):\s+(.+)$")
    _EXTRACTING_RE = re.compile(r"Extracting pages from (.+)$")
    _APP_READY_RE = re.compile(r"App-ready → (\S+)\s+\((\d+)\s+questions\)")
    total_chunks = 0
    total_pages = 0
    cumulative_questions = 0
    for line in proc.stdout:
        message = line.rstrip("\n")
        if not message:
            continue
        # Always emit the raw log line so the verbose progress log keeps
        # everything (power users can dig in if needed).
        emit("mehlman_downstream_log", message=message)
        # Then try to extract a hyperspecific structured event on top of it.
        m = _CHUNK_PROGRESS_RE.match(message)
        if m:
            chunk_num = int(m.group(1))
            q_count = int(m.group(2))
            cumulative_questions += q_count
            payload = {
                "phase": "generating",
                "message": f"Generated {q_count} question(s) from chunk {chunk_num}" + (f"/{total_chunks}" if total_chunks else ""),
                "chunk": chunk_num,
                "question": cumulative_questions,
            }
            if total_chunks:
                payload["chunkTotal"] = total_chunks
                # Mehlman default is ~1 question per chunk so chunkTotal is a
                # reasonable estimate for questionTotal — gives the renderer's
                # dynamic-percent bar a denominator to compute against.
                payload["questionTotal"] = total_chunks
            emit("pipeline_progress", **payload)
            continue
        m = _CHUNK_TOTAL_RE.match(message)
        if m:
            total_chunks = int(m.group(1))
            emit(
                "pipeline_progress",
                phase="chunking",
                message=f"Chunked text into {total_chunks} chunks",
                chunkTotal=total_chunks,
            )
            continue
        m = _PAGE_TOTAL_RE.match(message)
        if m:
            total_pages = int(m.group(1))
            emit(
                "pipeline_progress",
                phase="extracting",
                message=f"Extracting text from {total_pages} pages",
                pageTotal=total_pages,
            )
            continue
        m = _EXTRACTING_RE.search(message)
        if m:
            emit(
                "pipeline_progress",
                phase="extracting",
                message=f"Extracting pages from {Path(m.group(1)).name}",
            )
            continue
        m = _STAGE_RE.match(message)
        if m:
            stage_num = m.group(1)
            stage_desc = m.group(2).strip()
            phase_label = {"1": "extracting", "2": "chunking", "3": "generating"}.get(stage_num, f"stage-{stage_num}")
            emit(
                "pipeline_progress",
                phase=phase_label,
                message=f"Stage {stage_num}: {stage_desc}",
            )
            continue
        m = _APP_READY_RE.search(message)
        if m:
            q_count = int(m.group(2))
            emit(
                "pipeline_progress",
                phase="writing",
                message=f"Wrote app-ready JSON with {q_count} questions",
                question=q_count,
                questionTotal=q_count,
            )
            continue
    code = proc.wait()
    runtime = round(time.time() - started_at, 3)
    return code, runtime, newest_app_ready_since(started_at)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Mehlman PDF through shared ingestion before existing generation.")
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--mode", choices=["dry-run", "generate"], default="dry-run")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="First-page limit. 0 (default, v4.58) = process every page; >0 = cap for validation runs.",
    )
    parser.add_argument("--chunk-output", default="")
    # v5.8: Advanced Mode forwarding. --v5-chunk-size (not --chunk-size)
    # because Mehlman's existing CLI already owns --questions-per-chunk.
    parser.add_argument(
        "--v5",
        action="store_true",
        help="Run the downstream Mehlman generator with the v5.2 multi-stage organic pipeline. No-op in --mode dry-run.",
    )
    parser.add_argument("--v5-order-mix", default="0.25,0.45,0.30")
    parser.add_argument("--v5-difficulty-mix", default="0.30,0.45,0.25")
    parser.add_argument("--v5-seed", type=int, default=0)
    parser.add_argument("--v5-chunk-size", type=int, default=0)
    parser.add_argument("--questions-per-chunk", type=int, default=0,
                        help="Reused by both Mehlman's legacy path (1-fact-per-chunk) and v5 (Q-per-chunk).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_file = Path(args.input_file).expanduser().resolve()
    # v4.58: 0 means "no cap" — pass straight through to the shared chunk
    # pipeline (which treats 0/None as unlimited) and the existing generator
    # (which drops --max-pages when 0).
    page_limit = max(0, int(args.limit or 0))
    chunk_output = Path(args.chunk_output).expanduser().resolve() if args.chunk_output else (
        NORMALIZED_OUTPUT_DIR / f"{input_file.stem.replace(' ', '_')}_mehlman_normalized_chunks.json"
    )
    started_at = time.time()

    emit("mehlman_profile_start", inputFile=str(input_file), mode=args.mode, pageLimit=page_limit)
    shared = run_shared_chunk_pipeline(
        source_type="mehlman_pdf",
        input_path=input_file,
        output_path=chunk_output,
        limit=page_limit,
    )
    report = shared["report"]
    emit(
        "mehlman_normalized_chunks",
        outputPath=str(chunk_output),
        chunkCount=report.get("chunkCount", 0),
        assetCount=report.get("assetCount", 0),
        imageRefCount=report.get("imageRefCount", 0),
        tableRefCount=report.get("tableRefCount", 0),
        stageTimings=report.get("stageTimings", {}),
        warnings=report.get("warnings", []),
        errors=report.get("errors", []),
    )
    if not report.get("ok"):
        emit("mehlman_profile_complete", ok=False, error="Shared normalized chunk validation failed.", report=report)
        return 1

    # v5.8: build forwarded --v5 args when caller set --v5. Mehlman reuses
    # --questions-per-chunk for both legacy and v5; --v5-chunk-size is
    # v5-only (legacy Mehlman doesn't take a chunk-size flag).
    v5_args: list[str] = []
    if args.mode == "generate" and getattr(args, "v5", False):
        v5_args = [
            "--v5",
            "--v5-order-mix", str(args.v5_order_mix),
            "--v5-difficulty-mix", str(args.v5_difficulty_mix),
            "--v5-seed", str(args.v5_seed),
        ]
        if int(getattr(args, "v5_chunk_size", 0) or 0) > 0:
            v5_args.extend(["--v5-chunk-size", str(int(args.v5_chunk_size))])
        if int(getattr(args, "questions_per_chunk", 0) or 0) > 0:
            v5_args.extend(["--questions-per-chunk", str(int(args.questions_per_chunk))])
        emit("mehlman_v5_enabled", orderMix=args.v5_order_mix, difficultyMix=args.v5_difficulty_mix, seed=args.v5_seed)

    code, downstream_runtime, outputs = run_existing_mehlman_generator(input_file, args.mode, page_limit, v5_args=v5_args)
    final_report = {
        "schemaVersion": "mehlman-profile-runner-report-v1",
        "sourceType": "mehlman_pdf",
        "mode": args.mode,
        "pageLimit": page_limit,
        "normalizedChunkPath": str(chunk_output),
        "normalizedChunkCount": report.get("chunkCount", 0),
        "assetCount": report.get("assetCount", 0),
        "imageRefCount": report.get("imageRefCount", 0),
        "tableRefCount": report.get("tableRefCount", 0),
        "sharedStageTimings": report.get("stageTimings", {}),
        "downstreamRuntimeSeconds": downstream_runtime,
        "totalRuntimeSeconds": round(time.time() - started_at, 3),
        "outputPaths": outputs,
        "warnings": report.get("warnings", []),
        "errors": report.get("errors", []) if code == 0 else [f"Existing Mehlman generator exited with code {code}"],
    }
    final_report["recovery"] = recovery_metadata(
        source_type="mehlman_pdf",
        outcome="completed" if code == 0 else "failed_fatal",
        candidate_question_count=app_ready_question_count(outputs),
        warnings=final_report["warnings"],
        fatal_errors=final_report["errors"],
        survivors_import_safe=code == 0 and bool(outputs),
        retry_from_scratch_required=code != 0,
    )
    emit("mehlman_profile_complete", ok=code == 0, report=final_report, outputs=outputs)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
