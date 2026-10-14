# TestForge for the in-house dev team: local setup, running and testing

From a fresh clone to a running dashboard, then the checks that prove a change
works. Everything here runs on Linux, macOS and Windows (WSL recommended for the
browser scenarios).

## 1. What you need

| Tool | Version | Why |
| --- | --- | --- |
| Python | 3.11 | The image is `python:3.11-slim-bookworm`; match it locally |
| pip | any recent | Installs the pinned dependencies |
| Chromium | Playwright's build | Only for the browser recording/replay scenarios |
| git | any | Library publishing and the harness commit check |

No Node.js. The dashboard is plain JavaScript with vendored React and Tailwind
under `app/static/vendor/`; there is nothing to build.

## 2. Clone and install

```bash
git clone https://github.com/Raheelatta1984/testforge.git
cd testforge
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-test.txt
playwright install chromium          # only needed for browser scenarios
```

`requirements-test.txt` adds `httpx`, which FastAPI's `TestClient` needs. Keep
both files pinned: the deployment installs exactly these versions.

If your database will be SQL Server, also install the driver described in
`docs/dev-azure-sql-server.md` (`pip install -r requirements-sqlserver.txt`) and
the ODBC driver package for your OS. The core service does not need it, which is
why it is a separate file.

## 3. Run it

```bash
export TF_ARTIFACTS="$PWD/artifacts"     # screenshots, runs, exports
export PORT=8000
uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
```

or `bash start.sh`, which sets the same defaults and activates `.venv` if it
exists.

Then open `http://127.0.0.1:8000`. Useful first stops:

| URL | What it proves |
| --- | --- |
| `/api/health` | Process up, database reachable, recorder and executor importable, `revision` |
| `/api/diagnostics` | Dialect, tables created, schema repairs, errors |
| `/` | The dashboard and every tab |
| `/demo.html` | The built-in sample app the Demo tab records against |

Without `DATABASE_URL` the service uses SQLite at
`$TF_ARTIFACTS/testforge_erp.db`, which is the right choice for local work.

## 4. Run the tests

```bash
python -m tests.harness
```

The harness runs the unit suite and the library scenarios, then writes a
timestamped report under `logs/<YYYYMMDD-HHMMSS>/` and updates `logs/index.md`.
Exit status is 0 only when every executed scenario passed.

Faster loops:

```bash
# Unit modules only, no browser, no server:
python -m unittest discover -s tests -p "test_*.py" -t .

# One module:
python -m unittest tests.test_roadmap -v
```

Browser scenarios are skipped when Chromium is missing. Add `--require-browser`
to turn that skip into a failure — do that in CI, not on a laptop.

The harness never publishes to GitHub: it strips `TF_GITHUB_TOKEN`,
`GITHUB_TOKEN`, `GH_TOKEN`, `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and
`TF_GIT_REMOTE` from the server it starts, and sets `TF_LIBRARY_PUBLISH=0`.

## 5. Test isolation

`tests/bootstrap.ensure_test_env()` points `DATABASE_URL`, `TF_ARTIFACTS` and
`TF_LIBRARY_DIR` at a throwaway temporary directory. **It must run before
anything imports `app.config`**, because that module resolves the database URL at
import time. Every test module starts with:

```python
from tests.bootstrap import ensure_test_env
ensure_test_env()
from app.main import app          # after, never before
```

If your local run writes to `~/testforge_titan_data`, a module imported
`app.config` first. Fix the import order; do not delete the directory.

## 6. Adding a test

1. Create or extend a module under `tests/`.
2. Register it in `tests/harness.py`, in the `loadTestsFromNames([...])` list
   inside `run_unit_tests`. A module that is not listed is never run by the
   harness, however good it is.
3. Name methods `test_UT_<AREA>_<NN>_<what it checks>`; the harness derives the
   scenario ID `UT-<AREA>-<NN>` from the name.
4. Add that ID to `tests/SCENARIOS.md` under Unit scenarios. `UT-DOC-01` fails
   the run when an executable ID from `tests/test_unit.py` is missing there.

Rules the existing tests follow, and reviewers enforce:

- one behaviour per test, with the failure mode in the docstring;
- no network, no real GitHub, no paid service;
- browser work only in a scenario that says so, and never in a unit module;
- assert the message a user would see, not only the status code.

## 7. Local environment variables worth knowing

| Variable | Local default | Effect |
| --- | --- | --- |
| `TF_ARTIFACTS` | `~/testforge_titan_data` | Screenshots, runs, exports, PDFs |
| `DATABASE_URL` | SQLite under `TF_ARTIFACTS` | Point at Postgres or SQL Server to test that dialect |
| `TF_LIBRARY_DIR` | `library/` | Where projects and recordings live on disk |
| `TF_LIBRARY_PUBLISH` | on | Set `0` to write files without committing or pushing |
| `PORT` | `8000` | Port for `start.sh` and the harness server |
| `TF_ACCESS_TOKEN` | unset | Unset means no gate locally — never unset in a hosted instance |
| `RENDER_GIT_COMMIT` | `local` | Reported as `revision` by `/api/health` |

The full table, including every limit, is in `readme.md` under *Environment
variables*.

## 8. Working agreements

- Branch from `main`, keep the diff to one concern, and say in the PR what the
  harness ran.
- Do not commit anything under `artifacts/`, `.env`, `*.pem` or `*.key` — see
  `.gitignore`.
- Do not commit a recording that contains a real credential, even encrypted: the
  library is published to a repository.
- If a change alters a public endpoint's request or response, update the module
  docstring and the guide in `docs/` that describes it.
- A green build that executes no changed code is not evidence. Name the function
  your test called.

## 9. When something does not start

| Symptom | First thing to check |
| --- | --- |
| `RECORDER UNAVAILABLE` at boot | Playwright and Chromium are installed; read the printed reason |
| `DATABASE FATAL ERROR` | `/api/diagnostics` shows the redacted error and the dialect |
| Dashboard loads but every call is 401 | `TF_ACCESS_TOKEN` is set and you are not signed in |
| Windows desktop shows the simulator | `TF_WINDOWS_AGENT_URL` and `TF_WINDOWS_AGENT_TOKEN` are unset, which is expected locally |
| Harness says `server did not become healthy` | Read `logs/<timestamp>/server.log`; the harness prints the tail |
