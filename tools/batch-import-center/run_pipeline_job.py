#!/usr/bin/env python3
"""
Batch Import Center Python runner.

Accepts a job manifest, resolves the registered source pipeline, emits newline
delimited JSON progress events, and discovers *_app_ready.json outputs.
"""

from __future__ import annotations

import json
import hashlib
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve()
while APP_ROOT.name not in {"app", "Resources"} and APP_ROOT.parent != APP_ROOT:
    APP_ROOT = APP_ROOT.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

SHARED_INGESTION_DIR = Path(__file__).parent.parent / "shared-ingestion"
sys.path.insert(0, str(SHARED_INGESTION_DIR.resolve()))
from recovery_contract import recovery_metadata  # noqa: E402


RUNNER_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = RUNNER_DIR.parents[1]
sys.path.insert(0, str(PROJECT_ROOT.resolve()))
from core.shared.domain_routing import ExecutionMode, PipelineRoute, classify_pipeline_route, determine_execution_mode  # noqa: E402
REGISTRY_PATH = RUNNER_DIR / "pipeline_registry.json"
STANDARD_STAGES = {
    "preflight",
    "extraction",
    "OCR",
    "chunking",
    "normalization",
    "generation",
    "validation",
    "app-ready conversion",
    "import",
    "completed",
}
UOGA_EVENT_PREFIX = "CH" + "UNK_"


def uoga_event(name: str) -> str:
    return UOGA_EVENT_PREFIX + name
CURRENT_PROC: subprocess.Popen[str] | None = None
CANCEL_REQUESTED = False
PROGRESS_PREFIX = "BIC_PROGRESS "
CHECKPOINT_VERSION = 1


def emit(event_type: str, **payload: Any) -> None:
    event = {
        "type": event_type,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **payload,
    }
    print(json.dumps(event, ensure_ascii=False), flush=True)


def parse_pipeline_progress(message: str) -> dict[str, Any] | None:
    if not message.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(message[len(PROGRESS_PREFIX):])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def handle_cancel_signal(signum: int, _frame: Any) -> None:
    global CANCEL_REQUESTED
    CANCEL_REQUESTED = True
    emit("job_cancelled", message=f"Cancellation signal received: {signum}")
    if CURRENT_PROC and CURRENT_PROC.poll() is None:
        try:
            CURRENT_PROC.terminate()
        except Exception:
            pass


signal.signal(signal.SIGTERM, handle_cancel_signal)
signal.signal(signal.SIGINT, handle_cancel_signal)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path.resolve())


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def checkpoint_path(manifest: dict[str, Any]) -> Path | None:
    output_root = job_output_root(manifest)
    return output_root / "checkpoint" / "job_checkpoint.json" if output_root else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def input_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    fingerprint = {
        "path": str(path.resolve()),
        "kind": "directory" if path.is_dir() else "file",
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
    }
    if path.is_file():
        fingerprint["sha256"] = sha256_file(path)
    return fingerprint


def checkpoint_command_fingerprint(source: dict[str, Any], manifest: dict[str, Any]) -> str:
    return sha256_json({
        "sourceType": manifest.get("sourceType"),
        "workingDirectory": source.get("workingDirectory"),
        "pythonExecutable": source.get("pythonExecutable") or "python3",
        "dryRun": bool(manifest.get("dryRun")),
        "executePipeline": bool(manifest.get("executePipeline")),
        "existingOutputValidation": bool(manifest.get("existingOutputValidation")),
        "steps": command_steps(source, bool(manifest.get("dryRun"))),
    })


def checkpoint_restart_reasons(
    existing: dict[str, Any] | None,
    source_type: str,
    inputs: list[dict[str, Any]],
    command_fingerprint: str,
    initial_reasons: list[str] | None = None,
) -> list[str]:
    reasons: list[str] = list(initial_reasons or [])
    if not existing:
        return reasons
    if existing.get("checkpointVersion") != CHECKPOINT_VERSION:
        reasons.append("checkpoint_version_changed")
    if existing.get("sourceType") != source_type:
        reasons.append("source_type_changed")
    if existing.get("inputs") != inputs:
        reasons.append("input_fingerprint_changed")
    generator = existing.get("generator")
    if not isinstance(generator, dict):
        reasons.append("checkpoint_generator_invalid")
    elif generator.get("commandFingerprint") != command_fingerprint:
        reasons.append("command_config_fingerprint_changed")
    if "stages" in existing and not isinstance(existing.get("stages"), dict):
        reasons.append("checkpoint_stages_invalid")
    if "resume" in existing and not isinstance(existing.get("resume"), dict):
        reasons.append("checkpoint_resume_invalid")
    return reasons


