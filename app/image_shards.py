"""Image storage: logical shards over a machine map.

Screenshots and evidence images grow without bound, so they are not stored in
one directory. Each image key hashes to one of ``TF_IMAGE_SHARDS`` *logical*
shards, and a *machine map* assigns every shard range to a physical machine
(or volume, bucket, mount). Adding capacity means moving shard ranges to a new
machine and editing the map -- the hash never changes, so only the moved
ranges relocate.

Environment:
    TF_IMAGE_SHARDS     logical shard count (default 256, must be >= 1)
    TF_IMAGE_SHARD_MAP  JSON object of "start-end" -> "machine" entries, e.g.
                        {"0-127": "img-01:/data/img", "128-255": "img-02:/data/img"}
    TF_IMAGE_SHARD_MAP_FILE  path to a JSON file with the same object
                        (used when TF_IMAGE_SHARD_MAP is unset)
    TF_IMAGE_ROOT       local root for the "local" machine driver
                        (default <TF_ARTIFACTS>/images)

Machine entries are ``name`` or ``name:path``. The bundled driver stores every
machine under ``TF_IMAGE_ROOT/<name>/shard-<id>/`` so a single instance works
with no map at all (everything lands on machine ``local``). Cloud drivers
(S3/GCS prefixes per machine) can be added behind ``MachineStore`` without
changing the hashing or the map format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SHARDS = 256
_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def shard_count() -> int:
    """Logical shard count (``TF_IMAGE_SHARDS``)."""
    return max(1, _env_int("TF_IMAGE_SHARDS", DEFAULT_SHARDS))


def shard_for(key: str, total: int | None = None) -> int:
    """Deterministic logical shard for ``key`` (sha256 mod shard count)."""
    total = shard_count() if total is None else max(1, int(total))
    digest = hashlib.sha256(str(key).encode()).digest()
    return int.from_bytes(digest[:8], "big") % total


def default_root() -> Path:
    from app.config import ARTIFACTS
    return Path(os.environ.get("TF_IMAGE_ROOT", "") or
                os.path.join(ARTIFACTS, "images"))


@dataclass(frozen=True)
class Machine:
    """One entry of the machine map."""
    name: str
    location: str = ""

    @property
    def label(self) -> str:
        return self.name if not self.location else f"{self.name}:{self.location}"


def _parse_machine(raw: str) -> Machine:
    name, _, location = str(raw).partition(":")
    name = name.strip() or "local"
    return Machine(name=name, location=location.strip())


class MachineMap:
    """Shard-range -> machine routing table."""

    def __init__(self, total: int, ranges: list[tuple[int, int, Machine]]):
        self.total = total
        self.ranges = sorted(ranges)

    @classmethod
    def from_mapping(cls, mapping: dict | None, total: int) -> "MachineMap":
        """Build from ``{"start-end": "machine"}``. ``None``/``{}`` routes
        everything to machine ``local``."""
        if not mapping:
            return cls(total, [(0, total - 1, Machine("local"))])
        ranges: list[tuple[int, int, Machine]] = []
        for raw_range, raw_machine in mapping.items():
            match = _RANGE_RE.match(str(raw_range))
            if not match:
                raise ValueError(f"bad shard range {raw_range!r} (want 'start-end')")
            start, end = int(match.group(1)), int(match.group(2))
            if not (0 <= start <= end < total):
                raise ValueError(
                    f"shard range {raw_range!r} outside 0..{total - 1}")
            ranges.append((start, end, _parse_machine(raw_machine)))
        # Every shard must be covered exactly once.
        covered: list[Machine | None] = [None] * total
        for start, end, machine in ranges:
            for shard in range(start, end + 1):
                if covered[shard] is not None:
                    raise ValueError(f"shard {shard} mapped twice")
                covered[shard] = machine
        missing = [str(i) for i, m in enumerate(covered) if m is None]
        if missing:
            raise ValueError(
                f"shard map leaves shards unmapped: {','.join(missing[:8])}"
                + ("..." if len(missing) > 8 else ""))
        return cls(total, ranges)

    @classmethod
    def from_env(cls, total: int | None = None) -> "MachineMap":
        total = shard_count() if total is None else max(1, int(total))
        raw = os.environ.get("TF_IMAGE_SHARD_MAP", "").strip()
        if not raw:
            path = os.environ.get("TF_IMAGE_SHARD_MAP_FILE", "").strip()
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    raw = handle.read().strip()
        if not raw:
            return cls.from_mapping(None, total)
        try:
            mapping = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"TF_IMAGE_SHARD_MAP is not valid JSON: {exc}") from exc
        if not isinstance(mapping, dict):
            raise ValueError("TF_IMAGE_SHARD_MAP must be a JSON object")
        return cls.from_mapping(mapping, total)

    def machine_for(self, shard: int) -> Machine:
        """Machine owning ``shard`` (binary search over ranges)."""
        if not 0 <= shard < self.total:
            raise ValueError(f"shard {shard} outside 0..{self.total - 1}")
        lo, hi = 0, len(self.ranges) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            start, end, machine = self.ranges[mid]
            if shard < start:
                hi = mid - 1
            elif shard > end:
                lo = mid + 1
            else:
                return machine
        raise AssertionError("unreachable: map covers every shard")  # pragma: no cover

    def machine_for_key(self, key: str) -> tuple[int, Machine]:
        shard = shard_for(key, self.total)
        return shard, self.machine_for(shard)

    def describe(self) -> list[dict]:
        return [{"start": s, "end": e, "machine": m.label} for s, e, m in self.ranges]


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    cleaned = _SAFE_NAME.sub("_", str(name or "image")).strip("._") or "image"
    return cleaned[:180]


class ImageShardStore:
    """Content-addressed image files spread over logical shards.

    ``key`` is any stable identifier (run id + step index, content hash, ...).
    The shard derives from the key alone, so the file can always be located
    without a database lookup; the machine map decides *where* that shard
    lives today.
    """

    def __init__(self, machine_map: MachineMap | None = None,
                 root: Path | str | None = None):
        self.map = machine_map or MachineMap.from_env()
        self.root = Path(root) if root is not None else default_root()

    # -- location ------------------------------------------------------
    def locate(self, key: str, filename: str) -> dict:
        shard, machine = self.map.machine_for_key(key)
        name = safe_filename(filename)
        if machine.name == "local" and not machine.location:
            directory = self.root / "local" / f"shard-{shard:04d}"
        else:
            base = Path(machine.location) if machine.location else \
                self.root / machine.name
            # Absolute locations are honoured (mounted volumes); relative ones
            # hang under the machine directory.
            directory = base / f"shard-{shard:04d}" if base.is_absolute() else \
                self.root / machine.name / f"shard-{shard:04d}"
        return {"shard": shard, "machine": machine.label,
                "path": str(directory / name),
                "url": f"/api/images/{shard:04d}/{machine.name}/{name}"}

    # -- CRUD ----------------------------------------------------------
    def save(self, key: str, filename: str, data: bytes) -> dict:
        record = self.locate(key, filename)
        target = Path(record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        record["bytes"] = len(data)
        return record

    def load(self, key: str, filename: str) -> bytes | None:
        target = Path(self.locate(key, filename)["path"])
        try:
            return target.read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str, filename: str) -> bool:
        return Path(self.locate(key, filename)["path"]).exists()

    def delete(self, key: str, filename: str) -> bool:
        try:
            Path(self.locate(key, filename)["path"]).unlink()
            return True
        except FileNotFoundError:
            return False

    def shard_of(self, key: str) -> int:
        return shard_for(key, self.map.total)


_store: ImageShardStore | None = None
_store_lock = threading.Lock()


def default_store() -> ImageShardStore:
    """Process-wide store built from the environment."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ImageShardStore()
    return _store


def reset() -> None:
    """Drop the cached store (tests, diagnostics)."""
    global _store
    with _store_lock:
        _store = None


def status() -> dict:
    """Diagnostics snapshot."""
    try:
        machine_map = MachineMap.from_env()
        ranges = machine_map.describe()
        error = None
    except Exception as exc:
        machine_map = None
        ranges = []
        error = str(exc)
    machines = sorted({entry["machine"] for entry in ranges})
    return {
        "shards": shard_count(),
        "machines": machines,
        "ranges": ranges,
        "root": str(default_root()),
        "error": error,
        "ok": machine_map is not None,
    }
