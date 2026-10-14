# TestForge scenario catalog

This is the source of truth for the recording and replay harness. Every ID below is executed by `python -m tests.harness` and written, with the date and time, to `logs/<YYYYMMDD-HHMMSS>/`.

Unit scenarios call the TestForge library in-process and do not open a browser. Library scenarios go through a live TestForge server — the same recorder, runner, and HTTP API the dashboard uses. There is no second automation stack.

## How to run

From the repository root, with the project virtualenv active:

```bash
python -m tests.harness
```

Chromium is required for `LB-REC-*` and `LB-RUN-*`. The harness uses `TF_CHROMIUM_PATH` when set, otherwise Playwright's bundled browser, otherwise `/tmp/cr/chromium` if that file exists. Without a browser those scenarios are skipped and the unit suite still runs. `--require-browser` turns a missing browser into a failure.

Add a check by editing `tests/test_unit.py` (ID in the method name, `test_UT_AREA_NN_...`) or `tests/scenarios.json`, then add the same ID to this file. `UT-DOC-01` fails the run if an executable ID is missing here.

## Environment

| Item | Value |
| --- | --- |
| Application | TestForge dashboard and API |
| Sample app | `/demo.html` (name field at 80,140, Continue at 80,200, viewport 1280x800) |
| Unit database | temporary SQLite, discarded after the process exits |
| Library database | temporary SQLite, hydrated from a copy of `library/` |
| Logs | `logs/<timestamp>/{harness,unit,scenarios,server}.log` plus `results.json` and `results.md` |

## Unit scenarios

Workflow regressions: `UT-FLOW-01` asserts consecutive identical actions collapse into one step carrying a repeat count while distinct actions keep their own row and order; `UT-FLOW-02` checks idempotent save, editing and screenshot access; `UT-FLOW-03` checks generated formats and spreadsheet injection protection; `UT-FLOW-04` checks placeholders are typed as resolved values but stored as placeholders; `UT-FLOW-05` checks new/existing variable capture preserves prior steps; `UT-FLOW-06` checks compact PNG output.


