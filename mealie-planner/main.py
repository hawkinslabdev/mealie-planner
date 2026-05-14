import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import aiosqlite
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from urllib.parse import urlparse

# In-memory rate limiter
class _RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self.max = max_requests
        self.window = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request, *, key: str | None = None, max_hits: int | None = None) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        k = f"{self._client_ip(request)}:{key}" if key else self._client_ip(request)
        limit = max_hits if max_hits is not None else self.max

        bucket = self._buckets.setdefault(k, [])
        # Prune expired entries
        cutoff = now - self.window
        bucket[:] = [t for t in bucket if t > cutoff]

        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


_rate_limiter = _RateLimiter()

# Paths
DATA_PATH = "/app/data" if os.path.exists("/app") else "./data"
OPTIONS_FILE = os.path.join(DATA_PATH, "options.json")
CREDENTIALS_FILE = os.path.join(DATA_PATH, "credentials.json")
KEY_FILE = os.path.join(DATA_PATH, ".key")
CACHE_DB = os.path.join(DATA_PATH, "cache.db")

CACHE_TTL = 3600  # seconds before recipe cache is considered stale

# Encryption
def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


def _cipher() -> Fernet:
    return Fernet(_get_or_create_key())


def encrypt_token(raw: str) -> str:
    return _cipher().encrypt(raw.encode()).decode()


def decrypt_token(enc: str) -> str:
    return _cipher().decrypt(enc.encode()).decode()


def _looks_encrypted(value: str) -> bool:
    return value.startswith("gAAAAA")

# Config bridge
_DOCKER_MODE = bool(os.environ.get("MEALIE_API_URL"))
_PIN_CODE = os.environ.get("PIN_CODE", "")
_REQUIRE_AUTH = bool(_PIN_CODE)
_SESSION_COOKIE = "mp_session"
_SESSION_TTL = 86400 * 30


def get_mode() -> str:
    return "docker" if _DOCKER_MODE else "haos"

def get_credentials() -> tuple[str | None, str | None]:
    if _DOCKER_MODE:
        return os.environ.get("MEALIE_API_URL"), os.environ.get("MEALIE_API_KEY")

    # HAOS: prefer credentials.json
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE) as f:
            creds = json.load(f)
        url = creds.get("mealie_url")
        token_enc = creds.get("api_token")
        if url and token_enc:
            try:
                return url, decrypt_token(token_enc)
            except (InvalidToken, Exception):
                pass

    # Fall back to Supervisor-written options.json
    if os.path.exists(OPTIONS_FILE):
        with open(OPTIONS_FILE) as f:
            opts = json.load(f)
        url = opts.get("mealie_url")
        raw_token = opts.get("api_token")
        if url and raw_token:
            # Migrate to encrypted credentials.json on first read
            encrypted = encrypt_token(raw_token) if not _looks_encrypted(raw_token) else raw_token
            plain = raw_token if not _looks_encrypted(raw_token) else decrypt_token(raw_token)
            _write_credentials(url, encrypted)
            return url, plain

    return None, None


