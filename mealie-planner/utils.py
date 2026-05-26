import asyncio
import logging
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from fastapi import HTTPException, Request

logger = logging.getLogger("mealie_planner")


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


task_manager = _TaskManager()


class _RateLimiter:
    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self.max = max_requests
        self.window = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def _client_ip(self, request: Request) -> str:
        from config import DOCKER_MODE
        if not DOCKER_MODE:  # HAOS ingress sets a trusted X-Forwarded-For
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request, *, key: str | None = None, max_hits: int | None = None) -> bool:
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


rate_limiter = _RateLimiter()

# og:image extraction — handles both attribute orderings and quote styles
_OG_IMAGE_RE = re.compile(
    r"<meta\s[^>]*\bproperty=[\"']og:image[\"'][^>]*\bcontent=[\"']([^\"']+)[\"']"
    r"|<meta\s[^>]*\bcontent=[\"']([^\"']+)[\"'][^>]*\bproperty=[\"']og:image[\"']",
    re.IGNORECASE,
)


def extract_og_image(html: str) -> str | None:
    m = _OG_IMAGE_RE.search(html)
    return (m.group(1) or m.group(2)) if m else None


# Input validation
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]{1,200}$')
VALID_MEAL_TYPES = {"breakfast", "lunch", "dinner", "side"}


def require_uuid(val: str, name: str = "ID") -> None:
    if not _UUID_RE.match(val):
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def require_slug(val: str, name: str = "slug") -> None:
    if not _SLUG_RE.match(val):
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def require_int_id(val: str, name: str = "ID") -> None:
    if not val.isdigit() or int(val) <= 0:
        raise HTTPException(status_code=400, detail=f"Invalid {name}.")


def require_date(val: str) -> None:
    try:
        datetime.strptime(val, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD.")


def safe_redirect_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        return "/"
    return path


def normalize_meal_entry(entry: dict) -> dict:
    recipe = entry.get("recipe") or {}
    recipe_id = recipe.get("id") or entry.get("recipeId")
    recipe_name = recipe.get("name") or None
    return {
        "id": entry.get("id"),
        "date": entry.get("date"),
        "meal_type": entry.get("entryType", "dinner"),
        "recipe_id": recipe_id,
        "recipe_slug": recipe.get("slug"),
        "recipe_name": recipe_name,
        "image_url": f"/api/media/{recipe_id}" if recipe_id else None,
        "orphaned": not bool(recipe_name),
    }
