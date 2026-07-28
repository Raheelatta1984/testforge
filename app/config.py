import os

# FORCE ALL WRITING TO THE USER HOME DIRECTORY
# This is the ONLY place GitHub and Render guaranteed permission
HOME = os.path.expanduser("~")
ARTIFACTS = os.path.join(HOME, "testforge_data")

# Create the folders immediately
os.makedirs(ARTIFACTS, exist_ok=True)
os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)

# Database resides in the user home
DATABASE_URL = os.environ.get(
    "DATABASE_URL", 
    f"sqlite:///{os.path.join(ARTIFACTS, 'testforge.db')}"
)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_TERMUX = False
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")

print(f"--- SYSTEM BOOTED ---")
print(f"STORAGE: {ARTIFACTS}")
print(f"DATABASE: {DATABASE_URL}")