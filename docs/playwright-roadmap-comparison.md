# Playwright roadmap → TestForge requirements comparison

**Date:** 2026-10-11
**Code baseline:** `a00f569a68723846c794b4cf82d4481e6ee244fc`
**Reference:** User-supplied “Playwright Learning Roadmap for Manual Testers (2026)” screenshots: `IMG-20261011-WA0012.jpg`, `IMG-20261011-WA0013.jpg`, `IMG-20261011-WA0014.jpg`.

> **Implementation update:** This file records the pre-development comparison. See
> [Script import and sequential automation additions](script-import-and-actions.md)
> for what has now been implemented, the sequential-only cost policy, and remaining scope.

## Review decision

Treat the roadmap as **additional capability requirements for our application**, not a learning schedule. All week/duration estimates are excluded. Keep TestForge's existing Python/FastAPI/Playwright foundation; JavaScript/TypeScript learning topics do not require rewriting the application.

This document is the comparison and implementation specification requested before development. **Planned requirements below have not been implemented by writing this document.** It supplements the [implementation and deployment review](implementation-deployment-review.md), which covers security issues and existing advertised features.

### Status rules

- **Implemented:** Relevant integrated code exists; not a production-deployment claim.
- **Partial:** A subset exists, or only low-level operations/templates exist.
- **Missing:** No integrated user-facing capability was found in the reviewed code.
- **Engineering / learning topic:** Apply it to design and quality; do not invent a new app screen for it.
- **Deployment:** **Unverified for all rows.** No live revision or authorized production smoke-test evidence is available. “Unverified” must not be reported as “not deployed.”

Existing validation from this review session: **134 passed, 0 failed, 7 browser scenarios skipped**, after installing the undeclared test dependency `httpx`. These results do not establish coverage of the newly supplied requirements. See the [test report](../logs/20261011-092902/results.md). No additional browser or production tests were run for this document.

## 1. Programming foundation

| Reference topic | What it means for TestForge | Current position / development requirement |
| --- | --- | --- |
| JavaScript / TypeScript basics | Maintainable dashboard and generated test code | **Partial:** dashboard and generated JavaScript replay exist; TypeScript/Playwright Test project export is missing. No backend language migration is needed. See R09. |
| Variables and data types | Typed step parameters, test inputs and captured values | **Partial:** project string variables and capture exist. Add validated action-specific schemas and typed datasets; protect secrets and reject unresolved placeholders. R01, R02, R08. |
| Operators | Implementation technique | **Engineering topic:** already used in application code; not a missing product feature. |
| Conditional statements | Implementation technique | **Engineering topic:** already used in code. This roadmap does not by itself request user-authored conditional workflow branches. |
| Loops | Implementation technique and repeated test steps | **Implemented for basic repetition:** repeat counts and executor expansion exist. Data-row iteration is a separate gap under R08; do not label arbitrary workflow scripting complete. |
| Functions | Reusable implementation | **Engineering topic:** modules/functions already exist; shared action behavior needs to remain consistent across execution and exports. R01, R09. |
| Arrays and objects | Step lists, locator/configuration objects | **Engineering topic:** already used. Add schema validation rather than a separate feature. R01. |
| ES6 features | Frontend/export coding practices | **Engineering topic:** existing JavaScript uses modern syntax; use tested conventions for generated JS/TS. R09. |
| Async / await | Browser operations and asynchronous application work | **Implemented:** recorder, executor and batch code use asynchronous Playwright; frontend also uses async operations. This does not imply parallel test execution. |

## 2. Playwright fundamentals

