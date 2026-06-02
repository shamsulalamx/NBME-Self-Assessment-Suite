#!/usr/bin/env python3
"""License-clean external medical image sourcing for v5 Advanced Mode.

When the v5 organic generator decides a question would benefit from an
image-interpretation finding (ECG, CXR, CT, MRI, smear, histology, derm,
fundus, ultrasound, gross specimen) and the SOURCE document supplied no
suitable figure, this module borrows ONE real, published image from an
open-access library.

Hard safety rules (this is a high-trust-cost area):
  * NEVER fetch a URL produced by a language model. The model only emits a
    TEXT search query; the real image URL comes back from a provider's
    official search API.
  * Only http(s) URLs on an allow-listed host are downloaded.
  * Every candidate must carry a machine-readable, reuse-permissive license
    (public domain / CC0 / CC BY / CC BY-SA). NC / ND / unknown are dropped.
  * Downloads are validated by magic-bytes + size before use.
  * Results are cached by (query, modality) so repeat runs are deterministic
    and offline-friendly.
  * Any failure (network, parse, validation) degrades to ``None`` — the
    question simply gets no image. Never raises into the pipeline.

The HTTP layer is injectable (``transport``) so the whole module is unit
testable offline with fixtures.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = SCRIPT_DIR / "output_json" / "external_image_cache"

DEFAULT_TIMEOUT = float(os.environ.get("V5_EXTERNAL_TIMEOUT", "8") or "8")
DEFAULT_UA = (
    os.environ.get("V5_EXTERNAL_UA", "").strip()
    or "MedQGenBot/1.0 (educational, non-commercial study tool)"
)

MIN_IMAGE_BYTES = 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Image download is restricted to these hosts (defense in depth: even though
# URLs come from official APIs, never follow one onto an unexpected host).
_ALLOWED_IMAGE_HOSTS = (
    "upload.wikimedia.org",
    "commons.wikimedia.org",
)

# Transport contract: (url, *, headers, timeout) -> (status, content_type, body_bytes)
Transport = Callable[..., "tuple[int, str, bytes]"]


# ── HTTP ────────────────────────────────────────────────────────────────────
def _urllib_transport(url: str, *, headers: dict[str, str] | None = None,
                      timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (host allow-listed)
                status = int(getattr(resp, "status", 0) or resp.getcode() or 0)
                ctype = resp.headers.get("Content-Type", "") or ""
                body = resp.read(MAX_IMAGE_BYTES + 1)
            return status, ctype, body
        except urllib.error.HTTPError as exc:
            # Questions generate concurrently (ThreadPoolExecutor), so the
            # Wikimedia API can return 429/503 in a burst. One polite backoff
            # clears it; anything else propagates to degrade-to-None.
            if exc.code in (429, 503) and attempt <= 2:
                time.sleep(1.5 * attempt)
                continue
            raise


def _is_http_url(u: str) -> bool:
    try:
        p = urllib.parse.urlparse(u or "")
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _host_allowed(u: str) -> bool:
    host = urllib.parse.urlparse(u or "").netloc.lower()
    return any(host == h or host.endswith("." + h) for h in _ALLOWED_IMAGE_HOSTS)


# ── Validation ────────────────────────────────────────────────────────────────
def _sniff_mime(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_image(data: bytes) -> str | None:
    if not data or not (MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
        return None
    return _sniff_mime(data)


# ── License gate ──────────────────────────────────────────────────────────────
_REJECT_LICENSE_HINTS = (
    "by-nc", "-nc-", "-nc ", "noncommercial", "non-commercial",
    "by-nd", "-nd-", "-nd ", "noderiv", "no deriv",
    "fair use", "non-free", "all rights reserved", "copyright",
)
_ACCEPT_LICENSE_HINTS = (
    "public domain", "cc0", "cc-zero", "cc by", "cc-by",
    "pdm", "no restrictions", "attribution",
)


def _license_ok(license_str: str | None) -> bool:
    s = (license_str or "").lower()
    if not s:
        return False
    if any(bad in s for bad in _REJECT_LICENSE_HINTS):
        return False
    return any(good in s for good in _ACCEPT_LICENSE_HINTS)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", s or "")).strip()


# ── Encoding + cache ──────────────────────────────────────────────────────────
def _to_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _cache_key(query: str, modality: str) -> str:
    raw = f"v1|{query.strip().lower()}|{(modality or '').strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_load(cache_dir: Path, key: str) -> dict[str, Any] | None:
    meta_p = cache_dir / f"{key}.json"
    img_p = cache_dir / f"{key}.img"
    if not (meta_p.is_file() and img_p.is_file()):
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        data = img_p.read_bytes()
    except (OSError, ValueError):
        return None
    mime = meta.get("mimeType") or _sniff_mime(data) or "image/png"
    meta["dataUrl"] = _to_data_url(data, mime)
    meta["cacheHit"] = True
    return meta


def _cache_store(cache_dir: Path, key: str, data: bytes, meta: dict[str, Any]) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{key}.img").write_bytes(data)
        persisted = {k: v for k, v in meta.items() if k not in ("dataUrl", "cacheHit")}
        (cache_dir / f"{key}.json").write_text(
            json.dumps(persisted, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[v5-ext] cache write failed ({exc}); continuing without cache.",
              file=sys.stderr)


# ── Providers (return candidate dicts; NO downloading here) ───────────────────
def _wikimedia_search(srch: str, tx: Transport, timeout: float) -> list[dict[str, Any]]:
    """One Commons search → candidate dicts (no downloading)."""
    api = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"{srch} filetype:bitmap",
        "gsrnamespace": "6",
        "gsrlimit": "8",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "1024",
    })
    status, _ctype, body = tx(api, headers={"User-Agent": DEFAULT_UA,
                                            "Accept": "application/json"}, timeout=timeout)
    if status != 200:
        return []
    data = json.loads(body.decode("utf-8", "replace"))
    pages = ((data.get("query") or {}).get("pages") or {})
    candidates: list[dict[str, Any]] = []
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        ext = info.get("extmetadata") or {}
        lic = (
            (ext.get("LicenseShortName") or {}).get("value")
            or (ext.get("License") or {}).get("value")
            or (ext.get("UsageTerms") or {}).get("value")
            or ""
        )
        artist = _strip_html((ext.get("Artist") or {}).get("value") or "")
        credit = _strip_html((ext.get("Credit") or {}).get("value") or "")
        img_url = info.get("thumburl") or info.get("url") or ""
        candidates.append({
            "imageUrl": img_url,
            "pageUrl": info.get("descriptionurl") or "",
            "title": page.get("title", ""),
            "license": lic,
            "attribution": artist or credit or page.get("title", ""),
            "sourceName": "Wikimedia Commons",
        })
    return candidates


def _query_ladder(query: str, modality: str) -> list[str]:
    """Full query first, then progressively shorter prefixes. Commons search
    ANDs every term, so a long/hyper-specific query ("electrocardiogram inferior
    STEMI ST elevation II III aVF") matches ZERO files. The model is prompted to
    put the diagnosis/finding FIRST and the modality LAST, so prefixes preserve
    the most distinctive terms while widening the match."""
    base = query
    if modality and modality.lower() not in query.lower() and modality.lower() != "none":
        base = f"{query} {modality}"
    terms = base.split()
    ladder = [base]
    for n in (4, 3, 2):
        if len(terms) > n:
            ladder.append(" ".join(terms[:n]))
    seen: set[str] = set()
    return [s for s in ladder if not (s in seen or seen.add(s))]


def _provider_wikimedia(query: str, modality: str, tx: Transport,
                        timeout: float) -> list[dict[str, Any]]:
    for srch in _query_ladder(query, modality):
        candidates = _wikimedia_search(srch, tx, timeout)
        if candidates:
            if srch != query:
                print(f"[v5-ext] wikimedia matched on shortened query {srch!r} "
                      f"(original {query!r} had no hits)", file=sys.stderr)
            return candidates
    return []


def _default_providers() -> list[Callable[..., list[dict[str, Any]]]]:
    # Wikimedia Commons only for now: it exposes a machine-readable license
    # per file (LicenseShortName), so the reuse gate is reliable. NIH Open-i /
    # PMC Open Access need a per-image license-resolution step before they can
    # be enabled safely — left as a documented follow-up.
    return [_provider_wikimedia]


# ── Orchestrator ──────────────────────────────────────────────────────────────
def fetch_external_image(
    query: str,
    modality: str,
    *,
    cache_dir: Path | str | None = None,
    transport: Transport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    providers: list[Callable[..., list[dict[str, Any]]]] | None = None,
) -> dict[str, Any] | None:
    """Search open-access libraries for ONE reuse-permissive image matching
    *query* / *modality*, download + validate it, and return a dict with a
    base64 ``dataUrl`` plus license/attribution metadata. Returns ``None`` on
    any miss or failure (never raises)."""
    query = (query or "").strip()
    if not query:
        return None
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    key = _cache_key(query, modality or "")

    cached = _cache_load(cache_dir, key)
    if cached:
        return cached

    tx = transport or _urllib_transport
    provider_list = providers if providers is not None else _default_providers()

    for provider in provider_list:
        pname = getattr(provider, "__name__", "provider")
        try:
            candidates = provider(query, modality or "", tx, timeout)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash the run
            print(f"[v5-ext] {pname} search failed ({exc}); skipping.", file=sys.stderr)
            continue
        for cand in candidates or []:
            if not _license_ok(cand.get("license")):
                continue
            img_url = cand.get("imageUrl") or ""
            if not (_is_http_url(img_url) and _host_allowed(img_url)):
                continue
            try:
                status, _ctype, body = tx(
                    img_url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[v5-ext] download failed ({exc}); trying next candidate.",
                      file=sys.stderr)
                continue
            if status != 200:
                continue
            mime = _validate_image(body)
            if not mime:
                continue
            meta = {
                "mimeType": mime,
                "sourceName": cand.get("sourceName") or "",
                "sourceUrl": img_url,
                "pageUrl": cand.get("pageUrl") or "",
                "title": cand.get("title") or "",
                "license": cand.get("license") or "",
                "attribution": cand.get("attribution") or "",
                "query": query,
                "modality": modality or "",
            }
            _cache_store(cache_dir, key, body, meta)
            meta["dataUrl"] = _to_data_url(body, mime)
            meta["cacheHit"] = False
            return meta
    return None
