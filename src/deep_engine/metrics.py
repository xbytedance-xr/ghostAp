import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

@dataclass
class DeepEngineMetrics:
    """Performance metrics for a single DeepEngine execution.
    Part of the unified engine runtime monitoring system.
    """
    trace_id: str
    project_id: str
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    
    # Execution statistics
    tool_calls_total: int = 0
    tool_calls_by_kind: Dict[str, int] = field(default_factory=dict)
    text_chunks_total: int = 0
    plan_updates_total: int = 0
    
    # Outcome
    status: str = "unknown"
    error_type: Optional[str] = None
    
    @property
    def duration(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time

    def record_tool_call(self, kind: Optional[str]):
        self.tool_calls_total += 1
        k = kind or "unknown"
        self.tool_calls_by_kind[k] = self.tool_calls_by_kind.get(k, 0) + 1

    def record_text_chunk(self):
        self.text_chunks_total += 1

    def record_plan_update(self):
        self.plan_updates_total += 1

    def finish(self, status: str, error_type: Optional[str] = None):
        self.end_time = time.time()
        self.status = status
        self.error_type = error_type
        logger.info(
            "[Deep:Metrics] Execution finished: project=%s, duration=%.2fs, tools=%d, status=%s",
            self.project_id, self.duration, self.tool_calls_total, self.status
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "project_id": self.project_id,
            "duration": self.duration,
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_by_kind": self.tool_calls_by_kind,
            "text_chunks_total": self.text_chunks_total,
            "plan_updates_total": self.plan_updates_total,
            "status": self.status,
            "error_type": self.error_type,
            "timestamp": time.time()
        }
