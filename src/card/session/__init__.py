"""Card session subpackage — session lifecycle, configuration, and rotation.

Re-exports public API for backward compatibility:
    from src.card.session import CardSession, SessionConfig, ...
"""

from src.card.session.builder import SessionBuilder, SessionCollaborators
from src.card.session.config import SessionCallbacks, SessionConfig
from src.card.session.core import CardSession, _pending_action_to_event
from src.card.session.factory import CardSessionFactory
from src.card.session.rotator import SessionRotator
from src.card.session.static import StaticCardSession
from src.card.session.ttl import TTLHandler

__all__ = [
    "CardSession",
    "CardSessionFactory",
    "SessionBuilder",
    "SessionCallbacks",
    "SessionCollaborators",
    "SessionConfig",
    "SessionRotator",
    "StaticCardSession",
    "TTLHandler",
]
