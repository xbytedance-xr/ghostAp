import contextvars
import logging
import uuid
from typing import Optional

# Global context variable for request_id
_request_id_ctx_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    """Get the current request_id from context."""
    return _request_id_ctx_var.get()


def set_request_id(request_id: str):
    """Set the current request_id in context."""
    return _request_id_ctx_var.set(request_id)


class TraceContext:
    """Context manager to set and clear request_id."""

    def __init__(self, request_id: Optional[str] = None):
        self.request_id = request_id or str(uuid.uuid4())
        self.token = None

    def __enter__(self):
        self.token = _request_id_ctx_var.set(self.request_id)
        return self.request_id

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token:
            _request_id_ctx_var.reset(self.token)


class RequestIdFilter(logging.Filter):
    """Logging filter to inject request_id into log records."""

    def filter(self, record):
        request_id = get_request_id()
        record.request_id = request_id if request_id else "-"
        return True


def configure_logging_with_trace():
    """Helper to configure basic logging with trace support."""
    root = logging.getLogger()
    f = RequestIdFilter()
    for handler in root.handlers:
        handler.addFilter(f)
        # Try to update formatter if possible, or just rely on filter adding the field
        # Most formatters ignore extra fields unless explicitly configured.
        # But at least the record will have it.

    # Also add to the root logger itself to filter/enrich records before they hit handlers
    root.addFilter(f)
