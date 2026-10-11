# TestForge on Azure: application, database and repository

**Audience:** senior DevOps / development automation engineers.
**Scope:** deploy the application runtime, its PostgreSQL database and the source repository into
Azure + Azure DevOps, while the existing GitHub free-tier demo on Render continues to run untouched.

Everything below was verified against this repository at the commit you branch from. The claims
about behaviour (`publish_mode`, `init_db`, pool sizes, the health payload) are read from
`app/config.py`, `app/db.py`, `app/library_store.py` and `app/main.py` — not assumed. Where a fact
is about the platform rather than the code, it is marked **[verify]** because Azure and Render
quotas change.

---

## 0. Read this before you touch anything

### 0.1 The application has no authentication. None.

`render.yaml` passes an `API_Key` environment variable to the service. **No line of application code
reads it.** A grep of the whole package finds only outbound auth (the GitHub REST client sending its
own token) and nothing inbound:

```bash
grep -rn "API_Key\|HTTPBearer\|Authorization" app/*.py
# app/github_api.py:424:  request.add_header("Authorization", f"Bearer {self.token}")   <- outbound
```

There is no middleware, no dependency, no login. Every endpoint listed in `app/main.py` —
`POST /api/projects`, `POST /api/runs`, `POST /api/recordings/{id}/session`,
`POST /api/library/publish`, `POST /api/ai/rephrase`, `GET /api/diagnostics` — is reachable by
anyone who can reach the port. On Render's private, sleeping, 512 MB instance this was an
acceptable demo risk. **On an Azure VM with a public IP and a persistent database it is not.**

The relevant capability: this service drives a headless Chromium on its own host and can push to
`library/` in GitHub. An unauthenticated visitor can therefore consume all the VM's CPU with
batch runs, and — if a `TF_GITHUB_TOKEN` is present — write commits to your repository through
`POST /api/library/publish`.

> **Rule for the rest of this document:** the deployment is only allowed to listen on a loopback
> interface. Section 6 enforces that with nginx + a reverse-proxy allow-list. Do not "just open
> 80/443 to test it".

If you need real authentication before a fix lands in the app, put it at the proxy (OIDC via
Entra ID application proxy, or `auth_request` to an OAuth2 proxy sidecar) and treat the container
port as a trusted network. Section 12 has the prompt for it.

### 0.2 One process is a design constraint, not an oversight

| Evidence | Consequence |
| --- | --- |
| `render.yaml`: `numInstances: 1` | Do not set an instance count > 1. |
| `Dockerfile`: `--workers 1` | Do not add uvicorn workers. |
| `app/config.py`: `MAX_CONCURRENT_BROWSERS = 1` | Hard-coded; there is no env var to raise it. |
| `app/db.py`: SQLite branch uses `StaticPool` | A single shared SQLite connection; two processes on one file DB will contend. |

Every guardrail (`TF_MAX_BROWSERS`, `TF_MAX_LIVE_FRAME_RUNS`, `TF_BATCH_MAX_RSS_MB`) is
**per-process**. Doubling processes does not double capacity — it doubles memory demand against a
fixed ceiling and multiplies Chromium launches. Scale **up** (bigger VM SKU) or split the queue
consumer into its own container. Never scale out the API.

### 0.3 Two environments must not share one database

`app/main.py` runs `boot_hygiene()` on every startup, which calls `run_queue.reap_orphans()` and
**cancels every `queued` and `running` row** so the queue cannot show a pending count that will
never move. That is correct for a service that owns its database. It is destructive if two
environments share one: every Azure deploy silently cancels runs in flight on the other.

Therefore: **one database per environment.** No shared staging/prod Postgres, no shared
`TF_ARTIFACTS` volume. The table in section 8 makes this explicit.

### 0.4 The schema needs no migration tool — and that is deliberate

`init_db()` is called at import time (`app/main.py:56`) and runs `_ensure_schema()`:
`Base.metadata.create_all()` (creates missing *tables*), then `_add_missing_columns()` (adds columns
the models declare and backfills them), then `_relax_legacy_columns()` (Postgres-only: drops
`NOT NULL` on legacy columns the models no longer write). It never raises — failures land in
`SCHEMA_STATUS` and are reported by `GET /api/diagnostics`.

Consequences for your pipeline:

* **Do not add an Alembic stage.** There is no `alembic/` directory and no revision history;
  introducing one without removing the boot-time repair just creates two competing authorities.
* The deploy gate must assert on `/api/diagnostics` after the first boot (section 9), because a
  schema repair that failed will *not* crash the container.
* `create_all` needs DDL rights. The app role therefore cannot be a least-privilege
  `SELECT/INSERT/UPDATE/DELETE` role — see §5.3 for the compromise.

### 0.5 Publish mode changes when you leave the container — check which one you get

`app/library_store.py:publish_mode()` resolves in this order:

1. `TF_LIBRARY_PUBLISH` set and falsy → `disabled`
2. a `.git` directory at or above the library folder → `checkout` (uses the git binary in the image)
3. `TF_GITHUB_REPO` **and** `TF_GITHUB_TOKEN` present → `api`
4. otherwise → `disabled`

The Render container is built from `app/` + `library/` only, so it has no `.git` and lands on
**`api`**. If you naively deploy to a VM by `git clone`-ing the repo and running it in place, you get
**`checkout`** mode instead: it will `git push` from the working tree, require git credentials on the
VM, and — worse — fight the CI pipeline that also pushes to `main`.

**Decision for this rollout: force `api` mode on the VM too**, by running the *image* (which contains
no `.git`) with a bind-mount for `/app/artifacts` only. Keep one publishing code path across both
environments. If you specifically need `checkout` mode, see §7.3.

---

## 1. Target state

```
                    GitHub  Raheelatta1984/testforge   ← source of truth (people push here)
                       │
        ┌──────────────┴────────────────┐
        │ mirror (Azure Pipeline, [skip ci]) │
        ▼                               ▼
  Azure Repos Git                  Azure Pipelines  ── azure-pipelines.yml lives in Azure Repos
  testforge-mirror                 CI → build image → ACR → deploy → smoke gate
        │                               │
        │                               ├──▶ ACR (hardened) ──▶ Azure VM  ──▶ nginx+TLS ──▶ :8000 loopback
        │                               │                          └─ /var/lib/testforge/artifacts (persistent)
        │                               └──▶ Azure Database for PostgreSQL Flexible Server
        │
        └──▶ Render (unchanged, free)  demo/sandpit, read-only, no token, sleeps on idle
```