| ID | Scenario | Expected result |
| --- | --- | --- |
| UT-FLOW-01 | Merged repeats, ordered steps | Three identical clicks become one row with `repeat=3` and one PNG; a different action still gets the next order. |
| UT-FLOW-02 | Save and edit | Stop without a live session succeeds; edited input persists and screenshot access is scoped. |
| UT-FLOW-03 | Export formats | CSV/XLSX, Jenkins, BDD and Playwright templates are created; formula-like inputs are escaped. |
| UT-FLOW-04 | Runtime variables | Recorder types resolved values and preserves the placeholder for replay. |
| UT-FLOW-05 | Capture variable | New and existing variables take the focused input value without losing recording steps. |
| UT-FLOW-06 | Compact PNG | Optimized screenshots remain valid PNG and never grow. |
| UT-FLOW-07 | Merged repeat expansion | A step stored with `repeat=3` expands to three executions during replay. |
| UT-FLOW-08 | Capture is never merged | Two `save_variable` steps on the same field keep separate rows, because each captures a different value. |
| UT-FLOW-09 | Compress an existing recording | `POST /api/recordings/{id}/steps/compress` collapses four identical clicks into one step with `repeat=4`. |
| UT-FLOW-10 | Run a saved recording inside a live session | `RecorderSession.replay()` drives the open browser with another recording's steps and writes each executed action into the recording being made. |
| UT-FLOW-11 | A failed live replay reports its step | A step that raises stops the replay, reports its position and error, and only the steps that ran are recorded. |
| UT-FLOW-12 | A closed session refuses a replay | A session that has been closed raises instead of running steps. |
| UT-FLOW-13 | Fast recorder navigation survives external page timeouts | `RecorderSession` navigates with `wait_until="commit"`, halts hung loads with `window.stop()`, captures the first frame before going live, and never fails with `Page.goto: Timeout 30000ms exceeded`. |
| UT-BOOT-01 | Import the recorder and the executor | Both modules load. This is the regression where `IS_TERMUX`, `interpolate`, and `CICD_INTERVAL` were missing and both features were offline. |
| UT-BOOT-02 | Config contract | `IS_TERMUX` is a bool, `CICD_INTERVAL` is a positive int, and `interpolate` is `apply_variables`. |
| UT-BOOT-03 | Run model columns | `recording_id`, `execution_log`, `progress_pct`, and `rog_monitor_log` exist. `target_id` and `rog_investigation` do not. |
| UT-VAR-01 | Known `{{keys}}` | `Hello {{user}} from {{env}}` becomes `Hello Ada from qa`. |
| UT-VAR-02 | Unknown key | `{{missing}}` is left unchanged. |
| UT-VAR-03 | Empty text | `None` and `""` are not rewritten. |
| UT-VAR-04 | Inner whitespace | `{{ user }}` resolves. |
| UT-URL-01 | Blank start URL | Opens `http://127.0.0.1:$PORT/demo.html`. |
| UT-URL-02 | Relative path | `/demo.html` stays on this server. |
| UT-URL-03 | Scheme-less host | `example.com/login` becomes `https://example.com/login`. |
| UT-URL-04 | External URL | An `https://` URL is stored unchanged. |
| UT-URL-05 | Non-http scheme | `file://` is rejected with HTTP 422. |
| UT-URL-06 | Preview host | A URL whose host is the dashboard host is rewritten to loopback, query included. |
| UT-URL-07 | Other local port | `http://127.0.0.1:3000/app` and `http://localhost:5173/` are not rewritten onto PORT. |
| UT-URL-08 | Query on this server | `http://127.0.0.1:$PORT/demo.html?name=Ada` keeps the query. |
| UT-BRW-01 | Chromium library path | `TF_CHROMIUM_LIBS` is passed only to the browser process. The server's `LD_LIBRARY_PATH` is unchanged, so sqlite3 keeps working. |
| UT-BRW-02 | Missing binary | A `TF_CHROMIUM_PATH` that is not a file raises `RuntimeError`. |
| UT-BRW-03 | Install hint | A Playwright "executable doesn't exist" error becomes the `playwright install chromium` message, without the internal path. |
| UT-BRW-04 | Video override | A stand-in binary disables Playwright video so a video failure cannot fail the run. |
| UT-BRW-05 | Extra Chromium flags | `TF_CHROMIUM_ARGS` is appended to the launch arguments and is empty by default; the extra flag reaches only the browser process. |
| UT-SEC-01 | Credential redaction | `postgresql://user:password@host/db` loses the password before it can reach the UI. |
| UT-SEC-02 | Configuration surfaces carry no token | `describe()`, `status()` and both `publish_outcome()` payloads serialize without the token even when `TF_GITHUB_TOKEN` is configured and working. |
| UT-SEC-03 | Hostile GitHub errors are scrubbed at the transport | An HTTP error body or URL error quoting the `Authorization` header, the live token, or an unknown `github_pat_…` becomes `***` before `GitHubAPIError` leaves `request()`. |
| UT-SEC-04 | The bearer token travels only to the configured API host | `head_sha()` sends exactly one request to `https://api.github.com/...` carrying `Authorization: Bearer <token>`; `TF_GITHUB_API` relocates the whole conversation, token included, and nothing is sent anywhere else. |
| UT-SEC-05 | Publishing needs both gates | A named repository without a token is `disabled` with a reason naming `TF_GITHUB_TOKEN`; a `GITHUB_TOKEN` with no named repository is refused with a reason naming `TF_GITHUB_REPO`. |
| UT-SEC-06 | A push commits only `library/` | Every tree entry staged by the API publisher starts with `library/`, a sibling file next to the library root is never staged, and the ref update is fast-forward (`force=False`). |
| UT-SEC-07 | Dry run writes nothing | With changes pending, `publish(dry_run=True)` reports `pending` and the transport records GET reads only: no blob, tree, commit or ref call. |
| UT-SEC-08 | Request paths cannot be altered from configuration | A branch carrying `..`, `?`, `#`, `%`, control characters or a `.lock` ending leaves `describe()` unavailable and `GitHubClient` refuses before any request is built; padded names are stripped to the real branch before use, legal names (`main`, `release/2026.10`) still work, and a tree SHA that is not a git object id is refused. |
| UT-SEC-09 | The harness strips publishing credentials | `tests/harness.py` removes `TF_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`, `TF_GITHUB_REPO` and `TF_GIT_REMOTE` from the server environment before `subprocess.Popen`, so a developer shell cannot publish from a test run. |
| UT-SEC-10 | The HTTP surface never echoes the token | `/api/health`, `/api/diagnostics`, `/api/library` and `/api/library/publish/plan` respond without the configured token or an unknown `github_pat_…` quoted by a hostile GitHub error body, while the redaction marker `***` proves the scrub ran. |
| UT-SEC-11 | Failed-push errors are scrubbed before the banner | `publish_outcome(False, error)` keeps the useful push error but replaces credential-shaped text with `***` in `publish_message` and `publish_error`. |
| UT-REP-01 | Navigate interpolation | `goto` receives the interpolated URL and waits for `domcontentloaded`. |
| UT-REP-02 | Click by selector | A unique selector is clicked. Coordinates are not used. |
| UT-REP-03 | Click fallback | A missing selector falls back to the recorded x,y. |
| UT-REP-04 | Click with no target | No selector and no coordinates raises. |
| UT-REP-05 | Type interpolation | `{{user}}` is typed after the field is focused. The stored step still holds the template. |
| UT-REP-06 | Fill | `fill` replaces the field instead of appending. |
| UT-REP-07 | Key press | `press` sends `Enter`. |
| UT-REP-08 | Unsupported action | An unknown action raises rather than being reported as passed. |
| UT-REP-09 | Navigate without a URL | Raises before `goto`. |
| UT-REP-10 | Relative navigate URL | `/demo.html` is opened on `127.0.0.1:$PORT`. `http://127.0.0.1:3000/app` is not rewritten. |
| UT-REP-11 | Resilient replay navigation | A `navigate` step whose page already committed recovers cleanly when `domcontentloaded` times out on heavy external pages. |
| UT-LIB-01 | Create a project | `project.json` is written and the list comes from that file. Publish is off in unit tests. |
| UT-LIB-02 | Database-only project | A row that is not in `library/` is not listed, and materialize removes it. |
| UT-LIB-03 | Create a recording | `recording.json` and `resources/Jenkinsfile` are written. |
| UT-LIB-04 | Variable roundtrip | Create, update, and delete change `variables.json` and only that file. |
| UT-LIB-05 | Path escape | An id of `../etc` is rejected. |
| UT-LIB-06 | Publish | A local git remote receives only `library/` files. `KEEP.txt` is untouched. |
| UT-LIB-07 | Failed push | The project is not listed and the database row is removed. |
| UT-LIB-08 | File-only recording | A recording that exists only as JSON is loaded into the database for replay. |
| UT-LIB-09 | Rescue a database-only recording | A recording whose files are gone is reported as database-only by `library_health()` and re-exported by `rescue_library(apply=True)` before materialize could drop it. |
| UT-ENT-01 | An entity-less project is still tied to one | A project created without an entity reports the `unassigned` bucket, so no project is ever entity-less. |
| UT-ENT-02 | Create the entity, then the project | An entity is created with code and industry, the project names it, and an unknown `entity_id` is refused with `NotFound`. |
| UT-ENT-03 | Entity names are unique | A duplicate name (case-insensitive), a blank name, the reserved `Unassigned` name and an over-long name are all refused. |
| UT-ENT-04 | Rename and remove keep projects tied | A rename rewrites the member `project.json`; deleting an entity with members needs `reassign_to`, the members move, and the fallback bucket cannot be renamed or deleted. |
| UT-ENT-05 | The registry lives in the library tree | `library/entities.json` is written with `source=repository`, and a new entity is listed before the fallback bucket. |
| UT-RUN-01 | Recording with no steps | Status becomes `error`, the monitor log says no steps, and no browser is launched. |
| UT-RUN-02 | Run whose recording was deleted | Status becomes `error` and the event says `Recording not found`. |
| UT-RUN-03 | Preview with no viewer | While `should_capture` reports zero viewers the loop renders nothing; capture resumes once somebody watches. |
| UT-RUN-04 | Live window toggle reaches the queue | `POST /api/runs` with `display_window=false` records the choice on the run; omitting it defaults to on. |
| UT-RUN-05 | Window off, replay on | `execute_run(display_window=False)` passes, requests no JPEG frame, pushes no frame to the dashboard, and every step PNG is on disk before the run finishes. |
| UT-RUN-06 | Window on with a viewer | `execute_run(display_window=True)` captures JPEG frames while a viewer is attached. |
| UT-RUN-07 | Window on, nobody watching | With a viewer count of zero the run passes and renders no frames. |
| UT-RUN-08 | Repeat count on replay | A step stored with `repeat_count=3` produces three clicks. |
| UT-RUN-09 | Live frames stay synchronized to execution | With a viewer attached, a live JPEG is streamed during a slow step, tagged with its step index/order, and the exact post-step image arrives before that step's completion event. |
| UT-RUN-10 | Toggle the active run's live window | The Runs-tab switch can enable/disable preview for a queued or running single recording without requeueing it; finished and batch runs remain unchanged. |
| UT-RUN-11 | Runs monitor stays discoverable | The live-window switch and compact browser/step monitor appear before queue hygiene and batch controls; step/frame labels make synchronization visible. |
| UT-SAVE-01 | Slow export on save | `stop()` returns cleanly when the debounced export outlives its flush budget, the browser is closed, and the export is left to finish rather than cancelled mid-write. This is the regression that raised `AttributeError: 'NoneType' object has no attribute 'cancel'`. |
| UT-SAVE-02 | Export raises on save | `stop()` still closes the browser and records the failure in the session save log. |
| UT-PUB-01 | Disabled publishing promises no retry | `publish_outcome(False)` returns `publish_state=local-only`, `retry_scheduled=false`, `retry_in_seconds=null`, and a message that says publishing is disabled. This is the regression where the record tab showed "GitHub push will retry in 1 minute: GitHub publishing is disabled". |
| UT-PUB-02 | Failed push states the retry | With a checkout, a failed push returns `retry-pending`, the configured `TF_PUBLISH_RETRY_SECONDS` wait, and the git error verbatim; a successful push returns `published` with no retry. |
| UT-PUB-03 | API publisher commits and verifies | With no checkout, `github_api.publish()` creates one blob per file, a tree, a commit and a ref update against a fake transport, then reads the branch back before reporting `published`. Blob SHAs match what git would compute. |
| UT-PUB-04 | API publisher no-op when synced | A second publish with an unchanged library creates no blob and no commit and reports `state=synced`; a dry run publishes nothing. |
| UT-PUB-05 | Push bounds and token redaction | A file larger than `TF_GITHUB_MAX_FILE_BYTES` is skipped with a reason, and neither an `Authorization:` header nor a `ghp_…` token survives `_safe()`. |
| UT-PUB-06 | API mode is reported, not dashes | With `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and a token, `status()` reports `publish_mode=api`, `git_state=api`, the configured branch and repository URL, a `git_note`, and no git-shaped `status_reason`. |
| UT-PUB-07 | A stray token publishes nothing | A `GITHUB_TOKEN` with no configured repository leaves `publish_mode=disabled`: API publishing is opt-in, so a CI runner cannot push anywhere by accident. |
| UT-PUB-08 | A refused write says so and promises no retry | A `POST` answered with `403 Resource not accessible by personal access token` raises `GitHubAPIError` with `status=403`, `permanent=true` and a hint naming the missing Contents permission; the save comes back `publish_state=refused`, `retry_scheduled=false`, a message that says *refused* instead of *will be retried*, and `publish_blocked()` true. |
| UT-PUB-09 | A rate limit is still worth retrying | A `403` whose body says the API rate limit was exceeded stays `permanent=false`, so the save keeps `retry-pending` and its retry interval: backoff and refusal are the same status and must not be conflated. |
| UT-PUB-10 | Access report names the missing permission | `github_api.access_report()` reads `GET /repos/{slug}` once: `pull=true, push=false` reports `state=read-only`, `can_write=false` and a fix naming Contents for a fine-grained token; `push=true` reports `state=ok`, the classic token's scopes from `X-OAuth-Scopes`, and no fix. |
| UT-PUB-11 | The diagnosis endpoint hides the token | `GET /api/github/access?force=1` answers 200 with `can_write=false` and the fix, and the configured `github_pat_…` value appears nowhere in the body. |
| UT-PUB-12 | A refused `git push` is not retried either | A checkout has no status code, only git prose: `The requested URL returned error: 403` is recognised as a refusal, so the save is `publish_state=refused` with a classic-token hint naming the `repo` scope instead of a retry countdown. |
| UT-GIT-01 | No git checkout | `status()` reports `git_state=no-checkout` with a reason instead of a silent `branch: null`, and `remote_library_status()` explains the missing checkout. |
| UT-GIT-02 | Library disk report | `status()` reports `projects_on_disk`, `project_dirs`, `catalog_present` and `library_dir`. |
| UT-MEM-01 | Browser budget serialises | With one permit, a second browser waits for the first to finish; counters return to zero and the peak never exceeds the limit. |
| UT-MEM-02 | Cancelled waiter | Cancelling a waiter before it gets a slot does not permanently lose a permit. |
| UT-MEM-03 | Live frame cap | The oldest frame is evicted past the cap and every frame ages out by TTL. |
| UT-MEM-04 | Run buffer cap | Per-run events and the number of buffered runs are both capped; a new frame replaces the previous one. |
| UT-MEM-05 | Shared budget | `app.executor` and `app.recorder` hold the same `BrowserBudget`, so recording cannot open a second Chromium during a run. |
| UT-MEM-06 | Two runs, one browser | Two concurrent `execute_run` calls never have two browsers open at once. |
| UT-MEM-07 | Artifact export cap | `GET /api/sync/github` returns 413 rather than buffering more than `TF_MAX_ZIP_BYTES`. |
| UT-MEM-08 | Artifact export streams | Within the cap the endpoint returns a valid zip streamed from disk. |
| UT-API-01 | Run list is bounded | `GET /api/runs` omits `execution_log` by default, honours `limit`, and clamps it to `MAX_RUN_LIST_LIMIT`; `full=true` restores the log. |
| UT-LOG-01 | Logs come from GitHub | `/api/logs/source` returns raw.githubusercontent.com coordinates only - no log bodies, so the service never reads or serves them. |
| UT-LOG-02 | Branch validation | A branch of `../etc` is rejected with 422; `main` is accepted. |
| UT-LOG-03 | No git checkout | `/api/logs/source` reports `available: false` with a reason instead of a broken URL. |
| UT-LOG-04 | Local log index and file | With `TF_LOGS_DIR` set, the index is parsed from `index.md` (folder, result, counts) and a run's file is read back. |
| UT-LOG-05 | Local reader is bounded | `../` paths, unknown extensions and a 5KB read of a large file are refused or tail-truncated to the cap. |
| UT-LOG-06 | Logs tab fallback | With no checkout, `/api/logs/source` still returns a `local` block and actionable `fixes`, `/api/logs/local/index` lists runs, a file is served, and a traversal path returns 404. |
| UT-LOG-07 | Coordinates from configuration | With no checkout, `TF_GITHUB_REPO` and `TF_GITHUB_BRANCH` give `repo_ref()` a slug and branch, `/api/logs/source` returns raw.githubusercontent.com URLs, and `../etc` is still rejected with 422. |
| UT-QUEUE-01 | Orphans are cancelled | A queued run with no worker and a running run with no worker are cancelled with a restart reason; a run queued seconds ago and a run with a live worker are left alone. |
| UT-QUEUE-02 | Age filter and dry run | `clear(older_than_minutes=60)` reports one stale run without touching it in a dry run, then cancels only that run. |
| UT-QUEUE-03 | Duplicates are reused | `duplicate_of()` finds a queued, not-yet-started run of the same recording; `POST /api/runs` returns it with `deduplicated: true`, and `force: true` queues a new run. |
| UT-QUEUE-04 | History purge | `purge_finished(older_than_days=14)` deletes a 40-day-old finished run, keeps a recent one, and never deletes an unfinished run. |
| UT-QUEUE-05 | Clear endpoint | The boot reaper cancels a run left behind before startup; `POST /api/runs/queue/clear` rejects a negative or non-numeric `older_than_minutes` with 422, previews with `dry_run`, and reports `by_kind` plus the operator's reason. |
| UT-BATCH-01 | Browser reuse in normal Chromium mode | Without the single-process compatibility flag, three recordings replay through a single Chromium launch, every run carries the `batch_id` and both timestamps, the batch row reports `passed` at 100%, and the report counts the launches avoided. |
| UT-BATCH-02 | Learned pacing | Observed 100ms clicks produce a budget below the 3s default and above the floor; a 9s step is clamped at the ceiling; an unseen step keeps the default; `escalate()` relaxes within the ceiling; failures classify as timeout, navigation, selector or recording. |
| UT-BATCH-03 | Screenshots only on failure | The default `failure` mode captures exactly one PNG for the failed step and none for the passing recording, and the batch reports `partial`. |
| UT-BATCH-04 | Transient retry | A scripted timeout is retried once with an escalated budget, the run passes, and the log entry is marked `retried`. |
| UT-BATCH-05 | No retry for a defect | A click with no matching element and no coordinates is not retried: the batch fails with `retries: 0`. |
| UT-BATCH-06 | Origin grouping | `plan_order()` puts the origin with the most recordings first and orders shortest-first inside it, so one warm session covers them. |
| UT-BATCH-07 | Batch endpoints | `POST /api/runs/batch` rejects an empty selection with 422, an unknown recording with 404 and a bad screenshot mode with 422; a duplicate id is planned once; `GET /api/runs/batches` lists and an unknown batch is 404. |
| UT-BATCH-08 | Batch cancel | Cancelling marks the batch `cancelled`, cancels the recordings that had not started, and refuses to cancel a finished batch again. |
| UT-BATCH-09 | One batch at a time | The batch budget hands out one permit and refuses the second, then recovers both counters on release. |
| UT-BATCH-10 | Batch holds the browser slot | While a batch replays, the shared browser budget reports exactly one active slot, so a recording session cannot open a second Chromium; the slot is released when the batch ends. |
| UT-DOC-01 | Catalog matches this file | Every executable ID in `tests/test_unit.py` and `tests/scenarios.json` appears in this document. |
| UT-IMG-01 | COPY survives .dockerignore | Every local `COPY` source in the Dockerfile (app, library, logs, docs, requirements.txt) is present in the build context. A `docs` entry in `.dockerignore` once broke every deploy with `failed to compute cache key ... "/docs": not found` while the Azure tab needed the guide inside the image. |
| UT-IMG-02 | Container and browser agree on PORT | The Docker CMD binds the runtime `${PORT:-8000}` on `0.0.0.0` with one worker, and a Render-style `PORT=10000` rewrites the built-in demo URL to the same loopback port instead of refusing the connection. |
| UT-DB-01 | Labels fit their column | `fit_text` clips to 500 with a visible ellipsis and leaves `NULL` and short text alone; the clip is enforced on `RecordingStep`, so the API editor, the importer and the compressor are covered and not only the recorder. This is the guard against Postgres refusing an over-long label. |
| UT-DB-02 | A long navigate URL is still replayable | A 600+ character `login.microsoftonline.com` URL (what a D365 staging URL redirects to) is stored whole in `value` while the label is clipped to 500; compressing such a step keeps its ` ×N` marker inside the column. |
| UT-DB-03 | A narrow legacy column is widened | A `recording_steps.label varchar(300)` from an older revision is repaired to `VARCHAR(500)` on Postgres; a column already matching the model, an unbounded `TEXT` column and SQLite are left untouched. |
| UT-DB-04 | A rejected step is retried after the repair | An insert refused the way `varchar(300)` refuses it runs `repair_schema()` once and saves the step on the second attempt, producing exactly one row. This is the regression behind `Failed to save step: ... value too long for type character varying(300)`. |
| UT-DB-05 | A saved step is not read back | A committed step is saved without a `refresh()` SELECT, so a row a concurrent library write replaced cannot raise `Failed to save step: Could not refresh instance '<RecordingStep at 0x...>'`; the id returned is the id stored. |
| UT-DB-06 | Over-long create fields are refused up front | `POST /api/recordings` answers 422 for a name over 255 or a start URL over 1000 instead of letting the insert fail on a bounded column; a D365 staging URL is still accepted. |
| UT-DB-07 | A step survives a column nobody widened | When the repair cannot widen the column (a table the service does not own), the action is still saved: the insert is retried with a 200-character label while `value` keeps the whole URL, so a replay is unaffected and the recording is not broken by a display string. |
| UT-UI-01 | A store snapshot is cached | No function passed as `getSnapshot` to `useSyncExternalStore` returns a freshly allocated `Set`/`Map`/object/array. React compares snapshots with `Object.is`, so an allocating reader looks permanently "changed" and spins until it aborts with *Maximum update depth exceeded* (minified React error #185) and unmounts the whole tree. Returning a primitive is allowed — primitives compare by value. |
| UT-UI-02 | A cached snapshot still updates | The cache in `readHiddenTags` is keyed on the raw `localStorage` string, and `hideGuideTag` both persists and notifies subscribers. This is the other half of UT-UI-01: a reader that returned one permanent object would also stop the loop, by never updating at all, leaving a dismissed guide note stuck on screen. |
| UT-UI-03 | A writer copies before mutating | `hideGuideTag` wraps the cached snapshot in `new Set(...)` before adding to it. Mutating the instance React holds would corrupt that snapshot and hide the change from the `Object.is` comparison, so the note would be dismissed in storage but still on screen. |
| UT-UI-04 | A crash cannot blank every tab | An `ErrorBoundary` class exists, implements `getDerivedStateFromError` and `componentDidCatch`, and wraps the `createRoot(...).render(...)` call. Without it a throw in any one component unmounts everything below the root: no tabs, no message, nothing to click, while the API keeps answering 200. |
| UT-UI-05 | Every rendered component exists | Every capitalised component passed to `e(...)` across `index.html` and the three component files is defined in one of them. An undefined component is a `ReferenceError` on the first render, which blanks the app just as completely as a crash does. |

Dashboard boot (`UT-UI-*`) covers the failure where the API is perfectly healthy and the screen is empty anyway. The regression behind it: `readHiddenTags` returned a new `Set` on every call, `GuideToggle` read it through `useSyncExternalStore`, React re-rendered forever, hit its nested-update limit and unmounted the entire tree — a blank screen with no tabs and no error visible to the operator, while `/api/projects` and `/api/entities` still returned 200. UT-UI-01 to UT-UI-03 pin the snapshot contract from both sides, and UT-UI-04 and UT-UI-05 make sure that the next component-level mistake costs one panel rather than the whole dashboard.

Publishing (`UT-PUB-*`) covers the four ways a save can end: published, saved locally with a retry that really is scheduled, saved locally with publishing disabled and no retry promised, and *refused* — GitHub answered 401/403, so no retry is scheduled and the message names the fix. `UT-PUB-09` keeps a rate-limit 403 out of that last bucket, because it is the same status and a very different outcome. The API publisher is exercised against an in-memory fake transport, so no test touches GitHub.

Pen-test scenarios (`UT-SEC-*`) attack the credential surface rather than the features: they try to walk the token out through API responses, GitHub error bodies, request paths, the save banner and the test harness itself, and they pin the blast radius of one push (library prefix only, fast-forward only, dry runs inert, both configuration gates required).

Queue hygiene (`UT-QUEUE-*`) covers the runs that will never execute: orphans left by a restart, entries stale behind a busy browser, and duplicates of a recording that is already waiting. Batch execution (`UT-BATCH-*`) covers one browser for many recordings, learned step budgets, screenshots only where they are worth the CPU, and the retry rule that distinguishes a transient timeout from a broken recording.

Resource guardrails (`UT-MEM-*`) cover the limits that keep a 512MB instance inside its memory ceiling: one Chromium shared by recording and execution, bounded live frames, bounded run event buffers, a bounded run list, and an artifact export that streams instead of buffering.

Schema drift (`UT-DB-*`) covers a database that disagrees with the model: a label longer than the column it is stored in, a legacy `varchar(300)` that `create_all()` never widens because the table already exists, and the repair-and-retry that saves the user's step instead of failing their recording.

## QA agent hub

The QA agent hub (`app/qa_agents/`, dashboard tab "QA Agents") runs nine deterministic agents on pasted input or on stored runs, recordings and projects. Its checks live in `QAAgentHubTests` and cover the agents, the optional AI layer, the routes and the dashboard wiring.

| ID | Scenario | Expected result |
| --- | --- | --- |
| UT-AGT-01 | Catalogue and routes | `GET /api/agents` lists the nine agents in order and the four `/api/agents` routes are mounted on the app. |
| UT-AGT-02 | Every sample runs on rules | Each agent's built-in sample returns the common envelope (`engine` = `rules`, `ai_insights` = `[]`, AI not used) with no network call. |
| UT-AGT-03 | Requirement review wording | Vague terms, open questions and performance wording without a number are flagged; empty text is rejected with a 400-class error. |
| UT-AGT-04 | Test case generation | Numeric limits yield boundary cases at min-1, min, max and max+1; a criterion without "required" does not create empty-value cases for its field; the score is `None`. |
| UT-AGT-05 | API contract | Contract test IDs are unique across operations; Swagger 2 and YAML documents are accepted; `security: []` means unsecured. |
| UT-AGT-06 | Automation code review | A credential literal passed to `fill()` is critical (CR-06), a hard-coded sleep is flagged, and an environment-variable reference is not. |
| UT-AGT-07 | Failure triage | Timeouts classify as automation and HTTP 500 as product; an executor "other" bucket gives `executor_agrees` = `None` (no opinion); confidence stays within 0.2–0.95. |
| UT-AGT-08 | Flaky analysis | Plain "waiting for locator" timeouts count as timing, not as a locator fault; fewer than three runs gives "insufficient history". |
| UT-AGT-09 | Defect drafting | HTTP 500 is critical; an explicit reporter severity is kept (`low` becomes Jira `minor`); tokens and e-mail addresses are scrubbed from the draft; the title has no truncated "when…" suffix. |
| UT-AGT-10 | Regression selection | Tests matching a changed area and smoke-tagged tests are selected; the batch payload is `{recording_ids, name, screenshots: "failure"}`. |
| UT-AGT-11 | Release decision policy | No runs gives INSUFFICIENT EVIDENCE; a failing critical test gives NO-GO; all passing gives GO; a 90% pass rate gives CONDITIONAL GO. |
| UT-AGT-12 | AI layer is optional and safe | With a key, one call adds insights and leaves the rule findings unchanged; rules mode makes no call; a malformed reply or an exception is recorded and the rules result is kept; secrets and e-mail addresses are removed before sending. |
| UT-AGT-13 | Route errors | Input over 200,000 characters gives 413; empty text gives 400; an unknown agent gives 404; a bad mode, a non-object body and a stored source the agent does not accept give 400. |
| UT-AGT-14 | Stored-data sources | Failure triage and defect drafting read a run, flaky analysis reads a recording, and release summary and regression read a project; an unknown project gives 404. |
| UT-AGT-15 | Scrubbing stays linear | Scrubbing a 200,000-character word finishes in under two seconds (guards against a quadratic e-mail pattern). |
| UT-AGT-16 | Dashboard wiring | `index.html` loads the component, adds the "QA Agents" tab and keeps all sixteen existing tab keys. |
| UT-AGT-17 | Component classes exist in the stylesheet | Every Tailwind-style class in `qa-agents-components.js` has a rule in the committed `vendor/tailwind.css` (or is a known custom class), so no control renders unstyled. |

## Dev & Deploy kit and desktop application installation

`tests/test_dev_kit.py` covers the dashboard's Dev & Deploy tab: the guides it
serves from `docs/`, the AI prompt library generated from `app/dev_prompts.py`,
and the downloads. `tests/test_desktop_install.py` covers installing a custom
desktop application on the Windows target before recording it.

| ID | Scenario | Expected result |
| --- | --- | --- |
| UT-DEVKIT-01 | Kit index | `/api/dev/kit` lists all seven documents with sizes, plus the prompt library and its phases. |
| UT-DEVKIT-02 | Guide markdown | The Azure SQL guide and the SQL script companion are served with their repository path and no truncation. |
| UT-DEVKIT-03 | Slug allowlist | An unknown slug is a 404 naming the known documents; a traversal path never reaches the handler. |
| UT-DEVKIT-04 | Prompt download | `ai-prompt-library.md` is generated from the prompt list: every prompt id, prompt text and phase heading is present. |
| UT-DEVKIT-05 | Bundle | One zip holds every guide plus a reading-order README. |
| UT-DEVKIT-06 | Prompt quality | Every prompt has a unique id, a known phase, inputs, an expected output, review checks, and asks for no credential. |
| UT-DEVKIT-07 | Dashboard wiring | `index.html` loads `devdeploy-components.js`, declares the `Dev & Deploy` tab and renders `DevDeployTab`. |
| UT-DEVKIT-08 | Missing guide | A deployment without the file returns 404 pointing at GitHub, and the bundle skips it instead of failing. |
| UT-DEVKIT-09 | Boolean backfill on SQL Server | `BIT` columns are backfilled with `0`/`1` on `mssql` and stay `false`/`true` on PostgreSQL and SQLite. |
| UT-DEVKIT-10 | SQL script parity | The committed `CREATE TABLE` statements list exactly the columns the models declare, and JSON columns are `NVARCHAR(max)`. |
| UT-DSKINS-01 | Install registers the app | A simulated install is recorded with exit code 0 and the app becomes selectable with its receipt. |
| UT-DSKINS-02 | Command template | `msi` and `exe` installs build the documented command, with the operator's extra arguments appended. |
| UT-DSKINS-03 | Metacharacters refused | `&`, `|` and `;` in the installer or command are rejected with 422 before anything is sent to an agent. |
| UT-DSKINS-04 | Installer reference | A relative path, a plain-http URL and a wrong extension are rejected; an https URL is accepted. |
| UT-DSKINS-05 | Package managers | `winget` and `choco` take a package id and build the expected command; a path is rejected for them. |
| UT-DSKINS-06 | Verify before recording | Verification launches and closes the installed app, counts its controls, and refuses an unknown app. |
| UT-DSKINS-07 | Receipt on the recording | A desktop recording started against an installed app reports the install receipt in its session state. |
| UT-DSKINS-08 | Remove | Deleting an install drops the receipt, unregisters the profile and 404s the second time. |
| UT-DSKINS-09 | Agent dispatch | With an agent configured the `app_install` command and context are sent, the reported exit code decides the status, and the log is bounded. |
| UT-DSKINS-10 | Simulator action | `app_install` is a validated desktop action the simulator can run, reporting that nothing was really installed. |

## Library scenarios

These run against a live `uvicorn` process started by the harness.

| ID | Requires | Scenario | Expected result |
| --- | --- | --- | --- |
| LB-API-01 | api | `GET /api/health` | `status=ok`, recorder and executor both true. |
| LB-API-02 | api | POST a blank project name | HTTP 422, detail mentions name. |
| LB-API-03 | api | Create and list a project | The new name is in `GET /api/projects`, `source` is `repository`, and `repository_path` points at `project.json`. |
| LB-API-04 | api | Create, patch, and delete a variable | The variable is stored in `variables.json`. The patched value is returned, then delete succeeds. |
| LB-API-05 | api | Rephrase without an API key | `"  click   continue  "` becomes `Click continue.` |
| LB-API-06 | api | Screenshot path traversal | `../` and a missing file both return 404. No file outside artifacts is served. |
| LB-API-07 | api | Queue a run for an unknown recording | HTTP 404. |
| LB-API-08 | api | Queue a recording that has no steps | Run finishes `error` and the message contains `no steps`. |
| LB-API-09 | api | Record against another local port | `http://127.0.0.1:3000/app` is stored exactly, and the recording is in the repository catalog with a Jenkinsfile. |
| LB-API-10 | api | Diagnostics | Write probe is ok. Recorder and executor are ok. |
| LB-REC-01 | browser | Start a recording of the sample app | A JPEG frame arrives, at least 800x500, and the session does not report an error. |
| LB-REC-02 | browser | Click the name field, type, press Enter, click Continue | Steps are `navigate, click, type, press, click`. Selectors are `#name` and `#go`. After SAVE the recording and its Jenkinsfile are in the repository catalog. |
| LB-REC-03 | browser | Key press is recorded | Covered by the Enter step in LB-REC-02. A failure there fails this ID too. |
| LB-REC-04 | api | Stop a session that was never started, twice | Both calls succeed. Status is `absent`. |
| LB-RUN-01 | browser | Replay a literal recording | Status `passed`. The page excerpt contains `Hello, Quinn`. With a websocket viewer attached during replay, a live frame remains. The Jenkinsfile invokes `python playwright_test.py` (the executable replay), rather than merely echoing action names. |
| LB-RUN-02 | browser | Replay `{{user}}` with variable `user=Ada` | The stored step value is still `{{user}}`. After replay the page contains `Hello, Ada`. |
| LB-LIB-01 | api | Read `GET /api/library` | Source is `repository`. `qa-sample-app` is listed with variable `user` and both committed recordings, each with a Jenkinsfile. |
| LB-LIB-02 | browser | Run the committed recording `qa-hello-literal` | Status `passed`. The page contains `Hello, Quinn`. No new recording is created. |
| LB-LIB-03 | browser | Run the committed recording `qa-hello-variable` | Status `passed`. The page contains `Hello, Ada` because `user` comes from the repository. |
| LB-API-11 | api | `GET /api/runs/queue/status` then a dry-run clear | The status carries `hygiene` with `orphans`, `stale`, `clearable`, `duplicates` and a `policy`; `POST /api/runs/queue/clear` with `dry_run` matches the same count, cancels nothing, and leaves the queue unchanged. |
| LB-API-12 | api | Batch validation | An empty batch is 422, an unknown recording is 404, an unknown batch is 404, `GET /api/runs/batches` is a list, and `/api/diagnostics` reports `batch_executor: ok` plus the queue and logs reports. |
| LB-API-13 | api | Log source fallback | `/api/logs/source` either returns GitHub coordinates or a `local` block with actionable `fixes`; `/api/logs/local/index` answers with `source: local`, and `..%2F..%2Fetc%2Fpasswd` is 404. |
| LB-API-14 | api | Publishing contract | `/api/library` reports a `publish_mode` of `checkout`, `api` or `disabled`; when disabled it also reports `publish_disabled_reason` and `status_reason`; `/api/health` agrees on the mode; `POST /api/sync/github` is 503 with an explanation when publishing is disabled. |
| LB-BAT-01 | browser | Batch the two committed recordings | `POST /api/runs/batch` plans both, the batch finishes `passed`, both runs pass, `report.resources.browsers_launched` is 1, and the report carries throughput and elapsed seconds. |

