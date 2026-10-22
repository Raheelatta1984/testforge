# QA agent hub

The **QA Agents** tab runs nine QA agents over text you paste in, or over data
TestForge already holds (runs, recordings, projects). Each agent is a
deterministic rules engine. An optional AI layer can add short insights when the
server has an Anthropic API key or a local Ollama model. The AI never changes, adds
to, or removes a rule finding. The agents are also exposed as an MCP server for
Claude Code CLI and other MCP clients (see **MCP server** below).

Code: `app/qa_agents/` (one module per agent, plus `common.py`, `ai.py`,
`sources.py`, `registry.py`, `routes.py`, `mcp_server.py`). Dashboard: `app/static/qa-agents-components.js`.
Tests: `QAAgentHubTests` (`UT-AGT-01` to `UT-AGT-22`, see `tests/SCENARIOS.md`).

## The nine agents

| Agent (tab name) | Input | Stored data | What it returns |
| --- | --- | --- | --- |
| Requirement Review | `title`, `text` | none | Vague terms, open questions, missing numbers, sensitive behaviour without security rules, missing error and edge-case handling. |
| Test Case Generation | `feature`, `text` | none | Positive, negative and boundary cases per criterion, with a CSV export and a Gherkin `.feature` (one scenario per case). |
| API Contract | `title`, `spec` (OpenAPI 3 or Swagger 2, JSON or YAML) | none | Spec findings (security, error responses, sensitive fields, operationIds) and generated contract tests with unique IDs. |
| Automation Code Review | `filename`, `source` | none | Fragile locators, hard-coded sleeps and URLs, credential literals, tests without assertions. |
| Failure Triage | `errors` (list), `log` | a run | Category per failure (product, automation, data, environment), confidence, the signals behind it, and whether the executor's bucket agrees. |
| Flaky Test Analysis | `name`, `runs` (list) | a recording | Verdict (stable, flaky, intermittent, clustered, consistently failing), flip rate, timing ratio, repeated errors, likely causes with fixes. |
| Defect Drafting | `title`, `steps`, `expected`, `actual`, `error`, `logs`, `environment`, `severity` | a run | A defect draft as Markdown, Jira wiki markup and a Jira CSV row, with redacted log excerpt. |
| Regression Selection | `changed_files`, `diff`, `tests`, `max_tests` | a project | Selected tests with priority and the reason each matched, plus a batch payload for `POST /api/runs/batch`. |
| Release Summary | `release`, `window`, `runs`, `known_defects`, `open_risks` | a project | GO, CONDITIONAL GO, NO-GO or INSUFFICIENT EVIDENCE, with the reasons and a Markdown report. |

Text-only agents reject a stored source with 400. Values in `input` override the
values built from stored data.

### Decision rules

- **Flaky:** a run's flip rate and mix of passes and failures give the verdict.
  Flakiness is 0–100; the score shown is 100 minus flakiness. Fewer than three
  finished runs gives "insufficient history".
- **Triage:** confidence is `0.35 + 0.6 × share`, minus 0.15 when the runner-up is
  within one point, capped at 0.95 and floored at 0.2.
- **Defects:** severity comes from the reporter's value, otherwise *critical* for
  data-loss, security, crash or 500 wording, *minor* for cosmetic-only wording, and
  otherwise *major*. The hub's `low`/`high`/`medium`/`info` map to Jira's
  `minor`/`major`/`major`/`trivial`. Readiness is "incomplete" when a high or
  critical finding exists.
- **Release:** NO-GO when a critical or smoke test fails at its latest run, an open
  blocker or critical defect exists, or the latest pass rate is below 85%.
  CONDITIONAL GO for a pass rate of 85–95%, product failures, flaky tests or open
  major defects. GO at 95% or more with none of those. INSUFFICIENT EVIDENCE when
  nothing ran in the window. The pass rate uses the latest run per recording.
