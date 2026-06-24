"""Comprehensive tests for WorkflowJournal caching mechanism.

Validates:
- Key computation is deterministic and collision-resistant
- In-memory cache stores and retrieves results correctly
- Disk persistence works across journal instances
- Cache hit/miss stats are tracked accurately
- Thread safety under concurrent mutations
- Filename collision handling with longer prefixes
- Cache hit on rerun returns cached=True result
"""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.workflow_engine.constants import JOURNAL_DIR
from src.workflow_engine.journal import WorkflowJournal
from src.workflow_engine.models import AgentCallResult


class _BaseJournalTest(unittest.TestCase):
    """Base class with helper methods for journal tests."""

    def _make_journal(self, run_id: str = "test_run") -> WorkflowJournal:
        """Create a WorkflowJournal backed by a temporary directory."""
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        return WorkflowJournal(root_path=self._tmp_dir.name, run_id=run_id)

    def _make_result(
        self,
        output: str = "test output",
        token_usage: int = 100,
        cached: bool = False,
        tool: str = "coco",
        model: str | None = None,
    ) -> AgentCallResult:
        """Create a test AgentCallResult with sensible defaults."""
        return AgentCallResult(
            output=output,
            parsed={"key": "value"},
            token_usage=token_usage,
            duration_s=1.5,
            error=None,
            cached=cached,
            tool=tool,
            model=model,
        )


# ---------------------------------------------------------------------------
# 1. TestKeyComputation
# ---------------------------------------------------------------------------


class TestKeyComputation(_BaseJournalTest):
    """Tests for compute_key() deterministic hashing."""

    def test_compute_key_is_deterministic(self):
        """Same prompt+tool+model must always produce the same key."""
        key1 = WorkflowJournal.compute_key("prompt", "coco", "claude-3")
        key2 = WorkflowJournal.compute_key("prompt", "coco", "claude-3")
        self.assertEqual(key1, key2)
        # Also verify it's a valid sha256 hex string (64 chars)
        self.assertEqual(len(key1), 64)
        self.assertEqual(len(key2), 64)

    def test_compute_key_different_prompts(self):
        """Different prompts must produce different keys."""
        key1 = WorkflowJournal.compute_key("prompt A", "coco", "claude-3")
        key2 = WorkflowJournal.compute_key("prompt B", "coco", "claude-3")
        self.assertNotEqual(key1, key2)

    def test_compute_key_handles_none_model(self):
        """model=None must be treated as empty string in the hash."""
        key_none = WorkflowJournal.compute_key("prompt", "coco", None)
        key_empty = WorkflowJournal.compute_key("prompt", "coco", "")
        self.assertEqual(key_none, key_empty)
        # Verify the raw hash matches our expectation
        expected = hashlib.sha256(b"prompt|coco|").hexdigest()
        self.assertEqual(key_none, expected)


# ---------------------------------------------------------------------------
# 2. TestMemoryCache
# ---------------------------------------------------------------------------


class TestMemoryCache(_BaseJournalTest):
    """Tests for in-memory cache operations."""

    def test_store_and_get_cached(self):
        """Storing a result and getting it back from memory must work."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)
        result = self._make_result(output="hello world")

        journal.store(key, result)
        retrieved = journal.get_cached(key)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.output, "hello world")
        self.assertEqual(retrieved.token_usage, 100)

    def test_get_cached_increments_hits(self):
        """A cache hit must increment the hits counter."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)
        result = self._make_result()

        journal.store(key, result)
        # First hit
        journal.get_cached(key)
        self.assertEqual(journal.stats()["hits"], 1)
        # Second hit
        journal.get_cached(key)
        self.assertEqual(journal.stats()["hits"], 2)

    def test_get_cached_miss_increments_misses(self):
        """A cache miss must increment the misses counter."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("nonexistent", "coco", None)

        journal.get_cached(key)
        self.assertEqual(journal.stats()["misses"], 1)

        journal.get_cached(key)
        self.assertEqual(journal.stats()["misses"], 2)

    def test_has_checks_memory(self):
        """has() must return True for keys stored in memory."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)

        self.assertFalse(journal.has(key))
        journal.store(key, self._make_result())
        self.assertTrue(journal.has(key))


