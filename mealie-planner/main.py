import asyncio
import hmac
import json
import logging
import os
import random
import re
import time
import uuid as _uuid_mod
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import aiosqlite
import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from urllib.parse import urlparse

logger = logging.getLogger("mealie_planner")


# Task manager
class _TaskManager:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(self._wrap(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _wrap(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task crashed")

    async def cancel_all(self):
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=5.0)
            for task in pending:
                logger.warning("Task %s did not stop in time", task)


_task_manager = _TaskManager()


# Rate limiter
class _RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self.max = max_requests
        self.window = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def _client_ip(self, request: Request) -> str:
        if not _DOCKER_MODE:  # HAOS ingress sets a trusted X-Forwarded-For
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(
        self, request: Request, *, key: str | None = None, max_hits: int | None = None
    ) -> bool:
        now = time.time()
        k = f"{self._client_ip(request)}:{key}" if key else self._client_ip(request)
        limit = max_hits if max_hits is not None else self.max

        bucket = self._buckets.setdefault(k, [])
        cutoff = now - self.window
        bucket[:] = [t for t in bucket if t > cutoff]

        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


_rate_limiter = _RateLimiter()

# Input validation helpers
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]{1,200}$')
_VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "side"}


def _require_uuid(val: str, name: str = "ID") -> None:
    if not _UUID_RE.match(val):
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def _require_slug(val: str, name: str = "slug") -> None:
    if not _SLUG_RE.match(val):
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def _require_int_id(val: str, name: str = "ID") -> None:
    if not val.isdigit() or int(val) <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def _require_date(val: str) -> None:
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD.")


# Security headers
_IMPECCABLE_LIVE_DEV = " http://localhost:8400" if os.environ.get("DEV_MODE") else ""
_CSP = (
    "default-src 'self'; "
    f"script-src 'self' 'unsafe-inline' 'unsafe-eval'{_IMPECCABLE_LIVE_DEV}; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    f"connect-src 'self'{_IMPECCABLE_LIVE_DEV}; "
    "frame-ancestors 'self';"
)


