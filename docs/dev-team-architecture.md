# TestForge for the in-house dev team: architecture and codebase map

Read this before your first change. It answers three questions: what each part
owns, which constraints a change must not break, and where new code belongs.

Everything below is a statement about this repository, not a general FastAPI
tutorial. Where a claim depends on a deployment choice, it says so.

## 1. What the product is

TestForge is a single-service test-automation tool:

- a **FastAPI backend** (`app/`) that records browser and Windows-desktop
  sessions, stores them, replays them and produces evidence;
- a **build-free React dashboard** (`app/static/`) served by the same process;
- a **library on disk** (`library/`) of projects, recordings and their
  resources, optionally published to a GitHub repository;
- **portable exports** (Playwright Python/JS, Selenium, C#, Jenkinsfile, CSV/XLSX
  matrices) so a team can run a recording outside this service.

One process does all of it. There is no separate worker service, no message
broker and no scheduler beyond the run queue.

## 2. Module map

| Area | Module | Owns |
| --- | --- | --- |
| HTTP surface | `app/main.py` | FastAPI app, the access-gate middleware, recording/run/artifact routes, `/api/health`, `/api/diagnostics` |
| Access gate | `app/access_control.py` | Optional single-operator token gate (`TF_ACCESS_TOKEN`), ASGI middleware |
| Configuration | `app/config.py` | `TF_ARTIFACTS`, `DATABASE_URL` resolution, browser and interval limits |
| Persistence | `app/db.py` | SQLAlchemy models, engine/pool choice, schema create + repair at boot |
| Secrets | `app/secrets_store.py` | Encrypted variable values (`enc:v1:` prefix), key handling, masking |
| Errors | `app/errors.py` | `redact()` — strips credentials from anything shown to a user |
| Browser | `app/browser.py` | How Chromium is launched, launch-error explanation, video toggle |
| Recording | `app/recorder.py` | Server-side browser recording, live JPEG frames, step capture |
| Replay | `app/executor.py` | Replaying a recording, streaming frames to the Runs tab |
| Step runtime | `app/step_runtime.py` | The canonical action set and selector rules; also shipped inside exports |
| Plans | `app/plan_runtime.py` | Execution plans: matrix, setup/steps/teardown, dataset rows |
| Queue | `app/run_queue.py` | Orphan, stale and duplicate run hygiene |
| Batch | `app/batch_runner.py` | Many recordings in one browser, with memory-driven relaunch |
| Library | `app/library_store.py` | The `library/` tree, catalog caching, publication |
| Publishing | `app/github_api.py` | Publishing `library/` through the GitHub REST API when there is no git checkout |
| Exports | `app/export_formats.py`, `app/export_templates.py` | Portable script/matrix exports |
| Imports | `app/import_formats.py`, `app/script_import.py`, `app/import_routes.py`, `app/import_drafts.py`, `app/import_templates.py` | Onboarding existing test scripts into canonical steps (uploaded code is never executed) |
| AI onboarding | `app/ai_onboarding.py`, `app/onboarding_jobs.py`, `app/variables_ai.py` | Optional AI repair of imported steps and variable extraction |
| Desktop | `app/desktop_runner.py`, `app/desktop_recording.py`, `app/desktop_install.py` | Windows UIA agent bridge, deterministic simulator, interactive desktop recording, pre-recording app installation |
| Mobile | `app/mobile_runner.py` | Appium/lab bridge plus deterministic device simulator |
| Roadmap | `app/roadmap_routes.py`, `app/support_matrix.py`, `app/domain_packs.py` | Support matrix, pilots, registered desktop apps, synthetic demo packs |
| Reports | `app/report_routes.py`, `app/pdf_reports.py`, `app/evidence.py`, `app/images.py` | On-demand reports and bounded evidence images |
| Logs | `app/logs_local.py` | Bounded reader for the harness logs on this instance |
| Settings | `app/app_settings.py`, `app/settings_routes.py` | Instance-wide region, time zone, formats, language |
| Docs kit | `app/dev_kit.py`, `app/dev_prompts.py` | The Dev & Deploy tab: guides, SQL scripts and the AI prompt library |
| Guardrails | `app/guardrails.py` | Process-wide resource limits reported by `/api/health` |

