import json
import os
import time

from cryptography.fernet import Fernet

DATA_PATH = "/data" if os.path.exists("/data") else "./data"
OPTIONS_FILE = os.path.join(DATA_PATH, "options.json")
CREDENTIALS_FILE = os.path.join(DATA_PATH, "credentials.json")
KEY_FILE = os.path.join(DATA_PATH, ".key")
CACHE_DB = os.path.join(DATA_PATH, "cache.db")
CACHE_TTL = 3600

DOCKER_MODE = bool(os.environ.get("MEALIE_API_URL"))
PIN_CODE = os.environ.get("PIN_CODE", "")
REQUIRE_AUTH = bool(PIN_CODE)
SESSION_COOKIE = "mp_session"
SESSION_TTL = 86400 * 30

# For adding a new locale: add the translation (/assets/locales/<locale>.json) and add it to SUPPORTED_LOCALES and LOCALE_NAMES in `app.js` and update SUPPORTED_LOCALES in `main.py`
SUPPORTED_LOCALES = {"en", "de", "nl", "es", "fr", "it", "pl", "ru", "cs", "sv", "da", "no", "pt_BR"}
LOCALE_OVERRIDE = os.environ.get("LOCALE", "").strip().lower()
if LOCALE_OVERRIDE not in SUPPORTED_LOCALES:
    LOCALE_OVERRIDE = ""
LOCALE_DIR = os.path.join(os.path.dirname(__file__), "assets", "locales")


def get_mode() -> str:
    return "docker" if DOCKER_MODE else "haos"


# Encryption
def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


def _cipher() -> Fernet:
    return Fernet(_get_or_create_key())


def encrypt_token(raw: str) -> str:
    return _cipher().encrypt(raw.encode()).decode()


def decrypt_token(enc: str) -> str:
    return _cipher().decrypt(enc.encode()).decode()


def _looks_encrypted(value: str) -> bool:
    return value.startswith("gAAAAA")


# Session tokens
def create_session_token() -> str:
    return encrypt_token(json.dumps({"t": int(time.time()) + SESSION_TTL}))


def verify_session_token(token: str) -> bool:
    try:
        data = json.loads(decrypt_token(token))
        return data.get("t", 0) > int(time.time())
    except Exception:
        return False


# Credential cache
_cred_cache: dict[str, str | None] = {"url": None, "token": None}
_cred_loaded_at: float = 0.0


def _cred_cache_valid() -> bool:
    if not (_cred_cache["url"] and _cred_cache["token"]):
        return False
    try:
        if os.path.exists(CREDENTIALS_FILE) and os.path.getmtime(CREDENTIALS_FILE) > _cred_loaded_at:
            return False
    except OSError:
        pass
    return True


def get_credentials() -> tuple[str | None, str | None]:
    global _cred_loaded_at
    if _cred_cache_valid():
        return _cred_cache["url"], _cred_cache["token"]

    _cred_cache["url"] = None
    _cred_cache["token"] = None

    if DOCKER_MODE:
        _cred_cache["url"] = os.environ.get("MEALIE_API_URL")
        _cred_cache["token"] = os.environ.get("MEALIE_API_KEY")
        return _cred_cache["url"], _cred_cache["token"]

    stored_url: str | None = None
    stored_plain: str | None = None

    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE) as f:
                creds = json.load(f)
            url = creds.get("mealie_url") or ""
            token_enc = creds.get("api_token") or ""
            if url and token_enc:
                stored_url = url
                stored_plain = decrypt_token(token_enc)
        except Exception:
            pass

    # If options.json has different non-empty credentials, the user updated the HAOS add-on config and then importts them into credentials.json
    if os.path.exists(OPTIONS_FILE):
        try:
            with open(OPTIONS_FILE) as f:
                opts = json.load(f)
            opts_url = (opts.get("mealie_url") or "").strip()
            opts_raw = (opts.get("api_token") or "").strip()
            if opts_url and opts_raw:
                opts_plain = opts_raw if not _looks_encrypted(opts_raw) else decrypt_token(opts_raw)
                if opts_url != stored_url or opts_plain != stored_plain:
                    encrypted = encrypt_token(opts_plain) if not _looks_encrypted(opts_raw) else opts_raw
                    write_credentials(opts_url, encrypted)
                    return opts_url, opts_plain
        except Exception:
            pass

    if stored_url and stored_plain:
        _cred_cache["url"] = stored_url
        _cred_cache["token"] = stored_plain
        try:
            _cred_loaded_at = os.path.getmtime(CREDENTIALS_FILE)
        except OSError:
            pass
        return stored_url, stored_plain

    return None, None


def write_credentials(url: str, encrypted_token: str) -> None:
    global _cred_loaded_at
    os.makedirs(DATA_PATH, exist_ok=True)
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump({"mealie_url": url, "api_token": encrypted_token}, f)
    _cred_cache["url"] = url
    _cred_cache["token"] = decrypt_token(encrypted_token)
    try:
        _cred_loaded_at = os.path.getmtime(CREDENTIALS_FILE)
    except OSError:
        pass
