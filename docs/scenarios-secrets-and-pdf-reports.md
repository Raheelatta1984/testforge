# Ordered scenarios, protected inputs and temporary PDF reports

## Using the dashboard

1. Select a project, for example **D365**.
2. In **Variables**, create persistent variables or parameters. Select **Encrypt at rest** for passwords/tokens. Save explicitly; typing no longer saves every keystroke. Use `{{password}}`, not a literal password, in recording steps.
3. In **Library → High-level scenarios**, create **Validate Application login & logout**.
4. Add a named execution plan. Link the existing **Login** recording first and **Logout** recording second. Up/down controls set exact order. The same recording may be linked more than once, including recordings from another project.
5. Edit each link's defaults and save the scenario. On **Review inputs & run**, edit one-off values without changing saved defaults.
6. Execution is sequential, in one shared browser context. No adaptive reordering or automatic retries are used for these ordered plans. A failure stops the plan and cancels the remaining recordings. A memory-limit intervention aborts rather than silently replacing the authenticated session.
7. Completed single runs and batches offer **View PDF**, **Download PDF**, and **Copy GitHub share link**. The latter needs successful remote publication and an unexpired attachment. Private repository access still applies.

A recording's **Run** button also opens an input review. **Save recording defaults** persists its defaults; **Run once** does not change the project or recording defaults.

### Precedence and persistence

For ordinary recordings: source-project defaults → selected scenario-project defaults (when different) → recording defaults → linked-entry defaults → one-off execution overrides. Project inputs and ordinary steps are frozen when queued. Saved definitions live under `library/projects/<project>/scenarios/<scenario>.json`, with revision checks preventing stale edits. Recording defaults live in their recording JSON.

For imported matrix/data plans: frozen project defaults → authored plan variables → dataset row → recording defaults → one-off overrides. Runtime captures can then update runtime variables. Existing import testing/receipt requirements still apply. Linked scenarios accept ordinary recordings, not nested matrix plans that intentionally isolate browser contexts; legacy loop markers must first be expanded. One batch is limited to 40 links and 2,000 expanded actions (including matrix/retry budgets). Large selections must be split.

Parameters use the same persistent storage and `{{name}}` interpolation as variables, with the `Parameter` category. They work in imported/AI-authored scripts. This change **does not implement the autonomous `app.agent`**.

## Encryption and access prerequisites

Install the pinned requirements. **Encryption then works with no configuration**: the first time a key is needed the application generates a Fernet key and stores it in its own database, alongside the rest of the application state. Because it is persisted rather than held in memory or on a container filesystem, it survives restarts and redeploys on hosts with no persistent disk — the failure mode that makes a key generated at boot unsafe.

Be clear about what that protects. The managed key keeps plaintext out of the library files published to GitHub, which is the realistic leak path for this application; it does **not** protect against anyone who already has the database, because the key is in it.

For a stronger boundary, keep the key outside the database by provisioning one **outside Git**:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Run that command in your own protected administration environment, then store the result in the hosting platform's secret configuration as `TF_VARIABLE_ENCRYPTION_KEYS`. Do not paste it into a ticket/chat, commit it, or bake it into the image. Back it up securely: losing it loses access to the values sealed with it. Docker Compose forwards this environment variable; Render declares it as an operator-provisioned secret (`sync: false` in `render.yaml`, so the value is **not** carried by the blueprint).

The environment keyring takes precedence for new writes as soon as it is set, and any key the application generated earlier stays in the ring for decryption — so switching to an operator-provisioned key does not strand values already encrypted. Set `TF_MANAGED_ENCRYPTION_KEY=0` to exclude the managed keys entirely; with no environment key either, encryption is then genuinely unavailable and secret saves are refused rather than silently stored as plaintext.

### Rotating the managed key

`POST /api/encryption/rotate` (the **ROTATE KEY & RE-SEAL** button on the Variables tab) mints a new key, then decrypts and immediately re-encrypts every stored variable, recording default and scenario input with it, and publishes the result. Superseded keys are retained, so queued runs and older snapshots keep decrypting. A value whose key is no longer in the ring is reported in the `unreadable` count and left exactly as it was, never overwritten. The endpoint refuses with 422 when `TF_VARIABLE_ENCRYPTION_KEYS` is set, because rotation is then the operator's job: list the new key first, keep the old one (`TF_VARIABLE_ENCRYPTION_KEYS=new,old`) and restart.

