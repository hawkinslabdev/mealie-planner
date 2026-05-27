"""
API integration tests, mocked Mealie backend and database.

All Mealie HTTP calls and database I/O are replaced with lightweight fakes
so tests run offline, without a real Mealie instance or SQLite file.
The FastAPI app runs in-process via httpx.ASGITransport.
"""

import pytest
import pytest_asyncio
from contextlib import ExitStack
from cryptography.fernet import Fernet
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

import routers.settings as _settings_mod
from main import app
from utils import rate_limiter

# Test constants 

RECIPE_UUID = "11111111-1111-1111-1111-111111111111"
ACTION_UUID = "22222222-2222-2222-2222-222222222222"
MEALIE_URL = "http://mealie.local"
API_TOKEN = "test-token"

SAMPLE_RECIPE = {
    "id": RECIPE_UUID,
    "slug": "pasta-bake",
    "name": "Pasta Bake",
    "description": "Tasty",
}
SAMPLE_ENTRY = {
    "id": 42,
    "date": "2025-06-01",
    "entryType": "dinner",
    "recipe": {"id": RECIPE_UUID, "slug": "pasta-bake", "name": "Pasta Bake"},
}

# Fake database helpers


class _FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeExecute:
    """Supports both `await db.execute(sql)` and `async with db.execute(sql) as cur:`."""

    def __init__(self, rows=()):
        self._rows = rows

    def __await__(self):
        async def _r():
            return _FakeCursor(self._rows)

        return _r().__await__()

    async def __aenter__(self):
        return _FakeCursor(self._rows)

    async def __aexit__(self, *_):
        return False


class _FakeDB:
    def __init__(self, rows=(), count=0):
        self._rows = rows
        self._count = count
        self.committed = False

    def execute(self, sql="", params=()):
        # cache_refresh endpoint calls COUNT(*) to report how many recipes are cached
        if "COUNT(*)" in str(sql):
            return _FakeExecute([(self._count,)])
        return _FakeExecute(self._rows)

    async def commit(self):
        self.committed = True

@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level caches / rate-limiter state between tests."""
    _settings_mod._status_cache = {}
    _settings_mod._status_cached_at = 0.0
    rate_limiter._buckets.clear()


@pytest_asyncio.fixture
async def client():
    """FastAPI test client with all external I/O mocked."""
    with (
        patch("main.init_db", new_callable=AsyncMock),
        patch("main.warm_cache_if_needed", new_callable=AsyncMock),
        patch("main.close_db", new_callable=AsyncMock),
        patch("main.close_http_client", new_callable=AsyncMock),
        patch("config._get_or_create_key", return_value=Fernet.generate_key()),
        patch("config.REQUIRE_AUTH", False),
        patch("middleware.REQUIRE_AUTH", False),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


def _creds_ctx(url=MEALIE_URL, token=API_TOKEN):
    """Return an ExitStack that patches get_credentials everywhere it's imported."""
    stack = ExitStack()
    for target in (
        "mealie.get_credentials",
        "routers.settings.get_credentials",
        "routers.recipes.get_credentials",
    ):
        stack.enter_context(patch(target, return_value=(url, token)))
    return stack




class TestStatus:
    async def test_unconfigured(self, client):
        with patch("routers.settings.get_credentials", return_value=(None, None)):
            r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is False
        assert data["mealie_reachable"] is False

    async def test_configured_reachable(self, client):
        with (
            patch("routers.settings.get_credentials", return_value=(MEALIE_URL, API_TOKEN)),
            patch("routers.settings.mealie_get", new=AsyncMock(return_value={"version": "1.12.0"})),
        ):
            r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is True
        assert data["mealie_reachable"] is True
        assert data["version"] == "1.12.0"

    async def test_configured_unreachable(self, client):
        from fastapi import HTTPException

        with (
            patch("routers.settings.get_credentials", return_value=(MEALIE_URL, API_TOKEN)),
            patch("routers.settings.mealie_get", side_effect=HTTPException(502, "down")),
        ):
            r = await client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["configured"] is True
        assert data["mealie_reachable"] is False