| Concern | Azure | Render free | Notes |
| --- | --- | --- | --- |
| Role | primary / persistent | demo only | |
| Repo | Azure Repos mirror (read) | GitHub (read) | GitHub is authoritative |
| Image | ACR digest | built from `Dockerfile` by Render | same `Dockerfile`, so parity is real |
| Database | Flexible Server Postgres | `DATABASE_URL` → separate Neon DB, or ephemeral SQLite | never share |
| Artifacts | mounted volume, retained | container filesystem, **wiped on every deploy/spin-down** | inherent to Render free |
| `TF_GITHUB_TOKEN` | present → can publish | **absent** → publishing reports `disabled` | exactly one holder |
| Always on | yes | no: spins down after ~15 min idle, 30–60 s cold start **[verify]** | |

---

## 2. Prerequisites

Azure:

```bash
az login
az account set --subscription "<your subscription>"
az provider register --namespace Microsoft.DBforPostgreSQL --wait-registration   # first use only
```

Subscription type matters for cost: an **Azure free account** covers 750 h/month of `Standard_B1s`,
`B2pts_v2` or `B2ats_v2` for 12 months **[verify]**, and `B1s` is being retired in favour of the v2
burstables. Two always-on B-series VMs exhaust 750 h in about 15 days, so this design uses **one**.
Keep the OS disk at ≤ 64 GB (P6) to stay inside the free allowance **[verify]**.

Azure DevOps: free for 5 users, 1800 pipeline minutes/month with 1 Microsoft-hosted parallel job
**[verify]**. If your org has already hit a limit, `self-hosted` is not a good fallback here —
do **not** run a build agent on the 1 GB application VM; the build stage runs `playwright install`
and a Docker build, which will OOM the service. Use Microsoft-hosted, or a separate agent VM.

Access you must already hold:

* `Contributor` on the resource group (or Owner, to create role assignments)
* Permission to create a service principal or use managed identity for `AcrPull`
* Azure DevOps Project Admin (service connections, variable groups, environments, approvals)
* GitHub `Fine-grained PAT` with **Contents: Read and write** on this one repository for the runtime,
  and a separate **Code: Read & write** PAT for the mirror. See `docs/github-publishing-security-runbook.md`
  before issuing either — the runbook covers the 403-on-`git/blobs` failure that means "read-only token".

Naming used throughout (change freely; PostgreSQL server names must be globally unique):

| Variable | Value |
| --- | --- |
| `RG` | `rg-testforge-eas` |
| `LOC` | `australiaeast` |
| `ACR` | `testforgeacr01` |
| `PGSERVER` | `testforge-pg` |
| `VM` | `testforge-vm01` |
| DevOps org / project | `testforge` / `testforge` |

```bash
export RG=rg-testforge-eas LOC=australiaeast ACR=testforgeacr01 PGSERVER=testforge-pg VM=testforge-vm01
az group create -n $RG -l $LOC
```

---

## 3. Repository: GitHub and Azure Repos in parallel, without a break

**Model: GitHub is where humans push. Azure Repos is a mirror that pipelines and policies attach to.**
This is what makes the arrangement non-breaking: the existing GitHub workflow keeps working with zero
change, and nothing in Azure can lose your history because it holds no authority. Do not attempt
bidirectional sync on day one — conflict resolution needs a policy you have not written yet.

### 3.1 Create the mirror

```bash
az repos create --name testforge-mirror --organization https://dev.azure.com/testforge --project testforge
```

