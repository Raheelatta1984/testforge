"""Regression Selection: which existing tests a code change should run.

Changed files, identifiers and routes from a diff are matched against each
test's name, tags, start URL and step labels. Matching is IDF-weighted, so a
word that appears in every test (for example "page") does not select them all.
Tests tagged smoke or critical always run. The output includes a ready-made
batch payload for POST /api/runs/batch.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlparse

from .common import MAX_ITEMS, as_text, envelope, finding, lines

STOP = {
    "src", "app", "apps", "lib", "libs", "static", "js", "ts", "tsx", "jsx", "py", "html", "css", "json", "yml", "yaml",
    "md", "test", "tests", "spec", "index", "main", "utils", "util", "helpers", "common", "components", "component",
    "the", "and", "for", "with", "from", "get", "set", "new", "use", "api", "def", "function", "const", "class",
    "return", "export", "import", "default", "page", "pages", "view", "views", "node", "modules", "module", "file",
    "files", "data", "item", "items", "value", "values", "name", "type", "tmp", "dist", "build", "router", "routes",
    "route", "controller", "service", "model", "models", "schema", "config", "types", "string", "none", "true", "false",
}
PRIORITY_TAGS = {"smoke", "critical", "p1", "always"}


def _tokens(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    words = re.findall(r"[A-Za-z][A-Za-z0-9]+", spaced)
    out = []
    for word in words:
        lowered = word.lower()
        if len(lowered) >= 3 and lowered not in STOP:
            out.append(lowered)
    return out


def _changed(diff: str | None, files: list[str] | None):
    paths: list[str] = []
    idents: list[str] = []
    for item in files or []:
        value = str(item).strip()
        if value:
            paths.append(value[:300])
    for line in lines(as_text(diff or "", field="diff")):
        match = re.match(r"^\+\+\+ b/(.+)$|^diff --git a/\S+ b/(.+)$", line)
        if match:
            paths.append((match.group(1) or match.group(2))[:300])
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---")):
            for hit in re.findall(r"(?:def|class|function|const|let|var)\s+([A-Za-z_][\w]*)", line):
                idents.append(hit)
            for hit in re.findall(r"['\"](/[A-Za-z0-9/_\-{}:.]+)['\"]", line):
                idents.append(hit)
    unique_paths = list(dict.fromkeys(paths))[:500]
    return unique_paths, list(dict.fromkeys(idents))[:500]


def _test_tokens(test: dict) -> set[str]:
    parts = [str(test.get("name") or "")]
    parts.append(" ".join(str(t) for t in (test.get("tags") or [])))
    url = str(test.get("start_url") or "")
    if url:
        parsed = urlparse(url)
        parts.append(parsed.path)
        parts.append(parsed.netloc)
    parts.extend(str(label) for label in (test.get("labels") or [])[:80])
    tokens: set[str] = set()
    for part in parts:
        tokens.update(_tokens(part))
    return tokens


def select(tests: list[dict], diff: str | None = None, changed_files: list[str] | None = None,
           max_tests: int = 25) -> dict:
    max_tests = max(1, min(int(max_tests or 25), 200))
    files, idents = _changed(diff, changed_files)
    if not files and not idents:
        raise ValueError("Provide the changed file names or a unified diff.")

    change_tokens: dict[str, str] = {}
    for path in files:
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for token in _tokens(path.replace("/", " ").replace(".", " ")) + _tokens(stem):
            change_tokens.setdefault(token, path)
    for ident in idents:
        for token in _tokens(ident):
            change_tokens.setdefault(token, ident)

    catalogue = []
    for test in tests[:500]:
        catalogue.append({**test, "_tokens": _test_tokens(test)})
    n = max(1, len(catalogue))
    df: dict[str, int] = {}
    for item in catalogue:
        for token in item["_tokens"]:
            df[token] = df.get(token, 0) + 1

    scored = []
    matched_sources: set[str] = set()
    for item in catalogue:
        shared = [t for t in change_tokens if t in item["_tokens"]]
        score = 0.0
        reasons = []
        for token in shared:
            weight = math.log((n + 1) / (df.get(token, 1) + 0.5)) + 1
            score += weight
            reasons.append(token)
        tags = {str(t).lower() for t in (item.get("tags") or [])}
        always = bool(tags & PRIORITY_TAGS)
        if always:
            score += 0.5
        if score > 0 or always:
            scored.append({"item": item, "score": round(score, 2), "reasons": reasons[:12],
                           "always": always, "via": sorted({change_tokens[t] for t in shared})[:6]})
            matched_sources.update(change_tokens[t] for t in shared)

    scored.sort(key=lambda r: (not r["always"], -r["score"], str(r["item"].get("name") or "")))
    top = max((r["score"] for r in scored if not r["always"]), default=0.0)
    selected = []
    for row in scored[:max_tests]:
        item = row["item"]
        if row["always"] and set(str(t).lower() for t in item.get("tags") or []) & {"smoke", "critical", "p1"}:
            priority = "critical"
        elif top and row["score"] >= 0.6 * top:
            priority = "high"
        elif top and row["score"] >= 0.3 * top:
            priority = "medium"
        else:
            priority = "low"
        selected.append({"id": item.get("id"), "name": str(item.get("name") or "")[:200], "priority": priority,
                         "score": row["score"], "reasons": row["reasons"], "changed_by": row["via"],
                         "always_run": row["always"]})

    unmatched = [p for p in files if p not in matched_sources][:MAX_ITEMS]
    findings: list[dict] = []
    for path in unmatched[:20]:
        findings.append(finding("REG-01", "medium", "No test covers this change",
                                f"No test name, URL or step mentions '{path}'.",
                                "Add a recording for this area, or accept the risk for a low-traffic file."))
    if len(scored) > max_tests:
        findings.append(finding("REG-02", "info", "Selection capped",
                                f"{len(scored)} tests matched; the first {max_tests} are listed.",
                                "Raise max_tests or run the full suite for a release candidate."))
    if not tests:
        findings.append(finding("REG-03", "high", "No test catalogue supplied",
                                "There are no tests to select from.",
                                "Select a project with recordings, or send a list of tests."))
    coverage = round(100 * len(selected) / n, 1) if tests else 0.0
    batch = {"recording_ids": [s["id"] for s in selected if s["id"]], "name": "Regression selection",
             "screenshots": "failure"}
    summary = (f"{len(files)} changed file(s), {len(idents)} identifier(s)/route(s): "
               f"{len(selected)} of {len(tests)} test(s) selected ({coverage}% of the catalogue).")
    verdict = "selected" if selected else "no matching tests"
    return envelope("regression", "Regression Selection", summary, verdict, findings,
                    artifacts={"selected": selected, "unmatched_changes": unmatched,
                               "changed_files": files[:MAX_ITEMS], "identifiers": idents[:MAX_ITEMS],
                               "batch_payload": batch, "catalogue_size": len(tests),
                               "coverage_percent": coverage}, scored=False)
