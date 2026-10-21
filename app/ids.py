"""Sortable 64-bit IDs: ``next_id()``.

Layout (most significant bit first)::

    41 bits  millisecond timestamp since ``TF_ID_EPOCH_MS``
    13 bits  shard reference number (0..8191)
    10 bits  per-millisecond counter (0..1023)

Total: 64 bits. IDs generated on different shards at different times sort in
roughly chronological order (k-sortable), so they can be used as primary keys
or cursor pagination tokens without a separate ``created_at`` index, and two
application servers can never mint the same ID as long as each one is given a
unique ``shard_id``.

Why this split: 41 bits of milliseconds is ~69 years of timestamps from the
epoch; 13 bits allows 8192 shards (far more than the image/logical shard
count, so a shard reference always fits); 10 bits allows 1024 IDs per shard
per millisecond (~1M IDs/second/shard) before the generator waits for the
next millisecond.

Thread-safe. Clock rollback (NTP step, container restore) makes the generator
wait until the wall clock catches up instead of minting out-of-order IDs.

Configuration (environment):
    TF_ID_EPOCH_MS  custom epoch in milliseconds (default 2024-01-01T00:00:00Z)
    TF_SHARD_ID     default shard reference for this process (default 0)

Postgres note: the value fits a ``BIGINT`` column until ~2058 with the default
epoch (the sign bit is the 64th bit). See ``docs/ids-next-id.md``.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

# 2024-01-01T00:00:00Z in milliseconds. Fixed default so IDs minted by every
# deployment share an origin; override only when migrating an existing fleet.
DEFAULT_EPOCH_MS = 1704067200000

TIMESTAMP_BITS = 41
SHARD_BITS = 13
SEQUENCE_BITS = 10

MAX_TIMESTAMP = (1 << TIMESTAMP_BITS) - 1
MAX_SHARD = (1 << SHARD_BITS) - 1          # 8191
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1    # 1023

_SHARD_SHIFT = SEQUENCE_BITS
_TIMESTAMP_SHIFT = SHARD_BITS + SEQUENCE_BITS


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def epoch_ms() -> int:
    """Custom epoch in milliseconds (``TF_ID_EPOCH_MS``)."""
    return _env_int("TF_ID_EPOCH_MS", DEFAULT_EPOCH_MS)


def default_shard_id() -> int:
    """Shard reference for this process (``TF_SHARD_ID``, clamped to range)."""
    return _env_int("TF_SHARD_ID", 0) & MAX_SHARD


def encode(timestamp_ms: int, shard_id: int, sequence: int,
           epoch: int | None = None) -> int:
    """Assemble an ID from its three parts. Raises ``ValueError`` if any part
    is out of range."""
    epoch = DEFAULT_EPOCH_MS if epoch is None else int(epoch)
    ts = int(timestamp_ms) - epoch
    if not 0 <= ts <= MAX_TIMESTAMP:
        raise ValueError(f"timestamp out of 41-bit range for epoch {epoch}")
    if not 0 <= int(shard_id) <= MAX_SHARD:
        raise ValueError(f"shard_id must be 0..{MAX_SHARD}")
    if not 0 <= int(sequence) <= MAX_SEQUENCE:
        raise ValueError(f"sequence must be 0..{MAX_SEQUENCE}")
    return (ts << _TIMESTAMP_SHIFT) | (int(shard_id) << _SHARD_SHIFT) | int(sequence)


def decode(value: int, epoch: int | None = None) -> dict:
    """Split an ID back into ``timestamp_ms`` / ``datetime`` / ``shard_id`` /
    ``sequence``."""
    epoch = DEFAULT_EPOCH_MS if epoch is None else int(epoch)
    value = int(value)
    if value < 0 or value >= (1 << 64):
        raise ValueError("not a 64-bit ID")
    ts = (value >> _TIMESTAMP_SHIFT) & MAX_TIMESTAMP
    shard = (value >> _SHARD_SHIFT) & MAX_SHARD
    seq = value & MAX_SEQUENCE
    stamp_ms = epoch + ts
    return {
        "timestamp_ms": stamp_ms,
        "datetime": datetime.fromtimestamp(stamp_ms / 1000, tz=timezone.utc),
        "shard_id": shard,
        "sequence": seq,
        "epoch_ms": epoch,
    }


class NextIdGenerator:
    """Thread-safe mint for the 64-bit layout above."""

    def __init__(self, shard_id: int | None = None, epoch: int | None = None,
                 clock=time.time):
        shard = default_shard_id() if shard_id is None else int(shard_id)
        if not 0 <= shard <= MAX_SHARD:
            raise ValueError(f"shard_id must be 0..{MAX_SHARD}")
        self.shard_id = shard
        self.epoch = DEFAULT_EPOCH_MS if epoch is None else int(epoch)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_ms = -1
        self._sequence = 0

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def next_id(self, shard_id: int | None = None) -> int:
        """Mint one ID. ``shard_id`` overrides this generator's shard for that
        single call (validated the same way)."""
        shard = self.shard_id if shard_id is None else int(shard_id)
        if not 0 <= shard <= MAX_SHARD:
            raise ValueError(f"shard_id must be 0..{MAX_SHARD}")
        with self._lock:
            now = self._now_ms()
            if now < self._last_ms:
                # Clock went backwards: wait it out rather than mint IDs that
                # sort before IDs already handed out.
                wait_ms = self._last_ms - now
                if wait_ms > 5000:
                    raise RuntimeError(
                        f"clock moved backwards by {wait_ms}ms; refusing to mint IDs")
                time.sleep(wait_ms / 1000.0)
                now = self._now_ms()
                if now < self._last_ms:
                    now = self._last_ms  # clock stuck: stay on the last tick
            if now < self.epoch:
                raise RuntimeError(
                    f"clock {now}ms is before the ID epoch {self.epoch}ms; "
                    "refusing to mint IDs")
            if now == self._last_ms:
                self._sequence += 1
                if self._sequence > MAX_SEQUENCE:
                    # More than 1024 IDs in one millisecond on this shard:
                    # spin into the next millisecond.
                    while now <= self._last_ms:
                        time.sleep(0.0002)
                        now = self._now_ms()
                    self._sequence = 0
            else:
                self._sequence = 0
            self._last_ms = now
            return (((now - self.epoch) << _TIMESTAMP_SHIFT)
                    | (shard << _SHARD_SHIFT) | self._sequence)

    def next_ids(self, count: int, shard_id: int | None = None) -> list[int]:
        """Mint ``count`` IDs in one call (still strictly increasing)."""
        if count < 0:
            raise ValueError("count must be >= 0")
        return [self.next_id(shard_id) for _ in range(count)]

    def decode(self, value: int) -> dict:
        return decode(value, self.epoch)


_GENERATOR: NextIdGenerator | None = None
_GENERATOR_LOCK = threading.Lock()


def generator() -> NextIdGenerator:
    """Process-wide generator honouring ``TF_SHARD_ID`` / ``TF_ID_EPOCH_MS``."""
    global _GENERATOR
    if _GENERATOR is None:
        with _GENERATOR_LOCK:
            if _GENERATOR is None:
                _GENERATOR = NextIdGenerator(
                    shard_id=default_shard_id(), epoch=epoch_ms())
    return _GENERATOR


def next_id(shard_id: int | None = None) -> int:
    """Mint one globally-unique, time-sortable 64-bit ID.

    Pass ``shard_id`` (0..8191) to tag the ID with a shard reference number;
    otherwise the process default (``TF_SHARD_ID``) is used.
    """
    return generator().next_id(shard_id)


def next_id_str(shard_id: int | None = None) -> str:
    """String form, for JSON payloads and text columns."""
    return str(next_id(shard_id))


def is_sorted(values) -> bool:
    """True when the IDs are in non-decreasing (time) order."""
    prev = None
    for value in values:
        if prev is not None and int(value) < prev:
            return False
        prev = int(value)
    return True


def status() -> dict:
    """Diagnostics snapshot (no secrets)."""
    gen = _GENERATOR
    return {
        "layout": f"{TIMESTAMP_BITS}-{SHARD_BITS}-{SEQUENCE_BITS}",
        "epoch_ms": epoch_ms(),
        "default_shard_id": default_shard_id(),
        "max_shard": MAX_SHARD,
        "max_sequence": MAX_SEQUENCE,
        "generator_initialised": gen is not None,
    }
