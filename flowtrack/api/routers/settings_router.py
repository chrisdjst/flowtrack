"""Settings CRUD endpoints.

GET  /api/settings          — list all settings with effective value and source
PATCH /api/settings/{key}   — set a value (persists to DB, invalidates cache)
DELETE /api/settings/{key}  — remove DB override (reverts to env or default)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowtrack.core.runtime_config import RuntimeConfig, SETTINGS_SCHEMA

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingUpdate(BaseModel):
    value: str


@router.get("")
def list_settings() -> list[dict]:
    return RuntimeConfig.get_all_effective()


@router.patch("/{key}")
def update_setting(key: str, body: SettingUpdate) -> dict:
    if key not in SETTINGS_SCHEMA:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    RuntimeConfig.set(key, body.value)
    effective = RuntimeConfig.get_all_effective()
    return next(s for s in effective if s["key"] == key)


@router.delete("/{key}")
def reset_setting(key: str) -> dict:
    if key not in SETTINGS_SCHEMA:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    RuntimeConfig.delete(key)
    effective = RuntimeConfig.get_all_effective()
    return next(s for s in effective if s["key"] == key)
