"""Deterministic visual baselines: a bounded pixel diff and a verdict.

Layout, overlap and rendering defects pass every DOM assertion, so a scripted
suite can be green while the page looks broken. This module is the smallest
honest version of the answer:

* **A baseline store.** Promote the step screenshots of one run to baselines for
  a recording, keyed by recording, step and viewport. Baselines are ordinary
  files under ``ARTIFACTS/visual``, listable and deletable.
* **A bounded diff.** Two images are compared in luminance space with Pillow:
  per-pixel delta, a tolerance below which a pixel counts as unchanged, a
  changed-pixel ratio, the mean delta and the bounding box of the change.
* **A verdict with evidence.** ``ratio > threshold`` fails. Every comparison
  returns the numbers and both content hashes, so a failure can be re-derived
  by hand instead of trusted.

Deliberately not included, and not claimed anywhere: no AI "ignore regions", no
cross-browser baseline matrix, no auto-healing of baselines. A baseline changes
when a human promotes a new one, which is the only way a visual gate stays
trustworthy.

Standard library plus Pillow (already a runtime dependency). No network, no
model, no browser: the module never starts one and never reads a URL.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageChops

from app.config import ARTIFACTS

# --- Bounds -----------------------------------------------------------------
# A run screenshot is at most 512 KiB (app.evidence). The caps below are the
# belt-and-braces version of that: a promoted baseline is never larger than the
# artefact it came from, and no comparison can be asked to decode a huge image.
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4_000_000
# Steps compared in one pass. A recording is capped at 500 steps; comparing two
# hundred screenshots is already the expensive path on a small instance.
MAX_STEPS_COMPARED = 200
# Baselines kept per recording, so a runaway promote cannot fill the disk.
MAX_BASELINES_PER_RECORDING = 200

DEFAULT_THRESHOLD = 0.005          # 0.5% of pixels may differ
MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 0.5
# Per-pixel luminance delta (0-255) below which a pixel counts as unchanged.
# JPEG evidence is encoded at quality 35, so a byte-identical re-encode of the
# same page still moves single pixels by a little; 16 absorbs that.
TOLERANCE = 16

KEY_RE = re.compile(r"^[0-9a-f]{32}$")
STEP_FILE_RE = re.compile(r"^step-(\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

STORE_DIRNAME = "visual"
BASELINE_DIRNAME = "baselines"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def baseline_key(recording_id: str, step_index: int, viewport: str = "") -> str:
    """Stable key for one step's baseline. Deterministic, so it is cacheable."""
    seed = f"{recording_id}|{int(step_index)}|{viewport or ''}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalise_threshold(value=None) -> float:
    """Clamp a caller-supplied threshold into the supported range."""
    if value is None:
        return DEFAULT_THRESHOLD
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    if number != number:  # NaN
        return DEFAULT_THRESHOLD
    return min(MAX_THRESHOLD, max(MIN_THRESHOLD, number))


def _open(data: bytes) -> Image.Image:
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("image data must be bytes")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image exceeds the {MAX_IMAGE_BYTES} byte cap")
    image = Image.open(io.BytesIO(bytes(data)))
    image.load()
    if image.size[0] * image.size[1] > MAX_PIXELS:
        raise ValueError(f"image exceeds the {MAX_PIXELS} pixel cap")
    return image.convert("RGB")


def diff_metrics(baseline: Image.Image, candidate: Image.Image,
                 tolerance: int = TOLERANCE) -> tuple[float, float, list[int] | None, bool]:
    """Return ``(ratio, mean_delta, bbox, resized)`` for two RGB images."""
    resized = False
    if baseline.size != candidate.size:
        candidate = candidate.resize(baseline.size, Image.LANCZOS)
        resized = True
    delta = ImageChops.difference(baseline, candidate).convert("L")
    total = baseline.size[0] * baseline.size[1]
    histogram = delta.histogram()
    changed = sum(histogram[tolerance + 1:]) if tolerance + 1 < len(histogram) else 0
    mean_delta = sum(index * count for index, count in enumerate(histogram)) / max(1, total)
    mask = delta.point(lambda value: 255 if value > tolerance else 0)
    bbox = list(mask.getbbox()) if mask.getbbox() else None
    return (changed / max(1, total)), round(mean_delta, 3), bbox, resized


def compare(baseline_bytes: bytes, candidate_bytes: bytes, threshold=None,
            tolerance: int = TOLERANCE) -> dict:
    """Compare two images and return the verdict plus every number behind it."""
    threshold = normalise_threshold(threshold)
    baseline = _open(baseline_bytes)
    candidate = _open(candidate_bytes)
    ratio, mean_delta, bbox, resized = diff_metrics(baseline, candidate, tolerance)
    payload = {
        "verdict": "pass" if ratio <= threshold else "fail",
        "ratio": round(ratio, 6),
        "changed_pixels": int(round(ratio * baseline.size[0] * baseline.size[1])),
        "total_pixels": baseline.size[0] * baseline.size[1],
        "mean_delta": mean_delta,
        "bbox": bbox,
        "threshold": threshold,
        "tolerance": tolerance,
        "resized": resized,
        "baseline": {"width": baseline.size[0], "height": baseline.size[1],
                     "sha256": _sha256(baseline_bytes)},
        "candidate": {"width": candidate.size[0], "height": candidate.size[1],
                      "sha256": _sha256(candidate_bytes)},
    }
    return payload


