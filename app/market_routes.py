"""HTTP surface for the market position: data, audit, summary and reports.

    GET  /api/market/matrix            every capability × tool, scores, gaps, audit
    GET  /api/market/competitors       tool metadata and the scoreboard
    GET  /api/market/capabilities      capabilities with TestForge evidence
    GET  /api/market/gaps              where a compared tool leads
    GET  /api/market/differentiators   where no compared tool claims a capability
    GET  /api/market/summary           token-efficient answer for an AI client
    GET  /api/market/audit             re-verify every TestForge claim
    POST /api/market/audit             the same, and persist a 24-hour receipt
    GET  /api/market/report.html       self-contained, printable HTML document
    GET  /api/market/report.md         Markdown for a README or PR

Everything is read-only apart from the audit receipt, which is written under
``ARTIFACTS/market`` — never into the repository, so a stale receipt cannot be
committed as if it were current evidence.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from app import market_intel, market_report

router = APIRouter()


@router.get("/api/market/matrix")
def market_matrix():
    return market_intel.matrix()


@router.get("/api/market/competitors")
def market_competitors():
    full = market_intel.matrix()
    return {"generated_at": full["generated_at"], "self": full["self"], "tools": full["tools"],
            "scoreboard": full["scoreboard"], "summary": full["summary"]}


@router.get("/api/market/capabilities")
def market_capabilities(group: str | None = None):
    full = market_intel.matrix()
    rows = full["capabilities"]
    if group:
        rows = [row for row in rows if row["group"] == group]
    return {"generated_at": full["generated_at"], "groups": full["groups"], "capabilities": rows,
            "audit": full["audit"], "summary": full["summary"]}


@router.get("/api/market/gaps")
def market_gaps():
    full = market_intel.matrix()
    return {"generated_at": full["generated_at"], "gaps": full["gaps"], "differentiators": full["differentiators"],
            "leads": full["leads"], "audit": full["audit"]}


@router.get("/api/market/differentiators")
def market_differentiators():
    full = market_intel.matrix()
    return {"generated_at": full["generated_at"], "differentiators": full["differentiators"],
            "leads": full["leads"], "audit": full["audit"]}


@router.get("/api/market/summary")
def market_summary():
    return market_intel.compact_summary()


@router.get("/api/market/audit")
def market_audit():
    receipt = market_intel.audit_claims(persist=False)
    return receipt


@router.post("/api/market/audit")
def market_audit_persist():
    receipt = market_intel.audit_claims(persist=True)
    if not receipt["summary"]["passed"]:
        # Still a 200: the caller asked for the audit result, and the payload is
        # more useful than an error body. The CLI maps `passed` to an exit code.
        receipt["warning"] = "At least one TestForge claim is no longer supported by evidence."
    return receipt


@router.get("/api/market/report.html")
def market_report_html(download: int = 0):
    document = market_report.html_report()
    headers = {
        "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                                   "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if download:
        headers["Content-Disposition"] = 'attachment; filename="testforge-market-position.html"'
    return HTMLResponse(document, headers=headers)


@router.get("/api/market/report.md", response_class=PlainTextResponse)
def market_report_markdown(download: int = 0):
    document = market_report.markdown_report()
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = 'attachment; filename="testforge-market-position.md"'
    return PlainTextResponse(document, headers=headers)


@router.get("/api/market/evidence")
def market_evidence(capability: str = Query(default="")):
    """The probes behind one claim, or behind all of them."""
    receipt = market_intel.audit_claims(persist=False)
    rows = receipt.get("results") or []
    if capability:
        rows = [row for row in rows if row.get("id") == capability]
        if not rows:
            raise HTTPException(404, "Unknown capability. Read GET /api/market/capabilities for the ids.")
    return {"generated_at": receipt.get("generated_at"), "expires_at": receipt.get("expires_at"),
            "receipt_sha256": receipt.get("receipt_sha256"), "results": rows,
            "summary": receipt.get("summary")}