- **Regression:** IDF-weighted token overlap between the changed files and each
  test's name, tags, URL and step labels. Tests tagged `smoke`, `critical`, `p1` or
  `always` always run.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/agents` | Catalogue, AI status and modes. |
| GET | `/api/agents/{id}/sample` | Example input (the dashboard's LOAD SAMPLE button). |
| POST | `/api/agents/{id}/run` | Run on pasted input: `{"input": {...}, "mode": "auto"\|"rules"}`. |
| POST | `/api/agents/{id}/from-source` | Run on stored data: `{"source": {"type": "run"\|"recording"\|"project", ...}, "input": {...optional overrides}, "mode": ...}`. |

Errors: 400 for malformed or unsupported input, 404 for an unknown agent, run,
recording or project, 413 for input over the limit. Each error body is a short
message, and no credential is echoed back.

## MCP server (Claude Code CLI and other MCP clients)

`app/qa_agents/mcp_server.py` serves the agents over the Model Context Protocol
using stdio: one JSON-RPC 2.0 message per line. It is hand-written, so it adds no
dependency. It implements `initialize`, `ping`, `tools/list` and `tools/call`.

Run it from the repository root, with the app's Python environment:

    python -m app.qa_agents.mcp_server

Register it with a client. For example, with Claude Code CLI (check your client's
documentation for the exact flags, and set the working directory to the repository
root or `PYTHONPATH` to it):

    claude mcp add testforge -- /path/to/venv/bin/python -m app.qa_agents.mcp_server

| Tool | Input | Effect |
| --- | --- | --- |
| `qa_list_agents` | none | The nine agents, their inputs and the AI provider status. |
| `qa_get_sample` | `agent` | The agent's example input. |
| `qa_run_agent` | `agent`, `input`, `mode` | Runs an agent on input you supply. |
| `qa_run_from_source` | `agent`, `source`, `input?`, `mode` | Runs an agent on a stored run, recording or project. |
| `market_position` | none | TestForge's audited position against the compared tools: coverage, differentiators, open gaps, audit state. Small by design. |
| `market_gaps` | none | Every capability a compared tool claims and TestForge does not, with the leading tools and the disposition. |
| `resolve_step` | `phrase`, `project_id?` | Reuse-first authoring: bound, ambiguous or unbound, with ranked candidates, placeholders and example values. |
| `step_search` | `query`, `project_id?`, `limit?` | Deterministic ranked search over the step vocabulary built from stored recordings. |
| `impact_analysis` | `changed_files`, `routes`, `labels`, `diff?`, `project_id?`, `max_tests?` | Which stored recordings a change reaches, with the reason for every selection and a batch payload. |

All nine tools are marked read-only. They never start a browser or change stored
data, and the five market/library tools answer from this instance's audited data
and stored library, so an AI client does not have to paste a catalogue into the
prompt. `mode: "rules"` keeps everything on the machine. `mode: "auto"` sends the
scrubbed input to the configured AI provider, which for Ollama stays on the
local network. The server uses the same `DATABASE_URL` and `TF_*` settings as the
web app, and it sets up the schema on start, like the app does.

Stdout carries only protocol lines. The app's boot banner and progress messages go
to stderr, and `app/qa_agents/__init__.py` loads the application lazily, so the
banner never reaches the protocol stream. `UT-AGT-22` checks this with a real
subprocess. The server was also checked with the official `mcp` Python client
(version 2.3.0) in a temporary environment outside the repository: it listed the
four tools, ran them, and received the expected errors.

## Result envelope

Every agent returns the same fields: `agent`, `title`, `summary`, `verdict`,
`score` (`null` for generators and reports, which are not scored), `findings`
(each with `id`, `severity`, `title`, `detail`, `suggestion`, optional `location`),
`severity_counts`, `artifacts` (agent-specific), `warnings`, `engine`
(`rules` or `rules+ai`), `ai` (status and any error), `ai_insights` (always a list)
and `generated_at`.

## Limits

| Limit | Value | Where |
| --- | --- | --- |
| Pasted text per field | 200,000 characters | `common.MAX_TEXT_CHARS` (413) |
| Request body | 2.5 MB | `routes.MAX_BODY_BYTES` (413) |
| Runs read for a release or flaky analysis | 500 (release), 100 default and 200 max (flaky) | `sources.py` |
| Recordings read for a project | 300 | `sources.MAX_RECORDINGS` |
| Steps read per recording | 80 | `sources.MAX_STEPS` |
| Generated cases, rows and questions | 150 per list | `common.MAX_ITEMS` |

Scrubbing (tokens, passwords, keys, connection strings and personal e-mail
addresses) runs before anything is echoed or sent to a model. It is linear in the
input length.

## Optional AI layer

| Variable | Default | Effect |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Enables cloud AI insights (Anthropic). Without it, and without Ollama, every agent runs on rules only. |
| `TF_AGENT_AI_PROVIDER` | unset | `ollama` selects the local model. `anthropic` selects the cloud model. When unset, Anthropic is used if the key is set. An explicit `ollama` wins over the key. |
| `TF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server. The server calls it, so it must be reachable from this server. |
| `TF_OLLAMA_MODEL` | `llama3.1` | Ollama model name. Pull it first, for example `ollama pull llama3.1`. |
| `TF_AGENT_AI` | `1` | `0` disables the AI layer even when a key or Ollama is set. |
| `TF_AGENT_AI_MODEL` | `claude-sonnet-4-5` | Anthropic model name for the call. |
| `TF_AGENT_AI_URL` | Anthropic Messages API | Anthropic endpoint (useful for a proxy or a test double). |
| `TF_AGENT_AI_TIMEOUT` | `30` | Seconds before the call is abandoned. Raise it for a large model on a CPU-only machine. |