## Adding or changing a scenario

1. Add the check to `tests/test_unit.py` or a step list in `tests/scenarios.json`. If the check is a saved recording, add it under `library/projects/` and replay that id.
2. Document the ID, preconditions, and expected result in this file.
3. Run `python -m tests.harness`.
4. Commit the scenario, any `library/` change, and the new `logs/<timestamp>/` report together so the result has a date, a time, and the revision it ran against.

## Import, action-authoring and resource regressions

These unit/API cases live in `tests/test_imports.py` and are included by the harness.

| ID | Verification |
| --- | --- |
| UT-IMP-01 | Canonical CSV/JSON round trip; preview never executes source. |
| UT-IMP-02 | XLSX literal cells accepted; formulas/multiple sheets rejected. |
| UT-IMP-03 | Static Python actions, roles and assertions. |
| UT-IMP-04 | Uploaded Python is never executed; branches/loops/eval-like calls block save. |
| UT-IMP-05 | Supported JS/TS and rejection of arbitrary code/multiple tests. |
| UT-IMP-06 | Supported BDD/Cucumber phrases; unknown steps/outlines rejected. |
| UT-IMP-07 | Restricted Jenkins reference bundle; shell/installers/missing files rejected. |
| UT-IMP-08 | File/row/archive limits, unknown columns and unknown formats. |
| UT-IMP-09 | Action schemas reject unsafe URLs, paths, timeouts and options. |
| UT-IMP-10 | Preview does not publish; confirmed save exports/publishes once; full project ZIP. |
| UT-IMP-11 | Invalid save leaves no recording; oversized/malformed requests rejected. |
| UT-IMP-12 | Append/edit an assertion; unknown options rejected. |
| UT-IMP-13 | Optional access gate protects HTTP/websockets; signed-cookie login/logout; short key fails closed. |
| UT-IMP-14 | Portable HTML report escapes target content and disables active scripts. |
| UT-IMP-15 | Autosave skips expensive exports; explicit publish always regenerates them. |
| UT-IMP-16 | Request/fixture payloads are not duplicated into audit selectors. |
| UT-IMP-17 | Unresolved variables fail before keyboard input. |
| UT-IMP-18 | Zero typing delay, local UI assets, live opt-in and one-worker exported config. |
| UT-IMP-19 | Tags persist in fast catalog, selection filters are deterministic, parent cycles rejected. |
| UT-IMP-20 | Live recording GET sees immediate DB steps without forcing an export. |
| UT-IMP-21 | Action schema versions fail closed; JS option text is not rewritten accidentally. |
| UT-IMP-22 | Idle websocket sender is cancelled when the client disconnects. |
| UT-IMP-23 | Subscriber count/event queues are bounded and retain the final result. |
| UT-IMP-24 | Plain-text step list imports as canonical steps; `{{placeholders}}` are preserved. |
| UT-IMP-25 | Markdown/pipe table rows import in sheet order with their expected results. |
| UT-IMP-26 | Market headers (Test Case/Step/Action) import; metadata columns become non-blocking notes. |
| UT-IMP-27 | Two test cases in one sheet are refused rather than silently merged. |
| UT-IMP-28 | Pipe/semicolon delimited scripts (`.psv`) import without a CSV header guess. |
| UT-IMP-29 | Selenium IDE `.side` imports; a fixed `pause` is refused like `Thread.sleep`. |
| UT-IMP-30 | Postman v2 collection imports as an `api_request` with its status assertion. |
| UT-IMP-31 | Robot Framework suite imports; `*** Variables ***` become variable suggestions. |
| UT-IMP-32 | A Cypress spec is read by the Cypress grammar, not reported as Playwright. |
| UT-IMP-33 | Java/C# Selenium subsets import; `Thread.sleep` blocks the save with a fix. |
| UT-IMP-34 | Word `.docx` tables and paragraphs import; a macro-enabled file is rejected. |
| UT-IMP-35 | YAML and XML/test-case-name sources import as canonical steps. |
| UT-IMP-36 | HAR captures become navigate + API steps with Cookie headers dropped; unknown extensions are sniffed. |
| UT-IMP-37 | HTML pages and legacy Selenium HTML suites import; page headings are notes, not errors. |
| UT-IMP-38 | An unsupported line is an error that blocks saving; a note never blocks it. |
| UT-IMP-39 | Typed literals are named after their field, secrets marked, and expectations reuse the variable. |
| UT-IMP-40 | One repeated literal becomes one variable with every usage rewritten (URL query included). |
| UT-IMP-41 | Values already using `{{variables}}`, keys and invalid names are left alone. |
| UT-IMP-42 | `variable-plan`/`variables` on a recording rewrite stored steps and are idempotent on retry. |
| UT-IMP-43 | Import `variable-plan`/`variables` endpoints propose and create variables without saving. |
| UT-IMP-44 | `/api/plans/catalog` returns recordings, inputs and variables in one cached read. |
| UT-IMP-45 | A passing receipt is listed by `GET /api/imports/tests`, carries its lifetime, and still authorises a save when read back from disk. |
| UT-IMP-46 | Each refusal names its own cause: a non-passing test, inputs edited after the pass, and a receipt from another project. |
| UT-IMP-47 | The unverified override needs `allow_untested` **and** a written reason, marks the recording in every library view, and still never executes the source. |
| UT-IMP-48 | The catalog fast path keeps `tags`, `parent_id` and `start_url`, so the API and the store cannot disagree about one recording. |
| UT-IMP-49 | The `unverified` tag helpers recognise and add the marker without duplicating it. |

