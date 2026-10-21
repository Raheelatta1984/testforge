import os
import sys
import logging
from pathlib import Path

# ROG AGENT IDENTITIES
AGENT_ROLES = {
    "MONITOR": "ROG_ORCHESTRATOR_ALPHA",
    "DEVOPS": "ROG_INFRA_STABILIZER",
    "QA": "ROG_QUALITY_VALIATOR",
    "REPORTER": "ROG_INSIGHT_ENGINE"
}

# Termux (Android) has no bundled Playwright browser, so it must use the system one.
IS_TERMUX = bool(os.environ.get("TERMUX_VERSION")) or os.path.isdir("/data/data/com.termux")

# DIRECTORY HIERARCHY
# TF_ARTIFACTS is set by Docker / start.sh. Falling back to a home directory
# ignored that and wrote runs somewhere the container volume was not mounted.
HOME = os.path.expanduser("~")
ARTIFACTS = os.environ.get("TF_ARTIFACTS") or os.path.join(HOME, "testforge_titan_data")
RUNS_DIR = os.path.join(ARTIFACTS, "runs")
REC_DIR = os.path.join(ARTIFACTS, "recordings")
LOGS_DIR = os.path.join(ARTIFACTS, "system_logs")

for path in [ARTIFACTS, RUNS_DIR, REC_DIR, LOGS_DIR]:
    os.makedirs(path, exist_ok=True)

# DATABASE RESOLUTION
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = f"sqlite:///{os.path.join(ARTIFACTS, 'testforge_erp.db')}"

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# AI CONSTANTS
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
DEMO_MODE = not bool(ANTHROPIC_KEY)
CICD_HEARTBEAT_SECONDS = 300 # 5-minute detection cycle
CICD_INTERVAL = int(os.environ.get("TF_CICD_INTERVAL", str(CICD_HEARTBEAT_SECONDS)))
MAX_CONCURRENT_BROWSERS = 1

# PERFORMANCE TUNING
os.environ["PYPPETEER_NVNC"] = "true"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ROG_TITAN")

# --- scaling knobs (all optional; every default preserves old behaviour) ---
# PgBouncer: TF_PGBOUNCER=1 (or a :6439 URL, or TF_PGBOUNCER_URL) routes the
# primary through the pooler with a pooler-friendly (NullPool) engine.
# Reads: TF_READ_REPLICAS="url1,url2" fans read-only sessions (see
# app/db_replicas.py) across Neon read replicas.
# Cache: TF_MEMCACHED_SERVERS="host:port,..." shares app/cache.py entries
# between processes; unset = bounded in-memory cache.
# Images: TF_IMAGE_SHARDS / TF_IMAGE_SHARD_MAP(_FILE) steer
# app/image_shards.py logical shards over the machine map.
# IDs: TF_SHARD_ID / TF_ID_EPOCH_MS steer app/ids.py next_id().
PGBOUNCER_ENABLED = os.environ.get("TF_PGBOUNCER",
                    os.environ.get("TF_PGBOUNCER_ENABLED", "")).strip().lower() in {
    "1", "true", "yes", "on"}
PGBOUNCER_URL = os.environ.get("TF_PGBOUNCER_URL", "").strip()
READ_REPLICAS = [part.strip()
                 for part in os.environ.get("TF_READ_REPLICAS", "").split(",")
                 if part.strip()]
MEMCACHED_SERVERS = [part.strip()
                     for part in os.environ.get("TF_MEMCACHED_SERVERS", "").split(",")
                     if part.strip()]

print(f"--- TITAN ERP SYSTEM BOOTED ---")
print(f"LOGS: {LOGS_DIR}")