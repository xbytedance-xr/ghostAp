import contextvars
import logging
import uuid
from typing import Optional

# Global context variable for trace_id (preferred over request_id for internal tracing)
_trace_id_ctx_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("trace_id", default=None)
_request_id_ctx_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_id", default=None)


def get_trace_id() -> Optional[str]:
    """Get the current trace_id from context."""
    return _trace_id_ctx_var.get() or _request_id_ctx_var.get()



class TraceContext:
    """Context manager to set and clear request_id/trace_id."""

    def __init__(self, request_id: Optional[str] = None, trace_id: Optional[str] = None):
        # Prefer trace_id if provided, otherwise use request_id, otherwise generate new
        self.trace_id = trace_id or request_id or str(uuid.uuid4())
        self.req_token = None
        self.trace_token = None

    def __enter__(self):
        self.req_token = _request_id_ctx_var.set(self.trace_id)
        self.trace_token = _trace_id_ctx_var.set(self.trace_id)
        return self.trace_id

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.req_token:
            _request_id_ctx_var.reset(self.req_token)
        if self.trace_token:
            _trace_id_ctx_var.reset(self.trace_token)


class RequestIdFilter(logging.Filter):
    """Logging filter to inject request_id/trace_id into log records."""

    def filter(self, record):
        trace_id = get_trace_id()
        record.request_id = trace_id if trace_id else "-"
        record.trace_id = trace_id if trace_id else "-"
        return True


def configure_logging_with_trace() -> None:
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
