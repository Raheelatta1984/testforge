"""Market position: a self-auditing comparison of TestForge against the tools
that are actually proposed to the same buyers.

Two rules make this usable in front of an audience:

1. **No unsourced cells.** Every competitor row records the basis it was read
   from (vendor docs, the supplied GitHub MCP listing screenshots, or an
   explicit ``unknown``). A capability is only counted against a tool when the
   review could evidence it.
2. **No unsupported claims for TestForge.** Every TestForge "yes" carries
   machine-checkable evidence — a file, a symbol, a route decorator or a test.
   :func:`audit_claims` re-verifies those probes and CI fails when a claim rots.
   A rating that no longer has evidence is reported as unverified, never kept
   quietly.

The module is dependency-free and read-only: it never starts a browser, never
touches the network and never writes outside ``ARTIFACTS/market`` when an audit
receipt is explicitly requested.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path

from app.config import ARTIFACTS

DATA_FILE = Path(__file__).with_name("market_data.json")
SCHEMA = "testforge-market-matrix/1"
RECEIPT_TTL_HOURS = 24
RATINGS = ("yes", "partial", "no", "unknown")
PROBE_KINDS = ("file", "symbol", "route", "test")

_CACHE: dict = {}


# ---------------------------------------------------------------------------
# Loading and structural validation

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load(path=None) -> dict:
    """Load the matrix once per process (and per file mtime)."""
    source = Path(path or DATA_FILE)
    try:
        mtime = source.stat().st_mtime
    except OSError:
        raise ValueError("market matrix data file is missing") from None
    cached = _CACHE.get(str(source))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"market matrix data is not readable JSON: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise ValueError("market matrix data must be a JSON object")
    _CACHE[str(source)] = (mtime, payload)
    return payload


def tools(data: dict | None = None) -> list[dict]:
    return list((data or load()).get("tools") or [])


def groups(data: dict | None = None) -> list[dict]:
    return list((data or load()).get("groups") or [])


def capabilities(data: dict | None = None) -> list[dict]:
    return list((data or load()).get("capabilities") or [])


def competitor_ids(data: dict | None = None) -> list[str]:
    data = data or load()
    return [tool["id"] for tool in data.get("tools") or [] if not tool.get("self")]


def self_tool(data: dict | None = None) -> dict:
    for tool in (data or load()).get("tools") or []:
        if tool.get("self"):
            return tool
    raise ValueError("market matrix has no self tool")


def validate(data: dict | None = None) -> list[str]:
    """Return structural problems; an empty list means the data is usable.

    This is deliberately strict. A missing competitor rating or an invented
    rating token would silently change every score on the page, so it is a
    validation failure rather than a default.
    """
    data = data or load()
    problems: list[str] = []
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    tool_rows = data.get("tools") or []
    ids = [str(tool.get("id") or "") for tool in tool_rows]
    if len(ids) != len(set(ids)):
        problems.append("tool ids must be unique")
    if sum(1 for tool in tool_rows if tool.get("self")) != 1:
        problems.append("exactly one tool must be marked self")
    for tool in tool_rows:
        for key in ("id", "name", "vendor", "kind", "licence", "price", "deployment", "basis", "source"):
            if not str(tool.get(key) or "").strip():
                problems.append(f"tool {tool.get('id') or '?'} is missing {key}")
    known = set(ids) - {tool["id"] for tool in tool_rows if tool.get("self")}
    group_ids = {str(group.get("id") or "") for group in data.get("groups") or []}
    if len(group_ids) != len(data.get("groups") or []):
        problems.append("group ids must be unique")
    capability_ids = [str(cap.get("id") or "") for cap in data.get("capabilities") or []]
    if len(capability_ids) != len(set(capability_ids)):
        problems.append("capability ids must be unique")
    for cap in data.get("capabilities") or []:
        cid = cap.get("id") or "?"
        if cap.get("group") not in group_ids:
            problems.append(f"capability {cid} has an unknown group")
        for key in ("name", "why", "roadmap"):
            if not str(cap.get(key) or "").strip():
                problems.append(f"capability {cid} is missing {key}")
        own = cap.get("self") or {}
        if own.get("rating") not in RATINGS:
            problems.append(f"capability {cid} has an invalid self rating")
        evidence = own.get("evidence") or []
        if own.get("rating") == "yes" and not evidence:
            problems.append(f"capability {cid} claims 'yes' without evidence")
        for probe in evidence:
            if probe.get("kind") not in PROBE_KINDS:
                problems.append(f"capability {cid} has an unknown probe kind {probe.get('kind')!r}")
            if not str(probe.get("path") or "").strip():
                problems.append(f"capability {cid} has a probe without a path")
        ratings = cap.get("ratings") or {}
        unknown = cap.get("unknown") or []
        for tool_id, rating in ratings.items():
            if tool_id not in known:
                problems.append(f"capability {cid} rates an unknown or self tool {tool_id!r}")
            if rating not in ("yes", "partial", "no"):
                problems.append(f"capability {cid} has an invalid rating for {tool_id}")
        for tool_id in unknown:
            if tool_id not in known:
                problems.append(f"capability {cid} marks {tool_id!r} unknown but it is not a competitor")
            if tool_id in ratings:
                problems.append(f"capability {cid} both rates and marks {tool_id!r} unknown")
        for tool_id in known:
            if tool_id not in ratings and tool_id not in unknown:
                problems.append(f"capability {cid} has no entry for {tool_id!r}")
        notes = cap.get("notes") or {}
        for tool_id in notes:
            if tool_id not in known:
                problems.append(f"capability {cid} has a note for unknown tool {tool_id!r}")
    return problems


# ---------------------------------------------------------------------------
# Evidence probes

_ROUTE_DECORATOR = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete|head|options)\(\s*['\"]([^'\"]+)['\"]"
)


def _route_index(root: Path) -> list[tuple[str, str, str]]:
    """(method, path, relative file) for every declared API route.

    Reading decorators instead of importing ``app.main`` keeps the audit cheap
    and side-effect free: importing the application initialises the database and
    walks the library, which an audit has no business doing.
    """
    key = str(root)
    cached = _CACHE.get(f"routes:{key}")
    if cached:
        return cached[1]
    rows: list[tuple[str, str, str]] = []
    for folder in (root / "app",):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _ROUTE_DECORATOR.finditer(text):
                rows.append((match.group(1).upper(), match.group(2), str(path.relative_to(root))))
    _CACHE[f"routes:{key}"] = (None, rows)
    return rows


def _safe_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def probe_evidence(probe: dict, root: Path | None = None) -> tuple[bool, str]:
    """Verify one claim probe. Returns (ok, human-readable detail)."""
    root = Path(root or repo_root())
    kind = str(probe.get("kind") or "")
    if kind == "route":
        method = str(probe.get("method") or "GET").upper()
        wanted = str(probe.get("path") or "")
        if not wanted:
            return False, "route probe has no path"
        for declared_method, declared_path, file in _route_index(root):
            if declared_method == method and declared_path == wanted:
                return True, f"{method} {wanted} is declared in {file}"
        return False, f"{method} {wanted} is not declared by any router"
    relative = str(probe.get("path") or "")
    if not relative:
        return False, "probe has no path"
    path = _safe_path(root, relative)
    if path is None:
        return False, "path is outside the repository"
    if kind == "file":
        return (True, f"{relative} exists") if path.is_file() else (False, f"{relative} is missing")
    if kind in ("symbol", "test"):
        needle = str(probe.get("contains") or "")
        if not path.is_file():
            return False, f"{relative} is missing"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False, f"{relative} is unreadable"
        if needle and needle not in text:
            return False, f"{relative} no longer contains {needle!r}"
        return True, f"{relative} contains {needle!r}" if needle else f"{relative} is readable"
    return False, f"unknown probe kind {kind!r}"


def audit_claims(root: Path | None = None, persist: bool = False) -> dict:
    """Re-verify every TestForge claim and (optionally) write a receipt.

    A capability whose probes all pass keeps its rating. A capability with a
    failed probe is reported ``unverified`` regardless of what the data file
    says, and ``passed`` becomes false so CI and the dashboard can see it.
    """
    data = load()
    root = Path(root or repo_root())
    structural = validate(data)
    results = []
    verified_count = 0
    failed_count = 0
    claimed_count = 0
    not_claimed = 0
    for cap in data.get("capabilities") or []:
        own = cap.get("self") or {}
        claimed = own.get("rating") in ("yes", "partial")
        probes = []
        for probe in own.get("evidence") or []:
            ok, detail = probe_evidence(probe, root)
            probes.append({"probe": probe, "ok": ok, "detail": detail})
            if not ok:
                failed_count += 1
        ok_all = bool(probes) and all(item["ok"] for item in probes)
        if own.get("rating") == "no" and not probes:
            ok_all = True  # an honest "no" needs no evidence
        if claimed:
            claimed_count += 1
            if ok_all:
                verified_count += 1
        else:
            not_claimed += 1
        results.append({
            "id": cap.get("id"), "name": cap.get("name"),
            "rating": own.get("rating"), "note": own.get("note"),
            "verified": ok_all, "probes": probes,
        })
    generated = _now()
    receipt = {
        "schema": "testforge-claim-audit/1",
        "matrix_schema": data.get("schema"),
        "generated_at": _iso(generated),
        "expires_at": _iso(generated + datetime.timedelta(hours=RECEIPT_TTL_HOURS)),
        "suite": "market-claim-audit",
        "structural_problems": structural,
        "results": results,
        "summary": {
            "capabilities": len(results),
            "claimed": claimed_count,
            "not_claimed": not_claimed,
            "verified": verified_count,
            "failed": claimed_count - verified_count,
            "failed_probes": failed_count,
            "structural_problems": len(structural),
        },
    }
    receipt["summary"]["passed"] = not structural and receipt["summary"]["failed"] == 0
    body = json.dumps({k: v for k, v in receipt.items() if k not in ("created_path",)}, sort_keys=True,
                      ensure_ascii=True).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(body).hexdigest()
    if persist:
        target = Path(ARTIFACTS) / "market" / "claim-audit.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            receipt["receipt_path"] = str(target)
        except OSError:
            receipt["receipt_path"] = None
    return receipt


# ---------------------------------------------------------------------------
# Scores, gaps and differentiators

def _self_rating(cap: dict, audit_by_id: dict | None = None) -> str:
    own = (cap.get("self") or {}).get("rating") or "unknown"
    if audit_by_id is None:
        return own
    row = audit_by_id.get(cap.get("id"))
    if row is not None and not row.get("verified"):
        return "unverified"
    return own


def _rating_rows(cap: dict, data: dict) -> dict:
    rows = {}
    for tool_id in competitor_ids(data):
        if tool_id in (cap.get("ratings") or {}):
            rows[tool_id] = cap["ratings"][tool_id]
        elif tool_id in (cap.get("unknown") or []):
            rows[tool_id] = "unknown"
        else:  # validate() catches this; default keeps a caller alive
            rows[tool_id] = "unknown"
    return rows


def scoreboard(data: dict | None = None, audit_by_id: dict | None = None) -> list[dict]:
    data = data or load()
    caps = data.get("capabilities") or []
    rows = []
    for tool in data.get("tools") or []:
        tool_id = tool["id"]
        earned = 0.0
        known = 0
        counts = {"yes": 0, "partial": 0, "no": 0, "unknown": 0}
        for cap in caps:
            rating = _self_rating(cap, audit_by_id) if tool_id == self_tool(data)["id"] else _rating_rows(cap, data)[tool_id]
            if rating not in counts:
                rating = "unknown"
            counts[rating] += 1
            if rating == "unknown":
                continue
            known += 1
            earned += 1.0 if rating == "yes" else (0.5 if rating == "partial" else 0.0)
        rows.append({
            "id": tool_id, "name": tool["name"], "vendor": tool["vendor"],
            "kind": tool.get("kind"), "self": bool(tool.get("self")),
            "score": round(100 * earned / known, 1) if known else None,
            "known": known, "unknown": counts["unknown"],
            "yes": counts["yes"], "partial": counts["partial"], "no": counts["no"],
        })
    rows.sort(key=lambda row: (-(row["score"] or 0), row["name"].lower()))
    return rows


def gaps(data: dict | None = None, audit_by_id: dict | None = None) -> list[dict]:
    """Capabilities where at least one evidenced competitor is ahead of us."""
    data = data or load()
    out = []
    for cap in data.get("capabilities") or []:
        mine = _self_rating(cap, audit_by_id)
        if mine == "yes":
            continue
        ratings = _rating_rows(cap, data)
        leaders = [tool for tool, rating in ratings.items() if rating == "yes"]
        partials = [tool for tool, rating in ratings.items() if rating == "partial"]
        if not leaders:
            continue
        out.append({
            "id": cap.get("id"), "name": cap.get("name"), "group": cap.get("group"),
            "our_rating": mine, "leaders": leaders, "partial_leads": partials,
            "leader_count": len(leaders), "roadmap": cap.get("roadmap"),
            "why": cap.get("why"),
        })
    out.sort(key=lambda row: (-row["leader_count"], row["name"].lower()))
    return out


def differentiators(data: dict | None = None, audit_by_id: dict | None = None) -> list[dict]:
    """Capabilities we claim and no evidenced competitor claims."""
    data = data or load()
    out = []
    for cap in data.get("capabilities") or []:
        if _self_rating(cap, audit_by_id) != "yes":
            continue
        ratings = _rating_rows(cap, data)
        if any(rating == "yes" for rating in ratings.values()):
            continue
        out.append({
            "id": cap.get("id"), "name": cap.get("name"), "group": cap.get("group"),
            "partial_elsewhere": [tool for tool, rating in ratings.items() if rating == "partial"],
            "note": (cap.get("self") or {}).get("note"),
        })
    return out


def leads(data: dict | None = None, audit_by_id: dict | None = None, maximum: int = 1) -> list[dict]:
    """Capabilities where at most ``maximum`` evidenced competitors claim 'yes'."""
    data = data or load()
    out = []
    for cap in data.get("capabilities") or []:
        if _self_rating(cap, audit_by_id) != "yes":
            continue
        leaders = [tool for tool, rating in _rating_rows(cap, data).items() if rating == "yes"]
        if 0 < len(leaders) <= maximum:
            out.append({"id": cap.get("id"), "name": cap.get("name"),
                        "leaders": leaders, "note": (cap.get("self") or {}).get("note")})
    return out


def matrix(root: Path | None = None, audit: dict | None = None) -> dict:
    """Everything the dashboard, the report, MCP and the tests need."""
    data = load()
    problems = validate(data)
    audit = audit if audit is not None else audit_claims(root)
    audit_by_id = {row["id"]: row for row in audit.get("results") or []}
    caps = []
    for cap in data.get("capabilities") or []:
        own = cap.get("self") or {}
        caps.append({
            "id": cap.get("id"), "name": cap.get("name"), "group": cap.get("group"),
            "why": cap.get("why"), "roadmap": cap.get("roadmap"),
            "self": {
                "rating": _self_rating(cap, audit_by_id), "note": own.get("note"),
                "claimed": own.get("rating"),
                "verified": bool(audit_by_id.get(cap.get("id"), {}).get("verified")),
                "evidence": own.get("evidence") or [],
                "probe_results": audit_by_id.get(cap.get("id"), {}).get("probes") or [],
            },
            "ratings": _rating_rows(cap, data),
            "notes": cap.get("notes") or {},
        })
    score = scoreboard(data, audit_by_id)
    gap_rows = gaps(data, audit_by_id)
    diff_rows = differentiators(data, audit_by_id)
    self_row = next(row for row in score if row["self"])
    self_known = self_row["known"] or 1
    return {
        "schema": data.get("schema"),
        "matrix_schema": data.get("schema"),
        "last_reviewed": data.get("last_reviewed"),
        "disclaimer": data.get("disclaimer"),
        "generated_at": _iso(_now()),
        "self": self_tool(data),
        "tools": data.get("tools") or [],
        "groups": data.get("groups") or [],
        "capabilities": caps,
        "scoreboard": score,
        "gaps": gap_rows,
        "differentiators": diff_rows,
        "leads": leads(data, audit_by_id),
        "sources": data.get("sources") or [],
        "summary": {
            "tools": len(data.get("tools") or []),
            "competitors": len(competitor_ids(data)),
            "capabilities": len(caps),
            "self_coverage_pct": self_row["score"],
            "self_yes": self_row["yes"],
            "self_partial": self_row["partial"],
            "self_no": self_row["no"],
            "self_unknown": self_row["unknown"],
            "open_gaps": len(gap_rows),
            "differentiators": len(diff_rows),
            "claims_verified": audit.get("summary", {}).get("verified"),
            "claims_failed": audit.get("summary", {}).get("failed"),
            "audit_passed": bool(audit.get("summary", {}).get("passed")),
            "audit_generated_at": audit.get("generated_at"),
            "receipt_sha256": audit.get("receipt_sha256"),
            "structural_problems": problems,
        },
        "audit": {
            "passed": bool(audit.get("summary", {}).get("passed")),
            "generated_at": audit.get("generated_at"),
            "expires_at": audit.get("expires_at"),
            "receipt_sha256": audit.get("receipt_sha256"),
            "summary": audit.get("summary") or {},
        },
    }


def compact_summary(root: Path | None = None, audit: dict | None = None) -> dict:
    """Token-efficient answer for an MCP client.

    Deliberately small: a few hundred tokens instead of a 40,000-token table.
    The full matrix stays one call away.
    """
    full = matrix(root=root, audit=audit)
    summary = full["summary"]
    competitors = [row for row in full["scoreboard"] if not row["self"]]
    nearest = competitors[0] if competitors else None
    return {
        "generated_at": full["generated_at"],
        "last_reviewed": full["last_reviewed"],
        "audit_passed": summary["audit_passed"],
        "claims_verified": summary["claims_verified"],
        "claims_failed": summary["claims_failed"],
        "capabilities": summary["capabilities"],
        "tools_compared": summary["tools"],
        "testforge_coverage_pct": summary["self_coverage_pct"],
        "testforge_yes_partial_no": [summary["self_yes"], summary["self_partial"], summary["self_no"]],
        "differentiators": [row["name"] for row in full["differentiators"]][:8],
        "open_gaps": [
            {"capability": row["name"], "leaders": row["leaders"][:4],
             "ours": row["our_rating"], "disposition": row["roadmap"]}
            for row in full["gaps"][:8]
        ],
        "closest_competitor": nearest,
        "full_matrix": "GET /api/market/matrix",
        "report": "GET /api/market/report.html",
    }


def audit_gate(root: Path | None = None) -> tuple[int, dict]:
    """CI gate: persist a receipt and return (exit_code, receipt)."""
    receipt = audit_claims(root, persist=True)
    return (0 if receipt["summary"]["passed"] else 1), receipt
