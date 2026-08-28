"""Test-only hermetic environment bootstrap.

This module must be imported before the root ``config`` module on any offline
test process. Root :file:`config.py` builds a production ``RotatingFileHandler``
at import time from ``LOG_FILE_NAME``, which defaults to ``filesharingbot.log``
relative to the current working directory. Canonical offline test commands run
with the repository root as the working directory, so importing ``config``
without this bootstrap would create or rewrite ``<repository-root>\\filesharingbot.log``.

This helper redirects ``LOG_FILE_NAME`` to an absolute temporary directory that
lives outside the repository and registers a process-exit cleanup that only ever
touches that self-created temporary directory. It does not modify the production
logging setup nor the user's existing ``filesharingbot.log`` artifact.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile
from pathlib import Path

# Never depend on the current working directory; the repository root is derived
# from this module's own location so the guard remains valid however the tests
# are launched.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_TEMP_ROOT: Path | None = None


def _allowed_temp_root() -> Path:
    """Create a temp log directory outside the repository."""
    return Path(tempfile.mkdtemp(prefix="teledrop-test-logs-")).resolve()


def _cleanup() -> None:
    """Close test logging and remove only the temp dir this helper created."""
    global _TEMP_ROOT
    temp_root = _TEMP_ROOT
    _TEMP_ROOT = None
    if temp_root is None:
        return
    try:
        # Close any RotatingFileHandler that config.py opened inside the temp
        # directory so the directory is not locked on Windows at process exit.
        logging.shutdown()
    finally:
        # Remove only the directory this helper created and nothing else. It can
        # never be a repository path because mkdtemp() uses the system temp dir.
        shutil.rmtree(temp_root, ignore_errors=True)


def activate() -> None:
    """Redirect production test logging away from the repository, idempotently.

    Safe to call repeatedly and from any package initializer. Only the first call
    creates a temporary directory and registers the cleanup handler.
    """
    global _TEMP_ROOT
    if _TEMP_ROOT is not None:
        return
    temp_root = _allowed_temp_root()
    if _REPOSITORY_ROOT in temp_root.parents or temp_root == _REPOSITORY_ROOT:
        raise RuntimeError("hermetic temp log directory must not live inside the repository")
    _TEMP_ROOT = temp_root
    os.environ["LOG_FILE_NAME"] = str(temp_root / "config-test.log")
    atexit.register(_cleanup)


activate()