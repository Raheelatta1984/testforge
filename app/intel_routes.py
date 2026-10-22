"""Routes for the engineering-intelligence features added alongside the market
comparison: reuse-first step resolution, impact analysis, the zero-LLM explorer
and machine-readable CI results.

Nothing here is a second engine. Each endpoint calls the module that owns the
behaviour, so the CLI, the MCP server and the dashboard cannot drift apart.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app import ci_results, explorer, impact_map, step_catalog

router = APIRouter()
MAX_BODY_BYTES = 2_500_000
MAX_DIFF_CHARS = 200_000

# One browser-shaped scan at a time per process. The hosted instance has a hard
# memory ceiling and a crawl that overlaps a recording is the fastest route to an
# OOM; a second scan gets a clear 429 instead of a dead container.
_SCAN_LOCK = threading.Lock()


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, f"Request is larger than {MAX_BODY_BYTES // 1_000_000} MB")
    if not raw.strip():
        return {}
    import json
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Request body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _limit(value, default: int, maximum: int) -> int:
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise HTTPException(400, "limit must be a number") from None
    if number < 1:
        raise HTTPException(400, "limit must be at least 1")
    return min(number, maximum)


# ---------------------------------------------------------------------------
# Step catalogue


@router.get("/api/steps/catalog")
def steps_catalog(project_id: str | None = None, action: str | None = None, limit: int = 500):
    return step_catalog.catalog(project_id=project_id, action=action, limit=_limit(limit, 500, 4000))


@router.get("/api/steps/search")
def steps_search(q: str = Query(default=""), project_id: str | None = None,
                 action: str | None = None, limit: int = 25):
    return step_catalog.search(q, project_id=project_id, action=action, limit=_limit(limit, 25, 100))


@router.post("/api/steps/resolve")
async def steps_resolve(request: Request):
    body = await _body(request)
    phrase = body.get("phrase") or body.get("step") or ""
    if not isinstance(phrase, str):
        raise HTTPException(400, "phrase must be text")
    try:
        return step_catalog.resolve_step(phrase, project_id=body.get("project_id"),
                                         limit=_limit(body.get("limit"), 5, 20))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/api/steps/summary")
def steps_summary():
    return step_catalog.compact_summary()


# ---------------------------------------------------------------------------
# Impact analysis


@router.get("/api/impact/map")
def impact_map_get():
    payload, _ = impact_map.build_map()
    return payload


@router.get("/api/impact/summary")
def impact_summary():
    return impact_map.compact_summary()


@router.post("/api/impact/analyse")
async def impact_analyse(request: Request):
    body = await _body(request)
    files = body.get("changed_files") or []
    routes = body.get("routes") or []
    labels = body.get("labels") or []
    if not isinstance(files, list) or not isinstance(routes, list) or not isinstance(labels, list):
        raise HTTPException(400, "changed_files, routes and labels must be lists")
    diff = body.get("diff") or ""
    if not isinstance(diff, str):
        raise HTTPException(400, "diff must be text")
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(413, f"diff is larger than {MAX_DIFF_CHARS:,} characters; send changed_files instead")
    project_id = body.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        raise HTTPException(400, "project_id must be text")
    try:
        return impact_map.analyse(changed_files=files, diff=diff, routes=routes, labels=labels,
                                  project_id=project_id,
                                  max_tests=_limit(body.get("max_tests"), 25, 200),
                                  include_priorities=bool(body.get("include_priorities", True)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# ---------------------------------------------------------------------------
# Explorer (zero-LLM discovery)


@router.get("/api/explorer/capabilities")
def explorer_capabilities():
    return {
        "schema": "testforge-explorer-capabilities/1",
        "checks": [
            {"id": "http-status", "category": "availability", "severity": "critical/high",
             "what": "A crawled page answers 4xx or 5xx."},
            {"id": "console-error", "category": "frontend", "severity": "medium",
             "what": "The page logs errors in the browser console (browser mode)."},
            {"id": "page-error", "category": "frontend", "severity": "high",
             "what": "An uncaught JavaScript exception escapes during load (browser mode)."},
            {"id": "network-failure", "category": "availability", "severity": "high",
             "what": "A requested resource fails to load (browser mode)."},
            {"id": "broken-image", "category": "content", "severity": "low",
             "what": "An image answers an error status or fails to load."},
            {"id": "duplicate-id", "category": "frontend", "severity": "low",
             "what": "The same DOM id appears more than once."},
            {"id": "accessible-name", "category": "accessibility", "severity": "medium",
             "what": "An interactive control has no label, aria-label, title or visible text."},
            {"id": "missing-title", "category": "accessibility", "severity": "low",
             "what": "The document has no title."},
            {"id": "password-exposure", "category": "security", "severity": "critical",
             "what": "A password field is served over HTTP or submitted with GET."},
            {"id": "mixed-content", "category": "security", "severity": "medium",
             "what": "An HTTPS page loads an http:// resource."},
            {"id": "error-leak", "category": "security", "severity": "high",
             "what": "A stack trace, SQL error or framework error page reached the browser."},
            {"id": "security-headers", "category": "security", "severity": "low",
             "what": "The origin sends neither CSP nor X-Content-Type-Options."},
            {"id": "slow-page", "category": "performance", "severity": "low",
             "what": "The page took longer than the slow threshold to answer."},
            {"id": "placeholder-link", "category": "content", "severity": "info",
             "what": "A link points at '#' or javascript:void(0)."},
        ],
        "deterministic": True,
        "model_required": False,
        "verification": "A finding is verified only when the same check reproduces on a fresh fetch.",
        "safety": [
            "One origin only; assets, mailto:, tel: and javascript: links are not fetched.",
            "Destructive paths (logout, delete, pay, unsubscribe, reset) are never followed.",
            "Loopback and private addresses are refused unless TF_EXPLORER_ALLOW_PRIVATE=1.",
            "Bounded pages, depth, time budget and bytes; one scan at a time per process.",
        ],
        "modes": {"http": "standard library, static HTML analysis (default)",
                  "browser": "rendered DOM, console and network signals (TF_EXPLORER_BROWSER=1)"},
    }


@router.post("/api/explorer/scan")
async def explorer_scan(request: Request):
    body = await _body(request)
    url = body.get("url") or body.get("target")
    if not isinstance(url, str):
        raise HTTPException(400, "Provide the URL to explore under \"url\".")
    mode = str(body.get("mode") or "").strip().lower()
    if mode and mode not in ("http", "browser"):
        raise HTTPException(400, "mode must be 'http' or 'browser'")
    max_pages = _limit(body.get("max_pages"), 6, explorer.MAX_PAGES)
    max_depth = body.get("max_depth", 1)
    try:
        max_depth = max(0, min(int(max_depth), explorer.MAX_DEPTH))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_depth must be a number") from None
    fetcher = explorer.default_fetcher()
    if mode == "browser":
        fetcher = explorer.PlaywrightFetcher()
    elif mode == "http":
        fetcher = explorer.HttpFetcher()
    if not _SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Another exploration is running. Scans are sequential; retry when it finishes.")
    try:
        scan = explorer.Explorer(
            url,
            fetcher=fetcher,
            max_pages=max_pages,
            max_depth=max_depth,
            budget_seconds=body.get("budget_seconds") or 30,
            allow_private=body.get("allow_private") if isinstance(body.get("allow_private"), bool) else None,
            max_verifications=_limit(body.get("max_verifications"), 10, explorer.MAX_VERIFICATIONS),
            build_label=str(body.get("build_label") or ""),
        ).scan()
        if body.get("include_report"):
            scan["markdown"] = explorer.defect_report(scan)
        return scan
    except explorer.ExplorerError as exc:
        raise HTTPException(400, str(exc)) from None
    except explorer.FetchError as exc:
        raise HTTPException(502, f"Could not reach the target: {exc}") from None
    finally:
        _SCAN_LOCK.release()


# ---------------------------------------------------------------------------
# CI results


@router.get("/api/runs/{run_id}/junit.xml", response_class=PlainTextResponse)
def run_junit(run_id: str, download: int = 0):
    try:
        runs = ci_results.load_runs(run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    document = ci_results.junit_xml(runs, suite_name=f"TestForge · run {run_id}")
    headers = {"X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="testforge-{run_id}.xml"'
    return PlainTextResponse(document, media_type="application/xml", headers=headers)


@router.get("/api/runs/batch/{batch_id}/junit.xml", response_class=PlainTextResponse)
def batch_junit(batch_id: str, download: int = 0):
    try:
        runs = ci_results.load_runs(batch_id=batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    document = ci_results.junit_xml(runs, suite_name=f"TestForge · batch {batch_id}", batch_id=batch_id)
    headers = {"X-Content-Type-Options": "nosniff"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="testforge-batch-{batch_id}.xml"'
    return PlainTextResponse(document, media_type="application/xml", headers=headers)


@router.get("/api/runs/{run_id}/results.json")
def run_results(run_id: str):
    try:
        runs = ci_results.load_runs(run_id=run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return ci_results.summary(runs, suite_name=f"run {run_id}")


@router.get("/api/runs/batch/{batch_id}/results.json")
def batch_results(batch_id: str):
    try:
        runs = ci_results.load_runs(batch_id=batch_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    return ci_results.summary(runs, suite_name=f"batch {batch_id}")
