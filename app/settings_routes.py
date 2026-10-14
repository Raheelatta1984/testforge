"""Master setup endpoints: read and change the instance-wide settings.

Both routes sit behind the same access gate as the rest of /api/, so the settings
are only visible to and changeable by someone who could already use the dashboard.
"""
from fastapi import APIRouter, HTTPException

from app import app_settings

router = APIRouter()


@router.get("/api/settings")
def read_settings():
    return app_settings.payload()


@router.put("/api/settings")
def write_settings(body: dict):
    try:
        app_settings.update(body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return app_settings.payload()
