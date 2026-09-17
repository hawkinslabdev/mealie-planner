import logging
import time
import uuid as _uuid_mod
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from config import get_credentials
from mealie import mealie_delete, mealie_get, mealie_post
from utils import (
    normalize_meal_entry,
    rate_limiter,
    require_date,
    require_int_id,
    task_manager,
    VALID_MEAL_TYPES,
)

logger = logging.getLogger("mealie_planner")
router = APIRouter()

_mealplan_cache: dict[tuple[str, str], list[dict]] = {}
_mealplan_cached_at: dict[tuple[str, str], float] = {}
_mealplan_revalidating: set[tuple[str, str]] = set()
_mealplan_gen: int = 0
_MEALPLAN_TTL: int = 60
_MEALPLAN_MAX_KEYS: int = 32


def _stale(key: tuple[str, str]) -> bool:
    return time.time() - _mealplan_cached_at.get(key, 0.0) >= _MEALPLAN_TTL


def _entry_id(entry: dict) -> str:
    return str(entry.get("id"))


def _bump_gen() -> None:
    global _mealplan_gen
    _mealplan_gen += 1


def _store(key: tuple[str, str], entries: list[dict]) -> None:
    if key not in _mealplan_cache and len(_mealplan_cache) >= _MEALPLAN_MAX_KEYS:
        oldest = next(iter(_mealplan_cache))
        _mealplan_cache.pop(oldest, None)
        _mealplan_cached_at.pop(oldest, None)
    _mealplan_cache[key] = entries
    _mealplan_cached_at[key] = time.time()


async def _fetch_mealplan(start_date: str, end_date: str) -> list[dict]:
    data = await mealie_get(
        f"/api/households/mealplans?start_date={start_date}&end_date={end_date}&perPage=50"
    )
    items = data.get("items", []) if isinstance(data, dict) else data
    return [normalize_meal_entry(e) for e in items]


async def _revalidate(key: tuple[str, str], gen: int) -> None:
    try:
        entries = await _fetch_mealplan(*key)
        if gen == _mealplan_gen:
            _store(key, entries)
    finally:
        _mealplan_revalidating.discard(key)


def _schedule_revalidate(key: tuple[str, str]) -> None:
    if key in _mealplan_revalidating or not _stale(key):
        return
    _mealplan_revalidating.add(key)
    task_manager.spawn(_revalidate(key, _mealplan_gen))


@router.get("/api/mealplan")
async def get_mealplan(start_date: str, end_date: str):
    require_date(start_date)
    require_date(end_date)
    key = (start_date, end_date)
    if key in _mealplan_cache:
        _schedule_revalidate(key)
        return _mealplan_cache[key]
    entries = await _fetch_mealplan(start_date, end_date)
    _store(key, entries)
    return entries


def _cache_add(entry: dict) -> None:
    entry_date = entry.get("date")
    if not entry_date or entry.get("id") is None:
        return
    _bump_gen()
    for key in list(_mealplan_cache):
        start, end = key
        if start <= entry_date <= end:
            _mealplan_cache[key] = [
                x for x in _mealplan_cache[key] if _entry_id(x) != _entry_id(entry)
            ] + [entry]
            _mealplan_cached_at[key] = 0.0
            _schedule_revalidate(key)


def _cache_remove(entry_id: str) -> None:
    _bump_gen()
    for key in list(_mealplan_cache):
        entries = _mealplan_cache[key]
        filtered = [x for x in entries if _entry_id(x) != entry_id]
        if len(filtered) != len(entries):
            _mealplan_cache[key] = filtered
            _mealplan_cached_at[key] = 0.0
            _schedule_revalidate(key)


def clear_mealplan_cache() -> None:
    _bump_gen()
    _mealplan_cache.clear()
    _mealplan_cached_at.clear()
    _mealplan_revalidating.clear()


async def warm_mealplan_cache() -> None:
    url, token = get_credentials()
    if not url or not token:
        return
    today = date.today()
    key = (today.isoformat(), (today + timedelta(days=7)).isoformat())
    if key in _mealplan_cache:
        return
    gen = _mealplan_gen
    try:
        entries = await _fetch_mealplan(*key)
        if gen == _mealplan_gen:
            _store(key, entries)
    except Exception:
        logger.warning("mealplan.warm_failed")


class MealPlanEntry(BaseModel):
    date: str
    meal_type: str = "dinner"
    recipe_id: str

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        try:
            from datetime import datetime
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Use YYYY-MM-DD format.")
        return v

    @field_validator("meal_type")
    @classmethod
    def _check_meal_type(cls, v: str) -> str:
        if v not in VALID_MEAL_TYPES:
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


@router.post("/api/mealplan")
async def create_mealplan_entry(entry: MealPlanEntry, request: Request):
    if not rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    result = await mealie_post(
        "/api/households/mealplans",
        {
            "date": entry.date,
            "entryType": entry.meal_type,
            "recipeId": entry.recipe_id,
        },
    )
    normalized = normalize_meal_entry(result)
    _cache_add(normalized)
    return normalized


@router.delete("/api/mealplan/{entry_id}")
async def delete_mealplan_entry(entry_id: str, request: Request):
    require_int_id(entry_id, "entry ID")
    if not rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    try:
        await mealie_delete(f"/api/households/mealplans/{entry_id}")
    except HTTPException as e:
        # 404 means Mealie already removed this entry (e.g. its recipe was deleted); lets treat as succes
        if e.status_code != 404:
            raise
    _cache_remove(entry_id)
    return Response(status_code=204)
