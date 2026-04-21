import unittest
from unittest.mock import MagicMock, patch
from src.spec_engine.engine import SpecEngine, SpecProjectStatus
from src.spec_engine.validation import SpecInput
from pydantic import ValidationError

class TestSpecValidation(unittest.TestCase):
    def setUp(self):
        self.engine = SpecEngine(
            chat_id="test_chat",
            root_path="/tmp/test_project",
            agent_type="test_agent"
        )
        self.engine.settings = MagicMock()
        self.engine.settings.spec_max_cycles = 5

    def test_valid_input(self):
        # Should not raise exception
        input_data = SpecInput(requirement_text="Valid requirement", task_id="task_123")
        self.assertEqual(input_data.requirement_text, "Valid requirement")
        self.assertEqual(input_data.task_id, "task_123")

    def test_empty_requirement(self):
        with self.assertRaises(ValidationError):
            SpecInput(requirement_text="", task_id="task_123")

    def test_long_requirement(self):
        with self.assertRaises(ValidationError):
            SpecInput(requirement_text="a" * 50001, task_id="task_123")

    @patch("src.spec_engine.engine.SpecProject")
    def test_engine_execute_validation_failure(self, mock_project_cls):
        # Setup mock project
        mock_project = MagicMock()
        mock_project_cls.create.return_value = mock_project
        
        callbacks = MagicMock()
        
        # Execute with empty requirement
        result = self.engine.execute(requirement_text="", callbacks=callbacks)
        
        # Verify validation error handling
        self.assertEqual(result, mock_project)
        self.assertEqual(mock_project.status, SpecProjectStatus.ABORTED)
        self.assertTrue("非法配置参数" in mock_project.error)
        callbacks.on_error.assert_called_once()
        
    @patch("src.spec_engine.engine.SpecProject")
    def test_engine_execute_valid_input(self, mock_project_cls):
        # Setup mock project
        mock_project = MagicMock()
        mock_project_cls.create.return_value = mock_project
        
        # Setup mock session to avoid actual execution
        mock_session = MagicMock()
        self.engine._create_session_fn = MagicMock(return_value=mock_session)
        
        # Mock run_cycle_loop to return immediately
        self.engine._run_cycle_loop = MagicMock(return_value="success")
        self.engine._parse_acceptance_criteria = MagicMock(return_value=["Criteria 1"])
        
        callbacks = MagicMock()
        
        # Execute with valid requirement
        result = self.engine.execute(requirement_text="Valid requirement", callbacks=callbacks)
        
        # Verify no validation error
        self.assertNotEqual(mock_project.status, SpecProjectStatus.ABORTED)
        callbacks.on_error.assert_not_called()

if __name__ == "__main__":
    unittest.main()