| Reference topic | Status in our app | Evidence / remaining requirement |
| --- | --- | --- |
| Installation and project setup | **Implemented; validation setup partial** | `requirements.txt`, `Dockerfile`, `start.sh`. TestClient requires an undeclared test dependency; provision browsers and declare test dependencies. R12. |
| Project structure | **Implemented** | Separate browser, recorder, executor, batch, storage, database, exports, UI and test modules. Extend this structure rather than create another runner. |
| Launch browsers | **Partial** | Recorder/single/batch/export paths launch **Chromium only**. Firefox and WebKit selection/install/validation missing. R13. |
| Browser contexts | **Implemented at basic level** | Fresh contexts and optional per-origin batch context reuse exist. User-configured fixtures, session profiles and device options remain missing. R07, R08, R13. |
| Page navigation | **Implemented** | Recorded `navigate` actions and replay/export support. New page/frame targeting must extend, not replace, this behavior. R05. |
| Locators | **Partial** | Recorder generates CSS selectors from IDs/test IDs/names/attributes/DOM paths. Replay uses a primary selector, often `.first`, and can fall back to coordinates. Add structured role/label/text/test-ID locators, scoping and ambiguity errors. R01. |
| Assertions | **Missing** | `replay_step()` and step-edit action allowlist have no assertions. Successful clicks alone can pass a run with an incorrect business result. R02. |
| Auto waits | **Partial** | Playwright locator actions already auto-wait; navigation uses `domcontentloaded`; action timeouts exist. Coordinate clicks lack locator actionability checks; no authored condition/response waits or retrying assertions. R02, R03, R06. |

## 3. Web automation

| Reference topic | Status in our app | Evidence / remaining requirement |
| --- | --- | --- |
| Text boxes | **Implemented for basic operations** | Recorder typing, replay fill/type and capture are supported. Value assertions and secure input handling still needed. R02, R07. |
| Buttons | **Implemented for basic clicks** | Click recording/replay/export supported; verify expected outcome with assertions. R02. |
| Checkboxes | **Partial** | Generic clicks may toggle them, but no explicit check/uncheck/set-state action or checked-state assertion. R03. |
| Radio buttons | **Partial** | Generic clicks may select them; no explicit state-aware selection/assertion. R03. |
| Drop-downs | **Partial** | Generic keyboard/click interactions may work, but no native `select_option` authoring/replay/export contract. R03. |
| Auto suggestions | **Partial** | Typing/clicks exist; no scoped, wait-aware suggestion selection by expected text. R03. |
| Alerts and pop-ups | **Missing explicit handling** | No saved dialog expectation/accept/dismiss/prompt-input workflow. Playwright's default dialog behavior is not test coverage. DOM overlays may be clicked like ordinary elements; browser popups need R04/R05. |
| Frames / iframes | **Missing explicit targeting** | Recorder inspects the top-page DOM; replay targets one page with no stored frame chain. R05. |
| Multiple tabs and windows | **Missing** | One active page is created and previewed; no saved page aliases, popup-event capture or switching workflow. R05. |
| File upload and download | **Missing for the application under test** | No user-authored upload/download actions, managed fixtures or download-result checks. TestForge's own resource downloads and artifact ZIP endpoint do not satisfy this. R04. |
| Web tables | **Partial** | Generic selectors can click table elements, but there are no row/cell extraction, lookup or table assertions. R03. |
| Mouse and keyboard actions | **Partial** | Clicks, text and key chords exist; no complete authored hover/double-click/right-click/drag-and-drop/scroll action set with replay/export parity. R03. |

Evidence: `app/recorder.py::handle_input`, `_ELEMENT_JS`, `_FOCUSED_JS`; `app/executor.py::replay_step`; `app/main.py::edit_recording_step`; `app/export_formats.py::write_exports`.

## 4. Framework development

| Reference topic | Status in our app | Evidence / remaining requirement |
| --- | --- | --- |
| Page Object Model (POM) | **Missing as user-facing reusable page objects / export structure** | Generated scripts are linear step interpreters. Add a locator/page-object repository and reusable flow support; generate an appropriate project structure. POM is a design pattern, not a reason to discard the recorder. R09. |
| Fixtures | **Partial at infrastructure level** | The development harness isolates test data and uses browser doubles; the runner creates contexts. No user-defined setup/teardown fixtures and lifecycle management for recorded suites. R08. |
| Test data management | **Partial** | Project variables and captured input values exist. Add CSV/JSON datasets, per-row cases, overrides, isolation and safe secret references. R07, R08. |
| Configuration files | **Partial** | Environment configuration and deployment files exist. Missing validated per-project execution profiles for target environment, browser/device, timeouts and authentication. R08, R13. |
| Utility classes | **Implemented foundation** | `browser.py`, `guardrails.py`, `images.py`, `errors.py` and other helpers. Share action/locator semantics rather than implement each new feature separately in each runner. R01. |
| Logging | **Implemented; extension required** | Step logs, audit UI, batch reports and harness logs exist. Extend records for assertions, API checks, datasets and retry attempts with redaction. R10. |
| Screenshots | **Implemented** | Recorded-step PNGs, execution capture policies and artifact serving exist. No new image engine is required merely to satisfy “screenshots.” |
| HTML reports | **Partial** | Interactive dashboard audit exists; harness outputs Markdown/JSON. No standalone downloadable HTML test report with summaries, evidence and assertion details. R10. |

