# TestForge deployment, end to end

How to build this service, run it on Azure, prove the deployment is the revision
you built, and roll it back. The database half — including SQL Server on Azure —
is in `docs/dev-azure-sql-server.md`. The pipelines that automate this are in
`docs/dev-cicd-pipelines.md`.

Items marked **[verify]** depend on a quota, a price or a platform behaviour that
changes.

## 1. Target state

```
             ┌──────────────────────────────────────────┐
   browser ──▶  Reverse proxy / Application Gateway      │
             │  TLS, and the only public endpoint        │
             └───────────────┬──────────────────────────┘
                             │  8000
             ┌───────────────▼──────────────────────────┐
             │  TestForge container (exactly one)        │
             │  FastAPI + dashboard + Chromium           │
             │  /app/artifacts  (volume)                 │
             │  /app/library    (volume or checkout)     │
             └───────┬───────────────────┬───────────────┘
                     │ 1433/5432         │ 443
             ┌───────▼────────┐   ┌──────▼───────────────┐
             │ Azure SQL DB   │   │ GitHub API (optional)│
             │ or SQL on VM   │   │ publishes library/   │
             └────────────────┘   └──────────────────────┘
```

Two rules that shape every decision below:

- **Exactly one instance, one worker.** The image's `CMD` runs uvicorn with
  `--workers 1` and `render.yaml` pins `numInstances: 1`. Every resource
  guardrail is per process, and `TF_MAX_BROWSERS=1` is a design constraint:
  recording and execution share one Chromium.
- **Two things must survive a redeploy.** `TF_ARTIFACTS` (screenshots, runs,
  exports, PDFs) and the library directory (`TF_LIBRARY_DIR`, default `library/`).
  Both must be on a volume or a checkout, not in the container's writable layer.

## 2. What the image contains

From `Dockerfile`:

| COPY | Why |
| --- | --- |
| `app` | The service and the dashboard |
| `LICENSE` | Licence travels with the artifact |
| `library` | The dashboard's data store; without it the service boots empty |
| `logs` | Harness reports, so the Logs tab works in a container with no `.git` |
| `docs` | The Azure tab and the **Dev & Deploy** tab serve these guides from the image |

If you add a file the dashboard serves at runtime, add it to the `Dockerfile`
*and* check `.dockerignore`: `UT-IMG-01` in `tests/test_unit.py` exists because a
`COPY` source that `.dockerignore` strips fails the build with
`failed to compute cache key`.

The image defaults: `TF_ARTIFACTS=/app/artifacts`, `PORT=8000`,
`TF_BROWSER_MODE=bundled`, `TF_MAX_BROWSERS=1`, plus the bounded in-memory and
queue settings listed in the `Dockerfile`. `EXPOSE 10000` is metadata for Render;
the runtime command binds `${PORT:-8000}`.

## 3. Build and publish the image

```bash
git fetch --tags
REV=$(git rev-parse --short HEAD)
REGISTRY=<YOUR ACR>.azurecr.io

az acr login --name <YOUR ACR>

docker build -t "$REGISTRY/testforge:$REV" .
docker push "$REGISTRY/testforge:$REV"

# Pin by digest: `latest` makes rollback meaningless and makes
# "which build is this?" unanswerable.
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$REGISTRY/testforge:$REV")
echo "$DIGEST"
```

Record the digest. It is the unit of deployment and of rollback.

## 4. Runtime on Azure

### 4.1 Azure VM with Docker Compose (recommended)

The service drives Chromium and writes screenshots; a VM gives it a real disk and
no per-request container limits.

```bash
az vm create \
  --resource-group rg-testforge-dev --name vm-testforge-app \
  --image Ubuntu2404 --size Standard_D2s_v3 \
  --admin-username azureuser --ssh-key-values ~/.ssh/id_ed25519.pub \
  --public-ip-address "" --subnet snet-app --vnet-name vnet-testforge
```

On the VM, `/opt/testforge/docker-compose.prod.yml`:

```yaml
services:
  testforge:
    image: <YOUR ACR>.azurecr.io/testforge@sha256:<DIGEST>
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"     # the proxy terminates TLS, not the app
    env_file: /opt/testforge/testforge.env
    volumes:
      - /opt/testforge/artifacts:/app/artifacts
      - /opt/testforge/library:/app/library
    environment:
      TF_ARTIFACTS: /app/artifacts
      TF_LIBRARY_DIR: /app/library
      RENDER_GIT_COMMIT: "${REV:-local}"   # reported as `revision` by /api/health
```

`/opt/testforge/testforge.env` (mode `600`, never in Git):

```
DATABASE_URL=mssql+pyodbc://testforge_app:<PASSWORD>@<HOST>:1433/testforge?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
TF_ACCESS_TOKEN=<32+ CHARACTER RANDOM TOKEN>
TF_VARIABLE_ENCRYPTION_KEYS=<FERNET KEY>
TF_LIBRARY_PUBLISH=1
TF_GITHUB_REPO=<OWNER/NAME>
TF_GITHUB_TOKEN=<FINE-GRAINED TOKEN: CONTENTS READ AND WRITE>
```

Deploy:

```bash
cd /opt/testforge
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f --tail=100
```

The network security group must allow 443 from your users and **nothing** to 8000
from the internet. Section 0.1 of `docs/azure-devops-deployment-guide.md` covers
the access gate that protects the app when that rule is wrong.

### 4.2 Azure App Service / Container Apps

Both work, with conditions:

- **One replica, one worker.** Container Apps `minReplicas` and `maxReplicas`
  both `1`; App Service without scale-out. Two replicas double every guardrail
  and both compete for the same memory.
