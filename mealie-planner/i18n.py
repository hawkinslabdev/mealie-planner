import json
import logging
import os

from fastapi import Request

from config import LOCALE_DIR, LOCALE_OVERRIDE, resolve_locale

logger = logging.getLogger("mealie_planner")

def load_locale_json(lang: str) -> dict:
    path = os.path.join(LOCALE_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def detect_accept_language(accept_language: str) -> str | None:
    for part in accept_language.split(","):
        lang = resolve_locale(part.split(";")[0])
        if lang:
            return lang
    return None


def get_locale(request: Request) -> str:
    """Priority: cookie > LOCALE env var > Accept-Language header > 'en'."""
    cookie = resolve_locale(request.cookies.get("mp_locale", ""))
    if cookie:
        return cookie
    if LOCALE_OVERRIDE:
        return LOCALE_OVERRIDE
    detected = detect_accept_language(request.headers.get("accept-language", ""))
    return detected or "en"
