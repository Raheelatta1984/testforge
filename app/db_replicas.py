"""Read/write splitting: writes go to Neon primary, reads fan out to replicas.

The dashboard is read-heavy (project trees, run lists, report polls), so a
single primary spends most of its connections serving ``SELECT``. Point
``TF_READ_REPLICAS`` at one or more Neon read-replica URLs and all read-only
sessions are balanced across them while writes keep going to ``DATABASE_URL``.

Environment:
    TF_READ_REPLICAS    comma-separated replica DATABASE_URLs (default: none =
                        reads use the primary, exactly as before)
    TF_REPLICA_BALANCE  "round_robin" (default) or "random"
    TF_REPLICA_PROBE    1 = SELECT 1 each replica at first use and skip dead
                        ones (default 1)

Usage::
    from app.db_replicas import read_session, write_session, session_scope

    with session_scope(read_only=True) as db:      # replica (or primary fallback)
        projects = db.query(Project).all()
    with session_scope(read_only=False) as db:     # primary, commits
        db.add(Run(...)); db.commit()

Existing code that uses ``app.db.SessionLocal`` keeps working unchanged: it
always targets the primary, which is the safe default for read-modify-write
flows. Migrate pure reads to ``read_session()``/``session_scope(True)`` plus
``app.cache`` for the hot paths.
"""

from __future__ import annotations

import itertools
import logging
import os
import random
import threading
from contextlib import contextmanager

from sqlalchemy import text

logger = logging.getLogger("testforge.db_replicas")


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def replica_urls() -> list[str]:
    """Configured replica URLs (``TF_READ_REPLICAS``)."""
    return _env_list("TF_READ_REPLICAS")


def balance_mode() -> str:
    mode = os.environ.get("TF_REPLICA_BALANCE", "round_robin").strip().lower()
    return mode if mode in {"round_robin", "random"} else "round_robin"


def probe_enabled() -> bool:
    return os.environ.get("TF_REPLICA_PROBE", "1").strip().lower() not in {
        "0", "false", "no", "off"}


_lock = threading.Lock()
_replica_engines: list = []
_replica_alive: list[bool] = []
_round_robin: itertools.cycle | None = None


def _build_replica_engines():
    global _replica_engines, _replica_alive, _round_robin
    with _lock:
        if _replica_engines or _round_robin is not None:
            return _replica_engines
        from app.db import create_postgres_engine  # local import: no cycle
        urls = replica_urls()
        for url in urls:
            try:
                engine = create_postgres_engine(url, replica=True)
                _replica_engines.append(engine)
                _replica_alive.append(True)
            except Exception as exc:
                logger.warning("replica engine for %s not built: %s",
                               url.split("@")[-1], exc)
        _round_robin = itertools.cycle(range(len(_replica_engines))) \
            if _replica_engines else None
        return _replica_engines


def _primary_engine():
    from app import db as db_module
    return db_module.engine


def _probe(engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("replica probe failed, skipping replica: %s", exc)
        return False


def get_read_engine():
    """A replica engine (balanced), or the primary when no replica is usable."""
    engines = _build_replica_engines()
    if not engines:
        return _primary_engine()
    order: list[int]
    if balance_mode() == "random":
        order = random.sample(range(len(engines)), len(engines))
    else:
        assert _round_robin is not None
        with _lock:
            start = next(_round_robin)
        order = [(start + i) % len(engines) for i in range(len(engines))]
    for index in order:
        with _lock:
            alive = _replica_alive[index]
        engine = engines[index]
        if alive and probe_enabled():
            alive = _probe(engine)
            with _lock:
                _replica_alive[index] = alive
        if alive or not probe_enabled():
            return engine
    logger.warning("all %d replicas unreachable; reading from primary",
                   len(engines))
    return _primary_engine()


def get_write_engine():
    """The primary engine. Writes never go to a replica."""
    return _primary_engine()


def _sessionmaker_for(engine):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def read_session():
    """New ORM session bound to a replica (primary fallback). Read-only by
    convention: never ``commit()`` writes on it."""
    return _sessionmaker_for(get_read_engine())()


def write_session():
    """New ORM session bound to the primary."""
    return _sessionmaker_for(get_write_engine())()


@contextmanager
def session_scope(read_only: bool = False):
    """``with session_scope(read_only=True) as db:`` ... read from replica.

    Write scopes commit on clean exit and roll back on exception; read scopes
    always roll back (returning the connection clean to the pool)."""
    session = read_session() if read_only else write_session()
    try:
        yield session
        if read_only:
            session.rollback()
        else:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset() -> None:
    """Drop cached replica engines (tests, diagnostics)."""
    global _replica_engines, _replica_alive, _round_robin
    with _lock:
        for engine in _replica_engines:
            try:
                engine.dispose()
            except Exception:
                pass
        _replica_engines = []
        _replica_alive = []
        _round_robin = None


def status() -> dict:
    """Diagnostics snapshot (hosts only, never credentials)."""
    def _host(url: str) -> str:
        try:
            return url.split("@")[-1].split("/")[0]
        except Exception:
            return "?"
    engines = _build_replica_engines()
    with _lock:
        alive = list(_replica_alive)
    return {
        "replicas_configured": len(replica_urls()),
        "replicas_built": len(engines),
        "replicas_alive": sum(1 for flag in alive if flag),
        "balance": balance_mode(),
        "probe": probe_enabled(),
        "hosts": [_host(url) for url in replica_urls()],
    }