## Onboarding formats, recorded labels and the template pack

These unit/API cases live in `tests/test_onboarding_formats.py` and are included by the harness. They are the regression for the reported "Unsupported action" upload: a table exported by TestForge itself (the Action cell holds the recorded label) and the equivalent vendor tables must preview with **no error-severity diagnostic** while genuinely unsupported input must still fail closed.

| ID | Verification |
| --- | --- |
| UT-ONB-01 | The reported 17-label `azure-devops.xlsx` (recorded labels in Action) previews with zero errors; values keep `{{placeholders}}`, the recorded point stays a point, and a region-quoting label is clipped. |
| UT-ONB-02 | Exports round-trip: `azure-devops.xlsx/.csv`, `jira.csv/.xlsx`, `testcomplete.csv/.xlsx` and `steps.json` re-import identically (action, value, locator, coordinates, repeat). A legacy Jira table without a Selector column still imports and keeps the `×3`. |
| UT-ONB-03 | Sentence tables keep a multi-word value whole and give every row its own expected-result assertion, in sheet order. |
| UT-ONB-04 | Unsupported verbs, labels and a fixed wait still block the save with an error. |
| UT-ONB-05 | Coordinates and a focus-only typing step normalise; a non-pointer coordinate step and an empty selector are still refused. |
| UT-ONB-06 | A coordinate click replays on the recorded point and a selector-less `type` goes to the keyboard. |
| UT-ONB-07 | Every template in `app/import_templates.CATALOG` imports (single file or bundle) with zero errors and no real-world target. |
| UT-ONB-08 | The template endpoints serve the catalog, one file, a bundle ZIP and the whole pack; the listed bytes pass the real preview endpoint. |
| UT-ONB-09 | The reported upload goes through `POST /api/imports/preview`, is saveable, and still offers its literals for the variable review. |
| UT-ONB-10 | Recorder labels map to canonical actions (click/type/press/open/save/select/right click/hover/verify) and a value-only label binds to the element the previous click targeted. |
| UT-ONB-11 | Azure DevOps/TFS metadata columns (Area/Iteration Path, State, Assigned To, Work Item Type, Remaining Work, Associated Automation, Link…) import as notes, not errors; the steps and their expectations survive. |

