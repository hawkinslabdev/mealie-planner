import json
import logging
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from config import DOCKER_MODE, encrypt_token, get_credentials, get_mode, write_credentials
from database import get_db, refresh_recipe_cache
from mealie import get_http_client, mealie_get
from utils import rate_limiter, task_manager

logger = logging.getLogger("mealie_planner")
router = APIRouter()

_status_cache: dict = {}
_status_cached_at: float = 0.0
_STATUS_TTL: int = 30

_capabilities_cache: dict = {}
_capabilities_cached_at: float = 0.0
_CAPABILITIES_TTL: int = 60

_SETTINGS_DEFAULTS: dict[str, object] = {
    "show_quick_add": True,
    "translate_recipe": False,
    "quick_add_tab": "url",
}
_ALLOWED_SETTINGS: set[str] = set(_SETTINGS_DEFAULTS)


@router.get("/api/capabilities")
async def get_capabilities():
    global _capabilities_cache, _capabilities_cached_at
    if _capabilities_cache and time.time() - _capabilities_cached_at < _CAPABILITIES_TTL:
        return _capabilities_cache

    image_import_enabled = False
    video_instructions_enabled = True
    url, token = get_credentials()
    if url and token:
        try:
            data = await mealie_get("/api/groups/self")
            ai = data.get("aiProviderSettings") or {}
            providers = ai.get("providers") or []
            image_import_enabled = bool(ai.get("imageProviderEnabled") and len(providers) > 0)
            video_instructions_enabled = bool(ai.get("audioProviderId") and len(providers) > 0)
        except Exception:
            pass

    result = {"image_import_enabled": image_import_enabled, "video_instructions_enabled": video_instructions_enabled}
    _capabilities_cache = result
    _capabilities_cached_at = time.time()
    return result


@router.get("/api/status")
async def get_status(request: Request):
    global _status_cache, _status_cached_at
    url, token = get_credentials()
    configured = bool(url and token)

    if _status_cache and time.time() - _status_cached_at < _STATUS_TTL:
        return {**_status_cache, "configured": configured}

    reachable = False
    version = None

    if configured:
        try:
            data = await mealie_get("/api/app/about")
            reachable = True
            version = data.get("version")
        except HTTPException:
            pass

    result = {
        "configured": configured,
        "mode": get_mode(),
        "mealie_reachable": reachable,
        "version": version,
    }
    _status_cache = {"mode": get_mode(), "mealie_reachable": reachable, "version": version}
    _status_cached_at = time.time()
    return result


@router.get("/api/config")
async def get_config():
    url, _ = get_credentials()
    return {"mealie_url": url or "", "mode": get_mode()}


class ConfigPayload(BaseModel):
    mealie_url: str
    api_token: str = ""

    @field_validator("mealie_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL must be a valid http or https address.")
        return v


@router.post("/api/config")
async def save_config(payload: ConfigPayload, request: Request):
    global _status_cached_at, _capabilities_cached_at
    if DOCKER_MODE:
        raise HTTPException(status_code=400, detail="Configure via environment variables.")

    if not rate_limiter.check(request, key="config", max_hits=10):
        raise HTTPException(status_code=429, detail="Too many requests.")

    token_to_use = payload.api_token
    if not token_to_use:
        _, existing = get_credentials()
        if not existing:
            raise HTTPException(status_code=422, detail="API token is required for initial setup.")
        token_to_use = existing

    client = await get_http_client()
    headers = {"Authorization": f"Bearer {token_to_use}"}
    try:
        # SSRF: scheme is validated by ConfigPayload; follow_redirects=False prevents
        # chained redirects to internal hosts.
        resp = await client.get(
            f"{payload.mealie_url.rstrip('/')}/api/app/about",
            headers=headers,
            follow_redirects=False,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = (
            "Invalid API token."
            if e.response.status_code == 401
            else f"Mealie error: {e.response.status_code}"
        )
        raise HTTPException(status_code=422, detail=detail)
    except httpx.HTTPError:
        raise HTTPException(status_code=422, detail="Could not reach Mealie at the provided URL.")

    write_credentials(payload.mealie_url, encrypt_token(token_to_use))
    ip = request.client.host if request.client else "unknown"
    logger.info("config.updated url=%s ip=%s", payload.mealie_url, ip)
    _status_cached_at = 0.0
    _capabilities_cached_at = 0.0
    task_manager.spawn(refresh_recipe_cache())
    return {"ok": True}


@router.get("/api/settings")
async def get_settings():
    db = await get_db()
    async with db.execute("SELECT key, value FROM settings") as cur:
        rows = await cur.fetchall()
    result = dict(_SETTINGS_DEFAULTS)
    for row in rows:
        if row[0] in _ALLOWED_SETTINGS:
            try:
                result[row[0]] = json.loads(row[1])
            except Exception:
                pass
    return result


_VALID_QUICK_ADD_TABS = {"url", "recipe", "image"}


class SettingsPatch(BaseModel):
    show_quick_add: bool | None = None
    translate_recipe: bool | None = None
    quick_add_tab: str | None = None

    @field_validator("quick_add_tab")
    @classmethod
    def _check_tab(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_QUICK_ADD_TABS:
            raise ValueError(f"quick_add_tab must be one of {_VALID_QUICK_ADD_TABS}")
        return v


@router.patch("/api/settings")
async def update_settings(payload: SettingsPatch, request: Request):
    if not rate_limiter.check(request, key="settings", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    db = await get_db()
    updates = payload.model_dump(exclude_none=True)
    for key, value in updates.items():
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    if updates:
        await db.commit()
    return {"ok": True}
