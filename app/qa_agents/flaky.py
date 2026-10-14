"""Flaky Test Analysis: timing patterns and root-cause hypotheses from run history.

The existing plan runner marks a case 'flaky' when it passes on a retry. That
answers "did it pass eventually?" but not "why is it unstable?". This agent
reads the history of one recording and looks at outcome flips, the duration of
passing versus failing runs, the clustering of error messages, the hour of day
and the browser, and proposes ranked causes with the evidence for each.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from datetime import datetime

from .common import MAX_ITEMS, clip, envelope, finding, scrub
from .triage import classify

MIN_RUNS = 3
PASS = {"passed"}
FAIL = {"failed", "error"}

TIMEOUT = re.compile(r"timeout|timed out|exceeded|waiting for", re.I)
# Plain "waiting for locator" is a timeout (counted as timing); these are genuine locator failures.
LOCATOR = re.compile(r"strict mode|detached|not attached|intercept|not visible|stale element|no such element|element is covered", re.I)
NETWORK = re.compile(r"net::|ECONN|\b50[234]\b|socket|reset|DNS|ENOTFOUND|getaddrinfo", re.I)
DATA = re.compile(r"duplicate|already exists|no such record|not found|insufficient|expired|test data|fixture", re.I)


def _outcome(status) -> str | None:
    value = str(status or "").lower()
    if value in PASS:
        return "pass"
    if value in FAIL:
        return "fail"
    return None  # queued, running, cancelled: not evidence either way


def _ms(run: dict) -> float | None:
    value = run.get("duration_ms")
    if value is None and run.get("elapsed_seconds") is not None:
        value = float(run["elapsed_seconds"]) * 1000
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hour(run: dict) -> int | None:
    raw = run.get("started_at") or run.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).hour
    except ValueError:
        return None


def _signature(error: str) -> str:
    text = scrub(error).lower()
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<id>", text)
    text = re.sub(r"\b\d+(\.\d+)?\b", "<n>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<q>", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    return clip(" ".join(text.split()), 140)


def analyse(runs: list[dict], name: str | None = None) -> dict:
    cleaned = []
    for run in (runs or [])[:200]:
        outcome = _outcome(run.get("status") or run.get("outcome"))
        if outcome:
            cleaned.append({**run, "_outcome": outcome, "_ms": _ms(run), "_hour": _hour(run),
                            "_error": scrub(str(run.get("error") or "")) if run.get("error") else ""})
    cleaned.sort(key=lambda r: str(r.get("started_at") or r.get("created_at") or ""))
    findings: list[dict] = []
    label = name or "this test"

    total = len(cleaned)
    if total < MIN_RUNS:
        findings.append(finding("FLK-00", "info", "Not enough history",
                                f"{total} finished run(s); at least {MIN_RUNS} are needed to judge stability.",
                                "Run the test a few more times, then analyse again."))
        return envelope("flaky", "Flaky Test Analysis", f"{label}: {total} run(s), insufficient history.",
                        "insufficient history", findings, artifacts={"stats": {"runs": total}})

    passes = [r for r in cleaned if r["_outcome"] == "pass"]
    fails = [r for r in cleaned if r["_outcome"] == "fail"]
    pass_rate = len(passes) / total
    fail_rate = len(fails) / total
    flips = sum(1 for a, b in zip(cleaned, cleaned[1:]) if a["_outcome"] != b["_outcome"])
    flip_rate = flips / (total - 1)
    mixedness = 2 * min(pass_rate, fail_rate)
    flakiness = round(100 * (0.5 * mixedness + 0.5 * min(1.0, flip_rate * 2)))

    if not fails:
        verdict = "stable"
    elif not passes:
        verdict = "consistently failing"
    elif flip_rate >= 0.4:
        verdict = "flaky"
    elif flip_rate >= 0.15:
        verdict = "intermittent"
    else:
        verdict = "clustered failures"

    pass_ms = [r["_ms"] for r in passes if r["_ms"] is not None]
    fail_ms = [r["_ms"] for r in fails if r["_ms"] is not None]
    all_ms = pass_ms + fail_ms
    timing = {"median_pass_ms": None, "median_fail_ms": None, "p90_all_ms": None, "cv": None, "ratio": None}
    if pass_ms:
        timing["median_pass_ms"] = round(statistics.median(pass_ms))
    if fail_ms:
        timing["median_fail_ms"] = round(statistics.median(fail_ms))
    if len(all_ms) >= 4:
        ordered = sorted(all_ms)
        timing["p90_all_ms"] = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))])
        mean = statistics.mean(all_ms)
        if mean > 0:
            timing["cv"] = round(statistics.pstdev(all_ms) / mean, 2)
    if timing["median_pass_ms"] and timing["median_fail_ms"]:
        timing["ratio"] = round(timing["median_fail_ms"] / timing["median_pass_ms"], 2)

    fail_errors = [r["_error"] for r in fails if r["_error"]]
    signatures = Counter(_signature(e) for e in fail_errors)
    top_signatures = [{"signature": sig, "count": n} for sig, n in signatures.most_common(5)]

    fail_hours = Counter(r["_hour"] for r in fails if r["_hour"] is not None)
    cluster_hours = [h for h, _n in fail_hours.most_common(3)]
    clustered_share = (sum(fail_hours[h] for h in cluster_hours) / len(fails)) if fail_hours else 0.0
    time_pattern = len(fails) >= 3 and clustered_share >= 0.75 and len(fail_hours) >= 2

    browsers = {}
    for run in cleaned:
        key = str(run.get("browser") or "").strip()
        if key:
            browsers.setdefault(key, [0, 0])
            browsers[key][0] += 1
            browsers[key][1] += run["_outcome"] == "fail"
    browser_rates = {k: round(f / n, 2) for k, (n, f) in browsers.items() if n}
    browser_split = (len(browser_rates) >= 2 and max(browser_rates.values()) - min(browser_rates.values()) >= 0.4)

    retried_passes = sum(1 for r in passes if int(r.get("retries") or 0) > 0)

    def share(pattern):
        if not fail_errors:
            return 0.0
        return sum(1 for e in fail_errors if pattern.search(e)) / len(fail_errors)

    timeout_share, locator_share = share(TIMEOUT), share(LOCATOR)
    network_share, data_share = share(NETWORK), share(DATA)

    hypotheses = []
    if verdict in ("flaky", "intermittent", "clustered failures"):
        if timeout_share >= 0.3 or (timing["ratio"] and timing["ratio"] >= 1.5) or (timing["cv"] and timing["cv"] >= 0.5):
            hypotheses.append({
                "cause": "Timing / race condition", "confidence": round(min(0.9, 0.4 + timeout_share * 0.5), 2),
                "evidence": [f"{round(timeout_share * 100)}% of failures are waits or timeouts",
                             f"median failing run {timing['ratio']}x a passing run" if timing["ratio"] else
                             f"run-time variation (CV) {timing['cv']}"],
                "fix": "Replace sleeps and fixed timeouts with waits on app state (response, visible text, network idle)."})
        if locator_share >= 0.3:
            hypotheses.append({
                "cause": "Unstable locator or DOM churn", "confidence": round(min(0.9, 0.4 + locator_share * 0.5), 2),
                "evidence": [f"{round(locator_share * 100)}% of failures mention locators, detachment or interception"],
                "fix": "Use role, label or data-testid locators; wait for the element to be stable before acting."})
        if network_share >= 0.3 or time_pattern:
            evidence = [f"{round(network_share * 100)}% of failures carry network/gateway signals"]
            if time_pattern:
                evidence.append(f"failures cluster in hours {sorted(cluster_hours)} (UTC) - "
                                f"{round(clustered_share * 100)}% of failures")
            hypotheses.append({
                "cause": "Environment / network instability", "confidence": round(min(0.85, 0.35 + network_share * 0.5 + (0.15 if time_pattern else 0)), 2),
                "evidence": evidence,
                "fix": "Check the environment's health at those hours; add a retry only for the network step, not the whole test."})
        if data_share >= 0.3:
            hypotheses.append({
                "cause": "Test-data dependency or shared state", "confidence": round(min(0.85, 0.35 + data_share * 0.5), 2),
                "evidence": [f"{round(data_share * 100)}% of failures mention duplicates, missing or expired data"],
                "fix": "Create unique data per run and clean it up; do not share accounts between parallel runs."})
        if browser_split:
            hypotheses.append({
                "cause": "Browser-specific behaviour", "confidence": 0.6,
                "evidence": ["failure rate by browser: " + ", ".join(f"{k} {round(v * 100)}%" for k, v in browser_rates.items())],
                "fix": "Reproduce on the weaker browser alone; fix the engine-specific wait or CSS."})
        if not hypotheses:
            hypotheses.append({
                "cause": "Unknown - needs more evidence", "confidence": 0.2,
                "evidence": ["no dominant error class in the failure messages"],
                "fix": "Keep a trace and screenshot for every failure, then analyse again."})
    hypotheses.sort(key=lambda h: -h["confidence"])

    if verdict == "consistently failing":
        findings.append(finding("FLK-01", "high", "Not flaky - consistently failing",
                                f"{label} failed in all {len(fails)} finished runs.",
                                "This is a broken test or a real defect; triage it instead of quarantining it."))
    if verdict in ("flaky", "intermittent"):
        findings.append(finding("FLK-02", "high" if verdict == "flaky" else "medium", f"Test is {verdict}",
                                f"{len(fails)} failure(s) in {total} runs; {flips} outcome flip(s) (flip rate {round(flip_rate, 2)}).",
                                "Quarantine it from the release gate until the top hypothesis is fixed."))
    for h in hypotheses[:3]:
        findings.append(finding("FLK-03", "medium", "Likely cause: " + h["cause"],
                                "; ".join(h["evidence"]), h["fix"]))
    if retried_passes:
        findings.append(finding("FLK-04", "info", "Passed only after a retry",
                                f"{retried_passes} run(s) passed on a retry.",
                                "The plan runner already reports these as flaky; do not count them as clean passes."))

    summary = (f"{label}: {verdict}. {len(passes)} pass / {len(fails)} fail in {total} run(s), "
               f"flakiness {flakiness}/100.")
    return envelope("flaky", "Flaky Test Analysis", summary, verdict, findings,
                    artifacts={"stats": {"runs": total, "passed": len(passes), "failed": len(fails),
                                         "pass_rate": round(pass_rate, 3), "flip_rate": round(flip_rate, 3),
                                         "flakiness": flakiness},
                               "timing": timing, "top_errors": top_signatures[:MAX_ITEMS],
                               "hypotheses": hypotheses[:MAX_ITEMS], "browser_rates": browser_rates,
                               "failure_hours_utc": dict(sorted(fail_hours.items())),
                               "error_classes": dict(Counter(classify(e)["category"] for e in fail_errors[:50]))},
                    score=100 - flakiness)
