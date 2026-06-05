#!/usr/bin/env python3
"""
Run Anki plain-text exports through shared normalized chunk emission.

Supports two modes:
  --mode dry-run   (default) Invoke the existing Anki wrapper in dry-run mode,
                   producing placeholder app-ready JSON without calling Gemini.
  --mode generate  Invoke the existing Anki wrapper in live mode, calling
                   Gemini to produce real questions from the card content.
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
ANKI_WRAPPER = PROJECT_ROOT / "tools" / "anki-question-generator" / "generate_anki_questions.py"

sys.path.insert(0, str(SCRIPT_DIR))
from chunk_pipeline import run_shared_chunk_pipeline  # noqa: E402
from recovery_contract import recovery_metadata  # noqa: E402


SOURCE_TYPE = "anki_notes"


def emit(event_type: str, **payload: Any) -> None:
    payload = {"type": event_type, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
    print(json.dumps(payload), flush=True)


def parse_app_ready_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"App-ready output must be a JSON object: {path}")
    if not isinstance(payload.get("questions"), list):
        raise ValueError(f"App-ready output missing questions array: {path}")
    return payload


def run_anki_wrapper(
    input_path: Path,
    mode: str,
    v5_args: list[str] | None = None,
) -> dict[str, Any]:
    """
    Invoke generate_anki_questions.py in either dry-run or live mode.

    mode="dry-run"   -> passes --dry-run  (no Gemini calls, placeholder output)
    mode="generate"  -> passes --generate (live Gemini calls, real questions)
    v5_args            -> v5.8 Advanced Mode flags forwarded to downstream
    """
    label = "dry-run" if mode == "dry-run" else "live"
    output_root = OUTPUT_DIR / f"anki_app_ready_{label}" / input_path.stem.replace(" ", "_")
    generator_flag = "--dry-run" if mode == "dry-run" else "--generate"
    command = [
        sys.executable,
        str(ANKI_WRAPPER),
        "--input-file",
        str(input_path),
        generator_flag,
        "--output-dir",
        str(output_root),
    ]
    if v5_args:
        command.extend(v5_args)
    emit("anki_downstream_start", mode=mode, command=command, outputRoot=str(output_root))
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
        emit("anki_downstream_stdout", message=combined[-4000:])
    if proc.returncode != 0:
        raise RuntimeError(f"Anki wrapper ({label}) exited with code {proc.returncode}.")

    app_ready_path = output_root / "app_ready" / f"{input_path.stem}_app_ready.json"
    if not app_ready_path.exists():
        discovered = sorted((output_root / "app_ready").glob("*_app_ready.json")) if (output_root / "app_ready").exists() else []
        if len(discovered) == 1:
            app_ready_path = discovered[0]
        else:
            raise FileNotFoundError(f"Anki wrapper ({label}) did not produce expected app-ready JSON: {app_ready_path}")
    payload = parse_app_ready_json(app_ready_path)
    schema_label = f"anki-downstream-{label}-report-v1"
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
    emit("anki_downstream_complete", ok=True, report=report, outputs=[str(app_ready_path)])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Anki notes through shared-ingestion normalization.")
    parser.add_argument(
        "--mode",
        choices=["dry-run", "generate"],
        default="dry-run",
        help=(
            "dry-run: produce placeholder app-ready JSON without calling Gemini. "
            "generate: call Gemini to produce real questions (requires GEMINI_API_KEY)."
        ),
    )
    parser.add_argument("--input-file", required=True, help="Anki plain-text export file.")
    parser.add_argument("--limit", type=int, default=0, help="Chunk limit for the normalization run. 0 means all chunks.")
    parser.add_argument("--chunk-output", default="")
    # Backward-compatible alias for older registry/CLI invocations. Treated as
    # equivalent to --mode dry-run; explicit --mode generate wins if both set.
    parser.add_argument(
        "--emit-app-ready-dry-run",
        action="store_true",
        help="Alias for --mode dry-run (kept for backward compatibility).",
    )
    # v5.8: Advanced Mode forwarding. Only takes effect with --mode generate.
    parser.add_argument(
        "--v5",
        action="store_true",
        help="Run the downstream Anki generator with the v5.2 multi-stage organic pipeline. No-op in --mode dry-run.",
    )
    parser.add_argument("--v5-order-mix", default="0.25,0.45,0.30")
    parser.add_argument("--v5-difficulty-mix", default="0.30,0.45,0.25")
    parser.add_argument("--v5-seed", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--questions-per-chunk", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Resolve effective mode: explicit --mode takes precedence; the alias
    # --emit-app-ready-dry-run always means dry-run. If both are set
    # inconsistently, prefer the explicit --mode value and warn so it shows
    # up in the BIC log.
    mode = args.mode
    if args.emit_app_ready_dry_run and mode == "generate":
        emit("anki_profile_warning", message="--emit-app-ready-dry-run conflicts with --mode generate; using generate.")

    input_path = Path(args.input_file).expanduser().resolve()
    limit = max(0, int(args.limit or 0))
    chunk_output = Path(args.chunk_output).expanduser().resolve() if args.chunk_output else (
        OUTPUT_DIR / f"{input_path.stem.replace(' ', '_')}_anki_normalized_chunks.json"
    )
    started_at = time.time()
    emit("anki_profile_start", inputPath=str(input_path), mode=mode, limit=limit)
    shared = run_shared_chunk_pipeline(
        source_type=SOURCE_TYPE,
        input_path=input_path,
        output_path=chunk_output,
        limit=limit,
    )
    report = shared["report"]
    emit(
        "anki_normalized_chunks",
        outputPath=str(chunk_output),
        chunkCount=report.get("chunkCount", 0),
        stageTimings=report.get("stageTimings", {}),
        warnings=report.get("warnings", []),
        errors=report.get("errors", []),
    )
    if not report.get("ok"):
        emit("anki_profile_complete", ok=False, error="Shared normalized chunk validation failed.", report=report)
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
        emit("anki_v5_enabled", orderMix=args.v5_order_mix, difficultyMix=args.v5_difficulty_mix, seed=args.v5_seed)

    downstream_report: dict[str, Any] | None = None
    try:
        downstream_report = run_anki_wrapper(input_path, mode, v5_args=v5_args)
    except Exception as exc:
        final_report = {
            "schemaVersion": "anki-profile-runner-report-v1",
            "sourceType": SOURCE_TYPE,
            "mode": mode,
            "inputPath": str(input_path),
            "normalizedChunkPath": str(chunk_output),
            "normalizedChunkCount": report.get("chunkCount", 0),
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
        emit("anki_profile_complete", ok=False, error=str(exc), report=final_report, outputs=[])
        return 1

    downstream_status = f"{mode}-completed" if downstream_report else "not-requested"
    final_report = {
        "schemaVersion": "anki-profile-runner-report-v1",
        "sourceType": SOURCE_TYPE,
        "mode": mode,
        "inputPath": str(input_path),
        "normalizedChunkPath": str(chunk_output),
        "normalizedChunkCount": report.get("chunkCount", 0),
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
    emit("anki_profile_complete", ok=True, report=final_report, outputs=final_report["outputPaths"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
