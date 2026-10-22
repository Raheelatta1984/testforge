# SQL scripts for TestForge on Azure SQL

Copy-paste T-SQL for the setup, verification and migration checks described in
`docs/dev-azure-sql-server.md`. Run these with `sqlcmd`, Azure Data Studio or the
Azure portal query editor, connected to the **target database** (`testforge`),
not to `master`, unless a script says otherwise.

Two rules before you run anything here:

1. Replace every `<PLACEHOLDER>`. Nothing in this file should be run verbatim
   against production without a read.
2. The application creates its own schema at boot (`init_db()` in `app/db.py`).
   You only need §2 if your security team forbids runtime DDL. If you use §2,
   the application login can be granted `db_datareader` and `db_datawriter` only.

## 1. Login, user and least-privilege grants

```sql
-- Run against the target database.
-- The password comes from Key Vault; never paste a real one into a ticket.
CREATE LOGIN testforge_app WITH PASSWORD = '<STRONG PASSWORD>', CHECK_POLICY = ON;
GO

CREATE USER testforge_app FOR LOGIN testforge_app;
GO

ALTER ROLE db_datareader ADD MEMBER testforge_app;
ALTER ROLE db_datawriter ADD MEMBER testforge_app;
GO

-- Required only if the application is allowed to create its own schema at boot.
ALTER ROLE db_ddladmin ADD MEMBER testforge_app;
GO
```

If the login already exists on the server, skip `CREATE LOGIN` and run only the
`CREATE USER` and role statements in this database.

Audit what you just granted:

```sql
SELECT dp.name AS principal, dp.type_desc, r.name AS role
FROM sys.database_role_members m
JOIN sys.database_principals r ON r.principal_id = m.role_principal_id
JOIN sys.database_principals dp ON dp.principal_id = m.member_principal_id
WHERE dp.name = 'testforge_app'
ORDER BY r.name;
```

## 2. Schema (only if runtime DDL is forbidden)

This is what `Base.metadata.create_all()` produces on SQL Server, in dependency
order. Keep it in sync with the models in `app/db.py` — if a model changes and
this script does not, the boot check will try to add the column at runtime and
fail without `db_ddladmin`.

```sql
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE TABLE projects (
    id VARCHAR(36) NOT NULL,
    name VARCHAR(200) NOT NULL,
    base_url VARCHAR(500) NOT NULL,
    industry_type VARCHAR(100) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
GO

CREATE TABLE variables (
    id VARCHAR(36) NOT NULL,
    project_id VARCHAR(36) NOT NULL,
    name VARCHAR(100) NOT NULL,
    value TEXT NOT NULL,
    category VARCHAR(50) NOT NULL,
    is_secret BIT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (project_id) REFERENCES projects (id)
);
GO

CREATE TABLE recordings (
    id VARCHAR(36) NOT NULL,
    project_id VARCHAR(36) NOT NULL,
    parent_id VARCHAR(36) NULL,
    name VARCHAR(255) NOT NULL,
    purpose TEXT NOT NULL,
    start_url VARCHAR(1000) NOT NULL,
    execution_plan NVARCHAR(max) NULL,
    input_defaults NVARCHAR(max) NULL,
    tags VARCHAR(500) NOT NULL,
    status VARCHAR(50) NOT NULL,
    video_path VARCHAR(1000) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    FOREIGN KEY (project_id) REFERENCES projects (id),
    FOREIGN KEY (parent_id) REFERENCES recordings (id)
);
GO

CREATE TABLE recording_steps (
    id VARCHAR(36) NOT NULL,
    recording_id VARCHAR(36) NOT NULL,
    [order] INTEGER NOT NULL,
    action VARCHAR(100) NOT NULL,
    selector NVARCHAR(max) NULL,
    value TEXT NULL,
    label VARCHAR(500) NULL,
    screenshot_path VARCHAR(1000) NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    duration_ms FLOAT NULL,
    repeat_count INTEGER NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (recording_id) REFERENCES recordings (id)
);
GO

CREATE TABLE runs (
    id VARCHAR(36) NOT NULL,
    recording_id VARCHAR(36) NOT NULL,
    variable_snapshot NVARCHAR(max) NULL,
    input_overrides NVARCHAR(max) NULL,
    snapshot_steps NVARCHAR(max) NULL,
    sequence INTEGER NULL,
    status VARCHAR(50) NOT NULL,
    progress_pct INTEGER NOT NULL,
    execution_log NVARCHAR(max) NOT NULL,
    video_path VARCHAR(1000) NULL,
    trace_path VARCHAR(1000) NULL,
    rog_monitor_log TEXT NULL,
    rog_devops_log TEXT NULL,
    rog_qa_log TEXT NULL,
    batch_id VARCHAR(36) NULL,
    started_at DATETIME NULL,
    cancel_reason VARCHAR(500) NULL,
    execution_speed FLOAT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at DATETIME NULL,
    PRIMARY KEY (id),
    FOREIGN KEY (recording_id) REFERENCES recordings (id)
);
GO

CREATE TABLE batches (
    id VARCHAR(36) NOT NULL,
    name VARCHAR(200) NULL,
    status VARCHAR(50) NOT NULL,
    total INTEGER NOT NULL,
    done INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    progress_pct INTEGER NOT NULL,
    options NVARCHAR(max) NOT NULL,
    report NVARCHAR(max) NOT NULL,
    error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    PRIMARY KEY (id)
);
GO

CREATE TABLE app_settings (
    [key] VARCHAR(60) NOT NULL,
    value NVARCHAR(max) NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY ([key])
);
GO

CREATE TABLE managed_keys (
    id VARCHAR(36) NOT NULL,
    purpose VARCHAR(50) NOT NULL,
    sequence INTEGER NOT NULL,
    material TEXT NOT NULL,
    note VARCHAR(200) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
);
GO
```

