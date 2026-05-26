import json
import logging
import time

import aiosqlite
import httpx

from config import CACHE_DB, CACHE_TTL, get_credentials
from utils import task_manager

logger = logging.getLogger("mealie_planner")

_db: aiosqlite.Connection | None = None
_refresh_in_progress = False
_last_poll_at: int = 0
POLL_COOLDOWN_S: int = 30


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


async def cache_last_refreshed() -> int | None:
    db = await get_db()
    async with db.execute(
        "SELECT value FROM cache_meta WHERE key='recipes_last_refreshed'"
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def get_cached_recipes(
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


async def upsert_recipe_cache(recipe_data: dict) -> None:
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
        """INSERT OR REPLACE INTO recipes (id, slug, name, description, tags, image_url, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (recipe_id, slug, name, description, tags, "", int(time.time())),
    )
    await db.commit()


async def refresh_recipe_cache() -> int:
    global _refresh_in_progress
    if _refresh_in_progress:
        logger.debug("Cache refresh already in progress, skipping")
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
                except httpx.HTTPError as e:
                    logger.warning("cache.refresh_error page=%d: %s", page, e)
                    break

                items = data.get("items", [])
                if not items:
                    break

                now = int(time.time())
                for r in items:
                    seen_ids.add(r["id"])
                    description = r.get("description") or ""
                    tags = json.dumps(
                        [t.get("name") for t in (r.get("tags") or []) if isinstance(t, dict)]
                    )
                    await db.execute(
                        """INSERT OR REPLACE INTO recipes (id, slug, name, description, tags, image_url, cached_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
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


def is_cache_refreshing() -> bool:
    return _refresh_in_progress


async def warm_cache_if_needed() -> None:
    last = await cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        await refresh_recipe_cache()


async def ensure_cache_fresh() -> None:
    last = await cache_last_refreshed()
    if last is None or (int(time.time()) - last) > CACHE_TTL:
        if not _refresh_in_progress:
            task_manager.spawn(refresh_recipe_cache())