Rules:

- `mode: "rules"` makes no network call, even with a key.
- At most one model call per run. Input is capped at 24,000 characters and the
  reply at 900 tokens. At most six insights are kept.
- The model is told that the input is data, not instructions.
- Ollama (`TF_AGENT_AI_PROVIDER=ollama`) runs the model on your own network. The
  artifact under review does not leave it, and no key or internet access is
  needed. This is the option for offline or privacy-sensitive work. The request is
  non-streaming with JSON output. The rules result is unchanged if the model is
  slow, unreachable or returns something that is not JSON.
- A failed, slow or malformed reply leaves the rules result untouched and records
  why in `ai.error`.

## Comparison with existing functionality

"Existing" means what the repository had before this change. The verdicts ask
which option is better on each criterion. "Hub" means the new agent.

| Agent | Existing functionality | Modernization | Scalability | AI-driven capability |
| --- | --- | --- | --- | --- |
| Requirement Review | None. | Hub (no comparison). | Hub. | Hub (optional insights). |
| Test Case Generation | None. | Hub (no comparison). | Hub. | Hub (optional insights). |
| API Contract | Per-test assertions inside the recorder; the spec itself was not reviewed. | Hub. | Hub: reads a spec once, bounded. | Hub. |
| Automation Code Review | Import validator, which checks syntax only. | Hub for quality review. The validator is still the stricter syntax gate, and both should stay. | Tie: both are single-file, linear. | Hub. |
| Failure Triage | `classify_failure` in `batch_runner.py`: six buckets, used only to decide retries. | Hub: weighted signals, confidence, explanations. | Tie: both are per-failure string matching. | Hub. Existing is better at acting on the result, because it drives retries. |
| Flaky Test Analysis | Retry that passes is marked `flaky` in `plan_runtime.py`, and publication fails closed. | Hub for diagnosis. Existing is better for enforcement. | Existing: enforced in the run pipeline for every run. | Hub. |
| Defect Drafting | Per-step Jira CSV in `export_formats.py`. | Hub: a narrative report with redacted logs. | Existing is better for bulk export of steps to Jira. | Hub. |
| Regression Selection | None. Batches run whatever is chosen by hand. | Hub (no comparison). | Hub: one selection call; keyword-based, so it can miss tests whose names do not share the changed area. | Hub (optional insights). |
| Release Summary | Per-run PDF, HTML and JSON reports (`report_routes.py`, `pdf_reports.py`); no decision. | Hub for decisions and the Markdown summary. Existing is better for printable per-run evidence. | Existing: streams per-run reports. Hub reads one record per recording (bounded at 300). | Hub. |

The existing AI browser agent, `app/agent.py`, cannot be imported (it imports a
`Scenario` symbol that `app/db.py` no longer defines), so it is not part of this
comparison. The existing AI helpers (`ai_onboarding.py`, `variables_ai.py`) are
used for import and variable tasks, not for QA analysis.

