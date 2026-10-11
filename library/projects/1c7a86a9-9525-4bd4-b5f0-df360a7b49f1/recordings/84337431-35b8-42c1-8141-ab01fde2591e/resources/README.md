# TestForge exports

`recording.json` is the source of truth. `playwright_test.py` and `playwright_test.js` are runnable Playwright replays; Jenkinsfile invokes Python (set TF_VAR_<name> environment variables for placeholders). The Gherkin feature needs project-specific step definitions. Azure DevOps, Jira and TestComplete CSV/XLSX files are import templates that require mapping to the target instance; they are not native proprietary recordings. Screenshots are PNG. Video, when enabled, is WebM, not PNG.
