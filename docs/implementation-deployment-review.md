# TestForge implementation and deployment review

**Review date:** 2026-10-11 UTC
**Reviewed revision:** `a00f569a68723846c794b4cf82d4481e6ee244fc`
**Scope:** Compare the existing application with its own README, acceptance checklist, deployment configuration, and executable tests. This was the initial baseline before the reference screenshots were supplied. The subsequent [Playwright roadmap comparison](playwright-roadmap-comparison.md) maps the user's reference to current capabilities and defines additional implementation requirements R01–R15. Neither document treats planned features as completed or deployment configuration as proof of a live rollout.

**Decision:** Keep the existing recording/replay foundation. Resolve security and correctness gaps before expanding functionality or declaring the application production-ready. Review and approve the backlog below before implementation. No delivery-duration estimates are included.

> **Subsequent implementation:** The audit below describes the original revision.
> See [the implementation update](script-import-and-actions.md) for new imports,
> actions, optional access control, resource improvements and remaining gaps.

## 1. How to interpret the results

- **Implemented:** A working code path exists; this does not by itself establish production deployment or complete end-to-end validation.
- **Partial:** Some functionality exists, but the claim exceeds what is implemented or validated.
- **Not implemented / disconnected:** No integrated implementation was found in the reviewed application.
- **Deployment unverified:** There is insufficient live-environment evidence. This means neither “deployed” nor “not deployed.”

**Deployment is unverified for every feature below.** Docker/Render configuration and passing local tests are not proof of the version or functionality currently running in production.

## 2. Validation performed

| Check | Result | Evidence / qualification |
| --- | --- | --- |
| Clean runtime dependency installation | Succeeded | Python 3.11 virtual environment; `pip install -r requirements.txt`. |
| First harness run | 117 passed, 17 failed, 7 skipped | All 17 errors were caused by missing `httpx`, required by Starlette's TestClient but absent from runtime requirements; no separate test dependency file was found. [Initial report](../logs/20261011-092840/results.md). |
| Harness after installing `httpx==0.28.1` locally | **134 passed, 0 failed, 7 skipped** | 118 unit results and 16 passing library-scenario results. [Rerun report](../logs/20261011-092902/results.md), [machine-readable results](../logs/20261011-092902/results.json). Some library result rows represent additional requirements covered by one scenario, rather than separate executions. |
| Browser scenarios | **Not verified in this review** | Chromium is not installed. Skips: `LB-REC-01`, `LB-REC-02`, `LB-RUN-01`, `LB-RUN-02`, `LB-LIB-02`, `LB-LIB-03`, `LB-BAT-01`. |
| AI-agent import | Failed | `import app.agent` raises `ImportError: cannot import name 'Scenario' from 'app.db'`. |
| AI-rephrase behavior | Text cleanup only | Input `  click   submit  ` produces `Click submit.`; endpoint performs whitespace, capitalization, and punctuation changes, not model inference. |
| Unknown replay variable | Requirement mismatch | `interpolate('{{missing}}', {})` returns `'{{missing}}'`; the executor uses this interpolation without the recorder's missing-variable check. |
| GitHub release/deployment signals | No deployment records, Actions runs, or check runs returned | Read-only queries returned zero deployments, zero Actions runs, and zero check runs for the reviewed commit. Combined commit status returned `pending` with an empty statuses list; this is **not** evidence of an active deploy. |
| Live production application | Not inspected | No confirmed deployment URL/evidence was supplied. This workspace's outbound network is also restricted; production-host inspection may require user-supplied evidence or an accessible environment. |

The harness disables publishing and uses isolated test data. No production data was modified and no publish or deployment was triggered. Installing `httpx` changed only the local ignored virtual environment, not the application's dependency manifest.

## 3. Capability comparison

