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
| UT-LIB-01 | Create a project | `project.json` is written and the list comes from that file. Publish is off in unit tests. |
| UT-LIB-02 | Database-only project | A row that is not in `library/` is not listed, and materialize removes it. |
| UT-LIB-03 | Create a recording | `recording.json` and `resources/Jenkinsfile` are written. |
| UT-LIB-04 | Variable roundtrip | Create, update, and delete change `variables.json` and only that file. |
| UT-LIB-05 | Path escape | An id of `../etc` is rejected. |
| UT-LIB-06 | Publish | A local git remote receives only `library/` files. `KEEP.txt` is untouched. |
| UT-LIB-07 | Failed push | The project is not listed and the database row is removed. |
| UT-LIB-08 | File-only recording | A recording that exists only as JSON is loaded into the database for replay. |
| UT-RUN-01 | Recording with no steps | Status becomes `error`, the monitor log says no steps, and no browser is launched. |
| UT-RUN-02 | Run whose recording was deleted | Status becomes `error` and the event says `Recording not found`. |
| UT-RUN-03 | Preview with no viewer | While `should_capture` reports zero viewers the loop renders nothing; capture resumes once somebody watches. |
| UT-RUN-04 | Live window toggle reaches the queue | `POST /api/runs` with `display_window=false` records the choice on the run; omitting it defaults to on. |
| UT-RUN-05 | Window off, replay on | `execute_run(display_window=False)` passes, requests no JPEG frame, pushes no frame to the dashboard, and every step PNG is on disk before the run finishes. |
| UT-RUN-06 | Window on with a viewer | `execute_run(display_window=True)` captures JPEG frames while a viewer is attached. |
| UT-RUN-07 | Window on, nobody watching | With a viewer count of zero the run passes and renders no frames. |
| UT-RUN-08 | Repeat count on replay | A step stored with `repeat_count=3` produces three clicks. |
| UT-SAVE-01 | Slow export on save | `stop()` returns cleanly when the debounced export outlives its flush budget, the browser is closed, and the export is left to finish rather than cancelled mid-write. This is the regression that raised `AttributeError: 'NoneType' object has no attribute 'cancel'`. |
| UT-SAVE-02 | Export raises on save | `stop()` still closes the browser and records the failure in the session save log. |
| UT-PUB-01 | Disabled publishing promises no retry | `publish_outcome(False)` returns `publish_state=local-only`, `retry_scheduled=false`, `retry_in_seconds=null`, and a message that says publishing is disabled. This is the regression where the record tab showed "GitHub push will retry in 1 minute: GitHub publishing is disabled". |
| UT-PUB-02 | Failed push states the retry | With a checkout, a failed push returns `retry-pending`, the configured `TF_PUBLISH_RETRY_SECONDS` wait, and the git error verbatim; a successful push returns `published` with no retry. |
| UT-PUB-03 | API publisher commits and verifies | With no checkout, `github_api.publish()` creates one blob per file, a tree, a commit and a ref update against a fake transport, then reads the branch back before reporting `published`. Blob SHAs match what git would compute. |
| UT-PUB-04 | API publisher no-op when synced | A second publish with an unchanged library creates no blob and no commit and reports `state=synced`; a dry run publishes nothing. |
| UT-PUB-05 | Push bounds and token redaction | A file larger than `TF_GITHUB_MAX_FILE_BYTES` is skipped with a reason, and neither an `Authorization:` header nor a `ghp_…` token survives `_safe()`. |
| UT-PUB-06 | API mode is reported, not dashes | With `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and a token, `status()` reports `publish_mode=api`, `git_state=api`, the configured branch and repository URL, a `git_note`, and no git-shaped `status_reason`. |
| UT-PUB-07 | A stray token publishes nothing | A `GITHUB_TOKEN` with no configured repository leaves `publish_mode=disabled`: API publishing is opt-in, so a CI runner cannot push anywhere by accident. |
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
| UT-BATCH-01 | One browser per batch | Three recordings replay through a single Chromium launch, every run carries the `batch_id` and both timestamps, the batch row reports `passed` at 100%, and the report counts the launches avoided. |
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

Publishing (`UT-PUB-*`) covers the three ways a save can end: published, saved locally with a retry that really is scheduled, and saved locally with publishing disabled and no retry promised. The API publisher is exercised against an in-memory fake transport, so no test touches GitHub.

Pen-test scenarios (`UT-SEC-*`) attack the credential surface rather than the features: they try to walk the token out through API responses, GitHub error bodies, request paths, the save banner and the test harness itself, and they pin the blast radius of one push (library prefix only, fast-forward only, dry runs inert, both configuration gates required).

Queue hygiene (`UT-QUEUE-*`) covers the runs that will never execute: orphans left by a restart, entries stale behind a busy browser, and duplicates of a recording that is already waiting. Batch execution (`UT-BATCH-*`) covers one browser for many recordings, learned step budgets, screenshots only where they are worth the CPU, and the retry rule that distinguishes a transient timeout from a broken recording.

Resource guardrails (`UT-MEM-*`) cover the limits that keep a 512MB instance inside its memory ceiling: one Chromium shared by recording and execution, bounded live frames, bounded run event buffers, a bounded run list, and an artifact export that streams instead of buffering.

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
| LB-RUN-01 | browser | Replay a literal recording | Status `passed`. The page excerpt contains `Hello, Quinn`. A live frame remains. The Jenkins script mentions `navigate`. |
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
