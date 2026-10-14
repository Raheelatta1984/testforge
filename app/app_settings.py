"""Master setup: the instance-wide region, time zone, date/time format, language and
display defaults.

Every value is validated here, on the server, before it is stored. The dashboard
reads the same catalog (GET /api/settings) so the choices it offers can never
drift from what the server accepts.

A region is a starting point, not a lock: choosing one fills in its time zone,
date format, time format and language, and each of those can then be changed
on its own.
"""
from __future__ import annotations

import threading
import zoneinfo
from copy import deepcopy

from app.db import AppSetting, SessionLocal

MASTER_KEY = "master"
_LOCK = threading.Lock()

# Date formats are identified by the pattern the dashboard shows, so the
# stored value reads the same way it is displayed.
DATE_FORMATS = [
    {"id": "YYYY-MM-DD", "example": "2026-10-14"},
    {"id": "DD/MM/YYYY", "example": "14/10/2026"},
    {"id": "MM/DD/YYYY", "example": "10/14/2026"},
    {"id": "DD.MM.YYYY", "example": "14.10.2026"},
    {"id": "DD-MM-YYYY", "example": "14-10-2026"},
    {"id": "YYYY/MM/DD", "example": "2026/10/14"},
    {"id": "D MMM YYYY", "example": "14 Oct 2026"},
]
DATE_FORMAT_IDS = {item["id"] for item in DATE_FORMATS}
TIME_FORMATS = [{"id": "24h", "example": "14:05:33"}, {"id": "12h", "example": "2:05:33 PM"}]
TIME_FORMAT_IDS = {item["id"] for item in TIME_FORMATS}

# Languages the dashboard can display. Only the navigation and the Master setup
# screen are translated so far; everything else falls back to English.
LANGUAGES = [
    {"id": "en", "label": "English"},
    {"id": "de", "label": "Deutsch"},
    {"id": "fr", "label": "Français"},
    {"id": "es", "label": "Español"},
]
LANGUAGE_IDS = {item["id"] for item in LANGUAGES}

WINDOWS_VERSIONS = [{"id": "windows_10", "label": "Windows 10"}, {"id": "windows_11", "label": "Windows 11"}]
SCREEN_ZOOMS = [
    {"id": "fit", "label": "Fit the whole screen"},
    {"id": "100", "label": "100%"},
    {"id": "150", "label": "150%"},
    {"id": "200", "label": "200%"},
]

# code, display name, default time zone, date format, time format, language.
# Every default below is an ordinary local convention for that country; a user
# who wants something else changes the field, the region stays as selected.
_REGION_ROWS = [
    ("GLOBAL", "Global / International (UTC)", "UTC", "YYYY-MM-DD", "24h", "en"),
    ("US", "United States", "America/New_York", "MM/DD/YYYY", "12h", "en"),
    ("CA", "Canada", "America/Toronto", "YYYY-MM-DD", "12h", "en"),
    ("MX", "Mexico", "America/Mexico_City", "DD/MM/YYYY", "12h", "es"),
    ("BR", "Brazil", "America/Sao_Paulo", "DD/MM/YYYY", "24h", "en"),
    ("AR", "Argentina", "America/Argentina/Buenos_Aires", "DD/MM/YYYY", "24h", "es"),
    ("GB", "United Kingdom", "Europe/London", "DD/MM/YYYY", "24h", "en"),
    ("IE", "Ireland", "Europe/Dublin", "DD/MM/YYYY", "24h", "en"),
    ("DE", "Germany", "Europe/Berlin", "DD.MM.YYYY", "24h", "de"),
    ("CH", "Switzerland", "Europe/Zurich", "DD.MM.YYYY", "24h", "de"),
    ("FR", "France", "Europe/Paris", "DD/MM/YYYY", "24h", "fr"),
    ("ES", "Spain", "Europe/Madrid", "DD/MM/YYYY", "24h", "es"),
    ("PT", "Portugal", "Europe/Lisbon", "DD/MM/YYYY", "24h", "en"),
    ("IT", "Italy", "Europe/Rome", "DD/MM/YYYY", "24h", "en"),
    ("NL", "Netherlands", "Europe/Amsterdam", "DD-MM-YYYY", "24h", "en"),
    ("SE", "Sweden", "Europe/Stockholm", "YYYY-MM-DD", "24h", "en"),
    ("PL", "Poland", "Europe/Warsaw", "DD.MM.YYYY", "24h", "en"),
    ("TR", "Türkiye", "Europe/Istanbul", "DD.MM.YYYY", "24h", "en"),
    ("ZA", "South Africa", "Africa/Johannesburg", "YYYY/MM/DD", "24h", "en"),
    ("NG", "Nigeria", "Africa/Lagos", "DD/MM/YYYY", "12h", "en"),
    ("KE", "Kenya", "Africa/Nairobi", "DD/MM/YYYY", "12h", "en"),
    ("EG", "Egypt", "Africa/Cairo", "DD/MM/YYYY", "12h", "en"),
    ("AE", "United Arab Emirates", "Asia/Dubai", "DD/MM/YYYY", "12h", "en"),
    ("SA", "Saudi Arabia", "Asia/Riyadh", "DD/MM/YYYY", "12h", "en"),
    ("PK", "Pakistan", "Asia/Karachi", "DD/MM/YYYY", "12h", "en"),
    ("IN", "India", "Asia/Kolkata", "DD/MM/YYYY", "12h", "en"),
    ("SG", "Singapore", "Asia/Singapore", "DD/MM/YYYY", "12h", "en"),
    ("CN", "China", "Asia/Shanghai", "YYYY-MM-DD", "24h", "en"),
    ("JP", "Japan", "Asia/Tokyo", "YYYY/MM/DD", "24h", "en"),
    ("AU", "Australia", "Australia/Sydney", "DD/MM/YYYY", "12h", "en"),
    ("NZ", "New Zealand", "Pacific/Auckland", "DD/MM/YYYY", "12h", "en"),
]
REGIONS = [
    {"code": code, "name": name, "timezone": tz, "date_format": df, "time_format": tf, "language": lang}
    for code, name, tz, df, tf, lang in _REGION_ROWS
]
REGION_BY_CODE = {item["code"]: item for item in REGIONS}

