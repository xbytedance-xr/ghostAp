import logging
import os
import threading

import psutil

logger = logging.getLogger("sys_monitor")


class SystemMonitor:
    def __init__(self, interval: int = 60):
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = None
        self.process = psutil.Process(os.getpid())

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="sys_monitor")
            self._thread.start()
            logger.info("System monitor started.")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop_event.is_set():
            try:
                mem_info = self.process.memory_info()
                rss_mb = mem_info.rss / (1024 * 1024)
                threads = self.process.num_threads()
                cpu_percent = self.process.cpu_percent()

                logger.info(f"[MONITOR] RSS: {rss_mb:.2f} MB | Threads: {threads} | CPU: {cpu_percent}%")
            except Exception as e:
                logger.error(f"Error in system monitor: {e}")

            self._stop_event.wait(self.interval)


# Singleton instance
_monitor = None


def start_monitor(interval: int = 300):
    global _monitor
    if _monitor is None:
        _monitor = SystemMonitor(interval=interval)
        _monitor.start()
