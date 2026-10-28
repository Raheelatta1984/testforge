# Credential storage, XSS, cookie, header and supply-chain review — 2026-10-28

Scope: one vulnerability class — credentials readable by JavaScript (and the controls that make
that attack possible or survivable). Every finding cites `path:line`. Commands and trimmed output
are in the appendix.

Repository: `Raheelatta1984/testforge` · default branch `main` · work branch
`arena/5b95ea63-testforge` (the session branch; no separate `security/...` branch was created).

## 1. Applicability verdict

### Tier A — credential exposure: **PARTIALLY APPLICABLE, with one DECISION-REQUIRED item**

Searched (`grep -rniE` over `app/`, `tools/`, excluding `app/static/vendor/` and build output):

| Check | Result |
| --- | --- |
| `localStorage` / `sessionStorage` credential writes | **no credential hits.** 26 storage calls, all UI state: active project (`tf_project`), guide dismissal (`tf_guide*`), execution speed, Azure step checklist, import preview (`tf_import_session_<id>`). No token/JWT/session/secret/api-key key exists. |
| `AsyncStorage`, IndexedDB, `localforage`, `idb-keyval` | no matches. |
| `document.cookie =` / `Cookies.set` / `js-cookie` / `universal-cookie` | **no matches.** The one cookie in the product is set server-side. |
| `Bearer` built from web storage | no matches. `app/static/import-components.js:755` holds the typed access token in React state only (`useState('')`), clears it after login, and the panel says so at `:764`. |
| Tokens in URLs, `__NEXT_DATA__`-style HTML state, redux-persist | no matches (no SPA framework persistence layer). |
| Vendor auth SDKs (Auth0/Firebase/Supabase/MSAL/NextAuth/Clerk) | none installed — `requirements.txt` is FastAPI + SQLAlchemy + Playwright, `tools/ui/package.json` is React/Tailwind only. |

So the classic "JWT in localStorage" defect **does not exist here**. What does exist is the
single-operator session cookie and its surrounding controls:

- `app/access_control.py:132-136` — `tf_session` is set with `httponly=True`, `samesite="strict"`,
  `secure` derived from the request scheme, `max_age=TTL` (12h), but **no explicit `Path`**, and
  **no `__Host-` prefix** even when it is Secure.
- `app/access_control.py:140-143` — logout only expires the cookie in the browser. The cookie value
  is a stateless HMAC (`stamp.nonce.signature`, `:57-59`), so **a copied cookie keeps working for
  up to 12 hours after logout**; there is no server-side revocation.
- `app/access_control.py:88-110` — the gate validates the cookie but performs **no Origin/Referer
  check on unsafe HTTP methods**. Only the WebSocket path (:103-109) compares Origin to Host.
  `SameSite=Strict` is the only CSRF control, and it is the sole one.
- No CORS middleware exists anywhere (`grep -rn CORSMiddleware|Access-Control-Allow app/` → 0
  matches), so cross-origin browser calls are already impossible. That is a protection to keep,
  not to add to.

**DECISION REQUIRED (recorded, not implemented):** `Authorization: Bearer <TF_ACCESS_TOKEN>`
(`app/access_control.py:71-73`) is accepted from any client, and the token is the *deployment*
secret rather than a per-user credential. Non-browser consumers (CI, `curl`, the harness) use it.
Removing it, or moving the browser login to a per-user session store, would change the deployment
contract for consumers this run cannot verify (Render blueprint, Azure VM guide, CI scripts).
Proposal in §5.

### Tier B — defence in depth: **APPLICABLE (and the biggest gap in this repository)**

- **No security headers on any response.** `app = FastAPI(...)` at `app/main.py:58` is created with
  defaults and no header middleware; `App` has exactly two middlewares (`AccessGate` at
  `app/main.py:60`, `RateLimit`-style decorators elsewhere). Runtime probe (appendix A.10) shows
  `/`, `/api/health`, `/docs`, `/openapi.json`, `/redoc` return **only `content-type`** — no CSP,
  no `X-Content-Type-Options`, no `Referrer-Policy`, no `X-Frame-Options`, no
  `Permissions-Policy`, no HSTS.
