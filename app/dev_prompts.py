"""AI prompt library for the in-house development team.

One source of truth for the prompts a developer pastes into an AI assistant
while working on this repository. The dashboard's *Dev & Deploy* tab renders the
structured data from :func:`prompt_library`, and the downloadable
``ai-prompt-library.md`` is generated from the same list by
:func:`prompt_markdown`, so the file the team emails around can never drift from
what the dashboard shows.

Every prompt names real paths and environment variables from this repository.
Nothing here calls an AI service: the dashboard only displays and copies text.
"""
from __future__ import annotations

REPO_URL = "https://github.com/Raheelatta1984/testforge"

PROMPT_PHASES = [
    {"id": "discovery", "name": "Discovery & architecture",
     "detail": "Understand what exists before changing it."},
    {"id": "database", "name": "Database & Azure SQL",
     "detail": "Schema, connection strings, SQL Server on Azure."},
    {"id": "backend", "name": "Backend & API",
     "detail": "FastAPI routes, runners, bounded payloads."},
    {"id": "frontend", "name": "Dashboard UI",
     "detail": "Build-free React in app/static."},
    {"id": "testing", "name": "Tests & harness",
     "detail": "tests/harness.py, unit modules, library scenarios."},
    {"id": "delivery", "name": "CI/CD & deployment",
     "detail": "Docker image, Azure runtime, rollbacks."},
    {"id": "security", "name": "Security & review",
     "detail": "Secrets, redaction, the access gate."},
]

# Bounded on purpose: a prompt library that grows without review stops being read.
MAX_PROMPTS = 40

