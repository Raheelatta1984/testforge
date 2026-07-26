import os

def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")

IS_TERMUX = _is_termux()
ARTIFACTS = os.environ.get(
    "TF_ARTIFACTS",
    os.path.expanduser("~/testforge/artifacts") if IS_TERMUX else "/app/artifacts")
os.makedirs(ARTIFACTS, exist_ok=True)

# Automatically picks up Neon PostgreSQL URL from environment or falls back to SQLite
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    os.environ.get("NEON_DATABASE_URL", f"sqlite:///{ARTIFACTS}/testforge.db")
)

# Fix for Render/Neon postgres:// prefix compatibility in SQLAlchemy
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")