`managed_keys.material` holds an application-generated encryption key when
`TF_VARIABLE_ENCRYPTION_KEYS` is not supplied. Treat the table as sensitive: back
it up, but never paste its contents into a chat, a ticket or a log.

## 3. Post-boot verification

Run these after the first start and compare with `/api/diagnostics` in the
dashboard.

```sql
-- 3.1 All eight tables exist.
SELECT name FROM sys.tables ORDER BY name;

-- 3.2 Column counts per table (compare with the models in app/db.py).
SELECT t.name AS table_name, COUNT(c.column_id) AS columns
FROM sys.tables t
JOIN sys.columns c ON c.object_id = t.object_id
GROUP BY t.name
ORDER BY t.name;

-- 3.3 Server, database and compatibility level.
SELECT DB_NAME() AS db, DATABASEPROPERTYEX(DB_NAME(), 'CompatibilityLevel') AS compat,
       SERVERPROPERTY('Edition') AS edition, @@VERSION AS version;

-- 3.4 Collation, because step labels and values hold user text.
SELECT DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS collation;

-- 3.5 What the application login can actually do here.
SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'CREATE TABLE') AS can_create_table,
       IS_ROLEMEMBER('db_datareader') AS reader,
       IS_ROLEMEMBER('db_datawriter') AS writer,
       IS_ROLEMEMBER('db_ddladmin')  AS ddl;
```

## 4. Health checks while the service runs

```sql
-- 4.1 Current connections from the application.
SELECT session_id, login_name, host_name, program_name, status, cpu_time
FROM sys.dm_exec_sessions
WHERE login_name = 'testforge_app'
ORDER BY session_id;

-- 4.2 Longest-running statements (run during a recording or a run).
SELECT TOP 10
       qs.execution_count,
       qs.total_elapsed_time / 1000 AS total_ms,
       qs.total_elapsed_time / NULLIF(qs.execution_count, 0) / 1000 AS avg_ms,
       SUBSTRING(st.text, 1, 200) AS statement
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_elapsed_time DESC;

-- 4.3 Blocked or deadlocked sessions.
SELECT session_id, blocking_session_id, wait_type, wait_time, command
FROM sys.dm_exec_requests
WHERE blocking_session_id <> 0;
```

The app holds at most 30 connections per process (`pool_size=20`,
`max_overflow=10`). If §4.1 shows more than that from one host, something is
opening engines outside `app/db.py` — that is a bug, not a tuning problem.

## 5. Migration parity checks

Run each query on the **source** and on the **target** and compare the two
outputs. Counts alone miss truncation, so compare the checksums too.

