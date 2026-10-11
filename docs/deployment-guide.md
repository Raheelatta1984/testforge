# TestForge Deployment Guide

For senior DevOps engineers deploying this application and its database from scratch, with a
library of **Claude Code** prompts in [§13](#13-claude-code-prompt-library).

Every behaviour claimed here was read out of this repository (`app/config.py`, `app/db.py`,
`app/guardrails.py`, `app/library_store.py`, `app/main.py`, `Dockerfile`, `render.yaml`) rather than
assumed. Platform quotas are marked **[verify]**. Facts worth a second look are marked
**⚠ finding** — those are things that will cost you time precisely because the code disagrees with
what a reasonable reader would expect.

---

## 1. What you are deploying

One process. That is the whole design constraint, and it is not negotiable.

```
uvicorn (1 worker, :8000)
├── FastAPI app  app/main.py            ~70 routes + 3 websockets
├── Playwright → ONE headless Chromium   (recording AND execution share it)
├── SQLAlchemy → SQLite  (default)  or  PostgreSQL (DATABASE_URL)
├── ARTIFACTS/  runs, recordings, logs   ← local disk, must be persisted if you care about them
└── library/    projects + recordings    ← the durable store; published to GitHub
```

| Component | Fact | Where |
| --- | --- | --- |
| Web server | uvicorn, **one worker** | `Dockerfile` `CMD` |
| Browser | Chromium only, bundled in the image | `playwright install --with-deps chromium` |
| DB | SQLite unless `DATABASE_URL` is set | `app/config.py` |
| Schema | created/repaired **at import time** | `init_db()` called at `app/main.py:56` |
| Auth | **none** | §2 |
| Port | 8000, and the image CMD ignores `$PORT` | §7.3 ⚠ |
| Python | 3.11-slim-bookworm | `Dockerfile` |

**Do not add workers, replicas, or an autoscaling group.** `MAX_CONCURRENT_BROWSERS = 1` is hard-coded
in `app/config.py`, and every cap in `app/guardrails.py` is per-process — a second worker doubles the
memory demand of `TF_MAX_BROWSERS`-limited work without doubling the ceiling, and two Chromium
processes will OOM the instance. Scale up a VM, or split the queue consumer out deliberately.

---

## 2. Before you expose it to anything: there is no authentication

`render.yaml` passes `API_Key` to the service. **No line of application code reads it.** The only
`Authorization` header in the package is the one the app sends *out* to GitHub.

```bash
grep -rn "API_Key" app/ || echo "API_Key: never read — decorative"
grep -rn "HTTPBearer\|api_key_header\|Depends(.*auth" app/main.py || echo "no inbound auth"
```

So every endpoint is open to whoever can reach the port, including:

| Endpoint | What a stranger can do |
| --- | --- |
| `POST /api/runs`, `POST /api/runs/batch` | occupy the single Chromium indefinitely → free DoS |
| `POST /api/recordings/{id}/session`, `.../input` | drive a browser **on your host** to arbitrary URLs |
| `POST /api/library/publish` | write commits to your GitHub repo **if `TF_GITHUB_TOKEN` is set** |
| `GET /api/diagnostics` | read your DB dialect, live table columns, memory, guardrail config |
| `GET /api/sync/github` | pull the whole artifact tree |
| `PATCH`/`DELETE /api/variables/{id}` | read/overwrite project variables, which include secrets (§8) |

The project **variables** are worse than plaintext in the database, because they are also *committed to
the repository*: `app/library_store.py` writes them to `library/projects/<id>/variables.json` and the
publishing flow (§9) pushes that file to GitHub. `list_variables()` returns
`"value": variable.get("value")` **unconditionally** — the `is_secret` flag is copied through in five
serialization sites and read by nothing, so it does not redact the API response, the exported file, or
the git object.

### 2.1 ⚠ ACTION NOW: a credential is already public in this repo

`library/projects/1c7a86a9-9525-4bd4-b5f0-df360a7b49f1/variables.json` is tracked and pushed to a
**public** GitHub repository, and it contains a variable named `Extranet_pass` whose value is a
20-character high-entropy string. It is also duplicated as two identical entries in the same file
(a separate dedupe bug).

Treat that credential as **compromised and rotate it at the source system first** — removing it from
the repo does not un-leak it. Then:

1. Rotate/revoke the password on the extranet. Nothing in git does this for you.
2. Purge it from history (`git filter-repo --invert-paths --path library/projects/1c7a86a9-.../variables.json`
   then a force-push, or ask GitHub support to dereference the cached blobs), and note that **any fork,
   clone, Render build log, or CI cache already has it**.
3. Stop the leak at the source, in priority order:
   * don't let the API publisher write secret variables into `library/` at all — exclude
     `variables.json` keys with `is_secret: true`, or move secrets out of the variables table entirely
     and reference them by name (`TF_VAR_<name>`, §8) so only the *name* is committed;
   * redact `value` to `""`/`"••••"` in `list_variables()` when `is_secret` is set, and keep the true
     value server-side for execution only (interpolation reads it at run time — a test that *needs* the
     value must get it from the environment, not from this endpoint);
   * add a pre-commit and CI secret scan (`gitleaks`/`trufflehog`) so the next one is a build failure
     rather than a publication.
4. Verify no other project files hold credentials before you re-enable publishing:
   ```bash
   grep -rniE '"name": *"[^"]*(pass|secret|token|key|pwd)' library/ --include=*.json
   ```

Prompts **P8** (auth) and **P11** (secret handling) in §13 exist for exactly this. Until they land, do
not enable `TF_GITHUB_TOKEN` on any deployment that stores credentials as variables.

**Required posture until the app gains real auth:**

1. Bind the container to loopback: `--host 127.0.0.1` (the image CMD uses `0.0.0.0` — override it).
2. Terminate TLS at a reverse proxy and put the access control there (Entra ID application proxy,
   oauth2-proxy, or mTLS) — §7.4.
3. Firewall the host so 8000 is unreachable from anywhere but the proxy.
4. Never run this on a network segment with access to internal systems, because the browser it drives
   can reach them. That is the actual blast radius: an SSRF machine with a git push credential.

Prompt **P8** in §13 is the one that fixes it properly in-app.

---

## 3. Prerequisites

| Need | Notes |
| --- | --- |
| Linux host, Docker 24+ / Compose v2 | or Python 3.11 if running without Docker |
| 1 vCPU / 1 GB RAM | minimum; 2 vCPU / 4 GB for batches |
| 20 GB disk, persistent | screenshots, videos, exported bundles |
| Outbound 443 | GitHub API + every URL under test. Chromium loads a real page. |
| Chromium deps | already handled by the image; on a bare host, `playwright install-deps chromium` |
| PostgreSQL 14+ | only if you are not accepting SQLite |
| Fine-grained PAT | **Contents: Read and write**, scoped to this one repo, only if publishing is used |

No GPU, no admin rights on the target app, no inbound ports other than 443.

---

## 4. Option A — Docker Compose (recommended default)

The repo ships a working compose file. It mounts a named volume for artifacts, which is the part
people forget:

```bash
git clone https://github.com/Raheelatta1984/testforge.git && cd testforge
docker compose up -d --build          # builds image incl. Chromium: first build is ~3-6 min
docker compose ps
curl -s localhost:8000/api/health | python3 -m json.tool
```

Open `http://localhost:8000`. Note `docker-compose.yml` publishes `8000:8000` on all interfaces — on
a shared or public host, change it to `127.0.0.1:8000:8000` (§2).

### 4.1 The `.env` file

`docker-compose.yml` interpolates `${VAR}` from `.env` in the project directory. Create it, don't
commit it (`.gitignore` already excludes `.env*`, `*.pem`, `*.key`):

```bash
cat > .env <<'ENV'
# Optional: omit for SQLite. Must be a FULL URL including scheme.
DATABASE_URL=postgresql://testforge:CHANGE_ME@db:5432/testforge

# Only set for a deployment that must publish library/ to GitHub (§9).
TF_GITHUB_REPO=your-org/testforge
TF_GITHUB_TOKEN=github_pat_...
TF_GITHUB_BRANCH=main
ENV
chmod 600 .env
```

### 4.2 Adding PostgreSQL to the compose stack

Append to `docker-compose.yml`:

```yaml
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: testforge
      POSTGRES_USER: testforge
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set in .env}
    volumes: [tfpg:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U testforge -d testforge"]
      interval: 10s
      timeout: 5s
      retries: 10
volumes:
  testforge-data: {}
  tfpg: {}
```

and add `depends_on: { db: { condition: service_healthy } }` to the `testforge` service. The healthcheck
matters: without it the app's boot-time `init_db()` runs against a still-initialising Postgres, the
**error is swallowed into `SCHEMA_STATUS` instead of crashing the container** (§5.3), and you get a
running service that 503s on every write.

### 4.3 Pin the image, not the tag

```yaml
    image: registry.example.com/testforge@sha256:<digest>
```
Deploying `:latest` makes rollback impossible and makes "which build is live?" unanswerable —
`/api/health` only reports a revision if `RENDER_GIT_COMMIT` is set (§7.2).

---

## 5. Database: structure and setup

### 5.1 How the connection string is resolved

```python
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{os.path.join(ARTIFACTS, 'testforge_erp.db')}"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
```

Consequences:

* An **unset** `DATABASE_URL` means SQLite. A **malformed** one fails loudly at boot — that is the
  right behaviour, don't "fix" it.
* `postgres://` is accepted and rewritten, so a URL pasted from a portal's "connect" pane works.
* **URL-encode the password.** A `%` or `~` in it becomes an escape sequence in the DSN and fails as a
  confusing host-resolution error:
  `python3 -c "from urllib.parse import quote; print(quote('p@ss%word', safe=''))"`
* `ARTIFACTS` is `TF_ARTIFACTS`, else `~/testforge_titan_data`. ⚠ **finding:** if `TF_ARTIFACTS` is
  unset on a container that *does* have a writable home, SQLite silently lands **outside the mounted
  volume**, so your database is deleted on every redeploy while artifacts persist. Set `TF_ARTIFACTS`
  explicitly and assert it (§10.1).

Pool settings differ by dialect (`app/db.py`):

| Dialect | Pool | Note |
| --- | --- | --- |
| SQLite | `StaticPool`, `check_same_thread=False`, `timeout=30` | one shared connection; correct only for a single process |
| Postgres | `pool_size=20`, `max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=3600` | **up to 30 connections, one process** |

With one worker, 30 is fine on a small server. Do not multiply by worker count. `pool_recycle=3600`
exists because cloud Postgres kills idle connections — keep it.

### 5.2 The structure — 6 tables, generated DDL

There is no `schema.sql` to apply; this is what `create_all()` emits for PostgreSQL, compiled from
the models:

```sql
CREATE TABLE batches (
	id VARCHAR(36) NOT NULL,
	name VARCHAR(200),
	status VARCHAR(50) NOT NULL,
	total INTEGER NOT NULL,
	done INTEGER NOT NULL,
	passed INTEGER NOT NULL,
	failed INTEGER NOT NULL,
	skipped INTEGER NOT NULL,
	progress_pct INTEGER NOT NULL,
	options JSON NOT NULL,
	report JSON NOT NULL,
	error TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id)
);
CREATE TABLE projects (
	id VARCHAR(36) NOT NULL,
	name VARCHAR(200) NOT NULL,
	base_url VARCHAR(500) NOT NULL,
	industry_type VARCHAR(100) NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE recordings (
	id VARCHAR(36) NOT NULL,
	project_id VARCHAR(36) NOT NULL,
	parent_id VARCHAR(36),
	name VARCHAR(255) NOT NULL,
	start_url VARCHAR(1000) NOT NULL,
	tags VARCHAR(500) NOT NULL,
	status VARCHAR(50) NOT NULL,
	video_path VARCHAR(1000),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id) REFERENCES projects (id),
	FOREIGN KEY(parent_id) REFERENCES recordings (id)
);
CREATE TABLE variables (
	id VARCHAR(36) NOT NULL,
	project_id VARCHAR(36) NOT NULL,
	name VARCHAR(100) NOT NULL,
	value TEXT NOT NULL,
	category VARCHAR(50) NOT NULL,
	is_secret BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id) REFERENCES projects (id)
);
CREATE TABLE recording_steps (
	id VARCHAR(36) NOT NULL,
	recording_id VARCHAR(36) NOT NULL,
	"order" INTEGER NOT NULL,
	action VARCHAR(100) NOT NULL,
	selector JSON,
	value TEXT,
	label VARCHAR(500),
	screenshot_path VARCHAR(1000),
	repeat_count INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(recording_id) REFERENCES recordings (id)
);
CREATE TABLE runs (
	id VARCHAR(36) NOT NULL,
	recording_id VARCHAR(36) NOT NULL,
	status VARCHAR(50) NOT NULL,
	progress_pct INTEGER NOT NULL,
	execution_log JSON NOT NULL,
	video_path VARCHAR(1000),
	rog_monitor_log TEXT,
	rog_devops_log TEXT,
	rog_qa_log TEXT,
	batch_id VARCHAR(36),
	started_at TIMESTAMP WITHOUT TIME ZONE,
	cancel_reason VARCHAR(500),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(recording_id) REFERENCES recordings (id)
);
```

Read it carefully, because four things in there are load-bearing for you:

1. **`"order"` is quoted** — a reserved-ish word. Any hand-written SQL against
   `recording_steps` must quote it.
2. **`JSON`, not `JSONB`.** `selector`, `value`(steps), `execution_log`, `options`, `report` are the
   `JSON` type, stored as text: no GIN index, whole-column rewrite on update, and `execution_log`
   grows with every step of every run. That is why the API omits the log by default
   (`TF_RUN_LIST_LIMIT`, and the log is only included when `with_log` is set).
3. **All primary keys are `VARCHAR(36)` UUID strings.** Random insert order → index bloat and page
   splits on large tables. If `runs` reaches millions of rows before you hit
   `TF_RUN_RETENTION_DAYS`, this is why.
4. **Every default is Python-side, not DB-side** (`default=generate_uuid`, `default=list`,
   `default="queued"`), except `created_at`. So a row inserted by hand or by a restore script without
   those columns will violate `NOT NULL`. `server_default` exists only for `created_at`.

### 5.3 ⚠ There is no migration tool — and you should not add one

`init_db()` runs at import (`app/main.py:56`) and calls `_ensure_schema()`:

1. `Base.metadata.create_all(bind=engine)` — creates missing **tables**;
2. `_add_missing_columns()` — adds columns the models declare and the table lacks, **backfilling**
   existing rows via `_backfill_sql()`;
3. `_relax_legacy_columns()` — Postgres-only: drops `NOT NULL` on legacy columns the models no longer
   write (e.g. historical `runs.target_id`, `variables.scope`).

`init_db()` **never raises**; failures are stored in `SCHEMA_STATUS` and surfaced by
`GET /api/diagnostics`. That is deliberate (the dashboard stays up rather than crash-looping) and it
means a broken schema produces a *running, wrong* container. §10.2 is the check that catches it.

The README's own words: "`create_all()` only creates missing tables, so a database created by an
older revision keeps its original columns and every write to it fails until this migration runs."

Implications for you:

* **No Alembic stage in the pipeline.** Adding one without removing the boot repair gives you two
  authorities, and `create_all` will happily create a table your next revision then tries to alter.
* The app role needs **DDL rights** (`CREATE` on a schema) — it cannot be a DML-only least-privilege
  role as shipped.
* Repairs are **add-only**: columns are added and `NOT NULL` relaxed, never dropped. Which makes
  forward-migration and image rollback both safe in the common case (§11).

### 5.4 Provision the role

```sql
CREATE ROLE testforge_app LOGIN PASSWORD '<from your secret store>';
ALTER DATABASE testforge OWNER TO testforge_app;   -- simplest: app owns db + public schema
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT  ALL   ON SCHEMA public TO testforge_app;

-- §5.5: required, not cosmetic
ALTER DATABASE testforge SET timezone = 'UTC';
ALTER ROLE     testforge_app SET statement_timeout = '30s';
ALTER ROLE     testforge_app SET idle_in_transaction_session_timeout = '60s';
```

Owning the database is a conscious compromise so the self-healing schema works. If your standard
forbids it, split into a migration role (used by a one-shot job) and a DML-only runtime role — **and
then also remove the boot-time repair**, or the container logs `DATABASE FATAL ERROR` on every start
and the smoke test must be taught to expect it. Pick one; do not leave it half-done.

### 5.5 ⚠ Timestamps: SQLite is UTC, Postgres is whatever the server says

`created_at` is `server_default=func.now()` on a `TIMESTAMP WITHOUT TIME ZONE`.

* On **SQLite** that compiles to `DEFAULT (CURRENT_TIMESTAMP)` — always UTC.
* On **Postgres** it compiles to `now()`, cast to a naive timestamp **in the session timezone**.

Meanwhile every Python-side timestamp in the app is `datetime.utcnow()` — naive UTC
(`app/run_queue.py:84`, `app/library_store.py:191`). So on a Postgres whose `TimeZone` is e.g.
`Australia/Sydney`, `created_at` is 11 hours ahead of `started_at`/`finished_at` on the *same row*,
and the retention sweep

```python
cutoff = _utcnow() - timedelta(days=RETENTION_DAYS)
```

compares naive-UTC against server-local values. Symptom: runs vanish earlier than
`TF_RUN_RETENTION_DAYS`, or never expire, and batch reports sort strangely — with no error anywhere.

**Fix at provision time** (the `ALTER DATABASE ... SET timezone = 'UTC'` in §5.4) and verify:

```bash
psql "$DATABASE_URL" -c "show timezone;"                      # must be UTC
curl -s $BASE/api/health >/dev/null                            # reconnect to pick it up
psql "$DATABASE_URL" -c "select now(), now()::timestamp, (select max(created_at) from projects);"
```

`ALTER DATABASE` applies to **new sessions only**, and `pool_recycle=3600` means old pooled
connections can linger up to an hour. Restart the app to make it immediate. If rows were already
written under a non-UTC session, they are not retro-corrected — note the switchover time in the
change record.

### 5.6 ⚠ There are zero secondary indexes, and `create_all` will not add them later

```bash
grep -n "index=True\|Index(" app/db.py   # → nothing
```

The only indexes are the six primary keys. Yet the hot queries are:

| Query path | Filter | Today |
| --- | --- | --- |
| `GET /api/projects/{id}/recordings` | `recordings.project_id` | seq scan |
| step loading | `recording_steps.recording_id` | seq scan |
| `GET /api/runs` + retention sweep | `runs.status`, `runs.created_at` | seq scan + sort |
| batch progress | `runs.batch_id` | seq scan |
| variable lookup | `variables.project_id` | seq scan |

Invisible at demo scale, painful at retained-history scale. Two things to know before you fix it:

* **Adding `index=True` to a model does not backfill existing databases.** `create_all` skips existing
  tables entirely, and `_add_missing_columns` only handles *columns*. So you would silently get the
  index on fresh deploys and not on upgraded ones — the worst kind of divergence.
* Therefore create them explicitly, and do it as a documented step:

```sql
CREATE INDEX IF NOT EXISTS ix_recordings_project_id     ON recordings (project_id);
CREATE INDEX IF NOT EXISTS ix_recording_steps_recording_id ON recording_steps (recording_id);
CREATE INDEX IF NOT EXISTS ix_variables_project_id      ON variables (project_id);
CREATE INDEX IF NOT EXISTS ix_runs_recording_id         ON runs (recording_id);
CREATE INDEX IF NOT EXISTS ix_runs_batch_id             ON runs (batch_id);
CREATE INDEX IF NOT EXISTS ix_runs_status_created_at    ON runs (status, created_at);
```

On a live Postgres use `CREATE INDEX CONCURRENTLY` — which **cannot run inside a transaction**, so
`psql --single-transaction` or a Flyway/Alembic-wrapped migration will fail it. Run it as a standalone
statement. Also declare them in the models afterwards (with `index=True`) so fresh databases get them
at creation, and note the deliberate duplication: same index names, both paths idempotent.

### 5.7 Cascade is ORM-only

`cascade="all, delete-orphan"` lives on the SQLAlchemy relationships, **not** in the DDL — there is no
`ON DELETE CASCADE` in any table. So:

```sql
DELETE FROM projects WHERE id = '...';   -- FK violation, or orphaned rows if FKs are unenforced
```

Delete through `DELETE /api/projects/{id}` (or the ORM), never with SQL. If you must clean up with
`psql`, order it children-first: `recording_steps` → `runs` → `batches` → `recordings` → `variables` →
`projects`. On SQLite, foreign keys are only enforced if `PRAGMA foreign_keys=ON` is issued per
connection — it is not here, so an SQLite cleanup with the wrong order leaves orphans **silently**.
Also note the artifact files (screenshots, videos) are not in the database; deleting rows does not
reclaim `TF_ARTIFACTS` bytes.

### 5.8 Moving SQLite → Postgres (the common "we outgrew the demo" path)

```bash
docker compose stop testforge                      # writers must be stopped, not just paused
cp -a /var/lib/testforge/artifacts /var/lib/testforge/artifacts.bak-$(date +%F)

pipx install pgloader                                # handles types + the JSON columns
pgloader --type table --include "projects variables recordings recording_steps runs batches" \
         sqlite:///var/lib/testforge/artifacts/testforge_erp.db \
         postgresql://testforge_app:...@host:5432/testforge
```

Then, in order, before serving traffic:

```bash
psql "$NEW" -c 'select count(*) from projects;' -c 'select count(*) from runs;'   # compare to sqlite
psql "$NEW" -c "set timezone='UTC'; select max(created_at) from runs;"            # §5.5
```

Start the app pointing at Postgres and let the boot repair align any column drift
(`/api/diagnostics` → `schema.repairs` lists exactly what it changed). Then **re-point `TF_ARTIFACTS`
at the same path** — recording JSON lives in `library/` (git) and screenshots/video in `ARTIFACTS`
(disk). A DB-only migration leaves runs "passing" with broken screenshot links, which is a confusing
failure to debug from the UI. If pgloader is unacceptable in your environment, the supported
alternative is: start a fresh empty Postgres (let `init_db()` create the schema), and treat SQLite as
throwaway. Decide deliberately which of the two you are doing — a demo's history is worth less than a
quiet migration.

---

## 6. On-disk layout

```
$TF_ARTIFACTS/                 # TF_ARTIFACTS, else ~/testforge_titan_data
├── testforge_erp.db           # SQLite lives HERE, not beside the code
├── runs/                      # screenshots, live frames, per-run output
├── recordings/                # recorder scratch
└── system_logs/               # LOGS_DIR (app/config.py)
/app/library/                  # project + recording JSON; the git-published store (§9)
/app/logs/                     # harness reports baked into the image (Dockerfile COPY logs)
```

Persistence rules:

* `TF_ARTIFACTS` **must** be a volume. Losing it costs run evidence and screenshots, and — per §5.1 —
  possibly the whole SQLite DB.
* `/app/library` inside the container is **baked from the image**, so it is a snapshot of build time.
  Writes at runtime are local to the container and only become durable when published to GitHub (§9).
  If publishing is off, expect recordings to disappear on the next deploy, and say so to users.
* Disk grows monotonically. Nothing deletes `runs/` on its own — `TF_RUN_RETENTION_DAYS` prunes
  *database rows*, and the sweeper is the only janitor. Bound it at the filesystem level too
  (`logrotate`-style, or a cron that removes files older than N days from `runs/`).
* Set container logging limits (`logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}`)
  or the OS disk fills in a month.

---

## 7. Configuration reference

### 7.1 The complete environment surface — 62 variables

Produced by reading every call site, because **20 of these appear in no documentation and are read via
a helper rather than `os.environ.get`**:

```bash
grep -rhoE '"TF_[A-Z0-9_]+"' app/*.py | tr -d '"' | sort -u | wc -l    # 62
```

**Core**

| Var | Default | Effect |
| --- | --- | --- |
| `DATABASE_URL` | unset → SQLite under `TF_ARTIFACTS` | §5.1 |
| `TF_ARTIFACTS` | `~/testforge_titan_data` | root for runs/recordings/DB — **set it** (§5.1 ⚠) |
| `PORT` | `8000` | honoured by `start.sh` only, **not** by the image CMD (§7.3 ⚠) |
| `RENDER_GIT_COMMIT` | `local` | the *only* source of `revision` in health/diagnostics (§7.2) |
| `TF_LIBRARY_DIR` | `<repo>/library` | relocates the library tree (the harness uses a copy) |
| `TF_LOGS_DIR` | `<repo>/logs` | local log reader root |
| `TF_LOGS_BRANCH` | checked-out branch | branch the Logs tab reads |

**Browser**

| Var | Default | Effect |
| --- | --- | --- |
| `TF_MAX_BROWSERS` | `1` | `_int`-clamped, `max(1,…)` — **you cannot set 0** (§7.5) |
| `TF_BROWSER_MODE` | `auto` (`bundled` in image) | bundled vs system vs MCP |
| `TF_BROWSER_SINGLE_PROCESS` | `1` | one process, no zygote — required on a 512 MB instance |
| `TF_CHROMIUM_PATH` | unset | explicit binary (also used by the harness, before the bundled one) |
| `TF_CHROMIUM_LIBS` | unset | extra `LD_LIBRARY_PATH` entries for a non-Distroless host |
| `TF_VIEWPORT_WIDTH` / `_HEIGHT` | `1024` / `640` | recorder canvas |
| `TF_ENABLE_VIDEO` | `0` | `1` records a WebM per run |
| `TF_NO_VIDEO` | unset | `1` in bundled mode forces video off entirely |

**Live preview cost**

| Var | Default | Effect |
| --- | --- | --- |
| `TF_PREVIEW_INTERVAL` | `0.18` | seconds between preview frames |
| `TF_VIEWER_RECHECK` | `0.15` | idle viewer recheck |
| `TF_JPEG_QUALITY` | `35` | preview JPEG quality |
| `TF_LIVE_FRAME_TTL` | `120` | seconds before a frame is dropped |
| `TF_MAX_LIVE_FRAME_RUNS` | `4` | runs keeping a last frame in memory |
| `TF_RUN_EXCERPT` | `1` | `0` drops per-step body text from the audit |
| `TF_MAX_RUN_BUFFER_RUNS` / `_EVENTS` | `8` / `60` | broadcast buffer bounds |

**Queue, batch, export**

| Var | Default | Effect |
| --- | --- | --- |
| `TF_QUEUE_ORPHAN_GRACE` | `120` | grace before a restart orphans a run |
| `TF_QUEUE_STALE_MINUTES` | `30` | stale-run cancel threshold |
| `TF_QUEUE_SWEEP_SECONDS` | `300` | sweeper cadence |
| `TF_RUN_RETENTION_DAYS` | `14` | row retention (§5.5 — timezone!) |
| `TF_RUN_LIST_LIMIT` / `TF_RUN_LIST_MAX` | `25` / `100` | list cap, and the ceiling a caller may request |
| `TF_MAX_BATCHES` | `1` | concurrent batches |
| `TF_BATCH_MAX_RECORDINGS` | `40` | per batch |
| `TF_BATCH_MAX_RSS_MB` | `420` | browser relaunch threshold — leave at 420 on 512 MB |
| `TF_BATCH_SCREENSHOTS` | `failure` | `all` / `failure` / `off` |
| `TF_BATCH_SHARE_SESSION` | `1` | same-origin recordings share a context (keeps a login warm) |
| `TF_BATCH_RETRY_TRANSIENT` | `1` | retry transient step failures |
| `TF_BATCH_LOG_ENTRIES` / `TF_BATCH_REPORTS` | `200` / `40` | report bounds |
| `TF_MAX_ZIP_BYTES` | `67108864` | export refuses beyond this |
| `TF_MERGE_REPEAT_STEPS` | `1` | `0` = one row per action, no collapsing |
| `TF_EXPORT_FLUSH_TIMEOUT` | `10` | seconds `stop()` waits for the debounced export |

**Publishing (⚠ §12 — several are redundant or dead)**

| Var | Default | Effect |
| --- | --- | --- |
| `TF_LIBRARY_PUBLISH` | unset | master switch; see `publish_mode()` (§9) |
| `TF_GITHUB_REPO` | unset | `owner/name` — required for API publishing |
| `TF_GITHUB_TOKEN` | unset | fine-grained PAT, **Contents: Read and write** |
| `TF_GITHUB_BRANCH` **or** `TF_GIT_BRANCH` | `main` | read as a *pair* in `app/github_api.py:297` |
| `TF_GIT_REMOTE` **or** `TF_GITHUB_URL` | unset | second fallback chain (`app/github_api.py:289`) |
| `TF_GITHUB_API` | `https://api.github.com` | proxy / Enterprise host |
| `TF_GITHUB_TIMEOUT` | `25` | HTTP timeout |
| `TF_GITHUB_MAX_FILES` | `400` | push bound |
| `TF_GITHUB_MAX_FILE_BYTES` | `2097152` | per-file bound |
| `TF_GITHUB_MAX_PUSH_BYTES` | `25165824` | total push bound |
| `TF_PUBLISH_RETRY_SECONDS` | `60` | retry cadence |

**Logging / CI**

| Var | Default | Effect |
| --- | --- | --- |
| `TF_MAX_LOG_FILE_BYTES` | `262144` | local log reader tail cap |
| `TF_MAX_LOG_INDEX_ROWS` | `40` | index rows |
| `TF_CICD_INTERVAL` | `300` | heartbeat seconds |

**Only consumed by `app/agent.py`, which is imported by nothing** — setting these changes nothing:
`TF_MCP_CMD` (`npx`), `TF_MCP_ARGS`, `TF_AGENT_MODEL` (`claude-sonnet-4-5`), `TF_AGENT_MAX_ITERS` (`40`),
and `ANTHROPIC_API_KEY` (which exists only to compute `DEMO_MODE`). See §12.

**Variable interpolation is a separate mechanism:** `TF_VAR_<name>` env vars are read at *run* time to
fill `{{name}}` placeholders in generated tests (§8). `grep -rn TF_VAR_ app/`.

### 7.2 Make the deployment identifiable

`/api/health` reports `revision` from `RENDER_GIT_COMMIT` and **nothing else** — Render injects it, so
every other platform shows `local`, and you lose the ability to tell two environments apart. Set it
explicitly from your build:

```yaml
    environment:
      RENDER_GIT_COMMIT: ${GIT_SHA}     # `git rev-parse --short HEAD`
```
It is read at request time by `os.environ.get`, so no restart is needed to *use* it, but the container
must be restarted to *change* it.

### 7.3 ⚠ The image hard-codes port 8000; `PORT` is not honoured

```dockerfile
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

`start.sh` does honour `PORT`, but **`start.sh` is never `COPY`'d into the image** — only `app/`,
`library/` and `logs/` are. So:

* On a PaaS that assigns a random `PORT` (Render does), a Docker-based service will not bind it; it
  works on Render because the platform maps the `EXPOSE 8000` container port.
* In Kubernetes, if you set `targetPort: 8080` or `PORT=8080`, **nothing listens there**.
* To change the port, override the command:
  ```yaml
  command: ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8080", "--workers", "1"]
  ```
  Keep `--workers 1` (§1). Loopback bind is §2's requirement.

Consider adding `COPY start.sh /usr/local/bin/` + `ENTRYPOINT` upstream so `PORT` works as documented;
until then, always pin 8000 in the service definition.

### 7.4 Reference proxy config (TLS + the only access control you have)

```
testforge.example.com {
    encode zstd gzip
    @blocked not remote_ip 203.0.113.0/24
    respond @blocked 403 "forbidden"
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1     # REQUIRED: /ws/record/{rid}, /ws/runs/{id}, /ws/batches/{id}
    }
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
}
```

`flush_interval -1` is not a micro-optimisation. The recorder streams frames over WebSocket and falls
back to HTTP polling when a proxy drops them; a proxy that **buffers** produces a UI that looks
connected but shows a frozen frame, and every log line is clean. If the canvas freezes while clicks
still register, this is the cause.

### 7.5 ⚠ How values are parsed — typos fail open, not loud

| Helper | Behaviour | Trap |
| --- | --- | --- |
| `_int` (`guardrails.py`) | non-integer → **silently uses the built-in default**; result clamped by `max(1, …)` | `TF_MAX_BROWSERS=two` → 1, no error. `TF_MAX_LIVE_FRAME_RUNS=0` → **1**, not 0. You cannot disable a numeric guardrail. |
| `_flag` | true **only** for `1`,`true`,`yes`,`on` (case-insensitive) | `TF_BATCH_SHARE_SESSION=y` → **False**. `=enable` → False. |
| `_float(... or default)` | empty/`0` string → default | `TF_RUN_RETENTION_DAYS=0` becomes **14**, not "purge everything" |

So after any config change, verify it **landed**, don't verify the string in the YAML — §10.1 does that
from the running process, which is the only authority that matters.

---

## 8. Secrets for generated tests (`TF_VAR_*`)

The Library exports runnable Playwright Python/JS, a Jenkinsfile, a Gherkin feature, and CSV/XLSX
templates for Azure DevOps/Jira/TestComplete. Placeholders are **not** inlined into the generated
code — `app/export_formats.py` emits a lookup instead:

```js
if (!(`TF_VAR_${name}` in process.env)) throw new Error(`Missing TF_VAR_${name}`);
return process.env[`TF_VAR_${name}`];
```

This is the good design: a downloaded test contains no credentials. The operational consequence is
that **every CI job running an exported test must supply one env var per variable**, and the export
includes a hint to `set TF_VAR_<name> environment variables for placeholders`.

In a Jenkins/Azure Pipelines job, inject from the secret store — never echo:

```groovy
withEnv(["TF_VAR_username=${env.TF_VAR_USERNAME}",
         "TF_VAR_password=${env.TF_VAR_PASSWORD}"]) { sh 'node tests/playwright_test.js' }