Opt-in **real Chromium** checks in `tests/test_browser_features.py` use a local fixture
HTTP server. Set `TF_TEST_BROWSER=1`; the export parity case also needs
`TF_TEST_NODE_MODULES` pointing at Node dependencies including `@playwright/test@1.49.1`.

| ID | Verification |
| --- | --- |
| BR-NEW-01 | Controls, strict locators, state/text/value/count/URL/title assertions, capture and intentional failure. |
| BR-NEW-02 | Iframes, popup/new-page switching/closure, prompt accept/dismiss, upload and download. |
| BR-NEW-03 | API status/JSON checks, capture into a UI input, scoped mock and cleanup. |
| BR-NEW-04 | Incorrect assertions fail actual single and batch runs; no assertion retry; failure-only screenshots. |
| BR-NEW-05 | Generated Python/JS/TS projects execute correctly, and all three exit nonzero for an incorrect assertion. |
| BR-UI-01 | Actual dashboard CSV upload → preview → isolated test → save → replay → report, then a two-row synthetic domain plan → test → save; no browser console errors. |
| BR-UI-02 | Scenario inputs, a secret variable and a PDF report on the live dashboard. |
| BR-UI-03 | The reported recorded-label export is uploaded in the dashboard: zero errors, "ready to save", isolated test passes, the recording saves and replays to a passing run. |
| BR-UI-04 | A failed test leaves SAVE locked *with the reason on screen*; the explicit override saves it marked UNVERIFIED; a passed test unlocks SAVE and keeps it unlocked after opening another tab and returning; the Library lists recordings from every project. |

