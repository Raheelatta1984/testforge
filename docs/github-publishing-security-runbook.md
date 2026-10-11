# Runbook: Enable GitHub Publishing Securely (TestForge)

**Scope:** the two dashboard messages about "no .git directory" / "no API token",
and the complete, security-first procedure for giving TestForge a GitHub token so
saves publish to `Raheelatta1984/testforge` — without ever exposing that token.

**Golden rule (read first):** the token is a password. It must never appear in
this repository, in chat with any AI agent, in an issue, in a screenshot, in a
commit message, or in a shell history you will paste anywhere. If it ever does,
that token is burned — revoke it in 30 seconds (step 8) and mint a new one.

---

## 1. Why you see those messages (they are status, not errors)

Your deployment builds a Docker image from `app/`, `library/` and `logs/` only
(`Dockerfile`). The image therefore contains **no `.git` directory**, and the app
cannot `git push`. Publishing still works — but through the **GitHub REST API**
(`app/github_api.py`), which needs two things set as environment variables:

| Variable | Where it is already declared | Value today | Meaning |
| --- | --- | --- | --- |
| `TF_GITHUB_REPO` | `render.yaml` | `Raheelatta1984/testforge` | *Which* repository may be pushed to (opt-in). |
| `TF_GITHUB_BRANCH` | `render.yaml` | `main` | *Which* branch. |
| `TF_GITHUB_TOKEN` | `render.yaml` (`sync: false`) | **missing** | *Permission* to push. This is the encrypted secret you add. |

`sync: false` is Render's marker that the value is a **secret held only in the
Render Dashboard** — it is deliberately not stored in the YAML file, so it can
never be committed to git. Until the secret is set, the app reports exactly what
you saw:

* **Record screen:** *"SAVE keeps the recording on this instance only…"* — saves
  write to the instance's disk, nothing is published.
* **GitHub tab:** *"…no branch to verify or publish to…"* — same root cause.

Both messages come from one function (`library_store.publish_disabled_reason()`)
so the tabs cannot disagree. When the token arrives, they flip to *"SAVE
publishes to Raheelatta1984/testforge@main through the GitHub API"*.

**One token serves both purposes you asked about.** "Push into GitHub" and
"GitHub API" are the same mechanism here: the app commits `library/` with the
Git Data API (blobs → tree → commit → ref update) and then reads the branch back
to verify. There is no separate credential for the API.

---

## 2. Threat model (what we are defending, all the time)

| Threat | Control in this project | Control you apply (steps below) |
| --- | --- | --- |
| Token committed to git | Token is read from env only; `render.yaml` uses `sync: false`; `.gitignore` blocks `.env*` | Never type the token into a file inside the repo or into chat |
| Token leaked in logs / API responses | `github_api._safe()` redacts tokens, headers and URL credentials from every error; `describe()` never returns the token | Verify with the leak checks in step 6 |
| Over-privileged token | App only needs to write repository *contents* | Fine-grained PAT, **one repo**, **Contents: Read and write**, nothing else (step 3) |
| A stray CI token pushing by accident | Publishing is opt-in: no `TF_GITHUB_REPO` named → nothing publishes even if `GITHUB_TOKEN` exists (unit test `UT-PUB-07`) | Keep `TF_GITHUB_REPO` pointed only at this repo |
| One save uploading too much | Pushes bounded by `TF_GITHUB_MAX_FILES` / `TF_GITHUB_MAX_FILE_BYTES` / `TF_GITHUB_MAX_PUSH_BYTES`; skipped files are reported, not hidden | Review `GET /api/library/publish/plan` before the first real push (step 6) |
| Token sitting live forever | — | 60–90 day expiry + rotation calendar (step 7) |
| Someone else's fork using your token | — | Token is scoped to `Raheelatta1984/testforge` only — a fork cannot use it |
| Token stolen (leak, malware, shoulder-surf) | — | Incident response: revoke → re-mint → check audit log (step 8) |

---

## 3. Step 1 — Mint a least-privilege token (GitHub, 3 minutes)

Use a **fine-grained Personal Access Token**, not a classic one. A classic
`repo`-scoped token can read/write *every* repository you own and your
org membership — far more than this app needs.

1. Log in to GitHub as an owner of `Raheelatta1984/testforge`. **Make sure 2FA is
   enabled on the account first** (Settings → Password and authentication). If
   2FA is off, stop and turn it on — the token sits in a cloud runtime and 2FA is
   the backstop for account takeover.
2. Go to **Settings → Developer settings → Personal access tokens → Fine-grained
   tokens → Generate new token**. (`https://github.com/settings/personal-access-tokens/new`)
3. Configure it with least privilege in mind:
   * **Resource owner:** `Raheelatta1984` (the account/org that owns the repo).
   * **Expiration:** 60–90 days. *An expiring token is a self-healing limit on
     the damage a leak can do.* Put the expiry date in your calendar.
   * **Repository access:** *Only select repositories* → **`Raheelatta1984/testforge`**.
     Never "All repositories".
   * **Permissions → Repository permissions:** set **Contents: Read and write**.
     Nothing else. (Metadata: Read is added automatically and is harmless.)
     Do **not** grant Administration, Actions, Secrets, Variables, Workflows,
     or anything else — the push code only touches repository contents.
