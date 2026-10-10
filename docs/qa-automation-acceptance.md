# Recording and library quality plan

Rephrased as a senior QA automation engineer's acceptance criteria. The repository branch in this Arena session is `arena/4220000e-testforge`; the application publishes **only the checked-out branch**, never `main` implicitly.

| # | Testable requirement | Implementation / verification |
|---|---|---|
| 1 | A recorded `{{name}}` types the project's **value**, not its identifier or the literal placeholder. Missing names fail visibly; the step retains `{{name}}` for replay. | Recorder resolves before typing; replay interpolates. `UT-FLOW-04`. |
| 2 | The remote browser uses a large workspace with zoom controls; its click coordinates remain accurate when zoomed. Commands include Ctrl, Alt, copy, paste and save. The first tap enlarges/arms a command; the second tap executes it. A focused input can be saved to a new or existing project variable. | Recorder UI, `save_variable` step, and reduced keyboard typing delay. Browser/latency must be profiled on a deployment; no absolute latency guarantee. |
| 3 | Display newly recorded actions first (10, 9, …), but persist and execute in original order (1, 2, …), with **one editable step per action**, including repeated actions. | Recording UI prepends steps; repository export no longer compresses them. `UT-FLOW-01`. |
| 4 | SAVE is idempotent even if the live session has ended; it must not fail with “Recording session is not running.” | `/stop` exports without requiring a live session. `UT-FLOW-02`. |
| 5 | A saved recording is immediately written to the repository tree and a push is attempted/verified on the active branch. Generate runnable Playwright Python and JavaScript replays and a Jenkinsfile, a BDD feature, canonical JSON, and CSV/XLSX import templates for Azure DevOps, Jira and TestComplete. | `resources/` exports; download links in Library; `UT-FLOW-03`. The Gherkin feature requires step definitions and vendor templates require field mapping; these are **not** proprietary native recording files. A failed push is shown as pending, not reported as successful. |
| 6 | Step screenshots are PNG at CSS pixel resolution and are palette-compressed only if that saves bytes. Preview frames stay low-quality JPEG for responsiveness; video is off by default and, if enabled, is a small WebM, because PNG is a still-image format, not a video codec. | Screenshot writer and execution artifacts. PNG can exceed JPEG size on complex pages; benchmark actual pages before imposing a byte limit. |
| 7 | Open any saved recording from Library, inspect each action/input/selector alongside its screenshot, edit any step, and save back to the repository. | Step PATCH endpoint, scoped PNG endpoint and Library editor. `UT-FLOW-02`. |
| 8 | A run only passes after every step succeeds; completion becomes 100%. Queue status reports queued, running, passed, failed, pending and pass percentage. Completed runs leave the **pending** queue but remain in history. | Executor + queue API and Runs summary. Sequential execution avoids browser resource contention. |
| 9 | Audit shows each executed step's number, action, input, selector, outcome and screenshot together. | Audit tab and run log. |
| 10 | GitHub status checks the actual remote branch SHA (`ls-remote`), not only local Git state. A failed push keeps the local library and retries at one-minute intervals while the worker is running. | Remote verification and async retry task. Offline GitHub, permissions and branch protection cannot be overridden; errors must stay visible. Multi-worker/deployment restart durability of the timer requires a separate job service. |

## Validation

Run `.venv/bin/python -m tests.test_unit` or `python -m tests.harness`. Browser-dependent scenarios need Chromium (`playwright install chromium`) and a reachable test application. Run deployed browser tests and measure click-to-step latency and PNG sizes before marking those performance targets verified.
