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
| Library database | separate temporary SQLite used only by the harness server |
| Logs | `logs/<timestamp>/{harness,unit,scenarios,server}.log` plus `results.json` and `results.md` |

## Unit scenarios

| ID | Scenario | Expected result |
| --- | --- | --- |
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
| UT-RUN-01 | Recording with no steps | Status becomes `error`, the monitor log says no steps, and no browser is launched. |
| UT-RUN-02 | Run whose recording was deleted | Status becomes `error` and the event says `Recording not found`. |
| UT-DOC-01 | Catalog matches this file | Every executable ID in `tests/test_unit.py` and `tests/scenarios.json` appears in this document. |

## Library scenarios

These run against a live `uvicorn` process started by the harness.

| ID | Requires | Scenario | Expected result |
| --- | --- | --- | --- |
| LB-API-01 | api | `GET /api/health` | `status=ok`, recorder and executor both true. |
| LB-API-02 | api | POST a blank project name | HTTP 422, detail mentions name. |
| LB-API-03 | api | Create and list a project | The new name is in `GET /api/projects`. |
| LB-API-04 | api | Create, patch, and delete a variable | The patched value is returned, then delete succeeds. |
| LB-API-05 | api | Rephrase without an API key | `"  click   continue  "` becomes `Click continue.` |
| LB-API-06 | api | Screenshot path traversal | `../` and a missing file both return 404. No file outside artifacts is served. |
| LB-API-07 | api | Queue a run for an unknown recording | HTTP 404. |
| LB-API-08 | api | Queue a recording that has no steps | Run finishes `error` and the message contains `no steps`. |
| LB-API-09 | api | Record against another local port | `http://127.0.0.1:3000/app` is stored exactly. |
| LB-API-10 | api | Diagnostics | Write probe is ok. Recorder and executor are ok. |
| LB-REC-01 | browser | Start a recording of the sample app | A JPEG frame arrives, at least 800x500, and the session does not report an error. |
| LB-REC-02 | browser | Click the name field, type, press Enter, click Continue | Steps are `navigate, click, type, press, click`. Selectors are `#name` and `#go`. |
| LB-REC-03 | browser | Key press is recorded | Covered by the Enter step in LB-REC-02. A failure there fails this ID too. |
| LB-REC-04 | api | Stop a session that was never started, twice | Both calls succeed. Status is `absent`. |
| LB-RUN-01 | browser | Replay a literal recording | Status `passed`. The page excerpt contains `Hello, Quinn`. A live frame remains. The Jenkins script mentions `navigate`. |
| LB-RUN-02 | browser | Replay `{{user}}` with variable `user=Ada` | The stored step value is still `{{user}}`. After replay the page contains `Hello, Ada`. |

## Adding or changing a scenario

1. Add the check to `tests/test_unit.py` or a step list in `tests/scenarios.json`.
2. Document the ID, preconditions, and expected result in this file.
3. Run `python -m tests.harness`.
4. Commit the scenario and the new `logs/<timestamp>/` report together so the result has a date, a time, and the revision it ran against.
