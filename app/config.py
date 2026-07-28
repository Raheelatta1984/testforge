import os

# --- 1. DEFINE GUARANTEED WRITABLE PATHS ---
# GitHub Codespaces blocks writing to the /app folder. 
# Linux systems ALWAYS allow writing to /tmp.
ARTIFACTS = "/tmp/testforge_artifacts"

# Auto-create the folder structure in the writable location
try:
    os.makedirs(ARTIFACTS, exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)
    print(f"✅ WRITABLE STORAGE INITIALIZED AT: {ARTIFACTS}")
except Exception as e:
    # Fallback to home directory if /tmp is somehow restricted
    ARTIFACTS = os.path.expanduser("~/testforge_artifacts")
    os.makedirs(ARTIFACTS, exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)
    print(f"⚠️ FALLBACK STORAGE INITIALIZED AT: {ARTIFACTS}")

# --- 2. DATABASE CONFIGURATION ---
# Neon/Postgres takes priority, otherwise local SQLite in the writable artifacts folder
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"sqlite:///{os.path.join(ARTIFACTS, 'testforge.db')}"
)

# Fix for Render/Neon postgres:// prefix compatibility
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# --- 3. OTHER CONFIGS ---
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")