# TestForge AI Tracking
- **Database:** Permanently stored in Neon PostgreSQL / SQLite (Configured in config.py).
- **Architecture:** 8-tab Persistent Dashboard (Optimized for Android 12).
- **AI Features:** Voice-to-Text input, AI Step Rephrase, Recording-Variable association tagging.
- **Tree Hierarchy:** Parent Group -> Sub-recording chunk organization.
- **Execution:** Live CDP screencast window in 'Runs' tab with completion % and visual diff potential.
- **Maintenance:** Check `/api/library` for live remote verification and retry status. Failed pushes remain local and are retried; do not reset a branch to fix sync issues.

## Testing
The harness runs the unit suite and the library scenarios. Library scenarios are the recordings and projects in `library/`, executed through the same recorder and runner the dashboard uses. From the repository root:

```bash
python -m tests.harness
```

Scenarios are documented in `tests/SCENARIOS.md`. Each run writes a timestamped report under `logs/`, and `logs/index.md` lists them. Chromium is required for the browser scenarios; unit tests run without it.

## Record and run
Open a project, then the record tab. Leave the page blank to open the built-in sample app, or enter the application URL. The remote Chromium picture is polled over HTTP (so it still works when a proxy drops websockets). Click the picture to click in the browser, type with the text box, then SAVE. In the library, RUN replays those steps and the Runs tab shows the live browser plus each step.

The server needs Chromium: `playwright install chromium`. Docker already does this. If the browser cannot start, the record screen shows the error instead of a blank canvas.

## Deployment and manual QA
- Open `/api/health` on the deployed service to verify the app and database are ready. On Render, the response includes the `RENDER_GIT_COMMIT` revision.
- Open `/api/diagnostics` when something fails. It reports the database dialect, the schema repairs applied at boot, the live columns of every table, and the result of a test project insert that is rolled back.
- Create a project from **Projects**. It is written to `library/` and a push is attempted on the checked-out Git branch. GitHub status verifies the remote SHA; a failed push is not presented as published.
- Open the **GitHub** tab to see the repository catalog and push any unpublished library changes.

## Database migrations
`app/db.py` runs an idempotent schema check at startup. It creates missing tables, adds columns that the models declare but an older table lacks (backfilling existing rows), and relaxes `NOT NULL` on legacy columns the models no longer write. `create_all()` alone only creates missing tables, so a database created by an older revision keeps its original columns and every write to it fails until this migration runs.

## QA acceptance criteria and exports
See [docs/qa-automation-acceptance.md](docs/qa-automation-acceptance.md) for the ten testable recording, execution and GitHub acceptance criteria and implementation limitations. The Library editor can modify saved steps. Recording resources include Playwright Python, Jenkins, Gherkin and Azure/Jira/TestComplete CSV/XLSX templates; proprietary vendor imports require mapping. PNG screenshots are saved alongside steps; preview JPEGs are transient and optional video is WebM.
