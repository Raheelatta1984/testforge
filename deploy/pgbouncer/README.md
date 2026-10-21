# PgBouncer in front of Neon Postgres

## Which pooler?

| Setup | How |
|---|---|
| **Neon pooled endpoint (recommended)** | Use the `-pooler....neon.tech` hostname Neon already gives you as `TF_PGBOUNCER_URL`, set `TF_PGBOUNCER=1`. Nothing to host. |
| **Self-hosted PgBouncer** | Run the `pgbouncer` service from `docker-compose.yml` with this directory mounted, or copy `pgbouncer.ini` to your own host. |

## Self-hosted setup

1. Copy `userlist.txt.example` to `userlist.txt` with real md5 hashes.
2. Edit the `[databases]` section of `pgbouncer.ini` with your Neon host/db/user.
3. Start it:
   ```bash
   docker compose up -d pgbouncer
   ```
4. Point the app at it:
   ```bash
   export TF_PGBOUNCER_URL='postgresql://neondb:SECRET@127.0.0.1:6439/testforge'
   export TF_PGBOUNCER=1
   ```

## What the app does differently behind a pooler

* SQLAlchemy uses `NullPool` (the pooler owns pooling; a client-side pool would only pin server connections).
* Every checkout is pre-pinged and carries a statement timeout (`TF_STATEMENT_TIMEOUT_MS`, default 30s).
* Verify at runtime: `GET /api/scaling` shows `"pgbouncer": true` and pool implementation `NullPool`.

## Rules for transaction pooling

* Keep transactions short; never hold a session open across user input or sleeps.
* No prepared statements, `LISTEN`/`NOTIFY`, advisory locks, temporary tables, or `SET` that must survive the transaction.
* Writes go to the primary pool; reads additionally fan out to `TF_READ_REPLICAS` (see `docs/scaling-pgbouncer-replicas-cache.md`).
