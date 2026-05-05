"""Unified card delivery engine.

Public API:
- CardDelivery: main delivery engine
- CardAPIClient: protocol for Feishu API
- DeliveryRegistry: process-level instance registry
- DeliveryThrottle: adaptive throttle
- SequenceManager, BindingStore: internal state management
"""

from src.card.delivery.engine import CardDelivery, CardAPIClient, MutationOutcome, SequenceConflictError, TransportError
from src.card.delivery.feishu_client import FeishuCardAPIClient
from src.card.delivery.lock_pool import PoolStats
from src.card.delivery.registry import DeliveryRegistry, delivery_registry
from src.card.delivery.throttle import DeliveryThrottle
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.binding import BindingStore, DeliveryBinding, PageBinding
from src.card.delivery.tracker import DeliveryTracker, PendingAction

__all__ = [
    "CardDelivery",
    "CardAPIClient",
    "DeliveryRegistry",
    "delivery_registry",
    "FeishuCardAPIClient",
    "MutationOutcome",
    "PoolStats",
    "SequenceConflictError",
    "TransportError",
    "DeliveryThrottle",
    "SequenceManager",
    "BindingStore",
    "DeliveryBinding",
    "PageBinding",
    "DeliveryTracker",
    "PendingAction",
]