`BR-UI-01` lives in `tests/test_dashboard_import.py` and requires an isolated server via
`TF_TEST_APP_URL`. It refuses a publishing-enabled instance. Never target production.
These opt-in checks are run separately from the normal harness so a missing browser
or Node toolchain is not disguised as a passed integration check.

## Sequential plans and publication gates

`tests/test_plans.py` is included in the standard harness:

| ID | Verification |
| --- | --- |
| UT-PLAN-01 | Flow expansion/cycles, fixture/data schemas, engine/device validation and work caps. |
| UT-PLAN-02 | Dedicated secret namespace and expiring auth state; resolved values are not written to source. |
| UT-PLAN-03 | Drafts create no library/recording rows; changed steps reject publication; receipt save is idempotent. |
| UT-PLAN-04 | Failed tests, changed secrets and expired receipts block publishing. |
| UT-PLAN-05 | Extra engines require opt-in; invalid IDs and draft storage inside the library are rejected. |
| UT-PLAN-06 | JSON plan preview, database/repository round trip and companion exports; other-engine plans rejected from shared Chromium batches. |
| UT-PLAN-07 | All five local synthetic packs validate with positive/negative datasets and teardown. |
| UT-PLAN-08 | A second draft is refused while one runs; cancellation releases the draft. |
| UT-PLAN-09 | Dependencies, caches, replay outputs and symlinks are excluded from GitHub API collection. |
| UT-PLAN-10 | Reusable source plans survive saving; raw-step edits cannot silently diverge from the execution snapshot. |
| UT-PLAN-11 | A concurrent autosave read cannot roll back a recorder's uncommitted SQLite write. |
| UT-PLAN-12 | Import save requires a passing receipt; omitting it or passing require_test=false cannot bypass the gate. |