def initialize_checkpoint(manifest: dict[str, Any], source: dict[str, Any], inputs: list[Path]) -> dict[str, Any] | None:
    path = checkpoint_path(manifest)
    if not path:
        return None
    fingerprints = [input_fingerprint(path) for path in inputs]
    command_fingerprint = checkpoint_command_fingerprint(source, manifest)
    existing: dict[str, Any] | None = None
    initial_restart_reasons: list[str] = []
    if path.is_file():
        try:
            existing = load_json(path)
        except Exception as exc:
            initial_restart_reasons.append("checkpoint_manifest_unreadable")
            emit("checkpoint", message=f"Existing checkpoint could not be read and will not be reused: {exc}", checkpointPath=str(path))
    restart_reasons = checkpoint_restart_reasons(
        existing,
        str(manifest.get("sourceType") or ""),
        fingerprints,
        command_fingerprint,
        initial_restart_reasons,
    )
    existing_stages = existing.get("stages") if isinstance(existing, dict) and isinstance(existing.get("stages"), dict) else {}
    existing_last_safe_checkpoint = (
        existing.get("lastSafeCheckpoint", "")
        if isinstance(existing, dict) and isinstance(existing.get("lastSafeCheckpoint", ""), str)
        else ""
    )
    reuse_existing_stages = bool(existing) and not restart_reasons and existing.get("checkpointVersion") == CHECKPOINT_VERSION
    created_at = existing.get("createdAt") if reuse_existing_stages else utc_now_iso()
    checkpoint = {
        "checkpointVersion": CHECKPOINT_VERSION,
        "jobId": str(manifest.get("jobId") or ""),
        "sourceType": str(manifest.get("sourceType") or ""),
        "createdAt": created_at,
        "updatedAt": utc_now_iso(),
        "inputs": fingerprints,
        "generator": {
            "registrySourceType": str(manifest.get("sourceType") or ""),
            "workingDirectory": source.get("workingDirectory") or "",
            "commandFingerprint": command_fingerprint,
        },
        "stages": existing_stages if reuse_existing_stages else {},
        "lastSafeCheckpoint": existing_last_safe_checkpoint if reuse_existing_stages else "",
        "resume": {
            "eligible": reuse_existing_stages and bool(existing_last_safe_checkpoint),
            "lastSafeStage": existing_last_safe_checkpoint if reuse_existing_stages else "",
            "restartReasons": restart_reasons,
        },
    }
    write_json(path, checkpoint)
    emit(
        "checkpoint",
        message="Checkpoint initialized.",
        checkpointPath=str(path),
        lastSafeCheckpoint=checkpoint["lastSafeCheckpoint"],
        resumeEligible=checkpoint["resume"]["eligible"],
        restartReasons=restart_reasons,
    )
    return {"path": path, "checkpoint": checkpoint}


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = ["manifestVersion", "jobId", "sourceType", "inputs", "dryRun", "destination"]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"Manifest missing required field(s): {', '.join(missing)}")
    if manifest["manifestVersion"] != "batch-import-job-v1":
        raise ValueError("Unsupported manifestVersion")
    if not isinstance(manifest["inputs"], list) or not manifest["inputs"]:
        raise ValueError("Manifest inputs must be a non-empty array")
    if "requiresGemini" in manifest and not isinstance(manifest["requiresGemini"], bool):
        raise ValueError("Manifest requiresGemini must be true or false")
    if "existingOutputValidation" in manifest and not isinstance(manifest["existingOutputValidation"], bool):
        raise ValueError("Manifest existingOutputValidation must be true or false")
    if "outputRoot" in manifest and not isinstance(manifest["outputRoot"], str):
        raise ValueError("Manifest outputRoot must be a string")
    source_type = str(manifest.get("sourceType") or "")
    mode = determine_execution_mode(source_type)
    manifest["executionMode"] = mode.value


def get_source(registry: dict[str, Any], source_type: str) -> dict[str, Any]:
    source = registry.get("sources", {}).get(source_type)
    if not source:
        raise ValueError(f"Unregistered sourceType: {source_type}")
    if source.get("status") != "active":
        raise ValueError(f"Source type is not active: {source_type}")
    return source


def validate_inputs(manifest: dict[str, Any], source: dict[str, Any]) -> list[Path]:
    existing_output_validation = bool(manifest.get("existingOutputValidation"))
    allowed = {".json"} if existing_output_validation else {ext.lower() for ext in source.get("inputExtensions", [])}
    allow_directories = bool(source.get("allowDirectories")) and not existing_output_validation
    paths: list[Path] = []
    for item in manifest["inputs"]:
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not raw_path:
            raise ValueError("Each input must include path")
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Input file does not exist: {path}")
        if path.is_dir():
            if not allow_directories:
                raise ValueError(f"Input directories are not supported for this source: {path}")
            paths.append(path)
            continue
        if not path.is_file():
            raise ValueError(f"Input path is not a file: {path}")
        if allowed and path.suffix.lower() not in allowed:
            raise ValueError(f"Unsupported input extension for {path.name}: {path.suffix}")
        if existing_output_validation and not path.name.endswith("_app_ready.json"):
            raise ValueError(f"Existing-output validation requires *_app_ready.json: {path.name}")
        paths.append(path)
    return paths


