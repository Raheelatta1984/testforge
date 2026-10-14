"""QA agent hub: nine deterministic QA agents with optional AI insights.

See docs/qa-agent-hub.md. `router` (used by app/main.py) is loaded on first access, so
importing this package alone does not import the application. The MCP server relies on
that to keep the app's stdout banner out of its protocol stream.
"""
from __future__ import annotations

__all__ = ["router"]


def __getattr__(name: str):
    if name == "router":
        from .routes import router
        return router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
