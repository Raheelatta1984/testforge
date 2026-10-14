# TestForge on SQL Server in Azure, end to end

This is the database half of the development and deployment story: how to put
TestForge on **SQL Server in Azure**, from an empty subscription to a verified
deployment. Read `docs/dev-deployment-end-to-end.md` for the application
runtime, and `docs/dev-azure-sql-scripts.sql.md` for the copy-paste SQL.

Items marked **[verify]** depend on a quota, a price or a vendor limit that
changes. Check them against your subscription before you rely on them.

## 0. What this service supports today

The data layer is SQLAlchemy (`app/db.py`) with the URL taken from
`DATABASE_URL`. Three dialects are exercised:

| Dialect | Status | Where it is used |
| --- | --- | --- |
| SQLite | Default, fully tested | Local development, the harness, single-user instances |
| PostgreSQL | Fully tested, `psycopg2-binary` is pinned in `requirements.txt` | The Azure/Render deployments in `docs/azure-devops-deployment-guide.md` |
| SQL Server | Works through the generic engine path; the driver is **not** in `requirements.txt` | This guide |

What that means in practice for SQL Server:

- `app/config.py` passes `DATABASE_URL` through unchanged (it only rewrites
  `postgres://` to `postgresql://`), so `mssql+pyodbc://…` is used as given.
- `app/db.py` builds the engine for any non-SQLite URL with
  `pool_pre_ping=True, pool_size=20, max_overflow=10, pool_recycle=3600`.
- `init_db()` creates missing tables and adds missing columns at boot on any
  dialect. Two legacy-repair helpers — relaxing NOT NULL on columns the models no
  longer declare, and widening a legacy `VARCHAR` — are gated to
  `dialect.name == "postgresql"` and are **skipped on SQL Server**. On a fresh
  database that changes nothing; on a database created by a much older revision
  you may have to widen a column by hand (§6.3).
- There is no migration tool (no Alembic) in this repository, on any dialect.
  That is deliberate: the schema is created and repaired at boot.

Do not put the ODBC driver in `requirements.txt`. It needs a system package and
it would break the existing image build; `requirements-sqlserver.txt` exists for
SQL Server deployments only.

## 1. Choose the SQL Server option

| Option | Choose it when | Watch out for |
| --- | --- | --- |
| **Azure SQL Database** (single database) | You want the managed default: patching, backups and failover are Azure's job | No cross-database queries, no SQL Agent, no `msdb` jobs; connection limits per tier **[verify]** |
| **Azure SQL Managed Instance** | You need SQL Agent jobs, cross-database queries or near-100% SQL Server compatibility | Higher minimum cost **[verify]**, longer provisioning |
| **SQL Server on an Azure VM** | You must keep an existing on-premises habit: SQL Agent, linked servers, file shares, a specific edition | You own patching, backups, HA and the OS |

TestForge needs very little from a database: one schema, modest write volume,
JSON stored as text. **Azure SQL Database is the recommended default.** Choose
the VM only when a requirement in the right-hand column is real.

## 2. Prerequisites

```bash
az login
az account set --subscription "<SUBSCRIPTION ID OR NAME>"
az provider register --namespace Microsoft.Sql        # once per subscription
az provider register --namespace Microsoft.KeyVault   # once per subscription
```

Decide these before you start, and write them down:

| Decision | Example |
| --- | --- |
| Resource group | `rg-testforge-dev` |
| Region | `australiaeast` (match the application region) |
| Environment | `dev`, `test`, `prod` — one database per environment, never shared |
| Server name | `sql-testforge-dev` (globally unique) |
| Database name | `testforge` |
| Tier | `S1` for dev, `P1` or vCore `GP_S_Gen5_2` for prod **[verify]** |

**One database per environment.** `app/db.py` repairs the schema at boot; two
environments pointed at one database will fight over columns.

## 3. Provision Azure SQL Database

### 3.1 Resource group, logical server, database

