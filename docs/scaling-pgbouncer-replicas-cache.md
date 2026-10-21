# Scaling the database path: PgBouncer, read replicas, Memcached

The hosted deployment uses **Neon Postgres** (`DATABASE_URL`). Three layers sit in front of / beside it:

```
                    ┌──────────────┐
   writes ─────────►│ PgBouncer    │────► Neon primary
                    │ :6439        │
   reads ──► cache? ┴──────────────┘
     │ miss        ┌──────────────┐
     └────────────►│ replicas     │────► Neon read replica(s)
                   │ round-robin  │
              ┌────┴──────────────┴────┐
              │ Memcached (shared)    │
              │ or in-memory fallback │
              └───────────────────────┘
```

## 1. PgBouncer (connection pooling)

Neon caps direct connections; without a pooler each worker holds its own. Run PgBouncer in **transaction** mode (see `deploy/pgbouncer/`) or use Neon's `-pooler` endpoint, then:

```bash
export TF_PGBOUNCER_URL='postgresql://USER:PASS@pooler-host:6439/dbname'
export TF_PGBOUNCER=1
```

The app detects the pooler (flag, `:6439` URL, or `TF_PGBOUNCER_URL`) and switches SQLAlchemy to `NullPool` with pre-ping and a statement timeout (`TF_STATEMENT_TIMEOUT_MS`, default 30000). Detection lives in `app/db.py:is_pgbouncer_url()`; pooler config in `deploy/pgbouncer/pgbouncer.ini`.

Rules: short transactions only; no prepared statements, `LISTEN`/`NOTIFY`, advisory locks, temp tables, or session `SET`.

## 2. Read replicas (too many reads)

```bash
export TF_READ_REPLICAS='postgresql://USER:PASS@replica-1/db,postgresql://USER:PASS@replica-2/db'
export TF_REPLICA_BALANCE=round_robin   # or: random
export TF_REPLICA_PROBE=1               # skip dead replicas (SELECT 1 probe)
```

Use in code (`app/db_replicas.py`):

```python
from app.db_replicas import session_scope
with session_scope(read_only=True) as db:   # replica, balanced; primary fallback
    rows = db.query(Run).filter(...).all()
with session_scope() as db:                 # primary, commits on exit
    db.add(Run(...))
```

`app.db.SessionLocal` still targets the primary, so existing read-modify-write code is untouched. Migrate pure reads (`GET` handlers, polls, reports) to `session_scope(read_only=True)`. Dead replicas are skipped with a warning; if all are down, reads fall back to the primary rather than 500.

## 3. Memcached (hot-read cache)

```bash
export TF_MEMCACHED_SERVERS='127.0.0.1:11211'   # comma-separated for many
export TF_MEMCACHED_TTL=60
export TF_MEMCACHED_PREFIX='tf:'
```

`app/cache.py` shares entries across processes via `pymemcache`; with no servers configured (or if the server is down) it fails open to a bounded in-memory TTL map, so caching helps even on a single instance with zero setup.

```python
from app.cache import get_or_load, make_key, delete
key = make_key("projects", project_id, "runs")
runs = get_or_load(key, lambda: db.query(Run).all(), ttl=30)
# ... after a write that changes the listing:
delete(key)
```

or memoise a pure function:

```python
from app.cache import cached
@cached(ttl=60, namespace="catalog")
def project_tree(project_id): ...
```

Guidance: cache reference data (projects, recordings, variable maps) with short TTLs (15–120s); invalidate the exact key on write; use versioned key parts (`make_key("v3", ...)`) when a whole namespace must turn over, because Memcached has no prefix scan (`delete_prefix` only clears the in-memory tier exactly).

## 4. Observe it

`GET /api/scaling` reports pool implementation, PgBouncer detection, replica build/probe state, cache backend/entries, image shards, and ID generator config. Nothing in the report carries credentials.

## Local development

`docker compose up -d postgres pgbouncer memcached` starts throwaway Postgres + PgBouncer + Memcached; the `testforge` service is pre-wired with example URLs (override with your own env).
