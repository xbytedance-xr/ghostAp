import time
import threading
from typing import Optional
from collections import OrderedDict


class MessageProjectMapper:
    def __init__(self, ttl: int = 86400, max_size: int = 10000):
        self._map: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._ttl = ttl
        self._max_size = max_size
        self._lock = threading.Lock()

    def register(self, message_id: str, project_id: str):
        with self._lock:
            current_time = time.time()
            self._map[message_id] = (project_id, current_time)
            self._cleanup()

    def get_project_id(self, message_id: str) -> Optional[str]:
        with self._lock:
            if message_id not in self._map:
                return None

            project_id, timestamp = self._map[message_id]
            if time.time() - timestamp > self._ttl:
                del self._map[message_id]
                return None

            return project_id

    def _cleanup(self):
        current_time = time.time()

        expired_keys = []
        for msg_id, (_, timestamp) in self._map.items():
            if current_time - timestamp > self._ttl:
                expired_keys.append(msg_id)
            else:
                break

        for key in expired_keys:
            del self._map[key]

        while len(self._map) > self._max_size:
            self._map.popitem(last=False)

    def clear(self):
        with self._lock:
            self._map.clear()

    def __len__(self) -> int:
        return len(self._map)
