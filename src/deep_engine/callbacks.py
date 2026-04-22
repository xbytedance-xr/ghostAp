import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Default no-op callback
_NOOP: Callable[..., None] = lambda *a, **kw: None


@dataclass
class DeepEngineCallbacks:
    """Callbacks for deep engine lifecycle events.

    All callbacks default to no-ops so callers can register only the ones
    they care about.
    """

    on_start: Callable[..., None] = field(default=_NOOP)
    on_finish: Callable[..., None] = field(default=_NOOP)
    on_error: Callable[..., None] = field(default=_NOOP)
    on_analyzing_start: Callable[..., None] = field(default=_NOOP)
    on_analyzing_done: Callable[..., None] = field(default=_NOOP)

    @property
    def on_planning_start(self):
        return self.on_analyzing_start

    @on_planning_start.setter
    def on_planning_start(self, value):
        self.on_analyzing_start = value

    @property
    def on_planning_done(self):
        return self.on_analyzing_done

    @on_planning_done.setter
    def on_planning_done(self, value):
        self.on_analyzing_done = value