_IMPECCABLE_LIVE_SNIPPET = (
    b"\n<!-- impeccable-live-start -->"
    b'\n<script src="http://localhost:8400/live.js"></script>'
    b"\n<!-- impeccable-live-end -->\n"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = _CSP
        if _IMPECCABLE_LIVE_DEV and response.headers.get("content-type", "").startswith("text/html"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            body = body.replace(b"</body>", _IMPECCABLE_LIVE_SNIPPET + b"</body>", 1)
            response.headers["content-length"] = str(len(body))
            return StarletteResponse(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        return response


# Paths
DATA_PATH = "/data" if os.path.exists("/data") else "./data"
OPTIONS_FILE = os.path.join(DATA_PATH, "options.json")
CREDENTIALS_FILE = os.path.join(DATA_PATH, "credentials.json")
KEY_FILE = os.path.join(DATA_PATH, ".key")
CACHE_DB = os.path.join(DATA_PATH, "cache.db")
CACHE_TTL = 3600


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

_cred_cache: dict[str, str | None] = {"url": None, "token": None}
_cred_loaded_at: float = 0.0  # mtime watermark — cache invalidates when files are newer


def _cred_cache_valid() -> bool:
    if not (_cred_cache["url"] and _cred_cache["token"]):
        return False
    try:
        # Only watch credentials.json — options.json is touched by HAOS on every
        # update/restart even when values haven't changed, so using it as a cache
        # invalidation signal causes false misses after every add-on update.
        if os.path.exists(CREDENTIALS_FILE) and os.path.getmtime(CREDENTIALS_FILE) > _cred_loaded_at:
            return False
    except OSError:
        pass
    return True


def get_mode() -> str:
    return "docker" if _DOCKER_MODE else "haos"


def get_credentials() -> tuple[str | None, str | None]:
    global _cred_loaded_at

    if _cred_cache_valid():
        return _cred_cache["url"], _cred_cache["token"]

    # Clear stale values before re-reading
    _cred_cache["url"] = None
    _cred_cache["token"] = None

    if _DOCKER_MODE:
        _cred_cache["url"] = os.environ.get("MEALIE_API_URL")
        _cred_cache["token"] = os.environ.get("MEALIE_API_KEY")
        return _cred_cache["url"], _cred_cache["token"]

    # credentials.json is always the primary store (written by the app after first setup).
    # options.json is a bootstrap source — used only when credentials.json is absent
    # or when options.json contains different, non-empty values (user changed HAOS config).
    # HAOS rewrites options.json on every update/restart even with unchanged values,
    # so mtime comparison is unreliable for detecting intentional config changes.

    stored_url: str | None = None
    stored_plain: str | None = None

    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE) as f:
                creds = json.load(f)
            url = creds.get("mealie_url") or ""
            token_enc = creds.get("api_token") or ""
            if url and token_enc:
                stored_url = url
                stored_plain = decrypt_token(token_enc)
        except Exception:
            pass

    # Check options.json — if it carries different non-empty credentials the user
    # intentionally updated the HAOS add-on config; import them into credentials.json.
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE) as f:
                opts = json.load(f)
            opts_url = (opts.get("mealie_url") or "").strip()
            opts_raw = (opts.get("api_token") or "").strip()
            if opts_url and opts_raw:
                opts_plain = opts_raw if not _looks_encrypted(opts_raw) else decrypt_token(opts_raw)
                if opts_url != stored_url or opts_plain != stored_plain:
                    encrypted = encrypt_token(opts_plain) if not _looks_encrypted(opts_raw) else opts_raw
                    _write_credentials(opts_url, encrypted)
                    return opts_url, opts_plain
        except Exception:
            pass

    if stored_url and stored_plain:
        _cred_cache["url"] = stored_url
        _cred_cache["token"] = stored_plain
        try:
            _cred_loaded_at = os.path.getmtime(CREDENTIALS_FILE)
        except OSError:
            pass
        return stored_url, stored_plain

    return None, None


def _write_credentials(url: str, encrypted_token: str) -> None:
    global _cred_loaded_at
    os.makedirs(DATA_PATH, exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump({"mealie_url": url, "api_token": encrypted_token}, f)
    _cred_cache["url"] = url
    _cred_cache["token"] = decrypt_token(encrypted_token)
    try:
        _cred_loaded_at = os.path.getmtime(CREDENTIALS_FILE)
    except OSError:
        pass


# SQLite
_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(CACHE_DB)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout=5000")
        await _db.execute("PRAGMA synchronous=NORMAL")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def init_db() -> None:
    db = await get_db()
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
    db = await get_db()
    async with db.execute(
        "SELECT value FROM cache_meta WHERE key='recipes_last_refreshed'"
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def _get_cached_recipes(query: str | None = None) -> list[dict]:
    db = await get_db()
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
            "id": r[0],
            "slug": r[1],
            "name": r[2],
            "image_url": f"/api/media/{r[0]}",
            "description": r[3] or "",
            "tags": json.loads(r[4]) if r[4] else [],
        }
        for r in rows
    ]


# Cache refresh
_refresh_lock = asyncio.Lock()
_refresh_in_progress = False
_last_poll_at: int = 0
_POLL_COOLDOWN_S: int = 30


async def refresh_recipe_cache() -> int:
    global _refresh_in_progress
    if _refresh_in_progress:
        logger.info("Cache refresh already in progress, skipping")
        return 0

    _refresh_in_progress = True
    try:
        url, token = get_credentials()
        if not url or not token:
            return 0

        page = 1
        total = 0
        base_url = url.rstrip("/")

        db = await get_db()
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
                for r in items:
                    description = r.get("description") or ""
                    tags = json.dumps(
                        [
                            t.get("name")
                            for t in (r.get("tags") or [])
                            if isinstance(t, dict)
                        ]
                    )
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

        await db.execute(
            "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('recipes_last_refreshed', ?)",
            (str(int(time.time())),),
        )
        await db.commit()
        return total
    finally:
        _refresh_in_progress = False


async def warm_cache_if_needed() -> None:
    last = await _cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        await refresh_recipe_cache()


async def ensure_cache_fresh() -> None:
    last = await _cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        if not _refresh_in_progress:
            _task_manager.spawn(refresh_recipe_cache())


# HTTP client
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


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

    _client = client or await get_http_client()
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


async def mealie_get(path: str) -> dict | list:
    return await _mealie_request("GET", path)


async def mealie_post(path: str, body: dict) -> dict | list:
    return await _mealie_request("POST", path, body)


async def mealie_delete(path: str) -> None:
    await _mealie_request("DELETE", path)


# App
@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_PATH, exist_ok=True)
    _get_or_create_key()
    await init_db()
    _task_manager.spawn(warm_cache_if_needed())
    yield
    try:
        await _task_manager.cancel_all()
    except Exception:
        logger.exception("Error cancelling background tasks")
    try:
        await close_http_client()
    except Exception:
        logger.exception("Error closing HTTP client")
    try:
        await close_db()
    except Exception:
        logger.exception("Error closing database")


