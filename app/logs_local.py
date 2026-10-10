"""Bounded reader for the harness logs that live on *this* instance.

The Logs tab normally reads ``logs/`` straight from GitHub in the browser, which
costs the deployment nothing. That only works when the repository coordinates are
known, and a container built from ``app/`` has no ``.git`` directory to read them
from — the tab could only say *"No git checkout here, so the log location is
unknown."* and show nothing at all, even though the harness may well have written
reports to the instance's own disk.

This module is the fallback, and it is deliberately small and paranoid:

* only the configured logs directory is ever opened, and every path is resolved
  and re-checked against it, so ``../`` cannot escape;
* only ``.md``, ``.log``, ``.json`` and ``.txt`` files are served;
* only the *tail* of a file is read, up to ``TF_MAX_LOG_FILE_BYTES``, so opening a
  large log cannot allocate more than that;
* the index is cached, and lists at most ``TF_MAX_LOG_INDEX_ROWS`` runs.

It is not a replacement for the GitHub path: when the coordinates are known the
dashboard still fetches from ``raw.githubusercontent.com`` and this module is
never called.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from app import guardrails

# Repository-relative by default; TF_LOGS_DIR overrides it (Docker points it at a
# mounted volume, the harness at its own copy).
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_RUN_FOLDER = re.compile(r"^\d{8}-\d{6}$")
_ALLOWED_SUFFIXES = {".md", ".log", ".json", ".txt"}
_CACHE: dict[str, object] = {"ts": 0.0, "value": None, "key": None}
CACHE_TTL = 10.0

# Files a harness run writes, in the order the dashboard shows them.
LOG_FILES = ("results.md", "harness.log", "unit.log", "scenarios.log", "server.log", "results.json")


def logs_dir() -> Path:
    raw = (os.environ.get("TF_LOGS_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[1] / "logs"


def available() -> bool:
    folder = logs_dir()
    return folder.is_dir() and any(folder.iterdir())


def _safe_join(folder: str, name: str) -> Path | None:
    """Resolve `logs/<folder>/<name>` and refuse anything that leaves the folder."""
    if not folder or not name:
        return None
    for segment in (folder, name):
        if not _SAFE_SEGMENT.fullmatch(segment) or ".." in segment or segment.startswith("."):
            return None
    root = logs_dir().resolve()
    candidate = (root / folder / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _parse_index_table(text: str) -> list[dict]:
    """Read the committed `logs/index.md` table into rows."""
    rows: list[dict] = []
    headers: list[str] | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
            continue
        if headers is None:
            headers = cells
            continue
        row = {headers[i]: (cells[i] if i < len(cells) else "") for i in range(len(headers))}
        folder = row.get("Folder") or ""
        match = re.search(r"\(([^)]+)/results\.md\)", folder)
        row["folder"] = match.group(1) if match else folder.strip("[]")
        rows.append(row)
    return rows


def _scan_folders(root: Path) -> list[dict]:
    """Build the index from the folders on disk when index.md is not there."""
    rows: list[dict] = []
    for folder in sorted((item for item in root.iterdir() if item.is_dir() and _RUN_FOLDER.match(item.name)),
                         reverse=True):
        row = {"folder": folder.name, "Started": folder.name, "Result": "?", "Passed": 0,
               "Failed": 0, "Skipped": 0, "Revision": "?", "source": "disk"}
        results = folder / "results.json"
        if results.is_file():
            try:
                payload = json.loads(results.read_text(encoding="utf-8")[: guardrails.MAX_LOG_FILE_BYTES])
                summary = payload.get("summary") or payload
                row["Started"] = payload.get("started") or summary.get("started") or folder.name
                row["Result"] = (payload.get("result") or summary.get("result") or "?").upper()
                row["Passed"] = summary.get("passed", 0)
                row["Failed"] = summary.get("failed", 0)
                row["Skipped"] = summary.get("skipped", 0)
                row["Revision"] = payload.get("revision") or summary.get("revision") or "?"
            except (OSError, ValueError):
                pass
        rows.append(row)
        if len(rows) >= guardrails.MAX_LOG_INDEX_ROWS:
            break
    return rows


def index(limit: int | None = None) -> dict:
    """The run index, from `index.md` when it exists and from the folders otherwise."""
    cap = min(int(limit or guardrails.MAX_LOG_INDEX_ROWS), guardrails.MAX_LOG_INDEX_ROWS)
    root = logs_dir()
    key = f"{root}:{cap}"
    now = time.time()
    cached = _CACHE.get("value")
    if cached is not None and _CACHE.get("key") == key and (now - float(_CACHE.get("ts") or 0)) < CACHE_TTL:
        return dict(cached)  # type: ignore[arg-type]

    if not root.is_dir():
        value = {
            "source": "local",
            "available": False,
            "logs_dir": str(root),
            "rows": [],
            "files": list(LOG_FILES),
            "reason": (
                f"There is no logs directory at {root}. This deployment keeps no "
                "harness reports on disk, and it has no git checkout to read the "
                "committed ones from."
            ),
        }
        _CACHE.update({"ts": now, "value": value, "key": key})
        return value

    index_file = root / "index.md"
    rows: list[dict] = []
    origin = "folders"
    if index_file.is_file():
        try:
            rows = _parse_index_table(index_file.read_text(encoding="utf-8")[: guardrails.MAX_LOG_FILE_BYTES])
            origin = "index.md"
        except OSError:
            rows = []
    if not rows:
        rows = _scan_folders(root)
        origin = "folders"
    value = {
        "source": "local",
        "available": bool(rows),
        "logs_dir": str(root),
        "origin": origin,
        "rows": rows[:cap],
        "total_rows": len(rows),
        "files": list(LOG_FILES),
        "reason": None if rows else f"No harness runs are stored in {root} yet.",
        "limits": {"max_rows": cap, "max_file_bytes": guardrails.MAX_LOG_FILE_BYTES},
    }
    _CACHE.update({"ts": now, "value": value, "key": key})
    return value


def files_in(folder: str) -> list[str]:
    if not _SAFE_SEGMENT.fullmatch(folder or "") or ".." in folder:
        return []
    root = logs_dir() / folder
    if not root.is_dir():
        return []
    return sorted(
        item.name for item in root.iterdir()
        if item.is_file() and item.suffix.lower() in _ALLOWED_SUFFIXES
    )[: guardrails.MAX_LOG_INDEX_ROWS]


def read(folder: str, name: str, max_bytes: int | None = None) -> dict:
    """Return the tail of one log file, never more than `max_bytes` of it."""
    cap = int(max_bytes or guardrails.MAX_LOG_FILE_BYTES)
    cap = max(1024, min(cap, guardrails.MAX_LOG_FILE_BYTES))
    path = _safe_join(folder, name)
    if path is None:
        return {"ok": False, "error": "Path is not inside the logs directory."}
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        return {"ok": False, "error": "Only .md, .log, .json and .txt files are served."}
    if not path.is_file():
        return {"ok": False, "error": "File not found."}
    size = path.stat().st_size
    try:
        with path.open("rb") as handle:
            if size > cap:
                handle.seek(size - cap)
                data = handle.read(cap)
            else:
                data = handle.read(cap)
    except OSError as exc:
        return {"ok": False, "error": f"Could not read the file: {exc.__class__.__name__}"}
    return {
        "ok": True,
        "folder": folder,
        "name": name,
        "text": data.decode("utf-8", "replace"),
        "bytes": len(data),
        "size": size,
        "truncated": size > len(data),
        "tail": size > len(data),
    }


def source_report(git_reason: str | None = None) -> dict:
    """What `/api/logs/source` returns when GitHub coordinates are unavailable."""
    report = index()
    return {
        "available": bool(report.get("available")),
        "source": "local",
        "logs_dir": report.get("logs_dir"),
        "origin": report.get("origin"),
        "rows": report.get("rows") or [],
        "files": list(LOG_FILES),
        "endpoints": {
            "index": "/api/logs/local/index",
            "file": "/api/logs/local/{folder}/{name}",
        },
        "reason": report.get("reason"),
        "git_reason": git_reason,
        "limits": report.get("limits"),
    }
