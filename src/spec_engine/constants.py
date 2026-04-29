"""Spec-engine UI text constants.

This module re-exports SPEC_UI_TEXT from the shared utils layer for
backward compatibility.  All text definitions live in utils/ui_text.py
to avoid reverse dependencies between card/ and spec_engine/.
"""

from __future__ import annotations

from ..utils.ui_text import SPEC_UI_TEXT

__all__ = ["SPEC_UI_TEXT"]