- **Interactive API docs and the OpenAPI schema are public.** `/docs`, `/redoc`,
  `/openapi.json` return 200 with no authentication even when `TF_ACCESS_TOKEN` is configured,
  because `AccessGate` only guards paths starting with `/api/` or `/ws/`
  (`app/access_control.py:87`). Swagger UI additionally loads CSS/JS from
  `https://cdn.jsdelivr.net` (runtime probe, appendix A.10) — an un-pinned third-party script on
  this origin, with no SRI.
- **XSS surface is small and mostly structural, not per-call-site.**
  - `app/static/index.html:2891-2903` (`inlineMd`) builds React elements with text children, so the
    markdown renderer escapes by construction; link hrefs come from `docs/*.md` (repository
    content, not user input), and React does not block a `javascript:` href there — Info severity.
  - `app/static/index.html:1183,1476` put server-supplied `data:image/png|jpeg;base64,…` into
    `<img src>`. Low risk, but only a restrictive `img-src` keeps it that way.
  - `app/static/erp-crm-demo.html:65` defines `el(html){…innerHTML=html}` and every call site
    (`:67-133`) passes a **string literal**. Static, so not user-controlled — Info.
  - No `eval`, `new Function`, `document.write`, `dangerouslySetInnerHTML`, `v-html`.
  - No inline `on<event>=` attributes anywhere. The single `onclick` assignment
    (`app/static/domain-demo.html:22`) is done from JavaScript, which CSP does not block.
- **CSP is feasible without `'unsafe-inline'` in `script-src`.** Each of the seven shipped pages
  has exactly one inline `<script>` block and no external origins; hashing them is mechanical.
  `style-src` needs `'unsafe-inline'` (one inline `<style>` block per page plus React inline style
  props) — allowed by the brief, never in `script-src`.
- **Framing policy must allow same-origin.** `app/static/demo-components.js:61` and
  `app/static/index.html:1046` embed this instance's own demo fixtures in `<iframe>`s, so
  `frame-ancestors 'none'`/`X-Frame-Options: DENY` would break the preview feature.
  `frame-ancestors 'self'` + `SAMEORIGIN` gives the same clickjacking protection against other
  origins.
- **`Permissions-Policy` must not break features the app uses**: speech recognition
  (`app/static/demo-components.js:41`, `app/static/index.html:277` → microphone) and the clipboard
  (`app/static/qa-agents-components.js:110`, `scenario-components.js:24`). `microphone` and
  `clipboard-write` are therefore left at their default.
- Per-route CSP already exists for generated artefacts and is **stricter** than a global policy:
  `app/report_routes.py:80` and `app/market_routes.py:89-91` (`default-src 'none'`). A global
  middleware must not overwrite them.

### Tier C — supply chain: **APPLICABLE**

- Direct dependencies are pinned (`==`) in `requirements.txt:5-27`, but **no hash pinning**, and
  there is no lockfile for Python at all (`git ls-files` → only `tools/ui/package-lock.json`).
- CI (`.github/workflows/ci.yml`) installs with `pip install -r` (not hash-verified), pins **all
  five `uses:` by tag** (`actions/checkout@v4` etc.), and runs **no dependency audit**.
- No Dependabot/Renovate configuration.
- `pip-audit -r requirements.txt` currently reports **203 advisories across 5 packages**
  (`python-multipart 0.0.20`, `pillow 11.0.0`, `pypdf 5.1.0`, `cryptography 46.0.3`,
  `starlette 0.41.3`) — appendix A.13.
- No `.env` file is tracked (`git ls-files | grep -i '\.env'` → none; `.gitignore:10-14`).

## 2. Findings

