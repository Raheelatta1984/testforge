"""HTTP surface for the WCAG A/AA accessibility subset.

    POST /api/accessibility/scan              scan a black-box URL, persist the report
    GET  /api/accessibility/scans/{scan_id}   read back a persisted report
    GET  /api/accessibility/rules             the ten rules with criterion, level, impact

A scan crawls like the explorer (same bounds, same fetchers, same SSRF rules)
and runs the ten-rule subset over every page. The full per-rule detail is
persisted under ``ARTIFACTS/a11y`` (newest 50 kept); the response carries the
per-page counts plus the violations, so it stays small.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import accessibility as a11y
from app import explorer

router = APIRouter()

_SCAN_LOCK = threading.Lock()
MAX_SCANS_KEPT = 50


def _artifacts() -> Path:
    root = os.environ.get("TF_ARTIFACTS") or os.path.join(os.path.expanduser("~"), "testforge_titan_data")
    folder = Path(root) / "a11y"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Send the scan as a JSON object.") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Send the scan as a JSON object.")
    return payload


def _limit(value, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value if value is not None else default), maximum))
    except (TypeError, ValueError):
        raise HTTPException(400, "bounds must be numbers") from None


def _persist(report: dict) -> str:
    folder = _artifacts()
    scan_id = hashlib.sha256(
        f"{report['target']}|{report['generated_at']}".encode()).hexdigest()[:16]
    report["scan_id"] = scan_id
    (folder / f"{scan_id}.json").write_text(json.dumps(report, ensure_ascii=True, indent=2),
                                            encoding="utf-8")
    try:
        files = sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime)
        for stale in files[: -MAX_SCANS_KEPT] if len(files) > MAX_SCANS_KEPT else []:
            stale.unlink(missing_ok=True)
    except OSError:
        pass
    return scan_id


@router.get("/api/accessibility/rules")
def accessibility_rules():
    return {"schema": "testforge-a11y-rules/1",
            "rules": [{"id": rule_id, "title": title, "wcag": wcag, "level": level,
                       "impact": impact, "description": description}
                      for rule_id, (title, wcag, level, impact, description) in a11y.RULES.items()]}


@router.get("/api/accessibility/scans/{scan_id}")
def accessibility_scan(scan_id: str):
    name = "".join(char for char in str(scan_id or "") if char.isalnum())[:32]
    if not name:
        raise HTTPException(404, "Unknown accessibility scan.")
    path = _artifacts() / f"{name}.json"
    if not path.is_file():
        raise HTTPException(404, "Unknown accessibility scan.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "Unknown accessibility scan.") from None


@router.post("/api/accessibility/scan")
async def accessibility_scan(request: Request):
    body = await _body(request)
    url = body.get("url") or body.get("target")
    if not isinstance(url, str):
        raise HTTPException(400, "Provide the URL to scan under \"url\".")
    mode = str(body.get("mode") or "").strip().lower()
    if mode and mode not in ("http", "browser"):
        raise HTTPException(400, "mode must be 'http' or 'browser'")
    fetcher = explorer.default_fetcher()
    if mode == "browser":
        fetcher = explorer.PlaywrightFetcher()
    elif mode == "http":
        fetcher = explorer.HttpFetcher()
    if not _SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(429, "Another accessibility scan is running. Scans are sequential; retry when it finishes.")
    max_depth = body.get("max_depth", 0)
    try:
        max_depth = max(0, min(int(max_depth), explorer.MAX_DEPTH))
    except (TypeError, ValueError):
        raise HTTPException(400, "max_depth must be a number") from None
    try:
        worker = explorer.Explorer(
            url,
            fetcher=fetcher,
            max_pages=_limit(body.get("max_pages"), 3, explorer.MAX_PAGES),
            max_depth=max_depth,
            budget_seconds=body.get("budget_seconds") or 30,
            allow_private=body.get("allow_private") if isinstance(body.get("allow_private"), bool) else None,
            max_verifications=0,
            build_label=str(body.get("build_label") or ""),
        )
        started = time.monotonic()
        pages = worker.crawl()
        full_pages = []
        violations: list[dict] = []
        for page in pages:
            try:
                result = a11y.scan_page(page)
            except Exception:
                continue
            full_pages.append(result)
            for violation in result["violations"]:
                if len(violations) >= 50:
                    break
                violations.append({"url": page.url, **violation})
        by_impact: dict[str, int] = {}
        for violation in violations:
            impact = str(violation.get("impact") or "minor")
            by_impact[impact] = by_impact.get(impact, 0) + 1
        report = {
            "schema": "testforge-a11y-report/1",
            "target": worker.target,
            "mode": getattr(fetcher, "mode", "injected"),
            "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "pages": [{"url": item["url"], "counts": item["counts"], "rules": item["rules"]}
                      for item in full_pages],
            "violations": violations,
            "counts": {"pages": len(full_pages), "violations": len(violations),
                       "by_impact": by_impact},
            "notes": [
                "WCAG 2.x A/AA subset (ten rules). Rule ids and impact persist here and in the explorer report.",
                "Contrast without a rendered scan covers inline styles only; TF_EXPLORER_BROWSER=1 "
                "adds computed-style samples.",
            ],
        }
        scan_id = _persist(report)
        return {"scan_id": scan_id, "target": report["target"], "mode": report["mode"],
                "generated_at": report["generated_at"], "elapsed_seconds": report["elapsed_seconds"],
                "pages": [{"url": item["url"], "counts": item["counts"]} for item in full_pages],
                "violations": violations, "counts": report["counts"],
                "artifact": f"/api/accessibility/scans/{scan_id}"}
    except explorer.ExplorerError as exc:
        raise HTTPException(400, str(exc)) from None
    except explorer.FetchError as exc:
        raise HTTPException(502, f"Could not reach the target: {exc}") from None
    finally:
        _SCAN_LOCK.release()