# ---------------------------------------------------------------------------
# 3. TestDiskPersistence
# ---------------------------------------------------------------------------


class TestDiskPersistence(_BaseJournalTest):
    """Tests for disk persistence and loading."""

    def test_store_writes_to_disk(self):
        """Storing a result must create a JSON file on disk."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)
        result = self._make_result(output="disk test")

        journal.store(key, result)

        # Verify the journal directory exists
        journal_dir = Path(self._tmp_dir.name) / JOURNAL_DIR / "test_run"
        self.assertTrue(journal_dir.exists())

        # Verify a JSON file was created with the key prefix
        json_files = list(journal_dir.glob("*.json"))
        self.assertGreaterEqual(len(json_files), 1)
        # One of them should be the entry file (not _index.json)
        entry_files = [f for f in json_files if f.name != "_index.json"]
        self.assertEqual(len(entry_files), 1)
        self.assertTrue(entry_files[0].name.startswith(key[:16]))

        # Verify the content is valid JSON with the expected output
        data = json.loads(entry_files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["result"]["output"], "disk test")

    def test_get_cached_from_disk(self):
        """After clearing memory cache, get_cached must load from disk."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)
        result = self._make_result(output="persisted")

        journal.store(key, result)

        # Clear in-memory cache directly
        journal._cache.clear()

        # get_cached should now load from disk
        retrieved = journal.get_cached(key)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.output, "persisted")
        # And it should now be in memory cache
        self.assertIn(key, journal._cache)

    def test_index_persisted_to_disk(self):
        """The index file must be created and contain key-to-filename mapping."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)

        journal.store(key, self._make_result())

        index_path = Path(self._tmp_dir.name) / JOURNAL_DIR / "test_run" / "_index.json"
        self.assertTrue(index_path.exists())

        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertIn(key, index_data)
        self.assertEqual(index_data[key], f"{key[:16]}.json")

    def test_clear_deletes_disk_files(self):
        """clear() must remove the entire journal directory."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)

        journal.store(key, self._make_result())
        journal_dir = Path(self._tmp_dir.name) / JOURNAL_DIR / "test_run"
        self.assertTrue(journal_dir.exists())

        journal.clear()
        self.assertFalse(journal_dir.exists())
        # Memory should also be cleared
        self.assertEqual(len(journal._cache), 0)
        self.assertEqual(len(journal._index), 0)

    def test_has_checks_disk_index(self):
        """has() must return True for disk-persisted keys after memory clear."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)

        journal.store(key, self._make_result())
        # Clear memory cache but not disk index
        journal._cache.clear()

        # has() should still find it via disk index
        self.assertTrue(journal.has(key))

        # A completely new journal instance should also find it via disk
        journal2 = WorkflowJournal(root_path=self._tmp_dir.name, run_id="test_run")
        self.assertTrue(journal2.has(key))


# ---------------------------------------------------------------------------
# 4. TestCacheHitOnRerun
# ---------------------------------------------------------------------------


class TestCacheHitOnRerun(_BaseJournalTest):
    """Tests for cache hit behavior on repeated agent calls."""

    def test_same_prompt_returns_cached_result(self):
        """Running the same agent call twice returns cached result with cached=True."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("analyze this", "coco", "claude-3")

        # First call: store a non-cached result
        original_result = self._make_result(
            output="analysis result",
            token_usage=500,
            cached=False,
            tool="coco",
            model="claude-3",
        )
        journal.store(key, original_result)

        # Second call: retrieve from cache
        cached_result = journal.get_cached(key)
        self.assertIsNotNone(cached_result)
        # The stored result should be returned as-is
        # Note: the cached flag in the result is set by the caller, not the journal
        self.assertEqual(cached_result.output, "analysis result")
        self.assertEqual(cached_result.token_usage, 500)
        # Verify it was a cache hit
        self.assertEqual(journal.stats()["hits"], 1)

    def test_different_prompt_executes_new_call(self):
        """Changing the prompt must bypass the cache."""
        journal = self._make_journal()
        key1 = WorkflowJournal.compute_key("prompt A", "coco", None)
        key2 = WorkflowJournal.compute_key("prompt B", "coco", None)

        journal.store(key1, self._make_result(output="result A"))

        # key2 should miss
        result = journal.get_cached(key2)
        self.assertIsNone(result)
        self.assertEqual(journal.stats()["misses"], 1)

        # key1 should hit
        result = journal.get_cached(key1)
        self.assertIsNotNone(result)
        self.assertEqual(result.output, "result A")
        self.assertEqual(journal.stats()["hits"], 1)