`tests/test_plan_browser.py` adds explicit real-browser validation:

| ID | Verification |
| --- | --- |
| BR-PLAN-01 | Draft test before save, standalone replay, mixed ordinary/plan/multi-project sequential batch, plus Python/JS/TS plan exports with positive and deliberately failing assertions. |
| BR-PLAN-02 | Desktop/tablet/mobile viewport-touch profiles with operator-provided cookies and environment secret references; values do not appear in plan results. |
| BR-PLAN-03 | Teardown after failure, exactly one explicit retry, and flaky recovery rejected as a clean pass. |
| BR-PLAN-04 | All five synthetic domain packs execute both data rows correctly. |

Run `python tools/quality_gate.py` with its documented prerequisites for the
complete sequential, fail-closed gate. It does not install packages, publish,
schedule CI, dispatch provider workflows or deploy anything. Chromium was used
for the browser checks; Firefox/WebKit are opt-in paths, not verified claims.

## Ordered scenarios, vault inputs and PDFs

- `UT-SCENE-01..13`: encryption/masking/keyring and fail-closed behavior; ordered,
  repeated links; immutable input snapshots and precedence; saved input protection
  after a variable definition is deleted; one-session fake replay; PDF caching and
  JPEG passthrough; oldest-first/hourly remote retention discovery; provider
  response contracts; one upload/summary-only publication; malformed inputs; redaction before truncation.
