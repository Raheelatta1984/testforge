"""Read-through cache backed by Memcached, with an in-process fallback.

Hot dashboard reads (project lists, recording catalogues, variable maps) hit
this module instead of the database on every request. When
``TF_MEMCACHED_SERVERS`` names one or more ``host:port`` servers and the
``pymemcache`` package is installed, values are shared between processes;
otherwise a bounded in-memory TTL map is used so caching still works on a
single instance with zero setup.

Environment:
    TF_CACHE_ENABLED       1/0 master switch (default 1)
    TF_MEMCACHED_SERVERS   comma-separated host:port list (default: empty = memory only)
    TF_MEMCACHED_PREFIX    key prefix (default "tf:")
    TF_MEMCACHED_TTL       default TTL seconds (default 60)
    TF_CACHE_MAX_ENTRIES   in-memory cap (default 2000)

Every function in this module fails open: a dead Memcached server degrades to
a cache miss, never to a 500. Callers must therefore treat ``get()`` returning
``None`` as "load from the database", not as "the value is empty" -- use the
``cached()`` decorator or ``get_or_load()`` to get that pattern for free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import threading
import time
from functools import wraps

logger = logging.getLogger("testforge.cache")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("TF_CACHE_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off", ""}


def prefix() -> str:
    return os.environ.get("TF_MEMCACHED_PREFIX", "tf:")


def default_ttl() -> int:
    return max(1, _env_int("TF_MEMCACHED_TTL", 60))


def servers() -> list[str]:
    raw = os.environ.get("TF_MEMCACHED_SERVERS", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def make_key(*parts) -> str:
    """Build a Memcached-safe key (<=250 chars, no whitespace) from parts."""
    joined = ":".join("" if part is None else str(part) for part in parts)
    key = prefix() + joined
    key = "".join(char if not char.isspace() else "_" for char in key)
    if len(key) > 240:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        key = key[:200] + ":sha256:" + digest
    return key


# --------------------------------------------------------------------------
# In-memory fallback (also used when Memcached is unreachable mid-request)
# --------------------------------------------------------------------------

_MAX_ENTRIES = max(100, _env_int("TF_CACHE_MAX_ENTRIES", 2000))
_memory: dict[str, tuple[object, float | None]] = {}
_memory_lock = threading.Lock()


def _memory_get(key: str):
    with _memory_lock:
        hit = _memory.get(key)
        if hit is None:
            return None, False
        value, expires = hit
        if expires is not None and expires < time.time():
            _memory.pop(key, None)
            return None, False
        return value, True


def _memory_set(key: str, value, ttl: int | None) -> None:
    expires = time.time() + ttl if ttl else None
    with _memory_lock:
        if len(_memory) >= _MAX_ENTRIES and key not in _memory:
            # Drop ~10% of oldest entries (dicts preserve insertion order).
            for old in list(_memory)[: max(1, _MAX_ENTRIES // 10)]:
                _memory.pop(old, None)
        _memory[key] = (value, expires)


def _memory_delete(key: str) -> None:
    with _memory_lock:
        _memory.pop(key, None)


def _memory_delete_prefix(key_prefix: str) -> int:
    with _memory_lock:
        doomed = [key for key in _memory if key.startswith(key_prefix)]
        for key in doomed:
            _memory.pop(key, None)
        return len(doomed)


# --------------------------------------------------------------------------
# Memcached client (lazy, shared)
# --------------------------------------------------------------------------

_client = None
_client_failed = False
_client_lock = threading.Lock()


def _client_or_none():
    """Shared HashClient, or None when Memcached is not configured/usable.

    A failed construction is remembered for this process so every read does not
    pay for a reconnect attempt; the in-memory fallback keeps serving.
    """
    global _client, _client_failed
    if _client is not None:
        return _client
    if _client_failed or not servers():
        return None
    with _client_lock:
        if _client is not None:
            return _client
        if _client_failed:
            return None
        try:
            from pymemcache.client.hash import HashClient  # type: ignore
        except Exception as exc:  # package not installed
            logger.warning("pymemcache unavailable (%s); using memory cache", exc)
            _client_failed = True
            return None
        try:
            parsed = []
            for entry in servers():
                host, _, port = entry.partition(":")
                parsed.append((host.strip() or "127.0.0.1",
                               int(port.strip() or "11211")))
            _client = HashClient(parsed, connect_timeout=0.5, timeout=0.5,
                                 ignore_exc=True)
            return _client
        except Exception as exc:
            logger.warning("memcached client init failed (%s); using memory cache", exc)
            _client_failed = True
            return None


def _dumps(value) -> bytes:
    if isinstance(value, bytes):
        return b"\x00" + value
    try:
        return b"\x01" + json.dumps(value).encode()
    except (TypeError, ValueError):
        return b"\x02" + pickle.dumps(value, protocol=4)


def _loads(blob) -> object:
    if blob is None:
        return None
    if not isinstance(blob, (bytes, bytearray)):
        return blob
    blob = bytes(blob)
    if blob.startswith(b"\x00"):
        return blob[1:]
    if blob.startswith(b"\x01"):
        return json.loads(blob[1:].decode())
    if blob.startswith(b"\x02"):
        return pickle.loads(blob[1:])  # noqa: S301 - own cache entries only
    return blob


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get(key: str, default=None):
    """Return the cached value, or ``default`` on miss/disabled/error."""
    if not enabled():
        return default
    client = _client_or_none()
    if client is not None:
        try:
            blob = client.get(key)
            if blob is not None:
                return _loads(blob)
        except Exception as exc:
            logger.debug("memcached get failed: %s", exc)
    value, hit = _memory_get(key)
    return value if hit else default


def set(key: str, value, ttl: int | None = None) -> bool:
    """Store ``value`` under ``key`` for ``ttl`` seconds. Never raises."""
    if not enabled():
        return False
    ttl = default_ttl() if ttl is None else max(1, int(ttl))
    ok = False
    client = _client_or_none()
    if client is not None:
        try:
            client.set(key, _dumps(value), expire=ttl)
            ok = True
        except Exception as exc:
            logger.debug("memcached set failed: %s", exc)
    _memory_set(key, value, ttl)
    return ok or True


def delete(key: str) -> None:
    """Drop one key from every tier. Never raises."""
    client = _client_or_none()
    if client is not None:
        try:
            client.delete(key, ignore_not_found=True)
        except Exception:
            pass
    _memory_delete(key)


def delete_prefix(key_prefix: str) -> int:
    """Drop every key starting with ``key_prefix``.

    Memcached has no prefix scan, so the shared tier cannot be enumerated: the
    in-memory tier is cleared exactly, and the shared tier relies on TTL
    expiry. Returns the in-memory drop count. Prefer versioned keys
    (``make_key("v3", ...)``) when a whole namespace must turn over at once.
    """
    dropped = _memory_delete_prefix(key_prefix)
    return dropped


def clear_memory() -> None:
    """Empty the in-memory tier (tests, diagnostics)."""
    with _memory_lock:
        _memory.clear()


def get_or_load(key: str, loader, ttl: int | None = None):
    """Read-through helper: cached value, else ``loader()`` then store.

    ``loader`` exceptions propagate (the database error is real); only cache
    transport failures are swallowed.
    """
    marker = object()
    hit = get(key, marker)
    if hit is not marker:
        return hit
    value = loader()
    set(key, value, ttl)
    return value


def cached(ttl: int | None = None, namespace: str = "fn"):
    """Decorator: memoise a pure function call for ``ttl`` seconds.

    The key is ``namespace`` + module + qualname + repr(args/kwargs); keep
    arguments small and deterministic.
    """
    def decorate(func):
        key_base = f"{namespace}:{func.__module__}.{func.__qualname__}"

        @wraps(func)
        def wrapper(*args, **kwargs):
            fingerprint = repr((args, sorted(kwargs.items())))
            digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
            return get_or_load(make_key(key_base, digest),
                               lambda: func(*args, **kwargs), ttl)
        wrapper.cache_namespace = key_base  # type: ignore[attr-defined]
        return wrapper
    return decorate


def invalidate_namespace(namespace: str) -> int:
    """Best-effort invalidation for a ``cached()`` namespace (see
    ``delete_prefix`` for the Memcached caveat)."""
    return delete_prefix(prefix() + namespace)


def status() -> dict:
    """Diagnostics snapshot (no values, only shapes)."""
    with _memory_lock:
        mem_entries = len(_memory)
    return {
        "enabled": enabled(),
        "backend": "memcached" if _client_or_none() is not None else (
            "memory" if enabled() else "disabled"),
        "servers_configured": len(servers()),
        "prefix": prefix(),
        "default_ttl": default_ttl(),
        "memory_entries": mem_entries,
        "memory_cap": _MAX_ENTRIES,
    }