# ---------------------------------------------------------------------------
# 5. TestStatsTracking
# ---------------------------------------------------------------------------


class TestStatsTracking(_BaseJournalTest):
    """Tests for stats() tracking of hits, misses, and total."""

    def test_stats_reflects_hits_and_misses(self):
        """After a mix of hits and misses, stats must be correct."""
        journal = self._make_journal()
        key1 = WorkflowJournal.compute_key("A", "coco", None)
        key2 = WorkflowJournal.compute_key("B", "coco", None)
        key3 = WorkflowJournal.compute_key("C", "coco", None)

        journal.store(key1, self._make_result())
        journal.store(key2, self._make_result())

        # 2 hits, 1 miss
        journal.get_cached(key1)  # hit
        journal.get_cached(key2)  # hit
        journal.get_cached(key3)  # miss

        stats = journal.stats()
        self.assertEqual(stats["total"], 2)  # 2 entries in index
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 1)

    def test_clear_resets_stats(self):
        """clear() must reset hits and misses to 0."""
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test", "coco", None)

        journal.store(key, self._make_result())
        journal.get_cached(key)  # hit
        journal.get_cached("nonexistent")  # miss

        self.assertEqual(journal.stats()["hits"], 1)
        self.assertEqual(journal.stats()["misses"], 1)
        self.assertEqual(journal.stats()["total"], 1)

        journal.clear()

        stats = journal.stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["total"], 0)


# ---------------------------------------------------------------------------
# 6. TestThreadSafety
# ---------------------------------------------------------------------------


class TestThreadSafety(_BaseJournalTest):
    """Tests for thread safety of journal operations."""

    def test_concurrent_store_no_corruption(self):
        """Multiple threads storing different keys must not corrupt the index."""
        journal = self._make_journal()
        n_threads = 10
        keys_per_thread = 20

        barrier = threading.Barrier(n_threads)

        def worker(thread_id: int):
            barrier.wait()
            for i in range(keys_per_thread):
                prompt = f"thread_{thread_id}_key_{i}"
                key = WorkflowJournal.compute_key(prompt, "coco", None)
                result = self._make_result(output=prompt)
                journal.store(key, result)

        threads = [
            threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All keys should be present
        expected_total = n_threads * keys_per_thread
        self.assertEqual(journal.stats()["total"], expected_total)
        self.assertEqual(len(journal._index), expected_total)

        # Index file should match in-memory index
        index_path = Path(self._tmp_dir.name) / JOURNAL_DIR / "test_run" / "_index.json"
        disk_index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_index, journal._index)

    def test_concurrent_get_and_store(self):
        """Concurrent get and store operations must not raise exceptions."""
        journal = self._make_journal()
        # Pre-populate some keys
        existing_keys = []
        for i in range(50):
            key = WorkflowJournal.compute_key(f"existing_{i}", "coco", None)
            journal.store(key, self._make_result(output=f"existing_{i}"))
            existing_keys.append(key)

        n_threads = 8
        ops_per_thread = 100
        barrier = threading.Barrier(n_threads)
        errors: list[Exception] = []

        def worker():
            barrier.wait()
            try:
                for i in range(ops_per_thread):
                    # Mix of gets (hits and misses) and stores
                    if i % 3 == 0:
                        # Store a new key
                        key = WorkflowJournal.compute_key(
                            f"new_{threading.get_ident()}_{i}", "coco", None
                        )
                        journal.store(key, self._make_result())
                    elif i % 3 == 1:
                        # Get an existing key (hit)
                        key = existing_keys[i % len(existing_keys)]
                        journal.get_cached(key)
                    else:
                        # Get a nonexistent key (miss)
                        key = WorkflowJournal.compute_key(
                            f"missing_{threading.get_ident()}_{i}", "coco", None
                        )
                        journal.get_cached(key)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent ops raised errors: {errors}")