- **Persistent storage on** for `TF_ARTIFACTS`. Without it, screenshots and run
  evidence disappear on every restart.
- **Session affinity is not enough** for WebSocket streams if you ever run more
  than one replica — another reason to stay at one.
- Container startup is slower here; raise the startup probe timeout, because
  Playwright's Chromium and `init_db()` both run at boot **[verify]**.
- Set the health probe to `/api/health`, exactly as `render.yaml` does. A probe
  on `/` is served from the static mount and proves nothing about the database.

### 4.3 Publishing mode

`library_store.publish_mode()` decides how a save reaches GitHub:

| Mode | When | Requirement |
| --- | --- | --- |
| `checkout` | The library is inside a git working tree | `git` in the image (it is), and credentials for the remote |
| `api` | No `.git` — the hosted container case | `TF_GITHUB_REPO` plus a token with Contents: read and write |
| `disabled` | Neither, or `TF_LIBRARY_PUBLISH=0` | Nothing; the dashboard says publishing is off and why |

Exactly one environment should hold a write token. `/api/health` reports
`library_publish_mode`, so you can confirm which one you got instead of guessing.

## 5. Environment variables that matter at deploy time

| Variable | Effect if wrong |
| --- | --- |
| `DATABASE_URL` | Unset falls back to SQLite under `TF_ARTIFACTS` — a silently local database |
| `TF_ARTIFACTS` | Unset writes to the home directory, outside any mounted volume |
| `TF_LIBRARY_DIR` | Unset uses the `library/` baked into the image; saves are lost on redeploy |
| `TF_ACCESS_TOKEN` | Unset or under 32 characters means no usable access gate |
| `TF_VARIABLE_ENCRYPTION_KEYS` | Unset falls back to an application-managed key stored in the database; the UI says so |
| `TF_MAX_BROWSERS` | Above `1` breaks the memory ceiling the deployment was sized for |
| `RENDER_GIT_COMMIT` | Unset means `/api/health` reports `revision: local` and you cannot tell what is deployed |
| `PORT` | The container binds `${PORT:-8000}`; a mismatch makes the proxy 502 |

The full table is in `readme.md`.

## 6. Prove the deployment

Do not trust "the container started". Run these, in order:

```bash
BASE=https://testforge.<YOUR DOMAIN>
REV=<THE SHORT SHA YOU BUILT>

# 6.1 Health: database reachable, recorder and executor importable.
curl -fsS "$BASE/api/health" | python3 -m json.tool

# 6.2 The revision you think you deployed.
test "$(curl -fsS "$BASE/api/health" | python3 -c 'import json,sys;print(json.load(sys.stdin)["revision"])')" = "$REV" \
  && echo "revision OK" || echo "WRONG REVISION"

# 6.3 Schema state and dialect.
curl -fsS "$BASE/api/diagnostics" | python3 -m json.tool

# 6.4 The access gate is on.
test -n "$TF_ACCESS_TOKEN" && curl -s -o /dev/null -w '%{http_code}\n' "$BASE/api/health"   # 401 expected

# 6.5 The dashboard and every tab asset load.
for path in / /tf-settings.js /devdeploy-components.js /vendor/react.production.min.js; do
  printf '%s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE$path")"
done

# 6.6 The Dev & Deploy kit serves its documents.
curl -fsS "$BASE/api/dev/kit" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d["docs"]),"docs,",d["prompt_count"],"prompts")'
```

Then do one real pass in the browser: create a project, record two steps against
the Demo tab, run it, and open Artifacts. A deployment that cannot record and
replay is not deployed, whatever `/api/health` says.

## 7. Rollback

```bash
cd /opt/testforge
# Point the compose file at the previous digest.
sed -i "s#testforge@sha256:.*#testforge@sha256:<PREVIOUS DIGEST>#" docker-compose.prod.yml
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
curl -fsS http://127.0.0.1:8000/api/health | python3 -m json.tool
```

Rollback is a digest change plus a restart. It does not touch the database, and
it must not: the schema is repaired forward at boot by `init_db()`, and there is
no downgrade path for a column that was added.

## 8. Secret handling

- Secrets live in Key Vault or in the host's `env` file with mode `600`. They are
  never baked into the image: `Dockerfile` deliberately does not set
  `TF_GITHUB_REPO` or `TF_GITHUB_TOKEN`.
- `app/errors.redact()` strips credentials from anything derived from an
  exception before it reaches a response or a log.
- Encrypted variable values carry the `enc:v1:` prefix from `app/secrets_store`.
  Rotate by adding a new key to the front of `TF_VARIABLE_ENCRYPTION_KEYS`, not
  by replacing the list.
- Never commit `.env`, `*.pem` or `*.key`; `.gitignore` covers them, and the
  library is published to a repository.

## 9. Hand-over checklist

- [ ] Image pinned by digest; the digest is recorded against the release.
- [ ] One instance, one worker, `TF_MAX_BROWSERS=1`.
- [ ] `TF_ARTIFACTS` and `TF_LIBRARY_DIR` on persistent storage.
- [ ] `/api/health` probe wired in the platform, not `/`.
- [ ] `/api/health` reports the expected `revision` after deploy.
- [ ] `/api/diagnostics` shows `ready: true` and the dialect you intended.
- [ ] Access gate configured with a 32+ character token; unauthenticated request returns 401.
- [ ] Library publishing mode is the one you intended, in exactly one environment.
- [ ] A record-and-replay pass succeeded on the deployed instance.
- [ ] The rollback digest is written down before the deploy, not after the incident.
