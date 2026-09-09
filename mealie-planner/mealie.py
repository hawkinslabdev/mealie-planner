import asyncio
import logging

import httpx
from fastapi import HTTPException

from config import get_credentials

logger = logging.getLogger("mealie_planner")

_http_client: httpx.AsyncClient | None = None
_outbound_sem = asyncio.Semaphore(4)


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
    async with _outbound_sem:
        try:
            if method == "GET":
                resp = await _client.get(full_url, headers=headers)
            elif method == "POST":
                resp = await _client.post(full_url, json=body, headers=headers)
            elif method == "PATCH":
                resp = await _client.patch(full_url, json=body, headers=headers)
            elif method == "DELETE":
                resp = await _client.delete(full_url, headers=headers)
            else:
                raise ValueError(f"Unsupported method: {method}")

            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                logger.warning("mealie non-JSON response path=%s status=%s", path, resp.status_code)
                raise HTTPException(status_code=502, detail="Mealie returned an unexpected response.")
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=str(e))
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=str(e))


async def mealie_get(path: str) -> dict | list | None:
    return await _mealie_request("GET", path)


async def mealie_post(path: str, body: dict) -> dict | list | None:
    return await _mealie_request("POST", path, body)


async def mealie_patch(path: str, body: dict) -> dict | list | None:
    return await _mealie_request("PATCH", path, body)


async def mealie_delete(path: str) -> None:
    await _mealie_request("DELETE", path)