```

Two consequences to keep in mind:

* The source of truth for those names is the project's **variables** table, which is plaintext (§2).
  Anyone with `GET /api/variables` learns *which* secrets a run needs, and their values.
* A missing `TF_VAR_x` fails the test at runtime with a clear `Missing TF_VAR_x` — good — but a
  *wrong* one just fails a step. When an exported test fails only in CI and passes in the recorder,
  diff the variable **names** first: `TF_VAR_` keys are matched on the variable's `name`, which is
  editable in the UI and not validated against anything.

---

## 9. Publishing `library/` to GitHub

`publish_mode()` (`app/library_store.py:87`) resolves in this order:

| # | Condition | Mode | Meaning |
| --- | --- | --- | --- |
| 1 | `TF_LIBRARY_PUBLISH` set but falsy | `disabled` | explicit off |
| 2 | a `.git` at or above `library/` | `checkout` | commits and `git push` with the git binary |
| 3 | `TF_GITHUB_REPO` **and** `TF_GITHUB_TOKEN` | `api` | commits via the GitHub REST API |
| 4 | otherwise | `disabled` | and `publish_disabled_reason()` says *why* |

The Docker image has no `.git` (it's built from `app/`+`library/`+`logs/`), so containerised
deployments get **`api`** mode. Mounting a checkout flips you to **`checkout`**, which needs credentials
on the host *and* starts competing with whatever CI pushes to the same branch.

**Rules:**

1. **Exactly one environment holds `TF_GITHUB_TOKEN`.** If two both publish to `main`, each builds a
   tree from its own disk state; one gets a 409 and — per the README — reports *"did not complete and
   will be retried in 60s"*, so it retries forever while silently alternating winners.
2. **CI never pushes to `library/`.** A pipeline that commits generated library output races a human
   saving through the dashboard.
3. A `403 Resource not accessible by personal access token` on `POST /repos/{o}/{r}/git/blobs` is a
   **permission**, not an outage: fine-grained token needs *Contents: Read and write* (read-only reads
   fine and refuses every upload); classic needs the `repo` scope; org with SSO needs *Configure SSO*
   approved; GitHub App needs Contents RW + installation. Full procedure:
   [`docs/github-publishing-security-runbook.md`](github-publishing-security-runbook.md).
4. After rotating the token, use **CHECK TOKEN PERMISSIONS** in the GitHub tab
   (`GET /api/github/access`) instead of waiting for the next save to fail.
5. If you do not want runtime writes to git history at all, `TF_LIBRARY_PUBLISH=0` is a supported,
   honest state — the UI reports publishing disabled rather than promising a retry.

Verify the effective mode:

```bash
curl -s $BASE/api/library | jq '{publish_mode, publish_enabled: .library_publish_enabled, publish_disabled_reason, library_dir, projects_on_disk}'
curl -s $BASE/api/library/publish/plan | jq        # diffs this instance against the remote branch
```

---

## 10. Verification — do this on every deploy

### 10.1 Health: the process agrees with your config

```bash
BASE=https://testforge.example.com
curl -sf $BASE/api/health | jq
```

Required keys (all from `app/main.py:252`):

```json
{ "status": "ok", "database": "ok",
  "recorder": true, "executor": true, "batch_executor": true,
  "revision": "<your sha, not 'local'>",
  "library_publish_enabled": false, "library_publish_mode": "disabled",
  "guardrails": { "...": "live values" } }
