import os

# 1. GET THE ABSOLUTE PATH OF THE PROJECT ROOT
# This ensures we always know exactly where we are allowed to write files.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2. DEFINE AND CREATE THE ARTIFACTS DIRECTORY
# We put everything in a local folder called 'artifacts'
ARTIFACTS = os.path.join(BASE_DIR, "artifacts")

# Auto-create the folder structure on startup
try:
    os.makedirs(ARTIFACTS, exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)
except Exception as e:
    print(f"Warning: Could not create artifacts directory at {ARTIFACTS}: {e}")

# 3. DATABASE CONFIGURATION
# It will use the DATABASE_URL environment variable (for Neon/Render)
# Or default to a local SQLite file inside the artifacts folder.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"sqlite:///{os.path.join(ARTIFACTS, 'testforge.db')}"
)

# Fix for Render/Postgres strings (SQLAlchemy requires postgresql:// not postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 4. PLATFORM DETECT & MOCK MODE
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")