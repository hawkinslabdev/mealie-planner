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
from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
from urllib.parse import urlparse

logger = logging.getLogger("mealie_planner")
logger.setLevel(logging.DEBUG if os.environ.get("DEV_MODE") else logging.INFO)


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


_MAX_JSON_BODY_BYTES = 64 * 1024  # 64 KB; image uploads have their own 10 MB check


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "application/json" in request.headers.get("content-type", ""):
            cl = request.headers.get("content-length")
            if cl and int(cl) > _MAX_JSON_BODY_BYTES:
                return StarletteResponse(
                    content='{"detail":"Request body too large"}',
                    status_code=413,
                    media_type="application/json",
                )
        return await call_next(request)


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
_SUPPORTED_LOCALES = {"en", "de", "nl", "es", "fr", "it", "pl"}
_LOCALE_OVERRIDE = os.environ.get("LOCALE", "").strip().lower()
if _LOCALE_OVERRIDE not in _SUPPORTED_LOCALES:
    _LOCALE_OVERRIDE = ""
_LOCALE_DIR = os.path.join(os.path.dirname(__file__), "assets", "locales")
_locale_cache: dict[str, dict] = {}

_cred_cache: dict[str, str | None] = {"url": None, "token": None}
_cred_loaded_at: float = 0.0  # mtime watermark, cache invalidates when files are newer


def _cred_cache_valid() -> bool:
    if not (_cred_cache["url"] and _cred_cache["token"]):
        return False
    try:
        # Only watch credentials.json. options.json is touched by HAOS on every update/restart even when values haven't changed, so using it as a cache invalidation signal causes false misses after every add-on update.
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

    # credentials.json is the primary store, while options.json is a bootstrap fallback (used only if missing or modified), but HAOS rewrites it on every update, making mtime checks unreliable.
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

    # If options.json contains different non-empty credentials, the user intentionally updated the HAOS add-on config—import them into credentials.json.
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
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
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


