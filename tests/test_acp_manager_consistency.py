import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.acp.manager import ACPSessionManager


class TestACPSessionManagerConsistency(unittest.TestCase):
    def test_ensure_session_restarts_on_agent_mismatch(self):
        manager = ACPSessionManager(agent_type="coco")

        # Mock session 1
        mock_session_1 = MagicMock()
        mock_session_1._agent_type = "coco"
        mock_session_1._agent_args = ["acp", "serve"]
        mock_session_1.last_active = 1000
        mock_session_1.is_server_running.return_value = True

        # Mock session 2
        mock_session_2 = MagicMock()
        mock_session_2._agent_type = "claude"
        mock_session_2.session_id = "new_sid"

        with patch.object(manager, "start_session") as mock_start:
            # First, manually add session 1
            key = manager._session_key("chat1")
            manager._sessions[key] = mock_session_1

            # Now ensure_session with agent_type_override="claude"
            # It should detect mismatch and call start_session
            mock_start.return_value = mock_session_2

            with patch("time.time", return_value=1005):
                session = manager.ensure_session("chat1", agent_type_override="claude")

            self.assertEqual(session, mock_session_2)
            mock_start.assert_called()
            # Verify that the old session was cleaned up (not in _sessions if mock_start is real, but here we mock it)
            # In our implementation, start_session will overwrite it.

    def test_ensure_session_restarts_on_model_mismatch(self):
        manager = ACPSessionManager(agent_type="coco")

        # Mock session with model A
        mock_session = MagicMock()
        mock_session._agent_type = "coco"
        mock_session._agent_args = ["acp", "serve", "-c", "model.name=gpt-4"]
        mock_session.last_active = 1000
        mock_session.is_server_running.return_value = True

        with patch.object(manager, "start_session") as mock_start:
            key = manager._session_key("chat1")
            manager._sessions[key] = mock_session

            # ensure_session with model_name="claude-3"
            with patch("time.time", return_value=1005):
                manager.ensure_session("chat1", model_name="claude-3")

            # Should restart because "claude-3" is not in args
            mock_start.assert_called()

    def test_ensure_session_no_warm_restart_on_server_death(self):
        manager = ACPSessionManager(agent_type="coco")

        # Mock dead session
        mock_session_1 = MagicMock()
        mock_session_1._agent_type = "coco"
        mock_session_1.last_active = 1000
        mock_session_1.is_server_running.return_value = False # Dead

        # Mock new session
        mock_session_2 = MagicMock()
        mock_session_2.warm_restart_msg = ""

        with patch.object(manager, "start_session") as mock_start:
            key = manager._session_key("chat1")
            manager._sessions[key] = mock_session_1
            mock_start.return_value = mock_session_2

            with patch("time.time", return_value=1005):
                session = manager.ensure_session("chat1")

            self.assertEqual(session, mock_session_2)
            self.assertEqual(session.warm_restart_msg, "")

    def test_start_session_inner_delegates_backend_start_to_coordinator(self):
        from src.acp import startup_utils

        manager = ACPSessionManager(agent_type="coco")
        fake_session = MagicMock()
        fake_session.session_id = "sid"
        fake_session.load_local_history.return_value = []

        fake_coordinator = MagicMock()
        fake_coordinator.start.return_value = startup_utils.SessionStartupResult(
            session=fake_session,
            actual_id="sid",
            effective_agent_type="coco",
            model_name="gpt-test",
        )

        with patch.object(startup_utils, "SessionStartupCoordinator", return_value=fake_coordinator):
            session = manager._start_session_inner(
                key=manager._session_key("chat1", "project1"),
                chat_id="chat1",
                cwd="/tmp",
                session_id=None,
                startup_timeout=0.1,
                project_id="project1",
                agent_type_override=None,
                model_name="gpt-test",
                thread_id=None,
            )

        self.assertIs(session, fake_session)
        fake_coordinator.start.assert_called_once()
        start_request = fake_coordinator.start.call_args.args[0]
        self.assertEqual(start_request.key, manager._session_key("chat1", "project1"))
        self.assertEqual(start_request.effective_agent_type, "coco")
        self.assertEqual(start_request.cwd, "/tmp")
        self.assertEqual(start_request.model_name, "gpt-test")
        self.assertIs(manager._sessions[manager._session_key("chat1", "project1")], fake_session)

    def test_start_session_inner_fatal_startup_has_no_success_side_effects(self):
        from src.acp import startup_utils

        telemetry = MagicMock()
        manager = ACPSessionManager(agent_type="coco", session_telemetry=telemetry)
        fake_coordinator = MagicMock()
        fake_coordinator.start.side_effect = RuntimeError("fatal startup")
        key = manager._session_key("chat1", "project1")

        with patch.object(startup_utils, "SessionStartupCoordinator", return_value=fake_coordinator):
            with self.assertRaises(RuntimeError):
                manager._start_session_inner(
                    key=key,
                    chat_id="chat1",
                    cwd="/tmp",
                    session_id=None,
                    startup_timeout=0.1,
                    project_id="project1",
                    agent_type_override=None,
                    model_name="gpt-test",
                    thread_id=None,
                )

        self.assertNotIn(key, manager._sessions)
        telemetry.on_session_start.assert_not_called()
        telemetry.on_session_start_failed.assert_not_called()

    def test_start_session_releases_key_lock_after_success(self):
        manager = ACPSessionManager(agent_type="coco")
        fake_session = MagicMock()

        with patch.object(manager, "_start_session_inner", return_value=fake_session):
            assert manager.start_session("chat-lock", startup_timeout=0.1) is fake_session

        self.assertEqual(manager._key_locks, {})

    def test_start_session_releases_key_lock_after_failure(self):
        manager = ACPSessionManager(agent_type="coco")

        with patch.object(manager, "_start_session_inner", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                manager.start_session("chat-lock", startup_timeout=0.1)

        self.assertEqual(manager._key_locks, {})

    def test_start_session_releases_key_lock_ref_after_acquire_timeout(self):
        manager = ACPSessionManager(agent_type="coco")
        key = manager._session_key("chat-timeout")
        held_lock = manager._get_key_lock(key)
        held_lock.acquire()

        try:
            with self.assertRaises(TimeoutError):
                manager.start_session("chat-timeout", startup_timeout=0.01)

            self.assertIn(key, manager._key_locks)
            self.assertEqual(manager._key_locks[key][1], 1)
        finally:
            held_lock.release()
            manager._release_key_lock(key)

        self.assertEqual(manager._key_locks, {})

    def test_start_session_releases_key_lock_after_concurrent_starts(self):
        manager = ACPSessionManager(agent_type="coco")
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_start(*args, **kwargs):
            calls.append(args[1])
            entered.set()
            release.wait(timeout=2)
            return MagicMock()

        with patch.object(manager, "_start_session_inner", side_effect=fake_start):
            first = threading.Thread(target=lambda: manager.start_session("chat-lock", startup_timeout=1), daemon=True)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second_result: list[object] = []
            second = threading.Thread(
                target=lambda: second_result.append(manager.start_session("chat-lock", startup_timeout=1)),
                daemon=True,
            )
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(second_result), 1)
        self.assertEqual(manager._key_locks, {})

    def test_manager_uses_public_idle_health_facade_only(self):
        root = Path(__file__).resolve().parents[1]
        manager_source = (root / "src" / "acp" / "manager.py").read_text(encoding="utf-8")

        assert "_classify_idle_health_for_manager" not in manager_source
        assert "classify_manager_idle_health" in manager_source
        assert "IdleHealthConfig._resolve_for_manager" not in manager_source

    def test_manager_has_no_ttadk_startup_private_responsibilities(self):
        root = Path(__file__).resolve().parents[1]
        manager_source = (root / "src" / "acp" / "manager.py").read_text(encoding="utf-8")
        startup_source = (root / "src" / "acp" / "startup_utils.py").read_text(encoding="utf-8")

        assert "precheck_ttadk_startup_model" not in manager_source
        assert "_degrade_ttadk" not in manager_source
        assert "precheck_ttadk_startup_model" in startup_source
        assert "_degrade_ttadk" not in startup_source

    def test_public_idle_health_facade_matches_legacy_classification(self):
        from src.acp import telemetry
        from src.utils.time_ago import TimeAgoBucket

        bucket = TimeAgoBucket(label="刚刚", seconds=1.0, level="fresh")

        assert telemetry.classify_manager_idle_health(bucket) == telemetry._classify_idle_health_for_manager(bucket)

if __name__ == "__main__":
    unittest.main()
