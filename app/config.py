import os

# FORCE ALL WRITING TO THE USER HOME DIRECTORY FOR CLOUD PERMISSIONS
HOME = os.path.expanduser("~")
ARTIFACTS = os.path.join(HOME, "testforge_data")

# Create the folders immediately
try:
    os.makedirs(ARTIFACTS, exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
    os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)
except Exception as e:
    print(f"Directory Error: {e}")

# Database resides in user home or uses Neon URL
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"sqlite:///{os.path.join(ARTIFACTS, 'testforge.db')}"
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_TERMUX = False
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")