`app/agent.py` is **not imported anywhere** and does not import cleanly; treat it
as dead code and do not extend it.

## 3. Data: two stores, on purpose

**SQL database** (`DATABASE_URL`, resolved in `app/config.py`): projects,
variables, recordings, recording steps, runs, batches, app settings. The models
are in `app/db.py`.

**Library tree** (`TF_LIBRARY_DIR`, default `library/`): one directory per
project, and inside each recording a `recording.json`, its execution plan and a
`resources/` folder holding screenshots, exported scripts and matrices. This is
what gets published to GitHub, and it is what survives a database reset.

If you add a persisted artifact, decide which store it belongs to and say why in
the review. Evidence images and exports belong on disk; queryable state belongs
in the database.

## 4. Constraints a change must not break

1. **One browser at a time.** `MAX_CONCURRENT_BROWSERS = 1` in `app/config.py`,
   `TF_MAX_BROWSERS` at deployment. Recording and execution share that budget.
   A feature that opens a second Chromium is a rejection, not a tuning problem.
2. **One interactive recorder.** Browser recording and Windows desktop recording
   are mutually exclusive; `app/main.py` returns HTTP 409 if you try to start one
   while the other is live.
3. **Sequential execution.** Runs execute one at a time through the queue
   (`app/run_queue.py`). Batches reuse a single browser deliberately.
4. **No new dependency without a stated reason.** `requirements.txt` is pinned
   and deliberately short because the deployment target is a small instance.
5. **No new background worker or scheduler.** Existing loops are bounded and
   documented; "add a cron" is not an accepted design here.
6. **Bounded everything.** Every request body, list, file and screenshot has an
   explicit numeric limit. New code that accepts client input needs one too.
7. **No secrets in logs, artifacts, exports or error messages.** Use
   `app/errors.redact()` for anything derived from an exception, and
   `app/secrets_store` for values at rest.
8. **No build step in the dashboard.** `app/static/*.js` are plain scripts using
   `React.createElement`. No JSX, no bundler, no npm install.

## 5. Where new code belongs

| You are adding… | Put it in |
| --- | --- |
| A route for an existing feature | the feature's existing router module |
| A route for a new feature | a new `app/<feature>_routes.py` with its own `APIRouter`, included at the bottom of `app/main.py` |
| A new column | `app/db.py` — `init_db()` adds missing columns at boot; there is no migration tool |
| A new dashboard screen | `app/static/<feature>-components.js` + three edits in `app/static/index.html` (script tag, `tabs` entry, render line) plus a `TabSupportFooter` line |
| A new action for plans | `app/step_runtime.py` (web) or `app/desktop_runner.py` / `app/mobile_runner.py` |
| A guide for operators or developers | `docs/*.md`, then register it in `app/dev_kit.py` so the Dev & Deploy tab serves it |
| An AI prompt for the team | `app/dev_prompts.py` — the downloadable markdown is generated from it |

## 6. Reading order for a new developer

1. `readme.md` — features, environment variables, testing.
2. `app/main.py` — the route list is the product's real feature list.
3. `app/db.py` — the data model.
4. `app/recorder.py` and `app/executor.py` — the two paths everything else feeds.
5. `app/static/index.html` — the `tabs` array, then one component file end to end.
6. `docs/dev-local-setup.md` — get it running, then run the harness.
7. `docs/dev-azure-sql-server.md` and `docs/dev-deployment-end-to-end.md` — how it
   is deployed, and what that constrains.

## 7. Honesty rules this repository follows

- A simulated target is always labelled as simulated. The Windows desktop and
  mobile runners say whether they are talking to a real agent or to the
  deterministic simulator, in the UI and in the API response.
- Nothing claims vendor certification it does not have. See
  `app/support_matrix.py` for how the badges are computed.
- Docs mark anything that depends on a quota, a price or a vendor limit with
  `[verify]` so a reader checks it instead of trusting it.

Follow those rules in new code: if a feature is a simulator, say so in the
response payload, not only in the docs.
