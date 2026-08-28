"""Tests for the standalone control-plane server."""

# Ensure the shared test-only hermetic environment is active before any test
# module imports the root ``config`` module. The repository root is added to
# sys.path so ``tests._hermetic_environment`` resolves regardless of discovery
# context; the module self-activates on import.
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

import tests._hermetic_environment  # noqa: E402,F401  (module self-activates; import order matters)