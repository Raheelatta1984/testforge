import os
def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")
IS_TERMUX = _is_termux()
ARTIFACTS = os.environ.get("TF_ARTIFACTS", os.path.expanduser("~/testforge/artifacts") if IS_TERMUX else "/app/artifacts")
os.makedirs(ARTIFACTS, exist_ok=True)
DATABASE_URL = os.environ.get("TF_DATABASE_URL", f"sqlite:///{ARTIFACTS}/testforge.db")
DEMO_MODE = not os.environ.get("ANTHROPIC_API_KEY")