```

`/api/health` returns **503 with "Database is unavailable"** when `SELECT 1` fails — which is precisely
why `healthCheckPath: /api/health` beats `/` (the static root says nothing about DB or browser). The
`guardrails` block is the anti-typo check from §7.5: assert the numbers **the running process** resolved
match your intent, e.g.

```bash
[ "$(curl -s $BASE/api/health | jq -r .guardrails.limit)" = "1" ] || echo "browser cap not 1"
```

### 10.2 Diagnostics: the schema is actually right

```bash
curl -s $BASE/api/diagnostics > d.json
jq -e '.schema.ready==true'            d.json >/dev/null || echo "FAIL: schema not ready"
jq -e '.schema.error==null'           d.json >/dev/null || echo "FAIL: $(jq -r .schema.error d.json)"
jq -r '.schema.repairs[]'             d.json                    # what it just migrated
jq -r '.dialect, (.tables|keys)'      d.json                    # postgresql, and all 6 tables
jq -r '.memory'                       d.json                    # RSS vs cgroup ceiling
```

⚠ `diagnostics` returns HTTP 200 with a **hard-coded `"status":"ok"`** that is emitted before any of
these checks run. Asserting on 200 or on `status` proves only that the process is alive. `schema.ready`
and `schema.error` are the fields that report a broken deployment — because `init_db()` never raises
(§5.3), the endpoint is the *only* place a failed schema migration surfaces.

Also assert the negatives:

```bash
curl -sfo /dev/null http://<host>:8000/api/health && echo "FAIL: 8000 is exposed (§2)"
curl -s  -o /dev/null -w '%{http_code}\n' -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  $BASE/ws/runs/00000000-0000-0000-0000-000000000000     # want 101/403/404, not 200 or 502
```

### 10.3 The real suite (this is not a stub)

```bash
python -m tests.harness                    # unit suite + library scenarios, from repo root
python -m tests.harness --require-browser   # a missing Chromium is a FAILURE, not a skip
```

`tests/harness.py` executes the same recorder and runner the dashboard uses against `library/`'s
recordings, writes a timestamped report to `logs/<YYYYMMDD-HHMMSS>/` and indexes it in
`logs/index.md`. Exit 0 only when every executed scenario passed. Scenario IDs: `tests/SCENARIOS.md`
(browser-dependent: `LB-REC-*`, `LB-RUN-*`, `LB-BAT-01`).

Two facts that matter in CI:

* **No harness run can publish.** The harness strips `TF_GITHUB_TOKEN`, `GITHUB_TOKEN`, `GH_TOKEN`,
  `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and `TF_GIT_REMOTE` from the server env it starts. Safe by
  design; don't "fix" it to test publishing end-to-end against the real repo.