## 5. Advanced Playwright

| Reference topic | Status in our app | Evidence / remaining requirement |
| --- | --- | --- |
| API testing | **Missing as a product capability** | Tests of TestForge's own HTTP endpoints exist, but users cannot author HTTP method/URL/header/body steps and response assertions for their target systems. R06. |
| Network interception | **Missing** | No authored request-route fulfill/abort/mock rules, response waits or scoped mock lifecycle. R06. |
| Authentication handling | **Partial for the application under test** | Users can record login steps; batch context sharing may reuse login state. Missing secure reusable storage-state profiles, expiration handling and isolated roles/accounts. R07. **This is separate from TestForge's own missing access control.** |
| Parallel execution | **Missing / deliberately constrained** | Batches replay sequentially; browser and batch budgets protect a small single-process instance. Add bounded isolated workers only after resource/coordination redesign. Increasing a limit alone is not a safe implementation. R14. |
| Tags and test filtering | **Partial** | Recording tag metadata exists; batch selection is by recording IDs, project or all. Variable association tags are not test-suite filters. Add editable normalized tags and deterministic include/exclude filtering. R11. |
| Retries | **Partial** | Batch runner has one transient-failure retry; not a general configurable test retry/flaky-result policy. Preserve current safety and expose attempts; do not retry business-assertion failures silently. R11. |
| Cross-browser testing | **Missing** | All integrated browser paths select Chromium; Docker installs Chromium only. R13. |
| Mobile emulation | **Missing** | A configurable small viewport and Android-friendly dashboard do not configure device user agent, touch, scale factor or mobile behavior. Add supported Playwright device profiles and report their settings. R13. This is not native Android/iOS app automation. |

## 6. CI/CD and version control

| Reference topic | Status in our app | Evidence / remaining requirement |
| --- | --- | --- |
| Git and GitHub | **Implemented** | Repository-backed library, checkout/API publishing, remote verification and publish-plan/access checks. Production permissions and actual publication remain unverified. |
| GitHub Actions | **Missing checked-in workflow** | No `.github` workflows found in the reviewed tree. Add validation, browser tests, report artifacts and controlled release gates. R12. |
| Jenkins | **Partial** | `write_exports()` generates a Jenkinsfile that runs Python replay. No evidence of a configured Jenkins job, report publication or release gate. Extend/test the template and verify an actual job when infrastructure is available. R12. |
| Docker basics | **Implemented packaging** | Dockerfile and Compose exist; this review did not build or deploy the container. Add reproducible build validation and deployment smoke tests, keeping storage and resource limitations explicit. R12. |

## 7. Real-world projects

The roadmap names domains, not complete test requirements. TestForge can target web URLs, but an industry label is not a validated domain suite. The bundled sample is a name field and Continue button; no curated regression packs for the following five domains were established in this review.

| Domain | Additional acceptance pack under R15 |
| --- | --- |
| E-commerce | Synthetic catalog/cart, quantity and totals, checkout in a payment sandbox, success/failure outcomes. |
| Banking | Synthetic accounts and transfers, balance/authorization checks, rejected transactions; no real funds. |
| Insurance | Quote/policy/claim flow, validation rules and attachment handling using synthetic data. |
| HRMS | Role-scoped employee/leave workflows, approvals and access-denial cases without real personal data. |
| Travel booking | Search/date/passenger selection, availability/price checks, booking/cancellation in a sandbox. |

Each pack needs an authorized test application, fixtures, assertions and repeatable cleanup. These are **proposed example flows**, not detailed business rules inferred from the screenshot.

## 8. Interview preparation and final outcomes

