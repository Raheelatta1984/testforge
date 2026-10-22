"""Zero-LLM, black-box discovery: crawl a URL, find defects, verify them.

This is TestForge's answer to "autonomously finds unknown bugs" without putting
a language model in the release path. It is deliberately deterministic:

* the crawl is bounded (pages, depth, seconds, bytes) and stays on one origin;
* every check is a pure function over a fetched :class:`Page`, so the same page
  always produces the same findings;
* destructive paths (logout, delete, pay, unsubscribe) are never followed;
* a finding is only marked **verified** when the same check reproduces on a
  fresh fetch, and the whole report is sealed with a SHA-256 receipt.

Two fetchers ship with it:

``HttpFetcher``
    Standard library only. Fetches HTML, parses links, forms, images and
    accessibility basics. Works in the 512 MB service with no browser and no
    new dependency. This is the default.
``PlaywrightFetcher``
    Opt-in (``TF_EXPLORER_BROWSER=1``). Adds rendered DOM, console errors and
    failed network requests. Imported lazily so a deployment without Playwright
    still runs the default mode.

The module never writes to the application database and never returns a
credential: captured text is bounded and scrubbed with the same redaction rules
the QA agents use.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, build_opener

from app.qa_agents.common import clip, scrub

MAX_PAGES = 25
MAX_DEPTH = 2
MAX_BYTES = 1_500_000
MAX_SECONDS = 60
MAX_VERIFICATIONS = 12
MAX_FINDINGS = 200
DEFAULT_TIMEOUT = 8.0
SLOW_PAGE_MS = 3000
MAX_BODY = 400_000

DESTRUCTIVE = re.compile(
    r"/(log[-_]?out|sign[-_]?out|delete|remove|destroy|reset|unsubscribe|cancel|"
    r"checkout|pay|purchase|transfer|deactivate|revoke)\b", re.I)
SKIP_ASSET = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|mjs|map|woff2?|ttf|eot|mp4|webm|mp3|zip|pdf|gz|xlsx?|docx?)$", re.I)
SKIP_SCHEME = ("mailto:", "tel:", "javascript:", "data:", "file:", "ftp:", "blob:")
ERROR_LEAK = re.compile(
    r"(Traceback \(most recent call last\)|SQLSTATE\[|Whitelabel Error Page|"
    r"Unhandled exception|Stack trace:|Fatal error:|Notice: Undefined|"
    r"System\.NullReferenceException|\bat [\w$.]+\([\w$.]+\.java:\d+\)|"
    r"ORA-\d{5}|Microsoft OLE DB Provider)", re.I)
PLACEHOLDER_HREF = re.compile(r"^(#|#.*|javascript:void\(0\);?|javascript:;?)$", re.I)


class ExplorerError(ValueError):
    """Bad or unsafe scan request (HTTP 400)."""


class FetchError(RuntimeError):
    """The target could not be fetched at all."""


# ---------------------------------------------------------------------------
# Data


@dataclass
class Page:
    url: str
    status: int = 0
    headers: dict = field(default_factory=dict)
    body: str = ""
    elapsed_ms: int = 0
    console: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)
    links: list = field(default_factory=list)
    images: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    forms: list = field(default_factory=list)
    ids: list = field(default_factory=list)
    interactive: list = field(default_factory=list)
    title: str = ""
    mode: str = "http"


@dataclass
class Finding:
    check: str
    category: str
    severity: str
    title: str
    detail: str
    url: str
    expected: str
    actual: str
    evidence: dict
    replay_steps: list
    signature: str
    verified: bool = False
    attempts: int = 0
    verification: str = "not checked"

    def identity(self) -> str:
        return hashlib.sha256(f"{self.check}|{self.signature}".encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "id": self.identity(), "check": self.check, "category": self.category,
            "severity": self.severity, "title": self.title, "detail": self.detail,
            "url": self.url, "expected": self.expected, "actual": self.actual,
            "evidence": self.evidence, "replay_steps": self.replay_steps,
            "signature": self.signature, "verified": self.verified, "attempts": self.attempts,
            "verification": self.verification,
        }


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


# ---------------------------------------------------------------------------
# URL safety


def _is_private_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_private or parsed.is_loopback or parsed.is_link_local or parsed.is_reserved:
            return True
    return False


def validate_target(url: str, allow_private: bool | None = None) -> str:
    """Normalise and vet a scan target. Rejects SSRF-shaped input by default."""
    value = str(url or "").strip()
    if not value:
        raise ExplorerError("Provide the URL to explore, for example https://shop.example.test")
    if len(value) > 2000:
        raise ExplorerError("URL is too long")
    explicit_scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", value)
    if explicit_scheme and "://" not in value:
        raise ExplorerError("Only http and https URLs can be explored")
    parts = urlsplit(value if "://" in value else "https://" + value)
    if parts.scheme not in ("http", "https"):
        raise ExplorerError("Only http and https URLs can be explored")
    if not parts.hostname:
        raise ExplorerError("URL has no host")
    if parts.username or parts.password:
        raise ExplorerError("Credentials must not be put in the URL; configure them on the target instead")
    if allow_private is None:
        allow_private = os.environ.get("TF_EXPLORER_ALLOW_PRIVATE", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not allow_private and _is_private_host(parts.hostname):
        raise ExplorerError(
            "Refusing to scan a loopback or private address. Set TF_EXPLORER_ALLOW_PRIVATE=1 only on a "
            "trusted, single-operator instance.")
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _crawlable(candidate: str, origin: str, seen: set[str]) -> bool:
    if not candidate or candidate.startswith(SKIP_SCHEME):
        return False
    if candidate.startswith("#"):
        return False
    if _origin(candidate) != origin:
        return False
    if SKIP_ASSET.search(urlsplit(candidate).path or ""):
        return False
    if DESTRUCTIVE.search(urlsplit(candidate).path or ""):
        return False
    if candidate in seen:
        return False
    return True


# ---------------------------------------------------------------------------
# HTTP fetcher (default; no browser, no dependency)


class _Parser(HTMLParser):
    """Small, forgiving HTML collector: links, images, forms, ids, controls."""

    CONTROL_TAGS = {"button", "select", "textarea"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.images: list[dict] = []
        self.resources: list[dict] = []
        self.forms: list[dict] = []
        self.ids: list[str] = []
        self.interactive: list[dict] = []
        self.title = ""
        self._in_title = False
        self._form = None
        self._anchor: dict | None = None
        self._anchor_text: list[str] = []
        self._control: dict | None = None
        self._control_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(name).lower(): (value or "") for name, value in attrs}
        if attrs.get("id"):
            self.ids.append(attrs["id"][:200])
        if tag == "title":
            self._in_title = True
        if tag == "base" and attrs.get("href"):
            self.resources.append({"url": attrs["href"][:500], "kind": "base"})
        if tag in ("a", "area") and attrs.get("href"):
            self.links.append(attrs["href"][:2000])
            self._anchor = {"kind": "link", **self._describe(tag, attrs)}
            self._anchor_text = []
        if tag == "img":
            image = {"src": attrs.get("src", "")[:2000], "alt": attrs.get("alt", "")[:200], "kind": "img"}
            self.images.append(image)
            if attrs.get("src"):
                self.resources.append({"url": attrs["src"][:2000], "kind": "img"})
        if tag == "script" and attrs.get("src"):
            self.resources.append({"url": attrs["src"][:2000], "kind": "script"})
        if tag == "link" and attrs.get("href"):
            rel = attrs.get("rel", "").lower()
            self.resources.append({"url": attrs["href"][:2000], "kind": rel or "link"})
        if tag == "iframe" and attrs.get("src"):
            self.resources.append({"url": attrs["src"][:2000], "kind": "iframe"})
            self.links.append(attrs["src"][:2000])
        if tag == "form":
            self._form = {"action": attrs.get("action", "")[:2000], "method": (attrs.get("method") or "get").lower(),
                          "fields": []}
            self.forms.append(self._form)
        if tag in ("input", "select", "textarea", "button"):
            described = self._describe(tag, attrs)
            self.interactive.append(described)
            if self._form is not None:
                self._form["fields"].append({
                    "name": described.get("name", "")[:120],
                    "type": described.get("type", tag if tag != "input" else "text")[:40],
                    "required": bool(attrs.get("required")),
                    "label": described.get("name_from") or "",
                })
            if tag in self.CONTROL_TAGS:
                self._control = described
                self._control_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in ("a", "area") and self._anchor is not None:
            text = " ".join(part for part in self._anchor_text if part).strip()
            if text:
                self._anchor["text"] = clip(text, 120)
                if not self._anchor.get("aria_label") and not self._anchor.get("title"):
                    self.interactive.append(self._anchor)
            self._anchor = None
        if tag == "form":
            self._form = None
        if tag in self.CONTROL_TAGS:
            if self._control is not None:
                text = " ".join(part for part in self._control_text if part).strip()
                if text and not self._control.get("aria_label") and not self._control.get("title"):
                    self._control["text"] = clip(text, 120)
            self._control = None

    def handle_data(self, data):
        if self._in_title:
            self.title = clip((self.title + " " + data).strip(), 300)
        if self._anchor is not None:
            self._anchor_text.append(data.strip())
        if self._control is not None:
            self._control_text.append(data.strip())

    @staticmethod
    def _describe(tag: str, attrs: dict) -> dict:
        aria = attrs.get("aria-label") or attrs.get("aria-labelledby") or ""
        describedby = attrs.get("title") or ""
        label = aria or describedby or attrs.get("alt") or attrs.get("placeholder") or ""
        source = ("aria-label" if attrs.get("aria-label") else
                  "title" if attrs.get("title") else
                  "alt" if attrs.get("alt") else
                  "placeholder" if attrs.get("placeholder") else "")
        return {
            "tag": tag,
            "type": attrs.get("type", "")[:40],
            "id": attrs.get("id", "")[:200],
            "name": attrs.get("name", "")[:200],
            "role": attrs.get("role", "")[:80],
            "aria_label": aria[:200],
            "title": describedby[:200],
            "alt": attrs.get("alt", "")[:200],
            "placeholder": attrs.get("placeholder", "")[:200],
            "text": attrs.get("value", "")[:120],
            "name_from": source,
            "disabled": "disabled" in attrs,
            "hidden": attrs.get("type", "").lower() == "hidden" or "hidden" in attrs,
            "href": attrs.get("href", "")[:2000],
        }


class HttpFetcher:
    """Standard-library fetcher. Follows redirects; never executes scripts."""

    mode = "http"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = MAX_BYTES):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._opener = build_opener()

    def fetch(self, url: str) -> Page:
        started = time.monotonic()
        request = Request(url, headers={
            "User-Agent": "TestForge-Explorer/1.0 (+deterministic black-box QA)",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en",
        })
        page = Page(url=url, mode=self.mode)
        try:
            response = self._opener.open(request, timeout=self.timeout)
            status = getattr(response, "status", 200)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            raw = response.read(self.max_bytes + 1)
            final_url = response.geturl()
        except HTTPError as exc:
            status = int(exc.code)
            headers = {str(key).lower(): str(value) for key, value in (exc.headers or {}).items()}
            raw = (exc.read(self.max_bytes + 1) if hasattr(exc, "read") else b"") or b""
            final_url = url
        except (URLError, TimeoutError, OSError) as exc:
            raise FetchError(scrub(f"{type(exc).__name__}: {exc}")) from None
        page.elapsed_ms = int((time.monotonic() - started) * 1000)
        page.url = final_url
        page.status = status
        page.headers = headers
        if len(raw) > self.max_bytes:
            raw = raw[: self.max_bytes]
        page.body = raw.decode("utf-8", errors="replace")[:MAX_BODY]
        content_type = headers.get("content-type", "")
        if "html" in content_type or not content_type:
            parser = _Parser()
            try:
                parser.feed(page.body)
            except Exception:  # malformed markup is data, not a crash
                pass
            page.links = [urljoin(final_url, link) for link in parser.links]
            page.images = [{**image, "src": urljoin(final_url, image["src"])} if image.get("src") else image
                           for image in parser.images]
            page.resources = [{**item, "url": urljoin(final_url, item["url"])} if item.get("url") else item
                              for item in parser.resources]
            page.forms = parser.forms
            page.ids = parser.ids
            page.interactive = parser.interactive
            page.title = parser.title
            # Verify a bounded number of images: a 404 image is a real, cheap
            # finding and the default (browserless) mode should not miss it.
            self._check_images(page, final_url)
        return page

    def _check_images(self, page: Page, base: str) -> None:
        budget = 8
        for image in page.images:
            if budget <= 0:
                break
            source = str(image.get("src") or "")
            if not source.startswith(("http://", "https://")):
                continue
            budget -= 1
            request = Request(source, method="HEAD", headers={
                "User-Agent": "TestForge-Explorer/1.0 (+deterministic black-box QA)"})
            try:
                with self._opener.open(request, timeout=min(self.timeout, 4.0)) as response:
                    image["status"] = int(getattr(response, "status", 200) or 200)
            except HTTPError as exc:
                image["status"] = int(exc.code)
            except (URLError, TimeoutError, OSError):
                image["status"] = 0
            image["ok"] = int(image.get("status") or 0) < 400 and int(image.get("status") or 0) != 0


class PlaywrightFetcher:
    """Rendered fetcher: adds console errors, page errors and failed requests.

    Imported lazily. Headless Chromium only; the browser is closed even when the
    page fails, and the response body stays bounded like the HTTP mode.
    """

    mode = "browser"

    def __init__(self, timeout_ms: int = 15000):
        self.timeout_ms = timeout_ms

    def fetch(self, url: str) -> Page:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - depends on deployment
            raise FetchError("Playwright is not installed; use the HTTP explorer mode") from exc
        from app.browser import launch_kwargs

        page = Page(url=url, mode=self.mode)
        started = time.monotonic()
        console: list[str] = []
        page_errors: list[str] = []
        failed: list[dict] = []
        image_failures: list[dict] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(**launch_kwargs())
                try:
                    context = browser.new_context(ignore_https_errors=False)
                    tab = context.new_page()

                    def on_console(message):
                        if message.type in ("error", "warning"):
                            console.append(clip(f"{message.type}: {message.text}", 400))

                    tab.on("console", on_console)
                    tab.on("pageerror", lambda exc: page_errors.append(clip(str(exc), 400)))
                    tab.on("requestfailed", lambda request: failed.append({
                        "url": clip(request.url, 800),
                        "error": clip(str(request.failure or ""), 300)}))
                    tab.on("response", lambda response: image_failures.append({
                        "url": clip(response.url, 800), "status": response.status})
                        if response.request.resource_type == "image" else None)
                    response = tab.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    tab.wait_for_timeout(500)
                    page.status = response.status if response else 0
                    page.headers = {str(k).lower(): str(v) for k, v in (response.headers if response else {}).items()}
                    page.url = tab.url
                    structure = tab.evaluate(
                        """() => {
                          const attr = (el, name) => (el.getAttribute(name) || '').slice(0, 2000);
                          const describe = el => ({
                            tag: el.tagName.toLowerCase(), type: attr(el,'type'), id: attr(el,'id'),
                            name: attr(el,'name'), role: attr(el,'role'), aria_label: attr(el,'aria-label'),
                            title: attr(el,'title'), alt: attr(el,'alt'), placeholder: attr(el,'placeholder'),
                            text: (el.innerText || el.value || '').trim().slice(0,120),
                            hidden: el.type === 'hidden' || el.hidden || getComputedStyle(el).display === 'none',
                            href: attr(el,'href')
                          });
                          const interactive = Array.from(document.querySelectorAll('a[href],button,input,select,textarea'))
                            .map(describe).slice(0, 500);
                          const forms = Array.from(document.forms).map(form => ({
                            action: form.getAttribute('action') || '', method: (form.getAttribute('method') || 'get').toLowerCase(),
                            fields: Array.from(form.elements).slice(0,100).map(el => ({
                              name: (el.getAttribute('name')||'').slice(0,120),
                              type: (el.getAttribute('type')||el.tagName.toLowerCase()).slice(0,40),
                              required: !!el.required,
                              label: (el.getAttribute('aria-label')||el.getAttribute('placeholder')||'').slice(0,200)
                            }))}));
                          const images = Array.from(document.images).map(img => ({src: img.currentSrc || img.src, alt: attr(img,'alt')})).slice(0,300);
                          return {
                            title: document.title || '', html: document.documentElement.outerHTML.slice(0, 400000),
                            links: Array.from(document.querySelectorAll('a[href]')).map(a => a.href).slice(0,1000),
                            ids: Array.from(document.querySelectorAll('[id]')).map(el => el.id).slice(0,2000),
                            interactive, forms, images,
                            resources: Array.from(document.querySelectorAll('script[src],link[href],iframe[src]'))
                              .map(el => ({url: el.src || el.href || '', kind: el.tagName.toLowerCase()})).slice(0,300)
                          };
                        }""") or {}
                    page.body = str(structure.get("html") or "")[:MAX_BODY]
                    page.title = clip(structure.get("title") or "", 300)
                    page.links = [str(item)[:2000] for item in structure.get("links") or []]
                    page.ids = [str(item)[:200] for item in structure.get("ids") or []]
                    page.interactive = [item for item in structure.get("interactive") or [] if isinstance(item, dict)]
                    page.forms = [item for item in structure.get("forms") or [] if isinstance(item, dict)]
                    page.resources = [item for item in structure.get("resources") or [] if isinstance(item, dict)]
                    bad_images = {item["url"] for item in image_failures if item.get("status", 200) >= 400}
                    broken = {item["url"] for item in failed}
                    page.images = [{"src": str(item.get("src") or ""), "alt": item.get("alt") or "",
                                    "ok": str(item.get("src") or "") not in bad_images | broken}
                                   for item in structure.get("images") or [] if isinstance(item, dict)]
                    context.close()
                finally:
                    browser.close()
        except FetchError:
            raise
        except Exception as exc:  # pragma: no cover - depends on deployment
            raise FetchError(scrub(f"{type(exc).__name__}: {exc}")) from None
        page.elapsed_ms = int((time.monotonic() - started) * 1000)
        page.console = console[:50]
        page.page_errors = page_errors[:50]
        page.failed_requests = failed[:50]
        return page


def default_fetcher():
    """HTTP mode unless the operator opted into a rendered browser scan."""
    if os.environ.get("TF_EXPLORER_BROWSER", "0").strip().lower() in {"1", "true", "yes", "on"}:
        return PlaywrightFetcher()
    return HttpFetcher()


# ---------------------------------------------------------------------------
# Checks: pure functions over a Page


def _signature(*parts) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _replay(url: str, extra: list | None = None) -> list:
    steps = [{"action": "navigate", "value": url, "label": f"Open {url}"}]
    for item in extra or []:
        steps.append(item)
    return steps


def check_http(page: Page) -> list[Finding]:
    if page.status < 400:
        return []
    severity = "critical" if page.status >= 500 else "high"
    return [Finding(
        check="http-status", category="availability", severity=severity,
        title=f"HTTP {page.status} returned by {_path(page.url)}",
        detail=f"The target answered HTTP {page.status}. A page the crawl reached should answer 2xx or a "
               f"deliberate redirect.",
        url=page.url, expected="HTTP 2xx or an intentional redirect",
        actual=f"HTTP {page.status}",
        evidence={"status": page.status, "content_type": page.headers.get("content-type", ""),
                  "body_excerpt": scrub(page.body[:500])},
        replay_steps=_replay(page.url),
        signature=_signature("http-status", _path(page.url), page.status),
    )]


def check_console(page: Page) -> list[Finding]:
    findings = []
    errors = [item for item in page.console if str(item).lower().startswith("error")]
    if errors:
        findings.append(Finding(
            check="console-error", category="frontend", severity="medium",
            title=f"{len(errors)} console error(s) on {_path(page.url)}",
            detail="The page logged errors in the browser console; they usually indicate a failed render, a "
                   "blocked resource or an unhandled exception.",
            url=page.url, expected="No console errors", actual="; ".join(scrub(item) for item in errors[:3]),
            evidence={"console": [scrub(item) for item in errors[:10]]},
            replay_steps=_replay(page.url, [{"action": "assert_no_console_errors", "value": "", "label": "Assert no console errors"}]),
            signature=_signature("console-error", _path(page.url), errors[0][:120]),
        ))
    if page.page_errors:
        findings.append(Finding(
            check="page-error", category="frontend", severity="high",
            title=f"Unhandled page exception on {_path(page.url)}",
            detail="A JavaScript exception escaped during load. Users see a broken control, not a stack trace.",
            url=page.url, expected="No uncaught exceptions", actual=scrub(page.page_errors[0]),
            evidence={"page_errors": [scrub(item) for item in page.page_errors[:10]]},
            replay_steps=_replay(page.url, [{"action": "assert_no_page_errors", "value": "", "label": "Assert no uncaught exceptions"}]),
            signature=_signature("page-error", _path(page.url), page.page_errors[0][:120]),
        ))
    return findings


def check_network(page: Page) -> list[Finding]:
    findings = []
    if page.failed_requests:
        first = page.failed_requests[0]
        findings.append(Finding(
            check="network-failure", category="availability", severity="high",
            title=f"{len(page.failed_requests)} failed request(s) on {_path(page.url)}",
            detail="A resource the page requested did not load. This is often the cause behind a blank panel "
                   "or a missing image that the page reports as success.",
            url=page.url, expected="Every requested resource loads",
            actual=f"{scrub(str(first.get('url')))}: {scrub(str(first.get('error')))}",
            evidence={"failed_requests": page.failed_requests[:10]},
            replay_steps=_replay(page.url, [{"action": "assert_no_failed_requests", "value": "", "label": "Assert every request loads"}]),
            signature=_signature("network-failure", _path(page.url), str(first.get("url"))[:160]),
        ))
    broken = [image for image in page.images if image.get("ok") is False or image.get("status", 200) >= 400]
    if broken:
        findings.append(Finding(
            check="broken-image", category="content", severity="low",
            title=f"{len(broken)} broken image(s) on {_path(page.url)}",
            detail="An image failed to load. Customer-facing pages lose trust quickly over placeholder icons.",
            url=page.url, expected="Every image renders", actual=scrub(str(broken[0].get("src") or "")),
            evidence={"images": broken[:10]},
            replay_steps=_replay(page.url, [{"action": "assert_images_loaded", "value": "", "label": "Assert every image loaded"}]),
            signature=_signature("broken-image", _path(page.url), str(broken[0].get("src"))[:160]),
        ))
    return findings


def check_duplicate_ids(page: Page) -> list[Finding]:
    counts: dict[str, int] = {}
    for value in page.ids:
        counts[value] = counts.get(value, 0) + 1
    duplicates = sorted((value for value, count in counts.items() if count > 1), key=lambda value: (-counts[value], value))
    if not duplicates:
        return []
    return [Finding(
        check="duplicate-id", category="frontend", severity="low",
        title=f"{len(duplicates)} duplicated DOM id(s) on {_path(page.url)}",
        detail="Duplicate ids break label targeting, ARIA references and any selector that assumes uniqueness.",
        url=page.url, expected="Every id appears once", actual=", ".join(duplicates[:5]),
        evidence={"duplicates": duplicates[:20]},
        replay_steps=_replay(page.url, [{"action": "assert_unique_ids", "value": "", "label": "Assert ids are unique"}]),
        signature=_signature("duplicate-id", _path(page.url), duplicates[0]),
    )]


def _accessible_name(item: dict) -> str:
    for key in ("aria_label", "title", "alt", "placeholder", "text", "name"):
        value = str(item.get(key) or "").strip()
        if value and not (key == "text" and item.get("tag") == "input"):
            return value
    return ""


def check_accessible_names(page: Page) -> list[Finding]:
    findings = []
    unnamed = []
    for item in page.interactive:
        tag = str(item.get("tag") or "").lower()
        kind = str(item.get("type") or "").lower()
        if item.get("hidden") or kind in ("hidden", "submit", "button", "reset", "image"):
            continue
        if tag not in ("a", "button", "input", "select", "textarea"):
            continue
        if tag == "a" and not item.get("href"):
            continue
        if not _accessible_name(item):
            unnamed.append(item)
    if unnamed:
        labels = [f"<{item.get('tag')} type={item.get('type') or '-'} id={item.get('id') or '-'}>" for item in unnamed[:5]]
        findings.append(Finding(
            check="accessible-name", category="accessibility", severity="medium",
            title=f"{len(unnamed)} control(s) without an accessible name on {_path(page.url)}",
            detail="Screen readers cannot announce these controls and automation cannot target them by role or "
                   "name, which is what makes a locator stable.",
            url=page.url, expected="Every interactive control has a label, aria-label or visible text",
            actual=", ".join(labels),
            evidence={"controls": [{key: item.get(key) for key in ("tag", "type", "id", "name", "role")} for item in unnamed[:20]]},
            replay_steps=_replay(page.url, [{"action": "assert_accessible_names", "value": "", "label": "Assert every control is named"}]),
            signature=_signature("accessible-name", _path(page.url), labels[0] if labels else ""),
        ))
    if not page.title.strip():
        findings.append(Finding(
            check="missing-title", category="accessibility", severity="low",
            title=f"Page has no title on {_path(page.url)}",
            detail="An empty document title is a WCAG failure and makes browser history unusable.",
            url=page.url, expected="A descriptive <title>", actual="(empty)",
            evidence={"title": page.title},
            replay_steps=_replay(page.url, [{"action": "assert_title", "value": ".*", "label": "Assert the page has a title"}]),
            signature=_signature("missing-title", _path(page.url)),
        ))
    return findings


def check_security(page: Page) -> list[Finding]:
    findings = []
    parts = urlsplit(page.url)
    insecure_password = parts.scheme == "http" and re.search(r"type\s*=\s*[\"']?password", page.body, re.I)
    get_password = any(
        str(form.get("method") or "").lower() == "get" and
        any(str(field.get("type") or "").lower() == "password" for field in form.get("fields") or [])
        for form in page.forms)
    if insecure_password or get_password:
        findings.append(Finding(
            check="password-exposure", category="security", severity="critical",
            title=f"Password field submitted in the clear on {_path(page.url)}",
            detail="A password input is served over plain HTTP or submitted with GET, so the secret can be "
                   "read from the URL, proxy logs and browser history.",
            url=page.url, expected="HTTPS and POST for credentials",
            actual="plain HTTP" if insecure_password else "GET form containing a password field",
            evidence={"scheme": parts.scheme, "forms": page.forms[:5]},
            replay_steps=_replay(page.url, [{"action": "assert_https", "value": "", "label": "Assert the page is HTTPS"}]),
            signature=_signature("password-exposure", _path(page.url), parts.scheme),
        ))
    if parts.scheme == "https":
        mixed = []
        for item in page.resources:
            url = str(item.get("url") or "")
            if url.startswith("http://") and not url.startswith("https://"):
                mixed.append(url)
        if mixed:
            findings.append(Finding(
                check="mixed-content", category="security", severity="medium",
                title=f"{len(mixed)} insecure resource(s) on an HTTPS page",
                detail="Mixed content is blocked by modern browsers, so the script or style may silently fail.",
                url=page.url, expected="All resources use HTTPS", actual=scrub(mixed[0]),
                evidence={"resources": mixed[:20]},
                replay_steps=_replay(page.url, [{"action": "assert_no_mixed_content", "value": "", "label": "Assert no mixed content"}]),
                signature=_signature("mixed-content", _path(page.url), mixed[0][:160]),
            ))
    leaks = ERROR_LEAK.search(page.body or "")
    if leaks:
        start = max(0, leaks.start() - 120)
        excerpt = re.sub(r"\s+", " ", page.body[start:start + 400]).strip()
        findings.append(Finding(
            check="error-leak", category="security", severity="high",
            title=f"Internal error detail exposed on {_path(page.url)}",
            detail="A stack trace or database error reached the browser. Apart from looking broken, it tells an "
                   "attacker the framework, the file layout and sometimes the SQL.",
            url=page.url, expected="A friendly error page", actual=scrub(excerpt),
            evidence={"match": leaks.group(0), "excerpt": scrub(excerpt)},
            replay_steps=_replay(page.url, [{"action": "assert_no_error_render", "value": "", "label": "Assert no internal error text"}]),
            signature=_signature("error-leak", _path(page.url), leaks.group(0).lower()),
        ))
    return findings


def check_headers(page: Page) -> list[Finding]:
    if page.status >= 400:
        return []
    missing = [name for name in ("content-security-policy", "x-content-type-options") if name not in page.headers]
    # Headers are a deployment property of the origin, not of each page: only
    # report them once per scan.
    if len(missing) == 2:
        return [Finding(
            check="security-headers", category="security", severity="low",
            title="No Content-Security-Policy or X-Content-Type-Options header",
            detail="The origin sends neither header. CSP is the main defence against script injection and "
                   "X-Content-Type-Options stops MIME sniffing.",
            url=_origin(page.url) + "/", expected="CSP and nosniff headers present",
            actual=", ".join(missing) + " missing",
            evidence={"headers": sorted(page.headers)[:40]},
            replay_steps=_replay(page.url),
            signature=_signature("security-headers", _origin(page.url)),
        )]
    return []


def check_performance(page: Page) -> list[Finding]:
    if page.elapsed_ms <= SLOW_PAGE_MS or page.status >= 400:
        return []
    return [Finding(
        check="slow-page", category="performance", severity="low",
        title=f"Slow response on {_path(page.url)} ({page.elapsed_ms} ms)",
        detail=f"The page took {page.elapsed_ms} ms to answer. Threshold for this scan is {SLOW_PAGE_MS} ms.",
        url=page.url, expected=f"Under {SLOW_PAGE_MS} ms", actual=f"{page.elapsed_ms} ms",
        evidence={"elapsed_ms": page.elapsed_ms},
        replay_steps=_replay(page.url),
        signature=_signature("slow-page", _path(page.url)),
    )]


def check_placeholder_links(page: Page) -> list[Finding]:
    placeholders = [link for link in page.links if PLACEHOLDER_HREF.match(link.strip())]
    if not placeholders:
        return []
    return [Finding(
        check="placeholder-link", category="content", severity="info",
        title=f"{len(placeholders)} placeholder link(s) on {_path(page.url)}",
        detail="Links pointing at '#' or javascript:void(0) look interactive and do nothing. They are also a "
               "frequent source of false test passes.",
        url=page.url, expected="Every link navigates somewhere", actual=", ".join(placeholders[:5]),
        evidence={"links": placeholders[:20]},
        replay_steps=_replay(page.url),
        signature=_signature("placeholder-link", _path(page.url)),
    )]


CHECKS = (
    ("http-status", check_http),
    ("console", check_console),
    ("network", check_network),
    ("duplicate-id", check_duplicate_ids),
    ("accessible-name", check_accessible_names),
    ("security", check_security),
    ("headers", check_headers),
    ("performance", check_performance),
    ("placeholder-link", check_placeholder_links),
)


def run_checks(page: Page) -> list[Finding]:
    findings: list[Finding] = []
    for _, check in CHECKS:
        try:
            findings.extend(check(page))
        except Exception:  # one broken check must not hide the others
            continue
    return findings


# ---------------------------------------------------------------------------
# Scan, verification and the report


class Explorer:
    """Bounded, deterministic crawl with independent verification."""

    def __init__(self, target: str, fetcher=None, max_pages: int = 6, max_depth: int = 1,
                 budget_seconds: float = 30.0, allow_private: bool | None = None, max_verifications: int = 10,
                 build_label: str = ""):
        self.target = validate_target(target, allow_private=allow_private)
        self.fetcher = fetcher or default_fetcher()
        self.max_pages = max(1, min(int(max_pages), MAX_PAGES))
        self.max_depth = max(0, min(int(max_depth), MAX_DEPTH))
        self.budget_seconds = max(1.0, min(float(budget_seconds), MAX_SECONDS))
        self.max_verifications = max(0, min(int(max_verifications), MAX_VERIFICATIONS))
        self.build_label = clip(str(build_label or ""), 120)
        self.pages: list[Page] = []
        self.failures: list[dict] = []

    # -- crawl ------------------------------------------------------------
    def crawl(self) -> list[Page]:
        origin = _origin(self.target)
        queue: list[tuple[str, int]] = [(self.target, 0)]
        seen: set[str] = set()
        started = time.monotonic()
        while queue and len(self.pages) < self.max_pages:
            if time.monotonic() - started > self.budget_seconds:
                self.failures.append({"url": queue[0][0], "error": "scan time budget reached"})
                break
            url, depth = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                page = self.fetcher.fetch(url)
            except FetchError as exc:
                self.failures.append({"url": url, "error": clip(str(exc), 300)})
                continue
            self.pages.append(page)
            if depth >= self.max_depth:
                continue
            for link in page.links:
                if _crawlable(link, origin, seen):
                    queue.append((link, depth + 1))
        return self.pages

    # -- verification -----------------------------------------------------
    def verify_findings(self, findings: list[Finding]) -> list[Finding]:
        """Re-fetch the page and reproduce the check. Only reproductions verify."""
        verified: list[Finding] = []
        attempts_used = 0
        for finding in findings:
            if attempts_used >= self.max_verifications:
                # Out of verification budget: say so, do not pretend the
                # finding failed to reproduce.
                finding.attempts = 0
                finding.verification = "not checked (verification budget reached)"
                verified.append(finding)
                continue
            attempts_used += 1
            finding.attempts = 1
            try:
                page = self.fetcher.fetch(finding.url)
            except FetchError as exc:
                finding.verification = f"not checked (refetch failed: {clip(str(exc), 120)})"
                verified.append(finding)
                continue
            again = [item for item in run_checks(page) if item.check == finding.check]
            match = any(item.signature == finding.signature for item in again)
            finding.attempts = 2 if match else 1
            finding.verified = match
            finding.verification = "reproduced on a fresh fetch" if match else "did not reproduce on a fresh fetch"
            verified.append(finding)
        return verified

    # -- report -----------------------------------------------------------
    def scan(self) -> dict:
        started = time.monotonic()
        pages = self.crawl()
        raw: list[Finding] = []
        for page in pages:
            raw.extend(run_checks(page))
        # Deduplicate by signature, keeping the first occurrence and counting the rest.
        grouped: dict[str, Finding] = {}
        counts: dict[str, int] = {}
        for finding in raw:
            if finding.signature in grouped:
                counts[finding.signature] += 1
                continue
            grouped[finding.signature] = finding
            counts[finding.signature] = 1
        findings = self.verify_findings(list(grouped.values()))
        for finding in findings:
            finding.evidence = {**finding.evidence, "occurrences": counts.get(finding.signature, 1)}
        findings.sort(key=lambda item: (SEVERITY_ORDER.get(item.severity, 9), item.check, item.url, item.signature))
        findings = findings[:MAX_FINDINGS]
        elapsed = round(time.monotonic() - started, 3)
        report = {
            "schema": "testforge-explorer-report/1",
            "target": self.target,
            "mode": getattr(self.fetcher, "mode", "injected"),
            "build_label": self.build_label,
            "generated_at": _now_iso(),
            "elapsed_seconds": elapsed,
            "limits": {"pages": self.max_pages, "depth": self.max_depth,
                       "budget_seconds": self.budget_seconds, "verifications": self.max_verifications},
            "pages": [{"url": page.url, "status": page.status, "elapsed_ms": page.elapsed_ms,
                       "title": clip(page.title, 200)} for page in pages],
            "fetch_failures": self.failures[:50],
            "findings": [finding.as_dict() for finding in findings],
            "counts": _counts(findings),
            "notes": [
                "Deterministic, rule-based discovery: no language model is used, so the same target and the "
                "same rules produce the same report.",
                "Only findings marked verified reproduced on a fresh fetch. Unverified findings are kept, "
                "labelled and must not be filed as defects without a human check.",
                "Destructive paths (logout, delete, pay, unsubscribe) are never followed.",
                f"Scan mode: {getattr(self.fetcher, 'mode', 'injected')}. "
                + ("Rendered DOM, console and network signals included." if getattr(self.fetcher, "mode", "") == "browser"
                   else "Static HTML analysis; set TF_EXPLORER_BROWSER=1 for rendered DOM and console signals."),
            ],
        }
        report["receipt"] = _receipt(report)
        return report


def _counts(findings: list[Finding]) -> dict:
    counts = {"total": len(findings), "verified": 0, "unverified": 0, "not_checked": 0}
    for item in ("critical", "high", "medium", "low", "info"):
        counts[item] = 0
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        if finding.verified:
            counts["verified"] += 1
        elif finding.attempts:
            counts["unverified"] += 1
        else:
            counts["not_checked"] += 1
    return counts


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _receipt(report: dict) -> dict:
    body = {key: report[key] for key in ("target", "mode", "build_label", "generated_at", "pages",
                                         "findings", "counts", "limits") if key in report}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()
    return {
        "schema": "testforge-explorer-receipt/1",
        "generated_at": report["generated_at"],
        "target": report["target"],
        "mode": report["mode"],
        "build_label": report.get("build_label") or "",
        "findings": report["counts"]["total"],
        "verified": report["counts"]["verified"],
        "sha256": digest,
    }


def defect_report(report: dict) -> str:
    """Markdown a developer or a Jira ticket can use directly."""
    counts = report.get("counts") or {}
    receipt = report.get("receipt") or {}
    lines = [
        f"# Explorer findings — {report.get('target')}",
        "",
        f"- Mode: **{report.get('mode')}**" + (f" · build `{report.get('build_label')}`" if report.get("build_label") else ""),
        f"- Generated: {report.get('generated_at')} · pages scanned: {len(report.get('pages') or [])} "
        f"· elapsed: {report.get('elapsed_seconds')} s",
        f"- Findings: **{counts.get('total', 0)}** "
        f"(verified {counts.get('verified', 0)}, unverified {counts.get('unverified', 0)}, "
        f"not checked {counts.get('not_checked', 0)}) — "
        f"critical {counts.get('critical', 0)}, high {counts.get('high', 0)}, "
        f"medium {counts.get('medium', 0)}, low {counts.get('low', 0)}, info {counts.get('info', 0)}",
        f"- Receipt: `{receipt.get('sha256', 'n/a')}` (regenerate with the same target and build to compare)",
        "",
        "> Deterministic rules, no language model. Only **verified** findings reproduced on a fresh fetch. "
        "Unverified findings are hypotheses and must be checked by a human before filing.",
        "",
    ]
    for index, finding in enumerate(report.get("findings") or [], start=1):
        lines.extend([
            f"## {index}. {finding.get('title')}",
            "",
            f"- **Severity:** {finding.get('severity')} · **Category:** {finding.get('category')} "
            f"· **Check:** `{finding.get('check')}`",
            f"- **URL:** {finding.get('url')}",
            f"- **Status:** {'verified' if finding.get('verified') else 'NOT verified'}"
            f"{(' — ' + str(finding.get('verification'))) if finding.get('verification') else ''} "
            f"({finding.get('attempts')} attempt(s), {finding.get('evidence', {}).get('occurrences', 1)} occurrence(s))",
            f"- **Expected:** {finding.get('expected')}",
            f"- **Actual:** {finding.get('actual')}",
            "",
            finding.get("detail") or "",
            "",
            "**Replay steps**",
            "",
        ])
        for step in finding.get("replay_steps") or []:
            lines.append(f"1. `{step.get('action')}` {step.get('label') or ''} "
                         f"{('— `' + str(step.get('value')) + '`') if step.get('value') else ''}")
        evidence = json.dumps(finding.get("evidence") or {}, ensure_ascii=False, indent=2, default=str)
        lines.extend(["", "<details><summary>Evidence</summary>", "", "```json", evidence[:4000], "```", "", "</details>", ""])
    if report.get("fetch_failures"):
        lines.extend(["## Pages that could not be fetched", ""])
        for failure in report["fetch_failures"]:
            lines.append(f"- `{failure.get('url')}` — {failure.get('error')}")
        lines.append("")
    lines.append("---")
    lines.append("Generated by TestForge `app/explorer.py`. Sequential and bounded; no credentials are included "
                 "and captured text is redacted.")
    return "\n".join(lines)
