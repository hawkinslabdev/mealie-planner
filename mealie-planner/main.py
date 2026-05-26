import logging
import os
import re
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config as cfg
from database import close_db, init_db, warm_cache_if_needed
from i18n import get_locale, load_locale_json
from mealie import close_http_client
from middleware import IngressAndAuthMiddleware, MaxBodySizeMiddleware, SecurityHeadersMiddleware
from routers import actions, auth, mealplan, recipes, settings
from utils import task_manager

logger = logging.getLogger("mealie_planner")
logger.setLevel(logging.DEBUG if os.environ.get("DEV_MODE") else logging.INFO)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False

    class _SuppressNoisyAccess(logging.Filter):
        _QUIET = re.compile(r"GET /api/media/|GET /api/status|GET /api/recipes/poll|GET /assets/")

        def filter(self, record: logging.LogRecord) -> bool:
            if isinstance(record.args, tuple) and len(record.args) >= 5:
                status = record.args[4]
                if isinstance(status, int) and status >= 400:
                    return True
            return not self._QUIET.search(record.getMessage())

    logging.getLogger("uvicorn.access").addFilter(_SuppressNoisyAccess())


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    logger.info("startup mode=%s data=%s", cfg.get_mode(), cfg.DATA_PATH)
    os.makedirs(cfg.DATA_PATH, exist_ok=True)
    cfg._get_or_create_key()
    await init_db()
    task_manager.spawn(warm_cache_if_needed())
    yield
    try:
        await task_manager.cancel_all()
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

app.add_middleware(IngressAndAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxBodySizeMiddleware)

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(recipes.router)
app.include_router(mealplan.router)
app.include_router(actions.router)


@app.get("/favicon.ico")
async def favicon():
    return FileResponse("favicon.ico")


@app.get("/")
async def index(request: Request):
    locale = get_locale(request)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "ingress_path": request.state.ingress_path,
            "locale": locale,
            "translations": load_locale_json(locale),
        },
    )
