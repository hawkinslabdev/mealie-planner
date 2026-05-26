import logging
import os

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from config import DOCKER_MODE, REQUIRE_AUTH, SESSION_COOKIE, verify_session_token
from utils import safe_redirect_path

logger = logging.getLogger("mealie_planner")

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

_MAX_JSON_BODY_BYTES = 64 * 1024

_EXEMPT_PATHS = {"/auth", "/api/auth/verify", "/favicon.ico"}


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


class IngressAndAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")

        if REQUIRE_AUTH:
            path = request.url.path.rstrip("/")
            if not path.startswith("/assets/"):
                if path not in _EXEMPT_PATHS:
                    # Supervisor already authenticated the user; trust the injected header.
                    if not DOCKER_MODE and request.headers.get("X-Remote-User-Id"):
                        pass
                    else:
                        token = request.cookies.get(SESSION_COOKIE)
                        if not token or not verify_session_token(token):
                            ip = request.client.host if request.client else "unknown"
                            logger.warning("access.denied path=%s ip=%s", path, ip)
                            return RedirectResponse(
                                url=f"/auth?from={safe_redirect_path(request.url.path)}",
                                status_code=302,
                            )

        return await call_next(request)