def job_output_root(manifest: dict[str, Any]) -> Path | None:
    raw_path = str(manifest.get("outputRoot") or "").strip()
    if not raw_path:
        return None
    return Path(raw_path).expanduser().resolve()


def output_dirs(source: dict[str, Any], manifest: dict[str, Any]) -> list[Path]:
    cwd = (PROJECT_ROOT / source["workingDirectory"]).resolve()
    dirs = [(cwd / rel).resolve() for rel in source.get("outputDirectories", [])]
    durable_root = job_output_root(manifest)
    if durable_root:
        dirs.insert(0, durable_root)
    return dirs


def discover_outputs(source: dict[str, Any], manifest: dict[str, Any]) -> dict[str, float]:
    found: dict[str, float] = {}
    for out_dir in output_dirs(source, manifest):
        if not out_dir.exists():
            continue
        for path in out_dir.rglob("*_app_ready.json"):
            if path.is_file():
                found[str(path.resolve())] = path.stat().st_mtime
    return found


def discover_review_draft(manifest: dict[str, Any], started_at: float) -> dict[str, Any] | None:
    durable_root = job_output_root(manifest)
    if not durable_root:
        return None
    candidates: list[Path] = []
    manifest_draft = str(manifest.get("draftPath") or "").strip()
    if manifest_draft:
        manifest_path = Path(manifest_draft).expanduser().resolve()
        if durable_root.resolve() == manifest_path or durable_root.resolve() in manifest_path.parents:
            candidates.append(manifest_path)
    review_dir = durable_root / "review"
    if review_dir.is_dir():
        candidates.extend(sorted(review_dir.glob("*_review_draft.json")))
    for path in candidates:
        if not path.is_file() or path.stat().st_mtime < started_at - 1:
            continue
        try:
            draft = load_json(path)
        except Exception as exc:
            emit("warning", message=f"Review draft could not be read: {exc}", draftPath=str(path))
            continue
        candidate_questions = draft.get("candidateQuestions")
        survivors = draft.get("validQuestionIndexes")
        if (
            draft.get("draftVersion") != 1
            or draft.get("status") != "needs_review"
            or not isinstance(candidate_questions, list)
            or not candidate_questions
            or not isinstance(survivors, list)
        ):
            continue
        return {"path": str(path.resolve()), "draft": draft}
    return None


def expand_args(args: list[str], input_file: Path) -> list[str]:
    return [arg.replace("{inputFile}", str(input_file)) for arg in args]


def step_advanced_args(step: dict[str, Any]) -> list[str]:
    """Per-step advanced-mode CLI args appended when manifest.advancedMode is true.
    Empty list when the registry omits the field. Defined per-step (rather than
    per-source) so a multi-step pipeline can target the advanced flags at the
    specific generation step instead of every step in the chain."""
    advanced = step.get("advancedArgs")
    if isinstance(advanced, list):
        return [str(arg) for arg in advanced if isinstance(arg, str) and arg]
    return []


def manifest_advanced_config_args(manifest: dict[str, Any]) -> list[str]:
    """v5.6: append user-configurable knobs from manifest.advancedConfig.
    Currently OME-specific (chunkSize, questionsPerChunk) but the dispatcher
    is generic — each downstream runner ignores flags it doesn't recognize.
    Only emitted when the value is a positive int so unset/zero values fall
    back to the downstream generator's own defaults."""
    cfg = manifest.get("advancedConfig") or {}
    if not isinstance(cfg, dict):
        return []
    extra: list[str] = []
    chunk_size = cfg.get("chunkSize")
    if isinstance(chunk_size, int) and chunk_size > 0:
        extra.extend(["--chunk-size", str(chunk_size)])
    q_per_chunk = cfg.get("questionsPerChunk")
    if isinstance(q_per_chunk, int) and q_per_chunk > 0:
        extra.extend(["--questions-per-chunk", str(q_per_chunk)])
    return extra


def manifest_refined_enabled(manifest: dict[str, Any]) -> bool:
    """v5.10: Refined is an opt-in quality tier WITHIN Advanced Mode. It only
    applies when advancedMode is on (the v5 pipeline itself is gated behind
    advancedMode via the registry's --v5 advancedArgs) AND advancedConfig.refined
    is true. Engaged purely by exporting V5_REFINED=1 into the generator
    subprocess env (done in run_command): the v5 pipeline reads it via
    _default_mode(), and the var is inherited through the entire downstream
    spawn chain (profile runner -> generator), so no per-runner CLI flag
    plumbing is needed."""
    if not bool(manifest.get("advancedMode")):
        return False
    cfg = manifest.get("advancedConfig") or {}
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("refined"))


