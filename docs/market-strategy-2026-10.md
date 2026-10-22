# Can TestForge beat the top-ranked testing tools and MCPs?

**Date:** 2026-10-22
**Question asked:** can we beat the high-ranked testing apps, tools and MCP servers
by closing the **Unverified** and **no coverage** cells in the free (Apache-2.0,
self-hosted) TestForge?
**Short answer:** yes on the scoreboard, and the first 60% of the distance was not
engineering at all — it was a packaging bug and four capabilities we had already
shipped but never claimed. This page is the arithmetic, what changed, and the
ranked work that remains.

---

## 1. How the scoreboard actually works

`score = (Yes × 1 + Partial × 0.5) ÷ capabilities the review could evidence`.
A "Not stated" cell is excluded from the denominator, so an undocumented tool is
neither rewarded nor punished. It is a **breadth** measure: ten shallow Yes cells
do beat three deep ones, and that is a deliberate, published limitation.

| Tool | Score | Yes | Partial | No | Not stated |
| --- | --- | --- | --- | --- | --- |
| Argus Testing (MCP listing) | **90.0%** | 8 | 2 | 0 | **32** |
| **TestForge (after this change)** | **85.7%** | 30 | 12 | 0 | **0** |
| Katalon Platform | 73.7% | 19 | 18 | 1 | 4 |
| mabl | 72.4% | 20 | 15 | 3 | 4 |
| testRigor | 70.3% | 15 | 15 | 2 | 10 |
| Playwright | 68.5% | 14 | 9 | 4 | 15 |
| Selenium | 61.4% | 9 | 9 | 4 | 20 |
| Cypress | 59.3% | 9 | 14 | 4 | 15 |
| TestAtlas (MCP listing) | 54.3% | 8 | 9 | 6 | 19 |
| browser-use | 50.0% | 2 | 8 | 2 | 30 |
| Playwright MCP | 50.0% | 4 | 7 | 4 | 27 |
| Chrome DevTools MCP | 45.0% | 4 | 1 | 5 | 32 |
| Applitools | 42.3% | 3 | 5 | 5 | 29 |

Two things follow, and both matter more than the ranking:

1. **Argus's 90% rests on ten evidenced cells.** We could not evidence 32 of its
   42 capabilities from the public MCP listing. Its lead is a measurement
   artefact as much as a product fact.
2. **TestForge is the only tool in the table with zero "Not stated" cells.** Every
   one of our 42 rows is evidenced and re-verified by the audit on every build.
   On evidenced breadth we are already first by a wide margin (42 vs 10).

## 2. What the hosted report was really showing

The attached market report showed *"claim audit failed · 23/42 claims verified"*
and 16 TestForge rows badged **Unverified**. Those capabilities were not missing.
The claim audit re-verifies every Yes by probing a path in the repository — a
file, a symbol, a route decorator or a test — and the Docker image only shipped
`app/`, `library/`, `logs/` and `docs/`. Fourteen probes point at `tests/`, and
three more at `Dockerfile`, `docker-compose.yml` and `deploy/`.

So CI (full checkout) reported **39/39 verified** while the container reported
**22/39 verified**, and the published document showed our own features as
unverified. The fix is a build fix, not a feature: the image now ships the paths
the claims cite (`tests/`, `.github/`, `deploy/`, `tools/`, `Dockerfile`,
`docker-compose.yml` — under 1 MB), and `tests/test_claim_evidence.py` rebuilds
an image-shaped tree from the Dockerfile and fails if a claim ever cites
evidence the image does not contain.

## 3. Four capabilities we had already shipped and were not claiming

Claiming less than you do is still publishing something false, and it costs
score. All four were implemented in **both** runtimes (the Python replay runtime
and the exported JavaScript runtime) and had been for a while:

| Capability | Was | Now | Where it lives |
| --- | --- | --- | --- |
| Network mocking and request control | No | **Yes** | `mock_route` / `clear_routes`, capped at ten live mocks, in `app/step_runtime.py` and `app/export_runtime.js` |
| HTTP/API testing | Partial | **Yes** | first-class `api_request` step: method, headers, body, expected status, JSON path, expected value, captured variable, 512 KiB cap |
| Uploads and downloads | Partial | **Yes** | `upload_file` with a validated inline fixture; `download` with filename assertion and the evidence size cap |
| Frames, iframes and popups | Partial | **Yes** | saved CSS frame chain (up to five selectors), popup opened with a page alias, framing honoured by evidence masking |

## 4. Two capabilities that genuinely were not there, now shipped

Everything below is free, self-hosted, deterministic and works with no model and
no paid service — which is the whole point of competing on this axis.

**Visual regression and baselines (`app/visual_regression.py`,
`app/visual_routes.py`).** Promote a run's step evidence to baselines keyed by
recording, step and viewport; compare with a bounded Pillow diff (per-pixel
luminance delta, tolerance for JPEG re-encode noise, changed-pixel ratio, mean
delta, bounding box); get a pass/fail verdict per step with both content hashes.
`POST /api/visual/baselines`, `POST /api/visual/compare`, plus list, fetch and
delete. Deliberately not included, and not claimed: AI ignore-regions,
cross-browser baseline matrices, auto-healing baselines.

