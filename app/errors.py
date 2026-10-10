"""Helpers for turning internal failures into messages that are safe to display."""

import re

# Connection strings such as postgresql://user:password@host/db must never reach
# the browser, so credentials are stripped from anything we render in the UI.
_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[A-Za-z0-9+.\-]+://)(?P<user>[^:/@\s]+)(?::[^@/\s]*)?@"
)


def redact(value) -> str:
    """Return `value` as text with any database credentials removed."""
    text = str(value)
    return _CREDENTIAL_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", text)
