import os, shutil
from .config import IS_TERMUX
def _mode() -> str:
    m = os.environ.get("TF_BROWSER_MODE", "auto").lower()
    if m == "auto": return "system" if IS_TERMUX else "bundled"
    return m
def launch_kwargs() -> dict:
    kw = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if _mode() == "system":
        exe = next((shutil.which(n) for n in ("chromium", "chromium-browser", "chrome") if shutil.which(n)), None)
        if not exe: raise RuntimeError("Chromium not found.")
        kw["executable_path"] = exe
        kw["args"] += ["--disable-setuid-sandbox", "--disable-gpu", "--single-process"]
    return kw
def video_ok() -> bool:
    return _mode() == "bundled" and os.environ.get("TF_NO_VIDEO") != "1"
