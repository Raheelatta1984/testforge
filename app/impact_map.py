"""Blast radius: which stored tests does a change actually reach?

TestForge already knows every recording, its URL, its step labels, its locators
and its project. That is a dependency graph — it just was not written down. This
module writes it down and answers the regression question from it:

* a changed file path contributes tokens (``checkout-components`` → checkout,
  components, static …), weighted by how rare they are across the catalogue;
* a diff contributes identifiers and route strings;
* an explicit route, label or selector narrows the answer;
* recordings tagged ``smoke`` or ``critical`` always run, because a gate that
  skips them is not a gate.

The answer is deterministic, bounded and explainable: every selected recording
carries the reasons and the matched terms, and the caller gets a ready
``POST /api/runs/batch`` payload for the subset it wants to run.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime, timezone

from app import library_store
from app.qa_agents.common import clip, scrub

MAX_PROJECTS = 60
MAX_RECORDINGS = 800
MAX_STEPS = 300
MAX_TERMS = 200
MAX_SELECTED = 200
PRIORITY_TAGS = {"smoke", "critical", "p1", "always"}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]+|\d+")
_ROUTE = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9_\-{}\[\]$:.]+/?){1,8}")
_IDENT = re.compile(r"(?:def|class|function|const|let|var|async\s+function)\s+([A-Za-z_][\w]*)")
STOP = {
    "src", "app", "apps", "lib", "libs", "static", "test", "tests", "spec", "index", "main",
    "utils", "util", "helpers", "helper", "common", "components", "component", "the", "and",
    "for", "with", "from", "get", "set", "new", "use", "api", "def", "function", "const",
    "class", "return", "export", "import", "default", "page", "pages", "view", "views", "node",
    "modules", "module", "file", "files", "data", "item", "items", "value", "values", "name",
    "type", "tmp", "dist", "build", "router", "routes", "route", "controller", "service",
    "model", "models", "schema", "config", "types", "string", "none", "true", "false", "html",
    "css", "json", "yml", "yaml", "md", "js", "ts", "tsx", "jsx", "py", "testforge",
}

_CACHE: dict = {"at": 0.0, "key": None, "payload": None}
CACHE_TTL_SECONDS = 60.0


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tokens(text) -> list[str]:
    """Split camelCase, kebab-case, snake_case and paths into meaningful words."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    spaced = spaced.replace("_", " ").replace("-", " ").replace("/", " ").replace(".", " ")
    out = []
    for token in _TOKEN.findall(spaced):
        lowered = token.lower()
        if len(lowered) >= 3 and lowered not in STOP and not lowered.isdigit():
            out.append(lowered)
    return list(dict.fromkeys(out))


def _route_paths(text: str) -> list[str]:
    found = []
    for value in _ROUTE.findall(str(text or "")):
        value = value.rstrip(".,;:'\")")
        if len(value) > 2 and value not in found:
            found.append(value[:200])
    return found[:50]


def _recordings() -> tuple[list[dict], dict]:
    source = {"projects": 0, "recordings": 0, "steps": 0, "truncated": False}
    rows: list[dict] = []
    try:
        projects = (library_store.list_projects_fast() or library_store.list_projects())[:MAX_PROJECTS]
    except Exception:
        return [], {**source, "error": "library unavailable"}
    source["projects"] = len(projects)
    for project in projects:
        project_id = project.get("id")
        if not project_id:
            continue
        try:
            recordings = library_store.list_recordings(project_id)
        except Exception:
            continue
        for recording in recordings:
            if len(rows) >= MAX_RECORDINGS:
                source["truncated"] = True
                break
            try:
                full = library_store.get_recording(recording.get("id"))
            except Exception:
                continue
            if not full:
                continue
            rows.append({"project_id": project_id, "project_name": project.get("name") or "",
                         "recording": full})
        if len(rows) >= MAX_RECORDINGS:
            break
    source["recordings"] = len(rows)
    source["steps"] = sum(min(len(row["recording"].get("steps") or []), MAX_STEPS) for row in rows)
    return rows, source


