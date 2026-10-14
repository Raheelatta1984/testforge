# TestForge CI/CD pipelines

How the team builds, tests and ships a change. Two pipelines are described:

- **GitHub Actions** — a committed workflow, `.github/workflows/ci.yml`, that runs
  the test harness on every push and pull request.
- **Azure Pipelines** — the build-once/deploy-once pipeline described in
  `docs/azure-devops-deployment-guide.md` §4, which lives in Azure Repos.

Use one or the other for deployment. Running both against the same registry tag
produces two artifacts and an argument about which one is live.

## 1. What CI must prove

| Gate | Command | Why it matters |
| --- | --- | --- |
| Unit suite | `python -m unittest discover -s tests -p "test_*.py" -t .` | Fast, no browser, catches import and contract breaks |
| Full harness | `python -m tests.harness` | Unit suite plus the library scenarios through a live server |
| Browser present | `python -m tests.harness --require-browser` | A missing Chromium silently skips scenarios; CI must not accept that |
| Image builds | `docker build .` | A `COPY` source stripped by `.dockerignore` fails only here |

The harness exit status is the build result. A green build that skipped the
browser scenarios is a yellow build wearing a green badge.

## 2. GitHub Actions

`.github/workflows/ci.yml` is committed in this repository:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  unit:
    name: Unit suite (no browser)
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt -r requirements-test.txt
      - run: python -m unittest discover -s tests -p "test_*.py" -t .

  harness:
    name: Full harness (with Chromium)
    runs-on: ubuntu-latest
    timeout-minutes: 40
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the harness records the commit it tested
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt -r requirements-test.txt
      - run: python -m playwright install --with-deps chromium
      - run: python -m tests.harness --require-browser
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: harness-logs
          path: logs/
          retention-days: 14
```

Notes:

- **No publishing credentials in CI.** The harness strips `TF_GITHUB_TOKEN`,
  `GITHUB_TOKEN`, `GH_TOKEN`, `TF_GITHUB_REPO`, `TF_GITHUB_BRANCH` and
  `TF_GIT_REMOTE` from the server it starts and sets `TF_LIBRARY_PUBLISH=0`. Do
  not add a secret that would let a test push to `main`.
- The `logs/` artifact is how you read a failure. Every harness run writes
  `harness.log`, `unit.log`, `scenarios.log`, `server.log`, `results.md` and
  `results.json` under `logs/<timestamp>/`.
- `fetch-depth: 0` is not decoration: `git rev-parse` is how the report records
  which commit was tested.

## 3. Azure Pipelines

The full file, with the reasoning inline, is in
`docs/azure-devops-deployment-guide.md` §4.1. Its shape:

1. **Build once.** One job builds the image, tags it with the commit SHA, pushes
   it to Azure Container Registry and publishes the digest as a pipeline
   artifact.
2. **Test in the same build**, before the push: the unit suite, then the harness.
3. **Deploy by digest.** Each environment's release downloads the image *digest*
   artifact and deploys that exact digest — never `latest`.
4. **Verify after deploy.** The pipeline curls `/api/health`, compares `revision`
   with the source commit and fails the release if they differ.
5. **Secrets in the variable group**, marked as secrets, referenced by name.

Branch policy on `main` in Azure Repos: require a pull request, at least one
reviewer, and a passing build (§3.3 of that guide).

## 4. Deploy stage

Whatever triggers it, a deploy is these five steps (see
`docs/dev-deployment-end-to-end.md` §6 for the verification detail):

```bash
REV=$(git rev-parse --short HEAD)
docker build -t "$REGISTRY/testforge:$REV" .
docker push "$REGISTRY/testforge:$REV"
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$REGISTRY/testforge:$REV")

ssh deploy@$HOST "cd /opt/testforge \
  && sed -i \"s#testforge@sha256:.*#testforge@sha256:${DIGEST##*@sha256:}#\" docker-compose.prod.yml \
  && REV=$REV docker compose -f docker-compose.prod.yml pull \
  && REV=$REV docker compose -f docker-compose.prod.yml up -d"

curl -fsS https://testforge.<YOUR DOMAIN>/api/health
```

The last line is the gate. If `revision` is not `$REV`, the deploy failed even
though every command returned 0.

## 5. Environment promotion

| Environment | Database | Library publishing | Purpose |
| --- | --- | --- | --- |
| `dev` | Its own database (SQLite is fine) | `TF_LIBRARY_PUBLISH=0` | Feature work, the harness |
| `test` | Its own Azure SQL database | `disabled` or a scratch repository | Acceptance, demo data |
| `prod` | Its own Azure SQL database | The one environment holding a write token | Real use |

No two environments share a database (§0.3 of the Azure guide). No environment
except `prod` holds a token that can write to the library repository.

## 6. What a release record must contain

- The commit SHA and the image **digest**.
- The harness report directory for that SHA (`logs/<timestamp>/results.md`).
- The `DATABASE_URL` host (never the credentials) and the schema state from
  `/api/diagnostics`.
- The rollback digest.
- Who deployed, and the post-deploy record-and-replay result.

If you cannot produce the digest and the harness report, you do not know what is
running.
