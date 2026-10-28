"""Security header, session-cookie and cross-site-request regressions (UT-HDR-*).

These assert the properties a reader of the review document is asked to trust:
the baseline headers exist on a page and on an API response, the session cookie is
HttpOnly/Secure/SameSite/Path-scoped under the __Host- prefix, a foreign Origin on
an unsafe method is refused, logout revokes the value server-side, and the
interactive API documentation is not served unless it is asked for.
"""
import base64
import hashlib
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.bootstrap import ensure_test_env
ensure_test_env()
from fastapi.testclient import TestClient

from app.main import app
from app import access_control
from app.security_headers import STATIC_DIR, content_security_policy, inline_script_hashes

# Deliberately independent of the module's own pattern: a test that reuses the
# implementation's regex cannot catch a bug in it.
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.I | re.S)
BASELINE = ("content-security-policy", "x-content-type-options", "referrer-policy",
            "x-frame-options", "permissions-policy")
# A credential-shaped *literal* key. Kept identical to the guard in
# .github/workflows/ci.yml so the two cannot drift: the key must start with a
# quote, which is how a token would be written, and is not how the UI's computed
# keys (sessionKey(projectId)) are passed.
STORAGE_CREDENTIAL = re.compile(
    r"""(localStorage|sessionStorage)\.(setItem|getItem|removeItem)\s*\(\s*['"`][^'"`]*"""
    r"""(token|jwt|auth|session|secret|api[_-]?key|credential|refresh|password)""", re.I)
STORAGE_ALLOWLIST = Path(__file__).resolve().parent.parent / ".security-storage-allowlist"


