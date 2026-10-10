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
| UT-GIT-01 | No git checkout | `status()` reports `git_state=no-checkout` with a reason instead of a silent `branch: null`, and `remote_library_status()` explains the missing checkout. |
| UT-GIT-02 | Library disk report | `status()` reports `projects_on_disk`, `project_dirs`, `catalog_present` and `library_dir`. |
| UT-DOC-01 | Catalog matches this file | Every executable ID in `tests/test_unit.py` and `tests/scenarios.json` appears in this document. |

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

## Adding or changing a scenario

1. Add the check to `tests/test_unit.py` or a step list in `tests/scenarios.json`. If the check is a saved recording, add it under `library/projects/` and replay that id.
2. Document the ID, preconditions, and expected result in this file.
3. Run `python -m tests.harness`.
4. Commit the scenario, any `library/` change, and the new `logs/<timestamp>/` report together so the result has a date, a time, and the revision it ran against.
