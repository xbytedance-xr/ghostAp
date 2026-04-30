"""Unified card delivery engine.

Public API:
- CardDelivery: main delivery engine
- CardAPIClient: protocol for Feishu API
- DeliveryThrottle: adaptive throttle
- SequenceManager, BindingStore: internal state management
"""

from src.card.delivery.engine import CardDelivery, CardAPIClient, MutationOutcome, SequenceConflictError, TransportError
from src.card.delivery.engine_sender import EngineCardSender
from src.card.delivery.feishu_client import FeishuCardAPIClient
from src.card.delivery.throttle import DeliveryThrottle
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.binding import BindingStore, DeliveryBinding, PageBinding

__all__ = [
    "CardDelivery",
    "CardAPIClient",
    "EngineCardSender",
    "FeishuCardAPIClient",
    "MutationOutcome",
    "SequenceConflictError",
    "TransportError",
    "DeliveryThrottle",
    "SequenceManager",
    "BindingStore",
    "DeliveryBinding",
    "PageBinding",
]
