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

Install the pinned requirements and provision a stable Fernet key **outside Git**:

```sh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Run that command in your own protected administration environment, then store the result in the hosting platform's secret configuration as `TF_VARIABLE_ENCRYPTION_KEYS`. Do not paste it into a ticket/chat, commit it, bake it into the image, or generate a new key on every restart. Back it up securely: losing it loses access to the encrypted values. Docker Compose forwards this environment variable; Render declares it as an operator-provisioned secret.

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
- Set `TF_REPORTS_GITHUB=1` **only after** reviewing repository visibility, permissions and quotas. The deployment default is `0` to avoid unintended publication or charges.
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
