"""QA agent hub: nine deterministic QA agents with optional AI insights.

See docs/qa-agent-hub.md. The package is imported by app/main.py, which mounts
`router`; nothing here runs at import time except definitions.
"""
from .routes import router

__all__ = ["router"]