| Capability / current claim | Implementation status | Evidence and remaining work |
| --- | --- | --- |
| Projects and repository-backed library | Implemented | `app/main.py` project/library routes; `app/library_store.py` file storage and materialization. Existing API/library checks pass. |
| Project variables and recording associations | Partial | Variable CRUD, interpolation, capture and association tags exist. Secret protection is missing; missing replay variables do not fail closed. |
| Parent-group / sub-recording tree | Partial | `Recording.parent_id` and creation/storage support exist. `Library` in `app/static/index.html` lists recordings without a parent-tree UI or group-management workflow. |
| Remote browser recording | Implemented; browser validation outstanding | `app/recorder.py`, recording session/input/frame APIs, and `Recorder` UI. Keyboard controls, zoom and variable capture exist; this review did not validate actual browser interactions or mobile ergonomics. |
| Repeated-step compression and replay | Implemented | Recorder merge logic, compression endpoint, `repeat_count`, executor expansion and corresponding unit tests. |
| Idempotent SAVE and slow-export handling | Implemented | Stop endpoint and recorder shutdown logic; workflow/save regression tests pass. |
| Library step inspection/editing | Implemented | PATCH step and screenshot endpoints, editor UI, workflow tests. Browser rendering still needs validation. |
| Single-run replay, progress and history | Implemented | `app/executor.py` and run routes. A passed run means supported actions completed, not that business expectations were asserted. |
| Live run window toggle and viewer-aware frames | Implemented | `display_window`, preview viewer gate and bounded frame buffers; unit tests pass. Real websocket/proxy and browser operation remain to be checked. |
| Batch execution | Implemented | `app/batch_runner.py`: shared browser/session options, adaptive budgets, transient retries, screenshot policies, cancellation and reports. Browser batch scenario skipped here. |
| Queue hygiene | Implemented | `app/run_queue.py`: duplicate detection, orphan/stale cleanup, dry-run clearing and startup/sweeper integration. It cancels abandoned work rather than providing a durable distributed queue. |
| Screenshots, optional video and artifact export | Implemented | PNG screenshot processing, optional WebM, artifact serving and bounded ZIP streaming. Actual deployed capture/storage not verified. |
| Comparison / audit | Partial | `Comparison()` displays individual runs' step logs and screenshots. No baseline approval, image-diff engine, thresholds, or expected-versus-actual comparison workflow was found. |
| Explicit expected-result assertions | Not implemented in integrated replay | `replay_step()` supports navigation, clicks, typing/filling, keys and variable capture; no assertion action for text, URL, visibility or business outcomes was found. |
| “AI Step Rephrase” | Partial / misleading label | `/api/ai/rephrase` performs deterministic cleanup only. Rename accurately or implement and test a real AI service. |
| Autonomous AI-agent execution | Disconnected and broken | `app/agent.py` is not wired into `app/main.py`; import fails because `Scenario` is absent. `anthropic` and `mcp` are intentionally not installed. Setting an API key does not enable it. |
| Voice-to-text input | Partial | `AiInput` uses browser SpeechRecognition. No unsupported-browser guard or recognition-error handler is present; requires compatible client browser/permissions. |
| Playwright Python/JavaScript and Jenkins exports | Implemented generator | `app/export_formats.py` generates replay resources. Generator tests do not establish that every exported recording executes correctly on an external CI server. |
| BDD / Azure DevOps / Jira / TestComplete integration | Partial | Gherkin and CSV/XLSX templates exist. Gherkin requires step definitions; vendor templates need mapping. No native vendor recording format or direct vendor synchronization is implemented. |
| GitHub publishing and status | Implemented | Checkout/API/disabled modes, remote verification, dry-run plan, token-permission diagnosis and refusal/retry classification. Tests use fake transports; real deployment credentials/branch rules are unverified. |
| Scheduled regression execution / CI enforcement | Not implemented in reviewed repository | No `.github` workflow files or integrated scheduler found. `CICD_INTERVAL` exists as configuration but is not an integrated periodic test runner. Exported Jenkinsfiles and deployment guide examples are not active CI. |
| SQLite/PostgreSQL support and schema repair | Implemented; hosted database unverified | `app/config.py`, `app/db.py`. Local harness uses SQLite; no PostgreSQL/Neon integration test was performed here. |
| Resource guardrails | Implemented; instance headroom unverified | Browser, frame, event-buffer and artifact-size caps. Limits are process-local; multiple workers/replicas are not supported safely by this design. |
| Logs tab | Implemented | GitHub source selection plus bounded local fallback. Code/API coverage is not equivalent to successful browser fetching on the deployed network. |
| Azure tab | Implemented documentation UI only | Renders a deployment guide and stores checklist completion in browser localStorage. This is not an Azure provisioning service or evidence of an Azure deployment. |
| Authentication / authorization | Not implemented | No inbound authentication enforcement in app routes/websockets; configured `API_Key` is not consumed. Projects are not isolated by user/role. An external access gateway may exist, but was not verified. |
| Secret-safe variables and exports | Not implemented | `list_variables()` returns values and `_write_project_files()` writes them to `variables.json`, regardless of `is_secret`. Current tracked data contains two nonempty password-named entries; values were deliberately not reproduced. |
| Deployment packaging | Implemented configuration; live state unknown | Dockerfile, Compose, Render blueprint, Oracle scripts and Azure documentation exist. None establishes successful current production rollout. |

