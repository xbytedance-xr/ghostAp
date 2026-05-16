import collections
import gc
import logging
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

class MemorySnapshot:
    """
    Utility class to snapshot memory objects and calculate differences
    to help identify memory leaks.
    """

    def __init__(self):
        self.last_counts: Dict[str, int] = collections.defaultdict(int)

    def take_snapshot(self) -> Dict[str, int]:
        """
        Take a snapshot of current object counts by type.
        Returns the current counts.
        """
        counts = collections.defaultdict(int)
        # gc.get_objects() returns a list of all objects tracked by the garbage collector.
        # This can be slow for very large heaps, so use with caution in production.
        objects = gc.get_objects()
        for obj in objects:
            try:
                type_name = type(obj).__name__
                counts[type_name] += 1
            except Exception:
                # Some objects might not have a clean __name__ or accessing it might fail
                continue
        return counts

    def get_growth_diff(self, limit: int = 10) -> List[Tuple[str, int, int]]:
        """
        Calculate the difference between the current memory state and the last snapshot.
        Returns a list of (type_name, count_diff, current_count) tuples,
        sorted by count_diff descending.

        Args:
            limit: Number of top growing object types to return.
        """
        current_counts = self.take_snapshot()
        diff = []

        # Calculate diff for existing and new types
        for type_name, count in current_counts.items():
            prev_count = self.last_counts.get(type_name, 0)
            delta = count - prev_count
            if delta != 0:
                diff.append((type_name, delta, count))

        # Also check for types that disappeared (though usually we care about growth)
        for type_name, prev_count in self.last_counts.items():
            if type_name not in current_counts:
                diff.append((type_name, -prev_count, 0))

        # Sort by growth (delta) descending
        diff.sort(key=lambda x: x[1], reverse=True)

        # Update last_counts for the next call
        self.last_counts = current_counts

        return diff[:limit]

    def log_growth(self, limit: int = 10, logger_func=logger.info):
        """
        Log the top growing objects.
        """
        diff = self.get_growth_diff(limit)
        if not diff:
            return

        logger_func(f"[Memory Snapshot] Top {limit} growing object types:")
        for type_name, delta, current in diff:
            sign = "+" if delta > 0 else ""
            logger_func(f"  {type_name}: {sign}{delta} (Total: {current})")