class SecurityHeaderTests(unittest.TestCase):
    def test_UT_HDR_01_baseline_headers_on_page_and_api(self):
        with TestClient(app) as client:
            for path in ("/", "/api/health"):
                headers = client.get(path).headers
                for name in BASELINE:
                    self.assertIn(name, headers, f"{name} missing on {path}")
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["referrer-policy"], "strict-origin-when-cross-origin")
                # Same-origin framing only: the dashboard embeds its own fixtures.
                self.assertEqual(headers["x-frame-options"], "SAMEORIGIN")
                self.assertIn("frame-ancestors 'self'", headers["content-security-policy"])

    def test_UT_HDR_02_policy_is_strict_and_hashes_every_inline_script(self):
        policy = content_security_policy()
        script_src = policy.split("script-src", 1)[1].split(";", 1)[0]
        self.assertNotIn("'unsafe-inline'", script_src)
        self.assertNotIn("'unsafe-eval'", script_src)
        self.assertNotIn("*", policy)
        for directive in ("object-src 'none'", "base-uri 'none'", "form-action 'self'"):
            self.assertIn(directive, policy)
        # Every inline block in every shipped page is allowlisted by its own hash,
        # so a stricter policy cannot silently disable the dashboard.
        seen = 0
        for page in sorted(STATIC_DIR.glob("*.html")):
            for match in INLINE_SCRIPT.finditer(page.read_text(encoding="utf-8")):
                digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
                self.assertIn("'sha256-" + base64.b64encode(digest).decode() + "'", policy,
                              f"inline script in {page.name} is not in script-src")
                seen += 1
        self.assertGreaterEqual(seen, 7)
        self.assertIn("'sha256-", inline_script_hashes()[0])

    def test_UT_HDR_03_stricter_route_policy_is_not_overwritten(self):
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route
        from app.security_headers import SecurityHeaders

        async def report(request):
            # The generated report/PDF pages ship their own, stricter policy.
            return Response("<p>report</p>", media_type="text/html",
                            headers={"Content-Security-Policy": "default-src 'none'; img-src data:"})

        strict = Starlette(routes=[Route("/report", report)])
        strict.add_middleware(SecurityHeaders)
        with TestClient(strict) as client:
            headers = client.get("/report").headers
            self.assertEqual(headers["content-security-policy"], "default-src 'none'; img-src data:")
            # Everything the response left out is still filled in.
            self.assertEqual(headers["x-content-type-options"], "nosniff")
            self.assertEqual(headers["x-frame-options"], "SAMEORIGIN")

    def test_UT_HDR_04_no_store_on_api_and_hsts_only_over_https(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/health").headers.get("cache-control"), "no-store")
            self.assertNotIn("strict-transport-security", client.get("/").headers)
        with TestClient(app, base_url="https://testserver") as client:
            headers = client.get("/").headers
            self.assertIn("max-age=31536000", headers.get("strict-transport-security", ""))
            self.assertIn("upgrade-insecure-requests", headers["content-security-policy"])

    def test_UT_HDR_05_csp_report_only_switch_downgrades_the_header(self):
        from app.security_headers import SecurityHeaders
        with patch.dict(os.environ, {"TF_CSP_REPORT_ONLY": "1"}):
            middleware = SecurityHeaders(app)
            self.assertEqual(middleware._csp_header, "Content-Security-Policy-Report-Only")
        with patch.dict(os.environ, {"TF_CSP_REPORT_ONLY": ""}):
            self.assertEqual(SecurityHeaders(app)._csp_header, "Content-Security-Policy")


class SessionCookieTests(unittest.TestCase):
    def setUp(self):
        access_control._LOGIN_ATTEMPTS.clear()
        access_control._REVOKED.clear()

    def tearDown(self):
        access_control._LOGIN_ATTEMPTS.clear()
        access_control._REVOKED.clear()

    def test_UT_HDR_06_cookie_attributes_over_https(self):
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN': 'a' * 40}):
            with TestClient(app, base_url="https://testserver") as client:
                response = client.post('/api/auth/login', json={'token': 'a' * 40})
                self.assertEqual(response.status_code, 200)
                cookie = response.headers['set-cookie'].lower()
                self.assertIn('__host-tf_session=', cookie)
                self.assertIn('httponly', cookie)
                self.assertIn('secure', cookie)
                self.assertIn('samesite=strict', cookie)
                self.assertIn('path=/', cookie)
                self.assertNotIn('domain=', cookie)
                self.assertEqual(response.headers.get('cache-control'), 'no-store')

    def test_UT_HDR_07_cookie_attributes_on_plain_http(self):
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN': 'a' * 40}):
            with TestClient(app) as client:
                response = client.post('/api/auth/login', json={'token': 'a' * 40})
                cookie = response.headers['set-cookie'].lower()
                # __Host- requires Secure, which a plain-HTTP development instance
                # cannot honestly claim; every other attribute still applies.
                self.assertIn('tf_session=', cookie)
                self.assertIn('httponly', cookie)
                self.assertIn('samesite=strict', cookie)
                self.assertIn('path=/', cookie)

    def test_UT_HDR_08_logout_revokes_the_value_server_side(self):
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN': 'a' * 40}):
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/auth/login', json={'token': 'a' * 40}).status_code, 200)
                stolen = client.cookies.get(access_control.COOKIE)
                self.assertTrue(stolen)
                self.assertEqual(client.get('/api/projects').status_code, 200)
                self.assertEqual(client.post('/api/auth/logout').status_code, 200)
                client.cookies.clear()
                # A copy of the value made before logout no longer authenticates.
                hijack = TestClient(app)
                hijack.cookies.set(access_control.COOKIE, stolen)
                self.assertEqual(hijack.get('/api/projects').status_code, 401)


class CrossSiteRequestTests(unittest.TestCase):
    def setUp(self):
        access_control._LOGIN_ATTEMPTS.clear()
        access_control._REVOKED.clear()

    def tearDown(self):
        access_control._LOGIN_ATTEMPTS.clear()
        access_control._REVOKED.clear()

    def login(self):
        client = TestClient(app, base_url="https://testserver")
        client.post('/api/auth/login', json={'token': 'a' * 40})
        return client

    def test_UT_HDR_09_foreign_origin_on_unsafe_method_is_refused(self):
        with patch.dict(os.environ, {'TF_ACCESS_TOKEN': 'a' * 40}):
            client = self.login()
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                response = client.request(method, '/api/projects', headers={'Origin': 'https://evil.example'},
                                          json={'name': 'x'})
                self.assertEqual(response.status_code, 403, f"{method} {response.text}")
            self.assertEqual(client.post('/api/projects', headers={'Origin': 'https://testserver'},
                                         json={'name': 'x'}).status_code != 403, True)
            # Referer is the fallback when a client omits Origin.
            self.assertEqual(client.post('/api/projects', headers={'Referer': 'https://evil.example/x'},
                                         json={'name': 'x'}).status_code, 403)
            self.assertEqual(client.post('/api/projects', headers={'Sec-Fetch-Site': 'cross-site'},
                                         json={'name': 'x'}).status_code, 403)
            # Safe methods and the bearer path keep working.
            self.assertEqual(client.get('/api/projects', headers={'Origin': 'https://evil.example'}).status_code, 200)
            self.assertEqual(client.post('/api/projects', headers={'Origin': 'https://evil.example',
                                                                   'Authorization': 'Bearer ' + 'a' * 40},
                                         json={'name': 'x'}).status_code != 403, True)

    def test_UT_HDR_10_unauthenticated_requests_are_untouched_by_the_origin_check(self):
        with TestClient(app) as client:
            # No token configured and no cookie: nothing to protect, nothing refused.
            self.assertEqual(client.post('/api/projects', headers={'Origin': 'https://evil.example'},
                                         json={'name': 'x'}).status_code != 403, True)


class ExposedToolingTests(unittest.TestCase):
    def test_UT_HDR_11_api_documentation_is_off_by_default(self):
        with TestClient(app) as client:
            for path in ('/docs', '/redoc', '/openapi.json'):
                self.assertEqual(client.get(path).status_code, 404, path)

    def test_UT_HDR_12_enabled_documentation_goes_through_the_access_gate(self):
        with patch.dict(os.environ, {'TF_ENABLE_API_DOCS': '1'}):
            self.assertTrue(access_control.api_docs_enabled())
            for path in ('/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json'):
                self.assertTrue(access_control._gated(path), path)
            self.assertTrue(access_control._gated('/api/projects'))
        with patch.dict(os.environ, {'TF_ENABLE_API_DOCS': ''}):
            self.assertFalse(access_control.api_docs_enabled())
            self.assertFalse(access_control._gated('/docs'))
            self.assertTrue(access_control._gated('/api/projects'))


class StorageGuardTests(unittest.TestCase):
    def test_UT_HDR_13_no_credentials_in_web_storage(self):
        allowlist = STORAGE_ALLOWLIST
        self.assertTrue(allowlist.exists())
        allowed = [line.strip() for line in allowlist.read_text(encoding='utf-8').splitlines()
                   if line.strip() and not line.startswith('#')]
        offenders = []
        for path in sorted(STATIC_DIR.glob('*')):
            if path.suffix not in {'.js', '.html'} or path.name.startswith('tailwind'):
                continue
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if STORAGE_CREDENTIAL.search(line) and not any(key in line for key in allowed):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "credentials must live in memory or an HttpOnly cookie")