PROMPTS = [
    {
        "id": "arch-map",
        "phase": "discovery",
        "title": "Map the codebase before changing it",
        "when": "First day on the repository, or before touching a module you have not read.",
        "inputs": [
            "The output of `find app -name '*.py' | xargs wc -l | sort -n` (module sizes)",
            "`readme.md` (the environment-variable table and the Testing section)",
        ],
        "prompt": (
            "You are reviewing a FastAPI test-automation service. I will paste its module list with "
            "line counts and its readme. Produce:\n"
            "1. A dependency map: which module owns recordings, execution, the library on disk, "
            "exports, secrets and the desktop/mobile adapters.\n"
            "2. The single-worker constraints that a new feature must not break (concurrent browsers, "
            "one interactive recorder, sequential queue).\n"
            "3. Five questions I must answer before editing anything.\n"
            "Do not invent files. If a responsibility is unclear from what I pasted, say 'unclear' "
            "and name the file to open."
        ),
        "output": "A dependency map plus a pre-edit checklist, with every claim tied to a pasted file.",
        "review": [
            "Every module it names exists in the pasted list",
            "The single-worker constraints match `MAX_CONCURRENT_BROWSERS` in app/config.py",
        ],
    },
    {
        "id": "arch-new-feature",
        "phase": "discovery",
        "title": "Design a new feature that fits the existing shape",
        "when": "You have a feature request and need a design that this codebase will accept.",
        "inputs": [
            "One sentence of the feature request",
            "The route file closest to it (for example `app/roadmap_routes.py`)",
            "The matching static component file (for example `app/static/future-components.js`)",
        ],
        "prompt": (
            "Feature request: <ONE SENTENCE>.\n"
            "Here are the closest existing route module and dashboard component. Design the change so it "
            "follows the existing shape exactly:\n"
            "- a router module with bounded, validated request bodies (Pydantic model or dict checks),\n"
            "- no new background worker, no new dependency,\n"
            "- dashboard state in a component file loaded before app/static/index.html,\n"
            "- a test module that runs under `python -m tests.harness`.\n"
            "Return: files to add, files to change, the exact endpoint list with request/response shapes, "
            "the validation rules and their error strings, and the test names you would write. "
            "Flag anything that would need a new dependency as out of scope."
        ),
        "output": "A file-by-file plan with endpoint contracts and validation rules.",
        "review": [
            "No new pip dependency is introduced silently",
            "Every new route is behind the existing AccessGate middleware (app/access_control.py)",
        ],
    },
    {
        "id": "arch-reading-order",
        "phase": "discovery",
        "title": "Trace one user action end to end",
        "when": "A bug report says 'the run failed' and you do not know where to look.",
        "inputs": [
            "The failing request URL and response body",
            "The run or recording ID from the dashboard",
        ],
        "prompt": (
            "Trace this failure through the service. Request: <METHOD AND URL>. Response: <BODY>. "
            "ID: <ID>.\n"
            "Using the module list I paste, give me the call chain from the route handler to the point "
            "that produced this message, naming the function in each hop. Then list, in order, the log "
            "lines and artifact files I should read to confirm each hop, and the three most likely causes "
            "ranked by how cheaply I can rule them out. Do not guess at code you cannot see: mark those "
            "hops 'unverified'."
        ),
        "output": "A call chain with unverified hops labelled, plus a ranked cause list.",
        "review": ["Each hop names a real function", "The log/artifact paths exist under TF_ARTIFACTS"],
    },
    {
        "id": "db-schema-audit",
        "phase": "database",
        "title": "Audit the SQLAlchemy models against a live database",
        "when": "Before pointing an environment at a new database server.",
        "inputs": [
            "app/db.py (models plus `_ensure_schema`, `_add_missing_columns`)",
            "The output of `/api/diagnostics` from the target environment",
        ],
        "prompt": (
            "Here are the models and the schema-repair code, plus `/api/diagnostics` from the target "
            "database. Answer:\n"
            "1. Which tables and columns does `create_all()` create on a fresh database?\n"
            "2. Which repairs are dialect-gated to PostgreSQL only, and what breaks if the dialect is "
            "something else?\n"
            "3. Does the diagnostics payload show any table missing, any repair recorded, or an error?\n"
            "4. What would you check by hand with SQL before trusting this database?\n"
            "Give me the exact SQL for step 4 and mark anything you could not determine as 'unverified'."
        ),
        "output": "A schema-audit checklist with runnable SQL and the dialect caveats.",
        "review": [
            "The dialect-gated functions it names are the ones in app/db.py",
            "The SQL it gives runs on the dialect you actually use",
        ],
    },
    {
        "id": "db-azure-sql",
        "phase": "database",
        "title": "Provision SQL Server on Azure for this app",
        "when": "You are moving the database to Azure SQL Database or SQL Server on an Azure VM.",
        "inputs": [
            "docs/dev-azure-sql-server.md",
            "The current `DATABASE_URL` shape (not the credentials)",
            "The instance size you are deploying to",
        ],
        "prompt": (
            "I am moving this service to SQL Server on Azure. Here is the guide, the current connection "
            "string shape and the instance size.\n"
            "Produce an ordered runbook with copy-pasteable `az` CLI commands for: resource group, the "
            "SQL resource, an admin login stored in Key Vault, a firewall rule or private endpoint, the "
            "application login and its least-privilege grant, and the exact `DATABASE_URL` for "
            "`mssql+pyodbc` including `Encrypt=yes` and `TrustServerCertificate=no`.\n"
            "Then give me the pool arithmetic for this instance size (pool_size 20 + max_overflow 10 in "
            "app/db.py) against the DTU/vCore limit of the tier I named, and tell me if it does not fit. "
            "Never print a real password: use a Key Vault reference."
        ),
        "output": "An az CLI runbook, a connection string template and a pool-sizing verdict.",
        "review": [
            "The connection string sets Encrypt=yes",
            "No credential appears in the output or in the commit",
            "Pool size is checked against the chosen tier's connection limit",
        ],
    },
    {
        "id": "db-migrate",
        "phase": "database",
        "title": "Migrate data from PostgreSQL to SQL Server",
        "when": "An existing environment has real projects and recordings that must survive the move.",
        "inputs": [
            "The table list from `/api/diagnostics`",
            "Row counts per table on the source database",
            "Whether `TF_LIBRARY_DIR` is on a volume or baked into the image",
        ],
        "prompt": (
            "Plan a data migration from PostgreSQL to SQL Server for this service.\n"
            "Constraints you must respect: this repository has no Alembic — `init_db()` creates missing "
            "tables and adds missing columns at boot; the library of recordings also lives as JSON files "
            "under `TF_LIBRARY_DIR`, not only in the database; one worker only, so the move needs a "
            "maintenance window.\n"
            "Give me: the ordered steps, the tool you would use for the row copy and why, how JSON columns "
            "are handled, how you verify parity afterwards (exact SQL for row counts and a checksum of a "
            "few rows), the rollback plan, and the two things most likely to fail silently."
        ),
        "output": "An ordered migration plan with verification SQL and a rollback path.",
        "review": [
            "It acknowledges there is no migration tool in the repo",
            "Verification is by count and checksum, not by 'it started'",
        ],
    },
    {
        "id": "api-route",
        "phase": "backend",
        "title": "Write a bounded FastAPI route",
        "when": "Adding or changing an endpoint.",
        "inputs": [
            "The existing route module you are extending",
            "The validation helper it already uses",
        ],
        "prompt": (
            "Add this endpoint to the module I paste: <METHOD AND PATH> — <WHAT IT DOES>.\n"
            "Follow the local conventions: validate and bound every input (max lengths, max list sizes, "
            "allowed value sets) and raise HTTPException(422, '<plain-English reason>') for bad input; "
            "never accept a path from the client that reaches the filesystem without an allowlist; never "
            "return a secret, a token or a raw connection string; keep the response JSON small and "
            "bounded.\n"
            "Return the route function, the validation function with its error strings, and three tests "
            "(happy path, rejected input, boundary value). Match the file's existing import and style."
        ),
        "output": "Route + validation + three tests, in the file's existing style.",
        "review": [
            "Input limits are explicit numbers, not 'reasonable'",
            "No secret field appears in the response",
            "The 422 messages are user-readable sentences",
        ],
    },
    {
        "id": "api-desktop-agent",
        "phase": "backend",
        "title": "Extend the Windows agent bridge safely",
        "when": "You need a new desktop capability (for example an installer step).",
        "inputs": [
            "app/desktop_runner.py (DESKTOP_ACTIONS, validate_desktop_selector, RemoteWindowsAgent)",
            "app/desktop_recording.py (session lifecycle)",
        ],
        "prompt": (
            "I need this desktop capability: <CAPABILITY>.\n"
            "The bridge already validates actions against a fixed set, sends JSON over HTTPS with a bearer "
            "token and bounds screenshots to 512 KiB. Design the change so that:\n"
            "- the new action is in the allowed set and has an explicit selector rule,\n"
            "- the command string sent to Windows is built from a template and an allowlisted argument "
            "shape, never from free text,\n"
            "- without a configured agent the deterministic simulator still runs it, so CI on Linux stays "
            "green,\n"
            "- the response is size-bounded and any log the agent returns is truncated.\n"
            "Return the diff-level plan, the validation rules with their error strings, and the tests, "
            "including one that asserts the simulator path works with no agent configured."
        ),
        "output": "A plan that keeps the simulator path green and the command shape allowlisted.",
        "review": [
            "The simulator branch is exercised by a test",
            "No shell metacharacters can reach the agent command",
        ],
    },
    {
        "id": "ui-tab",
        "phase": "frontend",
        "title": "Add a dashboard tab without a build step",
        "when": "The dashboard needs a new screen.",
        "inputs": [
            "The `tabs` array and the `content-area` render block in app/static/index.html",
            "One existing component file, for example app/static/demo-components.js",
        ],
        "prompt": (
            "Add a '<TAB NAME>' tab to this dashboard. It is plain React with `React.createElement` "
            "aliased to `e`, no JSX, no bundler, no npm install, and Tailwind from a vendored CSS file.\n"
            "The component lives in a new file under app/static, loaded by a `<script>` tag before the "
            "inline script in index.html, and exposes one global function.\n"
            "Return: the new component file, the exact three edits to index.html (script tag, `tabs` "
            "entry, render line), the support-footer line for the tab, and how the tab fetches data "
            "through the existing `API.get`/`API.post` helpers. Keep every string user-readable — labels "
            "are translated by `tfT`, so write plain English labels, not keys."
        ),
        "output": "A new component file plus three precise index.html edits.",
        "review": [
            "No import/export statements or JSX in the new file",
            "The tab appears in `tabs`, in the render block and in TabSupportFooter",
        ],
    },
    {
        "id": "ui-markdown",
        "phase": "frontend",
        "title": "Render a markdown document inside a tab",
        "when": "A tab has to show one of the docs/ guides.",
        "inputs": [
            "The markdown file you want to render",
            "The `parseMdBlocks` and `MdBlocks` definitions in app/static/index.html",
        ],
        "prompt": (
            "Render this markdown document inside a dashboard tab using the existing `parseMdBlocks` and "
            "`MdBlocks` helpers — do not add a markdown library.\n"
            "Split the document on `##` headings while respecting fenced code blocks (a code block can "
            "contain lines that look like headings), give each section a collapse/expand control and a "
            "tick-box whose state is stored in localStorage, and show a progress bar. Fetch the document "
            "through the existing JSON endpoint, handle the 404 case by linking to the file on GitHub, and "
            "surface the `truncated` flag if the server cut the file short."
        ),
        "output": "A section-splitting renderer with per-section progress, using only existing helpers.",
        "review": [
            "Fenced code blocks are not split on headings",
            "The 404 and truncated cases are both handled",
        ],
    },
    {
        "id": "test-module",
        "phase": "testing",
        "title": "Write a test module the harness will actually run",
        "when": "You added behaviour and need it covered.",
        "inputs": [
            "tests/bootstrap.py and the `loadTestsFromNames` list in tests/harness.py",
            "One existing test module as a style reference",
        ],
        "prompt": (
            "Write tests for <WHAT YOU ADDED>.\n"
            "Rules from this repository: call `ensure_test_env()` from tests.bootstrap before importing "
            "anything from `app`, so DATABASE_URL and TF_ARTIFACTS point at a throwaway directory; use "
            "FastAPI's TestClient for route tests; never touch the network, never publish to GitHub, and "
            "never require Chromium unless the test is explicitly a browser scenario.\n"
            "Return the test module, the line to add to the harness's `loadTestsFromNames` list, and the "
            "scenario IDs to add to tests/SCENARIOS.md. Include one test per validation error string the "
            "code raises."
        ),
        "output": "A test module, the harness registration line and the SCENARIOS.md entries.",
        "review": [
            "`ensure_test_env()` runs before the app import",
            "The module is added to the harness list, not just written",
        ],
    },
    {
        "id": "test-failing",
        "phase": "testing",
        "title": "Diagnose a failing test without weakening it",
        "when": "A test fails and it is tempting to change the assertion.",
        "inputs": [
            "The failing test and its full traceback",
            "The code under test",
        ],
        "prompt": (
            "This test fails. Here is the test, the traceback and the code under it.\n"
            "Decide which is wrong: the test's expectation or the code. Explain the evidence. If the code "
            "is wrong, give the fix. If the expectation is genuinely outdated, say why and quote the "
            "behaviour that changed. Do not propose skipping, xfailing, loosening an assertion or "
            "broadening a tolerance unless the expectation is provably wrong, and never propose deleting "
            "the test."
        ),
        "output": "A verdict with evidence, and a fix that keeps the assertion's intent.",
        "review": ["No assertion was weakened to get green", "The reasoning quotes the traceback"],
    },
    {
        "id": "cicd-pipeline",
        "phase": "delivery",
        "title": "Build the CI pipeline for a change",
        "when": "You are wiring GitHub Actions or Azure Pipelines for this repo.",
        "inputs": [
            "requirements.txt and requirements-test.txt",
            "The Testing section of readme.md",
        ],
        "prompt": (
            "Write a CI pipeline for this repository that runs `python -m tests.harness` on every push and "
            "pull request.\n"
            "Constraints: the browser scenarios need Chromium (the harness skips them when it is absent "
            "and `--require-browser` makes that a failure), the unit suite must not need it, no test may "
            "publish to GitHub, and artifacts under `logs/` must be uploaded so a failure can be read "
            "later. Return the YAML, the cache keys, and the two jobs (unit-only fast job, full harness "
            "job). Point out which environment variables must NOT be set in CI and why."
        ),
        "output": "Pipeline YAML with a fast unit job, a full harness job and log upload.",
        "review": [
            "No publishing token is exposed to the test job",
            "The harness exit status is what fails the build",
        ],
    },
    {
        "id": "deploy-azure",
        "phase": "delivery",
        "title": "Deploy the image to Azure and prove it works",
        "when": "A release has to go out.",
        "inputs": [
            "docs/dev-deployment-end-to-end.md",
            "Dockerfile and docker-compose.yml",
            "The commit SHA you are deploying",
        ],
        "prompt": (
            "Deploy commit <SHA> of this service to Azure using the guide and the container files I paste.\n"
            "Give me the ordered commands for: build, tag by digest, push to the registry, pull and "
            "restart on the target host, and the health check that proves the new revision is serving "
            "(the app reports its revision — check it, do not assume). Include the rollback command for "
            "the previous digest, and the exact log lines I should read if the health check fails. "
            "Everything must be idempotent: re-running a step must be safe."
        ),
        "output": "An idempotent deploy sequence with a revision check and a rollback command.",
        "review": [
            "The image is pinned by digest, not `latest`",
            "The health check verifies the deployed revision",
        ],
    },
    {
        "id": "deploy-incident",
        "phase": "delivery",
        "title": "Triage a broken deployment",
        "when": "The service is down or unhealthy after a deploy.",
        "inputs": [
            "The container's last 200 log lines",
            "The output of `/api/health` and `/api/diagnostics`",
        ],
        "prompt": (
            "The service is unhealthy after a deploy. Here are the container logs, `/api/health` and "
            "`/api/diagnostics`.\n"
            "Tell me, in order: is the process up, is the database reachable, is the schema ready, is the "
            "browser runtime available, and which of those is the actual cause. Then give me the single "
            "command that confirms your diagnosis, and the fix. If the evidence is insufficient, say "
            "which extra log line you need instead of guessing. Do not propose a restart as the first "
            "step unless you can say what it would prove."
        ),
        "output": "A ranked diagnosis with a confirming command, not a blind restart.",
        "review": ["The conclusion cites a specific log line", "A restart is justified or not suggested"],
    },
    {
        "id": "sec-secrets",
        "phase": "security",
        "title": "Review a change for secret leakage",
        "when": "A change touches configuration, publishing or logs.",
        "inputs": [
            "The diff",
            "app/errors.py (`redact`) and app/secrets_store.py",
        ],
        "prompt": (
            "Review this diff for secret leakage. The service must never write a token, password or "
            "connection string to a log, an artifact, a recording, an export or an error message; "
            "`redact()` in app/errors.py is the existing filter and secrets are stored encrypted with a "
            "`TF_SECRET_` prefix.\n"
            "List every line where a credential could reach disk, a response body or an exception "
            "message, with the fix for each. Also flag: environment variables echoed into logs, exception "
            "strings passed through without `redact`, and any new file written under the artifacts "
            "directory that could contain a value."
        ),
        "output": "A line-by-line leakage review with a fix per finding.",
        "review": [
            "Every new error path goes through `redact`",
            "No new artifact file can contain a plaintext secret",
        ],
    },
    {
        "id": "sec-gate",
        "phase": "security",
        "title": "Check the access gate before exposing an instance",
        "when": "An instance is about to be reachable from outside a private network.",
        "inputs": [
            "app/access_control.py",
            "Section 0.1 of docs/azure-devops-deployment-guide.md",
            "The planned network exposure (public IP, private endpoint, gateway)",
        ],
        "prompt": (
            "This instance will be reachable as follows: <EXPOSURE>. Using the access-gate code and the "
            "guide section I paste, tell me exactly what must be true before it is safe: token length, "
            "where the token is stored, which routes the gate covers, what happens when the token is "
            "unset, and what the gate does NOT provide (multi-user, RBAC, rate limiting). Then give me "
            "the checklist an operator can tick off, and the single test that proves the gate is on."
        ),
        "output": "A pre-exposure checklist with an explicit list of what the gate does not do.",
        "review": [
            "It states plainly that this is not multi-tenant RBAC",
            "The verification step is a real request, not a config read",
        ],
    },
    {
        "id": "sec-review-pr",
        "phase": "security",
        "title": "Review a pull request like a maintainer",
        "when": "You are reviewing someone else's change before merge.",
        "inputs": ["The PR diff", "The PR description", "The test output from CI"],
        "prompt": (
            "Review this pull request against this repository's rules: one worker and sequential "
            "execution, no new dependency without a stated reason, every input bounded, every route "
            "behind the access gate, no secrets in logs or artifacts, dashboard code without a build "
            "step, and a test that the harness actually runs.\n"
            "Return: blocking issues, non-blocking suggestions, and the questions the author must answer. "
            "Quote the diff lines. If CI is green but no test exercises the changed path, say so "
            "explicitly — a green build that runs nothing is not evidence."
        ),
        "output": "Blocking vs non-blocking findings, each quoting a diff line.",
        "review": [
            "At least one test is shown to execute the changed function",
            "No new dependency slipped in without justification",
        ],
    },
]