def command_steps(source: dict[str, Any], dry_run: bool) -> list[dict[str, Any]]:
    steps_key = "dryRunSteps" if dry_run else "liveSteps"
    args_key = "dryRunArgs" if dry_run else "liveArgs"
    steps = source.get(steps_key)
    if isinstance(steps, list) and steps:
        return [step for step in steps if isinstance(step, dict) and isinstance(step.get("args"), list)]
    args = source.get(args_key)
    if isinstance(args, list) and args:
        return [{"args": args}]
    raise ValueError(f"Registry source has no {steps_key}")


def normalize_stage(step: dict[str, Any]) -> str:
    stage = str(step.get("stage") or step.get("stageLabel") or "generation").strip()
    aliases = {
        "ocr": "OCR",
        "app-ready": "app-ready conversion",
        "app_ready": "app-ready conversion",
        "app-ready conversion": "app-ready conversion",
        "complete": "completed",
    }
    stage = aliases.get(stage.lower(), stage)
    if stage not in STANDARD_STAGES:
        stage = "generation"
    return stage


def run_command(source: dict[str, Any], manifest: dict[str, Any], input_file: Path, step: dict[str, Any], step_index: int, step_total: int) -> int:
    global CURRENT_PROC
    dry_run = bool(manifest.get("dryRun"))
    cwd = (PROJECT_ROOT / source["workingDirectory"]).resolve()
    base_args = expand_args(step["args"], input_file)
    # Advanced Mode: when the UI checkbox is on AND the step declared
    # advancedArgs in the registry, append those flags to the subprocess
    # command. For OME this appends "--v5" to engage the v5.2 multi-stage
    # organic pipeline downstream. No-op for sources that don't declare
    # advancedArgs, even when advancedMode=true on the manifest.
    if bool(manifest.get("advancedMode")):
        base_args.extend(step_advanced_args(step))
        base_args.extend(manifest_advanced_config_args(manifest))
    cmd = [source.get("pythonExecutable") or "python3", *base_args]
    stage_label = str(step.get("stageLabel") or f"Pipeline step {step_index}")
    stage = normalize_stage(step)
    stage_started_at = time.time()
    chunk_label = str(step.get("chunkLabel") or step.get("stageLabel") or f"pipeline_step_{step_index}")
    route = classify_pipeline_route(str(manifest.get("sourceType") or ""))
    mode = determine_execution_mode(str(manifest.get("sourceType") or ""))
    organic_route = mode == ExecutionMode.UOGA
    runner_chunk_events = False
    if runner_chunk_events:
        emit(
            "pipeline_progress",
            phase="planning",
            source=manifest.get("sourceType"),
            message=f"Chunk plan: {step_total} pipeline step(s)",
            chunkEvent=uoga_event("PLAN"),
            jobId=manifest.get("jobId"),
            totalChunks=step_total,
            totalQuestions=0,
            chunkAllocation=[1 for _ in range(step_total)],
            route=route.value,
            stage=stage,
            stageLabel=stage_label,
            stepIndex=step_index,
        )
        emit(
            "pipeline_progress",
            phase=stage,
            source=manifest.get("sourceType"),
            message=f"Chunk {step_index}/{step_total}: {stage_label} started",
            chunkEvent=uoga_event("START"),
            jobId=manifest.get("jobId"),
            chunkLabel=chunk_label,
            chunkIndex=step_index,
            totalChunks=step_total,
            allocatedQuestions=0,
            globalRetryId=1,
            retryPhase="initial",
            route=route.value,
            stage=stage,
            stageLabel=stage_label,
            stepIndex=step_index,
        )
    else:
        emit(
            "pipeline_progress",
            phase=stage,
            source=manifest.get("sourceType"),
            message=f"{stage_label} started",
            route=route.value,
            stage=stage,
            stageLabel=stage_label,
            stepIndex=step_index,
        )
    emit(
        "stage_start",
        message=f"{stage_label} started for {input_file.name}",
        stage=stage,
        stageLabel=stage_label,
        inputFile=str(input_file),
        stepIndex=step_index,
    )
    emit("command_start", cwd=str(cwd), command=cmd, stepIndex=step_index, stageLabel=stage_label)
    env = os.environ.copy()
    # v5.10: Refined quality tier. Exporting V5_REFINED=1 here is the ONLY
    # backend plumbing required — the v5 pipeline reads it via _default_mode()
    # and every spawn in the downstream chain inherits the parent env (the
    # profile runners pass env={**os.environ} or no env= at all), so the tier
    # rides along to generate_v5 without threading a flag through any runner.
    if manifest_refined_enabled(manifest):
        env["V5_REFINED"] = "1"
        emit("pipeline_progress", phase=stage, message="Refined mode ON (v5 cost/latency tier)", stage=stage, stageLabel=stage_label, stepIndex=step_index)
    durable_root = job_output_root(manifest)
    if durable_root:
        durable_root.mkdir(parents=True, exist_ok=True)
        env["BIC_JOB_OUTPUT_ROOT"] = str(durable_root)
        env["BIC_JOB_ID"] = str(manifest.get("jobId") or "")
        durable_checkpoint_path = checkpoint_path(manifest)
        if durable_checkpoint_path:
            env["BIC_CHECKPOINT_PATH"] = str(durable_checkpoint_path)
            try:
                checkpoint = load_json(durable_checkpoint_path)
            except Exception:
                checkpoint = {}
            command_fingerprint = (checkpoint.get("generator") or {}).get("commandFingerprint")
            if command_fingerprint:
                env["BIC_COMMAND_FINGERPRINT"] = str(command_fingerprint)
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    CURRENT_PROC = proc
    assert proc.stdout is not None
    last_lines: list[str] = []
    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    stream_done = False
    # v4.80: bumped 30s → 60s. The floating log was emitting two lines every
    # 30s (one stage_heartbeat + one pipeline_progress for the same message);
    # at 30s cadence on long Mehlman runs that's a wall of duplicate noise.
    # 60s cadence is still informative without overwhelming.
    heartbeat_seconds = 60.0
    next_heartbeat = time.time() + heartbeat_seconds if heartbeat_seconds > 0 else 0
    last_chunk_event: dict[str, Any] | None = None
    graph_job_complete = False
    pending_graph_complete: dict[str, Any] | None = None
    while not stream_done:
        if CANCEL_REQUESTED:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if not graph_job_complete and heartbeat_seconds > 0 and proc.poll() is None and time.time() >= next_heartbeat:
                duration = round(time.time() - stage_started_at, 2)
                emit(
                    "stage_heartbeat",
                    message=f"{stage_label} still running after {duration}s",
                    stage=stage,
                    stageLabel=stage_label,
                    durationSeconds=duration,
                    stepIndex=step_index,
                )
                if runner_chunk_events:
                    last_chunk_event = {
                        "chunkEvent": uoga_event("HEARTBEAT"),
                        "chunkLabel": chunk_label,
                        "chunkIndex": step_index,
                        "totalChunks": step_total,
                        "phase": stage,
                        "elapsedMs": int((time.time() - stage_started_at) * 1000),
                        "globalRetryId": 1,
                        "retryPhase": "initial",
                    }
                    emit(
                        "pipeline_progress",
                        source=manifest.get("sourceType"),
                        message=f"Chunk {step_index}/{step_total}: {stage_label} still running",
                        jobId=manifest.get("jobId"),
                        route=route.value,
                        stage=stage,
                        stageLabel=stage_label,
                        stepIndex=step_index,
                        **last_chunk_event,
                    )
                else:
                    emit(
                        "pipeline_progress",
                        source=manifest.get("sourceType"),
                        message=f"{stage_label} still running after {duration}s",
                        jobId=manifest.get("jobId"),
                        route=route.value,
                        phase=stage,
                        stage=stage,
                        stageLabel=stage_label,
                        stepIndex=step_index,
                        durationSeconds=duration,
                    )
                next_heartbeat = time.time() + heartbeat_seconds
            continue
        if line is None:
            stream_done = True
            continue
        message = line.rstrip("\n")
        if message:
            last_lines.append(message)
            last_lines = last_lines[-12:]
        progress = parse_pipeline_progress(message)
        if progress is not None:
            if progress.get("chunkEvent"):
                if not organic_route:
                    raise ValueError(
                        f"Legacy mode source emitted UOGA chunk telemetry: sourceType={manifest.get('sourceType')!r}"
                    )
                if str(manifest.get("sourceType") or "") == "fast_facts_pptx" and progress.get("executionGraph") is None:
                    raise RuntimeError("Execution" + "Graph required for UOGA fast_facts_pptx pipeline")
                if progress.get("executionGraph") is None:
                    raise ValueError(
                        f"UOGA chunk telemetry missing executionGraph: sourceType={manifest.get('sourceType')!r} event={progress.get('chunkEvent')!r}"
                    )
                last_chunk_event = progress
                if progress.get("chunkEvent") == uoga_event("HEARTBEAT"):
                    continue
                if progress.get("chunkEvent") == "JOB_COMPLETE" or progress.get("event") == "JOB_COMPLETE":
                    graph_job_complete = True
                    pending_graph_complete = progress
                    next_heartbeat = time.time() + heartbeat_seconds if heartbeat_seconds > 0 else 0
                    continue
            emit(
                "pipeline_progress",
                **{key: value for key, value in progress.items() if key not in {"type", "timestamp", "stage", "stageLabel", "stepIndex"}},
                stage=progress.get("stage") or stage,
                stageLabel=progress.get("stageLabel") or stage_label,
                stepIndex=step_index,
            )
            continue
        if not organic_route:
            emit("log", message=message, stageLabel=stage_label)
    code = proc.wait()
    CURRENT_PROC = None
    reader.join(timeout=1)
    duration = round(time.time() - stage_started_at, 2)
    if code == 0:
        if pending_graph_complete:
            emit(
                "pipeline_progress",
                **{key: value for key, value in pending_graph_complete.items() if key not in {"type", "timestamp", "stage", "stageLabel", "stepIndex"}},
                stage=pending_graph_complete.get("stage") or stage,
                stageLabel=pending_graph_complete.get("stageLabel") or stage_label,
                stepIndex=step_index,
            )
        if runner_chunk_events and not graph_job_complete:
            emit(
                "pipeline_progress",
                phase="finalizing",
                source=manifest.get("sourceType"),
                message=f"Chunk {step_index}/{step_total}: {stage_label} completed",
                chunkEvent=uoga_event("SUCCESS"),
                jobId=manifest.get("jobId"),
                chunkLabel=chunk_label,
                chunkIndex=step_index,
                totalChunks=step_total,
                globalRetryId=1,
                retryPhase="initial",
                elapsedMs=int((time.time() - stage_started_at) * 1000),
                route=route.value,
                stage=stage,
                stageLabel=stage_label,
                stepIndex=step_index,
            )
        emit(
            "stage_complete",
            message=f"{stage_label} completed in {duration}s",
            stage=stage,
            stageLabel=stage_label,
            durationSeconds=duration,
            stepIndex=step_index,
        )
    elif CANCEL_REQUESTED:
        emit(
            "stage_cancelled",
            message=f"{stage_label} cancelled after {duration}s",
            stage=stage,
            stageLabel=stage_label,
            durationSeconds=duration,
            exitCode=code,
            stepIndex=step_index,
        )
    else:
        reason = (
            next((line for line in reversed(last_lines) if line.strip().upper().startswith("ERROR:")), "")
            or next((line for line in reversed(last_lines) if line.strip()), f"Process exited with code {code}")
        )
        emit(
            "stage_failed",
            message=f"{stage_label} failed: {reason}",
            stage=stage,
            stageLabel=stage_label,
            durationSeconds=duration,
            exitCode=code,
            failureReason=reason,
            stepIndex=step_index,
        )
        if runner_chunk_events:
            emit(
                "pipeline_progress",
                phase="finalizing",
                source=manifest.get("sourceType"),
                message=f"Chunk {step_index}/{step_total}: {stage_label} dropped",
                chunkEvent=uoga_event("DROP"),
                jobId=manifest.get("jobId"),
                chunkLabel=chunk_label,
                chunkIndex=step_index,
                totalChunks=step_total,
                reason="pre_generation_failure" if not last_chunk_event else "stage_failed",
                raw_error=reason,
                retry_attempts=1,
                globalRetryId=1,
                retryPhase="drop",
                conceptIds=[],
                elapsedMs=int((time.time() - stage_started_at) * 1000),
                route=route.value,
                stage=stage,
                stageLabel=stage_label,
                stepIndex=step_index,
            )
    return code