def _write_credentials(url: str, encrypted_token: str) -> None:
    os.makedirs(DATA_PATH, exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump({"mealie_url": url, "api_token": encrypted_token}, f)

# SQLite cached
async def init_db() -> None:
    async with aiosqlite.connect(CACHE_DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id          TEXT PRIMARY KEY,
                slug        TEXT NOT NULL UNIQUE,
                name        TEXT NOT NULL,
                description TEXT DEFAULT '',
                tags        TEXT DEFAULT '[]',
                image_url   TEXT,
                cached_at   INTEGER NOT NULL
            )
        """)
        # migrate older schemas that lack the new columns
        for col in ("description", "tags"):
            try:
                await db.execute(f"ALTER TABLE recipes ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_recipes_name ON recipes(name)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cache_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await db.commit()


async def _cache_last_refreshed() -> int | None:
    async with aiosqlite.connect(CACHE_DB) as db:
        async with db.execute(
            "SELECT value FROM cache_meta WHERE key='recipes_last_refreshed'"
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def _get_cached_recipes(query: str | None = None) -> list[dict]:
    async with aiosqlite.connect(CACHE_DB) as db:
        if query:
            q = f"%{query.lower()}%"
            async with db.execute(
                "SELECT id, slug, name, description, tags FROM recipes WHERE lower(name) LIKE ? ORDER BY name",
                (q,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, slug, name, description, tags FROM recipes ORDER BY name"
            ) as cur:
                rows = await cur.fetchall()
    return [
        {
            "id": r[0], "slug": r[1], "name": r[2],
            "image_url": f"/api/media/{r[0]}", "description": r[3] or "",
            "tags": json.loads(r[4]) if r[4] else [],
        }
        for r in rows
    ]


async def refresh_recipe_cache() -> int:
    url, token = get_credentials()
    if not url or not token:
        return 0

    page = 1
    total = 0
    base_url = url.rstrip("/")

    async with httpx.AsyncClient(timeout=15.0) as client:
        headers = {"Authorization": f"Bearer {token}"}
        while True:
            try:
                resp = await client.get(
                    f"{base_url}/api/recipes",
                    params={"page": page, "perPage": 100},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError:
                break

            items = data.get("items", [])
            if not items:
                break

            now = int(time.time())
            async with aiosqlite.connect(CACHE_DB) as db:
                for r in items:
                    description = r.get("description") or ""
                    tags = json.dumps([t.get("name") for t in (r.get("tags") or []) if isinstance(t, dict)])
                    await db.execute(
                        """INSERT INTO recipes (id, slug, name, description, tags, image_url, cached_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET
                             slug=excluded.slug, name=excluded.name,
                             description=excluded.description, tags=excluded.tags,
                             image_url=excluded.image_url, cached_at=excluded.cached_at""",
                        (r["id"], r["slug"], r["name"], description, tags, "", now),
                    )
                await db.commit()

            total += len(items)
            if len(items) < 100:
                break
            page += 1

    async with aiosqlite.connect(CACHE_DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('recipes_last_refreshed', ?)",
            (str(int(time.time())),),
        )
        await db.commit()

    return total


async def warm_cache_if_needed() -> None:
    last = await _cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        await refresh_recipe_cache()

# Mealie HTTP helpers
async def _mealie_request(
    method: str,
    path: str,
    body: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict | list | None:
    url, token = get_credentials()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    headers = {"Authorization": f"Bearer {token}"}
    full_url = f"{url.rstrip('/')}{path}"

    _client = client or httpx.AsyncClient(timeout=10.0)
    try:
        if method == "GET":
            resp = await _client.get(full_url, headers=headers)
        elif method == "POST":
            resp = await _client.post(full_url, json=body, headers=headers)
        elif method == "DELETE":
            resp = await _client.delete(full_url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=str(e))
    finally:
        if client is None:
            await _client.aclose()


async def mealie_get(path: str) -> dict | list:
    return await _mealie_request("GET", path)


async def mealie_post(path: str, body: dict) -> dict | list:
    return await _mealie_request("POST", path, body)


async def mealie_delete(path: str) -> None:
    await _mealie_request("DELETE", path)

# FastAPI app
@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_PATH, exist_ok=True)
    _get_or_create_key()
    await init_db()
    asyncio.create_task(warm_cache_if_needed())
    yield

app = FastAPI(title="Mealie Quick Planner", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
templates = Jinja2Templates(directory="templates")

# Session helpers (e.g. pin)
def _create_session_token() -> str:
    return encrypt_token(json.dumps({"t": int(time.time()) + _SESSION_TTL}))


def _verify_session_token(token: str) -> bool:
    try:
        data = json.loads(decrypt_token(token))
        return data.get("t", 0) > int(time.time())
    except Exception:
        return False

_EXEMPT_PATHS = {"/auth", "/api/auth/verify", "/favicon.ico"}

class IngressAndAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Always set ingress path
        request.state.ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")

        # PIN check
        if _REQUIRE_AUTH:
            path = request.url.path.rstrip("/")
            if not path.startswith("/assets/"):
                if path not in _EXEMPT_PATHS:
                    token = request.cookies.get(_SESSION_COOKIE)
                    if not token or not _verify_session_token(token):
                        return RedirectResponse(
                            url=f"/auth?from={_safe_redirect_path(request.url.path)}",
                            status_code=302,
                        )

        return await call_next(request)

app.add_middleware(IngressAndAuthMiddleware)

# Routes
@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "ingress_path": request.state.ingress_path},
    )

def _safe_redirect_path(path: str) -> str:
    """Only allow relative paths starting with /. Reject URLs with hosts."""
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        return "/"
    return path

@app.get("/auth")
async def auth_page(request: Request, from_: str = Query("/", alias="from")):
    return templates.TemplateResponse(
        "auth.html",
        {"request": request, "ingress_path": request.state.ingress_path, "from": _safe_redirect_path(from_)},
    )

class PinPayload(BaseModel):
    pin: str

@app.post("/api/auth/verify")
async def verify_pin(payload: PinPayload, request: Request, response: Response):
    if not _REQUIRE_AUTH:
        raise HTTPException(status_code=400, detail="PIN authentication not configured")

    if not _rate_limiter.check(request, key="verify", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    if payload.pin != _PIN_CODE:
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    response.set_cookie(
        key=_SESSION_COOKIE,
        value=_create_session_token(),
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}

@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key=_SESSION_COOKIE, path="/")
    return {"ok": True}

@app.get("/api/status")
async def get_status():
    url, token = get_credentials()
    configured = bool(url and token)
    reachable = False
    version = None

    if configured:
        try:
            data = await mealie_get("/api/app/about")
            reachable = True
            version = data.get("version")
        except HTTPException:
            pass

    return {
        "configured": configured,
        "mode": get_mode(),
        "mealie_reachable": reachable,
        "version": version,
    }


@app.get("/api/config")
async def get_config():
    url, _ = get_credentials()
    return {"mealie_url": url or "", "mode": get_mode()}


class ConfigPayload(BaseModel):
    mealie_url: str
    api_token: str = ""  # empty = keep existing token


@app.post("/api/config")
async def save_config(payload: ConfigPayload, request: Request):
    if _DOCKER_MODE:
        raise HTTPException(
            status_code=400,
            detail="Running in Docker mode — configure via environment variables.",
        )

    if not _rate_limiter.check(request, key="config", max_hits=10):
        raise HTTPException(status_code=429, detail="Too many requests.")

    # If no token supplied, reuse the stored one
    token_to_use = payload.api_token
    if not token_to_use:
        _, existing = get_credentials()
        if not existing:
            raise HTTPException(status_code=422, detail="API token is required for initial setup.")
        token_to_use = existing

    # Validate against Mealie before saving
    headers = {"Authorization": f"Bearer {token_to_use}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{payload.mealie_url.rstrip('/')}/api/app/about", headers=headers
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = "Invalid API token." if e.response.status_code == 401 else f"Mealie error: {e.response.status_code}"
        raise HTTPException(status_code=422, detail=detail)
    except httpx.HTTPError:
        raise HTTPException(status_code=422, detail="Could not reach Mealie at the provided URL.")

    _write_credentials(payload.mealie_url, encrypt_token(token_to_use))
    asyncio.create_task(refresh_recipe_cache())
    return {"ok": True}

@app.get("/api/recipes")
async def get_recipes(q: str | None = None):
    last = await _cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        asyncio.create_task(refresh_recipe_cache())
    return await _get_cached_recipes(q)

@app.post("/api/cache/refresh")
async def force_cache_refresh(request: Request):
    if not _rate_limiter.check(request, key="refresh", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")
    count = await refresh_recipe_cache()
    return {"count": count, "refreshed_at": datetime.utcnow().isoformat()}

@app.get("/api/media/{recipe_id}")
async def proxy_recipe_image(recipe_id: str):
    url, token = get_credentials()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    full_url = f"{url.rstrip('/')}/api/media/recipes/{recipe_id}/images/min-original.webp"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=10.0) as client:
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

@app.get("/api/recipe-link/{slug}")
async def recipe_link(slug: str):
    url, _ = get_credentials()
    if not url:
        raise HTTPException(status_code=400, detail="Mealie not configured")
    # slug is a single path segment — FastAPI won't route slashes here
    return RedirectResponse(url=f"{url.rstrip('/')}/g/home/r/{slug}")


@app.get("/api/recipes/{slug}")
async def get_recipe(slug: str):
    data = await mealie_get(f"/api/recipes/{slug}")
    return {
        "id": data.get("id"),
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description"),
        "image_url": f"/api/media/{data['id']}" if data.get("id") else None,
    }

def _normalize_entry(entry: dict) -> dict:
    recipe = entry.get("recipe") or {}
    recipe_id = recipe.get("id") or entry.get("recipeId")
    recipe_slug = recipe.get("slug")
    recipe_name = recipe.get("name")
    return {
        "id": entry.get("id"),
        "date": entry.get("date"),
        "meal_type": entry.get("entryType", "dinner"),
        "recipe_id": recipe_id,
        "recipe_slug": recipe_slug,
        "recipe_name": recipe_name,
        "image_url": f"/api/media/{recipe_id}" if recipe_id else None,
    }

@app.get("/api/mealplan")
async def get_mealplan(start_date: str, end_date: str):
    data = await mealie_get(
        f"/api/households/mealplans?start_date={start_date}&end_date={end_date}&perPage=50"
    )
    items = data.get("items", []) if isinstance(data, dict) else data
    return [_normalize_entry(e) for e in items]

class MealPlanEntry(BaseModel):
    date: str
    meal_type: str = "dinner"
    recipe_id: str

@app.post("/api/mealplan")
async def create_mealplan_entry(entry: MealPlanEntry, request: Request):
    if not _rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    result = await mealie_post(
        "/api/households/mealplans",
        {
            "date": entry.date,
            "entryType": entry.meal_type,
            "recipeId": entry.recipe_id,
        },
    )
    return _normalize_entry(result)

@app.delete("/api/mealplan/{entry_id}")
async def delete_mealplan_entry(entry_id: str, request: Request):
    if not _rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    await mealie_delete(f"/api/households/mealplans/{entry_id}")
    return Response(status_code=204)

@app.get("/api/sparkle")
async def sparkle(date: str, meal_type: str = "dinner"):
    # Compute same weekday last week
    try:
        anchor = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    last_week = anchor - timedelta(days=7)
    last_week_str = last_week.strftime("%Y-%m-%d")

    # Check if a recipe was planned that day/slot last week
    last_week_recipe = None
    try:
        data = await mealie_get(
            f"/api/households/mealplans?start_date={last_week_str}&end_date={last_week_str}&perPage=10"
        )
        items = data.get("items", []) if isinstance(data, dict) else data
        for item in items:
            if item.get("entryType", "dinner") == meal_type:
                last_week_recipe = _normalize_entry(item)
                break
    except HTTPException:
        pass

    # Build candidate pool from cache
    all_recipes = await _get_cached_recipes()
    if not all_recipes:
        raise HTTPException(status_code=404, detail="No recipes in cache. Trigger /api/cache/refresh first.")

    pool = list(all_recipes)

    # Weight last week's recipe 2x
    if last_week_recipe and last_week_recipe.get("recipe_id"):
        match = next(
            (r for r in all_recipes if r["id"] == last_week_recipe["recipe_id"]), None
        )
        if match:
            pool.append(match)

    return random.choice(pool)