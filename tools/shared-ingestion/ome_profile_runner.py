#!/usr/bin/env python3
"""
Run OME PDFs through shared normalized chunk emission.

Supports two modes:
  --mode dry-run   (default) Invoke the existing OME generator in dry-run mode,
                   producing placeholder app-ready JSON without calling Gemini.
  --mode generate  Invoke the existing OME generator in live mode, calling Gemini
                   to produce real questions from the PDF content.
                   Requires GEMINI_API_KEY in the environment.

The --emit-app-ready-dry-run flag is kept as a backward-compatible alias for
--mode dry-run so existing CLI invocations continue to work.
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
JOB_OUTPUT_ROOT = Path(os.environ["BIC_JOB_OUTPUT_ROOT"]).expanduser().resolve() if os.environ.get("BIC_JOB_OUTPUT_ROOT") else None
OUTPUT_DIR = JOB_OUTPUT_ROOT / "shared-ingestion" if JOB_OUTPUT_ROOT else SCRIPT_DIR / "output"
OME_GENERATOR = PROJECT_ROOT / "tools" / "ome-pdf-question-generator" / "generate_ome_questions.py"

sys.path.insert(0, str(SCRIPT_DIR))
from chunk_pipeline import run_shared_chunk_pipeline  # noqa: E402
from recovery_contract import recovery_metadata  # noqa: E402

SOURCE_TYPE = "ome_pdf"


def emit(event_type: str, **payload: Any) -> None:
    print(json.dumps({"type": event_type, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}, ensure_ascii=False), flush=True)


def parse_app_ready_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"App-ready output must be a JSON object: {path}")
    if not isinstance(payload.get("questions"), list):
        raise ValueError(f"App-ready output missing questions array: {path}")
    return payload


def run_ome_generator(
    input_path: Path,
    mode: str,
    v5_args: list[str] | None = None,
) -> dict[str, Any]:
    """
    Invoke generate_ome_questions.py in either dry-run or live mode.

    mode="dry-run"   -> passes --dry-run  (no Gemini calls, placeholder output)
    mode="generate"  -> passes --generate (live Gemini calls, real questions)

    v5_args lets the caller forward `--v5` and the related mix/seed flags
    so the downstream generator runs through the v5.2 multi-stage organic
    pipeline. The legacy single-call path stays the default when v5_args
    is empty (preserves pre-v5.3 BIC behavior).
    """
    label = "dry-run" if mode == "dry-run" else "live"
    output_root = OUTPUT_DIR / f"ome_app_ready_{label}" / input_path.stem.replace(" ", "_")
    generator_flag = "--dry-run" if mode == "dry-run" else "--generate"
    command = [
        sys.executable,
        str(OME_GENERATOR),
        "--input-file",
        str(input_path),
        generator_flag,
        "--output-dir",
        str(output_root),
    ]
    if v5_args:
        command.extend(v5_args)
    emit("ome_downstream_start", mode=mode, command=command, outputRoot=str(output_root))
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
        emit("ome_downstream_stdout", message=combined[-4000:])
    if proc.returncode != 0:
        raise RuntimeError(f"OME generator ({label}) exited with code {proc.returncode}.")

    app_ready_path = output_root / "app_ready" / f"{input_path.stem}_app_ready.json"
    if not app_ready_path.exists():
        discovered = sorted((output_root / "app_ready").glob("*_app_ready.json")) if (output_root / "app_ready").exists() else []
        if len(discovered) == 1:
            app_ready_path = discovered[0]
        else:
            raise FileNotFoundError(f"OME generator ({label}) did not produce expected app-ready JSON: {app_ready_path}")
    payload = parse_app_ready_json(app_ready_path)
    schema_label = f"ome-downstream-{label}-report-v1"
    report = {
        "schemaVersion": schema_label,
        "mode": mode,
        "outputRoot": str(output_root),
        "appReadyPath": str(app_ready_path),
        "schemaVersionObserved": payload.get("schemaVersion"),
        "sourceFormat": payload.get("sourceFormat"),
        "questionCount": len(payload.get("questions") or []),
        "runtimeSeconds": runtime,
        "exitCode": proc.returncode,
    }
    emit("ome_downstream_complete", ok=True, report=report, outputs=[str(app_ready_path)])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit normalized OME PDF text chunks through shared ingestion.")
    parser.add_argument("--input-file", required=True, help="OME text-layer PDF.")
    parser.add_argument("--limit", type=int, default=0, help="Chunk limit for the normalization run. 0 means all chunks.")
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
    # Backward-compatible alias kept so any existing CLI invocations still work.
    parser.add_argument(
        "--emit-app-ready-dry-run",
        action="store_true",
        help="Alias for --mode dry-run (kept for backward compatibility).",
    )
    # v5.3: forward v5 flags into the downstream OME generator. Only
    # applies when --mode generate. When unset, the downstream runs
    # through its legacy single-call path (pre-v5.3 BIC behavior).
    parser.add_argument(
        "--v5",
        action="store_true",
        help=(
            "Run the downstream OME generator with the v5.2 multi-stage "
            "organic pipeline (kernel → stem → distractors → critic → "
            "regen → length parity → assemble). No-op in --mode dry-run."
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
        help="v5 only: RNG seed for reproducible per-Q position randomization.",
    )
    # v5.6: forwarded to generate_ome_questions.py so the BIC UI can
    # control per-chunk character size and per-chunk question count.
    # Cap on total questions is removed — total = (eligible chunks) ×
    # (questions per chunk).
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=0,
        help="v5 only: forward chunk size cap (chars) to the downstream generator. 0 = generator default.",
    )
    parser.add_argument(
        "--questions-per-chunk",
        type=int,
        default=0,
        help="v5 only: forward questions-per-chunk to the downstream generator. 0 = generator default.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Resolve effective mode: explicit --mode takes precedence; --emit-app-ready-dry-run
    # is a backward-compatible alias that always means dry-run.
    mode = args.mode
    if args.emit_app_ready_dry_run and mode == "dry-run":
        pass  # consistent; no conflict
    elif args.emit_app_ready_dry_run and mode == "generate":
        emit("ome_profile_warning", message="--emit-app-ready-dry-run conflicts with --mode generate; using generate.")

    input_file = Path(args.input_file).expanduser().resolve()
    limit = max(0, int(args.limit or 0))
    chunk_output = Path(args.chunk_output).expanduser().resolve() if args.chunk_output else (
        OUTPUT_DIR / f"{input_file.stem.replace(' ', '_')}_ome_normalized_chunks.json"
    )
    started_at = time.time()
    emit("ome_profile_start", inputFile=str(input_file), mode=mode, limit=limit)
    shared = run_shared_chunk_pipeline(
        source_type=SOURCE_TYPE,
        input_path=input_file,
        output_path=chunk_output,
        limit=limit,
    )
    report = shared["report"]
    emit(
        "ome_normalized_chunks",
        outputPath=str(chunk_output),
        chunkCount=report.get("chunkCount", 0),
        stageTimings=report.get("stageTimings", {}),
        warnings=report.get("warnings", []),
        errors=report.get("errors", []),
    )
    if not report.get("ok"):
        emit("ome_profile_complete", ok=False, error="Shared normalized chunk validation failed.", report=report)
        return 1

    # Forward v5 args into the downstream generator only in generate mode.
    # v5 is a no-op for dry-run (the downstream's --dry-run path doesn't
    # call Gemini at all, so the multi-stage pipeline can't run).
    v5_args: list[str] = []
    if mode == "generate" and getattr(args, "v5", False):
        v5_args = [
            "--v5",
            "--v5-order-mix", str(args.v5_order_mix),
            "--v5-difficulty-mix", str(args.v5_difficulty_mix),
            "--v5-seed", str(args.v5_seed),
        ]
        # v5.6: forward chunk-size / questions-per-chunk only when
        # the caller actually set them (non-zero). The downstream's
        # own defaults handle the zero case.
        if int(getattr(args, "chunk_size", 0) or 0) > 0:
            v5_args.extend(["--chunk-size", str(int(args.chunk_size))])
        if int(getattr(args, "questions_per_chunk", 0) or 0) > 0:
            v5_args.extend(["--questions-per-chunk", str(int(args.questions_per_chunk))])
        emit(
            "ome_v5_enabled",
            orderMix=args.v5_order_mix,
            difficultyMix=args.v5_difficulty_mix,
            seed=args.v5_seed,
            chunkSize=int(getattr(args, "chunk_size", 0) or 0),
            questionsPerChunk=int(getattr(args, "questions_per_chunk", 0) or 0),
        )

    downstream_report: dict[str, Any] | None = None
    try:
        downstream_report = run_ome_generator(input_file, mode, v5_args=v5_args)
    except Exception as exc:
        final_report = {
            "schemaVersion": "ome-profile-runner-report-v1",
            "sourceType": SOURCE_TYPE,
            "mode": mode,
            "inputPath": str(input_file),
            "normalizedChunkPath": str(chunk_output),
            "normalizedChunkCount": report.get("chunkCount", 0),
            "chunkTypes": report.get("chunkTypes", []),
            "sharedStageTimings": report.get("stageTimings", {}),
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
        emit("ome_profile_complete", ok=False, error=str(exc), report=final_report, outputs=[])
        return 1

    downstream_status = f"{mode}-completed"
    final_report = {
        "schemaVersion": "ome-profile-runner-report-v1",
        "sourceType": SOURCE_TYPE,
        "mode": mode,
        "inputPath": str(input_file),
        "normalizedChunkPath": str(chunk_output),
        "normalizedChunkCount": report.get("chunkCount", 0),
        "chunkTypes": report.get("chunkTypes", []),
        "sharedStageTimings": report.get("stageTimings", {}),
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
    emit("ome_profile_complete", ok=True, report=final_report, outputs=final_report["outputPaths"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