| # | Severity | Finding | Evidence | Action | Tier |
|---|---|---|---|---|---|
| 1 | Medium | Session cookie has no `__Host-` prefix when Secure, and no explicit `Path`, so a sibling path/subdomain can shadow or overwrite it | `app/access_control.py:135` | Set `__Host-tf_session` with `path="/"` on HTTPS; accept the legacy name during migration | A |
| 2 | Medium | Logout does not revoke the session server-side; a copied cookie stays valid for the full 12h TTL | `app/access_control.py:57-59,140-143` | Bounded in-process revocation list checked in `allowed()` | A |
| 3 | Medium | No Origin/Referer validation on unsafe HTTP methods; `SameSite` alone is the only CSRF control | `app/access_control.py:87-110` | Reject cross-site `POST/PUT/PATCH/DELETE` that are authenticated by the session cookie | A |
| 4 | Medium | Interactive API docs + OpenAPI schema public, unauthenticated, and Swagger UI loads scripts from `cdn.jsdelivr.net` without SRI | `app/main.py:58`, `app/access_control.py:87`; probe A.10 | Disable by default (`TF_ENABLE_API_DOCS=1` to enable) and gate them behind `AccessGate` when enabled | B |
| 5 | Medium | No security headers at all (CSP, nosniff, Referrer-Policy, frame policy, Permissions-Policy, HSTS) | probe A.10; `app/main.py:58` | New `app/security_headers.py` ASGI middleware | B |
| 6 | Low | `X-Frame-Options`/`frame-ancestors` absent, so any site can frame this dashboard (clickjacking on recorder/runner controls) | probe A.10 | `frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN` (self-framing demos kept working) | B |
| 7 | Low | `Cache-Control: no-store` is set only on individual routes; most API responses are cacheable by intermediaries | `app/access_control.py:137`, `app/market_routes.py:102` vs. all other routes | Apply `no-store` to every `/api/` response | B |
| 8 | Low | Compiled HTML/SQL/XLSX export of run evidence is served without `no-store` except on some routes | `app/main.py:2439-2443` | Covered by finding 7 | B |
| 9 | Low | Markdown renderer passes link targets straight into `href` (`javascript:` would render), source is repo docs only | `app/static/index.html:2898-2899` | Documented; CSP `script-src` without `unsafe-inline`/`unsafe-eval` and `base-uri 'none'` blunt it. Not rewritten (behavioural risk outweighs benefit) | B |
| 10 | Low | CI actions pinned by tag, no dependency audit, no Dependabot, Python deps unhashed | `.github/workflows/ci.yml:24-68` | SHA pins with version comments, `pip-audit` gate, Dependabot for pip/npm/actions | C |
| 11 | Low | 203 advisories across 5 pinned dependencies (starlette 0.41.3 is pulled in by fastapi 0.115.6) | appendix A.13 | Leaf-library upgrades applied where the suite stays green; remainder recorded as an explicit, dated allowlist | C |
| 12 | Info | `innerHTML` helper in the ERP/CRM demo fixture | `app/static/erp-crm-demo.html:65` | Static string literals only; no change | B |
| 13 | Info | Single-operator token remains accepted as `Authorization: Bearer` | `app/access_control.py:71-73` | Decision required, see §5 | A |

## 3. Plan (implemented in this change)

**Tier A**
- `app/access_control.py`
  - `__Host-tf_session` on Secure connections (`path="/"`, no `Domain`); the legacy `tf_session`
    name is still *read* so existing sessions survive the rollout, and both names are cleared on
    logout. `SameSite=Strict` is kept: this dashboard has no external-link sign-in flow, so the
    brief's Lax default would trade a real protection for a UX problem that does not exist here.
  - Bounded, expiring revocation list (`_REVOKED`, max 10k) populated by logout, checked in
    `allowed()`.
  - Origin/Referer + `Sec-Fetch-Site` validation for cookie-authenticated unsafe methods.
    Bearer-authenticated requests are exempt (a custom header cannot be sent cross-site without a
    preflight, and no CORS middleware exists). Requests with no session cookie are unaffected.
  - No CORS middleware is added; none was removed.
- No browser-side storage change is required: nothing credential-like is stored in web storage
  today, and the login token already lives in memory only.

