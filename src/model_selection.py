"""Shared model-selection sentinels."""

from __future__ import annotations

DEFAULT_MODEL_OPTION_VALUE = "__ghostap_default_model__"


def is_default_model_option(value: object) -> bool:
    return str(value or "").strip() == DEFAULT_MODEL_OPTION_VALUE
