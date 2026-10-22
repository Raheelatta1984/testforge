# Market position, claim audit and the capabilities added alongside it

**Reviewed:** 2026-10-22
**Matrix data:** [`app/market_data.json`](../app/market_data.json)
**Code:** [`app/market_intel.py`](../app/market_intel.py), [`app/market_report.py`](../app/market_report.py),
[`app/market_routes.py`](../app/market_routes.py)
**Screens:** **Market position** and **QA Intel** tabs in the dashboard.

This page explains how the comparison is built, what it is allowed to claim, what
was added to TestForge because a competing tool already did it, and how to keep
the whole thing honest over time.

---

## 1. The problem a comparison usually has

Most "us vs them" tables on a vendor's own site fail in one of three ways:

1. **The vendor's column is unverifiable.** Every row is a green tick, nobody
   can check it, and the first missing feature destroys trust in all the others.
2. **The competitor column is a straw man.** Rows are worded so only one tool can
   win ("Autonomously finds unknown bugs: Argus ✔ / Playwright MCP ✖ driver"),
   which is true and useless at the same time.
3. **It goes stale.** The product ships, the page does not change, and a year
   later it still claims a capability that was removed.

TestForge's matrix is built to survive all three.

## 2. How a cell is decided

Each of the 42 capabilities has a group, a plain-English reason it matters, a
TestForge claim and a rating for every compared tool. Ratings are one of:

| Rating | Meaning for a competitor | Meaning for TestForge |
| --- | --- | --- |
| **Yes** | Documented, named capability | A claim with passing evidence probes |
| **Partial** | A subset, a paid tier only, or needs user code | Same, and described in the cell note |
| **No** | Not a capability of that tool | Not implemented — never claimed |
| **Not stated** | The review could not evidence it either way | Not used: TestForge is always rated |
| **Unverified** | n/a | A claim whose probes no longer pass |

Three rules keep the table fair:

* **"Not stated" is not a loss.** It is excluded from the score, so an
  undocumented tool is neither rewarded for silence nor punished for it.
* **Breadth is not quality.** The score is `Yes (1) + Partial (0.5) ÷ known`.
  Ten shallow Yes cells do not beat three deep ones; the report says so in print.
* **Every competitor cell has a basis.** Either the supplied GitHub MCP listing
  screenshots, the vendor's own documentation, or an explicit `unknown`. The
  per-tool `basis` and the source list travel with the report.

## 3. The self-audit: TestForge's column cannot lie

A TestForge "Yes" is only allowed to exist next to a machine-checkable probe. A
probe is one of four things:

```json
{ "kind": "file",   "path": "app/explorer.py" }
{ "kind": "symbol", "path": "app/explorer.py", "contains": "def verify_findings" }
{ "kind": "route",  "method": "POST", "path": "/api/explorer/scan" }
{ "kind": "test",   "path": "tests/test_market.py", "contains": "verification" }
```

`audit_claims()` re-checks them and reports:

* **verified** — every probe resolves;
* **unverified** — at least one does not. The dashboard shows `! Unverified`, the
  gap list starts counting the capability as a gap, and the audit fails.

The audit is exposed as `GET /api/market/audit` (side-effect free) and
`POST /api/market/audit` (also writes a 24-hour receipt under
`ARTIFACTS/market/claim-audit.json`). The receipt carries a SHA-256 over its own
content, so two people can compare hashes instead of trusting a screenshot.

The zero-dependency CI gate is:

```bash
python -m tools.market_audit          # exit 0 only if every claim is verified
python -m tools.market_audit --json   # machine-readable
python -m tools.market_audit --write-report docs/market-position-report.html
```

Run it in CI and in any release check. **Do not quote the matrix without it:** a
matrix whose audit is failing is a historical document, not a current claim.

## 4. What the comparison found, and what was added

The first audited run compared TestForge with twelve tools, including the two
GitHub MCP listings supplied with this review (Argus Testing and TestAtlas), the
browser-driver MCPs (Playwright MCP, Chrome DevTools MCP), an agent framework
(browser-use), the open-source runners (Playwright, Cypress, Selenium) and the
commercial platforms (Katalon, mabl, testRigor, Applitools).

Rather than only write the table, the gaps that were feasible to close **in this
repository, without a model and without a paid service**, were implemented:

| Gap a competitor had | What was added | Where |
| --- | --- | --- |
| Autonomously finds unknown bugs (Argus) | Deterministic black-box explorer: HTTP status, console/page errors, failed requests, broken images, duplicate ids, unnamed controls, missing title, password exposure, mixed content, error leaks, security headers, slow pages, placeholder links | `app/explorer.py`, `POST /api/explorer/scan` |
| Independently verifies each finding (Argus) | Every finding is re-fetched and only marked verified when the same check reproduces; the report is sealed with a SHA-256 receipt | `app/explorer.py::verify_findings` |
| Reproduction receipt and evidence-rich defect report (Argus) | Per-finding expected/actual, evidence JSON, replay steps and a Markdown defect report | `app/explorer.py::defect_report` |
| Reuse-first step catalogue and `resolve_step` (TestAtlas) | Deterministic catalogue over the stored library with placeholder normalisation, phrase resolution (bound/ambiguous/unbound), allowed values and a ranked search | `app/step_catalog.py`, `GET /api/steps/catalog`, `GET /api/steps/search`, `POST /api/steps/resolve` |
| Blast radius / impact map (TestAtlas) | Library dependency graph (project → recording → URL/steps/locators) with IDF-weighted change mapping, reasons for every selection and a ready batch payload | `app/impact_map.py`, `GET /api/impact/map`, `POST /api/impact/analyse` |
| Machine-readable CI results | JUnit XML and a JSON summary for a run or a batch, with cancelled/queued runs reported as skipped rather than passing | `app/ci_results.py`, `GET /api/runs/{id}/junit.xml`, `GET /api/runs/batch/{id}/junit.xml`, `.../results.json` |
| MCP tools beyond test execution | Five extra read-only MCP tools: `market_position`, `market_gaps`, `resolve_step`, `step_search`, `impact_analysis` | `app/qa_agents/mcp_server.py` |

