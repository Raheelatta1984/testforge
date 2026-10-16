"""Optional single-operator access gate, using only the standard library.

Set TF_ACCESS_TOKEN (at least 32 characters) at deployment. No accounts, external
identity service, billing integration or server-side session worker. Login token
attempts are limited in memory per direct peer address. This is NOT multi-tenant
RBAC or a signup system, and the limiter is per worker; production deployments
should add gateway-level controls. Unconfigured deployments retain legacy
behavior and explicitly report that they are unprotected; put them behind an
authenticated private gateway.
"""
import hashlib
import hmac
import json
import os
from http.cookies import SimpleCookie, CookieError
import secrets
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()
COOKIE = "tf_session"
TTL = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
_LOGIN_ATTEMPTS = defaultdict(deque)
_LOGIN_BUCKETS_MAX = 10_000


def _check_login_rate(request: Request) -> None:
    """Limit token-guessing attempts per direct peer address.

    Do not trust X-Forwarded-For here: unless the deployment has an explicitly
    configured trusted-proxy layer, clients can forge it to evade this control.
    This in-memory limit is per worker; deployments with multiple workers should
    also enforce rate limits at their reverse proxy/API gateway.
    """
    peer = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _LOGIN_ATTEMPTS[peer]
    while attempts and now - attempts[0] >= LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        retry_after = max(1, int(LOGIN_WINDOW_SECONDS - (now - attempts[0])))
        raise HTTPException(429, "Too many login attempts; try again later",
                            headers={"Retry-After": str(retry_after)})
    attempts.append(now)
    if len(_LOGIN_ATTEMPTS) > _LOGIN_BUCKETS_MAX:
        # Bound attacker-controlled address cardinality, even if every bucket
        # is still active. Ordered dict eviction is intentionally conservative.
        _LOGIN_ATTEMPTS.pop(next(iter(_LOGIN_ATTEMPTS)))


def token():
    return os.environ.get("TF_ACCESS_TOKEN", "")


def signed_session(key):
    payload = str(int(time.time())) + "." + secrets.token_hex(16)
    return payload + "." + hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def allowed(scope):
    key = token()
    if not key:
        return True
    if len(key) < 32:
        return False
    headers = dict(scope.get("headers", []))
    bearer = headers.get(b"authorization", b"").decode("latin-1")
    if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), key.encode()):
        return True
    try:
        cookie = SimpleCookie()
        cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
        value = cookie[COOKIE].value
        stamp, nonce, signature = value.split(".")
        age = time.time() - int(stamp)
        payload = stamp + "." + nonce
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return 0 <= age <= TTL and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError, KeyError, CookieError):
        return False


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] in {"http", "websocket"} and path.startswith(("/api/", "/ws/")):
            public = path in {"/api/auth/status", "/api/auth/login", "/api/health"}
            if not public and not allowed(scope):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Authentication required"}, status_code=401)(scope, receive, send)
                return
            if scope["type"] == "websocket" and token():
                # Browser websocket cookies must not authorize a cross-origin page.
                headers = dict(scope.get("headers", []))
                origin = headers.get(b"origin", b"").decode("latin-1")
                if origin:
                    from urllib.parse import urlparse
                    if urlparse(origin).netloc != headers.get(b"host", b"").decode("latin-1"):
                        await send({"type": "websocket.close", "code": 1008})
                        return
        await self.app(scope, receive, send)


@router.get("/api/auth/status")
def status(request: Request):
    return {"configured": bool(token()), "authenticated": allowed(request.scope),
            "mode": "single-operator" if token() else "unprotected"}


@router.post("/api/auth/login")
async def login(request: Request):
    _check_login_rate(request)
    from app.import_routes import bounded_body
    try:
        body = json.loads(await bounded_body(request, 4096))
        supplied = body.get("token")
    except (ValueError, AttributeError):
        raise HTTPException(422, "Expected a token")
    key = token()
    if len(key) < 32 or not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), key.encode()):
        raise HTTPException(401, "Access token is invalid or not configured securely")
    response = JSONResponse({"authenticated": True})
    # Trust proxy HTTPS indication only for making the cookie MORE restrictive.
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0] == "https"
    response.set_cookie(COOKIE, signed_session(key), httponly=True, secure=secure, samesite="strict", max_age=TTL)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/auth/logout")
def logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE)
    return response