- `UT-SCENE-14`: an unusable keyring — or one switched off with
  `TF_MANAGED_ENCRYPTION_KEY=0` — is reported by `GET /api/health` before a save is
  attempted: `configured`/`ready`/`keys` plus a `reason` that names
  `TF_VARIABLE_ENCRYPTION_KEYS` and the key-generation command, matching the 422 the
  save path returns, and never echoing a key.
- `UT-SCENE-15`: the Demo tab is registered in the dashboard nav, its component file
  loads, every tab carries a hover/assistive name, and both fixtures (`/demo.html`,
  `/domain-demo.html`) plus all five synthetic packs resolve and are described in the tab.
- `UT-SCENE-16`: with nothing set in the environment, encryption is still ready and
  `GET /api/encryption` reports `source: app-managed` with a note that admits the key
  lives in the same database; a secret variable saves, is ciphertext in the database,
  decrypts on demand, survives a cache reset (the key is persisted, not in memory),
  and never appears in the published project files.
- `UT-SCENE-17`: `POST /api/encryption/rotate` mints a key and re-seals in place —
  ciphertext changes, the value still decrypts, the superseded key stays in the ring
  so older snapshots keep opening, and a value whose key is gone is counted as
  `unreadable` and left byte-identical rather than overwritten. With
  `TF_VARIABLE_ENCRYPTION_KEYS` set the endpoint refuses with 422 and names the
  variable, and the environment key does not evict application-sealed values.
- `UT-SCENE-18`: every finished execution commits `library/reports/<kind>-<id>.json`
  and refreshes `index.md` without `TF_REPORTS_GITHUB` (which gates only the release
  asset); the committed summary carries outcomes and counts but no typed values,
  labels, selectors, error text or PDF; a batch publishes in one commit, not one per
  recording, and still back-fills every child; `GET /api/reports/...` returns the
  `repository_path`.
- `UT-SCENE-19`: every step records UTC `started_at`/`finished_at` and a monotonic
  `duration_ms`; a run keeps the speed factor it replayed at (`Run.execution_speed`, batch
  speed as fallback); the PDF prints the start date and time, the speed, a per-recording
  and per-step timing summary, and the committed summary/index carries the start time and speed.
- `BR-SCENE-01`: real Chromium cross-project login→logout, one shared session,
  encrypted one-off password override, masked JPEG pixel check, nine captures for
  nine actions, single and aggregate PDFs without any additional capture; single
  recording replay uses the same inputs/evidence path.
- `BR-SCENE-02`: deliberately wrong order fails; following recording is cancelled.
- `BR-SCENE-03`: frozen project defaults do not override dataset values; explicit
  one-off plan overrides do, without changing persistent project values.
- `BR-UI-02`: dashboard encrypted password/parameter save, ordered linked scenario,
  per-entry one-off editing, execution and PDF download; no browser JS errors.

Remote release operations use mocks. They do not verify live GitHub upload/delete
permission, deployment secrets, real D365 authentication, billing, or production.
