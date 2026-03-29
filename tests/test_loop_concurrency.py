import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from src.loop_engine.engine import LoopEngine
from src.engine_base import EngineRunState

class TestLoopConcurrency(unittest.TestCase):
    def setUp(self):
        self.engine = LoopEngine("chat_1", "/tmp/test")
        self.engine.settings = MagicMock()
        self.engine.settings.loop_max_iterations = 10
        self.engine._session = MagicMock()

    def test_concurrent_stop(self):
        # Simulate execute running in one thread (conceptually)
        with self.engine._lock:
            self.engine._run_state = EngineRunState.RUNNING
        
        def stop_thread():
            time.sleep(0.1)
            self.engine.stop()
            
        t = threading.Thread(target=stop_thread)
        t.start()
        
        # Simulate loop check
        start = time.time()
        stopped = False
        while time.time() - start < 1.0:
            with self.engine._lock:
                if self.engine._run_state != EngineRunState.RUNNING:
                    stopped = True
                    break
            time.sleep(0.01)
            
        t.join()
        self.assertTrue(stopped)
        self.assertEqual(self.engine.run_state, EngineRunState.STOPPING)

    def test_watchdog(self):
        with self.engine._lock:
            self.engine._run_state = EngineRunState.RUNNING
            self.engine._last_heartbeat = time.time() - 10.0
            
        # Threshold 5s, last activity 10s ago -> stalled
        self.assertTrue(self.engine.check_stalled(threshold=5.0))
        
        # Update heartbeat
        with self.engine._lock:
            self.engine._last_heartbeat = time.time()
            
        self.assertFalse(self.engine.check_stalled(threshold=5.0))

    def test_inject_guidance_concurrent(self):
        # Multiple threads injecting guidance
        def injector(i):
            for _ in range(100):
                self.engine.inject_guidance(f"msg {i}")
                
        threads = [threading.Thread(target=injector, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        self.assertEqual(len(self.engine._user_guidance), 500)

if __name__ == "__main__":
    unittest.main()