DEFAULTS = {
    "region": "GLOBAL",
    "timezone": "UTC",
    "date_format": "YYYY-MM-DD",
    "time_format": "24h",
    "language": "en",
    "default_windows_version": "windows_11",
    "screen_zoom": "fit",
}
EDITABLE = set(DEFAULTS)


def _timezones() -> list[str]:
    # Only real IANA zones: the zoneinfo database also lists posix/ and right/
    # mirrors and legacy aliases that are not useful choices in a picker.
    names = {
        name for name in zoneinfo.available_timezones()
        if (name == "UTC" or "/" in name) and not name.startswith(("posix/", "right/", "Etc/"))
    }
    names.add("UTC")
    return sorted(names)


_TIMEZONES: list[str] | None = None


def timezones() -> list[str]:
    global _TIMEZONES
    if _TIMEZONES is None:
        _TIMEZONES = _timezones()
    return _TIMEZONES


def validate_value(key: str, value):
    """Return the stored form of one setting, or raise ValueError with a reason."""
    if key not in EDITABLE:
        raise ValueError(f"'{key}' is not a master setting")
    if key == "region":
        if not isinstance(value, str) or value not in REGION_BY_CODE:
            raise ValueError("Choose a region from the list")
        return value
    if key == "timezone":
        if not isinstance(value, str) or value not in set(timezones()):
            raise ValueError("Choose a time zone from the list")
        return value
    if key == "date_format":
        if value not in DATE_FORMAT_IDS:
            raise ValueError("Choose a date format from the list")
        return value
    if key == "time_format":
        if value not in TIME_FORMAT_IDS:
            raise ValueError("Choose 24-hour or 12-hour time")
        return value
    if key == "language":
        if value not in LANGUAGE_IDS:
            raise ValueError("Choose a language from the list")
        return value
    if key == "default_windows_version":
        if value not in {item["id"] for item in WINDOWS_VERSIONS}:
            raise ValueError("Choose Windows 10 or Windows 11")
        return value
    if key == "screen_zoom":
        if value not in {item["id"] for item in SCREEN_ZOOMS}:
            raise ValueError("Choose a screen zoom from the list")
        return value
    raise ValueError(f"'{key}' is not a master setting")  # pragma: no cover


def _stored() -> dict:
    with SessionLocal() as db:
        row = db.get(AppSetting, MASTER_KEY)
        value = row.value if row is not None and isinstance(row.value, dict) else {}
    return value


def current() -> dict:
    """The effective master settings. Anything missing or no longer valid falls back to its default."""
    stored = _stored()
    result = {}
    for key, default in DEFAULTS.items():
        try:
            result[key] = validate_value(key, stored.get(key, default))
        except ValueError:
            result[key] = default
    return result


def update(patch: dict) -> dict:
    """Validate a partial update and store the merged result. Unknown keys are refused."""
    if not isinstance(patch, dict):
        raise ValueError("Settings must be an object")
    unknown = sorted(set(patch) - EDITABLE)
    if unknown:
        raise ValueError("Unknown setting(s): " + ", ".join(unknown[:5]))
    checked = {key: validate_value(key, value) for key, value in patch.items()}
    with _LOCK:
        merged = {**current(), **checked}
        with SessionLocal() as db:
            row = db.get(AppSetting, MASTER_KEY)
            if row is None:
                row = AppSetting(key=MASTER_KEY, value=merged)
                db.add(row)
            else:
                row.value = merged
            db.commit()
    return merged


def payload() -> dict:
    """Everything the Master setup screen needs, in one response."""
    return {
        "settings": current(),
        "defaults": deepcopy(DEFAULTS),
        "options": {
            "regions": deepcopy(REGIONS),
            "timezones": list(timezones()),
            "date_formats": deepcopy(DATE_FORMATS),
            "time_formats": deepcopy(TIME_FORMATS),
            "languages": deepcopy(LANGUAGES),
            "windows_versions": deepcopy(WINDOWS_VERSIONS),
            "screen_zooms": deepcopy(SCREEN_ZOOMS),
        },
    }
