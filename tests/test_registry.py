import unittest
from unittest.mock import MagicMock
from src.utils.registry import ServiceRegistry, ServiceLifecycle, CleanupRegistry

class TestServiceRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ServiceRegistry()

    def test_register_instance(self):
        obj = {"key": "value"}
        self.registry.register_instance("my_service", obj)
        self.assertEqual(self.registry.get("my_service"), obj)

    def test_register_factory_lazy(self):
        call_count = 0
        def factory():
            nonlocal call_count
            call_count += 1
            return {"instance": call_count}

        self.registry.register_factory("lazy_service", factory)
        self.assertEqual(call_count, 0)
        
        inst1 = self.registry.get("lazy_service")
        self.assertEqual(inst1["instance"], 1)
        self.assertEqual(call_count, 1)
        
        inst2 = self.registry.get("lazy_service")
        self.assertEqual(inst2, inst1)
        self.assertEqual(call_count, 1)

    def test_duplicate_registration(self):
        self.registry.register_instance("service", 1)
        with self.assertRaises(ValueError):
            self.registry.register_instance("service", 2)

    def test_missing_service(self):
        with self.assertRaises(KeyError):
            self.registry.get("missing")

    def test_has_service(self):
        self.assertFalse(self.registry.has("service"))
        self.registry.register_instance("service", 1)
        self.assertTrue(self.registry.has("service"))

    def test_reset(self):
        self.registry.register_instance("service", 1)
        self.registry.reset()
        self.assertFalse(self.registry.has("service"))

    # --- Transient lifecycle tests ---
    def test_register_transient(self):
        call_count = 0
        def factory():
            nonlocal call_count
            call_count += 1
            return {"n": call_count}

        self.registry.register_transient("t_service", factory)
        inst1 = self.registry.get("t_service")
        inst2 = self.registry.get("t_service")
        self.assertNotEqual(id(inst1), id(inst2))
        self.assertEqual(inst1["n"], 1)
        self.assertEqual(inst2["n"], 2)
        self.assertEqual(call_count, 2)

    def test_transient_has(self):
        self.assertFalse(self.registry.has("t"))
        self.registry.register_transient("t", lambda: 1)
        self.assertTrue(self.registry.has("t"))

    def test_transient_unregister(self):
        self.registry.register_transient("t", lambda: 1)
        self.assertTrue(self.registry.unregister("t"))
        self.assertFalse(self.registry.has("t"))

    def test_transient_duplicate_blocked(self):
        self.registry.register_transient("t", lambda: 1)
        with self.assertRaises(ValueError):
            self.registry.register_instance("t", 2)

    # --- list_services tests ---
    def test_list_services(self):
        self.registry.register_instance("a", 1)
        self.registry.register_factory("b", lambda: 2)
        self.registry.register_transient("c", lambda: 3)
        services = self.registry.list_services()
        keys = {s["key"] for s in services}
        self.assertEqual(keys, {"a", "b", "c"})
        for s in services:
            if s["key"] == "a":
                self.assertTrue(s["instantiated"])
                self.assertEqual(s["lifecycle"], "singleton")
            elif s["key"] == "b":
                self.assertFalse(s["instantiated"])
            elif s["key"] == "c":
                self.assertEqual(s["lifecycle"], "transient")

    # --- close() tests ---
    def test_close_calls_close_on_instances(self):
        mock_svc = MagicMock()
        self.registry.register_instance("svc", mock_svc)
        self.registry.close()
        mock_svc.close.assert_called_once()

    def test_close_idempotent(self):
        mock_svc = MagicMock()
        self.registry.register_instance("svc", mock_svc)
        self.registry.close()
        self.registry.close()
        mock_svc.close.assert_called_once()

    def test_close_exception_isolation(self):
        bad = MagicMock()
        bad.close.side_effect = RuntimeError("boom")
        good = MagicMock()
        self.registry.register_instance("bad", bad)
        self.registry.register_instance("good", good)
        self.registry.close()
        bad.close.assert_called_once()
        good.close.assert_called_once()

    # --- Scoped registry tests ---
    def test_scoped_registry_parent_lookup(self):
        self.registry.register_instance("root_svc", 42)
        child = self.registry.create_scope("child")
        self.assertEqual(child.get("root_svc"), 42)

    def test_scoped_registry_override(self):
        self.registry.register_instance("svc", 1)
        child = self.registry.create_scope("child")
        child.register_instance("svc", 2)
        self.assertEqual(child.get("svc"), 2)
        self.assertEqual(self.registry.get("svc"), 1)

class TestCleanupRegistry(unittest.TestCase):
    def test_cleanup_order(self):
        log = []
        registry = CleanupRegistry()
        registry.register("first", lambda: log.append(1))
        registry.register("second", lambda: log.append(2))
        registry.register("third", lambda: log.append(3))
        
        registry.cleanup()
        self.assertEqual(log, [3, 2, 1])

    def test_cleanup_once(self):
        log = []
        registry = CleanupRegistry()
        registry.register("only", lambda: log.append(1))
        
        registry.cleanup()
        registry.cleanup()
        self.assertEqual(log, [1])

    def test_register_after_cleanup(self):
        log = []
        registry = CleanupRegistry()
        registry.cleanup()
        
        # should execute immediately
        registry.register("late", lambda: log.append(1))
        self.assertEqual(log, [1])

    def test_cleanup_exception_isolation(self):
        log = []
        registry = CleanupRegistry()
        
        def bad():
            raise ValueError("fail")
            
        registry.register("first", lambda: log.append(1))
        registry.register("bad", bad)
        registry.register("third", lambda: log.append(3))
        
        # bad() should not block "first" from being cleaned up
        registry.cleanup()
        self.assertEqual(log, [3, 1])

    def test_cleanup_priority_ordering(self):
        log = []
        registry = CleanupRegistry()
        registry.register("low", lambda: log.append("low"), priority=200)
        registry.register("high", lambda: log.append("high"), priority=10)
        registry.register("mid", lambda: log.append("mid"), priority=100)
        registry.cleanup()
        self.assertEqual(log, ["high", "mid", "low"])

    def test_unregister_by_name(self):
        log = []
        registry = CleanupRegistry()
        registry.register("keep", lambda: log.append("keep"))
        registry.register("drop", lambda: log.append("drop"))
        self.assertTrue(registry.unregister("drop"))
        registry.cleanup()
        self.assertEqual(log, ["keep"])

    def test_unregister_returns_false_for_missing(self):
        registry = CleanupRegistry()
        self.assertFalse(registry.unregister("nonexistent"))

    def test_count_property(self):
        registry = CleanupRegistry()
        self.assertEqual(registry.count, 0)
        registry.register("a", lambda: None)
        self.assertEqual(registry.count, 1)
        registry.register("b", lambda: None)
        self.assertEqual(registry.count, 2)

    def test_register_returns_unregister_fn(self):
        log = []
        registry = CleanupRegistry()
        unreg = registry.register("task", lambda: log.append(1))
        unreg()
        registry.cleanup()
        self.assertEqual(log, [])

if __name__ == "__main__":
    unittest.main()