* Browser resolution order is `TF_CHROMIUM_PATH` → Playwright's bundled browser → `/tmp/cr/chromium`.
  In a container that has the bundled one, do nothing. If you see skips, run `--require-browser` to
  turn them into a loud failure.
* `python -m unittest tests.test_unit -v` is the fast loop with no browser at all (the batch executor
  is unit-tested against an in-memory browser double).

### 10.4 The two manual checks automation can't do

1. **Record → save → replay, end to end.** Open a project → record tab → leave the URL blank to use the
   built-in sample app → click the picture to click, type with the text box → SAVE → RUN from the
   Library. This is the only test that exercises containerised Chromium + live screencast + the publish
   path simultaneously. Acceptance criteria: `docs/qa-automation-acceptance.md`.
2. **Confirm the publish reached GitHub**, if publishing is meant to be on: the tab verifies the remote
   SHA (`ls-remote`), and *"Published to owner/name@branch"* is the only acceptable result.

---

## 11. Sizing, rollback, backup

### 11.1 Sizing

| Role | Resources | Settings |
| --- | --- | --- |
| Demo (Render free) | 0.1 CPU / 512 MB | `TF_BROWSER_SINGLE_PROCESS=1`, viewport 1024×640, quality 30, `TF_BATCH_MAX_RSS_MB=420`. Do not raise any of these. |
| Small team | 2 CPU / 2 GB | viewport 1280×720, quality 40, `TF_MAX_LIVE_FRAME_RUNS=6` |
| Real automation | 4 CPU / 8 GB | `TF_BATCH_MAX_RSS_MB` up to ~3000, `TF_BATCH_MAX_RECORDINGS` up, Postgres (not SQLite) |

