"""Flow control strategy for streaming updates.

This module encapsulates the adaptive flow control logic used to dynamically
adjust update intervals based on content arrival rates.
"""

from dataclasses import dataclass, field


@dataclass
class FlowControlConfig:
    """Configuration for flow control strategy."""
    base_interval_s: float = 0.3        # Baseline (fastest)
    max_interval_s: float = 2.0         # Max limit (slowest)
    low_rate_threshold: float = 20.0    # chars/sec below which use base_interval
    high_rate_threshold: float = 150.0  # chars/sec above which use max_interval
    ema_alpha: float = 0.3              # Exponential Moving Average factor


@dataclass
class FlowControlState:
    """State for flow control calculation."""
    content_arrival_rate: float = 0.0   # chars/sec (EMA)
    last_arrival_time: float = 0.0      # Timestamp of last data arrival
    min_update_interval_s: float = 0.3  # Current dynamic interval


class FlowControlStrategy:
    """Adaptive flow control strategy."""

    def __init__(self, config: FlowControlConfig):
        self.config = config

    def update_rate(self, state: FlowControlState, current_time: float, delta_content_len: int) -> None:
        """Update the arrival rate and calculate the new minimum update interval."""
        
        # 1. Calculate Instant Rate & EMA
        if state.last_arrival_time > 0:
            delta_t = current_time - state.last_arrival_time
            
            if delta_t > 0.01 and delta_content_len >= 0:
                instant_rate = delta_content_len / delta_t
                # Exponential Moving Average
                state.content_arrival_rate = (
                    (1 - self.config.ema_alpha) * state.content_arrival_rate + 
                    self.config.ema_alpha * instant_rate
                )
        
        state.last_arrival_time = current_time

        # 2. Adjust Interval based on Rate
        low = self.config.low_rate_threshold
        high = self.config.high_rate_threshold
        base = self.config.base_interval_s
        max_int = self.config.max_interval_s

        if state.content_arrival_rate <= low:
            state.min_update_interval_s = base
        elif state.content_arrival_rate >= high:
            state.min_update_interval_s = max_int
        else:
            # Linear interpolation
            ratio = (state.content_arrival_rate - low) / (high - low)
            state.min_update_interval_s = base + ratio * (max_int - base)
