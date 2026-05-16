"""Spec-domain compatibility entry for shared Spec utilities.

The legacy ``src.utils.spec_utils`` import remains supported. New Spec-domain
callers should prefer this module so domain parsing helpers are discoverable
from ``src.spec_engine`` without expanding the generic utils surface.
"""

from __future__ import annotations

from ..utils.spec_utils import *  # noqa: F401,F403 - compatibility facade
