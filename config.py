"""Validated configuration loaded exclusively from environment variables."""

import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv


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


TG_BOT_TOKEN = _required("TG_BOT_TOKEN")
APP_ID = _int("APP_ID", minimum=1)
API_HASH = _required("API_HASH")
CHANNEL_ID = _int("CHANNEL_ID")
OWNER_ID = _int("OWNER_ID", minimum=1)
PORT = _int("PORT", 8080, minimum=1)
DB_URI = _required("DATABASE_URL")
DB_NAME = os.environ.get("DATABASE_NAME", "filesharexbot").strip() or "filesharexbot"

# TIME controls delivered-message deletion. LINK_TTL controls link validity.
TIME = _int("TIME", 86400, minimum=0)
LINK_TTL = _int("LINK_TTL", 86400, minimum=60)
CLEANUP_INTERVAL = _int("CLEANUP_INTERVAL", 60, minimum=5)
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

START_MSG = os.environ.get("START_MESSAGE", "").strip() or (
    "<b>Halo {mention}! Saya dapat membagikan file melalui tautan sementara.</b>"
)
FORCE_MSG = os.environ.get("FORCE_SUB_MESSAGE", "").strip() or (
    "👋 Hello {first} {last}\n\n"
    "Kamu harus bergabung di Channel/Grup kami terlebih dahulu untuk melihat File/Link yang kami bagikan\n\n"
    "Silakan join ke Channel & Group terlebih dahulu kemudian klik tombol muat ulang dibawah untuk melanjutkan"
)
PICS = os.environ.get("PICS", "").split()
CUSTOM_CAPTION = os.environ.get("CUSTOM_CAPTION") or None
PROTECT_CONTENT = os.environ.get("PROTECT_CONTENT", "False").lower() == "true"
DISABLE_CHANNEL_BUTTON = os.environ.get("DISABLE_CHANNEL_BUTTON", "False").lower() == "true"
ALLOW_LEGACY_LINKS = os.environ.get("ALLOW_LEGACY_LINKS", "False").lower() == "true"
MAIN_CHANNEL_URL = os.environ.get("MAIN_CHANNEL_URL", "").strip()
SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "").strip()
BOT_STATS_TEXT = os.environ.get("BOT_STATS_TEXT", "").strip() or "<b>BOT UPTIME</b>\n{uptime}"
USER_REPLY_TEXT = os.environ.get(
    "USER_REPLY_TEXT", "<b>Pesan ini hanya dapat digunakan untuk berbagi file.</b>"
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