### Checking whether this instance can encrypt

`GET /api/encryption` (also embedded in `GET /api/health` under `encryption`) reports readiness, carrying no key material:

```json
{"variable": "TF_VARIABLE_ENCRYPTION_KEYS", "source": "app-managed", "configured": true,
 "ready": true, "keys": 1, "env_keys": 0, "managed_keys": 1, "reason": null}
```

`source` is `environment`, `app-managed` or `unavailable`; `configured` is whether any key source exists; `ready` is whether the keyring actually loads; `keys`/`env_keys`/`managed_keys` are counts — enough to confirm a rotation from outside the service. When `ready` is false, `reason` is the same sentence the API returns when a save is refused. Note that this probe mints the managed key if there is none, which is what makes a zero-configuration deployment able to encrypt its first secret.

The **Variables** tab reads that report when it opens. Normally it shows a collapsed panel naming the key's source, the keyring size, the candid note about the managed key's limits, and the rotate control. If encryption has been switched off or the configured keyring is unusable, it instead shows the fix and disables **Encrypt at rest** rather than accepting a password into a field that is going to reject it; plaintext variables continue to save normally.

Encrypted values use authenticated Fernet envelopes (`enc:v1:...`) in the database, repository variable files, recording/link defaults, and queued input snapshots. Public variable responses omit the value; default maps return `[encrypted]`. Preserve that sentinel to keep an existing default. A password replacement is sealed before storage. A missing/wrong key fails closed. API flags must be booleans, not strings such as `"true"`.

The keyring can contain comma-separated Fernet keys, newest first. New writes use the first key; older keys can still decrypt existing data. **Do not remove an old key until every retained variable, linked/recording default and run snapshot using it has been re-encrypted or removed.** There is no automatic bulk historical key migration.

Stored ciphertext is not decrypted and rewritten on every action. A wrapper retains ciphertext between actions and decrypts during interpolation/redaction. The server, browser driver and target application necessarily handle plaintext temporarily; Python does not guarantee memory zeroization. This is **not** an HSM, RBAC/tenant vault, or a guarantee against penetration testing, a compromised host, an authorized operator, memory inspection, target-page JavaScript or network interception. Use HTTPS, `TF_ACCESS_TOKEN` or an authenticated gateway, trusted targets and least-privileged test accounts. With no access token, the application is unprotected. Operators with execution access can use the stored credentials.

**Existing literal credentials and historical Git commits are not retroactively sanitized.** Rotate previously committed credentials. Convert/re-save legacy variables/defaults, replace literal recording inputs with references, and review old exports, screenshots, reports and history. Marking one variable encrypted does not rewrite every historical copy. Do not commit the encryption key alongside the ciphertext.

## Evidence and PDF behavior

- Each executed action produces at most one viewport JPEG: quality 35, CSS scale, no full-page capture and no second image encoding pass. Logs, the optional single-run live view and PDF reports reuse that file. The live execution view advances by step; it is not a second continuous screencast.
- Known secret input fields and matching rendered text, plus password inputs, are masked across up to 10 frames. Evidence fails closed when the frame cap or masking fails. Unknown sensitive page content is not automatically classified: **review screenshots before sharing**.
- Screenshots are limited to 512 KiB each. Capture failures/limits appear as `evidence_error`; they do not rerun the action or silently manufacture evidence. Failed actions get one capture attempt too.
- A PDF is generated once from completed logs and existing images and cached. Single-run and aggregate batch PDFs are separate documents referencing the same original JPEGs. PDF creation necessarily reads/layouts the evidence; it does not replay, recapture, or re-encode those JPEGs.
- PDFs are capped at 2,000 steps and 64 MiB. The local PDF cache defaults to 256 MiB (`TF_REPORTS_MAX_LOCAL_BYTES`) and 500 files. When capacity is reached, new reports are refused instead of deleting unexpired reports. Execution logs remain available. An old/deleted/expired PDF cannot be downloaded after its expiry, even if its screenshots remain under the separate run-history retention policy.
- Browser crashes, cancellation before a step, disconnected frames or a storage limit can make evidence incomplete. Reports identify missing evidence; they cannot guarantee a screenshot when the browser cannot provide one.