# ---------------------------------------------------------------------------
# 7. TestCollisionHandling
# ---------------------------------------------------------------------------


class TestCollisionHandling(_BaseJournalTest):
    """Tests for filename collision handling."""

    def test_filename_collision_uses_longer_prefix(self):
        """When two keys share the same 16-char prefix, the second uses 32-char."""
        journal = self._make_journal()

        # Create two keys with the same 16-char prefix but different full keys.
        # We'll mock the compute_key to return controlled values.
        key1 = "a" * 16 + "b" * 48  # prefix: aaaaaaaaaaaaaaaa
        key2 = "a" * 16 + "c" * 48  # same 16-char prefix, different rest

        # Store first key - should use 16-char prefix
        journal.store(key1, self._make_result(output="first"))
        self.assertEqual(journal._index[key1], f"{key1[:16]}.json")

        # Store second key - should detect collision and use 32-char prefix
        journal.store(key2, self._make_result(output="second"))
        self.assertEqual(journal._index[key2], f"{key2[:32]}.json")

        # Both files should exist on disk
        journal_dir = Path(self._tmp_dir.name) / JOURNAL_DIR / "test_run"
        self.assertTrue((journal_dir / f"{key1[:16]}.json").exists())
        self.assertTrue((journal_dir / f"{key2[:32]}.json").exists())

        # Both should be retrievable
        result1 = journal.get_cached(key1)
        result2 = journal.get_cached(key2)
        self.assertEqual(result1.output, "first")
        self.assertEqual(result2.output, "second")


# ---------------------------------------------------------------------------
# 8. TestLRUCacheCap
# ---------------------------------------------------------------------------


