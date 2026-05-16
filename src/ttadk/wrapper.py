"""TTADK-domain entry for the ACP wrapper runtime.

The legacy module path ``src.utils.ttadk_wrapper`` remains executable/importable
for existing subprocess launchers. New TTADK-domain callers can import this
facade while the implementation stays in the old location during the
compatibility window.
"""

from __future__ import annotations

from ..utils.ttadk_wrapper import *  # noqa: F401,F403 - compatibility facade
from ..utils.ttadk_wrapper import main as _main

if __name__ == "__main__":  # pragma: no cover - exercised via subprocess smoke tests
    _main()
