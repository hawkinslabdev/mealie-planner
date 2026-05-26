import json
import logging
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx
from curl_cffi.requests import AsyncSession as _CurlSession
from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator

from config import get_credentials
from i18n import get_locale
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
from mealie import get_http_client, mealie_get, mealie_patch, mealie_post
from utils import extract_og_image, normalize_meal_entry, rate_limiter, require_slug, require_uuid, task_manager

logger = logging.getLogger("mealie_planner")
router = APIRouter()

_last_poll_at: int = 0
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
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
async def proxy_recipe_image(recipe_id: str):
    require_uuid(recipe_id, "recipe ID")
    url, token = get_credentials()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    full_url = f"{url.rstrip('/')}/api/media/recipes/{recipe_id}/images/min-original.webp"
    headers = {"Authorization": f"Bearer {token}"}

    client = await get_http_client()
    try:
        resp = await client.get(full_url, headers=headers)
        resp.raise_for_status()
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/webp"),
            headers={"Cache-Control": "public, max-age=86400"},
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
    return {
        "id": data.get("id"),
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description"),
        "image_url": f"/api/media/{data['id']}" if data.get("id") else None,
    }


async def _fetch_and_import_via_proxy(
    recipe_url: str, mealie_url: str, token: str
) -> tuple[str, str | None]:
    """Fetch recipe HTML impersonating Chrome, then send to Mealie's HTML/JSON stream importer.
    Returns (slug, og_image_url_or_none)."""
    async with _CurlSession() as session:
        try:
            page = await session.get(recipe_url, impersonate="chrome146", timeout=30)
            page.raise_for_status()
        except Exception as e:
            resp = getattr(e, "response", None)
            sc = resp.status_code if resp is not None else None
            detail = (
                "The website is blocking server-side access — create the recipe manually."
                if sc == 403
                else f"Could not fetch the recipe page: {e}"
            )
            raise HTTPException(status_code=502, detail=detail)

    og_image_url = extract_og_image(page.text)
    logger.debug("proxy.fetched url=%s bytes=%d og_image=%r", recipe_url, len(page.content), og_image_url)

    slug: str | None = None
    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            async with client.stream(
                "POST",
                f"{mealie_url.rstrip('/')}/api/recipes/create/html-or-json/stream",
                # Include original URL so Mealie can resolve relative image paths and set orgURL
                json={"data": page.text, "url": recipe_url, "include_tags": True},
                headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code >= 400:
                    body_bytes = await resp.aread()
                    try:
                        detail = json.loads(body_bytes).get("detail", body_bytes.decode()[:200])
                    except Exception:
                        detail = body_bytes.decode()[:200]
                    raise HTTPException(
                        status_code=502, detail=f"Mealie could not parse the recipe — {detail}"
                    )
                async for line in resp.aiter_lines():
                    logger.debug("proxy.sse line=%r", line)
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(evt, str) and evt:
                        slug = evt
                    elif isinstance(evt, dict):
                        data = evt.get("data", evt)
                        if isinstance(data, str) and data:
                            slug = data
                        elif isinstance(data, dict):
                            s = data.get("slug") or data.get("name")
                            if s:
                                slug = s
        except HTTPException:
            raise
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    logger.debug("proxy.slug slug=%r", slug)
    if not slug:
        raise HTTPException(
            status_code=502,
            detail="Mealie could not extract a recipe from this page — try a different URL",
        )
    return slug, og_image_url


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


async def _finish_recipe_import(slug: str) -> dict:
    data = await mealie_get(f"/api/recipes/{slug}")
    recipe_id = data.get("id")
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

    async with httpx.AsyncClient(timeout=35.0) as client:
        try:
            resp = await client.post(
                f"{mealie_url.rstrip('/')}/api/recipes/create/url",
                json={"url": payload.url, "include_tags": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            body = resp.json()
            # Mealie returns bare slug string (older) or {"slug": "..."} object (newer)
            if isinstance(body, str) and body:
                slug = body
            elif isinstance(body, dict):
                slug = body.get("slug") or body.get("name")
                if not slug:
                    raise HTTPException(status_code=502, detail=f"Unexpected Mealie response: {body}")
            else:
                raise HTTPException(status_code=502, detail=f"Unexpected Mealie response: {body!r}")
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200] if e.response.content else str(e)
            if e.response.status_code in (400, 422):
                return JSONResponse(
                    status_code=422,
                    content={"detail": f"Could not import recipe — {detail}", "proxy_available": True},
                )
            raise HTTPException(status_code=502, detail=f"Mealie {e.response.status_code}: {detail}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    return await _finish_recipe_import(slug)


@router.post("/api/recipes/import-url-proxy")
async def import_recipe_url_proxy(payload: ImportUrlPayload, request: Request):
    if not rate_limiter.check(request, key="recipe-create", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")

    mealie_url, token = get_credentials()
    if not mealie_url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    slug, og_image_url = await _fetch_and_import_via_proxy(payload.url, mealie_url, token)
    result = await _finish_recipe_import(slug)
    recipe_id = result["id"]

    # Ensure orgURL is set (Mealie may not set it from HTML-only import)
    try:
        await mealie_patch(f"/api/recipes/{slug}", {"orgURL": payload.url})
    except Exception:
        logger.debug("proxy.patch_orgurl_failed slug=%s", slug)

    # Upload og:image — ensures the recipe card shows an image in the planner
    if og_image_url:
        try:
            async with _CurlSession() as session:
                img_resp = await session.get(og_image_url, impersonate="chrome146", timeout=15)
                img_resp.raise_for_status()
            raw_ext = og_image_url.split("?")[0].rsplit(".", 1)[-1].lower()
            ext = raw_ext if raw_ext in ("jpg", "jpeg", "png", "webp", "gif") else "jpg"
            fname_ext = "jpg" if ext == "jpeg" else ext
            ct = img_resp.headers.get("content-type", f"image/{fname_ext}").split(";")[0].strip()
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.put(
                    f"{mealie_url.rstrip('/')}/api/recipes/{slug}/image",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"image": (f"image.{fname_ext}", img_resp.content, ct)},
                    data={"extension": fname_ext},
                )
            logger.debug("proxy.image_uploaded slug=%s og_image=%s", slug, og_image_url)
        except Exception as e:
            logger.debug("proxy.image_upload_failed slug=%s: %s", slug, e)

    # Tag the recipe with a comment so its origin is traceable in Mealie
    try:
        await mealie_post("/api/comments", {
            "recipeId": recipe_id,
            "text": "Imported via Mealie Planner's proxy.",
        })
    except Exception:
        logger.debug("proxy.comment_failed slug=%s", slug)

    return result


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

    return {"ok": True}


@router.post("/api/recipes/import-image")
async def import_recipe_from_image(
    request: Request,
    file: UploadFile = Form(...),
    translate: bool = Form(False),
):
    if not rate_limiter.check(request, key="recipe-create", max_hits=5):
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

    params: dict[str, str] = {}
    if translate:
        locale = get_locale(request)
        bcp47 = _LOCALE_TO_BCP47.get(locale)
        if bcp47:
            params["translateLanguage"] = bcp47

    ext = ""
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
    if not ext:
        ext = content_type.split("/")[-1]
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("jpg", "png", "webp", "gif"):
        ext = "jpg"

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            resp = await client.post(
                f"{mealie_url.rstrip('/')}/api/recipes/create/image",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                files={"images": (file.filename or f"image.{ext}", contents, content_type)},
            )
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, str) and body:
                slug = body
            elif isinstance(body, dict):
                slug = body.get("slug") or body.get("name")
                if not slug:
                    raise HTTPException(status_code=502, detail=f"Unexpected Mealie response: {body}")
            else:
                raise HTTPException(status_code=502, detail=f"Unexpected Mealie response: {body!r}")
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:200] if e.response.content else str(e)
            raise HTTPException(status_code=502, detail=f"Mealie {e.response.status_code}: {detail}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    result = await _finish_recipe_import(slug)
    recipe_id = result["id"]

    try:
        await mealie_post("/api/comments", {
            "recipeId": recipe_id,
            "text": "Imported via Mealie Planner's proxy.",
        })
    except Exception:
        logger.debug("image_import.comment_failed slug=%s", slug)

    return result


@router.get("/api/sparkle")
async def sparkle(date: str, meal_type: str = "dinner"):
    import random
    from datetime import timedelta

    try:
        anchor = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    last_week = anchor - timedelta(days=7)
    last_week_str = last_week.strftime("%Y-%m-%d")

    last_week_recipe = None
    try:
        data = await mealie_get(
            f"/api/households/mealplans?start_date={last_week_str}&end_date={last_week_str}&perPage=10"
        )
        items = data.get("items", []) if isinstance(data, dict) else data
        for item in items:
            if item.get("entryType", "dinner") == meal_type:
                last_week_recipe = normalize_meal_entry(item)
                break
    except HTTPException:
        pass

    all_recipes = await get_cached_recipes(limit=10000)
    if not all_recipes:
        raise HTTPException(
            status_code=404,
            detail="No recipes in cache. Trigger /api/cache/refresh first.",
        )

    pool = list(all_recipes)

    if last_week_recipe and last_week_recipe.get("recipe_id"):
        match = next(
            (r for r in all_recipes if r["id"] == last_week_recipe["recipe_id"]), None
        )
        if match:
            pool.append(match)

    return random.choice(pool)
