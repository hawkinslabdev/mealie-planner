import json
import logging
import os

from fastapi import Request

from config import LOCALE_DIR, LOCALE_OVERRIDE, SUPPORTED_LOCALES

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
        lang = part.split(";")[0].strip().split("-")[0].lower()
        if lang in SUPPORTED_LOCALES:
            return lang
    return None


def get_locale(request: Request) -> str:
    """Priority: cookie > LOCALE env var > Accept-Language header > 'en'."""
    cookie = request.cookies.get("mp_locale", "").strip().lower()
    if cookie in SUPPORTED_LOCALES:
        return cookie
    if LOCALE_OVERRIDE:
        return LOCALE_OVERRIDE
    detected = detect_accept_language(request.headers.get("accept-language", ""))
    return detected or "en"
