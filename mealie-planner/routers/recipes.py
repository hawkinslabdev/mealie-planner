import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator

from config import DATA_PATH, get_credentials
from i18n import get_locale, load_locale_json
from database import (
    cache_last_refreshed,
    ensure_cache_fresh,
    get_cached_recipes,
    get_db,
    is_cache_refreshing,
    POLL_COOLDOWN_S,
    refresh_recipe_cache,
    upsert_recipe_cache,
)
from mealie import _outbound_sem, get_http_client, mealie_get, mealie_patch, mealie_post
from utils import rate_limiter, require_date, require_slug, require_uuid, task_manager

logger = logging.getLogger("mealie_planner")
router = APIRouter()

_last_poll_at: int = 0
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_AI_IMAGES = 5
_MAX_AI_TEXT_CHARS = 20000
_MEALIE_ERROR_KEYS = {
    "AI services are not enabled": "aiNotEnabled",
    "The AI provider was unable to complete the request. Please try again": "aiRequestFailed",
    "AI image services are not enabled": "imageProviderNotEnabled",
    "No recipe was found in the provided source": "noRecipeFound",
    "No content, images, or URL were provided": "noSource",
    "The AI provider did not return a recipe": "providerReturnedNothing",
    "Something went wrong while creating the recipe. Please try again": "unknownError",
    "Unable to read any content from the provided source": "unreadableSource",
    "Unable to download the video. It may be private, removed, or from an unsupported site": "videoDownloadFailed",
    "The AI provider is rate limiting requests. Please wait a moment and try again": "rateLimit",
}
_VIDEO_URL_RE = re.compile(r"youtube\.com|youtu\.be|tiktok\.com|instagram\.com/reel|vimeo\.com|twitch\.tv", re.I)
_IMAGE_CACHE_TTL = 86400  # 1 day

_img_cache_dir: Path | None = None


def _get_img_cache_dir() -> Path:
    global _img_cache_dir
    if _img_cache_dir is None:
        _img_cache_dir = Path(DATA_PATH) / "image_cache"
        _img_cache_dir.mkdir(exist_ok=True)
    return _img_cache_dir


def _img_cache_path(recipe_id: str) -> Path:
    return _get_img_cache_dir() / recipe_id


def _invalidate_image_cache(recipe_id: str) -> None:
    _img_cache_path(recipe_id).unlink(missing_ok=True)
_LOCALE_TO_BCP47: dict[str, str] = {
    "en": "en-US", "de": "de-DE", "nl": "nl-NL", "es": "es-ES",
    "fr": "fr-FR", "it": "it-IT", "pl": "pl-PL", "ru": "ru-RU",
    "cs": "cs-CZ", "sv": "sv-SE", "da": "da-DK", "no": "nb-NO",
    "pt_BR": "pt-BR",
}