Import creates a *copy* and then the two diverge. For a true mirror, create the empty repo and push
with `--mirror` (this preserves all branches and tags, which the app's GitHub tab depends on):

```bash
git clone --bare https://github.com/Raheelatta1984/testforge.git tf.git
cd tf.git
git remote add azure https://dev.azure.com/testforge/testforge/_git/testforge-mirror
git push --mirror azure
cd .. && rm -rf tf.git
```

Azure Repos accepts a PAT over HTTPS — use the username `anything` (the value is ignored) with a PAT
that has **Code: Status (write)**, **Code (read & write)** and **User profile (read)**; `Alt+T` in the
browser also works for interactive pushes. Store the PAT in Azure Key Vault (§4.2), never in the
pipeline YAML.

### 3.2 Keep it mirrored — and stop the loop

Add this as a pipeline **in Azure DevOps** with a GitHub *resource* trigger, so the mirror never
depends on an agent you control:

```yaml
# tf-mirror.yml — GitHub main -> Azure Repos, one way
trigger: none
pr: none

resources:
  repositories:
  - repository: github
    type: github
    name: Raheelatta1984/testforge
    endpoint: github-readonly          # service connection, read-only PAT
    trigger:
      branches:
        include: [main]

pool: { vmImage: ubuntu-latest }

jobs:
- job: mirror
  displayName: Mirror GitHub -> Azure Repos
  steps:
  - checkout: github
    persistCredentials: false
  - script: |
      set -euo pipefail
      AZURE_TOKEN="$(az repos pat)"     # replace: read from the linked Key Vault variable group
      git remote add azure "https://mirror-bot:${AZURE_TOKEN}@dev.azure.com/testforge/testforge/_git/testforge-mirror"
      git fetch --unshallow origin || true
      # --mirror for branches/tags; commit message carries [skip ci] so no cycle is possible
      git push --mirror --no-verify \
        "https://mirror-bot:${AZURE_TOKEN}@dev.azure.com/testforge/testforge/_git/testforge-mirror"
    displayName: Push mirror
    env:
      GIT_AUTHOR_NAME: tf-mirror
      GIT_COMMITTER_NAME: tf-mirror
```

Three things that will otherwise cost you an afternoon:

1. **`[skip ci]`** must appear in the mirror commit, or GitHub Actions and Azure Pipelines will
   trigger each other indefinitely. With a one-way push, the mirror commit is created on GitHub only
   if you also push back — so with the topology above the risk is only Azure-side; disable CI triggers
   on the mirror repo anyway: *Settings → Repository → Skip CI for new pushes*.
2. **Shallow clones.** Azure Repos may give you `--depth 1`; `git push --mirror` from a shallow clone
   is rejected. Hence the `--unshallow` line, and `git config --bool core.mirror true` if you script it.
3. **`--mirror` is destructive on the destination.** Anyone who pushes a feature branch to Azure
   Repos will have it deleted by the next sync. Protect `main` with a branch policy that blocks
   direct pushes to everything except the mirror-bot identity, and *tell people the mirror is
   read-only* — put it in the repo description.

### 3.3 Policies on `main` in Azure Repos

Set these before the first merge request; they are cheap now and painful to retrofit:

* Require a merge request, minimum 1 reviewer, `azure-pipelines.yml` must pass
* Require a policy ID (the build policy) — this is what proves the change builds before it reaches
  GitHub via your own sync-back process
* Block `library/` from direct edit without a reviewer from QA: the Library tree is the dashboard's
  data store, and a hand-edit there silently changes what every environment serves
* Build a path filter so docs-only changes do not spin a 6-minute Chromium build (used in §4.1)

---

## 4. Pipeline: build once, publish once, deploy to both clouds

### 4.1 `azure-pipelines.yml`

```yaml
# azure-pipelines.yml — lives in Azure Repos; GitHub copy kept byte-identical by CI check
trigger:
  branches: { include: [main] }
  paths:
    include: ['app/*', 'Dockerfile', 'requirements.txt', 'render.yaml', 'azure-pipelines.yml']
    exclude: ['*.md', 'docs/*', 'logs/*', 'library/**']
pr:
  branches: { include: ['*'] }
  paths:
    include: ['app/*', 'Dockerfile', 'requirements.txt', 'azure-pipelines.yml']

variables:
  IMAGE: testforge
  ACR: testforgeacr01.azurecr.io
  # Build.SourceVersion is predefined; do not shadow it. It is consumed in the `rev` step.

stages:
- stage: Test
  displayName: Unit tests inside the built image
  jobs:
  - job: pytest
    pool: { vmImage: ubuntu-latest }
    steps:
    - task: Docker@2
      displayName: Build (for test parity, not push)
      inputs:
        command: build
        Dockerfile: Dockerfile
        arguments: --build-arg BUILDKIT_INLINE_CACHE=1
        tags: |
          testforge:test
    # Running tests in the image is the whole point: the image already has the
    # Chromium that playwright install --with-deps put there, so the test
    # environment equals the production environment. A bare pip install on the
    # agent silently tests a different browser stack.
    - script: |
        docker run --rm \
          -e TF_ARTIFACTS=/tmp/tf \
          -e TF_BROWSER_MODE=bundled \
          -v "$(PWD)/tests:/app/tests:ro" \
          testforge:test \
          python -m pytest /app/tests/test_unit.py -q --maxfail=25 \
            || { echo "##vso[task.logissue type=error]unit tests failed"; exit 1; }
      displayName: pytest in image

- stage: BuildAndPush
  dependsOn: Test
  condition: and(succeeded(), eq(variables['Build.Reason'], 'BatchedCI'))
  jobs:
  - job: push
    pool: { vmImage: ubuntu-latest }
    steps:
    - task: Docker@2
      displayName: Login to ACR
      inputs:
        containerRegistry: acr-service-connection
        command: login
    - bash: |
        set -euo pipefail
        # 12 chars: unique for this history, readable in `docker images`. Azure Pipelines
        # classic expressions have no substring(), so derive it in a step.
        SHORT="$(echo "$(Build.SourceVersion)" | cut -c1-12)"
        # Same-job use -> $(digestTag). isOutput=true is what additionally makes it
        # consumable by the Deploy stage via stageDependencies (below).
        echo "##vso[task.setvariable variable=digestTag;isOutput=true]$SHORT"
      name: rev
      displayName: Resolve immutable tag
    - script: |
        set -euo pipefail
        docker build -t $(ACR)/$(IMAGE):$(digestTag) -t $(ACR)/$(IMAGE):latest .
        docker push $(ACR)/$(IMAGE):$(digestTag)
        docker push $(ACR)/$(IMAGE):latest
      displayName: Tag by commit and push

- stage: Deploy
  dependsOn: BuildAndPush
  variables:
    # Stage-to-stage output variable. This is why rollback works: the digest tag
    # is the commit, so the previous run's tag still exists in ACR.
    DIGEST_TAG: $[ stageDependencies.BuildAndPush.push.outputs['rev.digestTag'] ]
  jobs:
  - deployment: azure
    environment: azure-primary        # env + approvals & checks = your production gate
    pool: { vmImage: ubuntu-latest }
    strategy:
      runOnce:
        deploy:
          steps:
          - task: AzureCLI@2
            displayName: Deploy to VM by digest
            inputs:
              azureSubscription: azure-service-connection
              scriptType: bash
              scriptLocation: inlineScript
              inlineScript: |
                set -euo pipefail
                # apply.sh is copied into the agent's workspace by checkout, then handed
                # to the VM through run-command - no SSH, no long-lived admin access.
                az vm run-command invoke -g $(RG) -n $(VM) \
                  --command-id RunShellScript \
                  --scripts "$(Pipeline.Workspace)/s/deploy/azure/apply.sh" \
                  --parameters "$(ACR)" "testforge" "$(DIGEST_TAG)" "$(DIGEST_TAG)"
          - script: bash deploy/azure/smoke.sh "https://testforge.example.com" "$(DIGEST_TAG)"
            displayName: Smoke gate (§9)
```

In the `variables:` block at the top, only the image coordinates are needed — the revision is derived
in the `rev` step, because Azure Pipelines classic expressions have no `substring()`:

```yaml
variables:
  IMAGE: testforge
  ACR: testforgeacr01.azurecr.io
```

> **Read `apply.sh`'s contract before wiring it:** §6.5 takes `ACR IMAGE TAG [TAG]` and pins the
> compose file by *resolving the tag to a digest in ACR*. Passing the tag twice is deliberate — the
> fourth argument is what `RENDER_GIT_COMMIT` is set to, so `/api/health` reports the same string the
> pipeline echoed. If you skip that, every deployment looks like `local` (§13) and an operator
> comparing two environments has no way to tell them apart.


Notes on the parts that are easy to get wrong:

* **`condition: eq(variables['Build.Reason'], 'BatchedCI')`** keeps PR builds from pushing images.
  Adjust to `'individualCI'` if you trigger on every push to `main` instead of batching.
* **Tag by commit, never only `latest`.** Rollback (§10) depends on the previous digest still existing.
* The pipeline sets no `TF_GITHUB_TOKEN`; it only builds. Runtime secrets live in the VM's `.env`.
* `pool: ubuntu-latest` has Docker and no browser to install — the build stage takes ~4–6 min for
  `playwright install --with-deps chromium` plus a 3-layer cache. Cache the base image with
  `--cache-from` on a second tag if the 1800-minute free allowance **[verify]** starts hurting.

### 4.2 Where secrets live

| Secret | Home | Consumed by |
| --- | --- | --- |
| `TF_GITHUB_TOKEN` (runtime publisher, fine-grained, Contents: RW) | Key Vault secret `tf-github-token` | VM `/etc/testforge/.env` via §6.4 |
| ACR push | Azure DevOps service connection (SP) | pipeline only |
| Postgres admin | Key Vault secret, never a variable group *value* | one-time §5.2, then the app role |
| `API_Key` | **does nothing — §0.1.** Do not spend time rotating it | nothing |

Link the vault so values are never typed into a pipeline: *Pipelines → Library → Variable groups →
Azure key vault link*. Linked secrets are fetched at runtime and are masked in logs, but they are
**not** available to YAML expressions — they must be mapped as an env var into a task (the
`AzureKeyVault@2` task or `env:` on `AzureCLI@2`), which is a common first-run failure.

### 4.3 Do not also add a GitHub Actions file that pushes

The user-facing requirement "run parallel without break" is satisfied by *mirroring the repo* and
*keeping Actions alive for the demo*. Adding a second publisher that also pushes images/tags to the
same places is how you get two pipelines racing on `main`. If you want Actions to co-deploy Render,
let it only call the Render deploy API; leave ACR and Azure to this pipeline, and add a CI check that
`azure-pipelines.yml` and `.github/workflows/build.yml` reference the same `Dockerfile` and
`requirements.txt` hashes.

---

## 5. Database: Azure Database for PostgreSQL — Flexible Server

### 5.1 Create it, on a private network

```bash
az postgres flexible-server create \
  -g $RG -n $PGSERVER -l $LOC \
  --service-name microsoft.dbforpostgresql \
  --sku-name Standard_B2ms --tier Burstable \
  --storage-size 32 --storage-type PremiumV2_LRS \
  --admin-user tf_admin --admin-password "$(az keyvault secret show --vault-name kv-testforge -n pg-admin --query value -o tsv)" \
  --database-name testforge \
  --vnet vnet-testforge --vnet-address-prefix 10.40.0.0/16 \
  --subnet sn-data --subnet-address-prefix 10.40.1.0/24 \
  --public-access none \
  --ssl-min-ver TLSv1.2 \
  --defer
```

`--public-access none` + VNet integration is the correct default. The tempting
`--public-access 0.0.0.0` ("allow access from Azure services") opens the server to the whole Azure
address space — combined with §0.1's missing app auth and a weak admin password, that is a data
incident, not a shortcut. If you must debug from a laptop, use a firewall rule for your egress IP
with a TTL, or an SSH tunnel through the VM:

```bash
ssh -N -L 5432:${PGSERVER}.postgres.database.azure.com:5432 azureuser@${VM_IP}   # via Bastion/JIT
```

### 5.2 Role, not admin — but not least-privilege either

Because `init_db()` issues `CREATE TABLE` / `ALTER TABLE` at boot (§0.4), the runtime role needs DDL
on one schema. Grant that and nothing else:

```sql
CREATE ROLE testforge_app LOGIN PASSWORD '<from keyvault>';
ALTER DATABASE testforge OWNER TO testforge_app;      -- simplest: app owns the db and its schema
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO testforge_app;
ALTER ROLE testforge_app SET statement_timeout = '30s';
ALTER ROLE testforge_app SET idle_in_transaction_session_timeout = '60s';
```

Owning the database is a deliberate compromise so the self-healing schema works; if your standard is
stricter, split it: a migration role (`GRANT CREATE ON SCHEMA`) used by an *optional* one-shot job,
and a runtime role with DML only — but then you must also remove the boot-time repair or every
container start will log a `SCHEMA_STATUS.error`. Do not leave the app in a state where it logs a
permission failure on every boot and the smoke test ignores it.

### 5.3 Connection-string and pool arithmetic

```
DATABASE_URL=postgresql://testforge_app:<password>@testforge-pg.postgres.database.azure.com:5432/testforge?sslmode=require
```

* `app/config.py` rewrites a `postgres://` prefix to `postgresql://`, so the URL Azure prints in the
  portal's "Connect" pane works as pasted. **Do not paste it with `?sslmode=require` after the
  dbname twice** — SQLAlchemy will keep only the last query string.
* `sslmode=require` is mandatory: Flexible Server enforces TLS, and a missing `sslmode` surfaces as a
  confusing handshake error, not a clear "SSL is required".
* **URL-encode the password.** Azure's generated passwords happily include `%`, `~`, `#`. A `%2`
  inside the password becomes an escape sequence and the connection fails with a host-resolution
  error that looks like a network problem. `python -c "from urllib.parse import quote; print(quote(pw, safe=''))"`.
* `app/db.py` uses `pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=3600` for
  Postgres → **up to 30 connections per process**. With one worker that is fine on a `B2ms`
  (`max_connections` ~100 **[verify]**). Two workers, or an agent pool sharing the server, will hit
  the ceiling. Either lower the pool via a `TF_PG_POOL_SIZE` env (not implemented — a good first
  patch) or enable the built-in PgBouncer:
  `az postgres flexible-server parameter set -g $RG -s $PGSERVER -n pgbouncer.enabled -v true`.
* `pool_recycle=3600` exists because Azure silently kills idle connections; keep it.

`--public-access none` also means your **pipeline cannot reach the DB directly**. If you want a
seed or assertion step, run it from inside the VM (`az vm run-command`) or from a job on the VNet,
not from a Microsoft-hosted agent.

### 5.4 Render's database (the demo) — do not reuse this server

Per §0.3, the demo gets its own. Options, in order of preference:

1. A second, tiny Flexible Server in a separate resource group, `B1ms`, 128 GB minimum storage
   (note: Flexible Server storage cannot be shrunk, so this is the one cost line that does not scale
   down later).
2. Neon, which `requirements.txt` already anticipates ("the hosted deployment uses Neon"), free tier,
   and it sleeps too — which is *consistent* with a demo that sleeps.
3. Nothing — leave `DATABASE_URL` unset and let the free demo use SQLite on an ephemeral filesystem,
   which resets on every deploy. **This is the honest state of the current Render setup** and it is
   documented as such in the Guide tab: "demos here are disposable by design; the durable record is
   `library/` on GitHub."

That last sentence is the current app's saving grace and should be preserved: `library/` is committed
through the GitHub API (§7), so recorded work survives a Render rebuild even though the database rows
do not. Do not "fix" this by pointing both environments at one Postgres.

---

## 6. Runtime: Azure VM with Docker Compose

Use the image, not a host Python install. `Dockerfile` already pins `python:3.11-slim-bookworm`,
installs `git` for publishing, and bakes every `TF_*` guardrail as an image default — so the VM only
supplies overrides and state.

### 6.1 Network + VM

```bash
az network vnet create -g $RG -n vnet-testforge --address-prefix 10.40.0.0/16 \
  --subnet-name sn-app --subnet-prefix 10.40.0.0/24
az postgres flexible-server create ... --vnet vnet-testforge --subnet sn-data \
  --subnet-address-prefix 10.40.1.0/24          # §5.1

az vm create -g $RG -n $VM --size Standard_B2ats_v4 \
  --image Ubuntu2204 --admin-username azureuser \
  --generate-ssh-keys --os-disk-size-gb 64 --storage-sku Standard_LRS \
  --data-disk-sizes-gb 64 --vnet-name vnet-testforge --subnet sn-app \
  --nsg nsg-testforge --public-ip-sku Standard --zone 1
```

`B2ats_v4` (2 vCPU / 1 GB) is roughly Render's 512 MB plus headroom and is the cheapest defensible
choice for the *demo* role. For the primary role — persistent Chromium, batches of 40 recordings —
budget `Standard_D2as_v5` (2 vCPU / 8 GB, ~$100/mo **[verify]**). Running a browser farm on 1 GB is
optimistic; if you must, keep `TF_BATCH_MAX_RSS_MB` at 420 and accept that large batches will relaunch
the browser often.

Attach a managed data disk for artifacts so a redeploy or OS-disk swap never takes recordings with it:

```bash
DISKID=$(az vm list -d -g $RG -n $VM --query "[0].storageProfile.dataDisks[0].id" -o tsv)
az vm run-command invoke -g $RG -n $VM --command-id RunShellScript --scripts @- <<'SH'
set -euo pipefail
DEV=$(lsblk -npo NAME -d | sed -n '2p')     # first data disk
mkfs.xfs -f -L tfartifacts "$DEV" 2>/dev/null || true
mkdir -p /mnt/artifacts
UUID=$(blkid -s UUID -o value "$DEV")
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /mnt/artifacts xfs defaults,nofail 0 2" >> /tmp/fstab.new
cat /tmp/fstab.new >> /etc/fstab 2>/dev/null || true
mount -a || true
mkdir -p /mnt/artifacts/testforge/{runs,rec,batches}
chown -R 1000:1000 /mnt/artifacts/testforge    # container 'appuser'; adjust if you run as root
SH
```

### 6.2 NSG — the part that keeps §0.1 from becoming an incident

```bash
NSGID=$(az network nsg show -g $RG -n nsg-testforge --query id -o tsv)
az network nsg rule create -g $RG --nsg-name nsg-testforge -n allow-https \
  --priority 100 --access Allow --direction Inbound --protocol Tcp --destination-port-ranges 443
az network nsg rule create -g $RG --nsg-name nsg-testforge -n deny-ssh \
  --priority 110 --access Deny --direction Inbound --protocol Tcp --destination-port-ranges 22
az network nsg rule create -g $RG --nsg-name nsg-testforge -n allow-azure-bastion \
  --priority 120 --access Allow --direction Inbound --protocol Tcp \
  --source-service-tag AzureBastionSubnet --destination-port-ranges 22
```

**No port 8000 inbound. No port 22 from `0.0.0.0`.** Use Bastion or JIT; `RunShellScript` (§6.4)
means most operations need neither.

### 6.3 Managed identity, so the VM holds no ACR credential

```bash
az vm identity assign -g $RG -n $VM
SPID=$(az vm show -g $RG -n $VM --query identity.principalId -o tsv)
az role assignment create --assignee "$SPID" --role AcrPull --scope $(az acr show -n $ACR --query id -o tsv)
```

### 6.4 `/opt/testforge/docker-compose.prod.yml` on the VM

```yaml
services:
  testforge:
    # Pinned by digest at deploy time (apply.sh rewrites this line). Never :latest in production.
    image: testforgeacr01.azurecr.io/testforge@sha256:REPLACE
    restart: unless-stopped
    # §0.2 — one process. Do not add workers, do not add replicas.
    command: ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--workers", "1"]
    # Bind to loopback: nginx is the only public surface (§0.1, §6.2).
    ports: []
    expose: ["8000"]
    networks: [tf]
    environment:
      TF_ARTIFACTS: /app/artifacts
      TF_BROWSER_MODE: bundled
      # The container has no .git, so publishing stays in 'api' mode like Render (§0.5).
      TF_GITHUB_REPO: Raheelatta1984/testforge
      TF_GITHUB_BRANCH: main
      # Both /api/health (main.py:266) and /api/diagnostics (main.py:386) report
      # revision from os.environ["RENDER_GIT_COMMIT"] and nothing else — no generic
      # REVISION/GIT_COMMIT fallback exists. Interpolated from the env file, which
      # is why apply.sh passes `--env-file` (compose reads ${VAR} from the shell or
      # --env-file, NOT from an env_file: entry). Without it, health says "local".
      RENDER_GIT_COMMIT: ${RENDER_GIT_COMMIT:-unknown}
      TF_MAX_BROWSERS: "1"
      TF_VIEWPORT_WIDTH: "1280"        # more headroom than Render's 1024
      TF_VIEWPORT_HEIGHT: "720"
      TF_JPEG_QUALITY: "40"
      TF_BATCH_MAX_RSS_MB: "900"       # only valid on a >=4GB VM; leave 420 on 1GB
    env_file:
      - /etc/testforge/.env             # DATABASE_URL, TF_GITHUB_TOKEN — never in git (see .gitignore)
    volumes:
      - /mnt/artifacts/testforge:/app/artifacts
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5).status==200 else 1)\""]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s                 # Chromium import + browser discovery is not instant
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "3" }   # else the OS disk fills in a month

  caddy:
    image: library/caddy:2
    restart: unless-stopped
    network_mode: host                  # simplest correct TLS termination: 80 -> 443, ACME on both
    volumes:
      - /opt/testforge/Caddyfile:/etc/caddy/Caddyfile:ro
      - /opt/testforge/caddy-data:/data
      - /opt/testforge/caddy-config:/config
    depends_on: [testforge]

networks: { tf: { driver: bridge } }
```

`/opt/testforge/Caddyfile`:

```
testforge.example.com {
    encode zstd gzip
    # §0.1: until the app authenticates, this allow-list IS the access control.
    @notallowed not remote_ip 203.0.113.0/24 198.51.100.7
    respond @notallowed 403 "forbidden"
    reverse_proxy 127.0.0.1:8000 {
        # /ws/record/{rid}, /ws/runs/{id}, /ws/batches/{id} must not be buffered or time out
        flush_interval -1
    }
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
}
```

The `flush_interval -1` matters: `app/main.py` registers three WebSocket routes
(`/ws/record/{rid}`, `/ws/batches/{batch_id}`, `/ws/runs/{run_id}`) and the recorder falls back to
HTTP polling through proxies that drop them. A proxy that buffers frames produces a recorder UI that
looks alive but shows nothing — the worst kind of failure to debug.

`/etc/testforge/.env` (root:root 0600):

```
DATABASE_URL=postgresql://testforge_app:URL_ENCODED@...:5432/testforge?sslmode=require
TF_GITHUB_TOKEN=github_pat_...
# Managed by apply.sh; present here so the file's shape is obvious and perms are set once.
RENDER_GIT_COMMIT=unknown
```

`env_file` values land in the container's environment, but compose interpolates `${VAR}` in the
`environment:` block from the **shell or `--env-file`**, not from an `env_file:` entry — so `apply.sh`
invokes `docker compose --env-file /etc/testforge/.env`. Get this wrong and `RENDER_GIT_COMMIT`
resolves to the literal `unknown` while everything else works, which is a miserable thing to debug.

### 6.5 `deploy/azure/apply.sh` — the whole deploy, idempotent

```bash
#!/usr/bin/env bash
set -euo pipefail
ACR="$1"; IMAGE="$2"; TAG="$3"; REV="${4:-$TAG}"
az login --identity
DIGEST=$(az acr repository show -n "$ACR" --image "$IMAGE:$TAG" --query digest -o tsv)
cd /opt/testforge
# Pin by digest. `latest` makes rollback meaningless and makes "which build is this?" unanswerable.
sed -i -E "s#image: ${ACR}/${IMAGE}(@sha256:[0-9a-f]+)?:?.*#image: ${ACR}/${IMAGE}@${DIGEST}#" docker-compose.prod.yml
# RENDER_GIT_COMMIT is the ONLY name the app reads for its revision (§6.4). Written to the env
# file rather than baked into the compose file so a digest rollback updates it too.
# grep-then-append: a bare `sed s#^KEY=...#` is a silent no-op when the line is absent,
# which is exactly the state a first deploy is in.
ENVF=/etc/testforge/.env
if grep -q '^RENDER_GIT_COMMIT=' "$ENVF"; then
  sed -i -E "s#^RENDER_GIT_COMMIT=.*#RENDER_GIT_COMMIT=${REV}#" "$ENVF"
else
  printf 'RENDER_GIT_COMMIT=%s\n' "$REV" >> "$ENVF"
fi
chmod 600 "$ENVF"
docker login "${ACR}" -u "${ACR}" --password-stdin <<< "$(az acr login --expose-token --query accessToken -o tsv)"
docker compose --env-file /etc/testforge/.env -f docker-compose.prod.yml pull testforge
docker compose --env-file /etc/testforge/.env -f docker-compose.prod.yml up -d --remove-orphans
docker image prune -f          # else the 64GB disk fills with each deploy (~700MB/image)
```

Because a deploy restarts the service, `boot_hygiene()` will cancel in-flight runs. The app already
accounts for this (`TF_QUEUE_ORPHAN_GRACE`, `TF_QUEUE_STALE_MINUTES` in `render.yaml`) — but the
operator does not know that unless you say it. Put a drain step in the pipeline:
`POST /api/runs/queue/clear` then poll `/api/runs/queue/status` until `pending == 0`, or accept that
"deploys cancel queued runs" appears in the changelog.

---

## 7. Repository publishing: the Azure-specific decision

### 7.1 Exactly one environment may hold `TF_GITHUB_TOKEN`

If Render and Azure both publish `library/` to `main` with the same token, they will race: both
compute a tree from their own disk state, both create commits, one gets a `409` conflict, and — per
`readme.md` — that is reported as *"did not complete and will be retried in 60s"*, so it retries
forever while quietly alternating winners.

* **Azure = publisher.** Holds `TF_GITHUB_TOKEN`, `TF_LIBRARY_PUBLISH` unset or `1`.
* **Render = reader.** `TF_GITHUB_REPO` present for log coordinates, `TF_GITHUB_TOKEN` **absent**.
  `publish_mode()` → `disabled`, and the code deliberately reports *why* rather than guessing
  (`publish_disabled_reason()`), so the demo's GitHub tab states "publishing is disabled" instead of
  pretending. This is the correct, already-implemented behaviour — do not "improve" it.

### 7.2 The pipeline must not also push to `main`

The app pushes `library/` at runtime. CI pushes images and *reads* code. If a pipeline ever commits
generated `library/` output back to `main`, it is racing a human editing the same tree through the
dashboard. Make CI write only to tags/releases, never to a tracked path.

### 7.3 If you do want `checkout` mode on the VM

Legitimate reason: you want the VM's library to be a real git working tree so the GitHub tab shows
`git_state` and can push without a token in the environment. Then:

* mount the checkout at `/app/library` and **do not** `COPY library` into the image for that deployment
* give git credentials that survive a reboot: a credential helper pointing at a Key Vault-backed file,
  or an SSH deploy key — `git@github.com` needs egress on 22 to GitHub, or `url.https://github.com/.insteadOf`
* expect `publish_mode()` = `checkout` and confirm it via `GET /api/library` → `publish_mode`
* accept that the VM can now force-adjacent things with git that the API client is bounded against
  (the API path caps file count, per-file size and push size; the git path does not)

Recommendation: stay on `api` mode (§6.4). One code path across both environments, bounded publishes,
no credentials in a working tree.

---

## 8. Keeping the Render demo alive in parallel

`render.yaml` already exists and is correct — `plan: free`, `region: singapore`, `runtime: docker`,
`autoDeployTrigger: commit`, `healthCheckPath: /api/health`. **Change nothing in it except removing
the misleading `API_Key` entry** (§0.1). Leave it on its own branch trigger; it will keep deploying on
every push to `main`, including the pushes the Azure pipeline makes, so the demo stays in sync for free.

What to expect and document, rather than fight:

| Behaviour | Cause | Accept / mitigate |
| --- | --- | --- |
| 30–60 s first request after idle | free tier spins down after ~15 min **[verify]** | Accept. It is a demo. Say so on the landing page. |
| ~750 h/month **per workspace** | free allowance is shared, not per-service **[verify]** | Do not add a keep-alive pinger — a 5-minute pinger keeps the service warm *and* consumes the whole monthly allowance, risking overage. Sleep is how the free tier stays free. |
| Runs/recordings vanish on redeploy | ephemeral filesystem, no disk | Accept (§5.4). Durable record is `library/` on GitHub. |
| Chromium OOM | 512 MB, 0.1 CPU | Already handled: `TF_MAX_BROWSERS=1`, `TF_BATCH_MAX_RSS_MB=420`, `TF_BROWSER_SINGLE_PROCESS=1`. Do not raise any of these for Render. |
| `region: singapore` | chosen for the region, not for you | Leave it; latency to Australia is ~10–25 ms, and moving it does not help the free tier. |

Add a one-line note to `render.yaml` and the Guide tab stating the demo is read-only and disposable,
so nobody spends an hour "debugging" lost data that was never designed to persist.

---

## 9. Verification gate — put this in the pipeline, not in a wiki

`deploy/azure/smoke.sh`. Every assertion below is derived from what `/api/health` and
`/api/diagnostics` actually return, so it fails loudly on a real regression instead of checking a
status code.

```bash
#!/usr/bin/env bash
set -euo pipefail
BASE="${1:?base url}"; WANT_REV="${2:?commit}"
fail(){ echo "SMOKE FAIL: $*" >&2; exit 1; }

for i in $(seq 1 30); do
  code=$(curl -sS -o /tmp/h.json -w '%{http_code}' "$BASE/api/health" || true)
  [ "$code" = 200 ] && break
  sleep 5
done
[ "${code:-0}" = 200 ] || fail "health did not become 200 (last=$code)"

jq -e '.status=="ok" and .database=="ok"' /tmp/h.json >/dev/null || fail "database not ok"
jq -e '.recorder and .executor and .batch_executor' /tmp/h.json >/dev/null || fail "subsystems missing"
# publish_enabled must be TRUE on Azure and FALSE on Render — the opposite of expectation is the
# single most likely misconfiguration after §7.1, so assert it per environment.
jq -e '.library_publish_mode=="api"' /tmp/h.json >/dev/null || fail "publish mode is not api"
jq -e '.guardrails' /tmp/h.json >/dev/null || fail "guardrails absent"

curl -sSf "$BASE/api/diagnostics" > /tmp/d.json
jq -e '.schema.ready==true' /tmp/d.json >/dev/null || fail "schema not ready"
jq -e '.schema.error==null' /tmp/d.json >/dev/null || fail "schema error: $(jq -r .schema.error /tmp/d.json)"
# A repair is not a failure, but an unreviewed repair on a fresh DB means the image is older than the DB.
[ "$(jq '.schema.repairs|length' /tmp/d.json)" = 0 ] || echo "##vso[task.logtype value=1]schema repairs applied: $(jq -c .schema.repairs /tmp/d.json)"

# WebSocket routes must upgrade; a proxy that eats them breaks the recorder silently.
ws=$(curl -sS -o /dev/null -w '%{http_code}' -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     "$BASE/ws/runs/00000000-0000-0000-0000-000000000000" || true)
case "$ws" in 101|403|404) : ;; *) fail "websocket route returned $ws, expected 101/403/404" ;; esac

echo "SMOKE OK rev=$(jq -r .revision /tmp/h.json)"
```

> Note what the script does **not** assert on `diagnostics`. `report["status"]` in that endpoint is a
> hard-coded `"ok"` string, emitted before any of the schema or database checks — so an HTTP 200 or a
> `status == "ok"` test proves only that the process is alive. `schema.ready` and `schema.error` are
> the fields that actually report a broken deployment, because `init_db()` swallows every exception
> into `SCHEMA_STATUS` rather than crashing the container (§0.4).

Then two manual checks that no automated test here will catch, because they need a browser:

1. **Record → save → replay, end to end, on Azure.** Open the record tab, leave the URL blank to use
   the built-in sample app, click on the picture, type in the text box, SAVE, then RUN from the
   Library. This is the only test that exercises Chromium-in-container + the live screencast + the
   publish path at once. The QA doc calls for it explicitly (`docs/qa-automation-acceptance.md`).
2. **Confirm the publish actually reached GitHub.** The GitHub tab verifies the remote SHA;
   `GET /api/library` returns `last_api_push` and `publish_mode`. A save that says "Published to
   owner/name@branch" is the only acceptable result for the Azure environment (§7.1).

Also assert the **negative**: from a machine outside the allow-list, `curl https://vm-ip:8000` must
fail. If it answers, your NSG or bind is wrong and §6.2 has not been applied.

---

## 10. Rollback

1. `az vm run-command ... deploy/azure/apply.sh $ACR testforge <previous-sha>` — you can only do this
   because §4.1 tags by commit and never overwrites a digest.
2. **Database:** the boot-time repair only *adds* columns and *relaxes* NOT NULL — it never drops. So
   an app rollback on a Postgres that a newer image already widened is safe (extra nullable columns
   are ignored). It is safe **in that direction only**: if a newer release changed a column's
   semantics, rolling the image back does not roll the data back. Treat `SCHEMA_STATUS.repairs` in
   `/api/diagnostics` as the audit trail of what changed under you.
3. If you roll back past a `library/` publishing change, note that `library/` commits are already on
   GitHub and are *not* rolled back with the image.

---

## 11. Degraded mode (the "pipeline or VM unavailable" requirement)

| Failure | Impact | Operator action |
| --- | --- | --- |
| Azure DevOps down | No new builds/deploys. **GitHub Actions + Render unaffected** (mirror is one-way, so GitHub never waits on Azure). | Nothing. Record on Render or commit as usual; deploy later. |
| ACR / `AcrPull` broken | Deploy fails; running container unaffected (image is local). | `docker compose up` on the already-pulled digest. |
| VM down | Primary UI unreachable, but `library/` on GitHub is intact; Render demo still serves everything except the last unpushed saves. | Recover VM, or temporarily point DNS at Render. |
| Postgres down | `/api/health` returns **503** "Database is unavailable" — which is exactly why `healthCheckPath: /api/health` exists instead of `/`. Containers restart-loop; the app does not serve stale DB rows. | Restore server. Do not "work around" by letting the app fall back to SQLite while a broken `DATABASE_URL` is set — it will, because `config.py` only uses SQLite when the variable is **unset**; a bad URL fails loudly, which is right. |
| GitHub API outage | Pushes fail; per the runbook a failed push keeps the local library and retries at 60 s. Saves must not be lost. | Wait; verify `last_api_push` after recovery. |

Document it as: **the pipeline is a convenience, the runtime is the product, GitHub is the archive.**
That is what "still works with the GitHub repository" means in practice.

---

## 12. AI prompt library

The value of these is that the constraints are already inside them. A prompt that says "deploy this
FastAPI app to Azure" gets you a blog post; one that says "the app has no authentication, one worker
is a correctness requirement, and the schema self-heals at boot" gets you working config. Paste one,
then paste the repo's `Dockerfile`, `render.yaml` and the output of `grep -n "TF_" app/config.py`.

**P1 — Provisioning**
> Produce an idempotent Azure CLI (or Bicep) script for: resource group `rg-testforge-eas` in
> `australiaeast`; VNet 10.40.0.0/16 with subnets `sn-app` and `sn-data`; one `Standard_B2ats_v4`
> Ubuntu 22.04 VM with a 64 GB OS disk (must stay at or under 64 GB for free-tier eligibility) and a
> 64 GB data disk formatted xfs and mounted at `/mnt/artifacts`; an NSG allowing only 443 inbound,
> explicitly denying 22 from `0.0.0.0` and allowing it only from `AzureBastionSubnet`, with **no rule
> for port 8000**; an ACR with admin user **disabled** and the VM's system-assigned identity granted
> `AcrPull`. Every resource reused if it already exists, identified by name. Refuse and exit nonzero
> rather than silently charging: fail if the VM size is not free-eligible or the disk exceeds 64 GB.
> No secret may appear in a file the script writes to the repo.

**P2 — PostgreSQL**
> Create an Azure Database for PostgreSQL Flexible Server for a FastAPI/SQLAlchemy app that **runs
> `Base.metadata.create_all()` plus `ALTER TABLE ... ADD COLUMN` at container start**, so the runtime
> role needs `CREATE` on one schema and cannot be DML-only. Requirements: Burstable `B2ms`, 32 GB,
> TLS 1.2 minimum, `--public-access none`, VNet-integrated into `sn-data`, a separate admin login and
> an app login, `sslmode=require` in the DSN, PgBouncer evaluated against the app's
> `pool_size=20, max_overflow=10` (30 connections per process, exactly one process). Show the
> URL-encoding step for a generated password containing `%` and `~`, and explain the failure mode if
> I skip it. Do not add Alembic.

**P3 — Pipeline**
> Write a single `azure-pipelines.yml` for this repo. Constraints: `Dockerfile` builds
> `python:3.11-slim-bookworm` + `playwright install --with-deps chromium`; unit tests are
> `tests/test_unit.py` and must run **inside the built image** so the test and production browsers
> match; the app is a UI test recorder needing Chromium, so no second worker or replica; push images
> to ACR **tagged by commit SHA and never `latest`-only**; PR builds must not push; deploy to an Azure
> VM by digest through `az vm run-command` executing `deploy/azure/apply.sh`; a post-deploy gate runs
> `deploy/azure/smoke.sh` asserting `status=ok`, `database=ok`, `library_publish_mode=api` and
> `schema.ready=true`; `environment: azure-primary` carries the approval. Also set
> `RENDER_GIT_COMMIT` — it is the *only* variable the app reads for its revision (both `/api/health`
> and `/api/diagnostics`), and nothing supplies it outside Render, so an Azure deployment that skips it
> permanently reports `revision: local`. Use Microsoft-hosted agents; do not put a self-hosted agent on
> the 1 GB app VM.

**P4 — Repository mirror**
> Give me a one-way GitHub→Azure Repos mirror that cannot loop: a bare clone, `git push --mirror`
> preserving all branches and tags, handling shallow clones, and preventing the mirror from triggering
> the other system's CI. The destination must be treated as read-only by humans, so include the
> Azure Repos branch policy that blocks direct pushes while letting the mirror-bot identity write, and
> a note explaining that `--mirror` deletes branches anyone else pushed. State which PAT scopes are
> needed on each side and why read-only fails.

**P5 — Auth at the proxy (the real gap)**
> This application has **no authentication of any kind**: there is an `API_Key` env var in
> `render.yaml` that no code reads, and every endpoint including `POST /api/runs`,
> `POST /api/library/publish` and `GET /api/diagnostics` is open. It can launch Chromium on its host
> and push to a GitHub repo. Without modifying the app, design Entra ID Application Proxy (or an
> OAuth2-proxy sidecar in front of Caddy) so that: only allow-listed principals reach it; the three
> WebSocket routes (`/ws/record/{rid}`, `/ws/runs/{id}`, `/ws/batches/{id}`) still stream without
> buffering or idle timeouts; a WebSocket upgrade cannot be used to bypass the auth check; and the
> container stays bound to `127.0.0.1:8000` with no NSG rule for 8000. Show how to verify the bypass
> case. Then list the smallest in-app change that fixes this properly.

**P6 — Debug the unglamorous failures**
> Write a triage runbook for this Azure deployment covering, with commands and the exact signal to
> look for: (1) the service is fine on Render and the VM's Chromium dies on launch (missing shared
> libraries vs. OOM kill — how to tell from `dmesg` and `/api/diagnostics` RSS vs. cgroup limit);
> (2) the recorder shows a live frame on Render but a frozen one on the VM (proxy buffering
> WebSocket frames — `flush_interval`); (3) a save reports *"did not complete and will be retried in
> 60s"* forever (token read-only → 403 on `git/blobs`, or two environments racing on `main`);
> (4) `/api/health` returns 503 while the app is up (`Database is unavailable`, `SELECT 1`);
> (5) connection pool exhausted with `max_connections` errors at 30 connections per process; (6) the
> OS disk full from Docker images and json-file logs.

