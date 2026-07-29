import os
HOME = os.path.expanduser("~")
ARTIFACTS = os.path.join(HOME, "testforge_data")
os.makedirs(os.path.join(ARTIFACTS, "runs"), exist_ok=True)
os.makedirs(os.path.join(ARTIFACTS, "rec"), exist_ok=True)
os.makedirs(os.path.join(ARTIFACTS, "jenkins"), exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(ARTIFACTS, 'testforge.db')}")
if DATABASE_URL.startswith("postgres://"): DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")
CICD_INTERVAL = 300 # 5 minutes