def _library_key() -> str:
    try:
        root = library_store.library_dir()
        stamps = sorted((path.name, int(path.stat().st_mtime)) for path in root.iterdir() if path.is_dir())
        return hashlib.sha256(repr(stamps[:MAX_PROJECTS]).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "empty"


def _recording_row(row: dict) -> dict:
    recording = row["recording"]
    steps = list(recording.get("steps") or [])[:MAX_STEPS]
    labels = [clip(scrub(step.get("label") or ""), 160) for step in steps]
    selectors = []
    for step in steps:
        selector = step.get("selector") if isinstance(step.get("selector"), dict) else {}
        if selector.get("primary"):
            selectors.append(f"{selector.get('kind') or 'css'}:{clip(str(selector.get('primary')), 100)}")
        for key in ("testid", "test_id", "role", "label", "accessible_name", "id", "name", "css",
                    "automation_id", "accessibility_id", "resource_id", "window_title"):
            value = selector.get(key)
            if value not in (None, ""):
                selectors.append(f"{key}:{clip(str(value), 100)}")
    tags = [tag.strip().lower() for tag in str(recording.get("tags") or "").split(",") if tag.strip()]
    blob = " ".join([recording.get("name") or "", recording.get("purpose") or "",
                     recording.get("start_url") or "", *labels, *selectors])
    return {
        "id": recording.get("id"),
        "name": recording.get("name") or "",
        "project_id": row["project_id"],
        "project_name": row["project_name"],
        "start_url": recording.get("start_url") or "",
        "tags": tags,
        "unverified": bool(recording.get("unverified")),
        "step_count": len(recording.get("steps") or []),
        "labels": labels[:40],
        "selectors": selectors[:40],
        "tokens": set(_tokens(blob)),
        "routes": set(_route_paths(" ".join([recording.get("start_url") or "", *labels]))),
    }


def build_map() -> dict:
    key = _library_key()
    now = time.monotonic()
    if (_CACHE["payload"] is not None and _CACHE["key"] == key
            and now - _CACHE["at"] < CACHE_TTL_SECONDS):
        return _CACHE["payload"]
    rows, source = _recordings()
    recordings = [_recording_row(row) for row in rows]
    projects: dict[str, dict] = {}
    for row in recordings:
        project = projects.setdefault(row["project_id"], {
            "id": row["project_id"], "name": row["project_name"], "recordings": 0, "urls": set(), "tags": set()})
        project["recordings"] += 1
        if row["start_url"]:
            project["urls"].add(clip(row["start_url"], 300))
        project["tags"].update(row["tags"])
    edges = []
    for row in recordings:
        edges.append({"from": f"project:{row['project_id']}", "to": f"recording:{row['id']}", "kind": "contains"})
        if row["start_url"]:
            edges.append({"from": f"recording:{row['id']}", "to": f"url:{row['start_url']}", "kind": "navigates"})
        for label in row["labels"][:12]:
            edges.append({"from": f"recording:{row['id']}", "to": f"step:{clip(label, 80)}", "kind": "uses-step"})
        for selector in row["selectors"][:12]:
            edges.append({"from": f"recording:{row['id']}", "to": f"locator:{selector}", "kind": "targets"})
    payload = {
        "schema": "testforge-impact-map/1",
        "generated_at": _now(),
        "source": source,
        "nodes": {
            "projects": [{"id": item["id"], "name": item["name"], "recordings": item["recordings"],
                          "urls": sorted(item["urls"])[:10], "tags": sorted(item["tags"])}
                         for item in sorted(projects.values(), key=lambda value: value["name"].lower())],
            "recordings": [{"id": row["id"], "name": row["name"], "project_id": row["project_id"],
                            "start_url": row["start_url"], "step_count": row["step_count"],
                            "tags": row["tags"]} for row in sorted(recordings, key=lambda r: r["name"].lower())],
        },
        "edges": edges[:20000],
        "stats": {"projects": len(projects), "recordings": len(recordings),
                  "edges": len(edges[:20000]), "truncated": source.get("truncated", False)},
        "notes": [
            "Derived from stored recordings: project → recording → URL, step labels and locators.",
            "Every edge is explainable; an impact answer names the term that matched.",
            "This is a library graph, not a source-code call graph. Static-analysis tools map code; "
            "TestForge maps what it can actually replay.",
        ],
    }
    _CACHE.update({"at": now, "key": key, "payload": (payload, recordings)})
    return payload, recordings


def _idf(recordings: list[dict], terms: list[str]) -> dict:
    total = max(1, len(recordings))
    out = {}
    for term in terms:
        count = sum(1 for row in recordings if term in row["tokens"] or term in row["routes"])
        out[term] = math.log(1 + total / max(1, count))
    return out


def analyse(changed_files=None, diff: str = "", routes=None, labels=None, project_id: str | None = None,
            max_tests: int = 25, include_priorities: bool = True) -> dict:
    """Return the recordings a change reaches, with reasons and a batch payload."""
    files = [str(item).strip()[:300] for item in (changed_files or []) if str(item).strip()][:100]
    explicit_routes = [str(item).strip()[:300] for item in (routes or []) if str(item).strip()][:50]
    explicit_labels = [str(item).strip()[:200] for item in (labels or []) if str(item).strip()][:50]
    diff_text = str(diff or "")[:200_000]
    if not (files or explicit_routes or explicit_labels or diff_text.strip()):
        raise ValueError("Provide changed_files, routes, labels or a diff; an empty change set selects nothing")
    payload, recordings = build_map()
    if project_id:
        recordings = [row for row in recordings if row["project_id"] == project_id]

    # Terms: file paths, identifiers and routes from the diff, explicit routes/labels.
    terms: list[str] = []
    for path in files:
        terms.extend(_tokens(path))
    for identifier in _IDENT.findall(diff_text)[:MAX_TERMS]:
        terms.extend(_tokens(identifier))
    for path in _route_paths(diff_text)[:MAX_TERMS]:
        parts = [part for part in path.strip("/").split("/") if part]
        terms.extend(_tokens(" ".join(parts)))
    for path in explicit_routes:
        parts = [part for part in path.strip("/").split("/") if part]
        terms.extend(_tokens(" ".join(parts)))
    for label in explicit_labels:
        terms.extend(_tokens(label))
    terms = list(dict.fromkeys(terms))[:MAX_TERMS]
    if not terms:
        raise ValueError("The change set produced no usable terms (paths, routes or identifiers)")
    weights = _idf(recordings, terms)
    wanted_routes = [route.rstrip("/") or "/" for route in explicit_routes + _route_paths(diff_text)]

    selected = []
    for row in recordings:
        matched = [term for term in terms if term in row["tokens"] or term in row["routes"]]
        route_hits = [route for route in wanted_routes
                      if route and (route in (row["start_url"] or "") or
                                    any(route in (label or "") for label in row["labels"]))]
        label_hits = [label for label in explicit_labels
                      if label.lower() in " ".join(row["labels"]).lower() or
                      label.lower() in (row["name"] or "").lower()]
        priority = bool(include_priorities and (PRIORITY_TAGS & set(row["tags"])))
        if not (matched or route_hits or label_hits or priority):
            continue
        score = sum(weights.get(term, 1.0) for term in matched)
        score += 3.0 * len(label_hits) + 2.0 * len(route_hits)
        reasons = []
        if matched:
            reasons.append("matched " + ", ".join(sorted(matched)[:6]))
        if route_hits:
            reasons.append("route " + ", ".join(route_hits[:3]))
        if label_hits:
            reasons.append("step label " + ", ".join(label_hits[:3]))
        if priority:
            reasons.append("always-run tag (" + ", ".join(sorted(PRIORITY_TAGS & set(row["tags"]))) + ")")
            score += 4.0
        selected.append({
            "id": row["id"], "name": row["name"], "project_id": row["project_id"],
            "project_name": row["project_name"], "start_url": row["start_url"],
            "tags": row["tags"], "unverified": row["unverified"],
            "step_count": row["step_count"], "score": round(score, 3),
            "matched_terms": sorted(matched)[:12], "reasons": reasons,
        })
    selected.sort(key=lambda item: (-item["score"], item["name"].lower(), item["id"]))
    cap = max(1, min(int(max_tests or 25), MAX_SELECTED))
    chosen = selected[:cap]
    total = len(recordings) or 1
    return {
        "schema": "testforge-impact-analysis/1",
        "generated_at": _now(),
        "change": {"changed_files": files, "routes": explicit_routes, "labels": explicit_labels,
                   "diff_characters": len(diff_text)},
        "selected": chosen,
        "selected_count": len(chosen),
        "affected_total": len(selected),
        "catalogue_size": len(recordings),
        "reduction_pct": round(100 * (1 - len(chosen) / total), 1),
        "terms": [{"term": term, "weight": round(weights.get(term, 1.0), 3)} for term in terms],
        "batch_payload": {
            "recording_ids": [item["id"] for item in chosen],
            "name": "Impact selection · " + _now(),
            "screenshots": "failure",
            "endpoint": "POST /api/runs/batch",
            "note": "Send this body unchanged to run exactly the selected recordings.",
        },
        "unmatched_hint": ("No stored test matched. That is a coverage answer, not a failure: author a test "
                           "for this change or widen the terms.") if not selected else None,
        "notes": [
            "IDF weighting stops a word that appears in every test (\"page\") from selecting the whole catalogue.",
            "Smoke/critical recordings always run when include_priorities is on.",
            "Unverified recordings are labelled; they still match because they are still tests.",
            "Library graph only: code that no recording exercises cannot be mapped.",
        ],
    }


def compact_summary(limit: int = 8) -> dict:
    payload, _ = build_map()
    stats = payload["stats"]
    projects = payload["nodes"]["projects"][: max(1, min(int(limit or 8), 40))]
    return {
        "generated_at": payload["generated_at"],
        "projects": stats.get("projects"),
        "recordings": stats.get("recordings"),
        "edges": stats.get("edges"),
        "largest_projects": [{"name": item["name"], "recordings": item["recordings"]} for item in projects],
        "analyse": "POST /api/impact/analyse with changed_files/routes/labels",
        "map": "GET /api/impact/map",
    }
