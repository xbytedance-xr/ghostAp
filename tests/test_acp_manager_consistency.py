import pytest
import unittest
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

if __name__ == "__main__":
    unittest.main()