Chromium's own footprint is the ceiling; if RSS approaches the container limit, the *browser* gets
killed first and you see runs failing with "target closed" rather than an OOM message naming Python.
Render free spins down after ~15 min idle with a 30–60 s cold start **[verify]**, and its 750
instance-hours are **per workspace** **[verify]** — so a keep-alive pinger doesn't dodge the cold start
for free, it spends the whole monthly allowance. Either accept sleep or pay; not both.

### 11.2 Rollback

Redeploy the previous **digest** (possible only because §4.3 pins by digest). The database needs no
rollback in the common case: repairs are add-only, so a newer image that widened the schema still
serves an older one — extra nullable columns are ignored. Read `schema.repairs` from the *previous*
deploy as your audit trail of what changed underneath you. Two exceptions that do need thought:

* `library/` commits already pushed to GitHub are **not** rolled back with the image.
* If a release changed a column's *semantics* (not just its existence), data written by it stays.

### 11.3 Backup

Three separate things, and a "backup" that only covers one of them is how you lose the other two:

| What | Where | How |
| --- | --- | --- |
| Records + recordings as JSON | GitHub `library/` | already the durable store; keep a second remote if that matters |
| Relational state | Postgres | `pg_dump -Fc`, or the managed service's PITR |
| Screenshots/video/exports | `TF_ARTIFACTS` | `restic`/`rsync` — the only one most teams forget |

