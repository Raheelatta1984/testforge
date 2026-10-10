# TestForge AI Tracking
- **Database:** Permanently stored in Neon PostgreSQL / SQLite (Configured in config.py).
- **Architecture:** 8-tab Persistent Dashboard (Optimized for Android 12).
- **AI Features:** Voice-to-Text input, AI Step Rephrase, Recording-Variable association tagging.
- **Tree Hierarchy:** Parent Group -> Sub-recording chunk organization.
- **Execution:** Live CDP screencast window in 'Runs' tab with completion % and visual diff potential.
- **Maintenance:** Reset branch with `git fetch origin && git reset --hard origin/main` to fix sync issues.

## Deployment and manual QA
- Open `/api/health` on the deployed service to verify the app and database are ready. On Render, the response includes the `RENDER_GIT_COMMIT` revision.
- Create a project from the **Projects** tab with a name and optional application URL. TestForge projects are database records; they are not imported from this source-code repository.
- Open the **GitHub** tab and choose **Open Source Repository** to visit the repository.
