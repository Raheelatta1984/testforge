# TestForge AI Tracking
- **Database:** Permanently stored in Neon PostgreSQL / SQLite (Configured in config.py).
- **Architecture:** 11-tab Persistent Dashboard (Optimized for Android 12). The bar is icon-only to fit a phone; every tab carries its name as a hover/long-press title and an accessible label.
- **Demo tab:** the first tab. A guided walkthrough over the built-in sample app and the five synthetic domain fixtures, so recording, replay and evidence can be proved without pointing TestForge at a real application. See below.
- **Azure guide:** the **Azure tab** renders `docs/azure-devops-deployment-guide.md` inside the app (`GET /api/docs/azure-guide`) as checkable, follow-along steps for end users.
- **Local helpers (no paid AI calls):** Browser-dependent voice input, deterministic text cleanup, and recording-variable association tagging. Autonomous AI execution is not connected.
- **Tree Hierarchy:** Parent Group -> Sub-recording chunk organization.
- **Entities (companies):** a company/entity is created before its projects and every project belongs to one; the registry is `library/entities.json`, the Projects tab groups and filters by it, and a project written before entities existed sits in an `Unassigned` bucket that can be moved at any time.
- **Previous recordings tree (Record tab):** every recording in the library, grouped by project and nested by parent recording, with **RESUME** (open the old recording and append) and **RUN** (execute it as a run). **SCAN LIBRARY** / **RESCUE** list and re-export recordings that fell out of the catalog or exist only in the database, so nothing is lost.
- **Run a recording inside a recording:** while a session is live, `POST /api/recordings/{id}/replay` executes another saved recording in that same browser; its steps are added to the recording being made (or run without being written, at the operator's choice).
- **Repeated steps:** Consecutive identical actions are recorded as one step carrying a repeat count, and replay expands it back into N actions.
- **Execution:** Optional step-by-step live view in the Runs tab, reusing the evidence JPEG without a second screencast loop.
- **Batch execution:** One browser at a time replays recordings, with learned timeouts and one reusable low-quality screenshot per step (`POST /api/runs/batch`).
- **Queue hygiene:** Orphaned, stale and duplicate queue entries are reported and can be cleared; a restart no longer leaves runs pending forever.
- **Publishing:** `library/` is committed to one branch, by `git push` from a checkout or through the GitHub REST API when the deployment has none.
- **Maintenance:** Check `/api/library` for the publish mode, live remote verification and retry status. Failed pushes remain local and are retried; do not reset a branch to fix sync issues.

## Script upload, assertions and lower-overhead execution

See [Script import and sequential automation additions](docs/script-import-and-actions.md)
for the implemented workflow, supported format subsets, action schema, resource limits,
access-token setup, and the roadmap items still outstanding.

- **Library → UPLOAD TEST SCRIPTS:** preview CSV/TSV, XLSX, canonical JSON, supported
  Playwright Python/JS/TS, BDD/Cucumber phrases, or a restricted Jenkins + script bundle.
  Uploaded source is never executed; unsupported statements block saving.
  Use **TEST BEFORE PUBLISHING** to run a private draft; the dashboard enables
  publication only after a passing test of the reviewed execution inputs.
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

The **Runs** tab has a *Live browser window* switch. Off, a run replays headless with no screencast and no per-frame CPU; on, the browser picture streams while the tab is open. Either way the run result is identical. Even when the switch is on, no frame is rendered while no client is attached.

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

## Testing
The harness runs the unit suite and the library scenarios. Library scenarios are the recordings and projects in `library/`, executed through the same recorder and runner the dashboard uses. From the repository root:

```bash
python -m tests.harness
```

Scenarios are documented in `tests/SCENARIOS.md`. Each run writes a timestamped report under `logs/`, and `logs/index.md` lists them. Chromium is required for the browser scenarios (`LB-REC-*`, `LB-RUN-*`, `LB-BAT-01`); unit tests run without it, and the batch executor is unit-tested against an in-memory browser double.

No test or harness run ever publishes to GitHub. The API publisher is exercised against a fake transport, and the harness strips `TF_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and `TF_GIT_REMOTE` from the server environment it starts.

## Demo tab
The **Demo** tab (🧪, first in the bar) is the no-setup route into the product. Everything it shows
is a static file served by this instance: nothing signs in anywhere, reaches an external site, or
touches a real account, payment, policy, employee or booking.

1. **Select the sample project** — `TestForge Sample App` ships in `library/projects/qa-sample-app`,
   points at `/demo.html` and already defines `{{user}} = Ada`. If an instance was started against an
   empty `TF_LIBRARY_DIR` the tab offers to create it, as an ordinary project save.
2. **Replay a ready-made test** — `Hello literal` types a fixed name; `Hello variable` types `{{user}}`,
   which demonstrates substitution at replay time. RUN queues the recording and jumps to **Runs**.
3. **Record your own** — shortcuts into the Record and Library tabs. A blank URL in Record opens
   `/demo.html` on the server browser.
4. **Check the evidence** — shortcuts into Runs and Artifacts.

The sample app and the chosen domain fixture are also embedded in the tab, rendered by *your* browser
so you can see what the steps do. That costs the service nothing: no server-side browser, no
screencast and no run budget. The five synthetic packs (commerce, banking, insurance, HRMS, travel)
are listed with their positive/negative dataset rows; saving one as a real test is still the explicit
**Library → UPLOAD TEST SCRIPTS → synthetic domain pack** path, which tests before it publishes.

## Record and run
Open a project, then the record tab. Leave the page blank to open the built-in sample app, or enter the application URL. The remote Chromium picture is polled over HTTP (so it still works when a proxy drops websockets). Click the picture to click in the browser, type with the text box, then SAVE. In the library, RUN replays those steps and the Runs tab shows the live browser plus each step.

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

## Database migrations
`app/db.py` runs an idempotent schema check at startup. It creates missing tables, adds columns that the models declare but an older table lacks (backfilling existing rows), and relaxes `NOT NULL` on legacy columns the models no longer write. `create_all()` alone only creates missing tables, so a database created by an older revision keeps its original columns and every write to it fails until this migration runs.

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