- **Playwright concepts, framework design, coding questions and interview confidence:** learning outcomes, not mandatory product modules. No interview-preparation dashboard is required.
- **Debugging scenarios:** translate into useful error messages, step evidence, downloadable reports and reproducible failure tests (R02/R10/R12).
- **API + UI automation:** a real product gap; R06 must support capturing an API response value and using it in a UI workflow, as well as asserting an API outcome after UI actions.
- **Best practices / production-ready framework:** security, isolated state, deterministic assertions, repeatable tests, resource limits and deployment proof are acceptance gates—not achieved simply by adding features.
- **Portfolio projects / real-world experience:** evidence from safe sample suites and exported projects (R09/R15), not a separate social/portfolio feature.

**Not additional requirements from these images:** autonomous AI, native mobile automation, and visual baseline/diff testing. Those may remain separate proposals from the earlier review, but “screenshots” is not evidence that visual regression was requested. Do not let them displace the capabilities explicitly listed here.

## 9. Implementation requirements and acceptance checks

All R01–R15 are **planned**, not completed. The order below is dependency-based, with no estimates. Existing capabilities should be extended rather than rebuilt.

### Prerequisite: fix the earlier security/correctness blockers

Before public deployment: enforce TestForge access control; rotate exposed real credentials; prevent plaintext secrets in repository/API/log/export paths; constrain test-browser/API network destinations; define persistent storage. Fix unresolved replay variables and declare test dependencies. See the [earlier review, section 5](implementation-deployment-review.md#5-release-blockers-and-implementation-backlog).

| ID | Requirement and implementation boundary | Minimum acceptance evidence |
| --- | --- | --- |
| **R01** | **Versioned step and locator contract.** Validate action-specific parameters and structured role/label/text/test-ID/CSS locators with page/frame scope. Reuse semantics across recorder, editor, single replay, batch and exports. Preserve old recordings through explicit compatibility behavior. | Old library recordings still load/replay. Invalid actions/parameters fail before browser work. Ambiguous locators fail visibly; strict mode never silently clicks coordinates or the first unrelated match. Exported locator behavior matches in-app execution. |
| **R02** | **Assertions and condition waits.** Text, value, URL, visibility, enabled/checked state and count checks; bounded auto-retry; expected/actual evidence. Provide a user authoring/editing path. | A deliberately wrong expected value makes single and batch runs fail. Delayed valid content passes within timeout; unmet conditions fail with useful evidence. Assertions survive save/edit/export. Missing variables fail before an action. |
| **R03** | **Rich controls and interaction actions.** Set checkbox/radio state, native selects, scoped suggestions, table cell/row capture and checks, hover/double/right click, drag/drop and explicit scroll. | Local fixtures exercise each action and negative cases. Re-running “check” does not toggle a checked box off. A missing option/row fails rather than choosing another. Recorder/editor/replay/export agree. |
| **R04** | **Dialogs and file transfers.** Register dialog/download expectations before triggering actions; accept/dismiss/prompt rules; managed upload fixtures and scoped downloads. | Accept/dismiss/prompt tests verify dialog type/message. Upload fixtures cannot escape authorized storage. Unexpected or absent downloads fail appropriately; file name/size/content checks and cleanup work. No arbitrary server-file upload. |
| **R05** | **Page and frame lifecycle.** Named pages, popup expectations, page switching/closure, nested frame targets and correct active-page preview. | A flow traverses a nested iframe, opens a popup, switches back and closes it. Targets and screenshots remain tied to the correct page/frame. Missing/closed targets fail clearly; ordinary single-page recordings remain compatible. |
| **R06** | **API + UI steps and network control.** HTTP requests, bounded response/status/header/JSON assertions, captured values; scoped fulfill/abort/mock rules and response waits. | API setup → captured value → UI assertion → API verification passes against a local fixture service. Wrong responses fail. Mock rules do not leak to another test; payload limits, secret redaction and network restrictions are tested. |
| **R07** | **Secure target-authentication profiles.** Store protected storage state/credential references, support per-project roles and expiry, and make batch reuse opt-in within a safe boundary. | A protected session can be reused without retyping credentials; expired state fails clearly. Different projects/accounts never share cookies—even when they use the same origin. Cookies/tokens do not enter library exports or reports. |
| **R08** | **Execution profiles, data-driven suites and fixtures.** Environment overrides, typed CSV/JSON rows, explicit precedence, setup/teardown, per-row results and isolated state. | One recording produces distinct results for multiple rows. Bad data identifies the row/field; teardown runs after failures. Overrides do not modify saved defaults. Failed setup cannot report a passing test. |
| **R09** | **Reusable pages/flows and framework exports.** Shared named locators, composable flows with cycle protection; JS/TS Playwright Test project export using POM/fixtures/config, while preserving Python replay. | Updating a named locator updates its references. Generated projects install and run from a clean directory with both passing/failing examples; exit status and results match the in-app flow. No generated project embeds secrets. |
| **R10** | **Portable reports and debugging evidence.** Downloadable HTML plus machine-readable results; step expected/actual, screenshots, browser/profile, dataset and attempt details. Bound and redact artifacts. | A report opens independently of TestForge, explains an intentional failure and links/embeds allowed artifacts. Untrusted target text is escaped; no script injection or secret values. CI can collect machine-readable results. |
| **R11** | **Tags, filtering and explicit retry policy.** Tag CRUD, include/exclude selection, empty-selection behavior, bounded retries and flaky-result classification. | Selected test IDs match the filter deterministically. Attempts are preserved. A fail-then-pass is visibly flaky rather than silently green; business failures are not retried unless explicitly configured. |
| **R12** | **Reproducible validation and CI delivery.** Separate test dependencies, Chromium-required harness workflow, export checks, Docker checks, report uploads, Jenkins improvements and authorized deployment verification. | Clean CI installation succeeds. Missing browsers or failing tests make CI fail. Artifacts are retained and secrets are absent. Release evidence identifies the tested commit/image; production smoke checks validate that same revision. |
| **R13** | **Browser/device matrix.** Chromium/Firefox/WebKit selection, browser-specific launch options and install strategy, supported mobile device profiles; persist chosen settings in results. | A representative assertion-based suite runs on each supported engine and device profile. Unsupported combinations are rejected. UA/touch/viewport settings are verified, not just screenshot dimensions. Existing Chromium settings remain the default. |
| **R14** | **Bounded parallel execution.** Isolated worker/job ownership, global resource control, fair cancellation, per-run storage and safe restart behavior. Keep sequential mode on small deployments. | Concurrent tests cannot share variables/cookies/artifact paths accidentally. Worker loss produces an explicit terminal/recoverable state. Global resource limits hold under contention; single-worker mode still passes. |
| **R15** | **Domain regression packs.** Approved synthetic/sandbox examples for the five reference domains, using fixtures, data and assertions. | Each pack has positive/negative cases, deterministic reset, no real transactions/personal data, passing evidence and a deliberate defect that proves its assertions fail. |

### Recommended implementation sequence

1. **Secure and stabilize the existing core:** earlier blockers, declared test dependencies, backward-compatible R01 foundation.
2. **Make outcomes testable:** R02, then common controls R03.
3. **Complete browser workflows:** R04/R05 and secure target sessions R07.
4. **Build maintainable suites:** R08/R09/R11; extend reports R10 as new step types are introduced.
5. **Add API/network testing:** R06 on the same step, variable, security and reporting contracts.
6. **Automate verification:** R12's test pipeline starts with the first implementation changes; expand its coverage throughout. Deployment gates apply to every release, not just the last one.
7. **Expand environments safely:** R13, then R14 only when deployment resources and coordination support it.
8. **Prove real workflows:** R15 packs as their prerequisites become available.

## 10. Definition of implemented and deployed

For each requirement, track these independently:

| Stage | Evidence required |
| --- | --- |
| Developed | Versioned schema/config changes, API/authoring UI, runner behavior, persistence and supported exports are wired together. A helper function alone is insufficient. |
| Validated | Positive, negative, regression and real-browser tests pass; no required scenario skipped. New assertions must demonstrate intentional failures. |
| Released | CI report plus commit SHA/image digest and provider rollout result. |
| Deployed and verified | Live revision matches release; authenticated authorized smoke checks pass; data survives restart where promised; storage/resource/security checks pass. |

Remaining deployment input: the production URL and sanitized revision/provider evidence. Do not share passwords or tokens. Network restrictions in this workspace may require supplying health/deployment results from the production environment rather than a direct inspection here.

**Bottom line:** TestForge already has the recording, replay, batching, evidence and publishing foundation. The largest roadmap gaps are outcome assertions, richer browser actions, reusable test/framework structures, API/network capabilities, browser/device coverage and reproducible CI. This document converts those gaps into implementable, testable requirements without claiming they have already been developed or deployed.
