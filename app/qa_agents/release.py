"""Release Summary: a concise, stakeholder-ready quality report with a go/no-go recommendation.

The headline metric is the state of each recording at its *latest* execution
inside the window, not the raw run count: a test that failed twice and then
passed is counted once, as passing. The decision policy is explicit so a
stakeholder can read why the recommendation was made:

* NO-GO            a critical/smoke test is failing, or an open blocker/critical defect exists,
                   or the latest pass rate is below 85%.
* CONDITIONAL GO   pass rate 85-95%, or product failures / flaky tests / open major defects.
* GO               pass rate of at least 95%, no product failures, no flaky tests, no critical defects.
* INSUFFICIENT     nothing executed in the window.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from .common import MAX_ITEMS, clip, envelope, finding, scrub
from .triage import classify

EXECUTED = {"passed", "failed", "error"}
DEFECT_SEVERITY = {"blocker": 4, "critical": 3, "major": 2, "minor": 1, "trivial": 0}


def _when(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min


def _defects(known) -> list[dict]:
    out = []
    for item in (known or [])[:50]:
        if isinstance(item, dict):
            title = scrub(str(item.get("title") or "")).strip()
            severity = str(item.get("severity") or "major").lower()
        else:
            title = scrub(str(item)).strip()
            severity = "major"
        if title:
            out.append({"title": clip(title, 200), "severity": severity if severity in DEFECT_SEVERITY else "major"})
    return out


def report(runs: list[dict], release: str = "", known_defects=None, open_risks=None,
           window: str = "", critical_names: list[str] | None = None) -> dict:
    release = scrub(str(release or "Release candidate"))[:120]
    runs = [r for r in (runs or [])[:5000] if isinstance(r, dict)]
    executed = [r for r in runs if str(r.get("status", "")).lower() in EXECUTED]
    cancelled = sum(1 for r in runs if str(r.get("status", "")).lower() == "cancelled")
    pending = sum(1 for r in runs if str(r.get("status", "")).lower() in ("queued", "running"))

    by_recording: dict[str, list[dict]] = defaultdict(list)
    for run in executed:
        key = str(run.get("recording_id") or run.get("recording_name") or "?")
        by_recording[key].append(run)

    latest = {}
    flaky = []
    failing = []
    product_failures = 0
    critical_set = {str(n).lower() for n in (critical_names or [])}
    for key, rows in by_recording.items():
        rows.sort(key=lambda r: _when(r.get("created_at")))
        last = rows[-1]
        latest[key] = last
        outcomes = {str(r.get("status")).lower() == "passed" for r in rows}
        if len(outcomes) == 2:
            flaky.append({"name": scrub(str(last.get("recording_name") or key))[:160],
                          "runs": len(rows), "passes": sum(1 for r in rows if r.get("status") == "passed")})
        if str(last.get("status")).lower() != "passed":
            error = scrub(str(last.get("error") or ""))
            category = classify(error)["category"] if error else "automation"
            is_critical = bool(last.get("critical")) or \
                any(tag in critical_set for tag in (str(t).lower() for t in (last.get("tags") or []))) or \
                str(last.get("recording_name") or "").lower() in critical_set
            if category == "product":
                product_failures += 1
            failing.append({"name": scrub(str(last.get("recording_name") or key))[:160],
                            "status": last.get("status"), "category": category, "critical": is_critical,
                            "error": clip(error, 240), "run_id": last.get("id")})

    total_latest = len(latest)
    passing_latest = sum(1 for r in latest.values() if str(r.get("status")).lower() == "passed")
    pass_rate = round(passing_latest / total_latest, 3) if total_latest else None
    raw_pass = sum(1 for r in executed if str(r.get("status")).lower() == "passed")
    raw_rate = round(raw_pass / len(executed), 3) if executed else None
    critical_failing = [f for f in failing if f["critical"]]
    defects = _defects(known_defects)
    open_blockers = [d for d in defects if DEFECT_SEVERITY.get(d["severity"], 0) >= 3]
    open_majors = [d for d in defects if d["severity"] == "major"]
    risks = [clip(scrub(str(r)), 200) for r in (open_risks or []) if str(r).strip()][:20]

    reasons: list[str] = []
    if total_latest == 0:
        decision = "INSUFFICIENT EVIDENCE"
        reasons.append("No test was executed inside the window.")
    else:
        if critical_failing:
            decision = "NO-GO"
            reasons.append(f"{len(critical_failing)} critical/smoke test(s) failing at their latest run.")
        if open_blockers:
            decision = "NO-GO"
            reasons.append(f"{len(open_blockers)} open blocker/critical defect(s).")
        if not reasons:
            if pass_rate < 0.85:
                decision = "NO-GO"
                reasons.append(f"Latest pass rate {round(pass_rate * 100)}% is below 85%.")
            elif pass_rate < 0.95 or product_failures or flaky or open_majors:
                decision = "CONDITIONAL GO"
                if pass_rate < 0.95:
                    reasons.append(f"Latest pass rate {round(pass_rate * 100)}% is below 95%.")
                if product_failures:
                    reasons.append(f"{product_failures} failure(s) classified as product defects.")
                if flaky:
                    reasons.append(f"{len(flaky)} flaky test(s) in the window.")
                if open_majors:
                    reasons.append(f"{len(open_majors)} open major defect(s).")
            else:
                decision = "GO"
                reasons.append(f"Latest pass rate {round(pass_rate * 100)}% with no product failures, flaky tests or critical defects.")

    by_category = Counter(f["category"] for f in failing)
    metrics = {"executed_runs": len(executed), "recordings": total_latest, "passing_latest": passing_latest,
               "failing_latest": len(failing), "pass_rate_latest": pass_rate, "pass_rate_all_runs": raw_rate,
               "cancelled": cancelled, "pending": pending, "flaky": len(flaky),
               "critical_failing": len(critical_failing), "product_failures": product_failures,
               "failure_categories": dict(by_category)}

    md = [f"# {release} - quality summary", ""]
    if window:
        md.append(f"Window: {scrub(window)[:120]}")
    md.append(f"**Recommendation: {decision}**")
    md.extend(f"- {reason}" for reason in reasons)
    md.append("")
    md.append("## Key numbers")
    md.append(f"- Recordings in scope: {total_latest} (latest run per recording)")
    if pass_rate is not None:
        md.append(f"- Passing at latest run: {passing_latest}/{total_latest} ({round(pass_rate * 100)}%)")
    md.append(f"- Executed runs in window: {len(executed)}; cancelled: {cancelled}; pending: {pending}")
    md.append(f"- Flaky (passed and failed in window): {len(flaky)}; critical failing: {len(critical_failing)}")
    if failing:
        md.append("")
        md.append("## Failing tests (latest run)")
        for item in sorted(failing, key=lambda f: (not f["critical"], f["category"]))[:15]:
            tag = " [CRITICAL]" if item["critical"] else ""
            md.append(f"- {item['name']}{tag} - {item['category']}: {clip(item['error'] or item['status'], 120)}")
    if flaky:
        md.append("")
        md.append("## Flaky tests")
        for item in flaky[:10]:
            md.append(f"- {item['name']} - passed {item['passes']} of {item['runs']} runs")
    if defects or risks:
        md.append("")
        md.append("## Known defects and risks")
        for item in defects[:10]:
            md.append(f"- [{item['severity'].upper()}] {item['title']}")
        for risk in risks[:10]:
            md.append(f"- Risk: {risk}")
    markdown = "\n".join(md) + "\n"

    findings = []
    if decision == "NO-GO":
        findings.append(finding("REL-01", "critical", "Release blocked", "; ".join(reasons),
                                "Resolve the blocking items and re-run the affected tests."))
    elif decision == "CONDITIONAL GO":
        findings.append(finding("REL-02", "high", "Conditional release", "; ".join(reasons),
                                "Accept the listed risks explicitly or fix them before the go decision."))
    elif decision == "INSUFFICIENT EVIDENCE":
        findings.append(finding("REL-03", "high", "No executed evidence", "Nothing ran in the window.",
                                "Run the regression selection or the full suite before deciding."))
    if flaky:
        findings.append(finding("REL-04", "medium", "Flaky tests in scope", f"{len(flaky)} test(s) flipped outcome.",
                                "Use the Flaky Test Analysis agent on each before trusting the pass rate."))
    for item in failing[:MAX_ITEMS]:
        if item["critical"]:
            findings.append(finding("REL-05", "high", "Critical test failing: " + item["name"][:80],
                                    item["error"] or item["status"], "Triage and fix before release."))

    summary = (f"{decision}: {passing_latest}/{total_latest} recordings passing at their latest run"
               + (f" ({round(pass_rate * 100)}%)" if pass_rate is not None else "") + ".")
    return envelope("release-summary", "Release Summary", summary, decision.lower(), findings,
                    artifacts={"decision": decision, "reasons": reasons, "metrics": metrics,
                               "failing": failing[:MAX_ITEMS], "flaky": flaky[:MAX_ITEMS],
                               "defects": defects[:50], "risks": risks, "markdown": markdown,
                               "release": release}, scored=False)