```bash
# SQLite path — snapshot, never cp a live file
sqlite3 "$TF_ARTIFACTS/testforge_erp.db" ".backup '$BK/testforge-$(date +%F).db'"
pg_dump "$DATABASE_URL" -Fc -f "$BK/testforge-$(date +%F).dump"
```

Retain ≥ `TF_RUN_RETENTION_DAYS` (14) or you will restore a database whose rows point at deleted
screenshot files. Restore into a **fresh** database and let `init_db()` align it (§5.3), rather than
restoring over one a newer image already migrated.

---

## 12. ⚠ Known configuration debt

Documented so you don't waste hours, and because a deployment guide that hides these is worthless:

1. **`API_Key` does nothing** (§2). Do not build a security control on it or spend time rotating it.
2. **`docker-compose.yml` sets `ANTHROPIC_API_KEY` and `TF_MCP_ARGS`, both inert.** Their only consumer
   is `app/agent.py`, which is imported nowhere and — per `requirements.txt` — needs `anthropic` and
   `mcp`, which were deliberately removed and fail on import
   (`cannot import name 'Scenario' from app.db`). Consequence: **`DEMO_MODE` has no runtime effect**,
   and AI-driven scenarios in the UI are not what the README's `TF_AGENT_*` vars suggest.
3. **`start.sh` is not in the image** (§7.3), so it is host-run only and its `PORT` handling does not
   apply to container deployments.
4. **`logs/` and `library/` are baked into the image** (`Dockerfile`), which makes the image
   self-bootstrapping for the Logs and GitHub tabs but also means **the image is not reproducible**:
   the same commit built later differs if the working tree differed. Build from a clean checkout, and
   if you need bit-identical images, stop copying `logs/`.
5. Some `TF_*` names are read as **fallback pairs** (`TF_GITHUB_BRANCH`/`TF_GIT_BRANCH`,
   `TF_GIT_REMOTE`/`TF_GITHUB_URL`), so an audit that greps for a single `environ.get("NAME")` will
   wrongly conclude a variable is dead. Enumerate with:
   ```bash
   grep -rhoE '"TF_[A-Z0-9_]+"' app/*.py | tr -d '"' | sort -u
   ```
   and for each, `grep -rn "NAME" app/`.

---

## 13. Claude Code prompt library

These are written for **Claude Code** (agentic: it reads files, runs commands, edits, and can verify
its own work) rather than for a chat window. Each prompt gives it the constraints it cannot infer from
the code — which is where the value is. Paste into `claude` from the repo root, or run headless:

```bash
claude                                    # interactive: paste a prompt
claude -p "$(cat prompt.txt)" --allowedTools "Bash(git *)" "Read" "Edit"   # headless, CI-safe
```

**First, give it project memory.** Create `CLAUDE.md` in the repo root so every session starts with the
constraints instead of rediscovering them (this is the highest-value single artifact here):

```markdown
# TestForge — constraints for automation work

## Deployment shape (never violate)
- ONE uvicorn worker, ONE Chromium per process. `MAX_CONCURRENT_BROWSERS = 1` is hard-coded in
  app/config.py. Never add --workers, replicas, or an autoscaling group.
- The image CMD hard-codes --port 8000 and ignores $PORT; start.sh is NOT in the image.
- The app has NO authentication. `API_Key` in render.yaml is read by nothing. Never bind a container
  to 0.0.0.0 without a proxy-side access control in front.
- TF_ARTIFACTS must be a persistent volume, and must be set explicitly, or SQLite lands outside it.

## Database
- No Alembic. init_db() runs at import (app/main.py:56): create_all + add missing columns (backfilled)
  + relax legacy NOT NULL (postgres only). It never raises; failures surface only via /api/diagnostics
  schema.ready/schema.error. Do not add a migration tool without removing the boot repair.
- 6 tables only: projects, variables, recordings, recording_steps, runs, batches.
  All PKs VARCHAR(36) UUID. JSON (not JSONB). "order" is a quoted identifier.
- No secondary indexes exist. create_all will NOT add them to existing tables, so index changes need
  explicit CREATE INDEX (CONCURRENTLY) statements as well as model declarations.
- Timestamps: created_at is server-side now() (session TZ) while Python writes utcnow(). A non-UTC
  Postgres session timezone silently breaks TF_RUN_RETENTION_DAYS. Set ALTER DATABASE ... SET timezone='UTC'.
- FK cascade is ORM-only, not in DDL; on SQLite FKs are unenforced. Never delete with raw SQL.

## Config parsing
- guardrails._int: non-integer silently falls back to the default, and max(1,...) means 0 is impossible.
- _flag: true ONLY for 1|true|yes|on (case-insensitive). "y"/"enable" are false.
- Some vars are fallback pairs read in a loop (TF_GITHUB_BRANCH|TF_GIT_BRANCH, TF_GIT_REMOTE|TF_GITHUB_URL).
  Grep for the bare string, not environ.get("NAME", before concluding a variable is unused.

## Publishing
- publish_mode(): TF_LIBRARY_PUBLISH falsy → disabled; .git above library/ → checkout;
  TF_GITHUB_REPO+TF_GITHUB_TOKEN → api; else disabled.
- Exactly ONE environment may hold TF_GITHUB_TOKEN, or two deployments race on main.
- CI must never push to library/. A failed push is reported as pending and retried at
  TF_PUBLISH_RETRY_SECONDS; 401/403 means the token scope, not an outage (docs/github-publishing-security-runbook.md).

## Verify with (always, before declaring done)
- `python -m tests.harness --require-browser`  → exit 0. A missing browser must be a failure, not a skip.
- `curl -s localhost:8000/api/health | jq`     → database:"ok", revision != "local", guardrails as intended
- `curl -s localhost:8000/api/diagnostics | jq .schema`  → ready:true, error:null
- Do not assert on /api/diagnostics HTTP 200 or .status — both are hard-coded ok.
```

### P1 — Deploy from a clean host (the "do the whole thing" prompt)

> Deploy this repository on the current machine as a persistent service and prove it works. Steps, in
> order, and stop at the first failure rather than improvising around it:
>
> 1. Read `CLAUDE.md`, `Dockerfile`, `docker-compose.yml`, `render.yaml`, `app/config.py` before changing
>    anything. Report in one line what the constraint is on workers and browsers, so I know you read it.
> 2. Prereq check: Docker + Compose v2, ≥1 GB free RAM, ≥20 GB free disk on the volume you will use.
>    Abort with the specific missing item if any fail.
> 3. Create `.env` with `TF_ARTIFACTS=/var/lib/testforge/artifacts` (create that dir first, mode 0750)
>    and leave `DATABASE_URL` unset so SQLite is used — I want the simplest deployment that works.
>    `TF_GITHUB_TOKEN` must NOT be set; publishing stays disabled and that is the desired state.
> 4. Bind to loopback: the compose file must publish `127.0.0.1:8000:8000`, not `8000:8000`.
> 5. Build and start it. Then run all three verification commands from `CLAUDE.md`'s "Verify with"
>    section and paste their output.
> 6. Make it survive a reboot: a systemd unit for `docker compose up` with `Restart=always`, plus
>    `journalctl` limits so the log cannot fill the disk.
> 7. Finish with: the exact URL, the artifact path, what is NOT configured and why (TLS, auth,
>    publishing), and the single command I run to confirm it is still healthy at 9am tomorrow.
>
> Hard rules: no secrets in files you commit; do not modify `app/`; do not add workers, replicas or an
> init container; if the health check reports `database` anything but `ok`, stop and explain rather
> than restarting until it passes.