class TestConfig:
    async def test_get_config(self, client):
        with patch("routers.settings.get_credentials", return_value=(MEALIE_URL, API_TOKEN)):
            r = await client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["mealie_url"] == MEALIE_URL

    async def test_get_config_unconfigured(self, client):
        with patch("routers.settings.get_credentials", return_value=(None, None)):
            r = await client.get("/api/config")
        assert r.status_code == 200
        assert r.json()["mealie_url"] == ""

    async def test_post_config_invalid_url_scheme(self, client):
        r = await client.post("/api/config", json={"mealie_url": "ftp://mealie.local", "api_token": "tok"})
        assert r.status_code == 422

    async def test_post_config_not_a_url(self, client):
        r = await client.post("/api/config", json={"mealie_url": "not-a-url", "api_token": "tok"})
        assert r.status_code == 422

    async def test_post_config_docker_mode_blocked(self, client):
        with patch("routers.settings.DOCKER_MODE", True):
            r = await client.post("/api/config", json={"mealie_url": MEALIE_URL, "api_token": "tok"})
        assert r.status_code == 400

    async def test_post_config_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)

        with (
            patch("routers.settings.DOCKER_MODE", False),
            patch("routers.settings.get_http_client", new=AsyncMock(return_value=mock_http)),
            patch("routers.settings.get_credentials", return_value=(None, None)),
            patch("routers.settings.write_credentials"),
            patch("routers.settings.encrypt_token", return_value="enc"),
            patch("routers.settings.task_manager"),
        ):
            r = await client.post("/api/config", json={"mealie_url": MEALIE_URL, "api_token": "tok"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_post_config_invalid_token(self, client):
        import httpx

        mock_err_resp = MagicMock()
        mock_err_resp.status_code = 401
        mock_http = MagicMock()
        mock_http.get = AsyncMock(side_effect=httpx.HTTPStatusError("401", request=MagicMock(), response=mock_err_resp))

        with (
            patch("routers.settings.DOCKER_MODE", False),
            patch("routers.settings.get_http_client", new=AsyncMock(return_value=mock_http)),
            patch("routers.settings.get_credentials", return_value=(None, None)),
        ):
            r = await client.post("/api/config", json={"mealie_url": MEALIE_URL, "api_token": "bad"})
        assert r.status_code == 422
        assert "token" in r.json()["detail"].lower()

class TestSettings:
    async def test_get_defaults(self, client):
        db = _FakeDB()
        with patch("routers.settings.get_db", new=AsyncMock(return_value=db)):
            r = await client.get("/api/settings")
        assert r.status_code == 200
        assert "show_quick_add" in r.json()

    async def test_get_stored_value(self, client):
        import json

        db = _FakeDB(rows=[("show_quick_add", json.dumps(False))])
        with patch("routers.settings.get_db", new=AsyncMock(return_value=db)):
            r = await client.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["show_quick_add"] is False

    async def test_patch(self, client):
        db = _FakeDB()
        with patch("routers.settings.get_db", new=AsyncMock(return_value=db)):
            r = await client.patch("/api/settings", json={"show_quick_add": False})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_patch_rate_limit(self, client):
        db = _FakeDB()
        with patch("routers.settings.get_db", new=AsyncMock(return_value=db)):
            for _ in range(30):  # 30 is the burst ceiling for the settings rate limiter
                await client.patch("/api/settings", json={"show_quick_add": True})
            r = await client.patch("/api/settings", json={"show_quick_add": True})
        assert r.status_code == 429




class TestMealplan:
    async def test_get(self, client):
        with patch("routers.mealplan.mealie_get", new=AsyncMock(return_value={"items": [SAMPLE_ENTRY]})):
            r = await client.get("/api/mealplan?start_date=2025-06-01&end_date=2025-06-07")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["meal_type"] == "dinner"
        assert data[0]["recipe_id"] == RECIPE_UUID

    async def test_get_invalid_date(self, client):
        r = await client.get("/api/mealplan?start_date=not-a-date&end_date=2025-06-07")
        assert r.status_code == 400

    async def test_post(self, client):
        with patch("routers.mealplan.mealie_post", new=AsyncMock(return_value=SAMPLE_ENTRY)):
            r = await client.post(
                "/api/mealplan",
                json={"date": "2025-06-01", "meal_type": "dinner", "recipe_id": RECIPE_UUID},
            )
        assert r.status_code == 200
        assert r.json()["meal_type"] == "dinner"

    async def test_post_invalid_meal_type(self, client):
        r = await client.post(
            "/api/mealplan",
            json={"date": "2025-06-01", "meal_type": "brunch", "recipe_id": RECIPE_UUID},
        )
        assert r.status_code == 422

    async def test_post_invalid_uuid(self, client):
        r = await client.post(
            "/api/mealplan",
            json={"date": "2025-06-01", "meal_type": "dinner", "recipe_id": "not-a-uuid"},
        )
        assert r.status_code == 422

    async def test_post_invalid_date_format(self, client):
        r = await client.post(
            "/api/mealplan",
            json={"date": "01/06/2025", "meal_type": "dinner", "recipe_id": RECIPE_UUID},
        )
        assert r.status_code == 422

    async def test_delete(self, client):
        with patch("routers.mealplan.mealie_delete", new=AsyncMock(return_value=None)):
            r = await client.delete("/api/mealplan/42")
        assert r.status_code == 204

    async def test_delete_invalid_id(self, client):
        r = await client.delete("/api/mealplan/not-an-id")
        assert r.status_code == 400

    async def test_delete_zero_id(self, client):
        r = await client.delete("/api/mealplan/0")
        assert r.status_code == 400




class TestRecipes:
    async def test_list(self, client):
        cached = [{"id": RECIPE_UUID, "name": "Pasta", "slug": "pasta", "image_url": None}]
        with (
            patch("routers.recipes.ensure_cache_fresh", new=AsyncMock()),
            patch("routers.recipes.get_cached_recipes", new=AsyncMock(return_value=cached)),
        ):
            r = await client.get("/api/recipes")
        assert r.status_code == 200
        assert r.json()[0]["id"] == RECIPE_UUID

    async def test_list_search(self, client):
        with (
            patch("routers.recipes.ensure_cache_fresh", new=AsyncMock()),
            patch("routers.recipes.get_cached_recipes", new=AsyncMock(return_value=[])),
        ):
            r = await client.get("/api/recipes?q=pasta&limit=10")
        assert r.status_code == 200
        assert r.json() == []

    async def test_cache_refresh(self, client):
        db = _FakeDB(count=15)
        with (
            patch("routers.recipes.task_manager"),
            patch("routers.recipes.get_db", new=AsyncMock(return_value=db)),
            patch("routers.recipes.refresh_recipe_cache", new=MagicMock()),
        ):
            r = await client.post("/api/cache/refresh")
        assert r.status_code == 200
        assert r.json()["count"] == 15

    async def test_recipe_link_redirect(self, client):
        # follow_redirects=False: let httpx stop at the 3xx so we can inspect it;
        # following it would chase the URL to a real Mealie host and fail.
        with _creds_ctx():
            r = await client.get("/api/recipe-link/pasta-bake", follow_redirects=False)
        assert r.status_code in (301, 302, 307)
        assert "pasta-bake" in r.headers["location"]

    async def test_recipe_link_invalid_slug(self, client):
        with _creds_ctx():
            r = await client.get("/api/recipe-link/bad slug!!")
        assert r.status_code == 400

    async def test_media_invalid_uuid(self, client):
        with _creds_ctx():
            r = await client.get("/api/media/not-a-uuid")
        assert r.status_code == 400

    async def test_sparkle_returns_recipe(self, client):
        from fastapi import HTTPException

        cached = [{"id": RECIPE_UUID, "name": "Pasta", "slug": "pasta"}]
        with (
            # 400 from Mealie means no plan exists for the date; sparkle falls back to a random cached recipe
            patch("routers.recipes.mealie_get", side_effect=HTTPException(400, "no plans")),
            patch("routers.recipes.get_cached_recipes", new=AsyncMock(return_value=cached)),
        ):
            r = await client.get("/api/sparkle?date=2025-06-01&meal_type=dinner")
        assert r.status_code == 200
        assert r.json()["id"] == RECIPE_UUID

    async def test_sparkle_invalid_date(self, client):
        r = await client.get("/api/sparkle?date=not-a-date")
        assert r.status_code == 400

    async def test_sparkle_empty_cache(self, client):
        from fastapi import HTTPException

        with (
            patch("routers.recipes.mealie_get", side_effect=HTTPException(400, "no plans")),
            patch("routers.recipes.get_cached_recipes", new=AsyncMock(return_value=[])),
        ):
            r = await client.get("/api/sparkle?date=2025-06-01")
        assert r.status_code == 404

    async def test_quick_create(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value="pasta-bake")  # Mealie returns a bare slug string on create
        mock_http = MagicMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        with (
            _creds_ctx(),
            patch("routers.recipes.get_http_client", new=AsyncMock(return_value=mock_http)),
            patch("routers.recipes.mealie_get", new=AsyncMock(return_value=SAMPLE_RECIPE)),  # router fetches full recipe by slug
            patch("routers.recipes.upsert_recipe_cache", new=AsyncMock()),
        ):
            r = await client.post("/api/recipes/quick-create", json={"name": "Pasta Bake"})
        assert r.status_code == 200
        data = r.json()
        assert data["slug"] == "pasta-bake"
        assert data["id"] == RECIPE_UUID

    async def test_quick_create_empty_name(self, client):
        r = await client.post("/api/recipes/quick-create", json={"name": "   "})
        assert r.status_code == 422

    async def test_quick_create_name_too_long(self, client):
        r = await client.post("/api/recipes/quick-create", json={"name": "x" * 201})
        assert r.status_code == 422

    async def test_import_url_invalid_scheme(self, client):
        with _creds_ctx():
            r = await client.post("/api/recipes/import-url", json={"url": "ftp://example.com/pasta"})
        assert r.status_code == 422

    async def test_import_url_no_host(self, client):
        with _creds_ctx():
            r = await client.post("/api/recipes/import-url", json={"url": "http://"})
        assert r.status_code == 422




class TestAuth:
    async def test_verify_when_auth_not_configured(self, client):
        r = await client.post("/api/auth/verify", json={"pin": "123456"})
        assert r.status_code == 400

    async def test_verify_correct_pin(self, client):
        with (
            patch("routers.auth.REQUIRE_AUTH", True),
            patch("routers.auth.PIN_CODE", "123456"),
            patch("config.create_session_token", return_value="tok"),
        ):
            r = await client.post("/api/auth/verify", json={"pin": "123456"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    async def test_verify_wrong_pin(self, client):
        with (
            patch("routers.auth.REQUIRE_AUTH", True),
            patch("routers.auth.PIN_CODE", "123456"),
        ):
            r = await client.post("/api/auth/verify", json={"pin": "000000"})
        assert r.status_code == 401

    async def test_verify_rate_limit(self, client):
        with (
            patch("routers.auth.REQUIRE_AUTH", True),
            patch("routers.auth.PIN_CODE", "123456"),
        ):
            for _ in range(5):  # 5 failed attempts is the auth lockout threshold
                await client.post("/api/auth/verify", json={"pin": "000000"})
            r = await client.post("/api/auth/verify", json={"pin": "000000"})
        assert r.status_code == 429

    async def test_logout(self, client):
        r = await client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json()["ok"] is True




class TestActions:
    async def test_list(self, client):
        payload = {"items": [{"id": ACTION_UUID, "title": "Send to TV", "actionType": "link"}]}
        with patch("routers.actions.mealie_get", new=AsyncMock(return_value=payload)):
            r = await client.get("/api/recipe-actions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["id"] == ACTION_UUID
        assert data[0]["name"] == "Send to TV"

    async def test_trigger_link_action(self, client):
        action = {"id": ACTION_UUID, "actionType": "link", "url": "https://tv.local/{slug}"}
        recipe = {"id": RECIPE_UUID, "slug": "pasta-bake"}
        with patch("routers.actions.mealie_get", new=AsyncMock(side_effect=[action, recipe])):
            r = await client.post(
                f"/api/recipe-actions/{ACTION_UUID}/trigger",
                json={"recipe_slug": "pasta-bake"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "link"
        assert "pasta-bake" in data["url"]

    async def test_trigger_invalid_action_uuid(self, client):
        r = await client.post(
            "/api/recipe-actions/not-a-uuid/trigger",
            json={"recipe_slug": "pasta-bake"},
        )
        assert r.status_code == 400

    async def test_trigger_invalid_slug(self, client):
        r = await client.post(
            f"/api/recipe-actions/{ACTION_UUID}/trigger",
            json={"recipe_slug": "bad slug!!"},
        )
        assert r.status_code == 422




class TestBodySizeLimit:
    async def test_oversized_body_rejected(self, client):
        big = b'{"name":"' + b"x" * (65 * 1024) + b'"}'  # 65 KB intentionally exceeds the 64 KB limit
        r = await client.post(
            "/api/recipes/quick-create",
            content=big,
            # Content-Length must be set explicitly; httpx won't add it for raw content by default
            headers={"Content-Type": "application/json", "Content-Length": str(len(big))},
        )
        assert r.status_code == 413