@router.get("/api/recipes")
async def get_recipes(
    q: str | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    await ensure_cache_fresh()
    return await get_cached_recipes(q, limit=limit, offset=offset)


@router.get("/api/recipes/poll")
async def poll_recipe_changes():
    """One cheap Mealie call to detect new/updated recipes since last cache refresh."""
    global _last_poll_at
    now = int(time.time())
    if now - _last_poll_at < POLL_COOLDOWN_S:
        return {"stale": False}
    _last_poll_at = now

    try:
        url, token = get_credentials()
        if not url or not token:
            return {"stale": False}

        last_refreshed = await cache_last_refreshed()
        if last_refreshed is None:
            return {"stale": True}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/recipes",
                params={"page": 1, "perPage": 1, "orderBy": "dateUpdated", "orderDirection": "desc"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("items", [])
            mealie_total = payload.get("total")

        db = await get_db()
        cur = await db.execute("SELECT COUNT(*) FROM recipes")
        row = await cur.fetchone()
        total_count = row[0] if row else 0

        stale = False
        if items:
            date_updated = items[0].get("dateUpdated") or ""
            if date_updated:
                dt = datetime.fromisoformat(date_updated.replace("Z", "+00:00"))
                recipe_ts = int(dt.timestamp())
                stale = recipe_ts > last_refreshed

        if not stale and mealie_total is not None and mealie_total != total_count:
            stale = True

        if stale and not is_cache_refreshing():
            task_manager.spawn(refresh_recipe_cache())
        return {"stale": stale, "total_count": total_count}
    except Exception:
        return {"stale": False}


@router.post("/api/cache/refresh")
async def force_cache_refresh(request: Request):
    if not rate_limiter.check(request, key="refresh", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")
    task_manager.spawn(refresh_recipe_cache())
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM recipes")
    row = await cur.fetchone()
    return {"count": row[0] if row else 0}


@router.get("/api/media/{recipe_id}")
async def proxy_recipe_image(recipe_id: str, request: Request):
    require_uuid(recipe_id, "recipe ID")

    cache_path = _img_cache_path(recipe_id)
    now = time.time()

    if cache_path.exists():
        stat = cache_path.stat()
        if now - stat.st_mtime < _IMAGE_CACHE_TTL:
            etag = f'"{recipe_id}-{int(stat.st_mtime)}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304)
            return Response(
                content=cache_path.read_bytes(),
                media_type="image/webp",
                headers={"Cache-Control": f"public, max-age={_IMAGE_CACHE_TTL}", "ETag": etag},
            )

    url, token = get_credentials()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    full_url = f"{url.rstrip('/')}/api/media/recipes/{recipe_id}/images/min-original.webp"
    client = await get_http_client()
    try:
        async with _outbound_sem:
            resp = await client.get(full_url, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
        content = resp.content
        content_type = resp.headers.get("content-type", "image/webp")

        try:
            cache_path.write_bytes(content)
            etag = f'"{recipe_id}-{int(cache_path.stat().st_mtime)}"'
        except OSError:
            logger.warning("image_cache.write_failed recipe_id=%s", recipe_id)
            etag = f'"{recipe_id}"'

        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": f"public, max-age={_IMAGE_CACHE_TTL}", "ETag": etag},
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Image not found")
        raise HTTPException(status_code=502, detail="Failed to fetch image")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@router.get("/api/recipe-link/{slug}")
async def recipe_link(slug: str):
    require_slug(slug, "recipe slug")
    url, _ = get_credentials()
    if not url:
        raise HTTPException(status_code=400, detail="Mealie not configured")
    return RedirectResponse(url=f"{url.rstrip('/')}/g/home/r/{slug}")


@router.get("/api/recipes/{slug}")
async def get_recipe(slug: str):
    require_slug(slug, "recipe slug")
    data = await mealie_get(f"/api/recipes/{slug}")
    recipe_yield = (data.get("recipeYield") or "").strip()
    return {
        "id": data.get("id"),
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description"),
        "image_url": f"/api/media/{data['id']}" if data.get("id") else None,
        "yield": recipe_yield if any(c.isdigit() for c in recipe_yield) else None,
        "ingredients": [
            {
                "text": (i.get("display") or i.get("note") or (i.get("food") or {}).get("name") or "").strip(),
                "title": i.get("title") or None,
            }
            for i in (data.get("recipeIngredient") or [])
            if (i.get("display") or i.get("note") or (i.get("food") or {}).get("name"))
        ],
        "steps": [
            {"text": (s.get("text") or "").strip(), "title": s.get("title") or None}
            for s in (data.get("recipeInstructions") or [])
            if (s.get("text") or "").strip()
        ],
    }


class ImportUrlPayload(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL must be a valid http or https address.")
        return v


class QuickCreatePayload(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Recipe name cannot be empty.")
        if len(v) > 200:
            raise ValueError("Recipe name is too long (max 200 characters).")
        return v


def _image_filename(upload: UploadFile, content_type: str) -> str:
    ext = ""
    if upload.filename and "." in upload.filename:
        ext = upload.filename.rsplit(".", 1)[-1].lower()
    if not ext:
        ext = content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("jpg", "png", "webp", "gif"):
        ext = "jpg"
    return upload.filename or f"image.{ext}"


def _slug_from_mealie_response(body) -> str:
    if isinstance(body, str) and body:
        return body
    if isinstance(body, dict):
        slug = body.get("slug") or body.get("name")
        if slug:
            return slug
    raise HTTPException(status_code=502, detail=f"Unexpected Mealie response: {body!r}")


def _mealie_headers(token: str, request: Request) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept-Language": _LOCALE_TO_BCP47.get(get_locale(request), "en-US"),
    }


def _mealie_error_detail(resp: httpx.Response, request: Request) -> str:
    msg = ""
    try:
        d = resp.json().get("detail")
        if isinstance(d, dict) and d.get("message"):
            msg = str(d["message"]).strip()
    except Exception:
        pass
    logger.warning(
        "mealie.import_failed status=%s path=%s body=%s",
        resp.status_code, resp.request.url.path, (msg or resp.text)[:300],
    )
    key = _MEALIE_ERROR_KEYS.get(msg.rstrip("."))
    if key:
        translated = load_locale_json(get_locale(request)).get("quickAdd", {}).get("mealieErrors", {}).get(key)
        if translated:
            return translated
    if not msg:
        msg = resp.text[:200].strip() if resp.content else f"HTTP {resp.status_code}"
    return msg if msg.endswith((".", "!", "?")) else msg + "."


async def _finish_recipe_import(slug: str) -> dict:
    data = await mealie_get(f"/api/recipes/{slug}")
    recipe_id = data.get("id") if isinstance(data, dict) else None
    if not recipe_id:
        raise HTTPException(status_code=502, detail="Recipe imported but ID not found.")
    await upsert_recipe_cache(data)
    return {
        "id": recipe_id,
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description") or "",
        "image_url": f"/api/media/{recipe_id}",
    }


@router.post("/api/recipes/import-url")
async def import_recipe_url(payload: ImportUrlPayload, request: Request):
    if not rate_limiter.check(request, key="recipe-create", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")

    mealie_url, token = get_credentials()
    if not mealie_url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    base = mealie_url.rstrip("/")
    headers = _mealie_headers(token, request)
    is_video = bool(_VIDEO_URL_RE.search(payload.url))
    async with httpx.AsyncClient(timeout=180.0 if is_video else 35.0) as client:
        try:
            if is_video:
                resp = await client.post(f"{base}/api/recipes/create/ai", data={"url": payload.url}, headers=headers)
            else:
                resp = await client.post(
                    f"{base}/api/recipes/create/url",
                    json={"url": payload.url, "include_tags": True},
                    headers=headers,
                )
            resp.raise_for_status()
            slug = _slug_from_mealie_response(resp.json())
        except httpx.HTTPStatusError as e:
            detail = _mealie_error_detail(e.response, request)
            if e.response.status_code in (400, 422):
                return JSONResponse(
                    status_code=422,
                    content={"detail": detail},
                )
            raise HTTPException(status_code=502, detail=detail)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    return await _finish_recipe_import(slug)


@router.post("/api/recipes/quick-create")
async def quick_create_recipe(payload: QuickCreatePayload, request: Request):
    if not rate_limiter.check(request, key="recipe-create", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")

    mealie_url, token = get_credentials()
    if not mealie_url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    client = await get_http_client()
    try:
        resp = await client.post(
            f"{mealie_url.rstrip('/')}/api/recipes",
            json={"name": payload.name},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        slug = resp.json()
        if not isinstance(slug, str) or not slug:
            raise HTTPException(status_code=502, detail="Unexpected response from Mealie.")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=422, detail=f"Mealie error: {e.response.status_code}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e))

    data = await mealie_get(f"/api/recipes/{slug}")
    recipe_id = data.get("id")
    if not recipe_id:
        raise HTTPException(status_code=502, detail="Recipe created but ID not found.")

    await upsert_recipe_cache(data)
    return {
        "id": recipe_id,
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description") or "",
        "image_url": f"/api/media/{recipe_id}",
    }


@router.post("/api/recipes/{slug}/image")
async def upload_recipe_image(slug: str, file: UploadFile, request: Request):
    require_slug(slug, "recipe slug")
    if not rate_limiter.check(request, key="recipe-image", max_hits=10):
        raise HTTPException(status_code=429, detail="Too many requests.")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="File must be a JPEG, PNG, WebP, or GIF image.")

    contents = await file.read()
    if len(contents) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image too large (max 10 MB).")

    mealie_url, token = get_credentials()
    if not mealie_url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    ext = ""
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
    if not ext:
        ext = content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("jpg", "png", "webp", "gif"):
        ext = "jpg"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.put(
                f"{mealie_url.rstrip('/')}/api/recipes/{slug}/image",
                headers={"Authorization": f"Bearer {token}"},
                files={"image": (file.filename or f"image.{ext}", contents, content_type)},
                data={"extension": ext},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail="Image upload failed.")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    try:
        recipe_data = await mealie_get(f"/api/recipes/{slug}")
        if isinstance(recipe_data, dict):
            rid = recipe_data.get("id")
            if rid:
                _invalidate_image_cache(rid)
    except Exception:
        pass

    return {"ok": True}


@router.post("/api/recipes/import-ai")
async def import_recipe_with_ai(
    request: Request,
    files: list[UploadFile] = File(default_factory=list),
    text: str = Form(""),
    url: str = Form(""),
    translate: bool = Form(False),
    create_new_organizers: bool = Form(False),
):
    if not rate_limiter.check(request, key="recipe-create", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")

    text = text.strip()
    if len(text) > _MAX_AI_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"Pasted text is too long (max {_MAX_AI_TEXT_CHARS:,} characters).",
        )
    if len(files) > _MAX_AI_IMAGES:
        raise HTTPException(status_code=400, detail=f"Attach at most {_MAX_AI_IMAGES} photos.")

    uploads: list[tuple[str, tuple[str, bytes, str]]] = []
    for upload in files:
        content_type = (upload.content_type or "").split(";")[0].strip().lower()
        if content_type not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="Photos must be JPEG, PNG, WebP, or GIF images.")
        contents = await upload.read()
        if not contents:
            continue
        if len(contents) > _MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="Image too large (max 10 MB).")
        uploads.append(("images", (_image_filename(upload, content_type), contents, content_type)))

    url = url.strip()
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=422, detail="URL must be a valid http or https address.")
    if not uploads and not text and not url:
        raise HTTPException(status_code=400, detail="Add a photo, a link, or paste the recipe text.")

    mealie_url, token = get_credentials()
    if not mealie_url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    bcp47 = _LOCALE_TO_BCP47.get(get_locale(request)) if translate else None
    form: dict[str, str] = {}
    if text:
        form["content"] = text
    if url:
        form["url"] = url
    if bcp47:
        form["translateLanguage"] = bcp47
    if create_new_organizers:
        form["createNewOrganizers"] = "true"

    base = mealie_url.rstrip("/")
    headers = _mealie_headers(token, request)

    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(
                f"{base}/api/recipes/create/ai",
                data=form,
                files=uploads,
                headers=headers,
            )
            if resp.status_code in (404, 405):
                if not uploads or url:
                    raise HTTPException(
                        status_code=422,
                        detail="This Mealie version cannot import recipes from text or links. Upgrade to Mealie 3.24 or newer, or attach a photo instead.",
                    )
                logger.info("ai_import.fallback_to_legacy_image_endpoint status=%s", resp.status_code)
                resp = await client.post(
                    f"{base}/api/recipes/create/image",
                    params={"translateLanguage": bcp47} if bcp47 else {},
                    files=uploads[:1],
                    headers=headers,
                )
            resp.raise_for_status()
            slug = _slug_from_mealie_response(resp.json())
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=502, detail=_mealie_error_detail(e.response, request))
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    result = await _finish_recipe_import(slug)

    try:
        await mealie_post("/api/comments", {
            "recipeId": result["id"],
            "text": "Imported with AI via Mealie Planner.",
        })
    except Exception:
        logger.debug("ai_import.comment_failed slug=%s", slug)

    return result


@router.get("/api/sparkle")
async def sparkle(date: str, meal_type: str = "dinner"):
    import random

    require_date(date)

    all_recipes = await get_cached_recipes(limit=10000)
    if not all_recipes:
        raise HTTPException(
            status_code=404,
            detail="No recipes in cache. Trigger /api/cache/refresh first.",
        )

    return random.choice(all_recipes)