def output_metrics(output_path: Path) -> dict[str, Any]:
    metrics = {
        "questionCount": 0,
        "imageTableCount": 0,
        "figureRefs": [],
        "warnings": [],
        "errors": [],
    }
    try:
        data = load_json(output_path)
    except Exception as exc:
        metrics["errors"].append(f"{rel(output_path)} could not be read: {exc}")
        return metrics
    questions = data.get("questions") if isinstance(data.get("questions"), list) else []
    metrics["questionCount"] = len(questions)
    figure_refs: list[str] = []
    image_table_count = 0
    warnings: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        for key in ("figureRefs", "images", "explanationImages", "tables", "explanationTables"):
            value = question.get(key)
            if isinstance(value, list):
                image_table_count += len(value)
                if key == "figureRefs":
                    for item in value:
                        if isinstance(item, dict):
                            ref_id = item.get("id") or item.get("figureId") or item.get("placeholder")
                            if ref_id:
                                figure_refs.append(str(ref_id))
                        elif item:
                            figure_refs.append(str(item))
        extraction_warnings = question.get("extractionWarnings")
        if isinstance(extraction_warnings, list):
            warnings.extend(str(item) for item in extraction_warnings if item)
    metrics["imageTableCount"] = image_table_count
    metrics["figureRefs"] = sorted(set(figure_refs))
    metrics["warnings"] = warnings[:50]
    return metrics