**Tier B**
- New `app/security_headers.py`: ASGI middleware adding CSP (hash-allowlisted inline scripts),
  `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `frame-ancestors`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, HSTS on HTTPS only, and `no-store` on `/api/`.
  Existing stricter CSPs (report/market routes) are preserved — the middleware only adds headers
  that are absent.
- `app/main.py`: docs/redoc/openapi disabled unless `TF_ENABLE_API_DOCS=1`; those paths join the
  access gate when enabled.

**Tier C**
- `.github/workflows/ci.yml`: Actions pinned to 40-char SHAs with `# vX` comments, a `pip-audit`
  job, and a regression guard that fails if a credential-shaped web-storage call appears in client
  code.
- `.github/dependabot.yml` for pip, npm (`tools/ui`) and GitHub Actions.
- Dependency upgrades for the leaf libraries where the full suite stays green.

**Tests** — `tests/test_security.py` (`UT-HDR-01…13`), registered in `tests/SCENARIOS.md`: headers on
the root page and on an API route, cookie attributes on login, foreign-Origin rejection, docs
gating, CSP hash coverage of every inline script, revocation on logout.

## 4. Deferred

| Item | Why |
| --- | --- |
| Per-user sessions / BFF with refresh rotation | Tier A "decision required": single-operator design; would break CI and CLI consumers (§5) |
| Upgrade `fastapi`/`starlette` | Requires a framework move (fastapi 0.115 pins `starlette<0.42`); 200+ advisories in this environment's advisory DB are addressed by the leaf upgrades plus a dated allowlist |
| Nonce-based CSP | Hashing is deterministic here and avoids middleware rewriting HTML bodies |
| `Cross-Origin-Embedder-Policy` | `require-corp` would break the `data:`/`blob:` preview frames for no gain at this threat model |

## 5. Tier A decision proposal (not implemented)

Current: one shared deployment token, exchanged at `POST /api/auth/login` for a stateless HMAC
cookie. Non-browser clients keep using `Authorization: Bearer`.

Proposal for a later change:
1. Issue an opaque 256-bit session id server-side (`sessions` table: id hash, issued_at, expires_at,
   revoked_at), cookie `__Host-tf_session`, 12h absolute / 1h idle timeout, rotation on privilege
   change.
2. Keep the Origin check (already added here) plus `SameSite=Strict`.
3. Keep `Authorization: Bearer` for CI/CLI indefinitely; the browser path is the one that changes.
4. Migration: dual-read (opaque id, then legacy HMAC) for one release; one forced re-login.
5. Grace period: `LEGACY_TOKEN_GRACE_DAYS` is 0 in this run, so no legacy bearer acceptance was
   *added*; existing bearer support is unchanged.

## 6. Verification summary

- **Tests**: 503 tests, `FAILED (failures=1, skipped=17)` — byte-identical to the baseline
  (490 tests, same single failure in `tests/test_scale.py:122`, a cache-decorator timing assertion
  that predates this change). 13 new tests (`UT-HDR-01…13`) pass.
- **Headers at runtime**: CSP (hash allowlisted), `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` on `/`, `/api/health` and
  `/demo.html`; `Cache-Control: no-store` on `/api/`; HSTS + `upgrade-insecure-requests` only when
  the request is HTTPS.
- **Cookie at runtime**: `__Host-tf_session=…; HttpOnly; Secure; SameSite=strict; Path=/; Max-Age=43200`
  behind a TLS proxy, `tf_session` with the same attributes minus `Secure`/prefix on plain HTTP.
- **CSRF probe**: foreign `Origin` POST → 403; `Sec-Fetch-Site: cross-site` → 403; same-origin POST
  → 201; foreign-Origin GET → 200; bearer + foreign Origin → 201 (custom header, exempt by design).
- **Revocation**: cookie replayed after logout → 401; bearer unaffected → 200.
- **Docs**: `/docs`, `/redoc`, `/openapi.json` → 404.
- **Audit**: `pip-audit -r requirements.txt` → no findings after the leaf upgrades; 7 starlette
  advisories are accepted in `.github/security/pip-audit-allowlist.txt` with a reason each.
