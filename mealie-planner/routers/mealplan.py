import logging
import uuid as _uuid_mod

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator

from mealie import mealie_delete, mealie_get, mealie_post
from utils import normalize_meal_entry, rate_limiter, require_date, require_int_id, VALID_MEAL_TYPES

logger = logging.getLogger("mealie_planner")
router = APIRouter()


@router.get("/api/mealplan")
async def get_mealplan(start_date: str, end_date: str):
    require_date(start_date)
    require_date(end_date)
    data = await mealie_get(
        f"/api/households/mealplans?start_date={start_date}&end_date={end_date}&perPage=50"
    )
    items = data.get("items", []) if isinstance(data, dict) else data
    return [normalize_meal_entry(e) for e in items]


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
    return normalize_meal_entry(result)


@router.delete("/api/mealplan/{entry_id}")
async def delete_mealplan_entry(entry_id: str, request: Request):
    require_int_id(entry_id, "entry ID")
    if not rate_limiter.check(request, key="mealplan", max_hits=30):
        raise HTTPException(status_code=429, detail="Too many requests.")
    await mealie_delete(f"/api/households/mealplans/{entry_id}")
    return Response(status_code=204)