### P2 — Set up PostgreSQL correctly (the one that catches §5.5/§5.6)

> Stand up PostgreSQL for this app and point it at it, handling every database-specific trap in
> `CLAUDE.md`. Deliver, in this order:
>
> 1. A `docker-compose.override.yml` adding a `postgres:16-alpine` service with a named volume and a
>    `pg_isready` healthcheck, and `depends_on: condition: service_healthy` on the app — explain in a
>    comment why the healthcheck is mandatory here (the boot-time schema error is swallowed, not fatal).
> 2. The role/grant SQL: the app needs DDL because `init_db()` runs `create_all` + `ALTER TABLE` at
>    import, so a DML-only role will not work. Say so explicitly, and give the stricter split
>    (migration role + DML role) as an alternative with the extra step it requires.
> 3. `ALTER DATABASE testforge SET timezone = 'UTC'` with the reason (SQLite uses CURRENT_TIMESTAMP,
>    Postgres uses now() in the session timezone, Python writes utcnow(), and the retention sweeper
>    compares them) and the verification SQL that proves it took effect for the app's pooled
>    connections — including that `pool_recycle=3600` delays it and why a restart is needed.
> 4. The six `CREATE INDEX IF NOT EXISTS` statements for the FK and status/created_at lookups, plus an
>    explanation of why declaring `index=True` in the models is NOT sufficient for an existing database,
>    and why `CONCURRENTLY` cannot run inside a transaction.
> 5. Migrate the data from the existing SQLite file with a documented tool, then prove the migration by
>    comparing row counts per table and `max(created_at)` before/after.
> 6. Switch `DATABASE_URL`, restart, and show me `/api/diagnostics` with `.dialect == "postgresql"`,
>    `.schema.ready == true` and the `.schema.repairs` list interpreted (what it changed and whether
>    that is expected on a migrated DB vs a fresh one).
>
> Do not introduce Alembic. Do not add `ON DELETE CASCADE` to the DDL — cascade is ORM-only by design.
> Back up the SQLite file before touching it, and tell me the filename.

### P3 — Dockerfile hardening

> Improve `Dockerfile` for production without breaking the constraints in `CLAUDE.md`. For each change,
> show the diff and the one-line reason. Goals, in priority order:
>
> 1. Non-root runtime user. Verify nothing writes outside `TF_ARTIFACTS` — check every directory the
>    process touches at import (`app/config.py` makedirs at import time) and the paths the image already
>    creates, and set ownership accordingly. Tell me if any path would break.
> 2. `--workers 1` must stay, and the port must stay 8000 unless you also fix that `$PORT` is ignored
>    (start.sh is not copied in). Decide: either copy `start.sh` and use it as ENTRYPOINT so `PORT`
>    works, or leave CMD alone and document it. Explain the tradeoff; do not do neither or both.
> 3. Multi-stage to cut image size, keeping `playwright install --with-deps chromium` in the final stage
>    with `rm -rf /var/lib/apt/lists/*`. Chromium is the point of the image — never drop it or add
>    Firefox/WebKit.
> 4. OCI labels for revision/build, and an `HEALTHCHECK` hitting `/api/health`.
> 5. Pin `python:3.11-slim-bookworm` by digest, and pin `requirements.txt` (already pinned — confirm).
> 6. Add a build-time smoke step that imports `app.main` so an import error fails the build instead of
>    the boot — and note what it does NOT catch (browser launch at runtime).
>
> Then prove it: rebuild, run the container, paste `docker image inspect` size before/after, the
> `id` the process runs as, and a passing `python -m tests.harness --require-browser`. Flag that the
> image embeds `library/` and `logs/` (so it is not reproducible across checkouts) and recommend
> whether to keep that — do not silently change it.

### P4 — CI/CD

