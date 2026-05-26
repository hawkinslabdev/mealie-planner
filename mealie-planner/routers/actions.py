import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from mealie import mealie_get, mealie_post
from utils import _SLUG_RE, rate_limiter, require_uuid

logger = logging.getLogger("mealie_planner")
router = APIRouter()


@router.get("/api/recipe-actions/raw")
async def get_recipe_actions_raw():
    results = {}
    for path in ["/api/groups/recipe-actions", "/api/households/recipe-actions"]:
        try:
            results[path] = await mealie_get(f"{path}?perPage=10")
        except HTTPException as e:
            results[path] = {"error": e.detail}
    return results


@router.get("/api/recipe-actions")
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
                    "name": item.get("title") or item.get("name") or item.get("label") or "Action",
                    "action_type": item.get("actionType") or item.get("action_type") or "link",
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


@router.post("/api/recipe-actions/{action_id}/trigger")
async def trigger_recipe_action(action_id: str, payload: RecipeActionTrigger, request: Request):
    require_uuid(action_id, "action ID")
    if not rate_limiter.check(request, key="recipe-action", max_hits=20):
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