```bash
RG=rg-testforge-dev
REGION=australiaeast
SERVER=sql-testforge-dev
DB=testforge

az group create --name "$RG" --location "$REGION"

az sql server create \
  --resource-group "$RG" --name "$SERVER" --location "$REGION" \
  --admin-user tfadmin --admin-password "$(openssl rand -base64 24)"

az sql db create \
  --resource-group "$RG" --server "$SERVER" --name "$DB" \
  --edition Standard --service-objective S1 \
  --zone-redundant false
```

Put the admin password in Key Vault immediately and stop using the literal:

```bash
az keyvault create --resource-group "$RG" --name "kv-testforge-dev" --location "$REGION"
az keyvault secret set --vault-name "kv-testforge-dev" \
  --name "tf-sql-admin" --value "<THE ADMIN PASSWORD>"
```

### 3.2 Network: firewall rule or private endpoint

Quick path for a single deployment host (development only):

```bash
az sql server firewall-rule create \
  --resource-group "$RG" --server "$SERVER" \
  --name allow-app-host --start-ip-address <APP PUBLIC IP> --end-ip-address <APP PUBLIC IP>
```

Production path — no public IP on the database at all:

```bash
az sql server update --resource-group "$RG" --name "$SERVER" \
  --enable-public-network false

az network private-endpoint create \
  --resource-group "$RG" --name "pe-testforge-sql" \
  --vnet-name vnet-testforge --subnet snet-private-endpoints \
  --private-connection-resource-id \
    "$(az sql server show --resource-group "$RG" --name "$SERVER" --query id -o tsv)" \
  --group-ids sqlServer \
  --connection-name testforge-sql
```

Then create the private DNS zone link so `sql-testforge-dev.database.windows.net`
resolves inside the VNet. The application host must be in the same VNet or
peered to it.

### 3.3 Application login, least privilege

Do **not** give the application the server admin login. Create a login and a
user with rights on the one schema it owns. Run this in `sqlcmd` or Azure Data
Studio against the `testforge` database — the full script, with comments, is in
`docs/dev-azure-sql-scripts.sql.md`:

```sql
CREATE LOGIN testforge_app WITH PASSWORD = '<STRONG PASSWORD FROM KEY VAULT>';
CREATE USER testforge_app FOR LOGIN testforge_app;
ALTER ROLE db_datareader ADD MEMBER testforge_app;
ALTER ROLE db_datawriter ADD MEMBER testforge_app;
ALTER ROLE db_ddladmin  ADD MEMBER testforge_app;   -- needed: init_db() creates tables at boot
```

`db_ddladmin` is genuinely required: `init_db()` runs `CREATE TABLE` and
`ALTER TABLE … ADD COLUMN` on first boot and whenever a column is added to a
model. If your security team will not allow DDL at runtime, create the schema
yourself from §6.2 and grant only `db_datareader` + `db_datawriter` — the boot
check then finds nothing to do.

### 3.4 Microsoft Entra authentication (optional)

`Authentication=ActiveDirectoryServicePrincipal` works with ODBC Driver 18 and a
recent pyodbc **[verify]**. It removes the SQL password from the connection
string, which is worth having, but it adds an Entra configuration dependency.
SQL authentication with the password in Key Vault is the path this guide
verifies.

## 4. SQL Server on an Azure VM instead

Only if §1 sent you here.

```bash
az vm create \
  --resource-group "$RG" --name vm-testforge-sql \
  --image MicrosoftSQLServer:SQL2022-WS2022:sqldev-win2022:latest \
  --size Standard_D4s_v3 --admin-username azureuser \
  --admin-password "$(openssl rand -base64 24)" \
  --public-ip-address "" --subnet snet-data --vnet-name vnet-testforge

az vm open-port --resource-group "$RG" --name vm-testforge-sql \
  --port 1433 --priority 1010
```

Then, on the VM:

1. Enable **TCP/IP** in SQL Server Configuration Manager and set the port to
   1433; restart the SQL Server service.