- **Browser check**: no Playwright/Cypress harness runs the dashboard in CI (only the recording
  harness), and this sandbox cannot download a Chromium build, so the CSP was verified against the
  shipped markup (hashes recomputed from disk) rather than by watching a real console. See
  "Deferred" for the follow-up.
- **CI was already red at the base commit** — appendix B.6. Both pre-existing failures were then
  fixed so the pipeline can go green: one was a real cache bug, one was missing fixture data
  (appendix B.7).

## Appendix A — Evidence

Trimmed command output. `EXCL` = the exclusion list from the review brief (node_modules, vendor,
dist, build, .next, coverage, venv, __pycache__, target, out); `app/static/vendor/` is the only
vendored tree in this repository (React + Tailwind builds, tracked).

**A.1 Stack.** FastAPI 0.115.6 + Starlette 0.41.3 (server), `app/static/index.html` + 11 plain JS
component files (browser, React 18 from `app/static/vendor`, no bundler), SQLAlchemy 2 (SQLite /
Postgres / SQL Server), Playwright 1.49.1 for the recorder/runner. No Next/Nuxt/SvelteKit, no
mobile client, no auth SDK. Docker (`Dockerfile`), Render blueprint (`render.yaml`), no nginx
configs in the repository.

**A.2 Web storage.** `grep -rniE '(localStorage|sessionStorage)\.(setItem|getItem|removeItem)'`
over `app/static` (excluding vendor) → 26 calls in `index.html` (15), `import-components.js` (7),
`demo-components.js` (4), `future-components.js` (2), `devdeploy-components.js` (2). Keys:
`tf_project`, `tf_pending_recorder_url`, `tf_live_window`, `tf_guide*`, `tf_azure_steps`,
`tf_execution_speed`, `tf_devkit_done`, `tf_import_session_<id>`. No credential-shaped key.

**A.3 Sinks.** `innerHTML` only at `app/static/erp-crm-demo.html:65` (helper) with literal
arguments at `:67,79,96,99,110,122,127,130,133`; `eval`/`new Function`/`document.write`/
`dangerouslySetInnerHTML`/`v-html`/`srcdoc` → 0 matches; inline `on<event>=` attributes → 0
matches (`domain-demo.html:22` assigns `.onclick` from a script, which CSP does not block).

**A.4 Cookies.** `grep -rnE 'set_cookie|delete_cookie|Set-Cookie' app tests` →
`app/access_control.py:135,143` only.

**A.5 CORS/CSRF.** `grep -rniE 'CORSMiddleware|Access-Control-Allow|allow_origins' app` → 0
matches. Before this change the only origin check was the WebSocket comparison at
`app/access_control.py:103-109`.

**A.6 Existing headers.** Only on generated artefacts: `app/report_routes.py:80`,
`app/market_routes.py:89-91` (`default-src 'none'; … frame-ancestors 'none'`), plus
`X-Content-Type-Options: nosniff` at `app/intel_routes.py:233,246`.

**A.7 Inline scripts hashed for the CSP** (recomputed at import from the same files):

| page | inline bytes | sha256 |
| --- | --- | --- |
| `crm-demo.html` | 907 | `LdmMSsZK756KFtaxkjC+bezP/HDgtIICesiaxpKNkps=` |
| `demo.html` | 210 | `7UIQZln4Y8haU+6Q8th3rW/EMNd49/6qCPfiO/QPEks=` |
| `domain-demo.html` | 1470 | `a+gt/30FKsexZ9BPntuvw5ErZz1K05ta2dTMMgQPdc8=` |
| `erp-crm-demo.html` | 10820 | `SoguWCdnBzKypc/aBsRaA9loE3+XTSIvhJnjLUnD16I=` |
| `erp-demo.html` | 687 | `w02chDOEM8LAObZLFxOqPAx2kwuFCL8W1geyspNYc94=` |
| `index.html` | 203770 | `sKLZ0/nhdmfHCGI+8bkzvovc7BtTToSONQzNBB9tnvE=` |
| `mobile-zebra-demo.html` | 2009 | `t1s11yQX6jAQ0FZK85jOiuU5YNQeni2U5UjOyz0Lc34=` |