4. Generate and copy the token (`github_pat_…`). **This is the only time it is
   shown.**

**Do not paste it into this chat, into any AI tool, into an email, or into a
note-taking app.** You will paste it exactly once, directly into the Render
Dashboard secret field (step 4), from your clipboard.

> Classic PAT fallback (not recommended): scope `repo` only, same expiry rule,
> same 2FA requirement. It is strictly more powerful than what this app needs.

> Advanced alternative: a **GitHub App** installation token (auto-rotating,
> ~1 hour lifetime, repo-scoped) is the gold standard, but needs a small
> token-minting sidecar. Fine-grained PAT with 60-day expiry is the right
> trade-off for this project today.

---

## 4. Step 2 — Store it as an *encrypted* environment variable

You asked whether an encrypted variable can hold the token so the app can use it
for pushing and for the GitHub API. **Yes — and it is already wired for it.**
The secret lives in your hosting platform's secret store (encrypted at rest),
the app reads it from the process environment at call time, and it never touches
the repository. You set it in the provider's UI; an agent working in the git
checkout must never receive it.

### 4a. Render (your current deployment — `render.yaml`)

1. Open **Dashboard → UI-Test-Forge → Environment**.
2. `TF_GITHUB_REPO` and `TF_GITHUB_BRANCH` should already be present (from the
   blueprint). Find **`TF_GITHUB_TOKEN`** — it is declared with `sync: false`,
   which means Render left the value for you to fill in.
3. Click its value field and paste the token. Save. Render stores environment
   values **encrypted at rest** and masks them in the UI afterwards (it will show
   as `••••••`), and `sync: false` guarantees the value is never written back
   into `render.yaml` or git.
4. Save triggers a redeploy (or press **Manual Deploy**). The new instance picks
   the token up from its environment at runtime — no image rebuild needed and
   the token is **never baked into the Docker image** (the Dockerfile's comment
   block exists to keep it that way).

### 4b. Local / self-hosted with docker-compose

1. In a terminal on the host (not in any repo file), export it for the compose
   session only:
   ```bash
   export TF_GITHUB_TOKEN=github_pat_...   # host shell only
   TF_GITHUB_REPO=Raheelatta1984/testforge docker compose up -d
   ```
   `docker-compose.yml` already passes `TF_GITHUB_TOKEN: ${TF_GITHUB_TOKEN:-}`
   into the container. Compose interpolates it from the shell; the value stays
   out of the file and out of git.
2. If you prefer a file, use an untracked `.env` next to `docker-compose.yml`
   (`TF_GITHUB_TOKEN=github_pat_...`). **`.gitignore` now blocks `.env*`** so it
   cannot be committed. Also `chmod 600 .env`.
3. Never `docker commit`/push an image built with the token in `ENV`, and never
   `docker inspect` a running container in a screen share.

### 4c. What I (or any agent) can and cannot do with your token

* **Cannot:** reach into your Render account to set the secret, and **must not**
  be given the token in chat — a secret shared with a third party is a secret
  you no longer control.
* **Can (and already does):** read `TF_GITHUB_TOKEN` from the runtime environment
  and use it only against `api.github.com` for this repo, scrubbing it from every
  error message (`github_api._safe()`), bounding every push, and refusing to
  publish anywhere you did not name in `TF_GITHUB_REPO`.
* The two minutes of pasting into the Render Environment field is the one step
  that must be done by a human. That is a feature, not a limitation.

---

## 5. Step 3 — Verify (the "pen test" of your own configuration)

Trust nothing until you have verified it. In order:

1. **UI check:** open the dashboard → GitHub tab. The amber warning must be gone;
   you should see *"…publishes Raheelatta1984/testforge@main through the GitHub
   API"* and the record screen should say *"SAVE publishes to …"*.
2. **Status check:**
   ```bash
   curl -s https://<your-service>/api/library | python3 -m json.tool
   ```
   Expect `publish_mode: "api"`, `publish_enabled: true`, and
   `api_publish.available: true`. The response must **not** contain your token —
   check with `curl -s …/api/library | grep -i github_pat` (expect no output).
3. **Plan before you push (dry run):**
   ```bash
   curl -s https://<your-service>/api/library/publish/plan | python3 -m json.tool
   ```
   Review `added/changed/removed`. Only `library/**` paths may appear. If
   anything outside `library/` shows up, stop and investigate.
4. **First publish with a dry run:**
   ```bash
   curl -s -X POST https://<your-service>/api/library/publish \
        -H 'Content-Type: application/json' -d '{"dry_run": true, "message": "test"}'
   ```
   then without `"dry_run"` when the plan looks right. A successful push is only
   reported after the branch ref is **read back** and matches the new commit
   (built-in verify step in `github_api.publish()`).
5. **Confirm on GitHub:** open `https://github.com/Raheelatta1984/testforge/commits/main`
   and find the commit ("…committed to main and verified"). Commit author/summary
   must contain no secrets.
