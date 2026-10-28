"""Security headers for every response the service emits.

The dashboard is a static single-page app (``app/static``), so its policy is
derived from those files instead of being hand-maintained here. Each page ships
exactly one inline ``<script>`` block and no third-party origin, which is what
makes a strict ``script-src`` possible without ``'unsafe-inline'``: the inline
blocks are allowlisted by SHA-256 hash, recomputed from disk at import time. Edit
a page and its hash changes with it, rather than the page silently losing its
script when the browser enforces the stale hash.

``style-src`` keeps ``'unsafe-inline'``: every page carries an inline ``<style>``
block and React sets ``style`` attributes (progress bars, viewport scaling).
There is no ``'unsafe-inline'`` or ``'unsafe-eval'`` in ``script-src`` anywhere.

Registered in ``app/main.py`` as the outermost middleware, so 401/403 responses
from the access gate and validation errors from FastAPI carry the same headers as
a normal page.
"""
import base64
import hashlib
import os
import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Inline <script> blocks only: a tag with no src attribute. DOTALL because the
# blocks are multi-line, and the body is hashed exactly as the browser sees it
# (no HTML entity decoding happens inside a <script> element).
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                            re.IGNORECASE | re.DOTALL)

# The application deliberately uses speech recognition (microphone) and the
# clipboard, so those two are left at their browser default; everything listed
# here is a capability this product never requests.
PERMISSIONS_POLICY = ("accelerometer=(), camera=(), display-capture=(), geolocation=(), "
                      "gyroscope=(), magnetometer=(), payment=(), usb=(), serial=(), "
                      "bluetooth=(), midi=(), xr-spatial-tracking=()")


def _truthy(value):
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def inline_script_hashes(directory=None):
    """Sorted CSP source expressions for every inline script in the shipped pages."""
    directory = Path(directory or STATIC_DIR)
    hashes = set()
    try:
        pages = sorted(directory.glob("*.html"))
    except OSError:
        return []
    for page in pages:
        try:
            html = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _INLINE_SCRIPT.finditer(html):
            digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
            hashes.add("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return sorted(hashes)


def content_security_policy(hashes=None, https=False):
    """The policy for HTML and API responses alike.

    frame-ancestors/X-Frame-Options are ``'self'``/SAMEORIGIN rather than
    ``'none'``/DENY on purpose: the dashboard previews the demo fixtures it ships
    with in same-origin <iframe>s (app/static/demo-components.js, index.html).
    Other origins still cannot frame this instance.
    """
    hashes = inline_script_hashes() if hashes is None else hashes
    script_src = " ".join(["'self'"] + list(hashes))
    policy = (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "media-src 'self' blob:; "
        "frame-src 'self'; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'self'"
    )
    if https:
        policy += "; upgrade-insecure-requests"
    return policy


def _is_https(scope):
    if scope.get("scheme") == "https":
        return True
    # Only a proxy that terminates TLS can say this, and a forged value here
    # cannot loosen anything except marking the cookie Secure on a plain-HTTP
    # request, which the deployment guide already rules out.
    for name, value in scope.get("headers", []):
        if name.lower() == b"x-forwarded-proto":
            return value.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


class SecurityHeaders:
    """Pure-ASGI middleware: adds headers at response start, never buffers a body.

    Streaming responses (live JPEG frames, zipped artefacts, run logs) are passed
    through chunk by chunk, so this adds no latency and no copy.
    """

    def __init__(self, app):
        self.app = app
        self.report_only = _truthy(os.environ.get("TF_CSP_REPORT_ONLY"))
        self._hashes = inline_script_hashes()
        self._policy = content_security_policy(self._hashes, https=False)
        self._policy_https = content_security_policy(self._hashes, https=True)
        self._csp_header = ("Content-Security-Policy-Report-Only" if self.report_only
                            else "Content-Security-Policy")

    def _static_headers(self, scope):
        https = _is_https(scope)
        yield self._csp_header, self._policy_https if https else self._policy
        yield "X-Content-Type-Options", "nosniff"
        yield "Referrer-Policy", "strict-origin-when-cross-origin"
        yield "X-Frame-Options", "SAMEORIGIN"
        yield "Permissions-Policy", PERMISSIONS_POLICY
        yield "Cross-Origin-Opener-Policy", "same-origin"
        if https:
            # HTTPS-only hostnames covered by the deployment guide; never sent on
            # plain HTTP, where it would pin a scheme the server does not serve.
            yield "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        if scope.get("path", "").startswith("/api/"):
            # Run evidence, exports and credentials-adjacent payloads must not sit
            # in a shared cache. Routes that already chose a value keep it.
            yield "Cache-Control", "no-store"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.get("headers")
                if headers is None:
                    headers = message["headers"] = []
                present = {name.lower() for name, _ in headers}
                for name, value in self._static_headers(scope):
                    key = name.lower().encode("latin-1")
                    if key in present:
                        # A route already decided (report/market pages send a
                        # stricter default-src 'none' policy). Never override it.
                        continue
                    headers.append((key, value.encode("latin-1")))
                    present.add(key)
            await send(message)

        await self.app(scope, receive, send_with_headers)