async def _get_cached_recipes(
    query: str | None = None, limit: int = 500, offset: int = 0
) -> list[dict]:
    db = await get_db()
    if query:
        q = f"%{query.lower()}%"
        async with db.execute(
            "SELECT id, slug, name, description, tags FROM recipes WHERE lower(name) LIKE ? ORDER BY name LIMIT ? OFFSET ?",
            (q, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with db.execute(
            "SELECT id, slug, name, description, tags FROM recipes ORDER BY name LIMIT ? OFFSET ?",
            (limit, offset),
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


async def _upsert_recipe_cache(recipe_data: dict) -> None:
    recipe_id = recipe_data.get("id")
    slug = recipe_data.get("slug")
    name = recipe_data.get("name")
    if not all([recipe_id, slug, name]):
        return
    description = recipe_data.get("description") or ""
    tags = json.dumps(
        [t.get("name") for t in (recipe_data.get("tags") or []) if isinstance(t, dict)]
    )
    db = await get_db()
    await db.execute(
        """INSERT INTO recipes (id, slug, name, description, tags, image_url, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             slug=excluded.slug, name=excluded.name,
             description=excluded.description, tags=excluded.tags,
             image_url=excluded.image_url, cached_at=excluded.cached_at""",
        (recipe_id, slug, name, description, tags, "", int(time.time())),
    )
    await db.commit()


# Cache refresh
_refresh_in_progress = False
_last_poll_at: int = 0
_POLL_COOLDOWN_S: int = 30

# Status cache
_status_cache: dict = {}
_status_cached_at: float = 0.0
_STATUS_TTL: int = 30


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
        seen_ids: set[str] = set()
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
                    seen_ids.add(r["id"])
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

        # Purge recipes deleted from Mealie; chunked to stay under SQLite variable limit
        if seen_ids:
            all_cached = [
                row[0]
                async for row in await db.execute("SELECT id FROM recipes")
            ]
            to_delete = [rid for rid in all_cached if rid not in seen_ids]
            chunk = 200
            for i in range(0, len(to_delete), chunk):
                batch = to_delete[i : i + chunk]
                placeholders = ",".join("?" * len(batch))
                await db.execute(
                    f"DELETE FROM recipes WHERE id IN ({placeholders})", batch
                )
            if to_delete:
                await db.commit()

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
                            ip = request.client.host if request.client else "unknown"
                            logger.warning("access.denied path=%s ip=%s", path, ip)
                            return RedirectResponse(
                                url=f"/auth?from={_safe_redirect_path(request.url.path)}",
                                status_code=302,
                            )

        return await call_next(request)


app.add_middleware(IngressAndAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxBodySizeMiddleware)


# Routes
@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")


@app.get("/")
async def index(request: Request):
    locale = _get_locale(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "ingress_path": request.state.ingress_path,
            "locale": locale,
            "translations": _load_locale_json(locale),
        },
    )


def _safe_redirect_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        return "/"
    return path


@app.get("/auth")
async def auth_page(request: Request, from_: str = Query("/", alias="from")):
    locale = _get_locale(request)
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "ingress_path": request.state.ingress_path,
            "from": _safe_redirect_path(from_),
            "locale": locale,
            "translations": _load_locale_json(locale),
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

    ip = _rate_limiter._client_ip(request)
    if not hmac.compare_digest(payload.pin, _PIN_CODE):
        logger.warning("auth.pin_failed ip=%s", ip)
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    logger.info("auth.pin_success ip=%s", ip)
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


def _load_locale_json(lang: str) -> dict:
    if lang not in _locale_cache:
        path = os.path.join(_LOCALE_DIR, f"{lang}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _locale_cache[lang] = json.load(f)
        except Exception:
            _locale_cache[lang] = {}
    return _locale_cache[lang]


def _detect_accept_language(accept_language: str) -> str | None:
    for part in accept_language.split(","):
        lang = part.split(";")[0].strip().split("-")[0].lower()
        if lang in _SUPPORTED_LOCALES:
            return lang
    return None


def _get_locale(request: Request) -> str:
    """Priority: cookie > LOCALE env var > Accept-Language header > 'en'."""
    cookie = request.cookies.get("mp_locale", "").strip().lower()
    if cookie in _SUPPORTED_LOCALES:
        return cookie
    if _LOCALE_OVERRIDE:
        return _LOCALE_OVERRIDE
    detected = _detect_accept_language(request.headers.get("accept-language", ""))
    return detected or "en"


@app.get("/api/status")
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
            detail="Configure via environment variables.",
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
        # SSRF: scheme is validated by ConfigPayload; follow_redirects=False prevents
        # chained redirects to internal hosts. IP-range blocking is impractical here —
        # the Mealie URL is a user-configured, trusted internal address in this add-on.
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
        raise HTTPException(
            status_code=422, detail="Could not reach Mealie at the provided URL."
        )

    _write_credentials(payload.mealie_url, encrypt_token(token_to_use))
    ip = request.client.host if request.client else "unknown"
    logger.info("config.updated url=%s ip=%s", payload.mealie_url, ip)
    global _status_cached_at
    _status_cached_at = 0.0  # force status re-check after credential change
    _task_manager.spawn(refresh_recipe_cache())
    return {"ok": True}


_SETTINGS_DEFAULTS: dict[str, object] = {
    "show_quick_add": True,
}
_ALLOWED_SETTINGS: set[str] = set(_SETTINGS_DEFAULTS)


@app.get("/api/settings")
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


class SettingsPatch(BaseModel):
    show_quick_add: bool | None = None


@app.patch("/api/settings")
async def update_settings(payload: SettingsPatch, request: Request):
    if not _rate_limiter.check(request, key="settings", max_hits=30):
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


@app.get("/api/recipes")
async def get_recipes(
    q: str | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    await ensure_cache_fresh()
    return await _get_cached_recipes(q, limit=limit, offset=offset)


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
        db = await get_db()
        cur = await db.execute("SELECT COUNT(*) FROM recipes")
        row = await cur.fetchone()
        total_count = row[0] if row else 0
        return {"stale": stale, "total_count": total_count}
    except Exception:
        return {"stale": False}


@app.post("/api/cache/refresh")
async def force_cache_refresh(request: Request):
    if not _rate_limiter.check(request, key="refresh", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many requests.")
    _task_manager.spawn(refresh_recipe_cache())
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM recipes")
    row = await cur.fetchone()
    return {"count": row[0] if row else 0}


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


@app.post("/api/recipes/import-url")
async def import_recipe_url(payload: ImportUrlPayload, request: Request):
    if not _rate_limiter.check(request, key="recipe-create", max_hits=5):
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
                raise HTTPException(status_code=422, detail=f"Could not import recipe — {detail}")
            raise HTTPException(status_code=502, detail=f"Mealie {e.response.status_code}: {detail}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))

    data = await mealie_get(f"/api/recipes/{slug}")
    recipe_id = data.get("id")
    if not recipe_id:
        raise HTTPException(status_code=502, detail="Recipe imported but ID not found.")

    await _upsert_recipe_cache(data)

    return {
        "id": recipe_id,
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description") or "",
        "image_url": f"/api/media/{recipe_id}",
    }


@app.post("/api/recipes/quick-create")
async def quick_create_recipe(payload: QuickCreatePayload, request: Request):
    if not _rate_limiter.check(request, key="recipe-create", max_hits=5):
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

    await _upsert_recipe_cache(data)

    return {
        "id": recipe_id,
        "slug": data.get("slug"),
        "name": data.get("name"),
        "description": data.get("description") or "",
        "image_url": f"/api/media/{recipe_id}",
    }


_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


@app.post("/api/recipes/{slug}/image")
async def upload_recipe_image(slug: str, file: UploadFile, request: Request):
    _require_slug(slug, "recipe slug")
    if not _rate_limiter.check(request, key="recipe-image", max_hits=10):
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

    all_recipes = await _get_cached_recipes(limit=10000)
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