2. Set **Mixed Mode** authentication and create the `testforge_app` login with
   the same grants as §3.3.
3. Restrict the Windows firewall and the NSG to the application subnet only.
   Port 1433 must never be reachable from the internet.
4. Configure backups yourself: SQL Server Agent jobs plus an off-box copy (Azure
   Blob storage). Managed backups are not automatic here.
5. Patch the OS and SQL Server yourself; that is the trade you made in §1.

The application side is identical: only the host in the connection string
changes.

## 5. Driver and connection string

### 5.1 Install the driver

Debian/Ubuntu (the image base is `python:3.11-slim-bookworm`):

```bash
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | \
  gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] \
  https://packages.microsoft.com/debian/12/prod bookworm main" \
  > /etc/apt/sources.list.d/mssql-release.list
apt-get update
ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
```

Windows hosts: install **ODBC Driver 18 for SQL Server** from Microsoft, then
`pip install pyodbc`.

Python side — `requirements-sqlserver.txt` in this repository:

```bash
pip install -r requirements.txt -r requirements-sqlserver.txt
```

In a container, add the two `apt-get` lines and that `pip install` to your own
Dockerfile layer. Do not edit `requirements.txt`: it is what every other
deployment installs.

### 5.2 The connection string

```
mssql+pyodbc://testforge_app:<PASSWORD>@sql-testforge-dev.database.windows.net:1433/testforge?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
```

Rules:

- The driver name has spaces encoded as `+`: `ODBC+Driver+18+for+SQL+Server`.
- `Encrypt=yes` is the default in ODBC Driver 18 — set it explicitly anyway.
- `TrustServerCertificate=no` unless you are connecting to a VM with a
  self-signed certificate, and then only inside a private network.
- URL-encode a password that contains `@`, `/`, `:` or `%`.
- Store it in Key Vault or the host's secret store, never in `docker-compose.yml`
  or in Git. `docker-compose.yml` in this repository already interpolates
  `${VAR}` from the environment.

Set it on the host:

```bash
export DATABASE_URL="mssql+pyodbc://testforge_app:$(az keyvault secret show \
  --vault-name kv-testforge-dev --name tf-sql-app --query value -o tsv)\
@sql-testforge-dev.database.windows.net:1433/testforge\
?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"
```

### 5.3 Pool arithmetic

`app/db.py` opens `pool_size=20` with `max_overflow=10`, so **one process can
hold up to 30 connections**, with `pool_pre_ping=True` and
`pool_recycle=3600`.

- This service runs **one process** by design. Two workers double the pool to 60
  and break the single-browser constraint documented in
  `docs/dev-team-architecture.md` §4. Do not scale out.
- Azure SQL Database caps concurrent connections by tier: Basic/S0/S1 allow
  hundreds, but a burst above your tier's limit surfaces as
  `Timeout expired` or error 40501 **[verify]**. If you see it, lower
  `pool_size` before you buy a bigger tier.
- `pool_recycle=3600` matters: Azure SQL Database terminates idle connections
  (about 30 minutes), and `pool_pre_ping` is what turns that into a transparent
  reconnect instead of a failed request. Do not remove either setting.

## 6. Schema: what happens on first boot

### 6.1 The boot sequence

`init_db()` runs when `app.main` is imported:

1. `Base.metadata.create_all()` creates any missing table.
2. For every model, `_add_missing_columns()` adds columns the model declares but
   the table does not have, and backfills them so existing rows satisfy NOT NULL.
3. Two PostgreSQL-only repairs are skipped on SQL Server (§0).
4. The result is stored in `SCHEMA_STATUS` and served by `/api/diagnostics`.

The service never fails to start because of a database problem: it reports
`DATABASE FATAL ERROR` and `/api/health` returns 503 until it is fixed.

### 6.2 Expected schema

The authoritative definition is the models in `app/db.py`. The tables are:
`projects`, `variables`, `recordings`, `recording_steps`, `runs`, `batches`,
`app_settings`, `managed_keys`.

