
import unittest
from src.deep_engine.executor import TaskExecutor
from src.deep_engine.models import DeepTask

class MockSession:
    pass

class TestExecutorRobustness(unittest.TestCase):
    def setUp(self):
        self.executor = TaskExecutor(MockSession(), ".")

    def test_check_success_basic(self):
        self.assertTrue(self.executor._check_success("✅ Done"))
        self.assertTrue(self.executor._check_success("Mission Completed")) # 'Completed' is not in the list, but 'Success' is. Wait, 'Completed' is not. '完成' is.
        self.assertFalse(self.executor._check_success("❌ Error occurred"))

    def test_check_success_false_positives(self):
        # Case where "Error" is printed but it's actually successful
        output = """
        Running checks...
        Error: calculated value is 0 (expected > 0) -> adjusted to 1.
        ✅ Final Status: Success
        """
        # Current logic: if "Error:" is present, it returns False UNLESS "✅" is AFTER "Error:"
        self.assertTrue(self.executor._check_success(output))

    def test_check_success_false_negatives(self):
        # Case where it succeeds but uses words not in the list
        self.assertTrue(self.executor._check_success("The task is finished.")) # Might fail

    def test_check_success_ambiguous(self):
        output = "I tried to run it but failed to connect."
        self.assertFalse(self.executor._check_success(output))

if __name__ == '__main__':
    unittest.main()
