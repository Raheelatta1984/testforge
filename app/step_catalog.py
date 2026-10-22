"""Reuse-first authoring: a deterministic step catalogue over the stored library.

The question this answers, in a few hundred tokens, is the one that decides
whether a suite stays maintainable: *does a step for this already exist?*

It is built from the recordings TestForge already stores — no new database, no
index server, no model. Each distinct step becomes one entry with

* the canonical action,
* a normalised **phrase** where URLs, numbers and quoted values become
  placeholders (``<url>``, ``<n>``, ``<text>``),
* the placeholder names and the allowed example values seen for them,
* the recordings and projects that use it, so a change has an impact path,
* a deterministic signature, so identical steps collapse instead of duplicating.

Three read-only functions sit on top: :func:`search`, :func:`resolve_step` and
:func:`catalog`. All of them are bounded and stable: the same library produces
the same catalogue in the same order.
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
MAX_RECORDINGS_PER_PROJECT = 200
MAX_STEPS_PER_RECORDING = 500
MAX_ENTRIES = 4000
MAX_EXAMPLES = 5
CACHE_TTL_SECONDS = 60.0
STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "this", "that", "step", "page", "click", "type",
    "open", "select", "check", "assert", "wait", "goto", "fill", "press", "then", "when", "given",
}

_URL = re.compile(r"https?://[^\s'\"<>]+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QUOTED = re.compile(r"(['\"])(.*?)\1")
_DIGITS = re.compile(r"\b\d+(?:[.,]\d+)*\b")
_SPACE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_]+|\d+")

_CACHE: dict = {"at": 0.0, "key": None, "payload": None}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalise_phrase(text, limit: int = 300) -> str:
    """Lower-case phrase with placeholders instead of volatile values."""
    value = scrub(str(text if text is not None else ""))
    value = _URL.sub(" <url> ", value)
    value = _EMAIL.sub(" <email> ", value)
    value = _QUOTED.sub(" <text> ", value)
    value = _DIGITS.sub(" <n> ", value)
    value = _SPACE.sub(" ", value).strip(" .;:,").lower()
    return clip(value, limit)


def placeholders(phrase: str) -> list[str]:
    return sorted(set(re.findall(r"<([a-z]+)>", phrase or "")))


def tokens(text: str) -> list[str]:
    out = []
    for token in _TOKEN.findall(str(text or "").lower()):
        if len(token) >= 3 and token not in STOP_WORDS:
            out.append(token)
    return out


def _selector_signature(selector) -> str:
    if not isinstance(selector, dict):
        return ""
    # Canonical step schema first: {"kind": "test_id", "primary": "place-order"}.
    parts = []
    if selector.get("primary"):
        parts.append(f"{selector.get('kind') or 'css'}={clip(str(selector.get('primary')), 120)}")
    keys = ("testid", "test_id", "role", "label", "text", "accessible_name", "id", "name",
            "css", "xpath", "automation_id", "accessibility_id", "resource_id", "window_title")
    for key in keys:
        value = selector.get(key)
        if value not in (None, "") and key != "primary":
            parts.append(f"{key}={clip(str(value), 120)}")
    if not parts:
        for key in sorted(selector):
            if key in ("x", "y", "repeat", "repeat_count", "schema_version", "strict", "xpath", "css"):
                continue
            value = selector.get(key)
            if isinstance(value, (str, int, float)) and str(value):
                parts.append(f"{key}={clip(str(value), 120)}")
    return "; ".join(parts[:6])


def _library_key() -> str:
    """Cheap fingerprint of the library, so the cache expires when data changes."""
    try:
        root = library_store.library_dir()
        projects = sorted(path.name for path in root.iterdir() if path.is_dir())[:MAX_PROJECTS]
        stamps = []
        for name in projects:
            try:
                stamps.append((name, int((root / name).stat().st_mtime)))
            except OSError:
                continue
        return hashlib.sha256(repr(stamps).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return "empty"


def _recordings() -> tuple[list[dict], dict]:
    """Read bounded recordings from the library; never raises."""
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
            recordings = library_store.list_recordings(project_id)[:MAX_RECORDINGS_PER_PROJECT]
        except Exception:
            continue
        source["recordings"] += len(recordings)
        for recording in recordings:
            try:
                full = library_store.get_recording(recording.get("id"))
            except Exception:
                continue
            if not full:
                continue
            rows.append({"project_id": project_id, "project_name": project.get("name") or "",
                         "recording": full})
    return rows, source


def _entry_key(action: str, phrase: str, selector: str) -> str:
    return hashlib.sha256(f"{action}|{phrase}|{selector}".encode("utf-8", errors="replace")).hexdigest()[:20]


def build(force: bool = False) -> dict:
    """Build (or reuse for 60 seconds) the catalogue."""
    key = _library_key()
    now = time.monotonic()
    if (not force and _CACHE["payload"] is not None and _CACHE["key"] == key
            and now - _CACHE["at"] < CACHE_TTL_SECONDS):
        return _CACHE["payload"]

    rows, source = _recordings()
    entries: dict[str, dict] = {}
    for row in rows:
        recording = row["recording"]
        steps = list(recording.get("steps") or [])
        if len(steps) > MAX_STEPS_PER_RECORDING:
            source["truncated"] = True
            steps = steps[:MAX_STEPS_PER_RECORDING]
        source["steps"] += len(steps)
        for step in steps:
            action = clip(str(step.get("action") or "unknown"), 40)
            raw_label = str(step.get("label") or "")
            raw_value = step.get("value")
            phrase = normalise_phrase(raw_label or (f"{action} {raw_value}" if raw_value else action))
            selector = _selector_signature(step.get("selector"))
            key_id = _entry_key(action, phrase, selector)
            entry = entries.get(key_id)
            if entry is None:
                entry = {
                    "id": key_id, "action": action, "phrase": phrase,
                    "label": clip(scrub(raw_label), 200) or action,
                    "placeholders": placeholders(phrase),
                    "selector": selector,
                    "occurrences": 0,
                    "recordings": [], "projects": [],
                    "examples": [], "unverified": False,
                }
                entries[key_id] = entry
            entry["occurrences"] += 1
            if len(entry["recordings"]) < 6 and not any(
                    item["id"] == recording.get("id") for item in entry["recordings"]):
                entry["recordings"].append({"id": recording.get("id"), "name": recording.get("name") or "",
                                            "project_id": row["project_id"]})
            if row["project_id"] not in entry["projects"]:
                entry["projects"].append(row["project_id"])
            example = clip(scrub(str(raw_value if raw_value not in (None, "") else raw_label)), 120)
            if example and example not in entry["examples"] and len(entry["examples"]) < MAX_EXAMPLES:
                entry["examples"].append(example)
            if recording.get("unverified"):
                entry["unverified"] = True

    items = list(entries.values())
    items.sort(key=lambda item: (-item["occurrences"], item["action"], item["phrase"], item["id"]))
    if len(items) > MAX_ENTRIES:
        items = items[:MAX_ENTRIES]
        source["truncated"] = True
    by_action: dict[str, int] = {}
    for item in items:
        by_action[item["action"]] = by_action.get(item["action"], 0) + 1
    payload = {
        "schema": "testforge-step-catalog/1",
        "generated_at": _now(),
        "source": source,
        "actions": [{"action": action, "steps": count}
                    for action, count in sorted(by_action.items(), key=lambda pair: (-pair[1], pair[0]))],
        "entries": items,
        "notes": [
            "Built from stored recordings only; no model and no external index.",
            "Phrases replace volatile values with <url>, <n> and <text> so the same step is not duplicated by a "
            "different order number or product name.",
            "Examples are redacted. Treat them as evidence of where a step is used, not as test data.",
        ],
    }
    _CACHE.update({"at": now, "key": key, "payload": payload})
    return payload


def catalog(project_id: str | None = None, action: str | None = None, limit: int = 500) -> dict:
    payload = build()
    entries = payload["entries"]
    if project_id:
        entries = [entry for entry in entries if project_id in entry["projects"]]
    if action:
        entries = [entry for entry in entries if entry["action"] == action]
    cap = max(1, min(int(limit or 500), MAX_ENTRIES))
    return {**payload, "entries": entries[:cap],
            "filtered": {"project_id": project_id or None, "action": action or None,
                         "returned": min(len(entries), cap), "matched": len(entries)}}


def search(query: str, project_id: str | None = None, action: str | None = None, limit: int = 25) -> dict:
    """Rank catalogue entries against a phrase. Deterministic, no index needed."""
    phrase = normalise_phrase(query)
    wanted = tokens(query)
    if not wanted:
        return {"schema": "testforge-step-search/1", "query": clip(str(query or ""), 200), "results": [],
                "note": "Give at least one meaningful word to search for."}
    entries = catalog(project_id=project_id, action=action, limit=MAX_ENTRIES)["entries"]
    total = max(1, len(entries))
    # Document frequency over entries gives rare words more weight, the same idea
    # the regression selector uses. Deterministic and cheap.
    freq: dict[str, int] = {}
    for entry in entries:
        entry_tokens = set(tokens(entry["phrase"]) + tokens(entry["label"]) + tokens(entry["action"]))
        for token in wanted:
            if token in entry_tokens:
                freq[token] = freq.get(token, 0) + 1
    scored = []
    for entry in entries:
        haystack = f"{entry['label']} {entry['phrase']} {entry['action']} {entry['selector']}".lower()
        entry_tokens = set(tokens(haystack))
        score = 0.0
        matched = []
        for token in wanted:
            if token in entry_tokens:
                matched.append(token)
                idf = math.log(1 + total / max(1, freq.get(token, 1)))
                score += 1.0 + idf
        if not matched:
            continue
        if phrase and phrase == entry["phrase"]:
            score += 6.0
        elif phrase and phrase in entry["phrase"]:
            score += 2.0
        score += min(1.0, entry["occurrences"] / 10)
        scored.append({"entry": entry, "score": round(score, 3), "matched": matched})
    scored.sort(key=lambda item: (-item["score"], -item["entry"]["occurrences"], item["entry"]["phrase"]))
    cap = max(1, min(int(limit or 25), 100))
    return {
        "schema": "testforge-step-search/1",
        "query": clip(str(query or ""), 200),
        "normalised": phrase,
        "results": [{"score": item["score"], "matched": item["matched"], **item["entry"]}
                    for item in scored[:cap]],
        "matched": len(scored),
        "note": "Reuse beats duplication: if a result already does what you need, reference it instead of "
                "recording a near-identical step.",
    }


def resolve_step(phrase: str, project_id: str | None = None, limit: int = 5) -> dict:
    """Would this phrase bind an existing step definition?

    ``bound`` — one existing step matches closely.
    ``ambiguous`` — several plausible definitions; pick one deliberately.
    ``unbound`` — no existing step covers it; author a new one.

    Thresholds are explicit so the answer can be argued with: 0.9 exactness for
    bound, 0.55 similarity with a 0.08 margin for ambiguous.
    """
    text = str(phrase or "").strip()
    if not text:
        raise ValueError("Paste the step phrase to resolve")
    normalised = normalise_phrase(text)
    wanted = tokens(text)
    entries = catalog(project_id=project_id, limit=MAX_ENTRIES)["entries"]
    results = search(text, project_id=project_id, limit=MAX_ENTRIES)["results"]
    total = max(1, len(entries))

    def similarity(entry: dict) -> float:
        haystack = set(tokens(entry["phrase"]) + tokens(entry["label"]) + tokens(entry["action"]))
        if not wanted or not haystack:
            return 0.0
        overlap = len(set(wanted) & haystack) / len(set(wanted) | haystack)
        exact = 1.0 if normalised == entry["phrase"] else (0.8 if normalised in entry["phrase"] else 0.0)
        return min(1.0, 0.65 * overlap + 0.35 * exact + (0.05 if entry["occurrences"] > 1 else 0.0))

    ranked = []
    for entry in entries:
        score = similarity(entry)
        if score <= 0:
            continue
        ranked.append({"score": round(score, 3), "id": entry["id"], "action": entry["action"],
                       "phrase": entry["phrase"], "label": entry["label"],
                       "placeholders": entry["placeholders"], "examples": entry["examples"],
                       "recordings": entry["recordings"]})
    ranked.sort(key=lambda item: (-item["score"], item["phrase"]))
    top = ranked[: max(1, min(int(limit or 5), 20))]
    best = top[0] if top else None
    runner_up = top[1] if len(top) > 1 else None
    if best and best["score"] >= 0.9:
        status = "bound"
    elif best and best["score"] >= 0.55:
        status = "ambiguous"
    else:
        status = "unbound"
    margin = round(best["score"] - runner_up["score"], 3) if best and runner_up else None
    if status == "ambiguous" and margin is not None and margin >= 0.25:
        status = "bound" if best["score"] >= 0.8 else "ambiguous"
    return {
        "schema": "testforge-step-resolve/1",
        "phrase": clip(text, 300),
        "normalised": normalised,
        "placeholders": placeholders(normalised),
        "status": status,
        "best": best,
        "margin": margin,
        "candidates": top,
        "catalog_size": len(entries),
        "advice": {
            "bound": "Reuse the existing definition; do not record a near-duplicate.",
            "ambiguous": "Several definitions are close. Choose deliberately or tighten the phrase.",
            "unbound": "No existing definition covers this phrase. Recording a new step is justified.",
        }[status] + (" (searched " + str(total) + " unique step definitions)"),
    }


def compact_summary(limit: int = 12) -> dict:
    """Token-efficient view for an MCP client."""
    payload = build()
    return {
        "generated_at": payload["generated_at"],
        "projects": payload["source"]["projects"],
        "recordings": payload["source"]["recordings"],
        "steps": payload["source"]["steps"],
        "unique_steps": len(payload["entries"]),
        "actions": payload["actions"][:10],
        "most_reused": [
            {"phrase": entry["phrase"], "action": entry["action"], "occurrences": entry["occurrences"],
             "recordings": len(entry["recordings"])}
            for entry in payload["entries"][: max(1, min(int(limit or 12), 50))]
        ],
        "full_catalog": "GET /api/steps/catalog",
    }
