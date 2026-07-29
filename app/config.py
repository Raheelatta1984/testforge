import os

# DEFINE THE PROJECT DIRECTORIES
# We use the user's home directory to ensure we always have write permissions
# especially in cloud environments like GitHub Codespaces or Render.
HOME_DIR = os.path.expanduser("~")
ARTIFACTS_DIR = os.path.join(HOME_DIR, "testforge_data")

# Create the folder structure required for the application to function
# rec: stores temporary recording frames and individual step screenshots
# runs: stores execution logs, validation screenshots, and videos
try:
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS_DIR, "runs"), exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS_DIR, "rec"), exist_ok=True)
except Exception as e:
    print(f"Error creating system directories: {e}")

# SET UP THE DATABASE CONNECTION
# Priority 1: DATABASE_URL environment variable (Used for Neon PostgreSQL)
# Priority 2: Local SQLite file (Default fallback)
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"sqlite:///{os.path.join(ARTIFACTS_DIR, 'testforge.db')}"
)

# Render and other providers often provide 'postgres://'
# SQLAlchemy requires 'postgresql://' to function correctly
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Detect environment and set flags
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "")
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")

# Export variables for other modules
ARTIFACTS = ARTIFACTS_DIR