On SQL Server they compile to something like this (abridged — the full script is
in `docs/dev-azure-sql-scripts.sql.md`):

```sql
CREATE TABLE projects (
    id VARCHAR(36) NOT NULL,
    name VARCHAR(200) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    industry_type VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
```

Notes on the mapping:

- SQLAlchemy `JSON` columns (`execution_plan`, `selector`, `execution_log`, …)
  become `NVARCHAR(max)` holding serialized JSON. There are no JSON indexes; do
  not query inside them.
- `Text` becomes `TEXT` and `String(n)` becomes `VARCHAR(n)`. Unicode names and
  labels are stored in `NVARCHAR` only where SQLAlchemy chooses it, so if your
  team needs full Unicode in `VARCHAR` columns, say so in review before the first
  real data arrives.
- Boolean becomes `BIT`.

### 6.3 What to check by hand

```sql
SELECT name FROM sys.tables ORDER BY name;                  -- 8 tables expected
SELECT t.name AS table_name, COUNT(c.column_id) AS columns
FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id
GROUP BY t.name ORDER BY t.name;
SELECT DB_NAME() AS db, @@VERSION AS version;
```

Then open `/api/diagnostics` in the dashboard and confirm `dialect` is `mssql`,
`ready` is `true`, every table is `created` or `ok`, and `repairs` is empty or
lists only additions you expected.

If a legacy column is too narrow you will see a truncation error on insert. Widen
it by hand and re-run the boot check:

```sql
ALTER TABLE recording_steps ALTER COLUMN label VARCHAR(500) NULL;
```

## 7. Migrating from SQLite or PostgreSQL

1. Take a maintenance window. One worker, one database: there is no zero-downtime
   path.
2. Create the schema on the target by starting the service once against it
   (`init_db()`), or run the script from §6.2.
3. Copy rows in dependency order: `projects`, `variables`, `recordings`,
   `recording_steps`, `runs`, `batches`, `app_settings`, `managed_keys`.
4. Move `TF_LIBRARY_DIR` (default `library/`) as well. Recordings and their
   screenshots live on disk, and the library is the source of truth for
   publishing. A database copy alone is not a migration.
5. Verify parity with counts and a checksum of a few rows (script in
   `docs/dev-azure-sql-scripts.sql.md`).
6. Keep the source database read-only for one release before you delete it.

Useful tools: Azure Data Factory, `bcp`, or `sqlcmd` with generated `INSERT`
statements. Whichever you choose, compare counts before and after, and compare a
JSON column's length on a sample of rows — that is where a silent truncation
shows up first.

## 8. Operations

| Concern | What to do |
| --- | --- |
| Backups | Azure SQL Database: point-in-time restore is automatic; set a retention that satisfies your policy **[verify]**. On a VM: your own Agent jobs plus an off-box copy |
| Monitoring | Azure Monitor metrics for DTU/vCore, connections and deadlocks; the app's `/api/diagnostics` for schema state |
| Slow queries | `sys.dm_exec_query_stats` — the heaviest write path is `recording_steps` during a recording |
| Growth | `recording_steps` and `runs` grow with use; `TF_RUN_RETENTION_DAYS` governs run purging |
| Cost | Check the tier's DTU consumption before assuming it is oversized **[verify]** |

## 9. Hand-over checklist

- [ ] One database per environment; no environment shares one.
- [ ] The application login is not the server admin, and its password is in Key Vault.
- [ ] No public endpoint on the database in production (private endpoint or VNet rule).
- [ ] `Encrypt=yes` and `TrustServerCertificate=no` in the connection string.
- [ ] `requirements-sqlserver.txt` installed in the runtime image, `requirements.txt` untouched.
- [ ] `/api/health` returns 200 and `/api/diagnostics` shows `dialect: mssql`, `ready: true`.
- [ ] A recording can be created, replayed and exported end to end on the new database.
- [ ] Backups tested by restoring one, not by reading a policy.