app = FastAPI(title="Mealie Planner", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
templates = Jinja2Templates(directory="templates")


# Session
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
        request.state.ingress_path = request.headers.get("X-Ingress-Path", "").rstrip(
            "/"
        )

        if _REQUIRE_AUTH:
            path = request.url.path.rstrip("/")
            if not path.startswith("/assets/"):
                if path not in _EXEMPT_PATHS:
                    # Supervisor already authenticated the user; trust the injected header.
                    if not _DOCKER_MODE and request.headers.get("X-Remote-User-Id"):
                        pass
                    else:
                        token = request.cookies.get(_SESSION_COOKIE)
                        if not token or not _verify_session_token(token):
                            return RedirectResponse(
                                url=f"/auth?from={_safe_redirect_path(request.url.path)}",
                                status_code=302,
                            )

        return await call_next(request)


app.add_middleware(IngressAndAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


# Routes
@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"ingress_path": request.state.ingress_path},
    )


def _safe_redirect_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        return "/"
    return path


@app.get("/auth")
async def auth_page(request: Request, from_: str = Query("/", alias="from")):
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "ingress_path": request.state.ingress_path,
            "from": _safe_redirect_path(from_),
        },
    )


class PinPayload(BaseModel):
    pin: str


@app.post("/api/auth/verify")
async def verify_pin(payload: PinPayload, request: Request, response: Response):
    if not _REQUIRE_AUTH:
        raise HTTPException(status_code=400, detail="PIN authentication not configured")

    if not _rate_limiter.check(request, key="verify", max_hits=5):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Try again later."
        )

    if not hmac.compare_digest(payload.pin, _PIN_CODE):
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    _secure = request.url.scheme == "https"
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=_create_session_token(),
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_secure,
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
    api_token: str = ""

    @field_validator("mealie_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("URL must be a valid http or https address.")
        return v


@app.post("/api/config")
async def save_config(payload: ConfigPayload, request: Request):
    if _DOCKER_MODE:
        raise HTTPException(
            status_code=400,
            detail="Running in Docker mode — configure via environment variables.",
        )

    if not _rate_limiter.check(request, key="config", max_hits=10):
        raise HTTPException(status_code=429, detail="Too many requests.")

    token_to_use = payload.api_token
    if not token_to_use:
        _, existing = get_credentials()
        if not existing:
            raise HTTPException(
                status_code=422, detail="API token is required for initial setup."
            )
        token_to_use = existing

    client = await get_http_client()
    headers = {"Authorization": f"Bearer {token_to_use}"}
    try:
        resp = await client.get(
            f"{payload.mealie_url.rstrip('/')}/api/app/about", headers=headers
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
        raise HTTPException(
            status_code=422, detail="Could not reach Mealie at the provided URL."
        )

    _write_credentials(payload.mealie_url, encrypt_token(token_to_use))
    _task_manager.spawn(refresh_recipe_cache())
    return {"ok": True}


@app.get("/api/recipes")
async def get_recipes(q: str | None = None):
    await ensure_cache_fresh()
    return await _get_cached_recipes(q)


@app.get("/api/recipes/poll")
async def poll_recipe_changes():
    """One cheap Mealie call to detect new/updated recipes since last cache refresh."""
    global _last_poll_at, _refresh_in_progress
    now = int(time.time())
    if now - _last_poll_at < _POLL_COOLDOWN_S:
        return {"stale": False}
    _last_poll_at = now

    try:
        url, token = get_credentials()
        if not url or not token:
            return {"stale": False}

        last_refreshed = await _cache_last_refreshed()
        if last_refreshed is None:
            return {"stale": True}

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{url.rstrip('/')}/api/recipes",
                params={"page": 1, "perPage": 1, "orderBy": "dateUpdated", "orderDirection": "desc"},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])

        if not items:
            return {"stale": False}

        date_updated = items[0].get("dateUpdated") or ""
        if not date_updated:
            return {"stale": False}

        dt = datetime.fromisoformat(date_updated.replace("Z", "+00:00"))
        recipe_ts = int(dt.timestamp())
        stale = recipe_ts > last_refreshed
        if stale and not _refresh_in_progress:
            _task_manager.spawn(refresh_recipe_cache())
        return {"stale": stale}
    except Exception:
        return {"stale": False}


