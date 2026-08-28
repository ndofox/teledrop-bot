"""Local project tests package; prevents discovery from importing another tests package."""

# Activate the test-only hermetic environment before any test module can import
# the root ``config`` module, so production logging writes to a temporary
# directory outside the repository instead of creating ``filesharingbot.log``
# in the repository root relative to the current working directory.
from . import _hermetic_environment  # noqa: F401  (module self-activates)