def prompt_library() -> dict:
    """The prompt library as structured data for the dashboard."""
    prompts = PROMPTS[:MAX_PROMPTS]
    return {
        "phases": PROMPT_PHASES,
        "prompts": [dict(item) for item in prompts],
        "count": len(prompts),
        "repository": REPO_URL,
    }


def prompt_by_id(prompt_id: str) -> dict | None:
    return next((dict(item) for item in PROMPTS if item.get("id") == prompt_id), None)


def _fence(text: str) -> str:
    """A fenced block that cannot be closed early by the text inside it."""
    longest = 0
    run = 0
    for char in text:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def prompt_markdown() -> str:
    """Render the prompt library as the downloadable ``ai-prompt-library.md``."""
    lines = [
        "# TestForge AI prompt library for the in-house dev team",
        "",
        f"Generated from `app/dev_prompts.py` ({len(PROMPTS[:MAX_PROMPTS])} prompts). "
        "This file is served by the dashboard's **Dev & Deploy** tab and is safe to email: "
        "it contains no credentials, no connection strings and no internal hostnames.",
        "",
        "## How to use a prompt",
        "",
        "1. Copy the prompt block.",
        "2. Replace every `<ANGLE BRACKET PLACEHOLDER>` with your own value.",
        "3. Paste the *Inputs* listed under the prompt into the same message — an AI answer about code "
        "it has not seen is a guess.",
        "4. Check the *Before you accept it* list. If an item cannot be ticked, do not merge the answer.",
        "",
        "Rules that apply to every prompt in this file:",
        "",
        "- Never paste a real password, token or connection string. Use a Key Vault reference or a "
        "placeholder.",
        "- Ask for the file and line the answer is based on. If it cannot name one, it is inventing.",
        "- Treat the answer as a draft. The repository's own harness "
        "(`python -m tests.harness`) is the only proof a change works.",
        "",
    ]
    for phase in PROMPT_PHASES:
        members = [item for item in PROMPTS[:MAX_PROMPTS] if item.get("phase") == phase["id"]]
        if not members:
            continue
        lines.append(f"## {phase['name']}")
        lines.append("")
        lines.append(f"_{phase['detail']}_")
        lines.append("")
        for item in members:
            lines.append(f"### {item['title']}")
            lines.append("")
            lines.append(f"**Prompt ID:** `{item['id']}`")
            lines.append("")
            lines.append(f"**Use it when:** {item['when']}")
            lines.append("")
            lines.append("**Paste these inputs with the prompt:**")
            lines.append("")
            for value in item.get("inputs") or []:
                lines.append(f"- {value}")
            lines.append("")
            fence = _fence(item["prompt"])
            lines.append("**Prompt:**")
            lines.append("")
            lines.append(fence)
            lines.append(item["prompt"])
            lines.append(fence)
            lines.append("")
            lines.append(f"**You should get back:** {item['output']}")
            lines.append("")
            lines.append("**Before you accept it:**")
            lines.append("")
            for check in item.get("review") or []:
                lines.append(f"- [ ] {check}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