## 4. Acceptance-checklist traceability

Baseline: [Recording and library quality plan](qa-automation-acceptance.md). The checklist has 20 numbered criteria, despite older README wording describing ten.

| Criterion | Review conclusion |
| --- | --- |
| 1 — Variables resolve; missing names fail visibly | **Partial:** recorder rejects missing names; replay interpolation preserves unknown placeholders. Existing interpolation test explicitly expects that preservation. |
| 2 — Workspace, zoom, keyboard and capture | Code exists; actual browser and mobile interaction checks outstanding. |
| 3 — Reverse display, ordered persistence, repeat compression | Implemented; unit coverage passes. |
| 4 — SAVE without active session | Implemented; regression and non-browser library coverage pass. |
| 5 — Save/publish and replay/vendor exports | Implemented with documented template limitations; actual remote publication and vendor import not validated. |
| 6 — PNG steps, JPEG preview, optional WebM | Implemented; browser capture validation outstanding. |
| 7 — Inspect and edit saved steps | Implemented; workflow unit coverage passes. |
| 8 — Completion and queue/history | Implemented for successful execution of supported actions; does not imply expected-result assertions. |
| 9 — Per-step audit | Implemented; do not confuse with visual comparison. |
| 10 — Remote SHA verification and retries | Implemented with refusal/disabled exceptions; retry worker is process-local. |
| 11 — Safe slow-export shutdown | Regression tests pass; not a guarantee against every disk/database failure. |
| 12 — No unwatched frames, detached screenshot writes | Unit coverage passes; real browser/resource validation outstanding. |
| 13 — Working live-window option | Option is wired and unit-tested; real browser validation outstanding. |
| 14 — Explain missing Git checkout | Implemented; unit coverage passes. |
| 15 — Stay within memory ceiling | Caps implemented; actual production memory safety unverified. |
| 16 — GitHub logs with local fallback | Implemented; API/source-selection tests pass. |
| 17 — Truthful publish messages | Implemented; contract tests pass. |
| 18 — Publish without checkout | API publisher implemented and fake-transport tests pass; live write not performed. |
| 19 — Report/clear orphan, stale and duplicate runs | Implemented; unit and library API coverage pass. |
| 20 — Efficient batch execution | Implemented and unit-tested; real Chromium batch scenario skipped. |

## 5. Release blockers and implementation backlog

This is an **ordered proposal for approval**, not a claim that all items were previously requested. No application functionality was changed during this review.

### A. Security and data protection — before public exposure

1. **Rotate any real credentials already stored in the repository.** Two nonempty password-named entries remain in a tracked variables file, consistent with the existing deployment guide's warning. Their current validity is unknown. Treat real values as exposed; do not reproduce them in reports. Rotation and any approved history remediation need the credential/repository owner's action.
2. **Enforce authentication and authorization** across HTTP endpoints, websockets, artifacts and publishing. Verify any existing external access gateway instead of assuming one. Acceptance: unauthenticated requests cannot read variables, operate a browser, access artifacts or publish; access is scoped appropriately.
3. **Separate secrets from committed library content.** Store secret references rather than plaintext values; redact API/UI/log/export paths and inspect screenshots for sensitive-data handling. Do not rely solely on `is_secret`, since current credential-named entries are not flagged. Acceptance: sentinel-secret regression tests prove no secret appears in repository exports, responses or logs while replay can resolve it securely.
4. **Constrain browser network access.** Define permitted test destinations and protect loopback, private networks and cloud metadata as appropriate to the deployment. Acceptance: unauthorized destinations and redirect/subresource bypasses are blocked by application/network controls.

