import hmac
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import (
    DOCKER_MODE, PIN_CODE, REQUIRE_AUTH, SESSION_COOKIE, SESSION_TTL,
    create_session_token,
)
from i18n import get_locale, load_locale_json
from utils import rate_limiter, safe_redirect_path

logger = logging.getLogger("mealie_planner")
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/auth")
async def auth_page(request: Request, from_: str = Query("/", alias="from")):
    locale = get_locale(request)
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "ingress_path": request.state.ingress_path,
            "from": safe_redirect_path(from_),
            "locale": locale,
            "translations": load_locale_json(locale),
        },
    )


class PinPayload(BaseModel):
    pin: str


@router.post("/api/auth/verify")
async def verify_pin(payload: PinPayload, request: Request, response: Response):
    if not REQUIRE_AUTH:
        raise HTTPException(status_code=400, detail="PIN authentication not configured")

    if not rate_limiter.check(request, key="verify", max_hits=5):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    ip = rate_limiter._client_ip(request)
    if not hmac.compare_digest(payload.pin, PIN_CODE):
        logger.warning("auth.pin_failed ip=%s", ip)
        raise HTTPException(status_code=401, detail="Incorrect PIN")

    logger.info("auth.pin_success ip=%s", ip)
    _secure = request.url.scheme == "https"
    response.set_cookie(
        key=SESSION_COOKIE,
        value=create_session_token(),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_secure,
    )
    return {"ok": True}


@router.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    return {"ok": True}