Gaps that were **not** closed are published, not hidden. The largest are:

* **Parallel execution** — TestForge is sequential on purpose. Bounded workers are
  roadmap R14 in the [Playwright gap review](playwright-roadmap-comparison.md).
* **Visual regression** — there is no baseline store or pixel diff. Applitools and
  mabl lead; a deterministic Pillow diff is the planned answer.
* **Self-healing locators** — structured, stable locators exist; scored
  multi-candidate healing with an audit trail does not.
* **Full accessibility conformance** — signal-level checks exist (unnamed
  controls, duplicate ids, missing title); a WCAG rule engine does not.
* **Network mocking and first-class HTTP steps** — roadmap R06.
* **Multi-browser engines** — Chromium only today; roadmap R13.
* **RBAC/SSO** — the single-operator token gate is honest about being exactly that.

The `/api/market/gaps` endpoint and the **Market position** tab list all of these
with the leading tools, so the claim "we beat the market" is always scoped to a
named capability.

## 5. Reading the screen

The **Market position** tab shows:

* an audit badge and a **RUN CLAIM AUDIT** button that returns the receipt hash;
* five cards: coverage, Yes, Partial, differentiators, open gaps;
* the matrix, filtered by group, search and "gaps only", with tooltips carrying
  each competitor's basis and a click-to-expand evidence panel per row;
* the gap list with the leading tools and the disposition;
* where TestForge leads (and by how much: "no compared tool claims it" versus
  "one competitor claims it too");
* a trust/operations table: licence, price, deployment, model dependency, basis.

The **QA Intel** tab puts the three new tools behind one panel: phrase resolution,
impact analysis and the explorer.

## 6. Keeping it current

1. Change a product or spot a stale competitor claim?
   Edit `app/market_data.json` — one object per tool, one per capability.
2. Add a capability? Give it a group, a reason, a TestForge claim with **evidence
   probes**, a rating for every competitor (or an explicit `unknown`) and a
   `roadmap` line. `market_intel.validate()` fails the test suite if any tool is
   missing.
3. Ship a feature you claimed? Add the probe first, then raise the rating. The
   audit is the gate, not the wording.
4. Update `last_reviewed` and the per-tool `basis` when you re-read a vendor's
   docs. The date is printed on the report and in the API payload.

## 7. Limits (read before presenting)

* Ratings come from documentation and the supplied listings, **not** from a
  hands-on bake-off against the same application. Nobody ran the other tools here.
* A "Yes" means the capability exists and is documented. It says nothing about
  speed, stability or usability.
* Commercial pricing is indicative; packaging is per contract.
* TestForge evidence proves **wiring in this repository**, not a production
  deployment. Deployment claims need deployment evidence.
* Vendor names are used for identification only. No affiliation, partnership,
  certification or endorsement is claimed.

## 8. API reference

| Method and path | Purpose |
| --- | --- |
| `GET /api/market/matrix` | Full comparison: tools, groups, capabilities, scores, gaps, evidence, audit |
| `GET /api/market/competitors` | Tool metadata and the scoreboard |
| `GET /api/market/capabilities?group=` | Capabilities with TestForge evidence |
| `GET /api/market/gaps` | Where a compared tool leads, plus differentiators |
| `GET /api/market/differentiators` | Capabilities no compared tool claims |
| `GET /api/market/summary` | Small payload for an AI client |
| `GET` / `POST /api/market/audit` | Re-verify claims; POST also persists a receipt |
| `GET /api/market/evidence?capability=` | The probes behind one claim |
| `GET /api/market/report.html?download=1` | Self-contained, printable report (strict CSP, no scripts) |
| `GET /api/market/report.md?download=1` | Markdown for a README or PR |
| `GET /api/steps/catalog?project_id=&action=&limit=` | Step catalogue with placeholders and examples |
| `GET /api/steps/search?q=&project_id=&limit=` | Ranked, deterministic search |
| `POST /api/steps/resolve` | `{ "phrase": "…" }` → bound / ambiguous / unbound |
| `GET /api/steps/summary` | Counts and most-reused steps |
| `GET /api/impact/map` | Dependency graph over the library |
| `POST /api/impact/analyse` | `{ changed_files, diff, routes, labels, project_id, max_tests }` |
| `GET /api/impact/summary` | Graph counts |
| `GET /api/explorer/capabilities` | Checks, safety rules and modes |
| `POST /api/explorer/scan` | `{ url, max_pages, max_depth, mode, build_label, include_report }` |
| `GET /api/runs/{id}/junit.xml` | JUnit XML for one run |
| `GET /api/runs/batch/{id}/junit.xml` | JUnit XML for a batch |
| `GET /api/runs/{id}/results.json` | CI summary with an explicit exit code |
| `GET /api/runs/batch/{id}/results.json` | CI summary for a batch |

Tests: `tests/test_market.py` (31 cases covering the matrix, claim audit, reports, step catalogue, impact map, explorer, JUnit output, MCP tools, the CLI gate, the dashboard wiring and the no-downgrade checks) and `tests/test_claim_evidence.py` (the evidence the deployed image must ship, plus the capabilities that were shipped but under-claimed).

For where this leaves us against the top of the table, what it would take to lead, and what must never be claimed, see [`docs/market-strategy-2026-10.md`](market-strategy-2026-10.md).