def completion_report(
    manifest: dict[str, Any],
    source: dict[str, Any],
    run_started_at: float,
    outputs: list[str],
    source_summary: dict[str, Any] | None,
    review_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    question_count = 0
    image_table_count = 0
    figure_refs: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    for output in outputs:
        metrics = output_metrics(Path(output))
        question_count += int(metrics["questionCount"])
        image_table_count += int(metrics["imageTableCount"])
        figure_refs.extend(metrics["figureRefs"])
        warnings.extend(metrics["warnings"])
        errors.extend(metrics["errors"])
    destination = manifest.get("destination")
    target_folder = destination.get("folderId") if isinstance(destination, dict) else ""
    report = {
        "schemaVersion": "batch-import-completion-report-v1",
        "jobId": manifest.get("jobId"),
        "sourceType": manifest.get("sourceType"),
        "targetFolder": target_folder or "",
        "outputRoot": str(job_output_root(manifest) or ""),
        "runtimeSeconds": round(time.time() - run_started_at, 2),
        "questionCount": question_count,
        "imageTableCount": image_table_count,
        "figureRefs": sorted(set(figure_refs)),
        "cacheHits": (source_summary or {}).get("cacheHits"),
        "cacheMisses": (source_summary or {}).get("cacheMisses"),
        "warnings": warnings[:50],
        "errors": errors[:50],
        "outputJsonPath": outputs[0] if outputs else "",
        "outputPaths": outputs,
        "importedTestId": None,
        "importedTestName": None,
        "stage": "completed",
    }
    if isinstance(review_draft, dict) and isinstance(review_draft.get("draft"), dict):
        draft = review_draft["draft"]
        healthy_count = question_count
        review_count = len(draft.get("candidateQuestions") or [])
        report.update({
            "status": "completed_with_review_required",
            "stage": "completed_with_review_required",
            "draftPath": str(review_draft.get("path") or ""),
            "questionCount": healthy_count,
            "reviewRequiredQuestionCount": review_count,
            "allocatedQuestionCount": healthy_count + review_count,
            "warnings": [str(item) for item in draft.get("validationWarnings") or []][:50],
            "errors": [str(item) for item in draft.get("validationErrors") or []][:50],
        })
        report["recovery"] = recovery_metadata(
            source_type=str(manifest.get("sourceType") or ""),
            outcome="needs_review",
            candidate_question_count=review_count,
            surviving_question_count=len(draft.get("validQuestionIndexes") or []),
            warnings=report["warnings"],
            review_items=draft.get("reviewItems") or [],
            survivors_import_safe=True,
            retry_from_scratch_required=False,
            resume_checkpoint_safe_later=True,
        )
    else:
        completed_outcome = "failed_fatal" if report["errors"] else "completed"
        report["recovery"] = recovery_metadata(
            source_type=str(manifest.get("sourceType") or ""),
            outcome=completed_outcome,
            candidate_question_count=question_count,
            warnings=report["warnings"],
            fatal_errors=report["errors"],
            survivors_import_safe=bool(outputs and not report["errors"]),
            retry_from_scratch_required=False,
        )
    return report


def command_failure_report(
    manifest: dict[str, Any],
    run_started_at: float,
    step_index: int,
    exit_code: int,
) -> dict[str, Any]:
    error = f"Pipeline step {step_index} exited with code {exit_code}."
    destination = manifest.get("destination")
    target_folder = destination.get("folderId") if isinstance(destination, dict) else ""
    report = {
        "schemaVersion": "batch-import-completion-report-v1",
        "jobId": manifest.get("jobId"),
        "sourceType": manifest.get("sourceType"),
        "targetFolder": target_folder or "",
        "outputRoot": str(job_output_root(manifest) or ""),
        "runtimeSeconds": round(time.time() - run_started_at, 2),
        "questionCount": 0,
        "imageTableCount": 0,
        "figureRefs": [],
        "warnings": [],
        "errors": [error],
        "outputJsonPath": "",
        "outputPaths": [],
        "importedTestId": None,
        "importedTestName": None,
        "stage": "failed",
        "status": "failed",
    }
    report["recovery"] = recovery_metadata(
        source_type=str(manifest.get("sourceType") or ""),
        outcome="failed_fatal",
        fatal_errors=report["errors"],
    )
    return report


def newest_json_report(source: dict[str, Any], manifest: dict[str, Any], pattern: str, started_at: float) -> Path | None:
    cwd = (PROJECT_ROOT / source["workingDirectory"]).resolve()
    reports_dirs = [cwd / "reports"]
    durable_root = job_output_root(manifest)
    if durable_root:
        reports_dirs.insert(0, durable_root / "lecture-slide-question-generator" / "reports")
    candidates = []
    for reports_dir in reports_dirs:
        if not reports_dir.exists():
            continue
        candidates.extend(
            path for path in reports_dir.glob(pattern)
            if path.is_file() and path.stat().st_mtime >= started_at - 1
        )
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def emit_source_summary(source_type: str, source: dict[str, Any], manifest: dict[str, Any], started_at: float) -> dict[str, Any]:
    if source_type != "fast_facts_pptx":
        return {}
    report_path = newest_json_report(source, manifest, "fast_facts_generation_validation_report_*.json", started_at)
    if not report_path:
        emit("cache_summary", message="Fast Facts cache summary unavailable; validation report not produced.")
        return {}
    try:
        report = load_json(report_path)
    except Exception as exc:
        emit("cache_summary", message=f"Fast Facts cache summary unavailable: {exc}")
        return {}
    cache_report = report.get("cacheReport") if isinstance(report.get("cacheReport"), dict) else {}
    hits = int(cache_report.get("cacheHits") or 0)
    misses = int(cache_report.get("cacheMisses") or 0)
    regenerated = int(cache_report.get("regeneratedQuestions") or 0)
    dropped = int(cache_report.get("droppedQuestions") or 0)
    emit(
        "cache_summary",
        message=f"Fast Facts cache hits: {hits}; misses: {misses}; regenerated: {regenerated}; dropped: {dropped}.",
        reportPath=rel(report_path),
        cacheHits=hits,
        cacheMisses=misses,
        regeneratedQuestions=regenerated,
        droppedQuestions=dropped,
        runtimeDurationSeconds=cache_report.get("runtimeDurationSeconds"),
    )
    return {"cacheHits": hits, "cacheMisses": misses}


def main() -> int:
    if len(sys.argv) != 2:
        emit("error", message="Usage: run_pipeline_job.py /path/to/manifest.json")
        return 2

    manifest_path = Path(sys.argv[1]).expanduser().resolve()
    try:
        manifest = load_json(manifest_path)
        registry = load_json(REGISTRY_PATH)
        validate_manifest(manifest)
        source = get_source(registry, manifest["sourceType"])
        inputs = validate_inputs(manifest, source)
        initialize_checkpoint(manifest, source, inputs)

        preflight_started_at = time.time()
        emit("stage_start", stage="preflight", stageLabel="preflight", message="preflight started")
        emit(
            "job_start",
            jobId=manifest["jobId"],
            sourceType=manifest["sourceType"],
            dryRun=bool(manifest.get("dryRun")),
            inputCount=len(inputs),
            outputRoot=str(job_output_root(manifest) or ""),
        )
        emit(
            "stage_complete",
            stage="preflight",
            stageLabel="preflight",
            message="preflight completed",
            durationSeconds=round(time.time() - preflight_started_at, 2),
        )

        run_started_at = time.time()
        before = discover_outputs(source, manifest)
        execute_pipeline = bool(manifest.get("executePipeline"))
        existing_output_validation = bool(manifest.get("existingOutputValidation"))
        if existing_output_validation:
            emit(
                "validation_mode",
                message="Existing app-ready output validation mode enabled; generation skipped.",
                outputs=[str(path) for path in inputs],
            )
        elif manifest.get("dryRun") and not execute_pipeline:
            emit("dry_run", message="Validated manifest and registry. Pipeline execution skipped by dry-run.")
        else:
            steps = command_steps(source, bool(manifest.get("dryRun")))
            for input_file in inputs:
                for step_index, step in enumerate(steps, start=1):
                    if CANCEL_REQUESTED:
                        emit(
                            "job_complete",
                            jobId=manifest["jobId"],
                            ok=False,
                            cancelled=True,
                            dryRun=bool(manifest.get("dryRun")),
                            outputs=[],
                            error="Job cancelled.",
                        )
                        return 130
                    code = run_command(source, manifest, input_file, step, step_index, len(steps))
                    if CANCEL_REQUESTED:
                        emit(
                            "job_complete",
                            jobId=manifest["jobId"],
                            ok=False,
                            cancelled=True,
                            dryRun=bool(manifest.get("dryRun")),
                            outputs=[],
                            error="Job cancelled.",
                        )
                        return 130
                    if code != 0:
                        failure_report = command_failure_report(manifest, run_started_at, step_index, code)
                        emit(
                            "command_failed",
                            message=f"Pipeline step {step_index} exited with code {code}. See stage_failed for the explicit failure reason.",
                            exitCode=code,
                            stepIndex=step_index,
                        )
                        emit(
                            "job_complete",
                            jobId=manifest["jobId"],
                            ok=False,
                            dryRun=bool(manifest.get("dryRun")),
                            outputs=[],
                            report=failure_report,
                            error=f"Pipeline step {step_index} exited with code {code}.",
                        )
                        return code

        source_summary = emit_source_summary(manifest["sourceType"], source, manifest, run_started_at)
        after = discover_outputs(source, manifest)
        if existing_output_validation:
            discovered = set(after)
            selected = [str(path.resolve()) for path in inputs]
            outputs = selected[:]
            external = [path for path in selected if path not in discovered]
            if external:
                emit(
                    "warning",
                    message="Selected app-ready output was not in the packaged registered output directories; using explicit existing-output selection after discovery.",
                    externalOutputs=external,
                )
        else:
            new_outputs = {path for path in after if path not in before}
            touched_outputs = {
                path for path, modified_at in after.items()
                if modified_at >= run_started_at - 1
            }
            outputs = sorted(new_outputs | touched_outputs)
        emit("outputs_discovered", outputs=outputs, count=len(outputs))
        review_draft = discover_review_draft(manifest, run_started_at)
        report = completion_report(manifest, source, run_started_at, outputs, source_summary, review_draft)
        emit(
            "stage_complete",
            stage="completed",
            stageLabel="completed",
            message="job completed",
            durationSeconds=report["runtimeSeconds"],
        )
        emit(
            "job_complete",
            jobId=manifest["jobId"],
            ok=True,
            dryRun=bool(manifest.get("dryRun")),
            outputs=outputs,
            report=report,
        )
        return 0
    except Exception as exc:
        emit("job_complete", ok=False, error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
