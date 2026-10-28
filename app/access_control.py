"""Optional single-operator access gate, using only the standard library.

Set TF_ACCESS_TOKEN (at least 32 characters) at deployment. No accounts, external
identity service, billing integration or server-side session worker. Login token
attempts are limited in memory per direct peer address. This is NOT multi-tenant
RBAC or a signup system, and the limiter is per worker; production deployments
should add gateway-level controls. Unconfigured deployments retain legacy
behavior and explicitly report that they are unprotected; put them behind an
authenticated private gateway.

The browser session is a cookie, never web storage: HttpOnly (not readable by an
injected script), Secure over HTTPS under the __Host- prefix with Path=/ and no
Domain, so nothing on a sibling path or subdomain can set or shadow it. Because a
cookie alone would hand cross-site POSTs an authenticated request, the gate also
checks Origin (falling back to Referer and Sec-Fetch-Site) on unsafe methods, and
logout revokes the value server-side instead of only expiring it in one browser.
"""
import hashlib
import hmac
import json
import os
from http.cookies import SimpleCookie, CookieError
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()
COOKIE = "tf_session"
# __Host- is the only cookie prefix that guarantees Path=/, Secure and no Domain,
# so a compromised sibling cannot overwrite the session. It requires Secure, which
# is why the plain name stays in use on a plain-HTTP development instance.
HOST_COOKIE = "__Host-" + COOKIE
COOKIE_NAMES = (HOST_COOKIE, COOKIE)
# Cookies authenticate requests, so an unsafe cross-site request is a CSRF attempt
# even though SameSite already refuses to attach the cookie in modern browsers.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TTL = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
_LOGIN_ATTEMPTS = defaultdict(deque)
_LOGIN_BUCKETS_MAX = 10_000
# Logout used to only expire the cookie in the browser that asked, so a copied
# value kept working for the rest of the 12h TTL. Values are stateless HMACs, so
# revocation is a bounded, self-expiring deny list. It is per worker: a multi-worker
# deployment must terminate sessions at its gateway as well (see the module note).
_REVOKED = {}
_REVOKED_MAX = 10_000


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


def api_docs_enabled():
    """Interactive API docs are development tooling, off unless asked for.

    /docs, /redoc and /openapi.json describe every route and payload to anyone who
    can reach the port, and Swagger UI pulls its CSS/JS from a third-party CDN, so
    they are opt-in and pass through the access gate when they are on.
    """
    return os.environ.get("TF_ENABLE_API_DOCS", "").strip().lower() in {"1", "true", "yes", "on"}


def signed_session(key):
    payload = str(int(time.time())) + "." + secrets.token_hex(16)
    return payload + "." + hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def session_value(headers):
    """The presented session cookie, __Host- name first, or None."""
    try:
        cookie = SimpleCookie()
        cookie.load(headers.get(b"cookie", b"").decode("latin-1"))
    except CookieError:
        return None
    for name in COOKIE_NAMES:
        if name in cookie:
            return cookie[name].value
    return None


def revoke(value):
    """Stop a logged-out value working, for as long as it could have lived."""
    if not value:
        return
    now = time.time()
    for known, expires in list(_REVOKED.items()):
        if expires <= now:
            _REVOKED.pop(known, None)
    _REVOKED[value] = now + TTL
    while len(_REVOKED) > _REVOKED_MAX:
        _REVOKED.pop(next(iter(_REVOKED)))


def is_revoked(value):
    expires = _REVOKED.get(value)
    if expires is None:
        return False
    if expires <= time.time():
        _REVOKED.pop(value, None)
        return False
    return True


def _secure_request(request):
    """Whether the browser reached us over HTTPS (directly or via a TLS proxy)."""
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def _configured_origins():
    extra = os.environ.get("TF_ALLOWED_ORIGINS", "")
    return {urlparse(part.strip()).netloc for part in extra.split(",") if part.strip()}


def cross_site_request(scope):
    """True when the browser says this request came from another site.

    Origin is authoritative and always sent on cross-site POST/PUT/PATCH/DELETE;
    Referer is the fallback for the rare client that omits it and is spoofable by
    non-browser callers, who cannot obtain the cookie in the first place.
    """
    headers = dict(scope.get("headers", []))
    if headers.get(b"sec-fetch-site", b"").decode("latin-1").strip().lower() == "cross-site":
        return True
    host = headers.get(b"host", b"").decode("latin-1")
    source = headers.get(b"origin", b"").decode("latin-1").strip()
    if not source:
        source = headers.get(b"referer", b"").decode("latin-1").strip()
    if not source:
        # No Origin/Referer at all: a same-origin GET-style form post, curl, or a
        # privacy extension. Nothing to compare, and the cookie check already ran.
        return False
    if source.lower() == "null":
        # Sandboxed iframe or data: URL: no usable origin, so never trusted.
        return True
    netloc = urlparse(source).netloc
    if not netloc:
        return False
    return netloc != host and netloc not in _configured_origins()


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
    value = session_value(headers)
    if value is None:
        return False
    try:
        stamp, nonce, signature = value.split(".")
        age = time.time() - int(stamp)
        payload = stamp + "." + nonce
        expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not (0 <= age <= TTL and hmac.compare_digest(signature, expected)):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    return not is_revoked(value)


def _gated(path):
    if path.startswith(("/api/", "/ws/")):
        return True
    if api_docs_enabled():
        return path == "/openapi.json" or path == "/docs" or path.startswith(("/docs/", "/redoc"))
    return False


def _csrf_guard(scope):
    """Unsafe request authenticated by the cookie: reject a foreign site.

    Bearer-authenticated requests are exempt by design: a custom Authorization
    header cannot be attached cross-site without a preflight, and no CORS
    middleware exists to grant one.
    """
    if scope.get("type") != "http" or scope.get("method") not in UNSAFE_METHODS:
        return False
    if not token():
        return False
    headers = dict(scope.get("headers", []))
    if headers.get(b"authorization", b"").startswith(b"Bearer "):
        return False
    if session_value(headers) is None:
        return False
    return cross_site_request(scope)


class AccessGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] in {"http", "websocket"} and _gated(path):
            public = path in {"/api/auth/status", "/api/auth/login", "/api/health"}
            if not public and not allowed(scope):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"detail": "Authentication required"}, status_code=401)(scope, receive, send)
                return
            if not public and _csrf_guard(scope):
                await JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)(scope, receive, send)
                return
            if scope["type"] == "websocket" and token():
                # Browser websocket cookies must not authorize a cross-origin page.
                if cross_site_request(scope):
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
    secure = _secure_request(request)
    # SameSite stays Strict: this dashboard has no "arrive signed in from an
    # external link" flow, so there is no UX reason to weaken it to Lax.
    response.set_cookie(HOST_COOKIE if secure else COOKIE, signed_session(key),
                        httponly=True, secure=secure, samesite="strict", path="/", max_age=TTL)
    if secure:
        # Clear a pre-hardening cookie so a browser cannot hold both names.
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    # Expiring the cookie only removes it from the browser that asked; revoking the
    # value stops a copy of it working for the rest of the TTL.
    revoke(session_value(dict(request.scope.get("headers", []))))
    secure = _secure_request(request)
    response = JSONResponse({"authenticated": False})
    for name in COOKIE_NAMES:
        response.delete_cookie(name, path="/", secure=secure or name == HOST_COOKIE,
                               httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response