> Create a CI pipeline for this repo (state which system you chose and why for a small team) plus a
> CD job that deploys to the host from P1. Requirements:
>
> 1. Build the image, then run `python -m tests.harness --require-browser` **inside that image** so the
>    test browser equals the production browser. Do not `pip install` onto a bare runner and call it
>    tested. Cache the Chromium layer; report expected cold and warm runtimes.
> 2. Push only from the default branch, only with an immutable tag (commit SHA); never `latest`-only —
>    deploy by digest so rollback is possible.
> 3. Secrets: the pipeline must not hold `TF_GITHUB_TOKEN`. It builds images and reads code; publishing
>    is the runtime's job (§9). Assert this and fail the pipeline if the token is present in the
>    runtime env of a test stage (the harness strips it on purpose — keep that).
> 4. Post-deploy gate that curls `/api/health` and `/api/diagnostics` and fails on `database!="ok"`,
>    `schema.ready!=true`, or `revision=="local"` (the last means the revision wasn't injected, §7.2).
> 5. A drift check that fails if `Dockerfile` and the CI build args diverge from `render.yaml`.
> 6. Rollback as a manual job that redeploys the previous digest, and a runbook line for the two cases
>    where rollback is unsafe (`library/` commits; semantic column change).
>
> Give me the full config file, then the exact command to verify it locally before pushing.

### P5 — Reverse proxy, TLS, and the access control

> This app has no auth (§2, and `CLAUDE.md`). Put a Caddy or nginx reverse proxy in front of the
> compose stack that provides: TLS with automatic certificates; redirect off 80; HSTS; **proxy-side
> authentication** (basic auth as a floor, OIDC via Traefik/oauth2-proxy as the real answer — implement
> the one you recommend and note the other's config); and, critically, correct handling of the three
> WebSocket routes `/ws/record/{rid}`, `/ws/runs/{id}`, `/ws/batches/{id}` with buffering disabled
> (`flush_interval -1` or `proxy_buffering off` + upgrade headers).
>
> Prove each of these, with commands and expected output:
> - an unauthenticated request from outside the allow-list gets 401/403 on HTTP **and on a WebSocket
>   upgrade attempt** — check the upgrade path explicitly, because that is the classic bypass;
> - a live frame streams (not just connects) — capture two frames >1s apart and diff them;
> - port 8000 is unreachable from outside;
> - the container is bound to loopback only.
>
> If any proof fails, say which and stop — do not declare the proxy working because curl got a 200.

### P6 — Debug an incident

> The service is exhibiting a fault I will describe. Do not guess and do not restart things as a
> diagnostic — work the evidence.
>
> Fault: **[PASTE SYMPTOM — e.g. "recorder canvas frozen though clicks register" / "runs fail with
> 'target closed'" / "every save says the push did not complete and will retry in 60s" / "runs
> disappear after ~2 days although retention is 14" / "recorded project vanishes after redeploy"]**
>
> 1. Gather, and paste raw output: `docker logs --tail 300`, `/api/health`, `/api/diagnostics`
>    (`.schema`, `.memory`, `.guardrails`), `dmesg | tail -50`, `df -h` on the artifacts volume,
>    `docker inspect` for the effective env, and `ps` inside the container for chromium processes.
> 2. Map the symptom to the specific mechanism in this codebase, citing file:line. For each candidate
>    say what observation would confirm or kill it, then run that observation.
> 3. Only then propose the fix, ranked: config-only, then operational, then code change.
> 4. Tell me how to make it self-announcing (log line, health field, or a check in the pipeline) so
>    the next occurrence is not diagnosed by hand.
>
> Reference the known-mechanism list: frozen canvas → proxy buffering WebSockets; `target closed` →
> Chromium OOM-killed before Python; endless publish retry → 409 conflict from two environments sharing
> `TF_GITHUB_TOKEN`, or a read-only token; runs vanishing early → Postgres session timezone vs
> `datetime.utcnow()`; project lost on redeploy → `library/` is baked into the image and publishing was
> disabled.

### P7 — Load/scale assessment (before you buy a bigger box)

> Determine what actually limits this deployment and what changes when you add resources. Do not just
> raise every cap.
>
> 1. Read `app/guardrails.py` and enumerate every bound with its default and its env override, and note
>    which ones are silently clamped (`_int`'s `max(1,...)`) or silently defaulted.
> 2. Measure on the running instance: time and RSS for one recorded run of a real page, one 10-item
>    batch, one `GET /api/sync/github` export. Sample RSS at 1s intervals from inside the container;
>    report peak against the cgroup limit.
> 3. Report the ranking of constraints (Chromium memory, the single browser slot, `TF_MAX_*` buffers,
>    DB pool, disk, and the "one batch at a time" queue design) with the number that justifies it.
> 4. For a 2 GB and an 8 GB instance, give the exact env deltas — and state which caps must NOT be
>    raised (keep `TF_MAX_BROWSERS=1` and `TF_BROWSER_SINGLE_PROCESS=1` on 512 MB) and what becomes
>    safe only with a second machine.
> 5. Say plainly what cannot be solved by sizing (single-worker throughput) and what the smallest
>    architectural change is if we need more.

### P8 — Add real authentication (the fix for §2)

> Fix the root gap: this app has no inbound auth. Implement the **smallest** change that makes every
> route require a credential, and do not rewrite the app.
>
> 1. First enumerate the real attack surface from the code: all `@app.get/post/patch/delete` and
>    `@app.websocket` routes in `app/main.py`, including any static mounts that expose `logs/` or
>    `library/`. Output that list and its count — I want to see what "everything" means.
> 2. Add a FastAPI dependency, applied once at router level, accepting an `Authorization: Bearer` token
>    or session cookie, compared with `secrets.compare_digest`. It must cover **websockets** — show how
>    you verified that, since a browser cannot set an `Authorization` header on a WS handshake, and
>    query-string tokens leak into access logs. Recommend the mechanism accordingly (cookie or
>    `Sec-WebSocket-Protocol`), and note the first-frame-auth alternative if a cookie is not viable.
> 3. `/api/health` stays unauthenticated so platform probes work; `/api/diagnostics` must NOT, since it
>    exposes schema and memory. Say which other endpoints must stay open and justify each.
> 4. Replace the inert `API_Key` in `render.yaml` with something that is actually read, or delete it.
>    Do not leave a variable that implies a control which does not exist — that is worse than having
>    none. State which you chose.
> 5. Add tests: an unauthenticated request to a sample of every verb plus a websocket upgrade must be
>    rejected, and the suite in `CLAUDE.md` still passes.
>
> Show the diff, the threat it still does not address (this is a machine that browses arbitrary URLs and
> can push to git — SSRF and repo-write remain), and the network control that mitigates what the token
> cannot.

### P9 — Backup/restore you can trust

> Implement §11.3 as working scripts and prove restore, because an untested backup is not one.
>
> 1. Three paths, one script each with a lock, retry, 0600 perms and nonzero exit on failure:
>    `pg_dump -Fc` (or `sqlite3 .backup` — never `cp`), `restic` for `TF_ARTIFACTS`, and a check that
>    `library/` on the remote branch matches what the instance believes is published
>    (`GET /api/library` → `last_api_push` vs `git ls-remote`).
> 2. Retention ≥ `TF_RUN_RETENTION_DAYS` so rows never outlive their screenshots; enforce it in the
>    script and explain the failure mode if they don't align.
> 3. Verify: sha256 each artifact, and assert the dump is non-empty and, for pg_dump, that
>    `pg_restore --list` succeeds.
> 4. **The actual test**: restore into a throwaway container on a fresh database, start the app, run
>    `python -m tests.harness --require-browser` against it, and confirm a restored project can still
>    be opened, its steps listed, and its screenshot served. Report the restore duration and any row
>    that came back without its artifact file.
> 5. Emit a one-page runbook: where backups live, who can read them (they contain the plaintext
>    variables table — §2), restore procedure, and how to detect a backup that quietly stopped running.
>
> Do not encrypt "for safety" without telling me where the key lives; a backup decryptable by whoever
> stole the bucket is not encrypted.

### P11 — Stop committing secrets into `library/` (fixes §2.1)

> A credential is currently published in this repository because project variables are written to
> `library/projects/*/variables.json` and pushed by the API publisher. Fix the mechanism, do not just
> delete the file — deleting it without a guard means the next save republishes it.
>
> 1. First enumerate the exposure with evidence: every tracked file under `library/` containing a
>    secret-looking value (name matches pass|secret|token|key|pwd, or a high-entropy value ≥ 16 chars),
>    with the commit that introduced it, and whether `is_secret` was set. Then state plainly that
>    history rewrite cannot un-leak a public repo and that rotation at the source system is step one.
> 2. Make the API publisher refuse to write secret variable values into `library/`. Choose between
>    (a) omitting keys where `is_secret` is true, or (b) storing a reference (`env:TF_VAR_<name>`) so
>    only the name is committed. Implement one, and make the other's tradeoff explicit in a comment.
> 3. Redact `value` in `list_variables()` and `GET /api/variables` when `is_secret` is true — currently
>    the flag is copied through and honored nowhere. Keep execution working: interpolation must still
>    resolve the real value from the server side (`app/export_formats.py` uses `TF_VAR_<name>` at run
>    time; follow that pattern), and if the value only exists in the browser-facing response today, say
>    so rather than shipping a redaction that breaks replay.
> 4. Add a secret scan to CI (`gitleaks` or `trufflehog`) covering the whole repo including `library/`,
>    failing the build, plus a pre-commit hook. Prove it catches a planted decoy value and that it does
>    not flag the empty `[short]`/placeholder sample data.
> 5. Provide the `git filter-repo` command set to purge the identified files, the force-push sequence
>    with the exact warning about forks/clones/Render build logs, and the GitHub support step for
>    dereferencing cached blobs — then tell me what must be re-run after (open PRs, Render's last
>    successful build, any clone a teammate has).
> 6. Add tests: a variable created with `is_secret: true` must (i) not appear in the published
>    `variables.json` payload, (ii) not appear in the list endpoint response, and (iii) still
>    interpolate correctly into a run.
>
> Do not force-push, rewrite history, or modify the published `library/` files yourself. Produce the
> commands and the diff, apply only the code changes in steps 2-4 and 6, and stop for my approval on
> steps 1 and 5.

### P10 — Self-contained environment audit

> Audit this deployment against the guide and report only findings, most severe first, each with: the
> command/output that proves it, the consequence, and the one-line fix. Check:
>
> - a container bound to 0.0.0.0 with no proxy-side auth; a published 8000/5432/22;
> - `TF_ARTIFACTS` unset, or the SQLite file resolving outside the mounted volume;
> - workers ≠ 1, replicas ≠ 1, `TF_MAX_BROWSERS` ≠ 1 for the RAM actually available;
> - `DATABASE_URL` malformed or missing `sslmode` on a managed Postgres; password not URL-encoded;
> - Postgres session timezone not UTC (§5.5) with a query showing it; missing FK/`status` indexes
>   measured against real row counts;
> - `RENDER_GIT_COMMIT` unset (so `revision: local`);
> - `TF_GITHUB_TOKEN` present in more than one environment, or in a file that is git-tracked, or with
>   publish mode reporting something other than `api`/`disabled`;
> - any of the inert settings from §12 being relied on (`API_Key`, `ANTHROPIC_API_KEY`, `TF_MCP_ARGS`,
>   `TF_AGENT_*`);
> - a value in `_int`/`_flag` form that silently fell back (§7.5) — compare intended vs resolved;
> - no backup verification in the last 7 days.
>
> Output a table plus, at the end, the exact commands to fix the top three. Do not apply anything.

---

## 14. Definition of done

* [ ] `/api/health` → `database: "ok"`, `recorder/executor/batch_executor` all true, `revision` ≠
      `local`, `guardrails` matching intent (§10.1).
* [ ] `/api/diagnostics` → `schema.ready: true`, `schema.error: null`, dialect as expected; the first
      deploy's `schema.repairs` reviewed and explained, not ignored.
* [ ] `python -m tests.harness --require-browser` exits 0 with no skipped browser scenarios.
* [ ] Port 8000 unreachable from outside the proxy — verified with a negative test, not an assumption.
* [ ] An unauthenticated request gets 401/403 over HTTP **and** over a WebSocket upgrade attempt, or
      the deployment is loopback-only behind a VPN.
* [ ] `TF_ARTIFACTS` explicitly set, on a volume, and survives `docker compose down && up` (prove by
      recording a project, redeploying, and finding it still listed).
* [ ] Exactly one environment holds `TF_GITHUB_TOKEN`; the others report `disabled` *with a reason*.
* [ ] Postgres session timezone is UTC for the app's connections; the six indexes exist; row counts and
      `max(created_at)` match the source after migration.
* [ ] Rollback executed once against a previous digest, on a database whose `created_at` you checked
      for a timezone discontinuity (§5.5).
* [ ] A restore was performed into a throwaway environment and a restored project's screenshot was
      served.
* [ ] §12's inert settings are either removed or documented to their users as no-ops.
* [ ] Recorded → saved → replayed end to end in the browser, on the deployed instance, not locally.