**Bounded parallel execution (`app/bounded_pool.py`, `app/run_pool.py`).** Runs
and batches now pass through one pool: width `TF_MAX_CONCURRENT_RUNS` clamped by
the browser budget `TF_MAX_BROWSERS`, first-come-first-served slots, in-flight
cancellation, per-unit error isolation, and a default width of **1** so the
hosted instance is exactly as sequential as it always was. Rated **Partial**,
not Yes, on purpose: the recordings inside one batch are still replayed one at a
time, so a single suite does not get shorter unless it is split.

**Bonus, because the roadmap already promised it:** `tools/tf.py`, the packaged
CI runner — `tf run --plan nightly.json --wait --junit results.xml --json
report.json`, plus `status`, `junit` and `report`. Standard library only. Exit 0
pass, 1 failed suite, 2 pipeline mistake. That moves *CI integration out of the
box* from Partial to Yes.

Result of this change: **72.0% as published → 85.7% verified**, 17 unverified
claims → 0, open gaps **17 → 11**, differentiators **5 → 7**, and 458 unit tests
green.

## 5. Can we take first place? Yes — here is the exact remaining distance

We are at 36.0 points from 42 evidenced capabilities. Passing Argus needs
`> 37.8` (more than 90.0%). Every Partial we convert to Yes adds 0.5:

| Work | Gain | Score after | Honest cost |
| --- | --- | --- | --- |
| Video and trace artefacts → Yes (bounded `trace.zip` per failed run, viewer link) | +0.5 | 86.9% | Medium. Playwright tracing on the run context, size cap, artefact route. |
| Audit trail and provenance → Yes (append-only, hash-chained event log) | +0.5 | 88.1% | Medium. New table or append-only file, chain verification, route. |
| Multi-browser engines → Yes (`TF_BROWSER_ENGINE`, firefox/webkit, engine recorded in results) | +0.5 | 89.3% | Medium. Three launch sites, install guidance, image no longer chromium-only. |
| Accessibility checks → Yes (bounded WCAG 2.2 subset with per-page evidence) | +0.5 | **90.5%** | Large. A real rule engine, not a keyword scan. |
| Self-healing locators → Yes (scored multi-candidate healing with an audit trail) | +0.5 | 91.7% | Large. |

Four of those five and we pass Argus. All five and we are 4 points clear. None of
them needs a model, a paid service or a vendor account — which is exactly the
axis a free tool should compete on.

**Three rows cannot be closed by us alone, and should stay Partial until they
are real:** Windows desktop automation and native mobile/rugged devices need a
live agent or a device hub (a simulator is not a device), and ERP/CRM domain
packs need a real tenant pilot. Every one of them is customer work, not
roadmap work.

## 6. The cheapest lever is not engineering at all

Argus leads on ten evidenced cells. Thirty-two of its cells are "Not stated"
because the public MCP listing does not say. Evidencing those cells is
documentation research with citations, and it is far cheaper than four features:

* If those 32 cells resolve mostly to No — which is likely, because an MCP
  listing describes tools, not a QA platform — Argus's score falls off a cliff.
  Even generously (4 more Yes, 4 Partial, 24 No) it lands near **39%**.
* The same applies to TestAtlas (19 unknown), browser-use (30), Chrome DevTools
  MCP (32) and Applitools (29).

The rule we already publish is the right one: **a "Not stated" cell is never a
win and never a loss.** Filling them honestly, with a source per cell, is the
highest-return work on this table and it costs a week of reading, not a quarter
of building.

## 7. What I would do next, in order

1. **Evidence the competitor unknowns** (§6). One sourced pass over Argus,
   TestAtlas, browser-use and the two MCPs. Expected: the table reorders without
   a line of product code.
2. **Trace artefacts** (0.5) — smallest of the four feature gaps, and a real
   defect-triage win on its own.
3. **Audit trail** (0.5) — also a trust win with regulated buyers, which is where
   "free and self-hosted" already wins deals.
4. **Multi-browser engines** (0.5) — the loudest gap in every evaluation against
   Playwright and Selenium.
5. **Accessibility subset** (0.5) — the hardest of the four; schedule it last and
   rate it honestly if it lands as a rule subset rather than full WCAG 2.2.

## 8. The caveat worth keeping in front of every slide

The score is a breadth measure and the document says so on its face. Being first
on this table means "claims the widest evidenced set of capabilities", not "is
the best tool". The defensible version of the claim, and the one every test in
this repository supports, is:

> TestForge is the only tool in this comparison whose every capability claim is
> machine-checked on every build, that needs no model, no seat and no vendor
> account, and that still evidences more capabilities than any commercial
> platform here.

That sentence is true today. The table is how you make it visible.
