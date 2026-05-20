import argparse
import concurrent.futures
import logging
import os
import random
import sys
import time
from unittest.mock import MagicMock, patch

# Add src to python path to import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.deep_engine.engine import DeepEngine, DeepEngineCallbacks

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("stress_test.log")
    ]
)
logger = logging.getLogger("StressTest")

class MockSession:
    def __init__(self, *args, **kwargs):
        self.stop_reason = "end_turn"

    def send_prompt(self, prompt, on_event=None, timeout=None):
        # Simulate some processing time
        time.sleep(random.uniform(0.1, 0.5))

        # Simulate events if callback provided
        if on_event:
            mock_event = MagicMock()
            mock_event.event_type = "text_chunk"
            mock_event.text = "Simulated response..."
            on_event(mock_event)

        mock_result = MagicMock()
        mock_result.stop_reason = "end_turn"
        return mock_result

    def cancel(self):
        pass

    def close(self):
        pass

def run_deep_task(task_id: int):
    logger.info(f"Starting Deep Task {task_id}")
    try:
        # Mock create_engine_session to return our MockSession
        # Also mock psutil to trigger memory warning
        mock_process = MagicMock()
        mock_process.memory_percent.side_effect = [85.0, 70.0]

        with patch("src.deep_engine.engine.create_engine_session", return_value=MockSession()), \
             patch("psutil.Process", return_value=mock_process):

            engine = DeepEngine(
                chat_id=f"stress_chat_{task_id}",
                root_path=f"/tmp/stress_test/deep_{task_id}",
                agent_type="coco"
            )

            # Reset last check to ensure it runs
            engine._last_mem_check = 0.0

            callbacks = DeepEngineCallbacks(
                on_project_done=lambda p: logger.info(f"Deep Task {task_id} Done: {p.status}")
            )

            engine.plan_and_execute(
                requirement_text=f"Stress test requirement for task {task_id}",
                callbacks=callbacks
            )

            # Manually trigger memory check to ensure coverage
            engine._check_memory_and_gc()

            return True
    except Exception as e:
        logger.error(f"Deep Task {task_id} Failed: {e}", exc_info=True)
        return False

def main():
    parser = argparse.ArgumentParser(description="Run stress test for Deep engine")
    parser.add_argument("--duration", type=int, default=60, help="Duration in seconds")
    parser.add_argument("--concurrency", type=int, default=5, help="Number of concurrent tasks")
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock session (default: True)")

    args = parser.parse_args()

    logger.info(f"Starting stress test: duration={args.duration}s, concurrency={args.concurrency}")

    start_time = time.time()
    task_count = 0
    success_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = []

        while time.time() - start_time < args.duration:
            # Submit new tasks if we have capacity
            active_count = len([f for f in futures if not f.done()])
            if active_count < args.concurrency:
                task_id = task_count
                future = executor.submit(run_deep_task, task_id)
                futures.append(future)
                task_count += 1

            # Check completed tasks
            for i, future in enumerate(futures):
                if future.done():
                    try:
                        if future.result():
                            success_count += 1
                    except Exception as e:
                        logger.error(f"Task execution exception: {e}")

            futures = [f for f in futures if not f.done()]
            time.sleep(0.1)

        # Wait for remaining tasks
        logger.info("Time's up. Waiting for pending tasks...")
        concurrent.futures.wait(futures, timeout=5)

    logger.info(f"Stress test completed. Total tasks: {task_count}, Success: {success_count}")

if __name__ == "__main__":
    main()