Capturing every step costs more than failure-only capture. JPEG encoding, encryption, redaction, PDF layout, disk I/O and uploads have unavoidable cost. There are no extra browser workers, paid AI calls, scheduled GitHub Actions or added paid services, but **zero CPU increase, zero latency and zero hosting/provider charges cannot be guaranteed**. Check account quotas and workload limits.

## GitHub storage and 48-hour expiry

Binary PDFs are **release assets**, not committed files. Committing a PDF then deleting it would leave it in Git history and would not meet retention intent.

Operator configuration:

- `TF_GITHUB_REPO`: intended repository, preferably private for sensitive evidence.
- `TF_GITHUB_BRANCH`: intended library-summary branch; existing publication settings apply.
- `TF_GITHUB_TOKEN`: least-privileged token with repository contents/release write permission. Configure through the host's secret settings, not chat or Git.
- Enable the existing library publisher to commit summaries.
- `TF_REPORTS_GITHUB` governs **only** the release-asset upload of the PDF binary. The committed JSON summary and `library/reports/index.md` are written and pushed on every finished execution regardless, through the same publisher that commits the rest of the library — a default deployment therefore gets reports in the repository with no extra configuration. Set it to `1` **only after** reviewing repository visibility, permissions and quotas; the default is `0` to avoid unintended publication or charges.
- `TF_MAX_REPO_REPORTS` (default 200) bounds how many committed reports are retained; the oldest are pruned as new ones arrive, and `index.md` is regenerated each time.
- A batch publishes once, not once per recording: the per-recording summaries are written first and committed together with the batch summary in a single push.
- Keep `TF_QUEUE_SWEEP_SECONDS` positive (default 300) and run one application worker, as with the existing in-memory browser budget.

The application owns the prerelease `testforge-ephemeral-reports`. It uploads each cached PDF once, with a name containing the execution-completion timestamp. Only summary JSON and the attachment link are committed under `library/reports/`; variable values, step payloads and PDF bytes are not included there. Public repositories make their attachments public; private repository authorization is not an anonymous sharing service.

The existing maintenance loop performs report maintenance at most once per hour. It enumerates this dedicated release, deletes expired assets **oldest first**, expires summary links, and removes local PDFs whose completion time is more than 48 hours old. Remote enumeration makes cleanup independent of the local report index surviving a restart. Failed/pending uploads are retried hourly, reusing cached files and asset names to avoid duplicate uploads. Deleted/expired attachments are not recreated.

**Operational limitation:** hourly sweeps mean deletion at the first sweep after 48 hours (normally within the next hour), not exact-to-the-second expiry. Local access is denied at 48 hours. A stopped/sleeping instance, revoked token, disabled reporting, provider outage or blocked egress delays remote deletion until the application can resume maintenance. No background paid CI service was added to hide that limitation. Downloaded copies, caches and backups outside the application are not revocable.

Provider endpoints include `api.github.com` and `uploads.github.com`. The implementation sandbox permits the API host but not the upload host; live report upload/deletion was **not** validated here. Mocked provider tests are not proof of production authorization. Inspect:

- `GET /api/reports/run/<run-id>` or `/api/reports/batch/<batch-id>`: remote status, expiry, URL, summary publication status/error type.
- `GET /api/reports-maintenance`: last sweep and remote error types.
- `GET /api/runs/<run-id>/report.pdf[?download=true]`.
- `GET /api/runs/batch/<batch-id>/report.pdf[?download=true]`.

After deployment, use synthetic data to verify upload, private access, summary commits, a retention sweep, restart recovery and key persistence before storing real credentials/evidence. Merging source code does not configure secrets or verify a hosted rollout.

## Validation evidence

The complete sequential gate passed **204 checks**: 190 harness, 12 real Chromium
feature/plan/scenario checks, and 2 dashboard checks. Evidence:
[`logs/20261011-121418/results.md`](../logs/20261011-121418/results.md) and
[`quality-gate.log`](../logs/20261011-121418/quality-gate.log).

The live browser fixture verifies login→logout order across separate recording
projects, one context, encrypted one-off values, password masking in JPEG pixels,
one capture per action, single/aggregate PDF reuse, fail-stop sequencing, dataset
precedence and persistence. GitHub release publication/retention remains mocked.

A regression also verifies redaction before truncating long password-bearing errors.
