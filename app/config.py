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

# DIRECTORY HIERARCHY
HOME = os.path.expanduser("~")
ARTIFACTS = os.path.join(HOME, "testforge_titan_data")
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
MAX_CONCURRENT_BROWSERS = 1

# PERFORMANCE TUNING
os.environ["PYPPETEER_NVNC"] = "true"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ROG_TITAN")

print(f"--- TITAN ERP SYSTEM BOOTED ---")
print(f"LOGS: {LOGS_DIR}")