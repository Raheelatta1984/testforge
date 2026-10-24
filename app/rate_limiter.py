"""Simple in-memory rate limiter using token bucket algorithm.

This module provides rate limiting without requiring external dependencies like Redis.
It's suitable for a single-process deployment (e.g., uvicorn with one worker).

Environment:
    RATE_LIMIT_ENABLED  1/0 default 1
    RATE_LIMIT_REQUESTS default 100  (requests per period)
    RATE_LIMIT_PERIOD   default 60   (seconds)
    RATE_LIMIT_KEY      function to determine the rate limit key per request
"""

from __future__ import annotations

import inspect
import time
import threading
from collections import defaultdict
from functools import wraps
from fastapi import Request, Response
from fastapi.responses import JSONResponse

# Configuration
_rate_limit_enabled = True
_rate_limit_requests = 100  # default: 100 requests per period
_rate_limit_period = 60   # default: 60 seconds

# Token bucket state: {key: {"tokens": float, "last_refill": float}}
_buckets: dict[str, dict[str, float]] = defaultdict(dict)
_buckets_lock = threading.Lock()


def _init_bucket(key: str) -> dict[str, float]:
    """Initialize a new rate limit bucket for the given key."""
    return {
        "tokens": float(_rate_limit_requests),
        "last_refill": time.time(),
    }


def _refill_bucket(bucket: dict[str, float]) -> None:
    """Refill tokens based on elapsed time."""
    now = time.time()
    elapsed = now - bucket["last_refill"]
    # Add one token per _rate_limit_period / _rate_limit_requests seconds
    add_tokens = elapsed * (_rate_limit_requests / _rate_limit_period)
    if add_tokens >= 1:
        bucket["tokens"] = min(_rate_limit_requests, bucket["tokens"] + add_tokens)
        bucket["last_refill"] = now - (elapsed - (now - bucket["last_refill"]))


def is_allowed(key: str) -> tuple[bool, int, float]:
    """Check if a request is within rate limits for the given key.
    
    Returns (allowed: bool, remaining: int, reset_time: float)
    """
    with _buckets_lock:
        if key not in _buckets:
            _buckets[key] = _init_bucket(key)
        bucket = _buckets[key]
        _refill_bucket(bucket)
        
        if bucket["tokens"] > 0:
            bucket["tokens"] -= 1
            remaining = int(bucket["tokens"])
            # Calculate when the next token will be available
            if _rate_limit_requests > 0:
                seconds_per_token = _rate_limit_period / _rate_limit_requests
                reset_time = bucket["last_refill"] + seconds_per_token * (remaining + 1)
            else:
                reset_time = bucket["last_refill"]
            return True, max(0, remaining), reset_time
        else:
            # Rate limit exceeded
            reset_time = bucket["last_refill"] + _rate_limit_period
            return False, 0, reset_time


def set_config(enabled: int | None = None, requests: int | None = None, period: int | None = None) -> None:
    """Configure rate limiting settings."""
    global _rate_limit_enabled, _rate_limit_requests, _rate_limit_period
    if enabled is not None:
        _rate_limit_enabled = bool(enabled)
    if requests is not None:
        _rate_limit_requests = max(1, requests)
    if period is not None:
        _rate_limit_period = max(1, period)


def get_config() -> dict:
    """Get current rate limit configuration."""
    return {
        "enabled": _rate_limit_enabled,
        "requests_per_period": _rate_limit_requests,
        "period_seconds": _rate_limit_period,
    }


def rate_limit(func):
    """Decorator to apply rate limiting to a route handler.

    FastAPI decides what to pass a route handler from the handler's signature,
    so the wrapper must advertise the wrapped function's parameters *plus* a
    `Request` of its own whenever the handler does not already take one.
    `@wraps` alone is not enough: it points `inspect.signature` at the wrapped
    function, FastAPI then sees no `request` to inject, and calling the wrapper
    raises ``TypeError: ... missing 1 required positional argument: 'request'``
    — the 500 that fails the deployment's /api/health probe. The wrapper also
    must not push a `request` into handlers that never asked for one, and must
    await only handlers that are coroutines.
    """
    sig = inspect.signature(func)
    takes_request = "request" in sig.parameters
    params = list(sig.parameters.values())
    if not takes_request:
        # Keyword-only, so the injected request fits any parameter ordering.
        # FastAPI always calls endpoints with keyword arguments.
        injected = inspect.Parameter(
            "request", inspect.Parameter.KEYWORD_ONLY, annotation=Request
        )
        at = next(
            (i for i, p in enumerate(params)
             if p.kind is inspect.Parameter.VAR_KEYWORD),
            len(params),
        )
        params.insert(at, injected)
    wrapper_sig = sig.replace(parameters=params)

    async def _call(*args, **kwargs):
        """Invoke the handler, awaiting it only when it is a coroutine."""
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    @wraps(func)
    async def wrapper(*args, **kwargs):
        request = kwargs.get("request", None)
        if request is None and args and isinstance(args[0], Request):
            request = args[0]
        if not takes_request:
            kwargs.pop("request", None)
            args = tuple(a for a in args if not isinstance(a, Request))

        if not _rate_limit_enabled:
            return await _call(*args, **kwargs)

        # Determine the rate limit key
        # Use client IP by default
        key = "unknown"
        if request is not None:
            client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if not client_ip:
                client_ip = request.client.host if request.client else "unknown"
            key = f"ip:{client_ip}"

        allowed, remaining, reset_time = is_allowed(key)

        if not allowed:
            # Add rate limit info to response headers
            response = JSONResponse(
                content={"detail": "Rate limit exceeded"},
                status_code=429,
            )
            response.headers["X-RateLimit-Limit"] = str(_rate_limit_requests)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["X-RateLimit-Reset"] = str(int(reset_time))
            response.headers["Retry-After"] = str(int(reset_time - time.time()) + 1)
            return response

        # Add rate limit info to successful responses
        response = await _call(*args, **kwargs)
        if hasattr(response, 'headers'):
            response.headers["X-RateLimit-Limit"] = str(_rate_limit_requests)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(int(reset_time))

        return response

    # `@wraps` copied `__wrapped__`, which makes `inspect.signature` report the
    # handler's own parameters; override it with the merged signature so FastAPI
    # injects the request this wrapper needs while still parsing the handler's
    # query/path/body parameters.
    wrapper.__signature__ = wrapper_sig
    return wrapper
