# Script import and sequential automation additions

## Delivery status and cost boundary

This change implements a working import → review → isolated browser test → save/publish → replay/report flow, an extended shared action runtime, authoring controls, and lower-overhead defaults. **It does not complete every item in the roadmap and has not been deployed to production by this session.** Remaining scope is listed below.

No paid AI/API integration, additional server, parallel worker, scheduled test job, automatic CI workflow or hosting-plan upgrade has been added. The Docker/Render single-worker and one-browser limits remain unchanged. Playwright, React and the parsers run locally. **Zero charges and zero additional CPU cannot be guaranteed:** target pages, optional extra tabs, files, traffic, storage, provider quotas and externally triggered builds still consume resources. No claim of production latency or memory headroom is made from local tests.

The Render blueprint now ignores `library/**` and `logs/**`-only changes for builds, so saving test data need not rebuild the entire service. **That filter is configuration in this branch, not proof it is active in Render.** Apply/verify it in the deployment before relying on it. Normal code changes can still trigger the existing deployment mechanism.

## Use the new workflow

1. Select a project and open **Library → UPLOAD TEST SCRIPTS**.
2. Choose a file, or a restricted Jenkinsfile plus its referenced test file.
3. Click **VALIDATE & PREVIEW**. This only parses/validates; it does not execute a browser, run uploaded source, install dependencies, save a recording, or publish anything.
4. Resolve every diagnostic. Unsupported statements are errors, not silently skipped steps. Review the normalized actions, name and tags.
5. Click **TEST BEFORE PUBLISHING**. A private draft runs sequentially outside both the recording database and published library. Wait for a passing result; failures, cancellation, expired tests and flaky results do not enable publication.
   The panel names the state it is in: while nothing has been tested, while the test runs, after a failure (with the failing case's error) and after a receipt expires, **SAVE TO LIBRARY & PUBLISH** stays locked and the sentence underneath says which of those it is and what to do. The receipt is good for one hour, and the remaining minutes are shown next to the result.
   Leaving the tab no longer costs you the test. The receipt is kept for the project and re-read when you come back, and after a full page reload the panel recovers a still-valid passing test from `GET /api/imports/tests` — with the steps it tested — so you can save without running the browser again. A single dropped status poll does not discard it either.
6. Click **SAVE TO LIBRARY & PUBLISH**. The canonical recording and generated resource files use the existing GitHub publisher and its configured branch. The response distinguishes published, refused, retrying and local-only. Publication is not claimed unless verified.
   **Optional override.** When the test cannot pass — a target environment that is down, steps you will finish later — **SAVE WITHOUT A PASSING TEST (OPTIONAL, MARKED UNVERIFIED)** publishes anyway. It needs a written reason and an explicit acknowledgement, and the recording is tagged `unverified`: the Library card carries an `UNVERIFIED` badge, `unverified: true` is returned by every listing and by `/api/plans/catalog`, and the tag filter has an "unverified only" switch. The default path is unchanged — no passing test, no publication — and the uploaded source is still never executed.
7. Click **RUN SAVED STEPS**. TestForge executes the canonical actions sequentially, not the original source program.
8. Open the Runs tab's **HTML REPORT** or **JSON REPORT**. The Library's **DOWNLOAD RUNNABLE PROJECT ZIP** includes the companion files needed by exported Python/JavaScript/TypeScript runners.

The dashboard now tests before publication. **Draft isolation protects the library, not the target application:** browser/API actions can still change the target system. Test only authorized, non-production fixtures/accounts. A receipt binds the project, canonical steps, dataset, profiles, project variables, secret references and auth state to the successful test. Editing execution inputs requires another test. Receipts expire after one hour or a server restart; saving the same receipt twice does not create duplicate recordings. Drafts never enter the library, so an unrelated publish cannot accidentally publish them.

`POST /api/imports/save` also enforces the passing `draft_id`; callers cannot bypass
the import gate by omitting the receipt or setting `require_test: false`. The only way
past it is the override above, which has to name itself (`allow_untested: true`) and
carry an `override_reason`; the recording it writes is permanently marked instead of
being indistinguishable from a tested one. A refusal says which rule it hit — a
non-passing test, inputs edited after the pass, a receipt from another project, or an
expired receipt — rather than one sentence for all four.
Existing non-import recorder/editor publication paths are separate: this is not a claim
that every legacy recording/editor change has passed a full replay.

`GET /api/imports/tests?project_id=…` lists the receipts this instance still holds
(status, name, step count, case summary and seconds of life left); adding `?plan=true`
to a single receipt returns the plan it tested, which is what lets a reloaded dashboard
rebuild a save payload that still fingerprints identically. Resolved secrets are never
in a receipt — only `{"env": "TF_SECRET_*"}` references — so this exposes nothing the
uploader did not already write.

Do not upload literal passwords, personal data or production files: canonical step inputs and upload fixtures are repository content. Use the secret references below; previously committed secrets still need rotation.

## Format compatibility — explicit, not “any code”

| Input | Supported contract |
| --- | --- |
| CSV / TSV | One recording, one action per row. Required `action`; optional `selector`, `value`/`input`, `label`/`description`, `repeat_count`. The `action` cell may name a canonical verb **or** carry a recorded step label (`Click add to cart`, `Type: {{password}}`, `Click 528,562`) — see *Re-importing your own recordings*. Selected vendor metadata columns (`Screenshot`, `Evidence`, `Actual result`, `Attachment`, status/estimate columns) are accepted as non-steps. Multiple different `test_case`/`summary` values are rejected. |
| Excel `.xlsx` / `.xlsm` | The same step table in one worksheet. Literal cells only. Formulas, macros, external links and multi-sheet workbooks are rejected. |
| Legacy Excel `.xls` | The same step table read with `xlrd` (`.xlsm` also accepted). One worksheet, ≤12 columns, literal cells only. |
| JSON | A canonical step array, `{ "name": "…", "steps": [...] }`, or `{ "name": "…", "execution_plan": {...} }`. Array order is execution order. Use this format for a lossless re-import of supported canonical steps. |
| Playwright Python `.py` | Static straight-line `page` actions/expect assertions, simple literal constants/locator aliases, and one undecorated `test_*` function taking a `page` fixture. Sync/async action syntax is supported. Arbitrary imports, helpers, loops, branches, browser-bootstrap code and evaluated expressions are rejected. |
| Playwright JavaScript / TypeScript | One standard `test('name', async ({ page }) => { ... })` wrapper or straight-line actions; one supported action/assertion per line. Literal role `name` or `exact` options are supported. Arbitrary JS/TS, POM imports, hooks and general TypeScript syntax are not executed or inferred. |
| BDD / Cucumber `.feature` / `.bdd` | One scenario using the concrete phrases below. Custom step-definition code, outlines, data tables and arbitrary business-language phrases require explicit conversion. A feature file alone cannot define unknown business actions. |
| Jenkinsfile / `.jenkins` / `.groovy` bundle | A restricted declarative wrapper using `pipeline`, `agent any`, `stages`, `stage`, `steps` and exactly one literal `sh` test command. Supply its referenced Python/JS/TS file. Accepted commands: `python`, `python3`, `pytest`, `node`, or `npx playwright test` followed by a simple filename. No installer, checkout, credential block, shell pipeline or post hook runs. |
| Plain text / log / Markdown / RTF (`.txt`, `.md`, `.log`, `.rst`, `.rtf`) | Numbered, bulleted or `Gherkin`-style step lines in English: open/enter/type/click/check/hover/select/press, wait-for-element, capture text, expected results (`Expected result:` pairs mark the previous step's check). A `Test case:`/`Scenario:` line names the recording; preconditions become notes. Lines that are not steps are errors unless they are comments, notes or headings. |
| Any delimited or pipe table (`.csv`, `.tsv`, `.psv`, `.tab`, clipboard tables) | Aliased headers (`Step`/`Action`/`Test Step`/`Input`/`Expected Result`/`Locator`/`Object`/`Priority`…). The delimiter and header row are detected; metadata columns become non-blocking notes. More than one distinct `test_case` in a file is refused rather than merged. |
| Word `.docx` | The document's step tables (same header rules) or its paragraph step lines. A macro-enabled file (`vbaProject.bin`) is rejected; `.doc` must be re-saved as `.docx`. |
| PDF | The text layer of up to 30 pages, parsed as a step table or step lines. Scanned/image-only PDFs are refused with that reason; nothing is OCR'd. |
| Selenium IDE `.side` | `open/click/type/select/check/mouseOver/doubleClick/waitForElementVisible/assert*` commands. Fixed `pause` is refused (this project runs without fixed sleeps), `echo`/`comment`/`setWindowSize` are notes, unknown commands are errors. |
| Postman collection (v2 JSON) | Each request becomes an `api_request` step; `pm.response.to.have.status(...)`, `pm.response.to.have.jsonBody`/`pm.expect(pm.response.json()...)` become status/json assertions. Environment files and pre-request scripts are not read. |
| Robot Framework `.robot` / `.resource` | `*** Test Cases ***` keyword subset (Open Browser, Go To, Input Text, Click (Button/Element), Select From List, Checkbox, Press Key, Wait Until Element Is Visible, Page Should/Element Text Should Be, Mouse Over, Get Text). `*** Variables ***` become variable suggestions; arbitrary Python keywords are errors. |
| Cypress spec `.js`/`.ts` | `cy.visit`, `cy.get`/`cy.contains`, `.type`/`.click`/`.check`/`.select`/`.trigger`, `cy.url()`, `cy.title()`, `should('include'|'be.visible'|'have.text'|'have.value')`. `cy.intercept`, `cy.request`, aliases and hooks are errors. |
| Selenium Java / C# | A straight-line subset: `driver.get`, `findElement(By.*).click/sendKeys`, `Select`, `clear`, `getText`, `isDisplayed`, `Assert.assert*`/`NUnit`/`MSTest` assertions. `Thread.sleep` is refused, loops/branches/Page Objects are errors. |
| XML `.xml` / YAML `.yaml` | Canonical `<step action="…" selector="…" value="…"/>` elements or a `steps:`/`test:` list (or a full canonical YAML `execution_plan`), JUnit/TestNG `<testcase name="…">` case names, and step-text English lines. `safe_load` only: no Python object tags are constructed. |
| HAR capture `.har` | Browser network capture converted to one `navigate` step plus **deduplicated** `api_request` steps for the captured calls (≤100). `Cookie`/`Authorization` headers are stripped, not stored. |
| HTML page / legacy Selenium HTML suite `.html` | A `<table>` with Selenium command columns is read as a `.side` suite; otherwise the page's text is read as step lines. Headings become notes; scripts/styles are never executed. |
| Any other extension | The content is sniffed (JSON, XML, YAML, HTML, PDF/Word/Excel signatures, step text). Nothing is guessed about binary payloads that match no known shape. |
| Other formats / unsupported code | Rejected with diagnostics. There is no safe universal converter for arbitrary executable programs. No AI service guesses what omitted code was supposed to do. |

The generated Python/JS runners are executable artifacts, not input-language promises: **re-import their `steps.json` (ordinary recordings) or `execution-plan-source.json` (advanced plans)**, rather than their generic interpreter code. Vendor CSV/XLSX files remain mapping templates; they are not proprietary native test projects.

### Onboarding templates in every format

`GET /api/imports/templates` lists one working example per supported format, each of which previews with **zero diagnostics of severity `error`**: simple steps (`.csv`/`.xlsx`), plain-English Excel, Azure DevOps column names (`.xlsx`/`.csv`), a re-importable recorded-label table (`.xlsx`), Playwright Python and TypeScript, BDD `.feature`, a Jenkinsfile bundle, Robot Framework, Selenium IDE, Cypress, canonical JSON/YAML, Markdown step lines, and a Postman collection. `GET /api/imports/templates.zip` downloads the whole pack with its `README.md`; `GET /api/imports/templates/{id}` returns one file (a bundle returns a ZIP). The dashboard's **START FROM A TEMPLATE (ANY FORMAT)** picker previews a chosen template through the same parser as an upload, so the steps can be inspected before being replaced. Every template is synthetic (`example.test`, `{{variables}}`), never a real target.

### Re-importing your own recordings and vendor exports

TestForge's own `azure-devops.xlsx`/`.csv`, `jira.csv`/`.xlsx` and `testcomplete.csv`/`.xlsx` write the recorded *label* into the action/description cell, because that is the part a person reads. Those tables are re-importable and, for `azure-devops.*` and `testcomplete.*`, reproduce the recording exactly:

- The structured `Selector` (JSON, or a plain CSS/text cell) and `Input`/`value` columns win over whatever the label implies, so a recorded locator is not degraded into a text guess.
- A trailing `×N` repeat marker in a label is restored as the canonical `repeat_count` (`Click add to cart ×3`).
- `Click 528,562` (with or without `at`/parentheses/`x`) is a click on the recorded screen position. The preview adds a note suggesting a locator when the element has one; the step is saved as-is, because that is what the recorder captured.
- `Type: {{password}}` / `Type: granny apple` with no field name binds to the element the previous click targeted (the recorder focuses the element and then types), with a note naming that element. With no such element the value is typed into whatever has focus, exactly as the recorder replays it.
- `Press Enter`, `Open <url>`, `Select: <value>`, `Save input into <name>`, `Capture <field> as <name>`, `Verify: …` and the other recorder labels are recognised in either the canonical template or a label-only column.
- A label that quotes a whole page region (`Click search for products e.g. bread log in / sign up cart $0.00 0`) uses the leading phrase as its text locator and notes the shortening, instead of storing a whole paragraph as a locator.
- `jira.csv` has no locator of its own on older exports; the locator is rebuilt from the description column, and the step keeps the description as its label.

An unsupported verb or an unrecognised label is still an error: nothing is silently treated as tested, and no AI service guesses what a step meant.

### CSV example

```csv
action,selector,value,label,repeat_count
navigate,,{{base_url}},Open application,1
fill,#name,Ada,Enter name,1
click,#go,,Continue,1
assert_text,#out,"Hello, Ada",Verify result,1
```

Define `base_url` in the selected project, for example the HTTP(S) URL of the built-in `/demo.html`. Missing variables now fail replay before typing a literal `{{placeholder}}`.

### Supported Python example

```python
from playwright.sync_api import expect

def test_greeting(page):
    page.goto("https://your-authorized-test-app.example")
    page.get_by_label("Name").fill("Ada")
    page.get_by_role("button", name="Continue").click()
    expect(page.locator("#out")).to_have_text("Hello, Ada")
```

### Supported BDD phrases

```gherkin
Feature: Greeting
Scenario: Say hello
  Given I open "https://your-authorized-test-app.example"
  When I fill "#name" with "Ada"
  And I click "#go"
  Then "#out" should equal "Hello, Ada"
  And "#go" should be visible
```

Other supported phrases include `I navigate to`, `I check/uncheck/hover`, `I type into "selector" with "value"`, `I select "value" from "selector"`, `"selector" should contain/have value "value"`, checked/hidden/enabled/disabled states, and `the URL should be "url"`.

### Limits

- 1 MiB per script/bundle; up to eight files per bundle, one test recording.
- 500 authored steps, repeats 1–100, at most 2,000 expanded actions for imported recordings.
- XLSX expanded archive ≤8 MiB, ≤150 ZIP entries, one worksheet, ≤12 columns.
- One import parser/saver at a time; another request receives 409 instead of starting parallel work.
- File fixtures ≤512 KiB; upload filenames must be simple names, never host filesystem paths.
- Download acceptance and API JSON processing are capped at 512 KiB. **These checks do not guarantee the browser/network never receives a larger response before rejection.** Restrict targets and monitor bandwidth/storage at the infrastructure layer.
- Up to four pages in an explicitly authored multi-page flow and ten mock routes per recording. Additional pages are closed and mocks removed between batch recordings.
- Action/expect timeouts are bounded to 30 seconds maximum. This is a ceiling, not a sleep: successful actions continue immediately.

## Input values become variables

An imported script and a recorded session both end in the same onboarding step: the
literal values that were typed are listed, grouped by value, named after the field
they went into (`Ada.Lovelace` in `Username` → `{{username}}`), marked secret when
the field or value looks like a password/token (`Auth` category, encrypted at rest
when the keyring is configured) and shown with the exact before → after change.

- **Uploads:** after **VALIDATE & PREVIEW**, use **REVIEW INPUT VALUES → VARIABLES**.
  The review calls `POST /api/imports/variable-plan` and then
  `POST /api/imports/variables`, which creates the confirmed variables in the
  project and hands back the rewritten steps; the preview now holds
  `{{variables}}`, so the draft test and the saved recording carry parameters, not
  one person's data.
- **Recordings:** after **SAVE**, `POST /api/recordings/{id}/variable-plan` proposes
  the same list from the stored steps and `POST /api/recordings/{id}/variables`
  rewrites them and creates the variables. **KEEP LITERALS** skips the whole step;
  applying twice is a no-op, so a retry cannot duplicate a variable.
- Query strings are parameterised per key (`…?storeNo=426` → `…?storeNo={{storeno}}`),
  one literal becomes one variable no matter how many steps use it, expectations that
  embed a typed value follow that variable (`Welcome {{username}}`), and values that
  already contain a `{{placeholder}}` are never rewritten.

## Execution-plan tab

**🧭 plan** sits directly next to the AI-recording tab. It loads
`GET /api/plans/catalog` once — recordings, their saved input defaults and every
project's variables in a single cached read — so linking or selecting a recording
renders its variables and parameters without another request (the tab shows the
catalog's age). From there: link recordings in order, move them up/down, review each
one's inputs, optionally **SAVE AS RECORDING DEFAULTS**, and queue the ordered plan
with `POST /api/runs/batch` (`share_session: true`). High-level scenarios with
datasets, flows and profiles remain in **Library → Execution plans**.

## Shared action model and authoring

Open **Assertions & advanced actions** in the live recorder or Library editor. The live recorder executes the authored action and records it after success; the Library editor can append steps without executing them. Existing basic tap/text/key recording remains available.

New authored/imported steps carry `selector.schema_version = 1` and `strict = true`. Strict locators do not silently use `.first` or fall back to coordinates. Existing legacy recordings retain their original click-fallback behavior; they are not silently rewritten.

| Actions | Purpose / options |
| --- | --- |
| `navigate`, `click`, `fill`, `type`, `press`, `save_variable` | Existing basic workflow plus strict locators. `fill` replaces text efficiently; `type` emits sequential key events with zero artificial inter-character delay. |
| `check`, `uncheck`, `select_option` | State-aware checkbox/radio handling and native dropdown selection. For selection by visible label use `selector.by = "label"`. |
| `hover`, `dblclick`, `right_click`, `drag_to`, `scroll` | Extended interactions. `drag_to` takes a nested `selector.target` locator; `scroll` brings the element into view and scrolls it by integer `value` pixels. |
| `assert_text`, `assert_contains`, `assert_value`, `assert_count` | Auto-retrying outcome checks; failure marks the run failed. No blanket retry of assertion failures in batch execution. |
| `assert_visible`, `assert_hidden`, `assert_enabled`, `assert_disabled`, `assert_checked`, `assert_unchecked` | State assertions. |
| `assert_url`, `assert_title`, `wait_visible`, `wait_hidden` | Page checks and condition-based waits, not fixed sleeps. |
| `capture_text`, `save_variable` | Capture text/input into a runtime variable named by `value`, reusable by subsequent `{{name}}` references. Table cells can be targeted using normal scoped selectors. |
| `new_page`, `open_popup`, `switch_page`, `close_page` | Named-page lifecycle. Creation requires `selector.alias`; switching/closing uses `value`. `main` cannot be closed by an action. `open_popup` wraps a locator click. |
| `dialog` | Registers a dialog expectation before clicking the locator. Options: `decision` accept/dismiss, optional exact `message`/`dialog_type`; `value` is prompt input. |
| `upload_file` | Choose a small synthetic file in the authoring UI, or provide `filename`, `mime_type`, and `base64` fixture bytes. Never opens an arbitrary server path. |
| `download` | Expect a download triggered by a locator click; optional `value` checks suggested filename. Stored under a generated name in run artifacts. |
| `api_request` | `value` is an HTTP(S) URL. Options: method, headers, body, expected `status` (default 200), dotted `json_path`, typed `expected`, and `capture` variable. Redirects are not followed. |
| `mock_route`, `clear_routes` | Scoped browser request mocking/abort. `value` is a URL pattern; options include body, content_type, status or abort. Page route mocks do not intercept Playwright APIRequestContext calls. |

Locator options: `kind` of `css`, `role`, `label`, `text`, `test_id`, or `placeholder`; `primary`; optional accessible `name` and `exact`; `frames` as an ordered list of CSS frame selectors; `page` alias; optional `timeout_ms`.

## Reports, organization and exports

- Edit recording name, normalized tags and parent group in Library. Parent cycles and cross-project parents are rejected. The library renders the hierarchy.
- Filter Library by a tag. Batch include-tags require **all** listed tags; exclude-tags reject **any** match. Empty matches are rejected by existing batch selection validation, not reported as a passing suite.
- HTML reports are generated only when requested, contain escaped text and at most 4 MiB of embedded existing PNGs, and need no external scripts/CDNs. JSON reports preserve recorded step results.
- Report JSON and existing screenshots can still contain user-authored sensitive data. API headers, request bodies and fixture bytes are omitted from new run-log selector copies; this is **not** a complete secret-masking system.
- ZIP exports use disk-backed, uncompressed ZIP creation to avoid CPU-heavy compression. Existing artifact-size caps apply. Download all companion files together.
- Python resources include `requirements.txt`, the shared `testforge_runtime.py`, `steps.json` and `playwright_test.py`.
- JavaScript/TypeScript resources include pinned `package.json`, `testforge_runtime.js`, a standalone JS entry point, `recording.spec.ts` and `playwright.config.ts`. Defaults: **one worker, no parallel tests, no automatic retries, no video, failure-only screenshots**. `npm test` produces the Playwright HTML report when explicitly run outside TestForge.
- The generated Jenkinsfile invokes the exported Python replay. This does not configure or run a Jenkins service.

## Performance/resource changes

1. Existing one-browser/single-process deployment guardrails remain in force; no parallel test implementation was introduced.
2. New dashboard users default to live-run preview off. Previously saved user preferences are respected. The API's legacy `display_window` default is unchanged, but it still does not render frames without a viewer.
3. The newer scenario/reporting extension supersedes this original performance default: API-started single and batch runs now enforce one low-quality JPEG per step, reused in logs/PDFs/live views. The old `none`/`failure` API settings no longer disable required evidence. Recorder step screenshots remain separate. See `scenarios-secrets-and-pdf-reports.md` for cost and security limits.
4. Typing delay is zero in single/batch execution and the new runtime. Necessary Playwright actionability and assertion waits remain; removing them would create false failures or false passes.
5. Recording autosaves write current canonical data without rebuilding CSV/XLSX/scripts on each debounce. Explicit SAVE/publish regenerates exports. Live recording GET reads current database steps rather than a lagging file snapshot.
6. Recorder preview stops rendering when no frame has been requested for three seconds. Hidden recorder tabs stop polling; finished runs stop their detailed polling.
7. Run/batch websocket listeners notice disconnects even when no event arrives. Event queues are bounded to eight (or the lower configured event limit), with four viewers per stream and sixteen total. Slow viewers retain newer events and can retrieve complete logs over HTTP.
8. React and compiled Tailwind CSS are vendored locally. No browser-side Tailwind compiler or runtime CDN fetch is needed. Rebuild only during development with `npm ci --prefix tools/ui && npm run build --prefix tools/ui`; no Node process is added to the deployed Python service.
9. SQLite file databases now use two bounded, reused connections rather than sharing one active transaction across unrelated sessions. This prevents an autosave rollback from erasing a newly recorded step; no database worker or polling job was added. PostgreSQL configuration is unchanged.
10. Batch session reuse is scoped to project **and** origin, not just the hostname. Ordinary shared sessions still require disabling reuse for different accounts within one project. Advanced plans instead use fresh contexts and may select environment-backed authentication profiles.

## Advanced plans: data, fixtures, flows and profiles

Open **Datasets, reusable flows, fixtures & profiles** after importing steps, or
select a synthetic domain pack. The JSON editor is deliberate: no model service
infers test intent, and no opaque source program runs. Example options:

```json
{
  "variables": {"base_url": "https://your-authorized-app.example"},
  "setup": [{"action": "navigate", "value": "{{base_url}}"}],
  "flows": {
    "greet": [
      {"action": "fill", "selector": {"primary": "#name"}, "value": "{{person}}"},
      {"action": "click", "selector": {"primary": "#go"}}
    ]
  },
  "steps": [
    {"flow": "greet"},
    {"action": "assert_text", "selector": {"primary": "#out"}, "value": "Hello, {{person}}"}
  ],
  "dataset": [{"person": "Ada"}, {"person": "Grace"}],
  "teardown": [{"action": "fill", "selector": {"primary": "#name"}, "value": ""}],
  "matrix": [{"browser": "chromium", "device": "desktop"}],
  "retries": 0
}
```

If `steps` is omitted from the options, the reviewed imported steps are used.
Rows × profiles run **sequentially**. Precedence is project variables → plan
variables → dataset row → captures during that case. An explicit dataset row can
supply `base_url` to target a different authorized environment. Named flows expand
with cycle/depth checks; no recursion, arbitrary helper code or dynamic imports.

Each attempt gets isolated browser state. Setup runs first; teardown runs after
setup/test failure and attempts all authored cleanup actions. A failed teardown
fails the case. A retry is a whole fresh attempt, not a hidden per-step retry. The
default is zero; one explicit retry is allowed. A recovered attempt is **flaky**,
not a clean pass: publication and the exported process exit code fail closed.

Limits: 25 cases, six matrix entries, 500 expanded authored steps, 2,000 total
step repetitions **including retries**, 16 downloads, 100 variables per map,
30 named flows and eight flow-reference levels. The Python/server plan engine
has a five-minute overall ceiling; a draft's ceiling also includes browser-slot
waiting. Successful steps advance immediately; these ceilings are not sleeps.
Exported JS has bounded cases/actions and per-action timeouts; the generated
TypeScript test also has Playwright's test timeout. No overnight/scheduled runs
are created.

Chromium single-process mode can crash when replacing closed contexts. In this
mode TestForge closes the old browser **before** launching its replacement for
an isolated case/attempt. This maintains one live browser, but incurs real
case-startup work: it is not truthful to promise instant execution or unchanged
CPU for an expanded dataset. Shared Chromium batches support plans; other-engine
matrices must use the individual recording RUN control. Plans do not produce
live screencasts, video or automatic screenshots; reports contain stage/action/
case/attempt outcomes without target values or exception call logs.

The source plan is retained in the recording. In a downloaded project,
`execution-plan-source.json` is for editing/re-import; `execution-plan.json` is
its validated compiled execution snapshot. Python/JS/TS runners execute that
snapshot. Editing a saved plan's ordinary step rows is blocked: re-import the
source, test it and save the new version. Ordinary recordings retain their
existing editor. Python/JS `RecordedPage` helpers expose reusable locators from
`steps.json`; they are framework building blocks, not auto-designed business POMs.

### Secret and authentication references

For a project variable, save a **reference**, e.g. `env:TF_SECRET_PASSWORD`, rather
than the secret. For plan/dataset variables use:

```json
{"password": {"env": "TF_SECRET_PASSWORD"}}
```

The operator supplies the value in the server environment. Only the dedicated
`TF_SECRET_*` namespace can be read by this reference mechanism—not arbitrary
hosting/GitHub credentials. Missing references fail before the browser launches.
New saved source contains the reference, not the resolved value. Previously
saved literal secrets are **not** migrated, encrypted or removed by this change.

For a matrix entry with `"auth_profile": "STAGING_USER"`, provision
`TF_AUTH_STATE_STAGING_USER` in the hosting environment with this structure:

```json
{"expires_at": 1900000000, "state": {"cookies": [], "origins": []}}
```

Replace the illustrative expiration and empty state with a short-lived,
authorized Playwright storage state, using the provider's secure environment
settings—not chat or a repository file. Expired/malformed/oversized profiles
fail closed. State is loaded into a fresh context and is not written into the
library. This is operator-managed state reuse, **not** automatic login renewal,
an encrypted vault service, tenant isolation or a profile-capture GUI. Cookies
and browser storage remain sensitive at the operating-system/provider level.

### Browser/device opt-ins

Default: **Chromium only**. Desktop, tablet and mobile entries select bounded
viewport/touch profiles; they do not emulate every real device property or
change the user agent. Chromium profiles were tested here.

Firefox/WebKit have execution paths but were **not installed or validated in this
workspace**. An operator must explicitly install the desired engine, verify its
resource requirements, and set `TF_ALLOWED_TEST_BROWSERS`, e.g.
`chromium,firefox`. Nothing downloads engines, increases workers or upgrades a
hosting plan automatically. Leave these disabled when current free capacity is
insufficient. Do not change the existing single-worker/browser limits.

### Synthetic domain packs

The upload panel includes commerce, banking, insurance, HRMS and travel examples.
Each uses local `/domain-demo.html`, a two-row positive/negative dataset and
cleanup assertions. No real payments, accounts or customer information are
involved. The returned demo URL is a **server-browser target**; the frontend
continues to use relative API URLs. Exported examples require that fixture URL
to be reachable from the machine running the export, or editing/retesting the
plan for a new fixture location.

### Draft lifecycle and publication safety

Drafts live in `TF_ARTIFACTS/import-drafts`, which must be outside `TF_LIBRARY_DIR`.
They create no Recording/RecordingStep rows and cannot be included in a normal
library publish. Only one draft task is admitted; the existing global browser
budget still arbitrates drafts, recording, single runs and batches.

History is pruned **on demand**, not by another timer/service: at most ten drafts,
one-hour receipts, and an older-history byte threshold of 32 MiB before admitting
the next draft. One active draft can temporarily add up to its bounded source,
result and download allowance beyond that threshold. These limits do not cover
all existing application artifacts or provider disk quotas. Cancel a draft in
the UI to release its browser; a restart invalidates test receipts.

Generated `node_modules`, replay outputs, Python caches, Playwright reports and
result files are excluded from library publication. API publishing prunes those
directories before traversal/hashing; Git mode has matching ignore rules.

## Explicit local/CI gate—no automatic billing trigger

After installing the documented test dependencies and starting an isolated,
publishing-disabled preview, run:

```bash
TF_TEST_NODE_MODULES=/path/to/node_modules \
TF_TEST_APP_URL=http://127.0.0.1:8000 \
python tools/quality_gate.py
```

The gate runs the required-browser harness, real-browser/export-plan checks and
dashboard workflow **sequentially**. Missing prerequisites or a failed check
return nonzero. No package installer, GitHub push, workflow dispatch, deployment
or schedule is invoked. The dashboard test refuses a publishing-enabled server;
never target production. A CI provider can run this command when explicitly
configured, but its hosting/minute/storage costs remain provider-controlled.


## Access control and release prerequisites

Set **`TF_ACCESS_TOKEN` to a random value of at least 32 characters** in the hosting environment before public use. The UI offers a login screen, and protected HTTP/websocket endpoints require a bearer token or signed, HttpOnly, SameSite session cookie. Sessions expire after 12 hours; HTTPS deployments use Secure cookies. Websocket cookie access rejects foreign origins.

This is a **single-operator gate, not roles, tenant isolation or an identity provider**. If the variable is empty, legacy unprotected operation remains and the dashboard warns. A configured but short token fails closed. Use HTTPS and a private/authenticated gateway; do not send the token in chat. Health and auth-status endpoints remain public.

Outstanding pre-existing blockers: rotate real credentials already committed, implement proper secret references/redaction, constrain authorized browser/API network destinations, and verify database/artifact/library persistence and backups. The access gate alone does not resolve those issues.

## Roadmap status after this change

| Requirement | Current delivery / remaining work |
| --- | --- |
| R01 — step/locator contract | Versioned validated strict actions and basic structured/frame/page locators implemented. Arbitrary source-language conversion and every locator composition are not supported. |
| R02 — assertions/waits | Core assertions, condition waits and missing-variable failure implemented and browser-tested. Full structured expected/actual diagnostics beyond current logs still need improvement. |
| R03 — controls | Common controls and extended interactions implemented. Specialized autocomplete/table row helper UIs and exhaustive interaction matrices remain. |
| R04 — dialogs/files | Explicit dialog, bounded synthetic upload and download steps implemented. General filesystem access is intentionally prohibited. |
| R05 — pages/frames | Explicit page aliases, popup switching and frame targeting implemented. Automatic inference of arbitrary iframe/tab interactions from remote clicks is not complete. |
| R06 — API/network | Basic HTTP response/JSON assertions, API-to-UI capture and scoped browser mocks implemented. Advanced response schemas, arbitrary request expressions and response-event authoring remain. |
| R07 — target auth | Environment references, operator-provisioned expiring storage-state profiles, and the newer encrypted project-input/keyring support are implemented and Chromium-tested. No vault service, automatic login renewal, profile-capture GUI, RBAC or remediation of old committed secrets. |
| R08 — data/fixtures/profiles | Bounded JSON datasets, variable overrides, setup/teardown, fresh contexts and profile matrices implemented through advanced plan options. Separate CSV/XLSX dataset-table management and a graphical environment-profile editor are not supplied. |
| R09 — reusable framework | Named flow composition with cycle/size checks, retained source plans, portable Python/JS/TS plan runners and reusable Python/JS locator page-object facades implemented. This is not automatic generation of a domain-specific enterprise POM architecture. |
| R10 — reports | Portable HTML/JSON, resource ZIPs, reusable per-step JPEGs and cached single/batch PDFs implemented; temporary GitHub attachment transport/retention is mocked, not live-provider verified. Rich trace analysis/history comparison remains. |
| R11 — tags/retries | Tags and deterministic selection filters implemented. Plans default to zero retries, allow one explicitly requested retry, identify flaky recovery and reject it as a clean pass for publication. |
| R12 — CI/release | Explicit sequential `tools/quality_gate.py` added; it fails on missing prerequisites/test failures. No automatic CI workflow, scheduled job or rollout is activated. Production deployment evidence and provider billing configuration remain outside local validation. |
| R13 — browser/device matrix | Sequential Chromium/Firefox/WebKit selection and desktop/tablet/mobile viewport-touch profiles implemented. Chromium responsive profiles are browser-tested. Firefox/WebKit are disabled by default, not installed or tested here; operator installation/validation is required. These are not physical-device/cloud-device tests. |
| R14 — parallel workers | **Intentionally excluded at the user's request.** Sequential execution remains. |
| R15 — domain packs | Five local synthetic packs implemented and browser-tested: commerce, banking, insurance, HRMS and travel; each includes positive/negative data and teardown. They do not certify real business, financial or regulatory rules. |

Autonomous AI and visual baseline comparison were not added; they were not part of the supplied roadmap, and paid model calls conflict with the requested cost posture.

## Validation commands

```bash
python -m pip install -r requirements-test.txt
python -m playwright install chromium
python -m tests.harness --require-browser
TF_TEST_BROWSER=1 python -m unittest tests.test_browser_features -v
```

The export-parity case additionally needs Node's `@playwright/test@1.49.1`, with `TF_TEST_NODE_MODULES` pointing at its containing `node_modules` directory. The opt-in dashboard test requires an isolated **publishing-disabled** server and `TF_TEST_APP_URL`; never point it at production. See the source docstrings in `tests/test_browser_features.py` and `tests/test_dashboard_import.py`.

Passing tests establish local behavior, not production deployment, vendor billing guarantees or complete roadmap delivery.


## Recorded validation for this branch

The latest full gate passed:

- **215 harness checks passed; 0 failed, 7 skipped.** The skips are the Chromium
  scenarios: this sandbox cannot reach the browser mirrors, so no real-browser
  check ran here.
- **44 import/onboarding checks** (`tests/test_imports.py`, `UT-IMP-01…44`) cover
  every newly accepted format, the variable proposal/rewrite path and the
  Execution-plan catalog, including the refusals (fixed sleeps, macros,
  multi-case sheets, unsupported statements).
- A live-server smoke test exercised the same endpoints the dashboard calls:
  preview of a `.txt` script, `variable-plan` → `variables` (steps rewritten to
  `{{username}}`, one variable created), and `plans/catalog` returning the new
  project and variable immediately.

Evidence: [harness report](../logs/20261011-153237/results.md). The report
revision is the base Git HEAD; these checks exercised this modified, uncommitted
working tree. Python/JavaScript syntax checks (`node --check` on all three
served scripts, `python -m compileall`) and `git diff --check` also passed.

**No live GitHub write or production rollout was performed.** Tests use isolated
storage and disabled/stubbed publication. No external browser matrix or provider
billing settings were verified. The live preview is for validation only and has
publishing disabled.