Overall: on AI-driven capability the hub is better than, or the only option for,
all nine agents. The existing AI features (`ai_onboarding.py`, `variables_ai.py`)
serve import and variable tasks, not QA analysis. On modernization and scalability
the hub is better or equal in each row, but the existing functions stay better at
three things, which is why they are kept: enforcement (the flaky publication gate
in the run pipeline), bulk export (per-step Jira CSV, streamed per-run PDF/HTML/JSON
reports), and strict syntax validation (the import validator). Nothing existing was
removed or changed in behaviour.

## Stack review

Gap review of the attached infographic (Ollama, Claude Code CLI, Playwright MCP,
pytest, in a five-step flow). "Before" is what the repository had at the start of
this change.

| Item | Before | Decision | Where |
| --- | --- | --- | --- |
| Local LLM through Ollama for the AI layer (offline, privacy) | Anthropic only | **Implemented**: `TF_AGENT_AI_PROVIDER=ollama` | `app/qa_agents/ai.py` |
| QA agents available to Claude Code CLI and other MCP clients | No MCP server; the MCP packages were removed (deployment guide §12) | **Implemented**: stdio MCP server, four read-only tools, no SDK dependency | `app/qa_agents/mcp_server.py` |
| Gherkin from acceptance criteria | Gherkin only as the recording export (`recording.feature`) and `.feature` import | **Implemented**: `gherkin` artifact on Test Case Generation, with a download | `app/qa_agents/case_design.py`, `qa-agents-components.js` |
| pytest for the generated tests | The export made a standalone script only | **Implemented**: every recording export includes `test_recording.py`, which calls the same replay; `pytest==8.3.3` is pinned in `requirements.txt` | `app/export_templates.py` |
| Playwright script generation from requirements | None | **Not built**: selectors would be guesses. Recorded steps already carry real selectors, and those are exported. | none |
| Playwright MCP in the server | Removed on purpose (deployment guide §12) | **Not reintroduced**. The new MCP server exposes the hub, not a browser. | none |
| Cross-browser matrix (chromium, firefox, webkit) | Present | Already present; no change | `app/plan_runtime.py` |
| Playwright Python export | Present | Already present; now also runnable under pytest | `app/export_templates.py` |
| Gherkin import and export | Present | Already present; no change | `app/export_formats.py`, `app/import_formats.py`, `app/script_import.py` |

Verification of the new pieces:

- Gherkin: a generated feature parsed with the official Gherkin parser (`gherkin-official`
  from PyPI, in a temporary environment): 5 scenarios, 5 pickles.
- pytest: the exported folder was collected by pytest 8.3.3 (one test). The replay
  failed here because no browser can be installed in this sandbox, which confirms
  that a failing step fails the test.
- MCP: checked with the official `mcp` client, as described above.
- Ollama: checked against a local fake HTTP server (`UT-AGT-19`, `UT-AGT-20`). No real
  model was run, because model downloads are blocked here.

## Known limits

- The rules engine recognises wording and patterns, not intent. A requirement can
  be well written and still be missed, and the AI insights are a second opinion
  that needs a human check.
- Regression selection and triage are keyword and signal based. Use the batch
  payload as a starting point, not as a guarantee of coverage.
- The hub is advisory. It does not run tests, publish defects, or change releases.
- Ollama has been verified against a fake server, not a real model. Output quality and
  JSON compliance depend on the model. Try a few models on real artifacts before you rely
  on the insights.
- The MCP server speaks stdio only. It has no HTTP or SSE transport. Its tools run on the
  server's own data, so the client needs access to the repository.
- Generated Gherkin steps are plain English. A BDD runner needs step definitions bound
  to the application's locators. The existing `.feature` importer needs quoted CSS
  selectors, so generated features do not import back unchanged.
- The pytest wrapper replays the recording and needs a browser. It does not add
  assertions: it checks that the replay completes, as the existing script does.
- The dashboard's **Tailwind stylesheet** is the committed `vendor/tailwind.css`.
  The new tab only uses classes that appear in it, and `UT-AGT-17` enforces this.
  `tools/ui/build.cjs` now includes the new component in its scan list, so a future
  rebuild keeps those classes.
