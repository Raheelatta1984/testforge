"""Append-only, hash-chained audit trail for runs.

Each run keeps ``audit.jsonl`` next to its step evidence: one JSON object per
line, each row carrying the SHA-256 of the previous row's hash (genesis: 64
zeros). The pattern is the same as the market claim-audit receipt: the hash is
taken over canonical JSON (sorted keys, compact separators), so :meth:`verify`
can re-derive every link and detect any mutated, reordered or removed row.

What it proves: *this* is the log the run wrote, in order, unmodified since.
What it does not prove on its own: that no lines were dropped from the *tail*.
A caller that compares the ``head`` hash across reads (the audit endpoint
reports it) detects truncation too; a log whose last entry is neither
``run_finished`` nor a post-run ``locator_heal_review`` is additionally
reported as incomplete, which is what a truncated file looks like.

Bounds: every payload value is clipped, rows past 5,000 are refused (a run
expands to at most 2,000 steps), and verification refuses files over 4 MiB.
No secrets are written here on purpose: callers append entries that were
already scrubbed for the execution log.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path

FILENAME = "audit.jsonl"
GENESIS = "0" * 64
MAX_PAYLOAD_CHARS = 2000
MAX_ROWS = 5000
MAX_FILE_BYTES = 4 * 1024 * 1024


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(row: dict) -> bytes:
    return json.dumps(row, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str).encode("utf-8")


def _clip(value, limit: int = MAX_PAYLOAD_CHARS):
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
    if isinstance(value, dict):
        return {str(key)[:120]: _clip(item, limit) for key, item in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit) for item in list(value)[:50]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip(str(value), limit)


def artifacts_root() -> Path:
    root = os.environ.get("TF_ARTIFACTS") or os.path.join(os.path.expanduser("~"), "testforge_titan_data")
    return Path(root)


class AuditTrail:
    """One run's event log. Created once per run, appended as it executes."""

    def __init__(self, directory, run_id: str):
        self.directory = Path(directory)
        self.run_id = str(run_id)
        self.path = self.directory / FILENAME
        self._seq = 0
        self._prev = GENESIS
        if self.path.is_file():
            for row in self.read():
                self._seq = int(row.get("seq") or 0)
                self._prev = str(row.get("hash") or GENESIS)

    def append(self, event: str, payload=None) -> dict:
        """Append one event. Returns the sealed row."""
        if not str(event or "").strip():
            raise ValueError("audit event needs a name")
        if self._seq >= MAX_ROWS:
            raise ValueError(f"audit trail exceeds {MAX_ROWS} rows")
        self.directory.mkdir(parents=True, exist_ok=True)
        body = {
            "seq": self._seq + 1,
            "ts": _now_iso(),
            "run_id": self.run_id,
            "event": str(event)[:80],
            "payload": _clip(payload if isinstance(payload, dict) else {"value": payload}),
            "prev": self._prev,
        }
        row = {**body, "hash": hashlib.sha256(_canonical(body)).hexdigest()}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")
        self._seq += 1
        self._prev = row["hash"]
        return row

    def read(self) -> list[dict]:
        """Every row, oldest first. Raises ValueError on a malformed file."""
        if not self.path.is_file():
            return []
        if self.path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"audit file exceeds {MAX_FILE_BYTES} bytes")
        rows = []
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"audit row {line_number} is not JSON") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"audit row {line_number} is not an object")
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise ValueError(f"audit file exceeds {MAX_ROWS} rows")
        return rows

    def verify(self) -> dict:
        """Re-derive every link. ``ok`` is False when any row was changed."""
        try:
            rows = self.read()
        except ValueError as exc:
            return {"ok": False, "rows": 0, "detail": str(exc), "head": None,
                    "complete": False}
        if not rows:
            return {"ok": True, "rows": 0, "detail": "empty trail", "head": GENESIS,
                    "complete": False}
        prev = GENESIS
        for index, row in enumerate(rows, start=1):
            if self.run_id and str(row.get("run_id") or "") != self.run_id:
                return {"ok": False, "rows": index,
                        "detail": f"row {index} belongs to run {row.get('run_id')!r}: swapped file",
                        "head": None, "complete": False}
            if int(row.get("seq") or 0) != index:
                return {"ok": False, "rows": index,
                        "detail": f"row {index} has seq {row.get('seq')!r}: reordered or removed rows",
                        "head": None, "complete": False}
            if str(row.get("prev") or "") != prev:
                return {"ok": False, "rows": index,
                        "detail": f"row {index} does not chain to row {index - 1}: edited history",
                        "head": None, "complete": False}
            body = {key: row[key] for key in ("seq", "ts", "run_id", "event", "payload", "prev")
                    if key in row}
            if set(body) != {"seq", "ts", "run_id", "event", "payload", "prev"}:
                return {"ok": False, "rows": index,
                        "detail": f"row {index} is missing fields", "head": None,
                        "complete": False}
            if hashlib.sha256(_canonical(body)).hexdigest() != str(row.get("hash") or ""):
                return {"ok": False, "rows": index,
                        "detail": f"row {index} ({row.get('event')}) was modified after it was sealed",
                        "head": None, "complete": False}
            prev = str(row["hash"])
        return {"ok": True, "rows": len(rows), "detail": f"{len(rows)} rows chained",
                "head": prev, "complete": rows[-1].get("event") in {"run_finished", "locator_heal_review"}}


def verify_file(path) -> dict:
    """Verify an audit file; the run id is the directory that holds it."""
    trail = AuditTrail(Path(path).parent, Path(path).parent.name)
    trail.path = Path(path)
    result = trail.verify()
    result["present"] = Path(path).is_file()
    result["verified"] = result["ok"] if result["present"] else None
    return result


def verify_run(run_id: str, artifacts=None) -> dict:
    """Summary for one run: presence, verification, row count, head hash."""
    path = Path(artifacts or artifacts_root()) / "runs" / str(run_id) / FILENAME
    if not path.is_file():
        return {"present": False, "verified": None, "rows": 0,
                "detail": "this run recorded no audit trail", "head": None,
                "complete": False}
    result = verify_file(path)
    return {"present": True, "verified": result["ok"], "rows": result["rows"],
            "detail": result["detail"], "head": result["head"],
            "complete": result["complete"]}
