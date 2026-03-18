import threading
import time


class RateLimitExceededException(Exception):
    """Raised when rate limit is exceeded."""

    pass


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, capacity: int, fill_rate: float):
        """
        :param capacity: Max tokens in the bucket.
        :param fill_rate: Tokens added per second.
        """
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self.fill_rate = float(fill_rate)
        self._last_update_time = time.time()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1, blocking: bool = False, timeout: float = -1) -> bool:
        """
        Acquire tokens from the bucket.
        :param tokens: Number of tokens to acquire.
        :param blocking: Whether to block if tokens are not available.
        :param timeout: Max time to wait if blocking. -1 means infinite.
        :return: True if acquired, False otherwise (if not blocking or timeout reached).
        """
        if not blocking:
            return self._try_acquire(tokens)

        start_time = time.time()
        while True:
            if self._try_acquire(tokens):
                return True
            if timeout >= 0 and time.time() - start_time >= timeout:
                return False
            time.sleep(0.01)  # brief sleep to avoid busy waiting

    def _try_acquire(self, tokens: int) -> bool:
        with self._lock:
            now = time.time()
            # Add tokens based on elapsed time
            elapsed = now - self._last_update_time
            self._tokens = min(self.capacity, self._tokens + elapsed * self.fill_rate)
            self._last_update_time = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False
