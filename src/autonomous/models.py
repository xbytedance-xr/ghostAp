"""Compatibility re-exports for immutable autonomous domain models."""

from .domain import *  # noqa: F403
from .domain import __all__ as _DOMAIN_ALL
from .domain.ids import new_id as _new_id

__all__ = [*_DOMAIN_ALL, "_new_id"]