### B. Correctness and reproducible validation

5. **Fail replay on unresolved variables.** Keep recorder, single-run, batch and export semantics consistent. Add regression tests for missing/removed variables and ensure failure occurs before typing a literal placeholder.
6. **Declare test dependencies separately from runtime dependencies.** Add a reproducible developer/test install including `httpx`; keep runtime slim. Require `python -m tests.harness --require-browser` in a Chromium-capable validation environment so skipped browser scenarios cannot produce a release green light.
7. **Add explicit expected-result assertions.** Only if approved as product scope: text/URL/visibility/value checks, meaningful failure evidence and assertion-aware exports. Acceptance: a page with the wrong outcome fails even when every click succeeds.
8. **Make existing UI claims accurate and complete.** Clarify AI cleanup vs AI inference, audit vs comparison, documentation vs provisioning; complete parent-tree controls and speech error/unsupported-browser handling where required.

### C. Product enhancements — confirm against the intended comparison

9. **Visual comparison:** baseline storage/versioning, controlled viewport, region masking, thresholds, diff artifacts and review/approval workflow.
10. **Real AI assistance:** decide between rephrasing, test authoring and autonomous execution; repair or remove the disconnected module, define data/privacy boundaries and add integrated tests. A provider key alone is not implementation.
11. **Native integrations and scheduling:** prioritize actual vendor APIs, runnable BDD definitions and scheduled/CI-triggered suites only after agreeing which integrations are needed.
12. **Durable execution/publishing:** introduce external job coordination only if restart-resume or multi-worker support is required. Do not increase replica count against the current process-local design.

### D. Deployment confirmation — required before calling anything deployed

13. Obtain the production URL, deployed commit SHA/image digest, provider deployment result and sanitized configuration evidence. Compare the live revision to the approved release, not merely to a branch name.
14. Verify authentication at the edge and origin, `/api/health`, protected diagnostics, an actual Chromium launch, and a record → save → edit → replay → audit → batch workflow in an authorized test project.
15. Verify the actual database and storage configuration. `render.yaml` does not declare `DATABASE_URL` or a persistent disk; this does **not** rule out dashboard configuration. Compose persists `/app/artifacts` but does not mount `/app/library`; unpublished library changes need an explicit durability strategy. Its environment section also does not pass `DATABASE_URL` through automatically.
16. Test restart/redeploy survival for database, library, screenshots and reports; test restoration from backup. Confirm publish permissions and branch behavior through an authorized controlled test, not by writing to production during this audit.
17. Verify frontend delivery: the dashboard loads React and Tailwind from external CDNs. Check production access/CSP or bundle dependencies locally; API health alone cannot establish that the UI renders.

## 6. Information needed to finalize the comparison

- **Reference received:** the user's Playwright learning-roadmap screenshots are mapped in the [roadmap comparison](playwright-roadmap-comparison.md). Week estimates and interview-learning outcomes are excluded from product scope. Assertions are explicitly required by that reference; AI execution, visual regression and native vendor integrations remain separate proposals, not requirements inferred from the images.
- **Still needed:** the production environment/URL and sanitized deployment evidence. Do not send tokens, passwords or database connection secrets.
- Review the reference-backed R01–R15 requirements and implementation sequence before development; agree concrete domain business rules and deployment capacity where the screenshots do not specify them.

**Bottom line:** The application has a substantial implemented automation core, not just a mock-up. It is not yet justified to call all advertised capabilities complete, secure, or deployed. The next step is to review this comparison, agree the gaps to implement, then validate and deploy the approved changes with explicit evidence.
