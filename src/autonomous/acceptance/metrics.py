"""Acceptance metrics: percentile, Wilson confidence, and zero-event calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class PercentileResult:
    p50: float
    p95: float
    p99: float
    sample_count: int


@dataclass(frozen=True)
class WilsonInterval:
    lower: float
    upper: float
    center: float
    confidence: float
    n: int
    successes: int


@dataclass(frozen=True)
class ZeroEventBound:
    upper_bound: float
    confidence: float
    observation_time: float


def percentiles(values: list[float]) -> PercentileResult:
    if not values:
        return PercentileResult(p50=0.0, p95=0.0, p99=0.0, sample_count=0)

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _percentile(p: float) -> float:
        idx = (n - 1) * p
        lower = int(math.floor(idx))
        upper = int(math.ceil(idx))
        if lower == upper:
            return sorted_vals[lower]
        frac = idx - lower
        return sorted_vals[lower] * (1 - frac) + sorted_vals[upper] * frac

    return PercentileResult(
        p50=_percentile(0.50),
        p95=_percentile(0.95),
        p99=_percentile(0.99),
        sample_count=n,
    )


def wilson_confidence_interval(
    successes: int,
    trials: int,
    confidence: float = 0.95,
) -> WilsonInterval:
    """Wilson score interval for binomial proportion."""
    if trials == 0:
        return WilsonInterval(
            lower=0.0, upper=1.0, center=0.5, confidence=confidence, n=0, successes=0
        )

    z_map = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_map.get(confidence, 1.96)

    p_hat = successes / trials
    denominator = 1 + z * z / trials
    center = (p_hat + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(
        (p_hat * (1 - p_hat) + z * z / (4 * trials)) / trials
    ) / denominator

    return WilsonInterval(
        lower=max(0.0, center - margin),
        upper=min(1.0, center + margin),
        center=center,
        confidence=confidence,
        n=trials,
        successes=successes,
    )


def zero_event_upper_bound(
    observation_time: float,
    confidence: float = 0.95,
) -> ZeroEventBound:
    """Upper bound on event rate when zero events observed (Poisson).

    If we observe zero events in T time units, the upper bound on the
    true rate lambda at given confidence is: -ln(1-confidence) / T
    """
    if observation_time <= 0:
        return ZeroEventBound(
            upper_bound=float("inf"),
            confidence=confidence,
            observation_time=observation_time,
        )

    bound = -math.log(1 - confidence) / observation_time
    return ZeroEventBound(
        upper_bound=bound,
        confidence=confidence,
        observation_time=observation_time,
    )