**A.8 Third-party origins.** `grep -rhoE '(src|href)="https?://[^"]+"' app/static/*.html
app/static/*.js` → 0 matches; `fetch('http…')` → 0 matches. The only external asset in the product
was Swagger UI's CDN bundle, removed with the docs route (A.10).

**A.9 Secrets.** `git ls-files | grep -iE '(^|/)\.env'` → none; `.gitignore:10-14` excludes
`.env*`, `*.pem`, `*.key`. No credential value is printed by the app (`app/errors.py` redacts).

**A.10 Runtime probe, before the change** (`uvicorn app.main:app`, `TF_ACCESS_TOKEN` unset):

```
== /            HTTP/1.1 200 OK      content-type: text/html; charset=utf-8
== /api/health  HTTP/1.1 200 OK      content-type: application/json
== /docs        HTTP/1.1 200 OK      content-type: text/html; charset=utf-8
== /openapi.json HTTP/1.1 200 OK     content-type: application/json
== /redoc       HTTP/1.1 200 OK      content-type: text/html; charset=utf-8
```

(no CSP, nosniff, Referrer-Policy, X-Frame-Options, Permissions-Policy or HSTS on any of them, and
`/docs` renders `<script src="https://cdn.jsdelivr.net/npm/…">`.)

**A.11 Runtime probe, after the change** — see §6 and appendix B.

**A.12 Supply chain.** `git ls-files` lockfiles → `tools/ui/package-lock.json` only; no Python
lock or hashes. CI `uses:` pins → 5 tags (`actions/checkout@v4` ×3, `actions/setup-python@v5` ×3,
`actions/upload-artifact@v4` ×1); no audit step; no Dependabot/Renovate.

**A.13 Dependency audit.** Before: `pip-audit -r requirements.txt` → "Found 203 known
vulnerabilities in 5 packages" (python-multipart 0.0.20, pillow 11.0.0, pypdf 5.1.0,
cryptography 46.0.3, starlette 0.41.3). After upgrading the four leaf packages (and after
trialling and rejecting a fastapi jump, see the allowlist): 7 advisories remain, all starlette,
all unreachable without the framework upgrade; `pip-audit` with the allowlist → "No known
vulnerabilities found, 14 ignored", exit 0.

## Appendix B — Verification detail

**B.1 Baseline (before any edit).** `python -m unittest discover -s tests -p "test_*.py" -t .` →
`Ran 490 tests … FAILED (failures=1, skipped=17)`, failing
`tests.test_scale.CacheTests.test_SC_22_get_or_load_and_decorator` (`2 != 1`). `python -m
tools.market_audit` → PASS.

**B.2 After the change.** `Ran 503 tests … FAILED (failures=1, skipped=17)` — the same single
pre-existing failure, plus 13 new passing tests. `python -m tools.market_audit` → PASS.

**B.3 Runtime (app restarted with the change, `TF_ACCESS_TOKEN` = 40 chars).**

```
== /            content-security-policy: default-src 'self'; script-src 'self' 'sha256-sKLZ0/…' …;
                x-content-type-options: nosniff; referrer-policy: strict-origin-when-cross-origin;
                x-frame-options: SAMEORIGIN; permissions-policy: …; cross-origin-opener-policy: same-origin
== /api/health  … same headers … ; cache-control: no-store
== /demo.html   … same headers …
docs probes: /docs -> 404   /redoc -> 404   /openapi.json -> 404
login over http:  set-cookie: tf_session=…; HttpOnly; Max-Age=43200; Path=/; SameSite=strict
login via proxy:  set-cookie: __Host-tf_session=…; HttpOnly; Max-Age=43200; Path=/; SameSite=strict; Secure
GET /api/projects (cookie)              -> 200
foreign-origin POST                     -> 403
same-origin POST                        -> 201
Sec-Fetch-Site: cross-site POST         -> 403
foreign-origin GET                      -> 200
bearer + foreign-origin POST            -> 201
pre-logout cookie replayed after logout -> 401
bearer after logout                     -> 200
```

