"""Review metrics exporter framework.

Provides a pluggable metrics export interface for review exception metrics.
Default implementation logs via stdlib logger (preserving original behaviour).
Alternative ``JsonLinesExporter`` writes to a JSON Lines file for external
monitoring system ingestion (Prometheus/Grafana/StatsD adapters).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol (interface)
# ---------------------------------------------------------------------------

@runtime_checkable
class ReviewMetricsExporter(Protocol):
    """Pluggable metrics export interface."""

    def export_metrics(self, metrics: dict, *, prefix: str = "") -> None:
        """Export a single review metrics dict.

        Parameters
        ----------
        metrics : dict
            Structured metrics dict (metric_type, engine, fail_reason, …).
        prefix : str
            Log prefix, e.g. ``"[Spec]"`` or ``"[Loop]"``.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

class LoggerExporter:
    """Exports metrics via ``logger.info`` — the original default behaviour."""

    def export_metrics(self, metrics: dict, *, prefix: str = "") -> None:
        try:
            logger.info(
                "%s review_metrics: %s",
                prefix,
                json.dumps(metrics, ensure_ascii=False),
            )
        except Exception:
            pass


class JsonLinesExporter:
    """Appends each metrics dict as a JSON line to a file.

    Designed for ingestion by log collectors (Filebeat, Fluentd, etc.)
    that can forward to Prometheus / Grafana / StatsD.

    Parameters
    ----------
    path : str
        File path for JSON Lines output.  Parent directories are created
        automatically.  Defaults to ``review_metrics.jsonl`` in the
        current working directory.
    """

    def __init__(self, path: str = "review_metrics.jsonl") -> None:
        self._path = path
        _dir = os.path.dirname(path)
        if _dir:
            os.makedirs(_dir, exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def export_metrics(self, metrics: dict, *, prefix: str = "") -> None:
        try:
            line = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.debug(
                "JsonLinesExporter write failed: %s",
                str(Exception) or repr(Exception),
            )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_exporter_instance: ReviewMetricsExporter | None = None


def get_metrics_exporter(exporter_type: str = "logger", **kwargs: object) -> ReviewMetricsExporter:
    """Return a singleton metrics exporter based on *exporter_type*.

    Supported types:
    - ``"logger"`` (default): :class:`LoggerExporter`
    - ``"jsonl"``: :class:`JsonLinesExporter` (pass ``path=`` kwarg)

    The singleton is cached after the first call.  Pass a different
    *exporter_type* to re-create (useful in tests).
    """
    global _exporter_instance
    if _exporter_instance is not None:
        return _exporter_instance

    if exporter_type == "jsonl":
        _exporter_instance = JsonLinesExporter(**kwargs)  # type: ignore[arg-type]
    else:
        _exporter_instance = LoggerExporter()
    return _exporter_instance


def reset_metrics_exporter() -> None:
    """Reset the singleton — primarily for tests."""
    global _exporter_instance
    _exporter_instance = None
