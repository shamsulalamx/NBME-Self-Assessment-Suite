#!/usr/bin/env python3
"""
Run transcript-only Divine text inputs through shared normalized chunks.

By default this runner stops at transcript normalization. With
--emit-app-ready-dry-run it invokes the existing Divine generator in dry-run
mode with one selected transcript and a controlled output directory.
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
DIVINE_GENERATOR = PROJECT_ROOT / "tools" / "divine-audio-question-generator" / "generate_divine_questions.py"
SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md"}
SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav"}
SUPPORTED_EXTENSIONS = SUPPORTED_TEXT_EXTENSIONS | SUPPORTED_AUDIO_EXTENSIONS

sys.path.insert(0, str(SCRIPT_DIR))
from chunk_pipeline import run_shared_chunk_pipeline  # noqa: E402
from recovery_contract import recovery_metadata  # noqa: E402


SOURCE_TYPE = "divine_transcript"


def emit(event_type: str, **payload: Any) -> None:
    print(json.dumps({"type": event_type, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}, ensure_ascii=False), flush=True)


def selected_input(raw_path: str) -> Path:
    input_path = Path(raw_path).expanduser().resolve()
    if not input_path.exists():
        raise ValueError(f"--input-file does not exist: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"--input-file must be a file: {input_path}")
    if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"--input-file has unsupported extension '{input_path.suffix}'. Supported: {supported}")
    return input_path


def is_audio_input(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS


def parse_app_ready_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"App-ready output must be a JSON object: {path}")
    if not isinstance(payload.get("questions"), list):
        raise ValueError(f"App-ready output missing questions array: {path}")
    return payload


def run_divine_generator(
    input_path: Path,
    live: bool,
    v5_args: list[str] | None = None,
) -> dict[str, Any]:
    mode_subdir = "divine_app_ready_live" if live else "divine_app_ready_dry_run"
    output_root = OUTPUT_DIR / mode_subdir / input_path.stem.replace(" ", "_")
    mode_flag = "--generate" if live else "--dry-run"
    command = [
        sys.executable,
        str(DIVINE_GENERATOR),
        "--input-file",
        str(input_path),
        mode_flag,
        "--output-dir",
        str(output_root),
    ]
    # v5.8: Advanced Mode flag forwarding (live only).
    if v5_args and live:
        command.extend(v5_args)
    emit("divine_downstream_start", command=command, outputRoot=str(output_root), live=live)
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
        emit("divine_downstream_stdout", message=combined[-4000:])
    if proc.returncode != 0:
        mode_label = "live" if live else "dry-run"
        raise RuntimeError(f"Divine generator {mode_label} exited with code {proc.returncode}.")

    app_ready_path = output_root / "app_ready" / f"{input_path.stem}_app_ready.json"
    if not app_ready_path.exists():
        discovered = sorted((output_root / "app_ready").glob("*_app_ready.json")) if (output_root / "app_ready").exists() else []
        if len(discovered) == 1:
            app_ready_path = discovered[0]
        else:
            mode_label = "live" if live else "dry-run"
            raise FileNotFoundError(f"Divine generator {mode_label} did not produce expected app-ready JSON: {app_ready_path}")
    payload = parse_app_ready_json(app_ready_path)
    schema_version = (
        "divine-downstream-live-report-v1" if live else "divine-downstream-dry-run-report-v1"
    )
    report = {
        "schemaVersion": schema_version,
        "outputRoot": str(output_root),
        "appReadyOutputPath": str(app_ready_path),
        "observedSchemaVersion": payload.get("schemaVersion"),
        "observedSourceFormat": payload.get("sourceFormat"),
        "observedQuestionCount": len(payload["questions"]),
        "runtimeSeconds": runtime,
        "exitCode": proc.returncode,
        "live": live,
    }
    emit("divine_downstream_complete", ok=True, report=report, outputs=[str(app_ready_path)])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Divine transcript or audio inputs through Batch Import Center.")
    parser.add_argument("--input-file", required=True, help="Divine transcript (.txt, .md) or podcast audio (.mp3, .m4a, .wav).")
    parser.add_argument("--limit", type=int, default=0, help="Optional transcript block limit for text inputs. 0 emits all blocks.")
    parser.add_argument("--chunk-output", default="")
    parser.add_argument("--emit-app-ready-dry-run", action="store_true", help="After normalized chunk validation, invoke the Divine generator in dry-run mode. Text inputs only.")
    parser.add_argument("--emit-app-ready-live", action="store_true", help="Invoke the Divine generator in live mode (full transcribe + clean + question generation for audio; full generation for text). Requires GEMINI_API_KEY.")
    # v5.8: Advanced Mode forwarding. Only takes effect with --emit-app-ready-live.
    parser.add_argument(
        "--v5",
        action="store_true",
        help="Run the downstream Divine generator with the v5.2 multi-stage organic pipeline. No-op in dry-run.",
    )
    parser.add_argument("--v5-order-mix", default="0.25,0.45,0.30")
    parser.add_argument("--v5-difficulty-mix", default="0.30,0.45,0.25")
    parser.add_argument("--v5-seed", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--questions-per-chunk", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        input_path = selected_input(args.input_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2

    audio_input = is_audio_input(input_path)
    if audio_input and args.emit_app_ready_dry_run and not args.emit_app_ready_live:
        print(
            "ERROR: Audio inputs (.mp3/.m4a/.wav) require live mode. "
            "Re-run with --emit-app-ready-live in BIC 'generate' mode.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    limit = max(0, int(args.limit or 0))
    chunk_output = Path(args.chunk_output).expanduser().resolve() if args.chunk_output else (
        OUTPUT_DIR / f"{input_path.stem.replace(' ', '_')}_divine_transcript_normalized_chunks.json"
    )
    started_at = time.time()
    emit("divine_transcript_profile_start", inputFile=str(input_path), limit=limit, audioInput=audio_input)

    errors: list[str] = []
    warnings: list[str] = []
    chunk_types: list[str] = []
    chunk_count = 0
    stage_timings: dict[str, Any] = {}
    ok = True

    if not audio_input:
        shared = run_shared_chunk_pipeline(
            source_type=SOURCE_TYPE,
            input_path=input_path,
            output_path=chunk_output,
            limit=limit,
        )
        report = shared["report"]
        chunk_types = report.get("chunkTypes", [])
        chunk_count = report.get("chunkCount", 0)
        stage_timings = report.get("stageTimings", {})
        warnings = list(report.get("warnings", []))
        errors = list(report.get("errors", []))
        transcript_only = chunk_types == ["transcript"] or (not chunk_types and chunk_count == 0)
        if not transcript_only:
            errors.append(f"Divine transcript runner emitted non-transcript chunk types: {chunk_types}")
        ok = bool(report.get("ok")) and not errors
    else:
        emit(
            "divine_transcript_audio_input",
            message="Audio input detected; skipping shared chunk pipeline (chunks emerge after transcription).",
            inputFile=str(input_path),
        )

    # v5.8: build forwarded --v5 args when caller set --v5 + live mode.
    v5_args: list[str] = []
    if args.emit_app_ready_live and getattr(args, "v5", False):
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
        emit("divine_v5_enabled", orderMix=args.v5_order_mix, difficultyMix=args.v5_difficulty_mix, seed=args.v5_seed)

    downstream_report: dict[str, Any] | None = None
    live_requested = args.emit_app_ready_live
    if ok and live_requested:
        try:
            downstream_report = run_divine_generator(input_path, live=True, v5_args=v5_args)
        except Exception as exc:
            errors.append(str(exc))
            ok = False
    elif ok and args.emit_app_ready_dry_run:
        try:
            downstream_report = run_divine_generator(input_path, live=False, v5_args=v5_args)
        except Exception as exc:
            errors.append(str(exc))
            ok = False

    scope_label = (
        "audio → transcribe → clean → live questions" if audio_input
        else "transcript-first normalized chunks only"
    )
    if downstream_report:
        downstream_status = "live-completed" if live_requested else "dry-run-completed"
    elif live_requested or args.emit_app_ready_dry_run:
        downstream_status = "failed" if not ok else "not-requested"
    else:
        downstream_status = "not-requested"
    final_report = {
        "schemaVersion": "divine-transcript-profile-runner-report-v1",
        "sourceType": SOURCE_TYPE,
        "inputPath": str(input_path),
        "audioInput": audio_input,
        "normalizedChunkPath": None if audio_input else str(chunk_output),
        "normalizedChunkCount": chunk_count,
        "chunkTypes": chunk_types,
        "sharedStageTimings": stage_timings,
        "scope": scope_label,
        "downstreamStatus": downstream_status,
        "downstreamReport": downstream_report,
        "appReadyOutputPath": downstream_report["appReadyOutputPath"] if downstream_report else None,
        "observedSchemaVersion": downstream_report["observedSchemaVersion"] if downstream_report else None,
        "observedSourceFormat": downstream_report["observedSourceFormat"] if downstream_report else None,
        "observedQuestionCount": downstream_report["observedQuestionCount"] if downstream_report else None,
        "warnings": warnings,
        "errors": errors,
        "totalRuntimeSeconds": round(time.time() - started_at, 3),
    }
    final_report["recovery"] = recovery_metadata(
        source_type=SOURCE_TYPE,
        outcome="completed" if ok else "failed_fatal",
        candidate_question_count=int((downstream_report or {}).get("observedQuestionCount") or 0),
        warnings=final_report["warnings"],
        fatal_errors=errors,
        survivors_import_safe=bool(downstream_report),
        retry_from_scratch_required=not ok,
    )
    chunk_output_str = None if audio_input else str(chunk_output)
    emit("divine_transcript_normalized_chunks", outputPath=chunk_output_str, report=final_report)
    outputs: list[str] = []
    if chunk_output_str:
        outputs.append(chunk_output_str)
    if downstream_report:
        outputs.append(downstream_report["appReadyOutputPath"])
    emit("divine_transcript_profile_complete", ok=ok, report=final_report, outputs=outputs)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