**B.4 Regression guard.** The CI step (identical regex to `UT-HDR-13`) is clean on the repository,
and fails on a planted `localStorage.setItem("auth_token", t)` line (verified, then removed).

**B.5 Behaviour changes users will notice**

- `__Host-tf_session` replaces `tf_session` on HTTPS deployments: **existing logged-in browsers
  are signed out once**, and their old cookie is cleared on the next login.
- `/docs`, `/redoc` and `/openapi.json` return 404 unless `TF_ENABLE_API_DOCS=1`.
- A POST/PUT/PATCH/DELETE authenticated by the session cookie and sent from another origin is now
  refused with 403. Same-origin UI, `curl`, the CLI (`tools/quality_gate.py`) and bearer-authenticated
  CI clients are unaffected.
- Logout invalidates the session for good (a copied cookie stops working).
- No front-end storage key changed; no data migration; the dashboard's own iframe previews still
  work (`frame-ancestors 'self'`).

**B.6 CI status at the base commit (red before this change).** GitHub Actions on `main` at the base
commit `335bad6` (run 40640767384) failed two jobs — "Unit suite (no browser)" and "Full harness
(with Chromium)" — while "Smoke plan on chromium, firefox and webkit" passed. Reproduced locally on
a worktree of `335bad6` with a clean virtualenv built from the same requirement files:
`Ran 490 tests … FAILED (failures=1, skipped=17)`, failing
`tests.test_scale.CacheTests.test_SC_22_get_or_load_and_decorator` (`2 != 1`). On this branch the
same clean virtualenv gives `Ran 503 tests … FAILED (failures=1, skipped=17)` with **only that test**
failing, so the 13 new tests pass and the change introduces no new failure. The harness job failed
on its first product-touching step in both runs and took 90 s at the base commit versus 83 s here:
the same early, pre-existing failure rather than a later scenario regression (a CSP problem would
surface mid-run, minutes in). The branch's own new `Dependency audit` job and the Dependabot config
check pass. CI job logs are served from blob storage this environment cannot reach, so the
per-test output above is quoted from the local reproduction in the identical dependency set rather
than from the Actions log.

**B.7 The two pre-existing failures, fixed.** Neither was caused by the security work, both
blocked the pipeline, and both were repaired rather than silenced:

1. **`test_SC_22_get_or_load_and_decorator` (unit job).** A real defect in `app/cache.py`: a miss
   was recorded in the negative cache under the raw key, but a write only cleared entries matching
   `key.split(":")[0] + ":"`. For a key with no `:` in it (`heavy`) that prefix is `heavy:`, which
   never matches `heavy`, so the recorded miss outlived the write and every later read answered
   "not cached" for the whole 30 s negative TTL — the loader ran again and `get_or_load()` stopped
   caching. `set()` and `delete()` now clear the key's own negative entry (plus its namespace), so a
   write is visible immediately.
2. **`LB-LIB-01` (harness job).** The harness project the catalog and `library/README.md` document
   was missing from the tree: `library/catalog.json` listed `qa-sample-app` and its two recordings,
   but `library/projects/qa-sample-app/` did not exist, so `GET /api/library` could not resolve it
   and every `--require-browser` run failed with "project qa-sample-app not in repository library".
   Restored `project.json`, `variables.json` (variable `user` = `Ada`), both recordings
   (`qa-hello-literal` → `Hello, Quinn`, `qa-hello-variable` → `Hello, {{user}}`) with their
   `resources/Jenkinsfile`, all matching what `tests/scenarios.json` replays and what
   `app/static/demo.html` renders. `UT-IMP-50` now asserts that shape without a browser.

With both fixed the local suite is `Ran 504 tests … OK (skipped=17)` and
`python -m tests.harness` reports `PASS 332 passed, 0 failed, 7 skipped` (the seven browser
scenarios skip because this environment has no Chromium; CI installs it).