**P7 — Cost**
> Produce a table of monthly cost for this Azure topology under (a) an Azure free account in month 1
> and month 13, and (b) pay-as-you-go, listing: B-series VM hours against the 750 h/month cap,
> managed disks by SKU and size (and why exceeding 64 GB breaks free eligibility), PIP and egress,
> ACR (including whether it is free-eligible at all), Flexible Server compute and non-shrinkable
> storage, and Azure DevOps beyond 1800 pipeline minutes. Mark every number I must re-verify today.
> Then give the cheapest configuration that still keeps a persistent `library/` and artifacts.

**P8 — Acceptance criteria**
> From `docs/qa-automation-acceptance.md`, extract the criteria that depend on the **deployment**
> rather than the code (memory ceilings, preview capture, publish verification, log sourcing,
> `save_variable` round-trip) and turn them into an environment-specific acceptance checklist for
> Azure with a pass/fail command or UI action for each. Call out which criteria are already covered by
> `tests/test_unit.py` so I do not automate them twice, and which are impossible to test without a
> human at a browser.

---

## 13. Common failures

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker: unauthorized` on the VM | `AcrPull` on the wrong identity, or admin user assumed on | §6.3; `az vm run-command` `docker login` via `--expose-token` |
| Pipeline can't read a Key Vault variable | Linked secrets are not usable in YAML expressions | map into `env:` on the task |
| `FATAL: password authentication failed` with a valid password | unencoded `%`/`~` in the DSN (§5.3) | URL-encode |
| Health `revision: local` on Azure | nothing injects `RENDER_GIT_COMMIT` outside Render, or compose interpolated it without `--env-file` | §6.4 / §6.5 |
| `/api/diagnostics` returns 200 with `"status":"ok"` while the schema is broken | that field is a hard-coded literal; the real signal is `schema.ready` | assert `schema.ready`, never HTTP status (§9) |
| First request after deploy fails once, then fine | container restart + Chromium cold boot, or `start_period` too short | raise `start_period`; keep `/api/health` as the probe |
| Recorder canvas frozen but clicks register | proxy buffering WebSocket frames | `flush_interval -1` (§6.4) |
| Runs vanish after a deploy | `boot_hygiene()` reaping orphans — correct behaviour | drain before deploy (§6.5); never share a DB (§0.3) |
| `library/` oscillates between two histories | both environments pushing to `main` | §7.1 |
| VM billed despite "free" tier | 2+ B-series VMs, disk > 64 GB, or Standard SSD/Premium chosen | one VM, ≤64 GB, `Standard_LRS` |
| `GET /api/diagnostics` shows a schema repair on a fresh DB | image older than the database it points at | redeploy the digest the pipeline built; do not hand-patch the schema |

---

## 14. Definition of done

* [ ] GitHub is unchanged for humans: they push there and Actions still builds Render.
* [ ] Azure Repos holds a complete mirror (all branches/tags), with a policy that keeps it read-only
      for people and writable by the mirror identity.
* [ ] One pipeline run: tests inside the image → ACR digest → VM → `smoke.sh` green, including
      `library_publish_mode=api` and `schema.ready=true`.
* [ ] The VM listens only on loopback; port 8000 unreachable from outside (verified negatively);
      22 reachable only via Bastion/JIT; TLS valid with HSTS.
* [ ] Human auth exists at the proxy, and a request from outside the allow-list gets 403 on both HTTP
      and a WebSocket upgrade attempt.
* [ ] Azure holds the only runtime `TF_GITHUB_TOKEN`; Render reports publishing disabled, by design.
* [ ] Each environment has its own database and its own artifacts volume.
* [ ] A recorded project survives an Azure VM reboot and appears on GitHub; a Render demo save does not
      survive a redeploy and says so.
* [ ] Rollback executed once against the previous digest, with the DB left forward-compatible.
* [ ] The Guide tab in the app links this file and the three runbooks, with P1–P8 copy-pasteable.
