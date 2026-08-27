"""Validated configuration loaded exclusively from environment variables."""

import logging
import os
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse

from dotenv import load_dotenv

from config_helpers import normalize_env_text


# Load local development settings when a .env file is present. Secrets stay
# outside source control because .env is ignored by the repository.
load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int | None = None, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        if default is None:
            raw = _required(name)
        else:
            return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


def _optional_text(name: str) -> str:
    return os.environ.get(name, "").strip()


TG_BOT_TOKEN = _required("TG_BOT_TOKEN")
APP_ID = _int("APP_ID", minimum=1)
API_HASH = _required("API_HASH")
CHANNEL_ID = _int("CHANNEL_ID")
OWNER_ID = _int("OWNER_ID", minimum=1)
PORT = _int("PORT", 8080, minimum=1)
DB_URI = _required("DATABASE_URL")
DB_NAME = os.environ.get("DATABASE_NAME", "filesharexbot").strip() or "filesharexbot"

APP_VERSION = _optional_text("APP_VERSION") or "2.2.0-dev"
CONTROL_PLANE_URL = _optional_text("CONTROL_PLANE_URL").rstrip("/")
CONTROL_PLANE_INSTANCE_ID = _optional_text("CONTROL_PLANE_INSTANCE_ID")
CONTROL_PLANE_SECRET = _optional_text("CONTROL_PLANE_SECRET")
CONTROL_PLANE_HEARTBEAT_INTERVAL = _int("CONTROL_PLANE_HEARTBEAT_INTERVAL", 60, minimum=15)
CONTROL_PLANE_TIMEOUT = _int("CONTROL_PLANE_TIMEOUT", 10, minimum=1)
if CONTROL_PLANE_URL:
    parsed_control_plane_url = urlparse(CONTROL_PLANE_URL)
    is_local_http = parsed_control_plane_url.scheme == "http" and parsed_control_plane_url.hostname in {
        "localhost", "127.0.0.1", "::1"
    }
    if not parsed_control_plane_url.hostname:
        raise RuntimeError("CONTROL_PLANE_URL must include a hostname")
    if parsed_control_plane_url.scheme != "https" and not is_local_http:
        raise RuntimeError("CONTROL_PLANE_URL must use HTTPS (HTTP is allowed only for localhost)")
    if not CONTROL_PLANE_INSTANCE_ID:
        raise RuntimeError("CONTROL_PLANE_INSTANCE_ID is required when CONTROL_PLANE_URL is set")
    if not CONTROL_PLANE_SECRET:
        raise RuntimeError("CONTROL_PLANE_SECRET is required when CONTROL_PLANE_URL is set")
    if len(CONTROL_PLANE_SECRET) < 16:
        raise RuntimeError("CONTROL_PLANE_SECRET must contain at least 16 characters")

# TIME controls delivered-message deletion. LINK_TTL controls link validity.
TIME = _int("TIME", 86400, minimum=0)
LINK_TTL = _int("LINK_TTL", 86400, minimum=60)
CLEANUP_INTERVAL = _int("CLEANUP_INTERVAL", 60, minimum=5)
CLEANUP_MAX_ATTEMPTS = _int("CLEANUP_MAX_ATTEMPTS", 5, minimum=1)
CLEANUP_RETRY_BASE_SECONDS = _int("CLEANUP_RETRY_BASE_SECONDS", 60, minimum=1)
CLEANUP_RETRY_MAX_SECONDS = _int("CLEANUP_RETRY_MAX_SECONDS", 3600, minimum=1)
if CLEANUP_RETRY_MAX_SECONDS < CLEANUP_RETRY_BASE_SECONDS:
    raise RuntimeError("CLEANUP_RETRY_MAX_SECONDS must be >= CLEANUP_RETRY_BASE_SECONDS")
MAX_BATCH_MESSAGES = _int("MAX_BATCH_MESSAGES", 100, minimum=1)
TG_BOT_WORKERS = _int("TG_BOT_WORKERS", 4, minimum=1)
TELEGRAM_API_TIMEOUT = _int("TELEGRAM_API_TIMEOUT", 30, minimum=1)

FORCE_SUB_CHANNEL1 = _int("FORCE_SUB_CHANNEL1", 0)
FORCE_SUB_CHANNEL2 = _int("FORCE_SUB_CHANNEL2", 0)
FORCE_SUB_CHANNEL3 = _int("FORCE_SUB_CHANNEL3", 0)
FORCE_SUB_CHANNEL4 = _int("FORCE_SUB_CHANNEL4", 0)

try:
    ADMINS = [int(value) for value in os.environ.get("ADMINS", "").split()]
except ValueError as exc:
    raise RuntimeError("ADMINS must contain space-separated integer Telegram IDs") from exc
ADMINS = sorted(set(ADMINS + [OWNER_ID]))

START_MSG = normalize_env_text(os.environ.get("START_MESSAGE", "").strip()) or (
    "<b>Halo {mention}! Saya dapat membagikan file melalui tautan sementara.</b>"
)
FORCE_MSG = normalize_env_text(os.environ.get("FORCE_SUB_MESSAGE", "").strip()) or (
    "👋 Hello {first} {last}\n\n"
    "Kamu harus bergabung di Channel/Grup kami terlebih dahulu untuk melihat File/Link yang kami bagikan\n\n"
    "Silakan join ke Channel & Group terlebih dahulu kemudian klik tombol muat ulang dibawah untuk melanjutkan"
)
PICS = os.environ.get("PICS", "").split()
CUSTOM_CAPTION = normalize_env_text(os.environ.get("CUSTOM_CAPTION", "")) or None
PROTECT_CONTENT = os.environ.get("PROTECT_CONTENT", "False").lower() == "true"
DISABLE_CHANNEL_BUTTON = os.environ.get("DISABLE_CHANNEL_BUTTON", "False").lower() == "true"
ALLOW_LEGACY_LINKS = os.environ.get("ALLOW_LEGACY_LINKS", "False").lower() == "true"
MAIN_CHANNEL_URL = os.environ.get("MAIN_CHANNEL_URL", "").strip()
SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "").strip()
BOT_STATS_TEXT = normalize_env_text(os.environ.get("BOT_STATS_TEXT", "").strip()) or (
    "<b>BOT UPTIME</b>\n{uptime}"
)
USER_REPLY_TEXT = normalize_env_text(
    os.environ.get("USER_REPLY_TEXT", "<b>Pesan ini hanya dapat digunakan untuk berbagi file.</b>")
)

LOG_FILE_NAME = os.environ.get("LOG_FILE_NAME", "filesharingbot.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(levelname)s] - %(name)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S",
    handlers=[
        RotatingFileHandler(LOG_FILE_NAME, maxBytes=50_000_000, backupCount=10),
        logging.StreamHandler(),
    ],
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


def LOGGER(name: str) -> logging.Logger:
    return logging.getLogger(name)