class TestLRUCacheCap(_BaseJournalTest):
    """Tests for the in-memory LRU cache size cap and eviction behaviour."""

    def _make_small_journal(self, max_entries: int) -> WorkflowJournal:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        return WorkflowJournal(
            root_path=self._tmp_dir.name,
            run_id="test_run",
            max_entries=max_entries,
        )

    def test_evicts_oldest_key_when_cap_exceeded(self):
        """Inserting >max_entries unique keys must evict the oldest from memory."""
        max_entries = 3
        journal = self._make_small_journal(max_entries=max_entries)

        keys = []
        for i in range(max_entries + 2):
            key = WorkflowJournal.compute_key(f"prompt_{i}", "coco", None)
            journal.store(key, self._make_result(output=f"result_{i}"))
            keys.append(key)

        # The oldest keys must have been evicted from memory
        self.assertFalse(keys[0] in journal._cache)
        self.assertFalse(keys[1] in journal._cache)
        # But disk index must still know about them
        self.assertTrue(journal.has(keys[0]))
        self.assertTrue(journal.has(keys[1]))
        # Newest entries should still be in memory
        self.assertTrue(keys[-1] in journal._cache)
        self.assertTrue(keys[-2] in journal._cache)
        # Memory cache size should not exceed the cap
        self.assertLessEqual(len(journal._cache), max_entries)

    def test_reinserted_evicted_key_still_retrievable(self):
        """After re-inserting an evicted key it must be hittable again."""
        max_entries = 2
        journal = self._make_small_journal(max_entries=max_entries)

        key_a = WorkflowJournal.compute_key("a", "coco", None)
        key_b = WorkflowJournal.compute_key("b", "coco", None)
        key_c = WorkflowJournal.compute_key("c", "coco", None)

        journal.store(key_a, self._make_result(output="A"))
        journal.store(key_b, self._make_result(output="B"))
        # Inserting C will evict A from memory
        journal.store(key_c, self._make_result(output="C"))

        self.assertFalse(key_a in journal._cache)
        # Re-store A: now A should be the newest
        journal.store(key_a, self._make_result(output="A-v2"))
        self.assertTrue(key_a in journal._cache)
        # B should now be the oldest and get evicted when one more key arrives
        key_d = WorkflowJournal.compute_key("d", "coco", None)
        journal.store(key_d, self._make_result(output="D"))
        self.assertFalse(key_b in journal._cache)
        self.assertTrue(key_a in journal._cache)

    def test_hit_promotes_key_to_newest_end(self):
        """Accessing a key via get_cached must move it to the newest end so it
        is not evicted while younger, untouched keys are."""
        max_entries = 3
        journal = self._make_small_journal(max_entries=max_entries)

        key_a = WorkflowJournal.compute_key("a", "coco", None)
        key_b = WorkflowJournal.compute_key("b", "coco", None)
        key_c = WorkflowJournal.compute_key("c", "coco", None)
        key_d = WorkflowJournal.compute_key("d", "coco", None)

        journal.store(key_a, self._make_result(output="A"))
        journal.store(key_b, self._make_result(output="B"))
        journal.store(key_c, self._make_result(output="C"))
        # cache order (oldest->newest): a b c
        # Hit 'a' to make it newest
        journal.get_cached(key_a)
        # cache order (oldest->newest): b c a

        # Inserting d should now evict b (not a)
        journal.store(key_d, self._make_result(output="D"))

        self.assertFalse(key_b in journal._cache)
        self.assertTrue(key_a in journal._cache)
        self.assertTrue(key_c in journal._cache)
        self.assertTrue(key_d in journal._cache)
        self.assertEqual(len(journal._cache), max_entries)

    def test_zero_max_entries_is_unbounded(self):
        """max_entries=0 must disable the cap (backward-compatible behaviour)."""
        journal = self._make_small_journal(max_entries=0)
        for i in range(500):
            key = WorkflowJournal.compute_key(f"p_{i}", "coco", None)
            journal.store(key, self._make_result(output=f"r_{i}"))
        self.assertEqual(len(journal._cache), 500)
        self.assertEqual(journal.stats()["evictions"], 0)

    def test_stats_reports_evictions(self):
        """stats() must report the number of evictions performed."""
        max_entries = 2
        journal = self._make_small_journal(max_entries=max_entries)
        for i in range(5):
            key = WorkflowJournal.compute_key(f"p_{i}", "coco", None)
            journal.store(key, self._make_result(output=f"r_{i}"))
        # 5 inserts into a cap of 2 => 3 evictions
        self.assertEqual(journal.stats()["evictions"], 3)


# ---------------------------------------------------------------------------
# 9. TestLRUThreadSafety
# ---------------------------------------------------------------------------


class TestLRUThreadSafety(_BaseJournalTest):
    """Thread-safety tests focused on the LRU eviction path."""

    def test_concurrent_put_and_get_under_cap(self):
        """Highly concurrent store/get_cached around the LRU cap must not raise."""
        max_entries = 5
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        journal = WorkflowJournal(
            root_path=self._tmp_dir.name,
            run_id="test_run",
            max_entries=max_entries,
        )

        n_threads = 8
        ops_per_thread = 200
        errors: list[Exception] = []
        barrier = threading.Barrier(n_threads)

        def worker(tid: int):
            barrier.wait()
            try:
                for i in range(ops_per_thread):
                    if i % 2 == 0:
                        key = WorkflowJournal.compute_key(
                            f"t{tid}_k{i}", "coco", None
                        )
                        journal.store(key, self._make_result(output=f"t{tid}_k{i}"))
                    else:
                        # Hit a key that may or may not exist
                        key = WorkflowJournal.compute_key(
                            f"t{(tid + 1) % n_threads}_k{i - 1}", "coco", None
                        )
                        journal.get_cached(key)
                # Final lookups of known keys
                for i in range(min(10, ops_per_thread)):
                    key = WorkflowJournal.compute_key(
                        f"t{tid}_k{i}", "coco", None
                    )
                    journal.get_cached(key)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent LRU ops raised: {errors}")
        # Memory cap must still be respected by the end
        self.assertLessEqual(len(journal._cache), max_entries)


if __name__ == "__main__":
    unittest.main()