class BaselineStore:
    """Filesystem baseline store under ``ARTIFACTS/visual/baselines``."""

    def __init__(self, root: str | os.PathLike | None = None):
        base = Path(root) if root else Path(ARTIFACTS)
        self.root = base / STORE_DIRNAME / BASELINE_DIRNAME
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths --------------------------------------------------------------
    def image_path(self, key: str) -> Path:
        if not KEY_RE.match(str(key)):
            raise ValueError("invalid baseline key")
        return self.root / f"{key}.img"

    def meta_path(self, key: str) -> Path:
        if not KEY_RE.match(str(key)):
            raise ValueError("invalid baseline key")
        return self.root / f"{key}.json"

    # --- reads and writes ---------------------------------------------------
    def save(self, key: str, data: bytes, meta: dict | None = None) -> dict:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("a baseline needs non-empty image bytes")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds the {MAX_IMAGE_BYTES} byte cap")
        target = self.image_path(key)
        target.write_bytes(bytes(data))
        record = {
            "key": key,
            "bytes": len(data),
            "sha256": _sha256(bytes(data)),
            "created_at": _now(),
            **(meta or {}),
        }
        self.meta_path(key).write_text(json.dumps(record, indent=2, sort_keys=True),
                                       encoding="utf-8")
        return record

    def image(self, key: str) -> bytes | None:
        try:
            target = self.image_path(key)
        except ValueError:
            return None
        return target.read_bytes() if target.is_file() else None

    def meta(self, key: str) -> dict | None:
        try:
            target = self.meta_path(key)
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def delete(self, key: str) -> bool:
        try:
            target = self.image_path(key)
        except ValueError:
            return False
        removed = target.is_file()
        for path in (target, self.meta_path(key)):
            try:
                path.unlink()
            except OSError:
                pass
        return removed

    def list(self, recording_id: str | None = None) -> list[dict]:
        rows = []
        for meta_file in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if recording_id and record.get("recording_id") != recording_id:
                continue
            rows.append(record)
        rows.sort(key=lambda row: (str(row.get("recording_id") or ""),
                                   int(row.get("step_index") or 0)))
        return rows

    # --- run integration ----------------------------------------------------
    @staticmethod
    def step_files(run_dir: str | os.PathLike) -> list[tuple[int, Path]]:
        """``[(step_index, path)]`` for the evidence screenshots of one run."""
        directory = Path(run_dir)
        if not directory.is_dir():
            return []
        found = []
        for entry in directory.iterdir():
            match = STEP_FILE_RE.match(entry.name)
            if match and entry.is_file():
                found.append((int(match.group(1)), entry))
        found.sort(key=lambda row: row[0])
        return found[:MAX_STEPS_COMPARED]

    def promote(self, run_dir: str | os.PathLike, recording_id: str, viewport: str = "",
                run_id: str | None = None, steps: list[int] | None = None) -> list[dict]:
        """Copy a run's step screenshots into the baseline store."""
        if not SAFE_ID_RE.match(str(recording_id or "")):
            raise ValueError("invalid recording id")
        wanted = set(int(index) for index in steps) if steps else None
        saved = []
        for index, path in self.step_files(run_dir):
            if wanted is not None and index not in wanted:
                continue
            if len(saved) >= MAX_BASELINES_PER_RECORDING:
                break
            key = baseline_key(recording_id, index, viewport)
            saved.append(self.save(key, path.read_bytes(), {
                "recording_id": str(recording_id),
                "step_index": index,
                "viewport": viewport or "",
                "source_run": run_id,
                "source_file": path.name,
            }))
        return saved

    def compare_run(self, run_dir: str | os.PathLike, recording_id: str, viewport: str = "",
                    threshold=None, run_id: str | None = None) -> dict:
        """Compare every step screenshot of a run against the stored baseline."""
        if not SAFE_ID_RE.match(str(recording_id or "")):
            raise ValueError("invalid recording id")
        threshold = normalise_threshold(threshold)
        results = []
        for index, path in self.step_files(run_dir):
            key = baseline_key(recording_id, index, viewport)
            baseline = self.image(key)
            row = {"step_index": index, "key": key, "file": path.name}
            if baseline is None:
                row.update({"status": "missing_baseline", "verdict": "unknown"})
                results.append(row)
                continue
            try:
                row.update(compare(baseline, path.read_bytes(), threshold))
                row["status"] = "compared"
            except ValueError as exc:
                row.update({"status": "error", "verdict": "unknown", "error": str(exc)})
            results.append(row)
        failed = sum(1 for row in results if row.get("verdict") == "fail")
        missing = sum(1 for row in results if row.get("status") == "missing_baseline")
        errored = sum(1 for row in results if row.get("status") == "error")
        return {
            "recording_id": str(recording_id),
            "run_id": run_id,
            "viewport": viewport or "",
            "threshold": threshold,
            "steps": len(results),
            "passed": sum(1 for row in results if row.get("verdict") == "pass"),
            "failed": failed,
            "missing_baseline": missing,
            "errors": errored,
            "verdict": "pass" if not (failed or missing or errored) and results else "fail",
            "results": results,
        }

    def report(self) -> dict:
        rows = self.list()
        return {"root": str(self.root), "baselines": len(rows),
                "recordings": len({row.get("recording_id") for row in rows}),
                "threshold": DEFAULT_THRESHOLD, "tolerance": TOLERANCE,
                "max_baselines_per_recording": MAX_BASELINES_PER_RECORDING}