6. **Negative permission test (least privilege proof):** with the same token in
   your clipboard, check it *cannot* reach anything else — expect `404`/`403`:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' \
        -H "Authorization: Bearer $TF_GITHUB_TOKEN" \
        https://api.github.com/repos/Raheelatta1984/some-other-repo
   curl -s -o /dev/null -w '%{http_code}\n' \
        -H "Authorization: Bearer $TF_GITHUB_TOKEN" \
        https://api.github.com/repos/Raheelatta1984/testforge/actions/secrets
   ```
   A 200 on the second call means you over-scoped the token — revoke it and
   re-mint with only **Contents: Read and write**.
7. **Log scrub check:** force any harmless failure (e.g. temporarily set an
   invalid branch) and grep the app logs and `GET /api/diagnostics` output for
   `github_pat` — expect zero hits (the redactor runs before any message leaves
   the process).
8. **Repo hygiene check:**
   ```bash
   git log --all -p | grep -i github_pat   # expect zero hits in history
   ```
   Also flip on GitHub **Push protection / Secret scanning** for the repo
   (Settings → Code security) so any future leak is flagged before it lands.

---

## 6. Step 4 — Confirm the record screen end-to-end

1. Record a tiny test flow on the Record tab and press **SAVE**.
2. The response banner must read *"published to main"* (not *"saved locally"*).
3. `GET /api/library` shows the new revision under `revision`/`remote_sha`.
4. The GitHub tab's project list now includes the saved recording under `library/`.

If a push fails (network blip, rate limit), the app keeps the files locally and
schedules a retry — the banner says so. Files are never lost; the token is never
printed.

---

## 7. Ongoing operations (security is a process, not a setup)

| Cadence | Action |
| --- | --- |
| Every 60–90 days | Rotate: generate a new fine-grained PAT, replace the value in Render → Environment, revoke the old token. The expiry in step 3 is your reminder. |
| On any suspicion of leak | See step 8. |
| On people leaving the project | Rotate immediately. |
| Monthly | Glance at GitHub's token in `Settings → Developer settings` and at the repo's **Audit log** (org) / **Security log** (personal) for unexpected `git.*` / `contents.*` events from the token. |
| After every deploy | `GET /api/library` still shows `publish_mode: "api"` (env vars survive redeploys; they do not survive a *new* service — re-add the secret there). |

**Branch protection trade-off:** right now the token pushes straight to `main`.
If you enable branch protection on `main` (recommended against force-push and
deletion), direct pushes will start failing — either allow this actor to push, or
point `TF_GITHUB_BRANCH` at a `library-sync` branch and merge via PR review.
Either choice is defensible; unreviewed direct-to-main is the weakest.

---

## 8. Incident response — the token leaked (chat, commit, screenshot, log)

1. **Revoke first, talk later:** GitHub → Settings → Developer settings →
   Fine-grained tokens → **Revoke**. This is the only step that stops the bleed.
2. Mint a replacement with step 3 and put it in Render → Environment.
3. Check `https://github.com/Raheelatta1984/testforge/security` and the account
   security log for unknown activity (unexpected commits, new branches, pushes
   at odd hours).
4. If it landed in a **commit:** removing it from the tip is not enough — it is
   in history. Rotate (already done in 1–2), then purge history
   (`git filter-repo`) or accept the rotation as sufficient since the token is
   dead. A revoked token in history is a scar, not a wound.
5. If it leaked into a **log file or screenshot** you shared: treat the audience
   as hostile; rotation is the fix, not deletion of the copy.

---

## 9. FAQ

**"Can an AI agent add the encrypted variable for me?"**
Not safely, and not from inside this repository. The secret must be typed by a
human directly into the Render Dashboard (or a host shell), because the whole
security model rests on the token having exactly two copies: GitHub's issuer and
your platform's encrypted store. Everything else — the push logic, the scrubbing,
the bounds, the verification — is already built and tested (`98 unit tests`,
including *a stray token publishes nothing* and *errors never carry the token*).

**Why does the record screen say "SAVE keeps the recording on this instance only"?**
It is telling you the truth about the current state: without the token, a save
writes to `/app/library` on the container and to nowhere else. After step 4 it
publishes to `main` and verifies the ref.

**Is one token enough for both "push" and "GitHub API"?**
Yes. Pushing *is* a GitHub API call in this architecture (`app/github_api.py`
uses the Git Data API). `TF_GITHUB_TOKEN` is the only credential involved.

**What if I run this in GitHub Actions later?**
Do not confuse the workflow's automatic `GITHUB_TOKEN` with this feature. The
app deliberately refuses to publish unless `TF_GITHUB_REPO` is also named
(unit test `UT-PUB-07`), and the test harness strips all token variables before
booting the server (`tests/harness.py`). Keep it that way.

**Where is the token in the code?**
Nowhere. It is read at call time from the environment (`github_api._token()`),
sent only to `api.github.com` over TLS, and redacted from any message before it
leaves the process.
