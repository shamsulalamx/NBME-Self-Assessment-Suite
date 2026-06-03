#!/usr/bin/env python3
"""Durable per-chunk result cache for resumable BIC generation.

Why this exists
---------------
BIC generators (e.g. the Fast Facts lecture-slide path) process their input in
chunks, calling Gemini once per chunk. Before this module, each chunk's
generated items lived only in memory until the *entire* run finished and the
final app-ready JSON was written. If the user quit the app (or the job was
interrupted) mid-run, every completed chunk was lost and a retry regenerated
everything from scratch -- wasting Gemini calls, time, and money. That is the
exact complaint this fixes: "I quit the app, the already worked-on chunks get
removed, and the job starts all over again."

This cache persists each completed chunk's items to disk *immediately*, under
the job's durable output root. That root survives quit + retry because BIC
reuses the same jobId on retry (see electron/main.js `retry-queue-job`, which
resets status but keeps `outputRoot`), and nothing ever deletes the job dir.
On retry the generator loads previously-finished chunks and skips the Gemini
call for them, regenerating only the chunks that never completed.

Design notes
------------
* **Disabled without a durable root.** Standalone runs (no BIC_JOB_OUTPUT_ROOT)
  construct this with ``root=None``; every load misses and every save is a
  no-op, so behaviour is byte-for-byte identical to the pre-cache generator.
* **Atomic writes.** Each entry is written to a temp file then ``os.replace``d
  into place, so a process kill mid-write can never leave a half-written entry
  that a later run would misread. (The in-generator ``write_json`` is NOT
  atomic, which is why this module does its own.)
* **Per-entry files**, not one big rewritten file: a corrupt/partial entry only
  costs that one chunk a regeneration, never the whole cache.
* **Corrupt / stale tolerant.** Any unreadable, schema-mismatched, or
  version-mismatched entry is treated as a miss (regenerate), never an error.
* **Fingerprint is the caller's responsibility.** The caller builds a payload
  dict of the *stable* inputs that determine a chunk's output (slide content
  hashes, allocation counts, prompt/validator/ontology versions, generator id)
  and hands it to :func:`fingerprint`. Bump ``CACHE_FORMAT_VERSION`` here to
  globally invalidate every entry if the on-disk record shape changes.
* **Never cache failures.** ``save`` refuses empty item lists, so a chunk that
  produced nothing is always re-attempted on retry rather than "resumed" empty.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

# Bump to invalidate ALL on-disk entries (e.g. if the record envelope changes).
CACHE_FORMAT_VERSION = 1
_ENTRY_SCHEMA = "bic-chunk-resume-entry-v1"
_CACHE_DIRNAME = "chunk_resume_cache"


def fingerprint(payload: Any) -> str:
    """Stable, short hex hash of a caller-built chunk-identity payload.

    Canonicalises with sorted keys so dict ordering never changes the result.
    Truncated to 24 hex chars: collision-safe for a single job's chunk count
    while keeping filenames short.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:24]


def _sanitize_namespace(namespace: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (namespace or ""))
    cleaned = cleaned.strip("._-")
    return cleaned or "default"


class ChunkResumeCache:
    """Durable per-chunk item store. Safe to construct with ``root=None``.

    Parameters
    ----------
    root:
        The durable job output root (``BIC_JOB_OUTPUT_ROOT``). ``None`` disables
        the cache entirely (standalone / non-BIC runs).
    namespace:
        Keeps different generators' entries from colliding under one job root.
        Defensive only -- fingerprints should already include a generator id.
    """

    def __init__(self, root: Any, namespace: str) -> None:
        self._dir = (
            Path(root).expanduser() / _CACHE_DIRNAME / _sanitize_namespace(namespace)
            if root
            else None
        )

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    @property
    def directory(self) -> Path | None:
        return self._dir

    def fingerprint(self, payload: Any) -> str:
        """Instance convenience wrapper around the module-level fingerprint."""
        return fingerprint(payload)

    def _path_for(self, fp: str) -> Path | None:
        if self._dir is None:
            return None
        # fp is lowercase hex from fingerprint(); strip to alnum anyway so a
        # caller-supplied key can never escape the cache dir via path chars.
        safe = "".join(c for c in str(fp) if c.isalnum()).lower()[:64]
        if not safe:
            return None
        return self._dir / (safe + ".json")

    def has(self, fp: str) -> bool:
        path = self._path_for(fp)
        return bool(path is not None and path.exists())

    def load(self, fp: str) -> list[dict[str, Any]] | None:
        """Return the cached items for ``fp``, or ``None`` on any kind of miss.

        A miss includes: disabled cache, missing file, unreadable/corrupt JSON,
        schema or format-version mismatch, or a fingerprint that doesn't match
        the record's own stored fingerprint. Never raises.
        """
        path = self._path_for(fp)
        if path is None:
            return None
        try:
            if not path.exists():
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            not isinstance(record, dict)
            or record.get("schema") != _ENTRY_SCHEMA
            or record.get("formatVersion") != CACHE_FORMAT_VERSION
            or record.get("fingerprint") != fp
            or not isinstance(record.get("items"), list)
        ):
            return None
        return record["items"]

    def save(self, fp: str, items: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> bool:
        """Atomically persist ``items`` for ``fp``. Returns True iff written.

        No-op (returns False) when the cache is disabled or ``items`` is empty
        -- callers must never cache an empty/failed chunk, so a retry re-attempts
        it. Persistence is best-effort: a failed write returns False rather than
        raising, so a disk hiccup can never break the generation run.
        """
        path = self._path_for(fp)
        if path is None:
            return False
        if not isinstance(items, list) or not items:
            return False
        record: dict[str, Any] = {
            "schema": _ENTRY_SCHEMA,
            "formatVersion": CACHE_FORMAT_VERSION,
            "fingerprint": fp,
            "itemCount": len(items),
            "items": items,
        }
        if isinstance(meta, dict):
            record["meta"] = meta
        tmp: Path | None = None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
            tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(path))
            return True
        except Exception:
            if tmp is not None:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
            return False
