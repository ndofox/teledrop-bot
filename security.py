"""Pure security helpers for share-link tokens.

Only a hash of a token is persisted. The raw token is returned once when a
link is created and is safe to put in a Telegram deep-link payload.
"""

import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse


TOKEN_PATTERN = re.compile(r"^f-[A-Za-z0-9_-]{32,64}$")


def new_token() -> str:
    return f"f-{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_valid_token(token: str) -> bool:
    return bool(TOKEN_PATTERN.fullmatch(token))


def extract_token(value: str) -> str | None:
    """Extract a valid v2 token from a raw token or Telegram deep link."""
    value = value.strip()
    if value.startswith("f-"):
        return value if is_valid_token(value) else None
    try:
        query = parse_qs(urlparse(value).query)
        token = query.get("start", [""])[0]
    except ValueError:
        return None
    return token if is_valid_token(token) else None


def share_payload(token: str) -> str:
    if not is_valid_token(token):
        raise ValueError("invalid share token")
    return token