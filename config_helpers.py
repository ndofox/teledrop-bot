"""Pure helpers for normalizing environment-backed text configuration."""


def normalize_env_text(value: str) -> str:
    """Convert escaped newlines from .env values into real newline characters.

    Supports both ``\\n`` and legacy ``\\\\n`` values so existing deployments
    can be upgraded without manually reformatting every message first.
    """
    return value.replace("\\\\n", "\n").replace("\\n", "\n")