@app.post("/api/cache/refresh")
async def force_cache_refresh(request: Request):
    if not _rate_limiter.check(request, key="refresh", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")
    count = await refresh_recipe_cache()
    return {"count": count, "refreshed_at": datetime.utcnow().isoformat()}


@app.get("/api/media/{recipe_id}")
async def proxy_recipe_image(recipe_id: str):
    _require_uuid(recipe_id, "recipe ID")
    url, token = get_credentials()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Mealie not configured")

    full_url = (
        f"{url.rstrip('/')}/api/media/recipes/{recipe_id}/images/min-original.webp"
    )
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


@app.get("/api/recipe-link/{slug}")
async def recipe_link(slug: str):
    _require_slug(slug, "recipe slug")
    url, _ = get_credentials()
    if not url:
        raise HTTPException(status_code=400, detail="Mealie not configured")
    return RedirectResponse(url=f"{url.rstrip('/')}/g/home/r/{slug}")


@app.get("/api/recipes/{slug}")
async def get_recipe(slug: str):
    _require_slug(slug, "recipe slug")
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
    _require_date(start_date)
    _require_date(end_date)
    data = await mealie_get(
        f"/api/households/mealplans?start_date={start_date}&end_date={end_date}&perPage=50"
    )
    items = data.get("items", []) if isinstance(data, dict) else data
    return [_normalize_entry(e) for e in items]


class MealPlanEntry(BaseModel):
    date: str
    meal_type: str = "dinner"
    recipe_id: str

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Use YYYY-MM-DD format.")
        return v

    @field_validator("meal_type")
    @classmethod
    def _check_meal_type(cls, v: str) -> str:
        if v not in _VALID_MEAL_TYPES:
            raise ValueError("Must be breakfast, lunch, dinner, or side.")
        return v

    @field_validator("recipe_id")
    @classmethod
    def _check_recipe_id(cls, v: str) -> str:
        try:
            _uuid_mod.UUID(v)
        except ValueError:
            raise ValueError("Invalid recipe ID.")
        return v


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
    _require_int_id(entry_id, "entry ID")
    if not _rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    await mealie_delete(f"/api/households/mealplans/{entry_id}")
    return Response(status_code=204)


@app.get("/api/recipe-actions/raw")
async def get_recipe_actions_raw():
    results = {}
    for path in ["/api/groups/recipe-actions", "/api/households/recipe-actions"]:
        try:
            results[path] = await mealie_get(f"{path}?perPage=10")
        except HTTPException as e:
            results[path] = {"error": e.detail}
    return results


@app.get("/api/recipe-actions")
async def get_recipe_actions():
    for path in ["/api/groups/recipe-actions", "/api/households/recipe-actions"]:
        try:
            data = await mealie_get(f"{path}?perPage=100")
            items: list = (
                data.get("items", []) if isinstance(data, dict) else (data or [])
            )
            return [
                {
                    "id": item.get("id", ""),
                    "name": item.get("title")
                    or item.get("name")
                    or item.get("label")
                    or "Action",
                    "action_type": item.get("actionType")
                    or item.get("action_type")
                    or "link",
                }
                for item in items
                if isinstance(item, dict) and item.get("id")
            ]
        except HTTPException:
            continue
    return []


class RecipeActionTrigger(BaseModel):
    recipe_slug: str

    @field_validator("recipe_slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("Invalid recipe slug.")
        return v


@app.post("/api/recipe-actions/{action_id}/trigger")
async def trigger_recipe_action(
    action_id: str, payload: RecipeActionTrigger, request: Request
):
    _require_uuid(action_id, "action ID")
    if not _rate_limiter.check(request, key="recipe-action", max_hits=20):
        raise HTTPException(status_code=429, detail="Too many requests.")

    action: dict | None = None
    for path in [
        f"/api/households/recipe-actions/{action_id}",
        f"/api/groups/recipe-actions/{action_id}",
    ]:
        try:
            raw = await mealie_get(path)
            if isinstance(raw, dict):
                action = raw
                break
        except HTTPException:
            continue
    if not action:
        raise HTTPException(status_code=404, detail="Recipe action not found.")

    action_type: str = action.get("actionType") or action.get("action_type") or "post"

    if action_type == "link":
        action_url: str = action.get("url") or ""
        raw_recipe = await mealie_get(f"/api/recipes/{payload.recipe_slug}")
        recipe: dict = raw_recipe if isinstance(raw_recipe, dict) else {}
        final_url = (
            action_url.replace("{slug}", recipe.get("slug", ""))
            .replace("{recipeSlug}", recipe.get("slug", ""))
            .replace("{id}", recipe.get("id", ""))
            .replace("{recipeId}", recipe.get("id", ""))
        )
        return {"type": "link", "url": final_url}

    for path in [
        f"/api/households/recipe-actions/{action_id}/trigger/{payload.recipe_slug}",
        f"/api/groups/recipe-actions/{action_id}/trigger/{payload.recipe_slug}",
    ]:
        try:
            await mealie_post(path, {})
            return {"type": "post", "ok": True}
        except HTTPException as e:
            if e.status_code == 404:
                continue
            raise
    raise HTTPException(status_code=502, detail="Mealie trigger endpoint not found.")


@app.get("/api/sparkle")
async def sparkle(date: str, meal_type: str = "dinner"):
    try:
        anchor = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Invalid date format. Use YYYY-MM-DD."
        )

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
                last_week_recipe = _normalize_entry(item)
                break
    except HTTPException:
        pass

    all_recipes = await _get_cached_recipes()
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
