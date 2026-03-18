import unittest

from src.card.flow_control import FlowControlConfig, FlowControlState, FlowControlStrategy


class TestFlowControlStrategy(unittest.TestCase):
    def setUp(self):
        self.config = FlowControlConfig(
            base_interval_s=0.3, max_interval_s=2.0, low_rate_threshold=20.0, high_rate_threshold=150.0, ema_alpha=0.3
        )
        self.strategy = FlowControlStrategy(self.config)
        self.state = FlowControlState()

    def test_initial_state(self):
        self.assertEqual(self.state.min_update_interval_s, 0.3)
        self.assertEqual(self.state.content_arrival_rate, 0.0)

    def test_low_rate_keeps_base_interval(self):
        # Update with small content over large time -> low rate
        # Rate = 10 chars / 1.0 sec = 10.0 (<= 20.0)
        self.state.last_arrival_time = 100.0
        self.strategy.update_rate(self.state, 101.0, 10)

        # Rate calculation: 0.7*0 + 0.3*10 = 3.0
        self.assertAlmostEqual(self.state.content_arrival_rate, 3.0)
        self.assertEqual(self.state.min_update_interval_s, 0.3)

    def test_high_rate_increases_interval(self):
        # Update with large content over small time -> high rate
        # Rate = 200 chars / 0.1 sec = 2000.0
        self.state.last_arrival_time = 100.0
        self.strategy.update_rate(self.state, 100.1, 200)

        # Rate calculation: 0.7*0 + 0.3*2000 = 600.0
        self.assertAlmostEqual(self.state.content_arrival_rate, 600.0)
        # Should hit max interval
        self.assertEqual(self.state.min_update_interval_s, 2.0)

    def test_mid_rate_interpolates_interval(self):
        # Target a rate in the middle, say ~85
        # Range 20-150 (span 130). 85 is exactly mid.
        # Interval 0.3-2.0 (span 1.7). Mid is 0.3 + 0.85 = 1.15

        # We need to force the state rate to 85 directly to test interpolation logic
        # strictly speaking update_rate updates both rate and interval

        # Let's manually set a rate that will result in ~85 after update
        # current=0. New rate R. 0.3*R = 85 => R = 283.33
        self.state.last_arrival_time = 100.0
        self.strategy.update_rate(self.state, 101.0, 283)

        # Rate should be ~84.9
        self.assertTrue(20.0 < self.state.content_arrival_rate < 150.0)

        # Interval should be > 0.3 and < 2.0
        self.assertTrue(0.3 < self.state.min_update_interval_s < 2.0)

    def test_update_skips_invalid_time_delta(self):
        self.state.last_arrival_time = 100.0
        original_rate = self.state.content_arrival_rate

        # Very small delta_t
        self.strategy.update_rate(self.state, 100.001, 10)

        self.assertEqual(self.state.content_arrival_rate, original_rate)
        self.assertEqual(self.state.last_arrival_time, 100.001)


if __name__ == "__main__":
    unittest.main()