```sql
-- 5.1 Row counts.
SELECT 'projects' AS table_name, COUNT(*) AS rows_total FROM projects
UNION ALL SELECT 'variables', COUNT(*) FROM variables
UNION ALL SELECT 'recordings', COUNT(*) FROM recordings
UNION ALL SELECT 'recording_steps', COUNT(*) FROM recording_steps
UNION ALL SELECT 'runs', COUNT(*) FROM runs
UNION ALL SELECT 'batches', COUNT(*) FROM batches
UNION ALL SELECT 'app_settings', COUNT(*) FROM app_settings
UNION ALL SELECT 'managed_keys', COUNT(*) FROM managed_keys
ORDER BY table_name;

-- 5.2 Checksums: catches a changed or truncated value that a count cannot.
SELECT 'projects' AS table_name,
       ISNULL(SUM(CAST(CHECKSUM(id, name, base_url, industry_type) AS BIGINT)), 0) AS checksum
FROM projects
UNION ALL
SELECT 'variables',
       ISNULL(SUM(CAST(CHECKSUM(id, project_id, name, category, is_secret) AS BIGINT)), 0)
FROM variables
UNION ALL
SELECT 'recordings',
       ISNULL(SUM(CAST(CHECKSUM(id, project_id, name, status, tags) AS BIGINT)), 0)
FROM recordings
UNION ALL
SELECT 'recording_steps',
       ISNULL(SUM(CAST(CHECKSUM(id, recording_id, [order], action, repeat_count) AS BIGINT)), 0)
FROM recording_steps
UNION ALL
SELECT 'runs',
       ISNULL(SUM(CAST(CHECKSUM(id, recording_id, status, progress_pct) AS BIGINT)), 0)
FROM runs
ORDER BY table_name;

-- 5.3 JSON columns: length parity proves nothing was cut on the way over.
SELECT TOP 20 r.id,
       LEN(r.execution_plan) AS plan_len,
       (SELECT COUNT(*) FROM recording_steps s WHERE s.recording_id = r.id) AS steps
FROM recordings r
WHERE r.execution_plan IS NOT NULL
ORDER BY r.created_at DESC;

-- 5.4 Orphans: a step whose recording did not come across.
SELECT COUNT(*) AS orphan_steps
FROM recording_steps s
LEFT JOIN recordings r ON r.id = s.recording_id
WHERE r.id IS NULL;
```

Query 5.4 must return `0`. If it does not, stop and re-copy; foreign keys are
declared, so a non-zero count means the constraint was not created.

## 6. Fixing a legacy column

The PostgreSQL-only widening repair does not run on SQL Server, so a database
created by an older revision can keep a narrow column. The symptom is a
truncation error on insert.

```sql
-- Example: recording_steps.label is VARCHAR(300) but the model declares 500.
SELECT c.name, t.name AS type_name, c.max_length
FROM sys.columns c
JOIN sys.types t ON t.user_type_id = c.user_type_id
WHERE c.object_id = OBJECT_ID('recording_steps') AND c.name = 'label';

ALTER TABLE recording_steps ALTER COLUMN label VARCHAR(500) NULL;
```

Then restart the service and confirm `/api/diagnostics` reports no new error.

## 7. Maintenance

```sql
-- 7.1 Fragmentation on the two tables that grow.
SELECT OBJECT_NAME(ips.object_id) AS table_name, i.name AS index_name,
       ips.avg_fragmentation_in_percent, ips.page_count
FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, 'LIMITED') ips
JOIN sys.indexes i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
WHERE ips.page_count > 100
ORDER BY ips.avg_fragmentation_in_percent DESC;

-- 7.2 Statistics freshness.
SELECT OBJECT_NAME(object_id) AS table_name, name AS stats_name,
       STATS_DATE(object_id, stats_id) AS updated
FROM sys.stats
WHERE OBJECTPROPERTY(object_id, 'IsUserTable') = 1
ORDER BY updated;

-- 7.3 Space used by the biggest tables.
SELECT t.name AS table_name,
       SUM(p.rows) AS rows_total,
       SUM(a.total_pages) * 8 / 1024 AS total_mb
FROM sys.tables t
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
JOIN sys.allocation_units a ON a.container_id = p.partition_id
GROUP BY t.name
ORDER BY total_mb DESC;
```

Rebuild or reorganize only when §7.1 shows fragmentation above about 30%, and
update statistics after a bulk copy. On Azure SQL Database, index maintenance
consumes DTUs — schedule it outside working hours **[verify]**.

## 8. Rollback

There is no destructive rollback for data: restore a point-in-time copy of the
database and repoint `DATABASE_URL` at it. Use this only to remove an empty
schema you created with §2, in reverse dependency order:

```sql
DROP TABLE IF EXISTS managed_keys;
DROP TABLE IF EXISTS app_settings;
DROP TABLE IF EXISTS batches;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS recording_steps;
DROP TABLE IF EXISTS recordings;
DROP TABLE IF EXISTS variables;
DROP TABLE IF EXISTS projects;
GO
```

Removing the login:

```sql
DROP USER IF EXISTS testforge_app;
GO
-- On the server, in the same session that created it:
-- DROP LOGIN testforge_app;
```
