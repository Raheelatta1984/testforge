# TestForge Testing Tool
- **Database:** Permanently stored in Neon PostgreSQL / SQLite (Configured in config.py).
- **Architecture:** 19-tab persistent dashboard (optimized for Android 12) with visible, horizontally scrollable tab names and accessible labels.
- **Demo tab:** guided walkthrough over the built-in sample app and five synthetic fixtures, plus optional public shopping/grocery/fashion learning URLs.
- **Future release planning tab:** a clearly labeled, non-committal roadmap view for browser ERP/CRM packs, Windows desktop (.NET/C#/C++/WinForms) and Android/iOS/Zebra device automation; proposed items are not described as current support.
- **Azure guide:** the **Azure tab** renders `docs/azure-devops-deployment-guide.md` inside the app (`GET /api/docs/azure-guide`) as checkable, follow-along steps for end users.
- **Local helpers (no paid AI calls):** Browser-dependent voice input, deterministic text cleanup, and recording-variable association tagging.
- **AI smart onboarding for uploads:** the static import validator runs first; when it reports unsupported syntax and `ANTHROPIC_API_KEY` is set, the script is converted to canonical steps by a budgeted AI loop that re-validates **every single step** (validate → AI fix → re-validate). Uploaded source is still never executed, the save gate (passing test or written override) is unchanged, and without the key import stays fully offline. A runner script + its referenced `steps.json`/`execution-plan.json` can be uploaded as a bundle; the referenced steps are what gets imported.
- **AI onboarding loop (background, stoppable):** with `ANTHROPIC_API_KEY` set, PROCESS starts a background job (`POST /api/imports/onboard`) that loops convert → validate every step → AI repair → re-validate, and keeps going until **every step validates (100%)** or you press **STOP** (`POST /api/imports/onboard/{job_id}/stop`). Progress (round, validated steps, AI calls, repairs) is polled from `GET /api/imports/onboard/{job_id}`. A stopped or incomplete conversion is returned with its best steps but is always marked as an error, so SAVE stays blocked until the script is complete and tested. Safety ceilings (not the goal): `TF_ONBOARD_MAX_ROUNDS` (25), `TF_ONBOARD_MAX_CALLS` (300), `TF_ONBOARD_MAX_STEP_FIXES` (400), `TF_ONBOARD_TIME_LIMIT` (1800 s). Steps that still fail after their repair attempts are now reported with the reason instead of being dropped silently.
- **Fast AI recording start and inputs:** START creates the recording from local files and returns at once; the GitHub push runs in the background. The **Inputs** screen opens immediately, reads the variables as they are on disk now (including ones saved from an AI recording a moment ago), lists the ones the recording uses first, flags names the project does not have yet, and has a REFRESH button. SAVE RECORDING DEFAULTS and RUN ONCE do not wait for GitHub either.
- **Recorder screen:** the remote browser fills the screen (FIT, or zoom with scroll bars), clicks are mapped correctly through the letterbox, and **⌨ TYPE DIRECTLY** sends what you type on the picture to the remote page (printable text is batched into one typed step; Enter, Tab, arrows and Ctrl combinations are sent as keys).
- **Scroll bars:** every tab scrolls vertically and horizontally with visible bars, including the bottom tab strip and wide tables.
- **Tree Hierarchy:** Parent Group -> Sub-recording chunk organization. Parent/child, tags and
  start URL survive the catalog fast path, so the Library, the Record tab's tree and
  `GET /api/plans/catalog` all describe the same recording the same way.
- **Entities (companies):** a company/entity is created before its projects and every project belongs to one; the registry is `library/entities.json`, the Projects tab groups and filters by it, and a project written before entities existed sits in an `Unassigned` bucket that can be moved at any time.
- **Previous recordings tree (Record tab):** every recording in the library, grouped by project and nested by parent recording, with **RESUME** (open the old recording and append) and **RUN** (execute it as a run). **SCAN LIBRARY** / **RESCUE** list and re-export recordings that fell out of the catalog or exist only in the database, so nothing is lost.
- **Run a recording inside a recording:** while a session is live, `POST /api/recordings/{id}/replay` executes another saved recording in that same browser; its steps are added to the recording being made (or run without being written, at the operator's choice).
- **Repeated steps:** Consecutive identical actions are recorded as one step carrying a repeat count, and replay expands it back into N actions.
- **Execution:** Optional step-by-step live view in the Runs tab, reusing the evidence JPEG without a second screencast loop.
- **Batch execution:** One browser at a time replays recordings, with learned timeouts and one reusable low-quality screenshot per step (`POST /api/runs/batch`).
- **Queue hygiene:** Orphaned, stale and duplicate queue entries are reported and can be cleared; a restart no longer leaves runs pending forever.
- **Publishing:** `library/` is committed to one branch, by `git push` from a checkout or through the GitHub REST API when the deployment has none.
- **Market position:** a self-auditing comparison against 12 other tools (autonomous-QA MCPs, browser-driver MCPs, open-source runners and commercial platforms) across 42 capabilities. Every TestForge **Yes** is a claim with machine-checkable evidence (`file`, `symbol`, `route` or `test`) that the claim audit re-verifies; the gap list is published, not hidden. `GET /api/market/matrix`, `GET/POST /api/market/audit`, `GET /api/market/report.html`, `GET /api/market/report.md`. Zero-LLM gate: `python -m tools.market_audit` exits non-zero when a claim loses its evidence. See [`docs/market-position.md`](docs/market-position.md).
- **QA intelligence:** reuse-first step resolution over the stored library (`GET /api/steps/catalog`, `GET /api/steps/search`, `POST /api/steps/resolve`), change-to-test impact analysis with reasons and a batch payload (`GET /api/impact/map`, `POST /api/impact/analyse`), a deterministic black-box explorer that verifies each finding and seals a receipt (`POST /api/explorer/scan`), and JUnit/JSON CI results (`GET /api/runs/{id}/junit.xml`, `GET /api/runs/batch/{id}/junit.xml`).
- **Maintenance:** Check `/api/library` for the publish mode, live remote verification and retry status. Failed pushes remain local and are retried; do not reset a branch to fix sync issues.

## Script upload, assertions and lower-overhead execution

See [Script import and sequential automation additions](docs/script-import-and-actions.md)
for the implemented workflow, supported format subsets, action schema, resource limits,
access-token setup, and the roadmap items still outstanding.

- **Library → UPLOAD TEST SCRIPTS:** preview CSV/TSV, XLSX, canonical JSON, supported
  Playwright Python/JS/TS, BDD/Cucumber phrases, or a restricted Jenkins + script bundle.
  Uploaded source is never executed; unsupported statements block saving.
  Use **TEST BEFORE PUBLISHING** to run a private draft; the dashboard enables
  publication only after a passing test of the reviewed execution inputs. **A pass keeps
  working:** the receipt is held for the project, so opening another tab, reloading the
  page or losing one status poll no longer leaves **SAVE TO LIBRARY & PUBLISH** greyed out
  against a test that already passed, and a lock always prints its reason. When a test
  genuinely cannot pass, **SAVE WITHOUT A PASSING TEST** is an explicit, reasoned override
  that publishes the recording tagged `unverified` and badged as such everywhere.
- **Library tab lists the whole library:** every recording in `library/` grouped by
  project — AI recordings from the Record tab and scripts published from an upload alike —
  with the selected project first, plus search, tag/project filters, an "unverified only"
  switch and per-recording EDIT / RUN / project-ZIP actions. It used to show only the
  selected project, so anything recorded under another one looked unsaved.
- **Advanced plans:** bounded datasets, setup/teardown, reusable named flows,
  environment-backed secret/auth references, sequential responsive profiles,
  explicit retries/flaky detection, and five local synthetic domain packs.
  Chromium is default; other engines require operator installation/validation.
- **Assertions & advanced actions:** author state checks, controls, frame/page targets,
  dialogs, synthetic file fixtures, API checks and network mocks in the recorder/editor.
- **Sequential, no paid AI:** new dashboard users start with live preview off; API-started
  single and batch runs capture one low-quality JPEG per step. Successful actions have no artificial typing
  delay. Necessary actionability/assertion waits remain.
- **Reports and exports:** cached single/batch PDF and downloadable HTML/JSON reports, plus complete runnable
  Python/JavaScript/TypeScript project ZIPs; generated TypeScript uses one worker.
- **Access:** configure a random 32+ character `TF_ACCESS_TOKEN` before public use.
  Empty means legacy unprotected access; this gate is not multi-user RBAC or secret storage.
- **CI-ready:** `python tools/quality_gate.py` runs the full local gate explicitly
  and sequentially; prerequisites are documented in the implementation guide.
- No paid service, extra worker process, automatic CI workflow or production deployment was added.
  Hosting bills and instantaneous performance cannot be guaranteed by application code.

## Recording, repeats and the live window
Consecutive identical actions (the same click, the same key, the same text into the same field) are stored as a single step with `repeat_count = N`. The record tab shows the count as `×N 🔁`, exports carry it, and replay performs the action N times. `save_variable` is deliberately excluded: two captures of one input normally hold different values, so each keeps its own row. Set `TF_MERGE_REPEAT_STEPS=0` to store one row per action instead. Recordings saved before this rule can be collapsed with `POST /api/recordings/{id}/steps/compress`.

The **Runs** tab keeps the *Live browser window* switch and a compact browser/step monitor at the top, above queue hygiene and batch controls. The preference defaults off for new users and is remembered per browser. Off, runs replay headless; on, low-quality frames stream while the tab is open. You can also switch the selected single recording on or off while it is queued/running, without restarting it. Frames carry the executing step number and the post-step evidence frame is sent before that step is marked complete. Either mode preserves the run result, and no live frame is rendered while nobody is watching. Batch runs keep the mode chosen when the batch starts.

SAVE is safe against a slow export: `stop()` waits for the debounced export, leaves it running rather than cancelling it mid-write, and always closes the browser.

## Publishing: git checkout or GitHub API

A save writes `library/` and then publishes it. Which mechanism runs depends on the deployment, and `GET /api/library` says which one in `publish_mode`:

| `publish_mode` | When | What happens on save |
| --- | --- | --- |
| `checkout` | A `.git` directory exists at or above the library folder | Commit `library/` on the checked-out branch and `git push`, then verify the remote SHA with `ls-remote`. |
| `api` | No checkout, but `TF_GITHUB_REPO` (or a `TF_GIT_REMOTE` URL) **and** `TF_GITHUB_TOKEN` are set | Commit the same files through the GitHub REST API — blobs, tree, commit, ref update — then read the branch back. No git binary and no clone needed. |
| `disabled` | Neither | Files are written locally, nothing is committed, and **no retry is scheduled**. |

Every save response carries `publish_state`, `publish_message`, `retry_scheduled` and `retry_in_seconds`, and the dashboard prints `publish_message` verbatim. A retry is only promised when the retry loop was actually started; with publishing disabled the message says so and names the missing configuration, and with a push GitHub *refused* (`401`/`403`, or a denied `git push`) the message says refused, gives the fix, and schedules nothing — no wait changes GitHub's answer. A rate-limit `403` is the exception the classification keeps: same status, still retried. That wording comes from one function, `library_store.publish_outcome()`, so the record tab, the step editor and the GitHub tab cannot disagree.

`GET /api/github/access` answers the question behind a refusal: one read of `GET /repos/{owner}/{name}` reports the token's `push`/`pull` permissions (and a classic token's scopes), so `state` is `ok`, `read-only`, `no-access`, `unknown` or `unconfigured` before another save is attempted.

API publishing is opt-in on purpose. A CI runner exports `GITHUB_TOKEN` automatically, and a token alone must not be enough to start committing to a repository nobody named: without `TF_GITHUB_REPO` the mode stays `disabled`. Pushes are bounded by `TF_GITHUB_MAX_FILES`, `TF_GITHUB_MAX_FILE_BYTES` and `TF_GITHUB_MAX_PUSH_BYTES`; anything skipped is listed in the response rather than dropped silently, and credentials are scrubbed from every error message. The pen-test scenarios (`UT-SEC-*` in `tests/SCENARIOS.md`) attack this surface: token exfiltration through responses, error bodies and request paths, and the blast radius of a single push.

`GET /api/library/publish/plan` shows what the next publish would add, change and delete without publishing it.

## Execution queue hygiene

`POST /api/runs` used to be write-only, so two kinds of junk accumulated in the queue:

* **Orphans** — a deploy, an OOM kill or a crash leaves every `queued` and `running` row exactly as it was. No worker owns them any more, so they can never finish, yet the dashboard counted them as pending forever.
* **Duplicates** — pressing RUN twice queued the same recording twice, and with a browser budget of one the second run just waited behind the first.

Orphans are cancelled at boot and by a sweeper every `TF_QUEUE_SWEEP_SECONDS`. `POST /api/runs` now reuses a queued, not-yet-started run of the same recording instead of stacking another one (`force: true` overrides). `GET /api/runs/queue/status` reports `hygiene` — orphans, stale entries, duplicates, the oldest queued age and the policy in force — and `POST /api/runs/queue/clear` clears them:

```json
{"older_than_minutes": 30, "include_orphans": true, "include_stale": true,
 "include_duplicates": false, "cancel_running": false,
 "purge_finished_days": 14, "dry_run": true}
```

A dry run returns exactly the candidates the real call would act on, each with the reason it was selected. Cancelling a run that is mid-replay is possible with `cancel_running`, but a batch stops *between* recordings rather than mid-step, so a browser is never killed while it is writing artifacts.

## Batch execution

`POST /api/runs/batch` replays many recordings as one job. Select with `recording_ids`, `project_id` or `all`, then:

```json
{"recording_ids": ["…"], "screenshots": "failure", "share_session": true,
 "retry_transient": true, "display_window": false, "name": "Nightly"}
```

What makes it faster and lighter than running the recordings one at a time:

| Choice | Effect |
| --- | --- |
| One active Chromium, reused where compatible | Same-project/origin shared sessions avoid launches. Single-process context isolation requires closing before sequential relaunch; reports count actual launches and savings. |
| Origin grouping, shortest first | Same-host recordings run back to back and share a warm context (and a login). Shortest-first gives feedback early. |
| Learned step budgets | `AdaptivePacer` keeps an EMA of how long each kind of step actually took and derives the next timeout from it, clamped between a floor and the single-run ceiling. A click that always lands in 120ms is not given three seconds to fail in. The profile persists in `artifacts/batches/pacing.json` and can be seeded from run history. |
| `screenshots: failure` (default) | A PNG only when a step fails. `changes` captures only when the page moved, `none` never, `all` behaves like a single run. Step screenshots are the largest per-step CPU and disk cost of a replay. |
| One retry, transient only | A timeout or a network error is retried once with a relaxed budget. A missing selector is a real defect and is not retried, because retrying it only costs time. |
| Memory guard | RSS is sampled between recordings; past `TF_BATCH_MAX_RSS_MB` the browser is closed and relaunched to hand the memory back before the platform does it for us. |
| One batch at a time | `TF_MAX_BATCHES` (default 1). A second batch is refused with 409 rather than queued behind the first, holding run rows and a browser slot. |

The report (`GET /api/runs/batch/{id}`, also written to `artifacts/batches/<id>.json`) carries per-recording timings, throughput, browsers launched, contexts opened, RSS before and after, screenshot counts, retries and the pacing profile. Progress streams over `/ws/batches/{id}`; each recording also gets a normal `Run` row with `batch_id` set, so the Runs tab and the audit work unchanged.

## Hosting guardrails

The service is sized for a 512MB instance. Every limit below exists because something was previously unbounded, and each one has an environment override. `GET /api/diagnostics` reports the live values plus this process's resident memory and cgroup limit, so a slow leak is visible before the platform restarts the service.

| Guardrail | Why |
| --- | --- |
| One Chromium across recording *and* execution | Runs were capped; recording sessions were not, so both could hold a browser at once. |
| Live frames bounded by run count and TTL | The last frame of every run was kept for the life of the process. |
| Run event buffers bounded | Up to 200 events per run were kept forever. |
| `GET /api/runs` limited, log omitted by default | The dashboard polls it every 3s; it used to return every run ever recorded with every step. |
| One batch at a time, capped recordings per batch | A batch holds the single browser slot for its whole length, so a second one would only queue. |
| Batch log entries and reports capped | Per-run step logs and the on-disk report count are bounded (`TF_BATCH_LOG_ENTRIES`, `TF_BATCH_REPORTS`). |
| Local log reader capped and cached | The no-checkout fallback reads only the tail of a file, only known extensions, only inside `logs/`, and caches the index. |
| API publishing bounded | File count, per-file size and total push size are capped, so one save cannot build an enormous request. |
| `GET /api/sync/github` streamed from disk, capped | It built the entire artifact tree in a `BytesIO` and returned it in one piece - a one-request OOM. |
| One uvicorn worker | The limits are per-process; a second worker would double all of them. |

The **Logs** tab reads `logs/` straight from GitHub in your browser via `raw.githubusercontent.com`. The service only supplies the coordinates from `GET /api/logs/source` - it never reads, stores or serves the log files, so opening that tab costs the deployment nothing.

The coordinates come from the git checkout when there is one, and from `TF_GITHUB_REPO` with `TF_GITHUB_BRANCH` when there is not: reading a public repository needs neither a token nor a git binary. If neither is configured the tab no longer dead-ends at *"No git checkout here, so the log location is unknown"* - it falls back to the reports this instance wrote itself, through `GET /api/logs/local/index` and `GET /api/logs/local/{folder}/{name}`. That reader is deliberately small: only `logs/`, only `.md`/`.log`/`.json`/`.txt`, only the tail of a file up to `TF_MAX_LOG_FILE_BYTES`, at most `TF_MAX_LOG_INDEX_ROWS` runs, cached for ten seconds, and every path is resolved and re-checked so `../` cannot escape. The tab shows which source it is reading and lists what to configure to get the GitHub path back.

The dashboard also stops polling while its tab is hidden.

## QA agent hub

The **QA Agents** tab runs nine QA agents: Requirement Review, Test Case Generation, API Contract, Automation Code Review, Failure Triage, Flaky Test Analysis, Defect Drafting, Regression Selection and Release Summary. Paste an input (LOAD SAMPLE fills one in) or run on data already stored here: a run, a recording, or a project. The rules engine always answers and needs no network. With `ANTHROPIC_API_KEY` set, or with `TF_AGENT_AI_PROVIDER=ollama` for a local model that keeps the artifact on your network, one bounded call per run adds short AI insights; it never changes a rule finding. Test Case Generation also returns a Gherkin feature. The agents are available to Claude Code CLI and other MCP clients through `python -m app.qa_agents.mcp_server` (stdio, read-only tools). Endpoints: `GET /api/agents`, `GET /api/agents/{id}/sample`, `POST /api/agents/{id}/run`, `POST /api/agents/{id}/from-source`. Full behaviour, limits, the decision rules, and the comparison with existing functionality are in [`docs/qa-agent-hub.md`](docs/qa-agent-hub.md). Test IDs are `UT-AGT-01` to `UT-AGT-22`.

## Market position and the claim audit

The **Market position** tab (and `GET /api/market/matrix`) compares TestForge with the tools proposed to the same buyers: Argus Testing, TestAtlas, Playwright MCP, Chrome DevTools MCP, browser-use, Playwright, Cypress, Selenium, Katalon, mabl, testRigor and Applitools. It is built to be checkable rather than persuasive:

- **No unsourced competitor cell.** Each tool records the basis it was read from (the supplied GitHub MCP listing screenshots, vendor documentation, or an explicit `unknown`). "Not stated" is excluded from the score — it is neither a win nor a loss.
- **No unsupported TestForge claim.** Every **Yes** carries machine-checkable evidence probes, and the audit re-verifies them. A claim that loses its evidence shows as **Unverified**, becomes a gap, and fails `python -m tools.market_audit` (exit 1) — so a stale claim cannot survive CI. `POST /api/market/audit` writes a 24-hour receipt with a SHA-256 over its own content.
- **The gaps are published.** Seventeen capabilities show a competitor ahead (parallel execution, visual regression, self-healing, WCAG conformance, network mocking, multi-browser, RBAC and others), each with the leading tools and the roadmap disposition.
- **Two artefacts, one source.** `GET /api/market/report.html` serves a self-contained, printable document (strict CSP, no scripts, no external assets) and `GET /api/market/report.md` serves the same matrix as Markdown.

Gaps that could be closed without a model or a paid service were implemented in this repository: deterministic black-box discovery with independent verification and receipts (`app/explorer.py`), a reusable step catalogue with phrase resolution (`app/step_catalog.py`), a library dependency graph with impact analysis (`app/impact_map.py`), JUnit/JSON CI output (`app/ci_results.py`), and five extra read-only MCP tools. Full method, limits, API reference and the honesty rules are in [`docs/market-position.md`](docs/market-position.md). Tests: `tests/test_market.py`.

## Environment variables
| Variable | Default | Effect |
| --- | --- | --- |
| `TF_MERGE_REPEAT_STEPS` | `1` | `0` stores one row per action instead of merging repeats. |
| `TF_EXPORT_FLUSH_TIMEOUT` | `10` | Seconds `stop()` waits for the debounced export. |
| `TF_PREVIEW_INTERVAL` | `0.18` | Base seconds between live preview frames. |
| `TF_VIEWER_RECHECK` | `0.15` | Seconds between viewer checks while the preview is idle. |
| `TF_JPEG_QUALITY` | `35` | Preview JPEG quality. |
| `TF_RUN_EXCERPT` | `1` | `0` skips the per-step body-text excerpt in the run audit. |
| `TF_MAX_BROWSERS` | `1` | Concurrent Chromium processes, recording and execution combined. |
| `TF_MAX_LIVE_FRAME_RUNS` | `4` | How many runs keep a last frame in memory. |
| `TF_LIVE_FRAME_TTL` | `120` | Seconds before a cached frame is dropped. |
| `TF_MAX_RUN_BUFFER_RUNS` | `8` | How many runs keep a replay buffer for late clients. |
| `TF_MAX_RUN_BUFFER_EVENTS` | `60` | Events kept per buffered run. |
| `TF_RUN_LIST_LIMIT` | `25` | Default page size of `GET /api/runs`. |
| `TF_RUN_LIST_MAX` | `100` | Hard cap on that page size. |
| `TF_MAX_ZIP_BYTES` | `67108864` | Largest artifact export before it returns 413. |
| `TF_GITHUB_REPO` | none | `owner/name` to publish to over the API. Required for `publish_mode=api`; there is no built-in default. |
| `TF_GITHUB_TOKEN` | none | API token that may write repository contents: a fine-grained token with **Contents: Read and write** on `TF_GITHUB_REPO`, or a classic token with the `repo` scope. `GITHUB_TOKEN`/`GH_TOKEN` are also read. |
| `TF_GITHUB_BRANCH` | `main` | Branch the API publisher commits to. |
| `TF_GITHUB_API` | `https://api.github.com` | Override for a proxy or an Enterprise host. |
| `TF_GITHUB_MAX_FILES` | `400` | Most files in one API push. |
| `TF_GITHUB_MAX_FILE_BYTES` | `2097152` | Largest single file in one API push. |
| `TF_GITHUB_MAX_PUSH_BYTES` | `25165824` | Largest total payload in one API push. |
| `TF_PUBLISH_RETRY_SECONDS` | `60` | Wait between publish retries, and the interval the UI quotes. |
| `TF_QUEUE_ORPHAN_GRACE` | `120` | Seconds before an unowned queued run counts as an orphan. |
| `TF_QUEUE_STALE_MINUTES` | `30` | Minutes a queued run may wait before it is reported stale. |
| `TF_QUEUE_SWEEP_SECONDS` | `300` | Interval of the background orphan reaper; `0` disables it. |
| `TF_RUN_RETENTION_DAYS` | `14` | Default age at which finished runs may be purged. |
| `TF_BATCH_SCREENSHOTS` | `failure` | Default batch screenshot mode: `none`, `failure`, `changes`, `all`. |
| `TF_BATCH_MAX_RECORDINGS` | `40` | Most recordings one batch may claim. |
| `TF_MAX_BATCHES` | `1` | Batches executing at once; a second one is refused with 409. |
| `TF_BATCH_SHARE_SESSION` | `1` | `0` gives every recording a fresh context instead of sharing per origin. |
| `TF_BATCH_RETRY_TRANSIENT` | `1` | `0` disables the single escalated retry of a timeout or network error. |
| `TF_BATCH_MAX_RSS_MB` | `420` | RSS that triggers a browser relaunch between recordings. |
| `TF_BATCH_LOG_ENTRIES` | `200` | Step entries kept in memory per batch run. |
| `TF_BATCH_REPORTS` | `40` | Batch reports kept on disk. |
| `TF_PACER_SAFETY` | `4` | Multiplier from a step's observed duration to its timeout budget. |
| `TF_LOGS_DIR` | `<repo>/logs` | Where the local log reader looks. |
| `TF_MAX_LOG_FILE_BYTES` | `262144` | Largest tail the local log reader returns. |
| `TF_MAX_LOG_INDEX_ROWS` | `40` | Most runs the local log index lists. |
| `ANTHROPIC_API_KEY` | unset | Enables the optional AI insights of the QA agent hub. Rules run without it. |
| `TF_AGENT_AI` | `1` | `0` turns the QA agent AI insights off even when a key is set. |
| `TF_AGENT_AI_MODEL` | `claude-sonnet-4-5` | Model used for QA agent insights. |
| `TF_AGENT_AI_URL` | Anthropic Messages API | Endpoint for QA agent insights (proxy or test double). |
| `TF_AGENT_AI_TIMEOUT` | `30` | Seconds before a QA agent AI call is abandoned. |
| `TF_AGENT_AI_PROVIDER` | unset | `ollama` uses a local model for QA agent insights; `anthropic` uses the cloud model. |
| `TF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server used when the provider is `ollama`. |
| `TF_OLLAMA_MODEL` | `llama3.1` | Ollama model used for QA agent insights. |
| `TF_EXPLORER_BROWSER` | `0` | `1` runs the explorer in rendered-browser mode (console, page errors, failed requests). `0` is the standard-library HTTP mode. |
| `TF_EXPLORER_ALLOW_PRIVATE` | `0` | `1` allows scanning loopback/private addresses. Keep `0` on any shared instance: otherwise the explorer is an SSRF path. |

## Testing
The harness runs the unit suite and the library scenarios. Library scenarios are the recordings and projects in `library/`, executed through the same recorder and runner the dashboard uses. From the repository root:

```bash
python -m tests.harness
```

The market comparison is gated separately and needs no browser: `python -m tools.market_audit` exits non-zero when a TestForge claim loses its evidence, and CI runs it after the unit suite. `tests/test_market.py` covers the matrix, audit, reports, step catalogue, impact map, explorer, JUnit output, MCP tools, dashboard wiring and no-downgrade checks.

Scenarios are documented in `tests/SCENARIOS.md`. Each run writes a timestamped report under `logs/`, and `logs/index.md` lists them. Chromium is required for the browser scenarios (`LB-REC-*`, `LB-RUN-*`, `LB-BAT-01`); unit tests run without it, and the batch executor is unit-tested against an in-memory browser double.

No test or harness run ever publishes to GitHub. The API publisher is exercised against a fake transport, and the harness strips `TF_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and `TF_GIT_REMOTE` from the server environment it starts.

## Demo tab
The **Demo** tab (🧪, first in the bar) is the no-setup route into the product. Its built-in sample
and synthetic domain fixtures are served locally. It also lists optional public shopping, grocery and
fashion websites as external learning targets; no external site is embedded or contacted until a user
opens one or chooses it for an AI recording. Never use a real account, submit an order or enter payment data.

1. **Select the sample project** — `TestForge Testing Tool` ships in `library/projects/qa-sample-app`,
   points at `/demo.html` and already defines `{{user}} = Ada`. If an instance was started against an
   empty `TF_LIBRARY_DIR` the tab offers to create it, as an ordinary project save.
2. **Replay a ready-made test** — `Hello literal` types a fixed name; `Hello variable` types `{{user}}`,
   which demonstrates substitution at replay time. RUN queues the recording and jumps to **Runs**.
3. **Record your own** — shortcuts into the AI Recording and Library tabs. A blank URL opens
   `/demo.html` on the server browser; the public learning URLs can also prefill the recording URL.
4. **Check the evidence** — shortcuts into Runs and Artifacts.

The sample app and chosen domain fixture are also embedded in the tab, rendered by *your* browser so
you can see what the steps do. That costs the service nothing: no server-side browser, no screencast
and no run budget. The six public retailer links are optional examples, not stable fixtures: target
sites can change, require region/cookie choices or block automation. The five synthetic packs
(commerce, banking, insurance, HRMS, travel) remain deterministic and can be saved through
**Library → UPLOAD TEST SCRIPTS → synthetic domain pack**, which tests before it publishes.

## Record and run
Open a project, then the AI Recording tab. Leave the page blank to open the built-in TestForge Testing Tool, choose a public shopping/grocery/fashion learning URL, or enter an authorized application URL. The remote Chromium picture is polled over HTTP (so it still works when a proxy drops websockets). Click the picture to click in the browser, type with the text box, then SAVE. In the library, RUN replays those steps and the Runs tab shows the live browser plus each step. External store pages can change, require region/cookie choices, or block automation; use permitted public flows only and never submit real purchases or payment details.

The server needs Chromium: `playwright install chromium`. Docker already does this. If the browser cannot start, the record screen shows the error instead of a blank canvas.

## Deployment and manual QA

See [docs/deployment-guide.md](docs/deployment-guide.md) for the full deployment procedure:
Docker Compose, SQLite/PostgreSQL structure and provisioning, the complete 62-variable environment
reference, publishing modes, verification gates, and a Claude Code prompt library. Read its section 2
before exposing a deployment to any network.

For the optional Azure / Azure DevOps topology (image registry, VM runtime, repository mirror) see
[docs/azure-devops-deployment-guide.md](docs/azure-devops-deployment-guide.md).

- Open `/api/health` on the deployed service to verify the app and database are ready. On Render, the response includes the `RENDER_GIT_COMMIT` revision.
- Open `/api/diagnostics` when something fails. It reports the database dialect, the schema repairs applied at boot, the live columns of every table, and the result of a test project insert that is rolled back.
- Create a project from **Projects**. It is written to `library/` and a push is attempted on the checked-out Git branch. GitHub status verifies the remote SHA; a failed push is not presented as published.
- Open the **GitHub** tab to see the repository catalog and publish any unpublished library changes. It shows the publish mode (`GIT CHECKOUT`, `GITHUB API` or `LOCAL ONLY`), the branch and revision that mode can actually know, and *Why the panel reads this way* when something is missing. `GET /api/library` returns the same detail as `publish_mode`, `publish_disabled_reason`, `api_publish`, `last_api_push`, `git_state`, `git_note`, `git_error`, `status_reason`, `projects_on_disk` and `library_dir`.
- Set `TF_GITHUB_REPO` and `TF_GITHUB_TOKEN` on a container deployment to make the GitHub tab publish without a checkout; *WHAT WOULD THIS PUBLISH?* then diffs this instance against the remote branch before anything is committed.
- **CHECK TOKEN PERMISSIONS** in the GitHub tab asks GitHub what the configured token may do (`GET /api/github/access`, one read of `GET /repos/{owner}/{name}`) and answers *can push* / *read-only* / *cannot see the repository* with the fix for each. Use it after rotating `TF_GITHUB_TOKEN` instead of waiting for the next save.

### When a save says "the push did not complete"
A save ends in one of four ways, and the message in the record tab says which:

| Message | What it means | What to do |
| --- | --- | --- |
| `Published to owner/name@branch` | The push was verified against the remote branch. | Nothing. |
| `…did not complete (…) and will be retried in 60s` | Transient: network, branch conflict, rate limit. | Nothing; the retry loop is running. |
| `…was refused (…) and no retry will run` | GitHub rejected the token itself (401/403), or `git push` was denied. No wait will change the answer. | Fix the token (below), then publish again. |
| `Saved locally. GitHub publishing is disabled…` | No checkout and no API configuration. | Set `TF_GITHUB_REPO` + `TF_GITHUB_TOKEN`, or mount a checkout. |

`403 Resource not accessible by personal access token` on `POST /repos/{owner}/{name}/git/blobs` is the common one, and it is a *permission*, not an outage:

- **Fine-grained token** (`github_pat_…`): open Settings → Developer settings → Personal access tokens → Fine-grained, set *Repository access* to this repository, and give **Contents: Read and write**. *Read and write* is the whole point — Contents: read-only reads fine and refuses every blob upload, which is exactly this error. Metadata: read is enough.
- **Classic token** (`ghp_…`): it needs the **`repo`** scope. `public_repo` does not cover a private repository, and a token with no scopes covers nothing.
- **Organization with SSO**: click *Configure SSO* on the token and approve it for the owning organization.
- **GitHub App / installation token** (`ghs_`/`ghu_`): the app needs Contents: Read and write and must be installed on the repository.

Then redeploy with the new value of `TF_GITHUB_TOKEN` and use **CHECK TOKEN PERMISSIONS** to confirm it before recording again.

## Master setup (region, time zone, dates, language)
The **Settings** tab (⚙️) holds the instance-wide defaults. They are stored once on the server (`GET`/`PUT /api/settings`), not per project, so every browser and project shows the same dates. Choose a **region** to fill the time zone, date format, time format and language in one step; each field can then be changed on its own. Options: date formats `YYYY-MM-DD`, `DD/MM/YYYY`, `MM/DD/YYYY`, `DD.MM.YYYY`, `DD-MM-YYYY`, `YYYY/MM/DD`, `D MMM YYYY`; time 24-hour or 12-hour; languages English, Deutsch, Français and Español. A live preview shows the result before you save. The Settings tab also holds the default Windows version for new desktop recordings and the default screen zoom for the Windows screens.

Dates and times across the dashboard are shown through one formatter (`app/static/tf-settings.js`). Stored values stay UTC, and the formatter converts them to the chosen zone when displayed. Only the navigation and the Master setup screen are translated so far; everything else stays in English. PDF report times stay in UTC and are labelled `(UTC)`.

## Dev & Deploy tab
The **Dev & Deploy** tab (🛠️) is the hand-over pack for the in-house development team: the codebase
map, local setup, SQL Server on Azure end to end, deployment, CI/CD, the copy-paste SQL scripts and the
AI prompt library. It is served from this instance (`GET /api/dev/kit`,
`GET /api/docs/dev-kit/{slug}`), so the team reads the same text the repository holds. Every document
is downloadable on its own (`/api/docs/dev-kit/{slug}/download`) or as one archive
(`/api/docs/dev-kit/bundle`).

The guides live in `docs/` and are registered in `app/dev_kit.py`; add one there and it appears in the
tab, in the bundle and in the download endpoint. The prompt library is different: it is data in
`app/dev_prompts.py`, rendered by the tab and generated into `ai-prompt-library.md` on demand, so the
file you email cannot drift from the dashboard. Nothing in either is a credential: the guides use
placeholders and Key Vault references.

For a SQL Server database the ODBC driver is **not** in `requirements.txt` — install
`requirements-sqlserver.txt` in addition, and see
[docs/dev-azure-sql-server.md](docs/dev-azure-sql-server.md) for the connection string, the pooling
arithmetic and the two schema repairs that are gated to PostgreSQL.

## Windows desktop: install an application before recording it
The **Windows Desktop** tab records an application that is already on the target machine, so the
**INSTALL A DESKTOP APPLICATION BEFORE RECORDING** panel comes first: name the app, choose the
installer type (`msi`, `exe`, `msix`, `winget`, `choco`, `portable`), point at the installer on the
Windows target (an absolute path or an https URL the agent can download, or a package id), then
**INSTALL & REGISTER APPLICATION**. The application is registered, selected in the list below, and
**VERIFY IT OPENS** launches and closes it once before you record. The recording's session state
carries the install receipt, so the evidence says what was installed and when.

The install command is built from a template per installer kind, or checked against an allowlist when
you supply one, and shell metacharacters are rejected: the dashboard host never runs an installer and
never touches a Windows file system. With `TF_WINDOWS_AGENT_URL` and `TF_WINDOWS_AGENT_TOKEN` the
command goes to the authenticated agent over HTTPS and the agent's own exit code decides the status.
Without an agent the install is validated and registered against the deterministic simulator and is
labelled **SIMULATED** everywhere it appears. Install records live under
`$TF_ARTIFACTS/roadmap/desktop-installs.json` (at most 20, `GET/POST /api/desktop/installs`).

## Windows desktop: screen size and variables
The live Windows screen during recording and the screen in **Runs** share one viewer. **Fit** shows the whole screen, **−/+** steps through 50%–300%, **100%** shows real size, and **Enlarge** (or Esc to close) fills the window. Scroll inside the viewer to move around a zoomed screen.

When you press STOP on a Windows recording, TestForge reviews the values you typed into fields (text entry and list selections). It proposes a variable for each one, for example `customer_name`, with the typed value shown. Untick a value to leave it as a literal, rename it, mark it secret or choose its category, then press **APPLY TO RECORDING**. **KEEP LITERALS** saves the steps unchanged. Applying rewrites those steps to `{{customer_name}}` and the saved execution plan, so the next desktop run uses the variable. Checkboxes and clicks are not turned into variables. This is the same review the browser AI recording shows.

## Database migrations
`app/db.py` runs an idempotent schema check at startup. It creates missing tables, adds columns that the models declare but an older table lacks (backfilling existing rows), and relaxes `NOT NULL` on legacy columns the models no longer write. `create_all()` alone only creates missing tables, so a database created by an older revision keeps its original columns and every write to it fails until this migration runs.

The check is dialect-aware where it has to be: a new `NOT NULL` boolean column is backfilled with `false` on PostgreSQL and SQLite and with `0` on SQL Server, whose `BIT` type rejects the word `false`. The two legacy repairs that relax `NOT NULL` on columns the models no longer write, and widen a legacy `VARCHAR`, run on PostgreSQL only — on SQL Server, widen such a column by hand (see [docs/dev-azure-sql-server.md](docs/dev-azure-sql-server.md) §6.3).

## QA acceptance criteria and exports
See [docs/qa-automation-acceptance.md](docs/qa-automation-acceptance.md) for the ten testable recording, execution and GitHub acceptance criteria and implementation limitations. The Library editor can modify saved steps. Recording resources include Playwright Python, Jenkins, Gherkin and Azure/Jira/TestComplete CSV/XLSX templates; proprietary vendor imports require mapping. Recorder PNGs remain alongside recorded steps; execution evidence is a reusable low-quality JPEG per step and optional video is WebM.

## Ordered scenarios, encrypted inputs and PDF reports

Select a project → **Library → High-level scenarios → Execution plans** to link
existing recordings (for example, login then logout) in exact order and one shared
session. Review per-recording inputs before execution; project variables and
parameters remain persistent. Password fields support authenticated encryption at
rest with an operator-provisioned key. Completed single and batch runs offer cached
PDFs from the same low-quality JPEG captured once per step.

**Encryption is operator-provisioned and the dashboard says so up front.** `TF_VARIABLE_ENCRYPTION_KEYS`
must hold a Fernet keyring before an encrypted value can be saved; `render.yaml` declares it
`sync: false`, so a new Render service starts without one. `GET /api/health` reports
`encryption: {configured, ready, keys, reason}` — no key material — and the **Variables** tab reads
that when it opens: while the keyring is missing or unusable it prints which variable to set, the
command that generates a key, where to put it and the `new,old` rotation syntax, and it disables
**Encrypt at rest** instead of taking a password into a field that will reject it. Plaintext
variables keep saving normally. Nothing is generated automatically — a key invented at boot would
change on the next deploy and permanently strand every value sealed with the old one.

PDFs can be uploaded as GitHub release assets with 48-hour retention and oldest-first
hourly maintenance; only summaries/links enter Git. Remote publication is opt-in,
requires host configuration, and cannot clean up while the application is asleep or
offline. No paid worker/CI scheduler was added; screenshot/PDF work is not zero-cost.

See [setup, security limits, input precedence and retention operations](docs/scenarios-secrets-and-pdf-reports.md).
